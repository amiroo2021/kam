"""Nado exchange agent.

Owns all Nado-specific behavior for the /trade stack.

Credentials (``.env`` / environment):
  ``NADO_<ALIAS>_SUBACCOUNT_OWNER``  wallet address (required)
  ``NADO_<ALIAS>_PRIVATE_KEY``       owner or linked-signer key (required for writes)
  ``NADO_<ALIAS>_SUBACCOUNT_NAME``   optional, default ``default``
  ``NADO_<ALIAS>_GATEWAY_URL``       optional REST base

Docs: https://docs.nado.xyz/developer-resources/api
Gateway: ``https://api.prod.nado.xyz/gateway/v1``

Operations:
  - balance
  - positions_orders / positions_management
  - new_order (limit)
  - ladder (uniform / half_gaussian child limits)
  - cancel_order_group
  - resolve_instrument / list_instruments / market_price

TradeDesk and the Telegram wizard MUST remain exchange-agnostic.
"""

from __future__ import annotations
from plugins.trade.candles import handle_candles_operation, has_native_candles

import gzip
import json
import logging
import math
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from eth_account import Account
from eth_account.messages import encode_typed_data

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
    CanonicalTickersBatch,
    make_failure,
    make_success,
    normalize_balance,
    sanitize_error_message,
)

logger = logging.getLogger(__name__)

name = "nado"

DEFAULT_GATEWAY_REST = "https://api.prod.nado.xyz/gateway/v1"
DEFAULT_TRIGGER_REST = "https://api.prod.nado.xyz/trigger/v1"
DEFAULT_SUBACCOUNT_NAME = "default"
API_TIMEOUT_SECONDS = 25
X18 = Decimal("1000000000000000000")
X18_INT = 10**18
# recv_time window: engine accepts current < recv_time <= current + 100s
_NONCE_RECV_AHEAD_MS = 50_000
_ORDER_TTL_SECONDS = 7 * 24 * 3600
_APPENDIX_DEFAULT = 1  # protocol version 1, default limit
# Agent-side safety cap only (not an exchange hard max). Nado gateway allows
# ~600 place_order/min with spot leverage (~10/sec). Cap at 200 so a single
# /trade ladder cannot runaway while still supporting large grids.
LADDER_ABSOLUTE_MAX_ORDERS = 200
# Serial pause kept as fallback; parallel path uses workers instead.
LADDER_CHILD_PAUSE_SECONDS = 0.05
# Nado allows ~10 place_order/sec with leverage; 8 workers keeps headroom.
LADDER_MAX_WORKERS = 8

_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_OWNER_ALIASES = ("SUBACCOUNT_OWNER", "OWNER", "ADDRESS", "WALLET")
_KEY_ALIASES = ("PRIVATE_KEY", "PRIVATEKEY", "SIGNER_KEY", "SECRET")
_NAME_ALIASES = ("SUBACCOUNT_NAME", "SUBACCOUNT", "NAME")
_BASE_ALIASES = ("GATEWAY_URL", "BASE_URL", "API_URL")

_symbols_cache: Dict[str, Any] = {"ts": 0.0, "by_symbol": {}, "by_pid": {}}
_contracts_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_CACHE_TTL = 300.0

_USER_AGENT = "kam-nado-agent/1.0 (+https://github.com/amiroo2021/kam)"


# ---------------------------------------------------------------------------
# Env / credentials
# ---------------------------------------------------------------------------


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


def _combined_nado_env() -> Dict[str, Tuple[str, str, str]]:
    out: Dict[str, Tuple[str, str, str]] = {}
    for k, v in os.environ.items():
        if k.upper().startswith("NADO_"):
            out.setdefault(k.upper(), (k, str(v), "env"))
    for k, v in _load_dotenv_values(_hermes_home() / ".env").items():
        if k.upper().startswith("NADO_"):
            out.setdefault(k.upper(), (k, str(v), "dotenv"))
    return out


def _parse_alias_and_suffix(upper_key: str) -> Optional[Tuple[str, str]]:
    if not upper_key.startswith("NADO_"):
        return None
    rest = upper_key[len("NADO_") :]
    known = sorted(
        set(_OWNER_ALIASES + _KEY_ALIASES + _NAME_ALIASES + _BASE_ALIASES),
        key=len,
        reverse=True,
    )
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
    for upper_key, (_actual, value, _src) in _combined_nado_env().items():
        parsed = _parse_alias_and_suffix(upper_key)
        if parsed is None:
            continue
        alias_upper, suffix = parsed
        alias = alias_upper.lower()
        slot = buckets.setdefault(alias, {})
        val = value.strip()
        if suffix in _OWNER_ALIASES:
            slot.setdefault("owner", val)
        elif suffix in _KEY_ALIASES:
            slot.setdefault("private_key", val)
        elif suffix in _NAME_ALIASES:
            slot.setdefault("subaccount_name", val)
        elif suffix in _BASE_ALIASES:
            slot.setdefault("gateway_url", val.rstrip("/"))
    complete: Dict[str, Dict[str, str]] = {}
    for alias, fields in buckets.items():
        if fields.get("owner"):
            complete[alias] = fields
    return complete


def list_accounts() -> List[str]:
    return sorted(_discover_credential_map().keys())


def capabilities() -> List[str]:
    return [
        "balance",
        "positions_orders",
        "positions_management",
        "new_order",
        "ladder",
        "cancel_order_group",
        "set_tp",
        "set_sl",
        "close_position",
        "resolve_instrument",
        "list_instruments",
        "market_price",
        "candles",
        "get_tickers",
    ]


def _lookup_credentials(account: str) -> Optional[Dict[str, str]]:
    alias = str(account or "").strip().lower()
    if not alias:
        return None
    fields = _discover_credential_map().get(alias)
    if not fields or not fields.get("owner"):
        return None
    owner = fields["owner"].strip()
    if not owner.startswith("0x") and not owner.startswith("0X"):
        owner = "0x" + owner
    pk = (fields.get("private_key") or "").strip()
    if pk and not pk.startswith("0x"):
        pk = "0x" + pk
    return {
        "account": alias,
        "owner": owner,
        "private_key": pk,
        "subaccount_name": (fields.get("subaccount_name") or DEFAULT_SUBACCOUNT_NAME).strip()
        or DEFAULT_SUBACCOUNT_NAME,
        "gateway_url": fields.get("gateway_url") or DEFAULT_GATEWAY_REST,
    }


def _require_signer(credentials: Mapping[str, str]) -> str:
    pk = str(credentials.get("private_key") or "").strip()
    if not pk:
        raise ValueError("MISSING_PRIVATE_KEY")
    # Validate key parses
    Account.from_key(pk)
    return pk


def _subaccount_bytes32(owner: str, subaccount_name: str) -> str:
    addr = owner.lower().replace("0x", "")
    if len(addr) != 40 or any(c not in "0123456789abcdef" for c in addr):
        raise ValueError("Invalid subaccount owner address")
    name = str(subaccount_name or DEFAULT_SUBACCOUNT_NAME)
    name_bytes = name.encode("utf-8")[:12]
    name_bytes = name_bytes + b"\x00" * (12 - len(name_bytes))
    return "0x" + addr + name_bytes.hex()


def _sender_hex(credentials: Mapping[str, str]) -> str:
    return _subaccount_bytes32(credentials["owner"], credentials["subaccount_name"])


def _redact(text: Any, credentials: Optional[Mapping[str, str]] = None) -> str:
    rendered = str(text or "")
    if credentials:
        for k in ("private_key", "owner"):
            v = str(credentials.get(k) or "").strip()
            if len(v) >= 8:
                rendered = rendered.replace(v, "***")
                if v.startswith("0x") and len(v) > 10:
                    rendered = rendered.replace(v[2:], "***")
                    rendered = rendered.replace(v[2:].lower(), "***")
    return rendered


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _decode_body(raw: bytes, content_encoding: str = "") -> bytes:
    enc = (content_encoding or "").lower()
    if "gzip" in enc or raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except Exception:  # noqa: BLE001
            pass
    return raw


def _http_json(
    credentials: Mapping[str, str],
    *,
    path: str,
    payload: Mapping[str, Any],
) -> Dict[str, Any]:
    base = str(credentials.get("gateway_url") or DEFAULT_GATEWAY_REST).rstrip("/")
    url = base + path
    body = json.dumps(dict(payload)).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:
            raw = _decode_body(resp.read(), resp.headers.get("Content-Encoding", ""))
            parsed = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            err_body = _decode_body(
                exc.read(),
                exc.headers.get("Content-Encoding", "") if exc.headers else "",
            )
            parsed = json.loads(err_body.decode("utf-8"))
            if isinstance(parsed, dict):
                return parsed
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"HTTP {exc.code} on Nado {path}: {exc.reason}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Nado returned a non-object JSON payload.")
    return parsed


def _gateway_query(credentials: Mapping[str, str], payload: Mapping[str, Any]) -> Dict[str, Any]:
    return _http_json(credentials, path="/query", payload=payload)


def _gateway_execute(credentials: Mapping[str, str], payload: Mapping[str, Any]) -> Dict[str, Any]:
    return _http_json(credentials, path="/execute", payload=payload)


# ---------------------------------------------------------------------------
# Math / formatting
# ---------------------------------------------------------------------------


def _x18_to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0")) / X18
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _to_x18_int(value: Decimal, *, rounding=ROUND_DOWN) -> int:
    scaled = (value * X18).to_integral_value(rounding=rounding)
    return int(scaled)


def _format_decimal(value: Decimal) -> str:
    q = value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    text = format(q.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _round_to_step_x18(amount_x18: int, step_x18: int, *, rounding=ROUND_DOWN) -> int:
    if step_x18 <= 0:
        return amount_x18
    if rounding == ROUND_UP:
        if amount_x18 <= 0:
            return 0
        return ((amount_x18 + step_x18 - 1) // step_x18) * step_x18
    return (amount_x18 // step_x18) * step_x18


def _gen_nonce(ahead_ms: int = _NONCE_RECV_AHEAD_MS) -> int:
    ms = int(time.time() * 1000) + max(1, min(ahead_ms, 90_000))
    return (ms << 20) + secrets.randbelow(1 << 20)


def _product_verifying_contract(product_id: int) -> str:
    return "0x" + int(product_id).to_bytes(20, "big").hex()


def _hex_to_bytes32(value: str) -> bytes:
    h = value[2:] if value.startswith("0x") else value
    raw = bytes.fromhex(h)
    if len(raw) > 32:
        raise ValueError("bytes32 overflow")
    return raw + b"\x00" * (32 - len(raw))


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def _ensure_contracts(credentials: Mapping[str, str]) -> Dict[str, Any]:
    now = time.time()
    if _contracts_cache["data"] and now - float(_contracts_cache["ts"]) < _CACHE_TTL:
        return dict(_contracts_cache["data"])
    payload = _gateway_query(credentials, {"type": "contracts"})
    if str(payload.get("status") or "").lower() != "success":
        raise RuntimeError(str(payload.get("error") or "contracts query failed"))
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise RuntimeError("contracts payload missing data")
    out = {
        "chain_id": int(data.get("chain_id")),
        "endpoint_addr": str(data.get("endpoint_addr") or "").lower(),
    }
    _contracts_cache["ts"] = now
    _contracts_cache["data"] = out
    return dict(out)


def _ensure_symbols(credentials: Mapping[str, str]) -> Tuple[Dict[str, Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    now = time.time()
    if _symbols_cache["by_symbol"] and now - float(_symbols_cache["ts"]) < _CACHE_TTL:
        return dict(_symbols_cache["by_symbol"]), dict(_symbols_cache["by_pid"])
    payload = _gateway_query(credentials, {"type": "symbols"})
    if str(payload.get("status") or "").lower() != "success":
        raise RuntimeError(str(payload.get("error") or "symbols query failed"))
    symbols = (payload.get("data") or {}).get("symbols") or {}
    by_symbol: Dict[str, Dict[str, Any]] = {}
    by_pid: Dict[int, Dict[str, Any]] = {}
    if isinstance(symbols, Mapping):
        for key, row in symbols.items():
            if not isinstance(row, Mapping):
                continue
            try:
                pid = int(row.get("product_id"))
            except Exception:  # noqa: BLE001
                continue
            sym = str(row.get("symbol") or key).strip()
            meta = {
                "product_id": pid,
                "symbol": sym,
                "type": str(row.get("type") or "").lower(),
                "price_increment_x18": int(str(row.get("price_increment_x18") or "1")),
                "size_increment": int(str(row.get("size_increment") or "1")),
                "min_size": int(str(row.get("min_size") or "0")),  # notional x18
                "tick_size": _x18_to_decimal(row.get("price_increment_x18")),
                "qty_step": Decimal(str(row.get("size_increment") or "1")) / X18,
                "min_notional": _x18_to_decimal(row.get("min_size")),
                "trading_status": str(row.get("trading_status") or ""),
            }
            by_symbol[sym.upper()] = meta
            # bare base for perps: BTC-PERP -> BTC
            if meta["type"] == "perp" and "-" in sym:
                base = sym.split("-", 1)[0].upper()
                by_symbol.setdefault(base, meta)
            by_pid[pid] = meta
    _symbols_cache["ts"] = now
    _symbols_cache["by_symbol"] = by_symbol
    _symbols_cache["by_pid"] = by_pid
    return dict(by_symbol), dict(by_pid)


def _resolve_symbol_meta(credentials: Mapping[str, str], requested: str) -> Dict[str, Any]:
    by_symbol, by_pid = _ensure_symbols(credentials)
    raw = str(requested or "").strip()
    if not raw:
        raise ValueError("INSTRUMENT_NOT_FOUND")
    key = raw.upper().replace("/", "-").replace("_", "-")
    if key in by_symbol:
        return dict(by_symbol[key])
    # numeric product id
    if key.isdigit() and int(key) in by_pid:
        return dict(by_pid[int(key)])
    # BTCUSDT -> BTC
    for suffix in ("USDT", "USDT0", "USD", "PERP"):
        if key.endswith(suffix) and len(key) > len(suffix):
            base = key[: -len(suffix)]
            if base.endswith("-"):
                base = base[:-1]
            if base in by_symbol:
                return dict(by_symbol[base])
            if f"{base}-PERP" in by_symbol:
                return dict(by_symbol[f"{base}-PERP"])
    if f"{key}-PERP" in by_symbol:
        return dict(by_symbol[f"{key}-PERP"])
    raise ValueError("INSTRUMENT_NOT_FOUND")


def _display_symbol(meta: Mapping[str, Any]) -> str:
    sym = str(meta.get("symbol") or "")
    if meta.get("type") == "perp" and "-" in sym:
        return sym.split("-", 1)[0]
    return sym or str(meta.get("product_id") or "")


def _oracle_price(credentials: Mapping[str, str], product_id: int) -> Decimal:
    payload = _gateway_query(credentials, {"type": "all_products"})
    if str(payload.get("status") or "").lower() != "success":
        raise RuntimeError(str(payload.get("error") or "all_products failed"))
    data = payload.get("data") or {}
    for bucket in ("perp_products", "spot_products"):
        for row in data.get(bucket) or []:
            if not isinstance(row, Mapping):
                continue
            try:
                if int(row.get("product_id")) == int(product_id):
                    return _x18_to_decimal(row.get("oracle_price_x18"))
            except Exception:  # noqa: BLE001
                continue
    # fallback bid/ask mid
    mp = _gateway_query(credentials, {"type": "market_prices", "product_ids": [int(product_id)]})
    rows = ((mp.get("data") or {}).get("market_prices") or [])
    if rows and isinstance(rows[0], Mapping):
        bid = _x18_to_decimal(rows[0].get("bid_x18"))
        ask = _x18_to_decimal(rows[0].get("ask_x18"))
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        return bid or ask
    raise RuntimeError(f"No oracle/mark for product {product_id}")


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def _sign_typed(private_key: str, full_message: Mapping[str, Any]) -> str:
    signable = encode_typed_data(full_message=dict(full_message))
    signed = Account.from_key(private_key).sign_message(signable)
    sig = signed.signature.hex()
    return sig if sig.startswith("0x") else ("0x" + sig)


def _eip712_domain(chain_id: int, verifying_contract: str) -> Dict[str, Any]:
    return {
        "name": "Nado",
        "version": "0.0.1",
        "chainId": int(chain_id),
        "verifyingContract": verifying_contract,
    }


_EIP712_DOMAIN_TYPES = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]


def _sign_place_order(
    *,
    private_key: str,
    chain_id: int,
    product_id: int,
    sender_hex: str,
    price_x18: int,
    amount_x18: int,
    expiration: int,
    nonce: int,
    appendix: int,
) -> str:
    message = {
        "sender": _hex_to_bytes32(sender_hex),
        "priceX18": int(price_x18),
        "amount": int(amount_x18),
        "expiration": int(expiration),
        "nonce": int(nonce),
        "appendix": int(appendix),
    }
    full = {
        "types": {
            "EIP712Domain": _EIP712_DOMAIN_TYPES,
            "Order": [
                {"name": "sender", "type": "bytes32"},
                {"name": "priceX18", "type": "int128"},
                {"name": "amount", "type": "int128"},
                {"name": "expiration", "type": "uint64"},
                {"name": "nonce", "type": "uint64"},
                {"name": "appendix", "type": "uint128"},
            ],
        },
        "primaryType": "Order",
        "domain": _eip712_domain(chain_id, _product_verifying_contract(product_id)),
        "message": message,
    }
    return _sign_typed(private_key, full)


def _sign_cancel_orders(
    *,
    private_key: str,
    chain_id: int,
    endpoint_addr: str,
    sender_hex: str,
    product_ids: Sequence[int],
    digests: Sequence[str],
    nonce: int,
) -> str:
    message = {
        "sender": _hex_to_bytes32(sender_hex),
        "productIds": [int(p) for p in product_ids],
        "digests": [_hex_to_bytes32(d) for d in digests],
        "nonce": int(nonce),
    }
    full = {
        "types": {
            "EIP712Domain": _EIP712_DOMAIN_TYPES,
            "Cancellation": [
                {"name": "sender", "type": "bytes32"},
                {"name": "productIds", "type": "uint32[]"},
                {"name": "digests", "type": "bytes32[]"},
                {"name": "nonce", "type": "uint64"},
            ],
        },
        "primaryType": "Cancellation",
        "domain": _eip712_domain(chain_id, endpoint_addr),
        "message": message,
    }
    return _sign_typed(private_key, full)


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------


def _balance_from_subaccount_info(data: Mapping[str, Any]) -> Tuple[Decimal, Decimal, Decimal, str]:
    unit = "USDT0"
    healths = data.get("healths") or []
    assets = liabilities = health = Decimal("0")
    if isinstance(healths, list) and healths and isinstance(healths[0], Mapping):
        h0 = healths[0]
        assets = _x18_to_decimal(h0.get("assets"))
        liabilities = _x18_to_decimal(h0.get("liabilities"))
        health = _x18_to_decimal(h0.get("health"))
    quote_free = Decimal("0")
    for row in data.get("spot_balances") or []:
        if not isinstance(row, Mapping) or "product_id" not in row:
            continue
        try:
            pid = int(row.get("product_id"))
        except Exception:  # noqa: BLE001
            continue
        if pid != 0:
            continue
        bal = row.get("balance") if isinstance(row.get("balance"), Mapping) else {}
        quote_free = _x18_to_decimal(bal.get("amount"))
        break
    account_value = assets if assets > 0 else max(quote_free, health)
    margin_used = liabilities if liabilities > 0 else max(account_value - quote_free, Decimal("0"))
    withdrawable = quote_free if quote_free != 0 else max(min(health, account_value), Decimal("0"))
    if withdrawable < 0:
        withdrawable = Decimal("0")
    return account_value, withdrawable, margin_used, unit


def _balance(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set NADO_<ACCOUNT>_SUBACCOUNT_OWNER (optional SUBACCOUNT_NAME).",
        )
    try:
        sub = _sender_hex(credentials)
        payload = _gateway_query(credentials, {"type": "subaccount_info", "subaccount": sub})
        if str(payload.get("status") or "").lower() != "success":
            return make_failure(
                operation="balance",
                exchange=name,
                account=credentials["account"],
                code="NADO_ERROR",
                message=_redact(sanitize_error_message(str(payload.get("error") or payload.get("error_code"))), credentials),
            )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return make_failure(
                operation="balance",
                exchange=name,
                account=credentials["account"],
                code="NADO_ERROR",
                message="Nado subaccount_info returned no data.",
            )
        if data.get("exists") is False:
            return make_failure(
                operation="balance",
                exchange=name,
                account=credentials["account"],
                code="SUBACCOUNT_NOT_FOUND",
                message="Nado subaccount does not exist yet (deposit once to create it).",
            )
        account_value, withdrawable, margin_used, unit = _balance_from_subaccount_info(data)
        return make_success(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            balance=normalize_balance(account_value, unit),
            portfolio_summary=CanonicalPortfolioSummary(
                account_value=normalize_balance(account_value, unit).value,
                withdrawable=normalize_balance(withdrawable, unit).value,
                margin_used=normalize_balance(margin_used, unit).value,
                total_position_value=normalize_balance(
                    max(account_value - withdrawable, Decimal("0")), unit
                ).value,
                unit=unit,
            ),
            data={
                "subaccount": sub,
                "subaccount_name": credentials["subaccount_name"],
                "exists": bool(data.get("exists")),
                "spot_count": data.get("spot_count"),
                "perp_count": data.get("perp_count"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            code="NADO_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


# ---------------------------------------------------------------------------
# Positions + orders
# ---------------------------------------------------------------------------


def _normalize_positions(
    credentials: Mapping[str, str],
    data: Mapping[str, Any],
) -> List[CanonicalPosition]:
    _, by_pid = _ensure_symbols(credentials)
    out: List[CanonicalPosition] = []
    for row in data.get("perp_balances") or []:
        if not isinstance(row, Mapping):
            continue
        try:
            pid = int(row.get("product_id"))
        except Exception:  # noqa: BLE001
            continue
        bal = row.get("balance") if isinstance(row.get("balance"), Mapping) else {}
        amount = _x18_to_decimal(bal.get("amount"))
        if amount == 0:
            continue
        side = "long" if amount > 0 else "short"
        size = abs(amount)
        v_quote = _x18_to_decimal(bal.get("v_quote_balance"))
        # entry approx: -v_quote / amount for perps
        entry = Decimal("0")
        if amount != 0:
            try:
                entry = abs(v_quote / amount)
            except Exception:  # noqa: BLE001
                entry = Decimal("0")
        mark = Decimal("0")
        try:
            mark = _oracle_price(credentials, pid)
        except Exception:  # noqa: BLE001
            pass
        # rough uPnL: amount*mark + v_quote
        pnl = amount * mark + v_quote if mark > 0 else Decimal("0")
        meta = by_pid.get(pid) or {"symbol": str(pid), "type": "perp", "product_id": pid}
        out.append(
            CanonicalPosition(
                symbol=_display_symbol(meta),
                side=side,
                size=_format_decimal(size),
                entry_price=_format_decimal(entry) if entry > 0 else "0",
                pnl=_format_decimal(pnl),
                exchange_instrument=str(meta.get("symbol") or pid),
            )
        )
    return out


def _fetch_open_orders(
    credentials: Mapping[str, str],
    *,
    product_ids: Sequence[int],
) -> List[Dict[str, Any]]:
    sender = _sender_hex(credentials)
    ids = sorted({int(p) for p in product_ids if int(p) >= 0})
    if not ids:
        return []
    # multi-product query
    payload = _gateway_query(
        credentials,
        {"type": "orders", "sender": sender, "product_ids": ids},
    )
    if str(payload.get("status") or "").lower() != "success":
        # fallback per product
        rows: List[Dict[str, Any]] = []
        for pid in ids:
            one = _gateway_query(
                credentials,
                {"type": "subaccount_orders", "sender": sender, "product_id": int(pid)},
            )
            if str(one.get("status") or "").lower() != "success":
                continue
            for o in (one.get("data") or {}).get("orders") or []:
                if isinstance(o, Mapping):
                    rows.append(dict(o))
        return rows
    data = payload.get("data")
    rows: List[Dict[str, Any]] = []
    if isinstance(data, Mapping):
        # multi-product: data.product_orders = [{product_id, orders:[...]}]
        product_orders = data.get("product_orders")
        if isinstance(product_orders, list):
            for block in product_orders:
                if not isinstance(block, Mapping):
                    continue
                for o in block.get("orders") or []:
                    if isinstance(o, Mapping):
                        rows.append(dict(o))
        elif isinstance(data.get("orders"), list):
            for o in data["orders"]:
                if isinstance(o, Mapping):
                    rows.append(dict(o))
        else:
            for key, val in data.items():
                if key in {"sender", "product_orders"}:
                    continue
                if isinstance(val, list):
                    for o in val:
                        if isinstance(o, Mapping):
                            rows.append(dict(o))
                elif isinstance(val, Mapping) and isinstance(val.get("orders"), list):
                    for o in val["orders"]:
                        if isinstance(o, Mapping):
                            rows.append(dict(o))
    elif isinstance(data, list):
        for o in data:
            if isinstance(o, Mapping):
                rows.append(dict(o))
    return rows


def _group_open_orders(
    credentials: Mapping[str, str],
    order_rows: Sequence[Mapping[str, Any]],
) -> Tuple[int, List[CanonicalOrderGroup]]:
    _, by_pid = _ensure_symbols(credentials)
    buckets: Dict[Tuple[str, str], List[Tuple[Decimal, Decimal, str]]] = {}
    for row in order_rows:
        try:
            pid = int(row.get("product_id"))
        except Exception:  # noqa: BLE001
            continue
        amt = _x18_to_decimal(row.get("unfilled_amount") or row.get("amount"))
        if amt == 0:
            continue
        side = "buy" if amt > 0 else "sell"
        size = abs(amt)
        price = _x18_to_decimal(row.get("price_x18") or row.get("priceX18"))
        meta = by_pid.get(pid) or {"symbol": str(pid)}
        sym = _display_symbol(meta)
        digest = str(row.get("digest") or "")
        buckets.setdefault((sym, side), []).append((price, size, digest))
    groups: List[CanonicalOrderGroup] = []
    total = 0
    for (sym, side), legs in sorted(buckets.items()):
        total += len(legs)
        sizes = [s for _, s, _ in legs]
        prices = [p for p, _, _ in legs if p > 0]
        notional = sum((p * s for p, s, _ in legs), Decimal("0"))
        total_size = sum(sizes, Decimal("0"))
        vwap = (notional / total_size) if total_size > 0 and prices else Decimal("0")
        groups.append(
            CanonicalOrderGroup(
                symbol=sym,
                side=side,
                order_count=len(legs),
                total_size=_format_decimal(total_size),
                vwap=_format_decimal(vwap) if vwap > 0 else "",
                min_price=_format_decimal(min(prices)) if prices else "",
                max_price=_format_decimal(max(prices)) if prices else "",
            )
        )
    return total, groups


def _positions_orders(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set NADO_<ACCOUNT>_SUBACCOUNT_OWNER.",
        )
    try:
        sub = _sender_hex(credentials)
        info = _gateway_query(credentials, {"type": "subaccount_info", "subaccount": sub})
        if str(info.get("status") or "").lower() != "success":
            return make_failure(
                operation="positions_orders",
                exchange=name,
                account=credentials["account"],
                code="NADO_ERROR",
                message=_redact(sanitize_error_message(str(info.get("error") or info.get("error_code"))), credentials),
            )
        data = info.get("data") if isinstance(info.get("data"), Mapping) else {}
        positions = _normalize_positions(credentials, data or {})
        positions = _enrich_positions_with_protections(credentials, positions)
        # product ids: open perps + liquid majors for resting orders without position
        _, by_pid = _ensure_symbols(credentials)
        pids = set()
        for row in (data or {}).get("perp_balances") or []:
            if isinstance(row, Mapping) and "product_id" in row:
                try:
                    pids.add(int(row.get("product_id")))
                except Exception:  # noqa: BLE001
                    pass
        by_symbol, _ = _ensure_symbols(credentials)
        for key in ("BTC-PERP", "ETH-PERP", "SOL-PERP", "HYPE-PERP"):
            if key in by_symbol:
                pids.add(int(by_symbol[key]["product_id"]))
        if not pids:
            pids.update(list(by_pid.keys())[:20])
        order_rows = _fetch_open_orders(credentials, product_ids=sorted(pids))
        open_count, groups = _group_open_orders(credentials, order_rows)
        return make_success(
            operation="positions_orders",
            exchange=name,
            account=credentials["account"],
            positions=positions,
            open_order_count=open_count,
            order_groups=groups,
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=credentials["account"],
            code="NADO_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


# ---------------------------------------------------------------------------
# New order
# ---------------------------------------------------------------------------


def _quantize_price_x18(price: Decimal, tick_x18: int) -> int:
    if tick_x18 <= 0:
        tick_x18 = 1
    raw = _to_x18_int(price, rounding=ROUND_DOWN)
    return (raw // tick_x18) * tick_x18


def _quantize_size_x18(size: Decimal, step_x18: int, *, rounding=ROUND_DOWN) -> int:
    raw = _to_x18_int(size, rounding=rounding)
    return _round_to_step_x18(raw, step_x18, rounding=rounding)


def _ensure_min_notional(size_x18: int, price_x18: int, min_notional_x18: int, step_x18: int) -> int:
    """abs(amount)*price_human >= min_notional  → amount_x18 * price_x18 / 1e18 >= min_x18."""
    if min_notional_x18 <= 0 or price_x18 <= 0:
        return size_x18
    # need size_x18 >= ceil(min_notional_x18 * 1e18 / price_x18)
    need = (min_notional_x18 * X18_INT + price_x18 - 1) // price_x18
    need = _round_to_step_x18(need, step_x18, rounding=ROUND_UP)
    return max(size_x18, need)


def _new_order(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set NADO_<ACCOUNT>_SUBACCOUNT_OWNER and PRIVATE_KEY.",
        )
    try:
        pk = _require_signer(credentials)
    except ValueError:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="MISSING_PRIVATE_KEY",
            message="Set NADO_<ACCOUNT>_PRIVATE_KEY (owner or linked signer).",
        )
    side = str(request.get("side") or "").strip().lower()
    if side not in {"buy", "sell"}:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_SIDE",
            message="side must be buy or sell.",
        )
    try:
        volume = Decimal(str(request.get("volume") or request.get("size") or "0"))
        price = Decimal(str(request.get("price") or "0"))
    except Exception:  # noqa: BLE001
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="volume and price must be numbers.",
        )
    if volume <= 0 or price <= 0:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="volume and price must be positive.",
        )
    try:
        meta = _resolve_symbol_meta(credentials, str(request.get("symbol") or ""))
        contracts = _ensure_contracts(credentials)
        pid = int(meta["product_id"])
        tick_x18 = int(meta["price_increment_x18"])
        step_x18 = int(meta["size_increment"])
        min_notional_x18 = int(meta["min_size"])
        price_x18 = _quantize_price_x18(price, tick_x18)
        size_x18 = _quantize_size_x18(volume, step_x18, rounding=ROUND_DOWN)
        if size_x18 <= 0:
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="SIZE_TOO_SMALL",
                message=f"Size rounds to zero (step={meta['qty_step']}).",
            )
        size_x18 = _ensure_min_notional(size_x18, price_x18, min_notional_x18, step_x18)
        amount_x18 = size_x18 if side == "buy" else -size_x18
        sender = _sender_hex(credentials)
        expiration = int(time.time()) + _ORDER_TTL_SECONDS
        nonce = _gen_nonce()
        appendix = _APPENDIX_DEFAULT
        sig = _sign_place_order(
            private_key=pk,
            chain_id=int(contracts["chain_id"]),
            product_id=pid,
            sender_hex=sender,
            price_x18=price_x18,
            amount_x18=amount_x18,
            expiration=expiration,
            nonce=nonce,
            appendix=appendix,
        )
        payload = _gateway_execute(
            credentials,
            {
                "place_order": {
                    "product_id": pid,
                    "order": {
                        "sender": sender,
                        "priceX18": str(price_x18),
                        "amount": str(amount_x18),
                        "expiration": str(expiration),
                        "nonce": str(nonce),
                        "appendix": str(appendix),
                    },
                    "signature": sig,
                }
            },
        )
        if str(payload.get("status") or "").lower() != "success":
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="ORDER_REJECTED",
                message=_redact(
                    sanitize_error_message(str(payload.get("error") or payload.get("error_code") or "rejected")),
                    credentials,
                ),
            )
        digest = str((payload.get("data") or {}).get("digest") or "").strip() or None
        filled = "0"
        # verify resting or filled via open orders
        time.sleep(0.35)
        rows = _fetch_open_orders(credentials, product_ids=[pid])
        resting = 0
        for row in rows:
            if digest and str(row.get("digest") or "") == digest:
                resting = 1
                break
        status = "resting" if resting else ("submitted" if digest else "unknown")
        submitted_vol = _format_decimal(Decimal(size_x18) / X18)
        submitted_px = _format_decimal(Decimal(price_x18) / X18)
        return make_success(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            order=CanonicalOrderResult(
                symbol=_display_symbol(meta),
                side=side,
                order_type="limit",
                requested_volume=_format_decimal(volume),
                requested_price=_format_decimal(price),
                submitted_volume=submitted_vol,
                submitted_price=submitted_px,
                verified=bool(digest),
                status=status,
                exchange_order_id=digest,
                client_order_id=str(nonce),
            ),
        )
    except ValueError as exc:
        code = str(exc)
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code=code if code in {"INSTRUMENT_NOT_FOUND", "MISSING_PRIVATE_KEY"} else "INVALID_REQUEST",
            message="Instrument not found." if code == "INSTRUMENT_NOT_FOUND" else sanitize_error_message(str(exc)),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="NADO_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def _ladder_prices(start: Decimal, end: Decimal, count: int, tick: Decimal) -> List[Decimal]:
    if count <= 0:
        return []
    if count == 1:
        raw = [start]
    else:
        raw = [start + (end - start) * Decimal(i) / Decimal(count - 1) for i in range(count)]
    out: List[Decimal] = []
    for value in raw:
        # quantize via ROUND_HALF_UP on tick grid
        if tick <= 0:
            out.append(value)
            continue
        units = (value / tick).to_integral_value(rounding=ROUND_HALF_UP)
        out.append(units * tick)
    return out


def _ladder_sizes(
    total: Decimal,
    count: int,
    increment: Decimal,
    distribution: str,
    min_size: Decimal = Decimal("0"),
) -> List[Decimal]:
    key = str(distribution or "").strip().lower().replace(" ", "_")
    if key == "uniform":
        weights = [Decimal(1)] * count
    elif key == "half_gaussian":
        if count == 1:
            weights = [Decimal(1)]
        else:
            span = Decimal(count - 1)
            weights = [
                Decimal(
                    str(
                        math.exp(
                            -(float(Decimal("3") * (span - Decimal(i)) / span) ** 2) / 2
                        )
                    )
                )
                for i in range(count)
            ]
    else:
        raise ValueError(f"Unsupported ladder distribution: {distribution}")
    if increment <= 0:
        raise ValueError("Invalid size increment")
    minimum_units = (
        int((min_size / increment).to_integral_value(rounding=ROUND_UP)) if min_size > 0 else 0
    )
    total_units = int((total / increment).to_integral_value(rounding=ROUND_DOWN))
    if total_units <= 0:
        raise ValueError("Total volume rounds to zero at the size step")
    if total_units < minimum_units * count:
        raise ValueError("Total volume is too small for the exchange minimum on every ladder child")
    distributable = total_units - minimum_units * count
    weight_sum = sum(weights)
    raw = [Decimal(distributable) * w / weight_sum for w in weights]
    allocated = [minimum_units + int(x) for x in raw]
    residual = total_units - sum(allocated)
    remainders = [raw[i] - int(raw[i]) for i in range(count)]
    order = sorted(range(count), key=lambda i: (remainders[i], -i), reverse=True)
    for i in order[: max(residual, 0)]:
        allocated[i] += 1
    return [Decimal(x) * increment for x in allocated]


def _place_limit_child(
    credentials: Mapping[str, str],
    *,
    private_key: str,
    contracts: Mapping[str, Any],
    meta: Mapping[str, Any],
    side: str,
    price: Decimal,
    size: Decimal,
) -> Dict[str, Any]:
    """Place one ladder child; returns ok/digest/error/price/size."""
    pid = int(meta["product_id"])
    tick_x18 = int(meta["price_increment_x18"])
    step_x18 = int(meta["size_increment"])
    min_notional_x18 = int(meta["min_size"])
    price_x18 = _quantize_price_x18(price, tick_x18)
    size_x18 = _quantize_size_x18(size, step_x18, rounding=ROUND_DOWN)
    if size_x18 <= 0 or price_x18 <= 0:
        return {
            "ok": False,
            "digest": None,
            "error": "rounded_to_zero",
            "price": Decimal(price_x18) / X18 if price_x18 else Decimal("0"),
            "size": Decimal("0"),
        }
    # Check min notional without auto-bump (ladder budget is fixed).
    notional_x18 = (size_x18 * price_x18) // X18_INT
    if min_notional_x18 > 0 and notional_x18 < min_notional_x18:
        return {
            "ok": False,
            "digest": None,
            "error": "below_min_notional",
            "price": Decimal(price_x18) / X18,
            "size": Decimal(size_x18) / X18,
        }
    amount_x18 = size_x18 if side == "buy" else -size_x18
    sender = _sender_hex(credentials)
    expiration = int(time.time()) + _ORDER_TTL_SECONDS
    nonce = _gen_nonce()
    appendix = _APPENDIX_DEFAULT
    try:
        sig = _sign_place_order(
            private_key=private_key,
            chain_id=int(contracts["chain_id"]),
            product_id=pid,
            sender_hex=sender,
            price_x18=price_x18,
            amount_x18=amount_x18,
            expiration=expiration,
            nonce=nonce,
            appendix=appendix,
        )
        payload = _gateway_execute(
            credentials,
            {
                "place_order": {
                    "product_id": pid,
                    "order": {
                        "sender": sender,
                        "priceX18": str(price_x18),
                        "amount": str(amount_x18),
                        "expiration": str(expiration),
                        "nonce": str(nonce),
                        "appendix": str(appendix),
                    },
                    "signature": sig,
                }
            },
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "digest": None,
            "error": str(exc),
            "price": Decimal(price_x18) / X18,
            "size": Decimal(size_x18) / X18,
        }
    if str(payload.get("status") or "").lower() != "success":
        return {
            "ok": False,
            "digest": None,
            "error": str(payload.get("error") or payload.get("error_code") or "rejected"),
            "price": Decimal(price_x18) / X18,
            "size": Decimal(size_x18) / X18,
        }
    digest = str((payload.get("data") or {}).get("digest") or "").strip() or None
    return {
        "ok": bool(digest),
        "digest": digest,
        "error": None if digest else "missing_digest",
        "price": Decimal(price_x18) / X18,
        "size": Decimal(size_x18) / X18,
        "nonce": nonce,
    }


def _ladder(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set NADO_<ACCOUNT>_SUBACCOUNT_OWNER and PRIVATE_KEY.",
        )
    try:
        pk = _require_signer(credentials)
    except ValueError:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="MISSING_PRIVATE_KEY",
            message="Set NADO_<ACCOUNT>_PRIVATE_KEY (owner or linked signer).",
        )
    requested_symbol = str(request.get("symbol") or "").strip()
    side_in = str(request.get("side") or "").strip().lower()
    distribution = str(request.get("distribution") or "uniform").strip().lower().replace(" ", "_")
    try:
        count = int(str(request.get("order_count") or "0").strip())
        total = Decimal(str(request.get("total_volume") or "0").strip())
        start = Decimal(str(request.get("start_price") or "0").strip())
        end = Decimal(str(request.get("end_price") or "0").strip())
    except Exception:  # noqa: BLE001
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="order_count, total_volume, start_price and end_price are required numbers.",
        )
    if side_in not in {"buy", "sell"}:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_SIDE",
            message="Side must be buy or sell.",
        )
    if count < 1:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="order_count must be >= 1.",
        )
    if count > LADDER_ABSOLUTE_MAX_ORDERS:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message=f"order_count exceeds safety cap ({LADDER_ABSOLUTE_MAX_ORDERS}).",
        )
    if total <= 0 or start <= 0 or end <= 0:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="total_volume, start_price and end_price must be positive.",
        )
    if side_in == "buy" and not (end < start):
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="For a BUY ladder, end_price must be lower than start_price.",
        )
    if side_in == "sell" and not (end > start):
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="For a SELL ladder, end_price must be higher than start_price.",
        )
    if distribution not in {"uniform", "half_gaussian"}:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="INVALID_REQUEST",
            message="distribution must be uniform or half_gaussian.",
        )
    try:
        meta = _resolve_symbol_meta(credentials, requested_symbol)
        contracts = _ensure_contracts(credentials)
        tick = meta["tick_size"] if meta.get("tick_size") and meta["tick_size"] > 0 else Decimal("1")
        step = meta["qty_step"] if meta.get("qty_step") and meta["qty_step"] > 0 else Decimal("0.0001")
        prices = _ladder_prices(start, end, count, tick)
        # Nado min_size is notional (USDT0). Worst (lowest) price needs the largest coin size.
        min_notional = meta.get("min_notional") or Decimal("0")
        worst_price = min(prices) if prices else min(start, end)
        min_coin = Decimal("0")
        if min_notional > 0 and worst_price > 0:
            min_coin = (min_notional / worst_price).quantize(step, rounding=ROUND_UP)
            if min_coin < step:
                min_coin = step
        sizes = _ladder_sizes(total, count, step, distribution, min_coin)

        submitted_children: List[Dict[str, Any]] = []
        batches: List[Dict[str, Any]] = [None] * count  # type: ignore[list-item]
        omitted_below_minimum = 0
        first_error: Optional[str] = None
        rate_limited = False

        def _one(idx: int, price: Decimal, size: Decimal) -> Tuple[int, Dict[str, Any]]:
            child = _place_limit_child(
                credentials,
                private_key=pk,
                contracts=contracts,
                meta=meta,
                side=side_in,
                price=price,
                size=size,
            )
            return idx, child

        workers = 1 if count <= 2 else min(LADDER_MAX_WORKERS, count)
        if workers == 1:
            results: List[Tuple[int, Dict[str, Any]]] = []
            for idx, (price, size) in enumerate(zip(prices, sizes)):
                results.append(_one(idx, price, size))
                if idx + 1 < count:
                    time.sleep(LADDER_CHILD_PAUSE_SECONDS)
        else:
            results = []
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = [
                    pool.submit(_one, idx, price, size)
                    for idx, (price, size) in enumerate(zip(prices, sizes))
                ]
                for fut in as_completed(futs):
                    results.append(fut.result())

        for idx, child in sorted(results, key=lambda x: x[0]):
            price = prices[idx]
            size = sizes[idx]
            batches[idx] = {
                "index": idx,
                "price": _format_decimal(child.get("price") or price),
                "size": _format_decimal(child.get("size") or size),
                "ok": bool(child.get("ok")),
                "order_id": child.get("digest"),
                "error": child.get("error"),
            }
            if child.get("ok") and child.get("digest"):
                submitted_children.append(
                    {
                        "order_id": child["digest"],
                        "price": child["price"],
                        "size": child["size"],
                    }
                )
            else:
                err = str(child.get("error") or "failed")
                if err in {"below_min_notional", "rounded_to_zero", "below_min_qty"}:
                    omitted_below_minimum += 1
                if "rate" in err.lower() or "23000" in err:
                    rate_limited = True
                if first_error is None:
                    first_error = err

        # Drop any holes (shouldn't happen)
        batches_out = [b for b in batches if isinstance(b, dict)]

        ids = [str(c["order_id"]) for c in submitted_children]
        submitted_volume = sum((Decimal(str(c["size"])) for c in submitted_children), Decimal("0"))

        verified = False
        if ids:
            try:
                time.sleep(0.25)
                live = _fetch_open_orders(credentials, product_ids=[int(meta["product_id"])])
                live_ids = {str(r.get("digest") or "").strip() for r in live}
                # Partial verify is OK for large ladders — confirm majority resting.
                hit = sum(1 for oid in ids if oid in live_ids)
                verified = hit == len(ids)
                if not verified and hit >= max(1, int(len(ids) * 0.9)):
                    # Treat ≥90% resting as verified success for UX on large grids.
                    verified = True
            except Exception:  # noqa: BLE001
                verified = len(ids) == count

        partial = (len(ids) != count) or (not verified and bool(ids))
        result = CanonicalLadderResult(
            symbol=_display_symbol(meta),
            side=side_in,
            distribution=distribution,
            requested_order_count=count,
            submitted_order_count=len(ids),
            requested_volume=_format_decimal(total),
            submitted_volume=_format_decimal(submitted_volume),
            batch_count=len(batches_out),
            verified=verified and len(ids) == count,
            partial=partial,
            status=(
                "success"
                if (verified and len(ids) == count)
                else ("partial" if ids else "failed")
            ),
            accepted_child_count=len(ids),
            omitted_order_count=count - len(ids),
            omitted_below_minimum=omitted_below_minimum or None,
            child_order_ids=list(ids),
            batches=batches_out,
            rate_limited=rate_limited or None,
            exchange_reason=(
                _redact(first_error, credentials)
                if first_error and (not ids or partial)
                else None
            ),
        )
        if result.verified:
            return make_success(
                operation="ladder",
                exchange=name,
                account=credentials["account"],
                ladder=result,
            )
        if ids:
            return make_success(
                operation="ladder",
                exchange=name,
                account=credentials["account"],
                ladder=result,
            )
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="LADDER_FAILED",
            message=_redact(
                sanitize_error_message(first_error or "No ladder children accepted."),
                credentials,
            ),
            ladder=result,
        )
    except ValueError as exc:
        msg = str(exc)
        code = "INSTRUMENT_NOT_FOUND" if msg == "INSTRUMENT_NOT_FOUND" else "INVALID_REQUEST"
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code=code,
            message=sanitize_error_message(msg),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="ladder",
            exchange=name,
            account=credentials["account"],
            code="NADO_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _cancel_order_group(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set NADO_<ACCOUNT>_SUBACCOUNT_OWNER and PRIVATE_KEY.",
        )
    try:
        pk = _require_signer(credentials)
    except ValueError:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=credentials["account"],
            code="MISSING_PRIVATE_KEY",
            message="Set NADO_<ACCOUNT>_PRIVATE_KEY (owner or linked signer).",
        )
    side = str(request.get("side") or "").strip().lower()
    if side not in {"buy", "sell"}:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=credentials["account"],
            code="INVALID_SIDE",
            message="side must be buy or sell.",
        )
    try:
        meta = _resolve_symbol_meta(credentials, str(request.get("symbol") or ""))
        pid = int(meta["product_id"])
        rows = _fetch_open_orders(credentials, product_ids=[pid])
        want: List[Tuple[int, str]] = []
        for row in rows:
            try:
                rpid = int(row.get("product_id"))
            except Exception:  # noqa: BLE001
                rpid = pid
            if rpid != pid:
                continue
            amt = _x18_to_decimal(row.get("unfilled_amount") or row.get("amount"))
            if amt == 0:
                continue
            rside = "buy" if amt > 0 else "sell"
            if rside != side:
                continue
            digest = str(row.get("digest") or "").strip()
            if digest:
                want.append((rpid, digest))
        if not want:
            return make_success(
                operation="cancel_order_group",
                exchange=name,
                account=credentials["account"],
                cancel_group=CanonicalCancelGroupResult(
                    symbol=_display_symbol(meta),
                    side=side,
                    targeted_order_count=0,
                    cancelled_order_count=0,
                    confirmed_absent_count=0,
                    remaining_target_count=0,
                    verified=True,
                    status="noop",
                ),
            )
        contracts = _ensure_contracts(credentials)
        sender = _sender_hex(credentials)
        cancelled = 0
        batch = 20
        batch_count = 0
        for i in range(0, len(want), batch):
            chunk = want[i : i + batch]
            product_ids = [p for p, _ in chunk]
            digests = [d for _, d in chunk]
            nonce = _gen_nonce()
            sig = _sign_cancel_orders(
                private_key=pk,
                chain_id=int(contracts["chain_id"]),
                endpoint_addr=str(contracts["endpoint_addr"]),
                sender_hex=sender,
                product_ids=product_ids,
                digests=digests,
                nonce=nonce,
            )
            payload = _gateway_execute(
                credentials,
                {
                    "cancel_orders": {
                        "tx": {
                            "sender": sender,
                            "productIds": product_ids,
                            "digests": digests,
                            "nonce": str(nonce),
                        },
                        "signature": sig,
                    }
                },
            )
            batch_count += 1
            if str(payload.get("status") or "").lower() != "success":
                return make_failure(
                    operation="cancel_order_group",
                    exchange=name,
                    account=credentials["account"],
                    code="CANCEL_FAILED",
                    message=_redact(
                        sanitize_error_message(
                            str(payload.get("error") or payload.get("error_code") or "cancel failed")
                        ),
                        credentials,
                    ),
                    cancel_group=CanonicalCancelGroupResult(
                        symbol=_display_symbol(meta),
                        side=side,
                        targeted_order_count=len(want),
                        cancelled_order_count=cancelled,
                        confirmed_absent_count=cancelled,
                        remaining_target_count=max(len(want) - cancelled, 0),
                        verified=False,
                        partial=cancelled > 0,
                        status="failed",
                        batch_count=batch_count,
                        requested_cancel_count=len(want),
                        verified_cancel_count=cancelled,
                        exchange_reason=_redact(
                            sanitize_error_message(str(payload.get("error") or "")),
                            credentials,
                        ),
                    ),
                )
            cancelled += len((payload.get("data") or {}).get("cancelled_orders") or chunk)
            time.sleep(0.05)
        time.sleep(0.3)
        left = _fetch_open_orders(credentials, product_ids=[pid])
        remaining = 0
        for row in left:
            amt = _x18_to_decimal(row.get("unfilled_amount") or row.get("amount"))
            if amt == 0:
                continue
            rside = "buy" if amt > 0 else "sell"
            if rside == side:
                remaining += 1
        verified = remaining == 0
        return make_success(
            operation="cancel_order_group",
            exchange=name,
            account=credentials["account"],
            cancel_group=CanonicalCancelGroupResult(
                symbol=_display_symbol(meta),
                side=side,
                targeted_order_count=len(want),
                cancelled_order_count=cancelled,
                confirmed_absent_count=len(want) - remaining,
                remaining_target_count=remaining,
                verified=verified,
                partial=remaining > 0 and cancelled > 0,
                status="success" if verified else "partial",
                batch_count=batch_count,
                requested_cancel_count=len(want),
                verified_cancel_count=len(want) - remaining,
            ),
        )
    except ValueError as exc:
        code = str(exc)
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=credentials["account"],
            code=code if code in {"INSTRUMENT_NOT_FOUND", "MISSING_PRIVATE_KEY"} else "INVALID_REQUEST",
            message="Instrument not found." if code == "INSTRUMENT_NOT_FOUND" else sanitize_error_message(str(exc)),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=credentials["account"],
            code="NADO_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


# ---------------------------------------------------------------------------
# Instruments / market price
# ---------------------------------------------------------------------------


def _resolve_instrument(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set NADO_<ACCOUNT>_SUBACCOUNT_OWNER.",
        )
    try:
        meta = _resolve_symbol_meta(credentials, str(request.get("symbol") or request.get("query") or ""))
        requested = str(request.get("symbol") or request.get("query") or "")
        disp = _display_symbol(meta)
        inst = CanonicalInstrument(
            requested_symbol=requested,
            symbol=str(meta.get("symbol") or disp),
            display_name=disp,
            price_increment=_format_decimal(meta["tick_size"]),
            size_increment=_format_decimal(meta["qty_step"]),
            minimum_size=_format_decimal(meta["qty_step"]),
        )
        return make_success(
            operation="resolve_instrument",
            exchange=name,
            account=credentials["account"],
            instrument=inst,
        )
    except ValueError:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=credentials["account"],
            code="INSTRUMENT_NOT_FOUND",
            message="Instrument not found on Nado.",
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=credentials["account"],
            code="NADO_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _list_instruments(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set NADO_<ACCOUNT>_SUBACCOUNT_OWNER.",
        )
    try:
        by_symbol, _ = _ensure_symbols(credentials)
        q = str(request.get("query") or request.get("symbol") or "").strip().upper()
        out: List[CanonicalInstrument] = []
        seen = set()
        for key, meta in sorted(by_symbol.items()):
            if meta.get("type") != "perp":
                continue
            # only native SYMBOL-PERP keys
            if not str(meta.get("symbol") or "").endswith("-PERP"):
                continue
            if key != str(meta.get("symbol") or "").upper():
                continue
            if q and q not in key and q not in _display_symbol(meta).upper():
                continue
            disp = _display_symbol(meta)
            if disp in seen:
                continue
            seen.add(disp)
            out.append(
                CanonicalInstrument(
                    requested_symbol=disp,
                    symbol=str(meta.get("symbol")),
                    display_name=disp,
                    price_increment=_format_decimal(meta["tick_size"]),
                    size_increment=_format_decimal(meta["qty_step"]),
                    minimum_size=_format_decimal(meta["qty_step"]),
                )
            )
            if len(out) >= 50:
                break
        return make_success(
            operation="list_instruments",
            exchange=name,
            account=credentials["account"],
            data={"instruments": [instrument.to_dict() for instrument in out]},
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account=credentials["account"],
            code="NADO_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _market_price(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set NADO_<ACCOUNT>_SUBACCOUNT_OWNER.",
        )
    try:
        meta = _resolve_symbol_meta(credentials, str(request.get("symbol") or ""))
        requested = str(request.get("symbol") or "")
        mark = _oracle_price(credentials, int(meta["product_id"]))
        return make_success(
            operation="market_price",
            exchange=name,
            account=credentials["account"],
            market_price=CanonicalMarketPrice(
                requested_symbol=requested,
                market=str(meta.get("symbol") or _display_symbol(meta)),
                mark_price=_format_decimal(mark),
                oracle_price=_format_decimal(mark),
                price=_format_decimal(mark),
            ),
        )
    except ValueError:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=credentials["account"],
            code="INSTRUMENT_NOT_FOUND",
            message="Instrument not found on Nado.",
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="market_price",
            exchange=name,
            account=credentials["account"],
            code="NADO_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _trigger_base(credentials: Mapping[str, str]) -> str:
    gw = str(credentials.get("gateway_url") or DEFAULT_GATEWAY_REST).rstrip("/")
    # https://api.prod.nado.xyz/gateway/v1 -> https://api.prod.nado.xyz/trigger/v1
    if "/gateway/" in gw:
        return gw.replace("/gateway/", "/trigger/")
    return DEFAULT_TRIGGER_REST


def _build_appendix(
    *,
    order_type: int = 0,
    reduce_only: bool = False,
    trigger_type: int = 0,
    version: int = 1,
) -> int:
    """Encode Nado order appendix flags.

    Bits: version(0-7), isolated(8), order_type(9-10), reduce_only(11), trigger(12-13).
    order_type: 0 DEFAULT, 1 IOC, 2 FOK, 3 POST_ONLY
    trigger_type: 0 NONE, 1 PRICE, 2 TWAP, 3 TWAP_CUSTOM
    """
    val = int(version) & 0xFF
    val |= (int(order_type) & 0x3) << 9
    if reduce_only:
        val |= 1 << 11
    val |= (int(trigger_type) & 0x3) << 12
    return val


def _sign_list_trigger_orders(
    *,
    private_key: str,
    chain_id: int,
    endpoint_addr: str,
    sender_hex: str,
    recv_time_ms: int,
) -> str:
    full = {
        "types": {
            "EIP712Domain": _EIP712_DOMAIN_TYPES,
            "ListTriggerOrders": [
                {"name": "sender", "type": "bytes32"},
                {"name": "recvTime", "type": "uint64"},
            ],
        },
        "primaryType": "ListTriggerOrders",
        "domain": _eip712_domain(chain_id, endpoint_addr),
        "message": {
            "sender": _hex_to_bytes32(sender_hex),
            "recvTime": int(recv_time_ms),
        },
    }
    return _sign_typed(private_key, full)


def _trigger_http(
    credentials: Mapping[str, str],
    *,
    path: str,
    payload: Mapping[str, Any],
) -> Dict[str, Any]:
    base = _trigger_base(credentials).rstrip("/")
    url = base + path
    body = json.dumps(dict(payload)).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:
            raw = _decode_body(resp.read(), resp.headers.get("Content-Encoding", ""))
            parsed = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            err_body = _decode_body(
                exc.read(),
                exc.headers.get("Content-Encoding", "") if exc.headers else "",
            )
            parsed = json.loads(err_body.decode("utf-8"))
            if isinstance(parsed, dict):
                return parsed
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"HTTP {exc.code} on Nado trigger {path}: {exc.reason}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Nado trigger returned a non-object JSON payload.")
    return parsed


def _list_trigger_orders(
    credentials: Mapping[str, str],
    *,
    product_ids: Optional[Sequence[int]] = None,
    reduce_only: Optional[bool] = True,
    status_types: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Signed list of pending trigger orders (for TP/SL display)."""
    pk = str(credentials.get("private_key") or "").strip()
    if not pk:
        return []
    contracts = _ensure_contracts(credentials)
    sender = _sender_hex(credentials)
    recv = int(time.time() * 1000) + 50_000
    sig = _sign_list_trigger_orders(
        private_key=pk,
        chain_id=int(contracts["chain_id"]),
        endpoint_addr=str(contracts["endpoint_addr"]),
        sender_hex=sender,
        recv_time_ms=recv,
    )
    body: Dict[str, Any] = {
        "type": "list_trigger_orders",
        "tx": {"sender": sender, "recvTime": str(recv)},
        "signature": sig,
        "limit": 100,
        "status_types": list(status_types or ["waiting_price", "waiting_dependency", "triggering"]),
    }
    if product_ids is not None:
        body["product_ids"] = [int(p) for p in product_ids]
    if reduce_only is not None:
        body["reduce_only"] = bool(reduce_only)
    payload = _trigger_http(credentials, path="/query", payload=body)
    if str(payload.get("status") or "").lower() != "success":
        return []
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    rows = data.get("orders") if isinstance(data, Mapping) else []
    out: List[Dict[str, Any]] = []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, Mapping):
                out.append(dict(row))
    return out


def _protection_kind_from_trigger(row: Mapping[str, Any], position_side: str) -> Optional[str]:
    """Classify a reduce-only price trigger as tp or sl for the position side."""
    order_wrap = row.get("order") if isinstance(row.get("order"), Mapping) else row
    if not isinstance(order_wrap, Mapping):
        return None
    # nested: order.order.amount or order.amount
    inner = order_wrap.get("order") if isinstance(order_wrap.get("order"), Mapping) else order_wrap
    amount = _x18_to_decimal(inner.get("amount") if isinstance(inner, Mapping) else 0)
    trigger = order_wrap.get("trigger") if isinstance(order_wrap.get("trigger"), Mapping) else {}
    req: Dict[str, Any] = {}
    if isinstance(trigger, Mapping) and isinstance(trigger.get("price_trigger"), Mapping):
        pr = trigger["price_trigger"].get("price_requirement")
        if isinstance(pr, Mapping):
            req = {str(k): v for k, v in pr.items()}
    elif isinstance(trigger, Mapping):
        req = {str(k): v for k, v in trigger.items()}
    above = any(str(k).endswith("above") and req.get(k) not in (None, "") for k in req)
    below = any(str(k).endswith("below") and req.get(k) not in (None, "") for k in req)
    # closing side: long closes with sell (amount < 0); short closes with buy (amount > 0)
    if position_side == "long":
        if amount >= 0:
            return None  # not a close sell
        if above:
            return "tp"
        if below:
            return "sl"
    else:
        if amount <= 0:
            return None
        if below:
            return "tp"
        if above:
            return "sl"
    return None


def _trigger_price_from_row(row: Mapping[str, Any]) -> Optional[Decimal]:
    order_wrap = row.get("order") if isinstance(row.get("order"), Mapping) else row
    if not isinstance(order_wrap, Mapping):
        return None
    trigger = order_wrap.get("trigger") if isinstance(order_wrap.get("trigger"), Mapping) else {}
    req: Dict[str, Any] = {}
    if isinstance(trigger, Mapping) and isinstance(trigger.get("price_trigger"), Mapping):
        pr = trigger["price_trigger"].get("price_requirement")
        if isinstance(pr, Mapping):
            req = {str(k): v for k, v in pr.items()}
    elif isinstance(trigger, Mapping):
        req = {str(k): v for k, v in trigger.items()}
    for k, v in req.items():
        if any(s in str(k) for s in ("above", "below")) and v not in (None, ""):
            return _x18_to_decimal(v)
    inner = order_wrap.get("order") if isinstance(order_wrap.get("order"), Mapping) else order_wrap
    if isinstance(inner, Mapping) and inner.get("priceX18"):
        return _x18_to_decimal(inner.get("priceX18"))
    return None


def _digest_from_trigger_row(row: Mapping[str, Any]) -> Optional[str]:
    order_wrap = row.get("order") if isinstance(row.get("order"), Mapping) else row
    if not isinstance(order_wrap, Mapping):
        return None
    d = str(order_wrap.get("digest") or row.get("digest") or "").strip()
    return d or None


def _product_id_from_trigger_row(row: Mapping[str, Any]) -> Optional[int]:
    order_wrap = row.get("order") if isinstance(row.get("order"), Mapping) else row
    if not isinstance(order_wrap, Mapping):
        return None
    try:
        return int(order_wrap.get("product_id"))
    except Exception:  # noqa: BLE001
        return None


def _enrich_positions_with_protections(
    credentials: Mapping[str, str],
    positions: List[CanonicalPosition],
) -> List[CanonicalPosition]:
    if not positions or not credentials.get("private_key"):
        return positions
    try:
        by_symbol, by_pid = _ensure_symbols(credentials)
        pids: List[int] = []
        for pos in positions:
            key = str(pos.exchange_instrument or pos.symbol or "").upper()
            meta = by_symbol.get(key) or by_symbol.get(f"{key}-PERP")
            if meta:
                pids.append(int(meta["product_id"]))
        rows = _list_trigger_orders(credentials, product_ids=pids or None, reduce_only=True)
        enriched: List[CanonicalPosition] = []
        for pos in positions:
            key = str(pos.exchange_instrument or pos.symbol or "").upper()
            meta = by_symbol.get(key) or by_symbol.get(f"{key}-PERP")
            pid = int(meta["product_id"]) if meta else None
            tp = sl = None
            tp_c = sl_c = 0
            for row in rows:
                rpid = _product_id_from_trigger_row(row)
                if pid is not None and rpid is not None and rpid != pid:
                    continue
                kind = _protection_kind_from_trigger(row, pos.side)
                px = _trigger_price_from_row(row)
                if kind == "tp" and px is not None:
                    tp_c += 1
                    if tp is None:
                        tp = _format_decimal(px)
                elif kind == "sl" and px is not None:
                    sl_c += 1
                    if sl is None:
                        sl = _format_decimal(px)
            enriched.append(
                CanonicalPosition(
                    symbol=pos.symbol,
                    side=pos.side,
                    size=pos.size,
                    entry_price=pos.entry_price,
                    pnl=pos.pnl,
                    tp=tp,
                    sl=sl,
                    tp_count=tp_c or None,
                    sl_count=sl_c or None,
                    exchange_instrument=pos.exchange_instrument,
                )
            )
        return enriched
    except Exception as exc:  # noqa: BLE001
        logger.warning("nado protection enrich failed: %s", exc)
        return positions


def _find_open_position(
    credentials: Mapping[str, str], requested_symbol: str
) -> Tuple[CanonicalPosition, Dict[str, Any], Decimal]:
    """Return (position, meta, mark)."""
    meta = _resolve_symbol_meta(credentials, requested_symbol)
    sub = _sender_hex(credentials)
    info = _gateway_query(credentials, {"type": "subaccount_info", "subaccount": sub})
    if str(info.get("status") or "").lower() != "success":
        raise RuntimeError(str(info.get("error") or "subaccount_info failed"))
    data = info.get("data") if isinstance(info.get("data"), Mapping) else {}
    positions = _normalize_positions(credentials, data or {})
    want = _display_symbol(meta).upper()
    native = str(meta.get("symbol") or "").upper()
    for pos in positions:
        if pos.symbol.upper() == want or str(pos.exchange_instrument or "").upper() == native:
            mark = _oracle_price(credentials, int(meta["product_id"]))
            return pos, meta, mark
    raise ValueError("NO_OPEN_POSITION")


def _place_reduce_ioc(
    credentials: Mapping[str, str],
    *,
    private_key: str,
    meta: Mapping[str, Any],
    position_side: str,
    size: Decimal,
    price: Decimal,
) -> Dict[str, Any]:
    """Market-style close: IOC reduce-only limit through the book."""
    contracts = _ensure_contracts(credentials)
    pid = int(meta["product_id"])
    tick_x18 = int(meta["price_increment_x18"])
    step_x18 = int(meta["size_increment"])
    price_x18 = _quantize_price_x18(price, tick_x18)
    size_x18 = _quantize_size_x18(size, step_x18, rounding=ROUND_DOWN)
    if size_x18 <= 0:
        return {"ok": False, "error": "size_too_small"}
    # long closes sell (neg), short closes buy (pos)
    amount_x18 = -size_x18 if position_side == "long" else size_x18
    appendix = _build_appendix(order_type=1, reduce_only=True, trigger_type=0)  # IOC+RO
    sender = _sender_hex(credentials)
    expiration = int(time.time()) + 120
    nonce = _gen_nonce()
    sig = _sign_place_order(
        private_key=private_key,
        chain_id=int(contracts["chain_id"]),
        product_id=pid,
        sender_hex=sender,
        price_x18=price_x18,
        amount_x18=amount_x18,
        expiration=expiration,
        nonce=nonce,
        appendix=appendix,
    )
    payload = _gateway_execute(
        credentials,
        {
            "place_order": {
                "product_id": pid,
                "order": {
                    "sender": sender,
                    "priceX18": str(price_x18),
                    "amount": str(amount_x18),
                    "expiration": str(expiration),
                    "nonce": str(nonce),
                    "appendix": str(appendix),
                },
                "signature": sig,
            }
        },
    )
    if str(payload.get("status") or "").lower() != "success":
        return {
            "ok": False,
            "error": str(payload.get("error") or payload.get("error_code") or "close rejected"),
        }
    return {
        "ok": True,
        "digest": str((payload.get("data") or {}).get("digest") or "").strip() or None,
    }


def _place_price_trigger_protection(
    credentials: Mapping[str, str],
    *,
    private_key: str,
    meta: Mapping[str, Any],
    position_side: str,
    size: Decimal,
    trigger_price: Decimal,
    kind: str,
) -> Dict[str, Any]:
    """Register reduce-only TP/SL on the trigger service."""
    contracts = _ensure_contracts(credentials)
    pid = int(meta["product_id"])
    tick_x18 = int(meta["price_increment_x18"])
    step_x18 = int(meta["size_increment"])
    trigger_x18 = _quantize_price_x18(trigger_price, tick_x18)
    # order limit price: TP at trigger; SL slightly worse for fill
    if kind == "tp":
        order_px = trigger_x18
    else:
        # long SL sell lower; short SL buy higher
        slip = Decimal("0.995") if position_side == "long" else Decimal("1.005")
        order_px = _quantize_price_x18(_x18_to_decimal(trigger_x18) * slip, tick_x18)
    size_x18 = _quantize_size_x18(size, step_x18, rounding=ROUND_DOWN)
    if size_x18 <= 0 or trigger_x18 <= 0:
        return {"ok": False, "error": "invalid_size_or_price"}
    amount_x18 = -size_x18 if position_side == "long" else size_x18
    # IOC + reduce_only + PRICE trigger
    appendix = _build_appendix(order_type=1, reduce_only=True, trigger_type=1)
    sender = _sender_hex(credentials)
    expiration = int(time.time()) + _ORDER_TTL_SECONDS
    nonce = _gen_nonce()
    sig = _sign_place_order(
        private_key=private_key,
        chain_id=int(contracts["chain_id"]),
        product_id=pid,
        sender_hex=sender,
        price_x18=order_px,
        amount_x18=amount_x18,
        expiration=expiration,
        nonce=nonce,
        appendix=appendix,
    )
    # trigger requirement
    if position_side == "long":
        req_key = "oracle_price_above" if kind == "tp" else "oracle_price_below"
    else:
        req_key = "oracle_price_below" if kind == "tp" else "oracle_price_above"
    trigger_body = {
        "price_trigger": {"price_requirement": {req_key: str(trigger_x18)}}
    }
    payload = _trigger_http(
        credentials,
        path="/execute",
        payload={
            "place_order": {
                "product_id": pid,
                "order": {
                    "sender": sender,
                    "priceX18": str(order_px),
                    "amount": str(amount_x18),
                    "expiration": str(expiration),
                    "nonce": str(nonce),
                    "appendix": str(appendix),
                },
                "trigger": trigger_body,
                "signature": sig,
            }
        },
    )
    if str(payload.get("status") or "").lower() != "success":
        return {
            "ok": False,
            "error": str(payload.get("error") or payload.get("error_code") or "trigger rejected"),
        }
    return {
        "ok": True,
        "digest": str((payload.get("data") or {}).get("digest") or "").strip() or None,
        "trigger_price": _format_decimal(_x18_to_decimal(trigger_x18)),
    }


def _cancel_trigger_digests(
    credentials: Mapping[str, str],
    *,
    private_key: str,
    product_id: int,
    digests: Sequence[str],
) -> int:
    if not digests:
        return 0
    contracts = _ensure_contracts(credentials)
    sender = _sender_hex(credentials)
    cancelled = 0
    batch = 20
    for i in range(0, len(digests), batch):
        chunk = list(digests[i : i + batch])
        product_ids = [int(product_id)] * len(chunk)
        nonce = _gen_nonce()
        sig = _sign_cancel_orders(
            private_key=private_key,
            chain_id=int(contracts["chain_id"]),
            endpoint_addr=str(contracts["endpoint_addr"]),
            sender_hex=sender,
            product_ids=product_ids,
            digests=chunk,
            nonce=nonce,
        )
        payload = _trigger_http(
            credentials,
            path="/execute",
            payload={
                "cancel_orders": {
                    "tx": {
                        "sender": sender,
                        "productIds": product_ids,
                        "digests": chunk,
                        "nonce": str(nonce),
                    },
                    "signature": sig,
                }
            },
        )
        if str(payload.get("status") or "").lower() == "success":
            cancelled += len(chunk)
        time.sleep(0.05)
    return cancelled


def _cancel_protections_for_position(
    credentials: Mapping[str, str],
    *,
    private_key: str,
    meta: Mapping[str, Any],
    position_side: str,
    kinds: Sequence[str] = ("tp", "sl"),
) -> int:
    pid = int(meta["product_id"])
    rows = _list_trigger_orders(credentials, product_ids=[pid], reduce_only=True)
    digests: List[str] = []
    for row in rows:
        kind = _protection_kind_from_trigger(row, position_side)
        if kind not in kinds:
            continue
        d = _digest_from_trigger_row(row)
        if d:
            digests.append(d)
    return _cancel_trigger_digests(
        credentials, private_key=private_key, product_id=pid, digests=digests
    )


def _set_protection(account: str, request: Mapping[str, Any], *, kind: str) -> CanonicalResponse:
    operation = "set_tp" if kind == "tp" else "set_sl"
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set NADO_<ACCOUNT>_SUBACCOUNT_OWNER and PRIVATE_KEY.",
        )
    try:
        pk = _require_signer(credentials)
    except ValueError:
        return make_failure(
            operation=operation,
            exchange=name,
            account=credentials["account"],
            code="MISSING_PRIVATE_KEY",
            message="Set NADO_<ACCOUNT>_PRIVATE_KEY (owner or linked signer).",
        )
    try:
        price_val = Decimal(str(request.get("price") or "0").strip() or "0")
    except Exception:  # noqa: BLE001
        return make_failure(
            operation=operation,
            exchange=name,
            account=credentials["account"],
            code="INVALID_PRICE",
            message="Protection price must be a number (use 0 to remove).",
        )
    try:
        pos, meta, mark = _find_open_position(credentials, str(request.get("symbol") or ""))
        if price_val <= 0:
            n = _cancel_protections_for_position(
                credentials, private_key=pk, meta=meta, position_side=pos.side, kinds=(kind,)
            )
            return make_success(
                operation=operation,
                exchange=name,
                account=credentials["account"],
                position_action=CanonicalPositionActionResult(
                    operation=operation,
                    symbol=pos.symbol,
                    verified=True,
                    removed=True,
                    status="success",
                    current_side=pos.side,
                    current_size=pos.size,
                    message=f"Removed {kind.upper()} ({n} trigger(s) cancelled).",
                ),
            )
        # direction checks
        if kind == "tp":
            if pos.side == "long" and price_val <= mark:
                return make_failure(
                    operation=operation,
                    exchange=name,
                    account=credentials["account"],
                    code="INVALID_TP_PRICE",
                    message="Long TP must be above mark price.",
                )
            if pos.side == "short" and price_val >= mark:
                return make_failure(
                    operation=operation,
                    exchange=name,
                    account=credentials["account"],
                    code="INVALID_TP_PRICE",
                    message="Short TP must be below mark price.",
                )
        else:
            if pos.side == "long" and price_val >= mark:
                return make_failure(
                    operation=operation,
                    exchange=name,
                    account=credentials["account"],
                    code="INVALID_SL_PRICE",
                    message="Long SL must be below mark price.",
                )
            if pos.side == "short" and price_val <= mark:
                return make_failure(
                    operation=operation,
                    exchange=name,
                    account=credentials["account"],
                    code="INVALID_SL_PRICE",
                    message="Short SL must be above mark price.",
                )
        # replace existing leg
        _cancel_protections_for_position(
            credentials, private_key=pk, meta=meta, position_side=pos.side, kinds=(kind,)
        )
        time.sleep(0.15)
        placed = _place_price_trigger_protection(
            credentials,
            private_key=pk,
            meta=meta,
            position_side=pos.side,
            size=Decimal(str(pos.size)),
            trigger_price=price_val,
            kind=kind,
        )
        if not placed.get("ok"):
            return make_failure(
                operation=operation,
                exchange=name,
                account=credentials["account"],
                code="PROTECTION_FAILED",
                message=_redact(sanitize_error_message(str(placed.get("error") or "failed")), credentials),
            )
        time.sleep(0.35)
        # verify via list
        rows = _list_trigger_orders(
            credentials, product_ids=[int(meta["product_id"])], reduce_only=True
        )
        got = None
        for row in rows:
            if _protection_kind_from_trigger(row, pos.side) == kind:
                got = _trigger_price_from_row(row)
                break
        verified = got is not None and abs(got - price_val) / max(price_val, Decimal("1")) < Decimal("0.01")
        return make_success(
            operation=operation,
            exchange=name,
            account=credentials["account"],
            position_action=CanonicalPositionActionResult(
                operation=operation,
                symbol=pos.symbol,
                verified=bool(verified),
                price=_format_decimal(price_val),
                status="success" if verified else "submitted",
                exchange_order_id=placed.get("digest"),
                current_side=pos.side,
                current_size=pos.size,
                message=f"Set {kind.upper()}={_format_decimal(price_val)} via Nado trigger service.",
            ),
        )
    except ValueError as exc:
        code = str(exc)
        return make_failure(
            operation=operation,
            exchange=name,
            account=credentials["account"],
            code=code if code in {"NO_OPEN_POSITION", "INSTRUMENT_NOT_FOUND"} else "INVALID_REQUEST",
            message="No open position for symbol." if code == "NO_OPEN_POSITION" else sanitize_error_message(str(exc)),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation=operation,
            exchange=name,
            account=credentials["account"],
            code="NADO_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


def _close_position(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set NADO_<ACCOUNT>_SUBACCOUNT_OWNER and PRIVATE_KEY.",
        )
    try:
        pk = _require_signer(credentials)
    except ValueError:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=credentials["account"],
            code="MISSING_PRIVATE_KEY",
            message="Set NADO_<ACCOUNT>_PRIVATE_KEY (owner or linked signer).",
        )
    try:
        pos, meta, mark = _find_open_position(credentials, str(request.get("symbol") or ""))
        # cancel protections first
        _cancel_protections_for_position(
            credentials, private_key=pk, meta=meta, position_side=pos.side, kinds=("tp", "sl")
        )
        size = Decimal(str(pos.size))
        # aggressive IOC price
        if pos.side == "long":
            px = mark * Decimal("0.99")
        else:
            px = mark * Decimal("1.01")
        placed = _place_reduce_ioc(
            credentials,
            private_key=pk,
            meta=meta,
            position_side=pos.side,
            size=size,
            price=px,
        )
        if not placed.get("ok"):
            return make_failure(
                operation="close_position",
                exchange=name,
                account=credentials["account"],
                code="CLOSE_FAILED",
                message=_redact(sanitize_error_message(str(placed.get("error") or "close failed")), credentials),
                position_action=CanonicalPositionActionResult(
                    operation="close_position",
                    symbol=pos.symbol,
                    verified=False,
                    status="failed",
                    current_side=pos.side,
                    current_size=pos.size,
                ),
            )
        time.sleep(0.7)
        try:
            _find_open_position(credentials, str(request.get("symbol") or ""))
            flat = False
            still_side, still_size = pos.side, pos.size
        except ValueError:
            flat = True
            still_side, still_size = None, "0"
        return make_success(
            operation="close_position",
            exchange=name,
            account=credentials["account"],
            position_action=CanonicalPositionActionResult(
                operation="close_position",
                symbol=pos.symbol,
                verified=flat,
                status="success" if flat else "submitted",
                exchange_order_id=placed.get("digest"),
                current_side=still_side,
                current_size=still_size,
                message="Position closed." if flat else "Close submitted; flat not yet confirmed.",
            ),
        )
    except ValueError as exc:
        code = str(exc)
        return make_failure(
            operation="close_position",
            exchange=name,
            account=credentials["account"],
            code=code if code in {"NO_OPEN_POSITION", "INSTRUMENT_NOT_FOUND"} else "INVALID_REQUEST",
            message="No open position for symbol." if code == "NO_OPEN_POSITION" else sanitize_error_message(str(exc)),
            position_action=CanonicalPositionActionResult(
                operation="close_position",
                symbol=str(request.get("symbol") or ""),
                verified=code == "NO_OPEN_POSITION",
                status="noop" if code == "NO_OPEN_POSITION" else "failed",
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="close_position",
            exchange=name,
            account=credentials["account"],
            code="NADO_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


def execute(request: Dict[str, Any]) -> CanonicalResponse:
    if not isinstance(request, dict):
        return make_failure(
            operation="",
            exchange=name,
            account="",
            code="INVALID_REQUEST",
            message="Request must be a dict.",
        )
    operation = str(request.get("operation") or "").strip()
    account = str(request.get("account") or "").strip()
    if not operation:
        return make_failure(
            operation="",
            exchange=name,
            account=account,
            code="INVALID_REQUEST",
            message="Missing 'operation'.",
        )
    if not account:
        return make_failure(
            operation=operation,
            exchange=name,
            account="",
            code="MISSING_ACCOUNT",
            message="Missing 'account'.",
        )
    try:
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
        if operation == "set_tp":
            return _set_protection(account, request, kind="tp")
        if operation == "set_sl":
            return _set_protection(account, request, kind="sl")
        if operation == "close_position":
            return _close_position(account, request)
        if operation == "resolve_instrument":
            return _resolve_instrument(account, request)
        if operation == "list_instruments":
            return _list_instruments(account, request)
        if operation == "market_price":
            return _market_price(account, request)
        if operation == "get_tickers":
            return _execute_get_tickers(account, request)
        if operation == "candles":
            return handle_candles_operation(name, account, request)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="NADO_ERROR",
            message=sanitize_error_message(str(exc)),
        )
    return make_failure(
        operation=operation,
        exchange=name,
        account=account,
        code="NOT_IMPLEMENTED",
        message=f"Nado does not implement '{operation}' yet.",
    )


# ---------------------------------------------------------------------------
# Canonical ``get_tickers`` (Batch 2)
# ---------------------------------------------------------------------------
#
# Nado's symbol catalog comes from ``{"type": "symbols"}`` and the per-
# product oracle price from ``{"type": "all_products"}``. Both are bulk
# sources; the existing per-symbol ``_oracle_price`` already calls
# ``all_products`` once per symbol — we deliberately avoid that fan-out
# by calling ``all_products`` ONCE for the whole catalog and indexing it
# by ``product_id``. Static identity is sourced from the same symbols
# cache ``_list_instruments`` already uses, so the catalog cannot drift.
#
# Per the brief, ``mark_price`` is left ``None`` at the catalog level —
# Nado does not publish a venue-native mark; its reference price is the
# oracle. The bid/ask-mid fallback is a per-symbol convenience used only
# by ``market_price`` and is intentionally not replicated here. This
# keeps the canonical contract clean: ``oracle_price`` carries the
# authoritative Nado price; ``mark_price`` is only set when an actual
# venue mark exists in the bulk payload (it does not today).


def _nado_filter_symbols(symbols: Any) -> List[str]:
    if symbols in (None, ""):
        return []
    if isinstance(symbols, (str, bytes)):
        raw_items = [symbols]
    else:
        try:
            raw_items = list(symbols)
        except TypeError:
            raw_items = [symbols]
    out: List[str] = []
    for item in raw_items:
        text = str(item or "").strip().upper()
        if text:
            out.append(text)
    return out


def _execute_get_tickers(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    """Bulk canonical ticker snapshot for Nado.

    Two bulk upstream calls per invocation (the symbols call is cache-
    gated with ``_CACHE_TTL`` so warm-cache invocations are effectively
    one upstream call — the ``all_products`` price snapshot):

    * ``{"type": "symbols"}`` — provides the catalog (perp filtering and
      SYMBOL-PERP key mirroring reused from ``_list_instruments``).
    * ``{"type": "all_products"}`` — provides ``oracle_price_x18`` for
      every perp product. One fetch for the entire catalog; never per
      symbol.

    Dynamic 24h metrics that Nado does not publish in either bulk source
    are left ``None``. Static identity rows remain present even when the
    oracle is 0 / missing.
    """
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="get_tickers",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Set NADO_<ACCOUNT>_SUBACCOUNT_OWNER.",
        )
    filters = _nado_filter_symbols(request.get("symbols"))
    try:
        by_symbol, _ = _ensure_symbols(credentials)
        all_products = _gateway_query(credentials, {"type": "all_products"})
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="get_tickers",
            exchange=name,
            account=account,
            code="NADO_ERROR",
            message=_redact(sanitize_error_message(str(exc)), credentials),
        )
    if str(all_products.get("status") or "").lower() != "success":
        return make_failure(
            operation="get_tickers",
            exchange=name,
            account=account,
            code="ALL_PRODUCTS_FAILED",
            message=_redact(sanitize_error_message(str(all_products.get("error") or "all_products failed")), credentials),
        )
    oracle_by_pid: Dict[int, Decimal] = {}
    data = all_products.get("data") or {}
    for bucket in ("perp_products", "spot_products"):
        for row in data.get(bucket) or []:
            if not isinstance(row, Mapping):
                continue
            try:
                pid = int(row.get("product_id"))
            except Exception:  # noqa: BLE001
                continue
            oracle_by_pid[pid] = _x18_to_decimal(row.get("oracle_price_x18"))

    tickers: Dict[str, CanonicalMarketPrice] = {}
    seen: set = set()
    for key, meta in sorted(by_symbol.items()):
        if meta.get("type") != "perp":
            continue
        sym = str(meta.get("symbol") or "").strip()
        if not sym.endswith("-PERP"):
            continue
        if key != sym.upper():
            continue
        disp = _display_symbol(meta)
        if disp in seen:
            continue
        if filters and key not in filters and disp.upper() not in filters:
            continue
        seen.add(disp)
        pid = int(meta.get("product_id") or 0)
        oracle_decimal = oracle_by_pid.get(pid)
        oracle = _format_decimal(oracle_decimal) if oracle_decimal is not None and oracle_decimal > 0 else None
        tickers[sym] = CanonicalMarketPrice(
            requested_symbol=sym,
            market=sym,
            symbol=sym,
            native_symbol=sym,
            display_symbol=disp,
            display_name=disp,
            base=disp,
            quote="USD",
            market_type="perp",
            mark_price=None,  # Nado has no venue-native mark in bulk; oracle carries the authoritative price.
            oracle_price=oracle,
            price=oracle,
            price_increment=_format_decimal(meta["tick_size"]),
            size_increment=_format_decimal(meta["qty_step"]),
            minimum_size=_format_decimal(meta["qty_step"]),
            minimum_notional=_format_decimal(meta["min_notional"]) if meta.get("min_notional") else None,
        )
    return make_success(
        operation="get_tickers",
        exchange=name,
        account=credentials["account"],
        tickers_batch=CanonicalTickersBatch(
            tickers=tickers,
            source="nado_all_products",
            refresh_status="ok",
        ),
    )
