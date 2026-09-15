"""Intrabar dual-touch helpers (feeder-side; engine stays event-ordered)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from .config import Side
from .levels import ladder_step


def bar_touches_level(side: Side, *, high: Decimal, low: Decimal, level: Decimal, is_progression: bool) -> bool:
    """Whether OHLC range touches a ladder level for the given side semantics."""
    if is_progression:
        # Progression is adverse: BUY fills lower, SELL fills higher.
        if side is Side.BUY:
            return low <= level
        return high >= level
    # TP is favorable: BUY TP above, SELL TP below.
    if side is Side.BUY:
        return high >= level
    return low <= level


def dual_touch(
    side: Side,
    *,
    high: Decimal,
    low: Decimal,
    shared_tp: Decimal,
    next_p: Decimal,
) -> bool:
    """True if one bar's range touches both shared TP and next progression."""
    tp_hit = bar_touches_level(side, high=high, low=low, level=shared_tp, is_progression=False)
    prog_hit = bar_touches_level(side, high=high, low=low, level=next_p, is_progression=True)
    return tp_hit and prog_hit


def ambiguity_payload(
    *,
    ts_ms: int,
    o: Decimal,
    h: Decimal,
    l: Decimal,
    c: Decimal,
    shared_tp: Decimal,
    next_p: Decimal,
    cycle_id: int,
    highest_filled: int,
    p0: Decimal,
    side: Side,
) -> Dict[str, Any]:
    return {
        "ts_ms": ts_ms,
        "ohlc": {"o": str(o), "h": str(h), "l": str(l), "c": str(c)},
        "shared_tp": str(shared_tp),
        "next_progression": str(next_p),
        "cycle_id": cycle_id,
        "highest_filled": highest_filled,
        "p0": str(p0),
        "side": side.value,
        "reason": "bar_touches_both_tp_and_next_progression",
    }


def levels_for_state(
    side: Side,
    p0: Decimal,
    highest_filled: int,
    *,
    percentage: Decimal,
    phi: Decimal,
    max_step: int,
) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    """Return (shared_tp, next_p) for current hf, or (None, None) if inactive."""
    if highest_filled < 0:
        return None, None
    _, shared_tp = ladder_step(side, p0, highest_filled, phi=phi, percentage=percentage)
    if highest_filled >= max_step:
        return shared_tp, None
    next_p, _ = ladder_step(side, p0, highest_filled + 1, phi=phi, percentage=percentage)
    return shared_tp, next_p
