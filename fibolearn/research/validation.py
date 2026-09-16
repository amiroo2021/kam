from __future__ import annotations
from decimal import Decimal
from typing import Any, Dict, List

def leave_one_symbol_out(rows: List[Dict[str,Any]]) -> Dict[str,Dict[str,Any]]:
    symbols=sorted({r.get('symbol') for r in rows})
    out={}
    for s in symbols:
        train=[r for r in rows if r.get('symbol')!=s]; test=[r for r in rows if r.get('symbol')==s]
        out[s]={'train_samples':len(train),'test_samples':len(test),'test_hit_rate':(sum(1 for r in test if r.get('hit'))/len(test)) if test else None}
    return out

def temporal_oos_split(rows: List[Dict[str,Any]], *, test_fraction: Decimal = Decimal('0.3')):
    rows=sorted(rows, key=lambda r: int(r.get('timestamp_ms') or 0))
    cut=max(1, int(len(rows)*(Decimal(1)-test_fraction)))
    return rows[:cut], rows[cut:]

def walk_forward_validate(rows: List[Dict[str,Any]], *, folds:int=4)->Dict[str,Any]:
    rows=sorted(rows, key=lambda r:int(r.get('timestamp_ms') or 0))
    if len(rows)<2: return {'folds':0,'results':[]}
    size=max(1,len(rows)//folds); results=[]
    for i in range(1, folds):
        train=rows[:i*size]; test=rows[i*size:(i+1)*size]
        if not test: continue
        results.append({'train_samples':len(train),'test_samples':len(test),'test_hit_rate':sum(1 for r in test if r.get('hit'))/len(test)})
    return {'folds':len(results),'results':results}
