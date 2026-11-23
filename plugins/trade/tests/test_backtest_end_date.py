from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from plugins.trade import backtest_wizard as wizard
from goldenfibo.marketdata.historical import HistoricalRequest
from goldenfibo.marketdata.dukascopy_provider import DukascopyNoDataError


class BacktestEndDateTests(unittest.TestCase):
    def test_start_date_offers_today_by_default_and_custom_end_date(self) -> None:
        local = wizard.BacktestWizard()
        key = ("end-date",)
        local.open(key)
        local.handle_callback(key, "source:dukascopy")
        local.handle_callback(key, "ladder:sell")
        local.handle_callback(key, "dsymbol:xauusd")
        local.handle_callback(key, "dside:bid")
        local.handle_callback(key, "pct:0.001")
        local.handle_text(key, "3")
        local.handle_callback(key, "month:1")
        screen = local.handle_text(key, "2024")
        assert screen is not None
        self.assertEqual("end_date", screen.state)
        labels = [button["text"] for row in screen.buttons for button in row]
        self.assertIn("📅 Today", labels)
        self.assertIn("📆 Choose End Date", labels)

        today = local.handle_callback(key, "end:today")
        self.assertEqual("confirm_run", today.state)
        self.assertIn("End: Today", today.text)

        local.handle_callback(key, "end:custom")
        custom = local.handle_text(key, "2024-01-03")
        assert custom is not None
        self.assertEqual("confirm_run", custom.state)
        self.assertIn("End: 2024-01-03", custom.text)

    def test_custom_end_date_rejects_future_and_before_start(self) -> None:
        local = wizard.BacktestWizard()
        key = ("date-validation",)
        local.open(key)
        local.handle_callback(key, "source:dukascopy")
        local.handle_callback(key, "ladder:sell")
        local.handle_callback(key, "dsymbol:xauusd")
        local.handle_callback(key, "dside:bid")
        local.handle_callback(key, "pct:0.001")
        local.handle_text(key, "3")
        local.handle_callback(key, "month:1")
        local.handle_text(key, "2024")
        local.handle_callback(key, "end:custom")
        with mock.patch.object(wizard, "_today_utc", return_value=date(2024, 1, 10)):
            future = local.handle_text(key, "2024-01-11")
        assert future is not None
        self.assertEqual("await_end_date", future.state)
        self.assertIn("future", future.text.lower())

        before = local.handle_text(key, "2024-01-02")
        assert before is not None
        self.assertEqual("await_end_date", before.state)
        self.assertIn("on or after", before.text)

    def test_custom_end_date_uses_exclusive_following_midnight_for_dukascopy(self) -> None:
        local = wizard.BacktestWizard()
        state = wizard.State(
            data_source="dukascopy",
            ladder="sell",
            provider_instrument="xauusd",
            symbol="XAUUSD",
            price_side="bid",
            percentage=0.001,
            day=3,
            month=1,
            year=2024,
            end_mode="custom",
            end_date=date(2024, 1, 3),
        )
        fake_cache = mock.Mock(path=Path("/tmp/scratch-xau.sqlite"))
        fake_provider = mock.Mock()
        fake_provider.fetch_range.return_value = [
            [1704240000000 + i * 60_000, "100", "101", "99", "100", "1", 0, "100"]
            for i in range(180)
        ]
        with mock.patch.object(wizard, "_initialize_backtest_dukascopy_cache", return_value=fake_cache), mock.patch.object(
            wizard, "DukascopyProvider", return_value=fake_provider
        ), mock.patch.object(wizard, "_draw_jpg", return_value="/tmp/backtest.jpg"):
            screen = local._run_backtest(state)
        self.assertEqual("done", screen.state)
        request = fake_provider.fetch_range.call_args.args[0]
        self.assertEqual(datetime(2024, 1, 3, tzinfo=timezone.utc).date(), datetime.fromtimestamp(request.start_ms / 1000, timezone.utc).date())
        self.assertEqual(datetime(2024, 1, 4, tzinfo=timezone.utc), datetime.fromtimestamp(request.end_ms / 1000, timezone.utc))

    def test_today_end_is_resolved_when_execution_begins_and_binance_receives_exact_end(self) -> None:
        local = wizard.BacktestWizard()
        state = wizard.State(
            data_source="binance",
            ladder="sell",
            market="spot",
            symbol="BTCUSDT",
            percentage=0.001,
            day=3,
            month=1,
            year=2024,
            end_mode="today",
        )
        candles = [[1704240000000 + i * 60_000, "100", "101", "99", "100", "1", 0, "100"] for i in range(180)]
        fake_cache = mock.Mock(path=Path("/tmp/scratch-binance.sqlite"))
        fake_cache.stats_for.return_value = {"path": str(fake_cache.path), "bars": len(candles)}
        with mock.patch.object(wizard, "_initialize_backtest_kline_cache", return_value=fake_cache), mock.patch.object(
            wizard, "_fetch_klines", return_value=candles
        ) as fetch, mock.patch.object(wizard, "_draw_jpg", return_value="/tmp/backtest.jpg"), mock.patch.object(
            wizard, "_now_utc", return_value=datetime(2024, 1, 5, 12, 0, tzinfo=timezone.utc)
        ):
            screen = local._run_backtest(state)
        self.assertEqual("done", screen.state)
        self.assertEqual(int(datetime(2024, 1, 3, tzinfo=timezone.utc).timestamp() * 1000), fetch.call_args.args[2])
        self.assertEqual(int(datetime(2024, 1, 5, 12, 0, tzinfo=timezone.utc).timestamp() * 1000), fetch.call_args.args[3])

    def test_dukascopy_today_uses_previous_fully_available_utc_day(self) -> None:
        local = wizard.BacktestWizard()
        state = wizard.State(
            data_source="dukascopy",
            ladder="sell",
            provider_instrument="xauusd",
            symbol="XAUUSD",
            price_side="bid",
            percentage=0.001,
            day=1,
            month=1,
            year=2024,
            end_mode="today",
        )
        fake_cache = mock.Mock(path=Path("/tmp/scratch-xau-today.sqlite"))
        fake_provider = mock.Mock()
        fake_provider.fetch_range.return_value = [
            [1704240000000 + i * 60_000, "100", "101", "99", "100", "1", 0, "100"]
            for i in range(2)
        ]
        with mock.patch.object(wizard, "_initialize_backtest_dukascopy_cache", return_value=fake_cache), mock.patch.object(
            wizard, "DukascopyProvider", return_value=fake_provider
        ), mock.patch.object(wizard, "_draw_jpg", return_value="/tmp/backtest.jpg"), mock.patch.object(
            wizard, "_now_utc", return_value=datetime(2024, 1, 5, 12, 0, tzinfo=timezone.utc)
        ):
            screen = local._run_backtest(state)
        self.assertEqual("done", screen.state)
        request = fake_provider.fetch_range.call_args.args[0]
        self.assertEqual(datetime(2024, 1, 5, tzinfo=timezone.utc), datetime.fromtimestamp(request.end_ms / 1000, timezone.utc))
        self.assertIn("Requested end: Today", screen.text)
        self.assertIn("Effective replay end: 2024-01-04", screen.text)

    def test_dukascopy_no_data_reasons_are_user_distinguishable_and_logged(self) -> None:
        local = wizard.BacktestWizard()
        state = wizard.State(
            data_source="dukascopy",
            ladder="sell",
            provider_instrument="xauusd",
            symbol="XAUUSD",
            price_side="bid",
            percentage=0.01,
            day=3,
            month=1,
            year=2024,
            end_mode="custom",
            end_date=date(2024, 1, 3),
        )
        fake_cache = mock.Mock(path=Path("/tmp/scratch-xau-reasons.sqlite"))
        for reason, expected in (
            ("verified_closure_only", "only verified market-closure days"),
            ("unavailable_coverage", "no verified Dukascopy candles"),
        ):
            fake_provider = mock.Mock()
            fake_provider.fetch_range.side_effect = DukascopyNoDataError("internal", reason=reason)
            with mock.patch.object(wizard, "_initialize_backtest_dukascopy_cache", return_value=fake_cache), mock.patch.object(
                wizard, "DukascopyProvider", return_value=fake_provider
            ), mock.patch.object(wizard.logger, "warning") as warning:
                screen = local._run_backtest(state)
            self.assertEqual("error", screen.state)
            self.assertIn(expected.lower(), screen.text.lower())
            self.assertIn("Requested end: 2024-01-03", screen.text)
            self.assertTrue(any("backtest initiation" in str(call.args) for call in warning.call_args_list))

    def test_scratch_cache_path_is_configurable_without_production_path(self) -> None:
        self.assertIn("GOLDENFIBO_BACKTEST_DUKASCOPY_CACHE_PATH", wizard._CACHE_ENV_NAMES.values())
        self.assertNotEqual(Path("/root/kam/GoldenFibo/data/dukascopy_klines.sqlite"), wizard._configured_cache_path("GOLDENFIBO_BACKTEST_DUKASCOPY_CACHE_PATH", Path("/tmp/scratch.sqlite"), {"GOLDENFIBO_BACKTEST_DUKASCOPY_CACHE_PATH": "/tmp/scratch.sqlite"}))


if __name__ == "__main__":
    unittest.main()
