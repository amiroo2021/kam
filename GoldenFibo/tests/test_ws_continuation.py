from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from goldenfibo.marketdata.aggtrade_cache import AggTradeCache
from goldenfibo.marketdata.binance_public import combined_stream_url
from goldenfibo.metrics.trade_vap import AggTrade
from goldenfibo.session.controller import SessionController
from goldenfibo.session.types import SessionPhase
from goldenfibo.engine.events import MarketEvent, MarketEventKind


def test_futures_ws_uses_trade_stream_not_silent_aggtrade_stream():
    url = combined_stream_url("HYPEUSDT", "1m", market="futures")
    assert url == "wss://fstream.binance.com/stream?streams=hypeusdt@trade/hypeusdt@kline_1m"


def test_raw_futures_trade_persists_as_ws_and_dedupes(tmp_path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    msg = {"e": "trade", "t": 761141769, "p": "92.22500", "q": "7.59", "T": 1789823262465, "m": True}
    assert cache.ingest_ws_message("futures", "HYPEUSDT", msg) is True
    assert cache.ingest_ws_message("futures", "HYPEUSDT", msg) is False
    rows = cache.query_trades_range("futures", "HYPEUSDT", 1789823262465, 1789823262465)
    assert [(r.id_domain, r.agg_id, r.first_trade_id, r.price, r.qty, r.ts_ms) for r in rows] == [
        ("trade", -761141769, 761141769, 92.225, 7.59, 1789823262465)
    ]


def test_ws_observation_coverage_advances_only_while_connected(tmp_path):
    ctrl = SessionController()
    ctrl.market = "futures"
    ctrl.symbol = "HYPEUSDT"
    ctrl.aggtrade_cache = AggTradeCache(tmp_path / "aggtrades.sqlite")

    ctrl._record_ws_observation(1_000)
    ctrl._record_ws_observation(2_000)
    assert ctrl.aggtrade_cache.missing_ranges("futures", "HYPEUSDT", 1_000, 2_000) == []

    # Simulate disconnect/reconnect: do not bridge the disconnected interval.
    ctrl._ws_coverage_edge_ms = None
    ctrl._record_ws_observation(5_000)
    assert ctrl.aggtrade_cache.missing_ranges("futures", "HYPEUSDT", 1_000, 5_000) == [(2_001, 4_999)]

    # REST repair can verify the disconnected gap without synthetic trades.
    ctrl.aggtrade_cache.record_coverage("futures", "HYPEUSDT", 2_001, 4_999, source="rest", note="repair")
    assert ctrl.aggtrade_cache.missing_ranges("futures", "HYPEUSDT", 1_000, 5_000) == []


def test_controller_accepts_raw_trade_ws_for_cache_and_metrics(tmp_path):
    ctrl = SessionController()
    ctrl.market = "futures"
    ctrl.symbol = "HYPEUSDT"
    ctrl.phase = SessionPhase.LIVE
    ctrl._live_enabled = True
    ctrl._aggtrade_metrics_enabled = True
    ctrl.aggtrade_cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    ctrl.engine.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1_000_000, price=Decimal("100")))
    ctrl._p0_seeded = True
    ctrl.trade_store.set_windows(ladder_start_ms=1_000_000, step_start_ms=1_000_000)
    ctrl.trade_store.apply_rest_backfill(
        [AggTrade(1, 100.0, 1.0, 1_000_000)],
        requested_start_ms=1_000_000,
        requested_end_ms=1_000_000,
    )
    # Keep this test scoped to WS cache/metric ingestion; engine live-price
    # progression is covered elsewhere.
    ctrl.fence_ms = 2_000_000

    async def run():
        await ctrl._on_ws_raw(json.dumps({
            "stream": "hypeusdt@trade",
            "data": {"e": "trade", "t": 2, "p": "101.0", "q": "2.0", "T": 1_000_500, "m": False},
        }))

    asyncio.run(run())
    rows = ctrl.aggtrade_cache.query_trades_range("futures", "HYPEUSDT", 1_000_500, 1_000_500)
    assert len(rows) == 1
    assert rows[0].id_domain == "trade"
    assert rows[0].agg_id == -2
    assert rows[0].first_trade_id == 2
    assert ctrl.aggtrade_cache.missing_ranges("futures", "HYPEUSDT", 1_000_500, 1_000_500) == []
    snap = ctrl._metric_fragment()
    assert snap["ladder_metric_source"] == "AGGTRADE"
    assert snap["step_metric_source"] == "AGGTRADE"
