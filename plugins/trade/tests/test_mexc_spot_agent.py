"""Offline tests for the MEXC Spot /tradespot agent."""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from typing import Any, Dict, List, Mapping
from unittest import mock
import json

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

    def test_capabilities_include_limit_ladder_and_new_order(self) -> None:
        caps = set(spot.capabilities())
        self.assertIn("balance", caps)
        self.assertIn("orders", caps)
        self.assertIn("open_orders", caps)
        self.assertIn("list_instruments", caps)
        self.assertIn("new_order", caps)
        self.assertIn("cancel_orders", caps)
        # Phase 5: ladder is now an advertised capability.
        self.assertIn("ladder", caps)
        # MARKET + cancel_order_group + cancel_order remain NOT_IMPLEMENTED.
        self.assertNotIn("market_order", caps)

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

    def test_resolve_instrument_accepts_base_quote_fields(self) -> None:
        exchange_info = {
            "symbols": [
                {
                    "symbol": "SOLUSDT",
                    "baseAsset": "SOL",
                    "quoteAsset": "USDT",
                    "baseAssetPrecision": 4,
                    "quoteAssetPrecision": 4,
                    "quotePrecision": 4,
                    "orderTypes": ["LIMIT", "MARKET"],
                    "isSpotTradingAllowed": True,
                    "status": "1",
                    "filters": [],
                }
            ]
        }
        with mock.patch.object(spot, "_public_request", return_value=exchange_info):
            resp = spot.execute({
                "operation": "resolve_instrument",
                "exchange": "mexc",
                "account": "amiroo",
                "base": "SOL",
                "quote": "USDT",
            })
        self.assertTrue(resp.success)
        data = resp.data
        self.assertIsNotNone(data)
        assert data is not None
        self.assertEqual(data["instrument"]["symbol"], "SOLUSDT")

    def test_cancel_all_remains_unimplemented(self) -> None:
        # Phase 5: ladder is now implemented. cancel_order_group / market_order
        # remain NOT_IMPLEMENTED.
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            for op in ("cancel_order_group", "market_order"):
                resp = spot.execute({"operation": op, "exchange": "mexc", "account": "amiroo"})
                self.assertFalse(resp.success)
                self.assertIsNotNone(resp.error)
                self.assertEqual(resp.error.code, "NOT_IMPLEMENTED")

    def _solusdc_market(self) -> Dict[str, Any]:
        return {
            "symbol": "SOLUSDC",
            "baseAsset": "SOL",
            "quoteAsset": "USDC",
            "baseAssetPrecision": 2,
            "quotePrecision": 2,
            "baseSizePrecision": "0.01",
            "orderTypes": ["LIMIT", "MARKET"],
            "isSpotTradingAllowed": True,
            "status": "1",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "minQty": "0.01", "stepSize": "0.01"},
            ],
        }

    def test_new_order_posts_signed_limit_and_returns_id(self) -> None:
        captured: Dict[str, Any] = {}

        def fake_signed(credentials, method, path, params=None):
            captured["method"] = method
            captured["path"] = path
            captured["params"] = dict(params or {})
            captured["secret_in_params"] = "secret" in str(params)
            return {"symbol": "SOLUSDC", "orderId": 555, "status": "NEW", "origQty": "0.1", "price": "101"}

        info = {"by_symbol": {"SOLUSDC": self._solusdc_market()}, "symbols": [self._solusdc_market()], "ts": 1.0}
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_exchange_info", return_value=info):
                with mock.patch.object(spot, "_self_symbols", return_value=({"SOLUSDC"}, None)):
                    with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
                        resp = spot.execute({
                            "operation": "new_order",
                            "exchange": "mexc",
                            "account": "amiroo",
                            "symbol": "SOLUSDC",
                            "side": "BUY",
                            "order_type": "LIMIT",
                            "quantity": "0.129",
                            "price": "101.239",
                            "client_order_id": "tok123",
                        })
        self.assertTrue(resp.success)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/api/v3/order")
        self.assertEqual(captured["params"]["type"], "LIMIT")
        self.assertEqual(captured["params"]["side"], "BUY")
        self.assertEqual(captured["params"]["quantity"], "0.12")
        self.assertEqual(captured["params"]["price"], "101.23")
        self.assertFalse(captured["secret_in_params"])
        self.assertEqual(str(resp.order.exchange_order_id), "555")
        self.assertNotIn("secret", str(resp.data))

    def test_new_order_rejects_market(self) -> None:
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            resp = spot.execute({
                "operation": "new_order",
                "account": "amiroo",
                "symbol": "SOLUSDC",
                "side": "BUY",
                "order_type": "MARKET",
                "quantity": "1",
                "price": "1",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "LIMIT_ONLY")

    def test_new_order_timeout_is_unknown_and_not_retried(self) -> None:
        import urllib.error

        calls = {"n": 0}

        def boom(*_a, **_k):
            calls["n"] += 1
            raise urllib.error.URLError("timed out")
        info = {"by_symbol": {"SOLUSDC": self._solusdc_market()}, "symbols": [self._solusdc_market()], "ts": 1.0}
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_exchange_info", return_value=info):
                with mock.patch.object(spot, "_self_symbols", return_value=({"SOLUSDC"}, None)):
                    with mock.patch.object(spot, "_signed_request", side_effect=boom):
                        resp = spot.execute({
                            "operation": "new_order",
                            "account": "amiroo",
                            "symbol": "SOLUSDC",
                            "side": "BUY",
                            "order_type": "LIMIT",
                            "quantity": "0.1",
                            "price": "101",
                        })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "ORDER_STATUS_UNKNOWN")
        self.assertEqual(calls["n"], 1)

    def test_new_order_exchange_rejection(self) -> None:
        info = {"by_symbol": {"SOLUSDC": self._solusdc_market()}, "symbols": [self._solusdc_market()], "ts": 1.0}
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_exchange_info", return_value=info):
                with mock.patch.object(spot, "_self_symbols", return_value=({"SOLUSDC"}, None)):
                    with mock.patch.object(spot, "_signed_request", return_value={"code": -2010, "msg": "insufficient USDC"}):
                        resp = spot.execute({
                            "operation": "new_order",
                            "account": "amiroo",
                            "symbol": "SOLUSDC",
                            "side": "BUY",
                            "order_type": "LIMIT",
                            "quantity": "0.1",
                            "price": "101",
                        })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "EXCHANGE_REJECTED")
        self.assertIn("insufficient USDC", resp.error.message)


class MexcSpotQtyIncrementTests(unittest.TestCase):
    def setUp(self) -> None:
        spot._MARKET_CACHE.update({"ts": 0.0, "symbols": [], "by_symbol": {}})

    def _live_row(self, symbol: str, **fields: Any) -> Dict[str, Any]:
        row = {
            "symbol": symbol,
            "status": "1",
            "baseAsset": symbol.replace("USDC", ""),
            "quoteAsset": "USDC",
            "isSpotTradingAllowed": True,
            "orderTypes": ["LIMIT", "MARKET", "LIMIT_MAKER"],
            "filters": [{"filterType": "PERCENT_PRICE_BY_SIDE", "bidMultiplierUp": "0.1", "askMultiplierDown": "0.1"}],
        }
        row.update(fields)
        return row

    def _resolve(self, row: Dict[str, Any]) -> Any:
        with mock.patch.object(spot, "_public_request", return_value={"symbols": [row]}):
            return spot.execute({
                "operation": "resolve_instrument",
                "exchange": "mexc",
                "account": "amiroo",
                "symbol": row["symbol"],
            })

    def _instrument(self, resp: Any) -> Dict[str, Any]:
        self.assertTrue(resp.success, getattr(resp, "error", None))
        assert resp.data is not None
        return resp.data["instrument"]

    def test_suiusdc_resolve_exposes_normalized_steps(self) -> None:
        inst = self._instrument(self._resolve(self._live_row(
            "SUIUSDC",
            baseAssetPrecision=2,
            quotePrecision=4,
            quoteAssetPrecision=4,
            baseSizePrecision="0",
        )))
        self.assertEqual(inst["base"], "SUI")
        self.assertEqual(inst["quote"], "USDC")
        self.assertEqual(inst["size_step"], "0.01")
        self.assertEqual(inst["price_tick"], "0.0001")
        self.assertEqual(inst.get("min_qty") or "", "")
        self.assertEqual(inst.get("max_qty") or "", "")
        # MEXC spot always stamps its 1 USDC/USDT min-notional policy on
        # the resolved instrument so the exchange-neutral ladder planner
        # can enforce it without baking the default into spot_ladder.py.
        self.assertEqual(inst.get("min_notional") or "", "1")
        self.assertEqual(spot._quantize_down(Decimal("0.9"), Decimal(inst["size_step"])), Decimal("0.9"))

    def test_hypeusdc_resolve_exposes_normalized_steps(self) -> None:
        inst = self._instrument(self._resolve(self._live_row(
            "HYPEUSDC",
            baseAssetPrecision=2,
            quotePrecision=2,
            quoteAssetPrecision=2,
            baseSizePrecision="0",
        )))
        self.assertEqual(inst["size_step"], "0.01")
        self.assertEqual(inst["price_tick"], "0.01")
        self.assertEqual(spot._quantize_down(Decimal("0.1"), Decimal(inst["size_step"])), Decimal("0.1"))

    def test_solusdc_resolve_uses_base_size_precision_step_string(self) -> None:
        inst = self._instrument(self._resolve(self._live_row(
            "SOLUSDC",
            baseAssetPrecision=2,
            quotePrecision=2,
            quoteAssetPrecision=2,
            baseSizePrecision="0.000001",
        )))
        self.assertEqual(inst["size_step"], "0.000001")
        self.assertEqual(inst["price_tick"], "0.01")
        self.assertEqual(spot._quantize_down(Decimal("0.1"), Decimal(inst["size_step"])), Decimal("0.1"))
        self.assertEqual(spot._quantize_down(Decimal("0.0000019"), Decimal(inst["size_step"])), Decimal("0.000001"))

    def test_lot_size_and_price_filter_win(self) -> None:
        inst = self._instrument(self._resolve(self._live_row(
            "SOLUSDC",
            baseAssetPrecision=2,
            quotePrecision=2,
            quoteAssetPrecision=2,
            baseSizePrecision="0.000001",
            filters=[
                {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.05"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
            ],
        )))
        self.assertEqual(inst["size_step"], "0.001")
        self.assertEqual(inst["price_tick"], "0.05")
        self.assertEqual(inst["min_qty"], "0.001")
        self.assertEqual(inst["max_qty"], "1000")
        self.assertEqual(inst["min_notional"], "5")

    def test_integer_base_size_precision_is_a_step_not_places(self) -> None:
        inst = self._instrument(self._resolve(self._live_row(
            "FOOUSDC",
            baseAssetPrecision=2,
            quotePrecision=2,
            baseSizePrecision="1",
        )))
        self.assertEqual(inst["size_step"], "1")
        self.assertEqual(spot._quantize_down(Decimal("0.9"), Decimal(inst["size_step"])), Decimal("0"))

    def test_resolve_fails_when_size_or_price_step_cannot_be_derived(self) -> None:
        resp = self._resolve(self._live_row(
            "BADUSDC",
            baseAssetPrecision="",
            quotePrecision="",
            quoteAssetPrecision="",
            baseSizePrecision="0",
            filters=[{"filterType": "PERCENT_PRICE_BY_SIDE"}],
        ))
        self.assertFalse(resp.success)
        self.assertIsNotNone(resp.error)
        self.assertEqual(resp.error.code, "INSTRUMENT_CONSTRAINTS_UNAVAILABLE")


class MexcSpotLadderStubTests(unittest.TestCase):
    # Phase 5: `ladder` is now an advertised capability with full submit
    # logic. These legacy stubs assert the agent still rejects MARKET and
    # the unsupported `cancel_order_group` / `cancel_order` operations.

    def setUp(self) -> None:
        spot._MARKET_CACHE.update({"ts": 0.0, "symbols": [], "by_symbol": {}})

    def test_ladder_capability_is_advertised(self) -> None:
        self.assertIn("ladder", spot.capabilities())

    def test_market_order_remains_not_implemented(self) -> None:
        resp = spot.execute({
            "operation": "market_order",
            "exchange": "mexc",
            "account": "amiroo",
        })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "NOT_IMPLEMENTED")

    def test_cancel_order_remains_not_implemented(self) -> None:
        resp = spot.execute({
            "operation": "cancel_order",
            "exchange": "mexc",
            "account": "amiroo",
        })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "NOT_IMPLEMENTED")


class MexcSpotCancelOrdersTests(unittest.TestCase):
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

    def test_cancel_orders_deletes_only_listed_ids(self) -> None:
        calls: List[Dict[str, Any]] = []
        open_rows = [
            {"symbol": "SOLUSDC", "side": "BUY", "type": "LIMIT", "price": "73", "origQty": "1", "executedQty": "0", "status": "NEW", "orderId": "1"},
            {"symbol": "SOLUSDC", "side": "SELL", "type": "LIMIT", "price": "120", "origQty": "1", "executedQty": "0", "status": "NEW", "orderId": "9"},
            {"symbol": "SOLUSDT", "side": "BUY", "type": "LIMIT", "price": "74", "origQty": "1", "executedQty": "0", "status": "NEW", "orderId": "8"},
        ]

        def fake_signed(_credentials, method, path, params=None):
            calls.append({"method": method, "path": path, "params": dict(params or {})})
            if method.upper() == "DELETE":
                return {"symbol": "SOLUSDC", "orderId": params.get("orderId")}
            if path == "/api/v3/openOrders":
                remaining_ids = {c["params"]["orderId"] for c in calls if c["method"].upper() == "DELETE"}
                return [row for row in open_rows if str(row["orderId"]) not in remaining_ids]
            raise AssertionError(f"unexpected {method} {path}")

        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
                resp = spot.execute({
                    "operation": "cancel_orders",
                    "exchange": "mexc",
                    "account": "amiroo",
                    "symbol": "SOLUSDC",
                    "side": "BUY",
                    "order_ids": ["1"],
                })
        self.assertTrue(resp.success)
        deletes = [c for c in calls if c["method"].upper() == "DELETE"]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0]["path"], "/api/v3/order")
        self.assertEqual(deletes[0]["params"]["symbol"], "SOLUSDC")
        self.assertEqual(str(deletes[0]["params"]["orderId"]), "1")
        self.assertFalse(any(c["params"].get("orderId") in {"9", "8"} for c in deletes))
        self.assertEqual(resp.data["cancelled"], 1)
        self.assertEqual(resp.data["remaining"], 0)

    def test_cancel_orders_without_ids_does_not_hit_exchange(self) -> None:
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_signed_request", side_effect=AssertionError("no HTTP")):
                resp = spot.execute({
                    "operation": "cancel_orders",
                    "account": "amiroo",
                    "symbol": "SOLUSDC",
                    "side": "BUY",
                })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "MISSING_ORDER_IDS")

    def test_cancel_timeout_does_not_retry_delete(self) -> None:
        import urllib.error

        calls = {"delete": 0, "get": 0}

        def fake_signed(_credentials, method, path, params=None):
            if method.upper() == "DELETE":
                calls["delete"] += 1
                raise urllib.error.URLError("timed out")
            calls["get"] += 1
            return [{"symbol": "SOLUSDC", "side": "BUY", "type": "LIMIT", "price": "73", "origQty": "1", "executedQty": "0", "status": "NEW", "orderId": "1"}]

        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
                resp = spot.execute({
                    "operation": "cancel_orders",
                    "account": "amiroo",
                    "symbol": "SOLUSDC",
                    "side": "BUY",
                    "order_ids": ["1"],
                })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "CANCEL_STATUS_UNKNOWN")
        self.assertEqual(calls["delete"], 1)
        self.assertEqual(calls["get"], 1)


class MexcSpotLadderTests(unittest.TestCase):
    """Phase 5: live ladder submission via MEXC batchOrders.

    All HTTP is mocked. No live POST /api/v3/order, DELETE, or
    POST /api/v3/batchOrders ever fires during these tests.
    """

    def setUp(self) -> None:
        self.saved = {k: os.environ.get(k) for k in list(os.environ) if k.startswith("MEXC_") or k == "HERMES_HOME"}
        for k in list(os.environ):
            if k.startswith("MEXC_"):
                os.environ.pop(k, None)
        os.environ["HERMES_HOME"] = "/tmp/no-such-hermes-mexc-ladder"
        os.environ["MEXC_AMIROO_ACCESSKEY"] = "key"
        os.environ["MEXC_AMIROO_SECRETKEY"] = "secret"
        # Reset the in-memory cache so setUp order is deterministic.
        spot._MARKET_CACHE.update({"ts": 0.0, "symbols": [], "by_symbol": {}})

    def tearDown(self) -> None:
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        spot._MARKET_CACHE.update({"ts": 0.0, "symbols": [], "by_symbol": {}})

    def _children(self, n: int, *, price_first: str = "100", price_step: str = "0.01") -> list:
        out = []
        for i in range(n):
            qty = f"{(i + 1) * Decimal('0.000001'):f}".rstrip("0").rstrip(".") or "0"
            price = (Decimal(price_first) - Decimal(price_step) * i).quantize(Decimal("0.01"))
            out.append({
                "symbol": "SOLUSDC",
                "side": "BUY",
                "quantity": qty,
                "price": str(price),
                "client_order_id": f"lad_abc123_{i:03d}",
                "instrument": {"symbol": "SOLUSDC"},
            })
        return out

    def _request(self, n: int, **overrides):
        body = {
            "operation": "ladder",
            "exchange": "mexc",
            "account": "amiroo",
            "children": self._children(n),
        }
        body.update(overrides)
        return body

    # ----- capability -----
    def test_capability_advertised(self) -> None:
        self.assertIn("ladder", spot.capabilities())

    def test_ladder_result_uses_live_canonical_contract(self) -> None:
        """Regression: live CanonicalLadderResult has no expected_children kwarg."""
        import dataclasses
        from typing import Optional

        @dataclasses.dataclass(frozen=True)
        class LiveCanonicalLadderResult:
            symbol: str
            side: str
            distribution: str
            requested_order_count: int
            submitted_order_count: int
            requested_volume: str
            submitted_volume: str
            batch_count: int
            verified: bool
            partial: bool = False
            status: str = "success"
            accepted_child_count: Optional[int] = None
            omitted_order_count: Optional[int] = None
            omitted_below_minimum: Optional[int] = None
            child_order_ids: Optional[list[str | int]] = None
            batches: Optional[list[Dict[str, Any]]] = None
            rate_limited: Optional[bool] = None
            exchange_reason: Optional[str] = None

        def fake_signed(_c, method, p_path, params=None):
            if method.upper() == "POST" and p_path == "/api/v3/batchOrders":
                batch = json.loads(params.get("batchOrders", "[]"))
                return [
                    {"orderId": f"x{i}", "clientOrderId": b.get("newClientOrderId"), "symbol": "SOLUSDC", "price": b["price"], "origQty": b["quantity"], "status": "NEW"}
                    for i, b in enumerate(batch)
                ]
            if method.upper() == "GET" and p_path == "/api/v3/openOrders":
                return []
            return []

        with mock.patch.object(spot, "CanonicalLadderResult", LiveCanonicalLadderResult):
            with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
                with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
                    resp = spot.execute(self._request(10, distribution="uniform"))
        self.assertTrue(resp.success, msg=resp)
        self.assertIsNone(resp.error)
        self.assertEqual(resp.ladder.accepted_child_count, 10)
        self.assertEqual(resp.data["rejected"], 0)
        self.assertEqual(resp.data["unknown"], 0)
        self.assertEqual(resp.data["not_attempted"], 0)
        self.assertEqual(resp.data["planned_vwap"], resp.data["accepted_vwap"])
        self.assertEqual(len(resp.ladder.batches[0]["child_results"]), 10)

    # ----- batch-count parity -----
    def test_batch_count_table(self) -> None:
        # 1 / 10 / 20 / 21 / 40 / 41 / 50 / 100 / 200 / 500
        cases = [
            (1, 1),
            (10, 1),
            (20, 1),
            (21, 2),
            (40, 2),
            (41, 3),
            (50, 3),
            (100, 5),
            (200, 10),
            (500, 25),
        ]
        calls: list = []

        def fake_signed(_c, method, p_path, params=None):
            if p_path != "/api/v3/batchOrders":
                return []
            calls.append({"method": method.upper(), "path": p_path, "params": dict(params or {})})
            batch = json.loads(params.get("batchOrders", "[]"))
            return [
                {
                    "orderId": f"mock-{i}",
                    "clientOrderId": b.get("newClientOrderId"),
                    "symbol": "SOLUSDC",
                    "price": "100",
                    "origQty": "1",
                    "status": "NEW",
                }
                for i, b in enumerate(batch)
            ]

        for n, expected_batches in cases:
            calls.clear()
            with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
                with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
                    resp = spot.execute(self._request(n))
            self.assertTrue(resp.success, msg=f"n={n} resp={resp}")
            self.assertEqual(len(calls), expected_batches, msg=f"n={n} got {len(calls)} calls")
            for c in calls:
                self.assertEqual(c["method"], "POST")
                self.assertEqual(c["path"], "/api/v3/batchOrders")
                batch = json.loads(c["params"]["batchOrders"])
                self.assertGreaterEqual(len(batch), 1)
                self.assertLessEqual(len(batch), 20)

    # ----- max children per batch -----
    def test_max_children_per_batch_is_20(self) -> None:
        seen_sizes: list[int] = []

        def fake_signed(_c, _m, path, params=None):
            if path != "/api/v3/batchOrders":
                return []
            seen_sizes.append(len(json.loads(params.get("batchOrders", "[]"))))
            batch = json.loads(params.get("batchOrders", "[]"))
            return [
                {"orderId": f"x{i}", "clientOrderId": b.get("newClientOrderId"), "symbol": "SOLUSDC", "price": "100", "origQty": "1", "status": "NEW"}
                for i, b in enumerate(batch)
            ]

        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
                resp = spot.execute(self._request(500))
        self.assertTrue(resp.success)
        self.assertEqual(len(seen_sizes), 25)
        for i, s in enumerate(seen_sizes[:-1]):
            self.assertEqual(s, 20, f"batch {i} not 20")
        # Final batch is the remainder: 500 - 24*20 = 20.
        self.assertEqual(seen_sizes[-1], 20)

    # ----- idempotent client_order_id -----
    def test_idempotent_client_order_id(self) -> None:
        captured: list[list[str]] = []

        def fake_signed(_c, _m, _p, params=None):
            batch = json.loads(params.get("batchOrders", "[]"))
            captured.append([b.get("newClientOrderId") for b in batch])
            return [
                {"orderId": f"x{i}", "clientOrderId": b.get("newClientOrderId"), "symbol": "SOLUSDC", "price": "100", "origQty": "1", "status": "NEW"}
                for i, b in enumerate(batch)
            ]

        req = self._request(5)
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
                resp = spot.execute(req)
        # Children carry the user-provided client_order_id verbatim.
        flat = [cid for batch in captured for cid in batch]
        expected = [c["client_order_id"] for c in req["children"]]
        self.assertEqual(flat, expected)
        self.assertTrue(resp.success)

    # ----- MARKET rejected -----
    def test_market_type_rejected(self) -> None:
        req = self._request(3)
        for child in req["children"]:
            child["type"] = "MARKET"
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_signed_request", side_effect=AssertionError("no HTTP")):
                resp = spot.execute(req)
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "LIMIT_ONLY")

    # ----- empty children -----
    def test_missing_children_rejected(self) -> None:
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_signed_request", side_effect=AssertionError("no HTTP")):
                resp = spot.execute({
                    "operation": "ladder",
                    "account": "amiroo",
                    "children": [],
                })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "MISSING_CHILDREN")

    # ----- missing required field per child -----
    def test_child_missing_client_order_id_rejected(self) -> None:
        req = self._request(3)
        req["children"][0].pop("client_order_id")
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_signed_request", side_effect=AssertionError("no HTTP")):
                resp = spot.execute(req)
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "MISSING_CLIENT_ORDER_ID")

    # ----- per-child explicit rejection -----
    def test_per_child_rejection_classified(self) -> None:
        def fake_signed(_c, _m, _p, params=None):
            batch = json.loads(params.get("batchOrders", "[]"))
            results = []
            for i, b in enumerate(batch):
                if i == 1:
                    results.append({"code": 30002, "msg": "min notional"})
                else:
                    # The agent's payload uses `newClientOrderId` (per MEXC spec).
                    results.append({"orderId": f"x{i}", "clientOrderId": b.get("newClientOrderId"), "symbol": "SOLUSDC", "price": "100", "origQty": "1", "status": "NEW"})
            return results

        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
                resp = spot.execute(self._request(3))
        self.assertTrue(resp.success)
        self.assertEqual(resp.ladder.accepted_child_count, 2)
        self.assertEqual(resp.data["rejected"], 1)
        self.assertTrue(resp.ladder.partial)
        # Rejected child carries an error code in the per-batch breakdown.

    # ----- entire batch rejection -----
    def test_entire_batch_rejected(self) -> None:
        def fake_signed(_c, _m, _p, params=None):
            # All 5 children rejected by the exchange.
            return [{"code": -1021, "msg": "timestamp outside recvWindow"}] * 5

        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
                resp = spot.execute(self._request(5))
        # 5 children rejected by the exchange → partial submission. The
        # envelope is `success=True partial=True` because the call
        # completed and the agent did not retry.
        self.assertTrue(resp.success)
        self.assertTrue(resp.ladder.partial)
        self.assertEqual(resp.ladder.accepted_child_count, 0)
        self.assertEqual(resp.data["rejected"], 5)
        self.assertIsNotNone(resp.ladder.exchange_reason)

    # ----- ambiguous timeout -----
    def test_timeout_marks_unknown_and_stops_subsequent_batches(self) -> None:
        import urllib.error

        def fake_signed(_c, method, path, params=None):
            if path == "/api/v3/batchOrders":
                # First batch: request may have been transmitted (URLError timeout).
                raise urllib.error.URLError("timed out")
            return []

        calls = {"post": 0}

        def counter(_c, method, path, params=None):
            if method.upper() == "POST" and path == "/api/v3/batchOrders":
                calls["post"] += 1
            return fake_signed(_c, method, path, params)

        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_signed_request", side_effect=counter):
                resp = spot.execute(self._request(25))
        # 25 children: first batch (≤20) timed out → all UNKNOWN; remaining 5+ NOT_ATTEMPTED.
        # The envelope is `success=True partial=True` because the agent
        # refused to retry the ambiguous batch and refused to submit the
        # remaining ones.
        self.assertTrue(resp.success)
        self.assertTrue(resp.ladder.partial)
        self.assertEqual(resp.ladder.accepted_child_count, 0)
        self.assertEqual(resp.data["unknown"], 20)
        self.assertEqual(resp.data["not_attempted"], 5)
        # Subsequent batches must not be attempted after a timeout.
        self.assertEqual(calls["post"], 1)

    # ----- timeout on middle batch -----
    def test_timeout_on_middle_batch_stops(self) -> None:
        import urllib.error

        batch_idx = {"i": 0}

        def fake_signed(_c, method, path, params=None):
            if path == "/api/v3/batchOrders":
                batch_idx["i"] += 1
                if batch_idx["i"] == 2:  # middle batch
                    raise urllib.error.URLError("timed out")
                batch = json.loads(params.get("batchOrders", "[]"))
                return [
                    {"orderId": f"x{i}", "clientOrderId": b.get("newClientOrderId"), "symbol": "SOLUSDC", "price": "100", "origQty": "1", "status": "NEW"}
                    for i, b in enumerate(batch)
                ]
            return []

        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
                resp = spot.execute(self._request(45))  # 3 batches
        # First batch (20) accepted; second (20) timed out → UNKNOWN;
        # third (5) not attempted. STOP after middle batch.
        self.assertTrue(resp.success)
        self.assertTrue(resp.ladder.partial)
        self.assertEqual(batch_idx["i"], 2)  # stopped at middle batch

    # ----- partial success -----
    def test_partial_success_partial_true(self) -> None:
        def fake_signed(_c, _m, _p, params=None):
            batch = json.loads(params.get("batchOrders", "[]"))
            return [
                {"orderId": f"x{i}", "clientOrderId": b.get("newClientOrderId"), "symbol": "SOLUSDC", "price": "100", "origQty": "1", "status": "NEW"}
                if i % 2 == 0
                else {"code": -1121, "msg": "invalid symbol"}
                for i, b in enumerate(batch)
            ]

        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
                resp = spot.execute(self._request(4))
        self.assertTrue(resp.success)
        self.assertEqual(resp.ladder.accepted_child_count, 2)
        self.assertTrue(resp.ladder.partial)

    # ----- planned vs accepted VWAP -----
    def test_planned_vs_accepted_vwap(self) -> None:
        # Reject every other child so accepted VWAP differs from planned.
        def fake_signed(_c, _m, _p, params=None):
            batch = json.loads(params.get("batchOrders", "[]"))
            return [
                {"orderId": f"x{i}", "clientOrderId": b.get("newClientOrderId"), "symbol": "SOLUSDC", "price": b["price"], "origQty": b["quantity"], "status": "NEW"}
                if i % 2 == 0
                else {"code": -2010, "msg": "balance insufficient"}
                for i, b in enumerate(batch)
            ]

        req = self._request(6)
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
                resp = spot.execute(req)
        self.assertTrue(resp.success)
        self.assertIn("planned_vwap", resp.ladder.batches[-1])
        self.assertIn("accepted_vwap", resp.ladder.batches[-1])

    # ----- reconciliation by client_order_id -----
    def test_reconciliation_by_client_order_id(self) -> None:
        # Pass `reconcile=True` and verify the agent re-reads open orders.
        get_calls = {"openOrders": 0, "allOrders": 0}

        def fake_signed(_c, method, path, params=None):
            if method.upper() == "POST" and path == "/api/v3/batchOrders":
                batch = json.loads(params.get("batchOrders", "[]"))
                return [
                    {"orderId": f"x{i}", "clientOrderId": b.get("newClientOrderId"), "symbol": "SOLUSDC", "price": "100", "origQty": "1", "status": "NEW"}
                    for i, b in enumerate(batch)
                ]
            if path == "/api/v3/openOrders":
                get_calls["openOrders"] += 1
                return [{"orderId": "x0", "clientOrderId": "lad_abc123_000", "symbol": "SOLUSDC", "side": "BUY", "price": "100", "origQty": "1", "executedQty": "0", "status": "NEW"}]
            return []

        req = self._request(1)
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
                resp = spot.execute(req)
        self.assertTrue(resp.success)
        # Reconciliation re-reads open orders exactly once.
        self.assertEqual(get_calls["openOrders"], 1)

    # ----- 50-child batch-count table parity -----
    def test_50_child_partial_batches(self) -> None:
        calls = []

        def fake_signed(_c, _m, path, params=None):
            if path != "/api/v3/batchOrders":
                return []
            calls.append(len(json.loads(params.get("batchOrders", "[]"))))
            batch = json.loads(params.get("batchOrders", "[]"))
            return [
                {"orderId": f"x{i}", "clientOrderId": b.get("newClientOrderId"), "symbol": "SOLUSDC", "price": "100", "origQty": "1", "status": "NEW"}
                for i, b in enumerate(batch)
            ]

        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
                resp = spot.execute(self._request(50))
        self.assertTrue(resp.success)
        self.assertEqual(calls, [20, 20, 10])

    # ----- double-confirm idempotency -----
    def test_double_submit_yields_one_exchange_attempt(self) -> None:
        # The agent must not submit twice for the same client_order_ids.
        # The wizard layer enforces this via single-use tokens, but the
        # agent should still accept the same request twice without
        # creating duplicate exchange orders (the second submit just
        # finds the existing orders by client_order_id).
        post_calls: list[list[str]] = []

        def fake_signed(_c, _m, path, params=None):
            if path != "/api/v3/batchOrders":
                return []
            batch = json.loads(params.get("batchOrders", "[]"))
            post_calls.append([b.get("newClientOrderId") for b in batch])
            return [
                {"orderId": f"x{i}", "clientOrderId": b.get("newClientOrderId"), "symbol": "SOLUSDC", "price": "100", "origQty": "1", "status": "NEW"}
                for i, b in enumerate(batch)
            ]

        req = self._request(3)
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
                resp = spot.execute(req)
        self.assertTrue(resp.success)
        self.assertEqual(len(post_calls), 1)


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
        self.assertIn("new_order", agent.capabilities())
        self.assertIn("cancel_orders", agent.capabilities())
        # Phase 5: ladder is now advertised.
        self.assertIn("ladder", agent.capabilities())


if __name__ == "__main__":
    unittest.main()
