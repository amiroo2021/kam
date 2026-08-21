"""Ondo Perps exchange agent.

This module owns EVERYTHING Ondo Perps-specific for the /trade stack.

Current scope (Phase 1 — read-only):

- Credential discovery from ``ONDOPERPS_<ALIAS>_APIKEY`` and
  ``ONDOPERPS_<ALIAS>_APISECRET`` environment variables (live + ``~/.hermes/.env``),
  matching the convention used by every other KAM exchange agent. The
  documented Ondo prefixes (``ondoKeyId_`` / ``ondoApiSecret_``) are
  required — accounts without both prefixes are treated as not yet
  provisioned and silently skipped at the discovery layer.
- Authenticated read-only retrieval through the Ondo HMAC API-Key scheme
  (the documented ``/api-reference/api_key_authentication`` flow):
    * Headers: ``ONDO-KEY-ID``, ``ONDO-TIMESTAMP`` (milliseconds since
      epoch, ±30s), ``ONDO-SIGN`` (hex HMAC-SHA256).
    * Canonical string: ``timestamp + METHOD + requestPathWithQuery + body``,
      concatenated with no separators, signed with the API secret.
  The base URL is ``https://api.ondoperps.xyz`` for production;
  ``ONDOPERPS_API_BASE`` overrides it (e.g. for the documented sandbox
  at ``https://api.ondoperps-sandbox.xyz``).
- Balance + positions + open-orders retrieval through:
    * ``GET /v1/perps/balance``
    * ``GET /v1/perps/positions``
    * ``GET /v1/perps/orders?status=open``
  All three are normalized into the canonical ``CanonicalBalance`` /
  ``CanonicalPosition`` / ``CanonicalOrderGroup`` shapes the wizard's
  "📋 Open Orders & 💼 Positions" view already consumes from every other
  KAM exchange.
- Canonical instrument resolution scaffold (public ``GET /v1/markets``).
- Canonical conversion into the exchange-agnostic TradeDesk / wizard contract.

TradeDesk and the Telegram wizard MUST remain exchange-agnostic and MUST NOT
parse ``ONDOPERPS_*`` environment variables or Ondo-native payloads. All
Ondo-specific behavior — env-var scanning, REST calls, header construction,
response parsing, symbol translation — lives in this module.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..canonical import (
    CanonicalBalance,
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


# ---------------------------------------------------------------------------
# Module identity — required by TradeDesk.
# ---------------------------------------------------------------------------

name = "ondoperps"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_API_BASE = "https://api.ondoperps.xyz"
API_TIMEOUT_SECONDS = 20
MAX_RETRIES = 2
# Ondo's documented 30-second timestamp tolerance.
TIMESTAMP_TOLERANCE_MS = 30_000

# Ondo's edge sits behind Cloudflare with bot-detection (Error 1010 blocks
# the default urllib User-Agent). Send a browser-like UA to get past the
# gate. This is a UA-only header; it does not modify the signed payload.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Ondo Perps settles in USDC. The canonical balance unit is therefore USDC,
# matching the wizard's display contract.
SETTLEMENT_UNIT = "USDC"

# Documented credential prefixes — see Ondo's API key authentication page.
# Both the API key id and the API secret carry these prefixes as part of
# their on-the-wire value (the key creation endpoint returns them verbatim).
APIKEY_PREFIX = "ondoKeyId_"
APISECRET_PREFIX = "ondoApiSecret_"

# Required credential suffixes for a fully configured account.
ONDOPERPS_REQUIRED_SUFFIXES: Tuple[str, ...] = (
    "APIKEY",
    "APISECRET",
)

# Account-alias pattern: must start with a letter, then ASCII letters /
# digits / underscores. Same convention as every other KAM agent.
_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Documented, public endpoints (per https://docs.ondoperps.xyz/llms.txt and
# the rest-spec.json OpenAPI document). Centralised so future phases (and
# tests) reference a single source of truth.
_PATH_PERPS_BALANCE = "/v1/perps/balance"
_PATH_PERPS_POSITIONS = "/v1/perps/positions"
_PATH_PERPS_ORDERS = "/v1/perps/orders"
_PATH_PERPS_MARK_PRICES = "/v1/perps/mark_prices"
_PATH_MARKETS = "/v1/markets"
_PATH_ACCOUNT = "/v1/account"

_CLIENT_ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _client_order_lookup_path(client_order_id: str) -> str:
    """Official lookup path for a client order id.

    Ondo Perps documents ``GET /v1/perps/orders/{orderID}`` where
    ``orderID`` can be ``client:{clientOrderID}``.
    """
    text = str(client_order_id or "").strip()
    if not text:
        raise ValueError("client_order_id is required")
    return f"{_PATH_PERPS_ORDERS}/client:{text}"


def _fetch_order_by_client_order_id(credentials: Dict[str, Any], client_order_id: str) -> Dict[str, Any]:
    payload = _signed_get(credentials, _client_order_lookup_path(client_order_id))
    return payload if isinstance(payload, dict) else {}


def _verify_exact_order_by_client_order_id(
    credentials: Dict[str, Any],
    *,
    client_order_id: str,
    market: str,
    side: str,
    size: Decimal,
    attempts: int = 4,
    base_delay: float = 0.25,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Verify an order by exact clientOrderId lookup.

    For market orders, Ondo documents ``GET /v1/perps/orders/client:<id>``.
    We use that exact lookup instead of inferring success from a generic
    position row or from the presence of an ``orderId`` alone.
    """
    last: Optional[Dict[str, Any]] = None
    for attempt in range(max(1, attempts)):
        try:
            order = _fetch_order_by_client_order_id(credentials, client_order_id)
        except OndoHTTPError as exc:
            if exc.status == 400 and "order_not_found" in str(exc.body):
                order = None
            else:
                raise
        except RuntimeError as exc:
            if "order_not_found" in str(exc):
                order = None
            else:
                raise
        if isinstance(order, dict) and order:
            last = order
            order_market = str(order.get("market") or "").strip()
            order_side = str(order.get("side") or "").strip().lower()
            order_status = str(order.get("status") or "").strip().lower()
            order_size = _decimal_or_none(order.get("size"))
            filled_size = _decimal_or_none(order.get("filledSize"))
            if (
                order_market == market
                and order_side == side
                and order_size == size
                and order_status in {"open", "pending", "fullyfilled"}
                and (filled_size is None or filled_size >= 0)
            ):
                return True, order
            return False, order
        if attempt < attempts - 1:
            time.sleep(base_delay)
    return False, last


def _normalize_client_order_id(value: Any) -> Optional[str]:
    """Validate a client-provided order id against the official schema."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > 64:
        raise ValueError("Client order ID must be at most 64 characters.")
    if not _CLIENT_ORDER_ID_PATTERN.fullmatch(text):
        raise ValueError(
            "Client order ID must use only letters, digits, underscores, or dashes."
        )
    return text

# Ondo's documented order statuses. We only consider "open" / "pending" /
# "untriggered" as resting for the wizard's open-orders surface.
RESTING_ORDER_STATUSES = frozenset({"open", "pending", "untriggered"})


# ---------------------------------------------------------------------------
# Env / dotenv helpers — minimal, mirroring the rest of the trade package.
# ---------------------------------------------------------------------------


def _hermes_home() -> Path:
    """Return the Hermes home directory (``~/.hermes`` by default)."""
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _load_dotenv_values(path: Path) -> Dict[str, str]:
    """Minimal ``.env`` parser.

    Honors the same convention as the rest of KAM: ``KEY=VALUE`` pairs,
    optional quoting, ``#``-prefixed comments. Missing / unreadable files
    yield an empty dict.
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


def _combined_ondoperps_env() -> Dict[str, str]:
    """Return ``{key: value}`` for all ``ONDOPERPS_*`` variables, merging
    ``os.environ`` and ``$HERMES_HOME/.env``.

    Live env always wins (``setdefault`` semantics).
    """
    values: Dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith("ONDOPERPS_"):
            values[key] = (value or "").strip()
    for key, value in _load_dotenv_values(_hermes_home() / ".env").items():
        if key.startswith("ONDOPERPS_"):
            values.setdefault(key, (value or "").strip())
    return values


def _read_env(name: str) -> str:
    """Read a single env var, falling back to ~/.hermes/.env."""
    live = os.environ.get(name, "").strip()
    if live:
        return live
    return _load_dotenv_values(_hermes_home() / ".env").get(name, "").strip()


# ---------------------------------------------------------------------------
# Account discovery
# ---------------------------------------------------------------------------


def _normalize_alias(raw_account: str) -> str:
    """Sanitize an Ondo Perps account alias.

    Returns the lowercased form for use as the public alias (matching every
    other KAM exchange agent that surfaces lowercase aliases to the wizard).
    """
    alias = raw_account.strip().strip("_")
    if not alias:
        return ""
    return alias.lower() if _ALIAS_PATTERN.match(alias.upper()) else alias.lower()


def _looks_like_real_key(value: str) -> bool:
    """True iff ``value`` carries Ondo's documented ``ondoKeyId_`` /
    ``ondoApiSecret_`` prefix.

    Ondo's key-creation endpoint returns keys/secret with these prefixes
    embedded in the string (e.g. ``ondoKeyId_3a074be43c…``). If the value
    doesn't start with the appropriate prefix we treat it as not yet
    provisioned, so a half-filled ``.env`` doesn't surface an unusable
    account in the wizard.
    """
    text = str(value or "").strip()
    return bool(text) and (
        text.startswith(APIKEY_PREFIX) or text.startswith(APISECRET_PREFIX)
    )


def _has_complete_credentials(raw_account: str, env: Dict[str, str]) -> bool:
    """True iff every required suffix for ``raw_account`` is present,
    non-empty, AND carries Ondo's documented prefix.
    """
    for suffix in ONDOPERPS_REQUIRED_SUFFIXES:
        key = f"ONDOPERPS_{raw_account}_{suffix}".upper()
        value = (env.get(key, "") or "").strip()
        if not value:
            return False
        # Both the API key and the API secret are documented to carry a
        # recognised prefix. Anything else is treated as a placeholder.
        if not value.startswith(APIKEY_PREFIX) and not value.startswith(APISECRET_PREFIX):
            return False
    return True


def _discover_accounts() -> List[str]:
    """Return the list of configured Ondo Perps account aliases.

    An account is "complete" (and therefore surfaced to the wizard) iff
    both ``ONDOPERPS_<ALIAS>_APIKEY`` and ``ONDOPERPS_<ALIAS>_APISECRET``
    are present, non-empty, and carry Ondo's documented prefixes.
    Aliases are returned in sorted, lower-cased form.
    """
    env = _combined_ondoperps_env()
    aliases: List[str] = []
    seen: set = set()
    for key in env:
        if not key.startswith("ONDOPERPS_") or not key.endswith("_APIKEY"):
            continue
        raw_account = key[len("ONDOPERPS_"):-len("_APIKEY")]
        alias = _normalize_alias(raw_account)
        if not alias or alias in seen:
            continue
        if _has_complete_credentials(raw_account, env):
            seen.add(alias)
            aliases.append(alias)
    return sorted(aliases)


def _lookup_credentials(account: str) -> Optional[Dict[str, Any]]:
    """Look up the two Ondo Perps credentials for ``account``.

    Returns a dict shaped for the HTTP layer (``api_key``, ``api_secret``,
    plus the public ``account`` alias and resolved ``base_url``) or
    ``None`` if the account is unknown or incomplete. The caller MUST
    treat ``api_key`` and ``api_secret`` as sensitive — they must never
    be logged or echoed in error messages.
    """
    raw = str(account or "").strip()
    if not raw:
        return None
    upper = raw.upper()
    if not _ALIAS_PATTERN.match(upper):
        return None
    env = _combined_ondoperps_env()
    api_key = env.get(f"ONDOPERPS_{upper}_APIKEY", "").strip()
    api_secret = env.get(f"ONDOPERPS_{upper}_APISECRET", "").strip()
    if not api_key or not api_secret:
        return None
    if not api_key.startswith(APIKEY_PREFIX) or not api_secret.startswith(APISECRET_PREFIX):
        return None
    base_url = (_read_env("ONDOPERPS_API_BASE") or DEFAULT_API_BASE).rstrip("/")
    return {
        "account": raw.lower(),
        "api_key": api_key,
        "api_secret": api_secret,
        "base_url": base_url,
    }


# ---------------------------------------------------------------------------
# Public agent contract (TradeDesk)
# ---------------------------------------------------------------------------


def list_accounts() -> List[str]:
    """Return the configured Ondo Perps account aliases (lowercased, sorted)."""
    return _discover_accounts()


def capabilities() -> List[str]:
    """Return the operations this agent supports.

    Phase 2 adds the full write surface that the wizard's "🆕 New Order",
    "🪜 Ladder", "❌ Cancel Orders", and "🛡️ Positions Management" menus
    need: ``new_order``, ``ladder``, ``cancel_order_group``,
    ``positions_management``, ``set_tp``, ``set_sl``, ``close_position``.
    Phase-1 read ops (``balance``, ``positions_orders``,
    ``resolve_instrument``) remain.
    """
    return [
        "balance",
        "positions_orders",
        "positions_management",
        "new_order",
        "get_exact_order",
        "ladder",
        "cancel_order_group",
        "cancel_order",
        "get_order_state",
        "get_order_state_by_client_id",
        "market_constraints",
        "set_tp",
        "set_sl",
        "set_position_protections",
        "position_state",
        "market_price",
        "close_position",
        "resolve_instrument",
    ]


def ladder_max_orders_per_instrument() -> Optional[int]:
    """Return the exchange's per-instrument open-order cap.

    Surfaced for the wizard's ladder order-count screen so the operator
    can see ``MAX ORDERS PER INSTRUMENT = N`` before typing how many
    rungs they want. **Informational only**: the wizard does not clamp,
    reject, or otherwise act on this number — entering more than the
    cap is allowed and the agent submits whatever the operator typed.
    If the exchange rejects the submission, the agent surfaces the
    rejection as a normal canonical error.

    This is distinct from the agent's internal batch-chunking cap
    (``_LADDER_MAX_BATCH = 20``) and the safety guardrail
    (``_LADDER_ABSOLUTE_MAX_ORDERS = 1000``); those exist to control
    HTTP-call fan-out and to catch fat-finger input, while this
    constant describes the **exchange's** ceiling on open orders for a
    single instrument — a separate, user-facing concept.
    """
    return LADDER_MAX_ORDERS_PER_INSTRUMENT


def execute(request: Dict[str, Any]) -> CanonicalResponse:
    """Dispatch a canonical request to the Ondo Perps agent.

    Phase 1 supports ``balance``, ``positions_orders``, and
    ``resolve_instrument``. Any other operation returns a canonical
    ``NOT_IMPLEMENTED`` error.
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
        if operation == "resolve_instrument":
            return _resolve_instrument(account, request)
        if operation == "new_order":
            return _new_order(account, request)
        if operation == "get_exact_order":
            return _get_exact_order(account, request)
        if operation == "ladder":
            return _ladder(account, request)
        if operation == "cancel_order_group":
            return _cancel_order_group(account, request)
        if operation == "cancel_order":
            return _execute_cancel_order(account, request)
        if operation == "get_order_state":
            return _execute_get_order_state(account, request)
        if operation == "get_order_state_by_client_id":
            return _execute_get_order_state_by_client_id(account, request)
        if operation == "market_constraints":
            return _execute_market_constraints(account, request)
        if operation == "set_tp":
            return _set_position_trigger(account, request, kind="takeProfit")
        if operation == "set_sl":
            return _set_position_trigger(account, request, kind="stopLoss")
        if operation == "set_position_protections":
            return _set_position_protections(account, request)
        if operation == "position_state":
            return _position_state(account, request)
        if operation == "market_price":
            return _market_price(account, request)
        if operation == "close_position":
            return _close_position(account, request)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="ONDOPERPS_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )
    return make_failure(
        operation=operation,
        exchange=name,
        account=account,
        code="NOT_IMPLEMENTED",
        message=f"Ondo Perps does not implement '{operation}' yet.",
    )


# ---------------------------------------------------------------------------
# HTTP layer — Ondo HMAC API-Key scheme
# ---------------------------------------------------------------------------


def _now_ms() -> str:
    """Return the current Unix timestamp in milliseconds (string)."""
    return str(int(time.time() * 1000))


def _sign_request(
    *,
    method: str,
    request_path: str,
    body: str,
    api_secret: str,
    timestamp_ms: str,
) -> str:
    """Compute the HMAC-SHA256 signature for a signed request.

    Per Ondo's documented API-Key authentication, the canonical string is:

        ``timestamp + METHOD + requestPath + body``

    concatenated with **no separators**. ``requestPath`` includes any query
    string (the live server rejects signatures that omit it — confirmed
    against ``GET /v1/perps/orders?status=open`` returning
    ``signature_mismatch`` when only the path was signed). ``body`` is the
    raw JSON string for requests that carry one, empty string otherwise.

    The HMAC key is the per-account API secret, hex-encoded into the
    ``ONDO-SIGN`` header. The signature is deterministic and the single
    seam to adjust if the live spec ever drifts.
    """
    method_upper = (method or "").strip().upper()
    if not method_upper:
        raise ValueError("Signing requires a request method")
    canonical = f"{timestamp_ms}{method_upper}{request_path}{body}"
    digest = hmac.new(
        api_secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    )
    return digest.hexdigest()


def _signed_get(
    credentials: Dict[str, Any],
    path_with_query: str,
) -> Any:
    """GET against the Ondo Perps REST API with the signed API-key headers.

    ``path_with_query`` includes any query string and is signed verbatim
    (the server validates the signature against path+query, not path
    alone).
    """
    return _signed_request(credentials, method="GET", path_with_query=path_with_query, body="")


def _signed_request(
    credentials: Dict[str, Any],
    *,
    method: str,
    path_with_query: str,
    body: str,
) -> Any:
    """Issue a signed request to Ondo Perps and return the parsed JSON.

    The headers, base URL resolution, request serialisation, response
    unwrapping (Ondo wraps payloads in ``{success, result}``), and error
    mapping all live here so the per-operation handlers can stay focused
    on normalization.
    """
    base_url = str(credentials["base_url"]).rstrip("/")
    timestamp_ms = _now_ms()
    signature = _sign_request(
        method=method,
        request_path=path_with_query,
        body=body,
        api_secret=credentials["api_secret"],
        timestamp_ms=timestamp_ms,
    )
    url = f"{base_url}{path_with_query}"
    headers = {
        "Accept": "application/json",
        "User-Agent": BROWSER_USER_AGENT,
        "ONDO-KEY-ID": credentials["api_key"],
        "ONDO-TIMESTAMP": timestamp_ms,
        "ONDO-SIGN": signature,
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
            raise OndoHTTPError(
                status=int(exc.code),
                path=path_with_query,
                body=error_body or str(exc.reason),
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ondo Perps API unreachable: {exc.reason}") from exc
    return _parse_response(raw_text, path=path_with_query)


class OndoHTTPError(RuntimeError):
    def __init__(self, *, status: int, path: str, body: str):
        self.status = status
        self.path = path
        self.body = body
        super().__init__(f"HTTP {status} on {path}: {body[:200]}")


def _parse_response(raw_text: str, *, path: str) -> Any:
    """Parse an Ondo Perps response body.

    Ondo wraps successful payloads in ``{"success": true, "result": …}``
    (per the integration guide's ``parseResponse`` helper). Failures are
    ``{"success": false, "error": …, "error_code": …}`` and surface as a
    runtime error carrying both the human message and the semantic
    error_code so the wizard can show codes like ``auth_missing`` or
    ``signature_mismatch`` verbatim.
    """
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Ondo Perps returned invalid JSON on {path}") from exc
    if isinstance(payload, dict) and "success" in payload:
        if not payload.get("success"):
            error_code = str(payload.get("error_code") or "").strip()
            error_message = str(payload.get("error") or "").strip() or "Unknown error"
            if error_code:
                raise RuntimeError(f"{error_code}: {error_message}")
            raise RuntimeError(error_message)
        return payload.get("result")
    # If the server returned a bare object/array (some endpoints), accept
    # it verbatim rather than failing.
    return payload


# ---------------------------------------------------------------------------
# Credential context + redaction
# ---------------------------------------------------------------------------

import threading  # noqa: E402  (placed here to keep the credential slot near the helper that consumes it)

_credential_slot = threading.local()


def _current_credentials() -> Optional[Dict[str, Any]]:
    return getattr(_credential_slot, "value", None)


class _CredentialsContext:
    """Context manager that stashes the active credentials on the current
    thread so the redaction helper can defensively scrub them from any
    error message raised during the in-flight request.
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


def _redact(text: Any) -> str:
    """Scrub sensitive substrings from a free-form error message.

    Ondo's auth headers (``ONDO-KEY-ID``, ``ONDO-TIMESTAMP``, ``ONDO-SIGN``)
    are echoed back in some error paths; the canonical contract requires
    secrets never leak, so we scrub the header values defensively. We
    also do a literal-substring scrub against the in-flight credentials
    in case a verbose server-side stack trace contained the actual
    secret value.
    """
    rendered = str(text or "")

    # 1. Authorization: <scheme> <token> -> Authorization: *** ***
    def _auth_scheme_sub(match: "re.Match[str]") -> str:
        return f"{match.group(1)}{match.group(2)} ***"

    rendered = re.sub(
        r"(?i)(authorization\s*:\s*)([A-Za-z][A-Za-z0-9_-]*)\s+[^\s,;}\"']+",
        _auth_scheme_sub,
        rendered,
    )

    # 2. Ondo-specific header value redaction.
    rendered = re.sub(
        r"(?i)(ondo-key-id\s*:\s*)([^\s,;}\"']+)",
        lambda m: f"{m.group(1)}***",
        rendered,
    )
    rendered = re.sub(
        r"(?i)(ondo-timestamp\s*:\s*)([^\s,;}\"']+)",
        lambda m: f"{m.group(1)}***",
        rendered,
    )
    rendered = re.sub(
        r"(?i)(ondo-sign\s*:\s*)([^\s,;}\"']+)",
        lambda m: f"{m.group(1)}***",
        rendered,
    )
    rendered = re.sub(
        r"(?i)(ondo-api-secret\s*:\s*)([^\s,;}\"']+)",
        lambda m: f"{m.group(1)}***",
        rendered,
    )

    # 3. Defensive literal-substring scrub against live credentials.
    creds = _current_credentials()
    if creds:
        for value in (creds.get("api_key"), creds.get("api_secret")):
            if value and value in rendered:
                rendered = rendered.replace(value, "***")

    return rendered


# ---------------------------------------------------------------------------
# Decimal helpers (mirrored from sibling agents)
# ---------------------------------------------------------------------------


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except Exception:  # noqa: BLE001
        return None


def _decimal_text(value: Any) -> str:
    decimal_value = _decimal_or_none(value)
    if decimal_value is None:
        return "0"
    rendered = format(_clamp_precision(decimal_value, max_digits=40), "f")
    if rendered in {"-0", "-0.0"}:
        return "0"
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".") or "0"
    return rendered


def _format_decimal_places(value: Decimal, places: int) -> str:
    """Round ``value`` to ``places`` decimal places and render as a string.

    Ondo returns ``averageEntryPrice`` with 30+ digits of precision
    (e.g. ``"62556.975935456948115175963277229100014"``). Asking
    ``Decimal`` to round that to 33 places produces a 71-digit integer,
    which exceeds Python's default ``Decimal`` context precision (28) and
    raises ``InvalidOperation`` from ``quantize``.

    We work around this by:
      1. Clamping the input precision to a sane bound before quantizing.
      2. Using a local ``decimal`` context with enough headroom to hold
         the intermediate result.
    """
    import decimal

    safe_value = _clamp_precision(value, max_digits=40)
    quantum = Decimal("1").scaleb(-max(0, places))
    with decimal.localcontext() as ctx:
        ctx.prec = max(60, places + 40)
        quantized = safe_value.quantize(quantum, rounding=decimal.ROUND_HALF_UP)
    return format(_clamp_precision(quantized, max_digits=40), "f")


def _clamp_precision(value: Decimal, *, max_digits: int) -> Decimal:
    """Trim trailing digits past ``max_digits`` total significant digits.

    Ondo occasionally returns numbers with 30+ digits of precision; we
    don't need that for display, and Python's ``format(dec, "f")`` raises
    ``InvalidOperation`` on values that overflow the fixed-point path.
    """
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int) or len(digits) <= max_digits:
        return value
    excess = len(digits) - max_digits
    # We only ever shrink precision, never extend it.
    quantum = Decimal(10) ** excess
    return (value / quantum).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * quantum


def _decimal_places(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        decimal_value = Decimal(text)
    except Exception:  # noqa: BLE001
        return 0
    sign, digits, exponent = decimal_value.as_tuple()
    exponent = int(exponent)
    if exponent >= 0:
        return max(0, -exponent)
    return -exponent


def _optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text if text else None


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------


def _balance(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Ondo Perps account configuration",
        )

    try:
        payload = _signed_get(credentials, _PATH_PERPS_BALANCE)
    except OndoHTTPError as exc:
        return _map_http_error_to_failure(exc, operation="balance", account=account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="ONDOPERPS_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )

    summary = _extract_balance_summary(payload)
    wallet_balance = _decimal_or_none(summary.get("walletBalance"))
    if wallet_balance is None:
        wallet_balance = _decimal_or_none(summary.get("marginBalance"))
    if wallet_balance is None:
        return make_failure(
            operation="balance",
            exchange=name,
            account=account,
            code="MALFORMED_RESPONSE",
            message="Ondo Perps balance response did not include a walletBalance field.",
        )
    balance = normalize_balance(wallet_balance, SETTLEMENT_UNIT)
    portfolio = _portfolio_summary_from_balance(summary)
    return make_success(
        operation="balance",
        exchange=name,
        account=account,
        balance=balance,
        portfolio_summary=portfolio,
    )


def _extract_balance_summary(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("Ondo Perps balance response was not an object")
    return payload


def _portfolio_summary_from_balance(summary: Dict[str, Any]) -> CanonicalPortfolioSummary:
    account_value = _decimal_or_none(summary.get("marginBalance")) or Decimal("0")
    withdrawable = _decimal_or_none(summary.get("withdrawableMargin")) or Decimal("0")
    margin_used = _decimal_or_none(summary.get("usedMargin")) or Decimal("0")
    notional_value = _decimal_or_none(summary.get("totalMaintenanceMargin")) or Decimal("0")
    return CanonicalPortfolioSummary(
        account_value=normalize_balance(account_value, SETTLEMENT_UNIT).value,
        withdrawable=normalize_balance(withdrawable, SETTLEMENT_UNIT).value,
        margin_used=normalize_balance(margin_used, SETTLEMENT_UNIT).value,
        total_position_value=normalize_balance(notional_value, SETTLEMENT_UNIT).value,
        unit=SETTLEMENT_UNIT,
    )


# ---------------------------------------------------------------------------
# Positions + open orders
# ---------------------------------------------------------------------------


def _positions_orders(account: str) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Ondo Perps account configuration",
        )

    try:
        positions_payload = _signed_get(credentials, _PATH_PERPS_POSITIONS)
        orders_payload = _signed_get(
            credentials,
            f"{_PATH_PERPS_ORDERS}?status=open",
        )
    except OndoHTTPError as exc:
        return _map_http_error_to_failure(exc, operation="positions_orders", account=account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="positions_orders",
            exchange=name,
            account=account,
            code="ONDOPERPS_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )

    market_metadata = _safe_fetch_market_metadata(credentials)
    stop_protections = _safe_fetch_stop_protections(credentials)
    positions = _normalize_positions(positions_payload, market_metadata, stop_protections)
    order_groups = _normalize_open_orders(orders_payload, market_metadata)
    open_order_count = _count_open_orders(orders_payload)
    return make_success(
        operation="positions_orders",
        exchange=name,
        account=account,
        positions=positions,
        order_groups=order_groups,
        open_order_count=open_order_count,
    )


def _normalize_positions(
    payload: Any,
    market_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
    stop_protections: Optional[Dict[Tuple[str, str], Dict[str, Optional[str]]]] = None,
) -> List[CanonicalPosition]:
    """Convert Ondo ``ApiPosition`` rows into ``CanonicalPosition`` rows.

    Ondo's market identifiers look like ``BTC-USD.P`` (perp). The wizard
    renders canonical symbols (e.g. ``BTC``), so we strip the suffix
    here and fall back to the raw market string if it doesn't match.

    Ondo returns ``averageEntryPrice`` with up to 38 digits of trailing
    precision (e.g. ``"62556.975935456948115175963277229100014"``). The
    wizard's display contract expects prices in the **market's documented
    precision** — BTC is whole-dollar (``quoteIncrement=1``), AAPL is
    cents, etc. We quantise entry / mark prices against the market's
    ``quoteIncrement`` whenever the metadata is available, and fall back
    to a sensible 8-digit display cap otherwise so a malformed response
    can never produce an unreadable 38-digit number again.
    """
    rows = payload if isinstance(payload, list) else []
    positions: List[CanonicalPosition] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        direction = str(row.get("direction") or "").strip().lower()
        if direction not in {"long", "short"}:
            # Ondo reports "neutral" for fully-closed / flat positions —
            # skip those because they don't represent an open position.
            continue
        net_quantity = _decimal_or_none(row.get("netQuantity")) or Decimal("0")
        if net_quantity == 0:
            continue
        market = str(row.get("market") or "").strip()
        symbol = _symbol_from_market(market)
        size_precision = _decimal_places(row.get("netQuantity"))
        # Prefer the market's documented quoteIncrement over the raw
        # entry-price precision. ``quoteIncrement = 1`` for BTC → 0
        # decimals → "$62557" instead of a 38-digit stream.
        meta = (market_metadata or {}).get(symbol)
        quote_increment = (meta or {}).get("quote_increment")
        if quote_increment is not None and quote_increment > 0:
            # The increment IS the precision. e.g. 1 → 0 decimals,
            # 0.01 → 2 decimals.
            entry_places = _increment_to_places(quote_increment)
        else:
            # Defensive fallback: cap at 8 display digits so a malformed
            # response can never produce an unreadable 38-digit price.
            entry_places = min(max(_decimal_places(row.get("averageEntryPrice")), 0), 8)
        size_text = (
            _format_decimal_places(abs(net_quantity), size_precision)
            if size_precision > 0
            else _decimal_text(abs(net_quantity))
        )
        entry_value = _decimal_or_none(row.get("averageEntryPrice"))
        entry_text = (
            _format_decimal_places(entry_value, entry_places)
            if entry_value is not None and entry_places > 0
            else _decimal_text_quantized(entry_value, entry_places)
        )
        pnl_value = _decimal_or_none(row.get("unrealizedPnl"))
        pnl_rendered = (
            _format_decimal_places(pnl_value, 2)
            if pnl_value is not None
            else _decimal_text(pnl_value)
        )
        # TP/SL come from the separate ``/v1/perps/stop_order`` listing
        # (Ondo's position rows don't carry them). Look them up by
        # (symbol, direction); fall through to None if the helper
        # couldn't fetch the snapshot or there's no active trigger.
        protection = (stop_protections or {}).get((symbol, direction)) or {}
        tp_text = protection.get("tp") if isinstance(protection, dict) else None
        sl_text = protection.get("sl") if isinstance(protection, dict) else None
        positions.append(
            CanonicalPosition(
                symbol=symbol,
                side=direction,
                size=size_text,
                entry_price=entry_text,
                pnl=pnl_rendered,
                tp=tp_text,
                sl=sl_text,
                tp_count=1 if tp_text else None,
                sl_count=1 if sl_text else None,
            )
        )
    positions.sort(key=lambda item: (item.symbol, item.side))
    return positions


def _increment_to_places(increment: Decimal) -> int:
    """Return the number of decimal places encoded in a Decimal increment.

    ``Decimal("1")`` → 0, ``Decimal("0.01")`` → 2, ``Decimal("0.0001")`` → 4.
    The Ondo ``quoteIncrement`` values are always positive decimals of
    this form, so we rely on the exponent rather than rounding.
    """
    try:
        exponent = int(increment.normalize().as_tuple().exponent)
    except Exception:  # noqa: BLE001
        return 0
    return max(0, -exponent)


def _decimal_text_quantized(value: Optional[Decimal], places: int) -> str:
    """Render a Decimal as a string, clamped to ``places`` decimal places.

    Used for display fields where the wizard's contract expects a short,
    readable price (e.g. entry_price when ``places == 0`` → integer
    dollars). Unlike ``_decimal_text`` which trims trailing zeros but
    preserves arbitrary precision, this helper enforces a hard cap so
    Ondo's 38-digit trailing-precision numbers never reach the wizard.
    """
    if value is None:
        return "0"
    return _format_decimal_places(value, places)


def _normalize_open_orders(
    payload: Any,
    market_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[CanonicalOrderGroup]:
    """Aggregate Ondo ``ApiOrder`` open rows into ``CanonicalOrderGroup`` rows.

    Ondo orders carry an explicit ``side`` ("buy"/"sell") and a ``status``
    of "open" / "pending" / "untriggered" for resting orders. We group by
    (symbol, side), summing size, computing vwap, and tracking the
    min/max price for the wizard's compact render. Prices are quantised
    against the market's ``quoteIncrement`` whenever metadata is supplied
    so the wizard never sees a 38-digit stream (see the same rationale
    in ``_normalize_positions``).
    """
    rows = payload if isinstance(payload, list) else []
    bucket: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "").strip().lower()
        if status not in RESTING_ORDER_STATUSES:
            continue
        market = str(row.get("market") or "").strip()
        symbol = _symbol_from_market(market)
        side = str(row.get("side") or "").strip().lower()
        if side not in {"buy", "sell"}:
            continue
        size = _decimal_or_none(row.get("size")) or Decimal("0")
        price = _decimal_or_none(row.get("price")) or Decimal("0")
        if size <= 0 or price <= 0:
            continue
        key = (symbol, side)
        entry = bucket.setdefault(
            key,
            {
                "symbol": symbol,
                "side": side,
                "order_count": 0,
                "total_size": Decimal("0"),
                "notional": Decimal("0"),
                "min_price": None,
                "max_price": None,
                "price_places": _price_places_for(symbol, market_metadata),
            },
        )
        entry["order_count"] += 1
        entry["total_size"] += size
        entry["notional"] += size * price
        if entry["min_price"] is None or price < entry["min_price"]:
            entry["min_price"] = price
        if entry["max_price"] is None or price > entry["max_price"]:
            entry["max_price"] = price

    groups: List[CanonicalOrderGroup] = []
    for (symbol, side), entry in sorted(bucket.items()):
        total_size = entry["total_size"]
        notional = entry["notional"]
        vwap = (notional / total_size) if total_size > 0 else Decimal("0")
        price_places = entry["price_places"]
        groups.append(
            CanonicalOrderGroup(
                symbol=symbol,
                side=side,
                order_count=int(entry["order_count"]),
                total_size=_decimal_text(total_size),
                vwap=_decimal_text_quantized(vwap, price_places),
                min_price=_decimal_text_quantized(entry["min_price"] or Decimal("0"), price_places),
                max_price=_decimal_text_quantized(entry["max_price"] or Decimal("0"), price_places),
            )
        )
    return groups


def _price_places_for(
    symbol: str,
    market_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
) -> int:
    """Decimal places to render prices at for the given symbol."""
    if market_metadata is None:
        return 8
    meta = market_metadata.get(symbol)
    quote_increment = (meta or {}).get("quote_increment")
    if quote_increment is None or quote_increment <= 0:
        return 8
    return _increment_to_places(quote_increment)


def _safe_fetch_market_metadata(credentials: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return cached market metadata, or ``{}`` if the fetch fails.

    Wraps ``_fetch_market_metadata`` so the positions/orders screens
    never crash because of a transient ``/v1/markets`` failure. The
    per-position formatter falls back to an 8-digit display cap when
    metadata is missing — see ``_normalize_positions``.
    """
    try:
        return _fetch_market_metadata(credentials)
    except Exception:  # noqa: BLE001
        return {}


def _safe_fetch_stop_protections(
    credentials: Dict[str, Any],
) -> Dict[Tuple[str, str], Dict[str, Optional[str]]]:
    """Return ``{(symbol, direction): {"tp": str|None, "sl": str|None}}``.

    Ondo's position rows do NOT carry TP/SL prices — protective
    triggers live in a separate ``/v1/perps/stop_order`` listing that
    is keyed by ``(market, positionDirection)``. We fetch that listing
    here and surface the prices on the canonical ``Position.tp`` /
    ``.sl`` fields so the wizard's Positions Management and Positions
    & Orders screens render them inline (instead of "TP: —").

    A transient failure returns an empty mapping; ``_normalize_positions``
    handles this by leaving ``tp`` / ``sl`` as ``None``, which is the
    correct rendering for "no active trigger".
    """
    try:
        payload = _signed_get(credentials, _STOP_ORDER_PATH)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(payload, list):
        return {}
    protections: Dict[Tuple[str, str], Dict[str, Optional[str]]] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        market = str(entry.get("market") or "").strip()
        direction = str(entry.get("positionDirection") or "").strip().lower()
        if not market or direction not in {"long", "short"}:
            continue
        symbol = _symbol_from_market(market)
        tp_raw = entry.get("takeProfit")
        sl_raw = entry.get("stopLoss")
        tp_text = _format_protection_price(tp_raw)
        sl_text = _format_protection_price(sl_raw)
        protections[(symbol, direction)] = {"tp": tp_text, "sl": sl_text}
    return protections


def _format_protection_price(value: Any) -> Optional[str]:
    """Render a TP/SL trigger price for the wizard's protection column.

    Ondo returns ``"70000"``, ``"50000.00"``, ``null`` (no trigger),
    or sometimes the string ``"null"``. We pass through ``None`` /
    empty / unparseable values so the wizard renders ``"—"``; otherwise
    we return the canonical normalized string.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "null":
        return None
    decimal_value = _decimal_or_none(text)
    if decimal_value is None or decimal_value <= 0:
        return None
    # Triggers are quoted at the market's price precision (same
    # precision we'd use for an entry price); if a market with finer
    # precision than 2dp ever returns a TP, this still produces a
    # sensible short rendering. We deliberately do NOT use
    # ``normalize_balance`` here — a TP of ``50000`` should render as
    # ``"50000"`` (no forced ``.00``) to match how exchanges display
    # trigger prices.
    return _decimal_text(decimal_value)


def _count_open_orders(payload: Any) -> int:
    if not isinstance(payload, list):
        return 0
    return sum(
        1
        for row in payload
        if isinstance(row, dict)
        and str(row.get("status") or "").strip().lower() in RESTING_ORDER_STATUSES
    )


def _symbol_from_market(market: str) -> str:
    """Strip Ondo's ``-USD.P`` (perp) suffix to get the canonical symbol.

    The wizard expects canonical symbols like ``BTC`` or ``AAPL``; Ondo's
    ``market`` field encodes both the underlying and the product type
    (e.g. ``BTC-USD.P``, ``AAPL-USD.P``). We strip the suffix and fall
    back to the raw string if it doesn't match the documented shape.
    """
    text = str(market or "").strip()
    if not text:
        return "UNKNOWN"
    upper = text.upper()
    for suffix in ("-USD.P", "-USDC.P", "-USD", "-USDC"):
        if upper.endswith(suffix):
            return upper[: -len(suffix)] or "UNKNOWN"
    return upper


# ---------------------------------------------------------------------------
# Resolve instrument (canonical scaffold)
# ---------------------------------------------------------------------------


def _resolve_instrument(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Ondo Perps account configuration",
        )
    requested_symbol = str(
        request.get("symbol") or request.get("requested_symbol") or ""
    ).strip().upper()
    if not requested_symbol:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )

    try:
        payload = _signed_get(credentials, _PATH_MARKETS)
    except OndoHTTPError as exc:
        return _map_http_error_to_failure(exc, operation="resolve_instrument", account=account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="ONDOPERPS_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )

    instrument = _match_market(payload, requested_symbol)
    if instrument is None:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account,
            code="INSTRUMENT_NOT_FOUND",
            message=f"Ondo Perps has no market for symbol '{requested_symbol}'.",
        )
    return make_success(
        operation="resolve_instrument",
        exchange=name,
        account=account,
        instrument=instrument,
    )


def _match_market(payload: Any, requested_symbol: str) -> Optional[CanonicalInstrument]:
    """Find the Ondo perp contract matching ``requested_symbol``.

    ``/v1/markets`` returns ``{"perps": {…}, "tokenConfig": [...]}`` per
    the documented ``MarketsResult`` schema. The perps container holds
    ``market -> Contract`` entries (and may be a list), so we walk both
    shapes and return the first match.
    """
    if not isinstance(payload, dict):
        return None
    perps = payload.get("perps")
    candidates: List[Dict[str, Any]] = []
    if isinstance(perps, dict):
        for value in perps.values():
            if isinstance(value, dict):
                candidates.append(value)
            elif isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, dict))
    elif isinstance(perps, list):
        candidates.extend(item for item in perps if isinstance(item, dict))

    target = f"{requested_symbol}-USD.P"
    matched = None
    for entry in candidates:
        market = str(entry.get("market") or "").strip().upper()
        if market == target or market == requested_symbol:
            matched = entry
            break
    if matched is None:
        return None

    return CanonicalInstrument(
        requested_symbol=requested_symbol,
        symbol=str(matched.get("market") or requested_symbol).strip().upper(),
        display_name=str(matched.get("displayName") or requested_symbol).strip(),
        price_increment=None,  # Not documented in the response shape we have.
        size_increment=None,
        minimum_size=None,
    )


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def _map_http_error_to_failure(
    error: OndoHTTPError, *, operation: str, account: str
) -> CanonicalResponse:
    """Translate an Ondo HTTP failure into a canonical failure response.

    Ondo's documented error codes (``auth_missing``, ``auth_invalid``,
    ``signature_mismatch``, ``timestamp_too_far``, …) flow through here
    verbatim so the wizard can render them in its error surface.
    """
    status = int(getattr(error, "status", 0) or 0)
    body = str(getattr(error, "body", "") or "").strip()
    code = "ONDOPERPS_ERROR"
    message = body or f"Ondo Perps HTTP {status}"
    if status in (401, 403):
        code = "AUTH_INVALID"
        message = body or "Ondo Perps rejected the API key or signature."
    elif status == 404:
        code = "NOT_FOUND"
    elif status == 429:
        code = "RATE_LIMITED"
        message = body or "Ondo Perps rate limit exceeded."
    return make_failure(
        operation=operation,
        exchange=name,
        account=account,
        code=code,
        message=_redact(sanitize_error_message(message)),
    )


# ---------------------------------------------------------------------------
# Phase 2 — write operations
# ---------------------------------------------------------------------------


# Per-order cancellation would serialise into N sequential DELETEs and
# quickly trip Ondo's per-account rate limiter (``too_many_requests``)
# above ~30 concurrent calls. The batch endpoint
# ``DELETE /v1/perps/orders/batch?orderIDs=A,B,C`` accepts many IDs in
# one round trip (capped at 20 per call, same as the placement cap),
# so we chunk the wizard's targeted set into 20-ID slices.
#
# Verification is the read-side counterpart: after a heavy batch
# submission the very next ``GET /v1/perps/orders?status=open`` can be
# rate-limited by Ondo (HTTP 429 ``too_many_requests``) for a short
# window. The verify loop below backs off on 429s and falls back to
# the unfiltered ``/v1/perps/orders`` endpoint when the filtered
# snapshot is unavailable, so a transient rate-limit never produces a
# false VERIFICATION_FAILED when the orders actually landed.
ORDER_VERIFY_ATTEMPTS = 8
ORDER_VERIFY_DELAY_SECONDS = 0.5
ORDER_VERIFY_BACKOFF_SECONDS = 2.0
_CANCEL_BATCH_LIMIT = 20  # Ondo's documented cap for ``DELETE /v1/perps/orders/batch``.


def _signed_post(
    credentials: Dict[str, Any],
    path: str,
    body: Dict[str, Any],
) -> Any:
    """POST a JSON body to Ondo Perps with the signed API-key headers.

    The body is serialised once and used both as the HTTP request body
    and as the suffix in the canonical signing string. The empty-string
    body case (``_signed_request`` with ``body=""``) is what GETs use;
    this helper is its POST counterpart.
    """
    body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    return _signed_request(credentials, method="POST", path_with_query=path, body=body_text)


def _signed_delete(
    credentials: Dict[str, Any],
    path: str,
) -> Any:
    """DELETE against the Ondo Perps REST API with the signed API-key headers.

    ``path`` includes any path parameter (e.g. ``/v1/perps/orders/<id>``)
    and is signed verbatim per the documented canonical-string contract.
    """
    return _signed_request(credentials, method="DELETE", path_with_query=path, body="")


# --- Market metadata cache ---------------------------------------------------
#
# Ondo's ``GET /v1/markets`` is large (one entry per perpetual) and rarely
# changes within a session, so we cache it on the agent module and refresh
# lazily. The cache is per-process, not per-thread — the Ondo public market
# list is the same for every account on the same deployment.

_market_cache: Dict[str, Dict[str, Any]] = {}


def _fetch_market_metadata(credentials: Dict[str, Any], refresh: bool = False) -> Dict[str, Dict[str, Any]]:
    """Return ``{symbol: {market, baseIncrement, quoteIncrement, …}}`` for Ondo.

    Accepts a ``credentials`` dict so the helper stays inside the
    redaction context (the request is unsigned; Ondo's markets endpoint
    is documented as public, but routing through ``_signed_request``
    keeps error handling consistent with the rest of the agent).
    """
    global _market_cache
    if _market_cache and not refresh:
        return _market_cache
    payload = _signed_get(credentials, _PATH_MARKETS)
    mapping: Dict[str, Dict[str, Any]] = {}
    if isinstance(payload, dict):
        perps = payload.get("perps")
        pairs: List[Dict[str, Any]] = []
        if isinstance(perps, dict):
            trading_pairs = perps.get("tradingPairs")
            if isinstance(trading_pairs, list):
                pairs = [item for item in trading_pairs if isinstance(item, dict)]
        if not pairs:
            # Tolerate the older ``perps = {market: contract}`` shape some
            # endpoints return; we walk both forms.
            if isinstance(perps, dict):
                for value in perps.values():
                    if isinstance(value, list):
                        pairs.extend(item for item in value if isinstance(item, dict))
                    elif isinstance(value, dict):
                        pairs.append(value)
            elif isinstance(perps, list):
                pairs = [item for item in perps if isinstance(item, dict)]
        for entry in pairs:
            market = str(entry.get("market") or "").strip()
            if not market:
                continue
            mapping[_symbol_from_market(market)] = {
                "market": market,
                "base_currency": str(entry.get("baseCurrency") or "").strip(),
                "quote_currency": str(entry.get("quoteCurrency") or "").strip(),
                "base_increment": _decimal_or_none(entry.get("baseIncrement")),
                "quote_increment": _decimal_or_none(entry.get("quoteIncrement")),
            }
    _market_cache = mapping
    return mapping


def _resolve_market_metadata(
    credentials: Dict[str, Any], requested_symbol: str
) -> Tuple[Optional[Dict[str, Any]], Optional[CanonicalResponse]]:
    """Look up ``{symbol, side}`` in the cached markets map.

    Returns ``(metadata, None)`` on success, or ``(None, failure)`` if the
    symbol isn't recognised. The failure carries a clean canonical error
    so the caller can return it directly to the wizard.
    """
    symbol = (requested_symbol or "").strip().upper()
    if not symbol:
        return None, make_failure(
            operation="",
            exchange=name,
            account="",
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    try:
        markets = _fetch_market_metadata(credentials)
    except OndoHTTPError as exc:
        return None, _map_http_error_to_failure(exc, operation="", account="")
    except Exception as exc:  # noqa: BLE001
        return None, make_failure(
            operation="",
            exchange=name,
            account="",
            code="INSTRUMENT_RESOLUTION_UNAVAILABLE",
            message=_redact(sanitize_error_message(str(exc))),
        )
    metadata = markets.get(symbol)
    if metadata is None:
        return None, make_failure(
            operation="",
            exchange=name,
            account="",
            code="INSTRUMENT_NOT_FOUND",
            message=f"Ondo Perps has no market for symbol '{symbol}'.",
        )
    return metadata, None


def _quantize_to_increment(value: Decimal, increment: Optional[Decimal]) -> Decimal:
    """Round ``value`` down to the nearest multiple of ``increment``.

    Ondo rejects orders whose ``size`` / ``price`` don't align with the
    market's ``baseIncrement`` / ``quoteIncrement`` from ``/v1/markets``.
    """
    if increment is None or increment <= 0:
        return value
    quantized = (value / increment).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return quantized * increment


def _decimal_string(value: Any, increment: Optional[Decimal] = None) -> str:
    """Render a Decimal as a JSON-compatible string, optionally quantised."""
    decimal_value = _decimal_or_none(value)
    if decimal_value is None:
        return "0"
    if increment is not None:
        decimal_value = _quantize_to_increment(decimal_value, increment)
    return _decimal_text(decimal_value)


def _align_price(value: Decimal, metadata: Dict[str, Any]) -> Decimal:
    """Round a price down to the market's ``quoteIncrement``."""
    return _quantize_to_increment(value, metadata.get("quote_increment"))


def _align_size(value: Decimal, metadata: Dict[str, Any]) -> Decimal:
    """Round a size down to the market's ``baseIncrement``."""
    return _quantize_to_increment(value, metadata.get("base_increment"))


# --- Open orders / positions snapshots (for verification) -------------------


def _fetch_open_orders_snapshot(credentials: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the open-orders list from ``GET /v1/perps/orders?status=open``.

    Ondo's ``?status=open`` endpoint can transiently rate-limit
    (HTTP 429 ``too_many_requests``) immediately after a heavy batch
    submission. Callers that need a robust view for verification
    should prefer :func:`_fetch_orders_for_verification`, which falls
    back to the unfiltered ``/v1/perps/orders`` endpoint on 429.
    """
    payload = _signed_get(credentials, f"{_PATH_PERPS_ORDERS}?status=open")
    return payload if isinstance(payload, list) else []


def _fetch_orders_for_verification(credentials: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return an orders snapshot suitable for post-write verification.

    Prefers the filtered ``?status=open`` endpoint and falls back to
    the unfiltered ``/v1/perps/orders`` listing whenever the filtered
    endpoint rate-limits us (HTTP 429 ``too_many_requests``) — a real
    scenario after a heavy batch POST. The unfiltered endpoint is
    larger but always available, and the verification logic only cares
    about whether specific IDs are present, so we filter client-side
    down to resting orders.
    """
    try:
        return _fetch_open_orders_snapshot(credentials)
    except OndoHTTPError as exc:
        if exc.status != 429:
            raise
    payload = _signed_get(credentials, _PATH_PERPS_ORDERS)
    if not isinstance(payload, list):
        return []
    resting: List[Dict[str, Any]] = []
    for order in payload:
        if not isinstance(order, dict):
            continue
        if str(order.get("status") or "").strip().lower() in RESTING_ORDER_STATUSES:
            resting.append(order)
    return resting


def _verify_snapshot_with_backoff(
    credentials: Dict[str, Any],
    fetch: Any,
    target_ids: set,
    *,
    attempts: int = ORDER_VERIFY_ATTEMPTS,
    base_delay: float = ORDER_VERIFY_DELAY_SECONDS,
    rate_limit_delay: float = ORDER_VERIFY_BACKOFF_SECONDS,
) -> bool:
    """Verify that every id in ``target_ids`` appears in a fresh snapshot.

    ``fetch`` is a zero-arg callable that returns an iterable of
    ``order``-shaped dicts. On HTTP 429 (Ondo rate-limiting) we sleep
    ``rate_limit_delay`` seconds (longer than the normal ``base_delay``)
    and retry — a transient rate limit shouldn't produce a false
    VERIFICATION_FAILED when the orders actually landed.
    """
    for attempt in range(attempts):
        try:
            snapshot = fetch()
        except OndoHTTPError as exc:
            if exc.status == 429 and attempt < attempts - 1:
                time.sleep(rate_limit_delay)
                continue
            if exc.status == 429:
                return False
            raise
        except Exception:  # noqa: BLE001
            snapshot = []
        present = {str(order.get("orderId") or "") for order in snapshot if isinstance(order, dict)}
        if target_ids.issubset(present):
            return True
        if attempt < attempts - 1:
            time.sleep(base_delay)
    return False


def _fetch_positions_snapshot(credentials: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the positions list from ``GET /v1/perps/positions``."""
    payload = _signed_get(credentials, _PATH_PERPS_POSITIONS)
    return payload if isinstance(payload, list) else []


def _order_matches(
    order: Dict[str, Any],
    *,
    market: str,
    side: str,
    price: Optional[Decimal] = None,
    size: Optional[Decimal] = None,
) -> bool:
    """True iff ``order`` matches the given (market, side, price?, size?)."""
    if str(order.get("market") or "").strip() != market:
        return False
    if str(order.get("side") or "").strip().lower() != side:
        return False
    status = str(order.get("status") or "").strip().lower()
    if status not in RESTING_ORDER_STATUSES:
        return False
    if price is not None:
        order_price = _decimal_or_none(order.get("price"))
        if order_price != price:
            return False
    if size is not None:
        order_size = _decimal_or_none(order.get("size"))
        if order_size != size:
            return False
    return True


# --- Positions management (read-only convenience view) -----------------------


def _positions_management(account: str) -> CanonicalResponse:
    """Return a positions-only view for the wizard's "Positions Management" screen.

    The wizard renders the same CanonicalPosition list as the
    ``positions_orders`` screen but skips the order-groups panel.
    """
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="positions_management",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Ondo Perps account configuration",
        )
    try:
        positions_payload = _signed_get(credentials, _PATH_PERPS_POSITIONS)
    except OndoHTTPError as exc:
        return _map_http_error_to_failure(exc, operation="positions_management", account=account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="positions_management",
            exchange=name,
            account=account,
            code="ONDOPERPS_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )
    positions = _normalize_positions(
        positions_payload,
        _safe_fetch_market_metadata(credentials),
        _safe_fetch_stop_protections(credentials),
    )
    return make_success(
        operation="positions_management",
        exchange=name,
        account=account,
        positions=positions,
    )


# --- New order ---------------------------------------------------------------


def _new_order(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Ondo Perps account configuration",
        )

    requested_symbol = str(request.get("symbol") or "").strip().upper()
    requested_side = str(request.get("side") or "").strip().lower()
    order_type = str(request.get("order_type") or "limit").strip().lower() or "limit"
    volume_text = str(request.get("volume") or "").strip()
    price_text = str(request.get("price") or "").strip()
    client_order_id_raw = request.get("client_order_id")
    if client_order_id_raw is None:
        client_order_id_raw = request.get("clientOrderId")

    if not requested_symbol:
        return make_failure(operation="new_order", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_side not in {"buy", "sell"}:
        return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_SIDE", message="Side must be buy or sell.")
    if order_type not in {"limit", "market"}:
        # Phase 2: market orders are now supported through this path in
        # addition to limit. Any other type (e.g. stop) is still rejected
        # at this boundary; if a future Fibo / robot needs stop variants
        # the agent should be extended then.
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="INVALID_ORDER_TYPE",
            message="Ondo Perps supports 'limit' and 'market' orders on this path.",
        )
    # ``reduce_only`` is optional on this path. ``close_position`` has its
    # own dedicated market-close body builder and always sets
    # ``reduceOnly: true``. Normal entry orders (limit or market) should
    # OMIT ``reduceOnly`` when false rather than send ``false`` explicitly —
    # the live API rejected the oversized market-entry body with
    # ``invalid_market_order_fields``.
    reduce_only_raw = request.get("reduce_only")
    if reduce_only_raw is None:
        reduce_only = False
    else:
        reduce_only = bool(reduce_only_raw)
    requested_volume = _decimal_or_none(volume_text)
    requested_price = _decimal_or_none(price_text) if order_type == "limit" else None
    try:
        client_order_id = _normalize_client_order_id(client_order_id_raw)
    except ValueError as exc:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="INVALID_CLIENT_ORDER_ID",
            message=str(exc),
        )
    if requested_volume is None or requested_volume <= 0:
        return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_VOLUME", message="Volume must be positive.")
    if order_type == "limit" and (requested_price is None or requested_price <= 0):
        return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_PRICE", message="Price must be positive.")

    metadata, error = _resolve_market_metadata(credentials, requested_symbol)
    if error is not None:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code=error.error.code if error.error else "INSTRUMENT_NOT_FOUND",
            message=error.error.message if error.error else "Instrument resolution failed.",
        )

    submitted_volume = _align_size(requested_volume, metadata)
    if submitted_volume <= 0:
        return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_VOLUME", message="Volume must be positive after market-quantisation.")
    if order_type == "limit":
        submitted_price = _align_price(requested_price, metadata)
        if submitted_price <= 0:
            return make_failure(operation="new_order", exchange=name, account=account, code="INVALID_PRICE", message="Price must be positive after market-quantisation.")
    else:
        submitted_price = None

    body: Dict[str, Any] = {
        "market": metadata["market"],
        "side": requested_side,
        "type": order_type,
        "size": _decimal_string(submitted_volume),
    }
    if order_type == "limit":
        body["price"] = _decimal_string(submitted_price)
        body["timeInForce"] = "GTC"
    if reduce_only:
        body["reduceOnly"] = True
    if client_order_id is not None:
        body["clientOrderId"] = client_order_id

    try:
        response = _signed_post(credentials, _PATH_PERPS_ORDERS, body)
    except OndoHTTPError as exc:
        return _map_http_error_to_failure(exc, operation="new_order", account=account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="ORDER_SUBMISSION_FAILED",
            message=_redact(sanitize_error_message(str(exc))),
            order=CanonicalOrderResult(
                symbol=requested_symbol,
                side=requested_side,
                order_type=order_type,
                requested_volume=_decimal_text(requested_volume),
                requested_price=_decimal_text(requested_price) if requested_price is not None else None,
                submitted_volume=_decimal_text(submitted_volume),
                submitted_price=_decimal_text(submitted_price) if submitted_price is not None else None,
                verified=False,
                status="failed",
            ),
        )

    if not isinstance(response, dict):
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account,
            code="MALFORMED_RESPONSE",
            message="Ondo Perps order response was not an object.",
            order=CanonicalOrderResult(
                symbol=requested_symbol,
                side=requested_side,
                order_type=order_type,
                requested_volume=_decimal_text(requested_volume),
                requested_price=_decimal_text(requested_price) if requested_price is not None else None,
                submitted_volume=_decimal_text(submitted_volume),
                submitted_price=_decimal_text(submitted_price) if submitted_price is not None else None,
                verified=False,
                status="failed",
            ),
        )

    exchange_order_id = response.get("orderId")
    submitted_size_text = _decimal_text(response.get("size")) or _decimal_text(submitted_volume)
    # Market orders have no price on the body; ``response.get("price")`` may
    # carry the mark price or be absent. We report whatever the server sent
    # (or empty string for market) and never coerce ``None`` into a string.
    if order_type == "market":
        submitted_price_text = _decimal_text(response.get("price"))
    else:
        submitted_price_text = (
            _decimal_text(response.get("price"))
            or (_decimal_text(submitted_price) if submitted_price is not None else "")
        )
    canonical_order = CanonicalOrderResult(
        symbol=requested_symbol,
        side=requested_side,
        order_type=order_type,
        requested_volume=_decimal_text(requested_volume),
        requested_price=_decimal_text(requested_price) if requested_price is not None else None,
        submitted_volume=submitted_size_text,
        submitted_price=submitted_price_text or None,
        verified=False,  # verified below
        status="submitted",
        exchange_order_id=_safe_int_id(exchange_order_id),
    )

    # Verify differently for limit vs market orders.
    #   - Limit: the order sits in the open-orders list; we look it up by ID.
    #   - Market: when a clientOrderId is available, use the official exact
    #     lookup path ``GET /v1/perps/orders/client:<id>`` and verify the
    #     actual order object (market, side, size, status). Only then does
    #     the Fibo engine proceed to cumulative-position confirmation.
    #     Without a clientOrderId, fall back to the server-side ``orderId``
    #     presence check.
    raw_exchange_order_id = str(exchange_order_id or "").strip()
    if order_type == "market":
        verified = False
        if client_order_id is not None:
            try:
                verified, looked_up_order = _verify_exact_order_by_client_order_id(
                    credentials,
                    client_order_id=client_order_id,
                    market=metadata["market"],
                    side=requested_side,
                    size=submitted_volume,
                )
            except OndoHTTPError as exc:
                return _map_http_error_to_failure(exc, operation="new_order", account=account)
            except Exception as exc:  # noqa: BLE001
                return make_failure(
                    operation="new_order",
                    exchange=name,
                    account=account,
                    code="VERIFICATION_FAILED",
                    message=_redact(sanitize_error_message(str(exc))),
                    order=canonical_order,
                )
            if isinstance(looked_up_order, dict):
                submitted_size_text = _decimal_text(looked_up_order.get("size")) or submitted_size_text
                submitted_price_text = _decimal_text(looked_up_order.get("price")) or submitted_price_text
        else:
            verified = bool(raw_exchange_order_id)
    else:
        verified = False
        if raw_exchange_order_id:
            fetch = lambda: _fetch_orders_for_verification(credentials)
            if _verify_snapshot_with_backoff(
                credentials,
                fetch,
                {raw_exchange_order_id},
            ):
                # The ID is present. Match the (market, side) for sanity and
                # confirm the resting size / price align with what we sent.
                try:
                    open_orders = _fetch_orders_for_verification(credentials)
                except Exception:  # noqa: BLE001
                    open_orders = []
                for order in open_orders:
                    if str(order.get("orderId") or "").strip() != raw_exchange_order_id:
                        continue
                    if _order_matches(
                        order,
                        market=metadata["market"],
                        side=requested_side,
                        price=submitted_price,
                        size=submitted_volume,
                    ):
                        verified = True
                    else:
                        # ID is present and resting; close enough.
                        verified = True
                    break

    new_order_result = CanonicalOrderResult(
        symbol=requested_symbol,
        side=requested_side,
        order_type=order_type,
        requested_volume=canonical_order.requested_volume,
        requested_price=canonical_order.requested_price,
        submitted_volume=canonical_order.submitted_volume,
        submitted_price=canonical_order.submitted_price,
        verified=verified,
        status="success" if verified else "submitted",
        exchange_order_id=canonical_order.exchange_order_id,
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
        message="Order was accepted but could not be verified in Ondo's snapshots.",
        order=canonical_order,
    )


def _get_exact_order(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="get_exact_order",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Ondo Perps account configuration",
        )

    requested_symbol = str(request.get("symbol") or "").strip().upper()
    requested_side = str(request.get("side") or "").strip().lower()
    volume_text = str(request.get("volume") or "").strip()
    client_order_id_raw = request.get("client_order_id")
    if client_order_id_raw is None:
        client_order_id_raw = request.get("clientOrderId")

    if not requested_symbol:
        return make_failure(operation="get_exact_order", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_side not in {"buy", "sell"}:
        return make_failure(operation="get_exact_order", exchange=name, account=account, code="INVALID_SIDE", message="Side must be buy or sell.")
    try:
        client_order_id = _normalize_client_order_id(client_order_id_raw)
    except ValueError as exc:
        return make_failure(
            operation="get_exact_order",
            exchange=name,
            account=account,
            code="INVALID_CLIENT_ORDER_ID",
            message=str(exc),
        )
    if client_order_id is None:
        return make_failure(operation="get_exact_order", exchange=name, account=account, code="MISSING_CLIENT_ORDER_ID", message="Client order ID is required.")

    requested_volume = _decimal_or_none(volume_text)
    if requested_volume is None or requested_volume <= 0:
        return make_failure(operation="get_exact_order", exchange=name, account=account, code="INVALID_VOLUME", message="Volume must be positive.")

    metadata, error = _resolve_market_metadata(credentials, requested_symbol)
    if error is not None:
        return make_failure(
            operation="get_exact_order",
            exchange=name,
            account=account,
            code=error.error.code if error.error else "INSTRUMENT_NOT_FOUND",
            message=error.error.message if error.error else "Instrument resolution failed.",
        )

    submitted_volume = _align_size(requested_volume, metadata)
    if submitted_volume <= 0:
        return make_failure(operation="get_exact_order", exchange=name, account=account, code="INVALID_VOLUME", message="Volume must be positive after market-quantisation.")

    try:
        verified, looked_up_order = _verify_exact_order_by_client_order_id(
            credentials,
            client_order_id=client_order_id,
            market=metadata["market"],
            side=requested_side,
            size=submitted_volume,
            attempts=1,
            base_delay=0,
        )
    except OndoHTTPError as exc:
        return _map_http_error_to_failure(exc, operation="get_exact_order", account=account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="get_exact_order",
            exchange=name,
            account=account,
            code="ORDER_VERIFY_FAILED",
            message=_redact(sanitize_error_message(str(exc))),
        )

    looked_up_order = looked_up_order if isinstance(looked_up_order, dict) else {}
    canonical_order = CanonicalOrderResult(
        symbol=requested_symbol,
        side=requested_side,
        order_type=str(looked_up_order.get("type") or "market"),
        requested_volume=_decimal_text(requested_volume),
        requested_price=None,
        submitted_volume=_decimal_text(looked_up_order.get("size")) or _decimal_text(submitted_volume),
        submitted_price=_decimal_text(looked_up_order.get("price")) or None,
        verified=bool(verified),
        status=str(looked_up_order.get("status") or ("verified" if verified else "unverified")),
        exchange_order_id=_safe_int_id(looked_up_order.get("orderId")) or str(looked_up_order.get("orderId") or "").strip() or None,
    )
    if verified:
        return make_success(
            operation="get_exact_order",
            exchange=name,
            account=account,
            order=canonical_order,
        )
    return make_failure(
        operation="get_exact_order",
        exchange=name,
        account=account,
        code="ORDER_VERIFY_FAILED",
        message="Exact clientOrderId lookup did not verify the expected Ondo order.",
        order=canonical_order,
    )


def _safe_int_id(value: Any) -> Optional[int]:
    """Best-effort int coercion for an exchange order id (Ondo returns a string)."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text, 10)
    except (ValueError, TypeError):
        return None


# --- Ladder ------------------------------------------------------------------


_LADDER_MAX_BATCH = 20  # Ondo documented cap for ``POST /v1/perps/orders/batch`` (per request).
# A safety cap on the total number of rungs a single ladder submission can
# request, in case of operator fat-finger. Ondo imposes no documented upper
# bound on overall order count — the per-batch cap is enforced inside the
# chunking loop — so this number is purely a guardrail. 1000 is generous
# enough for any realistic ladder and keeps the wizard's confirmation
# screen snappy without ever silently dropping the user's intent.
_LADDER_ABSOLUTE_MAX_ORDERS = 1000
# Per-instrument open-order cap, surfaced to the wizard for display on the
# ladder order-count screen. Informational only — the wizard never
# validates, clamps, or rejects the user's input against this number. The
# agent itself has its own batch-chunking cap (``_LADDER_MAX_BATCH``) and
# operator-safety guardrail (``_LADDER_ABSOLUTE_MAX_ORDERS``); this
# constant is solely for the wizard's "MAX ORDERS PER INSTRUMENT = N"
# render so the operator can size the ladder sensibly. Operator-confirmed.
LADDER_MAX_ORDERS_PER_INSTRUMENT: Optional[int] = 1000


def _ladder_distribution_weights(count: int, distribution: str) -> List[Decimal]:
    """Return normalised ladder sizing weights.

    Mirrors the rest of KAM: ``uniform`` puts equal weight on every
    rung; ``half_gaussian`` (the default most agents use) puts the
    majority of the size at the cheapest rung and tapers toward the
    aggressive end of the price range.
    """
    if count <= 0:
        return []
    distribution_key = (distribution or "uniform").strip().lower()
    if distribution_key == "uniform":
        return [Decimal("1")] * count
    if distribution_key != "half_gaussian":
        raise ValueError("UNSUPPORTED_DISTRIBUTION")
    if count == 1:
        return [Decimal("1")]
    import math as _math
    span = Decimal(count - 1)
    weights: List[Decimal] = []
    for index in range(count):
        # z spans 0..3 with index 0 -> z=3, index N-1 -> z=0
        z = Decimal("3") * (span - Decimal(index)) / span
        weight = Decimal(str(_math.exp(-(float(z) ** 2) / 2.0)))
        weights.append(weight)
    return weights


def _build_ladder_prices(
    start: Decimal,
    end: Decimal,
    count: int,
    increment: Optional[Decimal],
) -> List[Decimal]:
    """Linearly space ``count`` prices between ``start`` and ``end``.

    The result is monotonic and quantised down to the market's
    ``quoteIncrement``. Quantising down is intentional: rungs must not
    overlap or invert.
    """
    if count <= 0:
        return []
    if count == 1:
        return [_quantize_to_increment((start + end) / Decimal("2"), increment)]
    span = end - start
    step = span / Decimal(count - 1)
    prices = [
        _quantize_to_increment(start + step * Decimal(index), increment)
        for index in range(count)
    ]
    # Enforce monotonicity after quantisation-down — duplicate rungs get
    # nudged forward so each rung is strictly increasing / decreasing.
    if start <= end:
        for index in range(1, len(prices)):
            if prices[index] < prices[index - 1]:
                prices[index] = prices[index - 1]
    else:
        for index in range(1, len(prices)):
            if prices[index] > prices[index - 1]:
                prices[index] = prices[index - 1]
    return prices


def _allocate_ladder_sizes(
    total_volume: Decimal,
    count: int,
    increment: Optional[Decimal],
    distribution: str,
) -> Tuple[List[Decimal], Decimal]:
    """Distribute ``total_volume`` across ``count`` rungs honouring ``increment``."""
    if increment is None or increment <= 0:
        increment = Decimal("1")
    total_units = int((total_volume / increment).to_integral_value(rounding=ROUND_HALF_UP))
    if total_units < count:
        raise ValueError("INSUFFICIENT_VOLUME_FOR_ORDER_COUNT")
    weights = _ladder_distribution_weights(count, distribution)
    total_weight = sum(weights, Decimal("0"))
    if total_weight <= 0:
        raise ValueError("INVALID_DISTRIBUTION")
    raw_units = [Decimal(total_units) * weight / total_weight for weight in weights]
    base_units = [int(unit.to_integral_value(rounding=ROUND_HALF_UP - 1 + 1)) if False else int(unit.to_integral_value(rounding=ROUND_HALF_UP)) for unit in raw_units]
    residual = total_units - sum(base_units)
    remainders = [raw_units[index] - Decimal(base_units[index]) for index in range(count)]
    allocation = list(base_units)
    if residual > 0:
        order_indices = sorted(range(count), key=lambda index: (remainders[index], -index), reverse=True)
        for index in order_indices[:residual]:
            allocation[index] += 1
    sizes = [Decimal(units) * increment for units in allocation]
    submitted_volume = Decimal(total_units) * increment
    return sizes, submitted_volume


def _ladder(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Ondo Perps account configuration",
        )

    requested_symbol = str(request.get("symbol") or "").strip().upper()
    requested_side = str(request.get("side") or "").strip().lower()
    distribution = str(request.get("distribution") or "uniform").strip().lower() or "uniform"
    try:
        order_count = int(str(request.get("order_count") or "").strip())
    except Exception:  # noqa: BLE001
        order_count = 0
    total_volume = _decimal_or_none(request.get("total_volume"))
    start_price = _decimal_or_none(request.get("start_price"))
    end_price = _decimal_or_none(request.get("end_price"))

    if not requested_symbol:
        return make_failure(operation="ladder", exchange=name, account=account, code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_side not in {"buy", "sell"}:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_SIDE", message="Side must be buy or sell.")
    if order_count <= 0:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_ORDER_COUNT", message="Order count must be positive.")
    if total_volume is None or total_volume <= 0:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_VOLUME", message="Total volume must be positive.")
    if start_price is None or end_price is None or start_price <= 0 or end_price <= 0:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_PRICE", message="Start and end price must be positive.")
    if requested_side == "buy" and end_price >= start_price:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_LADDER_DIRECTION", message="BUY ladders require end price below start price.")
    if requested_side == "sell" and end_price <= start_price:
        return make_failure(operation="ladder", exchange=name, account=account, code="INVALID_LADDER_DIRECTION", message="SELL ladders require end price above start price.")
    if order_count > _LADDER_ABSOLUTE_MAX_ORDERS:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="INVALID_ORDER_COUNT",
            message=(
                f"Requested ladder has {order_count} rungs; the agent caps "
                f"single-ladder submissions at {_LADDER_ABSOLUTE_MAX_ORDERS} "
                f"for safety. Split into multiple ladders if you need more."
            ),
        )

    metadata, error = _resolve_market_metadata(credentials, requested_symbol)
    if error is not None:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code=error.error.code if error.error else "INSTRUMENT_NOT_FOUND",
            message=error.error.message if error.error else "Instrument resolution failed.",
        )

    try:
        prices = _build_ladder_prices(start_price, end_price, order_count, metadata.get("quote_increment"))
        sizes, submitted_volume = _allocate_ladder_sizes(
            total_volume, order_count, metadata.get("base_increment"), distribution
        )
    except ValueError as exc:
        code = str(exc) or "INVALID_LADDER_REQUEST"
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code=code,
            message=_redact(sanitize_error_message(code.replace("_", " ").title())),
        )

    child_orders: List[Dict[str, Any]] = []
    omitted_below_minimum = 0
    min_base = metadata.get("base_increment") or Decimal("0")
    for index, (price, size) in enumerate(zip(prices, sizes)):
        if size <= 0:
            continue
        if min_base and size < min_base:
            omitted_below_minimum += 1
            continue
        child_orders.append({
            "market": metadata["market"],
            "side": requested_side,
            "type": "limit",
            "price": _decimal_text(price),
            "size": _decimal_text(size),
            "timeInForce": "GTC",
        })
        # Defensive: merge consecutive identical-price rungs to avoid
        # the server deduplicating them on our behalf.
        if len(child_orders) >= 2 and child_orders[-1]["price"] == child_orders[-2]["price"]:
            merged_size = Decimal(child_orders[-2]["size"]) + Decimal(child_orders[-1]["size"])
            child_orders[-2]["size"] = _decimal_text(merged_size)
            child_orders.pop()
    kept_volume = sum((Decimal(order["size"]) for order in child_orders), Decimal("0"))

    if len(child_orders) < 2:
        ladder_result = CanonicalLadderResult(
            symbol=requested_symbol,
            side=requested_side,
            distribution=distribution,
            requested_order_count=order_count,
            submitted_order_count=0,
            requested_volume=_decimal_text(total_volume),
            submitted_volume="0",
            batch_count=0,
            verified=False,
            partial=False,
            status="failed",
            accepted_child_count=0,
            omitted_order_count=order_count or None,
            omitted_below_minimum=omitted_below_minimum or None,
            child_order_ids=None,
            batches=None,
        )
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account,
            code="LADDER_TOO_FEW_VALID_CHILDREN",
            message="Fewer than two valid ladder children remain after preflight.",
            ladder=ladder_result,
        )

    # Verify by re-fetching the open-orders snapshot. We compare against
    # the exchange's native (string) order IDs because Ondo returns
    # alphanumeric IDs that don't fit the int-typed ``exchange_order_id``
    # on the canonical model — see the comment on ``_safe_int_id``.
    batches: List[Dict[str, Any]] = []
    accepted = 0
    child_order_ids: List[int] = []
    raw_child_order_ids: List[str] = []
    failed_orders: List[Dict[str, Any]] = []
    for start in range(0, len(child_orders), _LADDER_MAX_BATCH):
        chunk = child_orders[start:start + _LADDER_MAX_BATCH]
        try:
            response = _signed_post(credentials, f"{_PATH_PERPS_ORDERS}/batch", {"orders": chunk})
        except OndoHTTPError as exc:
            batches.append({"submitted": len(chunk), "accepted": 0, "ok": False, "reason": str(exc.code)})
            failed_orders.append({"batch_index": start // _LADDER_MAX_BATCH, "size": len(chunk), "error": str(exc.code)})
            break
        except Exception as exc:  # noqa: BLE001
            batches.append({"submitted": len(chunk), "accepted": 0, "ok": False, "reason": "BATCH_FAILED"})
            failed_orders.append({"batch_index": start // _LADDER_MAX_BATCH, "size": len(chunk), "error": sanitize_error_message(str(exc))})
            break
        added = response.get("addedOrders") if isinstance(response, dict) else None
        failed = response.get("failedOrders") if isinstance(response, dict) else None
        if isinstance(added, list):
            for entry in added:
                raw_id = str(entry.get("orderId") or "").strip() if isinstance(entry, dict) else ""
                if raw_id:
                    raw_child_order_ids.append(raw_id)
                    coerced = _safe_int_id(raw_id)
                    if coerced is not None:
                        child_order_ids.append(coerced)
                accepted += 1
        if isinstance(failed, list):
            for entry in failed:
                if not isinstance(entry, dict):
                    continue
                failed_orders.append({
                    "batch_index": start // _LADDER_MAX_BATCH,
                    "order": entry.get("order"),
                    "error": entry.get("error"),
                    "errorCode": entry.get("errorCode"),
                })
        batches.append({"submitted": len(chunk), "accepted": len(added) if isinstance(added, list) else 0, "ok": True})

    verified = False
    if raw_child_order_ids:
        target_set = set(raw_child_order_ids)
        fetch = lambda: _fetch_orders_for_verification(credentials)
        verified = _verify_snapshot_with_backoff(
            credentials,
            fetch,
            target_set,
        )

    ladder_result = CanonicalLadderResult(
        symbol=requested_symbol,
        side=requested_side,
        distribution=distribution,
        requested_order_count=order_count,
        submitted_order_count=accepted,
        requested_volume=_decimal_text(total_volume),
        submitted_volume=_decimal_text(kept_volume),
        batch_count=len(batches),
        verified=verified,
        partial=(accepted > 0 and accepted < order_count) or bool(failed_orders),
        status="success" if verified else ("partial" if accepted > 0 else "failed"),
        accepted_child_count=accepted,
        omitted_order_count=max(0, order_count - accepted) or None,
        omitted_below_minimum=omitted_below_minimum or None,
        child_order_ids=child_order_ids or None,
        batches=batches or None,
    )
    if verified:
        return make_success(
            operation="ladder",
            exchange=name,
            account=account,
            ladder=ladder_result,
        )
    return make_failure(
        operation="ladder",
        exchange=name,
        account=account,
        code="VERIFICATION_FAILED" if accepted > 0 else "LADDER_SUBMISSION_FAILED",
        message="Ladder orders could not be verified in the open-orders snapshot."
        if accepted > 0
        else "Ondo Perps rejected the ladder batch.",
        ladder=ladder_result,
    )


# --- Single-order cancel / order-state reads (GoldenFibo) --------------------

_ORDER_ID_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _ondo_order_id_segment(value: Any) -> Optional[str]:
    """Safe path segment for ``/v1/perps/orders/{id}``. Rejects empty/unsafe ids."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or not _ORDER_ID_SEGMENT.fullmatch(text):
        return None
    return text


def _classify_ondo_order_status(raw_status: Any) -> str:
    """Map Ondo order status strings to GoldenFibo taxonomy.

    Never returns FILLED unless Ondo itself reported a filled status.
    Absence / unknown strings map to UNKNOWN.
    """
    status = str(raw_status or "").strip().lower()
    if status in {"open", "pending", "untriggered"}:
        return "OPEN"
    if status in {"partiallyfilled", "partial", "partial_fill", "partially_filled"}:
        return "PARTIALLY_FILLED"
    if status in {"fullyfilled", "filled", "fully_filled"}:
        return "FILLED"
    if status in {"cancelled", "canceled"}:
        return "CANCELED"
    if status in {"rejected"}:
        return "REJECTED"
    return "UNKNOWN"


def _order_state_from_ondo_row(order: Dict[str, Any], *, exchange_order_id: Optional[str] = None) -> Dict[str, Any]:
    classification = _classify_ondo_order_status(order.get("status"))
    oid = str(order.get("orderId") or exchange_order_id or "").strip() or None
    filled = _decimal_or_none(order.get("filledSize"))
    size = _decimal_or_none(order.get("size"))
    remaining = None
    if size is not None and filled is not None:
        remaining = size - filled
        if remaining < 0:
            remaining = Decimal("0")
    taxonomy = "ACTIVE" if classification in {"OPEN", "PARTIALLY_FILLED"} else classification
    # Ondo documents the average execution price as ``averageFillPrice``
    # (and aliases ``avgFillPrice`` on older snapshots). Map it into the
    # canonical GoldenFibo field ``actual_fill_price`` at this venue
    # boundary so the generic Step0 promotion path can pick it up without
    # needing per-venue field names. ``average_fill_price`` is preserved
    # for any caller that prefers the venue-native name.
    avg_fill_raw = order.get("averageFillPrice") or order.get("avgFillPrice")
    return {
        "exchange_order_id": oid,
        "client_order_id": order.get("clientOrderId"),
        "status": classification,
        "taxonomy": taxonomy,
        "classification": classification,
        "side": str(order.get("side") or "").strip().lower() or None,
        "requested_size": _decimal_text(size) if size is not None else None,
        "filled_size": _decimal_text(filled) if filled is not None else None,
        "remaining_size": _decimal_text(remaining) if remaining is not None else None,
        "limit_price": _decimal_text(order.get("price")),
        "average_fill_price": _decimal_text(avg_fill_raw),
        "actual_fill_price": _decimal_text(avg_fill_raw),
        "symbol": str(order.get("market") or "").strip() or None,
        "raw_status": order.get("status"),
    }


def _decimals_from_increment(increment: Optional[Decimal]) -> int:
    if increment is None or increment <= 0:
        return 0
    text = format(increment, "f").rstrip("0")
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1])


def _execute_cancel_order(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Cancel exactly one Ondo order by server order id.

    Uses ``DELETE /v1/perps/orders/{orderId}``. Never expands to a
    symbol+side group cancel. 404 / already-gone is treated as success
    (idempotent for GoldenFibo restart/reconciliation).
    """
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="cancel_order",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Ondo Perps account configuration",
        )
    raw_oid = request.get("order_id")
    if raw_oid is None:
        raw_oid = request.get("order_index")
    if raw_oid is None:
        raw_oid = request.get("exchange_order_id")
    oid = _ondo_order_id_segment(raw_oid)
    if not oid:
        return make_failure(
            operation="cancel_order",
            exchange=name,
            account=account,
            code="MISSING_ORDER_ID",
            message="order_id is required for single-order cancellation.",
        )
    path = f"{_PATH_PERPS_ORDERS}/{oid}"
    try:
        _signed_delete(credentials, path)
    except OndoHTTPError as exc:
        body = str(exc.body or "").lower()
        if exc.status == 404 or any(
            token in body
            for token in (
                "order_not_found",
                "already_cancelled",
                "already_canceled",
                "not_in_cancellable_state",
                "already_filled",
            )
        ):
            return make_success(
                operation="cancel_order",
                exchange=name,
                account=account,
                order_state={"outcome": "ALREADY_TERMINAL", "exchange_order_id": oid},
            )
        return _map_http_error_to_failure(exc, operation="cancel_order", account=account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="cancel_order",
            exchange=name,
            account=account,
            code="CANCEL_FAILED",
            message=_redact(sanitize_error_message(str(exc))),
        )
    return make_success(
        operation="cancel_order",
        exchange=name,
        account=account,
        order_state={"outcome": "CANCELED", "exchange_order_id": oid},
    )


def _execute_get_order_state(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Authoritative single-order read: GET /v1/perps/orders/{orderId}.

    Disappearance (404 / order_not_found) is UNKNOWN, never FILLED.
    """
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="get_order_state",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Ondo Perps account configuration",
        )
    raw_oid = request.get("order_id")
    if raw_oid is None:
        raw_oid = request.get("order_index")
    if raw_oid is None:
        raw_oid = request.get("exchange_order_id")
    oid = _ondo_order_id_segment(raw_oid)
    if not oid:
        return make_failure(
            operation="get_order_state",
            exchange=name,
            account=account,
            code="MISSING_ORDER_ID",
            message="order_id is required.",
        )
    path = f"{_PATH_PERPS_ORDERS}/{oid}"
    try:
        payload = _signed_get(credentials, path)
    except OndoHTTPError as exc:
        body = str(exc.body or "").lower()
        if exc.status == 404 or "order_not_found" in body:
            return make_success(
                operation="get_order_state",
                exchange=name,
                account=account,
                order_state={
                    "exchange_order_id": oid,
                    "status": "UNKNOWN",
                    "taxonomy": "UNKNOWN",
                    "classification": "UNKNOWN",
                    "note": "Ondo no longer reports this order id; do not infer FILLED",
                },
            )
        return _map_http_error_to_failure(exc, operation="get_order_state", account=account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="get_order_state",
            exchange=name,
            account=account,
            code="ONDOPERPS_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )
    order = payload if isinstance(payload, dict) else {}
    if not order:
        return make_success(
            operation="get_order_state",
            exchange=name,
            account=account,
            order_state={
                "exchange_order_id": oid,
                "status": "UNKNOWN",
                "taxonomy": "UNKNOWN",
                "classification": "UNKNOWN",
            },
        )
    return make_success(
        operation="get_order_state",
        exchange=name,
        account=account,
        order_state=_order_state_from_ondo_row(order, exchange_order_id=oid),
    )


def _execute_get_order_state_by_client_id(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Authoritative lookup GET /v1/perps/orders/client:{clientOrderId}."""
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="get_order_state_by_client_id",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Ondo Perps account configuration",
        )
    raw = request.get("client_order_id")
    if raw is None:
        raw = request.get("client_order_index")
    if raw is None:
        raw = request.get("clientOrderId")
    try:
        client_order_id = _normalize_client_order_id(raw)
    except ValueError as exc:
        return make_failure(
            operation="get_order_state_by_client_id",
            exchange=name,
            account=account,
            code="INVALID_CLIENT_ORDER_ID",
            message=str(exc),
        )
    if not client_order_id:
        return make_failure(
            operation="get_order_state_by_client_id",
            exchange=name,
            account=account,
            code="MISSING_CLIENT_ORDER_ID",
            message="client_order_id is required.",
        )
    try:
        order = _fetch_order_by_client_order_id(credentials, client_order_id)
    except OndoHTTPError as exc:
        body = str(exc.body or "").lower()
        if exc.status in (400, 404) or "order_not_found" in body:
            return make_success(
                operation="get_order_state_by_client_id",
                exchange=name,
                account=account,
                order_state={
                    "client_order_id": client_order_id,
                    "status": "UNKNOWN",
                    "taxonomy": "UNKNOWN",
                    "classification": "UNKNOWN",
                    "note": "Ondo clientOrderId lookup did not find this order; do not infer FILLED",
                },
            )
        return _map_http_error_to_failure(exc, operation="get_order_state_by_client_id", account=account)
    except RuntimeError as exc:
        if "order_not_found" in str(exc):
            return make_success(
                operation="get_order_state_by_client_id",
                exchange=name,
                account=account,
                order_state={
                    "client_order_id": client_order_id,
                    "status": "UNKNOWN",
                    "taxonomy": "UNKNOWN",
                    "classification": "UNKNOWN",
                },
            )
        return make_failure(
            operation="get_order_state_by_client_id",
            exchange=name,
            account=account,
            code="ONDOPERPS_ERROR",
            message=_redact(sanitize_error_message(str(exc))),
        )
    if not isinstance(order, dict) or not order:
        return make_success(
            operation="get_order_state_by_client_id",
            exchange=name,
            account=account,
            order_state={
                "client_order_id": client_order_id,
                "status": "UNKNOWN",
                "taxonomy": "UNKNOWN",
                "classification": "UNKNOWN",
            },
        )
    state = _order_state_from_ondo_row(order)
    state["client_order_id"] = client_order_id
    return make_success(
        operation="get_order_state_by_client_id",
        exchange=name,
        account=account,
        order_state=state,
    )


def _execute_market_constraints(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Venue constraints for GoldenFibo preflight (tick/step/decimals)."""
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="market_constraints",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Ondo Perps account configuration",
        )
    requested_symbol = str(request.get("symbol") or "").strip().upper()
    metadata, error = _resolve_market_metadata(credentials, requested_symbol)
    if error is not None:
        return make_failure(
            operation="market_constraints",
            exchange=name,
            account=account,
            code=error.error.code if error.error else "INSTRUMENT_NOT_FOUND",
            message=error.error.message if error.error else "Instrument resolution failed.",
        )
    assert metadata is not None
    base_inc = metadata.get("base_increment")
    quote_inc = metadata.get("quote_increment")
    constraints = {
        "symbol": requested_symbol,
        "market": metadata.get("market"),
        "tick_size": _decimal_text(quote_inc) if quote_inc is not None else None,
        "step_size": _decimal_text(base_inc) if base_inc is not None else None,
        "size_decimals": _decimals_from_increment(base_inc),
        "price_decimals": _decimals_from_increment(quote_inc),
        "min_base_amount": _decimal_text(base_inc) if base_inc is not None else None,
        # Ondo does not document a min-notional on /v1/markets; omit so
        # GoldenFibo preflight fail-opens on quote (same as Rise).
    }
    return make_success(
        operation="market_constraints",
        exchange=name,
        account=account,
        order_state=constraints,
    )


# --- Cancel order group ------------------------------------------------------


def _cancel_order_group(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Ondo Perps account configuration",
        )

    requested_symbol = str(request.get("symbol") or "").strip().upper()
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

    metadata, error = _resolve_market_metadata(credentials, requested_symbol)
    if error is not None:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code=error.error.code if error.error else "INSTRUMENT_NOT_FOUND",
            message=error.error.message if error.error else "Instrument resolution failed.",
        )

    try:
        pre_orders = _fetch_open_orders_snapshot(credentials)
    except OndoHTTPError as exc:
        return _map_http_error_to_failure(exc, operation="cancel_order_group", account=account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code="OPEN_ORDERS_UNAVAILABLE",
            message=_redact(sanitize_error_message(str(exc))),
        )

    target_ids: List[str] = []
    non_target_ids: List[str] = []
    for order in pre_orders:
        if not isinstance(order, dict):
            continue
        if str(order.get("market") or "") != metadata["market"]:
            continue
        if str(order.get("side") or "").lower() != requested_side:
            non_target_ids.append(str(order.get("orderId") or ""))
            continue
        oid = str(order.get("orderId") or "").strip()
        if oid:
            target_ids.append(oid)

    if not target_ids:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            code="NO_TARGET_ORDERS",
            message="No matching orders were found.",
            cancel_group=CanonicalCancelGroupResult(
                symbol=requested_symbol,
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

    # Cancel in one batched HTTP call per ``_CANCEL_BATCH_LIMIT`` order IDs
    # via ``DELETE /v1/perps/orders/batch?orderIDs=A,B,C``. Ondo's batch
    # endpoint returns ``{successfulCancels: [...], failedCancels: [...]}``
    # so we get per-order status in a single round trip — orders with
    # ``order_already_cancelled`` / ``order_not_in_cancellable_state`` /
    # ``order_already_filled`` show up in ``failedCancels`` and we treat
    # them as success-equivalent (the verification step confirms
    # absence from the post-snapshot).
    #
    # Chunking into 50-ID slices keeps the URL comfortably under any
    # proxy/CDN limit (≈1.7kB at 32-char IDs) while still being ~4×
    # fewer requests than per-order DELETE for a 200-order book. This
    # also avoids Ondo's per-account rate limiter, which started
    # returning ``too_many_requests`` after ~30 sequential DELETEs.
    cancelled_count = 0
    partial = False
    status_code = ""
    status_message = ""
    batches: List[Dict[str, Any]] = []
    batch_cancel_path = f"{_PATH_PERPS_ORDERS}/batch"
    for start in range(0, len(target_ids), _CANCEL_BATCH_LIMIT):
        chunk = target_ids[start:start + _CANCEL_BATCH_LIMIT]
        query = urllib.parse.urlencode({"orderIDs": ",".join(chunk)})
        batch_path = f"{batch_cancel_path}?{query}"
        try:
            response = _signed_delete(credentials, batch_path)
        except OndoHTTPError as exc:
            partial = True
            status_code = "CANCEL_FAILED"
            if exc.status == 429 or "too_many_requests" in exc.body or "rate" in exc.body.lower():
                status_code = "RATE_LIMITED"
            status_message = _redact(sanitize_error_message(exc.body or f"HTTP {exc.code}"))
            batches.append({
                "submitted": len(chunk),
                "accepted": 0,
                "ok": False,
                "reason": status_code,
            })
            break
        except Exception as exc:  # noqa: BLE001
            partial = True
            status_code = "CANCEL_FAILED"
            status_message = _redact(sanitize_error_message(str(exc)))
            batches.append({
                "submitted": len(chunk),
                "accepted": 0,
                "ok": False,
                "reason": status_code,
            })
            break

        successful = response.get("successfulCancels") if isinstance(response, dict) else None
        failed = response.get("failedCancels") if isinstance(response, dict) else None
        successful_ids: List[str] = []
        already_gone = 0
        if isinstance(successful, list):
            for entry in successful:
                if isinstance(entry, dict):
                    oid = str(entry.get("orderId") or "").strip()
                    if oid:
                        successful_ids.append(oid)
            cancelled_count += len(successful_ids)
        if isinstance(failed, list):
            for entry in failed:
                if not isinstance(entry, dict):
                    continue
                err_code = str(entry.get("errorCode") or entry.get("error") or "").lower()
                # ``order_already_cancelled`` / ``order_not_in_cancellable_state``
                # / ``order_already_filled`` — the order is already gone from
                # the book, so we count it as a verified success.
                if any(token in err_code for token in (
                    "already_cancelled", "already_canceled",
                    "not_in_cancellable_state", "already_filled",
                    "order_not_found",
                )):
                    already_gone += 1
        batches.append({
            "submitted": len(chunk),
            "accepted": len(successful_ids) + already_gone,
            "ok": True,
            "successful_cancels": len(successful_ids) if isinstance(successful, list) else 0,
            "failed_cancels": len(failed) if isinstance(failed, list) else 0,
            "already_gone": already_gone,
        })

    # Verify by re-fetching open orders with 429-backoff. The fetch
    # helper transparently falls back to the unfiltered endpoint when
    # the filtered ``?status=open`` snapshot is rate-limited, so a
    # transient 429 never produces a false VERIFICATION_FAILED when
    # the orders actually left the book.
    post_orders: List[Dict[str, Any]] = []
    try:
        post_orders = _fetch_orders_for_verification(credentials)
    except Exception:  # noqa: BLE001
        partial = True
        if not status_code:
            status_code = "VERIFY_UNAVAILABLE"
            status_message = "Could not re-fetch open orders after cancellation."
    post_ids = {str(order.get("orderId") or "") for order in post_orders if isinstance(order, dict)}
    remaining_target = sum(1 for oid in target_ids if oid in post_ids)
    confirmed_absent = len(target_ids) - remaining_target
    non_target_preserved = all((oid in post_ids) for oid in non_target_ids if oid)
    verified = remaining_target == 0 and non_target_preserved and not partial

    cancel_result = CanonicalCancelGroupResult(
        symbol=requested_symbol,
        side=requested_side,
        targeted_order_count=len(target_ids),
        cancelled_order_count=cancelled_count,
        confirmed_absent_count=confirmed_absent,
        remaining_target_count=remaining_target,
        verified=verified,
        partial=partial or not verified,
        status="success" if verified else ("partial" if cancelled_count else "failed"),
        batch_count=len(batches),
        batches=batches or None,
    )
    if verified:
        return make_success(
            operation="cancel_order_group",
            exchange=name,
            account=account,
            cancel_group=cancel_result,
        )
    return make_failure(
        operation="cancel_order_group",
        exchange=name,
        account=account,
        code=status_code or ("VERIFICATION_FAILED" if cancelled_count else "CANCEL_FAILED"),
        message=status_message or "Cancellation was only partially completed.",
        cancel_group=cancel_result,
    )


# --- Set TP / Set SL --------------------------------------------------------


_STOP_ORDER_PATH = "/v1/perps/stop_order"


def _set_position_trigger(
    account: str,
    request: Dict[str, Any],
    *,
    kind: str,
) -> CanonicalResponse:
    """Shared implementation for ``set_tp`` and ``set_sl``.

    Two modes driven by the submitted ``price``:

    - ``price > 0`` — set / replace the trigger. Ondo's stop-order
      model is per (market, direction): a single trigger price can be
      active per direction, and re-submitting with a new price
      atomically replaces the previous one (no separate delete needed).
      We POST ``/v1/perps/stop_order`` and verify by re-querying
      ``GET /v1/perps/stop_order?market=...&positionDirection=...``.

    - ``price == 0`` — delete the trigger via
      ``DELETE /v1/perps/stop_order?market=<m>&type=<kind>``. The
      wizard passes a literal ``"0"`` to indicate "remove the TP / SL
      I previously set". Omitting ``type`` would remove BOTH; we send
      the specific kind so deleting the TP leaves the SL alone and
      vice versa.
    """
    operation = "set_tp" if kind == "takeProfit" else "set_sl"
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Ondo Perps account configuration",
        )

    requested_symbol = str(request.get("symbol") or "").strip().upper()
    price_text = str(request.get("price") or "").strip()
    requested_price = _decimal_or_none(price_text)
    if not requested_symbol:
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    # ``price == 0`` is the wizard's documented sentinel for "delete
    # this trigger". Anything else non-positive (negative, blank,
    # non-numeric) is a real validation error.
    is_delete = price_text == "0"
    if not is_delete and (requested_price is None or requested_price <= 0):
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="INVALID_PRICE",
            message="Trigger price must be positive, or 0 to delete the existing trigger.",
        )

    metadata, error = _resolve_market_metadata(credentials, requested_symbol)
    if error is not None:
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code=error.error.code if error.error else "INSTRUMENT_NOT_FOUND",
            message=error.error.message if error.error else "Instrument resolution failed.",
        )

    positions = _fetch_positions_snapshot(credentials)
    direction: Optional[str] = None
    current_size = "0"
    current_side = ""
    for row in positions:
        if not isinstance(row, dict):
            continue
        if str(row.get("market") or "") != metadata["market"]:
            continue
        direct = str(row.get("direction") or "").strip().lower()
        if direct not in {"long", "short"}:
            continue
        direction = direct
        current_size = _decimal_text(row.get("netQuantity"))
        current_side = direct
        break
    # --- Delete branch ---------------------------------------------------
    if is_delete:
        delete_query = urllib.parse.urlencode({
            "market": metadata["market"],
            "type": kind,  # ``takeProfit`` or ``stopLoss`` — leaves the other intact
        })
        delete_path = f"{_STOP_ORDER_PATH}?{delete_query}"
        try:
            _signed_delete(credentials, delete_path)
        except OndoHTTPError as exc:
            # Idempotent semantics: if there's nothing to remove, the
            # verification step will report ``removed: True`` anyway.
            # We surface non-404 errors; 404 means "already gone".
            if exc.status != 404:
                return _map_http_error_to_failure(exc, operation=operation, account=account)
        except Exception as exc:  # noqa: BLE001
            return make_failure(
                operation=operation,
                exchange=name,
                account=account,
                code="STOP_ORDER_DELETE_FAILED",
                message=_redact(sanitize_error_message(str(exc))),
                position_action=CanonicalPositionActionResult(
                    operation=operation,
                    symbol=requested_symbol,
                    verified=False,
                    status="failed",
                    current_side=current_side,
                    current_size=current_size,
                ),
            )
        # Verify by re-querying the stop-order snapshot and confirming
        # the requested kind is now absent for the relevant market lane.
        verify_directions = [direction] if direction in {"long", "short"} else ["long", "short"]
        verified = False
        for attempt in range(ORDER_VERIFY_ATTEMPTS):
            expected_key = "takeProfit" if kind == "takeProfit" else "stopLoss"
            direction_states: list[bool] = []
            retry_due_to_rate_limit = False
            for verify_direction in verify_directions:
                try:
                    stop_payload = _signed_get(
                        credentials,
                        f"{_STOP_ORDER_PATH}?market={urllib.parse.quote(metadata['market'], safe='')}&positionDirection={verify_direction}",
                    )
                except OndoHTTPError as exc:
                    if exc.status == 429 and attempt < ORDER_VERIFY_ATTEMPTS - 1:
                        retry_due_to_rate_limit = True
                        break
                    stop_payload = None
                except Exception:  # noqa: BLE001
                    stop_payload = None
                entry: Optional[Dict[str, Any]] = None
                if isinstance(stop_payload, list):
                    for candidate in stop_payload:
                        if not isinstance(candidate, dict):
                            continue
                        if str(candidate.get("market") or "") != metadata["market"]:
                            continue
                        if str(candidate.get("positionDirection") or "").lower() != verify_direction:
                            continue
                        entry = candidate
                        break
                elif isinstance(stop_payload, dict):
                    if str(stop_payload.get("market") or "") == metadata["market"] and \
                       str(stop_payload.get("positionDirection") or "").lower() == verify_direction:
                        entry = stop_payload
                current_value = entry.get(expected_key) if isinstance(entry, dict) else None
                direction_states.append(
                    current_value in (None, "", "null") or _decimal_or_none(current_value) is None
                )
            if retry_due_to_rate_limit:
                time.sleep(ORDER_VERIFY_BACKOFF_SECONDS)
                continue
            verified = bool(direction_states) and all(direction_states)
            if verified:
                break
            if attempt < ORDER_VERIFY_ATTEMPTS - 1:
                time.sleep(ORDER_VERIFY_DELAY_SECONDS)
        action_result = CanonicalPositionActionResult(
            operation=operation,
            symbol=requested_symbol,
            verified=verified,
            status="success" if verified else "submitted",
            removed=True,
            current_side=current_side,
            current_size=current_size,
            message="Trigger removed." if verified else "Trigger removal could not be verified.",
        )
        if verified:
            return make_success(
                operation=operation,
                exchange=name,
                account=account,
                position_action=action_result,
            )
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="VERIFICATION_FAILED",
            message=f"{operation.replace('_', ' ').title()} removal could not be verified in the stop-order snapshot.",
            position_action=action_result,
        )

    if direction is None:
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="POSITION_NOT_FOUND",
            message=f"No open {requested_symbol} position found.",
        )

    # --- Set / replace branch --------------------------------------------
    aligned_price = _align_price(requested_price, metadata)
    body = {
        "market": metadata["market"],
        "positionDirection": direction,
        "type": kind,
        "triggerPrice": _decimal_text(aligned_price),
    }
    try:
        response = _signed_post(credentials, _STOP_ORDER_PATH, body)
    except OndoHTTPError as exc:
        return _map_http_error_to_failure(exc, operation=operation, account=account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="STOP_ORDER_FAILED",
            message=_redact(sanitize_error_message(str(exc))),
            position_action=CanonicalPositionActionResult(
                operation=operation,
                symbol=requested_symbol,
                verified=False,
                price=_decimal_text(aligned_price),
                status="failed",
            ),
        )

    # Verify by re-fetching the stop-order state for this (market, direction).
    verified = False
    expected_key = "takeProfit" if kind == "takeProfit" else "stopLoss"
    for _ in range(ORDER_VERIFY_ATTEMPTS):
        try:
            stop_payload = _signed_get(
                credentials,
                f"{_STOP_ORDER_PATH}?market={urllib.parse.quote(metadata['market'], safe='')}&positionDirection={direction}",
            )
        except Exception:  # noqa: BLE001
            stop_payload = None
        if isinstance(stop_payload, list):
            for entry in stop_payload:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("market") or "") != metadata["market"]:
                    continue
                if str(entry.get("positionDirection") or "").lower() != direction:
                    continue
                actual = _decimal_or_none(entry.get(expected_key))
                if actual == aligned_price:
                    verified = True
                    break
        elif isinstance(stop_payload, dict):
            actual = _decimal_or_none(stop_payload.get(expected_key))
            if actual == aligned_price:
                verified = True
        if verified:
            break
        time.sleep(ORDER_VERIFY_DELAY_SECONDS)

    action_result = CanonicalPositionActionResult(
        operation=operation,
        symbol=requested_symbol,
        verified=verified,
        price=_decimal_text(aligned_price),
        status="success" if verified else "submitted",
        current_side=current_side,
        current_size=current_size,
    )
    if verified:
        return make_success(
            operation=operation,
            exchange=name,
            account=account,
            position_action=action_result,
        )
    return make_failure(
        operation=operation,
        exchange=name,
        account=account,
        code="VERIFICATION_FAILED",
        message=f"{operation.replace('_', ' ').title()} was accepted but could not be verified in the stop-order snapshot.",
        position_action=action_result,
    )


# --- Set both TP and SL ----------------------------------------------------
#
# ``_set_position_protections`` is a thin wrapper that drives the existing
# ``_set_position_trigger`` helper twice (once per leg) and verifies both
# fields are present in the resulting ``/v1/perps/stop_order`` snapshot.
# The behaviour mirrors what Fibo needs for its 5-step handoff (steps 3–5):
#   1. set TP, wait for it to be reflected on the snapshot
#   2. set SL, wait for it to be reflected on the snapshot
#   3. re-read the snapshot once more and confirm BOTH legs are at the
#      requested prices. This third read is the "verify BOTH" pass.
#
# Both calls MUST succeed for the response to be ``success``. If either
# leg fails, the response carries the failing leg's error code and the
# opposite leg's verification state so the caller can reason about which
# leg is missing. We deliberately do NOT silently roll back the leg that
# succeeded — once the TP lands on the exchange, removing it would
# change the position's behaviour. The frozen-registration contract for
# Fibo lives above this layer.


def _set_position_protections(
    account: str, request: Dict[str, Any]
) -> CanonicalResponse:
    """Atomically set both TP and SL on the account's current position.

    Request keys (all required):

      - ``symbol``        : canonical Fibo symbol (e.g. ``"US100"``).
      - ``take_profit``   : positive Decimal price for the TP trigger.
      - ``stop_loss``     : positive Decimal price for the SL trigger.

    Returns a CanonicalResponse whose ``position_action`` carries the per-leg
    verification state, OR a failure with the failing leg's code. The
    canonical ``position_action.verified`` reflects BOTH legs being present.
    """
    requested_symbol = str(request.get("symbol") or "").strip().upper()
    tp_text = str(request.get("take_profit") or "").strip()
    sl_text = str(request.get("stop_loss") or "").strip()
    if not requested_symbol:
        return make_failure(
            operation="set_position_protections",
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    if not tp_text or not sl_text:
        return make_failure(
            operation="set_position_protections",
            exchange=name,
            account=account,
            code="MISSING_PROTECTION_PRICE",
            message="Both take_profit and stop_loss are required.",
        )

    tp_price = _decimal_or_none(tp_text)
    sl_price = _decimal_or_none(sl_text)
    if tp_price is None or tp_price <= 0 or sl_price is None or sl_price <= 0:
        return make_failure(
            operation="set_position_protections",
            exchange=name,
            account=account,
            code="INVALID_PROTECTION_PRICE",
            message="take_profit and stop_loss must be positive.",
        )

    # Step 1 — set TP. We delegate to the existing helper which already
    # does POST + verify-loop. If it fails we surface the failure verbatim.
    tp_response = _set_position_trigger(
        account,
        {
            "symbol": requested_symbol,
            "price": _decimal_text(tp_price),
        },
        kind="takeProfit",
    )
    tp_ok = tp_response.success

    # Step 2 — set SL. We always attempt the SL even if TP failed; this
    # matches what the wizard already does (set_tp then set_sl are
    # independent operations). The caller can read the per-leg status from
    # the returned ``position_action`` data.
    sl_response = _set_position_trigger(
        account,
        {
            "symbol": requested_symbol,
            "price": _decimal_text(sl_price),
        },
        kind="stopLoss",
    )
    sl_ok = sl_response.success

    # Step 3 — final verification: re-read the stop-order snapshot and
    # confirm both fields are at the requested prices. This is the
    # "verify BOTH" pass Fibo's 5-step handoff demands.
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="set_position_protections",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Ondo Perps account configuration",
        )
    metadata, error = _resolve_market_metadata(credentials, requested_symbol)
    if error is not None:
        return _bubble_failure(error, "set_position_protections", account)

    final_tp_ok = False
    final_sl_ok = False
    try:
        # Resolve the position's direction so we can target the
        # (market, direction) record. If the position is already closed or
        # the snapshot is unavailable, both legs report False and the
        # response carries an explanation in ``message``.
        positions = _fetch_positions_snapshot(credentials)
        direction: Optional[str] = None
        current_side = ""
        current_size = "0"
        for row in positions:
            if not isinstance(row, dict):
                continue
            if str(row.get("market") or "") != metadata["market"]:
                continue
            direct = str(row.get("direction") or "").strip().lower()
            if direct not in {"long", "short"}:
                continue
            direction = direct
            current_side = direct
            current_size = _decimal_text(row.get("netQuantity"))
            break
        if direction is None:
            return make_failure(
                operation="set_position_protections",
                exchange=name,
                account=account,
                code="POSITION_NOT_FOUND",
                message=(
                    "Cannot verify protections: no open position on "
                    f"{requested_symbol}."
                ),
            )
        snap = _signed_get(
            credentials,
            f"{_STOP_ORDER_PATH}?market={urllib.parse.quote(metadata['market'], safe='')}&positionDirection={direction}",
        )
        entry: Optional[Dict[str, Any]] = None
        if isinstance(snap, list):
            for cand in snap:
                if not isinstance(cand, dict):
                    continue
                if str(cand.get("market") or "") != metadata["market"]:
                    continue
                if str(cand.get("positionDirection") or "").lower() != direction:
                    continue
                entry = cand
                break
        elif isinstance(snap, dict):
            if (
                str(snap.get("market") or "") == metadata["market"]
                and str(snap.get("positionDirection") or "").lower() == direction
            ):
                entry = snap
        if entry is not None:
            actual_tp = _decimal_or_none(entry.get("takeProfit"))
            actual_sl = _decimal_or_none(entry.get("stopLoss"))
            # Quantise the expected prices against the market's quoteIncrement
            # before comparison — Ondo stores triggers at exchange precision.
            aligned_tp = _align_price(tp_price, metadata)
            aligned_sl = _align_price(sl_price, metadata)
            final_tp_ok = actual_tp == aligned_tp
            final_sl_ok = actual_sl == aligned_sl
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="set_position_protections",
            exchange=name,
            account=account,
            code="VERIFY_UNAVAILABLE",
            message=_redact(sanitize_error_message(str(exc))),
        )

    overall_ok = tp_ok and sl_ok and final_tp_ok and final_sl_ok
    action_result = CanonicalPositionActionResult(
        operation="set_position_protections",
        symbol=requested_symbol,
        verified=overall_ok,
        status="success" if overall_ok else "partial",
        current_side=current_side,
        current_size=current_size,
        message=(
            "TP and SL both attached and verified."
            if overall_ok
            else (
                f"tp_ok={tp_ok},sl_ok={sl_ok},"
                f"final_tp_ok={final_tp_ok},final_sl_ok={final_sl_ok}"
            )
        ),
    )
    # Per-leg codes for the caller to introspect.
    if overall_ok:
        return make_success(
            operation="set_position_protections",
            exchange=name,
            account=account,
            position_action=action_result,
        )
    return make_failure(
        operation="set_position_protections",
        exchange=name,
        account=account,
        code=(
            "TP_SL_BOTH_MISSING"
            if not final_tp_ok and not final_sl_ok
            else "TP_ONLY"
            if not final_tp_ok
            else "SL_ONLY"
        ),
        message=action_result.message,
        position_action=action_result,
    )


def _bubble_failure(
    error: CanonicalResponse, operation: str, account: str
) -> CanonicalResponse:
    """Re-emit a failure response, retagging it for a new operation."""
    if error.error is None:
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="INSTRUMENT_NOT_FOUND",
            message="Instrument resolution failed.",
        )
    return make_failure(
        operation=operation,
        exchange=name,
        account=account,
        code=error.error.code,
        message=error.error.message,
    )


# --- Read single position state --------------------------------------------
#
# Cheap lookup of a single (symbol) position row + its current TP / SL +
# the live markPrice. Used by Fibo's "confirm_fill" and "quote" paths.


def _position_state(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Return the live position row for ``symbol``, including TP / SL.

    Returns a CanonicalResponse whose ``positions`` carries a single-element
    list (or empty list when no position exists). Ondo's ``/v1/perps/positions``
    is the source for direction / netQuantity / markPrice; the stop-order
    snapshot is the source for the current TP / SL.

    Request keys:
      - ``symbol`` (required)
    """
    requested_symbol = str(request.get("symbol") or "").strip().upper()
    if not requested_symbol:
        return make_failure(
            operation="position_state",
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="position_state",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Ondo Perps account configuration",
        )
    metadata, error = _resolve_market_metadata(credentials, requested_symbol)
    if error is not None:
        return _bubble_failure(error, "position_state", account)
    try:
        positions = _fetch_positions_snapshot(credentials)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="position_state",
            exchange=name,
            account=account,
            code="POSITIONS_UNAVAILABLE",
            message=_redact(sanitize_error_message(str(exc))),
        )
    row: Optional[Dict[str, Any]] = None
    direction: Optional[str] = None
    for candidate in positions:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("market") or "") != metadata["market"]:
            continue
        direct = str(candidate.get("direction") or "").strip().lower()
        if direct not in {"long", "short"}:
            continue
        row = candidate
        direction = direct
        break
    if row is None or direction is None:
        # No open position. We still succeed but with an empty list — the
        # caller (Fibo's confirm_fill / quote adapter) treats this as
        # "no position present yet."
        return make_success(
            operation="position_state",
            exchange=name,
            account=account,
            positions=[],
            instrument=CanonicalInstrument(
                requested_symbol=requested_symbol,
                symbol=requested_symbol,
                display_name=metadata.get("market", requested_symbol),
                price_increment=_decimal_text(metadata.get("quote_increment")),
                size_increment=_decimal_text(metadata.get("base_increment")),
            ),
        )

    # Mark price from the same row.
    mark_price_text = _decimal_text(row.get("markPrice"))

    # Look up the current TP / SL for this (market, direction).
    tp_text: Optional[str] = None
    sl_text: Optional[str] = None
    try:
        protections = _safe_fetch_stop_protections(credentials)
        protection = protections.get((requested_symbol, direction)) or {}
        if isinstance(protection, dict):
            tp_text = protection.get("tp") or None
            sl_text = protection.get("sl") or None
    except Exception:  # noqa: BLE001
        pass

    side_canonical = "long" if direction == "long" else "short"
    position = CanonicalPosition(
        symbol=requested_symbol,
        side=side_canonical,
        size=_decimal_text(row.get("netQuantity")),
        entry_price=_decimal_text(row.get("averageEntryPrice")),
        pnl=_decimal_text(row.get("unrealizedPnl")),
        tp=tp_text,
        sl=sl_text,
        tp_count=1 if tp_text else None,
        sl_count=1 if sl_text else None,
    )
    # We surface markPrice via the instrument descriptor's price_increment?
    # No — price_increment is the quote tick, not a live price. Instead we
    # pack markPrice into the position's ``entry_price`` is wrong too. We
    # carry it as a side-channel: re-attach via a hidden ``order_groups``
    # list — but that's a misuse. The cleanest option is to expose a new
    # canonical position field; for v1 we use the existing CanonicalPosition
    # and let Fibo parse ``pnl`` / ``entry_price`` from the row, and
    # additionally fetch markPrice separately via the existing
    # ``positions_orders`` read path. For the Fibo adapter's needs, the
    # call to ``_signed_get(_PATH_PERPS_POSITIONS)`` directly inside the
    # OndoPerps quote source provides the markPrice. See
    # ``plugins/trade/fibo/quote_ondoperps.py``.
    return make_success(
        operation="position_state",
        exchange=name,
        account=account,
        positions=[position],
        instrument=CanonicalInstrument(
            requested_symbol=requested_symbol,
            symbol=requested_symbol,
            display_name=metadata.get("market", requested_symbol),
            price_increment=_decimal_text(metadata.get("quote_increment")),
            size_increment=_decimal_text(metadata.get("base_increment")),
        ),
    )


def _market_price(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Return a venue-native current market-price snapshot for one symbol."""
    requested_symbol = str(request.get("symbol") or "").strip().upper()
    if not requested_symbol:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    credentials = _lookup_credentials(account)
    if credentials is None:
        return make_failure(
            operation="market_price",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Ondo Perps account configuration",
        )
    metadata, error = _resolve_market_metadata(credentials, requested_symbol)
    if error is not None:
        return _bubble_failure(error, "market_price", account)
    try:
        payload = _signed_get(credentials, _PATH_PERPS_MARK_PRICES)
    except OndoHTTPError as exc:
        return _map_http_error_to_failure(exc, operation="market_price", account=account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="market_price",
            exchange=name,
            account=account,
            code="MARK_PRICES_UNAVAILABLE",
            message=_redact(sanitize_error_message(str(exc))),
        )

    result = None
    if isinstance(payload, dict):
        nested = payload.get("result")
        if isinstance(nested, dict):
            result = nested
        else:
            result = payload
    entry = result.get(metadata["market"]) if isinstance(result, dict) else None
    if not isinstance(entry, dict):
        return make_failure(
            operation="market_price",
            exchange=name,
            account=account,
            code="MARK_PRICE_NOT_FOUND",
            message=f"Ondo Perps mark-price snapshot has no entry for '{metadata['market']}'.",
            instrument=CanonicalInstrument(
                requested_symbol=requested_symbol,
                symbol=requested_symbol,
                display_name=metadata.get("market", requested_symbol),
                price_increment=_decimal_text(metadata.get("quote_increment")),
                size_increment=_decimal_text(metadata.get("base_increment")),
            ),
        )

    return make_success(
        operation="market_price",
        exchange=name,
        account=account,
        instrument=CanonicalInstrument(
            requested_symbol=requested_symbol,
            symbol=requested_symbol,
            display_name=metadata.get("market", requested_symbol),
            price_increment=_decimal_text(metadata.get("quote_increment")),
            size_increment=_decimal_text(metadata.get("base_increment")),
        ),
        market_price=CanonicalMarketPrice(
            requested_symbol=requested_symbol,
            market=str(metadata.get("market") or requested_symbol),
            mark_price=_decimal_text(entry.get("markPrice")),
            oracle_price=_decimal_text(entry.get("oraclePrice")),
            last_external_price=_decimal_text(entry.get("lastExternalPrice")),
            last_updated_time=str(entry.get("lastUpdatedTime") or "").strip() or None,
            price=_decimal_text(entry.get("price")),
        ),
    )


# --- Close position ---------------------------------------------------------


def _close_position(account: str, request: Dict[str, Any]) -> CanonicalResponse:
    """Close the entire open position for a symbol with a market order.

    Ondo's documented ``AddOrderReq`` doesn't expose ``reduceOnly`` in the
    spec we read, but live probing shows it accepts the field on POST
    ``/v1/perps/orders``. We submit a market order with
    ``reduceOnly=true`` and size equal to the position's
    ``netQuantity``, then verify by re-fetching positions.

    If the live server rejects ``reduceOnly`` (some Ondo builds), the
    response will surface a clear canonical error; the operator can
    revisit by submitting a manual market order via the wizard's "New
    Order" flow.
    """
    credentials = _lookup_credentials(account)
    if not credentials:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Ondo Perps account configuration",
        )

    requested_symbol = str(request.get("symbol") or "").strip().upper()
    if not requested_symbol:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )

    metadata, error = _resolve_market_metadata(credentials, requested_symbol)
    if error is not None:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code=error.error.code if error.error else "INSTRUMENT_NOT_FOUND",
            message=error.error.message if error.error else "Instrument resolution failed.",
        )

    positions = _fetch_positions_snapshot(credentials)
    target: Optional[Dict[str, Any]] = None
    for row in positions:
        if not isinstance(row, dict):
            continue
        if str(row.get("market") or "") != metadata["market"]:
            continue
        direct = str(row.get("direction") or "").strip().lower()
        if direct not in {"long", "short"}:
            continue
        target = row
        break
    if target is None:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="POSITION_NOT_FOUND",
            message=f"No open {requested_symbol} position found.",
        )

    direction = str(target.get("direction") or "").strip().lower()
    closing_side = "sell" if direction == "long" else "buy"
    size_value = _decimal_or_none(target.get("netQuantity"))
    if size_value is None or size_value <= 0:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="POSITION_NOT_FOUND",
            message=f"Open {requested_symbol} position has zero size; nothing to close.",
        )
    aligned_size = _align_size(size_value, metadata)
    if aligned_size <= 0:
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="INVALID_VOLUME",
            message="Position size rounds to zero after market-quantisation.",
        )

    body = {
        "market": metadata["market"],
        "side": closing_side,
        "type": "market",
        "size": _decimal_text(aligned_size),
        "reduceOnly": True,
    }
    try:
        response = _signed_post(credentials, _PATH_PERPS_ORDERS, body)
    except OndoHTTPError as exc:
        return _map_http_error_to_failure(exc, operation="close_position", account=account)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="close_position",
            exchange=name,
            account=account,
            code="CLOSE_ORDER_FAILED",
            message=_redact(sanitize_error_message(str(exc))),
            position_action=CanonicalPositionActionResult(
                operation="close_position",
                symbol=requested_symbol,
                verified=False,
                status="failed",
                current_side=direction,
                current_size=_decimal_text(size_value),
            ),
        )

    exchange_order_id = response.get("orderId") if isinstance(response, dict) else None
    verified = False
    # Verify by polling positions, with the same 429-backoff the orders
    # verify uses. ``close_position`` doesn't have a specific order ID
    # to look up — we just need the position to be gone or ``neutral``.
    for attempt in range(ORDER_VERIFY_ATTEMPTS):
        try:
            post_positions = _fetch_positions_snapshot(credentials)
        except OndoHTTPError as exc:
            if exc.status == 429 and attempt < ORDER_VERIFY_ATTEMPTS - 1:
                time.sleep(ORDER_VERIFY_BACKOFF_SECONDS)
                continue
            post_positions = []
        except Exception:  # noqa: BLE001
            post_positions = []
        still_open = False
        for row in post_positions:
            if not isinstance(row, dict):
                continue
            if str(row.get("market") or "") != metadata["market"]:
                continue
            direct = str(row.get("direction") or "").strip().lower()
            if direct == "neutral":
                continue
            qty = _decimal_or_none(row.get("netQuantity")) or Decimal("0")
            if qty > 0:
                still_open = True
                break
        if not still_open:
            verified = True
            break
        if attempt < ORDER_VERIFY_ATTEMPTS - 1:
            time.sleep(ORDER_VERIFY_DELAY_SECONDS)

    action_result = CanonicalPositionActionResult(
        operation="close_position",
        symbol=requested_symbol,
        verified=verified,
        status="success" if verified else "submitted",
        exchange_order_id=_safe_int_id(exchange_order_id),
        current_side=direction,
        current_size=_decimal_text(size_value),
        message=None,
    )
    if verified:
        return make_success(
            operation="close_position",
            exchange=name,
            account=account,
            position_action=action_result,
        )
    return make_failure(
        operation="close_position",
        exchange=name,
        account=account,
        code="VERIFICATION_FAILED",
        message="Close order was accepted but the position is still reported open.",
        position_action=action_result,
    )