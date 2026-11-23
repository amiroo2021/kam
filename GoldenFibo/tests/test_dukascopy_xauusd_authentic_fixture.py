from __future__ import annotations

import hashlib
import lzma
import struct
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from goldenfibo.marketdata.dukascopy_format import parse_bi5_m1
from goldenfibo.marketdata.dukascopy_provider import DukascopyProvider, DukascopyProviderError
from goldenfibo.marketdata.dukascopy_cache import DukascopyKlineCache


FIXTURE = Path(__file__).parent / "fixtures/dukascopy/XAUUSD/2024/01/03/BID_candles_min_1.bi5"
EXPECTED_SHA256 = "03cf70fe768099d93f5256d7209c9ff06e7c7e61911bc8ce1ca95549b3fde5e3"


class DukascopyXauUsdAuthenticFixtureTests(unittest.TestCase):
    def test_authenticated_bid_fixture_decodes_with_scale_and_coverage(self) -> None:
        payload = FIXTURE.read_bytes()
        self.assertEqual(EXPECTED_SHA256, hashlib.sha256(payload).hexdigest())

        raw = lzma.decompress(payload)
        self.assertEqual(24, struct.calcsize(">5If"))
        self.assertEqual(1440, len(raw) // 24)
        self.assertEqual((0, 2059475, 2059155, 2059155, 2059545), struct.unpack_from(">5If", raw, 0)[:5])

        candles = parse_bi5_m1(payload, date(2024, 1, 3), price_scale=1000.0, price_side="bid")
        self.assertEqual(1440, len(candles))
        self.assertEqual("bid", candles[0].price_side)
        self.assertEqual((2059.475, 2059.545, 2059.155, 2059.155), candles[0].ohlc)
        self.assertEqual(1704240000000, candles[0].open_time_ms)
        self.assertEqual(1704326340000, candles[-1].open_time_ms)
        self.assertEqual((2030.515, 2065.835), (min(v for c in candles for v in c.ohlc), max(v for c in candles for v in c.ohlc)))
        self.assertTrue(all(c.volume >= 0 for c in candles))

    def test_xauusd_is_enabled_and_other_unverified_instruments_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            provider = DukascopyProvider(
                DukascopyKlineCache(Path(td) / "x.sqlite"),
                fetch_artifact=lambda *_: b"",
                disk_threshold_bytes=0,
            )
            self.assertEqual("xauusd", provider.validate_instrument("XAUUSD"))
            self.assertEqual(1000.0, provider.instrument_scales["xauusd"])
            self.assertEqual(100000.0, provider.instrument_scales["eurusd"])
            for symbol in ("USDJPY", "BTCUSD", "XAGUSD"):
                with self.subTest(unverified=symbol):
                    with self.assertRaises(DukascopyProviderError):
                        provider.validate_instrument(symbol)


if __name__ == "__main__":
    unittest.main()
