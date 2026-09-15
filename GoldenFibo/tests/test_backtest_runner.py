"""Backtest runner unit tests with synthetic candles."""

from __future__ import annotations

from decimal import Decimal

from goldenfibo import EngineConfig, GoldenFiboEngine, Side
from goldenfibo.engine.config import OhlcResolveMode
from goldenfibo.session.event_log import EventLog
from goldenfibo.session.runner import run_ohlc_on_engine


def test_backtest_preserves_event_log_for_future_stats():
    cfg = EngineConfig(side=Side.SELL, percentage=Decimal("0.001"), symbol="BTCUSDT")
    eng = GoldenFiboEngine(cfg)
    log = EventLog()
    t0 = 1_700_000_060_000
    candles = [
        [t0, "100", "100.01", "99.90", "100", "1", t0 + 59999, "100"],
        [t0 + 60000, "99.9", "100.2", "99.8", "100", "1", t0 + 119999, "100"],
    ]
    r = run_ohlc_on_engine(eng, candles, mode=OhlcResolveMode.LEGACY, event_log=log)
    summary = log.summary_counts()
    assert summary["domain_events"] > 0
    assert summary["cycles_started"] >= 1
    assert r.bars_processed == 2
    assert "ambiguity_count" in summary
