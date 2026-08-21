"""Lighter exchange agent.

This module owns Lighter-specific behavior for the /trade stack.

Current scope:
- Credential discovery from ``LIGHTER_<ACCOUNT>_*`` variables in the live
  environment or ``$HERMES_HOME/.env``.
- Dynamic account discovery across the Arbitrum and Robinhood deployments.
- Authenticated account balance retrieval through Lighter's account endpoint.
- Canonical conversion into the exchange-agnostic TradeDesk / wizard contract.

TradeDesk and the Telegram wizard MUST remain exchange-agnostic and must not
parse ``LIGHTER_*`` environment variables or Lighter-native payloads.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import math
import os
import re
import threading
import time
from collections import deque
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

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
    make_failure,
    make_success,
    normalize_balance,
    sanitize_error_message,
)

logger = logging.getLogger(__name__)

LIGHTER_MAX_CLIENT_ORDER_INDEX = (1 << 48) - 1
LIGHTER_VERIFY_ATTEMPTS = 4
LIGHTER_VERIFY_DELAY_SECONDS = 0.25
LIGHTER_CLOSE_MAX_SLIPPAGE = 0.05

# Lighter enforces a per-L1 sliding-window rate limit
# ("40 requests per 60 second is allowed" → 23000). We honour it
# with a process-wide sliding-window limiter, one bucket per
# (chain, account_index), so multiple accounts in the same process
# do not steal each other's quota.
LIGHTER_RATELIMIT_MAX_REQUESTS = 40
LIGHTER_RATELIMIT_WINDOW_SECONDS = 60.0
LIGHTER_RATELIMIT_BACKOFF_CAP_SECONDS = 30.0

# ---------------------------------------------------------------------------
# L2 transaction-type limit (separate from REST request rate limit).
# Per current official Lighter docs (apidocs.lighter.xyz/docs/trading.md),
# ALL account types are subject to transaction-type limits. The default
# is 40 transactions / minute for Standard accounts; sendTxBatch with n
# L2CreateOrder transactions counts as n against this budget.
#
# We track L2 transaction counts in a separate sliding-window so we can
# refuse to submit a batch whose n + recent_count would exceed the safe
# limit. Operational safety: keep below the hard observed limit.
# ---------------------------------------------------------------------------
LIGHTER_L2_TX_HARD_LIMIT = 40       # observed live (200-order test)
LIGHTER_L2_TX_SAFE_LIMIT = 30       # operational safety margin
LIGHTER_L2_TX_WINDOW_SECONDS = 60.0

# Ladder transport tuning. Exchange-local knobs — wizard, tradedesk,
# and ladder math are deliberately unaware of them.
#
# We submit ordinary independent L2CreateOrder transactions through
# REST sendTxBatch. Each batch is one HTTP request that carries up
# to ``LIGHTER_SEND_TX_BATCH_SIZE`` independently-signed transactions
# (one per ladder child), each with its own nonce and tx_hash.
#
# Why sendTxBatch and not create_grouped_orders:
#   * create_grouped_orders uses TxTypeL2CreateGroupedOrders, which
#     has a hard protocol cap of MaxGroupedOrderCount=3 per
#     transaction (verified against the official Lighter-Go source
#     and the live backend: an attempt with 9 children was rejected
#     with code 21742 "invalid order group size").
#   * sendTxBatch carries N separate TxTypeL2CreateOrder transactions
#     and has no documented protocol cap on the REST endpoint.
#
# The L1 quota cost is N transactions per sendTxBatch request
# (per the Lighter docs), and each sendTxBatch HTTP request itself
# consumes 1 sendTx/sendTxBatch quota slot. For a 200-child ladder
# at batch size 30, that's 7 HTTP requests carrying 200 L2CreateOrder
# transactions; the rolling 30-tx/60s budget paces them to one full
# batch per 60-second window.
#
# KAM safety invariants:
#   1. Each ``client_order_index`` is pre-assigned uniquely BEFORE
#      any sign. It is the authoritative reconciliation key.
#   2. ONE write attempt per sendTxBatch. NO automatic retry. NO
#      blind resend. A 429 or any non-200 envelope stops the ladder
#      and triggers a read-only reconcile of the batches that landed.
#   3. Pacing is preventive throttling — not a retry mechanism.
LIGHTER_SEND_TX_BATCH_SIZE = 30
LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS = 3.0

# Cancellation batching (L2CancelOrder, tx_type=15). Each sendTxBatch
# carries up to ``LIGHTER_CANCEL_TX_BATCH_SIZE`` independently-signed
# cancel transactions in ONE HTTP request. The L1-address transaction
# quota is shared with the create path (n txs consume n quota), so the
# same ``_LighterL2TxBudget`` (safe_limit=30 / 60s) governs cancels.
LIGHTER_CANCEL_TX_BATCH_SIZE = 30
LIGHTER_CANCEL_TX_BATCH_PAUSE_SECONDS = 3.0

name = "lighter"


class _LighterSlidingWindowLimiter:
    """Threadsafe sliding-window rate limiter.

    Tracks request timestamps in a deque and enforces that no more
    than ``max_requests`` calls happen within any rolling
    ``window_seconds`` window. ``acquire()`` blocks (sleeps) until a
    slot is available; ``record_failure(retry_after)`` extends the
    window's tail so a 429-rejected request still counts and we
    sleep at least ``retry_after`` seconds before the next attempt.
    """

    __slots__ = ("max_requests", "window_seconds", "_hits", "_lock")

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window_seconds = float(window_seconds)
        self._hits: "deque[float]" = deque()
        self._lock = threading.Lock()

    def _evict_locked(self) -> None:
        cutoff = time.monotonic() - self.window_seconds
        while self._hits and self._hits[0] < cutoff:
            self._hits.popleft()

    def acquire(self) -> float:
        """Block until a slot is available, then record the request.

        Returns the number of seconds waited (0 if no waiting needed).
        """
        waited = 0.0
        while True:
            with self._lock:
                self._evict_locked()
                if len(self._hits) < self.max_requests:
                    self._hits.append(time.monotonic())
                    return waited
                sleep_for = self.window_seconds - (time.monotonic() - self._hits[0])
            sleep_for = max(sleep_for, 0.0) + 0.01  # tiny jitter margin
            time.sleep(sleep_for)
            waited += sleep_for

    def record_failure(self, retry_after: Optional[float] = None) -> None:
        """Mark that the just-attempted request was 429-rejected.

        Lighter counts 429-rejected requests against the same window,
        so we keep the most-recent hit "fresh" by extending its tail
        forward by ``retry_after`` (or the configured cap). The next
        ``acquire()`` will therefore honour the server's hint.
        """
        backoff = float(retry_after) if retry_after else self.window_seconds
        backoff = max(0.0, min(backoff, LIGHTER_RATELIMIT_BACKOFF_CAP_SECONDS))
        with self._lock:
            self._evict_locked()
            fresh = time.monotonic()
            self._hits.append(fresh + backoff)
            self._evict_locked()


_LIGHTER_LIMITERS: Dict[Tuple[str, int], _LighterSlidingWindowLimiter] = {}
_LIGHTER_LIMITERS_LOCK = threading.Lock()


class _LighterL2TxBudget:
    """Threadsafe rolling-window tracker for L2 transactions.

    Lighter enforces a per-account-type transaction-type limit (default
    40/min for Standard accounts). One sendTxBatch with N L2CreateOrder
    transactions consumes N from this budget. This tracker lets us
    refuse to submit a batch whose N + recent would exceed the safe
    limit, instead of relying on a fixed sleep.
    """

    __slots__ = ("safe_limit", "window_seconds", "_hits", "_lock")

    def __init__(self, safe_limit: int, window_seconds: float) -> None:
        self.safe_limit = int(safe_limit)
        self.window_seconds = float(window_seconds)
        self._hits: "deque[float]" = deque()
        self._lock = threading.Lock()

    def _evict_locked(self) -> None:
        cutoff = time.monotonic() - self.window_seconds
        while self._hits and self._hits[0] < cutoff:
            self._hits.popleft()

    def current_usage(self) -> int:
        with self._lock:
            self._evict_locked()
            return len(self._hits)

    def can_acquire(self, n: int) -> bool:
        with self._lock:
            self._evict_locked()
            return len(self._hits) + n <= self.safe_limit

    def wait_for_capacity(self, n: int) -> float:
        """Block until ``n`` more slots are available in the rolling window.

        Concurrency-safe: the lock is held only to (a) prune entries,
        (b) check capacity, and (c) atomically reserve slots. The
        sleep happens OUTSIDE the lock so concurrent consumers are
        not blocked.

        Returns the number of seconds waited.
        """
        waited = 0.0
        while True:
            with self._lock:
                self._evict_locked()
                if len(self._hits) + n <= self.safe_limit:
                    # Atomically reserve n slots.
                    now = time.monotonic()
                    for _ in range(n):
                        self._hits.append(now)
                    return waited
                # Compute earliest safe release under the lock.
                sleep_for = self.window_seconds - (
                    time.monotonic() - self._hits[0]
                )
            # Sleep OUTSIDE the lock so concurrent consumers can
            # observe the budget.
            sleep_for = max(sleep_for, 0.0) + 0.01
            time.sleep(sleep_for)
            waited += sleep_for

    def rollback(self, n: int) -> None:
        """Reverse the most-recent N reservations atomically. Used
        when the batch envelope was rejected BEFORE the server
        consumed any transactions (e.g. HTTP 429 with envelope
        code 23000). Conservative — only call when the rejection is
        definitively pre-sequencing.

        Concurrency-safe: rollback is atomic under the lock.
        """
        with self._lock:
            # Cap at current_usage so concurrent consumers cannot
            # over-rollback across thread boundaries.
            rollback_n = min(n, len(self._hits))
            for _ in range(rollback_n):
                self._hits.pop()


_LIGHTER_L2_TX_BUDGETS: Dict[Tuple[str, int], _LighterL2TxBudget] = {}
_LIGHTER_L2_TX_BUDGETS_LOCK = threading.Lock()


def _get_lighter_l2_tx_budget(credentials: Dict[str, Any]) -> _LighterL2TxBudget:
    """Return the process-wide L2-tx budget tracker for one Lighter L1.

    Identity-key limitation (intentional, see also Lighter docs):

    The live Lighter 429 rejection explicitly names the L1Address as
    the rate-limit domain ("L1Address ratelimit reached 0x..."). The
    correct production key would therefore be
    ``(chain, normalized_l1_address)``.

    The current credential/config model does NOT expose the L1Address
    (no ``l1_address`` field on the resolved account). Pulling it
    would require a new ``account/by/index`` round-trip on every
    budget lookup, which would itself consume rate-limit slots and
    defeat the purpose of the budget.

    Therefore, the key used here is ``(chain, account_index)`` as a
    conservative approximation. This is sufficient for the single-
    account configuration we currently operate (the ROBIN account
    has exactly one ``account_index`` per chain). It does NOT
    guarantee cross-subaccount coordination: if a future Lighter
    account_type / subaccount shares the same L1Address with another
    subaccount on the same chain, those subaccounts would each get
    their own budget here and could jointly exceed the L1 limit.

    When the credential model gains a stable wallet/L1 owner address
    field, replace ``account_index`` with the normalized L1Address
    and the key will become cross-subaccount safe.
    """
    chain = str(credentials.get("chain") or "").strip().upper()
    account_index = int(credentials.get("account_index") or 0)
    key = (chain, account_index)
    with _LIGHTER_L2_TX_BUDGETS_LOCK:
        budget = _LIGHTER_L2_TX_BUDGETS.get(key)
        if budget is None:
            budget = _LighterL2TxBudget(
                LIGHTER_L2_TX_SAFE_LIMIT,
                LIGHTER_L2_TX_WINDOW_SECONDS,
            )
            _LIGHTER_L2_TX_BUDGETS[key] = budget
        return budget


def _get_lighter_limiter(credentials: Dict[str, Any]) -> _LighterSlidingWindowLimiter:
    """Return the process-wide limiter for one Lighter L1 address."""
    chain = str(credentials.get("chain") or "").strip().upper()
    account_index = int(credentials.get("account_index") or 0)
    key = (chain, account_index)
    with _LIGHTER_LIMITERS_LOCK:
        limiter = _LIGHTER_LIMITERS.get(key)
        if limiter is None:
            limiter = _LighterSlidingWindowLimiter(
                LIGHTER_RATELIMIT_MAX_REQUESTS,
                LIGHTER_RATELIMIT_WINDOW_SECONDS,
            )
            _LIGHTER_LIMITERS[key] = limiter
        return limiter


_LIGHTER_429_RETRY_AFTER_RE = re.compile(r"retry[- ]after[^0-9]*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)


_LIGHTER_429_MAX_RETRIES = 3


def _run_with_lighter_ratelimit(
    credentials: Dict[str, Any],
    runner: Any,
    *,
    max_retries: int = _LIGHTER_429_MAX_RETRIES,
) -> Any:
    """Run ``runner`` under the per-L1 sliding-window limiter.

    ``runner`` is a zero-arg callable (typically ``lambda:
    asyncio.run(coro)``) that performs one Lighter RPC. We acquire a
    window slot, execute, and on 429 we record the failure, sleep for
    the server-suggested (or default) backoff, and retry — without
    raising to the caller unless we exhaust retries. Non-rate-limit
    errors propagate immediately without consuming extra slots.

    Pass ``max_retries=0`` to disable retries entirely (the limiter
    still paces the call, but a 429 propagates immediately). This is
    the contract ladder batches rely on — KAM safety invariant is
    one write attempt → reconcile, never a blind resend.
    """
    limiter = _get_lighter_limiter(credentials)
    last_exc: Optional[BaseException] = None
    for attempt in range(max_retries + 1):
        limiter.acquire()
        try:
            return runner()
        except BaseException as exc:  # noqa: BLE001
            retry_after = _looks_like_lighter_429(exc)
            if retry_after is None or attempt >= max_retries:
                raise
            limiter.record_failure(retry_after)
            last_exc = exc
            # Small floor so a missing Retry-After hint still leaves
            # the server breathing room before we re-acquire.
            time.sleep(max(retry_after, 0.5))
    # Unreachable in practice; defensive only.
    if last_exc is not None:
        raise last_exc
    return None


def _run_lighter_coro_blocking(
    credentials: Dict[str, Any],
    coro_factory: Any,
    *,
    thread_name: str,
    max_retries: int = 0,
) -> Any:
    """Run a Lighter coroutine to completion from a synchronous caller,
    safe whether or not the calling thread already has a running event
    loop (the Hermes/Telegram gateway loop).

    Bridge semantics (mirrors the established pattern in
    ``_mint_auth_token`` / ``_submit_new_order`` / ``_submit_tpsl_order``
    / ``_execute_close_position``):

      * No running loop on this thread (direct script / CLI context):
        run the coroutine under the per-L1 sliding-window limiter via
        ``asyncio.run`` — the standard one-shot bridge.

      * A loop IS already running on this thread (Hermes gateway loop):
        NEVER call ``asyncio.run`` here (RuntimeError: "asyncio.run()
        cannot be called from a running event loop"). Instead offload
        the whole ``asyncio.run`` to a dedicated worker thread that has
        no running loop, and synchronously join it for the result.

    The coroutine object is constructed lazily *inside* the target
    execution context (via ``coro_factory``), so no coroutine/future is
    ever created on one loop and awaited on another. The
    ``_run_with_lighter_ratelimit`` wrapper (and its limiter acquire) is
    applied in the same context that runs the coroutine, keeping the
    rate-limit accounting consistent.

    ``coro_factory`` is a zero-arg callable returning the coroutine to
    run (e.g. ``lambda: _run_batch()``); it is invoked only inside the
    chosen execution context.
    """
    def _invoke() -> Any:
        return _run_with_lighter_ratelimit(
            credentials,
            lambda: asyncio.run(coro_factory()),
            max_retries=max_retries,
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop on this thread — safe to asyncio.run directly.
        return _invoke()

    # A loop is already running on this thread (Hermes gateway). Offload
    # to a worker thread with no running loop and bridge the result.
    result: Dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["value"] = _invoke()
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=_runner, name=thread_name, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _looks_like_lighter_429(exc: BaseException) -> Optional[float]:
    """Best-effort: detect Lighter's 23000 rate-limit error.

    Returns the retry-after hint to use, or ``None`` if this is not a
    429. We look for the documented error code, the literal phrase
    "Too Many Requests", or a numeric ``code:23000`` substring in the
    exception text. The HTTP body rarely carries a proper Retry-After
    header on Lighter, so we also accept the bare 1.5s "ladder"
    floor when nothing better is available.

    Also catches the Lighter SDK's behaviour of returning a successful
    tuple but with ``api_response.code == 23000`` — we wrap such cases
    in a synthetic ``_LighterRateLimitError`` whose ``__str__`` includes
    the canonical code+message so detection is uniform.
    """
    # Direct class match — synthetic error we raise ourselves.
    if isinstance(exc, _LighterRateLimitError):
        return _LIGHTER_429_DEFAULT_RETRY_AFTER

    text = ""
    for source in (exc, getattr(exc, "__cause__", None), getattr(exc, "__context__", None)):
        if source is None:
            continue
        try:
            text = str(source) or text
        except Exception:  # noqa: BLE001
            continue
    if not text:
        return None
    lowered = text.lower()
    # pydantic ValidationError on a 200-OK-with-error-body response from
    # Lighter surfaces as a missing-tx_hash error — treat that as a 429
    # because the only way tx_hash is missing on /send_tx is when the
    # backend rejected the order without submitting it (rate-limit or
    # similar error envelope).
    if (
        "23000" not in text
        and "too many requests" not in lowered
        and "ratelimit" not in lowered
        and "rate limit" not in lowered
        and "tx_hash" not in lowered
    ):
        return None
    match = _LIGHTER_429_RETRY_AFTER_RE.search(text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass
    return _LIGHTER_429_DEFAULT_RETRY_AFTER


_LIGHTER_429_DEFAULT_RETRY_AFTER = 1.5


class _LighterRateLimitError(RuntimeError):
    """Raised when Lighter's backend reports a 23000 rate-limit response.

    The Lighter SDK does not raise on rate-limit responses; it returns a
    successful tuple with ``api_response.code == 23000``. We wrap the
    call site to inspect the code and raise this synthetic error so the
    outer ``_run_with_lighter_ratelimit`` retry loop can see it.
    """

    def __init__(self, code: int, message: str, status_code: Optional[int] = None) -> None:
        if status_code:
            prefix = f"Lighter rate limit (HTTP {status_code}, code {code}): "
        else:
            prefix = f"Lighter rate limit (code {code}): "
        super().__init__(f"{prefix}{message}")
        self.code = code
        self.backend_message = message
        self.status_code = status_code


def _classify_lighter_api_response(api_response: Any) -> None:
    """Inspect an SDK ``api_response`` and raise on rate-limit / error codes.

    Classification precedence (post-fix):

      1. Structured success: ``code == 200`` is the authoritative
         success signal. A successful envelope is NEVER reclassified
         by textual heuristics against its body. The Lighter backend
         returns informational keys (e.g. ``"ratelimit":
         "didn't use volume quota"``) inside successful 200
         responses; these are status hints, not errors.

      2. Structured rate-limit: ``code == 23000`` (Lighter's documented
         rate-limit code) is a definitive rate-limit signal.

      3. HTTP-level rate-limit: ``status_code == 429`` is a definitive
         rate-limit signal, even when the body's ``code`` is something
         else (defensive against future code changes).

      4. Textual fallback: when the response shape is malformed (no
         ``code`` field, no ``status_code``, no parseable number) we
         fall back to the documented substring signals: ``"too many
         requests"``, the literal code ``"23000"``, or the
         SDK-style ``Retry-After`` hint.

    The pre-fix behaviour was:
        "too many requests" OR "ratelimit" in lowered message → 429

    The pre-fix classifier was wrong because Lighter returns
    ``{"ratelimit": "didn't use volume quota"}`` in successful
    ``code=200`` envelopes (confirmed during the 20-order live test
    cleanup). The substring ``"ratelimit"`` is part of an
    informational JSON key, NOT an error signal. After this fix,
    a successful envelope with ``code == 200`` is always SUCCESS
    regardless of body content.
    """
    if api_response is None:
        return
    status_code = (
        getattr(api_response, "status_code", None)
        or getattr(api_response, "status", None)
    )
    code = getattr(api_response, "code", None)
    message = str(getattr(api_response, "message", "") or "").strip()

    try:
        code_int = int(code) if code is not None else None
    except (TypeError, ValueError):
        code_int = None

    # ------------------------------------------------------------------
    # Step 1: structured success.
    # ------------------------------------------------------------------
    # ``code == 200`` is the authoritative success signal from Lighter's
    # backend. Any 200 response with a valid tx_hash is SUCCESS — even
    # if its informational body happens to contain text like "ratelimit"
    # or "rate limit" describing quota usage (e.g. "didn't use volume
    # quota"). Those are status hints, not error signals.
    if code_int == 200:
        return

    # ------------------------------------------------------------------
    # Step 2 + 3: structured rate-limit.
    # ------------------------------------------------------------------
    # Documented backend code 23000 is the Lighter rate-limit signal.
    # HTTP 429 is the standard rate-limit response. Either is sufficient
    # on its own.
    if code_int == 23000:
        raise _LighterRateLimitError(
            code=code_int,
            message=message or "Too Many Requests",
            status_code=status_code if isinstance(status_code, int) else None,
        )
    if status_code == 429:
        raise _LighterRateLimitError(
            code=code_int or 23000,
            message=message or "Too Many Requests",
            status_code=status_code,
        )

    # ------------------------------------------------------------------
    # Step 4: textual fallback for malformed responses.
    # ------------------------------------------------------------------
    # Only when we have NO structured evidence (no code, no status_code,
    # OR a non-numeric code that we couldn't parse) do we fall back to
    # textual detection. A structured non-200 code that is NOT 23000 or
    # 429 should not be reclassified here — it carries its own semantics.
    structured_signal_available = (
        code_int is not None
        or status_code is not None
    )
    if structured_signal_available:
        # The backend told us something specific (e.g. 21739
        # insufficient margin, 21706 invalid order, etc.) but it was
        # not a rate-limit. Surface that to the caller without raising.
        return

    # Malformed response — fall back to textual heuristics, but be
    # much stricter than the old substring match.
    lowered = message.lower()
    textual_429 = (
        "too many requests" in lowered
        or '"code":23000' in lowered
        or 'code":23000' in lowered
        or 'code:23000' in lowered
    )
    if textual_429:
        raise _LighterRateLimitError(
            code=23000,
            message=message or "Too Many Requests",
            status_code=status_code if isinstance(status_code, int) else None,
        )

    # Non-rate-limit backend errors are surfaced as a normal SDK error
    # by the caller (the SDK's create_order / cancel_order only set
    # the ``error`` tuple element for sign-time failures, not send-time).
    # We don't raise here — the verifier will detect the missing order.

LIGHTER_REQUIRED_FIELDS = (
    "CHAIN",
    "ACCOUNT_INDEX",
    "APIKEY_INDEX",
    "PUBLIC_KEY",
    "PRIVATE_KEY",
)
LIGHTER_CHAIN_URLS = {
    "ARBITRUM": "https://mainnet.zklighter.elliot.ai",
    "ROBINHOOD": "https://api.rh.lighter.xyz",
}
_URL_TO_CHAIN = {url.rstrip("/"): chain for chain, url in LIGHTER_CHAIN_URLS.items()}
_LIGHTER_ALIAS_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_LIGHTER_INT_PATTERN = re.compile(r"^[0-9]+$")
_LIGHTER_HEX_PATTERN = re.compile(r"^(0x)?[0-9a-fA-F]+$")


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
            value = value[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        values[key] = value
    return values


def _combined_lighter_env() -> Dict[str, str]:
    values: Dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith("LIGHTER_"):
            values[key] = (value or "").strip()
    for key, value in _load_dotenv_values(_hermes_home() / ".env").items():
        if key.startswith("LIGHTER_"):
            values.setdefault(key, (value or "").strip())
    return values


def _normalize_chain(value: Any) -> Optional[str]:
    chain = str(value or "").strip().upper()
    return chain if chain in LIGHTER_CHAIN_URLS else None


def _is_strict_int(value: Any) -> bool:
    return bool(_LIGHTER_INT_PATTERN.match(str(value or "").strip()))


def _is_hex(value: Any) -> bool:
    text = str(value or "").strip()
    if not text or not _LIGHTER_HEX_PATTERN.match(text):
        return False
    try:
        int(text[2:] if text.lower().startswith("0x") else text, 16)
    except ValueError:
        return False
    return True


def _display_chain(chain: str) -> str:
    lowered = str(chain or "").strip().lower()
    if lowered == "arbitrum":
        return "Arbitrum"
    if lowered == "robinhood":
        return "Robinhood"
    return str(chain or "").strip().title()


def _discover_account_entries() -> List[Dict[str, str]]:
    grouped: Dict[str, Dict[str, str]] = {}
    for key, value in _combined_lighter_env().items():
        if not key.startswith("LIGHTER_"):
            continue
        remainder = key[len("LIGHTER_"):]
        for field in LIGHTER_REQUIRED_FIELDS:
            suffix = f"_{field}"
            if not remainder.endswith(suffix):
                continue
            alias = remainder[: -len(suffix)]
            if not alias or not _LIGHTER_ALIAS_PATTERN.match(alias) or not value:
                break
            grouped.setdefault(alias, {})[field] = value
            break

    discovered: List[Dict[str, str]] = []
    for alias in sorted(grouped.keys()):
        entry = grouped[alias]
        if any(not entry.get(field) for field in LIGHTER_REQUIRED_FIELDS):
            continue
        chain = _normalize_chain(entry.get("CHAIN"))
        if not chain:
            continue
        if not _is_strict_int(entry.get("ACCOUNT_INDEX")):
            continue
        if not _is_strict_int(entry.get("APIKEY_INDEX")):
            continue
        if not _is_hex(entry.get("PUBLIC_KEY")) or not _is_hex(entry.get("PRIVATE_KEY")):
            continue
        account = alias.lower()
        discovered.append(
            {
                "account": account,
                "chain": chain,
                "label": f"{account} — {_display_chain(chain)}",
            }
        )
    return discovered


def _lookup_credentials(account: str) -> Optional[Dict[str, Any]]:
    alias = str(account or "").strip().upper()
    if not alias or not _LIGHTER_ALIAS_PATTERN.match(alias):
        return None
    env = _combined_lighter_env()
    values = {field: str(env.get(f"LIGHTER_{alias}_{field}", "")).strip() for field in LIGHTER_REQUIRED_FIELDS}
    if any(not values[field] for field in LIGHTER_REQUIRED_FIELDS):
        return None
    chain = _normalize_chain(values["CHAIN"])
    if not chain or not _is_strict_int(values["ACCOUNT_INDEX"]) or not _is_strict_int(values["APIKEY_INDEX"]):
        return None
    if not _is_hex(values["PUBLIC_KEY"]) or not _is_hex(values["PRIVATE_KEY"]):
        return None
    return {
        "account": alias.lower(),
        "chain": chain,
        "label": f"{alias.lower()} — {_display_chain(chain)}",
        "account_index": int(values["ACCOUNT_INDEX"]),
        "api_key_index": int(values["APIKEY_INDEX"]),
        "public_key": values["PUBLIC_KEY"],
        "private_key": values["PRIVATE_KEY"],
        "base_url": LIGHTER_CHAIN_URLS[chain].rstrip("/"),
    }


def list_accounts() -> List[Dict[str, str]]:
    return _discover_account_entries()


def capabilities() -> List[str]:
    return ["balance", "positions_orders", "positions_management", "new_order", "ladder", "cancel_orders"]


def _build_signer_client(credentials: Dict[str, Any]) -> Any:
    candidates = [
        ("lighter", "SignerClient"),
        ("lighter_sdk", "SignerClient"),
        ("zklighter", "SignerClient"),
    ]
    last_error: Optional[Exception] = None
    for module_name, attr in candidates:
        try:
            module = importlib.import_module(module_name)
            signer_cls = getattr(module, attr)
            return signer_cls(
                credentials["base_url"],
                credentials["account_index"],
                {credentials["api_key_index"]: credentials["private_key"]},
            )
        except ModuleNotFoundError as exc:
            last_error = exc
            continue
        except AttributeError as exc:
            last_error = exc
            continue
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Lighter SignerClient initialization failed: {exc}") from exc
    raise RuntimeError(
        "Lighter SignerClient is unavailable. Install the official Lighter SDK before using the lighter agent."
    ) from last_error


def _mint_auth_token(credentials: Dict[str, Any]) -> str:
    async def _create_token() -> str:
        signer = _build_signer_client(credentials)
        try:
            result = signer.create_auth_token_with_expiry(api_key_index=credentials["api_key_index"])
            if isinstance(result, tuple):
                token = result[0] if len(result) >= 1 else None
                err = result[1] if len(result) >= 2 else None
            else:
                token = result
                err = None
            if err:
                raise RuntimeError(f"Lighter auth token generation failed: {err}")
            token_text = str(token or "").strip()
            if not token_text:
                raise RuntimeError("Lighter auth token generation returned an empty token")
            return token_text
        finally:
            api_client = getattr(signer, "api_client", None)
            if api_client is not None and hasattr(api_client, "close"):
                try:
                    await api_client.close()
                except Exception:
                    pass

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run_with_lighter_ratelimit(credentials, lambda: asyncio.run(_create_token()))

    result: Dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["token"] = _run_with_lighter_ratelimit(credentials, lambda: asyncio.run(_create_token()))
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=_runner, name="lighter-auth-token", daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return str(result.get("token") or "")


# Lighter auth tokens are valid for ~10 minutes. Caching them avoids
# burning a rate-limit slot on every accountActiveOrders read (the
# ladder verifier used to call mint 4x per ladder). The cache lives
# process-wide and is keyed on (chain, account_index, api_key_index).
_LIGHTER_AUTH_TOKEN_TTL_SECONDS = 8 * 60
_LIGHTER_AUTH_TOKEN_CACHE: Dict[Tuple[str, int, int], Tuple[float, str]] = {}
_LIGHTER_AUTH_TOKEN_LOCK = threading.Lock()


def _mint_auth_token_cached(credentials: Dict[str, Any]) -> str:
    """Return a fresh or cached Lighter auth token.

    Tokens are reused across calls for ``_LIGHTER_AUTH_TOKEN_TTL_SECONDS``
    seconds, after which a new one is minted. The first call always mints;
    subsequent calls within the TTL window reuse the cached value without
    consuming a rate-limit slot. Concurrent callers serialize through
    ``_LIGHTER_AUTH_TOKEN_LOCK`` so only one mint runs at a time and
    every caller receives the same token within a window.

    The function NEVER returns an empty string. If minting fails or
    produces an empty token, the underlying exception is re-raised so
    callers cannot accidentally proceed with an empty Authorization
    header. Caching only happens with a non-empty token.
    """
    chain = str(credentials.get("chain") or "").strip().upper()
    account_index = int(credentials.get("account_index") or 0)
    api_key_index = int(credentials.get("api_key_index") or 0)
    cache_key = (chain, account_index, api_key_index)
    with _LIGHTER_AUTH_TOKEN_LOCK:
        cached = _LIGHTER_AUTH_TOKEN_CACHE.get(cache_key)
        if cached is not None:
            expires_at, token_text = cached
            if time.monotonic() < expires_at and token_text:
                return token_text
        # Cache miss or expired: mint while holding the lock so
        # concurrent callers wait for our mint instead of each minting.
        # (The underlying _mint_auth_token itself acquires a per-L1
        # rate-limit slot, which serialises at the Lighter layer too.)
        token_text = _mint_auth_token(credentials)
        # Strip happens below; refuse to cache or return an empty token.
        # The caller will see the exception and stop the read/verify
        # operation explicitly rather than silently making an
        # unauthorized request.
        if not token_text:
            raise RuntimeError(
                "Lighter auth token mint returned an empty result; "
                "refusing to proceed (see _mint_auth_token error)"
            )
        _LIGHTER_AUTH_TOKEN_CACHE[cache_key] = (
            time.monotonic() + _LIGHTER_AUTH_TOKEN_TTL_SECONDS,
            token_text,
        )
        return token_text


def _fetch_account_entry(request: Dict[str, Any]) -> Dict[str, Any]:
    account_name = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account_name)
    if not credentials:
        raise RuntimeError("Unknown or invalid Lighter account configuration")

    auth_token = _mint_auth_token_cached(credentials)
    limiter = _get_lighter_limiter(credentials)
    response = None
    try:
        limiter.acquire()
        response = requests.get(
            f"{credentials['base_url']}/api/v1/account",
            params={
                "by": "index",
                "value": str(credentials["account_index"]),
                "auth": auth_token,
            },
            headers={"Accept": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        retry_after = _looks_like_lighter_429(exc)
        if retry_after is not None:
            limiter.record_failure(retry_after)
            time.sleep(retry_after)
        raise
    payload = response.json()
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        raise RuntimeError("Lighter account response missing accounts[]")

    target = None
    for entry in accounts:
        if not isinstance(entry, dict):
            continue
        raw_entry_index = entry.get("account_index")
        if raw_entry_index is None:
            continue
        try:
            entry_index = int(str(raw_entry_index))
        except Exception:  # noqa: BLE001
            continue
        if entry_index == credentials["account_index"]:
            target = entry
            break
    if target is None:
        raise RuntimeError("Configured Lighter account_index was not present in the account response")

    return {
        "_raw": payload,
        "target": target,
        "account": credentials["account"],
        "chain": credentials["chain"],
        "label": credentials["label"],
        "credentials": credentials,
        "auth_token": auth_token,
    }


def _quantize_2(value: Any) -> str:
    decimal_value = Decimal(str(value or "0"))
    return format(decimal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


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
    rendered = format(decimal_value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".") or "0"
    return rendered


def _format_decimal_places(value: Decimal, places: int) -> str:
    quantum = Decimal("1").scaleb(-places)
    quantized = value.quantize(quantum, rounding=ROUND_HALF_UP)
    if places <= 0:
        return format(quantized, ".0f")
    return format(quantized, f".{places}f")


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


def _fetch_market_symbol_map(base_url: str) -> Dict[int, Dict[str, Any]]:
    limiter = _get_lighter_limiter({"chain": _URL_TO_CHAIN.get(base_url, ""), "account_index": 0})
    try:
        limiter.acquire()
        response = requests.get(
            f"{base_url}/api/v1/orderBookDetails",
            headers={"Accept": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        retry_after = _looks_like_lighter_429(exc)
        if retry_after is not None:
            limiter.record_failure(retry_after)
        raise
    payload = response.json()
    combined: List[Dict[str, Any]] = []
    if isinstance(payload.get("order_book_details"), list):
        combined.extend(item for item in payload.get("order_book_details") or [] if isinstance(item, dict))
    if isinstance(payload.get("spot_order_book_details"), list):
        combined.extend(item for item in payload.get("spot_order_book_details") or [] if isinstance(item, dict))
    market_map: Dict[int, Dict[str, Any]] = {}
    for entry in combined:
        try:
            market_id = int(entry.get("market_id"))
        except Exception:  # noqa: BLE001
            continue
        symbol = str(entry.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        market_map[market_id] = {
            "symbol": symbol,
            "price_precision": entry.get("price_decimals") or entry.get("supported_price_decimals"),
        }
    return market_map


def _fetch_market_catalog(base_url: str) -> List[Dict[str, Any]]:
    limiter = _get_lighter_limiter({"chain": _URL_TO_CHAIN.get(base_url, ""), "account_index": 0})
    try:
        limiter.acquire()
        response = requests.get(
            f"{base_url}/api/v1/orderBookDetails",
            headers={"Accept": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        retry_after = _looks_like_lighter_429(exc)
        if retry_after is not None:
            limiter.record_failure(retry_after)
        raise
    payload = response.json()
    combined: List[Dict[str, Any]] = []
    if isinstance(payload.get("order_book_details"), list):
        combined.extend(item for item in payload.get("order_book_details") or [] if isinstance(item, dict))
    if isinstance(payload.get("spot_order_book_details"), list):
        combined.extend(item for item in payload.get("spot_order_book_details") or [] if isinstance(item, dict))
    return combined


_LIGHTER_QUOTE_SUFFIXES = {"USD", "USDT", "USDC", "PERP"}


def _lighter_alias_keys(symbol: str) -> List[str]:
    raw = str(symbol or "").strip().upper()
    keys: List[str] = []
    if raw:
        keys.append(raw)
    if "-" in raw:
        base, rest = raw.split("-", 1)
        if rest in _LIGHTER_QUOTE_SUFFIXES and base:
            keys.append(base)
    return keys


def _resolve_market(base_url: str, requested_symbol: str) -> Optional[Dict[str, Any]]:
    symbol = str(requested_symbol or "").strip().upper()
    if not symbol:
        return None
    keys = _lighter_alias_keys(symbol)
    exact = []
    aliased = []
    for entry in _fetch_market_catalog(base_url):
        entry_symbol = str(entry.get("symbol") or "").strip().upper()
        if not entry_symbol:
            continue
        if entry_symbol == symbol:
            exact.append(entry)
        elif entry_symbol in keys:
            aliased.append(entry)
    candidates = exact or aliased
    if not candidates:
        return None
    if not exact:
        unique_ids = {int(str(item.get("market_id") or 0) or 0) for item in aliased}
        if len(unique_ids) > 1:
            return None
    candidates.sort(
        key=lambda item: (
            0 if str(item.get("market_type") or "").strip().lower() == "perp" else 1,
            0 if str(item.get("status") or "").strip().lower() == "active" else 1,
            int(item.get("market_id") or 0),
        )
    )
    chosen = dict(candidates[0])
    try:
        chosen["market_id"] = int(chosen.get("market_id"))
    except Exception:  # noqa: BLE001
        return None
    return chosen


def _quantize_down(value: Decimal, places: int) -> Decimal:
    quantum = Decimal("1").scaleb(-max(0, places))
    return value.quantize(quantum, rounding=ROUND_DOWN)


def _to_scaled_int(value: Decimal, places: int) -> int:
    scale = Decimal(10) ** max(0, places)
    scaled = (value * scale).quantize(Decimal("1"), rounding=ROUND_DOWN)
    return int(scaled)


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


def _build_ladder_prices(start_price: Decimal, end_price: Decimal, order_count: int, price_decimals: int) -> List[Decimal]:
    if order_count <= 0:
        return []
    if order_count == 1:
        return [_quantize_down((start_price + end_price) / Decimal("2"), price_decimals)]
    step = (end_price - start_price) / Decimal(order_count - 1)
    prices = [_quantize_down(start_price + (step * Decimal(index)), price_decimals) for index in range(order_count)]
    if start_price <= end_price:
        for index in range(1, len(prices)):
            if prices[index] < prices[index - 1]:
                prices[index] = prices[index - 1]
    else:
        for index in range(1, len(prices)):
            if prices[index] > prices[index - 1]:
                prices[index] = prices[index - 1]
    return prices


def _allocate_ladder_sizes(total_volume: Decimal, order_count: int, size_decimals: int, distribution: str) -> tuple[List[Decimal], Decimal]:
    increment = Decimal("1").scaleb(-max(0, size_decimals))
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


def _build_lighter_ladder_children(*, side: str, distribution: str, order_count: int, total_volume: Decimal, start_price: Decimal, end_price: Decimal, size_decimals: int, price_decimals: int, min_base_amount: Optional[Decimal]) -> tuple[List[Dict[str, Decimal]], Decimal, int]:
    prices = _build_ladder_prices(start_price, end_price, order_count, price_decimals)
    sizes, submitted_volume = _allocate_ladder_sizes(total_volume, order_count, size_decimals, distribution)
    children: List[Dict[str, Decimal]] = []
    omitted_below_minimum = 0
    for price, size in zip(prices, sizes):
        if size <= 0:
            continue
        if min_base_amount is not None and size < min_base_amount:
            omitted_below_minimum += 1
            continue
        if children and children[-1]["price"] == price:
            children[-1]["size"] = children[-1]["size"] + size
            continue
        children.append({"price": price, "size": size})
    kept_volume = sum((child["size"] for child in children), Decimal("0"))
    return children, kept_volume, omitted_below_minimum


def _submit_cancel_order(
    credentials: Dict[str, Any],
    order: Optional[Dict[str, Any]] = None,
    *,
    market_id: Optional[int] = None,
    order_id: Optional[int] = None,
    reason: str = "cancel",
) -> Dict[str, Any]:
    resolved_market_id = market_id
    resolved_order_id = order_id
    if order is not None:
        try:
            resolved_market_id = int(str(order.get("market_index") or resolved_market_id or 0))
        except Exception:  # noqa: BLE001
            pass
        try:
            resolved_order_id = int(str(order.get("order_index") or order.get("order_id") or resolved_order_id or 0))
        except Exception:  # noqa: BLE001
            pass
    if int(resolved_market_id or 0) <= 0 or int(resolved_order_id or 0) <= 0:
        raise RuntimeError(f"Lighter order cancellation failed: missing order metadata for {reason}")

    async def _run_cancel() -> Dict[str, Any]:
        signer = _build_signer_client(credentials)
        try:
            tx, api_response, error = await signer.cancel_order(
                int(resolved_market_id),
                int(resolved_order_id),
                api_key_index=credentials["api_key_index"],
            )
            if error:
                raise RuntimeError(f"Lighter order cancellation failed: {error}")
            # The SDK returns a successful tuple even when the backend
            # rejected the cancellation (e.g. 23000 rate-limit). Inspect
            # the api_response so the retry layer can see the 429.
            _classify_lighter_api_response(api_response)
            return {
                "exchange_order_id": int(resolved_order_id),
                "nonce": getattr(tx, "nonce", None),
                "tx_hash": getattr(api_response, "tx_hash", None),
            }
        finally:
            api_client = getattr(signer, "api_client", None)
            if api_client is not None and hasattr(api_client, "close"):
                try:
                    await api_client.close()
                except Exception:
                    pass

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run_with_lighter_ratelimit(credentials, lambda: asyncio.run(_run_cancel()))

    result: Dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["value"] = _run_with_lighter_ratelimit(credentials, lambda: asyncio.run(_run_cancel()))
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=_runner, name="lighter-cancel-order", daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return dict(result.get("value") or {})


def _execute_cancel_order_group(request: Dict[str, Any]) -> CanonicalResponse:
    account_name = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account_name)
    if not credentials:
        return make_failure(operation="cancel_order_group", exchange=name, account=account_name, code="UNKNOWN_ACCOUNT", message="Unknown or invalid Lighter account configuration")

    requested_symbol = str(request.get("symbol") or "").strip().upper()
    requested_side = str(request.get("side") or "").strip().lower()
    if not requested_symbol:
        return make_failure(operation="cancel_order_group", exchange=name, account=account_name, code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_side not in {"buy", "sell"}:
        return make_failure(operation="cancel_order_group", exchange=name, account=account_name, code="INVALID_SIDE", message="Side must be buy or sell.")

    market = _resolve_market(credentials["base_url"], requested_symbol)
    if market is None:
        return make_failure(operation="cancel_order_group", exchange=name, account=account_name, code="INSTRUMENT_NOT_FOUND", message="Instrument not found.")
    market_id_raw = market.get("market_id")
    market_id = int(str(market_id_raw or 0))

    # Optional exact-identity targeting (spec: cancel ONLY the known
    # test orders, never sweep broadly). When ``order_ids`` is supplied,
    # ONLY those OIDs are candidates; every other active order is
    # treated as baseline and must be preserved. The explicit set is
    # still intersected with active orders matching symbol+side so a
    # stale / foreign OID can never be cancelled by mistake.
    explicit_ids_raw = request.get("order_ids")
    explicit_ids: Optional[set[int]] = None
    if explicit_ids_raw is not None:
        if not isinstance(explicit_ids_raw, (list, tuple, set)):
            return make_failure(operation="cancel_order_group", exchange=name, account=account_name, code="INVALID_ORDER_IDS", message="order_ids must be a list of integer order ids.")
        explicit_ids = set()
        for raw in explicit_ids_raw:
            try:
                explicit_ids.add(int(raw))
            except Exception:  # noqa: BLE001
                return make_failure(operation="cancel_order_group", exchange=name, account=account_name, code="INVALID_ORDER_IDS", message=f"order_ids contains a non-integer value: {raw!r}")

    try:
        pre_orders = _fetch_active_orders(credentials, _mint_auth_token_cached(credentials))
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="cancel_order_group", exchange=name, account=account_name, code="OPEN_ORDERS_UNAVAILABLE", message=sanitize_error_message(str(exc)))

    target_orders: List[Dict[str, Any]] = []
    target_ids: List[int] = []
    non_target_ids: List[int] = []
    for order in pre_orders:
        if not isinstance(order, dict):
            continue
        try:
            order_market_id = int(str(order.get("market_index") or 0))
        except Exception:  # noqa: BLE001
            continue
        raw_order_id = str(order.get("order_id") or "").strip()
        try:
            parsed_order_id = int(raw_order_id)
        except Exception:  # noqa: BLE001
            continue
        side = "sell" if bool(order.get("is_ask")) else "buy"
        matches_scope = order_market_id == market_id and side == requested_side
        if explicit_ids is not None:
            # Exact-identity mode: only an OID in the explicit set AND
            # matching the symbol+side scope is a target.
            if matches_scope and parsed_order_id in explicit_ids:
                target_orders.append(order)
                target_ids.append(parsed_order_id)
            else:
                non_target_ids.append(parsed_order_id)
        elif matches_scope:
            target_orders.append(order)
            target_ids.append(parsed_order_id)
        else:
            non_target_ids.append(parsed_order_id)

    if not target_orders:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account_name,
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

    # ------------------------------------------------------------------
    # Batched, budgeted cancellation (L2CancelOrder, tx_type=15).
    #
    # Replaces the prior per-order loop that fired one L2CancelOrder
    # HTTP request per order with no preventive rolling budget. Now
    # each sendTxBatch carries up to ``LIGHTER_CANCEL_TX_BATCH_SIZE``
    # independently-signed cancel transactions in ONE HTTP request, and
    # the shared ``_LighterL2TxBudget`` (safe_limit=30 / 60s) paces the
    # rolling L1-address transaction quota. One attempt per batch; on
    # 429 / rejection / ambiguity we STOP and reconcile — no auto-retry.
    # ------------------------------------------------------------------
    accepted_count = 0
    submitted_count = 0
    stop_reason: Optional[Dict[str, Any]] = None
    batches: List[Dict[str, Any]] = []
    chunk_starts = list(range(0, len(target_ids), LIGHTER_CANCEL_TX_BATCH_SIZE))
    for batch_index, start in enumerate(chunk_starts):
        # Preventive pacing — only between batches, never before the
        # first or after the last.
        if batch_index > 0:
            time.sleep(LIGHTER_CANCEL_TX_BATCH_PAUSE_SECONDS)
        chunk_ids = target_ids[start:start + LIGHTER_CANCEL_TX_BATCH_SIZE]
        outcome = _submit_cancel_tx_batch(
            credentials=credentials,
            market_id=market_id,
            order_ids=chunk_ids,
        )
        submitted_count += int(outcome.get("submitted_count") or 0)
        accepted_count += int(outcome.get("accepted_count") or 0)
        batches.append({
            "batch_index": batch_index,
            "order_ids": list(chunk_ids),
            "submitted": int(outcome.get("submitted_count") or 0),
            "accepted": int(outcome.get("accepted_count") or 0),
            "rejected": int(outcome.get("rejected_count") or 0),
            "unknown": int(outcome.get("unknown_count") or 0),
            "outcome": outcome.get("outcome"),
            "api_code": outcome.get("api_code"),
            "api_message": outcome.get("api_message"),
            "per_tx": outcome.get("per_tx"),
        })
        # Continuation gate: continue ONLY if the batch fully succeeded.
        if outcome.get("outcome") != _LADDER_BATCH_OUTCOME_SUCCESS:
            stop_reason = outcome
            break

    # ------------------------------------------------------------------
    # Post-cancel reconciliation — exact target identity.
    #   * target OIDs must be ABSENT (cancelled)
    #   * baseline (non-target) OIDs must be PRESENT (preserved)
    # Do NOT infer success from HTTP code=200 alone.
    # ------------------------------------------------------------------
    try:
        post_orders = _fetch_active_orders(credentials, _mint_auth_token_cached(credentials))
    except Exception as exc:  # noqa: BLE001
        post_orders = []
        if stop_reason is None:
            stop_reason = {
                "outcome": _LADDER_BATCH_OUTCOME_AMBIGUOUS,
                "api_message": sanitize_error_message(str(exc)),
            }

    post_ids: set[int] = set()
    for order in post_orders:
        if not isinstance(order, dict):
            continue
        raw_order_id = str(order.get("order_id") or "").strip()
        try:
            post_ids.add(int(raw_order_id))
        except Exception:  # noqa: BLE001
            continue

    remaining_target_count = sum(1 for oid in target_ids if oid in post_ids)
    confirmed_absent_count = len(target_ids) - remaining_target_count
    non_target_preserved = all(oid in post_ids for oid in non_target_ids)

    cancelled_count = confirmed_absent_count
    verified_cancel_count = confirmed_absent_count
    rate_limited = bool(
        stop_reason is not None
        and stop_reason.get("outcome") == _LADDER_BATCH_OUTCOME_RATE_LIMITED
    )
    partial = confirmed_absent_count < len(target_ids)
    exchange_reason: Optional[str] = None
    if stop_reason is not None:
        exchange_reason = sanitize_lighter_message(
            str(stop_reason.get("api_message") or "")
        ) or None

    # verified = every target authoritatively confirmed absent AND all
    # baseline orders preserved AND no stop_reason.
    verified = (
        remaining_target_count == 0
        and non_target_preserved
        and stop_reason is None
    )
    if stop_reason is None:
        status = "success" if verified else ("partial" if confirmed_absent_count else "failed")
        error_code: Optional[str] = None
    else:
        status = "partial" if confirmed_absent_count else "failed"
        oc = stop_reason.get("outcome")
        if oc == _LADDER_BATCH_OUTCOME_RATE_LIMITED:
            error_code = "RATE_LIMITED"
        elif oc == _LADDER_BATCH_OUTCOME_AMBIGUOUS:
            error_code = "CANCEL_AMBIGUOUS"
        else:
            error_code = "CANCEL_REJECTED"

    cancel_result = CanonicalCancelGroupResult(
        symbol=requested_symbol,
        side=requested_side,
        targeted_order_count=len(target_ids),
        cancelled_order_count=cancelled_count,
        confirmed_absent_count=confirmed_absent_count,
        remaining_target_count=remaining_target_count,
        verified=verified,
        partial=partial,
        status=status,
        batch_count=len(batches),
        batches=batches or None,
        requested_cancel_count=len(target_ids),
        verified_cancel_count=verified_cancel_count,
        rate_limited=rate_limited,
        exchange_reason=exchange_reason,
    )
    if verified:
        return make_success(operation="cancel_order_group", exchange=name, account=account_name, cancel_group=cancel_result)
    return make_failure(
        operation="cancel_order_group",
        exchange=name,
        account=account_name,
        code=error_code or "VERIFICATION_FAILED",
        message=exchange_reason or "Cancellation was only partially completed.",
        cancel_group=cancel_result,
    )


def _allocate_client_order_indices(count: int) -> List[int]:
    """Pre-assign deterministic, unique ``client_order_index`` values.

    Each value is drawn from ``time.time_ns()`` and constrained to the
    SDK's 48-bit signed-positive range. Uniqueness is critical: the
    reconciler matches landed orders to ladder children by this exact
    value, so a duplicate would silently merge two children into one.
    """
    if count <= 0:
        return []
    indices: List[int] = []
    seen: set[int] = set()
    # LCG step (Knuth) — uniform enough for ID generation. We mix in
    # ``time.time_ns()`` so two ladder runs are extremely unlikely to
    # collide even back-to-back, and walk the sequence with a Knuth
    # LCG until we've collected ``count`` unique 48-bit-safe values.
    seed = time.time_ns() & 0xFFFFFFFFFFFFFFFF
    cursor = seed
    while len(indices) < count:
        candidate = int(cursor % LIGHTER_MAX_CLIENT_ORDER_INDEX)
        if candidate <= 0:
            candidate = 1
        if candidate not in seen:
            indices.append(candidate)
            seen.add(candidate)
        cursor = (cursor * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
    return indices


_LADDER_BATCH_OUTCOME_SUCCESS = "success"
_LADDER_BATCH_OUTCOME_RATE_LIMITED = "rate_limited"
_LADDER_BATCH_OUTCOME_EXCHANGE_REJECTED = "exchange_rejected"
_LADDER_BATCH_OUTCOME_AMBIGUOUS = "ambiguous"
_LADDER_BATCH_OUTCOME_INSUFFICIENT_MARGIN = "insufficient_margin"
_LADDER_BATCH_OUTCOME_INVALID_ORDER = "invalid_order"
_LADDER_BATCH_OUTCOME_PRICE_TOO_FAR = "price_too_far_from_mark"


async def _reserve_ladder_nonces(
    *,
    signer: Any,
    api_key_index: int,
    count: int,
) -> List[int]:
    """Reserve ``count`` consecutive nonces for ``api_key_index``.

    Returns a list of nonces in order. Each call to
    ``nonce_manager.async_next_nonce(api_key_index)`` increments the
    manager's local counter by one. We hold the key's per-key lock
    for the duration so concurrent callers can't interleave our
    reservation. Caller MUST ``acknowledge_failure(api_key_index)``
    if any of the subsequent sign operations fail, so the reserved
    nonces are released.
    """
    reserved: List[int] = []
    async with signer.nonce_manager.lock(api_key_index):
        for _ in range(count):
            _, nonce = await signer.nonce_manager.async_next_nonce(api_key_index)
            reserved.append(int(nonce))
    return reserved


# ---------------------------------------------------------------------------
# Per-transaction outcome classification
# ---------------------------------------------------------------------------
# The native ``sendTxBatch`` response shape (RespSendTxBatch, per the
# official OpenAPI spec at apidocs.lighter.xyz/reference/sendtxbatch.md
# and the installed lighter Python SDK model) carries these fields:
#
#   code: int (envelope status, 200 = accepted at API/sign layer)
#   message: optional str
#   tx_hash: list of str (one entry per submitted tx, in submitted order)
#   predicted_execution_time_ms: int
#   volume_quota_remaining: int
#
# The official Lighter docs (apidocs.lighter.xyz/docs/trading.md) state:
#
#   "Orders that have the correct syntax will be accepted by the API
#   servers, returning code=200. This does not guarantee the
#   execution of your order, as the sequencer could still reject it
#   if parameters are not set properly."
#
# Therefore ``tx_hash[i]`` presence ONLY proves that the API server
# accepted/signed tx i. It does NOT prove the order landed. The only
# authoritative landing proof is the ``accountActiveOrders`` query
# matched by ``client_order_index``.
#
# The ``sendTxBatch`` envelope has NO per-child definitive rejection
# field. There is no way to know from the response alone which
# children were rejected by the sequencer — only reconciliation can.
# ---------------------------------------------------------------------------
_LIGHTER_PER_TX_API_ACCEPTED = "API_ACCEPTED"  # tx_hash[i] present in HTTP 200 envelope
_LIGHTER_PER_TX_API_REJECTED = "API_REJECTED"  # envelope-level backend rejection (all children)
_LIGHTER_PER_TX_UNKNOWN = "UNKNOWN"            # no native signal — must reconcile

# Concrete backend code → canonical outcome. The set below covers
# envelope-level codes that apply to every submitted child when the
# envelope itself is rejected. Unknown codes fall back to a generic
# EXCHANGE_REJECTED outcome.
_LIGHTER_BACKEND_CODE_TO_OUTCOME: Dict[int, str] = {
    21739: _LADDER_BATCH_OUTCOME_INSUFFICIENT_MARGIN,
    21706: _LADDER_BATCH_OUTCOME_INVALID_ORDER,
    21743: _LADDER_BATCH_OUTCOME_INVALID_ORDER,
    21734: _LADDER_BATCH_OUTCOME_PRICE_TOO_FAR,
}


def _extract_backend_code_from_message(message: str) -> Optional[int]:
    """Pull the first integer token out of a JSON-style error body.

    ``message`` may look like ``{"code":21739,"message":"..."}``.
    """
    if not message:
        return None
    m = re.search(r'"code"\s*:\s*(\d+)', message)
    if m:
        try:
            return int(m.group(1))
        except (TypeError, ValueError):
            return None
    return None


# L1Address pattern: 0x followed by EXACTLY 40 hex chars, bounded on
# both sides by a non-hex char (or string boundary). This prevents the
# regex from matching the first 40 hex chars of a 64-hex tx hash.
# Bare 64-hex (tx hash without 0x) and 0x+64-hex (tx hash with 0x)
# are intentionally preserved.
_HEX_CHARS = re.compile(r"[0-9a-fA-F]")
_L1_ADDRESS_RE = re.compile(
    r"(?<![0-9a-fA-F])0x[0-9a-fA-F]{40}(?![0-9a-fA-F])"
)
# Fallback: bare 0x + 40 hex not preceded/followed by more hex
# (defensive in case the negative lookbehind is not supported in some
# Python regex builds — duplicated for clarity).
_L1_ADDRESS_RE_BARE = re.compile(r"0x[0-9a-fA-F]{40}\b")


def sanitize_lighter_message(message: str) -> str:
    """Strip L1 addresses from a Lighter error message.

    The Lighter backend includes the L1Address in its 23000 body and
    occasionally in other error envelopes. Operators and Telegram users
    must never see the L1Address. Transaction hashes (64-hex) and order
    indices are intentionally retained.

    Returns the original string if no L1 address is present, otherwise
    the address is replaced with ``[L1Address]``.
    """
    if not message:
        return ""
    # Try the strict pattern first
    if _L1_ADDRESS_RE.search(message):
        return _L1_ADDRESS_RE.sub("[L1Address]", message)
    # Fallback: in case the negative-lookbehind regex doesn't match,
    # we additionally scan for a non-bounded 40-hex match and check
    # the surrounding chars before substituting.
    m = _L1_ADDRESS_RE_BARE.search(message)
    if not m:
        return message
    start, end = m.span()
    # Validate that the next 64-hex-and-the-first-40 pattern does not
    # extend a longer token (i.e. the captured 40 hex chars are NOT
    # the prefix of a 64-hex tx hash).
    before_ok = start == 0 or not _HEX_CHARS.match(message[start - 1])
    after_end = end
    after_ok = after_end >= len(message) or not _HEX_CHARS.match(message[after_end])
    if before_ok and after_ok:
        return message[:start] + "[L1Address]" + message[end:]
    return message


def _extract_backend_code_from_exception(exc: BaseException) -> Optional[int]:
    """Best-effort extraction of a Lighter backend error code from an
    SDK ``BadRequestException``.

    The SDK's ``BadRequestException`` exposes ``.body`` (a JSON string
    like ``{"code":21739,"message":"..."}``) and ``.status`` (HTTP code).
    """
    body = getattr(exc, "body", None)
    if isinstance(body, str):
        code = _extract_backend_code_from_message(body)
        if code is not None:
            return code
    msg = str(exc)
    return _extract_backend_code_from_message(msg)


# --------------------------------------------------------------------------
def _classify_send_tx_batch_per_tx(
    *,
    client_order_indices: List[int],
    api_response: Any,
    envelope_code: int,
    envelope_message: str,
) -> List[Dict[str, Any]]:
    """Classify each submitted transaction into one of three states
    using the native ``sendTxBatch`` response shape.

    Returns a list in submitted order; one entry per child with keys
    ``index``, ``client_order_index``, ``status``, ``tx_hash``,
    ``reason``.

    States (see the long comment block above for primary sources):
      * ``API_ACCEPTED``  — envelope=200 AND tx_hash[i] is a non-empty
        string. Proves only that the API server accepted the
        signature; does NOT prove the order landed.
      * ``API_REJECTED``  — envelope is non-200 with a Lighter backend
        code (e.g. 21739 / 21706 / 21743 / 21734). The envelope
        itself rejected the whole batch; ALL children inherit this.
      * ``UNKNOWN``       — envelope=200 but tx_hash[i] is null/empty/
        absent. There is no native signal for this child. The
        authoritative answer comes from ``accountActiveOrders``
        reconciliation.

    No child is ever classified as DEFINITIVELY landed by this
    function alone. ``LANDED`` is reserved for the reconciliation
    pass that matches ``client_order_index`` against
    ``accountActiveOrders``.
    """
    out: List[Dict[str, Any]] = []
    # Fetch the per-tx hash list defensively. The SDK's StrictStr list
    # can fail to materialise when the response contains null entries
    # — fall back to additional_properties in that case.
    response_tx_hashes: List[Optional[str]] = []
    raw_hash_attr = getattr(api_response, "tx_hash", None)
    if raw_hash_attr is None:
        additional = getattr(api_response, "additional_properties", None) or {}
        raw_hash_attr = additional.get("tx_hash")
    if isinstance(raw_hash_attr, list):
        response_tx_hashes = list(raw_hash_attr)
    else:
        response_tx_hashes = []

    envelope_level_failure = envelope_code not in (None, 200)
    envelope_reason = envelope_message or ""

    for i, ci in enumerate(client_order_indices):
        # tx_hash[i] — be tolerant of null/empty entries
        tx_hash: Optional[str] = None
        if i < len(response_tx_hashes):
            h = response_tx_hashes[i]
            if isinstance(h, str) and h:
                tx_hash = h

        if envelope_level_failure:
            # Envelope rejected — every child is API_REJECTED with the
            # envelope-level reason. This is a DEFINITIVE outcome
            # because Lighter returns a concrete backend code (or
            # HTTP-level failure) for the envelope as a whole.
            out.append({
                "index": i,
                "client_order_index": int(ci),
                "status": _LIGHTER_PER_TX_API_REJECTED,
                "tx_hash": None,
                "reason": envelope_reason or "envelope rejected",
            })
            continue

        # Envelope accepted (code=200). The presence of a tx_hash
        # entry only proves the API server accepted the signature.
        if tx_hash is None:
            # No tx_hash for this child. Per official docs, code=200
            # does NOT guarantee sequencer execution. We classify this
            # as UNKNOWN — reconciliation is the only authoritative
            # answer.
            out.append({
                "index": i,
                "client_order_index": int(ci),
                "status": _LIGHTER_PER_TX_UNKNOWN,
                "tx_hash": None,
                "reason": "tx_hash absent in response; sequencer outcome unknown until reconciliation",
            })
            continue

        out.append({
            "index": i,
            "client_order_index": int(ci),
            "status": _LIGHTER_PER_TX_API_ACCEPTED,
            "tx_hash": tx_hash,
            "reason": "API accepted; landing must be verified by reconciliation",
        })
    return out


def _classify_send_tx_batch_envelope_failure(
    exc: BaseException,
) -> Dict[str, Any]:
    """Inspect a transport-level exception (e.g. SDK BadRequestException)
    and return a definite outcome for the entire batch.

    Returns ``{"outcome": <canonical>, "code": <int|None>,
              "reason": <str>, "ambiguity": <bool>}`` where ``outcome``
    is one of:
      * ``LADDER_BATCH_INSUFFICIENT_MARGIN`` (code 21739)
      * ``LADDER_BATCH_INVALID_ORDER`` (code 21706 / 21743)
      * ``LADDER_BATCH_PRICE_TOO_FAR_FROM_MARK`` (code 21734)
      * ``LADDER_BATCH_EXCHANGE_REJECTED`` (any other backend code)
      * ``LADDER_BATCH_AMBIGUOUS`` (no backend code; genuine transport
        ambiguity)
    """
    backend_code = _extract_backend_code_from_exception(exc)
    if backend_code is not None:
        if backend_code == 23000:
            canonical = _LADDER_BATCH_OUTCOME_RATE_LIMITED
        else:
            canonical = _LIGHTER_BACKEND_CODE_TO_OUTCOME.get(
                backend_code, _LADDER_BATCH_OUTCOME_EXCHANGE_REJECTED
            )
        return {
            "outcome": canonical,
            "code": backend_code,
            # Use the Lighter-specific sanitizer to strip the L1
            # address that the 23000 body explicitly includes.
            "reason": sanitize_lighter_message(str(exc)),
            "ambiguity": False,
        }
    # Genuine transport ambiguity (network drop, parse failure, etc.)
    return {
        "outcome": _LADDER_BATCH_OUTCOME_AMBIGUOUS,
        "code": None,
        "reason": sanitize_lighter_message(str(exc)),
        "ambiguity": True,
    }


def _submit_send_tx_batch(
    *,
    credentials: Dict[str, Any],
    market: Dict[str, Any],
    side: str,
    children: List[Dict[str, Decimal]],
    client_order_indices: List[int],
) -> Dict[str, Any]:
    """Submit one ``send_tx_batch`` HTTP request carrying N independent
    L2CreateOrder transactions. NO automatic retry.

    Each child is signed via ``signer.sign_create_order`` with an
    explicit nonce reserved from ``nonce_manager.async_next_nonce``.
    The signed tx_info strings and tx_types are then packed into a
    single ``signer.send_tx_batch`` HTTP call.

    On a sign-time failure for any child, we rollback ALL reserved
    nonces for this batch via ``nonce_manager.acknowledge_failure``
    so the manager's counter stays consistent. The caller is then
    free to retry or stop.

    Returned dict:
      ``outcome``            one of the ``_LADDER_BATCH_OUTCOME_*`` constants
      ``tx_hashes``          list of per-child tx hashes (from response, may
                             contain ``None`` for rejected children)
      ``per_tx``             list of per-tx outcome dicts
      ``submitted_count``    number of txs sent in the batch
      ``accepted_count``     number of txs with ACCEPTED outcome
      ``rejected_count``     number of txs with REJECTED outcome
      ``unknown_count``      number of txs with UNKNOWN outcome
      ``api_code``           envelope ``code`` (200 on success)
      ``api_message``        sanitized envelope ``message``
      ``raw_response``       the parsed ``RespSendTxBatch`` or None
      ``nonces``             per-child nonces we used (for reconciliation)
      ``child_to_tx_hash``   zip(client_order_indices, response.tx_hash)
    """
    if len(children) != len(client_order_indices):
        raise ValueError("children / client_order_indices length mismatch")
    if len(children) == 0:
        return {
            "outcome": _LADDER_BATCH_OUTCOME_SUCCESS,
            "tx_hashes": [],
            "submitted_count": 0,
            "api_code": 200,
            "api_message": "",
            "raw_response": None,
            "nonces": [],
            "child_to_tx_hash": {},
        }

    size_decimals = int(market.get("size_decimals") or market.get("supported_size_decimals") or 0)
    price_decimals = int(market.get("price_decimals") or market.get("supported_price_decimals") or 0)

    # Captured by the inner async block so the classifier can read
    # .body and .status on the original SDK exception in addition
    # to the sanitized string.
    last_send_tx_batch_exc_holder: List[Any] = [None]

    async def _run_batch() -> Dict[str, Any]:
        signer = _build_signer_client(credentials)
        api_key_index = int(credentials["api_key_index"])
        # ------------------------------------------------------------------
        # 0. L2 transaction-budget check.
        #
        # Lighter limits per-account-type L2 transactions in a rolling
        # 60-second window (40/min observed live for the Standard
        # account). sendTxBatch with N L2CreateOrder transactions
        # consumes N against this budget. We refuse to submit a batch
        # that would push us over the safe limit.
        # ------------------------------------------------------------------
        budget = _get_lighter_l2_tx_budget(credentials)
        budget.wait_for_capacity(len(children))
        # ------------------------------------------------------------------
        # 1. Reserve nonces for every child in this batch.
        # ------------------------------------------------------------------
        try:
            nonces = await _reserve_ladder_nonces(
                signer=signer,
                api_key_index=api_key_index,
                count=len(children),
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "outcome": _LADDER_BATCH_OUTCOME_AMBIGUOUS,
                "tx_hashes": [],
                "submitted_count": 0,
                "api_code": None,
                "api_message": sanitize_error_message(str(exc)),
                "raw_response": None,
                "nonces": [],
                "child_to_tx_hash": {},
                "ambiguity_reason": "nonce reservation failed",
            }

        # ------------------------------------------------------------------
        # 2. Sign each child independently.
        # ------------------------------------------------------------------
        tx_types: List[int] = []
        tx_infos: List[str] = []
        signed_hashes: List[str] = []
        sign_failed = False
        sign_failure_msg = ""
        try:
            for child, client_order_index, nonce in zip(children, client_order_indices, nonces):
                submitted_volume = _quantize_down(Decimal(child["size"]), size_decimals)
                submitted_price = _quantize_down(Decimal(child["price"]), price_decimals)
                base_amount = _to_scaled_int(submitted_volume, size_decimals)
                price_int = _to_scaled_int(submitted_price, price_decimals)
                if base_amount <= 0:
                    raise ValueError(
                        "Volume must be positive after Lighter size quantization"
                    )
                if price_int <= 0:
                    raise ValueError(
                        "Price must be positive after Lighter price quantization"
                    )
                # sign_create_order is the sync method on SignerClient
                # that returns (tx_type, tx_info, tx_hash, error)
                # without going through the network.
                tx_type, tx_info, tx_hash, error = signer.sign_create_order(
                    market_index=int(market["market_id"]),
                    client_order_index=int(client_order_index),
                    base_amount=int(base_amount),
                    price=int(price_int),
                    is_ask=side == "sell",
                    order_type=signer.ORDER_TYPE_LIMIT,
                    time_in_force=signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                    reduce_only=False,
                    trigger_price=signer.NIL_TRIGGER_PRICE,
                    order_expiry=signer.DEFAULT_28_DAY_ORDER_EXPIRY,
                    nonce=int(nonce),
                    api_key_index=api_key_index,
                )
                if error is not None:
                    sign_failed = True
                    sign_failure_msg = sanitize_error_message(str(error))
                    break
                tx_types.append(int(tx_type))
                tx_infos.append(tx_info)
                signed_hashes.append(tx_hash)
        except Exception as exc:  # noqa: BLE001
            sign_failed = True
            sign_failure_msg = sanitize_error_message(str(exc))

        if sign_failed:
            # Roll back ALL nonces we reserved for this batch.
            for _ in range(len(nonces)):
                try:
                    signer.nonce_manager.acknowledge_failure(api_key_index)
                except Exception:  # noqa: BLE001
                    pass
            return {
                "outcome": _LADDER_BATCH_OUTCOME_EXCHANGE_REJECTED,
                "tx_hashes": [],
                "submitted_count": 0,
                "api_code": None,
                "api_message": sign_failure_msg or "Sign-time failure",
                "raw_response": None,
                "nonces": list(nonces),
                "child_to_tx_hash": {},
                "ambiguity_reason": "sign failure",
            }

        # ------------------------------------------------------------------
        # 3. Submit through send_tx_batch — one HTTP request.
        # ------------------------------------------------------------------
        api_response = None
        submit_error: Optional[str] = None
        try:
            api_response = await signer.send_tx_batch(
                tx_types=tx_types, tx_infos=tx_infos
            )
        except Exception as exc:  # noqa: BLE001
            submit_error = sanitize_error_message(str(exc))
            # Capture the raw exception for the classifier below so it
            # can read .body and .status in addition to the sanitized
            # string. The exception object is also exposed via the
            # enclosing ``last_send_tx_batch_exc_holder`` list.
            last_send_tx_batch_exc_holder[0] = exc

        if submit_error is not None:
            # Transport / network exception during send_tx_batch.
            # Inspect the exception for a definite Lighter backend
            # code. If present, the outcome is concrete (e.g.
            # INSUFFICIENT_MARGIN). Only genuine transport / parse
            # failures (no backend code) get classified as AMBIGUOUS.
            #
            # Nonce policy (verified): when an envelope exception is
            # raised (e.g. BadRequestException with code 21739), the
            # Lighter backend has REJECTED the batch envelope before
            # sequencing any of the contained transactions. The
            # per-tx nonces are NOT consumed. We roll them back via
            # ``acknowledge_failure`` so the next batch's reservation
            # starts cleanly from the unchanged server counter.
            #
            # We classify from BOTH the original exception (which
            # carries .body and .status) and the sanitized string.
            envelope_failure = _classify_send_tx_batch_envelope_failure(
                last_send_tx_batch_exc_holder[0]
                if last_send_tx_batch_exc_holder[0] is not None
                else RuntimeError(submit_error)
            )
            if not envelope_failure["ambiguity"]:
                # Backend definitively rejected the batch envelope.
                # Nonces were not consumed. Roll them back.
                for _ in range(len(nonces)):
                    try:
                        signer.nonce_manager.acknowledge_failure(
                            api_key_index
                        )
                    except Exception:  # noqa: BLE001
                        pass
                return {
                    "outcome": envelope_failure["outcome"],
                    "tx_hashes": [],
                    "submitted_count": len(children),
                    "accepted_count": 0,
                    "rejected_count": len(children),
                    "unknown_count": 0,
                    "api_code": envelope_failure["code"],
                    "api_message": envelope_failure["reason"]
                    or "Backend rejected the batch",
                    "raw_response": None,
                    "nonces": list(nonces),
                    "child_to_tx_hash": {},
                    "per_tx": [
                        {
                            "index": i,
                            "client_order_index": int(ci),
                            "status": _LIGHTER_PER_TX_API_REJECTED,
                            "tx_hash": None,
                            "reason": envelope_failure["reason"]
                            or "Backend rejected the batch",
                        }
                        for i, ci in enumerate(client_order_indices)
                    ],
                }
            # Genuine transport ambiguity. Do NOT rollback — the
            # envelope MAY have been accepted by the backend and the
            # nonces consumed.
            #
            # Special case: HTTP 429 / SDK BadRequestException carrying
            # backend code 23000 is a definitive RATE_LIMITED signal,
            # not ambiguity. Promote it.
            last_send_tx_batch_exc_holder[0] = exc
            backend_code_from_exc = _extract_backend_code_from_exception(exc)
            exc_status = (
                getattr(exc, "status", None) or getattr(exc, "status_code", None)
            )
            is_rate_limit = (
                backend_code_from_exc == 23000
                or exc_status == 429
                or "Too Many Requests" in submit_error
                or "ratelimit" in submit_error.lower()
            )
            if is_rate_limit:
                # The L2 transactions for this batch never reached the
                # Lighter sequencer — the envelope was rejected at the
                # API layer. Roll back the local budget reservation
                # so a future batch can use the slots.
                budget.rollback(len(children))
                # Nonces were NOT consumed by the server because the
                # batch envelope never reached the sequencer. Roll
                # them back so the next batch's reservation starts
                # cleanly from the unchanged server counter.
                for _ in range(len(nonces)):
                    try:
                        signer.nonce_manager.acknowledge_failure(api_key_index)
                    except Exception:  # noqa: BLE001
                        pass
                # Sanitize the exception text — strip L1 addresses
                # (the Lighter backend includes the L1Address in its
                # 23000 body) and shorten to a stable, user-safe form.
                sanitized_exc_msg = sanitize_lighter_message(submit_error)
                return {
                    "outcome": _LADDER_BATCH_OUTCOME_RATE_LIMITED,
                    "tx_hashes": [],
                    "submitted_count": len(children),
                    "accepted_count": 0,
                    "rejected_count": 0,
                    "unknown_count": 0,
                    "api_code": 23000 if backend_code_from_exc == 23000 else None,
                    "api_message": sanitized_exc_msg
                    or "Too Many Requests: 40 requests per 60 second is allowed",
                    "raw_response": None,
                    "nonces": list(nonces),
                    "child_to_tx_hash": {},
                    "rate_limit_reason": "Too Many Requests: 40 requests per 60 second is allowed",
                }
            return {
                "outcome": _LADDER_BATCH_OUTCOME_AMBIGUOUS,
                "tx_hashes": [],
                "submitted_count": len(children),
                "accepted_count": 0,
                "rejected_count": 0,
                "unknown_count": len(children),
                "api_code": None,
                "api_message": submit_error,
                "raw_response": None,
                "nonces": list(nonces),
                "child_to_tx_hash": {},
                "ambiguity_reason": "transport exception during send_tx_batch; "
                "nonces NOT rolled back per Lighter nonce contract",
                "per_tx": [
                    {
                        "index": i,
                        "client_order_index": int(ci),
                        "status": _LIGHTER_PER_TX_UNKNOWN,
                        "tx_hash": None,
                        "reason": "transport exception",
                    }
                    for i, ci in enumerate(client_order_indices)
                ],
            }

        # ------------------------------------------------------------------
        # 4. Classify the envelope response.
        # ------------------------------------------------------------------
        api_code = getattr(api_response, "code", None)
        api_message = str(getattr(api_response, "message", "") or "").strip()

        try:
            code_int = int(api_code) if api_code is not None else 0
        except (TypeError, ValueError):
            code_int = 0

        # Per-tx classification using the SDK response shape. The
        # response.tx_hash list maps 1:1 to submitted transactions;
        # an absent / null entry marks a tx that the backend
        # rejected at the API server level (e.g. maker-order margin
        # check).
        per_tx = _classify_send_tx_batch_per_tx(
            client_order_indices=list(client_order_indices),
            api_response=api_response,
            envelope_code=code_int,
            envelope_message=api_message,
        )
        response_tx_hashes = [
            (entry.get("tx_hash") or "") for entry in per_tx
        ]
        accepted_count = sum(
            1 for entry in per_tx
            if entry.get("status") == _LIGHTER_PER_TX_API_ACCEPTED
        )
        rejected_count = sum(
            1 for entry in per_tx
            if entry.get("status") == _LIGHTER_PER_TX_API_REJECTED
        )
        unknown_count = sum(
            1 for entry in per_tx
            if entry.get("status") == _LIGHTER_PER_TX_UNKNOWN
        )

        # Map per-child client_order_index -> response tx_hash (may be None).
        child_to_tx_hash: Dict[int, Optional[str]] = {
            int(entry["client_order_index"]): entry.get("tx_hash")
            for entry in per_tx
        }

        # Sanitize the message — strip L1 addresses, etc.
        sanitized_msg = sanitize_lighter_message(api_message) if api_message else ""

        # Lighter's success code is 200. Anything else is a batch-level
        # rejection. Roll back nonces in that case.
        if code_int == 23000:
            for _ in range(len(nonces)):
                try:
                    signer.nonce_manager.acknowledge_failure(api_key_index)
                except Exception:  # noqa: BLE001
                    pass
            return {
                "outcome": _LADDER_BATCH_OUTCOME_RATE_LIMITED,
                "tx_hashes": response_tx_hashes,
                "submitted_count": len(children),
                "accepted_count": 0,
                "rejected_count": len(children),
                "unknown_count": 0,
                "api_code": code_int,
                "api_message": sanitized_msg or "Too Many Requests",
                "raw_response": api_response,
                "nonces": list(nonces),
                "child_to_tx_hash": child_to_tx_hash,
                "per_tx": per_tx,
            }
        if code_int != 200:
            # Envelope-level rejection (non-200, non-23000). Nonces
            # were not consumed. Roll back.
            for _ in range(len(nonces)):
                try:
                    signer.nonce_manager.acknowledge_failure(api_key_index)
                except Exception:  # noqa: BLE001
                    pass
            return {
                "outcome": _LADDER_BATCH_OUTCOME_EXCHANGE_REJECTED,
                "tx_hashes": response_tx_hashes,
                "submitted_count": len(children),
                "accepted_count": 0,
                "rejected_count": len(children),
                "unknown_count": 0,
                "api_code": code_int,
                "api_message": sanitized_msg or "Exchange rejected the batch",
                "raw_response": api_response,
                "nonces": list(nonces),
                "child_to_tx_hash": child_to_tx_hash,
                "per_tx": per_tx,
            }

        # code_int == 200 — envelope accepted. Inspect per-tx outcomes.
        #
        # Definite per-tx rejections (e.g. insufficient margin on a
        # maker order) cause the ladder to STOP. We do not send the
        # next batch — the rejected children's reason (e.g. margin
        # constraint) applies equally to remaining batches.
        #
        # Nonce policy on a 200 envelope with mixed outcomes:
        #   * accepted txs: nonces CONSUMED server-side
        #   * rejected (maker-API-rejection) txs: nonces NOT consumed
        #     server-side (per official docs)
        # We do NOT rollback any nonces. Future operations create a
        # fresh SignerClient which fetches the current server nonce
        # via ``_ensure_nonce``. No long-term state corruption.
        #
        # Choose the most severe definite outcome to report:
        #   * any INSUFFICIENT_MARGIN  →  INSUFFICIENT_MARGIN
        #   * any INVALID_ORDER       →  INVALID_ORDER
        #   * any PRICE_TOO_FAR        →  PRICE_TOO_FAR_FROM_MARK
        #   * any other REJECTED        →  EXCHANGE_REJECTED
        #   * all ACCEPTED              →  SUCCESS
        if rejected_count > 0:
            # Pick the most specific outcome. Backend code per-tx is
            # not in the response, so we map based on the envelope
            # message body (if present) or fall back to a generic
            # per-tx rejection (LADDER_BATCH_PARTIAL_PER_TX). The
            # canonical ladder code is decided in the post-loop
            # assembly; here we only pick the batch_outcome.
            envelope_msg_lower = sanitized_msg.lower()
            if "not enough margin" in envelope_msg_lower or "21739" in envelope_msg_lower:
                batch_outcome = _LADDER_BATCH_OUTCOME_INSUFFICIENT_MARGIN
            elif "price" in envelope_msg_lower and "far" in envelope_msg_lower:
                batch_outcome = _LADDER_BATCH_OUTCOME_PRICE_TOO_FAR
            elif "invalid" in envelope_msg_lower:
                batch_outcome = _LADDER_BATCH_OUTCOME_INVALID_ORDER
            else:
                # Generic per-tx rejection. Mark this as a partial-per-tx
                # stop so the ladder halts here; the canonical assembly
                # will surface LADDER_BATCH_PARTIAL_PER_TX.
                batch_outcome = _LADDER_BATCH_OUTCOME_EXCHANGE_REJECTED
        else:
            batch_outcome = _LADDER_BATCH_OUTCOME_SUCCESS

        return {
            "outcome": batch_outcome,
            "tx_hashes": response_tx_hashes,
            "submitted_count": len(children),
            "accepted_count": accepted_count,
            "rejected_count": rejected_count,
            "unknown_count": unknown_count,
            "api_code": code_int,
            "api_message": sanitized_msg,
            "raw_response": api_response,
            "nonces": list(nonces),
            "child_to_tx_hash": child_to_tx_hash,
            "per_tx": per_tx,
        }

    # Wrap in the per-L1 sliding-window limiter (1 HTTP slot per
    # send_tx_batch). A 429 inside the envelope propagates through the
    # returned dict's outcome — caller decides whether to stop.
    # No retry at this layer — KAM safety contract.
    #
    # ``_run_lighter_coro_blocking`` bridges the coroutine safely whether
    # or not the calling thread already has a running event loop (the
    # Hermes/Telegram gateway loop). It never calls ``asyncio.run`` on a
    # thread that already has a running loop.
    try:
        return _run_lighter_coro_blocking(
            credentials,
            lambda: _run_batch(),
            thread_name="lighter-send-tx-batch",
            max_retries=0,
        )
    except _LighterRateLimitError as exc:
        return {
            "outcome": _LADDER_BATCH_OUTCOME_RATE_LIMITED,
            "tx_hashes": [],
            "submitted_count": len(children),
            "api_code": 23000,
            "api_message": sanitize_error_message(str(exc)),
            "raw_response": None,
            "nonces": [],
            "child_to_tx_hash": {},
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "outcome": _LADDER_BATCH_OUTCOME_AMBIGUOUS,
            "tx_hashes": [],
            "submitted_count": 0,
            "api_code": None,
            "api_message": sanitize_error_message(str(exc)),
            "raw_response": None,
            "nonces": [],
            "child_to_tx_hash": {},
            "ambiguity_reason": "transport exception",
        }


def _submit_cancel_tx_batch(
    *,
    credentials: Dict[str, Any],
    market_id: int,
    order_ids: List[int],
) -> Dict[str, Any]:
    """Submit one ``send_tx_batch`` HTTP request carrying N independent
    L2CancelOrder transactions (tx_type=15). NO automatic retry.

    Mirrors ``_submit_send_tx_batch`` but for cancellations. Each
    target order is signed via ``signer.sign_cancel_order`` with an
    explicit nonce reserved from ``nonce_manager.async_next_nonce``.
    The signed tx_info strings and tx_types are packed into a single
    ``signer.send_tx_batch`` HTTP call, so N cancellations cost ONE
    HTTP request — but N against the L1-address transaction quota.

    Rate-limit scope (primary sources):
      * Lighter enforces a per-L1Address rolling 60s **per-transaction**
        quota ("40 requests per 60 second is allowed"; "n transactions
        in SendTxBatch consume n quota"). L2CancelOrder (tx_type=15)
        draws from the SAME L1-address transaction budget as
        L2CreateOrder (tx_type=14). We therefore reuse the shared
        ``_LighterL2TxBudget`` keyed ``(chain, account_index)``.

    Targeting: EXACT order identity only (``order_ids``). Never
    cancel-all / market-wide / side-wide.

    Returned dict mirrors ``_submit_send_tx_batch``:
      ``outcome``            one of the ``_LADDER_BATCH_OUTCOME_*`` constants
      ``tx_hashes``          per-target tx hashes (response, may hold None)
      ``per_tx``             per-target outcome dicts (keyed on order_id)
      ``submitted_count``    number of cancel txs sent
      ``accepted_count``     number of txs with ACCEPTED outcome
      ``rejected_count``     number of txs with REJECTED outcome
      ``unknown_count``      number of txs with UNKNOWN outcome
      ``api_code``           envelope ``code`` (200 on success)
      ``api_message``        sanitized envelope ``message`` (L1-redacted)
      ``raw_response``       parsed ``RespSendTxBatch`` or None
      ``nonces``             per-target nonces used
      ``order_to_tx_hash``   zip(order_ids, response.tx_hash)
    """
    if len(order_ids) == 0:
        return {
            "outcome": _LADDER_BATCH_OUTCOME_SUCCESS,
            "tx_hashes": [],
            "submitted_count": 0,
            "api_code": 200,
            "api_message": "",
            "raw_response": None,
            "nonces": [],
            "order_to_tx_hash": {},
            "per_tx": [],
        }

    last_send_tx_batch_exc_holder: List[Any] = [None]

    async def _run_batch() -> Dict[str, Any]:
        signer = _build_signer_client(credentials)
        api_key_index = int(credentials["api_key_index"])
        # ------------------------------------------------------------------
        # 0. L2 transaction-budget check (shared L1-address quota).
        # N L2CancelOrder transactions consume N against the same rolling
        # 60s budget used by the ladder create path.
        # ------------------------------------------------------------------
        budget = _get_lighter_l2_tx_budget(credentials)
        budget.wait_for_capacity(len(order_ids))
        # ------------------------------------------------------------------
        # 1. Reserve nonces for every cancel in this batch.
        # ------------------------------------------------------------------
        try:
            nonces = await _reserve_ladder_nonces(
                signer=signer,
                api_key_index=api_key_index,
                count=len(order_ids),
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "outcome": _LADDER_BATCH_OUTCOME_AMBIGUOUS,
                "tx_hashes": [],
                "submitted_count": 0,
                "api_code": None,
                "api_message": sanitize_error_message(str(exc)),
                "raw_response": None,
                "nonces": [],
                "order_to_tx_hash": {},
                "per_tx": [],
                "ambiguity_reason": "nonce reservation failed",
            }

        # ------------------------------------------------------------------
        # 2. Sign each cancel independently via sign_cancel_order.
        # ------------------------------------------------------------------
        tx_types: List[int] = []
        tx_infos: List[str] = []
        signed_hashes: List[str] = []
        sign_failed = False
        sign_failure_msg = ""
        try:
            for order_id, nonce in zip(order_ids, nonces):
                # sign_cancel_order is the sync method on SignerClient
                # that returns (tx_type, tx_info, tx_hash, error)
                # without going through the network. tx_type is 15
                # (L2CancelOrder) per the native signer.
                tx_type, tx_info, tx_hash, error = signer.sign_cancel_order(
                    int(market_id),
                    int(order_id),
                    signer.SKIP_NONCE_OFF,
                    int(nonce),
                    api_key_index,
                )
                if error is not None:
                    sign_failed = True
                    sign_failure_msg = sanitize_error_message(str(error))
                    break
                tx_types.append(int(tx_type))
                tx_infos.append(tx_info)
                signed_hashes.append(tx_hash)
        except Exception as exc:  # noqa: BLE001
            sign_failed = True
            sign_failure_msg = sanitize_error_message(str(exc))

        if sign_failed:
            # Roll back ALL nonces reserved for this batch.
            for _ in range(len(nonces)):
                try:
                    signer.nonce_manager.acknowledge_failure(api_key_index)
                except Exception:  # noqa: BLE001
                    pass
            return {
                "outcome": _LADDER_BATCH_OUTCOME_EXCHANGE_REJECTED,
                "tx_hashes": [],
                "submitted_count": 0,
                "api_code": None,
                "api_message": sign_failure_msg or "Sign-time failure",
                "raw_response": None,
                "nonces": list(nonces),
                "order_to_tx_hash": {},
                "per_tx": [],
                "ambiguity_reason": "sign failure",
            }

        # ------------------------------------------------------------------
        # 3. Submit through send_tx_batch — one HTTP request.
        # ------------------------------------------------------------------
        api_response = None
        submit_error: Optional[str] = None
        try:
            api_response = await signer.send_tx_batch(
                tx_types=tx_types, tx_infos=tx_infos
            )
        except Exception as exc:  # noqa: BLE001
            submit_error = sanitize_error_message(str(exc))
            last_send_tx_batch_exc_holder[0] = exc

        if submit_error is not None:
            envelope_failure = _classify_send_tx_batch_envelope_failure(
                last_send_tx_batch_exc_holder[0]
                if last_send_tx_batch_exc_holder[0] is not None
                else RuntimeError(submit_error)
            )
            if not envelope_failure["ambiguity"]:
                # Backend definitively rejected the batch envelope.
                # Nonces were not consumed. Roll them back.
                for _ in range(len(nonces)):
                    try:
                        signer.nonce_manager.acknowledge_failure(api_key_index)
                    except Exception:  # noqa: BLE001
                        pass
                return {
                    "outcome": envelope_failure["outcome"],
                    "tx_hashes": [],
                    "submitted_count": len(order_ids),
                    "accepted_count": 0,
                    "rejected_count": len(order_ids),
                    "unknown_count": 0,
                    "api_code": envelope_failure["code"],
                    "api_message": envelope_failure["reason"]
                    or "Backend rejected the batch",
                    "raw_response": None,
                    "nonces": list(nonces),
                    "order_to_tx_hash": {},
                    "per_tx": [
                        {
                            "index": i,
                            "order_id": int(oid),
                            "status": _LIGHTER_PER_TX_API_REJECTED,
                            "tx_hash": None,
                            "reason": envelope_failure["reason"]
                            or "Backend rejected the batch",
                        }
                        for i, oid in enumerate(order_ids)
                    ],
                }
            # Genuine transport ambiguity. Promote definite rate-limit.
            last_send_tx_batch_exc_holder[0] = exc
            backend_code_from_exc = _extract_backend_code_from_exception(exc)
            exc_status = (
                getattr(exc, "status", None) or getattr(exc, "status_code", None)
            )
            is_rate_limit = (
                backend_code_from_exc == 23000
                or exc_status == 429
                or "Too Many Requests" in submit_error
            )
            if is_rate_limit:
                # Envelope never reached the sequencer. Roll back budget
                # slots and nonces so a future batch can use them.
                budget.rollback(len(order_ids))
                for _ in range(len(nonces)):
                    try:
                        signer.nonce_manager.acknowledge_failure(api_key_index)
                    except Exception:  # noqa: BLE001
                        pass
                sanitized_exc_msg = sanitize_lighter_message(submit_error)
                return {
                    "outcome": _LADDER_BATCH_OUTCOME_RATE_LIMITED,
                    "tx_hashes": [],
                    "submitted_count": len(order_ids),
                    "accepted_count": 0,
                    "rejected_count": 0,
                    "unknown_count": 0,
                    "api_code": 23000 if backend_code_from_exc == 23000 else None,
                    "api_message": sanitized_exc_msg
                    or "Too Many Requests: 40 requests per 60 second is allowed",
                    "raw_response": None,
                    "nonces": list(nonces),
                    "order_to_tx_hash": {},
                    "per_tx": [],
                    "rate_limit_reason": "Too Many Requests: 40 requests per 60 second is allowed",
                }
            # Genuine transport ambiguity. Do NOT rollback nonces.
            return {
                "outcome": _LADDER_BATCH_OUTCOME_AMBIGUOUS,
                "tx_hashes": [],
                "submitted_count": len(order_ids),
                "accepted_count": 0,
                "rejected_count": 0,
                "unknown_count": len(order_ids),
                "api_code": None,
                "api_message": sanitize_lighter_message(submit_error),
                "raw_response": None,
                "nonces": list(nonces),
                "order_to_tx_hash": {},
                "ambiguity_reason": "transport exception during send_tx_batch; "
                "nonces NOT rolled back per Lighter nonce contract",
                "per_tx": [
                    {
                        "index": i,
                        "order_id": int(oid),
                        "status": _LIGHTER_PER_TX_UNKNOWN,
                        "tx_hash": None,
                        "reason": "transport exception",
                    }
                    for i, oid in enumerate(order_ids)
                ],
            }

        # ------------------------------------------------------------------
        # 4. Classify the envelope response (per-tx).
        # ------------------------------------------------------------------
        api_code = getattr(api_response, "code", None)
        api_message = str(getattr(api_response, "message", "") or "").strip()
        try:
            code_int = int(api_code) if api_code is not None else 0
        except (TypeError, ValueError):
            code_int = 0

        # Reuse the per-tx classifier, keyed on order_ids as the
        # "client_order_indices" so each cancel maps 1:1 to a tx_hash.
        per_tx = _classify_send_tx_batch_per_tx(
            client_order_indices=list(order_ids),
            api_response=api_response,
            envelope_code=code_int,
            envelope_message=api_message,
        )
        # Re-key per_tx entries from client_order_index -> order_id for
        # the cancel canonical result.
        for entry in per_tx:
            entry["order_id"] = entry.pop("client_order_index")
        response_tx_hashes = [
            (entry.get("tx_hash") or "") for entry in per_tx
        ]
        accepted_count = sum(
            1 for entry in per_tx
            if entry.get("status") == _LIGHTER_PER_TX_API_ACCEPTED
        )
        rejected_count = sum(
            1 for entry in per_tx
            if entry.get("status") == _LIGHTER_PER_TX_API_REJECTED
        )
        unknown_count = sum(
            1 for entry in per_tx
            if entry.get("status") == _LIGHTER_PER_TX_UNKNOWN
        )
        order_to_tx_hash: Dict[int, Optional[str]] = {
            int(entry["order_id"]): entry.get("tx_hash") for entry in per_tx
        }
        sanitized_msg = sanitize_lighter_message(api_message) if api_message else ""

        if code_int == 23000:
            for _ in range(len(nonces)):
                try:
                    signer.nonce_manager.acknowledge_failure(api_key_index)
                except Exception:  # noqa: BLE001
                    pass
            return {
                "outcome": _LADDER_BATCH_OUTCOME_RATE_LIMITED,
                "tx_hashes": response_tx_hashes,
                "submitted_count": len(order_ids),
                "accepted_count": 0,
                "rejected_count": len(order_ids),
                "unknown_count": 0,
                "api_code": code_int,
                "api_message": sanitized_msg or "Too Many Requests",
                "raw_response": api_response,
                "nonces": list(nonces),
                "order_to_tx_hash": order_to_tx_hash,
                "per_tx": per_tx,
            }
        if code_int != 200:
            # Envelope-level rejection (non-200, non-23000). Roll back.
            for _ in range(len(nonces)):
                try:
                    signer.nonce_manager.acknowledge_failure(api_key_index)
                except Exception:  # noqa: BLE001
                    pass
            return {
                "outcome": _LADDER_BATCH_OUTCOME_EXCHANGE_REJECTED,
                "tx_hashes": response_tx_hashes,
                "submitted_count": len(order_ids),
                "accepted_count": 0,
                "rejected_count": len(order_ids),
                "unknown_count": 0,
                "api_code": code_int,
                "api_message": sanitized_msg or "Exchange rejected the batch",
                "raw_response": api_response,
                "nonces": list(nonces),
                "order_to_tx_hash": order_to_tx_hash,
                "per_tx": per_tx,
            }

        # code_int == 200 — envelope accepted. Pick batch outcome.
        batch_outcome = (
            _LADDER_BATCH_OUTCOME_EXCHANGE_REJECTED
            if rejected_count > 0
            else _LADDER_BATCH_OUTCOME_SUCCESS
        )
        return {
            "outcome": batch_outcome,
            "tx_hashes": response_tx_hashes,
            "submitted_count": len(order_ids),
            "accepted_count": accepted_count,
            "rejected_count": rejected_count,
            "unknown_count": unknown_count,
            "api_code": code_int,
            "api_message": sanitized_msg,
            "raw_response": api_response,
            "nonces": list(nonces),
            "order_to_tx_hash": order_to_tx_hash,
            "per_tx": per_tx,
        }

    # Wrap in the per-L1 sliding-window limiter (1 HTTP slot per
    # send_tx_batch). No retry at this layer — KAM safety contract.
    #
    # ``_run_lighter_coro_blocking`` bridges the coroutine safely whether
    # or not the calling thread already has a running event loop (the
    # Hermes/Telegram gateway loop). It never calls ``asyncio.run`` on a
    # thread that already has a running loop.
    try:
        return _run_lighter_coro_blocking(
            credentials,
            lambda: _run_batch(),
            thread_name="lighter-cancel-tx-batch",
            max_retries=0,
        )
    except _LighterRateLimitError as exc:
        return {
            "outcome": _LADDER_BATCH_OUTCOME_RATE_LIMITED,
            "tx_hashes": [],
            "submitted_count": len(order_ids),
            "api_code": 23000,
            "api_message": sanitize_lighter_message(str(exc)),
            "raw_response": None,
            "nonces": [],
            "order_to_tx_hash": {},
            "per_tx": [],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "outcome": _LADDER_BATCH_OUTCOME_AMBIGUOUS,
            "tx_hashes": [],
            "submitted_count": 0,
            "api_code": None,
            "api_message": sanitize_error_message(str(exc)),
            "raw_response": None,
            "nonces": [],
            "order_to_tx_hash": {},
            "per_tx": [],
            "ambiguity_reason": "transport exception",
        }


def _reconcile_batch_by_client_order_index(
    *,
    credentials: Dict[str, Any],
    market_id: int,
    side: str,
    expected_client_order_indices: List[int],
    auth_token: str,
) -> Dict[int, int]:
    """Map ``client_order_index -> order_id`` for the expected children.

    Reads ``accountActiveOrders`` once and returns a dict containing
    only the children this batch expected to land. Anything missing
    is genuinely absent — atomicity guarantees either all children
    in a successful batch land or none do, so a partial match is
    itself a defect signal.
    """
    expected = {int(idx) for idx in expected_client_order_indices}
    if not expected:
        return {}
    orders = _fetch_active_orders(credentials, auth_token)
    out: Dict[int, int] = {}
    for entry in orders:
        if not isinstance(entry, dict):
            continue
        try:
            entry_market_id = int(entry.get("market_index") or 0)
        except Exception:  # noqa: BLE001
            continue
        if entry_market_id != market_id:
            continue
        entry_side = "sell" if bool(entry.get("is_ask")) else "buy"
        if entry_side != side:
            continue
        try:
            client_order_index = int(entry.get("client_order_index") or 0)
        except Exception:  # noqa: BLE001
            continue
        if client_order_index not in expected:
            continue
        raw_order_id = str(entry.get("order_id") or "").strip()
        try:
            out[client_order_index] = int(raw_order_id)
        except Exception:  # noqa: BLE001
            continue
    return out


def _execute_ladder(request: Dict[str, Any]) -> CanonicalResponse:
    account_name = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account_name)
    if not credentials:
        return make_failure(operation="ladder", exchange=name, account=account_name, code="UNKNOWN_ACCOUNT", message="Unknown or invalid Lighter account configuration")

    requested_symbol = str(request.get("symbol") or "").strip().upper()
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
        return make_failure(operation="ladder", exchange=name, account=account_name, code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_side not in {"buy", "sell"}:
        return make_failure(operation="ladder", exchange=name, account=account_name, code="INVALID_SIDE", message="Side must be buy or sell.")
    if order_count <= 0:
        return make_failure(operation="ladder", exchange=name, account=account_name, code="INVALID_ORDER_COUNT", message="Order count must be positive.")
    if total_volume is None or total_volume <= 0:
        return make_failure(operation="ladder", exchange=name, account=account_name, code="INVALID_VOLUME", message="Total volume must be positive.")
    if start_price is None or end_price is None:
        return make_failure(operation="ladder", exchange=name, account=account_name, code="INVALID_PRICE", message="Start and end price are required.")
    if requested_side == "buy" and end_price >= start_price:
        return make_failure(operation="ladder", exchange=name, account=account_name, code="INVALID_LADDER_DIRECTION", message="BUY ladders require end price below start price.")
    if requested_side == "sell" and end_price <= start_price:
        return make_failure(operation="ladder", exchange=name, account=account_name, code="INVALID_LADDER_DIRECTION", message="SELL ladders require end price above start price.")

    market = _resolve_market(credentials["base_url"], requested_symbol)
    if market is None:
        return make_failure(operation="ladder", exchange=name, account=account_name, code="INSTRUMENT_NOT_FOUND", message="Instrument not found.")

    size_decimals = int(market.get("size_decimals") or market.get("supported_size_decimals") or 0)
    price_decimals = int(market.get("price_decimals") or market.get("supported_price_decimals") or 0)
    min_base_amount = _decimal_or_none(market.get("min_base_amount"))

    try:
        children, submitted_volume, omitted_below_minimum = _build_lighter_ladder_children(
            side=requested_side,
            distribution=distribution,
            order_count=order_count,
            total_volume=total_volume,
            start_price=start_price,
            end_price=end_price,
            size_decimals=size_decimals,
            price_decimals=price_decimals,
            min_base_amount=min_base_amount,
        )
    except ValueError as exc:
        code = str(exc) or "INVALID_LADDER_REQUEST"
        return make_failure(operation="ladder", exchange=name, account=account_name, code=code, message=sanitize_error_message(code.replace("_", " ").title()))

    def _ladder_result(*, verified: bool, partial: bool, status: str, submitted_count: int, accepted_child_count: int, child_order_ids: List[int], batches: List[Dict[str, Any]]) -> CanonicalLadderResult:
        return CanonicalLadderResult(
            symbol=requested_symbol,
            side=requested_side,
            distribution=distribution,
            requested_order_count=order_count,
            submitted_order_count=submitted_count,
            requested_volume=_decimal_text(total_volume),
            submitted_volume=_decimal_text(submitted_volume),
            batch_count=1 if submitted_count else 0,
            verified=verified,
            partial=partial,
            status=status,
            accepted_child_count=accepted_child_count,
            omitted_order_count=max(0, order_count - submitted_count) or None,
            omitted_below_minimum=omitted_below_minimum or None,
            child_order_ids=child_order_ids or None,
            batches=batches or None,
        )

    if len(children) < 2:
        result = _ladder_result(verified=False, partial=False, status="failed", submitted_count=0, accepted_child_count=0, child_order_ids=[], batches=[])
        return make_failure(operation="ladder", exchange=name, account=account_name, code="LADDER_TOO_FEW_VALID_CHILDREN", message="Fewer than two valid ladder children remain after preflight.", ladder=result)

    # ------------------------------------------------------------------
    # Batched submission via REST sendTxBatch.
    #
    # Each batch is one HTTP request that carries up to
    # ``LIGHTER_SEND_TX_BATCH_SIZE`` independently-signed
    # ``L2CreateOrder`` transactions (one per ladder child). For a
    # 200-child ladder with batch size 30 this produces 7 sendTxBatch
    # requests, paced by the rolling 30-tx/60s budget.
    #
    # Invariants (KAM safety contract):
    #   1. Each ``client_order_index`` is pre-assigned uniquely BEFORE
    #      any sign. It is the authoritative reconciliation key —
    #      never re-issued, never duplicated.
    #   2. ONE write attempt per sendTxBatch. NO automatic retry. NO
    #      blind resend. A 429 (code 23000) or any non-200 envelope
    #      stops the ladder and triggers a read-only reconcile of the
    #      batches that already landed.
    #   3. sendTxBatch is NOT atomic at the transaction level — the
    #      envelope can be 200 while individual transactions in the
    #      same batch are rejected by the L1 sequencer. We therefore
    #      reconcile by client_order_index, not by tx_hash presence.
    #   4. Pacing is exchange-local preventive throttling — not a
    #      retry mechanism. We sleep between batches to keep the L1
    #      window comfortably below its ceiling.
    # ------------------------------------------------------------------
    client_order_indices = _allocate_client_order_indices(len(children))
    market_id_int = int(str(market.get("market_id") or 0))
    batch_records: List[Dict[str, Any]] = []
    accepted_child_count = 0
    reconciled_child_count = 0
    submitted_batch_count = 0
    landed_child_ids: List[int] = []
    stop_reason: Optional[Dict[str, Any]] = None  # populated on stop

    chunk_starts = list(range(0, len(children), LIGHTER_SEND_TX_BATCH_SIZE))
    for batch_index, start in enumerate(chunk_starts):
        # Preventive pacing — only sleep between batches, not before
        # the first one and never after the last batch.
        if batch_index > 0:
            time.sleep(LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS)
        chunk_children = children[start:start + LIGHTER_SEND_TX_BATCH_SIZE]
        chunk_indices = client_order_indices[start:start + LIGHTER_SEND_TX_BATCH_SIZE]

        outcome = _submit_send_tx_batch(
            credentials=credentials,
            market=market,
            side=requested_side,
            children=chunk_children,
            client_order_indices=chunk_indices,
        )

        # Stamp the batch record with the per-batch trail. The record
        # carries BOTH the agent's per-tx classification AND the
        # authoritative reconciliation outcome below.
        per_tx_outcomes = list(outcome.get("per_tx") or [])
        accepted_count = int(outcome.get("accepted_count") or 0)
        rejected_count = int(outcome.get("rejected_count") or 0)
        unknown_count = int(outcome.get("unknown_count") or 0)
        batch_records.append({
            "batch_index": batch_index,
            "client_order_indices": list(chunk_indices),
            "nonces": list(outcome.get("nonces") or []),
            "submitted": len(chunk_children),
            "requested": len(chunk_children),
            "accepted": accepted_count,
            "rejected": rejected_count,
            "unknown": unknown_count,
            "tx_hashes": list(outcome.get("tx_hashes") or []),
            "per_tx": per_tx_outcomes,
            "api_code": outcome.get("api_code"),
            "api_message": outcome.get("api_message"),
            "reason": outcome.get("reason") or outcome.get("api_message") or "",
        })
        outcome_kind = outcome.get("outcome")

        # ------------------------------------------------------------------
        # Outcome dispatch.
        #
        # The agent only continues to the next batch when the current
        # batch has a SAFE per-tx state — i.e. every submitted child
        # is API_ACCEPTED. Anything else stops the ladder:
        #
        #   SUCCESS / API_ACCEPTED for every child  →  continue.
        #   SUCCESS with any UNKNOWN child           →  STOP and
        #                                            reconcile THIS
        #                                            batch. We have
        #                                            no native signal
        #                                            that proves the
        #                                            child landed; do
        #                                            not send more
        #                                            batches whose
        #                                            fate we cannot
        #                                            reason about.
        #   INSUFFICIENT_MARGIN / INVALID_ORDER      →  STOP.
        #   PRICE_TOO_FAR_FROM_MARK
        #   EXCHANGE_REJECTED                       →  STOP.
        #   RATE_LIMITED                            →  STOP.
        #   AMBIGUOUS (genuine transport)            →  STOP.
        # ------------------------------------------------------------------
        # ``ok`` is True if reconciliation should run over this batch
        # (i.e. any children MIGHT have landed). This is independent
        # of the outcome dispatch decision.
        per_tx_outcomes_list = list(outcome.get("per_tx") or [])
        per_tx_statuses = [p.get("status") for p in per_tx_outcomes_list]
        api_rejected_count = sum(
            1 for s in per_tx_statuses
            if s == _LIGHTER_PER_TX_API_REJECTED
        )
        unknown_count = sum(
            1 for s in per_tx_statuses
            if s == _LIGHTER_PER_TX_UNKNOWN
        )
        # ``ok`` for reconciliation: true if at least one child has a
        # tx_hash that the API signed off on. UNKNOWN children are also
        # worth reconciling because they MIGHT have landed.
        batch_records[-1]["ok"] = (
            accepted_count > 0
            or api_rejected_count > 0
            or unknown_count > 0
        )
        # Always set the batch-level reason to the per-tx outcome
        # (so the canonical ledger tells the user what happened).
        batch_records[-1]["reason"] = outcome_kind.upper()

        # ------------------------------------------------------------------
        # Continue / STOP decision
        # ------------------------------------------------------------------
        # Continue only when every child has API_ACCEPTED status. Any
        # UNKNOWN or API_REJECTED status in the batch triggers STOP.
        all_api_accepted = (
            accepted_count == len(chunk_children)
            and api_rejected_count == 0
            and unknown_count == 0
        )

        if outcome_kind == _LADDER_BATCH_OUTCOME_SUCCESS and not all_api_accepted:
            # The envelope returned 200, but at least one child is
            # UNKNOWN or API_REJECTED. STOP — sending more batches
            # whose outcome we cannot reason about compounds risk.
            # Reconciliation will tell us which children actually
            # landed.
            submitted_batch_count += 1
            if api_rejected_count > 0 and unknown_count == 0:
                stop_reason = {
                    "code": "LADDER_BATCH_API_REJECTED",
                    "outcome": outcome_kind,
                    "message": (
                        outcome.get("api_message")
                        or f"{api_rejected_count} of {len(chunk_children)} children API-rejected"
                    ),
                    "raw_code": outcome.get("api_code"),
                }
            else:
                stop_reason = {
                    "code": "LADDER_BATCH_PER_TX_UNKNOWN",
                    "outcome": outcome_kind,
                    "message": (
                        f"{unknown_count} of {len(chunk_children)} children UNKNOWN after envelope 200; "
                        "stopping for reconciliation"
                    ),
                    "raw_code": outcome.get("api_code"),
                }
            break
        if outcome_kind == _LADDER_BATCH_OUTCOME_SUCCESS:
            submitted_batch_count += 1
            # accepted_count is filled in by the reconciliation pass
            # below — we do NOT trust the response tx_hash list as
            # proof of on-chain landing.
            continue
        if outcome_kind == _LADDER_BATCH_OUTCOME_RATE_LIMITED:
            submitted_batch_count += 1  # write was attempted
            stop_reason = {
                "code": "RATE_LIMITED",
                "outcome": _LADDER_BATCH_OUTCOME_RATE_LIMITED,
                "message": outcome.get("api_message") or "Too Many Requests",
                "raw_code": outcome.get("api_code"),
            }
            break
        if outcome_kind == _LADDER_BATCH_OUTCOME_INSUFFICIENT_MARGIN:
            submitted_batch_count += 1
            batch_records[-1]["reason"] = "INSUFFICIENT_MARGIN"
            stop_reason = {
                "code": "INSUFFICIENT_MARGIN",
                "outcome": _LADDER_BATCH_OUTCOME_INSUFFICIENT_MARGIN,
                "message": outcome.get("api_message") or "not enough margin",
                "raw_code": outcome.get("api_code"),
            }
            break
        if outcome_kind in (
            _LADDER_BATCH_OUTCOME_INVALID_ORDER,
            _LADDER_BATCH_OUTCOME_PRICE_TOO_FAR,
            _LADDER_BATCH_OUTCOME_EXCHANGE_REJECTED,
        ):
            submitted_batch_count += 1
            stop_reason = {
                "code": outcome_kind.upper(),
                "outcome": outcome_kind,
                "message": outcome.get("api_message")
                or "Exchange rejected the batch",
                "raw_code": outcome.get("api_code"),
            }
            break
        # AMBIGUOUS (transport / sign / nonce reservation failure)
        submitted_batch_count += 1
        batch_records[-1]["ambiguity_reason"] = outcome.get(
            "ambiguity_reason", "transport exception"
        )
        stop_reason = {
            "code": "LADDER_BATCH_AMBIGUOUS",
            "outcome": _LADDER_BATCH_OUTCOME_AMBIGUOUS,
            "message": outcome.get("api_message")
            or outcome.get("ambiguity_reason")
            or "Ambiguous batch outcome",
            "raw_code": outcome.get("api_code"),
        }
        break

    # ------------------------------------------------------------------
    # ONE authoritative reconciliation pass.
    #
    # Normal path: we run a single accountActiveOrders read after the
    # last batch has been submitted. The HTTP request budget for the
    # entire 200-child ladder is then:
    #   preflight + token-mint + nonce-fetch    =  3 requests
    #   20 sendTxBatch writes                    = 20 requests
    #   1 final accountActiveOrders read         =  1 request
    #                                            = 24 requests over ~60s
    # which sits comfortably below the observed 40/60s L1 cap.
    #
    # Abnormal path: if the loop exited early because of a 429 /
    # non-200 envelope / transport exception / parse error / ambiguous
    # outcome, we still do ONE reconciliation over every client_order_index
    # submitted so far. That tells us which batches landed before the
    # stop. We never reconcile per-batch — the cadence is "one final
    # read either way".
    # ------------------------------------------------------------------
    successful_batch_records = [
        rec for rec in batch_records if rec.get("ok")
    ]
    all_submitted_client_order_indices: List[int] = []
    for rec in successful_batch_records:
        all_submitted_client_order_indices.extend(rec.get("client_order_indices") or [])

    if all_submitted_client_order_indices:
        # Resolve the auth token FIRST. If minting fails we MUST
        # refuse to even call accountActiveOrders — issuing an
        # empty-token request would just 401 and look like "no
        # orders landed" to the verifier. Surface the failure
        # explicitly so the canonical result carries
        # RECONCILIATION_AUTH_FAILED and the wizard can show the
        # user that the writes may have actually succeeded.
        reconciliation_auth_failed = False
        reconcile_token = ""
        try:
            reconcile_token = _mint_auth_token_cached(credentials)
        except Exception as exc:  # noqa: BLE001
            reconciliation_auth_failed = True
            mid_reconcile_error = sanitize_error_message(str(exc))
        else:
            mid_reconcile_error = None
        if not reconcile_token:
            # Token mint either raised or returned empty. In
            # both cases treat the verification as auth-failed.
            reconciliation_auth_failed = True
            # Token mint failed (either raised or returned empty).
            # Record the error on every successful batch and mark
            # each as unverified, then fall through to the canonical
            # result assembly.
            for rec in batch_records:
                if rec.get("ok"):
                    rec["verified"] = False
                    rec["verify_error"] = (
                        f"reconciliation token mint failed: "
                        f"{mid_reconcile_error or 'empty token'}"
                    )
                    rec["accepted"] = 0
            landed_all = {}
            reconciliation_error = RuntimeError(
                f"reconciliation token mint failed: "
                f"{mid_reconcile_error or 'empty token'}"
            )
        else:
            # The accountActiveOrders indexer can occasionally lag the
            # sequence by a few hundred milliseconds. We retry the
            # read a few times (with a small linear backoff) before
            # declaring any child unfindable. This is purely read-only
            # — no side effects on the exchange state.
            #
            # The retry ONLY makes sense when we are not already in a
            # stop_reason situation. If the ladder stopped because of a
            # rate-limit / rejection / unknown outcome, we already know
            # that not every child landed. Retrying would not change
            # that answer and would just consume rate-limit slots.
            landed_all: Dict[int, int] = {}
            reconciliation_error = None
            if stop_reason is None:
                target_set = set(all_submitted_client_order_indices)
                for retry_idx in range(3):
                    try:
                        landed_all = _reconcile_batch_by_client_order_index(
                            credentials=credentials,
                            market_id=market_id_int,
                            side=requested_side,
                            expected_client_order_indices=all_submitted_client_order_indices,
                            auth_token=reconcile_token,
                        )
                        reconciliation_error = None
                    except Exception as exc:  # noqa: BLE001
                        landed_all = {}
                        reconciliation_error = exc
                        break
                    if len(landed_all) == len(target_set):
                        break
                    if retry_idx < 2:
                        time.sleep(0.5)
            else:
                # Stop reason exists — run ONE reconcile without retrying.
                try:
                    landed_all = _reconcile_batch_by_client_order_index(
                        credentials=credentials,
                        market_id=market_id_int,
                        side=requested_side,
                        expected_client_order_indices=all_submitted_client_order_indices,
                        auth_token=reconcile_token,
                    )
                    reconciliation_error = None
                except Exception as exc:  # noqa: BLE001
                    landed_all = {}
                    reconciliation_error = exc
        if reconciliation_error is not None and landed_all == {}:
            # Only the token-mint path fills this branch. The retry
            # loop above sets landed_all={} only on exception, in which
            # case it also sets reconciliation_error=exc and breaks.
            for rec in batch_records:
                if rec.get("ok"):
                    rec["verified"] = False
                    rec["verify_error"] = sanitize_error_message(str(reconciliation_error))
                    rec["accepted"] = 0
        else:
            # Distribute the landed mapping back to each batch record
            # so the per-batch detail survives in the canonical result.
            for rec in successful_batch_records:
                expected = rec.get("client_order_indices") or []
                if not expected:
                    rec["accepted"] = 0
                    rec["reconciled"] = 0
                    continue
                batch_landed_oids = [
                    landed_all[int(ci)] for ci in expected
                    if int(ci) in landed_all
                ]
                rec["verified_landed"] = batch_landed_oids
                # The API-level accepted count is what Lighter's sendTxBatch
                # envelope told us. It may exceed the OID-reconciled count
                # when the accountActiveOrders indexer lags the
                # sequencer (observed live during the 200-order test).
                api_accepted_count = int(rec.get("accepted") or 0)
                rec["api_accepted"] = api_accepted_count
                # OID-reconciled count (authoritative identity match).
                rec["reconciled"] = len(batch_landed_oids)
                # ``accepted_child_count`` is the API-accepted count
                # (what the operator submitted and got API 200 for);
                # ``reconciled_child_count`` is the OID-confirmed count.
                # They can diverge when the accountActiveOrders indexer
                # lags the sequencer. The canonical ``verified`` flag
                # then tells the operator exactly which children landed.
                rec["accepted"] = api_accepted_count
                accepted_child_count += api_accepted_count
                reconciled_child_count += rec["reconciled"]
                landed_child_ids.extend(batch_landed_oids)
                # Per-batch verified.
                if rec["reconciled"] == len(expected):
                    rec["verified"] = True
                elif rec["reconciled"] == 0:
                    # Backend said success but nothing landed —
                    # surface as ambiguous. We do NOT auto-retry.
                    rec["verified"] = False
                else:
                    rec["verified"] = False
    else:
        reconciliation_auth_failed = False

    # ------------------------------------------------------------------
    # Final canonical result.
    # ------------------------------------------------------------------
    requested_count = len(children)
    partial = reconciled_child_count < requested_count
    # verified = every child counted as accepted has been authoritatively
    # reconciled by client_order_index → OID.
    #   * True  → accepted_child_count > 0 AND
    #     reconciled_child_count == accepted_child_count.
    #     Every accepted child has a confirmed on-chain OID match.
    #   * False → reconciliation was attempted but the count of OID
    #     matches did not match accepted, OR reconciliation itself
    #     failed (token-mint failure).
    #
    # ``partial`` is derived from ``reconciled_child_count`` (not from
    # accepted_child_count) so a ladder where the indexer lags and
    # only 30/100 reconciled is still partial — the operator cannot
    # treat all 100 as landed.
    # ``status`` is computed using reconciled vs requested.
    verified_overall = (
        accepted_child_count > 0
        and reconciled_child_count == accepted_child_count
    )
    if stop_reason is None:
        status = (
            "success" if reconciled_child_count == requested_count
            else ("partial" if reconciled_child_count > 0 else "failed")
        )
        canonical_code: Optional[str] = None
    else:
        outcome_kind = stop_reason.get("outcome")
        # ``stop_reason.code`` may carry a more specific canonical
        # code than ``outcome_kind`` (e.g. "LADDER_BATCH_PARTIAL_PER_TX"
        # vs the underlying ``SUCCESS`` outcome_kind when the envelope
        # was 200 but some children were rejected). Prefer it when
        # present.
        canonical_code_raw = stop_reason.get("code") or ""
        if outcome_kind == _LADDER_BATCH_OUTCOME_RATE_LIMITED:
            status = "partial"
            canonical_code = "RATE_LIMITED"
        elif canonical_code_raw == "LADDER_BATCH_API_REJECTED":
            status = "partial" if accepted_child_count > 0 else "failed"
            canonical_code = "LADDER_BATCH_API_REJECTED"
        elif canonical_code_raw == "LADDER_BATCH_PER_TX_UNKNOWN":
            # The envelope was 200 but at least one child had no tx_hash
            # signal. We treat this as ambiguous at the canonical level
            # — reconciliation is authoritative.
            status = "partial" if accepted_child_count > 0 else "failed"
            canonical_code = "LADDER_BATCH_PER_TX_UNKNOWN"
        elif outcome_kind == _LADDER_BATCH_OUTCOME_INSUFFICIENT_MARGIN:
            status = "partial" if accepted_child_count > 0 else "failed"
            canonical_code = "INSUFFICIENT_MARGIN"
        elif outcome_kind == _LADDER_BATCH_OUTCOME_INVALID_ORDER:
            status = "partial" if accepted_child_count > 0 else "failed"
            canonical_code = "INVALID_ORDER"
        elif outcome_kind == _LADDER_BATCH_OUTCOME_PRICE_TOO_FAR:
            status = "partial" if accepted_child_count > 0 else "failed"
            canonical_code = "PRICE_TOO_FAR_FROM_MARK"
        elif outcome_kind == _LADDER_BATCH_OUTCOME_AMBIGUOUS:
            status = "partial" if accepted_child_count > 0 else "failed"
            canonical_code = "LADDER_BATCH_AMBIGUOUS"
        else:
            status = "partial" if accepted_child_count > 0 else "failed"
            canonical_code = "EXCHANGE_REJECTED"

    exchange_reason: Optional[str] = None
    rate_limited: Optional[bool] = None
    if canonical_code == "RATE_LIMITED":
        rate_limited = True
        # RATE_LIMITED comes from a definitive 23000 / HTTP 429. We
        # preserve a sanitized, operator-safe reason that does not leak
        # the L1Address. If the SDK message happens to carry the
        # Lighter L1Address, redact it here.
        raw_reason = (
            stop_reason.get("message")
            or "Too Many Requests: 40 requests per 60 second is allowed"
        )
        exchange_reason = sanitize_lighter_message(raw_reason)
        if "Too Many Requests" not in exchange_reason:
            exchange_reason = (
                exchange_reason + " (Too Many Requests: 40 requests per 60 second is allowed)"
            ).strip()
    elif canonical_code == "INSUFFICIENT_MARGIN":
        exchange_reason = sanitize_lighter_message(
            stop_reason.get("message") or "not enough margin"
        )
    elif canonical_code in ("INVALID_ORDER", "PRICE_TOO_FAR_FROM_MARK"):
        exchange_reason = sanitize_lighter_message(
            stop_reason.get("message") or "Exchange rejected the order"
        )
    elif canonical_code == "LADDER_BATCH_API_REJECTED":
        # Envelope rejected per-tx — surface the most informative message.
        exchange_reason = sanitize_lighter_message(
            stop_reason.get("message")
            or "Some children were API-rejected by the Lighter backend"
        )
    elif canonical_code == "LADDER_BATCH_PER_TX_UNKNOWN":
        # Envelope was 200 but at least one child has no native signal.
        # Surface this clearly to the user — reconciliation will resolve it.
        exchange_reason = sanitize_lighter_message(
            stop_reason.get("message")
            or "Some children have no native signal — reconciliation required"
        )
    elif canonical_code == "EXCHANGE_REJECTED":
        exchange_reason = sanitize_lighter_message(
            stop_reason.get("message") or "Exchange rejected the batch"
        )
    elif canonical_code == "LADDER_BATCH_AMBIGUOUS":
        exchange_reason = sanitize_lighter_message(
            stop_reason.get("message") or "Ambiguous batch outcome"
        )

    ladder = CanonicalLadderResult(
        symbol=requested_symbol,
        side=requested_side,
        distribution=distribution,
        requested_order_count=order_count,
        submitted_order_count=accepted_child_count,
        requested_volume=_decimal_text(total_volume),
        submitted_volume=_decimal_text(submitted_volume),
        batch_count=submitted_batch_count,
        verified=verified_overall,
        partial=partial,
        status=status,
        accepted_child_count=accepted_child_count,
        omitted_order_count=max(0, order_count - len(children)) or None,
        omitted_below_minimum=omitted_below_minimum or None,
        child_order_ids=landed_child_ids or None,
        batches=batch_records or None,
        rate_limited=rate_limited,
        exchange_reason=exchange_reason,
    )
    # Report success only if every child landed. A partial ladder —
    # whether due to a definite stop_reason OR to reconciliation
    # finding fewer children than expected — surfaces as a failure
    # so the wizard renders the partial count to the user.
    if canonical_code is None and not partial:
        # Fast path: every child landed, no auth spike. If the
        # reconciliation could not even authenticate we still
        # refuse to claim success — verified=false below.
        if reconciliation_auth_failed:
            return make_failure(
                operation="ladder",
                exchange=name,
                account=account_name,
                code="RECONCILIATION_AUTH_FAILED",
                message="Ladder submission may have succeeded but the "
                "reconciliation auth-token mint failed before any "
                "AccountActiveOrders read could be performed. "
                "Treat all children as unknown; no write retry was "
                f"attempted. Reason: {mid_reconcile_error or 'unknown'}",
                ladder=ladder,
            )
        return make_success(operation="ladder", exchange=name, account=account_name, ladder=ladder)
    if reconciliation_auth_failed:
        # Auth failed and there's nothing to report as success.
        # Skip the generic "partial" code and use the dedicated
        # verification failure so the user can distinguish
        # "exchange rejected the bundle" from "we couldn't even
        # authenticate to verify".
        message = sanitize_error_message(
            mid_reconcile_error
            or "Reconciliation auth-token mint failed after write"
        )
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account_name,
            code="RECONCILIATION_AUTH_FAILED",
            message=message,
            ladder=ladder,
        )
    if canonical_code is None:
        # Partial via reconciliation gap (backend said success but
        # not all children appeared on-chain). Use a generic
        # partial code; the per-batch detail is in ladder.batches.
        canonical_code = "LADDER_PARTIAL"
    # Sanitize L1 addresses (the Lighter backend includes the
    # L1Address in its 23000 body) from any user-visible message.
    message = sanitize_lighter_message(
        stop_reason.get("message") if stop_reason else "Ladder submission was only partially completed"
    )
    return make_failure(
        operation="ladder",
        exchange=name,
        account=account_name,
        code=canonical_code,
        message=message,
        ladder=ladder,
    )


def _submit_new_order(
    credentials: Dict[str, Any],
    market: Dict[str, Any],
    *,
    side: str,
    order_type: str,
    requested_volume: Decimal,
    requested_price: Decimal,
    reduce_only: bool,
    client_order_index: Optional[int] = None,
) -> Dict[str, Any]:
    async def _run_submit() -> Dict[str, Any]:
        signer = _build_signer_client(credentials)
        try:
            # L2CreateOrder budget check. This is shared with the
            # ladder path. _submit_tpsl_order has its own transaction
            # type (TpSlOrder) and is NOT throttled here.
            budget = _get_lighter_l2_tx_budget(credentials)
            budget.wait_for_capacity(1)
            size_decimals = int(market.get("size_decimals") or market.get("supported_size_decimals") or 0)
            price_decimals = int(market.get("price_decimals") or market.get("supported_price_decimals") or 0)
            submitted_volume = _quantize_down(requested_volume, size_decimals)
            submitted_price = _quantize_down(requested_price, price_decimals)
            base_amount = _to_scaled_int(submitted_volume, size_decimals)
            price_int = _to_scaled_int(submitted_price, price_decimals)
            if base_amount <= 0:
                raise RuntimeError("Volume must be positive after Lighter size quantization")
            if price_int <= 0:
                raise RuntimeError("Price must be positive after Lighter price quantization")
            # Resolve the effective client identity. The caller-supplied
            # parameter (client_order_index) is honored verbatim when
            # present; otherwise fall back to a time-based value for
            # backward compatibility with legacy /trade paths. Use a
            # distinct local name to avoid shadowing the closure param.
            effective_client_order_index = client_order_index
            if effective_client_order_index is None:
                effective_client_order_index = int(time.time_ns() % LIGHTER_MAX_CLIENT_ORDER_INDEX)
            effective_client_order_index = int(effective_client_order_index)
            if effective_client_order_index <= 0:
                effective_client_order_index = 1
            if effective_client_order_index > LIGHTER_MAX_CLIENT_ORDER_INDEX:
                effective_client_order_index = int(effective_client_order_index % LIGHTER_MAX_CLIENT_ORDER_INDEX) or 1
            if order_type == "market":
                tx, api_response, error = await signer.create_market_order_limited_slippage(
                    int(market["market_id"]),
                    effective_client_order_index,
                    base_amount,
                    LIGHTER_CLOSE_MAX_SLIPPAGE,
                    side == "sell",
                    reduce_only=reduce_only,
                    api_key_index=credentials["api_key_index"],
                )
            else:
                tx, api_response, error = await signer.create_order(
                    int(market["market_id"]),
                    effective_client_order_index,
                    base_amount,
                    price_int,
                    side == "sell",
                    signer.ORDER_TYPE_LIMIT,
                    signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                    reduce_only=reduce_only,
                    api_key_index=credentials["api_key_index"],
                )
            if error:
                raise RuntimeError(f"Lighter order submission failed: {error}")
            # The Lighter SDK returns a successful tuple even when the
            # backend rejects the order (e.g. 23000 rate-limit surfaced
            # as a 200-OK-with-error-body envelope). Inspect the
            # api_response so the retry layer can see the 429 — without
            # this, the order is silently treated as accepted and the
            # verifier later reports VERIFICATION_FAILED with no signal
            # that the actual cause was a rate-limit hit.
            _classify_lighter_api_response(api_response)
            exchange_order_id = None
            tx_nonce = getattr(tx, "nonce", None)
            response_hash = getattr(api_response, "tx_hash", None)
            return {
                "submitted_volume": _decimal_text(submitted_volume),
                "submitted_price": _decimal_text(submitted_price),
                "exchange_order_id": exchange_order_id,
                "client_order_index": effective_client_order_index,
                "nonce": tx_nonce,
                "tx_hash": response_hash,
            }
        finally:
            api_client = getattr(signer, "api_client", None)
            if api_client is not None and hasattr(api_client, "close"):
                try:
                    await api_client.close()
                except Exception:
                    pass

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run_with_lighter_ratelimit(credentials, lambda: asyncio.run(_run_submit()))

    result: Dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["value"] = _run_with_lighter_ratelimit(credentials, lambda: asyncio.run(_run_submit()))
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=_runner, name="lighter-new-order", daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return dict(result.get("value") or {})


def _execute_new_order(request: Dict[str, Any]) -> CanonicalResponse:
    account_name = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account_name)
    if not credentials:
        return make_failure(operation="new_order", exchange=name, account=account_name, code="UNKNOWN_ACCOUNT", message="Unknown or invalid Lighter account configuration")

    requested_symbol = str(request.get("symbol") or "").strip().upper()
    requested_side = str(request.get("side") or "").strip().lower()
    order_type = str(request.get("order_type") or "limit").strip().lower() or "limit"
    reduce_only = bool(request.get("reduce_only") or False)
    requested_volume = _decimal_or_none(request.get("volume"))
    requested_price = _decimal_or_none(request.get("price"))

    if not requested_symbol:
        return make_failure(operation="new_order", exchange=name, account=account_name, code="MISSING_SYMBOL", message="Symbol is required.")
    if requested_side not in {"buy", "sell"}:
        return make_failure(operation="new_order", exchange=name, account=account_name, code="INVALID_SIDE", message="Side must be buy or sell.")
    if order_type not in {"limit", "market"}:
        return make_failure(operation="new_order", exchange=name, account=account_name, code="INVALID_ORDER_TYPE", message="Only limit and market orders are currently supported for Lighter.")
    if requested_volume is None or requested_volume <= 0:
        return make_failure(operation="new_order", exchange=name, account=account_name, code="INVALID_VOLUME", message="Volume must be positive.")
    if order_type == "limit" and (requested_price is None or requested_price <= 0):
        return make_failure(operation="new_order", exchange=name, account=account_name, code="INVALID_PRICE", message="Price must be positive for limit orders.")

    market = _resolve_market(credentials["base_url"], requested_symbol)
    if market is None:
        return make_failure(operation="new_order", exchange=name, account=account_name, code="INSTRUMENT_NOT_FOUND", message="Instrument not found.")

    size_decimals = int(market.get("size_decimals") or market.get("supported_size_decimals") or 0)
    price_decimals = int(market.get("price_decimals") or market.get("supported_price_decimals") or 0)
    submitted_volume_decimal = _quantize_down(requested_volume, size_decimals)
    if order_type == "market":
        # MARKET orders don't carry a price from the caller. Use the
        # current mark as the reference for quantization; the actual
        # fill price is recovered from the venue's pending/filled
        # order records via get_order_state / accountInactiveOrders.
        try:
            mp_payload = _fetch_lighter_public_market_price(
                credentials["base_url"], market
            )
        except Exception:
            mp_payload = None
        ref_price = None
        if isinstance(mp_payload, dict):
            for key in ("mark", "last_trade", "ask", "bid"):
                raw = mp_payload.get(key)
                if raw is not None:
                    parsed = _decimal_or_none(raw)
                    if parsed is not None and parsed > 0:
                        ref_price = parsed
                        break
        if ref_price is None:
            return make_failure(
                operation="new_order",
                exchange=name,
                account=account_name,
                code="PRICE_UNAVAILABLE",
                message=(
                    f"Market order requires a reference price for quantization "
                    f"({requested_symbol})."
                ),
            )
        submitted_price_decimal = _quantize_down(ref_price, price_decimals)
    else:
        submitted_price_decimal = _quantize_down(requested_price, price_decimals)
    min_base_amount = _decimal_or_none(market.get("min_base_amount"))
    if submitted_volume_decimal <= 0:
        return make_failure(operation="new_order", exchange=name, account=account_name, code="INVALID_VOLUME", message="Volume must be positive after Lighter size quantization.")
    if min_base_amount is not None and submitted_volume_decimal < min_base_amount:
        return make_failure(operation="new_order", exchange=name, account=account_name, code="INVALID_VOLUME", message="Volume is below Lighter minimum size.")
    if submitted_price_decimal <= 0:
        return make_failure(operation="new_order", exchange=name, account=account_name, code="INVALID_PRICE", message="Price must be positive after Lighter price quantization.")

    # Submit + verify with bounded retry policy.
    #
    # Post-fix design:
    #   - Submit exactly ONCE. No write retry.
    #   - Verification is bounded (LIGHTER_VERIFY_ATTEMPTS=4) and
    #     READ-ONLY. The Lighter indexer can lag the sequence by up
    #     to a few hundred milliseconds; a single immediate read is
    #     not enough.
    #   - Authoritative identity: ``client_order_index`` returned by
    #     ``_submit_new_order``. Match by client_order_index first;
    #     fall back to (market, side, size, price) only if the
    #     indexer's response shape drops client_order_index.
    #   - On definite success the loop exits with the matched order.
    #   - On exhaustion return VERIFICATION_FAILED — never resubmit.
    try:
        # Optional deterministic client identity supplied by the caller
        # (e.g. GoldenFibo). When present, it is honored verbatim; when
        # absent, _submit_new_order falls back to time-based generation.
        requested_client_order_id = request.get("client_order_id")
        if requested_client_order_id is None:
            requested_client_order_id = request.get("client_order_index")
        client_order_index_arg: Optional[int] = None
        if requested_client_order_id is not None:
            try:
                client_order_index_arg = int(requested_client_order_id)
            except (TypeError, ValueError):
                return make_failure(
                    operation="new_order",
                    exchange=name,
                    account=account_name,
                    code="INVALID_INPUTS",
                    message="client_order_id must be an integer when provided",
                )

        submit_result = _submit_new_order(
            credentials,
            market,
            side=requested_side,
            order_type=order_type,
            requested_volume=submitted_volume_decimal,
            requested_price=submitted_price_decimal,
            reduce_only=reduce_only,
            client_order_index=client_order_index_arg,
        )
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account_name,
            code="ORDER_SUBMISSION_FAILED",
            message=sanitize_error_message(str(exc)),
            order=CanonicalOrderResult(
                symbol=requested_symbol,
                side=requested_side,
                order_type=order_type,
                requested_volume=_decimal_text(requested_volume),
                requested_price=_decimal_text(requested_price),
                submitted_volume=_decimal_text(submitted_volume_decimal),
                submitted_price=_decimal_text(submitted_price_decimal),
                verified=False,
                status="failed",
            ),
        )

    expected_client_order_index = submit_result.get("client_order_index")
    matched_order: Optional[Dict[str, Any]] = None
    last_error: Optional[str] = None
    for attempt in range(max(1, int(LIGHTER_VERIFY_ATTEMPTS))):
        try:
            post_orders = _fetch_active_orders(
                credentials, _mint_auth_token_cached(credentials)
            )
        except Exception as exc:  # noqa: BLE001
            last_error = sanitize_error_message(str(exc))
            if attempt + 1 < LIGHTER_VERIFY_ATTEMPTS:
                time.sleep(LIGHTER_VERIFY_DELAY_SECONDS * (attempt + 1))
                continue
            break

        # Determine whether the indexer's response shape drops the
        # client_order_index field. If every order carries a ci key,
        # ci-match is the only authoritative proof — falling back to
        # (market, side, size, price) would let a wrong-ci order
        # false-positive the verification.
        indexer_returns_ci = False
        for order in post_orders:
            if not isinstance(order, dict):
                continue
            if "client_order_index" in order:
                try:
                    if int(order.get("client_order_index") or 0) > 0:
                        indexer_returns_ci = True
                        break
                except (TypeError, ValueError):
                    continue

        # Match by client_order_index first (authoritative).
        if expected_client_order_index is not None and indexer_returns_ci:
            for order in post_orders:
                if not isinstance(order, dict):
                    continue
                try:
                    if int(order.get("client_order_index") or 0) == int(expected_client_order_index):
                        matched_order = order
                        break
                except (TypeError, ValueError):
                    continue

        # Fall back to (market, side, size, price) ONLY when the
        # indexer response doesn't carry client_order_index. In that
        # defensive case the (market, side, size, price) tuple is the
        # only available identity. We do NOT fall back if the indexer
        # returned a different ci — that's a clear mismatch.
        if matched_order is None and not indexer_returns_ci:
            for order in post_orders:
                if not isinstance(order, dict):
                    continue
                try:
                    market_id = int(order.get("market_index"))
                except Exception:  # noqa: BLE001
                    continue
                if market_id != int(market["market_id"]):
                    continue
                if ("sell" if bool(order.get("is_ask")) else "buy") != requested_side:
                    continue
                if _decimal_text(order.get("remaining_base_amount") or order.get("initial_base_amount")) != _decimal_text(submitted_volume_decimal):
                    continue
                if _decimal_text(order.get("price")) != _decimal_text(submitted_price_decimal):
                    continue
                matched_order = order
                break

        if matched_order is not None:
            break
        # For market orders, the order fills and is no longer in
        # active orders. Verify by reading the position size delta.
        if order_type == "market":
            try:
                post_size = _current_position_size(
                    request,
                    symbol=requested_symbol,
                    side=requested_side,
                )
            except Exception:
                post_size = Decimal("0")
            if post_size >= submitted_volume_decimal:
                matched_order = {
                    "order_id": submit_result.get("exchange_order_id"),
                    "client_order_index": submit_result.get("client_order_index"),
                    "market_index": int(market["market_id"]),
                    "symbol": requested_symbol,
                    "is_ask": requested_side == "sell",
                    "filled_base_amount": str(_to_scaled_int(post_size, size_decimals)),
                    # No submitted price (market). The actual fill price
                    # is recovered later via get_order_state from the
                    # inactive-orders record.
                }
                break
        # Not yet visible — back off and retry.
        if attempt + 1 < LIGHTER_VERIFY_ATTEMPTS:
            time.sleep(LIGHTER_VERIFY_DELAY_SECONDS * (attempt + 1))

    exchange_order_id = submit_result.get("exchange_order_id")
    if exchange_order_id is None and isinstance(matched_order, dict):
        raw_order_id = str(matched_order.get("order_id") or "").strip()
        try:
            exchange_order_id = int(raw_order_id) if raw_order_id else None
        except Exception:  # noqa: BLE001
            exchange_order_id = None

    order_result = CanonicalOrderResult(
        symbol=requested_symbol,
        side=requested_side,
        order_type=order_type,
        requested_volume=_decimal_text(requested_volume),
        requested_price=_decimal_text(requested_price),
        submitted_volume=str(submit_result.get("submitted_volume") or _decimal_text(submitted_volume_decimal)),
        submitted_price=str(submit_result.get("submitted_price") or _decimal_text(submitted_price_decimal)),
        verified=matched_order is not None,
        status="success" if matched_order is not None else "partial",
        exchange_order_id=exchange_order_id,
    )
    if matched_order is not None:
        return make_success(operation="new_order", exchange=name, account=account_name, order=order_result)
    # Surface the verification failure with explicit reason.
    if last_error:
        return make_failure(
            operation="new_order",
            exchange=name,
            account=account_name,
            code="VERIFICATION_FAILED",
            message=("Order submission could not be verified: " + last_error),
            order=order_result,
        )
    return make_failure(
        operation="new_order",
        exchange=name,
        account=account_name,
        code="VERIFICATION_FAILED",
        message="Order submission could not be verified.",
        order=order_result,
    )


def _fetch_active_orders(credentials: Dict[str, Any], auth_token: str) -> List[Dict[str, Any]]:
    limiter = _get_lighter_limiter(credentials)
    try:
        limiter.acquire()
        response = requests.get(
            f"{credentials['base_url']}/api/v1/accountActiveOrders",
            params={"account_index": str(credentials["account_index"])},
            headers={"Accept": "application/json", "authorization": auth_token},
            timeout=10,
        )
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        retry_after = _looks_like_lighter_429(exc)
        if retry_after is not None:
            limiter.record_failure(retry_after)
            time.sleep(retry_after)
        raise
    payload = response.json()
    orders = payload.get("orders")
    return orders if isinstance(orders, list) else []


def _normalize_positions(
    target: Dict[str, Any],
    market_map: Optional[Dict[int, Dict[str, Any]]] = None,
    price_precisions: Optional[Dict[int, int]] = None,
) -> List[CanonicalPosition]:
    positions_raw = target.get("positions")
    if not isinstance(positions_raw, list):
        return []
    rows: List[CanonicalPosition] = []
    for item in positions_raw:
        if not isinstance(item, dict):
            continue
        size_value = _decimal_or_none(item.get("position"))
        if size_value is None or size_value == 0:
            continue
        sign_value = int(item.get("sign") or 0)
        side = "long" if sign_value >= 0 else "short"
        try:
            market_id = int(item.get("market_id"))
        except Exception:  # noqa: BLE001
            market_id = -1
        market_info = (market_map or {}).get(market_id, {})
        symbol = str(item.get("symbol") or market_info.get("symbol") or f"MARKET-{market_id}").strip().upper()
        precision_value = market_info.get("price_precision")
        try:
            price_precision = int(precision_value) if precision_value is not None else int((price_precisions or {}).get(market_id, 1))
        except Exception:  # noqa: BLE001
            price_precision = int((price_precisions or {}).get(market_id, 1))
        entry_price_value = _decimal_or_none(item.get("avg_entry_price")) or Decimal("0")
        pnl_value = (_decimal_or_none(item.get("unrealized_pnl")) or Decimal("0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        pnl_text = _decimal_text(pnl_value)
        if pnl_value > 0:
            pnl_text = f"+{pnl_text}"
        rows.append(
            CanonicalPosition(
                symbol=symbol,
                side=side,
                size=_decimal_text(abs(size_value)),
                entry_price=_format_decimal_places(entry_price_value, max(0, price_precision)),
                pnl=pnl_text,
            )
        )
    rows.sort(key=lambda item: (item.symbol, item.side))
    return rows


def _position_action_result(
    *,
    operation: str,
    symbol: str,
    verified: bool,
    price: Optional[str] = None,
    removed: Optional[bool] = None,
    current_side: Optional[str] = None,
    current_size: Optional[str] = None,
    message: Optional[str] = None,
    exchange_order_id: Optional[int] = None,
    status: str = "success",
) -> CanonicalPositionActionResult:
    return CanonicalPositionActionResult(
        operation=operation,
        symbol=symbol,
        verified=verified,
        price=price,
        removed=removed,
        status=status,
        exchange_order_id=exchange_order_id,
        current_side=current_side,
        current_size=current_size,
        message=message,
    )


def _classify_protection_orders(*, orders: List[Dict[str, Any]], market_id: int, closing_side: str) -> Dict[str, List[Dict[str, Any]]]:
    bucket: Dict[str, List[Dict[str, Any]]] = {"tp": [], "sl": []}
    for order in orders:
        if not isinstance(order, dict):
            continue
        status = str(order.get("status") or "").strip().lower()
        if status and status not in {"open", "pending", "in-progress"}:
            continue
        try:
            order_market_id = int(str(order.get("market_index") or 0))
        except Exception:  # noqa: BLE001
            continue
        if order_market_id != market_id:
            continue
        side = "sell" if bool(order.get("is_ask")) else "buy"
        if side != closing_side:
            continue
        if not bool(order.get("reduce_only")):
            continue
        order_type = str(order.get("type") or "").strip().lower()
        if order_type in {"take-profit", "take-profit-limit"}:
            bucket["tp"].append(order)
        elif order_type in {"stop-loss", "stop-loss-limit"}:
            bucket["sl"].append(order)
    return bucket


def _augment_positions_with_protection(
    positions: List[CanonicalPosition],
    *,
    target: Dict[str, Any],
    active_orders: List[Dict[str, Any]],
    market_map: Optional[Dict[int, Dict[str, Any]]] = None,
) -> List[CanonicalPosition]:
    raw_positions = target.get("positions") if isinstance(target, dict) else None
    if not isinstance(raw_positions, list):
        return positions
    raw_by_key: Dict[tuple[str, str], Dict[str, Any]] = {}
    for item in raw_positions:
        if not isinstance(item, dict):
            continue
        size_value = _decimal_or_none(item.get("position"))
        if size_value is None or size_value == 0:
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        side = "long" if int(item.get("sign") or 0) >= 0 else "short"
        raw_by_key[(symbol, side)] = item
    enriched: List[CanonicalPosition] = []
    for position in positions:
        raw = raw_by_key.get((position.symbol, position.side))
        if not raw:
            enriched.append(position)
            continue
        try:
            market_id = int(str(raw.get("market_id") or 0))
        except Exception:  # noqa: BLE001
            market_id = 0
        closing_side = "sell" if position.side == "long" else "buy"
        protection = _classify_protection_orders(orders=active_orders, market_id=market_id, closing_side=closing_side)
        tp_orders = protection["tp"]
        sl_orders = protection["sl"]
        market_info = (market_map or {}).get(market_id, {})
        price_precision = int(market_info.get("price_precision") or market_info.get("price_decimals") or market_info.get("supported_price_decimals") or 0)
        tp_price = _format_decimal_places(_decimal_or_none(tp_orders[0].get("trigger_price") or tp_orders[0].get("price")) or Decimal("0"), price_precision) if tp_orders else None
        sl_price = _format_decimal_places(_decimal_or_none(sl_orders[0].get("trigger_price") or sl_orders[0].get("price")) or Decimal("0"), price_precision) if sl_orders else None
        enriched.append(CanonicalPosition(
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
    return enriched


def _aggregate_open_orders(
    orders: List[Dict[str, Any]],
    market_map: Optional[Dict[int, Dict[str, Any]]] = None,
    fallback_symbols: Optional[Dict[int, str]] = None,
) -> List[CanonicalOrderGroup]:
    grouped: Dict[tuple[str, str], Dict[str, Any]] = {}
    for order in orders:
        if not isinstance(order, dict):
            continue
        status = str(order.get("status") or "").strip().lower()
        if status and status not in {"open", "pending", "in-progress"}:
            continue
        try:
            market_id = int(order.get("market_index"))
        except Exception:  # noqa: BLE001
            continue
        market_info = (market_map or {}).get(market_id, {})
        symbol = str(market_info.get("symbol") or (fallback_symbols or {}).get(market_id) or f"MARKET-{market_id}").strip().upper()
        side = "sell" if bool(order.get("is_ask")) else "buy"
        size = _decimal_or_none(order.get("remaining_base_amount"))
        if size is None or size <= 0:
            size = _decimal_or_none(order.get("initial_base_amount")) or Decimal("0")
        if size <= 0:
            continue
        price = _decimal_or_none(order.get("price")) or Decimal("0")
        precision_value = market_info.get("price_precision")
        try:
            price_precision = int(precision_value) if precision_value is not None else _decimal_places(order.get("price"))
        except Exception:  # noqa: BLE001
            price_precision = _decimal_places(order.get("price"))
        key = (symbol, side)
        group = grouped.setdefault(
            key,
            {
                "symbol": symbol,
                "side": side,
                "order_count": 0,
                "total_size": Decimal("0"),
                "notional": Decimal("0"),
                "min_price": None,
                "max_price": None,
                "price_precision": price_precision,
            },
        )
        group["order_count"] += 1
        group["total_size"] += size
        group["notional"] += price * size
        group["price_precision"] = max(int(group.get("price_precision") or 0), price_precision)
        if group["min_price"] is None or price < group["min_price"]:
            group["min_price"] = price
        if group["max_price"] is None or price > group["max_price"]:
            group["max_price"] = price

    rows: List[CanonicalOrderGroup] = []
    for group in grouped.values():
        total_size: Decimal = group["total_size"]
        vwap = (group["notional"] / total_size) if total_size != 0 else Decimal("0")
        rows.append(
            CanonicalOrderGroup(
                symbol=group["symbol"],
                side=group["side"],
                order_count=int(group["order_count"]),
                total_size=_decimal_text(total_size),
                vwap=_format_decimal_places(vwap, int(group.get("price_precision") or 0)),
                min_price=_decimal_text(group["min_price"]),
                max_price=_decimal_text(group["max_price"]),
            )
        )
    rows.sort(key=lambda item: (item.symbol, item.side))
    return rows


def _to_portfolio_summary(target: Dict[str, Any]) -> CanonicalPortfolioSummary:
    return CanonicalPortfolioSummary(
        account_value=_quantize_2(target.get("total_asset_value", "0")),
        withdrawable=_quantize_2(target.get("available_balance", "0")),
        margin_used=_quantize_2(target.get("cross_initial_margin_requirement", "0")),
        total_position_value=_quantize_2(target.get("cross_asset_value", "0")),
        unit="USDC",
    )


def _balance(request: Dict[str, Any]) -> CanonicalResponse:
    fetched = _fetch_account_entry(request)
    target = fetched["target"]
    portfolio_summary = _to_portfolio_summary(target)
    balance = normalize_balance(target.get("total_asset_value", target.get("collateral", "0")), "USDC")
    return make_success(
        operation="balance",
        exchange=name,
        account=fetched["account"],
        balance=balance,
        portfolio_summary=portfolio_summary,
        positions=[],
    )


def _positions_orders(request: Dict[str, Any]) -> CanonicalResponse:
    fetched = _fetch_account_entry(request)
    target = fetched["target"]
    credentials = fetched["credentials"]
    auth_token = str(fetched["auth_token"] or "")
    active_orders = _fetch_active_orders(credentials, auth_token)
    market_map: Dict[int, Dict[str, Any]] = {}
    try:
        market_map = _fetch_market_symbol_map(credentials["base_url"])
    except Exception as exc:  # noqa: BLE001
        logger.info("Lighter orderBookDetails lookup failed; falling back to account symbols: %s", exc)
    fallback_symbols: Dict[int, str] = {}
    observed_price_precisions: Dict[int, int] = {}
    positions_raw = target.get("positions")
    if isinstance(positions_raw, list):
        for item in positions_raw:
            if not isinstance(item, dict):
                continue
            try:
                market_id = int(item.get("market_id"))
            except Exception:  # noqa: BLE001
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            if symbol:
                fallback_symbols[market_id] = symbol
    for order in active_orders:
        if not isinstance(order, dict):
            continue
        try:
            market_id = int(order.get("market_index"))
        except Exception:  # noqa: BLE001
            continue
        observed_price_precisions[market_id] = max(observed_price_precisions.get(market_id, 0), _decimal_places(order.get("price")))
    positions = _normalize_positions(target, market_map=market_map, price_precisions=observed_price_precisions)
    order_groups = _aggregate_open_orders(active_orders, market_map=market_map, fallback_symbols=fallback_symbols)
    return make_success(
        operation="positions_orders",
        exchange=name,
        account=fetched["account"],
        positions=positions,
        open_order_count=len(active_orders),
        order_groups=order_groups,
    )


def _positions_management(request: Dict[str, Any]) -> CanonicalResponse:
    fetched = _fetch_account_entry(request)
    target = fetched["target"]
    credentials = fetched["credentials"]
    auth_token = str(fetched["auth_token"] or "")
    active_orders = _fetch_active_orders(credentials, auth_token)
    market_map: Dict[int, Dict[str, Any]] = {}
    try:
        market_map = _fetch_market_symbol_map(credentials["base_url"])
    except Exception as exc:  # noqa: BLE001
        logger.info("Lighter orderBookDetails lookup failed during positions_management; falling back to account symbols: %s", exc)
    observed_price_precisions: Dict[int, int] = {}
    for order in active_orders:
        if not isinstance(order, dict):
            continue
        try:
            market_id = int(str(order.get("market_index") or 0))
        except Exception:  # noqa: BLE001
            continue
        observed_price_precisions[market_id] = max(observed_price_precisions.get(market_id, 0), _decimal_places(order.get("price")))
    positions = _normalize_positions(target, market_map=market_map, price_precisions=observed_price_precisions)
    positions = _augment_positions_with_protection(positions, target=target, active_orders=active_orders, market_map=market_map)
    return make_success(operation="positions_management", exchange=name, account=fetched["account"], positions=positions)


def _find_position_management_context(request: Dict[str, Any], *, include_active_orders: bool = True) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any], List[Dict[str, Any]], str, Decimal, str, str]:
    fetched = _fetch_account_entry(request)
    credentials = fetched["credentials"]
    target = fetched["target"]
    requested_symbol = str(request.get("symbol") or "").strip().upper()
    if not requested_symbol:
        raise ValueError("MISSING_SYMBOL")
    market = _resolve_market(credentials["base_url"], requested_symbol)
    if market is None:
        raise LookupError("INSTRUMENT_NOT_FOUND")
    canonical = str(market.get("symbol") or requested_symbol).strip().upper()
    canonical_mid = int(market.get("market_id") or 0)
    raw_positions = target.get("positions") if isinstance(target, dict) else None
    if not isinstance(raw_positions, list):
        raise LookupError("POSITION_NOT_FOUND")
    current_raw = None
    for item in raw_positions:
        if not isinstance(item, dict):
            continue
        item_sym = str(item.get("symbol") or "").strip().upper()
        try:
            item_mid = int(str(item.get("market_id") or 0) or 0)
        except Exception:
            item_mid = 0
        if item_sym != canonical and not (canonical_mid and item_mid == canonical_mid):
            continue
        size_value = _decimal_or_none(item.get("position"))
        if size_value in (None, Decimal("0")):
            continue
        current_raw = item
        break
    if current_raw is None:
        raise LookupError("POSITION_NOT_FOUND")
    market_map = _fetch_market_symbol_map(credentials["base_url"])
    market_id = int(str(current_raw.get("market_id") or canonical_mid or 0))
    mapped = dict(market_map.get(market_id) or {})
    if mapped.get("market_id"):
        market = dict(mapped)
    if not market.get("market_id"):
        market["market_id"] = market_id
    sign_value = int(current_raw.get("sign") or 0)
    current_side = "long" if sign_value >= 0 else "short"
    closing_side = "sell" if current_side == "long" else "buy"
    size_decimals = int(market.get("size_decimals") or market.get("supported_size_decimals") or 0)
    current_size = _quantize_down(abs(_decimal_or_none(current_raw.get("position")) or Decimal("0")), size_decimals)
    auth_token = str(fetched.get("auth_token") or "")
    active_orders = _fetch_active_orders(credentials, auth_token) if include_active_orders else []
    return fetched, credentials, target, market, active_orders, current_side, current_size, closing_side, auth_token


def _submit_tpsl_order(credentials: Dict[str, Any], market: Dict[str, Any], *, operation: str, current_size: Decimal, closing_side: str, trigger_price: Decimal) -> Dict[str, Any]:
    async def _run_submit() -> Dict[str, Any]:
        signer = _build_signer_client(credentials)
        try:
            size_decimals = int(market.get("size_decimals") or market.get("supported_size_decimals") or 0)
            price_decimals = int(market.get("price_decimals") or market.get("supported_price_decimals") or 0)
            submitted_volume = _quantize_down(current_size, size_decimals)
            submitted_price = _quantize_down(trigger_price, price_decimals)
            base_amount = _to_scaled_int(submitted_volume, size_decimals)
            trigger_price_int = _to_scaled_int(submitted_price, price_decimals)
            client_order_index = int(time.time_ns() % LIGHTER_MAX_CLIENT_ORDER_INDEX) or 1
            submitter = signer.create_tp_order if operation == "set_tp" else signer.create_sl_order
            tx, api_response, error = await submitter(
                int(market["market_id"]),
                client_order_index,
                base_amount,
                trigger_price_int,
                trigger_price_int,
                closing_side == "sell",
                reduce_only=True,
                api_key_index=credentials["api_key_index"],
            )
            if error:
                raise RuntimeError(f"Lighter position action failed: {error}")
            return {"exchange_order_id": getattr(tx, "order_index", None), "submitted_price": _decimal_text(submitted_price), "submitted_volume": _decimal_text(submitted_volume), "tx_hash": getattr(api_response, "tx_hash", None)}
        finally:
            api_client = getattr(signer, "api_client", None)
            if api_client is not None and hasattr(api_client, "close"):
                try:
                    await api_client.close()
                except Exception:
                    pass
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run_submit())
    result: Dict[str, Any] = {}
    def _runner() -> None:
        try:
            result["value"] = asyncio.run(_run_submit())
        except Exception as exc:
            result["error"] = exc
    thread = threading.Thread(target=_runner, name=f"lighter-{operation}", daemon=True)
    thread.start(); thread.join()
    if "error" in result:
        raise result["error"]
    return dict(result.get("value") or {})


def _submit_close_position(
    credentials: Dict[str, Any],
    market: Dict[str, Any],
    *,
    current_size: Decimal,
    closing_side: str,
    client_order_index: Optional[int] = None,
) -> Dict[str, Any]:
    async def _run_submit() -> Dict[str, Any]:
        signer = _build_signer_client(credentials)
        try:
            size_decimals = int(market.get("size_decimals") or market.get("supported_size_decimals") or 0)
            submitted_volume = _quantize_down(current_size, size_decimals)
            base_amount = _to_scaled_int(submitted_volume, size_decimals)
            if client_order_index is not None:
                coi = int(client_order_index)
                if coi <= 0 or coi > LIGHTER_MAX_CLIENT_ORDER_INDEX:
                    raise ValueError(
                        f"client_order_index out of Lighter range: {coi}"
                    )
            else:
                # Legacy /trade and non-GF callers: time-based id (unchanged).
                coi = int(time.time_ns() % LIGHTER_MAX_CLIENT_ORDER_INDEX) or 1
            tx, api_response, error = await signer.create_market_order_limited_slippage(
                int(market["market_id"]),
                coi,
                base_amount,
                LIGHTER_CLOSE_MAX_SLIPPAGE,
                closing_side == "sell",
                reduce_only=True,
                api_key_index=credentials["api_key_index"],
            )
            if error:
                raise RuntimeError(f"Lighter close position failed: {error}")
            return {
                "exchange_order_id": getattr(tx, "order_index", None),
                "submitted_volume": _decimal_text(submitted_volume),
                "tx_hash": getattr(api_response, "tx_hash", None),
                "client_order_index": coi,
            }
        finally:
            api_client = getattr(signer, "api_client", None)
            if api_client is not None and hasattr(api_client, "close"):
                try:
                    await api_client.close()
                except Exception:
                    pass
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run_submit())
    result: Dict[str, Any] = {}
    def _runner() -> None:
        try:
            result["value"] = asyncio.run(_run_submit())
        except Exception as exc:
            result["error"] = exc
    thread = threading.Thread(target=_runner, name='lighter-close-position', daemon=True)
    thread.start(); thread.join()
    if "error" in result:
        raise result["error"]
    return dict(result.get("value") or {})


def _verify_tpsl_state(*, credentials: Dict[str, Any], auth_token: str, market_id: int, closing_side: str, operation: str, expected_price: Optional[str]) -> tuple[bool, Optional[int]]:
    target_key = 'tp' if operation == 'set_tp' else 'sl'
    matched_order_id: Optional[int] = None
    for attempt in range(LIGHTER_VERIFY_ATTEMPTS):
        orders = _fetch_active_orders(credentials, auth_token)
        bucket = _classify_protection_orders(orders=orders, market_id=market_id, closing_side=closing_side)
        current = bucket[target_key]
        if expected_price is None:
            if not current:
                return True, None
        else:
            for order in current:
                trigger_text = _decimal_text(_decimal_or_none(order.get('trigger_price') or order.get('price')))
                if trigger_text == _decimal_text(expected_price):
                    try:
                        matched_order_id = int(str(order.get('order_index') or order.get('order_id') or 0))
                    except Exception:
                        matched_order_id = None
                    return True, matched_order_id
        if attempt < LIGHTER_VERIFY_ATTEMPTS - 1:
            time.sleep(LIGHTER_VERIFY_DELAY_SECONDS)
    return False, matched_order_id


def _verify_position_closed(request: Dict[str, Any], *, symbol: str) -> bool:
    expected = str(symbol or '').strip().upper()
    for attempt in range(LIGHTER_VERIFY_ATTEMPTS):
        fetched = _fetch_account_entry(request)
        target = fetched.get('target') if isinstance(fetched, dict) else None
        raw_positions = target.get('positions') if isinstance(target, dict) else None
        still_open = False
        if isinstance(raw_positions, list):
            for item in raw_positions:
                if not isinstance(item, dict):
                    continue
                if str(item.get('symbol') or '').strip().upper() != expected:
                    continue
                size_value = _decimal_or_none(item.get('position'))
                if size_value not in (None, Decimal('0')):
                    still_open = True
                    break
        if not still_open:
            return True
        if attempt < LIGHTER_VERIFY_ATTEMPTS - 1:
            time.sleep(LIGHTER_VERIFY_DELAY_SECONDS)
    return False


def _execute_set_tpsl(request: Dict[str, Any], *, operation: str) -> CanonicalResponse:
    account_name = str(request.get('account') or '').strip()
    requested_symbol = str(request.get('symbol') or '').strip().upper()
    requested_price = _decimal_or_none(request.get('price'))
    if not requested_symbol:
        return make_failure(operation=operation, exchange=name, account=account_name, code='MISSING_SYMBOL', message='Symbol is required.')
    if requested_price is None or requested_price < 0:
        return make_failure(operation=operation, exchange=name, account=account_name, code=('INVALID_TP_PRICE' if operation == 'set_tp' else 'INVALID_SL_PRICE'), message=('TP price must be numeric and non-negative.' if operation == 'set_tp' else 'SL price must be numeric and non-negative.'))
    try:
        _fetched, credentials, _target, market, active_orders, current_side, current_size, closing_side, auth_token = _find_position_management_context(request)
    except ValueError:
        return make_failure(operation=operation, exchange=name, account=account_name, code='MISSING_SYMBOL', message='Symbol is required.')
    except LookupError as exc:
        code = str(exc) or 'POSITION_NOT_FOUND'
        return make_failure(operation=operation, exchange=name, account=account_name, code=code, message=('Open position not found.' if code == 'POSITION_NOT_FOUND' else 'Instrument not found.'))
    except Exception as exc:
        return make_failure(operation=operation, exchange=name, account=account_name, code='POSITION_CONTEXT_UNAVAILABLE', message=sanitize_error_message(str(exc)))
    market_id = int(str(market.get('market_id') or 0))
    bucket = _classify_protection_orders(orders=active_orders, market_id=market_id, closing_side=closing_side)
    target_key = 'tp' if operation == 'set_tp' else 'sl'
    existing_orders = list(bucket[target_key])
    if len(existing_orders) > 1:
        return make_failure(operation=operation, exchange=name, account=account_name, code='AMBIGUOUS_PROTECTION_STATE', message='Multiple matching TP/SL orders were found.')
    existing_order = existing_orders[0] if existing_orders else None
    if requested_price == 0:
        if existing_order is None:
            action = _position_action_result(operation=operation, symbol=requested_symbol, verified=True, removed=False, current_side=current_side, current_size=_decimal_text(current_size), message=('No Take Profit was set.' if operation == 'set_tp' else 'No Stop Loss was set.'))
            return make_success(operation=operation, exchange=name, account=account_name, position_action=action)
        try:
            _submit_cancel_order(credentials, existing_order, reason='remove')
            verified, _matched = _verify_tpsl_state(credentials=credentials, auth_token=auth_token, market_id=market_id, closing_side=closing_side, operation=operation, expected_price=None)
        except Exception as exc:
            action = _position_action_result(operation=operation, symbol=requested_symbol, verified=False, removed=True, current_side=current_side, current_size=_decimal_text(current_size), status='failed')
            return make_failure(operation=operation, exchange=name, account=account_name, code=('TP_REMOVAL_FAILED' if operation == 'set_tp' else 'SL_REMOVAL_FAILED'), message=sanitize_error_message(str(exc)), position_action=action)
        action = _position_action_result(operation=operation, symbol=requested_symbol, verified=verified, removed=True, current_side=current_side, current_size=_decimal_text(current_size), message=('Take Profit removed.' if operation == 'set_tp' else 'Stop Loss removed.'), status=('success' if verified else 'failed'))
        if verified:
            return make_success(operation=operation, exchange=name, account=account_name, position_action=action)
        return make_failure(operation=operation, exchange=name, account=account_name, code='VERIFICATION_FAILED', message='TP/SL removal could not be verified.', position_action=action)
    try:
        submitted_price = _quantize_down(requested_price, int(market.get('price_decimals') or market.get('supported_price_decimals') or 0))
        if existing_order is not None:
            _submit_cancel_order(credentials, existing_order, reason='replace')
        submit_result = _submit_tpsl_order(credentials, market, operation=operation, current_size=current_size, closing_side=closing_side, trigger_price=submitted_price)
        verified, verified_oid = _verify_tpsl_state(credentials=credentials, auth_token=auth_token, market_id=market_id, closing_side=closing_side, operation=operation, expected_price=_decimal_text(submitted_price))
    except Exception as exc:
        action = _position_action_result(operation=operation, symbol=requested_symbol, verified=False, price=_decimal_text(requested_price), current_side=current_side, current_size=_decimal_text(current_size), status='failed')
        return make_failure(operation=operation, exchange=name, account=account_name, code='ORDER_SUBMISSION_FAILED', message=sanitize_error_message(str(exc)), position_action=action)
    action = _position_action_result(operation=operation, symbol=requested_symbol, verified=verified, price=str(submit_result.get('submitted_price') or _decimal_text(submitted_price)), current_side=current_side, current_size=_decimal_text(current_size), exchange_order_id=verified_oid or submit_result.get('exchange_order_id'), message=('Take Profit updated.' if operation == 'set_tp' else 'Stop Loss updated.'), status=('success' if verified else 'failed'))
    if verified:
        return make_success(operation=operation, exchange=name, account=account_name, position_action=action)
    return make_failure(operation=operation, exchange=name, account=account_name, code='VERIFICATION_FAILED', message='TP/SL update could not be verified.', position_action=action)


def _execute_close_position(request: Dict[str, Any]) -> CanonicalResponse:
    account_name = str(request.get('account') or '').strip()
    requested_symbol = str(request.get('symbol') or '').strip().upper()
    if not requested_symbol:
        return make_failure(operation='close_position', exchange=name, account=account_name, code='MISSING_SYMBOL', message='Symbol is required.')
    # Optional GoldenFibo V2 (or any caller-supplied) client_order_index.
    # When omitted, _submit_close_position keeps the legacy time_ns id
    # so /trade and other callers are unchanged.
    coi_raw = request.get("client_order_id")
    if coi_raw is None:
        coi_raw = request.get("client_order_index")
    client_order_index = None
    if coi_raw is not None and str(coi_raw).strip() != "":
        try:
            client_order_index = int(coi_raw)
        except (TypeError, ValueError):
            return make_failure(
                operation="close_position",
                exchange=name,
                account=account_name,
                code="INVALID_INPUTS",
                message="client_order_id must be an integer",
            )
    try:
        _fetched, credentials, _target, market, _active_orders, current_side, current_size, closing_side, _auth_token = _find_position_management_context(request, include_active_orders=False)
        submit_result = _submit_close_position(
            credentials,
            market,
            current_size=current_size,
            closing_side=closing_side,
            client_order_index=client_order_index,
        )
        verified = _verify_position_closed(request, symbol=requested_symbol)
    except LookupError as exc:
        code = str(exc) or 'POSITION_NOT_FOUND'
        return make_failure(operation='close_position', exchange=name, account=account_name, code=code, message=('Open position not found.' if code == 'POSITION_NOT_FOUND' else 'Instrument not found.'))
    except Exception as exc:
        action = _position_action_result(operation='close_position', symbol=requested_symbol, verified=False, status='failed')
        return make_failure(operation='close_position', exchange=name, account=account_name, code='ORDER_SUBMISSION_FAILED', message=sanitize_error_message(str(exc)), position_action=action)
    action = _position_action_result(operation='close_position', symbol=requested_symbol, verified=verified, current_side=current_side, current_size=_decimal_text(current_size), exchange_order_id=submit_result.get('exchange_order_id'), message='Position closed.', status=('success' if verified else 'failed'))
    if isinstance(action, dict) and submit_result.get("client_order_index") is not None:
        action["client_order_index"] = submit_result.get("client_order_index")
    if verified:
        return make_success(operation='close_position', exchange=name, account=account_name, position_action=action)
    return make_failure(operation='close_position', exchange=name, account=account_name, code='VERIFICATION_FAILED', message='Position close could not be verified.', position_action=action)


# ---------------------------------------------------------------------------
# GoldenFibo-required generic ops
#
# These are exchange-agnostic surfaces that any compliant perpetual-Dex
# adapter must expose for the GoldenFibo strategy to be implementable.
# GoldenFibo itself is not coded here — only the agent-level surface
# used by GoldenFibo's engine.
# ---------------------------------------------------------------------------

# Status taxonomy for the get_order_state op. The GoldenFibo engine
# uses these to decide whether to advance the cycle, hold (active),
# freeze (terminal failure), or clear (filled and position flat).
_LIGHTER_ORDER_STATUS_ACTIVE = "ACTIVE"
_LIGHTER_ORDER_STATUS_FILLED = "FILLED"
_LIGHTER_ORDER_STATUS_CANCELED = "CANCELED"
_LIGHTER_ORDER_STATUS_REJECTED = "REJECTED"
_LIGHTER_ORDER_STATUS_EXPIRED = "EXPIRED"
_LIGHTER_ORDER_STATUS_UNKNOWN = "UNKNOWN"


# State literals we use to interpret the venue's `status` field and
# transform it into the GoldenFibo taxonomy. The mapping is conservative:
# when the venue status is not in the known set, we fall back to
# UNKNOWN rather than guessing.
_LIGHTER_VENUE_STATUS_TO_TAXONOMY = {
    "open": _LIGHTER_ORDER_STATUS_ACTIVE,
    "active": _LIGHTER_ORDER_STATUS_ACTIVE,
    "pending": _LIGHTER_ORDER_STATUS_ACTIVE,
    "resting": _LIGHTER_ORDER_STATUS_ACTIVE,
    "filled": _LIGHTER_ORDER_STATUS_FILLED,
    "canceled": _LIGHTER_ORDER_STATUS_CANCELED,
    "cancelled": _LIGHTER_ORDER_STATUS_CANCELED,
    "rejected": _LIGHTER_ORDER_STATUS_REJECTED,
    "expired": _LIGHTER_ORDER_STATUS_EXPIRED,
    "expire": _LIGHTER_ORDER_STATUS_EXPIRED,
}


def _actual_fill_price(
    order: Dict[str, Any],
    *,
    size_decimals: int,
    price_decimals: int,
) -> Optional[Decimal]:
    """Compute the actual average fill price for a Lighter order.

    Per the Lighter Order model (lighter/models/order.py), a filled
    order carries ``filled_base_amount`` and ``filled_quote_amount``
    as scaled integers. The actual average execution price is the
    ratio of the two:

        fill_price = (filled_quote_amount / 10**price_decimals)
                    / (filled_base_amount  / 10**size_decimals)

    For an unfilled (resting) order, both are zero and we return
    None — the caller should use the order's submitted ``price`` for
    a resting limit display purposes.

    Never substitutes mark price, requested price, or position average
    entry for a missing actual fill price.
    """
    filled_base = _decimal_or_none(order.get("filled_base_amount"))
    filled_quote = _decimal_or_none(order.get("filled_quote_amount"))
    if filled_base is None or filled_quote is None:
        return None
    if filled_base <= 0 or filled_quote <= 0:
        return None
    base_scale = Decimal(10) ** int(size_decimals)
    quote_scale = Decimal(10) ** int(price_decimals)
    base_dec = filled_base / base_scale
    quote_dec = filled_quote / quote_scale
    if base_dec == 0:
        return None
    return quote_dec / base_dec


def _classify_order_status(order: Dict[str, Any]) -> str:
    """Map a Lighter Order object to the GoldenFibo status taxonomy.

    The mapping is intentionally conservative: any unrecognized status
    returns UNKNOWN so the engine freezes rather than acting on a
    guess. The caller should treat UNKNOWN as a freeze signal.
    """
    if not isinstance(order, dict):
        return _LIGHTER_ORDER_STATUS_UNKNOWN
    raw = str(order.get("status") or "").strip().lower()
    if not raw:
        return _LIGHTER_ORDER_STATUS_UNKNOWN
    return _LIGHTER_VENUE_STATUS_TO_TAXONOMY.get(raw, _LIGHTER_ORDER_STATUS_UNKNOWN)


# Public Lighter base URLs (also used by _execute_market_price for
# credential-less price reads).
_LIGHTER_URL_ARBITRUM = "https://mainnet.zklighter.elliot.ai"
_LIGHTER_URL_ROBINHOOD = "https://robinhood.zklighter.elliot.ai"


def _execute_resolve_instrument(request: Dict[str, Any]) -> CanonicalResponse:
    """Resolve a symbol to its market metadata (size/price decimals)."""
    account_name = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account_name)
    if not credentials:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account_name,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Lighter account configuration",
        )
    requested_symbol = str(request.get("symbol") or "").strip().upper()
    if not requested_symbol:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account_name,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    market = _resolve_market(credentials["base_url"], requested_symbol)
    if market is None:
        return make_failure(
            operation="resolve_instrument",
            exchange=name,
            account=account_name,
            code="INSTRUMENT_NOT_FOUND",
            message=f"Instrument not found: {requested_symbol}",
        )
    payload = {
        "symbol": str(market.get("symbol") or requested_symbol).strip().upper(),
        "market_id": market.get("market_id"),
        "size_decimals": int(market.get("size_decimals") or 0),
        "price_decimals": int(market.get("price_decimals") or 0),
        "min_base_amount": str(market.get("min_base_amount") or ""),
        "tick_size": str(market.get("tick_size") or ""),
    }
    instrument_obj = CanonicalInstrument(
        requested_symbol=requested_symbol,
        symbol=payload.get("symbol") or requested_symbol,
        display_name=payload.get("display_name") or "",
        price_increment=payload.get("tick_size") or None,
        size_increment=None,
        minimum_size=payload.get("min_base_amount") or None,
    )
    return make_success(
        operation="resolve_instrument",
        exchange=name,
        account=account_name,
        instrument=instrument_obj,
    )


def _execute_market_constraints(request: Dict[str, Any]) -> CanonicalResponse:
    """Generic read of full venue constraints for a symbol.

    Not GoldenFibo-specific. Returns min_base_amount, min_quote_amount,
    size_decimals, price_decimals, tick_size, market_id. Read-only.
    """
    account_name = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account_name)
    if not credentials:
        return make_failure(
            operation="market_constraints",
            exchange=name,
            account=account_name,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Lighter account configuration",
        )
    requested_symbol = str(request.get("symbol") or "").strip().upper()
    if not requested_symbol:
        return make_failure(
            operation="market_constraints",
            exchange=name,
            account=account_name,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    market = _resolve_market(credentials["base_url"], requested_symbol)
    if market is None:
        return make_failure(
            operation="market_constraints",
            exchange=name,
            account=account_name,
            code="INSTRUMENT_NOT_FOUND",
            message=f"Instrument not found: {requested_symbol}",
        )
    constraints = {
        "symbol": requested_symbol,
        "market_id": market.get("market_id"),
        "size_decimals": int(market.get("size_decimals") or market.get("supported_size_decimals") or 0),
        "price_decimals": int(market.get("price_decimals") or market.get("supported_price_decimals") or 0),
        "min_base_amount": str(market.get("min_base_amount") or ""),
        "min_quote_amount": str(market.get("min_quote_amount") or ""),
        "tick_size": str(market.get("tick_size") or ""),
    }
    return make_success(
        operation="market_constraints",
        exchange=name,
        account=account_name,
        order_state=constraints,
    )


def _fetch_lighter_inactive_orders(
    credentials: Dict[str, Any], auth_token: str
) -> List[Dict[str, Any]]:
    """Read paginated inactive orders for the Lighter account.

    Used by _execute_get_order_state to locate orders that have left
    the active list and to retrieve the actual fill price. Returns
    the raw shape (lighter.models.order.Order as dict).
    """
    limiter = _get_lighter_limiter(credentials)
    auth_token = str(auth_token or "")
    out: List[Dict[str, Any]] = []
    limit = 200
    offset = 0
    try:
        while True:
            limiter.acquire()
            response = requests.get(
                f"{credentials['base_url']}/api/v1/accountInactiveOrders",
                params={
                    "account_index": str(credentials["account_index"]),
                    "limit": str(limit),
                    "offset": str(offset),
                },
                headers={"Accept": "application/json", "authorization": auth_token},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json() or {}
            batch = payload.get("orders") or []
            if not isinstance(batch, list):
                break
            out.extend(batch)
            if len(batch) < limit:
                break
            offset += len(batch)
    except Exception as exc:  # noqa: BLE001
        retry_after = _looks_like_lighter_429(exc)
        if retry_after is not None:
            limiter.record_failure(retry_after)
        raise
    return out


def _fetch_lighter_public_market_price(
    base_url: str, market: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Read best-bid / best-ask / mark for one Lighter market.

    Returns a dict with ``mark``, ``last_trade``, ``bid``, ``ask``,
    ``ts`` or ``None``. The caller MUST NOT substitute this for an
    actual fill price.
    """
    market_id = market.get("market_id")
    try:
        response = requests.get(
            f"{base_url}/api/v1/orderBookDetails",
            params={"market_id": int(market_id)},
            headers={"Accept": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
    except Exception:  # noqa: BLE001
        return None
    payload = response.json() or {}
    details = payload.get("order_book_details") or payload.get("orderBookDetails") or []
    if not isinstance(details, list) or not details:
        return None
    first = details[0] if isinstance(details[0], dict) else {}
    last_price = _decimal_or_none(first.get("last_trade_price") or first.get("lastTradePrice"))
    mark_price = _decimal_or_none(first.get("mark_price") or first.get("markPrice"))
    bid = _decimal_or_none(first.get("best_bid") or first.get("bestBid"))
    ask = _decimal_or_none(first.get("best_ask") or first.get("bestAsk"))
    return {
        "mark": _decimal_text(mark_price) if mark_price is not None else None,
        "last_trade": _decimal_text(last_price) if last_price is not None else None,
        "bid": _decimal_text(bid) if bid is not None else None,
        "ask": _decimal_text(ask) if ask is not None else None,
        "ts": int(first.get("timestamp") or 0),
    }


def _execute_market_price(request: Dict[str, Any]) -> CanonicalResponse:
    """Return the current mark price for the requested symbol."""
    requested_symbol = str(request.get("symbol") or "").strip().upper()
    if not requested_symbol:
        return make_failure(
            operation="market_price",
            exchange=name,
            account="",
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    account_name = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account_name) if account_name else None
    if credentials is not None:
        market = _resolve_market(credentials["base_url"], requested_symbol)
        if market is None:
            return make_failure(
                operation="market_price",
                exchange=name,
                account=account_name,
                code="INSTRUMENT_NOT_FOUND",
                message=f"Instrument not found: {requested_symbol}",
            )
        payload = _fetch_lighter_public_market_price(credentials["base_url"], market)
        if payload is None:
            return make_failure(
                operation="market_price",
                exchange=name,
                account=account_name,
                code="PRICE_UNAVAILABLE",
                message=f"Price unavailable for {requested_symbol}",
            )
        payload["symbol"] = requested_symbol
        mp = CanonicalMarketPrice(
            requested_symbol=requested_symbol,
            market=requested_symbol,
            mark_price=payload.get("mark") if isinstance(payload, dict) else None,
            last_external_price=payload.get("last_trade") if isinstance(payload, dict) else None,
            price=payload.get("mark") if isinstance(payload, dict) else None,
        )
        return make_success(
            operation="market_price",
            exchange=name,
            account=account_name,
            market_price=mp,
        )
    # Credential-less mode: try each public base URL.
    for base_url in (_LIGHTER_URL_ARBITRUM, _LIGHTER_URL_ROBINHOOD):
        try:
            market = _resolve_market(base_url, requested_symbol)
            if market is None:
                continue
            payload = _fetch_lighter_public_market_price(base_url, market)
            if payload is None:
                continue
            payload["symbol"] = requested_symbol
            return make_success(
                operation="market_price",
                exchange=name,
                account=account_name,
                market_price=payload,
            )
        except Exception:  # noqa: BLE001
            continue
    return make_failure(
        operation="market_price",
        exchange=name,
        account=account_name,
        code="INSTRUMENT_NOT_FOUND",
        message=f"Instrument not found: {requested_symbol}",
    )


def _current_position_size(
    request: Dict[str, Any],
    *,
    symbol: str,
    side: str,
) -> Decimal:
    """Signed-position size for (symbol, side) on Lighter. Returns 0 when
    flat or when the position is on the opposite side. Used by the
    market-order verifier to confirm the order filled."""
    expected_symbol = str(symbol or "").strip().upper()
    want_sign = 1 if str(side or "").strip().lower() == "buy" else -1
    fetched = _fetch_account_entry(request)
    target = fetched.get("target") if isinstance(fetched, dict) else None
    raw_positions = target.get("positions") if isinstance(target, dict) else None
    if isinstance(raw_positions, list):
        for item in raw_positions:
            if not isinstance(item, dict):
                continue
            if str(item.get("symbol") or "").strip().upper() != expected_symbol:
                continue
            size_value = _decimal_or_none(item.get("position")) or Decimal("0")
            try:
                sign_value = int(item.get("sign") or 0)
            except Exception:  # noqa: BLE001
                sign_value = 1 if size_value >= 0 else -1
            item_sign = (1 if sign_value >= 0 else -1)
            if item_sign == want_sign:
                return abs(size_value)
    return Decimal("0")


def _execute_position_state(request: Dict[str, Any]) -> CanonicalResponse:
    """Return the current signed position state for (account, symbol)."""
    account_name = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account_name)
    if not credentials:
        return make_failure(
            operation="position_state",
            exchange=name,
            account=account_name,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Lighter account configuration",
        )
    requested_symbol = str(request.get("symbol") or "").strip().upper()
    if not requested_symbol:
        return make_failure(
            operation="position_state",
            exchange=name,
            account=account_name,
            code="MISSING_SYMBOL",
            message="Symbol is required.",
        )
    market = _resolve_market(credentials["base_url"], requested_symbol)
    if market is None:
        return make_failure(
            operation="position_state",
            exchange=name,
            account=account_name,
            code="INSTRUMENT_NOT_FOUND",
            message=f"Instrument not found: {requested_symbol}",
        )
    canonical = str(market.get("symbol") or requested_symbol).strip().upper()
    canonical_mid = int(market.get("market_id") or 0)
    fetched = _fetch_account_entry(request)
    target = fetched.get("target")
    auth_token = str(fetched.get("auth_token") or "")
    active_orders = _fetch_active_orders(credentials, auth_token)
    sl = None
    tp = None
    alias_keys = set(_lighter_alias_keys(requested_symbol))
    alias_keys.add(canonical)
    for order in active_orders:
        if not isinstance(order, dict):
            continue
        if str(order.get("symbol") or "").strip().upper() not in alias_keys:
            continue
        trigger = _decimal_or_none(order.get("trigger_price"))
        if trigger is None:
            continue
        text = _decimal_text(trigger)
        label = str(order.get("type") or "").lower()
        if "stop" in label or "sl" in label:
            sl = text
        elif "take" in label or "tp" in label:
            tp = text
    if not isinstance(target, dict):
        return make_failure(
            operation="position_state",
            exchange=name,
            account=account_name,
            code="POSITIONS_UNAVAILABLE",
            message="Lighter account payload was not an object.",
        )
    positions_raw = target.get("positions")
    if positions_raw is None:
        positions_raw = []
    if not isinstance(positions_raw, list):
        return make_failure(
            operation="position_state",
            exchange=name,
            account=account_name,
            code="POSITIONS_UNAVAILABLE",
            message="Lighter positions payload was malformed.",
        )
    matched = None
    for item in positions_raw:
        if not isinstance(item, dict):
            continue
        item_sym = str(item.get("symbol") or "").strip().upper()
        try:
            item_mid = int(str(item.get("market_id") or 0) or 0)
        except Exception:
            item_mid = 0
        if item_sym != canonical and not (canonical_mid and item_mid == canonical_mid) and item_sym not in alias_keys:
            continue
        size = _decimal_or_none(item.get("position")) or Decimal("0")
        sign = int(item.get("sign") or 0)
        if sign == 0 and size != 0:
            sign = 1 if size > 0 else -1
        if sign == 0:
            continue
        entry_price = _decimal_or_none(item.get("entry_quote"))
        matched = {
            "symbol": canonical,
            "side": "long" if sign > 0 else "short",
            "size": str(abs(size)),
            "entry_price": _decimal_text(entry_price) if entry_price is not None else None,
            "sl": sl,
            "tp": tp,
        }
        break
    if matched is None:
        matched = {
            "symbol": canonical,
            "side": None,
            "size": "0",
            "entry_price": None,
            "sl": sl,
            "tp": tp,
        }
    # Convert the matched dict into a CanonicalPosition for the
    # canonical envelope (positions is a list of CanonicalPosition).
    position_row = CanonicalPosition(
        symbol=matched["symbol"],
        side=matched["side"] or "",
        size=matched["size"],
        entry_price=matched["entry_price"] or "0",
        pnl="0",  # PnL not exposed in this surface
        tp=matched.get("tp"),
        sl=matched.get("sl"),
    )
    return make_success(
        operation="position_state",
        exchange=name,
        account=account_name,
        positions=[position_row],
    )


def _resolve_lighter_market_by_id(
    base_url: str, market_id: int
) -> Optional[Dict[str, Any]]:
    """Resolve a market by integer market_id (used by get_order_state)."""
    if not market_id:
        return None
    try:
        sym_map = _fetch_market_symbol_map(base_url)
    except Exception:  # noqa: BLE001
        return None
    return sym_map.get(int(market_id))


def _normalize_order_record_by_client_id(
    order: Dict[str, Any],
    *,
    market: Dict[str, Any],
    size_decimals: int,
) -> Dict[str, Any]:
    """Normalize an active/inactive Lighter order record for the
    generic get_order_state_by_client_id operation.

    Returns a plain dict with the normalized fields; no venue mutation.
    """
    status_raw = str(order.get("status") or "").lower()
    taxonomy = str(order.get("taxonomy") or "").upper()
    if not taxonomy:
        if status_raw in ("filled", "canceled", "cancelled", "rejected", "expired"):
            taxonomy = {
                "filled": "FILLED",
                "canceled": "CANCELED",
                "cancelled": "CANCELED",
                "rejected": "REJECTED",
                "expired": "EXPIRED",
            }[status_raw]
        else:
            taxonomy = "ACTIVE"

    # Best-effort fields (Lighter shapes vary between active/inactive).
    filled_base = _decimal_or_none(
        order.get("filled_base_amount")
        or order.get("base_amount_filled")
        or order.get("filled_size")
    )
    initial_base = _decimal_or_none(
        order.get("initial_base_amount")
        or order.get("base_amount")
        or order.get("remaining_base_amount")
        or order.get("size")
    )
    filled_quote = _decimal_or_none(
        order.get("filled_quote_amount")
        or order.get("quote_amount_filled")
        or order.get("filled_quote")
    )

    # Actual fill price: prefer native field, then quote/base derivation.
    actual_fill_price: Optional[Decimal] = None
    afp = _decimal_or_none(
        order.get("avg_execution_price")
        or order.get("average_execution_price")
        or order.get("avg_fill_price")
    )
    if afp is not None and afp > 0:
        actual_fill_price = afp
    elif (
        filled_quote is not None
        and filled_base is not None
        and filled_base > 0
        and filled_quote > 0
    ):
        # Lighter quote amounts are in quote units; base in base units.
        # price = quote / base (both already unit-scaled by the API).
        actual_fill_price = filled_quote / filled_base

    # Side: Lighter encodes is_ask=true as SELL.
    is_ask_raw = order.get("is_ask")
    if is_ask_raw is not None:
        side = "sell" if bool(is_ask_raw) else "buy"
    else:
        side = str(order.get("side") or "").lower() or None

    return {
        "exchange_order_id": order.get("order_index") or order.get("order_id"),
        "client_order_index": order.get("client_order_index"),
        "market_index": order.get("market_index"),
        "symbol": str(market.get("symbol") or "").upper(),
        "side": side,
        "type": str(order.get("type") or order.get("order_type") or "").lower() or None,
        "status": status_raw,
        "taxonomy": taxonomy,
        "requested_size": _decimal_text(initial_base) if initial_base is not None else None,
        "filled_size": _decimal_text(filled_base) if filled_base is not None else None,
        "filled_quote": _decimal_text(filled_quote) if filled_quote is not None else None,
        "requested_price": _decimal_text(order.get("price")),
        "actual_fill_price": str(actual_fill_price) if actual_fill_price is not None else None,
        "reduce_only": bool(order.get("reduce_only") or False),
        "created_at": order.get("created_at"),
        "updated_at": order.get("updated_at"),
        "raw": order,
    }


def _find_order_by_client_order_index(
    credentials: Dict[str, Any],
    market: Dict[str, Any],
    client_order_index: int,
    auth_token: str,
) -> Optional[Dict[str, Any]]:
    """Search active orders, then inactive/history, for a Lighter order
    whose client_order_index matches the given deterministic identity.

    Returns the raw order dict, or None if not found. Read-only.
    """
    target = int(client_order_index)

    # 1) Active orders
    try:
        for order in _fetch_active_orders(credentials, auth_token):
            if not isinstance(order, dict):
                continue
            try:
                if int(order.get("client_order_index") or 0) == target:
                    return order
            except (TypeError, ValueError):
                continue
    except Exception:  # noqa: BLE001
        pass

    # 2) Inactive / history (fills land here). Bounded paging.
    limit = 50
    offset = 0
    max_pages = 20
    for _ in range(max_pages):
        try:
            batch = _fetch_lighter_inactive_orders_page(
                credentials, auth_token, limit=limit, offset=offset
            )
        except Exception:  # noqa: BLE001
            break
        if not batch:
            break
        for order in batch:
            if not isinstance(order, dict):
                continue
            try:
                if int(order.get("client_order_index") or 0) == target:
                    return order
            except (TypeError, ValueError):
                continue
        if len(batch) < limit:
            break
        offset += limit
    return None


def _fetch_lighter_inactive_orders_page(
    credentials: Dict[str, Any],
    auth_token: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Single page of inactive orders (read-only)."""
    limiter = _get_lighter_limiter(credentials)
    limiter.acquire()
    response = requests.get(
        f"{credentials['base_url']}/api/v1/accountInactiveOrders",
        params={
            "account_index": str(credentials["account_index"]),
            "limit": str(limit),
            "offset": str(offset),
        },
        headers={"Accept": "application/json", "authorization": str(auth_token or "")},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json() or {}
    batch = payload.get("orders") or []
    return batch if isinstance(batch, list) else []


def _execute_get_order_state_by_client_id(request: Dict[str, Any]) -> CanonicalResponse:
    """Generic lookup of an order by its deterministic client_order_index.

    Not GoldenFibo-specific. Searches active orders first, then inactive
    history, and returns the normalized record.
    """
    account_name = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account_name)
    if not credentials:
        return make_failure(operation="get_order_state_by_client_id", exchange=name, account=account_name, code="UNKNOWN_ACCOUNT", message="Unknown or invalid Lighter account configuration")

    requested_symbol = str(request.get("symbol") or "").strip().upper()
    if not requested_symbol:
        return make_failure(operation="get_order_state_by_client_id", exchange=name, account=account_name, code="INVALID_INPUTS", message="symbol required")

    client_raw = request.get("client_order_index")
    if client_raw is None:
        client_raw = request.get("client_order_id")
    if client_raw is None:
        return make_failure(operation="get_order_state_by_client_id", exchange=name, account=account_name, code="INVALID_INPUTS", message="client_order_index required")
    try:
        client_order_index = int(client_raw)
    except (TypeError, ValueError):
        return make_failure(operation="get_order_state_by_client_id", exchange=name, account=account_name, code="INVALID_INPUTS", message="client_order_index must be an integer")

    try:
        market = _resolve_market(credentials["base_url"], requested_symbol)
        size_decimals = int(market.get("size_decimals") or 0)
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="get_order_state_by_client_id", exchange=name, account=account_name, code="INSTRUMENT_RESOLUTION_FAILED", message=f"resolve_instrument({requested_symbol}) failed: {exc}")

    auth_token = _mint_auth_token_cached(credentials)
    order = _find_order_by_client_order_index(credentials, market, client_order_index, auth_token)
    if order is None:
        return make_failure(
            operation="get_order_state_by_client_id",
            exchange=name,
            account=account_name,
            code="ORDER_NOT_FOUND",
            message=f"no active/inactive order with client_order_index={client_order_index} on {requested_symbol}",
        )

    normalized = _normalize_order_record_by_client_id(
        order, market=market, size_decimals=size_decimals
    )
    return make_success(
        operation="get_order_state_by_client_id",
        exchange=name,
        account=account_name,
        order_state=normalized,
    )


def _execute_get_order_state(request: Dict[str, Any]) -> CanonicalResponse:
    """Return the full state for an exact order_index.

    Reads active + inactive orders, picks the matching record, and
    surfaces the actual fill price (computed from filled_quote /
    filled_base) when the order is FILLED. Classifies the status into
    the GoldenFibo taxonomy.
    """
    account_name = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account_name)
    if not credentials:
        return make_failure(
            operation="get_order_state",
            exchange=name,
            account=account_name,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Lighter account configuration",
        )
    order_index_raw = request.get("order_index")
    if order_index_raw is None:
        return make_failure(
            operation="get_order_state",
            exchange=name,
            account=account_name,
            code="MISSING_ORDER_ID",
            message="order_index is required.",
        )
    try:
        order_index = int(order_index_raw)
    except (TypeError, ValueError):
        return make_failure(
            operation="get_order_state",
            exchange=name,
            account=account_name,
            code="INVALID_ORDER_ID",
            message="order_index must be an integer.",
        )
    auth_token = _mint_auth_token_cached(credentials)
    matched = None
    try:
        for order in _fetch_active_orders(credentials, auth_token):
            if not isinstance(order, dict):
                continue
            try:
                if int(order.get("order_index") or order.get("order_id") or 0) == order_index:
                    matched = order
                    break
            except (TypeError, ValueError):
                continue
    except Exception:  # noqa: BLE001
        pass
    if matched is None:
        try:
            for order in _fetch_lighter_inactive_orders(credentials, auth_token):
                if not isinstance(order, dict):
                    continue
                try:
                    if int(order.get("order_index") or order.get("order_id") or 0) == order_index:
                        matched = order
                        break
                except (TypeError, ValueError):
                    continue
        except Exception:  # noqa: BLE001
            pass
    if matched is None:
        return make_failure(
            operation="get_order_state",
            exchange=name,
            account=account_name,
            code="ORDER_NOT_FOUND",
            message=f"order_index {order_index} not found.",
        )
    try:
        market_id = int(matched.get("market_index") or 0)
    except (TypeError, ValueError):
        market_id = 0
    symbol = str(matched.get("symbol") or "").strip().upper()
    size_decimals = 0
    price_decimals = 0
    if market_id and symbol:
        market = _resolve_lighter_market_by_id(credentials["base_url"], market_id)
        if market is not None:
            size_decimals = int(market.get("size_decimals") or 0)
            price_decimals = int(market.get("price_decimals") or 0)
    fill_price = _actual_fill_price(
        matched,
        size_decimals=size_decimals,
        price_decimals=price_decimals,
    )
    payload = {
        "order_index": order_index,
        "client_order_index": int(matched.get("client_order_index") or 0) or None,
        "symbol": symbol,
        "market_id": market_id,
        "side": "sell" if bool(matched.get("is_ask")) else "buy",
        "type": str(matched.get("type") or ""),
        "status": str(matched.get("status") or ""),
        "taxonomy": _classify_order_status(matched),
        "requested_price": _decimal_text(_decimal_or_none(matched.get("price")))
            if _decimal_or_none(matched.get("price")) is not None
            else None,
        "requested_size": _decimal_text(_decimal_or_none(matched.get("initial_base_amount")))
            if _decimal_or_none(matched.get("initial_base_amount")) is not None
            else None,
        "filled_size": _decimal_text(_decimal_or_none(matched.get("filled_base_amount")))
            if _decimal_or_none(matched.get("filled_base_amount")) is not None
            else None,
        "filled_quote": _decimal_text(_decimal_or_none(matched.get("filled_quote_amount")))
            if _decimal_or_none(matched.get("filled_quote_amount")) is not None
            else None,
        "actual_fill_price": _decimal_text(fill_price) if fill_price is not None else None,
        "reduce_only": bool(matched.get("reduce_only")),
        "created_at": int(matched.get("created_at") or 0),
        "updated_at": int(matched.get("updated_at") or 0),
    }
    return make_success(
        operation="get_order_state",
        exchange=name,
        account=account_name,
        order_state=payload,
    )


def _execute_cancel_order(request: Dict[str, Any]) -> CanonicalResponse:
    """Cancel exactly one order by ``order_index``.

    Used by GoldenFibo to cancel an orphan pending limit when a
    shared TP closes the position out from under it. Verifies by
    re-reading the active orders list.
    """
    account_name = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account_name)
    if not credentials:
        return make_failure(
            operation="cancel_order",
            exchange=name,
            account=account_name,
            code="UNKNOWN_ACCOUNT",
            message="Unknown or invalid Lighter account configuration",
        )
    order_index_raw = request.get("order_index")
    if order_index_raw is None:
        return make_failure(
            operation="cancel_order",
            exchange=name,
            account=account_name,
            code="MISSING_ORDER_ID",
            message="order_index is required.",
        )
    try:
        order_index = int(order_index_raw)
    except (TypeError, ValueError):
        return make_failure(
            operation="cancel_order",
            exchange=name,
            account=account_name,
            code="INVALID_ORDER_ID",
            message="order_index must be an integer.",
        )
    auth_token = _mint_auth_token_cached(credentials)
    try:
        active_orders = _fetch_active_orders(credentials, auth_token)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="cancel_order",
            exchange=name,
            account=account_name,
            code="OPEN_ORDERS_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )
    target = None
    for order in active_orders:
        if not isinstance(order, dict):
            continue
        try:
            if int(order.get("order_index") or order.get("order_id") or 0) == order_index:
                target = order
                break
        except (TypeError, ValueError):
            continue
    if target is None:
        return make_failure(
            operation="cancel_order",
            exchange=name,
            account=account_name,
            code="ORDER_NOT_FOUND",
            message=f"order_index {order_index} not found in active orders.",
        )
    # The SDK cancel_order requires market_index as well as order_index.
    # Derive it from the target order record (found above).
    try:
        market_index = int(target.get("market_index") or target.get("market_id") or 0)
    except (TypeError, ValueError):
        market_index = 0
    if market_index <= 0:
        return make_failure(
            operation="cancel_order",
            exchange=name,
            account=account_name,
            code="MARKET_INDEX_UNAVAILABLE",
            message=f"could not determine market_index for order_index {order_index}.",
        )
    try:
        async def _do_cancel() -> Any:
            # Build the signer INSIDE the coroutine so its aiohttp session
            # binds to a running event loop (matches _submit_new_order).
            signer = _build_signer_client(credentials)
            try:
                return await signer.cancel_order(market_index, order_index)
            finally:
                api_client = getattr(signer, "api_client", None)
                if api_client is not None and hasattr(api_client, "close"):
                    try:
                        await api_client.close()
                    except Exception:
                        pass

        def _submit() -> Any:
            return _run_lighter_coro_blocking(
                credentials,
                _do_cancel,
                thread_name=f"lighter-cancel-{order_index}",
                max_retries=0,
            )
        _submit()
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="cancel_order",
            exchange=name,
            account=account_name,
            code="CANCEL_ORDER_FAILED",
            message=sanitize_error_message(str(exc)),
        )
    try:
        post_active = _fetch_active_orders(credentials, auth_token)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="cancel_order",
            exchange=name,
            account=account_name,
            code="VERIFICATION_FAILED",
            message=f"Cancel verification could not read active orders: {exc}",
        )
    post_oids = set()
    for order in post_active:
        if not isinstance(order, dict):
            continue
        try:
            post_oids.add(int(order.get("order_index") or order.get("order_id") or 0))
        except (TypeError, ValueError):
            continue
    if order_index in post_oids:
        return make_failure(
            operation="cancel_order",
            exchange=name,
            account=account_name,
            code="VERIFICATION_FAILED",
            message=f"order_index {order_index} still present after cancel.",
        )
    return make_success(
        operation="cancel_order",
        exchange=name,
        account=account_name,
        order_state={
            "order_index": order_index,
            "client_order_index": target.get("client_order_index"),
            "status": "canceled",
            "taxonomy": "CANCELED",
            "verified": True,
        },
    )


def execute(request: Dict[str, Any]) -> CanonicalResponse:
    operation = str(request.get("operation") or "").strip()
    account = str(request.get("account") or "").strip()
    if not operation:
        return make_failure(
            operation="",
            exchange=name,
            account=account,
            code="INVALID_REQUEST",
            message="Missing operation.",
        )
    if operation not in {"balance", "positions_orders", "positions_management", "set_tp", "set_sl", "close_position", "new_order", "ladder", "cancel_order_group", "resolve_instrument", "market_price", "position_state", "get_order_state", "get_order_state_by_client_id", "market_constraints", "cancel_order"}:
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="NOT_IMPLEMENTED",
            message="This Lighter operation is not implemented yet.",
        )
    if not account:
        return make_failure(
            operation=operation,
            exchange=name,
            account="",
            code="MISSING_ACCOUNT",
            message="Missing account.",
        )
    try:
        if operation == "balance":
            return _balance(request)
        if operation == "positions_orders":
            return _positions_orders(request)
        if operation == "positions_management":
            return _positions_management(request)
        if operation == "set_tp":
            return _execute_set_tpsl(request, operation="set_tp")
        if operation == "set_sl":
            return _execute_set_tpsl(request, operation="set_sl")
        if operation == "close_position":
            return _execute_close_position(request)
        if operation == "new_order":
            return _execute_new_order(request)
        if operation == "cancel_order_group":
            return _execute_cancel_order_group(request)
        if operation == "resolve_instrument":
            return _execute_resolve_instrument(request)
        if operation == "market_price":
            return _execute_market_price(request)
        if operation == "position_state":
            return _execute_position_state(request)
        if operation == "get_order_state":
            return _execute_get_order_state(request)
        if operation == "get_order_state_by_client_id":
            return _execute_get_order_state_by_client_id(request)
        if operation == "market_constraints":
            return _execute_market_constraints(request)
        if operation == "cancel_order":
            return _execute_cancel_order(request)
        return _execute_ladder(request)
    except requests.HTTPError as exc:
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="LIGHTER_HTTP_ERROR",
            message=sanitize_error_message(f"Lighter HTTP error: {exc}"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lighter %s failed: %s", operation, exc)
        return make_failure(
            operation=operation,
            exchange=name,
            account=account,
            code="LIGHTER_ERROR",
            message=sanitize_error_message(str(exc) or "Lighter request failed."),
        )
