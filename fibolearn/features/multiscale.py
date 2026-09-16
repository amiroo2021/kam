from __future__ import annotations
from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Any, Dict, Iterable
from fibolearn.collector.state_adapter import LadderStateInput, LadderObservation, extract_ladder_observation
from fibolearn.config.defaults import DEFAULT_DIRECTIONS, DEFAULT_PERCENTAGES

@dataclass(frozen=True)
class MultiScaleVector:
    symbol: str
    timestamp_ms: int
    market: Dict[str, Any]
    ladders: Dict[str, Dict[str, LadderObservation]]
    features: Dict[str, Any]
    raw: Dict[str, Any]

    def to_dict(self):
        return {'symbol':self.symbol,'timestamp_ms':self.timestamp_ms,'market':_ser(self.market),'ladders':{p:{d:o.to_dict() for d,o in sides.items()} for p,sides in self.ladders.items()},'features':_ser(self.features),'raw':_ser(self.raw)}

def _ser(x):
    if isinstance(x, Decimal): return str(x)
    if isinstance(x, dict): return {k:_ser(v) for k,v in x.items()}
    if isinstance(x, (list,tuple)): return [_ser(v) for v in x]
    return x

def build_multiscale_vector(symbol: str, timestamp_ms: int, price: Decimal, *, percentages=DEFAULT_PERCENTAGES, directions=DEFAULT_DIRECTIONS, market: Dict[str, Any] | None=None, raw: Dict[str, Any] | None=None) -> MultiScaleVector:
    ladders={}
    for pct in percentages:
        sides={}
        for direction in directions:
            sides[direction]=extract_ladder_observation(LadderStateInput(symbol=symbol,timestamp_ms=timestamp_ms,direction=direction,percentage=Decimal(str(pct)),cycle_id=1,p0=price,active_step=0,current_price=price,active_since_ms=timestamp_ms))
        ladders[str(pct)] = sides
    vec=MultiScaleVector(symbol, timestamp_ms, {'price':price, **(market or {})}, ladders, {}, raw or {})
    feats=cross_scale_relationships(vec)
    object.__setattr__(vec, 'features', feats)
    return vec

def cross_scale_relationships(vec: MultiScaleVector) -> Dict[str, Any]:
    buy_depth=sum(s['BUY'].active_step for s in vec.ladders.values() if 'BUY' in s)
    sell_depth=sum(s['SELL'].active_step for s in vec.ladders.values() if 'SELL' in s)
    pairs={}
    for pct,sides in vec.ladders.items():
        if 'BUY' in sides and 'SELL' in sides:
            pairs[pct]=sides['BUY'].pn - sides['SELL'].pn
    return {'buy_sell_progression_imbalance': buy_depth-sell_depth, 'cross_percentage_active_depth': {p:{d:o.active_step for d,o in sides.items()} for p,sides in vec.ladders.items()}, 'buy_sell_level_distance': pairs}
