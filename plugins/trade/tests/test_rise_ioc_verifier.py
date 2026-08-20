"""Phase 1 verifier-fix tests: pre/post position delta + raw order_id survival."""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest import mock

from plugins.trade.agents import x_rise_agent as rise


def _mk_market():
    return {
        "market_id": 5,
        "symbol": "HYPE",
        "step_price": "0.001",
        "step_size": "0.01",
        "min_order_size": "0.01",
        "active": True,
    }


def _mk_submit_ok(eoid="0xc0000024ef00000000000000000006f"):
    order = mock.Mock()
    order.exchange_order_id = eoid
    resp = mock.Mock()
    resp.success = True
    resp.order = order
    return resp, {}, Decimal("0.02"), Decimal("62.315")


class _StubPortfolio:
    def __init__(self, seq: List[Dict[str, Any]]):
        self.seq = seq
        self.idx = 0

    def pop(self):
        if self.idx >= len(self.seq):
            self.idx = len(self.seq)
            return None
        v = self.seq[self.idx]
        self.idx += 1
        return v


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


def _install_static_portfolio(rows):
    """Make _rise_position_snapshot deterministic across calls."""
    counter = {"i": 0}

    def snap(wallet, symbol):
        i = counter["i"]
        counter["i"] += 1
        if i >= len(rows):
            return rows[-1]
        return rows[i]

    return mock.patch.object(rise, "_rise_position_snapshot", side_effect=snap)


class IOCVerificationTests(_Base):
    # 1: IOC fills instantly + order disappears + position delta confirms full fill → SUCCESS
    def test_ioc_fills_and_order_disappears_full_fill(self):
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value={"markets": [_mk_market()]}), \
             mock.patch.object(rise, "_market_cache", return_value={"5": _mk_market()}), \
             mock.patch.object(rise, "_resolve_market_by_symbol", return_value=_mk_market()), \
             mock.patch.object(rise, "_rise_market_price",
                               return_value=Decimal("62")), \
             mock.patch.object(rise, "_submit_rise_limit_order",
                               return_value=_mk_submit_ok(eoid="0xEOID")) as _subm, \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": []}}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=[]), \
             _install_static_portfolio([
                 {"side": "flat", "size": Decimal("0"), "entry_price": Decimal("0")},
                 {"side": "long", "size": Decimal("0.02"), "entry_price": Decimal("62.138")},
             ]):
            resp = rise.execute({
                "operation": "market_immediate", "account": "BASED",
                "symbol": "HYPE", "side": "buy", "volume": "0.02",
                "slip_pct": "0.005", "max_wait_seconds": 0.5,
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.order.status, "filled")
        # 2: Raw exchange_order_id survives into result.
        self.assertEqual(resp.order.exchange_order_id, "0xEOID")
        self.assertEqual(resp.order_state["fill_price"], "62.138")
        self.assertEqual(resp.order_state["delta_size"], "0.02")

    # 3: IOC disappears + no position delta → FILL_NOT_CONFIRMED
    def test_disappear_no_delta(self):
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value={"markets": [_mk_market()]}), \
             mock.patch.object(rise, "_market_cache", return_value={"5": _mk_market()}), \
             mock.patch.object(rise, "_resolve_market_by_symbol", return_value=_mk_market()), \
             mock.patch.object(rise, "_rise_market_price",
                               return_value=Decimal("62")), \
             mock.patch.object(rise, "_submit_rise_limit_order",
                               return_value=_mk_submit_ok(eoid="0xEOID")) as _subm, \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": []}}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=[]), \
             _install_static_portfolio([
                 {"side": "flat", "size": Decimal("0"), "entry_price": Decimal("0")},
                 {"side": "flat", "size": Decimal("0"), "entry_price": Decimal("0")},
             ]):
            resp = rise.execute({
                "operation": "market_immediate", "account": "BASED",
                "symbol": "HYPE", "side": "buy", "volume": "0.02",
                "slip_pct": "0.005", "max_wait_seconds": 0.2,
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "FILL_NOT_CONFIRMED")
        # Order id preserved even when not filled.
        self.assertEqual(resp.order_state["exchange_order_id"], "0xEOID")
        self.assertEqual(resp.order_state["delta_size"], "0")

    # 4: partial position delta below threshold → no retry
    def test_partial_below_threshold(self):
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value={"markets": [_mk_market()]}), \
             mock.patch.object(rise, "_market_cache", return_value={"5": _mk_market()}), \
             mock.patch.object(rise, "_resolve_market_by_symbol", return_value=_mk_market()), \
             mock.patch.object(rise, "_rise_market_price",
                               return_value=Decimal("62")), \
             mock.patch.object(rise, "_submit_rise_limit_order",
                               return_value=_mk_submit_ok(eoid="0xEOID")) as _subm, \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": []}}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=[]), \
             _install_static_portfolio([
                 {"side": "flat", "size": Decimal("0"), "entry_price": Decimal("0")},
                 # Only ~0.005 filled (25% of 0.02); threshold default 95% => fail
                 {"side": "long", "size": Decimal("0.005"), "entry_price": Decimal("62.13")},
             ]):
            resp = rise.execute({
                "operation": "market_immediate", "account": "BASED",
                "symbol": "HYPE", "side": "buy", "volume": "0.02",
                "slip_pct": "0.005", "max_wait_seconds": 0.2,
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "FILL_NOT_CONFIRMED")
        # Did NOT submit a second order (mock submit call count = 1)
        self.assertEqual(_subm.call_count, 1)

    # 5: existing same-side long BEFORE submit does NOT falsely verify a new IOC
    #     unless post-submit delta confirms new fill
    def test_existing_same_side_no_false_pass(self):
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value={"markets": [_mk_market()]}), \
             mock.patch.object(rise, "_market_cache", return_value={"5": _mk_market()}), \
             mock.patch.object(rise, "_resolve_market_by_symbol", return_value=_mk_market()), \
             mock.patch.object(rise, "_rise_market_price",
                               return_value=Decimal("62")), \
             mock.patch.object(rise, "_submit_rise_limit_order",
                               return_value=_mk_submit_ok(eoid="0xEOID")) as _subm, \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": []}}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=[]), \
             _install_static_portfolio([
                 {"side": "long", "size": Decimal("0.5"), "entry_price": Decimal("61.5")},
                 {"side": "long", "size": Decimal("0.5"), "entry_price": Decimal("61.5")},
             ]):
            resp = rise.execute({
                "operation": "market_immediate", "account": "BASED",
                "symbol": "HYPE", "side": "buy", "volume": "0.02",
                "slip_pct": "0.005", "max_wait_seconds": 0.2,
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "FILL_NOT_CONFIRMED")
        # No retry submitted
        self.assertEqual(_subm.call_count, 1)

    # 6: opposite-side position post-submit → hard fail FILL_SIDE_MISMATCH
    def test_opposite_side_is_hard_fail(self):
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value={"markets": [_mk_market()]}), \
             mock.patch.object(rise, "_market_cache", return_value={"5": _mk_market()}), \
             mock.patch.object(rise, "_resolve_market_by_symbol", return_value=_mk_market()), \
             mock.patch.object(rise, "_rise_market_price",
                               return_value=Decimal("62")), \
             mock.patch.object(rise, "_submit_rise_limit_order",
                               return_value=_mk_submit_ok(eoid="0xEOID")) as _subm, \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": []}}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=[]), \
             _install_static_portfolio([
                 {"side": "flat", "size": Decimal("0"), "entry_price": Decimal("0")},
                 # OOPS: post is short when buy requested (must never happen)
                 {"side": "short", "size": Decimal("0.02"), "entry_price": Decimal("62.5")},
             ]):
            resp = rise.execute({
                "operation": "market_immediate", "account": "BASED",
                "symbol": "HYPE", "side": "buy", "volume": "0.02",
                "slip_pct": "0.005", "max_wait_seconds": 0.2,
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "FILL_SIDE_MISMATCH")

    # 7: avg_entry_price OUTSIDE slip bound (BUY side, fill_price > bound) → fail
    def test_avg_entry_price_above_buy_bound(self):
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value={"markets": [_mk_market()]}), \
             mock.patch.object(rise, "_market_cache", return_value={"5": _mk_market()}), \
             mock.patch.object(rise, "_resolve_market_by_symbol", return_value=_mk_market()), \
             mock.patch.object(rise, "_rise_market_price",
                               return_value=Decimal("62")), \
             mock.patch.object(rise, "_submit_rise_limit_order",
                               return_value=_mk_submit_ok(eoid="0xEOID")) as _subm, \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": []}}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=[]), \
             _install_static_portfolio([
                 {"side": "flat", "size": Decimal("0"), "entry_price": Decimal("0")},
                 {"side": "long", "size": Decimal("0.02"), "entry_price": Decimal("63.5")},
             ]):
            resp = rise.execute({
                "operation": "market_immediate", "account": "BASED",
                "symbol": "HYPE", "side": "buy", "volume": "0.02",
                "slip_pct": "0.005", "max_wait_seconds": 0.2,
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "FILL_PRICE_OUT_OF_SLIP")

    # 8: avg_entry_price INSIDE bound → success
    def test_avg_entry_price_inside_bound_success(self):
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value={"markets": [_mk_market()]}), \
             mock.patch.object(rise, "_market_cache", return_value={"5": _mk_market()}), \
             mock.patch.object(rise, "_resolve_market_by_symbol", return_value=_mk_market()), \
             mock.patch.object(rise, "_rise_market_price",
                               return_value=Decimal("62")), \
             mock.patch.object(rise, "_submit_rise_limit_order",
                               return_value=_mk_submit_ok(eoid="0xEOID")) as _subm, \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": []}}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=[]), \
             _install_static_portfolio([
                 {"side": "flat", "size": Decimal("0"), "entry_price": Decimal("0")},
                 {"side": "long", "size": Decimal("0.02"), "entry_price": Decimal("62.10")},
             ]):
            resp = rise.execute({
                "operation": "market_immediate", "account": "BASED",
                "symbol": "HYPE", "side": "buy", "volume": "0.02",
                "slip_pct": "0.005", "max_wait_seconds": 0.2,
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.order.exchange_order_id, "0xEOID")

    # 9: no duplicate submission across fills
    def test_no_duplicate_submission_on_fill(self):
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value={"markets": [_mk_market()]}), \
             mock.patch.object(rise, "_market_cache", return_value={"5": _mk_market()}), \
             mock.patch.object(rise, "_resolve_market_by_symbol", return_value=_mk_market()), \
             mock.patch.object(rise, "_rise_market_price",
                               return_value=Decimal("62")), \
             mock.patch.object(rise, "_submit_rise_limit_order",
                               return_value=_mk_submit_ok(eoid="0xEOID")) as _subm, \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": []}}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=[]), \
             _install_static_portfolio([
                 {"side": "flat", "size": Decimal("0"), "entry_price": Decimal("0")},
                 {"side": "long", "size": Decimal("0.02"), "entry_price": Decimal("62.10")},
             ]):
            resp = rise.execute({
                "operation": "market_immediate", "account": "BASED",
                "symbol": "HYPE", "side": "buy", "volume": "0.02",
                "slip_pct": "0.005", "max_wait_seconds": 0.2,
            })
        self.assertTrue(resp.success)
        self.assertEqual(_subm.call_count, 1)

    # 10: existing /trade Arcus LIMIT verification path remains unchanged
    #     (sanity: new_order op with order_type=market still rejected without
    #     enabling market_immediate op)
    def test_new_order_market_still_rejected(self):
        with mock.patch.object(rise, "_lookup_credentials",
                               return_value=("0x"+"ab"*20, "0x"+"11"*32)):
            r = rise.execute({
                "operation": "new_order", "account": "BASED",
                "symbol": "HYPE", "side": "buy",
                "order_type": "market", "volume": "0.02", "price": "62",
                "time_in_force": "GTC",
            })
        self.assertFalse(r.success)
        # MARKET still rejected at *new_order op* (the market_immediate op is the
        # only path that builds an IOC; we did NOT widen the new_order path).
        self.assertEqual(r.error.code, "INVALID_ORDER_TYPE")


if __name__ == "__main__":
    unittest.main()
