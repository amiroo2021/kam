from __future__ import annotations

import lzma
import struct
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from goldenfibo.marketdata.dukascopy_cache import DukascopyKlineCache
from goldenfibo.marketdata.dukascopy_provider import DukascopyProvider, DukascopyProviderError
from goldenfibo.marketdata.historical import HistoricalRequest
from goldenfibo.marketdata.kline_cache import KlineCache, fetch_range_cached
from plugins.trade import backtest_wizard as wizard


def payload(day: date, side: str = "bid") -> bytes:
    raw = b"".join(struct.pack(">5If", offset, 100000, 100500, 99000, 101000, 2.0) for offset in (0, 60))
    return lzma.compress(raw)


class CacheBoundaryRegressionTests(unittest.TestCase):
    def test_binance_full_cache_hit_does_not_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = KlineCache(Path(td) / "binance.sqlite")
            rows = [[i * 60_000, "100", "101", "99", "100", "1", i * 60_000 + 59_999, "100", 1, "1", "1", "0"] for i in range(3)]
            cache.upsert_klines("BTCUSDT", "1m", rows, market="spot")
            cache.set_availability("BTCUSDT", "1m", market="spot", first_available_open_time=0, last_available_open_time=120_000)
            calls: list[str] = []
            result = fetch_range_cached(
                "BTCUSDT", "1m", 0, 180_000, cache=cache, fetch=lambda url: calls.append(url) or [],
                closed_only_before_ms=180_000, refresh_tail_ms=0, sleep_s=0,
            )
            self.assertEqual(3, len(result.klines))
            self.assertEqual([], calls)
            self.assertEqual(3, result.stats.bars_from_cache)
            self.assertEqual(0, result.stats.bars_downloaded)

    def test_binance_partial_gap_downloads_only_missing_interval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = KlineCache(Path(td) / "binance.sqlite")
            cached = [[i * 60_000, "100", "101", "99", "100", "1", i * 60_000 + 59_999, "100", 1, "1", "1", "0"] for i in (0, 2)]
            missing = [[60_000, "100", "101", "99", "100", "1", 119_999, "100", 1, "1", "1", "0"]]
            cache.upsert_klines("BTCUSDT", "1m", cached, market="spot")
            cache.set_availability("BTCUSDT", "1m", market="spot", first_available_open_time=0, last_available_open_time=120_000)
            calls: list[str] = []
            result = fetch_range_cached(
                "BTCUSDT", "1m", 0, 180_000, cache=cache,
                fetch=lambda url: calls.append(url) or missing,
                closed_only_before_ms=180_000, refresh_tail_ms=0, sleep_s=0,
            )
            self.assertEqual(3, len(result.klines))
            self.assertEqual(1, len(calls))
            self.assertEqual(1, result.stats.bars_downloaded)

    def test_dukascopy_interrupted_daily_download_resumes_without_redownloading_complete_day(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "dukascopy.sqlite")
            day1 = date(2025, 1, 2)
            day2 = date(2025, 1, 3)
            calls: list[tuple[date, str]] = []
            fail_day2 = {"enabled": True}

            def fetch(_instrument: str, day: date, side: str) -> bytes:
                calls.append((day, side))
                if day == day2 and fail_day2["enabled"]:
                    raise OSError("interrupted")
                return payload(day, side)

            provider = DukascopyProvider(cache, fetch_artifact=fetch, disk_threshold_bytes=0, max_retries=0)
            start = int(datetime(2025, 1, 2, tzinfo=timezone.utc).timestamp() * 1000)
            end = int(datetime(2025, 1, 4, tzinfo=timezone.utc).timestamp() * 1000)
            with self.assertRaises(DukascopyProviderError):
                provider.fetch_range(HistoricalRequest("EURUSD", start, end, "bid"))
            self.assertEqual([(day1, "bid"), (day2, "bid")], calls)
            self.assertEqual("verified_data", cache.coverage("eurusd", day1, day1)[0].status)

            fail_day2["enabled"] = False
            rows = provider.fetch_range(HistoricalRequest("EURUSD", int(datetime(2025, 1, 3, tzinfo=timezone.utc).timestamp() * 1000), end, "bid"))
            self.assertEqual(2, len(rows))
            self.assertEqual([(day1, "bid"), (day2, "bid"), (day2, "bid")], calls)

    def test_scratch_cache_environment_names_are_distinct_from_production_defaults(self) -> None:
        self.assertEqual(Path("/tmp/scratch.sqlite"), wizard._configured_cache_path(
            wizard._CACHE_ENV_NAMES["dukascopy"],
            Path("/root/kam/GoldenFibo/data/dukascopy_klines.sqlite"),
            {wizard._CACHE_ENV_NAMES["dukascopy"]: "/tmp/scratch.sqlite"},
        ))


if __name__ == "__main__":
    unittest.main()
