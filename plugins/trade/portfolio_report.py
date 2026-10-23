"""Read-only multi-exchange portfolio aggregation for Summative Report.

This module sits beside TradeDesk. It:

* walks every discovered exchange/account through existing read-only
  ``balance`` and ``positions_orders`` operations only
* never places, cancels, closes, transfers, or mutates account settings
* normalizes open positions for cross-venue aggregation
* marks portfolio-wide totals incomplete when any account fetch fails

No daemon, no database, no write path.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Bounded fan-out for live desk reads. Keep small: many venues rate-limit.
_DEFAULT_MAX_WORKERS = 6
_READ_OPS = frozenset({"balance", "positions_orders"})

_SIDE_BUY = frozenset({"buy", "long", "b", "l"})
_SIDE_SELL = frozenset({"sell", "short", "s"})

_SYMBOL_STRIP_RE = re.compile(
    r"(?:[-_/]?(?:PERP|SWAP|USD\.P|USDC\.P|USDT\.P|\.P))$",
    re.IGNORECASE,
)
_PREFIX_STRIP_RE = re.compile(r"^(?:PERP|SWAP)[-_/]", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")


def _d(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text in {"—", "-", "None", "null", "nan", "NaN"}:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _q2(value: Optional[Decimal]) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return value


def normalize_side(side: Any) -> str:
    text = str(side or "").strip().lower()
    if text in _SIDE_BUY:
        return "BUY"
    if text in _SIDE_SELL:
        return "SELL"
    return (text.upper() or "?")


# EVM contract addresses: 0x + 40 hex chars (20 bytes) for ERC-20 tokens.
# Some venues (Aftermath) quote perpetual markets by a 32-byte market id,
# so accept 40 *or* 64 hex body chars.
_CONTRACT_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")


def is_opaque_exchange_identifier(value: Any) -> bool:
    """True when an exchange instrument is an opaque id, not a ticker.

    On-chain venues (Aftermath and similar wrappers) identify a market by its
    contract address.  An address carries no base/quote/market-type
    information, so it must never become a cross-exchange aggregation key
    when a human-readable canonical symbol exists: doing so splits one
    underlying asset into two visually identical rows.

    It remains perfectly usable as a *within-venue* identity component for
    position de-duplication.
    """
    text = str(value or "").strip()
    return bool(_CONTRACT_ADDRESS_RE.match(text))


def normalize_instrument_key(
    symbol: Any,
    exchange_instrument: Any = None,
) -> str:
    """Normalize venue-specific aliases into a stable aggregation key.

    Preference order:
    1. Display symbol — authoritative for cross-exchange grouping.
    2. ``exchange_instrument`` base token, but ONLY when it is a readable
       ticker.  Contract addresses and other opaque ids are skipped; they
       cannot establish instrument identity across venues.

    A contract address is never used as the aggregation key while a reliable
    canonical symbol is available.  When neither side yields a usable base
    token, the address is returned uppercased so it still forms a stable,
    collision-free group rather than merging into an unrelated ticker.
    """
    sym = str(symbol or "").strip()
    ei = str(exchange_instrument or "").strip()

    if sym and not is_opaque_exchange_identifier(sym):
        key = _base_token(sym)
        if key:
            return key
    if ei and not is_opaque_exchange_identifier(ei):
        key = _base_token(ei)
        if key:
            return key
    if sym:
        key = _base_token(sym)
        if key:
            return key
    if ei:
        # Opaque id with no usable symbol: keep it as its own group.
        return ei.upper()
    return "?"


def _base_token(raw: str) -> Optional[str]:
    """Reduce one raw venue string to its base asset token.

    Returns ``None`` when the string carries no usable base token.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    # HIP-3 / namespaced forms: xyz:SP500 -> SP500; k:BTC -> BTC
    if ":" in text:
        text = text.rsplit(":", 1)[-1]
    text = _PREFIX_STRIP_RE.sub("", text)
    # Common quote pair forms: BTC-USD.P, BTCUSDT, ETH-USDC, PERP_ZEC_USDC
    upper = text.upper()
    for quote in ("USDT", "USDC", "USD", "USDG", "DAI", "FDUSD"):
        if upper.endswith(quote) and len(upper) > len(quote):
            upper = upper[: -len(quote)]
            break
    upper = _SYMBOL_STRIP_RE.sub("", upper)
    upper = upper.replace("_", "-")
    parts = [p for p in upper.split("-") if p and p not in {"PERP", "SWAP", "P"}]
    if parts:
        # Prefer first non-quote token as base.
        base = parts[0]
        base = _NON_ALNUM_RE.sub("", base)
        if base:
            return base
    cleaned = _NON_ALNUM_RE.sub("", upper)
    return cleaned or None


def _account_parts(entry: Any) -> Tuple[Optional[str], str]:
    if isinstance(entry, str):
        alias = entry.strip()
        return (alias or None, alias)
    if isinstance(entry, dict):
        alias = str(entry.get("account", "")).strip()
        if not alias:
            return (None, "")
        label = str(entry.get("label", alias)).strip() or alias
        chain = str(entry.get("chain", "")).strip()
        if chain:
            label = f"{label} ({chain})"
        return (alias, label)
    return (None, "")


def _account_identity(exchange: str, entry: Any) -> str:
    """Stable identity used for de-duplication across list_accounts shapes."""
    if isinstance(entry, str):
        return f"{exchange}|{entry.strip().lower()}|"
    if isinstance(entry, dict):
        alias = str(entry.get("account", "")).strip().lower()
        chain = str(entry.get("chain", "")).strip().lower()
        return f"{exchange}|{alias}|{chain}"
    return f"{exchange}|{str(entry)}|"


def _monetary_normalization(exchange: str, unit: Optional[str]) -> Tuple[str, Decimal]:
    """Return normalized currency + factor without global currency assumptions.

    MetaTrader's documented ``USC`` denomination is cents, not an ISO
    currency: only that confirmed exchange/unit pair converts 100 USC to USD.
    Every other unit remains distinct.
    """
    source = str(unit or "?").strip() or "?"
    if str(exchange).strip().lower() == "metatrader" and source.upper() == "USC":
        return "USD", Decimal("0.01")
    return source, Decimal("1")


def _normalize_money(exchange: str, value: Optional[Decimal], unit: Optional[str]) -> Tuple[Optional[Decimal], str, Optional[Decimal], str]:
    normalized_unit, factor = _monetary_normalization(exchange, unit)
    native_unit = str(unit or "?").strip() or "?"
    return (None if value is None else value * factor, normalized_unit, value, native_unit)


@dataclass(frozen=True)
class OpenPositionRow:
    exchange: str
    account: str
    account_label: str
    symbol: str
    instrument_key: str
    side: str
    size: Optional[Decimal]
    entry_price: Optional[Decimal]
    mark: Optional[Decimal]
    pnl: Optional[Decimal]
    pnl_unit: str
    native_pnl: Optional[Decimal] = None
    native_pnl_unit: str = "?"
    exchange_instrument: Optional[str] = None
    # CanonicalPosition does not expose multiplier/inverse semantics.  Never
    # infer notional from size×price until agents supply verified metadata.
    notional: Optional[Decimal] = None
    identity: str = ""
    # === Provenance (additive, default None = unknown) =====================
    # Populated ONLY from verified canonical/exchange metadata.  None means
    # "the provider did not report it" and must never be read as a value,
    # never inferred from the ticker, and never treated as proof of
    # contract equivalence.
    quantity_unit: Optional[str] = None      # e.g. "BTC" (base units)
    market_type: Optional[str] = None        # "perp" | "spot" | ...
    contract_multiplier: Optional[Decimal] = None  # verified size->base factor
    contract_spec: Optional[str] = None      # venue contract specification id
    # Original broker-native lot size, preserved whenever a conversion was
    # applied so the raw exposure is never lost or silently overwritten.
    raw_lots: Optional[Decimal] = None
    broker_server: Optional[str] = None
    broker_company: Optional[str] = None
    calc_mode: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exchange": self.exchange,
            "account": self.account,
            "account_label": self.account_label,
            "symbol": self.symbol,
            "instrument_key": self.instrument_key,
            "side": self.side,
            "size": None if self.size is None else format(self.size, "f"),
            "entry_price": None if self.entry_price is None else format(self.entry_price, "f"),
            "mark": None if self.mark is None else format(self.mark, "f"),
            "pnl": None if self.pnl is None else format(self.pnl, "f"),
            "pnl_unit": self.pnl_unit,
            "native_pnl": None if self.native_pnl is None else format(self.native_pnl, "f"),
            "native_pnl_unit": self.native_pnl_unit,
            "exchange_instrument": self.exchange_instrument,
            "notional": None if self.notional is None else format(self.notional, "f"),
            "identity": self.identity,
            "quantity_unit": self.quantity_unit,
            "market_type": self.market_type,
            "contract_multiplier": (
                None if self.contract_multiplier is None
                else format(self.contract_multiplier, "f")
            ),
            "contract_spec": self.contract_spec,
            "raw_lots": None if self.raw_lots is None else format(self.raw_lots, "f"),
            "broker_server": self.broker_server,
            "broker_company": self.broker_company,
            "calc_mode": self.calc_mode,
        }


@dataclass
class AccountSnapshot:
    exchange: str
    account: str
    account_label: str
    identity: str
    ok: bool = False
    balance: Optional[Decimal] = None
    balance_unit: Optional[str] = None
    equity: Optional[Decimal] = None
    available: Optional[Decimal] = None
    margin_used: Optional[Decimal] = None
    position_value: Optional[Decimal] = None
    equity_unit: Optional[str] = None
    positions: List[OpenPositionRow] = field(default_factory=list)
    floating_pnl: Optional[Decimal] = None
    floating_pnl_unit: Optional[str] = None
    position_pnl_native_unit: Optional[str] = None
    errors: List[str] = field(default_factory=list)
    balance_ok: bool = False
    positions_ok: bool = False
    positions_empty: bool = False
    # True when the venue returned some positions but could not cover every
    # sub-source it queried (e.g. one Hyperliquid perp dex failed).  Such an
    # account is NOT full coverage: its totals are a floor, not a complete
    # picture, and the report must say so rather than imply zero elsewhere.
    positions_partial: bool = False
    partial_detail: List[str] = field(default_factory=list)
    # True when positions were fully read but the protective/open-order
    # lookup failed.  Position coverage is UNAFFECTED; this is surfaced on its
    # own so a missing TP/SL order is never confused with missing exposure.
    orders_partial: bool = False
    orders_partial_detail: List[str] = field(default_factory=list)
    native_monetary: Dict[str, Dict[str, Optional[Decimal]]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exchange": self.exchange,
            "account": self.account,
            "account_label": self.account_label,
            "identity": self.identity,
            "ok": self.ok,
            "balance": None if self.balance is None else format(self.balance, "f"),
            "balance_unit": self.balance_unit,
            "equity": None if self.equity is None else format(self.equity, "f"),
            "available": None if self.available is None else format(self.available, "f"),
            "margin_used": None if self.margin_used is None else format(self.margin_used, "f"),
            "position_value": None if self.position_value is None else format(self.position_value, "f"),
            "equity_unit": self.equity_unit,
            "positions": [p.to_dict() for p in self.positions],
            "floating_pnl": None if self.floating_pnl is None else format(self.floating_pnl, "f"),
            "floating_pnl_unit": self.floating_pnl_unit,
            "position_pnl_native_unit": self.position_pnl_native_unit,
            "errors": list(self.errors),
            "balance_ok": self.balance_ok,
            "positions_ok": self.positions_ok,
            "positions_empty": self.positions_empty,
            "positions_partial": self.positions_partial,
            "partial_detail": list(self.partial_detail),
            "orders_partial": self.orders_partial,
            "orders_partial_detail": list(self.orders_partial_detail),
            "native_monetary": {
                metric: {unit: None if value is None else format(value, "f") for unit, value in units.items()}
                for metric, units in self.native_monetary.items()
            },
        }


_CONTRACT_SPEC_TTL_SECONDS = 900.0
_CONTRACT_SPEC_LOCK = threading.Lock()
_CONTRACT_SPEC_CACHE: Dict[Tuple[str, str, str], Tuple[float, Optional[Dict[str, Any]]]] = {}

# MT5 calc modes we can interpret as a linear underlying-quantity contract.
# SYMBOL_CALC_MODE_FOREX (0) and SYMBOL_CALC_MODE_FOREX_NO_LEVERAGE (1) are
# currency-margin instruments; SYMBOL_CALC_MODE_FUTURES (2) is exchange
# style.  Calc mode 5 (SYMBOL_CALC_MODE_EXCH_FUTURES) is also linear.
# Anything else (e.g. CFD/quanto/synthetic) stays unverified by design.
_LINEAR_CALC_MODES = frozenset({0, 1, 2, 5})


def _contract_spec_key(exchange: str, account: str, symbol: str) -> Tuple[str, str, str]:
    return (str(exchange or "").strip().lower(),
            str(account or "").strip().upper(),
            str(symbol or "").strip().upper())


def resolve_contract_spec(
    desk: Any,
    exchange: str,
    account: str,
    symbol: str,
    *,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Return broker contract metadata for one (exchange, account, symbol).

    Read-only: issues a single ``symbol_info`` call through the existing
    agent dispatch and caches the result briefly.  Returns ``None`` when the
    venue has no contract specification concept or the lookup fails — never
    a fabricated default.
    """
    key = _contract_spec_key(exchange, account, symbol)
    if not key[1] or not key[2]:
        return None
    current = time.time() if now is None else now
    with _CONTRACT_SPEC_LOCK:
        cached = _CONTRACT_SPEC_CACHE.get(key)
        if cached is not None and (current - cached[0]) < _CONTRACT_SPEC_TTL_SECONDS:
            return cached[1]
    try:
        response = desk.execute(
            {
                "operation": "symbol_info",
                "exchange": key[0],
                "account": key[1],
                "symbol": key[2],
            }
        )
    except Exception:  # noqa: BLE001
        response = None
    spec: Optional[Dict[str, Any]] = None
    if response is not None and getattr(response, "success", False):
        data = getattr(response, "data", None) or {}
        contract_size = _d(data.get("trade_contract_size"))
        if contract_size is not None and contract_size > 0:
            calc_mode = data.get("trade_calc_mode")
            try:
                calc_mode_int = int(calc_mode) if calc_mode is not None else None
            except (TypeError, ValueError):
                calc_mode_int = None
            spec = {
                "trade_contract_size": contract_size,
                "trade_calc_mode": calc_mode_int,
                "currency_base": data.get("currency_base"),
                "currency_profit": data.get("currency_profit"),
                "currency_margin": data.get("currency_margin"),
                "volume_step": data.get("volume_step"),
                "volume_min": data.get("volume_min"),
                "volume_max": data.get("volume_max"),
                "tick_size": data.get("tick_size"),
                "server": data.get("server"),
                "company": data.get("company"),
                "broker": data.get("server"),
            }
    with _CONTRACT_SPEC_LOCK:
        _CONTRACT_SPEC_CACHE[key] = (current, spec)
    return spec


def contract_spec_is_linear(spec: Optional[Dict[str, Any]]) -> bool:
    """True only when the spec establishes a linear underlying quantity.

    Requires a positive contract size AND a known calculation mode we can
    interpret.  Unknown calc mode is treated as NOT linear: guessing here is
    exactly what produced the original wrong aggregation.
    """
    if not spec:
        return False
    size = _d(spec.get("trade_contract_size"))
    if size is None or size <= 0:
        return False
    mode = spec.get("trade_calc_mode")
    if mode is None:
        return False
    return int(mode) in _LINEAR_CALC_MODES


def underlying_quantity_from_lots(
    lots: Optional[Decimal],
    spec: Optional[Dict[str, Any]],
) -> Optional[Decimal]:
    """Convert MT5 lots to underlying units only for verified linear specs."""
    if lots is None or not contract_spec_is_linear(spec):
        return None
    size = _d(spec.get("trade_contract_size"))
    if size is None or size <= 0:
        return None
    return abs(lots) * size


def is_account_eligible(account: AccountSnapshot) -> bool:
    """Return True when an account is relevant enough to display.

    Eligible means at least one of:
      * a successfully retrieved, *meaningful* balance or equity, or
      * one or more confirmed open positions.

    Design rules (deliberate, do not "simplify" these away):

    * A failed or unsupported response is never treated as zero.  The
      ``balance_ok`` / ``positions_ok`` flags are set only from a successful
      canonical response, so an error can never satisfy eligibility.
    * A successful zero-balance/zero-equity response with no open positions
      does NOT qualify — an empty account is not relevant.
    * "Meaningful" means any non-zero successfully retrieved figure.  We do
      not impose a dust threshold, because the reader decides what counts as
      relevant and inventing one would silently hide real balances.
    """
    if account.positions_ok and account.positions:
        # Confirmed open positions always qualify.
        return True
    if account.balance_ok:
        for value in (account.equity, account.balance, account.available):
            if value is not None and value != 0:
                return True
    return False


@dataclass
class SymbolSideAggregate:
    instrument_key: str
    side: str
    volume: Decimal = Decimal("0")
    pnl: Decimal = Decimal("0")
    pnl_any: bool = False
    notional: Decimal = Decimal("0")
    notional_any: bool = False
    units: Dict[str, Decimal] = field(default_factory=dict)
    contributors: List[OpenPositionRow] = field(default_factory=list)

    @property
    def display_symbol(self) -> str:
        if self.contributors:
            return self.contributors[0].symbol or self.instrument_key
        return self.instrument_key


@dataclass
class SideVwap:
    """Quantity-weighted average entry price for one side of one group.

    Built ONLY from normalized underlying quantities, so a MetaTrader lot is
    never mixed with a token-denominated amount.  ``covered`` / ``total``
    expose how many positions actually contributed, so an incomplete average
    is reported as incomplete rather than silently looking authoritative.
    """

    quantity: Decimal = Decimal("0")
    notional: Decimal = Decimal("0")
    covered: int = 0
    total: int = 0

    @property
    def vwap(self) -> Optional[Decimal]:
        if self.quantity <= 0:
            return None
        return self.notional / self.quantity

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.covered == self.total


@dataclass
class PortfolioSnapshot:
    fetched_at: float
    accounts: List[AccountSnapshot] = field(default_factory=list)
    positions: List[OpenPositionRow] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    accounts_seen: int = 0
    accounts_ok: int = 0
    incomplete: bool = False
    max_workers: int = _DEFAULT_MAX_WORKERS

    @property
    def positions_accounts_seen(self) -> int:
        return len(self.accounts)

    @property
    def positions_accounts_ok(self) -> int:
        return sum(1 for account in self.accounts if account.positions_ok)

    @property
    def positions_accounts_partial(self) -> int:
        """Accounts that answered but could not cover every sub-source."""
        return sum(
            1 for account in self.accounts
            if account.positions_ok and getattr(account, "positions_partial", False)
        )

    @property
    def positions_accounts_full(self) -> int:
        """Accounts with a complete position read (partial or failed excluded)."""
        return sum(
            1 for account in self.accounts
            if account.positions_ok and not getattr(account, "positions_partial", False)
        )

    @property
    def positions_accounts_unavailable(self) -> int:
        """Accounts whose positions could not be retrieved at all."""
        return sum(1 for account in self.accounts if not account.positions_ok)

    @property
    def partial_accounts(self) -> List[AccountSnapshot]:
        return [
            account for account in self.accounts
            if getattr(account, "positions_partial", False)
        ]

    @property
    def orders_partial_accounts(self) -> List[AccountSnapshot]:
        """Accounts whose protective/open-order read was incomplete.

        Positions for these accounts are still fully known, so this is
        reported separately and does NOT reduce position coverage.
        """
        return [
            account for account in self.accounts
            if getattr(account, "orders_partial", False)
        ]

    @property
    def positions_incomplete(self) -> bool:
        # Partial coverage counts as incomplete even when every account
        # answered, because "answered" is not the same as "complete".
        return (
            self.positions_accounts_ok < self.positions_accounts_seen
            or self.positions_accounts_partial > 0
        )

    @property
    def position_failures(self) -> List[AccountSnapshot]:
        return [account for account in self.accounts if not account.positions_ok]

    # ------------------------------------------------------------------
    # Eligibility
    #
    # An account is *eligible for display* when it has at least one of:
    #   * a successfully retrieved, meaningful balance or equity, or
    #   * one or more confirmed open positions.
    #
    # A failed/unsupported response is NEVER read as zero: eligibility is
    # gated on `balance_ok` / `positions_ok`, which are only set from a
    # successful canonical response.  A successful zero-balance response
    # with no open positions therefore does not qualify.
    # ------------------------------------------------------------------

    @property
    def eligible_accounts(self) -> List[AccountSnapshot]:
        """Accounts with a real balance/equity or >=1 confirmed position."""
        return [a for a in self.accounts if is_account_eligible(a)]

    @property
    def eligible_exchanges(self) -> List[str]:
        """Distinct exchanges holding at least one eligible account."""
        return sorted({a.exchange for a in self.eligible_accounts})

    @property
    def position_bearing_accounts(self) -> List[AccountSnapshot]:
        """Accounts with >=1 confirmed open position."""
        return [a for a in self.accounts if a.positions_ok and a.positions]

    def filtered(
        self,
        exchange: Optional[str] = None,
        account: Optional[str] = None,
    ) -> "PortfolioSnapshot":
        ex = (exchange or "").strip()
        ac = (account or "").strip()
        accounts = [
            a
            for a in self.accounts
            if (not ex or a.exchange == ex) and (not ac or a.account == ac)
        ]
        identities = {a.identity for a in accounts}
        positions = [p for p in self.positions if f"{p.exchange}|{p.account}|" in {
            f"{a.exchange}|{a.account}|" for a in accounts
        } or any(
            a.exchange == p.exchange and a.account == p.account for a in accounts
        )]
        # Prefer identity match via account list rebuild
        positions = []
        for a in accounts:
            positions.extend(a.positions)
        snap = PortfolioSnapshot(
            fetched_at=self.fetched_at,
            accounts=accounts,
            positions=positions,
            errors=[e for e in self.errors if (not ex or e.startswith(ex))],
            accounts_seen=len(accounts),
            accounts_ok=sum(1 for a in accounts if a.balance_ok and a.positions_ok),
            incomplete=any(not a.positions_ok for a in accounts)
            or any(getattr(a, "positions_partial", False) for a in accounts)
            or bool(self.errors and accounts),
            max_workers=self.max_workers,
        )
        if identities and any(not a.positions_ok for a in accounts):
            snap.incomplete = True
        return snap


def _estimate_notional(size: Optional[Decimal], mark: Optional[Decimal], entry: Optional[Decimal]) -> Optional[Decimal]:
    if size is None:
        return None
    px = mark if mark is not None else entry
    if px is None:
        return None
    try:
        return abs(size) * abs(px)
    except Exception:  # noqa: BLE001
        return None


def _position_identity(exchange: str, account: str, position: Any) -> str:
    symbol = str(getattr(position, "symbol", "") or "")
    side = normalize_side(getattr(position, "side", ""))
    size = str(getattr(position, "size", "") or "")
    entry = str(getattr(position, "entry_price", "") or "")
    ei = str(getattr(position, "exchange_instrument", "") or "")
    return f"{exchange}|{account}|{ei or symbol}|{side}|{size}|{entry}"


def _extract_account_snapshot(
    *,
    exchange: str,
    account: str,
    account_label: str,
    identity: str,
    desk: Any,
) -> AccountSnapshot:
    snap = AccountSnapshot(
        exchange=exchange,
        account=account,
        account_label=account_label or account,
        identity=identity,
    )
    unit_hint = "USD"

    # balance
    try:
        bal_resp = desk.execute(
            {"operation": "balance", "exchange": exchange, "account": account}
        )
    except Exception as exc:  # noqa: BLE001
        snap.errors.append(f"{exchange}/{account_label}: balance error ({exc})")
        bal_resp = None

    if bal_resp is not None and getattr(bal_resp, "success", False):
        snap.balance_ok = True
        raw_unit: Optional[str] = None
        bal = getattr(bal_resp, "balance", None)
        if bal is not None:
            raw_balance = _d(getattr(bal, "value", None))
            raw_unit = str(getattr(bal, "unit", None) or "").strip() or None
            snap.balance, snap.balance_unit, native_value, native_unit = _normalize_money(exchange, raw_balance, raw_unit)
            snap.native_monetary["balance"] = {native_unit: native_value}
            if snap.balance_unit:
                unit_hint = snap.balance_unit
        summary = getattr(bal_resp, "portfolio_summary", None)
        if summary is not None:
            raw_summary_unit = str(getattr(summary, "unit", None) or raw_unit or unit_hint).strip() or unit_hint
            snap.position_pnl_native_unit = raw_summary_unit
            snap.equity, snap.equity_unit, native_equity, native_unit = _normalize_money(exchange, _d(getattr(summary, "account_value", None)), raw_summary_unit)
            snap.available, _, native_available, _ = _normalize_money(exchange, _d(getattr(summary, "withdrawable", None)), raw_summary_unit)
            snap.margin_used, _, native_margin, _ = _normalize_money(exchange, _d(getattr(summary, "margin_used", None)), raw_summary_unit)
            snap.position_value, _, native_position_value, _ = _normalize_money(exchange, _d(getattr(summary, "total_position_value", None)), raw_summary_unit)
            snap.native_monetary.update({
                "equity": {native_unit: native_equity},
                "available": {native_unit: native_available},
                "margin_used": {native_unit: native_margin},
                "position_value": {native_unit: native_position_value},
            })
            unit_hint = snap.equity_unit or unit_hint
        elif snap.balance is not None:
            # Some venues only expose free balance; treat as available, not equity.
            snap.available = snap.balance
            snap.equity_unit = snap.balance_unit
    elif bal_resp is not None:
        err = getattr(bal_resp, "error", None)
        msg = getattr(err, "message", None) if err is not None else None
        snap.errors.append(f"{exchange}/{account_label}: {msg or 'balance unavailable'}")

    # positions_orders (open positions only; ignore order groups / history)
    try:
        pos_resp = desk.execute(
            {
                "operation": "positions_orders",
                "exchange": exchange,
                "account": account,
            }
        )
    except Exception as exc:  # noqa: BLE001
        snap.errors.append(f"{exchange}/{account_label}: positions error ({exc})")
        pos_resp = None

    positions: List[OpenPositionRow] = []
    if pos_resp is not None and getattr(pos_resp, "success", False):
        snap.positions_ok = True
        raw_positions = list(getattr(pos_resp, "positions", None) or [])
        snap.positions_empty = len(raw_positions) == 0
        # A venue may report success while having silently skipped a sub-source
        # (Hyperliquid: one perp dex failing).  That is partial coverage, not a
        # complete answer, so flag it instead of presenting a floor as a total.
        resp_data = getattr(pos_resp, "data", None)
        if isinstance(resp_data, dict) and resp_data.get("partial_coverage"):
            failures = [
                f for f in (resp_data.get("dex_failures") or [])
                if isinstance(f, dict)
            ]
            # An orders-only failure still yields complete POSITIONS, so it is
            # tracked separately and must not reduce position coverage.
            orders_only = [f for f in failures if f.get("partial")]
            pos_failures = [f for f in failures if not f.get("partial")]
            queried = len(resp_data.get("dexes_queried") or [])
            if pos_failures:
                snap.positions_partial = True
                cats = sorted({str(f.get("category") or "unknown") for f in pos_failures})
                snap.partial_detail.append(
                    f"{exchange}/{account}: {len(pos_failures)} of {queried} "
                    f"perp DEXs unavailable ({', '.join(cats)})"
                )
            if orders_only:
                snap.orders_partial = True
                ocats = sorted({str(f.get("category") or "unknown") for f in orders_only})
                snap.orders_partial_detail.append(
                    f"{exchange}/{account}: open orders unavailable on "
                    f"{len(orders_only)} of {queried} perp DEXs ({', '.join(ocats)})"
                )
        seen_pos: set[str] = set()
        pnl_total = Decimal("0")
        pnl_any = False
        for position in raw_positions:
            size = _d(getattr(position, "size", None))
            if size is None or size == 0:
                continue
            side = normalize_side(getattr(position, "side", None))
            symbol = str(getattr(position, "symbol", "") or "").strip() or "?"
            ei = getattr(position, "exchange_instrument", None)
            ei_s = str(ei).strip() if ei is not None else None
            key = normalize_instrument_key(symbol, ei_s)
            entry = _d(getattr(position, "entry_price", None))
            mark = _d(getattr(position, "mark", None))
            native_pnl = _d(getattr(position, "pnl", None))
            pnl_source_unit = snap.position_pnl_native_unit or unit_hint
            pnl, pnl_unit, native_pnl, native_pnl_unit = _normalize_money(exchange, native_pnl, pnl_source_unit)
            pid = _position_identity(exchange, account, position)
            if pid in seen_pos:
                continue
            seen_pos.add(pid)

            # MetaTrader reports `size` in lots.  Only for a verified linear
            # contract specification from THIS account do we also derive an
            # underlying-asset quantity; otherwise the row keeps its native
            # lot unit and stays in its own aggregation group.
            spec = None
            underlying = None
            if str(exchange or "").strip().lower() == "metatrader":
                spec = resolve_contract_spec(desk, exchange, account, symbol)
                underlying = underlying_quantity_from_lots(abs(size), spec)

            quantity_unit = None
            contract_multiplier = None
            # market_type stays None: the EA does not report it, and inferring
            # "perp" from a USD suffix is a guess that would wrongly separate
            # converted MT5 rows from token-denominated venues.  Verified
            # absence is not evidence.
            market_type = None
            contract_spec = None
            if underlying is not None and spec is not None:
                quantity_unit = key
                contract_multiplier = _d(spec.get("trade_contract_size"))
                contract_spec = str(spec.get("server") or spec.get("broker") or "") or None

            row = OpenPositionRow(
                exchange=exchange,
                account=account,
                account_label=account_label or account,
                symbol=symbol,
                instrument_key=key,
                side=side,
                size=(underlying if underlying is not None else abs(size)),
                # raw_lots is the MT5 lot size.  Only set it for a
                # lot-denominated venue: on token venues `size` is already the
                # asset, and marking it as "lots" would both mislabel the row
                # and wrongly split it from other token rows.
                raw_lots=(abs(size) if spec is not None else None),
                entry_price=entry,
                mark=mark,
                pnl=pnl,
                pnl_unit=pnl_unit,
                native_pnl=native_pnl,
                native_pnl_unit=native_pnl_unit,
                exchange_instrument=ei_s or None,
                notional=None,
                identity=pid,
                # Provenance stays None unless the venue verified it.  The
                # positions feed exposes no market_type / quantity_unit /
                # multiplier, so these remain "unknown" for live positions and
                # must never be inferred from the ticker.
                quantity_unit=quantity_unit,
                market_type=market_type,
                contract_multiplier=contract_multiplier,
                contract_spec=contract_spec,
                broker_server=(str(spec.get("server")) if spec and spec.get("server") else None),
                broker_company=(str(spec.get("company")) if spec and spec.get("company") else None),
                calc_mode=(spec.get("trade_calc_mode") if spec else None),
            )
            positions.append(row)
            if pnl is not None:
                pnl_total += pnl
                pnl_any = True
        snap.positions = positions
        if pnl_any:
            snap.floating_pnl = pnl_total
            reported_units = {row.pnl_unit for row in positions if row.pnl is not None}
            snap.floating_pnl_unit = next(iter(reported_units)) if len(reported_units) == 1 else None
    elif pos_resp is not None:
        err = getattr(pos_resp, "error", None)
        msg = getattr(err, "message", None) if err is not None else None
        snap.errors.append(
            f"{exchange}/{account_label}: positions {msg or 'unavailable'}"
        )

    snap.ok = snap.balance_ok or snap.positions_ok
    return snap


def collect_portfolio_snapshot(
    desk: Any,
    *,
    max_workers: int = _DEFAULT_MAX_WORKERS,
    exchanges: Optional[Sequence[str]] = None,
    progress: Optional[Callable[[int, int, Optional[str]], None]] = None,
) -> PortfolioSnapshot:
    """Fetch open-position + equity snapshot across TradeDesk accounts.

    Strictly read-only: only ``balance`` and ``positions_orders``.

    ``progress`` (optional) is invoked as ``progress(done, total, current)``
    after each account snapshot completes.  ``done`` and ``total`` count
    accounts; ``current`` is the ``exchange/account`` label of the account
    that just finished.  The callback is allowed to raise — failures are
    swallowed so a buggy transport sink cannot break a fetch.
    """
    started = time.time()
    errors: List[str] = []
    targets: List[Tuple[str, str, str, str]] = []  # exchange, account, label, identity
    seen_identities: set[str] = set()

    try:
        exchange_names = list(exchanges) if exchanges is not None else list(desk.list_exchanges() or [])
    except Exception as exc:  # noqa: BLE001
        return PortfolioSnapshot(
            fetched_at=started,
            errors=[f"list_exchanges failed ({exc})"],
            incomplete=True,
            max_workers=max_workers,
        )

    for exchange in exchange_names:
        try:
            accounts = list(desk.list_accounts(exchange) or [])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{exchange}: list_accounts failed ({exc})")
            continue
        for entry in accounts:
            alias, label = _account_parts(entry)
            if not alias:
                continue
            identity = _account_identity(exchange, entry)
            if identity in seen_identities:
                # Same account reachable via multiple config shapes.
                continue
            seen_identities.add(identity)
            targets.append((exchange, alias, label or alias, identity))

    snapshots: List[AccountSnapshot] = []
    workers = max(1, min(int(max_workers or _DEFAULT_MAX_WORKERS), max(1, len(targets))))

    total_targets = len(targets)
    done_targets = 0
    if progress is not None:
        try:
            progress(0, total_targets, None)
        except Exception:  # noqa: BLE001
            logger.exception("portfolio snapshot progress(0) raised")

    def _emit_progress(current_label: str) -> None:
        if progress is None:
            return
        try:
            progress(done_targets, total_targets, current_label)
        except Exception:  # noqa: BLE001
            logger.exception("portfolio snapshot progress(%d) raised", done_targets)

    if targets:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _extract_account_snapshot,
                    exchange=ex,
                    account=ac,
                    account_label=label,
                    identity=ident,
                    desk=desk,
                ): (ex, ac)
                for ex, ac, label, ident in targets
            }
            for fut in as_completed(futures):
                ex, ac = futures[fut]
                try:
                    snapshots.append(fut.result())
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{ex}/{ac}: snapshot failed ({exc})")
                    snapshots.append(
                        AccountSnapshot(
                            exchange=ex,
                            account=ac,
                            account_label=ac,
                            identity=f"{ex}|{ac}|",
                            errors=[f"{ex}/{ac}: snapshot failed ({exc})"],
                        )
                    )
                done_targets += 1
                _emit_progress(f"{ex}/{ac}")

    # Stable order: exchange, account
    snapshots.sort(key=lambda a: (a.exchange.lower(), a.account.lower(), a.account_label.lower()))

    positions: List[OpenPositionRow] = []
    seen_pos: set[str] = set()
    for acct in snapshots:
        errors.extend(acct.errors)
        for pos in acct.positions:
            if pos.identity in seen_pos:
                continue
            seen_pos.add(pos.identity)
            positions.append(pos)

    accounts_ok = sum(1 for a in snapshots if a.balance_ok and a.positions_ok)
    # Position coverage is the completeness gate for every open-position/PnL
    # aggregate. A successful balance cannot turn an unavailable position
    # feed into a zero-position account.
    incomplete = bool(errors) or any(not a.positions_ok for a in snapshots)

    return PortfolioSnapshot(
        fetched_at=started,
        accounts=snapshots,
        positions=positions,
        errors=errors,
        accounts_seen=len(snapshots),
        accounts_ok=accounts_ok,
        incomplete=incomplete,
        max_workers=workers,
    )


def positions_are_quantity_compatible(rows: List[OpenPositionRow]) -> bool:
    """Return True when quantities from every row may be summed.

    Conservative by construction: unknown provenance is NOT proof of
    compatibility.  A row with unverified ``quantity_unit`` therefore blocks
    merging against rows that *do* carry a verified unit, rather than
    silently adding two different denominations.

    Rules:
      * quantity_unit must be known and equal on every row.
      * market_type must be known and equal on every row (spot != perp).
      * contract_multiplier must be known and equal on every row when any
        row reports one.
      * contract_spec must be equal on every row when any row reports one.

    Every row carrying no provenance at all is treated as mutually
    compatible, because with nothing to contradict it there is no evidence
    of incompatibility — this keeps the common case (live positions, which
    currently expose no metadata) aggregating as before rather than
    fragmenting every symbol into one group per venue.
    """
    units = {r.quantity_unit for r in rows if r.quantity_unit is not None}
    if len(units) > 1:
        return False

    market_types = {r.market_type for r in rows if r.market_type is not None}
    if len(market_types) > 1:
        return False

    multipliers = {r.contract_multiplier for r in rows if r.contract_multiplier is not None}
    if len(multipliers) > 1:
        return False

    specs = {r.contract_spec for r in rows if r.contract_spec is not None}
    if len(specs) > 1:
        return False

    return True


def _provenance_suffix(row: OpenPositionRow) -> str:
    """Short human tag distinguishing an otherwise identical-looking group."""
    bits: List[str] = []
    market_type = _provenance(row, "market_type")
    quantity_unit = _provenance(row, "quantity_unit")
    multiplier = _provenance(row, "contract_multiplier")
    spec = _provenance(row, "contract_spec")
    if market_type:
        bits.append(str(market_type))
    if quantity_unit and str(quantity_unit).upper() != str(
        getattr(row, "instrument_key", "") or ""
    ).upper():
        bits.append(str(quantity_unit))
    if multiplier is not None:
        bits.append(f"x{multiplier}")
    if spec:
        bits.append(str(spec))
    if not bits:
        unit = _provenance(row, "quantity_unit")
        if unit is None and _provenance(row, "raw_lots") is not None:
            bits.append("lots")
    if not bits and is_opaque_exchange_identifier(getattr(row, "exchange_instrument", None)):
        bits.append(str(row.exchange_instrument)[:8])
    return "·" + "/".join(bits) if bits else ""


def _accumulate_vwap(acc: SideVwap, pos: OpenPositionRow) -> None:
    """Add one position to a side's VWAP accumulator.

    A position with a missing or non-positive quantity, or a missing /
    non-positive entry price, is counted in ``total`` but contributes nothing
    to the average.  That is what makes ``covered < total`` detectable, so
    the UI can flag an incomplete VWAP instead of publishing a number that
    silently excludes part of the exposure.
    """
    if pos.side != "BUY" and pos.side != "SELL":
        return
    acc.total += 1
    # Coerce defensively: a row rebuilt from a serialized dict can carry
    # strings for numeric fields, and comparing those numerically raises.
    qty = _d(abs(pos.size)) if pos.size is not None else None
    entry = _d(pos.entry_price)
    if qty is None or qty <= 0 or entry is None or entry <= 0:
        return
    acc.quantity += qty
    acc.notional += qty * entry
    acc.covered += 1


def _side_vwap_payload(acc: SideVwap) -> Dict[str, Any]:
    return {
        "vwap": acc.vwap,
        "covered": acc.covered,
        "total": acc.total,
        "complete": acc.complete,
    }


def net_by_instrument(snapshot: PortfolioSnapshot) -> List[Dict[str, Any]]:
    """Aggregate open positions per instrument (combines BUY + SELL).

    Returns a list of dicts in the shape::

        {
            "instrument": str,         # normalized instrument key
            "display_symbol": str,      # canonical symbol for the group
            "gross": Decimal,           # sum of |size| across both sides
            "net": Decimal,            # signed net (BUY − SELL)
            "long": Decimal,           # BUY absolute size
            "short": Decimal,          # SELL absolute size
            "pnl_units": Dict[str, Decimal],  # per-unit floating PnL
            "accounts": int,           # unique exchange|account pairs
        }

    ``gross`` and ``net`` are derived from per-leg absolute/signed size,
    never from ``size * price``: notional requires verified contract
    multiplier/inverse metadata which CanonicalPosition does not supply.

    Grouping is two-stage:

    1. Bucket by normalized ``instrument_key``.  A contract address never
       becomes the key while a canonical symbol exists, so one underlying
       asset cannot split into visually identical rows.
    2. Within a bucket, split further into *compatibility subgroups* using
       verified provenance (quantity unit, market type, contract multiplier,
       contract specification).  Quantities are summed only across rows the
       provenance rules agree are summable.

    Every emitted row gets a unique ``display_symbol``: when a bucket had to
    be split, each subgroup is suffixed with its distinguishing provenance
    so two rows can never render as indistinguishable labels.
    """
    buckets: Dict[str, Dict[str, Any]] = {}
    for pos in snapshot.positions:
        if pos.size is None:
            continue
        slot = buckets.setdefault(
            pos.instrument_key,
            {
                "instrument": pos.instrument_key,
                "display_symbol": pos.symbol or pos.instrument_key,
                "gross": Decimal("0"),
                "net": Decimal("0"),
                "long": Decimal("0"),
                "short": Decimal("0"),
                "pnl_units": {},
                "_count": 0,
                "_symbols": {},
                "_rows": [],
                "_accounts": set(),
            },
        )
        slot["_count"] += 1
        # Deterministic display: the most common contributor symbol wins,
        # not whichever row happened to be visited last.
        symbol = pos.symbol or pos.instrument_key
        slot["_symbols"][symbol] = slot["_symbols"].get(symbol, 0) + 1
        slot["_rows"].append(pos)
        slot["_accounts"].add(f"{pos.exchange}|{pos.account}|")

    rows: List[Dict[str, Any]] = []
    for key, slot in buckets.items():
        subgroups = _compatibility_subgroups(slot["_rows"])

        # Most common contributor symbol, ties broken alphabetically so the
        # label is deterministic across runs.
        symbols: Dict[str, int] = slot["_symbols"]
        canonical_symbol = sorted(symbols.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

        if len(subgroups) == 1:
            groups = [subgroups[0]]
            labels = [canonical_symbol]
        else:
            groups = subgroups
            labels = [
                f"{canonical_symbol}{_provenance_suffix(g[0][0])}"
                for g in subgroups
            ]

        seen_labels: Dict[str, int] = {}
        final_labels: List[str] = []
        for label in labels:
            seen_labels[label] = seen_labels.get(label, 0) + 1
            if seen_labels[label] > 1:
                final_labels.append(f"{label} #{seen_labels[label]}")
            else:
                final_labels.append(label)

        for (group_rows, group_accounts), label in zip(groups, final_labels):
            gross = Decimal("0")
            net = Decimal("0")
            long_amt = Decimal("0")
            short_amt = Decimal("0")
            pnl_units: Dict[str, Decimal] = {}
            buy_vwap = SideVwap()
            sell_vwap = SideVwap()
            for pos in group_rows:
                if pos.size is None:
                    continue
                size_abs = abs(pos.size)
                gross += size_abs
                # VWAP is accumulated per side from normalized quantities only.
                # `size` here is already the underlying quantity for converted
                # MetaTrader rows, so lots never enter the average directly.
                if pos.side == "BUY":
                    long_amt += size_abs
                    net += pos.size
                    _accumulate_vwap(buy_vwap, pos)
                elif pos.side == "SELL":
                    short_amt += size_abs
                    net -= size_abs
                    _accumulate_vwap(sell_vwap, pos)
                else:
                    # Unknown side; treat as long for size accounting, not net.
                    long_amt += size_abs
                if pos.pnl is not None:
                    unit = pos.pnl_unit or "?"
                    pnl_units[unit] = pnl_units.get(unit, Decimal("0")) + pos.pnl
            rows.append(
                {
                    "instrument": slot["instrument"],
                    "display_symbol": label,
                    "gross": gross,
                    "net": net,
                    "long": long_amt,
                    "short": short_amt,
                    "buy_vwap": _side_vwap_payload(buy_vwap),
                    "sell_vwap": _side_vwap_payload(sell_vwap),
                    "pnl_units": pnl_units,
                    "accounts": len(group_accounts),
                }
            )
    return rows


def _provenance(row: Any, field: str) -> Any:
    """Read an optional provenance field defensively.

    Rows may be test stubs or duck-typed stand-ins that predate the
    provenance fields; absence means "unknown", never a value.
    """
    return getattr(row, field, None)


def _quantity_unit_signature(row: Any) -> Optional[str]:
    """Grouping key for a row's quantity denomination.

    Returns a verified underlying unit when the row declares one, a distinct
    sentinel for rows whose native units could not be verified (never merged
    with a token row), or ``None`` for a plain token-denominated position —
    the overwhelmingly common case on perps venues, where the venue reports
    the asset itself and no contract metadata exists or is needed.
    """
    unit = _provenance(row, "quantity_unit")
    if unit:
        return str(unit)
    if _provenance(row, "raw_lots") is not None:
        # Lot-denominated position with no verified contract specification.
        # Distinct unit: it must never be summed with a token position.
        return f"__unverified_lots__:{getattr(row, 'symbol', '') or '?'}"
    # Plain token-denominated position.  Its unit IS the instrument key, so a
    # converted MetaTrader row declaring quantity_unit="HYPE" and a perp venue
    # row with no metadata both denote HYPE and belong in one group.
    key = str(getattr(row, "instrument_key", "") or "")
    return key or None


def _compatibility_subgroups(
    rows: List[OpenPositionRow],
) -> List[Tuple[List[OpenPositionRow], set]]:
    """Split rows into quantity-summable subgroups.

    Rows are grouped by their verified quantity denomination.  Unverified
    lot-denominated rows get a sentinel unit so they stay separate from
    token rows instead of silently merging (or silently defaulting to 1).
    """
    groups: Dict[Tuple[Any, ...], List[OpenPositionRow]] = {}
    order: List[Tuple[Any, ...]] = []
    for pos in rows:
        # Once a row carries a verified underlying quantity, its contract
        # multiplier is the INPUT to that conversion, not a property of the
        # resulting amount.  Two brokers using 100 and 10 per contract both
        # yield plain ZEC, so they merge; the multiplier must not split them.
        sig = (
            _quantity_unit_signature(pos),
            _provenance(pos, "market_type"),
        )
        if sig not in groups:
            groups[sig] = []
            order.append(sig)
        groups[sig].append(pos)
    out: List[Tuple[List[OpenPositionRow], set]] = []
    for sig in order:
        members = groups[sig]
        accounts = {f"{p.exchange}|{p.account}|" for p in members}
        out.append((members, accounts))
    return out


def positions_by_symbol(snapshot: PortfolioSnapshot) -> List[SymbolSideAggregate]:
    groups: Dict[Tuple[str, str], SymbolSideAggregate] = {}
    for pos in snapshot.positions:
        key = (pos.instrument_key, pos.side)
        agg = groups.get(key)
        if agg is None:
            agg = SymbolSideAggregate(instrument_key=pos.instrument_key, side=pos.side)
            groups[key] = agg
        if pos.size is not None:
            agg.volume += pos.size
        if pos.pnl is not None:
            agg.pnl += pos.pnl
            agg.pnl_any = True
            unit = pos.pnl_unit or "?"
            agg.units[unit] = agg.units.get(unit, Decimal("0")) + pos.pnl
        if pos.notional is not None:
            agg.notional += pos.notional
            agg.notional_any = True
        agg.contributors.append(pos)

    rows = list(groups.values())
    rows.sort(
        key=lambda r: (
            -max((abs(value) for value in r.units.values()), default=Decimal("0")),
            r.instrument_key,
            r.side,
        )
    )
    return rows


def pnl_by_exchange(snapshot: PortfolioSnapshot) -> List[Dict[str, Any]]:
    """Aggregate floating PnL by exchange (all accounts under that exchange)."""
    by_ex: Dict[str, Dict[str, Any]] = {}
    # Ensure every known exchange appears, including zero-position successes.
    for acct in snapshot.accounts:
        slot = by_ex.setdefault(
            acct.exchange,
            {
                "exchange": acct.exchange,
                "pnl": Decimal("0"),
                "pnl_any": False,
                "units": {},
                "accounts": [],
                "ok_accounts": 0,
                "total_accounts": 0,
                "incomplete": False,
            },
        )
        slot["total_accounts"] += 1
        if acct.positions_ok:
            slot["ok_accounts"] += 1
        else:
            slot["incomplete"] = True
        slot["accounts"].append(acct)
        for position in acct.positions:
            if position.pnl is None:
                continue
            slot["pnl_any"] = True
            unit = position.pnl_unit or "?"
            units = slot["units"]
            units[unit] = units.get(unit, Decimal("0")) + position.pnl

    rows = list(by_ex.values())
    rows.sort(key=lambda r: (-abs(r["pnl"]) if r["pnl_any"] else Decimal("0"), r["exchange"]))
    return rows


def total_floating_pnl(snapshot: PortfolioSnapshot) -> Dict[str, Any]:
    """Return floating PnL grouped by exact normalized currency unit.

    There is deliberately no scalar portfolio total: USD, USDT, USDC and
    USDT0 remain distinct without an audited conversion source.
    """
    units: Dict[str, Decimal] = {}
    long_units: Dict[str, Decimal] = {}
    short_units: Dict[str, Decimal] = {}
    for pos in snapshot.positions:
        if pos.pnl is None:
            continue
        unit = pos.pnl_unit or "?"
        units[unit] = units.get(unit, Decimal("0")) + pos.pnl
        if pos.side == "BUY":
            long_units[unit] = long_units.get(unit, Decimal("0")) + pos.pnl
        elif pos.side == "SELL":
            short_units[unit] = short_units.get(unit, Decimal("0")) + pos.pnl
    exchanges = sorted({a.exchange for a in snapshot.accounts})
    return {
        "units": units,
        "long_units": long_units,
        "short_units": short_units,
        "open_positions": len(snapshot.positions),
        "exchanges": len(exchanges),
        "accounts": snapshot.positions_accounts_seen,
        "accounts_ok": snapshot.positions_accounts_ok,
        "incomplete": snapshot.positions_incomplete,
        "excluded_accounts": snapshot.position_failures,
    }


def total_equity(snapshot: PortfolioSnapshot) -> Dict[str, Any]:
    equity_units: Dict[str, Decimal] = {}
    available_units: Dict[str, Decimal] = {}
    margin_units: Dict[str, Decimal] = {}
    equity_any = available_any = margin_any = False
    missing_equity: List[str] = []
    for acct in snapshot.accounts:
        label = f"{acct.exchange}/{acct.account_label}"
        unit = acct.equity_unit or acct.balance_unit or "?"
        if acct.equity is not None:
            equity_units[unit] = equity_units.get(unit, Decimal("0")) + acct.equity
            equity_any = True
        else:
            # Fall back to balance only when equity missing, and mark it.
            if acct.balance is not None and acct.balance_ok:
                equity_units[unit] = equity_units.get(unit, Decimal("0")) + acct.balance
                equity_any = True
                missing_equity.append(f"{label}: equity unavailable; balance used")
            elif acct.balance_ok:
                missing_equity.append(f"{label}: equity unavailable")
            else:
                missing_equity.append(f"{label}: account fetch failed")
        if acct.available is not None:
            available_units[unit] = available_units.get(unit, Decimal("0")) + acct.available
            available_any = True
        if acct.margin_used is not None:
            margin_units[unit] = margin_units.get(unit, Decimal("0")) + acct.margin_used
            margin_any = True

    pnl_info = total_floating_pnl(snapshot)
    return {
        "equity_units": equity_units if equity_any else {},
        "available_units": available_units if available_any else {},
        "margin_units": margin_units if margin_any else {},
        "floating_pnl_units": pnl_info["units"],
        "missing": missing_equity,
        "incomplete": snapshot.positions_incomplete or bool(missing_equity),
        "accounts": snapshot.accounts_seen,
        "accounts_ok": sum(1 for account in snapshot.accounts if account.balance_ok),
    }


def risk_and_exposure(snapshot: PortfolioSnapshot) -> Dict[str, Any]:
    gross_long = Decimal("0")
    gross_short = Decimal("0")
    long_any = short_any = False
    net_by_instrument: Dict[str, Dict[str, Any]] = {}
    for pos in snapshot.positions:
        notion = pos.notional
        signed_size = pos.size or Decimal("0")
        if pos.side == "SELL":
            signed_size = -signed_size
        slot = net_by_instrument.setdefault(
            pos.instrument_key,
            {"instrument": pos.instrument_key, "net_size": Decimal("0"), "net_notional": Decimal("0"), "notional_any": False},
        )
        slot["net_size"] += signed_size
        if notion is not None:
            signed_notion = notion if pos.side == "BUY" else -notion
            slot["net_notional"] += signed_notion
            slot["notional_any"] = True
            if pos.side == "BUY":
                gross_long += notion
                long_any = True
            elif pos.side == "SELL":
                gross_short += notion
                short_any = True

    margin_units: Dict[str, Decimal] = {}
    margin_any = False
    equity_units: Dict[str, Decimal] = {}
    elevated: List[Dict[str, Any]] = []
    for acct in snapshot.accounts:
        unit = acct.equity_unit or acct.balance_unit or "?"
        if acct.margin_used is not None:
            margin_units[unit] = margin_units.get(unit, Decimal("0")) + acct.margin_used
            margin_any = True
        eq = acct.equity if acct.equity is not None else acct.balance
        if eq is not None:
            equity_units[unit] = equity_units.get(unit, Decimal("0")) + eq
        if acct.margin_used is not None and eq is not None and eq > 0:
            util = acct.margin_used / eq
            if util >= Decimal("0.70"):
                elevated.append(
                    {
                        "exchange": acct.exchange,
                        "account": acct.account,
                        "account_label": acct.account_label,
                        "utilization": util,
                        "margin_used": acct.margin_used,
                        "equity": eq,
                        "unit": unit,
                    }
                )

    elevated.sort(key=lambda r: r["utilization"], reverse=True)

    losers = [p for p in snapshot.positions if p.pnl is not None and p.pnl < 0]
    losers.sort(key=lambda p: p.pnl if p.pnl is not None else Decimal("0"))  # most negative first

    # Effective leverage only when both margin and equity exist in one unit.
    leverage = None
    leverage_note = None
    if margin_any and equity_units:
        # Only compute when a single unit dominates both sides.
        if len(margin_units) == 1 and len(equity_units) == 1:
            m_unit, m_val = next(iter(margin_units.items()))
            e_unit, e_val = next(iter(equity_units.items()))
            if m_unit == e_unit and e_val and e_val > 0:
                leverage = (m_val / e_val)
            else:
                leverage_note = "Margin/equity units differ; leverage not computed."
        else:
            leverage_note = "Mixed collateral units; leverage not computed."
    else:
        leverage_note = "Margin and/or equity unavailable; leverage not computed."

    net_rows = list(net_by_instrument.values())
    net_rows.sort(key=lambda r: (-abs(r["net_notional"]) if r["notional_any"] else Decimal("0"), r["instrument"]))

    return {
        "gross_long_notional": gross_long if long_any else None,
        "gross_short_notional": gross_short if short_any else None,
        "net_by_instrument": net_rows,
        "margin_units": margin_units if margin_any else {},
        "leverage": leverage,
        "leverage_note": leverage_note,
        "largest_losers": losers[:10],
        "elevated_margin": elevated[:10],
        # Liquidation prices are not on CanonicalPosition — never fabricate.
        "liquidation_supported": False,
        "incomplete": snapshot.positions_incomplete,
        "notional_incomplete": True,
        "notional_note": (
            "Notional is N/A: CanonicalPosition lacks verified contract multiplier, "
            "inverse-contract, and native-quantity metadata."
        ),
    }


def list_exchanges(snapshot: PortfolioSnapshot) -> List[str]:
    """Exchanges that hold at least one eligible account.

    Falls back to every known exchange when nothing is eligible, so an
    empty/unsupported snapshot still renders a usable (empty) view instead
    of an error.
    """
    eligible = snapshot.eligible_exchanges
    if eligible:
        return eligible
    return sorted({a.exchange for a in snapshot.accounts})


def accounts_for_exchange(snapshot: PortfolioSnapshot, exchange: str) -> List[AccountSnapshot]:
    """Eligible accounts on one exchange."""
    return [a for a in snapshot.accounts if a.exchange == exchange and is_account_eligible(a)]


def format_money(value: Optional[Decimal], *, signed: bool = False) -> str:
    if value is None:
        return "—"
    q = _q2(value)
    if q is None:
        return "—"
    if signed:
        sign = "+" if q >= 0 else "-"
        return f"{sign}{format(abs(q), 'f')}"
    return format(q, "f")


def snapshot_age_seconds(snapshot: PortfolioSnapshot, now: Optional[float] = None) -> float:
    now_v = time.time() if now is None else now
    return max(0.0, now_v - float(snapshot.fetched_at))


__all__ = [
    "AccountSnapshot",
    "OpenPositionRow",
    "PortfolioSnapshot",
    "SymbolSideAggregate",
    "accounts_for_exchange",
    "collect_portfolio_snapshot",
    "format_money",
    "is_account_eligible",
    "is_opaque_exchange_identifier",
    "list_exchanges",
    "net_by_instrument",
    "normalize_instrument_key",
    "normalize_side",
    "positions_are_quantity_compatible",
    "pnl_by_exchange",
    "positions_by_symbol",
    "risk_and_exposure",
    "snapshot_age_seconds",
    "total_equity",
    "total_floating_pnl",
]
