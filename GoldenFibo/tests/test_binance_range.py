"""Paginated kline range download (mocked HTTP)."""

from __future__ import annotations

from goldenfibo.marketdata.binance_klines_range import fetch_klines_range
from goldenfibo.marketdata.timeframes import require_aligned


def _bar(ot: int, px: str = "100") -> list:
    return [ot, px, px, px, px, "1", ot + 59_999, "100"]


def test_pagination_and_dedup():
    # 2500 bars of 1m from t0
    t0 = 1_700_000_000_000
    all_bars = [_bar(t0 + i * 60_000) for i in range(2500)]

    def fetch(url: str):
        # parse startTime from url
        import urllib.parse

        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        st = int(q["startTime"][0])
        en = int(q["endTime"][0])
        lim = int(q["limit"][0])
        batch = [b for b in all_bars if st <= b[0] <= en][:lim]
        return batch

    out = fetch_klines_range(
        "BTCUSDT",
        "1m",
        t0,
        t0 + 2500 * 60_000,
        limit=1000,
        sleep_s=0,
        fetch=fetch,
    )
    assert len(out) == 2500
    opens = [b[0] for b in out]
    assert opens == sorted(set(opens))


def test_closed_only_before():
    t0 = 1_700_000_000_000
    bars = [_bar(t0 + i * 60_000) for i in range(5)]

    def fetch(url: str):
        return bars

    # only first 3 closed if bound = t0+3*60k
    out = fetch_klines_range(
        "BTCUSDT",
        "1m",
        t0,
        t0 + 10 * 60_000,
        sleep_s=0,
        fetch=fetch,
        closed_only_before_ms=t0 + 3 * 60_000,
    )
    assert all(b[0] + 60_000 <= t0 + 3 * 60_000 for b in out)


def test_require_aligned_1m():
    t = 1_700_000_000_000
    # align floor
    aligned = t - (t % 60_000)
    require_aligned(aligned, "1m")
    try:
        require_aligned(aligned + 37_000, "1m")
        assert False, "should reject"
    except ValueError:
        pass
