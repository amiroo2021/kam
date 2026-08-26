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
CB_CREATE = "fibo:s:create"
CB_BACK = "fibo:s:back"
CB_CANCEL = "fibo:s:cancel"
CB_REFRESH = "fibo:s:refresh"
CB_VCONFIRM = "fibo:s:v"        # volume-confirmed ack (used after text input)


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
        session_store: Optional[FiboSessionStore] = None,
        stale_threshold_seconds: float = STALE_THRESHOLD_SECONDS,
        now_fn=None,
    ) -> None:
        # ``list_exchanges_fn`` is a callable returning ``List[str]``.
        # ``list_accounts_fn(exchange: str) -> List[Any]``.
        # Injected so the flow can be unit-tested without a real
        # TradeDesk; in production these are bound to the live
        # TradeDesk helpers.
        self._snapshot_store = snapshot_store
        self._registration_store = registration_store
        self._list_exchanges = list_exchanges_fn
        self._list_accounts = list_accounts_fn
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

        return self._render_invalid_callback(key)

    def handle_text(
        self,
        chat_id: Any,
        user_id: Any,
        text: str,
    ) -> Optional[Screen]:
        """Consume free-text volume input ONLY when the user's session
        is in AWAITING_VOLUME. Returns None when the wizard should not
        intercept (the underlying adapter can route the message to its
        normal handler).

        Per spec §6: text interception belongs only to the Start Fibo
        session manager.
        """
        try:
            key = self._validate_key(chat_id, user_id)
        except ValueError:
            return None
        sess = self._sessions.get(*key)
        if sess is None or not sess.is_awaiting_volume():
            return None
        raw = (text or "").strip()
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
            sess.choices_accounts = sorted(
                self._safe_list_accounts(str(sess.exchange))
            )
        except Exception:
            sess.choices_accounts = []
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
        if snap is None:
            return self._render_no_data(sess)
        idx = self._parse_index(data, len(CB_ACCT))
        if idx is None or idx >= len(sess.choices_accounts):
            return self._render_invalid_callback(sess.session_key)
        sess.account = sess.choices_accounts[idx]
        sess.state = SessionState.AWAITING_VOLUME
        return self._render_volume_screen(sess, snap)

    def _handle_back(
        self,
        sess: FiboSession,
        snap: Optional[Mt4Snapshot],
    ) -> Screen:
        # Back navigates one step up; cannot go past symbol+variant.
        prev = sess.state
        if prev == SessionState.AWAITING_VOLUME:
            sess.starting_volume = None
            sess.state = SessionState.AWAITING_ACCOUNT
            return self._render_account_screen(sess, snap) if snap else self._render_no_data(sess)
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
                symbol=sess.symbol,
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
            return self._render_duplicate(sess, snap, exc.registration_key)
        except Exception as exc:  # noqa: BLE001 - surface generically
            logger.error(
                "fibo_flow: registration append failed: %s", exc
            )
            return self._render_store_error(sess, snap, str(exc))

        self._sessions.reset(*sess.session_key)
        return self._render_registered(registration)

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
            symbol=sess.symbol or "?",
            variant=sess.variant or "?",
            side=side,
            exchange=sess.exchange or "?",
            account=sess.account or "?",
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

    # ------------------------------------------------------------------
    # Format helper
    # ------------------------------------------------------------------

    @staticmethod
    def _format_confirmation(
        *,
        symbol: str,
        variant: str,
        side: str,
        exchange: str,
        account: str,
        starting_volume: Optional[Decimal],
        source: str,
        source_seq: int,
        cycle_id: int,
        cumulative_weight: Decimal,
        percentage: Decimal,
        desired_exchange_size: Decimal,
        snapshot_age: str,
    ) -> str:
        vol = "?" if starting_volume is None else _fmt_decimal(starting_volume)
        return (
            f"📋 Confirm registration:\n\n"
            f"Symbol:      {symbol}\n"
            f"Variant:     {variant}\n"
            f"Side:        {side}\n"
            f"Exchange:    {exchange}\n"
            f"Account:     {account}\n"
            f"Volume:      {vol}\n"
            f"MT4 source:  {source} (seq {source_seq})\n"
            f"MT4 cycle:   {cycle_id}\n"
            f"MT4 weight:  {_fmt_decimal(cumulative_weight)}\n"
            f"MT4 %:       {_fmt_decimal(percentage)}\n"
            f"Calc target: {_fmt_decimal(desired_exchange_size)} "
            f"(preview — exchange position not yet inspected)\n"
            f"Snapshot age: {snapshot_age}"
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