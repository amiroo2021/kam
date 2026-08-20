"""close_position tests: Rise reduce-only IOC flatten."""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional, Callable
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


def _post_capture(close_order_id: str = "0xCLOSE1"):
    captured = {"calls": []}

    def side_effect(*args, **kwargs):
        body = kwargs.get("data") or (args[1] if len(args) > 1 else {}) or {}
        captured["calls"].append({"url": args[0] if args else kwargs.get("url"),
                                  "body": body})
        return {"data": {"order_id": close_order_id, "wide_order_id": "w1"}}

    return captured, side_effect


class _Base(unittest.TestCase):
    def setUp(self):
        self._cred = mock.patch.object(
            rise, "_lookup_credentials",
            return_value=("0x679fb6c74b531E3f3136DecbE9238cec6029F59A",
                          "0x" + ("11" * 32)),
        )
        self._cred.start()

    def tearDown(self):
        self._cred.stop()


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
                "operation": "close_position", "account": "BASED", "symbol": "SOL",
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.order_state["outcome"], "ALREADY_FLAT")
        # No submission at all.
        self.assertEqual(len(captured["calls"]), 0)


class VerificationWindowTests(_Base):
    def test_first_read_still_open_then_flat(self):
        captured, side_effect = _post_capture()
        snaps = [
            {"side": "long", "size": Decimal("0.2"), "entry_price": Decimal("80.5")},  # pre
            {"side": "long", "size": Decimal("0.2"), "entry_price": Decimal("80.5")},  # still open
            {"side": "flat", "size": Decimal("0"), "entry_price": Decimal("0")},       # flat
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
                "symbol": "SOL", "max_wait_seconds": 1.5,
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.order_state["outcome"], "CLOSED")
        self.assertEqual(len(captured["calls"]), 1)

    def test_transient_read_fail_then_flat_no_duplicate_close(self):
        captured, side_effect = _post_capture()
        first_done = {"done": False}

        def snap(wallet, symbol):
            if not first_done["done"]:
                first_done["done"] = True
                return {"side": "long", "size": Decimal("0.2"), "entry_price": Decimal("80.5")}
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
                "symbol": "SOL", "max_wait_seconds": 1.5,
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.order_state["outcome"], "CLOSED")
        self.assertEqual(len(captured["calls"]), 1)

    def test_persistent_failure_not_confirmed_no_duplicate(self):
        captured, side_effect = _post_capture()

        def snap(wallet, symbol):
            return {"side": "long", "size": Decimal("0.2"), "entry_price": Decimal("80.5")}

        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_sol()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"4": _sol_market_metadata()}), \
             mock.patch.object(rise, "_rise_market_price",
                               return_value=Decimal("81")), \
             mock.patch.object(rise, "_fetch_nonce_state",
                               return_value={"nonce_anchor": 1, "current_bitmap_index": 0}), \
             mock.patch.object(rise, "_rise_position_snapshot", side_effect=snap), \
             mock.patch.object(rise, "_post_json", side_effect=side_effect):
            resp = rise.execute({
                "operation": "close_position", "account": "BASED",
                "symbol": "SOL", "max_wait_seconds": 0.2,
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.order_state["outcome"], "NOT_CONFIRMED")
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
                "operation": "close_position", "account": "BASED", "symbol": "SOL",
            })
        self.assertFalse(resp.success)
        self.assertIn("500", (resp.error.message or ""))
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
                                   "entry_price": Decimal("80.5")},
                              ]), \
             mock.patch.object(rise, "_post_json", side_effect=side_effect):
            resp = rise.execute({
                "operation": "close_position", "account": "BASED", "symbol": "SOL",
                "max_wait_seconds": 0.2,
            })
        self.assertTrue(resp.success)  # submitted once, success container
        self.assertEqual(resp.order_state["outcome"], "FAILED")
        self.assertIn("opposite", resp.order_state.get("reason", "").lower())
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


class CloseSizeFreshnessTests(_Base):
    """Close must use the FRESH pre-read live position, not a stale/display size."""

    def test_uses_live_position_size_not_stale_displayed(self):
        captured, side_effect = _post_capture()
        # Caller passes a stale displayed size 0.1; live pre reads 0.2.
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
                "operation": "close_position", "account": "BASED", "symbol": "SOL",
                "volume": "0.1",  # stale/display — must NOT drive close size
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.order_state["outcome"], "CLOSED")
        body = captured["calls"][0]["body"]
        # submitted size_steps == live pre size / step = 0.2 / 0.001 = 200
        self.assertEqual(body["size_steps"], 200)
        self.assertEqual(body["side"], 1)  # SELL


class PartialCloseTests(_Base):
    def test_partial_remaining_position_reported(self):
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
                                  {"side": "long", "size": Decimal("0.05"),
                                   "entry_price": Decimal("80.5")},
                              ]), \
             mock.patch.object(rise, "_post_json", side_effect=side_effect):
            resp = rise.execute({
                "operation": "close_position", "account": "BASED",
                "symbol": "SOL", "max_wait_seconds": 0.3,
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.order_state["outcome"], "PARTIALLY_CLOSED")
        self.assertEqual(resp.order_state["pre_position_size"], "0.2")
        self.assertEqual(resp.order_state["post_position_size"], "0.05")
        self.assertEqual(len(captured["calls"]), 1)


class IOCNoMatchTests(_Base):
    def test_unchanged_returns_not_confirmed(self):
        # Close submitted but position unchanged (IOC no-match) => NOT_CONFIRMED.
        captured, side_effect = _post_capture()
        snaps = [{"side": "long", "size": Decimal("0.2"), "entry_price": Decimal("80.5")}]
        with mock.patch.object(rise, "_fetch_markets_payload",
                              return_value=_markets_sol()), \
             mock.patch.object(rise, "_market_cache",
                              return_value={"6880901": _sol_market_metadata()}), \
             mock.patch.object(rise, "_rise_market_price",
                              return_value=Decimal("81")), \
             mock.patch.object(rise, "_fetch_nonce_state",
                              return_value={"nonce_anchor": 1, "current_bitmap_index": 0}), \
             mock.patch.object(rise, "_rise_position_snapshot", side_effect=snaps), \
             mock.patch.object(rise, "_post_json", side_effect=side_effect):
            resp = rise.execute({
                "operation": "close_position", "account": "BASED",
                "symbol": "SOL", "max_wait_seconds": 0.2,
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.order_state["outcome"], "NOT_CONFIRMED")
        self.assertEqual(resp.order_state["post_position_size"], "0.2")
        self.assertEqual(len(captured["calls"]), 1)


class MalformedResponseTests(_Base):
    def test_malformed_submit_response_position_grounded(self):
        # A malformed/unparseable submit response must NOT be treated as a
        # definitive success; the outcome is grounded in the post-close
        # position read. Here the position flattens -> CLOSED regardless.
        captured = {"calls": []}

        def bad_post(*args, **kwargs):
            captured["calls"].append(1)
            return {"unexpected": "no data key"}

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
             mock.patch.object(rise, "_post_json", side_effect=bad_post):
            resp = rise.execute({
                "operation": "close_position", "account": "BASED", "symbol": "SOL",
                "max_wait_seconds": 1.5,
            })
        # Position flat => CLOSED (submitted exactly once).
        self.assertEqual(resp.order_state["outcome"], "CLOSED")
        self.assertEqual(len(captured["calls"]), 1)

    def test_malformed_submit_response_unchanged_not_confirmed(self):
        # Malformed response AND position stays non-flat => NOT_CONFIRMED.
        captured = {"calls": []}

        def bad_post(*args, **kwargs):
            captured["calls"].append(1)
            return {"unexpected": "no data key"}

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
             mock.patch.object(rise, "_post_json", side_effect=bad_post):
            resp = rise.execute({
                "operation": "close_position", "account": "BASED", "symbol": "SOL",
                "max_wait_seconds": 0.2,
            })
        self.assertEqual(resp.order_state["outcome"], "NOT_CONFIRMED")
        self.assertEqual(len(captured["calls"]), 1)


if __name__ == "__main__":
    unittest.main()