from __future__ import annotations

import hashlib
import hmac
import sys
import traceback
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.agents import x_azverse_agent_spot as spot  # noqa: E402
from plugins.trade.spotdesk import SpotDesk, _exchange_name_from_filename  # noqa: E402


class AzverseSpotAgentTests(unittest.TestCase):
    def test_spot_filename_is_isolated_from_perpetual_agent(self) -> None:
        self.assertEqual(_exchange_name_from_filename("x_azverse_agent_spot.py"), "azverse")
        self.assertIsNone(_exchange_name_from_filename("x_azverse_agent.py"))

    def test_discovers_api_key_and_secret_key_aliases(self) -> None:
        combined = {
            "AZVERSE_MAIN_API_KEY": ("AZVERSE_MAIN_API_KEY", "api-key", "env"),
            "AZVERSE_MAIN_SECRET_KEY": ("AZVERSE_MAIN_SECRET_KEY", "api-secret", "env"),
            "AZVERSE_HALF_KEY": ("AZVERSE_HALF_KEY", "key-only", "env"),
        }
        with mock.patch.object(spot.futures, "_combined_azverse_env", return_value=combined):
            self.assertEqual(spot.list_accounts(), ["main"])

    def test_capabilities_are_strictly_spot_balance_only(self) -> None:
        self.assertEqual(spot.capabilities(), ["balance"])
        for forbidden in ("orders", "new_order", "cancel_orders", "ladder", "positions_orders"):
            self.assertNotIn(forbidden, spot.capabilities())

    def test_spot_signature_matches_official_algorithm(self) -> None:
        credentials = {"api_key": "api-key", "secret": "api-secret"}
        headers, signature = spot._signed_headers(credentials, spot.BALANCES_PATH, "1000")
        signed_values = {
            "validate-appkey": "api-key",
            "validate-timestamp": "1000",
            "validate-recvwindow": "5000",
            "validate-algorithms": "HmacSHA256",
        }
        header_part = "&".join(f"{key}={signed_values[key]}" for key in sorted(signed_values))
        expected = hmac.new(b"api-secret", f"{header_part}#GET#{spot.BALANCES_PATH}".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(signature, expected)
        self.assertEqual(headers["validate-signature"], expected)

    def test_live_spot_balance_shape_is_normalized(self) -> None:
        payload = {
            "assets": [
                {
                    "currency": "usdt",
                    "availableAmount": "0.00000030",
                    "frozenAmount": "0.00000000",
                    "totalAmount": "0.00000030",
                },
                {
                    "currency": "btc",
                    "availableAmount": "0",
                    "frozenAmount": "0",
                    "totalAmount": "0",
                },
            ],
            "totalUsdtAmount": "0.0000",
        }
        credentials = {"account": "main", "api_key": "key", "secret": "secret", "spot_base": spot.DEFAULT_SPOT_BASE}
        with mock.patch.object(spot, "_lookup_credentials", return_value=credentials), mock.patch.object(
            spot, "_signed_get", return_value=payload
        ) as signed:
            response = spot.execute({"operation": "balance", "exchange": "azverse", "account": "main"})
        self.assertTrue(response.success, response)
        self.assertEqual(response.balance.value, "0.00")
        self.assertEqual(response.balance.unit, "USDT")
        self.assertEqual(response.data["assets"], [{"asset": "USDT", "free": "0.0000003", "locked": "0", "total": "0.0000003"}])
        signed.assert_called_once_with(credentials, spot.BALANCES_PATH)

    def test_malformed_spot_numeric_fields_fail_closed(self) -> None:
        credentials = {"account": "main", "api_key": "key", "secret": "secret", "spot_base": spot.DEFAULT_SPOT_BASE}
        bad_rows = (
            {"currency": "usdt", "availableAmount": "1", "frozenAmount": "0"},
            {"currency": "usdt", "availableAmount": "bad", "frozenAmount": "0", "totalAmount": "1"},
            {"currency": "usdt", "availableAmount": "1", "frozenAmount": "bad", "totalAmount": "1"},
        )
        for row in bad_rows:
            with self.subTest(row=row), mock.patch.object(spot, "_lookup_credentials", return_value=credentials), mock.patch.object(
                spot, "_signed_get", return_value={"assets": [row]}
            ):
                response = spot.execute({"operation": "balance", "exchange": "azverse", "account": "main"})
            self.assertFalse(response.success)
            self.assertEqual(response.error.code, "AZVERSE_SPOT_BALANCE_ERROR")

    def test_redirects_are_refused(self) -> None:
        handler = spot._NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://other.example/path"))

    def test_unexpected_exception_redacts_signature_and_credentials(self) -> None:
        credentials = {"account": "main", "api_key": "api-key", "secret": "api-secret", "spot_base": spot.DEFAULT_SPOT_BASE}
        _headers, signature = spot._signed_headers(credentials, spot.BALANCES_PATH, "1000")
        with mock.patch.object(spot.time, "time", return_value=1.0), mock.patch.object(
            spot, "_open_no_redirect", side_effect=RuntimeError(f"api-key api-secret {signature}")
        ):
            with self.assertRaises(RuntimeError) as caught:
                spot._signed_get(credentials, spot.BALANCES_PATH)
        rendered = str(caught.exception)
        self.assertNotIn("api-key", rendered)
        self.assertNotIn("api-secret", rendered)
        self.assertNotIn(signature, rendered)

    def test_mutations_are_rejected_without_http(self) -> None:
        with mock.patch.object(spot, "_signed_get") as signed:
            response = spot.execute({"operation": "new_order", "exchange": "azverse", "account": "main"})
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "NOT_IMPLEMENTED")
        signed.assert_not_called()

    def test_spotdesk_discovers_azverse_spot_agent(self) -> None:
        desk = SpotDesk()
        self.assertIn("azverse", desk.list_exchanges())
        self.assertEqual(desk.capabilities("azverse"), ["balance"])


    class _ExplodingErrorBody:
        def __init__(self, message: str) -> None:
            self.message = message

        def read(self, *_args, **_kwargs):
            raise RuntimeError(self.message)

        def close(self) -> None:
            return None

    def test_http_error_body_read_failure_redacts_signature_and_credentials(self) -> None:
        credentials = {"account": "main", "api_key": "api-key", "secret": "api-secret", "spot_base": spot.DEFAULT_SPOT_BASE}
        _headers, signature = spot._signed_headers(credentials, spot.BALANCES_PATH, "1000")
        error = urllib.error.HTTPError(
            f"{spot.DEFAULT_SPOT_BASE}{spot.BALANCES_PATH}",
            500,
            "Server Error",
            {},
            self._ExplodingErrorBody(f"api-key api-secret {signature}"),
        )
        with mock.patch.object(spot.time, "time", return_value=1.0), mock.patch.object(
            spot, "_open_no_redirect", side_effect=error
        ):
            with self.assertRaises(RuntimeError) as caught:
                spot._signed_get(credentials, spot.BALANCES_PATH)
        rendered = str(caught.exception)
        self.assertNotIn("api-key", rendered)
        self.assertNotIn("api-secret", rendered)
        self.assertNotIn(signature, rendered)


    def test_redacted_transport_exception_drops_secret_bearing_cause_and_traceback(self) -> None:
        credentials = {"account": "main", "api_key": "api-key", "secret": "api-secret", "spot_base": spot.DEFAULT_SPOT_BASE}
        _headers, signature = spot._signed_headers(credentials, spot.BALANCES_PATH, "1000")
        with mock.patch.object(spot.time, "time", return_value=1.0), mock.patch.object(
            spot, "_open_no_redirect", side_effect=RuntimeError(f"api-key api-secret {signature}")
        ):
            with self.assertRaises(RuntimeError) as caught:
                spot._signed_get(credentials, spot.BALANCES_PATH)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("api-key", rendered)
        self.assertNotIn("api-secret", rendered)
        self.assertNotIn(signature, rendered)


if __name__ == "__main__":
    unittest.main()
