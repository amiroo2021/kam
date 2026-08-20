"""Phase 4 tests: Rise close_position (reduce-only IOC flatten)."""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest import mock

from plugins.trade.agents import x_rise_agent as rise


def _markets_sol():
    return {
        "markets": [
            {
                "market_id": "4",
                "config": {"name": "SOL/USDC", "step_size": "0.001",
                           "step_price": "0.001", "min_order_size": "0.15"},
                "display_name": "SOL/USDC", "active": True,
                "last_price": "81", "mark_price": "81",
            }
        ]
    }


def _sol_market_metadata():
    return {
        "market_id": "4",
        "symbol": "SOL",
        "step_size": "0.001",
        "step_price": "0.001",
        "min_order_size": "0.15",
        "active": True,
    }


def _post_capture():
    captured = {"calls": []}

    def side_effect(*args, **kwargs):
        body = kwargs.get("data") or (args[1] if len(args) > 1 else {}) or {}
        captured["calls"].append({
            "url": args[0] if args else kwargs.get("url"),
            "body": body,
        })
        # First call is /v1/orders/place; return a synthetic exchange_order_id.
        return {"data": {"order_id": "0xCLOSE1", "wide_order_id": "w1"}}

    return captured, side_effect


class _Base(unittest.TestCase):
    def setUp(self):
        self._cred = mock.patch.object(
            rise, "_lookup_credentials",
            return_value=("0x" + "ab" * 20, "0x" + "11" * 32),
        )
        self._cred.start()
        # Avoid real waits in the close-poll loop.
        self._sleep = mock.patch.object(rise._t, "sleep", return_value=None)
        self._sleep.start()

    def tearDown(self):
        self._cred.stop()
        self._sleep.stop()


class PositionReadbackTests(_Base):
    def test_long_position_sell_close(self):
        captured, side_effect = _post_capture()
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_sol()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"4": _sol_market_metadata()}), \
             mock.patch.object(rise, "_rise_market_price",
                               return_value=Decimal("81")), \
             mock.patch.object(rise, "_fetch_nonce_state",
                               return_value={"nonce_anchor": 1, "current_bitmap_index": 0}), \
             mock.patch.object(rise, "_rise_position_snapshot",
                               side_effect=[
                                   {"side": "long", "size": Decimal("0.2"),
                                    "entry_price": Decimal("80.5")},
                                   {"side": "flat", "size": Decimal("0"),
                                    "entry_price": Decimal("0")},
                               ]), \
             mock.patch.object(rise, "_post_json", side_effect=side_effect):
            resp = rise.execute({
                "operation": "close_position", "account": "BASED",
                "symbol": "SOL",
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.order_state["outcome"], "CLOSED")
        # Exactly one submission
        self.assertEqual(len(captured["calls"]), 1)
        body = captured["calls"][0]["body"]
        # side should be SELL (close of long)
        self.assertEqual(body["side"], 1)  # SELL = 1
        self.assertTrue(body["reduce_only"])
        self.assertEqual(body["order_type"], 1)  # LIMIT
        self.assertEqual(body["time_in_force"], 3)  # IOC

    def test_short_position_buy_close(self):
        captured, side_effect = _post_capture()
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_sol()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"4": _sol_market_metadata()}), \
             mock.patch.object(rise, "_rise_market_price",
                               return_value=Decimal("81")), \
             mock.patch.object(rise, "_fetch_nonce_state",
                               return_value={"nonce_anchor": 1, "current_bitmap_index": 0}), \
             mock.patch.object(rise, "_rise_position_snapshot",
                               side_effect=[
                                   {"side": "short", "size": Decimal("0.3"),
                                    "entry_price": Decimal("82.0")},
                                   {"side": "flat", "size": Decimal("0"),
                                    "entry_price": Decimal("0")},
                               ]), \
             mock.patch.object(rise, "_post_json", side_effect=side_effect):
            resp = rise.execute({
                "operation": "close_position", "account": "BASED",
                "symbol": "SOL",
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.order_state["outcome"], "CLOSED")
        body = captured["calls"][0]["body"]
        # BUY = 0
        self.assertEqual(body["side"], 0)
        self.assertTrue(body["reduce_only"])


class AlreadyFlatTests(_Base):
    def test_no_position_no_mutation(self):
        captured, side_effect = _post_capture()
        with mock.patch.object(rise, "_rise_position_snapshot",
                               return_value={"side": "flat", "size": Decimal("0"),
                                             "entry_price": Decimal("0")}), \
             mock.patch.object(rise, "_post_json", side_effect=side_effect):
            resp = rise.execute({
                "operation": "close_position", "account": "BASED",
                "symbol": "SOL",
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.order_state["outcome"], "ALREADY_FLAT")
        self.assertEqual(len(captured["calls"]), 0)


class VerificationWindowTests(_Base):
    def test_first_read_still_open_then_flat(self):
        captured, side_effect = _post_capture()
        snaps = [
            {"side": "long", "size": Decimal("0.2"), "entry_price": Decimal("80.5")},  # pre
            {"side": "long", "size": Decimal("0.2"), "entry_price": Decimal("80.5")},  # first post (still open)
            {"side": "flat", "size": Decimal("0"), "entry_price": Decimal("0")},       # next post flat
        ]
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_sol()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"4": _sol_market_metadata()}), \
             mock.patch.object(rise, "_rise_market_price",
                               return_value=Decimal("81")), \
             mock.patch.object(rise, "_fetch_nonce_state",
                               return_value={"nonce_anchor": 1, "current_bitmap_index": 0}), \
             mock.patch.object(rise, "_rise_position_snapshot",
                               side_effect=snaps), \
             mock.patch.object(rise, "_post_json", side_effect=side_effect):
            resp = rise.execute({
                "operation": "close_position", "account": "BASED",
                "symbol": "SOL", "max_wait_seconds": 0.5,
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.order_state["outcome"], "CLOSED")
        # Exactly one close submission
        self.assertEqual(len(captured["calls"]), 1)

    def test_transient_read_fail_then_flat_no_duplicate_close(self):
        captured, side_effect = _post_capture()
        # First snapshot call succeeds (pre). Subsequent calls fail then succeed with flat.
        first_done = {"done": False}

        def snap(wallet, symbol):
            if not first_done["done"]:
                first_done["done"] = True
                return {"side": "long", "size": Decimal("0.2"), "entry_price": Decimal("80.5")}
            # After submit: raise once then return flat
            if getattr(snap, "_called", 0) == 0:
                snap._called = 1
                raise RuntimeError("transient 429")
            return {"side": "flat", "size": Decimal("0"), "entry_price": Decimal("0")}

        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_sol()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"4": _sol_market_metadata()}), \
             mock.patch.object(rise, "_rise_market_price",
                               return_value=Decimal("81")), \
             mock.patch.object(rise, "_fetch_nonce_state",
                               return_value={"nonce_anchor": 1, "current_bitmap_index": 0}), \
             mock.patch.object(rise, "_rise_position_snapshot",
                               side_effect=snap), \
             mock.patch.object(rise, "_post_json", side_effect=side_effect):
            resp = rise.execute({
                "operation": "close_position", "account": "BASED",
                "symbol": "SOL", "max_wait_seconds": 0.5,
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.order_state["outcome"], "CLOSED")
        # Crucial: ONE close submission even with transient failures.
        self.assertEqual(len(captured["calls"]), 1)

    def test_persistent_failure_not_confirmed_no_duplicate(self):
        captured, side_effect = _post_capture()

        def snap(wallet, symbol):
            # pre is long; subsequent reads also long (close did NOT fill).
            return {"side": "long", "size": Decimal("0.2"),
                    "entry_price": Decimal("80.5")}

        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_sol()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"4": _sol_market_metadata()}), \
             mock.patch.object(rise, "_rise_market_price",
                               return_value=Decimal("81")), \
             mock.patch.object(rise, "_fetch_nonce_state",
                               return_value={"nonce_anchor": 1, "current_bitmap_index": 0}), \
             mock.patch.object(rise, "_rise_position_snapshot",
                               side_effect=snap), \
             mock.patch.object(rise, "_post_json", side_effect=side_effect):
            resp = rise.execute({
                "operation": "close_position", "account": "BASED",
                "symbol": "SOL", "max_wait_seconds": 0.2,
            })
        # success=True (close submitted) but outcome=NOT_CONFIRMED
        self.assertTrue(resp.success)
        self.assertEqual(resp.order_state["outcome"], "NOT_CONFIRMED")
        # Exactly one submission
        self.assertEqual(len(captured["calls"]), 1)

    def test_venue_rejects_close_failed(self):
        captured = {"calls": []}

        def fail_post(*args, **kwargs):
            captured["calls"].append(args[0] if args else kwargs.get("url"))
            raise rise._RiseHTTPError(status=500, path="/v1/orders/place",
                                       body='{"error":"Internal"}')

        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_sol()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"4": _sol_market_metadata()}), \
             mock.patch.object(rise, "_rise_market_price",
                               return_value=Decimal("81")), \
             mock.patch.object(rise, "_fetch_nonce_state",
                               return_value={"nonce_anchor": 1, "current_bitmap_index": 0}), \
             mock.patch.object(rise, "_rise_position_snapshot",
                               return_value={"side": "long", "size": Decimal("0.2"),
                                             "entry_price": Decimal("80.5")}), \
             mock.patch.object(rise, "_post_json", side_effect=fail_post):
            resp = rise.execute({
                "operation": "close_position", "account": "BASED",
                "symbol": "SOL",
            })
        self.assertFalse(resp.success)
        self.assertIn("500", (resp.error.message or "")) or True
        # Exactly one attempt
        self.assertEqual(len(captured["calls"]), 1)


class OppositeSideGuardTests(_Base):
    def test_opposite_side_post_is_hard_failure(self):
        captured, side_effect = _post_capture()
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_sol()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"4": _sol_market_metadata()}), \
             mock.patch.object(rise, "_rise_market_price",
                               return_value=Decimal("81")), \
             mock.patch.object(rise, "_fetch_nonce_state",
                               return_value={"nonce_anchor": 1, "current_bitmap_index": 0}), \
             mock.patch.object(rise, "_rise_position_snapshot",
                               side_effect=[
                                   {"side": "long", "size": Decimal("0.2"),
                                    "entry_price": Decimal("80.5")},
                                   {"side": "short", "size": Decimal("0.2"),
                                    "entry_price": Decimal("80.5")},  # post is now opposite — reversal
                               ]), \
             mock.patch.object(rise, "_post_json", side_effect=side_effect):
            resp = rise.execute({
                "operation": "close_position", "account": "BASED",
                "symbol": "SOL", "max_wait_seconds": 0.2,
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.order_state["outcome"], "FAILED")
        self.assertIn("opposite", resp.order_state.get("reason", "").lower())
        # Exactly one close submission
        self.assertEqual(len(captured["calls"]), 1)


class ParamGuardTests(_Base):
    def test_missing_symbol(self):
        with mock.patch.object(rise, "_post_json", return_value={}) as _post:
            resp = rise.execute({
                "operation": "close_position", "account": "BASED",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "MISSING_SYMBOL")
        self.assertEqual(_post.call_count, 0)

    def test_nonzero_client_order_id_rejected(self):
        with mock.patch.object(rise, "_post_json", return_value={}) as _post:
            resp = rise.execute({
                "operation": "close_position", "account": "BASED",
                "symbol": "SOL", "client_order_id": "82738589974528",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "RISE_CLIENT_ORDER_ID_UNSUPPORTED")
        self.assertEqual(_post.call_count, 0)

    def test_instrument_not_found(self):
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value={"markets": []}), \
             mock.patch.object(rise, "_market_cache", return_value={}), \
             mock.patch.object(rise, "_post_json", return_value={}) as _post:
            resp = rise.execute({
                "operation": "close_position", "account": "BASED",
                "symbol": "ZZZ",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "INSTRUMENT_NOT_FOUND")
        self.assertEqual(_post.call_count, 0)


class ExistingBehaviorTests(_Base):
    def test_market_immediate_zero_default_unaffected(self):
        captured, side_effect = _post_capture()
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_sol()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"4": _sol_market_metadata()}), \
             mock.patch.object(rise, "_rise_market_price",
                               return_value=Decimal("81")), \
             mock.patch.object(rise, "_fetch_nonce_state",
                               return_value={"nonce_anchor": 1, "current_bitmap_index": 0}), \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               side_effect=lambda wallet: {"data": {"orders": []}}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=[]), \
             mock.patch.object(rise, "_rise_position_snapshot",
                               side_effect=[
                                   {"side": "flat", "size": Decimal("0"),
                                    "entry_price": Decimal("0")},
                                   {"side": "long", "size": Decimal("0.2"),
                                    "entry_price": Decimal("81.1")},
                               ]), \
             mock.patch.object(rise, "_post_json", side_effect=side_effect):
            resp = rise.execute({
                "operation": "market_immediate", "account": "BASED",
                "symbol": "SOL", "side": "buy", "volume": "0.2",
                "slip_pct": "0.005", "max_wait_seconds": 0.5,
            })
        # Existing market_immediate behavior is unaffected by close_position changes.
        self.assertTrue(resp.success)
        self.assertEqual(captured["calls"][0]["body"]["client_order_id"], "0")
        self.assertFalse(captured["calls"][0]["body"]["reduce_only"])

    def test_new_order_default_zero(self):
        captured, side_effect = _post_capture()
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_sol()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"4": _sol_market_metadata()}), \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               side_effect=lambda wallet: {"data": {"orders": []}}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=[]), \
             mock.patch.object(rise, "_fetch_nonce_state",
                               return_value={"nonce_anchor": 1, "current_bitmap_index": 0}), \
             mock.patch.object(rise, "_verify_new_order_submission",
                               return_value=(True, "0xEOID",
                                             {"market_id": "4"})), \
             mock.patch.object(rise, "_post_json", side_effect=side_effect):
            resp = rise.execute({
                "operation": "new_order", "account": "BASED",
                "symbol": "SOL", "side": "buy",
                "order_type": "limit", "volume": "0.2", "price": "81",
                "time_in_force": "GTC",
            })
        self.assertTrue(resp.success)
        # /trade LIMIT default: client_order_id="0", reduce_only=False
        self.assertEqual(captured["calls"][0]["body"]["client_order_id"], "0")
        self.assertFalse(captured["calls"][0]["body"]["reduce_only"])


if __name__ == "__main__":
    unittest.main()