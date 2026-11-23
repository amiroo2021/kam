from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from .historical import HistoricalRequest
from .kline_cache import CachedBinanceKlineSource, CachePolicy, KlineCache


class BinanceHistoricalProvider:
    provider_id = "binance"

    def __init__(self, cache: KlineCache, *, source: CachedBinanceKlineSource | None = None, refresh_tail_ms: int = 15 * 60_000) -> None:
        self.cache = cache
        self.source = source or CachedBinanceKlineSource(cache=cache, refresh_tail_ms=refresh_tail_ms)

    def fetch_range(self, request: HistoricalRequest, *, market: str = "spot", on_progress: Callable[[dict], None] | None = None) -> list[list]:
        return self.source.fetch_range(
            symbol=request.instrument,
            interval="1m",
            start_ms=int(request.start_ms),
            end_ms=int(request.end_ms),
            market=market,
            policy=CachePolicy.AUTO,
            closed_only_before_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            on_progress=on_progress,
        )
