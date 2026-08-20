"""Phase 1 offline tests: Rise market_immediate bounded-limit IOC fill."""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest import mock

from plugins.trade.agents import x_rise_agent as rise
from plugins.trade.canonical import make_success


def _patch_creds_and_time(rise_mod, *, force_no_sleep: bool = True):
    if force_no_sleep:
        # Force no real waiting
        return mock.patch.object(rise_mod._t, "sleep", return_value=None), mock.patch.object(
            rise_mod, "_fetch_nonce_state",
            return_value={"nonce_anchor": 1, "current_bitmap_index": 0},
        )
    return mock.patch.object(
        rise_mod, "_fetch_nonce_state",
        return_value={"nonce_anchor": 1, "current_bitmap_index": 0},
    )


def _install_static_portfolio(seq):
    counter = {"i": 0}

    def snap(wallet, symbol):
        i = counter["i"]
        counter["i"] += 1
        if i >= len(seq):
            return seq[-1]
        return seq[i]

    return mock.patch.object(rise, "_rise_position_snapshot", side_effect=snap)


def _market(market_id: int = 1, step_price: str = "0.01", step_size: str = "0.001", min_size: str = "0.001"):
    return {
        "market_id": market_id,
        "symbol": "BTC",
        "step_price": step_price,
        "step_size": step_size,
        "min_order_size": min_size,
        "last_traded_price": "100",
    }


def _passport_return(matched: bool = True, verified_eoid: Optional[str] = "0xabc"):
    sm = mock.Mock()
    sm.return_value = (matched, verified_eoid, {"order_id": verified_eoid, "side_int": 1, "market_id": "1"} if matched else None)
    return sm


def _fake_submit_response(*, eoid: str = "0xabc"):
    order = mock.Mock()
    order.exchange_order_id = eoid
    resp = mock.Mock()
    resp.success = True
    resp.order = order
    return resp, {}, Decimal("0.2"), Decimal("100.5")


class MarketImmediateTests(unittest.TestCase):
    def setUp(self):
        # Stub credentials without requiring eth_account
        self._cred_patcher = mock.patch.object(
            rise, "_lookup_credentials", return_value=("0x" + "ab" * 20, "0x" + "11" * 32)
        )
        self._cred_patcher.start()
        # Stop real time.sleep in polling
        self._sleep_patcher = mock.patch.object(rise._t, "sleep", return_value=None)
        self._sleep_patcher.start()
        # Stable nonce
        self._nonce_patcher = mock.patch.object(
            rise, "_fetch_nonce_state",
            return_value={"nonce_anchor": 1, "current_bitmap_index": 0},
        )
        self._nonce_patcher.start()

    def tearDown(self):
        self._cred_patcher.stop()
        self._sleep_patcher.stop()
        self._nonce_patcher.stop()

    def test_successful_fill_uses_position_avg_entry_price(self):
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value={"markets": [_market()]}), \
             mock.patch.object(rise, "_market_cache", return_value={"1": _market()}), \
             mock.patch.object(rise, "_resolve_market_by_symbol", return_value=_market()), \
             mock.patch.object(rise, "_rise_market_price", return_value=Decimal("100")), \
             mock.patch.object(rise, "_submit_rise_limit_order",
                               return_value=_fake_submit_response(eoid="0xabc")), \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": []}}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=[]), \
             _install_static_portfolio([
                 {"side": "flat", "size": Decimal("0"), "entry_price": Decimal("0")},
                 {"side": "long", "size": Decimal("0.2"), "entry_price": Decimal("100.5")},
             ]):
            resp = rise.execute({
                "operation": "market_immediate",
                "account": "test",
                "symbol": "BTC",
                "side": "buy",
                "volume": "0.2",
                "slip_pct": "0.02",
                "max_wait_seconds": 1.0,
            })
        self.assertTrue(resp.success)
        # Result must come from real confirmation (not invented)
        self.assertEqual(resp.order.side, "long")
        self.assertEqual(resp.order_state["fill_price"], "100.5")
        # Status must be filled, verified
        self.assertEqual(resp.order.status, "filled")
        self.assertTrue(resp.order.verified)
        self.assertEqual(resp.order.exchange_order_id, "0xabc")

    def test_no_invented_fill_when_order_still_resting(self):
        # Order keeps appearing in openOrders ⇒ NEVER declared filled.
        resting = [{
            "market_id": "1",
            "side_int": 1,
            "size_steps": 200,
            "price_ticks": 10010,
            "order_id": "0xabc",
            "resting_order_id": "4717",
            "wide_order_id": "9435",
            "symbol": "BTC",
            "side": "BUY",
            "size": "0.2",
            "price": "100.10",
            "reduce_only": False,
            "post_only": False,
            "order_type": "limit",
            "time_in_force": "IOC",
            "price_precision": 2,
        }]
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value={"markets": [_market()]}), \
             mock.patch.object(rise, "_market_cache", return_value={"1": _market()}), \
             mock.patch.object(rise, "_resolve_market_by_symbol", return_value=_market()), \
             mock.patch.object(rise, "_rise_market_price", return_value=Decimal("100")), \
             mock.patch.object(rise, "_submit_rise_limit_order",
                               return_value=_fake_submit_response(eoid="0xabc")), \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": resting}}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=resting), \
             _install_static_portfolio([
                 {"side": "flat", "size": Decimal("0"), "entry_price": Decimal("0")},
             ]):
            resp = rise.execute({
                "operation": "market_immediate",
                "account": "test",
                "symbol": "BTC",
                "side": "buy",
                "volume": "0.2",
                "slip_pct": "0.02",
                "max_wait_seconds": 0.5,
            })
        self.assertFalse(resp.success)
        # Specifically FILL_NOT_CONFIRMED, never "filled"
        self.assertEqual(resp.error.code, "FILL_NOT_CONFIRMED")

    def test_no_invented_fill_when_side_mismatch(self):
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value={"markets": [_market()]}), \
             mock.patch.object(rise, "_market_cache", return_value={"1": _market()}), \
             mock.patch.object(rise, "_resolve_market_by_symbol", return_value=_market()), \
             mock.patch.object(rise, "_rise_market_price", return_value=Decimal("100")), \
             mock.patch.object(rise, "_submit_rise_limit_order",
                               return_value=_fake_submit_response(eoid="0xabc")), \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": []}}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=[]), \
             _install_static_portfolio([
                 {"side": "flat", "size": Decimal("0"), "entry_price": Decimal("0")},
                 {"side": "short", "size": Decimal("0.2"), "entry_price": Decimal("100.5")},
             ]):
            resp = rise.execute({
                "operation": "market_immediate",
                "account": "test",
                "symbol": "BTC",
                "side": "buy",
                "volume": "0.2",
                "max_wait_seconds": 0.2,
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "FILL_SIDE_MISMATCH")

    def test_mark_unavailable_blocks_immediate(self):
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value={"markets": [_market()]}), \
             mock.patch.object(rise, "_market_cache", return_value={"1": _market()}), \
             mock.patch.object(rise, "_resolve_market_by_symbol", return_value=_market()), \
             mock.patch.object(rise, "_rise_market_price", return_value=Decimal("0")):
            resp = rise.execute({
                "operation": "market_immediate",
                "account": "test",
                "symbol": "BTC",
                "side": "buy",
                "volume": "0.2",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "MARK_PRICE_UNAVAILABLE")

    def test_volume_below_min_rejected_before_submission(self):
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value={"markets": [_market(min_size="1.0")]}), \
             mock.patch.object(rise, "_market_cache", return_value={"1": _market(min_size="1.0")}), \
             mock.patch.object(rise, "_resolve_market_by_symbol", return_value=_market(min_size="1.0")), \
             mock.patch.object(rise, "_rise_market_price", return_value=Decimal("100")):
            resp = rise.execute({
                "operation": "market_immediate",
                "account": "test",
                "symbol": "BTC",
                "side": "buy",
                "volume": "0.2",
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "INVALID_VOLUME")

    def test_invalid_slip_rejected(self):
        with mock.patch.object(rise, "_lookup_credentials",
                               return_value=("0x" + "ab" * 20, "0x" + "11" * 32)), \
             mock.patch.object(rise, "_fetch_markets_payload",
                               return_value={"markets": [_market()]}), \
             mock.patch.object(rise, "_market_cache", return_value={"1": _market()}), \
             mock.patch.object(rise, "_resolve_market_by_symbol", return_value=_market()):
            resp = rise.execute({
                "operation": "market_immediate",
                "account": "test",
                "symbol": "BTC",
                "side": "buy",
                "volume": "0.2",
                "slip_pct": "0",
            })
            self.assertFalse(resp.success)
            self.assertEqual(resp.error.code, "INVALID_SLIP_PCT")

    def test_existing_new_order_market_still_rejected(self):
        # Sanity: we did NOT widen the new_order path. MARKET still rejected.
        with mock.patch.object(rise, "_lookup_credentials", return_value=("0xwallet", "0x" + "11"*32)):
            r = rise.execute({
                "operation": "new_order", "account": "test",
                "symbol": "BTC", "side": "buy",
                "order_type": "market", "volume": "0.2", "price": "100",
            })
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "INVALID_ORDER_TYPE")

    def test_unrelated_symbols_never_matched(self):
        # Position belongs to ETH, BUY was requested on BTC.
        with mock.patch.object(rise, "_fetch_markets_payload",
                               return_value={"markets": [_market()]}), \
             mock.patch.object(rise, "_market_cache", return_value={"1": _market()}), \
             mock.patch.object(rise, "_resolve_market_by_symbol", return_value=_market()), \
             mock.patch.object(rise, "_rise_market_price", return_value=Decimal("100")), \
             mock.patch.object(rise, "_submit_rise_limit_order",
                               return_value=_fake_submit_response(eoid="0xabc")), \
             mock.patch.object(rise, "_fetch_open_orders_payload",
                               return_value={"data": {"orders": []}}), \
             mock.patch.object(rise, "_normalize_open_orders", return_value=[]), \
             _install_static_portfolio([
                 {"side": "flat", "size": Decimal("0"), "entry_price": Decimal("0")},
                 {"side": "flat", "size": Decimal("0"), "entry_price": Decimal("0")},
             ]):
            resp = rise.execute({
                "operation": "market_immediate",
                "account": "test",
                "symbol": "BTC",
                "side": "buy",
                "volume": "0.2",
                "max_wait_seconds": 0.1,
            })
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "FILL_NOT_CONFIRMED")


if __name__ == "__main__":
    unittest.main()
