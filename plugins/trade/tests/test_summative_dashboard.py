"""Tests for the Summative Report Main Dashboard / Net Positions redesign."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Dict, List

from plugins.trade.canonical import (
    CanonicalBalance,
    CanonicalPortfolioSummary,
    CanonicalPosition,
    make_failure,
    make_success,
)
from plugins.trade.portfolio_report import net_by_instrument
from plugins.trade.summative_report_ui import handle, progress_bar, progress_text
from plugins.trade.wizard import TradeWizard


class _StubState:
    def __init__(self) -> None:
        self.report: Dict[str, Any] = {}


class _Desk:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.accounts = {
            "bulk": ["main"],
            "metatrader": ["cents"],
            "hyperliquid": ["flex"],
        }

    def list_exchanges(self) -> List[str]:
        return list(self.accounts)

    def list_accounts(self, exchange: str) -> List[Any]:
        return self.accounts.get(exchange, [])

    def execute(self, request: Dict[str, Any]) -> Any:
        self.calls.append(dict(request))
        op, ex, ac = request["operation"], request["exchange"], request["account"]
        if op == "balance":
            if ex == "metatrader":
                return make_success(op, ex, ac,
                    balance=CanonicalBalance("12345", "USC"),
                    portfolio_summary=CanonicalPortfolioSummary("23456", "10000", "1200", "30000", "USC"))
            return make_success(op, ex, ac,
                balance=CanonicalBalance("1000.00", "USDC"),
                portfolio_summary=CanonicalPortfolioSummary("1200", "800", "200", "2000", "USDC"))
        if ex == "metatrader":
            rows = [
                CanonicalPosition("BTC", "long", "1.5", "50000", "250", mark="50250"),
                CanonicalPosition("BTC", "short", "0.5", "50000", "-50", mark="50250"),
            ]
        elif ex == "bulk":
            rows = [
                CanonicalPosition("BTC", "long", "2.0", "60000", "100", exchange_instrument="BTC-USDC.P", mark="60500"),
                CanonicalPosition("ETH", "short", "3.0", "2000", "-50", exchange_instrument="ETH-USDC.P", mark="2010"),
            ]
        else:
            rows = [
                CanonicalPosition("SOL", "long", "10", "100", "20", exchange_instrument="SOL", mark="101"),
                CanonicalPosition("SOL", "short", "4", "100", "-5", exchange_instrument="SOL", mark="101"),
            ]
        return make_success(op, ex, ac, positions=rows)


class _Snapshot:
    def __init__(self, desk: _Desk) -> None:
        from plugins.trade.portfolio_report import collect_portfolio_snapshot
        self.snap = collect_portfolio_snapshot(desk, max_workers=1)

    @property
    def positions(self) -> List[Any]:
        return self.snap.positions

    @property
    def accounts(self) -> List[Any]:
        return self.snap.accounts


class TestNetByInstrument(unittest.TestCase):
    def setUp(self) -> None:
        self.desk = _Desk()
        self.snapshot = _Snapshot(self.desk).snap

    def test_gross_and_net_per_instrument(self) -> None:
        rows = net_by_instrument(self.snapshot)
        by_symbol = {r["instrument"]: r for r in rows}
        btc = by_symbol["BTC"]
        self.assertEqual(btc["long"], 3.5)  # 1.5 + 2.0
        self.assertEqual(btc["short"], 0.5)
        self.assertEqual(btc["gross"], 4.0)
        self.assertEqual(btc["net"], 3.0)  # 1.5 + 2.0 - 0.5
        self.assertEqual(btc["accounts"], 2)
        sol = by_symbol["SOL"]
        self.assertEqual(sol["long"], 10)
        self.assertEqual(sol["short"], 4)
        self.assertEqual(sol["net"], 6)
        self.assertEqual(sol["gross"], 14)
        # ETH only exists in bulk+main with a SELL ⇒ net is negative
        eth = by_symbol["ETH"]
        self.assertEqual(eth["long"], 0)
        self.assertEqual(eth["short"], 3)
        self.assertEqual(eth["net"], -3)
        self.assertEqual(eth["gross"], 3)

    def test_empty_snapshot_yields_no_rows(self) -> None:
        from plugins.trade.portfolio_report import PortfolioSnapshot
        empty = PortfolioSnapshot(fetched_at=0.0)
        self.assertEqual(net_by_instrument(empty), [])


class TestProgressBar(unittest.TestCase):
    def test_zero_pct_all_empty(self) -> None:
        self.assertEqual(progress_bar(0.0), "[" + "░" * 10 + "]")

    def test_full_pct_all_filled(self) -> None:
        self.assertEqual(progress_bar(100.0), "[" + "█" * 10 + "]")

    def test_mid_pct(self) -> None:
        bar = progress_bar(40.0)
        self.assertEqual(bar[0], "[")
        self.assertEqual(bar[-1], "]")
        self.assertEqual(bar.count("█"), 4)
        self.assertEqual(bar.count("░"), 6)

    def test_clamps_out_of_range(self) -> None:
        self.assertEqual(progress_bar(-10.0), "[" + "░" * 10 + "]")
        self.assertEqual(progress_bar(150.0), "[" + "█" * 10 + "]")

    def test_progress_text_renders_current_and_counters(self) -> None:
        text = progress_text("Fetching positions", 40.0, done=8, total=20, current="Hyperliquid")
        self.assertIn("🔄 Fetching positions", text)
        self.assertIn("[", text)
        self.assertIn("(8/20)", text)
        self.assertIn("Current: Hyperliquid", text)


class TestMainDashboard(unittest.TestCase):
    def setUp(self) -> None:
        self.desk = _Desk()
        self.wizard = TradeWizard(tradedesk=self.desk)  # type: ignore[arg-type]
        self.key = ("dashboard",)
        self.wizard.open(self.key)
        self.menu = self.wizard.handle_callback(self.key, "summative_report")

    def test_menu_has_dashboard_sections(self) -> None:
        self.assertEqual(self.menu.state, "summative_report")
        self.assertIn("📊 Summative Report", self.menu.text)
        self.assertIn("DEX:", self.menu.text)
        self.assertIn("Accounts:", self.menu.text)
        self.assertIn("Equity (unconverted)", self.menu.text)
        self.assertIn("uPnL (unconverted)", self.menu.text)
        self.assertIn("Top instruments", self.menu.text)
        self.assertIn("Closest liquidation: unavailable", self.menu.text)
        labels = [item["text"] for row in self.menu.buttons for item in row]
        for expected in ("📈 Net Positions", "🏛 By Exchange", "📊 By Symbol",
                         "⚠️ Risks", "⚡ Actions", "🔍 Filter",
                         "🔄 Refresh", "⚙️ Settings", "◀ Back", "✕ Exit"):
            self.assertIn(expected, labels)

    def test_top_instruments_show_net_and_gross(self) -> None:
        self.assertIn("net", self.menu.text)
        self.assertIn("gross", self.menu.text)
        self.assertIn("acct)", self.menu.text)

    def test_fetch_status_line(self) -> None:
        self.assertIn("Data fetched now", self.menu.text)


class TestNetPositionsView(unittest.TestCase):
    def setUp(self) -> None:
        self.desk = _Desk()
        self.wizard = TradeWizard(tradedesk=self.desk)  # type: ignore[arg-type]
        self.key = ("net",)
        self.wizard.open(self.key)
        self.wizard.handle_callback(self.key, "summative_report")
        self.view = self.wizard.handle_callback(self.key, "sr:n")

    def test_default_sort_is_gross(self) -> None:
        self.assertIn("Sort: gross", self.view.text)
        # BTC has gross 4.0; SOL has 14; SOL is first line in the body
        lines = self.view.text.splitlines()
        sol_idx = next((i for i, l in enumerate(lines) if l.startswith("SOL ")), -1)
        btc_idx = next((i for i, l in enumerate(lines) if l.startswith("BTC ")), -1)
        self.assertGreater(sol_idx, -1)
        self.assertGreater(btc_idx, -1)
        self.assertLess(sol_idx, btc_idx)

    def test_long_only_filter(self) -> None:
        view = self.wizard.handle_callback(self.key, "sr:long")
        self.assertIn("Filter: long", view.text)
        # All visible rows should have positive net
        for line in view.text.splitlines():
            if line.startswith(("BTC ", "ETH ", "SOL ")):
                self.assertIn("net", line)
                # crude sign check: not start with "net -"
                self.assertFalse(line.startswith(("BTC    net -", "ETH    net -", "SOL    net -")))

    def test_short_only_filter(self) -> None:
        view = self.wizard.handle_callback(self.key, "sr:short")
        self.assertIn("Filter: short", view.text)

    def test_sort_pnl(self) -> None:
        view = self.wizard.handle_callback(self.key, "sr:spnl")
        self.assertIn("Sort: pnl", view.text)

    def test_sort_net(self) -> None:
        view = self.wizard.handle_callback(self.key, "sr:snet")
        self.assertIn("Sort: net", view.text)

    def test_detail_shows_contributors_and_stub_buttons(self) -> None:
        # Click first instrument's detail button
        first_btn_row = next(
            item
            for row in self.view.buttons
            for item in row
            if item.get("callback_data", "").startswith("sr:nd:")
        )
        detail = self.wizard.handle_callback(self.key, first_btn_row["callback_data"])
        self.assertIn("net", detail.text)
        self.assertIn("gross", detail.text)
        labels = [item["text"] for row in detail.buttons for item in row]
        for expected in ("Set TP", "Set SL", "Close 50%", "Close 100%",
                         "◀ Back", "🏠 Home", "🔄 Refresh"):
            self.assertIn(expected, labels)


class TestActionStubs(unittest.TestCase):
    def setUp(self) -> None:
        self.desk = _Desk()
        self.wizard = TradeWizard(tradedesk=self.desk)  # type: ignore[arg-type]
        self.key = ("actions",)
        self.wizard.open(self.key)
        self.wizard.handle_callback(self.key, "summative_report")

    def test_actions_menu(self) -> None:
        view = self.wizard.handle_callback(self.key, "sr:a")
        self.assertIn("⚡ Actions", view.text)
        self.assertIn("Bulk future operations", view.text)

    def test_settings_menu(self) -> None:
        view = self.wizard.handle_callback(self.key, "sr:s")
        self.assertIn("⚙️ Settings", view.text)

    def test_stub_buttons_dont_write(self) -> None:
        before = len(self.desk.calls)
        for suffix in ("sr:atp", "sr:asl", "sr:c50", "sr:c100",
                       "sr:asym", "sr:aex", "sr:acancel"):
            self.wizard.handle_callback(self.key, suffix)
        self.assertEqual(len(self.desk.calls), before)
        ops = {c["operation"] for c in self.desk.calls}
        self.assertEqual(ops, {"balance", "positions_orders"})

    def test_home_returns_to_dashboard(self) -> None:
        view = self.wizard.handle_callback(self.key, "sr:h")
        self.assertIn("📊 Summative Report", view.text)


if __name__ == "__main__":
    unittest.main()