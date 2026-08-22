"""GoldenFibo v1 polling engine.

The engine is a pure state machine. It is given:
- a config (GoldenFiboConfig)
- a state (GoldenFiboState)
- an adapter (LighterFiboAdapter)
- a deterministic client_order_id factory

The engine advances the state through Cases A/B/C/D per the locked
spec. It never assumes fills; it never infers FILLED from absence
alone; it ALWAYS correlates pending disappearance with the live
position size to safely advance a step.

Persistence: the engine mutates state in place. The caller is
responsible for persisting state.to_dict() after each tick (the
service does this).
"""

from __future__ import annotations

import time

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from plugins.trade.golden_fibo.config import (
    GoldenFiboConfig,
    MAX_STEP,
    golden_fibo_next_ladder_price,
    golden_fibo_tp_price,
    golden_fibo_volume,
)
from plugins.trade.golden_fibo.client_id_v2 import (
    ROLE_EMERGENCY_CLOSE as V2_ROLE_EMERGENCY_CLOSE,
    ROLE_LADDER_ENTRY as V2_ROLE_LADDER_ENTRY,
    ROLE_SHARED_TP as V2_ROLE_SHARED_TP,
    ROLE_STEP0 as V2_ROLE_STEP0,
    STEP_UNKNOWN,
    ClientIdError,
    SeqExhaustedError,
    allocate_client_id,
    allocate_cycle_uid,
)
from plugins.trade.golden_fibo.state import (
    ROLE_ENTRY,
    ROLE_LADDER,
    ROLE_TP,
    SHUTDOWN_MODE_SMOOTH,
    STATUS_COMPLETED,
    STATUS_NEEDS_RECOVERY,
    STATUS_RUNNING,
    STATUS_SMOOTH_SHUTDOWN,
    SUBMISSION_ATTEMPTED,
    SUBMISSION_CONFIRMED,
    SUBMISSION_NEEDS_RECOVERY,
    SUBMISSION_NOT_SUBMITTED,
    SUBMISSION_PREPARED,
    GoldenFiboState,
)


# Bounded reconciliation window for a FILLED TP whose position read has not
# yet gone flat. The venue's position read can lag the TP fill by a poll or
# two; we allow this many polls before declaring the exit stuck
# (TP_PARTIAL_EXIT_NOT_FLAT) and freezing. Never continue indefinitely with
# no active TP.
TP_EXIT_MAX_POLLS = 4


OrderIdentity = Tuple[Optional[int], Optional[int]]  # (client_order_id, exchange_order_id)


def coerce_exchange_order_id(value: Any) -> Optional[Any]:
    """Return ``value`` as the venue-native exchange_order_id, opaquely.

    Most venues (Lighter, Rise, Arcus) hand back decimal-int ids that fit
    directly into GoldenFiboState's ``Optional[Union[int, str]]`` slot.
    Ondo Perps hands back 32-char alphanumeric strings — we MUST preserve
    them verbatim because the venue rejects anything else on cancel /
    lookup. Returning the value unchanged (after stripping) is the
    safest portable behaviour across both kinds of venue.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    text = str(value).strip()
    return text or None


@dataclass
class TickResult:
    """Outcome of one engine tick."""

    state: GoldenFiboState
    actions: List[str]  # human-readable summary of what happened


class GoldenFiboEngine:
    """Stateless-ish state machine: holds the current state and the
    static config, drives state through one tick at a time."""

    def __init__(
        self,
        config: GoldenFiboConfig,
        state: GoldenFiboState,
        adapter: Any,
        client_order_id_factory: Optional[Callable[[], int]] = None,
        *,
        exchange_highest_cycle_uid: Optional[int] = None,
    ) -> None:
        self.config = config
        self.state = state
        self.adapter = adapter
        # Legacy factory kept only for unit tests that inject a simple counter.
        # Production paths always use V2 when client_id_version >= 2 (default).
        self._next_client_id = client_order_id_factory
        self._exchange_highest_cycle_uid = exchange_highest_cycle_uid
        self._venue_constraints_cache: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # V2 client_order_index allocation (stateful, restart-safe)
    # ------------------------------------------------------------------
    def _use_v2_ids(self) -> bool:
        """Production default is V2. Legacy factory is used only when
        ``state.client_id_version < 2`` (explicit opt-out for older tests).
        """
        return int(getattr(self.state, "client_id_version", 2) or 2) >= 2

    def _begin_new_cycle_uid(self) -> int:
        """Allocate a durable CYCLE_UID for a brand-new cycle; reset SEQ map."""
        prev = int(self.state.highest_cycle_uid or 0) or None
        # Prefer watermark; also consider current cycle_uid if set
        if self.state.cycle_uid:
            prev = max(prev or 0, int(self.state.cycle_uid))
        uid = allocate_cycle_uid(
            previous_local_cycle_uid=prev,
            highest_exchange_cycle_uid=self._exchange_highest_cycle_uid,
        )
        self.state.cycle_uid = int(uid)
        self.state.highest_cycle_uid = max(int(self.state.highest_cycle_uid or 0), int(uid))
        self.state.client_seq_by_role_step = {}
        self.state.client_id_version = 2
        return int(uid)

    def _allocate_v2_client_id(
        self,
        *,
        role_code: int,
        step: int,
        engine_role: str,
    ) -> int:
        """Allocate or reuse V2 client id for a logical order.

        Reuses submission_client_id when the same logical submission is
        already PREPARED/ATTEMPTED (idempotent retry path).
        """
        if (
            self.state.submission_client_id is not None
            and self.state.submission_role == engine_role
            and self.state.submission_step == step
            and self.state.submission_phase
            in (SUBMISSION_PREPARED, SUBMISSION_ATTEMPTED, SUBMISSION_NEEDS_RECOVERY)
        ):
            # Same logical order — never mint a new ID.
            return int(self.state.submission_client_id)

        if not self.state.cycle_uid:
            # Safety: should have been set at cycle start.
            self._begin_new_cycle_uid()

        try:
            return allocate_client_id(
                direction=self.config.direction,
                role=role_code,
                cycle_uid=int(self.state.cycle_uid),
                step=int(step),
                seq_map=self.state.client_seq_by_role_step,
            )
        except SeqExhaustedError as exc:
            # Freeze rather than wrap/duplicate.
            raise ClientIdError(str(exc)) from exc

    def _next_id_for(
        self,
        *,
        role_code: int,
        step: int,
        engine_role: str,
    ) -> int:
        if self._use_v2_ids():
            return self._allocate_v2_client_id(
                role_code=role_code, step=step, engine_role=engine_role
            )
        if self._next_client_id is None:
            raise ClientIdError("no client id factory and V2 disabled")
        return int(self._next_client_id())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def tick(self) -> TickResult:
        """Run one polling cycle. Returns updated state and an action log.

        The engine never assumes a fill. It must observe the live
        venue state, then choose a single safe action.
        """
        actions: List[str] = []

        # Read live state
        try:
            position = self.adapter.position_state(
                self.config.account, self.config.instrument
            )
        except Exception as exc:
            return self._freeze(f"position_state failed: {exc}")

        live_size = _parse_size(position.get("size"))
        live_side = position.get("side")  # "long", "short", or None
        has_position = live_size > 0 and live_side in ("long", "short")

        # Validate live direction matches our configured direction
        if has_position:
            live_direction = "BUY" if live_side == "long" else "SELL"
            if live_direction != self.config.direction:
                return self._freeze(
                    f"position direction mismatch: live={live_side} config={self.config.direction}"
                )

        # Read the pending-ladder state up front (fallback-aware). If the
        # pending Step(n) is FILLED, the full-step transition handles the TP
        # replacement + next step in one shot, so the TP-volume-sync at the
        # OLD price must NOT also fire on this tick. This enforces the locked
        # ordering: read pending -> decide (partial vs full) -> act.
        pending_filled: bool = False
        pending_state: Dict[str, Any] = {}
        # Read pending-ladder state only when there's actually a pending
        # ladder to check. The pending-RoleEntry path is handled by the
        # service's _maybe_confirm_step0 (which calls the client-id
        # lookup separately) — calling the lookup here for ROLE_ENTRY
        # would double the per-tick lookup count. Critically: when the
        # lookup IS run, the client-id fallback inside
        # _read_pending_order_state handles the case where
        # ``pending_order_exchange_id`` is None (Ondo alphanumeric ids
        # do not fit the legacy int slot). Gating this on a non-None
        # oid would leave such pending ladders unrecoverable.
        if self.state.pending_order_role == ROLE_LADDER:
            pending_state = self._read_pending_order_state()
            pending_status = str(pending_state.get("status") or "")
            pending_taxonomy = str(pending_state.get("taxonomy") or "")
            if pending_state:
                actions.append(
                    f"pending oid={self.state.pending_order_exchange_id} status={pending_status} taxonomy={pending_taxonomy}"
                )
            if pending_taxonomy == "ACTIVE":
                pass  # partial: TP-volume sync applies below
            elif pending_taxonomy == "FILLED":
                # Full fill: route to the full-step transition. The TP-volume
                # sync at the OLD price must NOT fire; _handle_confirmed_fill
                # replaces the TP ONCE at the NEW logical TP price + full live
                # position size.
                pending_filled = True
            elif pending_taxonomy in ("CANCELED", "REJECTED", "EXPIRED"):
                return self._freeze(
                    f"pending order unexpectedly {pending_taxonomy.lower()}"
                )

        # Read the shared TP state up front (needed to distinguish a legitimate
        # TP exit from unexpected position shrinkage below).
        tp_state: Dict[str, Any] = {}
        tp_taxonomy = ""
        if has_position and self.state.current_tp_order_id is not None:
            tp_state = self._read_tp_state()
            tp_taxonomy = str(tp_state.get("taxonomy") or "")

        # Validate accumulated size. This guards against unexpected position
        # shrinkage DURING normal operation. It must NOT fire when the shared
        # TP has legitimately FILLED (a TP exit intentionally reduces the
        # position below the accumulated size); that case is handled by the
        # TP-exit reconciliation below.
        if self.state.highest_filled_step >= 0 and tp_taxonomy != "FILLED":
            expected = self.config.cumulative_volume(self.state.highest_filled_step)
            if has_position and live_size < expected:
                return self._freeze(
                    f"position size shrunk unexpectedly: live={live_size} expected>={expected}"
                )

        # TP liveness + volume synchronization: while a position is open and a
        # shared TP is expected (current_tp_order_id set), the TP order must
        # remain ACTIVE and its volume must equal the ACTUAL live position
        # volume (independent of logical-step completion).
        #
        # A FILLED TP is the legitimate exit (position closing); it is NOT a
        # freeze. Only a genuine CANCELED/REJECTED/missing-without-fill TP
        # while the position remains open means "unprotected" -> NEEDS_RECOVERY.
        # We do NOT silently continue and we do NOT auto re-arm a new TP.
        if has_position and self.state.current_tp_order_id is not None:
            actions.append(
                f"tp oid={self.state.current_tp_order_id} taxonomy={tp_taxonomy}"
            )
            if tp_taxonomy == "ACTIVE":
                # TP healthy. Reset any prior exit-reconciliation counter.
                self.state.tp_exit_attempts = 0
                # PARTIAL-fill TP-volume sync (same price, full live size).
                # Skip when the pending ladder is FILLED on this tick — the
                # full-step transition (_handle_confirmed_fill -> _rotate_tp)
                # replaces the TP once at the NEW logical TP price + full live
                # size, so the at-old-price sync must NOT also fire.
                if not pending_filled:
                    live_tp_size = self._decimal_or_none(tp_state.get("requested_size"))
                    if live_tp_size is not None and live_tp_size != live_size:
                        sync = self._sync_tp_volume(live_size, actions)
                        if sync is not None:
                            return sync
            elif tp_taxonomy == "FILLED":
                # TP filled but the position read still shows exposure. This is
                # either (a) the position read lagging the fill (normal exit in
                # progress) or (b) a genuine partial TP fill. Allow a BOUNDED
                # number of reconciliation polls for the position to read flat;
                # do NOT freeze and do NOT mutate the ladder during the window.
                # If the position is still not flat after the bound, the exit is
                # stuck -> NEEDS_RECOVERY (never silently continue forever with
                # no active TP).
                self.state.tp_exit_attempts = int(self.state.tp_exit_attempts or 0) + 1
                actions.append(
                    f"shared TP oid={self.state.current_tp_order_id} FILLED; "
                    f"exit in progress (position read size={live_size}); "
                    f"reconciliation poll {self.state.tp_exit_attempts}/{TP_EXIT_MAX_POLLS}"
                )
                if self.state.tp_exit_attempts >= TP_EXIT_MAX_POLLS:
                    return self._freeze(
                        f"shared TP oid={self.state.current_tp_order_id} FILLED but "
                        f"position still not flat after {TP_EXIT_MAX_POLLS} polls "
                        f"(size={live_size}); TP_PARTIAL_EXIT_NOT_FLAT. Reconcile exchange state."
                    )
                return TickResult(state=self.state, actions=actions)
            else:
                # CANCELED / REJECTED / EXPIRED / missing -> unprotected position.
                return self._freeze(
                    f"shared TP oid={self.state.current_tp_order_id} unexpectedly "
                    f"{tp_taxonomy or 'missing'} while position open (size={live_size}); "
                    "position is unprotected. Reconcile exchange state before any resubmission."
                )

        # Check pending order state for Case B/cycle-end branching. The
# fallback-aware read already happened above; pending_state / pending_filled
# are populated. If the pending order is FILLED, the full-step transition
# handles it here (cancel old TP once, place new TP at logical P(n-1) for
# live size, then place Step(n+1)).
        pending_alive = False
        if pending_filled:
            return self._handle_confirmed_fill(actions)
        # Use the resolved pending_state taxonomy from the fallback-aware
        # client-id lookup (line 250) rather than gating on
        # pending_order_exchange_id. Ondo's alphanumeric ids leave
        # pending_order_exchange_id as None even when the order is live.
        if pending_state:
            pending_status = str(pending_state.get("status") or "")
            pending_taxonomy = str(pending_state.get("taxonomy") or "")
            if pending_taxonomy == "ACTIVE":
                pending_alive = True
            elif pending_taxonomy in ("CANCELED", "REJECTED", "EXPIRED"):
                return self._freeze(
                    f"pending order unexpectedly {pending_taxonomy.lower()}"
                )

        # Determine state-match case
        if not has_position:
            if self.state.pending_order_exchange_id is not None and pending_alive:
                # Case B: TP closed position while ladder pending
                return self._handle_orphan_pending(actions)
            if self.state.highest_filled_step >= 0 or self.state.next_step > 0:
                # Case B: cycle ended, no orphan (already cleared)
                pass
            # Case A possible: position=0, no pending, no progress
            if (
                self.state.highest_filled_step < 0
                and self.state.next_step == 0
                and self.state.pending_order_exchange_id is None
            ):
                return self._start_fresh_cycle(actions)
            # Already past Step0 but position is now 0 — TP exit
            return self._handle_cycle_end(actions)

        # has_position
        # Gate on pending_alive (resolved order state from the
        # fallback-aware client-id lookup) rather than
        # pending_order_exchange_id (legacy numeric slot). Ondo's
        # alphanumeric ids leave pending_order_exchange_id as None even
        # when the order is live and verifiable via client-id.
        if self.state.pending_order_exchange_id is None and not pending_alive:
            # Case C: position exists but expected pending ladder is absent
            return self._handle_missing_pending(position, live_size, actions)

        if not pending_alive:
            # Pending not in active surface and not from get_order_state
            # Either filled or otherwise gone. Use position delta.
            return self._handle_missing_pending(position, live_size, actions)

        # Case D: healthy waiting state
        actions.append("healthy waiting")
        return TickResult(state=self.state, actions=actions)

    def _is_smooth_shutdown(self) -> bool:
        mode = str(getattr(self.state, "shutdown_mode", "") or "")
        status = str(self.state.status or "")
        return mode == SHUTDOWN_MODE_SMOOTH or status == STATUS_SMOOTH_SHUTDOWN

    def _complete_smooth_shutdown(self, actions: List[str]) -> TickResult:
        """Current cycle finished under Smooth Shutdown — no new Step0."""
        actions.append("smooth_shutdown_complete: deregister (no new Step0)")
        self.state.status = STATUS_COMPLETED
        self.state.shutdown_mode = SHUTDOWN_MODE_SMOOTH
        self.state.freeze_reason = "smooth_shutdown_complete"
        # Clear cycle-scoped identities so list/detail show clean terminal state
        self.state.pending_order_role = None
        self.state.pending_order_client_id = None
        self.state.pending_order_exchange_id = None
        self.state.pending_requested_price = None
        self.state.pending_requested_size = None
        self.state.pending_confirmed_price = None
        self.state.pending_confirmed_size = None
        self.state.current_tp_client_id = None
        self.state.current_tp_order_id = None
        self.state.current_tp_role = None
        self.state.current_tp_price = None
        self.state.current_tp_size = None
        self.state.tp_exit_attempts = 0
        self.state.highest_filled_step = -1
        self.state.fill_prices = {}
        self.state.expected_cumulative_size = Decimal("0")
        self.state.next_step = 0
        self.state.submission_phase = SUBMISSION_NOT_SUBMITTED
        self.state.submission_client_id = None
        self.state.submission_step = None
        self.state.submission_role = None
        self.state.submission_attempted_at = None
        self.state.submission_exchange_order_id = None
        return TickResult(state=self.state, actions=actions)

    # ------------------------------------------------------------------
    # Case A: start a new cycle
    # ------------------------------------------------------------------
    def _start_fresh_cycle(self, actions: List[str]) -> TickResult:
        """Issue market Step0 with durable submission tracking.

        Invariant: once SUBMISSION_ATTEMPTED is persisted, an exception
        or unknown response MUST NOT permit automatic resubmission of
        the same logical Step0. The exchange must be reconciled first.

        If we re-enter this path with a prior SUBMISSION_ATTEMPTED
        still unresolved, we freeze instead of resubmitting.

        Smooth Shutdown: never start a fresh Step0 — complete instead.
        """
        if self._is_smooth_shutdown():
            return self._complete_smooth_shutdown(actions)
        # Guard: never resubmit an already-attempted or confirmed Step0.
        if self.state.submission_phase in (
            SUBMISSION_ATTEMPTED,
            SUBMISSION_CONFIRMED,
            SUBMISSION_NEEDS_RECOVERY,
        ):
            return self._freeze(
                "Step0 already attempted/confirmed (submission_phase="
                f"{self.state.submission_phase}); "
                "reconcile exchange state before any resubmission"
            )

        size = self.config.step0_volume
        order_side = self.config.direction.lower()  # "buy" or "sell"

        # Deterministic client identity per logical order.
        # V2 (default): cycle_uid + role/step/seq. Legacy: injected factory.
        try:
            if self._use_v2_ids():
                self._begin_new_cycle_uid()
                client_id = self._next_id_for(
                    role_code=V2_ROLE_STEP0, step=0, engine_role=ROLE_ENTRY
                )
            else:
                if self._next_client_id is None:
                    raise ClientIdError("legacy factory missing")
                client_id = int(self._next_client_id())
        except ClientIdError as exc:
            return self._freeze(f"client_id allocation failed: {exc}")

        # PREPARE: persist everything BEFORE the venue call.
        # Cycle-boundary reset: every cycle-scoped field must reflect ONLY the
        # current cycle. The previous cycle's step_orders map must NOT survive
        # into the new cycle (historical evidence belongs in forensic logs,
        # not the active state machine).
        self.state.cycle_id += 1
        # Note: cycle_uid already allocated; client_seq_by_role_step already reset
        # inside _begin_new_cycle_uid. Do not clear cycle_uid here.
        self.state.next_step = 0
        self.state.highest_filled_step = -1
        self.state.fill_prices = {}
        self.state.expected_cumulative_size = Decimal("0")
        self.state.step_orders = {}
        self.state.current_tp_price = None
        self.state.current_tp_size = None
        self.state.current_tp_order_id = None
        self.state.current_tp_client_id = None
        self.state.current_tp_role = None
        self.state.tp_exit_attempts = 0
        self.state.pending_order_client_id = client_id
        self.state.pending_order_exchange_id = None
        self.state.pending_requested_price = None
        self.state.pending_requested_size = size
        self.state.pending_confirmed_price = None
        self.state.pending_confirmed_size = None
        self.state.pending_order_role = ROLE_ENTRY
        self.state.status = STATUS_RUNNING
        self.state.freeze_reason = None
        self.state.submission_phase = SUBMISSION_PREPARED
        self.state.submission_client_id = client_id
        self.state.submission_step = 0
        self.state.submission_role = ROLE_ENTRY
        self.state.submission_attempted_at = None
        self.state.submission_exchange_order_id = None

        # ATTEMPT: persist immediately before dispatch.
        self.state.submission_phase = SUBMISSION_ATTEMPTED
        self.state.submission_attempted_at = time.time()

        try:
            submit = self.adapter.place_market(
                account=self.config.account,
                instrument=self.config.instrument,
                side=order_side,
                size=size,
                client_order_id=client_id,
            )
        except Exception as exc:
            # Submission was attempted; result unknown. NEVER auto-retry.
            self.state.submission_phase = SUBMISSION_NEEDS_RECOVERY
            return self._freeze(
                f"place_market Step0 attempted but result unknown: {exc}. "
                f"Reconcile exchange before any resubmission."
            )

        # CONFIRMED: submission accepted by venue.
        self.state.submission_phase = SUBMISSION_CONFIRMED
        actions.append(f"Step0 MARKET submit={submit}")
        exchange_oid = submit.get("exchange_order_id")
        if exchange_oid is not None and exchange_oid != "":
            # Venue-native order identifier: store opaquely. The adapter
            # is responsible for producing the right form for its venue
            # (decimal int for Lighter/Rise/Arcus, alphanumeric for Ondo).
            self.state.pending_order_exchange_id = exchange_oid
            self.state.submission_exchange_order_id = exchange_oid
        self.state.pending_confirmed_size = submit.get("submitted_volume") or str(size)
        return TickResult(state=self.state, actions=actions)

    # ------------------------------------------------------------------
    # Case A continues: confirm Step0 filled, persist P0, set TP, place Step1
    # ------------------------------------------------------------------
    def confirm_step0_filled(self, p0: Decimal) -> None:
        """Called by the service after observing the live venue state
        shows the Step0 market FILLED and the position established.

        Preserves the Step0 ENTRY order identity in step_orders[0] BEFORE
        clearing the generic pending/submission fields, so the identity of
        the order that created Step0 is never lost."""
        # Capture the Step0 ENTRY identity before clearing the generic fields.
        entry_client_id = self.state.pending_order_client_id or self.state.submission_client_id
        entry_exchange_oid = self.state.pending_order_exchange_id or self.state.submission_exchange_order_id
        self.state.step_orders[0] = {
            "role": ROLE_ENTRY,
            "client_id": entry_client_id,
            "exchange_order_id": entry_exchange_oid,
            "status": "filled",
            "price": str(p0),
            "size": str(self.config.step0_volume),
        }
        self.state.fill_prices[0] = Decimal(p0)
        self.state.highest_filled_step = 0
        self.state.expected_cumulative_size = self.config.cumulative_volume(0)
        self.state.next_step = 1
        self.state.pending_order_role = None
        self.state.pending_order_client_id = None
        self.state.pending_order_exchange_id = None
        self.state.pending_requested_price = None
        self.state.pending_requested_size = None
        self.state.pending_confirmed_price = None
        self.state.pending_confirmed_size = None
        # Step0 confirmed; clear durable submission tracking so the
        # next logical order (TP, ladder) gets its own fresh record.
        self.state.submission_phase = SUBMISSION_NOT_SUBMITTED
        self.state.submission_client_id = None
        self.state.submission_step = None
        self.state.submission_role = None
        self.state.submission_attempted_at = None
        self.state.submission_exchange_order_id = None

    def place_step0_tp_and_step1(self, p0: Decimal) -> Optional[TickResult]:
        """Place the shared TP0 and the Step1 LIMIT. Returns TickResult
        on failure, or None on success (the caller persists state)."""
        result = self._rotate_tp(p0)
        if result is not None:
            return result
        return self._place_next_ladder()

    def _rotate_tp(self, pk: Decimal) -> Optional[TickResult]:
        """Set the shared TP for the current accumulated position.

        For k=0 tpPrice = TP0 = P0 * (1 ± percentage).
        For k>=1 tpPrice = P(k-1).
        """
        actions: List[str] = []
        # Determine tp price
        if self.state.highest_filled_step == 0:
            tp_price = golden_fibo_tp_price(self.config.direction, pk, self.config.percentage)
        else:
            # TPk = P(k-1)
            prev_step = self.state.highest_filled_step - 1
            prev_pk = self.state.fill_prices.get(prev_step)
            if prev_pk is None:
                return self._freeze(
                    f"missing fill_prices[{prev_step}] for TP rotation"
                )
            tp_price = Decimal(prev_pk)

        tp_size = self.config.cumulative_volume(self.state.highest_filled_step)

        # Closing side for the shared TP is opposite the robot direction.
        tp_side = "sell" if self.config.direction.upper() == "BUY" else "buy"

        # Guard: never resubmit an already-attempted TP for this step.
        if (
            self.state.submission_phase == SUBMISSION_ATTEMPTED
            and self.state.submission_role == ROLE_TP
            and self.state.submission_step == self.state.highest_filled_step
        ):
            return self._freeze(
                f"TP for step {self.state.highest_filled_step} already attempted; "
                "reconcile exchange state before any resubmission"
            )

        # Validate TP notional >= venue min_quote (resting LIMIT TP is subject
        # to the ordinary LIMIT minimum-notional rule). Do NOT silently resize.
        vc = self._venue_constraints()
        if vc is not None:
            min_quote = vc.get("min_quote_amount") or Decimal("0")
            notional = Decimal(tp_size) * Decimal(tp_price)
            if min_quote > 0 and notional < min_quote:
                return self._freeze(
                    f"shared TP notional {notional} below venue minimum {min_quote} "
                    f"(size={tp_size} price={tp_price}); refusing to place TP. "
                    "Reconcile configuration before any resubmission."
                )

        try:
            tp_step = max(0, int(self.state.highest_filled_step))
            tp_client_id = self._next_id_for(
                role_code=V2_ROLE_SHARED_TP,
                step=tp_step,
                engine_role=ROLE_TP,
            )
        except ClientIdError as exc:
            return self._freeze(f"TP client_id allocation failed: {exc}")

        # Cancel the previous shared TP (if any) BEFORE placing the new one.
        # The shared TP is a single resting reduce-only LIMIT; replacing it
        # means canceling the old resting order first. This uses the fixed
        # exact-order cancel path.
        old_tp_oid = self.state.current_tp_order_id
        if old_tp_oid is not None:
            try:
                canceled = self.adapter.cancel_order(
                    account=self.config.account,
                    order_index=old_tp_oid,
                )
            except Exception as exc:
                return self._freeze(
                    f"cancel previous shared TP oid={old_tp_oid} failed: {exc}. "
                    "Reconcile exchange before any resubmission."
                )
            if not canceled:
                return self._freeze(
                    f"cancel previous shared TP oid={old_tp_oid} returned False. "
                    "Reconcile exchange before any resubmission."
                )
            actions.append(f"canceled previous shared TP oid={old_tp_oid}")

        # PREPARE + ATTEMPT durable record for the TP submission.
        self.state.submission_phase = SUBMISSION_PREPARED
        self.state.submission_client_id = tp_client_id
        self.state.submission_step = self.state.highest_filled_step
        self.state.submission_role = ROLE_TP
        self.state.submission_attempted_at = None
        self.state.submission_exchange_order_id = None
        self.state.submission_phase = SUBMISSION_ATTEMPTED
        self.state.submission_attempted_at = time.time()

        try:
            submit = self.adapter.set_shared_tp(
                account=self.config.account,
                instrument=self.config.instrument,
                price=tp_price,
                side=tp_side,
                size=tp_size,
                client_order_id=tp_client_id,
            )
        except Exception as exc:
            self.state.submission_phase = SUBMISSION_NEEDS_RECOVERY
            return self._freeze(
                f"set shared TP attempted but result unknown: {exc}. "
                f"Reconcile exchange before any resubmission."
            )

        # The resting LIMIT TP submit + verify is done inside the generic
        # new_order path; a failure is surfaced as an exception above.
        self.state.submission_phase = SUBMISSION_CONFIRMED
        self.state.submission_exchange_order_id = coerce_exchange_order_id(
            submit.get("exchange_order_id")
        )

        self.state.current_tp_price = Decimal(str(submit.get("submitted_price") or tp_price))
        self.state.current_tp_size = Decimal(str(submit.get("submitted_volume") or tp_size))
        self.state.current_tp_client_id = tp_client_id
        self.state.current_tp_order_id = coerce_exchange_order_id(
            submit.get("exchange_order_id")
        )
        self.state.current_tp_role = ROLE_TP
        actions.append(
            f"shared TP set price={self.state.current_tp_price} size={self.state.current_tp_size} oid={self.state.current_tp_order_id}"
        )
        return None

    def _place_next_ladder(self) -> Optional[TickResult]:
        """Place the next ladder LIMIT. Returns TickResult on failure."""
        actions: List[str] = []
        if self.state.next_step > MAX_STEP:
            # Step20 already placed; nothing to do
            return None
        next_n = self.state.next_step
        pk = self.state.fill_prices.get(next_n - 1)
        if pk is None:
            return self._freeze(
                f"missing P{next_n - 1} for next ladder placement"
            )
        # TPk for the new step
        if next_n == 1:
            tpk = golden_fibo_tp_price(self.config.direction, pk, self.config.percentage)
        else:
            tpk = Decimal(self.state.fill_prices.get(next_n - 2))  # P(k-1) for k>=1
        if tpk is None:
            return self._freeze(f"missing TP{next_n} level")
        next_price = golden_fibo_next_ladder_price(self.config.direction, pk, tpk)
        next_size = self.config.volume(next_n)

        # One-step-ahead venue validation at placement time: validate THIS
        # next order (positive valid price, valid size, valid increment,
        # notional >= venue minimum) right before placing it. On failure,
        # freeze (NEEDS_RECOVERY) and do NOT place the order. Deeper steps
        # are not speculated about.
        venue = self._venue_constraints()
        if venue is not None:
            from .preflight import validate_next_ladder_step
            check = validate_next_ladder_step(
                direction=self.config.direction,
                pk=pk,
                tpk=tpk,
                volume=next_size,
                min_base_amount=venue["min_base_amount"],
                min_quote_amount=venue["min_quote_amount"],
                size_decimals=venue["size_decimals"],
                price_decimals=venue["price_decimals"],
                step_n=next_n,
            )
            if not check.ok:
                return self._freeze(
                    f"ladder step{next_n} failed venue validation: "
                    f"{check.error}: {check.detail}"
                )

        # Guard: never resubmit an already-attempted ladder for this step.
        if (
            self.state.submission_phase == SUBMISSION_ATTEMPTED
            and self.state.submission_role == ROLE_LADDER
            and self.state.submission_step == next_n
        ):
            return self._freeze(
                f"ladder step{next_n} already attempted; "
                "reconcile exchange state before any resubmission"
            )

        try:
            client_id = self._next_id_for(
                role_code=V2_ROLE_LADDER_ENTRY,
                step=int(next_n),
                engine_role=ROLE_LADDER,
            )
        except ClientIdError as exc:
            return self._freeze(f"ladder client_id allocation failed: {exc}")
        order_side = self.config.direction.lower()

        # PREPARE + ATTEMPT durable record for the ladder submission.
        self.state.submission_phase = SUBMISSION_PREPARED
        self.state.submission_client_id = client_id
        self.state.submission_step = next_n
        self.state.submission_role = ROLE_LADDER
        self.state.submission_attempted_at = None
        self.state.submission_exchange_order_id = None

        # Persist the pending identity BEFORE the venue call so that a
        # crash between submit and verify still leaves the client_id
        # recoverable. This is the crash-boundary requirement: the
        # pending identity must survive a daemon restart.
        self.state.pending_order_client_id = client_id
        self.state.pending_order_role = ROLE_LADDER
        self.state.pending_requested_price = next_price
        self.state.pending_requested_size = next_size

        self.state.submission_phase = SUBMISSION_ATTEMPTED
        self.state.submission_attempted_at = time.time()

        try:
            submit = self.adapter.place_limit(
                account=self.config.account,
                instrument=self.config.instrument,
                side=order_side,
                size=next_size,
                price=next_price,
                client_order_id=client_id,
                reduce_only=False,
            )
        except Exception as exc:
            # The submit may have succeeded but the immediate verify
            # timed out (Ondo client-id index lag). The pending identity
            # is already persisted above; do NOT discard it. Freeze
            # with a recoverable reason so the daemon can later find
            # the order via the client-id lookup path.
            self.state.submission_phase = SUBMISSION_NEEDS_RECOVERY
            return self._freeze(
                f"place ladder step{next_n} attempted but result unknown: {exc}. "
                f"Reconcile exchange before any resubmission."
            )

        self.state.submission_phase = SUBMISSION_CONFIRMED
        self.state.submission_exchange_order_id = coerce_exchange_order_id(
            submit.get("exchange_order_id")
        )

        self.state.pending_order_exchange_id = coerce_exchange_order_id(
            submit.get("exchange_order_id")
        )
        self.state.pending_confirmed_price = (
            Decimal(submit["submitted_price"]) if submit.get("submitted_price") is not None else next_price
        )
        self.state.pending_confirmed_size = (
            Decimal(submit["submitted_volume"]) if submit.get("submitted_volume") is not None else next_size
        )
        actions.append(
            f"place step{next_n} LIMIT price={next_price} confirmed={self.state.pending_confirmed_price} size={next_size} oid={self.state.pending_order_exchange_id}"
        )
        return None

    # ------------------------------------------------------------------
    # Case C: missing pending order
    # ------------------------------------------------------------------
    def _handle_missing_pending(
        self, position: Dict[str, Any], live_size: Decimal, actions: List[str]
    ) -> TickResult:
        # If next_step is 0 and we have a pending entry, the Step0 just
        # got FILLED. The caller is responsible for confirming P0 via
        # get_order_state actual_fill_price.
        if self.state.pending_order_role == ROLE_ENTRY and self.state.next_step == 0:
            actions.append("entry order missing; assume Step0 filled, awaiting P0 confirm")
            return TickResult(state=self.state, actions=actions)

        # If pending is a ladder, check position delta
        if self.state.pending_order_role == ROLE_LADDER:
            expected_size = self.config.cumulative_volume(self.state.next_step)
            previous_expected = self.state.expected_cumulative_size
            delta = expected_size - previous_expected
            if live_size >= expected_size and delta > 0:
                # Position increased by expected delta — treat as filled
                return self._handle_confirmed_fill(actions)
            return self._freeze(
                f"pending ladder disappeared without expected position delta "
                f"(live={live_size} expected={expected_size})"
            )

        # Position-scoped TP exit reconciliation. When the strategy has
        # already promoted at least one step (highest_filled_step >= 0),
        # a TP was installed (current_tp_client_id / current_tp_price set),
        # and there is no pending ladder/entry order, a still-exposed
        # position means the TP has fired but the venue's position read
        # has not yet caught up. Do NOT freeze -- wait for the position
        # to read flat, then complete the cycle normally.
        if (
            self.state.highest_filled_step >= 0
            and self.state.current_tp_client_id is not None
            and self.state.current_tp_price is not None
            and self.state.current_tp_order_id is None
            and self.state.pending_order_role is None
        ):
            # Bounded reconciliation: allow a few polls for the position
            # read to go flat. If it doesn't, freeze with a descriptive
            # reason so the operator knows the exit is stuck.
            self.state.tp_exit_attempts = int(self.state.tp_exit_attempts or 0) + 1
            actions.append(
                f"position-scoped TP exit reconciliation poll "
                f"{self.state.tp_exit_attempts}/{TP_EXIT_MAX_POLLS} "
                f"(live_size={live_size})"
            )
            if self.state.tp_exit_attempts >= TP_EXIT_MAX_POLLS:
                return self._freeze(
                    f"position-scoped TP exit stuck: position still not flat after "
                    f"{TP_EXIT_MAX_POLLS} polls (size={live_size}). "
                    f"Reconcile exchange state."
                )
            return TickResult(state=self.state, actions=actions)

        return self._freeze(
            "pending order missing in unexpected state"
        )

    def _handle_confirmed_fill(self, actions: List[str]) -> TickResult:
        """Pending order confirmed FILLED. Advance to next step.

        Handles both Step0 (ROLE_ENTRY) and ladder (ROLE_LADDER) fills.
        Step0 fills are normally confirmed by the service via
        _maybe_confirm_step0, but the engine may also observe the fill
        directly during a tick (e.g., after restart reconciliation).
        """
        if self.state.pending_order_role == ROLE_ENTRY:
            # Step0 fill observed during tick. The service's
            # _maybe_confirm_step0 will promote P0 and place TP+Step1.
            # Return without freezing — the service handles the rest.
            actions.append("Step0 entry order observed FILLED; awaiting service confirm")
            return TickResult(state=self.state, actions=actions)
        if self.state.pending_order_role != ROLE_LADDER:
            return self._freeze("confirmed fill in unexpected role")
        if self.state.pending_confirmed_price is None:
            return self._freeze("missing pending_confirmed_price")

        step_n = self.state.next_step
        promoted_pk = Decimal(self.state.pending_confirmed_price)

        # Preserve the filled Step(n) LADDER order identity in step_orders
        # BEFORE clearing the generic pending fields, so the identity of the
        # order that advanced the ladder is never lost.
        self.state.step_orders[step_n] = {
            "role": ROLE_LADDER,
            "client_id": self.state.pending_order_client_id,
            "exchange_order_id": self.state.pending_order_exchange_id,
            "status": "filled",
            "price": str(promoted_pk),
            "size": str(self.config.volume(step_n)),
        }

        self.state.fill_prices[step_n] = promoted_pk
        self.state.highest_filled_step = step_n
        self.state.expected_cumulative_size = self.config.cumulative_volume(step_n)
        actions.append(f"step{step_n} FILLED P{step_n}={promoted_pk}")

        # Clear pending
        self.state.pending_order_role = None
        self.state.pending_order_client_id = None
        self.state.pending_order_exchange_id = None
        self.state.pending_requested_price = None
        self.state.pending_requested_size = None
        self.state.pending_confirmed_price = None
        self.state.pending_confirmed_size = None

        # Operator-controlled advance gate: when set, the engine still
        # rotates the TP (so the position is protected) but does NOT
        # place the next ladder order. The operator clears the gate via
        # the control plane to advance on their own schedule.
        pause_advance = bool(getattr(self.state, "pause_advance", False))

        # Rotate TP
        result = self._rotate_tp(promoted_pk)
        if result is not None:
            return result

        if pause_advance:
            actions.append(
                f"pause_advance=True; NOT placing step{step_n + 1} ladder. "
                f"Operator must clear pause_advance to continue."
            )
            return TickResult(state=self.state, actions=actions)

        # Place next ladder if not at Step20
        if step_n < MAX_STEP:
            self.state.next_step = step_n + 1
            result = self._place_next_ladder()
            if result is not None:
                return result
        else:
            actions.append("step20 reached; no Step21")

        return TickResult(state=self.state, actions=actions)

    # ------------------------------------------------------------------
    # Case B: cycle ended (TP closed position) but pending ladder remains
    # ------------------------------------------------------------------
    def _handle_orphan_pending(self, actions: List[str]) -> TickResult:
        if self.state.pending_order_exchange_id is None:
            return self._freeze("orphan pending with no order_id")
        try:
            self.adapter.cancel_order(
                account=self.config.account,
                order_index=self.state.pending_order_exchange_id,
            )
            actions.append(f"cancel orphan pending oid={self.state.pending_order_exchange_id}")
        except Exception as exc:
            return self._freeze(f"cancel orphan failed: {exc}")

        # Reset cycle state
        self.state.pending_order_role = None
        self.state.pending_order_client_id = None
        self.state.pending_order_exchange_id = None
        self.state.pending_requested_price = None
        self.state.pending_requested_size = None
        self.state.pending_confirmed_price = None
        self.state.pending_confirmed_size = None
        self.state.current_tp_client_id = None
        self.state.current_tp_order_id = None
        self.state.current_tp_role = None
        self.state.current_tp_price = None
        self.state.current_tp_size = None
        self.state.tp_exit_attempts = 0
        self.state.highest_filled_step = -1
        self.state.fill_prices = {}
        self.state.expected_cumulative_size = Decimal("0")
        self.state.next_step = 0
        self.state.cycle_id += 1
        if self._is_smooth_shutdown():
            # Orphan canceled after TP exit — cycle is done; do not re-arm Step0.
            return self._complete_smooth_shutdown(actions)
        return TickResult(state=self.state, actions=actions)

    def reconcile_needs_recovery_pending_fill(self, actions: List[str]) -> TickResult:
        """Explicit recovery path for a NEEDS_RECOVERY registration whose
        pending logical ladder order is proven FILLED on the venue via the
        fallback-aware lookup.

        Steps:
          1. Read live position via adapter.position_state.
          2. Fallback-aware read of pending order state.
          3. Confirm FILLED taxonomy + identity (client id / side / size).
          4. Call _handle_confirmed_fill (promote Step(n), preserve identity in
             step_orders, set fill_prices[n], update expected cumulative,
             cancel old TP once, place new TP at NEW logical price + full live
             position size, place Step(n+1)).
          5. Set status back to RUNNING.

        If the pending is NOT confirmed FILLED (still ACTIVE / missing /
        CANCELLED / REJECTED), the registration stays NEEDS_RECOVERY and
        returns a TickResult indicating "still needs recovery, pending not
        proven filled" (no mutations).

        Must NOT send START, must NOT create Step0, must NOT delete the
        registration.
        """
        # We allow recovery to proceed even when pending_order_exchange_id
        # is None — Ondo's orderId is alphanumeric and cannot fit the
        # legacy int slot; relying on it alone would freeze every Ondo
        # registration in needs_recovery forever. The fallback-aware
        # client-id lookup path inside _read_pending_order_state still
        # gets a chance to find the order. If it cannot, this method is
        # a no-op and the caller is expected to fall back to position-
        # based Step0 promotion (the service's _maybe_confirm_step0 owns
        # that path; Step0 ROLE_ENTRY here just returns without state
        # mutation, so we simply keep the registration parked).
        # Gate: allow recovery when EITHER the pending identity OR the
        # submission identity is present. The submission identity is the
        # fallback for registrations frozen before the pending-identity
        # persistence fix (pending_order_client_id was not yet persisted
        # at the submit boundary).
        if (
            self.state.pending_order_exchange_id is None
            and self.state.pending_order_client_id is None
            and self.state.submission_client_id is None
        ):
            return TickResult(state=self.state, actions=actions)
        try:
            position = self.adapter.position_state(
                self.config.account, self.config.instrument
            )
        except Exception as exc:
            return self._freeze(f"reconcile: position_state failed: {exc}")
        live_size = _parse_size(position.get("size"))
        if live_size <= 0 or position.get("side") not in ("long", "short"):
            return TickResult(state=self.state, actions=actions)
        pending_state = self._read_pending_order_state()
        pending_taxonomy = str(pending_state.get("taxonomy") or "")
        if pending_taxonomy == "ACTIVE":
            # The pending order is confirmed OPEN on the venue. The
            # original submit succeeded; the freeze was from the
            # immediate verification window. Clear the stale freeze
            # and return to RUNNING so the engine can wait for the
            # fill normally.
            self.state.status = STATUS_RUNNING
            self.state.freeze_reason = None
            self.state.submission_phase = SUBMISSION_CONFIRMED
            # Backfill the exchange orderId from the lookup.
            backfilled_oid = pending_state.get("exchange_order_id")
            if backfilled_oid is not None and backfilled_oid != "":
                self.state.pending_order_exchange_id = backfilled_oid
            # Backfill confirmed price/size from the lookup.
            if self.state.pending_confirmed_price is None:
                ap = pending_state.get("limit_price") or pending_state.get("price")
                if ap is not None:
                    try:
                        self.state.pending_confirmed_price = Decimal(str(ap))
                    except Exception:
                        pass
            if self.state.pending_confirmed_size is None:
                rs = pending_state.get("requested_size") or pending_state.get("size")
                if rs is not None:
                    try:
                        self.state.pending_confirmed_size = Decimal(str(rs))
                    except Exception:
                        pass
            actions.append(
                f"reconcile_needs_recovery: pending order confirmed OPEN; "
                f"cleared stale freeze, backfilled oid={backfilled_oid}"
            )
            return TickResult(state=self.state, actions=actions)
        if pending_taxonomy != "FILLED":
            return TickResult(state=self.state, actions=actions)

        # Position-compatibility gate. The pending order FILLED on the
        # venue, but that alone is not sufficient evidence to promote the
        # step and place the next ladder: the live position must also be
        # compatible. A wrong-side or insufficient-size live position
        # means the FILLED order was on the wrong account, against the
        # wrong direction, or partial — adopting it would create
        # downstream TP / Step2 against a position that does not match.
        # Fail closed: freeze with a descriptive NEEDS_RECOVERY reason
        # instead of promoting. The engine does not persist state; the
        # caller (_drive_one) will persist on this TickResult.
        expected_side = "long" if self.config.direction.upper() == "BUY" else "short"
        live_side = position.get("side")
        if live_side != expected_side:
            self.state.status = STATUS_NEEDS_RECOVERY
            self.state.freeze_reason = (
                f"ladder step order FILLED but live position side={live_side} "
                f"does not match expected {expected_side}; refusing to "
                f"promote against a mismatched position"
            )
            return self._freeze(self.state.freeze_reason)
        # For a pending ladder order at next_step=n, the live position
        # must already cover cumulative_volume(n) (Step0 + ... + Step_n
        # all filled). This is the cumulative evidence that the FILLED
        # order was actually ours.
        if self.state.pending_order_role == "ladder" and self.state.next_step > 0:
            try:
                expected_cumulative = self.config.cumulative_volume(
                    self.state.next_step
                )
            except Exception:
                expected_cumulative = None
            if expected_cumulative is not None and expected_cumulative > 0:
                if live_size + Decimal("0.0000001") < expected_cumulative:
                    self.state.status = STATUS_NEEDS_RECOVERY
                    self.state.freeze_reason = (
                        f"Step{self.state.next_step} order FILLED but live "
                        f"position size={live_size} is smaller than expected "
                        f"cumulative={expected_cumulative}; refusing to promote"
                    )
                    return self._freeze(self.state.freeze_reason)

        # Backfill pending identity fields from the submission record
        # when the pre-fix freeze left them null. The submission fields
        # were persisted at the submit boundary; the pending fields were
        # not (they were only set after a successful submit response).
        if self.state.pending_order_role is None and self.state.submission_role:
            self.state.pending_order_role = self.state.submission_role
        if self.state.pending_order_client_id is None and self.state.submission_client_id is not None:
            self.state.pending_order_client_id = self.state.submission_client_id

        # Backfill pending_order_exchange_id from the client-id lookup
        # BEFORE _handle_confirmed_fill records step_orders[n], so the
        # venue's native id (often alphanumeric on Ondo) is preserved
        # in step_orders rather than collapsing to None.
        backfilled_oid = pending_state.get("exchange_order_id")
        if backfilled_oid is not None and backfilled_oid != "":
            self.state.pending_order_exchange_id = backfilled_oid

        # Backfill pending_confirmed_price from the lookup when the
        # original submit raised before persisting it (the verify-
        # timeout path). The lookup's actual_fill_price is the
        # canonical price; falling back to pending_requested_price
        # is safe because the venue accepted the order at that price.
        if self.state.pending_confirmed_price is None:
            afp = pending_state.get("actual_fill_price")
            if afp is not None:
                try:
                    self.state.pending_confirmed_price = Decimal(str(afp))
                except Exception:
                    pass
            if self.state.pending_confirmed_price is None:
                self.state.pending_confirmed_price = self.state.pending_requested_price
        if self.state.pending_confirmed_size is None:
            self.state.pending_confirmed_size = self.state.pending_requested_size

        actions.append(
            f"reconcile_needs_recovery: pending oid={self.state.pending_order_exchange_id} "
            f"taxonomy=FILLED size={live_size}"
        )
        # Remember the pre-existing freeze markers so we can distinguish a
        # freeze freshly set by the confirmed-fill handler from the prior
        # placeholder.
        pre_status = self.state.status
        pre_freeze_reason = self.state.freeze_reason
        result = self._handle_confirmed_fill(actions)
        if result is None:
            result = TickResult(state=self.state, actions=actions)
        # Fresh freeze detection: the handler is considered to have frozen
        # only if freeze_reason changed (or was set anew) during the call.
        new_freeze = result.state.freeze_reason
        new_status = result.state.status
        handler_froze = (
            new_status == STATUS_NEEDS_RECOVERY
            and (new_freeze is not None)
            and (new_freeze != pre_freeze_reason)
        )
        if handler_froze:
            return result  # propagate fresh freeze
        # Success: clear the pre-existing freeze placeholder and mark running.
        result.state.status = STATUS_RUNNING
        result.state.freeze_reason = None
        # Pre-existing marker to record what we recovered (audit only).
        if pre_status == STATUS_NEEDS_RECOVERY and pre_freeze_reason:
            actions.append(
                "reconcile_needs_recovery: cleared prior NEEDS_RECOVERY "
                f"({pre_freeze_reason[:80]})"
            )
        actions.append("reconcile_needs_recovery: status -> RUNNING")
        return result

    def _cancel_pending_ladder_for_cycle_end(self, actions: List[str]) -> bool:
        """Cancel any remaining pending ladder order owned by the
        completed cycle. Returns True if cleanup succeeded (or no
        pending ladder exists), False if cancellation failed.

        Uses the persisted pending identity (client_id preferred,
        exchange_order_id as fallback). For Ondo, resolves the
        exchange orderId from the client-id lookup before cancelling.
        """
        cid = self.state.pending_order_client_id
        if cid is None:
            # No pending ladder to cancel.
            return True

        # Resolve the exchange orderId from the client-id lookup
        # (Ondo's alphanumeric ids are not persisted in
        # pending_order_exchange_id).
        oid = self.state.pending_order_exchange_id
        if oid is None:
            try:
                st = self.adapter.get_order_state_by_client_id(
                    self.config.account, self.config.instrument, int(cid)
                )
                oid = st.get("exchange_order_id")
            except Exception as exc:
                actions.append(
                    f"cycle-end cleanup: client-id lookup failed for cid={cid}: {exc}"
                )
                return False

        if oid is None:
            actions.append(
                f"cycle-end cleanup: no exchange orderId found for cid={cid}; "
                f"order may already be terminal"
            )
            return True

        # Cancel the exact owned order.
        try:
            canceled = self.adapter.cancel_order(
                account=self.config.account,
                order_index=oid,
            )
        except Exception as exc:
            actions.append(
                f"cycle-end cleanup: cancel_order failed for oid={oid}: {exc}"
            )
            return False

        if not canceled:
            actions.append(
                f"cycle-end cleanup: cancel_order returned False for oid={oid}"
            )
            return False

        actions.append(f"cycle-end cleanup: canceled pending ladder oid={oid} cid={cid}")
        return True

    def _handle_cycle_end(self, actions: List[str]) -> TickResult:
        # Already flat and no pending — start fresh cycle, unless Smooth Shutdown.
        if self._is_smooth_shutdown():
            return self._complete_smooth_shutdown(actions)
        if self.state.submission_phase in (
            SUBMISSION_ATTEMPTED,
            SUBMISSION_CONFIRMED,
            SUBMISSION_NEEDS_RECOVERY,
        ) and int(self.state.highest_filled_step) < 0:
            return self._freeze(
                "position reads flat after Step0 submission without confirmed fill; "
                "do not place another Step0. Reconcile exchange state."
            )
        if int(self.state.highest_filled_step) >= 0:
            # Completed cycle (TP exit). Reset submission so the next Step0
            # is a new cycle, not a repeat of the unconfirmed first Step0.
            self.state.submission_phase = SUBMISSION_NOT_SUBMITTED
            self.state.submission_client_id = None
            self.state.submission_exchange_order_id = None

        # Cycle-end cleanup: cancel any remaining pending ladder order
        # owned by the completed cycle BEFORE starting the next Step0.
        # A new GoldenFibo cycle MUST NEVER submit its new Step0 while
        # a still-live ladder order from the completed cycle remains on
        # the venue.
        #
        # The engine only tracks one pending ladder at a time
        # (pending_order_client_id). When the cycle ends, that pending
        # order may still be live on the venue. Cancel it first.
        cleanup_ok = self._cancel_pending_ladder_for_cycle_end(actions)
        if not cleanup_ok:
            return self._freeze(
                "cycle-end ladder cleanup failed; refusing to start new cycle. "
                "Reconcile exchange state."
            )

        # Operator-controlled cycle-restart gate. When True, the engine
        # completes the current cycle but does NOT start a new Step0.
        # Used for staged live validations. The registration stays in a
        # paused-completed state until the operator clears the gate.
        pause_cycle_restart = bool(getattr(self.state, "pause_cycle_restart", False))
        if pause_cycle_restart:
            # Clear the pending identity so the tick path does not see
            # the cancelled order as an unexpected terminal state.
            self.state.pending_order_role = None
            self.state.pending_order_client_id = None
            self.state.pending_order_exchange_id = None
            self.state.pending_requested_price = None
            self.state.pending_requested_size = None
            self.state.pending_confirmed_price = None
            self.state.pending_confirmed_size = None
            self.state.current_tp_client_id = None
            self.state.current_tp_order_id = None
            self.state.current_tp_role = None
            self.state.current_tp_price = None
            self.state.current_tp_size = None
            self.state.tp_exit_attempts = 0
            actions.append(
                "pause_cycle_restart=True; NOT starting new cycle. "
                "Operator must clear pause_cycle_restart to continue."
            )
            self.state.status = STATUS_RUNNING
            self.state.freeze_reason = None
            return TickResult(state=self.state, actions=actions)
        return self._start_fresh_cycle(actions)

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------
    @staticmethod
    def _decimal_or_none(value: Any) -> Optional[Decimal]:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    def _read_tp_state(self) -> Dict[str, Any]:
        """Read the shared TP order state, tolerating the single-order lookup's
        inability to see FILLED orders on some accounts.

        Tries get_order_state(order_index) first; if that returns an empty
        record (order not found in active OR the inactive search failed), falls
        back to get_order_state_by_client_id(client_id), which uses the bounded
        paging surface that DOES see filled orders. Returns {} only when both
        lookups find nothing.
        """
        oid = self.state.current_tp_order_id
        if oid is None:
            return {}
        try:
            st = self.adapter.get_order_state(self.config.account, int(oid))
        except Exception:
            st = {}
        if st:
            return st
        # Fallback: client-id lookup sees filled orders.
        cid = self.state.current_tp_client_id
        if cid is not None:
            try:
                st = self.adapter.get_order_state_by_client_id(
                    self.config.account, self.config.instrument, int(cid)
                )
            except Exception:
                st = {}
            if st:
                return st
        return {}

    def _read_pending_order_state(self) -> Dict[str, Any]:
        """Read the current pending ladder order state, tolerating the
        single-order lookup's inability to see FILLED orders on some accounts.

        Tries get_order_state(exchange_order_id) first; if that returns a
        useful record, use it. If empty/unavailable AND a persisted
        pending_order_client_id exists, fall back to
        get_order_state_by_client_id(client_id), which uses the bounded
        paging surface that DOES see filled orders.

        Critically: the client-id fallback runs even when
        ``pending_order_exchange_id`` is None (the very common Ondo case
        where the venue's alphanumeric ``orderId`` cannot fit the
        legacy numeric slot). Relying on the persisted exchange id alone
        would leave every such pending ladder unrecoverable forever.

        Identity validation on the fallback record: the returned order must
        match the persisted client id, the expected strategy side, the
        instrument, and the requested size. A mismatched record is NEVER
        adopted (treated as not-found, so the caller reconciles rather than
        promoting the wrong order).

        Returns {} only when no usable record is found.
        """
        st: Dict[str, Any] = {}
        oid = self.state.pending_order_exchange_id
        if oid is not None:
            try:
                st = self.adapter.get_order_state(self.config.account, oid)
            except Exception:
                st = {}
            if st:
                return st
        # Fallback: client-id lookup sees filled orders. Runs even when
        # ``pending_order_exchange_id`` is None — see the method docstring.
        # Use submission_client_id as fallback when pending_order_client_id
        # was not persisted (pre-fix registrations frozen at the submit
        # boundary before the pending-identity persistence patch).
        cid = self.state.pending_order_client_id or self.state.submission_client_id
        if cid is None:
            return {}
        try:
            st = self.adapter.get_order_state_by_client_id(
                self.config.account, self.config.instrument, int(cid)
            )
        except Exception:
            return {}
        if not st:
            return {}
        # Identity validation before adopting the fallback record.
        expected_side = self.config.direction.lower()
        rec_cid = st.get("client_order_index") or st.get("client_order_id")
        try:
            rec_cid_int = int(rec_cid) if rec_cid is not None else None
        except (TypeError, ValueError):
            rec_cid_int = None
        if rec_cid_int != int(cid):
            return {}
        rec_side = str(st.get("side") or "").lower()
        if rec_side and rec_side != expected_side:
            return {}
        # Requested size must match the persisted pending requested size.
        expected_size = self.state.pending_requested_size
        rec_size = self._decimal_or_none(st.get("requested_size") or st.get("size"))
        if expected_size is not None and rec_size is not None:
            if Decimal(str(expected_size)) != rec_size:
                return {}
        return st

    def _sync_tp_volume(self, live_size: Decimal, actions: List[str]) -> Optional[TickResult]:
        """Synchronize the shared TP VOLUME to the live position size at the
        SAME TP price. Used when a partial ladder fill grows the position: the
        TP must cover the full live exposure. Does NOT alter the logical step,
        fill_prices, highest_filled_step, TP price, or the pending ladder.

        Safe sequence: cancel exact old TP -> verify -> place exactly ONE new
        resting reduce-only GTC LIMIT TP at the same price for live_size ->
        verify -> persist new identity/size. On any failure -> NEEDS_RECOVERY
        (no duplicate TP, durable state preserved).
        """
        tp_price = self.state.current_tp_price
        old_oid = self.state.current_tp_order_id
        if tp_price is None or old_oid is None:
            return self._freeze("TP volume sync requested with no current TP")

        tp_side = "sell" if self.config.direction.upper() == "BUY" else "buy"

        # Notional guard (do not silently resize below venue minimum).
        vc = self._venue_constraints()
        if vc is not None:
            min_quote = vc.get("min_quote_amount") or Decimal("0")
            notional = Decimal(live_size) * Decimal(tp_price)
            if min_quote > 0 and notional < min_quote:
                return self._freeze(
                    f"TP volume sync notional {notional} below venue minimum {min_quote} "
                    f"(size={live_size} price={tp_price}); reconcile before resubmission."
                )

        # Cancel the exact old TP.
        try:
            canceled = self.adapter.cancel_order(
                account=self.config.account, order_index=old_oid
            )
        except Exception as exc:
            return self._freeze(f"TP volume sync: cancel old TP oid={old_oid} failed: {exc}")
        if not canceled:
            return self._freeze(f"TP volume sync: cancel old TP oid={old_oid} returned False")
        actions.append(f"TP volume sync: canceled old TP oid={old_oid}")

        # Place exactly ONE new TP at the same price for live_size.
        try:
            tp_step = max(0, int(self.state.highest_filled_step))
            tp_client_id = self._next_id_for(
                role_code=V2_ROLE_SHARED_TP,
                step=tp_step,
                engine_role=ROLE_TP,
            )
        except ClientIdError as exc:
            return self._freeze(f"TP volume sync client_id allocation failed: {exc}")
        self.state.submission_phase = SUBMISSION_PREPARED
        self.state.submission_client_id = tp_client_id
        self.state.submission_step = self.state.highest_filled_step
        self.state.submission_role = ROLE_TP
        self.state.submission_attempted_at = None
        self.state.submission_exchange_order_id = None
        self.state.submission_phase = SUBMISSION_ATTEMPTED
        self.state.submission_attempted_at = time.time()
        try:
            submit = self.adapter.set_shared_tp(
                account=self.config.account,
                instrument=self.config.instrument,
                price=tp_price,
                side=tp_side,
                size=live_size,
                client_order_id=tp_client_id,
            )
        except Exception as exc:
            self.state.submission_phase = SUBMISSION_NEEDS_RECOVERY
            return self._freeze(
                f"TP volume sync: new TP placement attempted but result unknown: {exc}. "
                "Reconcile exchange before any resubmission."
            )
        self.state.submission_phase = SUBMISSION_CONFIRMED
        self.state.submission_exchange_order_id = coerce_exchange_order_id(
            submit.get("exchange_order_id")
        )
        self.state.current_tp_client_id = tp_client_id
        self.state.current_tp_order_id = coerce_exchange_order_id(
            submit.get("exchange_order_id")
        )
        self.state.current_tp_size = Decimal(str(submit.get("submitted_volume") or live_size))
        # TP price unchanged.
        self.state.current_tp_role = ROLE_TP
        actions.append(
            f"TP volume sync: new TP oid={self.state.current_tp_order_id} "
            f"price={self.state.current_tp_price} size={self.state.current_tp_size}"
        )
        return None

    def _venue_constraints(self) -> Optional[Dict[str, Any]]:
        """Lazily read + cache venue constraints via the thin adapter.

        Returns None on read failure (fail-open: do not block placement on a
        metadata read error). The adapter exposes get_venue_constraints.
        """
        if self._venue_constraints_cache is not None:
            return self._venue_constraints_cache
        try:
            raw = self.adapter.get_venue_constraints(
                self.config.account, self.config.instrument
            )
        except Exception:
            return None
        if not raw:
            return None
        try:
            self._venue_constraints_cache = {
                "min_base_amount": Decimal(str(raw.get("min_base_amount") or "0")),
                "min_quote_amount": Decimal(str(raw.get("min_quote_amount") or "0")),
                "size_decimals": int(raw.get("size_decimals") or 0),
                "price_decimals": int(raw.get("price_decimals") or 0),
            }
        except Exception:
            return None
        return self._venue_constraints_cache

    def _freeze(self, reason: str) -> TickResult:
        self.state.status = STATUS_NEEDS_RECOVERY
        self.state.freeze_reason = reason
        return TickResult(state=self.state, actions=[f"NEEDS_RECOVERY: {reason}"])


def _parse_size(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal("0")
