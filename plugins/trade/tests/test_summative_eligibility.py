"""Eligibility + dashboard simplification tests for the Summative Report.

Covers the eight required scenarios:
  1. positive balance, no positions          -> eligible
  2. open positions, zero balance            -> eligible
  3. zero balance, zero positions            -> NOT eligible
  4. unsupported positions, valid balance    -> eligible (balance-only)
  5. failed balance, valid positions         -> eligible (position-only)
  6. failed endpoints                        -> NOT eligible
  7. multiple accounts per exchange          -> counted once each
  8. partial-data coverage                   -> concise warning, not a list

Also asserts the dashboard no longer carries technical explanations, that
failed reads are never coerced to zero, and that eligibility propagates to
the exchange/account filter lists.
"""
import unittest
from decimal import Decimal
from typing import Any, List, Optional
from unittest import mock

from plugins.trade import portfolio_report as portfolio
from plugins.trade.portfolio_report import AccountSnapshot, PortfolioSnapshot


def make_account(
    exchange: str = "ex",
    account: str = "main",
    *,
    balance_ok: bool = False,
    equity: Optional[str] = None,
    balance: Optional[str] = None,
    available: Optional[str] = None,
    positions_ok: bool = False,
    positions: int = 0,
    errors: Optional[List[str]] = None,
) -> AccountSnapshot:
    """Build an AccountSnapshot directly from explicit success flags."""
    rows = [
        portfolio.OpenPositionRow(
            exchange=exchange,
            account=account,
            account_label=account,
            symbol="BTC",
            instrument_key="BTC",
            side="BUY",
            size=Decimal("1"),
            entry_price=Decimal("100"),
            mark=Decimal("110"),
            pnl=Decimal("10"),
            pnl_unit="USD",
            identity=f"{exchange}|{account}|BTC|BUY|1",
        )
        for _ in range(positions)
    ]
    return AccountSnapshot(
        exchange=exchange,
        account=account,
        account_label=account,
        identity=f"{exchange}|{account}",
        ok=balance_ok or positions_ok,
        balance=Decimal(balance) if balance is not None else None,
        balance_unit="USD" if balance is not None else None,
        equity=Decimal(equity) if equity is not None else None,
        equity_unit="USD" if equity is not None else None,
        available=Decimal(available) if available is not None else None,
        positions=rows,
        errors=list(errors or []),
        balance_ok=balance_ok,
        positions_ok=positions_ok,
        positions_empty=positions_ok and positions == 0,
    )


def make_snapshot(accounts: List[AccountSnapshot]) -> PortfolioSnapshot:
    positions: List[Any] = []
    for account in accounts:
        positions.extend(account.positions)
    return PortfolioSnapshot(
        fetched_at=0.0,
        accounts=accounts,
        positions=positions,
        accounts_seen=len(accounts),
        accounts_ok=sum(1 for a in accounts if portfolio.is_account_eligible(a)),
        incomplete=any(not a.positions_ok for a in accounts),
    )


class TestEligibilityRules(unittest.TestCase):
    def test_positive_balance_no_positions_is_eligible(self) -> None:
        account = make_account(balance_ok=True, equity="1000", positions_ok=True, positions=0)
        self.assertTrue(portfolio.is_account_eligible(account))

    def test_open_positions_zero_balance_is_eligible(self) -> None:
        account = make_account(balance_ok=True, equity="0", balance="0", positions_ok=True, positions=2)
        self.assertTrue(portfolio.is_account_eligible(account))

    def test_zero_balance_zero_positions_is_not_eligible(self) -> None:
        account = make_account(balance_ok=True, equity="0", balance="0", available="0",
                               positions_ok=True, positions=0)
        self.assertFalse(portfolio.is_account_eligible(account))

    def test_failed_balance_with_valid_positions_is_eligible(self) -> None:
        account = make_account(balance_ok=False, positions_ok=True, positions=1,
                               errors=["ex/main: balance error (boom)"])
        self.assertTrue(portfolio.is_account_eligible(account))

    def test_unsupported_positions_with_valid_balance_is_eligible(self) -> None:
        # Positions endpoint unsupported must not disqualify a funded account.
        account = make_account(balance_ok=True, equity="500", positions_ok=False,
                               errors=["ex/main: positions UNSUPPORTED"])
        self.assertTrue(portfolio.is_account_eligible(account))

    def test_failed_endpoints_is_not_eligible(self) -> None:
        account = make_account(balance_ok=False, positions_ok=False,
                               errors=["ex/main: balance error", "ex/main: positions error"])
        self.assertFalse(portfolio.is_account_eligible(account))

    def test_failed_read_is_never_read_as_zero_balance(self) -> None:
        # A failed balance response leaves equity/balance as None, never 0.
        account = make_account(balance_ok=False, positions_ok=False)
        self.assertIsNone(account.equity)
        self.assertIsNone(account.balance)
        self.assertFalse(portfolio.is_account_eligible(account))


class TestSnapshotEligibleViews(unittest.TestCase):
    def test_counts_each_eligible_account_once(self) -> None:
        snap = make_snapshot([
            make_account("ex1", "a1", balance_ok=True, equity="10", positions_ok=True),
            make_account("ex1", "a2", balance_ok=True, equity="0", positions_ok=True, positions=1),
            make_account("ex1", "a3", balance_ok=True, equity="0", positions_ok=True, positions=0),
            make_account("ex2", "b1", balance_ok=False, positions_ok=False,
                         errors=["ex2/b1: boom"]),
        ])
        # a1 + a2 eligible; a3 (all zero) and b1 (failed) excluded.
        self.assertEqual([a.account for a in snap.eligible_accounts], ["a1", "a2"])
        self.assertEqual(snap.eligible_exchanges, ["ex1"])
        identities = [a.identity for a in snap.eligible_accounts]
        self.assertEqual(len(identities), len(set(identities)))

    def test_multiple_accounts_per_exchange_each_counted(self) -> None:
        snap = make_snapshot([
            make_account("ex1", "a1", balance_ok=True, equity="10", positions_ok=True),
            make_account("ex1", "a2", balance_ok=True, equity="20", positions_ok=True),
            make_account("ex1", "a3", balance_ok=True, equity="30", positions_ok=True),
        ])
        self.assertEqual(len(snap.eligible_accounts), 3)
        self.assertEqual(snap.eligible_exchanges, ["ex1"])

    def test_position_bearing_accounts_excludes_balance_only(self) -> None:
        snap = make_snapshot([
            make_account("ex1", "a1", balance_ok=True, equity="10", positions_ok=True, positions=1),
            make_account("ex1", "a2", balance_ok=True, equity="20", positions_ok=True, positions=0),
        ])
        self.assertEqual([a.account for a in snap.position_bearing_accounts], ["a1"])

    def test_list_exchanges_hides_fully_ineligible_exchange(self) -> None:
        snap = make_snapshot([
            make_account("good", "a1", balance_ok=True, equity="10", positions_ok=True),
            make_account("dead", "b1", balance_ok=False, positions_ok=False, errors=["dead/b1: boom"]),
        ])
        self.assertEqual(portfolio.list_exchanges(snap), ["good"])

    def test_list_exchanges_falls_back_when_nothing_eligible(self) -> None:
        snap = make_snapshot([
            make_account("dead", "b1", balance_ok=False, positions_ok=False, errors=["boom"]),
        ])
        # Degrades to a usable empty view instead of raising.
        self.assertEqual(portfolio.list_exchanges(snap), ["dead"])

    def test_accounts_for_exchange_filters_ineligible(self) -> None:
        snap = make_snapshot([
            make_account("ex1", "a1", balance_ok=True, equity="10", positions_ok=True),
            make_account("ex1", "a2", balance_ok=True, equity="0", positions_ok=True, positions=0),
        ])
        accounts = portfolio.accounts_for_exchange(snap, "ex1")
        self.assertEqual([a.account for a in accounts], ["a1"])


class TestDashboardSimplification(unittest.TestCase):
    def _render(self, accounts: List[AccountSnapshot]) -> Any:
        """Render the real dashboard against a prepared snapshot.

        The desk stub raises on every operation, so any accidental fetch
        inside the render path fails loudly instead of silently passing.
        """
        from plugins.trade import summative_report_ui as ui

        snap = make_snapshot(accounts)

        class State:
            def __init__(self) -> None:
                self.report: dict = {}

        state = State()
        with mock.patch.object(ui, "_report_snapshot", return_value=snap):
            return ui._menu(_NoDesk(), state, refresh=False)

    def test_dashboard_shows_only_the_six_requested_fields(self) -> None:
        view = self._render([
            make_account("ex1", "a1", balance_ok=True, equity="1000", positions_ok=True, positions=2),
            make_account("ex1", "a2", balance_ok=True, equity="0", positions_ok=True, positions=0),
        ])

        self.assertIn("Open positions:", view.text)
        self.assertIn("Exchanges:", view.text)
        self.assertIn("Accounts:", view.text)
        self.assertIn("Equity", view.text)
        self.assertIn("uPnL", view.text)
        self.assertIn("Top instruments", view.text)
        # Eligible counts: a1 only (a2 has no positions and zero balance).
        self.assertIn("Accounts: 1", view.text)

    def test_dashboard_omits_removed_sections(self) -> None:
        view = self._render([
            make_account("ex1", "a1", balance_ok=True, equity="1000", positions_ok=True, positions=1),
            make_account("ex2", "b1", balance_ok=False, positions_ok=False, errors=["boom"]),
        ])

        for banned in (
            "Closest liquidation",
            "canonical contract lacks",
            "USD notional: N/A",
            "Excluded position accounts",
            "(unconverted)",
        ):
            self.assertNotIn(banned, view.text)

    def test_partial_coverage_shows_concise_warning_only(self) -> None:
        view = self._render([
            make_account("ex1", "a1", balance_ok=True, equity="1000", positions_ok=True, positions=1),
            make_account("ex2", "b1", balance_ok=True, equity="50", positions_ok=False,
                         errors=["ex2/b1: positions UNSUPPORTED"]),
        ])

        self.assertIn("⚠️ Position data incomplete", view.text)
        # Counts are now broken out by coverage category instead of a single
        # "responded" fraction, so partial and unavailable are distinguishable.
        self.assertIn("Accounts responded: 1/2", view.text)
        self.assertIn("Accounts fully covered: 1/2", view.text)
        self.assertIn("Accounts unavailable: 1", view.text)
        # The failure detail must not dominate the dashboard.
        self.assertNotIn("UNSUPPORTED", view.text)

    def test_complete_coverage_has_no_warning(self) -> None:
        view = self._render([
            make_account("ex1", "a1", balance_ok=True, equity="1000", positions_ok=True, positions=1),
        ])
        self.assertNotIn("⚠️ Position data incomplete", view.text)

    def test_empty_snapshot_renders_without_crashing(self) -> None:
        view = self._render([])

        self.assertIn("📊 Summative Report", view.text)
        self.assertIn("Top instruments: none", view.text)
        self.assertIn("Equity: —", view.text)

    def test_currency_labels_are_not_repeated(self) -> None:
        from plugins.trade import summative_report_ui as ui

        # Dashboard form: one label, currency as a suffix, no bracketed repeat.
        lines: List[str] = []
        ui._native_lines(lines, "Equity", {"USD": Decimal("100")}, suffix_unit=True)
        self.assertEqual(lines, ["Equity 100.00 USD"])
        self.assertNotIn("(USD)", lines[0])

        # Detail form keeps the bracketed unit so each line is self-describing.
        detail: List[str] = []
        ui._native_lines(detail, "Floating PnL", {"USD": Decimal("100")}, signed=True)
        self.assertEqual(detail, ["Floating PnL (USD): +100.00 USD"])

    def test_buttons_preserved(self) -> None:
        view = self._render([
            make_account("ex1", "a1", balance_ok=True, equity="1000", positions_ok=True, positions=1),
        ])

        labels = [item["text"] for row in view.buttons for item in row]
        for expected in ("📈 Net Positions", "🏛 By Exchange", "📊 By Symbol",
                         "⚠️ Risks", "⚡ Actions", "🔍 Filter",
                         "🔄 Refresh", "⚙️ Settings", "◀ Back", "🏠 Home", "✕ Exit"):
            self.assertIn(expected, labels)


class _NoDesk:
    """Desk stub that refuses every operation (read-only guard)."""

    def execute(self, request: Any) -> Any:
        raise AssertionError(f"dashboard render must not fetch: {request!r}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()