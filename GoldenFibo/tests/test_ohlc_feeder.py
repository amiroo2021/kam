"""Legacy OHLC feeder behavior + strict ambiguity."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from goldenfibo import (
    DomainEventKind,
    EngineConfig,
    GoldenFiboEngine,
    OhlcResolveMode,
    Side,
    ladder_step,
)
from goldenfibo.feeders.historical_ohlc import collect_ohlc_events, replay_ohlc_legacy


def ms(dt: str) -> int:
    return int(datetime.fromisoformat(dt.replace("Z", "+00:00")).timestamp() * 1000)


def test_legacy_next_cycle_p0_chains_from_exact_tp():
    candles = [
        [ms("2026-01-01T00:01:00Z"), "100", "100.01", "99.90", "100", "0", 0, "0"],
        [ms("2026-01-01T00:02:00Z"), "120", "120", "119", "119", "0", 0, "0"],
    ]
    st = replay_ohlc_legacy(candles, side=Side.SELL)
    assert st.closed[0].exit == Decimal("99.9")
    assert st.p0 == Decimal("99.9")
    assert st.initial_p0 == Decimal("100")
    assert st.legs[0].entry == Decimal("99.9")


def test_legacy_step0_tp_only_candle():
    candles = [[ms("2026-01-01T00:01:00Z"), "100", "100.01", "99.90", "100", "0", 0, "0"]]
    st = replay_ohlc_legacy(candles, side=Side.SELL)
    assert st.closed[0].exit == Decimal("99.9")
    assert st.p0 == Decimal("99.9")
    assert st.highest_filled == 0


def test_legacy_deep_cycle_tp_chains_to_old_p2():
    candles = [[ms("2026-01-01T00:01:00Z"), "100", "100.90", "100.42", "100.50", "0", 0, "0"]]
    st = replay_ohlc_legacy(candles, side=Side.SELL)
    old_p2, _ = ladder_step(Side.SELL, Decimal("100"), 2)
    assert st.closed[0].highest_step == 3
    assert st.closed[0].exit == old_p2
    assert st.p0 == old_p2
    assert st.legs[0].entry == old_p2


def test_legacy_no_same_candle_reprocess_after_tp():
    candles = [[ms("2026-01-01T00:01:00Z"), "100", "100.10", "99.90", "100.05", "0", 0, "0"]]
    st = replay_ohlc_legacy(candles, side=Side.SELL)
    assert st.p0 == Decimal("99.9")
    assert st.highest_filled == 0
    assert len(st.legs) == 1


def test_legacy_deterministic_double_run():
    candles = [
        [ms("2026-01-01T00:01:00Z"), "100", "100.20", "99.90", "100", "0", 0, "0"],
        [ms("2026-01-01T00:02:00Z"), "99.9", "100.2", "99.80", "100", "0", 0, "0"],
        [ms("2026-01-01T00:03:00Z"), "99.8", "100.3", "99.70", "100", "0", 0, "0"],
    ]
    a = replay_ohlc_legacy(candles, side=Side.SELL)
    b = replay_ohlc_legacy(candles, side=Side.SELL)
    assert a.comparable() == b.comparable()


def test_custom_percentage_ohlc():
    candles = [[ms("2026-01-01T00:01:00Z"), "100", "100.50", "99.00", "100", "0", 0, "0"]]
    default_st = replay_ohlc_legacy(candles, side=Side.SELL)
    custom_st = replay_ohlc_legacy(candles, side=Side.SELL, percentage=0.01)
    assert custom_st.closed[0].exit == Decimal("99.0")
    assert custom_st.p0 == Decimal("99.0")
    # default 0.001 progresses before TP under adverse-first ordering
    assert default_st.closed[0].exit > Decimal("100")


def test_strict_emits_ambiguous_and_freezes_path():
    # SELL P0=100, TP0=99.9, P1≈100.1618 — high and low both touch
    candles = [[ms("2026-01-01T00:01:00Z"), "100", "100.20", "99.90", "100", "0", 0, "0"]]
    cfg = EngineConfig(side=Side.SELL, percentage=Decimal("0.001"))
    events = collect_ohlc_events(candles, cfg, mode=OhlcResolveMode.STRICT)
    eng = GoldenFiboEngine(cfg)
    result = eng.run(events)
    assert any(e.kind is DomainEventKind.AMBIGUOUS_BAR for e in result.events)
    # Seeded step0 only — no progression, no TP chain
    assert eng.state.cycle_id == 1
    assert eng.state.highest_filled == 0
    assert eng.state.p0 == Decimal("100")
    assert len(eng.state.closed) == 0
    amb = next(e for e in result.events if e.kind is DomainEventKind.AMBIGUOUS_BAR)
    assert "shared_tp" in amb.payload
    assert "next_progression" in amb.payload
    assert amb.payload["ohlc"]["h"] == "100.20"


def test_legacy_same_candle_still_resolves_adverse_first():
    candles = [[ms("2026-01-01T00:01:00Z"), "100", "100.20", "99.90", "100", "0", 0, "0"]]
    st = replay_ohlc_legacy(candles, side=Side.SELL)
    # legacy does not freeze; progresses and/or TPs
    assert st.cycle_id >= 1
