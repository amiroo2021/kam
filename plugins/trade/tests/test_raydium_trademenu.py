"""Raydium / Orderly TradeMenu candles + agent normalization."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import mock

from plugins.trade.agents import x_raydium_agent as rx
from plugins.trade.trademenu.marketdata import fetch_raydium_candles, _orderly_symbol_from_any


class OrderlySymbolMapTests(unittest.TestCase):
    def test_maps(self) -> None:
        self.assertEqual(_orderly_symbol_from_any("ZEC"), "PERP_ZEC_USDC")
        self.assertEqual(_orderly_symbol_from_any("zec"), "PERP_ZEC_USDC")
        self.assertEqual(_orderly_symbol_from_any("PERP_BTC_USDC"), "PERP_BTC_USDC")
        self.assertEqual(_orderly_symbol_from_any("BTCUSDC"), "PERP_BTC_USDC")


class RaydiumCandleTests(unittest.TestCase):
    def test_public_query_shape(self) -> None:
        sample = {
            "success": True,
            "data": {
                "rows": [
                    {
                        "open": "10",
                        "high": "12",
                        "low": "9",
                        "close": "11",
                        "volume": "1.5",
                        "timestamp": 1_700_000_000_000,
                    },
                    {
                        "open": "11",
                        "high": "13",
                        "low": "10",
                        "close": "12",
                        "volume": "2",
                        "timestamp": 1_700_000_900_000,
                    },
                ]
            },
        }
        with mock.patch("plugins.trade.trademenu.marketdata._http_json", return_value=sample) as m:
            candles = fetch_raydium_candles("ZEC", "15m", limit=10)
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[-1]["close"], 12.0)
        body = m.call_args.kwargs.get("body") or m.call_args[1].get("body")
        if body is None and m.call_args[0]:
            # positional not used
            body = m.call_args.kwargs.get("body")
        # ensure orderly symbol in request
        called = m.call_args
        self.assertIn("public/query", called[0][0])
        self.assertEqual(called.kwargs.get("method") or called[1].get("method"), "POST")
        req_body = called.kwargs.get("body") if called.kwargs else called[1].get("body")
        self.assertEqual(req_body["symbol"], "PERP_ZEC_USDC")
        self.assertEqual(req_body["interval"], "15m")

    def test_empty_raises(self) -> None:
        with mock.patch(
            "plugins.trade.trademenu.marketdata._http_json",
            return_value={"success": True, "data": {"rows": []}},
        ):
            with self.assertRaises(RuntimeError):
                fetch_raydium_candles("BTC", "15m", limit=5)


class RaydiumPositionNormalizeTests(unittest.TestCase):
    def test_mark_and_pnl_from_entry(self) -> None:
        rows = [
            {
                "symbol": "PERP_ZEC_USDC",
                "position_qty": "1.5",
                "average_open_price": "1000",
                "mark_price": "1100",
            }
        ]
        out = rx._normalize_positions(rows, symbol_rules={})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].mark, "1100")
        self.assertEqual(Decimal(out[0].pnl), Decimal("150"))
        self.assertEqual(out[0].exchange_instrument, "PERP_ZEC_USDC")

    def test_missing_mark_does_not_fake_zero_pnl(self) -> None:
        rows = [
            {
                "symbol": "PERP_ZEC_USDC",
                "position_qty": "1",
                "average_open_price": "1000",
            }
        ]
        out = rx._normalize_positions(rows, symbol_rules={})
        self.assertIsNone(out[0].mark)
        self.assertEqual(out[0].pnl, "")

    def test_capabilities_include_writes(self) -> None:
        caps = set(rx.capabilities())
        for op in ("set_tp", "set_sl", "close_position", "cancel_order_group", "ladder", "new_order"):
            self.assertIn(op, caps)


class RaydiumAggregateTests(unittest.TestCase):
    def test_entry_limit_group(self) -> None:
        rows = [
            {"symbol": "PERP_ZEC_USDC", "side": "BUY", "status": "NEW", "order_price": "100", "quantity": "1", "order_id": 1},
            {"symbol": "PERP_ZEC_USDC", "side": "BUY", "status": "NEW", "order_price": "101", "quantity": "2", "order_id": 2},
        ]
        count, groups = rx._aggregate_orders(rows, symbol_rules={})
        self.assertEqual(count, 2)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].classification, "entry_limit")
        self.assertEqual(set(groups[0].order_ids or []), {1, 2})


if __name__ == "__main__":
    unittest.main()
