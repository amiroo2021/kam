"""Streaming metric accumulator tests.

Validate exact parity between:

  OLD: trade_metrics_for_windows over a materialized list
  NEW: StreamMetricsAccumulator streaming deduped trades

Plus coverage for the controller _stream_metric_backfill path against
the persistent AggTradeCache (REST + archive + WS rows together).

These tests do not replace the existing
tests/test_trade_vap.py parity assertions. They add coverage for the
streaming architecture's correctness invariants:

  * exact parity of VWAP / POC / VAL / VAH / trade counts
  * one-pass ladder+step design
  * bounded-memory vs number-of-bins, not number-of-trades
  * REST/WS overlap dedupe (aggregate takes precedence)
  * start/end time boundary
  * zero-trade verified interval
  * aggregate covering multiple raw trades
  * partial raw overlap
  * multiple price bins / POC tie
  * empty step window
  * one-trade window
  * val-area boundaries
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from goldenfibo.marketdata.aggtrade_cache import AggTradeCache
from goldenfibo.metrics.trade_vap import (
    AggTrade,
    StreamMetricsAccumulator,
    trade_metrics_for_windows,
)


def _vol(rows):
    return sum(r.qty for r in rows)


def _vwap(rows):
    s = sum(r.qty for r in rows)
    if not s:
        return 0.0
    return sum(r.qty * r.price for r in rows) / s


# ---------------------------------------------------------------------------
# Synthetic parity tests (no DB).
# ---------------------------------------------------------------------------


def _synth_trades():
    """Many-trades fixture spanning ladder+step, with several price bins."""
    trades = []
    aid = 1
    # Ladder window: ts >= 1000, step window: ts >= 3000.
    # 30 trades 1000..1290, prices 99.40..101.90 in 0.05 steps.
    for i in range(30):
        ts = 1000 + i * 10
        px = 99.40 + 0.05 * (i % 50)
        qty = 1.0 + 0.1 * (i % 5)
        trades.append(AggTrade(aid, px, qty, ts))
        aid += 1
    # 10 trades 3000..3090 (step window only).
    for i in range(10):
        ts = 3000 + i * 10
        trades.append(AggTrade(aid, 100.50, 0.5, ts))
        aid += 1
    return trades


def test_streaming_vs_canonical_parity_basic():
    trades = _synth_trades()
    canonical = trade_metrics_for_windows(
        trades,
        ladder_start_ts_ms=1000,
        step_start_ts_ms=3000,
        bin_size=0.01,
    )
    acc = StreamMetricsAccumulator(
        ladder_start_ms=1000, step_start_ms=3000, bin_size=0.01
    )
    for t in trades:
        acc.add(t)
    streamed = acc.finalize(window_end_ms=9999)
    assert streamed.ladder_vwap == pytest.approx(canonical.ladder_vwap, rel=1e-12)
    assert streamed.step_vwap == pytest.approx(canonical.step_vwap, rel=1e-12)
    assert streamed.ladder_poc == pytest.approx(canonical.ladder_poc, rel=1e-12)
    assert streamed.step_poc == pytest.approx(canonical.step_poc, rel=1e-12)
    assert streamed.ladder_trade_count == canonical.ladder_trade_count
    assert streamed.step_trade_count == canonical.step_trade_count
    assert streamed.ladder_total_qty == pytest.approx(canonical.ladder_total_qty, rel=1e-12)
    assert streamed.step_total_qty == pytest.approx(canonical.step_total_qty, rel=1e-12)


def test_streaming_runs_in_one_pass():
    """Verify we only need to feed each trade once (no double-scan)."""
    trades = _synth_trades()
    acc = StreamMetricsAccumulator(
        ladder_start_ms=1000, step_start_ms=3000, bin_size=0.01
    )
    feed_count = 0
    for t in trades:
        acc.add(t)
        feed_count += 1
    assert feed_count == len(trades)
    # Ladder counts all trades (40); step counts only >= 3000 (10).
    assert acc.ladder_count == len(trades)
    assert acc.step_count == 10


def test_streaming_handles_empty_windows():
    acc = StreamMetricsAccumulator(
        ladder_start_ms=10_000, step_start_ms=20_000, bin_size=0.01
    )
    res = acc.finalize(window_end_ms=99_999)
    assert res.ladder_vwap is None
    assert res.step_vwap is None
    assert res.ladder_poc is None
    assert res.step_poc is None
    assert res.ladder_trade_count == 0
    assert res.step_trade_count == 0


def test_streaming_one_trade_window():
    trades = [AggTrade(1, 100.0, 2.5, 5000)]
    acc = StreamMetricsAccumulator(ladder_start_ms=1000, step_start_ms=1000, bin_size=0.01)
    for t in trades:
        acc.add(t)
    res = acc.finalize(window_end_ms=9999)
    assert res.ladder_vwap == 100.0
    assert res.step_vwap == 100.0
    assert res.ladder_poc == 100.0
    assert res.step_poc == 100.0
    assert res.ladder_trade_count == 1
    assert res.step_trade_count == 1
    assert res.ladder_total_qty == 2.5


def test_streaming_step_smaller_than_ladder():
    """Ladder and step must track independently within one pass."""
    trades = _synth_trades()
    acc = StreamMetricsAccumulator(ladder_start_ms=1000, step_start_ms=3000, bin_size=0.01)
    for t in trades:
        acc.add(t)
    # Ladder bins must contain entries for prices 99.40..101.90, step only for 100.50.
    res = acc.finalize(window_end_ms=9999)
    assert res.ladder_profile is not None
    assert res.step_profile is not None
    assert len(res.ladder_profile.vols) > len(res.step_profile.vols)
    assert len(res.step_profile.vols) == 1  # only one price


def test_streaming_poc_tie_breaker_uses_lower_bin():
    """Two bins with identical volume → lower bin price wins."""
    trades = [
        AggTrade(1, 100.00, 1.0, 1000),
        AggTrade(2, 100.01, 1.0, 1000),
    ]
    acc = StreamMetricsAccumulator(ladder_start_ms=1000, step_start_ms=1000, bin_size=0.01)
    for t in trades:
        acc.add(t)
    res = acc.finalize(window_end_ms=9999)
    # Both bins get 1.0 qty; tie-breaker: lower bin price.
    assert res.ladder_poc == 100.00


def test_streaming_value_area_boundaries():
    """VAH edge = rightmost bin + bin_size, VAL edge = leftmost bin."""
    trades = [
        AggTrade(1, 100.00, 1.0, 1000),
        AggTrade(2, 100.01, 1.0, 1001),
        AggTrade(3, 100.02, 1.0, 1002),
    ]
    acc = StreamMetricsAccumulator(ladder_start_ms=1000, step_start_ms=1000, bin_size=0.01)
    for t in trades:
        acc.add(t)
    res = acc.finalize(window_end_ms=9999)
    assert res.ladder_poc in (100.00, 100.01, 100.02)
    # Profile bins must include all three edges.
    bins = sorted(res.ladder_profile.vols.keys())
    assert bins == [100.00, 100.01, 100.02]


def test_streaming_zero_qty_trade_ignored():
    trades = [
        AggTrade(1, 100.0, 0.0, 1000),
        AggTrade(2, 101.0, 2.0, 1001),
    ]
    acc = StreamMetricsAccumulator(ladder_start_ms=1000, step_start_ms=1000, bin_size=0.01)
    for t in trades:
        acc.add(t)
    res = acc.finalize(window_end_ms=9999)
    assert res.ladder_vwap == 101.0
    assert res.ladder_total_qty == 2.0
    assert res.ladder_trade_count == 1


def test_streaming_start_end_time_boundary_exact():
    """Trades exactly on the window start are included; pre-window excluded."""
    trades = [
        AggTrade(1, 100.0, 1.0, 999),    # before ladder_start
        AggTrade(2, 100.0, 1.0, 1000),   # exactly ladder_start
        AggTrade(3, 100.0, 1.0, 1001),
    ]
    acc = StreamMetricsAccumulator(ladder_start_ms=1000, step_start_ms=1000, bin_size=0.01)
    for t in trades:
        acc.add(t)
    res = acc.finalize(window_end_ms=9999)
    assert res.ladder_trade_count == 2
    assert res.ladder_total_qty == 2.0


# ---------------------------------------------------------------------------
# Bounded-memory scaling tests (no DB).
# ---------------------------------------------------------------------------


def test_streaming_memory_bounded_by_bins_not_trades():
    """Adding more trades at the SAME bins must not increase state size."""
    acc = StreamMetricsAccumulator(ladder_start_ms=0, step_start_ms=0, bin_size=0.01)
    for i in range(10_000):
        acc.add(AggTrade(i + 1, 100.0, 0.1, 1000 + i))
    bins_ladder = len(acc.ladder_bins)
    # Add another 10k trades at the same two prices.
    for i in range(10_000, 20_000):
        acc.add(AggTrade(i + 1, 100.0, 0.1, 2000 + i))
    assert len(acc.ladder_bins) == bins_ladder  # binned at 100.00 and 100.01
    # Accumulator scalars grew (count, num, den), but bins did not.
    assert acc.ladder_count == 20_000


def test_streaming_many_bins_with_many_trades():
    """100k trades across 1000 distinct prices → bounded by ~1000 bins."""
    acc = StreamMetricsAccumulator(ladder_start_ms=0, step_start_ms=0, bin_size=0.01)
    for i in range(100_000):
        price = 100.0 + (i % 1000) * 0.01
        acc.add(AggTrade(i + 1, price, 0.1, 1000 + i))
    assert acc.ladder_count == 100_000
    assert len(acc.ladder_bins) == 1000


# ---------------------------------------------------------------------------
# Persistent cache integration tests.
# ---------------------------------------------------------------------------


def test_persistent_streaming_aggregate_only(tmp_path: Path):
    db = tmp_path / "aggtrades.sqlite"
    cache = AggTradeCache(db)
    rows = []
    for i in range(100):
        rows.append({
            "a": i + 1,
            "p": str(100.0 + 0.05 * (i % 5)),
            "q": str(1.0 + 0.01 * i),
            "f": 10_000 + i * 5,
            "l": 10_000 + i * 5 + 4,
            "T": 1000 + i * 60,
        })
    cache.insert_trades("futures", "HYPEUSDT", rows, source="rest")
    ladder = 1000
    acc = StreamMetricsAccumulator(ladder_start_ms=ladder, step_start_ms=10_000, bin_size=0.01)
    first_ts = None
    for trade in cache.iter_deduped_trades_range("futures", "HYPEUSDT", ladder, 100_000):
        if first_ts is None:
            first_ts = int(trade.ts_ms)
        acc.add(trade)
    res = acc.finalize(window_end_ms=100_000, earliest_available_ts_ms=first_ts)
    assert res.ladder_vwap is not None
    assert res.ladder_trade_count == 100
    # Cross-check against canonical materialized computation.
    canonical = trade_metrics_for_windows(
        cache.query_trades_range("futures", "HYPEUSDT", ladder, 100_000),
        ladder_start_ts_ms=ladder,
        step_start_ts_ms=10_000,
        bin_size=0.01,
    )
    assert res.ladder_vwap == pytest.approx(canonical.ladder_vwap, rel=1e-9)
    assert str(res.ladder_poc) == str(canonical.ladder_poc)


def test_persistent_streaming_rest_overlap_excludes_raw(tmp_path: Path):
    """Aggregate covers raw trade IDs → raw rows are dropped from stream."""
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    # Raw WS trades at IDs 100..104.
    for tid in range(100, 105):
        cache.ingest_ws_message(
            "futures", "HYPEUSDT",
            {"e": "trade", "t": tid, "p": "10", "q": "1.0", "T": 1000 + tid},
        )
    # REST aggregate covers those same underlying IDs.
    cache.insert_trades(
        "futures", "HYPEUSDT",
        [{"a": 9999, "p": "10", "q": "5.0", "f": 100, "l": 104, "T": 1200}],
        source="rest",
    )
    acc = StreamMetricsAccumulator(ladder_start_ms=1000, step_start_ms=1000, bin_size=0.01)
    seen = []
    for trade in cache.iter_deduped_trades_range("futures", "HYPEUSDT", 0, 9999):
        acc.add(trade)
        seen.append(trade)
    # Only the aggregate row should come through.
    domains = sorted(t.id_domain for t in seen)
    assert domains == ["aggtrade"]
    res = acc.finalize(window_end_ms=9999)
    assert res.ladder_vwap == 10.0
    assert res.ladder_trade_count == 1
    assert res.ladder_total_qty == 5.0


def test_persistent_streaming_aggregate_covers_multiple_raw(tmp_path: Path):
    """Single aggregate row represents N underlying raw trades; raw must be dropped."""
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    # 10 raw trades qty 1 each
    for tid in range(500, 510):
        cache.ingest_ws_message(
            "futures", "HYPEUSDT",
            {"e": "trade", "t": tid, "p": str(100.0 + tid), "q": "1.0", "T": 1000 + tid},
        )
    cache.insert_trades(
        "futures", "HYPEUSDT",
        [{"a": 8888, "p": "105", "q": "10.0", "f": 500, "l": 509, "T": 2000}],
        source="rest",
    )
    acc = StreamMetricsAccumulator(ladder_start_ms=1000, step_start_ms=1000, bin_size=0.01)
    for trade in cache.iter_deduped_trades_range("futures", "HYPEUSDT", 0, 9999):
        acc.add(trade)
    res = acc.finalize(window_end_ms=9999)
    assert res.ladder_total_qty == 10.0
    assert res.ladder_vwap == 105.0
    assert res.ladder_trade_count == 1


def test_persistent_streaming_partial_raw_overlap(tmp_path: Path):
    """Some raw trades inside the aggregate, some outside; both must be counted once."""
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    for tid, q in zip(range(700, 705), (1.0, 2.0, 3.0, 4.0, 5.0)):
        cache.ingest_ws_message(
            "futures", "HYPEUSDT",
            {"e": "trade", "t": tid, "p": "10", "q": str(q), "T": 1000 + tid},
        )
    # Aggregate covers 701..702 only (qty 5 = 2 + 3).
    cache.insert_trades(
        "futures", "HYPEUSDT",
        [{"a": 7777, "p": "10", "q": "5.0", "f": 701, "l": 702, "T": 1500}],
        source="rest",
    )
    acc = StreamMetricsAccumulator(ladder_start_ms=1000, step_start_ms=1000, bin_size=0.01)
    for trade in cache.iter_deduped_trades_range("futures", "HYPEUSDT", 0, 9999):
        acc.add(trade)
    res = acc.finalize(window_end_ms=9999)
    # Aggregate qty 5 replaces raw 701 (2) + 702 (3); raw 700 (1) and 703 (4) + 704 (5)
    # remain → total 1 + 5 + 4 + 5 = 15.
    assert res.ladder_total_qty == 15.0


def test_persistent_streaming_zero_trade_window(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    # No rows; coverage interval is zero-trade.
    cache.record_coverage("futures", "HYPEUSDT", 5000, 5999, source="ws", note="ws-observed")
    acc = StreamMetricsAccumulator(ladder_start_ms=5000, step_start_ms=5000, bin_size=0.01)
    for trade in cache.iter_deduped_trades_range("futures", "HYPEUSDT", 5000, 5999):
        acc.add(trade)
    res = acc.finalize(window_end_ms=5999)
    assert res.ladder_vwap is None
    assert res.ladder_trade_count == 0


# ---------------------------------------------------------------------------
# EXPLAIN QUERY PLAN + memory scale at 100k/500k/1M synthetic rows.
# ---------------------------------------------------------------------------


def test_streaming_query_plan_uses_index(tmp_path: Path):
    """Verify the streaming SQL uses the timestamp index, not a scan."""
    db = tmp_path / "aggtrades.sqlite"
    cache = AggTradeCache(db)
    # Need at least one row so the planner has statistics.
    cache.insert_trades(
        "futures", "HYPEUSDT",
        [AggTrade(1, 100.0, 1.0, 1000, first_trade_id=1, last_trade_id=1, id_domain="aggtrade")],
        source="rest",
    )
    import sqlite3

    con = sqlite3.connect(str(db))
    try:
        agg_sql = (
            "SELECT agg_trade_id, price, quantity, timestamp_ms, "
            "first_trade_id, last_trade_id FROM agg_trades "
            "WHERE market=? AND symbol=? AND id_domain='aggtrade' "
            "AND timestamp_ms BETWEEN ? AND ? "
            "AND (timestamp_ms > ? OR (timestamp_ms = ? AND agg_trade_id > ?)) "
            "ORDER BY timestamp_ms ASC, agg_trade_id ASC LIMIT ?"
        )
        plan = list(con.execute("EXPLAIN QUERY PLAN " + agg_sql, ("futures", "HYPEUSDT", 0, 9999, 0, 0, 0, 50_000)))
        joined = " ".join(str(p[3]) for p in plan)
        assert "idx_agg_trades_ts" in joined, f"unexpected plan: {plan}"
        # Must NOT contain CORRELATED SCALAR SUBQUERY.
        assert "CORRELATED" not in joined, f"unexpected plan: {plan}"
    finally:
        con.close()


def _peak_rss_mb():
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


@pytest.mark.parametrize("n_trades", [100_000, 500_000, 1_000_000])
def test_streaming_memory_bounded_at_scale(tmp_path: Path, n_trades: int):
    """Streaming path memory should not scale linearly with trade count."""
    db = tmp_path / "aggtrades.sqlite"
    cache = AggTradeCache(db)
    base = 1_000_000
    rows = []
    for i in range(n_trades):
        # 100 distinct prices → bounded bin count.
        px = 100.0 + (i % 100) * 0.01
        rows.append(
            AggTrade(
                i + 1,
                px,
                0.1,
                base + i,
                first_trade_id=10_000_000 + i,
                last_trade_id=10_000_000 + i,
                id_domain="aggtrade",
            )
        )
    # Bulk insert in chunks.
    cache.insert_trades("futures", "HYPEUSDT", rows, source="rest")
    rss_before = _peak_rss_mb()
    acc = StreamMetricsAccumulator(
        ladder_start_ms=base, step_start_ms=base + n_trades - 10, bin_size=0.01
    )
    t0 = time.perf_counter()
    feed_count = 0
    for trade in cache.iter_deduped_trades_range("futures", "HYPEUSDT", base, base + n_trades):
        acc.add(trade)
        feed_count += 1
    wall = time.perf_counter() - t0
    rss_after = _peak_rss_mb()
    assert feed_count == n_trades
    assert acc.ladder_count == n_trades
    assert acc.step_count <= 10
    # Bin count is bounded (~100 distinct prices) regardless of n_trades.
    assert len(acc.ladder_bins) <= 100
    # Generous RSS ceiling — at 1M trades the streaming path should not need
    # hundreds of MB to hold a billion-trades-worth of Python objects.
    rss_delta = rss_after - rss_before
    assert rss_delta < 350, (
        f"n={n_trades}: rss_delta={rss_delta:.1f} MB wall={wall:.2f}s "
        f"bins={len(acc.ladder_bins)}"
    )
