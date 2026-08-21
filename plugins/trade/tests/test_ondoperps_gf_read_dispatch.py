"""Real execute() dispatch for Ondo GoldenFibo read/cancel ops.

Does NOT mock execute(). Patches only low-level helpers on the same
module object the tests call, so full-suite module rebinding cannot
redirect the patch to a different x_ondoperps_agent copy.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import mock

from plugins.trade.agents import x_ondoperps_agent as ondo


CREDS = {"api_key": "x", "secret": "y"}


class OndoGfDispatchTests(unittest.TestCase):
    def test_market_constraints_via_execute(self):
        metadata = {
            "market": "ONDO-USD.P",
            "base_increment": Decimal("1"),
            "quote_increment": Decimal("0.0001"),
        }
        with mock.patch.object(ondo, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(ondo, "_resolve_market_metadata", return_value=(metadata, None)):
            resp = ondo.execute({
                "operation": "market_constraints",
                "account": "amiroo",
                "symbol": "ONDO",
            })
        self.assertTrue(resp.success)
        st = resp.order_state or {}
        self.assertEqual(st["tick_size"], "0.0001")
        self.assertEqual(st["step_size"], "1")
        self.assertEqual(st["size_decimals"], 0)
        self.assertEqual(st["price_decimals"], 4)
        self.assertNotIn("min_quote_amount", st)

    def test_get_order_state_open_via_execute(self):
        row = {
            "orderId": "555",
            "status": "open",
            "side": "buy",
            "size": "1",
            "filledSize": "0",
            "price": "0.4",
            "market": "ONDO-USD.P",
        }
        with mock.patch.object(ondo, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(ondo, "_signed_get", return_value=row):
            resp = ondo.execute({
                "operation": "get_order_state",
                "account": "amiroo",
                "order_id": "555",
            })
        self.assertTrue(resp.success)
        self.assertEqual((resp.order_state or {})["status"], "OPEN")
        self.assertEqual((resp.order_state or {})["taxonomy"], "ACTIVE")

    def test_get_order_state_404_is_unknown_not_filled(self):
        err = ondo.OndoHTTPError(status=404, path="/v1/perps/orders/555", body="order_not_found")
        with mock.patch.object(ondo, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(ondo, "_signed_get", side_effect=err):
            resp = ondo.execute({
                "operation": "get_order_state",
                "account": "amiroo",
                "order_id": "555",
            })
        self.assertTrue(resp.success)
        self.assertEqual((resp.order_state or {})["status"], "UNKNOWN")
        self.assertNotEqual((resp.order_state or {})["status"], "FILLED")

    def test_get_order_state_fullyfilled(self):
        row = {"orderId": "9", "status": "fullyFilled", "size": "1", "filledSize": "1", "side": "buy"}
        with mock.patch.object(ondo, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(ondo, "_signed_get", return_value=row):
            resp = ondo.execute({
                "operation": "get_order_state",
                "account": "amiroo",
                "order_id": "9",
            })
        self.assertEqual((resp.order_state or {})["status"], "FILLED")

    def test_get_order_state_by_client_id_via_execute(self):
        row = {"orderId": "9", "status": "open", "clientOrderId": "123", "size": "1", "filledSize": "0"}
        with mock.patch.object(ondo, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(ondo, "_fetch_order_by_client_order_id", return_value=row):
            resp = ondo.execute({
                "operation": "get_order_state_by_client_id",
                "account": "amiroo",
                "client_order_id": "123",
            })
        self.assertTrue(resp.success)
        self.assertEqual((resp.order_state or {})["status"], "OPEN")
        self.assertEqual((resp.order_state or {})["client_order_id"], "123")

    def test_new_order_still_omits_reduce_only_false(self):
        """Existing /trade market body must remain unchanged."""
        seen = {}
        metadata = {"market": "ONDO-USD.P", "base_increment": None, "quote_increment": None}

        def fake_post(credentials, path, body):
            seen["body"] = body
            return {"orderId": "1", "size": body["size"]}

        with mock.patch.object(ondo, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(ondo, "_resolve_market_metadata", return_value=(metadata, None)), \
             mock.patch.object(ondo, "_signed_post", side_effect=fake_post), \
             mock.patch.object(ondo, "_verify_exact_order_by_client_order_id",
                               return_value=(True, {"size": "1", "price": None, "status": "fullyFilled",
                                                    "market": "ONDO-USD.P", "side": "buy"})):
            resp = ondo.execute({
                "operation": "new_order",
                "account": "amiroo",
                "symbol": "ONDO",
                "side": "buy",
                "order_type": "market",
                "volume": "1",
                "client_order_id": "42",
            })
        self.assertTrue(resp.success)
        self.assertNotIn("reduceOnly", seen["body"])
        self.assertEqual(seen["body"]["clientOrderId"], "42")


if __name__ == "__main__":
    unittest.main()
