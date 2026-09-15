"""Inclusive BACKTEST end (candle-open inclusive) tests."""

from __future__ import annotations

from goldenfibo.marketdata.binance_klines_range import (
    fetch_klines_range,
    inclusive_open_range_fetch_end,
)
from goldenfibo.marketdata.timeframes import interval_ms


def test_inclusive_helper_adds_timeframe():
    end = 1_700_000_000_000
    assert inclusive_open_range_fetch_end(end, "1m") == end + 60_000
    assert inclusive_open_range_fetch_end(end, "5m") == end + 300_000
    assert inclusive_open_range_fetch_end(end, "1h") == end + 3_600_000


def test_single_bar_start_equals_end():
    t0 = 1_700_000_060_000  # aligned

    def fetch(url: str):
        import urllib.parse

        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        st, en = int(q["startTime"][0]), int(q["endTime"][0])
        # one bar at t0
        if st <= t0 <= en:
            return [[t0, "100", "101", "99", "100.5", "1", t0 + 59_999, "100"]]
        return []

    fetch_end = inclusive_open_range_fetch_end(t0, "1m")
    out = fetch_klines_range(
        "BTCUSDT",
        "1m",
        t0,
        fetch_end,
        sleep_s=0,
        fetch=fetch,
        closed_only_before_ms=fetch_end,
    )
    assert len(out) == 1
    assert int(out[0][0]) == t0


def test_1m_multi_bar_inclusive_and_pagination():
    t0 = 1_700_000_060_000
    # 1500 minutes inclusive
    n = 1500
    end_open = t0 + (n - 1) * 60_000
    all_bars = [[t0 + i * 60_000, "1", "2", "0.5", "1", "1", 0, "1"] for i in range(n)]

    def fetch(url: str):
        import urllib.parse

        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        st, en, lim = int(q["startTime"][0]), int(q["endTime"][0]), int(q["limit"][0])
        return [b for b in all_bars if st <= b[0] <= en][:lim]

    fetch_end = inclusive_open_range_fetch_end(end_open, "1m")
    out = fetch_klines_range(
        "BTCUSDT",
        "1m",
        t0,
        fetch_end,
        limit=1000,
        sleep_s=0,
        fetch=fetch,
        closed_only_before_ms=fetch_end,
    )
    assert len(out) == n
    assert int(out[0][0]) == t0
    assert int(out[-1][0]) == end_open


def test_5m_inclusive_end():
    t0 = 1_700_000_100_000
    # align to 5m
    step = interval_ms("5m")
    t0 = t0 - (t0 % step)
    end_open = t0 + 2 * step  # 3 bars
    bars = [[t0 + i * step, "1", "2", "1", "1.5", "1", 0, "1"] for i in range(3)]

    def fetch(url: str):
        import urllib.parse

        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        st, en = int(q["startTime"][0]), int(q["endTime"][0])
        return [b for b in bars if st <= b[0] <= en]

    fetch_end = inclusive_open_range_fetch_end(end_open, "5m")
    out = fetch_klines_range(
        "BTCUSDT",
        "5m",
        t0,
        fetch_end,
        sleep_s=0,
        fetch=fetch,
        closed_only_before_ms=fetch_end,
    )
    assert len(out) == 3
    assert int(out[-1][0]) == end_open
