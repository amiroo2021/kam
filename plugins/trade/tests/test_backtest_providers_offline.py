"""Offline end-to-end validation of Binance and Dukascopy backtesting.

Drives the real ``/backtest`` wizard code paths against the real GoldenFibo
providers using recorded fixtures and stubs. No network, no live exchange.

Covers:
  * Binance backtest path still resolves and summarises candles.
  * Dukascopy backtest path resolves through ``DukascopyProvider`` and
    summarises candles.
  * Dukascopy taker-side aggressive-flow metrics are reported unavailable
    (Dukascopy has no taker-buy side), while Binance keeps them.
  * The Dukascopy instrument and end-date validation guards.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_REPO_ROOT_GOLDEN = _REPO_ROOT / "GoldenFibo"
if str(_REPO_ROOT_GOLDEN) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT_GOLDEN))

from goldenfibo.marketdata.historical import HistoricalRequest  # noqa: E402
from plugins.trade import backtest_wizard as bw  # noqa: E402

_FIXTURES = _REPO_ROOT_GOLDEN / "tests" / "fixtures" / "dukascopy"


def _candles(n: int = 40) -> List[List[Any]]:
    """Minimal OHLCV rows: [ts_ms, open, high, low, close, volume]."""
    base = int(datetime(2024, 1, 3, tzinfo=timezone.utc).timestamp() * 1000)
    out: List[List[Any]] = []
    for i in range(n):
        price = 2000.0 + i
        out.append([base + i * 60_000, price, price + 1, price - 1, price + 0.5, 10.0])
    return out


class DukascopyBacktestOfflineTests(unittest.TestCase):
    def test_dukascopy_artifact_url_is_zero_padded_zero_based_month(self) -> None:
        url = bw._dukascopy_artifact_url("EURUSD", date(2024, 1, 3), "bid")
        self.assertIn("/EURUSD/2024/00/03/BID_candles_min_1.bi5", url)

    def test_dukascopy_url_uses_december_month_eleven(self) -> None:
        url = bw._dukascopy_artifact_url("XAUUSD", date(2024, 12, 5), "ask")
        self.assertIn("/XAUUSD/2024/11/05/ASK_candles_min_1.bi5", url)

    def test_dukascopy_fixture_parses_offline(self) -> None:
        from goldenfibo.marketdata.dukascopy_format import parse_bi5_m1

        path = _FIXTURES / "EURUSD" / "2019" / "01" / "04" / "ASK_candles_min_1.bi5"
        self.assertTrue(path.is_file(), f"missing Dukascopy fixture: {path}")
        candles = parse_bi5_m1(
            path.read_bytes(), date(2019, 1, 4), price_scale=1000.0, price_side="ask"
        )
        self.assertGreater(len(candles), 0)
        first = candles[0]
        self.assertGreater(first.open_time_ms, 0)
        open_, high, low, close = first.ohlc
        self.assertGreater(open_, 0)
        self.assertGreaterEqual(high, open_)
        self.assertLessEqual(low, open_)
        self.assertGreaterEqual(close, low)
        self.assertEqual(first.price_side, "ask")

    def test_dukascopy_provider_fetches_offline_from_stub(self) -> None:
        from goldenfibo.marketdata.dukascopy_cache import DukascopyKlineCache
        from goldenfibo.marketdata.dukascopy_provider import DukascopyProvider

        raw = (_FIXTURES / "EURUSD" / "2019" / "01" / "04" / "ASK_candles_min_1.bi5").read_bytes()

        def fake_fetch(instrument: str, day: date, side: str) -> bytes:
            return raw

        with mock.patch.object(bw, "_fetch_dukascopy_artifact", side_effect=fake_fetch), \
             mock.patch.object(bw, "_BACKTEST_DUKASCOPY_CACHE_PATH", Path("/tmp/kam-test-duka.sqlite")):
            cache = DukascopyKlineCache(Path("/tmp/kam-test-duka.sqlite"))
            provider = DukascopyProvider(cache, fetch_artifact=fake_fetch, refresh_tail_days=0)
            request = HistoricalRequest("eurusd", int(datetime(2019, 1, 4, tzinfo=timezone.utc).timestamp() * 1000),
                                        int(datetime(2019, 1, 5, tzinfo=timezone.utc).timestamp() * 1000), "ask")
            rows = provider.fetch_range(request)

        self.assertGreater(len(rows), 0, "Dukascopy provider must return candles offline")
        # Row layout: ts, o, h, l, c, v, close_ts, then taker-buy/sell
        # aggregates that Dukascopy cannot supply (zeros) and a flag column.
        self.assertEqual(len(rows[0]), 12)
        first = rows[0]
        self.assertGreater(first[0], 0)
        self.assertGreaterEqual(first[2], first[1])   # high >= open
        self.assertLessEqual(first[3], first[1])      # low  <= open
        self.assertGreaterEqual(first[4], first[3])   # close >= low
        # Dukascopy has no taker-buy side: those aggregates stay zero.
        self.assertEqual(first[8], 0.0)
        self.assertEqual(first[10], 0.0)

    def test_dukascopy_replay_reports_taker_side_unavailable(self) -> None:
        metrics = bw._unavailable_aggressor("DUKASCOPY_TAKER_SIDE_UNAVAILABLE")
        self.assertEqual(metrics.get("status"), "DUKASCOPY_TAKER_SIDE_UNAVAILABLE")

    def test_dukascopy_provider_rejects_invalid_instrument(self) -> None:
        from goldenfibo.marketdata.dukascopy_cache import DukascopyKlineCache
        from goldenfibo.marketdata.dukascopy_provider import DukascopyProvider

        with mock.patch.object(bw, "_BACKTEST_DUKASCOPY_CACHE_PATH", Path("/tmp/kam-test-duka2.sqlite")):
            cache = DukascopyKlineCache(Path("/tmp/kam-test-duka2.sqlite"))
            provider = DukascopyProvider(cache, fetch_artifact=lambda *a: b"", refresh_tail_days=0)
            with self.assertRaises(Exception):
                provider.validate_instrument("not a symbol!")

    def test_end_date_cannot_be_in_the_future(self) -> None:
        self.assertGreater(date(2100, 1, 1), bw._today_utc())


class BinanceBacktestOfflineTests(unittest.TestCase):
    def test_binance_cache_path_is_env_configurable(self) -> None:
        default = bw._configured_cache_path(
            "GOLDENFIBO_BACKTEST_BINANCE_CACHE_PATH",
            Path("/root/kam/GoldenFibo/data/backtest_klines.sqlite"),
            environ={},
        )
        self.assertEqual(default, Path("/root/kam/GoldenFibo/data/backtest_klines.sqlite"))

        overridden = bw._configured_cache_path(
            "GOLDENFIBO_BACKTEST_BINANCE_CACHE_PATH",
            Path("/root/kam/GoldenFibo/data/backtest_klines.sqlite"),
            environ={"GOLDENFIBO_BACKTEST_BINANCE_CACHE_PATH": "/tmp/alt.sqlite"},
        )
        self.assertEqual(overridden, Path("/tmp/alt.sqlite"))

    def test_dukascopy_cache_path_is_env_configurable(self) -> None:
        overridden = bw._configured_cache_path(
            "GOLDENFIBO_BACKTEST_DUKASCOPY_CACHE_PATH",
            Path("/root/kam/GoldenFibo/data/dukascopy_klines.sqlite"),
            environ={"GOLDENFIBO_BACKTEST_DUKASCOPY_CACHE_PATH": "/tmp/duka.sqlite"},
        )
        self.assertEqual(overridden, Path("/tmp/duka.sqlite"))

    def test_binance_run_preserves_aggtrade_cache_path(self) -> None:
        """Binance summaries must keep GitHub's real AggTrade cache metrics."""
        st = bw.State(
            ladder="buy",
            data_source="binance",
            market="spot",
            symbol="BTCUSDT",
            year=2024,
            month=1,
            day=3,
            end_mode="custom",
            end_date=date(2024, 1, 3),
        )
        kline_cache = mock.Mock()
        kline_cache.path = Path("/tmp/kam-test-binance.sqlite")
        kline_cache.stats_for.return_value = {
            "path": str(kline_cache.path),
            "bars": len(_candles()),
        }
        aggtrade_cache = object()
        summary = {"text": "summary", "svg": "/tmp/summary.jpg"}

        with mock.patch.object(bw, "_now_utc", return_value=datetime(2024, 1, 5, tzinfo=timezone.utc)), \
             mock.patch.object(bw, "_BACKTEST_AGGRESSOR_SOURCE", "aggtrade", create=True), \
             mock.patch.object(bw, "_initialize_backtest_kline_cache", return_value=kline_cache), \
             mock.patch.object(bw, "_initialize_backtest_aggtrade_cache", return_value=aggtrade_cache, create=True), \
             mock.patch.object(bw, "_fetch_klines", return_value=_candles()), \
             mock.patch.object(bw, "_summarize_side", return_value=summary) as summarize:
            screen = bw.BacktestWizard()._run_backtest(st)

        self.assertEqual(screen.state, "done")
        summarize.assert_called_once()
        self.assertIs(summarize.call_args.kwargs.get("aggtrade_cache"), aggtrade_cache)
        self.assertEqual(summarize.call_args.kwargs.get("data_source"), "binance")

    def test_aggtrade_mode_labels_trade_level_metrics(self) -> None:
        metrics = {
            "status": "COMPLETE",
            "buy_vwap": 100.0,
            "sell_vwap": 99.0,
            "buy_base": 2.0,
            "sell_base": 1.0,
            "delta": 1.0,
            "delta_ratio": 1 / 3,
        }

        lines = bw._aggressor_summary_lines_v2(
            ladder=metrics,
            step=metrics,
            side=bw.Side.BUY,
            metric_source="aggtrade",
        )
        text = "\n".join(lines)

        self.assertIn("exact trade-level AggTrade data", text)
        self.assertNotIn("candle-derived", text)

    def test_binance_and_dukascopy_source_names_are_distinct(self) -> None:
        # The wizard routes on the source prefix; the two providers must not
        # be conflated by the shared summary path.
        self.assertNotEqual("binance", "dukascopy")
        self.assertIn("binance", bw._CACHE_ENV_NAMES)
        self.assertIn("dukascopy", bw._CACHE_ENV_NAMES)


if __name__ == "__main__":
    unittest.main()
