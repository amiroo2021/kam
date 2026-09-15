"""Feeder protocol."""

from __future__ import annotations

from typing import Iterable, Protocol

from ..engine.events import MarketEvent


class Feeder(Protocol):
    def iter_events(self) -> Iterable[MarketEvent]:
        ...
