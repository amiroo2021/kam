"""QFEX exchange agent.

Credentials (``.env`` / environment):
  ``QFEX_<ALIAS>_PUBLIC_KEY`` + ``QFEX_<ALIAS>_SECRET_KEY``
  Optional: ``QFEX_<ALIAS>_BASE_URL`` (default https://api.qfex.com)
            ``QFEX_<ALIAS>_ACCOUNT_ID`` for x-qfex-requested-account-id

Current scope:
  - balance via GET /user/subaccounts/balance
  - positions_orders via GET /user/positions + Trade WebSocket get_user_orders
  - new_order / cancel_order_group via Trade WebSocket

QFEX HMAC auth per docs:
  signature = HMAC-SHA256(secret, f"{nonce}:{unix_ts}").hexdigest()
  headers: x-qfex-public-key, x-qfex-hmac-signature,
           x-qfex-nonce, x-qfex-timestamp
"""

from __future__ import annotations

import hmac
import json
import logging
import math
import os
import re
import secrets
import uuid
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

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

logger = logging.getLogger(__name__)

name = "qfex"
DEFAULT_API_BASE = "https://api.qfex.com"
API_TIMEOUT_SECONDS = 20
DEFAULT_UNIT = "USDT"

_PATH_SUBACCOUNT_BALANCE = "/user/subaccounts/balance"
_PATH_USER_POSITIONS = "/user/positions"
DEFAULT_WS_URL = "wss://trade.qfex.com/"

_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PUBLIC_KEY_ALIASES = ("PUBLIC_KEY", "PUBLICKEY", "API_KEY", "APIKEY", "KEY")
_SECRET_KEY_ALIASES = ("SECRET_KEY", "SECRETKEY", "API_SECRET", "APISECRET", "SECRET")
_BASE_URL_ALIASES = ("BASE_URL", "API_BASE", "URL")
_ACCOUNT_ID_ALIASES = ("ACCOUNT_ID", "ACCOUNTID", "SUBACCOUNT_ID", "SUBACCOUNTID")


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


def _combined_qfex_env() -> Dict[str, Tuple[str, str, str]]:
    out: Dict[str, Tuple[str, str, str]] = {}
    for key, value in os.environ.items():
        if key.upper().startswith("QFEX_"):
            out.setdefault(key.upper(), (key, str(value), "env"))
    for key, value in _load_dotenv_values(_hermes_home() / ".env").items():
        if key.upper().startswith("QFEX_"):
            out.setdefault(key.upper(), (key, str(value), "dotenv"))
    return out


def _parse_alias_and_suffix(upper_key: str) -> Optional[Tuple[str, str]]:
    if not upper_key.startswith("QFEX_"):
        return None
    rest = upper_key[len("QFEX_") :]
    suffixes = sorted(
        set(_PUBLIC_KEY_ALIASES + _SECRET_KEY_ALIASES + _BASE_URL_ALIASES + _ACCOUNT_ID_ALIASES),
        key=len,
        reverse=True,
    )
    for suffix in suffixes:
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
    for upper_key, (_actual, value, _src) in _combined_qfex_env().items():
        parsed = _parse_alias_and_suffix(upper_key)
        if parsed is None:
            continue
        alias_upper, suffix = parsed
        alias = alias_upper.lower()
        slot = buckets.setdefault(alias, {})
        val = value.strip()
        if suffix in _PUBLIC_KEY_ALIASES:
            slot.setdefault("public_key", val)
        elif suffix in _SECRET_KEY_ALIASES:
            slot.setdefault("secret_key", val)
        elif suffix in _BASE_URL_ALIASES:
            slot.setdefault("base_url", val.rstrip("/"))
        elif suffix in _ACCOUNT_ID_ALIASES:
            slot.setdefault("account_id", val)
    return {
        alias: fields
        for alias, fields in buckets.items()
        if fields.get("public_key") and fields.get("secret_key")
    }


def list_accounts() -> list[str]:
    return sorted(_discover_credential_map().keys())


def capabilities() -> list[str]:
    return ["balance", "positions_orders", "positions_management", "new_order", "cancel_order_group", "resolve_instrument", "market_price", "ladder", "close_position", "set_tp", "set_sl"]


def _lookup_credentials(account: str) -> Optional[Dict[str, str]]:
    alias = str(account or "").strip().lower()
    if not alias:
        return None
    fields = _discover_credential_map().get(alias)
    if not fields:
        return None
    out = {
        "account": alias,
        "public_key": fields["public_key"],
        "secret_key": fields["secret_key"],
        "base_url": fields.get("base_url") or DEFAULT_API_BASE,
    }
    if fields.get("account_id"):
        out["account_id"] = fields["account_id"]
    return out


def _redact(text: Any, credentials: Optional[Mapping[str, str]] = None) -> str:
    rendered = str(text or "")
    if credentials:
        for key in ("public_key", "secret_key", "account_id"):
            value = str(credentials.get(key) or "").strip()
            if len(value) >= 6:
                rendered = rendered.replace(value, "***")
    rendered = re.sub(r"(?i)(x-qfex-public-key\s*[:=]\s*)([^\s,;}\"']+)", r"\1***", rendered)
    rendered = re.sub(r"(?i)(x-qfex-hmac-signature\s*[:=]\s*)([^\s,;}\"']+)", r"\1***", rendered)
    return sanitize_error_message(rendered)


def _auth_headers(credentials: Mapping[str, str]) -> Dict[str, str]:
    nonce = secrets.token_hex(16)
    unix_ts = str(int(time.time()))
    signed = f"{nonce}:{unix_ts}".encode("utf-8")
    secret_key = str(credentials.get("secret_key") or "").encode("utf-8")
    signature = hmac.new(secret_key, signed, "sha256").hexdigest()
    headers = {
        "Accept": "application/json",
        "User-Agent": "Hermes-KAM-QFEXAgent/1.0",
        "x-qfex-public-key": str(credentials.get("public_key") or ""),
        "x-qfex-hmac-signature": signature,
        "x-qfex-nonce": nonce,
        "x-qfex-timestamp": unix_ts,
    }
    account_id = str(credentials.get("account_id") or "").strip()
    if account_id:
        headers["x-qfex-requested-account-id"] = account_id
    return headers



def _public_request(path: str, query: str = "") -> Dict[str, Any]:
    query = query.lstrip("?")
    url = f"{DEFAULT_API_BASE}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Hermes-KAM-QFEXAgent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:  # noqa: S310 HTTPS API
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(f"HTTP {exc.code}: {raw or exc}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason or exc)) from exc

def _signed_request(
    credentials: Mapping[str, str], method: str, path: str, query: str = ""
) -> Dict[str, Any]:
    base = str(credentials.get("base_url") or DEFAULT_API_BASE).rstrip("/")
    method_u = method.upper().strip()
    query = query.lstrip("?")
    url = f"{base}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, method=method_u, headers=_auth_headers(credentials))
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:  # noqa: S310 HTTPS API
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"detail": raw or str(exc)}
        detail = payload.get("detail") or payload.get("title") or payload.get("message") or str(exc)
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason or exc)) from exc


def _decimal_or_zero(value: Any) -> Decimal:
    try:
        if value is None or value == "":
            return Decimal("0")
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _format_decimal(value: Any) -> str:
    dec = _decimal_or_zero(value)
    if dec == dec.to_integral_value():
        return str(dec.quantize(Decimal("1")))
    return format(dec.normalize(), "f")


def _native_symbol(symbol: str) -> str:
    raw = str(symbol or "").strip().upper().replace("/", "-").replace("_", "-")
    if not raw:
        return ""
    if "-" not in raw:
        return f"{raw}-USD"
    return raw


def _display_symbol(symbol: str) -> str:
    raw = str(symbol or "").strip().upper()
    for suffix in ("-USD", "-USDC", "-USDT"):
        if raw.endswith(suffix) and len(raw) > len(suffix):
            return raw[: -len(suffix)]
    return raw


def _qfex_side(side: str) -> str:
    s = str(side or "").strip().lower()
    if s in {"buy", "long", "bid"}:
        return "BUY"
    if s in {"sell", "short", "ask"}:
        return "SELL"
    raise ValueError("INVALID_SIDE")


def _canonical_side(side: str) -> str:
    s = str(side or "").strip().upper()
    if s == "BUY":
        return "buy"
    if s == "SELL":
        return "sell"
    return str(side or "").strip().lower()


def _is_open_order(row: Mapping[str, Any]) -> bool:
    status = str(row.get("status") or row.get("terminal_status") or "").strip().upper()
    if status in {"CANCELLED", "FILLED", "EXPIRED", "REJECTED", "NOT_FOUND", "NO_SUCH_ORDER", "IOC_CANCELLED"}:
        return False
    remaining = _decimal_or_zero(row.get("quantity_remaining") or row.get("remaining_qty") or row.get("quantity"))
    return remaining > 0


def _extract_order_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    container = payload.get("all_orders_response") if isinstance(payload, Mapping) else None
    if isinstance(container, Mapping):
        rows = container.get("orders") or []
    else:
        rows = payload.get("orders") if isinstance(payload, Mapping) else []
    return [dict(r) for r in rows if isinstance(r, Mapping)]


def _normalize_positions(rows: Any) -> list[CanonicalPosition]:
    out: list[CanonicalPosition] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        qty = _decimal_or_zero(row.get("position") or row.get("size") or row.get("quantity"))
        if qty == 0:
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        side = "long" if qty > 0 else "short"
        pnl = _decimal_or_zero(row.get("unrealised_pnl")) + _decimal_or_zero(row.get("realised_pnl"))
        out.append(
            CanonicalPosition(
                symbol=_display_symbol(symbol),
                side=side,
                size=_format_decimal(abs(qty)),
                entry_price=_format_decimal(row.get("average_price") or row.get("entry_price") or "0"),
                pnl=_format_decimal(pnl),
                exchange_instrument=symbol or None,
            )
        )
    return out


def _group_open_orders(rows: list[Mapping[str, Any]]) -> tuple[int, list[CanonicalOrderGroup]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    total = 0
    for row in rows:
        if not _is_open_order(row):
            continue
        native = str(row.get("symbol") or "").strip().upper()
        symbol = _display_symbol(native)
        side = _canonical_side(str(row.get("side") or ""))
        if not symbol or side not in {"buy", "sell"}:
            continue
        size = abs(_decimal_or_zero(row.get("quantity_remaining") or row.get("quantity")))
        price = _decimal_or_zero(row.get("price"))
        if size <= 0:
            continue
        total += 1
        key = (symbol, side)
        slot = groups.setdefault(
            key,
            {"symbol": symbol, "side": side, "order_count": 0, "total_size": Decimal("0"), "notional": Decimal("0"), "min_price": None, "max_price": None},
        )
        slot["order_count"] += 1
        slot["total_size"] += size
        if price > 0:
            slot["notional"] += size * price
            slot["min_price"] = price if slot["min_price"] is None else min(slot["min_price"], price)
            slot["max_price"] = price if slot["max_price"] is None else max(slot["max_price"], price)
    out: list[CanonicalOrderGroup] = []
    for slot in groups.values():
        total_size = slot["total_size"]
        vwap = _format_decimal(slot["notional"] / total_size) if total_size > 0 and slot["notional"] > 0 else ""
        out.append(
            CanonicalOrderGroup(
                symbol=slot["symbol"],
                side=slot["side"],
                order_count=int(slot["order_count"]),
                total_size=_format_decimal(total_size),
                vwap=vwap,
                min_price=_format_decimal(slot["min_price"]) if slot["min_price"] is not None else "",
                max_price=_format_decimal(slot["max_price"]) if slot["max_price"] is not None else "",
            )
        )
    out.sort(key=lambda g: (g.symbol, g.side))
    return total, out


def _ws_auth_payload(credentials: Mapping[str, str]) -> dict[str, Any]:
    nonce = secrets.token_hex(16)
    unix_ts = int(time.time())
    signature = hmac.new(
        str(credentials.get("secret_key") or "").encode("utf-8"),
        f"{nonce}:{unix_ts}".encode("utf-8"),
        "sha256",
    ).hexdigest()
    params: dict[str, Any] = {
        "hmac": {
            "public_key": str(credentials.get("public_key") or ""),
            "nonce": nonce,
            "unix_ts": unix_ts,
            "signature": signature,
        }
    }
    account_id = str(credentials.get("account_id") or "").strip()
    if account_id:
        params["account_id"] = account_id
    return {"type": "auth", "params": params}


def _ws_command(credentials: Mapping[str, str], command: Mapping[str, Any], expect: Optional[set[str]] = None) -> Dict[str, Any]:
    """Send one QFEX Trade WebSocket command after HMAC auth."""
    import websocket  # type: ignore

    expected = expect or {"order_response", "all_orders_response", "ack", "position_response", "balance_response"}
    ws_url = str(credentials.get("ws_url") or DEFAULT_WS_URL)
    public_key = str(credentials.get("public_key") or "")
    if public_key and "api_key=" not in ws_url:
        sep = "&" if "?" in ws_url else "?"
        ws_url = f"{ws_url}{sep}{urllib.parse.urlencode({'api_key': public_key})}"
    ws = websocket.create_connection(ws_url, timeout=API_TIMEOUT_SECONDS)
    try:
        try:
            ws.settimeout(min(5, API_TIMEOUT_SECONDS))
        except Exception:
            pass
        ws.send(json.dumps(_ws_auth_payload(credentials), separators=(",", ":")))
        deadline = time.time() + API_TIMEOUT_SECONDS
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if "err" in msg:
                err = msg.get("err") or {}
                raise RuntimeError(str(err.get("message") or err.get("error_code") or err))
            if "authenticated_response" in msg or "ack" in msg or msg.get("authenticated") is True:
                break
        ws.send(json.dumps(dict(command), separators=(",", ":")))
        deadline = time.time() + API_TIMEOUT_SECONDS
        last: Dict[str, Any] = {}
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if isinstance(msg, dict):
                last = msg
            if "err" in msg:
                err = msg.get("err") or {}
                raise RuntimeError(str(err.get("message") or err.get("error_code") or err))
            if any(k in msg for k in expected):
                return msg
        raise RuntimeError(f"Timed out waiting for QFEX websocket response; last={last}")
    finally:
        ws.close()



def _data_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("data") if isinstance(payload, Mapping) else []
    return [dict(r) for r in rows if isinstance(r, Mapping)]


def _find_refdata_symbol(requested_symbol: str) -> Optional[dict[str, Any]]:
    native = _native_symbol(requested_symbol)
    base = _display_symbol(native)
    # Prefer full refdata for increments/limits. QFEX's ticker filter can return
    # no rows for symbols that are present in /md/contracts, so fall back below.
    payload = _public_request("/refdata")
    rows = _data_rows(payload)
    for row in rows:
        sym = str(row.get("symbol") or "").upper()
        if sym == native or str(row.get("base_asset") or "").upper() == base:
            return row
    contracts = _data_rows(_public_request("/md/contracts"))
    for row in contracts:
        ticker = str(row.get("ticker_id") or "").upper()
        if ticker == native or str(row.get("base_currency") or "").upper() == base:
            return {
                "symbol": ticker,
                "base_asset": row.get("base_currency"),
                "quote_asset": row.get("target_currency") or row.get("quote_currency"),
                "tick_size": row.get("tick_size") or "",
                "lot_size": row.get("lot_size") or "",
                "min_quantity": row.get("min_quantity") or "",
            }
    return None


def _resolve_instrument(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    requested = str(request.get("symbol") or request.get("query") or "").strip()
    try:
        row = _find_refdata_symbol(requested)
        if not row:
            return make_failure(operation="resolve_instrument", exchange=name, account=account, code="INSTRUMENT_NOT_FOUND", message=f"QFEX instrument not found: {requested}")
        native = str(row.get("symbol") or _native_symbol(requested)).upper()
        instrument = CanonicalInstrument(
            requested_symbol=requested,
            symbol=native,
            display_name=native,
            price_increment=str(row.get("tick_size") or ""),
            size_increment=str(row.get("lot_size") or ""),
            minimum_size=str(row.get("min_quantity") or ""),
        )
        return make_success(operation="resolve_instrument", exchange=name, account=str(account or ""), instrument=instrument)
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="resolve_instrument", exchange=name, account=str(account or ""), code="QFEX_ERROR", message=sanitize_error_message(str(exc)))


def _market_price(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    requested = str(request.get("symbol") or request.get("market") or "").strip()
    native = _native_symbol(requested)
    try:
        payload = _public_request("/md/contracts")
        rows = _data_rows(payload)
        match = None
        for row in rows:
            ticker = str(row.get("ticker_id") or "").upper()
            if ticker == native or str(row.get("base_currency") or "").upper() == _display_symbol(native):
                match = row
                break
        if not match:
            return make_failure(operation="market_price", exchange=name, account=account, code="INSTRUMENT_NOT_FOUND", message=f"QFEX price not found: {requested}")
        market = str(match.get("ticker_id") or native).upper()
        price = str(match.get("last_price") or match.get("index_price") or "")
        mp = CanonicalMarketPrice(
            requested_symbol=requested,
            market=market,
            mark_price=str(match.get("index_price") or "") or None,
            price=price or None,
        )
        return make_success(operation="market_price", exchange=name, account=str(account or ""), market_price=mp)
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="market_price", exchange=name, account=str(account or ""), code="QFEX_ERROR", message=sanitize_error_message(str(exc)))


def _balance(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set QFEX_<ACCOUNT>_PUBLIC_KEY and QFEX_<ACCOUNT>_SECRET_KEY.",
        )
    try:
        payload = _signed_request(credentials, "GET", _PATH_SUBACCOUNT_BALANCE)
        rows = payload.get("accounts") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            rows = []
        total_available = Decimal("0")
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            total_available += _decimal_or_zero(row.get("available_balance"))
        balance = normalize_balance(total_available, DEFAULT_UNIT)
        portfolio = CanonicalPortfolioSummary(
            account_value=balance.value,
            withdrawable=balance.value,
            margin_used="0.00",
            total_position_value="0.00",
            unit=DEFAULT_UNIT,
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
            code="QFEX_ERROR",
            message=_redact(exc, credentials),
        )



def _positions_orders(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set QFEX_<ACCOUNT>_PUBLIC_KEY and QFEX_<ACCOUNT>_SECRET_KEY.",
        )
    try:
        pos_payload = _signed_request(credentials, "GET", _PATH_USER_POSITIONS)
        positions = _normalize_positions(pos_payload.get("positions") if isinstance(pos_payload, Mapping) else [])
        orders_payload = _ws_command(
            credentials,
            {"type": "get_user_orders", "params": {"limit": 500, "offset": 0}},
            expect={"all_orders_response"},
        )
        order_rows = _extract_order_rows(orders_payload)
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
            code="QFEX_ERROR",
            message=_redact(exc, credentials),
        )


def _new_order(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="new_order", exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Set QFEX_<ACCOUNT>_PUBLIC_KEY and QFEX_<ACCOUNT>_SECRET_KEY.")
    requested_symbol = str(request.get("symbol") or "").strip()
    side_in = str(request.get("side") or "").strip().lower()
    volume_text = str(request.get("volume") or request.get("size") or request.get("quantity") or "").strip()
    price_text = str(request.get("price") or "").strip()
    order_type = str(request.get("order_type") or request.get("type") or "limit").strip().lower()
    try:
        native = _native_symbol(requested_symbol)
        side_q = _qfex_side(side_in)
        volume = _decimal_or_zero(volume_text)
        price = _decimal_or_zero(price_text)
    except Exception:
        return make_failure(operation="new_order", exchange=name, account=credentials["account"], code="INVALID_REQUEST", message="Symbol, buy/sell side, volume and price are required.")
    if order_type not in {"limit", ""}:
        return make_failure(operation="new_order", exchange=name, account=credentials["account"], code="UNSUPPORTED_ORDER_TYPE", message="QFEX agent currently supports limit orders only.")
    if not native or side_q not in {"BUY", "SELL"} or volume <= 0 or price <= 0:
        return make_failure(operation="new_order", exchange=name, account=credentials["account"], code="INVALID_REQUEST", message="Symbol, side, volume and price must be valid positive values.")
    client_order_id = str(request.get("client_order_id") or request.get("clientOrderId") or "").strip() or uuid.uuid4().hex[:32]
    command = {
        "type": "add_order",
        "params": {
            "symbol": native,
            "side": side_q,
            "order_type": "LIMIT",
            "order_time_in_force": "GTC",
            "quantity": float(volume),
            "price": float(price),
            "client_order_id": client_order_id,
        },
    }
    def _result_from_row(row: Mapping[str, Any], *, accepted: bool) -> CanonicalOrderResult:
        status_native = str(row.get("status") or "").upper()
        return CanonicalOrderResult(
            symbol=_display_symbol(native),
            side="buy" if side_q == "BUY" else "sell",
            order_type="limit",
            requested_volume=_format_decimal(volume),
            requested_price=_format_decimal(price),
            submitted_volume=_format_decimal(row.get("quantity") or volume),
            submitted_price=_format_decimal(row.get("price") or price),
            verified=accepted,
            status="success" if accepted else (status_native.lower() or "submitted"),
            exchange_order_id=row.get("order_id"),
            client_order_id=row.get("client_order_id") or client_order_id,
        )

    try:
        payload = _ws_command(credentials, command, expect={"order_response"})
        row = payload.get("order_response") if isinstance(payload, Mapping) else {}
        if not isinstance(row, Mapping):
            row = {}
        status_native = str(row.get("status") or "").upper()
        accepted = status_native in {"ACK", "MODIFIED", "IOC_PARTIALLY_FILLED"} or bool(row.get("order_id"))
        result = _result_from_row(row, accepted=accepted)
        if accepted:
            return make_success(operation="new_order", exchange=name, account=credentials["account"], order=result)
        return make_failure(operation="new_order", exchange=name, account=credentials["account"], code="ORDER_FAILED", message=status_native or "QFEX order rejected.", order=result)
    except Exception as exc:  # noqa: BLE001
        try:
            orders_payload = _ws_command(credentials, {"type": "get_user_orders", "params": {"limit": 500, "offset": 0, "symbol": native}}, expect={"all_orders_response"})
            for row in _extract_order_rows(orders_payload):
                if str(row.get("client_order_id") or "") == client_order_id and _is_open_order(row):
                    result = _result_from_row(row, accepted=True)
                    return make_success(operation="new_order", exchange=name, account=credentials["account"], order=result)
        except Exception:
            pass
        return make_failure(operation="new_order", exchange=name, account=credentials["account"], code="QFEX_ERROR", message=_redact(exc, credentials))


def _quantize_to_step(value: Decimal, step: Decimal, rounding=ROUND_HALF_UP) -> Decimal:
    if step <= 0:
        return value
    units = (value / step).to_integral_value(rounding=rounding)
    return units * step


def _find_position_row(credentials: Mapping[str, str], requested_symbol: str) -> Optional[dict[str, Any]]:
    native = _native_symbol(requested_symbol)
    payload = _signed_request(credentials, "GET", _PATH_USER_POSITIONS)
    rows = payload.get("positions") if isinstance(payload, Mapping) else []
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        qty = _decimal_or_zero(row.get("position") or row.get("size") or row.get("quantity"))
        if symbol == native and qty != 0:
            return dict(row)
    return None


def _position_action_unsupported(account: str, request: Mapping[str, Any], operation: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    canonical_account = credentials["account"] if credentials else account
    symbol = _display_symbol(_native_symbol(str(request.get("symbol") or "")))
    action = CanonicalPositionActionResult(
        operation=operation,
        symbol=symbol,
        verified=False,
        status="unsupported",
        message="QFEX TP/SL position-management writes are not enabled until QFEX stop-order creation semantics are confirmed.",
    )
    return make_failure(
        operation=operation,
        exchange=name,
        account=canonical_account,
        code="NOT_IMPLEMENTED",
        message=action.message or "QFEX operation is not implemented.",
        position_action=action,
    )


def _close_position(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="close_position", exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Set QFEX_<ACCOUNT>_PUBLIC_KEY and QFEX_<ACCOUNT>_SECRET_KEY.")
    requested_symbol = str(request.get("symbol") or "").strip()
    native = _native_symbol(requested_symbol)
    display = _display_symbol(native)
    try:
        before = _find_position_row(credentials, native)
        if before is None:
            action = CanonicalPositionActionResult(operation="close_position", symbol=display, verified=True, status="noop", current_size="0", message="No open position for symbol.")
            return make_success(operation="close_position", exchange=name, account=credentials["account"], position_action=action)
        before_qty = _decimal_or_zero(before.get("position") or before.get("size") or before.get("quantity"))
        side = "long" if before_qty > 0 else "short"
        client_order_id = str(request.get("client_order_id") or request.get("clientOrderId") or "").strip() or uuid.uuid4().hex[:32]
        command = {"type": "close_position", "params": {"symbol": native, "client_order_id": client_order_id}}
        try:
            _ws_command(credentials, command, expect={"position_response", "order_response", "ack"})
        except Exception:
            # QFEX write responses can time out after execution; verify below.
            pass
        after = _find_position_row(credentials, native)
        remaining = Decimal("0") if after is None else abs(_decimal_or_zero(after.get("position") or after.get("size") or after.get("quantity")))
        verified = remaining == 0
        action = CanonicalPositionActionResult(
            operation="close_position",
            symbol=display,
            verified=verified,
            status="success" if verified else "submitted",
            current_side=None if verified else side,
            current_size=_format_decimal(remaining),
            message=("QFEX position is flat." if verified else "QFEX close_position submitted.") + f" Client order id: {client_order_id}",
        )
        if verified:
            return make_success(operation="close_position", exchange=name, account=credentials["account"], position_action=action)
        return make_failure(operation="close_position", exchange=name, account=credentials["account"], code="CLOSE_UNVERIFIED", message="QFEX close_position submitted but position is not yet flat.", position_action=action)
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="close_position", exchange=name, account=credentials["account"], code="QFEX_ERROR", message=_redact(exc, credentials))


def _set_protection(account: str, request: Mapping[str, Any], *, kind: str) -> CanonicalResponse:
    operation = "set_tp" if kind == "tp" else "set_sl"
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation=operation, exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Set QFEX_<ACCOUNT>_PUBLIC_KEY and QFEX_<ACCOUNT>_SECRET_KEY.")
    requested_symbol = str(request.get("symbol") or "").strip()
    native = _native_symbol(requested_symbol)
    display = _display_symbol(native)
    price_in = _decimal_or_zero(request.get("price"))
    if price_in <= 0:
        action = CanonicalPositionActionResult(operation=operation, symbol=display, verified=False, removed=True, status="unsupported", message="QFEX TP/SL removal is not enabled yet.")
        return make_failure(operation=operation, exchange=name, account=credentials["account"], code="NOT_IMPLEMENTED", message=action.message or "QFEX TP/SL removal is not enabled yet.", position_action=action)
    try:
        pos = _find_position_row(credentials, native)
        if pos is None:
            action = CanonicalPositionActionResult(operation=operation, symbol=display, verified=False, status="failed", current_size="0", message="No open position for symbol.")
            return make_failure(operation=operation, exchange=name, account=credentials["account"], code="NO_OPEN_POSITION", message="No open position for symbol.", position_action=action)
        qty_signed = _decimal_or_zero(pos.get("position") or pos.get("size") or pos.get("quantity"))
        qty_abs = abs(qty_signed)
        if qty_abs <= 0:
            raise ValueError("NO_OPEN_POSITION")
        side_q = "SELL" if qty_signed > 0 else "BUY"
        side_label = "long" if qty_signed > 0 else "short"
        row = _find_refdata_symbol(native) or {}
        tick = _decimal_or_zero(row.get("tick_size"))
        step = _decimal_or_zero(row.get("lot_size"))
        price = _quantize_to_step(price_in, tick, ROUND_HALF_UP)
        qty = _quantize_to_step(qty_abs, step, ROUND_DOWN)
        if qty <= 0 or price <= 0:
            return make_failure(operation=operation, exchange=name, account=credentials["account"], code="INVALID_REQUEST", message="QFEX TP/SL quantity and price must be positive after quantization.")
        client_order_id = str(request.get("client_order_id") or request.get("clientOrderId") or "").strip() or uuid.uuid4().hex[:32]
        order_type = "TAKE_PROFIT" if kind == "tp" else "STOP_LOSS"
        params: dict[str, Any] = {
            "symbol": native,
            "side": side_q,
            "order_type": order_type,
            "order_time_in_force": "GTC",
            "quantity": float(qty),
            "price": float(price),
            "client_order_id": client_order_id,
        }
        if kind == "tp":
            params["take_profit"] = float(price)
        else:
            params["stop_loss"] = float(price)
        command = {"type": "add_order", "params": params}
        row_resp: Mapping[str, Any] = {}
        try:
            payload = _ws_command(credentials, command, expect={"order_response", "stop_order_response"})
            raw = payload.get("order_response") or payload.get("stop_order_response") if isinstance(payload, Mapping) else {}
            row_resp = raw if isinstance(raw, Mapping) else {}
        except Exception:
            orders_payload = _ws_command(credentials, {"type": "get_user_orders", "params": {"limit": 500, "offset": 0, "symbol": native}}, expect={"all_orders_response"})
            for order_row in _extract_order_rows(orders_payload):
                if str(order_row.get("client_order_id") or "") == client_order_id and _is_open_order(order_row):
                    row_resp = order_row
                    break
            if not row_resp:
                raise
        status_native = str(row_resp.get("status") or "").upper()
        accepted = status_native in {"ACK", "MODIFIED", "IOC_PARTIALLY_FILLED"} or bool(row_resp.get("order_id") or row_resp.get("stop_order_id"))
        action = CanonicalPositionActionResult(
            operation=operation,
            symbol=display,
            verified=accepted,
            price=_format_decimal(price),
            status="success" if accepted else (status_native.lower() or "submitted"),
            exchange_order_id=row_resp.get("order_id") or row_resp.get("stop_order_id"),
            current_side=side_label,
            current_size=_format_decimal(qty_abs),
            message=f"QFEX {order_type} submitted for {_format_decimal(qty)} @ {_format_decimal(price)}.",
        )
        if accepted:
            return make_success(operation=operation, exchange=name, account=credentials["account"], position_action=action)
        return make_failure(operation=operation, exchange=name, account=credentials["account"], code="ORDER_FAILED", message=status_native or "QFEX TP/SL order rejected.", position_action=action)
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation=operation, exchange=name, account=credentials["account"], code="QFEX_ERROR", message=_redact(exc, credentials))


def _ladder_prices(start: Decimal, end: Decimal, count: int, tick: Decimal = Decimal("0")) -> list[Decimal]:
    if count <= 0:
        return []
    if count == 1:
        raw = [start]
    else:
        raw = [start + (end - start) * Decimal(i) / Decimal(count - 1) for i in range(count)]
    return [_quantize_to_step(price, tick, ROUND_HALF_UP) for price in raw]


def _ladder_sizes(total: Decimal, count: int, distribution: str, step: Decimal = Decimal("0"), min_size: Decimal = Decimal("0")) -> list[Decimal]:
    if count <= 0:
        return []
    key = str(distribution or "uniform").strip().lower().replace(" ", "_")
    if key == "uniform" or count == 1:
        weights = [Decimal("1")] * count
    elif key == "half_gaussian":
        span = Decimal(count - 1)
        weights = [
            Decimal(str(math.exp(-(float(Decimal("3") * (span - Decimal(i)) / span) ** 2) / 2)))
            for i in range(count)
        ]
    else:
        raise ValueError("distribution must be uniform or half_gaussian")
    weight_sum = sum(weights)
    raw = [total * weight / weight_sum for weight in weights]
    if step <= 0:
        return raw
    total_units = int((total / step).to_integral_value(rounding=ROUND_DOWN))
    min_units = int((min_size / step).to_integral_value(rounding=ROUND_HALF_UP)) if min_size > 0 else 0
    if total_units <= 0 or total_units < min_units * count:
        raise ValueError("Total volume is too small for QFEX ladder size increment/minimum.")
    allocation = [max(min_units, int((item / step).to_integral_value(rounding=ROUND_DOWN))) for item in raw]
    # If minimum sizing pushed the sum over total, fail instead of oversizing.
    if sum(allocation) > total_units:
        raise ValueError("Total volume is too small for QFEX ladder minimum size.")
    residual = total_units - sum(allocation)
    remainders = [(raw[i] / step) - int((raw[i] / step).to_integral_value(rounding=ROUND_DOWN)) for i in range(count)]
    order = sorted(range(count), key=lambda i: (remainders[i], -i), reverse=True)
    for idx in order[:residual]:
        allocation[idx] += 1
    return [Decimal(units) * step for units in allocation]


def _ladder(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="ladder", exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Set QFEX_<ACCOUNT>_PUBLIC_KEY and QFEX_<ACCOUNT>_SECRET_KEY.")
    requested_symbol = str(request.get("symbol") or "").strip()
    side_in = str(request.get("side") or "").strip().lower()
    distribution = str(request.get("distribution") or "uniform").strip().lower().replace(" ", "_")
    try:
        native = _native_symbol(requested_symbol)
        side_q = _qfex_side(side_in)
        count = int(str(request.get("order_count") or "0").strip())
        total = Decimal(str(request.get("total_volume") or "0").strip())
        start = Decimal(str(request.get("start_price") or "0").strip())
        end = Decimal(str(request.get("end_price") or "0").strip())
    except Exception:  # noqa: BLE001
        return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="INVALID_REQUEST", message="symbol, side, order_count, total_volume, start_price and end_price are required.")
    if not native or side_q not in {"BUY", "SELL"} or count <= 0 or total <= 0 or start <= 0 or end <= 0:
        return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="INVALID_REQUEST", message="symbol, side, order_count, total_volume, start_price and end_price must be valid positive values.")
    if count > 100:
        return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="INVALID_REQUEST", message="order_count exceeds safety cap (100).")
    if count > 1 and side_in == "buy" and not (end < start):
        return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="INVALID_REQUEST", message="For a BUY ladder, end_price must be lower than start_price.")
    if count > 1 and side_in == "sell" and not (end > start):
        return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="INVALID_REQUEST", message="For a SELL ladder, end_price must be higher than start_price.")
    if distribution not in {"uniform", "half_gaussian"}:
        return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="INVALID_REQUEST", message="distribution must be uniform or half_gaussian.")

    try:
        row = _find_refdata_symbol(native) or {}
        tick = _decimal_or_zero(row.get("tick_size"))
        step = _decimal_or_zero(row.get("lot_size"))
        min_size = _decimal_or_zero(row.get("min_quantity"))
        prices = _ladder_prices(start, end, count, tick)
        sizes = _ladder_sizes(total, count, distribution, step, min_size)
        batches: list[dict[str, Any]] = []
        child_ids: list[str | int] = []
        submitted_volume = Decimal("0")
        first_error = ""
        for idx, (price, size) in enumerate(zip(prices, sizes)):
            child_client_id = uuid.uuid4().hex[:32]
            resp = _new_order(
                credentials["account"],
                {
                    "symbol": native,
                    "side": side_in,
                    "volume": _format_decimal(size),
                    "price": _format_decimal(price),
                    "client_order_id": child_client_id,
                },
            )
            ok = bool(resp.success and resp.order is not None and resp.order.verified)
            order_id = resp.order.exchange_order_id if resp.order is not None else None
            if ok:
                if order_id is not None:
                    child_ids.append(order_id)
                submitted_volume += size
            elif not first_error:
                first_error = resp.error.message if resp.error else "child order failed"
            batches.append(
                {
                    "index": idx,
                    "price": _format_decimal(price),
                    "size": _format_decimal(size),
                    "ok": ok,
                    "order_id": order_id,
                    "client_order_id": child_client_id,
                    "error": None if ok else (resp.error.message if resp.error else "failed"),
                }
            )
        submitted = sum(1 for b in batches if b.get("ok"))
        verified = submitted == count
        partial = submitted not in {0, count}
        result = CanonicalLadderResult(
            symbol=_display_symbol(native),
            side=side_in,
            distribution=distribution,
            requested_order_count=count,
            submitted_order_count=submitted,
            requested_volume=_format_decimal(total),
            submitted_volume=_format_decimal(submitted_volume),
            batch_count=len(batches),
            verified=verified,
            partial=partial,
            status="success" if verified else ("partial" if partial else "failed"),
            accepted_child_count=submitted,
            omitted_order_count=count - submitted,
            child_order_ids=child_ids,
            batches=batches,
            exchange_reason=first_error or None,
        )
        if verified:
            return make_success(operation="ladder", exchange=name, account=credentials["account"], ladder=result)
        return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="PARTIAL_LADDER" if partial else "LADDER_FAILED", message=first_error or f"Submitted {submitted}/{count} QFEX ladder orders.", ladder=result)
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="QFEX_ERROR", message=_redact(exc, credentials))


def _cancel_order_group(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="cancel_order_group", exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Set QFEX_<ACCOUNT>_PUBLIC_KEY and QFEX_<ACCOUNT>_SECRET_KEY.")
    native = _native_symbol(str(request.get("symbol") or "").strip())
    try:
        side_q = _qfex_side(str(request.get("side") or "").strip())
    except Exception:
        return make_failure(operation="cancel_order_group", exchange=name, account=credentials["account"], code="INVALID_SIDE", message="Side must be buy or sell.")
    try:
        orders_payload = _ws_command(credentials, {"type": "get_user_orders", "params": {"limit": 500, "offset": 0, "symbol": native}}, expect={"all_orders_response"})
        targets = [r for r in _extract_order_rows(orders_payload) if str(r.get("symbol") or "").upper() == native and str(r.get("side") or "").upper() == side_q and _is_open_order(r)]
        batches: list[dict[str, Any]] = []
        cancelled_ids: set[str] = set()
        uncertain_ids: set[str] = set()
        for row in targets:
            order_id = str(row.get("order_id") or "").strip()
            if not order_id:
                continue
            command = {"type": "cancel_order", "params": {"symbol": native, "order_id": order_id, "cancel_order_id_type": "order_id"}}
            status = ""
            ok = False
            try:
                response = _ws_command(credentials, command, expect={"order_response", "ack"})
                if isinstance(response.get("order_response"), Mapping):
                    status = str(response["order_response"].get("status") or "").upper()
                ok = status in {"CANCELLED", "ACK", "NO_SUCH_ORDER", "NOT_FOUND"} or "ack" in response
            except Exception as exc:  # noqa: BLE001
                status = f"VERIFY_AFTER_ERROR: {_redact(exc, credentials)}"
                uncertain_ids.add(order_id)
            if ok:
                cancelled_ids.add(order_id)
            batches.append({"order_id": order_id, "ok": ok, "status": status or ("ACK" if ok else "")})

        if uncertain_ids:
            verify_payload = _ws_command(credentials, {"type": "get_user_orders", "params": {"limit": 500, "offset": 0, "symbol": native}}, expect={"all_orders_response"})
            still_open = {
                str(r.get("order_id") or "").strip()
                for r in _extract_order_rows(verify_payload)
                if str(r.get("symbol") or "").upper() == native
                and str(r.get("side") or "").upper() == side_q
                and _is_open_order(r)
            }
            for batch in batches:
                order_id = str(batch.get("order_id") or "")
                if order_id in uncertain_ids and order_id not in still_open:
                    batch["ok"] = True
                    batch["status"] = "CONFIRMED_ABSENT"
                    cancelled_ids.add(order_id)

        cancelled = len(cancelled_ids)
        verified = cancelled == len(targets)
        result = CanonicalCancelGroupResult(
            symbol=_display_symbol(native),
            side="buy" if side_q == "BUY" else "sell",
            targeted_order_count=len(targets),
            cancelled_order_count=cancelled,
            confirmed_absent_count=cancelled,
            remaining_target_count=max(len(targets) - cancelled, 0),
            verified=verified,
            partial=not verified,
            status="success" if verified else "partial",
            batch_count=len(batches),
            batches=batches,
            requested_cancel_count=len(targets),
            verified_cancel_count=cancelled,
        )
        if verified:
            return make_success(operation="cancel_order_group", exchange=name, account=credentials["account"], cancel_group=result)
        return make_failure(operation="cancel_order_group", exchange=name, account=credentials["account"], code="PARTIAL_CANCEL", message=f"Cancelled {cancelled}/{len(targets)} QFEX orders.", cancel_group=result)
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="cancel_order_group", exchange=name, account=credentials["account"], code="QFEX_ERROR", message=_redact(exc, credentials))


def execute(request: Mapping[str, Any]) -> CanonicalResponse:
    operation = str(request.get("operation") or "").strip().lower()
    account = str(request.get("account") or "").strip()
    if operation == "resolve_instrument":
        return _resolve_instrument(account, request)
    if operation == "market_price":
        return _market_price(account, request)
    if operation == "balance":
        return _balance(account)
    if operation in {"positions_orders", "positions_management"}:
        return _positions_orders(account)
    if operation == "new_order":
        return _new_order(account, request)
    if operation == "ladder":
        return _ladder(account, request)
    if operation == "cancel_order_group":
        return _cancel_order_group(account, request)
    if operation == "close_position":
        return _close_position(account, request)
    if operation == "set_tp":
        return _set_protection(account, request, kind="tp")
    if operation == "set_sl":
        return _set_protection(account, request, kind="sl")
    credentials = _lookup_credentials(account)
    canonical_account = credentials["account"] if credentials else account
    return make_failure(
        operation=operation or "unknown",
        exchange=name,
        account=canonical_account,
        code="NOT_IMPLEMENTED",
        message=f"QFEX agent does not implement operation: {operation or 'unknown'}.",
    )


__all__ = [
    "name",
    "list_accounts",
    "capabilities",
    "execute",
]
