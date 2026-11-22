from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.agents import x_aster_agent as aster  # noqa: E402
from plugins.trade.tradedesk import _exchange_name_from_filename  # noqa: E402


class AsterAgentTests(unittest.TestCase):
    def test_trade_filename_parser_accepts_aster_agent(self) -> None:
        self.assertEqual(_exchange_name_from_filename("x_aster_agent.py"), "aster")

    def test_discovers_account_from_apiprivate_and_wallet(self) -> None:
        env = {
            "ASTER_AMIROO_APIPRIVATE": "0xpriv",
            "ASTER_AMIROO_WALLET": "0xwallet",
            "ASTER_BITGET_USER": "ignored_without_private",
            "ASTER_BITGET_PRIVATE_KEY": "0xpriv2",
        }
        with mock.patch.object(aster, "_combined_aster_env", return_value={k: (k, v, "env") for k, v in env.items()}):
            self.assertEqual(aster.list_accounts(), ["amiroo"])

    def test_capabilities_are_balance_only(self) -> None:
        self.assertEqual(aster.capabilities(), ["balance"])

    def test_balance_maps_assets_and_stable_total(self) -> None:
        creds = {
            "account": "amiroo",
            "api_private": "redacted-private",
            "wallet": "0xwallet",
            "signer": "0xsigner",
            "api_base": "https://sapi.asterdex.com",
        }
        payload = {
            "feeTier": 0,
            "canTrade": True,
            "updateTime": 123,
            "balances": [
                {"asset": "USDT", "free": "12.345", "locked": "0.005"},
                {"asset": "USDC", "free": "1", "locked": "2"},
                {"asset": "SOL", "free": "4", "locked": "0.5"},
            ],
        }
        with mock.patch.object(aster, "_lookup_credentials", return_value=creds), mock.patch.object(
            aster, "_signed_request", return_value=payload
        ) as signed:
            response = aster.execute({"operation": "balance", "exchange": "aster", "account": "amiroo"})

        self.assertTrue(response.success)
        self.assertIsNotNone(response.balance)
        balance = response.balance
        assert balance is not None
        self.assertEqual(balance.value, "12.35")
        self.assertEqual(balance.unit, "USDT")
        self.assertIsNotNone(response.portfolio_summary)
        data = response.data
        assert data is not None
        self.assertEqual(data["assets"][0], {"asset": "USDT", "free": "12.345", "locked": "0.005", "total": "12.35"})
        signed.assert_called_once_with(creds, "GET", "/api/v3/account")

    def test_balance_error_is_redacted(self) -> None:
        creds = {
            "account": "amiroo",
            "api_private": "very-secret-private",
            "wallet": "0xwallet",
            "signer": "0xsigner",
            "api_base": "https://sapi.asterdex.com",
        }
        with mock.patch.object(aster, "_lookup_credentials", return_value=creds), mock.patch.object(
            aster, "_signed_request", side_effect=RuntimeError("bad very-secret-private for 0xwallet")
        ):
            response = aster.execute({"operation": "balance", "exchange": "aster", "account": "amiroo"})

        self.assertFalse(response.success)
        self.assertIsNotNone(response.error)
        error = response.error
        assert error is not None
        self.assertNotIn("very-secret-private", error.message)
        self.assertNotIn("0xwallet", error.message)
        self.assertIn("***", error.message)


if __name__ == "__main__":
    unittest.main()
