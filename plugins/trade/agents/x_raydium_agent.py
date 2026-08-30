"""Raydium exchange agent.

Raydium Perps is a white-label deployment on top of Orderly Network.
This agent keeps all Raydium/Orderly-specific credential discovery,
authenticated request signing, response parsing, and normalization local
to the exchange module so TradeDesk and the Telegram wizard remain fully
exchange-agnostic.

Configured accounts are discovered from either the live environment or
~/.hermes/.env using complete credential triples:

- RAYDIUM_<ACCOUNT>_ACCOUNT_ID
- RAYDIUM_<ACCOUNT>_API_KEY
- RAYDIUM_<ACCOUNT>_SECRET_KEY
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import re
import time
import uuid
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..canonical import (
    CanonicalCancelGroupResult,
    CanonicalInstrument,
    CanonicalMarketPrice,
    CanonicalLadderResult,
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

name = "raydium"
DEFAULT_API_BASE = "https://api.orderly.org"
API_TIMEOUT_SECONDS = 20
_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {ch: index for index, ch in enumerate(_BASE58_ALPHABET)}


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
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1].replace("\\\"", '"').replace("\\\\", "\\")
        values[key] = value
    return values


def _combined_env(prefix: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith(prefix):
            values[key] = (value or "").strip()
    for key, value in _load_dotenv_values(_hermes_home() / ".env").items():
        if key.startswith(prefix):
            values.setdefault(key, (value or "").strip())
    return values


def _discover_accounts() -> List[str]:
    env_values = _combined_env("RAYDIUM_")
    grouped: Dict[str, Dict[str, str]] = {}
    for key, value in env_values.items():
        if not value or not key.startswith("RAYDIUM_"):
            continue
        remainder = key[len("RAYDIUM_"):]
        field = None
        alias = ""
        for suffix in ("_ACCOUNT_ID", "_API_KEY", "_SECRET_KEY"):
            if remainder.endswith(suffix):
                alias = remainder[: -len(suffix)]
                field = suffix[1:]
                break
        if field is None or not alias or not _ALIAS_PATTERN.match(alias):
            continue
        grouped.setdefault(alias, {})[field] = value
    valid: List[str] = []
    for alias, fields in grouped.items():
        if fields.get("ACCOUNT_ID") and fields.get("API_KEY") and fields.get("SECRET_KEY"):
            valid.append(alias.lower())
    return sorted(valid)


def list_accounts() -> List[str]:
    return _discover_accounts()


def _lookup_credentials(account: str) -> Optional[Dict[str, str]]:
    alias = str(account or "").strip().upper()
    if not alias or not _ALIAS_PATTERN.match(alias):
        return None
    env_values = _combined_env("RAYDIUM_")
    account_id = env_values.get(f"RAYDIUM_{alias}_ACCOUNT_ID", "").strip()
    api_key = env_values.get(f"RAYDIUM_{alias}_API_KEY", "").strip()
    secret_key = env_values.get(f"RAYDIUM_{alias}_SECRET_KEY", "").strip()
    if not account_id or not api_key or not secret_key:
        return None
    if not api_key.startswith("ed25519:"):
        api_key = f"ed25519:{api_key}"
    return {
        "account": alias.lower(),
        "account_id": account_id,
        "api_key": api_key,
        "secret_key": secret_key,
        "base_url": DEFAULT_API_BASE,
    }


def capabilities() -> List[str]:
    return [
        "balance",
        "positions_orders",
        "new_order",
        "ladder",
        "cancel_orders",
        "positions_management",
        "resolve_instrument",
        "list_instruments",
        "market_price",
    ]


def _base58_decode(value: str) -> bytes:
    text = str(value or "").strip()
    if text.lower().startswith("ed25519:"):
        text = text.split(":", 1)[1].strip()
    if not text:
        raise ValueError("empty base58 value")
    number = 0
    for char in text:
        if char not in _BASE58_INDEX:
            raise ValueError("invalid base58 character")
        number = number * 58 + _BASE58_INDEX[char]
    decoded = b"" if number == 0 else number.to_bytes((number.bit_length() + 7) // 8, "big")
    leading_zeros = len(text) - len(text.lstrip("1"))
    return b"\x00" * leading_zeros + decoded


def _private_key_from_credentials(credentials: Dict[str, str]) -> Ed25519PrivateKey:
    secret_bytes = _base58_decode(credentials["secret_key"])
    if len(secret_bytes) < 32:
        raise ValueError("RAYDIUM secret key is too short")
    return Ed25519PrivateKey.from_private_bytes(secret_bytes[:32])


def _private_get(credentials: Dict[str, str], path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    query = urlencode(params or {}, doseq=True)
    path_with_query = path if not query else f"{path}?{query}"
    url = f"{credentials['base_url'].rstrip('/')}{path_with_query}"
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}GET{path_with_query}"
    private_key = _private_key_from_credentials(credentials)
    signature = base64.urlsafe_b64encode(private_key.sign(message.encode("utf-8"))).decode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "orderly-account-id": credentials["account_id"],
        "orderly-key": credentials["api_key"],
        "orderly-timestamp": timestamp,
        "orderly-signature": signature,
    }
    response = requests.get(url, headers=headers, timeout=API_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Raydium response was not an object")
    if payload.get("success") is False:
        raise RuntimeError(str(payload.get("message") or payload.get("msg") or "Raydium request failed"))
    return payload


def _serialize_payload(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=False)


def _signed_post(credentials: Dict[str, str], path: str, payload: Any) -> Dict[str, Any]:
    body = _serialize_payload(payload)
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}POST{path}{body}"
    private_key = _private_key_from_credentials(credentials)
    signature = base64.urlsafe_b64encode(private_key.sign(message.encode("utf-8"))).decode("utf-8")
    url = f"{credentials['base_url'].rstrip('/')}{path}"
    headers = {
        "Content-Type": "application/json",
        "orderly-account-id": credentials["account_id"],
        "orderly-key": credentials["api_key"],
        "orderly-timestamp": timestamp,
        "orderly-signature": signature,
    }
    response = requests.post(url, headers=headers, data=body, timeout=API_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload_obj = response.json()
    if not isinstance(payload_obj, dict):
        raise RuntimeError("Raydium response was not an object")
    if payload_obj.get("success") is False:
        raise RuntimeError(str(payload_obj.get("message") or payload_obj.get("msg") or "Raydium request failed"))
    return payload_obj


def _signed_delete(credentials: Dict[str, str], path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    query = urlencode(params or {}, doseq=True)
    path_with_query = path if not query else f"{path}?{query}"
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}DELETE{path_with_query}"
    private_key = _private_key_from_credentials(credentials)
    signature = base64.urlsafe_b64encode(private_key.sign(message.encode("utf-8"))).decode("utf-8")
    url = f"{credentials['base_url'].rstrip('/')}{path_with_query}"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "orderly-account-id": credentials["account_id"],
        "orderly-key": credentials["api_key"],
        "orderly-timestamp": timestamp,
        "orderly-signature": signature,
    }
    response = requests.delete(url, headers=headers, timeout=API_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload_obj = response.json()
    if not isinstance(payload_obj, dict):
        raise RuntimeError("Raydium response was not an object")
    if payload_obj.get("success") is False:
        raise RuntimeError(str(payload_obj.get("message") or payload_obj.get("msg") or "Raydium request failed"))
    return payload_obj


def _quantize_2(value: Any) -> str:
    decimal_value = Decimal(str(value or "0"))
    return format(decimal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _decimal_or_zero(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")


def _decimal_text(value: Any) -> str:
    decimal_value = _decimal_or_zero(value)
    rendered = format(decimal_value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".") or "0"
    return rendered


def _decimal_places(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        # Use Decimal to handle both "0.00001" and "1e-05" without losing
        # significant precision (a naive str split strips trailing zeros that
        # are part of the actual precision, e.g. "0.10" -> 1 instead of 2).
        decimal_value = Decimal(text)
    except Exception:
        return 0
    sign, digits, exponent = decimal_value.as_tuple()
    exponent = int(exponent)
    if exponent >= 0:
        return max(0, -exponent)
    return -exponent


def _format_decimal_places(value: Decimal, places: int) -> str:
    quantum = Decimal("1").scaleb(-places)
    quantized = value.quantize(quantum, rounding=ROUND_HALF_UP)
    if places <= 0:
        return format(quantized, ".0f")
    return format(quantized, f".{places}f")


def _quantize_down(value: Decimal, places: int) -> Decimal:
    quantum = Decimal("1").scaleb(-max(0, places))
    return value.quantize(quantum, rounding=ROUND_DOWN)


def _symbol_from_orderly(symbol: Any) -> str:
    text = str(symbol or "").strip().upper()
    if text.startswith("PERP_") and text.endswith("_USDC"):
        return text[len("PERP_") : -len("_USDC")]
    return text


def _orderly_symbol(symbol: Any) -> str:
    text = str(symbol or "").strip().upper()
    if text.startswith("PERP_"):
        return text
    if text and not text.endswith("_USDC"):
        return f"PERP_{text}_USDC"
    return text


def _tick_decimals(value: Any) -> int:
    return _decimal_places(value)


def _public_get(path: str) -> Dict[str, Any]:
    url = f"{DEFAULT_API_BASE.rstrip('/')}{path}"
    response = requests.get(url, timeout=API_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Raydium public response was not an object")
    if payload.get("success") is False:
        raise RuntimeError(str(payload.get("message") or payload.get("msg") or "Raydium public request failed"))
    return payload


def _fetch_symbol_rules(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    rules: Dict[str, Dict[str, Any]] = {}
    for symbol in sorted({str(item or '').strip().upper() for item in symbols if str(item or '').strip()}):
        orderly_symbol = _orderly_symbol(symbol)
        try:
            payload = _public_get(f"/v1/public/info/{orderly_symbol}")
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                continue
            rules[orderly_symbol] = {
                "symbol": _symbol_from_orderly(data.get("symbol") or orderly_symbol),
                "price_precision": _tick_decimals(data.get("quote_tick")),
                "size_precision": _tick_decimals(data.get("base_tick")),
            }
        except Exception as exc:  # noqa: BLE001
            logger.info("Raydium symbol rules lookup failed for %s: %s", orderly_symbol, exc)
    return rules


def _resolve_symbol_metadata(symbol: Any) -> Dict[str, Any]:
    orderly_symbol = _orderly_symbol(symbol)
    payload = _public_get(f"/v1/public/info/{orderly_symbol}")
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("Raydium symbol metadata was unavailable")
    mark_price = _decimal_or_zero(data.get("mark_price") if data.get("mark_price") is not None else data.get("markPrice"))
    if mark_price <= 0:
        try:
            futures_payload = _public_get(f"/v1/public/futures/{orderly_symbol}")
            futures_data = futures_payload.get("data") if isinstance(futures_payload, dict) else None
            if isinstance(futures_data, dict):
                mark_price = _decimal_or_zero(
                    futures_data.get("mark_price")
                    if futures_data.get("mark_price") is not None
                    else futures_data.get("markPrice")
                    if futures_data.get("markPrice") is not None
                    else futures_data.get("index_price")
                    if futures_data.get("index_price") is not None
                    else futures_data.get("indexPrice")
                )
        except Exception as exc:  # noqa: BLE001
            logger.info("Raydium futures price lookup failed for %s: %s", orderly_symbol, exc)
    return {
        "symbol": orderly_symbol,
        "display_symbol": _symbol_from_orderly(data.get("symbol") or orderly_symbol),
        "price_precision": _tick_decimals(data.get("quote_tick")),
        "size_precision": _tick_decimals(data.get("base_tick")),
        "min_quantity": _decimal_or_zero(data.get("base_min") if data.get("base_min") is not None else data.get("min_quantity")),
        "min_notional": _decimal_or_zero(data.get("min_notional")),
        "price_scope": _decimal_or_zero(data.get("price_scope")),
        "mark_price": mark_price,
    }


def _resolve_request_value(request: Dict[str, Any], name: str, *, aliases: Optional[List[str]] = None) -> Any:
    raw_structured = request.get("structured_request")
    structured: Dict[str, Any] = raw_structured if isinstance(raw_structured, dict) else {}
    keys = [name] + list(aliases or [])
    chosen = None
    for key in keys:
        top = request.get(key)
        nested = structured.get(key)
        if top not in {None, ""} and nested not in {None, ""} and str(top) != str(nested):
            raise ValueError(f"Conflicting values for '{name}'")
        value = top if top not in {None, ""} else nested
        if value not in {None, ""}:
            chosen = value
            break
    return chosen


def _normalize_positions(rows: Any, symbol_rules: Optional[Dict[str, Dict[str, Any]]] = None) -> List[CanonicalPosition]:
    if not isinstance(rows, list):
        return []
    positions: List[CanonicalPosition] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        size = _decimal_or_zero(row.get("position_qty"))
        if size == 0:
            continue
        pnl_value = _decimal_or_zero(row.get("unsettled_pnl")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        pnl_text = _decimal_text(pnl_value)
        if pnl_value > 0:
            pnl_text = f"+{pnl_text}"
        orderly_symbol = _orderly_symbol(row.get("symbol"))
        rule = (symbol_rules or {}).get(orderly_symbol, {})
        symbol = str(rule.get("symbol") or _symbol_from_orderly(row.get("symbol"))).strip().upper()
        size_precision = int(rule.get("size_precision") or _decimal_places(row.get("position_qty")))
        price_precision = int(rule.get("price_precision") or _decimal_places(row.get("average_open_price")))
        positions.append(CanonicalPosition(
            symbol=symbol,
            side="long" if size > 0 else "short",
            size=_format_decimal_places(abs(size), size_precision),
            entry_price=_format_decimal_places(_decimal_or_zero(row.get("average_open_price")), price_precision),
            pnl=pnl_text,
        ))
    positions.sort(key=lambda item: (item.symbol, item.side))
    return positions


def _remaining_order_quantity(row: Dict[str, Any]) -> Decimal:
    quantity = _decimal_or_zero(
        row.get("leaves_qty")
        if row.get("leaves_qty") is not None
        else row.get("visible_quantity")
        if row.get("visible_quantity") is not None
        else row.get("quantity")
        if row.get("quantity") is not None
        else row.get("order_quantity")
    )
    executed = _decimal_or_zero(
        row.get("total_executed_quantity")
        if row.get("total_executed_quantity") is not None
        else row.get("executed")
        if row.get("executed") is not None
        else 0
    )
    return quantity if row.get("leaves_qty") is not None else max(Decimal("0"), quantity - executed)


def _aggregate_orders(rows: Any, symbol_rules: Optional[Dict[str, Dict[str, Any]]] = None) -> tuple[int, List[CanonicalOrderGroup]]:
    if not isinstance(rows, list):
        return (0, [])
    grouped: Dict[tuple[str, str], Dict[str, Any]] = {}
    open_count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").strip().upper()
        if status not in {"NEW", "PARTIAL_FILLED", "INCOMPLETE"}:
            continue
        remaining = _remaining_order_quantity(row)
        if remaining <= 0:
            continue
        orderly_symbol = _orderly_symbol(row.get("symbol"))
        rule = (symbol_rules or {}).get(orderly_symbol, {})
        symbol = str(rule.get("symbol") or _symbol_from_orderly(row.get("symbol"))).strip().upper()
        side = str(row.get("side") or "").strip().lower()
        if side not in {"buy", "sell"}:
            continue
        price = _decimal_or_zero(row.get("order_price") if row.get("order_price") is not None else row.get("price"))
        open_count += 1
        key = (symbol, side)
        group = grouped.setdefault(key, {
            "symbol": symbol,
            "side": side,
            "count": 0,
            "size": Decimal("0"),
            "notional": Decimal("0"),
            "min_price": None,
            "max_price": None,
            "price_precision": 0,
            "size_precision": int(rule.get("size_precision") or 0),
        })
        group["count"] += 1
        group["size"] += remaining
        group["notional"] += remaining * price
        group["price_precision"] = max(int(group.get("price_precision") or 0), int(rule.get("price_precision") or _decimal_places(price)))
        group["min_price"] = price if group["min_price"] is None or price < group["min_price"] else group["min_price"]
        group["max_price"] = price if group["max_price"] is None or price > group["max_price"] else group["max_price"]
    groups: List[CanonicalOrderGroup] = []
    for group in grouped.values():
        total_size: Decimal = group["size"]
        vwap = (group["notional"] / total_size) if total_size else Decimal("0")
        groups.append(CanonicalOrderGroup(
            symbol=group["symbol"],
            side=group["side"],
            order_count=int(group["count"]),
            total_size=_format_decimal_places(total_size, int(group.get("size_precision") or _decimal_places(total_size))),
            vwap=_format_decimal_places(vwap, int(group.get("price_precision") or 0)),
            min_price=_decimal_text(group["min_price"]),
            max_price=_decimal_text(group["max_price"]),
        ))
    groups.sort(key=lambda item: (item.symbol, item.side))
    return (open_count, groups)


def _extract_usdc_holding(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    holding = data.get("holding") if isinstance(data, dict) else None
    if not isinstance(holding, list):
        return None
    for row in holding:
        if isinstance(row, dict) and str(row.get("token") or row.get("coin") or "").strip().upper() == "USDC":
            return row
    return None


def _balance(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="balance", exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Raydium account is not configured.")
    try:
        payload = _private_get(credentials, "/v1/client/holding")
        row = _extract_usdc_holding(payload)
        if row is None:
            raise RuntimeError("USDC holding row was not present")
        total = row.get("holding") or row.get("balance") or "0"
        withdrawable = row.get("available_balance") or row.get("available") or total
        margin_used = row.get("frozen_holding") or row.get("holding_locked") or "0"
        portfolio_summary = CanonicalPortfolioSummary(
            account_value=_quantize_2(total),
            withdrawable=_quantize_2(withdrawable),
            margin_used=_quantize_2(margin_used),
            total_position_value="0.00",
            unit="USDC",
        )
        return make_success(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            balance=normalize_balance(total, "USDC"),
            portfolio_summary=portfolio_summary,
            positions=[],
        )
    except Exception as exc:
        return make_failure(operation="balance", exchange=name, account=account, code="RAYDIUM_ERROR", message=sanitize_error_message(str(exc)))


def _fetch_open_orders(credentials: Dict[str, str]) -> List[Dict[str, Any]]:
    orders_payload = _private_get(credentials, "/v1/orders", {"status": "NEW", "size": 100})
    orders_rows = ((orders_payload.get("data") or {}).get("rows") if isinstance(orders_payload, dict) else None)
    if not isinstance(orders_rows, list):
        orders_rows = []
    meta = ((orders_payload.get("data") or {}).get("meta") if isinstance(orders_payload, dict) else None)
    total = int((meta or {}).get("total") or len(orders_rows) or 0) if isinstance(meta, dict) else len(orders_rows)
    page = 1
    while len(orders_rows) < total:
        page += 1
        page_payload = _private_get(credentials, "/v1/orders", {"status": "NEW", "size": 100, "page": page})
        page_rows = ((page_payload.get("data") or {}).get("rows") if isinstance(page_payload, dict) else None)
        if not isinstance(page_rows, list) or not page_rows:
            break
        orders_rows.extend(page_rows)
    return orders_rows


def _fetch_algo_orders(credentials: Dict[str, str], symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"status": "INCOMPLETE", "size": 100}
    if symbol:
        params["symbol"] = _orderly_symbol(symbol)
    payload = _private_get(credentials, "/v1/algo/orders", params)
    rows = ((payload.get("data") or {}).get("rows") if isinstance(payload, dict) else None)
    if not isinstance(rows, list):
        rows = []
    return rows


def _extract_protection_from_algo_orders(algo_orders: List[Dict[str, Any]], symbol: str, closing_side: str) -> Dict[str, List[Dict[str, Any]]]:
    result: Dict[str, List[Dict[str, Any]]] = {"tp": [], "sl": []}
    target_symbol = _orderly_symbol(symbol)
    target_side = closing_side.upper()
    for algo in algo_orders:
        if not isinstance(algo, dict):
            continue
        if _orderly_symbol(algo.get("symbol")) != target_symbol:
            continue
        children = algo.get("child_orders")
        if not isinstance(children, list):
            continue
        for child in children:
            if not isinstance(child, dict):
                continue
            if str(child.get("side") or "").strip().upper() != target_side:
                continue
            if not bool(child.get("reduce_only")):
                continue
            if str(child.get("type") or "").strip().upper() != "CLOSE_POSITION":
                continue
            algo_type = str(child.get("algo_type") or child.get("algoType") or "").strip().upper()
            if algo_type == "TAKE_PROFIT":
                result["tp"].append({"algo_order_id": algo.get("algo_order_id") or algo.get("order_id"), "trigger_price": child.get("trigger_price")})
            elif algo_type == "STOP_LOSS":
                result["sl"].append({"algo_order_id": algo.get("algo_order_id") or algo.get("order_id"), "trigger_price": child.get("trigger_price")})
    return result


def _overlay_position_protection(positions: List[CanonicalPosition], algo_orders: List[Dict[str, Any]]) -> List[CanonicalPosition]:
    updated: List[CanonicalPosition] = []
    for position in positions:
        closing_side = "SELL" if position.side == "long" else "BUY"
        protection = _extract_protection_from_algo_orders(algo_orders, position.symbol, closing_side)
        tp_orders = protection["tp"]
        sl_orders = protection["sl"]
        updated.append(CanonicalPosition(
            symbol=position.symbol,
            side=position.side,
            size=position.size,
            entry_price=position.entry_price,
            pnl=position.pnl,
            tp=str(tp_orders[0].get("trigger_price")) if tp_orders and tp_orders[0].get("trigger_price") is not None else None,
            sl=str(sl_orders[0].get("trigger_price")) if sl_orders and sl_orders[0].get("trigger_price") is not None else None,
            tp_count=len(tp_orders) or None,
            sl_count=len(sl_orders) or None,
        ))
    return updated


def _find_current_position(credentials: Dict[str, str], symbol: str) -> Optional[CanonicalPosition]:
    positions_payload = _private_get(credentials, "/v1/positions")
    positions_rows = ((positions_payload.get("data") or {}).get("rows") if isinstance(positions_payload, dict) else None)
    symbol_rules = _fetch_symbol_rules([symbol])
    positions = _normalize_positions(positions_rows, symbol_rules=symbol_rules)
    for position in positions:
        if position.symbol == str(symbol or "").strip().upper():
            return position
    return None


def _position_action_result(*, operation: str, symbol: str, verified: bool, price: Optional[str] = None, removed: Optional[bool] = None, status: str = "success", exchange_order_id: Optional[int] = None, current_side: Optional[str] = None, current_size: Optional[str] = None, message: Optional[str] = None) -> CanonicalPositionActionResult:
    return CanonicalPositionActionResult(
        operation=operation,
        symbol=symbol,
        verified=verified,
        price=price,
        removed=removed,
        status=status,
        exchange_order_id=exchange_order_id,
        current_side=current_side,
        current_size=current_size,
        message=message,
    )


def _numeric_trigger_price(price: Decimal, price_precision: int) -> int | float:
    quantized_price = _quantize_down(price, price_precision)
    return int(quantized_price) if quantized_price == quantized_price.to_integral_value() else float(quantized_price)


def _build_tp_sl_algo_payload(*, symbol: str, side: str, tp_price: Optional[Decimal], sl_price: Optional[Decimal], metadata: Dict[str, Any]) -> Dict[str, Any]:
    price_precision = int(metadata.get("price_precision") or 0)
    child_orders: List[Dict[str, Any]] = []
    if tp_price is not None and tp_price > 0:
        child_orders.append({
            "symbol": _orderly_symbol(symbol),
            "algo_type": "TAKE_PROFIT",
            "side": side.upper(),
            "type": "CLOSE_POSITION",
            "trigger_price": _numeric_trigger_price(tp_price, price_precision),
            "trigger_price_type": "MARK_PRICE",
            "reduce_only": True,
        })
    if sl_price is not None and sl_price > 0:
        child_orders.append({
            "symbol": _orderly_symbol(symbol),
            "algo_type": "STOP_LOSS",
            "side": side.upper(),
            "type": "CLOSE_POSITION",
            "trigger_price": _numeric_trigger_price(sl_price, price_precision),
            "trigger_price_type": "MARK_PRICE",
            "reduce_only": True,
        })
    return {
        "symbol": _orderly_symbol(symbol),
        "algo_type": "POSITIONAL_TP_SL",
        "trigger_price_type": "MARK_PRICE",
        "child_orders": child_orders,
    }


def _positions_orders(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="positions_orders", exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Raydium account is not configured.")
    try:
        positions_payload = _private_get(credentials, "/v1/positions")
        orders_rows = _fetch_open_orders(credentials)
        positions_rows = ((positions_payload.get("data") or {}).get("rows") if isinstance(positions_payload, dict) else None)
        symbols: List[str] = []
        if isinstance(positions_rows, list):
            symbols.extend(str(row.get("symbol") or "") for row in positions_rows if isinstance(row, dict))
        symbols.extend(str(row.get("symbol") or "") for row in orders_rows if isinstance(row, dict))
        symbol_rules = _fetch_symbol_rules(symbols)
        positions = _normalize_positions(positions_rows, symbol_rules=symbol_rules)
        open_order_count, order_groups = _aggregate_orders(orders_rows, symbol_rules=symbol_rules)
        return make_success(
            operation="positions_orders",
            exchange=name,
            account=credentials["account"],
            positions=positions,
            open_order_count=open_order_count,
            order_groups=order_groups,
        )
    except Exception as exc:
        return make_failure(operation="positions_orders", exchange=name, account=account, code="RAYDIUM_ERROR", message=sanitize_error_message(str(exc)))


def _verify_order(orders: List[Dict[str, Any]], *, symbol: str, side: str, submitted_price: str, submitted_volume: str) -> tuple[bool, Optional[int]]:
    target_symbol = _orderly_symbol(symbol)
    target_side = side.upper()
    for row in orders:
        if not isinstance(row, dict):
            continue
        if _orderly_symbol(row.get("symbol")) != target_symbol:
            continue
        if str(row.get("side") or "").strip().upper() != target_side:
            continue
        price = _decimal_text(row.get("order_price") if row.get("order_price") is not None else row.get("price"))
        remaining = _remaining_order_quantity(row)
        if price != _decimal_text(submitted_price):
            continue
        if _decimal_text(remaining) != _decimal_text(submitted_volume):
            continue
        try:
            return True, int(str(row.get("order_id") or 0))
        except Exception:
            return True, None
    return False, None


def _submit_order(credentials: Dict[str, str], metadata: Dict[str, Any], *, side: str, order_type: str, requested_volume: Decimal, requested_price: Decimal, reduce_only: bool = False, client_order_id: Optional[str] = None) -> Dict[str, Any]:
    price_precision = int(metadata.get("price_precision") or 0)
    size_precision = int(metadata.get("size_precision") or 0)
    order_type_upper = str(order_type or "").strip().upper()
    submitted_price = _quantize_down(requested_price, price_precision) if order_type_upper == "LIMIT" else Decimal("0")
    submitted_volume = _quantize_down(requested_volume, size_precision)
    min_quantity = _decimal_or_zero(metadata.get("min_quantity"))
    min_notional = _decimal_or_zero(metadata.get("min_notional"))
    if submitted_volume <= 0:
        raise ValueError("INVALID_VOLUME")
    if min_quantity > 0 and submitted_volume < min_quantity:
        raise ValueError("VOLUME_BELOW_MINIMUM")
    effective_price = submitted_price if order_type_upper == "LIMIT" else _decimal_or_zero(metadata.get("mark_price"))
    if min_notional > 0 and effective_price > 0 and (effective_price * submitted_volume) < min_notional:
        raise ValueError("NOTIONAL_BELOW_MINIMUM")
    mark_price = _decimal_or_zero(metadata.get("mark_price"))
    price_scope = _decimal_or_zero(metadata.get("price_scope"))
    if order_type_upper == "LIMIT" and mark_price > 0 and price_scope > 0:
        lower = mark_price * (Decimal("1") - price_scope)
        upper = mark_price * (Decimal("1") + price_scope)
        if submitted_price < lower or submitted_price > upper:
            raise ValueError("PRICE_OUTSIDE_SCOPE")
    payload = {
        "symbol": metadata["symbol"],
        "client_order_id": client_order_id or f"raydium-{uuid.uuid4().hex[:16]}",
        "side": side.upper(),
        "order_type": order_type_upper,
        "order_quantity": _format_decimal_places(submitted_volume, size_precision),
    }
    if order_type_upper == "LIMIT":
        payload["order_price"] = _format_decimal_places(submitted_price, price_precision)
    if reduce_only:
        payload["reduce_only"] = True
    response = _signed_post(credentials, "/v1/order", payload)
    data = response.get("data") if isinstance(response, dict) else None
    exchange_order_id = None
    if isinstance(data, dict):
        try:
            exchange_order_id = int(str(data.get("order_id") or 0))
        except Exception:
            exchange_order_id = None
    return {
        "payload": payload,
        "submitted_price": str(payload.get("order_price") or "0"),
        "submitted_volume": payload["order_quantity"],
        "exchange_order_id": exchange_order_id,
        "response": response,
    }


def _ladder_distribution_weights(order_count: int, distribution: str) -> List[Decimal]:
    if order_count <= 0:
        return []
    distribution_key = str(distribution or "").strip().lower()
    if distribution_key == "uniform":
        return [Decimal("1")] * order_count
    if distribution_key != "half_gaussian":
        raise ValueError("UNSUPPORTED_DISTRIBUTION")
    if order_count == 1:
        return [Decimal("1")]
    weights: List[Decimal] = []
    span = Decimal(order_count - 1)
    for index in range(order_count):
        z = Decimal("3") * (span - Decimal(index)) / span
        weights.append(Decimal(str(math.exp(-(float(z) ** 2) / 2.0))))
    return weights


def _build_ladder_prices(start_price: Decimal, end_price: Decimal, order_count: int, price_decimals: int) -> List[Decimal]:
    if order_count <= 0:
        return []
    if order_count == 1:
        return [_quantize_down((start_price + end_price) / Decimal("2"), price_decimals)]
    step = (end_price - start_price) / Decimal(order_count - 1)
    prices = [_quantize_down(start_price + (step * Decimal(index)), price_decimals) for index in range(order_count)]
    if start_price <= end_price:
        for index in range(1, len(prices)):
            if prices[index] < prices[index - 1]:
                prices[index] = prices[index - 1]
    else:
        for index in range(1, len(prices)):
            if prices[index] > prices[index - 1]:
                prices[index] = prices[index - 1]
    return prices


def _allocate_ladder_sizes(total_volume: Decimal, order_count: int, size_decimals: int, distribution: str) -> tuple[List[Decimal], Decimal]:
    increment = Decimal("1").scaleb(-max(0, size_decimals))
    total_units = int((total_volume / increment).to_integral_value(rounding=ROUND_HALF_UP))
    if total_units < order_count:
        raise ValueError("INSUFFICIENT_VOLUME_FOR_ORDER_COUNT")
    weights = _ladder_distribution_weights(order_count, distribution)
    total_weight = sum(weights, Decimal("0"))
    if total_weight <= 0:
        raise ValueError("INVALID_DISTRIBUTION")
    raw_units = [Decimal(total_units) * weight / total_weight for weight in weights]
    base_units = [int(unit.to_integral_value(rounding=ROUND_DOWN)) for unit in raw_units]
    residual = total_units - sum(base_units)
    remainders = [raw_units[index] - Decimal(base_units[index]) for index in range(order_count)]
    allocation = list(base_units)
    if residual > 0:
        order_indices = sorted(range(order_count), key=lambda index: (remainders[index], -index), reverse=True)
        for index in order_indices[:residual]:
            allocation[index] += 1
    sizes = [Decimal(units) * increment for units in allocation]
    return sizes, Decimal(total_units) * increment


def _build_raydium_ladder_children(*, distribution: str, order_count: int, total_volume: Decimal, start_price: Decimal, end_price: Decimal, size_decimals: int, price_decimals: int, min_quantity: Optional[Decimal], min_notional: Optional[Decimal]) -> tuple[List[Dict[str, Decimal]], Decimal, int]:
    prices = _build_ladder_prices(start_price, end_price, order_count, price_decimals)
    sizes, _ = _allocate_ladder_sizes(total_volume, order_count, size_decimals, distribution)
    children: List[Dict[str, Decimal]] = []
    omitted_below_minimum = 0
    for price, size in zip(prices, sizes):
        if size <= 0:
            continue
        if min_quantity is not None and min_quantity > 0 and size < min_quantity:
            omitted_below_minimum += 1
            continue
        if min_notional is not None and min_notional > 0 and (price * size) < min_notional:
            omitted_below_minimum += 1
            continue
        if children and children[-1]["price"] == price:
            children[-1]["size"] = children[-1]["size"] + size
            continue
        children.append({"price": price, "size": size})
    kept_volume = sum((child["size"] for child in children), Decimal("0"))
    return children, kept_volume, omitted_below_minimum


def _execute_new_order(request: Dict[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="new_order", exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Raydium account is not configured.")
    try:
        symbol = str(_resolve_request_value(request, "symbol") or "").strip().upper()
        side = str(_resolve_request_value(request, "side") or "").strip().lower()
        order_type = str(_resolve_request_value(request, "order_type") or "").strip().lower()
        volume_raw = _resolve_request_value(request, "volume", aliases=["size", "quantity"])
        price_raw = _resolve_request_value(request, "price")
        if not symbol:
            return make_failure(operation="new_order", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
        if side not in {"buy", "sell"}:
            return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_SIDE", message="Side must be buy or sell.")
        if order_type != "limit":
            return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_ORDER_TYPE", message="Only limit orders are supported.")
        requested_volume = _decimal_or_zero(volume_raw)
        requested_price = _decimal_or_zero(price_raw)
        metadata = _resolve_symbol_metadata(symbol)
        submission = _submit_order(credentials, metadata, side=side, order_type=order_type, requested_volume=requested_volume, requested_price=requested_price)
        orders = _fetch_open_orders(credentials)
        verified, verified_order_id = _verify_order(
            orders,
            symbol=metadata["symbol"],
            side=side,
            submitted_price=submission["submitted_price"],
            submitted_volume=submission["submitted_volume"],
        )
        order_result = CanonicalOrderResult(
            symbol=str(metadata.get("display_symbol") or symbol),
            side=side,
            order_type=order_type,
            requested_volume=_decimal_text(requested_volume),
            requested_price=_decimal_text(requested_price),
            submitted_volume=submission["submitted_volume"],
            submitted_price=submission["submitted_price"],
            verified=verified,
            status="success" if verified else "failed",
            exchange_order_id=verified_order_id or submission.get("exchange_order_id"),
        )
        if verified:
            return make_success(operation="new_order", exchange=name, account=credentials["account"], order=order_result)
        return make_failure(operation="new_order", exchange=name, account=credentials["account"], code="VERIFICATION_FAILED", message="Order submission could not be verified.", order=order_result)
    except ValueError as exc:
        code = str(exc) or "INVALID_ORDER_REQUEST"
        return make_failure(operation="new_order", exchange=name, account=account, code=code, message=sanitize_error_message(code.replace("_", " ").title()))
    except Exception as exc:
        return make_failure(operation="new_order", exchange=name, account=account, code="ORDER_SUBMISSION_FAILED", message=sanitize_error_message(str(exc)))


def _execute_ladder(request: Dict[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="ladder", exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Raydium account is not configured.")
    try:
        symbol = str(_resolve_request_value(request, "symbol") or "").strip().upper()
        side = str(_resolve_request_value(request, "side") or "").strip().lower()
        distribution = str(_resolve_request_value(request, "distribution") or "").strip().lower()
        order_count = int(str(_resolve_request_value(request, "order_count") or "0"))
        total_volume = _decimal_or_zero(_resolve_request_value(request, "total_volume", aliases=["volume"]))
        start_price = _decimal_or_zero(_resolve_request_value(request, "start_price"))
        end_price = _decimal_or_zero(_resolve_request_value(request, "end_price"))
        if not symbol:
            return make_failure(operation="ladder", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
        if side not in {"buy", "sell"}:
            return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_SIDE", message="Side must be buy or sell.")
        if order_count <= 0:
            return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_ORDER_COUNT", message="Order count must be positive.")
        metadata = _resolve_symbol_metadata(symbol)
        children, submitted_volume, omitted_below_minimum = _build_raydium_ladder_children(
            distribution=distribution,
            order_count=order_count,
            total_volume=total_volume,
            start_price=start_price,
            end_price=end_price,
            size_decimals=int(metadata.get("size_precision") or 0),
            price_decimals=int(metadata.get("price_precision") or 0),
            min_quantity=_decimal_or_zero(metadata.get("min_quantity")),
            min_notional=_decimal_or_zero(metadata.get("min_notional")),
        )
        accepted = 0
        child_order_ids: List[int] = []
        batches: List[Dict[str, Any]] = []
        accepted_volume = Decimal("0")
        for child in children:
            try:
                submission = _submit_order(
                    credentials,
                    metadata,
                    side=side,
                    order_type="limit",
                    requested_volume=child["size"],
                    requested_price=child["price"],
                )
            except Exception as exc:
                batches.append({"submitted": 1, "accepted": 0, "ok": False, "reason": sanitize_error_message(str(exc))})
                ladder_result = CanonicalLadderResult(
                    symbol=str(metadata.get("display_symbol") or symbol),
                    side=side,
                    distribution=distribution,
                    requested_order_count=order_count,
                    submitted_order_count=accepted,
                    requested_volume=_decimal_text(total_volume),
                    submitted_volume=_decimal_text(accepted_volume),
                    batch_count=len(batches),
                    verified=False,
                    partial=accepted > 0,
                    status="partial" if accepted > 0 else "failed",
                    accepted_child_count=accepted,
                    omitted_order_count=max(0, order_count - len(children)),
                    omitted_below_minimum=omitted_below_minimum,
                    child_order_ids=child_order_ids or None,
                    batches=batches,
                )
                return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="ORDER_SUBMISSION_FAILED", message=sanitize_error_message(str(exc)), ladder=ladder_result)
            accepted += 1
            accepted_volume += child["size"]
            if submission.get("exchange_order_id") is not None:
                child_order_ids.append(int(submission["exchange_order_id"]))
            batches.append({"submitted": 1, "accepted": 1, "ok": True})
        orders = _fetch_open_orders(credentials)
        verified_count = 0
        matched_ids: List[int] = []
        for child in children:
            ok, matched_id = _verify_order(
                orders,
                symbol=metadata["symbol"],
                side=side,
                submitted_price=_format_decimal_places(child["price"], int(metadata.get("price_precision") or 0)),
                submitted_volume=_format_decimal_places(child["size"], int(metadata.get("size_precision") or 0)),
            )
            if not ok:
                break
            verified_count += 1
            if matched_id is not None:
                matched_ids.append(matched_id)
        verified = verified_count == len(children)
        ladder_result = CanonicalLadderResult(
            symbol=str(metadata.get("display_symbol") or symbol),
            side=side,
            distribution=distribution,
            requested_order_count=order_count,
            submitted_order_count=len(children),
            requested_volume=_decimal_text(total_volume),
            submitted_volume=_decimal_text(submitted_volume),
            batch_count=1,
            verified=verified,
            partial=not verified,
            status="success" if verified else "partial",
            accepted_child_count=(len(children) if verified else verified_count),
            omitted_order_count=max(0, order_count - len(children)),
            omitted_below_minimum=omitted_below_minimum,
            child_order_ids=(matched_ids or child_order_ids) or None,
            batches=batches or None,
        )
        if verified:
            return make_success(operation="ladder", exchange=name, account=credentials["account"], ladder=ladder_result)
        return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="VERIFICATION_FAILED", message="Ladder submission could not be verified.", ladder=ladder_result)
    except ValueError as exc:
        code = str(exc) or "INVALID_LADDER_REQUEST"
        return make_failure(operation="ladder", exchange=name, account=account, code=code, message=sanitize_error_message(code.replace("_", " ").title()))
    except Exception as exc:
        return make_failure(operation="ladder", exchange=name, account=account, code="LADDER_SUBMISSION_FAILED", message=sanitize_error_message(str(exc)))


def _positions_management(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="positions_management", exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Raydium account is not configured.")
    try:
        positions_payload = _private_get(credentials, "/v1/positions")
        orders_rows = _fetch_open_orders(credentials)
        algo_orders = _fetch_algo_orders(credentials)
        positions_rows = ((positions_payload.get("data") or {}).get("rows") if isinstance(positions_payload, dict) else None)
        symbols: List[str] = []
        if isinstance(positions_rows, list):
            symbols.extend(str(row.get("symbol") or "") for row in positions_rows if isinstance(row, dict))
        symbols.extend(str(row.get("symbol") or "") for row in orders_rows if isinstance(row, dict))
        positions = _normalize_positions(positions_rows, symbol_rules=_fetch_symbol_rules(symbols))
        positions = _overlay_position_protection(positions, algo_orders)
        return make_success(operation="positions_management", exchange=name, account=credentials["account"], positions=positions)
    except Exception as exc:
        return make_failure(operation="positions_management", exchange=name, account=account, code="RAYDIUM_ERROR", message=sanitize_error_message(str(exc)))


def _execute_set_tp_sl(request: Dict[str, Any], *, kind: str) -> CanonicalResponse:
    operation = "set_tp" if kind == "tp" else "set_sl"
    account = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation=operation, exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Raydium account is not configured.")
    symbol = str(request.get("symbol") or "").strip().upper()
    if not symbol:
        return make_failure(operation=operation, exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    try:
        metadata = _raydium_resolve_canonical(symbol)
    except RuntimeError:
        return make_failure(operation=operation, exchange=name, account=credentials["account"],
                            code="INSTRUMENT_NOT_FOUND",
                            message=f"Raydium instrument '{symbol}' is not available.")
    orderly_symbol = str(metadata.get("symbol") or "")
    display_symbol = str(metadata.get("display_symbol") or symbol)
    price = _decimal_or_zero(request.get("price"))
    try:
        positions_payload = _private_get(credentials, "/v1/positions")
        positions_rows = ((positions_payload.get("data") or {}).get("rows") if isinstance(positions_payload, dict) else None)
        symbol_rules = _fetch_symbol_rules([orderly_symbol])
        positions = _normalize_positions(positions_rows, symbol_rules=symbol_rules)
        position = _raydium_match_position(positions, metadata)
    except Exception as exc:
        return make_failure(operation=operation, exchange=name, account=account,
                            code="POSITIONS_UNAVAILABLE",
                            message=sanitize_error_message(str(exc)),
                            position_action=_position_action_result(operation=operation, symbol=display_symbol, verified=False, removed=False, status="failed"))
    if position is None:
        return make_failure(operation=operation, exchange=name, account=credentials["account"], code="POSITION_NOT_FOUND", message="Open position was not found.")
    closing_side = "SELL" if position.side == "long" else "BUY"
    try:
        algo_orders = _fetch_algo_orders(credentials, orderly_symbol)
    except Exception:
        algo_orders = []
    protection = _extract_protection_from_algo_orders(algo_orders, orderly_symbol, closing_side)
    current_orders = protection[kind]
    if price == 0:
        if not current_orders:
            return make_success(
                operation=operation,
                exchange=name,
                account=credentials["account"],
                position_action=_position_action_result(operation=operation, symbol=display_symbol, verified=True, removed=False, current_side=position.side, current_size=position.size, message=f"No {'Take Profit' if kind == 'tp' else 'Stop Loss'} was set."),
            )
        target = current_orders[0]
        target_id = int(str(target.get("algo_order_id") or 0))
        try:
            _signed_delete(credentials, "/v1/algo/order", {"order_id": target_id, "symbol": orderly_symbol})
            after = _fetch_algo_orders(credentials, orderly_symbol)
            after_state = _extract_protection_from_algo_orders(after, orderly_symbol, closing_side)
            removed = not after_state[kind]
            action = _position_action_result(operation=operation, symbol=display_symbol, verified=removed, removed=True, exchange_order_id=target_id, current_side=position.side, current_size=position.size, status="success" if removed else "failed", message=f"{'Take Profit' if kind == 'tp' else 'Stop Loss'} removed." if removed else None)
            if removed:
                return make_success(operation=operation, exchange=name, account=credentials["account"], position_action=action)
            return make_failure(operation=operation, exchange=name, account=credentials["account"], code="VERIFICATION_FAILED", message="Protection removal could not be verified.", position_action=action)
        except Exception as exc:
            return make_failure(operation=operation, exchange=name, account=account, code=f"{'TP' if kind == 'tp' else 'SL'}_REMOVAL_FAILED", message=sanitize_error_message(str(exc)), position_action=_position_action_result(operation=operation, symbol=display_symbol, verified=False, removed=True, status="failed", current_side=position.side, current_size=position.size))
    existing_tp_price = _decimal_or_zero(current_orders[0].get("trigger_price")) if current_orders else Decimal("0")
    sibling_kind = "sl" if kind == "tp" else "tp"
    sibling_orders = protection[sibling_kind]
    existing_sibling_price = _decimal_or_zero(sibling_orders[0].get("trigger_price")) if sibling_orders else Decimal("0")
    desired_tp_price = price if kind == "tp" else existing_sibling_price
    desired_sl_price = price if kind == "sl" else existing_sibling_price
    existing_root_ids = []
    for item in protection.get("tp", []) + protection.get("sl", []):
        try:
            root_id = int(str(item.get("algo_order_id") or 0))
        except Exception:
            root_id = 0
        if root_id > 0 and root_id not in existing_root_ids:
            existing_root_ids.append(root_id)
    for root_id in existing_root_ids:
        try:
            _signed_delete(credentials, "/v1/algo/order", {"order_id": root_id, "symbol": orderly_symbol})
        except Exception:
            pass
    payload = _build_tp_sl_algo_payload(symbol=orderly_symbol, side=closing_side, tp_price=desired_tp_price, sl_price=desired_sl_price, metadata=metadata)
    if not payload["child_orders"]:
        return make_failure(operation=operation, exchange=name, account=account, code="INVALID_PROTECTION_REQUEST", message="No TP/SL child orders were available to submit.", position_action=_position_action_result(operation=operation, symbol=display_symbol, verified=False, removed=False, price=_decimal_text(price), status="failed", current_side=position.side, current_size=position.size))
    try:
        response = _signed_post(credentials, "/v1/algo/order", payload)
        data = response.get("data") if isinstance(response, dict) else None
        exchange_order_id = None
        if isinstance(data, dict):
            try:
                exchange_order_id = int(str(data.get("algo_order_id") or data.get("order_id") or 0))
            except Exception:
                exchange_order_id = None
        after = _fetch_algo_orders(credentials, orderly_symbol)
        after_state = _extract_protection_from_algo_orders(after, orderly_symbol, closing_side)
        verified_orders = after_state[kind]
        target_price = _format_decimal_places(_quantize_down(price, int(metadata.get("price_precision") or 0)), int(metadata.get("price_precision") or 0))
        verified = any(_decimal_text(item.get("trigger_price")) == _decimal_text(target_price) for item in verified_orders)
        action = _position_action_result(operation=operation, symbol=display_symbol, verified=verified, removed=False, price=target_price, exchange_order_id=exchange_order_id, current_side=position.side, current_size=position.size, status="success" if verified else "failed")
        if verified:
            return make_success(operation=operation, exchange=name, account=credentials["account"], position_action=action)
        return make_failure(operation=operation, exchange=name, account=credentials["account"], code="VERIFICATION_FAILED", message="Protection submission could not be verified.", position_action=action)
    except Exception as exc:
        return make_failure(operation=operation, exchange=name, account=account, code=f"{'TP' if kind == 'tp' else 'SL'}_SUBMISSION_FAILED", message=sanitize_error_message(str(exc)), position_action=_position_action_result(operation=operation, symbol=display_symbol, verified=False, removed=False, price=_decimal_text(price), status="failed", current_side=position.side, current_size=position.size))


def _raydium_resolve_canonical(symbol: Any) -> Dict[str, Any]:
    """Resolve a caller symbol to the Raydium/Orderly canonical metadata.

    Returns a dict with ``symbol`` (Orderly ``PERP_…``), ``display_symbol``
    (display name, e.g. ``SOL``), and ``price_precision`` /
    ``size_precision`` fields the rest of the agent expects. Raises
    ``RuntimeError("INSTRUMENT_NOT_FOUND")`` when the venue's public
    metadata lookup fails — callers map that to ``INSTRUMENT_NOT_FOUND``
    or to their own explicit error code.
    """
    metadata = _resolve_symbol_metadata(symbol)
    orderly_symbol = str(metadata.get("symbol") or "")
    if not orderly_symbol:
        raise RuntimeError("INSTRUMENT_NOT_FOUND")
    return metadata


def _raydium_match_position(positions: List[Any], metadata: Dict[str, Any]) -> Optional[Any]:
    """Find a position whose Orderly or display symbol matches the resolved market.

    Venue rows may carry either the Orderly ``PERP_*_USDC`` symbol or the
    user-facing display; both must match. The position list is the same
    shape ``_normalize_positions`` emits (CanonicalPosition).
    """
    orderly_symbol = str(metadata.get("symbol") or "")
    display_symbol = str(metadata.get("display_symbol") or "")
    for position in positions or []:
        sym = getattr(position, "symbol", None) or ""
        if sym == orderly_symbol or sym == display_symbol:
            return position
    return None


def _execute_close_position(request: Dict[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="close_position", exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Raydium account is not configured.")
    symbol = str(request.get("symbol") or "").strip().upper()
    if not symbol:
        return make_failure(operation="close_position", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    try:
        metadata = _raydium_resolve_canonical(symbol)
    except RuntimeError:
        return make_failure(operation="close_position", exchange=name, account=credentials["account"],
                            code="INSTRUMENT_NOT_FOUND",
                            message=f"Raydium instrument '{symbol}' is not available.")
    try:
        positions_payload = _private_get(credentials, "/v1/positions")
        positions_rows = ((positions_payload.get("data") or {}).get("rows") if isinstance(positions_payload, dict) else None)
        symbol_rules = _fetch_symbol_rules([metadata["symbol"]])
        positions = _normalize_positions(positions_rows, symbol_rules=symbol_rules)
        position = _raydium_match_position(positions, metadata)
        if position is None:
            return make_failure(operation="close_position", exchange=name, account=credentials["account"], code="POSITION_NOT_FOUND", message="Open position was not found.")
        side = "sell" if position.side == "long" else "buy"
        submission = _submit_order(credentials, metadata, side=side, order_type="market", requested_volume=_decimal_or_zero(position.size), requested_price=Decimal("1"), reduce_only=True)
        verified = _raydium_match_position(_normalize_positions(_private_get(credentials, "/v1/positions").get("data", {}).get("rows") if isinstance(_private_get(credentials, "/v1/positions"), dict) else None, symbol_rules=symbol_rules), metadata) is None
        action = _position_action_result(operation="close_position", symbol=str(metadata.get("display_symbol") or symbol), verified=verified, removed=True, exchange_order_id=submission.get("exchange_order_id"), current_side=position.side, current_size=position.size, status="success" if verified else "failed")
        if verified:
            return make_success(operation="close_position", exchange=name, account=credentials["account"], position_action=action)
        return make_failure(operation="close_position", exchange=name, account=credentials["account"], code="VERIFICATION_FAILED", message="Close position could not be verified.", position_action=action)
    except Exception as exc:
        return make_failure(operation="close_position", exchange=name, account=account, code="CLOSE_POSITION_FAILED", message=sanitize_error_message(str(exc)))


def _execute_cancel_order_group(request: Dict[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="cancel_order_group", exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Raydium account is not configured.")
    symbol = str(request.get("symbol") or "").strip().upper()
    side = str(request.get("side") or "").strip().lower()
    if not symbol:
        return make_failure(operation="cancel_order_group", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    if side not in {"buy", "sell"}:
        return make_failure(operation="cancel_order_group", exchange=name, account=account, code="INVALID_SIDE", message="Side must be buy or sell.")
    try:
        metadata = _raydium_resolve_canonical(symbol)
    except RuntimeError:
        return make_failure(operation="cancel_order_group", exchange=name, account=credentials["account"],
                            code="INSTRUMENT_NOT_FOUND",
                            message=f"Raydium instrument '{symbol}' is not available.")
    target_symbol = str(metadata.get("symbol") or "")
    display_symbol = str(metadata.get("display_symbol") or symbol)
    try:
        before = _fetch_open_orders(credentials)
        targets = [row for row in before if isinstance(row, dict) and _orderly_symbol(row.get("symbol")) == target_symbol and str(row.get("side") or "").strip().upper() == side.upper()]
        if not targets:
            cancel_group = CanonicalCancelGroupResult(symbol=display_symbol, side=side, targeted_order_count=0, cancelled_order_count=0, confirmed_absent_count=0, remaining_target_count=0, verified=True, partial=False, status="success", batch_count=0)
            return make_success(operation="cancel_order_group", exchange=name, account=credentials["account"], cancel_group=cancel_group)
        cancelled = 0
        batches: List[Dict[str, Any]] = []
        for row in targets:
            order_id = int(str(row.get("order_id") or 0))
            client_order_id = str(row.get("client_order_id") or "").strip()
            try:
                if client_order_id:
                    _signed_delete(credentials, "/v1/client/order", {"client_order_id": client_order_id, "symbol": target_symbol})
                else:
                    _signed_delete(credentials, "/v1/order", {"order_id": order_id, "symbol": target_symbol})
                cancelled += 1
                batches.append({"submitted": 1, "accepted": 1, "ok": True})
            except Exception as exc:
                fallback_exc = None
                if client_order_id and order_id > 0:
                    try:
                        _signed_delete(credentials, "/v1/order", {"order_id": order_id, "symbol": target_symbol})
                        cancelled += 1
                        batches.append({"submitted": 1, "accepted": 1, "ok": True, "fallback_from_client_order_id": True})
                        continue
                    except Exception as inner_exc:
                        fallback_exc = inner_exc
                try:
                    latest = _fetch_open_orders(credentials)
                except Exception:
                    latest = []
                still_present = False
                for latest_row in latest:
                    if not isinstance(latest_row, dict):
                        continue
                    if _orderly_symbol(latest_row.get("symbol")) != target_symbol:
                        continue
                    if str(latest_row.get("side") or "").strip().upper() != side.upper():
                        continue
                    latest_order_id = str(latest_row.get("order_id") or "").strip()
                    latest_client_order_id = str(latest_row.get("client_order_id") or "").strip()
                    if latest_order_id == str(order_id) or (client_order_id and latest_client_order_id == client_order_id):
                        still_present = True
                        break
                if not still_present:
                    cancelled += 1
                    batches.append({"submitted": 1, "accepted": 1, "ok": True, "verified_after_error": True, "fallback_from_client_order_id": bool(client_order_id)})
                    continue
                reason_text = sanitize_error_message(str(fallback_exc or exc))
                batches.append({"submitted": 1, "accepted": 0, "ok": False, "reason": reason_text})
                cancel_group = CanonicalCancelGroupResult(symbol=display_symbol, side=side, targeted_order_count=len(targets), cancelled_order_count=cancelled, confirmed_absent_count=0, remaining_target_count=len(targets) - cancelled, verified=False, partial=cancelled > 0, status="partial" if cancelled > 0 else "failed", batch_count=len(batches), batches=batches)
                return make_failure(operation="cancel_order_group", exchange=name, account=credentials["account"], code="CANCEL_FAILED", message=reason_text, cancel_group=cancel_group)
        after = _fetch_open_orders(credentials)
        remaining = [row for row in after if isinstance(row, dict) and _orderly_symbol(row.get("symbol")) == target_symbol and str(row.get("side") or "").strip().upper() == side.upper()]
        confirmed_absent = len(targets) - len(remaining)
        verified = len(remaining) == 0
        cancel_group = CanonicalCancelGroupResult(symbol=display_symbol, side=side, targeted_order_count=len(targets), cancelled_order_count=cancelled, confirmed_absent_count=confirmed_absent, remaining_target_count=len(remaining), verified=verified, partial=not verified, status="success" if verified else "partial", batch_count=len(batches), batches=batches)
        if verified:
            return make_success(operation="cancel_order_group", exchange=name, account=credentials["account"], cancel_group=cancel_group)
        return make_failure(operation="cancel_order_group", exchange=name, account=credentials["account"], code="VERIFICATION_FAILED", message="Cancellation could not be verified.", cancel_group=cancel_group)
    except Exception as exc:
        return make_failure(operation="cancel_order_group", exchange=name, account=account, code="CANCEL_FAILED", message=sanitize_error_message(str(exc)))


def _raydium_resolve_instrument(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    requested = str(request.get("symbol") or "").strip()
    if not requested:
        return make_failure(operation="resolve_instrument", exchange=name, account=account,
                            code="MISSING_SYMBOL", message="Symbol is required.")
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="resolve_instrument", exchange=name, account=account,
                            code="ACCOUNT_NOT_FOUND", message="Raydium account is not configured.")
    try:
        metadata = _resolve_symbol_metadata(requested)
    except Exception as exc:
        # Unproven aliases like SOL-USDC, SOL_USDC, SOLUSDC do not exist
        # in the live Raydium/Orderly catalog and the public lookup fails.
        # Surface this as INSTRUMENT_NOT_FOUND so unknown / unsupported
        # aliases never collapse into a "no order" success.
        return make_failure(operation="resolve_instrument", exchange=name, account=credentials["account"],
                            code="INSTRUMENT_NOT_FOUND",
                            message=f"Raydium instrument '{requested}' is not available.")
    orderly_symbol = str(metadata.get("symbol") or "")
    if not orderly_symbol:
        return make_failure(operation="resolve_instrument", exchange=name, account=credentials["account"],
                            code="INSTRUMENT_NOT_FOUND",
                            message=f"Raydium instrument '{requested}' is not available.")
    display_symbol = str(metadata.get("display_symbol") or _symbol_from_orderly(orderly_symbol))
    instrument = CanonicalInstrument(
        requested_symbol=requested,
        symbol=orderly_symbol,
        display_name=display_symbol,
        price_increment=None,
        size_increment=None,
        minimum_size=str(metadata.get("min_quantity") or "") or None,
    )
    return make_success(operation="resolve_instrument", exchange=name, account=credentials["account"],
                        instrument=instrument)



def _raydium_list_instruments(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Enumerate Orderly public futures markets used by Raydium Trade API."""
    if not account:
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account="",
            code="MISSING_ACCOUNT",
            message="Account is required.",
        )
    try:
        payload = _public_get("/v1/public/info")
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account=account,
            code="CATALOG_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = []
    if isinstance(data, dict):
        rows = data.get("rows") or []
    elif isinstance(data, list):
        rows = data
    if not isinstance(rows, list):
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account=account,
            code="CATALOG_UNAVAILABLE",
            message="Orderly public info missing rows.",
        )
    # Optional futures prices map
    mark_by = {}
    try:
        fut = _public_get("/v1/public/futures")
        fdata = fut.get("data") if isinstance(fut, dict) else None
        frows = []
        if isinstance(fdata, dict):
            frows = fdata.get("rows") or []
        elif isinstance(fdata, list):
            frows = fdata
        for row in frows or []:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").strip()
            if not sym:
                continue
            mp = row.get("mark_price")
            if mp is None:
                mp = row.get("markPrice")
            if mp is None:
                mp = row.get("index_price") or row.get("indexPrice")
            if mp is not None:
                mark_by[sym] = mp
    except Exception:  # noqa: BLE001
        pass
    instruments: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        orderly_symbol = str(row.get("symbol") or "").strip()
        if not orderly_symbol:
            continue
        display = str(row.get("display_symbol_name") or _symbol_from_orderly(orderly_symbol) or orderly_symbol).strip()
        entry: Dict[str, Any] = {
            "instrument": orderly_symbol,
            "display_name": display,
            "base": display,
            "market_type": "perp",
        }
        mp = mark_by.get(orderly_symbol)
        if mp is not None:
            try:
                d = Decimal(str(mp))
                if d.is_finite() and d > 0:
                    entry["price"] = format(d.normalize(), "f")
            except Exception:  # noqa: BLE001
                pass
        instruments.append(entry)
    return make_success(
        operation="list_instruments",
        exchange=name,
        account=account,
        data={"instruments": instruments},
    )


def _raydium_market_price(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    requested = str(request.get("symbol") or request.get("requested_symbol") or "").strip()
    if not requested:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=account or "",
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    try:
        meta = _resolve_symbol_metadata(requested)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="market_price",
            exchange=name,
            account=account or "",
            code="INSTRUMENT_NOT_FOUND",
            message=sanitize_error_message(str(exc)) or f"Instrument not found: {requested}",
        )
    native = str(meta.get("symbol") or "").strip()
    mark = meta.get("mark_price")
    try:
        d = Decimal(str(mark))
    except Exception:  # noqa: BLE001
        d = Decimal("0")
    if d <= 0:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=account or "",
            code="PRICE_UNAVAILABLE",
            message=f"Price unavailable for {requested}",
        )
    text = format(d.normalize(), "f")
    display = str(meta.get("display_symbol") or native)
    return make_success(
        operation="market_price",
        exchange=name,
        account=account or "",
        instrument=CanonicalInstrument(
            requested_symbol=requested,
            symbol=native,
            display_name=display,
        ),
        market_price=CanonicalMarketPrice(
            requested_symbol=requested,
            market=native,
            mark_price=text,
            price=text,
        ),
    )



def execute(request: Dict[str, Any]) -> CanonicalResponse:
    operation = str(request.get("operation") or "").strip().lower()
    account = str(request.get("account") or "").strip()
    if operation == "balance":
        return _balance(account)
    if operation == "positions_orders":
        return _positions_orders(account)
    if operation == "new_order":
        return _execute_new_order(request)
    if operation == "ladder":
        return _execute_ladder(request)
    if operation == "positions_management":
        return _positions_management(account)
    if operation == "set_tp":
        return _execute_set_tp_sl(request, kind="tp")
    if operation == "set_sl":
        return _execute_set_tp_sl(request, kind="sl")
    if operation == "close_position":
        return _execute_close_position(request)
    if operation == "cancel_order_group":
        return _execute_cancel_order_group(request)
    if operation == "resolve_instrument":
        return _raydium_resolve_instrument(account, request)
    if operation == "list_instruments":
        return _raydium_list_instruments(account, request)
    if operation == "market_price":
        return _raydium_market_price(account, request)
    return make_failure(
        operation=operation or "unknown",
        exchange=name,
        account=account,
        code="NOT_IMPLEMENTED",
        message=f"Raydium does not support operation '{operation}' yet.",
    )
