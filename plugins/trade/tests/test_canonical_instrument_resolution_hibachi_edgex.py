"""Canonical instrument resolution: Hibachi + EdgeX (offline)."""

from __future__ import annotations

import os
import unittest
from decimal import Decimal
from typing import Any, Dict, List
from unittest import mock

from plugins.trade.agents import x_edgex_agent as edgex
from plugins.trade.agents import x_hibachi_agent as hibachi
from plugins.trade.tests.test_phase7_hibachi import SAMPLE_EXCHANGE_INFO, _set_account


def _dup_btc_payload() -> Dict[str, Any]:
    payload = {
        "futureContracts": [
            dict(SAMPLE_EXCHANGE_INFO["futureContracts"][0]),
            {
                "id": 22,
                "symbol": "BTC/USDC-P",
                "displayName": "BTC/USDC Perps",
                "underlyingSymbol": "BTC",
                "settlementSymbol": "USDC",
                "tickSize": "0.1",
                "stepSize": "0.001",
                "minOrderSize": "0.001",
                "minNotional": "1",
                "underlyingDecimals": 8,
                "settlementDecimals": 6,
                "live": True,
            },
        ]
    }
    return payload


class TestHibachiCanonicalResolution(unittest.TestCase):
    def setUp(self) -> None:
        for key in list(os.environ):
            if key.startswith("HIBACHI_"):
                os.environ.pop(key, None)
        hibachi._MarketCache.invalidate()
        _set_account("MAIN")

    def tearDown(self) -> None:
        for key in list(os.environ):
            if key.startswith("HIBACHI_"):
                os.environ.pop(key, None)
        hibachi._MarketCache.invalidate()

    def test_unique_aliases_resolve_same_market(self) -> None:
        with mock.patch.object(hibachi, "_request_json", return_value=SAMPLE_EXCHANGE_INFO):
            rows = []
            for raw in ("BTC", "BTC/USDT-P", "BTC-USDT", "BTCUSDT"):
                descriptor = hibachi._resolve_canonical_instrument(raw)
                self.assertIsNotNone(descriptor)
                rows.append((descriptor["id"], descriptor["symbol"], descriptor["underlying_symbol"]))
        self.assertTrue(all(row == (2, "BTC/USDT-P", "BTC") for row in rows))

    def test_wbtc_xbt_follow_existing_alias_table(self) -> None:
        self.assertEqual(hibachi._canonical_symbol_from_request("WBTC"), "BTC")
        self.assertEqual(hibachi._canonical_symbol_from_request("XBT"), "BTC")
        with mock.patch.object(hibachi, "_request_json", return_value=SAMPLE_EXCHANGE_INFO):
            for raw in ("WBTC", "XBT"):
                descriptor = hibachi._resolve_canonical_instrument(raw)
                self.assertEqual(descriptor["symbol"], "BTC/USDT-P")

    def test_duplicate_underlying_makes_bare_alias_ambiguous(self) -> None:
        with mock.patch.object(hibachi, "_request_json", return_value=_dup_btc_payload()):
            with self.assertRaises(ValueError) as ctx:
                hibachi._resolve_canonical_instrument("BTC")
        self.assertEqual(str(ctx.exception), "INSTRUMENT_AMBIGUOUS")
        with mock.patch.object(hibachi, "_request_json", return_value=_dup_btc_payload()):
            resp = hibachi.execute({
                "operation": "resolve_instrument",
                "account": "main",
                "symbol": "BTC",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "INSTRUMENT_AMBIGUOUS")

    def test_full_contract_symbol_selects_intended_market_when_underlying_is_ambiguous(self) -> None:
        with mock.patch.object(hibachi, "_request_json", return_value=_dup_btc_payload()):
            usdt = hibachi._resolve_canonical_instrument("BTC/USDT-P")
            usdc = hibachi._resolve_canonical_instrument("BTC/USDC-P")
        self.assertEqual(usdt["id"], 2)
        self.assertEqual(usdc["id"], 22)

    def test_unknown_cancel_is_instrument_not_found(self) -> None:
        with mock.patch.object(hibachi, "_request_json", return_value=SAMPLE_EXCHANGE_INFO), \
             mock.patch.object(hibachi, "_fetch_open_orders") as fetch:
            resp = hibachi.execute({
                "operation": "cancel_order_group",
                "account": "main",
                "symbol": "NOTAMARKET",
                "side": "sell",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "INSTRUMENT_NOT_FOUND")
        fetch.assert_not_called()

    def test_tp_fetch_failure_is_not_flat(self) -> None:
        with mock.patch.object(hibachi, "_lookup_credentials", return_value={"account": "main", "account_id": "1", "api_key": "k", "private_key": "p"}), \
             mock.patch.object(hibachi, "_set_current_credentials", return_value=None), \
             mock.patch.object(hibachi, "_fetch_account_info", side_effect=RuntimeError("boom")):
            resp = hibachi.execute({
                "operation": "set_tp",
                "account": "main",
                "symbol": "BTC",
                "price": "70000",
            })
        self.assertFalse(resp.success)
        self.assertNotEqual(resp.error.code, "ALREADY_FLAT")
        self.assertNotIn(resp.error.code, {"", None})
        self.assertFalse(resp.success)


class TestEdgeXCanonicalResolution(unittest.TestCase):
    def setUp(self) -> None:
        self._old = {k: v for k, v in os.environ.items() if k.startswith("EDGEX_")}
        for key in list(os.environ):
            if key.startswith("EDGEX_"):
                os.environ.pop(key)
        os.environ["EDGEX_MAIN_ACCOUNTID"] = "123"
        os.environ["EDGEX_MAIN_APIKEY"] = "key"
        os.environ["EDGEX_MAIN_APISECRET"] = "secret"
        os.environ["EDGEX_MAIN_APIPASSPHRASE"] = "pass"
        os.environ["EDGEX_MAIN_SIGNERKEY"] = "signer"

    def tearDown(self) -> None:
        for key in list(os.environ):
            if key.startswith("EDGEX_"):
                os.environ.pop(key)
        os.environ.update(self._old)

    def test_sol_aliases_share_contract_id(self) -> None:
        catalog = {"30000003": "SOLUSDC", "30000001": "BTCUSDC"}
        with mock.patch.object(edgex, "_metadata", return_value=catalog):
            ids = []
            natives = []
            for raw in ("SOL", "SOL-USDC", "SOLUSDC"):
                resolved = edgex._resolve_contract(raw)
                self.assertIsNotNone(resolved)
                ids.append(resolved[0])
                natives.append(resolved[1])
                resp = edgex.execute({"operation": "resolve_instrument", "account": "main", "symbol": raw})
                self.assertTrue(resp.success)
                self.assertEqual(resp.instrument.symbol, "SOLUSDC")
        self.assertEqual(set(ids), {"30000003"})
        self.assertEqual(set(natives), {"SOLUSDC"})

    def test_btc_aliases_share_contract_id(self) -> None:
        catalog = {"30000003": "SOLUSDC", "30000001": "BTCUSDC"}
        with mock.patch.object(edgex, "_metadata", return_value=catalog):
            a = edgex._resolve_contract("BTC")
            b = edgex._resolve_contract("BTCUSDC")
        self.assertEqual(a, b)
        self.assertEqual(a[0], "30000001")

    def test_unknown_is_instrument_not_found(self) -> None:
        with mock.patch.object(edgex, "_metadata", return_value={"30000001": "BTCUSDC"}):
            self.assertIsNone(edgex._resolve_contract("NOTAMARKET"))
            resp = edgex.execute({"operation": "resolve_instrument", "account": "main", "symbol": "NOTAMARKET"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "INSTRUMENT_NOT_FOUND")

    def test_metadata_failure_is_not_unknown_symbol(self) -> None:
        with mock.patch.object(edgex, "_metadata", side_effect=RuntimeError("timeout")):
            with self.assertRaises(Exception):
                edgex._resolve_contract("SOL")
            resp = edgex.execute({"operation": "resolve_instrument", "account": "main", "symbol": "SOL"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "METADATA_UNAVAILABLE")
        self.assertNotEqual(resp.error.code, "INSTRUMENT_NOT_FOUND")

    def test_duplicate_native_is_ambiguous(self) -> None:
        catalog = {"1": "SOLUSDC", "2": "SOLUSDC"}
        with mock.patch.object(edgex, "_metadata", return_value=catalog):
            with self.assertRaises(ValueError) as ctx:
                edgex._resolve_contract("SOLUSDC")
            self.assertEqual(str(ctx.exception), "INSTRUMENT_AMBIGUOUS")
            resp = edgex.execute({"operation": "resolve_instrument", "account": "main", "symbol": "SOLUSDC"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "INSTRUMENT_AMBIGUOUS")

    def test_close_binds_resolved_contract_id(self) -> None:
        captured: List[str] = []

        def fake_market(creds, cid, amount, side):
            captured.append(cid)
            return {"code": "SUCCESS", "data": {"orderId": "9"}}

        asset = {"positionList": [{"contractId": "30000003", "openSize": "2"}]}
        with mock.patch.object(edgex, "_metadata", return_value={"30000003": "SOLUSDC"}), \
             mock.patch.object(edgex, "_request", return_value=asset), \
             mock.patch.object(edgex, "_create_market_order", side_effect=fake_market):
            resp = edgex.execute({"operation": "close_position", "account": "main", "symbol": "SOL"})
        self.assertTrue(resp.success)
        self.assertEqual(captured, ["30000003"])

    def test_close_no_position_is_error_not_already_flat(self) -> None:
        with mock.patch.object(edgex, "_metadata", return_value={"30000003": "SOLUSDC"}), \
             mock.patch.object(edgex, "_request", return_value={"positionList": []}):
            resp = edgex.execute({"operation": "close_position", "account": "main", "symbol": "SOL"})
        self.assertFalse(resp.success)
        self.assertNotEqual(resp.error.code, "ALREADY_FLAT")
        self.assertIn(resp.error.code, {"POSITION_ACTION_FAILED", "POSITION_NOT_FOUND"})

    def test_cancel_unresolved_is_instrument_not_found(self) -> None:
        with mock.patch.object(edgex, "_metadata", return_value={"30000003": "SOLUSDC"}):
            resp = edgex.execute({
                "operation": "cancel_order_group",
                "account": "main",
                "symbol": "NOTAMARKET",
                "side": "sell",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "INSTRUMENT_NOT_FOUND")
        self.assertNotEqual(resp.error.code, "INVALID_REQUEST")


if __name__ == "__main__":
    unittest.main()
