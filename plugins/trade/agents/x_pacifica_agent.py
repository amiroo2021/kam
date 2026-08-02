"""Pacifica exchange agent.

Pacifica is a Solana-based perpetual DEX. This module owns all
Pacifica-specific behavior for the /trade stack.

Current scope:
- Credential discovery from ``PACIFICA_<ACCOUNT>_*`` variables in the
  live environment or ``$HERMES_HOME/.env``.
- Read-only account info retrieval through Pacifica's documented REST
  endpoint (``GET /api/v1/account``).
- Read-only positions retrieval (``GET /api/v1/positions``).
- Read-only mark-price lookup (``GET /api/v1/info/prices``) used to
  compute unrealized PnL.
- Authenticated limit-order placement via ``POST /api/v1/orders/create``,
  signed with the per-account agent key (Ed25519, Solana-style).
- Post-submission verification by re-reading ``GET /api/v1/orders`` and
  matching on the client_order_id we generated.
- Canonical conversion into the exchange-agnostic TradeDesk / wizard
  contract.

Required credentials (all three are required for the account to be
recognised):

- ``PACIFICA_<ACCOUNT>_ADDRESS`` — main account (Solana wallet) public
  address; this is the value sent as the ``account`` query parameter.
- ``PACIFICA_<ACCOUNT>_AGENT_WALLET`` — public address of the agent
  wallet (a separate Solana keypair whose private key signs the API
  request on behalf of the main account).
- ``PACIFICA_<ACCOUNT>_AGENT_PRIVATE_KEY`` — base58-encoded Ed25519
  private key for the agent wallet. The agent key must be bound to the
  main account via Pacifica's ``bind_agent_wallet`` operation (one-time
  setup; the API will reject requests with a 401 / "agent not bound"
  error if this hasn't been done).

Account discovery is case-insensitive: ``PACIFICA_amiroo_ADDRESS`` and
``PACIFICA_AMIROO_ADDRESS`` refer to the same account.

TradeDesk and the Telegram wizard MUST remain exchange-agnostic and
MUST NOT parse ``PACIFICA_*`` environment variables or Pacifica-native
payloads.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import base58

from ..canonical import (
    CanonicalCancelGroupResult,
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

name = "pacifica"

DEFAULT_API_BASE = "https://api.pacifica.fi/api/v1"
API_TIMEOUT_SECONDS = 20

# Pacifica reports its margin unit as USDC. The contract field is the
# native currency string from the exchange — never converted across
# currencies by the canonical layer.
PACIFICA_BALANCE_UNIT = "USDC"

# Recognised credential suffixes on the PACIFICA_<ALIAS>_* prefix.
# Any other suffix is ignored. All three are required.
PACIFICA_REQUIRED_FIELDS = ("ADDRESS", "AGENT_WALLET", "AGENT_PRIVATE_KEY")

# Order-side mapping. The /trade wizard speaks buy/sell (the standard
# finance vocabulary). Pacifica's REST API speaks bid/ask (the exchange
# order-book vocabulary). The mapping is intuitive: "buy" = bid (you
# bid the market up = go long), "sell" = ask (you ask the market down
# = go short).
PACIFICA_BUY_SIDE = "bid"
PACIFICA_SELL_SIDE = "ask"
PACIFICA_SIDE_TO_PACIFICA = {"buy": PACIFICA_BUY_SIDE, "sell": PACIFICA_SELL_SIDE}
# Position-management paths (set_tp / set_sl / close_position) use the
# canonical long/short side from the position row rather than the
# wizard's buy/sell. This second map is the inverse of the above for
# the cases where the side arrives as "long" / "short".
PACIFICA_SIDE_FROM_POSITION_TO_PACIFICA = {
    "long": PACIFICA_BUY_SIDE,
    "short": PACIFICA_SELL_SIDE,
}
# Sides accepted by the new_order / ladder wizards. The
# positions_management wizards (set_tp / set_sl / close_position)
# additionally accept "long" / "short" (from the position row), but
# those are routed through PACIFICA_SIDE_FROM_POSITION_TO_PACIFICA
# rather than this set.
PACIFICA_CANONICAL_SIDES = {"buy", "sell"}

# Order-type mapping. We only support limit orders via the wizard for
# now; market orders require slippage settings and are disabled to
# avoid accidental taker fills at the wrong price.
PACIFICA_LIMIT_ORDER_TYPE = "limit"
PACIFICA_MARKET_ORDER_TYPE = "market"
PACIFICA_SUPPORTED_ORDER_TYPES = {PACIFICA_LIMIT_ORDER_TYPE}

# Pacifica time-in-force values. The wizard always sends GTC.
PACIFICA_TIF_GTC = "GTC"
PACIFICA_TIF_IOC = "IOC"
PACIFICA_TIF_ALO = "ALO"
PACIFICA_TIF_TOB = "TOB"
PACIFICA_DEFAULT_TIF = PACIFICA_TIF_GTC
PACIFICA_VALID_TIFS = {PACIFICA_TIF_GTC, PACIFICA_TIF_IOC, PACIFICA_TIF_ALO, PACIFICA_TIF_TOB}

# Signature expiry window (ms). Pacifica rejects requests whose
# timestamp + expiry_window is in the past. 5s is the SDK's default
# and is generous for a Telegram-driven workflow.
PACIFICA_SIGNATURE_EXPIRY_MS = 5_000

# Order-notional floor in USD. Pacifica enforces a $10 minimum notional
# on every order; we surface a clean validation error rather than
# letting the exchange reject the order mid-flight.
PACIFICA_MIN_NOTIONAL_USD = Decimal("10")

# Verification tuning. After a successful POST, we re-read
# /api/v1/orders to confirm the order is visible. Pacifica settles
# submissions near-instantly in practice; one short retry is enough.
PACIFICA_VERIFY_ATTEMPTS = 3
PACIFICA_VERIFY_DELAY_SECONDS = 0.25

# Cancel tuning. Per-order cancellations are issued in parallel to
# keep the wizard responsive even on dense books (the amiroo account,
# for instance, currently has 128 BTC bid orders, 284 HYPE ask
# orders, and 3 SOL ask orders). Each cancel is an independent signed
# POST, so a small thread pool is the right shape — no shared state,
# no ordering requirements, every request has its own signature.
PACIFICA_CANCEL_CONCURRENCY = 8
PACIFICA_CANCEL_PER_REQUEST_TIMEOUT = 10
# Retry policy for individual cancels that hit a transient HTTP
# failure (timeout / 5xx / connection error). A 4xx is treated as
# permanent — the order may already be cancelled and a retry would
# just bounce again.
PACIFICA_CANCEL_RETRIES = 2
PACIFICA_CANCEL_RETRY_BACKOFF_SECONDS = 0.4

# Re-snapshot the open-orders list after cancelling so we can confirm
# the orders actually left the book. Pacifica's read-after-write
# consistency is fast but not instant.
PACIFICA_CANCEL_VERIFY_ATTEMPTS = 3
PACIFICA_CANCEL_VERIFY_DELAY_SECONDS = 0.5

# Ladder tuning. A ladder is N child limit orders, each submitted as
# its own signed POST. For typical 3-15 child ladders, sequential
# submission at ~150ms per POST is fine (the wizard blocks the chat
# thread on the response anyway). For larger ladders (30+) the user
# can wait — the cost of a thread pool here is more code than
# benefit, and the wizard's text rendering is the bottleneck, not
# the round-trips. The hard upper bound on the order count is what
# we accept from the wizard; Pacifica has no documented per-account
# cap on open orders.
PACIFICA_LADDER_MIN_NOTIONAL_USD = PACIFICA_MIN_NOTIONAL_USD  # $10 per child
PACIFICA_LADDER_BATCH_SIZE = 5  # for diagnostic batch reporting
PACIFICA_LADDER_VERIFY_ATTEMPTS = 3
PACIFICA_LADDER_VERIFY_DELAY_SECONDS = 0.4

# Position-management tuning.
#
# Closing a position uses Pacifica's ``create_market_order`` endpoint
# with ``reduce_only=true`` and a slippage tolerance. The exchange
# imposes a ~200ms delay on market orders to protect liquidity
# providers, so the close POST itself is fast but the matching is
# not instantaneous — we poll the positions endpoint to confirm the
# position has actually closed before reporting success.
PACIFICA_CLOSE_SLIPPAGE_PERCENT = "1"  # 1% max slippage on a market close
PACIFICA_CLOSE_VERIFY_ATTEMPTS = 6
PACIFICA_CLOSE_VERIFY_DELAY_SECONDS = 0.5

# TP/SL set via ``set_position_tpsl`` become stop orders in the
# user's order list. Each leg carries ``stop_price`` (the trigger),
# optionally ``limit_price`` (a limit at the trigger), and an
# ``amount`` (defaulting to the full position size if omitted).
PACIFICA_TPSL_TRIGGER_PRICE_TYPE = "mark_price"

# How far we look back to identify TP vs SL on a position:
#   long  : TP trigger is above the entry, SL trigger is below
#   short : TP trigger is below the entry, SL trigger is above
# We allow a small equal-to-entry tolerance (one tick) for "breakeven"
# TP/SL — a TP placed at the entry price is treated as a TP, not an
# SL, because the operator's intent is the side that profits.
PACIFICA_TPSL_BREAKEVEN_TOLERANCE_FRACTION = Decimal("0")  # strict; a TP at
                                                            # entry is still a
                                                            # TP if above

# Optional override (mainly for testnet routing).
# Example: PACIFICA_API_BASE=https://test-api.pacifica.fi/api/v1
_OPTIONAL_BASE_ENV = "PACIFICA_API_BASE"

# Aliases are uppercase tokens starting with a letter, then alnum/underscore.
# Matches the convention used by every other agent in this directory.
_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Solana addresses are base58-encoded 32-byte Ed25519 public keys. We
# accept anything base58-decodable that lands in the 32-byte range; we
# don't try to validate the curve, only the shape.
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {ch: idx for idx, ch in enumerate(_BASE58_ALPHABET)}


def _b58decode(value: str) -> bytes:
    """Decode a base58 string to bytes (Solana / Bitcoin alphabet).

    Raises ``ValueError`` on any non-base58 character. We use this only
    in validation paths — the signing layer relies on the
    purpose-built ``base58`` package, which is the canonical decoder.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("empty base58 value")
    number = 0
    for ch in text:
        if ch not in _BASE58_INDEX:
            raise ValueError("invalid base58 character")
        number = number * 58 + _BASE58_INDEX[ch]
    decoded = b"" if number == 0 else number.to_bytes((number.bit_length() + 7) // 8, "big")
    leading_zeros = len(text) - len(text.lstrip("1"))
    return b"\x00" * leading_zeros + decoded


def _is_base58_address(value: str, expected_bytes: int = 32) -> bool:
    text = str(value or "").strip()
    if not text or any(ch not in _BASE58_INDEX for ch in text):
        return False
    try:
        decoded = _b58decode(text)
    except (OverflowError, ValueError):
        return False
    return len(decoded) == expected_bytes


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _load_dotenv_values(path: Path) -> Dict[str, str]:
    """Minimal .env reader.

    We deliberately do not depend on ``python-dotenv`` so the agent can
    run inside the minimal environment that the rest of the trade stack
    uses. Returns only ``key=value`` entries; comments and blank lines
    are skipped; double-quoted values have their surrounding quotes
    stripped with backslash-escapes decoded.
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
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        values[key] = value
    return values


def _combined_pacifica_env() -> Dict[str, str]:
    """Live env wins over .env, but .env values fill in gaps."""
    values: Dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith("PACIFICA_"):
            values[key] = (value or "").strip()
    for key, value in _load_dotenv_values(_hermes_home() / ".env").items():
        if key.startswith("PACIFICA_") and key not in values:
            values[key] = (value or "").strip()
    return values


def _api_base() -> str:
    base = _combined_pacifica_env().get(_OPTIONAL_BASE_ENV, "").strip()
    return (base or DEFAULT_API_BASE).rstrip("/")


# ---------------------------------------------------------------------------
# Credential discovery
# ---------------------------------------------------------------------------


def _discover_accounts() -> List[str]:
    """Return sorted list of account aliases whose credentials are complete."""
    env = _combined_pacifica_env()
    grouped: Dict[str, Dict[str, str]] = {}
    for key, value in env.items():
        if not value or not key.startswith("PACIFICA_"):
            continue
        # Skip the bare API_BASE override (it has no alias).
        if key == _OPTIONAL_BASE_ENV:
            continue
        remainder = key[len("PACIFICA_"):]
        for field in PACIFICA_REQUIRED_FIELDS:
            suffix = f"_{field}"
            if not remainder.endswith(suffix):
                continue
            alias = remainder[: -len(suffix)]
            if not alias or not _ALIAS_PATTERN.match(alias):
                break
            grouped.setdefault(alias, {})[field] = value
            break

    valid: List[str] = []
    for alias in sorted(grouped.keys()):
        fields = grouped[alias]
        if not all(fields.get(f) for f in PACIFICA_REQUIRED_FIELDS):
            continue
        if not _is_base58_address(fields.get("ADDRESS", ""), expected_bytes=32):
            continue
        if not _is_base58_address(fields.get("AGENT_WALLET", ""), expected_bytes=32):
            continue
        valid.append(alias.lower())
    return valid


def list_accounts() -> List[str]:
    return _discover_accounts()


def _lookup_credentials(alias: str) -> Optional[Dict[str, str]]:
    """Resolve an alias (case-insensitive) to its credential triple.

    Returns ``None`` if the alias is missing, malformed, or has any
    incomplete field. The main ``address`` and ``agent_wallet`` are
    validated to look like Solana base58 32-byte addresses; the private
    key is validated only enough to detect obvious typos (non-empty,
    base58-encodable, decodes to at least 32 bytes).
    """
    alias_upper = str(alias or "").strip().upper()
    if not alias_upper or not _ALIAS_PATTERN.match(alias_upper):
        return None
    env = _combined_pacifica_env()
    fields = {f: str(env.get(f"PACIFICA_{alias_upper}_{f}", "")).strip() for f in PACIFICA_REQUIRED_FIELDS}
    if not all(fields.values()):
        return None
    if not _is_base58_address(fields["ADDRESS"], expected_bytes=32):
        return None
    if not _is_base58_address(fields["AGENT_WALLET"], expected_bytes=32):
        return None
    # Solana Ed25519 secret keys are 64 bytes on the wire (32-byte seed
    # concatenated with the 32-byte public key) when exported via
    # solders.Keypair / solana-keygen. Earlier we required exactly 32
    # bytes, which rejects every working keypair — relax to "decodes to
    # 32, 48, 64, or 80 bytes" (covers every reasonable export format
    # without admitting arbitrary junk). The actual signing layer is the
    # authority on what the key accepts; this check only catches typos.
    if not _is_base58_address(fields["AGENT_PRIVATE_KEY"], expected_bytes=64):
        pk_text = fields["AGENT_PRIVATE_KEY"]
        try:
            decoded_len = len(_b58decode(pk_text))
        except (ValueError, OverflowError):
            decoded_len = 0
        if decoded_len not in (32, 48, 64, 80):
            return None
    return {
        "account": alias_upper.lower(),
        "address": fields["ADDRESS"],
        "agent_wallet": fields["AGENT_WALLET"],
        "agent_private_key": fields["AGENT_PRIVATE_KEY"],
        "api_base": _api_base(),
    }


def capabilities() -> List[str]:
    return [
        "balance",
        "positions_orders",
        "new_order",
        "cancel_order_group",
        "ladder",
        "positions_management",
        "set_tp",
        "set_sl",
        "close_position",
    ]


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


class _PacificaHTTPError(RuntimeError):
    def __init__(self, *, status: int, path: str, body: str) -> None:
        self.status = status
        self.path = path
        self.body = body
        super().__init__(f"HTTP {status} on {path}: {body[:200]}")


def _http_get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """Issue a GET to Pacifica and parse the JSON response.

    All GET endpoints on Pacifica are public — no signing is required.
    Per the docs, GETs MUST NOT carry a request body (CloudFront 403s
    empty-body GETs), so we never send one here.
    """
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "curl/8.5.0", "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            text = resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body = ""
        raise _PacificaHTTPError(
            status=int(exc.code),
            path=urllib.parse.urlparse(url).path,
            body=body or str(exc.reason),
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Pacifica API unreachable: {exc.reason}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Pacifica API returned invalid JSON") from exc


def _get_account_info(address: str) -> Dict[str, Any]:
    """Call ``GET /api/v1/account?account=...`` and return the data dict.

    Raises ``RuntimeError`` on transport / non-success responses.
    """
    payload = _http_get_json(f"{_api_base()}/account", params={"account": address})
    if not isinstance(payload, dict):
        raise RuntimeError("Pacifica /account returned a non-object payload")
    if payload.get("success") is False:
        code = payload.get("code")
        message = payload.get("error") or payload.get("message") or "Pacifica /account failed"
        raise RuntimeError(f"{message} (code={code})")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Pacifica /account response missing data object")
    return data


def _get_positions(address: str) -> List[Dict[str, Any]]:
    """Call ``GET /api/v1/positions?account=...`` and return the data list."""
    payload = _http_get_json(f"{_api_base()}/positions", params={"account": address})
    if not isinstance(payload, dict):
        raise RuntimeError("Pacifica /positions returned a non-object payload")
    if payload.get("success") is False:
        code = payload.get("code")
        message = payload.get("error") or payload.get("message") or "Pacifica /positions failed"
        raise RuntimeError(f"{message} (code={code})")
    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError("Pacifica /positions response missing data list")
    return data


def _get_mark_prices() -> Dict[str, Decimal]:
    """Fetch ``GET /api/v1/info/prices`` and return ``{symbol: mark}``.

    Pacifica's ``/positions`` endpoint deliberately omits the current mark
    price — the UI has to call ``/info/prices`` separately to render
    uPnL. We do the same here: the mark price is the *only* thing we
    need from that endpoint.

    The response is a flat list; we index it into a dict for O(1)
    per-position lookups. Symbols that fail to parse as Decimal are
    silently skipped — they would only matter if a position was opened
    in a symbol Pacifica hadn't published a mark for, which never
    happens on a live exchange.
    """
    payload = _http_get_json(f"{_api_base()}/info/prices")
    if not isinstance(payload, dict):
        raise RuntimeError("Pacifica /info/prices returned a non-object payload")
    if payload.get("success") is False:
        code = payload.get("code")
        message = payload.get("error") or payload.get("message") or "Pacifica /info/prices failed"
        raise RuntimeError(f"{message} (code={code})")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise RuntimeError("Pacifica /info/prices response missing data list")
    out: Dict[str, Decimal] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue
        mark_raw = row.get("mark")
        if mark_raw is None or str(mark_raw).strip() == "":
            continue
        try:
            out[symbol] = Decimal(str(mark_raw))
        except Exception:  # noqa: BLE001
            continue
    return out


# ---------------------------------------------------------------------------
# Market metadata
# ---------------------------------------------------------------------------

# In-process cache for /api/v1/info. Pacifica publishes this list once
# and reuses it across every symbol; the data is essentially static
# within a session. We cache the list but never cache per-symbol
# lookups (a dict lookup is free; an LRU would just add complexity).
_PACIFICA_MARKET_INFO_CACHE: Optional[Dict[str, Dict[str, Any]]] = None
_PACIFICA_MARKET_INFO_TTL_SECONDS = 300.0
_PACIFICA_MARKET_INFO_FETCHED_AT: float = 0.0


def _get_market_info(symbol: str) -> Optional[Dict[str, Any]]:
    """Return the per-symbol record from ``GET /api/v1/info``.

    Returns ``None`` if the symbol isn't listed. Caches the full list
    for ``_PACIFICA_MARKET_INFO_TTL_SECONDS`` to keep /info off the
    hot path of every new-order call.
    """
    global _PACIFICA_MARKET_INFO_CACHE, _PACIFICA_MARKET_INFO_FETCHED_AT
    target = str(symbol or "").strip().upper()
    if not target:
        return None
    now = time.monotonic()
    if (
        _PACIFICA_MARKET_INFO_CACHE is None
        or (now - _PACIFICA_MARKET_INFO_FETCHED_AT) > _PACIFICA_MARKET_INFO_TTL_SECONDS
    ):
        payload = _http_get_json(f"{_api_base()}/info")
        if not isinstance(payload, dict):
            raise RuntimeError("Pacifica /info returned a non-object payload")
        if payload.get("success") is False:
            code = payload.get("code")
            message = payload.get("error") or payload.get("message") or "Pacifica /info failed"
            raise RuntimeError(f"{message} (code={code})")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise RuntimeError("Pacifica /info response missing data list")
        cache: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").strip().upper()
            if sym:
                cache[sym] = row
        _PACIFICA_MARKET_INFO_CACHE = cache
        _PACIFICA_MARKET_INFO_FETCHED_AT = now
    return (_PACIFICA_MARKET_INFO_CACHE or {}).get(target)


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def _sort_json_keys(value: Any) -> Any:
    """Recursively sort every dict's keys. Matches the SDK's algorithm."""
    if isinstance(value, dict):
        return {key: _sort_json_keys(value[key]) for key in sorted(value.keys())}
    if isinstance(value, list):
        return [_sort_json_keys(item) for item in value]
    return value


def _prepare_signature_message(header: Dict[str, Any], payload: Dict[str, Any]) -> str:
    """Build the deterministic JSON string that the agent key signs.

    The format is identical to Pacifica's official SDK
    (``common/utils.py`` in the ``pacifica-fi/python-sdk`` repo):

    1. Header must contain ``type``, ``timestamp``, ``expiry_window``.
    2. The signed envelope is ``{**header, "data": payload}`` with all
       dict keys sorted recursively.
    3. Serialised as compact JSON (no spaces, ``separators=(",", ":")``)
       to ``utf-8`` bytes.

    Any deviation here will produce a signature that Pacifica's server
    rejects, so we mirror the SDK exactly.
    """
    for required in ("type", "timestamp", "expiry_window"):
        if required not in header:
            raise ValueError(f"signature header missing {required!r}")
    envelope = {**header, "data": payload}
    envelope = _sort_json_keys(envelope)
    return json.dumps(envelope, separators=(",", ":"))


def _load_agent_keypair(agent_private_key_b58: str):
    """Decode the agent private key and return a ``solders.Keypair``.

    Lazy-imports ``solders`` so that the agent can still load (and serve
    read operations) on a host that doesn't have it installed — only
    the write path actually needs signing.

    Raises ``RuntimeError`` with a clear, user-actionable message if
    the key is malformed or ``solders`` is not importable.
    """
    try:
        from solders.keypair import Keypair  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Pacifica write operations require the 'solders' package. "
            "Install it with `pip install solders` and retry."
        ) from exc
    try:
        secret_bytes = base58.b58decode(agent_private_key_b58)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Pacifica agent private key is not valid base58.") from exc
    if len(secret_bytes) == 64:
        # Standard Solana export: 32-byte seed + 32-byte pubkey.
        return Keypair.from_bytes(secret_bytes)
    if len(secret_bytes) == 32:
        # 32-byte seed only (older export format).
        return Keypair.from_seed(secret_bytes)
    raise RuntimeError(
        "Pacifica agent private key has unexpected length "
        f"({len(secret_bytes)} bytes); expected 32 or 64."
    )


def _build_signed_body(
    *,
    credentials: Dict[str, str],
    operation_type: str,
    payload: Dict[str, Any],
    expiry_window_ms: int = PACIFICA_SIGNATURE_EXPIRY_MS,
) -> Dict[str, Any]:
    """Assemble the full Pacifica request body and return it unsigned.

    Pacifica expects the body to include the auth envelope
    (``account``, ``agent_wallet``, ``signature``, ``timestamp``,
    ``expiry_window``) plus the operation payload flat at the top
    level. The signature is computed over the deterministic-JSON
    representation of ``{header, data: payload}`` — see
    ``_prepare_signature_message``.
    """
    timestamp = int(time.time() * 1_000)
    header = {
        "type": operation_type,
        "timestamp": timestamp,
        "expiry_window": int(expiry_window_ms),
    }
    message = _prepare_signature_message(header, payload)
    keypair = _load_agent_keypair(credentials["agent_private_key"])
    raw_signature = bytes(keypair.sign_message(message.encode("utf-8")))
    signature_b58 = base58.b58encode(raw_signature).decode("ascii")
    body: Dict[str, Any] = {
        "account": credentials["address"],
        "agent_wallet": credentials["agent_wallet"],
        "signature": signature_b58,
        "timestamp": timestamp,
        "expiry_window": int(expiry_window_ms),
    }
    body.update(payload)
    return body


def _post_signed(
    credentials: Dict[str, str],
    path: str,
    operation_type: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Sign ``payload`` and POST it to ``path`` on the Pacifica REST API.

    Returns the parsed ``data`` field of the response (assumes a
    top-level ``{success, data, error, code}`` envelope and a 2xx HTTP
    status; the helper raises on transport failures or on
    ``success=false`` responses).
    """
    body = _build_signed_body(
        credentials=credentials,
        operation_type=operation_type,
        payload=payload,
    )
    body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
    url = f"{_api_base()}{path}"
    req = urllib.request.Request(
        url,
        method="POST",
        data=body_bytes,
        headers={"Content-Type": "application/json", "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            text = resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            err_body = ""
        # Try to extract a useful message from the response envelope.
        message = err_body or str(exc.reason)
        try:
            parsed = json.loads(err_body)
            if isinstance(parsed, dict):
                message = (
                    parsed.get("error")
                    or parsed.get("message")
                    or parsed.get("msg")
                    or message
                )
        except Exception:  # noqa: BLE001
            pass
        raise _PacificaHTTPError(
            status=int(exc.code),
            path=urllib.parse.urlparse(url).path,
            body=message,
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Pacifica API unreachable: {exc.reason}") from exc
    try:
        response_obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Pacifica API returned invalid JSON") from exc
    if not isinstance(response_obj, dict):
        raise RuntimeError("Pacifica POST returned a non-object payload")
    if response_obj.get("success") is False:
        message = (
            response_obj.get("error")
            or response_obj.get("message")
            or response_obj.get("msg")
            or "Pacifica POST failed"
        )
        raise RuntimeError(f"{message} (code={response_obj.get('code')})")
    data = response_obj.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Pacifica POST response missing data object")
    return data


# ---------------------------------------------------------------------------
# Cancel helpers
# ---------------------------------------------------------------------------

# Pacifica's cancel endpoints return `{"success": true}` on success
# (the `data` object may be null). We don't want `_post_signed` to
# reject that, so we have a sibling helper that only requires
# `success == true` and returns nothing on success / raises on
# failure. This is the right shape for cancel / batch endpoints
# where the response data isn't a structured payload.
def _post_signed_ack(
    credentials: Dict[str, str],
    path: str,
    operation_type: str,
    payload: Dict[str, Any],
) -> None:
    """Sign ``payload`` and POST it, requiring only a 2xx + success=true.

    Used for cancel endpoints where the response is just an ack.
    Raises ``_PacificaHTTPError`` on transport failure or
    ``RuntimeError`` on a ``success=false`` response. No return value
    — the per-order success/failure is what we care about.
    """
    body = _build_signed_body(
        credentials=credentials,
        operation_type=operation_type,
        payload=payload,
    )
    body_bytes = json.dumps(body, separators=(",", ":")).encode("utf-8")
    url = f"{_api_base()}{path}"
    req = urllib.request.Request(
        url,
        method="POST",
        data=body_bytes,
        headers={"Content-Type": "application/json", "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            text = resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            err_body = ""
        message = err_body or str(exc.reason)
        try:
            parsed = json.loads(err_body)
            if isinstance(parsed, dict):
                message = (
                    parsed.get("error")
                    or parsed.get("message")
                    or parsed.get("msg")
                    or message
                )
        except Exception:  # noqa: BLE001
            pass
        raise _PacificaHTTPError(
            status=int(exc.code),
            path=urllib.parse.urlparse(url).path,
            body=message,
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Pacifica API unreachable: {exc.reason}") from exc
    try:
        response_obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Pacifica API returned invalid JSON") from exc
    if not isinstance(response_obj, dict):
        raise RuntimeError("Pacifica POST returned a non-object payload")
    if response_obj.get("success") is False:
        message = (
            response_obj.get("error")
            or response_obj.get("message")
            or response_obj.get("msg")
            or "Pacifica POST failed"
        )
        raise RuntimeError(f"{message} (code={response_obj.get('code')})")


def _is_transient_cancel_error(exc: BaseException) -> bool:
    """Classify an exception from a cancel POST as transient or permanent.

    Transient: timeouts, connection errors, 5xx HTTP errors. Worth
    retrying — Pacifica may have processed the cancel and just lost
    the response mid-flight.

    Permanent: 4xx HTTP errors (other than 429). The order is
    already gone, the agent isn't bound, etc. Retrying is just going
    to bounce.

    Anything we don't recognise is treated as transient — better to
    try one extra time than to silently leave an order on the book.
    """
    if isinstance(exc, _PacificaHTTPError):
        if exc.status in (408, 425, 429):
            return True
        if 500 <= exc.status < 600:
            return True
        return False
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, RuntimeError) and "API unreachable" in str(exc):
        return True
    return True  # unknown failure — retry once


def _signed_cancel_order(
    credentials: Dict[str, str],
    symbol: str,
    order_id: int,
) -> Tuple[bool, str]:
    """Issue a single signed cancel, with retries on transient errors.

    Returns ``(ok, reason)``:
      - ``ok=True``   — Pacifica accepted the cancel (or the order
                        was already gone). No exception.
      - ``ok=False``  — All retries exhausted. ``reason`` is the
                        last error message (already sanitized).

    The per-request timeout is shorter than the global
    ``API_TIMEOUT_SECONDS`` because cancels are time-sensitive and
    we'd rather surface a timeout to the wizard than block forever.
    """
    payload = {
        "symbol": str(symbol).strip().upper(),
        "order_id": int(order_id),
    }
    last_reason = ""
    for attempt in range(PACIFICA_CANCEL_RETRIES + 1):
        try:
            _post_signed_ack(credentials, "/orders/cancel", "cancel_order", payload)
            return True, ""
        except Exception as exc:  # noqa: BLE001
            last_reason = sanitize_error_message(str(exc))
            if attempt >= PACIFICA_CANCEL_RETRIES:
                break
            if not _is_transient_cancel_error(exc):
                # Permanent: 4xx, etc. Don't retry.
                break
            time.sleep(PACIFICA_CANCEL_RETRY_BACKOFF_SECONDS * (attempt + 1))
    return False, last_reason


def _execute_cancel_order_group(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Cancel every open limit order for ``(symbol, side)`` on the account.

    Wizard contract:
      - ``request["symbol"]`` : str, required, uppercase
      - ``request["side"]``   : str, required, ``"long"`` or ``"short"``
                               (canonical; we translate to Pacifica's
                               ``bid``/``ask`` for filtering)

    Pacifica doesn't expose a per-(symbol, side) bulk-cancel
    endpoint. The ``/orders/cancel_all`` endpoint takes a symbol but
    no side filter — calling it would cancel resting orders on BOTH
    sides of the same symbol, which is wrong if the user has both
    sides open. To stay safe, we cancel per-order via
    ``/orders/cancel`` and parallelise with a small thread pool.

    Pipeline:
      1. Snapshot the open-orders list.
      2. Filter to (symbol, canonical-side) targets; remember their
         Pacifica order_ids.
      3. Cancel each order in parallel; record per-order outcome.
      4. Re-snapshot and confirm the targets are gone. This is the
         authoritative verification — the per-order cancel ack can
         be misleading if the order was filled between our snapshot
         and our cancel POST.
    """
    creds = _lookup_credentials(account)
    if creds is None:
        return make_failure(
            operation="cancel_order_group", exchange=name, account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or incomplete Pacifica account credentials.",
        )

    symbol = str(request.get("symbol") or "").strip().upper()
    canonical_side = str(request.get("side") or "").strip().lower()
    if not symbol:
        return make_failure(
            operation="cancel_order_group", exchange=name, account=account,
            code="MISSING_SYMBOL", message="Symbol is required.",
        )
    if canonical_side not in {"long", "short"}:
        return make_failure(
            operation="cancel_order_group", exchange=name, account=account,
            code="INVALID_SIDE", message="Side must be 'long' or 'short'.",
        )
    pacific_side = "bid" if canonical_side == "long" else "ask"

    # --- 1. snapshot open orders ---
    try:
        before = _get_open_orders(creds["address"])
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="cancel_order_group", exchange=name, account=account,
            code="ORDERS_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )

    # --- 2. identify targets ---
    target_ids: List[int] = []
    for row in before:
        if not isinstance(row, dict):
            continue
        row_symbol = str(row.get("symbol") or "").strip().upper()
        row_side = str(row.get("side") or "").strip().lower()
        if row_symbol != symbol or row_side != pacific_side:
            continue
        raw_id = row.get("order_id")
        try:
            order_id = int(str(raw_id))
        except (TypeError, ValueError):
            continue
        target_ids.append(order_id)

    # Empty target set: the user confirmed a cancel of a group that
    # is already gone (race with another client, or a stale wizard
    # view). This is a verified, no-op success.
    if not target_ids:
        return make_success(
            operation="cancel_order_group", exchange=name, account=creds["account"],
            cancel_group=CanonicalCancelGroupResult(
                symbol=symbol, side=canonical_side,
                targeted_order_count=0, cancelled_order_count=0,
                confirmed_absent_count=0, remaining_target_count=0,
                verified=True, partial=False, status="success",
                batch_count=0, batches=[],
            ),
        )

    # --- 3. cancel each target in parallel ---
    batches: List[Dict[str, Any]] = []
    accepted = 0
    # Per-order outcomes keyed by order_id (so the post-cancel
    # verification can count "targets that we believe we cancelled").
    per_order_outcome: Dict[int, Tuple[bool, str]] = {}

    def _cancel_one(order_id: int) -> Tuple[int, bool, str]:
        ok, reason = _signed_cancel_order(creds, symbol, order_id)
        return order_id, ok, reason

    with ThreadPoolExecutor(max_workers=PACIFICA_CANCEL_CONCURRENCY) as pool:
        futures = [pool.submit(_cancel_one, oid) for oid in target_ids]
        for fut in as_completed(futures):
            order_id, ok, reason = fut.result()
            per_order_outcome[order_id] = (ok, reason)
            if ok:
                accepted += 1
                batches.append({
                    "submitted": 1, "accepted": 1, "ok": True,
                    "order_id": order_id,
                })
            else:
                batches.append({
                    "submitted": 1, "accepted": 0, "ok": False,
                    "order_id": order_id,
                    "reason": reason,
                })

    # --- 4. re-snapshot and confirm the targets are absent ---
    after_open: List[Dict[str, Any]] = []
    snapshot_error: Optional[str] = None
    for attempt in range(PACIFICA_CANCEL_VERIFY_ATTEMPTS):
        try:
            after_open = _get_open_orders(creds["address"])
            snapshot_error = None
            break
        except Exception as exc:  # noqa: BLE001
            snapshot_error = sanitize_error_message(str(exc))
            if attempt < PACIFICA_CANCEL_VERIFY_ATTEMPTS - 1:
                time.sleep(PACIFICA_CANCEL_VERIFY_DELAY_SECONDS * (attempt + 1))

    if snapshot_error is not None:
        # Verification itself failed. The cancels were issued; we just
        # can't prove they succeeded. Report partial.
        return make_failure(
            operation="cancel_order_group", exchange=name, account=creds["account"],
            code="VERIFICATION_FAILED",
            message=(
                f"Cancellation was issued but verification could not "
                f"complete: {snapshot_error}"
            ),
            cancel_group=CanonicalCancelGroupResult(
                symbol=symbol, side=canonical_side,
                targeted_order_count=len(target_ids),
                cancelled_order_count=accepted,
                confirmed_absent_count=0,
                remaining_target_count=len(target_ids) - accepted,
                verified=False, partial=True, status="partial",
                batch_count=len(batches), batches=batches,
            ),
        )

    still_open_ids = set()
    for row in after_open:
        if not isinstance(row, dict):
            continue
        row_symbol = str(row.get("symbol") or "").strip().upper()
        row_side = str(row.get("side") or "").strip().lower()
        if row_symbol != symbol or row_side != pacific_side:
            continue
        try:
            still_open_ids.add(int(str(row.get("order_id"))))
        except (TypeError, ValueError):
            continue

    confirmed_absent = sum(1 for oid in target_ids if oid not in still_open_ids)
    remaining = len(target_ids) - confirmed_absent
    verified = remaining == 0
    partial = not verified

    result = CanonicalCancelGroupResult(
        symbol=symbol, side=canonical_side,
        targeted_order_count=len(target_ids),
        cancelled_order_count=accepted,
        confirmed_absent_count=confirmed_absent,
        remaining_target_count=remaining,
        verified=verified,
        partial=partial,
        status="success" if verified else "partial",
        batch_count=len(batches),
        batches=batches,
    )
    if verified:
        return make_success(
            operation="cancel_order_group", exchange=name,
            account=creds["account"], cancel_group=result,
        )
    # Still on the book: this shouldn't normally happen. The
    # per-order POSTs all returned success but the snapshot disagrees
    # — possibly a race, or a child that just got re-created by a
    # concurrent session. We surface partial, not failure, so the
    # wizard can show the residual list.
    return make_failure(
        operation="cancel_order_group", exchange=name,
        account=creds["account"],
        code="PARTIAL_CANCELLATION",
        message=(
            f"{confirmed_absent}/{len(target_ids)} orders confirmed cancelled. "
            f"{remaining} still on the book."
        ),
        cancel_group=result,
    )


# ---------------------------------------------------------------------------
# Ladder
# ---------------------------------------------------------------------------

# Ladder distributions accepted by the /trade wizard. The constants
# here are the canonical names; the wizard sends the exact strings
# the user picked in the distribution step.
PACIFICA_LADDER_DISTRIBUTION_UNIFORM = "uniform"
PACIFICA_LADDER_DISTRIBUTION_HALF_GAUSSIAN = "half_gaussian"
PACIFICA_LADDER_VALID_DISTRIBUTIONS = {
    PACIFICA_LADDER_DISTRIBUTION_UNIFORM,
    PACIFICA_LADDER_DISTRIBUTION_HALF_GAUSSIAN,
}

def _ladder_distribution_weights(order_count: int, distribution: str) -> List[Decimal]:
    """Return per-child weights (sum ≈ 1.0) for the chosen distribution.

    Mirrors the helper in the other agents so the wizard's
    "Smallest size near Start / Largest size near End" affordance
    means the same thing on every exchange.

      uniform       : flat 1/N
      half_gaussian  : σ=1 truncated to z ∈ [0, 3]; index 0 is
                       the smallest (z=3, exp(-4.5)), the last
                       index is the largest (z=0, exp(0)=1).
    """
    if order_count <= 0:
        return []
    distribution_key = str(distribution or "").strip().lower()
    if distribution_key == PACIFICA_LADDER_DISTRIBUTION_UNIFORM:
        return [Decimal("1")] * order_count
    if distribution_key != PACIFICA_LADDER_DISTRIBUTION_HALF_GAUSSIAN:
        raise ValueError("UNSUPPORTED_DISTRIBUTION")
    if order_count == 1:
        return [Decimal("1")]
    import math as _math
    span = Decimal(order_count - 1)
    weights: List[Decimal] = []
    for index in range(order_count):
        # Index 0 is the smallest weight (z=3); last index is largest
        # (z=0). Reverses the natural ascending z.
        z = Decimal("3") * (span - Decimal(index)) / span
        weight = _math.exp(-(float(z) ** 2) / 2.0)
        weights.append(Decimal(str(weight)))
    return weights


def _ladder_quantize_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    """Round ``value`` to the nearest multiple of ``increment``."""
    if increment <= 0:
        raise ValueError("INVALID_INCREMENT")
    units = (value / increment).to_integral_value(rounding=ROUND_HALF_UP)
    return units * increment


def _ladder_floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round ``value`` DOWN to the nearest multiple of ``step``."""
    if step <= 0:
        return value
    n = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return Decimal(n) * step


def _ladder_build_prices(
    start_price: Decimal,
    end_price: Decimal,
    order_count: int,
    price_increment: Decimal,
) -> List[Decimal]:
    """Lay out ladder prices evenly between start/end, then snap to ticks.

    After tick-quantization, adjacent prices can collapse to the same
    tick (the spread is too tight to express). We then enforce
    monotonicity: ascending ladders never step backward, descending
    ladders never step forward. This is the same approach the other
    agents use.
    """
    if order_count <= 0:
        return []
    if order_count == 1:
        return [_ladder_quantize_to_increment(
            (start_price + end_price) / Decimal("2"),
            price_increment,
        )]
    span = end_price - start_price
    step = span / Decimal(order_count - 1)
    raw_prices = [start_price + step * Decimal(index) for index in range(order_count)]
    prices = [_ladder_quantize_to_increment(p, price_increment) for p in raw_prices]
    if start_price <= end_price:
        for index in range(1, len(prices)):
            if prices[index] < prices[index - 1]:
                prices[index] = prices[index - 1]
    else:
        for index in range(1, len(prices)):
            if prices[index] > prices[index - 1]:
                prices[index] = prices[index - 1]
    return prices


def _ladder_allocate_sizes(
    total_volume: Decimal,
    order_count: int,
    size_increment: Decimal,
    distribution: str,
) -> Tuple[List[Decimal], Decimal]:
    """Allocate ``total_volume`` across ``order_count`` children by weight.

    Each child's raw share is rounded DOWN to the nearest lot so the
    total never exceeds the requested volume. The residual whole
    units are then distributed to the children with the largest
    fractional remainder (a stable, deterministic tiebreaker).

    Returns ``(sizes, kept_total)``.
    """
    if size_increment <= 0:
        raise ValueError("INVALID_INCREMENT")
    total_units = int(
        (total_volume / size_increment).to_integral_value(rounding=ROUND_HALF_UP)
    )
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
        # Stable tiebreaker: largest remainder first, with -index as
        # secondary key so earlier children get the leftover unit on
        # ties. Same convention as the other agents.
        order_indices = sorted(
            range(order_count),
            key=lambda index: (remainders[index], -index),
            reverse=True,
        )
        for index in order_indices[:residual]:
            allocation[index] += 1
    sizes = [Decimal(units) * size_increment for units in allocation]
    return sizes, Decimal(total_units) * size_increment


def _ladder_build_children(
    *,
    symbol: str,
    side: str,
    distribution: str,
    order_count: int,
    total_volume: Decimal,
    start_price: Decimal,
    end_price: Decimal,
    size_increment: Decimal,
    price_increment: Decimal,
) -> Tuple[List[Dict[str, Any]], Decimal, int, int]:
    """Build the per-child ladder rows, snap to instrument precision,
    drop sub-floor children, and return the kept list.

    The size-allocation convention matches the other agents: for SELL
    ladders the lowest-price child is the closest to market, so the
    smallest-size child sits there (i.e. we reverse the size array
    before assigning to prices, so the smallest size lands at the
    lowest price). For BUY ladders the lowest-price child is farthest
    from market, so it gets the smallest size — which is the natural
    ordering.

    Children below the per-order $10 USD notional floor are dropped
    WITHOUT redistribution. This matches the policy in every other
    agent and keeps the user's volume budget honest: if the ladder is
    too thin for the count, the user gets fewer orders but each
    survivor is valid. The wizard then sees the omitted count and
    status="partial" in the response.
    """
    if order_count <= 0:
        return [], Decimal("0"), 0, 0
    prices = _ladder_build_prices(start_price, end_price, order_count, price_increment)
    if not prices:
        return [], Decimal("0"), 0, 0
    raw_sizes, kept_volume_total = _ladder_allocate_sizes(
        total_volume=total_volume,
        order_count=order_count,
        size_increment=size_increment,
        distribution=distribution,
    )
    if side == "sell":
        raw_sizes = list(reversed(raw_sizes))

    children: List[Dict[str, Any]] = []
    omitted_below_minimum = 0
    for index, price in enumerate(prices):
        size = _ladder_floor_to_step(raw_sizes[index], size_increment)
        if size <= 0:
            omitted_below_minimum += 1
            continue
        if price * size < PACIFICA_LADDER_MIN_NOTIONAL_USD:
            omitted_below_minimum += 1
            continue
        children.append({
            "symbol": symbol,
            "side": side,
            "size": _format_market_value(size, size_increment),
            "price": _format_market_value(price, price_increment),
            # Client order id is generated in the submit loop so each
            # child gets its own UUID (Pacifica requires uniqueness
            # only when you intend to look up the order by it, but we
            # always do that for verification).
            "client_order_id": None,
        })
    kept_volume = sum(
        _decimal_or_zero(c["size"]) for c in children
    )
    return children, kept_volume, omitted_below_minimum, len(children)


def _ladder_submit_child(
    credentials: Dict[str, str],
    child: Dict[str, Any],
) -> Tuple[Optional[int], Optional[str], str]:
    """Submit a single ladder child as a signed ``create_order`` POST.

    Returns ``(exchange_order_id, error_message, client_order_id)``.
    On success, ``error_message`` is None and ``client_order_id`` is
    the UUID the wizard (and the verifier) will look up. On failure
    we keep the client_order_id so the user can correlate partial
    successes.
    """
    client_order_id = str(uuid.uuid4())
    child_with_cloid = {**child, "client_order_id": client_order_id}
    pacifica_side = PACIFICA_SIDE_TO_PACIFICA[str(child["side"]).lower()]
    payload = {
        "symbol": child["symbol"],
        "price": child["price"],
        "reduce_only": False,
        "amount": child["size"],
        "side": pacifica_side,
        "tif": PACIFICA_DEFAULT_TIF,
        "client_order_id": client_order_id,
    }
    try:
        response_data = _post_signed(
            credentials, "/orders/create", "create_order", payload,
        )
    except Exception as exc:  # noqa: BLE001
        return None, sanitize_error_message(str(exc)), client_order_id

    # The create-order response carries the exchange_order_id in
    # `data.i`; we don't strictly need it for verification (we look up
    # by client_order_id) but it's nice to surface in the batch log.
    raw_id = response_data.get("i")
    try:
        exchange_order_id = int(str(raw_id)) if raw_id is not None else None
    except (TypeError, ValueError):
        exchange_order_id = None
    return exchange_order_id, None, client_order_id


def _ladder_verify_children(
    credentials: Dict[str],
    child_cloids: List[str],
) -> Tuple[int, List[str]]:
    """Confirm a set of child orders (by client_order_id) are visible.

    Polls the open-orders snapshot a few times because Pacifica's
    read-after-write consistency is fast but not instant for
    cross-endpoint lookups.

    Returns ``(confirmed_count, missing_cloids)``.
    """
    if not child_cloids:
        return 0, []
    missing: Set[str] = set(child_cloids)
    for attempt in range(PACIFICA_LADDER_VERIFY_ATTEMPTS):
        try:
            payload = _http_get_json(
                f"{_api_base()}/orders",
                params={"account": credentials["address"]},
            )
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, list):
                for row in data:
                    if not isinstance(row, dict):
                        continue
                    cloid = str(row.get("client_order_id") or "").strip()
                    if cloid in missing:
                        missing.discard(cloid)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Pacifica ladder verify attempt %d/%d failed: %s",
                attempt + 1, PACIFICA_LADDER_VERIFY_ATTEMPTS, exc,
            )
        if not missing:
            break
        if attempt < PACIFICA_LADDER_VERIFY_ATTEMPTS - 1:
            time.sleep(PACIFICA_LADDER_VERIFY_DELAY_SECONDS * (attempt + 1))
    confirmed = len(child_cloids) - len(missing)
    return confirmed, sorted(missing)


def _execute_ladder(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Place a multi-order ladder of GTC limit orders on Pacifica.

    Pipeline:
      1. Validate the inputs (symbol, side, distribution, counts,
         prices, total volume) and resolve the instrument's
         tick_size / lot_size via the market cache.
      2. Build the per-child rows: lay out prices across [start, end]
         snapped to tick, distribute total_volume across N children
         by weight (uniform or half-gaussian), floor each child's
         size to the lot, drop children below the $10 USD notional
         floor (no redistribution).
      3. Submit each surviving child as a separate signed
         ``create_order`` POST. Each child gets its own UUID
         client_order_id so the verify pass can confirm it landed.
      4. Re-snapshot /orders and confirm every submitted child's
         client_order_id is visible. Survivors-of-survivors count as
         the accepted_child_count.

    Submission is **sequential** rather than parallel. The cancel
    operation parallelises because we have at most ~280 targets and
    the bottleneck is server-side; for a typical 3-15 child ladder,
    sequential submission at ~150ms per POST keeps the code simpler
    and avoids racing the user against themselves (each child is a
    real new order that hits the orderbook).
    """
    creds = _lookup_credentials(account)
    if creds is None:
        return make_failure(
            operation="ladder", exchange=name, account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or incomplete Pacifica account credentials.",
        )

    # --- 1. validate inputs ---
    requested_symbol = str(request.get("symbol") or "").strip().upper()
    requested_side = str(request.get("side") or "").strip().lower()
    distribution = str(request.get("distribution") or "").strip().lower()
    try:
        order_count = int(str(request.get("order_count") or "").strip())
    except Exception:
        order_count = 0
    try:
        total_volume = Decimal(str(request.get("total_volume") or "0"))
    except Exception:
        total_volume = Decimal("0")
    try:
        start_price = Decimal(str(request.get("start_price") or "0"))
    except Exception:
        start_price = Decimal("0")
    try:
        end_price = Decimal(str(request.get("end_price") or "0"))
    except Exception:
        end_price = Decimal("0")

    if not requested_symbol:
        return make_failure(
            operation="ladder", exchange=name, account=account,
            code="MISSING_SYMBOL", message="Symbol is required.",
        )
    if requested_side not in PACIFICA_CANONICAL_SIDES:
        return make_failure(
            operation="ladder", exchange=name, account=account,
            code="INVALID_SIDE", message="Side must be 'buy' or 'sell'.",
        )
    if distribution not in PACIFICA_LADDER_VALID_DISTRIBUTIONS:
        return make_failure(
            operation="ladder", exchange=name, account=account,
            code="INVALID_DISTRIBUTION",
            message=f"Distribution must be one of {sorted(PACIFICA_LADDER_VALID_DISTRIBUTIONS)}.",
        )
    if order_count <= 0:
        return make_failure(
            operation="ladder", exchange=name, account=account,
            code="INVALID_ORDER_COUNT", message="Order count must be positive.",
        )
    if total_volume <= 0:
        return make_failure(
            operation="ladder", exchange=name, account=account,
            code="INVALID_VOLUME", message="Total volume must be positive.",
        )
    if start_price <= 0 or end_price <= 0:
        return make_failure(
            operation="ladder", exchange=name, account=account,
            code="INVALID_PRICE", message="Start and end price must be positive.",
        )
    # Direction validation: BUY ladders need end < start (prices
    # descending from the user's "highest acceptable buy" to "lowest
    # acceptable buy"); SELL ladders need end > start. The wizard
    # already guides the user through this, but a swap is an easy
    # mistake to make on a phone keyboard.
    if requested_side == "buy" and end_price >= start_price:
        return make_failure(
            operation="ladder", exchange=name, account=account,
            code="INVALID_LADDER_DIRECTION",
            message="BUY ladders require end price below start price.",
        )
    if requested_side == "sell" and end_price <= start_price:
        return make_failure(
            operation="ladder", exchange=name, account=account,
            code="INVALID_LADDER_DIRECTION",
            message="SELL ladders require end price above start price.",
        )

    # --- 2. resolve market metadata + build children ---
    try:
        market = _get_market_info(requested_symbol)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="ladder", exchange=name, account=account,
            code="MARKET_INFO_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )
    if market is None:
        return make_failure(
            operation="ladder", exchange=name, account=account,
            code="INSTRUMENT_NOT_FOUND",
            message=f"Symbol '{requested_symbol}' is not listed on Pacifica.",
        )
    tick_size = _decimal_or_zero(market.get("tick_size"))
    lot_size = _decimal_or_zero(market.get("lot_size"))
    if tick_size <= 0 or lot_size <= 0:
        return make_failure(
            operation="ladder", exchange=name, account=account,
            code="MARKET_METADATA_INVALID",
            message=(
                f"Pacifica market '{requested_symbol}' has invalid "
                f"tick_size or lot_size; cannot ladder."
            ),
        )

    children, kept_volume, omitted_below_minimum, kept_count = _ladder_build_children(
        symbol=requested_symbol,
        side=requested_side,
        distribution=distribution,
        order_count=order_count,
        total_volume=total_volume,
        start_price=start_price,
        end_price=end_price,
        size_increment=lot_size,
        price_increment=tick_size,
    )
    omitted_total = (order_count - kept_count) - omitted_below_minimum
    if omitted_total < 0:
        omitted_total = 0

    if kept_count < 1:
        ladder_result = CanonicalLadderResult(
            symbol=requested_symbol, side=requested_side, distribution=distribution,
            requested_order_count=order_count, submitted_order_count=0,
            requested_volume=_format_market_value(total_volume, lot_size),
            submitted_volume="0",
            batch_count=0, verified=False, partial=True, status="failed",
            accepted_child_count=0,
            omitted_order_count=order_count - kept_count,
            omitted_below_minimum=omitted_below_minimum,
            child_order_ids=[], batches=[],
        )
        return make_failure(
            operation="ladder", exchange=name, account=creds["account"],
            code="LADDER_TOO_FEW_VALID_CHILDREN",
            message=(
                f"No ladder children survived the "
                f"${PACIFICA_LADDER_MIN_NOTIONAL_USD} notional floor "
                f"({kept_count} kept, {omitted_below_minimum} omitted)."
            ),
            ladder=ladder_result,
        )

    # --- 3. submit each child sequentially ---
    batches: List[Dict[str, Any]] = []
    accepted_cloids: List[str] = []
    accepted_order_ids: List[int] = []
    for index, child in enumerate(children):
        exchange_order_id, error, cloid = _ladder_submit_child(creds, child)
        if error is None:
            accepted_cloids.append(cloid)
            if exchange_order_id is not None:
                accepted_order_ids.append(exchange_order_id)
            batches.append({
                "submitted": 1, "accepted": 1, "ok": True,
                "client_order_id": cloid,
                "exchange_order_id": exchange_order_id,
                "price": child["price"],
                "size": child["size"],
                "index": index,
            })
        else:
            batches.append({
                "submitted": 1, "accepted": 0, "ok": False,
                "client_order_id": cloid,
                "reason": error,
                "price": child["price"],
                "size": child["size"],
                "index": index,
            })
        # We do NOT abort the ladder on a single-child failure — the
        # user explicitly asked for N orders, and partial fills are
        # the right behaviour here. The final result surfaces the
        # accept count so the wizard can show "Submitted: 8 of 10".

    if not accepted_cloids:
        ladder_result = CanonicalLadderResult(
            symbol=requested_symbol, side=requested_side, distribution=distribution,
            requested_order_count=order_count, submitted_order_count=0,
            requested_volume=_format_market_value(total_volume, lot_size),
            submitted_volume="0",
            batch_count=len(batches), verified=False, partial=True, status="failed",
            accepted_child_count=0,
            omitted_order_count=order_count - kept_count,
            omitted_below_minimum=omitted_below_minimum,
            child_order_ids=[], batches=batches,
        )
        return make_failure(
            operation="ladder", exchange=name, account=creds["account"],
            code="LADDER_SUBMISSION_FAILED",
            message="No ladder children could be submitted.",
            ladder=ladder_result,
        )

    # --- 4. verify accepted children are visible ---
    confirmed_count, missing_cloids = _ladder_verify_children(creds, accepted_cloids)
    # If the verify pass missed a child, downgrade the result to
    # partial. The per-order POSTs all returned success but the
    # snapshot disagrees — likely a transient read-after-write
    # race; the operator can refresh positions_orders and see the
    # child.
    verified = not missing_cloids
    submitted_volume = sum(
        _decimal_or_zero(b["size"]) for b in batches if b.get("ok")
    )
    status = "success" if verified else "partial"
    partial = not verified

    # If the user wanted 10 orders and we placed 10, but the snapshot
    # confirms only 8, the wizard should still see "Submitted: 10,
    # Verified: 8". The accepted_child_count is the number of
    # children the agent placed, the confirmed count is implicit in
    # the verified flag.
    accepted_child_count = len(accepted_cloids)
    submitted_order_count = accepted_child_count  # what was actually sent to the exchange

    ladder_result = CanonicalLadderResult(
        symbol=requested_symbol, side=requested_side, distribution=distribution,
        requested_order_count=order_count, submitted_order_count=submitted_order_count,
        requested_volume=_format_market_value(total_volume, lot_size),
        submitted_volume=_format_market_value(submitted_volume, lot_size),
        batch_count=len(batches),
        verified=verified, partial=partial, status=status,
        accepted_child_count=accepted_child_count,
        omitted_order_count=order_count - submitted_order_count,
        omitted_below_minimum=omitted_below_minimum,
        child_order_ids=accepted_order_ids or None,
        batches=batches,
    )
    if verified:
        return make_success(
            operation="ladder", exchange=name,
            account=creds["account"], ladder=ladder_result,
        )
    return make_failure(
        operation="ladder", exchange=name, account=creds["account"],
        code="LADDER_VERIFICATION_INCOMPLETE",
        message=(
            f"{confirmed_count}/{accepted_child_count} submitted children "
            f"confirmed on the book. Refresh the positions view to see "
            f"the residual; the others may appear shortly."
        ),
        ladder=ladder_result,
    )


# ---------------------------------------------------------------------------
# Position management (set_tp / set_sl / close_position / list)
# ---------------------------------------------------------------------------

# Canonical side convention used everywhere in this file:
#   "long"  : Pacifica "bid"  (operator bought)
#   "short" : Pacifica "ask"  (operator sold)
# The wizard always sends ``state.position["side"]`` as "long"/"short"
# (set in the positions_management view), but we also accept the raw
# Pacifica strings in case a future caller passes them through.

# Time-in-force used for stop orders. Pacifica's create_stop_order
# endpoint accepts the same TIF values as create_order; GTC is the
# wizard's default and what every other agent uses.
_PACIFICA_TPSL_TIF = "GTC"


def _position_action_result(
    *,
    operation: str,
    symbol: str,
    verified: bool,
    status: str = "success",
    price: Optional[str] = None,
    removed: Optional[bool] = None,
    current_side: Optional[str] = None,
    current_size: Optional[str] = None,
    exchange_order_id: Optional[int] = None,
    message: Optional[str] = None,
) -> CanonicalPositionActionResult:
    """Build a ``CanonicalPositionActionResult`` for the wizard renderer."""
    return CanonicalPositionActionResult(
        operation=operation,
        symbol=symbol,
        verified=verified,
        status=status,
        price=price,
        removed=removed,
        current_side=current_side,
        current_size=current_size,
        exchange_order_id=exchange_order_id,
        message=message,
    )


def _find_position(positions: List[Dict[str, Any]], symbol: str) -> Optional[Dict[str, Any]]:
    """Find the open position row matching ``symbol`` (case-insensitive).

    Returns the raw Pacifica ``/positions`` row (or ``None`` if no match).
    The caller is responsible for the case where two positions on the
    same symbol exist (Pacifica has one position per symbol in
    practice, but a defensive ``positions[0]`` would be a footgun if
    that ever changed).
    """
    target = str(symbol or "").strip().upper()
    if not target:
        return None
    for row in positions:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or "").strip().upper() == target:
            return row
    return None


def _classify_tpsl_stop_orders(
    orders: List[Dict[str, Any]],
    symbol: str,
    side_canonical: str,
    entry_price: Decimal,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Find the existing TP and SL stop child orders for ``(symbol, side)``.

    Pacifica's ``/positions/tpsl`` endpoint creates two stop child
    orders (one for take-profit, one for stop-loss) when both are
    configured. Each child sits in the user's ``/orders`` list with
    a non-limit ``order_type`` (``take_profit_market``,
    ``stop_market``, etc.) and ``stop_price`` set.

    The child's own ``side`` field is the *closing* side of the
    position (opposite the position's opening side), not the
    position's side — so we don't filter by ``side`` here. Instead
    we match by ``symbol`` + ``reduce_only=True`` (which is true for
    every TP/SL child Pacifica creates) and the order's price side
    relative to the entry:

      long  : TP trigger >= entry → take-profit; trigger < entry → stop-loss
      short : TP trigger <= entry → take-profit; trigger > entry → stop-loss

    Returns ``(tp_dict, sl_dict)`` — either may be ``None`` if the
    corresponding leg isn't currently set.
    """
    tp: Optional[Dict[str, Any]] = None
    sl: Optional[Dict[str, Any]] = None
    target = str(symbol or "").strip().upper()

    for row in orders:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or "").strip().upper() != target:
            continue
        # TP/SL children are always reduce_only. Skip stray rows
        # that aren't (defensive — shouldn't happen on a clean
        # exchange, but cheap insurance against future schema drift).
        if not _coerce_bool_safe(row.get("reduce_only")):
            continue
        order_type = str(row.get("order_type") or "").strip().lower()
        if order_type == "limit":
            continue
        # Must have a stop_price to be a TP/SL child.
        stop_price_text = str(row.get("stop_price") or "").strip()
        if not stop_price_text:
            continue
        try:
            stop_price = Decimal(stop_price_text)
        except Exception:  # noqa: BLE001
            continue
        if str(side_canonical or "").lower() == "long":
            if stop_price >= entry_price and tp is None:
                tp = row
            elif stop_price < entry_price and sl is None:
                sl = row
        else:  # short
            if stop_price <= entry_price and tp is None:
                tp = row
            elif stop_price > entry_price and sl is None:
                sl = row
    return tp, sl


def _cancel_stop_child(
    credentials: Dict[str, str],
    order: Dict[str, Any],
) -> Tuple[bool, str]:
    """Cancel a TP/SL stop child order via the dedicated endpoint.

    Returns ``(ok, reason)`` with the same retry semantics as the
    regular cancel path. We use ``/orders/stop/cancel`` (operation
    type ``cancel_stop_order``) rather than the generic
    ``/orders/cancel`` because stop and limit orders have separate
    cancel endpoints in Pacifica's API.
    """
    raw_id = order.get("order_id")
    try:
        order_id = int(str(raw_id)) if raw_id is not None else None
    except (TypeError, ValueError):
        order_id = None
    cloid = str(order.get("client_order_id") or "").strip()
    if order_id is None and not cloid:
        return False, "stop child order has no order_id or client_order_id"
    payload: Dict[str, Any] = {
        "symbol": str(order.get("symbol") or "").strip().upper(),
    }
    if order_id is not None:
        payload["order_id"] = order_id
    elif cloid:
        payload["client_order_id"] = cloid

    last_reason = ""
    for attempt in range(PACIFICA_CANCEL_RETRIES + 1):
        try:
            _post_signed_ack(
                credentials, "/orders/stop/cancel", "cancel_stop_order", payload,
            )
            return True, ""
        except Exception as exc:  # noqa: BLE001
            last_reason = sanitize_error_message(str(exc))
            if attempt >= PACIFICA_CANCEL_RETRIES:
                break
            if not _is_transient_cancel_error(exc):
                break
            time.sleep(PACIFICA_CANCEL_RETRY_BACKOFF_SECONDS * (attempt + 1))
    return False, last_reason


def _get_stop_child_orders(address: str) -> List[Dict[str, Any]]:
    """Fetch ``GET /api/v1/orders?account=...`` and return stop / TP / SL rows.

    Pacifica's ``/orders`` endpoint carries ALL order types (limit,
    take_profit_market, stop_market, stop_limit, etc.) but is
    flat — there's no separate endpoint for the protection orders.
    We pull the full list and keep only rows that are *not* plain
    limit orders, since the ladder / open-order path is served by
    a separate helper (``_get_open_orders``) that intentionally
    filters those out.

    Used by the TP / SL verify paths: after a ``set_position_tpsl``
    POST, the server creates one or more ``take_profit_*`` /
    ``stop_*`` rows; we look them up here to confirm the placement
    actually landed.
    """
    payload = _http_get_json(f"{_api_base()}/orders", params={"account": address})
    if not isinstance(payload, dict):
        raise RuntimeError("Pacifica /orders returned a non-object payload")
    if payload.get("success") is False:
        code = payload.get("code")
        message = payload.get("error") or payload.get("message") or "Pacifica /orders failed"
        raise RuntimeError(f"{message} (code={code})")
    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError("Pacifica /orders response missing data list")
    return [
        row for row in data
        if isinstance(row, dict)
        and str(row.get("order_type") or "").strip().lower() != "limit"
    ]


def _execute_positions_management(
    account: str, request: Dict[str, Any]
) -> CanonicalResponse:
    """List open positions with their current TP/SL children attached.

    The /trade wizard's "Manage Positions" screen calls this
    operation. We return the same per-position rows as
    ``positions_orders`` (size, entry, uPnL) plus the current TP / SL
    trigger prices and counts, looked up by scanning the user's order
    list for stop child orders whose symbol + side match the position.

    Positions with no TP or SL set get ``tp=None``, ``sl=None``,
    ``tp_count=0``, ``sl_count=0`` — the wizard renders those as "—".
    """
    creds = _lookup_credentials(account)
    if creds is None:
        return make_failure(
            operation="positions_management", exchange=name, account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or incomplete Pacifica account credentials.",
        )
    try:
        rows = _get_positions(creds["address"])
        try:
            mark_by_symbol = _get_mark_prices()
        except Exception:  # noqa: BLE001
            mark_by_symbol = {}
        # Soft fetch of stop child orders (TP/SL children) so we can
        # attach them to each position. A failure here just means
        # tp/sl will be None everywhere — the positions themselves
        # still come back.
        try:
            stop_children = _get_stop_child_orders(creds["address"])
        except Exception:  # noqa: BLE001
            stop_children = []

        positions = []
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            raw_side = str(row.get("side") or "").strip().lower()
            if not symbol or raw_side not in ("bid", "ask"):
                continue
            side_canonical = "long" if raw_side == "bid" else "short"
            try:
                size_dec = Decimal(_require_decimal_string(
                    row.get("amount"), f"positions[{symbol}].amount",
                ))
                entry_dec = Decimal(_require_decimal_string(
                    row.get("entry_price"), f"positions[{symbol}].entry_price",
                ))
            except Exception:  # noqa: BLE001
                continue
            mark = mark_by_symbol.get(symbol)
            pnl = _compute_unrealized_pnl(
                side_canonical=side_canonical,
                size=size_dec, entry_price=entry_dec, mark_price=mark,
            )
            # Find this position's TP/SL children, if any.
            tp_row, sl_row = _classify_tpsl_stop_orders(
                stop_children, symbol, side_canonical, entry_dec,
            )
            positions.append(CanonicalPosition(
                symbol=symbol,
                side=side_canonical,
                size=format(size_dec, "f"),
                entry_price=format(entry_dec, "f"),
                pnl=_format_pnl(pnl),
                tp=_format_decimal_for_wizard(tp_row.get("stop_price")) if tp_row else None,
                sl=_format_decimal_for_wizard(sl_row.get("stop_price")) if sl_row else None,
                tp_count=1 if tp_row else 0,
                sl_count=1 if sl_row else 0,
            ))
        return make_success(
            operation="positions_management", exchange=name,
            account=creds["account"], positions=positions,
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="positions_management", exchange=name, account=account,
            code="POSITIONS_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )


def _execute_set_tp(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Set or remove the take-profit leg of an open position.

    Wizard contract:
      - ``price > 0``  : place a new TP stop order at this trigger price
      - ``price <= 0`` : remove the existing TP (cancel the stop child)

    Implementation notes:
      - We look up the position first to derive the side, entry
        price, and size. Without a side in the body, the wizard
        can't be more specific than ``(symbol)``.
      - For setting a new TP, we cancel the existing TP child (if
        any) and submit a fresh ``set_position_tpsl`` with only the
        new TP. The existing SL (if any) is left alone — we
        deliberately do NOT submit a SL value because Pacifica
        doesn't expose a read endpoint for the current SL, and
        re-submitting the wrong SL would overwrite a perfectly good
        one. This trades a small risk (Pacifica might clear the SL
        when only TP is sent) for not destroying the user's SL.
      - The verification reads the stop children and confirms a new
        TP at (or very near) the requested price is now present.
    """
    creds = _lookup_credentials(account)
    if creds is None:
        return make_failure(
            operation="set_tp", exchange=name, account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or incomplete Pacifica account credentials.",
        )

    symbol = str(request.get("symbol") or "").strip().upper()
    price_text = str(request.get("price") or "").strip()
    if not symbol:
        return make_failure(
            operation="set_tp", exchange=name, account=account,
            code="MISSING_SYMBOL", message="Symbol is required.",
        )
    try:
        tp_price = Decimal(price_text) if price_text else Decimal("0")
    except Exception:
        return make_failure(
            operation="set_tp", exchange=name, account=account,
            code="INVALID_TP_PRICE", message="TP price must be numeric.",
        )

    # --- locate the open position ---
    try:
        positions = _get_positions(creds["address"])
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="set_tp", exchange=name, account=account,
            code="POSITIONS_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )
    pos = _find_position(positions, symbol)
    if pos is None:
        return make_failure(
            operation="set_tp", exchange=name, account=account,
            code="NO_OPEN_POSITION",
            message=f"No open position for {symbol}.",
            position_action=_position_action_result(
                operation="set_tp", symbol=symbol, verified=False,
                status="failed",
            ),
        )
    raw_side = str(pos.get("side") or "").strip().lower()
    side_canonical = "long" if raw_side == "bid" else "short"
    try:
        size_dec = Decimal(str(pos.get("amount") or "0"))
        entry_dec = Decimal(str(pos.get("entry_price") or "0"))
    except Exception:  # noqa: BLE001
        return make_failure(
            operation="set_tp", exchange=name, account=account,
            code="POSITION_METADATA_INVALID",
            message=f"Position {symbol} has unparseable amount/entry_price.",
            position_action=_position_action_result(
                operation="set_tp", symbol=symbol, verified=False,
                status="failed",
            ),
        )

    # --- remove path: price <= 0 → cancel the existing TP child ---
    if tp_price <= 0:
        try:
            stop_children = _get_stop_child_orders(creds["address"])
        except Exception:  # noqa: BLE001
            stop_children = []
        existing_tp, _ = _classify_tpsl_stop_orders(
            stop_children, symbol, side_canonical, entry_dec,
        )
        if existing_tp is None:
            return make_success(
                operation="set_tp", exchange=name, account=creds["account"],
                position_action=_position_action_result(
                    operation="set_tp", symbol=symbol, verified=True,
                    status="removed", removed=True,
                    current_side=side_canonical,
                    current_size=format(size_dec, "f"),
                    price="0",
                    message="No existing TP to cancel.",
                ),
            )
        ok, reason = _cancel_stop_child(creds, existing_tp)
        if not ok:
            return make_failure(
                operation="set_tp", exchange=name, account=creds["account"],
                code="TP_CANCEL_FAILED",
                message=(
                    f"Failed to cancel existing TP order: {reason}"
                ),
                position_action=_position_action_result(
                    operation="set_tp", symbol=symbol, verified=False,
                    status="failed",
                    current_side=side_canonical,
                    current_size=format(size_dec, "f"),
                    price=str(existing_tp.get("stop_price") or "0"),
                ),
            )
        return make_success(
            operation="set_tp", exchange=name, account=creds["account"],
            position_action=_position_action_result(
                operation="set_tp", symbol=symbol, verified=True,
                status="removed", removed=True,
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
                price="0",
                message=f"Cancelled existing TP order {existing_tp.get('order_id')}.",
            ),
        )

    # --- set path: snap the price, validate direction, submit ---
    try:
        market = _get_market_info(symbol)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="set_tp", exchange=name, account=creds["account"],
            code="MARKET_INFO_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
            position_action=_position_action_result(
                operation="set_tp", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
            ),
        )
    if market is None:
        return make_failure(
            operation="set_tp", exchange=name, account=creds["account"],
            code="INSTRUMENT_NOT_FOUND",
            message=f"Symbol '{symbol}' is not listed on Pacifica.",
            position_action=_position_action_result(
                operation="set_tp", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
            ),
        )
    tick_size = _decimal_or_zero(market.get("tick_size"))
    if tick_size <= 0:
        return make_failure(
            operation="set_tp", exchange=name, account=creds["account"],
            code="MARKET_METADATA_INVALID",
            message=f"Pacifica market '{symbol}' has invalid tick_size.",
            position_action=_position_action_result(
                operation="set_tp", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
            ),
        )
    snapped_tp = _quantize_to_step(tp_price, tick_size)
    if snapped_tp <= 0:
        return make_failure(
            operation="set_tp", exchange=name, account=creds["account"],
            code="INVALID_TP_PRICE",
            message=f"TP price {tp_price} rounds to zero at tick {tick_size}.",
            position_action=_position_action_result(
                operation="set_tp", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
                price="0",
            ),
        )
    # Validate direction: TP must be on the profitable side of entry.
    if side_canonical == "long" and snapped_tp <= entry_dec:
        return make_failure(
            operation="set_tp", exchange=name, account=creds["account"],
            code="INVALID_TP_DIRECTION",
            message=(
                f"TP price {snapped_tp} must be above the long entry {entry_dec}."
            ),
            position_action=_position_action_result(
                operation="set_tp", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
                price=format(snapped_tp, "f"),
            ),
        )
    if side_canonical == "short" and snapped_tp >= entry_dec:
        return make_failure(
            operation="set_tp", exchange=name, account=creds["account"],
            code="INVALID_TP_DIRECTION",
            message=(
                f"TP price {snapped_tp} must be below the short entry {entry_dec}."
            ),
            position_action=_position_action_result(
                operation="set_tp", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
                price=format(snapped_tp, "f"),
            ),
        )

    # Cancel any existing TP child before placing a new one.
    try:
        stop_children = _get_stop_child_orders(creds["address"])
    except Exception:  # noqa: BLE001
        stop_children = []
    existing_tp, _ = _classify_tpsl_stop_orders(
        stop_children, symbol, side_canonical, entry_dec,
    )
    if existing_tp is not None:
        # Best-effort cancel; a failure here doesn't block the new
        # placement (the server will reject the duplicate leg anyway).
        _cancel_stop_child(creds, existing_tp)

    # Submit the new TP via the dedicated /positions/tpsl endpoint.
    # We submit ONLY the take_profit leg and pass the closing side
    # of the position (the side a TP/SL child order would take when
    # the trigger fires): long → ask, short → bid. Pacifica
    # preserves any existing SL on the same (symbol, side) position
    # when we don't include a stop_loss object.
    client_order_id = str(uuid.uuid4())
    payload = {
        "symbol": symbol,
        "side": PACIFICA_SIDE_FROM_POSITION_TO_PACIFICA[
            "short" if side_canonical == "long" else "long"
        ],
        "take_profit": {
            "stop_price": format(snapped_tp, "f"),
            "client_order_id": client_order_id,
            "trigger_price_type": PACIFICA_TPSL_TRIGGER_PRICE_TYPE,
            # No `amount` → uses the full position size.
            # No `limit_price` → triggers a market order at the
            # trigger (per Pacifica docs).
        },
    }
    try:
        _post_signed_ack(creds, "/positions/tpsl", "set_position_tpsl", payload)
    except _PacificaHTTPError as exc:
        body = (exc.body or "").lower()
        if exc.status in (401, 403) and (
            "agent" in body or "permission" in body or "signature" in body
        ):
            return make_failure(
                operation="set_tp", exchange=name, account=creds["account"],
                code="AGENT_NOT_BOUND",
                message=(
                    "Pacifica rejected the signature. The agent key is "
                    "probably not bound to the main account yet."
                ),
                position_action=_position_action_result(
                    operation="set_tp", symbol=symbol, verified=False,
                    status="failed",
                    current_side=side_canonical,
                    current_size=format(size_dec, "f"),
                    price=format(snapped_tp, "f"),
                ),
            )
        return make_failure(
            operation="set_tp", exchange=name, account=creds["account"],
            code="TP_PLACEMENT_FAILED",
            message=sanitize_error_message(
                f"HTTP {exc.status} on {exc.path}: {exc.body[:200]}"
            ),
            position_action=_position_action_result(
                operation="set_tp", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
                price=format(snapped_tp, "f"),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="set_tp", exchange=name, account=creds["account"],
            code="TP_PLACEMENT_FAILED",
            message=sanitize_error_message(str(exc)),
            position_action=_position_action_result(
                operation="set_tp", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
                price=format(snapped_tp, "f"),
            ),
        )

    # Verify: re-read the stop children and confirm one is a TP at
    # (or very near) the requested price.
    verified = False
    verified_price: Optional[str] = None
    for attempt in range(PACIFICA_LADDER_VERIFY_ATTEMPTS):
        try:
            new_stop_children = _get_stop_child_orders(creds["address"])
        except Exception:  # noqa: BLE001
            new_stop_children = []
        new_tp, _ = _classify_tpsl_stop_orders(
            new_stop_children, symbol, side_canonical, entry_dec,
        )
        if new_tp is not None:
            try:
                tp_now = Decimal(str(new_tp.get("stop_price") or "0"))
            except Exception:  # noqa: BLE001
                tp_now = Decimal("0")
            if abs(tp_now - snapped_tp) <= tick_size:
                verified = True
                verified_price = format(tp_now, "f")
                break
        if attempt < PACIFICA_LADDER_VERIFY_ATTEMPTS - 1:
            time.sleep(PACIFICA_LADDER_VERIFY_DELAY_SECONDS * (attempt + 1))

    if verified:
        return make_success(
            operation="set_tp", exchange=name, account=creds["account"],
            position_action=_position_action_result(
                operation="set_tp", symbol=symbol, verified=True,
                status="success",
                price=verified_price or format(snapped_tp, "f"),
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
                message=f"TP set at {verified_price or format(snapped_tp, 'f')}.",
            ),
        )
    return make_failure(
        operation="set_tp", exchange=name, account=creds["account"],
        code="VERIFICATION_FAILED",
        message=(
            f"TP submission was acknowledged but the stop child at "
            f"~{format(snapped_tp, 'f')} was not visible after "
            f"{PACIFICA_LADDER_VERIFY_ATTEMPTS} snapshot attempts."
        ),
        position_action=_position_action_result(
            operation="set_tp", symbol=symbol, verified=False,
            status="partial",
            current_side=side_canonical,
            current_size=format(size_dec, "f"),
            price=format(snapped_tp, "f"),
        ),
    )


def _execute_set_sl(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Set or remove the stop-loss leg of an open position.

    Symmetric to ``_execute_set_tp`` but for stop-loss: validation
    uses the loss side of entry, the leg type is ``stop_loss`` in the
    /positions/tpsl body, and the verify pass looks for a child
    stop order whose trigger is on the loss side of entry.
    """
    creds = _lookup_credentials(account)
    if creds is None:
        return make_failure(
            operation="set_sl", exchange=name, account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or incomplete Pacifica account credentials.",
        )

    symbol = str(request.get("symbol") or "").strip().upper()
    price_text = str(request.get("price") or "").strip()
    if not symbol:
        return make_failure(
            operation="set_sl", exchange=name, account=account,
            code="MISSING_SYMBOL", message="Symbol is required.",
        )
    try:
        sl_price = Decimal(price_text) if price_text else Decimal("0")
    except Exception:
        return make_failure(
            operation="set_sl", exchange=name, account=account,
            code="INVALID_SL_PRICE", message="SL price must be numeric.",
        )

    # --- locate the open position ---
    try:
        positions = _get_positions(creds["address"])
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="set_sl", exchange=name, account=account,
            code="POSITIONS_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )
    pos = _find_position(positions, symbol)
    if pos is None:
        return make_failure(
            operation="set_sl", exchange=name, account=account,
            code="NO_OPEN_POSITION",
            message=f"No open position for {symbol}.",
            position_action=_position_action_result(
                operation="set_sl", symbol=symbol, verified=False,
                status="failed",
            ),
        )
    raw_side = str(pos.get("side") or "").strip().lower()
    side_canonical = "long" if raw_side == "bid" else "short"
    try:
        size_dec = Decimal(str(pos.get("amount") or "0"))
        entry_dec = Decimal(str(pos.get("entry_price") or "0"))
    except Exception:  # noqa: BLE001
        return make_failure(
            operation="set_sl", exchange=name, account=account,
            code="POSITION_METADATA_INVALID",
            message=f"Position {symbol} has unparseable amount/entry_price.",
            position_action=_position_action_result(
                operation="set_sl", symbol=symbol, verified=False,
                status="failed",
            ),
        )

    # --- remove path: price <= 0 → cancel the existing SL child ---
    if sl_price <= 0:
        try:
            stop_children = _get_stop_child_orders(creds["address"])
        except Exception:  # noqa: BLE001
            stop_children = []
        _, existing_sl = _classify_tpsl_stop_orders(
            stop_children, symbol, side_canonical, entry_dec,
        )
        if existing_sl is None:
            return make_success(
                operation="set_sl", exchange=name, account=creds["account"],
                position_action=_position_action_result(
                    operation="set_sl", symbol=symbol, verified=True,
                    status="removed", removed=True,
                    current_side=side_canonical,
                    current_size=format(size_dec, "f"),
                    price="0",
                    message="No existing SL to cancel.",
                ),
            )
        ok, reason = _cancel_stop_child(creds, existing_sl)
        if not ok:
            return make_failure(
                operation="set_sl", exchange=name, account=creds["account"],
                code="SL_CANCEL_FAILED",
                message=f"Failed to cancel existing SL order: {reason}",
                position_action=_position_action_result(
                    operation="set_sl", symbol=symbol, verified=False,
                    status="failed",
                    current_side=side_canonical,
                    current_size=format(size_dec, "f"),
                    price=str(existing_sl.get("stop_price") or "0"),
                ),
            )
        return make_success(
            operation="set_sl", exchange=name, account=creds["account"],
            position_action=_position_action_result(
                operation="set_sl", symbol=symbol, verified=True,
                status="removed", removed=True,
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
                price="0",
                message=f"Cancelled existing SL order {existing_sl.get('order_id')}.",
            ),
        )

    # --- set path: snap, validate direction, submit ---
    try:
        market = _get_market_info(symbol)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="set_sl", exchange=name, account=creds["account"],
            code="MARKET_INFO_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
            position_action=_position_action_result(
                operation="set_sl", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
            ),
        )
    if market is None:
        return make_failure(
            operation="set_sl", exchange=name, account=creds["account"],
            code="INSTRUMENT_NOT_FOUND",
            message=f"Symbol '{symbol}' is not listed on Pacifica.",
            position_action=_position_action_result(
                operation="set_sl", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
            ),
        )
    tick_size = _decimal_or_zero(market.get("tick_size"))
    if tick_size <= 0:
        return make_failure(
            operation="set_sl", exchange=name, account=creds["account"],
            code="MARKET_METADATA_INVALID",
            message=f"Pacifica market '{symbol}' has invalid tick_size.",
            position_action=_position_action_result(
                operation="set_sl", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
            ),
        )
    snapped_sl = _quantize_to_step(sl_price, tick_size)
    if snapped_sl <= 0:
        return make_failure(
            operation="set_sl", exchange=name, account=creds["account"],
            code="INVALID_SL_PRICE",
            message=f"SL price {sl_price} rounds to zero at tick {tick_size}.",
            position_action=_position_action_result(
                operation="set_sl", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
                price="0",
            ),
        )
    if side_canonical == "long" and snapped_sl >= entry_dec:
        return make_failure(
            operation="set_sl", exchange=name, account=creds["account"],
            code="INVALID_SL_DIRECTION",
            message=(
                f"SL price {snapped_sl} must be below the long entry {entry_dec}."
            ),
            position_action=_position_action_result(
                operation="set_sl", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
                price=format(snapped_sl, "f"),
            ),
        )
    if side_canonical == "short" and snapped_sl <= entry_dec:
        return make_failure(
            operation="set_sl", exchange=name, account=creds["account"],
            code="INVALID_SL_DIRECTION",
            message=(
                f"SL price {snapped_sl} must be above the short entry {entry_dec}."
            ),
            position_action=_position_action_result(
                operation="set_sl", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
                price=format(snapped_sl, "f"),
            ),
        )

    # Cancel any existing SL child before placing a new one.
    try:
        stop_children = _get_stop_child_orders(creds["address"])
    except Exception:  # noqa: BLE001
        stop_children = []
    _, existing_sl = _classify_tpsl_stop_orders(
        stop_children, symbol, side_canonical, entry_dec,
    )
    if existing_sl is not None:
        _cancel_stop_child(creds, existing_sl)

    client_order_id = str(uuid.uuid4())
    payload = {
        "symbol": symbol,
        "side": PACIFICA_SIDE_FROM_POSITION_TO_PACIFICA[
            "short" if side_canonical == "long" else "long"
        ],
        "stop_loss": {
            "stop_price": format(snapped_sl, "f"),
            "client_order_id": client_order_id,
            "trigger_price_type": PACIFICA_TPSL_TRIGGER_PRICE_TYPE,
        },
    }
    try:
        _post_signed_ack(creds, "/positions/tpsl", "set_position_tpsl", payload)
    except _PacificaHTTPError as exc:
        body = (exc.body or "").lower()
        if exc.status in (401, 403) and (
            "agent" in body or "permission" in body or "signature" in body
        ):
            return make_failure(
                operation="set_sl", exchange=name, account=creds["account"],
                code="AGENT_NOT_BOUND",
                message=(
                    "Pacifica rejected the signature. The agent key is "
                    "probably not bound to the main account yet."
                ),
                position_action=_position_action_result(
                    operation="set_sl", symbol=symbol, verified=False,
                    status="failed",
                    current_side=side_canonical,
                    current_size=format(size_dec, "f"),
                    price=format(snapped_sl, "f"),
                ),
            )
        return make_failure(
            operation="set_sl", exchange=name, account=creds["account"],
            code="SL_PLACEMENT_FAILED",
            message=sanitize_error_message(
                f"HTTP {exc.status} on {exc.path}: {exc.body[:200]}"
            ),
            position_action=_position_action_result(
                operation="set_sl", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
                price=format(snapped_sl, "f"),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="set_sl", exchange=name, account=creds["account"],
            code="SL_PLACEMENT_FAILED",
            message=sanitize_error_message(str(exc)),
            position_action=_position_action_result(
                operation="set_sl", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
                price=format(snapped_sl, "f"),
            ),
        )

    # Verify
    verified = False
    verified_price: Optional[str] = None
    for attempt in range(PACIFICA_LADDER_VERIFY_ATTEMPTS):
        try:
            new_stop_children = _get_stop_child_orders(creds["address"])
        except Exception:  # noqa: BLE001
            new_stop_children = []
        _, new_sl = _classify_tpsl_stop_orders(
            new_stop_children, symbol, side_canonical, entry_dec,
        )
        if new_sl is not None:
            try:
                sl_now = Decimal(str(new_sl.get("stop_price") or "0"))
            except Exception:  # noqa: BLE001
                sl_now = Decimal("0")
            if abs(sl_now - snapped_sl) <= tick_size:
                verified = True
                verified_price = format(sl_now, "f")
                break
        if attempt < PACIFICA_LADDER_VERIFY_ATTEMPTS - 1:
            time.sleep(PACIFICA_LADDER_VERIFY_DELAY_SECONDS * (attempt + 1))

    if verified:
        return make_success(
            operation="set_sl", exchange=name, account=creds["account"],
            position_action=_position_action_result(
                operation="set_sl", symbol=symbol, verified=True,
                status="success",
                price=verified_price or format(snapped_sl, "f"),
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
                message=f"SL set at {verified_price or format(snapped_sl, 'f')}.",
            ),
        )
    return make_failure(
        operation="set_sl", exchange=name, account=creds["account"],
        code="VERIFICATION_FAILED",
        message=(
            f"SL submission was acknowledged but the stop child at "
            f"~{format(snapped_sl, 'f')} was not visible after "
            f"{PACIFICA_LADDER_VERIFY_ATTEMPTS} snapshot attempts."
        ),
        position_action=_position_action_result(
            operation="set_sl", symbol=symbol, verified=False,
            status="partial",
            current_side=side_canonical,
            current_size=format(size_dec, "f"),
            price=format(snapped_sl, "f"),
        ),
    )


def _execute_close_position(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Close the entire open position for ``symbol`` at market.

    Pacifica has no dedicated "close position" endpoint, so we
    submit a market order in the OPPOSITE side with
    ``reduce_only=true`` and the full position size. The exchange
    imposes a ~200ms delay on market orders to protect liquidity
    providers — verification polls ``/positions`` until the
    position is gone (or the timeout elapses).

    Slippage tolerance is set to 1% (configurable above). For
    high-leverage positions on a thin book this can be too tight
    and the close will fail; for normal positions on a deep book
    it's a generous ceiling.
    """
    creds = _lookup_credentials(account)
    if creds is None:
        return make_failure(
            operation="close_position", exchange=name, account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or incomplete Pacifica account credentials.",
        )

    symbol = str(request.get("symbol") or "").strip().upper()
    if not symbol:
        return make_failure(
            operation="close_position", exchange=name, account=account,
            code="MISSING_SYMBOL", message="Symbol is required.",
            position_action=_position_action_result(
                operation="close_position", symbol=symbol, verified=False,
                status="failed",
            ),
        )

    # --- locate the position ---
    try:
        positions = _get_positions(creds["address"])
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="close_position", exchange=name, account=account,
            code="POSITIONS_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )
    pos = _find_position(positions, symbol)
    if pos is None:
        # Already-flat is a verified no-op success.
        return make_success(
            operation="close_position", exchange=name, account=creds["account"],
            position_action=_position_action_result(
                operation="close_position", symbol=symbol, verified=True,
                status="success",
                current_side="",
                current_size="0",
                message=f"No open position for {symbol}; nothing to close.",
            ),
        )

    raw_side = str(pos.get("side") or "").strip().lower()
    side_canonical = "long" if raw_side == "bid" else "short"
    try:
        size_dec = abs(Decimal(str(pos.get("amount") or "0")))
    except Exception:  # noqa: BLE001
        return make_failure(
            operation="close_position", exchange=name, account=creds["account"],
            code="POSITION_METADATA_INVALID",
            message=f"Position {symbol} has unparseable amount.",
            position_action=_position_action_result(
                operation="close_position", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
            ),
        )
    if size_dec <= 0:
        return make_failure(
            operation="close_position", exchange=name, account=creds["account"],
            code="INVALID_POSITION_SIZE",
            message=f"Position {symbol} has zero size; nothing to close.",
            position_action=_position_action_result(
                operation="close_position", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size="0",
            ),
        )

    # Snap the size to the market lot.
    try:
        market = _get_market_info(symbol)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="close_position", exchange=name, account=creds["account"],
            code="MARKET_INFO_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )
    if market is None:
        return make_failure(
            operation="close_position", exchange=name, account=creds["account"],
            code="INSTRUMENT_NOT_FOUND",
            message=f"Symbol '{symbol}' is not listed on Pacifica.",
        )
    lot_size = _decimal_or_zero(market.get("lot_size"))
    if lot_size <= 0:
        return make_failure(
            operation="close_position", exchange=name, account=creds["account"],
            code="MARKET_METADATA_INVALID",
            message=f"Pacifica market '{symbol}' has invalid lot_size.",
        )
    closed_size = _quantize_to_step(size_dec, lot_size)
    if closed_size <= 0:
        return make_failure(
            operation="close_position", exchange=name, account=creds["account"],
            code="INVALID_POSITION_SIZE",
            message=(
                f"Position size {size_dec} rounds to zero at lot "
                f"{lot_size}; cannot close."
            ),
            position_action=_position_action_result(
                operation="close_position", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
            ),
        )

    # Close = submit market in the OPPOSITE side, reduce-only.
    opposite_side = PACIFICA_SIDE_FROM_POSITION_TO_PACIFICA[
        "short" if side_canonical == "long" else "long"
    ]
    client_order_id = str(uuid.uuid4())
    payload = {
        "symbol": symbol,
        "side": opposite_side,
        "amount": _format_market_value(closed_size, lot_size),
        "slippage_percent": PACIFICA_CLOSE_SLIPPAGE_PERCENT,
        "reduce_only": True,
        "client_order_id": client_order_id,
    }
    try:
        response_data = _post_signed(
            credentials=creds, path="/orders/create_market",
            operation_type="create_market_order", payload=payload,
        )
    except _PacificaHTTPError as exc:
        body = (exc.body or "").lower()
        if exc.status in (401, 403) and (
            "agent" in body or "permission" in body or "signature" in body
        ):
            return make_failure(
                operation="close_position", exchange=name, account=creds["account"],
                code="AGENT_NOT_BOUND",
                message=(
                    "Pacifica rejected the signature. The agent key is "
                    "probably not bound to the main account yet."
                ),
                position_action=_position_action_result(
                    operation="close_position", symbol=symbol, verified=False,
                    status="failed",
                    current_side=side_canonical,
                    current_size=format(size_dec, "f"),
                ),
            )
        return make_failure(
            operation="close_position", exchange=name, account=creds["account"],
            code="CLOSE_SUBMISSION_FAILED",
            message=sanitize_error_message(
                f"HTTP {exc.status} on {exc.path}: {exc.body[:200]}"
            ),
            position_action=_position_action_result(
                operation="close_position", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="close_position", exchange=name, account=creds["account"],
            code="CLOSE_SUBMISSION_FAILED",
            message=sanitize_error_message(str(exc)),
            position_action=_position_action_result(
                operation="close_position", symbol=symbol, verified=False,
                status="failed",
                current_side=side_canonical,
                current_size=format(size_dec, "f"),
            ),
        )

    # The market-order response carries the order id; surface it.
    raw_id = response_data.get("order_id") if isinstance(response_data, dict) else None
    try:
        exchange_order_id: Optional[int] = int(str(raw_id)) if raw_id is not None else None
    except (TypeError, ValueError):
        exchange_order_id = None

    # Verify: poll /positions until the position is gone.
    verified = False
    last_size_text = format(size_dec, "f")
    for attempt in range(PACIFICA_CLOSE_VERIFY_ATTEMPTS):
        try:
            new_positions = _get_positions(creds["address"])
        except Exception:  # noqa: BLE001
            new_positions = []
        new_pos = _find_position(new_positions, symbol)
        if new_pos is None:
            verified = True
            last_size_text = "0"
            break
        try:
            new_size = abs(Decimal(str(new_pos.get("amount") or "0")))
        except Exception:  # noqa: BLE001
            new_size = size_dec
        last_size_text = format(new_size, "f")
        if new_size == Decimal("0"):
            verified = True
            break
        if attempt < PACIFICA_CLOSE_VERIFY_ATTEMPTS - 1:
            time.sleep(PACIFICA_CLOSE_VERIFY_DELAY_SECONDS * (attempt + 1))

    if verified:
        return make_success(
            operation="close_position", exchange=name, account=creds["account"],
            position_action=_position_action_result(
                operation="close_position", symbol=symbol, verified=True,
                status="success",
                current_side="",
                current_size="0",
                exchange_order_id=exchange_order_id,
                message=(
                    f"Closed {format(closed_size, 'f')} {symbol} via market order"
                    + (f" id={exchange_order_id}." if exchange_order_id else ".")
                ),
            ),
        )
    return make_failure(
        operation="close_position", exchange=name, account=creds["account"],
        code="VERIFICATION_FAILED",
        message=(
            f"Close order was submitted but {symbol} still shows "
            f"size {last_size_text} after {PACIFICA_CLOSE_VERIFY_ATTEMPTS} "
            f"snapshot attempts. The market order may still be filling."
        ),
        position_action=_position_action_result(
            operation="close_position", symbol=symbol, verified=False,
            status="partial",
            current_side=side_canonical,
            current_size=last_size_text,
            exchange_order_id=exchange_order_id,
        ),
    )


# ---------------------------------------------------------------------------
# Quantisation helpers
# ---------------------------------------------------------------------------

def _decimal_or_zero(value: Any) -> Decimal:
    """Best-effort Decimal parse. Returns 0 on any failure (missing, empty, bad)."""
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _quantize_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Floor ``value`` to the nearest multiple of ``step``.

    Pacifica's tick/lot sizes are positive; this is the standard
    ``value - (value % step)`` rounding used by every other exchange
    agent. We never round away from zero — a too-small volume becomes
    zero and gets caught by the post-quantization size check.
    """
    if step <= 0:
        raise ValueError("step must be positive")
    if value <= 0:
        return Decimal("0")
    quotient, remainder = divmod(value, step)
    if remainder == 0:
        return quotient * step
    return quotient * step


def _decimal_places(value: Any) -> int:
    """Return the number of fractional digits in a Decimal or string.

    Mirrors the helper in every other agent. Used to format prices and
    volumes at the exact precision the market expects (BTC ticks at 1
    USD = 0 decimals; kBONK ticks at 0.000001 = 6 decimals).
    """
    try:
        decimal_value = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return 0
    sign, digits, exponent = decimal_value.normalize().as_tuple()
    exponent = int(exponent)
    if exponent >= 0:
        return 0
    return -exponent


def _format_market_value(value: Decimal, step: Decimal) -> str:
    """Format a quantized value as a decimal string matching the market precision."""
    places = _decimal_places(step)
    quantum = Decimal("1").scaleb(-places)
    quantized = value.quantize(quantum, rounding=ROUND_HALF_UP)
    if places <= 0:
        return format(quantized, "f")
    return format(quantized, f".{places}f")


# ---------------------------------------------------------------------------
# Order verification
# ---------------------------------------------------------------------------

def _verify_order_submission(
    credentials: Dict[str, str],
    client_order_id: str,
) -> Tuple[bool, Optional[int]]:
    """Confirm a submitted order is visible in the user's order list.

    Pacifica's create-order response is acknowledged as soon as the
    matching engine accepts the order, but the order shows up in the
    snapshot endpoint (``/orders``) on a near-immediate but not
    necessarily synchronous timeline. We poll up to
    ``PACIFICA_VERIFY_ATTEMPTS`` times with a small delay between
    attempts; if the CLOID never appears, we return ``(False, None)``
    rather than blocking the wizard.

    Returns ``(verified, exchange_order_id)``. The exchange_order_id
    is the Pacifica-assigned integer ID; it's ``None`` if the order
    isn't visible (whether because the CLOID didn't match or the
    snapshot hasn't caught up yet).
    """
    last_exchange_id: Optional[int] = None
    for attempt in range(PACIFICA_VERIFY_ATTEMPTS):
        try:
            payload = _http_get_json(
                f"{_api_base()}/orders",
                params={"account": credentials["address"]},
            )
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, list):
                for row in data:
                    if not isinstance(row, dict):
                        continue
                    if str(row.get("client_order_id") or "").strip() == client_order_id:
                        raw_id = row.get("order_id")
                        try:
                            last_exchange_id = int(str(raw_id)) if raw_id is not None else None
                        except (TypeError, ValueError):
                            last_exchange_id = None
                        return True, last_exchange_id
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Pacifica order verification attempt %d/%d failed: %s",
                attempt + 1,
                PACIFICA_VERIFY_ATTEMPTS,
                exc,
            )
        if attempt < PACIFICA_VERIFY_ATTEMPTS - 1:
            time.sleep(PACIFICA_VERIFY_DELAY_SECONDS)
    return False, last_exchange_id


# ---------------------------------------------------------------------------
# Open orders (read-only, used by the positions_orders screen)
# ---------------------------------------------------------------------------

# How we decide whether a row from /orders is "truly open".
#
# Pacifica's ``/orders`` endpoint returns the user's full order history
# by default. We treat an order as open when:
#   - the order type is "limit"  (stop / TP / SL orders have their
#     own channels and aren't meant for the resting-order view), and
#   - the order is not linked to a stop parent  (``stop_parent_order_id
#     is null``), and
#   - the filled + cancelled amount is strictly less than the initial
#     amount (so a fully filled or fully cancelled order is excluded).
#
# Pacifica does not return a separate "status" field, so we derive one
# from the amount counters. This matches the "what's actually on the
# book right now" question the wizard asks.
def _is_open_limit_order(row: Dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    order_type = str(row.get("order_type") or "").strip().lower()
    if order_type != PACIFICA_LIMIT_ORDER_TYPE:
        return False
    if row.get("stop_parent_order_id") not in (None, "", "0"):
        return False
    try:
        initial = Decimal(str(row.get("initial_amount") or 0))
        filled = Decimal(str(row.get("filled_amount") or 0))
        cancelled = Decimal(str(row.get("cancelled_amount") or 0))
    except Exception:  # noqa: BLE001
        return False
    return (filled + cancelled) < initial


def _get_open_orders(address: str) -> List[Dict[str, Any]]:
    """Fetch ``GET /api/v1/orders?account=...`` and return the open rows.

    Pacifica returns the full history; this filters down to live
    resting limit orders via ``_is_open_limit_order``. The list comes
    back in reverse-chronological order from the API; we keep that
    ordering since it's already what a user wants to see (newest
    first).
    """
    payload = _http_get_json(f"{_api_base()}/orders", params={"account": address})
    if not isinstance(payload, dict):
        raise RuntimeError("Pacifica /orders returned a non-object payload")
    if payload.get("success") is False:
        code = payload.get("code")
        message = payload.get("error") or payload.get("message") or "Pacifica /orders failed"
        raise RuntimeError(f"{message} (code={code})")
    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError("Pacifica /orders response missing data list")
    return [row for row in data if _is_open_limit_order(row)]


def _aggregate_open_orders(orders: List[Dict[str, Any]]) -> List[CanonicalOrderGroup]:
    """Group open orders by ``(symbol, side)`` and produce aggregated rows.

    For each group we compute:
      - ``order_count``     : number of open limit orders in the group
      - ``total_size``      : sum of remaining (unfilled, not-cancelled)
                              initial amounts, in base-asset units
      - ``vwap``            : size-weighted average of the limit prices
                              across the group
      - ``min_price``       : smallest limit price in the group
      - ``max_price``       : largest limit price in the group
      - ``side``            : canonical long/short (bid -> long, ask -> short)

    Pacifica's ``/orders`` returns prices and amounts as decimal
    strings. We parse them through ``Decimal`` to avoid float drift on
    large notionals (413 open orders × arbitrary prices would silently
    lose precision otherwise). For the wizard display, we format prices
    and sizes at the per-market tick / lot precision so a $0.0001 HYPE
    size and a $61590 BTC VWAP both render cleanly.
    """
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for order in orders:
        if not isinstance(order, dict):
            continue
        # Defensive: the aggregator can be called from outside with
        # raw Pacifica rows that include stop / TP / SL orders and
        # fully-filled rows. Apply the same filter the public
        # _get_open_orders uses so the aggregation never accidentally
        # counts those rows.
        if not _is_open_limit_order(order):
            continue
        symbol = str(order.get("symbol") or "").strip().upper()
        raw_side = str(order.get("side") or "").strip().lower()
        if not symbol or raw_side not in ("bid", "ask"):
            continue
        try:
            price = Decimal(str(order.get("price") or 0))
            initial = Decimal(str(order.get("initial_amount") or 0))
            filled = Decimal(str(order.get("filled_amount") or 0))
            cancelled = Decimal(str(order.get("cancelled_amount") or 0))
        except Exception:  # noqa: BLE001
            continue
        remaining = initial - filled - cancelled
        if remaining <= 0 or price <= 0:
            continue
        key = (symbol, raw_side)
        bucket = grouped.setdefault(
            key,
            {
                "symbol": symbol,
                "raw_side": raw_side,
                "order_count": 0,
                "total_size": Decimal("0"),
                "notional": Decimal("0"),
                "min_price": None,
                "max_price": None,
            },
        )
        bucket["order_count"] += 1
        bucket["total_size"] += remaining
        bucket["notional"] += price * remaining
        if bucket["min_price"] is None or price < bucket["min_price"]:
            bucket["min_price"] = price
        if bucket["max_price"] is None or price > bucket["max_price"]:
            bucket["max_price"] = price

    out: List[CanonicalOrderGroup] = []
    for bucket in grouped.values():
        total_size = bucket["total_size"]
        if total_size <= 0:
            continue
        vwap = bucket["notional"] / total_size
        canonical_side = "long" if bucket["raw_side"] == "bid" else "short"

        # Per-market precision. We look up the market once per group;
        # a cache miss or transient failure falls back to the universal
        # `_format_decimal_for_wizard` which is good enough but slightly
        # noisier on long decimals. We never let a market-info failure
        # break the aggregation — the wizard needs the open-orders
        # block even if /info is briefly unavailable.
        size_text = _format_decimal_for_wizard(total_size)
        vwap_text = _format_decimal_for_wizard(vwap)
        min_text = _format_decimal_for_wizard(bucket["min_price"])
        max_text = _format_decimal_for_wizard(bucket["max_price"])
        try:
            market = _get_market_info(bucket["symbol"])
        except Exception:  # noqa: BLE001
            market = None
        if market is not None:
            tick = _decimal_or_zero(market.get("tick_size"))
            lot = _decimal_or_zero(market.get("lot_size"))
            if lot > 0:
                size_text = _format_market_value(total_size, lot)
            if tick > 0:
                vwap_text = _format_market_value(vwap, tick)
                min_text = _format_market_value(bucket["min_price"], tick)
                max_text = _format_market_value(bucket["max_price"], tick)

        out.append(
            CanonicalOrderGroup(
                symbol=bucket["symbol"],
                side=canonical_side,
                order_count=int(bucket["order_count"]),
                total_size=size_text,
                vwap=vwap_text,
                min_price=min_text,
                max_price=max_text,
            )
        )
    # Sort: alphabetical by symbol, with buys before sells within a
    # symbol (so the resting book reads top-down in the wizard).
    out.sort(key=lambda g: (g.symbol, 0 if g.side == "long" else 1))
    return out


def _format_decimal_for_wizard(value: Any) -> str:
    """Format a Decimal as a clean human-readable string (no trailing zeros).

    The wizard's ``_display_or_dash`` doesn't tolerate ``Decimal``
    objects, so we always stringify here. We strip trailing zeros so
    ``1.39000`` becomes ``1.39`` and ``100.000`` becomes ``100`` —
    matches what every other agent passes to the canonical layer.
    """
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception:  # noqa: BLE001
        return str(value)
    if decimal_value.is_nan() or decimal_value.is_infinite():
        return "0"
    rendered = format(decimal_value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".") or "0"
    return rendered


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _require_decimal_string(value: Any, field_name: str) -> str:
    """Coerce a Pacifica decimal-string field to a non-empty string.

    Pacifica returns monetary values as JSON strings (e.g. ``"2150.25"``)
    to preserve precision. We only need to validate that it's a parseable
    decimal — final rounding happens inside ``normalize_balance``.
    """
    text = str(value or "").strip()
    if not text:
        raise RuntimeError(f"Pacifica response missing {field_name}")
    try:
        Decimal(text)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Pacifica response has non-numeric {field_name}: {text!r}") from exc
    return text


def _side_text_to_canonical(side: str) -> str:
    """Pacifica reports position side as ``"bid"`` (long) or ``"ask"`` (short)."""
    text = str(side or "").strip().lower()
    if text == "bid":
        return "long"
    if text == "ask":
        return "short"
    # Defensive: don't crash the wizard on a future value, just pass through.
    return text or "long"


def _build_portfolio_summary(data: Dict[str, Any]) -> CanonicalPortfolioSummary:
    """Translate Pacifica /account fields into the canonical summary.

    Field mapping (per Pacifica docs):

    - ``balance``            → ``account_value`` (USD on the account
      before settlement — Pacifica's authoritative "how much USDC do I
      have right now" number; the wizard presents it as account value).
    - ``account_equity``     → also used as account_value when present
      (equity = balance + uPnL + isolated margin + raw spot value, so
      it's the more comprehensive number). When equity is present we
      prefer it; otherwise we fall back to ``balance``.
    - ``available_to_withdraw`` → ``withdrawable``
    - ``total_margin_used``  → ``margin_used``
    - positions not in this endpoint → ``total_position_value`` is left
      at 0.00 (the wizard can compute it from the positions list when
      needed). The Rise agent does the same.
    """
    balance_text = _require_decimal_string(data.get("balance"), "balance")
    equity_text = data.get("account_equity")
    if equity_text is not None and str(equity_text).strip():
        try:
            account_value = _require_decimal_string(equity_text, "account_equity")
        except RuntimeError:
            account_value = balance_text
    else:
        account_value = balance_text

    withdrawable_text = data.get("available_to_withdraw")
    if withdrawable_text is None or not str(withdrawable_text).strip():
        # Fall back to available_to_spend if the explicit withdrawable
        # number is missing — they normally differ, but either is
        # better than nothing for a balance screen.
        withdrawable_text = data.get("available_to_spend") or "0"
    margin_used_text = data.get("total_margin_used") or "0"

    account_value = normalize_balance(account_value, PACIFICA_BALANCE_UNIT).value
    withdrawable = normalize_balance(withdrawable_text, PACIFICA_BALANCE_UNIT).value
    margin_used = normalize_balance(margin_used_text, PACIFICA_BALANCE_UNIT).value

    return CanonicalPortfolioSummary(
        account_value=account_value,
        withdrawable=withdrawable,
        margin_used=margin_used,
        total_position_value="0.00",
        unit=PACIFICA_BALANCE_UNIT,
    )


def _compute_unrealized_pnl(
    *,
    side_canonical: str,
    size: Decimal,
    entry_price: Decimal,
    mark_price: Optional[Decimal],
) -> Decimal:
    """Return the position's unrealized PnL in the account unit (USDC).

    Formula:
        long  : (mark - entry) * size
        short : (entry - mark) * size

    If we don't have a mark price (e.g. Pacifica temporarily missing the
    symbol from ``/info/prices``), we return 0 rather than raising —
    the position is still real, we just can't quote its running P&L. The
    wizard renders 0 PnL honestly as "0.00", which is more useful than
    crashing the balance screen.
    """
    if mark_price is None:
        return Decimal("0")
    if side_canonical == "long":
        return (mark_price - entry_price) * size
    if side_canonical == "short":
        return (entry_price - mark_price) * size
    # Unknown side — fail safe.
    return Decimal("0")


def _format_pnl(value: Decimal) -> str:
    """Format a PnL value as a 2dp string, matching the wizard's display."""
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _normalize_positions(
    rows: List[Dict[str, Any]],
    mark_by_symbol: Dict[str, Decimal],
) -> List[CanonicalPosition]:
    """Translate Pacifica /positions rows into the canonical position list.

    The ``pnl`` field is set to **unrealized PnL** computed as
    ``(mark - entry) * size`` for longs or ``(entry - mark) * size`` for
    shorts, using the mark price from ``/info/prices``. This matches
    the convention used by every other exchange agent — the canonical
    layer's ``CanonicalPosition.pnl`` is always the live unrealized
    number, never a historical accounting figure.

    Pacifica *does* return a per-position ``funding`` field, but that's
    the cumulative funding cost paid since the position opened — it
    belongs in the realized-PnL bucket, not the running PnL. We
    deliberately drop it from the canonical row; the account-level
    `account_equity` already reflects it.
    """
    out: List[CanonicalPosition] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue
        side = _side_text_to_canonical(row.get("side", ""))
        try:
            size_dec = Decimal(_require_decimal_string(row.get("amount"), f"positions[{symbol}].amount"))
            entry_dec = Decimal(_require_decimal_string(row.get("entry_price"), f"positions[{symbol}].entry_price"))
        except (RuntimeError, Exception):  # noqa: BLE001
            # Skip malformed rows rather than crash the whole balance.
            continue
        mark = mark_by_symbol.get(symbol)
        pnl = _compute_unrealized_pnl(
            side_canonical=side,
            size=size_dec,
            entry_price=entry_dec,
            mark_price=mark,
        )
        out.append(
            CanonicalPosition(
                symbol=symbol,
                side=side,
                size=format(size_dec, "f"),
                entry_price=format(entry_dec, "f"),
                pnl=_format_pnl(pnl),
            )
        )
    return out


# ---------------------------------------------------------------------------
# New Order
# ---------------------------------------------------------------------------


def _execute_new_order(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Place a limit order on Pacifica and return a ``CanonicalOrderResult``.

    Request fields (per the /trade wizard contract):
      - ``symbol`` (str, required, uppercase) — Pacifica instrument
      - ``side`` (str, required, ``"buy"`` or ``"sell"``)
      - ``order_type`` (str, optional, default ``"limit"``)
      - ``volume`` (str, required) — base-asset units (e.g. BTC)
      - ``price`` (str, required) — limit price in USD
      - ``time_in_force`` (str, optional, default ``"GTC"``)
      - ``reduce_only`` (bool, optional, default False)
      - ``client_order_id`` (UUID str, optional) — caller-supplied CLOID;
        we always generate our own UUID on top, so the caller value is
        ignored (the wizard doesn't currently expose it).

    Validation order: auth → inputs → market metadata → quantisation →
    notional floor → signing → POST → verification.
    """
    creds = _lookup_credentials(account)
    if creds is None:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or incomplete Pacifica account credentials.",
        )

    # --- 1. input parsing ---
    requested_symbol = str(request.get("symbol") or "").strip().upper()
    requested_side = str(request.get("side") or "").strip().lower()
    order_type = str(request.get("order_type") or PACIFICA_LIMIT_ORDER_TYPE).strip().lower() or PACIFICA_LIMIT_ORDER_TYPE
    requested_volume_text = str(request.get("volume") or request.get("size") or "").strip()
    requested_price_text = str(request.get("price") or "").strip()
    requested_tif = str(request.get("time_in_force") or PACIFICA_DEFAULT_TIF).strip().upper() or PACIFICA_DEFAULT_TIF
    reduce_only = _coerce_bool_safe(request.get("reduce_only"))

    if not requested_symbol:
        return make_failure(
            operation="new_order", exchange=name, account=account,
            code="MISSING_SYMBOL", message="Symbol is required.",
        )
    if requested_side not in PACIFICA_CANONICAL_SIDES:
        return make_failure(
            operation="new_order", exchange=name, account=account,
            code="INVALID_SIDE", message="Side must be 'buy' or 'sell'.",
        )
    if order_type not in PACIFICA_SUPPORTED_ORDER_TYPES:
        return make_failure(
            operation="new_order", exchange=name, account=account,
            code="INVALID_ORDER_TYPE",
            message=f"Only {sorted(PACIFICA_SUPPORTED_ORDER_TYPES)} order types are supported.",
        )
    if requested_tif not in PACIFICA_VALID_TIFS:
        return make_failure(
            operation="new_order", exchange=name, account=account,
            code="INVALID_TIME_IN_FORCE",
            message=f"Time in force must be one of {sorted(PACIFICA_VALID_TIFS)}.",
        )
    try:
        requested_volume = Decimal(requested_volume_text)
    except Exception:
        return make_failure(
            operation="new_order", exchange=name, account=account,
            code="INVALID_VOLUME", message="Volume must be a positive number.",
        )
    try:
        requested_price = Decimal(requested_price_text)
    except Exception:
        return make_failure(
            operation="new_order", exchange=name, account=account,
            code="INVALID_PRICE", message="Price must be a positive number.",
        )
    if requested_volume <= 0:
        return make_failure(
            operation="new_order", exchange=name, account=account,
            code="INVALID_VOLUME", message="Volume must be positive.",
        )
    if requested_price <= 0:
        return make_failure(
            operation="new_order", exchange=name, account=account,
            code="INVALID_PRICE", message="Price must be positive.",
        )

    # --- 2. market metadata + quantisation ---
    try:
        market = _get_market_info(requested_symbol)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="new_order", exchange=name, account=account,
            code="MARKET_INFO_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )
    if market is None:
        return make_failure(
            operation="new_order", exchange=name, account=account,
            code="INSTRUMENT_NOT_FOUND",
            message=f"Symbol '{requested_symbol}' is not listed on Pacifica.",
        )

    tick_size = _decimal_or_zero(market.get("tick_size"))
    lot_size = _decimal_or_zero(market.get("lot_size"))
    if tick_size <= 0 or lot_size <= 0:
        return make_failure(
            operation="new_order", exchange=name, account=account,
            code="MARKET_METADATA_INVALID",
            message=f"Pacifica market '{requested_symbol}' returned invalid tick/lot size.",
        )

    submitted_price = _quantize_to_step(requested_price, tick_size)
    submitted_volume = _quantize_to_step(requested_volume, lot_size)
    if submitted_price <= 0:
        return make_failure(
            operation="new_order", exchange=name, account=account,
            code="INVALID_PRICE",
            message="Price rounds down to zero at this tick size; raise your price.",
            order=CanonicalOrderResult(
                symbol=requested_symbol, side=requested_side, order_type=order_type,
                requested_volume=_format_market_value(requested_volume, lot_size),
                requested_price=_format_market_value(requested_price, tick_size),
                submitted_volume="0",
                submitted_price="0",
                verified=False, status="failed",
            ),
        )
    if submitted_volume <= 0:
        return make_failure(
            operation="new_order", exchange=name, account=account,
            code="INVALID_VOLUME",
            message="Volume rounds down to zero at this lot size; raise your size.",
            order=CanonicalOrderResult(
                symbol=requested_symbol, side=requested_side, order_type=order_type,
                requested_volume=_format_market_value(requested_volume, lot_size),
                requested_price=_format_market_value(requested_price, tick_size),
                submitted_volume="0",
                submitted_price=_format_market_value(submitted_price, tick_size),
                verified=False, status="failed",
            ),
        )

    # --- 3. notional floor (Pacifica requires >= $10 USD) ---
    notional_usd = submitted_price * submitted_volume
    if notional_usd < PACIFICA_MIN_NOTIONAL_USD:
        return make_failure(
            operation="new_order", exchange=name, account=account,
            code="BELOW_MIN_NOTIONAL",
            message=(
                f"Order notional ~${notional_usd:.2f} is below the Pacifica "
                f"minimum of ${PACIFICA_MIN_NOTIONAL_USD:.2f}."
            ),
            order=CanonicalOrderResult(
                symbol=requested_symbol, side=requested_side, order_type=order_type,
                requested_volume=_format_market_value(requested_volume, lot_size),
                requested_price=_format_market_value(requested_price, tick_size),
                submitted_volume=_format_market_value(submitted_volume, lot_size),
                submitted_price=_format_market_value(submitted_price, tick_size),
                verified=False, status="failed",
            ),
        )

    # --- 4. build the signed payload and POST ---
    client_order_id = str(uuid.uuid4())
    pacifica_side = PACIFICA_SIDE_TO_PACIFICA[requested_side]
    payload = {
        "symbol": requested_symbol,
        "price": _format_market_value(submitted_price, tick_size),
        "reduce_only": reduce_only,
        "amount": _format_market_value(submitted_volume, lot_size),
        "side": pacifica_side,
        "tif": requested_tif,
        "client_order_id": client_order_id,
    }

    try:
        response_data = _post_signed(creds, "/orders/create", "create_order", payload)
    except _PacificaHTTPError as exc:
        # A 401 / 403 with a "agent" or "permission" hint means the
        # agent key isn't bound to this account yet. Surface that
        # specifically so the user knows to run bind_agent_wallet.
        body = (exc.body or "").lower()
        if exc.status in (401, 403) and ("agent" in body or "permission" in body or "signature" in body):
            return make_failure(
                operation="new_order", exchange=name, account=account,
                code="AGENT_NOT_BOUND",
                message=(
                    "Pacifica rejected the signature. The agent key is "
                    "probably not bound to the main account yet — run "
                    "Pacifica's bind_agent_wallet operation once via the "
                    "main wallet, then retry."
                ),
            )
        return make_failure(
            operation="new_order", exchange=name, account=account,
            code="ORDER_SUBMISSION_FAILED",
            message=sanitize_error_message(f"HTTP {exc.status} on {exc.path}: {exc.body[:200]}"),
            order=CanonicalOrderResult(
                symbol=requested_symbol, side=requested_side, order_type=order_type,
                requested_volume=_format_market_value(requested_volume, lot_size),
                requested_price=_format_market_value(requested_price, tick_size),
                submitted_volume=_format_market_value(submitted_volume, lot_size),
                submitted_price=_format_market_value(submitted_price, tick_size),
                verified=False, status="failed",
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="new_order", exchange=name, account=account,
            code="ORDER_SUBMISSION_FAILED",
            message=sanitize_error_message(str(exc)),
            order=CanonicalOrderResult(
                symbol=requested_symbol, side=requested_side, order_type=order_type,
                requested_volume=_format_market_value(requested_volume, lot_size),
                requested_price=_format_market_value(requested_price, tick_size),
                submitted_volume=_format_market_value(submitted_volume, lot_size),
                submitted_price=_format_market_value(submitted_price, tick_size),
                verified=False, status="failed",
            ),
        )

    # --- 5. verify by re-reading the order list ---
    exchange_order_id_raw = response_data.get("i")
    try:
        exchange_order_id: Optional[int] = int(str(exchange_order_id_raw)) if exchange_order_id_raw is not None else None
    except (TypeError, ValueError):
        exchange_order_id = None

    verified, verified_id = _verify_order_submission(creds, client_order_id)
    final_exchange_id = exchange_order_id if exchange_order_id is not None else verified_id

    result = CanonicalOrderResult(
        symbol=requested_symbol,
        side=requested_side,
        order_type=order_type,
        requested_volume=_format_market_value(requested_volume, lot_size),
        requested_price=_format_market_value(requested_price, tick_size),
        submitted_volume=_format_market_value(submitted_volume, lot_size),
        submitted_price=_format_market_value(submitted_price, tick_size),
        verified=verified,
        status="success" if verified else "partial",
        exchange_order_id=final_exchange_id,
    )
    if verified:
        return make_success(operation="new_order", exchange=name, account=account, order=result)
    return make_failure(
        operation="new_order", exchange=name, account=account,
        code="VERIFICATION_FAILED",
        message="Order submission was acknowledged but could not be confirmed via /orders.",
        order=result,
    )


def _coerce_bool_safe(value: Any) -> bool:
    """Lenient truthy coercion. Avoids the heavier helper in rise."""
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"", "0", "false", "no", "off", "none", "null"}:
        return False
    return True


# ---------------------------------------------------------------------------
# Operation dispatch
# ---------------------------------------------------------------------------


def _execute_balance(account: str) -> CanonicalResponse:
    creds = _lookup_credentials(account)
    if creds is None:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or incomplete Pacifica account credentials.",
        )
    try:
        data = _get_account_info(creds["address"])
        summary = _build_portfolio_summary(data)
        return make_success(
            operation="balance",
            exchange=name,
            account=creds["account"],
            balance=normalize_balance(summary.account_value, summary.unit),
            portfolio_summary=summary,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pacifica balance fetch failed for %s: %s", creds["account"], exc)
        return make_failure(
            operation="balance",
            exchange=name,
            account=creds["account"],
            code="BALANCE_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )


def _execute_positions_orders(account: str) -> CanonicalResponse:
    creds = _lookup_credentials(account)
    if creds is None:
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or incomplete Pacifica account credentials.",
        )
    try:
        rows = _get_positions(creds["address"])
        # Mark prices are required to compute uPnL — fetch them alongside
        # positions. We treat a mark-price failure as soft: positions
        # still come back, just with PnL=0.
        try:
            mark_by_symbol = _get_mark_prices()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pacifica mark prices unavailable: %s", exc)
            mark_by_symbol = {}
        positions = _normalize_positions(rows, mark_by_symbol)

        # Open orders are a separate read; the wizard's positions screen
        # renders both. We do the fetch + aggregation inline so the
        # wizard gets one response to render. A failure here is also
        # soft — the positions block still ships, just with an empty
        # orders block.
        open_order_count = 0
        order_groups: List[CanonicalOrderGroup] = []
        try:
            open_orders = _get_open_orders(creds["address"])
            open_order_count = len(open_orders)
            order_groups = _aggregate_open_orders(open_orders)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pacifica open-orders fetch failed: %s", exc)

        return make_success(
            operation="positions_orders",
            exchange=name,
            account=creds["account"],
            positions=positions,
            open_order_count=open_order_count,
            order_groups=order_groups,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pacifica positions fetch failed for %s: %s", creds["account"], exc)
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=creds["account"],
            code="POSITIONS_ORDERS_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )


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

    if operation == "balance":
        return _execute_balance(account)
    if operation == "positions_orders":
        return _execute_positions_orders(account)
    if operation == "new_order":
        return _execute_new_order(account, request)
    if operation == "cancel_order_group":
        return _execute_cancel_order_group(account, request)
    if operation == "ladder":
        return _execute_ladder(account, request)
    if operation == "positions_management":
        return _execute_positions_management(account, request)
    if operation == "set_tp":
        return _execute_set_tp(account, request)
    if operation == "set_sl":
        return _execute_set_sl(account, request)
    if operation == "close_position":
        return _execute_close_position(account, request)

    return make_failure(
        operation=operation,
        exchange=name,
        account=account,
        code="NOT_IMPLEMENTED",
        message="Not implemented yet.",
    )


__all__ = [
    "name",
    "list_accounts",
    "capabilities",
    "execute",
    "DEFAULT_API_BASE",
]
