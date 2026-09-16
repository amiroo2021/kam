from __future__ import annotations
from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Any, Dict, Optional

PHI = Decimal("1.618033988749894848204586834")

@dataclass(frozen=True)
class LadderStateInput:
    symbol: str
    timestamp_ms: int
    direction: str
    percentage: Decimal
    cycle_id: int
    p0: Decimal
    active_step: int
    current_price: Decimal
    active_since_ms: int
    pn_retests: int = 0
    highest_excursion: Decimal | None = None
    lowest_excursion: Decimal | None = None
    last_progression_ts_ms: Optional[int] = None
    last_step_change_ts_ms: Optional[int] = None

@dataclass(frozen=True)
class LadderObservation:
    symbol: str
    timestamp_ms: int
    direction: str
    percentage: Decimal
    cycle_id: int
    active_step: int
    pn_minus_1: Decimal | None
    pn: Decimal
    pn_plus_1: Decimal
    pn_plus_2: Decimal
    current_price: Decimal
    distance_to_pn_minus_1: Decimal | None
    distance_to_pn: Decimal
    distance_to_pn_plus_1: Decimal
    distance_to_pn_plus_2: Decimal
    time_since_pn_active_ms: int
    pn_retested: bool
    pn_retest_count: int
    highest_excursion_since_pn: Decimal | None
    lowest_excursion_since_pn: Decimal | None
    progression_state: str
    progression_depth: int
    highest_step_reached: int
    progressing: bool
    retreating_toward_tp: bool
    time_since_last_progression_ms: int | None
    time_since_active_step_changed_ms: int | None
    distance_to_tp: Decimal | None
    cycle_age_ms: int

    def to_dict(self) -> Dict[str, Any]:
        d=asdict(self)
        for k,v in list(d.items()):
            if isinstance(v, Decimal): d[k]=str(v)
        return d

def ladder_price(direction: str, p0: Decimal, step: int, percentage: Decimal) -> Decimal:
    # GoldenFibo geometry: each step is phi^step * percentage away from P0.
    if step < 0:
        step = 0
    pct = Decimal(str(percentage)) / Decimal("100") if Decimal(str(percentage)) >= 1 else Decimal(str(percentage))
    offset = p0 * pct * (PHI ** step)
    return p0 + offset if direction.upper() == "BUY" else p0 - offset

def extract_ladder_observation(inp: LadderStateInput) -> LadderObservation:
    direction=inp.direction.upper()
    n=int(inp.active_step)
    pn=ladder_price(direction, inp.p0, n, inp.percentage)
    pnm1=ladder_price(direction, inp.p0, n-1, inp.percentage) if n>=1 else None
    pn1=ladder_price(direction, inp.p0, n+1, inp.percentage)
    pn2=ladder_price(direction, inp.p0, n+2, inp.percentage)
    price=inp.current_price
    dist_tp=(price-pnm1) if pnm1 is not None else None
    progressing = (price >= pn if direction == "BUY" else price <= pn)
    retreating = (pnm1 is not None and (price <= pn if direction == "BUY" else price >= pn))
    return LadderObservation(
        symbol=inp.symbol, timestamp_ms=int(inp.timestamp_ms), direction=direction, percentage=inp.percentage,
        cycle_id=int(inp.cycle_id), active_step=n, pn_minus_1=pnm1, pn=pn, pn_plus_1=pn1, pn_plus_2=pn2, current_price=price,
        distance_to_pn_minus_1=(price-pnm1) if pnm1 is not None else None, distance_to_pn=price-pn, distance_to_pn_plus_1=price-pn1, distance_to_pn_plus_2=price-pn2,
        time_since_pn_active_ms=max(0, int(inp.timestamp_ms)-int(inp.active_since_ms)), pn_retested=inp.pn_retests>0, pn_retest_count=inp.pn_retests,
        highest_excursion_since_pn=inp.highest_excursion, lowest_excursion_since_pn=inp.lowest_excursion,
        progression_state="progressing" if progressing else ("retreating_to_tp" if retreating else "between_levels"), progression_depth=n, highest_step_reached=n,
        progressing=progressing, retreating_toward_tp=retreating,
        time_since_last_progression_ms=(int(inp.timestamp_ms)-inp.last_progression_ts_ms) if inp.last_progression_ts_ms is not None else None,
        time_since_active_step_changed_ms=(int(inp.timestamp_ms)-inp.last_step_change_ts_ms) if inp.last_step_change_ts_ms is not None else None,
        distance_to_tp=dist_tp, cycle_age_ms=max(0, int(inp.timestamp_ms)-int(inp.active_since_ms)),
    )
