"""Mutable engine state and chart/backtest snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .config import Side
from .levels import ladder_step


@dataclass(frozen=True)
class FilledLeg:
    step: int
    entry: Decimal
    ts_ms: int
    qty: Decimal


@dataclass(frozen=True)
class CycleCloseRecord:
    cycle_id: int
    ts_ms: int
    exit: Decimal
    highest_step: int
    open_legs: int


@dataclass
class EngineState:
    side: Side
    percentage: Decimal
    phi: Decimal
    max_step: int
    symbol: str = ""
    cycle_id: int = 0
    p0: Optional[Decimal] = None
    highest_filled: int = -1
    shared_tp: Optional[Decimal] = None
    legs: List[FilledLeg] = field(default_factory=list)
    closed: List[CycleCloseRecord] = field(default_factory=list)
    initial_p0: Optional[Decimal] = None
    active: bool = False
    last_event_ts_ms: Optional[int] = None

    def current_p(self) -> Optional[Decimal]:
        if self.p0 is None or self.highest_filled < 0:
            return None
        p, _ = ladder_step(self.side, self.p0, self.highest_filled, phi=self.phi, percentage=self.percentage)
        return p

    def tp_pn_minus_1(self) -> Optional[Decimal]:
        """Shared TP while at step n: TP[n] (== P[n-1] for n>=1, else TP0)."""
        return self.shared_tp

    def next_p(self) -> Optional[Decimal]:
        if self.p0 is None or self.highest_filled < 0:
            return None
        n = self.highest_filled + 1
        if n > self.max_step:
            return None
        p, _ = ladder_step(self.side, self.p0, n, phi=self.phi, percentage=self.percentage)
        return p

    def further_p(self) -> Optional[Decimal]:
        if self.p0 is None or self.highest_filled < 0:
            return None
        n = self.highest_filled + 2
        if n > self.max_step:
            return None
        p, _ = ladder_step(self.side, self.p0, n, phi=self.phi, percentage=self.percentage)
        return p

    def comparable(self) -> Dict[str, Any]:
        """Stable dict for parity asserts (mirrors legacy ReplayState.comparable)."""
        return {
            "side": self.side.value,
            "cycle": self.cycle_id,
            "p0": None if self.p0 is None else str(self.p0),
            "highest_filled": self.highest_filled,
            "shared_tp": None if self.shared_tp is None else str(self.shared_tp),
            "legs": [
                {"step": leg.step, "entry": str(leg.entry), "ts": leg.ts_ms, "qty": str(leg.qty)}
                for leg in self.legs
            ],
            "closed": [
                {
                    "cycle": c.cycle_id,
                    "ts": c.ts_ms,
                    "exit": str(c.exit),
                    "highest_step": c.highest_step,
                    "open_legs": c.open_legs,
                }
                for c in self.closed
            ],
            "initial_p0": None if self.initial_p0 is None else str(self.initial_p0),
            "active": self.active,
        }


@dataclass(frozen=True)
class StateSnapshot:
    """Read-only projection for future chart/backtester (no UI math)."""

    symbol: str
    side: str
    percentage: str
    cycle_id: int
    active: bool
    p0: Optional[str]
    initial_p0: Optional[str]
    n: int
    current_p: Optional[str]
    shared_tp: Optional[str]
    next_p: Optional[str]
    further_p: Optional[str]
    legs: List[Dict[str, Any]]
    closed_count: int
    last_event_ts_ms: Optional[int]

    # Extension points (not populated in Phase 1)
    ladder_vwap: Optional[str] = None
    step_vwap: Optional[str] = None
    ladder_poc: Optional[str] = None
    step_poc: Optional[str] = None

    @classmethod
    def from_state(cls, state: EngineState) -> "StateSnapshot":
        def s(x: Optional[Decimal]) -> Optional[str]:
            return None if x is None else str(x)

        return cls(
            symbol=state.symbol,
            side=state.side.value,
            percentage=str(state.percentage),
            cycle_id=state.cycle_id,
            active=state.active,
            p0=s(state.p0),
            initial_p0=s(state.initial_p0),
            n=state.highest_filled,
            current_p=s(state.current_p()),
            shared_tp=s(state.shared_tp),
            next_p=s(state.next_p()),
            further_p=s(state.further_p()),
            legs=[
                {"step": leg.step, "entry": str(leg.entry), "ts_ms": leg.ts_ms, "qty": str(leg.qty)}
                for leg in state.legs
            ],
            closed_count=len(state.closed),
            last_event_ts_ms=state.last_event_ts_ms,
        )
