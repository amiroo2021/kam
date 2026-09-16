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


def test_progression_keeps_ladder_store_and_skips_rest_backfill():
    """P0→P1→P2: ladder window+history stable; step filter moves; no REST on step change."""
    from goldenfibo import EngineConfig, GoldenFiboEngine, Side
    from goldenfibo.engine.levels import ladder_step
    from goldenfibo.live.price_path import apply_price_to_engine
    from goldenfibo.metrics.trade_vap import AggTrade
    from goldenfibo.session.types import SessionPhase

    rest_calls: list = []

    def fake_fetch(symbol, start_ms, end_ms, **kwargs):
        rest_calls.append((start_ms, end_ms))
        out = []
        aid = 1
        for ts in range(1_000_000, 1_600_000, 10_000):
            out.append(AggTrade(aid, 100.0 - (ts - 1_000_000) / 1_000_000, 1.0, ts))
            aid += 1
        return [t for t in out if start_ms <= t.ts_ms <= end_ms]

    ctrl = SessionController()
    ctrl.mode = SessionMode.LIVE
    ctrl.engine = GoldenFiboEngine(EngineConfig(side=Side.BUY, percentage=Decimal("0.001"), symbol="BTCUSDT"))
    ctrl._agg_trades_fetch = fake_fetch
    ctrl._aggtrade_metrics_enabled = True
    ctrl.phase = SessionPhase.LIVE
    ctrl._live_enabled = True
    ctrl._p0_seeded = True

    t0 = 1_000_000
    p0 = Decimal("100")
    ctrl.engine.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=t0, price=p0))
    p1, _ = ladder_step(Side.BUY, p0, 1, percentage=Decimal("0.001"))
    p2, _ = ladder_step(Side.BUY, p0, 2, percentage=Decimal("0.001"))

    async def go():
        await ctrl._run_trade_backfill()
        assert len(rest_calls) == 1
        assert ctrl.trade_store.ladder_start_ms == t0
        assert ctrl.trade_store.step_start_ms == t0
        before_ids = {t.agg_id for t in ctrl.trade_store.trades}
        before_len = len(ctrl.trade_store)
        md0 = ctrl.trade_store.compute(now_ms=1_590_000)
        assert md0.ladder_status == "COMPLETE" and md0.ladder_trade_count == md0.step_trade_count

        t1 = 1_200_000
        apply_price_to_engine(ctrl.engine, p1, t1)
        assert ctrl.engine.state.highest_filled == 1
        assert ctrl.engine.state.legs[-1].ts_ms == t1
        new_p0 = ctrl._sync_trade_windows_from_engine(schedule_backfill=False)
        assert new_p0 is False
        assert len(rest_calls) == 1  # no REST on progression
        assert ctrl.trade_store.ladder_start_ms == t0
        assert ctrl.trade_store.step_start_ms == t1
        assert before_ids <= {t.agg_id for t in ctrl.trade_store.trades}
        assert len(ctrl.trade_store) == before_len
        assert ctrl.trade_store.backfill_complete is True
        md1 = ctrl.trade_store.compute(now_ms=1_590_000)
        assert md1.ladder_status == md1.step_status == "COMPLETE"
        assert md1.source == "AGGTRADE"
        assert md1.ladder_trade_count == before_len
        assert md1.step_trade_count == sum(1 for t in ctrl.trade_store.trades if t.ts_ms >= t1)
        assert md1.step_trade_count < md1.ladder_trade_count
        # ladder metrics unchanged vs same end when only step filter moves
        assert md1.ladder_vwap == md0.ladder_vwap
        assert md1.ladder_poc == md0.ladder_poc
        assert md1.step_vwap != md1.ladder_vwap or md1.step_trade_count == md1.ladder_trade_count

        t2 = 1_400_000
        apply_price_to_engine(ctrl.engine, p2, t2)
        assert ctrl.engine.state.highest_filled == 2
        ctrl._sync_trade_windows_from_engine(schedule_backfill=False)
        assert len(rest_calls) == 1
        assert ctrl.trade_store.ladder_start_ms == t0
        assert ctrl.trade_store.step_start_ms == t2
        md2 = ctrl.trade_store.compute(now_ms=1_590_000)
        assert md2.step_trade_count < md1.step_trade_count
        assert md2.ladder_trade_count == md1.ladder_trade_count
        frag = ctrl._metric_fragment()
        assert frag["metric_source"] == "AGGTRADE"
        assert frag["ladder_metric_status"] == "COMPLETE"
        assert frag["ladder_poc"] is not None and frag["active_step_poc"] is not None

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
        assert ctrl._trade_backfill_task is not None
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
        assert snap["metric_source"] == "AGGTRADE"
        assert snap["ladder_metric_status"] == "COMPLETE"

    asyncio.run(go())
