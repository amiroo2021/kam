from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.agents import x_weex_agent as weex  # noqa: E402
from plugins.trade.tradedesk import _exchange_name_from_filename  # noqa: E402


class WeexAgentTests(unittest.TestCase):
    def test_trade_filename_parser_accepts_weex_agent(self) -> None:
        self.assertEqual(_exchange_name_from_filename("x_weex_agent.py"), "weex")

    def test_discovers_account_from_key_secret_and_passphrase(self) -> None:
        env = {
            "WEEX_AMIROO_API_KEY": "key-one",
            "WEEX_AMIROO_API_SECRET": "secret-one",
            "WEEX_AMIROO_PASSPHRASE": "pass-one",
            "WEEX_INCOMPLETE_API_KEY": "key-two",
        }
        with mock.patch.object(
            weex, "_combined_weex_env", return_value={k: (k, v, "env") for k, v in env.items()}
        ):
            self.assertEqual(weex.list_accounts(), ["amiroo"])

    def test_incomplete_credentials_are_not_discovered(self) -> None:
        env = {
            "WEEX_AMIROO_API_KEY": "key-one",
            "WEEX_AMIROO_API_SECRET": "secret-one",
        }
        with mock.patch.object(
            weex, "_combined_weex_env", return_value={k: (k, v, "env") for k, v in env.items()}
        ):
            self.assertEqual(weex.list_accounts(), [])

    def test_capabilities_are_balance_only(self) -> None:
        self.assertEqual(weex.capabilities(), ["balance"])

    def test_balance_maps_assets_and_stable_total(self) -> None:
        creds = {
            "account": "amiroo",
            "api_key": "redacted-key",
            "api_secret": "redacted-secret",
            "passphrase": "redacted-passphrase",
            "base_url": "https://api.weex.com",
        }
        payload = {
            "code": 0,
            "data": {
                "balances": [
                    {"asset": "USDT", "free": "12.345", "locked": "0.005"},
                    {"asset": "USDC", "free": "1", "locked": "2"},
                ]
            },
        }
        with mock.patch.object(weex, "_lookup_credentials", return_value=creds), mock.patch.object(
            weex, "_signed_request", return_value=payload
        ):
            response = weex.execute({"operation": "balance", "exchange": "weex", "account": "amiroo"})

        self.assertTrue(response.success)
        self.assertIsNotNone(response.balance)
        balance = response.balance
        assert balance is not None
        self.assertEqual(balance.unit, "USDT")
        self.assertEqual(balance.value, "15.35")
        data = response.data
        assert data is not None
        self.assertEqual(len(data["assets"]), 2)
        self.assertEqual(data["assets"][0]["asset"], "USDT")

    def test_missing_account_is_reported(self) -> None:
        with mock.patch.object(weex, "_lookup_credentials", return_value=None):
            response = weex.execute({"operation": "balance", "exchange": "weex", "account": "nobody"})

        self.assertFalse(response.success)
        self.assertIsNotNone(response.error)
        error = response.error
        assert error is not None
        self.assertEqual(error.code, "ACCOUNT_NOT_FOUND")

    def test_balance_error_is_redacted(self) -> None:
        creds = {
            "account": "amiroo",
            "api_key": "redacted-key",
            "api_secret": "very-secret-secret",
            "passphrase": "redacted-passphrase",
            "base_url": "https://api.weex.com",
        }
        with mock.patch.object(weex, "_lookup_credentials", return_value=creds), mock.patch.object(
            weex, "_signed_request", side_effect=RuntimeError("bad very-secret-secret for redacted-key")
        ):
            response = weex.execute({"operation": "balance", "exchange": "weex", "account": "amiroo"})

        self.assertFalse(response.success)
        self.assertIsNotNone(response.error)
        error = response.error
        assert error is not None
        self.assertNotIn("very-secret-secret", error.message)
        self.assertIn("***", error.message)


if __name__ == "__main__":
    unittest.main()
