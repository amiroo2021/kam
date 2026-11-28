"""Bulk Trade exchange agent for the /trade stack.

Credentials (``.env`` / environment):
  ``BULK_<ALIAS>_ACCOUNT``             account public key (base58)
  ``BULK_<ALIAS>_AGENT_PRIVATE_KEY``   agent signer key (required for discovery
                                        and signed writes)
  Optional: ``BULK_<ALIAS>_BASE_URL``  default https://mainnet-api1.bulk.trade/api/v1

Reads use unsigned POST /account {type: fullAccount, user}.
Writes use signed POST /order (new_order, ladder, cancel_order_group, close_position,
set_tp, set_sl).

Wizard Positions Management lists via operation ``positions_management``,
which is an alias of the same fullAccount snapshot as ``positions_orders``.
Bulk-native ``close_position``, ``set_tp`` and ``set_sl`` are supported.

Signing
-------
Transactions are signed with the Bulk "wincode" canonical binary format, the
same layout the official ``bulk-keychain`` SDK produces. The 4 October 2026
upgrade moved the wire to transaction ``version 12``: the signed message is
prefixed with ``0c 00 00 00`` (u32 LE version 12) and the JSON envelope
declares ``"version": 12``. Version 11 (and an omitted version) uses the
versionless framing ``[u64 count][actions][u64 nonce][32 account][1 domain]``.

The two framings are mutually exclusive: the node selects its parser from the
envelope's ``version`` field, so v12 bytes sent without ``"version": 12`` are
parsed as v11 and rejected. Conditional actions (``st``/``tp``) additionally
carry trailing ``commission``/``slippage`` Option tags that the pre-upgrade
layout lacked, so a pre-upgrade conditional encoder is rejected under both
framings.
"""

from __future__ import annotations

import json
import logging
import os
import re
import struct
import threading
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
    CanonicalPositionActionResult,
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

# Bulk transaction wire/signing schema version.
#
# Bulk's own signed-message formula (docs.bulk.trade/api-reference/signing.md)
# has NO version prefix:
#
#     binary_message = wincode_serialize(actions)
#                   || nonce_le_u64 || account_pubkey_32_bytes || domain_u8
#
# and the published request envelope has no "version" field. The 4 October
# 2026 upgrade note only requires the CONDITIONAL-action schema to change
# (trailing commission/slippage Option tags); it does not document a version
# prefix on the signed bytes.
#
# The two framings are:
#   * "versionless" (version 11): no u32 prefix, no "version" envelope field.
#   * "version 12":  u32 LE prefix 0c 00 00 00 + "version": 12 in the envelope
#                   (the field MUST be first in the JSON object - Bulk
#                   deserializes the envelope as a struct and rejects
#                   "version must precede actions").
#
# The two networks verify DIFFERENT framings. Both facts were established
# live, with real orders:
#   * TESTNET  (exchange-api.bulk.trade) accepts the version-12-prefixed
#               bytes and REJECTS the versionless bytes with
#               {"error":{"message":"bad signature"}}.
#   * MAINNET  (mainnet-api1.bulk.trade) verifies the VERSIONLESS bytes
#               (commit f288175 confirmed this with a live order) and rejects
#               the version-12-prefixed bytes with "bad signature".
#
# This split is what made the 2026-11-27 18:39 production take-profit fail:
# the revision that deployed version 12 to mainnet produced a signature the
# mainnet node refused, so the transaction never reached order admission and
# no order row was ever created.
#
# A signature failure happens BEFORE order admission and therefore creates
# NO order history row, so re-signing the same actions with the other framing
# can never duplicate an order.
_TRANSACTION_VERSION_MAINNET = None   # versionless framing on mainnet
_TRANSACTION_VERSION_TESTNET = 12     # version-12 framing on testnet

# Transaction-level error messages that mean "the node refused the signed
# bytes" rather than "the order was rejected by a business rule". These are
# returned as HTTP 200 with status "error" and create NO order history row.
_SIGNATURE_ERROR_MESSAGES = ("bad signature", "unauthorized signer")

# A per-account override lets an operator pin the framing for a specific
# account without a code change: BULK_<ALIAS>_TRANSACTION_VERSION=11 or 12.
_TRANSACTION_VERSION_SUFFIXES = ("TRANSACTION_VERSION", "TX_VERSION", "WIRE_VERSION")

# Sentinel so ``version=None`` can mean "versionless framing" while the
# default ("use the account/module default") stays distinguishable.
_VERSION_UNSET = object()

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
        set(
            _ACCOUNT_SUFFIXES + _PRIVATE_KEY_SUFFIXES + _BASE_URL_SUFFIXES
            + _AGENT_PUBLIC_KEY_SUFFIXES + _SIGNATURE_DOMAIN_SUFFIXES
            + _TRANSACTION_VERSION_SUFFIXES
        ),
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
        elif suffix in _TRANSACTION_VERSION_SUFFIXES:
            slot.setdefault("transaction_version", val.strip())

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
    # set_tp / set_sl use Bulk's native conditional orders (tp/st) on the
    # exchange trigger book; close_position submits a reduce-only market order.
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
        "set_tp",
        "set_sl",
        "close_position",
    ]


def _is_testnet_endpoint(base_url: Any) -> bool:
    """True when the base URL points at Bulk's testnet."""
    return "exchange-api.bulk.trade" in str(base_url or "").lower()


def _default_transaction_version(credentials: Mapping[str, str]) -> Optional[int]:
    """The framing this endpoint is expected to verify.

    Mainnet verifies the versionless bytes, testnet verifies the version-12
    bytes. Both were established live with real orders.
    """
    base = str((credentials or {}).get("base_url") or DEFAULT_API_BASE)
    if _is_testnet_endpoint(base):
        return _TRANSACTION_VERSION_TESTNET
    return _TRANSACTION_VERSION_MAINNET


def _transaction_version_for(credentials: Mapping[str, str]) -> Optional[int]:
    """Resolve the transaction framing version for one account.

    An explicit per-account override (``BULK_<ALIAS>_TRANSACTION_VERSION``)
    wins; otherwise the endpoint's default framing is used. ``11``,
    ``versionless`` and ``none`` select the legacy versionless framing.
    """
    raw = str((credentials or {}).get("transaction_version") or "").strip().lower()
    if raw:
        if raw in {"none", "versionless", "legacy", "v11", "11"}:
            return None
        try:
            return int(raw)
        except ValueError:
            pass
    return _default_transaction_version(credentials)


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
        "transaction_version": str(fields.get("transaction_version") or "").strip(),
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


def _conditional_orders(credentials: Mapping[str, str]) -> list[Dict[str, Any]]:
    """Return every live conditional (trigger-book) order for the account.

    Bulk keeps conditional orders on a separate trigger book. The dedicated
    ``openOrders`` query is documented to include "conditional orders parked
    in conditional books", so it is the authoritative source for TP/SL state;
    the ``fullAccount`` snapshot may not carry them.
    """
    payload = _account_query(credentials, "openOrders")
    rows: list[Dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            row = item.get("openOrder")
            rows.append(dict(row) if isinstance(row, dict) else dict(item))
    elif isinstance(payload, dict):
        raw = payload.get("openOrders")
        if isinstance(raw, list):
            rows.extend(dict(r) for r in raw if isinstance(r, dict))
        else:
            for item in payload.get("data") or []:
                if isinstance(item, dict):
                    row = item.get("openOrder")
                    rows.append(dict(row) if isinstance(row, dict) else dict(item))
    return rows


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
    """Derive display TP/SL from Bulk's conditional (trigger book) rows.

    Keyed by (display_symbol, position_side long|short). A take profit above
    the mark protects a long and a stop below it protects a long; a short is
    the mirror.
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
        classification = _classification(str(row.get("orderType") or ""), bool(row.get("reduceOnly")))
        if classification not in {"take_profit", "stop_loss"}:
            continue
        # The resting book's size sign is the TRIGGER LEG, not the position
        # side. The position a conditional protects is identified by the
        # trigger direction together with the action kind:
        #   take profit above -> protects a long    (take profit below -> short)
        #   stop below        -> protects a long    (stop above -> short)
        position_side = _conditional_protected_side(row)
        if position_side is None:
            continue
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


def _write_scaled_option(value: Any) -> bytes:
    """Option<scaled f64>: 1-byte tag (0=None, 1=Some) + u64 LE when Some."""
    if value is None:
        return b"\x00"
    return b"\x01" + _write_scaled(value)


def _write_option_commission(row: Mapping[str, Any]) -> bytes:
    """Optional builderCode: absent contributes no bytes, present is
    tag(1) + recipient pubkey(32) + fee bps(u8)."""
    code = row.get("builderCode")
    if not isinstance(code, Mapping):
        return b"\x00"
    recipient = str(code.get("to") or "").strip()
    if not recipient:
        return b"\x00"
    fee = int(code.get("fee") or 0)
    if fee < 1 or fee > 15:
        raise ValueError("Bulk builder-code fee must be 1..=15 bps")
    return b"\x01" + _b58decode(recipient) + struct.pack("<B", fee)


def _write_conditional_action(discriminant: int, row: Mapping[str, Any]) -> bytes:
    """Encode a conditional action (``st``=5 / ``tp``=6).

    Layout (canonical wincode, matching bulk-keychain v0.1.32):
        disc u32 LE | symbol (u64 len + utf8) | d u8 | size u64 scaled
        | threshold u64 scaled | limit Option<scaled> | iso u8
        | commission Option | slippage Option<scaled>

    ``d`` is the TRIGGER DIRECTION (true = at/above the threshold, false =
    at/below). The official SDK calls this field ``is_buy``, which is
    misleading: Bulk gives each action type its own meaning for ``d`` and
    for ``st``/``tp`` it selects the trigger direction. The exchange executes
    a fired conditional as a reduce-only close against the position, so the
    executed side is always protective.

    A conditional that is already satisfied when it is submitted fires
    immediately and is then cancelled as IOC, so the trigger price must be
    placed on the side of the market that has not been reached yet.
    """
    symbol = str(row.get("c") or "").strip()
    if not symbol:
        raise ValueError("Bulk conditional action requires a symbol")
    size = _write_scaled(row.get("sz"))
    threshold = _write_scaled(row.get("tr"))
    limit = row.get("lim")
    limit_bytes = b"\x00" if limit is None else b"\x01" + _write_scaled(limit)
    commission_bytes = _write_option_commission(row)
    slippage = row.get("slippage")
    slippage_bytes = b"\x00" if slippage is None else b"\x01" + _write_scaled(slippage)
    return b"".join([
        struct.pack("<I", discriminant),
        _write_string(symbol),
        _write_bool(bool(row.get("d"))),
        size,
        threshold,
        limit_bytes,
        _write_bool(bool(row.get("i", False))),
        commission_bytes,
        slippage_bytes,
    ])


def _encode_action(action: Mapping[str, Any]) -> bytes:
    if "m" in action:
        row = action["m"]
        return b"".join([
            struct.pack("<I", 0), _write_string(row["c"]), _write_bool(bool(row["b"])),
            _write_scaled(row["sz"]), _write_bool(bool(row.get("r", False))), _write_bool(bool(row.get("i", False))),
            _write_option_commission(row),
            b"\x00" if row.get("slippage") is None else b"\x01" + _write_scaled(row.get("slippage")),
        ])
    if "l" in action:
        row = action["l"]
        tif_map = {"GTC": 0, "IOC": 1, "ALO": 2, "ALO_SLIDE": 3, "ALO_JOIN": 4}
        tif = tif_map.get(str(row.get("tif") or "GTC").upper())
        if tif is None:
            raise ValueError("Unsupported Bulk time-in-force")
        return b"".join([
            struct.pack("<I", 1), _write_string(row["c"]), _write_bool(bool(row["b"])),
            _write_scaled(row["px"]), _write_scaled(row["sz"]), struct.pack("<I", tif),
            _write_bool(bool(row.get("r", False))), _write_bool(bool(row.get("i", False))),
            _write_option_commission(row),
        ])
    if "st" in action:
        return _write_conditional_action(5, action["st"])
    if "tp" in action:
        return _write_conditional_action(6, action["tp"])
    if "cx" in action:
        row = action["cx"]
        return b"".join([struct.pack("<I", 3), _write_string(row["c"]), _b58decode(str(row["oid"]))])
    if "cxa" in action:
        row = action["cxa"]
        symbols = list(row.get("c") or [])
        return b"".join([struct.pack("<I", 4), struct.pack("<Q", len(symbols)), *[_write_string(s) for s in symbols]])
    raise ValueError("Unsupported Bulk action for signing")


def _sign_transaction(
    credentials: Mapping[str, str],
    actions: list[Mapping[str, Any]],
    nonce: Optional[int] = None,
    version: Any = _VERSION_UNSET,
) -> Dict[str, Any]:
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
    if version is _VERSION_UNSET:
        version = _transaction_version_for(credentials)
    prefix = b"" if version is None else struct.pack("<I", version)
    message = b"".join([
        prefix,
        struct.pack("<Q", len(actions)),
        *[_encode_action(a) for a in actions],
        struct.pack("<Q", tx_nonce),
        account_bytes,
        bytes([_domain_byte(credentials)]),
    ])
    signature = signing_key.sign(message).signature
    envelope: Dict[str, Any] = {}
    # The node selects its transaction parser from this field: absent (or 11)
    # means the versionless framing, 12 means the prefixed framing. The two are
    # mutually exclusive, so the field and the signed bytes must agree.
    #
    # The field MUST come first in the JSON object: Bulk deserializes the
    # envelope as a struct and rejects "version must precede actions".
    if version is not None:
        envelope["version"] = version
    envelope.update({
        "actions": [dict(a) for a in actions],
        "nonce": str(tx_nonce),
        "account": account_pubkey,
        "signer": signer_pubkey,
        "signature": _b58encode(signature),
    })
    return envelope


def _transaction_error_message(payload: Any) -> Optional[str]:
    """Return the node's transaction-level error message, if any.

    Bulk answers a signature/authorisation failure as HTTP 200 with
    ``status: "error"`` and ``statuses: [{"error": {"message": "..."}}]``.
    Such a response means the signed bytes never reached order admission, so
    NO order was created - it is safe to retry with a different framing.
    """
    if not isinstance(payload, Mapping):
        return None
    if str(payload.get("status") or "").lower() != "error":
        return None
    found: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, inner in value.items():
                if key == "error" and isinstance(inner, Mapping):
                    msg = inner.get("message") or inner.get("reason")
                    if msg:
                        found.append(str(msg))
                visit(inner)
        elif isinstance(value, list):
            for inner in value:
                visit(inner)

    visit(payload)
    return "; ".join(dict.fromkeys(found)) or None


def _is_signature_error(message: Optional[str]) -> bool:
    if not message:
        return False
    lowered = str(message).lower()
    return any(marker in lowered for marker in _SIGNATURE_ERROR_MESSAGES)


def _submit_order(credentials: Mapping[str, str], actions: list[Mapping[str, Any]]) -> Dict[str, Any]:
    """Sign and submit one Bulk transaction.

    The framing is chosen per endpoint: mainnet verifies the versionless
    bytes, testnet verifies the version-12 bytes. When the node refuses the
    signed bytes the node's own message is surfaced verbatim - the rejection
    reason is never swallowed or replaced with a generic text.
    """
    base = str(credentials.get("base_url") or DEFAULT_API_BASE)
    version = _transaction_version_for(credentials)
    signed = _sign_transaction(credentials, actions, version=version)
    payload = _post_json(base, "/order", signed)

    message = _transaction_error_message(payload)
    if _is_signature_error(message):
        # The node refused the signed bytes. Surface the exact reason; the
        # framing is a deliberate per-network decision, so it is never
        # silently switched behind the operator's back.
        logger.warning(
            "Bulk transaction rejected by the node: %s (framing=%s, endpoint=%s)",
            message,
            "versionless" if version is None else f"v{version}",
            base,
        )
        return {
            "status": "error",
            "response": {
                "type": "order",
                "data": {"statuses": [{"error": {"message": message}}]},
            },
        }
    return payload if isinstance(payload, dict) else {"response": payload}


# Records the framing an endpoint verified, for diagnostics only.
_TRANSACTION_VERSION_CACHE: Dict[str, Optional[int]] = {}


def _remember_transaction_version(credentials: Mapping[str, str], version: Optional[int]) -> None:
    """Record the framing an endpoint verified (kept for diagnostics)."""
    key = str((credentials or {}).get("base_url") or DEFAULT_API_BASE)
    _TRANSACTION_VERSION_CACHE[key] = version


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
    # Conditional orders live on Bulk's separate trigger book, which the
    # dedicated openOrders query exposes. Merge them into the snapshot so
    # the position rows can carry their live TP and SL.
    try:
        conditional_rows = _conditional_orders(credentials)
    except Exception:  # noqa: BLE001 - the snapshot alone is still usable
        conditional_rows = []
    if conditional_rows:
        merged = dict(full_account)
        existing = list(full_account.get("openOrders") or [])
        seen = {str(r.get("orderId") or r.get("oid") or "") for r in existing}
        for row in conditional_rows:
            oid = str(row.get("orderId") or row.get("oid") or "")
            if oid and oid in seen:
                continue
            # Only conditional rows belong on the trigger book; anything else
            # is already represented in the account snapshot.
            if _classification(str(row.get("orderType") or ""), bool(row.get("reduceOnly"))) \
                    not in {"take_profit", "stop_loss"}:
                continue
            existing.append(row)
        merged["openOrders"] = existing
        full_account = merged
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



_close_guard_lock = threading.Lock()
_close_guard: set[tuple[str, str, bool]] = set()


def _position_rows(full_account: Mapping[str, Any], symbol: str) -> list[Dict[str, Any]]:
    rows = full_account.get("positions")
    if not isinstance(rows, list):
        raise ValueError("Bulk fullAccount positions must be an array")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("Bulk fullAccount contains a malformed position row")
    return [
        row for row in rows
        if str(row.get("symbol") or "").strip().upper() == symbol.upper()
        and _decimal(row.get("size")) != 0
    ]


def _validate_close_snapshot(credentials: Mapping[str, str], account: str, snapshot: Any) -> Mapping[str, Any]:
    """Validate the account-bound fullAccount snapshot used by close verification."""
    expected_alias = str(credentials.get("account") or "").strip().casefold()
    if expected_alias and expected_alias != str(account or "").strip().casefold():
        raise ValueError("Bulk credentials do not match the requested account alias")
    account_pubkey = str(credentials.get("account_pubkey") or "").strip()
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", account_pubkey):
        raise ValueError("Bulk query account identity is missing or malformed")
    if not isinstance(snapshot, Mapping):
        raise ValueError("Bulk fullAccount response is not an object")
    for identity_key in ("user", "account", "account_pubkey", "pubkey"):
        echoed_identity = snapshot.get(identity_key)
        if echoed_identity is not None and str(echoed_identity).strip() != account_pubkey:
            raise PermissionError("Bulk fullAccount identity does not match the requested account")
    positions = snapshot.get("positions")
    if not isinstance(positions, list):
        raise ValueError("Bulk fullAccount response omitted its positions array")
    for row in positions:
        if not isinstance(row, Mapping) or not str(row.get("symbol") or "").strip():
            raise ValueError("Bulk fullAccount contains a malformed position row")
        try:
            position_size = Decimal(str(row.get("size")))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError("Bulk fullAccount contains an invalid position size") from exc
        if not position_size.is_finite():
            raise ValueError("Bulk fullAccount contains a non-finite position size")
    return snapshot


def _position_action_status(payload: Any) -> Dict[str, Any]:
    """Parse Bulk's tagged order statuses without treating transaction acceptance as a fill."""
    status_classes = {
        "filled": "filled",
        "partiallyFilled": "partially_filled",
        "working": "pending",
        "resting": "pending",
        "triggered": "pending",
        "cancelledIoc": "cancelled",
        "cancelledRiskLimit": "cancelled",
        "cancelledSelfCrossing": "cancelled",
        "cancelledReduceOnly": "cancelled",
        "cancelled": "cancelled",
        "siblingCancelled": "cancelled",
        "triggerFailed": "cancelled",
        "rejectedCrossing": "rejected",
        "rejectedDuplicate": "rejected",
        "rejectedRiskLimit": "rejected",
        "rejectedInvalid": "rejected",
        "cancelOneRejected": "rejected",
        "cancelAllRejected": "rejected",
        "error": "rejected",
        # Recognize the remaining documented transaction status tags, but
        # never interpret non-order actions as a close fill or rejection.
        "deposit": "unknown",
        "depositFailed": "unknown",
        "agentWallet": "unknown",
        "agentWalletFailed": "unknown",
        "transfer": "unknown",
        "transferFailed": "unknown",
        "createSubAccount": "unknown",
        "createSubAccountFailed": "unknown",
        "removeSubAccount": "unknown",
        "removeSubAccountFailed": "unknown",
        "renameSubAccount": "unknown",
        "renameSubAccountFailed": "unknown",
    }
    statuses: list[str] = []
    ids: list[str] = [str(x) for x in _bulk_order_ids_from_payload(payload)]
    reasons: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key in value:
                if key in status_classes:
                    statuses.append(key)
            for child in value.values():
                if isinstance(child, (Mapping, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)

    # Collect the exchange's own rejection reasons. Bulk reports them in two
    # shapes:
    #   * order-level: {"rejectedInvalid": {"oid": "...", "reason": "..."}}
    #   * transaction-level: {"error": {"message": "..."}}
    def collect_reasons(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, inner in value.items():
                if isinstance(inner, Mapping):
                    for reason_key in ("reason", "message"):
                        reason = inner.get(reason_key)
                        if reason:
                            text = str(reason).strip()
                            if text:
                                reasons.append(text)
                elif isinstance(inner, list):
                    collect_reasons(inner)
                elif key in {"reason", "message"} and isinstance(inner, str):
                    text = inner.strip()
                    if text:
                        reasons.append(text)
                collect_reasons(inner)
        elif isinstance(value, list):
            for inner in value:
                collect_reasons(inner)

    collect_reasons(payload)

    unique = list(dict.fromkeys(statuses))
    classification = status_classes[unique[0]] if len(unique) == 1 else "unknown"
    return {
        "classification": classification,
        "statuses": unique,
        "order_ids": ids,
        "reasons": list(dict.fromkeys(reasons)),
    }


def _lane_position_size(rows: list[Dict[str, Any]], isolated: bool) -> Decimal:
    lane_rows = [row for row in rows if row.get("iso") is isolated]
    if len(lane_rows) > 1:
        raise ValueError("Multiple Bulk position rows match the target market and lane")
    return _decimal(lane_rows[0].get("size")) if lane_rows else Decimal("0")


def _conditional_rows(full_account: Mapping[str, Any], symbol: str) -> list[Dict[str, Any]]:
    """Return the live conditional (TP/SL) rows for one native symbol.

    Bulk keeps conditional orders on a separate trigger book, but they are
    still reported in ``openOrders`` with an ``orderType`` of ``stop`` or
    ``takeProfit`` and a populated ``trigger`` block.
    """
    raw_rows = full_account.get("openOrders")
    rows: Iterable[Any] = raw_rows if isinstance(raw_rows, list) else []
    out: list[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or "").strip().upper() != symbol.upper():
            continue
        if _classification(str(row.get("orderType") or ""), bool(row.get("reduceOnly"))) not in {"take_profit", "stop_loss"}:
            continue
        out.append(row)
    return out


def _protection_rows(
    credentials: Mapping[str, str], symbol: str
) -> dict[str, list[Dict[str, Any]]]:
    """Bucket the account's live TP and SL conditionals for one symbol.

    Reads the dedicated ``openOrders`` query, which is the authoritative view
    of the conditional trigger book, and falls back to the account snapshot's
    own ``openOrders`` when the query is unavailable.
    """
    rows: list[Dict[str, Any]] = []
    try:
        rows = _conditional_orders(credentials)
    except Exception:  # noqa: BLE001 - fall back to the snapshot below
        rows = []
    if not rows:
        try:
            rows = _conditional_rows(_live_account(credentials), symbol)
        except Exception:  # noqa: BLE001
            rows = []
    return _bucket_protection_rows(rows, symbol)


def _bucket_protection_rows(
    rows: Iterable[Any], symbol: str
) -> dict[str, list[Dict[str, Any]]]:
    """Split conditional rows into take-profit and stop-loss buckets.

    A row is bucketed by its ACTION KIND (``takeProfit`` / ``stop``), not by
    its trigger direction and not by the sign of its resting size:

    * the action kind is what the exchange set the order up as;
    * the trigger direction says WHICH side of the mark the order waits on
      and, together with the kind, which position it protects;
    * the resting size sign reflects the trigger leg, not the position
      side, so it must not be used to tell a take profit from a stop loss.

    A row whose kind is unknown is not bucketed: guessing would let the
    agent cancel the wrong protection.
    """
    buckets: dict[str, list[Dict[str, Any]]] = {"tp": [], "sl": []}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or "").strip().upper() != symbol.upper():
            continue
        classification = _classification(
            str(row.get("orderType") or ""), bool(row.get("reduceOnly"))
        )
        if classification not in {"take_profit", "stop_loss"}:
            continue
        buckets["tp" if classification == "take_profit" else "sl"].append(row)
    return buckets


def _conditional_trigger_price(row: Mapping[str, Any]) -> Optional[Decimal]:
    """Read a conditional's trigger price (the ``trigger.px`` block first)."""
    trigger = row.get("trigger")
    if isinstance(trigger, Mapping):
        raw = trigger.get("px")
        if raw is not None:
            return _decimal(raw)
    for key in ("triggerPrice", "price"):
        raw = row.get(key)
        if raw is not None:
            return _decimal(raw)
    return None


def _protection_trigger_direction(kind: str, position_side: str) -> bool:
    """The trigger direction (``d``) of a protective conditional.

    ``d`` is the TRIGGER DIRECTION on the wire, not the closing side:
    ``true`` fires at/above the threshold, ``false`` fires at/below it.
    Bulk's own docs and a live testnet check agree:

        take profit : fires ABOVE for a long, BELOW for a short
        stop loss   : fires BELOW for a long, ABOVE for a short

    The official bulk-keychain SDK calls the same field ``is_buy``, which is
    misleading for ``st``/``tp``: for those actions the exchange reads it as
    the trigger direction. The executed order is always a reduce-only close,
    so the side that actually closes the position is a separate calculation
    (see :func:`_protection_closing_side`).
    """
    if kind == "tp":
        return position_side == "long"
    if kind == "st":
        return position_side == "short"
    raise ValueError(f"Unsupported Bulk protection kind: {kind!r}")


def _protection_closing_side(position_side: str) -> str:
    """The order side that closes a position.

    A long is closed by selling, a short by buying. This is deliberately
    kept apart from the trigger direction: the two answer different
    questions, and conflating them is what produced inverted take-profits.
    """
    return "sell" if position_side == "long" else "buy"


def _conditional_protected_side(row: Mapping[str, Any]) -> Optional[str]:
    """Which position side a live conditional protects.

    Derived from the action kind together with the trigger direction, which
    is what the exchange itself pairs them with:

        take profit above -> protects a long    (take profit below -> short)
        stop        below -> protects a long    (stop        above -> short)

    Returns ``None`` when the row is not a conditional or does not carry a
    usable trigger direction.
    """
    classification = _classification(
        str(row.get("orderType") or ""), bool(row.get("reduceOnly"))
    )
    if classification not in {"take_profit", "stop_loss"}:
        return None
    is_above = _conditional_is_above(row)
    if is_above is None:
        return None
    if classification == "take_profit":
        return "long" if is_above else "short"
    return "short" if is_above else "long"


def _conditional_is_above(row: Mapping[str, Any]) -> Optional[bool]:
    """Read a conditional's trigger direction (``trigger.isAbove``)."""
    trigger = row.get("trigger")
    if isinstance(trigger, Mapping) and trigger.get("isAbove") is not None:
        return bool(trigger.get("isAbove"))
    return None


def _protection_rows_for_position(
    credentials: Mapping[str, str],
    full_account: Mapping[str, Any],
    symbol: str,
    isolated: bool,
    position_side: Optional[str] = None,
) -> dict[str, list[Dict[str, Any]]]:
    """Bucket the live TP/SL conditionals that protect one position lane.

    The order side is not used to select the rows: on the trigger book the
    stored size sign reflects the trigger leg, while the exchange executes a
    fired conditional as a reduce-only close against the position, so every
    conditional resting here protects the position.

    The lane is what separates them: a base-account position is not protected
    by a conditional that lives in an isolated account, and vice versa.

    The PROTECTED SIDE is what separates a take profit from a stop loss for
    a given position. A long is protected by a take profit above the mark
    and a stop loss below it; a short is the mirror. A conditional whose
    trigger direction does not match the position is therefore NOT a
    protection for it and is excluded - otherwise the agent could report a
    take profit for a short position that is really a stop loss.
    """
    buckets: dict[str, list[Dict[str, Any]]] = {"tp": [], "sl": []}
    for bucket, rows in _protection_rows(credentials, symbol).items():
        for row_item in rows:
            size = _decimal(row_item.get("size") if row_item.get("size") is not None else row_item.get("originalSize"))
            if size == 0:
                continue
            row_iso = row_item.get("iso")
            if isinstance(row_iso, bool) and row_iso != isolated:
                continue
            protected_side = _conditional_protected_side(row_item)
            if (
                position_side
                and protected_side is not None
                and protected_side != position_side
            ):
                # Protects the opposite side of the market - not ours.
                continue
            buckets[bucket].append(row_item)
    return buckets


def _bulk_conditional_action(
    *,
    kind: str,
    symbol: str,
    size: Decimal,
    threshold: Decimal,
    trigger_above: bool,
    isolated: bool,
) -> Dict[str, Any]:
    """Build a native Bulk conditional action (``tp`` / ``st``).

    ``d`` is the TRIGGER DIRECTION: ``true`` fires at/above the threshold,
    ``false`` fires at/below it. This is what the exchange reads it as, and
    it is confirmed both by Bulk's docs and by a live testnet check - a
    conditional only rests when its trigger sits on the side of the mark
    that has not been reached yet, and it fires the moment that side is
    crossed.

    The official bulk-keychain SDK names the same field ``is_buy``, which is
    misleading: Bulk gives each action type its own meaning for ``d`` (for
    ``rng`` it is the position direction), and for ``st``/``tp`` it selects
    the trigger direction. The exchange executes a fired conditional as a
    reduce-only close against the position, so the executed side is always
    protective regardless of the trigger direction.

    ``size`` is sent as an absolute quantity: the wire field is an unsigned
    fixed-point number, so the closing side is expressed by the exchange
    through the reduce-only routing, not by a sign on the size.

    MEASURED on Bulk testnet: when a conditional fires, the exchange
    submits its own reduce-only market order and chooses the closing side
    ITSELF - ``side: "sell"`` for a long position and ``side: "buy"`` for
    a short position, in all four long/short x TP/SL combinations,
    independent of ``d``. The trigger direction therefore selects WHEN
    the protection fires, and the exchange derives WHICH SIDE closes the
    position. That is why the two must stay separate: the agent supplies
    the trigger direction, and the closing side is the exchange's job.

    A protective conditional must trigger against the position, never with
    it: a long takes profit above and stops below, a short is the mirror.
    """
    row = {
        "c": symbol,
        "d": bool(trigger_above),
        "sz": float(abs(size)),
        "tr": float(threshold),
        "lim": None,
        "i": bool(isolated),
    }
    return {kind: row}


def _restore_protection(
    credentials: Mapping[str, str],
    kind: str,
    symbol: str,
    size: Decimal,
    threshold: Optional[Decimal],
    trigger_above: bool,
    isolated: bool,
) -> Optional[bool]:
    """Re-place a protection order that was cancelled by a failed update.

    Returns:
        ``True``  - the replacement was accepted.
        ``False`` - there was a previous protection to restore and the
                    restore FAILED (the position lost this protection).
        ``None``  - there was nothing to restore (no previous protection
                    existed), so nothing was lost and nothing was restored.

    The ``None`` case matters: reporting it as a failure made the caller
    claim the position was UNPROTECTED even when no protection had ever
    been cancelled.
    """
    if threshold is None or threshold <= 0:
        # No previous protection existed -> nothing was cancelled, so
        # nothing can be lost. Distinguish this from a failed restore.
        return None
    action = _bulk_conditional_action(
        kind=kind, symbol=symbol, size=size,
        threshold=threshold, trigger_above=trigger_above, isolated=isolated,
    )
    try:
        response = _submit_order(credentials, [action])
    except Exception:  # noqa: BLE001
        return False
    parsed = _position_action_status(response)
    return parsed["classification"] != "rejected"


def _reason_suffix(parsed: Mapping[str, Any]) -> str:
    """The exchange's own rejection reason, appended to a message.

    Bulk states the reason inside the status payload (for example
    ``rejectedRiskLimit`` with a ``reason`` string, or a transaction-level
    ``error`` with a ``message``). Reporting it verbatim is what makes a
    rejection diagnosable instead of a bare "Bulk rejected the order".
    """
    reasons = [str(r).strip() for r in (parsed or {}).get("reasons") or [] if str(r).strip()]
    if not reasons:
        return ""
    return " Bulk said: " + "; ".join(dict.fromkeys(reasons))


def _unprotected_note(restored: Optional[bool], had_previous: bool, label: str) -> str:
    """Whether the position lost its protection, stated honestly.

    ``restored`` is the three-state result of :func:`_restore_protection`:

    * ``None``  - there was no previous protection, so nothing was
                  cancelled and nothing was lost.
    * ``True``  - the previous protection was put back.
    * ``False`` - there WAS a previous protection, it was cancelled, and it
                  could NOT be put back: the position lost it.

    Only the last case may claim the position is unprotected, and it must
    name which protection was lost (Take Profit vs Stop Loss).
    """
    if restored is True:
        return ""
    if restored is None:
        # No previous protection existed -> nothing was lost.
        return ""
    if not had_previous:
        return ""
    return f" The previous {label} was cancelled and could NOT be restored, so the position is currently UNPROTECTED."


def _protection_min_notional_note(
    market: Mapping[str, Any], size: Decimal, threshold: Decimal, kind_label: str
) -> Optional[str]:
    """Whether a protective conditional would breach the minimum notional.

    Bulk publishes ``minNotional`` per market in ``exchangeInfo``. It is
    enforced on ordinary (non-reduce-only) orders - the production order
    history shows "notional 21.27 too small, < minimum 50" - but it is NOT
    enforced on conditional orders: a testnet take profit whose notional was
    32.00 against a 40.00 minimum rested without complaint, and every
    conditional carries ``reduceOnly: true``.

    Because the exemption is only observed (not documented), the check is
    advisory: it warns instead of blocking, so a valid protective order is
    never refused on the strength of an inferred rule.
    """
    raw_min = market.get("minNotional")
    if raw_min is None:
        return None
    try:
        minimum = _decimal(raw_min)
    except Exception:  # noqa: BLE001 - unparsable metadata is not fatal
        return None
    if minimum <= 0:
        return None
    notional = abs(size) * abs(threshold)
    if notional >= minimum:
        return None
    return (
        f"The {kind_label} notional {_plain_decimal(notional)} is below the market's "
        f"minimum notional {_plain_decimal(minimum)}. Bulk does not enforce "
        f"minNotional on reduce-only conditional orders, so this was submitted "
        f"anyway - but the exchange may reject it."
    )


def _bulk_protection_action(
    credentials: Mapping[str, str], account: str, operation: str, request: Mapping[str, Any]
) -> CanonicalResponse:
    """Create, replace or remove a native Bulk take-profit / stop-loss.

    Uses Bulk's own conditional orders (``tp`` / ``st``) on the exchange's
    trigger book. A price of ``0`` removes only the requested protection and
    leaves the other one untouched.
    """
    is_tp = operation == "set_tp"
    kind = "tp" if is_tp else "st"
    label = "Take Profit" if is_tp else "Stop Loss"
    other_label = "Stop Loss" if is_tp else "Take Profit"

    requested = str(request.get("symbol") or "").strip()
    if not requested:
        return make_failure(operation, name, account, "MISSING_SYMBOL", "Symbol is required for position action.")

    raw_price = request.get("price")
    price_text = str(raw_price if raw_price is not None else "").strip().replace(",", "")
    if price_text == "":
        return make_failure(
            operation, name, account,
            "INVALID_TP_PRICE" if is_tp else "INVALID_SL_PRICE",
            f"{label} price must be numeric and non-negative.",
        )
    try:
        requested_price = Decimal(price_text)
    except (InvalidOperation, ValueError, TypeError):
        return make_failure(
            operation, name, account,
            "INVALID_TP_PRICE" if is_tp else "INVALID_SL_PRICE",
            f"{label} price must be numeric and non-negative.",
        )
    if requested_price < 0:
        return make_failure(
            operation, name, account,
            "INVALID_TP_PRICE" if is_tp else "INVALID_SL_PRICE",
            f"{label} price must be numeric and non-negative.",
        )

    try:
        market = _find_market(credentials, requested)
    except Exception:  # noqa: BLE001
        return make_failure(operation, name, account, "UNKNOWN_SYMBOL", f"Bulk market {requested!r} was not found.")
    symbol = str(market.get("symbol") or "").strip()
    if not symbol:
        return make_failure(operation, name, account, "UNKNOWN_SYMBOL", "Bulk market row has no symbol.")

    try:
        before_account = _validate_close_snapshot(credentials, account, _live_account(credentials))
    except PermissionError as exc:
        return make_failure(operation, name, account, "ACCOUNT_IDENTITY_INVALID", sanitize_error_message(str(exc)))
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation, name, account, "ACCOUNT_SNAPSHOT_INVALID", sanitize_error_message(str(exc)))

    positions = _position_rows(before_account, symbol)
    if not positions:
        return make_failure(operation, name, account, "POSITION_NOT_FOUND", f"No open Bulk position for {symbol} in account {account!r}.")
    if len(positions) != 1:
        return make_failure(operation, name, account, "AMBIGUOUS_POSITION", f"Multiple open Bulk rows match {symbol}; refusing to choose a lane.")
    position = positions[0]
    size = _decimal(position.get("size"))
    if size == 0:
        return make_failure(operation, name, account, "POSITION_NOT_FOUND", f"No open Bulk position for {symbol} in account {account!r}.")
    if not isinstance(position.get("iso"), bool):
        return make_failure(operation, name, account, "POSITION_ROUTING_UNKNOWN", "Bulk position snapshot omitted its base/isolated routing flag.")
    isolated = position["iso"]
    if isolated and not str(position.get("isoPubkey") or "").strip():
        return make_failure(operation, name, account, "POSITION_ROUTING_UNKNOWN", "Bulk isolated position omitted isoPubkey; refusing to route a protection order.")

    position_side = "long" if size > 0 else "short"
    abs_size = abs(size)

    increment = _decimal(market.get("sizeIncrement", market.get("lotSize")), "0")
    precision_raw = market.get("sizeDecimals")
    if increment <= 0 or precision_raw is None:
        return make_failure(operation, name, account, "MARKET_SIZE_METADATA_MISSING", "Bulk market omitted valid size increment/precision metadata.")
    try:
        precision = int(precision_raw)
    except (ValueError, TypeError):
        return make_failure(operation, name, account, "MARKET_SIZE_METADATA_INVALID", "Bulk market sizeDecimals is invalid.")
    precision_step = Decimal(1).scaleb(-precision)
    if precision < 0 or abs_size % increment != 0 or abs_size % precision_step != 0:
        return make_failure(operation, name, account, "POSITION_SIZE_INVALID", "Live Bulk position size does not conform to market increment/precision.")

    tick_raw = market.get("tickSize")
    tick = _decimal(tick_raw, "0") if tick_raw is not None else Decimal("0")
    if tick <= 0:
        return make_failure(operation, name, account, "MARKET_TICK_METADATA_MISSING", "Bulk market omitted a valid tick size.")

    protection = _protection_rows_for_position(
        credentials, before_account, symbol, isolated, position_side
    )
    target_bucket = "tp" if is_tp else "sl"
    existing_rows = list(protection.get(target_bucket) or [])
    other_rows = list(protection.get("sl" if is_tp else "tp") or [])
    if len(existing_rows) > 1:
        return make_failure(operation, name, account, "AMBIGUOUS_PROTECTION_STATE", f"Multiple matching {label} orders were found.")

    current_side = position_side
    current_size_text = _plain_decimal(abs_size)

    def _action_result(**kwargs: Any) -> CanonicalPositionActionResult:
        return CanonicalPositionActionResult(
            operation=operation,
            symbol=_display_symbol(symbol),
            **kwargs,
        )

    # ---- Removal (price == 0) -------------------------------------------
    if requested_price == 0:
        if not existing_rows:
            return make_success(
                operation, name, account,
                position_action=_action_result(
                    verified=True, removed=False, current_side=current_side,
                    current_size=current_size_text,
                    message=(
                        f"No {label} was set for the {position_side} position, so "
                        f"nothing was removed and no other protection was touched."
                    ),
                ),
            )
        target = existing_rows[0]
        oid = str(target.get("orderId") or target.get("oid") or "").strip()
        if not oid:
            return make_failure(operation, name, account, "PROTECTION_ORDER_ID_MISSING", f"The live {label} order has no order id to cancel.")
        try:
            response = _submit_order(credentials, [{"cx": {"c": symbol, "oid": oid}}])
        except Exception as exc:  # noqa: BLE001 - outcome may be unknown
            return make_failure(
                operation, name, account, "PROTECTION_REMOVAL_UNCERTAIN",
                "Bulk did not confirm the cancellation. No automatic retry was made.",
                data={"retry_safe": False, "order_id": oid, "error_type": type(exc).__name__},
            )
        parsed = _position_action_status(response)
        if parsed["classification"] == "rejected":
            return make_failure(
                operation, name, account, "PROTECTION_REMOVAL_REJECTED",
                f"Bulk rejected the {label} cancellation." + _reason_suffix(parsed),
                data={"order_ids": parsed["order_ids"], "bulk_reasons": parsed["reasons"]},
            )

        try:
            after_account = _validate_close_snapshot(credentials, account, _live_account(credentials))
            after_protection = _protection_rows_for_position(
                credentials, after_account, symbol, isolated, position_side
            )
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation, name, account, "PROTECTION_VERIFICATION_UNAVAILABLE",
                f"Bulk returned a response but a valid, account-bound snapshot could not verify the {label} removal; no retry was made.",
                data={"retry_safe": False, "order_id": oid, "verification_error": sanitize_error_message(str(exc))},
            )
        after_rows = list(after_protection.get(target_bucket) or [])
        still_present = any(str(r.get("orderId") or r.get("oid") or "") == oid for r in after_rows)
        other_preserved = len(list(after_protection.get("sl" if is_tp else "tp") or [])) == len(other_rows)
        if still_present:
            return make_failure(
                operation, name, account, "PROTECTION_REMOVAL_NOT_VERIFIED",
                f"The {label} order is still present after the cancellation.",
                data={"retry_safe": False, "order_id": oid},
            )
        if not other_preserved:
            return make_failure(
                operation, name, account, "PROTECTION_STATE_CHANGED",
                f"The {other_label} was affected by the {label} removal.",
                data={"retry_safe": False, "order_id": oid},
            )
        return make_success(
            operation, name, account,
            position_action=_action_result(
                verified=True, removed=True, current_side=current_side,
                current_size=current_size_text, exchange_order_id=oid,
                message=(
                    f"{label} removed. The {other_label} was left untouched, so the "
                    f"{position_side} position is still protected by it."
                ),
            ),
        )

    # ---- Create / replace -----------------------------------------------
    quantized_price = (requested_price / tick).to_integral_value(rounding=ROUND_DOWN) * tick
    if quantized_price <= 0:
        return make_failure(operation, name, account, "TP_PRICE_BELOW_TICK" if is_tp else "SL_PRICE_BELOW_TICK", f"{label} price is below the market tick size.")

    # A protective conditional must trigger against the position, never with
    # it: a long takes profit above / stops below, a short is the mirror.
    #
    # The trigger direction (``d`` on the wire) and the closing side are two
    # different questions and are computed separately. Conflating them is
    # what produced inverted take-profits.
    trigger_above = _protection_trigger_direction(kind, position_side)
    closing_side = _protection_closing_side(position_side)

    # A conditional whose trigger is already satisfied fires the moment it is
    # submitted and is then cancelled as IOC, so it would never protect the
    # position. Refuse rather than place an order that cannot work.
    #
    # The trigger is compared against the MARK price (Bulk conditions on the
    # fair/mark price, not the last trade), so the mark is what matters here -
    # not the position's entry price.
    reference_price = _decimal(position.get("fairPrice") or 0)
    if reference_price <= 0:
        reference_price = _decimal(position.get("price") or 0)
    if reference_price > 0:
        if trigger_above and quantized_price <= reference_price:
            return make_failure(
                operation, name, account,
                "TP_PRICE_NOT_ABOVE_MARK" if is_tp else "SL_PRICE_NOT_BELOW_MARK",
                f"A {label} must be placed beyond the current mark price "
                f"({_plain_decimal(reference_price)}); Bulk fires a satisfied trigger immediately.",
            )
        if not trigger_above and quantized_price >= reference_price:
            return make_failure(
                operation, name, account,
                "TP_PRICE_NOT_ABOVE_MARK" if is_tp else "SL_PRICE_NOT_BELOW_MARK",
                f"A {label} must be placed beyond the current mark price "
                f"({_plain_decimal(reference_price)}); Bulk fires a satisfied trigger immediately.",
            )

    actions: list[Mapping[str, Any]] = []
    # The previous protection, captured before it is cancelled, so a failed
    # replacement can restore it instead of leaving the position unprotected.
    replaced_oid = ""
    replaced_price: Optional[Decimal] = None
    if existing_rows:
        # Replace: cancel the previous conditional first so the update never
        # leaves a duplicate protection behind.
        target = existing_rows[0]
        old_oid = str(target.get("orderId") or target.get("oid") or "").strip()
        if not old_oid:
            return make_failure(operation, name, account, "PROTECTION_ORDER_ID_MISSING", f"The live {label} order has no order id to cancel.")
        replaced_oid = old_oid
        replaced_price = _conditional_trigger_price(target)
        try:
            cancel_response = _submit_order(credentials, [{"cx": {"c": symbol, "oid": old_oid}}])
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation, name, account, "PROTECTION_REPLACEMENT_UNCERTAIN",
                "Bulk did not confirm the previous cancellation. No automatic retry was made.",
                data={"retry_safe": False, "order_id": old_oid, "error_type": type(exc).__name__},
            )
        cancel_parsed = _position_action_status(cancel_response)
        if cancel_parsed["classification"] == "rejected":
            return make_failure(
                operation, name, account, "PROTECTION_REPLACEMENT_REJECTED",
                f"Bulk rejected the replacement of the existing {label}."
                + _reason_suffix(cancel_parsed),
                data={"order_ids": cancel_parsed["order_ids"], "bulk_reasons": cancel_parsed["reasons"]},
            )

    action = _bulk_conditional_action(
        kind=kind, symbol=symbol, size=abs_size,
        threshold=quantized_price, trigger_above=trigger_above, isolated=isolated,
    )
    actions.append(action)
    min_notional_note = _protection_min_notional_note(
        market, abs_size, quantized_price, label
    )
    try:
        response = _submit_order(credentials, actions)
    except Exception as exc:  # noqa: BLE001 - outcome may be unknown
        restored = _restore_protection(
            credentials, kind, symbol, abs_size, replaced_price, trigger_above, isolated
        )
        unprotected = bool(replaced_oid) and restored is False
        return make_failure(
            operation, name, account, "PROTECTION_SUBMISSION_UNCERTAIN",
            f"Bulk did not confirm the {label} submission. No automatic retry was made."
            + _unprotected_note(restored, bool(replaced_oid), label),
            data={
                "retry_safe": False,
                "requested_price": _plain_decimal(quantized_price),
                "error_type": type(exc).__name__,
                "previous_protection_restored": restored,
                "position_unprotected": unprotected,
            },
        )
    parsed = _position_action_status(response)
    if parsed["classification"] == "rejected":
        restored = _restore_protection(
            credentials, kind, symbol, abs_size, replaced_price, trigger_above, isolated
        )
        unprotected = bool(replaced_oid) and restored is False
        return make_failure(
            operation, name, account, "PROTECTION_REJECTED",
            f"Bulk rejected the {label} order."
            + _reason_suffix(parsed)
            + _unprotected_note(restored, bool(replaced_oid), label),
            data={
                "order_ids": parsed["order_ids"],
                "requested_price": _plain_decimal(quantized_price),
                "previous_protection_restored": restored,
                "position_unprotected": unprotected,
                "bulk_reasons": parsed["reasons"],
                "native_statuses": parsed["statuses"],
            },
        )

    try:
        after_account = _validate_close_snapshot(credentials, account, _live_account(credentials))
        after_protection = _protection_rows_for_position(
            credentials, after_account, symbol, isolated, position_side
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation, name, account, "PROTECTION_VERIFICATION_UNAVAILABLE",
            f"Bulk returned a response but a valid, account-bound snapshot could not verify the {label}; no retry was made.",
            data={"retry_safe": False, "order_ids": parsed["order_ids"], "verification_error": sanitize_error_message(str(exc))},
        )
    after_rows = list(after_protection.get(target_bucket) or [])
    if len(after_rows) != 1:
        unprotected = len(after_rows) == 0 and bool(replaced_oid)
        return make_failure(
            operation, name, account, "PROTECTION_NOT_VERIFIED",
            f"Expected exactly one {label} order after the update but found {len(after_rows)}."
            + (" The position is currently UNPROTECTED." if unprotected else ""),
            data={
                "retry_safe": False,
                "order_ids": parsed["order_ids"],
                "position_unprotected": unprotected,
                "found_count": len(after_rows),
            },
        )
    placed = after_rows[0]
    placed_price = _conditional_trigger_price(placed)
    placed_above = _conditional_is_above(placed)
    placed_oid = str(placed.get("orderId") or placed.get("oid") or "")
    price_matches = placed_price is not None and placed_price == quantized_price
    direction_matches = placed_above is None or placed_above == trigger_above
    if not price_matches or not direction_matches:
        return make_failure(
            operation, name, account, "PROTECTION_NOT_VERIFIED",
            f"The live {label} order does not match the requested price and direction.",
            data={
                "retry_safe": False,
                "order_id": placed_oid,
                "expected_price": _plain_decimal(quantized_price),
                "live_price": _plain_decimal(placed_price) if placed_price is not None else None,
                "expected_trigger_above": trigger_above,
                "live_trigger_above": placed_above,
            },
        )
    other_after = list(after_protection.get("sl" if is_tp else "tp") or [])
    if len(other_after) != len(other_rows):
        return make_failure(
            operation, name, account, "PROTECTION_STATE_CHANGED",
            f"The {other_label} was affected by the {label} update.",
            data={"retry_safe": False, "order_id": placed_oid},
        )
    return make_success(
        operation, name, account,
        position_action=_action_result(
            verified=True, price=_plain_decimal(quantized_price), removed=False,
            current_side=current_side, current_size=current_size_text,
            exchange_order_id=placed_oid or None,
            message=(
                f"{label} updated. It triggers {'at or above' if trigger_above else 'at or below'} "
                f"{_plain_decimal(quantized_price)} and closes the {position_side} position "
                f"with a {closing_side} order (reduce-only)."
                + (f" {min_notional_note}" if min_notional_note else "")
            ),
        ),
    )


def _bulk_position_action(credentials: Mapping[str, str], account: str, operation: str, request: Mapping[str, Any]) -> CanonicalResponse:
    """Close a Bulk position natively; TP/SL are handled by
    :func:`_bulk_protection_action`."""
    if operation != "close_position":
        return make_failure(operation, name, account, "NOT_IMPLEMENTED", f"Bulk {operation} is not implemented.")
    requested = str(request.get("symbol") or "").strip()
    if not requested:
        return make_failure(operation, name, account, "MISSING_SYMBOL", "Symbol is required for position action.")
    try:
        market = _find_market(credentials, requested)
    except Exception:
        return make_failure(operation, name, account, "UNKNOWN_SYMBOL", f"Bulk market {requested!r} was not found.")
    symbol = str(market.get("symbol") or "").strip()
    if not symbol:
        return make_failure(operation, name, account, "UNKNOWN_SYMBOL", "Bulk market row has no symbol.")

    try:
        before_account = _validate_close_snapshot(credentials, account, _live_account(credentials))
    except PermissionError as exc:
        return make_failure(operation, name, account, "ACCOUNT_IDENTITY_INVALID", sanitize_error_message(str(exc)))
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation, name, account, "ACCOUNT_SNAPSHOT_INVALID", sanitize_error_message(str(exc)))
    positions = _position_rows(before_account, symbol)
    if not positions:
        return make_failure(operation, name, account, "POSITION_NOT_FOUND", f"No open Bulk position for {symbol} in account {account!r}.")
    if len(positions) != 1:
        return make_failure(operation, name, account, "AMBIGUOUS_POSITION", f"Multiple open Bulk rows match {symbol}; refusing to choose a lane.")
    position = positions[0]
    size = _decimal(position.get("size"))
    if size == 0:
        return make_failure(operation, name, account, "POSITION_NOT_FOUND", f"No open Bulk position for {symbol} in account {account!r}.")
    if not isinstance(position.get("iso"), bool):
        return make_failure(operation, name, account, "POSITION_ROUTING_UNKNOWN", "Bulk position snapshot omitted its base/isolated routing flag.")
    isolated = position["iso"]
    if isolated and not str(position.get("isoPubkey") or "").strip():
        return make_failure(operation, name, account, "POSITION_ROUTING_UNKNOWN", "Bulk isolated position omitted isoPubkey; refusing to route a close.")

    increment = _decimal(market.get("sizeIncrement", market.get("lotSize")), "0")
    precision_raw = market.get("sizeDecimals")
    if increment <= 0 or precision_raw is None:
        return make_failure(operation, name, account, "MARKET_SIZE_METADATA_MISSING", "Bulk market omitted valid size increment/precision metadata.")
    try:
        precision = int(precision_raw)
    except (ValueError, TypeError):
        return make_failure(operation, name, account, "MARKET_SIZE_METADATA_INVALID", "Bulk market sizeDecimals is invalid.")
    abs_size = abs(size)
    precision_step = Decimal(1).scaleb(-precision)
    if precision < 0 or abs_size % increment != 0 or abs_size % precision_step != 0:
        return make_failure(operation, name, account, "POSITION_SIZE_INVALID", "Live Bulk position size does not conform to market increment/precision.")

    close_side = "buy" if size < 0 else "sell"
    guard_key = (str(credentials.get("account_pubkey") or ""), symbol.upper(), isolated)
    with _close_guard_lock:
        if guard_key in _close_guard:
            return make_failure(operation, name, account, "CLOSE_OUTCOME_UNKNOWN", "A prior Bulk close for this account/market/lane is unresolved; reconcile state before another submission.", data={"close_status": "unknown", "retry_safe": False})
        pending = [o for o in _orders_for(before_account, symbol, close_side)
                   if bool(o.get("reduceOnly")) and bool(o.get("iso", False)) == isolated]
        if pending:
            return make_failure(operation, name, account, "CLOSE_IN_PROGRESS", "A matching Bulk reduce-only order is already open; refusing a duplicate close.", data={"close_status": "pending", "retry_safe": False})
        _close_guard.add(guard_key)

    # Bulk requires r=true for reduce-only and i routes base vs isolated margin.
    action = {"m": {"c": symbol, "b": close_side == "buy", "sz": float(abs_size), "r": True, "i": isolated}}
    try:
        response = _submit_order(credentials, [action])
    except Exception as exc:  # noqa: BLE001 - submission may have reached Bulk
        try:
            after = _validate_close_snapshot(credentials, account, _live_account(credentials))
            remaining = _lane_position_size(_position_rows(after, symbol), isolated)
        except Exception:
            remaining = size
        signed_remaining = _plain_decimal(remaining)
        if remaining * size > 0 and abs(remaining) < abs(size):
            return make_failure(operation, name, account, "CLOSE_PARTIALLY_FILLED", "Bulk submission outcome is uncertain; the live position was reduced. No automatic retry was made.", data={"close_status": "partially_filled", "verified": True, "requested_size": _plain_decimal(abs_size), "remaining_size": _plain_decimal(abs(remaining)), "remaining_signed_size": signed_remaining, "retry_safe": False, "error_type": type(exc).__name__})
        return make_failure(operation, name, account, "CLOSE_OUTCOME_UNKNOWN", "Bulk submission outcome is uncertain. No automatic retry was made.", data={"close_status": "unknown", "verified": False, "requested_size": _plain_decimal(abs_size), "remaining_signed_size": signed_remaining, "remaining_size": _plain_decimal(abs(remaining)), "retry_safe": False, "error_type": type(exc).__name__})

    parsed = _position_action_status(response)
    classification = parsed["classification"]
    try:
        after_account = _validate_close_snapshot(credentials, account, _live_account(credentials))
        after_rows = _position_rows(after_account, symbol)
        after_size = _lane_position_size(after_rows, isolated)
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation, name, account, "CLOSE_VERIFICATION_UNAVAILABLE", "Bulk returned a response but a valid, account-bound positions snapshot could not verify the result; no retry was made.", data={"close_status": "unknown", "verified": False, "retry_safe": False, "order_ids": parsed["order_ids"], "bulk_status": classification, "verification_error": sanitize_error_message(str(exc))})

    common = {"verified": True, "requested_size": _plain_decimal(abs_size), "remaining_size": _plain_decimal(abs(after_size)), "remaining_signed_size": _plain_decimal(after_size), "order_ids": parsed["order_ids"], "bulk_status": classification, "native_statuses": parsed["statuses"], "retry_safe": False}
    if classification == "rejected":
        with _close_guard_lock:
            _close_guard.discard(guard_key)
        return make_failure(operation, name, account, "CLOSE_REJECTED", "Bulk rejected the reduce-only close order.", data={**common, "close_status": "rejected"})
    reduced = after_size * size > 0 and abs(after_size) < abs(size)
    if reduced:
        return make_failure(operation, name, account, "CLOSE_PARTIALLY_FILLED", "Bulk close reduced but did not flatten the position; no automatic retry was made.", data={**common, "close_status": "partially_filled"})
    if classification == "filled" and after_size == 0:
        with _close_guard_lock:
            _close_guard.discard(guard_key)
        return make_success(operation, name, account, data={**common, "close_status": "fully_closed"})
    if classification == "cancelled":
        with _close_guard_lock:
            _close_guard.discard(guard_key)
        return make_failure(operation, name, account, "CLOSE_CANCELLED", "Bulk cancelled the close order; the position is not confirmed closed.", data={**common, "close_status": "cancelled"})
    after_close_orders = [o for o in _orders_for(after_account, symbol, close_side)
                          if bool(o.get("reduceOnly")) and bool(o.get("iso", False)) == isolated]
    if (classification == "pending" or (classification == "unknown" and _payload_ok(response))) and after_close_orders:
        return make_failure(operation, name, account, "CLOSE_ACCEPTED", "Bulk accepted a pending reduce-only close order; the position is not confirmed closed.", data={**common, "close_status": "pending", "open_order_ids": [str(o.get("orderId") or o.get("oid") or "") for o in after_close_orders]})
    return make_failure(operation, name, account, "CLOSE_OUTCOME_UNKNOWN", "Bulk response and verified position state do not establish a completed close; no automatic retry was made.", data={**common, "close_status": "unknown"})


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
        if operation in ("set_tp", "set_sl"):
            return _bulk_protection_action(credentials, account, operation, request)
        if operation in ("close_position",):
            return _bulk_position_action(credentials, account, operation, request)
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
