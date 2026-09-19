from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from goldenfibo.engine.config import EngineConfig, OhlcResolveMode, Side
from goldenfibo.engine.engine import GoldenFiboEngine
from goldenfibo.marketdata.kline_cache import CachePolicy, KlineCache, fetch_range_cached
from goldenfibo.metrics import OhlcvBar, build_volume_profile, ladder_vwap, step_vwap
from goldenfibo.session.runner import run_ohlc_on_engine


def _k(ts, o="100", h="101", l="99", c="100.5", v="1.5", q="150.75"):
    return [ts, o, h, l, c, v, ts + 59_999, q, 10, "0.7", "70.0", "0"]


@pytest.fixture
def tmp_cache(tmp_path):
    return KlineCache(tmp_path / "t.sqlite")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import urllib.request

    def _boom(*args, **kwargs):
        raise AssertionError("unexpected network request")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)


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


class _FakeDiscovery:
    def __init__(self, first_available: int):
        self.first_available = first_available
        self.calls = []

    def __call__(self, symbol, timeframe, *, market, fetch=None, base_url=None, max_years_back=10):
        self.calls.append((symbol, timeframe, market, base_url, max_years_back))
        return self.first_available


class _CountedFetch:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0
        self.urls = []

    def __call__(self, url: str):
        self.calls += 1
        self.urls.append(url)
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(url).query)
        st = int(q["startTime"][0])
        et = int(q["endTime"][0])
        lim = int(q["limit"][0])
        return [r for r in self.rows if st <= r[0] <= et][:lim]


@pytest.fixture
def shared_rows():
    t0 = 1_717_064_200_000
    return t0, [_k(t0 + i * 60_000) for i in range(16)]


def _engine_snapshot(klines):
    cfg = EngineConfig(side=Side.SELL, percentage=Decimal("0.001"), symbol="BTCUSDT")
    eng = GoldenFiboEngine(cfg)
    run_ohlc_on_engine(eng, klines, mode=OhlcResolveMode.LEGACY)
    st = eng.state
    bars = [OhlcvBar(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), float(k[7])) for k in klines]
    prof = build_volume_profile(bars, bars[0].ts_ms) if bars else None
    return {
        "p0": str(st.p0) if st.p0 is not None else None,
        "current_step": st.highest_filled,
        "tp": str(st.shared_tp) if st.shared_tp is not None else None,
        "next_p": str(st.next_p()) if st.next_p() is not None else None,
        "further_p": str(st.further_p()) if st.further_p() is not None else None,
        "closed_count": len(st.closed),
        "ladder_vwap": ladder_vwap(bars, bars[0].ts_ms) if bars else None,
        "step_vwap": step_vwap(bars, bars[0].ts_ms) if bars else None,
        "ladder_poc": None if prof is None else prof.poc,
        "step_poc": None if prof is None else prof.poc,
        "ladder_val": None if prof is None else prof.val,
        "ladder_vah": None if prof is None else prof.vah,
        "last_close": str(klines[-1][4]) if klines else None,
    }


def test_default_cache_path_is_backtest_klines():
    cache = KlineCache()
    assert str(cache.path).endswith("/data/backtest_klines.sqlite")


def test_explicit_cache_path_still_overrides_default(tmp_path):
    custom = tmp_path / "custom.sqlite"
    cache = KlineCache(custom)
    assert cache.path == custom


def test_cache_reuse_telegram_then_web_same_range(tmp_cache, shared_rows, monkeypatch):
    start, rows = shared_rows
    end = start + 16 * 60_000
    discovery = _FakeDiscovery(start)
    monkeypatch.setattr(KlineCache, "discover_first_available_open_time", discovery)

    telegram_fetch = _CountedFetch(rows)
    web_fetch = _CountedFetch(rows)

    telegram = fetch_range_cached(
        "BTCUSDT",
        "1m",
        start,
        end,
        cache=tmp_cache,
        policy=CachePolicy.AUTO,
        market="futures",
        fetch=telegram_fetch,
        sleep_s=0,
        refresh_tail_ms=0,
        closed_only_before_ms=end,
    )
    assert telegram_fetch.calls > 0

    web = fetch_range_cached(
        "BTCUSDT",
        "1m",
        start,
        end,
        cache=tmp_cache,
        policy=CachePolicy.AUTO,
        market="futures",
        fetch=web_fetch,
        sleep_s=0,
        refresh_tail_ms=0,
        closed_only_before_ms=end,
    )
    assert web_fetch.calls == 0
    assert [k[0] for k in web.klines] == [k[0] for k in telegram.klines]


def test_cache_reuse_web_then_telegram_same_range(tmp_cache, shared_rows, monkeypatch):
    start, rows = shared_rows
    end = start + 16 * 60_000
    discovery = _FakeDiscovery(start)
    monkeypatch.setattr(KlineCache, "discover_first_available_open_time", discovery)

    web_fetch = _CountedFetch(rows)
    telegram_fetch = _CountedFetch(rows)

    web = fetch_range_cached(
        "BTCUSDT",
        "1m",
        start,
        end,
        cache=tmp_cache,
        policy=CachePolicy.AUTO,
        market="futures",
        fetch=web_fetch,
        sleep_s=0,
        refresh_tail_ms=0,
        closed_only_before_ms=end,
    )
    assert web_fetch.calls > 0

    telegram = fetch_range_cached(
        "BTCUSDT",
        "1m",
        start,
        end,
        cache=tmp_cache,
        policy=CachePolicy.AUTO,
        market="futures",
        fetch=telegram_fetch,
        sleep_s=0,
        refresh_tail_ms=0,
        closed_only_before_ms=end,
    )
    assert telegram_fetch.calls == 0
    assert [k[0] for k in telegram.klines] == [k[0] for k in web.klines]


def test_percentage_and_direction_do_not_affect_cache_identity(tmp_cache, shared_rows, monkeypatch):
    start, rows = shared_rows
    end = start + 16 * 60_000
    monkeypatch.setattr(KlineCache, "discover_first_available_open_time", _FakeDiscovery(start))
    f = _CountedFetch(rows)

    a = fetch_range_cached("BTCUSDT", "1m", start, end, cache=tmp_cache, policy=CachePolicy.AUTO, market="futures", fetch=f, sleep_s=0, refresh_tail_ms=0, closed_only_before_ms=end)
    b = fetch_range_cached("BTCUSDT", "1m", start, end, cache=tmp_cache, policy=CachePolicy.AUTO, market="futures", fetch=f, sleep_s=0, refresh_tail_ms=0, closed_only_before_ms=end)
    c = fetch_range_cached("BTCUSDT", "1m", start, end, cache=tmp_cache, policy=CachePolicy.AUTO, market="futures", fetch=f, sleep_s=0, refresh_tail_ms=0, closed_only_before_ms=end)
    assert len(a.klines) == len(b.klines) == len(c.klines) == 16


def test_spot_and_futures_are_isolated(tmp_cache, shared_rows, monkeypatch):
    start, rows = shared_rows
    end = start + 16 * 60_000
    monkeypatch.setattr(KlineCache, "discover_first_available_open_time", _FakeDiscovery(start))
    fetch = _CountedFetch(rows)
    fetch_range_cached("BTCUSDT", "1m", start, end, cache=tmp_cache, policy=CachePolicy.AUTO, market="futures", fetch=fetch, sleep_s=0, refresh_tail_ms=0, closed_only_before_ms=end)
    spot_before = fetch.calls
    fetch_range_cached("BTCUSDT", "1m", start, end, cache=tmp_cache, policy=CachePolicy.AUTO, market="spot", fetch=fetch, sleep_s=0, refresh_tail_ms=0, closed_only_before_ms=end)
    assert fetch.calls > spot_before
    assert tmp_cache.stats_for("BTCUSDT", "1m", market="futures")["bars"] == 16
    assert tmp_cache.stats_for("BTCUSDT", "1m", market="spot")["bars"] == 16


def test_hype_prelisting_boundary_uses_unavailable_not_repeated_downloads(tmp_cache, monkeypatch):
    first = 1_717_064_200_000
    end = first + 10 * 60_000
    discovery = _FakeDiscovery(first)
    monkeypatch.setattr(KlineCache, "discover_first_available_open_time", discovery)
    rows = [_k(first + i * 60_000) for i in range(10)]
    fetch = _CountedFetch(rows)
    res = fetch_range_cached(
        "HYPEUSDT",
        "1m",
        first - 365 * 24 * 60 * 60 * 1000,
        end,
        cache=tmp_cache,
        policy=CachePolicy.AUTO,
        market="futures",
        fetch=fetch,
        sleep_s=0,
        refresh_tail_ms=0,
        closed_only_before_ms=end,
    )
    assert discovery.calls
    assert len(res.klines) == 10
    assert tmp_cache.read_range("HYPEUSDT", "1m", first - 365 * 24 * 60 * 60 * 1000, first, market="futures") == []


def test_internal_gap_still_downloads(tmp_cache, shared_rows, monkeypatch):
    start, rows = shared_rows
    end = start + 16 * 60_000
    monkeypatch.setattr(KlineCache, "discover_first_available_open_time", _FakeDiscovery(start))
    # Seed with a gap: remove 4..6
    seed = [r for r in rows if int(r[0]) not in {start + 4 * 60_000, start + 5 * 60_000, start + 6 * 60_000}]
    tmp_cache.upsert_klines("BTCUSDT", "1m", seed, market="futures")
    fetch = _CountedFetch(rows)
    res = fetch_range_cached("BTCUSDT", "1m", start, end, cache=tmp_cache, policy=CachePolicy.AUTO, market="futures", fetch=fetch, sleep_s=0, refresh_tail_ms=0, closed_only_before_ms=end)
    assert fetch.calls > 0
    assert len(res.klines) == 16
    assert tmp_cache.find_missing_ranges("BTCUSDT", "1m", start, end, market="futures") == []


def test_forming_candle_excluded(tmp_cache, shared_rows, monkeypatch):
    start, rows = shared_rows
    end = start + 16 * 60_000 + 15_000
    monkeypatch.setattr(KlineCache, "discover_first_available_open_time", _FakeDiscovery(start))
    fetch = _CountedFetch(rows)
    res = fetch_range_cached("BTCUSDT", "1m", start, end, cache=tmp_cache, policy=CachePolicy.AUTO, market="futures", fetch=fetch, sleep_s=0, refresh_tail_ms=0, closed_only_before_ms=start + 16 * 60_000)
    assert len(res.klines) == 16
    assert all(int(k[0]) < start + 16 * 60_000 for k in res.klines)


def test_duplicate_inserts_do_not_create_duplicate_rows(tmp_cache, shared_rows):
    start, rows = shared_rows
    tmp_cache.upsert_klines("BTCUSDT", "1m", rows, market="futures")
    tmp_cache.upsert_klines("BTCUSDT", "1m", rows, market="futures")
    assert len(tmp_cache.read_range("BTCUSDT", "1m", start, start + 16 * 60_000, market="futures")) == 16


def test_independent_readers_can_share_cache(tmp_cache, shared_rows, monkeypatch):
    start, rows = shared_rows
    end = start + 16 * 60_000
    monkeypatch.setattr(KlineCache, "discover_first_available_open_time", _FakeDiscovery(start))
    fetch = _CountedFetch(rows)
    fetch_range_cached("BTCUSDT", "1m", start, end, cache=tmp_cache, policy=CachePolicy.AUTO, market="futures", fetch=fetch, sleep_s=0, refresh_tail_ms=0, closed_only_before_ms=end)
    con1 = sqlite3.connect(str(tmp_cache.path))
    con2 = sqlite3.connect(str(tmp_cache.path))
    try:
        assert con1.execute("select count(*) from candles").fetchone()[0] == 16
        assert con2.execute("select count(*) from candles").fetchone()[0] == 16
    finally:
        con1.close(); con2.close()


def test_backtest_and_web_backtest_same_engine_snapshot(tmp_cache, shared_rows, monkeypatch):
    start, rows = shared_rows
    end = start + 16 * 60_000
    monkeypatch.setattr(KlineCache, "discover_first_available_open_time", _FakeDiscovery(start))
    fetch = _CountedFetch(rows)
    a = fetch_range_cached("BTCUSDT", "1m", start, end, cache=tmp_cache, policy=CachePolicy.AUTO, market="futures", fetch=fetch, sleep_s=0, refresh_tail_ms=0, closed_only_before_ms=end)
    b = fetch_range_cached("BTCUSDT", "1m", start, end, cache=tmp_cache, policy=CachePolicy.AUTO, market="futures", fetch=fetch, sleep_s=0, refresh_tail_ms=0, closed_only_before_ms=end)
    snap_a = _engine_snapshot(a.klines)
    snap_b = _engine_snapshot(b.klines)
    assert snap_a == snap_b
