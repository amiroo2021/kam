"""Canonical instrument identity routing for TradeMenu."""

from __future__ import annotations

import unittest
from unittest import mock

from plugins.trade.agents import x_raydium_agent as rx
from plugins.trade.canonical import CanonicalOrderGroup, CanonicalPosition, make_success
from plugins.trade.trademenu.service import TradeMenuService


class RaydiumFriendlyResolveTests(unittest.TestCase):
    def test_zecusd_maps_to_perp(self) -> None:
        self.assertEqual(rx._orderly_symbol("ZECUSD"), "PERP_ZEC_USDC")
        self.assertEqual(rx._orderly_symbol("ZEC"), "PERP_ZEC_USDC")
        self.assertEqual(rx._orderly_symbol("zec_usdc"), "PERP_ZEC_USDC")
        self.assertEqual(rx._orderly_symbol("PERP_ZEC_USDC"), "PERP_ZEC_USDC")


class OrderGroupNativeIdentityTests(unittest.TestCase):
    def test_aggregate_preserves_exchange_instrument(self) -> None:
        rows = [
            {
                "symbol": "PERP_ZEC_USDC",
                "side": "SELL",
                "status": "NEW",
                "order_price": "1900",
                "quantity": "1",
                "order_id": 11,
            }
        ]
        _, groups = rx._aggregate_orders(rows, symbol_rules={})
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].symbol, "ZEC")
        self.assertEqual(groups[0].exchange_instrument, "PERP_ZEC_USDC")


class TradeMenuServiceNativePassthroughTests(unittest.TestCase):
    def test_positions_and_orders_expose_native(self) -> None:
        class Desk:
            def list_exchanges(self):
                return ["raydium"]

            def list_accounts(self, exchange):
                return ["phantom"]

            def capabilities(self, exchange):
                return ["positions_orders"]

            def execute(self, req):
                return make_success(
                    "positions_orders",
                    "raydium",
                    "phantom",
                    positions=[
                        CanonicalPosition(
                            symbol="ZEC",
                            side="short",
                            size="1",
                            entry_price="1000",
                            pnl="0",
                            exchange_instrument="PERP_ZEC_USDC",
                        )
                    ],
                    order_groups=[
                        CanonicalOrderGroup(
                            symbol="ZEC",
                            side="sell",
                            order_count=95,
                            total_size="88",
                            vwap="1891",
                            min_price="1315",
                            max_price="2088",
                            classification="entry_limit",
                            display_type="SELL LIMIT",
                            exchange_instrument="PERP_ZEC_USDC",
                            order_ids=[1, 2],
                        )
                    ],
                    open_order_count=95,
                )

        svc = TradeMenuService(desk=Desk(), cache_ttl=0.01)  # type: ignore[arg-type]
        pos = svc.positions("raydium", "phantom")
        self.assertEqual(pos["positions"][0]["native_symbol"], "PERP_ZEC_USDC")
        ord_ = svc.orders("raydium", "phantom")
        g = ord_["groups"][0]
        self.assertEqual(g["native_symbol"], "PERP_ZEC_USDC")
        self.assertEqual(g["count"], 95)

    def test_safe_error_strips_http_url(self) -> None:
        from types import SimpleNamespace

        resp = SimpleNamespace(
            error=SimpleNamespace(
                code="ORDER_SUBMISSION_FAILED",
                message="400 Client Error: Bad Request for url: https://api.orderly.org/v1/order",
                exchange_reason=None,
            )
        )
        # _to_plain may not expand SimpleNamespace — pass a dict-shaped error via CanonicalError if needed
        from plugins.trade.canonical import CanonicalError, make_failure

        bad = make_failure(
            "ladder",
            "raydium",
            "phantom",
            "ORDER_SUBMISSION_FAILED",
            "400 Client Error: Bad Request for url: https://api.orderly.org/v1/order",
        )
        svc = TradeMenuService(desk=mock.Mock(), cache_ttl=0.01)  # type: ignore[arg-type]
        out = svc._safe_error(bad)
        self.assertNotIn("http", out["message"].lower())
        self.assertNotIn("orderly.org", out["message"].lower())
        self.assertIn("rejected", out["message"].lower())

    def test_partial_ladder_marks_success_with_counts(self) -> None:
        from plugins.trade.canonical import CanonicalLadderResult, make_failure

        class Desk:
            def list_exchanges(self):
                return ["raydium"]

            def list_accounts(self, exchange):
                return ["phantom"]

            def capabilities(self, exchange):
                return ["ladder", "new_order"]

            def execute(self, req):
                if req.get("operation") == "ladder":
                    return make_failure(
                        "ladder",
                        "raydium",
                        "phantom",
                        "ORDER_SUBMISSION_FAILED",
                        "400 Client Error: Bad Request for url: https://api.orderly.org/v1/order",
                        ladder=CanonicalLadderResult(
                            symbol="ZEC",
                            side="sell",
                            distribution="uniform",
                            requested_order_count=100,
                            submitted_order_count=95,
                            requested_volume="100",
                            submitted_volume="95",
                            accepted_child_count=95,
                            partial=True,
                            status="partial",
                            verified=False,
                            batch_count=95,
                        ),
                    )
                return make_failure(req.get("operation") or "x", "raydium", "phantom", "NO", "no")

        svc = TradeMenuService(desk=Desk(), session_secret="unit-test-session-secret-value-32b")  # type: ignore[arg-type]
        # Build a consumed plan via preview store directly
        plan = {
            "kind": "ladder",
            "exchange": "raydium",
            "account": "phantom",
            "native_symbol": "PERP_ZEC_USDC",
            "side": "sell",
            "distribution": "uniform",
            "order_count": 100,
            "requested_total_size": "100",
            "start_price": "2000",
            "end_price": "1300",
            "children": [{"price": "1", "size": "1"}] * 100,
            "exec": {
                "symbol": "PERP_ZEC_USDC",
                "side": "sell",
                "distribution": "uniform",
                "order_count": 100,
                "total_volume": "100",
                "start_price": "2000",
                "end_price": "1300",
            },
        }
        pid = svc.previews.issue(plan)
        out = svc.execute_preview(pid)
        self.assertTrue(out.get("partial"))
        self.assertEqual(out.get("accepted"), 95)
        self.assertEqual(out.get("requested"), 100)
        self.assertTrue(out.get("success"))
        self.assertIn("95", out.get("message") or "")
        self.assertNotIn("http", (out.get("message") or "").lower())
        # No automatic second execute — plan consumed
        out2 = svc.execute_preview(pid)
        self.assertFalse(out2.get("success"))


if __name__ == "__main__":
    unittest.main()
