"""
WebTrade2 chart-state regression tests.

Bug being prevented (real iPhone Safari findings):
  - Selecting a new instrument did not always clear the previous chart.
  - Slow Apex/HL/xyz candle responses could arrive AFTER the user
    switched markets and overwrite the new chart with stale data.
  - Failed candle requests (NVDA/QQQ/BZ/VVV on Apex, BZ/VVV on rise)
    must clear the chart, not leave the previous instrument's candles.
  - The price scale's autoscale state must reset between instruments,
    otherwise a BTC chart's scale could carry over into an XRP switch
    and produce wildly wrong visible prices.

These tests run a Node-based runner
(`_webtrade2_chart_state_runner.js`) that loads the real
/root/kam/plugins/trade/webtrade2/static/index.html and app.js into a
minimal DOM stub and exercises:

  1. BTC -> failed symbol -> BTC (recovery after failure)
  2. successful A -> successful B with very different price
  3. successful A -> failed B (must CLEAR, not leave A's candles)
  4. rapid A -> B -> C with slow A response arriving last
  5. timeframe change resets scale + data
  6. visible price range after switch matches the new candle range

The runner mocks /api/candles with per-test responses. It explicitly
does NOT mock /api/trade/execute and asserts it is never called.
"""

import re
import subprocess
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).parent
RUNNER = TESTS_DIR / "_webtrade2_chart_state_runner.js"
APP_JS = Path("/root/kam/plugins/trade/webtrade2/static/app.js")


class WebTrade2ChartStateTests(unittest.TestCase):
    """Frontend chart-state contract tests for WebTrade2.

    These do not exercise a live server. They run the Node-based runner
    against the real frontend source in an isolated DOM stub. The runner
    mocks /api/session, /api/phase2, /api/exchanges, /api/markets,
    /api/market_price, /api/account_state, /api/positions_orders, and
    /api/candles with controllable per-test responses.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not RUNNER.exists():
            raise unittest.SkipTest(f"frontend runner missing: {RUNNER}")
        # Syntax check both the runner and the shipped app.js.
        for label, path in [("runner", RUNNER), ("app.js", APP_JS)]:
            res = subprocess.run(
                ["node", "--check", str(path)],
                capture_output=True, text=True, timeout=15,
            )
            if res.returncode != 0:
                raise RuntimeError(
                    f"{label} syntax check failed:\n"
                    f"stdout: {res.stdout}\nstderr: {res.stderr}"
                )

    def test_runner_reports_all_pass(self) -> None:
        """All chart-state scenarios must pass.

        Covers every requirement in the user's bug report:
          1. BTC -> failed symbol -> BTC recovery
          2. successful A -> successful B with radically different price
          3. successful A -> failed B clears old candles
          4. rapid A -> B -> C with slow A arriving last
          5. timeframe change resets scale + data
          6. visible price range after switch matches new candles
          7. zero /api/trade/execute calls (live safety)
        """
        result = subprocess.run(
            ["node", str(RUNNER)],
            capture_output=True, text=True, timeout=120,
            cwd=str(TESTS_DIR),
        )
        combined = (result.stdout or "") + (result.stderr or "")
        self.assertEqual(
            result.returncode, 0,
            f"chart-state runner failed (rc={result.returncode}):\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}",
        )
        self.assertIn("ALL PASS", combined, "runner must print 'ALL PASS'")
        self.assertNotIn("FAIL:", combined, "no FAIL assertions allowed")
        # Live safety: zero /api/trade/execute calls during the test.
        self.assertRegex(
            combined, r"LIVE SAFETY: zero /api/trade/execute calls",
            "live safety assertion must be present",
        )

    def test_app_js_does_not_drop_generation_mechanism(self) -> None:
        """The chart-load generation / request-id staleness system must
        exist in app.js. This is the static guard for the core fix.

        The runner tests the runtime behavior; this test pins the source
        so a future refactor can't silently remove the staleness check.
        """
        raw = APP_JS.read_text(encoding="utf-8")
        # Generation counter on state.
        self.assertIn("chartLoadGen", raw,
                      "app.js must track a chartLoadGen counter on state")
        # Bump on every new load.
        self.assertRegex(
            raw, r"chartLoadGen\s*=\s*\([^)]*\|\|\s*0\)\s*\+\s*1",
            "app.js must bump chartLoadGen on every loadChartHistory() call",
        )
        # Both loadChartHistory() AND the polling function must check
        # staleness before applying data.
        self.assertRegex(
            raw, r"function\s+loadChartHistory[\s\S]*?myGen\s*!==\s*state\.chartLoadGen",
            "loadChartHistory must capture myGen and check staleness",
        )
        self.assertRegex(
            raw, r"function\s+updateLatestCandle[\s\S]*?myGen\s*!==\s*state\.chartLoadGen",
            "updateLatestCandle must also capture myGen and check staleness",
        )
        # The chart must be cleared on failure / before the new data is
        # applied, not left showing the previous instrument.
        self.assertRegex(
            raw, r"setData\(\[\]\)",
            "app.js must call setData([]) to clear stale candles",
        )
        # Failure path must show a message AND never re-show previous
        # candles.
        self.assertIn("showChartMessage", raw,
                      "app.js must define showChartMessage() for chart error UX")
        # Telemetry / debug surface must NOT be in the production build.
        # (Removed after iPhone Safari visual verification passed; the
        # underlying staleness + clear-old-chart protections remain.)
        self.assertNotIn("renderChartTelemetry", raw,
                         "app.js must not render a telemetry strip in production")
        self.assertNotIn("exposeChartTelemetry", raw,
                         "app.js must not expose window.__webtrade2_chart__ in production")
        self.assertNotIn("__webtrade2_chart__", raw,
                         "app.js must not reference window.__webtrade2_chart__")
        # Price scale must be reset to autoScale after setData.
        self.assertRegex(
            raw, r"priceScale\([^)]*\)\.applyOptions\(\{\s*autoScale:\s*true",
            "app.js must call priceScale().applyOptions({ autoScale: true }) to reset price scale",
        )

    def test_selectMarket_bumps_generation_before_awaits(self) -> None:
        """selectMarket() must bump chartLoadGen BEFORE any await so an
        in-flight loadChartHistory() / poll is immediately stale.

        Without this bump-before-await, a selectMarket() that awaits
        market_price would race a parallel loadChartHistory().
        """
        raw = APP_JS.read_text(encoding="utf-8")
        # Locate selectMarket and assert the gen bump is near the top.
        m = re.search(
            r"async\s+function\s+selectMarket\s*\([^)]*\)\s*\{(.{0,400})\}",
            raw, re.DOTALL,
        )
        self.assertIsNotNone(m, "selectMarket function must exist")
        head = m.group(1)
        self.assertIn("chartLoadGen", head,
                      "selectMarket must bump chartLoadGen near the top "
                      "before any await")

    def test_setTimeframe_bumps_generation(self) -> None:
        """setTimeframe() must bump chartLoadGen so a previous
        timeframe's in-flight candle load is dropped.
        """
        raw = APP_JS.read_text(encoding="utf-8")
        self.assertRegex(
            raw,
            r"function\s+setTimeframe\s*\([^)]*\)\s*\{[^}]*chartLoadGen[^}]*\}",
            "setTimeframe must bump chartLoadGen",
        )


if __name__ == "__main__":
    unittest.main()
