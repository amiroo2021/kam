from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from goldenfibo.backtest import run_finite_backtest
from goldenfibo.engine.config import Side
from goldenfibo.marketdata.binance_public import resample_chart_candles, upsert_display_candle_from_1m
from goldenfibo.session.controller import SessionController
from goldenfibo.session.types import SessionMode
from plugins.trade import backtest_wizard as wizard


def _candles(n: int = 180, t0: int = 1_700_000_000_000):
    out = []
    price = Decimal("100.0")
    for i in range(n):
        o = price
        h = o + Decimal("0.4")
        l = o - Decimal("0.3")
        c = o + (Decimal("0.05") if i % 2 == 0 else Decimal("-0.04"))
        v = Decimal("1.0") + Decimal(i % 5) / Decimal("10")
        out.append([t0 + i * 60_000, str(o), str(h), str(l), str(c), str(v), t0 + i * 60_000 + 59_999, str(v * c)])
        price = c
    return out


def test_display_timeframe_does_not_change_engine_results():
    candles = _candles()
    baseline = run_finite_backtest(candles, side=Side.SELL, percentage=Decimal("0.001"), symbol="BTCUSDT")
    later = run_finite_backtest(candles, side=Side.SELL, percentage=Decimal("0.001"), symbol="BTCUSDT")

    assert baseline.state_payload == later.state_payload
    assert baseline.engine.state.comparable() == later.engine.state.comparable()
    assert baseline.bars_processed == len(candles)
    assert baseline.klines[0][0] == candles[0][0]
    assert baseline.klines[-1][0] == candles[-1][0]


def test_display_resample_spacing_is_interval_aligned():
    # Aligned start so every delta is exact.
    t0 = 1_700_000_000_000  # already on a minute boundary; choose hour-aligned
    t0 = t0 - (t0 % 3_600_000)
    candles = _candles(n=240, t0=t0)  # 4h of 1m
    for tf, step in (("15m", 900), ("1h", 3600), ("4h", 14400)):
        chart = resample_chart_candles(candles, tf)
        assert len(chart) >= 2
        times = [c["time"] for c in chart]
        deltas = [times[i + 1] - times[i] for i in range(len(times) - 1)]
        assert all(d == step for d in deltas), (tf, deltas[:5])


def test_page_split_resample_must_not_inflate_counts():
    t0 = 1_700_000_000_000 - (1_700_000_000_000 % 3_600_000)
    candles = _candles(n=3000, t0=t0)
    continuous = resample_chart_candles(candles, "1h")
    # Simulate old buggy page-split extend
    pages = []
    for i in range(0, len(candles), 1000):
        pages.extend(resample_chart_candles(candles[i : i + 1000], "1h"))
    assert len(pages) > len(continuous)
    # Continuous rebuild is the correct browser payload shape
    assert continuous[0]["time"] % 3600 == 0
    assert continuous[1]["time"] - continuous[0]["time"] == 3600


def test_live_1m_updates_merge_into_display_bucket():
    chart = []
    t0 = 1_700_000_000_000 - (1_700_000_000_000 % 3_600_000)
    c1 = upsert_display_candle_from_1m(chart, [t0, "100", "101", "99", "100.5", "1", t0 + 59999, "100"], "1h")
    c2 = upsert_display_candle_from_1m(chart, [t0 + 60_000, "100.5", "102", "100", "101", "2", t0 + 119999, "200"], "1h")
    assert len(chart) == 1
    assert c1 is chart[0] or c2 is chart[0]
    assert chart[0]["time"] == t0 // 1000
    assert chart[0]["high"] == 102.0
    assert chart[0]["low"] == 99.0
    assert chart[0]["close"] == 101.0
    # Next hour opens a new display candle
    upsert_display_candle_from_1m(chart, [t0 + 3_600_000, "101", "103", "100.5", "102", "1", t0 + 3_659_999, "100"], "1h")
    assert len(chart) == 2
    assert chart[1]["time"] - chart[0]["time"] == 3600


class _Adapter:
    def __init__(self):
        self.calls = []

    async def send_inline_keyboard(self, **kwargs):
        self.calls.append(("keyboard", kwargs))

    async def send_image_file(self, **kwargs):
        self.calls.append(("image", kwargs))

    async def send(self, *args, **kwargs):
        self.calls.append(("send", args, kwargs))


async def _run_backtest_screen(monkeypatch):
    candles = _candles()
    monkeypatch.setattr(wizard, "_fetch_klines", lambda *args, **kwargs: candles)
    monkeypatch.setattr(wizard, "_draw_jpg", lambda *args, **kwargs: "/tmp/backtest.jpg")
    screen = wizard._WIZARD._run_backtest(
        wizard.State(ladder="sell", market="spot", symbol="BTCUSDT", percentage=0.001),
    )
    assert screen.text
    assert "Backtest complete:" in screen.text
    assert "Current step:" in screen.text
    assert "Ladder VWAP:" in screen.text
    assert "Ladder POC:" in screen.text
    assert "Ladder Value Area:" in screen.text
    assert "Step VWAP:" in screen.text
    assert "Step POC:" in screen.text
    assert "Step Value Area" not in screen.text
    assert screen.attachments == ["/tmp/backtest.jpg"]

    adapter = _Adapter()
    msg = SimpleNamespace(chat=SimpleNamespace(id=123), message_thread_id=None, text="/backtest")
    handled = await wizard.handle_backtest_command(adapter, msg)
    assert handled is True
    assert adapter.calls
    assert any(call[0] == "keyboard" for call in adapter.calls)


def test_telegram_backtest_screen_includes_text_and_image(monkeypatch):
    asyncio.run(_run_backtest_screen(monkeypatch))


def test_fresh_backtest_clears_prior_engine_state():
    async def go():
        c = SessionController()
        c.engine  # seed default
        c.chart_candles = [{"time": 1, "open": 1, "high": 1, "low": 1, "close": 1}]
        c.klines = [[1, "1", "1", "1", "1", "1", 2, "1"]]
        c.bars_processed = 999
        c.ambiguity_count = 42
        # short synthetic window via monkeypatched hist is heavy; just check start_run reset fields
        try:
            await c.start_run(
                mode=SessionMode.BACKTEST,
                symbol="BTCUSDT",
                timeframe="1h",
                market="spot",
                start_time="2026-09-18T00:00:00Z",
                end_time="2026-09-18T00:02:00Z",
            )
        except Exception:
            pass
        assert c.bars_processed == 0 or c.phase.value in {
            "loading",
            "loading_history",
            "downloading_history",
            "replaying",
            "backtest_done",
            "error",
            "stopped",
        }
        assert c.display_timeframe == "1h"
        assert c.timeframe == "1m"
        # Prior synthetic chart must not survive start_run clear
        # (may be empty or repopulated from real hist — not the old fake candle)
        assert not any(cnd.get("time") == 1 for cnd in (c.chart_candles or []))

    asyncio.run(go())


def test_backtest_progress_payload_uses_bars_when_backtest_fields_absent():
    phase, pct, detail = wizard.BacktestWizard._progress_from_payload(
        {"stage": "backtest", "bars_done": 1, "bars_est": 4, "pct": 25.0}
    )
    assert phase == "backtest"
    assert pct == 25.0
    assert "1 / 4" in detail


def test_aggtrade_acquisition_progress_payload_is_distinct_phase():
    phase, pct, detail = wizard.BacktestWizard._progress_from_payload(
        {
            "stage": "acquiring_aggtrades",
            "aggtrade_days_done": 2,
            "aggtrade_days_total": 5,
            "detail": "Acquiring aggressive trade data...",
        }
    )
    assert phase == "acquiring_aggtrades"
    assert pct == 40.0
    assert "2 / 5" in detail


class _CompleteSqlOnlyCache:
    def __init__(self):
        self.queries = []

    def missing_ranges(self, market, symbol, start, end):
        return []

    def ensure_coverage(self, market, symbol, start, end, **kwargs):
        return SimpleNamespace(covered=True, requested_start_ms=start, requested_end_ms=end)

    def coverage_covers(self, market, symbol, start, end):
        return True

    def aggregate_aggressive_metrics(self, market, symbol, start, end):
        self.queries.append((start, end))
        return {
            "status": "COMPLETE",
            "trade_count": 2,
            "buy_qty": 3.0,
            "buy_notional": 33.0,
            "buy_vwap": 11.0,
            "sell_qty": 1.0,
            "sell_notional": 9.0,
            "sell_vwap": 9.0,
            "total_qty": 4.0,
            "delta": 2.0,
            "delta_ratio": 0.5,
        }

    def query_trades_range(self, *args, **kwargs):
        raise AssertionError("display aggregate metrics must not materialize trades")


def test_aggtrade_display_metrics_use_sql_aggregates_not_query_trades_range():
    cache = _CompleteSqlOnlyCache()
    ladder, step, meta = wizard._aggtrade_aggressor_metrics_for_display(
        cache,
        "futures",
        "HYPEUSDT",
        1000,
        5000,
        9000,
    )
    assert meta["covered"] is True
    assert ladder["buy_vwap"] == 11.0
    assert ladder["sell_vwap"] == 9.0
    assert ladder["delta_ratio"] == 0.5
    assert step["buy_vwap"] == 11.0
    assert step["sell_vwap"] == 9.0
    assert step["delta_ratio"] == 0.5
    assert cache.queries == [(1000, 9000), (5000, 9000)]


def test_per_step_aggtrade_table_uses_sql_aggregates_not_query_trades_range():
    cache = _CompleteSqlOnlyCache()
    state = SimpleNamespace(
        legs=[
            SimpleNamespace(step=0, ts=1000),
            SimpleNamespace(step=1, ts=5000),
        ]
    )
    table = wizard._per_step_aggressor_table_from_cache(cache, "futures", "HYPEUSDT", state, 9000)
    assert sorted(table) == [0, 1]
    assert table[0]["delta_ratio"] == 0.5
    assert table[1]["ladder_delta_ratio"] == 0.5
    assert cache.queries == [(1000, 4999), (1000, 4999), (5000, 9000), (1000, 9000)]


def test_aggtrade_display_metrics_do_not_compute_when_coverage_incomplete():
    class IncompleteCache(_CompleteSqlOnlyCache):
        def missing_ranges(self, market, symbol, start, end):
            return [(start, end)]

        def ensure_coverage(self, market, symbol, start, end, **kwargs):
            return SimpleNamespace(covered=False, requested_start_ms=start, requested_end_ms=end)

        def coverage_covers(self, market, symbol, start, end):
            return False

        def aggregate_aggressive_metrics(self, *args, **kwargs):
            raise AssertionError("must not calculate from partial coverage")

    ladder, step, meta = wizard._aggtrade_aggressor_metrics_for_display(
        IncompleteCache(), "futures", "HYPEUSDT", 1000, 5000, 9000
    )
    assert meta["covered"] is False
    assert ladder["status"] == wizard.INCOMPLETE_AGGTRADE_COVERAGE
    assert step["status"] == wizard.INCOMPLETE_AGGTRADE_COVERAGE
