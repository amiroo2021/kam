from __future__ import annotations

import lzma
import struct
import unittest
from datetime import date
from pathlib import Path

from goldenfibo.marketdata.dukascopy_format import parse_bi5_m1


FIXTURE = Path(__file__).parent / "fixtures/dukascopy/EURUSD/2019/01/04/ASK_candles_min_1.bi5"


class DukascopyAuthenticFixtureTests(unittest.TestCase):
    def test_established_repository_fixture_decodes_as_ask_m1(self) -> None:
        """Fixture provenance: theorycraft-trading/dukascopy, Apache-2.0."""
        payload = FIXTURE.read_bytes()
        raw = lzma.decompress(payload)
        first = struct.unpack_from(">5If", raw, 0)
        self.assertEqual((0, 114545, 114574, 114545, 114574), first[:5])

        candles = parse_bi5_m1(payload, date(2019, 2, 4), price_scale=100_000, price_side="ask")
        self.assertEqual(1440, len(candles))
        self.assertEqual((1.14545, 1.14574, 1.14545, 1.14574), candles[0].ohlc)
        self.assertEqual("ask", candles[0].price_side)


if __name__ == "__main__":
    unittest.main()