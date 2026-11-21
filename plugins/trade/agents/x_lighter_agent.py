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
from plugins.trade.candles import handle_candles_operation, has_native_candles

import asyncio
import contextlib
import importlib
import json
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
    CanonicalTickersBatch,
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
# REST sendTxBatch: CDN body limit ~15 (official signing docs).
LIGHTER_REST_SEND_TX_BATCH_SIZE = 15
# WS jsonapi/sendtxbatch protocol max 50; larger → backend 21514.
LIGHTER_WS_SEND_TX_BATCH_SIZE = 50
LIGHTER_WS_MAX_MESSAGE_BYTES = 64 * 1024
LIGHTER_WS_RECV_TIMEOUT_SECONDS = 20.0
LIGHTER_WS_OPEN_TIMEOUT_SECONDS = 10.0

# Cancellation batching (L2CancelOrder, tx_type=15). Each sendTxBatch
# carries up to ``LIGHTER_CANCEL_TX_BATCH_SIZE`` independently-signed
# cancel transactions in ONE HTTP request.
#
# The cancel path submits over REST ``sendTxBatch``. The official
# signing docs warn: "request-body size limits at the CDN edge can
# reject a large REST batch before it reaches the API (you'll get an
# HTTP error without Lighter's JSON body). REST batches of up to ~15
# orders are safe." The cancel path has NO WebSocket transport, so it
# must use the CDN-safe REST size — not the WS protocol max.
#
# (Phase 3 safety review: this was 30, above the documented CDN-safe
# REST size. The 200-order cancel regression test exercised the pacing
# math, not the wire format, so it did not catch it.)
LIGHTER_CANCEL_TX_BATCH_SIZE = LIGHTER_REST_SEND_TX_BATCH_SIZE  # 15
LIGHTER_CANCEL_TX_BATCH_PAUSE_SECONDS = 3.0

# ---------------------------------------------------------------------------
# Tier-aware transaction pacing (Phase 3).
#
# Lighter documents THREE independent limit families. They must not be
# collapsed into one number:
#
#   1. REST request weight (per rolling minute). Standard 60 requests/min;
#      Plus/Premium 24,000 weighted requests/min. sendTx/sendTxBatch are
#      EXCLUDED from this bucket on Plus/Premium and have their own.
#   2. sendTx/sendTxBatch request bucket (per rolling minute), keyed by
#      account tier AND staked tokens:
#        - Plus:          8000 sendTx/sendTxBatch per minute
#        - Premium:       5000 + stake tier, up to 24000 (Lighter chain);
#                         RH chain table differs
#      This bucket is measured in REQUESTS, not transactions.
#   3. Transaction-TYPE limit (default 40 requests/minute). Per the docs
#      this applies "to all account types, OTHERWISE sendTx limits apply",
#      so on Plus/Premium the sendTx bucket governs instead.
#
# On Plus/Premium there is a fourth, separate resource: the VOLUME QUOTA.
# Only these transaction types consume it:
#       L2CreateOrder (14), L2CancelAllOrders (16), L2ModifyOrder (17),
#       L2CreateGroupedOrders (28)
# and "if you include n transactions in a sendTxBatch request, n quota
# will be consumed". CRITICALLY: "Single-order cancels (L2CancelOrder,
# 15) do not consume quota and have no rate limit."
#
# Consequences for this agent:
#   * CreateOrder draws on BOTH the sendTx request bucket AND volume
#     quota (n txs consume n quota for n txs in one request).
#   * CancelOrder (L2CancelOrder) consumes NEITHER volume quota NOR a
#     quota-bearing rate limit. It is bounded by the sendTx request
#     bucket only. So the create path and the cancel path need
#     DIFFERENT policies — they must not share one counter.
#   * Volume quota is "shared across all sub-accounts under the same L1
#     address", so the quota ledger must be keyed by L1 address, not by
#     (chain, account_index), and must coordinate across processes.
#
# Everything below is CONSERVATIVE by construction: any uncertainty
# (unreadable tier, malformed quota payload, missing L1 address, stale
# ledger) falls back to the Standard 30-tx/60s behaviour that is
# already proven in production.
# ---------------------------------------------------------------------------

# Tier identifiers as reported by GET /api/v1/accountLimits → user_tier.
LIGHTER_TIER_STANDARD = "standard"
LIGHTER_TIER_PLUS = "plus"
LIGHTER_TIER_PREMIUM = "premium"
LIGHTER_TIER_UNKNOWN = "unknown"

# Documented sendTx/sendTxBatch REQUESTS per rolling minute.
# Premium is stake-dependent; we use the LOWEST documented tier
# (5000) as the working assumption and never assume the maximum.
LIGHTER_PREMIUM_SEND_TX_RPM_LOW = 5000
LIGHTER_PLUS_SEND_TX_RPM = 8000

# Documented volume-quota cap for Premium accounts is 15,000,000 with a
# 30,000,000 purchase cap. We never plan against the cap; we plan
# against the REMAINING value the exchange reports.
LIGHTER_VOLUME_QUOTA_FLOOR = 1

# Headroom applied to exchange-reported remaining quota. We keep a
# reserve so a concurrent writer (another process) cannot drive us to
# zero between our read and our submit.
LIGHTER_VOLUME_QUOTA_HEADROOM_RATIO = 0.10

# How long a tier/quota probe result is trusted before re-reading.
LIGHTER_TIER_CACHE_TTL_SECONDS = 300.0

# Per-account ceiling on how many transactions we will place in one
# rolling window even for Premium. This is the "conservative,
# configurable headroom rather than assuming unlimited throughput"
# requirement: it is a POLICY cap, not an exchange limit.
#
# Sizing rationale: at Premium the exchange-side limits that could bind
# are the sendTx request bucket (>= 5000 requests/min) and the volume
# quota (observed 1,589,890 remaining against a 15,000,000 cap). A
# 250-order ladder is 5 WS batches = 5 requests and 250 quota units —
# three orders of magnitude below both. So the ceiling exists to bound
# a runaway loop, not to mirror an exchange limit, and it is sized so
# that realistic ladder sizes (50/100/250) complete inside one window.
#
# Override with LIGHTER_PREMIUM_TX_PER_WINDOW / LIGHTER_PLUS_TX_PER_WINDOW.
def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return int(default)
    try:
        value = int(raw)
    except ValueError:
        return int(default)
    return value if value > 0 else int(default)


LIGHTER_PREMIUM_TX_PER_WINDOW = _env_int("LIGHTER_PREMIUM_TX_PER_WINDOW", 500)
LIGHTER_PLUS_TX_PER_WINDOW = _env_int("LIGHTER_PLUS_TX_PER_WINDOW", 250)

# Cross-process coordination.
# The volume quota is shared per L1 address across processes, so the
# quota ledger must live OUTSIDE the process. A file-backed ledger with
# an advisory lock is used; if the lock cannot be taken the caller
# falls back to the conservative Standard policy.
LIGHTER_QUOTA_LEDGER_DIR = Path(
    os.environ.get("LIGHTER_QUOTA_LEDGER_DIR", "/tmp/lighter-quota-ledger")
)
LIGHTER_QUOTA_LEDGER_TIMEOUT_SECONDS = 5.0

# Per-account nonce lease (Phase 3 safety review).
#
# Lighter nonces are tracked PER API KEY and must be strictly
# ``previous + 1``. Two clients on one account would hand out
# colliding nonces, and a discarded client leaves a hole that poisons
# every later transaction on that key. The lease guarantees only one
# nonce-allocating client is live per account + API key at a time.
#
# It is cross-process (an exclusive flock on a per-account lock file),
# mirroring the quota ledger, so two Hermes processes on one host
# cannot interleave nonce streams either.
LIGHTER_NONCE_LEASE_DIR = Path(
    os.environ.get("LIGHTER_NONCE_LEASE_DIR", "/tmp/lighter-nonce-lease")
)
LIGHTER_NONCE_LEASE_TIMEOUT_SECONDS = float(
    os.environ.get("LIGHTER_NONCE_LEASE_TIMEOUT_SECONDS", "30")
)

# Tier-aware pacing policy constants.
LIGHTER_STANDARD_TX_PER_WINDOW = LIGHTER_L2_TX_SAFE_LIMIT  # 30
LIGHTER_STANDARD_WINDOW_SECONDS = LIGHTER_L2_TX_WINDOW_SECONDS  # 60.0

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

    LEGACY (Phase 3): the agent's submit paths now use
    ``_LighterTierBudget``, whose limit comes from the account's tier
    policy. This class is retained because it still documents and
    exercises the fixed 30-tx/60s Standard rolling-window behaviour
    that the tier policy falls back to, and the existing regression
    tests assert against it directly. Do not wire new call sites to
    it — use ``_get_lighter_tier_budget``.

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


# ---------------------------------------------------------------------------
# Tier + quota probing
# ---------------------------------------------------------------------------


class _LighterTierPolicy:
    """Resolved pacing policy for one Lighter account.

    Attributes
    ----------
    tier:
        ``standard`` | ``plus`` | ``premium`` | ``unknown``.
    tx_per_window:
        How many quota-bearing transactions (CreateOrder) we allow in one
        rolling window. Standard keeps the proven 30/60s behaviour.
    window_seconds:
        Length of that rolling window.
    send_tx_rpm:
        The sendTx/sendTxBatch REQUEST bucket (requests per minute).
        Requests and transactions are counted separately — a batch of
        50 txs is ONE request but FIFTY transactions.
    quota_remaining:
        Exchange-reported remaining volume quota, or ``None`` when the
        account is not quota-bearing or the payload was unreadable.
    quota_bearing:
        True only when the account is Plus/Premium AND the exchange
        reported a usable remaining-quota value.
    l1_address:
        The L1 address the quota is shared across, when known.
    reason:
        Human-readable justification, logged for auditability.
    """

    __slots__ = (
        "tier",
        "tx_per_window",
        "window_seconds",
        "send_tx_rpm",
        "quota_remaining",
        "quota_bearing",
        "l1_address",
        "reason",
    )

    def __init__(
        self,
        *,
        tier: str,
        tx_per_window: int,
        window_seconds: float,
        send_tx_rpm: int,
        quota_remaining: Optional[int],
        quota_bearing: bool,
        l1_address: Optional[str],
        reason: str,
    ) -> None:
        self.tier = tier
        self.tx_per_window = int(tx_per_window)
        self.window_seconds = float(window_seconds)
        self.send_tx_rpm = int(send_tx_rpm)
        self.quota_remaining = quota_remaining
        self.quota_bearing = bool(quota_bearing)
        self.l1_address = l1_address
        self.reason = reason

    @property
    def is_conservative(self) -> bool:
        """True when the policy must not exceed the Standard behaviour."""
        return self.tier in (LIGHTER_TIER_STANDARD, LIGHTER_TIER_UNKNOWN)

    def describe(self) -> Dict[str, Any]:
        return {
            "tier": self.tier,
            "tx_per_window": self.tx_per_window,
            "window_seconds": self.window_seconds,
            "send_tx_rpm": self.send_tx_rpm,
            "quota_remaining": self.quota_remaining,
            "quota_bearing": self.quota_bearing,
            "reason": self.reason,
        }


def _standard_lighter_policy(reason: str) -> _LighterTierPolicy:
    """The conservative fallback: exactly today's proven behaviour."""
    return _LighterTierPolicy(
        tier=LIGHTER_TIER_UNKNOWN,
        tx_per_window=LIGHTER_STANDARD_TX_PER_WINDOW,
        window_seconds=LIGHTER_STANDARD_WINDOW_SECONDS,
        send_tx_rpm=LIGHTER_RATELIMIT_MAX_REQUESTS,
        quota_remaining=None,
        quota_bearing=False,
        l1_address=None,
        reason=reason,
    )


def _normalize_lighter_tier(raw: Any) -> str:
    """Map an ``accountLimits`` tier string to a known tier.

    Unrecognised values map to ``unknown`` so the caller falls back to
    the conservative policy. Never guess upwards.
    """
    text = str(raw or "").strip().lower()
    if not text:
        return LIGHTER_TIER_UNKNOWN
    if "premium" in text or "advanced" in text:
        return LIGHTER_TIER_PREMIUM
    if "plus" in text:
        return LIGHTER_TIER_PLUS
    if "standard" in text or "basic" in text:
        return LIGHTER_TIER_STANDARD
    return LIGHTER_TIER_UNKNOWN


def _parse_quota_remaining(raw: Any) -> Optional[int]:
    """Parse an exchange-reported remaining volume quota.

    Returns ``None`` for any value that is not a usable non-negative
    integer. A malformed quota payload must NEVER be interpreted as
    "plenty of quota" — it degrades to the conservative policy.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, float):
        # A non-integral float is a malformed payload, not a rounding
        # opportunity.
        return int(raw) if float(raw).is_integer() and raw >= 0 else None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            value = int(text)
        except ValueError:
            return None
        return value if value >= 0 else None
    return None


def _build_tier_policy(
    *,
    tier: str,
    quota_remaining: Optional[int],
    quota_cap: Optional[int],
    l1_address: Optional[str],
    send_tx_rpm_hint: Optional[int] = None,
) -> _LighterTierPolicy:
    """Build the pacing policy for a resolved tier.

    Conservative by construction:
      * ``unknown`` tier → the proven Standard 30/60s behaviour.
      * Plus/Premium without a usable quota value → still capped by the
        per-window policy ceiling, but never above the Standard window.
      * A quota value at or below the floor is treated as EXHAUSTED:
        the policy allows no quota-bearing transactions.
    """
    normalized = _normalize_lighter_tier(tier)

    if normalized == LIGHTER_TIER_UNKNOWN:
        return _standard_lighter_policy(
            "tier not recognised; using conservative standard pacing"
        )

    if normalized == LIGHTER_TIER_STANDARD:
        return _LighterTierPolicy(
            tier=LIGHTER_TIER_STANDARD,
            tx_per_window=LIGHTER_STANDARD_TX_PER_WINDOW,
            window_seconds=LIGHTER_STANDARD_WINDOW_SECONDS,
            send_tx_rpm=LIGHTER_RATELIMIT_MAX_REQUESTS,
            quota_remaining=quota_remaining,
            quota_bearing=False,
            l1_address=l1_address,
            reason="standard tier; documented 40/min tx-type limit applies",
        )

    # Plus / Premium.
    ceiling = (
        LIGHTER_PREMIUM_TX_PER_WINDOW
        if normalized == LIGHTER_TIER_PREMIUM
        else LIGHTER_PLUS_TX_PER_WINDOW
    )
    rpm = int(send_tx_rpm_hint or 0) or (
        LIGHTER_PREMIUM_SEND_TX_RPM_LOW
        if normalized == LIGHTER_TIER_PREMIUM
        else LIGHTER_PLUS_SEND_TX_RPM
    )

    usable_quota = quota_remaining is not None
    if usable_quota and quota_remaining <= LIGHTER_VOLUME_QUOTA_FLOOR:
        # Quota exhausted (or effectively so). Allow nothing.
        return _LighterTierPolicy(
            tier=normalized,
            tx_per_window=0,
            window_seconds=LIGHTER_STANDARD_WINDOW_SECONDS,
            send_tx_rpm=rpm,
            quota_remaining=quota_remaining,
            quota_bearing=True,
            l1_address=l1_address,
            reason="volume quota exhausted; no quota-bearing transactions allowed",
        )

    # Apply headroom to the reported remaining quota so a concurrent
    # writer cannot strand us. The window cap stays the binding limit
    # in normal operation — quota is a hard ceiling, not a pacing knob.
    if usable_quota:
        reserve = int(quota_remaining * LIGHTER_VOLUME_QUOTA_HEADROOM_RATIO)
        budget = max(0, int(quota_remaining) - reserve)
    else:
        budget = ceiling

    tx_per_window = max(0, min(ceiling, budget))

    reason = (
        f"{normalized} tier; sendTx bucket {rpm} req/min; "
        f"quota_remaining={quota_remaining}; "
        f"window cap {ceiling} txs, headroom-adjusted {tx_per_window}"
    )
    return _LighterTierPolicy(
        tier=normalized,
        tx_per_window=tx_per_window,
        window_seconds=LIGHTER_STANDARD_WINDOW_SECONDS,
        send_tx_rpm=rpm,
        quota_remaining=quota_remaining,
        quota_bearing=usable_quota,
        l1_address=l1_address,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Live tier/quota probe
# ---------------------------------------------------------------------------

_LIGHTER_TIER_CACHE: Dict[Tuple[str, int], Tuple[float, _LighterTierPolicy]] = {}
_LIGHTER_TIER_CACHE_LOCK = threading.Lock()


def _fetch_lighter_account_limits(
    credentials: Dict[str, Any],
    auth_token: str,
) -> Optional[Dict[str, Any]]:
    """GET ``/api/v1/accountLimits`` and return the parsed JSON body.

    Returns ``None`` on any transport/parse failure. The caller treats
    ``None`` as "tier unknown" and falls back to conservative pacing.
    """
    try:
        response = requests.get(
            f"{credentials['base_url']}/api/v1/accountLimits",
            params={"account_index": str(credentials["account_index"])},
            headers={"Accept": "application/json", "authorization": auth_token},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[LIGHTER-TIER] accountLimits probe failed: %s",
            sanitize_error_message(str(exc))[:160],
        )
        return None
    if not isinstance(payload, dict):
        return None
    # A non-200 envelope means the probe itself failed.
    code = payload.get("code")
    if code is not None and int(code) != 200:
        logger.warning(
            "[LIGHTER-TIER] accountLimits returned code=%s", sanitize_error_message(str(code))[:80]
        )
        return None
    return payload


def _fetch_lighter_l1_address(
    credentials: Dict[str, Any],
    auth_token: str,
) -> Optional[str]:
    """Read the account's L1 address, which the volume quota is shared on.

    The L1 address is NOT in the credential model, so it must be read
    from ``/api/v1/account``. Returns a normalized lowercase hex string
    or ``None`` when unavailable.
    """
    try:
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
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[LIGHTER-TIER] account probe failed: %s",
            sanitize_error_message(str(exc))[:160],
        )
        return None
    accounts = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(accounts, list) or not accounts:
        return None
    first = accounts[0]
    if not isinstance(first, dict):
        return None
    raw = first.get("l1_address")
    text = str(raw or "").strip().lower()
    if not text.startswith("0x") or len(text) < 10:
        return None
    return text


def _probe_lighter_tier_policy(
    credentials: Dict[str, Any],
    *,
    use_cache: bool = True,
) -> _LighterTierPolicy:
    """Resolve the account's tier and build its pacing policy.

    Reads two documented endpoints:
      * ``/api/v1/accountLimits`` → ``user_tier``, ``volume_quota_remaining``
      * ``/api/v1/account``       → ``l1_address`` (quota sharing key)

    Any failure degrades to the conservative Standard policy. Results
    are cached for ``LIGHTER_TIER_CACHE_TTL_SECONDS`` so a ladder does
    not re-probe per batch.
    """
    chain = str(credentials.get("chain") or "").strip().upper()
    account_index = int(credentials.get("account_index") or 0)
    cache_key = (chain, account_index)

    if use_cache:
        with _LIGHTER_TIER_CACHE_LOCK:
            cached = _LIGHTER_TIER_CACHE.get(cache_key)
            if cached is not None:
                expires_at, policy = cached
                if time.monotonic() < expires_at:
                    return policy

    try:
        auth_token = _mint_auth_token_cached(credentials)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[LIGHTER-TIER] auth token unavailable: %s",
            sanitize_error_message(str(exc))[:160],
        )
        return _cache_tier_policy(
            cache_key,
            _standard_lighter_policy("auth token unavailable; conservative pacing"),
        )

    limits = _fetch_lighter_account_limits(credentials, auth_token)
    if not limits:
        return _cache_tier_policy(
            cache_key,
            _standard_lighter_policy(
                "accountLimits unavailable; conservative pacing"
            ),
        )

    tier_raw = limits.get("user_tier") or limits.get("user_tier_name")
    quota_remaining = _parse_quota_remaining(limits.get("volume_quota_remaining"))
    quota_cap = _parse_quota_remaining(limits.get("volume_quota_cap"))

    l1_address = _fetch_lighter_l1_address(credentials, auth_token)

    policy = _build_tier_policy(
        tier=str(tier_raw or ""),
        quota_remaining=quota_remaining,
        quota_cap=quota_cap,
        l1_address=l1_address,
    )
    logger.warning("[LIGHTER-TIER] %s", policy.reason)

    return _cache_tier_policy(cache_key, policy)


def _cache_tier_policy(
    cache_key: Tuple[str, int],
    policy: _LighterTierPolicy,
) -> _LighterTierPolicy:
    """Store the policy in the TTL cache and return it.

    Every probe outcome — including the conservative fallbacks — is
    cached. Otherwise a failing probe would re-hit the network on every
    batch, consuming rate-limit slots for no benefit.
    """
    with _LIGHTER_TIER_CACHE_LOCK:
        _LIGHTER_TIER_CACHE[cache_key] = (
            time.monotonic() + LIGHTER_TIER_CACHE_TTL_SECONDS,
            policy,
        )
    return policy


def _get_lighter_tier_policy(
    credentials: Dict[str, Any],
    *,
    use_cache: bool = True,
) -> _LighterTierPolicy:
    """Return the cached tier policy, probing only when needed."""
    chain = str(credentials.get("chain") or "").strip().upper()
    account_index = int(credentials.get("account_index") or 0)
    cache_key = (chain, account_index)
    if use_cache:
        with _LIGHTER_TIER_CACHE_LOCK:
            cached = _LIGHTER_TIER_CACHE.get(cache_key)
            if cached is not None:
                expires_at, policy = cached
                if time.monotonic() < expires_at:
                    return policy
    return _probe_lighter_tier_policy(credentials, use_cache=False)


# ---------------------------------------------------------------------------
# Cross-process volume-quota ledger
# ---------------------------------------------------------------------------


class _LighterQuotaLedgerUnavailable(RuntimeError):
    """The cross-process quota ledger could not be locked."""


class _LighterQuotaLedger:
    """File-backed ledger of volume quota consumed per L1 address.

    The volume quota is "shared across all sub-accounts under the same
    L1 address", so the ledger is keyed by L1 address and lives on disk
    so that SEPARATE Hermes processes share it. Each record is a list
    of ``(timestamp, amount)`` entries within the rolling window.

    The lock is an exclusive ``flock`` on a per-L1 lock file. If the
    lock cannot be acquired within the timeout, the caller MUST fall
    back to conservative pacing rather than proceed unserialised.
    """

    def __init__(self, l1_address: str, *, window_seconds: float = 60.0) -> None:
        self.l1_address = str(l1_address or "").strip().lower()
        self.window_seconds = float(window_seconds)
        safe = re.sub(r"[^0-9a-z]", "_", self.l1_address) or "unknown"
        self.directory = Path(LIGHTER_QUOTA_LEDGER_DIR)
        self.path = self.directory / f"{safe}.json"
        self.lock_path = self.directory / f"{safe}.lock"

    def _prune(self, records: List[List[Any]], now: float) -> List[List[Any]]:
        cutoff = now - self.window_seconds
        return [r for r in records if isinstance(r, list) and len(r) == 2 and float(r[0]) > cutoff]

    def _read(self) -> List[List[Any]]:
        try:
            raw = self.path.read_text()
        except (FileNotFoundError, OSError):
            return []
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            # A corrupt ledger is NOT "no usage". Treat it as unknown
            # so the caller can fail closed.
            raise _LighterQuotaLedgerUnavailable("quota ledger is unreadable")
        if not isinstance(data, list):
            raise _LighterQuotaLedgerUnavailable("quota ledger has an unexpected shape")
        return data

    def _write(self, records: List[List[Any]]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(json.dumps(records))
        os.replace(tmp, self.path)

    def reserve(
        self,
        amount: int,
        *,
        remaining_hint: Optional[int] = None,
        ceiling: Optional[int] = None,
    ) -> int:
        """Reserve ``amount`` quota units, returning the amount reserved.

        ``remaining_hint`` is the exchange-reported remaining quota. It
        is authoritative but may be STALE (read up to the tier-cache TTL
        ago), so it is treated as an upper bound on what is left.

        ``ceiling`` is the hard cap this account may consume in the
        window. When both the ledger's own accumulated usage and the
        hint are known, the reservation is refused if it would exceed
        either — that is what stops two processes on one host from
        jointly oversubscribing the shared per-L1 quota.

        Raises ``_LighterQuotaLedgerUnavailable`` when the ledger cannot
        be locked or read, OR when the reservation would exceed the
        available quota. The caller must then fall back to conservative
        pacing rather than proceed without coordination.
        """
        amount = int(amount)
        if amount <= 0:
            return 0
        self.directory.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            deadline = time.monotonic() + LIGHTER_QUOTA_LEDGER_TIMEOUT_SECONDS
            locked = False
            while time.monotonic() < deadline:
                try:
                    import fcntl

                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except (BlockingIOError, OSError):
                    time.sleep(0.02)
            if not locked:
                raise _LighterQuotaLedgerUnavailable(
                    "could not lock the cross-process quota ledger"
                )
            now = time.monotonic()
            records = self._prune(self._read(), now)
            used = sum(int(r[1]) for r in records if isinstance(r[1], (int, float)))
            if remaining_hint is not None:
                # The exchange-reported remaining is authoritative. If a
                # concurrent process consumed quota we cannot see, the
                # hint reflects it — refuse when the hint is exhausted.
                if int(remaining_hint) <= LIGHTER_VOLUME_QUOTA_FLOOR:
                    raise _LighterQuotaLedgerUnavailable(
                        "exchange reports volume quota exhausted"
                    )
            if ceiling is not None:
                # Enforce the ceiling against BOTH the ledger's own
                # accumulated usage and the exchange hint, whichever is
                # the tighter constraint.
                available = int(ceiling) - used
                if remaining_hint is not None:
                    available = min(available, int(remaining_hint))
                if amount > available:
                    raise _LighterQuotaLedgerUnavailable(
                        f"quota reservation of {amount} exceeds available {available} "
                        f"(used={used}, ceiling={ceiling}, hint={remaining_hint})"
                    )
            records.append([now, amount])
            self._write(records)
            return amount
        finally:
            try:
                import fcntl

                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001
                pass
            os.close(lock_fd)

    def release(self, amount: int) -> None:
        """Return ``amount`` quota units after a pre-apply rejection."""
        amount = int(amount)
        if amount <= 0:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            deadline = time.monotonic() + LIGHTER_QUOTA_LEDGER_TIMEOUT_SECONDS
            locked = False
            while time.monotonic() < deadline:
                try:
                    import fcntl

                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except (BlockingIOError, OSError):
                    time.sleep(0.02)
            if not locked:
                return
            now = time.monotonic()
            records = self._prune(self._read(), now)
            # Each record holds an AMOUNT, not a count of one. Release
            # must subtract quota units, not drop records.
            outstanding = sum(
                int(r[1]) for r in records if isinstance(r[1], (int, float))
            )
            to_release = min(int(amount), outstanding)
            remaining = outstanding - to_release
            # Rebuild the record list preserving the surviving usage.
            rebuilt: List[List[Any]] = []
            left = remaining
            for record in records:
                if left <= 0:
                    break
                value = int(record[1]) if isinstance(record[1], (int, float)) else 0
                if value <= left:
                    rebuilt.append(record)
                    left -= value
                else:
                    rebuilt.append([record[0], left])
                    left = 0
            self._write(rebuilt)
        except _LighterQuotaLedgerUnavailable:
            return
        finally:
            try:
                import fcntl

                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001
                pass
            os.close(lock_fd)

    def used_in_window(self) -> int:
        """Total quota units recorded inside the rolling window."""
        try:
            records = self._prune(self._read(), time.monotonic())
        except _LighterQuotaLedgerUnavailable:
            return 0
        return sum(int(r[1]) for r in records if isinstance(r[1], (int, float)))


# ---------------------------------------------------------------------------
# Tier-aware budgets
# ---------------------------------------------------------------------------


class _LighterTierBudget:
    """Rolling-window budget whose limit comes from the tier policy.

    Two INDEPENDENT budgets exist per account, because the exchange
    charges them differently:

    ``create``
        Counts L2CreateOrder transactions. Bounded by the transaction
        window cap AND (on Plus/Premium) by the shared volume quota.

    ``cancel``
        Counts L2CancelOrder transactions. Per the official docs
        "Single-order cancels (L2CancelOrder, 15) do not consume quota
        and have no rate limit" — so cancels are bounded only by the
        sendTx REQUEST bucket, not by volume quota and not by the
        40/min transaction-type limit.

    Keeping them separate is a correctness requirement, not an
    optimisation: sharing one counter would make cancels wait behind
    creates (and, worse, would let creates consume the quota that the
    docs say they do not).
    """

    __slots__ = ("safe_limit", "window_seconds", "_hits", "_lock", "kind")

    def __init__(self, safe_limit: int, window_seconds: float, *, kind: str = "create") -> None:
        self.safe_limit = int(safe_limit)
        self.window_seconds = float(window_seconds)
        self.kind = kind
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
        """Block until ``n`` slots are free, then reserve them.

        Returns the seconds waited. A ``safe_limit`` of 0 means the
        policy forbids these transactions entirely — the caller must
        check ``can_acquire`` first and abort rather than wait forever.
        """
        n = int(n)
        if n <= 0:
            return 0.0
        if self.safe_limit <= 0:
            raise _LighterBudgetExhausted(
                "tier policy forbids this transaction type (quota exhausted)"
            )
        if n > self.safe_limit:
            raise _LighterBudgetExhausted(
                f"batch of {n} exceeds the per-window limit {self.safe_limit}"
            )
        waited = 0.0
        while True:
            with self._lock:
                self._evict_locked()
                if len(self._hits) + n <= self.safe_limit:
                    now = time.monotonic()
                    for _ in range(n):
                        self._hits.append(now)
                    return waited
                sleep_for = self.window_seconds - (time.monotonic() - self._hits[0])
            sleep_for = max(sleep_for, 0.0) + 0.01
            time.sleep(sleep_for)
            waited += sleep_for

    def rollback(self, n: int) -> None:
        """Reverse the most-recent N reservations (pre-apply rejection)."""
        with self._lock:
            rollback_n = min(int(n), len(self._hits))
            for _ in range(rollback_n):
                self._hits.pop()


class _LighterBudgetExhausted(RuntimeError):
    """The tier policy does not permit the requested transactions."""


_LIGHTER_TIER_BUDGETS: Dict[Tuple[str, int, str], _LighterTierBudget] = {}
_LIGHTER_TIER_BUDGETS_LOCK = threading.Lock()


def _get_lighter_tier_budget(
    credentials: Dict[str, Any],
    policy: _LighterTierPolicy,
    *,
    kind: str = "create",
) -> _LighterTierBudget:
    """Return the tier-aware budget for one account and transaction kind.

    The limit is refreshed from the policy each time the budget is
    (re)built, and the existing tracker is reused so the rolling window
    survives a policy refresh. Reusing the tracker is what prevents a
    policy change from resetting the window and allowing a burst.
    """
    chain = str(credentials.get("chain") or "").strip().upper()
    account_index = int(credentials.get("account_index") or 0)
    key = (chain, account_index, kind)

    if kind == "cancel":
        # Cancels have no volume-quota cost and no 40/min type limit.
        # They are bounded by the sendTx REQUEST bucket. We still apply
        # a bounded per-window cap so a runaway loop cannot spin, but it
        # is far higher than the create cap.
        #
        # For a Standard/unknown account the sendTx bucket is the
        # shared 40/min request budget, so the cancel cap stays at that
        # documented figure — cancels are NOT given a free pass when the
        # tier is unknown.
        rpm = int(policy.send_tx_rpm or 0)
        if rpm <= 0:
            rpm = LIGHTER_RATELIMIT_MAX_REQUESTS
        limit = max(1, min(rpm, LIGHTER_PREMIUM_TX_PER_WINDOW * 2))
        window = LIGHTER_STANDARD_WINDOW_SECONDS
    else:
        limit = max(0, int(policy.tx_per_window))
        window = float(policy.window_seconds)

    with _LIGHTER_TIER_BUDGETS_LOCK:
        budget = _LIGHTER_TIER_BUDGETS.get(key)
        if budget is None:
            budget = _LighterTierBudget(limit, window, kind=kind)
            _LIGHTER_TIER_BUDGETS[key] = budget
        else:
            budget.safe_limit = limit
            budget.window_seconds = window
        return budget


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


class _LighterWsUnavailable(RuntimeError):
    """WebSocket connect/setup failed before any sendTxBatch write."""


class _LighterWsSession:
    """A WebSocket session bound to ONE dedicated event loop.

    ``websockets`` connections are bound to the loop that created them.
    Opening the session inside one ``asyncio.run()`` and then using it
    from a second ``asyncio.run()`` raises
    ``RuntimeError: ... got Future ... attached to a different loop`` —
    and ``asyncio.run()`` closes its loop on exit, so the original loop
    is gone entirely.

    This wrapper owns a long-lived loop in a dedicated thread and
    executes every WS coroutine on THAT loop. The ladder opens the
    session once and reuses it for every batch, so all sends share one
    loop, one connection, and one keepalive.
    """

    def __init__(self, session: Any, loop: Any) -> None:
        self._session = session
        self._loop = loop

    @property
    def session(self) -> Any:
        return self._session

    @staticmethod
    def _loop_forever(loop: Any) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            try:
                for task in asyncio.all_tasks(loop):
                    task.cancel()
            except Exception:  # noqa: BLE001
                pass
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _submit_to_loop(loop: Any, coro: Any, timeout: Optional[float]) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except BaseException:
            future.cancel()
            raise

    @classmethod
    def open(cls, credentials: Dict[str, Any]) -> "_LighterWsSession":
        """Connect on a dedicated loop and return the bound session."""
        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=cls._loop_forever,
            args=(loop,),
            name="lighter-ws-loop",
            daemon=True,
        )
        thread.start()
        try:
            raw = cls._submit_to_loop(
                loop,
                _open_lighter_ladder_ws(credentials),
                LIGHTER_WS_OPEN_TIMEOUT_SECONDS + 15.0,
            )
        except BaseException:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:  # noqa: BLE001
                pass
            raise
        return cls(raw, loop)

    def _await_on_owner(self, coro: Any, timeout: Optional[float]) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        wrapped = asyncio.wrap_future(future)

        async def _wait() -> Any:
            try:
                if timeout is None:
                    return await wrapped
                return await asyncio.wait_for(wrapped, timeout=timeout)
            except BaseException:
                future.cancel()
                raise

        return _wait()

    async def send(self, payload: str) -> None:
        await self._await_on_owner(self._session.send(payload), None)

    async def recv(self, timeout: Optional[float] = None) -> Any:
        return await self._await_on_owner(self._session.recv(), timeout)

    async def close(self) -> None:
        try:
            await self._await_on_owner(self._session.close(), 5.0)
        finally:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:  # noqa: BLE001
                pass


def _lighter_ladder_chunk_size(transport: str, budget: Any = None) -> int:
    """Create-order chunk size for one sendTxBatch / jsonapi/sendtxbatch.

    WS protocol max is 50. REST CDN-safe max is 15. Both are capped by
    the account's tier-aware tx budget so ``wait_for_capacity(n)``
    cannot deadlock when ``n > safe_limit``.

    For a Standard account the cap is the proven 30/60s limit, so the
    chunk size is unchanged from before Phase 3. For Plus/Premium the
    cap is the tier policy's per-window transaction ceiling, which is
    well above the protocol max — so the protocol max (50 WS / 15 REST)
    remains the binding constraint.
    """
    mode = str(transport or "rest").strip().lower()
    proto = LIGHTER_WS_SEND_TX_BATCH_SIZE if mode == "ws" else LIGHTER_REST_SEND_TX_BATCH_SIZE
    cap = int(getattr(budget, "safe_limit", 0) or LIGHTER_L2_TX_SAFE_LIMIT)
    return max(1, min(int(proto), cap))


def _lighter_open_order_caps(*, chain: str, user_tier: str) -> Dict[str, int]:
    """Documented active-order caps. Not inferred from live 429s."""
    tier = str(user_tier or "").strip().lower()
    chain_u = str(chain or "").strip().upper()
    if chain_u == "ROBINHOOD":
        if tier == "premium":
            return {"per_account": 1000, "per_market": 1000, "pending_per_account": 2000, "pending_per_market": 160}
        return {"per_account": 1000, "per_market": 1000, "pending_per_account": 200, "pending_per_market": 16}
    # Lighter (Arbitrum)
    if tier == "premium":
        return {"per_account": 1500, "per_market": 1000, "pending_per_account": 1000, "pending_per_market": 100}
    if tier == "plus":
        return {"per_account": 750, "per_market": 250, "pending_per_account": 500, "pending_per_market": 100}
    return {"per_account": 250, "per_market": 30, "pending_per_account": 50, "pending_per_market": 10}


def _lighter_capacity_allows(*, open_on_market: int, adding: int, caps: Dict[str, int]) -> bool:
    return int(open_on_market) + int(adding) <= int(caps.get("per_market") or 0)


def _lighter_ws_url(credentials: Dict[str, Any]) -> str:
    host = str(credentials.get("base_url") or "").replace("https://", "").replace("http://", "").rstrip("/")
    return f"wss://{host}/stream"


def _ladder_cid() -> str:
    return format(time.time_ns() & 0xFFFFFFFF, "08x")


def _ladder_log(cid: str, stage: str, *, dt_ms: float, total_ms: float, **fields: Any) -> None:
    extra = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    logger.warning(
        "[LIGHTER-LADDER] cid=%s stage=%s dt_ms=%.1f total_ms=%.1f %s",
        cid, stage, dt_ms, total_ms, extra,
    )


async def _open_lighter_ladder_ws(credentials: Dict[str, Any]) -> Any:
    """Open a stream WS for jsonapi/sendtxbatch. No market subscriptions."""
    url = _lighter_ws_url(credentials)
    try:
        import websockets
    except Exception as exc:  # noqa: BLE001
        raise _LighterWsUnavailable(f"websockets import failed: {exc}") from exc
    try:
        ws = await websockets.connect(
            url,
            open_timeout=LIGHTER_WS_OPEN_TIMEOUT_SECONDS,
            max_size=LIGHTER_WS_MAX_MESSAGE_BYTES,
        )
    except Exception as exc:  # noqa: BLE001
        raise _LighterWsUnavailable(sanitize_error_message(str(exc))) from exc
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        msg = json.loads(raw) if raw and raw[:1] in "{[" else {}
        if isinstance(msg, dict) and msg.get("type") == "ping":
            await ws.send(json.dumps({"type": "pong"}))
    except Exception:  # noqa: BLE001
        # Connected-frame is optional; proceed to sendtxbatch.
        pass
    return ws


def _parse_ws_sendtxbatch_response(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {"code": None, "message": raw[:300], "tx_hash": []}
    elif isinstance(raw, dict):
        payload = raw
    else:
        return {"code": None, "message": "unrecognized ws payload", "tx_hash": []}
    if not isinstance(payload, dict):
        return {"code": None, "message": "unrecognized ws payload", "tx_hash": []}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    code = data.get("code", payload.get("code"))
    try:
        code_int = int(code) if code is not None else None
    except (TypeError, ValueError):
        code_int = None
    tx_hash = data.get("tx_hash") or data.get("tx_hashes") or payload.get("tx_hash") or []
    if not isinstance(tx_hash, list):
        tx_hash = []
    message = str(data.get("message") or payload.get("message") or "")
    return {"code": code_int, "message": message, "tx_hash": tx_hash, "raw": payload}


class _WsBatchResponse:
    def __init__(self, parsed: Dict[str, Any]) -> None:
        self.code = parsed.get("code")
        self.message = parsed.get("message") or ""
        self.tx_hash = list(parsed.get("tx_hash") or [])
        self.additional_properties: Dict[str, Any] = {}


def _build_ws_sendtxbatch_payload(*, req_id: str, tx_types: List[int], tx_infos: List[str]) -> str:
    return json.dumps(
        {
            "type": "jsonapi/sendtxbatch",
            "data": {
                "id": req_id,
                "tx_types": json.dumps(tx_types),
                "tx_infos": json.dumps(tx_infos),
            },
        },
        separators=(",", ":"),
    )


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
    return [
        "balance", "positions_orders", "positions_management",
        "new_order", "ladder", "cancel_orders",
        "candles",
        # Public instrument resolve + Phase 2.4 catalog/price readers.
        "resolve_instrument", "list_instruments", "market_price",
        "get_tickers",
        # Position management actions
        "set_tp", "set_sl", "close_position",
    ]


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

# Common human aliases → Lighter venue symbols (catalog is the source
# of truth; these only expand match keys). GOLD is not listed on
# Lighter; the gold perp is ``XAU``.
_LIGHTER_SYMBOL_SYNONYMS: Dict[str, str] = {
    "GOLD": "XAU",
    "XAUUSD": "XAU",
    "XAUUSDT": "XAU",
    "XAU-USD": "XAU",
    "XAU-USDT": "XAU",
    "SILVER": "XAG",
    "XAGUSD": "XAG",
    "XAGUSDT": "XAG",
    "XAG-USD": "XAG",
    "XAG-USDT": "XAG",
}


def _lighter_alias_keys(symbol: str) -> List[str]:
    raw = str(symbol or "").strip().upper()
    keys: List[str] = []
    if raw:
        keys.append(raw)
    if "-" in raw:
        base, rest = raw.split("-", 1)
        if rest in _LIGHTER_QUOTE_SUFFIXES and base:
            keys.append(base)
    # Strip common quote suffixes (XAUUSD → XAU) even without a dash.
    for suffix in ("USDT", "USDC", "USD", "PERP"):
        if raw.endswith(suffix) and len(raw) > len(suffix):
            prefix = raw[: -len(suffix)]
            if prefix and prefix not in keys:
                keys.append(prefix)
    # Human synonyms (GOLD → XAU, SILVER → XAG, …).
    for seed in list(keys):
        mapped = _LIGHTER_SYMBOL_SYNONYMS.get(seed)
        if mapped and mapped not in keys:
            keys.append(mapped)
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

    # Phase 5.1: exact-identity targeting by ``client_order_index``.
    #
    # When a submission's reconciliation did not confirm every child,
    # the unresolved children are known by their client_order_index but
    # may not yet have a known exchange order_id. Cancelling by
    # client_order_index resolves that order_id from the book, so the
    # cleanup still reaches every positively identified test order.
    #
    # This is STILL exact identity: only orders whose
    # client_order_index is in the explicit set AND which match
    # symbol+side are cancelled. Every other active order is
    # baseline and is preserved.
    explicit_cois_raw = request.get("client_order_indices")
    explicit_cois: Optional[set[int]] = None
    if explicit_cois_raw is not None:
        if not isinstance(explicit_cois_raw, (list, tuple, set)):
            return make_failure(operation="cancel_order_group", exchange=name, account=account_name, code="INVALID_ORDER_IDS", message="client_order_indices must be a list of integer client order indices.")
        explicit_cois = set()
        for raw in explicit_cois_raw:
            try:
                explicit_cois.add(int(raw))
            except Exception:  # noqa: BLE001
                return make_failure(operation="cancel_order_group", exchange=name, account=account_name, code="INVALID_ORDER_IDS", message=f"client_order_indices contains a non-integer value: {raw!r}")

    # Classification scope (same contract as Hyperliquid): when the UI
    # cancels TAKE PROFIT / STOP LOSS, only those types are eligible.
    # Default with no classification and no order_ids: entry_limit only —
    # never sweep TP/SL when cancelling an ordinary LIMIT ladder.
    requested_classification = str(
        request.get("classification") or request.get("kind") or request.get("order_kind") or ""
    ).strip().lower()
    aliases = {
        "tp": "take_profit",
        "take-profit": "take_profit",
        "take_profit": "take_profit",
        "sl": "stop_loss",
        "stop-loss": "stop_loss",
        "stop_loss": "stop_loss",
        "entry": "entry_limit",
        "entry_limit": "entry_limit",
        "limit": "entry_limit",
        "trigger": "trigger",
    }
    if requested_classification:
        scope_classification = aliases.get(requested_classification, requested_classification)
    elif explicit_ids is not None or explicit_cois is not None:
        scope_classification = None  # exact identity only
    else:
        scope_classification = "entry_limit"

    try:
        pre_orders = _fetch_active_orders(credentials, _mint_auth_token_cached(credentials))
    except Exception as exc:  # noqa: BLE001
        return make_failure(operation="cancel_order_group", exchange=name, account=account_name, code="OPEN_ORDERS_UNAVAILABLE", message=sanitize_error_message(str(exc)))

    def _order_class(order: Dict[str, Any]) -> str:
        kind = _order_protection_kind(order)
        if kind == "tp":
            return "take_profit"
        if kind == "sl":
            return "stop_loss"
        if bool(order.get("reduce_only")) and _decimal_or_none(order.get("trigger_price")) is not None:
            return "trigger"
        return "entry_limit"

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
        raw_order_id = str(order.get("order_id") or order.get("order_index") or "").strip()
        try:
            parsed_order_id = int(raw_order_id)
        except Exception:  # noqa: BLE001
            continue
        side = "sell" if bool(order.get("is_ask")) else "buy"
        matches_scope = order_market_id == market_id and side == requested_side
        if matches_scope and scope_classification is not None:
            if _order_class(order) != scope_classification:
                matches_scope = False
        if explicit_ids is not None:
            # Exact-identity mode: only an OID in the explicit set AND
            # matching the symbol+side(+class) scope is a target.
            if matches_scope and parsed_order_id in explicit_ids:
                target_orders.append(order)
                target_ids.append(parsed_order_id)
            else:
                non_target_ids.append(parsed_order_id)
        elif explicit_cois is not None:
            # Exact-identity mode by client_order_index (Phase 5.1).
            try:
                entry_coi = int(order.get("client_order_index") or 0)
            except Exception:  # noqa: BLE001
                entry_coi = 0
            if matches_scope and entry_coi in explicit_cois:
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
    # the account's tier-aware CANCEL budget paces the rolling
    # sendTx request bucket. One attempt per batch; on
    # 429 / rejection / ambiguity we STOP and reconcile — no auto-retry.
    # ------------------------------------------------------------------
    accepted_count = 0
    submitted_count = 0
    stop_reason: Optional[Dict[str, Any]] = None
    batches: List[Dict[str, Any]] = []
    # Per-account nonce lease (Phase 3 safety review). Cancels allocate
    # nonces on the SAME per-API-key stream as creates, so they must not
    # run concurrently with a create on the same account.
    cancel_lease_cm = None
    try:
        cancel_lease_cm = _lighter_account_lease(
            credentials, int(credentials["api_key_index"])
        )
        cancel_lease_cm.__enter__()
    except _LighterNonceLeaseUnavailable as exc:
        return make_failure(
            operation="cancel_order_group",
            exchange=name,
            account=account_name,
            code="LIGHTER_NONCE_LEASE_UNAVAILABLE",
            message=(
                "Another Lighter operation is already running for this account; "
                "refusing to cancel with a second nonce stream."
            ),
            exchange_reason=sanitize_error_message(str(exc))[:160],
        )
    try:
        # Phase 4.1: run EVERY batch of this group on ONE event loop,
        # with ONE SignerClient built inside that loop. A client per
        # batch would re-seed the nonce counter from the server and
        # re-sign the same nonces; a client built in the caller's
        # context would be bound to a different loop than the one that
        # uses it (aiohttp needs a live loop at construction AND use).
        group = _run_cancel_tx_group(
            credentials=credentials,
            market_id=market_id,
            target_ids=list(target_ids),
        )
        batches = list(group.get("batches") or [])
        stop_reason = group.get("stop_reason")
        for row in batches:
            accepted_count += int(row.get("accepted") or 0)
            submitted_count += int(row.get("submitted") or 0)
    finally:
        try:
            if cancel_lease_cm is not None:
                cancel_lease_cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass

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
        elif oc == _LADDER_BATCH_OUTCOME_LOCAL_FAILURE:
            # Nothing was signed and nothing was sent. This is a definite
            # local failure, NOT an ambiguous cancellation.
            error_code = "CANCEL_LOCAL_FAILURE"
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
# A DEFINITE LOCAL failure: nothing was signed and nothing was sent
# (e.g. the SignerClient could not be constructed). Distinct from
# AMBIGUOUS, which means a transaction MAY have reached the exchange.
_LADDER_BATCH_OUTCOME_LOCAL_FAILURE = "local_failure"
_LADDER_BATCH_OUTCOME_INSUFFICIENT_MARGIN = "insufficient_margin"
_LADDER_BATCH_OUTCOME_INVALID_ORDER = "invalid_order"
_LADDER_BATCH_OUTCOME_PRICE_TOO_FAR = "price_too_far_from_mark"


# ---------------------------------------------------------------------------
# Per-account nonce coordination (Phase 3 safety review).
#
# The SDK's nonce counter and per-key lock live on the SignerClient
# INSTANCE. Every call site in this agent builds its own client, so two
# concurrent ladders would each seed a private counter from the server's
# ``nextNonce`` and hand out the SAME nonces.
#
# WHY THIS IS NOT SOLVED BY A HIGH-WATER MARK: Lighter's nonce contract
# is strictly ``new_nonce = previous_nonce + 1`` and nonces are tracked
# PER API KEY. The docs are explicit that a hole is as fatal as a
# collision -- "when a transaction fails WITHOUT consuming its nonce,
# any transactions you already sent on the same key with higher nonces
# will fail too". The SDK's own rollback (``acknowledge_failure``)
# decrements the CLIENT's counter, so a process-wide high-water mark
# that cannot be rolled back would leave a permanent hole and poison
# every later transaction on that key.
#
# THE FIX: keep the SDK's per-client counter authoritative (so its
# rollback stays correct and hole-free), and make sure only ONE client
# per account+API key is ever live at a time. A process-wide lease
# serialises trading per account:
#
#   * The lease is HELD for the whole of a ladder / order submission,
#     so the nonce counter of that client is the only one in use.
#   * A second concurrent caller on the same account either waits for
#     the lease or is refused with a clear, safe error. It never
#     allocates nonces from a second, unsynchronised client.
#   * Because the lease spans the whole submission, the SDK counter
#     stays consistent and ``acknowledge_failure`` can roll it back
#     exactly, leaving no hole.
#
# The lease is also cross-process: it is an exclusive ``flock`` on a
# per-account lock file, mirroring ``_LighterQuotaLedger``. If the lock
# cannot be taken, the caller MUST refuse to trade rather than proceed
# with an unsynchronised nonce stream.
#
# Why a plain ``threading.Lock`` for the in-process part: each batch
# runs under its own ``asyncio.run()`` (or a worker thread), and an
# ``asyncio.Lock`` is bound to the loop that created it. A plain lock
# is correct across threads AND event loops.
# ---------------------------------------------------------------------------

_LIGHTER_NONCE_LEASES: Dict[Tuple[str, int, int], threading.Lock] = {}
_LIGHTER_NONCE_LEASES_GUARD = threading.Lock()


def _nonce_scope_key(
    credentials: Optional[Dict[str, Any]],
    api_key_index: int,
) -> Tuple[str, int, int]:
    """Build the process-wide nonce key for one account + API key."""
    creds = credentials or {}
    chain = str(creds.get("chain") or "").strip().upper()
    account_index = int(creds.get("account_index") or 0)
    return (chain, account_index, int(api_key_index))


def _get_nonce_lease(scope: Tuple[str, int, int]) -> threading.Lock:
    """Return the in-process lease lock for one account + API key."""
    with _LIGHTER_NONCE_LEASES_GUARD:
        lease = _LIGHTER_NONCE_LEASES.get(scope)
        if lease is None:
            lease = _LIGHTER_NONCE_LEASES[scope] = threading.Lock()
        return lease


def _process_wide_nonce_guard() -> threading.Lock:
    """Return the guard that protects the nonce lease table.

    One lock for every account: the critical sections are microseconds
    long, so a single lock is simpler and safer than per-key locks
    (which would need their own lifecycle across loops).
    """
    return _LIGHTER_NONCE_LEASES_GUARD


class _LighterNonceLeaseUnavailable(RuntimeError):
    """The per-account nonce lease could not be acquired."""


@contextlib.contextmanager
def _lighter_account_lease(
    credentials: Optional[Dict[str, Any]],
    api_key_index: int,
    *,
    timeout_seconds: Optional[float] = None,
):
    """Hold the per-account nonce lease for the duration of a submission.

    Lighter nonces are tracked PER API KEY and must be strictly
    ``previous + 1``. Two clients on the same account would each seed
    their own counter from the server and hand out colliding nonces;
    and a client that is discarded mid-flight leaves a hole that
    poisons every later transaction on that key.

    The lease therefore guarantees that, for one account + API key, only
    ONE nonce-allocating client is live at a time:

      * an in-process ``threading.Lock`` (correct across threads and
        across the per-batch ``asyncio.run`` loops); and
      * a cross-process exclusive ``flock`` on a per-account lock file,
        mirroring ``_LighterQuotaLedger``.

    If the lock cannot be acquired within the timeout, this RAISES
    rather than proceeding — trading with an unsynchronised nonce
    stream is exactly the failure mode we are preventing.

    Read-only operations must NOT take this lease; only paths that
    allocate nonces need it.
    """
    scope = _nonce_scope_key(credentials, api_key_index)
    lease = _get_nonce_lease(scope)
    if timeout_seconds is None:
        timeout_seconds = LIGHTER_NONCE_LEASE_TIMEOUT_SECONDS

    directory = Path(LIGHTER_NONCE_LEASE_DIR)
    safe = re.sub(r"[^0-9a-z]", "_", "_".join(str(p) for p in scope)) or "unknown"
    lock_path = directory / f"{safe}.lock"

    directory.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    acquired_inproc = False
    try:
        # In-process first: cheap, and it prevents two threads in this
        # process from ever competing for the file lock.
        deadline = time.monotonic() + timeout_seconds
        while True:
            if lease.acquire(timeout=0.05):
                acquired_inproc = True
                break
            if time.monotonic() >= deadline:
                raise _LighterNonceLeaseUnavailable(
                    "timed out waiting for the in-process Lighter nonce lease"
                )
        # Cross-process: exclusive flock on the per-account lock file.
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                import fcntl

                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise _LighterNonceLeaseUnavailable(
                        "timed out waiting for the cross-process Lighter nonce lease"
                    )
                time.sleep(0.05)
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except Exception:  # noqa: BLE001
            pass
        os.close(lock_fd)
        if acquired_inproc:
            lease.release()


async def _reserve_ladder_nonces(
    *,
    signer: Any,
    api_key_index: int,
    count: int,
    credentials: Optional[Dict[str, Any]] = None,
) -> List[int]:
    """Reserve ``count`` consecutive nonces for ``api_key_index``.

    Returns a list of nonces in order. Caller MUST
    ``acknowledge_failure(api_key_index)`` if any of the subsequent sign
    operations fail, so the reserved nonces are released.

    SAFETY (Phase 3 review): the SDK's per-key lock and nonce counter
    live on the SignerClient INSTANCE. Each call site builds its own
    client, so two concurrent ladders in the same process would each
    seed their private counter from the server's ``nextNonce`` and
    allocate OVERLAPPING nonces. Lighter's contract is per-API-key
    ``previous + 1``, and a collision or a hole poisons every later
    transaction on that key.

    We therefore allocate from the SDK's own counter (so
    ``acknowledge_failure`` can roll it back exactly, leaving NO hole)
    while holding BOTH:
      * the SDK's per-key lock, so callers sharing one client cannot
        interleave; and
      * the per-account lease taken by the caller for the whole
        submission, so callers using DIFFERENT clients on the same
        account cannot interleave either.
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
    signer: Any = None,
    ws_session: Any = None,
    cid: str = "",
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
        batch_signer = signer if signer is not None else _build_signer_client(credentials)
        api_key_index = int(credentials["api_key_index"])
        # ------------------------------------------------------------------
        # 0. Tier-aware L2 transaction-budget check.
        #
        # The limit depends on the account tier:
        #   * Standard / unknown → the documented 40/min tx-type limit,
        #     applied conservatively at 30 txs per 60 s (today's proven
        #     behaviour).
        #   * Plus / Premium     → the sendTx request bucket governs and
        #     L2CreateOrder additionally consumes VOLUME QUOTA (n txs in
        #     one request consume n quota). The volume quota is shared
        #     per L1 address across processes, so it is reserved in the
        #     cross-process ledger before we sign.
        #
        # A sendTxBatch with N L2CreateOrder transactions consumes N
        # against this budget AND counts as ONE request against the
        # sendTx bucket. Requests and transactions are counted
        # separately.
        # ------------------------------------------------------------------
        policy = _get_lighter_tier_policy(credentials)
        budget = _get_lighter_tier_budget(credentials, policy, kind="create")
        quota_ledger: Optional[_LighterQuotaLedger] = None
        quota_reserved = 0
        if policy.quota_bearing and policy.l1_address:
            quota_ledger = _LighterQuotaLedger(
                policy.l1_address,
                window_seconds=policy.window_seconds,
            )
        try:
            budget.wait_for_capacity(len(children))
        except _LighterBudgetExhausted as exc:
            return {
                "outcome": _LADDER_BATCH_OUTCOME_RATE_LIMITED,
                "tx_hashes": [],
                "submitted_count": len(children),
                "accepted_count": 0,
                "rejected_count": 0,
                "unknown_count": 0,
                "api_code": 23000,
                "api_message": sanitize_error_message(str(exc)),
                "raw_response": None,
                "nonces": [],
                "child_to_tx_hash": {},
                "rate_limit_reason": sanitize_error_message(str(exc)),
                "tier_policy": policy.describe(),
            }
        if quota_ledger is not None:
            try:
                quota_reserved = quota_ledger.reserve(
                    len(children),
                    remaining_hint=policy.quota_remaining,
                    ceiling=policy.quota_remaining,
                )
            except _LighterQuotaLedgerUnavailable as exc:
                # Cross-process coordination is unavailable. Roll the
                # local reservation back and fall back to the
                # conservative budget rather than proceed unserialised.
                budget.rollback(len(children))
                logger.warning(
                    "[LIGHTER-TIER] quota ledger unavailable (%s); "
                    "falling back to conservative pacing",
                    sanitize_error_message(str(exc))[:160],
                )
                fallback = _standard_lighter_policy(
                    "cross-process quota ledger unavailable; conservative pacing"
                )
                budget = _get_lighter_tier_budget(credentials, fallback, kind="create")
                try:
                    budget.wait_for_capacity(len(children))
                except _LighterBudgetExhausted as exc2:
                    return {
                        "outcome": _LADDER_BATCH_OUTCOME_RATE_LIMITED,
                        "tx_hashes": [],
                        "submitted_count": len(children),
                        "accepted_count": 0,
                        "rejected_count": 0,
                        "unknown_count": 0,
                        "api_code": 23000,
                        "api_message": sanitize_error_message(str(exc2)),
                        "raw_response": None,
                        "nonces": [],
                        "child_to_tx_hash": {},
                        "rate_limit_reason": sanitize_error_message(str(exc2)),
                        "tier_policy": fallback.describe(),
                    }
        # ------------------------------------------------------------------
        # 1. Reserve nonces for every child in this batch.
        # ------------------------------------------------------------------
        try:
            nonces = await _reserve_ladder_nonces(
                signer=batch_signer,
                api_key_index=api_key_index,
                count=len(children),
                credentials=credentials,
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
                tx_type, tx_info, tx_hash, error = batch_signer.sign_create_order(
                    market_index=int(market["market_id"]),
                    client_order_index=int(client_order_index),
                    base_amount=int(base_amount),
                    price=int(price_int),
                    is_ask=side == "sell",
                    order_type=batch_signer.ORDER_TYPE_LIMIT,
                    time_in_force=batch_signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                    reduce_only=False,
                    trigger_price=batch_signer.NIL_TRIGGER_PRICE,
                    order_expiry=batch_signer.DEFAULT_28_DAY_ORDER_EXPIRY,
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
                    batch_signer.nonce_manager.acknowledge_failure(api_key_index)
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
        # 3. Submit through WS jsonapi/sendtxbatch when a session exists,
        #    else REST send_tx_batch. ONE write. NO retry.
        # ------------------------------------------------------------------
        api_response = None
        submit_error: Optional[str] = None
        sent_write = False
        t_submit = time.monotonic()
        try:
            if ws_session is not None:
                req_id = f"ladder-{cid or _ladder_cid()}-{int(t_submit * 1000)}"
                payload = _build_ws_sendtxbatch_payload(
                    req_id=req_id, tx_types=tx_types, tx_infos=tx_infos,
                )
                if len(payload.encode("utf-8")) > LIGHTER_WS_MAX_MESSAGE_BYTES:
                    raise ValueError("WS sendtxbatch payload exceeds 64KB")
                await ws_session.send(payload)
                sent_write = True
                raw = await asyncio.wait_for(
                    ws_session.recv(), timeout=LIGHTER_WS_RECV_TIMEOUT_SECONDS,
                )
                parsed = _parse_ws_sendtxbatch_response(raw)
                api_response = _WsBatchResponse(parsed)
                if parsed.get("code") == 23000:
                    raise _LighterRateLimitError(23000, parsed.get("message") or "Too Many Requests")
            else:
                sent_write = True
                api_response = await batch_signer.send_tx_batch(
                    tx_types=tx_types, tx_infos=tx_infos
                )
        except ValueError as exc:
            # Oversize / local reject: write never left this process.
            submit_error = sanitize_error_message(str(exc))
            last_send_tx_batch_exc_holder[0] = exc
        except _LighterRateLimitError as exc:
            submit_error = sanitize_error_message(str(exc))
            last_send_tx_batch_exc_holder[0] = exc
        except Exception as exc:  # noqa: BLE001
            submit_error = sanitize_error_message(str(exc))
            last_send_tx_batch_exc_holder[0] = exc

        if isinstance(last_send_tx_batch_exc_holder[0], _LighterRateLimitError):
            for _ in range(len(nonces)):
                try:
                    batch_signer.nonce_manager.acknowledge_failure(api_key_index)
                except Exception:  # noqa: BLE001
                    pass
            return {
                "outcome": _LADDER_BATCH_OUTCOME_RATE_LIMITED,
                "tx_hashes": [],
                "submitted_count": len(children),
                "accepted_count": 0,
                "rejected_count": len(children),
                "unknown_count": 0,
                "api_code": 23000,
                "api_message": submit_error or "Too Many Requests",
                "raw_response": None,
                "nonces": list(nonces),
                "child_to_tx_hash": {},
                "timing_ms": {"submit": (time.monotonic() - t_submit) * 1000.0},
            }

        if isinstance(last_send_tx_batch_exc_holder[0], ValueError) and "exceeds" in str(
            last_send_tx_batch_exc_holder[0]
        ):
            for _ in range(len(nonces)):
                try:
                    batch_signer.nonce_manager.acknowledge_failure(api_key_index)
                except Exception:  # noqa: BLE001
                    pass
            return {
                "outcome": _LADDER_BATCH_OUTCOME_EXCHANGE_REJECTED,
                "tx_hashes": [],
                "submitted_count": 0,
                "accepted_count": 0,
                "rejected_count": len(children),
                "unknown_count": 0,
                "api_code": None,
                "api_message": submit_error or "WS sendtxbatch payload exceeds 64KB",
                "raw_response": None,
                "nonces": list(nonces),
                "child_to_tx_hash": {},
                "timing_ms": {"submit": (time.monotonic() - t_submit) * 1000.0},
            }

        if submit_error is not None and not sent_write:
            # Local reject (oversize) or send() failed before the server
            # could accept — treat like envelope reject: roll back nonces.
            envelope_failure = _classify_send_tx_batch_envelope_failure(
                last_send_tx_batch_exc_holder[0]
                if last_send_tx_batch_exc_holder[0] is not None
                else RuntimeError(submit_error)
            )
            for _ in range(len(nonces)):
                try:
                    batch_signer.nonce_manager.acknowledge_failure(api_key_index)
                except Exception:  # noqa: BLE001
                    pass
            return {
                "outcome": envelope_failure.get("outcome") or _LADDER_BATCH_OUTCOME_EXCHANGE_REJECTED,
                "tx_hashes": [],
                "submitted_count": 0,
                "accepted_count": 0,
                "rejected_count": len(children),
                "unknown_count": 0,
                "api_code": envelope_failure.get("code"),
                "api_message": envelope_failure.get("reason") or submit_error,
                "raw_response": None,
                "nonces": list(nonces),
                "child_to_tx_hash": {},
                "timing_ms": {"submit": (time.monotonic() - t_submit) * 1000.0},
            }

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
                        batch_signer.nonce_manager.acknowledge_failure(
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
            #
            # NOTE: ``exc`` is deleted by Python when its ``except``
            # block ends, so the original exception is read from
            # ``last_send_tx_batch_exc_holder`` — never from ``exc``.
            transport_exc = last_send_tx_batch_exc_holder[0]
            if transport_exc is None:
                transport_exc = RuntimeError(submit_error)
            backend_code_from_exc = _extract_backend_code_from_exception(transport_exc)
            exc_status = (
                getattr(transport_exc, "status", None)
                or getattr(transport_exc, "status_code", None)
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
                # The volume quota was not consumed either (the txs
                # never reached the sequencer), so release it back to
                # the cross-process ledger.
                if quota_ledger is not None and quota_reserved:
                    quota_ledger.release(quota_reserved)
                # Nonces were NOT consumed by the server because the
                # batch envelope never reached the sequencer. Roll
                # them back so the next batch's reservation starts
                # cleanly from the unchanged server counter.
                for _ in range(len(nonces)):
                    try:
                        batch_signer.nonce_manager.acknowledge_failure(api_key_index)
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
                    batch_signer.nonce_manager.acknowledge_failure(api_key_index)
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
                    batch_signer.nonce_manager.acknowledge_failure(api_key_index)
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


async def _submit_cancel_tx_batch_async(
    *,
    credentials: Dict[str, Any],
    market_id: int,
    order_ids: List[int],
    signer: Any,
) -> Dict[str, Any]:
    """Submit one ``send_tx_batch`` HTTP request carrying N independent
    L2CancelOrder transactions (tx_type=15). NO automatic retry.

    ASYNC CORE — MUST be awaited on the event loop that owns ``signer``.

    The SDK's ``SignerClient`` creates an ``aiohttp.ClientSession`` at
    construction time, and aiohttp requires a live event loop at BOTH
    construction and use. The client is therefore built by the CALLER,
    inside the loop that will use it, and passed in here. One client
    spans every batch of a cancel group, so the SDK's nonce counter
    stays consecutive across batch boundaries (Phase 4.1).

    Mirrors ``_submit_send_tx_batch`` but for cancellations. Each
    target order is signed via ``signer.sign_cancel_order`` with an
    explicit nonce reserved from ``nonce_manager.async_next_nonce``.
    The signed tx_info strings and tx_types are packed into a single
    ``signer.send_tx_batch`` HTTP call, so N cancellations cost ONE
    HTTP request — but N against the L1-address transaction quota.

    Rate-limit scope (primary sources):
      * Per the official volume-quota documentation: "Single-order
        cancels (L2CancelOrder, 15) do not consume quota and have no
        rate limit." The volume quota is consumed only by
        L2CreateOrder (14), L2CancelAllOrders (16), L2ModifyOrder (17)
        and L2CreateGroupedOrders (28).
      * L2CancelOrder therefore draws on NEITHER the volume quota NOR
        the 40/min transaction-type limit. It is bounded by the
        sendTx/sendTxBatch REQUEST bucket.
      * Consequently cancels use their OWN tier-aware budget (the
        "cancel" kind), separate from the create budget. Sharing one
        counter would make cancels queue behind creates and would let
        creates consume quota that cancels do not touch.
      * A Standard/unknown account still shares the documented 40/min
        request budget, so its cancel cap stays at that figure.
      * Transport: REST ``sendTxBatch``, so the batch is capped at the
        CDN-safe REST size (15), not the WS protocol max.

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
    last_send_tx_batch_exc_holder: List[Any] = [None]

    # The client is ALWAYS supplied by the caller, which built it on
    # THIS loop. Building one here would bind an aiohttp session to a
    # different loop than the one that uses it (Phase 4.1).
    batch_signer = signer
    api_key_index = int(credentials["api_key_index"])
    # ------------------------------------------------------------------
    # 0. L2 transaction-budget check (shared L1-address quota).
    # N L2CancelOrder transactions consume N against the same rolling
    # 60s budget used by the ladder create path.
    # ------------------------------------------------------------------
    policy = _get_lighter_tier_policy(credentials)
    budget = _get_lighter_tier_budget(credentials, policy, kind="cancel")
    try:
        budget.wait_for_capacity(len(order_ids))
    except _LighterBudgetExhausted as exc:
        return {
            "outcome": _LADDER_BATCH_OUTCOME_RATE_LIMITED,
            "tx_hashes": [],
            "submitted_count": len(order_ids),
            "accepted_count": 0,
            "rejected_count": 0,
            "unknown_count": 0,
            "api_code": 23000,
            "api_message": sanitize_error_message(str(exc)),
            "raw_response": None,
            "nonces": [],
            "order_to_tx_hash": {},
            "per_tx": [],
            "rate_limit_reason": sanitize_error_message(str(exc)),
            "tier_policy": policy.describe(),
        }
    # ------------------------------------------------------------------
    # 1. Reserve nonces for every cancel in this batch.
    # ------------------------------------------------------------------
    try:
        nonces = await _reserve_ladder_nonces(
            signer=batch_signer,
            api_key_index=api_key_index,
            count=len(order_ids),
            credentials=credentials,
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
            # (L2CancelOrder) per the native batch_signer.
            tx_type, tx_info, tx_hash, error = batch_signer.sign_cancel_order(
                int(market_id),
                int(order_id),
                batch_signer.SKIP_NONCE_OFF,
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
                batch_signer.nonce_manager.acknowledge_failure(api_key_index)
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
        api_response = await batch_signer.send_tx_batch(
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
                    batch_signer.nonce_manager.acknowledge_failure(api_key_index)
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
        #
        # NOTE: ``exc`` is deleted by Python when its ``except``
        # block ends, so the original exception is read from
        # ``last_send_tx_batch_exc_holder`` — never from ``exc``.
        transport_exc = last_send_tx_batch_exc_holder[0]
        if transport_exc is None:
            transport_exc = RuntimeError(submit_error)
        backend_code_from_exc = _extract_backend_code_from_exception(transport_exc)
        exc_status = (
            getattr(transport_exc, "status", None)
            or getattr(transport_exc, "status_code", None)
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
                    batch_signer.nonce_manager.acknowledge_failure(api_key_index)
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
                batch_signer.nonce_manager.acknowledge_failure(api_key_index)
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
                batch_signer.nonce_manager.acknowledge_failure(api_key_index)
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


def _run_cancel_tx_group(
    *,
    credentials: Dict[str, Any],
    market_id: int,
    target_ids: List[int],
) -> Dict[str, Any]:
    """Run EVERY cancel batch of one group on ONE event loop.

    The SDK's ``SignerClient`` creates an ``aiohttp.ClientSession`` at
    construction time, and aiohttp requires a live event loop at BOTH
    construction and use. It must therefore be built AND used on the
    same loop.

    The whole group is bridged as a SINGLE coroutine so that:

      * one client spans every batch, so the SDK's nonce counter stays
        strictly consecutive across batch boundaries (a new client per
        batch would re-seed from the server's ``nextNonce`` and
        re-sign the same nonces);
      * the client is built and closed on the loop that owns it;
      * a synchronous CLI caller (no running loop) and the gateway
        (a running loop) both work.

    Returns ``{"batches": [...], "stop_reason": {...} | None}``.

    The caller must already hold the per-account nonce lease.
    """
    chunk_size = max(1, int(LIGHTER_CANCEL_TX_BATCH_SIZE))
    chunk_starts = list(range(0, len(target_ids), chunk_size))

    async def _run_group() -> Dict[str, Any]:
        client = _build_signer_client(credentials)
        batch_rows: List[Dict[str, Any]] = []
        stop_reason: Optional[Dict[str, Any]] = None
        try:
            for batch_index, start in enumerate(chunk_starts):
                # Preventive pacing — only between batches, never
                # before the first or after the last.
                if batch_index > 0:
                    await asyncio.sleep(LIGHTER_CANCEL_TX_BATCH_PAUSE_SECONDS)
                chunk_ids = target_ids[start:start + chunk_size]
                outcome = await _submit_cancel_tx_batch_async(
                    credentials=credentials,
                    market_id=market_id,
                    order_ids=chunk_ids,
                    signer=client,
                )
                batch_rows.append({
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
                # Continuation gate: continue ONLY if the batch fully
                # succeeded. No blind retries.
                if outcome.get("outcome") != _LADDER_BATCH_OUTCOME_SUCCESS:
                    stop_reason = outcome
                    break
        finally:
            # Close the client on the loop that owns it.
            closer = getattr(client, "close", None)
            if closer is not None:
                try:
                    result = closer()
                    if hasattr(result, "__await__"):
                        await result
                except Exception:  # noqa: BLE001
                    pass
        return {"batches": batch_rows, "stop_reason": stop_reason}

    try:
        return _run_lighter_coro_blocking(
            credentials,
            lambda: _run_group(),
            thread_name="lighter-cancel-tx-group",
            max_retries=0,
        )
    except Exception as exc:  # noqa: BLE001
        # The coroutine failed BEFORE any batch was attempted (e.g. the
        # SignerClient could not be constructed). Nothing was signed and
        # nothing was sent, so this is a DEFINITE local failure — NOT
        # ambiguous. Report it as such.
        return {
            "batches": [],
            "stop_reason": {
                "outcome": _LADDER_BATCH_OUTCOME_LOCAL_FAILURE,
                "api_code": None,
                "api_message": sanitize_error_message(str(exc)),
                "submitted_count": 0,
                "accepted_count": 0,
                "rejected_count": 0,
                "unknown_count": 0,
                "batches_attempted": 0,
                "local_failure_reason": "cancel group could not start",
            },
        }


def _submit_cancel_tx_batch(
    *,
    credentials: Dict[str, Any],
    market_id: int,
    order_ids: List[int],
    signer: Optional[Any] = None,
) -> Dict[str, Any]:
    """Submit ONE cancel batch from a synchronous caller.

    Builds the SignerClient INSIDE the loop that will use it, and closes
    it on that same loop. The SDK creates an aiohttp session at
    construction time and aiohttp needs a live loop at BOTH construction
    and use (Phase 4.1).

    The multi-batch cancel GROUP must use ``_run_cancel_tx_group`` so
    that ONE client — and therefore ONE consecutive nonce stream —
    spans every batch.
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

    async def _run() -> Dict[str, Any]:
        client = signer if signer is not None else _build_signer_client(credentials)
        try:
            return await _submit_cancel_tx_batch_async(
                credentials=credentials,
                market_id=market_id,
                order_ids=order_ids,
                signer=client,
            )
        finally:
            closer = getattr(client, "close", None)
            if closer is not None:
                try:
                    result = closer()
                    if hasattr(result, "__await__"):
                        await result
                except Exception:  # noqa: BLE001
                    pass

    try:
        return _run_lighter_coro_blocking(
            credentials,
            lambda: _run(),
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


# ---------------------------------------------------------------------------
# Reconciliation after a WebSocket batch (Phase 5.1).
#
# The exchange's ``accountActiveOrders`` indexer can lag an accepted
# batch by a few hundred milliseconds. A single read taken right after
# the ack can therefore return ZERO matches for children the exchange
# has already accepted. Measured on Phase 5 (100 orders, two WS
# batches): batch 1's read took 374 ms and returned 50/50; batch 2's
# took 146 ms and returned 0/50 — while the WS ack carried
# ``api_code=200`` and 50 tx hashes for every child, and a later read
# found all 50 on the book.
#
# Treating that stale snapshot as "the orders did not land" is wrong
# (it would trigger a duplicate submission of live orders) and
# treating it as an exchange REJECTION is worse (the exchange
# accepted them).
#
# The reconciliation is therefore a bounded read-after-write loop:
#   * fresh snapshot per attempt, read-only;
#   * matches ACCUMULATE across snapshots — a child confirmed by any
#     snapshot stays confirmed;
#   * backoff 250 ms, then 500 ms, then 1 s, under a 10 s deadline;
#   * a child that is absent from every snapshot is genuinely
#     unconfirmed, and is reported as unresolved — never as accepted;
#   * a child is confirmed by its unique ``client_order_index`` plus
#     identity checks (symbol, side, price, quantity, status);
#   * a child does NOT have to still be OPEN to be confirmed — a
#     filled or otherwise non-open order still proves it landed.
#
# This NEVER resends a CreateOrder. It only reads.
# ---------------------------------------------------------------------------

LIGHTER_RECONCILE_DEADLINE_SECONDS = 10.0
LIGHTER_RECONCILE_BACKOFF_SECONDS = (0.25, 0.5, 1.0)
# Hard cap on read attempts, so a caller-supplied no-op sleep (or a
# pathological clock) can never turn the pass into an unbounded loop.
LIGHTER_RECONCILE_MAX_ATTEMPTS = 40

# Stats from the most recent reconciliation, for the caller to read.
_LIGHTER_LAST_RECONCILE_STATS: Dict[str, Any] = {}

_LIGHTER_RECONCILE_OUTCOME_CONFIRMED = "confirmed"
_LIGHTER_RECONCILE_OUTCOME_PENDING = "pending"
_LIGHTER_RECONCILE_OUTCOME_TIMEOUT = "timeout"
_LIGHTER_RECONCILE_OUTCOME_ERROR = "error"


def _lighter_order_matches_child(
    entry: Dict[str, Any],
    *,
    market_id: int,
    side: str,
    price: Optional[Decimal],
    size: Optional[Decimal],
) -> Tuple[bool, str]:
    """Check that an order entry really is the child we submitted.

    Returns ``(ok, reason)``. ``reason`` is empty when the entry
    matches. Identity is checked on the fields the exchange echoes
    back: market, side, limit price, initial size and order type.

    The order does NOT have to be open — a filled child still proves
    the order landed.
    """
    try:
        entry_market_id = int(entry.get("market_index") or 0)
    except Exception:  # noqa: BLE001
        return False, "unparseable market_index"
    if entry_market_id != market_id:
        return False, f"market {entry_market_id} != {market_id}"

    entry_side = "sell" if bool(entry.get("is_ask")) else "buy"
    if entry_side != side:
        return False, f"side {entry_side} != {side}"

    order_type = str(entry.get("type") or "").lower()
    if order_type and order_type not in ("limit", "market"):
        return False, f"order type {order_type} is not a limit/market order"

    if price is not None:
        raw_price = entry.get("price")
        if raw_price is not None and str(raw_price) != "":
            try:
                if Decimal(str(raw_price)) != price:
                    return False, f"price {raw_price} != {price}"
            except Exception:  # noqa: BLE001
                return False, "unparseable price"

    if size is not None:
        raw_size = entry.get("initial_base_amount")
        if raw_size is not None and str(raw_size) != "":
            try:
                if Decimal(str(raw_size)) != size:
                    return False, f"size {raw_size} != {size}"
            except Exception:  # noqa: BLE001
                return False, "unparseable size"

    return True, ""


def _reconcile_batch_by_client_order_index(
    *,
    credentials: Dict[str, Any],
    market_id: int,
    side: str,
    expected_client_order_indices: List[int],
    auth_token: str,
    deadline_seconds: Optional[float] = None,
    backoff_seconds: Optional[Tuple[float, ...]] = None,
    expected_prices: Optional[Dict[int, Decimal]] = None,
    expected_sizes: Optional[Dict[int, Decimal]] = None,
    sleep: Optional[Any] = None,
) -> Dict[int, int]:
    """Map ``client_order_index -> order_id`` for the expected children.

    Read-only, bounded read-after-write. Returns a dict containing
    only the children this batch CONFIRMED landed. Matches accumulate
    across snapshots: a child seen in any snapshot is confirmed and is
    never forgotten.

    A child that is absent from every snapshot before the deadline is
    NOT confirmed and is simply absent from the returned mapping — the
    caller must treat that as unresolved, not as rejected and not as
    accepted.

    This never resends a CreateOrder; it only reads the book.
    """
    expected = {int(idx) for idx in expected_client_order_indices}
    if not expected:
        return {}
    delays = tuple(backoff_seconds or LIGHTER_RECONCILE_BACKOFF_SECONDS)
    napper = sleep if sleep is not None else time.sleep
    budget = (
        LIGHTER_RECONCILE_DEADLINE_SECONDS
        if deadline_seconds is None
        else float(deadline_seconds)
    )

    confirmed: Dict[int, int] = {}
    seen_indices: Dict[int, int] = {}
    duplicates: List[int] = []
    conflicts: List[Dict[str, Any]] = []
    attempts = 0
    deadline = time.monotonic() + max(0.0, budget)

    while True:
        attempts += 1
        # The deadline is checked BEFORE the read as well as after it.
        # A single read can itself block, and the backoff may be a
        # no-op (tests, or a caller-supplied sleep), so the loop must
        # not rely on wall-clock advancing to terminate.
        if attempts > 1 and time.monotonic() >= deadline:
            break
        try:
            orders = _fetch_active_orders(credentials, auth_token)
        except Exception:  # noqa: BLE001
            # A read failure is not evidence the children did not land.
            # Retry until the deadline; the caller sees the error only
            # if we never manage a successful read.
            if time.monotonic() >= deadline:
                raise
            napper(delays[min(attempts - 1, len(delays) - 1)])
            continue

        for entry in orders:
            if not isinstance(entry, dict):
                continue
            try:
                client_order_index = int(entry.get("client_order_index") or 0)
            except Exception:  # noqa: BLE001
                continue
            if client_order_index not in expected:
                continue
            raw_order_id = str(entry.get("order_id") or "").strip()
            try:
                order_id = int(raw_order_id)
            except Exception:  # noqa: BLE001
                continue

            # Duplicate / conflicting client_order_index detection.
            previous = seen_indices.get(client_order_index)
            if previous is not None and previous != order_id:
                conflicts.append({
                    "client_order_index": client_order_index,
                    "order_ids": [previous, order_id],
                })
                continue
            seen_indices[client_order_index] = order_id

            ok, reason = _lighter_order_matches_child(
                entry,
                market_id=market_id,
                side=side,
                price=(expected_prices or {}).get(client_order_index),
                size=(expected_sizes or {}).get(client_order_index),
            )
            if not ok:
                conflicts.append({
                    "client_order_index": client_order_index,
                    "order_id": order_id,
                    "reason": reason,
                })
                continue

            # Accumulate: a child confirmed in ANY snapshot stays
            # confirmed, even if a later snapshot no longer lists it
            # (it may have filled and moved out of the active feed).
            confirmed[client_order_index] = order_id

        if len(confirmed) >= len(expected):
            break
        if time.monotonic() >= deadline:
            break
        if attempts >= LIGHTER_RECONCILE_MAX_ATTEMPTS:
            break
        napper(delays[min(attempts - 1, len(delays) - 1)])

    _LIGHTER_LAST_RECONCILE_STATS.clear()
    _LIGHTER_LAST_RECONCILE_STATS.update({
        "attempts": attempts,
        "confirmed": len(confirmed),
        "expected": len(expected),
        "unresolved": sorted(expected - set(confirmed)),
        "duplicates": duplicates,
        "conflicts": conflicts,
        "outcome": (
            _LIGHTER_RECONCILE_OUTCOME_CONFIRMED
            if len(confirmed) >= len(expected)
            else _LIGHTER_RECONCILE_OUTCOME_TIMEOUT
        ),
    })
    return confirmed


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
    # Batched submission via WS jsonapi/sendtxbatch (REST sendTxBatch fallback).
    #
    # WS protocol max is 50 independently-signed L2CreateOrder txs.
    # REST fallback is capped at 15 (CDN body limit). Both are also
    # capped by the retained 30-tx/60s L2 budget so wait_for_capacity
    # cannot deadlock.
    #
    # Invariants (KAM safety contract):
    #   1. Each ``client_order_index`` is pre-assigned uniquely BEFORE
    #      any sign. It is the authoritative reconciliation key —
    #      never re-issued, never duplicated.
    #   2. ONE write attempt per batch. NO automatic retry. NO
    #      blind resend. A 429 (code 23000) or any non-200 envelope
    #      stops the ladder and triggers a read-only reconcile of the
    #      batches that already landed.
    #   3. sendTxBatch is NOT atomic at the transaction level — the
    #      envelope can be 200 while individual transactions in the
    #      same batch are rejected by the L1 sequencer. We therefore
    #      reconcile by client_order_index, not by tx_hash presence.
    #   4. Pacing is exchange-local preventive throttling — not a
    #      retry mechanism. The 30/60s L2 budget remains in force.
    #   5. One SignerClient (and therefore one nonce manager) per ladder.
    # ------------------------------------------------------------------
    cid = _ladder_cid()
    t0 = time.monotonic()
    client_order_indices = _allocate_client_order_indices(len(children))
    market_id_int = int(str(market.get("market_id") or 0))
    batch_records: List[Dict[str, Any]] = []
    accepted_child_count = 0
    reconciled_child_count = 0
    submitted_batch_count = 0
    landed_child_ids: List[int] = []
    stop_reason: Optional[Dict[str, Any]] = None  # populated on stop
    rejected_child_count = 0
    uncertain_child_count = 0

    t_ws = time.monotonic()
    ws_session = None
    transport = "rest"
    try:
        ws_session = _LighterWsSession.open(credentials)
        transport = "ws"
        _ladder_log(cid, "ws_open", dt_ms=(time.monotonic() - t_ws) * 1000.0, total_ms=(time.monotonic() - t0) * 1000.0)
    except _LighterWsUnavailable as exc:
        ws_session = None
        transport = "rest"
        _ladder_log(
            cid, "ws_unavailable_rest_fallback",
            dt_ms=(time.monotonic() - t_ws) * 1000.0,
            total_ms=(time.monotonic() - t0) * 1000.0,
            reason=sanitize_error_message(str(exc))[:120],
        )
    except Exception as exc:  # noqa: BLE001
        ws_session = None
        transport = "rest"
        _ladder_log(
            cid, "ws_unavailable_rest_fallback",
            dt_ms=(time.monotonic() - t_ws) * 1000.0,
            total_ms=(time.monotonic() - t0) * 1000.0,
            reason=sanitize_error_message(str(exc))[:120],
        )

    t_sign_client = time.monotonic()
    ladder_signer = _build_signer_client(credentials)
    _ladder_log(
        cid, "signer_ready",
        dt_ms=(time.monotonic() - t_sign_client) * 1000.0,
        total_ms=(time.monotonic() - t0) * 1000.0,
        transport=transport,
    )
    # ------------------------------------------------------------------
    # Per-account nonce lease (Phase 3 safety review).
    #
    # Lighter nonces are per-API-key and strictly ``previous + 1``. The
    # SDK's counter lives on the SignerClient instance, so a SECOND
    # ladder on the same account would seed its own counter from the
    # server and hand out colliding nonces -- and an unused earlier
    # nonce poisons every later transaction on that key. The lease
    # guarantees only one nonce-allocating client is live per account.
    #
    # The lease is held across the whole batch loop so the SDK's
    # ``acknowledge_failure`` rollback stays exact (no hole).
    # ------------------------------------------------------------------
    lease_api_key_index = int(credentials["api_key_index"])
    try:
        lease_cm = _lighter_account_lease(credentials, lease_api_key_index)
        lease_cm.__enter__()
    except _LighterNonceLeaseUnavailable as exc:
        return make_failure(
            operation="ladder",
            exchange=name,
            account=account_name,
            code="LIGHTER_NONCE_LEASE_UNAVAILABLE",
            message=(
                "Another Lighter operation is already running for this account; "
                "refusing to trade with a second nonce stream."
            ),
            exchange_reason=sanitize_error_message(str(exc))[:160],
        )
    budget = _get_lighter_tier_budget(
        credentials,
        _get_lighter_tier_policy(credentials),
        kind="create",
    )
    chunk_size = _lighter_ladder_chunk_size(transport, budget)

    chunk_starts = list(range(0, len(children), chunk_size))
    try:
      for batch_index, start in enumerate(chunk_starts):
        # Preventive pacing — only sleep between batches, not before
        # the first one and never after the last batch.
        if batch_index > 0:
            time.sleep(LIGHTER_SEND_TX_BATCH_PAUSE_SECONDS)
        chunk_children = children[start:start + chunk_size]
        chunk_indices = client_order_indices[start:start + chunk_size]

        t_batch = time.monotonic()
        try:
            outcome = _submit_send_tx_batch(
                credentials=credentials,
                market=market,
                side=requested_side,
                children=chunk_children,
                client_order_indices=chunk_indices,
                signer=ladder_signer,
                ws_session=ws_session,
                cid=cid,
            )
        except TypeError as exc:
            if "unexpected keyword" not in str(exc):
                raise
            outcome = _submit_send_tx_batch(
                credentials=credentials,
                market=market,
                side=requested_side,
                children=chunk_children,
                client_order_indices=chunk_indices,
            )
        batch_dt_ms = (time.monotonic() - t_batch) * 1000.0
        _ladder_log(
            cid, "batch_submit",
            dt_ms=batch_dt_ms,
            total_ms=(time.monotonic() - t0) * 1000.0,
            batch_index=batch_index,
            transport=transport,
            n=len(chunk_children),
            outcome=outcome.get("outcome"),
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
            "transport": transport,
            "timing_ms": {"batch": batch_dt_ms, **(outcome.get("timing_ms") or {})},
        })
        rejected_child_count += rejected_count
        uncertain_child_count += unknown_count
        outcome_kind = outcome.get("outcome")
        if outcome_kind == _LADDER_BATCH_OUTCOME_RATE_LIMITED:
            rejected_child_count += max(0, len(chunk_children) - rejected_count - unknown_count)
        elif outcome_kind == _LADDER_BATCH_OUTCOME_AMBIGUOUS:
            uncertain_child_count += max(0, len(chunk_children) - rejected_count - unknown_count)

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
        # Phase 5.1: children the exchange explicitly REJECTED cannot
        # land, so they are excluded from the reconciliation expected
        # set. Including them would make the read-after-write pass wait
        # out its full deadline for children that will never appear —
        # turning a definite rejection into a false "pending" verdict.
        rejected_indices = [
            int(idx)
            for idx, status in zip(chunk_indices, per_tx_statuses)
            if status == _LIGHTER_PER_TX_API_REJECTED
        ]
        batch_records[-1]["api_rejected_client_order_indices"] = rejected_indices
        if rejected_indices:
            batch_records[-1]["ok"] = False
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

    finally:
        if ws_session is not None:
            try:
                closer = getattr(ws_session, "close", None)
                if closer is not None:
                    result = closer()
                    if asyncio.iscoroutine(result):
                        _run_lighter_coro_blocking(
                            credentials,
                            lambda: result,
                            thread_name="lighter-ws-close",
                            max_retries=0,
                        )
            except Exception:  # noqa: BLE001
                pass
        # Release the per-account nonce lease. This MUST happen even on
        # an ambiguous outcome: the lease guards the nonce STREAM, and
        # an ambiguous batch may have consumed its nonces. Holding it
        # longer would needlessly block the account.
        try:
            lease_cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # ONE authoritative reconciliation pass.
    #
    # Normal path: a bounded read-after-write pass after the last batch
    # has been submitted. The HTTP request budget for the entire
    # 200-child ladder is then:
    #   preflight + token-mint + nonce-fetch    =  3 requests
    #   20 sendTxBatch writes                    = 20 requests
    #   N accountActiveOrders reads              =  N requests
    #                                            = 23 + N over ~60s
    # which sits comfortably below the observed 40/60s L1 cap. N is 1
    # in the common case (the indexer keeps up) and grows only while
    # children are still unconfirmed, under a 10 s deadline.
    #
    # Abnormal path: if the loop exited early because of a 429 /
    # non-200 envelope / transport exception / parse error / ambiguous
    # outcome, we still run the SAME read-after-write pass over every
    # client_order_index submitted so far. A rate limit tells us the
    # batch did not land, but a stale snapshot tells us NOTHING, and
    # the cleanup must cancel every child that actually landed. We
    # never reconcile per-batch — the cadence is "one final
    # read-after-write pass either way".
    # ------------------------------------------------------------------
    successful_batch_records = [
        rec for rec in batch_records if rec.get("ok")
    ]
    all_submitted_client_order_indices: List[int] = []
    for rec in successful_batch_records:
        all_submitted_client_order_indices.extend(rec.get("client_order_indices") or [])
    # Phase 5.1: children the exchange explicitly REJECTED provably
    # did not land, so they are excluded from the expected set. They
    # would otherwise make the read-after-write pass wait out its full
    # deadline and report a false "pending".
    api_rejected_client_order_indices: List[int] = []
    for rec in batch_records:
        api_rejected_client_order_indices.extend(
            rec.get("api_rejected_client_order_indices") or []
        )
    reconcile_expected = [
        int(idx)
        for idx in all_submitted_client_order_indices
        if int(idx) not in set(api_rejected_client_order_indices)
    ]

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
            # Bounded read-after-write reconciliation (Phase 5.1).
            #
            # The accountActiveOrders indexer can lag an accepted batch
            # by a few hundred milliseconds, so a single read taken
            # right after the ack can return ZERO matches for children
            # the exchange already accepted. The reconciler therefore
            # takes fresh read-only snapshots under a 10 s deadline and
            # ACCUMULATES matches across them.
            #
            # When a stop_reason already exists we still run the full
            # read-after-write pass: a rate-limit or rejection tells us
            # the batch did not land, but a stale snapshot tells us
            # NOTHING, and the cleanup must cancel every child that
            # actually landed. This is purely read-only — no side
            # effects on the exchange state, and never a resend.
            landed_all: Dict[int, int] = {}
            reconciliation_error = None
            try:
                landed_all = _reconcile_batch_by_client_order_index(
                    credentials=credentials,
                    market_id=market_id_int,
                    side=requested_side,
                    expected_client_order_indices=reconcile_expected,
                    auth_token=reconcile_token,
                )
                reconciliation_error = None
            except Exception as exc:  # noqa: BLE001
                landed_all = {}
                reconciliation_error = exc
            reconcile_stats = dict(_LIGHTER_LAST_RECONCILE_STATS)
            if reconcile_stats.get("unresolved"):
                # Unresolved children are preserved for the caller and
                # stop further placement. They are NOT converted to
                # accepted just because the envelope returned 200.
                unresolved_indices = list(reconcile_stats["unresolved"])
                stop_reason = stop_reason or {
                    "code": "LADDER_RECONCILIATION_PENDING",
                    "outcome": _LIGHTER_RECONCILE_OUTCOME_PENDING,
                    "message": (
                        f"{len(unresolved_indices)} of "
                        f"{reconcile_stats.get('expected')} children unconfirmed "
                        f"after {reconcile_stats.get('attempts')} reads; "
                        f"stopping for reconciliation"
                    ),
                    "raw_code": None,
                }
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
                # Children this batch expected but which NO snapshot
                # confirmed. They are preserved explicitly: the caller
                # must stop placing and must include them in cleanup
                # discovery, and they must NOT be counted as accepted
                # merely because the envelope returned 200.
                #
                # Children the exchange explicitly REJECTED are not
                # "unresolved" — they provably did not land.
                rejected_here = set(rec.get("api_rejected_client_order_indices") or [])
                rec["unresolved_client_order_indices"] = [
                    int(ci) for ci in expected
                    if int(ci) not in landed_all and int(ci) not in rejected_here
                ]
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
        elif canonical_code_raw == "LADDER_RECONCILIATION_PENDING":
            # The exchange ACCEPTED the batch (envelope 200) but the
            # read-after-write pass could not confirm every child
            # within its deadline. This is NOT an exchange rejection
            # and NOT an ambiguity about the write — it is a pending
            # reconciliation. Unresolved children are preserved and
            # placement stops; they are never counted as accepted.
            status = "partial" if accepted_child_count > 0 else "failed"
            canonical_code = "LADDER_RECONCILIATION_PENDING"
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

    submitted_batch_children = sum(int(rec.get("submitted") or 0) for rec in batch_records)
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
        rejected_child_count=rejected_child_count,
        uncertain_child_count=uncertain_child_count,
        not_attempted_child_count=max(0, requested_count - submitted_batch_children),
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
            # Per-account nonce lease (Phase 3 safety review). This path
            # allocates a nonce from the SDK's per-API-key counter, so
            # it must not run alongside another nonce-allocating
            # operation on the same account.
            with _lighter_account_lease(
                credentials, int(credentials["api_key_index"])
            ):
                return await _run_submit_locked(signer)
        finally:
            api_client = getattr(signer, "api_client", None)
            if api_client is not None and hasattr(api_client, "close"):
                try:
                    await api_client.close()
                except Exception:
                    pass

    async def _run_submit_locked(signer: Any) -> Dict[str, Any]:
        try:
            # L2CreateOrder budget check. This is shared with the
            # ladder path. _submit_tpsl_order has its own transaction
            # type (TpSlOrder) and is NOT throttled here.
            policy = _get_lighter_tier_policy(credentials)
            budget = _get_lighter_tier_budget(credentials, policy, kind="create")
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
        # Optional deterministic client identity supplied by the caller.
        # When present, it is honored verbatim; when
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


def _order_protection_kind(order: Dict[str, Any]) -> Optional[str]:
    """Return 'tp' / 'sl' from Lighter order type metadata, else None."""
    order_type = str(order.get("type") or "").strip().lower()
    if order_type in {"take-profit", "take-profit-limit"}:
        return "tp"
    if order_type in {"stop-loss", "stop-loss-limit"}:
        return "sl"
    return None


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
        kind = _order_protection_kind(order)
        if kind == "tp":
            bucket["tp"].append(order)
        elif kind == "sl":
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
    """Group open orders by (symbol, side, classification).

    Lighter TP/SL use ``type`` of ``take-profit`` / ``stop-loss`` (and
    *-limit variants). Those must not collapse into ordinary LIMIT ladders.
    """
    grouped: Dict[tuple[str, str, str], Dict[str, Any]] = {}
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
        # Prefer trigger_price for protection; fall back to limit price.
        kind = _order_protection_kind(order)
        if kind == "tp":
            classification = "take_profit"
        elif kind == "sl":
            classification = "stop_loss"
        elif bool(order.get("reduce_only")) and _decimal_or_none(order.get("trigger_price")) is not None:
            classification = "trigger"
        else:
            classification = "entry_limit"
        price = _decimal_or_none(order.get("trigger_price")) if classification != "entry_limit" else None
        if price is None:
            price = _decimal_or_none(order.get("price")) or Decimal("0")
        if price <= 0 and classification == "entry_limit":
            continue
        precision_value = market_info.get("price_precision")
        try:
            price_precision = int(precision_value) if precision_value is not None else _decimal_places(order.get("price") or order.get("trigger_price"))
        except Exception:  # noqa: BLE001
            price_precision = _decimal_places(order.get("price") or order.get("trigger_price"))
        key = (symbol, side, classification)
        group = grouped.setdefault(
            key,
            {
                "symbol": symbol,
                "side": side,
                "classification": classification,
                "order_count": 0,
                "total_size": Decimal("0"),
                "notional": Decimal("0"),
                "min_price": None,
                "max_price": None,
                "price_precision": price_precision,
                "order_ids": [],
                "reduce_only": classification != "entry_limit",
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
        oid = order.get("order_index") or order.get("order_id")
        try:
            if oid is not None:
                group["order_ids"].append(int(oid))
        except Exception:  # noqa: BLE001
            pass

    display_map = {
        "take_profit": "TAKE PROFIT",
        "stop_loss": "STOP LOSS",
        "trigger": "TRIGGER",
    }
    rows: List[CanonicalOrderGroup] = []
    for group in grouped.values():
        total_size: Decimal = group["total_size"]
        vwap = (group["notional"] / total_size) if total_size != 0 else Decimal("0")
        classification = group["classification"]
        side_u = str(group["side"]).upper()
        display_type = display_map.get(classification) or f"{side_u} LIMIT"
        min_p = group["min_price"]
        max_p = group["max_price"]
        rows.append(
            CanonicalOrderGroup(
                symbol=group["symbol"],
                side=group["side"],
                order_count=int(group["order_count"]),
                total_size=_decimal_text(total_size),
                vwap=_format_decimal_places(vwap, int(group.get("price_precision") or 0)),
                min_price=_decimal_text(min_p) if min_p is not None else "",
                max_price=_decimal_text(max_p) if max_p is not None else "",
                classification=classification,
                display_type=display_type,
                reduce_only=bool(group.get("reduce_only")),
                trigger_price=(
                    _decimal_text(min_p)
                    if classification in {"take_profit", "stop_loss", "trigger"} and min_p is not None
                    else None
                ),
                order_ids=list(group["order_ids"]) or None,
            )
        )
    rows.sort(key=lambda item: (item.symbol, item.classification, item.side))
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
    positions = _augment_positions_with_protection(
        positions,
        target=target,
        active_orders=active_orders,
        market_map=market_map,
    )
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
    raw_size = _decimal_or_none(current_raw.get("position")) or Decimal("0")
    current_size = _quantize_down(abs(raw_size), size_decimals)
    # Defensive fallback if size came back as 0 but request has it
    if current_size <= 0:
        req_size = request.get("size") or request.get("position_size")
        if req_size:
            try:
                fb = _quantize_down(abs(Decimal(str(req_size))), size_decimals)
                if fb > 0:
                    current_size = fb
            except Exception:
                pass
    auth_token = str(fetched.get("auth_token") or "")
    active_orders = _fetch_active_orders(credentials, auth_token) if include_active_orders else []
    return fetched, credentials, target, market, active_orders, current_side, current_size, closing_side, auth_token


def _submit_tpsl_order(credentials: Dict[str, Any], market: Dict[str, Any], *, operation: str, current_size: Decimal, closing_side: str, trigger_price: Decimal) -> Dict[str, Any]:
    async def _run_submit() -> Dict[str, Any]:
        signer = _build_signer_client(credentials)
        try:
            # Per-account nonce lease (Phase 3 safety review): this path
            # allocates a nonce from the SDK's per-API-key counter.
            with _lighter_account_lease(
                credentials, int(credentials["api_key_index"])
            ):
                return await _run_submit_locked(signer)
        finally:
            api_client = getattr(signer, "api_client", None)
            if api_client is not None and hasattr(api_client, "close"):
                try:
                    await api_client.close()
                except Exception:
                    pass

    async def _run_submit_locked(signer: Any) -> Dict[str, Any]:
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
            # Per-account nonce lease (Phase 3 safety review): this path
            # allocates a nonce from the SDK's per-API-key counter.
            with _lighter_account_lease(
                credentials, int(credentials["api_key_index"])
            ):
                return await _run_submit_locked(signer)
        finally:
            api_client = getattr(signer, "api_client", None)
            if api_client is not None and hasattr(api_client, "close"):
                try:
                    await api_client.close()
                except Exception:
                    pass

    async def _run_submit_locked(signer: Any) -> Dict[str, Any]:
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
    # Optional caller-supplied client_order_index.
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
# Generic single-order ops required by strategy adapters
#
# These are exchange-agnostic surfaces that any compliant perpetual-Dex
# adapter must expose for order-state-driven strategies to be implementable.
# ---------------------------------------------------------------------------

# Status taxonomy for the get_order_state op. Strategies use these
# to decide whether to advance, hold (active), freeze (terminal failure),
# or clear (filled and position flat).
_LIGHTER_ORDER_STATUS_ACTIVE = "ACTIVE"
_LIGHTER_ORDER_STATUS_FILLED = "FILLED"
_LIGHTER_ORDER_STATUS_CANCELED = "CANCELED"
_LIGHTER_ORDER_STATUS_REJECTED = "REJECTED"
_LIGHTER_ORDER_STATUS_EXPIRED = "EXPIRED"
_LIGHTER_ORDER_STATUS_UNKNOWN = "UNKNOWN"


# State literals we use to interpret the venue's `status` field and
# transform it into the canonical taxonomy. The mapping is conservative:
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
    """Map a Lighter Order object to the canonical status taxonomy.

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
# Phase 2.4: public mainnet URL used as the default for the
# unauthenticated catalog endpoint when no configured account is
# available to derive ``base_url`` from.
_LIGHTER_DEFAULT_BASE_URL = _LIGHTER_URL_ARBITRUM


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

    Returns min_base_amount, min_quote_amount,
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


def _normalize_lighter_market(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a single Lighter order_book_details entry to the
    Fibo common schema.

    Only ``instrument`` is required; the rest may be ``None`` if
    the upstream payload doesn't supply them. The Lighter catalog
    payload typically carries ``symbol`` (e.g. ``ETH``, ``BTC``)
    and ``market_id``. We preserve those plus any descriptive
    fields that are useful for the wizard's UI.
    """
    if not isinstance(entry, dict):
        return {"instrument": ""}
    instrument = (
        entry.get("symbol")
        or entry.get("market_symbol")
        or entry.get("market")
    )
    if not isinstance(instrument, str) or not instrument:
        return {"instrument": ""}
    out: Dict[str, Any] = {"instrument": instrument}
    # Optional descriptive fields the Lighter API may carry.
    desc = (
        entry.get("description")
        or entry.get("name")
        or entry.get("long_name")
    )
    if isinstance(desc, str) and desc:
        out["description"] = desc
    # Quote / base guess from symbol suffix (Lighter symbols like
    # ``ETH`` are pure base; perps use ``-PERP`` etc.).
    sym = instrument.upper()
    for suffix in ("-PERP", "-USD", "-USDT", "-USDC"):
        if sym.endswith(suffix):
            out["base"] = sym[: -len(suffix)] or None
            out["quote"] = suffix.lstrip("-")
            break
    if "base" not in out:
        out["base"] = sym
    # Market type — the caller tags each record with
    # ``market_type`` before passing it here.
    market_type = entry.get("market_type")
    if isinstance(market_type, str) and market_type:
        out["market_type"] = market_type
    display_name = entry.get("display_name") or entry.get("name")
    if isinstance(display_name, str) and display_name:
        out["display_name"] = display_name
    # Optional price (if a previous catalog pass bundled it).
    raw_price = entry.get("last_trade_price") or entry.get("mark_price")
    if raw_price is not None:
        try:
            from decimal import Decimal as _D
            d = _D(str(raw_price))
            if d.is_finite():
                out["price"] = format(d.normalize(), "f")
        except Exception:  # noqa: BLE001
            pass
    return out


def _execute_list_instruments(
    request: Dict[str, Any]
) -> CanonicalResponse:
    """Phase 2.4: read-only catalog enumeration for Lighter.

    Returns the normalized Fibo schema (see
    ``plugins/trade/fibo/discovery.py``) inside
    ``data["instruments"]``. Each record carries at minimum the
    venue-native ``instrument`` id.
    """
    account = str(request.get("account") or "").strip()
    if not account:
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account="",
            code="MISSING_ACCOUNT",
            message="Account is required.",
        )
    # Lighter's public catalog endpoint is unauthenticated — we
    # only need a base_url. Use the configured account's URL when
    # available, otherwise fall back to the public mainnet.
    credentials = _lookup_credentials(account)
    if credentials is not None:
        base_url = credentials["base_url"]
    else:
        base_url = _LIGHTER_DEFAULT_BASE_URL
    try:
        catalog = _fetch_market_catalog(base_url)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account=account,
            code="CATALOG_UNAVAILABLE",
            message=sanitize_error_message(str(exc)),
        )
    if not isinstance(catalog, list):
        return make_failure(
            operation="list_instruments",
            exchange=name,
            account=account,
            code="CATALOG_UNAVAILABLE",
            message="Lighter catalog returned an unexpected payload shape.",
        )
    # Re-fetch deterministically to know perp vs spot per symbol.
    perp_symbols = set()
    spot_symbols = set()
    try:
        perp_items = _fetch_lighter_market_catalog_raw(
            base_url, "order_book_details",
        )
        for it in perp_items:
            sym = it.get("symbol") if isinstance(it, dict) else None
            if isinstance(sym, str):
                perp_symbols.add(sym)
        spot_items = _fetch_lighter_market_catalog_raw(
            base_url, "spot_order_book_details",
        )
        for it in spot_items:
            sym = it.get("symbol") if isinstance(it, dict) else None
            if isinstance(sym, str):
                spot_symbols.add(sym)
    except Exception:  # noqa: BLE001
        pass
    instruments: List[Dict[str, Any]] = []
    for entry in catalog:
        if not isinstance(entry, dict):
            continue
        sym = entry.get("symbol")
        if not isinstance(sym, str):
            continue
        tagged = dict(entry)
        if sym in spot_symbols and sym not in perp_symbols:
            tagged["market_type"] = "spot"
        elif sym in perp_symbols and sym not in spot_symbols:
            tagged["market_type"] = "perp"
        else:
            tagged.setdefault("market_type", "perp")
        norm = _normalize_lighter_market(tagged)
        if norm.get("instrument"):
            instruments.append(norm)
    return make_success(
        operation="list_instruments",
        exchange=name,
        account=account,
        data={"instruments": instruments},
    )


def _fetch_lighter_market_catalog_raw(
    base_url: str, key: str
) -> List[Dict[str, Any]]:
    """Fetch only one of ``order_book_details`` /
    ``spot_order_book_details`` from Lighter. Used internally by
    ``_execute_list_instruments`` to tag each entry with perp vs
    spot. Returns ``[]`` on any error."""
    try:
        limiter = _get_lighter_limiter(
            {"chain": _URL_TO_CHAIN.get(base_url, ""), "account_index": 0}
        )
        limiter.acquire()
        response = requests.get(
            f"{base_url}/api/v1/orderBookDetails",
            headers={"Accept": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
    except Exception:  # noqa: BLE001
        return []
    payload = response.json() or {}
    items = payload.get(key) or []
    return [it for it in items if isinstance(it, dict)]


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

    Searches active orders first, then inactive history, and returns the
    normalized record.
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
    the canonical taxonomy.
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

    Cancels an orphan pending limit when a shared TP closes the
    position out from under it. Verifies by re-reading the active
    orders list.
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
    if operation not in {"balance", "positions_orders", "positions_management", "set_tp", "set_sl", "close_position", "new_order", "ladder", "cancel_order_group", "resolve_instrument", "list_instruments", "market_price", "candles", "position_state", "get_order_state", "get_order_state_by_client_id", "market_constraints", "cancel_order", "get_tickers"}:
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
        if operation == "list_instruments":
            return _execute_list_instruments(request)
        if operation == "get_tickers":
            return _execute_get_tickers(request)
        if operation == "market_price":
            return _execute_market_price(request)
        if operation == "candles":
            return handle_candles_operation(name, account, request)
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


# ---------------------------------------------------------------------------
# Canonical ``get_tickers`` (Batch 3)
# ---------------------------------------------------------------------------
#
# Lighter's existing ``_fetch_market_catalog`` returns the merged
# ``order_book_details`` + ``spot_order_book_details`` payload — one
# bulk HTTP call. Crucially, every entry already carries the venue-native
# ``market_type`` ("perp" or "spot"), so we do NOT need the current
# ``list_instruments`` re-fetch dance (which does a second + third call
# to tag entries by symbol membership). ``get_tickers`` reads the
# ``market_type`` field directly from each row, preserving spot/perp
# identity without per-symbol fan-out.
#
# Dynamic data lives in the same payload: ``mark_price`` / ``index_price``
# / ``last_trade_price`` (real mark + index + last), ``daily_*_volume``
# (24h quote and base), ``daily_price_change`` (24h change percent,
# already a percentage on the venue), and ``open_interest``. Lighter
# does NOT publish a per-row current funding rate — ``funding_*`` fields
# are config (clamp/multiplier). So ``funding_rate`` stays ``None``.
# Unavailable / zero / blank values are mapped to ``None`` rather than
# left as the string "0" so WebTrade2 renders "—".


def _lighter_first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and str(value).strip() != "":
            return value
    return None


def _lighter_decimal_or_none(value: Any) -> Optional["Decimal"]:
    from decimal import Decimal as _Decimal
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return _Decimal(text)
    except Exception:  # noqa: BLE001
        return None


def _lighter_decimal_text(value: Any) -> Optional[str]:
    decimal_value = _lighter_decimal_or_none(value)
    if decimal_value is None or decimal_value <= 0:
        return None
    rendered = format(decimal_value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".") or "0"
    return rendered


def _lighter_filter_symbols(symbols: Any) -> List[str]:
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


def _lighter_market_type_from_entry(entry: Dict[str, Any]) -> Optional[str]:
    """Read ``market_type`` from a single catalog row verbatim.

    Lighter's payload carries the venue-native value ("perp" or "spot");
    we never infer it from symbol membership — that would collapse
    spot/perp rows that share the same base symbol.
    """
    value = entry.get("market_type")
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in ("perp", "perpetual"):
            return "perp"
        if cleaned in ("spot",):
            return "spot"
    return None


def _execute_get_tickers(request: Dict[str, Any]) -> CanonicalResponse:
    """Bulk canonical ticker snapshot for Lighter.

    Single upstream call: ``/api/v1/orderBookDetails`` (already used by
    ``_execute_list_instruments`` and ``_resolve_market``). All per-row
    mark/index/last price, 24h quote & base volume, 24h change, open
    interest, and spot/perp identity come from the same response — no
    per-symbol fan-out, no extra fetch.

    Lighter does not publish per-row funding rate, so ``funding_rate``
    stays ``None`` per the brief.
    """
    account = str(request.get("account") or "").strip()
    credentials = _lookup_credentials(account) if account else None
    if credentials is not None:
        base_url = credentials["base_url"]
    else:
        base_url = _LIGHTER_DEFAULT_BASE_URL
    try:
        catalog = _fetch_market_catalog(base_url)
    except Exception as exc:  # noqa: BLE001
        return make_failure(
            operation="get_tickers",
            exchange=name,
            account=account,
            code="MARKETS_READ_FAILED",
            message=sanitize_error_message(str(exc)),
        )
    if not isinstance(catalog, list):
        return make_failure(
            operation="get_tickers",
            exchange=name,
            account=account,
            code="MARKETS_READ_FAILED",
            message="Lighter catalog returned an unexpected payload shape.",
        )
    filters = _lighter_filter_symbols(request.get("symbols"))
    tickers: Dict[str, CanonicalMarketPrice] = {}
    for entry in catalog:
        if not isinstance(entry, dict):
            continue
        symbol = str(entry.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        if filters and symbol not in filters:
            continue
        market_type = _lighter_market_type_from_entry(entry)
        # Quote / base: Lighter uses bare-base symbols for perps
        # ("ETH", "BTC") and "BASE/QUOTE" form for spot ("UNI/USDC").
        # Preserve symbol verbatim as ``base`` / ``display_symbol``.
        if market_type == "spot" and "/" in symbol:
            base_part, _, quote_part = symbol.partition("/")
            base = base_part.strip().upper() or None
            quote = quote_part.strip().upper() or None
        else:
            base = symbol
            quote = "USD" if market_type == "perp" else None
        display_name = symbol
        display_symbol = base or symbol
        # Per-row dynamic data. Zero / blank → None (no fake 0).
        mark = _lighter_decimal_text(_lighter_first_present(
            entry.get("mark_price"),
            entry.get("markPrice"),
        ))
        oracle = _lighter_decimal_text(_lighter_first_present(
            entry.get("index_price"),
            entry.get("indexPrice"),
        ))
        last_external = _lighter_decimal_text(_lighter_first_present(
            entry.get("last_trade_price"),
            entry.get("lastTradePrice"),
        ))
        # Lighter aliases:
        #   daily_quote_token_volume -> turnover_24h / volume_24h_quote
        #   daily_base_token_volume  -> volume_24h_base
        #   daily_price_change       -> change_24h_pct
        turnover = _lighter_decimal_text(entry.get("daily_quote_token_volume"))
        quote_volume = _lighter_decimal_text(entry.get("daily_quote_token_volume"))
        base_volume = _lighter_decimal_text(entry.get("daily_base_token_volume"))
        change_24h = _lighter_decimal_text(entry.get("daily_price_change"))
        open_interest = _lighter_decimal_text(entry.get("open_interest"))
        # Lighter publishes size_decimals / price_decimals, not ticks. We
        # do not fabricate a tick string from decimals here; ``None`` is
        # safer than a wrong-derived step. ``minimum_size`` and
        # ``minimum_notional`` come from ``min_base_amount`` and
        # ``min_quote_amount`` respectively — preserve as text without
        # numeric coercion so a fixture like "0.0001" survives verbatim.
        def _tick_text(value: Any) -> Optional[str]:
            d = _lighter_decimal_or_none(value)
            if d is None or d <= 0:
                return None
            return format(d.normalize(), "f")
        tickers[symbol] = CanonicalMarketPrice(
            requested_symbol=symbol,
            market=str(entry.get("market_id") or symbol),
            symbol=symbol,
            native_symbol=symbol,
            display_symbol=display_symbol,
            display_name=display_name,
            base=base,
            quote=quote,
            market_type=market_type,
            mark_price=mark,
            price=mark,
            oracle_price=oracle,
            last_external_price=last_external,
            turnover_24h=turnover,
            volume_24h_quote=quote_volume,
            volume_24h_base=base_volume,
            change_24h_pct=change_24h,
            open_interest=open_interest,
            minimum_size=_lighter_decimal_text(entry.get("min_base_amount")),
            minimum_notional=_lighter_decimal_text(entry.get("min_quote_amount")),
        )
    return make_success(
        operation="get_tickers",
        exchange=name,
        account=account,
        tickers_batch=CanonicalTickersBatch(
            tickers=tickers,
            source="lighter_order_book_details",
            refresh_status="ok",
        ),
    )
