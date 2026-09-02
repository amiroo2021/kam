"""Start Fibo wizard state machine + screen rendering.

Phase 1 implements ONLY the Start Fibo sub-flow. The entry menu's
other three buttons (Running / Stop / Exit) remain unchanged in
``plugins.trade.fibo_wizard``.

The flow:

    Start Fibo
     -> symbol + variant (from latest MT4 snapshot, unique pairs)
     -> BUY / SELL  (BUY uses buy fields, SELL uses sell fields)
        [inactive side = cycle_id <= 0 OR cumulative_weight <= 0
         blocks Continue — only the active side(s) are clickable]
     -> exchange  (TradeDesk.list_exchanges() — read-only)
     -> account   (TradeDesk.list_accounts(exchange) — read-only)
     -> starting volume  (text input; Decimal; > 0; preserved)
     -> confirmation   (full record layout + age + Refresh/Back/Cancel)
     -> Create  (atomic re-check of snapshot, persists registration)

Spec compliance:

* §4  Stale snapshot blocks Create. Confirmation shows age; when
       ``age > 30s`` Create is replaced with Refresh.
* §5  Compact callback_data (e.g. ``fibo:s:sym:0``). No symbol /
       variant / exchange / account / volume strings embedded in
       callbacks.
* §6  Per-user sessions, TTL = 15min, cleared on Create/Cancel/Exit/
       expiry. Text volume input consumed ONLY when the user's
       session is in AWAITING_VOLUME.
* §7  BUY/SELL use buy/sell fields. Inactive side → "no active MT4
       cycle" + Back.
* §8  Exchange/account discovery read-only. No exchange write.
* §9  Decimal parsing. ``> 0`` only. ``desired_exchange_size =
       starting_volume * cumulative_weight`` (preview only).
* §10 Identity normalized; persisted with required fields.
* §11 Duplicate registration REJECTED.
* §12 Pre-write re-check: feed freshness, source identity, (symbol,
       variant) presence, side cycle_id, side cumulative_weight
       changes. Cycle_id change -> re-confirm; weight change -> refresh
       target & re-confirm.
* §13 No execution engine.
* §14 All tests covered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .session import (
    FiboSession,
    FiboSessionStore,
    SESSION_TTL_SECONDS,
    SessionState,
    TEXT_INTERCEPT_STATES,
)
from .snapshot import (
    Mt4Snapshot,
    Mt4SnapshotStore,
    SIDE_BUY,
    SIDE_SELL,
)
from .store import (
    DuplicateRegistrationError,
    FiboRegistration,
    FiboRegistrationStore,
)
from .alias_memory import AliasMemory, alias_key

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# 30 seconds per spec §4.
STALE_THRESHOLD_SECONDS = 30.0

# Callback prefixes (all compact, well under 64 bytes).
CB_PREFIX = "fibo:s:"           # Start Fibo sub-flow callbacks
CB_SYM = "fibo:s:sym:"          # symbol+variant pick
CB_SIDE = "fibo:s:side:"        # side pick
CB_EX = "fibo:s:ex:"            # exchange pick
CB_ACCT = "fibo:s:acct:"        # account pick
CB_INST = "fibo:s:inst:"        # venue-instrument pick (Phase 2.1)
# Phase 2.2 — instrument-translation callbacks (spec §2-5).
# All prefixes are kept short so the indexed tokens stay under
# Telegram's 64-byte callback_data limit.
CB_AGREE = "fibo:s:agree"        # user accepts the proposed instrument
CB_OTHER = "fibo:s:other"        # user wants to enter an alias
CB_BROWSE = "fibo:s:browse"      # user wants to page through markets
CB_BROWSEPG = "fibo:s:browsepg:" # browse-pagination (negative=prev, 1..N=page)
CB_INSTSEL = "fibo:s:instsel:"   # market pick from Browse list
CB_CAND = "fibo:s:cand:"       # candidate pick (Phase 2.3)
CB_INSTFAIL_RETRY = "fibo:s:instrtry"  # "Try another" alias failure
CB_CREATE = "fibo:s:create"
CB_BACK = "fibo:s:back"
CB_CANCEL = "fibo:s:cancel"
CB_REFRESH = "fibo:s:refresh"
CB_VCONFIRM = "fibo:s:v"        # volume-confirmed ack (used after text input)

# Browse markets: page size for Telegram button rows.
_BROWSE_PAGE_SIZE = 10


def _catalog_instrument_id(entry: Any) -> str:
    """Venue-native id from a browse catalog entry (str or dict)."""
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        for key in ("instrument", "symbol", "market", "id"):
            val = entry.get(key)
            if val is None:
                continue
            text = str(val).strip()
            if text:
                return text
    return ""


def _catalog_short_symbol(instrument_id: str) -> str:
    """BTC-USD / BTC-USDT / ETH-USD.P → BTC / ETH."""
    text = str(instrument_id or "").strip()
    if not text:
        return "?"
    # Prefer base before quote separators.
    for sep in ("-", "/", "_"):
        if sep in text:
            base = text.split(sep, 1)[0].strip()
            if base:
                # strip trailing contract suffixes like .P already gone with split
                return base
    if "." in text:
        return text.split(".", 1)[0].strip() or text
    return text


def _catalog_display_symbol(entry: Any) -> str:
    """Button symbol: prefer base/display_name, else short instrument id."""
    if isinstance(entry, dict):
        for key in ("base", "display_name"):
            val = entry.get(key)
            if val is None:
                continue
            text = str(val).strip()
            if text:
                return text
    inst = _catalog_instrument_id(entry)
    return _catalog_short_symbol(inst) if inst else "?"


def _catalog_price_text(entry: Any) -> Optional[str]:
    """Optional pre-populated price string from a catalog record."""
    if not isinstance(entry, dict):
        return None
    raw = entry.get("price")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.lower() in {"none", "null"}:
        return None
    try:
        d = Decimal(text.replace(",", ""))
    except Exception:  # noqa: BLE001
        return text
    if not d.is_finite():
        return None
    rendered = format(d, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    try:
        if "." in rendered:
            whole, frac = rendered.split(".", 1)
            return f"{int(whole):,}.{frac}"
        return f"{int(rendered):,}"
    except Exception:  # noqa: BLE001
        return rendered


# Side button tokens (single char keeps callback short).
SIDE_TOKEN_BUY = "b"
SIDE_TOKEN_SELL = "s"


# ---------------------------------------------------------------------------
# Screen model (returned by every render step)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Screen:
    """Render-ready screen returned to the wizard caller.

    The caller (the fibo_wizard shim) translates this into the
    ``adapter.send_inline_keyboard`` call. The buttons list is a list
    of rows; each row is a list of ``{text, callback_data}`` dicts.
    """

    text: str
    buttons: List[List[Dict[str, str]]]
    # Optional flag for callers that want to suppress the keyboard
    # entirely (e.g. the closed confirmation after Create).
    no_keyboard: bool = False


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------


class StartFiboFlow:
    """State machine for the Start Fibo sub-flow.

    Holds the session store, snapshot store, and registration store.
    Reads MT4 data from disk only — never calls Telegram. Reads
    exchange/account info from TradeDesk which uses x_*_agent
    ``list_accounts()`` — pure env reads, no network.
    """

    def __init__(
        self,
        *,
        snapshot_store: Mt4SnapshotStore,
        registration_store: FiboRegistrationStore,
        list_exchanges_fn,
        list_accounts_fn,
        list_instruments_fn=None,
        resolve_instrument_fn=None,
        alias_memory=None,
        session_store: Optional[FiboSessionStore] = None,
        stale_threshold_seconds: float = STALE_THRESHOLD_SECONDS,
        now_fn=None,
    ) -> None:
        # ``list_exchanges_fn`` is a callable returning ``List[str]``.
        # ``list_accounts_fn(exchange: str) -> List[Any]``.
        # ``list_instruments_fn(exchange, account) -> List[str]``
        # (Phase 2.1, kept for the Browse fallback).
        # ``resolve_instrument_fn(exchange, account, symbol) -> str | None``
        # is the agent-validated translation path (Phase 2.2). It
        # must be the existing TradeDesk ``resolve_instrument``
        # operation (read-only by construction — see fibo_wizard).
        # ``alias_memory`` is an ``AliasMemory`` instance (Phase 2.2).
        # Both are optional so unit tests can run without them.
        self._snapshot_store = snapshot_store
        self._registration_store = registration_store
        self._list_exchanges = list_exchanges_fn
        self._list_accounts = list_accounts_fn
        self._list_instruments = list_instruments_fn
        self._resolve_instrument = resolve_instrument_fn
        self._alias_memory = alias_memory
        self._sessions = session_store or FiboSessionStore()
        self._stale_threshold = float(stale_threshold_seconds)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    # -- public properties -------------------------------------------

    @property
    def session_store(self) -> FiboSessionStore:
        return self._sessions

    @property
    def stale_threshold_seconds(self) -> float:
        return self._stale_threshold

    # -- public entry points ----------------------------------------

    def open(self, chat_id: Any, user_id: Any) -> Screen:
        """Enter the Start Fibo sub-flow (called by the wizard shim).

        Creates a fresh session. If the snapshot is missing or empty,
        renders a "no MT4 data yet" screen with a Cancel button.
        """
        key = self._validate_key(chat_id, user_id)
        sess = self._sessions.create(*key)
        snap = self._snapshot_store.load()
        if snap is None or not snap.fibos:
            return self._render_no_data(sess)
        sess.choices_symbols = snap.unique_symbol_variant_pairs()
        sess.state = SessionState.AWAITING_SYMBOL
        return self._render_symbols_screen(sess, snap)

    def handle_callback(
        self,
        chat_id: Any,
        user_id: Any,
        callback_data: str,
    ) -> Screen:
        """Advance the flow based on a ``fibo:s:*`` callback.

        Anything outside the ``fibo:s:`` prefix is ignored (returns a
        safe cancel-style screen so the caller can re-render the entry
        menu).
        """
        key = self._validate_key(chat_id, user_id)
        data = (callback_data or "").strip()
        if not data.startswith(CB_PREFIX):
            return self._render_invalid_callback(key)
        sess = self._sessions.get(*key)
        if sess is None:
            # Session expired or never opened — restart.
            return self.open(*key)
        sess.touch()
        snap = self._snapshot_store.load()

        # --- cancel & back ---------------------------------------
        if data == CB_CANCEL:
            self._sessions.reset(*key)
            return self._render_cancelled()

        if data == CB_BACK:
            return self._handle_back(sess, snap)

        # --- refresh ----------------------------------------------
        if data == CB_REFRESH:
            return self._render_confirmation(sess, snap or None)

        if data == CB_CREATE:
            return self._handle_create(sess, snap)

        # --- step picks -------------------------------------------
        if data.startswith(CB_SYM):
            return self._handle_symbol_pick(sess, snap, data)
        if data.startswith(CB_SIDE):
            return self._handle_side_pick(sess, snap, data)
        if data.startswith(CB_EX):
            return self._handle_exchange_pick(sess, snap, data)
        if data.startswith(CB_ACCT):
            return self._handle_account_pick(sess, snap, data)
        # Phase 2.2 dispatch (must come AFTER legacy CB_INST in case a
        # stale Phase 2.1 button is still around — the prefixes are
        # distinct so the order is cosmetic).
        if data == CB_AGREE:
            return self._handle_instrument_agree(sess, snap)
        if data == CB_OTHER:
            return self._handle_instrument_other(sess, snap)
        if data == CB_BROWSE:
            return self._handle_instrument_browse(sess, snap)
        if data.startswith(CB_BROWSEPG):
            return self._handle_browse_paginate(sess, snap, data)
        if data.startswith(CB_INSTSEL):
            return self._handle_browse_market_pick(sess, snap, data)
        if data.startswith(CB_CAND):
            return self._handle_candidate_pick(sess, snap, data)
        if data == CB_INSTFAIL_RETRY:
            return self._handle_instrument_other(sess, snap)
        if data.startswith(CB_INST):
            # Legacy Phase 2.1 direct-pick path. We keep it as a
            # fallback so old messages don't break — it routes
            # through the proposal screen (spec §5: "clicking a
            # market shows the translation approval screen").
            return self._handle_legacy_inst_pick(sess, snap, data)

        return self._render_invalid_callback(key)

    def handle_text(
        self,
        chat_id: Any,
        user_id: Any,
        text: str,
    ) -> Optional[Screen]:
        """Consume free-text input only when the user's session is
        in a whitelist state (spec §6, §12).

        Whitelisted states:
          * ``AWAITING_VOLUME`` — the user types their starting volume.
          * ``AWAITING_EXCHANGE_ALIAS`` — the user types an alias
            (e.g. ``US500``) for the agent to validate.

        All other states return ``None`` so the underlying adapter
        can route the message to its normal handler.

        Per-user isolation is enforced via the session store
        (``FiboSessionStore``), which is keyed by
        ``(chat_id, user_id)``. A user in ``AWAITING_VOLUME``
        cannot affect a different user's session, and vice versa.
        """
        try:
            key = self._validate_key(chat_id, user_id)
        except ValueError:
            return None
        sess = self._sessions.get(*key)
        if sess is None:
            return None
        raw = (text or "").strip()
        # Phase 2.2: alias-entry interception (spec §3).
        if sess.state == SessionState.AWAITING_EXCHANGE_ALIAS:
            return self._handle_alias_text(sess, raw)
        # Volume entry (spec §6).
        if sess.is_awaiting_volume():
            # Validation ---------------------------------------------
            try:
                d = Decimal(raw)
            except (InvalidOperation, ValueError):
                return self._render_volume_invalid(sess, reason="not_a_number")
            if not d.is_finite():
                return self._render_volume_invalid(sess, reason="not_finite")
            if d <= 0:
                return self._render_volume_invalid(sess, reason="not_positive")
            sess.starting_volume = d
            sess.touch()
            # Re-load the snapshot to capture its current state at
            # confirmation time. A stale or missing snapshot blocks Create
            # at the confirmation screen.
            snap = self._snapshot_store.load()
            if snap is None:
                return self._render_no_data(sess)
            sess.state = SessionState.AWAITING_CONFIRM
            self._capture_snapshot_metadata(sess, snap)
            return self._render_confirmation(sess, snap)
        # Any other state: do not intercept.
        return None

    def _handle_alias_text(
        self,
        sess: FiboSession,
        raw: str,
    ) -> Screen:
        """Phase 2.2 §3: the user typed an alias. Pass it through
        the SAME agent resolver as the source symbol. The agent
        MUST be the only authority for what becomes the canonical
        ``exchange_instrument``; we NEVER trust the raw user text
        as a venue contract id.

        Per-user isolation: this method is only reachable through
        ``handle_text`` which already gated on the session for the
        calling ``(chat_id, user_id)``.
        """
        raw = (raw or "").strip()
        if not raw:
            # Empty text: re-prompt.
            snap = self._snapshot_store.load()
            if snap is None:
                return self._render_no_data(sess)
            return self._render_alias_prompt(sess, snap)
        # Hard cap to keep callback_data sane (the typed text is
        # not stored as a callback; it just has to fit a single
        # line in the alias-prompt UI). The agent is the only
        # validator; we don't truncate here on purpose.
        snap = self._snapshot_store.load()
        if snap is None:
            return self._render_no_data(sess)
        sess.resolution_input = raw
        sess.proposal_origin = "alias"
        canonical = self._safe_resolve(
            sess.exchange or "", sess.account or "", raw,
        )
        if canonical is None:
            # Failure: stay in alias-entry flow (spec §3.B).
            sess.awaiting = "alias-failure"
            return self._render_alias_failure(sess, snap, raw)
        # Success: stage the proposal; user must still Agree.
        sess.awaiting = "proposal:" + canonical
        sess.state = SessionState.AWAITING_INSTRUMENT_CONFIRM
        return self._render_instrument_proposal(sess, snap)

    def reset(self, chat_id: Any, user_id: Any) -> None:
        """Drop the session (used by Exit, Cancel, Create, sweep)."""
        try:
            key = self._validate_key(chat_id, user_id)
        except ValueError:
            return
        self._sessions.reset(*key)

    # ------------------------------------------------------------------
    # Callback handlers
    # ------------------------------------------------------------------

    def _handle_symbol_pick(
        self,
        sess: FiboSession,
        snap: Optional[Mt4Snapshot],
        data: str,
    ) -> Screen:
        if snap is None:
            return self._render_no_data(sess)
        idx = self._parse_index(data, len(CB_SYM))
        if idx is None or idx >= len(sess.choices_symbols):
            return self._render_invalid_callback(sess.session_key)
        pick = sess.choices_symbols[idx]
        sess.symbol = pick["symbol"]
        sess.variant = pick["variant"]
        sess.state = SessionState.AWAITING_SIDE
        return self._render_side_screen(sess, snap)

    def _handle_side_pick(
        self,
        sess: FiboSession,
        snap: Optional[Mt4Snapshot],
        data: str,
    ) -> Screen:
        if snap is None:
            return self._render_no_data(sess)
        token = data[len(CB_SIDE):].strip().lower()
        if token == SIDE_TOKEN_BUY:
            side = SIDE_BUY
        elif token == SIDE_TOKEN_SELL:
            side = SIDE_SELL
        else:
            return self._render_invalid_callback(sess.session_key)
        fibo = snap.find_fibo(sess.symbol or "", sess.variant or "")
        if fibo is None or not fibo.is_side_active(side):
            return self._render_side_inactive(sess, snap, side)
        sess.side = side
        # Capture snapshot metadata NOW so the side's cycle_id and
        # cumulative_weight are pinned at the moment of the side pick,
        # ready for the pre-Create re-check.
        self._capture_snapshot_metadata(sess, snap)
        sess.choices_exchanges = sorted(self._safe_list_exchanges())
        sess.state = SessionState.AWAITING_EXCHANGE
        return self._render_exchange_screen(sess, snap)

    def _handle_exchange_pick(
        self,
        sess: FiboSession,
        snap: Optional[Mt4Snapshot],
        data: str,
    ) -> Screen:
        if snap is None:
            return self._render_no_data(sess)
        idx = self._parse_index(data, len(CB_EX))
        if idx is None or idx >= len(sess.choices_exchanges):
            return self._render_invalid_callback(sess.session_key)
        sess.exchange = sess.choices_exchanges[idx]
        try:
            raw_accounts = list(
                self._safe_list_accounts(str(sess.exchange))
            )
        except Exception:
            raw_accounts = []
        # Phase 2.4.1: an exchange's ``list_accounts()`` may return
        # either strings OR dicts (e.g. Lighter returns a list of
        # ``{'account': 'amiroo', 'chain': 'ARBITRUM', ...}``).
        # Normalize each to its canonical id; drop entries that
        # don't carry one. Sort for stable button indexing.
        sess.choices_accounts = sorted(
            aid for aid in (
                _account_id_for(entry) for entry in raw_accounts
            ) if aid is not None
        )
        if not sess.choices_accounts:
            # No accounts configured for this exchange; can't continue.
            return self._render_no_accounts(sess, snap)
        sess.state = SessionState.AWAITING_ACCOUNT
        return self._render_account_screen(sess, snap)

    def _handle_account_pick(
        self,
        sess: FiboSession,
        snap: Optional[Mt4Snapshot],
        data: str,
    ) -> Screen:
        """Phase 2.2: after account pick, kick off the
        agent-validated instrument translation flow.

        Order of operations (spec §7):

        1. Look up the alias-memory key for this (exchange,
           account, source_symbol). If present, call the agent to
           revalidate the stored ``exchange_instrument`` live.
        2. If revalidation succeeds, propose the cached mapping
           immediately (the user still must Agree).
        3. If alias memory misses OR revalidation fails OR the
           agent's response is empty, call
           ``resolve_instrument(source_symbol)`` fresh.
        4. If fresh resolution succeeds → render the proposal
           screen.
        5. If fresh resolution fails → render the "could not
           resolve" screen with Enter alias / Browse / Back / Cancel.

        The wizard MUST NOT auto-store ``exchange_instrument``
        here. The user must tap Agree.
        """
        if snap is None:
            return self._render_no_data(sess)
        idx = self._parse_index(data, len(CB_ACCT))
        if idx is None or idx >= len(sess.choices_accounts):
            return self._render_invalid_callback(sess.session_key)
        sess.account = sess.choices_accounts[idx]
        # Reset any prior instrument-translation state (this is
        # the gate between account pick and the proposal screen).
        sess.exchange_instrument = None
        sess.resolution_input = sess.symbol  # default input
        sess.proposal_origin = None  # set later by the proposal path
        sess.choices_instruments = []
        sess.instrument_page = 0
        sess.awaiting = None
        return self._propose_exchange_instrument(sess, snap)

    # ------------------------------------------------------------------
    # Phase 2.2: instrument-translation proposal + handlers
    # ------------------------------------------------------------------

    def _safe_resolve(
        self,
        exchange: str,
        account: str,
        symbol: str,
    ) -> Optional[str]:
        """Resolve ``symbol`` through the live exchange agent.

        Returns the canonical venue contract id (e.g.
        ``"ETH-USD.P"``) on success, ``None`` on failure or when
        no resolver is wired. NEVER raises — all exceptions are
        swallowed so a broken resolver never crashes the wizard.

        Resolution order (Phase 2.4):
        1. If a ``resolve_instrument_fn`` was supplied at
           construction time, use it (legacy path).
        2. Otherwise, dispatch through the public TradeDesk
           boundary (``fibo.discovery.list_market_catalog`` is
           NOT used here — this is a single-symbol resolve).
           Falls back to ``None`` if the desk rejects the call.
        """
        # 1. Legacy: injected resolver (used by older tests and
        # by callers that want a non-TradeDesk resolve path).
        if self._resolve_instrument is not None:
            try:
                result = self._resolve_instrument(
                    str(exchange), str(account), str(symbol)
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "fibo_flow: resolve_instrument(%r,%r,%r) raised: %s",
                    exchange, account, symbol, exc,
                )
                return None
            if not result:
                return None
            return str(result).strip() or None
        # 2. Public boundary: route through the same TradeDesk
        # singleton that ``discovery`` uses. This guarantees the
        # Fibo flow reads the same canonical contract id the
        # user would see from a live wizard session.
        try:
            from . import discovery as _discovery
            desk = _discovery._get_desk()
            resp = desk.execute({
                "operation": "resolve_instrument",
                "exchange": exchange,
                "account": account,
                "symbol": symbol,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "fibo_flow: resolve_instrument(via desk) raised: %s",
                exc,
            )
            return None
        if not getattr(resp, "success", False):
            return None
        inst = getattr(resp, "instrument", None)
        canonical = getattr(inst, "symbol", None) if inst is not None else None
        if not canonical:
            return None
        return str(canonical).strip() or None

    def _propose_exchange_instrument(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> Screen:
        """Render the agent-resolved instrument proposal.

        Spec §2 / §3 / §10: the canonical venue contract the agent
        returned is shown to the user with Agree / Other / Browse /
        Back / Cancel buttons. Only Agree commits the mapping.

        Phase 2.3: when neither the alias cache nor a direct
        ``resolve_instrument`` call produces a canonical, the flow
        ranks plausible candidates from the exchange catalog and
        shows the candidate picker (spec §3). Price evidence is
        attached to each candidate but is NEVER used to auto-select.
        """
        # Try cached alias memory first.
        key = alias_key(sess.exchange or "", sess.account or "",
                        sess.symbol or "")
        if self._alias_memory is not None and sess.symbol:
            try:
                cached = self._alias_memory.revalidate(
                    key, resolve_fn=self._safe_resolve,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "fibo_flow: alias_memory.revalidate failed: %s", exc
                )
                cached = None
            if cached is not None:
                # Cache hit + still valid → propose the cached
                # mapping directly. resolution_input is whatever
                # the user typed last time (or the original
                # source_symbol on first approval). Stage the
                # proposal canonical in sess.awaiting so Agree
                # can find it.
                sess.resolution_input = cached.resolution_input or sess.symbol
                sess.proposal_origin = "cached"
                sess.awaiting = "proposal:" + cached.exchange_instrument
                sess.state = SessionState.AWAITING_INSTRUMENT_CONFIRM
                return self._render_instrument_proposal(sess, snap)
        # Cache miss / stale / revalidation failed → fresh
        # resolution of the source symbol.
        canonical = self._safe_resolve(
            sess.exchange or "", sess.account or "",
            sess.symbol or "",
        )
        if canonical is None:
            # Phase 2.3: try ranked candidate discovery before
            # falling back to the "could not resolve" screen.
            return self._propose_via_candidates(sess, snap)
        # Resolution succeeded → render proposal.
        sess.resolution_input = sess.symbol
        sess.proposal_origin = "auto"
        sess.state = SessionState.AWAITING_INSTRUMENT_CONFIRM
        # We stage the proposal here. session.exchange_instrument
        # is still None — only the user's Agree commits it.
        # The render reads from a transient field on the session.
        sess.awaiting = "proposal:" + canonical
        return self._render_instrument_proposal(sess, snap)

    def _propose_via_candidates(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> Screen:
        """Phase 2.3: rank plausible venue candidates and show the
        candidate picker.

        This is the AMBIGUOUS SYMBOL path (spec §3). Steps:

        1. Fetch the full exchange catalog (read-only ``GET /v1/markets``).
        2. Rank with ``candidates.rank_candidates`` (price evidence
           is supporting only — semantic / name similarity ranks higher).
        3. Attach live prices via ``market_price`` (read-only ``GET
           /v1/perps/mark_prices``) when available. Missing prices
           do NOT block display.
        4. Store the ranked candidates on ``sess.candidates`` and
           render the picker.

        The flow MUST NEVER pick a candidate automatically. The
        user always taps a button (and that pick is still
        revalidated through the exchange agent before the proposal
        screen is shown).
        """
        from .candidates import (
            InstrumentCandidate,
            rank_candidates,
            attach_price,
        )
        from . import discovery

        sess.candidates = []
        sess.selected_candidate_canonical = None

        try:
            catalog = discovery.list_market_catalog(
                sess.exchange or "", sess.account or "",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "fibo_flow: catalog fetch failed: %s", exc
            )
            catalog = discovery.CATALOG_UNAVAILABLE

        # CATALOG_UNAVAILABLE sentinel means the chosen agent
        # doesn't implement list_instruments (resolve-only exchange
        # such as Apex, Pacifica or Raydium, or a transient
        # failure). Fall back to the manual-resolution screen —
        # never fabricate an empty catalog.
        if (
            catalog == discovery.CATALOG_UNAVAILABLE
            or not isinstance(catalog, list)
        ):
            sess.state = SessionState.AWAITING_INSTRUMENT_CONFIRM
            return self._render_instrument_unresolved(sess, snap)

        # A truly empty list (``[]``) means the agent successfully
        # returned zero markets for this account — extremely rare
        # but legal. Treat it the same as unavailable so the user
        # doesn't see a 0-candidate picker.
        if not catalog:
            sess.state = SessionState.AWAITING_INSTRUMENT_CONFIRM
            return self._render_instrument_unresolved(sess, snap)

        # Build the ranker + price lookup bound to the chosen venue.
        def _price_lookup(market: str):
            try:
                return discovery.get_market_price(
                    sess.exchange or "",
                    sess.account or "",
                    market,
                )
            except Exception:  # noqa: BLE001
                return None

        enriched = attach_price(catalog, _price_lookup)
        ranked = rank_candidates(enriched, sess.symbol or "")
        # Top-N: keep up to 10 candidates on screen.
        top = ranked[:10]
        sess.candidates = top
        if not top:
            sess.state = SessionState.AWAITING_INSTRUMENT_CONFIRM
            return self._render_instrument_unresolved(sess, snap)
        # Show the picker. The user picks one, which we revalidate
        # through the agent before staging the proposal.
        sess.state = SessionState.AWAITING_INSTRUMENT_CONFIRM
        return self._render_candidates_screen(sess, snap)

    def _handle_instrument_agree(
        self,
        sess: FiboSession,
        snap: Optional[Mt4Snapshot],
    ) -> Screen:
        """User tapped Agree. Commit the canonical contract id to
        ``exchange_instrument``, persist to alias memory (ONLY
        here), and advance to volume."""
        if snap is None:
            return self._render_no_data(sess)
        if sess.state != SessionState.AWAITING_INSTRUMENT_CONFIRM:
            return self._render_invalid_callback(sess.session_key)
        canonical = self._extract_proposal_canonical(sess)
        if not canonical:
            # Defensive: nothing to agree to.
            return self._render_invalid_callback(sess.session_key)
        sess.exchange_instrument = canonical
        sess.awaiting = None
        sess.choices_instruments = []
        sess.instrument_page = 0
        # Record to alias memory. Failure here must NOT block the
        # wizard — alias memory is a hint, not a source of truth.
        if self._alias_memory is not None and sess.symbol:
            try:
                key = alias_key(
                    sess.exchange or "", sess.account or "",
                    sess.symbol or "",
                )
                self._alias_memory.record_approval(
                    key,
                    source_symbol=sess.symbol or "",
                    resolution_input=sess.resolution_input or sess.symbol or "",
                    exchange_instrument=canonical,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "fibo_flow: alias_memory.record_approval failed: %s", exc
                )
        sess.state = SessionState.AWAITING_VOLUME
        return self._render_volume_screen(sess, snap)

    def _handle_instrument_other(
        self,
        sess: FiboSession,
        snap: Optional[Mt4Snapshot],
    ) -> Screen:
        """User tapped Other. Switch to the alias-entry state.
        Text input from this point is intercepted ONLY for this
        exact session.

        We deliberately preserve the staged proposal in
        ``sess.awaiting`` (if present) so Back returns to the
        original proposal screen instead of dropping the user
        to "could not resolve".
        """
        if snap is None:
            return self._render_no_data(sess)
        # NOTE: we DO NOT clear sess.awaiting here. The proposal
        # canonical stays staged so Back can restore it.
        sess.state = SessionState.AWAITING_EXCHANGE_ALIAS
        return self._render_alias_prompt(sess, snap)

    def _handle_instrument_browse(
        self,
        sess: FiboSession,
        snap: Optional[Mt4Snapshot],
    ) -> Screen:
        """User tapped Browse. Page through the read-only market
        list. Clicking a market sends the user to the same
        translation approval screen (spec §5)."""
        if snap is None:
            return self._render_no_data(sess)
        sess.instrument_page = 0
        # Refresh market list (the Phase 2.1 lister is reused).
        instruments: List[Any] = []
        if self._list_instruments is not None and sess.exchange and sess.account:
            try:
                raw = self._list_instruments(
                    str(sess.exchange), str(sess.account)
                )
            except Exception:
                raw = []
            # list_market_catalog returns either a list of records or the
            # sentinel string \"unavailable\". Never expand a string into chars.
            if isinstance(raw, list):
                instruments = [
                    entry
                    for entry in raw
                    if _catalog_instrument_id(entry)
                ]
            else:
                instruments = []
        sess.choices_instruments = list(instruments)
        sess.state = SessionState.AWAITING_MARKET_BROWSE
        return self._render_market_browse(sess, snap)

    def _handle_browse_paginate(
        self,
        sess: FiboSession,
        snap: Optional[Mt4Snapshot],
        data: str,
    ) -> Screen:
        if snap is None:
            return self._render_no_data(sess)
        # CB_BROWSEPG: -1 for prev, 1..N for 1-based page jump.
        try:
            delta = int(data[len(CB_BROWSEPG):])
        except ValueError:
            return self._render_invalid_callback(sess.session_key)
        if delta == -1:
            sess.instrument_page = max(0, int(sess.instrument_page or 0) - 1)
        elif delta > 0:
            sess.instrument_page = max(0, delta - 1)
        return self._render_market_browse(sess, snap)

    def _handle_browse_market_pick(
        self,
        sess: FiboSession,
        snap: Optional[Mt4Snapshot],
        data: str,
    ) -> Screen:
        """User picked a market from the Browse list.

        We do NOT immediately store it as ``exchange_instrument``.
        Instead we set up a fresh proposal with this market as
        the candidate resolution_input (spec §5)."""
        if snap is None:
            return self._render_no_data(sess)
        idx = self._parse_index(data, len(CB_INSTSEL))
        if idx is None or idx >= len(sess.choices_instruments):
            return self._render_invalid_callback(sess.session_key)
        candidate = _catalog_instrument_id(sess.choices_instruments[idx])
        if not candidate:
            return self._render_invalid_callback(sess.session_key)
        # Run the candidate through the SAME agent path so the
        # user sees the canonical id (browse can return the
        # canonical id directly, but we re-resolve for safety).
        canonical = self._safe_resolve(
            sess.exchange or "", sess.account or "",
            candidate,
        )
        if canonical is None:
            # Browse returned a market that the agent no longer
            # resolves. Treat as a failed proposal.
            sess.resolution_input = candidate
            sess.awaiting = "alias-failure"
            sess.state = SessionState.AWAITING_INSTRUMENT_CONFIRM
            return self._render_alias_failure(sess, snap, candidate)
        sess.resolution_input = candidate
        sess.proposal_origin = "candidate"
        sess.awaiting = "proposal:" + canonical
        sess.state = SessionState.AWAITING_INSTRUMENT_CONFIRM
        return self._render_instrument_proposal(sess, snap)

    def _handle_legacy_inst_pick(
        self,
        sess: FiboSession,
        snap: Optional[Mt4Snapshot],
        data: str,
    ) -> Screen:
        """Backwards-compat shim for stale Phase 2.1 messages.

        Treats the picked market as a resolution_input and routes
        through the proposal screen (spec §5)."""
        if snap is None:
            return self._render_no_data(sess)
        idx = self._parse_index(data, len(CB_INST))
        if idx is None or idx >= len(sess.choices_instruments):
            return self._render_invalid_callback(sess.session_key)
        candidate = sess.choices_instruments[idx]
        # Reuse browse pick path (handles dict or str entries).
        return self._handle_browse_market_pick(
            sess, snap, f"{CB_INSTSEL}{idx}"
        )

    # ------------------------------------------------------------------
    # Phase 2.3 — ranked candidate picker
    # ------------------------------------------------------------------

    def _handle_candidate_pick(
        self,
        sess: FiboSession,
        snap: Optional[Mt4Snapshot],
        data: str,
    ) -> Screen:
        """User picked a candidate from the ranked picker.

        The button callback carries ONLY an INDEX — never the raw
        venue contract string. We look up
        ``sess.candidates[idx].instrument``, then revalidate it
        through the same exchange agent path. The agent is the
        ONLY authority allowed to produce the canonical
        ``exchange_instrument``.

        Only after the agent confirms the contract id do we stage
        the proposal. (If the agent no longer resolves the
        candidate — e.g. the catalog raced — we drop back to the
        "could not resolve" screen.)
        """
        if snap is None:
            return self._render_no_data(sess)
        if sess.state != SessionState.AWAITING_INSTRUMENT_CONFIRM:
            return self._render_invalid_callback(sess.session_key)
        idx = self._parse_index(data, len(CB_CAND))
        if idx is None or idx >= len(sess.candidates):
            return self._render_invalid_callback(sess.session_key)
        candidate = sess.candidates[idx]
        # Validate through the live agent path. NEVER trust the
        # button payload as the canonical identity.
        canonical = self._safe_resolve(
            sess.exchange or "",
            sess.account or "",
            candidate.instrument,
        )
        if canonical is None:
            # Agent rejected the candidate — fall back to the
            # unresolved screen.
            sess.state = SessionState.AWAITING_INSTRUMENT_CONFIRM
            sess.awaiting = "alias-failure"
            return self._render_alias_failure(sess, snap, candidate.instrument)
        # Stage the proposal. session.exchange_instrument stays
        # None — only Agree commits.
        sess.resolution_input = candidate.instrument
        sess.proposal_origin = "candidate"
        sess.selected_candidate_canonical = canonical
        sess.awaiting = "proposal:" + canonical
        return self._render_instrument_proposal(sess, snap)

    def _render_candidates_screen(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> Screen:
        """Spec §3: render the ranked candidate picker.

        One button per candidate (``fibo:s:cand:<idx>``), plus the
        secondary actions (Other / Browse / Back / Cancel). Prices
        are displayed as evidence only — they never auto-select.

        Callback lengths are kept tight: ``fibo:s:cand:9`` is
        15 bytes — well under the 64-byte Telegram limit. The
        raw venue contract string NEVER appears in callback_data.
        """
        age = snap.age_seconds(self._now_fn())
        header = self._age_header(age)
        src = sess.symbol or "?"
        # Build per-candidate rows.
        rows: List[List[Dict[str, str]]] = []
        for i, cand in enumerate(sess.candidates):
            label = cand.instrument
            if len(label) > 24:
                label = label[:21] + "..."
            # Append the price as part of the button text (purely
            # decorative; not used for selection logic).
            if cand.price is not None:
                # Trim trailing zeros for compact display.
                ps = format(cand.price.normalize(), "f")
                rows.append([
                    {"text": f"{label} · {ps}",
                     "callback_data": f"{CB_CAND}{i}"},
                ])
            else:
                rows.append([
                    {"text": label,
                     "callback_data": f"{CB_CAND}{i}"},
                ])
        rows.append([
            {"text": "✏️ Other", "callback_data": CB_OTHER},
            {"text": "📋 Browse markets", "callback_data": CB_BROWSE},
        ])
        rows.append([
            {"text": "◀️ Back", "callback_data": CB_BACK},
            {"text": "❌ Cancel", "callback_data": CB_CANCEL},
        ])
        # Build the body listing per-candidate metadata. We render
        # only the top entries to keep the message short.
        body_lines: List[str] = []
        for i, cand in enumerate(sess.candidates[:8]):
            block = cand.to_compact_block(i)
            body_lines.append(block)
        body = "\n\n".join(body_lines)
        # Re-confirm: this is a hint, NOT an auto-selection.
        text = (
            f"{header}"
            f"🔎 Resolve MT4 instrument\n\n"
            f"Source: {src}\n"
            f"Exchange: {sess.exchange}\n"
            f"Account: {sess.account}\n\n"
            f"Possible matches:\n\n"
            f"{body}\n\n"
            f"Choose the exchange instrument that matches {src}.\n\n"
            f"Ranked by name similarity; price is supporting evidence only."
        )
        return Screen(text=text, buttons=rows)

    def _extract_proposal_canonical(self, sess: FiboSession) -> str:
        """Decode the canonical id for the current proposal.

        Sources (in order):
          1. ``sess.awaiting`` (set when entering the proposal
             screen — the staged-but-not-yet-Agreed canonical).
          2. ``sess.exchange_instrument`` (set after Agree; useful
             for back-navigation that returns to the proposal).

        Returns ``""`` when neither has a value.
        """
        tag = sess.awaiting or ""
        if tag.startswith("proposal:"):
            canonical = tag[len("proposal:"):].strip()
            if canonical:
                return canonical
        # Fall back to the agreed (or pre-agreed) value.
        if sess.exchange_instrument:
            return sess.exchange_instrument.strip()
        return ""

    # ------------------------------------------------------------------
    # Back navigation
    # ------------------------------------------------------------------

    def _handle_back(
        self,
        sess: FiboSession,
        snap: Optional[Mt4Snapshot],
    ) -> Screen:
        # Back navigates one step up; cannot go past symbol+variant.
        prev = sess.state
        # Phase 2.2: proposal / alias / browse states all go back
        # to the account screen (the user can re-pick an account
        # to retry resolution, or simply choose Other / Browse
        # again).
        if prev == SessionState.AWAITING_INSTRUMENT_CONFIRM:
            sess.awaiting = None
            sess.exchange_instrument = None
            sess.choices_instruments = []
            sess.instrument_page = 0
            sess.state = SessionState.AWAITING_ACCOUNT
            return self._render_account_screen(sess, snap) if snap else self._render_no_data(sess)
        if prev == SessionState.AWAITING_EXCHANGE_ALIAS:
            sess.state = SessionState.AWAITING_INSTRUMENT_CONFIRM
            # NOTE: do NOT clear sess.awaiting here — the staged
            # proposal canonical is preserved so the user can Agree.
            return self._render_instrument_proposal(sess, snap) if snap else self._render_no_data(sess)
        if prev == SessionState.AWAITING_MARKET_BROWSE:
            sess.instrument_page = 0
            sess.state = SessionState.AWAITING_INSTRUMENT_CONFIRM
            return self._render_instrument_proposal(sess, snap) if snap else self._render_no_data(sess)
        if prev == SessionState.AWAITING_INSTRUMENT:
            sess.exchange_instrument = None
            sess.choices_instruments = []
            sess.state = SessionState.AWAITING_ACCOUNT
            return self._render_account_screen(sess, snap) if snap else self._render_no_data(sess)
        if prev == SessionState.AWAITING_VOLUME:
            sess.starting_volume = None
            # Always go back to the proposal / confirm screen.
            sess.state = SessionState.AWAITING_INSTRUMENT_CONFIRM
            return self._render_instrument_proposal(sess, snap) if snap else self._render_no_data(sess)
        if prev == SessionState.AWAITING_ACCOUNT:
            sess.account = None
            sess.choices_accounts = []
            sess.state = SessionState.AWAITING_EXCHANGE
            return self._render_exchange_screen(sess, snap) if snap else self._render_no_data(sess)
        if prev == SessionState.AWAITING_EXCHANGE:
            sess.exchange = None
            sess.choices_exchanges = []
            sess.state = SessionState.AWAITING_SIDE
            return self._render_side_screen(sess, snap) if snap else self._render_no_data(sess)
        if prev == SessionState.AWAITING_SIDE:
            sess.side = None
            sess.state = SessionState.AWAITING_SYMBOL
            return self._render_symbols_screen(sess, snap) if snap else self._render_no_data(sess)
        if prev == SessionState.AWAITING_CONFIRM:
            sess.starting_volume = None
            sess.state = SessionState.AWAITING_VOLUME
            return self._render_volume_screen(sess, snap) if snap else self._render_no_data(sess)
        return self._render_symbols_screen(sess, snap) if snap else self._render_no_data(sess)

    def _handle_create(
        self,
        sess: FiboSession,
        snap: Optional[Mt4Snapshot],
    ) -> Screen:
        # Atomic re-check (spec §12).
        if snap is None:
            return self._render_no_data(sess)

        # Stale gate (§4).
        age = snap.age_seconds(self._now_fn())
        if age is not None and age > self._stale_threshold:
            return self._render_stale_confirmation(sess, snap)

        # Source identity must match what we captured.
        if sess.snap_source is not None and snap.source != sess.snap_source:
            return self._render_source_changed(sess, snap)

        fibo = snap.find_fibo(sess.symbol or "", sess.variant or "")
        if fibo is None:
            return self._render_symbol_gone(sess, snap)

        side = sess.side
        if side not in (SIDE_BUY, SIDE_SELL):
            return self._render_invalid_callback(sess.session_key)
        if not fibo.is_side_active(side):
            return self._render_side_inactive(sess, snap, side)

        cycle_id_now = fibo.side_cycle_id(side)
        weight_now = fibo.side_cumulative_weight(side)
        if sess.snap_cycle_id is not None and cycle_id_now != sess.snap_cycle_id:
            # Cycle changed -> refresh confirmation, require re-press.
            self._capture_snapshot_metadata(sess, snap)
            return self._render_cycle_changed(sess, snap)

        # Weight may have changed; refresh target & require re-press.
        if sess.snap_cumulative_weight is not None and \
                weight_now != sess.snap_cumulative_weight:
            self._capture_snapshot_metadata(sess, snap)
            return self._render_weight_changed(sess, snap)

        # All checks passed: persist.
        starting_volume = sess.starting_volume
        if starting_volume is None or starting_volume <= 0:
            return self._render_volume_invalid(sess, reason="missing")
        target = (starting_volume * weight_now)
        try:
            registration = FiboRegistration.build(
                exchange=sess.exchange,
                account=sess.account,
                symbol=sess.symbol,                # JSONL compat (kept = source_symbol)
                source_symbol=sess.symbol,         # MT4 source symbol
                exchange_instrument=sess.exchange_instrument,  # venue contract id
                variant=sess.variant,
                side=side,
                starting_volume=starting_volume,
                source=snap.source,
                source_seq=snap.seq,
                source_cycle_id=cycle_id_now,
                source_cumulative_weight=weight_now,
                source_percentage=fibo.percentage,
                source_snapshot_received_at=snap.received_at,
                desired_exchange_size=target,
            )
        except ValueError as exc:
            logger.warning(
                "fibo_flow: registration build failed: %s", exc
            )
            return self._render_invalid_callback(sess.session_key)

        try:
            self._registration_store.append(registration)
        except DuplicateRegistrationError as exc:
            # Phase 2.7: if the existing latest row for this key
            # is ``stopped``, reactivate it instead of refusing
            # the Start flow. The user's Start wizard values for
            # mutable/snapshot fields are honoured on reactivation;
            # identity fields are verified against the stopped row.
            restarted = self._reactivate_if_stopped(
                registration_key=exc.registration_key,
                sess=sess,
                snap=snap,
                cycle_id_now=cycle_id_now,
                weight_now=weight_now,
                target=target,
            )
            if restarted is not None:
                self._sessions.reset(*sess.session_key)
                return self._render_restarted(restarted)
            return self._render_duplicate(sess, snap, exc.registration_key)
        except Exception as exc:  # noqa: BLE001 - surface generically
            logger.error(
                "fibo_flow: registration append failed: %s", exc
            )
            return self._render_store_error(sess, snap, str(exc))

        self._sessions.reset(*sess.session_key)
        return self._render_registered(registration)

    # ------------------------------------------------------------------
    # Phase 2.7 — Restart helper for stopped registrations
    # ------------------------------------------------------------------

    def _reactivate_if_stopped(
        self,
        *,
        registration_key: str,
        sess: "FiboSession",
        snap: Mt4Snapshot,
        cycle_id_now: int,
        weight_now: Decimal,
        target: Decimal,
    ) -> Optional[FiboRegistration]:
        """Phase 2.7 restart: if the latest persisted row for
        ``registration_key`` is ``stopped``, reactivate it with
        the CURRENT snapshot fields taken from the new Start
        wizard session.

        Returns the reactivated registration on success, or
        ``None`` if the existing row is not stopped (in which
        case the caller falls back to the duplicate screen).

        Failure modes that raise ``ValueError`` (identity
        mismatch, missing registration, etc.) propagate up and
        are caught by ``_handle_create``.
        """
        latest = self._registration_store.get(registration_key)
        if latest is None or not latest.is_stopped:
            return None
        starting_volume = sess.starting_volume
        if starting_volume is None or starting_volume <= 0:
            return None
        percentage = self._current_percentage(snap, sess)
        return self._registration_store.reactivate(
            registration_key,
            source_symbol=sess.symbol or "",
            exchange_instrument=sess.exchange_instrument or "",
            starting_volume=starting_volume,
            desired_exchange_size=target,
            source=snap.source,
            source_seq=snap.seq,
            source_cycle_id=cycle_id_now,
            source_cumulative_weight=weight_now,
            source_percentage=percentage,
            source_snapshot_received_at=snap.received_at,
        )

    @staticmethod
    def _current_percentage(snap: Mt4Snapshot, sess: "FiboSession") -> Decimal:
        """Return the live MT4 percentage for the current
        symbol/variant. Used by ``_reactivate_if_stopped`` to
        populate ``source_percentage`` on the reactivated row.
        """
        try:
            fibo = snap.find_fibo(sess.symbol or "", sess.variant or "")
        except Exception:  # noqa: BLE001
            return Decimal("0")
        if fibo is None:
            return Decimal("0")
        return Decimal(str(fibo.percentage))

    # ------------------------------------------------------------------
    # Renderers
    # ------------------------------------------------------------------

    def _render_no_data(self, sess: FiboSession) -> Screen:
        text = (
            "⚠️ No MT4 data yet.\n\n"
            "The /fibo wizard reads its data from the MT4 Reader cache.\n"
            "Start the reader (see kam README) and try again.\n"
        )
        return Screen(
            text=text,
            buttons=[[{"text": "❌ Cancel", "callback_data": CB_CANCEL}]],
        )

    def _render_invalid_callback(self, key: Tuple[str, str]) -> Screen:
        return Screen(
            text=(
                "⚠️ Invalid or expired selection.\n"
                "Press Start Fibo to begin a fresh registration."
            ),
            buttons=[[{"text": "❌ Cancel", "callback_data": CB_CANCEL}]],
        )

    def _render_cancelled(self) -> Screen:
        return Screen(
            text="🚫 Start Fibo cancelled.",
            buttons=[],
        )

    def _render_symbols_screen(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> Screen:
        age = snap.age_seconds(self._now_fn())
        header = self._age_header(age)
        rows: List[List[Dict[str, str]]] = []
        for i, pair in enumerate(sess.choices_symbols):
            label = f"{pair['symbol']} — {pair['variant']}"
            rows.append([
                {"text": label, "callback_data": f"{CB_SYM}{i}"},
            ])
        rows.append([
            {"text": "❌ Cancel", "callback_data": CB_CANCEL},
        ])
        text = (
            f"{header}"
            f"📊 Pick a symbol + variant:\n\n"
            f"{len(sess.choices_symbols)} available."
        )
        return Screen(text=text, buttons=rows)

    def _render_side_screen(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> Screen:
        fibo = snap.find_fibo(sess.symbol or "", sess.variant or "")
        if fibo is None:
            return self._render_symbol_gone(sess, snap)
        age = snap.age_seconds(self._now_fn())
        header = self._age_header(age)
        buy_active = fibo.is_side_active(SIDE_BUY)
        sell_active = fibo.is_side_active(SIDE_SELL)
        buttons: List[List[Dict[str, str]]] = []
        if buy_active:
            buttons.append([
                {"text": "🟢 BUY", "callback_data": f"{CB_SIDE}{SIDE_TOKEN_BUY}"},
            ])
        if sell_active:
            buttons.append([
                {"text": "🔴 SELL", "callback_data": f"{CB_SIDE}{SIDE_TOKEN_SELL}"},
            ])
        buttons.append([
            {"text": "◀️ Back", "callback_data": CB_BACK},
            {"text": "❌ Cancel", "callback_data": CB_CANCEL},
        ])
        inactive_note = ""
        if not buy_active and not sell_active:
            inactive_note = (
                "\n\n⚠️ Neither side has an active MT4 cycle for this pair."
            )
        elif not buy_active:
            inactive_note = "\n\n(BUY side has no active MT4 cycle.)"
        elif not sell_active:
            inactive_note = "\n\n(SELL side has no active MT4 cycle.)"
        text = (
            f"{header}"
            f"📈 {sess.symbol} — {sess.variant}\n\n"
            f"Pick a side:"
            f"{inactive_note}"
        )
        return Screen(text=text, buttons=buttons)

    def _render_side_inactive(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
        side: str,
    ) -> Screen:
        text = (
            f"⚠️ No active MT4 cycle for {side} on "
            f"{sess.symbol} — {sess.variant}.\n\n"
            f"The MT4 Reader reports cycle_id > 0 AND cumulative_weight > 0 "
            f"is required to continue. Press Back and pick another pair or "
            f"side."
        )
        return Screen(
            text=text,
            buttons=[[
                {"text": "◀️ Back", "callback_data": CB_BACK},
                {"text": "❌ Cancel", "callback_data": CB_CANCEL},
            ]],
        )

    def _render_exchange_screen(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> Screen:
        age = snap.age_seconds(self._now_fn())
        header = self._age_header(age)
        rows: List[List[Dict[str, str]]] = []
        for i, name in enumerate(sess.choices_exchanges):
            rows.append([
                {"text": name, "callback_data": f"{CB_EX}{i}"},
            ])
        rows.append([
            {"text": "◀️ Back", "callback_data": CB_BACK},
            {"text": "❌ Cancel", "callback_data": CB_CANCEL},
        ])
        text = (
            f"{header}"
            f"🏦 Pick an exchange for {sess.symbol} {sess.side}:\n\n"
            f"{len(sess.choices_exchanges)} configured."
        )
        return Screen(text=text, buttons=rows)

    def _render_no_accounts(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> Screen:
        text = (
            f"⚠️ No accounts configured for {sess.exchange}.\n\n"
            f"Add credentials for this exchange in ~/.hermes/.env and try "
            f"again."
        )
        return Screen(
            text=text,
            buttons=[[
                {"text": "◀️ Back", "callback_data": CB_BACK},
                {"text": "❌ Cancel", "callback_data": CB_CANCEL},
            ]],
        )

    def _render_account_screen(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> Screen:
        age = snap.age_seconds(self._now_fn())
        header = self._age_header(age)
        rows: List[List[Dict[str, str]]] = []
        for i, alias in enumerate(sess.choices_accounts):
            rows.append([
                {"text": alias, "callback_data": f"{CB_ACCT}{i}"},
            ])
        rows.append([
            {"text": "◀️ Back", "callback_data": CB_BACK},
            {"text": "❌ Cancel", "callback_data": CB_CANCEL},
        ])
        text = (
            f"{header}"
            f"👤 Pick an account on {sess.exchange}:\n\n"
            f"{len(sess.choices_accounts)} configured."
        )
        return Screen(text=text, buttons=rows)

    def _render_instrument_screen(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> Screen:
        """Phase 2.1: the venue-instrument picker.

        Shows the list of actual exchange instruments discovered for
        the chosen exchange + account. The user MUST pick one — we
        do not assume any source/exchange equivalence.

        Callbacks stay under the 64-byte Telegram limit: a typical
        button is ``fibo:s:inst:7`` (15 bytes).
        """
        age = snap.age_seconds(self._now_fn())
        header = self._age_header(age)
        rows: List[List[Dict[str, str]]] = []
        for i, inst in enumerate(sess.choices_instruments):
            # Truncate the label so even a long instrument name fits
            # in a single button.
            label = _catalog_display_symbol(inst)
            price = _catalog_price_text(inst)
            if price:
                label = f"{label} · {price}"
            if len(label) > 28:
                label = label[:25] + "..."
            rows.append([
                {"text": label, "callback_data": f"{CB_INST}{i}"},
            ])
        rows.append([
            {"text": "◀️ Back", "callback_data": CB_BACK},
            {"text": "❌ Cancel", "callback_data": CB_CANCEL},
        ])
        text = (
            f"{header}"
            f"🏷️ Pick the exchange market for "
            f"{sess.symbol} {sess.variant} {sess.side} "
            f"on {sess.exchange}/{sess.account}:\n\n"
            f"MT4 source: {sess.symbol}  ·  {len(sess.choices_instruments)} markets available.\n\n"
            f"Choose the actual venue contract to target."
        )
        return Screen(text=text, buttons=rows)

    # ------------------------------------------------------------------
    # Phase 2.2 — instrument-translation renders
    # ------------------------------------------------------------------

    def _render_instrument_proposal(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> Screen:
        """Show the agent-resolved canonical venue contract to the
        user (spec §2.A). Buttons: Agree / Other / Browse / Back /
        Cancel. Only Agree commits the mapping.
        """
        canonical = self._extract_proposal_canonical(sess)
        # If there's no canonical staged (e.g. user navigated here
        # without an agent proposal), re-render the unresolved screen.
        if not canonical:
            return self._render_instrument_unresolved(sess, snap)
        age = snap.age_seconds(self._now_fn())
        header = self._age_header(age)
        src = sess.symbol or "?"
        # The rendered exchange label is dynamic. We pass the
        # canonical session exchange id through the generic
        # ``_exchange_display_label`` helper so the proposal screen
        # reflects the actual selected venue (hibachi / hyperliquid
        # / lighter / etc.), derived from the agent's ``name=...``
        # declaration at runtime — never hard-coded in this renderer.
        exchange_label = _exchange_display_label(sess.exchange)
        rows = [
            [
                {"text": "✅ Agree", "callback_data": CB_AGREE},
                {"text": "✏️ Other", "callback_data": CB_OTHER},
                {"text": "📋 Browse markets", "callback_data": CB_BROWSE},
            ],
            [
                {"text": "◀️ Back", "callback_data": CB_BACK},
                {"text": "❌ Cancel", "callback_data": CB_CANCEL},
            ],
        ]
        # Decide whether (and how) to show the alias line. UI polish:
        # the label depends on which path staged the proposal so we
        # never show a confusing blank or ambiguous "Your alias: …"
        # when there is no user-typed input to disclose.
        #   auto      → omit (fresh resolve_instrument succeeded)
        #   candidate → omit (user picked from picker)
        #   alias     → "Your input: …" (user typed via Other)
        #   cached    → "Learned alias: …" (alias memory hit)
        ri = (sess.resolution_input or "").strip()
        origin = sess.proposal_origin
        if origin in ("auto", "candidate") or not ri or ri == src:
            alias_line = ""
        elif origin == "alias":
            alias_line = f"Your input:    {ri}\n"
        elif origin == "cached":
            alias_line = f"Learned alias: {ri}\n"
        else:
            # Defensive fallback (unknown origin) — show as user
            # input rather than risk an empty labelled line.
            alias_line = f"Your input:    {ri}\n"
        # Show the live price when the canonical came with one.
        # For fresh proposals the price is on the candidate object;
        # for alias / cached paths we read it from the resolved
        # candidate if still in the session.
        price_text = ""
        if sess.candidates:
            for c in sess.candidates:
                if c.instrument == canonical and c.price is not None:
                    price_text = (
                        f"Price:        "
                        f"{format(c.price.normalize(), 'f')}\n"
                    )
                    break
        # Phase 2.4.2: pad the exchange label so the canonical
        # column lines up across heterogeneous exchange ids.
        # Field width 12 + ": " gives a stable 14-char left
        # edge, matching the "MT4 source:  " prefix above (which
        # is 14 chars including its leading label + 2 spaces).
        text = (
            f"{header}"
            f"🔎 Instrument translation\n\n"
            f"MT4 source:  {src}\n"
            f"{alias_line}"
            f"{exchange_label:<12}: {canonical}\n"
            f"{price_text}\n"
            f"Does this look correct?"
        )
        return Screen(text=text, buttons=rows)

    def _render_instrument_unresolved(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> Screen:
        """Show the "could not resolve" screen (spec §2.B).
        Buttons: Enter alias / Browse / Back / Cancel. No Agree —
        we have nothing to agree to."""
        age = snap.age_seconds(self._now_fn())
        header = self._age_header(age)
        src = sess.symbol or "?"
        exchange_label = sess.exchange or "?"
        rows = [
            [
                {"text": "✏️ Enter alias", "callback_data": CB_OTHER},
                {"text": "📋 Browse markets", "callback_data": CB_BROWSE},
            ],
            [
                {"text": "◀️ Back", "callback_data": CB_BACK},
                {"text": "❌ Cancel", "callback_data": CB_CANCEL},
            ],
        ]
        text = (
            f"{header}"
            f"⚠️ Could not uniquely resolve MT4 symbol:\n\n"
            f"MT4 source:  {src}\n"
            f"Exchange:    {exchange_label}\n\n"
            f"Enter another symbol/alias you believe the exchange uses,\n"
            f"or browse available markets."
        )
        return Screen(text=text, buttons=rows)

    def _render_alias_prompt(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> Screen:
        """Spec §3: the user is typing a free-form alias (e.g.
        ``US500``) which will be passed through the SAME agent
        resolver.
        """
        age = snap.age_seconds(self._now_fn())
        header = self._age_header(age)
        src = sess.symbol or "?"
        rows = [
            [
                {"text": "◀️ Back", "callback_data": CB_BACK},
                {"text": "❌ Cancel", "callback_data": CB_CANCEL},
            ],
        ]
        text = (
            f"{header}"
            f"✏️ Enter exchange alias for {src}\n\n"
            f"Type a symbol/alias the exchange might use\n"
            f"(e.g. ``US500`` for #SP500).\n\n"
            f"It will be validated through the {sess.exchange} agent."
        )
        return Screen(text=text, buttons=rows)

    def _render_alias_failure(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
        attempted: str,
    ) -> Screen:
        """Spec §3: the typed alias failed agent validation."""
        age = snap.age_seconds(self._now_fn())
        header = self._age_header(age)
        exchange_label = sess.exchange or "?"
        rows = [
            [
                {"text": "✏️ Try another", "callback_data": CB_INSTFAIL_RETRY},
                {"text": "📋 Browse markets", "callback_data": CB_BROWSE},
            ],
            [
                {"text": "◀️ Back", "callback_data": CB_BACK},
                {"text": "❌ Cancel", "callback_data": CB_CANCEL},
            ],
        ]
        text = (
            f"{header}"
            f"❌ {exchange_label} could not resolve \"{attempted}\".\n\n"
            f"Try another alias or browse markets."
        )
        return Screen(text=text, buttons=rows)

    def _render_market_browse(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> Screen:
        """Spec §5: compact paginated market list.

        Each row is one instrument button labeled ``BASE · price``
        (falls back to a short venue id). Callback_data is an INDEX
        (not the raw market string), so we stay under the 64-byte
        Telegram limit regardless of instrument name.
        """
        from .session import SESSION_TTL_SECONDS  # noqa: F401  (sanity)
        age = snap.age_seconds(self._now_fn())
        header = self._age_header(age)
        page_size = _BROWSE_PAGE_SIZE
        total = len(sess.choices_instruments)
        page_count = max(1, (total + page_size - 1) // page_size) if total else 1
        page = max(0, min(int(sess.instrument_page or 0), max(0, page_count - 1)))
        sess.instrument_page = page
        start = page * page_size
        end = min(start + page_size, total)
        rows: List[List[Dict[str, str]]] = []
        for i in range(start, end):
            entry = sess.choices_instruments[i]
            label = self._browse_button_label(sess, entry)
            if len(label) > 28:
                label = label[:25] + "..."
            rows.append([
                {"text": label, "callback_data": f"{CB_INSTSEL}{i}"},
            ])
        nav_row: List[Dict[str, str]] = []
        if page > 0:
            # 1-based page jump for previous page.
            nav_row.append(
                {"text": "◀ Prev", "callback_data": f"{CB_BROWSEPG}{page}"}
            )
        if end < total:
            # Next page is 1-based page+2 because handler uses delta-1.
            nav_row.append(
                {"text": "Next ▶", "callback_data": f"{CB_BROWSEPG}{page + 2}"}
            )
        if nav_row:
            rows.append(nav_row)
        rows.append([
            {"text": "◀️ Back", "callback_data": CB_BACK},
            {"text": "❌ Cancel", "callback_data": CB_CANCEL},
        ])
        text = (
            f"{header}"
            f"📋 Markets on {sess.exchange}/{sess.account}\n\n"
            f"Page {page + 1} of {page_count}"
            f"  ·  {total} markets\n\n"
            f"Pick a market to propose it as the canonical contract.\n"
            f"You'll still need to Agree on the next screen."
        )
        return Screen(text=text, buttons=rows)

    def _browse_button_label(self, sess: FiboSession, entry: Any) -> str:
        """Human button text: preferred ``BTC · 95,000`` style."""
        short = _catalog_display_symbol(entry)
        price = _catalog_price_text(entry)
        if not price:
            inst_id = _catalog_instrument_id(entry)
            if inst_id and sess.exchange and sess.account:
                price = self._lookup_browse_price(
                    str(sess.exchange), str(sess.account), inst_id
                )
                # Cache on dict entries so Next/Prev does not re-hit every time.
                if price and isinstance(entry, dict):
                    entry["price"] = price
        if price:
            return f"{short} · {price}"
        return short

    def _lookup_browse_price(
        self, exchange: str, account: str, instrument: str
    ) -> Optional[str]:
        """Best-effort live price for browse buttons (read-only)."""
        try:
            from .discovery import get_market_price
            px = get_market_price(exchange, account, instrument)
        except Exception:  # noqa: BLE001
            return None
        if px is None:
            return None
        try:
            d = Decimal(str(px))
        except Exception:  # noqa: BLE001
            return None
        if not d.is_finite():
            return None
        # Compact display: drop trailing zeros, keep readability.
        text = format(d.normalize(), "f") if d == d.to_integral() else format(d, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        # thousands separators for large marks
        try:
            if "." in text:
                whole, frac = text.split(".", 1)
                whole = f"{int(whole):,}"
                return f"{whole}.{frac}"
            return f"{int(text):,}"
        except Exception:  # noqa: BLE001
            return text

    def _render_volume_screen(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> Screen:
        age = snap.age_seconds(self._now_fn())
        header = self._age_header(age)
        prior = ""
        if sess.starting_volume is not None:
            prior = f"\n\n(Previous entry: {sess.starting_volume})"
        text = (
            f"{header}"
            f"💰 Send starting volume as a number > 0.\n\n"
            f"Decimal accepted (e.g. 0.10). Decimals preserved exactly.{prior}"
        )
        return Screen(text=text, buttons=[])

    def _render_volume_invalid(
        self,
        sess: FiboSession,
        *,
        reason: str,
    ) -> Screen:
        reason_text = {
            "not_a_number": "That wasn't a number. Try again.",
            "not_finite":   "NaN / infinity are not allowed. Try again.",
            "not_positive": "Volume must be > 0. Try again.",
            "missing":      "Volume is missing. Try again.",
        }.get(reason, "Invalid volume. Try again.")
        text = f"⚠️ {reason_text}\n\nSend a decimal number > 0:"
        # Stay in AWAITING_VOLUME so the next text input is intercepted.
        sess.state = SessionState.AWAITING_VOLUME
        return Screen(text=text, buttons=[[
            {"text": "◀️ Back", "callback_data": CB_BACK},
            {"text": "❌ Cancel", "callback_data": CB_CANCEL},
        ]])

    def _render_confirmation(
        self,
        sess: FiboSession,
        snap: Optional[Mt4Snapshot],
    ) -> Screen:
        if snap is None:
            return self._render_no_data(sess)
        # Capture snapshot metadata now (so Create can re-validate).
        self._capture_snapshot_metadata(sess, snap)
        fibo = snap.find_fibo(sess.symbol or "", sess.variant or "")
        if fibo is None:
            return self._render_symbol_gone(sess, snap)
        side = sess.side
        if side not in (SIDE_BUY, SIDE_SELL):
            return self._render_invalid_callback(sess.session_key)
        weight = fibo.side_cumulative_weight(side)
        cycle_id = fibo.side_cycle_id(side)
        target = (sess.starting_volume or Decimal("0")) * weight
        age = snap.age_seconds(self._now_fn())
        age_str = f"{age:.1f}s" if age is not None else "?"

        text = self._format_confirmation(
            source_symbol=sess.symbol or "?",
            variant=sess.variant or "?",
            side=side,
            exchange=sess.exchange or "?",
            account=sess.account or "?",
            exchange_instrument=sess.exchange_instrument or "",
            starting_volume=sess.starting_volume,
            source=snap.source,
            source_seq=snap.seq,
            cycle_id=cycle_id,
            cumulative_weight=weight,
            percentage=fibo.percentage,
            desired_exchange_size=target,
            snapshot_age=age_str,
        )

        # Stale gate: Create is replaced with Refresh (§4).
        if age is not None and age > self._stale_threshold:
            return Screen(
                text=text + "\n\n⚠️ MT4 feed stale — press Refresh to re-fetch.",
                buttons=[[
                    {"text": "🔄 Refresh", "callback_data": CB_REFRESH},
                    {"text": "◀️ Back", "callback_data": CB_BACK},
                    {"text": "❌ Cancel", "callback_data": CB_CANCEL},
                ]],
            )
        return Screen(
            text=text,
            buttons=[[
                {"text": "✅ Create", "callback_data": CB_CREATE},
                {"text": "◀️ Back", "callback_data": CB_BACK},
                {"text": "❌ Cancel", "callback_data": CB_CANCEL},
            ]],
        )

    def _render_stale_confirmation(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> Screen:
        # Re-render confirmation in stale mode.
        return self._render_confirmation(sess, snap)

    def _render_source_changed(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> Screen:
        text = (
            f"⚠️ MT4 source changed since you opened this confirmation.\n\n"
            f"Was: {sess.snap_source} (seq={sess.snap_seq})\n"
            f"Now: {snap.source} (seq={snap.seq})\n\n"
            f"Please re-confirm with the new snapshot."
        )
        self._capture_snapshot_metadata(sess, snap)
        return self._render_confirmation(sess, snap)

    def _render_cycle_changed(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> Screen:
        fibo = snap.find_fibo(sess.symbol or "", sess.variant or "")
        if fibo is None:
            return self._render_symbol_gone(sess, snap)
        confirm_screen = self._render_confirmation(sess, snap)
        notice = (
            f"🔁 MT4 cycle_id for {sess.side} changed while you were navigating.\n\n"
            f"Was: {sess.snap_cycle_id}\n"
            f"Now: {fibo.side_cycle_id(sess.side or SIDE_BUY)}\n\n"
            f"Please re-confirm with the updated cycle.\n\n"
        )
        # Prepend the change notice to the confirmation screen.
        return Screen(
            text=notice + confirm_screen.text,
            buttons=confirm_screen.buttons,
        )

    def _render_weight_changed(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> Screen:
        fibo = snap.find_fibo(sess.symbol or "", sess.variant or "")
        if fibo is None:
            return self._render_symbol_gone(sess, snap)
        side = sess.side or SIDE_BUY
        new_w = fibo.side_cumulative_weight(side)
        new_target = (sess.starting_volume or Decimal("0")) * new_w
        confirm_screen = self._render_confirmation(sess, snap)
        notice = (
            f"🔁 MT4 cumulative weight for {side} changed.\n\n"
            f"Was: {sess.snap_cumulative_weight} → target "
            f"{(sess.starting_volume or Decimal('0')) * (sess.snap_cumulative_weight or Decimal('0'))}\n"
            f"Now: {new_w} → target {new_target}\n\n"
            f"Please re-confirm with the updated target.\n\n"
        )
        return Screen(
            text=notice + confirm_screen.text,
            buttons=confirm_screen.buttons,
        )

    def _render_symbol_gone(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> Screen:
        text = (
            f"⚠️ {sess.symbol} — {sess.variant} is no longer in the snapshot.\n\n"
            f"The MT4 Observer may have rotated instruments. Press Back and "
            f"pick again."
        )
        return Screen(
            text=text,
            buttons=[[
                {"text": "◀️ Back", "callback_data": CB_BACK},
                {"text": "❌ Cancel", "callback_data": CB_CANCEL},
            ]],
        )

    def _render_duplicate(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
        registration_key: str,
    ) -> Screen:
        # Drop session so the user must reopen via Start Fibo.
        self._sessions.reset(*sess.session_key)
        text = (
            f"⚠️ Already registered: {registration_key}\n\n"
            f"Phase 1 does not allow re-registration of an existing key. "
            f"Press Start Fibo again to register a different combination, "
            f"or Cancel to exit."
        )
        return Screen(
            text=text,
            buttons=[[{"text": "❌ Cancel", "callback_data": CB_CANCEL}]],
        )

    def _render_store_error(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
        detail: str,
    ) -> Screen:
        text = (
            f"⚠️ Registration store error.\n\n"
            f"Detail: {detail}\n\n"
            f"Press Back or Cancel and try again."
        )
        return Screen(
            text=text,
            buttons=[[
                {"text": "◀️ Back", "callback_data": CB_BACK},
                {"text": "❌ Cancel", "callback_data": CB_CANCEL},
            ]],
        )

    def _render_registered(self, registration: FiboRegistration) -> Screen:
        text = (
            f"✅ Registered: {registration.registration_key}\n\n"
            f"Exchange:    {registration.exchange}\n"
            f"Account:     {registration.account}\n"
            f"Symbol:      {registration.symbol}\n"
            f"Variant:     {registration.variant}\n"
            f"Side:        {registration.side}\n"
            f"Volume:      {registration.starting_volume}\n"
            f"Target:      {registration.desired_exchange_size}\n"
            f"Source:      {registration.source} (seq {registration.source_seq})\n"
            f"Cycle ID:    {registration.source_cycle_id}\n"
            f"Cum weight:  {registration.source_cumulative_weight}\n"
            f"Percentage:  {registration.source_percentage}\n\n"
            f"Persisted to ~/.hermes/fibo/registrations.jsonl"
        )
        return Screen(text=text, buttons=[], no_keyboard=True)

    def _render_restarted(self, registration: FiboRegistration) -> Screen:
        """Phase 2.7 render for a successfully reactivated
        registration. The screen confirms the reactivation and
        shows the canonical identity + the new mutable /
        snapshot fields.
        """
        text = (
            "✅ Fibo restarted\n\n"
            f"Source symbol:       {registration.source_symbol}\n"
            f"Exchange instrument: {registration.exchange_instrument}\n"
            f"Variant:             {registration.variant}\n"
            f"Side:                {registration.side}\n"
            f"Exchange:            {registration.exchange}\n"
            f"Account:             {registration.account}\n"
            f"Volume:              {registration.starting_volume}\n\n"
            "Fibo reconciliation is active again."
        )
        return Screen(text=text, buttons=[], no_keyboard=True)

    # ------------------------------------------------------------------
    # Format helper
    # ------------------------------------------------------------------

    @staticmethod
    def _format_confirmation(
        *,
        source_symbol: str,
        variant: str,
        side: str,
        exchange: str,
        account: str,
        exchange_instrument: str,
        starting_volume: Optional[Decimal],
        source: str,
        source_seq: int,
        cycle_id: int,
        cumulative_weight: Decimal,
        percentage: Decimal,
        desired_exchange_size: Decimal,
        snapshot_age: str,
    ) -> str:
        """UI polish (Phase 2.3): the final confirmation labels the
        two identities unambiguously.

        ``Source symbol``       — the MT4 / Observer symbol.
        ``Exchange instrument``  — the canonical OndoPerps contract.

        It is impossible to confuse the MT4 symbol with the venue
        contract on this screen.

        Layout:
            Source symbol:       <source_symbol>
            Exchange instrument: <canonical venue>
            Variant:             <variant>
            Side:                <side>
            Exchange:            <exchange>
            Account:             <account>
            Volume:              <vol>

            MT4 feed:            <source> (seq <seq>)
            MT4 cycle:           <cycle_id>
            MT4 weight:          <weight>
            MT4 %:               <percentage>

            Calc target:         <desired_size>
            Snapshot age:        <age>
        """
        vol = "?" if starting_volume is None else _fmt_decimal(starting_volume)
        if exchange_instrument:
            instrument_line = (
                f"Exchange instrument: {exchange_instrument}\n"
            )
        else:
            instrument_line = (
                "Exchange instrument: ⚠ not selected "
                "(NEEDS_INSTRUMENT_SELECTION)\n"
            )
        return (
            f"📋 Confirm registration:\n\n"
            f"Source symbol:       {source_symbol}\n"
            f"{instrument_line}"
            f"Variant:             {variant}\n"
            f"Side:                {side}\n"
            f"Exchange:            {exchange}\n"
            f"Account:             {account}\n"
            f"Volume:              {vol}\n\n"
            f"MT4 feed:            {source} (seq {source_seq})\n"
            f"MT4 cycle:           {cycle_id}\n"
            f"MT4 weight:          {_fmt_decimal(cumulative_weight)}\n"
            f"MT4 %:               {_fmt_decimal(percentage)}\n\n"
            f"Calc target:         {_fmt_decimal(desired_exchange_size)}\n"
            f"Snapshot age:        {snapshot_age}"
        )

    @staticmethod
    def _age_header(age: Optional[float]) -> str:
        if age is None:
            return ""
        if age < 0:
            age = 0.0
        return f"🕒 MT4 feed age: {age:.1f}s\n\n"

    @staticmethod
    def _parse_index(data: str, prefix_len: int) -> Optional[int]:
        rest = data[prefix_len:].strip()
        try:
            return int(rest)
        except ValueError:
            return None

    def _capture_snapshot_metadata(
        self,
        sess: FiboSession,
        snap: Mt4Snapshot,
    ) -> None:
        fibo = snap.find_fibo(sess.symbol or "", sess.variant or "")
        sess.snap_source = snap.source
        sess.snap_seq = snap.seq
        sess.snap_received_at = snap.received_at
        if fibo is not None and sess.side in (SIDE_BUY, SIDE_SELL):
            sess.snap_cycle_id = fibo.side_cycle_id(sess.side)
            sess.snap_cumulative_weight = fibo.side_cumulative_weight(sess.side)
            sess.snap_percentage = fibo.percentage

    def _validate_key(self, chat_id: Any, user_id: Any) -> Tuple[str, str]:
        c = str(chat_id or "").strip()
        u = str(user_id or "").strip()
        if not c or not u:
            raise ValueError(
                "Start Fibo flow requires both chat_id and user_id"
            )
        return (c, u)

    # TradeDesk shims. Failures (e.g. broken agent import) are caught
    # so a missing exchange does not crash the wizard.
    def _safe_list_exchanges(self) -> List[str]:
        try:
            result = self._list_exchanges()
        except Exception as exc:  # noqa: BLE001
            logger.warning("fibo_flow: list_exchanges failed: %s", exc)
            return []
        if not isinstance(result, list):
            return []
        return [str(x) for x in result if x]

    def _safe_list_accounts(self, exchange: str) -> List[Any]:
        try:
            result = self._list_accounts(exchange)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "fibo_flow: list_accounts(%s) failed: %s", exchange, exc
            )
            return []
        if not isinstance(result, list):
            return []
        return list(result)


def _account_id_for(entry: Any) -> Optional[str]:
    """Return the canonical account id for a ``list_accounts()``
    entry, or ``None`` if the entry cannot be normalized.

    Phase 2.4.1: an exchange agent's ``list_accounts()`` may return
    either plain strings (e.g. ``["bitget"]``) or dicts with an
    ``account`` sub-key plus optional ``chain`` / ``label``
    (e.g. Lighter's ``[{'account': 'amiroo', 'chain': 'ARBITRUM',
    'label': 'amiroo — Arbitrum'}, ...]``). The /trade shared
    wizard uses the same normalization (see
    ``plugins/trade/wizard.py::_account_option_parts``); Fibo
    applies the identical rule here so the two wizards agree
    on the canonical id passed to ``TradeDesk.execute`` calls.
    """
    if isinstance(entry, str):
        v = entry.strip()
        return v or None
    if isinstance(entry, dict):
        # Prefer the explicit ``account`` field; fall back to
        # ``label`` / ``name`` / ``id`` as a defensive fallback.
        for key in ("account", "name", "id"):
            v = entry.get(key)
            if v is None:
                continue
            v = str(v).strip()
            if v:
                return v
    return None


# ---------------------------------------------------------------------------
# Display-label normalization (Phase 2.4.2)
# ---------------------------------------------------------------------------
#
# The instrument-translation proposal screen used to hard-code the
# string ``OndoPerps:`` regardless of the selected exchange. That
# was a Phase 2.3-era cosmetic label. Phase 2.4 / 2.4.1 made the
# flow exchange-agnostic; the proposal screen now needs to reflect
# the actual selected venue dynamically.
#
# This helper is intentionally generic: it derives a human-readable
# label from the canonical exchange id string via the same
# ``TitleCase`` rule the /trade shared wizard applies. It is NOT
# a per-exchange table. Adding a new exchange requires no change
# here — the canonical id ``x_y_z`` becomes ``X Y Z``.


def _exchange_display_label(exchange_id: Optional[str]) -> str:
    """Return a human-readable display label for an exchange id.

    The label is generated purely from the canonical id string so
    this helper works for every current and future exchange
    without any per-exchange branching.

    Rules (Phase 2.4.2):
      * Empty / missing id → ``"Exchange"``.
      * Underscores separate words; the FIRST character of the
        exchange id is upper-cased, and the remainder of the
        string is preserved verbatim. This derives ``"Ondoperps"``
        from ``"ondoperps"`` and ``"EdgeX"`` from ``"edgex"``,
        matching the on-the-wire labeling convention used by
        deployed exchange agents today.

    Examples:
      ``"ondoperps"``   → ``"Ondoperps"``
      ``"hibachi"``     → ``"Hibachi"``
      ``"hyperliquid"`` → ``"Hyperliquid"``
      ``"lighter"``     → ``"Lighter"``
      ``"edgex"``       → ``"EdgeX"``
      ``"arcus"``       → ``"Arcus"``
      ``"rise"``        → ``"Rise"``
      ``"apex"``        → ``"Apex"``
      ``"pacifica"``    → ``"Pacifica"``
      ``"raydium"``     → ``"Raydium"``

    NOTE: This helper is intentionally dumb-derivation — adding
    a new exchange requires no change here. If a friendlier label
    is needed for one specific exchange, the canonical place to
    fix that is the exchange agent's own ``name = "..."``
    declaration (see ``x_<exchange>_agent.py``); this helper
    then picks up the corrected id. We do NOT add per-exchange
    special-cases here.
    """
    if not exchange_id:
        return "Exchange"
    raw = str(exchange_id).strip()
    if not raw:
        return "Exchange"
    # Upper-case only the first character; leave the rest of the
    # id verbatim. This preserves existing camelcase like
    # ``"EdgeX"`` while deriving ``"Ondoperps"`` from
    # ``"ondoperps"`` etc.
    return raw[0].upper() + raw[1:]


def _fmt_decimal(value: Decimal) -> str:
    """Format a Decimal for display, preserving user-entered precision."""
    if not value.is_finite():
        return str(value)
    # Use the normalized form so trailing zeros from the user's input
    # are preserved (e.g. 0.10 -> 0.10, not 0.1).
    try:
        return format(value, "f")
    except Exception:
        return str(value)


# ---------------------------------------------------------------------------
# Defaults used by ``fibo_wizard`` shim
# ---------------------------------------------------------------------------


def default_snapshot_store(hermes_home: Path) -> Mt4SnapshotStore:
    return Mt4SnapshotStore(Path(hermes_home) / "fibo" / "mt4_snapshot.json")


def default_registration_store(hermes_home: Path) -> FiboRegistrationStore:
    return FiboRegistrationStore(
        Path(hermes_home) / "fibo" / "registrations.jsonl"
    )


__all__ = [
    "STALE_THRESHOLD_SECONDS",
    "CB_PREFIX",
    "CB_SYM",
    "CB_SIDE",
    "CB_EX",
    "CB_ACCT",
    "CB_INST",
    "CB_AGREE",
    "CB_OTHER",
    "CB_BROWSE",
    "CB_BROWSEPG",
    "CB_INSTSEL",
    "CB_CAND",
    "CB_INSTFAIL_RETRY",
    "CB_CREATE",
    "CB_BACK",
    "CB_CANCEL",
    "CB_REFRESH",
    "CB_VCONFIRM",
    "SIDE_TOKEN_BUY",
    "SIDE_TOKEN_SELL",
    "Screen",
    "StartFiboFlow",
    "default_snapshot_store",
    "default_registration_store",
]