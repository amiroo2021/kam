import math
import unittest
from pathlib import Path

from PIL import Image

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

    def test_value_area_uses_active_window_and_returns_val_poc_vah(self):
        candles = [
            # Closed-cycle volume before P0. If included, VA would be near 1000.
            [0, "1000", "1010", "990", "1000", "10000", 0, "10000000"],
            [120_000, "100", "101", "99", "100", "10", 0, "1000"],
            [180_000, "101", "102", "100", "101", "40", 0, "4040"],
            [240_000, "102", "103", "101", "102", "10", 0, "1020"],
        ]

        profile = wizard._value_area(candles, 120_000, bins=40, ratio=0.70)

        self.assertTrue(99 <= profile["val"] <= profile["poc"] <= profile["vah"] <= 103)
        self.assertGreater(profile["covered_volume"], 0)
        self.assertGreaterEqual(profile["covered_volume"] / profile["total_volume"], 0.70)

    def test_jpg_shades_value_area_not_future_ladder_levels(self):
        levels = [
            {"level": "P0", "price": 100.0},
            {"level": "P1", "price": 101.0},
            {"level": "P2", "price": 102.0, "role": "current step P(n)"},
            {"level": "P3", "price": 120.0, "role": "next P(n+1)"},
            {"level": "P4", "price": 130.0, "role": "further progression P(n+2)"},
        ]
        value_area = {"val": 100.5, "poc": 101.2, "vah": 102.5, "covered_volume": 70.0, "total_volume": 100.0}

        out = wizard._draw_jpg(
            "TEST", "spot", Side.SELL, levels, 2,
            ladder_vwap=101.4, step_vwap=101.8, ladder_poc=101.2, step_poc=101.9,
            current=102.0, ladder_value_area=value_area,
        )

        self.assertTrue(Path(out).is_file())
        img = Image.open(out).convert("RGB")
        pinkish = lambda px: px[0] > 245 and 215 <= px[1] <= 240 and 225 <= px[2] <= 248
        # Future P3/P4 live high on the chart. The top chart band should not be shaded pink.
        future_band_pixels = [img.getpixel((x, y)) for x in range(220, 1000, 40) for y in range(130, 360, 20)]
        self.assertLess(sum(1 for px in future_band_pixels if pinkish(px)), 30)
        # Active value-area region should contain many pink pixels.
        active_band_pixels = [img.getpixel((x, y)) for x in range(220, 1000, 40) for y in range(1265, 1335, 10)]
        self.assertGreater(sum(1 for px in active_band_pixels if pinkish(px)), 20)


if __name__ == "__main__":
    unittest.main()
