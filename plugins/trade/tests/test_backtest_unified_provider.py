from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from plugins.trade import backtest_wizard as wizard


class UnifiedBacktestWizardTests(unittest.TestCase):
    def test_dukascopy_url_uses_zero_padded_zero_based_month(self) -> None:
        self.assertEqual(
            "https://datafeed.dukascopy.com/datafeed/EURUSD/2025/00/02/BID_candles_min_1.bi5",
            wizard._dukascopy_artifact_url("eurusd", date(2025, 1, 2), "bid"),
        )
        self.assertEqual(
            "https://datafeed.dukascopy.com/datafeed/EURUSD/2025/09/02/ASK_candles_min_1.bi5",
            wizard._dukascopy_artifact_url("eurusd", date(2025, 10, 2), "ask"),
        )

    def test_source_first_route_preserves_binance_market_and_adds_dukascopy_side(self) -> None:
        key = ("unified",)
        screen = wizard.BacktestWizard().open(key)
        self.assertEqual("source", screen.state)
        local = wizard.BacktestWizard()
        local.open(key)
        self.assertEqual("ladder", local.handle_callback(key, "source:dukascopy").state)
        self.assertEqual("dukascopy_symbol", local.handle_callback(key, "ladder:buy").state)
        self.assertEqual("dukascopy_side", local.handle_callback(key, "dsymbol:xauusd").state)
        self.assertEqual("percentage", local.handle_callback(key, "dside:ask").state)

    def test_dukascopy_replay_uses_engine_but_suppresses_binance_aggressor_metrics(self) -> None:
        candles = [[1_700_000_000_000 + i * 60_000, "100", "101", "99", "100", "1", 0, "100"] for i in range(180)]
        fake_cache = mock.Mock(path=Path("/tmp/dukascopy-test.sqlite"))
        fake_provider = mock.Mock()
        fake_provider.fetch_range.return_value = candles
        local = wizard.BacktestWizard()
        state = wizard.State(data_source="dukascopy", ladder="buy", provider_instrument="xauusd", symbol="XAUUSD", price_side="ask", percentage=0.001)
        with mock.patch.object(wizard, "_initialize_backtest_dukascopy_cache", return_value=fake_cache), mock.patch.object(wizard, "DukascopyProvider", return_value=fake_provider), mock.patch.object(wizard, "_draw_jpg", return_value="/tmp/backtest.jpg"):
            screen = local._run_backtest(state)
        self.assertEqual("done", screen.state)
        self.assertIn("Source: Dukascopy", screen.text)
        self.assertIn("Dukascopy volume only; Binance taker-buy aggressive-flow metrics unavailable.", screen.text)
        self.assertNotIn("L-BVWAP", screen.text)
        fake_provider.fetch_range.assert_called_once()
        self.assertEqual("ask", fake_provider.fetch_range.call_args.args[0].price_side)


if __name__ == "__main__":
    unittest.main()
