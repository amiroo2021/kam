"""MT5-Python Socket Bridge.

A socket-based bridge for trading on MetaTrader 5 from Python.
"""

from .trading import MT5Bridge, MT5BridgeError, NotConnectedError, TradeError
from .protocol import (
    OrderType,
    Timeframe,
    OrderResult,
    TickData,
    AccountInfo,
    Position,
    CloseResult,
)
from .timing import (
    TimingData,
    LatencyStats,
    TimingReport,
    TimingCollector,
    LatencyAnalyzer,
    ConsoleReporter,
    CSVReporter,
)

__all__ = [
    "MT5Bridge",
    "MT5BridgeError",
    "NotConnectedError",
    "TradeError",
    "OrderType",
    "Timeframe",
    "OrderResult",
    "TickData",
    "AccountInfo",
    "Position",
    "CloseResult",
    # Timing
    "TimingData",
    "LatencyStats",
    "TimingReport",
    "TimingCollector",
    "LatencyAnalyzer",
    "ConsoleReporter",
    "CSVReporter",
]

__version__ = "0.1.0"
