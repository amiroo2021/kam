from __future__ import annotations
from decimal import Decimal
from typing import Dict, List
def D(x): return Decimal(str(x))

def price_action_features(candles: List[list], *, active_pn: Decimal, active_since_ms: int) -> Dict[str, object]:
    rel=[k for k in candles if int(k[0])>=int(active_since_ms)] or candles
    highs=[D(k[2]) for k in rel]; lows=[D(k[3]) for k in rel]; closes=[D(k[4]) for k in rel]
    hh=len(highs)>1 and highs[-1] > max(highs[:-1])
    hl=len(lows)>1 and lows[-1] > min(lows[:-1])
    lh=len(highs)>1 and highs[-1] < max(highs[:-1])
    ll=len(lows)>1 and lows[-1] < min(lows[:-1])
    above=[h>=active_pn for h in highs]; below=[l<=active_pn for l in lows]
    retests=sum(1 for k in rel if D(k[3]) <= active_pn <= D(k[2]))
    ranges=[D(k[2])-D(k[3]) for k in rel]
    return {
      'higher_high': hh, 'higher_low': hl, 'lower_high': lh, 'lower_low': ll,
      'breakout_above_pn': any(above), 'breakout_below_pn': any(below),
      'pn_retest': retests>0, 'pn_retest_count': retests, 'failed_pn_retest': retests>0 and closes[-1] < active_pn,
      'no_retest_duration_ms': (int(rel[-1][0])-int(active_since_ms)) if retests==0 else 0,
      'breakout_pullback_continuation': any(above) and retests>0 and closes[-1] > active_pn,
      'breakout_rejection': any(above) and closes[-1] < active_pn,
      'new_high_after_pullback': hh and retests>0, 'new_low_after_pullback': ll and retests>0,
      'pullback_distance': max(highs)-min(lows), 'time_between_events_ms': int(rel[-1][0])-int(rel[0][0]),
      'velocity': (closes[-1]-closes[0]) / Decimal(max(1, int(rel[-1][0])-int(rel[0][0]))),
      'volatility_range': sum(ranges)/Decimal(len(ranges)),
    }
