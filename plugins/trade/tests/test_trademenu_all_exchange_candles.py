"""TradeDesk candles operation + multi-exchange TradeMenu coverage."""

from __future__ import annotations

import unittest
from unittest import mock

from plugins.trade import candles as cmod
from plugins.trade.agents import x_perpl_agent as pmod
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

    def test_rise_trade_history_aggregates_market_pages_then_resamples(self) -> None:
        page1 = {
            "data": {
                "market_id": "1",
                "trades": [
                    {"id": "t3", "time": 1_700_000_050_000_000_000, "price": "102", "size": "3"},
                    {"id": "t2", "time": 1_700_000_030_000_000_000, "price": "105", "size": "2"},
                    {"id": "t1", "time": 1_700_000_000_000_000_000, "price": "100", "size": "1"},
                ],
            }
        }
        page2 = {"data": {"market_id": "1", "trades": []}}

        def fake_http_json(url, timeout=30):  # noqa: ARG001
            if "page=1" in url:
                return page1
            if "page=2" in url:
                return page2
            raise AssertionError(url)

        cmod._RISE_TRADE_CACHE.update({"market_id": "", "ts": 0.0, "rows": []})
        with mock.patch.object(cmod, "_rise_market_id", return_value="1"), \
             mock.patch.object(cmod, "_http_json", side_effect=fake_http_json):
            rows = cmod.fetch_rise("BTC", "15m", limit=10)
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[0]["open"], 100.0)
        self.assertEqual(rows[0]["high"], 105.0)
        self.assertEqual(rows[0]["low"], 100.0)
        self.assertEqual(rows[0]["close"], 102.0)
        self.assertEqual(rows[0]["volume"], 6.0)

    def test_rise_alias_keys_cover_plain_and_quote_suffixes(self) -> None:
        self.assertIn("ZEC", cmod._rise_alias_keys("ZECUSDC"))
        self.assertIn("ZEC", cmod._rise_alias_keys("ZEC-USDC"))
        self.assertIn("ZEC", cmod._rise_alias_keys("ZEC/USDC"))
        self.assertIn("ZEC", cmod._rise_alias_keys("ZEC"))

    def test_perpl_candles_use_authenticated_market_data(self) -> None:
        creds = pmod._credentials("BITGET")
        self.assertIsNotNone(creds)
        with mock.patch.object(pmod, "_markets_index", return_value={50: {"id": 50, "name": "ZEC", "price_decimals": 3, "size_decimals": 3}}), \
             mock.patch.object(pmod, "_match_market", return_value={"id": 50, "name": "ZEC", "price_decimals": 3, "size_decimals": 3}), \
             mock.patch.object(pmod, "_signed_request", return_value=(200, {"d": [{"t": 1700000000000, "o": 100, "h": 110, "l": 90, "c": 105, "v": "0"}]}, "")):
            rows = cmod.fetch_perpl("ZEC", "15m", limit=10, account="BITGET")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["open"], 0.1)
        self.assertEqual(rows[0]["close"], 0.105)
        self.assertEqual(rows[0]["volume"], 0.0)

    def test_pacifica_candles_use_public_kline_endpoint(self) -> None:
        payload = {"success": True, "data": [{"t": 1700000000000, "o": "1.0", "h": "2.0", "l": "0.5", "c": "1.5", "v": "3.0"}]}
        with mock.patch.object(cmod, "_http_json", return_value=payload):
            rows = cmod.fetch_pacifica("ZEC", "15m", limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["open"], 1.0)
        self.assertEqual(rows[0]["high"], 2.0)
        self.assertEqual(rows[0]["low"], 0.5)
        self.assertEqual(rows[0]["close"], 1.5)
        self.assertEqual(rows[0]["volume"], 3.0)

    def test_nado_candles_use_archive_indexer(self) -> None:
        symbols = {
            "ZEC-PERP": {"product_id": 18, "symbol": "ZEC-PERP"},
            "BTC-PERP": {"product_id": 1, "symbol": "BTC-PERP"},
        }
        archive = {"candlesticks": [{"timestamp": "1700000000", "open_x18": "1000000000000000000", "high_x18": "2000000000000000000", "low_x18": "500000000000000000", "close_x18": "1500000000000000000", "volume": "3000000000000000000"}]}
        def fake_query(payload, *, base="https://api.prod.nado.xyz/gateway/v1"):
            if payload.get("type") == "symbols":
                return {"status": "success", "data": {"symbols": symbols}}
            if payload.get("candlesticks"):
                return archive
            raise AssertionError(payload)
        with mock.patch.object(cmod, "_nado_gateway_query", side_effect=fake_query):
            rows = cmod.fetch_nado("ZEC-PERP", "1m", limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["open"], 1.0)
        self.assertEqual(rows[0]["high"], 2.0)
        self.assertEqual(rows[0]["low"], 0.5)
        self.assertEqual(rows[0]["close"], 1.5)
        self.assertEqual(rows[0]["volume"], 3.0)


if __name__ == "__main__":
    unittest.main()
