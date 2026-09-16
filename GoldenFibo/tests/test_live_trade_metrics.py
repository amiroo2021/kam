"""Controller integration: REST→WS handoff metrics with mocked Binance fetch."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from goldenfibo.engine.events import MarketEvent, MarketEventKind
from goldenfibo.metrics.trade_vap import AggTrade
from goldenfibo.session.controller import SessionController
from goldenfibo.session.types import SessionMode, SessionPhase


def _fake_fetch(symbol, start_ms, end_ms, **kwargs):
    return [
        AggTrade(1, 100.0, 1.0, start_ms),
        AggTrade(2, 100.0, 2.0, start_ms + 10),
        AggTrade(3, 101.0, 1.0, start_ms + 20),
    ]


def test_live_backfill_sets_aggtrade_source_and_complete_metrics():
    ctrl = SessionController()
    ctrl.mode = SessionMode.LIVE
    ctrl._agg_trades_fetch = _fake_fetch
    ctrl.phase = SessionPhase.LOADING
    ctrl.engine.on_event(
        MarketEvent(MarketEventKind.SEED_P0, ts_ms=1_000_000, price=Decimal("100"))
    )
    ctrl._p0_seeded = True
    ctrl._aggtrade_metrics_enabled = True

    async def go():
        ctrl._sync_trade_windows_from_engine(schedule_backfill=True)
        assert ctrl._trade_backfill_task is not None
        await ctrl._trade_backfill_task
        snap = ctrl.snapshot_dict()
        assert snap["metric_source"] == "AGGTRADE"
        assert snap["ladder_metric_status"] == "COMPLETE"
        assert snap["ladder_poc"] is not None
        assert snap["ladder_vwap"] is not None
        # partial must not appear when incomplete — here complete
        assert float(snap["ladder_vwap"]) > 0

    asyncio.run(go())


def test_incomplete_backfill_suppresses_chart_values():
    def bad_fetch(symbol, start_ms, end_ms, **kwargs):
        # first trade long after P0
        return [AggTrade(1, 100.0, 1.0, start_ms + 60_000)]

    ctrl = SessionController()
    ctrl.mode = SessionMode.LIVE
    ctrl._agg_trades_fetch = bad_fetch
    ctrl.engine.on_event(
        MarketEvent(MarketEventKind.SEED_P0, ts_ms=1_000_000, price=Decimal("100"))
    )
    ctrl._p0_seeded = True
    ctrl._aggtrade_metrics_enabled = True

    async def go():
        ctrl._sync_trade_windows_from_engine(schedule_backfill=True)
        await ctrl._trade_backfill_task
        snap = ctrl.snapshot_dict()
        assert snap["metric_source"] == "AGGTRADE"
        assert snap["ladder_metric_status"] == "INCOMPLETE_TRADE_HISTORY"
        assert snap["ladder_poc"] is None
        assert snap["ladder_vwap"] is None

    asyncio.run(go())


def test_ws_dedupe_after_rest():
    ctrl = SessionController()
    ctrl.mode = SessionMode.LIVE
    ctrl._agg_trades_fetch = _fake_fetch
    ctrl.engine.on_event(
        MarketEvent(MarketEventKind.SEED_P0, ts_ms=1_000_000, price=Decimal("100"))
    )
    ctrl._p0_seeded = True
    ctrl._aggtrade_metrics_enabled = True

    async def go():
        # WS trade arrives before REST finishes
        ctrl.trade_store.set_windows(ladder_start_ms=1_000_000, step_start_ms=1_000_000)
        ctrl.trade_store.ingest_ws_message(
            {"a": 2, "p": "100.0", "q": "2.0", "T": 1_000_010}
        )
        await ctrl._run_trade_backfill()
        assert len(ctrl.trade_store) == 3
        m = ctrl.trade_store.compute(now_ms=1_000_100)
        assert m.ladder_status == "COMPLETE"

    asyncio.run(go())


def test_snapshot_api_identifies_aggtrade_source_fields():
    """Chart/API contract: metric_source + status fields present for LIVE path."""
    ctrl = SessionController()
    ctrl.mode = SessionMode.LIVE
    ctrl._agg_trades_fetch = _fake_fetch
    ctrl.engine.on_event(
        MarketEvent(MarketEventKind.SEED_P0, ts_ms=1_000_000, price=Decimal("100"))
    )
    ctrl._p0_seeded = True
    ctrl._aggtrade_metrics_enabled = True
    ctrl.phase = SessionPhase.LIVE

    async def go():
        ctrl._sync_trade_windows_from_engine(schedule_backfill=True)
        await ctrl._trade_backfill_task
        snap = ctrl.snapshot_dict()
        for key in (
            "metric_source",
            "ladder_metric_status",
            "step_metric_status",
            "metrics_handoff_status",
            "ladder_trade_count",
        ):
            assert key in snap

    asyncio.run(go())
