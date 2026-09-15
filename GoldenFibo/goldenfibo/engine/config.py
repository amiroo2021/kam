"""Engine configuration."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class SizingPolicyName(str, Enum):
    LINEAR_RESEARCH = "linear_research"
    EXPONENTIAL_LIVE = "exponential_live"


class OhlcResolveMode(str, Enum):
    """How the historical OHLC feeder resolves path within a bar.

    Not an engine mode — feeders only. The engine always sees ordered MarketEvents.
    """

    LEGACY = "legacy"
    """Adverse progression touches first, then TP (recovered historical_replay)."""

    STRICT = "strict"
    """If a bar touches both TP and next progression, emit AMBIGUOUS_BAR only."""


@dataclass(frozen=True)
class EngineConfig:
    side: Side
    percentage: Decimal = Decimal("0.001")
    phi: Decimal = Decimal("1.618")
    max_step: int = 20
    sizing: SizingPolicyName = SizingPolicyName.LINEAR_RESEARCH
    # linear_research defaults (legacy paper)
    base_size: Decimal = Decimal("0.001")
    size_step: Decimal = Decimal("0.0001")
    # exponential_live step0 volume
    step0_volume: Decimal = Decimal("0.001")
    symbol: str = ""

    def __post_init__(self) -> None:
        if self.percentage <= 0:
            raise ValueError("percentage must be positive")
        if self.phi <= 0:
            raise ValueError("phi must be positive")
        if self.max_step < 0:
            raise ValueError("max_step must be >= 0")
        if self.side not in (Side.BUY, Side.SELL):
            raise ValueError(f"invalid side {self.side!r}")
