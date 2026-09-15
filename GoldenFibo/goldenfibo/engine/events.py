"""Market input events and engine domain output events."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional


class MarketEventKind(str, Enum):
    """Ordered inputs the engine understands.

    Feeders translate historical bars, replay clocks, or live ticks into these.
    The engine never inspects OHLC candles directly.
    """

    SEED_P0 = "seed_p0"
    """Open (or force-open) a cycle with entry at ``price`` (step 0 fill)."""

    PROGRESSION_TOUCH = "progression_touch"
    """Price path touched the next progression level — fill step n+1 at theoretical P."""

    TP_TOUCH = "tp_touch"
    """Price path touched the shared TP — close cycle and chain new P0 = TP."""

    AMBIGUOUS_BAR = "ambiguous_bar"
    """Strict mode: bar touches both TP and next progression; no path mutation."""


class DomainEventKind(str, Enum):
    CYCLE_STARTED = "cycle_started"
    STEP_FILLED = "step_filled"
    PROGRESSION = "progression"
    TP_HIT = "tp_hit"
    CYCLE_CLOSED = "cycle_closed"
    P0_RESET = "p0_reset"
    AMBIGUOUS_BAR = "ambiguous_bar"


@dataclass(frozen=True)
class MarketEvent:
    """One ordered market fact for the engine."""

    kind: MarketEventKind
    ts_ms: int
    price: Optional[Decimal] = None
    """SEED_P0: P0; optional annotation price for touches; unused for pure touches."""

    step: Optional[int] = None
    """Optional explicit step for PROGRESSION_TOUCH (defaults to highest_filled+1)."""

    meta: Dict[str, Any] = field(default_factory=dict)
    """Feeder annotations (OHLC, levels, reason). Not used for geometry."""


@dataclass(frozen=True)
class DomainEvent:
    kind: DomainEventKind
    ts_ms: int
    payload: Dict[str, Any] = field(default_factory=dict)
