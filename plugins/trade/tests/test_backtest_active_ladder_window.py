import math
import unittest

from golden_fibo.constants import Side
from golden_fibo.historical_replay import ReplayLeg, ReplayState
from plugins.trade import backtest_wizard as wizard


class BacktestActiveLadderWindowTests(unittest.TestCase):
    def test_ladder_metrics_start_at_active_cycle_p0_not_closed_cycles(self):
        state = ReplayState(side=Side.SELL, cycle=2, p0=100.0, highest_filled=0, shared_tp=99.9)
        state.legs = [ReplayLeg(0, 100.0, 120_000, "1970-01-01T00:02:00Z")]
        state.initial_p0 = 1000.0

        candles = [
            # Closed/old ladder candle before active P0: huge volume must not count.
            [0, "1000", "1010", "990", "1000", "1000", 0, "1000000"],
            # Active ladder window from latest P0 onward.
            [120_000, "100", "101", "99", "100", "10", 0, "1000"],
            [180_000, "102", "103", "101", "102", "10", 0, "1020"],
        ]

        start_ts = wizard._active_ladder_start_ts(state)
        self.assertEqual(start_ts, 120_000)
        self.assertAlmostEqual(wizard._vwap(candles, start_ts), 101.0)
        self.assertTrue(99 <= wizard._poc(candles, start_ts) <= 103)

        old_window_vwap = wizard._vwap(candles, 0)
        self.assertNotAlmostEqual(old_window_vwap, 101.0)
        self.assertTrue(math.isfinite(old_window_vwap))

    def test_active_ladder_start_requires_open_p0_leg(self):
        state = ReplayState(side=Side.BUY)
        with self.assertRaises(ValueError):
            wizard._active_ladder_start_ts(state)


if __name__ == "__main__":
    unittest.main()
