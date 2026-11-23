from __future__ import annotations

import lzma
import struct
import tempfile
import unittest
from collections import namedtuple
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from goldenfibo.backtest import run_finite_backtest
from goldenfibo.marketdata.dukascopy_cache import DukascopyKlineCache
from goldenfibo.marketdata.dukascopy_provider import (
    DukascopyNoDataError,
    DukascopyProvider,
    DukascopyProviderError,
    default_is_closed,
)
from goldenfibo.marketdata.historical import HistoricalRequest


def payload(day: date, offsets=(0, 60), value=100000) -> bytes:
    raw = b"".join(struct.pack(">5If", offset, value, value + 50, value - 100, value + 100, 2.0) for offset in offsets)
    return lzma.compress(raw)


def request(day=date(2025, 1, 2), side="bid"):
    start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
    return HistoricalRequest("EURUSD", start, start + 120_000, side)


class RateLimitedError(OSError):
    code = 429

    def __init__(self, retry_after="5"):
        super().__init__("HTTP 429 Too Many Requests")
        self.headers = {"Retry-After": retry_after}


class Phase3ProviderRegressions(unittest.TestCase):
    def test_closure_only_range_is_distinguished_from_unavailable_data(self):
        day = date(2025, 1, 4)
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            cache.mark_day("xauusd", day, "verified_provider_empty", bid_complete=True, ask_complete=True)
            p = DukascopyProvider(cache, fetch_artifact=lambda *_: self.fail("closure must not fetch"), disk_threshold_bytes=0)
            start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
            with self.assertRaises(DukascopyNoDataError) as ctx:
                p.fetch_range(HistoricalRequest("XAUUSD", start, start + 120_000, "bid"))
            self.assertEqual("verified_closure_only", ctx.exception.reason)

    def test_cached_xauusd_bid_range_returns_candles_without_fetching(self):
        day = date(2025, 1, 2)
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            from goldenfibo.marketdata.dukascopy_format import parse_bi5_m1
            candles = parse_bi5_m1(payload(day), day, price_scale=1000, price_side="bid")
            cache.upsert_side("xauusd", candles)
            cache.mark_day("xauusd", day, "verified_data", bid_complete=True, ask_complete=False)
            p = DukascopyProvider(cache, fetch_artifact=lambda *_: self.fail("cache hit must not fetch"), disk_threshold_bytes=0, refresh_tail_days=0)
            start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
            rows = p.fetch_range(HistoricalRequest("XAUUSD", start, start + 120_000, "bid"))
            self.assertEqual(2, len(rows))

    def test_invalid_provider_interval_is_distinguished(self):
        with tempfile.TemporaryDirectory() as td:
            p = DukascopyProvider(DukascopyKlineCache(Path(td) / "x.sqlite"), fetch_artifact=lambda *_: self.fail("invalid range must not fetch"), disk_threshold_bytes=0)
            with self.assertRaises(DukascopyProviderError) as ctx:
                p.fetch_range(HistoricalRequest("XAUUSD", 1000, 1000, "bid"))
            self.assertIn("invalid or empty", str(ctx.exception).lower())

    def test_http_503_retries_four_times_then_fails_as_http_5xx(self):
        attempts = {"n": 0}

        def fetch(_instrument, _day, _side):
            attempts["n"] += 1
            raise HTTPError("https://example.invalid", 503, "unavailable", {}, None)

        with tempfile.TemporaryDirectory() as td, \
                patch("goldenfibo.marketdata.dukascopy_provider.time.sleep") as sleep, \
                patch("goldenfibo.marketdata.dukascopy_provider.random.uniform", return_value=0.0), \
                patch("goldenfibo.marketdata.dukascopy_provider.logger.warning") as warning:
            p = DukascopyProvider(DukascopyKlineCache(Path(td) / "x.sqlite"), fetch_artifact=fetch, disk_threshold_bytes=0)
            with self.assertRaises(DukascopyProviderError):
                p.fetch_range(request())
            self.assertEqual(4, attempts["n"])
            self.assertEqual([5.0, 10.0, 20.0], [call.args[0] for call in sleep.call_args_list])
            retry_warnings = [call for call in warning.call_args_list if "artifact attempt failed" in str(call.args)]
            self.assertEqual(4, len(retry_warnings))
            self.assertTrue(all("http_5xx" in str(call.args) for call in retry_warnings))

    def test_current_day_http_404_is_failed_not_weekend_or_empty(self):
        day = date(2026, 11, 2)
        attempts = {"n": 0}

        def fetch(_instrument, _day, _side):
            attempts["n"] += 1
            raise HTTPError("https://example.invalid", 404, "not found", {}, None)

        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            p = DukascopyProvider(cache, fetch_artifact=fetch, disk_threshold_bytes=0, is_closed=lambda *_: False)
            start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
            with self.assertRaises(DukascopyProviderError):
                p.fetch_range(HistoricalRequest("XAUUSD", start, start + 120_000, "bid"))
            coverage = cache.coverage("xauusd", day, day)[0]
            self.assertEqual(1, attempts["n"])
            self.assertEqual("failed", coverage.status)
            self.assertFalse(coverage.bid_complete)
            self.assertNotEqual("verified_provider_empty", coverage.status)

    def test_historical_http_404_remains_fail_closed(self):
        day = date(2026, 1, 8)

        def fetch(_instrument, _day, _side):
            raise HTTPError("https://example.invalid", 404, "not found", {}, None)

        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            p = DukascopyProvider(cache, fetch_artifact=fetch, disk_threshold_bytes=0, is_closed=lambda *_: False)
            start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
            with self.assertRaises(DukascopyProviderError):
                p.fetch_range(HistoricalRequest("XAUUSD", start, start + 120_000, "bid"))
            coverage = cache.coverage("xauusd", day, day)[0]
            self.assertEqual("failed", coverage.status)
            self.assertFalse(coverage.bid_complete)

    def test_timeout_retries_use_bounded_five_ten_twenty_second_delays_and_warn(self):
        attempts = {"n": 0}

        def fetch(_instrument, _day, _side):
            attempts["n"] += 1
            if attempts["n"] < 4:
                raise TimeoutError("read operation timed out")
            return payload(date(2025, 1, 2))

        with tempfile.TemporaryDirectory() as td, \
                patch("goldenfibo.marketdata.dukascopy_provider.time.sleep") as sleep, \
                patch("goldenfibo.marketdata.dukascopy_provider.random.uniform", return_value=0.0), \
                patch("goldenfibo.marketdata.dukascopy_provider.logger.warning", create=True) as warning:
            p = DukascopyProvider(DukascopyKlineCache(Path(td) / "x.sqlite"), fetch_artifact=fetch, disk_threshold_bytes=0)
            self.assertEqual(2, len(p.fetch_range(request())))
            self.assertEqual(4, attempts["n"])
            self.assertEqual([5.0, 10.0, 20.0], [call.args[0] for call in sleep.call_args_list])
            retry_warnings = [call for call in warning.call_args_list if "artifact attempt failed" in str(call.args)]
            self.assertEqual(3, len(retry_warnings))
            self.assertTrue(all("timeout" in str(call.args).lower() for call in retry_warnings))

    def test_connection_reset_is_transient_and_retries(self):
        attempts = {"n": 0}

        def fetch(_instrument, _day, _side):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ConnectionResetError("connection reset")
            return payload(date(2025, 1, 2))

        with tempfile.TemporaryDirectory() as td, patch("goldenfibo.marketdata.dukascopy_provider.time.sleep"):
            p = DukascopyProvider(DukascopyKlineCache(Path(td) / "x.sqlite"), fetch_artifact=fetch, disk_threshold_bytes=0)
            self.assertEqual(2, len(p.fetch_range(request())))
            self.assertEqual(2, attempts["n"])

    def test_permanent_http_4xx_fails_without_retry(self):
        attempts = {"n": 0}

        def fetch(_instrument, _day, _side):
            attempts["n"] += 1
            raise HTTPError("https://example.invalid", 404, "not found", {}, None)

        with tempfile.TemporaryDirectory() as td, patch("goldenfibo.marketdata.dukascopy_provider.time.sleep") as sleep:
            p = DukascopyProvider(DukascopyKlineCache(Path(td) / "x.sqlite"), fetch_artifact=fetch, disk_threshold_bytes=0)
            with self.assertRaises(DukascopyProviderError):
                p.fetch_range(request())
            self.assertEqual(1, attempts["n"])
            sleep.assert_not_called()

    def test_verified_defaults_and_fail_closed_scales(self):
        with tempfile.TemporaryDirectory() as td:
            p = DukascopyProvider(DukascopyKlineCache(Path(td) / "x.sqlite"), fetch_artifact=lambda *_: payload(date(2025, 1, 2)), disk_threshold_bytes=0)
            self.assertEqual(100000.0, p.instrument_scales["eurusd"])
            self.assertEqual(1000.0, p.instrument_scales["xauusd"])
            self.assertEqual("xauusd", p.validate_instrument("XAUUSD"))
            for symbol in ("USDJPY", "BTCUSD", "XAGUSD"):
                with self.assertRaises(DukascopyProviderError):
                    p.validate_instrument(symbol)

    def test_explicit_verified_catalog_can_enable_uncertain_instrument(self):
        with tempfile.TemporaryDirectory() as td:
            p = DukascopyProvider(DukascopyKlineCache(Path(td) / "x.sqlite"), fetch_artifact=lambda *_: payload(date(2025, 1, 2)), instrument_scales={"btcusd": 10}, disk_threshold_bytes=0)
            self.assertEqual("btcusd", p.validate_instrument("BTC/USD"))

    def test_btc_weekend_is_not_automatically_closed(self):
        self.assertFalse(default_is_closed("btcusd", date(2025, 1, 4)))
        self.assertTrue(default_is_closed("eurusd", date(2025, 1, 4)))

    def test_empty_artifact_is_failed_not_complete(self):
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            p = DukascopyProvider(cache, fetch_artifact=lambda *_: lzma.compress(b""), disk_threshold_bytes=0, max_retries=0)
            with self.assertRaises(DukascopyProviderError):
                p.fetch_range(request())
            cov = cache.coverage("eurusd", date(2025, 1, 2), date(2025, 1, 2))[0]
            self.assertEqual("failed", cov.status)
            self.assertFalse(cov.bid_complete or cov.ask_complete)

    def test_bid_request_does_not_require_ask_coverage(self):
        calls = []
        def fetch(_instrument, day, side):
            calls.append(side)
            return payload(day)
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            p = DukascopyProvider(cache, fetch_artifact=fetch, disk_threshold_bytes=0, max_retries=0)
            rows = p.fetch_range(request())
            self.assertEqual(2, len(rows))
            cov = cache.coverage("eurusd", date(2025, 1, 2), date(2025, 1, 2))[0]
            self.assertEqual("verified_data", cov.status)
            self.assertTrue(cov.bid_complete)
            self.assertFalse(cov.ask_complete)
            self.assertEqual(["bid"], calls)

    def test_retry_is_bounded_and_resumes(self):
        attempts = {"n": 0}
        def fetch(_instrument, day, side):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise TimeoutError("transient")
            return payload(day)
        with tempfile.TemporaryDirectory() as td:
            p = DukascopyProvider(DukascopyKlineCache(Path(td) / "x.sqlite"), fetch_artifact=fetch, disk_threshold_bytes=0, max_retries=2, retry_backoff_seconds=0)
            rows = p.fetch_range(request())
            self.assertEqual(2, len(rows))
            self.assertEqual(3, attempts["n"])

    def test_rate_limit_honors_retry_after_with_jitter(self):
        attempts = {"n": 0}

        def fetch(_instrument, day, side):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RateLimitedError("5")
            return payload(day)

        with tempfile.TemporaryDirectory() as td, patch("goldenfibo.marketdata.dukascopy_provider.time.sleep") as sleep, patch("goldenfibo.marketdata.dukascopy_provider.random.uniform", return_value=0.25):
            DukascopyProvider._cooldown_until = 0.0
            p = DukascopyProvider(
                DukascopyKlineCache(Path(td) / "x.sqlite"),
                fetch_artifact=fetch,
                disk_threshold_bytes=0,
                max_retries=1,
                retry_backoff_seconds=1,
                retry_jitter_seconds=0.5,
            )
            self.assertEqual(2, len(p.fetch_range(request())))
            self.assertAlmostEqual(5.25, sleep.call_args_list[0].args[0], delta=0.1)
            self.assertEqual(2, attempts["n"])
            DukascopyProvider._cooldown_until = 0.0

    def test_rate_limit_stops_after_retry_budget(self):
        attempts = {"n": 0}

        def fetch(_instrument, _day, _side):
            attempts["n"] += 1
            raise RateLimitedError("1")

        with tempfile.TemporaryDirectory() as td, patch("goldenfibo.marketdata.dukascopy_provider.time.sleep"), patch("goldenfibo.marketdata.dukascopy_provider.random.uniform", return_value=0.0):
            DukascopyProvider._cooldown_until = 0.0
            p = DukascopyProvider(
                DukascopyKlineCache(Path(td) / "x.sqlite"),
                fetch_artifact=fetch,
                disk_threshold_bytes=0,
                max_retries=1,
                retry_backoff_seconds=1,
                retry_jitter_seconds=0,
            )
            with self.assertRaises(DukascopyProviderError):
                p.fetch_range(request())
            self.assertEqual(2, attempts["n"])
            DukascopyProvider._cooldown_until = 0.0

    def test_volume_does_not_become_quote_volume(self):
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            p = DukascopyProvider(DukascopyKlineCache(Path(td) / "y.sqlite"), fetch_artifact=lambda _i, d, _s: payload(d), disk_threshold_bytes=0)
            # Direct cache assertion isolates the replay-row compatibility shape.
            from goldenfibo.marketdata.dukascopy_format import parse_bi5_m1
            cache.upsert_side("eurusd", parse_bi5_m1(payload(date(2025, 1, 2)), date(2025, 1, 2), price_scale=100000, price_side="bid"))
            self.assertEqual(0.0, cache.rows("eurusd", 0, 2**63 - 1)[0][7])

    def test_disk_reserve_rejects_before_fetch(self):
        Usage = namedtuple("Usage", "total used free")
        called = []
        with tempfile.TemporaryDirectory() as td:
            p = DukascopyProvider(DukascopyKlineCache(Path(td) / "x.sqlite"), fetch_artifact=lambda *_: called.append(1) or payload(date(2025, 1, 2)), disk_threshold_bytes=1000, journal_reserve_bytes=1000)
            with patch("goldenfibo.marketdata.historical.shutil.disk_usage", return_value=Usage(10000, 9500, 500)):
                with self.assertRaises(RuntimeError):
                    p.fetch_range(request())
            self.assertEqual([], called)

    def test_backtest_candle_event_limits_are_controlled(self):
        row = [0, 100, 101, 99, 100, 1, 59999, 0, 0, 0.0, 0.0, "0"]
        with self.assertRaises(RuntimeError):
            run_finite_backtest([row], side="SELL", percentage="0.001", symbol="EURUSD", max_events=0)
        with self.assertRaises(RuntimeError):
            run_finite_backtest([row, row], side="SELL", percentage="0.001", symbol="EURUSD", max_candles=1)

    def test_invalid_backtest_input_releases_concurrency_slot(self):
        row = [0, 100, 101, 99, 100, 1, 59999, 0, 0, 0.0, 0.0, "0"]
        with self.assertRaises(ValueError):
            run_finite_backtest([row], side="INVALID", percentage="0.001", symbol="EURUSD")

        result = run_finite_backtest([row], side="SELL", percentage="0.001", symbol="EURUSD")

        self.assertEqual(1, result.bars_processed)


if __name__ == "__main__":
    unittest.main()
