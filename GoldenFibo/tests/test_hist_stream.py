"""Paged historical download + streaming engine apply."""
from __future__ import annotations

from decimal import Decimal

from goldenfibo import EngineConfig, GoldenFiboEngine, Side
from goldenfibo.engine.config import OhlcResolveMode
from goldenfibo.feeders.historical_ohlc import apply_ohlc_to_engine, collect_ohlc_events
from goldenfibo.marketdata.binance_klines_range import fetch_klines_range, iter_klines_pages
from goldenfibo.session.controller import CHART_CANDLE_LIMIT
from goldenfibo.session.runner import apply_ohlc_page, run_ohlc_on_engine


def _candle(ts, o, h, l, c, v=1.0):
    return [ts, str(o), str(h), str(l), str(c), str(v), ts + 59_999, str(float(v) * float(c))]


def test_on_page_progress_callback():
    t0 = 1_700_000_000_000
    step = 60_000
    rows = [_candle(t0 + i * step, 100, 101, 99, 100) for i in range(2500)]

    def fake_fetch(url: str):
        # parse startTime
        import urllib.parse
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        st = int(q["startTime"][0])
        et = int(q["endTime"][0])
        lim = int(q["limit"][0])
        batch = [r for r in rows if st <= r[0] <= et][:lim]
        return batch

    pages_seen = []

    def on_page(info):
        pages_seen.append(dict(info))

    out = fetch_klines_range(
        "BTCUSDT",
        "1m",
        t0,
        t0 + 2500 * step,
        fetch=fake_fetch,
        sleep_s=0,
        on_page=on_page,
    )
    assert len(out) == 2500
    assert len(pages_seen) >= 3
    assert pages_seen[0]["bars_done"] > 0
    assert pages_seen[-1]["bars_done"] == 2500


def test_multi_page_apply_matches_single_batch():
    t0 = 1_700_000_000_000
    step = 60_000
    # synthetic path with progressions possible
    candles = []
    px = 100.0
    for i in range(300):
        o = px
        h = px * 1.002
        l = px * 0.998
        c = px
        candles.append(_candle(t0 + i * step, o, h, l, c))
        px *= 1.0001

    cfg = EngineConfig(side=Side.SELL, percentage=Decimal("0.001"))
    eng_a = GoldenFiboEngine(cfg)
    eng_b = GoldenFiboEngine(cfg)

    # single shot via collect+run
    run_ohlc_on_engine(eng_a, candles, mode=OhlcResolveMode.LEGACY)

    # page streaming 100 at a time
    for i in range(0, len(candles), 100):
        apply_ohlc_page(eng_b, candles[i : i + 100], mode=OhlcResolveMode.LEGACY)

    assert eng_a.state.cycle_id == eng_b.state.cycle_id
    assert eng_a.state.highest_filled == eng_b.state.highest_filled
    assert eng_a.state.p0 == eng_b.state.p0
    assert eng_a.state.shared_tp == eng_b.state.shared_tp


def test_chart_candle_limit_constant():
    assert CHART_CANDLE_LIMIT == 2500
    assert CHART_CANDLE_LIMIT < 100_000


def test_iter_pages_yields_pages_not_flat():
    t0 = 1_700_000_000_000
    step = 60_000
    rows = [_candle(t0 + i * step, 1, 1, 1, 1) for i in range(1500)]

    def fake_fetch(url: str):
        import urllib.parse
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        st = int(q["startTime"][0])
        et = int(q["endTime"][0])
        lim = int(q["limit"][0])
        return [r for r in rows if st <= r[0] <= et][:lim]

    pages = list(
        iter_klines_pages(
            "BTCUSDT",
            "1m",
            t0,
            t0 + 1500 * step,
            fetch=fake_fetch,
            sleep_s=0,
        )
    )
    assert len(pages) == 2
    assert len(pages[0]) == 1000
    assert len(pages[1]) == 500
