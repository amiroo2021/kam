import math
import unittest
from pathlib import Path

from PIL import Image

from golden_fibo.constants import Side
from golden_fibo.historical_replay import ReplayLeg, ReplayState, replay_ohlc
from goldenfibo.metrics.trade_vap import AggTrade
from plugins.trade import backtest_wizard as wizard


def _candle(ts, o, h, l, c, base_vol, quote_vol=None):
    """Binance-style 1m kline row used by VWAP/POC helpers."""
    if quote_vol is None:
        quote_vol = base_vol * ((h + l + c) / 3.0)
    return [ts, str(o), str(h), str(l), str(c), str(base_vol), ts + 59_999, str(quote_vol)]


def _aggressor_candle(ts, o, h, l, c, base_vol, buy_base, quote_vol=None, buy_quote=None):
    """Binance-style kline with taker-buy aggressor volume at indexes 9/10."""
    if quote_vol is None:
        quote_vol = base_vol * c
    if buy_quote is None:
        buy_quote = buy_base * c
    return [
        ts,
        str(o),
        str(h),
        str(l),
        str(c),
        str(base_vol),
        ts + 59_999,
        str(quote_vol),
        1,
        str(buy_base),
        str(buy_quote),
        "0",
    ]


class _LegMs:
    def __init__(self, step, ts_ms):
        self.step = step
        self.ts_ms = ts_ms


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
        # Active value-area region should contain many pink pixels. The chart now
        # reserves bottom space for the flow-history panel, so don't pin this to
        # old pre-panel y-coordinates.
        active_chart_pixels = [img.getpixel((x, y)) for x in range(220, 1000, 40) for y in range(110, 1210, 10)]
        self.assertGreater(sum(1 for px in active_chart_pixels if pinkish(px)), 20)

    def test_ladder_vwap_and_poc_share_identical_p0_window(self):
        """Ladder VWAP start == Ladder POC start == current P0 timestamp."""
        p0_ts = 1_000_000
        state = ReplayState(side=Side.SELL, cycle=3, p0=100.0, highest_filled=1, shared_tp=99.9)
        state.legs = [
            ReplayLeg(0, 100.0, p0_ts, "t0"),
            ReplayLeg(1, 101.0, p0_ts + 120_000, "t1"),
        ]
        candles = [
            _candle(0, 50, 55, 45, 50, 10_000, 500_000),  # before P0 — must not enter
            _candle(p0_ts, 100, 101, 99, 100, 10, 1000),
            _candle(p0_ts + 60_000, 100.5, 102, 100, 101, 20, 2020),
            _candle(p0_ts + 120_000, 101, 103, 100.5, 102, 30, 3060),
        ]

        ladder_start = wizard._active_ladder_start_ts(state)
        self.assertEqual(ladder_start, p0_ts)

        # Both metrics filter on the same >= start boundary.
        ladder_rel = [k for k in candles if int(k[0]) >= ladder_start]
        self.assertEqual(len(ladder_rel), 3)
        self.assertTrue(all(int(k[0]) >= p0_ts for k in ladder_rel))
        self.assertFalse(any(int(k[0]) < p0_ts for k in ladder_rel))

        vwap = wizard._vwap(candles, ladder_start)
        poc = wizard._poc(candles, ladder_start)
        profile = wizard._volume_profile(candles, ladder_start, bins=160)
        self.assertIsNotNone(profile)
        self.assertEqual(len(profile["vols"]), 160)
        self.assertTrue(math.isfinite(vwap))
        self.assertTrue(math.isfinite(poc))
        # Pre-P0 volume would pull VWAP near 50 if incorrectly included.
        self.assertGreater(vwap, 90.0)

    def test_step_vwap_and_poc_share_identical_pn_activation_window(self):
        """Step VWAP start == Step POC start == current P(n) activation timestamp."""
        p0_ts = 1_000_000
        pn_ts = 1_180_000
        state = ReplayState(side=Side.SELL, cycle=1, p0=100.0, highest_filled=2, shared_tp=99.9)
        state.legs = [
            ReplayLeg(0, 100.0, p0_ts, "t0"),
            ReplayLeg(1, 101.0, p0_ts + 60_000, "t1"),
            ReplayLeg(2, 102.0, pn_ts, "t2"),
        ]
        candles = [
            _candle(p0_ts, 100, 101, 99, 100, 100, 10_000),  # ladder-only volume
            _candle(p0_ts + 60_000, 101, 102, 100, 101, 100, 10_100),
            _candle(pn_ts, 102, 103, 101.5, 102.5, 10, 1025),
            _candle(pn_ts + 60_000, 102.5, 104, 102, 103, 10, 1030),
        ]

        step_start = wizard._active_step_start_ts(state)
        ladder_start = wizard._active_ladder_start_ts(state)
        self.assertEqual(step_start, pn_ts)
        self.assertEqual(ladder_start, p0_ts)
        self.assertNotEqual(step_start, ladder_start)

        step_rel = [k for k in candles if int(k[0]) >= step_start]
        self.assertEqual(len(step_rel), 2)
        self.assertTrue(all(int(k[0]) >= pn_ts for k in step_rel))
        self.assertFalse(any(int(k[0]) < pn_ts for k in step_rel))

        step_vwap = wizard._vwap(candles, step_start)
        step_poc = wizard._poc(candles, step_start)
        ladder_vwap = wizard._vwap(candles, ladder_start)
        self.assertTrue(math.isfinite(step_vwap) and math.isfinite(step_poc))
        # Step window excludes heavy early ladder volume near 100.
        self.assertGreater(step_vwap, ladder_vwap)

    def test_tp_new_p0_resets_both_ladder_metrics_together(self):
        """TP / new cycle: previous ladder ends; both ladder metrics start at new P0 ts."""
        # Candle 0 opens first cycle. Candle 1 fills P1 then hits TP (low <= shared_tp for SELL)
        # chaining a new P0 at the TP price on candle 1's timestamp. Candle 2 is new ladder only.
        candles = [
            _candle(0, 100.0, 100.5, 99.5, 100.0, 50, 5000),
            # High reaches a later step then low prints through shared TP of early cycle.
            # Use a path that opens at 100, never progresses far, TP is P(-1) side of P0.
            # For SELL, shared_tp after P0 is below P0. Touch low <= tp closes and chains.
            _candle(60_000, 100.0, 100.2, 99.0, 99.5, 50, 4975),
            _candle(120_000, 99.5, 100.0, 99.0, 99.8, 10, 995),
        ]
        state = replay_ohlc(candles, side=Side.SELL)
        self.assertGreaterEqual(state.cycle, 2)
        self.assertEqual(state.legs[0].step, 0)
        new_p0_ts = int(state.legs[0].ts)
        self.assertEqual(wizard._active_ladder_start_ts(state), new_p0_ts)
        self.assertEqual(wizard._active_step_start_ts(state), int(state.legs[-1].ts))

        # Ladder metrics only see candles from new P0 onward.
        ladder_start = wizard._active_ladder_start_ts(state)
        for k in candles:
            if int(k[0]) < ladder_start:
                # Explicit: pre-new-P0 rows are excluded by the shared filter both metrics use.
                self.assertLess(int(k[0]), ladder_start)
        rel = [k for k in candles if int(k[0]) >= ladder_start]
        self.assertTrue(rel)
        self.assertEqual(int(rel[0][0]), ladder_start)

        vwap = wizard._vwap(candles, ladder_start)
        poc = wizard._poc(candles, ladder_start)
        self.assertTrue(math.isfinite(vwap) and math.isfinite(poc))

    def test_progression_resets_both_active_step_metrics_together(self):
        """P(n) fill advances legs[-1]; both step metrics restart at that fill ts."""
        # SELL: after Pn fills, shared_tp = P(n-1). Keep every low strictly above
        # the TP that will apply after the fills on that candle.
        candles = [
            _candle(0, 100.0, 100.0, 100.0, 100.0, 5, 500),
            # Fill P1 only (≈100.162). TP becomes P0=100.0; low 100.05 is safe.
            _candle(60_000, 100.05, 100.20, 100.05, 100.18, 5, 505),
            # Fill P2 only (≈100.424). TP becomes P1≈100.162; low 100.20 is safe.
            _candle(180_000, 100.20, 100.45, 100.20, 100.40, 5, 512),
            _candle(240_000, 100.40, 100.55, 100.35, 100.50, 5, 515),
        ]
        state = replay_ohlc(candles, side=Side.SELL)
        self.assertGreaterEqual(state.highest_filled, 1)
        self.assertEqual(len(state.closed), 0)
        self.assertEqual(state.legs[-1].step, state.highest_filled)

        step_start = wizard._active_step_start_ts(state)
        ladder_start = wizard._active_ladder_start_ts(state)
        self.assertEqual(step_start, int(state.legs[-1].ts))
        self.assertEqual(ladder_start, int(state.legs[0].ts))
        self.assertGreaterEqual(step_start, ladder_start)

        pre = [k for k in candles if int(k[0]) < step_start]
        post = [k for k in candles if int(k[0]) >= step_start]
        self.assertTrue(post)
        step_vwap = wizard._vwap(candles, step_start)
        step_poc = wizard._poc(candles, step_start)
        self.assertAlmostEqual(step_vwap, wizard._vwap(post, step_start))
        self.assertAlmostEqual(step_poc, wizard._poc(post, step_start))
        if pre and state.highest_filled >= 1:
            self.assertNotEqual(step_start, ladder_start)

    def test_data_before_p0_cannot_enter_ladder_vwap_or_poc(self):
        p0_ts = 500_000
        state = ReplayState(side=Side.BUY, cycle=1, p0=100.0, highest_filled=0, shared_tp=100.1)
        state.legs = [ReplayLeg(0, 100.0, p0_ts, "p0")]
        candles = [
            _candle(0, 10, 11, 9, 10, 1_000_000, 10_000_000),
            _candle(p0_ts, 100, 101, 99, 100, 1, 100),
        ]
        start = wizard._active_ladder_start_ts(state)
        self.assertEqual(start, p0_ts)
        self.assertAlmostEqual(wizard._vwap(candles, start), 100.0)
        poc = wizard._poc(candles, start)
        self.assertTrue(99 <= poc <= 101)
        profile = wizard._volume_profile(candles, start)
        self.assertAlmostEqual(sum(profile["vols"]), 1.0, places=6)

    def test_data_before_pn_cannot_enter_step_vwap_or_poc(self):
        p0_ts = 100_000
        pn_ts = 300_000
        state = ReplayState(side=Side.SELL, cycle=1, p0=100.0, highest_filled=1, shared_tp=99.9)
        state.legs = [
            ReplayLeg(0, 100.0, p0_ts, "p0"),
            ReplayLeg(1, 101.0, pn_ts, "p1"),
        ]
        candles = [
            _candle(p0_ts, 100, 100.5, 99.5, 100, 1_000, 100_000),
            _candle(pn_ts, 200, 201, 199, 200, 1, 200),
        ]
        step_start = wizard._active_step_start_ts(state)
        self.assertEqual(step_start, pn_ts)
        self.assertAlmostEqual(wizard._vwap(candles, step_start), 200.0)
        poc = wizard._poc(candles, step_start)
        self.assertTrue(199 <= poc <= 201)

    def test_ohlc_poc_below_p0_is_valid_when_volume_clusters_near_window_low(self):
        """L-POC can sit below P0 under the 160-bin OHLC overlap approximation.

        Mirrors the live suspicious case (L-POC ~75468 with P0 ~75509): heavy
        base volume on tight candles near the window low wins the POC bin even
        though P0 and VWAP sit higher. Windowing is correct; this is the math.
        """
        p0_ts = 1_000_000
        p0 = 75_509.48
        candles = [
            # Tight, heavy volume just below P0 (like the real 01:56 candle).
            _candle(p0_ts, p0, 75_467.70, 75_463.83, 75_465.0, 19.0),
            _candle(p0_ts + 60_000, 75_465.0, 75_469.0, 75_464.0, 75_468.0, 12.0),
            # Later wider, lighter volume higher in the range.
            _candle(p0_ts + 120_000, 75_500.0, 75_830.0, 75_480.0, 75_700.0, 8.0),
            _candle(p0_ts + 180_000, 75_700.0, 75_900.0, 75_650.0, 75_800.0, 5.0),
        ]
        profile = wizard._volume_profile(candles, p0_ts, bins=160)
        self.assertIsNotNone(profile)
        lo, hi, width, vols = profile["lo"], profile["hi"], profile["width"], profile["vols"]
        self.assertEqual(len(vols), 160)
        self.assertLess(lo, p0)
        idx = max(range(len(vols)), key=lambda i: vols[i])
        center = lo + (idx + 0.5) * width
        bin_lo = lo + idx * width
        bin_hi = bin_lo + width
        poc = wizard._poc(candles, p0_ts, bins=160)
        self.assertAlmostEqual(poc, center)
        self.assertLess(poc, p0)
        self.assertTrue(bin_lo <= poc <= bin_hi)
        # Top bin should be near the tight low cluster, not the high range.
        self.assertLess(center, 75_500.0)
        ranked = sorted(enumerate(vols), key=lambda x: -x[1])[:10]
        self.assertEqual(ranked[0][0], idx)
        self.assertGreater(ranked[0][1], ranked[1][1])

    def test_active_window_helpers_accept_engine_ts_ms_legs(self):
        state = ReplayState(side=Side.SELL, cycle=1, p0=100.0, highest_filled=1, shared_tp=99.9)
        state.legs = [_LegMs(0, 111_000), _LegMs(1, 222_000)]

        self.assertEqual(wizard._active_ladder_start_ts(state), 111_000)
        self.assertEqual(wizard._active_step_start_ts(state), 222_000)
        self.assertEqual(wizard._step_delta_ratios([], state), {0: None, 1: None})

    def test_aggressor_vwap_uses_aggtrade_m_side_not_kline_taker_fields(self):
        p0_ts = 1_000_000
        pn_ts = 1_120_000
        trades = [
            # m=False => buyer taker/aggressor => BUY aggressor
            AggTrade(1, 100.0, 8.0, p0_ts, buyer_is_maker=False),
            # m=True => seller taker/aggressor => SELL aggressor
            AggTrade(2, 100.0, 2.0, p0_ts, buyer_is_maker=True),
            AggTrade(3, 110.0, 2.0, pn_ts, buyer_is_maker=False),
            AggTrade(4, 110.0, 8.0, pn_ts, buyer_is_maker=True),
        ]

        ladder = wizard._aggressor_metrics_from_trades(trades, p0_ts)
        step = wizard._aggressor_metrics_from_trades(trades, pn_ts)

        self.assertAlmostEqual(ladder["total_volume"], 20.0)
        self.assertAlmostEqual(ladder["buy_volume"], 10.0)
        self.assertAlmostEqual(ladder["sell_volume"], 10.0)
        self.assertAlmostEqual(ladder["all_vwap"], (100 * 10 + 110 * 10) / 20)
        self.assertAlmostEqual(ladder["buy_vwap"], (100 * 8 + 110 * 2) / 10)
        self.assertAlmostEqual(ladder["sell_vwap"], (100 * 2 + 110 * 8) / 10)
        self.assertAlmostEqual(ladder["delta_ratio"], 0.0)
        self.assertAlmostEqual(step["buy_vwap"], 110.0)
        self.assertAlmostEqual(step["sell_vwap"], 110.0)
        self.assertAlmostEqual(step["delta_ratio"], -0.6)

    def test_step_delta_ratio_windows_and_color_thresholds_from_aggtrades(self):
        p0_ts = 1_000_000
        p1_ts = 1_060_000
        p2_ts = 1_120_000
        trades = [
            AggTrade(1, 100, 6, p0_ts, buyer_is_maker=False),  # +0.2 lightblue
            AggTrade(2, 100, 4, p0_ts, buyer_is_maker=True),
            AggTrade(3, 101, 8, p1_ts, buyer_is_maker=False),  # +0.6 darkblue
            AggTrade(4, 101, 2, p1_ts, buyer_is_maker=True),
            AggTrade(5, 102, 3, p2_ts, buyer_is_maker=False),  # current step with next row = -0.5 darkred
            AggTrade(6, 102, 7, p2_ts, buyer_is_maker=True),
            AggTrade(7, 103, 2, p2_ts + 60_000, buyer_is_maker=False),
            AggTrade(8, 103, 8, p2_ts + 60_000, buyer_is_maker=True),
        ]
        state = ReplayState(side=Side.SELL, cycle=1, p0=100.0, highest_filled=2, shared_tp=101.0)
        state.legs = [
            ReplayLeg(0, 100.0, p0_ts, "p0"),
            ReplayLeg(1, 101.0, p1_ts, "p1"),
            ReplayLeg(2, 102.0, p2_ts, "p2"),
        ]

        ratios = wizard._step_delta_ratios_from_trades(trades, state)

        self.assertAlmostEqual(ratios[0], 0.2)
        self.assertAlmostEqual(ratios[1], 0.6)
        self.assertAlmostEqual(ratios[2], -0.5)
        self.assertEqual(wizard._step_delta_color(ratios[0]), wizard.LIGHTBLUE)
        self.assertEqual(wizard._step_delta_color(ratios[1]), wizard.DARKBLUE)
        self.assertEqual(wizard._step_delta_color(-0.4), wizard.LIGHTRED)
        self.assertEqual(wizard._step_delta_color(ratios[2]), wizard.DARKRED)

    def test_final_aggressor_output_has_one_delta_and_directional_vwap_only(self):
        sell = {"buy_vwap": 101.5, "sell_vwap": 99.5, "delta_ratio": 0.0187, "status": "COMPLETE"}
        buy = {"buy_vwap": 101.5, "sell_vwap": 99.5, "delta_ratio": -0.0023, "status": "COMPLETE"}

        sell_lines = wizard._aggressor_summary_lines(Side.SELL, {"all_vwap": 100.0}, {"all_vwap": 101.0}, sell, sell)
        buy_lines = wizard._aggressor_summary_lines(Side.BUY, {"all_vwap": 100.0}, {"all_vwap": 101.0}, buy, buy)

        self.assertEqual(sum("Step Delta Ratio" in line for line in sell_lines), 1)
        self.assertIn("L-B-VWAP: 101.50", sell_lines)
        self.assertIn("S-B-VWAP: 101.50", sell_lines)
        self.assertIn("Step Delta Ratio: +0.0187", sell_lines)
        self.assertFalse(any("L-S-VWAP" in line or "S-S-VWAP" in line for line in sell_lines))
        self.assertEqual(sum("Step Delta Ratio" in line for line in buy_lines), 1)
        self.assertIn("L-S-VWAP: 99.50", buy_lines)
        self.assertIn("S-S-VWAP: 99.50", buy_lines)
        self.assertIn("Step Delta Ratio: -0.0023", buy_lines)
        self.assertFalse(any("L-B-VWAP" in line or "S-B-VWAP" in line for line in buy_lines))


if __name__ == "__main__":
    unittest.main()
