"""Deterministic GoldenFiboEngine — MarketEvent in, state + domain events out."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List

from ..simulation.sizing import quantity_for_step
from .config import EngineConfig
from .events import DomainEvent, DomainEventKind, MarketEvent, MarketEventKind
from .levels import ladder_step
from .state import CycleCloseRecord, EngineState, FilledLeg, StateSnapshot


@dataclass
class EngineResult:
    state: EngineState
    events: List[DomainEvent] = field(default_factory=list)
    snapshot: StateSnapshot = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.snapshot is None:
            self.snapshot = StateSnapshot.from_state(self.state)


class GoldenFiboEngine:
    """ONE strategy engine for backtest, replay, and future live feeders.

    Does not know data source mode. Only processes ordered MarketEvents.
    """

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.state = EngineState(
            side=config.side,
            percentage=config.percentage,
            phi=config.phi,
            max_step=config.max_step,
            symbol=config.symbol,
        )

    def snapshot(self) -> StateSnapshot:
        return StateSnapshot.from_state(self.state)

    def on_event(self, event: MarketEvent) -> EngineResult:
        emitted: List[DomainEvent] = []
        self.state.last_event_ts_ms = event.ts_ms

        if event.kind is MarketEventKind.AMBIGUOUS_BAR:
            emitted.append(
                DomainEvent(
                    kind=DomainEventKind.AMBIGUOUS_BAR,
                    ts_ms=event.ts_ms,
                    payload=dict(event.meta),
                )
            )
            return EngineResult(state=self.state, events=emitted)

        if event.kind is MarketEventKind.SEED_P0:
            if event.price is None:
                raise ValueError("SEED_P0 requires price")
            emitted.extend(self._open_cycle(event.price, event.ts_ms))
            return EngineResult(state=self.state, events=emitted)

        if event.kind is MarketEventKind.PROGRESSION_TOUCH:
            emitted.extend(self._fill_progression(event))
            return EngineResult(state=self.state, events=emitted)

        if event.kind is MarketEventKind.TP_TOUCH:
            emitted.extend(self._hit_tp(event.ts_ms))
            return EngineResult(state=self.state, events=emitted)

        raise ValueError(f"unknown market event kind {event.kind!r}")

    def run(self, events: List[MarketEvent]) -> EngineResult:
        all_events: List[DomainEvent] = []
        for ev in events:
            result = self.on_event(ev)
            all_events.extend(result.events)
        return EngineResult(state=self.state, events=all_events)

    # ----- internals ---------------------------------------------------------

    def _open_cycle(self, p0: Decimal, ts_ms: int) -> List[DomainEvent]:
        cfg = self.config
        p0 = Decimal(p0)
        _, tp = ladder_step(cfg.side, p0, 0, phi=cfg.phi, percentage=cfg.percentage)
        qty = quantity_for_step(cfg, 0)

        self.state.cycle_id += 1
        self.state.p0 = p0
        self.state.highest_filled = 0
        self.state.shared_tp = tp
        self.state.legs = [FilledLeg(step=0, entry=p0, ts_ms=ts_ms, qty=qty)]
        self.state.active = True
        if self.state.initial_p0 is None:
            self.state.initial_p0 = p0

        return [
            DomainEvent(
                kind=DomainEventKind.CYCLE_STARTED,
                ts_ms=ts_ms,
                payload={
                    "cycle_id": self.state.cycle_id,
                    "p0": str(p0),
                    "shared_tp": str(tp),
                    "qty": str(qty),
                },
            ),
            DomainEvent(
                kind=DomainEventKind.STEP_FILLED,
                ts_ms=ts_ms,
                payload={"step": 0, "entry": str(p0), "qty": str(qty), "cycle_id": self.state.cycle_id},
            ),
            DomainEvent(
                kind=DomainEventKind.P0_RESET,
                ts_ms=ts_ms,
                payload={"cycle_id": self.state.cycle_id, "p0": str(p0)},
            ),
        ]

    def _fill_progression(self, event: MarketEvent) -> List[DomainEvent]:
        st = self.state
        cfg = self.config
        if not st.active or st.p0 is None or st.highest_filled < 0:
            return []

        target = event.step if event.step is not None else st.highest_filled + 1
        if target <= st.highest_filled:
            return []
        if target > cfg.max_step:
            return []
        # Only allow contiguous next step (path engine fills one at a time).
        if target != st.highest_filled + 1:
            return []

        p, tp = ladder_step(cfg.side, st.p0, target, phi=cfg.phi, percentage=cfg.percentage)
        qty = quantity_for_step(cfg, target)
        st.highest_filled = target
        st.shared_tp = tp
        st.legs.append(FilledLeg(step=target, entry=p, ts_ms=event.ts_ms, qty=qty))

        return [
            DomainEvent(
                kind=DomainEventKind.STEP_FILLED,
                ts_ms=event.ts_ms,
                payload={
                    "step": target,
                    "entry": str(p),
                    "shared_tp": str(tp),
                    "qty": str(qty),
                    "cycle_id": st.cycle_id,
                },
            ),
            DomainEvent(
                kind=DomainEventKind.PROGRESSION,
                ts_ms=event.ts_ms,
                payload={
                    "highest_filled": target,
                    "p_n": str(p),
                    "shared_tp": str(tp),
                    "cycle_id": st.cycle_id,
                },
            ),
        ]

    def _hit_tp(self, ts_ms: int) -> List[DomainEvent]:
        st = self.state
        if not st.active or st.shared_tp is None or st.p0 is None:
            return []

        exit_price = st.shared_tp
        closed = CycleCloseRecord(
            cycle_id=st.cycle_id,
            ts_ms=ts_ms,
            exit=exit_price,
            highest_step=st.highest_filled,
            open_legs=len(st.legs),
        )
        st.closed.append(closed)

        events: List[DomainEvent] = [
            DomainEvent(
                kind=DomainEventKind.TP_HIT,
                ts_ms=ts_ms,
                payload={
                    "exit": str(exit_price),
                    "cycle_id": closed.cycle_id,
                    "highest_step": closed.highest_step,
                },
            ),
            DomainEvent(
                kind=DomainEventKind.CYCLE_CLOSED,
                ts_ms=ts_ms,
                payload={
                    "cycle_id": closed.cycle_id,
                    "exit": str(exit_price),
                    "highest_step": closed.highest_step,
                    "open_legs": closed.open_legs,
                },
            ),
        ]

        # Critical rule: next P0 = exact TP, not next bar open.
        st.active = False
        st.p0 = None
        st.shared_tp = None
        st.highest_filled = -1
        st.legs = []
        events.extend(self._open_cycle(exit_price, ts_ms))
        return events
