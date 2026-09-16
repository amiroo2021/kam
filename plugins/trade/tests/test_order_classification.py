"""Order classification / aggregation tests for Hyperliquid + TradeMenu."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import mock

from plugins.trade.agents.x_hyperliquid_agent import (
    _aggregate_open_orders,
    _order_classification,
    _order_display_type,
    _protection_order_tpsl,
)
from plugins.trade.canonical import CanonicalOrderGroup
from plugins.trade.trademenu.service import TradeMenuService


def _oid(n: int, **kwargs):
    base = {
        "symbol": "BTC",
        "side": "sell",
        "oid": n,
        "size": Decimal("1"),
        "price": Decimal("76000"),
        "trigger_px": None,
        "is_trigger": False,
        "is_position_tpsl": False,
        "reduce_only": False,
        "order_type": "Limit",
        "trigger_condition": "",
        "tpsl": None,
        "tp": None,
        "sl": None,
    }
    base.update(kwargs)
    return base


class OrderClassificationTests(unittest.TestCase):
    def test_entry_limit_buy_and_sell(self) -> None:
        buy = _oid(1, side="buy", price=Decimal("75000"), reduce_only=False)
        sell = _oid(2, side="sell", price=Decimal("76000"), reduce_only=False)
        self.assertEqual(_order_classification(buy), "entry_limit")
        self.assertEqual(_order_classification(sell), "entry_limit")
        self.assertEqual(_order_display_type("entry_limit", "buy"), "BUY LIMIT")
        self.assertEqual(_order_display_type("entry_limit", "sell"), "SELL LIMIT")

    def test_take_profit_from_explicit_tpsl(self) -> None:
        o = _oid(
            3,
            side="sell",
            reduce_only=True,
            is_position_tpsl=True,
            tpsl="tp",
            order_type="Take Profit Market",
            trigger_px=Decimal("76000"),
            price=Decimal("76000"),
            tp="76000",
        )
        self.assertEqual(_protection_order_tpsl(o), "tp")
        self.assertEqual(_order_classification(o), "take_profit")

    def test_stop_loss_from_explicit_tpsl(self) -> None:
        o = _oid(
            4,
            side="sell",
            reduce_only=True,
            is_position_tpsl=True,
            tpsl="sl",
            order_type="Stop Market",
            trigger_px=Decimal("74100"),
            price=Decimal("74100"),
            sl="74100",
        )
        self.assertEqual(_protection_order_tpsl(o), "sl")
        self.assertEqual(_order_classification(o), "stop_loss")

    def test_does_not_use_price_vs_entry(self) -> None:
        # Bare price-above without tpsl must NOT become TP.
        o = _oid(
            5,
            side="sell",
            reduce_only=True,
            is_trigger=True,
            order_type="Trigger",
            trigger_condition="price above",
            trigger_px=Decimal("80000"),
            tpsl=None,
            tp=None,
            sl=None,
        )
        # is_trigger without tp/sl → trigger, not take_profit
        self.assertEqual(_order_classification(o), "trigger")
        self.assertIsNone(_protection_order_tpsl(o))

    def test_reduce_only_without_label_is_other(self) -> None:
        o = _oid(6, side="sell", reduce_only=True, order_type="Limit", tpsl=None)
        self.assertEqual(_order_classification(o), "other")

    def test_aggregate_never_merges_tp_and_sl(self) -> None:
        orders = [
            _oid(10, side="buy", price=Decimal("75000"), size=Decimal("2")),
            _oid(11, side="buy", price=Decimal("75100"), size=Decimal("3")),
            _oid(
                12,
                side="sell",
                reduce_only=True,
                tpsl="tp",
                tp="76000",
                price=Decimal("76000"),
                trigger_px=Decimal("76000"),
                is_position_tpsl=True,
                order_type="Take Profit Market",
                size=Decimal("5"),
            ),
            _oid(
                13,
                side="sell",
                reduce_only=True,
                tpsl="sl",
                sl="74100",
                price=Decimal("74100"),
                trigger_px=Decimal("74100"),
                is_position_tpsl=True,
                order_type="Stop Market",
                size=Decimal("5"),
            ),
            _oid(14, side="sell", price=Decimal("77000"), size=Decimal("1")),  # ordinary sell limit
        ]
        groups = _aggregate_open_orders(orders)
        by_class = {(g.symbol, g.classification, g.side): g for g in groups}
        self.assertIn(("BTC", "entry_limit", "buy"), by_class)
        self.assertEqual(by_class[("BTC", "entry_limit", "buy")].order_count, 2)
        self.assertIn(("BTC", "take_profit", "sell"), by_class)
        self.assertIn(("BTC", "stop_loss", "sell"), by_class)
        self.assertIn(("BTC", "entry_limit", "sell"), by_class)
        # TP and SL are separate groups even though both SELL.
        self.assertEqual(by_class[("BTC", "take_profit", "sell")].order_count, 1)
        self.assertEqual(by_class[("BTC", "stop_loss", "sell")].order_count, 1)
        self.assertEqual(by_class[("BTC", "take_profit", "sell")].order_ids, [12])
        self.assertEqual(by_class[("BTC", "stop_loss", "sell")].order_ids, [13])
        self.assertNotEqual(
            by_class[("BTC", "take_profit", "sell")].display_type,
            by_class[("BTC", "stop_loss", "sell")].display_type,
        )

    def test_ordinary_sell_limit_not_confused_with_tp(self) -> None:
        orders = [
            _oid(20, side="sell", price=Decimal("76000"), reduce_only=False),
            _oid(
                21,
                side="sell",
                reduce_only=True,
                tpsl="tp",
                tp="76000",
                price=Decimal("76000"),
                is_position_tpsl=True,
                order_type="Take Profit Market",
            ),
        ]
        groups = _aggregate_open_orders(orders)
        kinds = sorted(g.classification for g in groups)
        self.assertEqual(kinds, ["entry_limit", "take_profit"])

    def test_trademenu_cancel_passes_classification_and_ids(self) -> None:
        class Desk:
            def __init__(self) -> None:
                self.calls = []

            def list_exchanges(self):
                return ["hyperliquid"]

            def list_accounts(self, exchange):
                return ["FLEX"]

            def capabilities(self, exchange):
                return ["cancel_order_group"]

            def execute(self, request):
                self.calls.append(request)
                from plugins.trade.canonical import CanonicalCancelGroupResult, make_success

                return make_success(
                    "cancel_order_group",
                    "hyperliquid",
                    "FLEX",
                    cancel_group=CanonicalCancelGroupResult(
                        symbol="BTC",
                        side="sell",
                        targeted_order_count=1,
                        cancelled_order_count=1,
                        confirmed_absent_count=1,
                        remaining_target_count=0,
                        verified=True,
                    ),
                )

        desk = Desk()
        svc = TradeMenuService(desk=desk)  # type: ignore[arg-type]
        out = svc.cancel_order_group(
            "hyperliquid",
            "FLEX",
            "BTC",
            "sell",
            classification="take_profit",
            order_ids=[12],
        )
        self.assertTrue(out["success"])
        self.assertEqual(desk.calls[0]["classification"], "take_profit")
        self.assertEqual(desk.calls[0]["order_ids"], [12])

    def test_cancel_message_when_all_targets_absent(self) -> None:
        class Desk:
            def list_exchanges(self):
                return ["hyperliquid"]

            def list_accounts(self, exchange):
                return ["FLEX"]

            def execute(self, request):
                from plugins.trade.canonical import CanonicalCancelGroupResult, make_success

                return make_success(
                    "cancel_order_group",
                    "hyperliquid",
                    "FLEX",
                    cancel_group=CanonicalCancelGroupResult(
                        symbol="BTC",
                        side="buy",
                        targeted_order_count=58,
                        cancelled_order_count=50,
                        confirmed_absent_count=58,
                        remaining_target_count=0,
                        verified=True,
                        partial=False,
                    ),
                )

        svc = TradeMenuService(desk=Desk())  # type: ignore[arg-type]
        out = svc.cancel_order_group("hyperliquid", "FLEX", "BTC", "buy", classification="entry_limit")
        self.assertTrue(out["success"])
        self.assertFalse(out.get("partial"))
        self.assertIn("All 58 targeted orders are no longer open", out["message"])


if __name__ == "__main__":
    unittest.main()
