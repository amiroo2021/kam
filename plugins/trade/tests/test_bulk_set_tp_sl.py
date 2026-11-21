"""Regression tests for Bulk's native set_tp / set_sl support.

Bulk previously returned NOT_IMPLEMENTED for set_tp/set_sl, and an earlier
attempt at this feature (reverted) hand-rolled a binary wincode encoder that
was missing bulk_keychain's trailing optional commission/slippage fields and
had no encoder at all for stop/take_profit actions - every live attempt
failed with "Bulk submission outcome is uncertain." This version delegates
all signing to bulk_keychain (Bulk's official signing SDK, vendored at
plugins/trade/vendor/bulk_keychain/), verified against its own upstream
test suite (`cargo test -p bulk-keychain`, including
all_modes_match_real_sdk_bytes_and_signatures) before being wired in here.
"""

from __future__ import annotations

import base58
import unittest
from unittest import mock

from plugins.trade.agents import x_bulk_agent as bulk

VALID_SEED_B58 = base58.b58encode(bytes([7] * 32)).decode("ascii")
ACCOUNT_PUBKEY = "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7"


def _account(size: float, price=None, order_type=None, order_id="existing-order-id", reduce_only=True, iso=False, order_side=None) -> dict:
    positions = [{"symbol": "ZEC-USDT", "size": size, "iso": iso}] if size else []
    open_orders = []
    if price is not None:
        open_orders.append({
            "symbol": "ZEC-USDT", "orderId": order_id, "orderType": order_type,
            "reduceOnly": reduce_only, "iso": iso, "price": price,
            "size": (abs(size) if order_side == "sell" else -abs(size)) if order_side else (1 if size < 0 else -1),
        })
    return {"positions": positions, "openOrders": open_orders}


CREDS = {"account": "bullet", "account_pubkey": ACCOUNT_PUBKEY, "agent_private_key": VALID_SEED_B58, "base_url": bulk.DEFAULT_API_BASE}
MARKET = {"symbol": "ZEC-USDT", "sizeIncrement": 0.0000001, "sizeDecimals": 7}


class TestBulkSetTpSl(unittest.TestCase):
    def setUp(self) -> None:
        with bulk._close_guard_lock:
            bulk._close_guard.clear()
        with bulk._protection_guard_lock:
            bulk._protection_guard.clear()

    # ---- signing/encoding correctness (real bulk_keychain, no mocking) ----

    def test_sign_transaction_produces_tp_and_st_actions_via_bulk_keychain(self) -> None:
        for tag, op in (("tp", "set_tp"), ("st", "set_sl")):
            action = {tag: {"c": "ZEC-USDT", "d": False, "sz": 121.6389651, "tr": 500.0, "lim": None, "i": False}}
            signed = bulk._sign_transaction(CREDS, [action], nonce=42)
            self.assertEqual(signed["actions"], [action])
            self.assertEqual(signed["account"], ACCOUNT_PUBKEY)
            self.assertTrue(signed["signer"])
            self.assertTrue(signed["signature"])
            self.assertEqual(signed["nonce"], "42")

    def test_sign_transaction_still_handles_market_limit_cancel_actions(self) -> None:
        actions = [
            {"m": {"c": "BTC-USD", "b": True, "sz": 0.5, "r": True, "i": False}},
            {"l": {"c": "BTC-USD", "b": False, "px": 70000.0, "sz": 0.1, "tif": "GTC", "r": False, "i": False}},
            {"cx": {"c": "BTC-USD", "oid": ACCOUNT_PUBKEY}},
            {"cxa": {"c": ["BTC-USD", "ETH-USD"]}},
        ]
        signed = bulk._sign_transaction(CREDS, actions, nonce=7)
        self.assertEqual(signed["actions"], actions)
        self.assertTrue(signed["signature"])

    def test_to_official_order_item_translates_every_action_tag(self) -> None:
        self.assertEqual(
            bulk._to_official_order_item({"tp": {"c": "ZEC-USDT", "d": True, "sz": 1.0, "tr": 10.0, "lim": 9.5, "i": True}}),
            {"type": "take_profit", "symbol": "ZEC-USDT", "is_buy": True, "size": 1.0, "trigger_price": 10.0, "limit_price": 9.5, "iso": True},
        )
        self.assertEqual(
            bulk._to_official_order_item({"st": {"c": "ZEC-USDT", "d": False, "sz": 1.0, "tr": 5.0, "lim": None, "i": False}}),
            {"type": "stop", "symbol": "ZEC-USDT", "is_buy": False, "size": 1.0, "trigger_price": 5.0, "limit_price": None, "iso": False},
        )

    # ---- _bulk_set_protection business logic (submission mocked) ----

    def test_set_tp_places_fresh_order_when_none_existed(self) -> None:
        live = _account(-121.6389651)
        after = _account(-121.6389651, price=500.0, order_type="takeProfit", order_side="sell")
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", side_effect=[live, after]), \
             mock.patch.object(bulk, "_submit_order", return_value={"status": "ok", "response": {"data": {"statuses": [{"resting": {}}]}}}) as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "bullet", "symbol": "ZEC", "price": "500"})
        self.assertTrue(resp.success, msg=str(resp.error))
        self.assertEqual(resp.data["protection_status"], "set")
        actions = submit.call_args.args[1]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0], {"tp": {"c": "ZEC-USDT", "d": True, "sz": 121.6389651, "tr": 500.0, "lim": None, "i": False}})

    def test_set_sl_short_position_trigger_direction(self) -> None:
        # Short position: protective SL buys back, trigger is_buy=True (d).
        live = _account(-121.6389651)
        after = _account(-121.6389651, price=600.0, order_type="stop", order_side="sell")
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", side_effect=[live, after]), \
             mock.patch.object(bulk, "_submit_order", return_value={"status": "ok", "response": {"data": {"statuses": [{"resting": {}}]}}}) as submit:
            resp = bulk.execute({"operation": "set_sl", "exchange": "bulk", "account": "bullet", "symbol": "ZEC", "price": "600"})
        self.assertTrue(resp.success, msg=str(resp.error))
        actions = submit.call_args.args[1]
        self.assertEqual(actions[0], {"st": {"c": "ZEC-USDT", "d": True, "sz": 121.6389651, "tr": 600.0, "lim": None, "i": False}})

    def test_set_tp_long_position_trigger_direction(self) -> None:
        # Long position: protective TP sells, trigger is_buy=False (d).
        live = _account(50.0)
        after = _account(50.0, price=100.0, order_type="takeProfit", order_side="buy")
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", side_effect=[live, after]), \
             mock.patch.object(bulk, "_submit_order", return_value={"status": "ok", "response": {"data": {"statuses": [{"resting": {}}]}}}) as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "bullet", "symbol": "ZEC", "price": "100"})
        self.assertTrue(resp.success, msg=str(resp.error))
        actions = submit.call_args.args[1]
        self.assertEqual(actions[0], {"tp": {"c": "ZEC-USDT", "d": False, "sz": 50.0, "tr": 100.0, "lim": None, "i": False}})

    def test_set_tp_replaces_existing_order_atomically(self) -> None:
        live = _account(-121.6389651, price=400.0, order_type="takeProfit", order_id="old-order-1", order_side="sell")
        after = _account(-121.6389651, price=500.0, order_type="takeProfit", order_id="new-order-2", order_side="sell")
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", side_effect=[live, after]), \
             mock.patch.object(bulk, "_submit_order", return_value={"status": "ok", "response": {"data": {"statuses": [{"resting": {}}]}}}) as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "bullet", "symbol": "ZEC", "price": "500"})
        self.assertTrue(resp.success, msg=str(resp.error))
        actions = submit.call_args.args[1]
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0], {"cx": {"c": "ZEC-USDT", "oid": "old-order-1"}})
        self.assertEqual(actions[1]["tp"]["tr"], 500.0)

    def test_set_tp_price_zero_removes_existing_order(self) -> None:
        live = _account(-121.6389651, price=500.0, order_type="takeProfit", order_id="old-order-1", order_side="sell")
        after = _account(-121.6389651)
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", side_effect=[live, after]), \
             mock.patch.object(bulk, "_submit_order", return_value={"status": "ok", "response": {"data": {"statuses": [{"cancelled": {}}]}}}) as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "bullet", "symbol": "ZEC", "price": "0"})
        self.assertTrue(resp.success, msg=str(resp.error))
        self.assertEqual(resp.data["protection_status"], "removed")
        actions = submit.call_args.args[1]
        self.assertEqual(actions, [{"cx": {"c": "ZEC-USDT", "oid": "old-order-1"}}])

    def test_set_tp_price_zero_with_nothing_existing_is_a_noop_success(self) -> None:
        live = _account(-121.6389651)
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", return_value=live), \
             mock.patch.object(bulk, "_submit_order") as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "bullet", "symbol": "ZEC", "price": "0"})
        self.assertTrue(resp.success, msg=str(resp.error))
        self.assertEqual(resp.data["protection_status"], "already_unset")
        submit.assert_not_called()

    def test_set_tp_fails_loudly_never_substitutes_stale_size_when_fetched_is_zero(self) -> None:
        # _position_rows already filters size==0 rows out (mirrors _normalize_positions),
        # so a fully-flat account surfaces as "no position" not a silent zero-size order.
        live = _account(0.0)
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", return_value=live), \
             mock.patch.object(bulk, "_submit_order") as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "bullet", "symbol": "ZEC", "price": "500"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "POSITION_NOT_FOUND")
        submit.assert_not_called()

    def test_set_tp_refuses_ambiguous_existing_protective_orders(self) -> None:
        live = _account(-121.6389651)
        live["openOrders"] = [
            {"symbol": "ZEC-USDT", "orderId": "a", "orderType": "takeProfit", "reduceOnly": True, "iso": False, "price": 400.0, "size": 10},
            {"symbol": "ZEC-USDT", "orderId": "b", "orderType": "takeProfit", "reduceOnly": True, "iso": False, "price": 450.0, "size": 10},
        ]
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", return_value=live), \
             mock.patch.object(bulk, "_submit_order") as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "bullet", "symbol": "ZEC", "price": "500"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "AMBIGUOUS_PROTECTIVE_ORDER")
        submit.assert_not_called()

    def test_set_tp_submission_error_reports_uncertain_outcome_not_crash(self) -> None:
        live = _account(-121.6389651)
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", return_value=live), \
             mock.patch.object(bulk, "_submit_order", side_effect=RuntimeError("timeout")):
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "bullet", "symbol": "ZEC", "price": "500"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "TP_PLACEMENT_FAILED")
        self.assertEqual(resp.data["protection_status"], "unknown")
        self.assertFalse(resp.data["retry_safe"])
        # Outcome is uncertain (we don't know if Bulk received the order), so
        # the guard must stay held — same as close_position's
        # CLOSE_OUTCOME_UNKNOWN pattern — forcing a human to reconcile state
        # before any further automatic submission for this market/lane.
        self.assertIn((ACCOUNT_PUBKEY, "ZEC-USDT", False, "take_profit"), bulk._protection_guard)

    def test_set_tp_rejected_by_exchange(self) -> None:
        live = _account(-121.6389651)
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", return_value=live), \
             mock.patch.object(bulk, "_submit_order", return_value={"response": {"data": {"statuses": [{"rejectedInvalid": {"reason": "trigger price must be below mark price for a short take-profit"}}]}}}):
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "bullet", "symbol": "ZEC", "price": "500"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "TP_REJECTED")
        self.assertIn("trigger price must be below mark price", resp.error.message)
        self.assertEqual(resp.data["reasons"], ["trigger price must be below mark price for a short take-profit"])

    def test_set_tp_rejects_non_conforming_position_size(self) -> None:
        # Market requires 7-decimal / 0.0000001 increments; a position size
        # with finer precision than the market allows must never be sent.
        live = _account(-121.63896519)
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", return_value=live), \
             mock.patch.object(bulk, "_submit_order") as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "bullet", "symbol": "ZEC", "price": "500"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "POSITION_SIZE_INVALID")
        submit.assert_not_called()

    def test_set_tp_quantizes_price_to_market_tick_size(self) -> None:
        market = dict(MARKET, tickSize=0.01, priceDecimals=2)
        live = _account(-121.6389651)
        after = _account(-121.6389651, price=500.12, order_type="takeProfit", order_side="sell")
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=market), \
             mock.patch.object(bulk, "_live_account", side_effect=[live, after]), \
             mock.patch.object(bulk, "_submit_order", return_value={"status": "ok", "response": {"data": {"statuses": [{"resting": {}}]}}}) as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "bullet", "symbol": "ZEC", "price": "500.126"})
        self.assertTrue(resp.success, msg=str(resp.error))
        actions = submit.call_args.args[1]
        self.assertEqual(actions[0]["tp"]["tr"], 500.12)

    def test_set_sl_rejected_by_exchange_uses_sl_code(self) -> None:
        live = _account(-121.6389651)
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", return_value=live), \
             mock.patch.object(bulk, "_submit_order", return_value={"response": {"data": {"statuses": [{"rejectedInvalid": {}}]}}}):
            resp = bulk.execute({"operation": "set_sl", "exchange": "bulk", "account": "bullet", "symbol": "ZEC", "price": "500"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "SL_REJECTED")

    def test_set_tp_negative_price_rejected(self) -> None:
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_submit_order") as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "bullet", "symbol": "ZEC", "price": "-5"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "INVALID_PRICE")
        submit.assert_not_called()

    def test_concurrent_protection_mutation_is_guarded(self) -> None:
        guard_key = (ACCOUNT_PUBKEY, "ZEC-USDT", False, "take_profit")
        with bulk._protection_guard_lock:
            bulk._protection_guard.add(guard_key)
        live = _account(-121.6389651)
        with mock.patch.object(bulk, "_lookup_credentials", return_value=CREDS), \
             mock.patch.object(bulk, "_find_market", return_value=MARKET), \
             mock.patch.object(bulk, "_live_account", return_value=live), \
             mock.patch.object(bulk, "_submit_order") as submit:
            resp = bulk.execute({"operation": "set_tp", "exchange": "bulk", "account": "bullet", "symbol": "ZEC", "price": "500"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "PROTECTION_OUTCOME_UNKNOWN")
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
