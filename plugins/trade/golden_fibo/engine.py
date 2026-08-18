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

        # Validate accumulated size
        if self.state.highest_filled_step >= 0:
            expected = self.config.cumulative_volume(self.state.highest_filled_step)
            if has_position and live_size < expected:
                return self._freeze(
                    f"position size shrunk unexpectedly: live={live_size} expected>={expected}"
                )

        # TP liveness monitoring: while a position is open and a shared TP is
        # expected (current_tp_order_id set), the TP order must remain ACTIVE.
        # If it unexpectedly disappears/cancels WITHOUT a corresponding
        # position close, the position is unprotected -> NEEDS_RECOVERY.
        # We do NOT silently continue and we do NOT auto re-arm a new TP.
        if has_position and self.state.current_tp_order_id is not None:
            try:
                tp_state = self.adapter.get_order_state(
                    self.config.account,
                    int(self.state.current_tp_order_id),
                )
            except Exception as exc:
                return self._freeze(f"get_order_state(TP) failed: {exc}")
            tp_taxonomy = str(tp_state.get("taxonomy") or "")
            actions.append(
                f"tp oid={self.state.current_tp_order_id} taxonomy={tp_taxonomy}"
            )
            if tp_taxonomy == "ACTIVE":
                pass  # TP healthy
            elif tp_taxonomy == "FILLED":
                # TP filled but position still open -> partial TP fill or a
                # residual; treat as unexpected and reconcile.
                return self._freeze(
                    f"shared TP oid={self.state.current_tp_order_id} FILLED but "
                    f"position still open (size={live_size}); reconcile exchange state"
                )
            else:
                # CANCELED / REJECTED / EXPIRED / missing -> unprotected position.
                return self._freeze(
                    f"shared TP oid={self.state.current_tp_order_id} unexpectedly "
                    f"{tp_taxonomy or 'missing'} while position open (size={live_size}); "
                    "position is unprotected. Reconcile exchange state before any resubmission."
                )

        # Check pending order state
        pending_alive = False
        pending_state: Dict[str, Any] = {}
        if self.state.pending_order_exchange_id is not None:
            try:
                pending_state = self.adapter.get_order_state(
                    self.config.account,
                    int(self.state.pending_order_exchange_id),
                )
            except Exception as exc:
                return self._freeze(f"get_order_state failed: {exc}")
            pending_status = str(pending_state.get("status") or "")
            pending_taxonomy = str(pending_state.get("taxonomy") or "")
            actions.append(
                f"pending oid={self.state.pending_order_exchange_id} status={pending_status} taxonomy={pending_taxonomy}"
            )
            if pending_taxonomy == "ACTIVE":
                pending_alive = True
            elif pending_taxonomy == "FILLED":
                # Confirmed fill via get_order_state
                return self._handle_confirmed_fill(actions)
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
        self.state.cycle_id += 1
        self.state.next_step = 0
        self.state.highest_filled_step = -1
        self.state.fill_prices = {}
        self.state.expected_cumulative_size = Decimal("0")
        self.state.current_tp_price = None
        self.state.current_tp_order_id = None
        self.state.current_tp_client_id = None
        self.state.current_tp_role = None
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
        self.state.current_tp_client_id = tp_client_id
        self.state.current_tp_order_id = (
            int(submit["exchange_order_id"]) if submit.get("exchange_order_id") is not None else None
        )
        self.state.current_tp_role = ROLE_TP
        actions.append(
            f"shared TP set price={self.state.current_tp_price} size={submit.get('submitted_volume') or tp_size} oid={self.state.current_tp_order_id}"
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
        self.state.highest_filled_step = -1
        self.state.fill_prices = {}
        self.state.expected_cumulative_size = Decimal("0")
        self.state.next_step = 0
        self.state.cycle_id += 1
        return TickResult(state=self.state, actions=actions)

    def _handle_cycle_end(self, actions: List[str]) -> TickResult:
        # Already flat and no pending — start fresh cycle
        return self._start_fresh_cycle(actions)

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------
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
