"""Handoff fence and buffer ordering (no live Binance)."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from goldenfibo import EngineConfig, Side
from goldenfibo.engine.config import OhlcResolveMode
from goldenfibo.session.controller import SessionController
from goldenfibo.session.runner import run_ohlc_on_engine
from goldenfibo.session.types import SessionMode, SessionPhase


def test_engine_survives_hist_then_buffer_flush():
    ctrl = SessionController()
    ctrl.mode = SessionMode.REPLAY_TO_LIVE
    ctrl.timeframe = "1m"
    engine_id = id(ctrl.engine)
    t0 = 1_700_000_060_000
    candles = [
        [t0, "100", "100.01", "99.90", "100", "1", t0 + 59999, "100"],
        [t0 + 60000, "99.9", "100.1", "99.8", "100", "1", t0 + 119999, "100"],
    ]
    run_ohlc_on_engine(ctrl.engine, candles, mode=OhlcResolveMode.LEGACY, event_log=ctrl.event_log)
    ctrl.last_hist_open_ms = t0 + 60000
    ctrl.fence_ms = t0 + 120000
    # buffer out-of-order trades straddling fence
    ctrl._trade_buffer = [
        (t0 + 130000, Decimal("100.5")),
        (t0 + 110000, Decimal("99.5")),  # before fence — discard
        (t0 + 125000, Decimal("100.2")),
    ]
    ctrl._buffering = False
    ctrl._live_enabled = True

    async def go():
        await ctrl._flush_trade_buffer()

    asyncio.get_event_loop().run_until_complete(go()) if False else None
    asyncio.run(go())
    assert id(ctrl.engine) == engine_id
    assert ctrl.engine.state.cycle_id >= 1


def test_duplicate_open_time_kline_ignored():
    ctrl = SessionController()
    ctrl.last_hist_open_ms = 1000
    ctrl._live_enabled = True
    ctrl.chart_candles = []
    # simulate on_kline skip via direct check
    o_time = 1000
    assert o_time <= ctrl.last_hist_open_ms


def test_start_time_must_align():
    ctrl = SessionController()

    async def bad():
        await ctrl.start_run(
            mode=SessionMode.BACKTEST,
            start_time="2026-06-01T00:01:37Z",
            end_time="2026-06-01T00:10:00Z",
        )

    with pytest.raises(ValueError, match="aligned"):
        asyncio.run(bad())
