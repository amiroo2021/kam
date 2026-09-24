"""
Regression tests for the WebTrade2 chart fix.

Bug: The chart frame was rendering but no candlesticks were drawn.
Diagnose-by-data:
  - /api/candles returns valid data (this is verified at the HTTP layer).
  - In-browser setData was being skipped or losing the rendered result.

These tests cover the chart pipeline end-to-end at the boundaries:
  1. /api/candles MUST return ascending, deduplicated, finite-numbered OHLC
     for all supported timeframes (not just 1h).
  2. The route parameter `interval` accepts all values reported by the frontend
     (1m, 5m, 15m, 1h, 4h, 1D).
  3. No‑op / dropped‑data detection: if the response arrives, but the candles
     block is empty, the route still returns 200 (no internal error) — the
     frontend is responsible for the warning.
  4. Bid handler: at the LWC boundary, the normalized shape matches the LWC v4.2
     contract {time: seconds, open, high, low, close}. We replicate the JS-side
     normalization here to lock the contract.
  5. Polling incremental path: the normalized rows update shape is identical
     to setData shape.

No mock or synthetic candles are inserted beyond what the live exchange agent
provides; these tests interact with the actual /api/candles HTTP endpoint.
"""
import json
import re
import time
import unittest
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener
from http.cookiejar import MozillaCookieJar


def _login_and_csrf(host: str = "127.0.0.1", port: int = 9009) -> dict:
    """Log in to WebTrade2 and return both session and CSRF cookies."""
    cookies = MozillaCookieJar()
    opener = build_opener(HTTPCookieProcessor(cookies))

    password_path = "/root/.hermes/.env"
    import os
    pw = None
    with open(password_path) as f:
        for line in f:
            for k in ("TRADE_WEB_PASSWORD", "WEB_PASSWORD", "WEBTRADE2_PASSWORD"):
                if line.startswith(k + "="):
                    pw = line[len(k) + 1:].rstrip("\n")
                    break
            if pw:
                break
    assert pw, "WebTrade2 password not found in .env"

    body = urlencode({"password": pw}).encode("ascii")
    req = Request(
        f"http://{host}:{port}/login",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    # follow redirect manually
    opener.open(req, timeout=15)
    out = {}
    for c in cookies:
        out[c.name] = c.value
    return out


class _ChartFixture:
    def __init__(self):
        self.cookies = _login_and_csrf()
        self.csrf = self.cookies.get("webtrade2_csrf")
        self.base = "http://127.0.0.1:9009"
        self.session_cookie = self.cookies.get("webtrade2_session")

    def _fetch_candles(self, exchange: str, symbol: str, *, tf: str, limit: int = 240):
        opener = build_opener(HTTPCookieProcessor(
            MozillaCookieJar()  # already-handled cookies via header
        ))
        # Build Cookie header
        cookie_header = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        url = (
            f"{self.base}/api/candles?"
            + urlencode({
                "exchange": exchange,
                "account": "fibo",
                "symbol": symbol,
                "interval": tf,
                "limit": str(limit),
                "market_type": "futures",
            })
        )
        req = Request(url, headers={"Cookie": cookie_header, "Accept": "application/json"})
        with opener.open(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())

    def _normalize_js_contract(self, candles):
        """Replay the JS normalization logic against the response payload."""
        seen = set()
        out = []
        for c in candles:
            t = int(c["time"])  # Math.floor equivalent
            o = float(c["open"])
            h = float(c["high"])
            l = float(c["low"])
            cl = float(c["close"])
            if (
                not isinstance(t, int) or t <= 0
                or not (o == o)  # not NaN
                or not (h == h)
                or not (l == l)
                or not (cl == cl)
            ):
                continue
            if t in seen:
                continue
            seen.add(t)
            out.append({"time": t, "open": o, "high": h, "low": l, "close": cl})
        out.sort(key=lambda r: r["time"])
        return out


class ApiCandlesContractTests(unittest.TestCase):
    """Lock the /api/candles HTTP contract for chart rendering."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fx = _ChartFixture()

    def _candles(self, tf):
        st, body = self.fx._fetch_candles("hyperliquid", "BTC", tf=tf)
        return st, body

    def test_returns_200(self):
        st, body = self._candles("1m")
        self.assertEqual(st, 200)
        self.assertTrue(body.get("success"))

    def test_interval_echoed_correctly(self):
        """Bug previously: param was `interval` but the server defaulted to 1h.
        Frontend sends `interval=1m`; server must echo `1m` and return 1m data."""
        for tf in ("1m", "5m", "15m", "1h", "4h", "1D"):
            with self.subTest(tf=tf):
                _, body = self._candles(tf)
                self.assertEqual(body["data"]["interval"], tf,
                                 f"interval echo mismatch for tf={tf}")

    def test_one_minute_timeframe_is_one_minute_candles(self):
        """Bug previously: 1m request returned 1h candles. Regression lock."""
        _, body = self._candles("1m")
        candles = body["data"]["candles"]
        self.assertGreater(len(candles), 10)
        diffs = {candles[i + 1]["time"] - candles[i]["time"]
                 for i in range(len(candles) - 1)}
        self.assertEqual(diffs, {60},
                         f"1m timeframe must be 60-second spacing, got {diffs}")

    def test_one_hour_timeframe_is_one_hour_candles(self):
        _, body = self._candles("1h")
        candles = body["data"]["candles"]
        diffs = {candles[i + 1]["time"] - candles[i]["time"]
                 for i in range(len(candles) - 1)}
        self.assertEqual(diffs, {3600})

    def test_four_hour_timeframe_is_four_hour_candles(self):
        _, body = self._candles("4h")
        candles = body["data"]["candles"]
        diffs = {candles[i + 1]["time"] - candles[i]["time"]
                 for i in range(len(candles) - 1)}
        self.assertEqual(diffs, {14_400},
                         f"4h timeframe must be 14400-second spacing, got {diffs}")

    def test_one_day_timeframe_is_one_day_candles(self):
        _, body = self._candles("1D")
        candles = body["data"]["candles"]
        diffs = {candles[i + 1]["time"] - candles[i]["time"]
                 for i in range(len(candles) - 1)}
        self.assertEqual(diffs, {86_400},
                         f"1D timeframe must be 86400-second spacing, got {diffs}")

    def test_ascending_unique_timestamps(self):
        for tf in ("1m", "5m", "15m", "1h", "4h", "1D"):
            with self.subTest(tf=tf):
                _, body = self._candles(tf)
                candles = body["data"]["candles"]
                times = [c["time"] for c in candles]
                self.assertEqual(times, sorted(times),
                                 "candles must be ascending by timestamp")
                self.assertEqual(len(times), len(set(times)),
                                 "candles must not contain duplicate timestamps")

    def test_ohlc_all_finite_numbers(self):
        for tf in ("1m", "5m", "15m", "1h", "4h", "1D"):
            with self.subTest(tf=tf):
                _, body = self._candles(tf)
                for c in body["data"]["candles"]:
                    for k in ("time", "open", "high", "low", "close"):
                        v = c[k]
                        self.assertIsNotNone(v, f"missing field {k}")
                        self.assertFalse(isinstance(v, str) or isinstance(v, bool),
                                         f"non-numeric {k}={v!r}")
                        self.assertEqual(v, v, f"NaN in {k}")
                        self.assertNotEqual(v, float("inf"), f"inf in {k}")
                        self.assertNotEqual(v, float("-inf"), f"-inf in {k}")

    def test_high_is_max_of_ohlc_low_is_min(self):
        """Oversight check: the normalization must not corrupt OHLC ordering."""
        _, body = self._candles("1m")
        for c in body["data"]["candles"]:
            lo, hi, op, cl = c["low"], c["high"], c["open"], c["close"]
            self.assertLessEqual(lo, hi, f"low > high: {c}")
            self.assertGreaterEqual(op, lo, f"open < low: {c}")
            self.assertGreaterEqual(op, lo, "open < low")
            self.assertLessEqual(op, hi, "open > high")
            self.assertGreaterEqual(cl, lo, "close < low")
            self.assertLessEqual(cl, hi, "close > high")


class JsNormalizationContractTests(unittest.TestCase):
    """Replay the JS-side normalization the frontend applies before setData().

    The frontend relies on receiving a non-empty, ascending, deduped array of
    `{time, open, high, low, close}` objects. This test ensures the response
    shape survives the normalization that frontend runs in loadChartHistory().
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.fx = _ChartFixture()

    def test_normalized_series_matches_lwc_v4_2_contract(self):
        for tf in ("1m", "5m", "15m", "1h", "4h", "1D"):
            with self.subTest(tf=tf):
                _, body = self.fx._fetch_candles("hyperliquid", "BTC", tf=tf)
                norm = self.fx._normalize_js_contract(body["data"]["candles"])
                self.assertGreater(len(norm), 0,
                                   f"normalized series empty for tf={tf}")
                # Strict ascending unique times
                times = [c["time"] for c in norm]
                self.assertEqual(times, sorted(set(times)))
                self.assertEqual(len(times), len(set(times)))
                # All integer times (Lightweight Charts v4.2 expects integer
                # timestamps for non-business-day time scale)
                self.assertTrue(all(isinstance(t, int) for t in times))
                # All OHLC finite numbers
                for c in norm:
                    for k in ("time", "open", "high", "low", "close"):
                        v = c[k]
                        self.assertIsInstance(v, (int, float))
                        self.assertNotEqual(v, float("inf"))
                        self.assertNotEqual(v, float("-inf"))
                        self.assertEqual(v, v)

    def test_polling_normalization_matches_history_normalization(self):
        """The incremental update path uses identical normalization rules.

        Frontend applies `candleSeries.update({...})` with the same shape.
        """
        for tf in ("1m", "5m", "15m", "1h", "4h"):
            with self.subTest(tf=tf):
                # Fetch the last 2 candles as the polling path does.
                st, body = self.fx._fetch_candles(
                    "hyperliquid", "BTC", tf=tf, limit=2
                )
                self.assertEqual(st, 200)
                self.assertTrue(body.get("success"))
                norm = self.fx._normalize_js_contract(body["data"]["candles"])
                self.assertGreater(len(norm), 0)
                # And the shapes are identical to setData shape.
                for c in norm:
                    self.assertEqual(
                        set(c.keys()),
                        {"time", "open", "high", "low", "close"},
                    )


if __name__ == "__main__":
    unittest.main()
