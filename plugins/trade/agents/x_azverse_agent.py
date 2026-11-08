"""AZverse futures agent for KAM ``/trade``.

Credentials are discovered from ``AZVERSE_<ALIAS>_API_KEY``/``_KEY`` and
``AZVERSE_<ALIAS>_SECRET_KEY``/``_SECRET`` in the process environment or
``$HERMES_HOME/.env``. Environment values win. Secrets never leave this
module and are redacted from all errors.

Read operations: balance, positions/orders, instrument catalog, and
market price. Writes are limited to limit ``new_order``, per-order ``cancel_order_group``,
and limit ``ladder``, plus position close/TP/SL. Live POSTs require ``AZVERSE_WRITE_ENABLED=1``
and ``AZVERSE_DRY_RUN=0``; otherwise Confirm is dry-run only. Close-all
and cancel-all endpoints are never used.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

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
PROFIT_CREATE_PATH = "/az/future/trade/v1/entrust/create-profit"
PROFIT_UPDATE_PATH = "/az/future/trade/v1/entrust/update-profit-stop"
PROFIT_CANCEL_PATH = "/az/future/trade/v1/entrust/cancel-profit-stop"
PROFIT_LIST_PATH = "/az/future/trade/v1/entrust/profit-list"
SYMBOLS_PATH = "/az/future/market/v3/public/symbol/list"
TICKER_PATH = "/az/future/market/v1/public/q/ticker"
MARK_PATH = "/az/future/market/v1/public/q/symbol-mark-price"
_ALLOWED_POST_PATHS = frozenset(
    {
        ORDER_CREATE_PATH,
        ORDER_CANCEL_PATH,
        PROFIT_CREATE_PATH,
        PROFIT_UPDATE_PATH,
        PROFIT_CANCEL_PATH,
    }
)
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
        "ladder",
        "close_position",
        "set_tp",
        "set_sl",
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


def _protection_display(prices: List[Decimal]) -> Tuple[Optional[str], Optional[int]]:
    if not prices:
        return None, None
    unique: List[Decimal] = []
    seen = set()
    for price in prices:
        if price not in seen:
            seen.add(price)
            unique.append(price)
    return ", ".join(_format_decimal(price) for price in unique), len(prices)


def _overlay_protections_from_profits(
    positions: List[CanonicalPosition],
    credentials: Mapping[str, str],
) -> List[CanonicalPosition]:
    if not positions:
        return positions
    natives: List[str] = []
    seen = set()
    for position in positions:
        native = str(position.exchange_instrument or "").strip() or position.symbol
        key = native.lower()
        if key in seen:
            continue
        seen.add(key)
        natives.append(native)
    profits_by_native: Dict[str, List[Mapping[str, Any]]] = {}
    for native in natives:
        profits_by_native[native.lower()] = _fetch_open_profits(credentials, native)
    overlaid: List[CanonicalPosition] = []
    for position in positions:
        native = str(position.exchange_instrument or "").strip() or position.symbol
        matches = _profits_for_position(profits_by_native.get(native.lower(), []), native, position.side)
        tp_prices = [price for price in (_protection_price(row, "tp") for row in matches) if price is not None]
        sl_prices = [price for price in (_protection_price(row, "sl") for row in matches) if price is not None]
        tp_text, tp_count = _protection_display(tp_prices)
        sl_text, sl_count = _protection_display(sl_prices)
        overlaid.append(replace(position, tp=tp_text, sl=sl_text, tp_count=tp_count, sl_count=sl_count))
    return overlaid


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
        positions = _overlay_protections_from_profits(
            _normalize_positions(_signed_get(credentials, POSITIONS_PATH)),
            credentials,
        )
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


_OPEN_ORDER_STATES = frozenset({
    "",
    "NEW",
    "UNFINISHED",
    "PARTIALLY_FILLED",
    "PARTIAL",
    "LIVE",
    "OPEN",
    "PENDING",
})
_TERMINAL_ORDER_STATES = frozenset({
    "CANCELED",
    "CANCELLED",
    "FILLED",
    "REJECTED",
    "EXPIRED",
    "FAILED",
    "COMPLETED",
})


def _order_state(row: Mapping[str, Any]) -> str:
    return str(_order_value(row, "state", "status", "orderStatus") or "").strip().upper()


def _is_open_order_row(row: Mapping[str, Any]) -> bool:
    return _order_state(row) not in _TERMINAL_ORDER_STATES


def _matching_open_ids(rows: List[Mapping[str, Any]], symbol: str, side: str, order_type: str) -> List[str]:
    ids: List[str] = []
    for row in rows:
        if not _order_matches_cancel(row, symbol, side, order_type):
            continue
        if not _is_open_order_row(row):
            continue
        order_id = _order_value(row, "orderId", "id", "entrustId")
        if order_id in (None, ""):
            raise ValueError("AZverse open order omitted orderId.")
        ids.append(str(order_id))
    return ids


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
        target_ids = _matching_open_ids(rows, symbol, side, order_type)
        if _is_dry_run(request):
            return make_failure(
                operation,
                name,
                credentials["account"],
                "AZVERSE_DRY_RUN",
                f"AZverse writes are dry-run; {len(target_ids)} matching order(s) were not cancelled.",
            )
        batches: List[Dict[str, Any]] = []
        for order_id in target_ids:
            _signed_request(credentials, "POST", ORDER_CANCEL_PATH, body={"orderId": order_id})
            batches.append({"order_id": order_id, "ok": True, "status": "CANCELLED"})
        remaining_ids = set(_matching_open_ids(_fetch_open_orders(credentials), symbol, side, order_type))
        still_open = [order_id for order_id in target_ids if order_id in remaining_ids]
        retries = 0
        while still_open and retries < 3:
            time.sleep(0.4)
            remaining_ids = set(_matching_open_ids(_fetch_open_orders(credentials), symbol, side, order_type))
            still_open = [order_id for order_id in target_ids if order_id in remaining_ids]
            retries += 1
        verified = not still_open
        cancelled_count = len(target_ids) - len(still_open)
        result = CanonicalCancelGroupResult(
            symbol=_short_symbol(symbol) if "_" in symbol else symbol,
            side=side,
            targeted_order_count=len(target_ids),
            cancelled_order_count=cancelled_count,
            confirmed_absent_count=cancelled_count,
            remaining_target_count=len(still_open),
            verified=verified,
            partial=not verified,
            status="success" if verified else "unknown" if still_open and cancelled_count == 0 else "partial",
            batch_count=len(batches),
            batches=batches,
            requested_cancel_count=len(target_ids),
            verified_cancel_count=cancelled_count,
        )
        if verified:
            return make_success(operation, name, credentials["account"], cancel_group=result)
        if cancelled_count == 0:
            return make_failure(
                operation,
                name,
                credentials["account"],
                "CANCEL_STATUS_UNKNOWN",
                "AZverse accepted the cancel, but the open-order list still showed it. Do not retry until Positions & Orders is reconciled.",
                cancel_group=result,
            )
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


def _quantize_floor(value: Decimal, increment: Optional[Decimal]) -> Decimal:
    if increment is None or increment <= 0:
        return value
    steps = (value / increment).to_integral_value(rounding=ROUND_DOWN)
    return steps * increment


def _increment_from_instrument(data: Mapping[str, Any], key: str) -> Optional[Decimal]:
    raw = data.get(key)
    if raw in (None, ""):
        return None
    parsed = _optional_decimal(raw)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _ladder_distribution_weights(order_count: int, distribution: str) -> List[Decimal]:
    if order_count <= 0:
        return []
    distribution_key = str(distribution or "").strip().lower()
    if distribution_key == "uniform":
        return [Decimal("1")] * order_count
    if distribution_key != "half_gaussian":
        raise ValueError("INVALID_DISTRIBUTION")
    if order_count == 1:
        return [Decimal("1")]
    weights: List[Decimal] = []
    span = Decimal(order_count - 1)
    for index in range(order_count):
        z = Decimal("3") * (span - Decimal(index)) / span
        weights.append(Decimal(str(math.exp(-(float(z) ** 2) / 2.0))))
    return weights


def _build_ladder_children(
    *,
    native: str,
    side: str,
    distribution: str,
    order_count: int,
    total_volume: Decimal,
    start_price: Decimal,
    end_price: Decimal,
    instrument: Mapping[str, Any],
) -> List[Dict[str, str]]:
    weights = _ladder_distribution_weights(order_count, distribution)
    weights_total = sum(weights) or Decimal("1")
    price_step = (end_price - start_price) / Decimal(max(order_count - 1, 1))
    tick = _increment_from_instrument(instrument, "price_increment")
    lot = _increment_from_instrument(instrument, "size_increment")
    min_size = _increment_from_instrument(instrument, "minimum_size") or Decimal("0")
    min_notional = _increment_from_instrument(instrument, "minimum_notional") or Decimal("0")
    children: List[Dict[str, str]] = []
    allocated = Decimal("0")
    for index in range(order_count):
        if index == order_count - 1:
            size = total_volume - allocated
        else:
            size = _quantize_floor(total_volume * weights[index] / weights_total, lot)
            allocated += size
        price = _quantize_floor(start_price + price_step * Decimal(index), tick)
        if size <= 0 or price <= 0:
            raise ValueError("NO_VALID_CHILDREN")
        if min_size > 0 and size < min_size:
            raise ValueError("CHILD_BELOW_MIN_SIZE")
        if min_notional > 0 and (price * size) < min_notional:
            raise ValueError("CHILD_BELOW_MIN_NOTIONAL")
        children.append(
            {
                "symbol": native,
                "orderSide": "BUY" if side == "buy" else "SELL",
                "orderType": "LIMIT",
                "origQty": _format_decimal(size),
                "positionSide": "LONG" if side == "buy" else "SHORT",
                "price": _format_decimal(price),
                "timeInForce": "GTC",
            }
        )
    return children


def _handle_ladder(account: str, request: Mapping[str, Any]):
    operation = "ladder"
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation, name, account, "MISSING_CREDENTIALS", "AZverse account credentials are not configured.")
    requested_symbol = str(request.get("symbol") or "").strip()
    side = str(request.get("side") or "").strip().lower()
    distribution = str(request.get("distribution") or "").strip().lower()
    if not requested_symbol:
        return make_failure(operation, name, account, "MISSING_SYMBOL", "Symbol is required.")
    if side not in {"buy", "sell"}:
        return make_failure(operation, name, account, "INVALID_SIDE", "Side must be buy or sell.")
    if distribution not in {"uniform", "half_gaussian"}:
        return make_failure(operation, name, account, "INVALID_DISTRIBUTION", "Distribution must be uniform or half_gaussian.")
    try:
        order_count = int(str(request.get("order_count") or "").strip())
    except (TypeError, ValueError):
        order_count = 0
    if order_count <= 0:
        return make_failure(operation, name, account, "INVALID_ORDER_COUNT", "Order count must be positive.")
    try:
        total_volume = _positive_decimal(request.get("total_volume"), "volume")
    except Exception:  # noqa: BLE001
        return make_failure(operation, name, account, "INVALID_VOLUME", "Total volume must be positive.")
    try:
        start_price = _positive_decimal(request.get("start_price"), "start_price")
        end_price = _positive_decimal(request.get("end_price"), "end_price")
        row = _find_symbol_row(requested_symbol)
        native = str(row.get("symbol") or "").strip()
        instrument = _instrument_dict(row)
        if side == "buy" and end_price >= start_price:
            return make_failure(operation, name, account, "INVALID_LADDER_DIRECTION", "BUY ladders require end price below start price.")
        if side == "sell" and end_price <= start_price:
            return make_failure(operation, name, account, "INVALID_LADDER_DIRECTION", "SELL ladders require end price above start price.")
        children = _build_ladder_children(
            native=native,
            side=side,
            distribution=distribution,
            order_count=order_count,
            total_volume=total_volume,
            start_price=start_price,
            end_price=end_price,
            instrument=instrument,
        )
    except ValueError as exc:
        code = str(exc)
        if code in {"INVALID_DISTRIBUTION", "NO_VALID_CHILDREN", "CHILD_BELOW_MIN_SIZE", "CHILD_BELOW_MIN_NOTIONAL"}:
            return make_failure(operation, name, account, code, _redact(exc, credentials))
        return make_failure(operation, name, account, "INVALID_REQUEST", _redact(exc, credentials))
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation, name, account, "INVALID_REQUEST", _redact(exc, credentials))
    submitted_volume = sum((Decimal(child["origQty"]) for child in children), Decimal("0"))
    display = _short_symbol(native)
    batches: List[Dict[str, Any]] = []
    child_ids: List[str] = []
    accepted = 0
    if _is_dry_run(request):
        result = CanonicalLadderResult(
            symbol=display,
            side=side,
            distribution=distribution,
            requested_order_count=order_count,
            submitted_order_count=len(children),
            requested_volume=_format_decimal(total_volume),
            submitted_volume=_format_decimal(submitted_volume),
            batch_count=0,
            verified=False,
            partial=False,
            status="dry_run",
            accepted_child_count=0,
            child_order_ids=None,
            batches=None,
        )
        return make_success(operation, name, credentials["account"], ladder=result, data={"dry_run": True, "children": children, "native_symbol": native})
    unknown = False
    unknown_message = ""
    for index, child in enumerate(children):
        try:
            payload = _signed_request(credentials, "POST", ORDER_CREATE_PATH, body=child)
            order_id = _order_id_from_payload(payload)
            if not order_id:
                unknown = True
                unknown_message = "AZverse create response omitted orderId."
                batches.append({"index": index, "ok": False, "status": "UNKNOWN", "path": ORDER_CREATE_PATH})
                break
            accepted += 1
            child_ids.append(order_id)
            batches.append({"index": index, "ok": True, "status": "ACCEPTED", "order_id": order_id, "path": ORDER_CREATE_PATH})
        except Exception as exc:  # noqa: BLE001
            message = _redact(exc, credentials)
            if accepted > 0:
                unknown = True
                unknown_message = message
                batches.append({"index": index, "ok": False, "status": "UNKNOWN", "path": ORDER_CREATE_PATH})
                break
            return make_failure(operation, name, account, "AZVERSE_ORDER_ERROR", message)
    result = CanonicalLadderResult(
        symbol=display,
        side=side,
        distribution=distribution,
        requested_order_count=order_count,
        submitted_order_count=accepted,
        requested_volume=_format_decimal(total_volume),
        submitted_volume=_format_decimal(sum((Decimal(children[i]["origQty"]) for i in range(accepted)), Decimal("0"))) if accepted else "0",
        batch_count=len(batches),
        verified=accepted == order_count and not unknown,
        partial=accepted not in {0, order_count},
        status="unknown" if unknown else ("success" if accepted == order_count else "failed"),
        accepted_child_count=accepted,
        child_order_ids=child_ids or None,
        batches=batches or None,
    )
    if unknown:
        return make_failure(
            operation,
            name,
            credentials["account"],
            "ORDER_STATUS_UNKNOWN",
            "One or more ladder orders may already have been submitted. Do not retry this ladder until open orders are reconciled.",
            ladder=result,
        )
    if accepted == order_count:
        return make_success(operation, name, credentials["account"], ladder=result, data={"children": children, "requested": order_count, "succeeded": accepted, "failed": 0})
    return make_failure(operation, name, credentials["account"], "AZVERSE_ORDER_ERROR", "AZverse ladder submission failed.", ladder=result)


def _request_position_side(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"buy", "long", "b"}:
        return "long"
    if text in {"sell", "short", "s"}:
        return "short"
    raise ValueError("INVALID_SIDE")


def _position_matches(row: Mapping[str, Any], symbol: str, side: str) -> bool:
    native = str(row.get("symbol") or "").strip()
    if not native:
        return False
    if _short_symbol(native).lower() != symbol.lower() and native.lower() != symbol.lower():
        return False
    raw = str(row.get("positionSide") or row.get("side") or "").strip().lower()
    if raw in {"short", "sell"}:
        row_side = "short"
    elif raw in {"long", "buy"}:
        row_side = "long"
    else:
        return False
    return row_side == side


def _find_position_row(credentials: Mapping[str, str], symbol: str, side: str) -> Mapping[str, Any]:
    rows = _signed_get(credentials, POSITIONS_PATH)
    if not isinstance(rows, list):
        raise ValueError("AZverse position response is not a list.")
    matches = [row for row in rows if isinstance(row, Mapping) and _position_matches(row, symbol, side)]
    open_matches = []
    for row in matches:
        size = abs(_required_first_decimal(row, "positionSize", "quantity"))
        if size > 0:
            open_matches.append(row)
    if not open_matches:
        raise ValueError("POSITION_NOT_FOUND")
    if len(open_matches) > 1:
        raise ValueError("POSITION_AMBIGUOUS")
    return open_matches[0]


def _profit_id(row: Mapping[str, Any]) -> Optional[str]:
    value = row.get("profitId")
    if value in (None, "", 0, "0"):
        return None
    return str(value)


def _profit_items(payload: Any) -> List[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        raise ValueError("AZverse profit-list response is not an object.")
    rows = payload.get("items")
    if rows is None:
        rows = payload.get("list")
    if rows is None:
        raise ValueError("AZverse profit-list omitted items.")
    if not isinstance(rows, list):
        raise ValueError("AZverse profit-list items are not a list.")
    if any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("AZverse profit-list contained a malformed row.")
    return list(rows)


def _fetch_open_profits(credentials: Mapping[str, str], native: str) -> List[Mapping[str, Any]]:
    payload = _signed_get(
        credentials,
        PROFIT_LIST_PATH,
        {"symbol": native, "state": "UNFINISHED", "page": 1, "size": 100},
    )
    open_rows: List[Mapping[str, Any]] = []
    for row in _profit_items(payload):
        state = str(row.get("state") or "").strip().upper()
        if state in _TERMINAL_ORDER_STATES:
            continue
        open_rows.append(row)
    return open_rows


def _profits_for_position(rows: List[Mapping[str, Any]], native: str, side: str) -> List[Mapping[str, Any]]:
    wanted = "LONG" if side == "long" else "SHORT"
    matches: List[Mapping[str, Any]] = []
    for row in rows:
        row_native = str(row.get("symbol") or "").strip()
        if row_native.lower() != native.lower() and _short_symbol(row_native).lower() != _short_symbol(native).lower():
            continue
        row_side = str(row.get("positionSide") or "").strip().upper()
        if row_side != wanted:
            continue
        matches.append(row)
    return matches


def _protection_price(row: Mapping[str, Any], kind: str) -> Optional[Decimal]:
    key = "triggerProfitPrice" if kind == "tp" else "triggerStopPrice"
    value = _optional_decimal(row.get(key))
    if value is not None and value <= 0:
        return None
    return value


def _protection_verified(
    rows: List[Mapping[str, Any]],
    kind: str,
    price: Decimal,
    removing: bool,
    ignored_ids: Optional[set] = None,
    cleared_ids: Optional[set] = None,
) -> bool:
    ignored_ids = ignored_ids or set()
    cleared_ids = cleared_ids or set()
    actuals: List[Optional[Decimal]] = []
    for row in rows:
        profit_id = _profit_id(row)
        if profit_id is not None and profit_id in ignored_ids:
            continue
        if removing and profit_id is not None and profit_id in cleared_ids:
            continue
        actuals.append(_protection_price(row, kind))
    if removing:
        return all(value is None for value in actuals)
    return any(value is not None and value == price for value in actuals)


def _handle_close_position(account: str, request: Mapping[str, Any]):
    operation = "close_position"
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation, name, account, "MISSING_CREDENTIALS", "AZverse account credentials are not configured.")
    symbol = str(request.get("symbol") or "").strip()
    if not symbol:
        return make_failure(operation, name, account, "MISSING_SYMBOL", "Symbol is required.")
    try:
        side = _request_position_side(request.get("side"))
        row = _find_position_row(credentials, symbol, side)
        native = str(row.get("symbol") or "").strip()
        size = abs(_required_first_decimal(row, "positionSize", "quantity"))
    except ValueError as exc:
        code = str(exc)
        if code in {"INVALID_SIDE", "POSITION_NOT_FOUND", "POSITION_AMBIGUOUS"}:
            return make_failure(operation, name, account, code, _redact(exc, credentials))
        return make_failure(operation, name, account, "INVALID_REQUEST", _redact(exc, credentials))
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation, name, account, "AZVERSE_POSITIONS_ORDERS_ERROR", _redact(exc, credentials))
    display = _short_symbol(native)
    body = {
        "symbol": native,
        "orderSide": "SELL" if side == "long" else "BUY",
        "orderType": "MARKET",
        "origQty": _format_decimal(size),
        "positionSide": "LONG" if side == "long" else "SHORT",
        "reduceOnly": True,
    }
    action = CanonicalPositionActionResult(
        operation=operation,
        symbol=display,
        verified=False,
        status="dry_run",
        current_side=side,
        current_size=_format_decimal(size),
        message="Close preview; no order was submitted.",
    )
    if _is_dry_run(request):
        return make_success(operation, name, credentials["account"], position_action=action, data={"dry_run": True, "native_symbol": native})
    try:
        payload = _signed_request(credentials, "POST", ORDER_CREATE_PATH, body=body)
        order_id = _order_id_from_payload(payload)
        remaining = _signed_get(credentials, POSITIONS_PATH)
        still = [
            item
            for item in remaining
            if isinstance(item, Mapping) and _position_matches(item, symbol, side) and abs(_required_first_decimal(item, "positionSize", "quantity")) > 0
        ] if isinstance(remaining, list) else [True]
        verified = not still
        result = CanonicalPositionActionResult(
            operation=operation,
            symbol=display,
            verified=verified,
            status="success" if verified else "unknown",
            exchange_order_id=order_id,
            current_side=None if verified else side,
            current_size="0" if verified else _format_decimal(size),
            message="Position closed." if verified else "Close submitted; reconcile Positions & Orders. Do not retry.",
        )
        if verified:
            return make_success(operation, name, credentials["account"], position_action=result)
        return make_failure(
            operation,
            name,
            credentials["account"],
            "ORDER_STATUS_UNKNOWN",
            "Close was submitted but the position still appears open. Do not retry until Positions & Orders is reconciled.",
            position_action=result,
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation, name, account, "AZVERSE_ORDER_ERROR", _redact(exc, credentials))


def _handle_set_protection(account: str, request: Mapping[str, Any], kind: str):
    operation = "set_tp" if kind == "tp" else "set_sl"
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation, name, account, "MISSING_CREDENTIALS", "AZverse account credentials are not configured.")
    symbol = str(request.get("symbol") or "").strip()
    if not symbol:
        return make_failure(operation, name, account, "MISSING_SYMBOL", "Symbol is required.")
    raw_price = request.get("price")
    try:
        side = _request_position_side(request.get("side"))
        if str(raw_price).strip() == "0":
            price = Decimal("0")
        else:
            price = _positive_decimal(raw_price, "price")
        row = _find_position_row(credentials, symbol, side)
        native = str(row.get("symbol") or "").strip()
        size = abs(_required_first_decimal(row, "positionSize", "quantity"))
        profits = _profits_for_position(_fetch_open_profits(credentials, native), native, side)
        existing = profits[0] if profits else None
        existing_id = _profit_id(existing) if existing is not None else _profit_id(row)
        existing_tp = _protection_price(existing, "tp") if existing is not None else _optional_decimal(row.get("triggerProfitPrice"))
        existing_sl = _protection_price(existing, "sl") if existing is not None else _optional_decimal(row.get("triggerStopPrice"))
        kind_rows = [item for item in profits if _protection_price(item, kind) is not None]
    except ValueError as exc:
        code = str(exc)
        if code in {"INVALID_SIDE", "POSITION_NOT_FOUND", "POSITION_AMBIGUOUS"}:
            return make_failure(operation, name, account, code, _redact(exc, credentials))
        return make_failure(operation, name, account, "INVALID_REQUEST", _redact(exc, credentials))
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation, name, account, "AZVERSE_POSITIONS_ORDERS_ERROR", _redact(exc, credentials))
    display = _short_symbol(native)
    current = _protection_price(kind_rows[0], kind) if kind_rows else (existing_tp if kind == "tp" else existing_sl)
    other = existing_sl if kind == "tp" else existing_tp
    removing = price == 0
    if removing and current is None:
        return make_success(
            operation,
            name,
            credentials["account"],
            position_action=CanonicalPositionActionResult(
                operation=operation,
                symbol=display,
                verified=True,
                removed=False,
                current_side=side,
                current_size=_format_decimal(size),
                message="No Take Profit was set." if kind == "tp" else "No Stop Loss was set.",
            ),
        )
    if _is_dry_run(request):
        return make_success(
            operation,
            name,
            credentials["account"],
            position_action=CanonicalPositionActionResult(
                operation=operation,
                symbol=display,
                verified=False,
                price=None if removing else _format_decimal(price),
                removed=removing,
                status="dry_run",
                current_side=side,
                current_size=_format_decimal(size),
                message="Protection preview; no order was submitted.",
            ),
            data={"dry_run": True},
        )
    try:
        if removing:
            cancelled_ids = set()
            cleared_ids = set()
            for item in kind_rows:
                profit_id = _profit_id(item)
                if not profit_id:
                    continue
                other_price = _protection_price(item, "sl" if kind == "tp" else "tp")
                if other_price is None:
                    _signed_request(credentials, "POST", PROFIT_CANCEL_PATH, body={"profitId": profit_id})
                    cancelled_ids.add(profit_id)
                    continue
                body = {"profitId": profit_id}
                if kind == "tp":
                    body["triggerStopPrice"] = _format_decimal(other_price)
                else:
                    body["triggerProfitPrice"] = _format_decimal(other_price)
                _signed_request(credentials, "POST", PROFIT_UPDATE_PATH, body=body)
                cleared_ids.add(profit_id)
        else:
            cancelled_ids = set()
            cleared_ids = set()
            if existing_id:
                body = {"profitId": existing_id}
                if kind == "tp":
                    body["triggerProfitPrice"] = _format_decimal(price)
                    if other is not None:
                        body["triggerStopPrice"] = _format_decimal(other)
                else:
                    body["triggerStopPrice"] = _format_decimal(price)
                    if other is not None:
                        body["triggerProfitPrice"] = _format_decimal(other)
                _signed_request(credentials, "POST", PROFIT_UPDATE_PATH, body=body)
            else:
                body = {
                    "symbol": native,
                    "positionSide": "LONG" if side == "long" else "SHORT",
                    "origQty": _format_decimal(size),
                    "triggerPriceType": "MARK_PRICE",
                }
                if kind == "tp":
                    body["triggerProfitPrice"] = _format_decimal(price)
                    if existing_sl is not None:
                        body["triggerStopPrice"] = _format_decimal(existing_sl)
                else:
                    body["triggerStopPrice"] = _format_decimal(price)
                    if existing_tp is not None:
                        body["triggerProfitPrice"] = _format_decimal(existing_tp)
                _signed_request(credentials, "POST", PROFIT_CREATE_PATH, body=body)
        refreshed = _profits_for_position(_fetch_open_profits(credentials, native), native, side)
        verified = _protection_verified(
            refreshed, kind, price, removing, ignored_ids=cancelled_ids, cleared_ids=cleared_ids
        )
        retries = 0
        while not verified and retries < 3:
            time.sleep(0.4)
            refreshed = _profits_for_position(_fetch_open_profits(credentials, native), native, side)
            verified = _protection_verified(
                refreshed, kind, price, removing, ignored_ids=cancelled_ids, cleared_ids=cleared_ids
            )
            retries += 1
        result = CanonicalPositionActionResult(
            operation=operation,
            symbol=display,
            verified=verified,
            price=None if removing else _format_decimal(price),
            removed=removing,
            status="success" if verified else "unknown",
            current_side=side,
            current_size=_format_decimal(size),
            message=("Take Profit removed." if removing else "Take Profit updated.") if kind == "tp" else ("Stop Loss removed." if removing else "Stop Loss updated."),
        )
        if verified:
            return make_success(operation, name, credentials["account"], position_action=result)
        return make_failure(
            operation,
            name,
            credentials["account"],
            "PROTECTION_STATUS_UNKNOWN",
            "AZverse accepted the protection change, but the profit-list did not yet show it. Do not retry until Positions & Orders is reconciled.",
            position_action=result,
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation, name, account, "AZVERSE_ORDER_ERROR", _redact(exc, credentials))


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
    if operation == "ladder":
        return _handle_ladder(account, request)
    if operation == "close_position":
        return _handle_close_position(account, request)
    if operation == "set_tp":
        return _handle_set_protection(account, request, "tp")
    if operation == "set_sl":
        return _handle_set_protection(account, request, "sl")
    return make_failure(
        operation or "unknown",
        name,
        account,
        "NOT_IMPLEMENTED",
        f"AZverse does not implement {operation!r}.",
    )
