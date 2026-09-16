from __future__ import annotations
from decimal import Decimal
from typing import Any, Dict
from fibolearn.collector.state_adapter import LadderObservation

def D(x): return Decimal(str(x))

def significant_levels_from_market(market: Dict[str, Any], features: Dict[str, Any] | None = None) -> Dict[str, Decimal]:
    out: Dict[str, Decimal] = {}
    for src in (market, (features or {}).get('significant_levels', {})):
        for key in ('vwap','poc','vah','val','swing_high','swing_low','previous_day_high','previous_day_low','VWAP','POC','VAH','VAL'):
            if isinstance(src, dict) and src.get(key) is not None:
                label = key.upper() if key.lower() in {'vwap','poc','vah','val'} else key
                out[label] = D(src[key])
    return out

def significant_context_for_ladder(obs: LadderObservation, market: Dict[str, Any], features: Dict[str, Any] | None = None) -> Dict[str, Dict[str, Decimal]]:
    levels = significant_levels_from_market(market, features)
    return {
        'pn_plus_1': {name: obs.pn_plus_1 - level for name, level in levels.items()},
        'pn_plus_2': {name: obs.pn_plus_2 - level for name, level in levels.items()},
    }

def nearest_level(distances: Dict[str, Decimal]) -> tuple[str, Decimal] | None:
    if not distances: return None
    return min(distances.items(), key=lambda kv: abs(kv[1]))
