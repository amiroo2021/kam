from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Sequence, Tuple

from fibolearn.features.definitions import FEATURE_DEFINITION, level_record
from fibolearn.goldenfibo_compat import GF_ROOT  # noqa: F401
from goldenfibo.engine.config import EngineConfig, OhlcResolveMode, Side
from goldenfibo.engine.engine import GoldenFiboEngine
from goldenfibo.session.runner import apply_ohlc_page

from fibolearn.collector.state_adapter import observation_from_engine
from fibolearn.config.defaults import DEFAULT_DIRECTIONS, DEFAULT_PERCENTAGES
from fibolearn.features.multiscale import build_multiscale_vector, MultiScaleVector


_D = Decimal
_DAY_MS = 86_400_000


def _utc_day_int(ts_ms: int) -> int:
    return ts_ms // _DAY_MS


@dataclass
class MarketContext:
    timestamp_ms: int
    price: Decimal
    volume: Decimal
    vwap: Decimal | None
    vwap_slope: Decimal | None
    vwap_migration: Decimal | None
    poc: Decimal | None
    vah: Decimal | None
    val: Decimal | None
    swing_high: Decimal | None
    swing_low: Decimal | None
    previous_day_high: Decimal | None
    previous_day_low: Decimal | None

    def as_dict(self) -> Dict[str, Any]:
        return {
            'price': self.price, 'volume': self.volume, 'vwap': self.vwap,
            'vwap_slope': self.vwap_slope, 'vwap_migration': self.vwap_migration,
            'poc': self.poc, 'vah': self.vah, 'val': self.val,
            'swing_high': self.swing_high, 'swing_low': self.swing_low,
            'previous_day_high': self.previous_day_high, 'previous_day_low': self.previous_day_low,
            'vwap_definition': FEATURE_DEFINITION['VWAP']['calculation_definition'],
            'profile_definition': FEATURE_DEFINITION['POC']['calculation_definition'],
            'session_definition': 'UTC daily session, no look-ahead, current day through observation timestamp',
        }


class _BinIndex:
    """Histogram bin index that supports range expansion without full rebuild.

    The strategy: keep all raw ranges seen today, but ALSO keep a sentinel
    "fully-indexed" view: if the day's range has been stable for N candles
    AND fits in the current bin grid, treat the histogram as valid and just
    add the new candle's volume to the existing bins. If the range expands,
    fall back to a one-shot full rebuild (which is O(n) but only happens once
    per expansion, which is rare).
    """

    def __init__(self, bins: int):
        self.bins = bins
        self.lo: Decimal | None = None
        self.hi: Decimal | None = None
        self.width: Decimal | None = None
        self.vol: List[Decimal] | None = None
        self.ranges: List[Tuple[Decimal, Decimal, Decimal]] = []

    def add(self, c_lo: Decimal, c_hi: Decimal, vol: Decimal) -> None:
        if self.lo is None:
            self.lo = c_lo
        if self.hi is None or c_hi > self.hi:
            self.hi = c_hi
        if self.lo > c_lo:
            self.lo = c_lo
        self.ranges.append((c_lo, c_hi, vol))
        if self.width is None:
            self._full_rebuild()
            return
        # If range expanded, fall back to full rebuild (rare).
        if c_lo < self.lo or c_hi > self.hi:
            self._full_rebuild()
            return
        a = max(0, min(self.bins - 1, int((c_lo - self.lo) / self.width)))
        b = max(0, min(self.bins - 1, int((c_hi - self.lo) / self.width)))
        share = vol / Decimal(max(1, b - a + 1))
        for i in range(a, b + 1):
            self.vol[i] += share

    def _full_rebuild(self) -> None:
        if self.lo is None or self.hi is None or self.hi <= self.lo:
            return
        bins = self.bins
        width = (self.hi - self.lo) / Decimal(bins)
        vol = [Decimal(0) for _ in range(bins)]
        for c_lo, c_hi, v in self.ranges:
            a = max(0, min(bins - 1, int((c_lo - self.lo) / width)))
            b = max(0, min(bins - 1, int((c_hi - self.lo) / width)))
            share = v / Decimal(max(1, b - a + 1))
            for i in range(a, b + 1):
                vol[i] += share
        self.width = width; self.vol = vol


def _poc_va(idx: _BinIndex):
    if idx.vol is None or idx.width is None or idx.lo is None:
        return None, None, None
    bins = idx.bins
    vol = idx.vol
    poc_i = max(range(bins), key=lambda i: vol[i])
    poc = idx.lo + (Decimal(poc_i) + Decimal('0.5')) * idx.width
    total = sum(vol, Decimal(0))
    target = total * Decimal('0.70')
    cum = vol[poc_i]
    left = right = poc_i
    while cum < target and (left > 0 or right < bins - 1):
        lv = vol[left - 1] if left > 0 else Decimal(-1)
        rv = vol[right + 1] if right < bins - 1 else Decimal(-1)
        if rv >= lv and right < bins - 1:
            right += 1; cum += vol[right]
        elif left > 0:
            left -= 1; cum += vol[left]
        else:
            break
    val = idx.lo + Decimal(left) * idx.width
    vah = idx.lo + Decimal(right + 1) * idx.width
    return poc, vah, val


class _RunningMarket:
    def __init__(self, swing_window: int = 20, profile_bins: int = 80):
        self.swing_window = swing_window
        self.profile_bins = profile_bins
        self._day: int | None = None
        self._day_base = Decimal(0)
        self._day_quote = Decimal(0)
        self._prev_vwap: Decimal | None = None
        self._idx: _BinIndex | None = None
        self._swing_highs: List[Decimal] = []
        self._swing_lows: List[Decimal] = []
        self._prev_day_high: Decimal | None = None
        self._prev_day_low: Decimal | None = None

    def push(self, k: Sequence) -> MarketContext:
        ts = int(k[0]); price = _D(k[4]); vol = _D(k[5])
        qv = _D(k[7]) if len(k) > 7 and k[7] is not None else price * vol
        day = _utc_day_int(ts)
        if day != self._day:
            self._roll_day()
            self._day = day
        self._day_base += vol
        self._day_quote += qv
        c_lo = _D(k[3]); c_hi = _D(k[2])
        if self._idx is None:
            self._idx = _BinIndex(self.profile_bins)
        self._idx.add(c_lo, c_hi, vol)
        poc, vah, val = _poc_va(self._idx)
        vwap = self._day_quote / self._day_base if self._day_base else price
        slope = (vwap - self._prev_vwap) if self._prev_vwap is not None else None
        self._prev_vwap = vwap
        self._swing_highs.append(c_hi)
        self._swing_lows.append(c_lo)
        if len(self._swing_highs) > self.swing_window:
            self._swing_highs.pop(0)
            self._swing_lows.pop(0)
        return MarketContext(
            timestamp_ms=ts, price=price, volume=vol, vwap=vwap, vwap_slope=slope, vwap_migration=slope,
            poc=poc, vah=vah, val=val,
            swing_high=max(self._swing_highs), swing_low=min(self._swing_lows),
            previous_day_high=self._prev_day_high, previous_day_low=self._prev_day_low,
        )

    def _roll_day(self):
        if self._idx is not None and self._idx.ranges:
            self._prev_day_high = max(c_hi for _, c_hi, _ in self._idx.ranges)
            self._prev_day_low = min(c_lo for c_lo, _, _ in self._idx.ranges)
        self._day_base = Decimal(0); self._day_quote = Decimal(0)
        self._idx = None
        self._prev_vwap = None


def _new_engine(symbol: str, direction: str, pct: Decimal):
    return GoldenFiboEngine(EngineConfig(side=Side(direction), percentage=Decimal(str(pct)), symbol=symbol.upper()))


def _legacy_market(candles: list[list]) -> Dict[str, Any]:
    from datetime import datetime, timezone
    from fibolearn.features.market import vwap_series, volume_profile
    last = candles[-1]
    ts = int(last[0])
    day = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime('%Y-%m-%d')
    daily = [k for k in candles if datetime.fromtimestamp(int(k[0]) / 1000, timezone.utc).strftime('%Y-%m-%d') == day]
    prior_days = sorted({datetime.fromtimestamp(int(k[0]) / 1000, timezone.utc).strftime('%Y-%m-%d') for k in candles if datetime.fromtimestamp(int(k[0]) / 1000, timezone.utc).strftime('%Y-%m-%d') < day})
    prev = [k for k in candles if prior_days and datetime.fromtimestamp(int(k[0]) / 1000, timezone.utc).strftime('%Y-%m-%d') == prior_days[-1]]
    vwaps = vwap_series(daily)
    prof = volume_profile(daily, bins=80)
    highs = [_D(k[2]) for k in candles]
    lows = [_D(k[3]) for k in candles]
    prev_high = max([_D(k[2]) for k in prev]) if prev else None
    prev_low = min([_D(k[3]) for k in prev]) if prev else None
    return {
        'price': _D(last[4]), 'volume': _D(last[5]), 'vwap': vwaps[-1].vwap if vwaps else None,
        'vwap_slope': vwaps[-1].slope if vwaps else None, 'vwap_migration': vwaps[-1].migration if vwaps else None,
        'poc': prof.poc, 'vah': prof.vah, 'val': prof.val,
        'swing_high': max(highs[-20:]), 'swing_low': min(lows[-20:]),
        'previous_day_high': prev_high, 'previous_day_low': prev_low,
        'vwap_definition': FEATURE_DEFINITION['VWAP']['calculation_definition'],
        'profile_definition': FEATURE_DEFINITION['POC']['calculation_definition'],
        'session_definition': 'UTC daily session, no look-ahead, current day through observation timestamp',
    }


def replay_multiscale(symbol: str, candles: List[list], *, percentages=DEFAULT_PERCENTAGES, directions=DEFAULT_DIRECTIONS, timeframe: str = '1m', use_optimized_market: bool = False) -> List[MultiScaleVector]:
    engines = {(str(p), d): _new_engine(symbol, d, _D(str(p))) for p in percentages for d in directions}
    retests = {(str(p), d): 0 for p in percentages for d in directions}
    last_prog = {(str(p), d): None for p in percentages for d in directions}
    last_step = {(str(p), d): None for p in percentages for d in directions}
    last_n = {(str(p), d): -1 for p in percentages for d in directions}
    out: List[MultiScaleVector] = []
    sofar: List[list] = []
    running = _RunningMarket(swing_window=20, profile_bins=80) if use_optimized_market else None
    from fibolearn.features.price_action import price_action_features
    for k in candles:
        sofar.append(k)
        ts = int(k[0]); price = _D(k[4]); high = _D(k[2]); low = _D(k[3])
        ladders = {}; raw_events = {}
        for pct in percentages:
            pct_s = str(pct); sides = {}
            for d in directions:
                key = (pct_s, d); eng = engines[key]
                res = apply_ohlc_page(eng, [k], mode=OhlcResolveMode.LEGACY)
                raw_events[f'{pct_s}:{d}'] = [e.kind.value for e in res.domain]
                st = eng.state; n = int(st.highest_filled)
                if n != last_n[key]:
                    last_step[key] = ts; last_prog[key] = ts; last_n[key] = n
                obs = observation_from_engine(symbol, ts, _D(pct_s), d, eng, price, retests=retests[key], high=high, low=low, last_progression_ts_ms=last_prog[key], last_step_change_ts_ms=last_step[key])
                if low <= obs.pn <= high:
                    retests[key] += 1
                sides[d] = obs
            ladders[pct_s] = sides
        if running is not None:
            ctx = running.push(k)
            market = ctx.as_dict()
        else:
            market = _legacy_market(sofar)
        pa = price_action_features(sofar, active_pn=price, active_since_ms=max(0, ts - 20 * 60_000))
        level_values = {'VWAP': market.get('vwap'), 'POC': market.get('poc'), 'VAH': market.get('vah'), 'VAL': market.get('val'), 'swing_high': market.get('swing_high'), 'swing_low': market.get('swing_low'), 'previous_day_high': market.get('previous_day_high'), 'previous_day_low': market.get('previous_day_low')}
        features = {
            'price_action': pa,
            'significant_levels': {k.lower() if k in {'VWAP', 'POC', 'VAH', 'VAL'} else k: v for k, v in level_values.items()},
            'significant_levels_versioned': {k: level_record(k, ts, v) for k, v in level_values.items()},
        }
        out.append(build_multiscale_vector(symbol, ts, price, percentages=percentages, directions=directions, market=market, raw={'source_candles': len(sofar), 'last_candle': k, 'domain_events': raw_events}, ladders=ladders, features=features))
    return out
