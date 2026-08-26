"""Per-user Start Fibo wizard session state.

Spec §6: state must be scoped by ``(chat_id, user_id)``, never global.
TTL = 15 minutes of inactivity. Sessions are cleared on:

* successful Create
* Cancel
* Exit (the existing ``fibo:exit`` button)
* expiry (lazy on next access)

Sessions are NEVER persisted to disk.

Public surface (consumed by ``flow.py``):

    FiboSession               # frozen dataclass; one in-flight session
    FiboSessionStore          # dict container + TTL sweep + reset

The wizard's ``handle_fibo_text`` returns ``True`` only when the
caller's session is specifically awaiting volume input.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# 15 minutes per spec §6.
SESSION_TTL_SECONDS = 15 * 60


# ---------------------------------------------------------------------------
# Session states
# ---------------------------------------------------------------------------


class SessionState(str, Enum):
    """Where the wizard currently is in the Start Fibo flow.

    Stored on the session itself so a callback/text handler can route
    without consulting a global state machine.

    Phase 2.2 instrument-translation states (spec §12):

    * ``AWAITING_INSTRUMENT_CONFIRM`` — the agent-resolved venue
      contract has been proposed to the user; the user must
      Agree (or Other / Browse) before continuing.
    * ``AWAITING_EXCHANGE_ALIAS`` — the user tapped "Other" and
      is typing a free-form alias (e.g. ``US500``) for the
      exchange agent to resolve.
    * ``AWAITING_MARKET_BROWSE`` — the user tapped "Browse markets"
      and is paging through the read-only market list.
    """

    AWAITING_SYMBOL = "awaiting_symbol"
    AWAITING_SIDE = "awaiting_side"
    AWAITING_EXCHANGE = "awaiting_exchange"
    AWAITING_ACCOUNT = "awaiting_account"
    AWAITING_INSTRUMENT = "awaiting_instrument"
    AWAITING_INSTRUMENT_CONFIRM = "awaiting_instrument_confirm"
    AWAITING_EXCHANGE_ALIAS = "awaiting_exchange_alias"
    AWAITING_MARKET_BROWSE = "awaiting_market_browse"
    AWAITING_VOLUME = "awaiting_volume"
    AWAITING_CONFIRM = "awaiting_confirm"


# Whitelist of states where free-text input is consumed.
# Matches spec §6 / §12:
#   * AWAITING_VOLUME — the user types their starting volume.
#   * AWAITING_EXCHANGE_ALIAS — the user types an exchange-side
#     alias they believe corresponds to the MT4 source symbol.
TEXT_INTERCEPT_STATES = frozenset({
    SessionState.AWAITING_VOLUME,
    SessionState.AWAITING_EXCHANGE_ALIAS,
})


# ---------------------------------------------------------------------------
# Session dataclass
# ---------------------------------------------------------------------------


def _session_key(chat_id: Any, user_id: Any) -> Tuple[str, str]:
    """Normalize a (chat_id, user_id) pair into the internal key.

    Returns a 2-tuple of stripped strings. Empty components are
    rejected — the caller should ensure both IDs are present.
    """
    c = str(chat_id or "").strip()
    u = str(user_id or "").strip()
    if not c or not u:
        raise ValueError(
            f"session key requires chat_id and user_id; got {chat_id!r}, {user_id!r}"
        )
    return (c, u)


@dataclass
class FiboSession:
    """One in-progress Start Fibo wizard session.

    Mutable because we update ``last_accessed_at`` on every read and
    accumulate choices as the user advances through the wizard.
    """

    chat_id: str
    user_id: str
    state: SessionState = SessionState.AWAITING_SYMBOL
    created_at: float = field(default_factory=time.monotonic)
    last_accessed_at: float = field(default_factory=time.monotonic)

    # Wizard data ----------------------------------------------------
    # choices for the symbol+variant screen
    choices_symbols: list = field(default_factory=list)        # [{symbol, variant}, ...]
    symbol: Optional[str] = None
    variant: Optional[str] = None
    side: Optional[str] = None                                 # canonical BUY / SELL
    # choices for the exchange screen
    choices_exchanges: list = field(default_factory=list)      # [exchange_name, ...]
    exchange: Optional[str] = None
    # choices for the account screen
    choices_accounts: list = field(default_factory=list)       # [account_alias, ...]
    account: Optional[str] = None
    # choices for the venue-instrument screen (Phase 2.1)
    choices_instruments: list = field(default_factory=list)     # [instrument, ...]
    # Phase 2.2: instrument-translation state (spec §12)
    resolution_input: Optional[str] = None   # the string the agent
                                             # was last asked to
                                             # resolve (source_symbol
                                             # or a user-supplied
                                             # alias)
    # Canonical venue contract id. Set ONLY after user Agree.
    # Alias memory MUST NOT bypass this gate.
    exchange_instrument: Optional[str] = None
    # Market-browse pagination (spec §5).
    instrument_page: int = 0
    # Set to a transient string ("alias", "browse") inside specific
    # states for downstream handler dispatch. Most state lives in
    # ``state``; this is a sub-mode tag.
    awaiting: Optional[str] = None
    # Phase 2.3: ranked candidate list for the candidate-picker
    # screen (spec §3). Each entry is an ``InstrumentCandidate``
    # dataclass (see candidates.py). Callback tokens reference
    # ``sess.candidates[i].instrument`` so the flow never embeds
    # the raw venue contract string in callback_data.
    candidates: list = field(default_factory=list)             # [InstrumentCandidate, ...]
    # The user-selected candidate's canonical venue contract id.
    # Set ONLY after the candidate is revalidated through the
    # exchange agent; never trusted from raw button payload.
    selected_candidate_canonical: Optional[str] = None
    starting_volume: Optional[Decimal] = None
    # Snapshot metadata captured at confirmation time so Create can
    # re-validate against the latest snapshot atomically.
    snap_source: Optional[str] = None
    snap_seq: Optional[int] = None
    snap_cycle_id: Optional[int] = None
    snap_cumulative_weight: Optional[Decimal] = None
    snap_percentage: Optional[Decimal] = None
    snap_received_at: Optional[str] = None

    @property
    def session_key(self) -> Tuple[str, str]:
        return (self.chat_id, self.user_id)

    def touch(self) -> None:
        """Mark the session as accessed now. Call on every operation."""
        self.last_accessed_at = time.monotonic()

    def is_expired(self, *, ttl_seconds: float = SESSION_TTL_SECONDS) -> bool:
        return (time.monotonic() - self.last_accessed_at) > ttl_seconds

    def is_awaiting_volume(self) -> bool:
        return self.state in TEXT_INTERCEPT_STATES


# ---------------------------------------------------------------------------
# Session container
# ---------------------------------------------------------------------------


class FiboSessionStore:
    """Per-user session container with lazy TTL expiry.

    Spec §6: cleanup belongs only to ``FiboSessionStore`` (and
    ``StartFiboFlow``), NOT to the MT4 Reader.
    """

    def __init__(self, *, ttl_seconds: float = SESSION_TTL_SECONDS) -> None:
        self._sessions: Dict[Tuple[str, str], FiboSession] = {}
        self._ttl_seconds = float(ttl_seconds)

    # -- lookups ------------------------------------------------------

    def get(
        self,
        chat_id: Any,
        user_id: Any,
    ) -> Optional[FiboSession]:
        """Return the live session for ``(chat_id, user_id)``, or None.

        Expired sessions are dropped lazily here. A ``get`` on an
        expired session returns None and removes the entry.
        """
        key = _session_key(chat_id, user_id)
        sess = self._sessions.get(key)
        if sess is None:
            return None
        if sess.is_expired(ttl_seconds=self._ttl_seconds):
            logger.info(
                "fibo_sessions: dropping expired session for chat=%s user=%s",
                sess.chat_id, sess.user_id,
            )
            self._sessions.pop(key, None)
            return None
        sess.touch()
        return sess

    def require(
        self,
        chat_id: Any,
        user_id: Any,
    ) -> Optional[FiboSession]:
        """Same as ``get`` but also expires the entire store first.

        ``require`` is used when the caller wants to guarantee the
        rest of the store is clean too (e.g. callback dispatch).
        """
        self._sweep_expired()
        return self.get(chat_id, user_id)

    # -- creation -----------------------------------------------------

    def create(
        self,
        chat_id: Any,
        user_id: Any,
    ) -> FiboSession:
        """Create a fresh session, replacing any existing one.

        The previous session (if any) is discarded — this matches the
        /trade wizard's "open" semantics.
        """
        key = _session_key(chat_id, user_id)
        # Sweep the whole store first so we don't accumulate stale
        # entries (spec §6 — cleanup belongs here).
        self._sweep_expired()
        sess = FiboSession(chat_id=key[0], user_id=key[1])
        self._sessions[key] = sess
        return sess

    # -- removal ------------------------------------------------------

    def reset(self, chat_id: Any, user_id: Any) -> None:
        """Drop the session for ``(chat_id, user_id)`` if present."""
        key = _session_key(chat_id, user_id)
        self._sessions.pop(key, None)

    def reset_all(self) -> None:
        self._sessions.clear()

    # -- maintenance --------------------------------------------------

    def _sweep_expired(self) -> None:
        """Drop every expired session."""
        if not self._sessions:
            return
        stale = [
            k for k, s in self._sessions.items()
            if s.is_expired(ttl_seconds=self._ttl_seconds)
        ]
        for k in stale:
            logger.info(
                "fibo_sessions: sweeping expired session for key=%s", k
            )
            self._sessions.pop(k, None)

    def __len__(self) -> int:
        return len(self._sessions)

    def keys(self):
        return tuple(self._sessions.keys())


__all__ = [
    "SESSION_TTL_SECONDS",
    "SessionState",
    "TEXT_INTERCEPT_STATES",
    "FiboSession",
    "FiboSessionStore",
]