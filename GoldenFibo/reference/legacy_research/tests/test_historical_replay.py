from datetime import datetime, timezone

from golden_fibo.constants import Side
from golden_fibo.historical_replay import historical_anchor_utc, replay_ohlc
from golden_fibo.ladder import ladder_step


def ms(dt: str) -> int:
    return int(datetime.fromisoformat(dt.replace('Z', '+00:00')).timestamp() * 1000)


def test_three_month_anchor_starts_at_0001_utc_same_calendar_day():
    got = historical_anchor_utc(datetime(2026, 9, 13, 20, 30, tzinfo=timezone.utc))
    assert got.isoformat() == "2026-06-13T00:01:00+00:00"


def test_next_cycle_p0_chains_from_exact_tp_not_next_candle_open():
    candles = [
        [ms("2026-01-01T00:01:00Z"), "100", "100.01", "99.90", "100", "0", 0, "0"],
        [ms("2026-01-01T00:02:00Z"), "120", "120", "119", "119", "0", 0, "0"],
    ]
    state = replay_ohlc(candles, side=Side.SELL)
    assert state.closed[0].exit == 99.9
    assert state.p0 == 99.9
    assert state.initial_p0 == 100.0
    assert state.legs[0].entry == 99.9


def test_a_step0_tp_chains_next_cycle_p0_to_exact_tp():
    candles = [
        [ms("2026-01-01T00:01:00Z"), "100", "100.01", "99.90", "100", "0", 0, "0"],
    ]
    state = replay_ohlc(candles, side=Side.SELL)
    assert state.closed[0].exit == 99.9
    assert state.p0 == 99.9
    assert state.legs[0].entry == 99.9
    assert state.highest_filled == 0


def test_b_deep_cycle_tp_chains_next_cycle_p0_to_exact_old_p2():
    # SELL P0=100 gives P1=100.1618, P2=100.4236124, P3=100.8478168632.
    # Same existing candle touches through P3 and down through the new shared TP=P2.
    candles = [
        [ms("2026-01-01T00:01:00Z"), "100", "100.90", "100.42", "100.50", "0", 0, "0"],
    ]
    state = replay_ohlc(candles, side=Side.SELL)
    old_p2, _ = ladder_step(Side.SELL, 100, 2)
    assert state.closed[0].highest_step == 3
    assert state.closed[0].exit == old_p2
    assert state.p0 == old_p2
    assert state.legs[0].entry == old_p2


def test_c_new_cycle_does_not_process_remainder_of_same_candle_after_tp():
    # Old cycle closes at 99.9 without touching old P1≈100.1618. New SELL cycle
    # P0=99.9 would have P1≈100.0616382, and this same candle's high=100.10
    # would touch it if same-candle reprocessing were incorrectly allowed.
    # Canonical behavior leaves the new cycle at step0.
    candles = [
        [ms("2026-01-01T00:01:00Z"), "100", "100.10", "99.90", "100.05", "0", 0, "0"],
    ]
    state = replay_ohlc(candles, side=Side.SELL)
    assert state.p0 == 99.9
    assert state.highest_filled == 0
    assert len(state.legs) == 1


def test_d_replay_identical_candles_twice_produces_identical_cycle_p0_step_tp():
    candles = [
        [ms("2026-01-01T00:01:00Z"), "100", "100.20", "99.90", "100", "0", 0, "0"],
        [ms("2026-01-01T00:02:00Z"), "99.9", "100.2", "99.80", "100", "0", 0, "0"],
        [ms("2026-01-01T00:03:00Z"), "99.8", "100.3", "99.70", "100", "0", 0, "0"],
    ]
    a = replay_ohlc(candles, side=Side.SELL)
    b = replay_ohlc(candles, side=Side.SELL)
    assert (a.cycle, a.p0, a.highest_filled, a.shared_tp) == (b.cycle, b.p0, b.highest_filled, b.shared_tp)
    assert a.comparable() == b.comparable()
