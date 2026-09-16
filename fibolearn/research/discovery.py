from __future__ import annotations
from math import sqrt
from typing import Any, Dict, List
from fibolearn.research.patterns import Pattern

def create_candidate_from_live_state(state: Dict[str,Any]) -> Pattern:
    symbol=state.get('symbol','UNKNOWN')
    p=Pattern.create('live-study-setup', {'frozen_state': state}, {'live_timestamp_ms': state.get('timestamp_ms')}, [symbol])
    p.mark_candidate(); return p

def _ci(rate: float, n: int):
    if n<=0: return (0.0,0.0)
    m=1.96*sqrt(max(0.0, rate*(1-rate)/n)); return (max(0.0,rate-m), min(1.0,rate+m))

def discover_candidates(rows: List[Dict[str,Any]], *, min_samples:int=30) -> List[Pattern]:
    rows=[r for r in rows if 'hit' in r]
    if not rows: return []
    baseline=sum(1 for r in rows if r.get('hit'))/len(rows)
    candidates=[]
    tests=[]
    for field in ('direction','percentage','progressing'):
        vals=sorted({str(r.get(field)) for r in rows})
        for v in vals:
            subset=[r for r in rows if str(r.get(field))==v]
            if len(subset)>=min_samples:
                tests.append((field,v,subset))
    for field,v,subset in tests:
        rate=sum(1 for r in subset if r.get('hit'))/len(subset); effect=rate-baseline
        # Conservative first version: keep interpretable candidates with sample-size
        # and baseline context; validation gates decide status later.
        if True:
            p=Pattern.create(f'{field}={v} -> Pn+1', {'field':field,'equals':v,'target_outcome':'reached_pn_plus_1','multiple_tests':len(tests)}, {'start':min(r['timestamp_ms'] for r in subset),'end':max(r['timestamp_ms'] for r in subset)}, sorted({r['symbol'] for r in subset}))
            p.mark_candidate(); p.sample_size=len(subset); p.baseline_rate=baseline; p.candidate_rate=rate; p.effect_size=effect; p.confidence_interval=_ci(rate,len(subset)); p.discovery_result={'baseline_n':len(rows),'candidate_n':len(subset),'baseline_rate':baseline,'candidate_rate':rate,'effect_size':effect,'ci95':p.confidence_interval}
            candidates.append(p)
    return candidates
