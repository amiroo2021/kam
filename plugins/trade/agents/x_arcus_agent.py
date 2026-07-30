"""Arcus exchange agent.

Arcus-specific credential discovery, signed REST write surface, and read-only
account normalization for the /trade stack. TradeDesk and the Telegram wizard
remain fully exchange-agnostic: all Arcus env names, REST routes, signing,
and response parsing live in this module.

Configured accounts are discovered from either the live environment or
~/.hermes/.env using the existing Arcus convention observed in this install:

- ARCUS_<ACCOUNT>_WALLET
- ARCUS_<ACCOUNT>_APISIGNINGKEY

Optional fields supported for flexibility:

- ARCUS_<ACCOUNT>_ACCOUNT_INDEX   (defaults to 0)
- ARCUS_<ACCOUNT>_BASE_URL        (defaults to https://api.arcus.xyz)
- ARCUS_<ACCOUNT>_PRIVATE_KEY     (optional ed25519 private key hex; required for
  signed write operations. If absent, signed writes return ARCUS_KEY_MISSING.)
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..canonical import (
    CanonicalCancelGroupResult,
    CanonicalOrderGroup,
    CanonicalOrderResult,
    CanonicalPortfolioSummary,
    CanonicalPosition,
    CanonicalResponse,
    make_failure,
    make_success,
    normalize_balance,
    sanitize_error_message,
)

name = "arcus"
DEFAULT_API_BASE = "https://api.arcus.xyz"
API_TIMEOUT_SECONDS = 20
_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_WALLET_PATTERN = re.compile(r"^(0x|0X)?[0-9a-fA-F]{40}$")

_SIDE_TO_INT = {"buy": 0, "sell": 1}
_INT_TO_SIDE = {0: "buy", 1: "sell"}
_TIF_TO_INT = {"gtt": 0, "fok": 1, "ioc": 2, "alo": 3}
_ARCUS_GOOD_TIL_TIME_MIN_FUTURE_MS = 30 * 86_400 * 1000
_ARCUS_PRIVATE_KEY_MISSING_CODE = "ARCUS_KEY_MISSING"


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


def _normalize_wallet(value: Any) -> Optional[str]:
    wallet = str(value or "").strip()
    if not wallet or not _WALLET_PATTERN.match(wallet):
        return None
    if wallet.startswith(("0x", "0X")):
        return f"0x{wallet[2:]}"
    return f"0x{wallet}"


def _discover_accounts() -> List[str]:
    env_values = _combined_env("ARCUS_")
    grouped: Dict[str, Dict[str, str]] = {}
    for key, value in env_values.items():
        if not value or not key.startswith("ARCUS_"):
            continue
        remainder = key[len("ARCUS_"):]
        field = None
        alias = ""
        for suffix in ("_WALLET", "_APISIGNINGKEY", "_ACCOUNT_INDEX", "_BASE_URL", "_PRIVATE_KEY"):
            if remainder.endswith(suffix):
                alias = remainder[: -len(suffix)]
                field = suffix[1:]
                break
        if field is None or not alias or not _ALIAS_PATTERN.match(alias):
            continue
        grouped.setdefault(alias, {})[field] = value
    valid: List[str] = []
    for alias in sorted(grouped.keys()):
        fields = grouped[alias]
        if not fields.get("WALLET") or not fields.get("APISIGNINGKEY"):
            continue
        if _normalize_wallet(fields.get("WALLET")) is None:
            continue
        valid.append(alias.lower())
    return valid


def list_accounts() -> List[str]:
    return _discover_accounts()


def _lookup_credentials(account: str) -> Optional[Dict[str, Any]]:
    alias = str(account or "").strip().upper()
    if not alias or not _ALIAS_PATTERN.match(alias):
        return None
    env_values = _combined_env("ARCUS_")
    wallet = _normalize_wallet(env_values.get(f"ARCUS_{alias}_WALLET", ""))
    api_signing_key = str(env_values.get(f"ARCUS_{alias}_APISIGNINGKEY", "")).strip()
    base_url = str(env_values.get(f"ARCUS_{alias}_BASE_URL", DEFAULT_API_BASE)).strip() or DEFAULT_API_BASE
    account_index_text = str(env_values.get(f"ARCUS_{alias}_ACCOUNT_INDEX", "0")).strip() or "0"
    private_key_hex = str(env_values.get(f"ARCUS_{alias}_PRIVATE_KEY", "")).strip()
    if wallet is None or not api_signing_key:
        return None
    try:
        account_index = int(account_index_text)
    except Exception:
        account_index = 0
    if account_index < 0 or account_index > 9:
        account_index = 0
    return {
        "account": alias.lower(),
        "wallet": wallet,
        "api_signing_key": api_signing_key,
        "private_key_hex": private_key_hex,
        "account_index": account_index,
        "base_url": base_url.rstrip("/"),
    }


def capabilities() -> List[str]:
    return ["balance", "positions_orders", "new_order", "cancel_orders"]


def _api_key_for_signing(api_signing_key: str) -> str:
    text = str(api_signing_key or "").strip()
    if text.startswith("ed25519:"):
        return text[len("ed25519:"):].strip()
    return text


def _ed25519_private_key_from_hex(private_key_hex: str) -> Ed25519PrivateKey:
    raw = str(private_key_hex or "").strip()
    if raw.startswith("ed25519:"):
        raw = raw[len("ed25519:"):].strip()
    try:
        secret_bytes = bytes.fromhex(raw)
    except ValueError as exc:
        raise ValueError("ARCUS private key must be a hex-encoded ed25519 private key") from exc
    if len(secret_bytes) != 32:
        raise ValueError("ARCUS private key must decode to 32 bytes (ed25519 seed)")
    return Ed25519PrivateKey.from_private_bytes(secret_bytes)


def _canonical_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=False)


def _format_arcus_error(status_code: int, payload: Any) -> str:
    if not isinstance(payload, dict):
        return f"HTTP {status_code}"
    code = payload.get("code") or payload.get("rejectionReason") or ""
    message = payload.get("error") or payload.get("message") or ""
    if code and message:
        return f"{code}: {message}"
    return str(code or message or f"HTTP {status_code}")


def _public_get(credentials: Dict[str, Any], path: str) -> Dict[str, Any]:
    response = requests.get(
        f"{credentials['base_url']}{path}",
        params={"address": credentials["wallet"], "accountIndex": credentials["account_index"]},
        timeout=API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Arcus response body was not a JSON object")
    return payload


def _coerce_order_id(order_id: Any) -> Optional[int]:
    text = str(order_id or "").strip()
    if not text:
        return None
    try:
        if text.isdigit():
            return int(text)
        return int(text, 16)
    except ValueError:
        return None


def _signed_post(credentials: Dict[str, Any], path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not credentials.get("private_key_hex"):
        raise RuntimeError(_ARCUS_PRIVATE_KEY_MISSING_CODE + ": ARCUS private key is required for signed writes.")
    private_key = _ed25519_private_key_from_hex(credentials["private_key_hex"])
    body = _canonical_json(payload)
    timestamp_ns = str(int(time.time() * 1000) * 1_000_000)
    signature = private_key.sign(body.encode("utf-8")).hex()
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": _api_key_for_signing(credentials["api_signing_key"]),
        "X-Timestamp": timestamp_ns,
        "X-Signature": signature,
    }
    response = requests.post(
        f"{credentials['base_url']}{path}",
        headers=headers,
        data=body,
        timeout=API_TIMEOUT_SECONDS,
    )
    try:
        payload_obj = response.json()
    except ValueError:
        payload_obj = {"raw": response.text}
    if response.status_code >= 400:
        raise RuntimeError(_format_arcus_error(response.status_code, payload_obj))
    return payload_obj if isinstance(payload_obj, dict) else {"raw": response.text}


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
    if rendered in {"-0", "-0.0"}:
        return "0"
    return rendered


def _decimal_places(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        decimal_value = Decimal(text)
    except Exception:
        return 0
    sign, digits, exponent = decimal_value.as_tuple()
    exponent = int(exponent)
    if exponent >= 0:
        return max(0, -exponent)
    return -exponent


def _format_decimal_places(value: Decimal, places: int) -> str:
    quantum = Decimal("1").scaleb(-max(0, places))
    quantized = value.quantize(quantum, rounding=ROUND_HALF_UP)
    return format(quantized, "f")


def _quantize_down(value: Decimal, places: int) -> Decimal:
    quantum = Decimal("1").scaleb(-max(0, places))
    return value.quantize(quantum, rounding=ROUND_DOWN)


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper() or "UNKNOWN"


def _normalize_positions(positions_payload: Any) -> List[CanonicalPosition]:
    positions_map = positions_payload if isinstance(positions_payload, dict) else {}
    positions: List[CanonicalPosition] = []
    for _, row in sorted(positions_map.items(), key=lambda item: str(item[0])):
        if not isinstance(row, dict):
            continue
        size = _decimal_or_zero(row.get("size"))
        if size == 0:
            continue
        side_text = str(row.get("side") or "").strip().lower()
        side = "long" if side_text in {"long", "buy"} else "short" if side_text in {"short", "sell"} else ("long" if size > 0 else "short")
        symbol = _normalize_symbol(row.get("marketDisplayName") or row.get("symbol") or row.get("market"))
        size_precision = max(_decimal_places(row.get("size")), 0)
        price_precision = max(_decimal_places(row.get("averageEntryPrice")), _decimal_places(row.get("markPx")))
        positions.append(
            CanonicalPosition(
                symbol=symbol,
                side=side,
                size=_format_decimal_places(abs(size), size_precision),
                entry_price=_format_decimal_places(_decimal_or_zero(row.get("averageEntryPrice")), price_precision),
                pnl=_decimal_text(row.get("unrealizedPnl")),
            )
        )
    return positions


def _aggregate_orders(orders_payload: Any) -> Tuple[int, List[CanonicalOrderGroup]]:
    orders = orders_payload if isinstance(orders_payload, list) else []
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    open_count = 0
    for row in orders:
        if not isinstance(row, dict):
            continue
        symbol = _normalize_symbol(row.get("marketDisplayName") or row.get("symbol") or row.get("market"))
        side = str(row.get("side") or "").strip().lower()
        if side not in {"buy", "sell"}:
            continue
        remaining = _decimal_or_zero(row.get("remainingSize") if row.get("remainingSize") is not None else row.get("originalSize"))
        if remaining <= 0:
            continue
        price = _decimal_or_zero(row.get("price"))
        key = (symbol, side)
        bucket = grouped.setdefault(key, {
            "count": 0,
            "remaining": Decimal("0"),
            "notional": Decimal("0"),
            "min_price": None,
            "max_price": None,
            "size_precision": 0,
            "price_precision": 0,
        })
        open_count += 1
        bucket["count"] += 1
        bucket["remaining"] += remaining
        bucket["notional"] += remaining * price
        bucket["size_precision"] = max(int(bucket["size_precision"]), _decimal_places(row.get("remainingSize") if row.get("remainingSize") is not None else row.get("originalSize")))
        bucket["price_precision"] = max(int(bucket["price_precision"]), _decimal_places(row.get("price")))
        bucket["min_price"] = price if bucket["min_price"] is None or price < bucket["min_price"] else bucket["min_price"]
        bucket["max_price"] = price if bucket["max_price"] is None or price > bucket["max_price"] else bucket["max_price"]
    groups: List[CanonicalOrderGroup] = []
    for (symbol, side), bucket in sorted(grouped.items()):
        total_size = bucket["remaining"]
        if total_size <= 0:
            continue
        vwap = bucket["notional"] / total_size if total_size else Decimal("0")
        groups.append(
            CanonicalOrderGroup(
                symbol=symbol,
                side=side,
                order_count=int(bucket["count"]),
                total_size=_format_decimal_places(total_size, int(bucket["size_precision"])),
                vwap=_format_decimal_places(vwap, int(bucket["price_precision"])),
                min_price=_format_decimal_places(bucket["min_price"], int(bucket["price_precision"])),
                max_price=_format_decimal_places(bucket["max_price"], int(bucket["price_precision"])),
            )
        )
    return (open_count, groups)


def _balance(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="balance", exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Arcus account is not configured.")
    try:
        payload = _public_get(credentials, "/v1/account")
        total = payload.get("equity") or payload.get("netQuoteBalance") or "0"
        withdrawable = payload.get("freeCollateral") or total
        net_quote = _decimal_or_zero(payload.get("netQuoteBalance"))
        position_value = _decimal_or_zero(payload.get("equity")) - net_quote
        margin_used = _decimal_or_zero(payload.get("equity")) - _decimal_or_zero(payload.get("freeCollateral"))
        portfolio_summary = CanonicalPortfolioSummary(
            account_value=_quantize_2(total),
            withdrawable=_quantize_2(withdrawable),
            margin_used=_quantize_2(margin_used),
            total_position_value=_quantize_2(position_value),
            unit="USD",
        )
        return make_success(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            balance=normalize_balance(total, "USD"),
            portfolio_summary=portfolio_summary,
            positions=_normalize_positions(payload.get("positions")),
        )
    except Exception as exc:
        return make_failure(operation="balance", exchange=name, account=account, code="ARCUS_ERROR", message=sanitize_error_message(str(exc)))


def _positions_orders(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="positions_orders", exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Arcus account is not configured.")
    try:
        account_payload = _public_get(credentials, "/v1/account")
        orders_payload = _public_get(credentials, "/v1/openOrders")
        positions = _normalize_positions(account_payload.get("positions"))
        open_order_count, order_groups = _aggregate_orders(orders_payload.get("orders"))
        return make_success(
            operation="positions_orders",
            exchange=name,
            account=credentials["account"],
            positions=positions,
            open_order_count=open_order_count,
            order_groups=order_groups,
        )
    except Exception as exc:
        return make_failure(operation="positions_orders", exchange=name, account=account, code="ARCUS_ERROR", message=sanitize_error_message(str(exc)))


def _resolve_request_value(request: Dict[str, Any], *keys: str, aliases: Optional[List[str]] = None) -> Any:
    structured = request.get("structured_request") if isinstance(request, dict) else None
    sources: List[Dict[str, Any]] = []
    if isinstance(request, dict):
        sources.append(request)
    if isinstance(structured, dict):
        sources.append(structured)
    names: List[str] = []
    for key in keys:
        if key not in names:
            names.append(key)
    if aliases:
        for alias in aliases:
            if alias not in names:
                names.append(alias)
    for source in sources:
        for name in names:
            if name in source and source[name] is not None:
                return source[name]
    return None


def _resolve_market(symbol: str) -> Dict[str, Any]:
    markets_payload = _public_get(_market_cache_credentials(), "/v1/markets")
    markets = markets_payload.get("markets") if isinstance(markets_payload, dict) else None
    if not isinstance(markets, list):
        raise ValueError("ARCUS_MARKET_LOOKUP_FAILED")
    target = str(symbol or "").strip().upper()
    if not target:
        raise ValueError("SYMBOL_NOT_FOUND")
    # Build a list of (display_name, market) with preference scoring.
    candidates: List[tuple[int, Dict[str, Any]]] = []
    target_base = target.split("-", 1)[0]
    for market in markets:
        if not isinstance(market, dict):
            continue
        display_name = _normalize_symbol(market.get("marketDisplayName"))
        base_asset = _normalize_symbol(market.get("baseAsset"))
        if display_name == target:
            candidates.append((0, market))
            continue
        if base_asset and base_asset == target:
            candidates.append((1, market))
            continue
        if base_asset and base_asset == target_base:
            candidates.append((2, market))
    if not candidates:
        raise ValueError("SYMBOL_NOT_FOUND")
    candidates.sort(key=lambda item: item[0])
    _, market = candidates[0]
    tick_size = _decimal_or_zero(market.get("tickSize"))
    step_size = _decimal_or_zero(market.get("stepSize"))
    min_notional = _decimal_or_zero(market.get("minOrderNotional"))
    return {
        "market_id": int(market.get("marketId") or 0),
        "display_symbol": _normalize_symbol(market.get("marketDisplayName")),
        "tick_size": tick_size,
        "step_size": step_size,
        "price_precision": _decimal_places(market.get("tickSize")),
        "size_precision": _decimal_places(market.get("stepSize")),
        "min_notional": min_notional,
    }


def _market_cache_credentials() -> Dict[str, Any]:
    credentials = _lookup_credentials(os.environ.get("HERMES_ARCUS_DEFAULT_ACCOUNT", ""))
    if credentials is None:
        accounts = _discover_accounts()
        if not accounts:
            raise ValueError("ARCUS_ACCOUNT_NOT_FOUND")
        credentials = _lookup_credentials(accounts[0])
    if credentials is None:
        raise ValueError("ARCUS_ACCOUNT_NOT_FOUND")
    return credentials


def _good_til_time_us(market_id: int) -> int:
    now_ms = int(time.time() * 1000)
    return (now_ms + _ARCUS_GOOD_TIL_TIME_MIN_FUTURE_MS) * 1000


def _build_new_order_payload(*, credentials: Dict[str, Any], market: Dict[str, Any], side: str, quantity: Decimal, price: Decimal, reduce_only: bool, client_id: str) -> Dict[str, Any]:
    return {
        "address": credentials["wallet"],
        "marketId": int(market["market_id"]),
        "accountIndex": int(credentials["account_index"]),
        "quantity": _format_decimal_places(quantity, int(market["size_precision"])),
        "price": _format_decimal_places(price, int(market["price_precision"])),
        "goodTilTime": str(_good_til_time_us(int(market["market_id"]))),
        "timestamp": int(time.time_ns()),
        "reduceOnly": bool(reduce_only),
        "clientId": client_id,
        "clientTime": str(int(time.time() * 1000)),
    }


def _new_order(request: Dict[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="new_order", exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Arcus account is not configured.")
    try:
        symbol = str(_resolve_request_value(request, "symbol") or "").strip().upper()
        side = str(_resolve_request_value(request, "side") or "").strip().lower()
        order_type = str(_resolve_request_value(request, "order_type") or "limit").strip().lower()
        requested_volume = _decimal_or_zero(_resolve_request_value(request, "volume", aliases=["size", "quantity"]))
        requested_price = _decimal_or_zero(_resolve_request_value(request, "price"))
        reduce_only = bool(_resolve_request_value(request, "reduce_only", aliases=["reduceOnly"])) if _resolve_request_value(request, "reduce_only", aliases=["reduceOnly"]) is not None else False
        client_id_raw = _resolve_request_value(request, "client_id", aliases=["clientId"])
        if not symbol:
            return make_failure(operation="new_order", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
        if side not in _SIDE_TO_INT:
            return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_SIDE", message="Side must be buy or sell.")
        if order_type != "limit":
            return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_ORDER_TYPE", message="Only limit orders are supported.")
        market = _resolve_market(symbol)
        step_size = _decimal_or_zero(market["step_size"])
        tick_size = _decimal_or_zero(market["tick_size"])
        submitted_volume = _quantize_down(requested_volume, int(market["size_precision"]))
        submitted_price = _quantize_down(requested_price, int(market["price_precision"]))
        if step_size > 0 and submitted_volume <= 0:
            return make_failure(operation="new_order", exchange=name, account=credentials["account"], code="INVALID_VOLUME", message="Volume rounds down to zero at the market step size.")
        if tick_size > 0 and submitted_price <= 0:
            return make_failure(operation="new_order", exchange=name, account=credentials["account"], code="INVALID_PRICE", message="Price rounds down to zero at the market tick size.")
        if _decimal_or_zero(market["min_notional"]) > 0 and submitted_volume * submitted_price < _decimal_or_zero(market["min_notional"]):
            return make_failure(operation="new_order", exchange=name, account=credentials["account"], code="NOTIONAL_BELOW_MINIMUM", message="Order notional is below the market minimum.")
        client_id = str(client_id_raw).strip() if client_id_raw is not None else ""
        if not client_id:
            client_id = f"arcus-{uuid.uuid4().hex[:16]}"
        payload = _build_new_order_payload(
            credentials=credentials,
            market=market,
            side=side,
            quantity=submitted_volume,
            price=submitted_price,
            reduce_only=reduce_only,
            client_id=client_id,
        )
        response = _signed_post(credentials, "/v1/placeOrder", payload)
        verified_order_id = _coerce_order_id(response.get("orderId") or response.get("order_id"))
        order_result = CanonicalOrderResult(
            symbol=str(market.get("display_symbol") or symbol),
            side=side,
            order_type=order_type,
            requested_volume=_decimal_text(requested_volume),
            requested_price=_decimal_text(requested_price),
            submitted_volume=_format_decimal_places(submitted_volume, int(market["size_precision"])),
            submitted_price=_format_decimal_places(submitted_price, int(market["price_precision"])),
            verified=verified_order_id is not None,
            status="success" if verified_order_id is not None else "failed",
            exchange_order_id=verified_order_id,
        )
        if verified_order_id is not None:
            return make_success(operation="new_order", exchange=name, account=credentials["account"], order=order_result)
        return make_failure(operation="new_order", exchange=name, account=credentials["account"], code="VERIFICATION_FAILED", message="Arcus order placement did not return an order id.", order=order_result)
    except ValueError as exc:
        code = str(exc) or "INVALID_ORDER_REQUEST"
        return make_failure(operation="new_order", exchange=name, account=account, code=code, message=sanitize_error_message(code.replace("_", " ").title()))
    except RuntimeError as exc:
        message = str(exc)
        if message.startswith(_ARCUS_PRIVATE_KEY_MISSING_CODE):
            return make_failure(operation="new_order", exchange=name, account=account, code="ARCUS_KEY_MISSING", message="Arcus private key is not configured. Set ARCUS_<ACCOUNT>_PRIVATE_KEY in ~/.hermes/.env.")
        return make_failure(operation="new_order", exchange=name, account=account, code="ORDER_SUBMISSION_FAILED", message=sanitize_error_message(message))
    except Exception as exc:
        return make_failure(operation="new_order", exchange=name, account=account, code="ORDER_SUBMISSION_FAILED", message=sanitize_error_message(str(exc)))


def _fetch_open_orders_for_account(credentials: Dict[str, Any]) -> List[Dict[str, Any]]:
    payload = _public_get(credentials, "/v1/openOrders")
    orders = payload.get("orders") if isinstance(payload, dict) else None
    if not isinstance(orders, list):
        return []
    return [row for row in orders if isinstance(row, dict)]


def _cancel_order_group(request: Dict[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="cancel_orders", exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Arcus account is not configured.")
    symbol = str(request.get("symbol") or "").strip().upper()
    side = str(request.get("side") or "").strip().lower()
    if not symbol:
        return make_failure(operation="cancel_orders", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    if side not in {"buy", "sell"}:
        return make_failure(operation="cancel_orders", exchange=name, account=account, code="INVALID_SIDE", message="Side must be buy or sell.")
    try:
        market = _resolve_market(symbol)
        market_id = int(market["market_id"])
    except ValueError as exc:
        return make_failure(operation="cancel_orders", exchange=name, account=account, code=str(exc), message=sanitize_error_message(str(exc)))
    try:
        before = _fetch_open_orders_for_account(credentials)
        target_symbol = _normalize_symbol(symbol)
        targets: List[Dict[str, Any]] = []
        for row in before:
            row_symbol = _normalize_symbol(row.get("marketDisplayName") or row.get("symbol") or row.get("market"))
            row_side = str(row.get("side") or "").strip().lower()
            if row_symbol != target_symbol or row_side != side:
                continue
            order_id = str(row.get("orderId") or "").strip()
            if not order_id:
                continue
            targets.append({"row": row, "order_id": order_id})
        if not targets:
            cancel_group = CanonicalCancelGroupResult(symbol=symbol, side=side, targeted_order_count=0, cancelled_order_count=0, confirmed_absent_count=0, remaining_target_count=0, verified=True, partial=False, status="success", batch_count=0)
            return make_success(operation="cancel_orders", exchange=name, account=credentials["account"], cancel_group=cancel_group)
        cancelled = 0
        batches: List[Dict[str, Any]] = []
        for target in targets:
            order_id = target["order_id"]
            try:
                payload = {
                    "address": credentials["wallet"],
                    "marketId": market_id,
                    "accountIndex": int(credentials["account_index"]),
                    "kind": "orderId",
                    "orderId": order_id,
                    "validUntil": int((time.time() + 60) * 1000),
                    "signature": "",
                    "timestamp": int(time.time_ns()),
                }
                payload["signature"] = _sign_legacy(credentials, payload, action="cancelOrder")
                _signed_post(credentials, "/v1/cancelOrder", payload)
                cancelled += 1
                batches.append({"submitted": 1, "accepted": 1, "ok": True})
            except Exception as exc:
                latest = _fetch_open_orders_for_account(credentials)
                still_present = False
                for latest_row in latest:
                    if _normalize_symbol(latest_row.get("marketDisplayName") or latest_row.get("symbol") or latest_row.get("market")) != target_symbol:
                        continue
                    if str(latest_row.get("side") or "").strip().lower() != side:
                        continue
                    if str(latest_row.get("orderId") or "").strip() == order_id:
                        still_present = True
                        break
                if not still_present:
                    cancelled += 1
                    batches.append({"submitted": 1, "accepted": 1, "ok": True, "verified_after_error": True})
                    continue
                reason_text = sanitize_error_message(str(exc))
                batches.append({"submitted": 1, "accepted": 0, "ok": False, "reason": reason_text})
                cancel_group = CanonicalCancelGroupResult(symbol=symbol, side=side, targeted_order_count=len(targets), cancelled_order_count=cancelled, confirmed_absent_count=0, remaining_target_count=len(targets) - cancelled, verified=False, partial=cancelled > 0, status="partial" if cancelled > 0 else "failed", batch_count=len(batches), batches=batches)
                return make_failure(operation="cancel_orders", exchange=name, account=credentials["account"], code="CANCEL_FAILED", message=reason_text, cancel_group=cancel_group)
        after = _fetch_open_orders_for_account(credentials)
        remaining = [row for row in after if _normalize_symbol(row.get("marketDisplayName") or row.get("symbol") or row.get("market")) == target_symbol and str(row.get("side") or "").strip().lower() == side]
        confirmed_absent = len(targets) - len(remaining)
        verified = len(remaining) == 0
        cancel_group = CanonicalCancelGroupResult(symbol=symbol, side=side, targeted_order_count=len(targets), cancelled_order_count=cancelled, confirmed_absent_count=confirmed_absent, remaining_target_count=len(remaining), verified=verified, partial=not verified, status="success" if verified else "partial", batch_count=len(batches), batches=batches)
        if verified:
            return make_success(operation="cancel_orders", exchange=name, account=credentials["account"], cancel_group=cancel_group)
        return make_failure(operation="cancel_orders", exchange=name, account=credentials["account"], code="VERIFICATION_FAILED", message="Cancellation could not be verified.", cancel_group=cancel_group)
    except Exception as exc:
        if isinstance(exc, RuntimeError) and str(exc).startswith(_ARCUS_PRIVATE_KEY_MISSING_CODE):
            return make_failure(operation="cancel_orders", exchange=name, account=account, code="ARCUS_KEY_MISSING", message="Arcus private key is not configured. Set ARCUS_<ACCOUNT>_PRIVATE_KEY in ~/.hermes/.env.")
        return make_failure(operation="cancel_orders", exchange=name, account=account, code="CANCEL_FAILED", message=sanitize_error_message(str(exc)))


def _sign_legacy(credentials: Dict[str, Any], payload: Dict[str, Any], *, action: str) -> str:
    private_key = _ed25519_private_key_from_hex(credentials["private_key_hex"])
    message = f"{int(time.time_ns())}{action}{_canonical_json(payload)}"
    return private_key.sign(message.encode("utf-8")).hex()


def execute(request: Dict[str, Any]) -> CanonicalResponse:
    if not isinstance(request, dict):
        operation = ""
        account = ""
    else:
        operation = str(request.get("operation") or "").strip()
        account = str(request.get("account") or "").strip()
    if not operation:
        return make_failure(operation="", exchange=name, account=account, code="INVALID_REQUEST", message="Missing 'operation'.")
    if not account:
        return make_failure(operation=operation, exchange=name, account=account, code="INVALID_REQUEST", message="Missing 'account'.")
    if operation == "balance":
        return _balance(account)
    if operation == "positions_orders":
        return _positions_orders(account)
    if operation == "new_order":
        return _new_order(request)
    if operation == "cancel_orders":
        return _cancel_order_group(request)
    return make_failure(operation=operation, exchange=name, account=account, code="NOT_IMPLEMENTED", message=f"Arcus does not implement '{operation}' yet.")