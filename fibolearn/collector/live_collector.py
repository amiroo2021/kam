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
        snaps = replay_multiscale(symbol, candles, percentages=self.settings.percentages, directions=self.settings.directions, timeframe=self.settings.timeframe, use_optimized_market=True)
        for snap in snaps:
            self.store.save_observation(snap)
        return len(snaps)

    def collect_historical_streaming(self, symbol: str, candles_iter, *, total: int | None = None) -> int:
        """Streaming replay+save. Generates snapshots one at a time to bound
        memory on long historical windows and yields progress information.

        Returns the number of snapshots persisted.
        """
        from fibolearn.backtest.engine import _new_engine
        from fibolearn.features.price_action import price_action_features
        from goldenfibo.engine.config import OhlcResolveMode
        from goldenfibo.session.runner import apply_ohlc_page
        from fibolearn.features.definitions import level_record
        from fibolearn.features.multiscale import build_multiscale_vector
        from fibolearn.backtest.engine import _RunningMarket, _D
        engs = {(str(p), d): _new_engine(symbol, d, _D(str(p))) for p in self.settings.percentages for d in self.settings.directions}
        retests = {(str(p), d): 0 for p in self.settings.percentages for d in self.settings.directions}
        last_prog = {(str(p), d): None for p in self.settings.percentages for d in self.settings.directions}
        last_step = {(str(p), d): None for p in self.settings.percentages for d in self.settings.directions}
        last_n = {(str(p), d): -1 for p in self.settings.percentages for d in self.settings.directions}
        running = _RunningMarket(swing_window=20, profile_bins=80)
        sofar = []
        n = 0
        for k in candles_iter:
            sofar.append(k)
            ts = int(k[0]); price = _D(k[4]); high = _D(k[2]); low = _D(k[3])
            ladders = {}; raw_events = {}
            for pct in self.settings.percentages:
                pct_s = str(pct); sides = {}
                for d in self.settings.directions:
                    key = (pct_s, d); eng = engs[key]
                    res = apply_ohlc_page(eng, [k], mode=OhlcResolveMode.LEGACY)
                    raw_events[f'{pct_s}:{d}'] = [e.kind.value for e in res.domain]
                    st = eng.state; stn = int(st.highest_filled)
                    if stn != last_n[key]:
                        last_step[key] = ts; last_prog[key] = ts; last_n[key] = stn
                    from fibolearn.collector.state_adapter import observation_from_engine
                    obs = observation_from_engine(symbol, ts, _D(pct_s), d, eng, price, retests=retests[key], high=high, low=low, last_progression_ts_ms=last_prog[key], last_step_change_ts_ms=last_step[key])
                    if low <= obs.pn <= high:
                        retests[key] += 1
                    sides[d] = obs
                ladders[pct_s] = sides
            ctx = running.push(k); market = ctx.as_dict()
            pa = price_action_features(sofar, active_pn=price, active_since_ms=max(0, ts - 20 * 60_000))
            level_values = {'VWAP': market.get('vwap'), 'POC': market.get('poc'), 'VAH': market.get('vah'), 'VAL': market.get('val'), 'swing_high': market.get('swing_high'), 'swing_low': market.get('swing_low'), 'previous_day_high': market.get('previous_day_high'), 'previous_day_low': market.get('previous_day_low')}
            features = {
                'price_action': pa,
                'significant_levels': {kk.lower() if kk in {'VWAP', 'POC', 'VAH', 'VAL'} else kk: v for kk, v in level_values.items()},
                'significant_levels_versioned': {kk: level_record(kk, ts, v) for kk, v in level_values.items()},
            }
            vec = build_multiscale_vector(symbol, ts, price, percentages=self.settings.percentages, directions=self.settings.directions, market=market, raw={'source_candles': len(sofar), 'last_candle': k, 'domain_events': raw_events}, ladders=ladders, features=features)
            self.store.save_observation(vec)
            # Bound retained candle buffer to last 20 for price-action history.
            if len(sofar) > 30:
                sofar = sofar[-20:]
            n += 1
        return n

    def collect_once_from_cache(self, symbol: str, limit: int = 240) -> int:
        from fibolearn.data.goldenfibo_data import fetch_recent_cached
        from fibolearn.config.defaults import SYMBOL_TO_BINANCE_SPOT
        candles = fetch_recent_cached(SYMBOL_TO_BINANCE_SPOT.get(symbol, symbol+'USDT'), self.settings.timeframe, limit=limit)
        return self.collect_historical(symbol, candles[-limit:]) if candles else 0
    def run_forever(self) -> None:
        self.store.set_running_state(True)
        while self.store.get_running_state():
            for symbol in self.settings.symbols:
                self.collect_once_from_cache(symbol, limit=240)
            time.sleep(max(1, int(self.settings.interval_seconds)))
