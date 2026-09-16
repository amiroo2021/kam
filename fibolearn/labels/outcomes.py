from __future__ import annotations
from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Any, Dict, List
from fibolearn.collector.state_adapter import LadderObservation
def D(x): return Decimal(str(x))

@dataclass(frozen=True)
class OutcomeLabels:
    reached_pn_plus_1: bool
    reached_pn_plus_2: bool
    returned_to_pn: bool
    returned_to_pn_minus_1: bool
    tp_cycle_closed: bool
    cycle_failure_reversal: bool
    time_to_pn_plus_1_ms: int | None
    time_to_pn_plus_2_ms: int | None
    time_to_tp_closure_ms: int | None
    mfe: Decimal | None
    mae: Decimal | None
    event_sequence: List[Dict[str, Any]]
    def to_dict(self):
        d=asdict(self)
        for k,v in list(d.items()):
            if isinstance(v,Decimal): d[k]=str(v)
        return d

def _hit(candle, level): return D(candle[3]) <= level <= D(candle[2])

def _hit_progression(obs, candle, level):
    return D(candle[2]) >= level if obs.direction == 'BUY' else D(candle[3]) <= level

def _hit_return(obs, candle, level):
    return D(candle[3]) <= level if obs.direction == 'BUY' else D(candle[2]) >= level

def complete_observation_outcome(obs: LadderObservation, future_candles: List[list], *, cycle_closed_ts_ms: int | None=None) -> OutcomeLabels:
    sequence=[]; seen=set()
    for k in future_candles:
        ts=int(k[0])
        checks=[('returned_to_pn', obs.pn, 'return'), ('reached_pn_plus_1', obs.pn_plus_1, 'progress'), ('reached_pn_plus_2', obs.pn_plus_2, 'progress')]
        if obs.pn_minus_1 is not None: checks.append(('returned_to_pn_minus_1', obs.pn_minus_1, 'return'))
        for name, level, kind in checks:
            hit = _hit_progression(obs, k, level) if kind == 'progress' else _hit_return(obs, k, level)
            if name not in seen and hit:
                seen.add(name); sequence.append({'event': name, 'timestamp_ms': ts, 'elapsed_ms': ts-obs.timestamp_ms, 'level': str(level)})
        if cycle_closed_ts_ms is not None and ts >= cycle_closed_ts_ms and 'tp_cycle_closed' not in seen:
            seen.add('tp_cycle_closed'); sequence.append({'event': 'tp_cycle_closed', 'timestamp_ms': cycle_closed_ts_ms, 'elapsed_ms': cycle_closed_ts_ms-obs.timestamp_ms})
    def t(name):
        for e in sequence:
            if e['event']==name: return int(e['elapsed_ms'])
        return None
    if future_candles:
        highs=[D(k[2]) for k in future_candles]; lows=[D(k[3]) for k in future_candles]
        if obs.direction == 'BUY': mfe=max(highs)-obs.current_price; mae=min(lows)-obs.current_price
        else: mfe=obs.current_price-min(lows); mae=obs.current_price-max(highs)
    else: mfe=mae=None
    return OutcomeLabels('reached_pn_plus_1' in seen, 'reached_pn_plus_2' in seen, 'returned_to_pn' in seen, 'returned_to_pn_minus_1' in seen, 'tp_cycle_closed' in seen, False, t('reached_pn_plus_1'), t('reached_pn_plus_2'), t('tp_cycle_closed'), mfe, mae, sequence)

def label_outcomes(obs: LadderObservation, future_candles: List[list], *, cycle_closed_ts_ms: int | None=None) -> OutcomeLabels:
    return complete_observation_outcome(obs, future_candles, cycle_closed_ts_ms=cycle_closed_ts_ms)
