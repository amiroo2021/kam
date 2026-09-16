from __future__ import annotations
import time
from fibolearn.goldenfibo_compat import GF_ROOT  # noqa: F401
from goldenfibo.marketdata.kline_cache import KlineCache, CachePolicy, fetch_range_cached
from goldenfibo.marketdata.timeframes import interval_ms

def fetch_range(symbol: str, timeframe: str, start_ms: int, end_ms: int, *, policy: str = 'AUTO') -> list[list]:
    return fetch_range_cached(symbol, timeframe, start_ms, end_ms, cache=KlineCache(), policy=CachePolicy(policy)).klines

def fetch_recent_cached(symbol: str, timeframe: str = '1m', *, limit: int = 240) -> list[list]:
    step = interval_ms(timeframe); now = int(time.time()*1000); end = (now//step)*step; start=end - limit*step
    return fetch_range(symbol, timeframe, start, end, policy='AUTO')
