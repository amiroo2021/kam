"""Phemex exchange agent.

Owns all Phemex-specific behavior for the /trade stack.

Credentials (``.env`` / environment):
  ``PHEMEX_<ALIAS>_ID`` + ``PHEMEX_<ALIAS>_APISECRET``
  Optional: ``PHEMEX_<ALIAS>_BASE_URL``, ``PHEMEX_<ALIAS>_CURRENCY``

Supported operations (Phase 2+):
  - balance
  - positions_orders / positions_management
  - new_order (limit, unified USDT-M / hedge posSide)
  - ladder (uniform / half_gaussian child limits)
  - cancel_order_group
  - resolve_instrument / list_instruments / market_price

Auth: HMAC-SHA256 over ``path + query + expiry + body`` with headers
``x-phemex-access-token``, ``x-phemex-request-expiry``,
``x-phemex-request-signature``.

Unified contract account endpoints:
  GET  /g-accounts/accountPositions?currency=USDT
  GET  /g-orders/activeList?symbol=BTCUSDT
  POST /g-orders
  DELETE /g-orders/cancel?symbol=&orderID=&posSide=
  GET  /md/v2/ticker/24hr?symbol=BTCUSDT  (public-style, no auth body)
  GET  /public/products
"""

from __future__ import annotations
from plugins.trade.candles import handle_candles_operation, has_native_candles

import hashlib
import hmac
import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..canonical import (
    CanonicalCancelGroupResult,
    CanonicalInstrument,
    CanonicalLadderResult,
    CanonicalMarketPrice,
    CanonicalOrderGroup,
    CanonicalOrderResult,
    CanonicalPortfolioSummary,
    CanonicalPosition,
    CanonicalPositionActionResult,
    CanonicalResponse,
    make_failure,
    make_success,
    normalize_balance,
    sanitize_error_message,
)

# Soft ceiling for ladder child count (wizard does not clamp; fat-finger guard).
LADDER_ABSOLUTE_MAX_ORDERS = 300
LADDER_CHILD_PAUSE_SECONDS = 0.08

logger = logging.getLogger(__name__)

name = "phemex"

DEFAULT_API_BASE = "https://api.phemex.com"
API_TIMEOUT_SECONDS = 20
MAX_RETRIES = 2
DEFAULT_CURRENCY = "USDT"
DEFAULT_OPEN_ORDER_SYMBOLS = ("BTCUSDT", "ETHUSDT")

_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ID_ALIASES = ("ID", "APIKEY", "ACCESS_TOKEN", "API_KEY")
_SECRET_ALIASES = ("APISECRET", "SECRET", "API_SECRET")

_PATH_G_ACCOUNT_POSITIONS = "/g-accounts/accountPositions"
_PATH_G_ORDERS = "/g-orders"
_PATH_G_ORDERS_ACTIVE = "/g-orders/activeList"
_PATH_G_ORDERS_CANCEL = "/g-orders/cancel"
_PATH_PUBLIC_PRODUCTS = "/public/products"
_PATH_TICKER = "/md/v2/ticker/24hr"
_PATH_G_FUTURES_ORDERS = "/api-data/g-futures/orders"

_PRODUCT_CACHE: Dict[str, Any] = {"ts": 0.0, "by_symbol": {}, "by_base": {}}
_PRODUCT_CACHE_TTL = 300.0

_OPEN_STATUS = {
    "new",
    "created",
    "partiallyfilled",
    "partially_filled",
    "pendingnew",
    "init",
    "untriggered",
}


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _load_dotenv_values(path: Path) -> Dict[str, str]:
    if not path.is_file():
        return {}
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        try:
            text = path.read_text(encoding="latin-1")
        except OSError:
            return {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _combined_phemex_env() -> Dict[str, Tuple[str, str, str]]:
    out: Dict[str, Tuple[str, str, str]] = {}
    for k, v in os.environ.items():
        if k.upper().startswith("PHEMEX_"):
            out.setdefault(k.upper(), (k, str(v), "env"))
    for k, v in _load_dotenv_values(_hermes_home() / ".env").items():
        if k.upper().startswith("PHEMEX_"):
            out.setdefault(k.upper(), (k, str(v), "dotenv"))
    return out


def _parse_alias_and_suffix(upper_key: str) -> Optional[Tuple[str, str]]:
    if not upper_key.startswith("PHEMEX_"):
        return None
    rest = upper_key[len("PHEMEX_") :]
    for suffix in sorted(
        set(_ID_ALIASES + _SECRET_ALIASES + ("BASE_URL", "CURRENCY")),
        key=len,
        reverse=True,
    ):
        token = "_" + suffix
        if rest.endswith(token):
            alias = rest[: -len(token)]
            if alias and _ALIAS_PATTERN.match(alias):
                return alias, suffix
    alias, _, suffix = rest.rpartition("_")
    if alias and suffix and _ALIAS_PATTERN.match(alias):
        return alias, suffix
    return None


def _discover_credential_map() -> Dict[str, Dict[str, str]]:
    buckets: Dict[str, Dict[str, str]] = {}
    for upper_key, (_actual, value, _src) in _combined_phemex_env().items():
        parsed = _parse_alias_and_suffix(upper_key)
        if parsed is None:
            continue
        alias_upper, suffix = parsed
        alias = alias_upper.lower()
        slot = buckets.setdefault(alias, {})
        if suffix in _ID_ALIASES:
            slot.setdefault("api_key", value.strip())
        elif suffix in _SECRET_ALIASES:
            slot.setdefault("api_secret", value.strip())
        elif suffix == "BASE_URL":
            slot["base_url"] = value.strip().rstrip("/")
        elif suffix == "CURRENCY":
            slot["currency"] = value.strip().upper()
    return {
        alias: fields
        for alias, fields in buckets.items()
        if fields.get("api_key") and fields.get("api_secret")
    }


def list_accounts() -> List[str]:
    return sorted(_discover_credential_map().keys())


def capabilities() -> List[str]:
    return [
        "candles",
        "balance",
        "positions_orders",
        "positions_management",
        "new_order",
        "ladder",
        "cancel_order_group",
        "set_tp",
        "set_sl",
        "close_position",
        "resolve_instrument",
        "list_instruments",
        "market_price",
    ]


def ladder_max_orders_per_instrument() -> Optional[int]:
    """Informational per-instrument open-order hint for the ladder UI."""
    return None


def _lookup_credentials(account: str) -> Optional[Dict[str, str]]:
    alias = str(account or "").strip().lower()
    if not alias:
        return None
    fields = _discover_credential_map().get(alias)
    if not fields:
        return None
    return {
        "account": alias,
        "api_key": fields["api_key"],
        "api_secret": fields["api_secret"],
        "base_url": fields.get("base_url") or DEFAULT_API_BASE,
        "currency": fields.get("currency") or DEFAULT_CURRENCY,
    }


# ---------------------------------------------------------------------------
# HTTP / signing
# ---------------------------------------------------------------------------


def _redact(text: Any, credentials: Optional[Mapping[str, str]] = None) -> str:
    rendered = str(text or "")
    if credentials:
        for k in ("api_key", "api_secret"):
            v = str(credentials.get(k) or "").strip()
            if len(v) >= 6:
                rendered = rendered.replace(v, "***")
    rendered = re.sub(r"(?i)(x-phemex-access-token\s*[:=]\s*)([^\s,;}\"']+)", r"\1***", rendered)
    rendered = re.sub(r"(?i)(x-phemex-request-signature\s*[:=]\s*)([^\s,;}\"']+)", r"\1***", rendered)
    return rendered


def _signed_request(
    credentials: Mapping[str, str],
    method: str,
    path: str,
    query: str = "",
    body: str = "",
    *,
    auth: bool = True,
) -> Dict[str, Any]:
    base = str(credentials.get("base_url") or DEFAULT_API_BASE).rstrip("/")
    method_u = method.upper().strip()
    query = query.lstrip("?")
    body = body or ""
    last_err: Optional[Exception] = None
    for attempt in range(MAX_RETRIES + 1):
        headers: Dict[str, str] = {}
        if auth:
            api_key = str(credentials["api_key"])
            api_secret = str(credentials["api_secret"])
            expiry = str(int(time.time()) + 60)
            payload = f"{path}{query}{expiry}{body}"
            signature = hmac.new(
                api_secret.encode("utf-8"),
                payload.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            headers["x-phemex-access-token"] = api_key
            headers["x-phemex-request-expiry"] = expiry
            headers["x-phemex-request-signature"] = signature
        if body:
            headers["Content-Type"] = "application/json"
        url = base + path + (("?" + query) if query else "")
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8") if body else None,
            method=method_u,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw) if raw else {}
            if not isinstance(parsed, dict):
                raise RuntimeError("Phemex returned a non-object JSON payload.")
            return parsed
        except urllib.error.HTTPError as exc:
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                err_body = ""
            try:
                parsed_err = json.loads(err_body) if err_body else {}
            except Exception:  # noqa: BLE001
                parsed_err = {}
            if isinstance(parsed_err, dict) and parsed_err:
                return parsed_err
            last_err = RuntimeError(
                f"HTTP {exc.code} on {path}: {_redact(err_body or exc.reason, credentials)}"
            )
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        if attempt < MAX_RETRIES:
            time.sleep(0.35 * (attempt + 1))
    raise RuntimeError(_redact(str(last_err or "Phemex request failed"), credentials))


def _phemex_ok(payload: Mapping[str, Any]) -> bool:
    code = payload.get("code")
    # ticker endpoint uses error:null / result
    if "result" in payload and payload.get("error") in (None, {}, ""):
        return True
    return code in (0, "0", None)


def _decimal_or_zero(value: Any) -> Decimal:
    try:
        if value is None or value == "":
            return Decimal("0")
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    try:
        if value is None or value == "":
            return None
        d = Decimal(str(value))
        if not d.is_finite():
            return None
        return d
    except Exception:  # noqa: BLE001
        return None


def _format_decimal(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    text = format(quantized.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _quantize(value: Decimal, step: Decimal, *, rounding=ROUND_DOWN) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=rounding) * step


def _display_symbol(raw: str) -> str:
    sym = str(raw or "").strip().upper()
    if not sym:
        return ""
    for quote in ("USDT", "USDC", "USD"):
        if sym.endswith(quote) and len(sym) > len(quote):
            return sym[: -len(quote)]
    return sym


# ---------------------------------------------------------------------------
# Products / instruments
# ---------------------------------------------------------------------------


def _load_products(credentials: Mapping[str, str], *, force: bool = False) -> Dict[str, Dict[str, Any]]:
    now = time.time()
    if (
        not force
        and _PRODUCT_CACHE.get("by_symbol")
        and now - float(_PRODUCT_CACHE.get("ts") or 0) < _PRODUCT_CACHE_TTL
    ):
        return dict(_PRODUCT_CACHE["by_symbol"])

    payload = _signed_request(credentials, "GET", _PATH_PUBLIC_PRODUCTS, auth=False)
    if not _phemex_ok(payload):
        raise RuntimeError(str(payload.get("msg") or "Failed to load Phemex products"))
    data = payload.get("data") or {}
    by_symbol: Dict[str, Dict[str, Any]] = {}
    by_base: Dict[str, List[str]] = {}

    lists: List[Any] = []
    if isinstance(data, dict):
        for key in ("perpProductsV2", "products", "perpProducts", "contractProducts"):
            arr = data.get(key)
            if isinstance(arr, list):
                lists.append(arr)
    elif isinstance(data, list):
        lists.append(data)

    for arr in lists:
        for row in arr:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            ptype = str(row.get("type") or "").strip()
            # Prefer USDT-M perpetual v2 for unified account.
            if symbol in by_symbol and ptype not in {"PerpetualV2", "Perpetual"}:
                continue
            meta = {
                "symbol": symbol,
                "type": ptype,
                "display": str(row.get("displaySymbol") or symbol),
                "tick_size": _decimal_or_zero(row.get("tickSize") or row.get("tickSizeRp") or "0.1"),
                "qty_step": _decimal_or_zero(row.get("qtyStepSize") or row.get("lotSize") or "0.001"),
                "min_qty": _decimal_or_zero(row.get("minOrderQtyRq") or row.get("qtyStepSize") or "0"),
                "min_notional": _decimal_or_zero(row.get("minOrderValueRv") or "0"),
                "max_qty": _decimal_or_zero(row.get("maxOrderQtyRq") or row.get("maxOrderQty") or "0"),
                "min_price": _decimal_or_zero(row.get("minPriceRp") or "0"),
                "max_price": _decimal_or_zero(row.get("maxPriceRp") or "0"),
                "status": str(row.get("status") or ""),
                "settle": str(row.get("settleCurrency") or ""),
                "qty_precision": int(row.get("qtyPrecision") or 8),
                "price_precision": int(row.get("pricePrecision") or 8),
            }
            if meta["qty_step"] <= 0:
                meta["qty_step"] = Decimal("0.001")
            if meta["tick_size"] <= 0:
                meta["tick_size"] = Decimal("0.1")
            # Prefer PerpetualV2 USDT over inverse when both exist.
            existing = by_symbol.get(symbol)
            if existing and existing.get("type") == "PerpetualV2" and ptype != "PerpetualV2":
                continue
            by_symbol[symbol] = meta
            base = _display_symbol(symbol)
            by_base.setdefault(base, [])
            if symbol not in by_base[base]:
                by_base[base].append(symbol)

    _PRODUCT_CACHE["ts"] = now
    _PRODUCT_CACHE["by_symbol"] = by_symbol
    _PRODUCT_CACHE["by_base"] = by_base
    return dict(by_symbol)


def _resolve_native_symbol(credentials: Mapping[str, str], requested: str) -> Tuple[str, Dict[str, Any]]:
    raw = str(requested or "").strip().upper().replace("/", "").replace("-", "").replace("_", "")
    if not raw:
        raise ValueError("MISSING_SYMBOL")
    products = _load_products(credentials)
    chosen: Optional[str] = None
    if raw in products:
        chosen = raw
        # Friendly *USD (not USDT/USDC) often means the linear USDT-m book the
        # account actually trades. Prefer PerpetualV2 USDT sibling over inverse.
        if raw.endswith("USD") and not raw.endswith(("USDT", "USDC")):
            base = raw[:-3]
            usdt = f"{base}USDT"
            if usdt in products and products[usdt].get("type") == "PerpetualV2":
                chosen = usdt
    if chosen is None:
        # BTC -> prefer BTCUSDT then BTCUSD
        base = raw
        for quote in ("USDT", "USDC", "USD"):
            if base.endswith(quote) and len(base) > len(quote):
                base = base[: -len(quote)]
                break
        candidates = list(_PRODUCT_CACHE.get("by_base", {}).get(base) or [])
        # also direct
        for q in ("USDT", "USDC", "USD"):
            sym = base + q
            if sym in products and sym not in candidates:
                candidates.append(sym)
        if not candidates:
            raise ValueError("INSTRUMENT_NOT_FOUND")
        # Prefer USDT perpetual v2
        def score(sym: str) -> Tuple[int, int]:
            meta = products[sym]
            s = 0
            if meta.get("settle") == "USDT":
                s += 10
            if meta.get("type") == "PerpetualV2":
                s += 5
            if meta.get("status") == "Listed":
                s += 1
            return (s, -len(sym))

        candidates.sort(key=score, reverse=True)
        chosen = candidates[0]
    assert chosen is not None
    return chosen, products[chosen]


def _side_to_phemex(side: str) -> str:
    s = str(side or "").strip().lower()
    if s in {"buy", "long", "bid"}:
        return "Buy"
    if s in {"sell", "short", "ask"}:
        return "Sell"
    raise ValueError("INVALID_SIDE")


def _pos_side_for_order(side: str, *, reduce_only: bool = False) -> str:
    """Map wizard buy/sell to hedge posSide.

    Opening: buy→Long, sell→Short.
    Reduce-only close of a long is sell+Long; close of short is buy+Short.
    Wizard new_order is open-oriented unless reduce_only is set.
    """
    s = str(side or "").strip().lower()
    if reduce_only:
        if s in {"sell", "short", "ask"}:
            return "Long"  # selling to reduce long
        if s in {"buy", "long", "bid"}:
            return "Short"
    if s in {"buy", "long", "bid"}:
        return "Long"
    if s in {"sell", "short", "ask"}:
        return "Short"
    raise ValueError("INVALID_SIDE")


def _pos_side_from_order_row(row: Mapping[str, Any]) -> str:
    explicit = str(row.get("posSide") or row.get("positionSide") or "").strip()
    if explicit:
        low = explicit.lower()
        if low in {"long", "buy"}:
            return "Long"
        if low in {"short", "sell"}:
            return "Short"
        if explicit in {"Long", "Short", "Merged"}:
            return explicit
    # Infer from order side for open-style orders.
    side = str(row.get("side") or "").strip().lower()
    if side in {"buy", "long", "bid"}:
        return "Long"
    if side in {"sell", "short", "ask"}:
        return "Short"
    return "Long"


# ---------------------------------------------------------------------------
# Account / positions / orders reads
# ---------------------------------------------------------------------------


def _fetch_account_positions(credentials: Mapping[str, str]) -> Dict[str, Any]:
    currency = urllib.parse.quote(str(credentials.get("currency") or DEFAULT_CURRENCY))
    payload = _signed_request(
        credentials, "GET", _PATH_G_ACCOUNT_POSITIONS, query=f"currency={currency}"
    )
    if not _phemex_ok(payload):
        raise RuntimeError(str(payload.get("msg") or payload.get("code") or "Phemex error"))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Phemex accountPositions returned no data object.")
    return data


def _side_from_position(row: Mapping[str, Any]) -> Optional[str]:
    side = str(row.get("side") or row.get("posSide") or "").strip().lower()
    if side in {"buy", "long"}:
        return "long"
    if side in {"sell", "short"}:
        return "short"
    for key in ("sizeRv", "size", "sizeRq"):
        size = _decimal_or_zero(row.get(key))
        if size > 0:
            return "long"
        if size < 0:
            return "short"
    return None


def _position_size(row: Mapping[str, Any]) -> Decimal:
    for key in ("sizeRv", "size", "sizeRq"):
        if key in row and row.get(key) not in (None, ""):
            return abs(_decimal_or_zero(row.get(key)))
    return Decimal("0")


def _is_protection_order(row: Mapping[str, Any]) -> bool:
    status = str(row.get("ordStatus") or "").strip().lower()
    exec_inst = str(row.get("execInst") or "").strip().lower()
    otype = str(row.get("orderType") or row.get("ordType") or "").strip()
    if "closeontrigger" in exec_inst.replace(" ", ""):
        return True
    if status == "untriggered" and otype in {"Stop", "MarketIfTouched", "LimitIfTouched", "StopLimit"}:
        return True
    return False


def _protection_kind(row: Mapping[str, Any], position_side: str) -> Optional[str]:
    """Classify an untriggered close-on-trigger order as tp or sl."""
    if not _is_protection_order(row):
        return None
    otype = str(row.get("orderType") or row.get("ordType") or "").strip()
    direction = str(row.get("stopDirection") or "").strip()
    # Prefer explicit type mapping used by Phemex auto-attach.
    if otype == "Stop":
        return "sl"
    if otype == "MarketIfTouched":
        return "tp"
    # Fallback by stop direction vs position side.
    if position_side == "long":
        if direction == "Falling":
            return "sl"
        if direction == "Rising":
            return "tp"
    if position_side == "short":
        if direction == "Rising":
            return "sl"
        if direction == "Falling":
            return "tp"
    return None


def _extract_protections(
    order_rows: Sequence[Mapping[str, Any]], native_symbol: str, position_side: str
) -> Tuple[Optional[str], Optional[str], int, int, List[Dict[str, Any]]]:
    tp: Optional[str] = None
    sl: Optional[str] = None
    tp_rows: List[Dict[str, Any]] = []
    sl_rows: List[Dict[str, Any]] = []
    want_side = "Sell" if position_side == "long" else "Buy"
    for row in order_rows:
        if str(row.get("symbol") or "").upper() != native_symbol.upper():
            continue
        if str(row.get("side") or "").strip() not in {want_side, want_side.lower()}:
            # also accept buy/sell lowercase
            side_l = str(row.get("side") or "").strip().lower()
            if position_side == "long" and side_l not in {"sell", "short"}:
                continue
            if position_side == "short" and side_l not in {"buy", "long"}:
                continue
        kind = _protection_kind(row, position_side)
        if kind is None:
            continue
        px = _decimal_or_zero(row.get("stopPxRp") or row.get("priceRp") or row.get("stopPx"))
        if px <= 0:
            continue
        item = dict(row)
        if kind == "tp":
            tp_rows.append(item)
            if tp is None:
                tp = _format_decimal(px)
        else:
            sl_rows.append(item)
            if sl is None:
                sl = _format_decimal(px)
    return tp, sl, len(tp_rows), len(sl_rows), tp_rows + sl_rows


def _normalize_positions(rows: Any) -> List[CanonicalPosition]:
    if not isinstance(rows, list):
        return []
    positions: List[CanonicalPosition] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        size = _position_size(row)
        if size <= 0:
            continue
        side = _side_from_position(row)
        if side not in {"long", "short"}:
            continue
        symbol_raw = str(row.get("symbol") or "").strip()
        symbol = _display_symbol(symbol_raw) or symbol_raw
        entry = _decimal_or_none(
            row.get("avgEntryPriceRp") or row.get("avgEntryPrice") or row.get("entryPrice")
        ) or Decimal("0")
        mark = _decimal_or_none(
            row.get("markPriceRp")
            or row.get("markPrice")
            or row.get("markPx")
            or row.get("mark_price")
        )
        if mark is not None and mark <= 0:
            mark = None
        # Prefer exchange-reported unrealized PnL. These USDT-m rows often omit
        # it entirely — never coerce missing → 0 (that made mark look like entry).
        pnl = _decimal_or_none(
            row.get("unrealisedPnlRv")
            or row.get("unrealisedPnlRp")
            or row.get("unrealizedPnlRv")
            or row.get("unrealizedPnlRp")
            or row.get("unrealizedPnl")
            or row.get("unrealisedPnl")
        )
        settle = str(row.get("currency") or row.get("settleCurrency") or "").strip().upper()
        # Linear USDT/USDC contracts: size is base coins; PnL ≈ (mark-entry)*size.
        if pnl is None and mark is not None and entry > 0 and size > 0 and settle in {"USDT", "USDC", "USD"}:
            if side == "long":
                pnl = (mark - entry) * size
            else:
                pnl = (entry - mark) * size
        positions.append(
            CanonicalPosition(
                symbol=symbol,
                side=side,
                size=_format_decimal(size),
                entry_price=_format_decimal(entry) if entry > 0 else "0",
                pnl=_format_decimal(pnl) if pnl is not None else "",
                mark=_format_decimal(mark) if mark is not None and mark > 0 else None,
                exchange_instrument=symbol_raw or None,
            )
        )
    positions.sort(key=lambda p: (p.symbol, p.side))
    return positions


def _enrich_positions_with_protections(
    credentials: Mapping[str, str], positions: List[CanonicalPosition]
) -> List[CanonicalPosition]:
    if not positions:
        return positions
    # Load open orders once per native symbol.
    by_native: Dict[str, List[Dict[str, Any]]] = {}
    enriched: List[CanonicalPosition] = []
    for pos in positions:
        native = str(pos.exchange_instrument or "").strip().upper()
        if not native:
            try:
                native, _ = _resolve_native_symbol(credentials, pos.symbol)
            except Exception:  # noqa: BLE001
                enriched.append(pos)
                continue
        if native not in by_native:
            try:
                by_native[native] = _fetch_active_orders_for_symbol(credentials, native)
            except Exception:  # noqa: BLE001
                by_native[native] = []
        tp, sl, tp_count, sl_count, _rows = _extract_protections(
            by_native[native], native, pos.side
        )
        enriched.append(
            CanonicalPosition(
                symbol=pos.symbol,
                side=pos.side,
                size=pos.size,
                entry_price=pos.entry_price,
                pnl=pos.pnl,
                mark=pos.mark,
                tp=tp,
                sl=sl,
                tp_count=tp_count or None,
                sl_count=sl_count or None,
                exchange_instrument=native or pos.exchange_instrument,
            )
        )
    return enriched


def _find_open_position(
    credentials: Mapping[str, str], requested_symbol: str
) -> Tuple[CanonicalPosition, str, Dict[str, Any]]:
    data = _fetch_account_positions(credentials)
    positions = _normalize_positions(data.get("positions"))
    if not positions:
        raise ValueError("NO_OPEN_POSITION")
    native, meta = _resolve_native_symbol(credentials, requested_symbol)
    display = _display_symbol(native) or native
    matches = [
        p
        for p in positions
        if str(p.exchange_instrument or "").upper() == native
        or str(p.symbol or "").upper() == display.upper()
        or str(p.symbol or "").upper() == str(requested_symbol or "").strip().upper()
    ]
    if not matches:
        raise ValueError("NO_OPEN_POSITION")
    if len(matches) > 1:
        # Prefer long if ambiguous (rare).
        matches.sort(key=lambda p: 0 if p.side == "long" else 1)
    pos = matches[0]
    return pos, native, meta


def _fetch_active_orders_for_symbol(
    credentials: Mapping[str, str], native_symbol: str
) -> List[Dict[str, Any]]:
    query = f"symbol={urllib.parse.quote(native_symbol)}"
    payload = _signed_request(credentials, "GET", _PATH_G_ORDERS_ACTIVE, query=query)
    # OM_ORDER_NOT_FOUND means empty book for that symbol.
    if payload.get("code") in (10002, "10002"):
        return []
    if not _phemex_ok(payload):
        msg = str(payload.get("msg") or payload.get("code") or "activeList failed")
        # treat empty-ish as empty
        if "not found" in msg.lower():
            return []
        raise RuntimeError(msg)
    data = payload.get("data")
    if isinstance(data, dict):
        rows = data.get("rows") or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    return [r for r in rows if isinstance(r, dict)]


def _candidate_symbols_for_open_orders(
    credentials: Mapping[str, str], positions: Sequence[CanonicalPosition]
) -> List[str]:
    symbols: List[str] = []
    for p in positions:
        if p.exchange_instrument:
            symbols.append(str(p.exchange_instrument).upper())
        else:
            try:
                native, _ = _resolve_native_symbol(credentials, p.symbol)
                symbols.append(native)
            except Exception:  # noqa: BLE001
                pass
    for sym in DEFAULT_OPEN_ORDER_SYMBOLS:
        symbols.append(sym)
    # Recent order history symbols (may include open + cancelled)
    try:
        currency = urllib.parse.quote(str(credentials.get("currency") or DEFAULT_CURRENCY))
        payload = _signed_request(
            credentials,
            "GET",
            _PATH_G_FUTURES_ORDERS,
            query=f"currency={currency}&limit=100",
        )
        if _phemex_ok(payload):
            data = payload.get("data") or {}
            rows = data.get("rows") if isinstance(data, dict) else data
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    sym = str(row.get("symbol") or "").upper()
                    if sym:
                        symbols.append(sym)
    except Exception as exc:  # noqa: BLE001
        logger.info("phemex history symbol scan failed: %s", _redact(exc, credentials))
    # unique preserve order
    out: List[str] = []
    seen = set()
    for s in symbols:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out[:40]  # bound fan-out


def _fetch_all_open_order_rows(
    credentials: Mapping[str, str], positions: Sequence[CanonicalPosition]
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for sym in _candidate_symbols_for_open_orders(credentials, positions):
        try:
            part = _fetch_active_orders_for_symbol(credentials, sym)
        except Exception as exc:  # noqa: BLE001
            logger.info("phemex activeList %s failed: %s", sym, _redact(exc, credentials))
            continue
        for row in part:
            row = dict(row)
            row.setdefault("symbol", sym)
            rows.append(row)
    return rows


def _group_open_orders(rows: List[Mapping[str, Any]]) -> Tuple[int, List[CanonicalOrderGroup]]:
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    total = 0
    for row in rows:
        status = str(row.get("ordStatus") or row.get("orderStatus") or "").strip().lower()
        if status and status not in _OPEN_STATUS and status not in {"", "unspecified"}:
            if status in {"filled", "canceled", "cancelled", "rejected", "expired", "deactivated"}:
                continue
        symbol = _display_symbol(str(row.get("symbol") or ""))
        side_raw = str(row.get("side") or "").strip().lower()
        if side_raw in {"buy", "long", "bid"}:
            side = "buy"
        elif side_raw in {"sell", "short", "ask"}:
            side = "sell"
        else:
            continue
        if not symbol:
            continue
        size = abs(
            _decimal_or_zero(
                row.get("leavesQtyRq")
                or row.get("orderQtyRq")
                or row.get("orderQty")
                or row.get("size")
            )
        )
        price = _decimal_or_zero(row.get("priceRp") or row.get("price") or row.get("stopPxRp"))
        if size <= 0:
            continue
        total += 1
        key = (symbol, side)
        slot = groups.setdefault(
            key,
            {
                "symbol": symbol,
                "side": side,
                "order_count": 0,
                "total_size": Decimal("0"),
                "notional": Decimal("0"),
                "min_price": None,
                "max_price": None,
            },
        )
        slot["order_count"] += 1
        slot["total_size"] += size
        if price > 0:
            slot["notional"] += size * price
            slot["min_price"] = price if slot["min_price"] is None else min(slot["min_price"], price)
            slot["max_price"] = price if slot["max_price"] is None else max(slot["max_price"], price)
    out: List[CanonicalOrderGroup] = []
    for slot in groups.values():
        total_size = slot["total_size"]
        vwap = ""
        if total_size > 0 and slot["notional"] > 0:
            vwap = _format_decimal(slot["notional"] / total_size)
        min_p = _format_decimal(slot["min_price"]) if slot["min_price"] is not None else ""
        max_p = _format_decimal(slot["max_price"]) if slot["max_price"] is not None else ""
        out.append(
            CanonicalOrderGroup(
                symbol=slot["symbol"],
                side=slot["side"],
                order_count=int(slot["order_count"]),
                total_size=_format_decimal(total_size),
                vwap=vwap,
                min_price=min_p,
                max_price=max_p,
            )
        )
    out.sort(key=lambda g: (g.symbol, g.side))
    return total, out


def _fetch_mark_price(credentials: Mapping[str, str], native_symbol: str) -> Decimal:
    query = f"symbol={urllib.parse.quote(native_symbol)}"
    # ticker works without auth headers on this host
    payload = _signed_request(credentials, "GET", _PATH_TICKER, query=query, auth=False)
    if payload.get("error"):
        # retry with auth
        payload = _signed_request(credentials, "GET", _PATH_TICKER, query=query, auth=True)
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    for key in ("markPriceRp", "indexPriceRp", "closeRp", "lastRp", "priceRp"):
        val = _decimal_or_zero(result.get(key))
        if val > 0:
            return val
    raise RuntimeError("Mark price unavailable")


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _balance(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set PHEMEX_<ACCOUNT>_ID and PHEMEX_<ACCOUNT>_APISECRET.",
        )
    try:
        data = _fetch_account_positions(credentials)
        account_row = data.get("account") if isinstance(data.get("account"), dict) else {}
        currency = str(
            account_row.get("currency") or credentials.get("currency") or DEFAULT_CURRENCY
        ).strip().upper() or DEFAULT_CURRENCY
        balance_dec = _decimal_or_zero(
            account_row.get("accountBalanceRv")
            or account_row.get("accountBalance")
            or account_row.get("totalEquityRv")
            or "0"
        )
        used_dec = _decimal_or_zero(
            account_row.get("totalUsedBalanceRv") or account_row.get("totalUsedBalance") or "0"
        )
        withdrawable = max(balance_dec - used_dec, Decimal("0"))
        pos_notional = Decimal("0")
        for row in data.get("positions") or []:
            if isinstance(row, Mapping) and _position_size(row) > 0:
                pos_notional += abs(_decimal_or_zero(row.get("valueRv") or row.get("value")))
        balance = normalize_balance(balance_dec, currency)
        portfolio = CanonicalPortfolioSummary(
            account_value=normalize_balance(balance_dec, currency).value,
            withdrawable=normalize_balance(withdrawable, currency).value,
            margin_used=normalize_balance(used_dec, currency).value,
            total_position_value=normalize_balance(pos_notional, currency).value,
            unit=currency,
        )
        return make_success(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            balance=balance,
            portfolio_summary=portfolio,
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            code="PHEMEX_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _positions_orders(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set PHEMEX_<ACCOUNT>_ID and PHEMEX_<ACCOUNT>_APISECRET.",
        )
    try:
        data = _fetch_account_positions(credentials)
        positions = _normalize_positions(data.get("positions"))
        positions = _enrich_positions_with_protections(credentials, positions)
        order_rows = _fetch_all_open_order_rows(credentials, positions)
        # Exclude pure protection rows from order-group ladder view? Keep them —
        # cancel groups may still want them. Grouping by buy/sell is fine.
        open_count, order_groups = _group_open_orders(order_rows)
        return make_success(
            operation="positions_orders",
            exchange=name,
            account=credentials["account"],
            positions=positions,
            open_order_count=open_count,
            order_groups=order_groups,
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=credentials["account"],
            code="PHEMEX_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _positions_management(account: str) -> CanonicalResponse:
    response = _positions_orders(account)
    if not response.success:
        return make_failure(
            operation="positions_management",
            exchange=name,
            account=account,
            code=getattr(response.error, "code", None) or "PHEMEX_ERROR",
            message=getattr(response.error, "message", None) or "Phemex error",
        )
    return make_success(
        operation="positions_management",
        exchange=name,
        account=response.account,
        positions=list(response.positions or []),
        open_order_count=response.open_order_count,
        order_groups=list(response.order_groups or []),
    )


def _resolve_instrument(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set PHEMEX_<ACCOUNT>_ID and PHEMEX_<ACCOUNT>_APISECRET.",
        )
    requested = str(request.get("symbol") or request.get("instrument") or "").strip()
    try:
        native, meta = _resolve_native_symbol(credentials, requested)
    except ValueError as exc:
        code = str(exc)
        if code == "MISSING_SYMBOL":
            return make_failure(
                operation="resolve_instrument",
                exchange=name,
                account=credentials["account"],
                code="MISSING_SYMBOL",
                message="Symbol is required.",
            )
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=credentials["account"],
            code="INSTRUMENT_NOT_FOUND",
            message=f"Phemex instrument '{requested}' not found.",
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=credentials["account"],
            code="PHEMEX_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )
    instrument = CanonicalInstrument(
        requested_symbol=requested or native,
        # Native exchange symbol (e.g. BTCUSDT) — not the display base BTC.
        symbol=native,
        display_name=str(meta.get("display") or native),
        price_increment=_format_decimal(meta["tick_size"]),
        size_increment=_format_decimal(meta["qty_step"]),
        minimum_size=_format_decimal(meta["min_qty"] or meta["qty_step"]),
    )
    return make_success(
        operation="resolve_instrument",
        exchange=name,
        account=credentials["account"],
        instrument=instrument,
        data={"native_symbol": native, "settle": meta.get("settle"), "type": meta.get("type")},
    )


def _list_instruments(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set PHEMEX_<ACCOUNT>_ID and PHEMEX_<ACCOUNT>_APISECRET.",
        )
    try:
        products = _load_products(credentials)
        items = []
        for sym, meta in sorted(products.items()):
            if meta.get("settle") and meta.get("settle") != credentials.get("currency", "USDT"):
                # still include USDT primarily
                if meta.get("settle") != "USDT":
                    continue
            items.append(
                {
                    "symbol": _display_symbol(sym) or sym,
                    "native_symbol": sym,
                    "display_name": meta.get("display") or sym,
                    "price_increment": _format_decimal(meta["tick_size"]),
                    "size_increment": _format_decimal(meta["qty_step"]),
                }
            )
        return make_success(
            operation="list_instruments",
            exchange=name,
            account=credentials["account"],
            data={"instruments": items, "count": len(items)},
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account=credentials["account"],
            code="PHEMEX_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _market_price(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set PHEMEX_<ACCOUNT>_ID and PHEMEX_<ACCOUNT>_APISECRET.",
        )
    requested = str(request.get("symbol") or "").strip()
    try:
        native, _meta = _resolve_native_symbol(credentials, requested)
        mark = _fetch_mark_price(credentials, native)
        mp = CanonicalMarketPrice(
            requested_symbol=requested or native,
            market=native,
            mark_price=_format_decimal(mark),
            price=_format_decimal(mark),
        )
        return make_success(
            operation="market_price",
            exchange=name,
            account=credentials["account"],
            market_price=mp,
        )
    except ValueError as exc:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=credentials["account"],
            code="INSTRUMENT_NOT_FOUND" if str(exc) == "INSTRUMENT_NOT_FOUND" else "INVALID_REQUEST",
            message=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="market_price",
            exchange=name,
            account=credentials["account"],
            code="PHEMEX_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _new_order(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set PHEMEX_<ACCOUNT>_ID and PHEMEX_<ACCOUNT>_APISECRET.",
        )
    requested_symbol = str(request.get("symbol") or "").strip()
    side_in = str(request.get("side") or "").strip().lower()
    volume_text = str(request.get("volume") or request.get("size") or "").strip()
    price_text = str(request.get("price") or "").strip()
    order_type = str(request.get("order_type") or request.get("type") or "limit").strip().lower()
    reduce_only = bool(request.get("reduce_only") or request.get("reduceOnly") or False)

    if order_type not in {"limit", ""}:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="UNSUPPORTED_ORDER_TYPE",
            message="Phemex agent currently supports limit orders only.",
        )
    try:
        side_p = _side_to_phemex(side_in)
        pos_side = _pos_side_for_order(side_in, reduce_only=reduce_only)
        volume = Decimal(volume_text)
        price = Decimal(price_text)
    except Exception:  # noqa: BLE001
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="Symbol, buy/sell side, volume and price are required.",
        )
    if volume <= 0 or price <= 0:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="Volume and price must be positive.",
        )

    try:
        native, meta = _resolve_native_symbol(credentials, requested_symbol)
        tick = meta["tick_size"]
        step = meta["qty_step"]
        submitted_price = _quantize(price, tick, rounding=ROUND_HALF_UP)
        submitted_volume = _quantize(volume, step, rounding=ROUND_DOWN)
        if submitted_volume <= 0:
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="INVALID_VOLUME",
                message=f"Volume rounds to zero at step {step}.",
            )
        if meta["min_qty"] > 0 and submitted_volume < meta["min_qty"]:
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="INVALID_VOLUME",
                message=f"Volume below minimum {meta['min_qty']}.",
            )
        notional = submitted_price * submitted_volume
        if meta["min_notional"] > 0 and notional < meta["min_notional"]:
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="INVALID_NOTIONAL",
                message=f"Order notional below minimum {meta['min_notional']} USDT.",
            )

        cl_ord_id = str(request.get("client_order_id") or request.get("clOrdID") or "").strip()
        if not cl_ord_id:
            cl_ord_id = uuid.uuid4().hex[:32]

        body_obj: Dict[str, Any] = {
            "symbol": native,
            "clOrdID": cl_ord_id,
            "side": side_p,
            "posSide": pos_side,
            "ordType": "Limit",
            "priceRp": _format_decimal(submitted_price),
            "orderQtyRq": _format_decimal(submitted_volume),
            "timeInForce": "GoodTillCancel",
        }
        if reduce_only:
            body_obj["reduceOnly"] = True
        body = json.dumps(body_obj, separators=(",", ":"))
        payload = _signed_request(credentials, "POST", _PATH_G_ORDERS, body=body)
        if not _phemex_ok(payload):
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="ORDER_FAILED",
                message=_redact(
                    sanitize_error_message(str(payload.get("msg") or payload.get("code") or "order failed")),
                    credentials,
                ),
                order=CanonicalOrderResult(
                    symbol=_display_symbol(native) or native,
                    side=side_in if side_in in {"buy", "sell"} else side_in,
                    order_type="limit",
                    requested_volume=_format_decimal(volume),
                    requested_price=_format_decimal(price),
                    submitted_volume=_format_decimal(submitted_volume),
                    submitted_price=_format_decimal(submitted_price),
                    verified=False,
                    status="failed",
                    client_order_id=cl_ord_id,
                ),
            )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        order_id = str(data.get("orderID") or data.get("orderId") or "").strip() or None

        verified = False
        if order_id:
            try:
                time.sleep(0.25)
                active = _fetch_active_orders_for_symbol(credentials, native)
                verified = any(
                    str(r.get("orderID") or r.get("orderId") or "") == order_id
                    or str(r.get("clOrdID") or r.get("clOrdId") or "") == cl_ord_id
                    for r in active
                )
            except Exception:  # noqa: BLE001
                verified = bool(order_id)

        result = CanonicalOrderResult(
            symbol=_display_symbol(native) or native,
            side="buy" if side_p == "Buy" else "sell",
            order_type="limit",
            requested_volume=_format_decimal(volume),
            requested_price=_format_decimal(price),
            submitted_volume=_format_decimal(submitted_volume),
            submitted_price=_format_decimal(submitted_price),
            verified=verified,
            status="success" if verified or order_id else "submitted",
            exchange_order_id=order_id,
            client_order_id=cl_ord_id,
        )
        return make_success(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            order=result,
        )
    except ValueError as exc:
        code = str(exc)
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INSTRUMENT_NOT_FOUND" if code == "INSTRUMENT_NOT_FOUND" else "INVALID_REQUEST",
            message=sanitize_error_message(str(exc)),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="PHEMEX_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _ladder_prices(start: Decimal, end: Decimal, count: int, tick: Decimal) -> List[Decimal]:
    if count <= 0:
        return []
    if count == 1:
        raw = [start]
    else:
        raw = [start + (end - start) * Decimal(i) / Decimal(count - 1) for i in range(count)]
    return [_quantize(value, tick, rounding=ROUND_HALF_UP) for value in raw]


def _ladder_sizes(
    total: Decimal,
    count: int,
    increment: Decimal,
    distribution: str,
    min_size: Decimal = Decimal("0"),
) -> List[Decimal]:
    key = str(distribution or "").strip().lower().replace(" ", "_")
    if key == "uniform":
        weights = [Decimal(1)] * count
    elif key == "half_gaussian":
        if count == 1:
            weights = [Decimal(1)]
        else:
            span = Decimal(count - 1)
            weights = [
                Decimal(
                    str(
                        math.exp(
                            -(float(Decimal("3") * (span - Decimal(i)) / span) ** 2) / 2
                        )
                    )
                )
                for i in range(count)
            ]
    else:
        raise ValueError(f"Unsupported ladder distribution: {distribution}")
    if increment <= 0:
        raise ValueError("Invalid size increment")
    minimum_units = int((min_size / increment).to_integral_value(rounding=ROUND_DOWN)) if min_size > 0 else 0
    total_units = int((total / increment).to_integral_value(rounding=ROUND_DOWN))
    if total_units <= 0:
        raise ValueError("Total volume rounds to zero at the size step")
    if total_units < minimum_units * count:
        raise ValueError("Total volume is too small for the exchange minimum on every ladder child")
    distributable = total_units - minimum_units * count
    weight_sum = sum(weights)
    raw = [Decimal(distributable) * w / weight_sum for w in weights]
    allocated = [minimum_units + int(x) for x in raw]
    residual = total_units - sum(allocated)
    remainders = [raw[i] - int(raw[i]) for i in range(count)]
    order = sorted(range(count), key=lambda i: (remainders[i], -i), reverse=True)
    for i in order[: max(residual, 0)]:
        allocated[i] += 1
    return [Decimal(x) * increment for x in allocated]


def _place_limit_child(
    credentials: Mapping[str, str],
    *,
    native: str,
    side_in: str,
    price: Decimal,
    size: Decimal,
    meta: Mapping[str, Any],
) -> Dict[str, Any]:
    """Place one ladder child; returns {ok, order_id, error}."""
    side_p = _side_to_phemex(side_in)
    pos_side = _pos_side_for_order(side_in, reduce_only=False)
    tick = meta["tick_size"]
    step = meta["qty_step"]
    submitted_price = _quantize(price, tick, rounding=ROUND_HALF_UP)
    submitted_volume = _quantize(size, step, rounding=ROUND_DOWN)
    if submitted_volume <= 0 or submitted_price <= 0:
        return {"ok": False, "order_id": None, "error": "rounded_to_zero", "price": submitted_price, "size": submitted_volume}
    if meta.get("min_qty") and submitted_volume < meta["min_qty"]:
        return {"ok": False, "order_id": None, "error": "below_min_qty", "price": submitted_price, "size": submitted_volume}
    notional = submitted_price * submitted_volume
    if meta.get("min_notional") and notional < meta["min_notional"]:
        return {"ok": False, "order_id": None, "error": "below_min_notional", "price": submitted_price, "size": submitted_volume}
    cl_ord_id = uuid.uuid4().hex[:32]
    body_obj = {
        "symbol": native,
        "clOrdID": cl_ord_id,
        "side": side_p,
        "posSide": pos_side,
        "ordType": "Limit",
        "priceRp": _format_decimal(submitted_price),
        "orderQtyRq": _format_decimal(submitted_volume),
        "timeInForce": "GoodTillCancel",
    }
    body = json.dumps(body_obj, separators=(",", ":"))
    payload = _signed_request(credentials, "POST", _PATH_G_ORDERS, body=body)
    if not _phemex_ok(payload):
        return {
            "ok": False,
            "order_id": None,
            "error": str(payload.get("msg") or payload.get("code") or "order failed"),
            "price": submitted_price,
            "size": submitted_volume,
            "client_order_id": cl_ord_id,
        }
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    order_id = str(data.get("orderID") or data.get("orderId") or "").strip() or None
    return {
        "ok": True,
        "order_id": order_id,
        "error": None,
        "price": submitted_price,
        "size": submitted_volume,
        "client_order_id": cl_ord_id,
    }


def _ladder(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set PHEMEX_<ACCOUNT>_ID and PHEMEX_<ACCOUNT>_APISECRET.",
        )
    requested_symbol = str(request.get("symbol") or "").strip()
    side_in = str(request.get("side") or "").strip().lower()
    distribution = str(request.get("distribution") or "uniform").strip().lower().replace(" ", "_")
    try:
        count = int(str(request.get("order_count") or "0").strip())
        total = Decimal(str(request.get("total_volume") or "0").strip())
        start = Decimal(str(request.get("start_price") or "0").strip())
        end = Decimal(str(request.get("end_price") or "0").strip())
    except Exception:  # noqa: BLE001
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="order_count, total_volume, start_price and end_price are required numbers.",
        )
    if side_in not in {"buy", "sell"}:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_SIDE",
            message="Side must be buy or sell.",
        )
    if count < 1:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="order_count must be >= 1.",
        )
    if count > LADDER_ABSOLUTE_MAX_ORDERS:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message=f"order_count exceeds safety cap ({LADDER_ABSOLUTE_MAX_ORDERS}).",
        )
    if total <= 0 or start <= 0 or end <= 0:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="total_volume, start_price and end_price must be positive.",
        )
    if side_in == "buy" and not (end < start):
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="For a BUY ladder, end_price must be lower than start_price.",
        )
    if side_in == "sell" and not (end > start):
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="For a SELL ladder, end_price must be higher than start_price.",
        )
    if distribution not in {"uniform", "half_gaussian"}:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="distribution must be uniform or half_gaussian.",
        )

    try:
        native, meta = _resolve_native_symbol(credentials, requested_symbol)
        tick = meta["tick_size"]
        step = meta["qty_step"]
        min_size = meta["min_qty"] if meta["min_qty"] > 0 else step
        prices = _ladder_prices(start, end, count, tick)
        sizes = _ladder_sizes(total, count, step, distribution, min_size)

        submitted_children: List[Dict[str, Any]] = []
        batches: List[Dict[str, Any]] = []
        omitted_below_minimum = 0
        first_error: Optional[str] = None

        for idx, (price, size) in enumerate(zip(prices, sizes)):
            child = _place_limit_child(
                credentials,
                native=native,
                side_in=side_in,
                price=price,
                size=size,
                meta=meta,
            )
            batches.append(
                {
                    "index": idx,
                    "price": _format_decimal(child.get("price") or price),
                    "size": _format_decimal(child.get("size") or size),
                    "ok": bool(child.get("ok")),
                    "order_id": child.get("order_id"),
                    "error": child.get("error"),
                }
            )
            if child.get("ok") and child.get("order_id"):
                submitted_children.append(
                    {
                        "order_id": child["order_id"],
                        "price": child["price"],
                        "size": child["size"],
                    }
                )
            else:
                err = str(child.get("error") or "failed")
                if err in {"below_min_qty", "below_min_notional", "rounded_to_zero"}:
                    omitted_below_minimum += 1
                if first_error is None:
                    first_error = err
            if idx + 1 < count:
                time.sleep(LADDER_CHILD_PAUSE_SECONDS)

        ids = [str(c["order_id"]) for c in submitted_children]
        submitted_volume = sum((Decimal(str(c["size"])) for c in submitted_children), Decimal("0"))

        verified = False
        if ids:
            try:
                time.sleep(0.35)
                active = _fetch_active_orders_for_symbol(credentials, native)
                live_ids = {
                    str(r.get("orderID") or r.get("orderId") or "").strip()
                    for r in active
                }
                verified = all(oid in live_ids for oid in ids)
            except Exception:  # noqa: BLE001
                verified = len(ids) == count

        partial = (len(ids) != count) or (not verified and bool(ids))
        result = CanonicalLadderResult(
            symbol=_display_symbol(native) or native,
            side=side_in,
            distribution=distribution,
            requested_order_count=count,
            submitted_order_count=len(ids),
            requested_volume=_format_decimal(total),
            submitted_volume=_format_decimal(submitted_volume),
            batch_count=len(batches),
            verified=verified and len(ids) == count,
            partial=partial,
            status="success" if (verified and len(ids) == count) else ("partial" if ids else "failed"),
            accepted_child_count=len(ids),
            omitted_order_count=count - len(ids),
            omitted_below_minimum=omitted_below_minimum or None,
            child_order_ids=list(ids),
            batches=batches,
            exchange_reason=_redact(first_error, credentials) if first_error and not ids else (
                _redact(first_error, credentials) if first_error and partial else None
            ),
        )
        if result.verified:
            return make_success(
                operation="ladder",
                exchange=name,
                account=credentials["account"],
                ladder=result,
            )
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="PARTIAL_LADDER" if ids else "LADDER_FAILED",
            message=(
                f"Ladder submitted {len(ids)}/{count} children"
                + (f" ({first_error})" if first_error else "")
                + ("." if ids else " — no children accepted.")
            ),
            ladder=result,
        )
    except ValueError as exc:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INSTRUMENT_NOT_FOUND" if str(exc) == "INSTRUMENT_NOT_FOUND" else "INVALID_REQUEST",
            message=sanitize_error_message(str(exc)),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="LADDER_FAILED",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _cancel_order_group(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set PHEMEX_<ACCOUNT>_ID and PHEMEX_<ACCOUNT>_APISECRET.",
        )
    requested_symbol = str(request.get("symbol") or "").strip()
    side_in = str(request.get("side") or "").strip().lower()
    if side_in in {"long"}:
        side_in = "buy"
    if side_in in {"short"}:
        side_in = "sell"
    if side_in not in {"buy", "sell"}:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=credentials["account"],
            code="INVALID_SIDE",
            message="Side must be buy or sell.",
        )
    try:
        native, _meta = _resolve_native_symbol(credentials, requested_symbol)
        active = _fetch_active_orders_for_symbol(credentials, native)
        want = "Buy" if side_in == "buy" else "Sell"
        targets = [
            r
            for r in active
            if str(r.get("side") or "").strip() == want
            or str(r.get("side") or "").strip().lower() == side_in
        ]
        if not targets:
            result = CanonicalCancelGroupResult(
                symbol=_display_symbol(native) or native,
                side=side_in,
                targeted_order_count=0,
                cancelled_order_count=0,
                confirmed_absent_count=0,
                remaining_target_count=0,
                verified=True,
                partial=False,
                status="success",
                batch_count=0,
            )
            return make_success(
                operation="cancel_order_group",
                exchange=name,
                account=credentials["account"],
                cancel_group=result,
            )

        cancelled = 0
        batches: List[Dict[str, Any]] = []
        target_ids: List[str] = []
        for row in targets:
            oid = str(row.get("orderID") or row.get("orderId") or "").strip()
            if not oid:
                continue
            target_ids.append(oid)
            pos_side = _pos_side_from_order_row(row)
            query = (
                f"symbol={urllib.parse.quote(native)}"
                f"&orderID={urllib.parse.quote(oid)}"
                f"&posSide={urllib.parse.quote(pos_side)}"
            )
            try:
                payload = _signed_request(
                    credentials, "DELETE", _PATH_G_ORDERS_CANCEL, query=query
                )
                ok = _phemex_ok(payload)
                if ok:
                    cancelled += 1
                batches.append(
                    {
                        "order_id": oid,
                        "ok": ok,
                        "code": payload.get("code"),
                        "msg": str(payload.get("msg") or "")[:120],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                batches.append(
                    {
                        "order_id": oid,
                        "ok": False,
                        "msg": _redact(str(exc), credentials)[:120],
                    }
                )
            time.sleep(0.05)

        time.sleep(0.35)
        after = _fetch_active_orders_for_symbol(credentials, native)
        still = {
            str(r.get("orderID") or r.get("orderId") or "")
            for r in after
            if (str(r.get("side") or "").strip() == want
                or str(r.get("side") or "").strip().lower() == side_in)
        }
        confirmed = sum(1 for oid in target_ids if oid not in still)
        remaining = len(target_ids) - confirmed
        verified = remaining == 0
        result = CanonicalCancelGroupResult(
            symbol=_display_symbol(native) or native,
            side=side_in,
            targeted_order_count=len(target_ids),
            cancelled_order_count=cancelled,
            confirmed_absent_count=confirmed,
            remaining_target_count=remaining,
            verified=verified,
            partial=not verified,
            status="success" if verified else "partial",
            batch_count=len(batches),
            batches=batches,
            requested_cancel_count=len(target_ids),
            verified_cancel_count=confirmed,
        )
        if verified:
            return make_success(
                operation="cancel_order_group",
                exchange=name,
                account=credentials["account"],
                cancel_group=result,
            )
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=credentials["account"],
            code="PARTIAL_CANCELLATION" if cancelled else "CANCEL_FAILED",
            message=(
                f"Cancelled {confirmed}/{len(target_ids)} {side_in} orders on {native}; "
                f"{remaining} still open."
            ),
            cancel_group=result,
        )
    except ValueError as exc:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=credentials["account"],
            code="INSTRUMENT_NOT_FOUND" if str(exc) == "INSTRUMENT_NOT_FOUND" else "INVALID_REQUEST",
            message=sanitize_error_message(str(exc)),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=credentials["account"],
            code="PHEMEX_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _cancel_protection_orders(
    credentials: Mapping[str, str],
    native: str,
    position_side: str,
    *,
    kinds: Optional[Sequence[str]] = None,
) -> int:
    """Cancel untriggered TP/SL close-on-trigger orders. Returns cancelled count."""
    want_kinds = set(kinds or ("tp", "sl"))
    rows = _fetch_active_orders_for_symbol(credentials, native)
    _tp, _sl, _tc, _sc, prot_rows = _extract_protections(rows, native, position_side)
    cancelled = 0
    for row in prot_rows:
        kind = _protection_kind(row, position_side)
        if kind not in want_kinds:
            continue
        oid = str(row.get("orderID") or row.get("orderId") or "").strip()
        if not oid:
            continue
        pos_side = "Long" if position_side == "long" else "Short"
        query = (
            f"symbol={urllib.parse.quote(native)}"
            f"&orderID={urllib.parse.quote(oid)}"
            f"&posSide={urllib.parse.quote(pos_side)}"
        )
        payload = _signed_request(credentials, "DELETE", _PATH_G_ORDERS_CANCEL, query=query)
        if _phemex_ok(payload):
            cancelled += 1
        time.sleep(0.05)
    return cancelled


def _attach_protections_via_bump(
    credentials: Mapping[str, str],
    *,
    native: str,
    meta: Mapping[str, Any],
    position_side: str,
    tp: Optional[Decimal],
    sl: Optional[Decimal],
) -> None:
    """Phemex only materializes position TP/SL after a fill carrying takeProfitRp/stopLossRp.

    Direct Stop/MIT placement returns TE_TRIGGER_INVALID. Workaround:
    buy/sell the minimum step in the position direction with TP/SL attached,
    then immediately reduce that step back. TP/SL orders remain on the position.
    """
    step = meta["qty_step"] if meta.get("qty_step") and meta["qty_step"] > 0 else Decimal("0.001")
    min_qty = meta["min_qty"] if meta.get("min_qty") and meta["min_qty"] > 0 else step
    bump = max(step, min_qty)
    mark = _fetch_mark_price(credentials, native)
    tick = meta["tick_size"] if meta.get("tick_size") and meta["tick_size"] > 0 else Decimal("0.1")
    if position_side == "long":
        open_side = "Buy"
        close_side = "Sell"
        pos_side = "Long"
        # cross the book to fill bump
        open_px = _quantize(mark * Decimal("1.002"), tick, rounding=ROUND_HALF_UP)
        close_px = _quantize(mark * Decimal("0.998"), tick, rounding=ROUND_HALF_UP)
    else:
        open_side = "Sell"
        close_side = "Buy"
        pos_side = "Short"
        open_px = _quantize(mark * Decimal("0.998"), tick, rounding=ROUND_HALF_UP)
        close_px = _quantize(mark * Decimal("1.002"), tick, rounding=ROUND_HALF_UP)

    body_obj: Dict[str, Any] = {
        "symbol": native,
        "clOrdID": uuid.uuid4().hex[:32],
        "side": open_side,
        "posSide": pos_side,
        "ordType": "Limit",
        "priceRp": _format_decimal(open_px),
        "orderQtyRq": _format_decimal(bump),
        "timeInForce": "ImmediateOrCancel",
    }
    if tp is not None and tp > 0:
        body_obj["takeProfitRp"] = _format_decimal(tp)
    if sl is not None and sl > 0:
        body_obj["stopLossRp"] = _format_decimal(sl)
    if "takeProfitRp" not in body_obj and "stopLossRp" not in body_obj:
        return
    payload = _signed_request(
        credentials, "POST", _PATH_G_ORDERS, body=json.dumps(body_obj, separators=(",", ":"))
    )
    if not _phemex_ok(payload):
        raise RuntimeError(str(payload.get("msg") or payload.get("code") or "TP/SL attach failed"))
    time.sleep(0.45)
    # reduce bump back (prefer market reduce-only)
    close_body = {
        "symbol": native,
        "clOrdID": uuid.uuid4().hex[:32],
        "side": close_side,
        "posSide": pos_side,
        "ordType": "Market",
        "orderQtyRq": _format_decimal(bump),
        "timeInForce": "ImmediateOrCancel",
        "reduceOnly": True,
    }
    close_payload = _signed_request(
        credentials, "POST", _PATH_G_ORDERS, body=json.dumps(close_body, separators=(",", ":"))
    )
    if not _phemex_ok(close_payload):
        # fallback aggressive limit reduce
        close_body = {
            "symbol": native,
            "clOrdID": uuid.uuid4().hex[:32],
            "side": close_side,
            "posSide": pos_side,
            "ordType": "Limit",
            "priceRp": _format_decimal(close_px),
            "orderQtyRq": _format_decimal(bump),
            "timeInForce": "ImmediateOrCancel",
            "reduceOnly": True,
        }
        close_payload = _signed_request(
            credentials, "POST", _PATH_G_ORDERS, body=json.dumps(close_body, separators=(",", ":"))
        )
        if not _phemex_ok(close_payload):
            raise RuntimeError(
                str(close_payload.get("msg") or close_payload.get("code") or "failed to reverse TP/SL bump")
            )


def _set_protection(account: str, request: Mapping[str, Any], *, kind: str) -> CanonicalResponse:
    operation = "set_tp" if kind == "tp" else "set_sl"
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set PHEMEX_<ACCOUNT>_ID and PHEMEX_<ACCOUNT>_APISECRET.",
        )
    requested_symbol = str(request.get("symbol") or "").strip()
    price_text = str(request.get("price") or "").strip()
    try:
        price_val = Decimal(price_text) if price_text else Decimal("0")
    except Exception:  # noqa: BLE001
        return make_failure(
            operation=operation,
            exchange=name,
            account=credentials["account"],
            code="INVALID_PRICE",
            message="Protection price must be a number (use 0 to remove).",
        )
    try:
        pos, native, meta = _find_open_position(credentials, requested_symbol)
        mark = _fetch_mark_price(credentials, native)
        # Load existing protections to preserve the other leg.
        rows = _fetch_active_orders_for_symbol(credentials, native)
        cur_tp, cur_sl, _tc, _sc, _prot = _extract_protections(rows, native, pos.side)
        remove = price_val <= 0
        if not remove:
            # Direction checks
            if kind == "tp":
                if pos.side == "long" and price_val <= mark:
                    return make_failure(
                        operation=operation,
                        exchange=name,
                        account=credentials["account"],
                        code="INVALID_TP_PRICE",
                        message="Long TP must be above mark price.",
                        position_action=CanonicalPositionActionResult(
                            operation=operation, symbol=pos.symbol, verified=False, status="failed",
                            current_side=pos.side, current_size=pos.size,
                        ),
                    )
                if pos.side == "short" and price_val >= mark:
                    return make_failure(
                        operation=operation,
                        exchange=name,
                        account=credentials["account"],
                        code="INVALID_TP_PRICE",
                        message="Short TP must be below mark price.",
                        position_action=CanonicalPositionActionResult(
                            operation=operation, symbol=pos.symbol, verified=False, status="failed",
                            current_side=pos.side, current_size=pos.size,
                        ),
                    )
            else:
                if pos.side == "long" and price_val >= mark:
                    return make_failure(
                        operation=operation,
                        exchange=name,
                        account=credentials["account"],
                        code="INVALID_SL_PRICE",
                        message="Long SL must be below mark price.",
                        position_action=CanonicalPositionActionResult(
                            operation=operation, symbol=pos.symbol, verified=False, status="failed",
                            current_side=pos.side, current_size=pos.size,
                        ),
                    )
                if pos.side == "short" and price_val <= mark:
                    return make_failure(
                        operation=operation,
                        exchange=name,
                        account=credentials["account"],
                        code="INVALID_SL_PRICE",
                        message="Short SL must be above mark price.",
                        position_action=CanonicalPositionActionResult(
                            operation=operation, symbol=pos.symbol, verified=False, status="failed",
                            current_side=pos.side, current_size=pos.size,
                        ),
                    )

        # Always clear existing legs we will rewrite so we don't stack duplicates.
        if remove:
            cancelled = _cancel_protection_orders(
                credentials, native, pos.side, kinds=(kind,)
            )
            return make_success(
                operation=operation,
                exchange=name,
                account=credentials["account"],
                position_action=CanonicalPositionActionResult(
                    operation=operation,
                    symbol=pos.symbol,
                    verified=True,
                    removed=True,
                    status="success",
                    current_side=pos.side,
                    current_size=pos.size,
                    message=f"Removed {kind.upper()} ({cancelled} order(s) cancelled).",
                ),
            )

        # Cancel both legs then re-attach desired combination (preserve other leg).
        _cancel_protection_orders(credentials, native, pos.side, kinds=("tp", "sl"))
        time.sleep(0.2)
        new_tp = price_val if kind == "tp" else (_decimal_or_zero(cur_tp) if cur_tp else None)
        new_sl = price_val if kind == "sl" else (_decimal_or_zero(cur_sl) if cur_sl else None)
        if new_tp is not None and new_tp <= 0:
            new_tp = None
        if new_sl is not None and new_sl <= 0:
            new_sl = None
        if new_tp is None and new_sl is None:
            return make_success(
                operation=operation,
                exchange=name,
                account=credentials["account"],
                position_action=CanonicalPositionActionResult(
                    operation=operation,
                    symbol=pos.symbol,
                    verified=True,
                    status="success",
                    current_side=pos.side,
                    current_size=pos.size,
                    message="No protections left to set.",
                ),
            )
        _attach_protections_via_bump(
            credentials,
            native=native,
            meta=meta,
            position_side=pos.side,
            tp=new_tp,
            sl=new_sl,
        )
        time.sleep(0.4)
        # verify
        rows2 = _fetch_active_orders_for_symbol(credentials, native)
        tp2, sl2, _a, _b, _c = _extract_protections(rows2, native, pos.side)
        want = _format_decimal(price_val)
        got = tp2 if kind == "tp" else sl2
        verified = got is not None and abs(_decimal_or_zero(got) - price_val) <= meta.get(
            "tick_size", Decimal("0.1")
        ) * 2
        # size should be back near original
        pos_after, _, _ = _find_open_position(credentials, requested_symbol)
        return make_success(
            operation=operation,
            exchange=name,
            account=credentials["account"],
            position_action=CanonicalPositionActionResult(
                operation=operation,
                symbol=pos.symbol,
                verified=bool(verified),
                price=want,
                status="success" if verified else "submitted",
                current_side=pos_after.side,
                current_size=pos_after.size,
                message=(
                    f"Set {kind.upper()}={want}"
                    + (f" (also kept other leg)" if (new_tp and new_sl) else "")
                    + ". Note: Phemex attaches TP/SL via a min-size bump+reduce."
                ),
            ),
        )
    except ValueError as exc:
        code = str(exc)
        return make_failure(
            operation=operation,
            exchange=name,
            account=credentials["account"],
            code=code if code in {"NO_OPEN_POSITION", "INSTRUMENT_NOT_FOUND"} else "INVALID_REQUEST",
            message="No open position for symbol." if code == "NO_OPEN_POSITION" else sanitize_error_message(str(exc)),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation=operation,
            exchange=name,
            account=credentials["account"],
            code="PHEMEX_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _close_position(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set PHEMEX_<ACCOUNT>_ID and PHEMEX_<ACCOUNT>_APISECRET.",
        )
    requested_symbol = str(request.get("symbol") or "").strip()
    try:
        pos, native, meta = _find_open_position(credentials, requested_symbol)
        # Cancel protections first so they don't race.
        _cancel_protection_orders(credentials, native, pos.side, kinds=("tp", "sl"))
        size = _decimal_or_zero(pos.size)
        if size <= 0:
            raise ValueError("NO_OPEN_POSITION")
        if pos.side == "long":
            close_side, pos_side = "Sell", "Long"
        else:
            close_side, pos_side = "Buy", "Short"
        body = {
            "symbol": native,
            "clOrdID": uuid.uuid4().hex[:32],
            "side": close_side,
            "posSide": pos_side,
            "ordType": "Market",
            "orderQtyRq": _format_decimal(size),
            "timeInForce": "ImmediateOrCancel",
            "reduceOnly": True,
        }
        payload = _signed_request(
            credentials, "POST", _PATH_G_ORDERS, body=json.dumps(body, separators=(",", ":"))
        )
        if not _phemex_ok(payload):
            # aggressive limit fallback
            mark = _fetch_mark_price(credentials, native)
            tick = meta["tick_size"] if meta.get("tick_size") and meta["tick_size"] > 0 else Decimal("0.1")
            if pos.side == "long":
                px = _quantize(mark * Decimal("0.995"), tick, rounding=ROUND_HALF_UP)
            else:
                px = _quantize(mark * Decimal("1.005"), tick, rounding=ROUND_HALF_UP)
            body = {
                "symbol": native,
                "clOrdID": uuid.uuid4().hex[:32],
                "side": close_side,
                "posSide": pos_side,
                "ordType": "Limit",
                "priceRp": _format_decimal(px),
                "orderQtyRq": _format_decimal(size),
                "timeInForce": "ImmediateOrCancel",
                "reduceOnly": True,
            }
            payload = _signed_request(
                credentials, "POST", _PATH_G_ORDERS, body=json.dumps(body, separators=(",", ":"))
            )
            if not _phemex_ok(payload):
                return make_failure(
                    operation="close_position",
                    exchange=name,
                    account=credentials["account"],
                    code="CLOSE_FAILED",
                    message=_redact(
                        sanitize_error_message(str(payload.get("msg") or payload.get("code") or "close failed")),
                        credentials,
                    ),
                    position_action=CanonicalPositionActionResult(
                        operation="close_position",
                        symbol=pos.symbol,
                        verified=False,
                        status="failed",
                        current_side=pos.side,
                        current_size=pos.size,
                    ),
                )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        oid = str(data.get("orderID") or data.get("orderId") or "").strip() or None
        time.sleep(0.6)
        # verify flat
        try:
            _find_open_position(credentials, requested_symbol)
            flat = False
            still_side, still_size = pos.side, pos.size
        except ValueError:
            flat = True
            still_side, still_size = None, "0"
        return make_success(
            operation="close_position",
            exchange=name,
            account=credentials["account"],
            position_action=CanonicalPositionActionResult(
                operation="close_position",
                symbol=pos.symbol,
                verified=flat,
                status="success" if flat else "submitted",
                exchange_order_id=oid,
                current_side=still_side,
                current_size=still_size,
                message="Position closed." if flat else "Close submitted; flat not yet confirmed.",
            ),
        )
    except ValueError as exc:
        code = str(exc)
        return make_failure(
            operation="close_position",
            exchange=name,
            account=credentials["account"],
            code=code if code in {"NO_OPEN_POSITION", "INSTRUMENT_NOT_FOUND"} else "INVALID_REQUEST",
            message="No open position for symbol." if code == "NO_OPEN_POSITION" else sanitize_error_message(str(exc)),
            position_action=CanonicalPositionActionResult(
                operation="close_position",
                symbol=str(request.get("symbol") or ""),
                verified=code == "NO_OPEN_POSITION",
                status="noop" if code == "NO_OPEN_POSITION" else "failed",
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="close_position",
            exchange=name,
            account=credentials["account"],
            code="PHEMEX_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def execute(request: Dict[str, Any]) -> CanonicalResponse:
    if not isinstance(request, dict):
        return make_failure(
            operation="",
            exchange=name,
            account="",
            code="INVALID_REQUEST",
            message="Request must be a dict.",
        )
    operation = str(request.get("operation") or "").strip()
    account = str(request.get("account") or "").strip()
    if not operation:
        return make_failure(
            operation="",
            exchange=name,
            account=account,
            code="INVALID_REQUEST",
            message="Missing 'operation'.",
        )
    if not account:
        return make_failure(
            operation=operation,
            exchange=name,
            account="",
            code="MISSING_ACCOUNT",
            message="Missing 'account'.",
        )
    try:
        if operation == "balance":
            return _balance(account)
        if operation == "positions_orders":
            return _positions_orders(account)
        if operation == "positions_management":
            return _positions_management(account)
        if operation == "new_order":
            return _new_order(account, request)
        if operation == "ladder":
            return _ladder(account, request)
        if operation == "cancel_order_group":
            return _cancel_order_group(account, request)
        if operation == "set_tp":
            return _set_protection(account, request, kind="tp")
        if operation == "set_sl":
            return _set_protection(account, request, kind="sl")
        if operation == "close_position":
            return _close_position(account, request)
        if operation == "resolve_instrument":
            return _resolve_instrument(account, request)
        if operation == "list_instruments":
            return _list_instruments(account, request)
        if operation == "market_price":
            return _market_price(account, request)
        if operation == "candles":
            return handle_candles_operation(name, account, request)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="PHEMEX_ERROR",
            message=sanitize_error_message(str(exc)),
        )
    return make_failure(
        operation=operation,
        exchange=name,
        account=account,
        code="NOT_IMPLEMENTED",
        message=f"Phemex does not implement '{operation}' yet.",
    )
