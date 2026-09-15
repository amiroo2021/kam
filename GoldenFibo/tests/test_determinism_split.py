"""Determinism: same MarketEvent stream all-at-once vs split resume."""

from __future__ import annotations

from decimal import Decimal

import pytest

from goldenfibo import EngineConfig, GoldenFiboEngine, Side
from goldenfibo.engine.config import OhlcResolveMode
from goldenfibo.feeders.historical_ohlc import collect_ohlc_events
from goldenfibo.session.runner import run_ohlc_on_engine


def _c(ts: int, o, h, l, c) -> list:
    return [ts, str(o), str(h), str(l), str(c), "1", ts + 59_999, "100"]


def synthetic_sell_series():
    t0 = 1_700_000_060_000  # aligned
    return [
        _c(t0, 100, 100.20, 99.90, 100),
        _c(t0 + 60_000, 99.9, 100.2, 99.80, 100),
        _c(t0 + 120_000, 99.8, 100.3, 99.70, 100),
        _c(t0 + 180_000, 99.7, 100.5, 99.50, 100.1),
        _c(t0 + 240_000, 100.1, 100.9, 100.0, 100.4),
        _c(t0 + 300_000, 100.4, 101.0, 99.9, 100.2),
    ]


@pytest.mark.parametrize("split", [1, 2, 3, 4, 5, 8, 12, 20])
def test_event_stream_split_identical_state(split):
    cfg = EngineConfig(side=Side.SELL, percentage=Decimal("0.001"))
    candles = synthetic_sell_series()
    events = collect_ohlc_events(candles, cfg, mode=OhlcResolveMode.LEGACY)
    if split >= len(events):
        split = max(1, len(events) // 2)

    eng_a = GoldenFiboEngine(cfg)
    eng_a.run(events)

    eng_b = GoldenFiboEngine(cfg)
    eng_b.run(events[:split])
    eng_b.run(events[split:])

    assert eng_a.state.comparable() == eng_b.state.comparable()


def test_split_after_seed_and_around_tp():
    cfg = EngineConfig(side=Side.SELL, percentage=Decimal("0.001"))
    candles = synthetic_sell_series()
    events = collect_ohlc_events(candles, cfg, mode=OhlcResolveMode.LEGACY)
    # first event is SEED
    assert events[0].kind.value == "seed_p0"
    for split in (1, 2, len(events) - 1):
        a = GoldenFiboEngine(cfg)
        a.run(events)
        b = GoldenFiboEngine(cfg)
        b.run(events[:split])
        b.run(events[split:])
        assert a.state.comparable() == b.state.comparable()


def test_candle_runner_same_engine_object():
    cfg = EngineConfig(side=Side.BUY, percentage=Decimal("0.001"))
    candles = synthetic_sell_series()  # works for buy too
    eng = GoldenFiboEngine(cfg)
    eid = id(eng)
    run_ohlc_on_engine(eng, candles[:3], mode=OhlcResolveMode.LEGACY)
    run_ohlc_on_engine(eng, candles[3:], mode=OhlcResolveMode.LEGACY)
    assert id(eng) == eid


def test_ambiguity_counted_on_dual_touch_legacy():
    cfg = EngineConfig(side=Side.SELL, percentage=Decimal("0.001"))
    # one bar touches TP and progression
    candles = [[1_700_000_060_000, "100", "100.20", "99.90", "100", "1", 0, "100"]]
    events = collect_ohlc_events(candles, cfg, mode=OhlcResolveMode.LEGACY)
    from goldenfibo.feeders.historical_ohlc import count_ambiguous

    assert count_ambiguous(events) >= 1
