"""Ondo Perps single-order cancel — GoldenFibo safety requirement.

Ondo documents:
  DELETE /v1/perps/orders/<id>          — one order
  DELETE /v1/perps/orders/batch?orderIDs=A,B,C — many IDs

GoldenFibo must cancel EXACTLY the tracked order. cancel_order_group
(symbol+side) is forbidden as a fallback because it would also cancel
unrelated /trade or other-registration orders on that side.
"""

from __future__ import annotations

import unittest
from unittest import mock

from plugins.trade.agents import x_ondoperps_agent as ondo


class OndoSingleCancelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.credentials = {"api_key": "x", "secret": "y"}

    def test_capabilities_include_cancel_order(self):
        self.assertIn("cancel_order", ondo.capabilities())

    def test_cancel_order_deletes_exactly_one_id(self):
        seen = {}

        def fake_delete(credentials, path):
            seen["path"] = path
            return {"successfulCancels": [{"orderId": "555"}]}

        with mock.patch.object(ondo, "_lookup_credentials", return_value=self.credentials), \
             mock.patch.object(ondo, "_signed_delete", side_effect=fake_delete):
            resp = ondo.execute({
                "operation": "cancel_order",
                "account": "amiroo",
                "order_id": "555",
            })
        self.assertTrue(resp.success)
        self.assertEqual(seen["path"], "/v1/perps/orders/555")
        self.assertNotIn("batch", seen["path"])
        self.assertNotIn("orderIDs", seen["path"])

    def test_cancel_order_does_not_call_cancel_order_group(self):
        with mock.patch.object(ondo, "_lookup_credentials", return_value=self.credentials), \
             mock.patch.object(ondo, "_signed_delete", return_value={}), \
             mock.patch.object(ondo, "_cancel_order_group") as group:
            resp = ondo.execute({
                "operation": "cancel_order",
                "account": "amiroo",
                "order_index": 555,
            })
        self.assertTrue(resp.success)
        group.assert_not_called()

    def test_cancel_order_404_is_idempotent_success(self):
        err = ondo.OndoHTTPError(status=404, path="/v1/perps/orders/555", body="order_not_found")
        with mock.patch.object(ondo, "_lookup_credentials", return_value=self.credentials), \
             mock.patch.object(ondo, "_signed_delete", side_effect=err):
            resp = ondo.execute({
                "operation": "cancel_order",
                "account": "amiroo",
                "order_id": "555",
            })
        self.assertTrue(resp.success)

    def test_cancel_order_missing_id_fails_without_write(self):
        with mock.patch.object(ondo, "_lookup_credentials", return_value=self.credentials), \
             mock.patch.object(ondo, "_signed_delete") as delete:
            resp = ondo.execute({
                "operation": "cancel_order",
                "account": "amiroo",
            })
        self.assertFalse(resp.success)
        delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
