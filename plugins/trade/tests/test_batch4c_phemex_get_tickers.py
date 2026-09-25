"""RED-first offline tests for canonical ``get_tickers`` on Phemex (Batch 4c).

Coverage taxonomy (per brief):

A. capabilities advertises ``get_tickers`` (additive; nothing removed)
B. dispatcher routes ``operation == "get_tickers"`` to a Phemex-owned function
C. supported perpetual universe filtering (USDT settle, active status)
D. NO spot fan-out (only PerpetualV2/Perpetual USDT contracts)
E. canonical identity mapping (symbol/native_symbol/base/quote/market_type)
F. mark_price from markPriceRp (no scaling)
G. oracle_price from indexPriceRp
H. price from markPriceRp with closeRp fallback
I. funding_rate from fundingRateRr
J. open_interest from openInterestRv
K. turnover_24h/volume_24h_quote from turnoverRv
L. volume_24h_base from volumeRq
M. change_24h_pct derived from openRp/closeRp
N. last_updated_time from timestamp
O. static fields: price_increment from catalog, size_increment from qty_step
P. missing/zero/invalid dynamic values -> None (catalog row preserved)
Q. partial-failure isolation (one bad symbol does not poison others)
R. stale-value retention for still-listed contracts
S. removed-product non-resurrection (catalog prune)
T. TTL cache (60s warm window)
U. request coalescing (5 concurrent callers -> 1 refresh)
V. concurrency cap <= 8 (Semaphore)
W. rate-limit detection (429 / rate-limit tokens)
X. circuit/cooldown open on rate-limit
Y. warm request = zero upstream ticker calls
Z. WebTrade2 contains zero Phemex-specific acquisition code
AA. canonical.py unchanged
"""

from __future__ import annotations

import os
import subprocess
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

REPO = Path("/root/kam")
os.environ.setdefault("HERMES_HOME", "/root/.hermes")


def _imp(name: str):
    return __import__(f"plugins.trade.agents.{name}", fromlist=["*"])


FAKE_CREDENTIALS = {
    "account": "amiroo",
    "api_key": "fake_api_key",
    "api_secret": "fake_api_secret",
    "base_url": "https://api.phemex.com",
    "currency": "USDT",
}


def _make_catalog(symbol: str, base: str, quote: str = "USDT",
                  tick_size: str = "0.1", qty_step: str = "0.001",
                  min_qty: str = "0.001", min_notional: str = "1",
                  ptype: str = "PerpetualV2", status: str = "Listed",
                  settle: str = "USDT") -> Dict[str, Any]:
    """Build a single Phemex product meta row matching _load_products shape."""
    from decimal import Decimal
    return {
        "symbol": symbol,
        "type": ptype,
        "display": f"{base}/{quote}",
        "tick_size": Decimal(tick_size),
        "qty_step": Decimal(qty_step),
        "min_qty": Decimal(min_qty),
        "min_notional": Decimal(min_notional),
        "qty_precision": 8,
        "price_precision": 8,
        "status": status,
        "settle": settle,
    }


SAMPLE_CATALOG = {
    "BTCUSDT": _make_catalog("BTCUSDT", "BTC", tick_size="0.1", qty_step="0.001", min_qty="0.001", min_notional="1"),
    "ETHUSDT": _make_catalog("ETHUSDT", "ETH", tick_size="0.01", qty_step="0.01", min_qty="0.01", min_notional="1"),
    "SOLUSDT": _make_catalog("SOLUSDT", "SOL", tick_size="0.001", qty_step="0.1", min_qty="0.1", min_notional="1"),
    # Spot product — must NOT be in the fan-out universe
    "BTCUSDT_SPOT": _make_catalog("BTCUSDT_SPOT", "BTC", tick_size="0.01", qty_step="0.0001", min_qty="0.0001", min_notional="1",
                                  ptype="Spot"),
    # Inverse perp (BTCUSD not USDT) — must NOT be in the fan-out universe
    "BTCUSD": _make_catalog("BTCUSD", "BTC", tick_size="0.5", qty_step="1", min_qty="1", min_notional="1",
                            ptype="Perpetual", settle="USD"),
    # Delisted product — must NOT be in the fan-out universe
    "OLDCOINUSDT": _make_catalog("OLDCOINUSDT", "OLDCOIN", tick_size="0.0001", qty_step="1", min_qty="1", min_notional="1",
                                  status="Delisted"),
}


def _make_ticker(symbol: str, **overrides) -> Dict[str, Any]:
    """Build a Phemex-style per-symbol ticker payload.

    Keys are the actual Phemex ``/md/v2/ticker/24hr`` response fields.
    ``None``/missing/0 values flow through as None in canonical rows.
    """
    base = {
        "symbol": symbol,
        "markPriceRp": "84680.5",
        "indexPriceRp": "84680.1",
        "lastRp": "84680.4",
        "closeRp": "84650.0",
        "openRp": "84300.0",
        "highRp": "85000.0",
        "lowRp": "84000.0",
        "fundingRateRr": "0.0001",
        "predFundingRateRr": "0.00012",
        "openInterestRv": "553033453",
        "turnoverRv": "320455411.5135",
        "volumeRq": "3804.275",
        "timestamp": 1790336692829,
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


def _fetch_ticker_stub(ticker_by_sym):
    """Returns a callable that mimics _phemex_fetch_ticker with a symbol map.

    The agent's worker calls ``_phemex_fetch_ticker(credentials, symbol)``
    with a native symbol string.
    """
    def _stub(credentials, symbol):
        return ticker_by_sym.get(str(symbol).upper())

    return _stub


def _fetch_ticker_raising(exc: Exception):
    def _stub(credentials, symbol):
        raise exc

    return _stub


class TestPhemexGetTickersOffline(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.agent_module = _imp("x_phemex_agent")
        # Save the REAL rate-limit predicate before setUp's patcher
        # overrides it; rate-limit-detection tests need to exercise the
        # actual logic, not the patched-return-False mock. Stored as a
        # module-level attribute on a sentinel object so it is not
        # wrapped as a bound method (which would require ``self``).
        import types as _types
        _sentinel = _types.SimpleNamespace()
        _sentinel.fn = cls.agent_module._phemex_is_rate_limited
        cls._real_rate_limit_sentinel = _sentinel

    def setUp(self):
        # Reset module-level mutable state between tests.
        for attr in (
            "_PHEMEX_GET_TICKERS_INFLIGHT",
            "_PHEMEX_GET_TICKERS_CIRCUIT_OPEN_UNTIL",
            "_PHEMEX_GET_TICKERS_LAST_RATE_LIMIT_AT",
        ):
            v = getattr(self.agent_module, attr, None)
            if isinstance(v, (int, float)):
                setattr(self.agent_module, attr, 0.0)
            elif v is None:
                setattr(self.agent_module, attr, None)
        # Force-default the cache so tests start clean.
        c = self.agent_module._PHEMEX_GET_TICKERS_CACHE
        if isinstance(c, dict):
            c.clear()
            c.update({
                "ts": 0.0,
                "tickers": {},
                "stale_symbols": [],
                "failed_symbols": [],
                "refresh_status": "no_data",
                "source": self.agent_module._PHEMEX_GET_TICKERS_SOURCE,
                "fetched_at": None,
                "served_from_cache": False,
            })
        # Freeze time to a deterministic anchor.
        self._now_patcher = mock.patch.object(self.agent_module, "_phemex_now",
                                              return_value=1_000_000.0)
        self._now_patcher.start()
        # Default rate-limit predicate to False (override per-test).
        self._rate_patcher = mock.patch.object(self.agent_module,
                                              "_phemex_is_rate_limited",
                                              return_value=False)
        self._rate_patcher.start()

    def tearDown(self):
        self._rate_patcher.stop()
        self._now_patcher.stop()
        # Force-clear cache to prevent test bleed-through.
        c = self.agent_module._PHEMEX_GET_TICKERS_CACHE
        if isinstance(c, dict):
            c.clear()
            c.update({
                "ts": 0.0,
                "tickers": {},
                "stale_symbols": [],
                "failed_symbols": [],
                "refresh_status": "no_data",
                "source": self.agent_module._PHEMEX_GET_TICKERS_SOURCE,
                "fetched_at": None,
                "served_from_cache": False,
            })

    def _creds(self):
        return mock.patch.object(self.agent_module, "_lookup_credentials",
                                 return_value=dict(FAKE_CREDENTIALS))

    def _products(self):
        return mock.patch.object(self.agent_module, "_load_products",
                                 return_value=dict(SAMPLE_CATALOG))

    # ------------------------------------------------------------------
    # A. capabilities
    # ------------------------------------------------------------------

    def test_A_capabilities_advertise_get_tickers(self):
        caps = list(self.agent_module.capabilities())
        self.assertIn("get_tickers", caps)
        # Nothing previously-present got removed.
        for must in ("list_instruments", "market_price", "new_order",
                     "ladder", "resolve_instrument", "balance",
                     "candles", "positions_orders", "positions_management",
                     "cancel_order_group", "set_tp", "set_sl",
                     "close_position"):
            self.assertIn(must, caps)

    # ------------------------------------------------------------------
    # B. dispatcher routing
    # ------------------------------------------------------------------

    def test_B_get_tickers_dispatch_wired(self):
        # Verify the dispatcher calls the Phemex-owned _execute_get_tickers.
        with self._creds(), self._products(), \
             mock.patch.object(self.agent_module, "_phemex_fetch_ticker",
                               return_value=_make_ticker("BTCUSDT")), \
             mock.patch.object(self.agent_module, "_execute_get_tickers",
                               wraps=self.agent_module._execute_get_tickers) as spy:
            response = self.agent_module.execute(
                {"operation": "get_tickers", "account": "amiroo"})
        self.assertTrue(response.success)
        spy.assert_called_once()

    def test_B2_account_not_found_returns_failure(self):
        with mock.patch.object(self.agent_module, "_lookup_credentials",
                               return_value=None):
            response = self.agent_module.execute(
                {"operation": "get_tickers", "account": "missing"})
        rd = _resp_to_dict(response)
        self.assertFalse(rd["success"])
        self.assertEqual(rd["error"]["code"], "ACCOUNT_NOT_FOUND")

    # ------------------------------------------------------------------
    # C/D. supported perpetual universe (no spot, no inverse, no delisted)
    # ------------------------------------------------------------------

    def test_C_supported_perpetual_universe_filtering(self):
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        symbols = sorted(r["symbol"] for r in universe)
        # Only PerpetualV2 USDT active products.
        self.assertEqual(symbols, ["BTCUSDT", "ETHUSDT", "SOLUSDT"])

    def test_C2_universe_excludes_spot_inverse_delisted(self):
        # Direct verification per symbol.
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        symbols = {r["symbol"] for r in universe}
        # Spot product excluded by ptype filter.
        self.assertNotIn("BTCUSDT_SPOT", symbols)
        # Inverse excluded by settleCurrency != USDT filter.
        self.assertNotIn("BTCUSD", symbols)
        # Delisted excluded by status filter.
        self.assertNotIn("OLDCOINUSDT", symbols)

    # ------------------------------------------------------------------
    # E. canonical identity mapping
    # ------------------------------------------------------------------

    def test_E_canonical_identity(self):
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        btc = next(r for r in universe if r["symbol"] == "BTCUSDT")
        ticker = _make_ticker("BTCUSDT")
        row = self.agent_module._phemex_canonical_row_from_ticker(btc, ticker)
        d = row.to_dict()
        self.assertEqual(d["symbol"], "BTCUSDT")
        self.assertEqual(d["native_symbol"], "BTCUSDT")
        self.assertEqual(d["display_symbol"], "BTCUSDT")
        self.assertEqual(d["base"], "BTC")
        self.assertEqual(d["quote"], "USDT")
        self.assertEqual(d["market_type"], "perp")
        self.assertEqual(d["market"], "BTCUSDT")
        self.assertEqual(d["display_name"], "BTC/USDT")

    # ------------------------------------------------------------------
    # F. mark_price from markPriceRp (no scaling)
    # ------------------------------------------------------------------

    def test_F_mark_price_from_markPriceRp(self):
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        btc = next(r for r in universe if r["symbol"] == "BTCUSDT")
        ticker = _make_ticker("BTCUSDT", markPriceRp="84680.5")
        row = self.agent_module._phemex_canonical_row_from_ticker(btc, ticker)
        self.assertEqual(row.mark_price, "84680.5")

    def test_F2_mark_price_zero_or_missing_becomes_None(self):
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        btc = next(r for r in universe if r["symbol"] == "BTCUSDT")
        for missing in (None, "", "0", "abc"):
            ticker = _make_ticker("BTCUSDT", markPriceRp=missing)
            row = self.agent_module._phemex_canonical_row_from_ticker(btc, ticker)
            self.assertIsNone(row.mark_price,
                              f"markPriceRp={missing!r} should be None")

    # ------------------------------------------------------------------
    # G. oracle_price from indexPriceRp
    # ------------------------------------------------------------------

    def test_G_oracle_price_from_indexPriceRp(self):
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        btc = next(r for r in universe if r["symbol"] == "BTCUSDT")
        ticker = _make_ticker("BTCUSDT", indexPriceRp="84700.123")
        row = self.agent_module._phemex_canonical_row_from_ticker(btc, ticker)
        self.assertEqual(row.oracle_price, "84700.123")

    # ------------------------------------------------------------------
    # H. price from markPriceRp with closeRp fallback
    # ------------------------------------------------------------------

    def test_H_price_from_markPriceRp(self):
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        btc = next(r for r in universe if r["symbol"] == "BTCUSDT")
        ticker = _make_ticker("BTCUSDT", markPriceRp="84680.5", closeRp="84650")
        row = self.agent_module._phemex_canonical_row_from_ticker(btc, ticker)
        self.assertEqual(row.price, "84680.5")

    def test_H2_price_falls_back_to_closeRp_when_mark_unavailable(self):
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        btc = next(r for r in universe if r["symbol"] == "BTCUSDT")
        ticker = _make_ticker("BTCUSDT", markPriceRp=None, closeRp="84650")
        row = self.agent_module._phemex_canonical_row_from_ticker(btc, ticker)
        self.assertEqual(row.price, "84650")

    def test_H3_price_None_when_both_missing(self):
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        btc = next(r for r in universe if r["symbol"] == "BTCUSDT")
        ticker = _make_ticker("BTCUSDT", markPriceRp=None, closeRp=None)
        row = self.agent_module._phemex_canonical_row_from_ticker(btc, ticker)
        self.assertIsNone(row.price)

    # ------------------------------------------------------------------
    # I. funding_rate from fundingRateRr
    # ------------------------------------------------------------------

    def test_I_funding_rate_from_fundingRateRr(self):
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        btc = next(r for r in universe if r["symbol"] == "BTCUSDT")
        ticker = _make_ticker("BTCUSDT", fundingRateRr="0.00012")
        row = self.agent_module._phemex_canonical_row_from_ticker(btc, ticker)
        self.assertEqual(row.funding_rate, "0.00012")

    # ------------------------------------------------------------------
    # J. open_interest from openInterestRv
    # ------------------------------------------------------------------

    def test_J_open_interest_from_openInterestRv(self):
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        btc = next(r for r in universe if r["symbol"] == "BTCUSDT")
        ticker = _make_ticker("BTCUSDT", openInterestRv="12345.678")
        row = self.agent_module._phemex_canonical_row_from_ticker(btc, ticker)
        self.assertEqual(row.open_interest, "12345.678")

    # ------------------------------------------------------------------
    # K. turnover_24h / volume_24h_quote from turnoverRv
    # ------------------------------------------------------------------

    def test_K_turnover_24h_from_turnoverRv(self):
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        btc = next(r for r in universe if r["symbol"] == "BTCUSDT")
        ticker = _make_ticker("BTCUSDT", turnoverRv="9876543.21")
        row = self.agent_module._phemex_canonical_row_from_ticker(btc, ticker)
        self.assertEqual(row.turnover_24h, "9876543.21")
        self.assertEqual(row.volume_24h_quote, "9876543.21")

    # ------------------------------------------------------------------
    # L. volume_24h_base from volumeRq
    # ------------------------------------------------------------------

    def test_L_volume_24h_base_from_volumeRq(self):
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        btc = next(r for r in universe if r["symbol"] == "BTCUSDT")
        ticker = _make_ticker("BTCUSDT", volumeRq="1234.5678")
        row = self.agent_module._phemex_canonical_row_from_ticker(btc, ticker)
        self.assertEqual(row.volume_24h_base, "1234.5678")
        # Sanity: base and quote volumes are NEVER conflated.
        self.assertNotEqual(row.volume_24h_base, row.volume_24h_quote)

    # ------------------------------------------------------------------
    # M. change_24h_pct from openRp/closeRp
    # ------------------------------------------------------------------

    def test_M_change_24h_pct_from_openRp_closeRp(self):
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        btc = next(r for r in universe if r["symbol"] == "BTCUSDT")
        # (close - open)/open * 100
        ticker = _make_ticker("BTCUSDT", openRp="100", closeRp="105")
        row = self.agent_module._phemex_canonical_row_from_ticker(btc, ticker)
        self.assertEqual(row.change_24h_pct, "5")

    def test_M2_change_24h_pct_negative(self):
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        btc = next(r for r in universe if r["symbol"] == "BTCUSDT")
        ticker = _make_ticker("BTCUSDT", openRp="100", closeRp="95")
        row = self.agent_module._phemex_canonical_row_from_ticker(btc, ticker)
        self.assertEqual(row.change_24h_pct, "-5")

    def test_M3_change_24h_pct_None_when_open_zero(self):
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        btc = next(r for r in universe if r["symbol"] == "BTCUSDT")
        ticker = _make_ticker("BTCUSDT", openRp="0", closeRp="100")
        row = self.agent_module._phemex_canonical_row_from_ticker(btc, ticker)
        self.assertIsNone(row.change_24h_pct)

    def test_M4_change_24h_pct_None_when_inputs_missing(self):
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        btc = next(r for r in universe if r["symbol"] == "BTCUSDT")
        ticker = _make_ticker("BTCUSDT", openRp=None, closeRp="100")
        row = self.agent_module._phemex_canonical_row_from_ticker(btc, ticker)
        self.assertIsNone(row.change_24h_pct)

    # ------------------------------------------------------------------
    # N. last_updated_time from timestamp
    # ------------------------------------------------------------------

    def test_N_last_updated_time_from_timestamp(self):
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        btc = next(r for r in universe if r["symbol"] == "BTCUSDT")
        ticker = _make_ticker("BTCUSDT", timestamp=1790336692829)
        row = self.agent_module._phemex_canonical_row_from_ticker(btc, ticker)
        self.assertEqual(row.last_updated_time, "1790336692829")

    # ------------------------------------------------------------------
    # O. static fields from catalog
    # ------------------------------------------------------------------

    def test_O_price_increment_from_catalog(self):
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        btc = next(r for r in universe if r["symbol"] == "BTCUSDT")
        ticker = _make_ticker("BTCUSDT")
        row = self.agent_module._phemex_canonical_row_from_ticker(btc, ticker)
        self.assertEqual(row.price_increment, "0.1")
        self.assertEqual(row.size_increment, "0.001")
        self.assertEqual(row.minimum_size, "0.001")
        self.assertEqual(row.minimum_notional, "1")

    # ------------------------------------------------------------------
    # P. missing/zero/invalid -> None, but catalog row preserved
    # ------------------------------------------------------------------

    def test_P_catalog_row_preserved_with_all_None_dynamics(self):
        universe = self.agent_module._phemex_perpetual_universe(SAMPLE_CATALOG)
        btc = next(r for r in universe if r["symbol"] == "BTCUSDT")
        ticker = {"symbol": "BTCUSDT"}  # all dynamic fields missing
        row = self.agent_module._phemex_canonical_row_from_ticker(btc, ticker)
        d = row.to_dict()
        # Identity preserved.
        self.assertEqual(d["symbol"], "BTCUSDT")
        self.assertEqual(d["base"], "BTC")
        self.assertEqual(d["quote"], "USDT")
        self.assertEqual(d["market_type"], "perp")
        # All dynamics None.
        for f in ("mark_price", "oracle_price", "price",
                  "funding_rate", "open_interest",
                  "turnover_24h", "volume_24h_base",
                  "change_24h_pct", "last_updated_time"):
            self.assertIsNone(d[f], f"{f} should be None")

    # ------------------------------------------------------------------
    # Q. partial-failure isolation
    # ------------------------------------------------------------------

    def test_Q_partial_failure_preserves_remaining_rows(self):
        tickers = {
            "BTCUSDT": _make_ticker("BTCUSDT", markPriceRp="84680.5"),
            "ETHUSDT": _make_ticker("ETHUSDT", markPriceRp="2712.5"),
        }
        def partial(credentials, symbol):
            if symbol == "ETHUSDT":
                raise Exception("HTTP 503 service unavailable")
            return tickers.get(symbol)
        with self._creds(), self._products(), \
             mock.patch.object(self.agent_module, "_phemex_fetch_ticker",
                               side_effect=partial):
            response = self.agent_module.execute(
                {"operation": "get_tickers", "account": "amiroo"})
        rd = _resp_to_dict(response)
        tickers_dict = _tickers_dict(rd)
        # BTCUSDT survived even though ETHUSDT failed.
        self.assertIn("BTCUSDT", tickers_dict)
        self.assertEqual(tickers_dict["BTCUSDT"]["mark_price"], "84680.5")
        # ETHUSDT is in failed_symbols (per the brief).
        self.assertIn("ETHUSDT", rd["tickers_batch"]["failed_symbols"])
        # SOLUSDT was not requested by the stub; treat as failed.
        self.assertIn("SOLUSDT", rd["tickers_batch"]["failed_symbols"])

    # ------------------------------------------------------------------
    # R. stale-value retention for still-listed contracts
    # ------------------------------------------------------------------

    def test_R_stale_value_retained_for_listed_contract(self):
        # First refresh: all succeed.
        tickers_ok = {
            "BTCUSDT": _make_ticker("BTCUSDT", markPriceRp="84680.5"),
            "ETHUSDT": _make_ticker("ETHUSDT", markPriceRp="2712.5"),
            "SOLUSDT": _make_ticker("SOLUSDT", markPriceRp="150.0"),
        }
        with self._creds(), self._products(), \
             mock.patch.object(self.agent_module, "_phemex_fetch_ticker",
                               side_effect=_fetch_ticker_stub(tickers_ok)):
            self.agent_module.execute(
                {"operation": "get_tickers", "account": "amiroo"})

        # Second refresh: ETHUSDT fails (still in catalog); others succeed.
        tickers_partial = {
            "BTCUSDT": _make_ticker("BTCUSDT", markPriceRp="84700.0"),
            "SOLUSDT": _make_ticker("SOLUSDT", markPriceRp="151.0"),
        }
        def fail_eth(credentials, symbol):
            if symbol == "ETHUSDT":
                raise Exception("HTTP 503 service unavailable")
            return tickers_partial.get(symbol)
        with self._creds(), self._products(), \
             mock.patch.object(self.agent_module, "_phemex_fetch_ticker",
                               side_effect=fail_eth):
            response = self.agent_module.execute(
                {"operation": "get_tickers", "account": "amiroo", "force": True})
        rd = _resp_to_dict(response)
        tickers_dict = _tickers_dict(rd)
        # BTCUSDT and SOLUSDT refreshed.
        self.assertEqual(tickers_dict["BTCUSDT"]["mark_price"], "84700")
        self.assertEqual(tickers_dict["SOLUSDT"]["mark_price"], "151")
        # ETHUSDT retained from prior refresh (mark_price=2712.5).
        self.assertEqual(tickers_dict["ETHUSDT"]["mark_price"], "2712.5")
        # ETHUSDT listed in stale_symbols.
        self.assertIn("ETHUSDT", rd["tickers_batch"]["stale_symbols"])

    # ------------------------------------------------------------------
    # S. removed-product non-resurrection
    # ------------------------------------------------------------------

    def test_S_removed_product_not_resurrected(self):
        # First refresh: BTC, ETH, SOL present.
        tickers_all = {
            "BTCUSDT": _make_ticker("BTCUSDT"),
            "ETHUSDT": _make_ticker("ETHUSDT"),
            "SOLUSDT": _make_ticker("SOLUSDT"),
        }
        with self._creds(), self._products(), \
             mock.patch.object(self.agent_module, "_phemex_fetch_ticker",
                               side_effect=_fetch_ticker_stub(tickers_all)):
            self.agent_module.execute(
                {"operation": "get_tickers", "account": "amiroo"})

        # Second refresh: catalog drops ETHUSDT; only BTC + SOL returned.
        catalog_no_eth = {k: v for k, v in SAMPLE_CATALOG.items()
                          if k != "ETHUSDT"}
        tickers_partial = {
            "BTCUSDT": _make_ticker("BTCUSDT"),
            "SOLUSDT": _make_ticker("SOLUSDT"),
        }
        with self._creds(), \
             mock.patch.object(self.agent_module, "_load_products",
                               return_value=dict(catalog_no_eth)), \
             mock.patch.object(self.agent_module, "_phemex_fetch_ticker",
                               side_effect=_fetch_ticker_stub(tickers_partial)):
            response = self.agent_module.execute(
                {"operation": "get_tickers", "account": "amiroo"})
        rd = _resp_to_dict(response)
        tickers_dict = _tickers_dict(rd)
        self.assertIn("BTCUSDT", tickers_dict)
        self.assertIn("SOLUSDT", tickers_dict)
        # ETHUSDT was dropped from the catalog → NOT resurrected.
        self.assertNotIn("ETHUSDT", tickers_dict)

    # ------------------------------------------------------------------
    # T. TTL cache (60s warm window)
    # ------------------------------------------------------------------

    def test_T_warm_window_serves_cache(self):
        tickers = {"BTCUSDT": _make_ticker("BTCUSDT"),
                   "ETHUSDT": _make_ticker("ETHUSDT"),
                   "SOLUSDT": _make_ticker("SOLUSDT")}
        fetch_mock = mock.MagicMock(
            side_effect=_fetch_ticker_stub(tickers))
        with self._creds(), self._products(), \
             mock.patch.object(self.agent_module, "_phemex_fetch_ticker",
                               fetch_mock):
            r1 = self.agent_module.execute(
                {"operation": "get_tickers", "account": "amiroo"})
            call_count_after_cold = fetch_mock.call_count
            r2 = self.agent_module.execute(
                {"operation": "get_tickers", "account": "amiroo"})
            call_count_after_warm = fetch_mock.call_count
        # Warm window: zero new ticker calls.
        self.assertEqual(call_count_after_cold, call_count_after_warm)
        rd1 = _resp_to_dict(r1)
        rd2 = _resp_to_dict(r2)
        self.assertFalse(rd1["tickers_batch"]["served_from_cache"])
        self.assertTrue(rd2["tickers_batch"]["served_from_cache"])
        self.assertEqual(rd1["tickers_batch"]["refresh_status"], "ok")
        self.assertEqual(rd2["tickers_batch"]["refresh_status"], "ok")

    def test_T2_warm_window_drops_after_TTL_expires(self):
        tickers = {"BTCUSDT": _make_ticker("BTCUSDT"),
                   "ETHUSDT": _make_ticker("ETHUSDT"),
                   "SOLUSDT": _make_ticker("SOLUSDT")}
        fetch_mock = mock.MagicMock(
            side_effect=_fetch_ticker_stub(tickers))
        with self._creds(), self._products(), \
             mock.patch.object(self.agent_module, "_phemex_fetch_ticker",
                               fetch_mock):
            self.agent_module.execute(
                {"operation": "get_tickers", "account": "amiroo"})
            count_after_first = fetch_mock.call_count
            # Advance the clock past the 60s TTL.
            self._now_patcher.stop()
            with mock.patch.object(self.agent_module, "_phemex_now",
                                   return_value=1_000_000.0 + 61.0):
                self.agent_module.execute(
                    {"operation": "get_tickers", "account": "amiroo"})
            count_after_second = fetch_mock.call_count
        # After TTL, a fresh refresh DID run (3 additional calls).
        self.assertGreater(count_after_second, count_after_first)

    # ------------------------------------------------------------------
    # U. request coalescing
    # ------------------------------------------------------------------

    def test_U_concurrent_callers_coalesce(self):
        import threading
        tickers = {"BTCUSDT": _make_ticker("BTCUSDT"),
                   "ETHUSDT": _make_ticker("ETHUSDT"),
                   "SOLUSDT": _make_ticker("SOLUSDT")}
        fetch_mock = mock.MagicMock(
            side_effect=_fetch_ticker_stub(tickers))
        responses = []
        errors = []
        barrier = threading.Barrier(5)
        def caller():
            try:
                barrier.wait(timeout=10)
                r = self.agent_module.execute(
                    {"operation": "get_tickers", "account": "amiroo"})
                responses.append(r)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        with self._creds(), self._products(), \
             mock.patch.object(self.agent_module, "_phemex_fetch_ticker",
                               fetch_mock):
            threads = [threading.Thread(target=caller) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        self.assertFalse(errors, f"caller errors: {errors}")
        self.assertEqual(len(responses), 5)
        # 5 callers → 1 refresh → 3 fan-out calls (one per contract).
        self.assertEqual(fetch_mock.call_count, 3)

    # ------------------------------------------------------------------
    # V. concurrency cap <= 8
    # ------------------------------------------------------------------

    def test_V_concurrency_cap_le_8(self):
        # 24 fake contracts → fan-out must never have > 8 in flight.
        import threading as _threading
        big_catalog = {
            f"COIN{i:02d}USDT": _make_catalog(f"COIN{i:02d}USDT", f"COIN{i:02d}")
            for i in range(24)
        }
        big_tickers = {
            f"COIN{i:02d}USDT": _make_ticker(f"COIN{i:02d}USDT")
            for i in range(24)
        }
        in_flight = {"value": 0, "peak": 0}
        counter_lock = _threading.Lock()

        def slow_fetch(credentials, symbol):
            with counter_lock:
                in_flight["value"] += 1
                if in_flight["value"] > in_flight["peak"]:
                    in_flight["peak"] = in_flight["value"]
            import time as _t
            _t.sleep(0.05)
            try:
                return big_tickers.get(symbol)
            finally:
                with counter_lock:
                    in_flight["value"] -= 1

        with self._creds(), \
             mock.patch.object(self.agent_module, "_load_products",
                               return_value=dict(big_catalog)), \
             mock.patch.object(self.agent_module, "_phemex_fetch_ticker",
                               side_effect=slow_fetch):
            self.agent_module.execute(
                {"operation": "get_tickers", "account": "amiroo",
                 "force": True})
        # Peak in-flight must be <= 8.
        self.assertGreater(in_flight["peak"], 0,
                           "fan-out never ran (peak=0)")
        self.assertLessEqual(in_flight["peak"], 8,
                             f"peak {in_flight['peak']} > 8 (concurrency cap violated)")

    # ------------------------------------------------------------------
    # W. rate-limit detection
    # ------------------------------------------------------------------

    def test_W_rate_limit_detection_from_exception(self):
        real = self._real_rate_limit_sentinel.fn
        self.assertTrue(real(Exception("HTTP 429 Too Many Requests")))
        self.assertTrue(real(Exception("rate limit exceeded")))

    def test_W2_rate_limit_detection_from_payload(self):
        real = self._real_rate_limit_sentinel.fn
        self.assertTrue(real({"code": 429, "msg": "Too Many Requests"}))

    def test_W3_non_rate_limit_returns_False(self):
        real = self._real_rate_limit_sentinel.fn
        self.assertFalse(real(Exception("HTTP 503 service unavailable")))
        self.assertFalse(real(None))
        self.assertFalse(real({}))

    # ------------------------------------------------------------------
    # X. circuit/cooldown open on rate-limit
    # ------------------------------------------------------------------

    def test_X_rate_limit_opens_circuit(self):
        rl_exc = Exception("HTTP 429 rate limit exceeded")
        # Allow the worker's rate-limit detector to actually fire.
        self._rate_patcher.stop()
        try:
            with self._creds(), self._products(), \
                 mock.patch.object(self.agent_module, "_phemex_fetch_ticker",
                                   side_effect=_fetch_ticker_raising(rl_exc)):
                response = self.agent_module.execute(
                    {"operation": "get_tickers", "account": "amiroo"})
            # Circuit now open.
            circuit_until = self.agent_module._PHEMEX_GET_TICKERS_CIRCUIT_OPEN_UNTIL
            self.assertGreater(circuit_until, 1_000_000.0)
            rd = _resp_to_dict(response)
            self.assertEqual(rd["tickers_batch"]["refresh_status"],
                             "rate_limited")
        finally:
            self._rate_patcher = mock.patch.object(
                self.agent_module, "_phemex_is_rate_limited",
                return_value=False)
            self._rate_patcher.start()

    def test_X2_circuit_open_serves_stale_without_fan_out(self):
        # Populate cache via a successful refresh first.
        tickers = {"BTCUSDT": _make_ticker("BTCUSDT", markPriceRp="84680.5"),
                   "ETHUSDT": _make_ticker("ETHUSDT", markPriceRp="2712.5"),
                   "SOLUSDT": _make_ticker("SOLUSDT", markPriceRp="150")}
        with self._creds(), self._products(), \
             mock.patch.object(self.agent_module, "_phemex_fetch_ticker",
                               side_effect=_fetch_ticker_stub(tickers)):
            self.agent_module.execute(
                {"operation": "get_tickers", "account": "amiroo"})

        # Now open the circuit manually.
        rl_exc = Exception("HTTP 429 rate limit exceeded")
        fetch_mock = mock.MagicMock(side_effect=_fetch_ticker_raising(rl_exc))
        self._rate_patcher.stop()
        try:
            with self._creds(), self._products(), \
                 mock.patch.object(self.agent_module, "_phemex_fetch_ticker",
                                   fetch_mock), \
                 mock.patch.object(self.agent_module,
                                   "_PHEMEX_GET_TICKERS_CIRCUIT_OPEN_UNTIL",
                                   9_999_999_999.0):
                response = self.agent_module.execute(
                    {"operation": "get_tickers", "account": "amiroo"})
            rd = _resp_to_dict(response)
            # Served from cache, no fan-out.
            self.assertEqual(fetch_mock.call_count, 0)
            self.assertTrue(rd["tickers_batch"]["served_from_cache"])
            self.assertEqual(rd["tickers_batch"]["refresh_status"],
                             "circuit_open")
            # BTCUSDT retained its prior mark_price.
            self.assertEqual(_tickers_dict(rd)["BTCUSDT"]["mark_price"],
                             "84680.5")
        finally:
            self._rate_patcher = mock.patch.object(
                self.agent_module, "_phemex_is_rate_limited",
                return_value=False)
            self._rate_patcher.start()

    # ------------------------------------------------------------------
    # Y. warm request = zero upstream ticker calls
    # ------------------------------------------------------------------

    def test_Y_warm_request_zero_upstream(self):
        tickers = {"BTCUSDT": _make_ticker("BTCUSDT"),
                   "ETHUSDT": _make_ticker("ETHUSDT"),
                   "SOLUSDT": _make_ticker("SOLUSDT")}
        fetch_mock = mock.MagicMock(
            side_effect=_fetch_ticker_stub(tickers))
        with self._creds(), self._products(), \
             mock.patch.object(self.agent_module, "_phemex_fetch_ticker",
                               fetch_mock):
            self.agent_module.execute(
                {"operation": "get_tickers", "account": "amiroo"})
            cold_count = fetch_mock.call_count
            # Three more warm requests.
            for _ in range(3):
                self.agent_module.execute(
                    {"operation": "get_tickers", "account": "amiroo"})
            warm_count = fetch_mock.call_count
        self.assertEqual(cold_count, warm_count)
        self.assertGreater(cold_count, 0)  # sanity: cold actually ran

    # ------------------------------------------------------------------
    # Z. WebTrade2 contains zero Phemex-specific acquisition code
    # ------------------------------------------------------------------

    def test_Z_webtrade2_has_no_phemex_acquisition_branches(self):
        wt2_dir = REPO / "plugins" / "trade" / "webtrade2"
        offenders = []
        if wt2_dir.exists():
            for path in wt2_dir.rglob("*.py"):
                txt = path.read_text(errors="replace")
                lines = txt.splitlines()
                for i, line in enumerate(lines, 1):
                    low = line.lower()
                    # The presence of any direct Phemex API endpoint OR a
                    # branch on exchange == "phemex" inside webtrade2 is a
                    # violation. Whitelist: comments / docstrings explicitly
                    # mentioning phemex in agent-routing contexts are fine.
                    if "/md/v2/ticker/24hr" in low or "phemex_md_v2" in low:
                        offenders.append((str(path), i, line.strip()))
                    if "exchange" in low and "phemex" in low:
                        # Allow comments but flag branches.
                        stripped = line.strip()
                        if (stripped.startswith("if ")
                                or stripped.startswith("elif ")
                                or stripped.startswith("== ")
                                or "==" in stripped and "phemex" in stripped):
                            offenders.append((str(path), i, line.strip()))
        self.assertEqual(offenders, [],
                         f"WebTrade2 Phemex-specific acquisition code: "
                         f"{offenders}")

    # ------------------------------------------------------------------
    # AA. canonical.py unchanged
    # ------------------------------------------------------------------

    def test_AA_canonical_py_unchanged(self):
        canonical = REPO / "plugins" / "trade" / "canonical.py"
        result = subprocess.run(
            ["git", "diff", "--", "plugins/trade/canonical.py"],
            cwd=str(REPO), capture_output=True, text=True, check=True,
        )
        self.assertEqual(result.stdout, "",
                         "canonical.py was modified; Batch 4c forbids it.")

    # ------------------------------------------------------------------
    # Bonus: dispatcher without credentials returns ACCOUNT_NOT_FOUND
    # ------------------------------------------------------------------

    def test_BA_no_credentials(self):
        with mock.patch.object(self.agent_module, "_lookup_credentials",
                               return_value=None):
            response = self.agent_module.execute(
                {"operation": "get_tickers", "account": "missing"})
        rd = _resp_to_dict(response)
        self.assertFalse(rd["success"])
        self.assertEqual(rd["error"]["code"], "ACCOUNT_NOT_FOUND")


def _peak_inflight_helper(agent_module):
    """Return the in-flight count for the Semaphore in fan-out.

    Kept as a stub for backwards-compatibility with any external callers;
    Batch 4c's test_V uses a per-call counter instead.
    """
    return 0
