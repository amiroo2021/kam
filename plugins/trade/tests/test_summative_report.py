"""Offline tests for the read-only /trade Summative Report."""
from __future__ import annotations

import unittest
from typing import Any, Dict, List

from plugins.trade.canonical import (
    CanonicalBalance,
    CanonicalPortfolioSummary,
    CanonicalPosition,
    make_failure,
    make_success,
)
from plugins.trade.portfolio_report import collect_portfolio_snapshot, positions_by_symbol
from plugins.trade.wizard import TradeWizard


class ReportDesk:
    """Fake desk that rejects every non-read operation."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.accounts = {
            "bulk": ["main", "duplicate", "duplicate"],
            "hyperliquid": [{"account": "flex", "label": "Flex"}],
            "apex": ["bad"],
        }

    def list_exchanges(self) -> List[str]:
        return list(self.accounts)

    def list_accounts(self, exchange: str) -> List[Any]:
        return self.accounts.get(exchange, [])

    def execute(self, request: Dict[str, Any]) -> Any:
        self.calls.append(dict(request))
        op, ex, ac = request["operation"], request["exchange"], request["account"]
        if op not in {"balance", "positions_orders"}:
            raise AssertionError(f"non-read operation called: {op}")
        if ex == "apex":
            return make_failure(op, ex, ac, "RATE_LIMITED", "mock read failure")
        if op == "balance":
            if ex == "bulk" and ac == "main":
                return make_success(op, ex, ac,
                    balance=CanonicalBalance("1000.00", "USDC"),
                    portfolio_summary=CanonicalPortfolioSummary("1200", "800", "300", "2000", "USDC"))
            if ex == "bulk":
                return make_success(op, ex, ac,
                    balance=CanonicalBalance("400.00", "USDC"),
                    portfolio_summary=CanonicalPortfolioSummary("500", "300", "50", "700", "USDC"))
            return make_success(op, ex, ac,
                balance=CanonicalBalance("700.00", "USDT"),
                portfolio_summary=CanonicalPortfolioSummary("900", "600", "100", "1200", "USDT"))
        if ex == "bulk" and ac == "main":
            rows = [
                CanonicalPosition("BTC", "long", "2", "60000", "100", exchange_instrument="BTC-USDC.P", mark="60500"),
                CanonicalPosition("ETH", "short", "3", "2000", "-50", exchange_instrument="ETH-USDC.P", mark="2010"),
            ]
        elif ex == "bulk":
            rows = [CanonicalPosition("BTC", "long", "0.5", "59000", "20", exchange_instrument="BTC-USDC.P", mark="60400")]
        else:
            rows = [CanonicalPosition("BTC", "sell", "1", "60000", "-30", exchange_instrument="BTC", mark="60100")]
        return make_success(op, ex, ac, positions=rows)


class TestPortfolioAggregation(unittest.TestCase):
    def setUp(self) -> None:
        self.desk = ReportDesk()

    def test_open_positions_are_normalized_deduplicated_and_read_only(self) -> None:
        snap = collect_portfolio_snapshot(self.desk, max_workers=1)
        groups = positions_by_symbol(snap)
        self.assertEqual(len(snap.accounts), 4)  # duplicate bulk account suppressed
        self.assertTrue(snap.incomplete)
        self.assertEqual([(g.instrument_key, g.side, str(g.volume)) for g in groups],
                         [("BTC", "BUY", "2.5"), ("ETH", "SELL", "3"), ("BTC", "SELL", "1")])
        self.assertEqual({c["operation"] for c in self.desk.calls}, {"balance", "positions_orders"})

    def test_wizard_menu_views_and_filter_are_read_only(self) -> None:
        wizard = TradeWizard(tradedesk=self.desk)  # type: ignore[arg-type]
        key = ("report-test",)
        wizard.open(key)
        menu = wizard.handle_callback(key, "summative_report")
        labels = [item["text"] for row in menu.buttons for item in row]
        self.assertEqual(menu.state, "summative_report")
        for expected in ("📈 Net Positions", "🏛 By Exchange", "📊 By Symbol",
                         "⚠️ Risks", "⚡ Actions", "🔍 Filter"):
            self.assertIn(expected, labels)
        positions = wizard.handle_callback(key, "sr:p")
        self.assertIn("BTC BUY | Volume: 2.5", positions.text)
        self.assertIn("Overall floating PnL", positions.text)
        detail = wizard.handle_callback(key, "sr:d:0")
        self.assertIn("Contributing open positions", detail.text)
        exchange = wizard.handle_callback(key, "sr:e")
        self.assertIn("bulk:", exchange.text)
        self.assertIn("hyperliquid:", exchange.text)
        total = wizard.handle_callback(key, "sr:t")
        self.assertIn("Floating PnL (USDC)", total.text)
        equity = wizard.handle_callback(key, "sr:q")
        self.assertIn("Total Equity (USDC)", equity.text)
        risk = wizard.handle_callback(key, "sr:r")
        self.assertIn("Liquidation prices: unavailable", risk.text)
        filters = wizard.handle_callback(key, "sr:f")
        self.assertIn("All Exchanges", [item["text"] for row in filters.buttons for item in row])
        bulk = wizard.handle_callback(key, "sr:fx:1")
        self.assertIn("Select an account", bulk.text)
        account = wizard.handle_callback(key, "sr:fa:0")
        self.assertIn("Open positions:", account.text)
        self.assertTrue(all(call["operation"] in {"balance", "positions_orders"} for call in self.desk.calls))

    def test_refresh_reloads_only_read_operations(self) -> None:
        wizard = TradeWizard(tradedesk=self.desk)  # type: ignore[arg-type]
        key = ("refresh-test",)
        wizard.open(key)
        wizard.handle_callback(key, "summative_report")
        before = len(self.desk.calls)
        wizard.handle_callback(key, "sr:refresh")
        self.assertGreater(len(self.desk.calls), before)
        self.assertTrue(all(call["operation"] in {"balance", "positions_orders"} for call in self.desk.calls))

    def test_currency_totals_never_combine_native_units(self) -> None:
        from plugins.trade.portfolio_report import total_floating_pnl
        snap = collect_portfolio_snapshot(self.desk, max_workers=1)
        info = total_floating_pnl(snap)
        self.assertNotIn("total", info)
        self.assertEqual(info["units"], {"USDC": 70, "USDT": -30})
        wizard = TradeWizard(tradedesk=self.desk)  # type: ignore[arg-type]
        key = ("currency-test",)
        wizard.open(key)
        wizard.handle_callback(key, "summative_report")
        total = wizard.handle_callback(key, "sr:t")
        self.assertIn("Floating PnL (USDC): +70.00 USDC", total.text)
        self.assertIn("Floating PnL (USDT): -30.00 USDT", total.text)
        self.assertNotIn("TOTAL FLOATING PnL: $", total.text)

    def test_metatrader_usc_is_converted_once_and_native_is_retained(self) -> None:
        class USCDesk(ReportDesk):
            def __init__(self):
                super().__init__()
                self.accounts = {"metatrader": ["cents"]}
            def execute(self, request):
                op = request["operation"]
                self.calls.append(dict(request))
                if op == "balance":
                    return make_success(op, "metatrader", "cents",
                        balance=CanonicalBalance("12345", "USC"),
                        portfolio_summary=CanonicalPortfolioSummary("23456", "10000", "1200", "30000", "USC"))
                return make_success(op, "metatrader", "cents", positions=[
                    CanonicalPosition("BTC", "long", "1", "50000", "250", mark="50250"),
                    CanonicalPosition("ETH", "short", "1", "3000", "-125", mark="3125"),
                ])
        snap = collect_portfolio_snapshot(USCDesk(), max_workers=1)
        account = snap.accounts[0]
        self.assertEqual(str(account.equity), "234.56")
        self.assertEqual(account.equity_unit, "USD")
        self.assertEqual(str(account.positions[0].pnl), "2.50")
        self.assertEqual(str(account.positions[1].pnl), "-1.25")
        self.assertEqual(account.positions[0].native_pnl_unit, "USC")
        self.assertEqual(str(account.positions[0].native_pnl), "250")

    def test_position_failure_marks_coverage_partial_not_zero(self) -> None:
        class PartialDesk(ReportDesk):
            def __init__(self):
                super().__init__()
                self.accounts = {"good": ["open", "empty"], "bad": ["balance-only"]}
            def execute(self, request):
                self.calls.append(dict(request))
                op, ex, ac = request["operation"], request["exchange"], request["account"]
                if op == "balance":
                    return make_success(op, ex, ac, balance=CanonicalBalance("100", "USDC"))
                if ex == "bad":
                    return make_failure(op, ex, ac, "UNSUPPORTED", "positions unavailable")
                rows = [] if ac == "empty" else [CanonicalPosition("BTC", "long", "1", "1", "1", mark="1")]
                return make_success(op, ex, ac, positions=rows)
        desk = PartialDesk()
        snap = collect_portfolio_snapshot(desk, max_workers=1)
        self.assertEqual((snap.positions_accounts_ok, snap.positions_accounts_seen), (2, 3))
        self.assertTrue(snap.positions_incomplete)
        bad = next(a for a in snap.accounts if a.exchange == "bad")
        self.assertFalse(bad.positions_ok)
        self.assertFalse(bad.positions_empty)
        empty = next(a for a in snap.accounts if a.account == "empty")
        self.assertTrue(empty.positions_ok)
        self.assertTrue(empty.positions_empty)
        wizard = TradeWizard(tradedesk=desk)  # type: ignore[arg-type]
        key = ("partial-test",)
        wizard.open(key)
        wizard.handle_callback(key, "summative_report")
        screen = wizard.handle_callback(key, "sr:t")
        self.assertIn("Positions coverage: 2/3 accounts · PARTIAL", screen.text)
        self.assertIn("Excluded position accounts", screen.text)

    def test_risk_never_invents_notional_without_contract_metadata(self) -> None:
        from plugins.trade.portfolio_report import risk_and_exposure
        snap = collect_portfolio_snapshot(self.desk, max_workers=1)
        risk = risk_and_exposure(snap)
        self.assertIsNone(risk["gross_long_notional"])
        self.assertIsNone(risk["gross_short_notional"])
        self.assertTrue(risk["notional_incomplete"])

    def test_refresh_recovers_position_coverage_after_transient_failure(self) -> None:
        class RecoveryDesk(ReportDesk):
            def __init__(self):
                super().__init__()
                self.accounts = {"venue": ["a"]}
                self.fail = True
            def execute(self, request):
                self.calls.append(dict(request))
                op = request["operation"]
                if op == "balance":
                    return make_success(op, "venue", "a", balance=CanonicalBalance("100", "USDC"))
                if self.fail:
                    return make_failure(op, "venue", "a", "TIMEOUT", "temporary positions failure")
                return make_success(op, "venue", "a", positions=[])
        desk = RecoveryDesk()
        wizard = TradeWizard(tradedesk=desk)  # type: ignore[arg-type]
        key = ("recover-test",)
        wizard.open(key)
        wizard.handle_callback(key, "summative_report")
        partial = wizard.handle_callback(key, "sr:t")
        self.assertIn("Positions coverage: 0/1 accounts · PARTIAL", partial.text)
        desk.fail = False
        recovered = wizard.handle_callback(key, "sr:refresh")
        self.assertIn("Positions: 1/1 accounts", recovered.text)


if __name__ == "__main__":
    unittest.main()
