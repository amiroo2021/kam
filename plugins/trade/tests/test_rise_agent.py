from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.trade.agents import x_rise_agent as rise


class RiseAgentTests(unittest.TestCase):
    def test_extract_response_order_id_uses_only_canonical_order_id(self):
        payload = {"data": {"order_id": "0xabc", "resting_order_id": "4717", "wide_order_id": "9435"}}
        self.assertEqual(rise._extract_response_order_id(payload), "0xabc")
        self.assertIsNone(rise._extract_response_order_id({"data": {"resting_order_id": "4717", "wide_order_id": "9435"}}))

    def test_canonical_order_id_reads_order_id_only(self):
        order = {"order_id": "0xabc", "resting_order_id": "4717", "wide_order_id": "9435"}
        self.assertEqual(rise._canonical_order_id(order), "0xabc")
        self.assertIsNone(rise._canonical_order_id({"resting_order_id": "4717"}))

    def test_verify_new_order_submission_returns_canonical_order_id_and_row(self):
        open_rows = [{
            "market_id": "24",
            "side_int": 1,
            "size_steps": 15,
            "price_ticks": 80454,
            "order_id": "0xabc",
            "resting_order_id": "4717",
            "wide_order_id": "9435",
        }]
        with mock.patch.object(rise, "_fetch_open_orders_payload", return_value={}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=open_rows):
            verified, exchange_order_id, matched_row = rise._verify_new_order_submission(
                wallet="0xwallet",
                market_cache={},
                market_id="24",
                side_int=1,
                size_steps=15,
                price_ticks=80454,
                response_order_id="0xabc",
            )
        self.assertTrue(verified)
        self.assertEqual(exchange_order_id, "0xabc")
        self.assertEqual(matched_row["order_id"], "0xabc")
        self.assertEqual(matched_row["resting_order_id"], "4717")
        self.assertEqual(matched_row["wide_order_id"], "9435")

    def test_verify_new_order_submission_rejects_resting_order_id_substitution(self):
        open_rows = [{
            "market_id": "24",
            "side_int": 1,
            "size_steps": 15,
            "price_ticks": 80454,
            "order_id": "0xabc",
            "resting_order_id": "4717",
            "wide_order_id": "9435",
        }]
        with mock.patch.object(rise, "_fetch_open_orders_payload", return_value={}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=open_rows):
            verified, exchange_order_id, matched_row = rise._verify_new_order_submission(
                wallet="0xwallet",
                market_cache={},
                market_id="24",
                side_int=1,
                size_steps=15,
                price_ticks=80454,
                response_order_id="4717",
            )
        self.assertFalse(verified)
        self.assertIsNone(exchange_order_id)
        self.assertIsNone(matched_row)

    def test_verify_new_order_submission_requires_canonical_response_order_id(self):
        open_rows = [{
            "market_id": "24",
            "side_int": 1,
            "size_steps": 15,
            "price_ticks": 80454,
            "order_id": "0xabc",
            "resting_order_id": "4717",
            "wide_order_id": "9435",
        }]
        with mock.patch.object(rise, "_fetch_open_orders_payload", return_value={}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=open_rows):
            verified, exchange_order_id, matched_row = rise._verify_new_order_submission(
                wallet="0xwallet",
                market_cache={},
                market_id="24",
                side_int=1,
                size_steps=15,
                price_ticks=80454,
                response_order_id=None,
            )
        self.assertFalse(verified)
        self.assertIsNone(exchange_order_id)
        self.assertIsNone(matched_row)

    def test_verify_rise_ladder_submission_returns_canonical_order_ids(self):
        open_rows = [
            {"market_id": "24", "side_int": 1, "size_steps": 15, "price_ticks": 80454, "order_id": "0xaaa", "resting_order_id": "4717", "wide_order_id": "9435"},
            {"market_id": "24", "side_int": 1, "size_steps": 107, "price_ticks": 80636, "order_id": "0xbbb", "resting_order_id": "8589939309", "wide_order_id": "17179878619"},
        ]
        expected = [
            {"size_steps": 15, "price_ticks": 80454, "response_order_id": "0xaaa"},
            {"size_steps": 107, "price_ticks": 80636, "response_order_id": "0xbbb"},
        ]
        with mock.patch.object(rise, "_fetch_open_orders_payload", return_value={}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=open_rows):
            verified, order_ids, matched_rows = rise._verify_rise_ladder_submission(
                wallet="0xwallet",
                market_cache={},
                market_id="24",
                side_int=1,
                expected_payloads=expected,
            )
        self.assertTrue(verified)
        self.assertEqual(order_ids, ["0xaaa", "0xbbb"])
        self.assertEqual([row["resting_order_id"] for row in matched_rows], ["4717", "8589939309"])

    def test_verify_rise_ladder_submission_requires_one_to_one_row_matching(self):
        open_rows = [
            {"market_id": "24", "side_int": 1, "size_steps": 15, "price_ticks": 80454, "order_id": "0xaaa", "resting_order_id": "4717", "wide_order_id": "9435"},
        ]
        expected = [
            {"size_steps": 15, "price_ticks": 80454, "response_order_id": "0xaaa"},
            {"size_steps": 15, "price_ticks": 80454, "response_order_id": "0xaaa"},
        ]
        with mock.patch.object(rise, "_fetch_open_orders_payload", return_value={}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=open_rows):
            verified, order_ids, matched_rows = rise._verify_rise_ladder_submission(
                wallet="0xwallet",
                market_cache={},
                market_id="24",
                side_int=1,
                expected_payloads=expected,
            )
        self.assertFalse(verified)
        self.assertEqual(order_ids, ["0xaaa"])
        self.assertEqual(len(matched_rows), 1)

    def test_preexisting_identical_order_cannot_satisfy_child_without_response_order_id(self):
        open_rows = [
            {"market_id": "24", "side_int": 1, "size_steps": 15, "price_ticks": 80454, "order_id": "0xaaa", "resting_order_id": "4717", "wide_order_id": "9435"},
        ]
        expected = [
            {"size_steps": 15, "price_ticks": 80454},
        ]
        with mock.patch.object(rise, "_fetch_open_orders_payload", return_value={}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=open_rows):
            verified, order_ids, matched_rows = rise._verify_rise_ladder_submission(
                wallet="0xwallet",
                market_cache={},
                market_id="24",
                side_int=1,
                expected_payloads=expected,
            )
        self.assertFalse(verified)
        self.assertEqual(order_ids, [])
        self.assertEqual(matched_rows, [])

    def test_find_matching_open_order_does_not_mix_identifier_families(self):
        open_rows = [
            {"market_id": "24", "side_int": 1, "size_steps": 15, "price_ticks": 80454, "order_id": "0xaaa", "resting_order_id": "4717", "wide_order_id": "9435"},
            {"market_id": "24", "side_int": 1, "size_steps": 15, "price_ticks": 80454, "order_id": "0xbbb", "resting_order_id": "0xaaa", "wide_order_id": "9999"},
        ]
        matched = rise._find_matching_open_order(
            post_orders=open_rows,
            market_id="24",
            side_int=1,
            size_steps=15,
            price_ticks=80454,
            response_order_id="0xaaa",
            used_positions=set(),
        )
        self.assertIsNotNone(matched)
        self.assertEqual(matched[1]["order_id"], "0xaaa")
        self.assertEqual(matched[1]["resting_order_id"], "4717")

    def test_execute_cancel_order_group_uses_resting_order_id_for_signing_and_order_id_for_payload(self):
        open_rows = [
            {"market_id": "24", "symbol": "SOL", "side": "sell", "order_id": "0xaaa", "resting_order_id": "4717", "wide_order_id": "9435"},
            {"market_id": "24", "symbol": "SOL", "side": "sell", "order_id": "0xbbb", "resting_order_id": "8589939309", "wide_order_id": "17179878619"},
        ]
        post_calls = []
        action_hash_calls = []
        def fake_open_orders(_wallet):
            return {"rows": []}
        def fake_normalize(_payload, _cache):
            return open_rows if fake_normalize.calls == 0 else []
        fake_normalize.calls = 0
        def normalize_with_count(payload, cache):
            result = open_rows if normalize_with_count.calls == 0 else []
            normalize_with_count.calls += 1
            return result
        normalize_with_count.calls = 0
        def fake_action_hash(*, market_id, resting_order_id):
            action_hash_calls.append((market_id, resting_order_id))
            return b"hash"
        def fake_post(url, payload):
            post_calls.append((url, payload))
            return {"ok": True}
        with mock.patch.object(rise, "_lookup_credentials", return_value=("0xwallet", "0xpriv")), \
             mock.patch.object(rise, "_fetch_markets_payload", return_value={}), \
             mock.patch.object(rise, "_market_cache", return_value={}), \
             mock.patch.object(rise, "_fetch_open_orders_payload", side_effect=fake_open_orders), \
             mock.patch.object(rise, "_normalize_open_orders", side_effect=normalize_with_count), \
             mock.patch.object(rise, "_fetch_nonce_state", return_value={"nonce_anchor": 10, "current_bitmap_index": 11}), \
             mock.patch.object(rise, "_rise_order_deadline", return_value=123456), \
             mock.patch.object(rise, "_rise_encode_cancel_action_hash", side_effect=fake_action_hash), \
             mock.patch.object(rise, "_rise_sign_eip712_verify_witness", return_value=b"sig"), \
             mock.patch.object(rise, "_rise_sig_to_base64", return_value="c2ln"), \
             mock.patch.object(rise.Account, "from_key") as from_key, \
             mock.patch.object(rise, "_post_json", side_effect=fake_post):
            from_key.return_value.address = "0xsigner"
            response = rise._execute_cancel_order_group("bitget", {"symbol": "SOL", "side": "sell"}).to_dict()
        self.assertTrue(response["success"])
        self.assertEqual(action_hash_calls, [(24, 4717), (24, 8589939309)])
        self.assertEqual(post_calls[0][1]["order_id"], "0xaaa")
        self.assertEqual(post_calls[1][1]["order_id"], "0xbbb")
        self.assertEqual(post_calls[0][1]["permit"]["nonce_anchor"], "10")
        self.assertEqual(post_calls[0][1]["permit"]["nonce_bitmap_index"], 11)

    def test_opaque_hex_order_id_survives_unmodified_when_verify_after_submit_is_disabled(self):
        with mock.patch.object(rise, "_post_json", return_value={"data": {"order_id": "0x0000000cfeedbeef"}}), \
             mock.patch.object(rise, "_fetch_nonce_state", return_value={"nonce_anchor": 10, "current_bitmap_index": 11}), \
             mock.patch.object(rise, "_rise_order_deadline", return_value=123456), \
             mock.patch.object(rise, "_rise_encode_order_action_hash", return_value=b"hash"), \
             mock.patch.object(rise, "_rise_sign_eip712_verify_witness", return_value=b"sig"), \
             mock.patch.object(rise, "_rise_sig_to_base64", return_value="c2ln"), \
             mock.patch.object(rise.Account, "from_key") as from_key:
            from_key.return_value.address = "0xsigner"
            response, payload, _submitted_volume, _submitted_price = rise._submit_rise_limit_order(
                wallet="0xwallet",
                signer_private="0xpriv",
                market_cache={},
                market={"market_id": "24", "step_price": "0.001", "step_size": "0.001", "min_order_size": "0.001", "symbol": "SOL"},
                requested_symbol="SOL",
                requested_side="sell",
                requested_volume=rise.Decimal("1.234"),
                requested_price=rise.Decimal("80.454"),
                requested_tif="GTC",
                reduce_only=False,
                operation="ladder",
                account="bitget",
                verify_after_submit=False,
            )
        self.assertTrue(response.success)
        self.assertEqual(response.order.exchange_order_id, "0x0000000cfeedbeef")
        self.assertEqual(payload["size_steps"], rise._rise_steps(rise.Decimal("1.234"), rise.Decimal("0.001")))

    def test_submit_response_without_order_id_cannot_verify_single_order(self):
        market = {"market_id": "24", "step_price": "0.001", "step_size": "0.001", "min_order_size": "0.001", "symbol": "SOL"}
        open_rows = [{
            "market_id": "24",
            "side_int": 1,
            "size_steps": rise._rise_steps(rise.Decimal("1.234"), rise.Decimal("0.001")),
            "price_ticks": rise._rise_steps(rise.Decimal("80.454"), rise.Decimal("0.001")),
            "order_id": "0xabc",
            "resting_order_id": "4717",
            "wide_order_id": "9435",
        }]
        with mock.patch.object(rise, "_post_json", return_value={"data": {}}) as post_json, \
             mock.patch.object(rise, "_fetch_nonce_state", return_value={"nonce_anchor": 10, "current_bitmap_index": 11}), \
             mock.patch.object(rise, "_rise_order_deadline", return_value=123456), \
             mock.patch.object(rise, "_rise_encode_order_action_hash", return_value=b"hash"), \
             mock.patch.object(rise, "_rise_sign_eip712_verify_witness", return_value=b"sig"), \
             mock.patch.object(rise, "_rise_sig_to_base64", return_value="c2ln"), \
             mock.patch.object(rise, "_fetch_open_orders_payload", return_value={}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=open_rows), \
             mock.patch.object(rise.Account, "from_key") as from_key:
            from_key.return_value.address = "0xsigner"
            response, _payload, _submitted_volume, _submitted_price = rise._submit_rise_limit_order(
                wallet="0xwallet",
                signer_private="0xpriv",
                market_cache={},
                market=market,
                requested_symbol="SOL",
                requested_side="buy",
                requested_volume=rise.Decimal("1.234"),
                requested_price=rise.Decimal("80.454"),
                requested_tif="GTC",
                reduce_only=False,
                operation="new_order",
                account="bitget",
                verify_after_submit=True,
            )
        result = response.to_dict()
        self.assertFalse(result["success"])
        self.assertEqual(result["error"]["code"], "VERIFICATION_FAILED")
        self.assertFalse(result["order"]["verified"])
        self.assertIsNone(result["order"].get("exchange_order_id"))
        self.assertEqual(post_json.call_count, 1)

    def test_resting_order_id_only_response_cannot_verify_single_order(self):
        market = {"market_id": "24", "step_price": "0.001", "step_size": "0.001", "min_order_size": "0.001", "symbol": "SOL"}
        open_rows = [{
            "market_id": "24",
            "side_int": 1,
            "size_steps": rise._rise_steps(rise.Decimal("1.234"), rise.Decimal("0.001")),
            "price_ticks": rise._rise_steps(rise.Decimal("80.454"), rise.Decimal("0.001")),
            "order_id": "0xabc",
            "resting_order_id": "4717",
            "wide_order_id": "9435",
        }]
        with mock.patch.object(rise, "_post_json", return_value={"data": {"resting_order_id": "4717"}}), \
             mock.patch.object(rise, "_fetch_nonce_state", return_value={"nonce_anchor": 10, "current_bitmap_index": 11}), \
             mock.patch.object(rise, "_rise_order_deadline", return_value=123456), \
             mock.patch.object(rise, "_rise_encode_order_action_hash", return_value=b"hash"), \
             mock.patch.object(rise, "_rise_sign_eip712_verify_witness", return_value=b"sig"), \
             mock.patch.object(rise, "_rise_sig_to_base64", return_value="c2ln"), \
             mock.patch.object(rise, "_fetch_open_orders_payload", return_value={}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=open_rows), \
             mock.patch.object(rise.Account, "from_key") as from_key:
            from_key.return_value.address = "0xsigner"
            response, _payload, _submitted_volume, _submitted_price = rise._submit_rise_limit_order(
                wallet="0xwallet",
                signer_private="0xpriv",
                market_cache={},
                market=market,
                requested_symbol="SOL",
                requested_side="buy",
                requested_volume=rise.Decimal("1.234"),
                requested_price=rise.Decimal("80.454"),
                requested_tif="GTC",
                reduce_only=False,
                operation="new_order",
                account="bitget",
                verify_after_submit=True,
            )
        self.assertFalse(response.success)
        self.assertIsNotNone(response.order)
        self.assertFalse(response.order.verified)
        self.assertIsNone(response.order.exchange_order_id)

    def test_wide_order_id_only_response_cannot_verify_single_order(self):
        market = {"market_id": "24", "step_price": "0.001", "step_size": "0.001", "min_order_size": "0.001", "symbol": "SOL"}
        open_rows = [{
            "market_id": "24",
            "side_int": 1,
            "size_steps": rise._rise_steps(rise.Decimal("1.234"), rise.Decimal("0.001")),
            "price_ticks": rise._rise_steps(rise.Decimal("80.454"), rise.Decimal("0.001")),
            "order_id": "0xabc",
            "resting_order_id": "4717",
            "wide_order_id": "9435",
        }]
        with mock.patch.object(rise, "_post_json", return_value={"data": {"wide_order_id": "9435"}}), \
             mock.patch.object(rise, "_fetch_nonce_state", return_value={"nonce_anchor": 10, "current_bitmap_index": 11}), \
             mock.patch.object(rise, "_rise_order_deadline", return_value=123456), \
             mock.patch.object(rise, "_rise_encode_order_action_hash", return_value=b"hash"), \
             mock.patch.object(rise, "_rise_sign_eip712_verify_witness", return_value=b"sig"), \
             mock.patch.object(rise, "_rise_sig_to_base64", return_value="c2ln"), \
             mock.patch.object(rise, "_fetch_open_orders_payload", return_value={}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=open_rows), \
             mock.patch.object(rise.Account, "from_key") as from_key:
            from_key.return_value.address = "0xsigner"
            response, _payload, _submitted_volume, _submitted_price = rise._submit_rise_limit_order(
                wallet="0xwallet",
                signer_private="0xpriv",
                market_cache={},
                market=market,
                requested_symbol="SOL",
                requested_side="buy",
                requested_volume=rise.Decimal("1.234"),
                requested_price=rise.Decimal("80.454"),
                requested_tif="GTC",
                reduce_only=False,
                operation="new_order",
                account="bitget",
                verify_after_submit=True,
            )
        self.assertFalse(response.success)
        self.assertIsNotNone(response.order)
        self.assertFalse(response.order.verified)
        self.assertIsNone(response.order.exchange_order_id)

    def test_partial_ladder_path_requires_canonical_child_order_ids_for_verification(self):
        child_one = rise.CanonicalOrderResult(
            symbol="SOL",
            side="sell",
            order_type=rise.RISE_ORDER_TYPE_LIMIT,
            requested_volume="1.000",
            requested_price="80.100",
            submitted_volume="1.000",
            submitted_price="80.100",
            verified=True,
            status="success",
            exchange_order_id="0xaaa",
        )
        child_two = rise.CanonicalOrderResult(
            symbol="SOL",
            side="sell",
            order_type=rise.RISE_ORDER_TYPE_LIMIT,
            requested_volume="1.500",
            requested_price="80.200",
            submitted_volume="1.500",
            submitted_price="80.200",
            verified=False,
            status="partial",
            exchange_order_id=None,
        )
        market = {"market_id": "24", "step_price": "0.001", "step_size": "0.001", "min_order_size": "0.001", "symbol": "SOL"}
        children = [
            {"size": rise.Decimal("1.0"), "price": rise.Decimal("80.1")},
            {"size": rise.Decimal("1.5"), "price": rise.Decimal("80.2")},
        ]
        responses = [
            (rise.make_success(operation="ladder", exchange=rise.name, account="bitget", order=child_one), {"size_steps": 1000, "price_ticks": 80100}, rise.Decimal("1.0"), rise.Decimal("80.1")),
            (rise.make_success(operation="ladder", exchange=rise.name, account="bitget", order=child_two), {"size_steps": 1500, "price_ticks": 80200}, rise.Decimal("1.5"), rise.Decimal("80.2")),
        ]
        with mock.patch.object(rise, "_fetch_markets_payload", return_value={}), \
             mock.patch.object(rise, "_market_cache", return_value={}), \
             mock.patch.object(rise, "_resolve_market_by_symbol", return_value=market), \
             mock.patch.object(rise, "_build_rise_ladder_children", return_value=(children, rise.Decimal("2.5"), 0)), \
             mock.patch.object(rise, "_lookup_credentials", return_value=("0xwallet", "0xpriv")), \
             mock.patch.object(rise, "_submit_rise_limit_order", side_effect=responses) as submit_mock, \
             mock.patch.object(rise, "_verify_rise_ladder_submission", return_value=(False, ["0xaaa"], [{"order_id": "0xaaa"}])):
            response = rise._execute_ladder("bitget", {
                "symbol": "SOL",
                "side": "sell",
                "distribution": "uniform",
                "order_count": "2",
                "total_volume": "2.5",
                "start_price": "80.1",
                "end_price": "80.2",
                "time_in_force": "GTC",
            }).to_dict()
        self.assertFalse(response["success"])
        self.assertEqual(response["error"]["code"], "VERIFICATION_FAILED")
        self.assertEqual(response["ladder"]["submitted_order_count"], 2)
        self.assertEqual(response["ladder"]["submitted_volume"], "2.5")
        self.assertEqual(response["ladder"]["child_order_ids"], ["0xaaa"])
        self.assertFalse(response["ladder"]["verified"])
        self.assertTrue(response["ladder"]["partial"])
        self.assertEqual(submit_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
