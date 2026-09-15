"""Same ordered MarketEvents → same engine state (source-agnostic)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from goldenfibo import EngineConfig, GoldenFiboEngine, MarketEvent, MarketEventKind, Side
from goldenfibo.feeders.historical_ohlc import collect_ohlc_events


def ms(dt: str) -> int:
    return int(datetime.fromisoformat(dt.replace("Z", "+00:00")).timestamp() * 1000)


def test_identical_event_stream_yields_identical_state_hash():
    """Simulate historical feeder vs 'live/replay' replaying the same events."""
    candles = [
        [ms("2026-01-01T00:01:00Z"), "100", "100.20", "99.90", "100", "0", 0, "0"],
        [ms("2026-01-01T00:02:00Z"), "99.9", "100.2", "99.80", "100", "0", 0, "0"],
        [ms("2026-01-01T00:03:00Z"), "99.8", "100.3", "99.70", "100", "0", 0, "0"],
    ]
    cfg = EngineConfig(side=Side.SELL, percentage=Decimal("0.001"))

    # Path A: events from historical OHLC feeder
    events_hist = collect_ohlc_events(candles, cfg)
    eng_a = GoldenFiboEngine(cfg)
    eng_a.run(events_hist)

    # Path B: same events as if a live/replay bus re-emitted them
    events_live = [
        MarketEvent(
            kind=e.kind,
            ts_ms=e.ts_ms,
            price=e.price,
            step=e.step,
            meta=dict(e.meta),
        )
        for e in events_hist
    ]
    eng_b = GoldenFiboEngine(cfg)
    eng_b.run(events_live)

    assert eng_a.state.comparable() == eng_b.state.comparable()


def test_manual_event_path_matches_feeder_for_simple_progression():
    cfg = EngineConfig(side=Side.BUY, percentage=Decimal("0.001"))
    candles = [[ms("2026-01-01T00:01:00Z"), "2500", "2500", "2495", "2496", "0", 0, "0"]]
    from_feeder = collect_ohlc_events(candles, cfg)

    eng_f = GoldenFiboEngine(cfg)
    eng_f.run(from_feeder)

    eng_m = GoldenFiboEngine(cfg)
    # Manually: seed + progression steps present in feeder output
    for e in from_feeder:
        eng_m.on_event(e)

    assert eng_f.state.comparable() == eng_m.state.comparable()
    assert eng_f.state.highest_filled >= 0
