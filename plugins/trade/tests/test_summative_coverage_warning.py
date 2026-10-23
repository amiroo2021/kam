"""Focused tests for the four-category coverage warning.

The Summative Report must distinguish, separately:
  * accounts that responded
  * accounts with complete position coverage
  * accounts with partial position coverage
  * accounts that were unavailable

and must report an open-orders-only failure WITHOUT reducing position
coverage, because exposure is still fully known in that case.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import List, Optional

from plugins.trade import portfolio_report as portfolio
from plugins.trade.portfolio_report import AccountSnapshot, PortfolioSnapshot


def acct(
    name: str,
    *,
    ok: bool = True,
    partial: bool = False,
    orders_partial: bool = False,
    exchange: str = "hyperliquid",
) -> AccountSnapshot:
    return AccountSnapshot(
        exchange=exchange, account=name, account_label=name,
        identity=f"{exchange}|{name}", ok=ok, balance=None, balance_unit=None,
        equity=None, available=None, balance_ok=ok, positions_ok=ok,
        positions_partial=partial,
        partial_detail=[f"{exchange}/{name}: 1 of 2 perp DEXs unavailable (rate_limited)"] if partial else [],
        orders_partial=orders_partial,
        orders_partial_detail=[f"{exchange}/{name}: open orders unavailable on 1 of 2 perp DEXs (timeout)"] if orders_partial else [],
    )


def render_warnings(scope: PortfolioSnapshot) -> str:
    """Mirror of the real UI warning block, so assertions are on real text."""
    lines: List[str] = []
    if scope.positions_incomplete:
        seen = scope.positions_accounts_seen
        lines += [
            "⚠️ Position data incomplete",
            f"   Accounts responded: {scope.positions_accounts_ok}/{seen}",
            f"   Accounts fully covered: {scope.positions_accounts_full}/{seen}",
            f"   Accounts unavailable: {scope.positions_accounts_unavailable}",
        ]
    partials = scope.partial_accounts
    if partials:
        lines += ["", f"⚠️ Partial venue coverage ({len(partials)} account(s) — totals may be low):"]
        for a in partials[:6]:
            for detail in (getattr(a, "partial_detail", None) or ["partial coverage"]):
                lines.append(f"   • {detail}")
    orders_partial = scope.orders_partial_accounts
    if orders_partial:
        lines += ["", f"ℹ️ Open-order data incomplete ({len(orders_partial)} account(s) — positions unaffected):"]
        for a in orders_partial[:6]:
            for detail in (getattr(a, "orders_partial_detail", None) or ["open orders unavailable"]):
                lines.append(f"   • {detail}")
    return "\n".join(lines)


class TestCoverageCounters(unittest.TestCase):
    def test_counters_split_the_four_categories(self) -> None:
        scope = PortfolioSnapshot(
            fetched_at=0.0,
            accounts=[
                acct("A"),                       # full
                acct("B"),                       # full
                acct("C", partial=True),         # partial
                acct("D", ok=False),             # unavailable
            ],
            positions=[], accounts_seen=4, accounts_ok=3,
        )
        self.assertEqual(scope.positions_accounts_seen, 4)
        self.assertEqual(scope.positions_accounts_ok, 3)
        self.assertEqual(scope.positions_accounts_full, 2)
        self.assertEqual(scope.positions_accounts_partial, 1)
        self.assertEqual(scope.positions_accounts_unavailable, 1)

    def test_categories_are_mutually_exclusive(self) -> None:
        scope = PortfolioSnapshot(
            fetched_at=0.0,
            accounts=[acct("A"), acct("B", partial=True), acct("C", ok=False)],
            positions=[], accounts_seen=3, accounts_ok=2,
        )
        total = (
            scope.positions_accounts_full
            + scope.positions_accounts_partial
            + scope.positions_accounts_unavailable
        )
        self.assertEqual(total, scope.positions_accounts_seen)


class TestWarningText(unittest.TestCase):
    def _mixed(self) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            fetched_at=0.0,
            accounts=[
                acct("A"), acct("B"), acct("C"),
                acct("ZERO", partial=True),
                acct("D", ok=False),
            ],
            positions=[], accounts_seen=5, accounts_ok=4,
        )

    def test_warning_distinguishes_all_four_categories(self) -> None:
        text = render_warnings(self._mixed())
        self.assertIn("⚠️ Position data incomplete", text)
        self.assertIn("Accounts responded: 4/5", text)
        self.assertIn("Accounts fully covered: 3/5", text)
        self.assertIn("Accounts unavailable: 1", text)
        self.assertIn("⚠️ Partial venue coverage (1 account(s)", text)

    def test_no_partial_warning_when_all_fully_covered(self) -> None:
        scope = PortfolioSnapshot(
            fetched_at=0.0, accounts=[acct("A"), acct("B")],
            positions=[], accounts_seen=2, accounts_ok=2,
        )
        text = render_warnings(scope)
        self.assertNotIn("Partial venue coverage", text)
        self.assertNotIn("Position data incomplete", text)

    def test_partial_alone_triggers_incomplete(self) -> None:
        scope = PortfolioSnapshot(
            fetched_at=0.0, accounts=[acct("A"), acct("ZERO", partial=True)],
            positions=[], accounts_seen=2, accounts_ok=2,
        )
        text = render_warnings(scope)
        self.assertIn("Position data incomplete", text)
        self.assertIn("Accounts responded: 2/2", text)
        self.assertIn("Accounts fully covered: 1/2", text)

    def test_warning_does_not_leak_secrets(self) -> None:
        text = render_warnings(self._mixed())
        self.assertNotIn("0x", text)
        self.assertNotIn("secret", text.lower())


class TestOrdersOnlyDoesNotReducePositionCoverage(unittest.TestCase):
    def test_orders_partial_is_not_position_partial(self) -> None:
        a = acct("ZERO", orders_partial=True)
        self.assertFalse(a.positions_partial)
        self.assertTrue(a.orders_partial)

    def test_orders_partial_does_not_count_against_full_coverage(self) -> None:
        scope = PortfolioSnapshot(
            fetched_at=0.0, accounts=[acct("ZERO", orders_partial=True)],
            positions=[], accounts_seen=1, accounts_ok=1,
        )
        self.assertEqual(scope.positions_accounts_full, 1)
        self.assertEqual(scope.positions_accounts_partial, 0)
        self.assertFalse(scope.positions_incomplete)

    def test_orders_partial_gets_its_own_warning(self) -> None:
        scope = PortfolioSnapshot(
            fetched_at=0.0, accounts=[acct("ZERO", orders_partial=True)],
            positions=[], accounts_seen=1, accounts_ok=1,
        )
        text = render_warnings(scope)
        self.assertIn("Open-order data incomplete", text)
        self.assertIn("positions unaffected", text)
        self.assertNotIn("Partial venue coverage", text)

    def test_orders_and_positions_partial_are_reported_separately(self) -> None:
        scope = PortfolioSnapshot(
            fetched_at=0.0,
            accounts=[acct("X", partial=True), acct("Y", orders_partial=True)],
            positions=[], accounts_seen=2, accounts_ok=2,
        )
        text = render_warnings(scope)
        self.assertIn("Partial venue coverage (1 account(s)", text)
        self.assertIn("Open-order data incomplete (1 account(s)", text)


class TestPositionsRetainedInTotals(unittest.TestCase):
    def test_partial_account_position_still_counted(self) -> None:
        row = portfolio.OpenPositionRow(
            exchange="hyperliquid", account="ZERO", account_label="ZERO",
            symbol="HYPE", instrument_key="HYPE", side="SELL",
            size=Decimal("135.73"), entry_price=Decimal("81.1482"),
            mark=None, pnl=None, pnl_unit="USD", identity="ZERO|HYPE",
        )
        scope = PortfolioSnapshot(
            fetched_at=0.0,
            accounts=[acct("ZERO", partial=True)],
            positions=[row], accounts_seen=1, accounts_ok=1, incomplete=True,
        )
        groups = portfolio.net_by_instrument(scope)
        hype = [g for g in groups if g["instrument"] == "HYPE"]
        self.assertEqual(len(hype), 1, "partial account must still contribute")
        self.assertEqual(hype[0]["gross"], Decimal("135.73"))
        self.assertEqual(hype[0]["accounts"], 1)


if __name__ == "__main__":
    unittest.main()