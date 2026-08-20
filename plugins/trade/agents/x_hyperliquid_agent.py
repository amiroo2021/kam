"""Hyperliquid exchange agent.

This module owns EVERYTHING Hyperliquid-specific:

- Credential discovery (parses ``HYPERLIQUID_<ALIAS>_WALLET`` /
  ``HYPERLIQUID_<ALIAS>_SECRET`` environment variables, including from
  ``~/.hermes/.env``).
- Authentication material handling (wallet address used as the public
  identifier for read-only ``/info`` calls; private key is never
  transmitted to the API for balance queries).
- API interaction (POST to ``https://api.hyperliquid.xyz/info`` using
  stdlib only — no external SDK).
- Hyperliquid response interpretation (parses ``spotClearinghouseState``
  payload, finds the USDC row, extracts the held total).
- Conversion from Hyperliquid-native to canonical contract.
- Error sanitization (raw exceptions are scrubbed before being put in
  the canonical error envelope).

Phase 1 is strictly READ-ONLY. No order placement, no signing, no
write operations.

TradeDesk and the Telegram wizard MUST NOT parse any
``HYPERLIQUID_*`` environment variable. That is exclusively this
module's responsibility.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.utils.signing import CancelRequest

from ..canonical import (
    CanonicalBalance,
    CanonicalCancelGroupResult,
    CanonicalInstrument,
    CanonicalLadderResult,
    CanonicalOrderGroup,
    CanonicalOrderResult,
    CanonicalPosition,
    CanonicalPositionActionResult,
    CanonicalResponse,
    make_failure,
    make_success,
    normalize_balance,
    sanitize_error_message,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module identity — required by TradeDesk.
# ---------------------------------------------------------------------------

name = "hyperliquid"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_API_BASE = "https://api.hyperliquid.xyz"
API_TIMEOUT_SECONDS = 20
MAX_RETRIES = 2
CANCEL_BATCH_SIZE = 200

# Credentials are matched as: HYPERLIQUID_<ALIAS>_WALLET and
# HYPERLIQUID_<ALIAS>_SECRET. Aliases are uppercase identifiers
# (allowing ASCII letters, digits, underscores). This is intentionally
# permissive — the user may have many accounts.
_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
# Process-lifetime cache of perp DEX names (native "" + HIP-3). Populated
# only by network-capable callers; read (without network) by
# ``_build_exchange_client`` so order/close construction stays hermetic and
# unit tests never require live connectivity.
_perp_dex_names_cache: Optional[List[str]] = None

# Preflight heuristic: a valid Hyperliquid wallet address is a 0x-prefixed
# 40-char hex string. We don't strictly validate the checksum — the API
# will reject mismatches. We do require the 0x prefix and 40 hex chars.
_WALLET_HEX_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")


# ---------------------------------------------------------------------------
# Credential discovery — only legitimate place that reads HYPERLIQUID_* vars.
# ---------------------------------------------------------------------------

def _hermes_home() -> Path:
    """Return the Hermes home directory (~/.hermes by default)."""
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _load_dotenv_values(path: Path) -> Dict[str, str]:
    """Minimal .env parser — only used for Hyperliquid credential discovery.

    Honors the same convention as the rest of Hermes: KEY=VALUE pairs,
    with optional quoting. Lines starting with ``#`` are comments.
    Missing files yield an empty dict.
    """
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


def _read_env(name: str) -> str:
    """Read an environment variable, falling back to ~/.hermes/.env.

    Returns the trimmed value, or empty string if absent.
    """
    live = os.environ.get(name, "").strip()
    if live:
        return live
    dotenv = _load_dotenv_values(_hermes_home() / ".env").get(name, "").strip()
    return dotenv


def _discover_accounts() -> List[str]:
    """Return the list of configured Hyperliquid account aliases.

    A credential is considered "configured" when both
    ``HYPERLIQUID_<ALIAS>_WALLET`` and ``HYPERLIQUID_<ALIAS>_SECRET``
    are non-empty and the wallet looks like a 0x-prefixed 40-char hex
    address. Returns aliases in uppercase, sorted.

    NOTE: This function never returns the wallet address or secret
    value. Only the alias.
    """
    # Build a combined view of all HYPERLIQUID_* env vars from both
    # the live environment and the dotenv file.
    env_values: Dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith("HYPERLIQUID_"):
            env_values[key] = (value or "").strip()
    for key, value in _load_dotenv_values(_hermes_home() / ".env").items():
        if key.startswith("HYPERLIQUID_"):
            env_values.setdefault(key, (value or "").strip())

    wallets: Dict[str, str] = {}
    secrets: Dict[str, str] = {}
    for key, value in env_values.items():
        # Format: HYPERLIQUID_<ALIAS>_<FIELD>
        if not (key.startswith("HYPERLIQUID_") and key.count("_") >= 2):
            continue
        # Strip prefix and split off the last underscore-suffix.
        remainder = key[len("HYPERLIQUID_"):]
        # The last underscore-suffix is the field name (WALLET or SECRET).
        # The alias is everything before that.
        if remainder.endswith("_WALLET"):
            alias = remainder[: -len("_WALLET")]
            field = "WALLET"
        elif remainder.endswith("_SECRET"):
            alias = remainder[: -len("_SECRET")]
            field = "SECRET"
        else:
            continue
        if not alias or not _ALIAS_PATTERN.match(alias):
            continue
        if not value:
            continue
        if field == "WALLET":
            wallets[alias] = value
        else:
            secrets[alias] = value

    # Keep only aliases that have BOTH a wallet and a secret, and where
    # the wallet format looks plausible.
    valid: List[str] = []
    for alias in sorted(wallets.keys() & secrets.keys()):
        if _WALLET_HEX_PATTERN.match(wallets[alias]):
            valid.append(alias)
    return valid


def _lookup_credentials(alias: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (wallet, secret) for a given alias.

    Returns (None, None) if either is missing or malformed. The caller
    MUST NOT log the secret value.
    """
    alias_upper = (alias or "").strip().upper()
    if not _ALIAS_PATTERN.match(alias_upper):
        return (None, None)

    wallet = _read_env(f"HYPERLIQUID_{alias_upper}_WALLET")
    secret = _read_env(f"HYPERLIQUID_{alias_upper}_SECRET")
    if not wallet or not secret:
        return (None, None)
    if not _WALLET_HEX_PATTERN.match(wallet):
        return (None, None)
    return (wallet, secret)


# ---------------------------------------------------------------------------
# Public agent contract
# ---------------------------------------------------------------------------

def list_accounts() -> List[str]:
    """Return the configured Hyperliquid account aliases (uppercase)."""
    return _discover_accounts()


def capabilities() -> List[str]:
    """Return the operations this agent supports."""
    return ["balance", "positions_orders", "positions_management", "resolve_instrument", "new_order", "cancel_order_group", "ladder"]


def execute(request: Dict[str, Any]) -> CanonicalResponse:
    """Dispatch a canonical request to the Hyperliquid agent.

    Phase 1 supports only ``operation == "balance"``. Any other
    operation returns a canonical ``NOT_IMPLEMENTED`` error.
    """
    if not isinstance(request, dict):
        operation = ""
    else:
        operation = str(request.get("operation") or "").strip()
    logger.info("Hyperliquid agent execute: operation=%s", operation)
    if not isinstance(request, dict):
        return make_failure(
            operation="",
            exchange=name,
            account="",
            code="INVALID_REQUEST",
            message="Request must be a dict.",
        )

    operation = (request.get("operation") or "").strip()
    account = (request.get("account") or "").strip()

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

    if operation == "balance":
        return _execute_balance(account)
    if operation == "positions_orders":
        return _execute_positions_orders(account, request)
    if operation == "positions_management":
        return _execute_positions_management(account, request)
    if operation == "set_tp":
        return _execute_set_tp(account, request)
    if operation == "set_sl":
        return _execute_set_sl(account, request)
    if operation == "close_position":
        return _execute_close_position(account, request)
    if operation == "resolve_instrument":
        return _execute_resolve_instrument(account, request)
    if operation == "new_order":
        return _execute_new_order(account, request)
    if operation == "cancel_order_group":
        return _execute_cancel_order_group(account, request)
    if operation == "ladder":
        return _execute_ladder(account, request)
    return make_failure(
        operation=operation,
        exchange=name,
        account=account,
        code="NOT_IMPLEMENTED",
        message="Not implemented yet.",
    )


# ---------------------------------------------------------------------------
# Balance implementation
# ---------------------------------------------------------------------------


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def _decimal_text(value: Any) -> str:
    decimal_value = _decimal_or_none(value)
    if decimal_value is None:
        return "0"
    rendered = format(decimal_value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".") or "0"
    return rendered


def _positive_decimal_text(value: Any) -> str:
    decimal_value = _decimal_or_none(value)
    if decimal_value is None:
        return "0"
    return _decimal_text(abs(decimal_value))


def _decimal_places_from_text(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        decimal_value = Decimal(text)
    except Exception:  # noqa: BLE001
        return None
    exponent = decimal_value.as_tuple().exponent
    if not isinstance(exponent, int):
        return None
    return max(0, -exponent)


def _format_decimal_places(value: Decimal, places: int) -> str:
    quantum = Decimal("1").scaleb(-places)
    quantized = value.quantize(quantum, rounding=ROUND_HALF_UP)
    return format(quantized, "f")


def _hyperliquid_max_price_decimals(sz_decimals: Any, is_spot: bool = False) -> int:
    try:
        precision = int(sz_decimals)
    except Exception:  # noqa: BLE001
        return 0
    if precision < 0:
        return 0
    max_decimals = 8 if is_spot else 6
    return max(0, max_decimals - precision)


def _decimal_significant_figures(value: Decimal) -> int:
    if value == 0:
        return 1
    normalized = value.normalize()
    digits = normalized.as_tuple().digits
    return len(digits) if digits else 1


def _hyperliquid_price_is_valid(price: Decimal, sz_decimals: Any, is_spot: bool = False) -> bool:
    if price is None or price <= 0:
        return False
    if price == price.to_integral_value():
        return True
    max_decimals = _hyperliquid_max_price_decimals(sz_decimals, is_spot=is_spot)
    normalized = price.normalize()
    decimal_places = max(0, -normalized.as_tuple().exponent)
    if decimal_places > max_decimals:
        return False
    return _decimal_significant_figures(price) <= 5


def _normalize_hyperliquid_order_price(price: Decimal, sz_decimals: Any, is_spot: bool = False) -> Decimal:
    decimal_price = _decimal_or_none(price)
    if decimal_price is None or decimal_price <= 0:
        raise ValueError("INVALID_PRICE")

    max_decimals = _hyperliquid_max_price_decimals(sz_decimals, is_spot=is_spot)
    if decimal_price == decimal_price.to_integral_value():
        return decimal_price.to_integral_value()

    decimal_quantum = Decimal("1").scaleb(-max_decimals)
    price_candidate = decimal_price.quantize(decimal_quantum, rounding=ROUND_HALF_UP)
    if _hyperliquid_price_is_valid(price_candidate, sz_decimals, is_spot=is_spot):
        return price_candidate

    sig_figs = 5
    adjusted = int(price_candidate.adjusted())
    sig_quantum = Decimal("1").scaleb(adjusted - sig_figs + 1)
    price_candidate = price_candidate.quantize(sig_quantum, rounding=ROUND_HALF_UP)
    if price_candidate == price_candidate.to_integral_value():
        return price_candidate.to_integral_value()

    price_candidate = price_candidate.quantize(decimal_quantum, rounding=ROUND_HALF_UP)
    if _hyperliquid_price_is_valid(price_candidate, sz_decimals, is_spot=is_spot):
        return price_candidate

    price_candidate = price_candidate.to_integral_value(rounding=ROUND_HALF_UP)
    if price_candidate <= 0:
        raise ValueError("INVALID_PRICE")
    return price_candidate


def _market_price_decimals_by_symbol(payload: Any) -> Dict[str, int]:
    if not isinstance(payload, list) or len(payload) < 2:
        return {}
    meta, asset_ctxs = payload[0], payload[1]
    if not isinstance(meta, dict) or not isinstance(asset_ctxs, list):
        return {}

    universe = meta.get("universe")
    if not isinstance(universe, list):
        return {}

    result: Dict[str, int] = {}
    for index, instrument in enumerate(universe):
        if not isinstance(instrument, dict):
            continue
        symbol = str(instrument.get("name") or "").strip()
        if not symbol or index >= len(asset_ctxs):
            continue
        ctx = asset_ctxs[index]
        if not isinstance(ctx, dict):
            continue
        precision = _decimal_places_from_text(ctx.get("markPx"))
        if precision is None:
            precision = _decimal_places_from_text(ctx.get("midPx"))
        if precision is None:
            precision = _decimal_places_from_text(ctx.get("prevDayPx"))
        if precision is None:
            precision = _decimal_places_from_text(ctx.get("oraclePx"))
        if precision is not None:
            result[symbol] = precision
    return result


def _symbol_key(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).upper()
    if text.endswith("USDC") and len(text) > 4:
        text = text[:-4]
    return text


def _price_increment_from_precision(precision: Optional[int]) -> Optional[str]:
    if precision is None:
        return None
    return _decimal_text(Decimal("1").scaleb(-precision))


def _size_increment_from_sz_decimals(sz_decimals: Any) -> Optional[str]:
    try:
        precision = int(sz_decimals)
    except Exception:  # noqa: BLE001
        return None
    if precision < 0:
        return None
    return _decimal_text(Decimal("1").scaleb(-precision))


def _candidate_sz_decimals(candidate: Dict[str, Any]) -> Any:
    if "sz_decimals" in candidate and candidate["sz_decimals"] is not None:
        return candidate["sz_decimals"]
    size_increment = _decimal_from_request(candidate.get("size_increment"))
    if size_increment is None or size_increment <= 0:
        return None
    exponent = size_increment.normalize().as_tuple().exponent
    if not isinstance(exponent, int):
        return None
    return max(0, -exponent)


def _resolve_exact_symbol_alias(symbol: str) -> str:
    aliases = {
        "SPX": "SP500",
        "SP500USDC": "SP500",
        "S&P500": "SP500",
        "S&P 500": "SP500",
        "SNP500": "SP500",
        "GOLDUSDC": "GOLD",
        "SILVERUSDC": "SILVER",
        "XYZ100USDC": "XYZ100",
        "OILUSDC": "OIL",
    }
    return aliases.get(symbol, symbol)


def _fetch_perp_dex_names() -> List[str]:
    """Return the native dex ("") plus every HIP-3 perp DEX name.

    Memoized because perp DEX membership is static within a process lifetime
    and this is called from discovery and from Exchange construction; caching
    avoids repeated network round-trips. The cache is only populated by
    callers that may hit the network (discovery/resolve); ``_build_exchange_client``
    reads it without forcing a network fetch so unit tests stay hermetic.
    """
    global _perp_dex_names_cache
    if _perp_dex_names_cache is not None:
        return list(_perp_dex_names_cache)
    raw = _post_info({"type": "perpDexs"})
    names: List[str] = [""]
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            dex_name = str(item.get("name") or "").strip()
            if dex_name and dex_name not in names:
                names.append(dex_name)
    _perp_dex_names_cache = names[:]
    return list(_perp_dex_names_cache)


def _cached_perp_dex_names() -> List[str]:
    """Return the perp DEX list only if already fetched (no network)."""
    if _perp_dex_names_cache is None:
        return [""]
    return list(_perp_dex_names_cache)


def _fetch_perp_market_candidates() -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for dex_index, dex in enumerate(_fetch_perp_dex_names()):
        payload: Dict[str, Any] = {"type": "metaAndAssetCtxs"}
        if dex:
            payload["dex"] = dex
        raw = _post_info(payload)
        if not isinstance(raw, list) or len(raw) < 2:
            continue
        meta, asset_ctxs = raw[0], raw[1]
        if not isinstance(meta, dict) or not isinstance(asset_ctxs, list):
            continue
        universe = meta.get("universe")
        if not isinstance(universe, list):
            continue
        for index, instrument in enumerate(universe):
            if not isinstance(instrument, dict):
                continue
            internal_name = str(instrument.get("name") or "").strip()
            if not internal_name:
                continue
            public_symbol = internal_name.split(":", 1)[1] if ":" in internal_name else internal_name
            ctx = asset_ctxs[index] if index < len(asset_ctxs) and isinstance(asset_ctxs[index], dict) else {}
            precision = _decimal_places_from_text(ctx.get("markPx"))
            if precision is None:
                precision = _decimal_places_from_text(ctx.get("midPx"))
            if precision is None:
                precision = _decimal_places_from_text(ctx.get("prevDayPx"))
            if precision is None:
                precision = _decimal_places_from_text(ctx.get("oraclePx"))
            candidates.append(
                {
                    "dex": dex,
                    "dex_index": dex_index,
                    "internal_name": internal_name,
                    # route_symbol is the FULL wire/execution identifier incl. dex
                    # prefix (e.g. ``xyz:SP500``) — must be passed to the SDK for
                    # market_close/order/TP-SL/cancel so the dex is resolved.
                    "route_symbol": internal_name,
                    # public_symbol is the dex-stripped display alias (e.g. ``SP500``)
                    # used only for user-facing display and fuzzy matching.
                    "public_symbol": public_symbol,
                    "public_key": _symbol_key(public_symbol),
                    "internal_key": _symbol_key(internal_name),
                    "display_name": f"{public_symbol}-USDC",
                    "price_increment": _price_increment_from_precision(precision),
                    "size_increment": _size_increment_from_sz_decimals(instrument.get("szDecimals")),
                    "sz_decimals": instrument.get("szDecimals"),
                }
            )
    return candidates


def _resolve_instrument_candidate(requested_symbol: str, candidates: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], str]:
    requested = (requested_symbol or "").strip()
    if not requested:
        return None, "INSTRUMENT_NOT_FOUND"
    requested_key = _symbol_key(requested)
    alias_key = _symbol_key(_resolve_exact_symbol_alias(requested_key))
    if not requested_key:
        return None, "INSTRUMENT_NOT_FOUND"

    ranked: List[Tuple[Tuple[int, int, int, str, str], Dict[str, Any]]] = []
    for candidate in candidates:
        public_key = str(candidate.get("public_key") or "")
        internal_key = str(candidate.get("internal_key") or "")
        if not public_key and not internal_key:
            continue

        if requested_key == public_key:
            match_kind = 0
        elif requested_key == internal_key:
            match_kind = 1
        elif alias_key == public_key:
            match_kind = 2
        elif alias_key == internal_key:
            match_kind = 3
        elif requested_key in public_key or public_key in requested_key:
            match_kind = 4
        elif requested_key in internal_key or internal_key in requested_key:
            match_kind = 5
        else:
            continue

        rank = (
            match_kind,
            int(candidate.get("dex_index") or 0),
            len(public_key) if public_key else len(internal_key),
            str(candidate.get("public_symbol") or ""),
            str(candidate.get("internal_name") or ""),
        )
        ranked.append((rank, candidate))

    if not ranked:
        return None, "INSTRUMENT_NOT_FOUND"

    ranked.sort(key=lambda item: item[0])
    best_rank = ranked[0][0]
    best = [candidate for rank, candidate in ranked if rank == best_rank]
    if len(best) > 1:
        return None, "INSTRUMENT_AMBIGUOUS"
    return best[0], ""


def _execute_resolve_instrument(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    alias = _normalize_account_alias(account)
    if not alias:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="MISSING_ACCOUNT",
            message="Account alias is required.",
        )

    wallet, _secret = _lookup_credentials(alias)
    if wallet is None:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_CONFIGURED",
            message="Account is not configured.",
        )

    requested_symbol = str(request.get("symbol") or "").strip()
    if not requested_symbol:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )

    try:
        candidates = _fetch_perp_market_candidates()
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="INSTRUMENT_RESOLUTION_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )

    candidate, error_code = _resolve_instrument_candidate(requested_symbol, candidates)
    if candidate is None:
        message = "Multiple instruments match this symbol." if error_code == "INSTRUMENT_AMBIGUOUS" else "Instrument not found."
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code=error_code,
            message=message,
        )

    instrument = CanonicalInstrument(
        requested_symbol=requested_symbol,
        # Route symbol carries the full wire identifier incl. dex prefix
        # (e.g. ``xyz:SP500``) so downstream new_order/close/TP/SL/cancel
        # route to the correct HIP-3 DEX.
        symbol=candidate["route_symbol"],
        display_name=candidate["display_name"],
        price_increment=candidate["price_increment"],
        size_increment=candidate["size_increment"],
        minimum_size=candidate["size_increment"],
    )
    return make_success(
        operation="resolve_instrument",
        exchange=name,
        account=account,
        instrument=instrument,
    )


# ---------------------------------------------------------------------------
# Ladder implementation
# ---------------------------------------------------------------------------


def _decimal_from_request(value: Any) -> Optional[Decimal]:
    return _decimal_or_none(value)


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
        weight = math.exp(-(float(z) ** 2) / 2.0)
        weights.append(Decimal(str(weight)))
    return weights


def _quantize_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        raise ValueError("INVALID_INCREMENT")
    units = (value / increment).to_integral_value(rounding=ROUND_HALF_UP)
    return units * increment


def _build_ladder_prices(start_price: Decimal, end_price: Decimal, order_count: int, sz_decimals: Any) -> List[Decimal]:
    if order_count <= 0:
        return []
    if order_count == 1:
        return [_normalize_hyperliquid_order_price((start_price + end_price) / Decimal("2"), sz_decimals)]
    step = (end_price - start_price) / Decimal(order_count - 1)
    prices = [_normalize_hyperliquid_order_price(start_price + (step * Decimal(index)), sz_decimals) for index in range(order_count)]
    if start_price <= end_price:
        for index in range(1, len(prices)):
            if prices[index] < prices[index - 1]:
                prices[index] = prices[index - 1]
    else:
        for index in range(1, len(prices)):
            if prices[index] > prices[index - 1]:
                prices[index] = prices[index - 1]
    return prices


def _allocate_ladder_sizes(total_volume: Decimal, order_count: int, increment: Decimal, distribution: str) -> Tuple[List[Decimal], Decimal]:
    if increment <= 0:
        raise ValueError("INVALID_INCREMENT")
    total_units = int((total_volume / increment).to_integral_value(rounding=ROUND_HALF_UP))
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
        order_indices = sorted(range(order_count), key=lambda index: (remainders[index], -index), reverse=True)
        for index in order_indices[:residual]:
            allocation[index] += 1
    sizes = [Decimal(units) * increment for units in allocation]
    return sizes, Decimal(total_units) * increment


_HYPERLIQUID_MINIMUM_ORDER_VALUE = Decimal("10")
_LADDER_MIN_VALID_CHILDREN = 2


def _build_ladder_order_requests(
    symbol: str,
    side: str,
    distribution: str,
    order_count: int,
    total_volume: Decimal,
    start_price: Decimal,
    end_price: Decimal,
    sz_decimals: Any,
    size_increment: Decimal,
) -> Tuple[List[Dict[str, Any]], Decimal]:
    prices = _build_ladder_prices(start_price, end_price, order_count, sz_decimals)
    sizes, submitted_volume = _allocate_ladder_sizes(total_volume, order_count, size_increment, distribution)
    is_buy = side == "buy"
    order_requests: List[Dict[str, Any]] = []
    for price, size in zip(prices, sizes):
        if size <= 0:
            continue
        order_requests.append(
            {
                "coin": symbol,
                "is_buy": is_buy,
                "sz": size,
                "limit_px": price,
                "order_type": {"limit": {"tif": "Gtc"}},
                "reduce_only": False,
            }
        )
    merged_requests: List[Dict[str, Any]] = []
    for request in order_requests:
        if merged_requests:
            last = merged_requests[-1]
            if _decimal_from_request(last.get("limit_px")) == _decimal_from_request(request.get("limit_px")):
                last_sz = _decimal_from_request(last.get("sz")) or Decimal("0")
                request_sz = _decimal_from_request(request.get("sz")) or Decimal("0")
                last["sz"] = last_sz + request_sz
                continue
        merged_requests.append(dict(request))
    return merged_requests, submitted_volume


def _validate_final_ladder_children(
    order_requests: List[Dict[str, Any]],
    sz_decimals: Any,
    size_increment: Decimal,
) -> List[Dict[str, Any]]:
    validated: List[Dict[str, Any]] = []
    for index, request in enumerate(order_requests, start=1):
        price = _decimal_from_request(request.get("limit_px"))
        size = _decimal_from_request(request.get("sz"))
        notional = price * size if price is not None and size is not None else None
        price_precision_ok = (
            price is not None
            and price > 0
            and _hyperliquid_price_is_valid(price, sz_decimals)
        )
        size_precision_ok = (
            size is not None
            and size > 0
            and size_increment > 0
            and size % size_increment == 0
        )
        minimum_size_ok = size is not None and size >= size_increment
        minimum_notional_ok = notional is not None and notional >= _HYPERLIQUID_MINIMUM_ORDER_VALUE
        validated.append(
            {
                "index": index,
                "price": price,
                "size": size,
                "notional": notional,
                "price_precision_ok": price_precision_ok,
                "size_precision_ok": size_precision_ok,
                "minimum_size_ok": minimum_size_ok,
                "minimum_notional_ok": minimum_notional_ok,
                "valid": price_precision_ok and size_precision_ok and minimum_size_ok and minimum_notional_ok,
            }
        )
    return validated


def _ladder_request_units(request: Dict[str, Any], size_increment: Decimal) -> int:
    size = _decimal_from_request(request.get("sz")) or Decimal("0")
    if size_increment <= 0:
        return 0
    units = (size / size_increment).to_integral_value(rounding=ROUND_HALF_UP)
    return int(units)


def _redistribute_ladder_units(
    order_requests: List[Dict[str, Any]],
    removed_units: int,
    size_increment: Decimal,
) -> List[Dict[str, Any]]:
    if removed_units <= 0 or not order_requests:
        return [dict(request) for request in order_requests]
    current_units = [_ladder_request_units(request, size_increment) for request in order_requests]
    total_units = sum(current_units)
    if total_units <= 0:
        return [dict(request) for request in order_requests]
    raw_additions = [Decimal(removed_units) * Decimal(units) / Decimal(total_units) for units in current_units]
    base_additions = [int(addition.to_integral_value(rounding=ROUND_DOWN)) for addition in raw_additions]
    residual = removed_units - sum(base_additions)
    remainders = [raw_additions[index] - Decimal(base_additions[index]) for index in range(len(order_requests))]
    allocation = list(base_additions)
    if residual > 0:
        order_indices = sorted(range(len(order_requests)), key=lambda index: (remainders[index], -index), reverse=True)
        for index in order_indices[:residual]:
            allocation[index] += 1
    reconciled: List[Dict[str, Any]] = []
    for index, request in enumerate(order_requests):
        units = current_units[index] + allocation[index]
        updated = dict(request)
        updated["sz"] = Decimal(units) * size_increment
        reconciled.append(updated)
    return reconciled


def _reconcile_final_ladder_children(
    order_requests: List[Dict[str, Any]],
    sz_decimals: Any,
    size_increment: Decimal,
) -> Tuple[List[Dict[str, Any]], int, str]:
    current_requests = [dict(request) for request in order_requests if (_decimal_from_request(request.get("sz")) or Decimal("0")) > 0]
    omitted_below_minimum = 0
    max_iterations = max(1, len(current_requests))
    for _ in range(max_iterations):
        validation = _validate_final_ladder_children(current_requests, sz_decimals, size_increment)
        invalid_precision = [child for child in validation if not child["price_precision_ok"] or not child["size_precision_ok"] or not child["minimum_size_ok"]]
        if invalid_precision:
            return current_requests, omitted_below_minimum, "INVALID_PRECISION"
        invalid_indices = [index for index, child in enumerate(validation) if not child["minimum_notional_ok"]]
        if not invalid_indices:
            break
        if len(invalid_indices) == len(current_requests):
            return [], omitted_below_minimum + len(invalid_indices), "NO_VALID_CHILDREN"
        remaining_count = len(current_requests) - len(invalid_indices)
        if remaining_count < _LADDER_MIN_VALID_CHILDREN:
            return [], omitted_below_minimum + len(invalid_indices), "TOO_FEW_VALID_CHILDREN"
        removed_units = sum(_ladder_request_units(current_requests[index], size_increment) for index in invalid_indices)
        omitted_below_minimum += len(invalid_indices)
        invalid_index_set = set(invalid_indices)
        current_requests = [request for index, request in enumerate(current_requests) if index not in invalid_index_set]
        current_requests = _redistribute_ladder_units(current_requests, removed_units, size_increment)
    return current_requests, omitted_below_minimum, ""


def _coerce_response_dict(response: Any) -> Optional[Dict[str, Any]]:
    if isinstance(response, dict):
        return response
    json_method = getattr(response, "json", None)
    if callable(json_method):
        try:
            payload = json_method()
        except Exception:  # noqa: BLE001
            return None
        if isinstance(payload, dict):
            return payload
    return None


def _sanitize_ladder_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"https?://\S+", "[url]", text)
    text = re.sub(r"0x[a-fA-F0-9]{8,}", "0x…", text)
    text = re.sub(r"(?i)\b(authorization|bearer|api[_-]?key|secret|signature|private key)\b\s*[:=]?\s*[^\s,;]+", r"\1=[redacted]", text)
    return text


def _ladder_exchange_reason_code(reason: str) -> str:
    lowered = reason.lower()
    if any(token in lowered for token in ("insufficient margin", "not enough margin", "margin is insufficient")):
        return "INSUFFICIENT_MARGIN"
    if "price must be divisible by tick size" in lowered:
        return "INVALID_PRICE_TICK"
    if any(token in lowered for token in ("minimum value", "minimum notional", "below minimum", "must have minimum value", "order size too small")):
        return "ORDER_BELOW_MINIMUM"
    if any(token in lowered for token in ("too many orders", "weight limit", "rate limit", "request limit", "too many requests", "batch limit")):
        return "BATCH_LIMIT_EXCEEDED"
    return "EXCHANGE_REJECTED"


def _ladder_response_reason(*sources: Any) -> Optional[str]:
    for source in sources:
        if isinstance(source, str):
            text = _sanitize_ladder_text(source)
            if text:
                return text
    return None


def _log_ladder_parse_branch(branch: str, expected_count: int, accepted_count: int, rejected_count: int, exchange_reason: Optional[str]) -> None:
    """Compatibility no-op kept to preserve the frozen ladder error contract."""
    return None


def _order_response_details(response: Any, expected_count: int) -> Tuple[bool, int, int, List[int], List[Dict[str, Any]], str, Optional[str], str]:
    payload = _coerce_response_dict(response)
    if not isinstance(payload, dict):
        branch = "malformed_envelope"
        return False, 0, expected_count, [], [], "AMBIGUOUS_LADDER_RESPONSE", None, branch

    response_body = payload.get("response")
    top_status = str(payload.get("status") or "").strip().lower()
    top_response_reason = _ladder_response_reason(
        payload.get("error"),
        payload.get("response") if isinstance(payload.get("response"), str) else None,
    )
    response_status_reason = None
    if isinstance(response_body, dict):
        response_status_reason = _ladder_response_reason(
            response_body.get("error"),
            response_body.get("response") if isinstance(response_body.get("response"), str) else None,
        )
    explicit_reason = top_response_reason or response_status_reason
    if top_status == "err" or explicit_reason:
        exchange_reason = explicit_reason
        branch = "top_level_error"
        code = _ladder_exchange_reason_code(exchange_reason or "")
        _log_ladder_parse_branch(branch, expected_count, 0, expected_count, exchange_reason)
        return False, 0, expected_count, [], [], code, exchange_reason, branch

    if not isinstance(response_body, dict):
        branch = "malformed_envelope"
        return False, 0, expected_count, [], [], "AMBIGUOUS_LADDER_RESPONSE", None, branch

    data = response_body.get("data")
    statuses = None
    if isinstance(data, dict):
        statuses = data.get("statuses")
    elif isinstance(response_body.get("statuses"), list):
        statuses = response_body.get("statuses")
    elif isinstance(payload.get("statuses"), list):
        statuses = payload.get("statuses")

    if not isinstance(statuses, list):
        branch = "missing_statuses"
        return False, 0, expected_count, [], [], "AMBIGUOUS_LADDER_RESPONSE", None, branch
    accepted = 0
    child_oids: List[int] = []
    records: List[Dict[str, Any]] = []
    first_exchange_reason: Optional[str] = None
    for status in statuses:
        if not isinstance(status, dict):
            branch = "unknown_child"
            _log_ladder_parse_branch(branch, expected_count, accepted, expected_count - accepted, None)
            return False, accepted, expected_count - accepted, child_oids, records, "AMBIGUOUS_LADDER_RESPONSE", None, branch
        if isinstance(status.get("error"), str) and status.get("error"):
            exchange_reason = _ladder_response_reason(status.get("error"))
            if first_exchange_reason is None:
                first_exchange_reason = exchange_reason
            break
        resting = status.get("resting")
        filled = status.get("filled")
        oid = None
        kind = None
        if isinstance(resting, dict):
            oid = resting.get("oid")
            kind = "resting"
        elif isinstance(filled, dict):
            oid = filled.get("oid")
            kind = "filled"
        else:
            branch = "unknown_child"
            _log_ladder_parse_branch(branch, expected_count, accepted, expected_count - accepted, None)
            return False, accepted, expected_count - accepted, child_oids, records, "AMBIGUOUS_LADDER_RESPONSE", None, branch
        record: Dict[str, Any] = {"kind": kind}
        if oid is not None:
            oid_int = None
            try:
                oid_int = int(oid)
            except Exception:  # noqa: BLE001
                oid_int = None
            if oid_int is not None:
                record["oid"] = oid_int
                child_oids.append(oid_int)
        records.append(record)
        accepted += 1

    if first_exchange_reason is not None:
        branch = "child_error" if accepted == 0 else "partial_child_error"
        code = _ladder_exchange_reason_code(first_exchange_reason or "")
        rejected = expected_count - accepted if accepted > 0 else expected_count
        _log_ladder_parse_branch(branch, expected_count, accepted, rejected, first_exchange_reason)
        return False, accepted, rejected, child_oids, records, code, first_exchange_reason, branch

    if len(statuses) != expected_count:
        branch = "status_count_mismatch"
        return False, 0, expected_count, [], [], "AMBIGUOUS_LADDER_RESPONSE", None, branch

    branch = "success"
    _log_ladder_parse_branch(branch, expected_count, accepted, 0, None)
    return True, accepted, 0, child_oids, records, "", None, branch


def _order_response_statuses(response: Any, expected_count: int) -> Tuple[bool, int, List[int], str]:
    ok, accepted, _rejected, child_oids, _records, error_code, _exchange_reason, _branch = _order_response_details(response, expected_count)
    return ok, accepted, child_oids, error_code


def _execute_new_order(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    alias = _normalize_account_alias(account)
    if not alias:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="MISSING_ACCOUNT",
            message="Account alias is required.",
        )

    requested_symbol = str(request.get("symbol") or "").strip()
    requested_side = str(request.get("side") or "").strip().lower()
    order_type = str(request.get("order_type") or "limit").strip().lower() or "limit"
    volume_text = str(request.get("volume") or "").strip()
    price_text = str(request.get("price") or "").strip()

    if not requested_symbol:
        return make_failure(operation="new_order", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_side not in {"buy", "sell"}:
        return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_SIDE", message="Side must be buy or sell.")
    if order_type != "limit":
        return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_ORDER_TYPE", message="Only limit orders are supported.")

    try:
        requested_volume = _decimal_from_request(volume_text)
        requested_price = _decimal_from_request(price_text)
    except Exception:  # noqa: BLE001
        requested_volume = None
        requested_price = None
    if requested_volume is None or requested_volume <= 0:
        return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_VOLUME", message="Volume must be positive.")
    if requested_price is None or requested_price <= 0:
        return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_PRICE", message="Price must be positive.")

    try:
        candidates = _fetch_perp_market_candidates()
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="new_order", exchange=name, account=account, code="INSTRUMENT_RESOLUTION_UNAVAILABLE", message=sanitize_error_message(str(exc)))

    candidate, error_code = _resolve_instrument_candidate(requested_symbol, candidates)
    if candidate is None:
        message = "Multiple instruments match this symbol." if error_code == "INSTRUMENT_AMBIGUOUS" else "Instrument not found."
        return make_failure(operation="new_order", exchange=name, account=account, code=error_code, message=message)

    exchange_client, wallet, _secret = _build_exchange_client(account)
    if exchange_client is None or wallet is None:
        return make_failure(operation="new_order", exchange=name, account=account, code="ACCOUNT_NOT_CONFIGURED", message="Account is not configured.")

    size_increment = _decimal_from_request(candidate.get("size_increment")) or Decimal("0.00001")
    sz_decimals = _candidate_sz_decimals(candidate)

    try:
        submitted_volume = _quantize_to_increment(requested_volume, size_increment)
        submitted_price = _normalize_hyperliquid_order_price(requested_price, sz_decimals)
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_ORDER_PRECISION", message=sanitize_error_message(str(exc)))

    if submitted_volume <= 0:
        return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_VOLUME", message="Volume must be positive.")
    if submitted_price <= 0:
        return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_PRICE", message="Price must be positive.")

    coin = str(candidate.get("route_symbol") or candidate.get("public_symbol") or requested_symbol)
    order_request = {
        "coin": coin,
        "is_buy": requested_side == "buy",
        "sz": float(submitted_volume),
        "limit_px": float(submitted_price),
        "order_type": {"limit": {"tif": "Gtc"}},
        "reduce_only": False,
    }

    try:
        response = exchange_client.bulk_orders([order_request])  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="ORDER_SUBMISSION_FAILED",
            message=sanitize_error_message(str(exc)),
            order=CanonicalOrderResult(
                symbol=requested_symbol,
                side=requested_side,
                order_type=order_type,
                requested_volume=_decimal_text(requested_volume),
                requested_price=_decimal_text(requested_price),
                submitted_volume=_decimal_text(submitted_volume),
                submitted_price=_decimal_text(submitted_price),
                verified=False,
                status="failed",
            ),
        )

    ok, accepted_count, oids, error_code = _order_response_statuses(response, 1)
    if not ok:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code=error_code or "ORDER_REJECTED",
            message="Order submission failed.",
            order=CanonicalOrderResult(
                symbol=requested_symbol,
                side=requested_side,
                order_type=order_type,
                requested_volume=_decimal_text(requested_volume),
                requested_price=_decimal_text(requested_price),
                submitted_volume=_decimal_text(submitted_volume),
                submitted_price=_decimal_text(submitted_price),
                verified=False,
                status="failed",
            ),
        )

    status_payload = None
    try:
        statuses = (((response or {}).get("response") or {}).get("data") or {}).get("statuses")
        if isinstance(statuses, list) and statuses:
            first = statuses[0]
            if isinstance(first, dict):
                status_payload = first
    except Exception:  # noqa: BLE001
        status_payload = None

    exchange_order_id = oids[0] if oids else None
    status_kind = "unknown"
    if isinstance(status_payload, dict):
        if isinstance(status_payload.get("resting"), dict):
            status_kind = "resting"
            exchange_order_id = status_payload["resting"].get("oid", exchange_order_id)
        elif isinstance(status_payload.get("filled"), dict):
            status_kind = "filled"
            exchange_order_id = status_payload["filled"].get("oid", exchange_order_id)

    verified = False
    if status_kind in {"filled", "resting"}:
        verified = True
    else:
        try:
            post_orders = _fetch_open_orders_snapshot(wallet)
        except Exception:
            post_orders = []
        for order in post_orders:
            if _order_matches_resolved_instrument(order, candidate, requested_side):
                if _decimal_from_request(order.get("sz")) == submitted_volume and _decimal_from_request(order.get("limitPx") or order.get("px")) == submitted_price:
                    verified = True
                    if exchange_order_id is None:
                        oid = order.get("oid")
                        if isinstance(oid, int):
                            exchange_order_id = oid
                    break

    new_order_result = CanonicalOrderResult(
        symbol=requested_symbol,
        side=requested_side,
        order_type=order_type,
        requested_volume=_decimal_text(requested_volume),
        requested_price=_decimal_text(requested_price),
        submitted_volume=_decimal_text(submitted_volume),
        submitted_price=_decimal_text(submitted_price),
        verified=verified,
        status="success" if verified else "partial",
        exchange_order_id=exchange_order_id,
    )

    if verified:
        return make_success(
            operation="new_order",
            exchange=name,
            account=account,
            order=new_order_result,
        )
    return make_failure(
        operation="new_order",
        exchange=name,
        account=account,
        code="VERIFICATION_FAILED",
        message="Order submission could not be verified.",
        order=new_order_result,
    )


def _execute_ladder(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    alias = _normalize_account_alias(account)
    if not alias:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="MISSING_ACCOUNT",
            message="Account alias is required.",
        )

    requested_symbol = str(request.get("symbol") or "").strip()
    requested_side = str(request.get("side") or "").strip().lower()
    distribution = str(request.get("distribution") or "").strip().lower()
    requested_order_count_raw = request.get("order_count")
    requested_volume_text = str(request.get("total_volume") or "").strip()
    start_price_text = str(request.get("start_price") or "").strip()
    end_price_text = str(request.get("end_price") or "").strip()

    if not requested_symbol:
        return make_failure(operation="ladder", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_side not in {"buy", "sell"}:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_SIDE", message="Side must be buy or sell.")

    try:
        order_count = int(str(requested_order_count_raw).strip())
    except Exception:  # noqa: BLE001
        order_count = 0
    if order_count <= 0:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_ORDER_COUNT", message="Order count must be positive.")

    total_volume = _decimal_from_request(request.get("total_volume"))
    start_price = _decimal_from_request(request.get("start_price"))
    end_price = _decimal_from_request(request.get("end_price"))
    if total_volume is None or total_volume <= 0:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_VOLUME", message="Total volume must be positive.")
    if start_price is None or end_price is None:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_PRICE", message="Start and end price are required.")
    if requested_side == "buy" and end_price >= start_price:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_LADDER_DIRECTION", message="BUY ladders require end price below start price.")
    if requested_side == "sell" and end_price <= start_price:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_LADDER_DIRECTION", message="SELL ladders require end price above start price.")

    try:
        candidates = _fetch_perp_market_candidates()
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="ladder", exchange=name, account=account, code="INSTRUMENT_RESOLUTION_UNAVAILABLE", message=sanitize_error_message(str(exc)))

    candidate, error_code = _resolve_instrument_candidate(requested_symbol, candidates)
    if candidate is None:
        message = "Multiple instruments match this symbol." if error_code == "INSTRUMENT_AMBIGUOUS" else "Instrument not found."
        return make_failure(operation="ladder", exchange=name, account=account, code=error_code, message=message)

    exchange_client, wallet, _secret = _build_exchange_client(account)
    if exchange_client is None or wallet is None:
        return make_failure(operation="ladder", exchange=name, account=account, code="ACCOUNT_NOT_CONFIGURED", message="Account is not configured.")

    size_increment = _decimal_from_request(candidate.get("size_increment")) or Decimal("0.00001")
    sz_decimals = _candidate_sz_decimals(candidate)

    try:
        order_requests, submitted_volume = _build_ladder_order_requests(
            symbol=str(candidate.get("public_symbol") or requested_symbol),
            side=requested_side,
            distribution=distribution,
            order_count=order_count,
            total_volume=total_volume,
            start_price=start_price,
            end_price=end_price,
            sz_decimals=sz_decimals,
            size_increment=size_increment,
        )
    except ValueError as exc:
        code = str(exc) or "INVALID_LADDER_REQUEST"
        return make_failure(operation="ladder", exchange=name, account=account, code=code, message=sanitize_error_message(code.replace("_", " ").title()))

    if not order_requests:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_LADDER_REQUEST", message="Ladder request produced no child orders.")

    def _submitted_volume_for(requests: List[Dict[str, Any]]) -> str:
        total = sum((_decimal_from_request(request.get("sz")) or Decimal("0")) for request in requests)
        quantizer = Decimal(str(size_increment)) if size_increment > 0 else None
        if quantizer is not None:
            try:
                return format(total.quantize(quantizer, rounding=ROUND_HALF_UP), "f")
            except Exception:  # noqa: BLE001
                pass
        return _decimal_text(total)

    def _ladder_result(
        *,
        status: str,
        verified: bool,
        partial: bool,
        submitted_count: int,
        batch_count: int,
        accepted_child_count: int,
        omitted_order_count: int = 0,
        omitted_below_minimum: int = 0,
        child_order_ids: List[int],
        batches: List[Dict[str, Any]],
        requests: Optional[List[Dict[str, Any]]] = None,
    ) -> CanonicalLadderResult:
        resolved_requests = requests if requests is not None else final_requests
        return CanonicalLadderResult(
            symbol=str(candidate.get("public_symbol") or requested_symbol),
            side=requested_side,
            distribution=distribution,
            requested_order_count=order_count,
            submitted_order_count=submitted_count,
            requested_volume=requested_volume_text or _decimal_text(total_volume),
            submitted_volume=_submitted_volume_for(resolved_requests),
            batch_count=batch_count,
            verified=verified,
            partial=partial,
            status=status,
            accepted_child_count=accepted_child_count,
            omitted_order_count=omitted_order_count or None,
            omitted_below_minimum=omitted_below_minimum or None,
            child_order_ids=child_order_ids or None,
            batches=batches or None,
        )

    final_requests, omitted_below_minimum, reconcile_reason = _reconcile_final_ladder_children(order_requests, sz_decimals, size_increment)
    if not final_requests:
        failure_code = "LADDER_TOO_FEW_VALID_CHILDREN" if reconcile_reason == "TOO_FEW_VALID_CHILDREN" else "LADDER_NO_VALID_CHILDREN"
        failure_message = "Fewer than two valid ladder children remain after preflight." if failure_code == "LADDER_TOO_FEW_VALID_CHILDREN" else "No valid ladder children remain after preflight."
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code=failure_code,
            message=failure_message,
            ladder=_ladder_result(
                status="failed",
                verified=False,
                partial=False,
                submitted_count=0,
                batch_count=0,
                accepted_child_count=0,
                omitted_order_count=omitted_below_minimum,
                omitted_below_minimum=omitted_below_minimum,
                child_order_ids=[],
                batches=[],
                requests=[],
            ),
        )
    if reconcile_reason == "INVALID_PRECISION":
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="LADDER_CHILD_INVALID",
            message="One or more ladder orders are invalid after reconciliation.",
            ladder=_ladder_result(
                status="failed",
                verified=False,
                partial=False,
                submitted_count=0,
                batch_count=0,
                accepted_child_count=0,
                omitted_order_count=omitted_below_minimum,
                omitted_below_minimum=omitted_below_minimum,
                child_order_ids=[],
                batches=[],
                requests=[],
            ),
        )
    if len(final_requests) < _LADDER_MIN_VALID_CHILDREN:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="LADDER_TOO_FEW_VALID_CHILDREN",
            message="Fewer than two valid ladder children remain after preflight.",
            ladder=_ladder_result(
                status="failed",
                verified=False,
                partial=False,
                submitted_count=0,
                batch_count=0,
                accepted_child_count=0,
                omitted_order_count=omitted_below_minimum,
                omitted_below_minimum=omitted_below_minimum,
                child_order_ids=[],
                batches=[],
                requests=[],
            ),
        )

    def _client_ladder_request(request: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(request)
        sz = _decimal_from_request(payload.get("sz"))
        px = _decimal_from_request(payload.get("limit_px"))
        if sz is not None:
            payload["sz"] = float(sz)
        if px is not None:
            payload["limit_px"] = float(px)
        return payload

    batch_summaries: List[Dict[str, Any]] = []
    submission_records: List[Dict[str, Any]] = []
    child_order_ids: List[int] = []
    accepted_total = 0
    batch_count = 0
    for batch_index, start in enumerate(range(0, len(final_requests), CANCEL_BATCH_SIZE), start=1):
        chunk = [_client_ladder_request(request) for request in final_requests[start : start + CANCEL_BATCH_SIZE]]
        try:
            response = exchange_client.bulk_orders(chunk)
        except Exception as exc:  # noqa: BLE001
            sanitized_exc = _sanitize_ladder_text(f"{type(exc).__name__}: {exc}") or "Unknown error"
            logger.info("SUBMIT_EXCEPTION=%s", sanitized_exc)
            confirmed = accepted_total
            return make_failure(
                operation="ladder",
                exchange=name,
                account=account,
                code="EXCHANGE_SUBMISSION_ERROR",
                message="Hyperliquid ladder submission raised an exception.",
                exchange_reason=sanitized_exc,
                ladder=_ladder_result(
                    status="failed",
                    verified=False,
                    partial=bool(confirmed),
                    submitted_count=confirmed,
                    batch_count=batch_index - 1,
                    accepted_child_count=confirmed,
                    child_order_ids=child_order_ids,
                    batches=batch_summaries,
                ),
            )

        ok, accepted_count, rejected_count, oids, records, error_code, exchange_reason, branch = _order_response_details(response, len(chunk))
        if not ok:
            confirmed = accepted_total + accepted_count
            if error_code == "AMBIGUOUS_LADDER_RESPONSE" and not exchange_reason:
                failure_message = "Ladder submission failed."
            elif accepted_count == 0:
                failure_message = "Hyperliquid rejected the ladder."
            elif branch in {"child_error", "partial_child_error"}:
                failure_message = "Hyperliquid rejected one or more ladder children."
            else:
                failure_message = "Hyperliquid rejected the ladder."
            return make_failure(
                operation="ladder",
                exchange=name,
                account=account,
                code=error_code or "AMBIGUOUS_LADDER_RESPONSE",
                message=failure_message,
                exchange_reason=exchange_reason,
                ladder=_ladder_result(
                    status="failed",
                    verified=False,
                    partial=bool(confirmed),
                    submitted_count=confirmed,
                    batch_count=batch_index,
                    accepted_child_count=confirmed,
                    omitted_order_count=rejected_count,
                    child_order_ids=child_order_ids + oids,
                    batches=batch_summaries,
                ),
            )

        accepted_total += accepted_count
        child_order_ids.extend(oids)
        submission_records.extend(records)
        batch_summaries.append(
            {
                "batch_index": batch_index,
                "submitted_order_count": len(chunk),
                "accepted_child_count": accepted_count,
                "child_order_ids": list(oids),
            }
        )
        batch_count = batch_index

    verified, verified_count, verified_oids = _verify_ladder_submission(wallet, candidate, requested_side, final_requests, submission_records)
    if not verified:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="VERIFICATION_FAILED",
            message="Ladder submission could not be verified.",
            ladder=_ladder_result(
                status="failed",
                verified=False,
                partial=False,
                submitted_count=len(final_requests),
                batch_count=batch_count,
                accepted_child_count=accepted_total,
                child_order_ids=verified_oids or child_order_ids,
                batches=batch_summaries,
            ),
        )

    return make_success(
        operation="ladder",
        exchange=name,
        account=account,
        ladder=_ladder_result(
            status="success",
            verified=True,
            partial=False,
            submitted_count=len(final_requests),
            batch_count=batch_count,
            accepted_child_count=accepted_total,
            omitted_order_count=omitted_below_minimum,
            omitted_below_minimum=omitted_below_minimum,
            child_order_ids=verified_oids or child_order_ids,
            batches=batch_summaries,
        ),
    )


# ---------------------------------------------------------------------------
# Read-only positions / orders implementation
# ---------------------------------------------------------------------------


def _position_side_from_size(size: Decimal) -> Optional[str]:
    if size > 0:
        return "long"
    if size < 0:
        return "short"
    return None


def _open_order_side(raw_side: Any) -> str:
    """Normalize native open-order side encodings to canonical buy/sell.

    Hyperliquid can expose sides as native one-letter encodings (e.g. A/B)
    or as textual buy/sell values in some snapshots. Treat A/ask/sell as
    sell and B/bid/buy as buy so the same helper can be reused everywhere we
    reconcile open orders against positions.
    """
    text = str(raw_side or "").strip().lower()
    if text in {"b", "buy", "bid"}:
        return "buy"
    if text in {"a", "s", "sell", "ask"}:
        return "sell"
    return text or "unknown"


def _maybe_tp_sl_price(order: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    order_type = str(order.get("orderType") or order.get("order_type") or "").strip().lower()
    trigger_condition = str(order.get("triggerCondition") or order.get("trigger_condition") or "").strip().lower()
    tpsl = str(order.get("tpsl") or order.get("tpSl") or order.get("tp_sl") or "").strip().lower()
    is_position_tpsl = bool(order.get("isPositionTpsl") or order.get("is_position_tpsl"))
    trigger_px = order.get("triggerPx") or order.get("trigger_px")
    limit_px = order.get("limitPx") or order.get("px") or order.get("limit_px")
    price_px = order.get("price") or order.get("triggerPx") or order.get("trigger_px") or order.get("limitPx") or order.get("px") or order.get("limit_px")

    def _price_text() -> Optional[str]:
        price = price_px if price_px not in (None, "", "0", "0.0") else (trigger_px if trigger_px not in (None, "", "0", "0.0") else limit_px)
        return _decimal_text(price)

    if tpsl == "tp" or "take profit" in order_type or "take profit" in trigger_condition or "price above" in trigger_condition:
        return (_price_text(), None)
    if tpsl == "sl" or "stop loss" in order_type or "stop market" in order_type or "stop" in order_type or "price below" in trigger_condition:
        return (None, _price_text())
    if is_position_tpsl and (order_type == "tp" or order_type == "sl"):
        if order_type == "tp":
            return (_price_text(), None)
        return (None, _price_text())
    return (None, None)


def _protection_order_tpsl(order: Dict[str, Any]) -> Optional[str]:
    tpsl = str(order.get("tpsl") or order.get("tpSl") or order.get("tp_sl") or "").strip().lower()
    if tpsl in {"tp", "sl"}:
        return tpsl

    tp, sl = _maybe_tp_sl_price(order)
    if tp is not None and sl is None:
        return "tp"
    if sl is not None and tp is None:
        return "sl"
    return None


def _normalize_positions_orders_positions(payload: Any, protection_orders_by_symbol: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> List[CanonicalPosition]:
    if not isinstance(payload, dict):
        return []

    def _first_leg_price(orders: List[Dict[str, Any]], leg: str) -> Optional[str]:
        for order in orders:
            tp, sl = _maybe_tp_sl_price(order)
            price = tp if leg == "tp" else sl
            if price is not None:
                return price
        return None

    rows: List[CanonicalPosition] = []
    for item in payload.get("assetPositions", []):
        if not isinstance(item, dict):
            continue
        position = item.get("position") if isinstance(item.get("position"), dict) else item
        if not isinstance(position, dict):
            continue
        size_raw = _decimal_or_none(position.get("szi"))
        if size_raw is None or size_raw == 0:
            continue
        side = _position_side_from_size(size_raw)
        if side is None:
            continue
        symbol = str(position.get("coin") or "").strip()
        if not symbol:
            continue
        symbol_protections = protection_orders_by_symbol.get(symbol, {"tp": [], "sl": []})
        tp_orders = list(symbol_protections.get("tp") or [])
        sl_orders = list(symbol_protections.get("sl") or [])
        rows.append(
            CanonicalPosition(
                symbol=symbol,
                side=side,
                size=_positive_decimal_text(size_raw),
                entry_price=_decimal_text(position.get("entryPx")),
                pnl=_decimal_text(position.get("unrealizedPnl")),
                tp=_first_leg_price(tp_orders, "tp"),
                sl=_first_leg_price(sl_orders, "sl"),
                tp_count=len(tp_orders) or None,
                sl_count=len(sl_orders) or None,
            )
        )
    return rows


def _normalize_open_orders(payload: Any) -> List[Dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    rows: List[Dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        order = item.get("order") if isinstance(item.get("order"), dict) else item
        if not isinstance(order, dict):
            continue
        symbol = str(order.get("coin") or "").strip()
        side = _open_order_side(order.get("side"))
        size = _decimal_or_none(order.get("sz") if order.get("sz") not in (None, "") else order.get("origSz"))
        price = _decimal_or_none(order.get("limitPx") or order.get("px"))
        oid_raw = order.get("oid")
        try:
            oid = int(oid_raw)
        except Exception:  # noqa: BLE001
            oid = None
        if not symbol or side == "unknown" or size is None or size <= 0 or price is None or oid is None:
            continue
        tp, sl = _maybe_tp_sl_price(order)
        rows.append(
            {
                "symbol": symbol,
                "side": side,
                "oid": oid,
                "size": size,
                "price": price,
                "is_trigger": bool(order.get("isTrigger")),
                "is_position_tpsl": bool(order.get("isPositionTpsl")),
                "reduce_only": bool(order.get("reduceOnly")),
                "order_type": str(order.get("orderType") or "").strip(),
                "trigger_condition": str(order.get("triggerCondition") or "").strip(),
                "tp": tp,
                "sl": sl,
            }
        )
    return rows


def _aggregate_open_orders(
    orders: List[Dict[str, Any]],
    price_decimals_by_symbol: Optional[Dict[str, int]] = None,
) -> List[CanonicalOrderGroup]:
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for order in orders:
        key = (order["symbol"], order["side"])
        group = grouped.setdefault(
            key,
            {
                "symbol": order["symbol"],
                "side": order["side"],
                "order_count": 0,
                "total_size": Decimal("0"),
                "notional": Decimal("0"),
                "min_price": None,
                "max_price": None,
            },
        )
        group["order_count"] += 1
        group["total_size"] += order["size"]
        group["notional"] += order["price"] * order["size"]
        min_price = group["min_price"]
        max_price = group["max_price"]
        if min_price is None or order["price"] < min_price:
            group["min_price"] = order["price"]
        if max_price is None or order["price"] > max_price:
            group["max_price"] = order["price"]

    rows: List[CanonicalOrderGroup] = []
    for group in grouped.values():
        total_size: Decimal = group["total_size"]
        notional: Decimal = group["notional"]
        vwap = notional / total_size if total_size != 0 else Decimal("0")
        precision = None
        if price_decimals_by_symbol:
            precision = price_decimals_by_symbol.get(group["symbol"])
        rows.append(
            CanonicalOrderGroup(
                symbol=group["symbol"],
                side=group["side"],
                order_count=group["order_count"],
                total_size=_decimal_text(total_size),
                vwap=_format_decimal_places(vwap, precision) if precision is not None else _decimal_text(vwap),
                min_price=_decimal_text(group["min_price"]),
                max_price=_decimal_text(group["max_price"]),
            )
        )
    rows.sort(key=lambda item: (item.symbol, item.side))
    return rows


# --- Read-only positions / orders (dex-aware) -----------------------------

def _fetch_clearinghouse_state(wallet: str, dex: str = "") -> Dict[str, Any]:
    """POST clearinghouseState for a wallet, optionally scoped to a perp dex.

    ``dex=""`` is the native perp clearinghouse; HIP-3 DEXes (e.g. ``xyz``)
    return their own assetPositions keyed by full ``<dex>:<coin>`` coins.
    """
    payload: Dict[str, Any] = {"type": "clearinghouseState", "user": wallet}
    if dex:
        payload["dex"] = dex
    raw = _post_info(payload)
    return raw if isinstance(raw, dict) else {}


def _fetch_open_orders_for_dex(wallet: str, dex: str = "") -> List[Dict[str, Any]]:
    payload: Dict[str, Any] = {"type": "frontendOpenOrders", "user": wallet}
    if dex:
        payload["dex"] = dex
    raw = _post_info(payload)
    return _normalize_open_orders(raw if isinstance(raw, list) else [])


def _price_decimal_map_for_dex(dex: str = "") -> Dict[str, int]:
    payload: Dict[str, Any] = {"type": "metaAndAssetCtxs"}
    if dex:
        payload["dex"] = dex
    raw = _post_info(payload)
    return _market_price_decimals_by_symbol(raw)


def _discover_perp_dex_names() -> List[str]:
    """Return native dex + every available HIP-3 perp DEX as route prefixes."""
    names = _fetch_perp_dex_names()
    if "" not in names:
        names = [""] + names
    return names


def _execute_positions_orders(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    alias = _normalize_account_alias(account)
    if not alias:
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=account,
            code="MISSING_ACCOUNT",
            message="Account alias is required.",
        )

    wallet, _secret = _lookup_credentials(alias)
    if wallet is None:
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_CONFIGURED",
            message="Account is not configured.",
        )

    dex_names = (
        [str(request.get("dex") or "").strip()]
        if (request.get("dex") or "")
        else _discover_perp_dex_names()
    )

    try:
        aggregated_positions: Dict[str, Any] = {"assetPositions": []}
        aggregated_orders: List[Dict[str, Any]] = []
        price_decimals_by_symbol: Dict[str, int] = {}
        if dex_names:
            for dex_idx, dex in enumerate(dex_names):
                try:
                    positions_raw = _fetch_clearinghouse_state(wallet, dex)
                    open_orders = _fetch_open_orders_for_dex(wallet, dex)
                except Exception:  # noqa: BLE001
                    continue
                dex_positions = positions_raw.get("assetPositions")
                if isinstance(dex_positions, list):
                    aggregated_positions["assetPositions"].extend(dex_positions)
                aggregated_orders.extend(open_orders)
                try:
                    _decimals = _price_decimal_map_for_dex(dex)
                    for sym, prec in _decimals.items():
                        if sym not in price_decimals_by_symbol:
                            price_decimals_by_symbol[sym] = prec
                except Exception:  # noqa: BLE001
                    continue
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=account,
            code="POSITIONS_ORDERS_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )

    protection_orders_by_symbol: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for order in aggregated_orders:
        symbol = order["symbol"]
        protection_type = _protection_order_tpsl(order)
        if protection_type not in {"tp", "sl"}:
            continue
        symbol_map = protection_orders_by_symbol.setdefault(symbol, {"tp": [], "sl": []})
        symbol_map[protection_type].append(order)

    positions = _normalize_positions_orders_positions(aggregated_positions, protection_orders_by_symbol)
    order_groups = _aggregate_open_orders(aggregated_orders, price_decimals_by_symbol)

    return make_success(
        operation="positions_orders",
        exchange=name,
        account=account,
        positions=positions,
        open_order_count=len(aggregated_orders),
        order_groups=order_groups,
    )


def _execute_positions_management(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    response = _execute_positions_orders(account, request)
    if not response.success:
        return response
    return make_success(
        operation="positions_management",
        exchange=name,
        account=account,
        positions=response.positions,
    )


def _current_position_management_context(
    operation: str,
    account: str,
    requested_symbol: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[CanonicalResponse]]:
    alias = _normalize_account_alias(account)
    if not alias:
        return None, make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="MISSING_ACCOUNT",
            message="Account alias is required.",
        )

    positions_response = _execute_positions_orders(account, {"operation": "positions_orders", "exchange": name, "account": account})
    if not positions_response.success:
        return None, make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code=positions_response.error.code if positions_response.error else "POSITIONS_UNAVAILABLE",
            message=positions_response.error.message if positions_response.error else "Positions unavailable.",
            exchange_reason=getattr(positions_response.error, "exchange_reason", None) if positions_response.error else None,
        )

    positions = list(positions_response.positions or [])
    requested_raw = (requested_symbol or "").strip().upper()
    symbol = requested_raw
    # Match a position by its full route identifier (e.g. ``xyz:SP500``)
    # OR by its dex-stripped alias (e.g. ``SP500``). Positions now carry the
    # full coin from the API (including any HIP-3 dex prefix), so we must
    # accept either form to preserve dex identity without an extra network
    # round-trip here. ``_symbol_key`` strips non-alphanumerics, so both
    # ``xyz:SP500`` and ``SP500`` collapse to the same canonical key when the
    # requested symbol is a bare alias; a fully-prefixed request (``xyz:SP500``)
    # only matches the position whose symbol carries that prefix, preventing
    # cross-dex collisions.
    requested_keys = {_symbol_key(requested_raw)}
    if ":" in requested_raw:
        # Fully-prefixed request: accept the prefix-preserved position symbol.
        pass
    else:
        # Bare alias: a position may surface as either ``SP500`` (native-style,
        # no prefix) or ``xyz:SP500`` (HIP-3). Accept the dex-stripped tail too.
        requested_keys.add(_symbol_key(requested_raw))
    current_position = None
    for position in positions:
        pos_key = _symbol_key(getattr(position, "symbol", ""))
        if not pos_key:
            continue
        pos_display = pos_key
        if ":" in str(getattr(position, "symbol", "")):
            # Strip the dex prefix for alias matching (kept the exact key
            # above for prefixed requests). The full route key is already in
            # ``requested_keys`` when the caller passed ``xyz:SP500``.
            pos_display_alias = _symbol_key(str(getattr(position, "symbol", "")).split(":", 1)[1])
        else:
            pos_display_alias = pos_key
        if pos_key in requested_keys or pos_display_alias in requested_keys:
            current_position = position
            break
    if current_position is None:
        return None, make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="POSITION_NOT_FOUND",
            message="Position not found.",
        )

    wallet, _secret = _lookup_credentials(alias)
    if wallet is None:
        return None, make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_CONFIGURED",
            message="Account is not configured.",
        )

    try:
        candidates = _fetch_perp_market_candidates()
    except Exception as exc:  # noqa: BLE001
        return None, make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="INSTRUMENT_RESOLUTION_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )

    candidate, error_code = _resolve_instrument_candidate(symbol, candidates)
    if candidate is None:
        message = "Multiple instruments match this symbol." if error_code == "INSTRUMENT_AMBIGUOUS" else "Instrument not found."
        return None, make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code=error_code,
            message=message,
        )

    try:
        open_orders = _fetch_open_orders_snapshot(wallet)
    except Exception as exc:  # noqa: BLE001
        return None, make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="OPEN_ORDERS_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )

    mark_price = _fetch_candidate_mark_price(candidate)
    reference_price = mark_price if mark_price is not None else _decimal_or_none(getattr(current_position, "entry_price", None))
    current_size = _decimal_or_none(getattr(current_position, "size", None))
    if current_size is None or current_size <= 0:
        return None, make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="POSITION_NOT_FOUND",
            message="Position not found.",
        )

    current_side = str(getattr(current_position, "side", "")).strip().lower()
    closing_side = "sell" if current_side == "long" else "buy" if current_side == "short" else ""

    return {
        "alias": alias,
        "account": account,
        "wallet": wallet,
        "candidate": candidate,
        "positions_response": positions_response,
        "current_position": current_position,
        "open_orders": open_orders,
        "reference_price": reference_price,
        "current_size": current_size,
        "current_side": current_side,
        "closing_side": closing_side,
    }, None


def _fetch_candidate_mark_price(candidate: Dict[str, Any]) -> Optional[Decimal]:
    internal_name = str(candidate.get("internal_name") or "").strip()
    if not internal_name:
        return None
    payload: Dict[str, Any] = {"type": "metaAndAssetCtxs"}
    dex = str(candidate.get("dex") or "").strip()
    if dex:
        payload["dex"] = dex
    try:
        raw = _post_info(payload)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    meta, asset_ctxs = raw[0], raw[1]
    if not isinstance(meta, dict) or not isinstance(asset_ctxs, list):
        return None
    universe = meta.get("universe")
    if not isinstance(universe, list):
        return None
    for index, instrument in enumerate(universe):
        if not isinstance(instrument, dict):
            continue
        if str(instrument.get("name") or "").strip() != internal_name:
            continue
        ctx = asset_ctxs[index] if index < len(asset_ctxs) and isinstance(asset_ctxs[index], dict) else {}
        for key in ("markPx", "midPx", "prevDayPx", "oraclePx"):
            price = _decimal_or_none(ctx.get(key))
            if price is not None and price > 0:
                return price
    return None


def _classify_position_protection_orders(
    open_orders: List[Dict[str, Any]],
    symbol: str,
    closing_side: str,
) -> Dict[str, List[Dict[str, Any]]]:
    result: Dict[str, List[Dict[str, Any]]] = {"tp": [], "sl": [], "unknown": []}
    for order in open_orders:
        if str(order.get("symbol") or order.get("coin") or "").strip().upper() != symbol:
            continue
        if _open_order_side(order.get("side")) != closing_side:
            continue
        if not bool(order.get("reduce_only") or order.get("reduceOnly")):
            continue
        tpsl = _protection_order_tpsl(order)
        if tpsl == "tp":
            result["tp"].append(order)
        elif tpsl == "sl":
            result["sl"].append(order)
    return result


def _build_position_trigger_request(
    candidate: Dict[str, Any],
    current_position: Any,
    closing_side: str,
    price: Decimal,
    tpsl: str,
) -> Dict[str, Any]:
    symbol = str(candidate.get("public_symbol") or getattr(current_position, "symbol", "") or "").strip()
    size = _decimal_or_none(getattr(current_position, "size", None))
    if size is None:
        size = Decimal("0")
    return {
        "coin": symbol,
        "is_buy": closing_side == "buy",
        "sz": float(size),
        "limit_px": float(price),
        "order_type": {"trigger": {"triggerPx": float(price), "isMarket": True, "tpsl": tpsl}},
        "reduce_only": True,
    }


def _position_action_result(
    operation: str,
    symbol: str,
    verified: bool,
    price: Optional[Decimal] = None,
    removed: Optional[bool] = None,
    status: str = "success",
    exchange_order_id: Optional[int] = None,
    current_side: Optional[str] = None,
    current_size: Optional[Decimal] = None,
    message: Optional[str] = None,
) -> CanonicalPositionActionResult:
    return CanonicalPositionActionResult(
        operation=operation,
        symbol=symbol,
        verified=verified,
        price=_decimal_text(price) if price is not None else None,
        removed=removed,
        status=status,
        exchange_order_id=exchange_order_id,
        current_side=current_side,
        current_size=_decimal_text(current_size) if current_size is not None else None,
        message=message,
    )


def _find_position_protection_order(
    open_orders: List[Dict[str, Any]],
    symbol: str,
    closing_side: str,
    tpsl: str,
) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    for order in open_orders:
        if str(order.get("symbol") or order.get("coin") or "").strip().upper() != symbol:
            continue
        if _open_order_side(order.get("side")) != closing_side:
            continue
        if not bool(order.get("reduce_only") or order.get("reduceOnly")):
            continue
        if _protection_order_tpsl(order) != tpsl:
            continue
        tp, sl = _maybe_tp_sl_price(order)
        if tpsl == "tp" and tp is not None and sl is None:
            matches.append(order)
        elif tpsl == "sl" and sl is not None and tp is None:
            matches.append(order)
    return matches


def _find_position_protection_removal_order(
    open_orders: List[Dict[str, Any]],
    symbol: str,
    closing_side: str,
    expected_price: Decimal,
    expected_size: Optional[Decimal] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    def _order_price(order: Dict[str, Any]) -> Optional[Decimal]:
        if order.get("price") not in (None, ""):
            return _decimal_or_none(order.get("price"))
        return _decimal_or_none(order.get("limitPx") or order.get("px") or order.get("triggerPx"))

    matches: List[Dict[str, Any]] = []
    for order in open_orders:
        if str(order.get("symbol") or order.get("coin") or "").strip().upper() != symbol:
            continue
        if _open_order_side(order.get("side")) != closing_side:
            continue
        if not bool(order.get("reduce_only") or order.get("reduceOnly")):
            continue
        price = _order_price(order)
        if price != expected_price:
            continue
        if expected_size is not None:
            size = _decimal_or_none(order.get("size") or order.get("sz") or order.get("origSz"))
            if size != expected_size:
                continue
        matches.append(order)
    if len(matches) > 1:
        return None, "AMBIGUOUS_PROTECTION_STATE"
    if len(matches) == 1:
        return matches[0], None
    return None, None


def _verify_position_action(
    operation: str,
    account: str,
    requested_symbol: str,
    expected_tpsl: Optional[str],
    target_oid: Optional[int],
    expected_price: Optional[Decimal],
    removed: bool,
) -> Tuple[Optional[Dict[str, Any]], Optional[CanonicalResponse]]:
    context, failure = _current_position_management_context(operation, account, requested_symbol)
    if failure is not None:
        return None, failure
    assert context is not None
    symbol = str(requested_symbol or "").strip().upper()
    matches = [] if expected_tpsl is None else _find_position_protection_order(context["open_orders"], symbol, context["closing_side"], expected_tpsl)
    if expected_tpsl is not None and len(matches) > 1:
        return None, make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="AMBIGUOUS_PROTECTION_STATE",
            message="Multiple matching protection orders were found.",
        )
    if removed:
        if expected_tpsl is not None and matches:
            return None, make_failure(
                operation=operation,
                exchange=name,
                account=account,
                code="VERIFICATION_FAILED",
                message="Protection order still present after cancellation.",
            )
        return context, None
    if expected_tpsl is not None and not matches:
        return None, make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="VERIFICATION_FAILED",
            message="Protection order could not be verified.",
        )
    if target_oid is not None:
        oid_matches = [order for order in matches if int(order.get("oid", -1)) == target_oid]
        if not oid_matches:
            return None, make_failure(
                operation=operation,
                exchange=name,
                account=account,
                code="VERIFICATION_FAILED",
                message="Expected protection order ID was not present after submission.",
            )
    if expected_price is not None and matches:
        price = _decimal_or_none(matches[0].get("price"))
        trigger_price = _decimal_or_none(matches[0].get("triggerPx") or matches[0].get("trigger_px"))
        if price is not None and price != expected_price and trigger_price is not None and trigger_price != expected_price:
            return None, make_failure(
                operation=operation,
                exchange=name,
                account=account,
                code="VERIFICATION_FAILED",
                message="Protection order price did not match the requested value.",
            )
    return context, None


def _verify_position_protection_submission(
    operation: str,
    account: str,
    requested_symbol: str,
    expected_tpsl: str,
    expected_price: Decimal,
    submitted_oid: Optional[int],
    pre_order_oids: Set[int],
    ambiguous_submission: bool,
) -> Tuple[Optional[Dict[str, Any]], Optional[CanonicalResponse], Optional[int]]:
    context, failure = _current_position_management_context(operation, account, requested_symbol)
    if failure is not None:
        return None, failure, None
    assert context is not None

    symbol = str(requested_symbol or "").strip().upper()
    current_position = context["current_position"]
    current_size = _decimal_or_none(getattr(current_position, "size", None))
    position_tp = _decimal_or_none(getattr(current_position, "tp", None))
    position_sl = _decimal_or_none(getattr(current_position, "sl", None))

    def _price_of(order: Dict[str, Any]) -> Optional[Decimal]:
        return _decimal_or_none(order.get("price") or order.get("limitPx") or order.get("px") or order.get("triggerPx"))

    def _size_of(order: Dict[str, Any]) -> Optional[Decimal]:
        return _decimal_or_none(order.get("size") or order.get("sz") or order.get("origSz"))

    strict_matches = _find_position_protection_order(context["open_orders"], symbol, context["closing_side"], expected_tpsl)
    if len(strict_matches) > 1:
        return None, make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="AMBIGUOUS_PROTECTION_STATE",
            message="Multiple matching protection orders were found.",
        ), None

    matches = list(strict_matches)
    if not matches:
        authoritative_price = position_tp if expected_tpsl == "tp" else position_sl if expected_tpsl == "sl" else None
        if authoritative_price is not None and authoritative_price == expected_price and current_size is not None:
            heuristic_matches: List[Dict[str, Any]] = []
            for order in context["open_orders"]:
                order_symbol = str(order.get("symbol") or order.get("coin") or "").strip().upper()
                if order_symbol != symbol:
                    continue
                if _open_order_side(order.get("side")) != context["closing_side"]:
                    continue
                if not bool(order.get("reduce_only") or order.get("reduceOnly")):
                    continue
                price = _price_of(order)
                size = _size_of(order)
                if price != expected_price or size != current_size:
                    continue
                heuristic_matches.append(order)
            if len(heuristic_matches) > 1:
                return None, make_failure(
                    operation=operation,
                    exchange=name,
                    account=account,
                    code="AMBIGUOUS_PROTECTION_STATE",
                    message="Multiple matching take-profit orders were found.",
                ), None
            matches = heuristic_matches

    if not matches:
        semantic_matches: List[Dict[str, Any]] = []
        for order in context["open_orders"]:
            order_symbol = str(order.get("symbol") or order.get("coin") or "").strip().upper()
            if order_symbol != symbol:
                continue
            if _open_order_side(order.get("side")) != context["closing_side"]:
                continue
            if not bool(order.get("reduce_only") or order.get("reduceOnly")):
                continue
            try:
                oid = int(order.get("oid"))
            except Exception:  # noqa: BLE001
                oid = None
            if oid is None or oid in pre_order_oids:
                continue
            price = _price_of(order)
            size = _size_of(order)
            if price != expected_price or size != current_size:
                continue
            if _protection_order_tpsl(order) != expected_tpsl:
                continue
            semantic_matches.append(order)
        if len(semantic_matches) > 1:
            return None, make_failure(
                operation=operation,
                exchange=name,
                account=account,
                code="AMBIGUOUS_PROTECTION_STATE",
                message="Multiple matching protection orders were found.",
            ), None
        matches = semantic_matches

    if not matches:
        code = "POSITION_ACTION_RESPONSE_AMBIGUOUS" if ambiguous_submission else "VERIFICATION_FAILED"
        return None, make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code=code,
            message="Position protection submission could not be verified.",
        ), None

    matching_oids: List[int] = []
    for order in matches:
        try:
            oid = int(order.get("oid"))
        except Exception:  # noqa: BLE001
            continue
        matching_oids.append(oid)

    new_oids = [oid for oid in matching_oids if oid not in pre_order_oids]
    verified_oid = submitted_oid if submitted_oid in matching_oids else None
    if verified_oid is None and len(new_oids) == 1:
        verified_oid = new_oids[0]

    if verified_oid is None:
        code = "POSITION_ACTION_RESPONSE_AMBIGUOUS" if ambiguous_submission else "VERIFICATION_FAILED"
        message = "Position protection submission could not be verified."
        if matching_oids and all(oid in pre_order_oids for oid in matching_oids):
            message = "Existing protection order matched but no new write could be proven."
        return None, make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code=code,
            message=message,
        ), None

    return context, None, verified_oid


def _execute_set_tp(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    requested_symbol = str(request.get("symbol") or "").strip()
    price_value = _decimal_from_request(request.get("price"))
    if not requested_symbol:
        return make_failure(operation="set_tp", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    if price_value is None:
        return make_failure(operation="set_tp", exchange=name, account=account, code="INVALID_TP_PRICE", message="TP price must be numeric.")

    context, failure = _current_position_management_context("set_tp", account, requested_symbol)
    if failure is not None:
        return failure
    assert context is not None

    symbol = str(requested_symbol or "").strip().upper()
    current_side = str(context["current_side"] or "").strip().lower()
    current_position = context["current_position"]
    reference_price = context["reference_price"]
    pre_order_oids = {
        int(order.get("oid"))
        for order in context["open_orders"]
        if isinstance(order.get("oid"), int)
    }

    if price_value == 0:
        protection_state = _classify_position_protection_orders(context["open_orders"], symbol, context["closing_side"])
        if protection_state["unknown"]:
            return make_failure(operation="set_tp", exchange=name, account=account, code="AMBIGUOUS_PROTECTION_STATE", message="Protection ownership could not be determined safely.")
        if len(protection_state["tp"]) > 1:
            return make_failure(operation="set_tp", exchange=name, account=account, code="AMBIGUOUS_PROTECTION_STATE", message="Multiple matching take-profit orders were found.")
        target_tp = protection_state["tp"][0] if protection_state["tp"] else None
        if target_tp is None:
            known_tp = _decimal_or_none(getattr(current_position, "tp", None))
            if known_tp is not None:
                target_tp, removal_error = _find_position_protection_removal_order(
                    context["open_orders"],
                    symbol,
                    context["closing_side"],
                    known_tp,
                    _decimal_or_none(context["current_size"]),
                )
                if removal_error is not None:
                    return make_failure(operation="set_tp", exchange=name, account=account, code=removal_error, message="Protection ownership could not be determined safely.")
                if target_tp is None:
                    return make_failure(operation="set_tp", exchange=name, account=account, code="TP_REMOVAL_TARGET_NOT_FOUND", message="Take Profit removal target could not be determined safely.")
        if target_tp is None:
            return make_success(
                operation="set_tp",
                exchange=name,
                account=account,
                position_action=_position_action_result(
                    operation="set_tp",
                    symbol=symbol,
                    verified=True,
                    removed=False,
                    status="success",
                    current_side=current_side,
                    current_size=context["current_size"],
                    message="No Take Profit was set.",
                ),
            )
        target_oid = None
        if isinstance(target_tp, dict) and isinstance(target_tp.get("oid"), int):
            target_oid = int(target_tp["oid"])
        if target_oid is None:
            return make_failure(operation="set_tp", exchange=name, account=account, code="AMBIGUOUS_PROTECTION_STATE", message="Take Profit ownership could not be determined safely.")
        exchange_client, wallet, _secret = _build_exchange_client(account)
        if exchange_client is None or wallet is None:
            return make_failure(operation="set_tp", exchange=name, account=account, code="ACCOUNT_NOT_CONFIGURED", message="Account is not configured.")
        try:
            response = exchange_client.bulk_cancel([{"coin": symbol, "oid": target_oid}])
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation="set_tp",
                exchange=name,
                account=account,
                code="TP_REMOVAL_FAILED",
                message=sanitize_error_message(str(exc)),
                position_action=_position_action_result(
                    operation="set_tp",
                    symbol=symbol,
                    verified=False,
                    removed=True,
                    status="failed",
                    current_side=current_side,
                    current_size=context["current_size"],
                ),
            )
        ok, _confirmed, error_code = _cancel_response_statuses(response, 1)
        if not ok:
            if error_code != "AMBIGUOUS_CANCEL_RESPONSE":
                return make_failure(
                    operation="set_tp",
                    exchange=name,
                    account=account,
                    code=error_code or "TP_REMOVAL_FAILED",
                    message="Take Profit removal failed.",
                    position_action=_position_action_result(
                        operation="set_tp",
                        symbol=symbol,
                        verified=False,
                        removed=True,
                        status="failed",
                        current_side=current_side,
                        current_size=context["current_size"],
                    ),
                )
        verified_context, verification_failure = _verify_position_action("set_tp", account, requested_symbol, "tp", target_oid, None, removed=True)
        if verification_failure is not None:
            return verification_failure
        assert verified_context is not None
        post_oids = {
            int(order.get("oid"))
            for order in verified_context["open_orders"]
            if isinstance(order.get("oid"), int)
        }
        if not pre_order_oids.difference({target_oid}).issubset(post_oids):
            return make_failure(
                operation="set_tp",
                exchange=name,
                account=account,
                code="VERIFICATION_FAILED",
                message="Unrelated orders changed during TP removal.",
                position_action=_position_action_result(
                    operation="set_tp",
                    symbol=symbol,
                    verified=False,
                    removed=True,
                    status="failed",
                    current_side=current_side,
                    current_size=context["current_size"],
                ),
            )
        return make_success(
            operation="set_tp",
            exchange=name,
            account=account,
            position_action=_position_action_result(
                operation="set_tp",
                symbol=symbol,
                verified=True,
                removed=True,
                status="success",
                exchange_order_id=target_oid,
                current_side=current_side,
                current_size=context["current_size"],
                message="Take Profit removed.",
            ),
        )

    if reference_price is not None:
        if current_side == "long" and price_value <= reference_price:
            return make_failure(operation="set_tp", exchange=name, account=account, code="INVALID_TP_PRICE", message="TP price must be above the current reference price.")
        if current_side == "short" and price_value >= reference_price:
            return make_failure(operation="set_tp", exchange=name, account=account, code="INVALID_TP_PRICE", message="TP price must be below the current reference price.")

    protection_state = _classify_position_protection_orders(context["open_orders"], symbol, context["closing_side"])
    if protection_state["unknown"]:
        return make_failure(operation="set_tp", exchange=name, account=account, code="AMBIGUOUS_PROTECTION_STATE", message="Protection ownership could not be determined safely.")
    if len(protection_state["tp"]) > 1:
        return make_failure(operation="set_tp", exchange=name, account=account, code="AMBIGUOUS_PROTECTION_STATE", message="Multiple matching take-profit orders were found.")

    exchange_client, wallet, _secret = _build_exchange_client(account)
    if exchange_client is None or wallet is None:
        return make_failure(operation="set_tp", exchange=name, account=account, code="ACCOUNT_NOT_CONFIGURED", message="Account is not configured.")

    existing_tp = protection_state["tp"][0] if protection_state["tp"] else None
    target_oid = None
    if isinstance(existing_tp, dict):
        existing_oid = existing_tp.get("oid")
        if isinstance(existing_oid, int):
            target_oid = existing_oid
    if existing_tp is not None:
        existing_price = _decimal_or_none(existing_tp.get("price"))
        existing_trigger = _decimal_or_none(existing_tp.get("triggerPx") or existing_tp.get("trigger_px"))
        if existing_price == price_value or existing_trigger == price_value:
            verified_context, verification_failure = _verify_position_action("set_tp", account, requested_symbol, "tp", target_oid, price_value, removed=False)
            if verification_failure is not None:
                return verification_failure
            assert verified_context is not None
            action = _position_action_result(
                operation="set_tp",
                symbol=symbol,
                verified=True,
                price=price_value,
                removed=False,
                status="success",
                exchange_order_id=target_oid,
                current_side=current_side,
                current_size=context["current_size"],
                message="Take Profit already set.",
            )
            return make_success(operation="set_tp", exchange=name, account=account, position_action=action)

    request_payload = _build_position_trigger_request(context["candidate"], current_position, context["closing_side"], price_value, "tp")
    submitted_oid: Optional[int] = target_oid

    try:
        if existing_tp is not None and target_oid is not None:
            response = exchange_client.bulk_modify_orders_new([{"oid": target_oid, "order": request_payload}])
        else:
            response = exchange_client.bulk_orders([request_payload], grouping="positionTpsl")
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="set_tp",
            exchange=name,
            account=account,
            code="TP_SUBMISSION_FAILED",
            message=sanitize_error_message(str(exc)),
            position_action=_position_action_result(
                operation="set_tp",
                symbol=symbol,
                verified=False,
                price=price_value,
                removed=False,
                status="failed",
                current_side=current_side,
                current_size=context["current_size"],
            ),
        )

    ok, _accepted_count, _rejected_count, oids, _records, error_code, exchange_reason, branch = _order_response_details(response, 1)
    ambiguous_submission = not ok and error_code == "AMBIGUOUS_LADDER_RESPONSE"
    if not ok and not ambiguous_submission and existing_tp is None:
        return make_failure(
            operation="set_tp",
            exchange=name,
            account=account,
            code=error_code or "TP_SUBMISSION_FAILED",
            message="Take Profit submission failed.",
            exchange_reason=exchange_reason,
            position_action=_position_action_result(
                operation="set_tp",
                symbol=symbol,
                verified=False,
                price=price_value,
                removed=False,
                status="failed",
                current_side=current_side,
                current_size=context["current_size"],
            ),
        )
    if oids:
        submitted_oid = oids[0]

    verified_context, verification_failure, verified_oid = _verify_position_protection_submission(
        "set_tp",
        account,
        requested_symbol,
        "tp",
        price_value,
        submitted_oid,
        pre_order_oids,
        ambiguous_submission,
    )
    if verification_failure is not None:
        if ambiguous_submission:
            return make_failure(
                operation="set_tp",
                exchange=name,
                account=account,
                code="POSITION_ACTION_RESPONSE_AMBIGUOUS",
                message="Take Profit submission could not be verified.",
                position_action=_position_action_result(
                    operation="set_tp",
                    symbol=symbol,
                    verified=False,
                    price=price_value,
                    removed=False,
                    status="failed",
                    current_side=current_side,
                    current_size=context["current_size"],
                ),
            )
        return verification_failure
    assert verified_context is not None
    if verified_oid is not None:
        submitted_oid = verified_oid
    post_oids = {
        int(order.get("oid"))
        for order in verified_context["open_orders"]
        if isinstance(order.get("oid"), int)
    }
    if not pre_order_oids.difference({target_oid} if target_oid is not None else set()).issubset(post_oids):
        return make_failure(
            operation="set_tp",
            exchange=name,
            account=account,
            code="VERIFICATION_FAILED",
            message="Unrelated orders changed during TP submission.",
            position_action=_position_action_result(
                operation="set_tp",
                symbol=symbol,
                verified=False,
                price=price_value,
                removed=False,
                status="failed",
                current_side=current_side,
                current_size=context["current_size"],
            ),
        )
    return make_success(
        operation="set_tp",
        exchange=name,
        account=account,
        position_action=_position_action_result(
            operation="set_tp",
            symbol=symbol,
            verified=True,
            price=price_value,
            removed=False,
            status="success",
            exchange_order_id=submitted_oid,
            current_side=current_side,
            current_size=context["current_size"],
        ),
    )


def _execute_set_sl(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    requested_symbol = str(request.get("symbol") or "").strip()
    price_value = _decimal_from_request(request.get("price"))
    if not requested_symbol:
        return make_failure(operation="set_sl", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    if price_value is None:
        return make_failure(operation="set_sl", exchange=name, account=account, code="INVALID_SL_PRICE", message="SL price must be numeric.")

    context, failure = _current_position_management_context("set_sl", account, requested_symbol)
    if failure is not None:
        return failure
    assert context is not None

    symbol = str(requested_symbol or "").strip().upper()
    current_side = str(context["current_side"] or "").strip().lower()
    current_position = context["current_position"]
    reference_price = context["reference_price"]
    pre_order_oids = {
        int(order.get("oid"))
        for order in context["open_orders"]
        if isinstance(order.get("oid"), int)
    }

    if price_value == 0:
        protection_state = _classify_position_protection_orders(context["open_orders"], symbol, context["closing_side"])
        if protection_state["unknown"]:
            return make_failure(operation="set_sl", exchange=name, account=account, code="AMBIGUOUS_PROTECTION_STATE", message="Protection ownership could not be determined safely.")
        if len(protection_state["sl"]) > 1:
            return make_failure(operation="set_sl", exchange=name, account=account, code="AMBIGUOUS_PROTECTION_STATE", message="Multiple matching stop-loss orders were found.")
        target_sl = protection_state["sl"][0] if protection_state["sl"] else None
        if target_sl is None:
            known_sl = _decimal_or_none(getattr(current_position, "sl", None))
            if known_sl is not None:
                target_sl, removal_error = _find_position_protection_removal_order(
                    context["open_orders"],
                    symbol,
                    context["closing_side"],
                    known_sl,
                    _decimal_or_none(context["current_size"]),
                )
                if removal_error is not None:
                    return make_failure(operation="set_sl", exchange=name, account=account, code=removal_error, message="Protection ownership could not be determined safely.")
                if target_sl is None:
                    return make_failure(operation="set_sl", exchange=name, account=account, code="SL_REMOVAL_TARGET_NOT_FOUND", message="Stop Loss removal target could not be determined safely.")
        if target_sl is None:
            return make_success(
                operation="set_sl",
                exchange=name,
                account=account,
                position_action=_position_action_result(
                    operation="set_sl",
                    symbol=symbol,
                    verified=True,
                    removed=False,
                    status="success",
                    current_side=current_side,
                    current_size=context["current_size"],
                    message="No Stop Loss was set.",
                ),
            )
        target_oid = None
        if isinstance(target_sl, dict) and isinstance(target_sl.get("oid"), int):
            target_oid = int(target_sl["oid"])
        if target_oid is None:
            return make_failure(operation="set_sl", exchange=name, account=account, code="AMBIGUOUS_PROTECTION_STATE", message="Stop Loss ownership could not be determined safely.")
        exchange_client, wallet, _secret = _build_exchange_client(account)
        if exchange_client is None or wallet is None:
            return make_failure(operation="set_sl", exchange=name, account=account, code="ACCOUNT_NOT_CONFIGURED", message="Account is not configured.")
        try:
            response = exchange_client.bulk_cancel([{"coin": symbol, "oid": target_oid}])
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation="set_sl",
                exchange=name,
                account=account,
                code="SL_REMOVAL_FAILED",
                message=sanitize_error_message(str(exc)),
                position_action=_position_action_result(
                    operation="set_sl",
                    symbol=symbol,
                    verified=False,
                    removed=True,
                    status="failed",
                    current_side=current_side,
                    current_size=context["current_size"],
                ),
            )
        ok, _confirmed, error_code = _cancel_response_statuses(response, 1)
        if not ok:
            if error_code != "AMBIGUOUS_CANCEL_RESPONSE":
                return make_failure(
                    operation="set_sl",
                    exchange=name,
                    account=account,
                    code=error_code or "SL_REMOVAL_FAILED",
                    message="Stop Loss removal failed.",
                    position_action=_position_action_result(
                        operation="set_sl",
                        symbol=symbol,
                        verified=False,
                        removed=True,
                        status="failed",
                        current_side=current_side,
                        current_size=context["current_size"],
                    ),
                )
        verified_context, verification_failure = _verify_position_action("set_sl", account, requested_symbol, "sl", target_oid, None, removed=True)
        if verification_failure is not None:
            return verification_failure
        assert verified_context is not None
        post_oids = {
            int(order.get("oid"))
            for order in verified_context["open_orders"]
            if isinstance(order.get("oid"), int)
        }
        if not pre_order_oids.difference({target_oid}).issubset(post_oids):
            return make_failure(
                operation="set_sl",
                exchange=name,
                account=account,
                code="VERIFICATION_FAILED",
                message="Unrelated orders changed during SL removal.",
                position_action=_position_action_result(
                    operation="set_sl",
                    symbol=symbol,
                    verified=False,
                    removed=True,
                    status="failed",
                    current_side=current_side,
                    current_size=context["current_size"],
                ),
            )
        return make_success(
            operation="set_sl",
            exchange=name,
            account=account,
            position_action=_position_action_result(
                operation="set_sl",
                symbol=symbol,
                verified=True,
                removed=True,
                status="success",
                exchange_order_id=target_oid,
                current_side=current_side,
                current_size=context["current_size"],
                message="Stop Loss removed.",
            ),
        )

    if reference_price is not None:
        if current_side == "long" and price_value >= reference_price:
            return make_failure(operation="set_sl", exchange=name, account=account, code="INVALID_SL_PRICE", message="SL price must be below the current reference price.")
        if current_side == "short" and price_value <= reference_price:
            return make_failure(operation="set_sl", exchange=name, account=account, code="INVALID_SL_PRICE", message="SL price must be above the current reference price.")

    protection_state = _classify_position_protection_orders(context["open_orders"], symbol, context["closing_side"])
    if protection_state["unknown"]:
        return make_failure(operation="set_sl", exchange=name, account=account, code="AMBIGUOUS_PROTECTION_STATE", message="Protection ownership could not be determined safely.")
    if len(protection_state["sl"]) > 1:
        return make_failure(operation="set_sl", exchange=name, account=account, code="AMBIGUOUS_PROTECTION_STATE", message="Multiple matching stop-loss orders were found.")

    exchange_client, wallet, _secret = _build_exchange_client(account)
    if exchange_client is None or wallet is None:
        return make_failure(operation="set_sl", exchange=name, account=account, code="ACCOUNT_NOT_CONFIGURED", message="Account is not configured.")

    existing_sl = protection_state["sl"][0] if protection_state["sl"] else None
    target_oid = None
    if isinstance(existing_sl, dict):
        existing_oid = existing_sl.get("oid")
        if isinstance(existing_oid, int):
            target_oid = existing_oid
    if existing_sl is not None:
        existing_price = _decimal_or_none(existing_sl.get("price"))
        existing_trigger = _decimal_or_none(existing_sl.get("triggerPx") or existing_sl.get("trigger_px"))
        if existing_price == price_value or existing_trigger == price_value:
            verified_context, verification_failure = _verify_position_action("set_sl", account, requested_symbol, "sl", target_oid, price_value, removed=False)
            if verification_failure is not None:
                return verification_failure
            assert verified_context is not None
            return make_success(
                operation="set_sl",
                exchange=name,
                account=account,
                position_action=_position_action_result(
                    operation="set_sl",
                    symbol=symbol,
                    verified=True,
                    price=price_value,
                    removed=False,
                    status="success",
                    exchange_order_id=target_oid,
                    current_side=current_side,
                    current_size=context["current_size"],
                    message="Stop Loss already set.",
                ),
            )

    request_payload = _build_position_trigger_request(context["candidate"], current_position, context["closing_side"], price_value, "sl")
    submitted_oid: Optional[int] = target_oid

    try:
        if existing_sl is not None and target_oid is not None:
            response = exchange_client.bulk_modify_orders_new([{"oid": target_oid, "order": request_payload}])
        else:
            response = exchange_client.bulk_orders([request_payload], grouping="positionTpsl")
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="set_sl",
            exchange=name,
            account=account,
            code="SL_SUBMISSION_FAILED",
            message=sanitize_error_message(str(exc)),
            position_action=_position_action_result(
                operation="set_sl",
                symbol=symbol,
                verified=False,
                price=price_value,
                removed=False,
                status="failed",
                current_side=current_side,
                current_size=context["current_size"],
            ),
        )

    ok, _accepted_count, _rejected_count, oids, _records, error_code, exchange_reason, branch = _order_response_details(response, 1)
    ambiguous_submission = not ok and error_code == "AMBIGUOUS_LADDER_RESPONSE"
    if not ok and not ambiguous_submission and existing_sl is None:
        return make_failure(
            operation="set_sl",
            exchange=name,
            account=account,
            code=error_code or "SL_SUBMISSION_FAILED",
            message="Stop Loss submission failed.",
            exchange_reason=exchange_reason,
            position_action=_position_action_result(
                operation="set_sl",
                symbol=symbol,
                verified=False,
                price=price_value,
                removed=False,
                status="failed",
                current_side=current_side,
                current_size=context["current_size"],
            ),
        )
    if oids:
        submitted_oid = oids[0]

    verified_context, verification_failure, verified_oid = _verify_position_protection_submission(
        "set_sl",
        account,
        requested_symbol,
        "sl",
        price_value,
        submitted_oid,
        pre_order_oids,
        ambiguous_submission,
    )
    if verification_failure is not None:
        if ambiguous_submission:
            return make_failure(
                operation="set_sl",
                exchange=name,
                account=account,
                code="POSITION_ACTION_RESPONSE_AMBIGUOUS",
                message="Stop Loss submission could not be verified.",
                position_action=_position_action_result(
                    operation="set_sl",
                    symbol=symbol,
                    verified=False,
                    price=price_value,
                    removed=False,
                    status="failed",
                    current_side=current_side,
                    current_size=context["current_size"],
                ),
            )
        return verification_failure
    assert verified_context is not None
    if verified_oid is not None:
        submitted_oid = verified_oid
    post_oids = {
        int(order.get("oid"))
        for order in verified_context["open_orders"]
        if isinstance(order.get("oid"), int)
    }
    if not pre_order_oids.difference({target_oid} if target_oid is not None else set()).issubset(post_oids):
        return make_failure(
            operation="set_sl",
            exchange=name,
            account=account,
            code="VERIFICATION_FAILED",
            message="Unrelated orders changed during SL submission.",
            position_action=_position_action_result(
                operation="set_sl",
                symbol=symbol,
                verified=False,
                price=price_value,
                removed=False,
                status="failed",
                current_side=current_side,
                current_size=context["current_size"],
            ),
        )
    return make_success(
        operation="set_sl",
        exchange=name,
        account=account,
        position_action=_position_action_result(
            operation="set_sl",
            symbol=symbol,
            verified=True,
            price=price_value,
            removed=False,
            status="success",
            exchange_order_id=submitted_oid,
            current_side=current_side,
            current_size=context["current_size"],
        ),
    )


@dataclass(frozen=True)
class _CloseResponseVerdict:
    """Evidence-based classification of a single-order market_close response.

    Returned by ``_classify_close_response``. Re-interprets the shared
    ``_order_response_details`` parser output (which was designed for
    multi-order ladder batches) so a single close can never propagate
    ladder-specific error codes like ``AMBIGUOUS_LADDER_RESPONSE``.
    """
    kind: str  # one of: "success", "rejected", "unconfirmed", "malformed"
    code: str
    message: str
    exchange_reason: Optional[str] = None


def _classify_close_response(response: Any) -> _CloseResponseVerdict:
    """Classify a market_close response for the single-order close path.

    Maps the shared parser's branches into evidence-based verdicts:

      ``success``             → ``success`` (the parser saw a filled
                                or resting child result).
      ``top_level_error``     → ``rejected`` (the exchange rejected
                                the entire submission; reason
                                preserved verbatim).
      ``child_error``         → ``rejected`` (the exchange rejected
                                this child; reason preserved).
      ``missing_statuses``,
      ``status_count_mismatch``,
      ``unknown_child``       → ``unconfirmed`` (the submission
                                response did not provide a definitive
                                single-order status). Code
                                ``CLOSE_OUTCOME_UNCONFIRMED``.
                                Evidence-based — does NOT infer IOC
                                cancellation or liquidity failure.
      ``malformed_envelope``  → ``malformed``. Code
                                ``CLOSE_RESPONSE_MALFORMED``.

    The Hyperliquid SDK's ``market_close()`` can also return ``None``
    (implicit) when its internal state lookup finds no matching
    position for the requested coin — see the SDK source. We treat
    that as a distinct, evidence-based outcome rather than folding
    it into ``malformed_envelope``:
      ``response is None``     → ``unconfirmed``. Code
                                ``CLOSE_SUBMISSION_NOOP``. The
                                position will still be re-verified
                                after.

    In every non-success case the close path falls through to
    post-submit position verification; that re-read is authoritative.
    The string ``AMBIGUOUS_LADDER_RESPONSE`` MUST NOT appear here.
    """
    # SDK silently returned None — most often because market_close()
    # found no matching position for the requested coin under the
    # configured account context. Evidence-based: report what we
    # observed, not a guess at root cause.
    if response is None:
        return _CloseResponseVerdict(
            kind="unconfirmed",
            code="CLOSE_SUBMISSION_NOOP",
            message=(
                "Hyperliquid SDK returned no close result. "
                "No matching position was found by the SDK close "
                "operation. The position was re-verified and no "
                "automatic retry was attempted."
            ),
            exchange_reason=None,
        )

    _ok, _accepted, _rejected, _oids, _records, code, reason, branch = (
        _order_response_details(response, 1)
    )
    if branch == "success":
        return _CloseResponseVerdict(
            kind="success",
            code="",
            message="",
        )
    if branch == "top_level_error":
        return _CloseResponseVerdict(
            kind="rejected",
            code=code or "EXCHANGE_REJECTED",
            message=str(reason or "Exchange rejected the close submission."),
            exchange_reason=reason,
        )
    if branch == "child_error":
        return _CloseResponseVerdict(
            kind="rejected",
            code=code or "EXCHANGE_REJECTED",
            message=str(reason or "Exchange rejected this order."),
            exchange_reason=reason,
        )
    if branch in ("missing_statuses", "status_count_mismatch", "unknown_child"):
        # Evidence-based: the parser could not derive a definitive
        # single-order status. Do NOT infer IOC cancellation or
        # liquidity failure — that's an exchange-side fact that must
        # come from the exchange itself.
        return _CloseResponseVerdict(
            kind="unconfirmed",
            code="CLOSE_OUTCOME_UNCONFIRMED",
            message=(
                "Exchange response did not contain a definitive "
                "single-order status. Position will be re-verified."
            ),
            exchange_reason=None,
        )
    # branch == "malformed_envelope" or anything else unexpected.
    return _CloseResponseVerdict(
        kind="malformed",
        code="CLOSE_RESPONSE_MALFORMED",
        message=(
            "Exchange response could not be parsed. "
            "Position will be re-verified."
        ),
        exchange_reason=None,
    )


def _verify_close_position(
    post_positions_response: Any,
    symbol: str,
    original_signed_size: Decimal,
    *,
    tolerance: Decimal = Decimal("1e-9"),
) -> Dict[str, Any]:
    """Authoritative post-submit position verification.

    Compares the re-read position's *signed* size against
    ``original_signed_size`` and returns one of:

      ``flat``                — abs(verify) <= tolerance AND
                                verify direction matches original
                                (residual within tolerance counts as
                                flat for practical purposes; a true
                                zero is preferred).
      ``partial``             — abs(verify) > tolerance AND
                                abs(verify) < abs(original) AND
                                sign unchanged. ``reduced_by`` is
                                positive and meaningful.
      ``unchanged``           — abs(verify - original) <= tolerance.
                                No measurable change.
      ``increased_or_reversed`` — abs(verify) >= abs(original) AND
                                sign unchanged (size grew — likely a
                                competing fill) OR sign flipped.
      ``unknown``             — re-read failed or returned no
                                matching position row.

    Inputs:
      post_positions_response — ``CanonicalResponse`` from
                                ``_execute_positions_orders``.
      symbol                 — upper-cased instrument symbol.
      original_signed_size   — signed size at submission time
                                (positive for LONG, negative for
                                SHORT).
      tolerance              — Decimal tolerance for "equal" and
                                "flat" comparisons (default 1e-9
                                matches Hyperliquid's sz_decimals).

    All comparisons use ``Decimal`` arithmetic — no float subtraction.
    """
    if post_positions_response is None or not getattr(
        post_positions_response, "success", False
    ):
        return {"outcome": "unknown", "verify_signed_size": None,
                "reduced_by": None, "original_signed_size": original_signed_size}

    positions = list(getattr(post_positions_response, "positions", None) or [])
    # Match the re-read position by its full route identifier (e.g.
    # ``xyz:SP500``) OR its dex-stripped alias (e.g. ``SP500``). HIP-3
    # positions surface with the prefixed coin, while ``symbol`` here may be
    # either form depending on what the caller supplied.
    requested_key = _symbol_key(symbol)
    requested_has_dex = ":" in str(symbol)
    verify_position = None
    for position in positions:
        pos_symbol = str(getattr(position, "symbol", ""))
        pos_key = _symbol_key(pos_symbol)
        if pos_key != requested_key and not (":" in pos_symbol and _symbol_key(pos_symbol.split(":", 1)[1]) == requested_key):
            continue
        # If the caller supplied a fully-prefixed route symbol, require the
        # position to carry that same dex prefix (prevents cross-dex collision).
        if requested_has_dex and ":" not in pos_symbol:
            continue
        verify_position = position
        break
    if verify_position is None:
        # Re-read returned no row for this symbol. The agent's normalizer
        # drops zero-size positions, so a missing row means the position
        # leg is genuinely flat. Per the user's contract: "Position is
        # flat → Return success." This applies regardless of original
        # size — the live venue snapshot is the source of truth.
        return {
            "outcome": "flat",
            "verify_signed_size": Decimal("0"),
            "reduced_by": original_signed_size,
            "original_signed_size": original_signed_size,
        }

    abs_size = _decimal_or_none(getattr(verify_position, "size", None))
    side = str(getattr(verify_position, "side", "")).strip().lower()
    if abs_size is None:
        return {"outcome": "unknown", "verify_signed_size": None,
                "reduced_by": None, "original_signed_size": original_signed_size}
    verify_signed = abs_size if side == "long" else -abs_size

    # Sign flip detection (reversal)
    original_sign_is_negative = original_signed_size < 0
    verify_sign_is_negative = verify_signed < 0
    if original_sign_is_negative != verify_sign_is_negative and abs(verify_signed) > tolerance:
        return {
            "outcome": "increased_or_reversed",
            "verify_signed_size": verify_signed,
            "reduced_by": original_signed_size - verify_signed,
            "original_signed_size": original_signed_size,
        }

    if abs(verify_signed) <= tolerance:
        return {
            "outcome": "flat",
            "verify_signed_size": verify_signed,
            "reduced_by": original_signed_size,
            "original_signed_size": original_signed_size,
        }

    abs_original = abs(original_signed_size)
    abs_verify = abs(verify_signed)
    if abs(abs_verify - abs_original) <= tolerance:
        return {
            "outcome": "unchanged",
            "verify_signed_size": verify_signed,
            "reduced_by": Decimal("0"),
            "original_signed_size": original_signed_size,
        }
    if abs_verify < abs_original:
        return {
            "outcome": "partial",
            "verify_signed_size": verify_signed,
            "reduced_by": abs_original - abs_verify,
            "original_signed_size": original_signed_size,
        }
    # abs_verify > abs_original
    return {
        "outcome": "increased_or_reversed",
        "verify_signed_size": verify_signed,
        "reduced_by": original_signed_size - verify_signed,
        "original_signed_size": original_signed_size,
    }


def _execute_close_position(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Single-position close for Hyperliquid.

    Flow:
      1. Build context (current symbol, side, signed size).
      2. Submit ``market_close`` (an IOC reduce-only limit via the SDK).
      3. Classify the response — ladder-specific error codes
         (``AMBIGUOUS_LADDER_RESPONSE``) are re-mapped by
         ``_classify_close_response`` and never reach the wizard.
      4. **Authoritative post-submit verification**: re-read positions
         and compare signed sizes. Outcomes:
           flat              → success
           partial           → CLOSE_PARTIALLY_FILLED (surface before /
                              after / reduced-by; no auto-retry)
           unchanged         → CLOSE_OUTCOME_UNCONFIRMED (or
                              CLOSE_RESPONSE_MALFORMED if response was
                              malformed); surface remaining position
           increased_or_reversed → CLOSE_POSITION_MISMATCH
           unknown (re-read failed) → CLOSE_VERIFICATION_UNAVAILABLE

    The function NEVER auto-retries. AMBIGUOUS_LADDER_RESPONSE is
    NEVER returned.
    """
    requested_symbol = str(request.get("symbol") or "").strip()
    if not requested_symbol:
        return make_failure(operation="close_position", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")

    context, failure = _current_position_management_context("close_position", account, requested_symbol)
    if failure is not None:
        return failure
    assert context is not None

    symbol = str(requested_symbol or "").strip().upper()
    current_position = context["current_position"]
    current_size = context["current_size"]  # positive Decimal (CanonicalPosition.size is stored absolute)
    current_side = str(context["current_side"] or "").strip().lower()
    original_signed_size = current_size if current_side == "long" else -current_size
    exchange_client, wallet, _secret = _build_exchange_client(account)
    if exchange_client is None or wallet is None:
        return make_failure(operation="close_position", exchange=name, account=account, code="ACCOUNT_NOT_CONFIGURED", message="Account is not configured.")

    # --- 1. Submit ---
    try:
        response = exchange_client.market_close(str(context["candidate"].get("route_symbol") or context["candidate"].get("public_symbol") or symbol), sz=float(current_size))
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="CLOSE_POSITION_FAILED",
            message=sanitize_error_message(str(exc)),
            position_action=_position_action_result(
                operation="close_position",
                symbol=symbol,
                verified=False,
                removed=None,
                status="failed",
                current_side=current_side,
                current_size=current_size,
            ),
        )

    # --- 2. Evidence-based response classification (NOT ladder) ---
    verdict = _classify_close_response(response)
    submission_log = (
        f"close_position {symbol}: classify={verdict.kind} "
        f"code={verdict.code!r} reason={verdict.exchange_reason!r}"
    )
    try:
        logging.info(submission_log)
    except Exception:  # noqa: BLE001
        pass

    # Rejected: the exchange told us explicitly. Don't re-verify —
    # surface the exchange reason and the current position size.
    if verdict.kind == "rejected":
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code=verdict.code or "EXCHANGE_REJECTED",
            message=verdict.message,
            exchange_reason=verdict.exchange_reason,
            position_action=_position_action_result(
                operation="close_position",
                symbol=symbol,
                verified=False,
                removed=None,
                status="rejected",
                current_side=current_side,
                current_size=current_size,
                message=verdict.message,
            ),
        )

    # --- 3. Authoritative post-submit verification ---
    # Reached for: success, unconfirmed, malformed. The re-read is
    # the source of truth for whether the close actually took effect.
    post_positions_response = _execute_positions_orders(
        account, {"operation": "positions_orders", "exchange": name, "account": account}
    )
    verification = _verify_close_position(
        post_positions_response, symbol, original_signed_size
    )
    outcome = verification["outcome"]
    verify_signed = verification["verify_signed_size"]
    reduced_by = verification["reduced_by"]

    if outcome == "flat":
        return make_success(
            operation="close_position",
            exchange=name,
            account=account,
            position_action=_position_action_result(
                operation="close_position",
                symbol=symbol,
                verified=True,
                removed=None,
                status="success",
                current_side=current_side,
                current_size=current_size,
                message="Position closed.",
            ),
        )

    if outcome == "partial":
        remaining_size = abs(verify_signed) if verify_signed is not None else None
        msg = (
            f"Position partially closed. Original={original_signed_size}, "
            f"remaining={verify_signed}, reduced_by={reduced_by}. "
            "Not retrying automatically."
        )
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="CLOSE_PARTIALLY_FILLED",
            message=msg,
            position_action=_position_action_result(
                operation="close_position",
                symbol=symbol,
                verified=False,
                removed=None,
                status="partial",
                current_side=current_side,
                current_size=remaining_size,
                message=msg,
            ),
        )

    if outcome == "unchanged":
        # Response was definitive enough not to claim exchange-level
        # rejection, but the position is unchanged.
        code = verdict.code if verdict.kind in ("unconfirmed", "malformed") else "CLOSE_OUTCOME_UNCONFIRMED"
        if verdict.kind == "malformed":
            code = "CLOSE_RESPONSE_MALFORMED"
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code=code,
            message=(
                "Close submission did not change the position. "
                f"Remaining position: {original_signed_size}. "
                "Not retrying automatically."
            ),
            position_action=_position_action_result(
                operation="close_position",
                symbol=symbol,
                verified=False,
                removed=None,
                status="unchanged",
                current_side=current_side,
                current_size=current_size,
            ),
        )

    if outcome == "increased_or_reversed":
        remaining_size = abs(verify_signed) if verify_signed is not None else current_size
        msg = (
            f"Position size unexpectedly changed during close: "
            f"original={original_signed_size}, after={verify_signed}. "
            "Not retrying automatically."
        )
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="CLOSE_POSITION_MISMATCH",
            message=msg,
            position_action=_position_action_result(
                operation="close_position",
                symbol=symbol,
                verified=False,
                removed=None,
                status="mismatch",
                current_side=current_side,
                current_size=remaining_size,
                message=msg,
            ),
        )

    # outcome == "unknown" (re-read failed)
    return make_failure(
        operation="close_position",
        exchange=name,
        account=account,
        code="CLOSE_VERIFICATION_UNAVAILABLE",
        message=(
            "Could not re-read positions after close submission. "
            "Manual verification required."
        ),
        position_action=_position_action_result(
            operation="close_position",
            symbol=symbol,
            verified=False,
            removed=None,
            status="unknown",
            current_side=current_side,
            current_size=current_size,
        ),
    )


def _build_exchange_client(account: str) -> Tuple[Optional[Exchange], Optional[str], Optional[str]]:
    alias = _normalize_account_alias(account)
    if not alias:
        return None, None, None
    trading_account_address, secret = _lookup_credentials(alias)
    if trading_account_address is None or secret is None:
        return None, trading_account_address, secret
    try:
        api_wallet = Account.from_key(secret)
    except Exception:  # noqa: BLE001
        return None, trading_account_address, secret
    # The signing key (`api_wallet`) and the trading account
    # (`trading_account_address`) can differ — Hyperliquid supports API/agent
    # wallets that sign on behalf of a master account. We pass the master
    # account's public address as `account_address` so the SDK's internal
    # state lookups (e.g. `market_close` reading current positions) target
    # the same account the /trade wizard already queries, instead of the
    # signing key's address. The signing key (`api_wallet`) still signs.
    #
    # ``perp_dexs`` must include the native dex ("") AND every HIP-3 perp DEX
    # (e.g. ``xyz``) so the SDK's ``Info`` meta load registers the prefixed
    # coins (``xyz:SP500``, …) for price/asset/size resolution. Without it,
    # market_close/order/TP-SL on a HIP-3 coin would raise KeyError.
    # Use the process-lifetime cache (populated by discovery) — never force a
    # network fetch here so construction stays hermetic in unit tests.
    perp_dex_names = _cached_perp_dex_names()
    return Exchange(
        wallet=api_wallet,
        base_url=_api_base(),
        account_address=trading_account_address,
        perp_dexs=perp_dex_names,
    ), trading_account_address, secret


def _fetch_frontend_open_orders(wallet: str, dex: str = "") -> List[Dict[str, Any]]:
    payload: Dict[str, Any] = {"type": "frontendOpenOrders", "user": wallet}
    if dex:
        payload["dex"] = dex
    return _normalize_open_orders(_post_info(payload))


def _fetch_open_orders_snapshot(wallet: str) -> List[Dict[str, Any]]:
    seen_oids: set[int] = set()
    rows: List[Dict[str, Any]] = []
    dex_names = _fetch_perp_dex_names()
    if "" not in dex_names:
        dex_names = [""] + dex_names
    for dex in dex_names:
        try:
            orders = _fetch_frontend_open_orders(wallet, dex)
        except Exception:  # noqa: BLE001
            continue
        for order in orders:
            oid = order.get("oid")
            if not isinstance(oid, int):
                try:
                    oid = int(oid)
                except Exception:  # noqa: BLE001
                    continue
            if oid in seen_oids:
                continue
            seen_oids.add(oid)
            row = dict(order)
            row["dex"] = dex
            rows.append(row)
    rows.sort(key=lambda item: (item.get("symbol", ""), item.get("side", ""), int(item.get("oid", 0))))
    return rows


def _normalized_ladder_oid(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:  # noqa: BLE001
        return None


def _order_matches_ladder_submission(order: Dict[str, Any], candidate: Dict[str, Any], side: str, request: Dict[str, Any]) -> bool:
    if not _order_matches_resolved_instrument(order, candidate, side):
        return False
    expected_price = _decimal_from_request(request.get("limit_px"))
    expected_size = _decimal_from_request(request.get("sz"))
    actual_price = _decimal_from_request(order.get("limitPx") or order.get("px"))
    actual_size = _decimal_from_request(order.get("sz"))
    if expected_price is None or expected_size is None or actual_price is None or actual_size is None:
        return False
    return actual_price == expected_price and actual_size == expected_size


def _verify_ladder_submission(
    wallet: str,
    candidate: Dict[str, Any],
    side: str,
    order_requests: List[Dict[str, Any]],
    submission_records: List[Dict[str, Any]],
) -> Tuple[bool, int, List[int]]:
    def _pop_order_by_oid(orders: List[Dict[str, Any]], oid: int) -> Optional[Dict[str, Any]]:
        for order_index, order in enumerate(orders):
            if _normalized_ladder_oid(order.get("oid")) == oid:
                return orders.pop(order_index)
        return None

    max_attempts = 3
    verified_count = 0
    matched_oids: List[int] = []
    for attempt in range(max_attempts):
        try:
            open_orders = _fetch_open_orders_snapshot(wallet)
        except Exception:  # noqa: BLE001
            return False, 0, []

        remaining = list(open_orders)
        open_by_oid: Dict[int, Dict[str, Any]] = {}
        for order in remaining:
            oid = _normalized_ladder_oid(order.get("oid"))
            if oid is not None and oid not in open_by_oid:
                open_by_oid[oid] = order

        attempt_verified_count = 0
        attempt_matched_oids: List[int] = []
        attempt_failed = False
        for index, request in enumerate(order_requests):
            record = submission_records[index] if index < len(submission_records) else {}
            kind = str(record.get("kind") or "").strip().lower()
            oid_hint = _normalized_ladder_oid(record.get("oid"))
            if kind == "filled":
                attempt_verified_count += 1
                if oid_hint is not None:
                    attempt_matched_oids.append(oid_hint)
                continue

            matched_order: Optional[Dict[str, Any]] = None
            if oid_hint is not None:
                candidate_order = open_by_oid.get(oid_hint)
                if candidate_order is not None and _order_matches_resolved_instrument(candidate_order, candidate, side):
                    matched_order = _pop_order_by_oid(remaining, oid_hint)
                else:
                    attempt_failed = True
                    break
            else:
                match_index = None
                for candidate_index, order in enumerate(remaining):
                    if _order_matches_ladder_submission(order, candidate, side, request):
                        match_index = candidate_index
                        break
                if match_index is None:
                    attempt_failed = True
                    break
                matched_order = remaining.pop(match_index)

            if matched_order is None:
                attempt_failed = True
                break

            matched_oid = _normalized_ladder_oid(matched_order.get("oid"))
            if matched_oid is not None:
                attempt_matched_oids.append(matched_oid)
            attempt_verified_count += 1

        if not attempt_failed and attempt_verified_count == len(order_requests):
            return True, attempt_verified_count, attempt_matched_oids

        verified_count = attempt_verified_count
        matched_oids = attempt_matched_oids
        if attempt < max_attempts - 1:
            time.sleep(0.2 * (attempt + 1))

    return False, verified_count, matched_oids


def _order_matches_resolved_instrument(order: Dict[str, Any], candidate: Dict[str, Any], side: str) -> bool:
    if _open_order_side(order.get("side")) != side:
        return False
    order_key = _symbol_key(order.get("symbol"))
    candidate_keys = {
        candidate.get("public_key") or "",
        candidate.get("internal_key") or "",
        _symbol_key(candidate.get("public_symbol")),
        _symbol_key(candidate.get("internal_name")),
    }
    candidate_keys = {key for key in candidate_keys if key}
    return order_key in candidate_keys


def _cancel_response_statuses(response: Any, expected_count: int) -> Tuple[bool, int, str]:
    if not isinstance(response, dict):
        return False, 0, "AMBIGUOUS_CANCEL_RESPONSE"
    data = response.get("response")
    if not isinstance(data, dict):
        return False, 0, "AMBIGUOUS_CANCEL_RESPONSE"
    payload = data.get("data")
    if not isinstance(payload, dict):
        return False, 0, "AMBIGUOUS_CANCEL_RESPONSE"
    statuses = payload.get("statuses")
    if not isinstance(statuses, list) or len(statuses) != expected_count:
        return False, 0, "AMBIGUOUS_CANCEL_RESPONSE"
    success_count = 0
    for status in statuses:
        if status == "success":
            success_count += 1
            continue
        if isinstance(status, dict) and status.get("error"):
            continue
        return False, success_count, "AMBIGUOUS_CANCEL_RESPONSE"
    return success_count == expected_count, success_count, ""


def _execute_cancel_order_group(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    alias = _normalize_account_alias(account)
    if not alias:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code="MISSING_ACCOUNT",
            message="Account alias is required.",
        )

    requested_symbol = str(request.get("symbol") or "").strip()
    requested_side = str(request.get("side") or "").strip().lower()
    if not requested_symbol:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    if requested_side not in {"buy", "sell"}:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code="INVALID_SIDE",
            message="Side must be buy or sell.",
        )

    try:
        candidates = _fetch_perp_market_candidates()
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code="INSTRUMENT_RESOLUTION_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )

    candidate, error_code = _resolve_instrument_candidate(requested_symbol, candidates)
    if candidate is None:
        message = "Multiple instruments match this symbol." if error_code == "INSTRUMENT_AMBIGUOUS" else "Instrument not found."
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code=error_code,
            message=message,
        )

    exchange_client, wallet, _secret = _build_exchange_client(account)
    if exchange_client is None or wallet is None:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_CONFIGURED",
            message="Account is not configured.",
        )

    try:
        pre_orders = _fetch_open_orders_snapshot(wallet)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code="OPEN_ORDERS_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )

    target_orders = [order for order in pre_orders if _order_matches_resolved_instrument(order, candidate, requested_side)]
    if not target_orders:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code="NO_TARGET_ORDERS",
            message="No matching orders were found.",
            cancel_group=CanonicalCancelGroupResult(
                symbol=str(candidate.get("public_symbol") or requested_symbol),
                side=requested_side,
                targeted_order_count=0,
                cancelled_order_count=0,
                confirmed_absent_count=0,
                remaining_target_count=0,
                verified=False,
                partial=False,
                status="failed",
                batch_count=0,
            ),
        )

    target_oids: List[int] = []
    for order in target_orders:
        oid = order.get("oid")
        if isinstance(oid, int):
            target_oids.append(oid)
    non_target_oids: List[int] = []
    target_oid_set = set(target_oids)
    for order in pre_orders:
        oid = order.get("oid")
        if isinstance(oid, int) and oid not in target_oid_set:
            non_target_oids.append(oid)

    submitted_batches = 0
    cancelled_count = 0
    partial = False
    status_code = ""
    status_message = ""
    batches: List[Dict[str, Any]] = []

    for start in range(0, len(target_orders), CANCEL_BATCH_SIZE):
        chunk = target_orders[start : start + CANCEL_BATCH_SIZE]
        cancel_requests = [CancelRequest(coin=str(order["symbol"]), oid=int(order["oid"])) for order in chunk]
        try:
            response = exchange_client.bulk_cancel(cancel_requests)
        except Exception as exc:  # noqa: BLE001
            partial = True
            status_code = "CANCEL_FAILED"
            status_message = sanitize_error_message(str(exc))
            batches.append({"submitted": len(chunk), "accepted": 0, "ok": False, "reason": status_code})
            break

        ok, accepted_count, parse_code = _cancel_response_statuses(response, len(cancel_requests))
        submitted_batches += 1
        cancelled_count += accepted_count
        batches.append({"submitted": len(chunk), "accepted": accepted_count, "ok": ok})
        if not ok:
            partial = True
            status_code = parse_code or "CANCEL_REJECTED"
            status_message = "Cancellation was rejected or ambiguous."
            break

    try:
        post_orders = _fetch_open_orders_snapshot(wallet)
    except Exception as exc:  # noqa: BLE001
        post_orders = []
        partial = True
        if not status_code:
            status_code = "VERIFY_UNAVAILABLE"
            status_message = sanitize_error_message(str(exc))

    post_oids: set[int] = set()
    for order in post_orders:
        oid = order.get("oid")
        if isinstance(oid, int):
            post_oids.add(oid)
    remaining_target_count = sum(1 for oid in target_oids if oid in post_oids)
    confirmed_absent_count = len(target_oids) - remaining_target_count
    non_target_preserved = all(oid in post_oids for oid in non_target_oids)
    verified = remaining_target_count == 0 and non_target_preserved and cancelled_count == len(target_oids) and not partial

    cancel_result = CanonicalCancelGroupResult(
        symbol=str(candidate.get("public_symbol") or requested_symbol),
        side=requested_side,
        targeted_order_count=len(target_oids),
        cancelled_order_count=cancelled_count,
        confirmed_absent_count=confirmed_absent_count,
        remaining_target_count=remaining_target_count,
        verified=verified,
        partial=partial or not verified,
        status="success" if verified else ("partial" if cancelled_count else "failed"),
        batch_count=submitted_batches,
        batches=batches or None,
    )

    if verified:
        return make_success(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            cancel_group=cancel_result,
        )

    error_code = status_code or ("VERIFICATION_FAILED" if cancelled_count else "NO_TARGET_ORDERS")
    error_message = status_message or "Cancellation was only partially completed."
    return make_failure(
        operation="cancel_order_group",
        exchange=name,
        account=account,
        code=error_code,
        message=error_message,
        cancel_group=cancel_result,
    )


def _api_base() -> str:
    """Return the Hyperliquid API base URL, allowing override."""
    override = _read_env("HYPERLIQUID_API_URL").rstrip("/")
    return override or DEFAULT_API_BASE


def _info_url() -> str:
    base = _api_base()
    if base.endswith("/info"):
        return base
    return f"{base}/info"


def _post_info(payload: Dict[str, Any], timeout: int = API_TIMEOUT_SECONDS) -> Any:
    """POST a JSON payload to the Hyperliquid /info endpoint with retries."""
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "HermesAgent/TradePlugin/1.0",
    }

    url = _info_url()
    last_exc: Optional[BaseException] = None
    for attempt in range(MAX_RETRIES + 1):
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.load(response)
            return body
        except urllib.error.HTTPError as exc:
            # 429 rate limited — retry with backoff.
            if exc.code == 429 and attempt < MAX_RETRIES:
                time.sleep(0.5 * (attempt + 1))
                last_exc = exc
                continue
            raise RuntimeError(f"Hyperliquid HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("Hyperliquid connection error") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("Hyperliquid response was not valid JSON") from exc

    # Should not reach here, but be defensive.
    if last_exc is not None:
        raise RuntimeError("Hyperliquid request failed after retries") from last_exc
    raise RuntimeError("Hyperliquid request failed")


def _normalize_account_alias(account: str) -> str:
    """Normalize the user-supplied account alias to the discovery format."""
    return (account or "").strip().upper()


def _extract_usdc_balance(payload: Any) -> Optional[Decimal]:
    """Find the USDC row in a spotClearinghouseState payload and return
    its total as a Decimal.

    The payload shape is::

        {"balances": [{"coin": "USDC", "hold": "...", "total": "..."}, ...]}

    We prefer the ``total`` field (free + held). If the payload is
    malformed, or there's no USDC row, return None.
    """
    if not isinstance(payload, dict):
        return None
    balances = payload.get("balances")
    if not isinstance(balances, list):
        return None
    for entry in balances:
        if not isinstance(entry, dict):
            continue
        coin = entry.get("coin") or entry.get("token")
        if not isinstance(coin, str):
            continue
        if coin.strip().upper() != "USDC":
            continue
        total_raw = entry.get("total")
        if total_raw is None or total_raw == "":
            continue
        try:
            total = Decimal(str(total_raw))
        except Exception:  # noqa: BLE001
            return None
        return total
    return None


def _execute_balance(account: str) -> CanonicalResponse:
    """Run the read-only balance flow for ``account``."""
    alias = _normalize_account_alias(account)
    if not alias:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="MISSING_ACCOUNT",
            message="Account alias is required.",
        )

    wallet, _secret = _lookup_credentials(alias)
    if wallet is None:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_CONFIGURED",
            message="Account is not configured.",
        )

    # Read-only call: only the public wallet address is transmitted.
    payload = {"type": "spotClearinghouseState", "user": wallet}
    try:
        response = _post_info(payload)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="BALANCE_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )

    total = _extract_usdc_balance(response)
    if total is None:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="BALANCE_UNAVAILABLE",
            message="Balance unavailable.",
        )

    balance: CanonicalBalance = normalize_balance(total, "USDC")
    return make_success(
        operation="balance",
        exchange=name,
        account=account,
        balance=balance,
    )
