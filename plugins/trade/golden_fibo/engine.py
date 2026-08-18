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
from plugins.trade.golden_fibo.state import (
    ROLE_ENTRY,
    ROLE_LADDER,
    ROLE_TP,
    STATUS_NEEDS_RECOVERY,
    STATUS_RUNNING,
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
        client_order_id_factory: Callable[[], int],
    ) -> None:
        self.config = config
        self.state = state
        self.adapter = adapter
        self._next_client_id = client_order_id_factory
        self._venue_constraints_cache: Optional[Dict[str, Any]] = None

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
        if self.state.pending_order_exchange_id is not None:
            pending_state = self._read_pending_order_state()
            pending_status = str(pending_state.get("status") or "")
            pending_taxonomy = str(pending_state.get("taxonomy") or "")
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
        if self.state.pending_order_exchange_id is not None:
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
        if self.state.pending_order_exchange_id is None:
            # Case C: position exists but expected pending ladder is absent
            return self._handle_missing_pending(position, live_size, actions)

        if not pending_alive:
            # Pending not in active surface and not from get_order_state
            # Either filled or otherwise gone. Use position delta.
            return self._handle_missing_pending(position, live_size, actions)

        # Case D: healthy waiting state
        actions.append("healthy waiting")
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
        """
        # Guard: never resubmit an already-attempted Step0.
        if self.state.submission_phase == SUBMISSION_ATTEMPTED:
            return self._freeze(
                "Step0 already attempted (submission_attempted persisted); "
                "reconcile exchange state before any resubmission"
            )

        size = self.config.step0_volume
        order_side = self.config.direction.lower()  # "buy" or "sell"

        # Deterministic client identity per (registration, cycle, step, role).
        # Computed ONCE per logical Step0 and persisted BEFORE the venue call.
        # Never regenerated on retry/recovery for the same logical order.
        client_id = self._next_client_id()

        # PREPARE: persist everything BEFORE the venue call.
        # Cycle-boundary reset: every cycle-scoped field must reflect ONLY the
        # current cycle. The previous cycle's step_orders map must NOT survive
        # into the new cycle (historical evidence belongs in forensic logs,
        # not the active state machine).
        self.state.cycle_id += 1
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
        if exchange_oid is not None:
            self.state.pending_order_exchange_id = int(exchange_oid)
            self.state.submission_exchange_order_id = int(exchange_oid)
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

        tp_client_id = self._next_client_id()

        # Cancel the previous shared TP (if any) BEFORE placing the new one.
        # The shared TP is a single resting reduce-only LIMIT; replacing it
        # means canceling the old resting order first. This uses the fixed
        # exact-order cancel path.
        old_tp_oid = self.state.current_tp_order_id
        if old_tp_oid is not None:
            try:
                canceled = self.adapter.cancel_order(
                    account=self.config.account,
                    order_index=int(old_tp_oid),
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
        self.state.submission_exchange_order_id = (
            int(submit["exchange_order_id"]) if submit.get("exchange_order_id") is not None else None
        )

        self.state.current_tp_price = Decimal(str(submit.get("submitted_price") or tp_price))
        self.state.current_tp_size = Decimal(str(submit.get("submitted_volume") or tp_size))
        self.state.current_tp_client_id = tp_client_id
        self.state.current_tp_order_id = (
            int(submit["exchange_order_id"]) if submit.get("exchange_order_id") is not None else None
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

        client_id = self._next_client_id()
        order_side = self.config.direction.lower()

        # PREPARE + ATTEMPT durable record for the ladder submission.
        self.state.submission_phase = SUBMISSION_PREPARED
        self.state.submission_client_id = client_id
        self.state.submission_step = next_n
        self.state.submission_role = ROLE_LADDER
        self.state.submission_attempted_at = None
        self.state.submission_exchange_order_id = None
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
            self.state.submission_phase = SUBMISSION_NEEDS_RECOVERY
            return self._freeze(
                f"place ladder step{next_n} attempted but result unknown: {exc}. "
                f"Reconcile exchange before any resubmission."
            )

        self.state.submission_phase = SUBMISSION_CONFIRMED
        self.state.submission_exchange_order_id = (
            int(submit["exchange_order_id"]) if submit.get("exchange_order_id") is not None else None
        )

        self.state.pending_order_client_id = client_id
        self.state.pending_order_exchange_id = (
            int(submit["exchange_order_id"]) if submit.get("exchange_order_id") is not None else None
        )
        self.state.pending_requested_price = next_price
        self.state.pending_requested_size = next_size
        self.state.pending_confirmed_price = (
            Decimal(submit["submitted_price"]) if submit.get("submitted_price") is not None else next_price
        )
        self.state.pending_confirmed_size = (
            Decimal(submit["submitted_volume"]) if submit.get("submitted_volume") is not None else next_size
        )
        self.state.pending_order_role = ROLE_LADDER
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

        # Rotate TP
        result = self._rotate_tp(promoted_pk)
        if result is not None:
            return result

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
                order_index=int(self.state.pending_order_exchange_id),
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
        if self.state.pending_order_exchange_id is None:
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
        if pending_taxonomy != "FILLED":
            return TickResult(state=self.state, actions=actions)
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

    def _handle_cycle_end(self, actions: List[str]) -> TickResult:
        # Already flat and no pending — start fresh cycle
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

        Tries get_order_state(exchange_order_id) first. If that returns a
        useful record, use it. If empty/unavailable and a persisted
        pending_order_client_id exists, fall back to
        get_order_state_by_client_id(client_id), which uses the bounded paging
        surface that DOES see filled orders.

        Identity validation on the fallback record: the returned order must
        match the persisted client id, the expected strategy side, the
        instrument, and the requested size. A mismatched record is NEVER
        adopted (treated as not-found, so the caller reconciles rather than
        promoting the wrong order).

        Returns {} only when no usable record is found.
        """
        oid = self.state.pending_order_exchange_id
        if oid is None:
            return {}
        try:
            st = self.adapter.get_order_state(self.config.account, int(oid))
        except Exception:
            st = {}
        if st:
            return st
        # Fallback: client-id lookup sees filled orders.
        cid = self.state.pending_order_client_id
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
                account=self.config.account, order_index=int(old_oid)
            )
        except Exception as exc:
            return self._freeze(f"TP volume sync: cancel old TP oid={old_oid} failed: {exc}")
        if not canceled:
            return self._freeze(f"TP volume sync: cancel old TP oid={old_oid} returned False")
        actions.append(f"TP volume sync: canceled old TP oid={old_oid}")

        # Place exactly ONE new TP at the same price for live_size.
        tp_client_id = self._next_client_id()
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
        self.state.submission_exchange_order_id = (
            int(submit["exchange_order_id"]) if submit.get("exchange_order_id") is not None else None
        )
        self.state.current_tp_client_id = tp_client_id
        self.state.current_tp_order_id = (
            int(submit["exchange_order_id"]) if submit.get("exchange_order_id") is not None else None
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
