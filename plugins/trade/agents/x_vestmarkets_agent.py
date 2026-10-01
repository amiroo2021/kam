"""Vest Markets exchange agent for the KAM /trade stack.

This module owns Vest-Markets-specific behavior for the /trade wizard.

Current scope (Phase 2 — read-only):

- Credential discovery from ``VEST_<ALIAS>_PUBLIC_KEY``,
  ``VEST_<ALIAS>_API_KEY``, ``VEST_<ALIAS>_SIGN_PRIVATE_KEY``, and
  ``VEST_<ALIAS>_ACCOUNT_GROUP`` (live ``os.environ`` wins, then
  ``$HERMES_HOME/.env``), matching the convention used by every other
  KAM exchange agent. An account is "complete" (and therefore surfaced
  to the wizard) iff ALL FOUR fields are present and non-empty.

- Read-only retrieval through the documented header-auth scheme
  (``X-API-KEY`` + ``xrestservermm: restserver{account_group}``).
  No per-request signature is required for ``GET /account`` /
  ``GET /orders``.

- ``balance``  — ``GET /v2/account`` → ``CanonicalBalance`` +
  ``CanonicalPortfolioSummary`` + an ``assets`` list under
  ``data["assets"]`` for the wizard's Assets sub-list.

- ``positions_orders`` — ``GET /v2/account`` (positions) +
  ``GET /v2/orders`` (all statuses, filtered client-side to
  ``NEW``/``PARTIALLY_FILLED`` because the venue rejects
  comma-separated multi-status filters with code 1130) normalised into
  ``CanonicalPosition`` + ``CanonicalOrderGroup`` rows.

- ``positions_management`` — read alias of ``positions_orders``
  (positions only, no order-groups panel) so the wizard's Positions
  Management screen works the same way as Positions & Orders.

- Identity gate on every read: refuse to render if ``/account``
  returns a primary address other than the configured
  ``VEST_<ALIAS>_PUBLIC_KEY`` (a misrouted account-group query).

- Optional override of the production base URL via
  ``VEST_API_BASE`` (e.g. for the documented dev endpoint
  ``https://server-dev.hz.vestmarkets.com/v2``).

Vest's authentication scheme (per the public Vest API docs):

    - ``X-API-KEY`` header carries the per-account ``apiKey`` returned
      by ``POST /register``.
    - ``xrestservermm: restserver{account_group}`` header routes the
      request to the right account-group shard (the user's env stores
      ``VEST_FIBO_ACCOUNT_GROUP=0`` for the Fibo account).

This agent deliberately does NOT implement ``POST /register`` (the
operator already has a provisioned ``apiKey``), does NOT implement any
write operations (``POST /orders``, ``POST /orders/cancel``,
``POST /account/leverage``), and does NOT subscribe to the private WS
endpoint. Those are future work pending explicit user authorisation.

Vest settles in USDC (per the API reference's "All ambiguous monetary
values like margin requirements are ubiquitously USDC"). The canonical
balance unit is therefore USDC, matching the wizard's display contract.

TradeDesk and the Telegram wizard MUST remain exchange-agnostic and
MUST NOT parse ``VEST_*`` environment variables or Vest-native
payloads. All Vest-specific behavior — env-var discovery, header
construction, response parsing, decimal/string normalization — lives
in this module.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from eth_abi.abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

from ..canonical import (
    CanonicalBalance,
    CanonicalCancelGroupResult,
    CanonicalError,
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
    normalize_balance,
    sanitize_error_message,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module identity — required by TradeDesk.
# ---------------------------------------------------------------------------

name = "vestmarkets"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Documented production base URL.
# https://vest-labs.gitbook.io/vest-markets-old/vest-api
DEFAULT_API_BASE = "https://server-prod.hz.vestmarkets.com/v2"

# Documented dev base URL, surfaced for completeness; only used when the
# operator overrides ``VEST_API_BASE``.
# https://server-dev.hz.vestmarkets.com/v2

API_TIMEOUT_SECONDS = 20

# Vest's only numéraire — every monetary field on /account is USDC.
SETTLEMENT_UNIT = "USDC"

# Documented credential suffixes for a fully configured Vest account.
# All four are required — a missing group means the request cannot be
# routed to the right shard; a missing private key means the user has
# not yet provisioned an on-chain signing identity.
VEST_REQUIRED_SUFFIXES: Tuple[str, ...] = (
    "PUBLIC_KEY",
    "API_KEY",
    "SIGN_PRIVATE_KEY",
    "ACCOUNT_GROUP",
)

# Account-alias pattern: starts with a letter, then ASCII letters /
# digits / underscores. Mirrors every other KAM agent.
_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")

# EVM address shape used for VEST_<ALIAS>_PUBLIC_KEY.
# A Vest primary address is a 0x + 40 hex chars (20-byte EOA).
_EVM_ADDRESS_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")

# 32-byte (64 hex chars) raw private-key shape, either with or without
# the "0x" prefix (some operators store it without the prefix).
_HEX32_PATTERN = re.compile(r"^(0x)?[a-fA-F0-9]{64}$")

# Documented, public endpoints (per Vest API docs).
_PATH_ACCOUNT = "/account"

# Documented private endpoints (per Vest API docs). Header-authenticated
# like ``/account``; no per-request signature on reads.
_PATH_ORDERS = "/orders"
_PATH_ORDERS_CANCEL = "/orders/cancel"
_PATH_ACCOUNT_NONCE = "/account/nonce"

# Per the API docs the canonical order types are:
#   MARKET, LIMIT, STOP_LOSS, TAKE_PROFIT, LIQUIDATION (response only)
# We restrict write support to LIMIT + MARKET in this phase; TP/SL live
# on the parent LIMIT order's ``tpPrice`` / ``slPrice`` fields (each
# requiring its own signature sub-proof per the docs). Vest does not
# surface a ``clientOrderId`` field on its orders — the documented
# correlation field is the server-returned ``"id"`` plus the local
# (time, nonce) pair used at submission time.
_SUPPORTED_ORDER_TYPES = ("LIMIT", "MARKET")

# Documented order enums for read-side filtering (the venue rejects
# comma-separated multi-status filters with code 1130, so we filter
# client-side):
#   NEW, FILLED, PARTIALLY_FILLED, CANCELLED, REJECTED.
RESTING_ORDER_STATUSES = ("NEW", "PARTIALLY_FILLED")

# Default ``recvWindow`` (ms) per the docs (server discards if
# ``server_ts > time + recvWindow``).
DEFAULT_RECV_WINDOW_MS = 60_000

# Conservative ladder cap. Vest does not document a per-account or
# per-instrument open-order ceiling so we cap at a small number; the
# wizard's ladder screen surfaces this via
# ``ladder_max_orders_per_instrument``. The batch POST itself is one
# placement per call — we issue them serially with a single nonce walk
# so the server doesn't see a duplicate nonce race.
LADDER_MAX_ORDERS_PER_INSTRUMENT = 20
LADDER_MAX_BATCH = 5

# Order-verification polling.
ORDER_VERIFY_ATTEMPTS = 6
ORDER_VERIFY_DELAY_SECONDS = 0.5

# Header names — used by both the live request path and the redaction
# helper so credentials in error bodies are scrubbed before reaching the
# operator's Telegram chat.
_HEADER_API_KEY = "X-API-KEY"
_HEADER_SERVER = "xrestservermm"

# A browser-like UA is included because some Vest frontends sit behind
# bot-detection layers that reject default ``urllib`` UAs. UA-only
# header; it does not change the signed payload (Vest does not sign GET
# /account — only the header set is required for read-only calls).
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
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


def _combined_vest_env() -> Dict[str, str]:
    """Return ``{key: value}`` for all ``VEST_*`` variables, merging
    ``os.environ`` and ``$HERMES_HOME/.env``.

    Live env always wins (``setdefault`` semantics).
    """
    values: Dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith("VEST_"):
            values[key] = (value or "").strip()
    for key, value in _load_dotenv_values(_hermes_home() / ".env").items():
        if key.startswith("VEST_"):
            values.setdefault(key, (value or "").strip())
    return values


def _read_env(name_: str) -> str:
    """Read a single env var, falling back to ``~/.hermes/.env``."""
    live = os.environ.get(name_, "").strip()
    if live:
        return live
    return _load_dotenv_values(_hermes_home() / ".env").get(name_, "").strip()


# ---------------------------------------------------------------------------
# Account discovery
# ---------------------------------------------------------------------------


def _normalize_alias(raw_account: str) -> str:
    """Sanitize a Vest account alias.

    Returns the lowercased form for use as the public alias (matching
    every other KAM exchange agent that surfaces lowercase aliases to
    the wizard).
    """
    alias = raw_account.strip().strip("_")
    if not alias:
        return ""
    return alias.lower() if _ALIAS_PATTERN.match(alias.upper()) else alias.lower()


def _has_complete_credentials(raw_account: str, env: Dict[str, str]) -> bool:
    """True iff every required suffix for ``raw_account`` is present
    and non-empty.

    We do NOT validate the on-wire shape (EVM address regex, hex-key
    regex, integer account-group) here — those checks happen lazily in
    ``_lookup_credentials`` so that a partially-provisioned account
    doesn't show up in the wizard at all. The current rule is: all
    four vars must be non-empty strings.
    """
    for suffix in VEST_REQUIRED_SUFFIXES:
        key = f"VEST_{raw_account}_{suffix}".upper()
        value = (env.get(key, "") or "").strip()
        if not value:
            return False
    return True


def _discover_accounts() -> List[str]:
    """Return the list of configured Vest account aliases.

    An account is "complete" (and therefore surfaced to the wizard)
    iff ``VEST_<ALIAS>_PUBLIC_KEY``, ``VEST_<ALIAS>_API_KEY``,
    ``VEST_<ALIAS>_SIGN_PRIVATE_KEY``, and
    ``VEST_<ALIAS>_ACCOUNT_GROUP`` are all present and non-empty.
    Aliases are returned in sorted, lowercased form.
    """
    env = _combined_vest_env()
    aliases: List[str] = []
    seen: set = set()
    for key in env:
        if not key.startswith("VEST_") or not key.endswith("_PUBLIC_KEY"):
            continue
        raw_account = key[len("VEST_"):-len("_PUBLIC_KEY")]
        if not raw_account:
            continue
        alias = _normalize_alias(raw_account)
        if not alias or alias in seen:
            continue
        if _has_complete_credentials(raw_account, env):
            seen.add(alias)
            aliases.append(alias)
    return sorted(aliases)


def _lookup_credentials(account: str) -> Optional[Dict[str, Any]]:
    """Look up the four Vest credentials for ``account``.

    Returns a dict shaped for the HTTP layer (``public_key``,
    ``api_key``, ``sign_private_key``, ``account_group``,
    ``base_url``) or ``None`` if the account is unknown or incomplete.
    The caller MUST treat ``api_key`` and ``sign_private_key`` as
    sensitive — they must never be logged or echoed in error messages.
    """
    raw = str(account or "").strip()
    if not raw:
        return None
    upper = raw.upper()
    if not _ALIAS_PATTERN.match(upper):
        return None
    env = _combined_vest_env()
    public_key = env.get(f"VEST_{upper}_PUBLIC_KEY", "").strip()
    api_key = env.get(f"VEST_{upper}_API_KEY", "").strip()
    sign_private_key = env.get(f"VEST_{upper}_SIGN_PRIVATE_KEY", "").strip()
    account_group_raw = env.get(f"VEST_{upper}_ACCOUNT_GROUP", "").strip()

    # Validate the on-wire shape per-field so a half-rotated .env doesn't
    # surface an unusable row.
    if not _EVM_ADDRESS_PATTERN.match(public_key):
        return None
    if not api_key:
        return None
    if not _HEX32_PATTERN.match(sign_private_key):
        return None
    if not account_group_raw.isdigit():
        return None
    account_group = int(account_group_raw)

    base_url = (_read_env("VEST_API_BASE") or DEFAULT_API_BASE).rstrip("/")
    return {
        "account": raw.lower(),
        "public_key": public_key,
        "api_key": api_key,
        "sign_private_key": sign_private_key,
        "account_group": account_group,
        "base_url": base_url,
    }


# ---------------------------------------------------------------------------
# Public agent contract (TradeDesk)
# ---------------------------------------------------------------------------


def list_accounts() -> List[str]:
    """Return the configured Vest account aliases (lowercased, sorted)."""
    return _discover_accounts()


def capabilities() -> List[str]:
    """Return the operations this agent supports.

    Phase 3 read-back / build-and-sign surface. The build + sign +
    submit path is wired through, but a separate explicit LIVE
    authorisation message is required before any real order lands
    (skill rule 22: "The LIVE flip and the first live order each
    require a fresh explicit message after the resolved plan is on
    screen").

    Read-only (always safe):
    - ``balance`` — /account USDC value + 4-field portfolio summary
    - ``positions_orders`` — /account positions + /orders resting
    - ``positions_management`` — read alias of ``positions_orders``

    Build + sign + verify-ready writes (sandboxed via test mocks; the
    LIVE flag requires explicit user authorisation):
    - ``new_order`` — LIMIT + MARKET, signed POST /orders, verify via
      GET /orders?id=<id>
    - ``cancel_order`` — single id cancel, verify via GET /orders?id=<id>
    - ``cancel_order_group`` — filter by symbol + side, walk all ids
    - ``ladder`` — manual ladder of up to
      ``LADDER_MAX_ORDERS_PER_INSTRUMENT`` children, each a separate
      signed POST /orders
    - ``get_exact_order`` — read single order by id
    - ``market_constraints`` — read /exchangeInfo for size/price
      decimals

    Phase 4: instrument catalog + mark-price (wizard symbol picker):
    - ``resolve_instrument`` — single match or candidate list
    - ``list_instruments`` — full /exchangeInfo enumeration
    - ``market_price`` — /ticker/latest mark price for one symbol

    TP/SL / close-position live in a later phase (``new_order``
    rejects anything except ``LIMIT`` / ``MARKET`` here; TP/SL orders
    require their own signature sub-proofs per Vest's docs).
    """
    return [
        "balance",
        "positions_orders",
        "positions_management",
        "new_order",
        "cancel_order",
        "cancel_order_group",
        "ladder",
        "get_exact_order",
        "market_constraints",
        "resolve_instrument",
        "list_instruments",
        "market_price",
    ]


def ladder_max_orders_per_instrument() -> Optional[int]:
    """Return the exchange's per-instrument open-order cap.

    Vest does not document a per-account / per-instrument open-order
    ceiling so we surface a conservative
    :data:`LADDER_MAX_ORDERS_PER_INSTRUMENT` value. The wizard renders
    this number on the ladder screen as ``MAX ORDERS PER INSTRUMENT``.
    """
    return LADDER_MAX_ORDERS_PER_INSTRUMENT


def execute(request: Dict[str, Any]) -> CanonicalResponse:
    """Dispatch a canonical request to the Vest Markets agent.

    Phase 2 supports the three read operations advertised in
    :func:`capabilities` (``balance``, ``positions_orders``,
    ``positions_management``). Anything else returns a canonical
    ``NOT_IMPLEMENTED`` error so the wizard surfaces it cleanly
    instead of pretending to handle it.
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
        if operation == "cancel_order":
            return _cancel_order(account, request)
        if operation == "cancel_order_group":
            return _cancel_order_group(account, request)
        if operation == "ladder":
            return _ladder(account, request)
        if operation == "get_exact_order":
            return _get_exact_order(account, request)
        if operation == "market_constraints":
            return _market_constraints(account, request)
        if operation == "resolve_instrument":
            return _resolve_instrument(account, request)
        if operation == "list_instruments":
            return _list_instruments(account, request)
        if operation == "market_price":
            return _market_price(account, request)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="VEST_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )
    return make_failure(
        operation=operation,
        exchange=name,
        account=account,
        code="NOT_IMPLEMENTED",
        message=f"Vest Markets does not implement '{operation}' yet.",
    )


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


class VestHTTPError(RuntimeError):
    """Structured HTTP failure raised by ``_signed_get`` / ``_signed_post``."""

    def __init__(
        self,
        *,
        status: int,
        path: str,
        body: str,
        credentials: Optional[Dict[str, Any]] = None,
    ):
        self.status = status
        self.path = path
        self.body = body
        # Stash the in-flight credentials so the redaction helper can
        # scrub secrets out of the error body AFTER the request
        # ``with`` block has exited. Without this, ``_redact`` runs
        # outside the credentials context and never sees the
        # sign_private_key / api_key to scrub — a real defence-in-depth
        # gap. Stays ``None`` when the exception originates outside a
        # ``_with_credentials`` context (test mocks, etc.).
        self.credentials = credentials
        super().__init__(f"HTTP {status} on {path}: {body[:200]}")


def _attach_query(path_with_query: str, query: Optional[Dict[str, Any]]) -> str:
    """Append ``query`` to ``path_with_query`` and return the full URL path.

    Vest's authenticated GET endpoints (notably ``/account/nonce``) and
    its cancel POST endpoint require a small set of replay-protection
    parameters in the URL query string — the venue's server-side
    validator rejects requests without them with a FastAPI 422:

        {"detail":[{"type":"missing","loc":["query","time"],
                    "msg":"Field required","input":null}]}

    The server treats these query parameters as plain validation /
    replay metadata — they are NOT included in the per-request
    signature for ``POST /orders`` or ``POST /orders/cancel`` (the
    signature covers only the JSON body's ``order`` block, per the
    Vest docs). ``None``-valued entries are skipped; already-present
    query parameters (``foo=``) are preserved.

    Both :func:`_signed_get` and :func:`_signed_post` delegate here so
    the URL-encoding rules live in a single place.
    """
    if not query:
        return path_with_query
    items: List[str] = []
    for key, value in query.items():
        if value is None:
            continue
        items.append(
            f"{urllib.parse.quote(str(key), safe='')}"
            f"={urllib.parse.quote(str(value), safe='')}"
        )
    if not items:
        return path_with_query
    sep = "&" if "?" in path_with_query else "?"
    return path_with_query + sep + "&".join(items)


def _signed_get(
    credentials: Dict[str, Any],
    path_with_query: str,
    *,
    query: Optional[Dict[str, Any]] = None,
) -> Any:
    """GET against the Vest REST API.

    Thin wrapper that delegates to :func:`_signed_request` so the
    HTTP/error-handling logic lives in a single place.

    ``query`` is an optional mapping whose entries are appended to the
    URL via :func:`_attach_query`. Only one Vest authenticated GET
    endpoint requires this today (``GET /v2/account/nonce``, which
    needs ``time=<Unix_ms>`` for server-side replay protection); the
    others (``/account``, ``/orders``, ``/orders?id=…``) accept the
    query but do not enforce it.
    """
    full_path = _attach_query(path_with_query, query)
    return _signed_request(
        credentials, method="GET", path_with_query=full_path, body=""
    )


def _signed_post(
    credentials: Dict[str, Any],
    path: str,
    body: Dict[str, Any],
    *,
    query: Optional[Dict[str, Any]] = None,
) -> Any:
    """POST a JSON body to the Vest REST API with the documented headers.

    The body is serialized once via :func:`json.dumps` with
    ``separators=(",", ":")`` so the wire payload is compact and the
    server-side JSON parser sees exactly what we built. The signing
    key NEVER travels in the request — only the precomputed
    ``signature`` field inside ``body`` is included in the JSON.

    Vest's POST endpoints also require a small set of replay-protection
    parameters in the **query string** (time, nonce). The server
    rejects the request with a ``missing ... query.time`` validation
    error if these aren't on the URL. We mirror them there to keep the
    envelope consistent — the JSON body also carries the same
    ``time`` / ``nonce`` inside the ``order`` block.

    The query is NOT included in the signature digest (the signature
    covers only the JSON ``order`` block per the Vest docs).
    """
    body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    full_path = _attach_query(path, query)
    return _signed_request(
        credentials,
        method="POST",
        path_with_query=full_path,
        body=body_text,
    )


def _signed_request(
    credentials: Dict[str, Any],
    *,
    method: str,
    path_with_query: str,
    body: str,
) -> Any:
    """Issue an HTTP request to Vest and return the parsed JSON.

    Headers (carries the documented per-account auth):

        ``X-API-KEY``       — the per-account ``apiKey``
        ``xrestservermm``   — ``restserver{account_group}`` routing

    ``body`` is the raw JSON string for POST requests, empty string for
    GET. No per-request signature is required for the read-side
    endpoints (``GET /account``, ``GET /orders``, ``GET
    /account/nonce``); the signature lives inside the JSON envelope for
    POST writes (``POST /orders``, ``POST /orders/cancel``).
    """
    base_url = str(credentials["base_url"]).rstrip("/")
    url = f"{base_url}{path_with_query}"
    headers = {
        "Accept": "application/json",
        "User-Agent": BROWSER_USER_AGENT,
        _HEADER_API_KEY: credentials["api_key"],
        _HEADER_SERVER: f"restserver{credentials['account_group']}",
    }
    data: Optional[bytes] = body.encode("utf-8") if body else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, method=method, headers=headers, data=data)
    with _with_credentials(credentials):
        try:
            with urllib.request.urlopen(request, timeout=API_TIMEOUT_SECONDS) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                raw_text = response.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                error_body = ""
            raise VestHTTPError(
                status=int(exc.code),
                path=path_with_query,
                body=error_body or str(exc.reason),
                credentials=credentials,
            ) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            reason_cls = type(reason).__name__ if reason is not None else ""
            raise VestHTTPError(
                status=0,
                path=path_with_query,
                body=f"transport error: {reason_cls}: {reason}",
                credentials=credentials,
            ) from exc
        except (TimeoutError, ConnectionError) as exc:
            raise VestHTTPError(
                status=0,
                path=path_with_query,
                body=f"timeout/connection error: {exc}",
                credentials=credentials,
            ) from exc
    return _parse_response(raw_text, path=path_with_query)


def _parse_response(raw_text: str, *, path: str) -> Any:
    """Parse a Vest response body and return the JSON payload.

    Vest returns either a JSON object — bare (for ``GET /account``) or
    wrapped (``{"code": 0, "msg": "...", "data": ...}``). We accept
    both shapes and return the decoded JSON verbatim; per-operation
    handlers unwrap as needed.
    """
    safe_raw = _redact(raw_text or "")
    stripped = (raw_text or "").strip()
    if not stripped:
        raise VestHTTPError(status=0, path=path, body="<empty body>")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise VestHTTPError(
            status=0,
            path=path,
            body=f"invalid JSON: {exc.msg}",
        ) from exc
    # ``safe_raw`` is computed so the redaction regexes run on the raw
    # text; in the success path it has no observable effect because
    # the credentials don't appear in a normal JSON body.
    del safe_raw


# ---------------------------------------------------------------------------
# Credential context + redaction
# ---------------------------------------------------------------------------

_credential_slot = threading.local()


def _current_credentials() -> Optional[Dict[str, Any]]:
    return getattr(_credential_slot, "value", None)


class _CredentialsContext:
    """Context manager that stashes the active credentials on the
    current thread so the redaction helper can defensively scrub them
    from any error message raised during the in-flight request.
    """

    def __init__(self, creds: Optional[Dict[str, Any]]):
        self._creds = creds
        self._previous: Optional[Dict[str, Any]] = None

    def __enter__(self):
        self._previous = _current_credentials()
        _credential_slot.value = self._creds
        return self

    def __exit__(self, exc_type, exc, tb):
        _credential_slot.value = self._previous
        return False


def _with_credentials(creds: Optional[Dict[str, Any]]):
    return _CredentialsContext(creds)


def _redact(text: Any, *, fallback_credentials: Optional[Dict[str, Any]] = None) -> str:
    """Scrub sensitive substrings from a free-form error message.

    Vest's ``X-API-KEY`` header value is occasionally a stub for a
    verbose server-side error; the canonical contract requires
    secrets never leak, so we scrub it defensively. We also do a
    literal-substring scrub against the in-flight credentials in case
    a verbose stack trace contained the actual secret value.

    ``fallback_credentials`` is used when the redaction helper is
    called outside an active ``_with_credentials`` context (e.g. when
    ``_map_http_error_to_failure`` scrubs a ``VestHTTPError`` raised
    earlier). The exception carries its own credentials stash so the
    caller can pass ``fallback_credentials=exc.credentials``.
    """
    rendered = str(text or "")

    # 1. ``X-API-KEY: <value>`` → ``X-API-KEY: ***``
    rendered = re.sub(
        r"(?i)(x-api-key\s*:\s*)([^\s,;}\"']+)",
        lambda m: f"{m.group(1)}***",
        rendered,
    )

    # 2. ``xrestservermm: restserver<N>`` is non-sensitive (group number
    # is not a secret), but redact the value just in case a future
    # operator pins an apiKey variant into the routing header.
    rendered = re.sub(
        r"(?i)(xrestservermm\s*:\s*)([^\s,;}\"']+)",
        lambda m: f"{m.group(1)}***",
        rendered,
    )

    # 3. ``Authorization: *** <token>`` → ``Authorization: *** ***``
    def _auth_scheme_sub(match: "re.Match[str]") -> str:
        return f"{match.group(1)}{match.group(2)} ***"

    rendered = re.sub(
        r"(?i)(authorization\s*:\s*)([A-Za-z][A-Za-z0-9_-]*)\s+[^\s,;}\"']+",
        _auth_scheme_sub,
        rendered,
    )

    # 4. Defensive literal-substring scrub against live credentials.
    creds = _current_credentials() or fallback_credentials
    if creds:
        for value in (
            creds.get("api_key"),
            creds.get("sign_private_key"),
            creds.get("public_key"),
        ):
            if value and value in rendered:
                rendered = rendered.replace(value, "***")

    return rendered


# ---------------------------------------------------------------------------
# Decimal helpers (mirrored from sibling agents)
# ---------------------------------------------------------------------------


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    """Parse a string-as-decimal value per the Vest docs.

    Vest documents every decimal field as a JSON string (``"1234.56"``).
    Return ``None`` if the value is missing, empty, or unparseable so
    callers can distinguish "missing" from "zero".
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    try:
        return Decimal(text)
    except Exception:  # noqa: BLE001
        return None


def _decimal_text(value: Any) -> str:
    """Render a Decimal as a string, trimming trailing zeros.

    Vest returns strings with the trailing ``.000000`` padding for the
    collateral precision (typically 6 dp). The wizard's display
    contract expects a short, readable form (``"100"`` instead of
    ``"100.000000"``).

    Note: ``format(Decimal("-0.000000"), "f")`` keeps the trailing zeros
    (``"-0.000000"``), so we normalise first to collapse negative-zero
    variants to ``"0"`` regardless of how many trailing zeros the input
    carried.
    """
    decimal_value = _decimal_or_none(value)
    if decimal_value is None:
        return "0"
    # ``format(Decimal, "f")`` preserves trailing zeros AND keeps the
    # minus sign on negative zero (``"-0.000000"`` would render verbatim
    # and only the explicit zero-equality check below collapses it).
    rendered = format(decimal_value, "f")
    decimal_stripped = decimal_value.normalize()
    if decimal_stripped == 0:
        return "0"
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".") or "0"
    return rendered


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------


def _balance(account: str) -> CanonicalResponse:
    """Return the canonical balance view for ``account``.

    Calls ``GET /v2/account`` with the documented per-account headers
    and maps the JSON response into ``CanonicalBalance`` +
    ``CanonicalPortfolioSummary``. The wizard renders the value unit and
    the four summary fields (account value / withdrawable / margin used
    / total position value) under the "💼 Balance" screen, plus an
    Assets sub-list from ``balances[]`` when present.
    """
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Vest Markets account configuration",
        )

    try:
        payload = _signed_get(credentials, _PATH_ACCOUNT)
    except VestHTTPError as exc:
        return _map_http_error_to_failure(exc, operation="balance", account=account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="VEST_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )

    summary = _extract_account_summary(payload)
    identity_failure = _validate_account_identity(credentials, summary)
    if identity_failure is not None:
        # Re-stamp the operation name on the borrowed failure so the
        # caller sees the operation they asked for.
        if identity_failure.error is not None:
            return make_failure(
                operation="balance",
                exchange=name,
                account=account,
                code=identity_failure.error.code,
                message=identity_failure.error.message,
            )
        return identity_failure

    total_account_value = _decimal_or_none(summary.get("totalAccountValue"))
    if total_account_value is None:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="MALFORMED_RESPONSE",
            message=(
                "Vest Markets /account response did not include a "
                "totalAccountValue field."
            )
        )

    balance = normalize_balance(total_account_value, SETTLEMENT_UNIT)
    portfolio = _portfolio_summary_from_account(summary)
    assets = _normalize_assets(summary.get("balances"))
    return make_success(
        operation="balance",
        exchange=name,
        account=account,
        balance=balance,
        portfolio_summary=portfolio,
        data={"assets": assets},
    )


def _validate_account_identity(
    credentials: Dict[str, Any], summary: Dict[str, Any]
) -> Optional[CanonicalResponse]:
    """Return ``None`` when ``summary`` matches the configured primary
    address; otherwise a canonical ``WRONG_ACCOUNT`` failure.

    Used by both ``_balance`` and ``_positions_orders`` so a misrouted
    account-group query never renders either surface.
    """
    address = _optional_text(summary.get("address"))
    if not address:
        return make_failure(
            operation="balance",
            exchange=name,
            account=str(credentials.get("account") or ""),
            code="MALFORMED_RESPONSE",
            message="Vest Markets /account response did not include an address.",
        )
    expected_address = str(credentials.get("public_key") or "").lower()
    if expected_address and address.lower() != expected_address:
        return make_failure(
            operation="balance",
            exchange=name,
            account=str(credentials.get("account") or ""),
            code="WRONG_ACCOUNT",
            message=(
                f"Vest Markets /account returned address '{address}' which does "
                f"not match the configured primary key. Refusing to render."
            )
        )
    return None


def _extract_account_summary(payload: Any) -> Dict[str, Any]:
    """Return the account-object payload, tolerating both the bare and
    wrapped response shapes Vest may emit.

    The documented example response is a bare JSON object. Some
    endpoints wrap payloads in ``{"code": 0, "data": {...}}``; we
    unwrap that variant defensively so a future format change does not
    brick the balance screen.
    """
    if not isinstance(payload, dict):
        raise RuntimeError("Vest Markets /account response was not an object")
    if "address" in payload and "balances" in payload:
        return payload
    # Wrapped variant — ``code`` must be 0 and ``data`` must be a dict.
    if isinstance(payload.get("data"), dict):
        return payload["data"]
    raise RuntimeError(
        "Vest Markets /account response did not match the documented schema."
    )


def _portfolio_summary_from_account(summary: Dict[str, Any]) -> CanonicalPortfolioSummary:
    """Map Vest's /account response into the canonical portfolio summary.

    - ``account_value``        ← ``totalAccountValue``
    - ``withdrawable``        ← ``withdrawable``
    - ``margin_used``         ← ``openOrderMargin`` + ``totalMaintMargin``
      (best read of "operator-side relief that the venue has earmarked
      against open orders and maintenance"; if both are missing we
      fall back to ``0``).
    - ``total_position_value`` ← sum of ``|size * markPrice|`` across
      non-zero positions (the documented numéraire is USDC).
    """
    account_value = _decimal_or_none(summary.get("totalAccountValue")) or Decimal("0")
    withdrawable = _decimal_or_none(summary.get("withdrawable")) or Decimal("0")
    open_order_margin = _decimal_or_none(summary.get("openOrderMargin")) or Decimal("0")
    maint_margin = _decimal_or_none(summary.get("totalMaintMargin")) or Decimal("0")
    margin_used = open_order_margin + maint_margin

    position_value = Decimal("0")
    for position in summary.get("positions") or []:
        if not isinstance(position, dict):
            continue
        size = _decimal_or_none(position.get("size"))
        if size is None or size == 0:
            continue
        # Prefer mark price; fall back to entry price; fall back to 0.
        mark = _decimal_or_none(position.get("markPrice"))
        if mark is None or mark <= 0:
            mark = _decimal_or_none(position.get("indexPrice"))
        if mark is None or mark <= 0:
            mark = _decimal_or_none(position.get("entryPrice")) or Decimal("0")
        position_value += abs(size) * mark

    return CanonicalPortfolioSummary(
        account_value=normalize_balance(account_value, SETTLEMENT_UNIT).value,
        withdrawable=normalize_balance(withdrawable, SETTLEMENT_UNIT).value,
        margin_used=normalize_balance(margin_used, SETTLEMENT_UNIT).value,
        total_position_value=normalize_balance(position_value, SETTLEMENT_UNIT).value,
        unit=SETTLEMENT_UNIT,
    )


def _normalize_assets(balances: Any) -> List[Dict[str, str]]:
    """Return ``[{"asset": "USDC", "total": "1000", "locked": "0"}, ...]``.

    Vest's ``GET /account`` returns a ``balances`` array of
    ``{asset, total, locked}`` rows. We surface them on the wizard's
    Assets sub-list so a 1-token account shows "USDC: 1000" and a
    multi-token account shows every non-zero asset. Zero-quantity rows are
    included too if Vest ever starts surfacing them, so the Assets list never
    disagrees with the venue's own snapshot.
    """
    rows = balances if isinstance(balances, list) else []
    out: List[Dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        asset = str(row.get("asset") or "").strip()
        if not asset:
            continue
        total = _decimal_text(row.get("total"))
        locked = _decimal_text(row.get("locked"))
        out.append({"asset": asset, "total": total, "locked": locked})
    return out


def _symbol_from_vest_market(symbol: str) -> str:
    """Strip Vest's documented perp/equity suffixes from a market string.

    Vest documents two symbol conventions:

    - crypto perpetuals: ``BTC-PERP``
    - equities / indices / forex perpetuals: ``AAPL-USD-PERP``

    The wizard renders the canonical short name (``BTC``, ``AAPL``).
    Unknown shapes pass through verbatim — the wizard falls back to the raw
    market when no recognised suffix matches, so a future non-perp
    instrument doesn't render as ``UNKNOWN``.
    """
    text = str(symbol or "").strip().upper()
    if not text:
        return ""
    for suffix in ("-USD-PERP", "-USDC-PERP", "-PERP"):
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return text


def _normalize_positions(summary: Dict[str, Any]) -> List[CanonicalPosition]:
    """Convert Vest ``positions[]`` rows into canonical positions.

    Vest's documented payload carries ``symbol``, ``isLong``, ``size``
    (decimal string), ``entryPrice``, ``markPrice``, ``indexPrice``,
    ``liqPrice``, ``unrealizedPnl`` (includes funding), ``initMargin``,
    ``maintMargin``, ``initMarginRatio``. ``tp`` / ``sl`` are not part
    of Vest's position payload — the venue attaches protective orders
    to the parent LIMIT order rather than to the position itself.
    """
    rows = summary.get("positions") if isinstance(summary, dict) else None
    rows = rows if isinstance(rows, list) else []
    positions: List[CanonicalPosition] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_symbol = str(row.get("symbol") or "").strip()
        if not raw_symbol:
            continue
        size_raw = _decimal_or_none(row.get("size"))
        if size_raw is None or size_raw == 0:
            # Vest reports closed/flat positions as a zero-size row;
            # skip them so the wizard doesn't render empty cards.
            continue
        symbol = _symbol_from_vest_market(raw_symbol) or raw_symbol.upper()
        is_long = bool(row.get("isLong"))
        side = "long" if is_long else "short"
        # Per docs the size string may carry the sign or not; we always
        # render ``abs(size)`` because the side flag is the direction.
        display_size = abs(size_raw)
        entry = _decimal_or_none(row.get("entryPrice"))
        mark = _decimal_or_none(row.get("markPrice"))
        pnl = _decimal_or_none(row.get("unrealizedPnl"))
        positions.append(
            CanonicalPosition(
                symbol=symbol,
                side=side,
                size=_decimal_text(display_size),
                entry_price=_decimal_text(entry),
                pnl=_decimal_text(pnl),
                tp=None,
                sl=None,
                tp_count=None,
                sl_count=None,
                exchange_instrument=raw_symbol.upper() or None,
                mark=_decimal_text(mark) if mark is not None else None,
            )
        )
    positions.sort(key=lambda item: (item.symbol, item.side))
    return positions


def _extract_orders_payload(payload: Any) -> List[Dict[str, Any]]:
    """Return a list of order rows from a Vest ``GET /orders`` payload.

    The documented shape is a bare array of order objects. We also
    tolerate the ``{"code": 0, "data": [...]}`` wrap so a future
    format change doesn't brick the orders surface.
    """
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [row for row in payload["data"] if isinstance(row, dict)]
    return []


def _normalize_open_orders(
    orders_payload: List[Dict[str, Any]],
) -> Tuple[List[CanonicalOrderGroup], int]:
    """Convert Vest order rows into ``CanonicalOrderGroup`` rows.

    Vest's documented order payload includes ``isBuy``, ``orderType``
    (``LIMIT`` / ``MARKET`` / ``STOP_LOSS`` / ``TAKE_PROFIT`` /
    ``LIQUIDATION``), ``limitPrice``, ``size``, ``reduceOnly``,
    ``tpPrice``, ``slPrice``. We group by ``(symbol, side)`` and sum
    notional, summing across both entry LIMIT orders and TP/SL
    protective orders separately so the wizard's order panel renders
    them as ``(Limit / Side)`` rather than merging TP+SELL into "Sell
    LIMITS" (which would hide the protective intent — skill rule 20:
    "The canonical ``classification`` separates entry ladders from
    protective / trigger orders").

    We also surface the open-order count so the wizard's
    "📋 Open Orders" header reflects only resting orders.
    """
    resting_buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}
    protective_buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}
    open_count = 0
    for row in orders_payload:
        status = str(row.get("status") or "").strip().upper()
        if status not in RESTING_ORDER_STATUSES:
            continue
        raw_symbol = str(row.get("symbol") or "").strip()
        if not raw_symbol:
            continue
        symbol = _symbol_from_vest_market(raw_symbol) or raw_symbol.upper()
        side_raw = str(row.get("isBuy"))
        is_buy = side_raw.strip().lower() in {"true", "1", "yes"}
        side = "buy" if is_buy else "sell"
        size_raw = _decimal_or_none(row.get("size"))
        if size_raw is None or size_raw <= 0:
            continue
        price_raw = _decimal_or_none(row.get("limitPrice"))
        # ``limitPrice`` for MARKET/STOP/TAKE_PROFIT may be null; fall
        # back to ``markPrice`` for display, then to 0.
        if price_raw is None or price_raw <= 0:
            price_raw = _decimal_or_none(row.get("markPrice"))
        if price_raw is None or price_raw <= 0:
            price_raw = Decimal("0")
        order_type = str(row.get("orderType") or "LIMIT").strip().upper()
        classification = _classify_order(order_type)
        bucket_dict = (
            protective_buckets if classification != "entry_limit" else resting_buckets
        )
        key = (symbol, side)
        entry = bucket_dict.setdefault(
            key,
            {
                "symbol": symbol,
                "side": side,
                "order_count": 0,
                "total_size": Decimal("0"),
                "notional": Decimal("0"),
                "min_price": None,
                "max_price": None,
                "classification": classification,
                "display_type": order_type,
                "reduce_only": bool(row.get("reduceOnly")),
                "trigger_price": None,
                "limit_price": None,
                "exchange_instrument": raw_symbol.upper() or None,
            },
        )
        open_count += 1
        entry["order_count"] += 1
        entry["total_size"] += size_raw
        entry["notional"] += size_raw * price_raw
        if entry["min_price"] is None or (price_raw > 0 and price_raw < entry["min_price"]):
            entry["min_price"] = price_raw
        if entry["max_price"] is None or (price_raw > entry["max_price"]):
            entry["max_price"] = price_raw
        # Capture trigger/limit hint if Vest surfaces them; for entry
        # LIMIT rows we keep them ``None`` so the wizard renders an
        # ordinary limit row.
        if classification != "entry_limit":
            tp_raw = _decimal_or_none(row.get("tpPrice"))
            sl_raw = _decimal_or_none(row.get("slPrice"))
            entry["trigger_price"] = _decimal_text(tp_raw) if tp_raw else _decimal_text(sl_raw) if sl_raw else None
            entry["limit_price"] = _decimal_text(price_raw) if price_raw > 0 else None

    groups: List[CanonicalOrderGroup] = []
    for bucket_dict in (resting_buckets, protective_buckets):
        for (symbol, side), data in sorted(bucket_dict.items()):
            total_size = data["total_size"]
            notional = data["notional"]
            vwap = (notional / total_size) if total_size > 0 else Decimal("0")
            groups.append(
                CanonicalOrderGroup(
                    symbol=symbol,
                    side=side,
                    order_count=int(data["order_count"]),
                    total_size=_decimal_text(total_size),
                    vwap=_decimal_text(vwap),
                    min_price=_decimal_text(data["min_price"] or Decimal("0")),
                    max_price=_decimal_text(data["max_price"] or Decimal("0")),
                    classification=str(data["classification"]),
                    display_type=str(data["display_type"]),
                    reduce_only=bool(data["reduce_only"]),
                    trigger_price=data.get("trigger_price"),
                    limit_price=data.get("limit_price"),
                    exchange_instrument=data.get("exchange_instrument") or None,
                )
            )
    return groups, open_count


def _classify_order(order_type: str) -> str:
    """Map a Vest ``orderType`` string to a canonical classification.

    The wizard's open-orders surface renders TP/SL rows separately
    from ordinary LIMIT entry rows so protective triggers never merge
    merely because they share a side. Per skill rule 20 the canonical
    enums are ``entry_limit | take_profit | stop_loss | trigger | other``.
    """
    upper = (order_type or "").strip().upper()
    if upper in {"LIMIT", "MARKET"}:
        return "entry_limit"
    if upper == "TAKE_PROFIT":
        return "take_profit"
    if upper == "STOP_LOSS":
        return "stop_loss"
    if upper == "LIQUIDATION":
        return "trigger"
    return "other"


def _positions_orders(account: str) -> CanonicalResponse:
    """Return positions + open orders for the wizard's
    "📋 Open Orders & 💼 Positions" screen.

    One ``GET /account`` call supplies positions; one ``GET /orders``
    call supplies the raw order list (no ``?status=`` filter — the
    venue rejects comma-separated multi-status filters with code 1130,
    so we filter to ``NEW``/``PARTIALLY_FILLED`` client-side). Both
    are header-authenticated and never require a signature, so the
    round-trip is two HTTP GETs and a tolerant identity check against
    the configured primary address.
    """
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Vest Markets account configuration",
        )

    try:
        account_payload = _signed_get(credentials, _PATH_ACCOUNT)
        orders_payload = _signed_get(credentials, _PATH_ORDERS)
    except VestHTTPError as exc:
        return _map_http_error_to_failure(
            exc, operation="positions_orders", account=account
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=account,
            code="VEST_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )

    summary = _extract_account_summary(account_payload)
    identity_failure = _validate_account_identity(credentials, summary)
    if identity_failure is not None:
        if identity_failure.error is not None:
            return make_failure(
                operation="positions_orders",
                exchange=name,
                account=account,
                code=identity_failure.error.code,
                message=identity_failure.error.message,
            )
        return identity_failure

    positions = _normalize_positions(summary)
    orders_rows = _extract_orders_payload(orders_payload)
    order_groups, open_order_count = _normalize_open_orders(orders_rows)
    return make_success(
        operation="positions_orders",
        exchange=name,
        account=account,
        positions=positions,
        order_groups=order_groups,
        open_order_count=open_order_count,
    )


def _positions_management(account: str) -> CanonicalResponse:
    """Positions-only view for the wizard's "Positions Management" screen.

    Per skill rule 20 the wizard emits
    ``operation: "positions_management"`` (not ``positions_orders``) for
    that screen. Mature agents (Apex/HL/MT) treat it as a read alias of
    the same positions snapshot used by Positions & Orders — a real
    agent operation, not a wizard composite. We dispatch to the same
    ``/account`` fetch but skip the order-groups panel.
    """
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="positions_management",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Vest Markets account configuration",
        )

    try:
        account_payload = _signed_get(credentials, _PATH_ACCOUNT)
    except VestHTTPError as exc:
        return _map_http_error_to_failure(
            exc, operation="positions_management", account=account
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="positions_management",
            exchange=name,
            account=account,
            code="VEST_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )

    summary = _extract_account_summary(account_payload)
    identity_failure = _validate_account_identity(credentials, summary)
    if identity_failure is not None:
        if identity_failure.error is not None:
            return make_failure(
                operation="positions_management",
                exchange=name,
                account=account,
                code=identity_failure.error.code,
                message=identity_failure.error.message,
            )
        return identity_failure

    positions = _normalize_positions(summary)
    return make_success(
        operation="positions_management",
        exchange=name,
        account=account,
        positions=positions,
    )


def _optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text if text else None


# ---------------------------------------------------------------------------
# Write paths (Phase 3 read-back / build-and-sign). The actual LIVE
# order submission is gated behind a separate explicit user authorisation
# phase; the helpers below let us round-trip a sandbox probe (mocked
# HTTP) so the wire payload, signature shape, and read-after-write
# verify are exercised end-to-end before any real funds move.
# ---------------------------------------------------------------------------


# Vest's private-endpoint signing is a plain ``eth_sign``-style personal
# digest over a 32-byte keccak hash of an ABI-encoded tuple (NOT
# EIP-712 typed data). See:
#   https://vest-labs.gitbook.io/vest-markets-old/vest-api#example-signature
_NEW_ORDER_ABI_TYPES = (
    "uint256",  # time
    "uint256",  # nonce
    "string",   # orderType
    "string",   # symbol
    "bool",     # isBuy
    "string",   # size
    "string",   # limitPrice
    "bool",     # reduceOnly
)
_CANCEL_ABI_TYPES = ("uint256", "uint256", "string")  # time, nonce, id


def _keccak_digest(*, abi_types: Tuple[str, ...], values: Tuple[Any, ...]) -> bytes:
    """Compute the 32-byte keccak digest that Vest signs per the docs.

    The encoded tuple follows Vest's documented ABI type layout
    (``uint256, uint256, string, string, bool, string, string, bool``
    for new-order; ``uint256, uint256, string`` for cancel). The
    digest is returned as raw bytes — callers wrap it with
    :func:`_personal_sign` to produce the wire signature.
    """
    encoded = abi_encode(list(abi_types), list(values))
    return Web3.keccak(encoded)


def _personal_sign(*, digest: bytes, private_key: str) -> str:
    """Sign a 32-byte digest with the documented ``encode_defunct`` wrapper.

    Returns the 0x-prefixed hex signature (65 bytes: ``R || S || V``).
    The caller passes ``digest`` exactly as produced by
    :func:`_keccak_digest`.
    """
    signable = encode_defunct(digest)
    signed = Account.sign_message(signable, private_key=private_key)
    return "0x" + signed.signature.hex()


def _normalize_signing_key(value: str) -> str:
    """Ensure ``value`` is a ``0x``-prefixed 32-byte hex string.

    The documented env-var ``VEST_<ALIAS>_SIGN_PRIVATE_KEY`` may be
    stored with or without the ``0x`` prefix (the operator's
    ``.hermes/.env`` shows ``52edbb...9874`` without the prefix). This
    helper accepts both shapes and returns the canonical form.
    """
    text = str(value or "").strip()
    if not text.startswith("0x"):
        text = "0x" + text
    return text


def _now_ms() -> int:
    """Return the current Unix timestamp in milliseconds (int).

    Per Vest's docs the ``time`` field is in milliseconds since epoch.
    """
    return int(time.time() * 1000)


def _fetch_next_nonce(credentials: Dict[str, Any]) -> int:
    """Fetch and return the next usable nonce from ``GET /account/nonce``.

    Per the docs the server returns ``{"lastNonce": N}`` — the next
    nonce the operator should use is ``N + 1``. The endpoint is
    header-authenticated (no signature). A failed fetch raises
    :class:`VestHTTPError` so the caller surfaces it as a structured
    failure.

    Vest's server-side validator requires ``time`` (Unix milliseconds)
    in the URL query string for replay protection — without it the
    venue returns HTTP 422:

        {"detail":[{"type":"missing","loc":["query","time"],
                    "msg":"Field required","input":null}]}

    ``time`` is plain validation / replay metadata — it is NOT part
    of the signature (no GET endpoint has a signature). We use the
    agent's :func:`_now_ms` clock helper so tests can override the
    clock if needed.
    """
    payload = _signed_get(
        credentials,
        _PATH_ACCOUNT_NONCE,
        query={"time": _now_ms()},
    )
    if not isinstance(payload, dict):
        raise VestHTTPError(
            status=0,
            path=_PATH_ACCOUNT_NONCE,
            body="non-dict payload",
        )
    last = payload.get("lastNonce")
    try:
        last_int = int(last)
    except (TypeError, ValueError):
        raise VestHTTPError(
            status=0,
            path=_PATH_ACCOUNT_NONCE,
            body=f"unexpected lastNonce: {last!r}",
        ) from None
    return last_int + 1


def _build_new_order_payload(
    *,
    symbol: str,
    order_type: str,
    is_buy: bool,
    size_text: str,
    limit_price_text: str,
    reduce_only: bool,
    time_ms: int,
    nonce: int,
    recv_window_ms: int,
    signing_key: str,
) -> Dict[str, Any]:
    """Build the JSON envelope for ``POST /orders``.

    Mirrors the doc example verbatim:

        {
            "order": {
                "time": <ms>,
                "nonce": <int>,
                "symbol": "BTC-PERP",
                "isBuy": true,
                "size": "0.1000",
                "orderType": "LIMIT",
                "limitPrice": "30000.00",
                "reduceOnly": false,
                "timeInForce": "GTC"
            },
            "recvWindow": 60000,
            "signature": "0x0"
        }

    The signature is computed over the keccak digest of the ABI-encoded
    order fields (``time, nonce, orderType, symbol, isBuy, size,
    limitPrice, reduceOnly``), then personal_sign-wrapped. Vest's docs
    note ``timeInForce`` is "optional str: only accepted when
    orderType == LIMIT, must be GTC or FOK".
    """
    symbol_clean = str(symbol or "").strip()
    if order_type == "LIMIT":
        body_order: Dict[str, Any] = {
            "time": int(time_ms),
            "nonce": int(nonce),
            "symbol": symbol_clean,
            "isBuy": bool(is_buy),
            "size": str(size_text),
            "orderType": order_type,
            "limitPrice": str(limit_price_text),
            "reduceOnly": bool(reduce_only),
            "timeInForce": "GTC",
        }
    else:
        # MARKET — ``limitPrice`` is omitted from the body (Vest does not
        # accept it on market orders per the docs).
        body_order = {
            "time": int(time_ms),
            "nonce": int(nonce),
            "symbol": symbol_clean,
            "isBuy": bool(is_buy),
            "size": str(size_text),
            "orderType": order_type,
            "reduceOnly": bool(reduce_only),
        }
    digest = _keccak_digest(
        abi_types=_NEW_ORDER_ABI_TYPES,
        values=(
            int(time_ms),
            int(nonce),
            order_type,
            symbol_clean,
            bool(is_buy),
            str(size_text),
            str(limit_price_text),
            bool(reduce_only),
        ),
    )
    signature = _personal_sign(digest=digest, private_key=signing_key)
    return {
        "order": body_order,
        "recvWindow": int(recv_window_ms),
        "signature": signature,
    }


def _build_cancel_payload(
    *,
    order_id: str,
    time_ms: int,
    nonce: int,
    recv_window_ms: int,
    signing_key: str,
) -> Dict[str, Any]:
    """Build the JSON envelope for ``POST /orders/cancel``.

    Mirrors the doc example:

        {
            "order": {
                "time": <ms>,
                "nonce": <int>,
                "id": "0x..."   # server-returned order id
            },
            "recvWindow": 60000,
            "signature": "0x0"
        }
    """
    order_id_clean = str(order_id or "").strip()
    digest = _keccak_digest(
        abi_types=_CANCEL_ABI_TYPES,
        values=(int(time_ms), int(nonce), order_id_clean),
    )
    signature = _personal_sign(digest=digest, private_key=signing_key)
    return {
        "order": {
            "time": int(time_ms),
            "nonce": int(nonce),
            "id": order_id_clean,
        },
        "recvWindow": int(recv_window_ms),
        "signature": signature,
    }


def _parse_order_response(payload: Any) -> Dict[str, Any]:
    """Return the parsed ``POST /orders`` response payload.

    Vest documents ``{"id": "0x..."}`` on success. We also tolerate the
    ``{"code": 0, "msg": "...", "data": {...}}`` wrap on the new-order and
    cancel paths.

    Error envelopes (``{"code": <int>, "msg": "..."}`` — no ``id`` and no
    ``data``) are also preserved verbatim in a normalised form so that the
    audit trail keeps the venue's exact acknowledgement, e.g.
    ``{"code": 3017, "msg": "Order already processed or cancelled"}``
    becomes ``{"venue_code": 3017, "venue_message": "Order already processed
    or cancelled"}``. The caller decides whether ``venue_code`` represents
    success or failure — we never discard the envelope here.
    """
    if isinstance(payload, dict):
        if "id" in payload:
            return payload
        if isinstance(payload.get("data"), dict):
            return payload["data"]
        # Vest error envelope — keep the venue's exact code + message so the
        # audit trail / cancel handler can branch on it (e.g. 3017 ORDER_NOT_FOUND).
        raw_code = payload.get("code")
        if isinstance(raw_code, int):
            raw_msg = payload.get("msg") or ""
            return {
                "venue_code": raw_code,
                "venue_message": str(raw_msg),
            }
    return {}


def _extract_order_id(payload: Dict[str, Any]) -> Optional[str]:
    """Return the server-returned order id (0x-prefixed hex) or ``None``."""
    raw = payload.get("id")
    if raw is None:
        return None
    text = str(raw).strip()
    return text if text else None


def _verify_order_status(
    credentials: Dict[str, Any],
    order_id: str,
    *,
    expected_statuses: Tuple[str, ...],
    attempts: int = ORDER_VERIFY_ATTEMPTS,
    base_delay: float = ORDER_VERIFY_DELAY_SECONDS,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Poll ``GET /orders?id=<id>`` until the order matches one of
    ``expected_statuses`` or the attempt budget is exhausted.

    Returns ``(matched, order_dict_or_None)``. ``matched=True`` means the
    final observed order is one of ``expected_statuses`` AND its id
    matches the requested id. ``matched=False`` with ``order_dict=None``
    means we never saw the order — the verify path is inconclusive, not
    necessarily a venue rejection.
    """
    last_order: Optional[Dict[str, Any]] = None
    for attempt in range(max(1, attempts)):
        try:
            payload = _signed_get(credentials, f"{_PATH_ORDERS}?id={order_id}")
        except VestHTTPError:
            payload = None
        rows = _extract_orders_payload(payload) if payload is not None else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("id") or "").strip().lower() != order_id.strip().lower():
                continue
            last_order = row
            observed = str(row.get("status") or "").strip().upper()
            if observed in expected_statuses:
                return True, row
        if attempt < attempts - 1:
            time.sleep(base_delay)
    return False, last_order


def _extract_open_order_ids(
    credentials: Dict[str, Any],
    *,
    symbol: Optional[str] = None,
    side: Optional[str] = None,
) -> List[Tuple[str, Dict[str, Any]]]:
    """Return ``[(id, row), ...]`` for every resting order, optionally
    filtered by ``symbol`` and ``side``.

    ``side`` is the wizard's canonical ``"long"`` / ``"short"`` —
    mapped back to Vest's ``isBuy`` flag (``"long"`` ⇔ ``isBuy=true``).
    """
    payload = _signed_get(credentials, _PATH_ORDERS)
    rows = _extract_orders_payload(payload)
    out: List[Tuple[str, Dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").strip().upper()
        if status not in RESTING_ORDER_STATUSES:
            continue
        if symbol:
            row_symbol = _symbol_from_vest_market(
                str(row.get("symbol") or "")
            ) or str(row.get("symbol") or "").strip().upper()
            target = _symbol_from_vest_market(symbol) or symbol.strip().upper()
            if row_symbol != target:
                continue
        if side:
            is_buy_raw = row.get("isBuy")
            is_buy = (
                str(is_buy_raw).strip().lower() in {"true", "1", "yes"}
                if not isinstance(is_buy_raw, bool)
                else is_buy_raw
            )
            want_buy = side.strip().lower() == "long"
            if is_buy != want_buy:
                continue
        order_id = str(row.get("id") or "").strip()
        if not order_id:
            continue
        out.append((order_id, row))
    return out


def _submit_new_order(credentials: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """Submit ``POST /orders`` and return the parsed response payload."""
    raw = _signed_post(credentials, _PATH_ORDERS, payload)
    return _parse_order_response(raw)


def _submit_cancel(
    credentials: Dict[str, Any],
    payload: Dict[str, Any],
    *,
    time_ms: int,
) -> Dict[str, Any]:
    """Submit ``POST /orders/cancel`` and return the parsed response.

    The Vest cancel endpoint enforces a server-side replay-protection
    validation that requires ``time`` (Unix milliseconds) in the URL
    query string. Without it the venue responds with HTTP 422:

        {"detail":[{"type":"missing","loc":["query","time"],
                    "msg":"Field required","input":null}]}

    The new-order endpoint (``POST /orders``) does not impose this
    constraint, which is why place-order works but cancel does not. We
    always mirror the JSON body's ``order.time`` field into the query
    string so the URL is consistent with the signed payload. ``nonce``
    is also mirrored for symmetry, even though only ``time`` is
    enforced as required at present.
    """
    raw = _signed_post(
        credentials,
        _PATH_ORDERS_CANCEL,
        payload,
        query={"time": int(time_ms), "nonce": payload.get("order", {}).get("nonce")},
    )
    return _parse_order_response(raw)


def _make_new_order_result(
    *,
    order_id: str,
    submitted: Dict[str, Any],
    verified: Optional[Dict[str, Any]],
    verified_ok: bool,
) -> Dict[str, Any]:
    """Render the canonical OrderResult payload for the wizard.

    ``submitted`` is the JSON body POSTed. ``verified`` is the latest
    GET /orders row matching ``order_id``, or ``None`` if the verify
    budget elapsed. ``_in_progress`` is True when the order is still
    resting (no fill yet) and False when fully filled or rejected.
    """
    safe = {
        "exchange": name,
        "order_id": order_id,
        "submitted": submitted,
        "verified": verified,
        "verified_ok": verified_ok,
        "status": (verified or {}).get("status") if verified else "UNKNOWN",
    }
    return safe


# --- Operation handlers -----------------------------------------------------


def _new_order(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Submit a single ``LIMIT`` or ``MARKET`` order.

    Phase 3 read-back / build-and-sign probe path. The agent:

    1. Validates request fields (symbol, side, type, size, price, reduce-only).
    2. Fetches ``/account/nonce`` to learn the next value.
    3. Builds the canonical order JSON via :func:`_build_new_order_payload`.
    4. Signs the keccak digest with the configured
       ``VEST_<ALIAS>_SIGN_PRIVATE_KEY``.
    5. POSTs ``/orders``.
    7. Polls ``GET /orders?id=<id>`` with backoff to confirm the order
       landed in a resting state.

    Returns the canonical OrderResult. Phase-3 LIVE submission is
    gated behind a separate explicit authorisation message.
    """
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Vest Markets account configuration",
        )

    requested_symbol = str(request.get("symbol") or "").strip().upper()
    requested_side = str(request.get("side") or "").strip().lower()
    order_type = str(request.get("order_type") or "limit").strip().upper()
    if order_type == "LIMIT":
        order_type = "LIMIT"
    elif order_type == "MARKET":
        order_type = "MARKET"
    if order_type not in _SUPPORTED_ORDER_TYPES:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="INVALID_ORDER_TYPE",
            message=(
                f"Vest Markets supports {', '.join(_SUPPORTED_ORDER_TYPES)} on this path; "
                f"got {order_type!r}."
            ),
        )
    if requested_side not in {"buy", "sell"}:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="INVALID_SIDE",
            message="Side must be 'buy' or 'sell'.",
        )
    if not requested_symbol:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    volume_text = str(request.get("volume") or "").strip()
    volume_decimal = _decimal_or_none(volume_text)
    if volume_decimal is None or volume_decimal <= 0:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="INVALID_VOLUME",
            message="Volume must be a positive decimal string.",
        )
    if order_type == "LIMIT":
        price_text = str(request.get("price") or "").strip()
        price_decimal = _decimal_or_none(price_text)
        if price_decimal is None or price_decimal <= 0:
            return make_failure(
                operation="new_order",
                exchange=name,
                account=account,
                code="INVALID_PRICE",
                message="Limit price must be a positive decimal string.",
            )
    else:
        price_decimal = None
        price_text = "0"
    reduce_only_raw = request.get("reduce_only")
    reduce_only = bool(reduce_only_raw) if reduce_only_raw is not None else False

    try:
        signing_key = _normalize_signing_key(credentials["sign_private_key"])
        next_nonce = _fetch_next_nonce(credentials)
    except VestHTTPError as exc:
        return _map_http_error_to_failure(exc, operation="new_order", account=account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="VEST_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )

    time_ms = _now_ms()
    payload = _build_new_order_payload(
        symbol=requested_symbol,
        order_type=order_type,
        is_buy=(requested_side == "buy"),
        size_text=_decimal_text(volume_decimal),
        limit_price_text=_decimal_text(price_decimal) if price_decimal is not None else "0",
        reduce_only=reduce_only,
        time_ms=time_ms,
        nonce=next_nonce,
        recv_window_ms=DEFAULT_RECV_WINDOW_MS,
        signing_key=signing_key,
    )

    try:
        response_payload = _submit_new_order(credentials, payload)
    except VestHTTPError as exc:
        return _map_http_error_to_failure(exc, operation="new_order", account=account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="VEST_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )

    order_id = _extract_order_id(response_payload)
    if not order_id:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="MALFORMED_RESPONSE",
            message="Vest Markets /orders response did not include an order id.",
            exchange_reason=str(response_payload)[:200] or None,
        )

    verified_ok, verified_row = _verify_order_status(
        credentials, order_id, expected_statuses=RESTING_ORDER_STATUSES
    )
    order_result_payload = _make_new_order_result(
        order_id=order_id,
        submitted=payload,
        verified=verified_row,
        verified_ok=verified_ok,
    )
    return make_success(
        operation="new_order",
        exchange=name,
        account=account,
        order=CanonicalOrderResult(
            symbol=requested_symbol,
            side=requested_side,
            order_type=order_type,
            requested_volume=_decimal_text(volume_decimal),
            requested_price=_decimal_text(price_decimal) if price_decimal is not None else "0",
            submitted_volume=_decimal_text(volume_decimal),
            submitted_price=_decimal_text(price_decimal) if price_decimal is not None else "0",
            verified=verified_ok,
            status=("success" if verified_ok else "unverified"),
            exchange_order_id=order_id,
        ),
        data=order_result_payload,
    )


def _cancel_order(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Cancel a single order by its server-returned ``id``.

    The wizard's cancel-ticket flow calls this directly when the operator
    has the exact id (from ``positions_orders`` or a prior placement).
    """
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="cancel_order",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Vest Markets account configuration",
        )
    order_id = str(request.get("order_id") or "").strip()
    if not order_id:
        return make_failure(
            operation="cancel_order",
            exchange=name,
            account=account,
            code="MISSING_ORDER_ID",
            message="order_id is required.",
        )

    try:
        signing_key = _normalize_signing_key(credentials["sign_private_key"])
        next_nonce = _fetch_next_nonce(credentials)
    except VestHTTPError as exc:
        return _map_http_error_to_failure(exc, operation="cancel_order", account=account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="cancel_order",
            exchange=name,
            account=account,
            code="VEST_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )

    time_ms = _now_ms()
    payload = _build_cancel_payload(
        order_id=order_id,
        time_ms=time_ms,
        nonce=next_nonce,
        recv_window_ms=DEFAULT_RECV_WINDOW_MS,
        signing_key=signing_key,
    )
    try:
        response_payload = _submit_cancel(credentials, payload, time_ms=time_ms)
    except VestHTTPError as exc:
        return _map_http_error_to_failure(exc, operation="cancel_order", account=account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="cancel_order",
            exchange=name,
            account=account,
            code="VEST_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )

    # Verify: poll for CANCELLED status on the id. The verify lookup is
    # READ-ONLY — no second cancel POST is ever issued from this path.
    verified_ok, verified_row = _verify_order_status(
        credentials,
        order_id,
        expected_statuses=("CANCELLED",),
    )

    # Inspect the venue response: if Vest returned an error envelope (e.g.
    # 3017 ORDER_NOT_FOUND = "Order already processed or cancelled"), we
    # branch on the verified status rather than blindly reporting success.
    venue_code = 0
    venue_message = ""
    if isinstance(response_payload, dict):
        raw_code = response_payload.get("venue_code")
        if isinstance(raw_code, int):
            venue_code = raw_code
        raw_msg = response_payload.get("venue_message")
        if isinstance(raw_msg, str):
            venue_message = raw_msg
    verified_status = (
        str((verified_row or {}).get("status") or "").upper()
        if verified_row is not None
        else ""
    )

    audit = {
        "order_id": order_id,
        "submitted": payload,
        "response": response_payload,
        "venue_code": venue_code if venue_code else None,
        "venue_message": venue_message,
        "verified_ok": verified_ok,
        "verified": verified_row,
    }

    if venue_code == 3017:
        # Vest reports ORDER_NOT_FOUND — the order was already in a
        # terminal state at the moment of POST evaluation. The verify
        # poll arbitrates which terminal state it was.
        if verified_status == "CANCELLED":
            return make_success(
                operation="cancel_order",
                exchange=name,
                account=account,
                data={**audit, "status_label": "cancelled"},
            )
        if verified_status == "FILLED":
            return make_failure(
                operation="cancel_order",
                exchange=name,
                account=account,
                code="ALREADY_FILLED",
                message=(
                    f"Vest reports order {order_id} already FILLED; the "
                    "cancellation did not occur and the fill stands."
                ),
                order_state=audit,
            )
        # 3017 envelope arrived but verify still shows NEW/OPEN or the
        # lookup failed — surface as an explicit, well-named ambiguous
        # condition. NO retry POST is issued from this branch.
        return make_failure(
            operation="cancel_order",
            exchange=name,
            account=account,
            code="CANCEL_AMBIGUOUS",
            message=(
                f"Vest returned ORDER_NOT_FOUND (3017) for order "
                f"{order_id} but the verification lookup "
                f"reports status={verified_status!r}; cancel state "
                "is inconclusive."
            ),
            order_state=audit,
        )

    # Conventional success path: venue accepted the cancel; verify
    # confirms CANCELLED (verified_ok=True) or is inconclusive
    # (verified_ok=False). We never auto-retry.
    status_label = "cancelled" if verified_ok else "unverified"
    return make_success(
        operation="cancel_order",
        exchange=name,
        account=account,
        data={**audit, "status_label": status_label},
    )


def _cancel_order_group(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Cancel every resting order, optionally filtered by symbol/side.

    The wizard's "❌ Cancel Orders" screen calls this with the
    instrument and (optionally) side that the operator selected.

    Each child is processed by delegating to the shared single-order
    primitive :func:`_cancel_order`. This means there is ONE canonical
    Vest cancel wire implementation: every child fetches its own fresh
    nonce, sends exactly one signed ``POST /orders/cancel`` with
    ``?time=<ms>&nonce=<n>`` on the URL, runs the read-only verify
    poll, and classifies the 3017 / FILLED / ambiguous outcomes through
    the existing branches.

    Behaviour guarantees (inherited from the single-order primitive):

    * Exactly ONE ``POST /orders/cancel`` per child — no automatic
      retry, no fallback POST.
    * ``query.time`` mirrors ``body.order.time`` for every POST.
    * 3017 ORDER_NOT_FOUND is disambiguated by the verify poll:
      ``CANCELLED`` → child success; ``FILLED`` → child
      ``ALREADY_FILLED``; anything else → child ``CANCEL_AMBIGUOUS``.
    * Group selection is exact — only the order IDs returned by
      :func:`_extract_open_order_ids` for the requested
      ``symbol``/``side`` are touched.
    """
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Vest Markets account configuration",
        )
    symbol_filter = str(request.get("symbol") or "").strip().upper() or None
    side_filter_raw = request.get("side")
    side_filter: Optional[str] = None
    if side_filter_raw is not None:
        side_text = str(side_filter_raw).strip().lower()
        if side_text in {"buy", "sell", "long", "short"}:
            side_filter = "long" if side_text in {"buy", "long"} else "short"
        else:
            return make_failure(
                operation="cancel_order_group",
                exchange=name,
                account=account,
                code="INVALID_SIDE",
                message="side must be one of buy / sell / long / short.",
            )

    try:
        targets = _extract_open_order_ids(
            credentials, symbol=symbol_filter, side=side_filter
        )
    except VestHTTPError as exc:
        return _map_http_error_to_failure(
            exc, operation="cancel_order_group", account=account
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code="VEST_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )

    # Group preflight classification — what we'd report if no children
    # match the requested filter. The wizard relies on the counts to
    # render the confirmation modal; we keep the existing semantics.
    if not targets:
        return make_success(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            cancel_group=CanonicalCancelGroupResult(
                symbol=symbol_filter or "",
                side=side_filter or "",
                targeted_order_count=0,
                cancelled_order_count=0,
                confirmed_absent_count=0,
                remaining_target_count=0,
                verified=True,
                partial=False,
                status="success",
                batch_count=0,
                requested_cancel_count=0,
                verified_cancel_count=0,
            ),
            data={
                "symbol": symbol_filter,
                "side": side_filter,
                "succeeded": [],
                "failed": [],
                "already_filled": [],
                "ambiguous": [],
                "children": [],
            },
        )

    # Iterate over the targeted ids and let the single-order primitive do
    # the work. The shared primitive already handles nonce, query.time,
    # 3017 classification, verify poll, and the "exactly one POST per
    # child" invariant — there is no second independent Vest cancel
    # wire implementation.
    succeeded: List[str] = []
    failed: List[Dict[str, str]] = []
    already_filled: List[str] = []
    ambiguous: List[str] = []
    children: List[Dict[str, Any]] = []
    for order_id, _row in targets:
        child_resp = _cancel_order(
            account,
            {"order_id": order_id, "operation": "cancel_order"},
        )
        # Classify the per-child outcome. The single-order primitive's
        # canonical surface is unchanged — we only re-shape it into a
        # per-group row for the wizard's bulk-summary view.
        entry: Dict[str, Any] = {
            "order_id": order_id,
            "success": bool(child_resp.success),
            "status_label": None,
        }
        if child_resp.data is not None:
            entry["status_label"] = child_resp.data.get("status_label")
            entry["venue_code"] = child_resp.data.get("venue_code")
            entry["venue_message"] = child_resp.data.get("venue_message")
            entry["verified_ok"] = child_resp.data.get("verified_ok")
        if child_resp.order_state is not None:
            entry["order_state"] = child_resp.order_state
        if child_resp.success:
            succeeded.append(order_id)
        else:
            code = child_resp.error.code if child_resp.error else "VEST_ERROR"
            message = (
                child_resp.error.message
                if child_resp.error is not None
                else "Unknown error"
            )
            if code == "ALREADY_FILLED":
                already_filled.append(order_id)
            elif code == "CANCEL_AMBIGUOUS":
                ambiguous.append(order_id)
            else:
                failed.append(
                    {"order_id": order_id, "code": code, "message": message}
                )
            entry["failure_code"] = code
            entry["failure_message"] = message
        children.append(entry)

    return make_success(
        operation="cancel_order_group",
        exchange=name,
        account=account,
        cancel_group=CanonicalCancelGroupResult(
            symbol=symbol_filter or "",
            side=side_filter or "",
            targeted_order_count=len(targets),
            cancelled_order_count=len(succeeded),
            confirmed_absent_count=len(succeeded),
            remaining_target_count=max(0, len(targets) - len(succeeded)),
            verified=(len(succeeded) == len(targets)),
            partial=(len(failed) > 0 or len(ambiguous) > 0 or len(already_filled) > 0),
            status=(
                "success"
                if not (failed or ambiguous or already_filled)
                else "partial"
            ),
            batch_count=len(targets),
            requested_cancel_count=len(targets),
            verified_cancel_count=len(succeeded),
        ),
        data={
            "symbol": symbol_filter,
            "side": side_filter,
            "succeeded": succeeded,
            "already_filled": already_filled,
            "ambiguous": ambiguous,
            "failed": failed,
            "children": children,
        },
    )


def _ladder(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Place a ladder of ``LADDER_MAX_BATCH`` children per parent.

    Vest's docs do not mention batch placement. We emulate the wizard's
    "🪜 Ladder" flow by issuing one signed ``POST /orders`` per rung
    in increasing-price order for BUY ladders (decreasing for SELL),
    with a monotonic nonce walk so no nonce is reused within the
    sweep. Each child is verified individually for ``NEW`` /
    ``PARTIALLY_FILLED`` status via :func:`_verify_order_status`.
    """
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Vest Markets account configuration",
        )

    requested_symbol = str(request.get("symbol") or "").strip().upper()
    requested_side = str(request.get("side") or "").strip().lower()
    if not requested_symbol or requested_side not in {"buy", "sell"}:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="INVALID_REQUEST",
            message="Ladder requires symbol and side (buy/sell).",
        )
    children_raw = request.get("children")
    if not isinstance(children_raw, list) or not children_raw:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="INVALID_CHILDREN",
            message="Ladder requires a non-empty children list.",
        )
    if len(children_raw) > LADDER_MAX_ORDERS_PER_INSTRUMENT:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="TOO_MANY_CHILDREN",
            message=(
                f"Ladder exceeds {LADDER_MAX_ORDERS_PER_INSTRUMENT} children "
                f"(got {len(children_raw)})."
            ),
        )

    children: List[Dict[str, Any]] = []
    for child in children_raw:
        if not isinstance(child, dict):
            continue
        price_text = str(child.get("price") or "").strip()
        size_text = str(child.get("size") or "").strip()
        price_decimal = _decimal_or_none(price_text)
        size_decimal = _decimal_or_none(size_text)
        if (
            price_decimal is None
            or price_decimal <= 0
            or size_decimal is None
            or size_decimal <= 0
        ):
            return make_failure(
                operation="ladder",
                exchange=name,
                account=account,
                code="INVALID_CHILD",
                message="Each ladder child needs positive price and size.",
            )
        children.append(
            {
                "price_text": _decimal_text(price_decimal),
                "size_text": _decimal_text(size_decimal),
            }
        )
    if not children:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="INVALID_CHILDREN",
            message="Ladder children list is empty after validation.",
        )

    try:
        signing_key = _normalize_signing_key(credentials["sign_private_key"])
        start_nonce = _fetch_next_nonce(credentials)
    except VestHTTPError as exc:
        return _map_http_error_to_failure(exc, operation="ladder", account=account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="VEST_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )

    next_nonce = start_nonce
    time_ms = _now_ms()
    succeeded: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []
    for child in children:
        payload = _build_new_order_payload(
            symbol=requested_symbol,
            order_type="LIMIT",
            is_buy=(requested_side == "buy"),
            size_text=child["size_text"],
            limit_price_text=child["price_text"],
            reduce_only=False,
            time_ms=time_ms,
            nonce=next_nonce,
            recv_window_ms=DEFAULT_RECV_WINDOW_MS,
            signing_key=signing_key,
        )
        try:
            response_payload = _submit_new_order(credentials, payload)
        except VestHTTPError as exc:
            failed.append(
                {
                    "price": child["price_text"],
                    "size": child["size_text"],
                    "code": str(exc.status),
                    "body": str(exc.body)[:200],
                }
            )
            next_nonce += 1
            continue
        except Exception as exc:  # noqa: BLE001
            failed.append(
                {
                    "price": child["price_text"],
                    "size": child["size_text"],
                    "code": "VEST_ERROR",
                    "body": _redact(sanitize_error_message(str(exc)))[:200],
                }
            )
            next_nonce += 1
            continue
        order_id = _extract_order_id(response_payload)
        if not order_id:
            failed.append(
                {
                    "price": child["price_text"],
                    "size": child["size_text"],
                    "code": "MALFORMED_RESPONSE",
                    "body": "no id in response",
                }
            )
            next_nonce += 1
            continue
        verified_ok, _ = _verify_order_status(
            credentials, order_id, expected_statuses=RESTING_ORDER_STATUSES
        )
        succeeded.append(
            {
                "price": child["price_text"],
                "size": child["size_text"],
                "order_id": order_id,
                "verified_ok": verified_ok,
            }
        )
        next_nonce += 1
    return make_success(
        operation="ladder",
        exchange=name,
        account=account,
        ladder=CanonicalLadderResult(
            symbol=requested_symbol,
            side=requested_side,
            distribution="manual",
            requested_order_count=len(children),
            submitted_order_count=len(succeeded),
            requested_volume=str(sum(Decimal(child["size_text"]) for child in children)),
            submitted_volume=str(
                sum(
                    Decimal(placement["size"])
                    for placement in succeeded
                    if isinstance(placement.get("size"), str)
                )
            ),
            batch_count=len(children),
            verified=(len(succeeded) == len(children)),
            partial=(len(failed) > 0),
            status=("success" if not failed else "partial"),
            accepted_child_count=len(succeeded),
            omitted_order_count=len(failed),
            child_order_ids=[p["order_id"] for p in succeeded if "order_id" in p],
            batches=[
                {
                    "price": p["price"],
                    "size": p["size"],
                    "order_id": p["order_id"],
                    "verified_ok": p["verified_ok"],
                }
                for p in succeeded
            ],
        ),
    )


def _get_exact_order(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Read a single order by id and return its canonical order_state.

    Used by the wizard to refresh a placement's status after
    submission. The order_state dict is consumed as
    ``response.data["order_state"]`` by the wizard's order-state screen.
    """
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="get_exact_order",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Vest Markets account configuration",
        )
    order_id = str(request.get("order_id") or "").strip()
    if not order_id:
        return make_failure(
            operation="get_exact_order",
            exchange=name,
            account=account,
            code="MISSING_ORDER_ID",
            message="order_id is required.",
        )
    try:
        payload = _signed_get(credentials, f"{_PATH_ORDERS}?id={order_id}")
    except VestHTTPError as exc:
        return _map_http_error_to_failure(
            exc, operation="get_exact_order", account=account
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="get_exact_order",
            exchange=name,
            account=account,
            code="VEST_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )
    rows = _extract_orders_payload(payload)
    match: Optional[Dict[str, Any]] = None
    for row in rows:
        if isinstance(row, dict) and str(row.get("id") or "").strip().lower() == order_id.lower():
            match = row
            break
    if match is None:
        return make_failure(
            operation="get_exact_order",
            exchange=name,
            account=account,
            code="ORDER_NOT_FOUND",
            message=f"Order {order_id} not found on Vest Markets.",
        )
    return make_success(
        operation="get_exact_order",
        exchange=name,
        account=account,
        order_state=match,
        data={"order_state": match, "order_id": order_id},
    )


def _market_constraints(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Return documented market-level constraints (size/price decimals).

    Vest's ``/exchangeInfo`` returns ``sizeDecimals`` and ``priceDecimals``
    per symbol. The wizard's "Other…" picker uses these to validate
    user input before building the new-order envelope.

    We fetch ``/exchangeInfo`` with no auth (public endpoint), filter
    to ``requested_symbol``, and return the matching row.
    """
    requested_symbol = str(request.get("symbol") or "").strip().upper()
    if not requested_symbol:
        return make_failure(
            operation="market_constraints",
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    try:
        payload = _signed_get(_public_credentials(), "/exchangeInfo")
    except VestHTTPError as exc:
        return _map_http_error_to_failure(
            exc, operation="market_constraints", account=account
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="market_constraints",
            exchange=name,
            account=account,
            code="VEST_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )
    rows = payload.get("symbols") if isinstance(payload, dict) else None
    rows = rows if isinstance(rows, list) else []
    match: Optional[Dict[str, Any]] = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_symbol = str(row.get("symbol") or "").strip().upper()
        if row_symbol == requested_symbol:
            match = row
            break
    if match is None:
        return make_failure(
            operation="market_constraints",
            exchange=name,
            account=account,
            code="INSTRUMENT_NOT_FOUND",
            message=f"Vest Markets has no symbol {requested_symbol!r}.",
        )
    size_decimals_raw = match.get("sizeDecimals")
    price_decimals_raw = match.get("priceDecimals")
    size_increment = (
        str(10 ** -int(size_decimals_raw))
        if isinstance(size_decimals_raw, int) and size_decimals_raw >= 0
        else None
    )
    price_increment = (
        str(10 ** -int(price_decimals_raw))
        if isinstance(price_decimals_raw, int) and price_decimals_raw >= 0
        else None
    )
    return make_success(
        operation="market_constraints",
        exchange=name,
        account=account,
        instrument=CanonicalInstrument(
            requested_symbol=requested_symbol,
            symbol=str(match.get("symbol") or requested_symbol).upper(),
            display_name=str(match.get("displayName") or requested_symbol).strip(),
            price_increment=price_increment,
            size_increment=size_increment,
            minimum_size=None,
        ),
        data={"match": match, "symbol": requested_symbol},
    )


def _public_credentials() -> Dict[str, Any]:
    """Return a credentials dict for public-market reads.

    ``/exchangeInfo`` does not require auth; we still want the same
    base URL + redaction context. The ``api_key`` placeholder keeps the
    headers consistent but is never used (the X-API-KEY value is
    ignored for public endpoints per the docs).
    """
    return {
        "account": "",
        "public_key": "",
        "api_key": "PUBLIC",
        "sign_private_key": "",
        "account_group": 0,
        "base_url": (_read_env("VEST_API_BASE") or DEFAULT_API_BASE).rstrip("/"),
    }


# ---------------------------------------------------------------------------
# Instrument catalog (Phase 4: symbol resolution for New Order / Ladder)
# ---------------------------------------------------------------------------
#
# Vest's public catalog endpoint (``GET /exchangeInfo``) returns the full
# contract metadata. We cache the parsed catalog briefly (default 60s)
# because the wizard fires one or two ``resolve_instrument`` / ``market_price``
# calls per symbol pick and the catalog is large but stable.

_PATH_EXCHANGE_INFO = "/exchangeInfo"
_PATH_TICKER_LATEST = "/ticker/latest"
_CATALOG_TTL_SECONDS = 60.0
_catalog_cache: Dict[str, Any] = {"ts": 0.0, "rows": [], "lock": None}
_catalog_lock = threading.Lock()


def _fetch_catalog(credentials: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the parsed ``/exchangeInfo`` catalog.

    Caches the parsed rows for ``_CATALOG_TTL_SECONDS`` so the wizard's
    symbol picker doesn't replay an expensive call on every keystroke.
    """
    global _catalog_cache
    now = time.monotonic()
    with _catalog_lock:
        if _catalog_cache["rows"] and (now - _catalog_cache["ts"]) < _CATALOG_TTL_SECONDS:
            return list(_catalog_cache["rows"])
        try:
            payload = _signed_get(credentials, _PATH_EXCHANGE_INFO)
        except VestHTTPError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VestHTTPError(
                status=0,
                path=_PATH_EXCHANGE_INFO,
                body=f"catalog fetch error: {exc}",
                credentials=credentials,
            ) from exc
        rows_raw = (
            payload.get("symbols")
            if isinstance(payload, dict)
            else None
        )
        rows: List[Dict[str, Any]] = []
        if isinstance(rows_raw, list):
            for row in rows_raw:
                if not isinstance(row, dict):
                    continue
                symbol = str(row.get("symbol") or "").strip()
                if not symbol:
                    continue
                base = str(row.get("base") or "").strip() or symbol.split("-", 1)[0]
                quote = str(row.get("quote") or "").strip() or "USDC"
                display = str(row.get("displayName") or symbol).strip()
                rows.append(
                    {
                        "symbol": symbol,
                        "display_name": display,
                        "base": base,
                        "quote": quote,
                        "size_decimals": row.get("sizeDecimals"),
                        "price_decimals": row.get("priceDecimals"),
                        "init_margin_ratio": row.get("initMarginRatio"),
                        "maint_margin_ratio": row.get("maintMarginRatio"),
                        "taker_fee": row.get("takerFee"),
                        "isolated": row.get("isolated"),
                    }
                )
        _catalog_cache = {"ts": now, "rows": rows, "lock": None}
        return list(rows)


def _strip_quote_suffix(token: str, quote_hint: str = "USDC") -> str:
    """Strip ``-USDC`` / ``USDC`` / ``-USD`` / ``USD`` suffixes to a base."""
    base = token.strip().upper()
    for suffix in ("USDC", "USDT", "USD"):
        if base.endswith(suffix) and len(base) > len(suffix):
            return base[: -len(suffix)]
    return base


def _vest_resolve_symbol(
    requested: str,
    catalog: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return every catalog row matching ``requested``.

    Accepts any of:
      - bare base: ``BTC``, ``HYPE``, ``SPY``, ``NDX``
      - full symbol: ``BTC-PERP``, ``BTC-USDC``
      - canonical alias: ``"NASDAQ"`` → ``NDX`` futures,
        ``"GOLD"`` → ``XAU``, ``"S&P"`` → SPX, ``"ES"`` → ES futures
      - substring keywords: ``"500"``, ``"NASDAQ"``, ``"S&P"`` match any
        catalog row whose display_name contains the keyword (so the
        wizard can offer both ``SPX-USD-PERP`` and ``SPY-USD-PERP``
        when the user types ``"500"`` or ``"S&P 500"``).

    Returns a list of normalized row dicts (same shape as one entry of
    ``_fetch_catalog``). Empty list = no match. Order: exact symbol,
    canonical-alias, base, then display-substring matches — keeps the
    most-confident match at the top of the wizard's button column.
    """
    target = (requested or "").strip().upper()
    if not target:
        return []
    # Canonical aliases — what the user "meant" by an ambiguous token.
    # Keys are normalised to UPPERCASE so the lookup is case-insensitive.
    aliases = {
        "GOLD": "XAU",
        "SILVER": "XAG",
        "OIL": "USO",
        "CRUDE": "USO",
        # Indices: Vest's venue id for the Nasdaq 100 futures is ``NDX``,
        # not ``NQ``. NQ is a generic symbol prefix; ``NDAQ`` is the
        # actual Nasdaq Inc. stock. We map the user-typed alias to the
        # futures contract so single-match resolution picks the right
        # one; substring keyword handling below surfaces ``NDAQ`` too
        # when the user types ``"NASDAQ"``.
        "NASDAQ": "NDX",
        "NASD": "NDX",
        "NDX": "NDX",
        "NQ": "NDX",
        # S&P 500 → SPX futures (canonical), SPY ETF, ES futures all
        # appear via the substring match below.
        "SP500": "SPX",
        "S&P500": "SPX",
        "S&P": "SPX",
        "SPX": "SPX",
        "ES": "ES",
    }
    target_base = _strip_quote_suffix(target)
    target_base = aliases.get(target, target_base)
    candidates_for_target = {
        target,
        f"{target}-PERP",
        f"{target}-USDC",
        f"{target}-USD",
    }
    candidates_for_base = {
        target_base,
        f"{target_base}-PERP",
        f"{target_base}-USDC",
        f"{target_base}-USD",
    }
    # Substring keywords — match display names that include the term.
    # Used to surface *related* instruments the user might want when
    # the typed token is a search phrase rather than a symbol. Also
    # used for free-text search terms (e.g. "GOLD" → GLD/SPDR Gold
    # Shares ETF, GC/gold futures).
    sub_keyword_aliases = {
        "500": ["500"],
        "S&P": ["S&P"],
        "S and P": ["S&P"],
        "NASDAQ": ["NASDAQ"],
        "NDX": ["NASDAQ"],
        "NASD": ["NASDAQ"],
        "NQ": ["NASDAQ"],
        "DOW": ["DOW"],
        "DJI": ["DOW"],
        "RUSSELL": ["RUSSELL"],
        "FTSE": ["FTSE"],
        "NIKKEI": ["NIKKE"],
        "GOLD": ["GOLD"],
        "XAU": ["GOLD"],
        "OIL": ["OIL"],
        "CRUDE": ["OIL", "CRUDE"],
        "USO": ["OIL"],
        "CL": ["OIL"],
        "WTI": ["OIL"],
        "SILVER": ["SILVER"],
        "XAG": ["SILVER"],
    }
    sub_keywords = sub_keyword_aliases.get(target, [])

    exact: List[Dict[str, Any]] = []
    by_alias: List[Dict[str, Any]] = []
    by_base: List[Dict[str, Any]] = []
    by_display: List[Dict[str, Any]] = []
    for row in catalog:
        symbol = str(row.get("symbol") or "").upper()
        base = str(row.get("base") or "").upper()
        display = str(row.get("display_name") or "").upper()
        if symbol == target:
            exact.append(row)
            continue
        if symbol in candidates_for_target:
            exact.append(row)
            continue
        # Canonical-alias path — match via the base the alias maps to.
        if target in aliases:
            aliased = aliases[target]
            if base == aliased or symbol.startswith(f"{aliased}-USD"):
                by_alias.append(row)
                continue
        # Bare-base path — "BTC" matches ``BTC-PERP`` because ``BTC`` is
        # the base of the row.
        if base == target_base or base in candidates_for_base:
            by_base.append(row)
            continue
        # Display-name substring path — "500" → SPX (display
        # "S&P 500 E-mini Futures") and SPY (display "SPDR S&P 500 ETF
        # Trust").
        if display == target:
            by_display.append(row)
            continue
        if any(kw in display for kw in sub_keywords):
            by_display.append(row)
            continue

    # De-dup while preserving order: exact > alias > base > display.
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in exact + by_alias + by_base + by_display:
        key = str(row.get("symbol") or "").upper()
        if key and key not in seen:
            seen.add(key)
            out.append(row)
    return out


def _vest_fetch_mark_price(
    credentials: Dict[str, Any],
    native_symbol: str,
) -> Optional[Decimal]:
    """Return the latest ``markPrice`` for ``native_symbol`` or ``None``.

    Caches the per-symbol price for ``_CATALOG_TTL_SECONDS`` so the
    wizard's candidate button renderer doesn't replay for every key.
    """
    cache_key = "mark:" + native_symbol.upper()
    now = time.monotonic()
    cache = _catalog_cache
    cached = cache.get(cache_key)
    if cached and cached[0] > now - _CATALOG_TTL_SECONDS:
        return cached[1]
    try:
        payload = _signed_get(
            credentials,
            f"{_PATH_TICKER_LATEST}?symbols={urllib.parse.quote(native_symbol, safe='')}",
        )
    except VestHTTPError:
        return None
    except Exception:  # noqa: BLE001
        return None
    tickers = payload.get("tickers") if isinstance(payload, dict) else None
    if not isinstance(tickers, list) or not tickers:
        return None
    for entry in tickers:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("symbol") or "").upper() != native_symbol.upper():
            continue
        mark_raw = entry.get("markPrice")
        if mark_raw is None:
            continue
        mark = _decimal_or_none(mark_raw)
        if mark is not None and mark > 0:
            _catalog_cache["mark:" + native_symbol.upper()] = (now, mark)
            return mark
    return None


def _instrument_from_catalog_row(
    requested: str,
    row: Dict[str, Any],
    *,
    mark_price: Optional[Decimal] = None,
) -> CanonicalInstrument:
    """Build a CanonicalInstrument from a parsed catalog row."""
    native = str(row.get("symbol") or "").strip()
    base = str(row.get("base") or "").strip()
    quote = str(row.get("quote") or "USDC").strip()
    price_decimals = _decimal_or_none(row.get("price_decimals"))
    size_decimals = _decimal_or_none(row.get("size_decimals"))
    return CanonicalInstrument(
        requested_symbol=requested,
        symbol=native,
        display_name=str(row.get("display_name") or native),
        native_symbol=native,
        display_symbol=str(row.get("display_name") or native),
        base=base or None,
        quote=quote or None,
        market_type="perp",
        price_increment=(
            str(Decimal(10) ** -int(price_decimals))
            if price_decimals is not None
            else None
        ),
        size_increment=(
            str(Decimal(10) ** -int(size_decimals))
            if size_decimals is not None
            else None
        ),
    )


def _list_instruments(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Enumerate the parsed catalog for the symbol picker."""
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Vest Markets account configuration",
        )
    try:
        catalog = _fetch_catalog(credentials)
    except VestHTTPError as exc:
        return _map_http_error_to_failure(
            exc, operation="list_instruments", account=credentials["account"]
        )
    instruments: List[Dict[str, Any]] = []
    for row in catalog:
        instruments.append(
            {
                "instrument": str(row.get("symbol") or ""),
                "display_name": str(row.get("display_name") or row.get("symbol") or ""),
                "base": str(row.get("base") or ""),
                "quote": str(row.get("quote") or "USDC"),
                "market_type": "perp",
            }
        )
    return make_success(
        operation="list_instruments",
        exchange=name,
        account=credentials["account"],
        data={"instruments": instruments},
    )


def _resolve_instrument(
    account: str, request: Dict[str, Any]
) -> CanonicalResponse:
    """Resolve a wizard-supplied symbol to a single match or fail with
    candidate-priced list.
    """
    requested = str(
        request.get("symbol")
        or request.get("requested_symbol")
        or request.get("query")
        or ""
    ).strip()
    if not requested:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Vest Markets account configuration",
        )
    try:
        catalog = _fetch_catalog(credentials)
    except VestHTTPError as exc:
        return _map_http_error_to_failure(
            exc, operation="resolve_instrument", account=credentials["account"]
        )
    matches = _vest_resolve_symbol(requested, catalog)
    if not matches:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=credentials["account"],
            code="INSTRUMENT_NOT_FOUND",
            message=f"Vest symbol '{requested}' is not available.",
        )
    if len(matches) > 1:
        # The wizard reads ``data.candidates`` off the failure response so
        # it can render priced candidate buttons without a second round
        # trip to ``list_instruments``. ``make_failure`` doesn't expose a
        # ``data=`` kwarg, so we build the CanonicalResponse directly.
        candidates_payload: List[Dict[str, Any]] = []
        for row in matches[:5]:
            mark = _vest_fetch_mark_price(credentials, str(row.get("symbol") or ""))
            candidates_payload.append(
                {
                    "instrument": str(row.get("symbol") or ""),
                    "display_name": str(
                        row.get("display_name") or row.get("symbol") or ""
                    ),
                    "base": str(row.get("base") or ""),
                    "quote": str(row.get("quote") or "USDC"),
                    "market_type": "perp",
                    "price": _decimal_text(mark) if mark is not None else None,
                }
            )
        return CanonicalResponse(
            success=False,
            operation="resolve_instrument",
            exchange=name,
            account=credentials["account"],
            error=CanonicalError(
                code="INSTRUMENT_AMBIGUOUS",
                message=f"Vest symbol '{requested}' is ambiguous.",
            ),
            data={"candidates": candidates_payload},
        )
    row = matches[0]
    native = str(row.get("symbol") or "").strip()
    mark = _vest_fetch_mark_price(credentials, native)
    return make_success(
        operation="resolve_instrument",
        exchange=name,
        account=credentials["account"],
        instrument=_instrument_from_catalog_row(requested, row),
        market_price=(
            CanonicalMarketPrice(
                requested_symbol=requested,
                market=native,
                mark_price=_decimal_text(mark) if mark is not None else None,
                price=_decimal_text(mark) if mark is not None else None,
            )
            if mark is not None
            else None
        ),
    )


def _market_price(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Return the latest mark price for a single resolved symbol."""
    requested = str(
        request.get("symbol")
        or request.get("requested_symbol")
        or request.get("query")
        or ""
    ).strip()
    if not requested:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Vest Markets account configuration",
        )
    try:
        catalog = _fetch_catalog(credentials)
    except VestHTTPError as exc:
        return _map_http_error_to_failure(
            exc, operation="market_price", account=credentials["account"]
        )
    matches = _vest_resolve_symbol(requested, catalog)
    if not matches:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=credentials["account"],
            code="INSTRUMENT_NOT_FOUND",
            message=f"Vest symbol '{requested}' is not available.",
        )
    row = matches[0]
    native = str(row.get("symbol") or "").strip()
    mark = _vest_fetch_mark_price(credentials, native)
    if mark is None or mark <= 0:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=credentials["account"],
            code="PRICE_UNAVAILABLE",
            message=f"Price unavailable for {requested}",
        )
    return make_success(
        operation="market_price",
        exchange=name,
        account=credentials["account"],
        instrument=_instrument_from_catalog_row(requested, row),
        market_price=CanonicalMarketPrice(
            requested_symbol=requested,
            market=native,
            mark_price=_decimal_text(mark),
            price=_decimal_text(mark),
        ),
    )


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _map_http_error_to_failure(
    error: VestHTTPError, *, operation: str, account: str
) -> CanonicalResponse:
    """Translate a Vest HTTP failure into a canonical failure response.

    Vest's documented error codes (``UNAUTHORIZED=1002``,
    ``INVALID_SIGNATURE=1022``, ``INVALID_NONCE=1023``, ``ACCOUNT_NOT_FOUND=1099``,
    ``TOO_MANY_REQUESTS=1003``, …) flow through here verbatim so the
    wizard can render them in its error surface.
    """
    status = int(getattr(error, "status", 0) or 0)
    body = str(getattr(error, "body", "") or "").strip()
    code = "VEST_ERROR"
    message = body or f"Vest Markets HTTP {status}"
    if status in (401, 403):
        code = "AUTH_INVALID"
        message = body or "Vest Markets rejected the API key."
    elif status == 404:
        code = "NOT_FOUND"
    elif status == 429:
        code = "RATE_LIMITED"
        message = body or "Vest Markets rate limit exceeded."
    elif status == 0:
        code = "TRANSPORT_ERROR"
        message = body or "Vest Markets transport error."
    return make_failure(
        operation=operation,
        exchange=name,
        account=account,
        code=code,
        message=_redact(
            sanitize_error_message(message),
            fallback_credentials=getattr(error, "credentials", None),
        ),
    )


# ---------------------------------------------------------------------------
# Vest API path constants (kept here so future write phases have a
# single source of truth; not used in Phase 1).
# ---------------------------------------------------------------------------

# Documented endpoints from https://vest-labs.gitbook.io/vest-markets-old/vest-api
# Kept here for the next phase; remove if the operator says so.
_PATH_PUBLIC_EXCHANGE_INFO = "/exchangeInfo"
_PATH_PUBLIC_TICKER_LATEST = "/ticker/latest"
_PATH_PUBLIC_TICKER_24HR = "/ticker/24hr"
_PATH_PUBLIC_FUNDING_HISTORY = "/funding/history"
_PATH_PUBLIC_KLINES = "/klines"
_PATH_PUBLIC_TRADES = "/trades"
_PATH_PUBLIC_DEPTH = "/depth"
_PATH_PRIVATE_REGISTER = "/register"
_PATH_PRIVATE_ACCOUNT_NONCE = "/account/nonce"
_PATH_PRIVATE_ACCOUNT_LISTEN_KEY = "/account/listenKey"
_PATH_PRIVATE_LEVERAGE = "/account/leverage"
_PATH_PRIVATE_ORDERS = "/orders"
_PATH_PRIVATE_ORDERS_CANCEL = "/orders/cancel"


# Vest Verifying contract addresses (per the API docs). Used by future
# ``POST /register`` and order-signing phases. Not used in Phase 1.
VERIFYING_CONTRACT_PROD = "0x919386306C47b2Fe1036e3B4F7C40D22D2461a23"
VERIFYING_CONTRACT_DEV = "0x8E4D87AEf4AC4D5415C35A12319013e34223825B"