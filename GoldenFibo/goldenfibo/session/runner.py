"""Backtest / historical replay runner (OHLC → MarketEvents → one GoldenFiboEngine)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, List, Optional, Sequence

from ..engine.config import EngineConfig, OhlcResolveMode, Side
from ..engine.engine import EngineResult, GoldenFiboEngine
from ..engine.events import DomainEvent, MarketEvent
from ..feeders.historical_ohlc import apply_ohlc_to_engine, collect_ohlc_events, count_ambiguous
from ..marketdata.timeframes import interval_ms, require_aligned, validate_interval
from .event_log import EventLog

ProgressCb = Callable[[dict], None]


@dataclass
class HistRunResult:
    engine: GoldenFiboEngine
    events: List[MarketEvent]
    domain: List[DomainEvent]
    klines: List[list]
    ambiguity_count: int
    bars_processed: int
    start_ms: int
    end_ms: int
    event_log: EventLog = field(default_factory=EventLog)


def run_ohlc_on_engine(
    engine: GoldenFiboEngine,
    candles: Sequence[Sequence],
    *,
    mode: OhlcResolveMode = OhlcResolveMode.LEGACY,
    event_log: Optional[EventLog] = None,
    progress_every: int = 0,
    on_progress: Optional[ProgressCb] = None,
) -> HistRunResult:
    """Apply OHLC-derived MarketEvents to an existing engine (no new engine object)."""
    cfg = engine.config
    events = collect_ohlc_events(candles, cfg, mode=mode)
    domain: List[DomainEvent] = []
    total = len(events)
    for i, ev in enumerate(events):
        result: EngineResult = engine.on_event(ev)
        domain.extend(result.events)
        if event_log is not None:
            event_log.extend(result.events)
        if on_progress and progress_every and (i + 1) % progress_every == 0:
            on_progress(
                {
                    "bars_hint": i + 1,
                    "events_done": i + 1,
                    "events_total": total,
                    "pct": 100.0 * (i + 1) / max(1, total),
                }
            )
    if on_progress and total:
        on_progress(
            {
                "events_done": total,
                "events_total": total,
                "pct": 100.0,
            }
        )
    return HistRunResult(
        engine=engine,
        events=events,
        domain=domain,
        klines=list(candles),
        ambiguity_count=count_ambiguous(events),
        bars_processed=len(candles),
        start_ms=int(candles[0][0]) if candles else 0,
        end_ms=int(candles[-1][0]) + 1 if candles else 0,
        event_log=event_log or EventLog(),
    )


def new_engine_for_run(
    *,
    side: Side,
    percentage: Decimal,
    symbol: str,
    timeframe: str,
) -> GoldenFiboEngine:
    validate_interval(timeframe)
    return GoldenFiboEngine(
        EngineConfig(side=side, percentage=percentage, symbol=symbol.upper())
    )



def apply_ohlc_page(
    engine: GoldenFiboEngine,
    candles: Sequence[Sequence],
    *,
    mode: OhlcResolveMode = OhlcResolveMode.LEGACY,
    event_log: Optional[EventLog] = None,
) -> HistRunResult:
    """Stream one OHLC page into an existing engine (no shadow re-seed)."""
    market, domain, amb = apply_ohlc_to_engine(engine, candles, mode=mode)
    if event_log is not None:
        event_log.extend(domain)
    return HistRunResult(
        engine=engine,
        events=market,
        domain=domain,
        klines=list(candles),
        ambiguity_count=amb,
        bars_processed=len(candles),
        start_ms=int(candles[0][0]) if candles else 0,
        end_ms=int(candles[-1][0]) + 1 if candles else 0,
        event_log=event_log or EventLog(),
    )
