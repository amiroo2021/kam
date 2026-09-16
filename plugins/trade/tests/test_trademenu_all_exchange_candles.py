"""TradeDesk candles operation + multi-exchange TradeMenu coverage."""

from __future__ import annotations

import unittest
from unittest import mock

from plugins.trade import candles as cmod
from plugins.trade.tradedesk import TradeDesk
from plugins.trade.trademenu import marketdata as md


class CandleRegistryTests(unittest.TestCase):
    def test_all_desk_exchanges_are_classified(self) -> None:
        desk = TradeDesk()
        for ex in desk.list_exchanges():
            native = cmod.has_native_candles(ex)
            caps = desk.capabilities(ex)
            if native:
                self.assertIn("candles", caps, msg=f"{ex} should advertise candles")
            else:
                self.assertNotIn("candles", caps, msg=f"{ex} must not advertise unsupported candles")
                self.assertIn(ex, cmod.UNSUPPORTED_NATIVE_CANDLES)

    def test_fetch_candles_routes_via_desk_not_phase1_switch(self) -> None:
        from pathlib import Path

        src = Path(md.__file__).read_text(encoding="utf-8")
        # Dispatch body must not keep the old Phase-1 per-exchange error string.
        self.assertNotIn("not implemented for exchange", src.lower())
        self.assertIn('operation": "candles"', src)
        self.assertIn("TradeDesk", src)

    def test_handle_rise_btc(self) -> None:
        fake = [{"time": 1, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 0.0}]
        with mock.patch.object(cmod, "fetch_for_exchange", return_value=fake) as fn:
            resp = cmod.handle_candles_operation("rise", "AMIROO", {"symbol": "BTC", "interval": "15m", "limit": 10})
        self.assertTrue(resp.success)
        self.assertEqual(resp.data["candles"], fake)
        fn.assert_called_once()

    def test_unsupported_exchange_clean_error(self) -> None:
        out = md.fetch_candles("lighter", "amiroo", "BTC", "15m", limit=10)
        self.assertFalse(out["success"])
        self.assertEqual(out["error"]["code"], "UNSUPPORTED_CANDLES")
        self.assertNotIn("Phase 1", out["error"]["message"])
        self.assertIn("lighter", out["error"]["message"].lower())

    def test_hip3_and_raydium_and_arcus_natives_still_in_fetchers(self) -> None:
        self.assertIn("hyperliquid", cmod.FETCHERS)
        self.assertIn("raydium", cmod.FETCHERS)
        self.assertIn("arcus", cmod.FETCHERS)
        self.assertIn("rise", cmod.FETCHERS)
        self.assertIn("pacifica", cmod.FETCHERS)


class RiseCandleUnitTests(unittest.TestCase):
    def test_rise_market_id_mapping(self) -> None:
        markets = {
            "data": {
                "markets": [
                    {"market_id": "1", "config": {"name": "BTC/USDC"}, "display_name": "BTC/USDC"},
                    {"market_id": "2", "config": {"name": "ETH/USDC"}, "display_name": "ETH/USDC"},
                ]
            }
        }
        cmod._RISE_MARKET_CACHE["ts"] = 0.0
        cmod._RISE_MARKET_CACHE["by_sym"] = {}
        with mock.patch.object(cmod, "_http_json", return_value=markets):
            self.assertEqual(cmod._rise_market_id("BTC", api_base="https://api.rise.trade"), "1")
            self.assertEqual(cmod._rise_market_id("BTC/USDC", api_base="https://api.rise.trade"), "1")
            self.assertEqual(cmod._rise_market_id("BTCUSD", api_base="https://api.rise.trade"), "1")
            self.assertEqual(cmod._rise_market_id("ETH", api_base="https://api.rise.trade"), "2")

    def test_rise_candle_normalize_ns(self) -> None:
        payload = {
            "data": {
                "data": [
                    {
                        "market_id": "1",
                        "interval": "15m",
                        "time": "1700000000000000000",
                        "open": "100",
                        "high": "110",
                        "low": "90",
                        "close": "105",
                        "volume": "1",
                    }
                ]
            }
        }
        with mock.patch.object(cmod, "_rise_market_id", return_value="1"):
            with mock.patch.object(cmod, "_http_json", return_value=payload):
                rows = cmod.fetch_rise("BTC", "15m", limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["time"], 1_700_000_000)
        self.assertEqual(rows[0]["close"], 105.0)


if __name__ == "__main__":
    unittest.main()
