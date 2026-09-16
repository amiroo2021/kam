from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict
from fibolearn.research.discovery import create_candidate_from_live_state
from fibolearn.research.validation import temporal_oos_split, walk_forward_validate, leave_one_symbol_out

@dataclass
class StudyReport:
    match_count: int
    outcome_rate: float | None
    by_symbol: Dict[str, Any]
    by_percentage: Dict[str, Any]
    oos: Dict[str, Any]
    walk_forward: Dict[str, Any]
    pattern: Any

def _rate(rows): return (sum(1 for r in rows if r.get('hit'))/len(rows)) if rows else None

def study_setup(store, observation: Dict[str,Any] | None, *, min_matches:int=30) -> StudyReport:
    if observation is None: raise ValueError('no observation to study')
    state=observation['state_vector']; rows=store.dataset_rows()
    # Conservative comparable-state v1: same symbol plus rows with completed/known outcome when available.
    matches=[r for r in rows if r.get('symbol')==state.get('symbol')] or rows
    pattern=create_candidate_from_live_state(state); pattern.sample_size=len(matches)
    if len(matches) >= min_matches: pattern.mark_backtested(len(matches), {'outcome_rate': _rate(matches)})
    by_symbol={s:{'n':len([r for r in matches if r['symbol']==s]), 'rate':_rate([r for r in matches if r['symbol']==s])} for s in sorted({r['symbol'] for r in matches})}
    by_pct={p:{'n':len([r for r in matches if r['percentage']==p]), 'rate':_rate([r for r in matches if r['percentage']==p])} for p in sorted({r['percentage'] for r in matches})}
    train,test=temporal_oos_split(matches) if len(matches)>1 else (matches,[])
    return StudyReport(len(matches), _rate(matches), by_symbol, by_pct, {'train_n':len(train),'test_n':len(test),'test_rate':_rate(test)}, walk_forward_validate(matches), pattern)
