"""MEXC Spot agent for /tradespot.

Uses the existing MEXC credential convention from ``x_mexc_agent.py``:
``MEXC_<ALIAS>_ACCESSKEY`` + ``MEXC_<ALIAS>_SECRETKEY`` (plus common
API-key/secret aliases). This agent is strictly separate from the futures
``x_mexc_agent.py`` used by /trade.

Read operations plus LIMIT ``new_order`` and grouped ``cancel_orders``.
Ladder, MARKET, and exchange-wide cancel-all remain disabled.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import socket
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..canonical import (
    CanonicalBalance,
    CanonicalCancelGroupResult,
    CanonicalInstrument,
    CanonicalMarketPrice,
    CanonicalOrderGroup,
    CanonicalOrderResult,
    CanonicalResponse,
    make_failure,
    make_success,
    normalize_balance,
    sanitize_error_message,
)

logger = logging.getLogger(__name__)

name = "mexc"

# MEXC spot enforces a per-child minimum notional of 1 USDT/USDC (cost.min
# in CCXT markets for every spot pair tested; minNotional is not always
# published in exchangeInfo filters). Stamp this on the resolved
# instrument so the exchange-neutral ladder planner can enforce it
# per-child without baking an exchange default into spot_ladder.py.
MEXC_SPOT_MIN_NOTIONAL = "1"

DEFAULT_SPOT_BASE = "https://api.mexc.com"
API_TIMEOUT_SECONDS = 20
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_KEY_ALIASES = ("ACCESSKEY", "ACCESS_KEY", "APIKEY", "API_KEY", "KEY")
_SECRET_ALIASES = ("SECRETKEY", "SECRET_KEY", "APISECRET", "API_SECRET", "SECRET")
_SPOT_BASE_ALIASES = ("SPOT_BASE", "SPOT_URL")
_STABLE_ASSETS = frozenset({"USDT", "USDC", "USD", "FDUSD", "BUSD", "TUSD", "DAI"})
_MARKET_CACHE: Dict[str, Any] = {"ts": 0.0, "symbols": [], "by_symbol": {}}
_MARKET_CACHE_TTL = 300.0


# ---------------------------------------------------------------------------
# Environment / credentials
# ---------------------------------------------------------------------------


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _load_dotenv_values(path: Path) -> Dict[str, str]:
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        try:
            text = path.read_text(encoding="latin-1")
        except OSError:
            return {}
    values: Dict[str, str] = {}
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


def _combined_mexc_env() -> Dict[str, Tuple[str, str, str]]:
    out: Dict[str, Tuple[str, str, str]] = {}
    for k, v in os.environ.items():
        if k.upper().startswith("MEXC_"):
            out.setdefault(k.upper(), (k, str(v), "env"))
    for k, v in _load_dotenv_values(_hermes_home() / ".env").items():
        if k.upper().startswith("MEXC_"):
            out.setdefault(k.upper(), (k, str(v), "dotenv"))
    return out


def _parse_alias_and_suffix(upper_key: str) -> Optional[Tuple[str, str]]:
    if not upper_key.startswith("MEXC_"):
        return None
    rest = upper_key[len("MEXC_") :]
    known = sorted(set(_KEY_ALIASES + _SECRET_ALIASES + _SPOT_BASE_ALIASES), key=len, reverse=True)
    for suffix in known:
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
    for upper_key, (_actual, value, _src) in _combined_mexc_env().items():
        parsed = _parse_alias_and_suffix(upper_key)
        if parsed is None:
            continue
        alias_upper, suffix = parsed
        alias = alias_upper.lower()
        slot = buckets.setdefault(alias, {})
        val = value.strip()
        if suffix in _KEY_ALIASES:
            slot.setdefault("access_key", val)
        elif suffix in _SECRET_ALIASES:
            slot.setdefault("secret_key", val)
        elif suffix in _SPOT_BASE_ALIASES:
            slot.setdefault("spot_base", val.rstrip("/"))

    env_map = {k: v for k, (_, v, _) in _combined_mexc_env().items()}
    global_spot = (env_map.get("MEXC_SPOT_BASE") or env_map.get("MEXC_SPOT_URL") or "").strip().rstrip("/")

    complete: Dict[str, Dict[str, str]] = {}
    for alias, fields in buckets.items():
        if fields.get("access_key") and fields.get("secret_key"):
            if not fields.get("spot_base") and global_spot:
                fields["spot_base"] = global_spot
            complete[alias] = fields
    return complete


def list_accounts() -> List[str]:
    return sorted(_discover_credential_map().keys())


def capabilities() -> List[str]:
    # NOTE: `ladder` is intentionally NOT advertised. Phase 1 is source-only —
    # the wizard supports the ladder flow when `_ladder_preview_only` is True,
    # the agent always rejects `ladder` writes with `NOT_IMPLEMENTED`. Future
    # phases will flip this on and add the real submit path.
    return [
        "balance",
        "orders",
        "open_orders",
        "list_instruments",
        "resolve_instrument",
        "market_price",
        "new_order",
        "cancel_orders",
    ]


def _lookup_credentials(account: str) -> Optional[Dict[str, str]]:
    alias = str(account or "").strip().lower()
    if not alias:
        return None
    fields = _discover_credential_map().get(alias)
    if not fields:
        return None
    return {
        "account": alias,
        "access_key": fields["access_key"],
        "secret_key": fields["secret_key"],
        "spot_base": fields.get("spot_base") or DEFAULT_SPOT_BASE,
    }


def _redact(text: Any, credentials: Optional[Mapping[str, str]] = None) -> str:
    rendered = str(text or "")
    if credentials:
        for key in ("access_key", "secret_key"):
            value = str(credentials.get(key) or "").strip()
            if len(value) >= 6:
                rendered = rendered.replace(value, "***")
    return rendered


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _json_request(method: str, url: str, headers: Mapping[str, str]) -> Any:
    req = urllib.request.Request(url, method=method.upper(), headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8")
    except (TimeoutError, socket.timeout) as exc:
        raise urllib.error.URLError("timed out") from exc
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except Exception:  # noqa: BLE001
            parsed = {"code": exc.code, "msg": exc.reason}
        if isinstance(parsed, dict):
            parsed.setdefault("http_status", exc.code)
            return parsed
        return {"http_status": exc.code, "msg": str(parsed)}
    parsed = json.loads(body)
    return parsed


def _public_request(path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
    qs = urllib.parse.urlencode(dict(params or {}))
    url = f"{DEFAULT_SPOT_BASE}{path}{('?' + qs) if qs else ''}"
    return _json_request("GET", url, {"Accept": "application/json", "User-Agent": _USER_AGENT})


def _signed_request(
    credentials: Mapping[str, str],
    method: str,
    path: str,
    params: Optional[Mapping[str, Any]] = None,
) -> Any:
    base = str(credentials.get("spot_base") or DEFAULT_SPOT_BASE).rstrip("/")
    payload = dict(params or {})
    payload["timestamp"] = str(int(time.time() * 1000))
    payload.setdefault("recvWindow", "5000")
    qs = urllib.parse.urlencode(payload)
    signature = hmac.new(credentials["secret_key"].encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()
    url = f"{base}{path}?{qs}&signature={signature}"
    return _json_request(
        method,
        url,
        {
            "X-MEXC-APIKEY": credentials["access_key"],
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        },
    )


def _response_error(payload: Any, fallback: str) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    if payload.get("orderId") is not None or "balances" in payload:
        return None
    code = payload.get("code")
    if code in (None, 0, "0", 200, "200"):
        if "msg" not in payload and "message" not in payload:
            return None
        if code in (0, "0", 200, "200"):
            return None
        return None
    if "code" in payload or "msg" in payload or "message" in payload:
        return str(payload.get("msg") or payload.get("message") or payload.get("code") or fallback)
    return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value is not None else "0"))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _format_decimal(value: Decimal) -> str:
    q = value.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    text = format(q.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _nonzero_balances(rows: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        asset = str(row.get("asset") or "").upper().strip()
        if not asset:
            continue
        free = _to_decimal(row.get("free"))
        locked = _to_decimal(row.get("locked"))
        total = free + locked
        if total <= 0:
            continue
        out.append(
            {
                "asset": asset,
                "free": _format_decimal(free),
                "locked": _format_decimal(locked),
                "total": _format_decimal(total),
            }
        )
    return sorted(out, key=lambda x: (x["asset"] not in _STABLE_ASSETS, x["asset"]))


def _parse_order(row: Mapping[str, Any]) -> Dict[str, Any]:
    orig = _to_decimal(row.get("origQty"))
    executed = _to_decimal(row.get("executedQty"))
    remaining = max(orig - executed, Decimal("0"))
    symbol = str(row.get("symbol") or "").upper()
    side = str(row.get("side") or "").upper()
    order_type = str(row.get("type") or "").upper()
    price = _to_decimal(row.get("price"))
    return {
        "symbol": symbol,
        "pair": _display_pair(symbol),
        "side": side,
        "type": order_type,
        "price": _format_decimal(price),
        "orig_qty": _format_decimal(orig),
        "executed_qty": _format_decimal(executed),
        "remaining_qty": _format_decimal(remaining),
        "status": str(row.get("status") or "").upper(),
        "order_id": str(row.get("orderId") or row.get("order_id") or ""),
        "client_order_id": str(row.get("clientOrderId") or ""),
    }


def _display_pair(symbol: str) -> str:
    sym = str(symbol or "").upper()
    for quote in ("USDT", "USDC", "FDUSD", "BTC", "ETH", "MX", "USD"):
        if sym.endswith(quote) and len(sym) > len(quote):
            return f"{sym[:-len(quote)]}/{quote}"
    return sym


def _group_orders(parsed: List[Dict[str, Any]]) -> List[CanonicalOrderGroup]:
    buckets: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for row in parsed:
        symbol = str(row.get("pair") or row.get("symbol") or "")
        side = str(row.get("side") or "").lower()
        order_type = str(row.get("type") or "")
        if not symbol or not side:
            continue
        buckets.setdefault((symbol, side, order_type), []).append(row)
    groups: List[CanonicalOrderGroup] = []
    for (symbol, side, order_type), rows in sorted(buckets.items()):
        total_size = sum((_to_decimal(r.get("remaining_qty")) for r in rows), Decimal("0"))
        prices = [_to_decimal(r.get("price")) for r in rows if _to_decimal(r.get("price")) > 0]
        notional = sum((_to_decimal(r.get("price")) * _to_decimal(r.get("remaining_qty")) for r in rows), Decimal("0"))
        vwap = notional / total_size if total_size > 0 and prices else Decimal("0")
        groups.append(
            CanonicalOrderGroup(
                symbol=symbol,
                side=side,
                order_count=len(rows),
                total_size=_format_decimal(total_size),
                vwap=_format_decimal(vwap) if vwap > 0 else "",
                min_price=_format_decimal(min(prices)) if prices else "",
                max_price=_format_decimal(max(prices)) if prices else "",
                display_type=order_type,
            )
        )
    return groups


# ---------------------------------------------------------------------------
# Market metadata / eligibility
# ---------------------------------------------------------------------------


def _exchange_info() -> Dict[str, Any]:
    now = time.time()
    if _MARKET_CACHE["symbols"] and now - float(_MARKET_CACHE["ts"]) < _MARKET_CACHE_TTL:
        return {"symbols": list(_MARKET_CACHE["symbols"]), "by_symbol": dict(_MARKET_CACHE["by_symbol"])}
    payload = _public_request("/api/v3/exchangeInfo")
    if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
        raise RuntimeError(str(payload.get("msg") if isinstance(payload, dict) else "exchangeInfo failed"))
    symbols = [row for row in payload.get("symbols") or [] if isinstance(row, Mapping)]
    by_symbol = {str(row.get("symbol") or "").upper(): dict(row) for row in symbols if row.get("symbol")}
    _MARKET_CACHE.update({"ts": now, "symbols": symbols, "by_symbol": by_symbol})
    return {"symbols": list(symbols), "by_symbol": dict(by_symbol)}


def _self_symbols(credentials: Mapping[str, str]) -> Tuple[Optional[set[str]], Optional[str]]:
    payload = _signed_request(credentials, "GET", "/api/v3/selfSymbols")
    err = _response_error(payload, "selfSymbols failed")
    if err:
        return None, err
    raw: Any
    if isinstance(payload, dict):
        raw = payload.get("symbols") or payload.get("data") or payload.get("symbol")
    else:
        raw = payload
    if isinstance(raw, list):
        return {str(x).upper() for x in raw if str(x).strip()}, None
    return None, "selfSymbols returned an unsupported shape"


def _market_row(symbol: str) -> Optional[Dict[str, Any]]:
    key = str(symbol or "").upper().replace("/", "").replace("-", "").replace("_", "")
    return _exchange_info()["by_symbol"].get(key)


def _instrument_from_market(row: Mapping[str, Any]) -> Dict[str, Any]:
    symbol = str(row.get("symbol") or "").upper()
    raw_filters = row.get("filters")
    filters: List[Any] = raw_filters if isinstance(raw_filters, list) else []
    min_qty = ""
    max_qty = ""
    lot_step = ""
    tick_size = ""
    min_notional = ""
    for f in filters:
        if not isinstance(f, Mapping):
            continue
        ftype = str(f.get("filterType") or "")
        if ftype == "LOT_SIZE":
            lot_step = str(f.get("stepSize") or "")
            min_qty = str(f.get("minQty") or "")
            max_qty = str(f.get("maxQty") or "")
        elif ftype == "PRICE_FILTER":
            tick_size = str(f.get("tickSize") or "")
        elif ftype in {"MIN_NOTIONAL", "NOTIONAL"}:
            min_notional = str(f.get("minNotional") or f.get("notional") or "")
    size_step = _decimal_step(lot_step)
    if size_step <= 0:
        size_step = _decimal_step(row.get("baseSizePrecision"))
    if size_step <= 0:
        size_step = _places_increment(row.get("baseAssetPrecision"))
    price_tick = _decimal_step(tick_size)
    if price_tick <= 0:
        for key in ("quotePrecision", "quoteAssetPrecision"):
            price_tick = _places_increment(row.get(key))
            if price_tick > 0:
                break
    base = str(row.get("baseAsset") or "")
    quote = str(row.get("quoteAsset") or "")
    order_types = [str(x) for x in row.get("orderTypes") or []]
    is_allowed = bool(row.get("isSpotTradingAllowed"))
    status = str(row.get("status") or "")
    # MEXC spot enforces a per-child minimum of 1 USDT/USDC; exchangeInfo
    # does NOT publish MIN_NOTIONAL consistently, so the agent stamps the
    # exchange's policy on the resolved instrument for the planner to
    # consume. Callers can override via the dict before passing into
    # spot_ladder.compute_ladder_with_min_notional.
    resolved_min_notional = _decimal_step(min_notional)
    if resolved_min_notional <= 0:
        resolved_min_notional = _decimal_step(MEXC_SPOT_MIN_NOTIONAL)
    return {
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "baseAsset": base,
        "quoteAsset": quote,
        "display_name": f"{base}/{quote}" if base and quote else symbol,
        "status": status,
        "isSpotTradingAllowed": is_allowed,
        "orderTypes": order_types,
        "size_step": _format_step(size_step),
        "price_tick": _format_step(price_tick),
        "min_qty": _format_step(_decimal_step(min_qty)),
        "max_qty": _format_step(_decimal_step(max_qty)),
        "min_notional": _format_step(resolved_min_notional),
        "tick_size": tick_size,
        "step_size": lot_step,
        "api_eligible": status.upper() in {"1", "ENABLED", "TRADING"} and is_allowed,
    }


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _missing_account(operation: str, account: str) -> CanonicalResponse:
    return make_failure(
        operation=operation,
        exchange=name,
        account=account,
        code="ACCOUNT_NOT_FOUND",
        message="Set MEXC_<ACCOUNT>_ACCESSKEY and MEXC_<ACCOUNT>_SECRETKEY.",
    )


def _balance(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return _missing_account("balance", account)
    try:
        payload = _signed_request(credentials, "GET", "/api/v3/account")
        err = _response_error(payload, "account failed")
        if err:
            return make_failure(
                operation="balance",
                exchange=name,
                account=credentials["account"],
                code="MEXC_SPOT_AUTH_FAILED",
                message=_redact(sanitize_error_message(err), credentials),
                exchange_reason=_redact(sanitize_error_message(err), credentials),
            )
        balances = _nonzero_balances(payload.get("balances") if isinstance(payload, dict) else [])
        stable_total = sum((_to_decimal(x["total"]) for x in balances if x["asset"] in _STABLE_ASSETS), Decimal("0"))
        unit = "USDT"
        value = stable_total
        if value <= 0 and balances:
            unit = balances[0]["asset"]
            value = _to_decimal(balances[0]["total"])
        return make_success(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            balance=normalize_balance(value, unit),
            data={
                "source": "mexc_spot_account",
                "accountType": payload.get("accountType") if isinstance(payload, dict) else None,
                "permissions": payload.get("permissions") if isinstance(payload, dict) else None,
                "canTrade": payload.get("canTrade") if isinstance(payload, dict) else None,
                "canWithdraw": payload.get("canWithdraw") if isinstance(payload, dict) else None,
                "canDeposit": payload.get("canDeposit") if isinstance(payload, dict) else None,
                "assets": balances[:50],
                "nonzero_asset_count": len(balances),
            },
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            code="MEXC_SPOT_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _orders(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return _missing_account("orders", account)
    try:
        payload = _signed_request(credentials, "GET", "/api/v3/openOrders")
        err = _response_error(payload, "openOrders failed")
        if err:
            return make_failure(
                operation="orders",
                exchange=name,
                account=credentials["account"],
                code="MEXC_SPOT_ORDERS_FAILED",
                message=_redact(sanitize_error_message(err), credentials),
                exchange_reason=_redact(sanitize_error_message(err), credentials),
            )
        rows = payload if isinstance(payload, list) else payload.get("orders", []) if isinstance(payload, dict) else []
        parsed = [_parse_order(row) for row in rows if isinstance(row, Mapping)]
        groups = _group_orders(parsed)
        return make_success(
            operation="orders",
            exchange=name,
            account=credentials["account"],
            open_order_count=len(parsed),
            order_groups=groups,
            data={"source": "mexc_spot_openOrders", "orders": parsed[:100]},
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="orders",
            exchange=name,
            account=credentials["account"],
            code="MEXC_SPOT_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _list_instruments(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    self_set: Optional[set[str]] = None
    self_error: Optional[str] = None
    if credentials is not None:
        try:
            self_set, self_error = _self_symbols(credentials)
        except Exception as exc:  # noqa: BLE001
            self_error = _redact(sanitize_error_message(str(exc)), credentials)
    try:
        q = str(request.get("query") or request.get("symbol") or "").strip().upper().replace("/", "")
        rows = _exchange_info()["symbols"]
        out: List[Dict[str, Any]] = []
        for row in rows:
            info = _instrument_from_market(row)
            symbol = str(info["symbol"]).upper()
            if q and q not in symbol and q not in str(info.get("baseAsset") or "").upper():
                continue
            if self_set is not None:
                info["api_enabled_for_key"] = symbol in self_set
                info["api_eligible"] = bool(info["api_eligible"] and symbol in self_set)
            out.append(info)
            if len(out) >= int(request.get("limit") or 100):
                break
        instruments = [
            CanonicalInstrument(
                requested_symbol=str(item["symbol"]),
                symbol=str(item["symbol"]),
                display_name=str(item.get("display_name") or item["symbol"]),
                price_increment=str(item.get("price_tick") or ""),
                size_increment=str(item.get("size_step") or ""),
                minimum_size=str(item.get("min_qty") or ""),
            ).to_dict()
            for item in out
        ]
        for idx, item in enumerate(out):
            instruments[idx].update(item)
        return make_success(
            operation="list_instruments",
            exchange=name,
            account=str(account or ""),
            data={
                "source": "mexc_spot_exchangeInfo",
                "instruments": instruments,
                "count": len(instruments),
                "selfSymbols_available": self_set is not None,
                "selfSymbols_count": len(self_set) if self_set is not None else None,
                "selfSymbols_error": self_error,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account=str(account or ""),
            code="MEXC_SPOT_MARKETS_FAILED",
            message=sanitize_error_message(str(exc)),
        )


def _resolve_instrument(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    requested = str(request.get("symbol") or request.get("query") or "").strip().upper()
    if not requested:
        base = str(request.get("base") or request.get("baseAsset") or "").strip().upper()
        quote = str(request.get("quote") or request.get("quoteAsset") or "").strip().upper()
        if base and quote:
            requested = f"{base}{quote}"
    row = _market_row(requested)
    if row is None:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=str(account or ""),
            code="INSTRUMENT_NOT_FOUND",
            message="MEXC spot symbol not found.",
        )
    info = _instrument_from_market(row)
    if not info.get("size_step") or not info.get("price_tick"):
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=str(account or ""),
            code="INSTRUMENT_CONSTRAINTS_UNAVAILABLE",
            message="MEXC spot instrument is missing size or price constraints.",
        )
    inst = CanonicalInstrument(
        requested_symbol=requested,
        symbol=str(info["symbol"]),
        display_name=str(info.get("display_name") or info["symbol"]),
        price_increment=str(info.get("price_tick") or ""),
        size_increment=str(info.get("size_step") or ""),
        minimum_size=str(info.get("min_qty") or ""),
    )
    return make_success(
        operation="resolve_instrument",
        exchange=name,
        account=str(account or ""),
        instrument=inst,
        data={"instrument": {**inst.to_dict(), **info}},
    )


def _market_price(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    requested = str(request.get("symbol") or "").strip().upper()
    row = _market_row(requested)
    if row is None:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=str(account or ""),
            code="INSTRUMENT_NOT_FOUND",
            message="MEXC spot symbol not found.",
        )
    symbol = str(row.get("symbol") or requested).upper()
    payload = _public_request("/api/v3/ticker/price", {"symbol": symbol})
    if not isinstance(payload, dict) or payload.get("price") is None:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=str(account or ""),
            code="MEXC_SPOT_PRICE_FAILED",
            message=sanitize_error_message(str(payload)),
        )
    price = str(payload.get("price"))
    return make_success(
        operation="market_price",
        exchange=name,
        account=str(account or ""),
        market_price=CanonicalMarketPrice(
            requested_symbol=requested,
            market=symbol,
            mark_price=price,
            price=price,
            last_external_price=price,
        ),
        data={"symbol": symbol, "price": price},
    )


def _unsupported(operation: str, account: str) -> CanonicalResponse:
    return make_failure(
        operation=operation,
        exchange=name,
        account=str(account or ""),
        code="NOT_IMPLEMENTED",
        message="MEXC Spot does not enable this write operation.",
    )


def _ladder_not_enabled(account: str) -> CanonicalResponse:
    return make_failure(
        operation="ladder",
        exchange=name,
        account=str(account or ""),
        code="NOT_IMPLEMENTED",
        # Phase 1 source-only: planned batch submission architecture uses
        # `POST /api/v3/batchOrders` (max 20 orders per call, rate-limit
        # bucket shared with /api/v3/order at 12 req/s) and fallbacks to
        # per-order `POST /api/v3/order` for >20-child ladders, with
        # idempotent `newClientOrderId` values, bounded batches, and explicit
        # accepted/failed tracking. The actual submit path is not wired yet.
        message="Live ladder submission is not enabled yet.",
    )


def _decimal_step(value: Any) -> Decimal:
    text = str(value or "").strip()
    if not text:
        return Decimal("0")
    try:
        inc = Decimal(text)
    except Exception:  # noqa: BLE001
        return Decimal("0")
    return inc if inc > 0 else Decimal("0")


def _places_increment(value: Any) -> Decimal:
    text = str(value or "").strip()
    if not text:
        return Decimal("0")
    try:
        places = int(text)
    except Exception:  # noqa: BLE001
        return Decimal("0")
    if places < 0:
        return Decimal("0")
    return Decimal("1").scaleb(-places)


def _format_step(value: Decimal) -> str:
    if value <= 0:
        return ""
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _quantize_down(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        return value
    steps = (value / increment).to_integral_value(rounding=ROUND_DOWN)
    return steps * increment


def _size_increment(info: Mapping[str, Any]) -> Decimal:
    inc = _decimal_step(info.get("size_step"))
    if inc > 0:
        return inc
    inc = _decimal_step(info.get("step_size"))
    if inc > 0:
        return inc
    inc = _decimal_step(info.get("baseSizePrecision"))
    if inc > 0:
        return inc
    return _places_increment(info.get("baseAssetPrecision"))


def _price_increment(info: Mapping[str, Any]) -> Decimal:
    inc = _decimal_step(info.get("price_tick"))
    if inc > 0:
        return inc
    inc = _decimal_step(info.get("tick_size"))
    if inc > 0:
        return inc
    for key in ("quotePrecision", "quoteAssetPrecision"):
        inc = _places_increment(info.get(key))
        if inc > 0:
            return inc
    return Decimal("0")


def _is_timeout_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    reason = str(getattr(exc, "reason", "")).lower()
    return "timed out" in text or "timeout" in text or "timed out" in reason or "timeout" in reason


def _new_order(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return _missing_account("new_order", account)
    side = str(request.get("side") or "").strip().upper()
    order_type = str(request.get("order_type") or request.get("type") or "LIMIT").strip().upper()
    symbol = str(request.get("symbol") or "").strip().upper().replace("/", "")
    if side not in {"BUY", "SELL"}:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_SIDE",
            message="Side must be BUY or SELL.",
        )
    if order_type != "LIMIT":
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="LIMIT_ONLY",
            message="MEXC Spot /tradespot accepts LIMIT orders only.",
        )
    qty = _to_decimal(request.get("quantity") or request.get("qty") or request.get("volume"))
    price = _to_decimal(request.get("price") or request.get("limit_price"))
    if qty <= 0 or price <= 0:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_ORDER",
            message="Quantity and limit price must be positive.",
        )
    row = _market_row(symbol)
    if row is None:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INSTRUMENT_NOT_FOUND",
            message="MEXC spot symbol not found.",
        )
    info = _instrument_from_market(row)
    order_types = {str(x).upper() for x in info.get("orderTypes") or []}
    if order_types and "LIMIT" not in order_types:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="LIMIT_NOT_ALLOWED",
            message="LIMIT is not an allowed order type for this symbol.",
        )
    if not info.get("isSpotTradingAllowed") or not info.get("api_eligible"):
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="SYMBOL_NOT_TRADABLE",
            message="Symbol is not API-enabled for spot trading.",
        )
    try:
        self_set, _self_err = _self_symbols(credentials)
    except Exception:  # noqa: BLE001
        self_set = None
    if self_set is not None and str(info["symbol"]) not in self_set:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="SYMBOL_NOT_API_ENABLED",
            message="Symbol is not API-enabled for this key.",
        )
    qty = _quantize_down(qty, _size_increment(info))
    price = _quantize_down(price, _price_increment(info))
    if qty <= 0 or price <= 0:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_ORDER",
            message="Normalized quantity or price is not positive.",
        )
    qty_text = _format_decimal(qty)
    price_text = _format_decimal(price)
    client_id = str(request.get("client_order_id") or request.get("newClientOrderId") or "").strip()
    params: Dict[str, Any] = {
        "symbol": str(info["symbol"]),
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": qty_text,
        "price": price_text,
    }
    if client_id:
        params["newClientOrderId"] = client_id[:32]
    try:
        payload = _signed_request(credentials, "POST", "/api/v3/order", params)
        err = _response_error(payload, "order rejected")
        if err:
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="EXCHANGE_REJECTED",
                message=_redact(sanitize_error_message(err), credentials),
                exchange_reason=_redact(sanitize_error_message(err), credentials),
            )
        if not isinstance(payload, dict) or payload.get("orderId") is None:
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="ORDER_STATUS_UNKNOWN",
                message="MEXC did not return an order id; status is unknown. Not retried.",
            )
        order_id = payload.get("orderId")
        status = str(payload.get("status") or "NEW")
        return make_success(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            order=CanonicalOrderResult(
                symbol=str(info["symbol"]),
                side=side,
                order_type="LIMIT",
                requested_volume=qty_text,
                requested_price=price_text,
                submitted_volume=str(payload.get("origQty") or qty_text),
                submitted_price=str(payload.get("price") or price_text),
                verified=True,
                status=status,
                exchange_order_id=order_id,
                client_order_id=payload.get("clientOrderId") or (client_id or None),
            ),
            data={
                "source": "mexc_spot_order",
                "orderId": order_id,
                "status": status,
                "symbol": str(info["symbol"]),
                "side": side,
                "quantity": qty_text,
                "price": price_text,
            },
        )
    except urllib.error.URLError as exc:
        if _is_timeout_error(exc):
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="ORDER_STATUS_UNKNOWN",
                message="MEXC request timed out after transmission. Order status is unknown; not retried.",
            )
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="MEXC_SPOT_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )
    except Exception as exc:  # noqa: BLE001
        if _is_timeout_error(exc):
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="ORDER_STATUS_UNKNOWN",
                message="MEXC request timed out after transmission. Order status is unknown; not retried.",
            )
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="MEXC_SPOT_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _open_order_rows(credentials: Mapping[str, str]) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], bool]:
    try:
        payload = _signed_request(credentials, "GET", "/api/v3/openOrders")
        err = _response_error(payload, "openOrders failed") if isinstance(payload, dict) else None
        if err:
            return None, err, False
        rows = payload if isinstance(payload, list) else payload.get("orders", []) if isinstance(payload, dict) else []
        parsed = [_parse_order(row) for row in rows if isinstance(row, Mapping)]
        return parsed, None, False
    except urllib.error.URLError as exc:
        if _is_timeout_error(exc):
            return None, "MEXC openOrders timed out.", True
        return None, _redact(sanitize_error_message(str(exc)), credentials), False
    except Exception as exc:  # noqa: BLE001
        if _is_timeout_error(exc):
            return None, "MEXC openOrders timed out.", True
        return None, _redact(sanitize_error_message(str(exc)), credentials), False


def _cancel_orders(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return _missing_account("cancel_orders", account)
    raw_ids = request.get("order_ids") if request.get("order_ids") is not None else request.get("orderIds")
    if not isinstance(raw_ids, list) or not raw_ids:
        return make_failure(
            operation="cancel_orders",
            exchange=name,
            account=credentials["account"],
            code="MISSING_ORDER_IDS",
            message="cancel_orders requires an explicit list of MEXC order IDs.",
        )
    side = str(request.get("side") or "").strip().upper()
    symbol = str(request.get("symbol") or "").strip().upper().replace("/", "")
    if side not in {"BUY", "SELL"}:
        return make_failure(
            operation="cancel_orders",
            exchange=name,
            account=credentials["account"],
            code="INVALID_SIDE",
            message="Side must be BUY or SELL.",
        )
    if not symbol:
        return make_failure(
            operation="cancel_orders",
            exchange=name,
            account=credentials["account"],
            code="MISSING_SYMBOL",
            message="cancel_orders requires a symbol.",
        )
    order_ids = [str(oid).strip() for oid in raw_ids if str(oid).strip()]
    if not order_ids:
        return make_failure(
            operation="cancel_orders",
            exchange=name,
            account=credentials["account"],
            code="MISSING_ORDER_IDS",
            message="cancel_orders requires an explicit list of MEXC order IDs.",
        )
    timed_out = False
    for oid in order_ids:
        try:
            _signed_request(
                credentials,
                "DELETE",
                "/api/v3/order",
                {"symbol": symbol, "orderId": oid},
            )
        except urllib.error.URLError as exc:
            if _is_timeout_error(exc):
                timed_out = True
                continue
            return make_failure(
                operation="cancel_orders",
                exchange=name,
                account=credentials["account"],
                code="MEXC_SPOT_ERROR",
                message=_redact(sanitize_error_message(str(exc)), credentials),
            )
        except Exception as exc:  # noqa: BLE001
            if _is_timeout_error(exc):
                timed_out = True
                continue
            return make_failure(
                operation="cancel_orders",
                exchange=name,
                account=credentials["account"],
                code="MEXC_SPOT_ERROR",
                message=_redact(sanitize_error_message(str(exc)), credentials),
            )
    parsed, list_error, list_timeout = _open_order_rows(credentials)
    if parsed is None:
        return make_failure(
            operation="cancel_orders",
            exchange=name,
            account=credentials["account"],
            code="CANCEL_STATUS_UNKNOWN",
            message=(list_error or "MEXC open-order status is unknown after cancel.") + " Not retried.",
        )
    matching = [
        row
        for row in parsed
        if str(row.get("symbol") or "").upper().replace("/", "") == symbol
        and str(row.get("side") or "").upper() == side
        and str(row.get("status") or "").upper() not in {"FILLED", "CANCELED", "CANCELLED"}
    ]
    remaining_ids = {str(row.get("order_id") or "") for row in matching}
    requested_set = set(order_ids)
    still_open_requested = requested_set & remaining_ids
    cancelled = len(requested_set) - len(still_open_requested)
    remaining = len(matching)
    verified = len(still_open_requested) == 0
    data = {
        "source": "mexc_spot_cancel",
        "symbol": symbol,
        "side": side,
        "order_ids": order_ids,
        "requested": len(order_ids),
        "cancelled": cancelled,
        "remaining": remaining,
        "verified": verified,
    }
    cancel_group = CanonicalCancelGroupResult(
        symbol=symbol,
        side=side,
        targeted_order_count=len(order_ids),
        cancelled_order_count=cancelled,
        confirmed_absent_count=cancelled,
        remaining_target_count=len(still_open_requested),
        verified=verified,
        partial=cancelled > 0 and not verified,
        status="unknown" if timed_out or list_timeout else ("success" if verified else "partial"),
        requested_cancel_count=len(order_ids),
        verified_cancel_count=cancelled,
    )
    if timed_out or list_timeout:
        return make_failure(
            operation="cancel_orders",
            exchange=name,
            account=credentials["account"],
            code="CANCEL_STATUS_UNKNOWN",
            message="MEXC cancel request timed out after transmission. Order status is unknown; not retried.",
            cancel_group=cancel_group,
        )
    return make_success(
        operation="cancel_orders",
        exchange=name,
        account=credentials["account"],
        cancel_group=cancel_group,
        data=data,
    )


def execute(request: Mapping[str, Any]) -> CanonicalResponse:
    op = str(request.get("operation") or "").strip()
    account = str(request.get("account") or "").strip()
    if op == "balance":
        return _balance(account)
    if op in {"orders", "open_orders", "positions_orders"}:
        return _orders(account)
    if op == "list_instruments":
        return _list_instruments(account, request)
    if op == "resolve_instrument":
        return _resolve_instrument(account, request)
    if op == "market_price":
        return _market_price(account, request)
    if op == "new_order":
        return _new_order(account, request)
    if op == "cancel_orders":
        return _cancel_orders(account, request)
    if op == "ladder":
        return _ladder_not_enabled(account)
    if op in {"cancel_order_group", "cancel_order"}:
        return _unsupported(op, account)
    return make_failure(
        operation=op,
        exchange=name,
        account=account,
        code="NOT_IMPLEMENTED",
        message=f"Operation '{op}' is not implemented by MEXC Spot.",
    )


__all__ = ["name", "list_accounts", "capabilities", "execute"]
