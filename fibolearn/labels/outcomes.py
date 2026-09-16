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
    def to_dict(self):
        d=asdict(self)
        for k,v in list(d.items()):
            if isinstance(v,Decimal): d[k]=str(v)
        return d

def _hit(obs, candle, level):
    return D(candle[3]) <= level <= D(candle[2])

def label_outcomes(obs: LadderObservation, future_candles: List[list], *, cycle_closed_ts_ms: int | None=None) -> OutcomeLabels:
    t1=t2=tp=None; retpn=False; retpm1=False
    for k in future_candles:
        ts=int(k[0])
        if t1 is None and _hit(obs,k,obs.pn_plus_1): t1=ts-obs.timestamp_ms
        if t2 is None and _hit(obs,k,obs.pn_plus_2): t2=ts-obs.timestamp_ms
        if _hit(obs,k,obs.pn): retpn=True
        if obs.pn_minus_1 is not None and _hit(obs,k,obs.pn_minus_1): retpm1=True
    if not future_candles:
        return OutcomeLabels(False,False,False,False,False,False,None,None,None,None,None)
    highs=[D(k[2]) for k in future_candles]; lows=[D(k[3]) for k in future_candles]
    if obs.direction == 'BUY': mfe=max(highs)-obs.current_price; mae=min(lows)-obs.current_price
    else: mfe=obs.current_price-min(lows); mae=obs.current_price-max(highs)
    return OutcomeLabels(t1 is not None,t2 is not None,retpn,retpm1,cycle_closed_ts_ms is not None,False,t1,t2,(cycle_closed_ts_ms-obs.timestamp_ms) if cycle_closed_ts_ms is not None else None,mfe,mae)
