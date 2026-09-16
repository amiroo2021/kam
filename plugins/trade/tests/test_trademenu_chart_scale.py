"""TradeMenu chart price-scale reset on instrument switch."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCALE_JS = ROOT / "plugins/trade/trademenu/static/chart_scale.js"
APP_JS = ROOT / "plugins/trade/trademenu/static/app.js"
INDEX_HTML = ROOT / "plugins/trade/trademenu/static/index.html"


def _node(expr: str) -> str:
    script = f"""
const m = require({str(SCALE_JS)!r});
const result = ({expr});
process.stdout.write(typeof result === 'string' ? result : JSON.stringify(result));
"""
    proc = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


class ChartScaleHelperTests(unittest.TestCase):
    def test_should_reset_sp500_to_eth(self) -> None:
        out = _node('m.shouldResetPriceScale("xyz:SP500", "ETH")')
        self.assertEqual(out, "true")

    def test_should_not_reset_same_instrument(self) -> None:
        out = _node('m.shouldResetPriceScale("ETH", "ETH")')
        self.assertEqual(out, "false")
        out2 = _node('m.shouldResetPriceScale("eth", "ETH")')
        self.assertEqual(out2, "false")

    def test_first_paint_resets(self) -> None:
        out = _node('m.shouldResetPriceScale(null, "ETH")')
        self.assertEqual(out, "true")

    def test_btc_eth_extreme_ranges(self) -> None:
        plan = _node(
            """m.planInstrumentRender("BTC", "ETH", [
              {low: 2380, high: 2420},
              {low: 2390, high: 2410}
            ])"""
        )
        import json

        data = json.loads(plan)
        self.assertTrue(data["instrumentChanged"])
        self.assertTrue(data["replaceSeries"])
        self.assertTrue(data["forceAutoscale"])
        self.assertFalse(data["preserveUserZoom"])
        self.assertIsNotNone(data["visibleRange"])
        self.assertLess(data["visibleRange"]["to"], 5000)
        self.assertGreater(data["visibleRange"]["from"], 2000)
        self.assertEqual(data["steps"][0], "clear_overlays")
        self.assertIn("replace_candle_series", data["steps"])
        self.assertIn("setData", data["steps"])
        self.assertIn("autoScale_true", data["steps"])
        self.assertIn("fitContent", data["steps"])

    def test_sp500_plan_range_does_not_include_eth(self) -> None:
        import json

        sp = _node(
            """m.planInstrumentRender(null, "xyz:SP500", [
              {low: 7450, high: 7775},
              {low: 7500, high: 7600}
            ])"""
        )
        eth = _node(
            """m.planInstrumentRender("xyz:SP500", "ETH", [
              {low: 2380, high: 2425},
              {low: 2395, high: 2415}
            ])"""
        )
        sp_d = json.loads(sp)
        eth_d = json.loads(eth)
        self.assertGreater(sp_d["visibleRange"]["from"], 7000)
        self.assertLess(eth_d["visibleRange"]["to"], 3000)
        # Ranges must not overlap — proves scale cannot stay on SP500 for ETH.
        self.assertLess(eth_d["visibleRange"]["to"], sp_d["visibleRange"]["from"])

    def test_same_symbol_refresh_preserves_zoom_flag(self) -> None:
        import json

        plan = _node(
            """m.planInstrumentRender("ETH", "ETH", [
              {low: 2380, high: 2420}
            ])"""
        )
        data = json.loads(plan)
        self.assertFalse(data["instrumentChanged"])
        self.assertFalse(data["replaceSeries"])
        self.assertTrue(data["preserveUserZoom"])
        self.assertIsNone(data["visibleRange"])

    def test_eth_to_btc_and_zeec(self) -> None:
        import json

        btc = json.loads(
            _node(
                """m.planInstrumentRender("ETH", "BTC", [
                  {low: 75000, high: 77000}
                ])"""
            )
        )
        zec = json.loads(
            _node(
                """m.planInstrumentRender("ETH", "PERP_ZEC_USDC", [
                  {low: 1200, high: 1400}
                ])"""
            )
        )
        self.assertTrue(btc["forceAutoscale"])
        self.assertGreater(btc["visibleRange"]["from"], 60000)
        self.assertTrue(zec["forceAutoscale"])
        self.assertLess(zec["visibleRange"]["to"], 2000)


class AppWiringTests(unittest.TestCase):
    def test_index_loads_chart_scale_and_v4_2(self) -> None:
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn("lightweight-charts@4.2.0", html)
        self.assertIn("/static/chart_scale.js", html)

    def test_app_uses_instrument_change_sequence(self) -> None:
        src = APP_JS.read_text(encoding="utf-8")
        self.assertIn("shouldResetPriceScale", src)
        self.assertIn("replaceCandleSeries", src)
        self.assertIn("applyPriceScaleAfterSetData", src)
        self.assertIn("chartRenderedNative", src)
        self.assertIn("activateInstrument", src)
        # Locate the successful candle render block (setData(candles) call site).
        i_set = src.find("candleSeries.setData(candles)")
        self.assertGreater(i_set, 0)
        window = src[i_set - 800 : i_set + 900]
        self.assertIn("instrumentChanged", window)
        self.assertIn("replaceCandleSeries()", window)
        self.assertIn("applyPriceScaleAfterSetData(candles", window)
        self.assertIn("clearPositionLines()", window)
        # Sequence inside the window: clear → replace → setData → scale
        i_clear = window.find("clearPositionLines()")
        i_replace = window.find("replaceCandleSeries()")
        i_set_local = window.find("candleSeries.setData(candles)")
        i_scale = window.find("applyPriceScaleAfterSetData(candles")
        self.assertGreater(i_replace, i_clear)
        self.assertGreater(i_set_local, i_replace)
        self.assertGreater(i_scale, i_set_local)
        # updateOverlay must still run after the setData block in loadCandles
        i_overlay_after = src.find("updateOverlay()", i_set)
        self.assertGreater(i_overlay_after, i_set)


if __name__ == "__main__":
    unittest.main()
