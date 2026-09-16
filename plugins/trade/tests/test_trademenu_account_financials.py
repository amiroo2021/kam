"""TradeMenu account financials toolbar (TradeDesk balance normalization)."""

from __future__ import annotations

import unittest
from typing import Any, Dict, List

from fastapi.testclient import TestClient

from plugins.trade.canonical import (
    CanonicalBalance,
    CanonicalPortfolioSummary,
    make_failure,
    make_success,
)
from plugins.trade.trademenu.app import create_app
from plugins.trade.trademenu.config import TradeMenuConfig
from plugins.trade.trademenu.formatting import format_money
from plugins.trade.trademenu.service import TradeMenuService


def _cfg() -> TradeMenuConfig:
    cfg = object.__new__(TradeMenuConfig)
    cfg.password = "test-password-xyz"
    cfg.hint = "unit-test-hint"
    cfg.session_secret = "unit-test-session-secret-value-32b"
    cfg.cookie_name = "trademenu_session"
    cfg.session_max_age_seconds = 3600
    cfg.login_max_failures = 3
    cfg.login_lockout_seconds = 2
    cfg.host = "0.0.0.0"
    cfg.port = 8001
    return cfg


class FinancialsFakeDesk:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.mode = "full"  # full | balance_only | fail | unsupported
        self.balance_by_account: Dict[str, str] = {
            "A": "125430.25",
            "B": "10.00",
        }

    def list_exchanges(self) -> List[str]:
        return ["hyperliquid", "lighter", "nado"]

    def list_accounts(self, exchange: str) -> List[Any]:
        return ["A", "B"]

    def capabilities(self, exchange: str) -> List[str]:
        if self.mode == "unsupported":
            return ["positions_orders"]
        return ["balance", "positions_orders"]

    def execute(self, request: Dict[str, Any]):
        self.calls.append(dict(request))
        op = str(request.get("operation") or "")
        ex = str(request.get("exchange") or "")
        acct = str(request.get("account") or "")
        if op != "balance":
            return make_failure(op, ex, acct, "UNSUPPORTED", "no")
        if self.mode == "fail":
            return make_failure("balance", ex, acct, "BALANCE_UNAVAILABLE", "down")
        bal_val = self.balance_by_account.get(acct, "100.00")
        if self.mode == "balance_only":
            return make_success(
                "balance",
                ex,
                acct,
                balance=CanonicalBalance(value=bal_val, unit="USDC"),
            )
        return make_success(
            "balance",
            ex,
            acct,
            balance=CanonicalBalance(value=bal_val, unit="USDC"),
            portfolio_summary=CanonicalPortfolioSummary(
                account_value=bal_val if acct == "A" else "108793.50",
                withdrawable="38720.10" if acct == "A" else "5.00",
                margin_used="1000.00",
                total_position_value="5000.00",
                unit="USDC",
            ),
        )


class FormatMoneyTests(unittest.TestCase):
    def test_thousands(self) -> None:
        self.assertEqual(format_money("125430.25"), "125,430.25")
        self.assertEqual(format_money("8412"), "8,412.00")
        self.assertIsNone(format_money(None))
        self.assertIsNone(format_money(""))


class AccountFinancialsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.desk = FinancialsFakeDesk()
        self.svc = TradeMenuService(desk=self.desk, cache_ttl=0.01, session_secret="t")  # type: ignore[arg-type]

    def test_full_fields(self) -> None:
        out = self.svc.account_financials("hyperliquid", "A")
        self.assertTrue(out["success"])
        keys = [f["key"] for f in out["fields"]]
        self.assertEqual(keys, ["balance", "equity", "available"])
        self.assertEqual(out["balance_display"], "125,430.25")
        self.assertEqual(out["equity_display"], "125,430.25")
        self.assertEqual(out["available_display"], "38,720.10")
        self.assertEqual(out["available_label"], "Withdrawable")
        self.assertEqual(out["currency"], "USDC")
        short = next(f for f in out["fields"] if f["key"] == "available")
        self.assertEqual(short["short_label"], "Available")

    def test_balance_only_omits_equity_and_available(self) -> None:
        self.desk.mode = "balance_only"
        out = self.svc.account_financials("hyperliquid", "A")
        self.assertTrue(out["success"])
        self.assertEqual([f["key"] for f in out["fields"]], ["balance"])
        self.assertNotIn("equity", out)
        self.assertNotIn("available", out)

    def test_failed_balance(self) -> None:
        self.desk.mode = "fail"
        out = self.svc.account_financials("hyperliquid", "A")
        self.assertFalse(out["success"])
        self.assertEqual(out["fields"], [])
        self.assertEqual(out["error"]["code"], "BALANCE_UNAVAILABLE")

    def test_unsupported_capability(self) -> None:
        self.desk.mode = "unsupported"
        out = self.svc.account_financials("hyperliquid", "A")
        self.assertFalse(out["success"])
        self.assertEqual(out["error"]["code"], "UNSUPPORTED")

    def test_cache_hit_and_force(self) -> None:
        a = self.svc.account_financials("hyperliquid", "A")
        self.assertFalse(a.get("cache_hit"))
        b = self.svc.account_financials("hyperliquid", "A")
        self.assertTrue(b.get("cache_hit"))
        self.assertEqual(len([c for c in self.desk.calls if c["operation"] == "balance"]), 1)
        c = self.svc.account_financials("hyperliquid", "A", force=True)
        self.assertFalse(c.get("cache_hit"))
        self.assertEqual(len([c for c in self.desk.calls if c["operation"] == "balance"]), 2)

    def test_account_switch_uses_correct_cache_key(self) -> None:
        a = self.svc.account_financials("hyperliquid", "A")
        b = self.svc.account_financials("hyperliquid", "B")
        self.assertEqual(a["balance"], "125430.25")
        self.assertEqual(b["balance"], "10.00")
        a2 = self.svc.account_financials("hyperliquid", "A")
        self.assertTrue(a2.get("cache_hit"))
        self.assertEqual(a2["balance"], "125430.25")

    def test_write_invalidates_balance_cache(self) -> None:
        self.svc.account_financials("hyperliquid", "A")
        self.assertTrue(self.svc.account_financials("hyperliquid", "A").get("cache_hit"))

        def execute(req):
            self.desk.calls.append(dict(req))
            op = req.get("operation")
            if op == "balance":
                return make_success(
                    "balance",
                    req["exchange"],
                    req["account"],
                    balance=CanonicalBalance(value="1.00", unit="USDC"),
                )
            return make_success(str(op), req["exchange"], req["account"])

        self.desk.execute = execute  # type: ignore[method-assign]
        self.svc._execute_write("set_tp", "hyperliquid", "A", {"symbol": "BTC", "price": "1"})
        out = self.svc.account_financials("hyperliquid", "A")
        self.assertFalse(out.get("cache_hit"))


class AccountFinancialsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.desk = FinancialsFakeDesk()
        self.cfg = _cfg()
        self.svc = TradeMenuService(desk=self.desk, session_secret=self.cfg.session_secret)  # type: ignore[arg-type]
        self.app = create_app(config=self.cfg, service=self.svc)
        self.client = TestClient(self.app)

    def _login(self) -> None:
        r = self.client.post("/login", data={"password": "test-password-xyz"}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def test_api_full(self) -> None:
        self._login()
        r = self.client.get(
            "/api/account/financials",
            params={"exchange": "hyperliquid", "account": "A"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["balance_display"], "125,430.25")
        self.assertEqual(body["available_display"], "38,720.10")

    def test_api_auth_required(self) -> None:
        r = self.client.get(
            "/api/account/financials",
            params={"exchange": "hyperliquid", "account": "A"},
        )
        self.assertEqual(r.status_code, 401)

    def test_static_includes_financials_slot(self) -> None:
        self._login()
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("accountFinancials", r.text)
        # HTML loads app.js; function name lives in static JS (cache-busted)
        js = self.client.get("/static/app.js")
        self.assertEqual(js.status_code, 200)
        self.assertIn("loadAccountFinancials", js.text)
        self.assertIn("accountKey()", js.text)


if __name__ == "__main__":
    unittest.main()
