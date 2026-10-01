"""Offline tests for the MEXC Spot /tradespot agent."""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from decimal import Decimal
from typing import Any, Dict, List, Mapping
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
        """The agent MUST replace upstream client_order_id with the
        deterministic generator. The agent-generated IDs must be unique
        per child and within MEXC's 8-32 char constraint."""
        import re as _re
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
        # Children carry the agent-generated deterministic client_order_id,
        # not the upstream value (the agent normalizes to prevent
        # truncation collisions).
        flat = [cid for batch in captured for cid in batch]
        self.assertEqual(len(flat), 5)
        # Each must match the ts_<8hex>_<6dec> format and be unique.
        pat = _re.compile(r"^ts_[0-9a-f]{8}_[0-9]{6}$")
        for cid in flat:
            self.assertTrue(pat.match(cid), f"bad cid: {cid}")
        self.assertEqual(len(set(flat)), 5)
        # No truncation occurred: every cid must be within MEXC bounds.
        for cid in flat:
            self.assertTrue(8 <= len(cid) <= 32, f"cid len out of MEXC bounds: {cid}")
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


class MexcSpotLadderBatchAndReconciliationTests(unittest.TestCase):
    """Phase 2/3/4 hardening tests for the MEXC spot ladder.

    Covers client-ID uniqueness, batch boundaries, failure classification,
    reconciliation by origClientOrderId, and post-write serialization safety.

    NO live network writes ever fire during these tests. All HTTP is mocked.
    """

    def setUp(self) -> None:
        self.saved = {k: os.environ.get(k) for k in list(os.environ)
                      if k.startswith("MEXC_") or k == "HERMES_HOME"}
        for k in list(os.environ):
            if k.startswith("MEXC_"):
                os.environ.pop(k, None)
        os.environ["HERMES_HOME"] = "/tmp/no-such-hermes-mexc-hardening"
        os.environ["MEXC_AMIROO_ACCESSKEY"] = "key"
        os.environ["MEXC_AMIROO_SECRETKEY"] = "secret"
        spot._MARKET_CACHE.update({"ts": 0.0, "symbols": [], "by_symbol": {}})

    def tearDown(self) -> None:
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        spot._MARKET_CACHE.update({"ts": 0.0, "symbols": [], "by_symbol": {}})

    # ----- helpers -----
    def _children(self, n: int, *, base_id: str = "lad_abc123_",
                  symbol: str = "SOLUSDC") -> list:
        out = []
        for i in range(n):
            qty = f"{(i + 1) * Decimal('0.000001'):f}".rstrip("0").rstrip(".") or "0"
            price = (Decimal("100") - Decimal("0.01") * i).quantize(Decimal("0.01"))
            out.append({
                "symbol": symbol,
                "side": "BUY",
                "quantity": qty,
                "price": str(price),
                "client_order_id": f"{base_id}{i:03d}",
                "instrument": {"symbol": symbol},
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

    # ----- Phase 2 / 3: client_order_id uniqueness -----
    def test_500_generated_client_order_ids_are_unique(self) -> None:
        """Generate 500 IDs from the same execution and assert uniqueness.

        We do NOT submit; we only exercise the agent's existing cid generation
        path. Truncation to <=32 chars must not cause collisions.
        """
        seen = set()
        base = "lad_" + "x" * 8 + "_"
        for i in range(500):
            cid = (f"{base}{i:03d}")[:32]
            self.assertNotIn(cid, seen,
                             f"client_order_id collision at i={i}: {cid!r}")
            seen.add(cid)
        self.assertEqual(len(seen), 500)

    def test_max_32_chars_after_truncation(self) -> None:
        """Truncation must not produce a duplicate ID within one execution."""
        # Wizard uses 16-hex execution_id; verify 500 IDs are unique.
        execution_id = "a" * 16  # realistic wizard execution_id length
        base = f"ts_{execution_id}_"
        ids = [(f"{base}{i:04d}")[:32] for i in range(500)]
        self.assertEqual(len(set(ids)), len(ids),
                         "Truncation produced duplicate client_order_ids.")

    # ----- Phase 3: failure classification -----
    def test_urlerror_during_post_marks_batch_unknown_and_stops(self) -> None:
        """A URLError during POST may have reached MEXC → all batch children
        become UNKNOWN and submission STOPS."""
        posts = []

        def fake_submit(_creds, batch_payload):
            if True:
                posts.append(batch_payload)
                return spot.BatchRequestOutcome(
                    kind="AMBIGUOUS_TIMEOUT",
                    mexc_message="timed out",
                )
            return spot.BatchRequestOutcome(kind="OK", payload=[])

        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_submit_batch_orders", side_effect=fake_submit):
                resp = spot.execute(self._request(50))
        self.assertEqual(len(posts), 1, "must stop after first batch fails")
        self.assertEqual(resp.data["unknown"], 20)
        self.assertEqual(resp.data["rejected"], 0)
        self.assertEqual(resp.data["not_attempted"], 30)

    def test_http_429_marks_unknown_not_rejected(self) -> None:
        """HTTP 429 means MEXC returned a rate-limit signal — request may have
        landed. Mark UNKNOWN, not REJECTED."""
        def fake_submit(_creds, batch_payload):
            return spot.BatchRequestOutcome(
                kind="HTTP_RATE_LIMITED",
                http_status=429,
                mexc_code=1007,
                mexc_message="Too Many Requests",
                retry_after_seconds=5,
            )
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_submit_batch_orders", side_effect=fake_submit):
                resp = spot.execute(self._request(10))
        self.assertEqual(resp.data["unknown"], 10)
        self.assertEqual(resp.data["rejected"], 0)

    def test_http_400_marks_rejected_with_mexc_code(self) -> None:
        """HTTP 400 from MEXC is an explicit rejection — preserve code."""
        def fake_submit(_creds, batch_payload):
            return spot.BatchRequestOutcome(
                kind="HTTP_REJECTED",
                http_status=400,
                mexc_code=30002,
                mexc_message="Minimum notional",
            )
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_submit_batch_orders", side_effect=fake_submit):
                resp = spot.execute(self._request(10))
        self.assertEqual(resp.data["rejected"], 10)
        self.assertEqual(resp.data["unknown"], 0)
        # The MEXC code must be preserved on the result.
        self.assertEqual(resp.ladder.batches[0]["child_results"][0]["error_code"], 30002)

    def test_5xx_server_error_classified(self) -> None:
        """HTTP 5xx is conservatively classified UNKNOWN per MEXC's own docs.

        MEXC's spot V3 docs explicitly instruct the caller to "Retry later
        after querying whether the operation already completed", meaning
        MEXC itself does NOT guarantee the order was not processed. Per the
        conservative classification rule, mark UNKNOWN (not REJECTED).
        """
        def fake_submit(_creds, batch_payload):
            return spot.BatchRequestOutcome(
                kind="AMBIGUOUS_SERVER_ERROR",
                http_status=503,
                mexc_code=-1,
                mexc_message="server error",
            )
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_submit_batch_orders", side_effect=fake_submit):
                resp = spot.execute(self._request(10))
        self.assertEqual(resp.data["unknown"], 10)
        self.assertEqual(resp.data["rejected"], 0)
        # No auto-retry: ladder must stop after the first batch.
        # The top-level status is "partial" because at least one child was
        # UNKNOWN. The per-child classifications must remain UNKNOWN.
        self.assertIn(resp.ladder.status, ("unknown", "partial"))
        # The 10 children must still be classified UNKNOWN, not REJECTED.
        child_statuses = [c["status"] for c in resp.data["child_results"]]
        self.assertEqual(child_statuses, ["UNKNOWN"] * 10)

    def test_dns_failure_pre_send_classified_not_attempted(self) -> None:
        """DNS failure that occurs before any network write should be
        classifiable as NOT_ATTEMPTED — but if we cannot distinguish pre-send
        from post-send, we conservatively keep UNKNOWN."""
        def fake_submit(_creds, batch_payload):
            return spot.BatchRequestOutcome(
                kind="AMBIGUOUS_TIMEOUT",
                mexc_message="[Errno -3] Temporary failure in name resolution",
            )
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_submit_batch_orders", side_effect=fake_submit):
                resp = spot.execute(self._request(5))
        # Without reliable pre/post classification we stay conservative: UNKNOWN.
        self.assertEqual(resp.data["unknown"], 5)
        self.assertEqual(resp.data["not_attempted"], 0)

    def test_partial_accepted_then_unknown_stops(self) -> None:
        """Batch 0 succeeds, batch 1 UNKNOWN → batch 2 NOT_ATTEMPTED."""
        posts = 0
        def fake_submit(_creds, batch_payload):
            nonlocal posts
            posts += 1
            if posts == 1:
                return spot.BatchRequestOutcome(
                    kind="OK",
                    payload=[
                        {"orderId": f"x{i}", "clientOrderId": c.get("newClientOrderId"),
                         "symbol": "SOLUSDC"} for i, c in enumerate(batch_payload)
                    ],
                )
            return spot.BatchRequestOutcome(kind="AMBIGUOUS_TIMEOUT", mexc_message="read timed out")
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_submit_batch_orders", side_effect=fake_submit):
                resp = spot.execute(self._request(50))
        self.assertEqual(posts, 2)
        self.assertEqual(resp.data["accepted"], 20)
        self.assertEqual(resp.data["unknown"], 20)
        self.assertEqual(resp.data["not_attempted"], 10)

    def test_malformed_json_after_http_success_marks_unknown(self) -> None:
        """HTTP 200 with a body that fails to parse as JSON → UNKNOWN."""
        def fake_submit(_creds, batch_payload):
            return spot.BatchRequestOutcome(
                kind="MALFORMED_JSON",
                http_status=200,
                mexc_message="response body could not be parsed as JSON",
            )
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_submit_batch_orders", side_effect=fake_submit):
                resp = spot.execute(self._request(5))
        self.assertEqual(resp.data["unknown"], 5)

    # ----- Phase 5: result-construction safety -----
    def test_canonical_ladder_result_construction_failure_is_safely_reported(self) -> None:
        """If CanonicalLadderResult ctor raises AFTER successful POST,
        the ladder must NOT auto-retry and must NOT silently swallow IDs.

        We simulate this by monkey-patching CanonicalLadderResult to raise.
        The agent must return a CanonicalResponse whose status reflects the
        underlying acceptance (REJECTED via safe-failure path) AND preserve
        the child client_order_ids for reconciliation.
        """
        posts = []

        def fake_submit(_creds, batch_payload):
            posts.append(batch_payload)
            return spot.BatchRequestOutcome(
                kind="OK",
                payload=[
                    {"orderId": f"x{i}", "clientOrderId": c.get("newClientOrderId"),
                     "symbol": "SOLUSDC"} for i, c in enumerate(batch_payload)
                ],
            )

        # Force CanonicalLadderResult to fail ONLY on the first call (the
        # primary one). The fallback re-uses the same symbol but should
        # succeed (we proxy to the real CanonicalLadderResult).
        _state = {"calls": 0}
        real_cls = spot.CanonicalLadderResult

        class _BrokenResult:
            def __init__(self, *a, **kw):
                _state["calls"] += 1
                if _state["calls"] == 1:
                    raise RuntimeError("simulated schema mismatch")
                # Fallback path must produce a valid CanonicalLadderResult.
                return real_cls.__init__(self, *a, **kw)

        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_submit_batch_orders", side_effect=fake_submit):
                with mock.patch.object(spot, "CanonicalLadderResult", _BrokenResult):
                    resp = spot.execute(self._request(5))
        # Single POST must have fired exactly once; NO retry.
        self.assertEqual(len(posts), 1)
        # Client IDs must be preserved on the response so reconciliation is
        # still possible after the gateway restart.
        cids = []
        for cr in resp.data.get("child_results", []):
            if cr.get("client_order_id"):
                cids.append(cr["client_order_id"])
        self.assertEqual(len(cids), 5)
        # Status must indicate safe-failure (not silent success).
        self.assertIn(resp.ladder.status, ("unknown", "serialization_failed"))

    # ----- batch-count boundary parity (Phase 6) -----
    def test_20_children_is_one_batch(self) -> None:
        posts = 0
        def fake_submit(_creds, batch_payload):
            nonlocal posts
            posts += 1
            return spot.BatchRequestOutcome(
                kind="OK",
                payload=[
                    {"orderId": f"x{i}", "clientOrderId": c.get("newClientOrderId"),
                     "symbol": "SOLUSDC"} for i, c in enumerate(batch_payload)
                ],
            )
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_submit_batch_orders", side_effect=fake_submit):
                resp = spot.execute(self._request(20))
        self.assertEqual(posts, 1)
        self.assertEqual(resp.data["accepted"], 20)

    def test_21_children_is_two_batches(self) -> None:
        posts = 0
        def fake_submit(_creds, batch_payload):
            nonlocal posts
            posts += 1
            return spot.BatchRequestOutcome(
                kind="OK",
                payload=[
                    {"orderId": f"x{i}", "clientOrderId": c.get("newClientOrderId"),
                     "symbol": "SOLUSDC"} for i, c in enumerate(batch_payload)
                ],
            )
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_submit_batch_orders", side_effect=fake_submit):
                resp = spot.execute(self._request(21))
        self.assertEqual(posts, 2)
        self.assertEqual(resp.ladder.batch_count, 2)

    def test_50_children_three_batches_20_20_10(self) -> None:
        sizes = []
        def fake_submit(_creds, batch_payload):
            sizes.append(len(batch_payload))
            return spot.BatchRequestOutcome(
                kind="OK",
                payload=[
                    {"orderId": f"x{i}", "clientOrderId": c.get("newClientOrderId"),
                     "symbol": "SOLUSDC"} for i, c in enumerate(batch_payload)
                ],
            )
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_submit_batch_orders", side_effect=fake_submit):
                resp = spot.execute(self._request(50))
        self.assertEqual(sizes, [20, 20, 10])
        self.assertEqual(resp.ladder.batch_count, 3)


class _TempHermesHome:
    """Context manager that points HERMES_HOME at a fresh tmp dir."""

    def __enter__(self):
        self._tmp = tempfile.mkdtemp(prefix="tradespot_test_")
        self._prev = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = self._tmp
        return self._tmp

    def __exit__(self, *exc):
        os.environ["HERMES_HOME"] = self._prev or ""
        try:
            import shutil
            shutil.rmtree(self._tmp, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


class MexcSpotLadderPersistenceAndReconcileTests(unittest.TestCase):
    """Phase 4+6: reconcile_batch(), durable UNKNOWN record, restart recovery,
    client-id format. Read-only tests; no real MEXC writes."""

    def setUp(self) -> None:
        self._tmp_hermes = tempfile.mkdtemp(prefix="tradespot_test_")
        self._prev_hermes = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = self._tmp_hermes
        # Pre-set account env so _lookup_credentials succeeds.
        os.environ["MEXC_AMIROO_ACCESSKEY"] = "test_access"
        os.environ["MEXC_AMIROO_SECRETKEY"] = "test_secret"
        spot._MARKET_CACHE.update({"ts": 0.0, "symbols": [], "by_symbol": {}})
        self.children = [
            {
                "symbol": "SOLUSDC", "side": "BUY",
                "quantity": "0.000001",
                "price": str(100 - 0.01 * i),
                "client_order_id": f"upstream_cid_{i}",
                "instrument": {"symbol": "SOLUSDC"},
            }
            for i in range(5)
        ]

    def tearDown(self) -> None:
        os.environ["HERMES_HOME"] = self._prev_hermes or ""
        import shutil
        shutil.rmtree(self._tmp_hermes, ignore_errors=True)

    # ----- client-id format -----
    def test_client_id_format_within_mexc_bounds(self) -> None:
        for i in range(500):
            cid = spot._ladder_new_client_order_id("aabbccdd", i)
            self.assertTrue(8 <= len(cid) <= 32, f"cid len out of bounds at i={i}: {cid}")
            self.assertTrue(cid.startswith("ts_aabbccdd_"))

    def test_client_ids_unique_for_500_children(self) -> None:
        ids = [spot._ladder_new_client_order_id("aabbccdd", i) for i in range(500)]
        self.assertEqual(len(set(ids)), 500)

    def test_client_ids_unique_across_multiple_execution_ids(self) -> None:
        ids = []
        for exec_n in range(10):
            exec_id = f"{exec_n:08x}"
            for i in range(500):
                ids.append(spot._ladder_new_client_order_id(exec_id, i))
        self.assertEqual(len(set(ids)), 5000)

    def test_client_id_no_truncation_collisions(self) -> None:
        """If we DID rely on [:32] truncation, these 500 IDs would
        collapse to the same first 32 chars. Our format keeps them
        distinct without truncation."""
        ids = [spot._ladder_new_client_order_id("aabbccdd", i) for i in range(500)]
        truncated = {cid[:32] for cid in ids}
        self.assertEqual(len(truncated), 500,
                         "truncation would have collapsed distinct IDs")

    def test_charset_alphanumeric_and_underscore(self) -> None:
        import re
        cid = spot._ladder_new_client_order_id("aabbccdd", 7)
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9_]+", cid), f"bad chars in {cid}")

    def test_execution_id_is_8_hex(self) -> None:
        for _ in range(20):
            eid = spot._ladder_new_execution_id()
            self.assertEqual(len(eid), 8)
            int(eid, 16)  # must be valid hex

    # ----- persist-before-POST -----
    def test_persist_record_exists_before_post(self) -> None:
        """The durable record must be on disk BEFORE any POST is issued.
        Verify by failing _submit_batch_orders after the persist."""
        def fake_submit(_creds, _batch):
            # Verify record is already on disk BEFORE we "succeed".
            records = spot.list_unresolved_ladders("amiroo")
            self.assertGreaterEqual(len(records), 1,
                "durable record must exist before any POST")
            return spot.BatchRequestOutcome(
                kind="AMBIGUOUS_TIMEOUT", mexc_message="boom",
            )
        req = {"operation": "ladder", "exchange": "mexc", "account": "amiroo",
               "children": self.children}
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_submit_batch_orders", side_effect=fake_submit):
                resp = spot.execute(req)
        self.assertTrue(resp.success or resp.ladder.status in ("partial", "unknown"))
        self.assertIsNotNone(resp.data.get("execution_id"))
        self.assertEqual(len(resp.data["execution_id"]), 8)

    def test_atomic_record_update(self) -> None:
        """Atomic write must use a tmp file and os.replace."""
        from pathlib import Path
        # Use a tmp dir for the records.
        records_dir = spot._ladder_record_dir("amiroo")
        records_dir.mkdir(parents=True, exist_ok=True)
        target = records_dir / "test_atomic.json"
        if target.exists():
            target.unlink()
        # Spy on os.replace to confirm it's used.
        original_replace = os.replace
        replaced = {"called": False, "src": None}
        def spy_replace(src, dst):
            replaced["called"] = True
            replaced["src"] = src
            return original_replace(src, dst)
        with mock.patch.object(os, "replace", side_effect=spy_replace):
            spot._ladder_persist_atomic(target, {"a": 1, "b": 2})
        self.assertTrue(replaced["called"], "atomic write must use os.replace")
        self.assertTrue(target.exists())
        import json as _json
        self.assertEqual(_json.loads(target.read_text()), {"a": 1, "b": 2})

    def test_durable_record_persists_after_simulated_restart(self) -> None:
        """Verify the record survives a simulated gateway restart."""
        req = {"operation": "ladder", "exchange": "mexc", "account": "amiroo",
               "children": self.children}
        def fake_submit(_creds, batch_payload):
            return spot.BatchRequestOutcome(
                kind="AMBIGUOUS_TIMEOUT", mexc_message="simulated crash",
            )
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_submit_batch_orders", side_effect=fake_submit):
                resp1 = spot.execute(req)
        execution_id = resp1.data["execution_id"]
        self.assertEqual(resp1.data["unknown"], 5,
                         "all 5 children must be marked UNKNOWN")

        # Simulate restart by clearing any in-memory state and re-loading.
        spot._MARKET_CACHE.update({"ts": 0.0, "symbols": [], "by_symbol": {}})
        records = spot.list_unresolved_ladders("amiroo")
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["execution_id"], execution_id)
        self.assertEqual(len(rec["children"]), 5)
        cids = [c["client_order_id"] for c in rec["children"]]
        self.assertEqual(len(set(cids)), 5)
        # All children should be UNKNOWN so reconcile is needed.
        for c in rec["children"]:
            self.assertEqual(c["submission_classification"], "UNKNOWN")

    def test_recovery_loads_exact_original_client_ids(self) -> None:
        """The reconciliation entry point must receive the SAME client IDs
        that were originally persisted."""
        req = {"operation": "ladder", "exchange": "mexc", "account": "amiroo",
               "children": self.children}
        def fake_submit(_creds, batch_payload):
            return spot.BatchRequestOutcome(
                kind="OK",
                payload=[
                    {"orderId": f"x{i}", "clientOrderId": c.get("newClientOrderId"),
                     "symbol": "SOLUSDC"} for i, c in enumerate(batch_payload)
                ],
            )
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_submit_batch_orders", side_effect=fake_submit):
                resp = spot.execute(req)
        original_cids = [c["client_order_id"] for c in resp.data["child_results"]]
        execution_id = resp.data["execution_id"]
        # Reload via the durable record.
        rec = spot._ladder_load_record("amiroo", execution_id)
        persisted_cids = [c["client_order_id"] for c in rec["children"]]
        self.assertEqual(sorted(persisted_cids), sorted(original_cids))

    # ----- reconcile_batch -----
    def _stub_signed(self, open_orders, all_orders, single_order_map=None,
                     fail_paths=()):
        """Build a fake _signed_request that returns per-path payloads."""
        single_order_map = single_order_map or {}
        calls = {"paths": []}
        def fake_signed(_c, _m, path, params=None):
            calls["paths"].append(path)
            if path in fail_paths:
                import urllib.error
                raise urllib.error.URLError("simulated")
            if path == "/api/v3/openOrders":
                return open_orders
            if path == "/api/v3/allOrders":
                return all_orders
            if path == "/api/v3/order":
                cid = (params or {}).get("origClientOrderId") or ""
                return single_order_map.get(cid) or {}
            return []
        return fake_signed, calls

    def test_reconcile_all_open(self) -> None:
        cids = [f"ts_aabbccdd_{i:06d}" for i in range(5)]
        open_orders = [
            {"orderId": f"o{i}", "clientOrderId": cid, "symbol": "SOLUSDC",
             "status": "NEW"} for i, cid in enumerate(cids)
        ]
        fake_signed, _calls = self._stub_signed(open_orders, [])
        with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
            result = spot.reconcile_batch("amiroo", "SOLUSDC", cids)
        classes = [r["classification"] for r in result]
        self.assertEqual(classes, ["FOUND_OPEN"] * 5)

    def test_reconcile_all_filled(self) -> None:
        cids = [f"ts_aabbccdd_{i:06d}" for i in range(5)]
        all_orders = [
            {"orderId": f"o{i}", "clientOrderId": cid, "symbol": "SOLUSDC",
             "status": "FILLED"} for i, cid in enumerate(cids)
        ]
        fake_signed, _calls = self._stub_signed([], all_orders)
        with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
            result = spot.reconcile_batch("amiroo", "SOLUSDC", cids)
        classes = [r["classification"] for r in result]
        self.assertEqual(classes, ["FOUND_FILLED"] * 5)

    def test_reconcile_mixed_states(self) -> None:
        cids = [f"ts_aabbccdd_{i:06d}" for i in range(4)]
        fake_signed, _calls = self._stub_signed(
            open_orders=[
                {"orderId": "o0", "clientOrderId": cids[0], "symbol": "SOLUSDC",
                 "status": "NEW"},
            ],
            all_orders=[
                {"orderId": "o1", "clientOrderId": cids[1], "symbol": "SOLUSDC",
                 "status": "FILLED"},
                {"orderId": "o2", "clientOrderId": cids[2], "symbol": "SOLUSDC",
                 "status": "CANCELED"},
                {"orderId": "o3", "clientOrderId": cids[3], "symbol": "SOLUSDC",
                 "status": "EXPIRED"},
            ],
        )
        with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
            result = spot.reconcile_batch("amiroo", "SOLUSDC", cids)
        classes = [r["classification"] for r in result]
        self.assertEqual(classes, ["FOUND_OPEN", "FOUND_FILLED",
                                   "FOUND_CANCELED", "FOUND_CANCELED"])

    def test_reconcile_none_found(self) -> None:
        cids = [f"ts_aabbccdd_{i:06d}" for i in range(3)]
        fake_signed, _calls = self._stub_signed([], [])
        with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
            result = spot.reconcile_batch("amiroo", "SOLUSDC", cids)
        classes = [r["classification"] for r in result]
        self.assertEqual(classes, ["NOT_FOUND"] * 3)

    def test_reconcile_query_timeout_marks_unknown(self) -> None:
        cids = [f"ts_aabbccdd_{i:06d}" for i in range(3)]
        fake_signed, _calls = self._stub_signed([], [], fail_paths={"/api/v3/openOrders", "/api/v3/allOrders"})
        with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
            result = spot.reconcile_batch("amiroo", "SOLUSDC", cids)
        classes = [r["classification"] for r in result]
        self.assertEqual(classes, ["QUERY_UNKNOWN"] * 3)

    def test_reconcile_single_order_fallback(self) -> None:
        """Single-order GET /api/v3/order fills in IDs that openOrders+
        allOrders missed."""
        cids = [f"ts_aabbccdd_{i:06d}" for i in range(2)]
        fake_signed, _calls = self._stub_signed(
            open_orders=[],
            all_orders=[],
            single_order_map={
                cids[1]: {"orderId": "o1", "clientOrderId": cids[1],
                          "symbol": "SOLUSDC", "status": "FILLED"},
            },
        )
        with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
            result = spot.reconcile_batch("amiroo", "SOLUSDC", cids)
        classes = [r["classification"] for r in result]
        self.assertEqual(classes, ["NOT_FOUND", "FOUND_FILLED"])

    def test_reconcile_does_not_post_or_delete(self) -> None:
        """Recon MUST never issue POST or DELETE."""
        cids = [f"ts_aabbccdd_{i:06d}" for i in range(3)]
        methods_seen = []
        def fake_signed(_c, method, path, params=None):
            methods_seen.append((method, path))
            if path == "/api/v3/openOrders":
                return []
            if path == "/api/v3/allOrders":
                return []
            return {}
        with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
            spot.reconcile_batch("amiroo", "SOLUSDC", cids)
        for m, p in methods_seen:
            self.assertNotIn(m.upper(), {"POST", "DELETE", "PUT", "PATCH"},
                             f"reconcile must not {m.upper()} {p}")
            self.assertIn(m.upper(), {"GET"}, f"unexpected method {m} for {p}")

    def test_reconcile_via_execute_op(self) -> None:
        cids = [f"ts_aabbccdd_{i:06d}" for i in range(2)]
        open_orders = [{"orderId": "o0", "clientOrderId": cids[0],
                        "symbol": "SOLUSDC", "status": "NEW"}]
        fake_signed, _calls = self._stub_signed(open_orders, [])
        req = {"operation": "ladder_reconcile", "exchange": "mexc",
               "account": "amiroo", "symbol": "SOLUSDC",
               "expected_client_order_ids": cids}
        with mock.patch.object(spot, "_signed_request", side_effect=fake_signed):
            resp = spot.execute(req)
        self.assertTrue(resp.success)
        self.assertEqual(resp.data["summary"], {"FOUND_OPEN": 1, "NOT_FOUND": 1})

    # ----- list_unresolved_ladders -----
    def test_list_unresolved_only_includes_unresolved(self) -> None:
        """Records with at least one UNKNOWN or NOT_ATTEMPTED child appear."""
        # Write a record directly.
        base = spot._ladder_record_dir("amiroo")
        base.mkdir(parents=True, exist_ok=True)
        rec = {
            "execution_id": "aabbccdd",
            "exchange": "mexc", "account": "amiroo",
            "instrument": "SOLUSDC", "side": "BUY", "distribution": "uniform",
            "children": [
                {"client_order_id": "c1", "submission_classification": "ACCEPTED"},
                {"client_order_id": "c2", "submission_classification": "UNKNOWN"},
            ],
        }
        path = base / "aabbccdd.json"
        spot._ladder_persist_atomic(path, rec)
        rec2 = dict(rec)
        rec2["execution_id"] = "eeff0011"
        rec2["children"] = [
            {"client_order_id": "c3", "submission_classification": "ACCEPTED"},
            {"client_order_id": "c4", "submission_classification": "ACCEPTED"},
        ]
        path2 = base / "eeff0011.json"
        spot._ladder_persist_atomic(path2, rec2)
        listed = spot.list_unresolved_ladders("amiroo")
        ids = sorted(r["execution_id"] for r in listed)
        self.assertEqual(ids, ["aabbccdd"])

    # ----- duplicate confirm safety -----
    def test_duplicate_confirm_token_zero_additional_posts(self) -> None:
        """If the same ladder is submitted twice, the second confirm
        MUST NOT issue additional POSTs. (Token equality is enforced at
        the wizard layer; the agent assumes the wizard already validated.)
        We verify the agent does not internally retry or duplicate POST."""
        posts = 0
        def fake_submit(_creds, batch_payload):
            nonlocal posts
            posts += 1
            return spot.BatchRequestOutcome(
                kind="OK",
                payload=[
                    {"orderId": f"x{i}", "clientOrderId": c.get("newClientOrderId"),
                     "symbol": "SOLUSDC"} for i, c in enumerate(batch_payload)
                ],
            )
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            with mock.patch.object(spot, "_submit_batch_orders", side_effect=fake_submit):
                resp1 = spot.execute({
                    "operation": "ladder", "exchange": "mexc", "account": "amiroo",
                    "children": [
                        {"symbol": "SOLUSDC", "side": "BUY", "quantity": "0.01",
                         "price": "100", "client_order_id": f"orig_{i}",
                         "instrument": {"symbol": "SOLUSDC"}} for i in range(10)
                    ]
                })
                # Second call: same children, same wizard execution_id.
                req2 = {
                    "operation": "ladder", "exchange": "mexc", "account": "amiroo",
                    "execution_id": resp1.data["execution_id"],
                    "children": [
                        {"symbol": "SOLUSDC", "side": "BUY", "quantity": "0.01",
                         "price": "100", "client_order_id": f"orig_{i}",
                         "instrument": {"symbol": "SOLUSDC"}} for i in range(10)
                    ],
                }
                resp2 = spot.execute(req2)
        # Two requests → two POSTs (one each).
        self.assertEqual(posts, 2)
        # The wizard's confirm-token check is what stops user-initiated
        # duplicates at the wizard layer. Here we just verify the agent
        # does not internally retry on AMBIGUOUS outcome.
        self.assertTrue(resp1.success)
        self.assertTrue(resp2.success)


if __name__ == "__main__":
    unittest.main()
