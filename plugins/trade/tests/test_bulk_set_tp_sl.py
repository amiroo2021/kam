"""Regression tests for Bulk's native set_tp / set_sl support.

Bulk previously returned NOT_IMPLEMENTED for set_tp/set_sl. This activates
them as native conditional orders (tp/st action tags; see
docs.bulk.trade/bulk-exchange/conditional-orders) using the same flow and
conventions as the other exchange agents:

  * price == 0  -> removes the existing protective order (cancel), success
                   either way (idempotent), never an error.
  * price  > 0  -> places (or atomically cancel+replaces) a reduce-only
                   conditional order sized to the live position.
  * the live position size must resolve to a strictly positive number;
    it is NEVER replaced with a stale/display size, and a size of 0
    fails loudly with POSITION_SIZE_UNRESOLVED (never silently defaulted).
  * trigger direction and reduce-only closing side are correct for both
    LONG and SHORT.
"""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

bulk = importlib.import_module("plugins.trade.agents.x_bulk_agent")

CREDS = {
    "account": "main",
    "account_pubkey": "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7",
    "agent_private_key": "secret-main",
    "base_url": bulk.DEFAULT_API_BASE,
}
MARKET = {"symbol": "BTC-USD", "sizeIncrement": 0.01, "sizeDecimals": 2}
RESTING = {"response": {"data": {"statuses": [{"resting": {"oid": "new-oid"}}]}}}
FILLED_CANCEL = {"response": {"data": {"statuses": [{"cancelled": {"oid": "old-oid"}}]}}}


def _account(size: float, *, orders=None, iso: bool = False) -> dict:
    return {
        "positions": [{"symbol": "BTC-USD", "size": size, "iso": iso}],
        "openOrders": orders or [],
    }


def _tp_order(oid: str, price: float, size: float, *, iso: bool = False) -> dict:
    return {"symbol": "BTC-USD", "orderId": oid, "price": price, "size": size,
            "reduceOnly": True, "orderType": "takeProfit", "iso": iso}


def _sl_order(oid: str, price: float, size: float, *, iso: bool = False) -> dict:
    return {"symbol": "BTC-USD", "orderId": oid, "price": price, "size": size,
            "reduceOnly": True, "orderType": "stop", "iso": iso}


class TestBulkSetTpSl(unittest.TestCase):
    def setUp(self) -> None:
        with bulk._protection_guard_lock:
            bulk._protection_guard.clear()
        with bulk._close_guard_lock:
            bulk._close_guard.clear()

    # -- capability activation ------------------------------------------------

    def test_set_tp_set_sl_now_advertised_in_capabilities(self) -> None:
        caps = bulk.capabilities()
        self.assertIn("set_tp", caps)
        self.assertIn("set_sl", caps)

    # -- the displayed=15.17 / fetched=0.00 shape, reused for Bulk ------------

    def test_set_tp_fails_loudly_never_substitutes_stale_size_when_fetched_is_zero(self) -> None:
        # Bulk's _position_rows already filters size==0 rows out of the
        # positions array (same guarantee Apex's display filter gives) --
        # so a zero fetched size surfaces as POSITION_NOT_FOUND, not a
        # live row with size 0. Either way, the displayed/stale size
        # ("15.17" from the wizard) must never be substituted.
        live = _account(0.0)
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", return_value=live), \
             mock.patch.object(bulk, "_submit_order") as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "main",
                                  "symbol": "BTC", "price": "70000", "size": "15.17"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "POSITION_NOT_FOUND")
        submit.assert_not_called()

    def test_set_sl_fails_loudly_never_substitutes_stale_size_when_fetched_is_zero(self) -> None:
        live = _account(0.0)
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", return_value=live), \
             mock.patch.object(bulk, "_submit_order") as submit:
            resp = bulk.execute({"operation": "set_sl", "exchange": "bulk", "account": "main",
                                  "symbol": "BTC", "price": "60000", "size": "15.17"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "POSITION_NOT_FOUND")
        submit.assert_not_called()

    def test_set_tp_hard_guard_never_fires_price_0_with_size_0_position_row(self) -> None:
        # Direct unit-level exercise of the size==0 hard guard inside
        # _bulk_set_protection, bypassing _position_rows' own filter, to
        # prove the guard itself never substitutes or defaults the size.
        live = {"positions": [{"symbol": "BTC-USD", "size": 0.0, "iso": False}], "openOrders": []}
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", return_value={}), \
             mock.patch.object(bulk, "_validate_close_snapshot", return_value=live), \
             mock.patch.object(bulk, "_position_rows", return_value=live["positions"]), \
             mock.patch.object(bulk, "_submit_order") as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "main",
                                  "symbol": "BTC", "price": "70000", "size": "15.17"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "POSITION_SIZE_UNRESOLVED")
        self.assertEqual(resp.data["fetched_size"], "0")
        submit.assert_not_called()

    # -- place TP: LONG and SHORT direction correctness -----------------------

    def test_set_tp_long_places_above_trigger_reduce_only_sell_shape(self) -> None:
        live = _account(0.5)  # long
        after = _account(0.5, orders=[_tp_order("new-oid", 70000.0, 0.5)])
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", side_effect=[live, after]), \
             mock.patch.object(bulk, "_submit_order", return_value=RESTING) as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "main",
                                  "symbol": "BTC", "price": "70000"})
        self.assertTrue(resp.success, msg=str(resp))
        self.assertEqual(resp.data["protection_status"], "set")
        self.assertEqual(resp.data["price"], "70000")
        actions = submit.call_args[0][1]
        self.assertEqual(len(actions), 1)
        tp = actions[0]["tp"]
        self.assertEqual(tp["c"], "BTC-USD")
        self.assertTrue(tp["d"])  # TP triggers above for longs
        self.assertEqual(tp["sz"], 0.5)
        self.assertEqual(tp["tr"], 70000.0)
        self.assertIsNone(tp["lim"])
        self.assertFalse(tp["i"])

    def test_set_tp_short_places_below_trigger(self) -> None:
        live = _account(-0.5)  # short
        after = _account(-0.5, orders=[_tp_order("new-oid", 1400.0, 0.5)])
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", side_effect=[live, after]), \
             mock.patch.object(bulk, "_submit_order", return_value=RESTING) as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "main",
                                  "symbol": "ETH-USDT", "price": "1400"})
        self.assertTrue(resp.success, msg=str(resp))
        tp = submit.call_args[0][1][0]["tp"]
        self.assertFalse(tp["d"])  # TP triggers below for shorts

    def test_set_sl_long_places_below_trigger(self) -> None:
        live = _account(0.5)  # long
        after = _account(0.5, orders=[_sl_order("new-oid", 90000.0, 0.5)])
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", side_effect=[live, after]), \
             mock.patch.object(bulk, "_submit_order", return_value=RESTING) as submit:
            resp = bulk.execute({"operation": "set_sl", "exchange": "bulk", "account": "main",
                                  "symbol": "BTC", "price": "90000"})
        self.assertTrue(resp.success, msg=str(resp))
        st = submit.call_args[0][1][0]["st"]
        self.assertFalse(st["d"])  # Stop triggers below for longs

    def test_set_sl_short_places_above_trigger(self) -> None:
        live = _account(-0.5)  # short
        after = _account(-0.5, orders=[_sl_order("new-oid", 1500.0, 0.5)])
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", side_effect=[live, after]), \
             mock.patch.object(bulk, "_submit_order", return_value=RESTING) as submit:
            resp = bulk.execute({"operation": "set_sl", "exchange": "bulk", "account": "main",
                                  "symbol": "ETH-USDT", "price": "1500"})
        self.assertTrue(resp.success, msg=str(resp))
        st = submit.call_args[0][1][0]["st"]
        self.assertTrue(st["d"])  # Stop triggers above for shorts

    # -- replace an existing protective order (cancel + place atomically) ----

    def test_set_tp_replaces_existing_resting_tp_with_cancel_plus_place(self) -> None:
        live = _account(0.5, orders=[_tp_order("old-oid", 68000.0, 0.5)])
        after = _account(0.5, orders=[_tp_order("new-oid", 70000.0, 0.5)])
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", side_effect=[live, after]), \
             mock.patch.object(bulk, "_submit_order", return_value=RESTING) as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "main",
                                  "symbol": "BTC", "price": "70000"})
        self.assertTrue(resp.success, msg=str(resp))
        actions = submit.call_args[0][1]
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0]["cx"], {"c": "BTC-USD", "oid": "old-oid"})
        self.assertEqual(actions[1]["tp"]["tr"], 70000.0)

    def test_set_sl_does_not_touch_an_unrelated_tp_order_on_the_same_symbol(self) -> None:
        live = _account(0.5, orders=[_tp_order("tp-oid", 90000.0, 0.5)])
        after = _account(0.5, orders=[_tp_order("tp-oid", 90000.0, 0.5), _sl_order("sl-oid", 80000.0, 0.5)])
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", side_effect=[live, after]), \
             mock.patch.object(bulk, "_submit_order", return_value=RESTING) as submit:
            resp = bulk.execute({"operation": "set_sl", "exchange": "bulk", "account": "main",
                                  "symbol": "BTC", "price": "80000"})
        self.assertTrue(resp.success, msg=str(resp))
        actions = submit.call_args[0][1]
        self.assertEqual(len(actions), 1)  # no cancel action -- TP order is untouched
        self.assertIn("st", actions[0])

    # -- "update to 0 removes TP or SL" ---------------------------------------

    def test_set_tp_price_zero_cancels_existing_tp(self) -> None:
        live = _account(0.5, orders=[_tp_order("old-oid", 70000.0, 0.5)])
        after = _account(0.5, orders=[])
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", side_effect=[live, after]), \
             mock.patch.object(bulk, "_submit_order", return_value=FILLED_CANCEL) as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "main",
                                  "symbol": "BTC", "price": "0"})
        self.assertTrue(resp.success, msg=str(resp))
        self.assertEqual(resp.data["protection_status"], "removed")
        actions = submit.call_args[0][1]
        self.assertEqual(actions, [{"cx": {"c": "BTC-USD", "oid": "old-oid"}}])

    def test_set_sl_price_zero_cancels_existing_sl(self) -> None:
        live = _account(-0.5, orders=[_sl_order("old-oid", 1500.0, 0.5)])
        after = _account(-0.5, orders=[])
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", side_effect=[live, after]), \
             mock.patch.object(bulk, "_submit_order", return_value=FILLED_CANCEL) as submit:
            resp = bulk.execute({"operation": "set_sl", "exchange": "bulk", "account": "main",
                                  "symbol": "ETH-USDT", "price": "0"})
        self.assertTrue(resp.success, msg=str(resp))
        self.assertEqual(resp.data["protection_status"], "removed")

    def test_set_tp_price_zero_with_no_existing_tp_is_idempotent_success(self) -> None:
        live = _account(0.5, orders=[])
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", return_value=live), \
             mock.patch.object(bulk, "_submit_order") as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "main",
                                  "symbol": "BTC", "price": "0"})
        self.assertTrue(resp.success, msg=str(resp))
        self.assertEqual(resp.data["protection_status"], "already_removed")
        submit.assert_not_called()

    # -- ambiguity / rejection / verification-failure guards ------------------

    def test_set_tp_refuses_to_choose_between_two_resting_tp_orders(self) -> None:
        live = _account(0.5, orders=[_tp_order("oid-1", 70000.0, 0.5), _tp_order("oid-2", 71000.0, 0.5)])
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", return_value=live), \
             mock.patch.object(bulk, "_submit_order") as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "main",
                                  "symbol": "BTC", "price": "70000"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "AMBIGUOUS_TP_ORDER")
        submit.assert_not_called()

    def test_set_tp_reports_rejection(self) -> None:
        live = _account(0.5, orders=[])
        rejected = {"response": {"data": {"statuses": [{"rejectedRiskLimit": {"reason": "risk"}}]}}}
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", side_effect=[live, live]), \
             mock.patch.object(bulk, "_submit_order", return_value=rejected):
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "main",
                                  "symbol": "BTC", "price": "70000"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "TP_REJECTED")

    def test_set_tp_negative_price_rejected_before_any_submission(self) -> None:
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_submit_order") as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "main",
                                  "symbol": "BTC", "price": "-5"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "INVALID_PRICE")
        submit.assert_not_called()

    # -- isolated-lane disambiguation (mirrors close_position's rigor) -------

    def test_set_tp_isolated_position_only_cancels_isolated_order_not_base(self) -> None:
        live = {
            "positions": [{"symbol": "BTC-USD", "size": 0.3, "iso": True, "isoPubkey": "isoPub111111111111111111111111111"}],
            "openOrders": [_tp_order("base-oid", 65000.0, 0.3, iso=False), _tp_order("iso-oid", 69000.0, 0.3, iso=True)],
        }
        after = {
            "positions": [{"symbol": "BTC-USD", "size": 0.3, "iso": True, "isoPubkey": "isoPub111111111111111111111111111"}],
            "openOrders": [_tp_order("base-oid", 65000.0, 0.3, iso=False), _tp_order("new-oid", 70000.0, 0.3, iso=True)],
        }
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", side_effect=[live, after]), \
             mock.patch.object(bulk, "_submit_order", return_value=RESTING) as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "main",
                                  "symbol": "BTC", "price": "70000"})
        self.assertTrue(resp.success, msg=str(resp))
        actions = submit.call_args[0][1]
        # Only the isolated-lane order is cancelled; the base-lane order is untouched.
        self.assertEqual(actions[0]["cx"], {"c": "BTC-USD", "oid": "iso-oid"})
        self.assertTrue(actions[1]["tp"]["i"])


if __name__ == "__main__":
    unittest.main()
