"""MEXC Spot read-only agent for /tradespot.

Uses the existing MEXC credential convention from ``x_mexc_agent.py``:
``MEXC_<ALIAS>_ACCESSKEY`` + ``MEXC_<ALIAS>_SECRETKEY`` (plus common
API-key/secret aliases). This agent is strictly separate from the futures
``x_mexc_agent.py`` used by /trade.

Phase 1 is READ-ONLY:
  - balance
  - orders / open_orders
  - list_instruments / resolve_instrument / market_price

No write operation is advertised or executed here.
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
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..canonical import (
    CanonicalBalance,
    CanonicalInstrument,
    CanonicalMarketPrice,
    CanonicalOrderGroup,
    CanonicalResponse,
    make_failure,
    make_success,
    normalize_balance,
    sanitize_error_message,
)

logger = logging.getLogger(__name__)

name = "mexc"

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
    return [
        "balance",
        "orders",
        "open_orders",
        "list_instruments",
        "resolve_instrument",
        "market_price",
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
    if isinstance(payload, dict) and ("code" in payload or "msg" in payload or "message" in payload):
        # MEXC success payloads for these endpoints carry domain fields like balances/symbols,
        # not code/msg. If code/msg is present, treat it as an error body.
        if "balances" not in payload and "symbols" not in payload:
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
    step_size = ""
    tick_size = ""
    min_notional = ""
    for f in filters:
        if not isinstance(f, Mapping):
            continue
        ftype = str(f.get("filterType") or "")
        if ftype in {"LOT_SIZE", "MARKET_LOT_SIZE"} and not step_size:
            step_size = str(f.get("stepSize") or "")
            min_qty = str(f.get("minQty") or "")
        elif ftype == "PRICE_FILTER":
            tick_size = str(f.get("tickSize") or "")
        elif ftype in {"MIN_NOTIONAL", "NOTIONAL"}:
            min_notional = str(f.get("minNotional") or f.get("minNotionalForMarket") or "")
    order_types = [str(x) for x in row.get("orderTypes") or []]
    is_allowed = bool(row.get("isSpotTradingAllowed"))
    status = str(row.get("status") or "")
    return {
        "symbol": symbol,
        "baseAsset": str(row.get("baseAsset") or ""),
        "quoteAsset": str(row.get("quoteAsset") or ""),
        "display_name": f"{row.get('baseAsset')}/{row.get('quoteAsset')}",
        "status": status,
        "isSpotTradingAllowed": is_allowed,
        "orderTypes": order_types,
        "baseAssetPrecision": row.get("baseAssetPrecision"),
        "quoteAssetPrecision": row.get("quoteAssetPrecision"),
        "quotePrecision": row.get("quotePrecision"),
        "tick_size": tick_size,
        "step_size": step_size,
        "min_qty": min_qty,
        "min_notional": min_notional,
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
                price_increment=str(item.get("tick_size") or ""),
                size_increment=str(item.get("step_size") or ""),
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
    inst = CanonicalInstrument(
        requested_symbol=requested,
        symbol=str(info["symbol"]),
        display_name=str(info.get("display_name") or info["symbol"]),
        price_increment=str(info.get("tick_size") or ""),
        size_increment=str(info.get("step_size") or ""),
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
        message="MEXC Spot phase 1 is read-only; writes are disabled.",
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
    if op in {"new_order", "ladder", "cancel_orders", "cancel_order_group", "cancel_order"}:
        return _unsupported(op, account)
    return make_failure(
        operation=op,
        exchange=name,
        account=account,
        code="NOT_IMPLEMENTED",
        message=f"Operation '{op}' is not implemented by MEXC Spot.",
    )


__all__ = ["name", "list_accounts", "capabilities", "execute"]
