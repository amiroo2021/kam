from __future__ import annotations

import lzma
import struct
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from goldenfibo.marketdata.dukascopy_cache import DukascopyKlineCache
from goldenfibo.marketdata.dukascopy_format import parse_bi5_m1
from goldenfibo.marketdata.dukascopy_provider import DukascopyNoDataError, DukascopyProvider, DukascopyProviderError
from goldenfibo.marketdata.historical import HistoricalRequest


def payload(day: date, side: str = "bid", volume: float = 2.0) -> bytes:
    raw = b"".join(struct.pack(">5If", offset, 100000, 100500, 99000, 101000, volume) for offset in (0, 60))
    return lzma.compress(raw)


class DukascopyCacheTests(unittest.TestCase):
    def test_bid_and_ask_share_rows_without_mixing_databases(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "dukascopy_klines.sqlite")
            day = date(2025, 1, 2)
            cache.upsert_side("eurusd", parse_bi5_m1(payload(day), day, price_scale=1000, price_side="bid"))
            cache.upsert_side("eurusd", parse_bi5_m1(payload(day), day, price_scale=1000, price_side="ask"))
            cache.mark_day("eurusd", day, "verified_data")
            bid = cache.rows("eurusd", 0, 2**63 - 1, price_side="bid")
            ask = cache.rows("eurusd", 0, 2**63 - 1, price_side="ask")
            self.assertEqual(2, len(bid))
            self.assertEqual(bid[0][1:5], ask[0][1:5])
            self.assertEqual("verified_data", cache.coverage("eurusd", day, day)[0].status)

    def test_bid_and_ask_volumes_remain_side_specific(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "dukascopy_klines.sqlite")
            day = date(2025, 1, 2)
            cache.upsert_side(
                "eurusd",
                parse_bi5_m1(payload(day, volume=11.0), day, price_scale=1000, price_side="bid"),
            )
            cache.upsert_side(
                "eurusd",
                parse_bi5_m1(payload(day, volume=22.0), day, price_scale=1000, price_side="ask"),
            )

            bid = cache.rows("eurusd", 0, 2**63 - 1, price_side="bid")
            ask = cache.rows("eurusd", 0, 2**63 - 1, price_side="ask")

            self.assertEqual(11.0, bid[0][5])
            self.assertEqual(22.0, ask[0][5])

    def test_missing_days_are_idempotent_and_refreshable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "dukascopy_klines.sqlite")
            start = date(2025, 1, 1)
            end = date(2025, 1, 3)
            cache.mark_day("btcusd", start, "verified_data", bid_complete=True, ask_complete=True)
            self.assertEqual([date(2025, 1, 2), end], cache.missing_days("btcusd", start, end))
            self.assertEqual([start, date(2025, 1, 2), end], cache.missing_days("btcusd", start, end, refresh_days={start}))


class DukascopyProviderTests(unittest.TestCase):
    def test_fetches_daily_gaps_resumes_and_emits_final_validation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "dukascopy_klines.sqlite")
            calls = []
            provider = DukascopyProvider(cache, fetch_artifact=lambda instrument, day, side: (calls.append((day, side)) or payload(day, side)), disk_threshold_bytes=0)
            start = int(datetime(2025, 1, 2, tzinfo=timezone.utc).timestamp() * 1000)
            events = []
            rows = provider.fetch_range(HistoricalRequest("EUR/USD", start, start + 120_000, "bid"), on_progress=events.append)
            self.assertEqual(2, len(rows))
            self.assertEqual([(date(2025, 1, 2), "bid")], calls)
            self.assertEqual(["loading_cache", "downloading_gaps", "validating", "cache_ready"], [x["stage"] for x in events])
            rows2 = provider.fetch_range(HistoricalRequest("EUR/USD", start, start + 120_000, "ask"))
            self.assertEqual(2, len(rows2))
            self.assertEqual([(date(2025, 1, 2), "bid"), (date(2025, 1, 2), "ask")], calls)

    def test_fresh_weekend_only_request_is_classified_as_verified_closure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "dukascopy_klines.sqlite")
            provider = DukascopyProvider(
                cache,
                fetch_artifact=lambda *_: self.fail("closed day must not fetch"),
                disk_threshold_bytes=0,
            )
            start = int(datetime(2025, 1, 4, tzinfo=timezone.utc).timestamp() * 1000)
            end = int(datetime(2025, 1, 6, tzinfo=timezone.utc).timestamp() * 1000)

            with self.assertRaises(DukascopyNoDataError) as ctx:
                provider.fetch_range(HistoricalRequest("EURUSD", start, end, "bid"))

            self.assertEqual("verified_closure_only", ctx.exception.reason)

    def test_friday_to_monday_range_allows_verified_weekend_gap(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "dukascopy_klines.sqlite")
            provider = DukascopyProvider(
                cache,
                fetch_artifact=lambda _instrument, day, side: payload(day, side),
                disk_threshold_bytes=0,
            )
            start = int(datetime(2025, 1, 3, tzinfo=timezone.utc).timestamp() * 1000)
            end = int(datetime(2025, 1, 6, 0, 2, tzinfo=timezone.utc).timestamp() * 1000)

            rows = provider.fetch_range(HistoricalRequest("EURUSD", start, end, "bid"))

            self.assertEqual(4, len(rows))
            self.assertEqual(start, rows[0][0])
            self.assertEqual(int(datetime(2025, 1, 6, tzinfo=timezone.utc).timestamp() * 1000), rows[2][0])

    def test_adjacent_weekday_gap_is_rejected_without_verified_closure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "dukascopy_klines.sqlite")
            provider = DukascopyProvider(
                cache,
                fetch_artifact=lambda _instrument, day, side: payload(day, side),
                disk_threshold_bytes=0,
            )
            start = int(datetime(2025, 1, 2, tzinfo=timezone.utc).timestamp() * 1000)
            end = int(datetime(2025, 1, 3, 0, 2, tzinfo=timezone.utc).timestamp() * 1000)

            with self.assertRaisesRegex(DukascopyProviderError, "unverified intra-day gap"):
                provider.fetch_range(HistoricalRequest("EURUSD", start, end, "bid"))

    def test_dynamic_catalog_scale_validates_other_instrument(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "dukascopy_klines.sqlite")
            provider = DukascopyProvider(cache, fetch_artifact=lambda *_: payload(date(2025, 1, 2)), instrument_catalog=lambda symbol: 1000.0 if symbol == "customusd" else None, disk_threshold_bytes=0)
            self.assertEqual("customusd", provider.validate_instrument("CUSTOM/USD"))

        with tempfile.TemporaryDirectory() as td:
            cache = DukascopyKlineCache(Path(td) / "dukascopy_klines.sqlite")
            provider = DukascopyProvider(cache, fetch_artifact=lambda *_: b"bad", disk_threshold_bytes=0)
            start = int(datetime(2025, 1, 2, tzinfo=timezone.utc).timestamp() * 1000)
            with self.assertRaises(DukascopyProviderError):
                provider.fetch_range(HistoricalRequest("NOTREAL", start, start + 60_000, "bid"))
            with self.assertRaises(DukascopyProviderError):
                provider.fetch_range(HistoricalRequest("EURUSD", start, start + 60_000, "bid"))
            self.assertNotEqual("verified_data", cache.coverage("eurusd", date(2025, 1, 2), date(2025, 1, 2))[0].status)


if __name__ == "__main__":
    unittest.main()
