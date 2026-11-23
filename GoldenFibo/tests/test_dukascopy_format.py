from __future__ import annotations

import lzma
import struct
import unittest
from datetime import date

from goldenfibo.marketdata.dukascopy_format import (
    DukascopyFormatError,
    parse_bi5_m1,
    validate_candles,
)


class DukascopyFormatTests(unittest.TestCase):
    def test_parse_lzma_bi5_preserves_m1_ohlc_volume_and_timestamp(self) -> None:
        raw = b"".join(
            # Native Dukascopy order is timestamp, open, close, low, high, volume.
            struct.pack(">5If", offset, 100000, 100500, 99000, 101000, 12.5)
            for offset in (0, 60)
        )
        candles = parse_bi5_m1(lzma.compress(raw), date(2025, 1, 2), price_scale=1000)
        self.assertEqual(2, len(candles))
        self.assertEqual(1735776000000, candles[0].open_time_ms)
        self.assertEqual((100.0, 101.0, 99.0, 100.5), candles[0].bid_ohlc)
        self.assertEqual(12.5, candles[0].volume)
        self.assertEqual(1735776060000, candles[1].open_time_ms)

    def test_native_close_and_high_are_not_swapped(self) -> None:
        raw = struct.pack(">5If", 0, 100000, 100500, 99000, 101000, 12.5)
        candle = parse_bi5_m1(lzma.compress(raw), date(2025, 1, 2), price_scale=1000)[0]
        self.assertEqual((100.0, 101.0, 99.0, 100.5), candle.ohlc)

    def test_rejects_invalid_ohlc_invariants(self) -> None:
        cases = (
            (100000, 100500, 99000, 99500),  # high below open
            (100000, 100500, 100100, 101000),  # low above close
            (100000, 100500, 102000, 101000),  # high below low
            (0, 100500, 99000, 101000),  # non-positive open
        )
        for open_, close, low, high in cases:
            with self.subTest(open=open_, close=close, low=low, high=high):
                raw = struct.pack(">5If", 0, open_, close, low, high, 1.0)
                with self.assertRaises(DukascopyFormatError):
                    parse_bi5_m1(lzma.compress(raw), date(2025, 1, 2), price_scale=1000)

    def test_rejects_partial_record(self) -> None:
        with self.assertRaises(DukascopyFormatError):
            parse_bi5_m1(lzma.compress(b"short"), date(2025, 1, 2), price_scale=1000)

    def test_validation_rejects_duplicate_or_misaligned_candles(self) -> None:
        raw = b"".join(
            struct.pack(">5If", offset, 100000, 101000, 99000, 100500, 1.0)
            for offset in (0, 0)
        )
        with self.assertRaises(DukascopyFormatError):
            parse_bi5_m1(lzma.compress(raw), date(2025, 1, 2), price_scale=1000)


if __name__ == "__main__":
    unittest.main()
