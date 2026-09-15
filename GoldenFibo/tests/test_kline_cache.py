"""Local Binance kline SQLite cache."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from goldenfibo import EngineConfig, GoldenFiboEngine, Side
from goldenfibo.engine.config import OhlcResolveMode
from goldenfibo.marketdata.kline_cache import (
    CachePolicy,
    KlineCache,
    fetch_range_cached,
    merge_klines,
    validate_klines_sequence,
)
from goldenfibo.marketdata.timeframes import interval_ms
from goldenfibo.session.runner import run_ohlc_on_engine
from goldenfibo.api.schemas import levels_for_render, build_state_payload
from goldenfibo.metrics import bar_from_binance_kline, metrics_for_legs


def _k(ts, o="100", h="101", l="99", c="100.5", v="1.5", q="150.75"):
    return [ts, o, h, l, c, v, ts + 59_999, q, 10, "0.7", "70.0", "0"]


@pytest.fixture
def cache(tmp_path):
    return KlineCache(tmp_path / "t.sqlite")


def test_empty_cache_and_upsert(cache):
    t0 = 1_700_000_000_000
    step = 60_000
    assert cache.read_range("BTCUSDT", "1m", t0, t0 + 10 * step) == []
    rows = [_k(t0 + i * step) for i in range(5)]
    assert cache.upsert_klines("BTCUSDT", "1m", rows) == 5
    got = cache.read_range("BTCUSDT", "1m", t0, t0 + 5 * step)
    assert len(got) == 5
    assert got[0][1] == "100"  # precision preserved as string


def test_duplicate_prevention(cache):
    t0 = 1_700_000_000_000
    cache.upsert_klines("BTCUSDT", "1m", [_k(t0, o="1")])
    cache.upsert_klines("BTCUSDT", "1m", [_k(t0, o="2")])  # refresh
    got = cache.read_range("BTCUSDT", "1m", t0, t0 + 60_000)
    assert len(got) == 1
    assert got[0][1] == "2"


def test_gap_detection_partial_begin_end_internal(cache):
    t0 = 1_700_000_000_000
    step = 60_000
    # store 0..2 and 5..7 — gap 3..4, missing end 8..9 relative to range 0..10
    rows = [_k(t0 + i * step) for i in list(range(0, 3)) + list(range(5, 8))]
    cache.upsert_klines("BTCUSDT", "1m", rows)
    missing = cache.find_missing_ranges("BTCUSDT", "1m", t0, t0 + 10 * step)
    # expected missing: 3-5, 8-10 (and nothing at begin)
    flat = []
    for a, b in missing:
        t = a
        while t < b:
            flat.append(t)
            t += step
    assert t0 + 3 * step in flat
    assert t0 + 4 * step in flat
    assert t0 + 8 * step in flat
    assert t0 + 9 * step in flat
    assert t0 not in flat


def test_full_cache_hit_no_download(cache):
    t0 = 1_700_000_000_000
    step = 60_000
    rows = [_k(t0 + i * step) for i in range(20)]
    cache.upsert_klines("BTCUSDT", "1m", rows)
    calls = []

    def fetch(url: str):
        calls.append(url)
        raise AssertionError("network should not be called on pure hit with refresh_tail=0")

    res = fetch_range_cached(
        "BTCUSDT",
        "1m",
        t0,
        t0 + 20 * step,
        cache=cache,
        policy=CachePolicy.AUTO,
        refresh_tail_ms=0,
        fetch=fetch,
        closed_only_before_ms=t0 + 20 * step,
    )
    assert len(res.klines) == 20
    assert res.stats.rest_pages == 0
    assert calls == []


def test_partial_end_downloads_only_missing(cache):
    t0 = 1_700_000_000_000
    step = 60_000
    cache.upsert_klines("BTCUSDT", "1m", [_k(t0 + i * step) for i in range(10)])
    all_rows = {t0 + i * step: _k(t0 + i * step, o=str(i)) for i in range(20)}

    def fetch(url: str):
        import urllib.parse
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        st, et = int(q["startTime"][0]), int(q["endTime"][0])
        lim = int(q["limit"][0])
        return [all_rows[t] for t in sorted(all_rows) if st <= t <= et][:lim]

    res = fetch_range_cached(
        "BTCUSDT",
        "1m",
        t0,
        t0 + 20 * step,
        cache=cache,
        policy=CachePolicy.AUTO,
        refresh_tail_ms=0,
        fetch=fetch,
        sleep_s=0,
        closed_only_before_ms=t0 + 20 * step,
    )
    assert len(res.klines) == 20
    assert res.stats.bars_downloaded >= 10
    assert res.stats.rest_pages >= 1


def test_bypass_does_not_write(cache, tmp_path):
    t0 = 1_700_000_000_000
    step = 60_000
    rows = [_k(t0 + i * step) for i in range(5)]

    def fetch(url: str):
        import urllib.parse
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        st, et = int(q["startTime"][0]), int(q["endTime"][0])
        return [r for r in rows if st <= r[0] <= et]

    res = fetch_range_cached(
        "BTCUSDT",
        "1m",
        t0,
        t0 + 5 * step,
        cache=cache,
        policy=CachePolicy.BYPASS,
        fetch=fetch,
        sleep_s=0,
    )
    assert len(res.klines) == 5
    assert cache.read_range("BTCUSDT", "1m", t0, t0 + 5 * step) == []


def test_open_candle_not_cached_as_final(cache):
    t0 = 1_700_000_000_000
    step = 60_000
    # bars 0..4 available; closed_only_before = t0+4*step means open of 3 is last closed? 
    # closed if ot+step <= closed_only → last closed open = closed_only - step
    fence = t0 + 4 * step  # forming starts at open t0+4*step if now=fence
    rows = [_k(t0 + i * step) for i in range(5)]

    def fetch(url: str):
        import urllib.parse
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        st, et = int(q["startTime"][0]), int(q["endTime"][0])
        return [r for r in rows if st <= r[0] <= et]

    res = fetch_range_cached(
        "BTCUSDT",
        "1m",
        t0,
        t0 + 5 * step,
        cache=cache,
        policy=CachePolicy.REFRESH,
        fetch=fetch,
        sleep_s=0,
        closed_only_before_ms=fence,
    )
    # REFRESH stores only closed: ot+step <= fence → ot <= fence-step = t0+3*step
    stored = cache.read_range("BTCUSDT", "1m", t0, t0 + 5 * step)
    assert all(int(k[0]) + step <= fence for k in stored)
    assert not any(int(k[0]) == t0 + 4 * step for k in stored)


def test_out_of_order_merge_and_validate():
    t0 = 1_700_000_000_000
    step = 60_000
    a = [_k(t0 + 2 * step), _k(t0)]
    b = [_k(t0 + step)]
    m = merge_klines(a, b)
    assert [int(x[0]) for x in m] == [t0, t0 + step, t0 + 2 * step]
    v = validate_klines_sequence(m, timeframe="1m")
    assert v["ok"] and v["dupes"] == 0


def test_cached_vs_fresh_engine_equality(cache):
    """Same candles → same GoldenFibo final state regardless of cache path."""
    t0 = 1_700_000_000_000
    step = 60_000
    # synthetic trending path
    rows = []
    px = 100.0
    for i in range(200):
        o = px
        h = px * 1.003
        l = px * 0.997
        c = px * 1.001
        rows.append(_k(t0 + i * step, o=str(o), h=str(h), l=str(l), c=str(c), v="2", q=str(2 * c)))
        px = c

    def fetch(url: str):
        import urllib.parse
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        st, et = int(q["startTime"][0]), int(q["endTime"][0])
        lim = int(q["limit"][0])
        return [r for r in rows if st <= r[0] <= et][:lim]

    fresh = fetch_range_cached(
        "BTCUSDT", "1m", t0, t0 + 200 * step,
        cache=cache, policy=CachePolicy.BYPASS, fetch=fetch, sleep_s=0,
        closed_only_before_ms=t0 + 200 * step,
    )
    # populate cache via REFRESH
    fetch_range_cached(
        "BTCUSDT", "1m", t0, t0 + 200 * step,
        cache=cache, policy=CachePolicy.REFRESH, fetch=fetch, sleep_s=0,
        closed_only_before_ms=t0 + 200 * step,
    )
    cached = fetch_range_cached(
        "BTCUSDT", "1m", t0, t0 + 200 * step,
        cache=cache, policy=CachePolicy.AUTO, fetch=fetch, sleep_s=0,
        refresh_tail_ms=0,
        closed_only_before_ms=t0 + 200 * step,
    )
    assert len(fresh.klines) == len(cached.klines) == 200
    for a, b in zip(fresh.klines, cached.klines):
        assert int(a[0]) == int(b[0])
        assert a[1:6] == b[1:6]
        assert a[7] == b[7]

    cfg = EngineConfig(side=Side.SELL, percentage=Decimal("0.001"))
    ea = GoldenFiboEngine(cfg)
    eb = GoldenFiboEngine(cfg)
    run_ohlc_on_engine(ea, fresh.klines, mode=OhlcResolveMode.LEGACY)
    run_ohlc_on_engine(eb, cached.klines, mode=OhlcResolveMode.LEGACY)
    assert ea.state.cycle_id == eb.state.cycle_id
    assert ea.state.highest_filled == eb.state.highest_filled
    assert ea.state.p0 == eb.state.p0
    assert ea.state.shared_tp == eb.state.shared_tp


def test_visual_clip_rule():
    """Mirror frontend clip: visual_start = max(activation, chart_first)."""
    chart_first = 1_700_100_000
    act_old = 1_700_000_000
    act_new = 1_700_100_500
    assert max(act_old, chart_first) == chart_first
    assert max(act_new, chart_first) == act_new
