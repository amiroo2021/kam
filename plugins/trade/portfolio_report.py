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


def normalize_instrument_key(
    symbol: Any,
    exchange_instrument: Any = None,
) -> str:
    """Normalize venue-specific aliases into a stable aggregation key.

    Preference order:
    1. Base extracted from exchange_instrument when present
    2. Display symbol with common perp suffixes stripped
    """
    candidates: List[str] = []
    ei = str(exchange_instrument or "").strip()
    sym = str(symbol or "").strip()
    if ei:
        candidates.append(ei)
    if sym:
        candidates.append(sym)

    for raw in candidates:
        text = raw.strip()
        if not text:
            continue
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
        if cleaned:
            return cleaned
    return (sym or ei or "?").upper() or "?"


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
            "native_monetary": {
                metric: {unit: None if value is None else format(value, "f") for unit, value in units.items()}
                for metric, units in self.native_monetary.items()
            },
        }


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
    def positions_incomplete(self) -> bool:
        return self.positions_accounts_ok < self.positions_accounts_seen

    @property
    def position_failures(self) -> List[AccountSnapshot]:
        return [account for account in self.accounts if not account.positions_ok]

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
            incomplete=any(not a.positions_ok for a in accounts) or bool(self.errors and accounts),
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
            row = OpenPositionRow(
                exchange=exchange,
                account=account,
                account_label=account_label or account,
                symbol=symbol,
                instrument_key=key,
                side=side,
                size=abs(size),
                entry_price=entry,
                mark=mark,
                pnl=pnl,
                pnl_unit=pnl_unit,
                native_pnl=native_pnl,
                native_pnl_unit=native_pnl_unit,
                exchange_instrument=ei_s or None,
                notional=None,
                identity=pid,
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


def net_by_instrument(snapshot: PortfolioSnapshot) -> List[Dict[str, Any]]:
    """Aggregate open positions per instrument (combines BUY + SELL).

    Returns a list of dicts in the shape::

        {
            "instrument": str,         # normalized instrument key
            "display_symbol": str,      # first contributor's display symbol
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
    """
    buckets: Dict[str, Dict[str, Any]] = {}
    accounts: Dict[str, set] = {}
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
            },
        )
        slot["_count"] += 1
        slot["display_symbol"] = pos.symbol or pos.instrument_key
        size_abs = abs(pos.size)
        slot["gross"] += size_abs
        if pos.side == "BUY":
            slot["long"] += size_abs
            slot["net"] += pos.size
        elif pos.side == "SELL":
            slot["short"] += size_abs
            slot["net"] -= size_abs
        else:
            # Unknown side; treat as long for size accounting, not for net.
            slot["long"] += size_abs
        if pos.pnl is not None:
            unit = pos.pnl_unit or "?"
            slot["pnl_units"][unit] = slot["pnl_units"].get(unit, Decimal("0")) + pos.pnl
        accounts.setdefault(pos.instrument_key, set()).add(
            f"{pos.exchange}|{pos.account}|"
        )
    rows: List[Dict[str, Any]] = []
    for key, slot in buckets.items():
        rows.append(
            {
                "instrument": slot["instrument"],
                "display_symbol": slot["display_symbol"],
                "gross": slot["gross"],
                "net": slot["net"],
                "long": slot["long"],
                "short": slot["short"],
                "pnl_units": slot["pnl_units"],
                "accounts": len(accounts.get(key, set())),
            }
        )
    return rows


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
    return sorted({a.exchange for a in snapshot.accounts})


def accounts_for_exchange(snapshot: PortfolioSnapshot, exchange: str) -> List[AccountSnapshot]:
    return [a for a in snapshot.accounts if a.exchange == exchange]


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
    "list_exchanges",
    "net_by_instrument",
    "normalize_instrument_key",
    "normalize_side",
    "pnl_by_exchange",
    "positions_by_symbol",
    "risk_and_exposure",
    "snapshot_age_seconds",
    "total_equity",
    "total_floating_pnl",
]
