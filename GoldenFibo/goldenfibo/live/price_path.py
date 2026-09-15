"""Ordered live prices → MarketEvents using engine-exposed levels only.

Does not recompute ladder geometry. Reads next_p / shared_tp from EngineState
and emits PROGRESSION_TOUCH / TP_TOUCH for the canonical GoldenFiboEngine.

Gap policy (single trade price):
  - Apply contiguous progression fills while the trade price still touches next_p
    (BUY: price <= next_p; SELL: price >= next_p), one engine step at a time.
  - Then, if the same price touches shared_tp, emit TP_TOUCH.
  - A single price cannot be both adverse progression and favorable TP for the
    standard ladder geometry (progressions move away from TP), so dual-touch
    on one tick does not occur; multi-step gaps only chain progressions.
"""

from __future__ import annotations

from decimal import Decimal
from typing import List, Optional

from ..engine.config import Side
from ..engine.engine import GoldenFiboEngine
from ..engine.events import MarketEvent, MarketEventKind
from ..engine.state import EngineState


def _touched_progression(side: Side, price: Decimal, next_p: Decimal) -> bool:
    if side is Side.BUY:
        return price <= next_p
    return price >= next_p


def _touched_tp(side: Side, price: Decimal, shared_tp: Decimal) -> bool:
    if side is Side.BUY:
        return price >= shared_tp
    return price <= shared_tp


def market_events_for_price(
    state: EngineState,
    *,
    price: Decimal,
    ts_ms: int,
    max_steps_per_tick: int = 20,
) -> List[MarketEvent]:
    """Translate one ordered trade/mark price into zero or more MarketEvents.

    Uses a scratch walk over level checks without mutating ``state``. Caller
    applies events to the real engine in order.
    """
    if not state.active or state.p0 is None or state.highest_filled < 0:
        return []

    events: List[MarketEvent] = []
    # Local mirrors for multi-step gap without mutating real state.
    hf = state.highest_filled
    side = state.side
    p0 = state.p0
    percentage = state.percentage
    phi = state.phi
    max_step = state.max_step

    from ..engine.levels import ladder_step

    steps = 0
    while hf < max_step and steps < max_steps_per_tick:
        next_p, _ = ladder_step(side, p0, hf + 1, phi=phi, percentage=percentage)
        if not _touched_progression(side, price, next_p):
            break
        events.append(
            MarketEvent(
                kind=MarketEventKind.PROGRESSION_TOUCH,
                ts_ms=ts_ms,
                price=next_p,
                step=hf + 1,
            )
        )
        hf += 1
        steps += 1

    # shared TP after the would-be fills
    _, shared_tp = ladder_step(side, p0, hf, phi=phi, percentage=percentage)
    if _touched_tp(side, price, shared_tp):
        events.append(MarketEvent(kind=MarketEventKind.TP_TOUCH, ts_ms=ts_ms, price=shared_tp))

    return events


def apply_price_to_engine(engine: GoldenFiboEngine, price: Decimal, ts_ms: int) -> List:
    """Helper: translate + apply; returns domain events from all applications."""
    from ..engine.events import DomainEvent

    out: List[DomainEvent] = []
    # Re-read state after each event so multi-step gaps stay correct.
    guard = 0
    while guard < 64:
        guard += 1
        batch = market_events_for_price(engine.state, price=price, ts_ms=ts_ms, max_steps_per_tick=1)
        if not batch:
            break
        # Only apply the first event, then re-evaluate (handles TP after progression).
        result = engine.on_event(batch[0])
        out.extend(result.events)
        if batch[0].kind is MarketEventKind.TP_TOUCH:
            break
    return out
