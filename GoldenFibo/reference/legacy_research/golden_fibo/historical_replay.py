"""Deterministic historical OHLC replay for GoldenFibo paper ladders."""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .constants import Side
from .ladder import PERCENTAGE, ladder_step


@dataclass
class ReplayLeg:
    step: int
    entry: float
    ts: int
    utc: str


@dataclass
class CycleCloseRecord:
    cycle: int
    ts: int
    utc: str
    exit: float
    highest_step: int
    open_legs: int


@dataclass
class ReplayState:
    side: Side
    cycle: int = 0
    p0: float | None = None
    highest_filled: int = -1
    shared_tp: float | None = None
    legs: list[ReplayLeg] = field(default_factory=list)
    closed: list[CycleCloseRecord] = field(default_factory=list)
    initial_p0: float | None = None

    def comparable(self) -> dict[str, Any]:
        return {
            "side": self.side.value,
            "cycle": self.cycle,
            "p0": self.p0,
            "highest_filled": self.highest_filled,
            "shared_tp": self.shared_tp,
            "legs": [leg.__dict__ for leg in self.legs],
            "closed": [c.__dict__ for c in self.closed],
            "initial_p0": self.initial_p0,
        }


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def calendar_months_before(dt: datetime, months: int = 3) -> datetime:
    """Calendar month subtraction, clamping day to target month length."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    y = dt.year
    m = dt.month - months
    while m <= 0:
        y -= 1
        m += 12
    day = min(dt.day, calendar.monthrange(y, m)[1])
    return datetime(y, m, day, tzinfo=timezone.utc)


def historical_anchor_utc(replay_dt: datetime, months_back: int = 3) -> datetime:
    """Start at 00:01:00 UTC on the calendar date exactly months_back before replay_dt."""
    base = calendar_months_before(replay_dt.astimezone(timezone.utc), months_back)
    return datetime(base.year, base.month, base.day, 0, 1, 0, tzinfo=timezone.utc)


def replay_ohlc(
    candles: Sequence[Sequence[Any]],
    *,
    side: Side | str = Side.SELL,
    max_step: int = 20,
    percentage: float = PERCENTAGE,
) -> ReplayState:
    """
    Deterministic 1m OHLC replay.

    First cycle P0 is the initial candle open. After that, every cycle chains
    exactly from the previous cycle's closing shared TP:
        next P0 = previous shared TP

    When TP touches inside a candle, the new cycle is created immediately at
    the exact TP but is not evaluated further until the next candle because
    the remaining intraminute path is unknown.
    """
    side = Side(side)
    state = ReplayState(side=side)

    def open_cycle(p0: float, ts: int) -> None:
        state.cycle += 1
        state.p0 = p0
        _, tp = ladder_step(side, p0, 0, percentage=percentage)
        state.highest_filled = 0
        state.shared_tp = tp
        state.legs = [ReplayLeg(0, p0, ts, iso(ts))]
        if state.initial_p0 is None:
            state.initial_p0 = p0

    def fill_step(n: int, ts: int) -> None:
        if state.p0 is None:
            raise RuntimeError("cannot fill without p0")
        p, tp = ladder_step(side, state.p0, n, percentage=percentage)
        state.highest_filled = n
        state.shared_tp = tp
        state.legs.append(ReplayLeg(n, p, ts, iso(ts)))

    def close_and_chain(ts: int) -> None:
        if state.shared_tp is None:
            raise RuntimeError("cannot close without shared TP")
        exit_price = state.shared_tp
        state.closed.append(
            CycleCloseRecord(
                cycle=state.cycle,
                ts=ts,
                utc=iso(ts),
                exit=exit_price,
                highest_step=state.highest_filled,
                open_legs=len(state.legs),
            )
        )
        # Critical rule: deterministic cycle chain from exact TP, not next candle open.
        open_cycle(exit_price, ts)

    for candle in candles:
        ts = int(candle[0])
        o = float(candle[1])
        h = float(candle[2])
        l = float(candle[3])
        if state.p0 is None:
            open_cycle(o, ts)

        # Price-touch entries before TP for conservative adverse-first replay.
        if side is Side.SELL:
            while state.highest_filled < max_step:
                next_p, _ = ladder_step(side, state.p0, state.highest_filled + 1, percentage=percentage)  # type: ignore[arg-type]
                if h >= next_p:
                    fill_step(state.highest_filled + 1, ts)
                else:
                    break
            if state.shared_tp is not None and l <= state.shared_tp:
                close_and_chain(ts)
                continue  # Do not evaluate new cycle inside this same candle.
        else:
            while state.highest_filled < max_step:
                next_p, _ = ladder_step(side, state.p0, state.highest_filled + 1, percentage=percentage)  # type: ignore[arg-type]
                if l <= next_p:
                    fill_step(state.highest_filled + 1, ts)
                else:
                    break
            if state.shared_tp is not None and h >= state.shared_tp:
                close_and_chain(ts)
                continue  # Do not evaluate new cycle inside this same candle.

    return state


def levels_p0_to_pn(state: ReplayState, n: int = 11, *, percentage: float = PERCENTAGE) -> list[dict[str, float | str]]:
    if state.p0 is None:
        return []
    out = []
    for i in range(n + 1):
        p, _ = ladder_step(state.side, state.p0, i, percentage=percentage)
        role = ""
        if i == 0:
            role = "P0"
        if i == state.highest_filled - 1:
            role = "ladder TP / P(n-1)"
        if i == state.highest_filled:
            role = "current step P(n)"
        if i == state.highest_filled + 1:
            role = "next P(n+1)"
        if i == state.highest_filled + 2:
            role = "further progression P(n+2)"
        out.append({"level": f"P{i}", "price": p, "role": role})
    return out
