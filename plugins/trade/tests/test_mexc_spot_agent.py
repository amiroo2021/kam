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

    def test_capabilities_include_limit_new_order_only(self) -> None:
        caps = set(spot.capabilities())
        self.assertIn("balance", caps)
        self.assertIn("orders", caps)
        self.assertIn("open_orders", caps)
        self.assertIn("list_instruments", caps)
        self.assertIn("new_order", caps)
        self.assertIn("cancel_orders", caps)
        self.assertNotIn("ladder", caps)

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

    def test_ladder_and_cancel_all_remain_unimplemented(self) -> None:
        with mock.patch.object(spot, "_load_dotenv_values", return_value={}):
            for op in ("ladder", "cancel_order_group"):
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
        self.assertEqual(inst.get("min_notional") or "", "")
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
    def setUp(self) -> None:
        spot._MARKET_CACHE.update({"ts": 0.0, "symbols": [], "by_symbol": {}})

    def test_ladder_capability_is_not_advertised(self) -> None:
        self.assertNotIn("ladder", spot.capabilities())

    def test_ladder_write_is_not_implemented(self) -> None:
        resp = spot.execute({
            "operation": "ladder",
            "exchange": "mexc",
            "account": "amiroo",
        })
        self.assertFalse(resp.success)
        self.assertIsNotNone(resp.error)
        self.assertEqual(resp.error.code, "NOT_IMPLEMENTED")
        self.assertIn("not enabled", (resp.error.message or "").lower())


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
        self.assertNotIn("ladder", agent.capabilities())


if __name__ == "__main__":
    unittest.main()
