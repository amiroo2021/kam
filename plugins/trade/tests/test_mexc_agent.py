"""Unit tests for MEXC agent (offline)."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from plugins.trade.agents import x_mexc_agent as mexc


class MexcAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = os.environ.copy()
        for k in list(os.environ):
            if k.upper().startswith("MEXC_"):
                del os.environ[k]

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def test_list_accounts(self) -> None:
        os.environ["MEXC_AMIROO_ACCESSKEY"] = "mx_test_key"
        os.environ["MEXC_AMIROO_SECRETKEY"] = "secret_test"
        os.environ["MEXC_HALF_SECRETKEY"] = "only_secret"
        with mock.patch.object(mexc, "_load_dotenv_values", return_value={}):
            self.assertEqual(mexc.list_accounts(), ["amiroo"])

    def test_balance_success(self) -> None:
        os.environ["MEXC_AMIROO_ACCESSKEY"] = "mx_test_key"
        os.environ["MEXC_AMIROO_SECRETKEY"] = "secret_test"

        def fake_contract(_creds, method, path, params=None):
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
                    mexc,
                    "_spot_request",
                    side_effect=RuntimeError("skip spot"),
                ):
                    resp = mexc.execute(
                        {"operation": "balance", "exchange": "mexc", "account": "AMIROO"}
                    )
        self.assertTrue(resp.success, resp)
        assert resp.balance is not None
        assert resp.portfolio_summary is not None
        self.assertEqual(resp.balance.unit, "USDC")
        # 8.08 + 31548.10
        self.assertEqual(resp.balance.value, "31556.18")
        self.assertEqual(resp.portfolio_summary.margin_used, "702.59")
        self.assertIn("USDC", (resp.data or {}).get("stable_currencies") or [])
        self.assertIn("USDT", (resp.data or {}).get("stable_currencies") or [])

    def test_not_implemented(self) -> None:
        os.environ["MEXC_AMIROO_ACCESSKEY"] = "mx_test_key"
        os.environ["MEXC_AMIROO_SECRETKEY"] = "secret_test"
        with mock.patch.object(mexc, "_load_dotenv_values", return_value={}):
            resp = mexc.execute(
                {"operation": "new_order", "exchange": "mexc", "account": "amiroo"}
            )
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "NOT_IMPLEMENTED")


if __name__ == "__main__":
    unittest.main()
