"""Offline tests for the MEXC Spot /tradespot agent."""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Mapping
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.spotdesk import SpotDesk  # noqa: E402
from plugins.trade.tradedesk import _exchange_name_from_filename as trade_exchange_name_from_filename  # noqa: E402
from plugins.trade.agents import x_mexc_agent_spot as spot  # noqa: E402


class MexcSpotEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = {k: os.environ.get(k) for k in list(os.environ) if k.startswith("MEXC_") or k == "HERMES_HOME"}
        for k in list(os.environ):
            if k.startswith("MEXC_"):
                os.environ.pop(k, None)
        spot._MARKET_CACHE.update({"ts": 0.0, "symbols": [], "by_symbol": {}})

    def tearDown(self) -> None:
        for k in list(os.environ):
            if k.startswith("MEXC_"):
                os.environ.pop(k, None)
        os.environ.pop("HERMES_HOME", None)
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_discovers_existing_mexc_credential_convention(self) -> None:
        os.environ["MEXC_AMIROO_ACCESSKEY"] = "key"
        os.environ["MEXC_AMIROO_SECRETKEY"] = "secret"
        os.environ["MEXC_HALF_ACCESSKEY"] = "key-only"
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            self.assertEqual(spot.list_accounts(), ["amiroo"])

    def test_capabilities_are_read_only(self) -> None:
        caps = set(spot.capabilities())
        self.assertIn("balance", caps)
        self.assertIn("orders", caps)
        self.assertIn("open_orders", caps)
        self.assertIn("list_instruments", caps)
        self.assertNotIn("new_order", caps)
        self.assertNotIn("ladder", caps)
        self.assertNotIn("cancel_orders", caps)

    def test_signed_request_uses_mexc_spot_signature_without_exposing_secret(self) -> None:
        captured: Dict[str, Any] = {}

        def fake_json(method: str, url: str, headers: Mapping[str, str]) -> Dict[str, Any]:
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = dict(headers)
            return {"ok": True}

        creds = {"access_key": "access_public", "secret_key": "secret_private", "spot_base": "https://api.mexc.com"}
        with mock.patch.object(spot.time, "time", return_value=1700000000.0):
            with mock.patch.object(spot, "_json_request", side_effect=fake_json):
                payload = spot._signed_request(creds, "GET", "/api/v3/account", {"recvWindow": "5000"})
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(captured["method"], "GET")
        self.assertEqual(captured["headers"]["X-MEXC-APIKEY"], "access_public")
        self.assertNotIn("secret_private", captured["url"])
        qs = captured["url"].split("?", 1)[1]
        signed_part, sig = qs.rsplit("&signature=", 1)
        expected = hmac.new(b"secret_private", signed_part.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(sig, expected)


class MexcSpotParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = {k: os.environ.get(k) for k in list(os.environ) if k.startswith("MEXC_") or k == "HERMES_HOME"}
        for k in list(os.environ):
            if k.startswith("MEXC_"):
                os.environ.pop(k, None)
        os.environ["MEXC_AMIROO_ACCESSKEY"] = "key"
        os.environ["MEXC_AMIROO_SECRETKEY"] = "secret"
        spot._MARKET_CACHE.update({"ts": 0.0, "symbols": [], "by_symbol": {}})

    def tearDown(self) -> None:
        for k in list(os.environ):
            if k.startswith("MEXC_"):
                os.environ.pop(k, None)
        os.environ.pop("HERMES_HOME", None)
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_spot_balance_filters_zero_and_handles_locked(self) -> None:
        account_payload = {
            "accountType": "SPOT",
            "canTrade": True,
            "permissions": ["SPOT"],
            "balances": [
                {"asset": "USDT", "free": "10", "locked": "2.5"},
                {"asset": "SOL", "free": "0", "locked": "0"},
                {"asset": "MX", "free": "1.25", "locked": "0.75"},
            ],
        }
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_signed_request", return_value=account_payload):
                resp = spot.execute({"operation": "balance", "exchange": "mexc", "account": "amiroo"})
        self.assertTrue(resp.success)
        self.assertIsNotNone(resp.balance)
        self.assertIsNotNone(resp.data)
        self.assertEqual(resp.balance.value, "12.50")
        self.assertEqual(resp.balance.unit, "USDT")
        assets = resp.data["assets"]
        self.assertEqual([a["asset"] for a in assets], ["USDT", "MX"])
        self.assertEqual(assets[0]["free"], "10")
        self.assertEqual(assets[0]["locked"], "2.5")
        self.assertEqual(assets[0]["total"], "12.5")

    def test_spot_open_order_parsing(self) -> None:
        orders = [
            {
                "symbol": "SOLUSDC",
                "side": "BUY",
                "type": "LIMIT",
                "price": "123.45",
                "origQty": "4",
                "executedQty": "1.5",
                "status": "NEW",
                "orderId": "abc123",
            }
        ]
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_signed_request", return_value=orders):
                resp = spot.execute({"operation": "orders", "exchange": "mexc", "account": "amiroo"})
        self.assertTrue(resp.success)
        self.assertIsNotNone(resp.data)
        self.assertIsNotNone(resp.order_groups)
        self.assertEqual(resp.open_order_count, 1)
        self.assertEqual(resp.data["orders"][0]["pair"], "SOL/USDC")
        self.assertEqual(resp.data["orders"][0]["remaining_qty"], "2.5")
        self.assertEqual(resp.order_groups[0].symbol, "SOL/USDC")
        self.assertEqual(resp.order_groups[0].total_size, "2.5")

    def test_symbol_api_eligibility_parsing(self) -> None:
        exchange_info = {
            "symbols": [
                {
                    "symbol": "SOLUSDC",
                    "baseAsset": "SOL",
                    "quoteAsset": "USDC",
                    "baseAssetPrecision": 4,
                    "quoteAssetPrecision": 4,
                    "quotePrecision": 4,
                    "orderTypes": ["LIMIT", "MARKET"],
                    "isSpotTradingAllowed": True,
                    "status": "1",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "minNotional": "1"},
                    ],
                },
                {
                    "symbol": "DELISTEDUSDT",
                    "baseAsset": "DELISTED",
                    "quoteAsset": "USDT",
                    "orderTypes": ["LIMIT"],
                    "isSpotTradingAllowed": False,
                    "status": "0",
                    "filters": [],
                },
            ]
        }
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_public_request", return_value=exchange_info):
                with mock.patch.object(spot, "_self_symbols", return_value=({"SOLUSDC"}, None)):
                    resp = spot.execute({"operation": "list_instruments", "exchange": "mexc", "account": "amiroo"})
        self.assertTrue(resp.success)
        self.assertIsNotNone(resp.data)
        rows = {r["symbol"]: r for r in resp.data["instruments"]}
        self.assertTrue(rows["SOLUSDC"]["api_eligible"])
        self.assertTrue(rows["SOLUSDC"]["api_enabled_for_key"])
        self.assertFalse(rows["DELISTEDUSDT"]["api_eligible"])
        self.assertFalse(rows["DELISTEDUSDT"]["api_enabled_for_key"])
        self.assertEqual(rows["SOLUSDC"]["tick_size"], "0.01")
        self.assertEqual(rows["SOLUSDC"]["step_size"], "0.001")
        self.assertEqual(rows["SOLUSDC"]["min_notional"], "1")

    def test_write_operations_are_not_possible(self) -> None:
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            for op in ("new_order", "ladder", "cancel_orders", "cancel_order_group"):
                resp = spot.execute({"operation": op, "exchange": "mexc", "account": "amiroo"})
                self.assertFalse(resp.success)
                self.assertIsNotNone(resp.error)
                self.assertEqual(resp.error.code, "NOT_IMPLEMENTED")


class MexcSpotDiscoveryNamespaceTests(unittest.TestCase):
    def test_spotdesk_discovers_mexc_spot_agent(self) -> None:
        desk = SpotDesk()
        self.assertIn("mexc", desk.list_exchanges())

    def test_trade_and_spot_namespaces_can_both_have_mexc(self) -> None:
        spot_desk = SpotDesk()
        self.assertIn("mexc", spot_desk.list_exchanges())
        # TradeDesk should still parse/use the normal futures/general agent filename,
        # while SpotDesk owns the spot filename.
        self.assertEqual(trade_exchange_name_from_filename("x_mexc_agent.py"), "mexc")
        self.assertIsNone(trade_exchange_name_from_filename("x_mexc_agent_spot.py"))

    def test_tradespot_uses_spot_module_not_trade_module(self) -> None:
        desk = SpotDesk()
        desk._ensure_loaded()
        agent = desk._agents.get("mexc")
        self.assertIsNotNone(agent)
        self.assertTrue(str(getattr(agent, "__file__", "")).endswith("x_mexc_agent_spot.py"))
        assert agent is not None
        self.assertNotIn("new_order", agent.capabilities())


if __name__ == "__main__":
    unittest.main()
