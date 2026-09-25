"""Phase C.1 tests for the Apex canonical get_tickers provider.

Offline-only: these tests never contact Apex and never touch trade execution.
They exercise the agent-owned provider cache / circuit-breaker / fallback
logic directly, then verify the canonical mapping via a mocked Apex agent.
"""

from __future__ import annotations

import importlib
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class FakeApexClientC1:
    def __init__(self) -> None:
        self.configV3 = {
            "contractConfig": {
                "perpetualContract": [
                    {"symbol": "BTC-USDT", "tickSize": "0.1", "stepSize": "0.001", "minOrderSize": "0.001"},
                ],
                "prelaunchContract": [],
                "stockContract": [],
            }
        }
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.calls = 0

    def set_default_account_type(self, account_type: str) -> None:
        return None

    def configs_v3(self) -> None:
        return None

    def ticker_v3(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        self.calls += 1
        row = self.rows.get(str(symbol or "").upper())
        return {"data": [row]} if row else {"data": []}


def _row(symbol: str, mark: float = 100.0, turnover: float = 1_000_000.0, volume: float = 10.0) -> Dict[str, Any]:
    return {
        "symbol": symbol.replace("-", ""),
        "markPrice": mark,
        "lastPrice": mark,
        "oraclePrice": mark + 1,
        "price24hPcnt": 0.12,
        "turnover24h": turnover,
        "volume24h": volume,
        "fundingRate": 0.0001,
        "openInterest": 500.0,
    }


class TestApexTickerProviderC1(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = importlib.import_module("plugins.trade.agents.x_apex_agent")

    def test_fresh_cache_avoids_another_fanout(self) -> None:
        provider = self.agent._ApexTickerProvider(ttl_seconds=60, max_concurrency=2)
        calls = {"n": 0}

        def fetch(sym: str) -> Optional[Dict[str, Any]]:
            calls["n"] += 1
            return _row(sym)

        first = provider.get_or_refresh("acct", "", ["BTC-USDT"], fetch_one_sdk=fetch)
        second = provider.get_or_refresh("acct", "", ["BTC-USDT"], fetch_one_sdk=fetch)

        self.assertEqual(calls["n"], 1)
        self.assertFalse(first["served_from_cache"])
        self.assertTrue(second["served_from_cache"])
        self.assertEqual(second["refresh_status"], "ok")

    def test_expired_cache_refreshes(self) -> None:
        provider = self.agent._ApexTickerProvider(ttl_seconds=0, max_concurrency=2)
        calls = {"n": 0}

        def fetch(sym: str) -> Optional[Dict[str, Any]]:
            calls["n"] += 1
            return _row(sym, mark=100.0 + calls["n"])

        first = provider.get_or_refresh("acct", "", ["BTC-USDT"], fetch_one_sdk=fetch)
        second = provider.get_or_refresh("acct", "", ["BTC-USDT"], fetch_one_sdk=fetch)

        self.assertEqual(calls["n"], 2)
        self.assertNotEqual(first["snapshot"]["BTC-USDT"]["markPrice"], second["snapshot"]["BTC-USDT"]["markPrice"])

    def test_simultaneous_callers_coalesce_into_one_refresh(self) -> None:
        provider = self.agent._ApexTickerProvider(ttl_seconds=60, max_concurrency=2)
        calls = {"n": 0}
        lock = threading.Lock()
        start = threading.Barrier(5)
        results = []

        def fetch(sym: str) -> Optional[Dict[str, Any]]:
            with lock:
                calls["n"] += 1
            time.sleep(0.05)
            return _row(sym)

        def worker() -> None:
            start.wait(timeout=2)
            out = provider.get_or_refresh("acct", "", ["BTC-USDT", "ETH-USDT"], fetch_one_sdk=fetch)
            results.append(out)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3)

        self.assertEqual(calls["n"], 2)  # one refresh over two symbols, not five refreshes
        self.assertEqual(len(results), 5)
        self.assertTrue(any(r["served_from_cache"] for r in results))

    def test_bounded_concurrency(self) -> None:
        provider = self.agent._ApexTickerProvider(ttl_seconds=0, max_concurrency=3)
        active = {"n": 0, "max": 0}
        lock = threading.Lock()

        def fetch(sym: str) -> Optional[Dict[str, Any]]:
            with lock:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
            time.sleep(0.03)
            with lock:
                active["n"] -= 1
            return _row(sym)

        provider.get_or_refresh("acct", "", [f"SYM{i}-USDT" for i in range(10)], fetch_one_sdk=fetch)
        self.assertLessEqual(active["max"], 3)

    def test_rate_limit_detection_and_cooldown_prevents_hammering(self) -> None:
        provider = self.agent._ApexTickerProvider(ttl_seconds=0, max_concurrency=2, circuit_cooldown_s=60)
        calls = {"n": 0}

        def blocked(sym: str) -> Optional[Dict[str, Any]]:
            calls["n"] += 1
            return {"code": 1, "msg": "sorry, you have been blocked"}

        first = provider.get_or_refresh("acct", "", ["BTC-USDT", "ETH-USDT"], fetch_one_sdk=blocked)
        second = provider.get_or_refresh("acct", "", ["BTC-USDT", "ETH-USDT"], fetch_one_sdk=blocked)

        self.assertEqual(first["refresh_status"], "rate_limited")
        self.assertEqual(second["refresh_status"], "circuit_open")
        self.assertEqual(calls["n"], 2)  # second call did not fan out again

    def test_fallback_source_when_sdk_fails(self) -> None:
        provider = self.agent._ApexTickerProvider(ttl_seconds=0, max_concurrency=2)
        sdk_calls = {"n": 0}
        http_calls = {"n": 0}

        def sdk(sym: str) -> Optional[Dict[str, Any]]:
            sdk_calls["n"] += 1
            return None

        def http(sym: str) -> Optional[Dict[str, Any]]:
            http_calls["n"] += 1
            return _row(sym, mark=123.0)

        out = provider.get_or_refresh("acct", "", ["BTC-USDT"], fetch_one_sdk=sdk, fetch_one_http=http)
        self.assertEqual(out["source"], "http_fallback")
        self.assertEqual(out["snapshot"]["BTC-USDT"]["markPrice"], 123.0)
        self.assertEqual(sdk_calls["n"], 1)
        self.assertEqual(http_calls["n"], 1)

    def test_partial_refresh_preserves_previous_successful_values(self) -> None:
        provider = self.agent._ApexTickerProvider(ttl_seconds=0, max_concurrency=2)

        def first(sym: str) -> Optional[Dict[str, Any]]:
            return _row(sym, mark=100.0 if sym.startswith("BTC") else 200.0)

        provider.get_or_refresh("acct", "", ["BTC-USDT", "ETH-USDT"], fetch_one_sdk=first)

        def second(sym: str) -> Optional[Dict[str, Any]]:
            if sym == "BTC-USDT":
                return _row(sym, mark=101.0)
            return None

        out = provider.get_or_refresh("acct", "", ["BTC-USDT", "ETH-USDT"], fetch_one_sdk=second)
        self.assertEqual(out["refresh_status"], "partial")
        self.assertEqual(out["snapshot"]["BTC-USDT"]["markPrice"], 101.0)
        self.assertEqual(out["snapshot"]["ETH-USDT"]["markPrice"], 200.0)
        self.assertIn("ETH-USDT", out["stale_symbols"])
        self.assertNotIn("ETH-USDT", out["failed_symbols"])

    def test_completely_unavailable_symbol_remains_unavailable(self) -> None:
        provider = self.agent._ApexTickerProvider(ttl_seconds=0, max_concurrency=1)
        out = provider.get_or_refresh("acct", "", ["UNKNOWN-USDT"], fetch_one_sdk=lambda sym: None)
        self.assertEqual(out["refresh_status"], "no_data")
        self.assertEqual(out["snapshot"], {})
        self.assertIn("UNKNOWN-USDT", out["failed_symbols"])


class TestApexCanonicalVolumeSemanticsC1(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = importlib.import_module("plugins.trade.agents.x_apex_agent")
        self.agent._APEX_TICKER_PROVIDER = self.agent._ApexTickerProvider()  # type: ignore[attr-defined]

    def test_turnover_is_quote_volume_and_base_volume_is_not_used_for_ranking(self) -> None:
        fake = FakeApexClientC1()
        fake.rows["BTC-USDT"] = _row("BTC-USDT", mark=100.0, turnover=999_999.0, volume=7.0)
        self.agent._resolve_credentials = lambda account: (  # type: ignore[assignment]
            {"account": account or "primary", "api_key": "stub", "secret": "stub", "passphrase": "stub"},
            None,
        )
        self.agent._client_for_credentials = lambda credentials: fake  # type: ignore[assignment]
        self.agent._apex_fetch_supported_markets = lambda client: [  # type: ignore[assignment]
            {"symbol": "BTC-USDT", "tickSize": "0.1", "stepSize": "0.001", "minOrderSize": "0.001"},
        ]

        resp = self.agent.execute({"operation": "get_tickers", "account": "primary", "symbols": ["BTC-USDT"]})
        self.assertTrue(resp.success, msg=resp.error)
        mp = resp.tickers_batch.tickers["BTC-USDT"]
        self.assertEqual(mp.turnover_24h, "999999.0")
        self.assertEqual(mp.volume_24h_quote, "999999.0")
        self.assertEqual(mp.volume_24h_base, "7.0")


if __name__ == "__main__":
    unittest.main()
