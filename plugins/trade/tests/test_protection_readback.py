"""TP/SL protection readback for Hibachi / Pacifica positions_orders."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import mock

from plugins.trade.canonical import CanonicalPosition
from plugins.trade.agents import x_hibachi_agent as hb
from plugins.trade.agents import x_pacifica_agent as pc


class HibachiProtectionReadbackTests(unittest.TestCase):
    def test_augment_from_trigger_orders(self) -> None:
        positions = [
            CanonicalPosition(symbol="BTC", side="long", size="1.5", entry_price="77155", pnl="0"),
        ]
        raw_orders = [
            {
                "symbol": "BTC/USDT-P",
                "side": "ASK",
                "triggerPrice": "77500",
                "triggerDirection": "HIGH",
                "orderFlags": "REDUCE_ONLY",
                "orderId": "1",
                "orderType": "MARKET",
            },
            {
                "symbol": "BTC/USDT-P",
                "side": "ASK",
                "triggerPrice": "74500",
                "triggerDirection": "LOW",
                "orderFlags": "REDUCE_ONLY",
                "orderId": "2",
                "orderType": "MARKET",
            },
            # ordinary limit — ignored for protection
            {
                "symbol": "BTC/USDT-P",
                "side": "BID",
                "price": "75000",
                "totalQuantity": "0.1",
                "orderFlags": "",
                "orderId": "3",
            },
        ]
        out = hb._augment_positions_with_protection(positions, raw_orders)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].tp, "77500")
        self.assertEqual(out[0].sl, "74500")
        self.assertEqual(out[0].tp_count, 1)
        self.assertEqual(out[0].sl_count, 1)

    def test_group_open_orders_separates_tp_sl(self) -> None:
        positions = [
            CanonicalPosition(symbol="BTC", side="long", size="1", entry_price="77155", pnl="0"),
        ]
        raw_orders = [
            {
                "symbol": "BTC/USDT-P",
                "side": "BID",
                "price": "75000",
                "totalQuantity": "0.5",
                "orderId": "10",
            },
            {
                "symbol": "BTC/USDT-P",
                "side": "ASK",
                "triggerPrice": "77500",
                "triggerDirection": "HIGH",
                "orderFlags": "REDUCE_ONLY",
                "orderId": "11",
                "orderType": "MARKET",
            },
            {
                "symbol": "BTC/USDT-P",
                "side": "ASK",
                "triggerPrice": "74500",
                "triggerDirection": "LOW",
                "orderFlags": "REDUCE_ONLY",
                "orderId": "12",
                "orderType": "MARKET",
            },
        ]
        count, groups = hb._group_open_orders(raw_orders, positions=positions)
        by_class = {g.classification: g for g in groups}
        self.assertIn("entry_limit", by_class)
        self.assertIn("take_profit", by_class)
        self.assertIn("stop_loss", by_class)
        self.assertEqual(by_class["take_profit"].min_price, "77500")
        self.assertEqual(by_class["stop_loss"].min_price, "74500")
        self.assertEqual(by_class["take_profit"].display_type, "TAKE PROFIT")
        self.assertEqual(by_class["stop_loss"].display_type, "STOP LOSS")


class PacificaProtectionReadbackTests(unittest.TestCase):
    def test_attach_protection_from_stop_children(self) -> None:
        positions = [
            CanonicalPosition(symbol="BTC", side="long", size="1", entry_price="77000", pnl="0"),
        ]
        stops = [
            {
                "symbol": "BTC",
                "side": "ask",
                "order_type": "take_profit_market",
                "stop_price": "78000",
                "reduce_only": True,
                "order_id": 1,
                "initial_amount": "1",
            },
            {
                "symbol": "BTC",
                "side": "ask",
                "order_type": "stop_market",
                "stop_price": "74000",
                "reduce_only": True,
                "order_id": 2,
                "initial_amount": "1",
            },
            # plain limit-like should be ignored by classifier (no stop_price handled elsewhere)
        ]
        out = pc._attach_pacifica_protection(positions, stops)
        self.assertEqual(out[0].tp, "78000")
        self.assertEqual(out[0].sl, "74000")

    def test_protection_order_groups_classified(self) -> None:
        positions = [
            CanonicalPosition(symbol="ETH", side="short", size="1", entry_price="2600", pnl="0"),
        ]
        stops = [
            {
                "symbol": "ETH",
                "side": "bid",
                "order_type": "take_profit_market",
                "stop_price": "2500",
                "reduce_only": True,
                "order_id": 9,
                "initial_amount": "1",
            },
        ]
        groups = pc._aggregate_pacifica_protection_orders(stops, positions)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].classification, "take_profit")
        self.assertEqual(groups[0].display_type, "TAKE PROFIT")
        self.assertEqual(groups[0].trigger_price, "2500")

    def test_unrelated_symbol_ignored(self) -> None:
        positions = [
            CanonicalPosition(symbol="BTC", side="long", size="1", entry_price="77000", pnl="0"),
        ]
        stops = [
            {
                "symbol": "ETH",
                "side": "ask",
                "order_type": "take_profit_market",
                "stop_price": "3000",
                "reduce_only": True,
                "order_id": 3,
            },
        ]
        out = pc._attach_pacifica_protection(positions, stops)
        self.assertIsNone(out[0].tp)
        self.assertIsNone(out[0].sl)


if __name__ == "__main__":
    unittest.main()
