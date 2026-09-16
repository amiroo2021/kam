from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Sequence
import time

from fibolearn.config.defaults import DEFAULT_PERCENTAGES, DEFAULT_SYMBOLS, SYMBOL_TO_BINANCE_SPOT
from fibolearn.backtest.engine import replay_multiscale
from fibolearn.storage.sqlite_store import FiboLearnStore

@dataclass(frozen=True)
class CollectorSettings:
    interval_seconds: int = 60
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    percentages: tuple[Decimal, ...] = DEFAULT_PERCENTAGES
    directions: tuple[str, ...] = ('BUY','SELL')
    timeframe: str = '1m'

class FiboLearnCollector:
    def __init__(self, store: FiboLearnStore, *, settings: CollectorSettings | None = None):
        self.store=store; self.settings=settings or CollectorSettings()
    def collect_historical(self, symbol: str, candles: list[list]) -> int:
        snaps = replay_multiscale(symbol, candles, percentages=self.settings.percentages, directions=self.settings.directions, timeframe=self.settings.timeframe)
        for snap in snaps:
            self.store.save_observation(snap)
        return len(snaps)
    def collect_once_from_cache(self, symbol: str, limit: int = 240) -> int:
        from fibolearn.data.goldenfibo_data import fetch_recent_cached
        candles = fetch_recent_cached(SYMBOL_TO_BINANCE_SPOT.get(symbol, symbol+'USDT'), self.settings.timeframe, limit=limit)
        return self.collect_historical(symbol, candles[-limit:]) if candles else 0
    def run_forever(self) -> None:
        self.store.set_running_state(True)
        while self.store.get_running_state():
            for symbol in self.settings.symbols:
                self.collect_once_from_cache(symbol, limit=240)
            time.sleep(max(1, int(self.settings.interval_seconds)))
