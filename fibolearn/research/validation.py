from __future__ import annotations
from typing import Any, Dict, Iterable, List

def leave_one_symbol_out(rows: List[Dict[str,Any]]) -> Dict[str,Dict[str,Any]]:
    symbols=sorted({r.get('symbol') for r in rows})
    out={}
    for s in symbols:
        train=[r for r in rows if r.get('symbol')!=s]; test=[r for r in rows if r.get('symbol')==s]
        out[s]={'train_samples':len(train),'test_samples':len(test),'test_hit_rate':(sum(1 for r in test if r.get('hit'))/len(test)) if test else None}
    return out
