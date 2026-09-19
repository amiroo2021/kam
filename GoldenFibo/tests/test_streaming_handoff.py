"""Streaming handoff invariants: same-millisecond trades, endpoint-ms races,
high-volume WS handoff losslessness, REST repair reconciliation after
incremental counting.

These tests exercise the production streaming path: the accumulator
is installed without a materialized `_by_id` and we use the persistent
SQLite cache as the authoritative source of trades.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from pathlib import Path

from goldenfibo.marketdata.aggtrade_cache import AggTradeCache
from goldenfibo.metrics.trade_store import TradeMetricStore
from goldenfibo.metrics.trade_vap import (
    AggTrade,
    StreamMetricsAccumulator,
    trade_metrics_for_windows,
)


def _agg_row(aid: int, ts: int, px: float, qty: float, f: int, l: int):
    return {
        "a": aid,
        "p": str(px),
        "q": str(qty),
        "f": f,
        "l": l,
        "T": ts,
    }


def _populate(cache, rows):
    cache.insert_trades("futures", "HYPEUSDT", rows, source="rest")


def _install_streaming_snapshot(store, cache, ladder_start, step_start, end):
    acc = StreamMetricsAccumulator(
        ladder_start_ms=ladder_start, step_start_ms=step_start, bin_size=0.01
    )
    for trade in cache.iter_deduped_trades_range("futures", "HYPEUSDT", ladder_start, end):
        acc.add(trade)
    store.apply_streaming_backfill(
        acc,
        market="futures",
        symbol="HYPEUSDT",
        ladder_start_ms=ladder_start,
        step_start_ms=step_start,
        endpoint_ts_ms=end,
    )


# ---------------------------------------------------------------------------
# Identity: timestamp is NOT identity.
# ---------------------------------------------------------------------------


def test_same_millisecond_trades_all_counted(tmp_path: Path):
    """Multiple raw trades at the same ts must each be counted once. Trade
    identity is (id_domain, real_trade_id), NOT timestamp."""
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    # Aggregate at ts=1000 covers raw IDs 90..99. So raws at ts=1000 with
    # IDs 100..102 are NOT covered.
    _populate(cache, [_agg_row(1001, 1000, 100.0, 5.0, 90, 99)])
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 100, "p": "10", "q": "1.0", "T": 1000})
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 101, "p": "10", "q": "1.0", "T": 1000})
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 102, "p": "10", "q": "1.0", "T": 1000})
    # Duplicate delivery of ID 101 (resilient to redelivery).
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 101, "p": "10", "q": "1.0", "T": 1000})

    store = TradeMetricStore(tick_size=0.01, market="futures", symbol="HYPEUSDT")
    store.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    _install_streaming_snapshot(store, cache, 1000, 1000, 2000)
    md = store.compute()
    # 1 aggregate (qty=5) + 3 raw trades (qty=1 each) = 8.0
    assert md.ladder_trade_count == 4
    assert abs(md.ladder_total_qty - 8.0) < 1e-9
    # All three raw IDs are present in canonical deduped query.
    rows = list(
        cache.iter_deduped_trades_range("futures", "HYPEUSDT", 0, 9999)
    )
    raw_tids = sorted([r.first_trade_id for r in rows if r.id_domain == "trade"])
    assert raw_tids == [100, 101, 102]


def test_same_ms_aggregate_storage_id_distinct(tmp_path: Path):
    """Two aggregates at the same ts with different agg IDs must each be
    counted once."""
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    _populate(cache, [
        _agg_row(5001, 1000, 100.0, 1.0, 1, 5),
        _agg_row(5002, 1000, 110.0, 2.0, 6, 10),
    ])
    store = TradeMetricStore(tick_size=0.01, market="futures", symbol="HYPEUSDT")
    store.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    _install_streaming_snapshot(store, cache, 1000, 1000, 2000)
    md = store.compute()
    assert md.ladder_trade_count == 2
    assert abs(md.ladder_total_qty - 3.0) < 1e-9


# ---------------------------------------------------------------------------
# Endpoint-millisecond race.
# ---------------------------------------------------------------------------


def test_endpoint_ms_race_includes_same_ms_new_trades(tmp_path: Path):
    """Historical final trade ID 200 @ ts=T0. While worker is completing,
    trade ID 201 @ ts=T0 arrives. Trade ID 202 @ ts=T0+1 arrives. The
    accumulator must include all three exactly once.

    Identity is (ts, storage_id). Same-ms trades with distinct storage IDs
    are all distinct economic trades.
    """
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    _populate(cache, [_agg_row(200, 1000, 100.0, 1.0, 1, 1)])
    store = TradeMetricStore(tick_size=0.01, market="futures", symbol="HYPEUSDT")
    store.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    _install_streaming_snapshot(store, cache, 1000, 1000, 1000)

    md_pre = store.compute()
    pre_count = md_pre.ladder_trade_count
    pre_qty = md_pre.ladder_total_qty

    # Trade 201 arrives at the same ms as the final historical trade.
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 201, "p": "10", "q": "1.0", "T": 1000})
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 202, "p": "10", "q": "1.0", "T": 1001})

    n = store.catchup_from_persistent(cache, max_ts_ms=9999)
    assert n == 2
    md_post = store.compute()
    assert md_post.ladder_trade_count == pre_count + 2
    assert abs(md_post.ladder_total_qty - (pre_qty + 2.0)) < 1e-9


# ---------------------------------------------------------------------------
# High-volume WS handoff (>5000, 20,000 trades). No loss possible because
# authoritative source is the persistent cache.
# ---------------------------------------------------------------------------


def test_20000_trade_handoff_lossless(tmp_path: Path):
    """20,000 distinct raw trades arriving during backfill must all be
    incorporated exactly once. Authoritative source is the persistent
    cache; the in-memory buffer is no longer used."""
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    _populate(cache, [_agg_row(101, 1000, 100.0, 1.0, 1, 1)])
    store = TradeMetricStore(tick_size=0.01, market="futures", symbol="HYPEUSDT")
    store.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    _install_streaming_snapshot(store, cache, 1000, 1000, 1500)

    md_pre = store.compute()
    pre_qty = md_pre.ladder_total_qty
    pre_count = md_pre.ladder_trade_count

    # Persist 20,000 raw trades with distinct real_trade_ids and ts > endpoint.
    base_ts = 2000
    rng = random.Random(42)
    prices = [round(100.0 + i * 0.001, 4) for i in range(200)]
    for i in range(20_000):
        tx = base_ts + (i // 100)  # 100 trades per ms
        px = prices[i % 200]
        cache.ingest_ws_message(
            "futures", "HYPEUSDT",
            {"e": "trade", "t": 1000 + i, "p": str(px), "q": "0.1", "T": tx},
        )
    # Lossless catch-up.
    n = store.catchup_from_persistent(cache, max_ts_ms=99999)
    assert n == 20_000
    md_post = store.compute()
    # 20,000 trades added, each qty=0.1.
    assert md_post.ladder_trade_count == pre_count + 20_000
    assert abs(md_post.ladder_total_qty - (pre_qty + 20_000 * 0.1)) < 1e-6
    # Canonical six-metric parity.
    canonical_trades = list(
        cache.iter_deduped_trades_range("futures", "HYPEUSDT", 1000, 99999)
    )
    canonical = trade_metrics_for_windows(
        canonical_trades,
        ladder_start_ts_ms=1000,
        step_start_ts_ms=1000,
        bin_size=0.01,
    )
    assert abs(md_post.ladder_vwap - canonical.ladder_vwap) < 1e-6
    assert abs(md_post.step_vwap - canonical.step_vwap) < 1e-6
    assert md_post.ladder_poc == canonical.ladder_poc
    assert md_post.step_poc == canonical.step_poc
    assert abs(md_post.ladder_total_qty - canonical.ladder_total_qty) < 1e-6


# ---------------------------------------------------------------------------
# REST repair after incremental counting.
# ---------------------------------------------------------------------------


def test_rest_repair_after_incremental_counting(tmp_path: Path):
    """Scenario:
       1. Raw WS trades 300,301,302 are incrementally incorporated
       2. Later REST repair inserts aggregate A covering [300..302]
       3. Persistent canonical query prefers aggregate A
       4. Reconciliation: invalidate streaming snapshot, re-stream
          [ladder_start, current_endpoint], atomically install.
       Final result must equal canonical deduped SQLite query.
    """
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    # Step 1: incremental WS raws (no aggregate history yet).
    for tid in (300, 301, 302):
        cache.ingest_ws_message(
            "futures", "HYPEUSDT",
            {"e": "trade", "t": tid, "p": "10", "q": "1.0", "T": 1000 + tid},
        )
    # Initial snapshot (only raws at this point).
    store = TradeMetricStore(tick_size=0.01, market="futures", symbol="HYPEUSDT")
    store.set_windows(ladder_start_ms=1300, step_start_ms=1300)
    _install_streaming_snapshot(store, cache, 1300, 1300, 9999)
    md_pre = store.compute()
    assert md_pre.ladder_trade_count == 3
    assert abs(md_pre.ladder_total_qty - 3.0) < 1e-9

    # Step 2: REST aggregate covering raws 300..302.
    cache.insert_trades(
        "futures", "HYPEUSDT",
        [_agg_row(9001, 1500, 10.0, 3.0, 300, 302)],
        source="rest",
    )

    # Step 3: canonical deduped query now prefers aggregate A.
    canonical_trades = list(
        cache.iter_deduped_trades_range("futures", "HYPEUSDT", 1300, 9999)
    )
    assert len(canonical_trades) == 1
    assert canonical_trades[0].id_domain == "aggtrade"
    assert canonical_trades[0].qty == 3.0

    # Step 4: bounded re-stream + atomic install. Window unchanged, so
    # the streaming fast-path is invalidated (because the new accumulator
    # reflects updated canonical data) and re-installed with the same
    # identity.
    install_end = 9999
    acc = StreamMetricsAccumulator(ladder_start_ms=1300, step_start_ms=1300, bin_size=0.01)
    for trade in cache.iter_deduped_trades_range("futures", "HYPEUSDT", 1300, install_end):
        acc.add(trade)
    store.apply_streaming_backfill(
        acc,
        market="futures",
        symbol="HYPEUSDT",
        ladder_start_ms=1300,
        step_start_ms=1300,
        endpoint_ts_ms=install_end,
    )
    md_post = store.compute()
    # Aggregate replaces the three raws (canonical preference).
    assert md_post.ladder_trade_count == 1
    assert abs(md_post.ladder_total_qty - 3.0) < 1e-9
    assert md_post.ladder_vwap == 10.0
    # Six metrics match canonical computation.
    canonical = trade_metrics_for_windows(
        canonical_trades,
        ladder_start_ts_ms=1300,
        step_start_ts_ms=1300,
        bin_size=0.01,
        max_start_gap_ms=2000,
    )
    assert abs(md_post.ladder_vwap - canonical.ladder_vwap) < 1e-9
    assert abs(md_post.step_vwap - canonical.step_vwap) < 1e-9
    assert md_post.ladder_poc == canonical.ladder_poc
    assert md_post.step_poc == canonical.step_poc
    assert abs(md_post.ladder_total_qty - canonical.ladder_total_qty) < 1e-9


# ---------------------------------------------------------------------------
# Step transition with live trades at endpoint ms.
# ---------------------------------------------------------------------------


def test_step_transition_with_endpoint_ms_live_trades(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    _populate(cache, [
        _agg_row(101, 1000, 100.0, 1.0, 1, 1),
        _agg_row(102, 2000, 110.0, 2.0, 2, 2),
    ])
    store = TradeMetricStore(tick_size=0.01, market="futures", symbol="HYPEUSDT")
    store.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    _install_streaming_snapshot(store, cache, 1000, 1000, 2000)

    # Step transition at the same ms as the final historical trade.
    store.set_windows(ladder_start_ms=1000, step_start_ms=2000)
    md_after = store.compute()
    assert md_after.ladder_metric_source == "AGGTRADE"
    assert md_after.step_metric_source == "OHLC_APPROXIMATION"

    # Re-stream step window.
    ok = store.reconcile_step_window(cache, new_step_start_ms=2000, endpoint_ts_ms=2500)
    assert ok
    md_post = store.compute()
    assert md_post.ladder_metric_source == "AGGTRADE"
    assert md_post.step_metric_source == "AGGTRADE"
    # Ladder unchanged.
    assert md_post.ladder_trade_count == 2
    assert abs(md_post.ladder_total_qty - 3.0) < 1e-9
    # Step = trade at ts=2000 only (qty=2).
    assert md_post.step_trade_count == 1
    assert abs(md_post.step_total_qty - 2.0) < 1e-9

    # Live trades after the reconcile endpoint are picked up via catch-up.
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 999, "p": "120", "q": "0.5", "T": 3000})
    n = store.catchup_from_persistent(cache, max_ts_ms=9999)
    assert n == 1
    md_final = store.compute()
    assert md_final.ladder_trade_count == 3
    assert abs(md_final.ladder_total_qty - 3.5) < 1e-9
    assert md_final.step_trade_count == 2
    assert abs(md_final.step_total_qty - 2.5) < 1e-9


# ---------------------------------------------------------------------------
# P0 transition with live trades at endpoint ms.
# ---------------------------------------------------------------------------


def test_p0_transition_with_endpoint_ms_live_trades(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    _populate(cache, [
        _agg_row(101, 1000, 100.0, 1.0, 1, 1),
        _agg_row(102, 3000, 110.0, 2.0, 2, 2),
        _agg_row(103, 4000, 120.0, 3.0, 3, 3),
    ])
    store = TradeMetricStore(tick_size=0.01, market="futures", symbol="HYPEUSDT")
    store.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    _install_streaming_snapshot(store, cache, 1000, 1000, 5000)
    md1 = store.compute()
    assert md1.ladder_trade_count == 3
    assert abs(md1.ladder_total_qty - 6.0) < 1e-9

    # New P0 at ts=2500.
    store.set_windows(ladder_start_ms=2500, step_start_ms=2500)
    md_after = store.compute()
    assert md_after.ladder_metric_source == "OHLC_APPROXIMATION"
    assert md_after.step_metric_source == "OHLC_APPROXIMATION"

    # Re-stream bounded ladder+step window for new P0.
    install_end = 5000
    acc = StreamMetricsAccumulator(ladder_start_ms=2500, step_start_ms=2500, bin_size=0.01)
    for trade in cache.iter_deduped_trades_range("futures", "HYPEUSDT", 2500, install_end):
        acc.add(trade)
    store.apply_streaming_backfill(
        acc,
        market="futures", symbol="HYPEUSDT",
        ladder_start_ms=2500, step_start_ms=2500, endpoint_ts_ms=install_end,
    )
    md_new = store.compute()
    # New ladder sees only ts >= 2500 → trades at ts=3000 and ts=4000 (qty=5).
    assert md_new.ladder_trade_count == 2
    assert abs(md_new.ladder_total_qty - 5.0) < 1e-9
    # Old-cycle 6.0 must NOT leak.
    assert md_new.ladder_total_qty != md1.ladder_total_qty

    # Live trade at endpoint ms.
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 500, "p": "130", "q": "1.0", "T": 4000})
    n = store.catchup_from_persistent(cache, max_ts_ms=9999)
    assert n == 1  # trade ID 500 at ts=4000 NOT covered by aggregate [2..3] or [3..3] (aggregate 103 is f=3,l=3).
    md_final = store.compute()
    assert md_final.ladder_trade_count == 3
    assert abs(md_final.ladder_total_qty - 6.0) < 1e-9


# ---------------------------------------------------------------------------
# No historical rescan on ordinary live tick.
# ---------------------------------------------------------------------------


def test_ordinary_live_tick_does_not_rescan_history(tmp_path: Path):
    """An ordinary live tick must NOT call iter_deduped_trades_range or
    iter_after_frontier. The persistent catch-up is only invoked by the
    controller's `_run_trade_backfill`."""
    import goldenfibo.marketdata.aggtrade_cache as ac_mod

    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    _populate(cache, [_agg_row(101, 1000, 100.0, 1.0, 1, 1)])
    store = TradeMetricStore(tick_size=0.01, market="futures", symbol="HYPEUSDT")
    store.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    _install_streaming_snapshot(store, cache, 1000, 1000, 2000)

    orig_deduped = ac_mod.AggTradeCache.iter_deduped_trades_range
    orig_after = ac_mod.AggTradeCache.iter_after_frontier
    calls = {"deduped": 0, "after": 0}

    def counted_d(*a, **kw):
        calls["deduped"] += 1
        return orig_deduped(*a, **kw)

    def counted_a(*a, **kw):
        calls["after"] += 1
        return orig_after(*a, **kw)

    ac_mod.AggTradeCache.iter_deduped_trades_range = counted_d
    ac_mod.AggTradeCache.iter_after_frontier = counted_a
    try:
        # Drive ordinary live updates.
        for i in range(50):
            store.ingest_live_trade(
                ts_ms=5000 + i, price=100.0, qty=0.1,
                storage_id=-(10000 + i), real_trade_id=10000 + i,
                id_domain="trade",
            )
        # Drive compute() calls.
        for _ in range(50):
            store.compute()
        assert calls["deduped"] == 0, f"deduped called {calls['deduped']} times"
        assert calls["after"] == 0, f"after called {calls['after']} times"
    finally:
        ac_mod.AggTradeCache.iter_deduped_trades_range = orig_deduped
        ac_mod.AggTradeCache.iter_after_frontier = orig_after


# ---------------------------------------------------------------------------
# Strict (ts, storage_id) frontier — proving same-ms distinct IDs accepted.
# ---------------------------------------------------------------------------


def test_live_add_accepts_same_ms_higher_storage_id(tmp_path: Path):
    """live_add must accept a trade whose ts == latest_observed_ts_ms but
    whose storage_id is strictly greater (Binance can produce multiple
    economic trades at the same ms)."""
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    _populate(cache, [_agg_row(101, 1000, 100.0, 1.0, 1, 1)])
    store = TradeMetricStore(tick_size=0.01, market="futures", symbol="HYPEUSDT")
    store.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    _install_streaming_snapshot(store, cache, 1000, 1000, 2000)
    md_pre = store.compute()
    pre_count = md_pre.ladder_trade_count

    # Live trade at ts=1000 (same as final historical). Distinct real
    # trade ID; storage_id in negative keyspace (raw @trade).
    ok = store.ingest_live_trade(
        ts_ms=1000, price=110.0, qty=0.5,
        storage_id=-500, real_trade_id=500, id_domain="trade",
    )
    assert ok
    md1 = store.compute()
    assert md1.ladder_trade_count == pre_count + 1
    # Second live trade at the same ms, distinct storage_id.
    ok2 = store.ingest_live_trade(
        ts_ms=1000, price=120.0, qty=0.3,
        storage_id=-501, real_trade_id=501, id_domain="trade",
    )
    assert ok2
    md2 = store.compute()
    assert md2.ladder_trade_count == pre_count + 2


def test_live_add_rejects_same_ms_duplicate_identity(tmp_path: Path):
    """Same-ms, same (id_domain, real_trade_id) → reject (no double-count)."""
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    _populate(cache, [_agg_row(101, 1000, 100.0, 1.0, 1, 1)])
    store = TradeMetricStore(tick_size=0.01, market="futures", symbol="HYPEUSDT")
    store.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    _install_streaming_snapshot(store, cache, 1000, 1000, 2000)
    md_pre = store.compute()
    pre_count = md_pre.ladder_trade_count
    # Live trade at ts=1000 with storage_id=-1000, real_trade_id=1000.
    ok = store.ingest_live_trade(
        ts_ms=1000, price=110.0, qty=0.5,
        storage_id=-1000, real_trade_id=1000, id_domain="trade",
    )
    assert ok
    md1 = store.compute()
    assert md1.ladder_trade_count == pre_count + 1
    # Same (id_domain, real_trade_id) → reject (no double-count) regardless
    # of storage_id value or ts equality.
    ok2 = store.ingest_live_trade(
        ts_ms=1000, price=999.0, qty=99.0,
        storage_id=-1000, real_trade_id=1000, id_domain="trade",
    )
    assert ok2 is False
    md_final = store.compute()
    assert md_final.ladder_trade_count == pre_count + 1
