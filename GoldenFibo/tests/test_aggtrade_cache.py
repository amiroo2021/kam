from __future__ import annotations

from pathlib import Path

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
