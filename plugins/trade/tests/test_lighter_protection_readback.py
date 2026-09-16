"""Lighter TP/SL protection readback + order classification."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import mock

from plugins.trade.agents import x_lighter_agent as lt
from plugins.trade.canonical import CanonicalPosition


def _o(
    *,
    oid: int,
    market_index: int = 1,
    is_ask: bool = True,
    otype: str = "limit",
    price: str = "76000",
    trigger_price: str | None = None,
    size: str = "1",
    reduce_only: bool = False,
    status: str = "pending",
) -> dict:
    return {
        "order_id": str(oid),
        "order_index": oid,
        "market_index": market_index,
        "is_ask": is_ask,
        "type": otype,
        "status": status,
        "price": price,
        "trigger_price": trigger_price if trigger_price is not None else price,
        "initial_base_amount": size,
        "remaining_base_amount": size,
        "reduce_only": reduce_only,
    }


class LighterProtectionKindTests(unittest.TestCase):
    def test_types(self) -> None:
        self.assertEqual(lt._order_protection_kind({"type": "take-profit"}), "tp")
        self.assertEqual(lt._order_protection_kind({"type": "take-profit-limit"}), "tp")
        self.assertEqual(lt._order_protection_kind({"type": "stop-loss"}), "sl")
        self.assertEqual(lt._order_protection_kind({"type": "stop-loss-limit"}), "sl")
        self.assertIsNone(lt._order_protection_kind({"type": "limit"}))
        self.assertIsNone(lt._order_protection_kind({"type": "market"}))


class LighterClassifyProtectionTests(unittest.TestCase):
    def test_long_tp_sl(self) -> None:
        orders = [
            _o(oid=1, otype="take-profit", price="77500", trigger_price="77500", reduce_only=True, size="13.9"),
            _o(oid=2, otype="stop-loss", price="74500", trigger_price="74500", reduce_only=True, size="13.9"),
            _o(oid=3, otype="limit", price="76000", reduce_only=False, size="0.5"),
        ]
        b = lt._classify_protection_orders(orders=orders, market_id=1, closing_side="sell")
        self.assertEqual(len(b["tp"]), 1)
        self.assertEqual(len(b["sl"]), 1)
        self.assertEqual(str(b["tp"][0]["trigger_price"]), "77500")
        self.assertEqual(str(b["sl"][0]["trigger_price"]), "74500")

    def test_short_tp_sl_buy_side(self) -> None:
        orders = [
            _o(oid=10, is_ask=False, otype="take-profit", price="2500", trigger_price="2500", reduce_only=True),
            _o(oid=11, is_ask=False, otype="stop-loss", price="2800", trigger_price="2800", reduce_only=True),
        ]
        b = lt._classify_protection_orders(orders=orders, market_id=1, closing_side="buy")
        self.assertEqual(len(b["tp"]), 1)
        self.assertEqual(len(b["sl"]), 1)

    def test_wrong_side_ignored(self) -> None:
        orders = [
            _o(oid=1, is_ask=False, otype="take-profit", price="77500", reduce_only=True),
        ]
        b = lt._classify_protection_orders(orders=orders, market_id=1, closing_side="sell")
        self.assertEqual(b["tp"], [])
        self.assertEqual(b["sl"], [])

    def test_non_reduce_only_ignored(self) -> None:
        orders = [
            _o(oid=1, otype="take-profit", price="77500", reduce_only=False),
        ]
        b = lt._classify_protection_orders(orders=orders, market_id=1, closing_side="sell")
        self.assertEqual(b["tp"], [])

    def test_wrong_market_ignored(self) -> None:
        orders = [
            _o(oid=1, market_index=99, otype="take-profit", price="77500", reduce_only=True),
        ]
        b = lt._classify_protection_orders(orders=orders, market_id=1, closing_side="sell")
        self.assertEqual(b["tp"], [])

    def test_multiple_tp_levels_preserved(self) -> None:
        orders = [
            _o(oid=1, otype="take-profit", price="77000", trigger_price="77000", reduce_only=True, size="1"),
            _o(oid=2, otype="take-profit", price="78000", trigger_price="78000", reduce_only=True, size="1"),
            _o(oid=3, otype="stop-loss", price="74000", trigger_price="74000", reduce_only=True, size="2"),
        ]
        b = lt._classify_protection_orders(orders=orders, market_id=1, closing_side="sell")
        self.assertEqual(len(b["tp"]), 2)
        self.assertEqual(len(b["sl"]), 1)


class LighterAugmentPositionsTests(unittest.TestCase):
    def test_long_tp_sl_on_position(self) -> None:
        positions = [
            CanonicalPosition(symbol="BTC", side="long", size="13.97786", entry_price="76709.5", pnl="0"),
        ]
        target = {
            "positions": [
                {"symbol": "BTC", "sign": 1, "position": "13.97786", "market_id": 1, "avg_entry_price": "76709.5"},
            ]
        }
        orders = [
            _o(oid=101, otype="take-profit", price="77500.0", trigger_price="77500.0", reduce_only=True, size="13.97786"),
            _o(oid=102, otype="stop-loss", price="74500.0", trigger_price="74500.0", reduce_only=True, size="13.97786"),
        ]
        market_map = {1: {"symbol": "BTC", "price_precision": 1}}
        out = lt._augment_positions_with_protection(
            positions, target=target, active_orders=orders, market_map=market_map
        )
        self.assertEqual(out[0].tp, "77500.0")
        self.assertEqual(out[0].sl, "74500.0")
        self.assertEqual(out[0].tp_count, 1)
        self.assertEqual(out[0].sl_count, 1)

    def test_short_tp_sl(self) -> None:
        positions = [
            CanonicalPosition(symbol="ETH", side="short", size="0.0349", entry_price="2615.66", pnl="0"),
        ]
        target = {
            "positions": [
                {"symbol": "ETH", "sign": -1, "position": "0.0349", "market_id": 0},
            ]
        }
        orders = [
            _o(
                oid=1,
                market_index=0,
                is_ask=False,
                otype="take-profit",
                price="2500",
                trigger_price="2500",
                reduce_only=True,
            ),
            _o(
                oid=2,
                market_index=0,
                is_ask=False,
                otype="stop-loss",
                price="2800",
                trigger_price="2800",
                reduce_only=True,
            ),
        ]
        out = lt._augment_positions_with_protection(
            positions, target=target, active_orders=orders, market_map={0: {"symbol": "ETH", "price_precision": 2}}
        )
        self.assertEqual(Decimal(out[0].tp), Decimal("2500"))
        self.assertEqual(Decimal(out[0].sl), Decimal("2800"))

    def test_unrelated_symbol_ignored(self) -> None:
        positions = [
            CanonicalPosition(symbol="BTC", side="long", size="1", entry_price="70000", pnl="0"),
        ]
        target = {"positions": [{"symbol": "BTC", "sign": 1, "position": "1", "market_id": 1}]}
        orders = [
            _o(oid=1, market_index=2, otype="take-profit", price="1", reduce_only=True),
        ]
        out = lt._augment_positions_with_protection(
            positions, target=target, active_orders=orders, market_map={1: {"symbol": "BTC"}}
        )
        self.assertIsNone(out[0].tp)
        self.assertIsNone(out[0].sl)


class LighterAggregateClassificationTests(unittest.TestCase):
    def test_tp_sl_not_grouped_with_limit(self) -> None:
        orders = [
            _o(oid=1, otype="take-profit", price="77500", trigger_price="77500", reduce_only=True, size="13.97786"),
            _o(oid=2, otype="stop-loss", price="74500", trigger_price="74500", reduce_only=True, size="13.97786"),
            _o(oid=3, otype="limit", price="76000", reduce_only=False, size="0.5"),
            _o(oid=4, otype="limit", price="76100", reduce_only=False, size="0.5"),
        ]
        mm = {1: {"symbol": "BTC", "price_precision": 1}}
        groups = lt._aggregate_open_orders(orders, market_map=mm)
        by_c = {g.classification: g for g in groups}
        self.assertIn("take_profit", by_c)
        self.assertIn("stop_loss", by_c)
        self.assertIn("entry_limit", by_c)
        self.assertEqual(by_c["take_profit"].display_type, "TAKE PROFIT")
        self.assertEqual(by_c["stop_loss"].display_type, "STOP LOSS")
        self.assertEqual(by_c["take_profit"].order_count, 1)
        self.assertEqual(by_c["stop_loss"].order_count, 1)
        self.assertEqual(by_c["entry_limit"].order_count, 2)
        self.assertEqual(by_c["take_profit"].min_price, "77500")
        self.assertEqual(by_c["stop_loss"].min_price, "74500")
        self.assertEqual(by_c["take_profit"].order_ids, [1])
        self.assertEqual(by_c["stop_loss"].order_ids, [2])
        self.assertEqual(set(by_c["entry_limit"].order_ids or []), {3, 4})

    def test_ordinary_buy_and_sell_limit(self) -> None:
        orders = [
            _o(oid=1, is_ask=False, otype="limit", price="100", size="2"),
            _o(oid=2, is_ask=True, otype="limit", price="110", size="3"),
        ]
        mm = {1: {"symbol": "HYPE", "price_precision": 3}}
        groups = lt._aggregate_open_orders(orders, market_map=mm)
        self.assertEqual(len(groups), 2)
        for g in groups:
            self.assertEqual(g.classification, "entry_limit")
            self.assertIn("LIMIT", g.display_type)

    def test_ladder_stays_entry_limit(self) -> None:
        orders = [
            _o(oid=i, otype="limit", price=str(100 + i), size="1", is_ask=True)
            for i in range(48)
        ]
        mm = {1: {"symbol": "HYPE", "price_precision": 3}}
        groups = lt._aggregate_open_orders(orders, market_map=mm)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].classification, "entry_limit")
        self.assertEqual(groups[0].order_count, 48)

    def test_tp_not_grouped_with_sl(self) -> None:
        orders = [
            _o(oid=1, otype="take-profit", price="77500", trigger_price="77500", reduce_only=True),
            _o(oid=2, otype="stop-loss", price="74500", trigger_price="74500", reduce_only=True),
        ]
        groups = lt._aggregate_open_orders(orders, market_map={1: {"symbol": "BTC"}})
        classes = {g.classification for g in groups}
        self.assertEqual(classes, {"take_profit", "stop_loss"})

    def test_multiple_tp_levels_in_group(self) -> None:
        orders = [
            _o(oid=1, otype="take-profit", price="77000", trigger_price="77000", reduce_only=True, size="1"),
            _o(oid=2, otype="take-profit", price="78000", trigger_price="78000", reduce_only=True, size="1"),
        ]
        groups = lt._aggregate_open_orders(orders, market_map={1: {"symbol": "BTC"}})
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].classification, "take_profit")
        self.assertEqual(groups[0].order_count, 2)
        self.assertEqual(set(groups[0].order_ids or []), {1, 2})
        self.assertEqual(groups[0].min_price, "77000")
        self.assertEqual(groups[0].max_price, "78000")

    def test_does_not_classify_from_price_vs_entry(self) -> None:
        # reduce-only LIMIT without TP/SL type stays entry_limit (or trigger only if trigger_price set without type)
        orders = [
            _o(oid=1, otype="limit", price="90000", reduce_only=True, trigger_price=None),
        ]
        # trigger_price defaults to price in helper — force no type and no trigger semantics:
        o = _o(oid=1, otype="limit", price="90000", reduce_only=True)
        o["trigger_price"] = None
        groups = lt._aggregate_open_orders([o], market_map={1: {"symbol": "BTC"}})
        self.assertEqual(groups[0].classification, "entry_limit")


class LighterCancelScopeTests(unittest.TestCase):
    """Unit-level filter logic for cancel classification (no network)."""

    def test_cancel_tp_ids_only(self) -> None:
        orders = [
            _o(oid=101, otype="take-profit", price="77500", trigger_price="77500", reduce_only=True),
            _o(oid=102, otype="stop-loss", price="74500", trigger_price="74500", reduce_only=True),
            _o(oid=103, otype="limit", price="76000", reduce_only=False),
        ]
        # Replicate cancel class filter
        def _order_class(order):
            kind = lt._order_protection_kind(order)
            if kind == "tp":
                return "take_profit"
            if kind == "sl":
                return "stop_loss"
            return "entry_limit"

        tp_ids = [int(o["order_id"]) for o in orders if _order_class(o) == "take_profit"]
        sl_ids = [int(o["order_id"]) for o in orders if _order_class(o) == "stop_loss"]
        entry_ids = [int(o["order_id"]) for o in orders if _order_class(o) == "entry_limit"]
        self.assertEqual(tp_ids, [101])
        self.assertEqual(sl_ids, [102])
        self.assertEqual(entry_ids, [103])
        self.assertNotIn(101, entry_ids)
        self.assertNotIn(102, entry_ids)


if __name__ == "__main__":
    unittest.main()
