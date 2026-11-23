"""Dukascopy independent BID/ASK coverage regressions.

Contract under test
-------------------
BID and ASK coverage are tracked as two INDEPENDENT facts about a day:

* a successful BID acquisition verifies BID coverage for that day,
* a successful ASK acquisition verifies ASK coverage for that day,
* a failure on ONE side must never invalidate the other side's already
  verified coverage.

Required regression sequence:

1. BID successfully downloads and is verified.
2. ASK acquisition fails.
3. BID is requested again.
4. BID is served from the existing verified cache.
5. Zero additional provider requests occur.

The candidate stored a single ``status`` per (instrument, day). A failed ASK
overwrote the day's status with ``failed``, which made the verified BID
coverage look incomplete and forced a re-download of BID.
"""

from __future__ import annotations

import lzma
import sqlite3
import struct
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from goldenfibo.marketdata.dukascopy_cache import DukascopyKlineCache
from goldenfibo.marketdata.dukascopy_format import parse_bi5_m1
from goldenfibo.marketdata.dukascopy_provider import DukascopyProvider, DukascopyProviderError
from goldenfibo.marketdata.historical import HistoricalRequest

DAY = date(2025, 1, 2)
BID_VOLUME = 11.0
ASK_VOLUME = 22.0


def payload(day: date = DAY, volume: float = 2.0) -> bytes:
    raw = b"".join(
        struct.pack(">5If", offset, 100000, 100500, 99000, 101000, volume)
        for offset in (0, 60)
    )
    return lzma.compress(raw)


def request(day: date = DAY, side: str = "bid") -> HistoricalRequest:
    start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
    return HistoricalRequest("EURUSD", start, start + 120_000, side)


class SideAwareFetch:
    """Provider stub that records every artifact request and can fail one side."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.calls: list[tuple[date, str]] = []

    def __call__(self, _instrument: str, day: date, side: str) -> bytes:
        self.calls.append((day, side))
        if side in self.fail:
            raise OSError(f"{side} acquisition failed")
        return payload(day, volume=BID_VOLUME if side == "bid" else ASK_VOLUME)


class DukascopyIndependentSideCoverageTests(unittest.TestCase):
    def test_failed_ask_does_not_invalidate_verified_bid(self) -> None:
        """Steps 1-5: verified BID survives a failed ASK and is reused."""
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            fetch = SideAwareFetch()
            provider = DukascopyProvider(
                cache,
                fetch_artifact=fetch,
                disk_threshold_bytes=0,
                max_retries=0,
                refresh_tail_days=0,
            )

            # 1. BID downloads and is verified.
            rows = provider.fetch_range(request(side="bid"))
            self.assertEqual(2, len(rows))
            self.assertEqual([(DAY, "bid")], fetch.calls)

            coverage = cache.coverage("eurusd", DAY, DAY)[0]
            self.assertTrue(coverage.bid_complete)
            self.assertFalse(coverage.ask_complete)

            # 2. ASK acquisition fails.
            fetch.fail = {"ask"}
            with self.assertRaises(DukascopyProviderError):
                provider.fetch_range(request(side="ask"))

            # The failed ASK must not clear the verified BID.
            coverage = cache.coverage("eurusd", DAY, DAY)[0]
            self.assertTrue(
                coverage.bid_complete,
                "a failed ASK must not invalidate the previously verified BID",
            )
            self.assertFalse(coverage.ask_complete)

            # 3-4. BID is requested again.
            calls_before = len(fetch.calls)
            rows = provider.fetch_range(request(side="bid"))

            # 5. Zero additional provider requests occur.
            self.assertEqual(calls_before, len(fetch.calls))
            self.assertEqual([(DAY, "bid"), (DAY, "ask")], fetch.calls)
            self.assertEqual(2, len(rows))

    def test_failed_bid_does_not_invalidate_verified_ask(self) -> None:
        """Mirror image: a verified ASK survives a failed BID."""
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            fetch = SideAwareFetch()
            provider = DukascopyProvider(
                cache,
                fetch_artifact=fetch,
                disk_threshold_bytes=0,
                max_retries=0,
                refresh_tail_days=0,
            )

            rows = provider.fetch_range(request(side="ask"))
            self.assertEqual(2, len(rows))
            self.assertEqual([(DAY, "ask")], fetch.calls)

            coverage = cache.coverage("eurusd", DAY, DAY)[0]
            self.assertTrue(coverage.ask_complete)
            self.assertFalse(coverage.bid_complete)

            fetch.fail = {"bid"}
            with self.assertRaises(DukascopyProviderError):
                provider.fetch_range(request(side="bid"))

            coverage = cache.coverage("eurusd", DAY, DAY)[0]
            self.assertTrue(
                coverage.ask_complete,
                "a failed BID must not invalidate the previously verified ASK",
            )
            self.assertFalse(coverage.bid_complete)

            calls_before = len(fetch.calls)
            rows = provider.fetch_range(request(side="ask"))
            self.assertEqual(calls_before, len(fetch.calls))
            self.assertEqual([(DAY, "ask"), (DAY, "bid")], fetch.calls)
            self.assertEqual(2, len(rows))

    def test_both_sides_successfully_verified(self) -> None:
        """Both sides verified => both sides complete for the day."""
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            fetch = SideAwareFetch()
            provider = DukascopyProvider(
                cache,
                fetch_artifact=fetch,
                disk_threshold_bytes=0,
                max_retries=0,
                refresh_tail_days=0,
            )

            provider.fetch_range(request(side="bid"))
            provider.fetch_range(request(side="ask"))

            coverage = cache.coverage("eurusd", DAY, DAY)[0]
            self.assertTrue(coverage.bid_complete)
            self.assertTrue(coverage.ask_complete)
            self.assertEqual(
                [],
                cache.missing_days("eurusd", DAY, DAY),
                "a fully verified day must not be reported as missing",
            )

    def test_failed_side_retry_does_not_invalidate_the_successful_side(self) -> None:
        """A later failed retry on one side leaves the other side verified."""
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            fetch = SideAwareFetch()
            provider = DukascopyProvider(
                cache,
                fetch_artifact=fetch,
                disk_threshold_bytes=0,
                max_retries=0,
                refresh_tail_days=0,
            )

            provider.fetch_range(request(side="bid"))

            # A failed ASK, then a successful ASK: BID stayed verified the
            # whole time and ASK becomes verified at the end.
            fetch.fail = {"ask"}
            with self.assertRaises(DukascopyProviderError):
                provider.fetch_range(request(side="ask"))
            fetch.fail = set()
            provider.fetch_range(request(side="ask"))

            coverage = cache.coverage("eurusd", DAY, DAY)[0]
            self.assertTrue(coverage.bid_complete)
            self.assertTrue(coverage.ask_complete)

    def test_side_specific_volume_is_preserved(self) -> None:
        """BID and ASK volume columns stay independent."""
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            cache.upsert_side(
                "eurusd",
                parse_bi5_m1(payload(volume=BID_VOLUME), DAY, price_scale=100_000, price_side="bid"),
            )
            cache.upsert_side(
                "eurusd",
                parse_bi5_m1(payload(volume=ASK_VOLUME), DAY, price_scale=100_000, price_side="ask"),
            )

            bid = cache.rows("eurusd", 0, 2**63 - 1, price_side="bid")
            ask = cache.rows("eurusd", 0, 2**63 - 1, price_side="ask")

            self.assertEqual(BID_VOLUME, bid[0][5])
            self.assertEqual(ASK_VOLUME, ask[0][5])

    def test_verified_weekend_closure_is_accepted_for_both_sides(self) -> None:
        """A verified closure day satisfies coverage for the requested side."""
        saturday = date(2025, 1, 4)
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            fetch = SideAwareFetch()
            provider = DukascopyProvider(
                cache,
                fetch_artifact=fetch,
                disk_threshold_bytes=0,
                max_retries=0,
                refresh_tail_days=0,
            )
            start = int(datetime(2025, 1, 4, tzinfo=timezone.utc).timestamp() * 1000)
            end = int(datetime(2025, 1, 6, tzinfo=timezone.utc).timestamp() * 1000)

            with self.assertRaises(DukascopyProviderError):
                provider.fetch_range(HistoricalRequest("EURUSD", start, end, "bid"))

            coverage = cache.coverage("eurusd", saturday, saturday)[0]
            self.assertEqual("verified_provider_empty", coverage.status)
            self.assertTrue(coverage.bid_complete)
            self.assertTrue(coverage.ask_complete)
            self.assertEqual(
                [],
                cache.missing_days("eurusd", saturday, saturday),
                "a verified closure day must not be reported as missing",
            )

    def test_unverified_weekday_gap_remains_rejected(self) -> None:
        """An unverified weekday gap must still raise."""
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            fetch = SideAwareFetch()
            provider = DukascopyProvider(
                cache,
                fetch_artifact=fetch,
                disk_threshold_bytes=0,
                max_retries=0,
                refresh_tail_days=0,
            )
            start = int(datetime(2025, 1, 2, tzinfo=timezone.utc).timestamp() * 1000)
            end = int(datetime(2025, 1, 3, 0, 2, tzinfo=timezone.utc).timestamp() * 1000)

            # Only the first day is fetched; the second day never completes,
            # so the intra-day gap check must reject the range.
            def fetch_two_days(_instrument, day, side):
                fetch.calls.append((day, side))
                if day == date(2025, 1, 3):
                    raise OSError("gap day unavailable")
                return payload(day)

            provider.fetch_artifact = fetch_two_days
            with self.assertRaises(DukascopyProviderError):
                provider.fetch_range(HistoricalRequest("EURUSD", start, end, "bid"))

    def test_existing_sqlite_database_migrates_and_preserves_side_coverage(self) -> None:
        """A legacy DB without side columns migrates and keeps its data."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "legacy.sqlite"
            with sqlite3.connect(path) as db:
                db.executescript(
                    """
                    CREATE TABLE candles (
                        instrument TEXT NOT NULL,
                        open_time_ms INTEGER NOT NULL,
                        bid_open REAL, bid_high REAL, bid_low REAL, bid_close REAL,
                        ask_open REAL, ask_high REAL, ask_low REAL, ask_close REAL,
                        volume REAL,
                        volume_units TEXT NOT NULL DEFAULT 'provider',
                        source_day TEXT NOT NULL,
                        PRIMARY KEY (instrument, open_time_ms)
                    );
                    CREATE TABLE coverage_days (
                        instrument TEXT NOT NULL,
                        day TEXT NOT NULL,
                        status TEXT NOT NULL,
                        artifact TEXT,
                        verified_at_ms INTEGER NOT NULL,
                        note TEXT,
                        PRIMARY KEY (instrument, day)
                    );
                    """
                )
                db.execute(
                    "INSERT INTO coverage_days(instrument,day,status,verified_at_ms) VALUES(?,?,?,?)",
                    ("eurusd", DAY.isoformat(), "verified_data", 1),
                )
                db.execute(
                    "INSERT INTO candles(instrument,open_time_ms,bid_open,bid_high,bid_low,bid_close,volume,source_day)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    ("eurusd", 1_735_776_000_000, 1.0, 1.1, 0.9, 1.05, 3.0, DAY.isoformat()),
                )

            cache = DukascopyKlineCache(path)
            coverage = cache.coverage("eurusd", DAY, DAY)[0]
            self.assertEqual("verified_data", coverage.status)
            # A legacy row carried no side flags; it must not silently claim
            # to be verified on either side.
            self.assertFalse(coverage.bid_complete)
            self.assertFalse(coverage.ask_complete)

            # The legacy candle row survived the migration untouched.
            legacy_rows = cache.rows("eurusd", 0, 2**63 - 1, price_side="bid")
            self.assertEqual(1, len(legacy_rows))
            self.assertEqual(1.05, legacy_rows[0][4])

            # The migrated DB must accept new side-aware writes.
            cache.upsert_side(
                "eurusd",
                parse_bi5_m1(payload(), DAY, price_scale=100_000, price_side="bid"),
            )
            cache.mark_day("eurusd", DAY, "verified_data", bid_complete=True, ask_complete=False)
            coverage = cache.coverage("eurusd", DAY, DAY)[0]
            self.assertTrue(coverage.bid_complete)
            self.assertFalse(coverage.ask_complete)

            rows = cache.rows("eurusd", 0, 2**63 - 1, price_side="bid")
            self.assertEqual(2, len(rows))
            # The re-downloaded artifact replaced the legacy close (100500/100000).
            self.assertEqual(1.005, rows[-1][4])


    def test_market_closure_reclassification_does_not_downgrade_verified_data(self) -> None:
        """A closure reclassification must not lose a genuine verification.

        A day already verified with real BID candles must keep that
        verification when the provider later classifies the day as closed.
        Losing it would force a needless re-download and is the exact
        failure mode the per-side state exists to prevent.
        """
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            cache.mark_day(
                "eurusd",
                DAY,
                "verified_data",
                bid_complete=True,
                ask_complete=False,
                bid_status="verified_data",
                ask_status="",
            )

            # The provider classifies the day as closed.
            cache.mark_day(
                "eurusd",
                DAY,
                "verified_provider_empty",
                bid_complete=True,
                ask_complete=True,
                bid_status="verified_provider_empty",
                ask_status="verified_provider_empty",
            )

            coverage = cache.coverage("eurusd", DAY, DAY)[0]
            self.assertEqual(
                "verified_data",
                coverage.bid_status,
                "a genuine BID verification must survive a closure reclassification",
            )
            self.assertTrue(cache.side_is_verified(coverage, "bid"))

    def test_stale_explicit_peer_status_cannot_overwrite_a_newer_verification(self) -> None:
        """A stale peer status must not overwrite a newer verification.

        The provider reads the prior row, then writes. If the peer side was
        verified in between, the stale value must not clobber it. The stale
        value here is a NON-EMPTY status (the provider reported the peer's
        previously known status), which is the case that silently lost a
        genuine verification.
        """
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            cache.mark_day(
                "eurusd",
                DAY,
                "verified_data",
                bid_complete=True,
                ask_complete=False,
                bid_status="verified_data",
                ask_status="",
            )

            # The provider captured a stale prior: the peer had a status, but
            # that status was superseded by a newer verification.
            stale_peer_status = "failed"

            # Another writer verifies the peer side.
            with sqlite3.connect(cache.path) as db:
                db.execute(
                    "UPDATE coverage_days SET ask_status='verified_data', ask_complete=1 "
                    "WHERE instrument='eurusd' AND day=?",
                    (DAY.isoformat(),),
                )

            # The provider writes using the stale peer value.
            cache.mark_day(
                "eurusd",
                DAY,
                "failed",
                bid_complete=True,
                ask_complete=False,
                bid_status="verified_data",
                ask_status=stale_peer_status,
            )

            coverage = cache.coverage("eurusd", DAY, DAY)[0]
            self.assertEqual(
                "verified_data",
                coverage.ask_status,
                "a stale peer status must not overwrite a newer verification",
            )
            self.assertTrue(cache.side_is_verified(coverage, "ask"))

    def test_failed_side_marks_only_the_failed_side(self) -> None:
        """A failed acquisition must not mark the peer side failed."""
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "x.sqlite")
            cache.mark_day(
                "eurusd",
                DAY,
                "verified_data",
                bid_complete=True,
                ask_complete=False,
                bid_status="verified_data",
                ask_status="",
            )

            cache.mark_day(
                "eurusd",
                DAY,
                "failed",
                bid_complete=True,
                ask_complete=False,
                bid_status="verified_data",
                ask_status="failed",
            )

            coverage = cache.coverage("eurusd", DAY, DAY)[0]
            self.assertEqual("verified_data", coverage.bid_status)
            self.assertEqual("failed", coverage.ask_status)
            self.assertTrue(cache.side_is_verified(coverage, "bid"))
            self.assertFalse(cache.side_is_verified(coverage, "ask"))


if __name__ == "__main__":
    unittest.main()
