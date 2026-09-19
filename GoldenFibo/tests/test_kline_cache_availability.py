from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from goldenfibo.marketdata.kline_cache import CachePolicy, KlineCache, fetch_range_cached


def _k(ts, o="100", h="101", l="99", c="100.5", v="1.5", q="150.75"):
    return [ts, o, h, l, c, v, ts + 59_999, q, 10, "0.7", "70.0", "0"]


@pytest.fixture
def cache(tmp_path):
    return KlineCache(tmp_path / "t.sqlite")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import urllib.request

    def _boom(*args, **kwargs):
        raise AssertionError("unexpected external HTTP request")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)


class _FakeDiscovery:
    def __init__(self, first_available: int):
        self.first_available = first_available
        self.calls = []

    def __call__(self, symbol, timeframe, *, market, fetch=None, base_url=None, max_years_back=10):
        self.calls.append((symbol, timeframe, market, base_url, max_years_back))
        return self.first_available


class _FakeFetch:
    def __init__(self, rows):
        self.rows = rows
        self.urls = []

    def __call__(self, url: str):
        self.urls.append(url)
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(url).query)
        st = int(q["startTime"][0])
        et = int(q["endTime"][0])
        lim = int(q["limit"][0])
        return [r for r in self.rows if st <= r[0] <= et][:lim]


def test_boundary_storage_and_restart_persistence(tmp_path):
    cache = KlineCache(tmp_path / "t.sqlite")
    cache.set_availability("HYPEUSDT", "1m", market="futures", first_available_open_time=1_708_025_800_000)
    del cache
    reopened = KlineCache(tmp_path / "t.sqlite")
    avail = reopened.get_availability("HYPEUSDT", "1m", market="futures")
    assert avail["first_available_open_time"] == 1_708_025_800_000


def test_boundaries_are_market_and_symbol_independent(tmp_path):
    cache = KlineCache(tmp_path / "t.sqlite")
    cache.set_availability("HYPEUSDT", "1m", market="futures", first_available_open_time=1)
    cache.set_availability("HYPEUSDT", "1m", market="spot", first_available_open_time=2)
    cache.set_availability("BTCUSDT", "1m", market="futures", first_available_open_time=3)
    assert cache.get_availability("HYPEUSDT", "1m", market="futures")["first_available_open_time"] == 1
    assert cache.get_availability("HYPEUSDT", "1m", market="spot")["first_available_open_time"] == 2
    assert cache.get_availability("BTCUSDT", "1m", market="futures")["first_available_open_time"] == 3


def test_discovery_persists_first_available_without_network(cache):
    first = 1_717_064_200_000
    rows = [_k(first + i * 60_000) for i in range(3)]
    discovery = _FakeFetch(rows)
    found = cache.discover_first_available_open_time("HYPEUSDT", "1m", market="futures", fetch=discovery)
    assert found == first
    assert len(discovery.urls) == 1
    assert "/fapi/v1/klines?" in discovery.urls[0]
    avail = cache.get_availability("HYPEUSDT", "1m", market="futures")
    assert avail["first_available_open_time"] == first


def test_requested_start_before_listing_uses_effective_start(cache, monkeypatch):
    first = 1_717_064_200_000
    end = first + 10 * 60_000
    cache.set_availability("HYPEUSDT", "1m", market="futures", first_available_open_time=first)
    rows = [_k(first + i * 60_000) for i in range(10)]
    cache.upsert_klines("HYPEUSDT", "1m", rows, market="futures")
    discovery = _FakeDiscovery(first)
    monkeypatch.setattr(KlineCache, "discover_first_available_open_time", discovery)
    res = fetch_range_cached(
        "HYPEUSDT",
        "1m",
        first - 365 * 24 * 60 * 60 * 1000,
        end,
        cache=cache,
        policy=CachePolicy.AUTO,
        market="futures",
        fetch=lambda url: (_ for _ in ()).throw(AssertionError(f"network should not be used: {url}")),
        sleep_s=0,
        refresh_tail_ms=0,
    )
    assert len(res.klines) == 10
    assert res.stats.bars_from_cache == 10
    assert res.stats.gaps_remaining == 0
    assert discovery.calls == []
    assert cache.read_range("HYPEUSDT", "1m", first - 365 * 24 * 60 * 60 * 1000, first, market="futures") == []


def test_exact_first_available_and_after_first_available(cache, monkeypatch):
    first = 1_717_064_200_000
    cache.set_availability("HYPEUSDT", "1m", market="futures", first_available_open_time=first)
    rows = [_k(first + i * 60_000) for i in range(5)]
    cache.upsert_klines("HYPEUSDT", "1m", rows, market="futures")
    discovery = _FakeDiscovery(first)
    monkeypatch.setattr(KlineCache, "discover_first_available_open_time", discovery)
    res = fetch_range_cached(
        "HYPEUSDT",
        "1m",
        first,
        first + 5 * 60_000,
        cache=cache,
        policy=CachePolicy.AUTO,
        market="futures",
        fetch=lambda url: (_ for _ in ()).throw(AssertionError(f"network should not be used: {url}")),
        sleep_s=0,
        refresh_tail_ms=0,
    )
    assert len(res.klines) == 5
    assert res.stats.bars_from_cache == 5
    assert discovery.calls == []


def test_genuine_internal_gap_after_listing(cache):
    first = 1_717_064_200_000
    cache.set_availability("HYPEUSDT", "1m", market="futures", first_available_open_time=first)
    rows = [_k(first + i * 60_000) for i in [0, 1, 2, 5, 6]]
    cache.upsert_klines("HYPEUSDT", "1m", rows, market="futures")
    missing = cache.find_missing_ranges("HYPEUSDT", "1m", first, first + 7 * 60_000, market="futures")
    assert missing == [(first + 3 * 60_000, first + 5 * 60_000)]


def test_multiple_internal_gaps_after_listing(cache):
    first = 1_717_064_200_000
    cache.set_availability("HYPEUSDT", "1m", market="futures", first_available_open_time=first)
    rows = [_k(first + i * 60_000) for i in [0, 1, 4, 5, 8, 9]]
    cache.upsert_klines("HYPEUSDT", "1m", rows, market="futures")
    missing = cache.find_missing_ranges("HYPEUSDT", "1m", first, first + 10 * 60_000, market="futures")
    assert missing == [
        (first + 2 * 60_000, first + 4 * 60_000),
        (first + 6 * 60_000, first + 8 * 60_000),
    ]


def test_percentage_does_not_affect_cache_identity(cache):
    first = 1_717_064_200_000
    cache.set_availability("HYPEUSDT", "1m", market="futures", first_available_open_time=first)
    assert cache.get_availability("HYPEUSDT", "1m", market="futures")["first_available_open_time"] == first


def test_no_fake_prelisting_rows_inserted(cache):
    first = 1_717_064_200_000
    cache.set_availability("HYPEUSDT", "1m", market="futures", first_available_open_time=first)
    cache.upsert_klines("HYPEUSDT", "1m", [_k(first)], market="futures")
    con = sqlite3.connect(cache.path)
    rows = con.execute(
        "SELECT open_time FROM candles WHERE source='binance' AND market='futures' AND symbol='HYPEUSDT' AND timeframe='1m' ORDER BY open_time"
    ).fetchall()
    assert rows == [(first,)]


def test_now_does_not_count_forming_candle_as_historical_missing(cache, monkeypatch):
    first = 1_717_064_200_000
    cache.set_availability("HYPEUSDT", "1m", market="futures", first_available_open_time=first)
    rows = [_k(first + i * 60_000) for i in range(10)]
    cache.upsert_klines("HYPEUSDT", "1m", rows, market="futures")
    discovery = _FakeDiscovery(first)
    monkeypatch.setattr(KlineCache, "discover_first_available_open_time", discovery)
    res = fetch_range_cached(
        "HYPEUSDT",
        "1m",
        first,
        first + 10 * 60_000 + 15_000,
        cache=cache,
        policy=CachePolicy.AUTO,
        market="futures",
        fetch=lambda url: (_ for _ in ()).throw(AssertionError(f"network should not be used: {url}")),
        sleep_s=0,
        closed_only_before_ms=first + 10 * 60_000,
        refresh_tail_ms=0,
    )
    assert len(res.klines) == 10
    assert res.stats.gaps_remaining == 0
    assert discovery.calls == []
