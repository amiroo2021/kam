from __future__ import annotations

import sys
import unittest
from pathlib import Path
import re
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.agents import x_ondoperps_agent as ondo  # noqa: E402
from plugins.trade.fibo.adapters.ondoperps import OndoPerpsFiboAdapter  # noqa: E402
from plugins.trade.fibo.engine import RealOrderSide  # noqa: E402


class OndoPerpsOrderBodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.credentials = {"api_key": "x", "secret": "y"}
        self.metadata = {
            "market": "ONDO-USD.P",
            "base_increment": None,
            "quote_increment": None,
        }

    def _patch_common(self):
        patches = [
            mock.patch.object(ondo, "_lookup_credentials", return_value=self.credentials),
            mock.patch.object(ondo, "_resolve_market_metadata", return_value=(self.metadata, None)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_limit_order_body_includes_only_limit_fields(self):
        self._patch_common()
        seen = {}

        def fake_signed_post(credentials, path, body):
            seen["path"] = path
            seen["body"] = body
            return {"orderId": "123", "size": body["size"], "price": body["price"]}

        with mock.patch.object(ondo, "_signed_post", side_effect=fake_signed_post), \
             mock.patch.object(ondo, "_fetch_orders_for_verification", return_value=[{
                 "orderId": "123",
                 "market": "ONDO-USD.P",
                 "side": "sell",
                 "status": "open",
                 "price": "0.3331",
                 "size": "1",
             }]), \
             mock.patch.object(ondo, "_verify_snapshot_with_backoff", return_value=True):
            response = ondo._new_order("amiroo", {
                "symbol": "ONDO",
                "side": "sell",
                "order_type": "limit",
                "volume": "1",
                "price": "0.3331",
            })

        self.assertTrue(response.success)
        self.assertEqual(seen["path"], "/v1/perps/orders")
        self.assertEqual(seen["body"], {
            "market": "ONDO-USD.P",
            "side": "sell",
            "type": "limit",
            "size": "1",
            "price": "0.3331",
            "timeInForce": "GTC",
        })

    def test_normal_market_buy_body_omits_limit_only_and_reduce_only_false_fields(self):
        self._patch_common()
        seen = {}

        def fake_signed_post(credentials, path, body):
            seen["path"] = path
            seen["body"] = body
            return {"orderId": "124", "size": body["size"]}

        def fake_signed_get(credentials, path):
            seen["lookup_path"] = path
            return {
                "orderId": "124",
                "clientOrderId": "FIBO_amiroo_ONDO_CB_A7K9_Y1_C1",
                "market": "ONDO-USD.P",
                "side": "buy",
                "type": "market",
                "size": "1",
                "filledSize": "1",
                "status": "fullyfilled",
            }

        with mock.patch.object(ondo, "_signed_post", side_effect=fake_signed_post), \
             mock.patch.object(ondo, "_signed_get", side_effect=fake_signed_get):
            response = ondo._new_order("amiroo", {
                "symbol": "ONDO",
                "side": "buy",
                "order_type": "market",
                "volume": "1",
                "reduce_only": False,
                "client_order_id": "FIBO_amiroo_ONDO_CB_A7K9_Y1_C1",
            })

        self.assertTrue(response.success)
        self.assertEqual(seen["body"], {
            "market": "ONDO-USD.P",
            "side": "buy",
            "type": "market",
            "size": "1",
            "clientOrderId": "FIBO_amiroo_ONDO_CB_A7K9_Y1_C1",
        })
        self.assertEqual(seen["lookup_path"], "/v1/perps/orders/client:FIBO_amiroo_ONDO_CB_A7K9_Y1_C1")
        self.assertNotIn("timeInForce", seen["body"])
        self.assertNotIn("price", seen["body"])
        self.assertNotIn("reduceOnly", seen["body"])

    def test_normal_market_sell_body_omits_limit_only_and_reduce_only_false_fields(self):
        self._patch_common()
        seen = {}

        def fake_signed_post(credentials, path, body):
            seen["path"] = path
            seen["body"] = body
            return {"orderId": "125", "size": body["size"]}

        def fake_signed_get(credentials, path):
            seen["lookup_path"] = path
            return {
                "orderId": "125",
                "clientOrderId": "FIBO_amiroo_ONDO_CS_A7K9_Y1_C1",
                "market": "ONDO-USD.P",
                "side": "sell",
                "type": "market",
                "size": "1",
                "filledSize": "1",
                "status": "fullyfilled",
            }

        with mock.patch.object(ondo, "_signed_post", side_effect=fake_signed_post), \
             mock.patch.object(ondo, "_signed_get", side_effect=fake_signed_get):
            response = ondo._new_order("amiroo", {
                "symbol": "ONDO",
                "side": "sell",
                "order_type": "market",
                "volume": "1",
                "reduce_only": False,
                "client_order_id": "FIBO_amiroo_ONDO_CS_A7K9_Y1_C1",
            })

        self.assertTrue(response.success)
        self.assertEqual(seen["body"], {
            "market": "ONDO-USD.P",
            "side": "sell",
            "type": "market",
            "size": "1",
            "clientOrderId": "FIBO_amiroo_ONDO_CS_A7K9_Y1_C1",
        })
        self.assertEqual(seen["lookup_path"], "/v1/perps/orders/client:FIBO_amiroo_ONDO_CS_A7K9_Y1_C1")
        self.assertNotIn("timeInForce", seen["body"])
        self.assertNotIn("price", seen["body"])
        self.assertNotIn("reduceOnly", seen["body"])

    def test_market_order_verification_fails_on_wrong_market(self):
        self._patch_common()

        with mock.patch.object(ondo, "_signed_post", return_value={"orderId": "125", "size": "1"}), \
             mock.patch.object(ondo, "_signed_get", return_value={
                 "orderId": "125", "clientOrderId": "FIBO_amiroo_ONDO_CS_A7K9_Y1_C1",
                 "market": "BTC-USD.P", "side": "sell", "type": "market",
                 "size": "1", "filledSize": "1", "status": "fullyfilled",
             }):
            response = ondo._new_order("amiroo", {
                "symbol": "ONDO", "side": "sell", "order_type": "market",
                "volume": "1", "client_order_id": "FIBO_amiroo_ONDO_CS_A7K9_Y1_C1",
            })
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "VERIFICATION_FAILED")

    def test_market_order_verification_fails_on_wrong_side(self):
        self._patch_common()

        with mock.patch.object(ondo, "_signed_post", return_value={"orderId": "125", "size": "1"}), \
             mock.patch.object(ondo, "_signed_get", return_value={
                 "orderId": "125", "clientOrderId": "FIBO_amiroo_ONDO_CS_A7K9_Y1_C1",
                 "market": "ONDO-USD.P", "side": "buy", "type": "market",
                 "size": "1", "filledSize": "1", "status": "fullyfilled",
             }):
            response = ondo._new_order("amiroo", {
                "symbol": "ONDO", "side": "sell", "order_type": "market",
                "volume": "1", "client_order_id": "FIBO_amiroo_ONDO_CS_A7K9_Y1_C1",
            })
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "VERIFICATION_FAILED")

    def test_market_order_verification_fails_on_wrong_size(self):
        self._patch_common()

        with mock.patch.object(ondo, "_signed_post", return_value={"orderId": "125", "size": "1"}), \
             mock.patch.object(ondo, "_signed_get", return_value={
                 "orderId": "125", "clientOrderId": "FIBO_amiroo_ONDO_CS_A7K9_Y1_C1",
                 "market": "ONDO-USD.P", "side": "sell", "type": "market",
                 "size": "2", "filledSize": "2", "status": "fullyfilled",
             }):
            response = ondo._new_order("amiroo", {
                "symbol": "ONDO", "side": "sell", "order_type": "market",
                "volume": "1", "client_order_id": "FIBO_amiroo_ONDO_CS_A7K9_Y1_C1",
            })
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "VERIFICATION_FAILED")

    def test_get_exact_order_operation_returns_lookup_order_id(self):
        self._patch_common()

        with mock.patch.object(ondo, "_signed_get", return_value={
            "orderId": "555",
            "clientOrderId": "FIBO_amiroo_ONDO_CS_A7K9_Y1_C1",
            "market": "ONDO-USD.P",
            "side": "sell",
            "type": "market",
            "size": "1",
            "filledSize": "1",
            "status": "fullyfilled",
        }):
            response = ondo.execute({
                "operation": "get_exact_order",
                "exchange": "ondoperps",
                "account": "amiroo",
                "symbol": "ONDO",
                "side": "sell",
                "volume": "1",
                "client_order_id": "FIBO_amiroo_ONDO_CS_A7K9_Y1_C1",
            })
        self.assertTrue(response.success)
        self.assertEqual(response.order.exchange_order_id, 555)
        self.assertEqual(response.order.status, "fullyfilled")

    def test_get_exact_order_operation_returns_order_verify_failed_on_mismatch(self):
        self._patch_common()

        with mock.patch.object(ondo, "_signed_get", return_value={
            "orderId": "555",
            "clientOrderId": "FIBO_amiroo_ONDO_CS_A7K9_Y1_C1",
            "market": "BTC-USD.P",
            "side": "sell",
            "type": "market",
            "size": "1",
            "filledSize": "1",
            "status": "fullyfilled",
        }):
            response = ondo.execute({
                "operation": "get_exact_order",
                "exchange": "ondoperps",
                "account": "amiroo",
                "symbol": "ONDO",
                "side": "sell",
                "volume": "1",
                "client_order_id": "FIBO_amiroo_ONDO_CS_A7K9_Y1_C1",
            })
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "ORDER_VERIFY_FAILED")

    def test_reduce_only_market_close_body_includes_reduce_only_true(self):
        self._patch_common()
        seen = {}

        def fake_signed_post(credentials, path, body):
            seen["path"] = path
            seen["body"] = body
            return {"orderId": "126"}

        with mock.patch.object(ondo, "_fetch_positions_snapshot", side_effect=[[
                {"market": "ONDO-USD.P", "direction": "short", "netQuantity": "1"}
            ], []]), \
             mock.patch.object(ondo, "_signed_post", side_effect=fake_signed_post):
            response = ondo._close_position("amiroo", {"symbol": "ONDO"})

        self.assertTrue(response.success)
        self.assertEqual(seen["body"], {
            "market": "ONDO-USD.P",
            "side": "buy",
            "type": "market",
            "size": "1",
            "reduceOnly": True,
        })
        self.assertNotIn("timeInForce", seen["body"])
        self.assertNotIn("price", seen["body"])

    def test_fibo_adapter_routes_counterBUY_to_buy_market_and_counterSELL_to_sell_market(self):
        class FakeAgent:
            def __init__(self):
                self.calls = []
            def execute(self, req):
                self.calls.append(req)
                return {
                    "success": True,
                    "operation": "new_order",
                    "exchange": "ondoperps",
                    "account": "amiroo",
                    "order": {"exchange_order_id": "777"},
                }

        agent = FakeAgent()
        adapter = OndoPerpsFiboAdapter("ondoperps", "amiroo", agent)
        adapter._run_id_by_instance["k1"] = "A7K9"
        adapter._run_id_by_instance["k2"] = "A7K9"
        buy_id = adapter.submit_volume_market_order(
            instance_key="k1", instrument="ONDO", side=RealOrderSide.BUY, counter_step=1, volume=1
        )
        sell_id = adapter.submit_volume_market_order(
            instance_key="k2", instrument="ONDO", side=RealOrderSide.SELL, counter_step=1, volume=1
        )
        new_order_calls = [c for c in agent.calls if c.get("operation") == "new_order"]
        self.assertEqual(buy_id, "777")
        self.assertEqual(sell_id, "777")
        self.assertEqual(new_order_calls[0]["side"], "buy")
        self.assertEqual(new_order_calls[1]["side"], "sell")
        self.assertEqual(new_order_calls[0]["order_type"], "market")
        self.assertEqual(new_order_calls[1]["order_type"], "market")
        self.assertEqual(new_order_calls[0]["client_order_id"], "FIBO_amiroo_ONDO_CB_A7K9_Y1_C1")
        self.assertEqual(new_order_calls[1]["client_order_id"], "FIBO_amiroo_ONDO_CS_A7K9_Y1_C1")

    def test_lookup_request_format_uses_client_prefix(self):
        self.assertEqual(
            ondo._client_order_lookup_path("FIBO_amiroo_ONDO_CS_A7K9_Y2_C3"),
            "/v1/perps/orders/client:FIBO_amiroo_ONDO_CS_A7K9_Y2_C3",
        )

    def test_invalid_client_order_id_chars_are_rejected_before_submit(self):
        self._patch_common()
        with mock.patch.object(ondo, "_signed_post") as signed_post:
            response = ondo._new_order("amiroo", {
                "symbol": "ONDO",
                "side": "buy",
                "order_type": "market",
                "volume": "1",
                "client_order_id": "bad id with spaces",
            })
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "INVALID_CLIENT_ORDER_ID")
        signed_post.assert_not_called()

    def test_client_order_ids_use_allowed_charset(self):
        agent = type("A", (), {"execute": lambda self, req: {"success": True, "order": {"exchange_order_id": "1"}}})()
        adapter = OndoPerpsFiboAdapter("ondoperps", "amiroo", agent)
        adapter._run_id_by_instance["ondoperps:amiroo:ONDO:counterSELL"] = "A7K9"
        cid = adapter._client_order_id_for(
            instance_key="ondoperps:amiroo:ONDO:counterSELL",
            instrument="ONDO",
            side=RealOrderSide.SELL,
            counter_step=1,
        )
        self.assertRegex(cid, r"^[A-Za-z0-9_-]{1,64}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
