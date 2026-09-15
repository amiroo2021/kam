"""Pure GoldenFibo calculation engine (mode-agnostic)."""

from .config import EngineConfig, OhlcResolveMode, Side, SizingPolicyName
from .engine import EngineResult, GoldenFiboEngine
from .events import DomainEvent, DomainEventKind, MarketEvent, MarketEventKind
from .levels import PHI, DEFAULT_PERCENTAGE, MAX_STEP, ladder_step
from .state import EngineState, StateSnapshot

__all__ = [
    "EngineConfig",
    "OhlcResolveMode",
    "Side",
    "SizingPolicyName",
    "GoldenFiboEngine",
    "EngineResult",
    "DomainEvent",
    "DomainEventKind",
    "MarketEvent",
    "MarketEventKind",
    "PHI",
    "DEFAULT_PERCENTAGE",
    "MAX_STEP",
    "ladder_step",
    "EngineState",
    "StateSnapshot",
]
