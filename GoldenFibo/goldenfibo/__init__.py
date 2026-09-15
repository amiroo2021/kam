"""GoldenFibo — unified deterministic ladder engine."""

from .engine.config import EngineConfig, OhlcResolveMode, Side, SizingPolicyName
from .engine.engine import EngineResult, GoldenFiboEngine
from .engine.events import DomainEvent, DomainEventKind, MarketEvent, MarketEventKind
from .engine.levels import PHI, DEFAULT_PERCENTAGE, MAX_STEP, ladder_step, levels_through
from .engine.state import EngineState, FilledLeg, StateSnapshot

__version__ = "0.1.0"

__all__ = [
    "PHI",
    "DEFAULT_PERCENTAGE",
    "MAX_STEP",
    "Side",
    "SizingPolicyName",
    "OhlcResolveMode",
    "EngineConfig",
    "MarketEvent",
    "MarketEventKind",
    "DomainEvent",
    "DomainEventKind",
    "EngineState",
    "FilledLeg",
    "StateSnapshot",
    "GoldenFiboEngine",
    "EngineResult",
    "ladder_step",
    "levels_through",
    "__version__",
]
