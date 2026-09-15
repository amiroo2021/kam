from datetime import datetime, timezone

from golden_fibo.constants import Side
from golden_fibo.historical_replay import levels_p0_to_pn, replay_ohlc


def ms(dt: str) -> int:
    return int(datetime.fromisoformat(dt.replace('Z', '+00:00')).timestamp() * 1000)


def test_replay_accepts_custom_percentage_without_changing_default_math():
    candles = [[ms("2026-01-01T00:01:00Z"), "100", "100.50", "99.00", "100", "0", 0, "0"]]
    default_state = replay_ohlc(candles, side=Side.SELL)
    custom_state = replay_ohlc(candles, side=Side.SELL, percentage=0.01)

    assert default_state.closed[0].exit > 100.0  # default 0.001 progresses before TP under canonical ordering
    assert custom_state.closed[0].exit == 99.0   # custom step-0 TP is 1% below P0
    assert custom_state.p0 == 99.0

    levels = levels_p0_to_pn(custom_state, 1, percentage=0.01)
    assert levels[0]["price"] == 99.0
    assert levels[1]["price"] > 99.0
