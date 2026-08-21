"""Canonical instrument resolution: Apex + Raydium (offline)."""

from __future__ import annotations

import os
import unittest
from decimal import Decimal
from typing import Any, Dict, List
from unittest import mock

from plugins.trade.agents import x_apex_agent as apex
from plugins.trade.agents import x_raydium_agent as ray


def _apex_contract(symbol: str) -> Dict[str, Any]:
    return {
        "id": symbol,
        "symbol": symbol,
        "symbolDisplayName": symbol.replace("-", ""),
        "tickSize": "0.1",
        "stepSize": "0.01",
        "minOrderSize": "0.01",
        "minOrderNotional": "1",
        "underlyingDecimals": 4,
        "settlementDecimals": 4,
        "live": True,
    }


def _apex_dup_contract(id_, sym, display) -> Dict[str, Any]:
    return {
        "id": id_,
        "symbol": sym,
        "symbolDisplayName": display,
        "tickSize": "0.1",
        "stepSize": "0.01",
        "minOrderSize": "0.01",
        "minOrderNotional": "1",
        "underlyingDecimals": 4,
        "settlementDecimals": 4,
        "live": True,
    }


def _apex_catalog_unique() -> List[Dict[str, Any]]:
    return [_apex_contract("BTC-USDT")]


def _apex_catalog_dup() -> List[Dict[str, Any]]:
    return [
        _apex_dup_contract("2", "BTC-USDT", "BTCUSDT"),
        _apex_dup_contract("22", "BTC-USDC", "BTCUSDC"),
    ]


class _ApexResolveBase(unittest.TestCase):
    def setUp(self) -> None:
        for k in list(os.environ):
            if k.startswith("APEX_"):
                os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k in list(os.environ):
            if k.startswith("APEX_"):
                os.environ.pop(k, None)


class TestApexCanonicalResolution(_ApexResolveBase):
    def test_aliases_share_market(self) -> None:
        catalog = _apex_catalog_unique()
        canonicals = []
        for raw in ("BTC", "BTC-USDT", "BTCUSDT"):
            meta = apex._apex_resolve_symbol(raw, catalog)
            self.assertIsNotNone(meta)
            canonicals.append(meta["symbol"])
        self.assertEqual(set(canonicals), {"BTC-USDT"})

    def test_unknown_returns_none(self) -> None:
        self.assertIsNone(apex._apex_resolve_symbol("NOTAMARKET", _apex_catalog_unique()))
        self.assertIsNone(apex._apex_resolve_symbol("", _apex_catalog_unique()))

    def test_duplicate_candidates_is_ambiguous(self) -> None:
        catalog = _apex_catalog_dup()
        with self.assertRaises(ValueError) as ctx:
            apex._apex_resolve_symbol("BTC", catalog)
        self.assertEqual(str(ctx.exception), "INSTRUMENT_AMBIGUOUS")
        # Bare alias BTCUSDT also matches both contracts' display names.
        with self.assertRaises(ValueError) as ctx:
            apex._apex_resolve_symbol("BTCUSDT", catalog)
        self.assertEqual(str(ctx.exception), "INSTRUMENT_AMBIGUOUS")
        self.assertIsNotNone(apex._apex_resolve_symbol("BTC-USDT", catalog))
        self.assertIsNotNone(apex._apex_resolve_symbol("BTC-USDC", catalog))

    def test_resolve_instrument_op(self) -> None:
        with mock.patch.object(apex, "_apex_fetch_supported_markets", return_value=_apex_catalog_unique()):
            r = apex.execute({"operation": "resolve_instrument", "account": "BITGET", "symbol": "BTC"})
        self.assertTrue(r.success)
        self.assertEqual(r.instrument.symbol, "BTC-USDT")
        self.assertEqual(r.instrument.display_name, "BTCUSDT")

    def test_resolve_instrument_unknown(self) -> None:
        with mock.patch.object(apex, "_apex_fetch_supported_markets", return_value=_apex_catalog_unique()):
            r = apex.execute({"operation": "resolve_instrument", "account": "BITGET", "symbol": "NOTAMARKET"})
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "INSTRUMENT_NOT_FOUND")

    def test_resolve_instrument_ambiguous(self) -> None:
        with mock.patch.object(apex, "_apex_fetch_supported_markets", return_value=_apex_catalog_dup()):
            r = apex.execute({"operation": "resolve_instrument", "account": "BITGET", "symbol": "BTC"})
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "INSTRUMENT_AMBIGUOUS")

    def test_position_fetch_failure_is_explicit(self) -> None:
        client = mock.Mock()
        client.get_account_v3.side_effect = RuntimeError("boom")
        with self.assertRaises(Exception):
            apex._apex_fetch_positions(client)

    def test_close_unknown_is_instrument_not_found(self) -> None:
        client = mock.Mock()
        client.configV3 = {"contractConfig": {"perpetualContract": _apex_catalog_unique()}}
        client.accountV3 = {"positions": []}
        client.get_account_v3.return_value = {}
        client.configs_v3.return_value = {}
        client.ticker_v3.return_value = {"data": []}
        with mock.patch.object(apex, "_client_for_credentials", return_value=client), \
             mock.patch.object(apex, "_apex_fetch_positions", return_value=[]):
            r = apex.execute({"operation": "close_position", "account": "BITGET", "symbol": "NOTAMARKET"})
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "INSTRUMENT_NOT_FOUND")

    def test_close_fetch_exception_is_positions_unavailable(self) -> None:
        client = mock.Mock()
        client.configV3 = {"contractConfig": {"perpetualContract": _apex_catalog_unique()}}
        client.get_account_v3.return_value = {}
        client.configs_v3.return_value = {}
        client.ticker_v3.return_value = {"data": []}
        with mock.patch.object(apex, "_client_for_credentials", return_value=client), \
             mock.patch.object(apex, "_apex_fetch_positions", side_effect=RuntimeError("boom")):
            r = apex.execute({"operation": "close_position", "account": "BITGET", "symbol": "BTC"})
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "POSITIONS_UNAVAILABLE")

    def test_close_resolved_empty_positions_is_no_open_position(self) -> None:
        client = mock.Mock()
        client.configV3 = {"contractConfig": {"perpetualContract": _apex_catalog_unique()}}
        client.get_account_v3.return_value = {}
        client.configs_v3.return_value = {}
        client.ticker_v3.return_value = {"data": []}
        with mock.patch.object(apex, "_client_for_credentials", return_value=client), \
             mock.patch.object(apex, "_apex_fetch_positions", return_value=[]):
            r = apex.execute({"operation": "close_position", "account": "BITGET", "symbol": "BTC"})
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "NO_OPEN_POSITION")

    def test_cancel_unknown_is_instrument_not_found(self) -> None:
        client = mock.Mock()
        client.configV3 = {"contractConfig": {"perpetualContract": _apex_catalog_unique()}}
        client.get_account_v3.return_value = {}
        client.configs_v3.return_value = {}
        with mock.patch.object(apex, "_client_for_credentials", return_value=client), \
             mock.patch.object(apex, "_extract_open_orders", return_value=[]):
            r = apex.execute({"operation": "cancel_order_group", "account": "BITGET", "symbol": "NOTAMARKET", "side": "sell"})
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "INSTRUMENT_NOT_FOUND")

    def test_supported_catalog_includes_stock(self) -> None:
        catalog = _apex_catalog_unique() + [
            {
                "id": "101",
                "symbol": "AAPLX-USDT",
                "symbolDisplayName": "AAPLXUSDT",
                "tickSize": "0.01",
                "stepSize": "0.001",
                "minOrderSize": "0.001",
                "minOrderNotional": "1",
                "underlyingDecimals": 3,
                "settlementDecimals": 4,
                "live": True,
            }
        ]
        with mock.patch.object(apex, "_apex_fetch_supported_markets", return_value=catalog):
            r = apex.execute({"operation": "resolve_instrument", "account": "BITGET", "symbol": "AAPLX"})
        self.assertTrue(r.success)
        self.assertEqual(r.instrument.symbol, "AAPLX-USDT")


def _ray_position(symbol: str = "PERP_SOL_USDC", side: str = "LONG", size: str = "2") -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "position_qty": size if side == "LONG" else f"-{size}",
        "average_open_price": "123.45",
        "unsettled_pnl": "0",
    }


class TestRaydiumCanonicalResolution(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_hermes = os.environ.get("HERMES_HOME")
        self._saved_ray = {k: v for k, v in os.environ.items() if k.startswith("RAYDIUM_")}
        for k in list(os.environ):
            if k.startswith("RAYDIUM_"):
                os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k in list(os.environ):
            if k.startswith("RAYDIUM_"):
                os.environ.pop(k, None)
        os.environ.update(self._saved_ray)
        if self._saved_hermes is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._saved_hermes

    def _ray_meta(self, orderly: str, display: str) -> Dict[str, Any]:
        return {
            "symbol": orderly,
            "display_symbol": display,
            "quote_tick": "0.01",
            "base_tick": "0.001",
            "base_min": "0.01",
            "min_notional": "1",
            "mark_price": "100",
        }

    def test_resolve_instrument_op(self) -> None:
        with mock.patch.object(ray, "_resolve_symbol_metadata",
                               return_value=self._ray_meta("PERP_SOL_USDC", "SOL")):
            r = ray.execute({"operation": "resolve_instrument", "account": "phantom", "symbol": "SOL"})
        self.assertTrue(r.success)
        self.assertEqual(r.instrument.symbol, "PERP_SOL_USDC")
        self.assertEqual(r.instrument.display_name, "SOL")

    def test_aliases_share_market(self) -> None:
        cids = []
        for raw in ("SOL", "PERP_SOL_USDC"):
            with mock.patch.object(ray, "_resolve_symbol_metadata",
                                   return_value=self._ray_meta("PERP_SOL_USDC", "SOL")):
                r = ray.execute({"operation": "resolve_instrument", "account": "phantom", "symbol": raw})
                self.assertTrue(r.success)
                self.assertEqual(r.instrument.symbol, "PERP_SOL_USDC")
                cids.append(r.instrument.symbol)
        self.assertEqual(set(cids), {"PERP_SOL_USDC"})

    def test_unknown_returns_failure(self) -> None:
        def boom(_symbol):
            raise RuntimeError("Raydium symbol metadata was unavailable")
        with mock.patch.object(ray, "_resolve_symbol_metadata", side_effect=boom):
            r = ray.execute({"operation": "resolve_instrument", "account": "phantom", "symbol": "SOL-USDC"})
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "INSTRUMENT_NOT_FOUND")

    def test_unsupported_dash_alias_does_not_resolve(self) -> None:
        def boom(_symbol):
            raise RuntimeError("Raydium symbol metadata was unavailable")
        with mock.patch.object(ray, "_resolve_symbol_metadata", side_effect=boom):
            for raw in ("SOL-USDC", "SOL_USDC", "SOLUSDC"):
                r = ray.execute({"operation": "resolve_instrument", "account": "phantom", "symbol": raw})
                self.assertFalse(r.success)
                self.assertEqual(r.error.code, "INSTRUMENT_NOT_FOUND")

    def test_close_resolved_empty_positions_is_position_not_found(self) -> None:
        with mock.patch.object(ray, "_resolve_symbol_metadata",
                               return_value=self._ray_meta("PERP_SOL_USDC", "SOL")), \
             mock.patch.object(ray, "_find_current_position", return_value=None):
            r = ray.execute({"operation": "close_position", "account": "phantom", "symbol": "SOL"})
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "POSITION_NOT_FOUND")

    def test_close_unknown_is_instrument_not_found(self) -> None:
        def boom(_symbol):
            raise RuntimeError("Raydium symbol metadata was unavailable")
        with mock.patch.object(ray, "_resolve_symbol_metadata", side_effect=boom):
            r = ray.execute({"operation": "close_position", "account": "phantom", "symbol": "NOTAMARKET"})
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "INSTRUMENT_NOT_FOUND")

    def test_close_finds_position_for_canonical_alias(self) -> None:
        from plugins.trade.canonical import CanonicalPosition
        position = CanonicalPosition(symbol="SOL", side="long", size="2", entry_price="123", pnl="0")
        position_payload = {"data": {"rows": [{"symbol": "PERP_SOL_USDC", "position_qty": "2", "average_open_price": "123", "unsettled_pnl": "0"}]}}
        empty_payload = {"data": {"rows": []}}
        for raw in ("SOL", "PERP_SOL_USDC"):
            calls = {"n": 0}

            def fake_private_get(creds, path, params=None):
                calls["n"] += 1
                return position_payload if calls["n"] == 1 else empty_payload

            with mock.patch.object(ray, "_resolve_symbol_metadata",
                                   return_value=self._ray_meta("PERP_SOL_USDC", "SOL")), \
                 mock.patch.object(ray, "_private_get", side_effect=fake_private_get), \
                 mock.patch.object(ray, "_fetch_symbol_rules", return_value={"PERP_SOL_USDC": {"symbol": "SOL", "price_precision": 2, "size_precision": 3}}), \
                 mock.patch.object(ray, "_submit_order", return_value={"submitted_price": "1", "submitted_volume": "2", "exchange_order_id": "7"}), \
                 mock.patch.object(ray, "_lookup_credentials", return_value={"account": "phantom", "account_id": "1", "api_key": "k", "secret_key": "s"}):
                r = ray.execute({"operation": "close_position", "account": "phantom", "symbol": raw})
            self.assertIsNone(r.error, msg=f"{raw!r} error={getattr(r.error,'code',None)} msg={getattr(r.error,'message',None)}")

    def test_set_tp_unknown_is_instrument_not_found(self) -> None:
        def boom(_symbol):
            raise RuntimeError("Raydium symbol metadata was unavailable")
        with mock.patch.object(ray, "_resolve_symbol_metadata", side_effect=boom):
            r = ray.execute({"operation": "set_tp", "account": "phantom", "symbol": "NOTAMARKET", "price": "100"})
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "INSTRUMENT_NOT_FOUND")

    def test_cancel_unknown_is_instrument_not_found(self) -> None:
        def boom(_symbol):
            raise RuntimeError("Raydium symbol metadata was unavailable")
        with mock.patch.object(ray, "_resolve_symbol_metadata", side_effect=boom):
            r = ray.execute({"operation": "cancel_order_group", "account": "phantom", "symbol": "NOTAMARKET", "side": "sell"})
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "INSTRUMENT_NOT_FOUND")

    def test_cancel_metadata_failure_is_explicit(self) -> None:
        def boom(_symbol):
            raise RuntimeError("Raydium symbol metadata was unavailable")
        with mock.patch.object(ray, "_resolve_symbol_metadata", side_effect=boom):
            r = ray.execute({"operation": "cancel_order_group", "account": "phantom", "symbol": "SOL", "side": "sell"})
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "INSTRUMENT_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
