from __future__ import annotations
from decimal import Decimal
from typing import Iterable, List
from fibolearn.features.multiscale import build_multiscale_vector

def replay_multiscale(symbol: str, candles: List[list], *, percentages, directions):
    out=[]
    sofar=[]
    for k in candles:
        sofar.append(k)
        price=Decimal(str(k[4]))
        out.append(build_multiscale_vector(symbol, int(k[0]), price, percentages=percentages, directions=directions, raw={'source_candles': len(sofar), 'last_candle': k}))
    return out
