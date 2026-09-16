from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import Dict, Iterable, List, Mapping
getcontext().prec = 28

def D(x) -> Decimal: return Decimal(str(x))

@dataclass(frozen=True)
class VwapPoint:
    timestamp_ms: int
    vwap: Decimal
    slope: Decimal | None = None
    migration: Decimal | None = None

@dataclass(frozen=True)
class VolumeProfile:
    poc: Decimal
    val: Decimal
    vah: Decimal
    low: Decimal
    high: Decimal
    bins: int

def vwap_series(candles: List[list]) -> List[VwapPoint]:
    out=[]; base=Decimal(0); quote=Decimal(0); prev=None
    for k in candles:
        base += D(k[5]); quote += D(k[7]) if len(k)>7 else D(k[4])*D(k[5])
        vw = quote/base if base else D(k[4])
        slope = (vw-prev) if prev is not None else None
        out.append(VwapPoint(int(k[0]), vw, slope, slope))
        prev=vw
    return out

def volume_profile(candles: List[list], bins: int=160) -> VolumeProfile:
    lows=[D(k[3]) for k in candles]; highs=[D(k[2]) for k in candles]
    lo=min(lows); hi=max(highs)
    if hi <= lo: return VolumeProfile(D(candles[-1][4]), lo, hi, lo, hi, bins)
    bins=max(1,int(bins)); width=(hi-lo)/D(bins); vols=[D(0) for _ in range(bins)]
    for k in candles:
        h,l,v=D(k[2]),D(k[3]),D(k[5])
        a=max(0,min(bins-1,int((l-lo)/width))); b=max(0,min(bins-1,int((h-lo)/width)))
        share=v/D(max(1,b-a+1))
        for i in range(a,b+1): vols[i]+=share
    poc_i=max(range(bins), key=lambda i: vols[i]); poc=lo+(D(poc_i)+D('0.5'))*width
    total=sum(vols); target=total*D('0.70'); cum=vols[poc_i]; left=right=poc_i
    while cum < target and (left>0 or right<bins-1):
        lv=vols[left-1] if left>0 else D(-1); rv=vols[right+1] if right<bins-1 else D(-1)
        if rv >= lv and right<bins-1: right+=1; cum+=vols[right]
        elif left>0: left-=1; cum+=vols[left]
        else: break
    return VolumeProfile(poc, lo+D(left)*width, lo+D(right+1)*width, lo, hi, bins)

def significant_level_distances(targets: Mapping[str, Decimal], levels: Mapping[str, Decimal]) -> Dict[str, Dict[str, Decimal]]:
    return {name:{lname: D(price)-D(level) for lname,level in levels.items()} for name,price in targets.items()}
