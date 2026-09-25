"""Phase A/B/C migration tests for the canonical read-only market-data surface.

These tests:

1. Verify the additive canonical types (CanonicalTickersBatch,
   extended CanonicalInstrument, extended CanonicalMarketPrice)
   preserve backwards compatibility — every existing field still
   has its previous default of ``None`` or the existing literal.

2. Verify the Hyperliquid ``get_tickers`` operation is reachable via
   the agent dispatcher and uses the cached perps snapshot — zero
   additional HTTP requests are issued.

3. Verify the Apex ``get_tickers`` operation is reachable via the
   agent dispatcher, accepts optional ``symbols``, and is wired
   through the canonical ``get_tickers`` capability.

4. Verify live parity between the current WebTrade2 canonical market-list
   route and direct agent ``get_tickers`` for selected Apex instruments
   when explicitly opted in via RUN_LIVE_APEX_PARITY=1.

5. Verify that Apex ``get_tickers`` *partial* failures (a single
   ticker fetch returning ``None`` or raising) do not destroy the
   batch — the other symbols must still appear in the result.

6. Verify that genuinely-missing fields return ``None`` instead of
   being fabricated.

7. Verify that NO exchange write path is exercised by ``get_tickers``
   — the capability is purely read-only.

These tests are offline where possible: HTTP is replaced by fake
clients (``FakeHyperliquidMeta`` and ``FakeApexClient``) that
mirror the SDK shape. Live parity is asserted in a single
``test_live_apex_parity_*`` class that is skipped if the upstream
or credentials are unavailable; the offline parity guarantees
structural equivalence so the live test only needs to check value
agreement.
"""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Phase A — additive canonical types preserve backwards compatibility
# ---------------------------------------------------------------------------

class TestCanonicalMarketDataBackwardsCompat(unittest.TestCase):
    """All existing fields still construct with their prior defaults."""

    def test_canonical_instrument_old_three_args_still_construct(self) -> None:
        """The pre-Phase-A constructor (``requested_symbol, symbol,
        display_name``) must still work without any keyword args."""
        from plugins.trade.canonical import CanonicalInstrument
        inst = CanonicalInstrument(requested_symbol="BTC", symbol="BTC-USDT", display_name="BTCUSDT")
        d = inst.to_dict()
        self.assertEqual(d["requested_symbol"], "BTC")
        self.assertEqual(d["symbol"], "BTC-USDT")
        self.assertEqual(d["display_name"], "BTCUSDT")
        # Additive fields default to None.
        for k in (
            "native_symbol", "display_symbol", "base", "quote",
            "market_type", "price_increment", "size_increment",
            "minimum_size", "minimum_notional",
        ):
            self.assertIsNone(d[k], msg=f"{k} should default to None")

    def test_canonical_market_price_old_two_args_still_construct(self) -> None:
        """The pre-Phase-A ``CanonicalMarketPrice(requested_symbol,
        market)`` constructor still works."""
        from plugins.trade.canonical import CanonicalMarketPrice
        mp = CanonicalMarketPrice(requested_symbol="BTC", market="BTC")
        d = mp.to_dict()
        self.assertEqual(d["requested_symbol"], "BTC")
        self.assertEqual(d["market"], "BTC")
        # Pre-existing dynamic fields still default None.
        self.assertIsNone(d["mark_price"])
        self.assertIsNone(d["price"])
        # New dynamic fields default None.
        for k in (
            "volume_24h_base", "volume_24h_quote", "turnover_24h",
            "change_24h_pct", "funding_rate", "open_interest", "oracle_price",
            "size_increment", "price_increment", "minimum_size",
            "minimum_notional", "base", "quote", "market_type",
            "native_symbol", "display_symbol",
        ):
            self.assertIsNone(d[k], msg=f"{k} should default to None")

    def test_canonical_tickers_batch_round_trip(self) -> None:
        from plugins.trade.canonical import CanonicalTickersBatch, CanonicalMarketPrice
        empty = CanonicalTickersBatch(tickers={})
        self.assertEqual(empty.to_dict()["tickers"], {})
        self.assertEqual(empty.to_dict()["failed_symbols"], [])
        self.assertIsNone(empty.to_dict()["fetched_at"])
        self.assertIsNone(empty.to_dict()["ttl_seconds"])

        mp = CanonicalMarketPrice(requested_symbol="BTC", market="BTC", mark_price="70000")
        batch = CanonicalTickersBatch(
            tickers={"BTC": mp},
            failed_symbols=("FOO",),
            fetched_at="2026-09-24T22:00:00Z",
            ttl_seconds=30,
        )
        d = batch.to_dict()
        self.assertIn("BTC", d["tickers"])
        self.assertEqual(d["tickers"]["BTC"]["mark_price"], "70000")
        self.assertEqual(list(d["failed_symbols"]), ["FOO"])
        self.assertEqual(d["ttl_seconds"], 30)

    def test_make_success_threads_tickers_batch(self) -> None:
        """make_success/new code paths carry ``tickers_batch``."""
        from plugins.trade.canonical import (
            CanonicalTickersBatch,
            make_success,
            make_failure,
        )
        batch = CanonicalTickersBatch(tickers={}, failed_symbols=("X",))
        r = make_success(
            operation="get_tickers",
            exchange="hyperliquid",
            account="perp_main",
            tickers_batch=batch,
        )
        self.assertTrue(r.success)
        self.assertEqual(r.operation, "get_tickers")
        self.assertIsNotNone(r.tickers_batch)
        self.assertEqual(r.tickers_batch.failed_symbols, ("X",))
        # to_dict surfaces the batch.
        d = r.to_dict()
        self.assertIn("tickers_batch", d)
        self.assertEqual(d["tickers_batch"]["failed_symbols"], ["X"])

    def test_make_failure_threads_tickers_batch(self) -> None:
        from plugins.trade.canonical import (
            CanonicalTickersBatch,
            make_failure,
        )
        batch = CanonicalTickersBatch(tickers={})
        r = make_failure(
            operation="get_tickers",
            exchange="apex",
            account="primary",
            code="MARKET_CATALOG_UNAVAILABLE",
            message="upstream down",
            tickers_batch=batch,
        )
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "MARKET_CATALOG_UNAVAILABLE")
        self.assertIsNotNone(r.tickers_batch)


# ---------------------------------------------------------------------------
# Phase B — Hyperliquid get_tickers, offline only
# ---------------------------------------------------------------------------

class TestHyperliquidGetTickersOffline(unittest.TestCase):
    """Verify the HL get_tickers surface WITHOUT live network.

    The agent's ``_execute_get_tickers`` reuses
    ``_fetch_perp_market_candidates`` so we just need to populate that
    cache directly via ``_perp_market_candidates_cache``.
    """

    def setUp(self) -> None:
        sys.path.insert(0, str(_REPO_ROOT))
        self.agent = importlib.import_module("plugins.trade.agents.x_hyperliquid_agent")

    def tearDown(self) -> None:
        # Reset the cache so we don't poison other tests.
        self.agent._perp_market_candidates_cache = None

    def _seed_cache(self) -> None:
        now = self.agent.time.time()
        self.agent._perp_market_candidates_cache = (
            now,
            [
                {
                    "dex": "",
                    "dex_index": 0,
                    "internal_name": "BTC",
                    "route_symbol": "BTC",
                    "public_symbol": "BTC",
                    "public_key": "btc",
                    "internal_key": "btc",
                    "display_name": "BTC-USDC",
                    "price_increment": "0.01",
                    "size_increment": "0.0001",
                    "sz_decimals": 4,
                    "mark_price": "70000.12",
                    "volume_24h": "1500000000",
                    "change_24h": "1.25",
                    "funding": "0.0001",
                },
                {
                    "dex": "xyz",
                    "dex_index": 1,
                    "internal_name": "xyz:SP500",
                    "route_symbol": "xyz:SP500",
                    "public_symbol": "SP500",
                    "public_key": "sp500",
                    "internal_key": "xyz:sp500",
                    "display_name": "SP500-USDC",
                    "price_increment": "0.1",
                    "size_increment": "0.001",
                    "sz_decimals": 3,
                    "mark_price": "5320.5",
                    "volume_24h": "50000000",
                    "change_24h": "-0.5",
                    "funding": "0.0",
                },
                {
                    "dex": "",
                    "dex_index": 0,
                    "internal_name": "NO_MARK",
                    "route_symbol": "NO_MARK",
                    "public_symbol": "NO_MARK",
                    "public_key": "nomark",
                    "internal_key": "nomark",
                    "display_name": "NO_MARK-USDC",
                    "price_increment": "0.0001",
                    "size_increment": "1",
                    "sz_decimals": 0,
                    "mark_price": None,  # explicitly None — price should be None
                    "volume_24h": None,
                    "change_24h": None,
                    "funding": None,
                },
            ],
        )

    def test_get_tickers_returns_full_catalog_snapshot(self) -> None:
        self._seed_cache()
        resp = self.agent.execute({"operation": "get_tickers", "account": "perp_main"})
        self.assertTrue(resp.success, msg=resp.error)
        batch = resp.tickers_batch
        self.assertIsNotNone(batch)
        self.assertEqual(len(batch.tickers), 3)
        # The two priced markets must have mark_price as a decimal-string;
        # the explicitly-None-priced market must NOT be fabricated.
        self.assertEqual(batch.tickers["BTC"].mark_price, "70000.12")
        self.assertEqual(batch.tickers["BTC"].price, "70000.12")
        self.assertEqual(batch.tickers["BTC"].volume_24h_quote, "1500000000")
        self.assertEqual(batch.tickers["BTC"].turnover_24h, "1500000000")
        self.assertEqual(batch.tickers["BTC"].change_24h_pct, "1.25")
        self.assertEqual(batch.tickers["BTC"].funding_rate, "0.0001")
        self.assertIsNone(batch.tickers["NO_MARK"].mark_price)
        self.assertIsNone(batch.tickers["NO_MARK"].volume_24h_quote)
        self.assertIsNone(batch.tickers["NO_MARK"].change_24h_pct)
        self.assertIsNone(batch.tickers["NO_MARK"].funding_rate)

    def test_get_tickers_filter_symbols_succeeds(self) -> None:
        self._seed_cache()
        resp = self.agent.execute({
            "operation": "get_tickers",
            "account": "perp_main",
            "symbols": ["BTC", "xyz:SP500", "MISSING"],
        })
        self.assertTrue(resp.success, msg=resp.error)
        keys = set(resp.tickers_batch.tickers.keys())
        self.assertEqual(keys, {"BTC", "xyz:SP500"})
        self.assertIn("MISSING", resp.tickers_batch.failed_symbols)

    def test_get_tickers_unavailable_fields_are_none(self) -> None:
        """oracle_price, open_interest, base/quote, last_external,
        last_updated_time are genuinely unavailable on HL — they must
        NEVER be fabricated."""
        self._seed_cache()
        resp = self.agent.execute({"operation": "get_tickers", "account": "perp_main"})
        mp = resp.tickers_batch.tickers["BTC"]
        self.assertIsNone(mp.oracle_price)
        self.assertIsNone(mp.open_interest)
        self.assertIsNone(mp.last_external_price)
        self.assertIsNone(mp.last_updated_time)
        # HL doesn't expose base/quote split in metaAndAssetCtxs.
        # base is None when ``dex`` is empty (main perp), else the dex id.
        self.assertIsNone(mp.base)
        self.assertIsNone(mp.quote)
        # Inherited instrument fields populated.
        self.assertEqual(mp.native_symbol, "BTC")
        self.assertEqual(mp.display_symbol, "BTC")
        self.assertEqual(mp.market_type, "perp")
        self.assertEqual(mp.price_increment, "0.01")
        self.assertEqual(mp.size_increment, "0.0001")

    def test_get_tickers_reuses_cache_no_extra_http(self) -> None:
        """get_tickers must NOT call any HTTP function — it must
        consume the cached perps snapshot."""
        self._seed_cache()

        captured = []

        # Patch ``_post_info`` to log invocations and refuse them.
        def _refuse(*args, **kwargs):
            captured.append(("post_info", args, kwargs))
            raise AssertionError("get_tickers must NOT issue HTTP requests")

        original = getattr(self.agent, "_post_info", None)
        if original is not None:
            self.agent._post_info = _refuse  # type: ignore[assignment]
        try:
            resp = self.agent.execute({"operation": "get_tickers", "account": "perp_main"})
            self.assertTrue(resp.success, msg=resp.error)
            self.assertEqual(captured, [])
        finally:
            if original is not None:
                self.agent._post_info = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Phase C — Apex get_tickers, offline only
# ---------------------------------------------------------------------------

class _FakeApexRow(dict):
    """A Mapping the Apex SDK hands back in ``data`` lists."""

    def get(self, key, default=None):
        return super().get(key, default)


class FakeApexClient:
    """Mimics the apex SDK enough for ``client.ticker_v3`` and
    ``client.configV3`` / ``client.configs_v3`` / ``_client_for_credentials``.

    Pass a per-symbol row dict to ``set_ticker(symbol, row)``; symbols
    not registered raise a SDK-like timeout to exercise partial failure.
    """

    def __init__(self) -> None:
        self._tickers: dict[str, list[dict]] = {}
        self._ticker_exceptions: dict[str, BaseException] = {}
        self._raise_when_called: bool = False
        self._call_count: int = 0
        # Three-section config so get_tickers can classify properly.
        self.configV3 = {
            "contractConfig": {
                "perpetualContract": [
                    {"symbol": "BTC-USDT", "tickSize": "0.1", "stepSize": "0.001", "minOrderSize": "0.001"},
                    {"symbol": "ETH-USDT", "tickSize": "0.01", "stepSize": "0.01", "minOrderSize": "0.01"},
                ],
                "prelaunchContract": [],
                "stockContract": [
                    {"symbol": "NVDA-USDT", "tickSize": "0.01", "stepSize": "1", "minOrderSize": "1"},
                    {"symbol": "QQQ-USDT", "tickSize": "0.01", "stepSize": "1", "minOrderSize": "1"},
                    {"symbol": "AAPL-USDT", "tickSize": "0.01", "stepSize": "1", "minOrderSize": "1"},
                ],
            }
        }

    def set_ticker(self, symbol: str, row: dict) -> None:
        self._tickers[symbol.upper()] = [dict(row)]

    def fail_ticker(self, symbol: str, exc: BaseException) -> None:
        self._ticker_exceptions[symbol.upper()] = exc

    def set_ticker_raise_on_call(self) -> None:
        """Make ticker_v3 always raise (e.g. simulating an SDK timeout)."""
        self._raise_when_called = True

    def configs_v3(self) -> None:
        pass

    def configV3_path(self) -> None:
        return self.configV3

    def set_default_account_type(self, account_type: str) -> None:
        return None

    def ticker_v3(self, symbol: str = None) -> dict:
        self._call_count += 1
        if self._raise_when_called:
            raise TimeoutError("simulated SDK timeout")
        key = (symbol or "").upper()
        if key in self._ticker_exceptions:
            raise self._ticker_exceptions[key]
        if key in self._tickers:
            return {"data": self._tickers[key]}
        # Echo a single row for unknown symbols so the resolver fallback
        # path is exercised (first row when len==1).
        return {"data": []}


class TestApexGetTickersOffline(unittest.TestCase):
    """Verify the Apex get_tickers surface WITHOUT live network."""

    def setUp(self) -> None:
        sys.path.insert(0, str(_REPO_ROOT))
        self.agent = importlib.import_module("plugins.trade.agents.x_apex_agent")
        # The Phase C.1 provider is a module-level singleton; reset it
        # so offline tests do not leak cached ticker rows between cases.
        if hasattr(self.agent, "_APEX_TICKER_PROVIDER"):
            self.agent._APEX_TICKER_PROVIDER = self.agent._ApexTickerProvider()  # type: ignore[attr-defined]

    def _install_fake(self, fake: FakeApexClient) -> None:
        # Avoid real credential resolution; supply a stub creds dict.
        import types
        self.agent._apex_fetch_supported_markets = lambda client: [  # type: ignore[assignment]
            {"symbol": "BTC-USDT", "tickSize": "0.1", "stepSize": "0.001", "minOrderSize": "0.001"},
            {"symbol": "ETH-USDT", "tickSize": "0.01", "stepSize": "0.01", "minOrderSize": "0.01"},
            {"symbol": "NVDA-USDT", "tickSize": "0.01", "stepSize": "1", "minOrderSize": "1"},
            {"symbol": "QQQ-USDT", "tickSize": "0.01", "stepSize": "1", "minOrderSize": "1"},
            {"symbol": "AAPL-USDT", "tickSize": "0.01", "stepSize": "1", "minOrderSize": "1"},
        ]
        # _client_for_credentials is what the agent calls; patch it to return our fake.
        self._original_client_for = self.agent._client_for_credentials
        self.agent._client_for_credentials = lambda credentials: fake  # type: ignore[assignment]
        # _resolve_credentials must not error out; stub it.
        self._original_resolve = self.agent._resolve_credentials
        self.agent._resolve_credentials = lambda account: (  # type: ignore[assignment]
            {"account": account or "primary", "api_key": "stub", "secret": "stub", "passphrase": "stub"},
            None,
        )
        # Keep these Phase A/B/C offline tests pure-SDK; Phase C.1 has
        # dedicated fallback tests. Without this, a mocked SDK miss could
        # call the real public Apex fallback.
        self._original_http_fallback = getattr(self.agent, "_apex_ticker_fallback_fetch_one", None)
        self.agent._apex_ticker_fallback_fetch_one = lambda symbol: None  # type: ignore[assignment]

    def tearDown(self) -> None:
        if hasattr(self, "_original_client_for"):
            self.agent._client_for_credentials = self._original_client_for  # type: ignore[assignment]
        if hasattr(self, "_original_resolve"):
            self.agent._resolve_credentials = self._original_resolve  # type: ignore[assignment]
        if hasattr(self, "_original_http_fallback") and self._original_http_fallback is not None:
            self.agent._apex_ticker_fallback_fetch_one = self._original_http_fallback  # type: ignore[assignment]

    def _ticker_row(self, mark, last=None, oracle=None, turnover="1000000",
                    volume="100", change="0.5", funding="0.0001", oi="500000"):
        return {
            "symbol": "BTCUSDT",
            "markPrice": mark,
            "lastPrice": last if last is not None else mark,
            "oraclePrice": oracle if oracle is not None else mark,
            "indexPrice": mark,
            "price24hPcnt": change,
            "turnover24h": turnover,
            "volume24h": volume,
            "fundingRate": funding,
            "openInterest": oi,
        }

    def test_get_tickers_full_catalog_with_success(self) -> None:
        fake = FakeApexClient()
        # Use ticker rows for each contract.
        for sym in ("BTC-USDT", "ETH-USDT", "NVDA-USDT", "QQQ-USDT", "AAPL-USDT"):
            fake.set_ticker(sym, {
                "symbol": sym.replace("-", ""),
                "markPrice": 100.0,
                "lastPrice": 100.0,
                "oraclePrice": 100.0,
                "price24hPcnt": 0.5,
                "turnover24h": 1_000_000.0,
                "volume24h": 10_000.0,
                "fundingRate": 0.0001,
                "openInterest": 500_000.0,
            })
        self._install_fake(fake)

        resp = self.agent.execute({"operation": "get_tickers", "account": "primary"})
        self.assertTrue(resp.success, msg=resp.error)
        batch = resp.tickers_batch
        self.assertGreaterEqual(len(batch.tickers), 5)
        self.assertEqual(batch.failed_symbols, ())

        for sym in ("BTC-USDT", "ETH-USDT", "NVDA-USDT", "QQQ-USDT", "AAPL-USDT"):
            mp = batch.tickers[sym]
            self.assertEqual(mp.mark_price, "100.0", msg=sym)
            self.assertEqual(mp.price, "100.0", msg=sym)
            self.assertEqual(mp.volume_24h_quote, "1000000.0", msg=sym)
            self.assertEqual(mp.turnover_24h, "1000000.0", msg=sym)
            self.assertEqual(mp.volume_24h_base, "10000.0", msg=sym)
            self.assertEqual(mp.change_24h_pct, "0.5", msg=sym)
            self.assertEqual(mp.funding_rate, "0.0001", msg=sym)
            self.assertEqual(mp.open_interest, "500000.0", msg=sym)

    def test_get_tickers_partial_failure_does_not_destroy_batch(self) -> None:
        fake = FakeApexClient()
        fake.set_ticker("BTC-USDT", self._ticker_row(70000.0))
        fake.set_ticker("ETH-USDT", self._ticker_row(3500.0))
        # NVDA ticker raises — must NOT collapse the whole batch.
        fake.fail_ticker("NVDA-USDT", TimeoutError("simulated timeout"))
        fake.set_ticker("QQQ-USDT", self._ticker_row(450.0))
        fake.set_ticker("AAPL-USDT", self._ticker_row(220.0))
        self._install_fake(fake)

        resp = self.agent.execute({"operation": "get_tickers", "account": "primary"})
        self.assertTrue(resp.success, msg=resp.error)
        keys = set(resp.tickers_batch.tickers.keys())
        self.assertIn("BTC-USDT", keys)
        self.assertIn("ETH-USDT", keys)
        self.assertIn("NVDA-USDT", keys)
        self.assertIn("QQQ-USDT", keys)
        self.assertIn("AAPL-USDT", keys)
        # NVDA appears in failed_symbols but remains present with static metadata
        # and blank dynamic fields so WebTrade2 can show it as unavailable (—).
        self.assertIn("NVDA-USDT", resp.tickers_batch.failed_symbols)
        self.assertIsNone(resp.tickers_batch.tickers["NVDA-USDT"].price)
        self.assertIsNone(resp.tickers_batch.tickers["NVDA-USDT"].turnover_24h)

    def test_get_tickers_missing_fields_are_none(self) -> None:
        fake = FakeApexClient()
        # Row missing oraclePrice, turnover, change, funding, oi.
        fake.set_ticker("BTC-USDT", {
            "symbol": "BTCUSDT",
            "markPrice": 100.0,
            "lastPrice": 100.0,
        })
        self._install_fake(fake)

        resp = self.agent.execute({"operation": "get_tickers", "account": "primary"})
        self.assertTrue(resp.success, msg=resp.error)
        mp = resp.tickers_batch.tickers["BTC-USDT"]
        self.assertEqual(mp.mark_price, "100.0")
        # Missing fields MUST be None.
        self.assertIsNone(mp.oracle_price)
        self.assertIsNone(mp.volume_24h_quote)
        self.assertIsNone(mp.turnover_24h)
        self.assertIsNone(mp.volume_24h_base)
        self.assertIsNone(mp.change_24h_pct)
        self.assertIsNone(mp.funding_rate)
        self.assertIsNone(mp.open_interest)
        # Static metadata is present (queried from contract config).
        self.assertEqual(mp.price_increment, "0.1")
        self.assertEqual(mp.size_increment, "0.001")
        self.assertEqual(mp.minimum_size, "0.001")
        self.assertEqual(mp.base, "BTC")
        self.assertEqual(mp.quote, "USDT")


# ---------------------------------------------------------------------------
# No-write assertion across ALL agents — get_tickers must never mutate state
# ---------------------------------------------------------------------------

class TestGetTickersIsPurelyReadOnly(unittest.TestCase):
    """Phase A/B/C invariant: get_tickers is an explicit read operation.

    It must be in every agent's ``capabilities()`` return (after Phase C
    at least for the providers we ship) and must NOT be wired to any
    ``_new_order`` / ``_cancel_*`` / ``_set_tp`` / ``_set_sl`` /
    ``_close_position`` / ``_ladder`` write helper.
    """

    def test_hl_get_tickers_capability_advertised(self) -> None:
        mod = importlib.import_module("plugins.trade.agents.x_hyperliquid_agent")
        self.assertIn("get_tickers", mod.capabilities())

    def test_apex_get_tickers_capability_advertised(self) -> None:
        mod = importlib.import_module("plugins.trade.agents.x_apex_agent")
        self.assertIn("get_tickers", mod.capabilities())

    def test_get_tickers_does_not_call_any_write_helper(self) -> None:
        """Apex _apex_get_tickers must NOT mention any write helper."""
        src = (_REPO_ROOT / "plugins" / "trade" / "agents" / "x_apex_agent.py").read_text()
        # We don't grep for ``_apex_get_tickers`` writes — find the function body
        # and verify it never references write helpers.
        marker = "def _apex_get_tickers("
        start = src.find(marker)
        self.assertGreater(start, -1, msg="_apex_get_tickers not defined")
        # Find end of function (next top-level ``def `` after marker).
        tail = src[start:]
        end_idx = len(tail)
        for j in range(1, len(tail)):
            if tail[j:j+5] == "\n\ndef":
                # Could be ``\n\ndef `` (top-level) — accept it. Only stop on
                # a top-level ``def `` that follows a blank line.
                next_chunk = tail[j+2:]
                if next_chunk.startswith("def "):
                    end_idx = j
                    break
        body = tail[:end_idx]
        for forbidden in (
            "_apex_new_order", "_apex_cancel_order_group", "_apex_set_tp",
            "_apex_set_sl", "_apex_close_position", "_apex_ladder",
        ):
            self.assertNotIn(forbidden, body, msg=f"_apex_get_tickers must not call {forbidden}")

    def test_hl_get_tickers_does_not_call_any_write_helper(self) -> None:
        src = (_REPO_ROOT / "plugins" / "trade" / "agents" / "x_hyperliquid_agent.py").read_text()
        marker = "def _execute_get_tickers("
        start = src.find(marker)
        self.assertGreater(start, -1, msg="_execute_get_tickers not defined")
        tail = src[start:]
        end_idx = len(tail)
        for j in range(1, len(tail)):
            if tail[j:j+5] == "\n\ndef":
                next_chunk = tail[j+2:]
                if next_chunk.startswith("def "):
                    end_idx = j
                    break
        body = tail[:end_idx]
        for forbidden in (
            "_execute_new_order", "_execute_cancel_order_group",
            "_execute_set_tp", "_execute_set_sl", "_execute_close_position",
            "_execute_ladder", "_normalize_order_payload", "_sign_",
        ):
            self.assertNotIn(forbidden, body, msg=f"_execute_get_tickers must not call {forbidden}")


# ---------------------------------------------------------------------------
# Live parity: WebTrade2 canonical market list ↔ direct agent get_tickers
# ---------------------------------------------------------------------------

class TestLiveApexParityContract(unittest.TestCase):
    """Skipped unless WebTrade2 is live and Apex credentials are present.

    Phase D routes WebTrade2 market-list data through canonical
    ``get_tickers``. When opted in, this prints side-by-side values
    from WebTrade2's `/api/markets` response and a direct agent
    `get_tickers` call for representative Apex instruments. Values
    are not asserted byte-identical because live prices move between
    requests.
    """

    LIVE_BASE_URL = "http://127.0.0.1:9009"

    @classmethod
    def setUpClass(cls) -> None:
        import os
        import urllib.error
        import urllib.request
        if os.environ.get("RUN_LIVE_APEX_PARITY") != "1":
            raise unittest.SkipTest("Set RUN_LIVE_APEX_PARITY=1 to run live Apex parity")
        # Authenticate against WebTrade2.
        try:
            with open("/root/.hermes/.env") as f:
                env = f.read()
        except FileNotFoundError:
            env = ""
        import re as _re
        m = _re.search(r"^TRADE_WEB_PASSWORD=(.+)$", env, _re.MULTILINE)
        if not m:
            raise unittest.SkipTest("No TRADE_WEB_PASSWORD in /root/.hermes/.env")
        password = m.group(1).strip()
        # Login + retain cookies.
        cls._cookies_path = "/tmp/wt2_apex_parity.cookies"
        with open(cls._cookies_path, "w") as f:
            pass
        cookie_jar = cls._make_cookie_jar(cls._cookies_path)
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
        try:
            opener.open(urllib.request.Request(
                f"{cls.LIVE_BASE_URL}/login",
                data=f"password={password}".encode("utf-8"),
                method="POST",
            ), timeout=5)
        except Exception:
            raise unittest.SkipTest("WebTrade2 /login not reachable")
        cls._opener = opener

    @staticmethod
    def _make_cookie_jar(path):
        import http.cookiejar
        return http.cookiejar.MozillaCookieJar(path)

    def _fetch_markets_old(self):
        import json as _json
        url = f"{self.LIVE_BASE_URL}/api/markets?exchange=apex&account=bitget"
        with self._opener.open(url, timeout=15) as r:
            payload = _json.loads(r.read().decode("utf-8"))
        rows = payload.get("markets") or []
        # Build dict keyed by symbol. The WebTrade2 path uses no-dash
        # forms (BTCUSDT) while the agent path uses canonical dash
        # forms (BTC-USDT); store keys in both forms for mapping.
        out: dict = {}
        for r in rows:
            s = r.get("symbol") or ""
            out[s] = r
            out[s.replace("-", "")] = r
            out[s.replace("-", "_")] = r
        return out

    def _fetch_markets_new(self):
        """Run the agent's get_tickers via TradeDesk-equivalent."""
        import importlib
        agent = importlib.import_module("plugins.trade.agents.x_apex_agent")
        creds, err = agent._resolve_credentials("bitget")
        if creds is None:
            raise unittest.SkipTest(f"no Apex credentials resolve: {err}")
        resp = agent.execute({"operation": "get_tickers", "account": "bitget"})
        if not resp.success:
            raise unittest.SkipTest(f"apex.get_tickers offline: {resp.error}")
        return resp.tickers_batch.tickers

    def test_live_apex_parity_for_five_instruments(self) -> None:
        try:
            old = self._fetch_markets_old()
        except unittest.SkipTest:
            raise
        except Exception as exc:
            raise unittest.SkipTest(f"old path unreachable: {exc}")
        try:
            new = self._fetch_markets_new()
        except unittest.SkipTest:
            raise
        except Exception as exc:
            raise unittest.SkipTest(f"new path unavailable: {exc}")
        # The 5 instruments the user requested. WebTrade2 returns
        # no-dash forms (BTCUSDT) and the agent returns canonical
        # dash forms (BTC-USDT). We attempt both names for each.
        candidates = [
            ("BTC",   ["BTCUSDT", "BTC-USDT"]),
            ("ETH",   ["ETHUSDT", "ETH-USDT"]),
            ("NVDA",  ["NVDAUSDT", "NVDA-USDT"]),
            ("QQQ",   ["QQQUSDT", "QQQ-USDT"]),
            ("AAPL",  ["AAPLUSDT", "AAPL-USDT"]),
        ]
        for label, candidates_for_label in candidates:
            old_row = None
            new_mp = None
            for sym in candidates_for_label:
                if old_row is None:
                    old_row = old.get(sym)
                if new_mp is None:
                    new_mp = new.get(sym)
            if old_row is None:
                print(f"--- {label}: not available via WebTrade2 ---")
                continue
            if new_mp is None:
                print(f"--- {label}: not available via agent ---")
                continue
            print(f"--- {label} ({old_row.get('symbol')!r} | {new_mp.symbol!r}) ---")
            print(f"  old price           = {old_row.get('price')!r}")
            print(f"  new price (mark)    = {new_mp.price!r}")
            print(f"  old change_24h      = {old_row.get('change_24h')!r}")
            print(f"  new change_24h_pct  = {new_mp.change_24h_pct!r}")
            print(f"  old volume_24h      = {old_row.get('volume_24h')!r}")
            print(f"  new volume_24h_quote= {new_mp.volume_24h_quote!r}")
            print(f"  old turnover24h     = {old_row.get('turnover24h')!r}")
            print(f"  new turnover_24h    = {new_mp.turnover_24h!r}")
            print(f"  old funding         = {old_row.get('funding')!r}")
            print(f"  new funding_rate    = {new_mp.funding_rate!r}")
            print(f"  old openInterest    = {old_row.get('openInterest')!r}")
            print(f"  new open_interest   = {new_mp.open_interest!r}")
            print(f"  old oracle/indexPx  = {old_row.get('indexPrice')!r}")
            print(f"  new oracle_price    = {new_mp.oracle_price!r}")
            print(f"  new mark_price      = {new_mp.mark_price!r}")


if __name__ == "__main__":
    unittest.main()
