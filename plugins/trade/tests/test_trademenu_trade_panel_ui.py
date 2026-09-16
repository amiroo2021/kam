"""Static checks for TradeMenu Single/Ladder panel separation."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "trademenu" / "static"


class TradePanelModeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "index.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "style.css").read_text(encoding="utf-8")
        cls.js = (ROOT / "app.js").read_text(encoding="utf-8")

    def test_single_and_ladder_forms_are_separate(self) -> None:
        self.assertIn('id="singleForm"', self.html)
        self.assertIn('id="ladderForm"', self.html)
        self.assertIn('data-mode-panel="single"', self.html)
        self.assertIn('data-mode-panel="ladder"', self.html)
        # Ladder starts hidden in markup
        self.assertRegex(self.html, r'id="ladderForm"[^>]*\bhidden\b')

    def test_ladder_field_order(self) -> None:
        # Extract ladder form block
        m = re.search(r'id="ladderForm".*?</div>\s*\n\s*<div id="previewBox"', self.html, re.S)
        self.assertIsNotNone(m)
        block = m.group(0)
        positions = [
            block.find("Start Price"),
            block.find("End Price"),
            block.find(">Orders<") if ">Orders<" in block else block.find("Orders"),
            block.find("Total Size"),
            block.find("Distribution"),
            block.find("Preview Ladder"),
        ]
        self.assertTrue(all(p >= 0 for p in positions), positions)
        self.assertEqual(positions, sorted(positions))

    def test_css_hidden_overrides_display_grid(self) -> None:
        self.assertIn(".trade-form[hidden]", self.css)
        self.assertIn("display: none !important", self.css)

    def test_js_mode_visibility_and_validation(self) -> None:
        self.assertIn("function applyModeVisibility", self.js)
        self.assertIn("function setTradeMode", self.js)
        self.assertIn('tradeMode !== "single"', self.js)
        self.assertIn('tradeMode !== "ladder"', self.js)
        # Single preview must not require ladder fields
        self.assertIn("Enter Limit Price and Size", self.js)
        self.assertIn("Enter Start, End, Orders, and Total Size", self.js)
        # Mode switch clears preview lines
        self.assertIn("clearPreviewLines()", self.js)
        self.assertIn("updateSingleNotional", self.js)

    def test_size_semantics_labels(self) -> None:
        self.assertIn("this order only", self.html)
        self.assertIn("distributed across children", self.html)
        self.assertIn('id="orderSize"', self.html)
        self.assertIn('id="ladderTotal"', self.html)
        self.assertNotEqual(
            re.search(r'id="orderSize"', self.html).start(),
            re.search(r'id="ladderTotal"', self.html).start(),
        )


if __name__ == "__main__":
    unittest.main()
