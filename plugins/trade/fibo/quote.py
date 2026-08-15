"""Quote surface used by the Fibo engine.

The engine itself only ever calls ``quote_source.current_bid_ask(symbol)``
and treats the returned ``Quote`` as a single instantaneous snapshot.

The ``QuoteSource`` Protocol is the swap point: a v1 polling implementation
satisfies it by hitting a REST endpoint on a fixed cadence, and a future v2
WebSocket implementation can satisfy the same Protocol without any change
to ``engine.py``.

Nothing in this module imports from ``engine`` — the boundary is one-way:
engine consumes Quote/QuoteSource; quote has no dependency on engine state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Quote:
    """A single instantaneous market snapshot for one instrument.

    Attributes:
        bid: Best bid. Must be > 0 and >= ask when ``bid > 0`` and ``ask > 0``.
        ask: Best ask. Must be > 0 and >= bid.
    """

    bid: float
    ask: float

    def __post_init__(self) -> None:
        if self.bid <= 0 or self.ask <= 0:
            raise ValueError(
                f"invalid quote: bid={self.bid} ask={self.ask} (both must be > 0)"
            )
        if self.ask < self.bid:
            raise ValueError(
                f"invalid quote: bid={self.bid} ask={self.ask} (ask < bid)"
            )

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


class QuoteSource(Protocol):
    """Minimal interface required by ``FiboEngine.on_quote``.

    v1 may implement this with a REST polling loop; v2 may implement it with
    a WebSocket subscription. ``engine.py`` never reaches into the source.
    """

    def current_bid_ask(self, symbol: str) -> Quote:
        """Return the most recent ``Quote`` for ``symbol``.

        Implementations MUST raise ``LookupError`` (or a subclass) when no
        quote is currently available for the symbol — the engine treats that
        as "skip this tick, do not advance the cascade." Any other exception
        is also treated as a non-fatal skip (logged at the source layer).
        """
        ...
