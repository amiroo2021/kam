"""Unit tests for QFEX agent (offline mocks)."""

from __future__ import annotations

import hmac
import os
import unittest
from unittest import mock

from plugins.trade.agents import x_qfex_agent as qfex
from plugins.trade import tradedesk


class QfexAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = os.environ.copy()
        for key in list(os.environ):
            if key.upper().startswith("QFEX_"):
                del os.environ[key]

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def _creds(self) -> None:
        os.environ["QFEX_AMIROO_PUBLIC_KEY"] = "public-key"
        os.environ["QFEX_AMIROO_SECRET_KEY"] = "secret-key"

    def test_list_accounts_requires_public_and_secret_key(self) -> None:
        self._creds()
        os.environ["QFEX_HALF_PUBLIC_KEY"] = "public-only"
        self.assertEqual(qfex.list_accounts(), ["amiroo"])

    def test_tradedesk_discovers_qfex(self) -> None:
        self.assertIn("qfex", tradedesk.TradeDesk().list_exchanges())

    def test_capabilities_balance_only(self) -> None:
        self.assertIn("balance", qfex.capabilities())
        self.assertNotIn("new_order", qfex.capabilities())

    def test_auth_headers_use_nonce_timestamp_hmac_signature(self) -> None:
        self._creds()
        creds = qfex._lookup_credentials("AMIROO")
        assert creds is not None
        with mock.patch.object(qfex.secrets, "token_hex", return_value="abc123"), \
             mock.patch.object(qfex.time, "time", return_value=1700000000):
            headers = qfex._auth_headers(creds)
        expected = hmac.new(b"secret-key", b"abc123:1700000000", "sha256").hexdigest()
        self.assertEqual(headers["x-qfex-public-key"], "public-key")
        self.assertEqual(headers["x-qfex-nonce"], "abc123")
        self.assertEqual(headers["x-qfex-timestamp"], "1700000000")
        self.assertEqual(headers["x-qfex-hmac-signature"], expected)

    def test_balance_rolls_up_available_balances(self) -> None:
        self._creds()

        def fake_request(creds, method, path, query=""):
            self.assertEqual(method, "GET")
            self.assertEqual(path, "/user/subaccounts/balance")
            return {
                "accounts": [
                    {"account_id": "master", "available_balance": 100.125, "is_master": True},
                    {"account_id": "sub", "available_balance": "50.125", "is_master": False},
                ]
            }

        with mock.patch.object(qfex, "_signed_request", side_effect=fake_request):
            resp = qfex.execute({"operation": "balance", "exchange": "qfex", "account": "AMIROO"})
        self.assertTrue(resp.success, resp)
        assert resp.balance is not None
        assert resp.portfolio_summary is not None
        self.assertEqual(resp.balance.unit, "USDT")
        self.assertEqual(resp.balance.value, "150.25")
        self.assertEqual(resp.portfolio_summary.withdrawable, "150.25")

    def test_unsupported_operation_returns_canonical_failure(self) -> None:
        self._creds()
        resp = qfex.execute({"operation": "new_order", "exchange": "qfex", "account": "amiroo"})
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "NOT_IMPLEMENTED")


if __name__ == "__main__":
    unittest.main()
