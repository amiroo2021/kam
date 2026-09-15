"""Historical market-data source interface (OHLC now; trades/ticks later)."""

from __future__ import annotations

from typing import Iterator, List, Protocol, Sequence, runtime_checkable


@runtime_checkable
class HistoricalBarSource(Protocol):
    """Yields exchange kline-shaped rows: [open_ms, o, h, l, c, base_vol, ..., quote_vol]."""

    def fetch_range(
        self,
        *,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> List[list]:
        ...


@runtime_checkable
class HistoricalTradeSource(Protocol):
    """Future: ordered historical trades → same MarketEvent pipeline upstream of the engine."""

    def iter_trades(
        self,
        *,
        symbol: str,
        start_ms: int,
        end_ms: int,
    ) -> Iterator[dict]:
        ...
