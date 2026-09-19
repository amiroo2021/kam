"""Regression tests for /backtest progress presentation."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from goldenfibo.marketdata.kline_cache import CachePolicy, KlineCache, fetch_range_cached
from plugins.trade import backtest_wizard as wizard


class _DummyQuery:
    def __init__(self) -> None:
        self.answered = False
        self.edits: list[str] = []
        self.message = SimpleNamespace(chat=SimpleNamespace(id=123), message_thread_id=None)

    async def answer(self) -> None:
        self.answered = True

    async def edit_message_text(self, text: str, reply_markup=None) -> None:
        self.edits.append(text)


class BacktestProgressPresentationTests(unittest.TestCase):
    def test_initial_unknown_total_omits_x_over_zero(self) -> None:
        phase, pct, detail = wizard.BacktestWizard._progress_from_cache({"path": "/tmp/cache.sqlite"})
        text = wizard.BacktestWizard._progress_text(phase, pct, detail)
        self.assertIn("Historical Data Ready", text)
        self.assertIn("Cache: /tmp/cache.sqlite", text)
        self.assertNotIn("/0", text)
        self.assertNotIn("0/0", text)

    def test_fully_cached_range_formats_ready_screen(self) -> None:
        phase, pct, detail = wizard.BacktestWizard._progress_from_cache(
            {"bars_total": 281703, "bars_from_cache": 281703, "bars_downloaded": 0, "path": "/root/kam/GoldenFibo/data/backtest_klines.sqlite"}
        )
        text = wizard.BacktestWizard._progress_text(phase, pct, detail)
        self.assertIn("Historical Data Ready", text)
        self.assertIn("281,703 candles loaded from cache", text)
        self.assertIn("Downloaded: 0", text)
        self.assertIn("Cache: /root/kam/GoldenFibo/data/backtest_klines.sqlite", text)
        self.assertNotIn("Downloading Gaps", text)
        self.assertNotIn("/0", text)

    def test_partially_cached_range_uses_missing_denominal(self) -> None:
        phase, pct, detail = wizard.BacktestWizard._progress_from_cache(
            {"bars_total": 281703, "bars_from_cache": 100000, "bars_downloaded": 0, "path": "/root/kam/GoldenFibo/data/backtest_klines.sqlite"}
        )
        text = wizard.BacktestWizard._progress_text("downloading_gaps", 37.5, "Cached: 100,000\nMissing: 181,703")
        self.assertIn("Downloading Gaps", text)
        self.assertIn("Cached: 100,000", text)
        self.assertIn("Missing: 181,703", text)
        self.assertNotIn("/0", text)

    def test_completely_uncached_range_no_x_over_zero(self) -> None:
        text = wizard.BacktestWizard._progress_text("downloading_gaps", 0.0, "Cached: 0\nMissing: 281,703")
        self.assertIn("Downloading Gaps", text)
        self.assertIn("Cached: 0", text)
        self.assertIn("Missing: 281,703", text)
        self.assertNotIn("/0", text)

    def test_multiple_gaps_keep_counter_separate(self) -> None:
        text = wizard.BacktestWizard._progress_text("downloading_gaps", 50.0, "90,852 / 181,703 missing candles\nCached: 100,000")
        self.assertIn("90,852 / 181,703 missing candles", text)
        self.assertIn("Cached: 100,000", text)
        self.assertNotIn("281,703/0", text)
        self.assertNotIn("0/0", text)

    def test_partial_cache_end_to_end_callback_uses_download_total(self) -> None:
        # Use the deployed handler path with a small controlled partial cache.
        start_ms = 1_756_684_860_000
        step = 60_000
        end_ms = start_ms + 10_000 * step
        partial_cached = 8_000
        missing = 2_000
        universe = [
            [start_ms + i * step, "44", "45", "43", "44.5", "10", start_ms + i * step + 59_999, "445", 1, "5", "222", "0"]
            for i in range(10_000)
        ]
        with tempfile.TemporaryDirectory() as td:
            cache = KlineCache(Path(td) / "t.sqlite")
            cache.upsert_klines("HYPEUSDT", "1m", universe[:partial_cached], market="futures")
            callbacks: list[dict] = []

            def fetch(url: str):
                from urllib.parse import parse_qs, urlparse
                qs = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
                st, et, lim = int(qs["startTime"]), int(qs["endTime"]), int(qs["limit"])
                rows = [r for r in universe if st <= r[0] <= et][:lim]
                return rows

            def progress(p):
                callbacks.append(dict(p))

            res = fetch_range_cached(
                "HYPEUSDT",
                "1m",
                start_ms,
                end_ms,
                cache=cache,
                policy=CachePolicy.AUTO,
                fetch=fetch,
                market="futures",
                sleep_s=0,
                on_progress=progress,
                refresh_tail_ms=0,
            )
            self.assertEqual(len(res.klines), 10_000)
            self.assertEqual(res.stats.bars_from_cache, partial_cached)
            self.assertEqual(res.stats.bars_downloaded, missing)
            download_payloads = [p for p in callbacks if p.get("stage") == "downloading_gaps"]
            self.assertTrue(download_payloads)
            first = download_payloads[0]
            self.assertEqual(int(first.get("stats", {}).get("bars_from_cache") or 0), partial_cached)
            self.assertEqual(int(first.get("download_total") or 0), missing)
            self.assertEqual(int(first.get("download_done") or 0), 1000)


if __name__ == "__main__":
    unittest.main()
