"""Phase 2 tests: Rise single-order cancel by exchange_order_id."""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest import mock

from plugins.trade.agents import x_rise_agent as rise


def _mk_open_row(order_id: str, market_id: str, side: str, side_int: int,
                 resting_order_id: int, sym: str = "HYPE"):
    return {
        "market_id": market_id,
        "side_int": side_int,
        "size_steps": 1,
        "price_ticks": 60000,
        "order_id": order_id,
        "resting_order_id": str(resting_order_id),
        "wide_order_id": "w" + str(resting_order_id),
        "symbol": sym,
        "side": side,
        "size": "0.01",
        "price": "62.300",
        "reduce_only": False,
        "post_only": False,
        "order_type": "limit",
        "time_in_force": "GTC",
        "price_precision": 3,
    }


def _target_row():
    return _mk_open_row(
        order_id="0xEOID", market_id="5", side="BUY", side_int=1,
        resting_order_id=100, sym="HYPE/USDC",
    )


def _markets_minimal():
    return {
        "markets": [
            {
                "market_id": "5",
                "config": {"name": "HYPE/USDC"},
                "display_name": "HYPE/USDC",
                "active": True,
            },
            {
                "market_id": "1",
                "config": {"name": "BTC/USDC"},
                "display_name": "BTC/USDC",
                "active": True,
            },
        ]
    }


def _btc_unrelated_row():
    return _mk_open_row(
        order_id="0xBTC_RELATED", market_id="1", side="SELL", side_int=1,
        resting_order_id=999, sym="BTC/USDC",
    )


class _Base(unittest.TestCase):
    def setUp(self):
        self._cred = mock.patch.object(
            rise, "_lookup_credentials",
            return_value=("0x" + "ab" * 20, "0x" + "11" * 32),
        )
        self._sleep = mock.patch.object(rise._t, "sleep", return_value=None)
        self._nonce = mock.patch.object(
            rise, "_fetch_nonce_state",
            return_value={"nonce_anchor": 1, "current_bitmap_index": 0},
        )
        self._cred.start(); self._sleep.start(); self._nonce.start()

    def tearDown(self):
        self._cred.stop(); self._sleep.stop(); self._nonce.stop()


def _wire_common_pre(matcher=None):
    """Wire common mocks: markets + pre-openOrders + post-openOrders identical
    unless caller provides matcher.

    Returns a context managers tuple; expected counts:
      - _post (mock for post /v1/orders/cancel) tracks call count.
    """
    target = _target_row()
    others = [_btc_unrelated_row()]
    return [
        mock.patch.object(rise, "_fetch_markets_payload", return_value=_markets_minimal()),
        mock.patch.object(rise, "_market_cache",
                          return_value={"5": target, "1": others[0]}),
        mock.patch.object(rise, "_rise_market_price",
                          return_value=Decimal("62")),
        mock.patch.object(rise, "_fetch_nonce_state",
                          return_value={"nonce_anchor": 1, "current_bitmap_index": 0}),
        mock.patch.object(rise, "_fetch_open_orders_payload",
                          return_value={"data": {"orders": [target] + others}}),
        mock.patch.object(rise, "_normalize_open_orders",
                          return_value=[target] + others),
    ]


def _ctx_active_pre_only():
    """Pre shows target active + unrelated."""
    target = _target_row()
    others = [_btc_unrelated_row()]
    pre_cms = [
        mock.patch.object(rise, "_fetch_markets_payload", return_value=_markets_minimal()),
        mock.patch.object(rise, "_market_cache",
                          return_value={"5": target, "1": others[0]}),
        mock.patch.object(rise, "_normalize_open_orders",
                          return_value=[target] + others),
        mock.patch.object(rise, "_fetch_open_orders_payload",
                          return_value={"data": {"orders": [target] + others}}),
    ]
    return pre_cms, target, others


class SingleCancelTests(_Base):
    # 1: target canceled, unrelated preserved → CANCELED
    def test_target_canceled_unrelated_preserved(self):
        target = _target_row()
        others = [_btc_unrelated_row()]
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_minimal()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"5": target, "1": others[0]}), \
             mock.patch.object(rise, "_normalize_open_orders",
                               side_effect=[[target] + others, others]), \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": [target] + others}}) as _get, \
             mock.patch.object(rise, "_post_json", return_value={}) as _post_json:
            resp = rise.execute({
                "operation": "cancel_order", "account": "BASED",
                "exchange_order_id": "0xEOID",
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.order_state["outcome"], "CANCELED")
        self.assertTrue(resp.order_state["unrelated_preserved"])
        self.assertEqual(resp.order_state["unrelated_count"], 1)
        # Exactly one cancel-body submission
        self.assertEqual(_post_json.call_count, 1)

    # 2: target canceled while unrelated order remains (BTC order untouched)
    def test_unrelated_btc_untouched(self):
        target = _target_row()
        others = [_btc_unrelated_row()]
        # After cancel, only target row removed; BTC remains.
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_minimal()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"5": target, "1": others[0]}), \
             mock.patch.object(rise, "_normalize_open_orders",
                               side_effect=[[target] + others, others]), \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": [target] + others}}), \
             mock.patch.object(rise, "_post_json", return_value={}):
            resp = rise.execute({
                "operation": "cancel_order", "account": "BASED",
                "exchange_order_id": "0xEOID",
            })
        self.assertEqual(resp.order_state["outcome"], "CANCELED")
        self.assertTrue(resp.order_state["unrelated_preserved"])

    # 3: two orders same symbol/side/price/size, only ONE id canceled.
    #     Risolve: caller provides distinct exchange_order_id; matcher picks target row.
    def test_only_target_id_canceled_not_ambiguous(self):
        target = _target_row()
        twin = _mk_open_row(
            order_id="0xTWIN", market_id="5", side="BUY", side_int=1,
            resting_order_id=101, sym="HYPE/USDC",
        )
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_minimal()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"5": target}), \
             mock.patch.object(rise, "_normalize_open_orders",
                               side_effect=[[target, twin], [twin]]), \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": [target, twin]}}), \
             mock.patch.object(rise, "_post_json", return_value={}):
            resp = rise.execute({
                "operation": "cancel_order", "account": "BASED",
                "exchange_order_id": "0xEOID",
            })
        self.assertEqual(resp.order_state["outcome"], "CANCELED")
        # We never submitted a cancel for the twin id
        self.assertNotIn("0xTWIN", str(resp))

    # 4: target already absent (pre did not contain target) → ALREADY_TERMINAL
    def test_target_already_canceled(self):
        others = [_btc_unrelated_row()]
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_minimal()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"5": _target_row(), "1": others[0]}), \
             mock.patch.object(rise, "_normalize_open_orders",
                               side_effect=[others]), \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": others}}), \
             mock.patch.object(rise, "_post_json", return_value={}) as _post_json:
            resp = rise.execute({
                "operation": "cancel_order", "account": "BASED",
                "exchange_order_id": "0xEOID",
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.order_state["outcome"], "ALREADY_TERMINAL")
        # We did not submit a cancel call
        self.assertEqual(_post_json.call_count, 0)

    # 5: target already filled = same path as 4 (absent in openOrders).
    # Already handled: ALREADY_TERMINAL.
    def test_target_already_filled_idempotent(self):
        others = [_btc_unrelated_row()]
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_minimal()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"5": _target_row(), "1": others[0]}), \
             mock.patch.object(rise, "_normalize_open_orders",
                               side_effect=[others]), \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": others}}), \
             mock.patch.object(rise, "_post_json", return_value={}):
            resp = rise.execute({
                "operation": "cancel_order", "account": "BASED",
                "exchange_order_id": "0xEOID",
            })
        self.assertEqual(resp.order_state["outcome"], "ALREADY_TERMINAL")

    # 6: unknown id handled safely (becomes ALREADY_TERMINAL).
    def test_unknown_id_safe(self):
        others = [_btc_unrelated_row()]
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_minimal()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"5": _target_row(), "1": others[0]}), \
             mock.patch.object(rise, "_normalize_open_orders",
                               side_effect=[others]), \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": others}}), \
             mock.patch.object(rise, "_post_json", return_value={}):
            resp = rise.execute({
                "operation": "cancel_order", "account": "BASED",
                "exchange_order_id": "0xDEADBEEFCAFE00000000000000000000",
            })
        self.assertEqual(resp.order_state["outcome"], "ALREADY_TERMINAL")

    # 7: HTTP failure → CANCEL_REJECTED, no broader cancellation
    def test_http_failure_no_broader_cancellation(self):
        target = _target_row()
        others = [_btc_unrelated_row()]
        # Custom POST that raises _RiseHTTPError
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_minimal()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"5": target, "1": others[0]}), \
             mock.patch.object(rise, "_normalize_open_orders",
                               side_effect=[[target] + others, [target] + others]), \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": [target] + others}}), \
             mock.patch.object(rise, "_post_json",
                               side_effect=rise._RiseHTTPError(
                                   status=400, path="/v1/orders/cancel",
                                   body='{"error":"bad"}')):
            resp = rise.execute({
                "operation": "cancel_order", "account": "BASED",
                "exchange_order_id": "0xEOID",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "CANCEL_REJECTED")
        # No state mutation beyond reporting
        self.assertEqual(resp.order_state["outcome"], "FAILED")

    # 8: post-confirm read fails → NOT_CONFIRMED
    def test_post_confirm_failure_not_confirmed(self):
        target = _target_row()
        others = [_btc_unrelated_row()]
        pre_cms = _ctx_active_pre_only()
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_minimal()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"5": target, "1": others[0]}), \
             mock.patch.object(rise, "_normalize_open_orders",
                               side_effect=[[target] + others]), \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               side_effect=[{"data": {"orders": [target] + others}},
                                            Exception("network blip")]), \
             mock.patch.object(rise, "_post_json", return_value={}):
            resp = rise.execute({
                "operation": "cancel_order", "account": "BASED",
                "exchange_order_id": "0xEOID",
            })
        # The POST happened, but we could not confirm; success="NOT_CONFIRMED"
        self.assertTrue(resp.success)
        self.assertEqual(resp.order_state["outcome"], "NOT_CONFIRMED")

    # 9: malformed exchange_order_id rejected before mutation
    def test_malformed_id_rejected_before_mutation(self):
        with mock.patch.object(rise, "_post_json", return_value={}) as _post_json:
            resp = rise.execute({
                "operation": "cancel_order", "account": "BASED",
                "exchange_order_id": "x;DROP TABLE",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "MALFORMED_ORDER_ID")
        self.assertEqual(_post_json.call_count, 0)

    # 9b: empty id rejected
    def test_empty_id_rejected(self):
        with mock.patch.object(rise, "_post_json", return_value={}) as _post_json:
            resp = rise.execute({
                "operation": "cancel_order", "account": "BASED",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "MISSING_ORDER_ID")
        self.assertEqual(_post_json.call_count, 0)

    # 10: exactly one cancellation submission per call
    def test_exactly_one_submission(self):
        target = _target_row()
        others = [_btc_unrelated_row()]
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_minimal()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"5": target, "1": others[0]}), \
             mock.patch.object(rise, "_normalize_open_orders",
                               side_effect=[[target] + others, others]), \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": [target] + others}}), \
             mock.patch.object(rise, "_post_json", return_value={}) as _post_json:
            rise.execute({
                "operation": "cancel_order", "account": "BASED",
                "exchange_order_id": "0xEOID",
            })
        self.assertEqual(_post_json.call_count, 1)

    # 11: existing /trade group-cancel NOT IMPLEMENTED stays
    #     sanity: existing group_cancel STILL DISPATCHES via the unchanged code path.
    def test_group_cancel_dispatch_unchanged(self):
        # The dispatch table should still call _execute_cancel_order_group.
        from plugins.trade.agents import x_rise_agent as rise_mod
        text = open(rise_mod.__file__, "r").read()
        self.assertIn(
            'if operation == "cancel_order_group":\n        return _execute_cancel_order_group(account, normalized_request)',
            text,
        )
        # And our new op dispatches to _execute_cancel_order
        self.assertIn(
            'if operation == "cancel_order":\n        return _execute_cancel_order(account, normalized_request)',
            text,
        )

    # 12: Phase 1 market_immediate tests remain green — covered by test_rise_market_immediate.py
    #     and test_rise_ioc_verifier.py. This file does not exercise them.

    # bonus: identity mismatch with supplied context_symbol
    def test_identity_mismatch_with_context_symbol(self):
        target = _target_row()  # HYPE/USDC
        others = [_btc_unrelated_row()]
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value=_markets_minimal()), \
             mock.patch.object(rise, "_market_cache",
                               return_value={"5": target, "1": others[0]}), \
             mock.patch.object(rise, "_normalize_open_orders",
                               return_value=[target] + others), \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": [target] + others}}), \
             mock.patch.object(rise, "_post_json", return_value={}) as _post_json:
            resp = rise.execute({
                "operation": "cancel_order", "account": "BASED",
                "exchange_order_id": "0xEOID",
                "symbol": "BTC",  # wrong context — should reject
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "IDENTITY_MISMATCH")
        self.assertEqual(_post_json.call_count, 0)


if __name__ == "__main__":
    unittest.main()
