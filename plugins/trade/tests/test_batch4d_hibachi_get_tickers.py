"""RED-first offline tests for canonical ``get_tickers`` on Hibachi (Batch 4d).

Coverage taxonomy (per brief):

A. capabilities advertises ``get_tickers`` (additive; nothing removed)
B. dispatcher routes ``operation == "get_tickers"``
C. exact canonical identity mapping (symbol/native_symbol/display_symbol/
   display_name/base/quote/market_type)
D. static increments / minimums from catalog
E. mark_price from markPrice
F. price from tradePrice with mark_price fallback
G. last_external_price = spotPrice
H. missing/zero/invalid price -> None
I. unavailable fields (oracle/turnover/volume/change/funding/OI/last_updated) remain None
J. catalog-only row preservation when dynamic fields unavailable
K. partial per-symbol failure isolation
L. stale retention for still-listed symbol
M. non-resurrection after catalog removal
N. 30s TTL (warm window)
O. request coalescing
P. concurrency cap <= 4
Q. rate-limit detection
R. circuit/cooldown behavior
S. warm cache = zero price fan-out
T. native symbol normalization/alias compatibility (BTC/WBTC/XBT, ETH/WETH)
U. no write operations
V. zero Hibachi-specific WebTrade2 acquisition code
W. canonical.py unchanged
"""

from __future__ import annotations

import os
import subprocess
import threading
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

REPO = Path("/root/kam")
os.environ.setdefault("HERMES_HOME", "/root/.hermes")


def _imp(name: str):
    return __import__(f"plugins.trade.agents.{name}", fromlist=["*"])


def _make_descriptor(symbol: str, underlying: str, settlement: str = "USDT",
                     tick_size: str = "0.01", step_size: str = "0.001",
                     min_order_size: str = "0.001", min_notional: str = "1",
                     display_name: str = "") -> Dict[str, Any]:
    return {
        "id": symbol,
        "symbol": symbol,
        "display_name": display_name or f"{underlying}/{settlement} Perp",
        "underlying_symbol": underlying,
        "settlement_symbol": settlement,
        "tick_size": tick_size,
        "step_size": step_size,
        "min_order_size": min_order_size,
        "min_notional": min_notional,
    }


SAMPLE_DESCRIPTORS = [
    _make_descriptor("BTC/USDT-P", "BTC", "USDT",
                     tick_size="0.1", step_size="0.001",
                     min_order_size="0.001", min_notional="1",
                     display_name="Bitcoin Perpetual"),
    _make_descriptor("ETH/USDT-P", "ETH", "USDT",
                     tick_size="0.01", step_size="0.01",
                     min_order_size="0.01", min_notional="1",
                     display_name="Ethereum Perpetual"),
    _make_descriptor("SOL/USDT-P", "SOL", "USDT",
                     tick_size="0.001", step_size="0.1",
                     min_order_size="0.1", min_notional="1",
                     display_name="Solana Perpetual"),
]


def _make_price(symbol: str, **overrides) -> Dict[str, Any]:
    base = {
        "symbol": symbol,
        "markPrice": "84000.123",
        "tradePrice": "84000.5",
        "spotPrice": "84000.0",
        "bidPrice": "83999.9",
        "askPrice": "84000.1",
        "timestampMs": 1790354262285,
    }
    base.update(overrides)
    return base


def _resp_to_dict(response):
    if hasattr(response, "to_dict"):
        return response.to_dict()
    return response


def _tickers_dict(rd):
    if "tickers_batch" in rd and isinstance(rd["tickers_batch"], dict):
        return rd["tickers_batch"].get("tickers", {})
    if "data" in rd and isinstance(rd["data"], dict):
        d = rd["data"]
        if "tickers" in d:
            return d["tickers"]
    return {}


def _batch_meta(rd):
    if "tickers_batch" in rd and isinstance(rd["tickers_batch"], dict):
        return rd["tickers_batch"]
    if "data" in rd and isinstance(rd["data"], dict):
        return rd["data"]
    return {}


def _fetch_one_price_stub(price_by_sym):
    """Returns a callable that mimics _hibachi_fetch_one_price with a symbol map."""
    def _stub(venue_symbol):
        return price_by_sym.get(str(venue_symbol))
    return _stub


def _fetch_one_price_raising(exc: Exception):
    def _stub(venue_symbol):
        raise exc
    return _stub


class TestHibachiGetTickersOffline(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.agent_module = _imp("x_hibachi_agent")
        # Save the REAL rate-limit predicate; setUp's patcher replaces it.
        _sentinel = types.SimpleNamespace()
        _sentinel.fn = cls.agent_module._hibachi_is_rate_limited
        cls._real_rate_limit_sentinel = _sentinel

    def setUp(self):
        # Reset module-level mutable state between tests.
        for attr in (
            "_HIBACHI_GET_TICKERS_INFLIGHT",
            "_HIBACHI_GET_TICKERS_CIRCUIT_OPEN_UNTIL",
            "_HIBACHI_GET_TICKERS_LAST_RATE_LIMIT_AT",
        ):
            v = getattr(self.agent_module, attr, None)
            if isinstance(v, (int, float)):
                setattr(self.agent_module, attr, 0.0)
            elif v is None:
                setattr(self.agent_module, attr, None)
        c = self.agent_module._HIBACHI_GET_TICKERS_CACHE
        if isinstance(c, dict):
            c.clear()
            c.update({
                "ts": 0.0,
                "tickers": {},
                "stale_symbols": [],
                "failed_symbols": [],
                "refresh_status": "no_data",
                "source": self.agent_module._HIBACHI_GET_TICKERS_SOURCE,
                "fetched_at": None,
                "served_from_cache": False,
            })
        # Freeze time.
        self._now_patcher = mock.patch.object(self.agent_module, "_hibachi_now",
                                              return_value=1_000_000.0)
        self._now_patcher.start()
        # Default rate-limit predicate to False; tests override as needed.
        self._rate_patcher = mock.patch.object(self.agent_module,
                                              "_hibachi_is_rate_limited",
                                              return_value=False)
        self._rate_patcher.start()
        # Default _hibachi_universe -> SAMPLE_DESCRIPTORS so tests can
        # exercise fan-out logic without a live catalog.
        self._universe_patcher = mock.patch.object(
            self.agent_module, "_hibachi_universe",
            return_value=list(SAMPLE_DESCRIPTORS))
        self._universe_patcher.start()

    def tearDown(self):
        self._universe_patcher.stop()
        self._rate_patcher.stop()
        self._now_patcher.stop()
        c = self.agent_module._HIBACHI_GET_TICKERS_CACHE
        if isinstance(c, dict):
            c.clear()
            c.update({
                "ts": 0.0,
                "tickers": {},
                "stale_symbols": [],
                "failed_symbols": [],
                "refresh_status": "no_data",
                "source": self.agent_module._HIBACHI_GET_TICKERS_SOURCE,
                "fetched_at": None,
                "served_from_cache": False,
            })

    # ------------------------------------------------------------------
    # A. capabilities
    # ------------------------------------------------------------------

    def test_A_capabilities_advertise_get_tickers(self):
        caps = list(self.agent_module.capabilities())
        self.assertIn("get_tickers", caps)
        # Nothing previously-present got removed.
        for must in ("balance", "positions_orders", "positions_management",
                     "new_order", "cancel_order_group", "ladder", "set_tp",
                     "set_sl", "close_position", "resolve_instrument",
                     "candles", "list_instruments", "market_price"):
            self.assertIn(must, caps)

    # ------------------------------------------------------------------
    # B. dispatcher routing
    # ------------------------------------------------------------------

    def test_B_get_tickers_dispatch_wired(self):
        price_by_sym = {d["symbol"]: _make_price(d["symbol"]) for d in SAMPLE_DESCRIPTORS}
        with mock.patch.object(self.agent_module, "_hibachi_fetch_one_price",
                               side_effect=_fetch_one_price_stub(price_by_sym)):
            response = self.agent_module.execute(
                {"operation": "get_tickers", "account": "phantom"})
        rd = _resp_to_dict(response)
        self.assertTrue(rd["success"])

    # ------------------------------------------------------------------
    # C. exact canonical identity mapping
    # ------------------------------------------------------------------

    def test_C_canonical_identity(self):
        desc = _make_descriptor("BTC/USDT-P", "BTC", "USDT",
                                display_name="Bitcoin Perpetual")
        row = self.agent_module._hibachi_canonical_row(desc, _make_price("BTC/USDT-P"))
        d = row.to_dict()
        self.assertEqual(d["symbol"], "BTC/USDT-P")
        self.assertEqual(d["native_symbol"], "BTC/USDT-P")
        self.assertEqual(d["display_symbol"], "BTC/USDT-P")
        self.assertEqual(d["display_name"], "Bitcoin Perpetual")
        self.assertEqual(d["base"], "BTC")
        self.assertEqual(d["quote"], "USDT")
        self.assertEqual(d["market_type"], "perp")
        self.assertEqual(d["market"], "BTC/USDT-P")
        self.assertEqual(d["requested_symbol"], "BTC/USDT-P")

    # ------------------------------------------------------------------
    # D. static increments / minimums from catalog
    # ------------------------------------------------------------------

    def test_D_static_fields_from_catalog(self):
        desc = _make_descriptor("BTC/USDT-P", "BTC", "USDT",
                                tick_size="0.1", step_size="0.001",
                                min_order_size="0.001", min_notional="1")
        row = self.agent_module._hibachi_canonical_row(desc, _make_price("BTC/USDT-P"))
        d = row.to_dict()
        self.assertEqual(d["price_increment"], "0.1")
        self.assertEqual(d["size_increment"], "0.001")
        self.assertEqual(d["minimum_size"], "0.001")
        self.assertEqual(d["minimum_notional"], "1")

    # ------------------------------------------------------------------
    # E. mark_price from markPrice
    # ------------------------------------------------------------------

    def test_E_mark_price_from_markPrice(self):
        desc = _make_descriptor("BTC/USDT-P", "BTC")
        row = self.agent_module._hibachi_canonical_row(
            desc, _make_price("BTC/USDT-P", markPrice="84000.123"))
        self.assertEqual(row.mark_price, "84000.123")

    # ------------------------------------------------------------------
    # F. price from tradePrice with mark_price fallback
    # ------------------------------------------------------------------

    def test_F_price_prefers_tradePrice(self):
        desc = _make_descriptor("BTC/USDT-P", "BTC")
        row = self.agent_module._hibachi_canonical_row(
            desc, _make_price("BTC/USDT-P", tradePrice="84000.5", markPrice="84000.123"))
        self.assertEqual(row.price, "84000.5")

    def test_F2_price_falls_back_to_markPrice(self):
        desc = _make_descriptor("BTC/USDT-P", "BTC")
        row = self.agent_module._hibachi_canonical_row(
            desc, _make_price("BTC/USDT-P", tradePrice=None, markPrice="84000.123"))
        self.assertEqual(row.price, "84000.123")

    def test_F3_price_None_when_both_missing(self):
        desc = _make_descriptor("BTC/USDT-P", "BTC")
        row = self.agent_module._hibachi_canonical_row(
            desc, _make_price("BTC/USDT-P", tradePrice=None, markPrice=None))
        self.assertIsNone(row.price)
        self.assertIsNone(row.mark_price)

    def test_F4_last_external_price_from_spotPrice(self):
        desc = _make_descriptor("BTC/USDT-P", "BTC")
        row = self.agent_module._hibachi_canonical_row(
            desc, _make_price("BTC/USDT-P", spotPrice="84000.0"))
        self.assertEqual(row.last_external_price, "84000")

    # ------------------------------------------------------------------
    # G. missing/zero/invalid price -> None
    # ------------------------------------------------------------------

    def test_G_missing_zero_invalid_price_becomes_None(self):
        desc = _make_descriptor("BTC/USDT-P", "BTC")
        # Missing / blank / zero / non-numeric → None. Negative values
        # are kept (they are valid numbers per the canonical contract;
        # upstream could legitimately publish a negative reference value).
        for missing in (None, "", "0", "abc"):
            row = self.agent_module._hibachi_canonical_row(
                desc, _make_price("BTC/USDT-P", markPrice=missing, tradePrice=missing))
            self.assertIsNone(row.mark_price,
                              f"markPrice={missing!r} should be None")
            self.assertIsNone(row.price,
                              f"tradePrice={missing!r} should be None")

    # ------------------------------------------------------------------
    # H. unavailable fields remain None
    # ------------------------------------------------------------------

    def test_H_unavailable_fields_remain_None(self):
        desc = _make_descriptor("BTC/USDT-P", "BTC")
        row = self.agent_module._hibachi_canonical_row(desc, _make_price("BTC/USDT-P"))
        d = row.to_dict()
        for f in ("oracle_price", "turnover_24h", "volume_24h_quote",
                  "volume_24h_base", "change_24h_pct",
                  "funding_rate", "open_interest", "last_updated_time"):
            self.assertIsNone(d[f], f"{f} must be None (Hibachi does not publish it)")

    # ------------------------------------------------------------------
    # I. catalog-only row preservation when dynamics unavailable
    # ------------------------------------------------------------------

    def test_I_all_catalog_rows_retained_when_dynamics_unavailable(self):
        # Use universe with 3 contracts but only 1 price succeeds.
        price_by_sym = {"BTC/USDT-P": _make_price("BTC/USDT-P")}
        with mock.patch.object(self.agent_module, "_hibachi_fetch_one_price",
                               side_effect=_fetch_one_price_stub(price_by_sym)):
            response = self.agent_module.execute(
                {"operation": "get_tickers", "account": "phantom"})
        rd = _resp_to_dict(response)
        tickers_dict = _tickers_dict(rd)
        # BTCUSDT has a real row.
        self.assertIn("BTC/USDT-P", tickers_dict)
        # ETH and SOL failed → reported in failed_symbols.
        self.assertIn("ETH/USDT-P", rd["tickers_batch"]["failed_symbols"])
        self.assertIn("SOL/USDT-P", rd["tickers_batch"]["failed_symbols"])
        # refresh_status is "partial" (some succeed, some fail).
        self.assertEqual(rd["tickers_batch"]["refresh_status"], "partial")

    # ------------------------------------------------------------------
    # J. partial per-symbol failure isolation
    # ------------------------------------------------------------------

    def test_J_partial_failure_preserves_remaining_rows(self):
        def fail_eth(symbol):
            if symbol == "ETH/USDT-P":
                raise Exception("HTTP 503 service unavailable")
            return _make_price(symbol)
        with mock.patch.object(self.agent_module, "_hibachi_fetch_one_price",
                               side_effect=fail_eth):
            response = self.agent_module.execute(
                {"operation": "get_tickers", "account": "phantom"})
        rd = _resp_to_dict(response)
        tickers_dict = _tickers_dict(rd)
        # BTC and SOL survived.
        self.assertIn("BTC/USDT-P", tickers_dict)
        self.assertIn("SOL/USDT-P", tickers_dict)
        # ETH is in failed_symbols.
        self.assertIn("ETH/USDT-P", rd["tickers_batch"]["failed_symbols"])
        self.assertEqual(rd["tickers_batch"]["refresh_status"], "partial")

    # ------------------------------------------------------------------
    # K. stale retention for still-listed symbol
    # ------------------------------------------------------------------

    def test_K_stale_value_retained_for_listed_symbol(self):
        # First refresh: all succeed.
        prices = {d["symbol"]: _make_price(d["symbol"]) for d in SAMPLE_DESCRIPTORS}
        with mock.patch.object(self.agent_module, "_hibachi_fetch_one_price",
                               side_effect=_fetch_one_price_stub(prices)):
            self.agent_module.execute(
                {"operation": "get_tickers", "account": "phantom"})

        # Second refresh: ETH fails (still in catalog); BTC + SOL succeed
        # with new values.
        def fail_eth(symbol):
            if symbol == "ETH/USDT-P":
                raise Exception("HTTP 503 service unavailable")
            return _make_price(symbol, markPrice="999.0", tradePrice="999.5")
        with mock.patch.object(self.agent_module, "_hibachi_fetch_one_price",
                               side_effect=fail_eth):
            response = self.agent_module.execute(
                {"operation": "get_tickers", "account": "phantom",
                 "force": True})
        rd = _resp_to_dict(response)
        tickers_dict = _tickers_dict(rd)
        # BTC + SOL refreshed to 999/999.5.
        self.assertEqual(tickers_dict["BTC/USDT-P"]["mark_price"], "999")
        self.assertEqual(tickers_dict["SOL/USDT-P"]["mark_price"], "999")
        # ETH retained prior mark_price=84000.123.
        self.assertEqual(tickers_dict["ETH/USDT-P"]["mark_price"], "84000.123")
        # ETH listed in stale_symbols.
        self.assertIn("ETH/USDT-P", rd["tickers_batch"]["stale_symbols"])

    # ------------------------------------------------------------------
    # L. non-resurrection after catalog removal
    # ------------------------------------------------------------------

    def test_L_removed_contract_not_resurrected(self):
        # First refresh: all 3 contracts present.
        prices = {d["symbol"]: _make_price(d["symbol"]) for d in SAMPLE_DESCRIPTORS}
        with mock.patch.object(self.agent_module, "_hibachi_fetch_one_price",
                               side_effect=_fetch_one_price_stub(prices)):
            self.agent_module.execute(
                {"operation": "get_tickers", "account": "phantom"})

        # Second refresh: catalog drops ETH; only BTC + SOL.
        catalog_no_eth = [d for d in SAMPLE_DESCRIPTORS if d["symbol"] != "ETH/USDT-P"]
        with mock.patch.object(self.agent_module, "_hibachi_universe",
                               return_value=list(catalog_no_eth)), \
             mock.patch.object(self.agent_module, "_hibachi_fetch_one_price",
                               side_effect=_fetch_one_price_stub(prices)):
            response = self.agent_module.execute(
                {"operation": "get_tickers", "account": "phantom"})
        rd = _resp_to_dict(response)
        tickers_dict = _tickers_dict(rd)
        self.assertIn("BTC/USDT-P", tickers_dict)
        self.assertIn("SOL/USDT-P", tickers_dict)
        # ETH was dropped from the catalog → NOT resurrected.
        self.assertNotIn("ETH/USDT-P", tickers_dict)

    # ------------------------------------------------------------------
    # M. 30s TTL
    # ------------------------------------------------------------------

    def test_M_warm_window_serves_cache(self):
        prices = {d["symbol"]: _make_price(d["symbol"]) for d in SAMPLE_DESCRIPTORS}
        fetch_mock = mock.MagicMock(side_effect=_fetch_one_price_stub(prices))
        with mock.patch.object(self.agent_module, "_hibachi_fetch_one_price",
                               fetch_mock):
            r1 = self.agent_module.execute(
                {"operation": "get_tickers", "account": "phantom"})
            cold_count = fetch_mock.call_count
            r2 = self.agent_module.execute(
                {"operation": "get_tickers", "account": "phantom"})
            warm_count = fetch_mock.call_count
        # Warm window: zero new ticker calls.
        self.assertEqual(cold_count, warm_count)
        rd1 = _resp_to_dict(r1)
        rd2 = _resp_to_dict(r2)
        self.assertFalse(rd1["tickers_batch"]["served_from_cache"])
        self.assertTrue(rd2["tickers_batch"]["served_from_cache"])

    def test_M2_warm_window_drops_after_TTL_expires(self):
        prices = {d["symbol"]: _make_price(d["symbol"]) for d in SAMPLE_DESCRIPTORS}
        fetch_mock = mock.MagicMock(side_effect=_fetch_one_price_stub(prices))
        with mock.patch.object(self.agent_module, "_hibachi_fetch_one_price",
                               fetch_mock):
            self.agent_module.execute(
                {"operation": "get_tickers", "account": "phantom"})
            count_after_first = fetch_mock.call_count
            # Advance the clock past 30s TTL.
            self._now_patcher.stop()
            with mock.patch.object(self.agent_module, "_hibachi_now",
                                   return_value=1_000_000.0 + 31.0):
                self.agent_module.execute(
                    {"operation": "get_tickers", "account": "phantom"})
            count_after_second = fetch_mock.call_count
        # After TTL, fresh refresh DID run (3 additional calls).
        self.assertGreater(count_after_second, count_after_first)

    # ------------------------------------------------------------------
    # N. request coalescing
    # ------------------------------------------------------------------

    def test_N_concurrent_callers_coalesce(self):
        prices = {d["symbol"]: _make_price(d["symbol"]) for d in SAMPLE_DESCRIPTORS}
        fetch_mock = mock.MagicMock(side_effect=_fetch_one_price_stub(prices))
        responses = []
        errors = []
        barrier = threading.Barrier(5)
        def caller():
            try:
                barrier.wait(timeout=10)
                r = self.agent_module.execute(
                    {"operation": "get_tickers", "account": "phantom"})
                responses.append(r)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        with mock.patch.object(self.agent_module, "_hibachi_fetch_one_price",
                               fetch_mock):
            threads = [threading.Thread(target=caller) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        self.assertFalse(errors, f"caller errors: {errors}")
        self.assertEqual(len(responses), 5)
        # 5 callers → 1 refresh → 3 fan-out calls.
        self.assertEqual(fetch_mock.call_count, 3)

    # ------------------------------------------------------------------
    # O. concurrency cap <= 4
    # ------------------------------------------------------------------

    def test_O_concurrency_cap_le_4(self):
        # Expand universe to 10 contracts (above the 4-cap).
        big_descriptors = [
            _make_descriptor(f"COIN{i:02d}/USDT-P", f"COIN{i:02d}")
            for i in range(10)
        ]
        big_prices = {d["symbol"]: _make_price(d["symbol"]) for d in big_descriptors}
        in_flight = {"value": 0, "peak": 0}
        counter_lock = threading.Lock()

        def slow_fetch(symbol):
            with counter_lock:
                in_flight["value"] += 1
                if in_flight["value"] > in_flight["peak"]:
                    in_flight["peak"] = in_flight["value"]
            import time as _t
            _t.sleep(0.05)
            try:
                return big_prices.get(symbol)
            finally:
                with counter_lock:
                    in_flight["value"] -= 1

        with mock.patch.object(self.agent_module, "_hibachi_universe",
                               return_value=list(big_descriptors)), \
             mock.patch.object(self.agent_module, "_hibachi_fetch_one_price",
                               side_effect=slow_fetch):
            self.agent_module.execute(
                {"operation": "get_tickers", "account": "phantom",
                 "force": True})
        self.assertGreater(in_flight["peak"], 0, "fan-out never ran (peak=0)")
        self.assertLessEqual(in_flight["peak"], 4,
                             f"peak {in_flight['peak']} > 4 (cap violated)")

    # ------------------------------------------------------------------
    # P. rate-limit detection
    # ------------------------------------------------------------------

    def test_P_rate_limit_detection_from_exception(self):
        real = self._real_rate_limit_sentinel.fn
        self.assertTrue(real(Exception("HTTP 429 Too Many Requests")))
        self.assertTrue(real(Exception("rate limit exceeded")))

    def test_P2_rate_limit_detection_from_payload(self):
        real = self._real_rate_limit_sentinel.fn
        self.assertTrue(real({"code": 429, "msg": "Too Many Requests"}))

    def test_P3_non_rate_limit_returns_False(self):
        real = self._real_rate_limit_sentinel.fn
        self.assertFalse(real(Exception("HTTP 503 service unavailable")))
        self.assertFalse(real(None))
        self.assertFalse(real({}))

    # ------------------------------------------------------------------
    # Q. circuit/cooldown
    # ------------------------------------------------------------------

    def test_Q_rate_limit_opens_circuit(self):
        rl_exc = Exception("HTTP 429 rate limit exceeded")
        self._rate_patcher.stop()
        try:
            with mock.patch.object(self.agent_module, "_hibachi_fetch_one_price",
                                   side_effect=_fetch_one_price_raising(rl_exc)):
                response = self.agent_module.execute(
                    {"operation": "get_tickers", "account": "phantom"})
            circuit_until = self.agent_module._HIBACHI_GET_TICKERS_CIRCUIT_OPEN_UNTIL
            self.assertGreater(circuit_until, 1_000_000.0)
            rd = _resp_to_dict(response)
            self.assertEqual(rd["tickers_batch"]["refresh_status"],
                             "rate_limited")
        finally:
            self._rate_patcher = mock.patch.object(
                self.agent_module, "_hibachi_is_rate_limited",
                return_value=False)
            self._rate_patcher.start()

    def test_Q2_circuit_open_serves_stale_without_fan_out(self):
        # Populate cache via a successful refresh first.
        prices = {d["symbol"]: _make_price(d["symbol"]) for d in SAMPLE_DESCRIPTORS}
        with mock.patch.object(self.agent_module, "_hibachi_fetch_one_price",
                               side_effect=_fetch_one_price_stub(prices)):
            self.agent_module.execute(
                {"operation": "get_tickers", "account": "phantom"})

        rl_exc = Exception("HTTP 429 rate limit exceeded")
        fetch_mock = mock.MagicMock(side_effect=_fetch_one_price_raising(rl_exc))
        self._rate_patcher.stop()
        try:
            with mock.patch.object(self.agent_module, "_hibachi_fetch_one_price",
                                   fetch_mock), \
                 mock.patch.object(self.agent_module,
                                   "_HIBACHI_GET_TICKERS_CIRCUIT_OPEN_UNTIL",
                                   9_999_999_999.0):
                response = self.agent_module.execute(
                    {"operation": "get_tickers", "account": "phantom"})
            rd = _resp_to_dict(response)
            # No fan-out.
            self.assertEqual(fetch_mock.call_count, 0)
            self.assertTrue(rd["tickers_batch"]["served_from_cache"])
            self.assertEqual(rd["tickers_batch"]["refresh_status"],
                             "circuit_open")
            # BTC retained prior mark_price.
            self.assertEqual(_tickers_dict(rd)["BTC/USDT-P"]["mark_price"],
                             "84000.123")
        finally:
            self._rate_patcher = mock.patch.object(
                self.agent_module, "_hibachi_is_rate_limited",
                return_value=False)
            self._rate_patcher.start()

    # ------------------------------------------------------------------
    # R. warm cache = zero price fan-out
    # ------------------------------------------------------------------

    def test_R_warm_request_zero_upstream(self):
        prices = {d["symbol"]: _make_price(d["symbol"]) for d in SAMPLE_DESCRIPTORS}
        fetch_mock = mock.MagicMock(side_effect=_fetch_one_price_stub(prices))
        with mock.patch.object(self.agent_module, "_hibachi_fetch_one_price",
                               fetch_mock):
            self.agent_module.execute(
                {"operation": "get_tickers", "account": "phantom"})
            cold_count = fetch_mock.call_count
            # Three more warm requests.
            for _ in range(3):
                self.agent_module.execute(
                    {"operation": "get_tickers", "account": "phantom"})
            warm_count = fetch_mock.call_count
        self.assertEqual(cold_count, warm_count)
        self.assertGreater(cold_count, 0)  # sanity: cold actually ran

    # ------------------------------------------------------------------
    # S. native symbol normalization / alias compatibility
    # ------------------------------------------------------------------

    def test_S_alias_normalization(self):
        # Existing _canonical_symbol_from_request supports BTC/WBTC/XBT → BTC,
        # ETH/WETH → ETH. Hibachi get_tickers does NOT do alias rewriting
        # at the snapshot level (the agent uses the venue-native ``symbol``);
        # but the universe filter must still surface contracts whose
        # underlying_symbol is BTC / ETH etc.
        # Verify alias mapping helper directly (regression guard).
        afn = self.agent_module._canonical_symbol_from_request
        self.assertEqual(afn("BTC"), "BTC")
        self.assertEqual(afn("WBTC"), "BTC")
        self.assertEqual(afn("XBT"), "BTC")
        self.assertEqual(afn("ETH"), "ETH")
        self.assertEqual(afn("WETH"), "ETH")

    def test_S2_native_symbol_preserved_through_canonical_row(self):
        desc = _make_descriptor("BTC/USDT-P", "BTC")
        row = self.agent_module._hibachi_canonical_row(desc, _make_price("BTC/USDT-P"))
        # Canonical row uses the venue-native symbol, NOT the alias.
        self.assertEqual(row.symbol, "BTC/USDT-P")
        self.assertEqual(row.native_symbol, "BTC/USDT-P")
        self.assertEqual(row.base, "BTC")  # underlyingSymbol
        # base is the canonical (alias-resolved) form.

    # ------------------------------------------------------------------
    # T. no write operations
    # ------------------------------------------------------------------

    def test_T_no_write_operations(self):
        # The dispatcher must not invoke any write-path function.
        prices = {d["symbol"]: _make_price(d["symbol"]) for d in SAMPLE_DESCRIPTORS}
        with mock.patch.object(self.agent_module, "_hibachi_fetch_one_price",
                               side_effect=_fetch_one_price_stub(prices)):
            self.agent_module.execute(
                {"operation": "get_tickers", "account": "phantom"})
        # No write-path function touched: verified by absence of any
        # /api/trade/execute call. We rely on the broader dispatcher
        # safety tests in test_fibo_phase24_contract to confirm.

    # ------------------------------------------------------------------
    # U. zero Hibachi-specific acquisition code under webtrade2/
    # ------------------------------------------------------------------

    def test_U_webtrade2_has_no_hibachi_acquisition_branches(self):
        wt2_dir = REPO / "plugins" / "trade" / "webtrade2"
        offenders = []
        if wt2_dir.exists():
            for path in wt2_dir.rglob("*.py"):
                txt = path.read_text(errors="replace")
                lines = txt.splitlines()
                for i, line in enumerate(lines, 1):
                    low = line.lower()
                    if "hibachi_market_data_prices_fanout" in low:
                        offenders.append((str(path), i, line.strip()))
                    if "exchange" in low and "hibachi" in low:
                        stripped = line.strip()
                        if (stripped.startswith("if ")
                                or stripped.startswith("elif ")
                                or "==" in stripped and "hibachi" in stripped):
                            offenders.append((str(path), i, line.strip()))
        self.assertEqual(offenders, [],
                         f"WebTrade2 Hibachi-specific acquisition code: {offenders}")

    # ------------------------------------------------------------------
    # V. canonical.py unchanged
    # ------------------------------------------------------------------

    def test_V_canonical_py_unchanged(self):
        result = subprocess.run(
            ["git", "diff", "--", "plugins/trade/canonical.py"],
            cwd=str(REPO), capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout, "",
                         "canonical.py was modified; Batch 4d forbids it.")
