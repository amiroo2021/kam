"""Append-only domain event log for later statistics (cycles, TP exits, etc.)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from ..engine.events import DomainEvent, DomainEventKind


@dataclass
class EventLog:
    """Preserves domain history for future P&L / expectancy stats (no dashboard yet)."""

    events: List[DomainEvent] = field(default_factory=list)
    max_keep: int = 200_000

    def extend(self, items: List[DomainEvent]) -> None:
        if not items:
            return
        self.events.extend(items)
        if len(self.events) > self.max_keep:
            self.events = self.events[-self.max_keep :]

    def clear(self) -> None:
        self.events.clear()

    @property
    def ambiguity_count(self) -> int:
        return sum(1 for e in self.events if e.kind is DomainEventKind.AMBIGUOUS_BAR)

    def summary_counts(self) -> dict:
        cycles_started = sum(1 for e in self.events if e.kind is DomainEventKind.CYCLE_STARTED)
        tp_hits = sum(1 for e in self.events if e.kind is DomainEventKind.TP_HIT)
        progressions = sum(1 for e in self.events if e.kind is DomainEventKind.PROGRESSION)
        step_fills = sum(1 for e in self.events if e.kind is DomainEventKind.STEP_FILLED)
        return {
            "domain_events": len(self.events),
            "cycles_started": cycles_started,
            "tp_hits": tp_hits,
            "progressions": progressions,
            "step_fills": step_fills,
            "ambiguity_count": self.ambiguity_count,
        }
