from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from goldenfibo.backtest import run_finite_backtest
from goldenfibo.engine.config import Side
from plugins.trade import backtest_wizard as wizard


def _candles():
    t0 = 1_700_000_000_000
    out = []
    price = Decimal("100.0")
    for i in range(180):
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
    assert len(baseline.chart_candles) == len(candles)
    assert baseline.chart_candles[0]["time"] == candles[0][0] // 1000
    assert baseline.chart_candles[-1]["time"] == candles[-1][0] // 1000


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
    assert screen.attachments == ["/tmp/backtest.jpg"]

    adapter = _Adapter()
    msg = SimpleNamespace(chat=SimpleNamespace(id=123), message_thread_id=None, text="/backtest")
    handled = await wizard.handle_backtest_command(adapter, msg)
    assert handled is True
    assert adapter.calls
    assert any(call[0] == "keyboard" for call in adapter.calls)


def test_telegram_backtest_screen_includes_text_and_image(monkeypatch):
    asyncio.run(_run_backtest_screen(monkeypatch))
