from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from goldenfibo.marketdata.aggtrade_cache import AggTradeCache
from goldenfibo.metrics.trade_vap import AggTrade


def test_schema_and_idempotent_insert(tmp_path: Path):
    db = tmp_path / "aggtrades.sqlite"
    cache = AggTradeCache(db)
    assert db.exists()
    n1 = cache.insert_trades(
        "futures",
        "HYPEUSDT",
        [AggTrade(1, 100.0, 1.0, 1000), AggTrade(2, 101.0, 2.0, 2000)],
        source="rest",
    )
    n2 = cache.insert_trades(
        "futures",
        "HYPEUSDT",
        [AggTrade(1, 100.0, 1.0, 1000), AggTrade(2, 101.0, 2.0, 2000)],
        source="rest",
    )
    assert n1 == 2
    assert n2 == 0
    rows = cache.query_trades_range("futures", "HYPEUSDT", 0, 3000)
    assert [t.agg_id for t in rows] == [1, 2]
    first_ts, last_ts, count = cache.query_trades_minmax("futures", "HYPEUSDT")
    assert (first_ts, last_ts, count) == (1000, 2000, 2)


def test_coverage_interval_merge_and_missing_ranges(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    cache.record_coverage("futures", "HYPEUSDT", 1000, 2000, source="archive", note="d1")
    cache.record_coverage("futures", "HYPEUSDT", 2001, 3000, source="archive", note="d2")
    intervals = cache.coverage_intervals("futures", "HYPEUSDT")
    assert len(intervals) == 1
    assert intervals[0].start_ms == 1000 and intervals[0].end_ms == 3000
    assert cache.coverage_covers("futures", "HYPEUSDT", 1000, 3000) is True
    assert cache.missing_ranges("futures", "HYPEUSDT", 500, 3500) == [(500, 999), (3001, 3500)]


def test_archive_url_construction():
    url = AggTradeCache.archive_url_for_day("HYPEUSDT", "2026-09-16", market="futures")
    assert url.endswith("/data/futures/um/daily/aggTrades/HYPEUSDT/HYPEUSDT-aggTrades-2026-09-16.zip")


def test_archive_parsing_and_ws_dedupe(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    cache.insert_trades("futures", "HYPEUSDT", [AggTrade(1, 100.0, 1.0, 1000)], source="archive")
    inserted = cache.ingest_ws_message("futures", "HYPEUSDT", {"a": 1, "p": "100.0", "q": "1.0", "T": 1000})
    assert inserted is False
    inserted2 = cache.ingest_ws_message("futures", "HYPEUSDT", {"a": 2, "p": "101.0", "q": "1.0", "T": 2000})
    assert inserted2 is True
    rows = cache.query_trades_range("futures", "HYPEUSDT", 0, 3000)
    assert [t.agg_id for t in rows] == [1, 2]


def test_unpublished_archive_does_not_mark_coverage(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    def boom(*args, **kwargs):
        raise RuntimeError("404")
    cache._download_archive_zip = boom  # type: ignore[method-assign]
    rows = cache.fetch_archive_range("futures", "HYPEUSDT", 1000, 2000)
    assert rows == []
    assert cache.coverage_intervals("futures", "HYPEUSDT") == []


def test_id_domain_separates_same_numeric_rest_and_raw_ws_ids(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    assert cache.insert_trades(
        "futures",
        "HYPEUSDT",
        [{"a": 42, "p": "100", "q": "1", "f": 9000, "l": 9001, "T": 1000}],
        source="rest",
    ) == 1
    assert cache.ingest_ws_message(
        "futures", "HYPEUSDT", {"e": "trade", "t": 42, "p": "101", "q": "2", "T": 1001}
    ) is True
    assert cache.ingest_ws_message(
        "futures", "HYPEUSDT", {"e": "trade", "t": 42, "p": "101", "q": "2", "T": 1001}
    ) is False
    rows = cache.query_trades_range("futures", "HYPEUSDT", 999, 1002)
    assert len(rows) == 2
    assert sorted((r.id_domain, r.agg_id, r.first_trade_id, r.last_trade_id) for r in rows) == [
        ("aggtrade", 42, 9000, 9001),
        ("trade", -42, 42, 42),
    ]


def test_rest_aggregate_excludes_overlapping_raw_trades_from_metrics(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 100, "p": "10", "q": "1.5", "T": 1000})
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 101, "p": "10", "q": "2.5", "T": 1001})
    raw_only = cache.query_trades_range("futures", "HYPEUSDT", 1000, 1001)
    assert sum(t.qty for t in raw_only) == 4.0

    # REST aggregate covers the same underlying raw trade IDs 100..101.
    cache.insert_trades(
        "futures",
        "HYPEUSDT",
        [{"a": 5000, "p": "10", "q": "4.0", "f": 100, "l": 101, "T": 1001}],
        source="rest",
    )
    rows = cache.query_trades_range("futures", "HYPEUSDT", 1000, 1001)
    assert [(r.id_domain, r.agg_id, r.qty, r.first_trade_id, r.last_trade_id) for r in rows] == [
        ("aggtrade", 5000, 4.0, 100, 101)
    ]
    assert sum(t.qty for t in rows) == 4.0


def test_rest_gap_repair_then_ws_continuation_timestamp_coverage(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    cache.record_coverage("futures", "HYPEUSDT", 1000, 1999, source="rest", note="history")
    cache.record_coverage("futures", "HYPEUSDT", 3000, 3000, source="ws", note="ws-observed")
    assert cache.missing_ranges("futures", "HYPEUSDT", 1000, 3000) == [(2000, 2999)]
    cache.record_coverage("futures", "HYPEUSDT", 2000, 2999, source="rest", note="repair")
    assert cache.missing_ranges("futures", "HYPEUSDT", 1000, 3000) == []


# ---------------------------------------------------------------------------
# Correctness coverage (A..L) for query_trades_range overlap semantics.
# ---------------------------------------------------------------------------


def _vol(rows):
    return sum(r.qty for r in rows)


def _vwap(rows):
    s = sum(r.qty for r in rows)
    if not s:
        return 0.0
    return sum(r.qty * r.price for r in rows) / s


def _vp_notional(rows):
    return sum(r.qty * r.price for r in rows)


def test_overlap_aggregate_only(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    cache.insert_trades(
        "futures",
        "HYPEUSDT",
        [
            {"a": 1, "p": "10", "q": "1.0", "f": 100, "l": 100, "T": 1000},
            {"a": 2, "p": "11", "q": "2.0", "f": 101, "l": 102, "T": 1001},
        ],
        source="rest",
    )
    rows = cache.query_trades_range("futures", "HYPEUSDT", 0, 9999)
    assert all(r.id_domain == "aggtrade" for r in rows)
    assert len(rows) == 2
    assert _vol(rows) == 3.0
    assert abs(_vwap(rows) - ((1.0 * 10 + 2.0 * 11) / 3.0)) < 1e-9
    assert _vp_notional(rows) == 1.0 * 10 + 2.0 * 11


def test_overlap_raw_only(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 200, "p": "5", "q": "0.5", "T": 1000})
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 201, "p": "6", "q": "1.5", "T": 1001})
    rows = cache.query_trades_range("futures", "HYPEUSDT", 0, 9999)
    assert all(r.id_domain == "trade" for r in rows)
    assert len(rows) == 2
    assert _vol(rows) == 2.0
    assert abs(_vwap(rows) - ((0.5 * 5 + 1.5 * 6) / 2.0)) < 1e-9
    assert _vp_notional(rows) == 0.5 * 5 + 1.5 * 6


def test_overlap_exact_full_overlap(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    for tid in (300, 301, 302):
        cache.ingest_ws_message(
            "futures", "HYPEUSDT",
            {"e": "trade", "t": tid, "p": "10", "q": "1.0", "T": 1000 + tid},
        )
    # Aggregate fully covers raw trade IDs 300..302.
    cache.insert_trades(
        "futures", "HYPEUSDT",
        [{"a": 9999, "p": "10", "q": "3.0", "f": 300, "l": 302, "T": 1100}],
        source="rest",
    )
    rows = cache.query_trades_range("futures", "HYPEUSDT", 0, 9999)
    # Aggregate takes precedence: raw rows dropped, volume counted once.
    assert len(rows) == 1
    assert rows[0].id_domain == "aggtrade"
    assert rows[0].qty == 3.0
    assert _vol(rows) == 3.0
    assert _vwap(rows) == 10.0


def test_overlap_aggregate_covers_multiple_raw_trades(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    # 5 raw trades qty=1 each, prices varying; aggregate covers raw IDs 10..14
    for tid, p in zip(range(10, 15), (10, 11, 12, 13, 14)):
        cache.ingest_ws_message(
            "futures", "HYPEUSDT",
            {"e": "trade", "t": tid, "p": str(p), "q": "1.0", "T": 1000 + tid},
        )
    cache.insert_trades(
        "futures", "HYPEUSDT",
        [{"a": 7777, "p": "12", "q": "5.0", "f": 10, "l": 14, "T": 1200}],
        source="rest",
    )
    rows = cache.query_trades_range("futures", "HYPEUSDT", 0, 9999)
    assert len(rows) == 1
    assert rows[0].qty == 5.0
    assert _vwap(rows) == 12.0
    assert _vol(rows) == 5.0


def test_overlap_partial_raw_overlap(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    # raw trades 20..23
    for tid, q in zip(range(20, 24), (1.0, 2.0, 3.0, 4.0)):
        cache.ingest_ws_message(
            "futures", "HYPEUSDT",
            {"e": "trade", "t": tid, "p": "10", "q": str(q), "T": 1000 + tid},
        )
    # aggregate covers only 21..22
    cache.insert_trades(
        "futures", "HYPEUSDT",
        [{"a": 5555, "p": "10", "q": "5.0", "f": 21, "l": 22, "T": 1100}],
        source="rest",
    )
    rows = cache.query_trades_range("futures", "HYPEUSDT", 0, 9999)
    # Expected: aggregate qty=5 covers raw qty 2+3=5; raw 20 (1) and 23 (4) remain.
    assert sum(r.qty for r in rows) == 1.0 + 5.0 + 4.0
    total_notional = 1.0 * 10 + 5.0 * 10 + 4.0 * 10
    assert abs(_vwap(rows) - total_notional / _vol(rows)) < 1e-9
    domains = sorted(r.id_domain for r in rows)
    assert domains.count("aggtrade") == 1
    assert domains.count("trade") == 2


def test_overlap_multiple_aggregate_ranges(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    # raw trades spread across two windows
    for tid, p in zip((100, 101, 102, 200, 201), (10, 10, 10, 10, 10)):
        cache.ingest_ws_message(
            "futures", "HYPEUSDT",
            {"e": "trade", "t": tid, "p": "10", "q": "1.0", "T": 1000 + tid},
        )
    cache.insert_trades(
        "futures", "HYPEUSDT",
        [
            {"a": 1, "p": "10", "q": "2.0", "f": 100, "l": 101, "T": 1100},
            {"a": 2, "p": "10", "q": "1.0", "f": 200, "l": 200, "T": 1300},
        ],
        source="rest",
    )
    rows = cache.query_trades_range("futures", "HYPEUSDT", 0, 9999)
    # aggregates win for 100,101 and 200..200; raw 102 and 201 remain uncovered.
    by_tid_remaining = [r.first_trade_id for r in rows if r.id_domain == "trade"]
    assert sorted(by_tid_remaining) == [102, 201]
    # Economic volume = aggregates (2+1) + raw 102 + raw 201 = 5
    assert abs(_vol(rows) - 5.0) < 1e-9


def test_overlap_same_numeric_id_different_domains(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    cache.insert_trades(
        "futures", "HYPEUSDT",
        [{"a": 500, "p": "10", "q": "1.0", "f": 900, "l": 901, "T": 1000}],
        source="rest",
    )
    # raw trade whose real t=500; numeric clash with aggregate id 500, but
    # raw id stored under first_trade_id.
    cache.ingest_ws_message(
        "futures", "HYPEUSDT",
        {"e": "trade", "t": 500, "p": "11", "q": "2.0", "T": 1500},
    )
    rows = cache.query_trades_range("futures", "HYPEUSDT", 0, 9999)
    # raw trade id 500 is NOT covered by aggregate (aggregate covers 900..901)
    assert _vol(rows) == 1.0 + 2.0
    assert sorted(r.id_domain for r in rows) == ["aggtrade", "trade"]


def test_overlap_duplicate_raw_id(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 700, "p": "10", "q": "1.0", "T": 1000})
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 700, "p": "10", "q": "1.0", "T": 1000})
    rows = cache.query_trades_range("futures", "HYPEUSDT", 0, 9999)
    # PK dedupe: raw insert_trades path used by ingest_ws_message must dedupe.
    assert len(rows) == 1
    assert rows[0].qty == 1.0


def test_overlap_duplicate_aggregate_id(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    cache.insert_trades(
        "futures", "HYPEUSDT",
        [{"a": 42, "p": "10", "q": "1.0", "f": 1, "l": 1, "T": 1000}],
        source="rest",
    )
    cache.insert_trades(
        "futures", "HYPEUSDT",
        [{"a": 42, "p": "10", "q": "1.0", "f": 1, "l": 1, "T": 1000}],
        source="rest",
    )
    rows = cache.query_trades_range("futures", "HYPEUSDT", 0, 9999)
    assert len(rows) == 1
    assert rows[0].qty == 1.0


def test_overlap_start_end_time_boundary(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    # raw trade exactly on boundary, aggregate whose interval is outside
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 800, "p": "10", "q": "1.0", "T": 1000})
    cache.insert_trades(
        "futures", "HYPEUSDT",
        [
            {"a": 11, "p": "20", "q": "5.0", "f": 900, "l": 905, "T": 999},
            {"a": 12, "p": "20", "q": "5.0", "f": 900, "l": 905, "T": 1001},
        ],
        source="rest",
    )
    # Aggregate at t=999 must NOT cover raw trade at t=1000. Containment is
    # by underlying trade ID, not by timestamp.
    rows = cache.query_trades_range("futures", "HYPEUSDT", 1000, 1000)
    assert len(rows) == 1
    assert rows[0].id_domain == "trade"
    assert rows[0].qty == 1.0
    # When window includes both, raw still remains (its ID 800 is uncovered).
    rows2 = cache.query_trades_range("futures", "HYPEUSDT", 999, 1001)
    # Aggregate rows count their own quantity, raw t=800 remains.
    assert sum(r.qty for r in rows2) == 5.0 + 5.0 + 1.0


def test_overlap_rest_repair_then_ws_continuation(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    # raw trades before repair window (WS continuation)
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 1000, "p": "10", "q": "1.0", "T": 1000})
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 1001, "p": "10", "q": "2.0", "T": 1001})
    # REST repair covers the same underlying IDs in a later row.
    cache.insert_trades(
        "futures", "HYPEUSDT",
        [{"a": 99999, "p": "10", "q": "3.0", "f": 1000, "l": 1001, "T": 1500}],
        source="rest",
    )
    rows = cache.query_trades_range("futures", "HYPEUSDT", 0, 9999)
    # Aggregate covers both raws; volume counted exactly once.
    assert len(rows) == 1
    assert rows[0].id_domain == "aggtrade"
    assert _vol(rows) == 3.0


def test_overlap_zero_trade_ws_interval(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    # No rows at all in this window; just a verified zero-trade coverage entry.
    cache.record_coverage("futures", "HYPEUSDT", 5000, 5999, source="ws", note="ws-observed")
    rows = cache.query_trades_range("futures", "HYPEUSDT", 5000, 5999)
    assert rows == []


# ---------------------------------------------------------------------------
# Performance regression (structural, not flaky).
# ---------------------------------------------------------------------------


def test_perf_no_correlated_query_and_bounded_time(tmp_path: Path):
    """Many aggregate rows + thousands of raw WS rows + overlap. Must not
    regress into a per-row correlated scan."""
    db = tmp_path / "aggtrades.sqlite"
    cache = AggTradeCache(db)
    # Insert 50k aggregate rows (typical mid-window chunk).
    A = 50_000
    base = 1_000_000
    t = base
    agg_rows = []
    for i in range(A):
        f = 10_000_000 + i * 5  # raw IDs in 5-id bundles per aggregate
        agg_rows.append({
            "a": 1_000_000_000 + i,
            "p": "100",
            "q": "5.0",
            "f": f,
            "l": f + 4,
            "T": t,
        })
        t += 1
    assert cache.insert_trades("futures", "HYPEUSDT", agg_rows, source="rest") == A

    # Insert 2000 raw WS rows; half are covered by aggregates, half are not.
    raw_rows = []
    for i in range(1000):
        # covered: pick a raw ID inside an aggregate range
        agg_idx = i * 10
        raw_tid = 10_000_000 + agg_idx * 5 + 2  # inside agg_idx range
        raw_rows.append({"e": "trade", "t": raw_tid, "p": "100", "q": "1.0", "T": t})
        t += 1
    for i in range(1000):
        # uncovered: pick a raw ID past the highest aggregate ID
        raw_tid = 100_000_000 + i
        raw_rows.append({"e": "trade", "t": raw_tid, "p": "100", "q": "1.0", "T": t})
        t += 1
    for r in raw_rows:
        cache.ingest_ws_message("futures", "HYPEUSDT", r)

    t0 = time.perf_counter()
    rows = cache.query_trades_range("futures", "HYPEUSDT", base - 1, t + 10)
    elapsed = time.perf_counter() - t0

    # No double-counting: aggregate qty covers 5 raw * 1 each, plus the 1000
    # uncovered raw rows. So volume = A*5 (aggregate) + 1000 (raw) = 255000.
    assert _vol(rows) == A * 5.0 + 1000.0
    # Structural: should not return more than A aggregate rows.
    assert sum(1 for r in rows if r.id_domain == "aggtrade") == A
    # Generous ceiling (avoids flake on slow CI).
    assert elapsed < 5.0, f"query took {elapsed:.2f}s"

    # Verify the SQL retrieval has no correlated subquery scanning aggregates.
    conn = sqlite3.connect(str(db))
    try:
        for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_agg_trades_ts'"
        ):
            assert row[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SQL aggregate aggressive-flow metrics must match canonical query semantics
# without materializing every trade in Python.
# ---------------------------------------------------------------------------


def _expected_aggressive(rows):
    buy_qty = sell_qty = buy_notional = sell_notional = 0.0
    for r in rows:
        qty = float(r.qty)
        notional = float(r.price) * qty
        if bool(r.buyer_is_maker):
            sell_qty += qty
            sell_notional += notional
        else:
            buy_qty += qty
            buy_notional += notional
    total = buy_qty + sell_qty
    return {
        "buy_qty": buy_qty,
        "buy_notional": buy_notional,
        "buy_vwap": buy_notional / buy_qty if buy_qty else None,
        "sell_qty": sell_qty,
        "sell_notional": sell_notional,
        "sell_vwap": sell_notional / sell_qty if sell_qty else None,
        "total_qty": total,
        "delta_ratio": (buy_qty - sell_qty) / total if total else None,
        "trade_count": len(rows),
    }


def _assert_metrics_close(actual, expected, *, tol=1e-12):
    for key, want in expected.items():
        got = actual[key]
        if want is None:
            assert got is None, key
        else:
            assert got == pytest.approx(want, abs=tol, rel=tol), key


def test_aggregate_aggressive_metrics_buy_only_sell_only_mixed_and_boundaries(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    cache.insert_trades(
        "futures",
        "HYPEUSDT",
        [
            {"a": 1, "p": "10.5", "q": "2.0", "f": 1, "l": 1, "T": 1000, "m": False},
            {"a": 2, "p": "11.5", "q": "3.0", "f": 2, "l": 2, "T": 2000, "m": False},
            {"a": 3, "p": "12.5", "q": "4.0", "f": 3, "l": 3, "T": 3000, "m": True},
            {"a": 4, "p": "13.5", "q": "5.0", "f": 4, "l": 4, "T": 4000, "m": True},
        ],
        source="archive",
    )

    _assert_metrics_close(
        cache.aggregate_aggressive_metrics("futures", "HYPEUSDT", 1000, 2000),
        _expected_aggressive(cache.query_trades_range("futures", "HYPEUSDT", 1000, 2000)),
    )
    _assert_metrics_close(
        cache.aggregate_aggressive_metrics("futures", "HYPEUSDT", 3000, 4000),
        _expected_aggressive(cache.query_trades_range("futures", "HYPEUSDT", 3000, 4000)),
    )
    _assert_metrics_close(
        cache.aggregate_aggressive_metrics("futures", "HYPEUSDT", 2000, 3000),
        _expected_aggressive(cache.query_trades_range("futures", "HYPEUSDT", 2000, 3000)),
    )


def test_aggregate_aggressive_metrics_empty_interval(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    actual = cache.aggregate_aggressive_metrics("futures", "HYPEUSDT", 5000, 5999)
    assert actual["buy_qty"] == 0.0
    assert actual["sell_qty"] == 0.0
    assert actual["total_qty"] == 0.0
    assert actual["buy_vwap"] is None
    assert actual["sell_vwap"] is None
    assert actual["delta_ratio"] is None
    assert actual["trade_count"] == 0


def test_aggregate_aggressive_metrics_matches_old_query_with_overlap_semantics(tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 100, "p": "10", "q": "1", "T": 1000, "m": False})
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 101, "p": "10", "q": "2", "T": 1001, "m": True})
    cache.ingest_ws_message("futures", "HYPEUSDT", {"e": "trade", "t": 999, "p": "20", "q": "3", "T": 1002, "m": False})
    cache.insert_trades(
        "futures",
        "HYPEUSDT",
        [{"a": 500, "p": "10", "q": "3", "f": 100, "l": 101, "T": 1001, "m": True}],
        source="rest",
    )
    rows = cache.query_trades_range("futures", "HYPEUSDT", 1000, 1002)
    assert sorted((r.id_domain, r.first_trade_id, r.last_trade_id) for r in rows) == [
        ("aggtrade", 100, 101),
        ("trade", 999, 999),
    ]
    _assert_metrics_close(
        cache.aggregate_aggressive_metrics("futures", "HYPEUSDT", 1000, 1002),
        _expected_aggressive(rows),
    )


def test_aggregate_aggressive_metrics_large_dataset_does_not_call_query_trades_range(monkeypatch, tmp_path: Path):
    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    rows = [
        {"a": i, "p": "100.25", "q": "0.5", "f": i, "l": i, "T": 1_000_000 + i, "m": bool(i % 2)}
        for i in range(20_000)
    ]
    assert cache.insert_trades("futures", "HYPEUSDT", rows, source="archive") == 20_000

    def forbidden(*args, **kwargs):
        raise AssertionError("query_trades_range must not be used by SQL aggregation")

    monkeypatch.setattr(cache, "query_trades_range", forbidden)
    actual = cache.aggregate_aggressive_metrics("futures", "HYPEUSDT", 1_000_000, 1_020_000)
    assert actual["trade_count"] == 20_000
    assert actual["buy_qty"] == pytest.approx(5_000.0)
    assert actual["sell_qty"] == pytest.approx(5_000.0)
    assert actual["delta_ratio"] == pytest.approx(0.0)
