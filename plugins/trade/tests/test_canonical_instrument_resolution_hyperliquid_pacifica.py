"""Canonical instrument resolution: Hyperliquid + Pacifica (offline)."""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any, Dict
from unittest import mock

from plugins.trade.agents import x_hyperliquid_agent as hl
from plugins.trade.agents import x_pacifica_agent as pacifica
from plugins.trade.canonical import CanonicalPosition


def _hl_candidate(dex: str, internal_name: str, dex_index: int = 1) -> Dict[str, Any]:
    public = internal_name.split(":", 1)[1] if ":" in internal_name else internal_name
    return {
        "dex": dex,
        "dex_index": dex_index,
        "internal_name": internal_name,
        "route_symbol": internal_name,
        "public_symbol": public,
        "public_key": hl._symbol_key(public),
        "internal_key": hl._symbol_key(internal_name),
        "display_name": f"{public}-USDC",
        "price_increment": "0.1",
        "size_increment": "0.01",
        "sz_decimals": 2,
    }


class HyperliquidCanonicalResolverTests(unittest.TestCase):
    def test_explicit_route_and_bare_alias_agree_when_unique(self):
        xyz = _hl_candidate("xyz", "xyz:SP500")
        cand_route, err_a = hl._resolve_instrument_candidate("xyz:SP500", [xyz])
        cand_alias, err_b = hl._resolve_instrument_candidate("SP500", [xyz])
        self.assertEqual(err_a, "")
        self.assertEqual(err_b, "")
        self.assertEqual(cand_route["route_symbol"], "xyz:SP500")
        self.assertEqual(cand_alias["route_symbol"], "xyz:SP500")

    def test_bare_alias_ambiguous_across_two_hip3_routes(self):
        xyz = _hl_candidate("xyz", "xyz:SP500", dex_index=1)
        flx = _hl_candidate("flx", "flx:SP500", dex_index=2)
        cand, err = hl._resolve_instrument_candidate("SP500", [xyz, flx])
        self.assertIsNone(cand)
        self.assertEqual(err, "INSTRUMENT_AMBIGUOUS")

    def test_route_qualified_selects_intended_dex(self):
        xyz = _hl_candidate("xyz", "xyz:SP500")
        flx = _hl_candidate("flx", "flx:SP500")
        a, _ = hl._resolve_instrument_candidate("xyz:SP500", [xyz, flx])
        b, _ = hl._resolve_instrument_candidate("flx:SP500", [xyz, flx])
        self.assertEqual(a["dex"], "xyz")
        self.assertEqual(b["dex"], "flx")

    def test_sol_does_not_resolve_to_solana(self):
        solana = _hl_candidate("", "SOLANA", dex_index=0)
        solana["public_symbol"] = "SOLANA"
        solana["public_key"] = hl._symbol_key("SOLANA")
        cand, err = hl._resolve_instrument_candidate("SOL", [solana])
        self.assertIsNone(cand)
        self.assertEqual(err, "INSTRUMENT_NOT_FOUND")

    def test_native_btc_still_resolves(self):
        btc = _hl_candidate("", "BTC", dex_index=0)
        cand, err = hl._resolve_instrument_candidate("BTC", [btc])
        self.assertEqual(err, "")
        self.assertEqual(cand["route_symbol"], "BTC")

    def test_position_context_binds_same_route(self):
        xyz = _hl_candidate("xyz", "xyz:SP500")
        flx = _hl_candidate("flx", "flx:SP500")
        pos = CanonicalPosition(symbol="xyz:SP500", side="short", size="47.7", entry_price="7770", pnl="0")
        positions_resp = mock.Mock(success=True, error=None, positions=[pos])
        with mock.patch.object(hl, "_normalize_account_alias", return_value="FLEX"), mock.patch.object(
            hl, "_execute_positions_orders", return_value=positions_resp
        ), mock.patch.object(hl, "_lookup_credentials", return_value=("0x1", "s")), mock.patch.object(
            hl, "_fetch_perp_market_candidates", return_value=[xyz, flx]
        ), mock.patch.object(hl, "_fetch_open_orders_snapshot", return_value=[]), mock.patch.object(
            hl, "_fetch_candidate_mark_price", return_value=Decimal("7770")
        ):
            ctx, fail = hl._current_position_management_context("close_position", "FLEX", "xyz:SP500")
        self.assertIsNone(fail)
        self.assertEqual(ctx["candidate"]["route_symbol"], "xyz:SP500")
        self.assertEqual(ctx["current_position"].symbol, "xyz:SP500")

    def test_bare_alias_does_not_bind_wrong_dex_position(self):
        xyz = _hl_candidate("xyz", "xyz:SP500")
        flx = _hl_candidate("flx", "flx:SP500")
        pos = CanonicalPosition(symbol="xyz:SP500", side="short", size="47.7", entry_price="7770", pnl="0")
        positions_resp = mock.Mock(success=True, error=None, positions=[pos])
        with mock.patch.object(hl, "_normalize_account_alias", return_value="FLEX"), mock.patch.object(
            hl, "_execute_positions_orders", return_value=positions_resp
        ), mock.patch.object(hl, "_lookup_credentials", return_value=("0x1", "s")), mock.patch.object(
            hl, "_fetch_perp_market_candidates", return_value=[xyz, flx]
        ):
            ctx, fail = hl._current_position_management_context("close_position", "FLEX", "SP500")
        self.assertIsNone(ctx)
        self.assertIsNotNone(fail)
        self.assertEqual(fail.error.code, "INSTRUMENT_AMBIGUOUS")


class PacificaCanonicalResolverTests(unittest.TestCase):
    def setUp(self):
        pacifica._PACIFICA_MARKET_INFO_CACHE = {
            "BTC": {"symbol": "BTC", "tick_size": "0.1", "lot_size": "0.001"},
            "SOL": {"symbol": "SOL", "tick_size": "0.01", "lot_size": "0.1"},
        }
        pacifica._PACIFICA_MARKET_INFO_FETCHED_AT = 10**12

    def tearDown(self):
        pacifica._PACIFICA_MARKET_INFO_CACHE = None
        pacifica._PACIFICA_MARKET_INFO_FETCHED_AT = 0.0

    def _creds(self):
        return mock.patch.object(
            pacifica,
            "_lookup_credentials",
            return_value={"account": "main", "address": "Addr", "agent_wallet": "W", "agent_private_key": "k"},
        )

    def test_listed_native_symbol_resolves(self):
        with self._creds():
            r = pacifica.execute({"operation": "resolve_instrument", "account": "main", "symbol": "BTC"})
        self.assertTrue(r.success)
        self.assertEqual(r.instrument.symbol, "BTC")

    def test_unresolved_symbol_fails(self):
        with self._creds():
            r = pacifica.execute({"operation": "resolve_instrument", "account": "main", "symbol": "NOTAMARKET"})
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "INSTRUMENT_NOT_FOUND")

    def test_close_unresolved_is_not_already_flat(self):
        with self._creds(), mock.patch.object(pacifica, "_get_positions", return_value=[]):
            r = pacifica.execute({"operation": "close_position", "account": "main", "symbol": "NOTAMARKET"})
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "INSTRUMENT_NOT_FOUND")
        self.assertNotEqual(getattr(r.position_action, "status", ""), "success")

    def test_close_fetch_exception_is_not_already_flat(self):
        with self._creds(), mock.patch.object(pacifica, "_get_positions", side_effect=RuntimeError("boom")):
            r = pacifica.execute({"operation": "close_position", "account": "main", "symbol": "BTC"})
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "POSITIONS_UNAVAILABLE")

    def test_close_resolved_empty_is_already_flat(self):
        with self._creds(), mock.patch.object(pacifica, "_get_positions", return_value=[]):
            r = pacifica.execute({"operation": "close_position", "account": "main", "symbol": "BTC"})
        self.assertTrue(r.success)
        self.assertTrue(r.position_action.verified)

    def test_cancel_unresolved_fails(self):
        with self._creds(), mock.patch.object(pacifica, "_get_open_orders", return_value=[]):
            r = pacifica.execute(
                {"operation": "cancel_order_group", "account": "main", "symbol": "NOTAMARKET", "side": "long"}
            )
        self.assertFalse(r.success)
        self.assertEqual(r.error.code, "INSTRUMENT_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
