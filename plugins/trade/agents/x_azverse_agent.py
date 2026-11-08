"""AZverse futures agent for KAM ``/trade``.

Credentials are discovered from ``AZVERSE_<ALIAS>_API_KEY``/``_KEY`` and
``AZVERSE_<ALIAS>_SECRET_KEY``/``_SECRET`` in the process environment or
``$HERMES_HOME/.env``. Environment values win. Secrets never leave this
module and are redacted from all errors.

Read operations: balance, positions/orders, instrument catalog, and
market price. Writes are limited to limit ``new_order`` and per-order
``cancel_order_group``. Live POSTs require ``AZVERSE_WRITE_ENABLED=1``
and ``AZVERSE_DRY_RUN=0``; otherwise Confirm is dry-run only. Ladder,
TP/SL, and close-position are not implemented.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..canonical import (
    CanonicalCancelGroupResult,
    CanonicalInstrument,
    CanonicalMarketPrice,
    CanonicalOrderGroup,
    CanonicalOrderResult,
    CanonicalPortfolioSummary,
    CanonicalPosition,
    make_failure,
    make_success,
    normalize_balance,
    sanitize_error_message,
)

name = "azverse"

DEFAULT_API_BASE = "https://f-api.azverse.xyz"
API_TIMEOUT_SECONDS = 30
BALANCE_PATH = "/az/future/user/v1/balance/list"
POSITIONS_PATH = "/az/future/user/v1/position/list"
ORDERS_PATH = "/az/future/trade/v1/order/list"
ORDER_CREATE_PATH = "/az/future/trade/v1/order/create"
ORDER_CANCEL_PATH = "/az/future/trade/v1/order/cancel"
SYMBOLS_PATH = "/az/future/market/v3/public/symbol/list"
TICKER_PATH = "/az/future/market/v1/public/q/ticker"
MARK_PATH = "/az/future/market/v1/public/q/symbol-mark-price"
_ALLOWED_POST_PATHS = frozenset({ORDER_CREATE_PATH, ORDER_CANCEL_PATH})
_USER_AGENT = "Hermes-KAM-AZverseAgent/1.0"
_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_KEY_SUFFIXES = ("API_KEY", "KEY")
_SECRET_SUFFIXES = ("SECRET_KEY", "SECRET")
_REQUIRED_SUFFIXES = tuple(sorted(_KEY_SUFFIXES + _SECRET_SUFFIXES, key=len, reverse=True))


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


def _combined_azverse_env() -> Dict[str, Tuple[str, str, str]]:
    combined: Dict[str, Tuple[str, str, str]] = {}
    for key, value in os.environ.items():
        if key.upper().startswith("AZVERSE_"):
            combined.setdefault(key.upper(), (key, str(value), "env"))
    for key, value in _load_dotenv_values(_hermes_home() / ".env").items():
        if key.upper().startswith("AZVERSE_"):
            combined.setdefault(key.upper(), (key, str(value), "dotenv"))
    return combined


def _parse_alias_suffix(upper_key: str) -> Optional[Tuple[str, str]]:
    if not upper_key.startswith("AZVERSE_"):
        return None
    rest = upper_key[len("AZVERSE_") :]
    for suffix in _REQUIRED_SUFFIXES:
        token = "_" + suffix
        if rest.endswith(token):
            alias = rest[: -len(token)]
            if alias and _ALIAS_PATTERN.fullmatch(alias):
                field = "KEY" if suffix in _KEY_SUFFIXES else "SECRET"
                return alias, field
    return None


def _discover_credential_map() -> Dict[str, Dict[str, str]]:
    selections: Dict[str, Dict[str, Tuple[Tuple[int, int], str]]] = {}
    source_rank = {"dotenv": 1, "env": 2}
    for upper_key, (_actual, raw_value, source) in _combined_azverse_env().items():
        parsed = _parse_alias_suffix(upper_key)
        if parsed is None:
            continue
        alias_upper, suffix = parsed
        value = raw_value.strip()
        if not value:
            continue
        alias_rank = 2 if upper_key.endswith(("_API_KEY", "_SECRET_KEY")) else 1
        priority = (source_rank.get(source, 0), alias_rank)
        field = suffix.lower()
        alias = alias_upper.lower()
        current = selections.setdefault(alias, {}).get(field)
        if current is None or priority > current[0]:
            selections[alias][field] = (priority, value)
    return {
        alias: {field: selected[1] for field, selected in fields.items()}
        for alias, fields in selections.items()
        if fields.get("key") and fields.get("secret")
    }


def list_accounts() -> List[str]:
    return sorted(_discover_credential_map())


def capabilities() -> List[str]:
    return [
        "balance",
        "positions_orders",
        "positions_management",
        "new_order",
        "cancel_orders",
        "cancel_order_group",
        "list_instruments",
        "resolve_instrument",
        "market_price",
    ]


def _lookup_credentials(account: str) -> Optional[Dict[str, str]]:
    alias = str(account or "").strip().lower()
    fields = _discover_credential_map().get(alias)
    if not fields:
        return None
    return {
        "account": alias,
        "api_key": fields["key"],
        "secret": fields["secret"],
        "api_base": DEFAULT_API_BASE,
    }


def _redact(
    value: Any,
    credentials: Optional[Mapping[str, str]] = None,
    extra_secrets: Tuple[str, ...] = (),
) -> str:
    text = str(value or "")
    if credentials:
        for key in ("api_key", "secret"):
            secret = str(credentials.get(key) or "")
            if secret:
                text = text.replace(secret, "***")
    for secret in extra_secrets:
        if secret:
            text = text.replace(secret, "***")
    text = re.sub(
        r'((?:["\']?validate-(?:appkey|signature)["\']?)\s*(?:=|:)\s*["\']?)[^&#\s,"\'}]+',
        r"\1***",
        text,
        flags=re.IGNORECASE,
    )
    return sanitize_error_message(text)


def _query_string(query: Optional[Mapping[str, Any]]) -> str:
    filtered = {
        str(key): str(value)
        for key, value in dict(query or {}).items()
        if value is not None
    }
    return urllib.parse.urlencode(sorted(filtered.items()))


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so signed headers never cross an origin boundary."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def _open_no_redirect(request: urllib.request.Request, timeout: int):
    opener = urllib.request.build_opener(_NoRedirectHandler())
    return opener.open(request, timeout=timeout)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _falsey(value: Any) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "off", "live"}


def _is_dry_run(request: Optional[Mapping[str, Any]] = None) -> bool:
    if isinstance(request, Mapping) and "dry_run" in request:
        return bool(request.get("dry_run"))
    if not _truthy(os.environ.get("AZVERSE_WRITE_ENABLED", "")):
        return True
    return not _falsey(os.environ.get("AZVERSE_DRY_RUN", "1"))


def _json_body(body: Optional[Mapping[str, Any]]) -> str:
    if body is None:
        return ""
    if not isinstance(body, Mapping):
        raise ValueError("AZverse request body must be an object.")
    return json.dumps(dict(body), separators=(",", ":"), ensure_ascii=False)


def _read_http_payload(
    credentials: Optional[Mapping[str, str]],
    request: urllib.request.Request,
    extra_secrets: Tuple[str, ...] = (),
) -> str:
    transport_error: Optional[str] = None
    body = ""
    try:
        with _open_no_redirect(request, API_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(error_body)
            message = str(parsed.get("msgInfo") or parsed.get("error") or parsed.get("message") or exc.reason)
            code = parsed.get("returnCode") or parsed.get("code") or exc.code
            transport_error = _redact(f"AZverse HTTP {exc.code} ({code}): {message}", credentials, extra_secrets)
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
            transport_error = _redact(f"AZverse HTTP {exc.code}: {exc.reason}", credentials, extra_secrets)
        except Exception as body_exc:  # noqa: BLE001 - error-body readers are untrusted
            transport_error = _redact(body_exc, credentials, extra_secrets)
    except urllib.error.URLError as exc:
        transport_error = _redact(f"AZverse request failed: {exc.reason}", credentials, extra_secrets)
    except Exception as exc:  # noqa: BLE001 - fail closed and redact auth material
        transport_error = _redact(exc, credentials, extra_secrets)
    if transport_error is not None:
        raise RuntimeError(transport_error)
    return body


def _parse_envelope(body: str, credentials: Optional[Mapping[str, str]] = None, extra_secrets: Tuple[str, ...] = ()) -> Any:
    processing_error: Optional[str] = None
    result: Any = None
    try:
        payload = json.loads(body)
        if not isinstance(payload, Mapping):
            processing_error = "AZverse returned a non-object response envelope."
        else:
            result = _unwrap_envelope(payload)
    except json.JSONDecodeError:
        processing_error = "AZverse returned invalid JSON."
    except Exception as exc:  # noqa: BLE001 - response processing must also redact
        processing_error = _redact(exc, credentials, extra_secrets)
    if processing_error is not None:
        raise RuntimeError(processing_error)
    return result


def _signed_request(
    credentials: Mapping[str, str],
    method: str,
    path: str,
    query: Optional[Mapping[str, Any]] = None,
    body: Optional[Mapping[str, Any]] = None,
) -> Any:
    method_upper = str(method or "").strip().upper()
    if method_upper not in {"GET", "POST"}:
        raise RuntimeError(f"AZverse does not allow {method_upper} requests.")
    if method_upper == "POST" and path not in _ALLOWED_POST_PATHS:
        raise RuntimeError("AZverse mutation path is not allowed.")
    if method_upper == "GET" and body is not None:
        raise RuntimeError("AZverse GET requests cannot carry a body.")
    query_string = _query_string(query)
    body_string = _json_body(body)
    timestamp = str(int(time.time() * 1000))
    request_part = f"#{path}"
    if query_string:
        request_part += f"#{query_string}"
    if body_string:
        request_part += f"#{body_string}"
    presign = (
        f"validate-appkey={credentials['api_key']}"
        f"&validate-timestamp={timestamp}{request_part}"
    )
    signature = hmac.new(
        str(credentials["secret"]).encode("utf-8"),
        presign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    url = f"{str(credentials.get('api_base') or DEFAULT_API_BASE).rstrip('/')}{path}"
    if query_string:
        url += "?" + query_string
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
        "validate-appkey": str(credentials["api_key"]),
        "validate-timestamp": timestamp,
        "validate-signature": signature,
    }
    request = urllib.request.Request(
        url,
        data=body_string.encode("utf-8") if method_upper == "POST" else None,
        method=method_upper,
        headers=headers,
    )
    raw = _read_http_payload(credentials, request, (signature,))
    return _parse_envelope(raw, credentials, (signature,))


def _signed_get(
    credentials: Mapping[str, str],
    path: str,
    query: Optional[Mapping[str, Any]] = None,
) -> Any:
    return _signed_request(credentials, "GET", path, query=query)


def _public_get(path: str, query: Optional[Mapping[str, Any]] = None) -> Any:
    query_string = _query_string(query)
    url = f"{DEFAULT_API_BASE.rstrip('/')}{path}"
    if query_string:
        url += "?" + query_string
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        },
    )
    return _parse_envelope(_read_http_payload(None, request))


def _unwrap_envelope(payload: Mapping[str, Any]) -> Any:
    return_code = payload.get("returnCode")
    if return_code not in (None, 0, "0", 200, "200"):
        message = payload.get("msgInfo") or payload.get("error") or "AZverse rejected the request."
        raise RuntimeError(f"AZverse API {return_code}: {message}")
    if "result" not in payload:
        raise RuntimeError("AZverse response omitted result.")
    return payload.get("result")


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        result = Decimal(str(value if value not in (None, "") else default))
        return result if result.is_finite() else Decimal(default)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _optional_decimal(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("AZverse numeric value is invalid.") from exc
    if not result.is_finite():
        raise ValueError("AZverse numeric value is not finite.")
    return result


def _required_decimal(row: Mapping[str, Any], key: str) -> Decimal:
    if key not in row or row.get(key) in (None, ""):
        raise ValueError(f"AZverse row omitted {key}.")
    try:
        value = _optional_decimal(row.get(key))
    except ValueError as exc:
        raise ValueError(f"AZverse row has invalid {key}.") from exc
    if value is None:
        raise ValueError(f"AZverse row has invalid {key}.")
    return value


def _decimal_or_zero_if_absent(row: Mapping[str, Any], key: str) -> Decimal:
    if key not in row or row.get(key) in (None, ""):
        return Decimal("0")
    return _required_decimal(row, key)


def _required_first_decimal(row: Mapping[str, Any], *keys: str) -> Decimal:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return _required_decimal(row, key)
    raise ValueError(f"AZverse row omitted {'/'.join(keys)}.")


def _format_decimal(value: Decimal) -> str:
    if value.normalize() == 0:
        return "0"
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _short_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper().replace("-", "_").replace("/", "_")
    for quote in ("_USDT", "_USDC", "USDT", "USDC"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)].rstrip("_")
    return symbol


def _balance_rows(payload: Any) -> List[Mapping[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError("AZverse balance response is not a list.")
    if any(not isinstance(row, Mapping) for row in payload):
        raise ValueError("AZverse balance list contained a malformed row.")
    return list(payload)


def _handle_balance(account: str):
    operation = "balance"
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation, name, account, "MISSING_CREDENTIALS", "AZverse account credentials are not configured.")
    try:
        rows = _balance_rows(_signed_get(credentials, BALANCE_PATH))
        if not rows:
            return make_failure(operation, name, account, "AZVERSE_BALANCE_EMPTY", "AZverse returned no futures balance rows.")
        preferred = next(
            (row for coin in ("USDT", "USDC") for row in rows if str(row.get("coin") or "").upper() == coin),
            rows[0],
        )
        unit = str(preferred.get("coin") or "").strip().upper()
        if not unit:
            return make_failure(operation, name, account, "AZVERSE_BALANCE_MALFORMED", "AZverse balance row omitted coin.")
        wallet = _required_decimal(preferred, "walletBalance")
        available = _required_decimal(preferred, "availableBalance")
        margin_used = sum(
            (
                _decimal_or_zero_if_absent(preferred, "isolatedMargin"),
                _decimal_or_zero_if_absent(preferred, "crossedMargin"),
                _decimal_or_zero_if_absent(preferred, "openOrderMarginFrozen"),
                _decimal_or_zero_if_absent(preferred, "openOrderFeeFrozen"),
            ),
            Decimal("0"),
        )
        _decimal_or_zero_if_absent(preferred, "unrealizedPnl")
        assets = []
        for row in rows:
            coin = str(row.get("coin") or "").strip().upper()
            if not coin:
                raise ValueError("AZverse balance row omitted coin.")
            total = _required_decimal(row, "walletBalance")
            free = _required_decimal(row, "availableBalance")
            assets.append(
                {
                    "asset": coin,
                    "total": _format_decimal(total),
                    "free": _format_decimal(free),
                    "locked": _format_decimal(total - free),
                }
            )
        balance = normalize_balance(wallet, unit)
        summary = CanonicalPortfolioSummary(
            account_value=balance.value,
            withdrawable=normalize_balance(available, unit).value,
            margin_used=normalize_balance(margin_used, unit).value,
            total_position_value="",
            unit=unit,
        )
        return make_success(
            operation,
            name,
            credentials["account"],
            balance=balance,
            portfolio_summary=summary,
            data={"assets": assets},
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation, name, account, "AZVERSE_BALANCE_ERROR", _redact(exc, credentials))


def _normalize_positions(payload: Any) -> List[CanonicalPosition]:
    if not isinstance(payload, list):
        raise ValueError("AZverse position response is not a list.")
    if any(not isinstance(row, Mapping) for row in payload):
        raise ValueError("AZverse position list contained a malformed row.")
    positions: List[CanonicalPosition] = []
    for row in payload:
        size = abs(_required_first_decimal(row, "positionSize", "quantity"))
        if size == 0:
            continue
        symbol_native = str(row.get("symbol") or "").strip()
        if not symbol_native:
            raise ValueError("AZverse position omitted symbol.")
        side_raw = str(row.get("positionSide") or row.get("side") or "").strip().lower()
        if side_raw in {"short", "sell"}:
            side = "short"
        elif side_raw in {"long", "buy"}:
            side = "long"
        else:
            raise ValueError("AZverse position has invalid side.")
        entry = _required_decimal(row, "entryPrice")
        if entry <= 0:
            raise ValueError("AZverse position has invalid entryPrice.")
        mark = _optional_decimal(row.get("markPrice"))
        if mark is not None and mark <= 0:
            raise ValueError("AZverse position has invalid markPrice.")
        explicit_pnl = _optional_decimal(
            row.get("unrealizedPnl")
            if row.get("unrealizedPnl") not in (None, "")
            else row.get("unrealisedPnl")
        )
        if explicit_pnl is not None:
            pnl = _format_decimal(explicit_pnl)
        elif mark is not None and side == "long":
            pnl = _format_decimal((mark - entry) * size)
        elif mark is not None and side == "short":
            pnl = _format_decimal((entry - mark) * size)
        else:
            pnl = ""
        take_profit = _optional_decimal(row.get("triggerProfitPrice"))
        stop_loss = _optional_decimal(row.get("triggerStopPrice"))
        if take_profit is not None and take_profit <= 0:
            raise ValueError("AZverse position has invalid triggerProfitPrice.")
        if stop_loss is not None and stop_loss <= 0:
            raise ValueError("AZverse position has invalid triggerStopPrice.")
        positions.append(
            CanonicalPosition(
                symbol=_short_symbol(symbol_native),
                side=side,
                size=_format_decimal(size),
                entry_price=_format_decimal(entry),
                pnl=pnl,
                tp=_format_decimal(take_profit) if take_profit is not None else None,
                sl=_format_decimal(stop_loss) if stop_loss is not None else None,
                exchange_instrument=symbol_native,
                mark=_format_decimal(mark) if mark is not None else None,
            )
        )
    return positions


def _order_items(payload: Any) -> Tuple[List[Mapping[str, Any]], int]:
    if isinstance(payload, list):
        raise ValueError("AZverse order response must use an object envelope.")
    if not isinstance(payload, Mapping):
        raise ValueError("AZverse order response is not an object or list.")

    raw_items: Any = None
    found_items = False
    for key in ("items", "list", "dataList"):
        if key in payload:
            raw_items = payload.get(key)
            found_items = True
            break
    if not found_items:
        raise ValueError("AZverse order response omitted items.")
    if not isinstance(raw_items, list):
        raise ValueError("AZverse order response has invalid items.")
    if any(not isinstance(row, Mapping) for row in raw_items):
        raise ValueError("AZverse order list contained a malformed row.")
    rows = list(raw_items)

    if "total" not in payload or payload.get("total") is None:
        raise ValueError("AZverse order response omitted total.")
    total_raw = payload.get("total")
    if isinstance(total_raw, bool):
        raise ValueError("AZverse order response has invalid total.")
    try:
        total = int(total_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("AZverse order response has invalid total.") from exc
    if str(total_raw).strip() != str(total):
        raise ValueError("AZverse order response has invalid total.")
    if total < 0 or total < len(rows):
        raise ValueError("AZverse order response has inconsistent total.")
    return rows, total


def _fetch_open_orders(credentials: Mapping[str, str]) -> List[Mapping[str, Any]]:
    page = 1
    size = 100
    expected_total: Optional[int] = None
    all_rows: List[Mapping[str, Any]] = []
    seen: set[str] = set()
    while True:
        payload = _signed_get(credentials, ORDERS_PATH, {"page": page, "size": size, "state": "UNFINISHED"})
        rows, total = _order_items(payload)
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise ValueError("AZverse order pagination changed the reported total.")
        if len(all_rows) + len(rows) > expected_total:
            raise ValueError("AZverse order pagination exceeded the reported total.")
        if len(rows) < size and len(all_rows) + len(rows) < expected_total:
            raise ValueError("AZverse order pagination returned a short non-final page.")
        for row in rows:
            order_id = _order_value(row, "orderId", "id", "entrustId")
            identity = f"id:{order_id}" if order_id not in (None, "") else "row:" + json.dumps(row, sort_keys=True, default=str)
            if identity in seen:
                raise ValueError("AZverse order pagination repeated an order.")
            seen.add(identity)
        if not rows and len(all_rows) < expected_total:
            raise ValueError("AZverse order pagination ended before the reported total.")
        all_rows.extend(rows)
        if len(all_rows) == expected_total:
            return all_rows
        page += 1
        if page > 100:
            raise RuntimeError("AZverse open-order pagination exceeded 100 pages.")


def _order_value(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _optional_first_decimal_or_zero(row: Mapping[str, Any], *keys: str) -> Decimal:
    for key in keys:
        if key in row and row.get(key) not in (None, ""):
            return _required_decimal(row, key)
    return Decimal("0")


def _order_bool(row: Mapping[str, Any], key: str) -> bool:
    value = row.get(key)
    if value in (None, "", False, 0, "0"):
        return False
    if value in (True, 1, "1"):
        return True
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    raise ValueError(f"AZverse order has invalid {key}.")


def _order_classification(row: Mapping[str, Any]) -> Tuple[str, str]:
    raw_type = _order_value(row, "orderType", "type", "entrustType")
    if raw_type in (None, ""):
        raise ValueError("AZverse order omitted order type.")
    order_type = str(raw_type).strip().upper()
    if "TAKE_PROFIT" in order_type or order_type in {"TP", "PROFIT"}:
        return "take_profit", order_type
    if "STOP_LOSS" in order_type or order_type in {"SL", "STOP"}:
        return "stop_loss", order_type
    if "TRIGGER" in order_type or "PLAN" in order_type:
        return "trigger", order_type
    if "LIMIT" in order_type:
        return "entry_limit", order_type
    return "other", order_type


def _normalize_order_groups(rows: List[Mapping[str, Any]]) -> List[CanonicalOrderGroup]:
    grouped: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    for row in rows:
        symbol_native = str(_order_value(row, "symbol", "contract") or "").strip()
        if not symbol_native:
            raise ValueError("AZverse order omitted symbol.")
        side = str(_order_value(row, "orderSide", "side") or "").strip().lower()
        if side not in {"buy", "sell"}:
            raise ValueError("AZverse order has invalid side.")
        classification, display_type = _order_classification(row)
        original = _required_first_decimal(row, "origQty", "quantity", "size", "volume")
        executed = _optional_first_decimal_or_zero(row, "executedQty", "dealQty", "filledQty")
        if original <= 0 or executed < 0 or executed > original:
            raise ValueError("AZverse order has invalid quantity values.")
        price = _optional_first_decimal_or_zero(row, "price", "orderPrice", "limitPrice")
        if price < 0 or (classification == "entry_limit" and price <= 0):
            raise ValueError("AZverse order has invalid price.")
        remaining = original - executed
        if remaining == 0:
            continue
        key = (symbol_native, side, classification, display_type)
        slot = grouped.setdefault(
            key,
            {
                "symbol": _short_symbol(symbol_native),
                "symbol_native": symbol_native,
                "side": side,
                "classification": classification,
                "display_type": display_type,
                "count": 0,
                "size": Decimal("0"),
                "notional": Decimal("0"),
                "prices": [],
                "ids": [],
                "reduce_only": False,
            },
        )
        slot["count"] += 1
        slot["size"] += remaining
        slot["notional"] += remaining * price
        if price > 0:
            slot["prices"].append(price)
        order_id = _order_value(row, "orderId", "id", "entrustId")
        if order_id not in (None, ""):
            slot["ids"].append(str(order_id))
        slot["reduce_only"] = slot["reduce_only"] or _order_bool(row, "reduceOnly")

    groups: List[CanonicalOrderGroup] = []
    for key in sorted(grouped):
        slot = grouped[key]
        prices = slot["prices"]
        size = slot["size"]
        vwap = slot["notional"] / size if size > 0 and prices else Decimal("0")
        groups.append(
            CanonicalOrderGroup(
                symbol=slot["symbol"],
                side=slot["side"],
                order_count=slot["count"],
                total_size=_format_decimal(size),
                vwap=_format_decimal(vwap) if prices else "",
                min_price=_format_decimal(min(prices)) if prices else "",
                max_price=_format_decimal(max(prices)) if prices else "",
                classification=slot["classification"],
                display_type=slot["display_type"],
                reduce_only=slot["reduce_only"],
                order_ids=slot["ids"],
                exchange_instrument=slot["symbol_native"] or None,
            )
        )
    return groups


def _handle_positions(account: str, operation: str, include_orders: bool):
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation, name, account, "MISSING_CREDENTIALS", "AZverse account credentials are not configured.")
    try:
        positions = _normalize_positions(_signed_get(credentials, POSITIONS_PATH))
        if not include_orders:
            return make_success(operation, name, credentials["account"], positions=positions)
        order_rows = _fetch_open_orders(credentials)
        return make_success(
            operation,
            name,
            credentials["account"],
            positions=positions,
            open_order_count=len(order_rows),
            order_groups=_normalize_order_groups(order_rows),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation, name, account, "AZVERSE_POSITIONS_ORDERS_ERROR", _redact(exc, credentials))


def _compact_symbol(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").strip().upper())


def _symbol_matches(row: Mapping[str, Any], requested: str) -> bool:
    native = str(row.get("symbol") or "").strip()
    if not native or not requested:
        return False
    requested_norm = requested.strip()
    compact_requested = _compact_symbol(requested_norm)
    candidates = {
        native.lower(),
        _short_symbol(native).lower(),
        str(row.get("baseCoin") or "").strip().lower(),
        str(row.get("pair") or "").strip().lower(),
        _compact_symbol(native).lower(),
        _compact_symbol(_short_symbol(native)).lower(),
    }
    return requested_norm.lower() in candidates or compact_requested.lower() in candidates


def _is_tradable_symbol(row: Mapping[str, Any]) -> bool:
    if row.get("isBlack") is True:
        return False
    if row.get("tradeSwitch") is False:
        return False
    if row.get("isOpenApi") is False:
        return False
    contract = str(row.get("contractType") or row.get("productType") or "").strip().upper()
    if contract and "PERPETUAL" not in contract and contract != "PERP":
        return False
    return True


def _symbol_rows() -> List[Mapping[str, Any]]:
    payload = _public_get(SYMBOLS_PATH)
    rows: Any
    if isinstance(payload, Mapping) and "symbols" in payload:
        rows = payload.get("symbols")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise ValueError("AZverse symbol catalog is not a list.")
    if any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("AZverse symbol catalog contained a malformed row.")
    tradable = [row for row in rows if _is_tradable_symbol(row) and str(row.get("symbol") or "").strip()]
    if not tradable:
        raise ValueError("AZverse symbol catalog contained no tradable perpetual contracts.")
    return tradable


def _precision_step(row: Mapping[str, Any], key: str) -> Optional[str]:
    raw = row.get(key)
    if raw in (None, ""):
        return None
    try:
        precision = int(raw)
    except (TypeError, ValueError):
        return None
    if precision < 0 or precision > 18:
        return None
    step = Decimal("1").scaleb(-precision)
    return _format_decimal(step)


def _instrument_dict(row: Mapping[str, Any]) -> Dict[str, Any]:
    native = str(row.get("symbol") or "").strip()
    if not native:
        raise ValueError("AZverse instrument omitted symbol.")
    base = str(row.get("baseCoin") or "").strip().upper() or None
    quote = str(row.get("quoteCoin") or "").strip().upper() or None
    display = str(row.get("enName") or "").strip() or _short_symbol(native)
    min_qty = row.get("minQty")
    min_notional = row.get("minNotional")
    step_price = row.get("minStepPrice")
    return {
        "instrument": native,
        "symbol": native,
        "native_symbol": native,
        "display_symbol": _short_symbol(native),
        "display_name": display,
        "base": base,
        "quote": quote,
        "market_type": "perp",
        "price_increment": str(step_price).strip() if step_price not in (None, "") else _precision_step(row, "pricePrecision"),
        "size_increment": _precision_step(row, "quantityPrecision"),
        "minimum_size": str(min_qty).strip() if min_qty not in (None, "") else None,
        "minimum_notional": str(min_notional).strip() if min_notional not in (None, "") else None,
    }


def _canonical_instrument(row: Mapping[str, Any], requested: str) -> CanonicalInstrument:
    data = _instrument_dict(row)
    return CanonicalInstrument(
        requested_symbol=requested,
        symbol=str(data["symbol"]),
        display_name=str(data["display_name"]),
        native_symbol=str(data["native_symbol"]),
        display_symbol=str(data["display_symbol"]),
        base=data.get("base"),
        quote=data.get("quote"),
        market_type="perp",
        price_increment=data.get("price_increment"),
        size_increment=data.get("size_increment"),
        minimum_size=data.get("minimum_size"),
        minimum_notional=data.get("minimum_notional"),
    )


def _find_symbol_row(requested: str) -> Mapping[str, Any]:
    query = str(requested or "").strip()
    if not query:
        raise ValueError("Symbol is required.")
    matches = [row for row in _symbol_rows() if _symbol_matches(row, query)]
    if len(matches) == 1:
        return matches[0]
    exact = [
        row
        for row in matches
        if str(row.get("symbol") or "").strip().lower() == query.lower()
        or _short_symbol(row.get("symbol")).lower() == query.lower()
    ]
    if len(exact) == 1:
        return exact[0]
    if not matches:
        raise ValueError(f"AZverse instrument {query!r} was not found.")
    raise ValueError(f"AZverse instrument {query!r} is ambiguous.")


def _handle_list_instruments(account: str, request: Mapping[str, Any]):
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure("list_instruments", name, account, "MISSING_CREDENTIALS", "AZverse account credentials are not configured.")
    query = str(request.get("query") or request.get("symbol") or "").strip()
    try:
        rows = _symbol_rows()
        if query:
            rows = [row for row in rows if _symbol_matches(row, query)]
        instruments = [_instrument_dict(row) for row in rows]
        return make_success("list_instruments", name, credentials["account"], data={"instruments": instruments})
    except Exception as exc:  # noqa: BLE001
        return make_failure("list_instruments", name, account, "CATALOG_UNAVAILABLE", _redact(exc, credentials))


def _handle_resolve_instrument(account: str, request: Mapping[str, Any]):
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure("resolve_instrument", name, account, "MISSING_CREDENTIALS", "AZverse account credentials are not configured.")
    requested = str(request.get("symbol") or request.get("requested_symbol") or "").strip()
    if not requested:
        return make_failure("resolve_instrument", name, account, "INVALID_REQUEST", "Symbol is required.")
    try:
        row = _find_symbol_row(requested)
        return make_success(
            "resolve_instrument",
            name,
            credentials["account"],
            instrument=_canonical_instrument(row, requested),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure("resolve_instrument", name, account, "INSTRUMENT_NOT_FOUND", _redact(exc, credentials))


def _handle_market_price(account: str, request: Mapping[str, Any]):
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure("market_price", name, account, "MISSING_CREDENTIALS", "AZverse account credentials are not configured.")
    requested = str(request.get("symbol") or request.get("requested_symbol") or "").strip()
    if not requested:
        return make_failure("market_price", name, account, "INVALID_REQUEST", "Symbol is required.")
    try:
        row = _find_symbol_row(requested)
        native = str(row.get("symbol") or "").strip()
        ticker = _public_get(TICKER_PATH, {"symbol": native})
        mark_payload = _public_get(MARK_PATH, {"symbol": native})
        if not isinstance(ticker, Mapping) or not isinstance(mark_payload, Mapping):
            raise ValueError("AZverse market-price response is not an object.")
        last = _optional_decimal(ticker.get("c"))
        mark = _optional_decimal(mark_payload.get("p") if mark_payload.get("p") not in (None, "") else ticker.get("mx"))
        if last is not None and last <= 0:
            raise ValueError("AZverse last price is invalid.")
        if mark is not None and mark <= 0:
            raise ValueError("AZverse mark price is invalid.")
        if last is None and mark is None:
            raise ValueError("AZverse market price omitted last and mark.")
        instrument = _canonical_instrument(row, requested)
        return make_success(
            "market_price",
            name,
            credentials["account"],
            market_price=CanonicalMarketPrice(
                requested_symbol=requested,
                market=native,
                symbol=native,
                display_name=instrument.display_name,
                native_symbol=native,
                display_symbol=instrument.display_symbol,
                base=instrument.base,
                quote=instrument.quote,
                market_type="perp",
                price_increment=instrument.price_increment,
                size_increment=instrument.size_increment,
                minimum_size=instrument.minimum_size,
                minimum_notional=instrument.minimum_notional,
                mark_price=_format_decimal(mark) if mark is not None else None,
                price=_format_decimal(last) if last is not None else (_format_decimal(mark) if mark is not None else None),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure("market_price", name, account, "MARKET_PRICE_UNAVAILABLE", _redact(exc, credentials))


def _positive_decimal(value: Any, field: str) -> Decimal:
    parsed = _optional_decimal(value)
    if parsed is None or parsed <= 0:
        raise ValueError(f"{field} must be a positive number.")
    return parsed


def _order_id_from_payload(payload: Any) -> Optional[str]:
    if isinstance(payload, Mapping):
        value = _order_value(payload, "orderId", "id", "entrustId")
        return str(value) if value not in (None, "") else None
    if payload not in (None, ""):
        return str(payload)
    return None


def _handle_new_order(account: str, request: Mapping[str, Any]):
    operation = "new_order"
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation, name, account, "MISSING_CREDENTIALS", "AZverse account credentials are not configured.")
    requested_symbol = str(request.get("symbol") or "").strip()
    side = str(request.get("side") or "").strip().lower()
    order_type = str(request.get("order_type") or request.get("type") or "limit").strip().lower()
    if order_type not in {"limit", ""}:
        return make_failure(operation, name, account, "UNSUPPORTED_ORDER_TYPE", "AZverse /trade new_order supports limit orders only.")
    if side not in {"buy", "sell"}:
        return make_failure(operation, name, account, "INVALID_REQUEST", "Side must be buy or sell.")
    try:
        volume = _positive_decimal(request.get("volume") or request.get("size") or request.get("quantity"), "volume")
        price = _positive_decimal(request.get("price"), "price")
        row = _find_symbol_row(requested_symbol)
        native = str(row.get("symbol") or "").strip()
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation, name, account, "INVALID_REQUEST", _redact(exc, credentials))
    display = _short_symbol(native)
    body = {
        "symbol": native,
        "orderSide": "BUY" if side == "buy" else "SELL",
        "orderType": "LIMIT",
        "origQty": _format_decimal(volume),
        "positionSide": "LONG" if side == "buy" else "SHORT",
        "price": _format_decimal(price),
        "timeInForce": "GTC",
    }
    result = CanonicalOrderResult(
        symbol=display,
        side=side,
        order_type="limit",
        requested_volume=_format_decimal(volume),
        requested_price=_format_decimal(price),
        submitted_volume=_format_decimal(volume),
        submitted_price=_format_decimal(price),
        verified=False,
        status="dry_run",
    )
    if _is_dry_run(request):
        return make_success(operation, name, credentials["account"], order=result, data={"dry_run": True, "native_symbol": native})
    try:
        payload = _signed_request(credentials, "POST", ORDER_CREATE_PATH, body=body)
        exchange_order_id = _order_id_from_payload(payload)
        if not exchange_order_id:
            return make_failure(operation, name, account, "ORDER_UNVERIFIED", "AZverse create response omitted orderId.")
        submitted_volume = volume
        submitted_price = price
        if isinstance(payload, Mapping):
            if payload.get("origQty") not in (None, ""):
                submitted_volume = _required_decimal(payload, "origQty")
            if payload.get("price") not in (None, ""):
                submitted_price = _required_decimal(payload, "price")
        live = CanonicalOrderResult(
            symbol=display,
            side=side,
            order_type="limit",
            requested_volume=_format_decimal(volume),
            requested_price=_format_decimal(price),
            submitted_volume=_format_decimal(submitted_volume),
            submitted_price=_format_decimal(submitted_price),
            verified=True,
            status="success",
            exchange_order_id=exchange_order_id,
        )
        return make_success(operation, name, credentials["account"], order=live)
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation, name, account, "AZVERSE_ORDER_ERROR", _redact(exc, credentials))


def _order_matches_cancel(row: Mapping[str, Any], symbol: str, side: str, order_type: str) -> bool:
    native = str(_order_value(row, "symbol", "contract") or "").strip()
    if not native:
        return False
    if _short_symbol(native).lower() != symbol.lower() and native.lower() != symbol.lower():
        return False
    row_side = str(_order_value(row, "orderSide", "side") or "").strip().lower()
    if row_side != side:
        return False
    if not order_type:
        return True
    _classification, display_type = _order_classification(row)
    return display_type.upper() == order_type.upper()


def _handle_cancel_order_group(account: str, request: Mapping[str, Any]):
    operation = "cancel_order_group"
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation, name, account, "MISSING_CREDENTIALS", "AZverse account credentials are not configured.")
    symbol = str(request.get("symbol") or "").strip()
    side = str(request.get("side") or "").strip().lower()
    order_type = str(request.get("order_type") or "").strip()
    if not symbol or side not in {"buy", "sell"}:
        return make_failure(operation, name, account, "INVALID_REQUEST", "Symbol and buy/sell side are required.")
    try:
        rows = _fetch_open_orders(credentials)
        targets = [row for row in rows if _order_matches_cancel(row, symbol, side, order_type)]
        target_ids = []
        for row in targets:
            order_id = _order_value(row, "orderId", "id", "entrustId")
            if order_id in (None, ""):
                raise ValueError("AZverse open order omitted orderId.")
            target_ids.append(str(order_id))
        if _is_dry_run(request):
            return make_failure(
                operation,
                name,
                credentials["account"],
                "AZVERSE_DRY_RUN",
                f"AZverse writes are dry-run; {len(target_ids)} matching order(s) were not cancelled.",
            )
        batches: List[Dict[str, Any]] = []
        cancelled: List[str] = []
        for order_id in target_ids:
            _signed_request(credentials, "POST", ORDER_CANCEL_PATH, body={"orderId": order_id})
            batches.append({"order_id": order_id, "ok": True, "status": "CANCELLED"})
            cancelled.append(order_id)
        remaining_rows = _fetch_open_orders(credentials)
        remaining_ids = {
            str(_order_value(row, "orderId", "id", "entrustId"))
            for row in remaining_rows
            if _order_matches_cancel(row, symbol, side, order_type)
        }
        still_open = [order_id for order_id in target_ids if order_id in remaining_ids]
        verified = not still_open
        result = CanonicalCancelGroupResult(
            symbol=_short_symbol(symbol) if "_" in symbol else symbol,
            side=side,
            targeted_order_count=len(target_ids),
            cancelled_order_count=len(target_ids) - len(still_open),
            confirmed_absent_count=len(target_ids) - len(still_open),
            remaining_target_count=len(still_open),
            verified=verified,
            partial=not verified,
            status="success" if verified else "partial",
            batch_count=len(batches),
            batches=batches,
            requested_cancel_count=len(target_ids),
            verified_cancel_count=len(target_ids) - len(still_open),
        )
        if verified:
            return make_success(operation, name, credentials["account"], cancel_group=result)
        return make_failure(
            operation,
            name,
            credentials["account"],
            "PARTIAL_CANCEL",
            f"Cancelled {result.cancelled_order_count}/{result.targeted_order_count} AZverse orders.",
            cancel_group=result,
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation, name, account, "AZVERSE_CANCEL_ERROR", _redact(exc, credentials))


def execute(request: Mapping[str, Any]):
    operation = str(request.get("operation") or "").strip().lower()
    account = str(request.get("account") or "").strip()
    if operation == "balance":
        return _handle_balance(account)
    if operation == "positions_orders":
        return _handle_positions(account, operation, include_orders=True)
    if operation == "positions_management":
        return _handle_positions(account, operation, include_orders=False)
    if operation == "list_instruments":
        return _handle_list_instruments(account, request)
    if operation == "resolve_instrument":
        return _handle_resolve_instrument(account, request)
    if operation == "market_price":
        return _handle_market_price(account, request)
    if operation == "new_order":
        return _handle_new_order(account, request)
    if operation in {"cancel_orders", "cancel_order_group"}:
        return _handle_cancel_order_group(account, request)
    return make_failure(
        operation or "unknown",
        name,
        account,
        "NOT_IMPLEMENTED",
        f"AZverse does not implement {operation!r}.",
    )
