"""Unit/API tests for TradeMenu Phase 1 (read-only)."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from unittest import mock

from fastapi.testclient import TestClient

from plugins.trade.canonical import (
    CanonicalInstrument,
    CanonicalMarketPrice,
    CanonicalPosition,
    CanonicalResponse,
    make_failure,
    make_success,
)
from plugins.trade.trademenu.auth import LoginRateLimiter, SessionManager
from plugins.trade.trademenu.config import TradeMenuConfig
from plugins.trade.trademenu.app import create_app
from plugins.trade.trademenu.service import TradeMenuService


class FakeDesk:
    def __init__(self) -> None:
        self._exchanges = ["hyperliquid", "binance", "phemex"]
        self._accounts = {
            "hyperliquid": ["FIBO", "BITGET"],
            "binance": [{"account": "spot", "label": "Spot"}, {"account": "futures", "label": "Futures"}],
            "phemex": ["dramiroo"],
        }

    def list_exchanges(self) -> List[str]:
        return list(self._exchanges)

    def list_accounts(self, exchange: str) -> List[Any]:
        return list(self._accounts.get(exchange, []))

    def capabilities(self, exchange: str) -> List[str]:
        if exchange == "binance":
            return ["resolve_instrument", "market_price"]
        return ["resolve_instrument", "market_price", "positions_management", "positions_orders"]

    def execute(self, request: Dict[str, Any]) -> CanonicalResponse:
        op = str(request.get("operation") or "")
        ex = str(request.get("exchange") or "")
        acct = str(request.get("account") or "")
        if op == "resolve_instrument":
            sym = str(request.get("symbol") or "")
            native = "BTC" if ex == "hyperliquid" else "BTCUSDT"
            if sym.upper().replace("-", "") in {"BTCUSD", "BTCUSDT", "BTC"}:
                return make_success(
                    op,
                    ex,
                    acct,
                    instrument=CanonicalInstrument(requested_symbol=sym, symbol=native, display_name=native),
                )
            return make_failure(op, ex, acct, "INSTRUMENT_NOT_FOUND", "not found")
        if op == "market_price":
            return make_success(
                op,
                ex,
                acct,
                market_price=CanonicalMarketPrice(
                    requested_symbol=str(request.get("symbol") or ""),
                    market=ex,
                    price="75000.5",
                    mark_price="75000.5",
                ),
            )
        if op in {"positions_management", "positions_orders"}:
            if ex == "hyperliquid" and acct == "FIBO":
                return make_success(
                    op,
                    ex,
                    acct,
                    positions=[
                        CanonicalPosition(
                            symbol="BTC",
                            side="long",
                            size="1.5",
                            entry_price="74000",
                            pnl="100",
                            tp="77500",
                            sl="74500",
                        )
                    ],
                )
            return make_success(op, ex, acct, positions=[])
        return make_failure(op or "unknown", ex or "", acct or "", "NOT_IMPLEMENTED", "nope")


def _cfg() -> TradeMenuConfig:
    # Bypass env loader by constructing directly.
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


class TradeMenuAuthTests(unittest.TestCase):
    def test_session_roundtrip(self) -> None:
        sm = SessionManager(_cfg())
        tok = sm.issue()
        self.assertTrue(sm.verify(tok))
        self.assertFalse(sm.verify(tok + "x"))
        self.assertFalse(sm.verify(None))

    def test_password_compare(self) -> None:
        sm = SessionManager(_cfg())
        self.assertTrue(sm.password_ok("test-password-xyz"))
        self.assertFalse(sm.password_ok("wrong"))

    def test_rate_limiter_locks(self) -> None:
        lim = LoginRateLimiter(max_failures=3, lockout_seconds=30)
        self.assertTrue(lim.check("ip").allowed)
        lim.record_failure("ip")
        lim.record_failure("ip")
        gate = lim.record_failure("ip")
        self.assertFalse(gate.allowed)
        self.assertFalse(lim.check("ip").allowed)


class TradeMenuApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = _cfg()
        self.service = TradeMenuService(desk=FakeDesk())  # type: ignore[arg-type]
        self.app = create_app(config=self.cfg, service=self.service)
        self.client = TestClient(self.app)

    def _login(self, password: str = "test-password-xyz"):
        return self.client.post("/login", data={"password": password}, follow_redirects=False)

    def test_unauthenticated_root_shows_login(self) -> None:
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("TradeMenu", r.text)
        self.assertIn("Password", r.text)
        self.assertIn("unit-test-hint", r.text)

    def test_api_requires_auth(self) -> None:
        r = self.client.get("/api/exchanges")
        self.assertEqual(r.status_code, 401)

    def test_wrong_password_rejected(self) -> None:
        r = self._login("nope")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Invalid password", r.text)
        self.assertIsNone(r.cookies.get(self.cfg.cookie_name))

    def test_login_logout_flow(self) -> None:
        r = self._login()
        self.assertIn(r.status_code, (302, 303))
        self.assertTrue(self.client.cookies.get(self.cfg.cookie_name))
        r2 = self.client.get("/api/exchanges")
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json()["success"])
        self.assertIn("hyperliquid", r2.json()["exchanges"])
        r3 = self.client.post("/logout", follow_redirects=False)
        self.assertIn(r3.status_code, (302, 303))
        # TestClient may keep jar; clear and ensure unauth
        self.client.cookies.clear()
        r4 = self.client.get("/api/exchanges")
        self.assertEqual(r4.status_code, 401)

    def test_accounts_change_with_exchange(self) -> None:
        self._login()
        r = self.client.get("/api/accounts?exchange=hyperliquid")
        self.assertEqual(r.status_code, 200)
        aliases = [a["account"] for a in r.json()["accounts"]]
        self.assertEqual(set(aliases), {"BITGET", "FIBO"})
        r2 = self.client.get("/api/accounts?exchange=binance")
        aliases2 = [a["account"] for a in r2.json()["accounts"]]
        self.assertEqual(set(aliases2), {"futures", "spot"})

    def test_resolve_btcusd(self) -> None:
        self._login()
        r = self.client.get(
            "/api/instruments/resolve",
            params={"exchange": "hyperliquid", "account": "FIBO", "symbol": "BTCUSD"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["instrument"]["symbol"], "BTC")

    def test_positions_scoped(self) -> None:
        self._login()
        r = self.client.get("/api/positions", params={"exchange": "hyperliquid", "account": "FIBO"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["success"])
        self.assertEqual(len(body["positions"]), 1)
        self.assertEqual(body["positions"][0]["symbol"], "BTC")
        self.assertEqual(body["positions"][0]["tp"], "77500")
        r2 = self.client.get("/api/positions", params={"exchange": "hyperliquid", "account": "BITGET"})
        self.assertEqual(r2.json()["positions"], [])

    def test_candles_endpoint_uses_marketdata(self) -> None:
        self._login()
        fake_candles = [
            {"time": 1, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
            {"time": 2, "open": 1.5, "high": 2.5, "low": 1.2, "close": 2.0, "volume": 11},
        ]
        with mock.patch(
            "plugins.trade.trademenu.app.fetch_candles",
            return_value={
                "success": True,
                "exchange": "hyperliquid",
                "account": "FIBO",
                "symbol": "BTC",
                "timeframe": "15m",
                "candles": fake_candles,
            },
        ):
            r = self.client.get(
                "/api/candles",
                params={
                    "exchange": "hyperliquid",
                    "account": "FIBO",
                    "symbol": "BTCUSD",
                    "tf": "15m",
                },
            )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["success"])
        self.assertEqual(len(body["candles"]), 2)
        self.assertEqual(body["native_symbol"], "BTC")

    def test_candles_unsupported_exchange_clear_error(self) -> None:
        self._login()
        # Use real fetch_candles path for an exchange without candle support.
        # First add a fake exchange account validation by using phemex then patch validate.
        with mock.patch.object(self.service, "validate_exchange_account", return_value=None):
            with mock.patch.object(
                self.service,
                "resolve_instrument",
                return_value={"success": True, "instrument": {"symbol": "BTC"}},
            ):
                r = self.client.get(
                    "/api/candles",
                    params={"exchange": "qfex", "account": "amiroo", "symbol": "BTC", "tf": "15m"},
                )
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["error"]["code"], "UNSUPPORTED_CANDLES")

    def test_fail_closed_without_password(self) -> None:
        with mock.patch("plugins.trade.trademenu.app.load_config", side_effect=Exception("should not")):
            # Directly exercise create_app fail-closed branch via TradeMenuConfigError
            from plugins.trade.trademenu.config import TradeMenuConfigError

            def boom():
                raise TradeMenuConfigError("missing")

            with mock.patch("plugins.trade.trademenu.app.load_config", side_effect=TradeMenuConfigError("missing")):
                app = create_app()
                client = TestClient(app)
                r = client.get("/")
                self.assertEqual(r.status_code, 503)
                self.assertIn("unavailable", r.text.lower())


if __name__ == "__main__":
    unittest.main()
