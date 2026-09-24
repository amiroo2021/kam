"""
WebTrade2 frontend order-side state regression tests.

Bug being prevented:
  The previous WebTrade2 code had no click handler for the ORDER BUY/SELL
  buttons, and previewOrder() derived side from the DOM (`.active` class on
  the .buy button). Because the .active class was only set in the initial
  HTML and never toggled, the order side was permanently 'buy' regardless
  of which button the user tapped.

Fix:
  - state.orderSide = 'buy' | 'sell' is the single authoritative state.
  - Click handlers on #orderBuy / #orderSell update state AND DOM in
    lockstep via setOrderSide(side).
  - previewOrder() reads state.orderSide — never the DOM.

These tests run a Node-based runner (`_webtrade2_frontend_runner.js`) that
loads the actual /root/kam/plugins/trade/webtrade2/static/index.html and
app.js into a minimal DOM stub and exercises:

  - Initial default BUY (state + .active class).
  - Tap SELL → state becomes 'sell', .active moves, ladder side unchanged.
  - Preview payload side matches state.
  - Confirmation modal contains "SELL ... LIMIT ..." summary.
  - Tap BUY → reverses state, preview payload side === 'buy'.
  - Changing side invalidates the existing preview (fresh preview call).
  - /api/trade/execute is NEVER called (live safety).
  - Mobile layout (data-mobile-target='trade') behaves identically.
"""

import os
import subprocess
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).parent
RUNNER = TESTS_DIR / "_webtrade2_frontend_runner.js"


class WebTrade2OrderSideStateTests(unittest.TestCase):
    """Frontend contract tests for the WebTrade2 ORDER BUY/SELL state machine.

    These tests do NOT exercise the live server. They run the Node-based
    runner against the real frontend source code (index.html + app.js) in
    an isolated DOM stub. The runner mocks /api/session, /api/phase2,
    /api/exchanges, /api/markets, /api/account_state, /api/positions_orders,
    and /api/trade/preview_order. It explicitly does NOT mock
    /api/trade/execute, and asserts that it is never called.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not RUNNER.exists():
            raise unittest.SkipTest(f"frontend runner missing: {RUNNER}")
        cls.node = subprocess.run(
            ["node", "--check", str(RUNNER)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if cls.node.returncode != 0:
            raise RuntimeError(
                f"frontend runner syntax check failed:\n"
                f"stdout: {cls.node.stdout}\nstderr: {cls.node.stderr}"
            )

    def test_frontend_runner_reports_pass(self) -> None:
        """The Node runner must report ALL PASS and produce no FAILURES.

        The runner covers every requirement in the bug report:
          - initial/default BUY → preview payload side=buy (covered)
          - tap SELL → state becomes sell (covered)
          - SELL becomes visually active and BUY inactive (covered)
          - preview after SELL sends side=sell (covered)
          - confirmation displays SELL / SELL BTC LIMIT (covered)
          - tap BUY again → preview sends side=buy (covered)
          - changing side invalidates an existing preview (covered)
          - works in the mobile layout as well as desktop (covered)
          - /api/trade/execute is NEVER called (live safety, covered)
        """
        result = subprocess.run(
            ["node", str(RUNNER)],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(TESTS_DIR),
        )
        combined = (result.stdout or "") + (result.stderr or "")
        # The runner prints "ALL PASS" at the end on success and exits 0.
        self.assertEqual(
            result.returncode, 0,
            f"frontend runner failed (rc={result.returncode}):\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}",
        )
        self.assertIn("ALL PASS", combined, "runner must print 'ALL PASS'")
        # No FAILURES line.
        self.assertNotIn("FAILURES:", combined)
        # No execute calls.
        self.assertRegex(
            combined, r"execute calls: 0",
            "live safety: /api/trade/execute must never be called during the test",
        )
        # Pass-count must include the side-correctness assertions.
        # Count any line that starts with "PASS:".
        import re
        passes = re.findall(r"^PASS:", combined, re.MULTILINE)
        self.assertGreaterEqual(
            len(passes), 25,
            f"expected >= 25 PASS assertions, got {len(passes)}:\n{combined}",
        )

    def test_app_js_node_syntax_check(self) -> None:
        """The shipped app.js must pass node --check (no syntax errors)."""
        app_js = Path("/root/kam/plugins/trade/webtrade2/static/app.js")
        self.assertTrue(app_js.exists(), f"app.js missing: {app_js}")
        result = subprocess.run(
            ["node", "--check", str(app_js)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(
            result.returncode, 0,
            f"app.js syntax error:\nstdout: {result.stdout}\nstderr: {result.stderr}",
        )

    def test_app_js_does_not_read_side_from_dom(self) -> None:
        """The frontend MUST NOT infer side from CSS classes or DOM state.

        This is a static guard: any code path that derives `side` from
        `.active` / `.buy` / `.sell` class membership is the bug we're
        preventing. The only authoritative source is `state.orderSide`.
        Comments are stripped before the check.
        """
        raw = Path("/root/kam/plugins/trade/webtrade2/static/app.js").read_text(encoding="utf-8")
        # Strip /* … */ block comments and // line comments before checking.
        import re
        no_block = re.sub(r"/\*[\s\S]*?\*/", "", raw)
        # Strip line comments only outside of string literals is hard; use
        # a simple heuristic: remove `// …` to end of line. (The ship-time
        # code does not embed `// …` in any string literal that we care
        # about for these checks.)
        no_line = re.sub(r"//[^\n]*", "", no_block)
        app_js = no_line
        bad_patterns = [
            # original bug: read side from .active class
            "$('.side-row .seg.buy.active')",
            # general anti-pattern: inferring order side from DOM
            "const side = ($('.side-row",
            "side ?= ($('.side-row",
        ]
        for needle in bad_patterns:
            self.assertNotIn(
                needle, app_js,
                f"app.js still contains DOM-as-state side read: {needle!r}",
            )
        # The authoritative read MUST exist.
        self.assertIn(
            "state.orderSide",
            raw,
            "app.js must read side from state.orderSide (single source of truth)",
        )
        # The setter MUST exist.
        self.assertIn(
            "function setOrderSide",
            raw,
            "app.js must define setOrderSide(side) to update both state and DOM",
        )
        # Direct click handlers on #orderBuy / #orderSell MUST exist.
        self.assertIn(
            "#orderBuy",
            raw,
            "app.js must wire a click handler on #orderBuy",
        )
        self.assertIn(
            "#orderSell",
            raw,
            "app.js must wire a click handler on #orderSell",
        )

    def test_app_js_ladder_side_independent(self) -> None:
        """The ladderSide state must be separate from orderSide."""
        app_js = Path("/root/kam/plugins/trade/webtrade2/static/app.js").read_text(encoding="utf-8")
        # Both states must be declared.
        self.assertIn("orderSide:", app_js, "state.orderSide must be declared in the state object")
        self.assertIn("ladderSide:", app_js, "state.ladderSide must be declared in the state object")
        # Setters must be distinct.
        self.assertIn("function setOrderSide", app_js)
        self.assertIn("function setLadderSide", app_js)
        # Ladder preview must read ladderSide (not orderSide).
        # previewLadderThenConfirm uses state.ladderSide directly.
        self.assertIn("state.ladderSide", app_js)

    def test_index_html_has_order_side_buttons_with_ids(self) -> None:
        """The HTML must expose the buttons with stable IDs so JS can wire them."""
        html = Path("/root/kam/plugins/trade/webtrade2/static/index.html").read_text(encoding="utf-8")
        self.assertIn('id="orderBuy"', html, "index.html must have #orderBuy button")
        self.assertIn('id="orderSell"', html, "index.html must have #orderSell button")
        self.assertIn('id="ladderBuy"', html, "index.html must have #ladderBuy button")
        self.assertIn('id="ladderSell"', html, "index.html must have #ladderSell button")


if __name__ == "__main__":
    unittest.main()
