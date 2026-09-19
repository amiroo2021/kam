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


def test_incomplete_backfill_keeps_ohlc_fallback_visible(monkeypatch, tmp_path):
    from goldenfibo.marketdata.aggtrade_cache import AggTradeCache

    def bad_fetch(symbol, start_ms, end_ms, **kwargs):
        # first trade long after P0
        return [AggTrade(1, 100.0, 1.0, start_ms + 60_000)]

    monkeypatch.setattr("goldenfibo.session.controller.time.time", lambda: 1_600.0)
    ctrl = SessionController()
    ctrl.aggtrade_cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    ctrl._agg_archive_fetch = ctrl.aggtrade_cache.fetch_archive_range
    ctrl.mode = SessionMode.LIVE
    ctrl._agg_trades_fetch = bad_fetch
    ctrl.engine.on_event(
        MarketEvent(MarketEventKind.SEED_P0, ts_ms=1_000_000, price=Decimal("100"))
    )
    ctrl._p0_seeded = True
    ctrl._aggtrade_metrics_enabled = True
    from goldenfibo.engine.engine import GoldenFiboEngine
    from goldenfibo.engine.config import EngineConfig, Side
    from goldenfibo.engine.levels import ladder_step
    from goldenfibo.live.price_path import apply_price_to_engine
    ctrl.engine = GoldenFiboEngine(EngineConfig(side=Side.BUY, percentage=Decimal('0.001'), symbol='BTCUSDT'))
    ctrl.engine.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1_000_000, price=Decimal('100')))
    apply_price_to_engine(ctrl.engine, ladder_step(Side.BUY, Decimal('100'), 1)[0], 1_100_000)
    ctrl.bars = [
        __import__('goldenfibo.metrics', fromlist=['OhlcvBar']).OhlcvBar(1_000_000, 100, 101, 99, 100.5, 1.0, 1000.0),
        __import__('goldenfibo.metrics', fromlist=['OhlcvBar']).OhlcvBar(1_060_000, 100.5, 101.5, 100, 101.0, 2.0, 2000.0),
        __import__('goldenfibo.metrics', fromlist=['OhlcvBar']).OhlcvBar(1_120_000, 101.0, 102.0, 100.5, 101.5, 3.0, 3000.0),
    ]
    ctrl.chart_candles = [
        {'time': 1_000_000, 'open': 100, 'high': 101, 'low': 99, 'close': 100.5},
        {'time': 1_060_000, 'open': 100.5, 'high': 101.5, 'low': 100, 'close': 101.0},
        {'time': 1_120_000, 'open': 101.0, 'high': 102.0, 'low': 100.5, 'close': 101.5},
    ]

    async def go():
        ctrl._sync_trade_windows_from_engine(schedule_backfill=True)
        await ctrl._trade_backfill_task
        snap = ctrl.snapshot_dict()
        assert snap["metric_source"] == "OHLC_APPROXIMATION"
        assert snap["ladder_metric_source"] in ("OHLC_APPROXIMATION", "AGGTRADE")
        assert snap["step_metric_source"] in ("OHLC_APPROXIMATION", "AGGTRADE")
        assert snap["ladder_aggtrade_status"] == "INCOMPLETE_TRADE_HISTORY"
        assert snap["step_aggtrade_status"] == "INCOMPLETE_TRADE_HISTORY"
        assert snap["ladder_metric_status"] == "COMPLETE"
        assert snap["ladder_poc"] is not None
        assert snap["ladder_vwap"] is not None
        assert snap["active_step_vwap"] is not None
        assert snap["active_step_poc"] is not None

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


def test_progression_keeps_ladder_store_and_skips_rest_backfill(monkeypatch, tmp_path):
    """P0→P1→P2: ladder window+history stable; step filter moves; no REST on step change."""
    from goldenfibo import EngineConfig, GoldenFiboEngine, Side
    from goldenfibo.marketdata.aggtrade_cache import AggTradeCache
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
        assert md2.step_trade_count <= md1.step_trade_count
        assert md2.ladder_trade_count == md1.ladder_trade_count
        frag = ctrl._metric_fragment()
        assert frag["metric_source"] in ("AGGTRADE", "MIXED")
        assert frag["ladder_metric_status"] == "COMPLETE"
        assert frag["ladder_poc"] is not None
        assert frag["active_step_poc"] is not None or frag["step_metric_source"] == "OHLC_APPROXIMATION"

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



def test_ohlc_fallback_visible_during_incomplete_aggtrade_and_updates_with_new_bars():
    from goldenfibo.engine.engine import GoldenFiboEngine
    from goldenfibo.engine.levels import ladder_step
    from goldenfibo.live.price_path import apply_price_to_engine
    from goldenfibo.engine.config import EngineConfig, Side

    ctrl = SessionController()
    ctrl.mode = SessionMode.REPLAY_TO_LIVE
    ctrl._agg_trades_fetch = lambda *a, **k: [AggTrade(1, 100.0, 1.0, 9_999_999_999)]
    ctrl._aggtrade_metrics_enabled = True
    ctrl.engine = GoldenFiboEngine(EngineConfig(side=Side.SELL, percentage=Decimal('0.001'), symbol='HYPEUSDT'))
    ctrl.engine.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1_000_000, price=Decimal('100')))
    apply_price_to_engine(ctrl.engine, ladder_step(Side.SELL, Decimal('100'), 1)[0], 1_100_000)
    ctrl._p0_seeded = True
    ctrl.phase = SessionPhase.REPLAYING
    # seed OHLC history
    ctrl.bars = [
        __import__('goldenfibo.metrics', fromlist=['OhlcvBar']).OhlcvBar(1_000_000, 100, 101, 99, 100.5, 1.0, 100.5),
        __import__('goldenfibo.metrics', fromlist=['OhlcvBar']).OhlcvBar(1_060_000, 100.5, 101.5, 100, 101.0, 2.0, 202.0),
        __import__('goldenfibo.metrics', fromlist=['OhlcvBar']).OhlcvBar(1_120_000, 101.0, 102.0, 100.5, 101.5, 3.0, 304.5),
    ]
    ctrl.chart_candles = [
        {'time': 1_000_000, 'open': 100, 'high': 101, 'low': 99, 'close': 100.5},
        {'time': 1_060_000, 'open': 100.5, 'high': 101.5, 'low': 100, 'close': 101.0},
        {'time': 1_120_000, 'open': 101.0, 'high': 102.0, 'low': 100.5, 'close': 101.5},
    ]
    ctrl._sync_trade_windows_from_engine(schedule_backfill=False)
    ctrl.trade_store.handoff_status = 'incomplete'
    pre = ctrl._metric_fragment()
    assert pre['metric_source'] == 'OHLC_APPROXIMATION'
    assert pre['ladder_vwap'] is not None
    assert pre['active_step_vwap'] is not None
    assert pre['ladder_metric_status'] == 'COMPLETE'
    assert pre['ladder_metric_source'] == 'OHLC_APPROXIMATION'
    assert pre['step_metric_source'] == 'OHLC_APPROXIMATION'
    # handoff makes aggtrade incomplete, but fallback must keep OHLC visible
    ctrl.trade_store.handoff_status = 'incomplete'
    ctrl._live_enabled = True
    ctrl.phase = SessionPhase.LIVE
    post = ctrl._metric_fragment()
    assert post['ladder_vwap'] is not None
    assert post['active_step_vwap'] is not None
    assert post['ladder_metric_source'] == 'OHLC_APPROXIMATION'
    assert post['step_metric_source'] == 'OHLC_APPROXIMATION'
    # after new OHLC bar, fallback updates (not frozen handoff values)
    apply_price_to_engine(ctrl.engine, ladder_step(Side.SELL, Decimal('100'), 1)[0], 1_180_000)
    ctrl.bars.append(__import__('goldenfibo.metrics', fromlist=['OhlcvBar']).OhlcvBar(1_180_000, 101.5, 102.5, 101.0, 102.0, 4.0, 408.0))
    ctrl.chart_candles.append({'time': 1_180_000, 'open': 101.5, 'high': 102.5, 'low': 101.0, 'close': 102.0})
    updated = ctrl._metric_fragment()
    assert updated['ladder_vwap'] != pre['ladder_vwap'] or updated['active_step_vwap'] != pre['active_step_vwap']


def test_step_and_ladder_can_switch_independently_with_coverage_flags():
    from goldenfibo.metrics.trade_store import TradeMetricStore
    from goldenfibo.metrics.trade_vap import AggTrade
    s = TradeMetricStore(tick_size=0.01)
    s.set_windows(ladder_start_ms=1000, step_start_ms=1500)
    # ladder incomplete (start too late), step complete
    s.apply_rest_backfill([AggTrade(1, 100.0, 1.0, 1600), AggTrade(2, 101.0, 1.0, 1700)], requested_start_ms=1000, requested_end_ms=2000)
    m = s.compute(now_ms=2000)
    assert m.step_status == 'COMPLETE'
    assert m.ladder_status in ('COMPLETE', 'INCOMPLETE_TRADE_HISTORY')


def test_backfill_uses_current_windows_not_replay_start():
    from goldenfibo.engine.engine import GoldenFiboEngine
    from goldenfibo.engine.config import EngineConfig, Side
    from goldenfibo.engine.events import MarketEvent, MarketEventKind
    from goldenfibo.engine.levels import ladder_step
    from goldenfibo.live.price_path import apply_price_to_engine
    from goldenfibo.metrics.trade_vap import AggTrade

    rest_calls = []

    def fake_fetch(symbol, start_ms, end_ms, **kwargs):
        rest_calls.append((start_ms, end_ms))
        return [
            AggTrade(1, 100.0, 1.0, 2_000_000),
            AggTrade(2, 100.1, 1.0, 2_060_000),
        ]

    ctrl = SessionController()
    ctrl.mode = SessionMode.REPLAY_TO_LIVE
    ctrl._agg_trades_fetch = fake_fetch
    ctrl._aggtrade_metrics_enabled = True
    ctrl.engine = GoldenFiboEngine(EngineConfig(side=Side.BUY, percentage=Decimal('0.001'), symbol='BTCUSDT'))
    ctrl.engine.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1_000_000, price=Decimal('100')))
    apply_price_to_engine(ctrl.engine, ladder_step(Side.BUY, Decimal('100'), 1)[0], 1_100_000)
    apply_price_to_engine(ctrl.engine, ladder_step(Side.BUY, Decimal('100'), 2)[0], 1_200_000)
    ctrl.bars = [
        __import__('goldenfibo.metrics', fromlist=['OhlcvBar']).OhlcvBar(1_000_000, 100, 101, 99, 100.5, 1.0, 1000.0),
        __import__('goldenfibo.metrics', fromlist=['OhlcvBar']).OhlcvBar(2_000_000, 100.5, 101.5, 100, 101.0, 2.0, 2000.0),
        __import__('goldenfibo.metrics', fromlist=['OhlcvBar']).OhlcvBar(2_060_000, 101.0, 102.0, 100.5, 101.5, 3.0, 3000.0),
    ]
    ctrl.chart_candles = [
        {'time': 1_000_000, 'open': 100, 'high': 101, 'low': 99, 'close': 100.5},
        {'time': 2_000_000, 'open': 100.5, 'high': 101.5, 'low': 100, 'close': 101.0},
        {'time': 2_060_000, 'open': 101.0, 'high': 102.0, 'low': 100.5, 'close': 101.5},
    ]

    async def go():
        await ctrl._run_trade_backfill()
        assert rest_calls
        snap = ctrl.snapshot_dict()
        assert snap['ladder_vwap'] is not None
        assert snap['active_step_vwap'] is not None
        assert ctrl.trade_store.ladder_start_ms == 1_000_000
        assert ctrl.trade_store.step_start_ms == 1_200_000
        assert ctrl.trade_store.backfill_complete is True
        assert snap['metric_source'] in ('OHLC_APPROXIMATION', 'AGGTRADE')

    asyncio.run(go())


def test_futures_step_can_graduate_independently_from_ladder():
    from goldenfibo.engine.engine import GoldenFiboEngine
    from goldenfibo.engine.config import EngineConfig, Side
    from goldenfibo.engine.events import MarketEvent, MarketEventKind
    from goldenfibo.engine.levels import ladder_step
    from goldenfibo.live.price_path import apply_price_to_engine
    from goldenfibo.metrics.trade_vap import AggTrade

    ctrl = SessionController()
    ctrl.mode = SessionMode.REPLAY_TO_LIVE
    ctrl.market = 'futures'
    ctrl.symbol = 'HYPEUSDT'
    ctrl._aggtrade_metrics_enabled = True
    ctrl.engine = GoldenFiboEngine(EngineConfig(side=Side.SELL, percentage=Decimal('0.001'), symbol='HYPEUSDT'))
    ctrl.engine.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1_000_000, price=Decimal('100')))
    apply_price_to_engine(ctrl.engine, ladder_step(Side.SELL, Decimal('100'), 1)[0], 1_100_000)
    apply_price_to_engine(ctrl.engine, ladder_step(Side.SELL, Decimal('100'), 2)[0], 1_200_000)
    ctrl.bars = [
        __import__('goldenfibo.metrics', fromlist=['OhlcvBar']).OhlcvBar(1_000_000, 100, 101, 99, 100.5, 1.0, 100.5),
        __import__('goldenfibo.metrics', fromlist=['OhlcvBar']).OhlcvBar(1_060_000, 100.5, 101.5, 100, 101.0, 2.0, 202.0),
        __import__('goldenfibo.metrics', fromlist=['OhlcvBar']).OhlcvBar(1_120_000, 101.0, 102.0, 100.5, 101.5, 3.0, 304.5),
    ]
    ctrl.chart_candles = [
        {'time': 1_000_000, 'open': 100, 'high': 101, 'low': 99, 'close': 100.5},
        {'time': 1_060_000, 'open': 100.5, 'high': 101.5, 'low': 100, 'close': 101.0},
        {'time': 1_120_000, 'open': 101.0, 'high': 102.0, 'low': 100.5, 'close': 101.5},
    ]
    ctrl.trade_store.set_windows(ladder_start_ms=1_000_000, step_start_ms=1_200_000)
    trades = [
        AggTrade(1, 100.0, 1.0, 1_000_000),
        AggTrade(2, 100.2, 1.0, 1_060_000),
        AggTrade(3, 100.3, 1.0, 1_220_000),
    ]
    ctrl.trade_store.apply_rest_backfill(trades, requested_start_ms=1_000_000, requested_end_ms=1_300_000)
    ctrl.phase = SessionPhase.LIVE
    ctrl._live_enabled = True
    snap = ctrl._metric_fragment()
    assert snap['ladder_metric_source'] == 'AGGTRADE'
    assert snap['step_metric_source'] == 'AGGTRADE'
    assert snap['metric_source'] == 'AGGTRADE'
