"""Batch 1 canonical get_tickers migration tests.

Offline-only tests for Rise, Perpl, and Pacifica. They verify the new read-only
bulk ticker operation uses existing bulk/cache sources and does not introduce
per-symbol fan-out or trading/write calls.
"""

from __future__ import annotations

import importlib
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.canonical import CanonicalMarketPrice, CanonicalTickersBatch


_WRITE_OPS = {
    "new_order",
    "ladder",
    "cancel_order_group",
    "cancel_orders",
    "cancel_order",
    "set_tp",
    "set_sl",
    "close_position",
    "market_immediate",
}


class _Batch1Assertions(unittest.TestCase):
    agent: Any
    exchange: str

    def assert_tickers_response(self, response: Any) -> CanonicalTickersBatch:
        self.assertTrue(response.success, response.to_dict() if hasattr(response, "to_dict") else response)
        self.assertEqual(response.operation, "get_tickers")
        self.assertIsInstance(response.tickers_batch, CanonicalTickersBatch)
        return response.tickers_batch

    def assert_no_write_capabilities_changed(self, before: List[str], after: List[str]) -> None:
        self.assertEqual(set(before) | {"get_tickers"}, set(after))
        self.assertEqual(set(before) & _WRITE_OPS, set(after) & _WRITE_OPS)

    def assert_none_not_zero(self, mp: CanonicalMarketPrice, fields: List[str]) -> None:
        data = mp.to_dict()
        for field in fields:
            self.assertIsNone(data[field], f"{self.exchange} {field} should remain None, not zero/fabricated")


class TestRiseGetTickersOffline(_Batch1Assertions):
    exchange = "rise"

    def setUp(self) -> None:
        self.agent = importlib.reload(importlib.import_module("plugins.trade.agents.x_rise_agent"))

    def _markets_payload(self) -> Dict[str, Any]:
        return {
            "markets": [
                {
                    "market_id": "1",
                    "display_name": "Bitcoin Perp",
                    "base_asset_symbol": "BTC",
                    "quote_asset_symbol": "USD",
                    "last_price": "70000.5",
                    "turnover_24h": "1234567",
                    "volume_24h_quote": "1234567",
                    "volume_24h_base": "17.5",
                    "price_change_24h_pct": "1.25",
                    "funding_rate": "0.0001",
                    "open_interest": "42",
                    "config": {
                        "name": "BTC-PERP",
                        "market_type": "perp",
                        "step_price": "0.1",
                        "step_size": "0.001",
                        "min_order_size": "0.001",
                    },
                },
                {
                    "market_id": "2",
                    "display_name": "Ether Perp",
                    "base_asset_symbol": "ETH",
                    "quote_asset_symbol": "USD",
                    "last_price": None,
                    "config": {
                        "name": "ETH-PERP",
                        "market_type": "perp",
                        "step_price": "0.01",
                        "step_size": "0.01",
                        "min_order_size": "0.01",
                    },
                },
            ]
        }

    def test_get_tickers_capability_dispatches_read_only_bulk_payload(self) -> None:
        before = [c for c in self.agent.capabilities() if c != "get_tickers"]
        self.assertIn("get_tickers", self.agent.capabilities())
        self.assert_no_write_capabilities_changed(before, self.agent.capabilities())

        with mock.patch.object(self.agent, "_fetch_markets_payload", return_value=self._markets_payload()) as fetch, \
             mock.patch.object(self.agent, "_post_json", side_effect=AssertionError("write path called")), \
             mock.patch.object(self.agent, "_execute_market_immediate", side_effect=AssertionError("write path called")):
            batch = self.assert_tickers_response(
                self.agent.execute({"operation": "get_tickers", "account": "acct"})
            )

        fetch.assert_called_once()
        self.assertEqual(set(batch.tickers), {"BTC-PERP", "ETH-PERP"})
        btc = batch.tickers["BTC-PERP"]
        self.assertEqual(btc.symbol, "BTC-PERP")
        self.assertEqual(btc.native_symbol, "BTC-PERP")
        self.assertEqual(btc.display_symbol, "BTC")
        self.assertEqual(btc.display_name, "Bitcoin Perp")
        self.assertEqual(btc.base, "BTC")
        self.assertEqual(btc.quote, "USD")
        self.assertEqual(btc.market_type, "perp")
        self.assertEqual(btc.mark_price, "70000.5")
        self.assertEqual(btc.price_increment, "0.1")
        self.assertEqual(btc.size_increment, "0.001")
        self.assertEqual(btc.minimum_size, "0.001")
        self.assertEqual(btc.turnover_24h, "1234567")
        self.assertEqual(btc.volume_24h_quote, "1234567")
        self.assertEqual(btc.volume_24h_base, "17.5")
        self.assertEqual(btc.change_24h_pct, "1.25")
        self.assertEqual(btc.funding_rate, "0.0001")
        self.assertEqual(btc.open_interest, "42")
        self.assert_none_not_zero(batch.tickers["ETH-PERP"], [
            "mark_price", "turnover_24h", "volume_24h_quote", "volume_24h_base",
            "change_24h_pct", "funding_rate", "open_interest", "minimum_notional",
        ])

    def test_get_tickers_symbol_filter_and_existing_reads_still_work(self) -> None:
        with mock.patch.object(self.agent, "_fetch_markets_payload", return_value=self._markets_payload()), \
             mock.patch.object(self.agent, "_get_json", return_value=self._markets_payload()):
            batch = self.assert_tickers_response(
                self.agent.execute({"operation": "get_tickers", "account": "acct", "symbols": ["ETH", "ETH-PERP"]})
            )
            self.assertEqual(list(batch.tickers), ["ETH-PERP"])
            list_resp = self.agent.execute({"operation": "list_instruments", "account": "acct"})
            price_resp = self.agent.execute({"operation": "market_price", "account": "acct", "symbol": "BTC-PERP"})
        self.assertTrue(list_resp.success)
        self.assertTrue(price_resp.success)
        self.assertEqual(price_resp.market_price.mark_price, "70000.5")


class TestPerplGetTickersOffline(_Batch1Assertions):
    exchange = "perpl"

    def setUp(self) -> None:
        self.agent = importlib.reload(importlib.import_module("plugins.trade.agents.x_perpl_agent"))

    def _context(self) -> Dict[str, Any]:
        return {
            "markets": [
                {
                    "id": 1,
                    "name": "BTC",
                    "config": {"price_decimals": 2, "size_decimals": 4},
                    "state": {
                        "mrk": "7000012",
                        "vol24h_quote": "555000",
                        "vol24h_base": "7.9",
                        "change24h": "-0.5",
                        "funding": "0.0002",
                        "open_interest": "99",
                    },
                },
                {
                    "id": 2,
                    "name": "ETH",
                    "config": {"price_decimals": 1, "size_decimals": 3},
                    "state": {},
                },
            ]
        }

    def test_get_tickers_reuses_cached_context_without_per_symbol_http(self) -> None:
        before = [c for c in self.agent.capabilities() if c != "get_tickers"]
        self.assertIn("get_tickers", self.agent.capabilities())
        self.assert_no_write_capabilities_changed(before, self.agent.capabilities())

        with mock.patch.object(self.agent, "_credentials", return_value={"account": "ACCT", "api_url": "https://perpl.test"}), \
             mock.patch.object(self.agent, "_fetch_context", return_value=self._context()) as fetch_context, \
             mock.patch.object(self.agent, "_ws_send_orders", side_effect=AssertionError("write path called")), \
             mock.patch.object(self.agent, "_http", side_effect=AssertionError("unexpected per-symbol HTTP")):
            batch = self.assert_tickers_response(
                self.agent.execute({"operation": "get_tickers", "account": "acct"})
            )

        fetch_context.assert_called_once_with("https://perpl.test")
        self.assertEqual(set(batch.tickers), {"BTC", "ETH"})
        btc = batch.tickers["BTC"]
        self.assertEqual(btc.symbol, "BTC")
        self.assertEqual(btc.native_symbol, "1")
        self.assertEqual(btc.display_symbol, "BTC")
        self.assertEqual(btc.base, "BTC")
        self.assertEqual(btc.quote, "USDC")
        self.assertEqual(btc.market_type, "perp")
        self.assertEqual(btc.mark_price, "70000.12")
        self.assertEqual(btc.price_increment, "0.01")
        self.assertEqual(btc.size_increment, "0.0001")
        self.assertEqual(btc.volume_24h_quote, "555000")
        self.assertEqual(btc.volume_24h_base, "7.9")
        self.assertEqual(btc.change_24h_pct, "-0.5")
        self.assertEqual(btc.funding_rate, "0.0002")
        self.assertEqual(btc.open_interest, "99")
        self.assert_none_not_zero(batch.tickers["ETH"], [
            "mark_price", "turnover_24h", "volume_24h_quote", "volume_24h_base",
            "change_24h_pct", "funding_rate", "open_interest", "minimum_size", "minimum_notional",
        ])

    def test_get_tickers_symbol_filter_and_existing_reads_still_work(self) -> None:
        with mock.patch.object(self.agent, "_credentials", return_value={"account": "ACCT", "api_url": "https://perpl.test"}), \
             mock.patch.object(self.agent, "_fetch_context", return_value=self._context()):
            batch = self.assert_tickers_response(
                self.agent.execute({"operation": "get_tickers", "account": "acct", "symbols": ["ETH"]})
            )
            self.assertEqual(list(batch.tickers), ["ETH"])
            list_resp = self.agent.execute({"operation": "list_instruments", "account": "acct"})
            price_resp = self.agent.execute({"operation": "market_price", "account": "acct", "symbol": "BTC"})
        self.assertTrue(list_resp.success)
        self.assertTrue(price_resp.success)
        self.assertEqual(price_resp.market_price.mark_price, "70000.12")


class TestPacificaGetTickersOffline(_Batch1Assertions):
    exchange = "pacifica"

    def setUp(self) -> None:
        self.agent = importlib.reload(importlib.import_module("plugins.trade.agents.x_pacifica_agent"))

    def _info_rows(self) -> List[Dict[str, Any]]:
        return [
            {
                "symbol": "BTC",
                "display_name": "BTC Perp",
                "base": "BTC",
                "quote": "USDC",
                "market_type": "perp",
                "quote_tick": "0.1",
                "base_tick": "0.001",
                "base_min": "0.001",
                "min_notional": "10",
                "volume_24h_quote": "888000",
                "volume_24h_base": "12.5",
                "price_change_24h_pct": "2.5",
                "funding_rate": "0.0003",
                "open_interest": "321",
            },
            {"symbol": "ETH", "base": "ETH", "quote": "USDC", "market_type": "perp"},
        ]

    def test_get_tickers_composes_info_and_prices_without_per_symbol_lookup(self) -> None:
        before = [c for c in self.agent.capabilities() if c != "get_tickers"]
        self.assertIn("get_tickers", self.agent.capabilities())
        self.assert_no_write_capabilities_changed(before, self.agent.capabilities())

        with mock.patch.object(self.agent, "_pacifica_all_market_rows", return_value=self._info_rows()) as info, \
             mock.patch.object(self.agent, "_get_mark_prices", return_value={"BTC": Decimal("70000.25")}) as prices, \
             mock.patch.object(self.agent, "_get_market_info", side_effect=AssertionError("per-symbol info lookup called")), \
             mock.patch.object(self.agent, "_post_signed", side_effect=AssertionError("write path called")):
            batch = self.assert_tickers_response(
                self.agent.execute({"operation": "get_tickers", "account": "acct"})
            )

        info.assert_called_once()
        prices.assert_called_once()
        self.assertEqual(set(batch.tickers), {"BTC", "ETH"})
        btc = batch.tickers["BTC"]
        self.assertEqual(btc.symbol, "BTC")
        self.assertEqual(btc.native_symbol, "BTC")
        self.assertEqual(btc.display_symbol, "BTC")
        self.assertEqual(btc.display_name, "BTC Perp")
        self.assertEqual(btc.base, "BTC")
        self.assertEqual(btc.quote, "USDC")
        self.assertEqual(btc.market_type, "perp")
        self.assertEqual(btc.mark_price, "70000.25")
        self.assertEqual(btc.price_increment, "0.1")
        self.assertEqual(btc.size_increment, "0.001")
        self.assertEqual(btc.minimum_size, "0.001")
        self.assertEqual(btc.minimum_notional, "10")
        self.assertEqual(btc.volume_24h_quote, "888000")
        self.assertEqual(btc.volume_24h_base, "12.5")
        self.assertEqual(btc.change_24h_pct, "2.5")
        self.assertEqual(btc.funding_rate, "0.0003")
        self.assertEqual(btc.open_interest, "321")
        self.assert_none_not_zero(batch.tickers["ETH"], [
            "mark_price", "turnover_24h", "volume_24h_quote", "volume_24h_base",
            "change_24h_pct", "funding_rate", "open_interest", "minimum_size", "minimum_notional",
        ])

    def test_get_tickers_symbol_filter_and_existing_reads_still_work(self) -> None:
        with mock.patch.object(self.agent, "_pacifica_all_market_rows", return_value=self._info_rows()), \
             mock.patch.object(self.agent, "_get_mark_prices", return_value={"BTC": Decimal("70000.25")}), \
             mock.patch.object(self.agent, "_get_market_info", return_value={"symbol": "BTC"}):
            batch = self.assert_tickers_response(
                self.agent.execute({"operation": "get_tickers", "account": "acct", "symbols": ["ETH"]})
            )
            self.assertEqual(list(batch.tickers), ["ETH"])
            list_resp = self.agent.execute({"operation": "list_instruments", "account": "acct"})
            price_resp = self.agent.execute({"operation": "market_price", "account": "acct", "symbol": "BTC"})
        self.assertTrue(list_resp.success)
        self.assertTrue(price_resp.success)
        self.assertEqual(price_resp.market_price.mark_price, "70000.25")


if __name__ == "__main__":
    unittest.main()
