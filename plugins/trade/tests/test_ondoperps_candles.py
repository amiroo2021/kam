"""OndoPerps TradeMenu candles via authenticated GET /v1/perps/candles."""

from __future__ import annotations

import unittest
from unittest import mock

from plugins.trade.agents import x_ondoperps_agent as ondo
from plugins.trade.canonical import make_success
from plugins.trade.tradedesk import TradeDesk
from plugins.trade.trademenu.marketdata import fetch_candles


class OndoCandlesUnitTests(unittest.TestCase):
    def test_capabilities_advertise_candles(self) -> None:
        self.assertIn("candles", ondo.capabilities())

    def test_execute_candles_maps_btc_to_market_and_normalizes(self) -> None:
        creds = {
            "account": "amiroo",
            "api_key": "k",
            "api_secret": "s",
            "base_url": "https://api.ondoperps.xyz",
        }
        meta = {
            "market": "BTC-USD.P",
            "quote_increment": "0.1",
            "base_increment": "0.0001",
        }
        rows = [
            {
                "startTime": "2026-09-16T12:00:00Z",
                "open": "100",
                "high": "110",
                "low": "90",
                "close": "105",
                "volume": "1.5",
            },
            {
                "startTime": "2026-09-16T12:15:00Z",
                "open": "105",
                "high": "120",
                "low": "100",
                "close": "118",
                "volume": "2",
            },
        ]
        with mock.patch.object(ondo, "_lookup_credentials", return_value=creds):
            with mock.patch.object(ondo, "_resolve_market_metadata", return_value=(meta, None)):
                with mock.patch.object(ondo, "_signed_get", return_value=rows) as get:
                    resp = ondo.execute(
                        {
                            "operation": "candles",
                            "account": "amiroo",
                            "symbol": "BTC-USD.P",
                            "interval": "15m",
                            "limit": 50,
                        }
                    )
        self.assertTrue(resp.success)
        candles = resp.data["candles"]
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0]["open"], 100.0)
        self.assertEqual(candles[1]["close"], 118.0)
        self.assertLess(candles[0]["time"], candles[1]["time"])
        self.assertEqual(resp.data["native_symbol"], "BTC-USD.P")
        path = get.call_args[0][1]
        self.assertIn("/v1/perps/candles?", path)
        self.assertIn("market=BTC-USD.P", path)
        self.assertIn("resolution=15", path)

    def test_friendly_btc_resolves_wire_market(self) -> None:
        creds = {
            "account": "amiroo",
            "api_key": "k",
            "api_secret": "s",
            "base_url": "https://api.ondoperps.xyz",
        }
        meta = {"market": "BTC-USD.P"}
        with mock.patch.object(ondo, "_lookup_credentials", return_value=creds):
            with mock.patch.object(ondo, "_resolve_market_metadata", return_value=(meta, None)):
                with mock.patch.object(
                    ondo,
                    "_signed_get",
                    return_value=[
                        {
                            "startTime": "2026-09-16T12:00:00Z",
                            "open": "1",
                            "high": "2",
                            "low": "0.5",
                            "close": "1.5",
                            "volume": "0",
                        }
                    ],
                ) as get:
                    resp = ondo.execute(
                        {
                            "operation": "candles",
                            "account": "amiroo",
                            "symbol": "BTC",
                            "interval": "15m",
                            "limit": 10,
                        }
                    )
        self.assertTrue(resp.success)
        self.assertIn("market=BTC-USD.P", get.call_args[0][1])


class OndoCandlesLiveReadOnly(unittest.TestCase):
    """Live RO against configured amiroo — skip if account missing."""

    @classmethod
    def setUpClass(cls) -> None:
        desk = TradeDesk()
        cls.desk = desk
        cls.skip = "ondoperps" not in desk.list_exchanges() or not desk.list_accounts(
            "ondoperps"
        )

    def test_live_btc_eth_sol_candles(self) -> None:
        if self.skip:
            self.skipTest("ondoperps not configured")
        acct = self.desk.list_accounts("ondoperps")[0]
        if isinstance(acct, dict):
            acct = acct.get("account")
        for friendly, native in (
            ("BTC", "BTC-USD.P"),
            ("BTC-USD.P", "BTC-USD.P"),
            ("ETH", "ETH-USD.P"),
            ("SOL", "SOL-USD.P"),
        ):
            r = self.desk.execute(
                {
                    "operation": "resolve_instrument",
                    "exchange": "ondoperps",
                    "account": acct,
                    "symbol": friendly,
                }
            )
            self.assertTrue(r.success, (friendly, r))
            self.assertEqual(r.instrument.symbol, native)
            out = fetch_candles("ondoperps", acct, native, "15m", limit=50)
            self.assertTrue(out["success"], (native, out.get("error")))
            bars = out.get("candles") or []
            self.assertGreater(len(bars), 0, native)
            self.assertIn("close", bars[-1])


if __name__ == "__main__":
    unittest.main()
