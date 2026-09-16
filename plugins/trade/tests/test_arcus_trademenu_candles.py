"""Arcus TradeMenu candles + native position overlay matching."""

from __future__ import annotations

import unittest
from unittest import mock

from plugins.trade.agents import x_arcus_agent as arcus
from plugins.trade.canonical import CanonicalPosition
from plugins.trade.trademenu import marketdata as md


class ArcusCandleFetchTests(unittest.TestCase):
    def test_arcus_market_preserves_btc_usd(self) -> None:
        self.assertEqual(md._arcus_market_symbol("BTC-USD"), "BTC-USD")
        self.assertEqual(md._arcus_market_symbol("btc-usd"), "BTC-USD")
        self.assertEqual(md._arcus_market_symbol("ETH/USD"), "ETH-USD")
        # Must not become BTCUSD / BTCUSDT
        self.assertNotIn("BTCUSD", md._arcus_market_symbol("BTC-USD"))
        self.assertNotIn("USDT", md._arcus_market_symbol("BTC-USD"))

    def test_fetch_arcus_candles_maps_countback_and_us_open_time(self) -> None:
        payload = {
            "candles": [
                {
                    "marketDisplayName": "BTC-USD",
                    "openTime": 1_700_000_000_000_000,  # µs
                    "open": "100",
                    "high": "110",
                    "low": "90",
                    "close": "105",
                    "volume": "1.5",
                },
                {
                    "marketDisplayName": "BTC-USD",
                    "openTime": 1_700_000_900_000_000,
                    "open": "105",
                    "high": "120",
                    "low": "100",
                    "close": "118",
                    "volume": "2",
                },
            ]
        }
        with mock.patch.object(md, "_http_json", return_value=payload) as http:
            rows = md.fetch_arcus_candles("BTC-USD", "15m", limit=50)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["time"], 1_700_000_000)
        self.assertEqual(rows[0]["open"], 100.0)
        self.assertEqual(rows[1]["close"], 118.0)
        self.assertLess(rows[0]["time"], rows[1]["time"])
        called_url = http.call_args[0][0]
        self.assertIn("/v1/candles?", called_url)
        self.assertIn("market=BTC-USD", called_url)
        self.assertIn("timeframe=15m", called_url)
        self.assertIn("countback=50", called_url)
        self.assertIn("to=", called_url)

    def test_fetch_candles_arcus_dispatch(self) -> None:
        fake = [{"time": 1, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 0.0}]
        with mock.patch.object(md, "fetch_arcus_candles", return_value=fake) as fn:
            out = md.fetch_candles("arcus", "amiroo", "BTC-USD", "15m", limit=10)
        fn.assert_called_once_with("BTC-USD", "15m", limit=10)
        self.assertTrue(out["success"])
        self.assertEqual(out["candles"], fake)

    def test_interval_1d_maps_lowercase(self) -> None:
        with mock.patch.object(md, "_http_json", return_value={"candles": []}) as http:
            with self.assertRaises(RuntimeError):
                md.fetch_arcus_candles("ETH-USD", "1D", limit=5)
        self.assertIn("timeframe=1d", http.call_args[0][0])


class ArcusPositionNativeTests(unittest.TestCase):
    def test_normalize_positions_sets_exchange_instrument_and_mark(self) -> None:
        positions_payload = {
            "1": {
                "marketDisplayName": "BTC-USD",
                "side": "long",
                "size": "20",
                "averageEntryPrice": "76156.7",
                "markPx": "76203.1",
                "unrealizedPnl": "12.5",
            }
        }
        protections = {"BTC-USD": {"tp": "77500", "sl": "74500", "tp_count": 1, "sl_count": 1}}
        rows = arcus._normalize_positions(positions_payload, protections)
        self.assertEqual(len(rows), 1)
        p = rows[0]
        self.assertIsInstance(p, CanonicalPosition)
        self.assertEqual(p.symbol, "BTC-USD")
        self.assertEqual(p.exchange_instrument, "BTC-USD")
        self.assertEqual(p.side, "long")
        self.assertEqual(p.entry_price, "76156.7")
        self.assertEqual(p.mark, "76203.1")
        self.assertEqual(p.tp, "77500")
        self.assertEqual(p.sl, "74500")

    def test_aggregate_orders_sets_exchange_instrument(self) -> None:
        orders = [
            {
                "marketDisplayName": "BTC-USD",
                "side": "sell",
                "remainingSize": "1",
                "price": "77000",
            },
            {
                "marketDisplayName": "ETH-USD",
                "side": "sell",
                "remainingSize": "2",
                "price": "2500",
            },
        ]
        count, groups = arcus._aggregate_orders(orders)
        self.assertEqual(count, 2)
        by_sym = {g.symbol: g for g in groups}
        self.assertEqual(by_sym["BTC-USD"].exchange_instrument, "BTC-USD")
        self.assertEqual(by_sym["ETH-USD"].exchange_instrument, "ETH-USD")


class ArcusOverlayMatchSemanticsTests(unittest.TestCase):
    """Mirror frontend exact-native priority used by updateOverlay."""

    @staticmethod
    def _match(pos_sym, native, requested, row_native=None) -> bool:
        candidates = [c for c in (str(pos_sym or "").strip(), str(row_native or "").strip()) if c]
        targets = [t for t in (str(native or "").strip(), str(requested or "").strip()) if t]
        if not candidates or not targets:
            return False
        for c in candidates:
            for t in targets:
                if c.upper() == t.upper():
                    return True
        return False

    def test_exact_btc_usd_native_match(self) -> None:
        self.assertTrue(self._match("BTC-USD", "BTC-USD", "BTC-USD", "BTC-USD"))
        self.assertTrue(self._match("BTC-USD", "BTC-USD", "BTC-USD", None))
        self.assertFalse(self._match("ETH-USD", "BTC-USD", "BTC-USD", "ETH-USD"))

    def test_overlay_fields_from_position(self) -> None:
        p = {
            "symbol": "BTC-USD",
            "native_symbol": "BTC-USD",
            "side": "long",
            "entry": "76156.7",
            "tp": "77500",
            "sl": "74500",
        }
        self.assertTrue(self._match(p["symbol"], "BTC-USD", "BTC-USD", p["native_symbol"]))
        self.assertEqual(p["entry"], "76156.7")
        self.assertEqual(p["sl"], "74500")
        self.assertEqual(p["tp"], "77500")


if __name__ == "__main__":
    unittest.main()
