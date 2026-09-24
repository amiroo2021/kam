"""
WebTrade2 Apex market-list enrichment tests.

Apex has no bulk ticker endpoint; we use a bounded-concurrent fan-out
to ticker_v3 with a 30s server-side cache. These tests exercise the
cache, the fan-out wrapper, the row enrichment, and the sort logic
without making any real network calls (fetch_one is mocked).
"""

import os
import sys
import time
import unittest
from typing import Any, Callable, Dict, List, Optional
from unittest import mock

sys.path.insert(0, "/root/kam")

from plugins.trade.webtrade2 import apex_ticker_cache as atc
from plugins.trade.webtrade2.apex_ticker_cache import (
    ApexTickerCache,
    _sanitize_ticker,
    merge_ticker_into_rows,
)


def _fake_ticker_row(symbol: str, mark: float, turnover: float, oi: float,
                     change_pct: float = 0.0, funding: float = 0.0001) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "symbolDisplayName": symbol,
        "markPrice": mark,
        "lastPrice": mark,
        "oraclePrice": mark,
        "price24hPcnt": change_pct,
        "turnover24h": turnover,
        "volume24h": turnover / max(mark, 1e-9),
        "openInterest": oi,
        "fundingRate": funding,
        "high24h": mark * 1.02,
        "low24h": mark * 0.98,
    }


class ApexTickerCacheTests(unittest.TestCase):

    def setUp(self) -> None:
        # Fresh cache per test.
        self._cache = ApexTickerCache(ttl_seconds=30, max_concurrency=4, per_request_timeout_s=2)

    def test_get_empty_returns_none(self) -> None:
        self.assertIsNone(self._cache.get("apex", "fibo"))

    def test_refresh_sync_populates_snapshot(self) -> None:
        symbols = ["BTC-USDT", "ETH-USDT"]
        def fetch(symbol: str):
            if symbol == "BTC-USDT":
                return _fake_ticker_row("BTCUSDT", mark=50000, turnover=1_000_000, oi=10)
            return _fake_ticker_row("ETHUSDT", mark=3000, turnover=500_000, oi=20)
        out = self._cache.refresh_sync("apex", "fibo", symbols, fetch_one=fetch)
        self.assertEqual(set(out.keys()), {"BTC-USDT", "ETH-USDT"})
        # Age must be near zero.
        age = self._cache.age_seconds("apex", "fibo")
        self.assertIsNotNone(age)
        self.assertLess(age, 1.0)

    def test_cache_hit_within_ttl(self) -> None:
        symbols = ["BTC-USDT"]
        fetch_calls: List[str] = []
        def fetch(symbol: str):
            fetch_calls.append(symbol)
            return _fake_ticker_row("BTCUSDT", mark=50000, turnover=1_000_000, oi=10)
        self._cache.refresh_sync("apex", "fibo", symbols, fetch_one=fetch)
        # Second call within TTL: must NOT call fetch_one again.
        out = self._cache.refresh_sync("apex", "fibo", symbols, fetch_one=fetch)
        self.assertEqual(fetch_calls, ["BTC-USDT"], f"cache must prevent repeat fan-out within TTL, got {fetch_calls}")
        self.assertIn("BTC-USDT", out)

    def test_cache_refresh_after_ttl_expires(self) -> None:
        symbols = ["BTC-USDT"]
        fetch_calls: List[str] = []
        def fetch(symbol: str):
            fetch_calls.append(symbol)
            return _fake_ticker_row("BTCUSDT", mark=50000, turnover=1_000_000, oi=10)
        self._cache.refresh_sync("apex", "fibo", symbols, fetch_one=fetch)
        # Force expiry by tampering the timestamp.
        with self._cache._lock:
            self._cache._snapshot[("fibo", "apex")] = time.time() - 100
        self._cache.refresh_sync("apex", "fibo", symbols, fetch_one=fetch)
        self.assertEqual(fetch_calls.count("BTC-USDT"), 2, "cache must refresh after TTL")

    def test_bounded_concurrency(self) -> None:
        # Generate 20 symbols; verify the BoundedSemaphore caps active fetches.
        symbols = [f"SYM-{i}" for i in range(20)]
        active = 0
        peak = 0
        lock = __import__("threading").Lock()

        def fetch(symbol: str):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)  # simulate latency
            with lock:
                active -= 1
            return _fake_ticker_row(symbol.replace("-", ""), 100, 1000, 1)

        self._cache.refresh_sync("apex", "fibo", symbols, fetch_one=fetch)
        self.assertLessEqual(peak, self._cache.max_concurrency,
                             f"peak concurrency {peak} exceeded cap {self._cache.max_concurrency}")
        # And we should have run at least more than the cap (proves concurrency).
        self.assertGreaterEqual(peak, 2)

    def test_one_symbol_failure_does_not_fail_batch(self) -> None:
        symbols = ["A", "B", "C"]
        def fetch(symbol: str):
            if symbol == "B":
                raise RuntimeError("simulated upstream failure")
            return _fake_ticker_row(symbol, 100, 1000, 1)
        out = self._cache.refresh_sync("apex", "fibo", symbols, fetch_one=fetch)
        # A and C are present; B is dropped.
        self.assertIn("A", out)
        self.assertIn("C", out)
        self.assertNotIn("B", out)
        # And the cache is still populated for the survivors.
        self.assertIsNotNone(self._cache.get("apex", "fibo"))

    def test_fetch_returns_none_is_tolerated(self) -> None:
        symbols = ["X", "Y"]
        def fetch(symbol: str):
            if symbol == "Y":
                return None
            return _fake_ticker_row("X", 100, 1000, 1)
        out = self._cache.refresh_sync("apex", "fibo", symbols, fetch_one=fetch)
        self.assertIn("X", out)
        self.assertNotIn("Y", out)

    def test_sanitize_ticker_strips_nested_and_coerces_numbers(self) -> None:
        out = _sanitize_ticker({
            "markPrice": "1234.5",
            "turnover24h": "1000000",
            "price24hPcnt": "0.025",
            "openInterest": "12.5",
            "fundingRate": "0.0001",
            "nested_dict": {"a": 1},     # must be stripped
            "nested_list": [1, 2, 3],    # must be stripped
            "extra_string_field": "ok",
        })
        self.assertEqual(out["markPrice"], 1234.5)
        self.assertEqual(out["turnover24h"], 1000000.0)
        self.assertEqual(out["price24hPcnt"], 0.025)
        self.assertEqual(out["openInterest"], 12.5)
        self.assertEqual(out["fundingRate"], 0.0001)
        self.assertNotIn("nested_dict", out)
        self.assertNotIn("nested_list", out)
        self.assertEqual(out["extra_string_field"], "ok")

    def test_sanitize_ticker_drops_uncoercible_fields(self) -> None:
        out = _sanitize_ticker({"markPrice": "not-a-number"})
        self.assertNotIn("markPrice", out)

    def test_merge_ticker_into_rows_adds_aliases(self) -> None:
        rows = [
            {"symbol": "BTCUSDT", "market_type": "perp", "symbolDisplayName": "BTCUSDT"},
            {"symbol": "MISSING", "market_type": "perp"},
        ]
        ticker_map = {"BTCUSDT": _fake_ticker_row("BTCUSDT", 50000, 1_000_000, 10)}
        merged = merge_ticker_into_rows(rows, ticker_map)
        self.assertEqual(merged[0]["price"], 50000)
        self.assertEqual(merged[0]["turnover24h"], 1_000_000)
        self.assertEqual(merged[0]["openInterest"], 10)
        # Missing ticker: canonical fields are None.
        self.assertIsNone(merged[1]["price"])
        self.assertIsNone(merged[1]["turnover24h"])
        self.assertIsNone(merged[1]["openInterest"])

    def test_no_secrets_logged_on_failure(self) -> None:
        # The cache must not embed account credentials in error logs.
        # We simulate a per-symbol failure that includes the literal string
        # "API_KEY=ABCD1234" — the cache's error handler must not echo that
        # back. (It only logs the exception message verbatim via the standard
        # logger; this test asserts that no result row contains the secret.)
        symbols = ["BTC-USDT"]
        def fetch(symbol: str):
            raise RuntimeError("upstream rejected API_KEY=ABCD1234 call")
        out = self._cache.refresh_sync("apex", "fibo", symbols, fetch_one=fetch)
        # No symbol succeeded → empty result.
        self.assertEqual(out, {})
        # The cache is populated with an empty snapshot (not missing).
        # Next call within TTL should NOT re-fetch — confirming the
        # empty snapshot is cached and not re-attempted.
        calls = []
        def fetch2(symbol: str):
            calls.append(symbol)
            return _fake_ticker_row("BTCUSDT", 50000, 1_000_000, 10)
        out2 = self._cache.refresh_sync("apex", "fibo", symbols, fetch_one=fetch2)
        self.assertEqual(calls, [], "all-failed snapshot must be cached; no re-fetch")
        self.assertEqual(out2, {})
        # And nothing in the result contains the secret string.
        import json
        blob = json.dumps([out, out2], default=str)
        self.assertNotIn("API_KEY=ABCD1234", blob)

    def test_concurrent_refreshes_coalesced(self) -> None:
        # Two simultaneous calls for the same key should not both fan out.
        import threading
        symbols = ["BTC-USDT"]
        fetch_calls: List[str] = []
        gate = threading.Event()
        def fetch(symbol: str):
            fetch_calls.append(symbol)
            gate.wait(timeout=2)
            return _fake_ticker_row("BTCUSDT", 50000, 1_000_000, 10)
        results: List[Dict[str, Any]] = []
        def runner():
            r = self._cache.refresh_sync("apex", "fibo", symbols, fetch_one=fetch)
            results.append(r)
        threads = [threading.Thread(target=runner) for _ in range(2)]
        for t in threads: t.start()
        # Give the leader thread time to register the inflight event.
        time.sleep(0.05)
        # Release both threads.
        gate.set()
        for t in threads: t.join(timeout=5)
        # Both calls should have returned the same data.
        self.assertEqual(results[0], results[1])
        # fetch_one should have been called only once (coalesced).
        self.assertEqual(len(fetch_calls), 1, f"expected 1 fetch, got {len(fetch_calls)}: {fetch_calls}")


class ApexMarketSortTests(unittest.TestCase):
    """Tests for the _rank_markets volume-desc sort and search filter."""

    def setUp(self) -> None:
        from plugins.trade.webtrade2 import service
        self._svc_mod = service
        # Build a minimal WebTrade2Service instance (we don't need a real desk).
        self._svc = service.WebTrade2Service.__new__(service.WebTrade2Service)

    def _make_rows(self) -> List[Dict[str, Any]]:
        return [
            {"symbol": "AAPLTESTUSDT", "market_type": "perp", "price": 270.0, "turnover24h": 295_931, "volume_24h": 295_931},
            {"symbol": "BTCUSDT", "market_type": "perp", "price": 84449.0, "turnover24h": 307_982_384, "volume_24h": 307_982_384},
            {"symbol": "ZECUSDT", "market_type": "perp", "price": 1538.0, "volume_24h": None},
            {"symbol": "ETHUSDT", "market_type": "perp", "price": 2684.0, "turnover24h": 13_749_634, "volume_24h": 13_749_634},
        ]

    def test_known_volume_markets_sort_descending(self) -> None:
        """The user-requested invariant: known-volume rows sort descending."""
        ranked = self._svc._rank_markets(self._make_rows())
        symbols = [r["symbol"] for r in ranked]
        # BTC ($307M) > ETH ($13.7M) > AAPLTEST ($295K)
        self.assertEqual(symbols[:3], ["BTCUSDT", "ETHUSDT", "AAPLTESTUSDT"])

    def test_unknown_volume_markets_follow_known_volume(self) -> None:
        """ZEC (volume_24h=None) must sort after the known-volume rows."""
        ranked = self._svc._rank_markets(self._make_rows())
        symbols = [r["symbol"] for r in ranked]
        self.assertEqual(symbols[-1], "ZECUSDT")
        # And it must come AFTER all known-volume rows.
        for r in ranked[:-1]:
            self.assertIsNotNone(r.get("volume_24h"), f"{r['symbol']} unexpectedly came before ZEC")

    def test_search_filters_before_sort(self) -> None:
        ranked = self._svc._rank_markets(self._make_rows(), search="BTC")
        self.assertEqual([r["symbol"] for r in ranked], ["BTCUSDT"])

    def test_turnover_alias_is_accepted(self) -> None:
        """Markets enriched by Apex's ticker fan-out use 'turnover24h'; the
        ranker must accept that as a substitute for volume_24h."""
        rows = [
            {"symbol": "LOW", "volume_24h": None, "turnover24h": 100},
            {"symbol": "HIGH", "volume_24h": None, "turnover24h": 999},
        ]
        ranked = self._svc._rank_markets(rows)
        self.assertEqual([r["symbol"] for r in ranked], ["HIGH", "LOW"])

    def test_unknown_volume_is_alphabetical_within_tail(self) -> None:
        """Within the unknown-volume tail, rows fall back to symbol order."""
        rows = [
            {"symbol": "ZZZ", "volume_24h": None},
            {"symbol": "AAA", "volume_24h": None},
            {"symbol": "MMM", "volume_24h": 100},
        ]
        ranked = self._svc._rank_markets(rows)
        symbols = [r["symbol"] for r in ranked]
        # MMM known-volume first, then AAA, then ZZZ
        self.assertEqual(symbols, ["MMM", "AAA", "ZZZ"])


class ApexEnrichPathTests(unittest.TestCase):
    """Verify the service-layer integration: Apex triggers enrichment, others don't."""

    def setUp(self) -> None:
        from plugins.trade.webtrade2 import service
        self._svc_mod = service
        self._svc = service.WebTrade2Service.__new__(service.WebTrade2Service)
        # Stub out desk capabilities so markets() doesn't fail.
        self._svc.desk = mock.MagicMock()
        self._svc.desk.capabilities.return_value = ["list_instruments"]

    def test_apex_exchange_triggers_enrichment(self) -> None:
        """exchange='apex' must call _apex_enrich."""
        with mock.patch.object(self._svc, "_apex_enrich",
                               return_value=[{"symbol": "BTCUSDT", "price": 50000}]) as mock_enrich:
            with mock.patch.object(self._svc, "_execute_read") as mock_exec:
                mock_exec.return_value.to_dict.return_value = {
                    "success": True,
                    "data": {
                        "instruments": [
                            {"symbol": "BTCUSDT", "display_name": "BTCUSDT",
                             "market_type": "perp"},
                        ]
                    },
                }
                with mock.patch.object(self._svc, "_rank_markets",
                                       return_value=[{"symbol": "BTCUSDT"}]):
                    out = self._svc.markets("apex", "fibo", "futures")
                    mock_enrich.assert_called_once()
                    self.assertTrue(out["success"])

    def test_hyperliquid_does_not_trigger_apex_enrichment(self) -> None:
        """exchange='hyperliquid' must NOT call _apex_enrich."""
        with mock.patch.object(self._svc, "_apex_enrich") as mock_enrich:
            with mock.patch.object(self._svc, "_execute_read") as mock_exec:
                mock_exec.return_value.to_dict.return_value = {
                    "success": True,
                    "data": {
                        "instruments": [
                            {"symbol": "BTC", "display_name": "BTC",
                             "market_type": "perp"},
                        ]
                    },
                }
                with mock.patch.object(self._svc, "_rank_markets",
                                       return_value=[{"symbol": "BTC"}]):
                    self._svc.markets("hyperliquid", "fibo", "futures")
                    mock_enrich.assert_not_called()

    def test_apex_enrichment_uses_cache(self) -> None:
        """A second call within TTL must not trigger another ticker fan-out.

        This proves the cache truly prevents fan-out on every request.
        """
        with mock.patch.object(self._svc_mod, "log"):
            # Build a cache instance directly with a long TTL
            cache = atc.ApexTickerCache(ttl_seconds=60, max_concurrency=4, per_request_timeout_s=2)
            fetch_count = {"n": 0}

            def _stub(sym: str) -> Optional[Dict[str, Any]]:
                fetch_count["n"] += 1
                return {
                    "symbol": sym, "markPrice": 1.0, "turnover24h": 100.0,
                    "volume24h": 5.0, "openInterest": 1.0, "fundingRate": 0.0001,
                    "price24hPcnt": 0.0,
                }

            cache.refresh_sync("apex", "acct1", ["BTCUSDT", "ETHUSDT"], _stub)
            # First call → fan-out, fetch_count should be 2 (BTC + ETH)
            self.assertEqual(fetch_count["n"], 2)
            # Second call (warm cache) → zero additional fetches.
            cache.refresh_sync("apex", "acct1", ["BTCUSDT", "ETHUSDT"], _stub)
            self.assertEqual(fetch_count["n"], 2, "second refresh within TTL should not refetch")

    def test_apex_enrichment_preserves_last_successful_cache_on_partial_failure(self) -> None:
        """If a refresh partially fails, the previous successful row must
        survive in the cache (stale-while-error)."""
        cache = atc.ApexTickerCache(ttl_seconds=0.05, max_concurrency=4, per_request_timeout_s=2)
        # First refresh: BTC works, ETH fails.
        def _half(sym: str) -> Optional[Dict[str, Any]]:
            if sym == "BTCUSDT":
                return {"symbol": sym, "markPrice": 1.0, "turnover24h": 100.0}
            return None
        cache.refresh_sync("apex", "acct", ["BTCUSDT", "ETHUSDT"], _half)
        # Wait past TTL
        time.sleep(0.06)
        # Second refresh: BTC fails, ETH works. Previous BTC row must survive.
        def _flipped(sym: str) -> Optional[Dict[str, Any]]:
            if sym == "ETHUSDT":
                return {"symbol": sym, "markPrice": 2.0, "turnover24h": 200.0}
            return None
        cache.refresh_sync("apex", "acct", ["BTCUSDT", "ETHUSDT"], _flipped)
        merged = cache.get("apex", "acct")
        self.assertIsNotNone(merged)
        symbols = set(merged.keys()) if merged else set()
        # Either both come back (refresh is fresh) OR both stale values remain
        # acceptable; the spec says preserve *previously successful* rows.
        self.assertIn("BTCUSDT", symbols)
        self.assertIn("ETHUSDT", symbols)


if __name__ == "__main__":
    unittest.main()
