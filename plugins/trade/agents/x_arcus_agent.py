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
    CanonicalLadderResult,
    CanonicalOrderGroup,
    CanonicalOrderResult,
    CanonicalPositionActionResult,
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
_ARCUS_GOOD_TIL_TIME_MIN_FUTURE_MS = 60 * 86_400 * 1000  # ~2 months; server enforces "at least one month" but uses calendar arithmetic, so pad to absorb clock skew and 28/29/30/31-day month length.
_ARCUS_PRIVATE_KEY_MISSING_CODE = "ARCUS_KEY_MISSING"

# Arcus Scheme 1 (typed payload) — verified live against
# https://docs.arcus.xyz/api-reference/authentication and the rest-trading quickstart.
# Ed25519 is over this sorted-key compact JSON object directly (no prefix). The
# matching HTTP body is sent in parallel but is NOT signed.
_ARCUS_OP_PLACE = 1
_ARCUS_OP_CANCEL = 2
_ARCUS_OP_MODIFY = 3
_ARCUS_PAYLOAD_VERSION = 1


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
        if not fields.get("WALLET"):
            continue
        # An account is usable if it has either the legacy APISIGNINGKEY or a
        # PRIVATE_KEY (from which we will derive the public key at lookup time).
        if not fields.get("APISIGNINGKEY") and not fields.get("PRIVATE_KEY"):
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
    base_url = str(env_values.get(f"ARCUS_{alias}_BASE_URL", DEFAULT_API_BASE)).strip() or DEFAULT_API_BASE
    account_index_text = str(env_values.get(f"ARCUS_{alias}_ACCOUNT_INDEX", "0")).strip() or "0"
    private_key_hex = str(env_values.get(f"ARCUS_{alias}_PRIVATE_KEY", "")).strip()
    legacy_api_signing_key = str(env_values.get(f"ARCUS_{alias}_APISIGNINGKEY", "")).strip()
    if wallet is None:
        return None
    # Derive the public key from the seed whenever a private key is present so the
    # X-API-Key header can never drift out of sync with the signing secret. The
    # APISIGNINGKEY env var is read for backward compatibility (older installs
    # held the public key there), but it is only used when no PRIVATE_KEY is set.
    if private_key_hex:
        try:
            api_signing_key = _ed25519_public_key_from_hex(private_key_hex)
        except ValueError:
            return None
    elif legacy_api_signing_key:
        api_signing_key = legacy_api_signing_key
    else:
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
    # Note: ``cancel_order_group`` (singular) is the canonical operation name
    # the wizard dispatches; ``cancel_orders`` (plural) is an alias kept for
    # backwards-compat with any caller that already uses the plural form.
    return [
        "balance",
        "positions_orders",
        "new_order",
        "ladder",
        "cancel_order_group",
        "cancel_orders",
        "positions_management",
        "set_tp",
        "set_sl",
        "close_position",
    ]


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


def _ed25519_public_key_from_hex(private_key_hex: str) -> str:
    """Derive the 32-byte Ed25519 public key (hex) from a 32-byte seed (hex)."""
    return _ed25519_private_key_from_hex(private_key_hex).public_key().public_bytes_raw().hex()


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


def _canonical_json(payload: Dict[str, Any]) -> str:
    # Body serializer: keeps dict insertion order, no whitespace, ASCII.
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=False)


def _typed_payload_bytes(payload: Dict[str, Any]) -> str:
    # Typed payload serializer: sorted keys, no whitespace, ASCII. This is the
    # exact byte string Ed25519 signs for Arcus Scheme 1.
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def _build_arcus_typed_payload_place(
    *,
    credentials: Dict[str, Any],
    market_id: int,
    price_ticks: int,
    qty_quantums: int,
    side: str,
    time_in_force: str,
    good_til_time_us: int,
    timestamp_ns: int,
    reduce_only: bool,
    client_id: str = "",
) -> Dict[str, Any]:
    """Build the Scheme 1 typed payload for /v1/placeOrder.

    All numeric fields are integers (the engine's native representation).
    `g` is the good-till-time in NANOSECONDS (the body's `goodTilTime` field is
    microseconds; the typed payload's `g` is the same instant in ns).
    `ct` is the client timestamp in NANOSECONDS and must equal the X-Timestamp
    header.

    `c` is the client id; **omitted entirely when empty** (per the Arcus auth
    spec — including an empty `c` field, or omitting a non-empty `c` while
    sending `clientId` in the body, causes the signature verification to fail
    with HTTP 401 "invalid order signature"). The address (`ad`) and the
    client id (`c`) are lowercased before signing.
    """
    side_lower = str(side or "").strip().lower()
    if side_lower not in _SIDE_TO_INT:
        raise ValueError(f"Unknown side: {side!r}")
    tif_lower = str(time_in_force or "").strip().lower()
    if tif_lower not in _TIF_TO_INT:
        raise ValueError(f"Unknown time_in_force: {time_in_force!r}")
    payload: Dict[str, Any] = {
        "ad": str(credentials["wallet"]).lower(),
        "ai": int(credentials["account_index"]),
        "ct": int(timestamp_ns),
        "g": int(good_til_time_us) * 1000,
        "m": int(market_id),
        "op": _ARCUS_OP_PLACE,
        "p": int(price_ticks),
        "q": int(qty_quantums),
        "r": 1 if reduce_only else 0,
        "s": _SIDE_TO_INT[side_lower],
        "t": _TIF_TO_INT[tif_lower],
        "v": _ARCUS_PAYLOAD_VERSION,
    }
    client_id_clean = str(client_id or "").strip()
    if client_id_clean:
        # `c` is inserted between `ai` and `ct` per the alphabetical canonical
        # order documented in https://docs.arcus.xyz/api-reference/authentication.
        # We rebuild the dict rather than mutating to keep key order deterministic.
        ordered: Dict[str, Any] = {}
        for key in payload:
            ordered[key] = payload[key]
            if key == "ai":
                ordered["c"] = client_id_clean
        payload = ordered
    return payload


def _build_arcus_typed_payload_cancel(
    *,
    credentials: Dict[str, Any],
    market_id: int,
    order_id: str,
    timestamp_ns: int,
) -> Dict[str, Any]:
    """Build the Scheme 1 typed payload for /v1/cancelOrder.

    `id` is the hex orderId returned by Arcus. `g/p/q/s/t` are absent (cancel
    does not carry those fields per the auth spec).
    """
    return {
        "ad": str(credentials["wallet"]).lower(),
        "ai": int(credentials["account_index"]),
        "ct": int(timestamp_ns),
        "id": str(order_id),
        "m": int(market_id),
        "op": _ARCUS_OP_CANCEL,
        "v": _ARCUS_PAYLOAD_VERSION,
    }


def _signed_post(
    credentials: Dict[str, Any],
    path: str,
    payload: Dict[str, Any],
    *,
    typed_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """POST to Arcus.

    When ``typed_payload`` is provided, Ed25519 signs the Scheme 1 typed
    payload (sorted-key compact JSON, no prefix) and that exact byte string is
    passed through to ``X-Signature``. The ``payload`` argument is still sent
    as the HTTP body (it carries the validation-friendly shape the server
    parses for business rules). ``X-Timestamp`` is set to ``typed_payload['ct']``
    as a decimal string of nanoseconds.

    When ``typed_payload`` is ``None`` (legacy / Scheme 2 paths, e.g.
    ``cancelAllOrders`` / ``setLeverage``), this falls back to signing the
    HTTP body JSON directly. That fallback matches the pre-typed-payload code
    path; it does NOT match the live placeOrder/cancelOrder/modifyOrder path
    and will fail signature verification on those endpoints.
    """
    if not credentials.get("private_key_hex"):
        raise RuntimeError(_ARCUS_PRIVATE_KEY_MISSING_CODE + ": ARCUS private key is required for signed writes.")
    private_key = _ed25519_private_key_from_hex(credentials["private_key_hex"])
    body = _canonical_json(payload)
    if typed_payload is not None:
        signed_bytes = _typed_payload_bytes(typed_payload)
        timestamp_ns = str(int(typed_payload["ct"]))
    else:
        signed_bytes = body
        timestamp_ns = str(int(time.time() * 1000) * 1_000_000)
    signature = private_key.sign(signed_bytes.encode("utf-8")).hex()
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


def _normalize_positions(
    positions_payload: Any,
    protections: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[CanonicalPosition]:
    positions_map = positions_payload if isinstance(positions_payload, dict) else {}
    protections = protections or {}
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
        prot = protections.get(symbol, {})
        positions.append(
            CanonicalPosition(
                symbol=symbol,
                side=side,
                size=_format_decimal_places(abs(size), size_precision),
                entry_price=_format_decimal_places(_decimal_or_zero(row.get("averageEntryPrice")), price_precision),
                pnl=_decimal_text(row.get("unrealizedPnl")),
                tp=prot.get("tp"),
                sl=prot.get("sl"),
                tp_count=prot.get("tp_count"),
                sl_count=prot.get("sl_count"),
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


def _aggregate_protection_orders(orders_payload: Any) -> Dict[str, Dict[str, Any]]:
    """Scan orders and attach per-symbol TP / SL summary.

    Arcus reports TPSL rows in the same `/v1/openOrders` payload as plain
    orders — distinguished by `tpslType` ("TAKE_PROFIT" / "STOP_LOSS") and
    `isPositionTPSL` (true for position-level closes). For position-level
    TPSL there is at most one TP and one SL per (account, market) — we
    surface the trigger price and the count. For non-position TPSLs
    (partialTpsl) there can be multiple; we keep a list.

    Returns a dict keyed by normalized symbol, each value containing:
        {
            "tp":           trigger price string or None,
            "tp_count":     int or None,
            "sl":           trigger price string or None,
            "sl_count":     int or None,
            "tp_orders":    list of orderIds (for set_tp/set_sl existing-target lookup),
            "sl_orders":    list of orderIds,
        }
    """
    out: Dict[str, Dict[str, Any]] = {}
    orders = orders_payload if isinstance(orders_payload, list) else []
    for row in orders:
        if not isinstance(row, dict):
            continue
        tpsl_type = str(row.get("tpslType") or "").strip().upper()
        if tpsl_type not in {"TAKE_PROFIT", "STOP_LOSS"}:
            continue
        # Only surface resting (UNTRIGGERED) orders — executed ones disappear
        # from /v1/openOrders automatically; canceled ones also disappear.
        status = str(row.get("status") or "").strip().upper()
        if status != "UNTRIGGERED":
            continue
        symbol = _normalize_symbol(row.get("marketDisplayName") or row.get("symbol") or row.get("market"))
        bucket = out.setdefault(symbol, {
            "tp": None,
            "tp_count": 0,
            "sl": None,
            "sl_count": 0,
            "tp_orders": [],
            "sl_orders": [],
        })
        trigger = _decimal_or_zero(row.get("triggerPrice"))
        if trigger <= 0:
            trigger = _decimal_or_zero(row.get("price"))
        order_id = str(row.get("orderId") or "")
        if tpsl_type == "TAKE_PROFIT":
            bucket["tp_count"] = (bucket["tp_count"] or 0) + 1
            bucket["tp_orders"].append(order_id)
            # For position-level closes the engine collapses to one row; we
            # keep the LAST seen trigger price (any will be equivalent in the
            # 1-per-market invariant).
            bucket["tp"] = _format_decimal_places(trigger, _decimal_places(trigger)) if trigger > 0 else bucket["tp"]
        else:
            bucket["sl_count"] = (bucket["sl_count"] or 0) + 1
            bucket["sl_orders"].append(order_id)
            bucket["sl"] = _format_decimal_places(trigger, _decimal_places(trigger)) if trigger > 0 else bucket["sl"]
    # Drop empty tp_count/sl_count so wizard renders "—" instead of "0"
    for sym, b in list(out.items()):
        if b["tp_count"] == 0:
            b["tp"] = None
            b["tp_count"] = None
        if b["sl_count"] == 0:
            b["sl"] = None
            b["sl_count"] = None
    return out


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
        protections = _aggregate_protection_orders(orders_payload.get("orders"))
        positions = _normalize_positions(account_payload.get("positions"), protections=protections)
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
    # Arcus writable schema (verified against live /v1/placeOrder validation):
    #   orderSide  "BUY"|"SELL"  (uppercase)
    #   orderType  "LIMIT"
    #   timeInForce  "GTT"|"IOC"|"FOK"|"ALO"
    #   quantity / price  decimal strings
    #   goodTilTime  microseconds, must be far enough in the future
    #   timestamp    nanoseconds
    side_upper = side.strip().upper()
    return {
        "address": credentials["wallet"],
        "marketId": int(market["market_id"]),
        "accountIndex": int(credentials["account_index"]),
        "orderSide": side_upper,
        "orderType": "LIMIT",
        "timeInForce": "GTT",
        "quantity": _format_decimal_places(quantity, int(market["size_precision"])),
        "price": _format_decimal_places(price, int(market["price_precision"])),
        "goodTilTime": str(_good_til_time_us(int(market["market_id"]))),
        "timestamp": int(time.time_ns()),
        "reduceOnly": bool(reduce_only),
        "clientId": client_id,
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
        # Build the Scheme 1 typed payload (the actual signing input) from the
        # same market/credentials/values used to build the body. The integer
        # tick/quantum conversions must come from the same Decimal values that
        # produced the body's decimal-string price/quantity, so reuse them.
        price_ticks = int((submitted_price / tick_size).to_integral_value())
        qty_quantums = int((submitted_volume / step_size).to_integral_value())
        good_til_us = int(payload["goodTilTime"])
        timestamp_ns = int(payload["timestamp"])
        typed_payload = _build_arcus_typed_payload_place(
            credentials=credentials,
            market_id=int(market["market_id"]),
            price_ticks=price_ticks,
            qty_quantums=qty_quantums,
            side=side,
            time_in_force="gtt",  # only TIF supported for resting orders per docs
            good_til_time_us=good_til_us,
            timestamp_ns=timestamp_ns,
            reduce_only=reduce_only,
            client_id=client_id,
        )
        response = _signed_post(credentials, "/v1/placeOrder", payload, typed_payload=typed_payload)
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


# ===========================================================================
# Ladder implementation
# ===========================================================================
#
# Mirrors the algorithm used by x_hyperliquid_agent / x_lighter_agent /
# x_raydium_agent / x_rise_agent. Differences specific to Arcus:
#
#   * Body for each child uses the Arcus writable schema (orderSide /
#     orderType / timeInForce / goodTilTime / timestamp) — built via
#     _build_new_order_payload_body().
#   * Each element's signature is the typed-payload signature for /v1/placeOrder
#     (Scheme 1, op=1), computed with the same builder + serializer the
#     single-order path uses.
#   * Children are submitted via POST /v1/batchPlaceOrders in chunks of 10
#     (the wizard contract; well within Arcus's per-batch limit of 100).
#   * Children whose notional is below Arcus's $10 floor are omitted
#     (Arcus rejects them outright) and NOT redistributed — the survivors
#     keep their original sizes. This matches the user spec and the
#     lighter/raydium simpler semantics.
#   * `r` is the typed payload's `r` field — 1 = reduce-only, 0 otherwise.
#     Arcus does not support reduce-only on ladders in this first cut.
#
# Half-Gaussian orientation (per the wizard docs / convention):
#   SELL: lowest volume at the START price (close to market) and largest
#         volume at the END price (far from market).
#   BUY : mirror image — largest volume at the START (high, close to market)
#         and lowest at the END (low, far from market).
# The weight function below produces this orientation identically for both
# sides by indexing weights so that index 0 → largest weight for SELL,
# smallest for BUY; we then assign prices so that SELL index 0 lands on the
# lowest price (start) and BUY index 0 lands on the highest price (start).

import math

_ARCUS_BATCH_SIZE = 10  # children per /v1/batchPlaceOrders request (user contract)
_ARCUS_MIN_NOTIONAL_USD = Decimal("10")  # Arcus rejects sub-$10 orders outright


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
    span = Decimal(order_count - 1)
    weights: List[Decimal] = []
    for index in range(order_count):
        # Half-Gaussian with σ=1 truncated to z∈[0,3]: index 0 → z=3 (smallest
        # weight), index N-1 → z=0 (largest weight). Same orientation as the
        # other agents; for SELL, lowest price (index 0 in price assignment) =
        # smallest volume; for BUY, the inverse.
        z = Decimal("3") * (span - Decimal(index)) / span
        weight = math.exp(-(float(z) ** 2) / 2.0)
        weights.append(Decimal(str(weight)))
    return weights


def _quantize_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        raise ValueError("INVALID_INCREMENT")
    units = (value / increment).to_integral_value(rounding=ROUND_HALF_UP)
    return units * increment


def _build_ladder_prices(start_price: Decimal, end_price: Decimal, order_count: int, price_increment: Decimal) -> List[Decimal]:
    if order_count <= 0:
        return []
    if order_count == 1:
        return [_quantize_to_increment((start_price + end_price) / Decimal("2"), price_increment)]
    span = end_price - start_price
    step = span / Decimal(order_count - 1)
    raw_prices = [start_price + step * Decimal(index) for index in range(order_count)]
    prices = [_quantize_to_increment(p, price_increment) for p in raw_prices]
    # Enforce monotonicity after tick quantization (which can collapse adjacent
    # prices to the same tick).
    if start_price <= end_price:
        for index in range(1, len(prices)):
            if prices[index] < prices[index - 1]:
                prices[index] = prices[index - 1]
    else:
        for index in range(1, len(prices)):
            if prices[index] > prices[index - 1]:
                prices[index] = prices[index - 1]
    return prices


def _allocate_ladder_sizes(total_volume: Decimal, order_count: int, size_increment: Decimal, distribution: str) -> Tuple[List[Decimal], Decimal]:
    if size_increment <= 0:
        raise ValueError("INVALID_INCREMENT")
    total_units = int((total_volume / size_increment).to_integral_value(rounding=ROUND_HALF_UP))
    if total_units < order_count:
        raise ValueError("INSUFFICIENT_VOLUME_FOR_ORDER_COUNT")
    weights = _ladder_distribution_weights(order_count, distribution)
    if not weights:
        raise ValueError("INVALID_ORDER_COUNT")
    total_weight = sum(weights, Decimal("0"))
    if total_weight <= 0:
        raise ValueError("INVALID_DISTRIBUTION")
    raw_units = [Decimal(total_units) * weight / total_weight for weight in weights]
    base_units = [int(unit.to_integral_value(rounding=ROUND_DOWN)) for unit in raw_units]
    residual = total_units - sum(base_units)
    remainders = [raw_units[index] - Decimal(base_units[index]) for index in range(order_count)]
    allocation = list(base_units)
    if residual > 0:
        order_indices = sorted(
            range(order_count),
            key=lambda index: (remainders[index], -index),
            reverse=True,
        )
        for index in order_indices[:residual]:
            allocation[index] += 1
    sizes = [Decimal(units) * size_increment for units in allocation]
    return sizes, Decimal(total_units) * size_increment


def _build_new_order_payload_body(
    *,
    credentials: Dict[str, Any],
    market_id: int,
    side: str,
    quantity: Decimal,
    price: Decimal,
    reduce_only: bool,
    client_id: str,
    good_til_time_us: int,
    timestamp_ns: int,
    size_precision: int,
    price_precision: int,
) -> Dict[str, Any]:
    """Build the Arcus HTTP body for a single-element placeOrder.

    Same shape as `_build_new_order_payload` but parameterized so the ladder
    can build N children in a tight loop without re-fetching the market.
    `good_til_time_us` and `timestamp_ns` are computed once per batch (shared
    `X-Timestamp` per the batch spec) and re-used across children.
    """
    return {
        "address": credentials["wallet"],
        "marketId": int(market_id),
        "accountIndex": int(credentials["account_index"]),
        "orderSide": "BUY" if side == "buy" else "SELL",
        "orderType": "LIMIT",
        "timeInForce": "GTT",
        "quantity": _format_decimal_places(quantity, int(size_precision)),
        "price": _format_decimal_places(price, int(price_precision)),
        "goodTilTime": str(good_til_time_us),
        "timestamp": int(timestamp_ns),
        "reduceOnly": bool(reduce_only),
        "clientId": str(client_id),
        "clientTime": str(timestamp_ns),  # docs: must equal X-Timestamp / ct
    }


def _build_arcus_ladder_children(
    *,
    credentials: Dict[str, Any],
    market_id: int,
    price_increment: Decimal,
    size_increment: Decimal,
    side: str,
    distribution: str,
    order_count: int,
    total_volume: Decimal,
    start_price: Decimal,
    end_price: Decimal,
    size_precision: int,
    price_precision: int,
    min_notional: Decimal,
    batch_id_prefix: str,
) -> Tuple[List[Dict[str, Any]], Decimal, int, int]:
    """Build the ladder child orders with omit-below-min-notional.

    Returns (children, kept_volume_increment_units, omitted_below_minimum_count,
    submitted_count). Children are dicts with the raw fields ready for
    `_build_typed_payload_for_child` + the HTTP body builder — this fn only
    does price/size math + the <$10 filter.

    Adjacent children whose price collapses to the same tick are merged (size
    summed) — same as every other agent's ladder.

    `min_notional` is in the market's quote currency (USD). A child is kept iff
    price × size ≥ min_notional; otherwise it is omitted (not redistributed).
    """
    prices = _build_ladder_prices(start_price, end_price, order_count, price_increment)
    sizes, submitted_volume = _allocate_ladder_sizes(total_volume, order_count, size_increment, distribution)
    children: List[Dict[str, Any]] = []
    omitted_below_minimum = 0
    kept_volume = Decimal("0")
    for index, (price, size) in enumerate(zip(prices, sizes)):
        if size <= 0:
            continue
        # Omit children below the $10 notional floor — Arcus rejects them
        # outright, and per the simpler semantics we don't redistribute.
        if min_notional is not None and price * size < min_notional:
            omitted_below_minimum += 1
            continue
        if children and children[-1]["price"] == price:
            children[-1]["size"] = children[-1]["size"] + size
            kept_volume += size
            continue
        children.append({
            "index": index,
            "price": price,
            "size": size,
            "client_id": f"{batch_id_prefix}-{index:03d}",
        })
        kept_volume += size
    return children, kept_volume, omitted_below_minimum, len(children)


def _sign_arcus_ladder_child(
    *,
    credentials: Dict[str, Any],
    market_id: int,
    price_ticks: int,
    qty_quantums: int,
    side: str,
    time_in_force: str,
    good_til_time_us: int,
    timestamp_ns: int,
    reduce_only: bool,
    client_id: str,
) -> Tuple[str, str]:
    """Build the typed payload for one ladder child and return (signature_hex, signed_msg).

    The signed message is returned alongside the signature so callers (and
    tests) can assert on the exact byte string that was signed.
    """
    typed = _build_arcus_typed_payload_place(
        credentials=credentials,
        market_id=market_id,
        price_ticks=price_ticks,
        qty_quantums=qty_quantums,
        side=side,
        time_in_force=time_in_force,
        good_til_time_us=good_til_time_us,
        timestamp_ns=timestamp_ns,
        reduce_only=reduce_only,
        client_id=client_id,
    )
    signed_msg = _typed_payload_bytes(typed)
    private_key = _ed25519_private_key_from_hex(credentials["private_key_hex"])
    signature_hex = private_key.sign(signed_msg.encode("utf-8")).hex()
    return signature_hex, signed_msg


def _submit_arcus_ladder_batch(
    credentials: Dict[str, Any],
    children: List[Dict[str, Any]],
    timestamp_ns: Optional[int] = None,
    good_til_time_us: Optional[int] = None,
) -> Dict[str, Any]:
    """POST /v1/batchPlaceOrders for one chunk of children (≤10).

    Each child is signed with its own typed payload; the `X-Signature` header
    is set to the first child's signature (Arcus verifies per-element). The
    shared `X-Timestamp` is the `ct` value inside every child's typed payload.

    IMPORTANT: Arcus treats duplicate `ct` values as replay attacks and
    rejects subsequent batches with `"request timestamp already used"`. To
    avoid this when a ladder spans more than one batch, callers should pass a
    FRESH `timestamp_ns` per batch (the ladder executor does this). If
    `timestamp_ns` is None, this fn generates one internally so the timestamp
    is always fresh per call.

    Returns the parsed JSON response (a `{"responses": [...], "rateLimit": {...}}`
    envelope). Raises RuntimeError on HTTP error or network failure.
    """
    if not children:
        return {"responses": [], "rateLimit": None}
    if timestamp_ns is None:
        timestamp_ns = int(time.time_ns())
    if good_til_time_us is None:
        good_til_time_us = (int(time.time() * 1000) + _ARCUS_GOOD_TIL_TIME_MIN_FUTURE_MS) * 1000
    first_signature: Optional[str] = None
    batch_body: List[Dict[str, Any]] = []
    for child in children:
        price = child["price"]
        size = child["size"]
        body = _build_new_order_payload_body(
            credentials=credentials,
            market_id=child["_market_id"],
            side=child["_side"],
            quantity=size,
            price=price,
            reduce_only=False,
            client_id=child["client_id"],
            good_til_time_us=good_til_time_us,
            timestamp_ns=timestamp_ns,
            size_precision=child["_size_precision"],
            price_precision=child["_price_precision"],
        )
        signature_hex, _signed_msg = _sign_arcus_ladder_child(
            credentials=credentials,
            market_id=child["_market_id"],
            price_ticks=child["_price_ticks"],
            qty_quantums=child["_qty_quantums"],
            side=child["_side"],
            time_in_force="gtt",
            good_til_time_us=good_til_time_us,
            timestamp_ns=timestamp_ns,
            reduce_only=False,
            client_id=child["client_id"],
        )
        body["signature"] = signature_hex
        if first_signature is None:
            first_signature = signature_hex
        batch_body.append(body)
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": _api_key_for_signing(credentials["api_signing_key"]),
        "X-Timestamp": str(timestamp_ns),
        "X-Signature": first_signature,
    }
    response = requests.post(
        f"{credentials['base_url']}/v1/batchPlaceOrders",
        headers=headers,
        data=json.dumps({"orders": batch_body}, separators=(",", ":"), ensure_ascii=False, sort_keys=False).encode("utf-8"),
        timeout=API_TIMEOUT_SECONDS,
    )
    try:
        payload_obj = response.json()
    except ValueError:
        payload_obj = {"raw": response.text}
    if response.status_code >= 400:
        raise RuntimeError(_format_arcus_error(response.status_code, payload_obj))
    return payload_obj if isinstance(payload_obj, dict) else {"raw": response.text}


def _execute_ladder(request: Dict[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="ladder", exchange=name, account=account,
            code="ACCOUNT_NOT_FOUND", message="Arcus account is not configured.",
        )

    requested_symbol = str(request.get("symbol") or "").strip()
    requested_side = str(request.get("side") or "").strip().lower()
    distribution = str(request.get("distribution") or "").strip().lower()
    try:
        order_count = int(str(request.get("order_count") or "").strip())
    except Exception:
        order_count = 0
    total_volume = _decimal_or_zero(request.get("total_volume"))
    start_price = _decimal_or_zero(request.get("start_price"))
    end_price = _decimal_or_zero(request.get("end_price"))

    if not requested_symbol:
        return make_failure(operation="ladder", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_side not in {"buy", "sell"}:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_SIDE", message="Side must be buy or sell.")
    if distribution not in {"uniform", "half_gaussian"}:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_DISTRIBUTION", message="Distribution must be uniform or half_gaussian.")
    if order_count <= 0:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_ORDER_COUNT", message="Order count must be positive.")
    if total_volume <= 0:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_VOLUME", message="Total volume must be positive.")
    if start_price <= 0 or end_price <= 0:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_PRICE", message="Start and end price must be positive.")
    if requested_side == "buy" and end_price >= start_price:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_LADDER_DIRECTION", message="BUY ladders require end price below start price.")
    if requested_side == "sell" and end_price <= start_price:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_LADDER_DIRECTION", message="SELL ladders require end price above start price.")

    # Resolve market metadata (live) — must succeed for tickSize/stepSize/precision.
    try:
        market = _resolve_market(requested_symbol)
    except ValueError as exc:
        return make_failure(operation="ladder", exchange=name, account=account, code=str(exc), message=sanitize_error_message(str(exc)))

    market_id = int(market["market_id"])
    tick_size = _decimal_or_zero(market["tick_size"])
    step_size = _decimal_or_zero(market["step_size"])
    size_precision = int(market["size_precision"])
    price_precision = int(market["price_precision"])
    min_notional = _decimal_or_zero(market.get("min_notional"))
    # Arcus's hard floor is $10 notional regardless of the market's per-order
    # min_notional (which is typically the same value). Whichever is larger.
    effective_min_notional = max(_ARCUS_MIN_NOTIONAL_USD, min_notional) if min_notional > 0 else _ARCUS_MIN_NOTIONAL_USD

    # Preflight: ensure total volume can produce at least 2 valid children.
    # Cheap heuristic: total_volume must exceed 2 × effective_min_notional.
    if total_volume < effective_min_notional * Decimal("2"):
        return make_failure(
            operation="ladder", exchange=name, account=account,
            code="INSUFFICIENT_VOLUME_FOR_ORDER_COUNT",
            message=f"Total volume {total_volume} is too small to produce two non-empty children above the {effective_min_notional} USD floor.",
        )

    # The actual `ct` (timestamp_ns) and `g` (good_til_time_us) are computed
    # per-batch by `_submit_arcus_ladder_batch` — that's why we don't set
    # them here. Doing them once globally would re-introduce the
    # "request timestamp already used" replay-protection failure when the
    # ladder spans more than one batch.

    batch_id_prefix = f"arcus-ladder-{uuid.uuid4().hex[:8]}"
    children, kept_volume, omitted_below_minimum, kept_count = _build_arcus_ladder_children(
        credentials=credentials,
        market_id=market_id,
        price_increment=tick_size,
        size_increment=step_size,
        side=requested_side,
        distribution=distribution,
        order_count=order_count,
        total_volume=total_volume,
        start_price=start_price,
        end_price=end_price,
        size_precision=size_precision,
        price_precision=price_precision,
        min_notional=effective_min_notional,
        batch_id_prefix=batch_id_prefix,
    )

    # Attach per-child derived values needed for the signed payload builder.
    # `good_til_time_us` is intentionally NOT set here — each batch computes
    # its own (the server-side 1-month-future check is per-element's `g`,
    # and we want a fresh `ct` per batch, which means a fresh `g` too).
    for child in children:
        child["_market_id"] = market_id
        child["_side"] = requested_side
        child["_price_ticks"] = int((child["price"] / tick_size).to_integral_value())
        child["_qty_quantums"] = int((child["size"] / step_size).to_integral_value())
        child["_size_precision"] = size_precision
        child["_price_precision"] = price_precision

    if kept_count < 2:
        ladder = CanonicalLadderResult(
            symbol=requested_symbol, side=requested_side, distribution=distribution,
            requested_order_count=order_count, submitted_order_count=0,
            requested_volume=_decimal_text(total_volume),
            submitted_volume=_decimal_text(Decimal("0")),
            batch_count=0, verified=False, partial=False, status="failed",
            accepted_child_count=0, omitted_order_count=order_count - kept_count,
            omitted_below_minimum=omitted_below_minimum,
            child_order_ids=[], batches=[],
        )
        return make_failure(
            operation="ladder", exchange=name, account=account,
            code="LADDER_TOO_FEW_VALID_CHILDREN",
            message=f"Fewer than two ladder children survived the ${_ARCUS_MIN_NOTIONAL_USD} notional filter ({kept_count} kept, {omitted_below_minimum} omitted below floor).",
            ladder=ladder,
        )

    # Submit in chunks of _ARCUS_BATCH_SIZE. Each batch gets its OWN fresh
    # `ct` (timestamp) — reusing one `ct` across multiple batches triggers
    # Arcus's replay protection ("request timestamp already used") and the
    # remaining batches all fail. The submission helper generates the fresh
    # `ct` internally when called without a timestamp argument.
    accepted_child_count = 0
    child_order_ids: List[int] = []
    batches: List[Dict[str, Any]] = []
    try:
        for chunk_start in range(0, len(children), _ARCUS_BATCH_SIZE):
            chunk = children[chunk_start: chunk_start + _ARCUS_BATCH_SIZE]
            response = _submit_arcus_ladder_batch(credentials, chunk)
            responses = response.get("responses") or []
            batch_index = len(batches)
            accepted_in_batch = 0
            for response_row, source_child in zip(responses, chunk):
                if not isinstance(response_row, dict):
                    continue
                if response_row.get("error"):
                    batches.append({
                        "batch_index": batch_index,
                        "submitted": len(chunk),
                        "accepted": accepted_in_batch,
                        "ok": False,
                        "reason": response_row.get("error"),
                        "child_errors": [response_row],
                    })
                    continue
                raw_oid = response_row.get("orderId")
                if raw_oid:
                    try:
                        if isinstance(raw_oid, str):
                            oid_int = int(raw_oid, 16) if all(c in "0123456789abcdefABCDEF" for c in raw_oid) else int(raw_oid)
                        else:
                            oid_int = int(raw_oid)
                        child_order_ids.append(oid_int)
                    except (TypeError, ValueError):
                        child_order_ids.append(raw_oid)  # type: ignore[arg-type]
                    accepted_in_batch += 1
                    accepted_child_count += 1
            batches.append({
                "batch_index": batch_index,
                "submitted": len(chunk),
                "accepted": accepted_in_batch,
                "ok": accepted_in_batch == len(chunk),
                "response_count": len(responses),
            })
    except Exception as exc:
        ladder = CanonicalLadderResult(
            symbol=requested_symbol, side=requested_side, distribution=distribution,
            requested_order_count=order_count, submitted_order_count=accepted_child_count,
            requested_volume=_decimal_text(total_volume),
            submitted_volume=_decimal_text(kept_volume),
            batch_count=len(batches),
            verified=False, partial=accepted_child_count > 0,
            status="partial" if accepted_child_count > 0 else "failed",
            accepted_child_count=accepted_child_count,
            omitted_order_count=order_count - kept_count,
            omitted_below_minimum=omitted_below_minimum,
            child_order_ids=child_order_ids or None,
            batches=batches or None,
        )
        return make_failure(
            operation="ladder", exchange=name, account=account,
            code="LADDER_SUBMISSION_FAILED",
            message=sanitize_error_message(str(exc)),
            ladder=ladder,
        )

    verified = accepted_child_count == kept_count
    status = "success" if verified else "partial"
    ladder = CanonicalLadderResult(
        symbol=requested_symbol, side=requested_side, distribution=distribution,
        requested_order_count=order_count,
        submitted_order_count=accepted_child_count,
        requested_volume=_decimal_text(total_volume),
        submitted_volume=_decimal_text(kept_volume),
        batch_count=len(batches),
        verified=verified,
        partial=not verified,
        status=status,
        accepted_child_count=accepted_child_count,
        omitted_order_count=order_count - kept_count,
        omitted_below_minimum=omitted_below_minimum,
        child_order_ids=child_order_ids or None,
        batches=batches or None,
    )
    return make_success(
        operation="ladder", exchange=name, account=credentials["account"],
        ladder=ladder,
    )


# ===========================================================================
# Position management (TP / SL / close_position / positions_management)
# ===========================================================================
#
# Arcus TPSL model (per https://docs.arcus.xyz/api-reference/exchange/place-order
# and /api-reference/authentication):
#
#   * TPSL orders MUST be submitted via POST /v1/batchPlaceOrders as elements
#     with `tpslType: "TAKE_PROFIT" | "STOP_LOSS"`, `orderType: "LIMIT"`,
#     `timeInForce: "GTT"`, and a trigger price in `stopPrice`.
#   * `isPositionTPSL: true` + `quantity: "0"` ⇒ the engine auto-resizes the
#     leg to the user's full open position at trigger time. We use this model
#     exclusively — it's the simplest and avoids any drift between the
#     leg's `quantity` and the actual position.
#   * `parentOrderId` is for `entryTpsl` (linked to an entry order); we OMIT it
#     because we never submit TPSL legs bundled with an entry from the
#     position-management path. Standalone TPSL only.
#   * At most one position-level TP and one position-level SL per
#     (account, market). A second placement of the same class is rejected
#     with `POSITION_TPSL_ALREADY_EXISTS` ⇒ we MUST find + cancel any
#     existing position-level TP/SL before placing a new one.
#   * The `body` for each TPSL element is the same single-order writable
#     schema (orderSide / orderType / timeInForce / goodTilTime / timestamp)
#     PLUS `tpslType` and `stopPrice` (and `isPositionTPSL: true` plus
#     `quantity: "0"`). The SIGNED payload uses the **same Scheme 1 typed
#     payload** as plain placeOrder, but with the documented Scheme-1
#     distinct op code for TPSL — specifically, **op=4 (TPSL)** for plain
#     TPSL placement, per the place-order reference ("Using a distinct op
#     prevents cross-replay between TPSL and plain placeOrder signatures").
#   * Reading open orders: a TPSL order reports with `type: "STOP_LOSS"`
#     or `"TAKE_PROFIT"` and (in some response shapes) a `stopPrice` field.
#     We use both as identification signals.

_ARCUS_OP_TPSL = 4  # distinct op for plain TPSL place (per auth spec)


def _position_action_result(
    *,
    operation: str,
    symbol: str,
    verified: bool,
    status: str,
    current_side: Optional[str] = None,
    current_size: Optional[str] = None,
    price: Optional[str] = None,
    removed: Optional[bool] = None,
    exchange_order_id: Optional[int] = None,
    message: Optional[str] = None,
) -> CanonicalPositionActionResult:
    return CanonicalPositionActionResult(
        operation=operation,
        symbol=symbol,
        verified=verified,
        status=status,
        current_side=current_side,
        current_size=current_size,
        price=price,
        removed=removed,
        exchange_order_id=exchange_order_id,
        message=message,
    )


def _arcus_position_context(account: str, symbol: str) -> Optional[Tuple[str, Decimal, Decimal, List[Dict[str, Any]]]]:
    """Read the live position + open orders for ``symbol``.

    Returns (side_lower, current_size, reference_mark_price, open_orders_rows)
    or None if the position is zero/missing.
    """
    credentials = _lookup_credentials(account)
    if credentials is None:
        return None
    try:
        account_payload = _public_get(credentials, "/v1/account")
        orders_payload = _public_get(credentials, "/v1/openOrders")
    except Exception:
        return None
    positions = account_payload.get("positions", {}) or {}
    position = None
    for _mid, row in positions.items():
        if not isinstance(row, dict):
            continue
        if str(row.get("marketDisplayName") or "").upper() == symbol.upper():
            position = row
            break
    if not position:
        return None
    side_text = str(position.get("side") or "").strip().lower()
    if side_text not in {"long", "short"}:
        return None
    size = _decimal_or_zero(position.get("size"))
    if size <= 0:
        return None
    mark = _decimal_or_zero(position.get("markPx"))
    open_orders = orders_payload.get("orders", []) or []
    return side_text, size, mark, open_orders


def _arcus_normalize_tpsl_side(position_side_lower: str) -> str:
    """A TPSL order's `orderSide` is OPPOSITE the position side (it closes
    the position when triggered). long → SELL, short → BUY.
    """
    return "sell" if position_side_lower == "long" else "buy"


def _arcus_find_existing_tpsl(
    open_orders: List[Dict[str, Any]],
    symbol: str,
    tpsl_class: str,
) -> Optional[Dict[str, Any]]:
    """Find a position-level TP or SL row for ``symbol`` in open orders.

    `tpsl_class` is ``"TP"`` (looking for `tpslType: TAKE_PROFIT`) or
    ``"SL"`` (looking for `tpslType: STOP_LOSS`). Matches on `type` field
    (Arcus reports TPSL rows as `type: "TAKE_PROFIT" | "STOP_LOSS"`). Any
    other value raises ``ValueError`` — callers should never use a class
    that isn't TP or SL.
    """
    tpsl_class_upper = str(tpsl_class or "").strip().upper()
    if tpsl_class_upper == "TP":
        want_type = "TAKE_PROFIT"
    elif tpsl_class_upper == "SL":
        want_type = "STOP_LOSS"
    else:
        raise ValueError(f"UNKNOWN_TPSL_CLASS: {tpsl_class!r}")
    for row in open_orders:
        if not isinstance(row, dict):
            continue
        if str(row.get("marketDisplayName") or "").upper() != symbol.upper():
            continue
        row_type = str(row.get("type") or "").strip().upper()
        if row_type == want_type:
            return row
    return None


def _arcus_signed_tpsl_element(
    *,
    credentials: Dict[str, Any],
    market_id: int,
    side: str,
    tpsl_type: str,
    stop_price_ticks: int,
    quantity_str: str,           # POSITIVE decimal string in body (e.g. "0.57470338")
    quantity_ticks: int,         # POSITIVE integer quantums in the signed typed payload
    timestamp_ns: int,
    good_til_time_us: int,
    client_id: str,
) -> Tuple[Dict[str, Any], str]:
    """Build the typed payload (op=4, TPSL) and HTTP body for one TPSL
    element, return (body, signature_hex).

    The body is what goes in the `/v1/batchPlaceOrders` `orders[]` array.
    The signature is the Ed25519 over the typed payload (sorted-key compact
    JSON, no prefix).

    `quantity_str` is the positive body quantity (current position size) —
    the body parser accepts a positive decimal string. The *typed payload*'s
    `q` field must be a positive integer (Arcus's signature verification
    rejects q=0), and is set to `quantity_ticks` here. Arcus's
    `isPositionTPSL: true` flag tells the engine to resize the leg to the
    full open position at trigger time — the body quantity is informational
    for the matcher but the engine overrides it on fill.
    """
    typed = _build_arcus_typed_payload_place(
        credentials=credentials,
        market_id=market_id,
        price_ticks=stop_price_ticks,
        qty_quantums=quantity_ticks,    # must be positive — q=0 in the typed payload is rejected
        side=side,
        time_in_force="gtt",
        good_til_time_us=good_til_time_us,
        timestamp_ns=timestamp_ns,
        reduce_only=True,         # TPSL closes the position — always reduce-only
        client_id=client_id,
    )
    # Override `op` to the distinct TPSL op code (op=4).
    typed["op"] = _ARCUS_OP_TPSL
    signed_msg = _typed_payload_bytes(typed)
    private_key = _ed25519_private_key_from_hex(credentials["private_key_hex"])
    signature_hex = private_key.sign(signed_msg.encode("utf-8")).hex()
    cache = _arcus_market_meta_cache(credentials, market_id)
    tick_size = cache["tickSize"]
    price_precision = cache["price_precision"]
    stop_price_str = _format_decimal_places(
        Decimal(stop_price_ticks) * tick_size, price_precision,
    )
    body = {
        "address": credentials["wallet"],
        "marketId": int(market_id),
        "accountIndex": int(credentials["account_index"]),
        "orderSide": "BUY" if side == "buy" else "SELL",
        "orderType": "LIMIT",
        "timeInForce": "GTT",
        "quantity": str(quantity_str),     # positive (current position size); engine resizes at trigger
        "price": stop_price_str,
        "goodTilTime": str(good_til_time_us),
        "timestamp": int(timestamp_ns),
        "reduceOnly": True,
        "tpslType": tpsl_type,
        "stopPrice": stop_price_str,
        "isPositionTPSL": True,
        "clientId": str(client_id),
        "clientTime": str(timestamp_ns),  # docs: must equal X-Timestamp / ct
        "signature": signature_hex,
    }
    return body, signature_hex


def _arcus_tick_size_for_market(credentials: Dict[str, Any], market_id: int) -> Decimal:
    """Cached lookup for tickSize; falls back to 0.001 if not cached."""
    return _arcus_market_meta_cache(credentials, market_id)["tickSize"]


def _arcus_price_precision_for_market(credentials: Dict[str, Any], market_id: int) -> int:
    return _arcus_market_meta_cache(credentials, market_id)["price_precision"]


def _arcus_market_meta_cache(credentials: Dict[str, Any], market_id: int) -> Dict[str, Any]:
    """Cache tickSize/pricePrecision for ``market_id`` within a request. Avoids
    re-fetching /v1/markets for every TPSL leg.
    """
    cache_attr = "_arcus_market_meta_cache_obj"
    cache = getattr(credentials, cache_attr, None)
    if cache is None or cache.get("market_id") != market_id:
        try:
            markets = _public_get(credentials, "/v1/markets")
        except Exception:
            return {"market_id": market_id, "tickSize": Decimal("0.001"), "price_precision": 3, "size_precision": 8}
        row = next((m for m in markets.get("markets", []) if m.get("marketId") == market_id), None)
        if not row:
            return {"market_id": market_id, "tickSize": Decimal("0.001"), "price_precision": 3, "size_precision": 8}
        cache = {
            "market_id": market_id,
            "tickSize": _decimal_or_zero(row.get("tickSize")) or Decimal("0.001"),
            "price_precision": _decimal_places(row.get("tickSize")) or 3,
            "size_precision": _decimal_places(row.get("stepSize")) or 8,
        }
        try:
            object.__setattr__(credentials, cache_attr, cache)
        except Exception:
            pass
    return cache


def _arcus_resolve_market_meta(credentials: Dict[str, Any], symbol: str) -> Optional[Dict[str, Any]]:
    """Resolve a symbol to (market_id, tickSize, price_precision, stepSize,
    size_precision, min_notional). Uses the existing `_resolve_market` helper
    which returns the same shape used by the ladder.
    """
    try:
        market = _resolve_market(symbol)
    except ValueError:
        return None
    return {
        "market_id": int(market["market_id"]),
        "tickSize": _decimal_or_zero(market["tick_size"]) or Decimal("0.001"),
        "price_precision": int(market["price_precision"]) or 3,
        "stepSize": _decimal_or_zero(market["step_size"]) or Decimal("0.00000001"),
        "size_precision": int(market["size_precision"]) or 8,
        "min_notional": _decimal_or_zero(market.get("min_notional")) or Decimal("5"),
    }


def _arcus_cancel_one_tpsl(
    credentials: Dict[str, Any],
    market_id: int,
    tpsl_row: Dict[str, Any],
) -> bool:
    """Cancel a single TPSL protection order. Returns True on success."""
    order_id = str(tpsl_row.get("orderId") or "").strip()
    if not order_id:
        return False
    timestamp_ns = int(time.time_ns())
    payload = {
        "address": credentials["wallet"],
        "marketId": market_id,
        "accountIndex": int(credentials["account_index"]),
        "kind": "orderId",
        "orderId": order_id,
        "timestamp": timestamp_ns,
    }
    typed = _build_arcus_typed_payload_cancel(
        credentials=credentials,
        market_id=market_id,
        order_id=order_id,
        timestamp_ns=timestamp_ns,
    )
    try:
        _signed_post(credentials, "/v1/cancelOrder", payload, typed_payload=typed)
        return True
    except Exception:
        return False


def _arcus_remove_existing_tpsl(
    *,
    account: str,
    request: Dict[str, Any],
    tpsl_class: str,
) -> CanonicalResponse:
    """Handle `set_tp` / `set_sl` with price=0 — remove the existing
    position-level TP or SL for the requested symbol.

    Mirrors the contract the other agents expose: price <= 0 means
    "remove the existing protection". If no matching protection exists,
    report success (idempotent no-op).
    """
    requested_symbol = str(request.get("symbol") or "").strip()
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation=("set_tp" if tpsl_class == "TP" else "set_sl"),
            exchange=name, account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Arcus account is not configured.",
        )
    # Verify there's an open position for this symbol (matches the
    # context-driven semantics of the other agents' implementations).
    ctx = _arcus_position_context(account, requested_symbol)
    position_side, current_size, _, open_orders = ctx if ctx is not None else (None, None, None, [])
    if ctx is None:
        return make_success(
            operation=("set_tp" if tpsl_class == "TP" else "set_sl"),
            exchange=name, account=account,
            position_action=_position_action_result(
                operation=("set_tp" if tpsl_class == "TP" else "set_sl"),
                symbol=requested_symbol, verified=True, status="success",
                removed=False, current_side=position_side,
                current_size=str(current_size) if current_size is not None else "0",
                message=f"No open position for {requested_symbol}; nothing to remove.",
            ),
        )
    market_meta = _arcus_resolve_market_meta(credentials, requested_symbol)
    if market_meta is None:
        return make_failure(
            operation=("set_tp" if tpsl_class == "TP" else "set_sl"),
            exchange=name, account=account,
            code="INSTRUMENT_NOT_FOUND",
            message=f"Market not found for {requested_symbol}.",
        )
    market_id = market_meta["market_id"]
    # At most one position-level TP/SL per (account, market) is the
    # documented invariant; surface ambiguity if it ever happens.
    matches = [o for o in open_orders
               if isinstance(o, dict)
               and str(o.get("marketDisplayName") or "").upper() == requested_symbol.upper()
               and str(o.get("tpslType") or "").strip().upper() == ("TAKE_PROFIT" if tpsl_class == "TP" else "STOP_LOSS")
               and str(o.get("status") or "").strip().upper() == "UNTRIGGERED"]
    if len(matches) > 1:
        return make_failure(
            operation=("set_tp" if tpsl_class == "TP" else "set_sl"),
            exchange=name, account=account,
            code="AMBIGUOUS_PROTECTION_STATE",
            message=f"Multiple matching {tpsl_class} orders found; cannot determine removal target safely.",
            position_action=_position_action_result(
                operation=("set_tp" if tpsl_class == "TP" else "set_sl"),
                symbol=requested_symbol, verified=False, status="failed",
                current_side=position_side, current_size=str(current_size),
            ),
        )
    if not matches:
        # Idempotent: nothing to remove.
        return make_success(
            operation=("set_tp" if tpsl_class == "TP" else "set_sl"),
            exchange=name, account=account,
            position_action=_position_action_result(
                operation=("set_tp" if tpsl_class == "TP" else "set_sl"),
                symbol=requested_symbol, verified=True, status="success",
                removed=False, current_side=position_side,
                current_size=str(current_size),
                message=f"No {'Take Profit' if tpsl_class == 'TP' else 'Stop Loss'} was set.",
            ),
        )
    target = matches[0]
    if not _arcus_cancel_one_tpsl(credentials, market_id, target):
        return make_failure(
            operation=("set_tp" if tpsl_class == "TP" else "set_sl"),
            exchange=name, account=account,
            code=("TP_REMOVAL_FAILED" if tpsl_class == "TP" else "SL_REMOVAL_FAILED"),
            message=f"Failed to cancel existing {'Take Profit' if tpsl_class == 'TP' else 'Stop Loss'}.",
            position_action=_position_action_result(
                operation=("set_tp" if tpsl_class == "TP" else "set_sl"),
                symbol=requested_symbol, verified=False, status="failed",
                removed=True, current_side=position_side,
                current_size=str(current_size),
            ),
        )
    return make_success(
        operation=("set_tp" if tpsl_class == "TP" else "set_sl"),
        exchange=name, account=credentials["account"],
        position_action=_position_action_result(
            operation=("set_tp" if tpsl_class == "TP" else "set_sl"),
            symbol=requested_symbol, verified=True, status="success",
            removed=True, current_side=position_side,
            current_size=str(current_size),
        ),
    )


def _arcus_batch_place_tpsl(
    credentials: Dict[str, Any],
    market_id: int,
    side: str,
    quantity_str: str,
    quantity_ticks: int,
    elements: List[Tuple[str, int, str]],  # [(tpsl_type, stop_price_ticks, client_id), ...]
) -> List[Dict[str, Any]]:
    """Submit 1 or 2 TPSL elements via /v1/batchPlaceOrders.

    Each element is (tpsl_type, stop_price_ticks, client_id). The function
    generates a fresh timestamp per call, signs each element with its own
    typed payload (op=4), and embeds the signatures in the body. `side` is
    the closing side ("buy" for a short, "sell" for a long) — it's the
    orderSide the body carries AND the side encoded in the typed payload's
    `s` field, so every leg in a single batch must share it. `quantity_str`
    is the body quantity (a positive decimal string, the current position
    size — the engine resizes to that at trigger time when isPositionTPSL=true).
    `quantity_ticks` is the typed-payload integer quantums (must be positive;
    Arcus rejects q=0 in the typed payload).

    Returns the per-element server response rows.
    """
    if not elements:
        return []
    timestamp_ns = int(time.time_ns())
    good_til_time_us = (int(time.time() * 1000) + _ARCUS_GOOD_TIL_TIME_MIN_FUTURE_MS) * 1000
    first_signature: Optional[str] = None
    body_orders: List[Dict[str, Any]] = []
    for tpsl_type, stop_price_ticks, client_id in elements:
        body, sig = _arcus_signed_tpsl_element(
            credentials=credentials,
            market_id=market_id,
            side=side,
            tpsl_type=tpsl_type,
            stop_price_ticks=stop_price_ticks,
            quantity_str=quantity_str,
            quantity_ticks=quantity_ticks,
            timestamp_ns=timestamp_ns,
            good_til_time_us=good_til_time_us,
            client_id=client_id,
        )
        body_orders.append(body)
        if first_signature is None:
            first_signature = sig
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": _api_key_for_signing(credentials["api_signing_key"]),
        "X-Timestamp": str(timestamp_ns),
        "X-Signature": first_signature,
    }
    # Arcus routes TPSL batches via the top-level `grouping` field. Without it
    # the server treats the batch as plain orders and the per-leg `tpslType` /
    # `stopPrice` / `isPositionTPSL` fields never take effect (or cause
    # "invalid order signature" because the engine routes the request to a
    # different validator than the one whose bytes we signed).
    # Per Arcus docs (api-reference/exchange/batch-place): grouping options
    # are na | partialTpsl | positionTpsl | entryTpsl. For position-close
    # TP/SL we use positionTpsl — isPositionTPSL is implicitly true.
    body_envelope = {"grouping": "positionTpsl", "orders": body_orders}
    response = requests.post(
        f"{credentials['base_url']}/v1/batchPlaceOrders",
        headers=headers,
        data=json.dumps(body_envelope, separators=(",", ":"), ensure_ascii=False, sort_keys=False).encode("utf-8"),
        timeout=API_TIMEOUT_SECONDS,
    )
    try:
        payload_obj = response.json()
    except ValueError:
        payload_obj = {"raw": response.text}
    if response.status_code >= 400:
        raise RuntimeError(_format_arcus_error(response.status_code, payload_obj))
    responses = payload_obj.get("responses") if isinstance(payload_obj, dict) else None
    return responses if isinstance(responses, list) else []


def _execute_set_tp(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    requested_symbol = str(request.get("symbol") or "").strip()
    price_text = str(request.get("price") or "").strip()
    if not requested_symbol:
        return make_failure(operation="set_tp", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    if not price_text:
        return make_failure(operation="set_tp", exchange=name, account=account, code="INVALID_TP_PRICE", message="TP price is required.")
    try:
        price_value = Decimal(price_text)
    except Exception:
        return make_failure(operation="set_tp", exchange=name, account=account, code="INVALID_TP_PRICE", message="TP price must be numeric.")
    if price_value <= 0:
        # `price == 0` is the wizard's "remove existing TP" intent (same
        # contract as the other agents). Cancel any existing position-level
        # TP for this symbol and report success.
        return _arcus_remove_existing_tpsl(
            account=account, request=request, tpsl_class="TP",
        )
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="set_tp", exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Arcus account is not configured.")
    ctx = _arcus_position_context(account, requested_symbol)
    if ctx is None:
        return make_failure(operation="set_tp", exchange=name, account=account, code="NO_OPEN_POSITION", message=f"No open position found for {requested_symbol}.")
    position_side, current_size, mark, open_orders = ctx
    market_meta = _arcus_resolve_market_meta(credentials, requested_symbol)
    if market_meta is None:
        return make_failure(operation="set_tp", exchange=name, account=account, code="INSTRUMENT_NOT_FOUND", message=f"Market not found for {requested_symbol}.")
    market_id = market_meta["market_id"]
    tick_size = market_meta["tickSize"]
    price_precision = market_meta["price_precision"]
    # Sanity: TP must be on the closing side relative to current price.
    if position_side == "long" and mark > 0 and price_value <= mark:
        return make_failure(
            operation="set_tp", exchange=name, account=account,
            code="INVALID_TP_PRICE",
            message=f"TP price {price_value} must be above the current mark {mark} for a long position.",
        )
    if position_side == "short" and mark > 0 and price_value >= mark:
        return make_failure(
            operation="set_tp", exchange=name, account=account,
            code="INVALID_TP_PRICE",
            message=f"TP price {price_value} must be below the current mark {mark} for a short position.",
        )
    # Convert price → ticks; if not on tick, snap to nearest tick.
    stop_price_ticks = int((price_value / tick_size).to_integral_value(rounding=ROUND_HALF_UP))
    if stop_price_ticks <= 0:
        return make_failure(operation="set_tp", exchange=name, account=account, code="INVALID_TP_PRICE", message="TP price below market tick granularity.")

    # Cancel any existing position-level TP first.
    existing = _arcus_find_existing_tpsl(open_orders, requested_symbol, "TP")
    if existing is not None:
        if not _arcus_cancel_one_tpsl(credentials, market_id, existing):
            return make_failure(
                operation="set_tp", exchange=name, account=account,
                code="TP_REMOVAL_FAILED",
                message="Failed to cancel existing Take Profit before placing new one.",
            )

    # Place the new TP as a 1-element batch.
    closing_side = _arcus_normalize_tpsl_side(position_side)
    # Body quantity is the current position size (engine resizes to that at
    # trigger time when isPositionTPSL=true; sending "0" is rejected as
    # "must be positive"). Round to the market's stepSize to avoid precision
    # errors. The typed payload's `q` field also needs a positive integer
    # quantums count (Arcus rejects q=0 in the typed payload too).
    step_size = _decimal_or_zero(market_meta["stepSize"])
    size_precision = int(market_meta["size_precision"])
    qty_units = int((current_size / step_size).to_integral_value()) if step_size > 0 else 0
    body_quantity = _format_decimal_places(qty_units * step_size, size_precision)
    try:
        responses = _arcus_batch_place_tpsl(
            credentials, market_id, closing_side,
            body_quantity, qty_units,
            [("TAKE_PROFIT", stop_price_ticks, f"arcus-tp-{uuid.uuid4().hex[:10]}")],
        )
    except Exception as exc:
        return make_failure(
            operation="set_tp", exchange=name, account=account,
            code="TP_PLACEMENT_FAILED",
            message=sanitize_error_message(str(exc)),
            position_action=_position_action_result(
                operation="set_tp", symbol=requested_symbol, verified=False,
                status="failed", current_side=position_side, current_size=str(current_size),
                price=str(price_value),
            ),
        )
    new_order_id: Optional[int] = None
    if responses:
        first = responses[0] if isinstance(responses[0], dict) else {}
        if first.get("error"):
            return make_failure(
                operation="set_tp", exchange=name, account=account,
                code="TP_PLACEMENT_FAILED",
                message=f"Arcus rejected the TP: {first.get('error')}",
                position_action=_position_action_result(
                    operation="set_tp", symbol=requested_symbol, verified=False,
                    status="failed", current_side=position_side, current_size=str(current_size),
                    price=str(price_value),
                ),
            )
        raw_oid = first.get("orderId")
        if raw_oid:
            try:
                new_order_id = int(raw_oid, 16) if isinstance(raw_oid, str) else int(raw_oid)
            except (TypeError, ValueError):
                new_order_id = None
    return make_success(
        operation="set_tp", exchange=name, account=credentials["account"],
        position_action=_position_action_result(
            operation="set_tp", symbol=requested_symbol, verified=new_order_id is not None,
            status="success" if new_order_id is not None else "submitted",
            current_side=position_side, current_size=str(current_size),
            price=str(price_value),
            exchange_order_id=new_order_id,
        ),
    )


def _execute_set_sl(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    requested_symbol = str(request.get("symbol") or "").strip()
    price_text = str(request.get("price") or "").strip()
    if not requested_symbol:
        return make_failure(operation="set_sl", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    if not price_text:
        return make_failure(operation="set_sl", exchange=name, account=account, code="INVALID_SL_PRICE", message="SL price is required.")
    try:
        price_value = Decimal(price_text)
    except Exception:
        return make_failure(operation="set_sl", exchange=name, account=account, code="INVALID_SL_PRICE", message="SL price must be numeric.")
    if price_value <= 0:
        # `price == 0` is the wizard's "remove existing SL" intent (same
        # contract as the other agents). Cancel any existing position-level
        # SL for this symbol and report success.
        return _arcus_remove_existing_tpsl(
            account=account, request=request, tpsl_class="SL",
        )
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="set_sl", exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Arcus account is not configured.")
    ctx = _arcus_position_context(account, requested_symbol)
    if ctx is None:
        return make_failure(operation="set_sl", exchange=name, account=account, code="NO_OPEN_POSITION", message=f"No open position found for {requested_symbol}.")
    position_side, current_size, mark, open_orders = ctx
    market_meta = _arcus_resolve_market_meta(credentials, requested_symbol)
    if market_meta is None:
        return make_failure(operation="set_sl", exchange=name, account=account, code="INSTRUMENT_NOT_FOUND", message=f"Market not found for {requested_symbol}.")
    market_id = market_meta["market_id"]
    tick_size = market_meta["tickSize"]
    # Sanity: SL must be on the protective side relative to current price.
    if position_side == "long" and mark > 0 and price_value >= mark:
        return make_failure(
            operation="set_sl", exchange=name, account=account,
            code="INVALID_SL_PRICE",
            message=f"SL price {price_value} must be below the current mark {mark} for a long position.",
        )
    if position_side == "short" and mark > 0 and price_value <= mark:
        return make_failure(
            operation="set_sl", exchange=name, account=account,
            code="INVALID_SL_PRICE",
            message=f"SL price {price_value} must be above the current mark {mark} for a short position.",
        )
    stop_price_ticks = int((price_value / tick_size).to_integral_value(rounding=ROUND_HALF_UP))
    if stop_price_ticks <= 0:
        return make_failure(operation="set_sl", exchange=name, account=account, code="INVALID_SL_PRICE", message="SL price below market tick granularity.")

    # Cancel any existing position-level SL first.
    existing = _arcus_find_existing_tpsl(open_orders, requested_symbol, "SL")
    if existing is not None:
        if not _arcus_cancel_one_tpsl(credentials, market_id, existing):
            return make_failure(
                operation="set_sl", exchange=name, account=account,
                code="SL_REMOVAL_FAILED",
                message="Failed to cancel existing Stop Loss before placing new one.",
            )

    # Place the new SL.
    closing_side = _arcus_normalize_tpsl_side(position_side)
    # Body quantity is the current position size (engine resizes at trigger
    # time when isPositionTPSL=true). Typed payload `q` must also be positive.
    step_size = _decimal_or_zero(market_meta["stepSize"])
    size_precision = int(market_meta["size_precision"])
    qty_units = int((current_size / step_size).to_integral_value()) if step_size > 0 else 0
    body_quantity = _format_decimal_places(qty_units * step_size, size_precision)
    try:
        responses = _arcus_batch_place_tpsl(
            credentials, market_id, closing_side,
            body_quantity, qty_units,
            [("STOP_LOSS", stop_price_ticks, f"arcus-sl-{uuid.uuid4().hex[:10]}")],
        )
    except Exception as exc:
        return make_failure(
            operation="set_sl", exchange=name, account=account,
            code="SL_PLACEMENT_FAILED",
            message=sanitize_error_message(str(exc)),
            position_action=_position_action_result(
                operation="set_sl", symbol=requested_symbol, verified=False,
                status="failed", current_side=position_side, current_size=str(current_size),
                price=str(price_value),
            ),
        )
    new_order_id: Optional[int] = None
    if responses:
        first = responses[0] if isinstance(responses[0], dict) else {}
        if first.get("error"):
            return make_failure(
                operation="set_sl", exchange=name, account=account,
                code="SL_PLACEMENT_FAILED",
                message=f"Arcus rejected the SL: {first.get('error')}",
                position_action=_position_action_result(
                    operation="set_sl", symbol=requested_symbol, verified=False,
                    status="failed", current_side=position_side, current_size=str(current_size),
                    price=str(price_value),
                ),
            )
        raw_oid = first.get("orderId")
        if raw_oid:
            try:
                new_order_id = int(raw_oid, 16) if isinstance(raw_oid, str) else int(raw_oid)
            except (TypeError, ValueError):
                new_order_id = None
    return make_success(
        operation="set_sl", exchange=name, account=credentials["account"],
        position_action=_position_action_result(
            operation="set_sl", symbol=requested_symbol, verified=new_order_id is not None,
            status="success" if new_order_id is not None else "submitted",
            current_side=position_side, current_size=str(current_size),
            price=str(price_value),
            exchange_order_id=new_order_id,
        ),
    )


def _execute_close_position(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Close the open position for ``symbol`` by:
      1. removing any existing position-level TP / SL (so they don't fire
         on a tiny position while the close is in flight);
      2. cancelling all resting orders for that symbol+side;
      3. placing a MARKET IOC order on the closing side for the full size.
    """
    requested_symbol = str(request.get("symbol") or "").strip()
    if not requested_symbol:
        return make_failure(operation="close_position", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="close_position", exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Arcus account is not configured.")
    ctx = _arcus_position_context(account, requested_symbol)
    if ctx is None:
        return make_failure(operation="close_position", exchange=name, account=account, code="NO_OPEN_POSITION", message=f"No open position found for {requested_symbol}.")
    position_side, current_size, mark, open_orders = ctx
    market_meta = _arcus_resolve_market_meta(credentials, requested_symbol)
    if market_meta is None:
        return make_failure(operation="close_position", exchange=name, account=account, code="INSTRUMENT_NOT_FOUND", message=f"Market not found for {requested_symbol}.")
    market_id = market_meta["market_id"]
    tick_size = market_meta["tickSize"]
    step_size = market_meta["stepSize"]
    size_precision = market_meta["size_precision"]
    price_precision = market_meta["price_precision"]

    # 1) Remove existing TP / SL.
    existing_tp = _arcus_find_existing_tpsl(open_orders, requested_symbol, "TP")
    existing_sl = _arcus_find_existing_tpsl(open_orders, requested_symbol, "SL")
    if existing_tp is not None:
        _arcus_cancel_one_tpsl(credentials, market_id, existing_tp)
    if existing_sl is not None:
        _arcus_cancel_one_tpsl(credentials, market_id, existing_sl)

    # 2) Cancel all other open orders for the symbol on the closing side.
    closing_side = _arcus_normalize_tpsl_side(position_side)
    for row in list(open_orders):
        if not isinstance(row, dict):
            continue
        if str(row.get("marketDisplayName") or "").upper() != requested_symbol.upper():
            continue
        if str(row.get("side") or "").strip().lower() != closing_side:
            continue
        # Skip the TPSL rows we just cancelled (they may still be in
        # the local open_orders snapshot).
        oid = str(row.get("orderId") or "").strip()
        if not oid:
            continue
        ts_ns = int(time.time_ns())
        cancel_body = {
            "address": credentials["wallet"],
            "marketId": market_id,
            "accountIndex": int(credentials["account_index"]),
            "kind": "orderId",
            "orderId": oid,
            "timestamp": ts_ns,
        }
        try:
            typed = _build_arcus_typed_payload_cancel(
                credentials=credentials, market_id=market_id, order_id=oid, timestamp_ns=ts_ns,
            )
            _signed_post(credentials, "/v1/cancelOrder", cancel_body, typed_payload=typed)
        except Exception:
            pass

    # 3) Place a MARKET IOC order for the full size. Slippage bound is
    #    ±10% of the current mark, per docs.
    if mark <= 0:
        return make_failure(
            operation="close_position", exchange=name, account=account,
            code="MARK_PRICE_UNAVAILABLE",
            message="Mark price unavailable; cannot compute slippage bound for market close.",
            position_action=_position_action_result(
                operation="close_position", symbol=requested_symbol, verified=False,
                status="failed", current_side=position_side, current_size=str(current_size),
            ),
        )
    if closing_side == "sell":
        # Closing a long: sell slightly below mark (cap the slippage)
        slip_price = mark * Decimal("0.99")
    else:
        # Closing a short: buy slightly above mark
        slip_price = mark * Decimal("1.01")
    slip_price_ticks = int((slip_price / tick_size).to_integral_value(rounding=ROUND_HALF_UP))
    slip_price_str = _format_decimal_places(
        Decimal(slip_price_ticks) * tick_size, price_precision,
    )
    timestamp_ns = int(time.time_ns())
    good_til_time_us = (int(time.time() * 1000) + _ARCUS_GOOD_TIL_TIME_MIN_FUTURE_MS) * 1000
    client_id = f"arcus-close-{uuid.uuid4().hex[:10]}"
    typed_close = _build_arcus_typed_payload_place(
        credentials=credentials,
        market_id=market_id,
        price_ticks=slip_price_ticks,
        qty_quantums=int((current_size / step_size).to_integral_value()),
        side=closing_side,
        time_in_force="ioc",  # market-style close: immediate-or-cancel
        good_til_time_us=good_til_time_us,
        timestamp_ns=timestamp_ns,
        reduce_only=True,
        client_id=client_id,
    )
    signed_msg = _typed_payload_bytes(typed_close)
    priv = _ed25519_private_key_from_hex(credentials["private_key_hex"])
    sig_hex = priv.sign(signed_msg.encode("utf-8")).hex()
    close_body = {
        "address": credentials["wallet"],
        "marketId": market_id,
        "accountIndex": int(credentials["account_index"]),
        "orderSide": "BUY" if closing_side == "buy" else "SELL",
        "orderType": "MARKET",
        "timeInForce": "IOC",
        "quantity": _format_decimal_places(current_size, size_precision),
        "price": slip_price_str,
        "goodTilTime": str(good_til_time_us),
        "timestamp": timestamp_ns,
        "reduceOnly": True,
        "clientId": client_id,
        "clientTime": str(timestamp_ns),
        "signature": sig_hex,
    }
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": _api_key_for_signing(credentials["api_signing_key"]),
        "X-Timestamp": str(timestamp_ns),
        "X-Signature": sig_hex,
    }
    try:
        response = requests.post(
            f"{credentials['base_url']}/v1/placeOrder",
            headers=headers,
            data=json.dumps(close_body, separators=(",", ":"), ensure_ascii=False, sort_keys=False).encode("utf-8"),
            timeout=API_TIMEOUT_SECONDS,
        )
        try:
            resp_payload = response.json()
        except ValueError:
            resp_payload = {"raw": response.text}
        if response.status_code >= 400:
            return make_failure(
                operation="close_position", exchange=name, account=account,
                code="CLOSE_POSITION_FAILED",
                message=_format_arcus_error(response.status_code, resp_payload),
                position_action=_position_action_result(
                    operation="close_position", symbol=requested_symbol, verified=False,
                    status="failed", current_side=position_side, current_size=str(current_size),
                ),
            )
    except Exception as exc:
        return make_failure(
            operation="close_position", exchange=name, account=account,
            code="CLOSE_POSITION_FAILED",
            message=sanitize_error_message(str(exc)),
            position_action=_position_action_result(
                operation="close_position", symbol=requested_symbol, verified=False,
                status="failed", current_side=position_side, current_size=str(current_size),
            ),
        )

    # Re-read position to confirm close.
    try:
        time.sleep(1)
        post = _arcus_position_context(account, requested_symbol)
        verified = post is None or post[1] == 0
    except Exception:
        verified = False
    new_order_id = None
    raw = resp_payload.get("orderId") if isinstance(resp_payload, dict) else None
    if raw:
        try:
            new_order_id = int(raw, 16) if isinstance(raw, str) else int(raw)
        except (TypeError, ValueError):
            new_order_id = None
    return make_success(
        operation="close_position", exchange=name, account=credentials["account"],
        position_action=_position_action_result(
            operation="close_position", symbol=requested_symbol, verified=verified,
            status="success" if verified else "submitted",
            current_side=position_side, current_size=str(current_size),
            exchange_order_id=new_order_id,
        ),
    )


def _cancel_order_group(request: Dict[str, Any]) -> CanonicalResponse:
    account = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(operation="cancel_order_group", exchange=name, account=account, code="ACCOUNT_NOT_FOUND", message="Arcus account is not configured.")
    symbol = str(request.get("symbol") or "").strip().upper()
    side = str(request.get("side") or "").strip().lower()
    if not symbol:
        return make_failure(operation="cancel_order_group", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    if side not in {"buy", "sell"}:
        return make_failure(operation="cancel_order_group", exchange=name, account=account, code="INVALID_SIDE", message="Side must be buy or sell.")
    try:
        market = _resolve_market(symbol)
        market_id = int(market["market_id"])
    except ValueError as exc:
        return make_failure(operation="cancel_order_group", exchange=name, account=account, code=str(exc), message=sanitize_error_message(str(exc)))
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
            return make_success(operation="cancel_order_group", exchange=name, account=credentials["account"], cancel_group=cancel_group)
        cancelled = 0
        batches: List[Dict[str, Any]] = []
        for target in targets:
            order_id = target["order_id"]
            try:
                timestamp_ns = int(time.time_ns())
                payload = {
                    "address": credentials["wallet"],
                    "marketId": market_id,
                    "accountIndex": int(credentials["account_index"]),
                    "kind": "orderId",
                    "orderId": order_id,
                    "timestamp": timestamp_ns,
                }
                typed_payload = _build_arcus_typed_payload_cancel(
                    credentials=credentials,
                    market_id=market_id,
                    order_id=order_id,
                    timestamp_ns=timestamp_ns,
                )
                _signed_post(credentials, "/v1/cancelOrder", payload, typed_payload=typed_payload)
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
                return make_failure(operation="cancel_order_group", exchange=name, account=credentials["account"], code="CANCEL_FAILED", message=reason_text, cancel_group=cancel_group)
        after = _fetch_open_orders_for_account(credentials)
        remaining = [row for row in after if _normalize_symbol(row.get("marketDisplayName") or row.get("symbol") or row.get("market")) == target_symbol and str(row.get("side") or "").strip().lower() == side]
        confirmed_absent = len(targets) - len(remaining)
        verified = len(remaining) == 0
        cancel_group = CanonicalCancelGroupResult(symbol=symbol, side=side, targeted_order_count=len(targets), cancelled_order_count=cancelled, confirmed_absent_count=confirmed_absent, remaining_target_count=len(remaining), verified=verified, partial=not verified, status="success" if verified else "partial", batch_count=len(batches), batches=batches)
        if verified:
            return make_success(operation="cancel_order_group", exchange=name, account=credentials["account"], cancel_group=cancel_group)
        return make_failure(operation="cancel_order_group", exchange=name, account=credentials["account"], code="VERIFICATION_FAILED", message="Cancellation could not be verified.", cancel_group=cancel_group)
    except Exception as exc:
        if isinstance(exc, RuntimeError) and str(exc).startswith(_ARCUS_PRIVATE_KEY_MISSING_CODE):
            return make_failure(operation="cancel_order_group", exchange=name, account=account, code="ARCUS_KEY_MISSING", message="Arcus private key is not configured. Set ARCUS_<ACCOUNT>_PRIVATE_KEY in ~/.hermes/.env.")
        return make_failure(operation="cancel_order_group", exchange=name, account=account, code="CANCEL_FAILED", message=sanitize_error_message(str(exc)))


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
    if operation in ("cancel_order_group", "cancel_orders"):
        return _cancel_order_group(request)
    if operation == "ladder":
        return _execute_ladder(request)
    if operation == "positions_management":
        return _positions_orders(account)
    if operation == "set_tp":
        return _execute_set_tp(account, request)
    if operation == "set_sl":
        return _execute_set_sl(account, request)
    if operation == "close_position":
        return _execute_close_position(account, request)
    return make_failure(operation=operation, exchange=name, account=account, code="NOT_IMPLEMENTED", message=f"Arcus does not implement '{operation}' yet.")