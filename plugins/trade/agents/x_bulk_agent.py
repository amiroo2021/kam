"""Bulk Trade exchange agent for the /trade stack.

Credentials (``.env`` / environment):
  ``BULK_<ALIAS>_ACCOUNT``             account public key (base58)
  ``BULK_<ALIAS>_AGENT_PRIVATE_KEY``   agent signer key (required for discovery
                                        and signed writes)
  Optional: ``BULK_<ALIAS>_BASE_URL``  default https://mainnet-api1.bulk.trade/api/v1

Reads use unsigned POST /account {type: fullAccount, user}.
Writes use signed POST /order (new_order, ladder, cancel_order_group).

Wizard Positions Management lists via operation ``positions_management``,
which is an alias of the same fullAccount snapshot as ``positions_orders``.
``set_tp`` / ``set_sl`` / ``close_position`` are not advertised until implemented.
"""

from __future__ import annotations

import json
import logging
import os
import re
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from ..canonical import (
    CanonicalBalance,
    CanonicalCancelGroupResult,
    CanonicalInstrument,
    CanonicalLadderResult,
    CanonicalMarketPrice,
    CanonicalOrderGroup,
    CanonicalOrderResult,
    CanonicalPortfolioSummary,
    CanonicalPosition,
    CanonicalResponse,
    make_failure,
    make_success,
    sanitize_error_message,
)

logger = logging.getLogger(__name__)

name = "bulk"
DEFAULT_API_BASE = "https://mainnet-api1.bulk.trade/api/v1"
API_TIMEOUT_SECONDS = 20
DEFAULT_UNIT = "USDC"
# Ladder children are submitted as multiple signed POST /order transactions.
# Each transaction carries up to LADDER_BATCH_SIZE limit actions.
# Example: 70 orders -> batches of 50 + 20; 110 -> 50 + 50 + 10; 200 -> 50×4.
LADDER_BATCH_SIZE = 50
LADDER_BATCH_PAUSE_SECONDS = 5.0
# On HTTP 429, retry the *same* batch with exponential backoff before giving up.
# Bounded retries only — do not probe the live rate limit by inventing extra traffic.
LADDER_BATCH_MAX_RETRIES = 3
LADDER_BATCH_RETRY_BASE_SECONDS = 5.0
# Cancel group uses the same chunk size: side-filtered `cx` actions, not symbol-wide
# `cxa` (cxa would also cancel the opposite side on that symbol).
CANCEL_BATCH_SIZE = 50
CANCEL_BATCH_PAUSE_SECONDS = 5.0
CANCEL_BATCH_MAX_RETRIES = 3
CANCEL_BATCH_RETRY_BASE_SECONDS = 5.0

_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_BASE58_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,64}$")
_ACCOUNT_SUFFIXES = ("ACCOUNT", "PUBLIC_KEY", "PUBKEY", "USER")
_PRIVATE_KEY_SUFFIXES = ("AGENT_PRIVATE_KEY", "AGENT_PRIVATEKEY", "PRIVATE_KEY", "PRIVATEKEY", "SECRET")
_BASE_URL_SUFFIXES = ("BASE_URL", "API_BASE", "URL")
_AGENT_PUBLIC_KEY_SUFFIXES = ("AGENT_PUBLIC_KEY", "AGENT_PUBKEY", "SIGNER", "SIGNER_PUBLIC_KEY")
_SIGNATURE_DOMAIN_SUFFIXES = ("SIGNATURE_DOMAIN", "DOMAIN", "NETWORK")


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


def _combined_bulk_env() -> Dict[str, Tuple[str, str, str]]:
    """Return BULK_* env values, preferring process env over ~/.hermes/.env."""
    out: Dict[str, Tuple[str, str, str]] = {}
    for key, value in os.environ.items():
        if key.upper().startswith("BULK_"):
            out.setdefault(key.upper(), (key, str(value), "env"))
    for key, value in _load_dotenv_values(_hermes_home() / ".env").items():
        if key.upper().startswith("BULK_"):
            out.setdefault(key.upper(), (key, str(value), "dotenv"))
    return out


def _parse_alias_and_suffix(upper_key: str) -> Optional[Tuple[str, str]]:
    if not upper_key.startswith("BULK_"):
        return None
    rest = upper_key[len("BULK_") :]
    suffixes = sorted(
        set(_ACCOUNT_SUFFIXES + _PRIVATE_KEY_SUFFIXES + _BASE_URL_SUFFIXES + _AGENT_PUBLIC_KEY_SUFFIXES + _SIGNATURE_DOMAIN_SUFFIXES),
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
    for upper_key, (_actual, value, _source) in _combined_bulk_env().items():
        parsed = _parse_alias_and_suffix(upper_key)
        if parsed is None:
            continue
        alias_upper, suffix = parsed
        alias = alias_upper.lower()
        slot = buckets.setdefault(alias, {})
        val = str(value or "").strip()
        if suffix in _ACCOUNT_SUFFIXES:
            slot.setdefault("account_pubkey", val)
        elif suffix in _PRIVATE_KEY_SUFFIXES:
            slot.setdefault("agent_private_key", val)
        elif suffix in _BASE_URL_SUFFIXES:
            slot.setdefault("base_url", val.rstrip("/"))
        elif suffix in _AGENT_PUBLIC_KEY_SUFFIXES:
            slot.setdefault("agent_public_key", val)
        elif suffix in _SIGNATURE_DOMAIN_SUFFIXES:
            slot.setdefault("signature_domain", val.lower())

    complete: Dict[str, Dict[str, str]] = {}
    for alias, fields in buckets.items():
        account_pubkey = str(fields.get("account_pubkey") or "").strip()
        agent_private_key = str(fields.get("agent_private_key") or "").strip()
        if account_pubkey and agent_private_key:
            complete[alias] = fields
    return complete


def list_accounts() -> list[str]:
    return sorted(_discover_credential_map().keys())


def capabilities() -> list[str]:
    # positions_management is the wizard list op (alias of positions_orders).
    # set_tp / set_sl / close_position are intentionally absent until implemented.
    return [
        "balance",
        "positions_orders",
        "positions_management",
        "new_order",
        "ladder",
        "cancel_order_group",
        "resolve_instrument",
        "list_instruments",
        "market_price",
    ]


def _lookup_credentials(account: str) -> Optional[Dict[str, str]]:
    alias = str(account or "").strip().lower()
    if not alias:
        return None
    fields = _discover_credential_map().get(alias)
    if not fields:
        return None
    account_pubkey = str(fields.get("account_pubkey") or "").strip()
    agent_private_key = str(fields.get("agent_private_key") or "").strip()
    if not _BASE58_PATTERN.match(account_pubkey):
        return None
    return {
        "account": alias,
        "account_pubkey": account_pubkey,
        "agent_private_key": agent_private_key,
        "agent_public_key": str(fields.get("agent_public_key") or "").strip(),
        "signature_domain": str(fields.get("signature_domain") or "").strip().lower(),
        "base_url": fields.get("base_url") or DEFAULT_API_BASE,
    }


def _redact(text: Any, credentials: Optional[Mapping[str, str]] = None) -> str:
    rendered = str(text or "")
    if credentials:
        for key in ("agent_private_key",):
            value = str(credentials.get(key) or "").strip()
            if len(value) >= 4:
                rendered = rendered.replace(value, "***")
    rendered = re.sub(r"(?i)(BULK_[A-Z0-9_]+_(?:AGENT_)?PRIVATE_?KEY\s*=\s*)([^\s,;}\"']+)", r"\1***", rendered)
    return sanitize_error_message(rendered)


def _raise_http_error(exc: urllib.error.HTTPError) -> None:
    raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
    message = raw or str(exc)
    retry_after_raw = None
    try:
        retry_after_raw = exc.headers.get("Retry-After") if exc.headers else None
    except Exception:  # noqa: BLE001
        retry_after_raw = None
    text = f"HTTP {exc.code}: {message}"
    if retry_after_raw:
        text = f"{text} (Retry-After: {retry_after_raw})"
    err = RuntimeError(text)
    setattr(err, "http_status", int(exc.code))
    if retry_after_raw is not None:
        setattr(err, "retry_after", retry_after_raw)
    raise err from exc


def _post_json(base_url: str, path: str, payload: Mapping[str, Any], headers: Optional[Mapping[str, str]] = None) -> Any:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    body = json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Hermes-KAM-BulkAgent/1.0",
            **dict(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:  # noqa: S310 HTTPS API
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        _raise_http_error(exc)
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason or exc)) from exc
    raise RuntimeError("Bulk HTTP POST failed without a response")


def _get_json(base_url: str, path: str, query: Optional[Mapping[str, Any]] = None) -> Any:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})}"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": "Hermes-KAM-BulkAgent/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:  # noqa: S310 HTTPS API
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        _raise_http_error(exc)
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason or exc)) from exc
    raise RuntimeError("Bulk HTTP GET failed without a response")


def _account_query(credentials: Mapping[str, str], query_type: str = "fullAccount") -> Any:
    return _post_json(
        str(credentials.get("base_url") or DEFAULT_API_BASE),
        "/account",
        {"type": query_type, "user": str(credentials.get("account_pubkey") or "")},
    )


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        if value is None or value == "":
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _money(value: Any) -> str:
    return str(_decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _plain_decimal(value: Any) -> str:
    dec = _decimal(value)
    if dec == dec.to_integral_value():
        return str(dec.quantize(Decimal("1")))
    return format(dec.normalize(), "f")


def _is_rate_limit_error(exc: BaseException) -> bool:
    status = getattr(exc, "http_status", None)
    if status == 429:
        return True
    text = str(exc).lower()
    return "http 429" in text or "too many requests" in text or "rate limit" in text or "rate_limited" in text


POST_WRITE_STATUS_UNKNOWN_MESSAGE = (
    "One or more orders may already have been submitted. "
    "Do not retry this ladder until open orders are reconciled."
)


def _retry_after_seconds(exc: BaseException) -> Optional[float]:
    """Parse Retry-After from a Bulk HTTP error when the exchange provides one."""
    raw = getattr(exc, "retry_after", None)
    if raw is None:
        match = re.search(r"retry-after\s*[:=]?\s*(\d+(?:\.\d+)?)", str(exc), flags=re.IGNORECASE)
        raw = match.group(1) if match else None
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    # Cap a single wait so a bad header cannot stall the wizard for minutes.
    return min(value, 60.0)


def _submit_order_with_retries(
    credentials: Mapping[str, str],
    actions: list[Mapping[str, Any]],
    *,
    max_retries: int,
    base_sleep: float,
    label: str,
    batch_index: int,
) -> Dict[str, Any]:
    """Submit one multi-action POST /order, retrying only on HTTP 429.

    Retries are bounded and only re-send the same batch payload after a pause.
    This is recovery, not rate-limit probing.
    """
    attempts = max(0, int(max_retries)) + 1
    last_exc: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            return _submit_order(credentials, actions)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not _is_rate_limit_error(exc) or attempt + 1 >= attempts:
                raise
            delay = _retry_after_seconds(exc)
            if delay is None:
                delay = float(base_sleep) * (2 ** attempt)
            delay = max(0.5, min(float(delay), 60.0))
            logger.warning(
                "Bulk %s batch %s rate-limited (attempt %s/%s); sleeping %.1fs then retrying same batch",
                label,
                batch_index,
                attempt + 1,
                attempts,
                delay,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _is_ambiguous_submission_error(exc: BaseException) -> bool:
    """Return True when a POST /order attempt may have reached Bulk."""
    if isinstance(exc, TimeoutError):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "timed out",
            "timeout",
            "temporarily unavailable",
            "connection reset",
            "connection aborted",
            "remote end closed",
            "http post failed without a response",
        )
    )


def _bulk_order_ids_from_payload(payload: Any) -> list[Any]:
    """Best-effort order-id extraction from Bulk POST /order responses."""
    out: list[Any] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key in ("orderId", "order_id", "oid", "id"):
                if key in value and value[key] not in (None, ""):
                    out.append(value[key])
            for key in ("orderIds", "order_ids", "oids", "ids"):
                child = value.get(key)
                if isinstance(child, list):
                    for item in child:
                        if item not in (None, ""):
                            out.append(item)
            for child in value.values():
                if isinstance(child, (Mapping, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    deduped: list[Any] = []
    for item in out:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _non_retry_safe_ladder_failure(
    account: str,
    *,
    reason: str,
    accepted_count: int,
    requested_count: int,
    accepted_order_ids: list[Any],
    batch_plan: list[Dict[str, Any]],
    batches: list[Dict[str, Any]],
) -> CanonicalResponse:
    response = make_failure(
        "ladder",
        name,
        account,
        "ORDER_STATUS_UNKNOWN",
        f"{POST_WRITE_STATUS_UNKNOWN_MESSAGE} Detail: {sanitize_error_message(reason)}",
        exchange_reason=sanitize_error_message(reason),
    )
    object.__setattr__(response, "data", {
        "accepted_child_count": accepted_count,
        "requested_order_count": requested_count,
        "accepted_order_ids": list(accepted_order_ids),
        "batch_plan": list(batch_plan),
        "batches": list(batches),
        "status_unknown": True,
        "retry_safe": False,
    })
    return response


def _display_symbol(symbol: Any) -> str:
    text = str(symbol or "").strip()
    if text.upper().endswith("-USD"):
        return text[:-4]
    if text.upper().endswith("-USDC"):
        return text[:-5]
    return text or "UNKNOWN"


def _extract_full_account(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and isinstance(item.get("fullAccount"), dict):
                return item["fullAccount"]
        return {}
    if isinstance(payload, dict):
        if isinstance(payload.get("fullAccount"), dict):
            return payload["fullAccount"]
        if isinstance(payload.get("accountSnapshot"), dict):
            return payload["accountSnapshot"]
        if isinstance(payload.get("data"), list):
            return _extract_full_account(payload["data"])
    return {}


def _portfolio_from_account(full_account: Mapping[str, Any]) -> CanonicalPortfolioSummary:
    raw_margin = full_account.get("margin")
    margin: Mapping[str, Any] = raw_margin if isinstance(raw_margin, dict) else {}
    return CanonicalPortfolioSummary(
        account_value=_money(margin.get("totalMargin", margin.get("totalBalance"))),
        withdrawable=_money(margin.get("transferableBalance", margin.get("availableMargin", margin.get("availableBalance")))),
        margin_used=_money(margin.get("marginUsed")),
        total_position_value=_money(margin.get("notional")),
        unit=DEFAULT_UNIT,
    )


def _protection_prices_from_orders(full_account: Mapping[str, Any]) -> Dict[tuple[str, str], Dict[str, Any]]:
    """Derive display TP/SL from Bulk openOrders conditional/reduce-only rows.

    Keyed by (display_symbol, position_side long|short). For a long, protective
    sells are TP/SL; for a short, protective buys are TP/SL.
    """
    raw_rows = full_account.get("openOrders")
    rows: Iterable[Any] = raw_rows if isinstance(raw_rows, list) else []
    buckets: Dict[tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        size = _decimal(row.get("size") if row.get("size") is not None else row.get("originalSize"))
        if size == 0:
            continue
        order_side = "buy" if size > 0 else "sell"
        classification = _classification(str(row.get("orderType") or ""), bool(row.get("reduceOnly")))
        if classification not in {"take_profit", "stop_loss"}:
            continue
        # Protective order side is opposite the position side.
        position_side = "long" if order_side == "sell" else "short"
        key = (_display_symbol(row.get("symbol")), position_side)
        bucket = buckets.setdefault(key, {"tp": [], "sl": []})
        price = _plain_decimal(row.get("price") if row.get("price") is not None else row.get("triggerPrice"))
        if classification == "take_profit":
            bucket["tp"].append(price)
        else:
            bucket["sl"].append(price)
    out: Dict[tuple[str, str], Dict[str, Any]] = {}
    for key, bucket in buckets.items():
        tp_prices = bucket["tp"]
        sl_prices = bucket["sl"]
        entry: Dict[str, Any] = {}
        if tp_prices:
            unique = sorted(set(tp_prices), key=lambda p: _decimal(p))
            entry["tp"] = unique[0] if len(unique) == 1 else "Mixed"
            entry["tp_count"] = len(tp_prices)
        if sl_prices:
            unique = sorted(set(sl_prices), key=lambda p: _decimal(p))
            entry["sl"] = unique[0] if len(unique) == 1 else "Mixed"
            entry["sl_count"] = len(sl_prices)
        if entry:
            out[key] = entry
    return out


def _positions_from_account(full_account: Mapping[str, Any]) -> list[CanonicalPosition]:
    protection = _protection_prices_from_orders(full_account)
    raw_rows = full_account.get("positions")
    rows: Iterable[Any] = raw_rows if isinstance(raw_rows, list) else []
    out: list[CanonicalPosition] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        size = _decimal(row.get("size"))
        if size == 0:
            continue
        symbol_native = str(row.get("symbol") or "").strip()
        side = "long" if size > 0 else "short"
        display = _display_symbol(symbol_native)
        prot = protection.get((display, side), {})
        out.append(
            CanonicalPosition(
                symbol=display,
                side=side,
                size=_plain_decimal(abs(size)),
                entry_price=_plain_decimal(row.get("price")),
                pnl=_plain_decimal(row.get("unrealizedPnl")),
                tp=prot.get("tp"),
                sl=prot.get("sl"),
                tp_count=prot.get("tp_count"),
                sl_count=prot.get("sl_count"),
                exchange_instrument=symbol_native or None,
                mark=_plain_decimal(row.get("fairPrice")) if row.get("fairPrice") is not None else None,
            )
        )
    return out


def _classification(order_type: str, reduce_only: bool) -> str:
    normalized = str(order_type or "").strip().lower()
    if normalized in {"takeprofit", "take_profit", "tp"}:
        return "take_profit"
    if normalized in {"stop", "stoploss", "stop_loss"}:
        return "stop_loss"
    if normalized in {"range", "trigger", "trailing"}:
        return "trigger"
    if reduce_only and normalized != "limit":
        return "other"
    return "entry_limit"


def _order_groups_from_account(full_account: Mapping[str, Any]) -> list[CanonicalOrderGroup]:
    raw_rows = full_account.get("openOrders")
    rows: Iterable[Any] = raw_rows if isinstance(raw_rows, list) else []
    buckets: Dict[Tuple[str, str, str, bool], Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol_native = str(row.get("symbol") or "").strip()
        if not symbol_native:
            continue
        remaining = _decimal(row.get("size", row.get("originalSize")))
        if remaining == 0:
            original = _decimal(row.get("originalSize"))
            remaining = original
        if remaining == 0:
            continue
        side = "buy" if remaining > 0 else "sell"
        reduce_only = bool(row.get("reduceOnly"))
        order_type = str(row.get("orderType") or "limit").strip() or "limit"
        classification = _classification(order_type, reduce_only)
        key = (symbol_native, side, classification, reduce_only)
        bucket = buckets.setdefault(
            key,
            {"count": 0, "size": Decimal("0"), "notional": Decimal("0"), "prices": [], "ids": [], "display_type": order_type.upper()},
        )
        size_abs = abs(remaining)
        price = _decimal(row.get("price"))
        bucket["count"] += 1
        bucket["size"] += size_abs
        bucket["notional"] += size_abs * price
        bucket["prices"].append(price)
        oid = row.get("orderId") or row.get("oid")
        if oid is not None:
            bucket["ids"].append(str(oid))
        if order_type:
            bucket["display_type"] = order_type.upper()

    groups: list[CanonicalOrderGroup] = []
    for (symbol_native, side, classification, reduce_only), bucket in sorted(buckets.items()):
        total_size = bucket["size"]
        prices = bucket["prices"] or [Decimal("0")]
        vwap = bucket["notional"] / total_size if total_size else Decimal("0")
        groups.append(
            CanonicalOrderGroup(
                symbol=_display_symbol(symbol_native),
                side=side,
                order_count=int(bucket["count"]),
                total_size=_plain_decimal(total_size),
                vwap=_plain_decimal(vwap),
                min_price=_plain_decimal(min(prices)),
                max_price=_plain_decimal(max(prices)),
                classification=classification,
                display_type=str(bucket.get("display_type") or "LIMIT"),
                reduce_only=reduce_only,
                order_ids=list(bucket["ids"]),
                exchange_instrument=symbol_native,
            )
        )
    return groups


def _open_order_count(full_account: Mapping[str, Any]) -> int:
    orders = full_account.get("openOrders")
    return len(orders) if isinstance(orders, list) else 0



# ---------------------------------------------------------------------------
# Instruments / market data
# ---------------------------------------------------------------------------

_exchange_info_cache: Dict[str, Tuple[float, list[Dict[str, Any]]]] = {}
_TTL_SECONDS = 60.0


def _exchange_info(credentials: Mapping[str, str]) -> list[Dict[str, Any]]:
    base = str(credentials.get("base_url") or DEFAULT_API_BASE).rstrip("/")
    now = time.time()
    cached = _exchange_info_cache.get(base)
    if cached and now - cached[0] < _TTL_SECONDS:
        return cached[1]
    payload = _get_json(base, "/exchangeInfo")
    rows = payload if isinstance(payload, list) else []
    instruments = [r for r in rows if isinstance(r, dict)]
    _exchange_info_cache[base] = (now, instruments)
    return instruments


def _norm_symbol_text(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _instrument_candidates(credentials: Mapping[str, str], requested: str) -> list[Dict[str, Any]]:
    req = _norm_symbol_text(requested)
    if not req:
        return []
    out: list[tuple[int, Dict[str, Any]]] = []
    for row in _exchange_info(credentials):
        sym = str(row.get("symbol") or "").strip()
        if not sym:
            continue
        base = str(row.get("baseAsset") or "").strip()
        quote = str(row.get("quoteAsset") or "").strip()
        keys = {_norm_symbol_text(sym), _norm_symbol_text(base), _norm_symbol_text(base + quote)}
        score = 0
        if req in keys:
            score = 100
        elif any(req in k or k in req for k in keys if k):
            score = 70
        elif req and any(k.startswith(req) or req.startswith(k) for k in keys if k):
            score = 50
        if score:
            item = _instrument_entry(row)
            item["score"] = score
            out.append((score, item))
    out.sort(key=lambda x: (-x[0], str(x[1].get("symbol") or "")))
    return [x[1] for x in out]


def _instrument_entry(row: Mapping[str, Any]) -> Dict[str, Any]:
    sym = str(row.get("symbol") or "").strip()
    base = str(row.get("baseAsset") or "").strip()
    quote = str(row.get("quoteAsset") or "").strip()
    return {
        "symbol": sym,
        "instrument": sym,
        "base": base,
        "quote": quote,
        "display_name": f"{sym} ({base}/{quote})" if base and quote else sym,
        "price_increment": str(row.get("tickSize") or ""),
        "size_increment": str(row.get("lotSize") or ""),
        "minimum_size": str(row.get("lotSize") or ""),
        "min_notional": str(row.get("minNotional") or ""),
        "status": str(row.get("status") or ""),
    }


def _find_market(credentials: Mapping[str, str], requested: Any) -> Dict[str, Any]:
    text = str(requested or "").strip()
    if not text:
        raise ValueError("INSTRUMENT_NOT_FOUND")
    candidates = _instrument_candidates(credentials, text)
    if candidates:
        sym = str(candidates[0].get("symbol") or "").strip()
        for row in _exchange_info(credentials):
            if str(row.get("symbol") or "").strip().upper() == sym.upper():
                return row
    native = _native_symbol(text)
    for row in _exchange_info(credentials):
        if str(row.get("symbol") or "").strip().upper() == native.upper():
            return row
    raise ValueError("INSTRUMENT_NOT_FOUND")


def _ticker(credentials: Mapping[str, str], symbol: str) -> Dict[str, Any]:
    payload = _get_json(str(credentials.get("base_url") or DEFAULT_API_BASE), f"/ticker/{urllib.parse.quote(symbol, safe='')}")
    return payload if isinstance(payload, dict) else {}


def _price_from_ticker(payload: Mapping[str, Any]) -> Optional[str]:
    for key in ("markPrice", "lastPrice", "price", "oraclePrice"):
        if payload.get(key) is not None:
            return _plain_decimal(payload.get(key))
    return None


def _list_instruments(credentials: Mapping[str, str], account: str) -> CanonicalResponse:
    instruments = [_instrument_entry(row) for row in _exchange_info(credentials)]
    return make_success("list_instruments", name, account, data={"instruments": instruments})


def _resolve_instrument(credentials: Mapping[str, str], account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    requested = str(request.get("symbol") or request.get("query") or "").strip()
    try:
        row = _find_market(credentials, requested)
    except ValueError:
        cands = _instrument_candidates(credentials, requested)[:5]
        return make_failure(
            "resolve_instrument", name, account, "INSTRUMENT_NOT_FOUND",
            f"Bulk instrument not found: {requested}",
            exchange_reason=None,
        ) if not cands else make_failure(
            "resolve_instrument", name, account, "INSTRUMENT_NOT_FOUND",
            f"Bulk instrument not found: {requested}",
            exchange_reason=None,
        )
    entry = _instrument_entry(row)
    sym = entry["symbol"]
    inst = CanonicalInstrument(
        requested_symbol=requested,
        symbol=sym,
        display_name=entry["display_name"],
        price_increment=entry.get("price_increment") or None,
        size_increment=entry.get("size_increment") or None,
        minimum_size=entry.get("minimum_size") or None,
    )
    price = None
    try:
        price = _price_from_ticker(_ticker(credentials, sym))
    except Exception:
        price = None
    data = dict(entry)
    if price:
        data["price"] = price
    return make_success("resolve_instrument", name, account, instrument=inst, data=data)


def _market_price(credentials: Mapping[str, str], account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    requested = str(request.get("symbol") or request.get("query") or "").strip()
    row = _find_market(credentials, requested)
    sym = str(row.get("symbol") or "").strip()
    tick = _ticker(credentials, sym)
    price = _price_from_ticker(tick)
    if not price:
        return make_failure("market_price", name, account, "PRICE_UNAVAILABLE", f"Bulk price unavailable for {sym}")
    mp = CanonicalMarketPrice(
        requested_symbol=requested,
        market=sym,
        mark_price=price,
        oracle_price=_plain_decimal(tick.get("oraclePrice")) if tick.get("oraclePrice") is not None else None,
        last_external_price=_plain_decimal(tick.get("lastPrice")) if tick.get("lastPrice") is not None else price,
        price=price,
    )
    return make_success("market_price", name, account, market_price=mp, data={"symbol": sym, "price": price, "mark_price": price})


# ---------------------------------------------------------------------------
# Bulk signing / trading helpers
# ---------------------------------------------------------------------------

def _domain_byte(credentials: Mapping[str, str]) -> int:
    configured = str(credentials.get("signature_domain") or "").strip().lower()
    if configured in {"1", "mainnet", "main"}:
        return 1
    if configured in {"2", "testnet", "test"}:
        return 2
    if configured in {"3", "devnet", "dev"}:
        return 3
    base = str(credentials.get("base_url") or DEFAULT_API_BASE).lower()
    if "exchange-api.bulk.trade" in base:
        return 2
    return 1


def _b58decode(value: str) -> bytes:
    import base58  # type: ignore
    raw = base58.b58decode(str(value or ""))
    if len(raw) != 32:
        raise ValueError("Bulk public key/order id must decode to 32 bytes")
    return raw


def _b58encode(value: bytes) -> str:
    import base58  # type: ignore
    return base58.b58encode(value).decode("ascii")


def _write_string(value: str) -> bytes:
    raw = str(value).encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _write_bool(value: bool) -> bytes:
    return b"\x01" if value else b"\x00"


def _write_scaled(value: Any) -> bytes:
    scaled = int((_decimal(value) * Decimal("100000000")).to_integral_value(rounding=ROUND_HALF_UP))
    if scaled < 0:
        raise ValueError("Bulk scaled numeric field cannot be negative")
    return struct.pack("<Q", scaled)


def _encode_action(action: Mapping[str, Any]) -> bytes:
    if "m" in action:
        row = action["m"]
        return b"".join([
            struct.pack("<I", 0), _write_string(row["c"]), _write_bool(bool(row["b"])),
            _write_scaled(row["sz"]), _write_bool(bool(row.get("r", False))), _write_bool(bool(row.get("i", False))),
        ])
    if "l" in action:
        row = action["l"]
        tif_map = {"GTC": 0, "IOC": 1, "ALO": 2}
        tif = tif_map.get(str(row.get("tif") or "GTC").upper())
        if tif is None:
            raise ValueError("Unsupported Bulk time-in-force")
        return b"".join([
            struct.pack("<I", 1), _write_string(row["c"]), _write_bool(bool(row["b"])),
            _write_scaled(row["px"]), _write_scaled(row["sz"]), struct.pack("<I", tif),
            _write_bool(bool(row.get("r", False))), _write_bool(bool(row.get("i", False))),
        ])
    if "cx" in action:
        row = action["cx"]
        return b"".join([struct.pack("<I", 3), _write_string(row["c"]), _b58decode(str(row["oid"]))])
    if "cxa" in action:
        row = action["cxa"]
        symbols = list(row.get("c") or [])
        return b"".join([struct.pack("<I", 4), struct.pack("<Q", len(symbols)), *[_write_string(s) for s in symbols]])
    raise ValueError("Unsupported Bulk action for signing")


def _sign_transaction(credentials: Mapping[str, str], actions: list[Mapping[str, Any]], nonce: Optional[int] = None) -> Dict[str, Any]:
    from nacl.signing import SigningKey  # type: ignore
    import base58  # type: ignore

    account_pubkey = str(credentials.get("account_pubkey") or "")
    account_bytes = _b58decode(account_pubkey)
    secret_raw = base58.b58decode(str(credentials.get("agent_private_key") or ""))
    if len(secret_raw) == 64:
        seed = secret_raw[:32]
    elif len(secret_raw) == 32:
        seed = secret_raw
    else:
        raise ValueError("Bulk agent private key must be a 32-byte seed or 64-byte keypair encoded as base58")
    signing_key = SigningKey(seed)
    derived_signer = _b58encode(bytes(signing_key.verify_key))
    configured_signer = str(credentials.get("agent_public_key") or "").strip()
    signer_pubkey = configured_signer or derived_signer
    if configured_signer and configured_signer != derived_signer:
        raise ValueError("Configured Bulk agent public key does not match the private key")
    tx_nonce = int(nonce if nonce is not None else time.time_ns())
    message = b"".join([
        struct.pack("<Q", len(actions)),
        *[_encode_action(a) for a in actions],
        struct.pack("<Q", tx_nonce),
        account_bytes,
        bytes([_domain_byte(credentials)]),
    ])
    signature = signing_key.sign(message).signature
    return {
        "actions": [dict(a) for a in actions],
        "nonce": str(tx_nonce),
        "account": account_pubkey,
        "signer": signer_pubkey,
        "signature": _b58encode(signature),
    }


def _submit_order(credentials: Mapping[str, str], actions: list[Mapping[str, Any]]) -> Dict[str, Any]:
    signed = _sign_transaction(credentials, actions)
    payload = _post_json(str(credentials.get("base_url") or DEFAULT_API_BASE), "/order", signed)
    return payload if isinstance(payload, dict) else {"response": payload}


def _native_symbol(symbol: Any) -> str:
    text = str(symbol or "").strip().upper().replace("/", "-")
    if not text:
        raise ValueError("MISSING_SYMBOL")
    if "-" not in text:
        text = f"{text}-USD"
    return text


def _live_account(credentials: Mapping[str, str]) -> Dict[str, Any]:
    return _extract_full_account(_account_query(credentials, "fullAccount"))


def _orders_for(full_account: Mapping[str, Any], native: str, side: Optional[str] = None) -> list[Dict[str, Any]]:
    raw = full_account.get("openOrders")
    rows = raw if isinstance(raw, list) else []
    out: list[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or str(row.get("symbol") or "").upper() != native.upper():
            continue
        size = _decimal(row.get("size", row.get("originalSize")))
        row_side = "buy" if size > 0 else "sell"
        if side and row_side != side:
            continue
        out.append(row)
    return out


def _extract_status(payload: Mapping[str, Any]) -> str:
    status = payload.get("status") or payload.get("type") or ""
    if isinstance(payload.get("response"), dict):
        status = payload["response"].get("type") or status
    return str(status or "")


def _new_order(credentials: Mapping[str, str], account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    row = _find_market(credentials, request.get("symbol"))
    symbol = str(row.get("symbol") or "").strip()
    side = str(request.get("side") or "").strip().lower()
    if side not in {"buy", "sell"}:
        return make_failure("new_order", name, account, "INVALID_SIDE", "Side must be buy or sell.")
    order_type = str(request.get("order_type") or "limit").strip().lower()
    volume = _decimal(request.get("volume") or request.get("size"))
    price = _decimal(request.get("price"))
    if volume <= 0 or (order_type == "limit" and price <= 0):
        return make_failure("new_order", name, account, "INVALID_REQUEST", "Positive volume and price are required.")
    if order_type not in {"limit", "market"}:
        return make_failure("new_order", name, account, "UNSUPPORTED_ORDER_TYPE", "Bulk adapter supports limit and market orders only.")
    action: Dict[str, Any]
    if order_type == "market":
        action = {"m": {"c": symbol, "b": side == "buy", "sz": float(volume), "r": bool(request.get("reduce_only", False)), "i": bool(request.get("isolated", False))}}
        submitted_price = "0"
    else:
        action = {"l": {"c": symbol, "b": side == "buy", "px": float(price), "sz": float(volume), "tif": "GTC", "r": bool(request.get("reduce_only", False)), "i": bool(request.get("isolated", False))}}
        submitted_price = _plain_decimal(price)
    try:
        payload = _submit_order(credentials, [action])
    except Exception as exc:  # noqa: BLE001
        if _is_rate_limit_error(exc):
            result = CanonicalOrderResult(
                symbol=_display_symbol(symbol), side=side, order_type=order_type,
                requested_volume=_plain_decimal(volume), requested_price=_plain_decimal(price),
                submitted_volume="0", submitted_price=submitted_price,
                verified=False, status="rate_limited",
            )
            return make_failure("new_order", name, account, "BULK_RATE_LIMITED", "Bulk rate-limited the order transaction; wait briefly and try again.", order=result)
        raise
    text = json.dumps(payload, sort_keys=True)
    ok = '"status": "ok"' in text or "resting" in text or "filled" in text or "working" in text or str(payload.get("status", "")).lower() == "ok"
    time.sleep(0.75)
    verification_error = None
    try:
        after = _live_account(credentials)
        live_orders = _orders_for(after, symbol, side)
    except Exception as exc:  # noqa: BLE001
        verification_error = sanitize_error_message(str(exc))
        live_orders = []
    verified = bool(live_orders) if order_type == "limit" else ok
    exchange_order_id = None
    for row in live_orders:
        exchange_order_id = row.get("orderId") or row.get("oid")
        break
    result = CanonicalOrderResult(
        symbol=_display_symbol(symbol), side=side, order_type=order_type,
        requested_volume=_plain_decimal(volume), requested_price=_plain_decimal(price),
        submitted_volume=_plain_decimal(volume), submitted_price=submitted_price,
        verified=verified, status="success" if verified else ("submitted" if ok else "failed"),
        exchange_order_id=exchange_order_id,
    )
    if ok:
        data: Dict[str, Any] = {"bulk_status": _extract_status(payload)}
        if verification_error:
            data["verification_error"] = verification_error
            data["verification_delayed"] = True
        return make_success("new_order", name, account, order=result, data=data)
    return make_failure("new_order", name, account, "ORDER_FAILED", _redact(text[:500], credentials), order=result)



def _quantize_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return units * step


def _ladder_children(request: Mapping[str, Any], row: Mapping[str, Any]) -> list[tuple[Decimal, Decimal]]:
    count = int(_decimal(request.get("order_count")))
    total = _decimal(request.get("total_volume"))
    start = _decimal(request.get("start_price"))
    end = _decimal(request.get("end_price"))
    if count <= 0 or total <= 0 or start <= 0 or end <= 0:
        raise ValueError("INVALID_LADDER_REQUEST")
    tick = _decimal(row.get("tickSize"), "0")
    lot = _decimal(row.get("lotSize"), "0")
    prices = []
    if count == 1:
        prices = [start]
    else:
        prices = [start + (end - start) * Decimal(i) / Decimal(count - 1) for i in range(count)]
    prices = [_quantize_down(p, tick) if tick > 0 else p for p in prices]
    distribution = str(request.get("distribution") or "uniform").strip().lower()
    if distribution == "half_gaussian" and count > 1:
        import math
        weights = [Decimal(str(math.exp(-(float(Decimal("3") * Decimal(count - 1 - i) / Decimal(count - 1)) ** 2) / 2))) for i in range(count)]
    else:
        weights = [Decimal("1")] * count
    total_weight = sum(weights)
    sizes = [total * w / total_weight for w in weights]
    sizes = [_quantize_down(sz, lot) if lot > 0 else sz for sz in sizes]
    children = [(p, sz) for p, sz in zip(prices, sizes) if p > 0 and sz > 0]
    if not children:
        raise ValueError("INVALID_LADDER_REQUEST")
    return children


def _chunk_list(items: list[Any], size: int) -> list[list[Any]]:
    if size <= 0:
        raise ValueError("batch size must be positive")
    return [items[i : i + size] for i in range(0, len(items), size)]


def _payload_ok(payload: Any) -> bool:
    text = json.dumps(payload, sort_keys=True) if not isinstance(payload, str) else payload
    if not isinstance(payload, Mapping):
        return "resting" in text or '"status": "ok"' in text
    status = str(payload.get("status", "")).lower()
    return status == "ok" or '"status": "ok"' in text or "resting" in text or "working" in text or "filled" in text


def _ladder(credentials: Mapping[str, str], account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    row = _find_market(credentials, request.get("symbol"))
    symbol = str(row.get("symbol") or "").strip()
    side = str(request.get("side") or "").strip().lower()
    if side not in {"buy", "sell"}:
        return make_failure("ladder", name, account, "INVALID_SIDE", "Side must be buy or sell.")
    try:
        children = _ladder_children(request, row)
    except Exception as exc:  # noqa: BLE001
        return make_failure("ladder", name, account, "INVALID_LADDER_REQUEST", sanitize_error_message(str(exc)))
    actions = [
        {"l": {"c": symbol, "b": side == "buy", "px": float(price), "sz": float(size), "tif": "GTC", "r": False, "i": False}}
        for price, size in children
    ]
    requested = int(_decimal(request.get("order_count")))
    action_batches = _chunk_list(actions, LADDER_BATCH_SIZE)
    child_batches = _chunk_list(children, LADDER_BATCH_SIZE)
    batch_plan = [{"index": i, "size": len(batch)} for i, batch in enumerate(action_batches)]
    batches: list[Dict[str, Any]] = []
    accepted_actions = 0
    accepted_volume = Decimal("0")
    accepted_order_ids: list[Any] = []
    first_error: Optional[str] = None
    rate_limited = False
    last_payload: Any = None

    for batch_index, (batch_actions, batch_children) in enumerate(zip(action_batches, child_batches)):
        batch_meta: Dict[str, Any] = {
            "index": batch_index,
            "requested": len(batch_actions),
            "ok": False,
            "accepted": 0,
        }
        try:
            payload = _submit_order_with_retries(
                credentials,
                batch_actions,
                max_retries=LADDER_BATCH_MAX_RETRIES,
                base_sleep=LADDER_BATCH_RETRY_BASE_SECONDS,
                label="ladder",
                batch_index=batch_index,
            )
            last_payload = payload
            ok = _payload_ok(payload)
            batch_meta["ok"] = ok
            batch_meta["bulk_status"] = _extract_status(payload) if isinstance(payload, Mapping) else None
            if ok:
                order_ids = _bulk_order_ids_from_payload(payload)
                accepted_order_ids.extend(order_ids)
                accepted_actions += len(batch_actions)
                accepted_volume += sum((sz for _p, sz in batch_children), Decimal("0"))
                batch_meta["accepted"] = len(batch_actions)
                if order_ids:
                    batch_meta["order_ids"] = order_ids
            else:
                text = json.dumps(payload, sort_keys=True)[:300]
                batch_meta["error"] = _redact(text, credentials)
                if first_error is None:
                    first_error = batch_meta["error"]
        except Exception as exc:  # noqa: BLE001
            err = sanitize_error_message(str(exc))
            batch_meta["error"] = _redact(err, credentials)
            if _is_rate_limit_error(exc):
                rate_limited = True
                batch_meta["rate_limited"] = True
                if first_error is None:
                    first_error = (
                        f"Bulk rate-limited ladder batch {batch_index + 1}/{len(action_batches)} "
                        f"after {LADDER_BATCH_MAX_RETRIES} retries; "
                        f"{accepted_actions}/{len(children)} children already resting."
                    )
                batches.append(batch_meta)
                logger.warning(
                    "Bulk ladder stopped after rate-limit on batch %s/%s (accepted=%s/%s)",
                    batch_index + 1,
                    len(action_batches),
                    accepted_actions,
                    len(children),
                )
                break
            if _is_ambiguous_submission_error(exc):
                batch_meta["status_unknown"] = True
                batches.append(batch_meta)
                return _non_retry_safe_ladder_failure(
                    account,
                    reason=err,
                    accepted_count=accepted_actions,
                    requested_count=requested,
                    accepted_order_ids=accepted_order_ids,
                    batch_plan=batch_plan,
                    batches=batches,
                )
            if first_error is None:
                first_error = batch_meta["error"]
            batches.append(batch_meta)
            break
        batches.append(batch_meta)
        if not batch_meta["ok"]:
            break
        if batch_index + 1 < len(action_batches):
            time.sleep(LADDER_BATCH_PAUSE_SECONDS)

    planned_volume = sum((sz for _p, sz in children), Decimal("0"))
    requested_volume = _plain_decimal(request.get("total_volume"))
    if accepted_actions == 0 and rate_limited:
        result = CanonicalLadderResult(
            symbol=_display_symbol(symbol),
            side=side,
            distribution=str(request.get("distribution") or "uniform"),
            requested_order_count=requested,
            submitted_order_count=0,
            requested_volume=requested_volume,
            submitted_volume="0",
            batch_count=len(batches),
            verified=False,
            partial=False,
            status="rate_limited",
            accepted_child_count=0,
            rate_limited=True,
            exchange_reason=first_error,
            batches=batches,
        )
        response = make_failure(
            "ladder",
            name,
            account,
            "BULK_RATE_LIMITED",
            first_error or "Bulk rate-limited the ladder batch transaction; wait briefly and try again.",
            ladder=result,
        )
        object.__setattr__(response, "data", {
            "requested": requested,
            "succeeded": 0,
            "failed": len(children),
            "batch_size": LADDER_BATCH_SIZE,
            "batch_count": len(batches),
            "batch_plan": batch_plan,
            "batches": batches,
            "rate_limited": True,
            "batch_pause_seconds": LADDER_BATCH_PAUSE_SECONDS,
            "batch_max_retries": LADDER_BATCH_MAX_RETRIES,
        })
        return response

    verification_error = None
    submitted = accepted_actions
    verified = False
    if accepted_actions > 0:
        time.sleep(1.0)
        try:
            after = _live_account(credentials)
            live_orders = _orders_for(after, symbol, side)
            submitted = len(live_orders)
            verified = submitted >= min(len(children), accepted_actions, requested) and accepted_actions >= len(children)
        except Exception as exc:  # noqa: BLE001
            verification_error = sanitize_error_message(str(exc))
            submitted = accepted_actions
            verified = False

    partial = accepted_actions > 0 and (accepted_actions < len(children) or not verified)
    if verified and accepted_actions >= len(children):
        status = "success"
    elif accepted_actions > 0 and verification_error:
        status = "submitted"
    elif accepted_actions > 0:
        status = "partial" if accepted_actions < len(children) or rate_limited else "submitted"
    else:
        status = "failed"

    try:
        result = CanonicalLadderResult(
            symbol=_display_symbol(symbol),
            side=side,
            distribution=str(request.get("distribution") or "uniform"),
            requested_order_count=requested,
            submitted_order_count=submitted,
            requested_volume=requested_volume,
            submitted_volume=_plain_decimal(accepted_volume if accepted_actions else planned_volume if status != "failed" else "0"),
            batch_count=len(batches),
            verified=verified,
            partial=partial or (not verified and accepted_actions > 0),
            status=status,
            accepted_child_count=accepted_actions,
            omitted_order_count=max(0, len(children) - accepted_actions),
            child_order_ids=list(accepted_order_ids) or None,
            rate_limited=True if rate_limited else (_is_rate_limit_error(RuntimeError(verification_error)) if verification_error else None),
            exchange_reason=first_error or verification_error,
            batches=batches,
        )
    except Exception as exc:  # noqa: BLE001
        if accepted_actions > 0:
            return _non_retry_safe_ladder_failure(
                account,
                reason=str(exc),
                accepted_count=accepted_actions,
                requested_count=requested,
                accepted_order_ids=accepted_order_ids,
                batch_plan=batch_plan,
                batches=batches,
            )
        raise
    data: Dict[str, Any] = {
        "requested": requested,
        "succeeded": accepted_actions,
        "failed": max(0, len(children) - accepted_actions),
        "batch_size": LADDER_BATCH_SIZE,
        "batch_count": len(batches),
        "batch_plan": batch_plan,
        "accepted_order_ids": list(accepted_order_ids),
        "batches": batches,
        "rate_limited": bool(rate_limited),
        "batch_pause_seconds": LADDER_BATCH_PAUSE_SECONDS,
        "batch_max_retries": LADDER_BATCH_MAX_RETRIES,
    }
    if verification_error:
        data["verification_error"] = verification_error
        data["verification_delayed"] = True
    if accepted_actions > 0 and (verified or verification_error or accepted_actions == len(children)):
        return make_success("ladder", name, account, ladder=result, data=data)
    if accepted_actions > 0:
        return make_success("ladder", name, account, ladder=result, data=data)
    fail_text = first_error or (json.dumps(last_payload, sort_keys=True)[:500] if last_payload is not None else "ladder failed")
    return make_failure("ladder", name, account, "LADDER_FAILED", _redact(str(fail_text), credentials), ladder=result)


def _cancel_order_group(credentials: Mapping[str, str], account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    row = _find_market(credentials, request.get("symbol"))
    symbol = str(row.get("symbol") or "").strip()
    side = str(request.get("side") or "").strip().lower()
    if side in {"long"}:
        side = "buy"
    if side in {"short"}:
        side = "sell"
    if side not in {"buy", "sell"}:
        return make_failure("cancel_order_group", name, account, "INVALID_SIDE", "Side must be buy or sell.")
    try:
        before = _live_account(credentials)
    except Exception as exc:  # noqa: BLE001
        if _is_rate_limit_error(exc):
            result = CanonicalCancelGroupResult(
                symbol=_display_symbol(symbol),
                side=side,
                targeted_order_count=0,
                cancelled_order_count=0,
                confirmed_absent_count=0,
                remaining_target_count=0,
                verified=False,
                partial=False,
                status="rate_limited",
                batch_count=0,
                requested_cancel_count=0,
                verified_cancel_count=0,
                rate_limited=True,
                exchange_reason="Bulk rate-limited the open-order read before cancel; wait briefly and try again.",
            )
            return make_failure(
                "cancel_order_group",
                name,
                account,
                "BULK_RATE_LIMITED",
                "Bulk rate-limited the open-order read before cancel; wait briefly and try again.",
                cancel_group=result,
            )
        raise
    targets = _orders_for(before, symbol, side)
    target_ids = [str(r.get("orderId") or r.get("oid") or "") for r in targets if r.get("orderId") or r.get("oid")]
    if not target_ids:
        result = CanonicalCancelGroupResult(_display_symbol(symbol), side, 0, 0, 0, 0, True, False, "success", 0)
        return make_success("cancel_order_group", name, account, cancel_group=result)

    actions = [{"cx": {"c": symbol, "oid": oid}} for oid in target_ids]
    action_batches = _chunk_list(actions, CANCEL_BATCH_SIZE)
    id_batches = _chunk_list(target_ids, CANCEL_BATCH_SIZE)
    batches: list[Dict[str, Any]] = []
    accepted_ids: list[str] = []
    first_error: Optional[str] = None
    rate_limited = False
    last_payload: Any = None

    for batch_index, (batch_actions, batch_ids) in enumerate(zip(action_batches, id_batches)):
        batch_meta: Dict[str, Any] = {
            "index": batch_index,
            "requested": len(batch_actions),
            "ok": False,
            "accepted": 0,
        }
        try:
            payload = _submit_order_with_retries(
                credentials,
                batch_actions,
                max_retries=CANCEL_BATCH_MAX_RETRIES,
                base_sleep=CANCEL_BATCH_RETRY_BASE_SECONDS,
                label="cancel",
                batch_index=batch_index,
            )
            last_payload = payload
            ok = _payload_ok(payload) or "cancelled" in json.dumps(payload, sort_keys=True).lower()
            batch_meta["ok"] = ok
            batch_meta["bulk_status"] = _extract_status(payload) if isinstance(payload, Mapping) else None
            if ok:
                accepted_ids.extend(batch_ids)
                batch_meta["accepted"] = len(batch_ids)
            else:
                text = json.dumps(payload, sort_keys=True)[:300]
                batch_meta["error"] = _redact(text, credentials)
                if first_error is None:
                    first_error = batch_meta["error"]
        except Exception as exc:  # noqa: BLE001
            err = sanitize_error_message(str(exc))
            batch_meta["error"] = _redact(err, credentials)
            if _is_rate_limit_error(exc):
                rate_limited = True
                batch_meta["rate_limited"] = True
                if first_error is None:
                    first_error = (
                        f"Bulk rate-limited cancel batch {batch_index + 1}/{len(action_batches)} "
                        f"after {CANCEL_BATCH_MAX_RETRIES} retries; "
                        f"{len(accepted_ids)}/{len(target_ids)} cancels already accepted."
                    )
                batches.append(batch_meta)
                logger.warning(
                    "Bulk cancel stopped after rate-limit on batch %s/%s (accepted=%s/%s)",
                    batch_index + 1,
                    len(action_batches),
                    len(accepted_ids),
                    len(target_ids),
                )
                break
            if first_error is None:
                first_error = batch_meta["error"]
            batches.append(batch_meta)
            break
        batches.append(batch_meta)
        if not batch_meta["ok"]:
            break
        if batch_index + 1 < len(action_batches):
            time.sleep(CANCEL_BATCH_PAUSE_SECONDS)

    if not accepted_ids and rate_limited:
        result = CanonicalCancelGroupResult(
            symbol=_display_symbol(symbol),
            side=side,
            targeted_order_count=len(target_ids),
            cancelled_order_count=0,
            confirmed_absent_count=0,
            remaining_target_count=len(target_ids),
            verified=False,
            partial=False,
            status="rate_limited",
            batch_count=len(batches),
            batches=batches,
            requested_cancel_count=len(target_ids),
            verified_cancel_count=0,
            rate_limited=True,
            exchange_reason=first_error,
        )
        return make_failure(
            "cancel_order_group",
            name,
            account,
            "BULK_RATE_LIMITED",
            first_error or "Bulk rate-limited the cancel batch transaction; wait briefly and try again.",
            cancel_group=result,
        )

    verification_error = None
    confirmed = 0
    remaining = len(target_ids)
    if accepted_ids:
        time.sleep(1.0)
        try:
            after = _live_account(credentials)
            remaining_ids = {
                str(r.get("orderId") or r.get("oid") or "")
                for r in _orders_for(after, symbol, side)
            }
            confirmed = sum(1 for oid in target_ids if oid not in remaining_ids)
            remaining = len(target_ids) - confirmed
        except Exception as exc:  # noqa: BLE001
            verification_error = sanitize_error_message(str(exc))
            # Treat accepted submit batches as cancelled when verify is rate-limited.
            confirmed = len(accepted_ids)
            remaining = len(target_ids) - confirmed

    verified = remaining == 0 and not verification_error and len(accepted_ids) >= len(target_ids)
    partial = (not verified) and (confirmed > 0 or len(accepted_ids) > 0)
    if verified:
        status = "success"
    elif rate_limited and confirmed == 0 and not accepted_ids:
        status = "rate_limited"
    elif verification_error and accepted_ids:
        status = "submitted"
    elif partial:
        status = "partial"
    else:
        status = "failed"

    result = CanonicalCancelGroupResult(
        symbol=_display_symbol(symbol),
        side=side,
        targeted_order_count=len(target_ids),
        cancelled_order_count=confirmed,
        confirmed_absent_count=confirmed if not verification_error else 0,
        remaining_target_count=remaining,
        verified=verified,
        partial=partial,
        status=status,
        batch_count=len(batches),
        batches=batches,
        requested_cancel_count=len(target_ids),
        verified_cancel_count=confirmed if not verification_error else None,
        rate_limited=True if rate_limited else (_is_rate_limit_error(RuntimeError(verification_error)) if verification_error else None),
        exchange_reason=first_error or verification_error,
    )
    data: Dict[str, Any] = {
        "requested": len(target_ids),
        "succeeded": confirmed,
        "failed": remaining,
        "batch_size": CANCEL_BATCH_SIZE,
        "batch_count": len(batches),
        "batch_plan": [{"index": i, "size": len(b)} for i, b in enumerate(action_batches)],
        "batches": batches,
        "accepted_submit_count": len(accepted_ids),
    }
    if verification_error:
        data["verification_error"] = verification_error
        data["verification_delayed"] = True
    if last_payload is not None and isinstance(last_payload, Mapping):
        data["bulk_status"] = _extract_status(last_payload)
    if verified or (accepted_ids and (verification_error or confirmed > 0)):
        return make_success("cancel_order_group", name, account, cancel_group=result, data=data)
    fail_text = first_error or (
        json.dumps(last_payload, sort_keys=True)[:500] if last_payload is not None else "cancel failed"
    )
    return make_failure(
        "cancel_order_group",
        name,
        account,
        "CANCEL_FAILED",
        _redact(str(fail_text), credentials),
        cancel_group=result,
    )

def _balance(credentials: Mapping[str, str], account: str) -> CanonicalResponse:
    payload = _account_query(credentials, "fullAccount")
    full_account = _extract_full_account(payload)
    if not full_account:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Bulk account snapshot was empty.",
        )
    summary = _portfolio_from_account(full_account)
    return make_success(
        operation="balance",
        exchange=name,
        account=account,
        balance=CanonicalBalance(value=summary.account_value, unit=summary.unit),
        portfolio_summary=summary,
        data={
            "account_pubkey": str(credentials.get("account_pubkey") or ""),
            "kind": full_account.get("kind"),
            "position_count": len(full_account.get("positions") or []),
            "open_order_count": _open_order_count(full_account),
        },
    )


def _positions_orders(
    credentials: Mapping[str, str],
    account: str,
    *,
    operation: str = "positions_orders",
) -> CanonicalResponse:
    payload = _account_query(credentials, "fullAccount")
    full_account = _extract_full_account(payload)
    if not full_account:
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message="Bulk account snapshot was empty.",
        )
    summary = _portfolio_from_account(full_account)
    positions = _positions_from_account(full_account)
    # Wizard Positions Management only needs the position rows; still include
    # order metadata so Positions & Orders stays complete on the same path.
    return make_success(
        operation=operation,
        exchange=name,
        account=account,
        portfolio_summary=summary,
        positions=positions,
        open_order_count=_open_order_count(full_account),
        order_groups=_order_groups_from_account(full_account),
        data={
            "account_pubkey": str(credentials.get("account_pubkey") or ""),
            "kind": full_account.get("kind"),
            "position_count": len(positions),
        },
    )


def execute(request: Dict[str, Any]) -> CanonicalResponse:
    operation = str(request.get("operation") or "").strip()
    account = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation=operation or "",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Bulk account alias is not configured or has an invalid account public key.",
        )

    try:
        if operation == "balance":
            return _balance(credentials, account)
        if operation == "positions_orders":
            return _positions_orders(credentials, account, operation="positions_orders")
        if operation == "positions_management":
            # Wizard list op — same fullAccount snapshot as positions_orders.
            # Mutation actions (set_tp/set_sl/close_position) are separate ops
            # and remain NOT_IMPLEMENTED until explicitly wired.
            return _positions_orders(credentials, account, operation="positions_management")
        if operation == "list_instruments":
            return _list_instruments(credentials, account)
        if operation == "resolve_instrument":
            return _resolve_instrument(credentials, account, request)
        if operation == "market_price":
            return _market_price(credentials, account, request)
        if operation == "new_order":
            return _new_order(credentials, account, request)
        if operation == "ladder":
            return _ladder(credentials, account, request)
        if operation == "cancel_order_group":
            return _cancel_order_group(credentials, account, request)
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="NOT_IMPLEMENTED",
            message=f"Bulk operation {operation!r} is not implemented.",
        )
    except Exception as exc:  # noqa: BLE001 - canonical envelope boundary
        logger.warning("Bulk %s failed: %s", operation, _redact(exc, credentials))
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="BULK_REQUEST_FAILED",
            message=_redact(exc, credentials),
        )
