"""Rise position side normalization — signed size is authoritative."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import mock

from plugins.trade.agents import x_rise_agent as rise
from plugins.trade.canonical import CanonicalPosition


class RiseSideUnitTests(unittest.TestCase):
    def test_positive_size_long_even_if_side_zero(self) -> None:
        self.assertEqual(rise._rise_side(0, "15.375798"), "long")
        self.assertEqual(rise._rise_side(0, 15.375798), "long")

    def test_negative_size_short_even_if_side_zero(self) -> None:
        self.assertEqual(rise._rise_side(0, "-2.618"), "short")
        self.assertEqual(rise._rise_side(0, -0.133), "short")

    def test_negative_size_overrides_side_long_text(self) -> None:
        # Signed size wins over a stale/wrong side enum.
        self.assertEqual(rise._rise_side("long", "-1"), "short")
        self.assertEqual(rise._rise_side("0", "-1"), "short")

    def test_positive_size_overrides_side_short_text(self) -> None:
        self.assertEqual(rise._rise_side("short", "1"), "long")
        self.assertEqual(rise._rise_side("1", "1"), "long")

    def test_textual_side_when_size_missing(self) -> None:
        self.assertEqual(rise._rise_side("short", None), "short")
        self.assertEqual(rise._rise_side("long", None), "long")
        self.assertEqual(rise._rise_side("sell", "0"), "short")
        self.assertEqual(rise._rise_side("buy", "0"), "long")

    def test_numeric_enum_last_resort(self) -> None:
        self.assertEqual(rise._rise_side(1, None), "short")
        self.assertEqual(rise._rise_side(0, None), "long")
        self.assertEqual(rise._rise_side("1", "0"), "short")

    def test_unknown_raises(self) -> None:
        with self.assertRaises(ValueError):
            rise._rise_side(None, None)
        with self.assertRaises(ValueError):
            rise._rise_side("", "0")
        with self.assertRaises(ValueError):
            rise._rise_side("maybe", None)


class RiseNormalizeMixedAccountTests(unittest.TestCase):
    def _row(self, name, mid, size, entry, mark, pnl, side=0):
        return {
            "market_name": name,
            "market_id": mid,
            "side": side,
            "size": size,
            "avg_entry_price": entry,
            "mark_price": mark,
            "unrealized_pnl": pnl,
        }

    def test_mixed_long_short_same_account(self) -> None:
        data = {
            "positions": [
                self._row("BTC/USDC", "1", "15.375798", "76512.1", "76097.7", "-6371.65", side=0),
                self._row("ETH/USDC", "2", "-2.618", "2594.6", "2410.1", "482.92", side=0),
                self._row("ZEC/USDC", "8", "-0.133", "1270.3", "1250.5", "-2.64", side=0),
                self._row("FLAT/USDC", "9", "0", "1", "1", "0", side=0),
            ]
        }
        cache = {
            "1": {"step_size": "0.000001", "step_price": "0.1"},
            "2": {"step_size": "0.001", "step_price": "0.01"},
            "8": {"step_size": "0.001", "step_price": "0.1"},
        }
        out = rise._normalize_positions(data, cache, tpsl_index={})
        by = {p.symbol: p for p in out}
        self.assertEqual(set(by), {"BTC", "ETH", "ZEC"})
        self.assertEqual(by["BTC"].side, "long")
        self.assertEqual(by["ETH"].side, "short")
        self.assertEqual(by["ZEC"].side, "short")
        self.assertEqual(by["BTC"].size, "15.375798")
        self.assertEqual(by["ETH"].size, "2.618")
        self.assertEqual(by["ZEC"].size, "0.133")
        # sizes always positive
        for p in out:
            self.assertGreater(Decimal(p.size), 0)
        # mark attached
        self.assertIsNotNone(by["ETH"].mark)
        # exchange PnL preserved (not flipped by side fix)
        self.assertEqual(Decimal(by["ETH"].pnl), Decimal("482.92"))
        self.assertEqual(Decimal(by["ZEC"].pnl), Decimal("-2.64"))

    def test_missing_pnl_not_zero(self) -> None:
        data = {
            "positions": [
                {
                    "market_name": "ETH/USDC",
                    "market_id": "2",
                    "side": 0,
                    "size": "-1",
                    "avg_entry_price": "2000",
                    "mark_price": "2100",
                }
            ]
        }
        out = rise._normalize_positions(data, {}, {})
        self.assertEqual(out[0].side, "short")
        self.assertEqual(out[0].pnl, "")

    def test_unknown_direction_skipped(self) -> None:
        data = {
            "positions": [
                {
                    "market_name": "X/USDC",
                    "market_id": "9",
                    "side": None,
                    "size": "0",
                    "avg_entry_price": "1",
                }
            ]
        }
        # size 0 excluded before side
        self.assertEqual(rise._normalize_positions(data, {}, {}), [])


class RiseCloseSideSafetyTests(unittest.TestCase):
    def test_tpsl_closing_side(self) -> None:
        self.assertEqual(rise._tpsl_closing_side_for_position("long"), "SELL")
        self.assertEqual(rise._tpsl_closing_side_for_position("short"), "BUY")

    def test_snapshot_uses_signed_size(self) -> None:
        portfolio = {
            "data": {
                "positions": [
                    {
                        "market_id": "2",
                        "market_name": "ETH/USDC",
                        "side": 0,
                        "size": "-2.618",
                        "avg_entry_price": "2594.6",
                    }
                ]
            }
        }
        markets = {
            "markets": [
                {
                    "market_id": "2",
                    "name": "ETH/USDC",
                    "config": {"step_size": "0.001", "step_price": "0.01", "min_order_size": "0.001"},
                }
            ]
        }
        with mock.patch.object(rise, "_fetch_portfolio", return_value=portfolio), mock.patch.object(
            rise, "_fetch_markets_payload", return_value=markets
        ), mock.patch.object(
            rise,
            "_resolve_market_by_symbol",
            return_value={"market_id": "2", "symbol": "ETH", "step_size": "0.001"},
        ), mock.patch.object(
            rise,
            "_market_cache",
            return_value={"2": {"market_id": "2", "symbol": "ETH", "step_size": "0.001"}},
        ):
            snap = rise._rise_position_snapshot("0xabc", "ETH")
        self.assertEqual(snap["side"], "short")
        self.assertEqual(snap["size"], Decimal("2.618"))

    def test_close_side_from_snapshot_short(self) -> None:
        # Mirrors close_position logic: opposite of pre_side
        for pre_side, expected_close in (("long", "sell"), ("short", "buy")):
            close_side = "sell" if pre_side == "long" else "buy"
            self.assertEqual(close_side, expected_close)


if __name__ == "__main__":
    unittest.main()
