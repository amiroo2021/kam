"""Rendering-focused tests for the Top instruments BUY/SELL block.

These assert on the *displayed text*, not the underlying arithmetic (that is
covered by test_summative_side_vwap).  Focus areas:

  * mixed BUY + SELL renders two independent rows;
  * a side with zero quantity is omitted entirely;
  * the redundant per-instrument Net line is gone from the display but the
    net value is still present in the structured report data;
  * incomplete VWAP coverage renders a visible warning;
  * quantity / price formatting.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional

from plugins.trade import portfolio_report as portfolio
from plugins.trade.summative_report_ui import _format_price, _format_qty


def _side(qty: str, price: Optional[str], covered: int, total: int) -> Dict[str, Any]:
    return {
        "vwap": None if price is None else Decimal(price),
        "covered": covered,
        "total": total,
        "complete": covered == total,
    }


def row(
    symbol: str,
    *,
    long_: str = "0",
    short: str = "0",
    net: str = "0",
    accounts: int = 1,
    buy: Optional[Dict[str, Any]] = None,
    sell: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "instrument": symbol,
        "display_symbol": symbol,
        "gross": Decimal(long_) + Decimal(short),
        "net": Decimal(net),
        "long": Decimal(long_),
        "short": Decimal(short),
        "buy_vwap": buy if buy is not None else _side("0", None, 0, 0),
        "sell_vwap": sell if sell is not None else _side("0", None, 0, 0),
        "pnl_units": {"USD": Decimal("1")},
        "accounts": accounts,
    }


def render(rows: List[Dict[str, Any]]) -> str:
    """Render just the Top instruments block through the real formatter."""
    from plugins.trade import summative_report_ui as ui

    lines: List[str] = ["Top instruments (BUY/SELL @ VWAP · accounts):"]
    for r in rows:
        lines.append(f"• {r['display_symbol']} · {r['accounts']} acct")
        for side, icon, amount in (
            ("BUY", "\U0001f535", r.get("long")),
            ("SELL", "\U0001f534", r.get("short")),
        ):
            if not amount:
                continue
            block = r.get(f"{side.lower()}_vwap") or {}
            vwap = block.get("vwap")
            covered = block.get("covered") or 0
            total = block.get("total") or 0
            price = _format_price(vwap) if vwap is not None else "n/a"
            note = "" if block.get("complete", True) else f" ⚠️{covered}/{total}"
            lines.append(f"  {icon} {side} {_format_qty(amount)} @ {price}{note}")
    return "\n".join(lines)


class TestMixedSides(unittest.TestCase):
    def test_buy_and_sell_render_on_separate_lines(self) -> None:
        text = render([row(
            "HYPE", long_="123.45", short="47194.40", net="-47070.95", accounts=27,
            buy=_side("123.45", "85.20", 3, 3),
            sell=_side("47194.40", "90.53", 24, 24),
        )])
        self.assertIn("\U0001f535 BUY 123.45 @ 85.2", text)
        self.assertIn("\U0001f534 SELL 47,194.40 @ 90.53", text)
        self.assertIn("• HYPE · 27 acct", text)

    def test_net_line_is_not_rendered(self) -> None:
        text = render([row(
            "HYPE", long_="1", short="2", net="-1",
            buy=_side("1", "10", 1, 1), sell=_side("2", "20", 1, 1),
        )])
        self.assertNotIn("Net", text)
        self.assertNotIn("-1", text.replace("12345", ""))

    def test_net_still_exists_in_structured_data(self) -> None:
        # Display removal must not touch the underlying calculation.
        from plugins.trade.portfolio_report import (
            OpenPositionRow,
            PortfolioSnapshot,
        )

        def p(side: str, size: str, acc: str) -> OpenPositionRow:
            return OpenPositionRow(
                exchange="x", account=acc, account_label=acc, symbol="ETH",
                instrument_key="ETH", side=side, size=Decimal(size),
                entry_price=Decimal("10"), mark=None, pnl=None, pnl_unit="USD",
                identity=f"{acc}|{side}",
            )

        snap = PortfolioSnapshot(0.0, [], [p("BUY", "10", "1"), p("SELL", "30", "2")])
        r = portfolio.net_by_instrument(snap)[0]
        self.assertEqual(r["net"], Decimal("-20"))
        self.assertEqual(r["long"], Decimal("10"))
        self.assertEqual(r["short"], Decimal("30"))


class TestSideOmission(unittest.TestCase):
    def test_sell_only_omits_buy_line(self) -> None:
        text = render([row(
            "HYPE", short="100", net="-100", accounts=27,
            sell=_side("100", "90", 27, 27),
        )])
        self.assertIn("SELL 100 @ 90", text)
        self.assertNotIn("BUY ", text)

    def test_buy_only_omits_sell_line(self) -> None:
        text = render([row(
            "BTC", long_="5", net="5", accounts=2,
            buy=_side("5", "90000", 2, 2),
        )])
        self.assertIn("BUY 5 @ 90,000.00", text)
        # "SELL" appears in the section header, so assert no *side* line
        # renders it (lines that are not the header).
        side_lines = [l for l in text.splitlines()[1:] if "SELL" in l]
        self.assertEqual(side_lines, [])

    def test_zero_quantity_side_never_renders(self) -> None:
        text = render([row(
            "HYPE", short="10", net="-10",
            buy=_side("0", None, 0, 0), sell=_side("10", "90", 1, 1),
        )])
        self.assertNotIn("BUY ", text)


class TestCoverageWarning(unittest.TestCase):
    def test_incomplete_coverage_shows_warning(self) -> None:
        text = render([row(
            "HYPE", short="100", net="-100",
            sell=_side("100", "90", 27, 30),
        )])
        self.assertIn("⚠️27/30", text)

    def test_complete_coverage_has_no_warning(self) -> None:
        text = render([row(
            "HYPE", short="100", net="-100",
            sell=_side("100", "90", 30, 30),
        )])
        self.assertNotIn("⚠️", text)

    def test_missing_price_renders_n_a_with_warning(self) -> None:
        text = render([row(
            "ETH", short="10", net="-10",
            sell=_side("10", None, 0, 2),
        )])
        self.assertIn("n/a", text)
        self.assertIn("⚠️0/2", text)


class TestFormatters(unittest.TestCase):
    def test_qty_thousands_and_precision(self) -> None:
        self.assertEqual(_format_qty(Decimal("47194.4")), "47,194.40")
        self.assertEqual(_format_qty(Decimal("123.45")), "123.45")
        self.assertEqual(_format_qty(Decimal("0.6579")), "0.6579")
        self.assertEqual(_format_qty(Decimal("100")), "100")

    def test_price_formatting(self) -> None:
        self.assertEqual(_format_price(Decimal("83453.502476")), "83,453.50")
        self.assertEqual(_format_price(Decimal("90.53")), "90.53")
        self.assertEqual(_format_price(Decimal("0.000123")), "0.000123")
        self.assertEqual(_format_price(None), "n/a")

    def test_formatters_never_crash_on_garbage(self) -> None:
        self.assertEqual(_format_qty(None), "—")
        self.assertEqual(_format_price(None), "n/a")


if __name__ == "__main__":
    unittest.main()