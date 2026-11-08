"""Read-only AZverse futures agent for KAM ``/trade``.

Credentials are discovered from ``AZVERSE_<ALIAS>_API_KEY``/``_KEY`` and
``AZVERSE_<ALIAS>_SECRET_KEY``/``_SECRET`` in the process environment or
``$HERMES_HOME/.env``. Environment values win. Secrets never leave this
module and are redacted from all errors.

Supported operations are deliberately limited to balance, positions/orders,
and the positions-management read alias. No exchange mutation is implemented
or advertised.
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
    CanonicalOrderGroup,
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
    return ["balance", "positions_orders", "positions_management"]


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


def _signed_get(
    credentials: Mapping[str, str],
    path: str,
    query: Optional[Mapping[str, Any]] = None,
) -> Any:
    query_string = _query_string(query)
    timestamp = str(int(time.time() * 1000))
    request_part = f"#{path}" + (f"#{query_string}" if query_string else "")
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
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
            "validate-appkey": str(credentials["api_key"]),
            "validate-timestamp": timestamp,
            "validate-signature": signature,
        },
    )
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
            transport_error = _redact(f"AZverse HTTP {exc.code} ({code}): {message}", credentials, (signature,))
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
            transport_error = _redact(f"AZverse HTTP {exc.code}: {exc.reason}", credentials, (signature,))
        except Exception as body_exc:  # noqa: BLE001 - error-body readers are untrusted
            transport_error = _redact(body_exc, credentials, (signature,))
    except urllib.error.URLError as exc:
        transport_error = _redact(f"AZverse request failed: {exc.reason}", credentials, (signature,))
    except Exception as exc:  # noqa: BLE001 - fail closed and redact auth material
        transport_error = _redact(exc, credentials, (signature,))
    if transport_error is not None:
        raise RuntimeError(transport_error)

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
        processing_error = _redact(exc, credentials, (signature,))
    if processing_error is not None:
        raise RuntimeError(processing_error)
    return result


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


def execute(request: Mapping[str, Any]):
    operation = str(request.get("operation") or "").strip().lower()
    account = str(request.get("account") or "").strip()
    if operation == "balance":
        return _handle_balance(account)
    if operation == "positions_orders":
        return _handle_positions(account, operation, include_orders=True)
    if operation == "positions_management":
        return _handle_positions(account, operation, include_orders=False)
    return make_failure(
        operation or "unknown",
        name,
        account,
        "NOT_IMPLEMENTED",
        f"AZverse does not implement {operation!r}.",
    )
