"""Offline contract tests for MEXC Spot request signing.

MEXC signs the canonical "totalParams" string with HMAC-SHA256. The signed
string and the transmitted payload must be byte-identical, otherwise the
exchange rejects the request. These tests pin the two transports:

* single-order POST  -> signed string in the form-urlencoded request body
* batchOrders POST   -> signed string in the query string

They use no network and no credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.agents import x_mexc_agent_spot as spot  # noqa: E402

_CREDENTIALS = {
    "account": "amiroo",
    "access_key": "test-access-key",
    "secret_key": "test-secret-key",
    "spot_base": "https://api.mexc.com",
}


def _expected_signature(total_params: str) -> str:
    return hmac.new(
        b"test-secret-key", total_params.encode("utf-8"), hashlib.sha256
    ).hexdigest()


class MexcSpotSigningTests(unittest.TestCase):
    def _capture(self, method: str, path: str, params=None):
        """Call _signed_request and capture the url/body/headers actually sent."""
        captured: dict = {}

        def fake_json_request(method_u, url, headers, request_body=None):
            captured["method"] = method_u
            captured["url"] = url
            captured["headers"] = dict(headers)
            captured["body"] = request_body
            return {"ok": True}

        with mock.patch.object(spot, "_json_request", side_effect=fake_json_request):
            spot._signed_request(_CREDENTIALS, method, path, params)
        return captured

    def test_single_order_post_signs_body_not_query(self) -> None:
        captured = self._capture("POST", "/api/v3/order", {"symbol": "SOLUSDC", "side": "BUY"})

        # The signature must NOT ride in the query string for a single POST.
        self.assertNotIn("signature=", captured["url"])
        self.assertEqual(captured["url"], "https://api.mexc.com/api/v3/order")
        self.assertEqual(
            captured["headers"]["Content-Type"], "application/x-www-form-urlencoded"
        )

        body = captured["body"]
        self.assertIsNotNone(body)
        signed_params = body.rsplit("&signature=", 1)
        total_params = signed_params[0]
        signature = signed_params[1]

        # Signature must cover exactly the transmitted totalParams.
        self.assertEqual(signature, _expected_signature(total_params))

        # The signed body must carry the request parameters.
        parsed = dict(urllib.parse.parse_qsl(total_params))
        self.assertEqual(parsed["symbol"], "SOLUSDC")
        self.assertEqual(parsed["side"], "BUY")
        self.assertIn("timestamp", parsed)
        self.assertEqual(parsed["recvWindow"], "5000")

    def test_batch_orders_post_signs_query_string(self) -> None:
        captured = self._capture(
            "POST", "/api/v3/batchOrders", {"batchOrders": '[{"symbol":"SOLUSDC"}]'}
        )

        # batchOrders must carry the signed string in the query.
        self.assertIn("signature=", captured["url"])
        self.assertIsNone(captured["body"])
        self.assertNotIn("Content-Type", captured["headers"])

        query = captured["url"].split("?", 1)[1]
        total_params, signature = query.rsplit("&signature=", 1)
        self.assertEqual(signature, _expected_signature(total_params))

        parsed = dict(urllib.parse.parse_qsl(total_params))
        self.assertEqual(parsed["batchOrders"], '[{"symbol":"SOLUSDC"}]')

    def test_get_request_signs_query_string(self) -> None:
        captured = self._capture("GET", "/api/v3/openOrders", {"symbol": "SOLUSDC"})

        self.assertIn("signature=", captured["url"])
        self.assertIsNone(captured["body"])
        query = captured["url"].split("?", 1)[1]
        total_params, signature = query.rsplit("&signature=", 1)
        self.assertEqual(signature, _expected_signature(total_params))

    def test_signed_string_uses_uppercase_percent_encoding(self) -> None:
        # MEXC signs the uppercase-percent-encoded form; quote_via=quote keeps
        # the canonical encoding stable and identical on both sides.
        captured = self._capture("POST", "/api/v3/order", {"symbol": "SOL/USDC"})
        body = captured["body"]
        self.assertIsNotNone(body)
        total_params = body.rsplit("&signature=", 1)[0]
        self.assertIn("%2F", total_params)


if __name__ == "__main__":
    unittest.main()
