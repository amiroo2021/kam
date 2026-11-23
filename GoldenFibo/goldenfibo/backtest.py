"""Canonical finite backtest helper for GoldenFibo.

This module is the shared production-facing finite historical replay path used
by Telegram /backtest and can be used by tests without importing web/session
lifecycle code.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import os
import threading
from typing import Any, Dict, List, Sequence

from .api import schemas
from .engine.config import EngineConfig, OhlcResolveMode, Side
from .engine.engine import GoldenFiboEngine
from .feeders.historical_ohlc import apply_ohlc_to_engine, collect_ohlc_events
from .marketdata.binance_public import bars_to_chart_candles
from .metrics import OhlcvBar, metrics_for_legs
from .metrics.trade_store import MetricDisplay, STATUS_COMPLETE, SOURCE_OHLC
from .session.event_log import EventLog
from .session.runner import run_ohlc_on_engine


_BACKTEST_SLOT = threading.BoundedSemaphore(max(1, int(os.environ.get("GOLDENFIBO_BACKTEST_MAX_CONCURRENT", "1"))))


@dataclass
class FiniteBacktestResult:
    engine: GoldenFiboEngine
    klines: List[list]
    bars: List[OhlcvBar]
    chart_candles: List[Dict[str, Any]]
    recent_domain: List[Any]
    event_log: EventLog
    ambiguity_count: int
    bars_processed: int
    state_payload: Dict[str, Any]
    metric_display: MetricDisplay


def _bars_from_klines(klines: Sequence[Sequence[Any]]) -> List[OhlcvBar]:
    return [
        OhlcvBar(
            int(k[0]),
            float(k[1]),
            float(k[2]),
            float(k[3]),
            float(k[4]),
            float(k[5]),
            float(k[7]),
        )
        for k in klines
    ]


def run_finite_backtest(
    klines: Sequence[Sequence[Any]],
    *,
    side: Side | str,
    percentage: Decimal | float | str,
    symbol: str,
    timeframe: str = "1m",
    mode: OhlcResolveMode = OhlcResolveMode.LEGACY,
    max_candles: int | None = None,
    max_events: int | None = None,
) -> FiniteBacktestResult:
    """Run the canonical historical OHLC→engine path and return a finite result."""
    candle_limit = int(os.environ.get("GOLDENFIBO_BACKTEST_MAX_CANDLES", "1000000")) if max_candles is None else int(max_candles)
    event_limit = int(os.environ.get("GOLDENFIBO_BACKTEST_MAX_EVENTS", "1500000")) if max_events is None else int(max_events)
    if len(klines) > candle_limit:
        raise RuntimeError(f"historical backtest rejected: {len(klines)} candles exceeds limit {candle_limit}")
    if not _BACKTEST_SLOT.acquire(blocking=False):
        raise RuntimeError("historical backtest rejected: concurrency limit reached")
    try:
        cfg = EngineConfig(side=Side(side), percentage=Decimal(str(percentage)), symbol=symbol.upper())
        engine = GoldenFiboEngine(cfg)
        event_log = EventLog()
        hist = run_ohlc_on_engine(engine, klines, mode=mode, event_log=event_log, max_events=event_limit)
        bars = _bars_from_klines(hist.klines)
        chart_candles = bars_to_chart_candles(hist.klines)
        ladder_vwap, step_vwap, ladder_poc, step_poc, ladder_val, ladder_vah = metrics_for_legs(
            bars,
            ladder_start_ts_ms=engine.state.legs[0].ts_ms if engine.state.legs else None,
            step_start_ts_ms=engine.state.legs[-1].ts_ms if engine.state.legs else None,
        )
        metric_display = MetricDisplay(
            source=SOURCE_OHLC,
            ladder_vwap=ladder_vwap,
            step_vwap=step_vwap,
            ladder_poc=ladder_poc,
            step_poc=step_poc,
            ladder_val=ladder_val,
            ladder_vah=ladder_vah,
            ladder_status=STATUS_COMPLETE if engine.state.legs else "loading",
            step_status=STATUS_COMPLETE if engine.state.legs else "loading",
            handoff_status="finite_backtest",
            detail="canonical finite backtest",
        )
        state_payload = schemas.build_state_payload(
            mode="BACKTEST", symbol=symbol.upper(), timeframe=timeframe, side=cfg.side.value,
            percentage=str(cfg.percentage), price=str(hist.klines[-1][4]) if hist.klines else None,
            engine=engine, candles=chart_candles, bars=bars,
            recent_domain=list(hist.domain[-80:]), connected=False,
            metric_display=metric_display, prefer_aggtrade=False,
        )
        for key, source in (("step_vwap", "active_step_vwap"), ("step_poc", "active_step_poc"), ("ladder_vwap", "ladder_vwap"), ("ladder_poc", "ladder_poc"), ("ladder_val", "ladder_val"), ("ladder_vah", "ladder_vah")):
            state_payload[key] = state_payload.get(source)
        return FiniteBacktestResult(
            engine=engine, klines=list(hist.klines), bars=bars, chart_candles=chart_candles,
            recent_domain=list(hist.domain[-80:]), event_log=event_log,
            ambiguity_count=hist.ambiguity_count, bars_processed=hist.bars_processed,
            state_payload=state_payload, metric_display=metric_display,
        )
    finally:
        _BACKTEST_SLOT.release()
