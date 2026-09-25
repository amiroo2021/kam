"""RED-first offline tests for canonical ``get_tickers`` on EdgeX (Batch 4b).

Coverage (per the approved brief):
  A. get_tickers advertised additively
  B. catalog -> canonical identity mapping
  C. price / oracle / index mapping
  D. value -> turnover_24h + volume_24h_quote
  E. size -> volume_24h_base; base NEVER used as ranking fallback
  F. priceChangePercent ratio -> percentage mapping
  G. funding + OI mapping
  H. static tick/step/min-size mapping
  I. missing ticker retains catalog row with None dynamic fields
  J. prior usable ticker retained as stale after refresh failure
  K. removed catalog symbol is NOT resurrected from stale cache
  L. TTL prevents a second fan-out
  M. concurrent refresh callers coalesce into one refresh
  N. concurrency never exceeds 8
  O. rate-limit detection opens circuit/cooldown
  P. circuit-open request does NOT launch another fan-out; serves stale
  Q. partial failure does not fail entire batch
  R. zero/invalid prices become None (not 0)
  S. canonical batch metadata accurately reports fresh/cached/stale/partial/rate-limited/circuit-open states
  T. WebTrade2 zero EdgeX-specific acquisition branches
  U. existing list_instruments / market_price / resolve behavior remains compatible
"""

from __future__ import annotations

import os
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

REPO = Path("/root/kam")
os.environ.setdefault("HERMES_HOME", "/root/.hermes")


def _imp(name: str):
    return __import__(f"plugins.trade.agents.{name}", fromlist=["*"])


# Sample EdgeX contractList entry (live-confirmed shape).
def _sample_contract(
    cid: str = "10000001",
    contract_name: str = "BTCUSDC",
    base_coin_id: str = "2",
    quote_coin_id: str = "1000",
    tick_size: str = "0.01",
    step_size: str = "0.0001",
    min_order_size: str = "0.0001",
) -> Dict[str, Any]:
    return {
        "contractId": cid,
        "contractName": contract_name,
        "baseCoinId": base_coin_id,
        "quoteCoinId": quote_coin_id,
        "tickSize": tick_size,
        "stepSize": step_size,
        "minOrderSize": min_order_size,
        "enableTrade": True,
    }


# Sample coinList entry (live-confirmed shape).
def _sample_coin(coin_id: str, name: str) -> Dict[str, Any]:
    return {
        "coinId": coin_id,
        "coinName": name,
        "stepSize": "0.000001",
        "showStepSize": "0.0001",
    }


# Sample ticker payload (live-confirmed shape).
def _sample_ticker(
    contract_id: str = "10000001",
    contract_name: str = "BTCUSDC",
    mark_price: str = "84676.8",
    oracle_price: str = "84680.0",
    index_price: str = "84681.0",
    last_price: str = "84676.0",
    value: str = "134589.4011",
    size: str = "849.37",
    open_interest: str = "52.13",
    funding_rate: str = "-0.00005202",
    price_change_percent: str = "-0.007057",
    high: str = "85223.4",
    low: str = "83301.4",
    open_v: str = "83900.0",
    close: str = "84676.0",
) -> Dict[str, Any]:
    return {
        "contractId": contract_id,
        "contractName": contract_name,
        "markPrice": mark_price,
        "oraclePrice": oracle_price,
        "indexPrice": index_price,
        "lastPrice": last_price,
        "value": value,
        "size": size,
        "openInterest": open_interest,
        "fundingRate": funding_rate,
        "priceChangePercent": price_change_percent,
        "high": high,
        "low": low,
        "open": open_v,
        "close": close,
        "trades": "5420",
        "startTime": "1790250300000",
        "endTime": "1790336700000",
    }


def _resp_to_dict(response: Any) -> Dict[str, Any]:
    if hasattr(response, "to_dict"):
        return response.to_dict()
    return response


def _tickers_dict(rd: Dict[str, Any]) -> Dict[str, Any]:
    if "tickers_batch" in rd and rd["tickers_batch"]:
        return rd["tickers_batch"].get("tickers", {})
    if "data" in rd and isinstance(rd["data"], dict):
        d = rd["data"]
        if "tickers" in d:
            return d["tickers"]
    return {}


def _batch_meta(rd: Dict[str, Any]) -> Dict[str, Any]:
    if "tickers_batch" in rd and rd["tickers_batch"]:
        return rd["tickers_batch"]
    if "data" in rd and isinstance(rd["data"], dict):
        return rd["data"]
    return {}


# Fixture: a small catalog of 3 contracts + matching coin list.
SAMPLE_CATALOG = {
    "contractList": [
        _sample_contract("10000001", "BTCUSDC", "2", "1000", "0.01", "0.0001", "0.0001"),
        _sample_contract("10000002", "ETHUSDC", "3", "1000", "0.01", "0.001", "0.001"),
        _sample_contract("10000003", "SOLUSDC", "4", "1000", "0.001", "0.01", "0.01"),
    ],
    "coinList": [
        _sample_coin("2", "BTC"),
        _sample_coin("3", "ETH"),
        _sample_coin("4", "SOL"),
        _sample_coin("1000", "USDC"),
    ],
}


def _metadata_full_stub() -> Dict[str, Any]:
    """Stub for EdgeX agent's _metadata_full() — returns our test catalog."""
    return SAMPLE_CATALOG


def _fetch_ticker_stub(ticker_by_cid: Dict[str, Dict[str, Any]]):
    """Returns a callable that mimics the EdgeX per-contract fetch.

    The agent's worker calls ``_edgex_fetch_ticker_raising(contract_dict)``
    so we must accept a dict and key off ``contractId``.
    """

    def _stub(contract):
        if isinstance(contract, dict):
            return ticker_by_cid.get(str(contract.get("contractId") or ""))
        return ticker_by_cid.get(str(contract))

    return _stub


def _fetch_ticker_raising_stub(exc_factory):
    """Returns a callable that always raises the given exception factory."""

    def _stub(contract_id: str):
        raise exc_factory(contract_id)

    return _stub


class TestEdgexGetTickersOffline(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.agent_module = _imp("x_edgex_agent")
        # Snapshot helpers are agent-private; tests access via execute().

    def setUp(self):
        # Reset the snapshot cache between tests so they don't leak state.
        cache_mod = getattr(self.agent_module, "_EDGEX_GET_TICKERS_CACHE", None)
        if isinstance(cache_mod, dict):
            cache_mod.clear()
        # Reset circuit / coalesce state.
        for attr in ("_EDGEX_GET_TICKERS_INFLIGHT", "_EDGEX_GET_TICKERS_CIRCUIT_OPEN_UNTIL",
                     "_EDGEX_GET_TICKERS_LAST_RATE_LIMIT_AT"):
            v = getattr(self.agent_module, attr, None)
            if isinstance(v, (int, float)):
                setattr(self.agent_module, attr, 0.0)
            elif isinstance(v, dict):
                v.clear()
            elif isinstance(v, threading.Event):
                v.clear()
        # Default: bypass rate-limit check during most tests.
        self._rate_patcher = mock.patch.object(
            self.agent_module, "_edgex_is_rate_limited", return_value=False
        )
        self._rate_patcher.start()
        # Bypass clock for deterministic TTL tests.
        self._now_patcher = mock.patch.object(
            self.agent_module, "_edgex_now", return_value=1_000_000.0
        )
        self._now_patcher.start()

    def tearDown(self):
        self._rate_patcher.stop()
        self._now_patcher.stop()

    # ------------------------------------------------------------------
    # A. Capability advertises get_tickers additively
    # ------------------------------------------------------------------

    def test_A_capabilities_advertise_get_tickers(self):
        caps = list(self.agent_module.capabilities())
        self.assertIn("get_tickers", caps)
        for must in ("list_instruments", "market_price", "new_order",
                     "ladder", "resolve_instrument", "balance",
                     "candles", "positions_orders", "positions_management",
                     "set_tp", "set_sl", "close_position"):
            self.assertIn(must, caps)

    # ------------------------------------------------------------------
    # B. Catalog -> canonical identity mapping
    # ------------------------------------------------------------------

    def test_B_catalog_identity_mapping(self):
        ticker_by_cid = {
            "10000001": _sample_ticker("10000001", "BTCUSDC"),
            "10000002": _sample_ticker("10000002", "ETHUSDC"),
            "10000003": _sample_ticker("10000003", "SOLUSDC"),
        }
        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=SAMPLE_CATALOG), \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                               side_effect=_fetch_ticker_stub(ticker_by_cid)), \
             mock.patch.object(self.agent_module, "_edgex_now", return_value=1_000_000.0):
            response = self.agent_module.execute({
                "operation": "get_tickers", "exchange": "edgex", "account": "default"
            })
        rd = _resp_to_dict(response)
        tickers = _tickers_dict(rd)
        self.assertEqual(len(tickers), 3)
        for sym in ("BTCUSDC", "ETHUSDC", "SOLUSDC"):
            self.assertIn(sym, tickers)
            m = tickers[sym]
            self.assertEqual(m["symbol"], sym)
            self.assertEqual(m["native_symbol"], sym)
            self.assertEqual(m["market_type"], "perp")
        self.assertEqual(tickers["BTCUSDC"]["base"], "BTC")
        self.assertEqual(tickers["BTCUSDC"]["quote"], "USDC")
        self.assertEqual(tickers["ETHUSDC"]["base"], "ETH")
        self.assertEqual(tickers["SOLUSDC"]["base"], "SOL")

    # ------------------------------------------------------------------
    # C. Price / oracle / index mapping
    # ------------------------------------------------------------------

    def test_C_price_oracle_index_mapping(self):
        ticker_by_cid = {"10000001": _sample_ticker("10000001", "BTCUSDC")}
        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=SAMPLE_CATALOG), \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                               side_effect=_fetch_ticker_stub(ticker_by_cid)):
            response = self.agent_module.execute({
                "operation": "get_tickers", "exchange": "edgex", "account": "default"
            })
        rd = _resp_to_dict(response)
        m = _tickers_dict(rd)["BTCUSDC"]
        self.assertEqual(m["mark_price"], "84676.8")
        # Trailing-zero stripping is consistent with canonical formatters
        # used by Apex/Lighter/Raydium/MEXC.
        self.assertEqual(m["oracle_price"], "84680")
        self.assertEqual(m["last_external_price"], "84681")
        # 'price' uses lastPrice (current trading value)
        self.assertEqual(m["price"], "84676")

    # ------------------------------------------------------------------
    # D. value -> turnover_24h + volume_24h_quote
    # ------------------------------------------------------------------

    def test_D_value_maps_to_turnover_and_quote_volume(self):
        ticker_by_cid = {"10000001": _sample_ticker("10000001", "BTCUSDC",
                                                       value="1000000")}
        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=SAMPLE_CATALOG), \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                               side_effect=_fetch_ticker_stub(ticker_by_cid)):
            response = self.agent_module.execute({
                "operation": "get_tickers", "exchange": "edgex", "account": "default"
            })
        rd = _resp_to_dict(response)
        m = _tickers_dict(rd)["BTCUSDC"]
        self.assertEqual(m["turnover_24h"], "1000000")
        self.assertEqual(m["volume_24h_quote"], "1000000")

    # ------------------------------------------------------------------
    # E. size -> volume_24h_base; base NEVER ranking fallback
    # ------------------------------------------------------------------

    def test_E_size_maps_to_base_volume_only(self):
        ticker_by_cid = {"10000001": _sample_ticker("10000001", "BTCUSDC",
                                                       value="0", size="500")}
        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=SAMPLE_CATALOG), \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                               side_effect=_fetch_ticker_stub(ticker_by_cid)):
            response = self.agent_module.execute({
                "operation": "get_tickers", "exchange": "edgex", "account": "default"
            })
        rd = _resp_to_dict(response)
        m = _tickers_dict(rd)["BTCUSDC"]
        self.assertEqual(m["volume_24h_base"], "500")
        # turnover / quote must be None when value is 0
        self.assertIsNone(m["turnover_24h"])
        self.assertIsNone(m["volume_24h_quote"])
        # The ranking helper (_volume_key) lives in webtrade2.service.
        # Importing the canonical helper to prove base is NOT used:
        try:
            from plugins.trade.webtrade2.service import _volume_key  # type: ignore
        except Exception:
            _volume_key = None
        if _volume_key is not None:
            rank = _volume_key(m)
            # rank tuple: (0, -vol, symbol) when turnover present,
            # (1, Decimal('0'), symbol) when not. volume_24h_base must
            # not appear in the ranking key.
            # The 2nd element should be 0 (no turnover), not -500.
            self.assertEqual(rank[1], 0)
            self.assertEqual(rank[0], 1)

    # ------------------------------------------------------------------
    # F. priceChangePercent ratio -> percentage mapping
    # ------------------------------------------------------------------

    def test_F_price_change_ratio_to_percent(self):
        # EdgeX returns priceChangePercent as ratio (-0.007057 = -0.7057%)
        ticker_by_cid = {"10000001": _sample_ticker("10000001", "BTCUSDC",
                                                       price_change_percent="-0.007057")}
        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=SAMPLE_CATALOG), \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                               side_effect=_fetch_ticker_stub(ticker_by_cid)):
            response = self.agent_module.execute({
                "operation": "get_tickers", "exchange": "edgex", "account": "default"
            })
        rd = _resp_to_dict(response)
        m = _tickers_dict(rd)["BTCUSDC"]
        # 0.007057 -> -0.7057 (signed)
        from decimal import Decimal
        d = Decimal(str(m["change_24h_pct"]))
        self.assertAlmostEqual(float(d), -0.7057, places=4)

    # ------------------------------------------------------------------
    # G. funding + OI mapping
    # ------------------------------------------------------------------

    def test_G_funding_and_oi_mapping(self):
        ticker_by_cid = {"10000001": _sample_ticker("10000001", "BTCUSDC",
                                                       funding_rate="-0.00005202",
                                                       open_interest="52.13")}
        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=SAMPLE_CATALOG), \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                               side_effect=_fetch_ticker_stub(ticker_by_cid)):
            response = self.agent_module.execute({
                "operation": "get_tickers", "exchange": "edgex", "account": "default"
            })
        rd = _resp_to_dict(response)
        m = _tickers_dict(rd)["BTCUSDC"]
        self.assertEqual(m["funding_rate"], "-0.00005202")
        self.assertEqual(m["open_interest"], "52.13")

    # ------------------------------------------------------------------
    # H. static tick / step / min-size mapping
    # ------------------------------------------------------------------

    def test_H_static_tick_step_min_size(self):
        ticker_by_cid = {
            "10000001": _sample_ticker("10000001", "BTCUSDC",
                                          mark_price="84676.8"),
        }
        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=SAMPLE_CATALOG), \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                               side_effect=_fetch_ticker_stub(ticker_by_cid)):
            response = self.agent_module.execute({
                "operation": "get_tickers", "exchange": "edgex", "account": "default"
            })
        rd = _resp_to_dict(response)
        m = _tickers_dict(rd)["BTCUSDC"]
        self.assertEqual(m["price_increment"], "0.01")
        self.assertEqual(m["size_increment"], "0.0001")
        self.assertEqual(m["minimum_size"], "0.0001")

    # ------------------------------------------------------------------
    # I. Missing ticker -> catalog row preserved, dynamic fields None
    # ------------------------------------------------------------------

    def test_I_missing_ticker_preserves_catalog_row(self):
        # No ticker rows at all -> catalog row still present, dynamic None.
        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=SAMPLE_CATALOG), \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                               side_effect=_fetch_ticker_stub({})):
            response = self.agent_module.execute({
                "operation": "get_tickers", "exchange": "edgex", "account": "default"
            })
        rd = _resp_to_dict(response)
        tickers = _tickers_dict(rd)
        self.assertEqual(len(tickers), 3)
        m = tickers["BTCUSDC"]
        self.assertEqual(m["symbol"], "BTCUSDC")
        self.assertIsNone(m["mark_price"])
        self.assertIsNone(m["turnover_24h"])
        self.assertIsNone(m["funding_rate"])

    # ------------------------------------------------------------------
    # J. Prior usable ticker retained as stale after refresh failure
    # ------------------------------------------------------------------

    def test_J_stale_retained_on_failure(self):
        # First refresh succeeds -> cache populated.
        ticker_by_cid = {"10000001": _sample_ticker("10000001", "BTCUSDC",
                                                       mark_price="84676.8")}
        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=SAMPLE_CATALOG), \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                               side_effect=_fetch_ticker_stub(ticker_by_cid)):
            response1 = self.agent_module.execute({
                "operation": "get_tickers", "exchange": "edgex", "account": "default"
            })
        rd1 = _resp_to_dict(response1)
        # Advance time past TTL so next refresh is forced.
        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=SAMPLE_CATALOG), \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                               side_effect=Exception("transient upstream")), \
             mock.patch.object(self.agent_module, "_edgex_now",
                               return_value=1_000_000.0 + 60.0):
            response2 = self.agent_module.execute({
                "operation": "get_tickers", "exchange": "edgex", "account": "default"
            })
        rd2 = _resp_to_dict(response2)
        meta = _batch_meta(rd2)
        # On partial failure, prior usable value must be retained.
        # We assert the stale retention semantics via the canonical batch.
        self.assertIn("stale_symbols", meta)
        self.assertIn("BTCUSDC", list(meta.get("stale_symbols") or []))
        # Tickers dict should still contain the prior mark_price for BTCUSDC.
        tickers2 = _tickers_dict(rd2)
        self.assertEqual(tickers2["BTCUSDC"]["mark_price"], "84676.8")

    # ------------------------------------------------------------------
    # K. Removed catalog symbol is NOT resurrected from stale cache
    # ------------------------------------------------------------------

    def test_K_removed_catalog_symbol_not_resurrected(self):
        # First refresh with full catalog.
        ticker_by_cid = {"10000001": _sample_ticker("10000001", "BTCUSDC")}
        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=SAMPLE_CATALOG), \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                               side_effect=_fetch_ticker_stub(ticker_by_cid)):
            self.agent_module.execute({
                "operation": "get_tickers", "exchange": "edgex", "account": "default"
            })
        # Now refresh with BTCUSDC removed from catalog.
        reduced_catalog = {
            "contractList": [
                _sample_contract("10000002", "ETHUSDC", "3", "1000", "0.01", "0.001", "0.001"),
                _sample_contract("10000003", "SOLUSDC", "4", "1000", "0.001", "0.01", "0.01"),
            ],
            "coinList": SAMPLE_CATALOG["coinList"],
        }
        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=reduced_catalog), \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                               side_effect=_fetch_ticker_stub({})), \
             mock.patch.object(self.agent_module, "_edgex_now",
                               return_value=1_000_000.0 + 60.0):
            response = self.agent_module.execute({
                "operation": "get_tickers", "exchange": "edgex", "account": "default"
            })
        rd = _resp_to_dict(response)
        tickers = _tickers_dict(rd)
        self.assertNotIn("BTCUSDC", tickers)
        self.assertIn("ETHUSDC", tickers)
        self.assertIn("SOLUSDC", tickers)

    # ------------------------------------------------------------------
    # L. TTL prevents a second fan-out
    # ------------------------------------------------------------------

    def test_L_TTL_prevents_second_fan_out(self):
        ticker_by_cid = {"10000001": _sample_ticker("10000001", "BTCUSDC")}
        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=SAMPLE_CATALOG) as meta_mock, \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                               side_effect=_fetch_ticker_stub(ticker_by_cid)) as fetch_mock:
            # First call.
            r1 = self.agent_module.execute({
                "operation": "get_tickers", "exchange": "edgex", "account": "default"
            })
            self.assertTrue(_resp_to_dict(r1).get("success"))
            # Second call inside TTL should serve from cache.
            r2 = self.agent_module.execute({
                "operation": "get_tickers", "exchange": "edgex", "account": "default"
            })
            rd2 = _resp_to_dict(r2)
            self.assertTrue(rd2.get("success"))
            meta2 = _batch_meta(rd2)
            self.assertTrue(meta2.get("served_from_cache"))
        # The fetch_ticker helper is the per-symbol fan-out call site.
        # It should have been called exactly once (first refresh) for
        # the 3 catalog rows = 3 calls.
        self.assertEqual(fetch_mock.call_count, 3)

    # ------------------------------------------------------------------
    # M. Concurrent callers coalesce into ONE refresh
    # ------------------------------------------------------------------

    def test_M_concurrent_callers_coalesce(self):
        ticker_by_cid = {"10000001": _sample_ticker("10000001", "BTCUSDC")}
        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=SAMPLE_CATALOG), \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                               side_effect=_fetch_ticker_stub(ticker_by_cid)) as fetch_mock:
            results = []
            barrier = threading.Barrier(5)

            def worker():
                barrier.wait()
                r = self.agent_module.execute({
                    "operation": "get_tickers", "exchange": "edgex", "account": "default"
                })
                results.append(r)

            threads = [threading.Thread(target=worker) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        # All 5 callers succeed.
        for r in results:
            self.assertTrue(_resp_to_dict(r).get("success"))
        # All 5 callers share ONE refresh. SAMPLE_CATALOG has 3 contracts,
        # so a single refresh fans out 3 times (one per contract). Subsequent
        # callers hit the warm-cache path and trigger zero additional fetches.
        # Total fetch count == number-of-contracts-in-catalog == 3.
        self.assertEqual(fetch_mock.call_count, len(SAMPLE_CATALOG["contractList"]))

    # ------------------------------------------------------------------
    # N. Concurrency never exceeds 8
    # ------------------------------------------------------------------

    def test_N_concurrency_capped_at_8(self):
        # Build a 24-contract catalog.
        big_catalog = {"contractList": [], "coinList": SAMPLE_CATALOG["coinList"]}
        for i in range(24):
            cid = str(20000000 + i)
            big_catalog["contractList"].append(
                _sample_contract(cid, f"SYM{i}USDC", "2", "1000",
                                  "0.01", "0.01", "0.01"))
        in_flight_peak = [0]
        in_flight_now = [0]
        lock = threading.Lock()

        def slow_fetch(contract_id: str) -> Optional[Dict[str, Any]]:
            with lock:
                in_flight_now[0] += 1
                in_flight_peak[0] = max(in_flight_peak[0], in_flight_now[0])
            time.sleep(0.05)
            with lock:
                in_flight_now[0] -= 1
            return _sample_ticker(contract_id, f"SYM{contract_id}USDC")

        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=big_catalog), \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                               side_effect=slow_fetch):
            response = self.agent_module.execute({
                "operation": "get_tickers", "exchange": "edgex", "account": "default"
            })
        self.assertTrue(_resp_to_dict(response).get("success"))
        self.assertLessEqual(in_flight_peak[0], 8,
                             f"observed {in_flight_peak[0]} concurrent fan-outs")

    # ------------------------------------------------------------------
    # O. Rate-limit detection opens circuit/cooldown
    # ------------------------------------------------------------------

    def test_O_rate_limit_opens_circuit(self):
        rl_exc = Exception("HTTP 429 rate limit exceeded")
        ticker_by_cid = {"10000001": _sample_ticker("10000001", "BTCUSDC")}
        # First, force a rate-limit hit during fetch.
        def fetch_with_first_rl(contract):
            # ``contract`` may be either a dict (production path) or a string
            # (legacy compat); support both for robustness.
            if isinstance(contract, dict):
                cid = str(contract.get("contractId") or "")
            else:
                cid = str(contract)
            if cid == "10000001":
                raise rl_exc
            return _sample_ticker(cid, f"SYM{cid}")

        # Stop the default "no rate limit" patcher so detection works.
        self._rate_patcher.stop()
        try:
            with mock.patch.object(self.agent_module, "_metadata_full",
                                   return_value=SAMPLE_CATALOG), \
                 mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                                   side_effect=fetch_with_first_rl):
                response1 = self.agent_module.execute({
                    "operation": "get_tickers", "exchange": "edgex", "account": "default"
                })
                rd1 = _resp_to_dict(response1)
                meta1 = _batch_meta(rd1)
                # Circuit must be open now.
                circuit_until = getattr(self.agent_module, "_EDGEX_GET_TICKERS_CIRCUIT_OPEN_UNTIL", 0.0)
                self.assertGreater(circuit_until, 1_000_000.0,
                                   "circuit must be open after rate-limit")
                # Refresh status must report rate_limited.
                self.assertEqual(meta1.get("refresh_status"), "rate_limited")
        finally:
            self._rate_patcher.start()

    # ------------------------------------------------------------------
    # P. Circuit-open request serves stale, no new fan-out
    # ------------------------------------------------------------------

    def test_P_circuit_open_serves_stale_without_fan_out(self):
        ticker_by_cid = {"10000001": _sample_ticker("10000001", "BTCUSDC",
                                                       mark_price="84676.8")}
        # First, populate the cache by a normal refresh.
        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=SAMPLE_CATALOG), \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                               side_effect=_fetch_ticker_stub(ticker_by_cid)):
            self.agent_module.execute({
                "operation": "get_tickers", "exchange": "edgex", "account": "default"
            })
        # Force circuit open (until far in the future).
        with mock.patch.object(self.agent_module, "_EDGEX_GET_TICKERS_CIRCUIT_OPEN_UNTIL",
                               9_999_999_999.0):
            with mock.patch.object(self.agent_module, "_metadata_full",
                                   return_value=SAMPLE_CATALOG), \
                 mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                                   side_effect=AssertionError(
                                       "circuit-open must NOT trigger fan-out")) as fetch_mock:
                response = self.agent_module.execute({
                    "operation": "get_tickers", "exchange": "edgex", "account": "default"
                })
                rd = _resp_to_dict(response)
                self.assertTrue(rd.get("success"))
                meta = _batch_meta(rd)
                self.assertEqual(meta.get("refresh_status"), "circuit_open")
                # Stale retained values served.
                tickers = _tickers_dict(rd)
                self.assertEqual(tickers["BTCUSDC"]["mark_price"], "84676.8")
                self.assertTrue(meta.get("served_from_cache"))
        self.assertEqual(fetch_mock.call_count, 0)

    # ------------------------------------------------------------------
    # Q. Partial failure does not fail the entire batch
    # ------------------------------------------------------------------

    def test_Q_partial_failure_preserves_remaining_rows(self):
        def partial(contract):
            # Agent passes a contract dict.
            if isinstance(contract, dict):
                cid = str(contract.get("contractId") or "")
            else:
                cid = str(contract)
            if cid == "10000002":  # ETH fails
                raise Exception("HTTP 503 service unavailable")
            return _sample_ticker(cid, "BTCUSDC" if cid == "10000001" else "SOLUSDC")

        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=SAMPLE_CATALOG), \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                               side_effect=partial):
            response = self.agent_module.execute({
                "operation": "get_tickers", "exchange": "edgex", "account": "default"
            })
        rd = _resp_to_dict(response)
        self.assertTrue(rd.get("success"))
        meta = _batch_meta(rd)
        # BTCUSDC and SOLUSDC succeeded; ETH failed.
        failed = set(meta.get("failed_symbols") or [])
        self.assertIn("ETHUSDC", failed)
        self.assertNotIn("BTCUSDC", failed)
        self.assertNotIn("SOLUSDC", failed)
        # ETH row still in tickers (from catalog) with dynamic None.
        tickers = _tickers_dict(rd)
        self.assertIn("ETHUSDC", tickers)
        self.assertIsNone(tickers["ETHUSDC"]["mark_price"])

    # ------------------------------------------------------------------
    # R. Zero / invalid prices become None
    # ------------------------------------------------------------------

    def test_R_zero_invalid_prices_become_none(self):
        ticker_by_cid = {"10000001": _sample_ticker("10000001", "BTCUSDC",
                                                       mark_price="0",
                                                       oracle_price="0",
                                                       index_price="0",
                                                       last_price="",
                                                       value="0",
                                                       size="0",
                                                       open_interest="0",
                                                       funding_rate="0",
                                                       price_change_percent="0")}
        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=SAMPLE_CATALOG), \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                               side_effect=_fetch_ticker_stub(ticker_by_cid)):
            response = self.agent_module.execute({
                "operation": "get_tickers", "exchange": "edgex", "account": "default"
            })
        rd = _resp_to_dict(response)
        m = _tickers_dict(rd)["BTCUSDC"]
        for k in ("mark_price", "price", "oracle_price", "last_external_price",
                  "turnover_24h", "volume_24h_quote", "volume_24h_base",
                  "open_interest", "funding_rate", "change_24h_pct"):
            self.assertIsNone(m[k], f"{k} should be None for zero/invalid value, got {m[k]}")

    # ------------------------------------------------------------------
    # S. Canonical batch metadata states
    # ------------------------------------------------------------------

    def test_S_metadata_fresh_state(self):
        ticker_by_cid = {"10000001": _sample_ticker("10000001", "BTCUSDC")}
        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=SAMPLE_CATALOG), \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker_raising",
                               side_effect=_fetch_ticker_stub(ticker_by_cid)):
            response = self.agent_module.execute({
                "operation": "get_tickers", "exchange": "edgex", "account": "default"
            })
        rd = _resp_to_dict(response)
        meta = _batch_meta(rd)
        self.assertEqual(meta.get("source"), "edgex_quote_getTicker_fanout")
        self.assertEqual(meta.get("refresh_status"), "ok")
        self.assertIs(meta.get("served_from_cache"), False)
        self.assertEqual(meta.get("ttl_seconds"), 30)

    # ------------------------------------------------------------------
    # T. WebTrade2 zero EdgeX-specific acquisition branches
    # ------------------------------------------------------------------

    def test_T_webtrade2_zero_edgex_acquisition_branches(self):
        webtrade2 = REPO / "plugins/trade/webtrade2"
        if not webtrade2.exists():
            self.skipTest("no webtrade2 plugin")
        bad_substrings = (
            "/api/v2/public/quote/getTicker",
            "/api/v2/public/meta/getMetaData",
            "edgex_quote_getTicker_fanout",
        )
        offenders = []
        for p in webtrade2.rglob("*.py"):
            txt = p.read_text()
            for sub in bad_substrings:
                if sub in txt:
                    offenders.append(f"{p}: {sub}")
        self.assertEqual(offenders, [],
                         f"WebTrade2 must not contain EdgeX-specific acquisition "
                         f"branches; offenders: {offenders}")

    def test_T2_webtrade2_source_grep_edgex(self):
        webtrade2 = REPO / "plugins/trade/webtrade2"
        if not webtrade2.exists():
            self.skipTest("no webtrade2 plugin")
        import re as _re
        offenders = []
        for p in webtrade2.rglob("*.py"):
            txt = p.read_text()
            for pat in (r"exchange\s*==\s*['\"]edgex['\"]",
                        r"exchange\.lower\(\)\s*==\s*['\"]edgex['\"]"):
                for m in _re.finditer(pat, txt):
                    offenders.append(f"{p}: {pat!r}: {m.group(0)}")
        self.assertEqual(offenders, [],
                         f"WebTrade2 must not have EdgeX-specific market-data "
                         f"branches; offenders: {offenders}")

    # ------------------------------------------------------------------
    # U. list_instruments / market_price / resolve behavior compatible
    # ------------------------------------------------------------------

    def test_U_list_instruments_path_untouched(self):
        with mock.patch.object(self.agent_module, "_metadata_full",
                               return_value=SAMPLE_CATALOG):
            response = self.agent_module.execute({
                "operation": "list_instruments", "exchange": "edgex", "account": "default"
            })
        rd = _resp_to_dict(response)
        self.assertTrue(rd.get("success"), rd)

    def test_U2_market_price_path_untouched(self):
        with mock.patch.object(self.agent_module, "_resolve_contract",
                               return_value=("10000001", "BTCUSDC")), \
             mock.patch.object(self.agent_module, "_edgex_fetch_ticker",
                               return_value=_sample_ticker("10000001", "BTCUSDC")):
            response = self.agent_module.execute({
                "operation": "market_price", "exchange": "edgex",
                "account": "default", "symbol": "BTC"
            })
        rd = _resp_to_dict(response)
        self.assertTrue(rd.get("success"), rd)

    def test_U3_resolve_path_untouched(self):
        with mock.patch.object(self.agent_module, "_resolve_contract",
                               return_value=("10000001", "BTCUSDC")):
            response = self.agent_module.execute({
                "operation": "resolve_instrument", "exchange": "edgex",
                "account": "default", "symbol": "BTC"
            })
        rd = _resp_to_dict(response)
        self.assertTrue(rd.get("success"), rd)


class TestEdgexGetTickersSafety(unittest.TestCase):
    """Safety: zero /api/trade/execute calls and zero write operations in the
    EdgeX agent's get_tickers code path."""

    def test_V_no_trade_execute(self):
        src = (REPO / "plugins/trade/agents/x_edgex_agent.py").read_text()
        self.assertNotIn("/api/trade/execute", src)


if __name__ == "__main__":
    unittest.main()
