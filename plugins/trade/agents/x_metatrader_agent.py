"""Unified MetaTrader exchange agent for KAM /trade (Phase A read-only).

Architecture:
Linux/Hermes -> Windows FastAPI (LINUX_WIN_HOST:LINUX_WIN_PORT) ->
localhost TCP hub -> HubListener EA(s) -> MT4/MT5 accounts.

This agent deliberately does not know whether a destination terminal is MT4
or MT5. The selected account alias maps only to the actual MetaTrader login
number from MT_<ALIAS>_ACCOUNT. All platform-specific behavior belongs to the
Windows EA side.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from ..ladder_math import build_ladder_children
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

name = "metatrader"

_ACCOUNT_KEY_RE = re.compile(r"^MT_([A-Z0-9_]+)_ACCOUNT$")
_ALIAS_RE = re.compile(r"^[A-Z0-9_]+$")
_MUTATING_OPS = frozenset(
    {
        "modify_order",
        "place_order",
        "position_close",
        "position_tp",
        "position_sl",
    }
)
_READ_OPS = frozenset({"ping", "balance", "positions_orders"})
_TIMEOUT_SECONDS = 30
METATRADER_MAGIC_NUMBER = 26092601


class MetaTraderConfigError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


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
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _combined_env() -> Dict[str, str]:
    values = _load_dotenv_values(_hermes_home() / ".env")
    for key, value in os.environ.items():
        values[key] = value
    return {str(k): str(v).strip() for k, v in values.items()}


def _positive_int(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not re.fullmatch(r"[1-9][0-9]*", text):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def discover_accounts() -> Dict[str, int]:
    """Return {ALIAS: MetaTrader login} from MT_<ALIAS>_ACCOUNT variables."""
    env = _combined_env()
    out: Dict[str, int] = {}
    for key, value in env.items():
        match = _ACCOUNT_KEY_RE.fullmatch(key)
        if not match:
            continue
        alias = match.group(1).strip().upper()
        if not alias or not _ALIAS_RE.fullmatch(alias):
            continue
        account = _positive_int(value)
        if account is None:
            continue
        out[alias] = account
    return dict(sorted(out.items()))


def list_accounts() -> List[str]:
    return sorted(discover_accounts().keys())


def capabilities() -> List[str]:
    return [
        "balance",
        "positions_orders",
        "positions_management",
        "ping",
        "new_order",
        "cancel_order",
        "cancel_orders",
        "cancel_order_group",
        "close_position",
        "set_tp",
        "set_sl",
        "ladder",
        "symbols",
        "ticker",
        "candles",
        "list_instruments",
        "resolve_instrument",
        "market_price",
    ]


def _read_required_env(key: str) -> str:
    value = _combined_env().get(key, "").strip()
    if not value:
        raise MetaTraderConfigError("BRIDGE_CONFIG_INVALID", f"Missing required {key}.")
    return value


def bridge_base_url() -> str:
    host = _read_required_env("LINUX_WIN_HOST")
    raw_port = _read_required_env("LINUX_WIN_PORT")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise MetaTraderConfigError("BRIDGE_CONFIG_INVALID", "LINUX_WIN_PORT must be an integer.") from exc
    if not (1 <= port <= 65535):
        raise MetaTraderConfigError("BRIDGE_CONFIG_INVALID", "LINUX_WIN_PORT must be between 1 and 65535.")
    return f"http://{host}:{port}"


def _request_id() -> str:
    return str(uuid.uuid4())


def _coerce_request_id(value: Any) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", text):
        return text.lower()
    return _request_id()


def _short_comment(request_id: str) -> str:
    return "XA:" + str(request_id).replace("-", "")[:12]


def _decimal_field(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _json_number(value: Decimal) -> float | int:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _alias_to_account(alias: str) -> Tuple[str, Optional[int]]:
    alias_upper = str(alias or "").strip().upper()
    if not alias_upper:
        return alias_upper, None
    return alias_upper, discover_accounts().get(alias_upper)


def _bridge_post(payload: Mapping[str, Any], timeout_seconds: int = _TIMEOUT_SECONDS) -> Dict[str, Any]:
    url = bridge_base_url().rstrip("/") + "/ea"
    data = json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Hermes-MetaTraderAgent/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:  # noqa: S310 configured bridge
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            decoded_error = json.loads(body)
        except json.JSONDecodeError:
            decoded_error = None
        if isinstance(decoded_error, dict):
            return decoded_error
        raise MetaTraderConfigError("BRIDGE_HTTP_ERROR", sanitize_error_message(body or str(exc))) from exc
    except TimeoutError as exc:
        raise MetaTraderConfigError("EA_TIMEOUT", "Timed out waiting for Windows bridge.") from exc
    except Exception as exc:  # noqa: BLE001
        raise MetaTraderConfigError("BRIDGE_UNAVAILABLE", sanitize_error_message(str(exc))) from exc
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MetaTraderConfigError("BRIDGE_MALFORMED_RESPONSE", "Windows bridge returned non-JSON response.") from exc
    if not isinstance(decoded, dict):
        raise MetaTraderConfigError("BRIDGE_MALFORMED_RESPONSE", "Windows bridge response was not an object.")
    return decoded


def _failure(operation: str, account: str, code: str, message: str) -> CanonicalResponse:
    return make_failure(operation=operation, exchange=name, account=account, code=code, message=message)


def _readonly_failure(operation: str, account: str) -> CanonicalResponse:
    return _failure(
        operation,
        account,
        "NOT_IMPLEMENTED",
        "MetaTrader Phase A is read-only; trading, cancellation, and position mutation are not implemented.",
    )


def _decimal_text(value: Any, default: str = "0") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip()
        return text if text else default
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, ValueError):
        return str(value)


def _sum_counts(rows: Iterable[Mapping[str, Any]]) -> int:
    total = 0
    for row in rows:
        try:
            total += int(row.get("count") or row.get("order_count") or 0)
        except (TypeError, ValueError):
            continue
    return total


def _is_ok_response(data: Mapping[str, Any]) -> bool:
    status = str(data.get("status") or "").strip().upper()
    return data.get("ok") is True or status == "COMPLETED"


def _error_from_response(operation: str, alias: str, data: Mapping[str, Any]) -> CanonicalResponse:
    if data.get("forwarded") is True:
        try:
            clients = int(data.get("clients") or 0)
        except (TypeError, ValueError):
            clients = 0
        if clients <= 0:
            return _failure(operation, alias, "EA_DISCONNECTED", "No EA connected for selected MetaTrader account.")
        return _failure(
            operation,
            alias,
            "EA_NO_RESPONSE",
            "Windows bridge forwarded the request but did not return a correlated EA response.",
        )
    code = str(data.get("error") or data.get("code") or "EA_FAILED").strip() or "EA_FAILED"
    message = str(data.get("message") or data.get("detail") or code).strip() or code
    return _failure(operation, alias, code, sanitize_error_message(message))


def _validate_correlated(operation: str, alias: str, payload: Mapping[str, Any], response: Mapping[str, Any]) -> Optional[CanonicalResponse]:
    expected_id = str(payload.get("request_id") or "")
    got_id = str(response.get("request_id") or "")
    if got_id and got_id != expected_id:
        return _failure(operation, alias, "REQUEST_ID_MISMATCH", "EA response request_id did not match the request.")
    expected_account = payload.get("account")
    if response.get("account") is not None:
        try:
            got_account = int(str(response.get("account")))
        except (TypeError, ValueError):
            return _failure(operation, alias, "ACCOUNT_MISMATCH", "EA response account was invalid.")
        if got_account != expected_account:
            return _failure(operation, alias, "ACCOUNT_MISMATCH", "EA response account did not match selected alias.")
    return None


def _call_ea(operation: str, alias: str) -> Tuple[Optional[Dict[str, Any]], Optional[CanonicalResponse]]:
    alias_upper, account_login = _alias_to_account(alias)
    if account_login is None:
        return None, _failure(operation, alias, "ACCOUNT_NOT_CONFIGURED", "MetaTrader account alias is not configured.")
    try:
        payload = {
            "request_id": _request_id(),
            "account": account_login,
            "action": operation,
        }
        response = _bridge_post(payload)
    except MetaTraderConfigError as exc:
        return None, _failure(operation, alias_upper or alias, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001
        return None, _failure(operation, alias_upper or alias, "BRIDGE_UNAVAILABLE", sanitize_error_message(str(exc)))
    mismatch = _validate_correlated(operation, alias_upper, payload, response)
    if mismatch is not None:
        return None, mismatch
    if not _is_ok_response(response):
        return None, _error_from_response(operation, alias_upper, response)
    return response, None


def _call_ea_payload(
    operation: str,
    alias: str,
    payload: Mapping[str, Any],
    *,
    ambiguous_on_transport_error: bool = False,
) -> Tuple[Optional[Dict[str, Any]], Optional[CanonicalResponse]]:
    alias_upper, account_login = _alias_to_account(alias)
    if account_login is None:
        return None, _failure(operation, alias, "ACCOUNT_NOT_CONFIGURED", "MetaTrader account alias is not configured.")
    wire_payload = dict(payload)
    wire_payload["account"] = account_login
    wire_payload["action"] = operation
    try:
        response = _bridge_post(wire_payload)
    except MetaTraderConfigError as exc:
        if ambiguous_on_transport_error and exc.code in {"EA_TIMEOUT", "BRIDGE_UNAVAILABLE", "BRIDGE_HTTP_ERROR"}:
            return None, _failure(
                operation,
                alias_upper or alias,
                "AMBIGUOUS_EXECUTION",
                "Order submission outcome is unknown; do not retry automatically. Reconcile before resubmitting.",
            )
        return None, _failure(operation, alias_upper or alias, exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001
        if ambiguous_on_transport_error:
            return None, _failure(
                operation,
                alias_upper or alias,
                "AMBIGUOUS_EXECUTION",
                "Order submission outcome is unknown; do not retry automatically. Reconcile before resubmitting.",
            )
        return None, _failure(operation, alias_upper or alias, "BRIDGE_UNAVAILABLE", sanitize_error_message(str(exc)))
    mismatch = _validate_correlated(operation, alias_upper, wire_payload, response)
    if mismatch is not None:
        return None, mismatch
    if not _is_ok_response(response):
        return None, _error_from_response(operation, alias_upper, response)
    return response, None


def _execute_ping(account: str) -> CanonicalResponse:
    response, failure = _call_ea("ping", account)
    if failure is not None:
        return failure
    assert response is not None
    return make_success(operation="ping", exchange=name, account=str(account).strip().upper(), data=dict(response))


def _execute_balance(account: str) -> CanonicalResponse:
    response, failure = _call_ea("balance", account)
    if failure is not None:
        return failure
    assert response is not None
    unit = str(response.get("currency") or "").strip()
    if not unit:
        return _failure("balance", str(account).strip().upper(), "BALANCE_MISSING_CURRENCY", "EA balance response did not include currency.")
    balance_value = response.get("balance")
    if balance_value is None:
        return _failure("balance", str(account).strip().upper(), "BALANCE_MISSING", "EA balance response did not include balance.")
    try:
        balance = normalize_balance(balance_value, unit)
        equity = normalize_balance(response.get("equity", balance_value), unit).value
        margin = normalize_balance(response.get("margin", 0), unit).value
        free_margin = normalize_balance(response.get("free_margin", response.get("margin_free", 0)), unit).value
    except Exception as exc:  # noqa: BLE001
        return _failure("balance", str(account).strip().upper(), "BALANCE_MALFORMED", sanitize_error_message(str(exc)))
    data = dict(response)
    return make_success(
        operation="balance",
        exchange=name,
        account=str(account).strip().upper(),
        balance=balance,
        portfolio_summary=CanonicalPortfolioSummary(
            account_value=equity,
            withdrawable=free_margin,
            margin_used=margin,
            total_position_value=equity,
            unit=unit,
        ),
        data=data,
    )


def _position_from_group(row: Mapping[str, Any]) -> CanonicalPosition:
    side = str(row.get("direction") or row.get("side") or "").strip().upper()
    if side not in {"BUY", "SELL"}:
        side = side or "UNKNOWN"
    symbol = str(row.get("symbol") or "").strip()
    size = _decimal_text(row.get("total_volume", row.get("volume", row.get("size", 0))))
    entry = _decimal_text(row.get("vwap", row.get("entry_price", row.get("avg_entry_price", 0))))
    pnl = _decimal_text(row.get("floating_pl", row.get("pnl", 0)))
    tp = row.get("tp")
    sl = row.get("sl")
    return CanonicalPosition(
        symbol=symbol,
        side=side,
        size=size,
        entry_price=entry,
        pnl=pnl,
        tp=None if tp is None else _decimal_text(tp),
        sl=None if sl is None else _decimal_text(sl),
    )


def _order_group_from_row(row: Mapping[str, Any]) -> CanonicalOrderGroup:
    order_type = str(row.get("order_type") or row.get("type") or row.get("display_type") or "").strip().upper()
    side = "buy" if order_type.startswith("BUY") else "sell" if order_type.startswith("SELL") else str(row.get("side") or "").strip().lower()
    if side not in {"buy", "sell"}:
        side = "buy"
    count = int(row.get("count") or row.get("order_count") or 0)
    total = _decimal_text(row.get("total_volume", row.get("volume", row.get("total_size", 0))))
    vwap = _decimal_text(row.get("vwap", row.get("avg_price", row.get("price", 0))))
    min_price = _decimal_text(row.get("min_price", row.get("minimum_price", row.get("price", 0))))
    max_price = _decimal_text(row.get("max_price", row.get("maximum_price", row.get("price", 0))))
    return CanonicalOrderGroup(
        symbol=str(row.get("symbol") or "").strip(),
        side=side,
        order_count=count,
        total_size=total,
        vwap=vwap,
        min_price=min_price,
        max_price=max_price,
        display_type=order_type or "PENDING",
        classification="entry_limit" if "LIMIT" in order_type else "trigger" if "STOP" in order_type else "other",
    )


def _execute_positions_orders(account: str, operation: str = "positions_orders") -> CanonicalResponse:
    response, failure = _call_ea("positions_orders", account)
    if failure is not None:
        return failure
    assert response is not None
    raw_positions = response.get("positions") or response.get("position_groups") or []
    raw_orders = response.get("pending_orders") or response.get("order_groups") or response.get("orders") or []
    if not isinstance(raw_positions, list):
        return _failure(operation, str(account).strip().upper(), "POSITIONS_MALFORMED", "EA positions payload was not a list.")
    if not isinstance(raw_orders, list):
        return _failure(operation, str(account).strip().upper(), "ORDERS_MALFORMED", "EA pending_orders payload was not a list.")
    try:
        positions = [_position_from_group(row) for row in raw_positions if isinstance(row, Mapping)]
        order_groups = [_order_group_from_row(row) for row in raw_orders if isinstance(row, Mapping)]
    except Exception as exc:  # noqa: BLE001
        return _failure(operation, str(account).strip().upper(), "SUMMARY_MALFORMED", sanitize_error_message(str(exc)))
    open_count = response.get("open_order_count")
    if open_count is None:
        open_count = _sum_counts(row for row in raw_orders if isinstance(row, Mapping))
    try:
        open_count_int = int(open_count)
    except (TypeError, ValueError):
        open_count_int = len(order_groups)
    return make_success(
        operation=operation,
        exchange=name,
        account=str(account).strip().upper(),
        positions=positions,
        open_order_count=open_count_int,
        order_groups=order_groups,
        data=dict(response),
    )


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _order_result_from_response(
    response: Mapping[str, Any],
    *,
    requested_symbol: str,
    requested_side: str,
    requested_volume: Decimal,
    requested_price: Decimal,
) -> CanonicalOrderResult:
    accepted_volume = response.get("accepted_volume", response.get("executed_volume", response.get("volume", requested_volume)))
    accepted_price = response.get("accepted_price", response.get("executed_price", response.get("price", requested_price)))
    order_ticket = response.get("order_ticket", response.get("order", response.get("ticket")))
    client_id = response.get("request_id")
    return CanonicalOrderResult(
        symbol=str(response.get("symbol") or requested_symbol),
        side=str(response.get("side") or requested_side).lower(),
        order_type=str(response.get("order_type") or "limit").lower(),
        requested_volume=_decimal_text(requested_volume),
        requested_price=_decimal_text(requested_price),
        submitted_volume=_decimal_text(accepted_volume),
        submitted_price=_decimal_text(accepted_price),
        verified=True,
        status="success",
        exchange_order_id=order_ticket,
        client_order_id=client_id,
    )


def _execute_new_order(request: Mapping[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "").strip()
    alias_upper, account_login = _alias_to_account(account)
    if account_login is None:
        return _failure("new_order", account, "ACCOUNT_NOT_CONFIGURED", "MetaTrader account alias is not configured.")

    symbol = str(request.get("symbol") or "").strip()
    side = str(request.get("side") or "").strip().lower()
    order_type = str(request.get("order_type") or request.get("type") or "").strip().lower()
    volume = _decimal_field(request.get("volume", request.get("size")))
    price = _decimal_field(request.get("price"))

    if not symbol:
        return _failure("new_order", alias_upper, "MISSING_SYMBOL", "Symbol is required.")
    if side not in {"buy", "sell"}:
        return _failure("new_order", alias_upper, "INVALID_SIDE", "Side must be buy or sell.")
    if order_type != "limit":
        return _failure("new_order", alias_upper, "UNSUPPORTED_ORDER_TYPE", "MetaTrader Phase B supports limit orders only.")
    if _bool_value(request.get("reduce_only", request.get("reduceOnly"))):
        return _failure("new_order", alias_upper, "UNSUPPORTED_PARAMETER", "reduce_only is not supported for MetaTrader Phase B.")
    if volume is None or volume <= 0:
        return _failure("new_order", alias_upper, "INVALID_VOLUME", "Volume must be a positive finite number.")
    if price is None or price <= 0:
        return _failure("new_order", alias_upper, "INVALID_PRICE", "Limit price must be a positive finite number.")

    request_id = _coerce_request_id(request.get("request_id") or request.get("client_order_id"))
    payload = {
        "request_id": request_id,
        "account": account_login,
        "action": "new_order",
        "symbol": symbol,
        "side": side.upper(),
        "order_type": "LIMIT",
        "volume": _json_number(volume),
        "price": _json_number(price),
    }
    response, failure = _call_ea_payload(
        "new_order",
        alias_upper,
        payload,
        ambiguous_on_transport_error=True,
    )
    if failure is not None:
        return failure
    assert response is not None
    order = _order_result_from_response(
        response,
        requested_symbol=symbol,
        requested_side=side,
        requested_volume=volume,
        requested_price=price,
    )
    data = dict(response)
    data.setdefault("magic", METATRADER_MAGIC_NUMBER)
    data.setdefault("comment", _short_comment(request_id))
    return make_success(
        operation="new_order",
        exchange=name,
        account=alias_upper,
        order=order,
        data=data,
    )


def _execute_bridge_read_action(request: Mapping[str, Any], action: str) -> CanonicalResponse:
    account = str(request.get("account") or "").strip()
    alias_upper, account_login = _alias_to_account(account)
    if account_login is None:
        return _failure(action, account, "ACCOUNT_NOT_CONFIGURED", "MetaTrader account alias is not configured.")
    payload: Dict[str, Any] = {
        "request_id": _coerce_request_id(request.get("request_id")),
        "account": account_login,
        "action": action,
    }
    if action in {"ticker", "candles"}:
        symbol = str(request.get("symbol") or "").strip()
        if not symbol:
            return _failure(action, alias_upper, "MISSING_SYMBOL", "Symbol is required.")
        payload["symbol"] = symbol
    if action == "candles":
        timeframe = str(request.get("timeframe") or "M1").strip().upper()
        count_raw = request.get("count", 10)
        try:
            count = int(count_raw)
        except (TypeError, ValueError):
            return _failure(action, alias_upper, "INVALID_COUNT", "Candle count must be an integer.")
        if count <= 0 or count > 500:
            return _failure(action, alias_upper, "INVALID_COUNT", "Candle count must be between 1 and 500.")
        payload["timeframe"] = timeframe
        payload["count"] = count
    response, failure = _call_ea_payload(action, alias_upper, payload)
    if failure is not None:
        return failure
    assert response is not None
    return make_success(operation=action, exchange=name, account=alias_upper, data=dict(response))


def _execute_cancel_order(request: Mapping[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "").strip()
    alias_upper, account_login = _alias_to_account(account)
    if account_login is None:
        return _failure("cancel_order", account, "ACCOUNT_NOT_CONFIGURED", "MetaTrader account alias is not configured.")
    raw_ticket = request.get("order_ticket", request.get("ticket"))
    try:
        ticket = int(str(raw_ticket).strip())
    except (TypeError, ValueError):
        return _failure("cancel_order", alias_upper, "INVALID_ORDER_TICKET", "order_ticket must be a positive integer.")
    if ticket <= 0:
        return _failure("cancel_order", alias_upper, "INVALID_ORDER_TICKET", "order_ticket must be a positive integer.")
    payload = {
        "request_id": _coerce_request_id(request.get("request_id")),
        "account": account_login,
        "action": "cancel_order",
        "order_ticket": ticket,
    }
    response, failure = _call_ea_payload(
        "cancel_order",
        alias_upper,
        payload,
        ambiguous_on_transport_error=True,
    )
    if failure is not None:
        return failure
    assert response is not None
    return make_success(operation="cancel_order", exchange=name, account=alias_upper, data=dict(response))


def _batch_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _compact_batch_data(data: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("orders", "tickets", "children", "results", "position_tickets"):
        rows = data.get(key)
        if isinstance(rows, list) and len(rows) > 20:
            data.pop(key, None)
    return data


def _batch_success(
    operation: str,
    alias: str,
    response: Mapping[str, Any],
    extra: Optional[Mapping[str, Any]] = None,
) -> CanonicalResponse:
    data = _compact_batch_data(dict(response))
    failures = data.get("failures")
    if isinstance(failures, list) and len(failures) > 20:
        data["failures"] = failures[:20]
        data["failures_omitted"] = len(failures) - 20
    extra = dict(extra or {})
    requested = _batch_int(data.get("requested", data.get("matched")))
    succeeded = _batch_int(data.get("succeeded"))
    failed = _batch_int(data.get("failed"))
    data.setdefault("requested", requested)
    data.setdefault("matched", requested)
    data.setdefault("succeeded", succeeded)
    data.setdefault("failed", failed)
    kwargs: Dict[str, Any] = {}
    if operation == "ladder":
        kwargs["ladder"] = CanonicalLadderResult(
            symbol=str(data.get("symbol") or extra.get("symbol") or ""),
            side=str(data.get("side") or extra.get("side") or "").lower(),
            distribution=str(extra.get("distribution") or data.get("distribution") or ""),
            requested_order_count=requested,
            submitted_order_count=succeeded,
            requested_volume=str(extra.get("requested_volume") or data.get("requested_volume") or ""),
            submitted_volume=str(data.get("submitted_volume") or extra.get("submitted_volume") or ""),
            batch_count=1,
            verified=failed == 0,
            partial=bool(failed and succeeded),
            status="partial" if failed else "success",
            accepted_child_count=succeeded,
        )
    elif operation in {"cancel_orders", "cancel_order_group"}:
        remaining = max(0, requested - succeeded)
        kwargs["cancel_group"] = CanonicalCancelGroupResult(
            symbol=str(data.get("symbol") or extra.get("symbol") or ""),
            side=str(data.get("side") or extra.get("side") or "").lower(),
            targeted_order_count=requested,
            cancelled_order_count=succeeded,
            confirmed_absent_count=succeeded,
            remaining_target_count=remaining,
            verified=failed == 0,
            partial=bool(failed and succeeded),
            status="partial" if failed else "success",
            requested_cancel_count=requested,
            verified_cancel_count=succeeded,
        )
    elif operation in {"close_position", "set_tp", "set_sl"}:
        raw_price = extra.get("price")
        if raw_price is None:
            raw_price = data.get("tp") if operation == "set_tp" else data.get("sl") if operation == "set_sl" else None
        removed = False
        if operation in {"set_tp", "set_sl"} and raw_price is not None:
            parsed = _decimal_field(raw_price)
            removed = parsed is not None and parsed == 0
        kwargs["position_action"] = CanonicalPositionActionResult(
            operation=operation,
            symbol=str(extra.get("symbol") or data.get("symbol") or ""),
            verified=failed == 0,
            price=None if raw_price is None else _decimal_text(raw_price),
            removed=removed if operation in {"set_tp", "set_sl"} else None,
            current_side=str(extra.get("side") or data.get("side") or "").lower() or None,
            message=f"matched {requested} · succeeded {succeeded} · failed {failed}",
        )
    return make_success(operation=operation, exchange=name, account=alias, data=data, **kwargs)


def _side_value(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if text in {"buy", "long"}:
        return "BUY"
    if text in {"sell", "short"}:
        return "SELL"
    return None


def _order_type_value(value: Any) -> Optional[str]:
    text = str(value or "limit").strip().upper().replace("-", "_").replace(" ", "_")
    if text == "LIMIT":
        return "LIMIT"
    if text in {"BUY_LIMIT", "SELL_LIMIT"}:
        return text
    return None


def _positive_decimal_or_failure(operation: str, alias: str, value: Any, code: str, label: str) -> Tuple[Optional[Decimal], Optional[CanonicalResponse]]:
    parsed = _decimal_field(value)
    if parsed is None or parsed <= 0:
        return None, _failure(operation, alias, code, f"{label} must be a positive finite number.")
    return parsed, None


def _nonnegative_decimal_or_failure(operation: str, alias: str, value: Any, code: str, label: str) -> Tuple[Optional[Decimal], Optional[CanonicalResponse]]:
    parsed = _decimal_field(value)
    if parsed is None or parsed < 0:
        return None, _failure(operation, alias, code, f"{label} must be a non-negative finite number.")
    return parsed, None


def _execute_ladder(request: Mapping[str, Any]) -> CanonicalResponse:
    operation = "ladder"
    account = str(request.get("account") or "").strip()
    alias_upper, account_login = _alias_to_account(account)
    if account_login is None:
        return _failure(operation, account, "ACCOUNT_NOT_CONFIGURED", "MetaTrader account alias is not configured.")
    symbol = str(request.get("symbol") or "").strip()
    side = _side_value(request.get("side"))
    order_type = str(request.get("order_type") or request.get("type") or "limit").strip().lower()
    if not symbol:
        return _failure(operation, alias_upper, "MISSING_SYMBOL", "Symbol is required.")
    if side is None:
        return _failure(operation, alias_upper, "INVALID_SIDE", "Side must be buy or sell.")
    if order_type != "limit":
        return _failure(operation, alias_upper, "UNSUPPORTED_ORDER_TYPE", "MetaTrader ladder supports limit orders only.")
    try:
        count = int(str(request.get("order_count") or request.get("count") or "0").strip())
    except (TypeError, ValueError):
        return _failure(operation, alias_upper, "INVALID_LADDER", "order_count must be a positive integer.")
    total, failure = _positive_decimal_or_failure(operation, alias_upper, request.get("total_volume", request.get("volume")), "INVALID_VOLUME", "total_volume")
    if failure is not None:
        return failure
    start, failure = _positive_decimal_or_failure(operation, alias_upper, request.get("start_price"), "INVALID_PRICE", "start_price")
    if failure is not None:
        return failure
    end, failure = _positive_decimal_or_failure(operation, alias_upper, request.get("end_price"), "INVALID_PRICE", "end_price")
    if failure is not None:
        return failure
    size_increment = _decimal_field(request.get("size_increment", request.get("volume_step", "0.01")))
    price_increment = _decimal_field(request.get("price_increment", request.get("tick_size", "0.01")))
    if count <= 0 or size_increment is None or size_increment <= 0 or price_increment is None or price_increment <= 0:
        return _failure(operation, alias_upper, "INVALID_LADDER", "order_count and increments must be positive.")
    distribution = str(request.get("distribution") or "uniform").strip().lower().replace(" ", "_")
    try:
        children, _submitted, _vwap = build_ladder_children(
            side=side.lower(),
            distribution=distribution,
            order_count=count,
            total_volume=total,
            start_price=start,
            end_price=end,
            size_increment=size_increment,
            price_increment=price_increment,
        )
    except ValueError as exc:
        code = str(exc) or "INVALID_LADDER"
        if code in {"INSUFFICIENT_VOLUME_FOR_ORDER_COUNT", "INVALID_VOLUME"}:
            return _failure(operation, alias_upper, "INVALID_VOLUME", sanitize_error_message(code))
        return _failure(operation, alias_upper, "INVALID_LADDER", sanitize_error_message(code))
    orders = []
    for child in children:
        price = _decimal_field(child.get("price"))
        volume = _decimal_field(child.get("size"))
        if price is None or price <= 0 or volume is None or volume <= 0:
            return _failure(operation, alias_upper, "INVALID_LADDER", "Calculated child order was invalid.")
        orders.append({"price": _json_number(price), "volume": _json_number(volume)})
    if not orders:
        return _failure(operation, alias_upper, "INVALID_LADDER", "Ladder must contain at least one child order.")
    payload = {
        "request_id": _coerce_request_id(request.get("request_id")),
        "account": account_login,
        "action": operation,
        "symbol": symbol,
        "side": side,
        "order_type": "LIMIT",
        "orders": orders,
    }
    response, failure = _call_ea_payload(operation, alias_upper, payload, ambiguous_on_transport_error=True)
    if failure is not None:
        return failure
    assert response is not None
    return _batch_success(
        operation,
        alias_upper,
        response,
        extra={
            "symbol": symbol,
            "side": side,
            "distribution": distribution,
            "requested_volume": _decimal_text(total),
        },
    )


def _execute_grouped_cancel(request: Mapping[str, Any]) -> CanonicalResponse:
    operation = str(request.get("operation") or "cancel_orders").strip() or "cancel_orders"
    account = str(request.get("account") or "").strip()
    alias_upper, account_login = _alias_to_account(account)
    if account_login is None:
        return _failure(operation, account, "ACCOUNT_NOT_CONFIGURED", "MetaTrader account alias is not configured.")
    symbol = str(request.get("symbol") or "").strip()
    if not symbol:
        return _failure(operation, alias_upper, "MISSING_SYMBOL", "Symbol is required.")
    payload: Dict[str, Any] = {
        "request_id": _coerce_request_id(request.get("request_id")),
        "account": account_login,
        "action": "cancel_orders",
        "symbol": symbol,
    }
    if str(request.get("side") or "").strip():
        side = _side_value(request.get("side"))
        if side is None:
            return _failure(operation, alias_upper, "INVALID_SIDE", "Side must be buy or sell.")
        payload["side"] = side
    raw_type = str(request.get("order_type") or request.get("type") or request.get("display_type") or "").strip()
    if raw_type:
        order_type = _order_type_value(raw_type)
        if order_type is None:
            return _failure(operation, alias_upper, "UNSUPPORTED_ORDER_TYPE", "Grouped cancel supports limit pending orders only.")
        payload["order_type"] = order_type
    response, failure = _call_ea_payload("cancel_orders", alias_upper, payload, ambiguous_on_transport_error=True)
    if failure is not None:
        return failure
    assert response is not None
    return _batch_success(operation, alias_upper, response, extra={"symbol": symbol, "side": payload.get("side")})


def _execute_grouped_position_action(request: Mapping[str, Any], operation: str) -> CanonicalResponse:
    account = str(request.get("account") or "").strip()
    alias_upper, account_login = _alias_to_account(account)
    if account_login is None:
        return _failure(operation, account, "ACCOUNT_NOT_CONFIGURED", "MetaTrader account alias is not configured.")
    symbol = str(request.get("symbol") or "").strip()
    side = _side_value(request.get("side") or request.get("position_side"))
    if not symbol:
        return _failure(operation, alias_upper, "MISSING_SYMBOL", "Symbol is required.")
    if side is None:
        return _failure(operation, alias_upper, "INVALID_SIDE", "Side must be buy or sell.")
    payload: Dict[str, Any] = {
        "request_id": _coerce_request_id(request.get("request_id")),
        "account": account_login,
        "action": operation,
        "symbol": symbol,
        "side": side,
    }
    if operation in {"set_tp", "set_sl"}:
        raw_price = request.get("price")
        if raw_price is None:
            raw_price = request.get("tp") if operation == "set_tp" else request.get("sl")
        price, failure = _nonnegative_decimal_or_failure(operation, alias_upper, raw_price, "INVALID_PRICE", "price")
        if failure is not None:
            return failure
        if operation == "set_tp":
            payload["tp"] = _json_number(price)
            payload["preserve_sl"] = True
        else:
            payload["sl"] = _json_number(price)
            payload["preserve_tp"] = True
    response, failure = _call_ea_payload(operation, alias_upper, payload, ambiguous_on_transport_error=True)
    if failure is not None:
        return failure
    assert response is not None
    extra: Dict[str, Any] = {"symbol": symbol, "side": side}
    if operation in {"set_tp", "set_sl"}:
        extra["price"] = payload.get("tp") if operation == "set_tp" else payload.get("sl")
    return _batch_success(operation, alias_upper, response, extra=extra)


def _symbol_records(raw: Any) -> List[Dict[str, Any]]:
    records: List[Any]
    if isinstance(raw, dict):
        records = raw.get("symbols") or raw.get("instruments") or []
    elif isinstance(raw, list):
        records = raw
    else:
        records = []
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in records:
        if isinstance(item, str):
            symbol = item.strip()
            row: Dict[str, Any] = {"symbol": symbol}
        elif isinstance(item, Mapping):
            symbol = str(item.get("symbol") or item.get("name") or "").strip()
            row = dict(item)
            row["symbol"] = symbol
        else:
            continue
        key = symbol.upper()
        if not symbol or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _ticker_increments(ticker: Mapping[str, Any]) -> Dict[str, str]:
    tick = _decimal_text(ticker.get("tick_size", ticker.get("point", ticker.get("price_increment"))))
    step = _decimal_text(ticker.get("volume_step", ticker.get("size_increment", ticker.get("lot_step"))))
    minimum = _decimal_text(ticker.get("volume_min", ticker.get("minimum_size", ticker.get("lot_min"))))
    meta: Dict[str, str] = {}
    if tick and tick != "0":
        meta["price_increment"] = tick
    if step and step != "0":
        meta["size_increment"] = step
    if minimum and minimum != "0":
        meta["minimum_size"] = minimum
    return meta


def _ticker_last_price(ticker: Mapping[str, Any]) -> Optional[str]:
    for key in ("last", "mark", "bid", "ask"):
        parsed = _decimal_field(ticker.get(key))
        if parsed is not None and parsed > 0:
            return _decimal_text(parsed)
    bid = _decimal_field(ticker.get("bid"))
    ask = _decimal_field(ticker.get("ask"))
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return _decimal_text((bid + ask) / 2)
    return None


def _execute_list_instruments(request: Mapping[str, Any]) -> CanonicalResponse:
    response = _execute_bridge_read_action(request, "symbols")
    if not response.success:
        return response
    data = dict(response.data or {})
    items = []
    for row in _symbol_records(data.get("symbols") or data):
        symbol = str(row.get("symbol") or "").strip()
        items.append(
            {
                "symbol": symbol,
                "native_symbol": symbol,
                "display_name": symbol,
                **_ticker_increments(row),
            }
        )
    return make_success(
        operation="list_instruments",
        exchange=name,
        account=str(request.get("account") or "").strip().upper(),
        data={"instruments": items, "count": len(items)},
    )


def _execute_resolve_instrument(request: Mapping[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "").strip()
    requested = str(request.get("symbol") or request.get("requested_symbol") or "").strip()
    alias_upper, account_login = _alias_to_account(account)
    if account_login is None:
        return _failure("resolve_instrument", account, "ACCOUNT_NOT_CONFIGURED", "MetaTrader account alias is not configured.")
    if not requested:
        return _failure("resolve_instrument", alias_upper, "MISSING_SYMBOL", "Symbol is required.")
    ticker_resp = _execute_bridge_read_action({"account": account, "symbol": requested, "request_id": request.get("request_id")}, "ticker")
    if not ticker_resp.success:
        return _failure("resolve_instrument", alias_upper, "INSTRUMENT_NOT_FOUND", f"Instrument {requested!r} was not found.")
    ticker = dict(ticker_resp.data or {})
    native = str(ticker.get("symbol") or requested).strip() or requested
    meta = _ticker_increments(ticker)
    instrument = CanonicalInstrument(
        requested_symbol=requested,
        symbol=native,
        display_name=native,
        price_increment=meta.get("price_increment"),
        size_increment=meta.get("size_increment"),
        minimum_size=meta.get("minimum_size"),
    )
    return make_success(operation="resolve_instrument", exchange=name, account=alias_upper, instrument=instrument, data=dict(ticker))


def _execute_market_price(request: Mapping[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "").strip()
    requested = str(request.get("symbol") or "").strip()
    alias_upper, _account_login = _alias_to_account(account)
    ticker_resp = _execute_bridge_read_action(request, "ticker")
    if not ticker_resp.success:
        return ticker_resp
    ticker = dict(ticker_resp.data or {})
    native = str(ticker.get("symbol") or requested).strip() or requested
    meta = _ticker_increments(ticker)
    price = _ticker_last_price(ticker)
    market_price = CanonicalMarketPrice(
        requested_symbol=requested or native,
        market=native,
        mark_price=price,
        price=price,
        last_external_price=price,
    )
    data = dict(ticker)
    data.setdefault("symbol", native)
    data.update(meta)
    return make_success(operation="market_price", exchange=name, account=alias_upper, market_price=market_price, data=data)


def execute(request: Mapping[str, Any]) -> CanonicalResponse:
    if not isinstance(request, Mapping):
        return _failure("", "", "INVALID_REQUEST", "Request must be a dict.")
    operation = str(request.get("operation") or "").strip()
    account = str(request.get("account") or "").strip()
    if not operation:
        return _failure("", account, "INVALID_REQUEST", "Missing 'operation'.")
    if not account:
        return _failure(operation, account, "MISSING_ACCOUNT", "Account alias is required.")
    if operation in _MUTATING_OPS:
        return _readonly_failure(operation, account)
    if operation == "ping":
        return _execute_ping(account)
    if operation == "balance":
        return _execute_balance(account)
    if operation == "positions_orders":
        return _execute_positions_orders(account)
    if operation == "positions_management":
        return _execute_positions_orders(account, operation="positions_management")
    if operation == "new_order":
        return _execute_new_order(request)
    if operation in {"symbols", "ticker", "candles"}:
        return _execute_bridge_read_action(request, operation)
    if operation == "list_instruments":
        return _execute_list_instruments(request)
    if operation == "resolve_instrument":
        return _execute_resolve_instrument(request)
    if operation == "market_price":
        return _execute_market_price(request)
    if operation == "cancel_order":
        return _execute_cancel_order(request)
    if operation == "ladder":
        return _execute_ladder(request)
    if operation in {"cancel_orders", "cancel_order_group"}:
        return _execute_grouped_cancel(request)
    if operation in {"close_position", "set_tp", "set_sl"}:
        return _execute_grouped_position_action(request, operation)
    if operation in _READ_OPS:
        return _failure(operation, account, "NOT_IMPLEMENTED", f"MetaTrader read operation {operation!r} is not implemented.")
    return _failure(operation, account, "NOT_IMPLEMENTED", f"MetaTrader does not implement {operation!r} in Phase B.")
