"""RED-first offline tests for canonical ``get_tickers`` on MEXC (Batch 4a).

Coverage mirrors Batch 1/2/3 taxonomy:

A. capabilities advertises ``get_tickers`` (additive; nothing removed)
B. dispatcher routes ``operation == "get_tickers"``
C. response shape uses CanonicalTickersBatch (tickers dict + metadata)
D. exactly ONE bulk ticker request per refresh; NO per-symbol fan-out
E. symbol-join uses venue-native contract symbol
F. quote-currency preservation (USDT / USDC / USD / USD1 stay distinct)
G. canonical field mapping per approved spec
H. missing/zero/invalid dynamic values become ``None`` but preserve catalog rows
I. ranking semantics unchanged; ``volume_24h_base`` is NEVER a ranking fallback
J. existing ``list_instruments`` and ``market_price`` behavior is unchanged
K. WebTrade2 generic routing works with zero MEXC-specific branches
L. safety: zero ``/api/trade/execute`` calls
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

REPO = Path("/root/kam")
os.environ.setdefault("HERMES_HOME", "/root/.hermes")


def _imp(name: str):
    return __import__(f"plugins.trade.agents.{name}", fromlist=["*"])


FAKE_CREDENTIALS = {
    "account": "default",
    "access_key": "fake_access_key",
    "secret_key": "fake_secret_key",
    "contract_base": "https://contract.mexc.com",
}


def _make_meta(symbol, base, quote, contract_size="0.0001",
               vol_unit="1", price_unit="0.1", min_vol="1"):
    from decimal import Decimal
    return {
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "contract_size": Decimal(contract_size),
        "vol_unit": Decimal(vol_unit),
        "price_unit": Decimal(price_unit),
        "min_vol": Decimal(min_vol),
        "display": f"{base}/{quote}",
    }


def _bulk_ticker_row(symbol, **overrides):
    base = {
        "contractId": 10,
        "symbol": symbol,
        "lastPrice": "84680",
        "bid1": "84679.9",
        "ask1": "84680",
        "volume24": 383507569,
        "amount24": 3231038549.81834,
        "holdVol": 553033453,
        "lower24Price": 83301.4,
        "high24Price": 85223.4,
        "riseFallRate": 0.0037,
        "riseFallValue": 313.2,
        "indexPrice": "84715.8",
        "fairPrice": "84680",
        "fundingRate": "-6e-06",
        "maxBidPrice": "93187.3",
        "minAskPrice": "76244.2",
        "timestamp": 1790336692829,
    }
    base.update(overrides)
    return base


def _resp_to_dict(response):
    if hasattr(response, "to_dict"):
        return response.to_dict()
    return response


def _tickers_dict(rd):
    if "tickers_batch" in rd and rd["tickers_batch"]:
        return rd["tickers_batch"].get("tickers", {})
    if "data" in rd and isinstance(rd["data"], dict):
        d = rd["data"]
        if "tickers" in d:
            return d["tickers"]
    return {}


def _batch_meta(rd):
    if "tickers_batch" in rd and rd["tickers_batch"]:
        return rd["tickers_batch"]
    if "data" in rd and isinstance(rd["data"], dict):
        return rd["data"]
    return {}


class TestMexcGetTickersOffline(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.agent_module = _imp("x_mexc_agent")

    def _creds(self):
        return mock.patch.object(self.agent_module, "_lookup_credentials",
                                 return_value=dict(FAKE_CREDENTIALS))

    def test_A_capabilities_advertise_get_tickers(self):
        caps = list(self.agent_module.capabilities())
        self.assertIn("get_tickers", caps)
        for must in ("list_instruments", "market_price", "new_order",
                     "ladder", "resolve_instrument", "balance",
                     "candles", "positions_orders", "positions_management"):
            self.assertIn(must, caps)

    def test_B_get_tickers_dispatch_wired(self):
        with self._creds(), \
             mock.patch.object(self.agent_module, "_ensure_contracts",
                               return_value=({}, {})), \
             mock.patch.object(self.agent_module, "_fetch_ticker_bulk",
                               return_value=[]), \
             mock.patch.object(self.agent_module, "_now_iso",
                               return_value="1970-01-01T00:00:00+00:00"):
            response = self.agent_module.execute(
                {"operation": "get_tickers", "exchange": "mexc", "account": "default"}
            )
        rd = _resp_to_dict(response)
        self.assertTrue(rd.get("success"))

    def test_C_response_shape_matches_canonical(self):
        meta_btc = _make_meta("BTC_USDT", "BTC", "USDT")
        meta_eth = _make_meta("ETH_USDT", "ETH", "USDT")
        cat_by_base = {"BTC": meta_btc, "ETH": meta_eth}
        cat_by_symbol = {m["symbol"]: m for m in cat_by_base.values()}
        ticker_rows = [
            _bulk_ticker_row("BTC_USDT"),
            _bulk_ticker_row("ETH_USDT", lastPrice="3200", fairPrice="3200",
                              indexPrice="3201", amount24="100000",
                              volume24="30", holdVol="1000", riseFallRate="0.01"),
        ]
        with self._creds(), \
             mock.patch.object(self.agent_module, "_ensure_contracts",
                               return_value=(cat_by_symbol, cat_by_base)), \
             mock.patch.object(self.agent_module, "_fetch_ticker_bulk",
                               return_value=ticker_rows), \
             mock.patch.object(self.agent_module, "_now_iso",
                               return_value="1970-01-01T00:00:00+00:00"):
            response = self.agent_module.execute(
                {"operation": "get_tickers", "exchange": "mexc", "account": "default"}
            )
        rd = _resp_to_dict(response)
        self.assertTrue(rd["success"], rd)
        tickers = _tickers_dict(rd)
        self.assertEqual(len(tickers), 2)
        meta = _batch_meta(rd)
        self.assertEqual(meta.get("source"), "mexc_contract_ticker_bulk")
        self.assertEqual(meta.get("refresh_status"), "ok")
        self.assertEqual(meta.get("ttl_seconds"), 30)
        self.assertIs(meta.get("served_from_cache"), False)

    def test_D_one_bulk_request_no_fan_out(self):
        cat_by_base = {
            "BTC": _make_meta("BTC_USDT", "BTC", "USDT"),
            "ETH": _make_meta("ETH_USDT", "ETH", "USDT"),
            "SOL": _make_meta("SOL_USDT", "SOL", "USDT"),
        }
        cat_by_symbol = {m["symbol"]: m for m in cat_by_base.values()}
        ticker_rows = [_bulk_ticker_row("BTC_USDT"), _bulk_ticker_row("ETH_USDT"),
                       _bulk_ticker_row("SOL_USDT")]
        with self._creds(), \
             mock.patch.object(self.agent_module, "_ensure_contracts",
                               return_value=(cat_by_symbol, cat_by_base)), \
             mock.patch.object(self.agent_module, "_fetch_ticker_bulk",
                               return_value=ticker_rows) as bulk_mock, \
             mock.patch.object(self.agent_module, "_fetch_ticker",
                               side_effect=AssertionError(
                                   "per-symbol fan-out must NOT be used")) as fanout, \
             mock.patch.object(self.agent_module, "_now_iso",
                               return_value="1970-01-01T00:00:00+00:00"):
            self.agent_module.execute(
                {"operation": "get_tickers", "exchange": "mexc", "account": "default"}
            )
        self.assertEqual(bulk_mock.call_count, 1,
                         "exactly one bulk ticker request per refresh")
        fanout.assert_not_called()

    def test_E_native_symbol_join(self):
        meta_btc = _make_meta("BTC_USDT", "BTC", "USDT")
        cat_by_base = {"BTC": meta_btc}
        cat_by_symbol = {"BTC_USDT": meta_btc}
        ticker_rows = [_bulk_ticker_row("BTC_USDT")]
        with self._creds(), \
             mock.patch.object(self.agent_module, "_ensure_contracts",
                               return_value=(cat_by_symbol, cat_by_base)), \
             mock.patch.object(self.agent_module, "_fetch_ticker_bulk",
                               return_value=ticker_rows), \
             mock.patch.object(self.agent_module, "_now_iso",
                               return_value="1970-01-01T00:00:00+00:00"):
            response = self.agent_module.execute(
                {"operation": "get_tickers", "exchange": "mexc", "account": "default"}
            )
        rd = _resp_to_dict(response)
        market = _tickers_dict(rd)["BTC_USDT"]
        self.assertEqual(market["symbol"], "BTC_USDT")
        self.assertEqual(market["native_symbol"], "BTC_USDT")
        self.assertEqual(market["base"], "BTC")
        self.assertEqual(market["quote"], "USDT")
        self.assertEqual(market["market_type"], "perp")

    def test_F_mixed_quote_currencies_remain_distinct(self):
        cat_by_base = {
            "BTC": _make_meta("BTC_USDT", "BTC", "USDT"),
            "ETH": _make_meta("ETH_USDC", "ETH", "USDC"),
            "SOL": _make_meta("SOL_USD", "SOL", "USD"),
            "XRP": _make_meta("XRP_USD1", "XRP", "USD1"),
        }
        cat_by_symbol = {m["symbol"]: m for m in cat_by_base.values()}
        ticker_rows = [_bulk_ticker_row("BTC_USDT"), _bulk_ticker_row("ETH_USDC"),
                       _bulk_ticker_row("SOL_USD"), _bulk_ticker_row("XRP_USD1")]
        with self._creds(), \
             mock.patch.object(self.agent_module, "_ensure_contracts",
                               return_value=(cat_by_symbol, cat_by_base)), \
             mock.patch.object(self.agent_module, "_fetch_ticker_bulk",
                               return_value=ticker_rows), \
             mock.patch.object(self.agent_module, "_now_iso",
                               return_value="1970-01-01T00:00:00+00:00"):
            response = self.agent_module.execute(
                {"operation": "get_tickers", "exchange": "mexc", "account": "default"}
            )
        rd = _resp_to_dict(response)
        tickers = _tickers_dict(rd)
        quotes = {m["quote"] for m in tickers.values()}
        self.assertSetEqual(quotes, {"USDT", "USDC", "USD", "USD1"})

    def test_G_canonical_field_mapping(self):
        meta = _make_meta("BTC_USDT", "BTC", "USDT",
                          contract_size="0.0001", vol_unit="1",
                          price_unit="0.1", min_vol="1")
        cat_by_base = {"BTC": meta}
        cat_by_symbol = {"BTC_USDT": meta}
        ticker_row = _bulk_ticker_row(
            "BTC_USDT",
            lastPrice="84680", fairPrice="84680", indexPrice="84715.8",
            amount24="3231038549.81834", volume24="383507569",
            holdVol="553033453", fundingRate="-0.000006",
            riseFallRate="0.0037",
        )
        with self._creds(), \
             mock.patch.object(self.agent_module, "_ensure_contracts",
                               return_value=(cat_by_symbol, cat_by_base)), \
             mock.patch.object(self.agent_module, "_fetch_ticker_bulk",
                               return_value=[ticker_row]), \
             mock.patch.object(self.agent_module, "_now_iso",
                               return_value="1970-01-01T00:00:00+00:00"):
            response = self.agent_module.execute(
                {"operation": "get_tickers", "exchange": "mexc", "account": "default"}
            )
        rd = _resp_to_dict(response)
        market = _tickers_dict(rd)["BTC_USDT"]
        self.assertEqual(market["mark_price"], "84680")
        self.assertEqual(market["price"], "84680")
        self.assertEqual(market["oracle_price"], "84715.8")
        self.assertEqual(market["last_external_price"], "84680")
        self.assertEqual(market["price_increment"], "0.1")
        self.assertEqual(market["size_increment"], "0.0001")
        self.assertEqual(market["minimum_size"], "0.0001")
        self.assertEqual(market["funding_rate"], "-0.000006")
        self.assertEqual(market["open_interest"], "553033453")
        self.assertEqual(market["turnover_24h"], "3231038549.81834")
        self.assertEqual(market["volume_24h_quote"], "3231038549.81834")
        self.assertEqual(market["volume_24h_base"], "383507569")
        self.assertEqual(market["change_24h_pct"], "0.37")

    def test_H_missing_dynamic_values_become_none(self):
        meta = _make_meta("BTC_USDT", "BTC", "USDT")
        cat_by_base = {"BTC": meta}
        cat_by_symbol = {"BTC_USDT": meta}
        ticker_row = {"symbol": "BTC_USDT", "lastPrice": "0", "fairPrice": "0",
                       "indexPrice": None, "amount24": 0, "volume24": 0,
                       "holdVol": 0, "fundingRate": "0", "riseFallRate": "0"}
        with self._creds(), \
             mock.patch.object(self.agent_module, "_ensure_contracts",
                               return_value=(cat_by_symbol, cat_by_base)), \
             mock.patch.object(self.agent_module, "_fetch_ticker_bulk",
                               return_value=[ticker_row]), \
             mock.patch.object(self.agent_module, "_now_iso",
                               return_value="1970-01-01T00:00:00+00:00"):
            response = self.agent_module.execute(
                {"operation": "get_tickers", "exchange": "mexc", "account": "default"}
            )
        rd = _resp_to_dict(response)
        tickers = _tickers_dict(rd)
        self.assertEqual(len(tickers), 1)
        market = tickers["BTC_USDT"]
        self.assertEqual(market["symbol"], "BTC_USDT")
        self.assertIsNone(market["mark_price"])
        self.assertIsNone(market["price"])
        self.assertIsNone(market["oracle_price"])
        self.assertIsNone(market["last_external_price"])
        self.assertIsNone(market["funding_rate"])
        self.assertIsNone(market["open_interest"])
        self.assertIsNone(market["turnover_24h"])
        self.assertIsNone(market["volume_24h_quote"])
        self.assertIsNone(market["volume_24h_base"])
        self.assertIsNone(market["change_24h_pct"])

    def test_H2_catalog_row_preserved_when_ticker_row_missing(self):
        meta = _make_meta("BTC_USDT", "BTC", "USDT")
        cat_by_base = {"BTC": meta}
        cat_by_symbol = {"BTC_USDT": meta}
        with self._creds(), \
             mock.patch.object(self.agent_module, "_ensure_contracts",
                               return_value=(cat_by_symbol, cat_by_base)), \
             mock.patch.object(self.agent_module, "_fetch_ticker_bulk",
                               return_value=[]), \
             mock.patch.object(self.agent_module, "_now_iso",
                               return_value="1970-01-01T00:00:00+00:00"):
            response = self.agent_module.execute(
                {"operation": "get_tickers", "exchange": "mexc", "account": "default"}
            )
        rd = _resp_to_dict(response)
        tickers = _tickers_dict(rd)
        self.assertEqual(len(tickers), 1)
        market = tickers["BTC_USDT"]
        self.assertEqual(market["symbol"], "BTC_USDT")
        self.assertIsNone(market["mark_price"])
        self.assertIsNone(market["turnover_24h"])

    def test_I_ranking_uses_quote_turnover_not_base(self):
        meta = _make_meta("X_USDT", "X", "USDT")
        cat_by_base = {"X": meta}
        cat_by_symbol = {"X_USDT": meta}
        ticker_row = {"symbol": "X_USDT", "fairPrice": "100", "indexPrice": "100",
                       "lastPrice": "100", "amount24": 0, "volume24": 999999,
                       "holdVol": 0, "fundingRate": "0", "riseFallRate": "0"}
        with self._creds(), \
             mock.patch.object(self.agent_module, "_ensure_contracts",
                               return_value=(cat_by_symbol, cat_by_base)), \
             mock.patch.object(self.agent_module, "_fetch_ticker_bulk",
                               return_value=[ticker_row]), \
             mock.patch.object(self.agent_module, "_now_iso",
                               return_value="1970-01-01T00:00:00+00:00"):
            response = self.agent_module.execute(
                {"operation": "get_tickers", "exchange": "mexc", "account": "default"}
            )
        rd = _resp_to_dict(response)
        market = _tickers_dict(rd)["X_USDT"]
        self.assertIsNone(market["turnover_24h"])
        self.assertEqual(market["volume_24h_base"], "999999")

    def test_J_list_instruments_path_unchanged(self):
        meta = _make_meta("BTC_USDT", "BTC", "USDT")
        with self._creds(), \
             mock.patch.object(self.agent_module, "_ensure_contracts",
                               return_value=({"BTC": meta}, {"BTC": meta})), \
             mock.patch.object(self.agent_module, "_fetch_ticker_bulk",
                               side_effect=AssertionError(
                                   "list_instruments must not trigger bulk ticker")):
            response = self.agent_module.execute(
                {"operation": "list_instruments", "exchange": "mexc", "account": "default"}
            )
        rd = _resp_to_dict(response)
        self.assertTrue(rd["success"], rd)

    def test_J2_market_price_path_unchanged(self):
        meta = _make_meta("BTC_USDT", "BTC", "USDT")
        with self._creds(), \
             mock.patch.object(self.agent_module, "_ensure_contracts",
                               return_value=({"BTC_USDT": meta}, {"BTC": meta})), \
             mock.patch.object(self.agent_module, "_resolve_meta",
                               return_value=dict(meta)), \
             mock.patch.object(self.agent_module, "_fetch_ticker",
                               return_value={"lastPrice": "84680", "fairPrice": "84680",
                                              "indexPrice": "84715.8"}) as per_sym, \
             mock.patch.object(self.agent_module, "_fetch_ticker_bulk",
                               side_effect=AssertionError(
                                   "market_price must not trigger bulk ticker")) as bulk:
            response = self.agent_module.execute(
                {"operation": "market_price", "exchange": "mexc",
                 "account": "default", "symbol": "BTC_USDT"}
            )
        rd = _resp_to_dict(response)
        self.assertTrue(rd["success"], rd)
        self.assertEqual(per_sym.call_count, 1)
        bulk.assert_not_called()

    def test_K_webtrade2_generic_routing(self):
        meta = _make_meta("BTC_USDT", "BTC", "USDT")
        cat_by_base = {"BTC": meta}
        cat_by_symbol = {"BTC_USDT": meta}
        ticker_rows = [_bulk_ticker_row("BTC_USDT")]
        with self._creds(), \
             mock.patch.object(self.agent_module, "_ensure_contracts",
                               return_value=(cat_by_symbol, cat_by_base)), \
             mock.patch.object(self.agent_module, "_fetch_ticker_bulk",
                               return_value=ticker_rows), \
             mock.patch.object(self.agent_module, "_now_iso",
                               return_value="1970-01-01T00:00:00+00:00"):
            response = self.agent_module.execute(
                {"operation": "get_tickers", "exchange": "mexc", "account": "default"}
            )
        rd = _resp_to_dict(response)
        self.assertTrue(rd["success"], rd)
        self.assertIn("tickers_batch", rd)
        tb = rd["tickers_batch"]
        self.assertIn("tickers", tb)
        self.assertIn("BTC_USDT", tb["tickers"])
        self.assertEqual(tb["source"], "mexc_contract_ticker_bulk")

    def test_L_no_trade_execute_calls(self):
        src = (REPO / "plugins/trade/agents/x_mexc_agent.py").read_text()
        self.assertNotIn("/api/trade/execute", src)


class TestWebTrade2ZeroMexcAcquisitionLogic(unittest.TestCase):

    def test_K2_webtrade2_no_mexc_acquisition_branches(self):
        webtrade2 = REPO / "plugins/trade/webtrade2"
        if not webtrade2.exists():
            self.skipTest("no webtrade2 plugin")
        bad_substrings = (
            "/api/v1/contract/ticker",
            "/api/v1/contract/detail",
            "mexc_contract_ticker_bulk",
        )
        offenders = []
        for p in webtrade2.rglob("*.py"):
            txt = p.read_text()
            for sub in bad_substrings:
                if sub in txt:
                    offenders.append(f"{p}: {sub}")
        self.assertEqual(offenders, [],
                         f"WebTrade2 must not contain MEXC-specific acquisition "
                         f"branches; offenders: {offenders}")

    def test_K3_webtrade2_source_grep_mexc(self):
        webtrade2 = REPO / "plugins/trade/webtrade2"
        if not webtrade2.exists():
            self.skipTest("no webtrade2 plugin")
        import re as _re
        offenders = []
        for p in webtrade2.rglob("*.py"):
            txt = p.read_text()
            for pat in (r"exchange\s*==\s*['\"]mexc['\"]",
                        r"exchange\.lower\(\)\s*==\s*['\"]mexc['\"]"):
                for m in _re.finditer(pat, txt):
                    offenders.append(f"{p}: {pat!r}: {m.group(0)}")
        self.assertEqual(offenders, [],
                         f"WebTrade2 must not have MEXC-specific market-data "
                         f"branches; offenders: {offenders}")


class TestCanonicalUnchanged(unittest.TestCase):
    def test_M_canonical_mtime_unchanged(self):
        r = subprocess.run(
            ["git", "status", "--short", "--", "plugins/trade/canonical.py"],
            capture_output=True, text=True, cwd=REPO,
        )
        self.assertEqual(r.stdout.strip(), "",
                         "canonical.py must not be modified by Batch 4a")


if __name__ == "__main__":
    unittest.main()
