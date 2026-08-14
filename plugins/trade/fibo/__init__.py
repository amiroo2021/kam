"""Fibo v1 — exchange-neutral Fibonacci counter-strategy core.

This package is exchange-agnostic. It contains:

- ``engine``  : CounterType, FiboConfig, FiboInstance, FiboEngine, FiboManager,
                CascadeState, Fibonacci percent-math helpers. One Fibo
                registration = exchange + account + instrument + counterType.
                counterBUY runs the virtual BUY cascade and sends REAL BUY
                counters. counterSELL runs the virtual SELL cascade and sends
                REAL SELL counters. Both virtual cascades never run inside one
                instance.

- ``quote``   : ``Quote`` dataclass + ``QuoteSource`` Protocol. The Protocol
                boundary lets polling be replaced with WebSocket later
                without touching ``engine.py``.

The strategy core has NO Telegram coupling, NO exchange-specific code, and NO
networking. Real-order execution is delegated to an ``ExchangeAdapter``
(Protocol, declared in ``engine``) that any per-exchange implementation can
satisfy. Telegram wizards and live exchange adapters live elsewhere.
"""

from __future__ import annotations

from .engine import (
    FIB_TABLE,
    FIB_START_VALUE,
    KILL_CYCLE_STEP,
    CascadeState,
    CounterType,
    DEFAULT_COUNTER_1,
    DEFAULT_COUNTER_2,
    DEFAULT_COUNTER_3,
    DEFAULT_COUNTER_4,
    DEFAULT_DIVIDE_PERCENT,
    ExchangeAdapter,
    FiboConfig,
    FiboEngine,
    FiboInstance,
    FiboManager,
    ProtectionState,
    RealOrderSide,
    fib_distance,
    step_price,
    step_tp,
    step0_tp,
)
from .quote import Quote, QuoteSource
from .adapters import OndoPerpsFiboAdapter
from .quote_ondoperps import OndoPerpsQuoteSource
from .runner import FiboLiveRunner, JsonlLogSink, PreflightSnapshot, RegistrationSpec

__all__ = [
    # engine
    "FIB_TABLE",
    "FIB_START_VALUE",
    "KILL_CYCLE_STEP",
    "DEFAULT_DIVIDE_PERCENT",
    "DEFAULT_COUNTER_1",
    "DEFAULT_COUNTER_2",
    "DEFAULT_COUNTER_3",
    "DEFAULT_COUNTER_4",
    "CounterType",
    "RealOrderSide",
    "CascadeState",
    "ProtectionState",
    "FiboConfig",
    "FiboInstance",
    "FiboEngine",
    "FiboManager",
    "ExchangeAdapter",
    "fib_distance",
    "step_price",
    "step_tp",
    "step0_tp",
    # quote
    "Quote",
    "QuoteSource",
    # Phase 2: OndoPerps adapter + quote source
    "OndoPerpsFiboAdapter",
    "OndoPerpsQuoteSource",
    # live runner
    "RegistrationSpec",
    "PreflightSnapshot",
    "JsonlLogSink",
    "FiboLiveRunner",
]
