"""
WebTrade2 /api/candles contract tests.

Regression: chart was blank because the API returned mixed timestamp units
(Hyperliquid in seconds, Apex in milliseconds). Lightweight Charts v4.2
expects `time` in UTC SECONDS at all times. The fix is in
plugins/trade/candles.py::handle_candles_operation which now normalizes
all candles to seconds, de-duplicates, sorts ascending, and drops invalid
OHLC rows.

These tests exercise handle_candles_operation with synthetic fetch results
representative of real upstream shapes:

  - Hyperliquid (candles already in seconds).
  - Apex (candles in milliseconds).
  - Microseconds (rare, but handled).
  - Duplicates (must be collapsed).
  - Invalid OHLC (must be dropped).
  - Empty result.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "/root/kam")

from plugins.trade import candles as candles_mod


def _ts_seconds(hours_offset: int = 0, base: int = 1_790_000_000) -> int:
    return base + hours_offset * 3600


def _ts_ms(seconds: int) -> int:
    return seconds * 1000


def _ok_ohlc(time_, o=100.0, h=101.0, l=99.0, c=100.5, v=10.0):
    return {"time": time_, "open": o, "high": h, "low": l, "close": c, "volume": v}


# Accept None / strings deliberately for invalid-OHLC tests (the contract
# strips them, but the raw upstream sometimes returns them).
def _bad_ohlc(time_, field: str, bad_value) -> dict:
    base = {"time": time_, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0}
    base[field] = bad_value
    return base


def _run_handle(exchange: str, raw_candles):
    """Run handle_candles_operation with mocked fetch_for_exchange."""
    with mock.patch.object(candles_mod, "fetch_for_exchange", return_value=raw_candles):
        out = candles_mod.handle_candles_operation(
            exchange, "fibo",
            {"symbol": "X", "interval": "1m", "limit": 100, "market_type": "futures"},
        )
    return out.to_dict()


class WebTrade2CandlesContractTests(unittest.TestCase):

    # ---- /api/candles contract: always SECONDS ----

    def test_hyperliquid_candles_already_seconds_kept_as_seconds(self) -> None:
        # Real HL shape: time in seconds.
        t1 = _ts_seconds(0)
        t2 = _ts_seconds(1)
        raw = [_ok_ohlc(t1), _ok_ohlc(t2)]
        out = _run_handle("hyperliquid", raw)
        self.assertTrue(out.get("success"), out)
        cs = out["data"]["candles"]
        self.assertEqual(len(cs), 2)
        self.assertEqual(cs[0]["time"], t1, "HL seconds must remain seconds (no divide-by-1000)")
        self.assertEqual(cs[1]["time"], t2)

    def test_apex_candles_milliseconds_converted_to_seconds(self) -> None:
        # Real Apex shape: time in milliseconds.
        s1 = _ts_seconds(0)
        s2 = _ts_seconds(1)
        raw = [_ok_ohlc(_ts_ms(s1)), _ok_ohlc(_ts_ms(s2))]
        out = _run_handle("apex", raw)
        self.assertTrue(out.get("success"), out)
        cs = out["data"]["candles"]
        self.assertEqual(len(cs), 2)
        self.assertEqual(cs[0]["time"], s1, "Apex ms must be divided to seconds")
        self.assertEqual(cs[1]["time"], s2)

    def test_microseconds_converted_to_seconds(self) -> None:
        s1 = _ts_seconds(0)
        us = s1 * 1_000_000
        raw = [_ok_ohlc(us)]
        out = _run_handle("apex", raw)
        self.assertTrue(out.get("success"))
        cs = out["data"]["candles"]
        self.assertEqual(cs[0]["time"], s1)

    # ---- Dedup, ascending sort, invalid-OHLC drop ----

    def test_duplicate_timestamps_deduped(self) -> None:
        t = _ts_seconds(0)
        raw = [_ok_ohlc(t), _ok_ohlc(t), _ok_ohlc(t)]
        out = _run_handle("apex", raw)
        cs = out["data"]["candles"]
        self.assertEqual(len(cs), 1)

    def test_candles_returned_ascending(self) -> None:
        # Reverse the order; API must sort ascending.
        raw = [_ok_ohlc(_ts_seconds(5)), _ok_ohlc(_ts_seconds(3)),
               _ok_ohlc(_ts_seconds(7)), _ok_ohlc(_ts_seconds(1))]
        out = _run_handle("apex", raw)
        cs = out["data"]["candles"]
        ts = [c["time"] for c in cs]
        self.assertEqual(ts, sorted(ts))
        self.assertEqual(ts, [_ts_seconds(1), _ts_seconds(3), _ts_seconds(5), _ts_seconds(7)])

    def test_invalid_ohlc_dropped(self) -> None:
        raw = [
            _ok_ohlc(_ts_seconds(0)),
            _bad_ohlc(_ts_seconds(1), "open", float("nan")),
            _bad_ohlc(_ts_seconds(2), "high", None),
            _bad_ohlc(_ts_seconds(3), "low", "bad"),
            _bad_ohlc(_ts_seconds(4), "close", float("inf")),
            _ok_ohlc(_ts_seconds(5)),
        ]
        out = _run_handle("apex", raw)
        cs = out["data"]["candles"]
        # Only the 2 fully valid rows remain.
        self.assertEqual(len(cs), 2)
        self.assertEqual([c["time"] for c in cs], [_ts_seconds(0), _ts_seconds(5)])

    def test_zero_or_negative_time_dropped(self) -> None:
        raw = [_ok_ohlc(0), _ok_ohlc(-1), _ok_ohlc(_ts_seconds(0))]
        out = _run_handle("apex", raw)
        cs = out["data"]["candles"]
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0]["time"], _ts_seconds(0))

    def test_empty_candles_returns_empty_list(self) -> None:
        out = _run_handle("apex", [])
        cs = out["data"]["candles"]
        self.assertEqual(cs, [])
        self.assertEqual(out["data"]["count"], 0)

    def test_string_time_coerced_to_int(self) -> None:
        # Upstream occasionally returns stringified timestamps.
        raw = [_ok_ohlc(str(_ts_ms(_ts_seconds(2))))]
        out = _run_handle("apex", raw)
        cs = out["data"]["candles"]
        self.assertEqual(cs[0]["time"], _ts_seconds(2))

    # ---- All response fields are numbers ----

    def test_ohlc_are_numbers_not_strings(self) -> None:
        raw = [_ok_ohlc(_ts_seconds(0))]
        out = _run_handle("apex", raw)
        c = out["data"]["candles"][0]
        for field in ("time", "open", "high", "low", "close", "volume"):
            self.assertIsInstance(c[field], (int, float), f"{field} must be numeric")

    def test_all_required_fields_present(self) -> None:
        raw = [_ok_ohlc(_ts_seconds(0))]
        out = _run_handle("apex", raw)
        c = out["data"]["candles"][0]
        for field in ("time", "open", "high", "low", "close", "volume"):
            self.assertIn(field, c)

    # ---- Mix HL+apex style timestamps in the same batch ----

    def test_mixed_units_in_one_batch_normalized_correctly(self) -> None:
        # Simulate an upstream that returns seconds for some rows and ms for others.
        raw = [
            _ok_ohlc(_ts_seconds(0)),                        # seconds
            _ok_ohlc(_ts_ms(_ts_seconds(1))),                # ms
            _ok_ohlc(_ts_seconds(2)),                        # seconds
        ]
        out = _run_handle("apex", raw)
        cs = out["data"]["candles"]
        ts = [c["time"] for c in cs]
        self.assertEqual(ts, [_ts_seconds(0), _ts_seconds(1), _ts_seconds(2)])


if __name__ == "__main__":
    unittest.main()
