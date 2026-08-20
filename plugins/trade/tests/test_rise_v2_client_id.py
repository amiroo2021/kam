"""Phase 3 follow-up tests: Rise client_order_id policy tightened to '0' only.

Live evidence from Phase 3 (main) showed Rise PlaceOrderWithPermitV2 reverts on
any non-zero client_order_id. This file updates the tests to assert that:
  * empty / missing -> "0" (legacy default)
  * "0"            -> "0" (explicit zero is accepted)
  * any non-zero   -> RISE_CLIENT_ORDER_ID_UNSUPPORTED, no HTTP mutation

The original Phase 3 normalizer unit tests (which assumed the V2 48-bit policy)
are kept as references for future multi-venue helpers; the active production
policy is "0-only".
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any, Dict
from unittest import mock

from plugins.trade.agents import x_rise_agent as rise


def _markets_min():
    return {
        "markets": [
            {
                "market_id": "5",
                "config": {"name": "HYPE/USDC", "step_size": "0.01",
                           "step_price": "0.001", "min_order_size": "0.01"},
                "display_name": "HYPE/USDC", "active": True,
                "last_price": "62", "mark_price": "62",
            }
        ]
    }


def _market_metadata():
    return {
        "market_id": "5",
        "symbol": "HYPE",
        "step_size": "0.01",
        "step_price": "0.001",
        "min_order_size": "0.01",
        "active": True,
    }


def _aligned_target_row(size="0.02", price="62", eoid="0xEOID", resting=100, market="5", side="BUY"):
    side_int = 0 if side.upper() == "BUY" else 1
    return {
        "market_id": market,
        "side_int": side_int,
        "size_steps": int(int(float(size) * 1000)),
        "price_ticks": int(int(float(price) * 1000)),
        "order_id": eoid,
        "resting_order_id": str(resting),
        "wide_order_id": "w" + str(resting),
        "symbol": "HYPE",
        "side": side.upper(),
        "size": size,
        "price": price,
        "reduce_only": False,
        "post_only": False,
        "order_type": "limit",
        "time_in_force": "GTC",
        "price_precision": 3,
    }


def _capture_body_call():
    captured = {"body": None}

    def side_effect(*args, **kwargs):
        body = kwargs.get("data")
        if body is None and len(args) > 1:
            body = args[1]
        if body is None and len(args) == 1:
            body = {}
        captured["body"] = body
        return {"data": {"order_id": "0xEOID", "wide_order_id": "w0"}}

    return side_effect, captured


class _Base(unittest.TestCase):
    def setUp(self):
        self._cred = mock.patch.object(
            rise, "_lookup_credentials",
            return_value=("0x" + "ab" * 20, "0x" + "11" * 32),
        )
        self._verify = mock.patch.object(
            rise, "_verify_new_order_submission",
            return_value=(True, "0xEOID", _aligned_target_row()),
        )
        self._cred.start()
        self._verify.start()

    def tearDown(self):
        self._cred.stop()
        self._verify.stop()


class NormalizerZeroOnlyTests(unittest.TestCase):
    def test_missing_is_none(self):
        self.assertIsNone(rise._rise_normalize_v2_client_order_id(None))

    def test_empty_is_none(self):
        self.assertIsNone(rise._rise_normalize_v2_client_order_id(""))

    def test_zero_string_ok(self):
        self.assertEqual(rise._rise_normalize_v2_client_order_id("0"), "0")

    def test_zero_int_ok(self):
        self.assertEqual(rise._rise_normalize_v2_client_order_id(0), "0")

    def test_nonzero_string_rejected(self):
        with self.assertRaises(ValueError):
            rise._rise_normalize_v2_client_order_id("82738589974528")

    def test_nonzero_int_rejected(self):
        with self.assertRaises(ValueError):
            rise._rise_normalize_v2_client_order_id(1)

    def test_hex_rejected(self):
        with self.assertRaises(ValueError):
            rise._rise_normalize_v2_client_order_id("0xFF")

    def test_bool_rejected(self):
        with self.assertRaises(ValueError):
            rise._rise_normalize_v2_client_order_id(True)


class NewOrderClientIdPolicyTests(_Base):
    def test_omission_keeps_zero(self):
        side_effect, captured = _capture_body_call()
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_min()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"5": _market_metadata()}), \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               side_effect=lambda wallet: {"data": {"orders": [_aligned_target_row()]}}), \
             mock.patch.object(rise, "_normalize_open_orders",
                               return_value=[_aligned_target_row()]), \
             mock.patch.object(rise, "_fetch_nonce_state",
                               return_value={"nonce_anchor": 1, "current_bitmap_index": 0}), \
             mock.patch.object(rise, "_post_json", side_effect=side_effect):
            resp = rise.execute({
                "operation": "new_order", "account": "BASED",
                "symbol": "HYPE", "side": "buy",
                "order_type": "limit", "volume": "0.02", "price": "62",
                "time_in_force": "GTC",
            })
        self.assertTrue(resp.success)
        self.assertEqual(str(captured["body"]["client_order_id"]), "0")
        self.assertEqual(str(resp.order.client_order_id), "0")

    def test_explicit_zero_works(self):
        side_effect, captured = _capture_body_call()
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_min()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"5": _market_metadata()}), \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               side_effect=lambda wallet: {"data": {"orders": [_aligned_target_row()]}}), \
             mock.patch.object(rise, "_normalize_open_orders",
                               return_value=[_aligned_target_row()]), \
             mock.patch.object(rise, "_fetch_nonce_state",
                               return_value={"nonce_anchor": 1, "current_bitmap_index": 0}), \
             mock.patch.object(rise, "_post_json", side_effect=side_effect):
            resp = rise.execute({
                "operation": "new_order", "account": "BASED",
                "symbol": "HYPE", "side": "buy",
                "order_type": "limit", "volume": "0.02", "price": "62",
                "time_in_force": "GTC",
                "client_order_id": "0",
            })
        self.assertTrue(resp.success)
        self.assertEqual(str(captured["body"]["client_order_id"]), "0")

    def test_nonzero_rejected_before_mutation(self):
        with mock.patch.object(rise, "_post_json", return_value={}) as _post:
            resp = rise.execute({
                "operation": "new_order", "account": "BASED",
                "symbol": "HYPE", "side": "buy",
                "order_type": "limit", "volume": "0.02", "price": "62",
                "time_in_force": "GTC",
                "client_order_id": "82738589974528",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "RISE_CLIENT_ORDER_ID_UNSUPPORTED")
        self.assertEqual(_post.call_count, 0)

    def test_alias_nonzero_also_rejected(self):
        with mock.patch.object(rise, "_post_json", return_value={}) as _post:
            resp = rise.execute({
                "operation": "new_order", "account": "BASED",
                "symbol": "HYPE", "side": "buy",
                "order_type": "limit", "volume": "0.02", "price": "62",
                "time_in_force": "GTC",
                "client_id": "82738589974528",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "RISE_CLIENT_ORDER_ID_UNSUPPORTED")
        self.assertEqual(_post.call_count, 0)


class MarketImmediateClientIdPolicyTests(_Base):
    def test_nonzero_rejected(self):
        with mock.patch.object(rise, "_post_json", return_value={}) as _post:
            resp = rise.execute({
                "operation": "market_immediate", "account": "BASED",
                "symbol": "HYPE", "side": "buy", "volume": "0.02",
                "slip_pct": "0.005", "max_wait_seconds": 0.05,
                "client_order_id": "82738589974528",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "RISE_CLIENT_ORDER_ID_UNSUPPORTED")
        self.assertEqual(_post.call_count, 0)

    def test_omission_defaults_to_zero(self):
        side_effect, captured = _capture_body_call()
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_min()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"5": _market_metadata()}), \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               side_effect=lambda wallet: {"data": {"orders": []}}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=[]), \
             mock.patch.object(rise, "_rise_market_price",
                               return_value=Decimal("62")), \
             mock.patch.object(rise, "_rise_position_snapshot",
                               side_effect=[{"side": "flat", "size": Decimal("0"),
                                             "entry_price": Decimal("0")},
                                            {"side": "long", "size": Decimal("0.02"),
                                             "entry_price": Decimal("62.10")}]), \
             mock.patch.object(rise, "_fetch_nonce_state",
                               return_value={"nonce_anchor": 1, "current_bitmap_index": 0}), \
             mock.patch.object(rise, "_post_json", side_effect=side_effect):
            resp = rise.execute({
                "operation": "market_immediate", "account": "BASED",
                "symbol": "HYPE", "side": "buy", "volume": "0.02",
                "slip_pct": "0.005", "max_wait_seconds": 0.05,
            })
        self.assertTrue(resp.success)
        self.assertEqual(str(captured["body"]["client_order_id"]), "0")
        self.assertEqual(resp.order_state.get("venue_roundtrip_verified"), False)


if __name__ == "__main__":
    unittest.main()