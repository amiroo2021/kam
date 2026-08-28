"""Phase 2.13.18 — Cycle-aware Fibo decision logic.

Pure decision function: given the current eligibility result,
the current MT4 snapshot, the persisted cycle-state for the
registration, and the current exchange `before` position,
decide what action (if any) should be taken.

This function does NOT issue any exchange operations. It only
returns a decision object the caller is expected to act on.
The caller (``live_converge``) is responsible for executing
the action safely with retries and state updates.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from plugins.trade.fibo.executor import (
    ExchangePosition,
    Mt4Target,
    _Delta,
)
from plugins.trade.fibo.snapshot import Mt4Snapshot


@dataclass(frozen=True)
class CycleDecision:
    """Result of cycle-aware decision.

    action: "NOOP" | "CLOSE_REQUIRED" | "OPEN_REQUIRED" | "FAIL_CLOSED"
    """

    action: str
    reason: str
    block: bool
    block_code: Optional[str] = None
    delta: Optional[_Delta] = None
    close_size: Optional[Decimal] = None
    new_cycle_id: Optional[int] = None


def decide_cycle_action(
    *,
    registration_key: str,
    source_symbol: str,
    variant: str,
    side: str,
    target: Mt4Target,
    before: ExchangePosition,
    snap: Mt4Snapshot,
    synchronized_cycle_id: Optional[int],
    transition: Optional[str] = None,
) -> CycleDecision:
    """Decide what to do given the persisted cycle state and
    the current exchange position.

    Policy (Phase 2.13.18 user-defined rule):

      * If the current MT4 side is INACTIVE (cycle_id == 0 AND
        weight == 0):
        - exchange FLAT: NOOP.
        - exchange non-flat AND no synchronized cycle: FAIL_CLOSED.
        - exchange non-flat AND we own the cycle: CLOSE_REQUIRED.

      * If the current MT4 cycle_id == synchronized_cycle_id:
        same-cycle semantics:
        - actual == target: NOOP.
        - actual < target, same correct side: OPEN_REQUIRED (delta).
        - actual > target, same correct side: FAIL_CLOSED.
        - opposite-side actual: FAIL_CLOSED.

      * If the current MT4 cycle_id != synchronized_cycle_id:
        cycle-change semantics:
        - exchange FLAT: OPEN_REQUIRED.
        - exchange non-flat: CLOSE_REQUIRED.
        - exchange OPPOSITE-side: FAIL_CLOSED.
    """

    fibo = snap.find_fibo(source_symbol, variant)
    side_is_active = False
    cycle_id = 0
    weight = Decimal("0")
    if fibo is not None:
        cycle_id = fibo.side_cycle_id(side)
        weight = fibo.side_cumulative_weight(side)
        side_is_active = fibo.is_side_active(side)

    venue_side_for_compare = "sell" if side == "SELL" else "buy"

    # CASE 0: OPEN_SENT + actual=target → safely finalize STEADY.
    # We previously sent an open for the new cycle and now
    # the exchange matches target on the correct side. No
    # duplicate order; transition to STEADY.
    if (
        transition == "OPEN_SENT"
        and cycle_id > 0
        and synchronized_cycle_id is not None
        and synchronized_cycle_id != cycle_id
        and not before.is_flat
        and before.side
        and before.side.lower() == venue_side_for_compare
        and before.size == target.size
    ):
        return CycleDecision(
            action="NOOP",
            reason=(
                f"OPEN_SENT + actual==target for new cycle={cycle_id}; "
                "finalize STEADY without duplicate order"
            ),
            block=False,
        )

    # CASE A: current MT4 side is INACTIVE (target == 0).
    if not side_is_active or target.size <= Decimal("0"):
        if before.is_flat:
            return CycleDecision(
                action="NOOP",
                reason="target zero, actual flat — nothing to do",
                block=False,
            )
        if synchronized_cycle_id is None or synchronized_cycle_id == 0:
            return CycleDecision(
                action="FAIL_CLOSED",
                reason=(
                    "BLOCKED_CYCLE_OWNERSHIP_UNKNOWN: target zero but "
                    "exchange non-flat and no synchronized_cycle_id; "
                    "refusing to auto-flatten unowned exposure"
                ),
                block_code="BLOCKED_CYCLE_OWNERSHIP_UNKNOWN",
                block=True,
            )
        return CycleDecision(
            action="CLOSE_REQUIRED",
            reason=(
                f"target zero (MT4 cycle inactive) but synchronized "
                f"cycle {synchronized_cycle_id} owns exposure of "
                f"{_fmt(before.size)} {venue_side_for_compare.upper()}"
                " — closing old-cycle position"
            ),
            block=False,
            close_size=before.size,
            new_cycle_id=0,
        )

    # CASE B: opposite-side exposure on this instrument.
    # Per spec: opposite-side same-cycle exposure is FAIL_CLOSED.
    # We do not auto-flip; an explicit operator decision is required.
    if (
        not before.is_flat
        and before.side
        and before.side.lower() != venue_side_for_compare
    ):
        if synchronized_cycle_id == cycle_id:
            return CycleDecision(
                action="FAIL_CLOSED",
                reason=(
                    f"BLOCKED_OPPOSITE_POSITION: actual side "
                    f"{before.side.upper()} does not match target "
                    f"side {venue_side_for_compare.upper()} in same "
                    f"cycle={cycle_id}; refusing to auto-flip"
                ),
                block_code="BLOCKED_OPPOSITE_POSITION",
                block=True,
            )
        if synchronized_cycle_id is None or synchronized_cycle_id == 0:
            return CycleDecision(
                action="FAIL_CLOSED",
                reason=(
                    "BLOCKED_CYCLE_OWNERSHIP_UNKNOWN: opposite-side "
                    "exposure and no synchronized_cycle_id; "
                    "refusing to auto-reverse unowned exposure"
                ),
                block_code="BLOCKED_CYCLE_OWNERSHIP_UNKNOWN",
                block=True,
            )
        return CycleDecision(
            action="CLOSE_REQUIRED",
            reason=(
                f"opposite-side exposure detected "
                f"({_fmt(before.size)} {before.side.upper()}); "
                f"closing synchronized_cycle={synchronized_cycle_id} "
                "before opening current new-cycle target"
            ),
            block=False,
            close_size=before.size,
            new_cycle_id=cycle_id,
        )

    # CASE C: same-cycle semantics.
    if synchronized_cycle_id == cycle_id:
        if before.is_flat:
            return CycleDecision(
                action="OPEN_REQUIRED",
                reason=(
                    f"same cycle={cycle_id}, actual flat, "
                    f"target={_fmt(target.size)} — opening fresh"
                ),
                block=False,
                delta=_Delta(action="OPEN", side=side, size=target.size),
                new_cycle_id=cycle_id,
            )
        if before.size > target.size:
            return CycleDecision(
                action="FAIL_CLOSED",
                reason=(
                    f"BLOCKED_ACTUAL_EXCEEDS_TARGET: actual="
                    f"{_fmt(before.size)} > target={_fmt(target.size)} "
                    f"in same cycle={cycle_id}; refusing to silently "
                    "reduce exposure"
                ),
                block_code="BLOCKED_ACTUAL_EXCEEDS_TARGET",
                block=True,
            )
        if before.size < target.size:
            delta_size = target.size - before.size
            return CycleDecision(
                action="OPEN_REQUIRED",
                reason=(
                    f"same cycle={cycle_id}, target increased; "
                    f"opening delta={_fmt(delta_size)}"
                ),
                block=False,
                delta=_Delta(action="OPEN", side=side, size=delta_size),
                new_cycle_id=cycle_id,
            )
        return CycleDecision(
            action="NOOP",
            reason=(
                f"same cycle={cycle_id}, actual==target; nothing to do"
            ),
            block=False,
        )

    # CASE D: cycle-change semantics.
    if synchronized_cycle_id is None or synchronized_cycle_id == 0:
        # No prior ownership. The user-defined rule says
        # "exchange exposure != 0 AND no synchronized_cycle_id
        # -> FAIL_CLOSED with BLOCKED_CYCLE_OWNERSHIP_UNKNOWN".
        # We strictly follow this: no auto-bootstrap when
        # exchange is non-flat, even if actual matches the
        # current target exactly. The operator must explicitly
        # adopt the position via a separate migration procedure.
        if not before.is_flat:
            return CycleDecision(
                action="FAIL_CLOSED",
                reason=(
                    "BLOCKED_CYCLE_OWNERSHIP_UNKNOWN: cycle changed "
                    f"from unknown to {cycle_id} but exchange already "
                    f"has {_fmt(before.size)} {before.side.upper()}; "
                    "refusing to auto-close unowned exposure"
                ),
                block_code="BLOCKED_CYCLE_OWNERSHIP_UNKNOWN",
                block=True,
            )
        return CycleDecision(
            action="OPEN_REQUIRED",
            reason=(
                f"bootstrap: no synchronized_cycle_id, exchange flat, "
                f"current cycle={cycle_id}, target={_fmt(target.size)} "
                "— opening fresh and adopting cycle"
            ),
            block=False,
            delta=_Delta(action="OPEN", side=side, size=target.size),
            new_cycle_id=cycle_id,
        )

    # We have an old synchronized cycle AND exchange is non-flat.
    if not before.is_flat:
        return CycleDecision(
            action="CLOSE_REQUIRED",
            reason=(
                f"cycle changed from {synchronized_cycle_id} to "
                f"{cycle_id}; closing old-cycle exposure "
                f"{_fmt(before.size)} {before.side.upper()} before "
                "opening new-cycle target"
            ),
            block=False,
            close_size=before.size,
            new_cycle_id=cycle_id,
        )

    return CycleDecision(
        action="OPEN_REQUIRED",
        reason=(
            f"cycle changed from {synchronized_cycle_id} to {cycle_id}; "
            "exchange already flat — opening new-cycle target"
        ),
        block=False,
        delta=_Delta(action="OPEN", side=side, size=target.size),
        new_cycle_id=cycle_id,
    )


def _fmt(d: Decimal) -> str:
    s = format(d, "f")
    if "." not in s:
        return s
    return s.rstrip("0").rstrip(".")
