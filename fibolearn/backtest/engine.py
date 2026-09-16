from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, List, Sequence

from fibolearn.goldenfibo_compat import GF_ROOT  # noqa: F401
from goldenfibo.engine.config import EngineConfig, OhlcResolveMode, Side
from goldenfibo.engine.engine import GoldenFiboEngine
from goldenfibo.session.runner import apply_ohlc_page

from fibolearn.collector.state_adapter import observation_from_engine
from fibolearn.config.defaults import DEFAULT_DIRECTIONS, DEFAULT_PERCENTAGES
from fibolearn.features.market import vwap_series, volume_profile
from fibolearn.features.multiscale import build_multiscale_vector, MultiScaleVector
from fibolearn.features.price_action import price_action_features
from fibolearn.features.definitions import FEATURE_DEFINITION, level_record
from datetime import datetime, timezone

def D(x): return Decimal(str(x))

def _new_engine(symbol: str, direction: str, pct: Decimal):
    return GoldenFiboEngine(EngineConfig(side=Side(direction), percentage=Decimal(str(pct)), symbol=symbol.upper()))

def _utc_day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms/1000, timezone.utc).strftime('%Y-%m-%d')

def _market_for(candles: list[list]) -> dict:
    last = candles[-1]
    ts = int(last[0])
    day = _utc_day(ts)
    daily = [k for k in candles if _utc_day(int(k[0])) == day]
    prior_days = sorted({_utc_day(int(k[0])) for k in candles if _utc_day(int(k[0])) < day})
    prev = [k for k in candles if prior_days and _utc_day(int(k[0])) == prior_days[-1]]
    vwaps = vwap_series(daily)
    prof = volume_profile(daily, bins=80)
    highs = [D(k[2]) for k in candles]
    lows = [D(k[3]) for k in candles]
    prev_high = max([D(k[2]) for k in prev]) if prev else None
    prev_low = min([D(k[3]) for k in prev]) if prev else None
    return {
        'price': D(last[4]), 'volume': D(last[5]), 'vwap': vwaps[-1].vwap if vwaps else None,
        'vwap_slope': vwaps[-1].slope if vwaps else None, 'vwap_migration': vwaps[-1].migration if vwaps else None,
        'poc': prof.poc, 'vah': prof.vah, 'val': prof.val,
        'swing_high': max(highs[-20:]), 'swing_low': min(lows[-20:]),
        'previous_day_high': prev_high, 'previous_day_low': prev_low,
        'vwap_definition': FEATURE_DEFINITION['VWAP']['calculation_definition'],
        'profile_definition': FEATURE_DEFINITION['POC']['calculation_definition'],
        'session_definition': 'UTC daily session, no look-ahead, current day through observation timestamp',
    }

def replay_multiscale(symbol: str, candles: List[list], *, percentages=DEFAULT_PERCENTAGES, directions=DEFAULT_DIRECTIONS, timeframe: str='1m') -> List[MultiScaleVector]:
    engines = {(str(p), d): _new_engine(symbol, d, Decimal(str(p))) for p in percentages for d in directions}
    retests = {(str(p), d): 0 for p in percentages for d in directions}
    last_prog = {(str(p), d): None for p in percentages for d in directions}
    last_step = {(str(p), d): None for p in percentages for d in directions}
    last_n = {(str(p), d): -1 for p in percentages for d in directions}
    out: list[MultiScaleVector] = []
    sofar: list[list] = []
    for k in candles:
        sofar.append(k)
        ts = int(k[0]); price = D(k[4]); high=D(k[2]); low=D(k[3])
        ladders = {}
        raw_events = {}
        for pct in percentages:
            pct_s = str(pct); sides = {}
            for d in directions:
                key=(pct_s,d); eng=engines[key]
                res = apply_ohlc_page(eng, [k], mode=OhlcResolveMode.LEGACY)
                raw_events[f'{pct_s}:{d}'] = [e.kind.value for e in res.domain]
                st=eng.state; n=int(st.highest_filled)
                if n != last_n[key]:
                    last_step[key] = ts; last_prog[key] = ts; last_n[key]=n
                obs = observation_from_engine(symbol, ts, Decimal(pct_s), d, eng, price, retests=retests[key], high=high, low=low, last_progression_ts_ms=last_prog[key], last_step_change_ts_ms=last_step[key])
                # Retest is counted after active level known for future observations.
                if low <= obs.pn <= high:
                    retests[key] += 1
                sides[d] = obs
            ladders[pct_s] = sides
        market = _market_for(sofar)
        pa = price_action_features(sofar, active_pn=price, active_since_ms=max(0, ts - 20*60_000))
        level_values = {'VWAP': market.get('vwap'), 'POC': market.get('poc'), 'VAH': market.get('vah'), 'VAL': market.get('val'), 'swing_high': market.get('swing_high'), 'swing_low': market.get('swing_low'), 'previous_day_high': market.get('previous_day_high'), 'previous_day_low': market.get('previous_day_low')}
        features = {
            'price_action': pa,
            'significant_levels': {k.lower() if k in {'VWAP','POC','VAH','VAL'} else k: v for k,v in level_values.items()},
            'significant_levels_versioned': {k: level_record(k, ts, v) for k,v in level_values.items()},
        }
        out.append(build_multiscale_vector(symbol, ts, price, percentages=percentages, directions=directions, market=market, raw={'source_candles': len(sofar), 'last_candle': k, 'domain_events': raw_events}, ladders=ladders, features=features))
    return out
