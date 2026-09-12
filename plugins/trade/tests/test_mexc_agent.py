"""Unit tests for MEXC agent (offline)."""

from __future__ import annotations

import os
import unittest
from decimal import Decimal
from unittest import mock

from plugins.trade.agents import x_mexc_agent as mexc


class MexcAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = os.environ.copy()
        for k in list(os.environ):
            if k.upper().startswith("MEXC_"):
                del os.environ[k]
        mexc._CONTRACT_CACHE.update({"ts": 0.0, "by_symbol": {}, "by_base": {}})

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def _creds(self) -> None:
        os.environ["MEXC_AMIROO_ACCESSKEY"] = "mx_test_key"
        os.environ["MEXC_AMIROO_SECRETKEY"] = "secret_test"

    def test_list_accounts(self) -> None:
        self._creds()
        os.environ["MEXC_HALF_SECRETKEY"] = "only_secret"
        with mock.patch.object(mexc, "_load_dotenv_values", return_value={}):
            self.assertEqual(mexc.list_accounts(), ["amiroo"])

    def test_capabilities(self) -> None:
        for op in (
            "balance",
            "positions_orders",
            "new_order",
            "cancel_order_group",
            "set_tp",
            "set_sl",
            "close_position",
        ):
            self.assertIn(op, mexc.capabilities())

    def test_balance_rollup_usdc_usdt(self) -> None:
        self._creds()

        def fake_contract(_creds, method, path, params=None, body=None):
            if path.endswith("/assets"):
                return {
                    "success": True,
                    "code": 0,
                    "data": [
                        {
                            "currency": "USDT",
                            "equity": "8.08",
                            "availableBalance": "8.08",
                            "frozenBalance": "0",
                            "positionMargin": "0",
                            "unrealized": "0",
                            "bonus": "0",
                        },
                        {
                            "currency": "USDC",
                            "equity": "31548.10",
                            "availableBalance": "29830.72",
                            "frozenBalance": "1087.42",
                            "positionMargin": "702.59",
                            "unrealized": "-72.63",
                            "bonus": "0",
                        },
                    ],
                }
            return {"success": True, "code": 0, "data": []}

        with mock.patch.object(mexc, "_load_dotenv_values", return_value={}):
            with mock.patch.object(mexc, "_contract_request", side_effect=fake_contract):
                with mock.patch.object(
                    mexc, "_spot_request", side_effect=RuntimeError("skip spot")
                ):
                    resp = mexc.execute(
                        {"operation": "balance", "exchange": "mexc", "account": "AMIROO"}
                    )
        self.assertTrue(resp.success, resp)
        assert resp.balance is not None
        self.assertEqual(resp.balance.unit, "USDC")
        self.assertEqual(resp.balance.value, "31556.18")

    def test_positions_orders(self) -> None:
        self._creds()
        meta = {
            "symbol": "BTC_USDC",
            "base": "BTC",
            "quote": "USDC",
            "contract_size": Decimal("0.0001"),
            "price_unit": Decimal("0.1"),
            "vol_unit": Decimal("1"),
            "min_vol": Decimal("1"),
            "display": "BTC/USDC",
            "max_leverage": 200,
        }

        def fake_contract(_creds, method, path, params=None, body=None):
            if "open_positions" in path:
                return {
                    "success": True,
                    "code": 0,
                    "data": [
                        {
                            "symbol": "BTC_USDC",
                            "positionType": 1,
                            "holdVol": 9000,
                            "holdAvgPrice": 77292.8,
                            "unRealizedPnl": -10.5,
                        }
                    ],
                }
            if "open_orders" in path:
                return {
                    "success": True,
                    "code": 0,
                    "data": [
                        {
                            "orderId": "1",
                            "symbol": "BTC_USDC",
                            "side": 1,
                            "price": 75000,
                            "vol": 1000,
                            "dealVol": 0,
                        }
                    ],
                }
            return {"success": True, "code": 0, "data": []}

        with mock.patch.object(mexc, "_load_dotenv_values", return_value={}):
            with mock.patch.object(mexc, "_ensure_contracts", return_value=({"BTC_USDC": meta, "BTC": meta}, {"BTC": meta})):
                with mock.patch.object(mexc, "_contract_request", side_effect=fake_contract):
                    resp = mexc.execute(
                        {"operation": "positions_orders", "exchange": "mexc", "account": "amiroo"}
                    )
        self.assertTrue(resp.success, resp)
        self.assertEqual(len(resp.positions or []), 1)
        assert resp.positions is not None
        self.assertEqual(resp.positions[0].symbol, "BTC")
        self.assertEqual(resp.positions[0].side, "long")
        self.assertEqual(resp.positions[0].size, "0.9")
        self.assertEqual(resp.open_order_count, 1)

    def test_cancel_sends_order_id_array(self) -> None:
        self._creds()
        meta = {
            "symbol": "BTC_USDC",
            "base": "BTC",
            "quote": "USDC",
            "contract_size": Decimal("0.0001"),
            "price_unit": Decimal("0.1"),
            "vol_unit": Decimal("1"),
            "min_vol": Decimal("1"),
            "display": "BTC/USDC",
            "max_leverage": 200,
        }
        calls = []

        def fake_contract(_creds, method, path, params=None, body=None):
            calls.append((method, path, body))
            if "open_orders" in path:
                if len([c for c in calls if "open_orders" in c[1]]) == 1:
                    return {
                        "success": True,
                        "code": 0,
                        "data": [
                            {
                                "orderId": "111",
                                "symbol": "BTC_USDC",
                                "side": 1,
                                "price": 1,
                                "vol": 1,
                                "dealVol": 0,
                            }
                        ],
                    }
                return {"success": True, "code": 0, "data": []}
            if path.endswith("/cancel"):
                return {
                    "success": True,
                    "code": 0,
                    "data": [{"orderId": 111, "errorCode": 0, "errorMsg": "success"}],
                }
            return {"success": True, "code": 0, "data": []}

        with mock.patch.object(mexc, "_load_dotenv_values", return_value={}):
            with mock.patch.object(mexc, "_ensure_contracts", return_value=({"BTC_USDC": meta, "BTC": meta}, {"BTC": meta})):
                with mock.patch.object(mexc, "_contract_request", side_effect=fake_contract):
                    with mock.patch.object(mexc.time, "sleep", return_value=None):
                        resp = mexc.execute(
                            {
                                "operation": "cancel_order_group",
                                "exchange": "mexc",
                                "account": "amiroo",
                                "symbol": "BTC",
                                "side": "buy",
                            }
                        )
        self.assertTrue(resp.success, resp)
        cancel_calls = [c for c in calls if c[1].endswith("/cancel")]
        self.assertEqual(cancel_calls[0][2], ["111"])

    def test_not_implemented(self) -> None:
        self._creds()
        with mock.patch.object(mexc, "_load_dotenv_values", return_value={}):
            resp = mexc.execute(
                {"operation": "ladder", "exchange": "mexc", "account": "amiroo"}
            )
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "NOT_IMPLEMENTED")


if __name__ == "__main__":
    unittest.main()
