"""Hibachi exchange agent.

This module owns EVERYTHING Hibachi-specific for the /trade stack.

Current scope (Phase 1 — balance only):

- Credential discovery from ``HIBACHI_<ALIAS>_ACCOUNTID``,
  ``HIBACHI_<ALIAS>_APIKEY``, and ``HIBACHI_<ALIAS>_PRIVATEKEY`` in
  the live environment or ``$HERMES_HOME/.env``. Account aliases are
  case-insensitive at the env-var level; the canonical alias surfaced
  to the wizard is the lowercased form, matching every other KAM
  exchange agent.
- Authenticated account-info retrieval through Hibachi's documented
  REST endpoint ``GET https://api.hibachi.xyz/trade/account/info``
  using the ``Authorization: <APIKEY>`` header. The richer
  ``/trade/account/info`` endpoint is used (rather than
  ``/capital/balance``) because it returns balance plus positions,
  assets, leverage, and margin fields that future phases will need —
  the canonical normalization in this module exposes only the
  ``balance`` value the wizard needs today, but the raw response is
  preserved for downstream code.
- Authenticated open-orders retrieval through ``GET
  https://api.hibachi.xyz/trade/orders?accountId=<accountId>`` and
  the ``positions`` field nested inside ``/trade/account/info``. Both
  are normalized into the canonical ``CanonicalPosition`` /
  ``CanonicalOrderGroup`` shapes the wizard's "📋 Open Orders & 💼
  Positions" view already consumes from every other KAM exchange.
- Canonical conversion into the exchange-agnostic TradeDesk / wizard
  contract. Hibachi settles in USDT, so the canonical balance unit is
  ``USDT`` (Hibachi's settlement currency, not converted to USDC).
- Canonical instrument resolution scaffold. The agent fetches
  ``/market/exchange-info`` from ``data-api.hibachi.xyz`` (public,
  no auth) and builds a ``canonical_symbol -> Hibachi market
  descriptor`` map. The rest of KAM continues to address instruments
  by canonical symbol (``BTC``, ``ETH``, ``SOL``); the agent
  translates to Hibachi's native ``"BTC/USDT-P"`` + integer
  ``contractId`` on demand. This is wired for future trading — the
  resolver is implemented now and used to validate market existence
  during ``balance`` enrichment, but no write paths use it yet.

The agent deliberately supports **only exchange-managed (HMAC)
accounts** in Phase 1. Trustless (ECDSA) accounts have a different
key lifecycle (the on-chain wallet key, not an HMAC secret) and
will be added later behind the same ``_lookup_credentials`` /
``_signed_request`` seam — the agent is structured so that addition
does not require a redesign.

Write operations (new_order / ladder / set_tp / set_sl /
close_position / cancel_orders) are NOT IMPLEMENTED in Phase 2. The
canonical ``NOT_IMPLEMENTED`` error is returned for everything except
``balance``, ``positions_orders``, and ``resolve_instrument``.

TradeDesk and the Telegram wizard MUST remain exchange-agnostic and
MUST NOT parse ``HIBACHI_*`` environment variables or Hibachi-native
payloads. All Hibachi-specific behavior — env-var scanning, REST
calls, header construction, response parsing, symbol translation —
lives in this module.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from eth_keys.datatypes import PrivateKey as EthPrivateKey


from ..canonical import (
    CanonicalBalance,
    CanonicalCancelGroupResult,
    CanonicalInstrument,
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


# ---------------------------------------------------------------------------
# Module identity — required by TradeDesk.
# ---------------------------------------------------------------------------

name = "hibachi"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Hibachi's documented REST base URLs. The account-info endpoint lives on the
# ``api`` host; the public market metadata endpoint lives on ``data-api``.
# Both are documented at https://api-doc.hibachi.xyz/ and verified against
# the live Postman collection.
DEFAULT_ACCOUNT_API_BASE = "https://api.hibachi.xyz"
DEFAULT_MARKET_API_BASE = "https://data-api.hibachi.xyz"
API_TIMEOUT_SECONDS = 20
MAX_RETRIES = 2

# Hibachi returns balances as decimal strings with up to 6 decimal places for
# USDT (the documented ``settlementDecimals`` for the contracts we surveyed).
# The agent never truncates; it normalizes to 2dp via ``normalize_balance`` for
# the canonical balance surface.
USDT_SETTLEMENT_DECIMALS = 6

# Account-alias pattern: must start with a letter, then ASCII letters /
# digits / underscores. Same convention as every other KAM agent — the
# pattern rejects names that would alias in surprising ways.
_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")

# AccountId is documented as a positive integer in the live Postman
# collection ("accountId": 128 in the example response). We accept that
# and also tolerate the stringified form some operators paste.
_ACCOUNT_ID_PATTERN = re.compile(r"^[0-9]+$")

# Endpoint path constants. Centralised so that future phases (and tests) can
# reference a single source of truth and we never accidentally hit a
# near-miss path.
_PATH_TRADE_ACCOUNT_INFO = "/trade/account/info"
_PATH_TRADE_ORDER = "/trade/order"
_PATH_TRADE_ORDERS = "/trade/orders"
_PATH_TRADE_ORDERS_HISTORY = "/trade/orders/history"
_PATH_MARKET_EXCHANGE_INFO = "/market/exchange-info"

# Hibachi's price-signing formula uses a fixed 2^32 multiplier.
_PRICE_MULTIPLIER = 1 << 32
# Hibachi's live API verifies fee-rate fields as ``rate * 10^8``.
# The docs' worked example around ``0.0005`` is inconsistent, but the
# server-side digest emitted during real order attempts proves the live
# verifier expects 10^8 scaling (e.g. 0.00045 -> 45000 -> 0xAFC8).
_MAX_FEES_RATE_SCALE = Decimal("100000000")
_ORDER_VERIFICATION_ATTEMPTS = 4
_ORDER_VERIFICATION_SLEEP_SECONDS = 0.5
_QUANTITY_SCALE = Decimal("1")
_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,32}$")
_SIDE_TO_HIBACHI_ORDER = {"buy": "BID", "sell": "ASK"}
_SIDE_TO_SIGNATURE_INT = {"sell": 0, "buy": 1}

# Hibachi's open-orders endpoint returns a flat list. We cap the page
# size conservatively to stay within the documented default and to
# keep the wizard's render loop bounded; the API itself does not
# document an explicit cap.
OPEN_ORDERS_MAX = 500

# Required credential suffixes. The agent considers an account fully
# configured only when all three are present and non-empty.
HIBACHI_REQUIRED_SUFFIXES: Tuple[str, ...] = (
    "ACCOUNTID",
    "APIKEY",
    "PRIVATEKEY",
)

# Mapping from env-var suffix to internal credential dict key. The internal
# keys are intentionally short so they read cleanly at the call site and
# mirror the way the other agents (e.g. Arcus) hand their credentials to the
# HTTP layer.
_CREDENTIAL_FIELD_BY_SUFFIX: Dict[str, str] = {
    "ACCOUNTID": "account_id",
    "APIKEY": "api_key",
    "PRIVATEKEY": "private_key",
}

# Sensitive values that must never appear in error messages or logs. The
# redaction helper below uses this set to scrub free-form strings that may
# contain user-controlled data — see ``_redact``.
_SENSITIVE_KEYS: Tuple[str, ...] = (
    "api_key",
    "private_key",
    "apikey",
    "privatekey",
    "accountid",
)


# ---------------------------------------------------------------------------
# Env / dotenv helpers — minimal, mirroring the rest of the trade package.
# ---------------------------------------------------------------------------


def _hermes_home() -> Path:
    """Return the Hermes home directory (``~/.hermes`` by default)."""
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _load_dotenv_values(path: Path) -> Dict[str, str]:
    """Minimal ``.env`` parser.

    Honors the same convention as the rest of KAM: ``KEY=VALUE`` pairs,
    optional quoting, ``#``-prefixed comments. Missing / unreadable
    files yield an empty dict.
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
            value = value[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        values[key] = value
    return values


def _combined_hibachi_env() -> Dict[str, Tuple[str, str, str]]:
    """Return ``{upper_key: (actual_key, value, source)}`` for all
    ``HIBACHI_*`` variables, merging ``os.environ`` and ``$HERMES_HOME/.env``.

    Discovery is case-insensitive — the agent matches on the upper-cased
    key but preserves the original key for diagnostic purposes. ``dotenv``
    entries are never allowed to override live env (we ``setdefault``)
    so the live environment always wins, matching the rest of KAM.
    """
    out: Dict[str, Tuple[str, str, str]] = {}
    for k, v in os.environ.items():
        if k.startswith("HIBACHI_"):
            out.setdefault(k.upper(), (k, v, "env"))
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        env_path = os.path.join(hermes_home, ".env")
        if os.path.isfile(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, _, value = line.partition("=")
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if key.upper().startswith("HIBACHI_"):
                            out.setdefault(key.upper(), (key, value, "dotenv"))
            except OSError:
                pass
    return out


# ---------------------------------------------------------------------------
# Account discovery
# ---------------------------------------------------------------------------


def _normalize_alias(raw_account: str) -> str:
    """Sanitize a Hibachi account alias.

    Strips stray underscores, upper-cases for validation, and returns the
    lower-cased form for use as the public alias (matching every other
    KAM exchange agent that surfaces lowercase aliases to the wizard).
    """
    alias = raw_account.strip().strip("_")
    if not alias:
        return ""
    return alias.lower() if _ALIAS_PATTERN.match(alias.upper()) else alias.lower()


def _discover_accounts() -> List[str]:
    """Return the list of configured Hibachi account aliases.

    An account is "complete" (and therefore surfaced to the wizard) iff
    all three required credential suffixes are present and non-empty.
    Aliases are returned in sorted, lower-cased form.

    Mirrors the same env-merging pattern the other KAM agents use so
    that a Hibachi account configured in ``$HERMES_HOME/.env`` is
    visible to the wizard even if it is not in the live process
    environment.
    """
    env = _combined_hibachi_env()
    aliases: List[str] = []
    seen: set = set()
    for upper_key in env:
        if not upper_key.startswith("HIBACHI_") or not upper_key.endswith("_ACCOUNTID"):
            continue
        raw_account = upper_key[len("HIBACHI_"):-len("_ACCOUNTID")]
        alias = _normalize_alias(raw_account)
        if not alias or alias in seen:
            continue
        if _has_complete_credentials(raw_account, env):
            seen.add(alias)
            aliases.append(alias)
    return sorted(aliases)


def _has_complete_credentials(
    raw_account: str,
    env: Mapping[str, Tuple[str, str, str]],
) -> bool:
    """True iff every required suffix for ``raw_account`` is present and
    non-empty in the env map."""
    for suffix in HIBACHI_REQUIRED_SUFFIXES:
        key = f"HIBACHI_{raw_account}_{suffix}".upper()
        value = env.get(key)
        if not value or not value[1].strip():
            return False
    return True


def _lookup_credentials(account: str) -> Optional[Dict[str, Any]]:
    """Look up the three Hibachi credentials for ``account``.

    Returns a dict shaped for the HTTP layer (``account_id``, ``api_key``,
    ``private_key``, plus the public ``account`` alias) or ``None`` if
    the account is unknown or incomplete. The caller MUST treat the
    returned ``api_key`` and ``private_key`` as sensitive — they must
    never be logged or echoed in error messages.
    """
    raw = str(account or "").strip()
    if not raw:
        return None
    upper = raw.upper()
    if not _ALIAS_PATTERN.match(upper):
        return None
    env = _combined_hibachi_env()
    creds: Dict[str, Any] = {"account": raw.lower()}
    for suffix, field in _CREDENTIAL_FIELD_BY_SUFFIX.items():
        key = f"HIBACHI_{upper}_{suffix}".upper()
        value = env.get(key)
        if not value or not value[1].strip():
            return None
        creds[field] = value[1].strip()
    # Validate the accountId shape defensively. Hibachi expects a positive
    # integer; if the operator pasted something nonsensical we surface
    # ACCOUNT_NOT_FOUND with a hint rather than letting the API return
    # a less-actionable error.
    if not _ACCOUNT_ID_PATTERN.match(str(creds.get("account_id") or "")):
        return None
    return creds


# ---------------------------------------------------------------------------
# Public agent contract (TradeDesk)
# ---------------------------------------------------------------------------


def list_accounts() -> List[str]:
    """Return the configured Hibachi account aliases (lowercased, sorted)."""
    return _discover_accounts()


def capabilities() -> List[str]:
    """Return the operations this agent supports.

    Hibachi now advertises read support (``balance``, ``positions_orders``,
    ``positions_management``, ``resolve_instrument``) plus single-order
    placement, exact-scope group cancellation by canonical ``(symbol,
    side)``, batched ladder submission, and TP/SL position management.
    """
    return [
        "balance",
        "positions_orders",
        "positions_management",
        "new_order",
        "cancel_order_group",
        "ladder",
        "set_tp",
        "set_sl",
        "resolve_instrument",
        # Phase 2.4: read-only catalog enumeration.
        "list_instruments",
    ]


def execute(request: Dict[str, Any]) -> CanonicalResponse:
    """Dispatch a canonical request to the Hibachi agent.

    Phase 3 supports ``balance``, ``positions_orders``, ``new_order``
    and ``resolve_instrument``. Any other operation returns a canonical
    ``NOT_IMPLEMENTED`` error so the wizard's UI surfaces a clear
    "not yet" message rather than crashing.
    """
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
        if operation == "positions_orders":
            return _positions_orders(account)
        if operation == "positions_management":
            return _positions_management(account)
        if operation == "new_order":
            return _new_order(account, request)
        if operation == "cancel_order_group":
            return _cancel_order_group(account, request)
        if operation == "ladder":
            return _ladder(account, request)
        if operation == "set_tp":
            return _set_position_trigger(account, request, operation="set_tp")
        if operation == "set_sl":
            return _set_position_trigger(account, request, operation="set_sl")
        if operation == "resolve_instrument":
            return _resolve_instrument(account, request)
        if operation == "list_instruments":
            return _execute_list_instruments(account, request)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="HIBACHI_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )
    return make_failure(
        operation=operation,
        exchange=name,
        account=account,
        code="NOT_IMPLEMENTED",
        message=f"Hibachi does not implement '{operation}' yet.",
    )


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


def _redact(text: Any) -> str:
    """Scrub sensitive substrings from a free-form error message.

    Hibachi errors occasionally echo back request parameters; the
    canonical error contract requires that secrets never leak, so we
    defensively scrub for ``api_key=…`` / ``private_key=…`` patterns
    and replace them with ``***``. We also do a literal-substring
    scrub against any credential values we may have in scope via the
    thread-local ``_current_credentials`` slot — that catches the case
    where a server returned a verbose stack trace containing the
    actual secret.

    The ``Authorization:`` case is handled specially: an HTTP
    ``Authorization: <scheme> <token>`` header carries the scheme
    label (``Bearer``, ``Basic``, …) as part of the credential
    payload, so the redaction must consume the scheme token as well.
    A naive ``Authorization:[^\\s]+`` regex would only catch
    ``Bearer`` and leave the actual token leaking after a space; the
    helper below consumes both the scheme and the token in a single
    pass. The original case of the marker is preserved so the
    operator still sees the field label the server reported.
    """
    rendered = str(text or "")
    # 1. Authorization: <scheme> <token> -> Authorization: <scheme> ***
    def _auth_scheme_sub(match: "re.Match[str]") -> str:
        return f"{match.group(1)}{match.group(2)} ***"
    rendered = re.sub(
        r"(?i)(authorization\s*:\s*)([A-Za-z][A-Za-z0-9_-]*)\s+[^\s,;}\"']+",
        _auth_scheme_sub,
        rendered,
    )
    # 2. ``Authorization: <single-token-value>`` with no scheme prefix
    #    — also rare. Skip this branch if the previous pass already
    #    produced ``***`` right after the colon, to avoid double-
    #    scrubbing ``Authorization: Bearer ***`` into
    #    ``Authorization: *** ***``.
    def _auth_naked_sub(match: "re.Match[str]") -> str:
        return f"{match.group(1)}***"
    rendered = re.sub(
        r"(?i)(authorization\s*:\s*)(?!Bearer\s|Basic\s|Digest\s|Token\s|HOBA\s|Mutual\s|Negotiate\s|OAuth\s|SCRAM-SHA-1\s|SCRAM-SHA-256\s|VAPID\s)[^\s,;}\"']+",
        _auth_naked_sub,
        rendered,
    )
    # 3. Compact ``key=value`` markers. Case-insensitive so
    #    ``Api_Key=`` and ``api_key=`` are both caught, but the
    #    original casing of the marker is preserved in the output.
    for marker in ("api_key=", "private_key=", "apikey=", "privatekey="):
        rendered = re.sub(
            re.escape(marker) + r"[^\s,;}\"']+",
            marker + "***",
            rendered,
            flags=re.IGNORECASE,
        )
    # 4. Final defensive literal-substring scrub against any credential
    #    values we may have in scope (catches server stack traces that
    #    echo the secret verbatim).
    sensitive_values: List[str] = []
    creds = _current_credentials()
    if creds:
        for key in _SENSITIVE_KEYS:
            value = creds.get(key)
            if isinstance(value, str) and value:
                sensitive_values.append(value)
    for secret in sensitive_values:
        if secret in rendered:
            rendered = rendered.replace(secret, "***")
    return rendered


# Thread-local storage for the credentials of the in-flight request, used by
# the redaction helper. This is intentionally module-local (a thread-local)
# so that one request can never see another request's secrets, and so that
# a multi-threaded caller (Telegram adapter) doesn't have to plumb the
# credentials dict into every error path.
_credential_slot = threading.local()


def _current_credentials() -> Optional[Dict[str, Any]]:
    return getattr(_credential_slot, "value", None)


def _set_current_credentials(creds: Optional[Dict[str, Any]]) -> None:
    _credential_slot.value = creds


def _request_json(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    body: Optional[Mapping[str, Any]] = None,
    timeout: int = API_TIMEOUT_SECONDS,
) -> Any:
    """HTTP helper used by every Hibachi call.

    Uses stdlib only (mirrors Hyperliquid and Rise) so the agent has no
    extra third-party dependencies. Returns the parsed JSON value on
    success, or raises ``RuntimeError`` with a redacted message on
    failure. ``Response.raise_for_status``-style errors are translated
    into ``"HTTP <code> on <path>: <body>"`` so the wizard can show a
    useful diagnostic.

    The return type is intentionally ``Any`` because Hibachi uses
    multiple top-level JSON shapes: ``/trade/account/info`` returns a
    dict, ``/trade/orders`` returns a list of order objects, and
    ``/market/exchange-info`` returns a dict. Each call site is
    responsible for coercing the value into the shape it expects
    (see ``_fetch_account_info``, ``_fetch_open_orders``, and the
    market cache loader).
    """
    data: Optional[bytes] = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    # Hibachi sits behind Cloudflare and rejects the default Python
    # ``urllib`` User-Agent with HTTP 1010 (browser_signature_banned).
    # Set a modern, browser-shaped User-Agent on every outbound request
    # so the edge lets the call through. The header is benign — read
    # endpoints do not pin a specific UA.
    outbound_headers = dict(headers or {})
    outbound_headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )
    request = urllib.request.Request(url, method=method, data=data, headers=outbound_headers)
    last_error: Optional[BaseException] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                text = resp.read().decode(charset, errors="replace")
                if not text:
                    return None
                try:
                    return json.loads(text)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"Hibachi returned invalid JSON from {url}"
                    ) from exc
        except urllib.error.HTTPError as exc:
            try:
                body_text = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                body_text = ""
            raise RuntimeError(
                f"HTTP {int(exc.code)} on {urllib.parse.urlparse(url).path}: "
                f"{_redact(body_text or str(exc.reason))}"
            ) from exc
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt >= MAX_RETRIES:
                break
            time.sleep(0.2 * (attempt + 1))
            continue
    raise RuntimeError(
        f"Hibachi API unreachable at {urllib.parse.urlparse(url).netloc}: "
        f"{_redact(str(getattr(last_error, 'reason', last_error)))}"
    )


def _account_api_base() -> str:
    """Return the account-API base URL. Operator-overridable via
    ``HIBACHI_API_BASE`` (mostly useful for tests / staging)."""
    return (
        os.environ.get("HIBACHI_API_BASE", "").strip()
        or DEFAULT_ACCOUNT_API_BASE
    ).rstrip("/")


def _market_api_base() -> str:
    """Return the market-API base URL. Operator-overridable via
    ``HIBACHI_MARKET_API_BASE``."""
    return (
        os.environ.get("HIBACHI_MARKET_API_BASE", "").strip()
        or DEFAULT_MARKET_API_BASE
    ).rstrip("/")


def _fetch_account_info(credentials: Dict[str, Any]) -> Dict[str, Any]:
    """Authenticated ``GET /trade/account/info`` call.

    Hibachi's documented read auth is a single ``Authorization`` header
    carrying the API key (no HMAC signing is required for read
    endpoints — the docs are explicit: "Signing is ONLY required for
    the write-operations listed below. GET requests do not require a
    signature."). We still take the full credentials dict so that the
    future write path can add HMAC headers without a caller-shape
    change.
    """
    base = _account_api_base()
    account_id = str(credentials.get("account_id") or "").strip()
    if not account_id:
        raise RuntimeError("Hibachi accountId is missing from the credentials")
    url = f"{base}{_PATH_TRADE_ACCOUNT_INFO}?{urllib.parse.urlencode({'accountId': account_id})}"
    headers = {
        "Authorization": str(credentials.get("api_key") or "").strip(),
        "Accept": "application/json",
    }
    return _request_json("GET", url, headers=headers)


# ---------------------------------------------------------------------------
# Instrument resolution (canonical <-> Hibachi)
# ---------------------------------------------------------------------------


class _MarketCache:
    """Lazy, in-process cache of the ``/market/exchange-info`` payload.

    The endpoint is public (no auth required) and the response changes
    only when Hibachi lists a new contract, so a 5-minute TTL is more
    than enough for the wizard's usage pattern while keeping the
    surface area small. Future write paths will use the same cache
    to translate canonical symbols to ``contractId`` / ``symbol``
    before signing.
    """

    TTL_SECONDS = 300.0
    _lock = threading.Lock()
    _expires_at: float = 0.0
    _payload: Optional[Dict[str, Any]] = None

    @classmethod
    def get(cls) -> Dict[str, Any]:
        now = time.monotonic()
        with cls._lock:
            if cls._payload is not None and now < cls._expires_at:
                return cls._payload
            payload = _fetch_exchange_info()
            # ``/market/exchange-info`` is documented to return a dict.
            # If we ever see a non-dict (proxy / maintenance page
            # returning a list or scalar), keep the cache empty rather
            # than crashing the resolver — Phase 1 callers treat an
            # empty market index as "no live contracts" and surface
            # an empty list to the user, which is the correct
            # degradation for a degraded read path.
            if not isinstance(payload, dict):
                cls._payload = {}
            else:
                cls._payload = payload
            cls._expires_at = now + cls.TTL_SECONDS
            return cls._payload

    @classmethod
    def invalidate(cls) -> None:
        with cls._lock:
            cls._payload = None
            cls._expires_at = 0.0


def _fetch_exchange_info() -> Dict[str, Any]:
    """Public ``GET /market/exchange-info`` call.

    No auth required. Returns the parsed response — the agent does not
    introspect the contents at this layer, leaving structure parsing
    to the resolver helpers below.
    """
    base = _market_api_base()
    url = f"{base}{_PATH_MARKET_EXCHANGE_INFO}"
    headers = {"Accept": "application/json"}
    return _request_json("GET", url, headers=headers)


def _extract_future_contracts(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return the ``futureContracts`` list from an exchange-info payload.

    Defensive against shape drift: returns an empty list if the field is
    missing or the wrong type rather than raising.
    """
    raw = payload.get("futureContracts")
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


# Aliases that the wizard might type that we want to map to the
# canonical symbol. The keys are upper-cased stripped forms. Values are
# canonical symbols. Future phases can extend this without touching
# any other code.
_CANONICAL_ALIASES: Dict[str, str] = {
    "BTC": "BTC",
    "WBTC": "BTC",
    "XBT": "BTC",
    "ETH": "ETH",
    "WETH": "ETH",
    "SOL": "SOL",
    "HYPE": "HYPE",
}


def _canonical_symbol_from_request(value: Any) -> str:
    """Normalize a user-supplied symbol to its canonical form.

    The wizard / canonical contract uses the bare underlying symbol
    (``BTC`` / ``ETH`` / ``SOL`` / ``HYPE``). We strip common quote /
    perp suffixes so that ``BTC/USDT``, ``BTC-USDT``, ``BTC-USD``,
    ``BTC-PERP``, ``BTCUSDT``, and ``BTC/USDT-P`` all resolve to
    ``BTC`` — the same surface the other KAM agents expose.

    Stripping happens against a normalized upper-case form where the
    separators (``/`` / ``-`` / ``_``) are converted to spaces and then
    re-collapsed, so a trailing ``-P`` is recognizable as a distinct
    token rather than being glued to the base symbol by an early
    alphanumeric-only scrub.
    """
    raw_text = re.sub(r"[^A-Za-z0-9]+", " ", str(value or "")).strip().upper()
    if not raw_text:
        return ""
    parts = raw_text.split()
    # Strip a trailing perp-style marker first ("P", "PERP"), so the
    # base symbol is left intact even if a quote token is also present.
    while parts and parts[-1] in ("P", "PERP"):
        parts.pop()
    # Strip a trailing quote token (USDT, USDC, USD, USDG) when present.
    if parts and parts[-1] in ("USDT", "USDC", "USD", "USDG"):
        parts.pop()
    base = parts[0] if parts else ""
    if not base:
        return ""
    # When the user passed a single concatenated token with no
    # separators (e.g. ``BTCUSDT`` or ``HYPEUSDT``) the trailing-quote
    # strip above does not fire because the whole string is one token.
    # Substring-trim those here as a final fallback so a pasted
    # ``BTCUSDT`` still resolves to ``BTC``.
    for quote in ("USDT", "USDC", "USDG", "USD"):
        if base.endswith(quote) and len(base) > len(quote):
            base = base[: -len(quote)]
            break
    return _CANONICAL_ALIASES.get(base, base)


def _hibachi_live_descriptors(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return one descriptor per live Hibachi future contract."""
    descriptors: List[Dict[str, Any]] = []
    for contract in _extract_future_contracts(payload):
        if not contract.get("live", True):
            continue
        underlying = str(contract.get("underlyingSymbol") or "").strip().upper()
        if not underlying:
            continue
        descriptors.append({
            "id": contract.get("id"),
            "symbol": str(contract.get("symbol") or "").strip(),
            "display_name": str(contract.get("displayName") or "").strip(),
            "underlying_symbol": underlying,
            "settlement_symbol": str(contract.get("settlementSymbol") or "USDT").strip().upper() or "USDT",
            "tick_size": str(contract.get("tickSize") or "").strip() or None,
            "step_size": str(contract.get("stepSize") or "").strip() or None,
            "min_order_size": str(contract.get("minOrderSize") or "").strip() or None,
            "min_notional": str(contract.get("minNotional") or "").strip() or None,
            "underlying_decimals": contract.get("underlyingDecimals"),
            "settlement_decimals": contract.get("settlementDecimals"),
        })
    return descriptors


def _normalize_hibachi_market(contract: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a single Hibachi future-contract entry to the
    Fibo common schema.

    Only ``instrument`` is required; the rest may be ``None`` if
    the upstream payload doesn't supply them. Hibachi carries
    ``symbol`` (the venue-native id, e.g. ``ETH/USDT-P``),
    ``displayName``, ``underlyingSymbol`` (base), and
    ``settlementSymbol`` (quote).

    Phase 2.4.1: the normalized ``instrument`` field is the
    **stripped** canonical (``BTC``) — the same form
    ``_resolve_instrument`` returns via
    ``_canonical_symbol_from_request``. Catalog + resolver
    MUST agree on the same canonical id, otherwise picking a
    catalog entry revalidates against a different id than
    what the catalog displayed.
    """
    if not isinstance(contract, dict):
        return {"instrument": ""}
    raw_symbol = str(contract.get("symbol") or "").strip()
    if not raw_symbol:
        return {"instrument": ""}
    # Strip the venue-side quote + perp markers so the catalog
    # ``instrument`` matches what ``resolve_instrument`` returns
    # (e.g. ``BTC/USDT-P`` → ``BTC``).
    instrument = _canonical_symbol_from_request(raw_symbol) or raw_symbol
    out: Dict[str, Any] = {"instrument": instrument}
    base = str(contract.get("underlyingSymbol") or "").strip()
    if base:
        out["base"] = base
    quote = str(contract.get("settlementSymbol") or "").strip()
    if quote:
        out["quote"] = quote
    desc = str(contract.get("displayName") or "").strip()
    if desc:
        out["description"] = desc
    # Hibachi exposes perps in this catalog.
    if (
        contract.get("contractType")
        or "PERP" in raw_symbol.upper()
        or raw_symbol.endswith("-P")
    ):
        out["market_type"] = "perp"
    # No bundled price in exchange-info — Fibo will call
    # ``market_price`` separately when supported (Hibachi does
    # not currently expose that operation).
    return out


def _execute_list_instruments(
    account: str, request: Dict[str, Any]
) -> CanonicalResponse:
    """Phase 2.4: read-only catalog enumeration for Hibachi.

    Returns the normalized Fibo schema (see
    ``plugins/trade/fibo/discovery.py``) inside
    ``data["instruments"]``. Each record carries at minimum the
    venue-native ``instrument`` id. No price is attached —
    Hibachi does not currently expose a market_price operation.
    """
    if not account:
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account="",
            code="MISSING_ACCOUNT",
            message="Account is required.",
        )
    try:
        payload = _fetch_exchange_info()
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account=account,
            code="CATALOG_UNAVAILABLE",
            message=_redact(sanitize_error_message(str(exc))),
        )
    if not isinstance(payload, dict):
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account=account,
            code="CATALOG_UNAVAILABLE",
            message="Hibachi exchange-info returned an unexpected payload shape.",
        )
    instruments: List[Dict[str, Any]] = []
    for contract in _extract_future_contracts(payload):
        if not isinstance(contract, dict):
            continue
        # Skip non-live contracts (the resolver layer does the
        # same filter when building market metadata).
        if not contract.get("live", True):
            continue
        norm = _normalize_hibachi_market(contract)
        if norm.get("instrument"):
            instruments.append(norm)
    return make_success(
        operation="list_instruments",
        exchange=name,
        account=account,
        data={"instruments": instruments},
    )


def _build_hibachi_market_index(payload: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Index future contracts by their canonical underlying symbol.

    Used for portfolio/unit display only. Order placement and other
    symbol-sensitive operations must go through
    ``_resolve_canonical_instrument``, which refuses to pick a market
    when more than one live contract shares an underlying.
    """
    index: Dict[str, Dict[str, Any]] = {}
    for candidate in _hibachi_live_descriptors(payload):
        underlying = str(candidate.get("underlying_symbol") or "")
        existing = index.get(underlying)
        if existing is None:
            index[underlying] = candidate
            continue
        try:
            new_id = int(candidate["id"]) if candidate["id"] is not None else None
            old_id = int(existing["id"]) if existing["id"] is not None else None
        except (TypeError, ValueError):
            new_id = None
            old_id = None
        if new_id is not None and (old_id is None or new_id < old_id):
            index[underlying] = candidate
    return index


def _normalized_hibachi_contract_key(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).upper()


def _resolve_canonical_instrument(
    requested: str,
    market_index: Optional[Mapping[str, Dict[str, Any]]] = None,
    payload: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Return the Hibachi market descriptor for a requested symbol.

    Priority:
    1. Exact venue contract symbol (``BTC/USDT-P``), unique
    2. Separator-insensitive contract key, unique
    3. Unique live underlying (``BTC``, ``WBTC``, ``BTCUSDT``)
    Multiple matches at the chosen rank raise ``ValueError("INSTRUMENT_AMBIGUOUS")``.
    ``market_index`` is accepted for call-site compatibility but is not
    used to collapse duplicates.
    """
    requested_raw = str(requested or "").strip()
    if not requested_raw:
        return None
    if payload is None:
        payload = _MarketCache.get()
    contracts = _hibachi_live_descriptors(payload)
    requested_upper = requested_raw.upper()
    requested_key = _normalized_hibachi_contract_key(requested_raw)
    exact = [row for row in contracts if str(row.get("symbol") or "").upper() == requested_upper]
    if not exact and requested_key:
        exact = [
            row for row in contracts
            if _normalized_hibachi_contract_key(row.get("symbol")) == requested_key
        ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ValueError("INSTRUMENT_AMBIGUOUS")
    canonical = _canonical_symbol_from_request(requested_raw)
    if not canonical:
        return None
    underlying_matches = [
        row for row in contracts if str(row.get("underlying_symbol") or "") == canonical
    ]
    if len(underlying_matches) == 1:
        return underlying_matches[0]
    if len(underlying_matches) > 1:
        raise ValueError("INSTRUMENT_AMBIGUOUS")
    return None


# ---------------------------------------------------------------------------
# Read: balance
# ---------------------------------------------------------------------------


def _balance(account: str) -> CanonicalResponse:
    """Fetch the account-level balance and normalize to the canonical
    portfolio summary used by the wizard.

    Hibachi's ``/trade/account/info`` returns ``balance`` (net equity in
    USDT, including unrealized PnL), plus ``maximalWithdraw`` (free
    collateral), ``initialMargin``, ``maintenanceMargin``,
    ``totalPositionNotional``, ``totalOrderNotional``, and a list of
    ``assets`` / ``positions``. Phase 1 surfaces the summary fields via
    ``CanonicalPortfolioSummary``; positions are normalized for
    future use but not rendered by the wizard yet (positions_orders
    is a Phase 2 operation on Hibachi).

    The market index is loaded eagerly here so that the resolve side
    of the agent stays in sync with whatever Hibachi is currently
    listing — if the network call fails the error surfaces as
    ``BALANCE_UNAVAILABLE`` rather than silently dropping the data.
    """
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message=(
                f"Hibachi account '{account}' is not configured. "
                "Set HIBACHI_<alias>_ACCOUNTID, HIBACHI_<alias>_APIKEY, "
                "and HIBACHI_<alias>_PRIVATEKEY."
            ),
        )
    token = _set_current_credentials(credentials)
    try:
        try:
            raw = _fetch_account_info(credentials)
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation="balance",
                exchange=name,
                account=credentials["account"],
                code="BALANCE_UNAVAILABLE",
                message=_redact(sanitize_error_message(str(exc))),
            )

        # Defensive: Hibachi's documented contract is a JSON object, but
        # upstream proxies / maintenance responses occasionally return a
        # list or a scalar. Treat anything that is not a dict as
        # ``BALANCE_UNAVAILABLE`` so the wizard surfaces a clear error
        # rather than an opaque ``AttributeError``.
        if not isinstance(raw, dict):
            return make_failure(
                operation="balance",
                exchange=name,
                account=credentials["account"],
                code="BALANCE_UNAVAILABLE",
                message=(
                    "Hibachi /trade/account/info returned an unexpected "
                    "response shape."
                ),
            )

        balance_text = raw.get("balance")
        if balance_text is None or str(balance_text).strip() == "":
            return make_failure(
                operation="balance",
                exchange=name,
                account=credentials["account"],
                code="BALANCE_UNAVAILABLE",
                message=(
                    "Hibachi /trade/account/info returned a response without "
                    "a 'balance' field."
                ),
            )

        # Sanity-load the public market metadata. Failure here is
        # non-fatal — Phase 1 only needs the balance, but loading the
        # cache now means a later ``resolve_instrument`` call is
        # immediate. If the call fails we swallow the error and let
        # the resolver retry on demand.
        market_index: Dict[str, Dict[str, Any]] = {}
        try:
            market_index = _build_hibachi_market_index(_MarketCache.get())
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Hibachi market cache preload failed (non-fatal): %s",
                _redact(sanitize_error_message(str(exc))),
            )

        summary = _build_portfolio_summary(raw, market_index)
        return make_success(
            operation="balance",
            exchange=name,
            account=credentials["account"],
            balance=normalize_balance(summary.account_value, summary.unit),
            portfolio_summary=summary,
        )
    finally:
        _set_current_credentials(token)


def _build_portfolio_summary(
    raw: Mapping[str, Any],
    market_index: Mapping[str, Dict[str, Any]],
) -> CanonicalPortfolioSummary:
    """Translate a Hibachi ``/trade/account/info`` payload into the
    canonical portfolio summary.

    Hibachi's response carries net equity (``balance``), free
    collateral (``maximalWithdraw``), initial / maintenance margin,
    and the notional totals. We map them 1:1 onto the canonical
    fields. The unit is the settlement currency reported by
    Hibachi — ``USDT`` for the contracts we have seen; the helper
    falls back to ``USDT`` defensively.
    """
    unit = _unit_from_payload(raw, market_index) or "USDT"
    account_value = _money_text(raw.get("balance"), unit)
    withdrawable = _money_text(raw.get("maximalWithdraw"), unit)
    margin_used = _money_text(raw.get("initialMargin"), unit)
    total_position_value = _money_text(raw.get("totalPositionNotional"), unit)
    return CanonicalPortfolioSummary(
        account_value=account_value,
        withdrawable=withdrawable,
        margin_used=margin_used,
        total_position_value=total_position_value,
        unit=unit,
    )


def _unit_from_payload(
    raw: Mapping[str, Any],
    market_index: Mapping[str, Dict[str, Any]],
) -> str:
    """Pick the canonical unit for a balance response.

    Hibachi reports USDT in every payload we have observed, so the
    default is ``USDT``. We only override the unit when the operator
    has configured an explicit non-USDT settlement (no documented case
    today, but the resolver leaves the door open).
    """
    # Prefer the most common settlement across the market index, since
    # a future spot market in a different unit would otherwise pull
    # the summary into mixed units.
    counts: Dict[str, int] = {}
    for descriptor in market_index.values():
        unit = str(descriptor.get("settlement_symbol") or "").strip().upper()
        if unit:
            counts[unit] = counts.get(unit, 0) + 1
    if counts:
        return max(counts.items(), key=lambda item: item[1])[0]
    assets = raw.get("assets")
    if isinstance(assets, list):
        for entry in assets:
            if isinstance(entry, dict):
                unit = str(entry.get("symbol") or "").strip().upper()
                if unit:
                    return unit
    return "USDT"


def _money_text(value: Any, unit: str) -> str:
    """Render a monetary value as a 2dp string, preserving the unit
    and never raising on missing/garbage input."""
    if value is None or str(value).strip() == "":
        return normalize_balance("0", unit).value
    try:
        return normalize_balance(str(value), unit).value
    except (InvalidOperation, ValueError, TypeError) if False else Exception:  # noqa: BLE001
        return normalize_balance("0", unit).value


# ---------------------------------------------------------------------------
# Write: new_order (Phase 3)
# ---------------------------------------------------------------------------


def _new_order(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Place a single Hibachi limit order.

    Implementation deliberately routes through the batch endpoint
    ``POST /trade/orders`` with a single ``{"action": "place"}``
    child. That keeps the single-order path identical to the future
    ladder / multi-place path the user explicitly asked for, so we do
    not have to maintain two parallel submission/signing code paths.

    Scope intentionally mirrors the wizard's current UI:

    - symbol / side / volume / price
    - ``order_type == "limit"`` only
    - no reduce-only / TP / SL / trigger orders yet

    Verification strategy is the same one used by the other KAM
    exchanges: if the submit response returns an order id, read it back
    via ``GET /trade/order``; if that fails, fall back to scanning the
    open-orders snapshot for a row matching `(symbol, side, size,
    price)`. If neither succeeds, the submission is returned with a
    canonical ``VERIFICATION_FAILED`` envelope so the operator can
    decide whether to inspect the exchange UI before retrying.
    """
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message=(
                f"Hibachi account '{account}' is not configured. "
                "Set HIBACHI_<alias>_ACCOUNTID, HIBACHI_<alias>_APIKEY, "
                "and HIBACHI_<alias>_PRIVATEKEY."
            ),
        )

    requested_symbol = str(request.get("symbol") or "").strip()
    requested_side = str(request.get("side") or "").strip().lower()
    order_type = str(request.get("order_type") or "limit").strip().lower() or "limit"
    client_id = str(request.get("client_id") or request.get("clientId") or "").strip()
    reduce_only_raw = request.get("reduce_only")
    if reduce_only_raw is None:
        reduce_only_raw = request.get("reduceOnly")
    reduce_only = bool(reduce_only_raw) if reduce_only_raw is not None else False

    requested_volume = _decimal_or_none(
        request.get("volume") or request.get("size") or request.get("quantity")
    )
    requested_price = _decimal_or_none(request.get("price"))

    if not requested_symbol:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    if requested_side not in {"buy", "sell"}:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_SIDE",
            message="Side must be buy or sell.",
        )
    if order_type != "limit":
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_ORDER_TYPE",
            message="Only limit orders are currently supported for Hibachi.",
        )
    if requested_volume is None or requested_volume <= 0:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_VOLUME",
            message="Volume must be positive.",
        )
    if requested_price is None or requested_price <= 0:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_PRICE",
            message="Price must be positive.",
        )
    if reduce_only:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="UNSUPPORTED_PARAMETER",
            message="Hibachi reduce_only is not implemented yet.",
        )
    if client_id and not _CLIENT_ID_PATTERN.match(client_id):
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_CLIENT_ID",
            message="client_id must be 1-32 characters of letters, digits, or '-'.",
        )

    try:
        market_payload = _MarketCache.get()
        market_index = _build_hibachi_market_index(market_payload)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INSTRUMENT_RESOLUTION_UNAVAILABLE",
            message=_redact(sanitize_error_message(str(exc))),
        )

    try:
        descriptor = _resolve_canonical_instrument(requested_symbol, payload=market_payload)
    except ValueError as exc:
        if str(exc) == "INSTRUMENT_AMBIGUOUS":
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="INSTRUMENT_AMBIGUOUS",
                message=f"Hibachi instrument '{requested_symbol}' is ambiguous.",
            )
        raise
    if descriptor is None:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INSTRUMENT_NOT_FOUND",
            message=f"Unknown Hibachi instrument '{requested_symbol}'.",
        )

    step_size = _decimal_or_none(descriptor.get("step_size")) or Decimal("0")
    tick_size = _decimal_or_none(descriptor.get("tick_size")) or Decimal("0")
    min_order_size = _decimal_or_none(descriptor.get("min_order_size")) or Decimal("0")
    min_notional = _decimal_or_none(descriptor.get("min_notional")) or Decimal("0")

    try:
        submitted_volume = _quantize_down_to_increment(requested_volume, step_size)
        submitted_price = _quantize_down_to_increment(requested_price, tick_size)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_ORDER_PRECISION",
            message=_redact(sanitize_error_message(str(exc))),
        )

    if submitted_volume <= 0:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_VOLUME",
            message="Volume rounds down to zero at the market step size.",
        )
    if submitted_price <= 0:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_PRICE",
            message="Price rounds down to zero at the market tick size.",
        )
    if min_order_size > 0 and submitted_volume < min_order_size:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="INVALID_VOLUME",
            message="Volume is below the market minimum size.",
        )
    if min_notional > 0 and submitted_volume * submitted_price < min_notional:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="NOTIONAL_BELOW_MINIMUM",
            message=f"Order notional is below the market minimum ({_decimal_text(min_notional)}).",
        )

    order_result = CanonicalOrderResult(
        symbol=_canonical_symbol_from_request(requested_symbol),
        side=requested_side,
        order_type=order_type,
        requested_volume=_decimal_text(requested_volume),
        requested_price=_decimal_text(requested_price),
        submitted_volume=_decimal_text(submitted_volume),
        submitted_price=_decimal_text(submitted_price),
        verified=False,
        status="submitted",
    )

    token = _set_current_credentials(credentials)
    try:
        try:
            batch_payload = _build_single_order_batch_payload(
                credentials=credentials,
                descriptor=descriptor,
                side=requested_side,
                submitted_volume=submitted_volume,
                submitted_price=submitted_price,
                client_id=client_id or None,
                market_payload=market_payload if isinstance(market_payload, dict) else {},
            )
            response_payload = _submit_order_batch(credentials, batch_payload)
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="ORDER_SUBMISSION_FAILED",
                message=_redact(sanitize_error_message(str(exc))),
                order=order_result,
            )

        exchange_order_id = _extract_order_id_from_batch_response(response_payload)
        verification_context = _describe_hibachi_submit_response(response_payload)
        submit_rejection = _extract_hibachi_submit_rejection(response_payload)
        if submit_rejection is not None:
            final_result = CanonicalOrderResult(
                symbol=order_result.symbol,
                side=order_result.side,
                order_type=order_result.order_type,
                requested_volume=order_result.requested_volume,
                requested_price=order_result.requested_price,
                submitted_volume=order_result.submitted_volume,
                submitted_price=order_result.submitted_price,
                verified=False,
                status="rejected",
                exchange_order_id=exchange_order_id,
            )
            return make_failure(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                code="ORDER_REJECTED",
                message="Order was rejected by Hibachi before creation.",
                order=final_result,
                exchange_reason=submit_rejection,
            )
        if exchange_order_id is not None:
            order_result = CanonicalOrderResult(
                symbol=order_result.symbol,
                side=order_result.side,
                order_type=order_result.order_type,
                requested_volume=order_result.requested_volume,
                requested_price=order_result.requested_price,
                submitted_volume=order_result.submitted_volume,
                submitted_price=order_result.submitted_price,
                verified=False,
                status="submitted",
                exchange_order_id=exchange_order_id,
            )

        verified = _verify_submitted_order(
            credentials,
            descriptor=descriptor,
            side=requested_side,
            submitted_volume=submitted_volume,
            submitted_price=submitted_price,
            exchange_order_id=exchange_order_id,
        )

        final_result = CanonicalOrderResult(
            symbol=order_result.symbol,
            side=order_result.side,
            order_type=order_result.order_type,
            requested_volume=order_result.requested_volume,
            requested_price=order_result.requested_price,
            submitted_volume=order_result.submitted_volume,
            submitted_price=order_result.submitted_price,
            verified=verified,
            status="success" if verified else "submitted",
            exchange_order_id=exchange_order_id,
        )
        if verified:
            return make_success(
                operation="new_order",
                exchange=name,
                account=credentials["account"],
                order=final_result,
            )
        return make_failure(
            operation="new_order",
            exchange=name,
            account=credentials["account"],
            code="VERIFICATION_FAILED",
            message="Order submission could not be verified on Hibachi.",
            order=final_result,
            exchange_reason=verification_context,
        )
    finally:
        _set_current_credentials(token)


def _build_hibachi_ladder_payload(
    *,
    credentials: Dict[str, Any],
    descriptor: Mapping[str, Any],
    side: str,
    order_requests: Sequence[Mapping[str, Any]],
    market_payload: Mapping[str, Any],
) -> Dict[str, Any]:
    base_nonce = _next_hibachi_nonce()
    max_fees_percent = _hibachi_max_fees_percent(market_payload)
    native_symbol = str(descriptor.get("symbol") or "").strip()
    native_side = _SIDE_TO_HIBACHI_ORDER[side]
    contract_id = _require_contract_id(descriptor)
    underlying_decimals = _require_non_negative_int(descriptor.get("underlying_decimals"), field="underlying_decimals")
    settlement_decimals = _require_non_negative_int(descriptor.get("settlement_decimals"), field="settlement_decimals")
    children: List[Dict[str, Any]] = []
    for index, request in enumerate(order_requests):
        quantity = _decimal_or_none(request.get("quantity"))
        price = _decimal_or_none(request.get("price"))
        if quantity is None or quantity <= 0 or price is None or price <= 0:
            raise ValueError("Invalid Hibachi ladder child request")
        nonce = base_nonce + index
        children.append({
            "action": "place",
            "nonce": nonce,
            "symbol": native_symbol,
            "orderType": "LIMIT",
            "side": native_side,
            "quantity": _decimal_text(quantity),
            "price": _decimal_text(price),
            "maxFeesPercent": _decimal_text(max_fees_percent),
            "signature": _sign_hibachi_place_order(
                private_key=str(credentials.get("private_key") or ""),
                nonce=nonce,
                contract_id=contract_id,
                quantity=quantity,
                price=price,
                side=side,
                underlying_decimals=underlying_decimals,
                settlement_decimals=settlement_decimals,
                max_fees_percent=max_fees_percent,
            ),
        })
    account_id = _require_non_negative_int(credentials.get("account_id"), field="account_id")
    return {"accountId": account_id, "orders": children}


_HIBACHI_LADDER_MIN_VALID_CHILDREN = 2


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


def _build_hibachi_ladder_prices(
    start_price: Decimal,
    end_price: Decimal,
    order_count: int,
    tick_size: Decimal,
) -> List[Decimal]:
    if order_count <= 0:
        return []
    if order_count == 1:
        return [_quantize_down_to_increment((start_price + end_price) / Decimal("2"), tick_size)]
    step = (end_price - start_price) / Decimal(order_count - 1)
    prices = [
        _quantize_down_to_increment(start_price + (step * Decimal(index)), tick_size)
        for index in range(order_count)
    ]
    if start_price <= end_price:
        for index in range(1, len(prices)):
            if prices[index] < prices[index - 1]:
                prices[index] = prices[index - 1]
    else:
        for index in range(1, len(prices)):
            if prices[index] > prices[index - 1]:
                prices[index] = prices[index - 1]
    return prices


def _allocate_hibachi_ladder_sizes(
    total_volume: Decimal,
    order_count: int,
    step_size: Decimal,
    distribution: str,
) -> Tuple[List[Decimal], Decimal]:
    if step_size <= 0:
        raise ValueError("INVALID_INCREMENT")
    total_units = int((total_volume / step_size).to_integral_value(rounding="ROUND_DOWN"))
    if total_units < order_count:
        raise ValueError("INSUFFICIENT_VOLUME_FOR_ORDER_COUNT")
    weights = _ladder_distribution_weights(order_count, distribution)
    if not weights:
        raise ValueError("INVALID_ORDER_COUNT")
    total_weight = sum(weights, Decimal("0"))
    if total_weight <= 0:
        raise ValueError("INVALID_DISTRIBUTION")
    raw_units = [Decimal(total_units) * weight / total_weight for weight in weights]
    base_units = [int(unit.to_integral_value(rounding="ROUND_DOWN")) for unit in raw_units]
    residual = total_units - sum(base_units)
    remainders = [raw_units[index] - Decimal(base_units[index]) for index in range(order_count)]
    allocation = list(base_units)
    if residual > 0:
        order_indices = sorted(range(order_count), key=lambda index: (remainders[index], -index), reverse=True)
        for index in order_indices[:residual]:
            allocation[index] += 1
    sizes = [Decimal(units) * step_size for units in allocation]
    return sizes, Decimal(total_units) * step_size


def _build_hibachi_ladder_order_requests(
    *,
    descriptor: Mapping[str, Any],
    side: str,
    distribution: str,
    order_count: int,
    total_volume: Decimal,
    start_price: Decimal,
    end_price: Decimal,
) -> Tuple[List[Dict[str, Decimal]], Decimal]:
    tick_size = _decimal_or_none(descriptor.get("tick_size")) or Decimal("0")
    step_size = _decimal_or_none(descriptor.get("step_size")) or Decimal("0")
    prices = _build_hibachi_ladder_prices(start_price, end_price, order_count, tick_size)
    sizes, submitted_volume = _allocate_hibachi_ladder_sizes(total_volume, order_count, step_size, distribution)
    order_requests: List[Dict[str, Decimal]] = []
    for price, size in zip(prices, sizes):
        if size <= 0 or price <= 0:
            continue
        order_requests.append({"price": price, "quantity": size})
    merged_requests: List[Dict[str, Decimal]] = []
    for request in order_requests:
        if merged_requests and merged_requests[-1]["price"] == request["price"]:
            merged_requests[-1]["quantity"] = merged_requests[-1]["quantity"] + request["quantity"]
            continue
        merged_requests.append(dict(request))
    return merged_requests, submitted_volume


def _validate_hibachi_ladder_children(
    order_requests: Sequence[Mapping[str, Any]],
    *,
    min_order_size: Decimal,
    min_notional: Decimal,
    tick_size: Decimal,
    step_size: Decimal,
) -> List[Dict[str, Any]]:
    validated: List[Dict[str, Any]] = []
    for index, request in enumerate(order_requests, start=1):
        price = _decimal_or_none(request.get("price"))
        quantity = _decimal_or_none(request.get("quantity"))
        notional = price * quantity if price is not None and quantity is not None else None
        price_precision_ok = price is not None and price > 0 and (tick_size <= 0 or price % tick_size == 0)
        size_precision_ok = quantity is not None and quantity > 0 and (step_size <= 0 or quantity % step_size == 0)
        minimum_size_ok = quantity is not None and quantity >= min_order_size
        minimum_notional_ok = notional is not None and (min_notional <= 0 or notional >= min_notional)
        validated.append({
            "index": index,
            "price": price,
            "quantity": quantity,
            "notional": notional,
            "price_precision_ok": price_precision_ok,
            "size_precision_ok": size_precision_ok,
            "minimum_size_ok": minimum_size_ok,
            "minimum_notional_ok": minimum_notional_ok,
            "valid": price_precision_ok and size_precision_ok and minimum_size_ok and minimum_notional_ok,
        })
    return validated


def _hibachi_ladder_request_units(request: Mapping[str, Any], step_size: Decimal) -> int:
    quantity = _decimal_or_none(request.get("quantity")) or Decimal("0")
    if step_size <= 0:
        return 0
    units = (quantity / step_size).to_integral_value(rounding="ROUND_HALF_UP")
    return int(units)


def _redistribute_hibachi_ladder_units(
    order_requests: Sequence[Mapping[str, Any]],
    removed_units: int,
    step_size: Decimal,
) -> List[Dict[str, Decimal]]:
    if removed_units <= 0 or not order_requests:
        return [{"price": _decimal_or_none(request.get("price")) or Decimal("0"), "quantity": _decimal_or_none(request.get("quantity")) or Decimal("0")} for request in order_requests]
    current_units = [_hibachi_ladder_request_units(request, step_size) for request in order_requests]
    total_units = sum(current_units)
    if total_units <= 0:
        return [{"price": _decimal_or_none(request.get("price")) or Decimal("0"), "quantity": _decimal_or_none(request.get("quantity")) or Decimal("0")} for request in order_requests]
    raw_additions = [Decimal(removed_units) * Decimal(units) / Decimal(total_units) for units in current_units]
    base_additions = [int(addition.to_integral_value(rounding="ROUND_DOWN")) for addition in raw_additions]
    residual = removed_units - sum(base_additions)
    remainders = [raw_additions[index] - Decimal(base_additions[index]) for index in range(len(current_units))]
    allocation = list(base_additions)
    if residual > 0:
        order_indices = sorted(range(len(current_units)), key=lambda index: (remainders[index], -index), reverse=True)
        for index in order_indices[:residual]:
            allocation[index] += 1
    reconciled: List[Dict[str, Decimal]] = []
    for index, request in enumerate(order_requests):
        reconciled.append({
            "price": _decimal_or_none(request.get("price")) or Decimal("0"),
            "quantity": Decimal(current_units[index] + allocation[index]) * step_size,
        })
    return reconciled


def _reconcile_hibachi_ladder_children(
    order_requests: Sequence[Mapping[str, Any]],
    *,
    min_order_size: Decimal,
    min_notional: Decimal,
    tick_size: Decimal,
    step_size: Decimal,
) -> Tuple[List[Dict[str, Decimal]], int, str]:
    current_requests = [
        {"price": _decimal_or_none(request.get("price")) or Decimal("0"), "quantity": _decimal_or_none(request.get("quantity")) or Decimal("0")}
        for request in order_requests
        if (_decimal_or_none(request.get("quantity")) or Decimal("0")) > 0
    ]
    omitted_below_minimum = 0
    max_iterations = max(1, len(current_requests))
    for _ in range(max_iterations):
        validation = _validate_hibachi_ladder_children(
            current_requests,
            min_order_size=min_order_size,
            min_notional=min_notional,
            tick_size=tick_size,
            step_size=step_size,
        )
        invalid_precision = [child for child in validation if not child["price_precision_ok"] or not child["size_precision_ok"] or not child["minimum_size_ok"]]
        if invalid_precision:
            return current_requests, omitted_below_minimum, "INVALID_PRECISION"
        invalid_indices = [index for index, child in enumerate(validation) if not child["minimum_notional_ok"]]
        if not invalid_indices:
            break
        if len(invalid_indices) == len(current_requests):
            return [], omitted_below_minimum + len(invalid_indices), "NO_VALID_CHILDREN"
        remaining_count = len(current_requests) - len(invalid_indices)
        if remaining_count < _HIBACHI_LADDER_MIN_VALID_CHILDREN:
            return [], omitted_below_minimum + len(invalid_indices), "TOO_FEW_VALID_CHILDREN"
        removed_units = sum(_hibachi_ladder_request_units(current_requests[index], step_size) for index in invalid_indices)
        omitted_below_minimum += len(invalid_indices)
        invalid_index_set = set(invalid_indices)
        current_requests = [request for index, request in enumerate(current_requests) if index not in invalid_index_set]
        current_requests = _redistribute_hibachi_ladder_units(current_requests, removed_units, step_size)
    return current_requests, omitted_below_minimum, ""


def _extract_order_ids_from_batch_response(payload: Any) -> List[Optional[int]]:
    rows = _coerce_hibachi_order_rows(payload)
    return [_parse_optional_int(row.get("orderId") or row.get("order_id")) for row in rows]


def _extract_hibachi_submit_rejections(payload: Any) -> List[str]:
    rows = _coerce_hibachi_order_rows(payload)
    rejections: List[str] = []
    for row in rows:
        status = str(row.get("status") or "").strip().lower()
        if status == "failed":
            rejections.append(_redact(_describe_hibachi_submit_response({"orders": [row]})))
    return rejections


def _ladder(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message=(
                f"Hibachi account '{account}' is not configured. "
                "Set HIBACHI_<alias>_ACCOUNTID, HIBACHI_<alias>_APIKEY, "
                "and HIBACHI_<alias>_PRIVATEKEY."
            ),
        )

    requested_symbol = str(request.get("symbol") or "").strip()
    requested_side = str(request.get("side") or "").strip().lower()
    distribution = str(request.get("distribution") or "").strip().lower()
    try:
        order_count = int(str(request.get("order_count") or "").strip())
    except Exception:  # noqa: BLE001
        order_count = 0
    total_volume = _decimal_or_none(request.get("total_volume"))
    start_price = _decimal_or_none(request.get("start_price"))
    end_price = _decimal_or_none(request.get("end_price"))

    if not requested_symbol:
        return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_side not in {"buy", "sell"}:
        return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="INVALID_SIDE", message="Side must be buy or sell.")
    if order_count <= 0:
        return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="INVALID_ORDER_COUNT", message="Order count must be positive.")
    if total_volume is None or total_volume <= 0:
        return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="INVALID_VOLUME", message="Total volume must be positive.")
    if start_price is None or end_price is None or start_price <= 0 or end_price <= 0:
        return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="INVALID_PRICE", message="Start and end price are required.")
    if requested_side == "buy" and end_price >= start_price:
        return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="INVALID_LADDER_DIRECTION", message="BUY ladders require end price below start price.")
    if requested_side == "sell" and end_price <= start_price:
        return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="INVALID_LADDER_DIRECTION", message="SELL ladders require end price above start price.")

    token = _set_current_credentials(credentials)
    try:
        try:
            market_payload = _MarketCache.get()
        except Exception as exc:  # noqa: BLE001
            return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="INSTRUMENT_RESOLUTION_UNAVAILABLE", message=_redact(sanitize_error_message(str(exc))))

        try:
            descriptor = _resolve_canonical_instrument(requested_symbol, payload=market_payload)
        except ValueError as exc:
            if str(exc) == "INSTRUMENT_AMBIGUOUS":
                return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="INSTRUMENT_AMBIGUOUS", message=f"Hibachi instrument '{requested_symbol}' is ambiguous.")
            raise
        if descriptor is None:
            return make_failure(operation="ladder", exchange=name, account=credentials["account"], code="INSTRUMENT_NOT_FOUND", message=f"Unknown Hibachi instrument '{requested_symbol}'.")

        step_size = _decimal_or_none(descriptor.get("step_size")) or Decimal("0")
        tick_size = _decimal_or_none(descriptor.get("tick_size")) or Decimal("0")
        min_order_size = _decimal_or_none(descriptor.get("min_order_size")) or Decimal("0")
        min_notional = _decimal_or_none(descriptor.get("min_notional")) or Decimal("0")

        try:
            order_requests, submitted_volume = _build_hibachi_ladder_order_requests(
                descriptor=descriptor,
                side=requested_side,
                distribution=distribution,
                order_count=order_count,
                total_volume=total_volume,
                start_price=start_price,
                end_price=end_price,
            )
        except ValueError as exc:
            code = str(exc) or "INVALID_LADDER_REQUEST"
            return make_failure(operation="ladder", exchange=name, account=credentials["account"], code=code, message=sanitize_error_message(code.replace("_", " ").title()))

        final_requests, omitted_below_minimum, reconcile_reason = _reconcile_hibachi_ladder_children(
            order_requests,
            min_order_size=min_order_size,
            min_notional=min_notional,
            tick_size=tick_size,
            step_size=step_size,
        )

        def _ladder_result(*, status: str, verified: bool, partial: bool, submitted_requests: Sequence[Mapping[str, Any]], accepted_child_count: int, child_order_ids: List[int], batches: List[Dict[str, Any]]) -> CanonicalLadderResult:
            submitted_total = sum((_decimal_or_none(req.get("quantity")) or Decimal("0")) for req in submitted_requests)
            return CanonicalLadderResult(
                symbol=_canonical_symbol_from_request(requested_symbol),
                side=requested_side,
                distribution=distribution,
                requested_order_count=order_count,
                submitted_order_count=len(submitted_requests),
                requested_volume=_decimal_text(total_volume),
                submitted_volume=_decimal_text(submitted_total),
                batch_count=1 if submitted_requests else 0,
                verified=verified,
                partial=partial,
                status=status,
                accepted_child_count=accepted_child_count,
                omitted_order_count=(order_count - len(submitted_requests)) or None,
                omitted_below_minimum=omitted_below_minimum or None,
                child_order_ids=child_order_ids or None,
                batches=batches or None,
            )

        if not final_requests:
            failure_code = "LADDER_TOO_FEW_VALID_CHILDREN" if reconcile_reason == "TOO_FEW_VALID_CHILDREN" else "LADDER_NO_VALID_CHILDREN"
            failure_message = "Fewer than two valid ladder children remain after preflight." if failure_code == "LADDER_TOO_FEW_VALID_CHILDREN" else "No valid ladder children remain after preflight."
            return make_failure(
                operation="ladder",
                exchange=name,
                account=credentials["account"],
                code=failure_code,
                message=failure_message,
                ladder=_ladder_result(status="failed", verified=False, partial=False, submitted_requests=[], accepted_child_count=0, child_order_ids=[], batches=[]),
            )
        if reconcile_reason == "INVALID_PRECISION":
            return make_failure(
                operation="ladder",
                exchange=name,
                account=credentials["account"],
                code="LADDER_CHILD_INVALID",
                message="One or more ladder orders are invalid after reconciliation.",
                ladder=_ladder_result(status="failed", verified=False, partial=False, submitted_requests=[], accepted_child_count=0, child_order_ids=[], batches=[]),
            )
        if len(final_requests) < _HIBACHI_LADDER_MIN_VALID_CHILDREN:
            return make_failure(
                operation="ladder",
                exchange=name,
                account=credentials["account"],
                code="LADDER_TOO_FEW_VALID_CHILDREN",
                message="Fewer than two valid ladder children remain after preflight.",
                ladder=_ladder_result(status="failed", verified=False, partial=False, submitted_requests=[], accepted_child_count=0, child_order_ids=[], batches=[]),
            )

        try:
            batch_payload = _build_hibachi_ladder_payload(
                credentials=credentials,
                descriptor=descriptor,
                side=requested_side,
                order_requests=final_requests,
                market_payload=market_payload if isinstance(market_payload, dict) else {},
            )
            response_payload = _submit_order_batch(credentials, batch_payload)
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation="ladder",
                exchange=name,
                account=credentials["account"],
                code="ORDER_SUBMISSION_FAILED",
                message="Hibachi ladder submission failed.",
                exchange_reason=_redact(sanitize_error_message(str(exc))),
                ladder=_ladder_result(status="failed", verified=False, partial=False, submitted_requests=[], accepted_child_count=0, child_order_ids=[], batches=[]),
            )

        rows = _coerce_hibachi_order_rows(response_payload)
        accepted_pairs: List[Tuple[Mapping[str, Any], int]] = []
        batches: List[Dict[str, Any]] = []
        for index, row in enumerate(rows):
            order_id = _parse_optional_int(row.get("orderId") or row.get("order_id"))
            status = str(row.get("status") or "").strip().lower() or "accepted"
            batches.append({
                "index": index + 1,
                "status": status,
                "order_id": order_id,
                "response": _redact(_describe_hibachi_submit_response({"orders": [row]})),
            })
            if order_id is not None and index < len(final_requests):
                accepted_pairs.append((final_requests[index], order_id))
        rejections = _extract_hibachi_submit_rejections(response_payload)
        accepted_child_count = len(accepted_pairs)
        child_order_ids = [order_id for _req, order_id in accepted_pairs]
        if rejections:
            result = _ladder_result(
                status="partial" if accepted_child_count else "failed",
                verified=False,
                partial=bool(accepted_child_count),
                submitted_requests=[req for req, _oid in accepted_pairs],
                accepted_child_count=accepted_child_count,
                child_order_ids=child_order_ids,
                batches=batches,
            )
            return make_failure(
                operation="ladder",
                exchange=name,
                account=credentials["account"],
                code="ORDER_REJECTED",
                message="One or more Hibachi ladder orders were rejected before creation.",
                exchange_reason=" | ".join(rejections),
                ladder=result,
            )
        if accepted_child_count != len(final_requests):
            result = _ladder_result(
                status="partial" if accepted_child_count else "failed",
                verified=False,
                partial=accepted_child_count > 0,
                submitted_requests=[req for req, _oid in accepted_pairs],
                accepted_child_count=accepted_child_count,
                child_order_ids=child_order_ids,
                batches=batches,
            )
            return make_failure(
                operation="ladder",
                exchange=name,
                account=credentials["account"],
                code="ORDER_SUBMISSION_FAILED",
                message="Hibachi ladder submission did not return order IDs for every child.",
                exchange_reason=_describe_hibachi_submit_response(response_payload),
                ladder=result,
            )

        verified = True
        for request_child, order_id in accepted_pairs:
            child_verified = _verify_submitted_order(
                credentials,
                descriptor=descriptor,
                side=requested_side,
                submitted_volume=_decimal_or_none(request_child.get("quantity")) or Decimal("0"),
                submitted_price=_decimal_or_none(request_child.get("price")) or Decimal("0"),
                exchange_order_id=order_id,
            )
            if not child_verified:
                verified = False
                break
        result = _ladder_result(
            status="success" if verified else "submitted",
            verified=verified,
            partial=False,
            submitted_requests=final_requests,
            accepted_child_count=accepted_child_count,
            child_order_ids=child_order_ids,
            batches=batches,
        )
        if verified:
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
            code="VERIFICATION_FAILED",
            message="Ladder submission could not be fully verified on Hibachi.",
            exchange_reason=_describe_hibachi_submit_response(response_payload),
            ladder=result,
        )
    finally:
        _set_current_credentials(token)


def _build_single_order_batch_payload(
    *,
    credentials: Dict[str, Any],
    descriptor: Mapping[str, Any],
    side: str,
    submitted_volume: Decimal,
    submitted_price: Decimal,
    client_id: Optional[str],
    market_payload: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build the ``POST /trade/orders`` body for a single place action.

    Hibachi's batch endpoint is structurally identical to the single
    ``POST /trade/order`` endpoint except that ``accountId`` is lifted
    to the top level and each child row carries an ``action`` field.
    We intentionally standardise on the batch shape even for single
    orders so ladder / multi-place can reuse the exact same signing +
    response parsing logic later.
    """
    native_side = _SIDE_TO_HIBACHI_ORDER[side]
    nonce = _next_hibachi_nonce()
    max_fees_percent = _hibachi_max_fees_percent(market_payload)
    signature = _sign_hibachi_place_order(
        private_key=str(credentials.get("private_key") or ""),
        nonce=nonce,
        contract_id=_require_contract_id(descriptor),
        quantity=submitted_volume,
        price=submitted_price,
        side=side,
        underlying_decimals=_require_non_negative_int(descriptor.get("underlying_decimals"), field="underlying_decimals"),
        settlement_decimals=_require_non_negative_int(descriptor.get("settlement_decimals"), field="settlement_decimals"),
        max_fees_percent=max_fees_percent,
    )
    child: Dict[str, Any] = {
        "action": "place",
        "nonce": nonce,
        "symbol": str(descriptor.get("symbol") or "").strip(),
        "orderType": "LIMIT",
        "side": native_side,
        "quantity": _decimal_text(submitted_volume),
        "price": _decimal_text(submitted_price),
        "maxFeesPercent": _decimal_text(max_fees_percent),
        "signature": signature,
    }
    if client_id:
        child["clientId"] = client_id
    account_id = _require_non_negative_int(
        credentials.get("account_id"), field="account_id"
    )
    return {
        "accountId": account_id,
        "orders": [child],
    }


def _submit_order_batch(credentials: Dict[str, Any], payload: Mapping[str, Any]) -> Any:
    """Submit ``POST /trade/orders`` with one or more signed children."""
    url = f"{_account_api_base()}{_PATH_TRADE_ORDERS}"
    headers = {
        "Authorization": str(credentials.get("api_key") or "").strip(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    return _request_json("POST", url, headers=headers, body=dict(payload))


def _fetch_order_by_id(credentials: Dict[str, Any], order_id: int) -> Optional[Dict[str, Any]]:
    """Authenticated ``GET /trade/order`` read-back used for verification."""
    account_id = str(credentials.get("account_id") or "").strip()
    if not account_id:
        raise RuntimeError("Hibachi accountId is missing from the credentials")
    query = urllib.parse.urlencode({"accountId": account_id, "orderId": str(order_id)})
    url = f"{_account_api_base()}{_PATH_TRADE_ORDER}?{query}"
    headers = {
        "Authorization": str(credentials.get("api_key") or "").strip(),
        "Accept": "application/json",
    }
    payload = _request_json("GET", url, headers=headers)
    return payload if isinstance(payload, dict) else None


def _verify_submitted_order(
    credentials: Dict[str, Any],
    *,
    descriptor: Mapping[str, Any],
    side: str,
    submitted_volume: Decimal,
    submitted_price: Decimal,
    exchange_order_id: Optional[int],
) -> bool:
    """Verify a submitted order against Hibachi's eventually-consistent reads.

    Hibachi can acknowledge a successful submit before ``GET /trade/order``
    or ``GET /trade/orders`` reflects the new order. A single immediate
    read is therefore too strict and causes false ``VERIFICATION_FAILED``
    envelopes for orders that did get accepted. Retry both read paths for
    a short bounded window before giving up, then consult recent order
    history as a final fallback for orders that were accepted but are no
    longer open.
    """
    attempts = max(1, int(_ORDER_VERIFICATION_ATTEMPTS))
    for attempt in range(attempts):
        if exchange_order_id is not None:
            try:
                confirmed = _fetch_order_by_id(credentials, exchange_order_id)
            except Exception:
                confirmed = None
            if _hibachi_order_matches_submission(
                confirmed,
                descriptor=descriptor,
                side=side,
                submitted_volume=submitted_volume,
                submitted_price=submitted_price,
            ):
                return True
        try:
            open_orders = _fetch_open_orders(credentials)
        except Exception:
            open_orders = []
        for order in open_orders:
            if _hibachi_order_matches_submission(
                order,
                descriptor=descriptor,
                side=side,
                submitted_volume=submitted_volume,
                submitted_price=submitted_price,
            ):
                return True
        if attempt + 1 < attempts:
            time.sleep(max(0.0, float(_ORDER_VERIFICATION_SLEEP_SECONDS)))

    try:
        recent_orders = _fetch_recent_order_history(credentials, lookback_ms=10 * 60 * 1000)
    except Exception:
        recent_orders = []
    for order in recent_orders:
        if _hibachi_order_matches_submission(
            order,
            descriptor=descriptor,
            side=side,
            submitted_volume=submitted_volume,
            submitted_price=submitted_price,
        ):
            return True
    return False


def _fetch_recent_order_history(
    credentials: Dict[str, Any],
    *,
    lookback_ms: int,
) -> List[Dict[str, Any]]:
    """Fetch recent order history rows for verification fallback."""
    account_id = str(credentials.get("account_id") or "").strip()
    if not account_id:
        raise RuntimeError("Hibachi accountId is missing from the credentials")
    end_ms = int(time.time() * 1000)
    start_ms = max(0, end_ms - max(1000, int(lookback_ms)))
    query = urllib.parse.urlencode({
        "accountId": account_id,
        "startTime": str(start_ms),
        "endTime": str(end_ms),
    })
    url = f"{_account_api_base()}{_PATH_TRADE_ORDERS_HISTORY}?{query}"
    headers = {
        "Authorization": str(credentials.get("api_key") or "").strip(),
        "Accept": "application/json",
    }
    payload = _request_json("GET", url, headers=headers)
    if isinstance(payload, dict):
        inner = payload.get("orders")
        if isinstance(inner, list):
            return [entry for entry in inner if isinstance(entry, dict)]
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    return []


def _hibachi_max_fees_percent(payload: Mapping[str, Any]) -> Decimal:
    """Return a conservative ``maxFeesPercent`` for order submission.

    The docs require the submitted value to be *at least* the value
    returned by ``/market/exchange-info``. We therefore choose the
    maximum of maker and taker fee rates (falling back to the taker
    example ``0.00045`` from the docs when metadata is missing) so a
    post-only-vs-taking distinction never causes a server-side reject.
    """
    fee_config = payload.get("feeConfig") if isinstance(payload, Mapping) else None
    if not isinstance(fee_config, Mapping):
        return Decimal("0.00045")
    values = [
        _decimal_or_none(fee_config.get("tradeTakerFeeRate")),
        _decimal_or_none(fee_config.get("tradeMakerFeeRate")),
    ]
    values = [value for value in values if value is not None and value >= 0]
    return max(values) if values else Decimal("0.00045")


def _sign_hibachi_place_order(
    *,
    private_key: str,
    nonce: int,
    contract_id: int,
    quantity: Decimal,
    price: Optional[Decimal],
    side: str,
    underlying_decimals: int,
    settlement_decimals: int,
    max_fees_percent: Decimal,
) -> str:
    """Return the signature for a place-order buffer.

    Hibachi supports two write-path signing schemes depending on account
    type:

    - exchange-managed accounts: HMAC-SHA256(buffer) -> 32-byte digest
      encoded as lowercase hex
    - trustless / ECDSA-style accounts: sign SHA-256(buffer) with a
      secp256k1 private key and send the 65-byte ``r || s || v`` result as
      lowercase hex

    The live ``bitget`` credentials are shaped like a 0x-prefixed 32-byte
    hex private key, and Hibachi explicitly rejected the previous 32-byte
    HMAC hex digest with "expected 64-byte signature and a recovery ID".
    We therefore auto-detect that key shape and emit the documented 65-byte
    ECDSA signature for it, while retaining the original HMAC path for true
    secret-key credentials.
    """
    buffer = _build_hibachi_place_order_buffer(
        nonce=nonce,
        contract_id=contract_id,
        quantity=quantity,
        price=price,
        side=side,
        underlying_decimals=underlying_decimals,
        settlement_decimals=settlement_decimals,
        max_fees_percent=max_fees_percent,
    )
    normalized_private_key = str(private_key or "").strip()
    if _looks_like_hibachi_ecdsa_private_key(normalized_private_key):
        digest = hashlib.sha256(buffer).digest()
        key_bytes = bytes.fromhex(normalized_private_key[2:])
        signature = EthPrivateKey(key_bytes).sign_msg_hash(digest)
        return (
            signature.r.to_bytes(32, "big").hex()
            + signature.s.to_bytes(32, "big").hex()
            + signature.v.to_bytes(1, "big").hex()
        )
    return hmac.new(
        normalized_private_key.encode("utf-8"),
        buffer,
        digestmod="sha256",
    ).hexdigest()


def _looks_like_hibachi_ecdsa_private_key(value: str) -> bool:
    text = str(value or "").strip()
    return bool(re.fullmatch(r"0x[0-9a-fA-F]{64}", text))


def _build_hibachi_place_order_buffer(
    *,
    nonce: int,
    contract_id: int,
    quantity: Decimal,
    price: Optional[Decimal],
    side: str,
    underlying_decimals: int,
    settlement_decimals: int,
    max_fees_percent: Decimal,
) -> bytes:
    side_int = _SIDE_TO_SIGNATURE_INT.get(side)
    if side_int is None:
        raise ValueError(f"Unsupported Hibachi side for signing: {side!r}")
    quantity_int = _encode_hibachi_quantity(quantity, underlying_decimals)
    fees_int = _encode_hibachi_fee_rate(max_fees_percent)
    parts = [
        _uint_to_bytes(int(nonce), 8),
        _uint_to_bytes(int(contract_id), 4),
        _uint_to_bytes(quantity_int, 8),
        _uint_to_bytes(int(side_int), 4),
    ]
    if price is not None:
        price_int = _encode_hibachi_price(price, underlying_decimals, settlement_decimals)
        parts.append(_uint_to_bytes(price_int, 8))
    parts.append(_uint_to_bytes(fees_int, 8))
    return b"".join(parts)


def _encode_hibachi_quantity(quantity: Decimal, underlying_decimals: int) -> int:
    scaled = quantity * (Decimal(10) ** int(underlying_decimals))
    return _decimal_to_uint_exact(scaled, field="quantity")


def _encode_hibachi_price(
    price: Decimal,
    underlying_decimals: int,
    settlement_decimals: int,
) -> int:
    scaled = (
        price
        * Decimal(_PRICE_MULTIPLIER)
        * (Decimal(10) ** (int(settlement_decimals) - int(underlying_decimals)))
    )
    # The official Hibachi Python SDK uses ``int(...)`` on this scaled
    # Decimal, which truncates toward zero for positive values. Some valid
    # tick-aligned ETH prices land on non-integral fixed-point values, so
    # SDK parity requires truncation/flooring here rather than half-up
    # rounding.
    return _decimal_to_uint_truncated(scaled, field="price")


def _encode_hibachi_fee_rate(max_fees_percent: Decimal) -> int:
    scaled = max_fees_percent * _MAX_FEES_RATE_SCALE
    return _decimal_to_uint_exact(scaled, field="maxFeesPercent")


def _decimal_to_uint_exact(value: Decimal, *, field: str) -> int:
    if value < 0:
        raise ValueError(f"{field} must be non-negative for Hibachi signing")
    integral = value.to_integral_value(rounding=ROUND_HALF_UP)
    if integral != value:
        raise ValueError(f"{field} cannot be represented exactly in Hibachi's signed integer format")
    return int(integral)


def _decimal_to_uint_truncated(value: Decimal, *, field: str) -> int:
    """Truncate a non-negative Decimal toward zero to an unsigned integer.

    Hibachi's official Python SDK encodes signed order-price fixed-point
    values via ``int(...)`` on a positive Decimal. Match that behavior
    exactly so our offline digest equals the SDK/server digest byte-for-byte.
    """
    if value < 0:
        raise ValueError(f"{field} must be non-negative for Hibachi signing")
    return int(value)


def _uint_to_bytes(value: int, width: int) -> bytes:
    if value < 0:
        raise ValueError("Hibachi signing integers must be non-negative")
    return int(value).to_bytes(width, byteorder="big", signed=False)


def _next_hibachi_nonce() -> int:
    """Return a microsecond timestamp nonce.

    Hibachi accepts milliseconds or microseconds; we use microseconds so
    rapid successive submissions in the same second remain unique.
    """
    return int(time.time_ns() // 1_000)


def _require_contract_id(descriptor: Mapping[str, Any]) -> int:
    contract_id = _parse_optional_int(descriptor.get("id"))
    if contract_id is None or contract_id <= 0:
        raise ValueError("Resolved Hibachi instrument is missing a valid contract id")
    return contract_id


def _require_non_negative_int(value: Any, *, field: str) -> int:
    parsed = _parse_optional_int(value)
    if parsed is None or parsed < 0:
        raise ValueError(f"Resolved Hibachi instrument is missing a valid {field}")
    return parsed


def _parse_optional_int(value: Any) -> Optional[int]:
    try:
        rendered = str(value or "").strip()
        if not rendered:
            return None
        return int(rendered)
    except Exception:  # noqa: BLE001
        return None


def _extract_order_id_from_batch_response(payload: Any) -> Optional[int]:
    """Pull the first ``orderId`` out of ``POST /trade/orders`` response."""
    rows = _coerce_hibachi_order_rows(payload)
    if not rows:
        return None
    first = rows[0]
    return _parse_optional_int(first.get("orderId") or first.get("order_id"))


def _coerce_hibachi_order_rows(payload: Any) -> List[Dict[str, Any]]:
    """Coerce Hibachi order-list payloads into a list of dict rows.

    Hibachi inconsistently returns both a bare list and a top-level
    ``{"orders": [...]}`` object across endpoints and environments. Use a
    single helper so submit parsing, open-order reads, and diagnostics all
    agree on the accepted shapes.
    """
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if isinstance(payload, Mapping):
        inner = payload.get("orders")
        if isinstance(inner, list):
            return [entry for entry in inner if isinstance(entry, dict)]
    return []


def _describe_hibachi_submit_response(payload: Any) -> str:
    """Return a compact, redacted summary of the batch submit response."""
    rows = _coerce_hibachi_order_rows(payload)
    if rows:
        first = rows[0]
        parts = [f"submit order row keys={sorted(first.keys())}"]
        order_id = first.get("orderId") or first.get("order_id")
        if order_id is not None:
            parts.append(f"orderId={order_id}")
        status = first.get("status")
        if status is not None:
            parts.append(f"status={status}")
        message = first.get("message")
        if message:
            parts.append(f"message={message}")
        if isinstance(payload, Mapping) and "orders" in payload:
            parts.append("shape=dict.orders")
        elif isinstance(payload, list):
            parts.append("shape=list")
        return _redact("; ".join(parts))
    if isinstance(payload, list):
        return "submit response: empty list"
    if isinstance(payload, Mapping):
        parts = [f"submit response keys={sorted(payload.keys())}"]
        for key in ("status", "message", "errorCode", "error", "reason"):
            value = payload.get(key)
            if value is not None and value != "":
                parts.append(f"{key}={value}")
        return _redact("; ".join(parts))
    if payload is None:
        return "submit response: null"
    return f"submit response type={type(payload).__name__}"


def _extract_hibachi_submit_rejection(payload: Any) -> Optional[str]:
    """Return a redacted rejection summary for explicit failed submit rows."""
    rows = _coerce_hibachi_order_rows(payload)
    if not rows:
        return None
    first = rows[0]
    status = str(first.get("status") or "").strip().lower()
    if status != "failed":
        return None
    return _describe_hibachi_submit_response(payload)


def _hibachi_order_matches_submission(
    raw: Any,
    *,
    descriptor: Mapping[str, Any],
    side: str,
    submitted_volume: Decimal,
    submitted_price: Decimal,
) -> bool:
    if not isinstance(raw, Mapping):
        return False
    expected_symbol = str(descriptor.get("symbol") or "").strip()
    if str(raw.get("symbol") or "").strip() != expected_symbol:
        return False
    raw_side = _normalize_hibachi_side(raw.get("side"), mapping=_HIBACHI_SIDE_FROM_ORDER)
    if raw_side != side:
        return False
    raw_size = _decimal_or_none(raw.get("totalQuantity") or raw.get("quantity"))
    raw_price = _decimal_or_none(raw.get("price"))
    if raw_size is None or raw_price is None:
        return False
    return raw_size == submitted_volume and raw_price == submitted_price


def _quantize_down_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    """Floor ``value`` to an exchange increment.

    Hibachi contracts expose decimal-string tick/step sizes in
    ``/market/exchange-info``. We floor to those increments rather than
    round half-up so we never accidentally submit more volume or a more
    aggressive price than the operator asked for.
    """
    if increment is None or increment <= 0:
        return value
    steps = (value / increment).to_integral_value(rounding="ROUND_DOWN")
    quantized = steps * increment
    # Render through Decimal(str(...)) semantics rather than normalize()
    # so trailing zeros implied by the increment stay intact when the
    # caller later formats with ``_decimal_text`` / ``_format_decimal_places``.
    return quantized


# ---------------------------------------------------------------------------
# Read: positions_orders (Phase 2)
# ---------------------------------------------------------------------------


_HIBACHI_SIDE_FROM_ORDER = {"BID": "buy", "ASK": "sell", "BUY": "buy", "SELL": "sell"}
_HIBACHI_SIDE_FROM_POSITION = {"Long": "long", "Short": "short", "long": "long", "short": "short"}
_HIBACHI_ORDER_SIDE_NORMALIZED = {"buy": "buy", "sell": "sell"}


def _fetch_open_orders(credentials: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Authenticated ``GET /trade/orders`` call.

    Hibachi documents the endpoint as a flat list of order objects.
    We defensively coerce the response into a list — the live API
    has been observed to occasionally return a bare object in error
    paths — and trust the empty list when the shape is unexpected
    (the wizard's render already handles "0 open orders" cleanly).
    """
    base = _account_api_base()
    account_id = str(credentials.get("account_id") or "").strip()
    if not account_id:
        raise RuntimeError("Hibachi accountId is missing from the credentials")
    url = (
        f"{base}{_PATH_TRADE_ORDERS}"
        f"?{urllib.parse.urlencode({'accountId': account_id})}"
    )
    headers = {
        "Authorization": str(credentials.get("api_key") or "").strip(),
        "Accept": "application/json",
    }
    payload = _request_json("GET", url, headers=headers)
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    # Some error paths return ``{"orders": [...], ...}`` even though
    # the documented success shape is a bare list — be tolerant.
    if isinstance(payload, dict):
        inner = payload.get("orders")
        if isinstance(inner, list):
            return [entry for entry in inner if isinstance(entry, dict)]
    return []


def _cancel_order_group(account: str, request: Mapping[str, Any]) -> CanonicalResponse:
    """Cancel exactly the open Hibachi orders matching ``(symbol, side)``.

    The wizard presents grouped open orders by canonical symbol and side.
    Hibachi does not currently expose a dedicated exact-scope group-cancel
    endpoint, so we follow the official SDK's workaround strategy and submit
    one ``DELETE /trade/order`` per targeted order id.
    """
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message=(
                f"Hibachi account '{account}' is not configured. "
                "Set HIBACHI_<alias>_ACCOUNTID, HIBACHI_<alias>_APIKEY, "
                "and HIBACHI_<alias>_PRIVATEKEY."
            ),
        )

    requested_raw = str(request.get("symbol") or "").strip()
    requested_side = str(request.get("side") or "").strip().lower()
    if not requested_raw:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=credentials["account"],
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    if requested_side not in {"buy", "sell"}:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=credentials["account"],
            code="INVALID_SIDE",
            message="Side must be buy or sell.",
        )
    try:
        descriptor = _resolve_canonical_instrument(requested_raw)
    except ValueError as exc:
        if str(exc) == "INSTRUMENT_AMBIGUOUS":
            return make_failure(
                operation="cancel_order_group",
                exchange=name,
                account=credentials["account"],
                code="INSTRUMENT_AMBIGUOUS",
                message=f"Hibachi instrument '{requested_raw}' is ambiguous.",
            )
        raise
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=credentials["account"],
            code="INSTRUMENT_RESOLUTION_UNAVAILABLE",
            message=_redact(sanitize_error_message(str(exc))),
        )
    if descriptor is None:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=credentials["account"],
            code="INSTRUMENT_NOT_FOUND",
            message=f"Unknown Hibachi instrument '{requested_raw}'.",
        )
    requested_symbol = str(descriptor.get("underlying_symbol") or "")

    token = _set_current_credentials(credentials)
    try:
        try:
            pre_orders = _fetch_open_orders(credentials)
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation="cancel_order_group",
                exchange=name,
                account=credentials["account"],
                code="OPEN_ORDERS_UNAVAILABLE",
                message=_redact(sanitize_error_message(str(exc))),
            )

        target_orders = [
            order for order in pre_orders
            if _hibachi_order_matches_cancel_group(
                order,
                symbol=requested_symbol,
                side=requested_side,
            )
        ]
        if not target_orders:
            return make_failure(
                operation="cancel_order_group",
                exchange=name,
                account=credentials["account"],
                code="NO_TARGET_ORDERS",
                message="No matching orders were found.",
            )

        targeted_ids: List[int] = []
        for order in target_orders:
            order_id = _parse_optional_int(order.get("orderId") or order.get("order_id"))
            if order_id is None:
                return make_failure(
                    operation="cancel_order_group",
                    exchange=name,
                    account=credentials["account"],
                    code="INVALID_ORDER_ID",
                    message="A matching Hibachi order is missing its orderId.",
                )
            targeted_ids.append(order_id)

        cancelled_count = 0
        batches: List[Dict[str, Any]] = []
        rejection_message: Optional[str] = None
        for order_id in targeted_ids:
            try:
                cancel_response = _submit_cancel_order(credentials, order_id=order_id)
                cancelled_count += 1
                batches.append({
                    "order_id": order_id,
                    "status": "submitted",
                    "response": _redact(sanitize_error_message(json.dumps(cancel_response, sort_keys=True))),
                })
            except Exception as exc:  # noqa: BLE001
                rejection_message = _redact(sanitize_error_message(str(exc)))
                batches.append({
                    "order_id": order_id,
                    "status": "failed",
                    "response": rejection_message,
                })
                break

        try:
            post_orders = _fetch_open_orders(credentials)
        except Exception as exc:  # noqa: BLE001
            post_orders = pre_orders
            if rejection_message is None:
                rejection_message = _redact(sanitize_error_message(str(exc)))

        remaining_orders = [
            order for order in post_orders
            if _hibachi_order_matches_cancel_group(
                order,
                symbol=requested_symbol,
                side=requested_side,
            )
        ]
        remaining_target_count = len(remaining_orders)
        confirmed_absent_count = max(0, len(targeted_ids) - remaining_target_count)
        result = CanonicalCancelGroupResult(
            symbol=requested_symbol,
            side=requested_side,
            targeted_order_count=len(targeted_ids),
            cancelled_order_count=cancelled_count,
            confirmed_absent_count=confirmed_absent_count,
            remaining_target_count=remaining_target_count,
            verified=(remaining_target_count == 0 and rejection_message is None),
            partial=(remaining_target_count != 0 or cancelled_count != len(targeted_ids)),
            status="success" if remaining_target_count == 0 and rejection_message is None else "partial",
            batch_count=len(batches),
            batches=batches,
        )
        if rejection_message is not None:
            return make_failure(
                operation="cancel_order_group",
                exchange=name,
                account=credentials["account"],
                code="CANCEL_REJECTED",
                message="Hibachi rejected one of the exact-scope cancellation requests.",
                cancel_group=result,
                exchange_reason=rejection_message,
            )
        if remaining_target_count != 0:
            return make_failure(
                operation="cancel_order_group",
                exchange=name,
                account=credentials["account"],
                code="CANCEL_GROUP_VERIFICATION_FAILED",
                message="Some matching Hibachi orders remain open after cancellation.",
                cancel_group=result,
            )
        return make_success(
            operation="cancel_order_group",
            exchange=name,
            account=credentials["account"],
            cancel_group=result,
        )
    finally:
        _set_current_credentials(token)


def _hibachi_order_matches_cancel_group(
    order: Mapping[str, Any],
    *,
    symbol: str,
    side: str,
) -> bool:
    return (
        _canonical_symbol_from_request(order.get("symbol")) == symbol
        and _normalize_hibachi_side(order.get("side"), mapping=_HIBACHI_SIDE_FROM_ORDER) == side
    )


def _build_hibachi_cancel_payload(*, order_id: Optional[int], nonce: Optional[int]) -> bytes:
    if order_id is not None:
        return _uint_to_bytes(int(order_id), 8)
    if nonce is None:
        raise ValueError("Either order_id or nonce must be provided for Hibachi cancellation")
    return _uint_to_bytes(int(nonce), 8)


def _sign_hibachi_payload(*, private_key: str, payload: bytes) -> str:
    normalized_private_key = str(private_key or "").strip()
    if _looks_like_hibachi_ecdsa_private_key(normalized_private_key):
        digest = hashlib.sha256(payload).digest()
        key_bytes = bytes.fromhex(normalized_private_key[2:])
        signature = EthPrivateKey(key_bytes).sign_msg_hash(digest)
        return (
            signature.r.to_bytes(32, "big").hex()
            + signature.s.to_bytes(32, "big").hex()
            + signature.v.to_bytes(1, "big").hex()
        )
    return hmac.new(
        normalized_private_key.encode("utf-8"),
        payload,
        digestmod="sha256",
    ).hexdigest()


def _build_hibachi_cancel_request_data(
    *,
    private_key: str,
    order_id: Optional[int],
    nonce: Optional[int],
) -> Dict[str, Any]:
    payload = _build_hibachi_cancel_payload(order_id=order_id, nonce=nonce)
    signature = _sign_hibachi_payload(private_key=private_key, payload=payload)
    request: Dict[str, Any] = {"signature": signature}
    if order_id is not None:
        request["orderId"] = str(order_id)
    else:
        request["nonce"] = str(nonce)
    return request


def _submit_cancel_order(
    credentials: Dict[str, Any],
    *,
    order_id: Optional[int] = None,
    nonce: Optional[int] = None,
) -> Any:
    account_id = _require_non_negative_int(credentials.get("account_id"), field="account_id")
    body = _build_hibachi_cancel_request_data(
        private_key=str(credentials.get("private_key") or ""),
        order_id=order_id,
        nonce=nonce,
    )
    body["accountId"] = account_id
    url = f"{_account_api_base()}{_PATH_TRADE_ORDER}"
    headers = {
        "Authorization": str(credentials.get("api_key") or "").strip(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    return _request_json("DELETE", url, headers=headers, body=body)


def _normalize_hibachi_side(value: Any, *, mapping: Mapping[str, str]) -> Optional[str]:
    """Map a Hibachi side string through a fixed mapping table.

    Returns ``None`` when the value is empty or unrecognised so the
    caller can decide whether to drop the row or surface an error.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return mapping.get(text) or mapping.get(text.title()) or mapping.get(text.upper())


def _positions_orders(account: str) -> CanonicalResponse:
    """Fetch positions + open orders and normalize to the wizard's
    combined "📋 Open Orders & 💼 Positions" view.

    Hibachi's positions ride inside the ``/trade/account/info``
    payload we already fetch for ``balance``; we re-fetch the same
    endpoint rather than threading a shared cache through the
    dispatch path so the request is independently retryable. Open
    orders come from the dedicated ``/trade/orders`` endpoint and
    are bucketed by ``(canonical_symbol, side)`` per the canonical
    order-group contract.

    Errors raised by either HTTP call are converted to
    ``POSITIONS_ORDERS_UNAVAILABLE`` so the wizard surfaces a single,
    clear failure code (the request is logically atomic from the
    wizard's perspective — partial data is more confusing than a
    clean retry).
    """
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message=(
                f"Hibachi account '{account}' is not configured. "
                "Set HIBACHI_<alias>_ACCOUNTID, HIBACHI_<alias>_APIKEY, "
                "and HIBACHI_<alias>_PRIVATEKEY."
            ),
        )
    token = _set_current_credentials(credentials)
    try:
        try:
            raw_account = _fetch_account_info(credentials)
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation="positions_orders",
                exchange=name,
                account=credentials["account"],
                code="POSITIONS_ORDERS_UNAVAILABLE",
                message=_redact(sanitize_error_message(str(exc))),
            )
        try:
            raw_orders = _fetch_open_orders(credentials)
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation="positions_orders",
                exchange=name,
                account=credentials["account"],
                code="POSITIONS_ORDERS_UNAVAILABLE",
                message=_redact(sanitize_error_message(str(exc))),
            )

        positions = _normalize_positions_from_account_info(raw_account)
        open_order_count, order_groups = _group_open_orders(raw_orders)
        return make_success(
            operation="positions_orders",
            exchange=name,
            account=credentials["account"],
            positions=positions,
            open_order_count=open_order_count,
            order_groups=order_groups,
        )
    finally:
        _set_current_credentials(token)


def _positions_management(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="positions_management",
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message=(
                f"Hibachi account '{account}' is not configured. "
                "Set HIBACHI_<alias>_ACCOUNTID, HIBACHI_<alias>_APIKEY, "
                "and HIBACHI_<alias>_PRIVATEKEY."
            ),
        )
    token = _set_current_credentials(credentials)
    try:
        try:
            raw_account = _fetch_account_info(credentials)
            raw_orders = _fetch_open_orders(credentials)
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation="positions_management",
                exchange=name,
                account=credentials["account"],
                code="POSITIONS_UNAVAILABLE",
                message=_redact(sanitize_error_message(str(exc))),
            )
        positions = _augment_positions_with_protection(
            _normalize_positions_from_account_info(raw_account),
            raw_orders,
        )
        return make_success(
            operation="positions_management",
            exchange=name,
            account=credentials["account"],
            positions=positions,
        )
    finally:
        _set_current_credentials(token)


def _position_action_result(
    *,
    operation: str,
    symbol: str,
    verified: bool,
    price: Optional[str] = None,
    removed: Optional[bool] = None,
    status: str = "success",
    exchange_order_id: Optional[int] = None,
    current_side: Optional[str] = None,
    current_size: Optional[str] = None,
    message: Optional[str] = None,
) -> CanonicalPositionActionResult:
    return CanonicalPositionActionResult(
        operation=operation,
        symbol=str(symbol or "").strip().upper(),
        verified=bool(verified),
        price=price,
        removed=removed,
        status=status,
        exchange_order_id=exchange_order_id,
        current_side=current_side,
        current_size=current_size,
        message=message,
    )


def _augment_positions_with_protection(
    positions: Sequence[CanonicalPosition],
    raw_orders: Sequence[Mapping[str, Any]],
) -> List[CanonicalPosition]:
    if not positions:
        return []
    augmented: List[CanonicalPosition] = []
    for position in positions:
        protection = _classify_hibachi_protection_orders(
            raw_orders,
            symbol=position.symbol,
            current_side=position.side,
        )
        tp_orders = protection["tp"]
        sl_orders = protection["sl"]
        tp_price = _first_trigger_price(tp_orders, symbol=position.symbol)
        sl_price = _first_trigger_price(sl_orders, symbol=position.symbol)
        augmented.append(CanonicalPosition(
            symbol=position.symbol,
            side=position.side,
            size=position.size,
            entry_price=position.entry_price,
            pnl=position.pnl,
            tp=tp_price,
            sl=sl_price,
            tp_count=len(tp_orders) or None,
            sl_count=len(sl_orders) or None,
        ))
    return augmented


def _first_trigger_price(orders: Sequence[Mapping[str, Any]], *, symbol: str) -> Optional[str]:
    for order in orders:
        price = order.get("triggerPrice") if isinstance(order, Mapping) else None
        if price is None:
            continue
        rendered = _format_hibachi_display_price(price, symbol=symbol)
        if rendered != "0":
            return rendered
    return None


def _order_flags_include_reduce_only(value: Any) -> bool:
    text = str(value or "").strip().upper()
    if not text:
        return False
    parts = {part.strip() for part in re.split(r"[|,\s]+", text) if part.strip()}
    return "REDUCE_ONLY" in parts


def _expected_trigger_direction(current_side: str, operation: str) -> Optional[str]:
    side = str(current_side or "").strip().lower()
    if side == "long":
        return "HIGH" if operation == "set_tp" else "LOW"
    if side == "short":
        return "LOW" if operation == "set_tp" else "HIGH"
    return None


def _classify_hibachi_protection_orders(
    raw_orders: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    current_side: str,
) -> Dict[str, List[Dict[str, Any]]]:
    result: Dict[str, List[Dict[str, Any]]] = {"tp": [], "sl": []}
    canonical_symbol = _canonical_symbol_from_request(symbol)
    closing_side = "sell" if str(current_side or "").strip().lower() == "long" else "buy"
    for entry in raw_orders:
        if not isinstance(entry, Mapping):
            continue
        if _canonical_symbol_from_request(entry.get("symbol")) != canonical_symbol:
            continue
        raw_side = _normalize_hibachi_side(entry.get("side"), mapping=_HIBACHI_SIDE_FROM_ORDER)
        if raw_side != closing_side:
            continue
        if not _order_flags_include_reduce_only(entry.get("orderFlags")):
            continue
        trigger_price = _decimal_or_none(entry.get("triggerPrice"))
        if trigger_price is None or trigger_price <= 0:
            continue
        trigger_direction = str(entry.get("triggerDirection") or "").strip().upper()
        if trigger_direction not in {"HIGH", "LOW"}:
            continue
        kind = None
        if str(current_side or "").strip().lower() == "long":
            kind = "tp" if trigger_direction == "HIGH" else "sl"
        elif str(current_side or "").strip().lower() == "short":
            kind = "tp" if trigger_direction == "LOW" else "sl"
        if kind is None:
            continue
        result[kind].append(dict(entry))
    for kind in ("tp", "sl"):
        result[kind].sort(key=lambda item: (_decimal_or_none(item.get("triggerPrice")) or Decimal("0"), int(_parse_optional_int(item.get("orderId")) or 0)))
    return result


def _submit_single_order(credentials: Dict[str, Any], payload: Mapping[str, Any]) -> Any:
    url = f"{_account_api_base()}{_PATH_TRADE_ORDER}"
    headers = {
        "Authorization": str(credentials.get("api_key") or "").strip(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    return _request_json("POST", url, headers=headers, body=dict(payload))


def _cancel_hibachi_order(credentials: Dict[str, Any], *, order_id: int) -> Any:
    payload = _build_hibachi_cancel_request_data(
        private_key=str(credentials.get("private_key") or ""),
        order_id=order_id,
        nonce=None,
    )
    account_id = _require_non_negative_int(credentials.get("account_id"), field="account_id")
    payload["accountId"] = account_id
    headers = {
        "Authorization": str(credentials.get("api_key") or "").strip(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    return _request_json("DELETE", f"{_account_api_base()}{_PATH_TRADE_ORDER}", headers=headers, body=payload)


def _build_hibachi_trigger_order_payload(
    *,
    credentials: Dict[str, Any],
    descriptor: Mapping[str, Any],
    current_side: str,
    current_size: Decimal,
    trigger_price: Decimal,
    operation: str,
    market_payload: Mapping[str, Any],
) -> Dict[str, Any]:
    contract_id = _require_contract_id(descriptor)
    underlying_decimals = _require_non_negative_int(descriptor.get("underlying_decimals"), field="underlying_decimals")
    settlement_decimals = _require_non_negative_int(descriptor.get("settlement_decimals"), field="settlement_decimals")
    nonce = _next_hibachi_nonce()
    max_fees_percent = _hibachi_max_fees_percent(market_payload)
    closing_side = "sell" if current_side == "long" else "buy"
    native_side = _SIDE_TO_HIBACHI_ORDER[closing_side]
    trigger_direction = _expected_trigger_direction(current_side, operation)
    if trigger_direction is None:
        raise ValueError("Unknown position side for TP/SL")
    signature = _sign_hibachi_place_order(
        private_key=str(credentials.get("private_key") or ""),
        nonce=nonce,
        contract_id=contract_id,
        quantity=current_size,
        price=None,
        side=closing_side,
        underlying_decimals=underlying_decimals,
        settlement_decimals=settlement_decimals,
        max_fees_percent=max_fees_percent,
    )
    account_id = _require_non_negative_int(credentials.get("account_id"), field="account_id")
    return {
        "accountId": account_id,
        "nonce": nonce,
        "symbol": str(descriptor.get("symbol") or "").strip(),
        "quantity": _decimal_text(current_size),
        "orderType": "MARKET",
        "side": native_side,
        "maxFeesPercent": _decimal_text(max_fees_percent),
        "signature": signature,
        "triggerPrice": _decimal_text(trigger_price),
        "triggerDirection": trigger_direction,
        "orderFlags": "REDUCE_ONLY",
    }


def _current_position_reference_price(raw_position: Mapping[str, Any], current_size: Decimal) -> Optional[Decimal]:
    if current_size <= 0:
        return None
    for key in ("notionalValue", "entryNotional"):
        value = _decimal_or_none(raw_position.get(key))
        if value is not None and value > 0:
            return value / current_size
    return None


def _find_hibachi_position_context(account: str, requested_symbol: str, *, operation: str) -> Tuple[Optional[Dict[str, Any]], Optional[CanonicalResponse]]:
    credentials = _lookup_credentials(account)
    if credentials is None:
        return None, make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="ACCOUNT_NOT_FOUND",
            message=(
                f"Hibachi account '{account}' is not configured. "
                "Set HIBACHI_<alias>_ACCOUNTID, HIBACHI_<alias>_APIKEY, "
                "and HIBACHI_<alias>_PRIVATEKEY."
            ),
        )
    token = _set_current_credentials(credentials)
    try:
        raw_account = _fetch_account_info(credentials)
        raw_orders = _fetch_open_orders(credentials)
        market_payload = _MarketCache.get()
    except Exception as exc:  # noqa: BLE001
        _set_current_credentials(token)
        return None, make_failure(
            operation=operation,
            exchange=name,
            account=credentials["account"],
            code="POSITION_CONTEXT_UNAVAILABLE",
            message=_redact(sanitize_error_message(str(exc))),
        )
    _set_current_credentials(token)
    try:
        descriptor = _resolve_canonical_instrument(requested_symbol, payload=market_payload if isinstance(market_payload, Mapping) else None)
    except ValueError as exc:
        if str(exc) == "INSTRUMENT_AMBIGUOUS":
            return None, make_failure(
                operation=operation,
                exchange=name,
                account=credentials["account"],
                code="INSTRUMENT_AMBIGUOUS",
                message=f"Hibachi instrument '{requested_symbol}' is ambiguous.",
            )
        raise
    if descriptor is None:
        return None, make_failure(
            operation=operation,
            exchange=name,
            account=credentials["account"],
            code="INSTRUMENT_NOT_FOUND",
            message=f"Unknown Hibachi instrument '{requested_symbol}'.",
        )
    canonical_symbol = _canonical_symbol_from_request(requested_symbol)
    raw_position = None
    raw_positions_value = raw_account.get("positions")
    raw_positions: List[Any] = raw_positions_value if isinstance(raw_positions_value, list) else []
    for entry in raw_positions:
        if not isinstance(entry, Mapping):
            continue
        if _canonical_symbol_from_request(entry.get("symbol")) != canonical_symbol:
            continue
        quantity = _decimal_or_none(entry.get("quantity"))
        if quantity is None or quantity == 0:
            continue
        raw_position = dict(entry)
        break
    if raw_position is None:
        return None, make_failure(
            operation=operation,
            exchange=name,
            account=credentials["account"],
            code="POSITION_NOT_FOUND",
            message="Open position not found.",
        )
    quantity = _decimal_or_none(raw_position.get("quantity")) or Decimal("0")
    current_side = _normalize_hibachi_side(raw_position.get("direction"), mapping=_HIBACHI_SIDE_FROM_POSITION) or ("long" if quantity > 0 else "short")
    current_size = abs(quantity)
    current_position = _augment_positions_with_protection(
        _normalize_positions_from_account_info({"positions": [raw_position]}),
        raw_orders,
    )
    return {
        "credentials": credentials,
        "descriptor": descriptor,
        "market_payload": market_payload if isinstance(market_payload, Mapping) else {},
        "raw_account": raw_account,
        "raw_orders": raw_orders,
        "raw_position": raw_position,
        "current_position": current_position[0] if current_position else None,
        "current_side": current_side,
        "current_size": current_size,
        "reference_price": _current_position_reference_price(raw_position, current_size),
        "protection": _classify_hibachi_protection_orders(raw_orders, symbol=canonical_symbol, current_side=current_side),
        "canonical_symbol": canonical_symbol,
    }, None


def _verify_hibachi_position_trigger_state(
    credentials: Dict[str, Any],
    *,
    symbol: str,
    current_side: str,
    operation: str,
    expected_price: Optional[Decimal],
) -> Tuple[bool, Optional[int]]:
    protection = _classify_hibachi_protection_orders(
        _fetch_open_orders(credentials),
        symbol=symbol,
        current_side=current_side,
    )
    target_key = "tp" if operation == "set_tp" else "sl"
    target_orders = protection[target_key]
    if expected_price is None:
        return (len(target_orders) == 0), None
    expected_text = _decimal_text(expected_price)
    matched = [order for order in target_orders if _decimal_text(order.get("triggerPrice")) == expected_text]
    if len(matched) != 1:
        return False, None
    return True, _parse_optional_int(matched[0].get("orderId"))


def _set_position_trigger(account: str, request: Dict[str, Any], *, operation: str) -> CanonicalResponse:
    requested_symbol = str(request.get("symbol") or "").strip()
    requested_price = _decimal_or_none(request.get("price"))
    if not requested_symbol:
        return make_failure(operation=operation, exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_price is None or requested_price < 0:
        return make_failure(operation=operation, exchange=name, account=account, code=("INVALID_TP_PRICE" if operation == "set_tp" else "INVALID_SL_PRICE"), message=("TP price must be numeric and non-negative." if operation == "set_tp" else "SL price must be numeric and non-negative."))
    context, failure = _find_hibachi_position_context(account, requested_symbol, operation=operation)
    if failure is not None:
        return failure
    assert context is not None
    credentials = context["credentials"]
    descriptor = context["descriptor"]
    market_payload = context["market_payload"]
    current_side = str(context["current_side"] or "")
    current_size = context["current_size"]
    reference_price = context["reference_price"]
    canonical_symbol = context["canonical_symbol"]
    protection = context["protection"]
    current_position = context.get("current_position")
    target_key = "tp" if operation == "set_tp" else "sl"
    existing_orders = list(protection[target_key])
    if len(existing_orders) > 1:
        return make_failure(operation=operation, exchange=name, account=credentials["account"], code="AMBIGUOUS_PROTECTION_STATE", message="Multiple matching TP/SL orders were found.")
    if reference_price is not None and requested_price > 0:
        if operation == "set_tp":
            if current_side == "long" and requested_price <= reference_price:
                return make_failure(operation=operation, exchange=name, account=credentials["account"], code="INVALID_TP_PRICE", message="TP price must be above the current reference price.")
            if current_side == "short" and requested_price >= reference_price:
                return make_failure(operation=operation, exchange=name, account=credentials["account"], code="INVALID_TP_PRICE", message="TP price must be below the current reference price.")
        else:
            if current_side == "long" and requested_price >= reference_price:
                return make_failure(operation=operation, exchange=name, account=credentials["account"], code="INVALID_SL_PRICE", message="SL price must be below the current reference price.")
            if current_side == "short" and requested_price <= reference_price:
                return make_failure(operation=operation, exchange=name, account=credentials["account"], code="INVALID_SL_PRICE", message="SL price must be above the current reference price.")
    existing_order = existing_orders[0] if existing_orders else None
    current_size_text = getattr(current_position, "size", _decimal_text(current_size)) if current_position is not None else _decimal_text(current_size)
    if requested_price == 0:
        if existing_order is None:
            return make_success(
                operation=operation,
                exchange=name,
                account=credentials["account"],
                position_action=_position_action_result(
                    operation=operation,
                    symbol=canonical_symbol,
                    verified=True,
                    removed=False,
                    current_side=current_side,
                    current_size=current_size_text,
                    message=("No Take Profit was set." if operation == "set_tp" else "No Stop Loss was set."),
                ),
            )
        try:
            _cancel_hibachi_order(credentials, order_id=int(existing_order.get("orderId")))
            verified, _verified_oid = _verify_hibachi_position_trigger_state(credentials, symbol=canonical_symbol, current_side=current_side, operation=operation, expected_price=None)
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation=operation,
                exchange=name,
                account=credentials["account"],
                code=("TP_REMOVAL_FAILED" if operation == "set_tp" else "SL_REMOVAL_FAILED"),
                message=_redact(sanitize_error_message(str(exc))),
                position_action=_position_action_result(
                    operation=operation,
                    symbol=canonical_symbol,
                    verified=False,
                    removed=True,
                    current_side=current_side,
                    current_size=current_size_text,
                    status="failed",
                ),
            )
        action = _position_action_result(
            operation=operation,
            symbol=canonical_symbol,
            verified=verified,
            removed=True,
            current_side=current_side,
            current_size=current_size_text,
            status=("success" if verified else "failed"),
            message=("Take Profit removed." if operation == "set_tp" else "Stop Loss removed."),
        )
        if verified:
            return make_success(operation=operation, exchange=name, account=credentials["account"], position_action=action)
        return make_failure(operation=operation, exchange=name, account=credentials["account"], code="VERIFICATION_FAILED", message="TP/SL removal could not be verified.", position_action=action)

    trigger_price = requested_price
    tick_size = _decimal_or_none(descriptor.get("tick_size")) or Decimal("0")
    step_size = _decimal_or_none(descriptor.get("step_size")) or Decimal("0")
    submitted_price = _quantize_down_to_increment(trigger_price, tick_size)
    submitted_volume = _quantize_down_to_increment(current_size, step_size)
    if submitted_volume <= 0:
        return make_failure(operation=operation, exchange=name, account=credentials["account"], code="INVALID_VOLUME", message="Current position size is not tradable at the market step size.")
    try:
        if existing_order is not None:
            _cancel_hibachi_order(credentials, order_id=int(existing_order.get("orderId")))
        payload = _build_hibachi_trigger_order_payload(
            credentials=credentials,
            descriptor=descriptor,
            current_side=current_side,
            current_size=submitted_volume,
            trigger_price=submitted_price,
            operation=operation,
            market_payload=market_payload,
        )
        response_payload = _submit_single_order(credentials, payload)
        submitted_oid = _parse_optional_int(response_payload.get("orderId")) if isinstance(response_payload, Mapping) else None
        verified, verified_oid = _verify_hibachi_position_trigger_state(
            credentials,
            symbol=canonical_symbol,
            current_side=current_side,
            operation=operation,
            expected_price=submitted_price,
        )
        final_oid = submitted_oid or verified_oid
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation=operation,
            exchange=name,
            account=credentials["account"],
            code="ORDER_SUBMISSION_FAILED",
            message=_redact(sanitize_error_message(str(exc))),
            position_action=_position_action_result(
                operation=operation,
                symbol=canonical_symbol,
                verified=False,
                price=_decimal_text(submitted_price),
                removed=False,
                current_side=current_side,
                current_size=current_size_text,
                status="failed",
            ),
        )
    action = _position_action_result(
        operation=operation,
        symbol=canonical_symbol,
        verified=verified,
        price=_decimal_text(submitted_price),
        removed=False,
        status=("success" if verified else "failed"),
        exchange_order_id=final_oid,
        current_side=current_side,
        current_size=current_size_text,
        message=("Take Profit updated." if operation == "set_tp" else "Stop Loss updated."),
    )
    if verified:
        return make_success(operation=operation, exchange=name, account=credentials["account"], position_action=action)
    return make_failure(operation=operation, exchange=name, account=credentials["account"], code="VERIFICATION_FAILED", message="TP/SL submission could not be verified.", position_action=action)


def _normalize_positions_from_account_info(
    raw_account: Mapping[str, Any],
) -> List[CanonicalPosition]:
    """Translate ``/trade/account/info``'s ``positions[]`` field into
    the canonical ``CanonicalPosition`` list.

    Hibachi's example payload carries ``direction`` (Long/Short),
    ``quantity`` (absolute positive string), ``entryNotional`` (=
    entry_price × quantity), and ``unrealizedTradingPnl`` /
    ``unrealizedFundingPnl`` (we sum them for the total unrealized
    PnL). The contract ``size`` field is always positive — the
    ``side`` carries the direction.

    The ``tp``/``sl``/``tp_count``/``sl_count`` fields are left
    ``None`` because Hibachi does not currently expose a separate
    trigger-order surface in the read endpoints we have
    documented — the future TP/SL agent will source them from
    whichever order endpoint surfaces them. Until then, the
    canonical contract is preserved with the protection fields
    unset, matching how the wizard renders an unprotected
    position.
    """
    raw_positions = raw_account.get("positions")
    if not isinstance(raw_positions, list):
        return []
    positions: List[CanonicalPosition] = []
    for entry in raw_positions:
        if not isinstance(entry, dict):
            continue
        quantity = _decimal_or_none(entry.get("quantity"))
        if quantity is None or quantity == 0:
            continue
        side = _normalize_hibachi_side(
            entry.get("direction"), mapping=_HIBACHI_SIDE_FROM_POSITION
        )
        if side is None:
            # Last-resort: derive side from the sign of quantity. Hibachi
            # payloads have not been observed to omit ``direction``, but
            # we keep this fallback so a future payload shape change
            # degrades gracefully rather than crashing the screen.
            side = "long" if quantity > 0 else "short"
        symbol = _canonical_symbol_from_request(entry.get("symbol"))
        if not symbol:
            continue
        # Hibachi reports ``entryNotional`` (entry price × quantity) but
        # not the entry price directly. Recover the entry price so the
        # canonical ``entry_price`` field is meaningful.
        entry_notional = _decimal_or_none(entry.get("entryNotional"))
        if entry_notional is not None and quantity != 0:
            entry_price = entry_notional / quantity
        else:
            entry_price = Decimal("0")
        pnl = _decimal_or_none(entry.get("unrealizedTradingPnl") or 0) or Decimal("0")
        funding = _decimal_or_none(entry.get("unrealizedFundingPnl") or 0) or Decimal("0")
        total_pnl = pnl + funding
        size_precision = _decimal_places_from_text(entry.get("quantity")) or 0
        positions.append(CanonicalPosition(
            symbol=symbol,
            side=side,
            size=_format_decimal_places_trimmed(abs(quantity), size_precision),
            entry_price=_format_hibachi_display_price(entry_price, symbol=symbol),
            pnl=_decimal_text(total_pnl),
            tp=None,
            sl=None,
            tp_count=None,
            sl_count=None,
        ))
    positions.sort(key=lambda item: (item.symbol, item.side))
    return positions


def _group_open_orders(
    raw_orders: List[Dict[str, Any]],
) -> Tuple[int, List[CanonicalOrderGroup]]:
    """Bucket ``/trade/orders`` rows into the canonical
    ``(symbol, side)`` order-group shape.

    Each Hibachi order object carries ``side: "BID" / "ASK"``,
    ``price`` and ``totalQuantity`` (the originally submitted size
    — not the remaining size; the spec defines ``totalQuantity`` as
    the value the user submitted and ``availableQuantity`` as the
    remainder, so we honour the documented field). The
    ``open_order_count`` is the raw row count, regardless of how
    many buckets the rows collapse into.

    The bucket key is the **canonical** symbol so the wizard can
    line up the group with the corresponding position row in the
    same view.
    """
    if not raw_orders:
        return 0, []
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for entry in raw_orders:
        if not isinstance(entry, dict):
            continue
        side = _normalize_hibachi_side(
            entry.get("side"), mapping=_HIBACHI_SIDE_FROM_ORDER
        )
        if side is None or side not in _HIBACHI_ORDER_SIDE_NORMALIZED:
            continue
        symbol = _canonical_symbol_from_request(entry.get("symbol"))
        if not symbol:
            continue
        price = _decimal_or_none(entry.get("price"))
        size = _decimal_or_none(entry.get("totalQuantity"))
        if price is None or size is None or size <= 0:
            continue
        key = (symbol, side)
        bucket = buckets.setdefault(key, {
            "symbol": symbol,
            "side": side,
            "count": 0,
            "total_size": Decimal("0"),
            "weighted_price": Decimal("0"),
            "min_price": price,
            "max_price": price,
        })
        bucket["count"] += 1
        bucket["total_size"] += size
        bucket["weighted_price"] += price * size
        if price < bucket["min_price"]:
            bucket["min_price"] = price
        if price > bucket["max_price"]:
            bucket["max_price"] = price

    groups: List[CanonicalOrderGroup] = []
    for (symbol, side), bucket in sorted(buckets.items()):
        total_size = bucket["total_size"]
        if total_size > 0:
            vwap = bucket["weighted_price"] / total_size
        else:
            vwap = bucket["min_price"]
        size_precision = _decimal_places_from_text(bucket["total_size"]) or 0
        groups.append(CanonicalOrderGroup(
            symbol=symbol,
            side=side,
            order_count=int(bucket["count"]),
            total_size=_format_decimal_places_trimmed(total_size, size_precision),
            vwap=_format_hibachi_display_price(vwap, symbol=symbol, fallback_places=_decimal_places_from_text(bucket["min_price"])),
            min_price=_format_hibachi_display_price(bucket["min_price"], symbol=symbol, fallback_places=_decimal_places_from_text(bucket["min_price"])),
            max_price=_format_hibachi_display_price(bucket["max_price"], symbol=symbol, fallback_places=_decimal_places_from_text(bucket["max_price"])),
        ))
    return len(raw_orders), groups


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    """Parse a numeric value to ``Decimal`` or return ``None`` on failure.

    Mirrors the same defensive helper used by the other KAM agents
    (Hyperliquid's ``_decimal_or_none`` is functionally identical).
    Coerces ``None`` and empty strings to ``None`` rather than
    raising so callers can keep their normalization branches flat.
    """
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def _decimal_text(value: Any) -> str:
    """Render a monetary value as a trimmed decimal string with no
    scientific notation. Returns ``"0"`` for missing / unparseable
    input so the wizard never has to guard against blank values.
    """
    decimal_value = _decimal_or_none(value)
    if decimal_value is None:
        return "0"
    rendered = format(decimal_value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".") or "0"
    if rendered in {"-0", "-0.0"}:
        return "0"
    return rendered


def _decimal_places_from_text(value: Any) -> Optional[int]:
    """Return the number of decimal places implied by a numeric string.

    ``"0.0001"`` -> 4, ``"1"`` -> 0, ``""`` / unparseable -> ``None``.
    Used to size the precision on the canonical position / order
    field so Hibachi's 10-dp ``underlyingDecimals`` does not bleed
    into the wizard's display.
    """
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


def _hibachi_price_display_places(symbol: str) -> Optional[int]:
    try:
        descriptor = _resolve_canonical_instrument(symbol)
    except ValueError:
        return None
    if not isinstance(descriptor, Mapping):
        return None
    return _decimal_places_from_text(descriptor.get("tick_size"))


def _format_decimal_places_trimmed(value: Decimal, places: int) -> str:
    return _decimal_text(_format_decimal_places(value, places))


def _format_hibachi_display_price(
    value: Any,
    *,
    symbol: str,
    fallback_places: Optional[int] = None,
) -> str:
    decimal_value = _decimal_or_none(value)
    if decimal_value is None:
        return "0"
    places = _hibachi_price_display_places(symbol)
    if places is None:
        places = fallback_places
    if places is None:
        return _decimal_text(decimal_value)
    return _format_decimal_places_trimmed(decimal_value, places)


def _format_decimal_places(value: Decimal, places: int) -> str:
    """Quantize a ``Decimal`` to ``places`` decimal places using
    banker-friendly half-up rounding and return it as a string."""
    quantum = Decimal("1").scaleb(-max(0, int(places)))
    quantized = value.quantize(quantum, rounding=ROUND_HALF_UP)
    return format(quantized, "f")


# ---------------------------------------------------------------------------
# Read: resolve_instrument (scaffold for Phase 2)
# ---------------------------------------------------------------------------


def _resolve_instrument(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Resolve a canonical symbol to the Hibachi market descriptor.

    Phase 1 implements this so that future write paths (new_order,
    ladder, cancel, etc.) can use the same code path without
    duplicating the market-index build. The wizard does not surface
    this directly today, but the canonical contract keeps the
    response shape identical to every other agent's
    ``resolve_instrument`` so a future wizard menu item will work
    without any per-agent wiring.

    Authentication is intentionally not enforced here — the market
    metadata endpoint is public. We still require an ``account`` so
    the dispatch path is uniform.
    """
    requested = str(request.get("symbol") or "").strip()
    if not requested:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    try:
        _MarketCache.get()
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="INSTRUMENT_UNAVAILABLE",
            message=_redact(sanitize_error_message(str(exc))),
        )
    try:
        descriptor = _resolve_canonical_instrument(requested)
    except ValueError as exc:
        if str(exc) == "INSTRUMENT_AMBIGUOUS":
            return make_failure(
                operation="resolve_instrument",
                exchange=name,
                account=account,
                code="INSTRUMENT_AMBIGUOUS",
                message=f"Hibachi instrument '{requested}' is ambiguous.",
            )
        raise
    if descriptor is None:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="INSTRUMENT_NOT_FOUND",
            message=f"Unknown Hibachi instrument '{requested}'.",
        )
    canonical = _canonical_symbol_from_request(requested)
    instrument = CanonicalInstrument(
        requested_symbol=requested,
        symbol=canonical,
        display_name=str(descriptor.get("display_name") or canonical).strip() or canonical,
        price_increment=str(descriptor.get("tick_size") or "") or None,
        size_increment=str(descriptor.get("step_size") or "") or None,
        minimum_size=str(descriptor.get("min_order_size") or "") or None,
    )
    return make_success(
        operation="resolve_instrument",
        exchange=name,
        account=account,
        instrument=instrument,
    )
