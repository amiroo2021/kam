"""Dukascopy disk-budget accounting regressions.

Contract under test
-------------------
``DukascopyProvider._check_disk`` must hand ``check_disk_budget`` the
estimated INCREMENTAL growth only.

``check_disk_budget`` already reasons about *currently available* free space
(``shutil.disk_usage(...).free``). The bytes an existing cache file already
occupies are therefore already deducted from that free space. Adding the
existing cache size to the estimate a second time double-counts it and
rejects downloads that actually fit.

These tests fail against the candidate that summed
``existing_cache_size + incremental_growth``.
"""

from __future__ import annotations

import lzma
import os
import struct
import tempfile
import unittest
from collections import namedtuple
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from goldenfibo.marketdata.dukascopy_cache import DukascopyKlineCache
from goldenfibo.marketdata.dukascopy_provider import DukascopyProvider
from goldenfibo.marketdata.historical import HistoricalRequest

# The provider's per-day incremental growth estimate: 1,440 M1 rows per day
# with both side columns plus SQLite page and rollback/WAL headroom.
PER_DAY_ESTIMATE_BYTES = 1440 * 300

Usage = namedtuple("Usage", "total used free")


def payload(day: date, side: str = "bid") -> bytes:
    raw = b"".join(
        struct.pack(">5If", offset, 100000, 100500, 99000, 101000, 2.0)
        for offset in (0, 60)
    )
    return lzma.compress(raw)


def request(day: date = date(2025, 1, 2), side: str = "bid") -> HistoricalRequest:
    start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
    return HistoricalRequest("EURUSD", start, start + 120_000, side)


def capture_estimates(provider: DukascopyProvider, missing_days: int) -> list[int]:
    """Record the ``estimated_bytes`` argument handed to ``check_disk_budget``."""
    captured: list[int] = []

    def _spy(path, estimated_bytes, **kwargs):
        captured.append(int(estimated_bytes))
        return None

    with patch(
        "goldenfibo.marketdata.dukascopy_provider.check_disk_budget",
        side_effect=_spy,
    ):
        provider._check_disk(missing_days)
    return captured


class DukascopyDiskBudgetAccountingTests(unittest.TestCase):
    def test_incremental_estimate_excludes_existing_cache_size(self) -> None:
        """The estimate is the incremental growth, not existing + growth."""
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            provider = DukascopyProvider(
                cache,
                fetch_artifact=lambda *_: payload(date(2025, 1, 2)),
                disk_threshold_bytes=0,
            )

            estimates = capture_estimates(provider, 1)

            self.assertEqual([PER_DAY_ESTIMATE_BYTES], estimates)

    def test_estimate_does_not_grow_with_existing_cache_size(self) -> None:
        """A larger pre-existing cache must not change the estimate."""
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            provider = DukascopyProvider(
                cache,
                fetch_artifact=lambda *_: payload(date(2025, 1, 2)),
                disk_threshold_bytes=0,
            )

            before = capture_estimates(provider, 2)

            # Grow the on-disk cache well past the per-day estimate. The file
            # is only stat()ed by _check_disk, never re-opened here.
            with open(cache.path, "r+b") as handle:
                handle.truncate(64 << 20)

            after = capture_estimates(provider, 2)

            self.assertEqual(before, after)
            self.assertEqual([2 * PER_DAY_ESTIMATE_BYTES], after)

    def test_existing_cache_does_not_block_download_that_fits(self) -> None:
        """End-to-end: a populated cache plus enough free space still downloads."""
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            existing = cache.path.stat().st_size
            self.assertGreater(existing, 0)
            provider = DukascopyProvider(
                cache,
                fetch_artifact=lambda *_: payload(date(2025, 1, 2)),
                disk_threshold_bytes=0,
                journal_reserve_bytes=0,
            )
            # Free space covers the incremental download, but not the
            # incremental download *plus* the already-consumed cache bytes.
            free = PER_DAY_ESTIMATE_BYTES + existing // 2
            usage = Usage(free * 2, free, free)

            with patch(
                "goldenfibo.marketdata.historical.shutil.disk_usage",
                return_value=usage,
            ):
                rows = provider.fetch_range(request())

            self.assertEqual(2, len(rows))

    def test_insufficient_incremental_free_space_is_rejected(self) -> None:
        """Insufficient room for the incremental growth must still be refused."""
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            provider = DukascopyProvider(
                cache,
                fetch_artifact=lambda *_: payload(date(2025, 1, 2)),
                disk_threshold_bytes=0,
            )
            usage = Usage(10_000, 9_000, 1_000)

            with patch(
                "goldenfibo.marketdata.historical.shutil.disk_usage",
                return_value=usage,
            ):
                with self.assertRaises(RuntimeError):
                    provider._check_disk(1)

    def test_configured_minimum_free_space_threshold_is_enforced(self) -> None:
        """The configured threshold still gates the incremental growth."""
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            provider = DukascopyProvider(
                cache,
                fetch_artifact=lambda *_: payload(date(2025, 1, 2)),
                disk_threshold_bytes=1_000,
                journal_reserve_bytes=0,
            )
            just_short = PER_DAY_ESTIMATE_BYTES + 1_000 - 1
            usage = Usage(just_short * 2, just_short, just_short)

            with patch(
                "goldenfibo.marketdata.historical.shutil.disk_usage",
                return_value=usage,
            ):
                with self.assertRaises(RuntimeError):
                    provider._check_disk(1)

            exactly_enough = PER_DAY_ESTIMATE_BYTES + 1_000
            usage_ok = Usage(exactly_enough * 2, exactly_enough, exactly_enough)

            with patch(
                "goldenfibo.marketdata.historical.shutil.disk_usage",
                return_value=usage_ok,
            ):
                provider._check_disk(1)

    def test_journal_reserve_is_still_reserved_on_top_of_incremental_growth(self) -> None:
        """The journal/WAL reserve is added to the incremental growth."""
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            provider = DukascopyProvider(
                cache,
                fetch_artifact=lambda *_: payload(date(2025, 1, 2)),
                disk_threshold_bytes=0,
                journal_reserve_bytes=5_000,
            )
            without_reserve = PER_DAY_ESTIMATE_BYTES
            usage = Usage(without_reserve * 2, without_reserve, without_reserve)

            with patch(
                "goldenfibo.marketdata.historical.shutil.disk_usage",
                return_value=usage,
            ):
                with self.assertRaises(RuntimeError):
                    provider._check_disk(1)

            with_reserve = PER_DAY_ESTIMATE_BYTES + 5_000
            usage_ok = Usage(with_reserve * 2, with_reserve, with_reserve)

            with patch(
                "goldenfibo.marketdata.historical.shutil.disk_usage",
                return_value=usage_ok,
            ):
                provider._check_disk(1)

    def test_disk_reserve_rejects_before_any_provider_request(self) -> None:
        """Failure handling is unchanged: the fetch never starts."""
        called: list = []
        with tempfile.TemporaryDirectory() as td:
            provider = DukascopyProvider(
                DukascopyKlineCache(Path(td) / "x.sqlite"),
                fetch_artifact=lambda *_: (called.append(1) or payload(date(2025, 1, 2))),
                disk_threshold_bytes=1_000,
                journal_reserve_bytes=1_000,
            )
            usage = Usage(10_000, 9_500, 500)

            with patch(
                "goldenfibo.marketdata.historical.shutil.disk_usage",
                return_value=usage,
            ):
                with self.assertRaises(RuntimeError):
                    provider.fetch_range(request())

            self.assertEqual([], called)


if __name__ == "__main__":
    unittest.main()
