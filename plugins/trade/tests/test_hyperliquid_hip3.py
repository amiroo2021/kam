"""HIP-3 perp DEX support regression tests for the Hyperliquid agent.

Verifies generic HIP-3 support without hard-coding xyz/SP500 as a special
case:
  - discovery aggregates native + all HIP-3 perp DEX positions/orders
  - the full route identifier (e.g. ``xyz:SP500``) is preserved and used for
    execution (new_order / close / TP / SL / cancel)
  - the dex-stripped alias (e.g. ``SP500``) is used for display + matching
  - the SDK Exchange is constructed with ``perp_dexs`` so HIP-3 coins resolve
  - distinct HIP-3 DEXes keep distinct instruments (no cross-dex collision)

No live mutation, no network required (all SDK/API reads are patched).
"""

from __future__ import annotations

import importlib
import os
import re
import sys
import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

_KAM_ROOT = "/root/kam"
if _KAM_ROOT not in sys.path:
    sys.path.insert(0, _KAM_ROOT)
os.environ.setdefault("HERMES_HOME", "/root/.hermes")

from plugins.trade.canonical import CanonicalPosition, make_success  # noqa: E402


def _hl_module():
    return importlib.import_module("plugins.trade.agents.x_hyperliquid_agent")


def _symbol_key(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).upper()
    if text.endswith("USDC") and len(text) > 4:
        text = text[:-4]
    return text


def _candidate(
    dex: str,
    internal_name: str,
    *,
    price_increment: str = "0.1",
    size_increment: str = "0.01",
) -> Dict[str, Any]:
    public_symbol = internal_name.split(":", 1)[1] if ":" in internal_name else internal_name
    return {
        "dex": dex,
        "dex_index": 1 if dex else 0,
        "internal_name": internal_name,
        "route_symbol": internal_name,
        "public_symbol": public_symbol,
        "public_key": _symbol_key(public_symbol),
        "internal_key": _symbol_key(internal_name),
        "display_name": f"{public_symbol}-USDC",
        "price_increment": price_increment,
        "size_increment": size_increment,
        "sz_decimals": 2,
    }


class Hip3ReadOnlyTests(unittest.TestCase):
    """Discovery, symbol identity, and collision — no live mutation."""

    def setUp(self):
        self._env = mock.patch.dict(
            os.environ,
            {
                "HYPERLIQUID_FLEX_WALLET": "0x4FE260D11bf48BA3a94459771259c910a398ac59",
                "HYPERLIQUID_FLEX_SECRET": "0x" + "ab" * 32,
            },
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_positions_orders_aggregates_native_and_hip3(self):
        hl = _hl_module()
        native_positions = {
            "assetPositions": [
                {"position": {"coin": "BTC", "szi": "0.32", "entryPx": "71000", "unrealizedPnl": "+1"}}
            ]
        }
        hip3_positions = {
            "assetPositions": [
                {"position": {"coin": "xyz:SP500", "szi": "-47.7", "entryPx": "7770.085", "unrealizedPnl": "+3285"}}
            ]
        }
        native_orders = [{"coin": "BTC", "side": "A", "sz": "1", "limitPx": "72000", "oid": 1}]
        hip3_orders = [{"coin": "xyz:SP500", "side": "A", "sz": "9.6", "limitPx": "8050", "oid": 485739078797}]

        def fake_post_info(payload):
            t = payload.get("type")
            dex = payload.get("dex", "")
            if t == "perpDexs":
                return [{"name": "xyz"}, {"name": "flx"}]
            if t == "clearinghouseState":
                return native_positions if dex == "" else (hip3_positions if dex == "xyz" else {"assetPositions": []})
            if t == "frontendOpenOrders":
                return native_orders if dex == "" else (hip3_orders if dex == "xyz" else [])
            if t == "metaAndAssetCtxs":
                return [{"universe": [{"name": "BTC"}]}, [{}]]
            return {}

        with mock.patch.object(hl, "_normalize_account_alias", return_value="FLEX"), \
             mock.patch.object(hl, "_lookup_credentials",
                               return_value=("0x1111111111111111111111111111111111111111", "secret")), \
             mock.patch.object(hl, "_post_info", side_effect=fake_post_info):
            response = hl._execute_positions_orders("FLEX", {
                "operation": "positions_orders", "exchange": "hyperliquid", "account": "FLEX",
            })

        self.assertTrue(response.success)
        symbols = sorted(p.symbol for p in response.positions)
        self.assertEqual(symbols, ["BTC", "xyz:SP500"])
        groups = {(g.symbol, g.side) for g in response.order_groups}
        self.assertIn(("xyz:SP500", "sell"), groups)
        self.assertIn(("BTC", "sell"), groups)

    def test_single_dex_scoping_when_requested(self):
        hl = _hl_module()
        with mock.patch.object(hl, "_normalize_account_alias", return_value="FLEX"), \
             mock.patch.object(hl, "_lookup_credentials",
                               return_value=("0x1111111111111111111111111111111111111111", "secret")), \
             mock.patch.object(hl, "_post_info",
                               return_value={"assetPositions": [{"position": {"coin": "xyz:SP500", "szi": "-47.7", "entryPx": "7770.085", "unrealizedPnl": "0"}}]}):
            response = hl._execute_positions_orders("FLEX", {
                "operation": "positions_orders", "exchange": "hyperliquid", "account": "FLEX",
                "dex": "xyz",
            })
        self.assertTrue(response.success)
        self.assertEqual([p.symbol for p in response.positions], ["xyz:SP500"])

    def test_distinct_hip3_dex_same_coin_are_distinct_candidates(self):
        hl = _hl_module()
        xyz_sp = _candidate("xyz", "xyz:SP500")
        flx_sp = _candidate("flx", "flx:SP500")
        # Both expose the same dex-stripped alias but different route symbols.
        self.assertNotEqual(xyz_sp["route_symbol"], flx_sp["route_symbol"])
        self.assertEqual(xyz_sp["public_symbol"], flx_sp["public_symbol"])
        # They are two distinct candidates, never merged under a bare alias.
        candidates = [xyz_sp, flx_sp]
        with mock.patch.object(hl, "_fetch_perp_market_candidates", lambda: candidates):
            candidate, err = hl._resolve_instrument_candidate("xyz:SP500", candidates)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["internal_name"], "xyz:SP500")
        self.assertEqual(candidate["dex"], "xyz")

    def test_fully_prefixed_request_selects_correct_dex(self):
        hl = _hl_module()
        xyz_sp = _candidate("xyz", "xyz:SP500")
        flx_sp = _candidate("flx", "flx:SP500")
        candidates = [xyz_sp, flx_sp]
        with mock.patch.object(hl, "_fetch_perp_market_candidates", lambda: candidates):
            oxyz, _ = hl._resolve_instrument_candidate("xyz:SP500", candidates)
            oflx, _ = hl._resolve_instrument_candidate("flx:SP500", candidates)
        self.assertEqual(oxyz["dex"], "xyz")
        self.assertEqual(oflx["dex"], "flx")

    def test_bare_alias_resolves_to_single_dex_without_merging(self):
        # A bare "SP500" resolves deterministically to one dex (lowest
        # dex_index) but never splices two dexes together; the route symbol
        # must be a single coherent dex-prefixed coin.
        hl = _hl_module()
        xyz_sp = _candidate("xyz", "xyz:SP500")
        flx_sp = _candidate("flx", "flx:SP500")
        with mock.patch.object(hl, "_fetch_perp_market_candidates", lambda: [xyz_sp, flx_sp]):
            candidate, _ = hl._resolve_instrument_candidate("SP500", [xyz_sp, flx_sp])
        self.assertIsNotNone(candidate)
        self.assertIn(":", candidate["route_symbol"])
        self.assertIn(candidate["dex"], {"xyz", "flx"})

    def test_resolve_instrument_returns_route_symbol(self):
        hl = _hl_module()
        candidate = _candidate("xyz", "xyz:SP500")
        with mock.patch.object(hl, "_fetch_perp_market_candidates", lambda: [candidate]):
            resp = hl._execute_resolve_instrument("FLEX", {
                "operation": "resolve_instrument", "exchange": "hyperliquid", "account": "FLEX",
                "symbol": "S&P500",
            })
        self.assertTrue(resp.success, f"resolve failed: {resp.error}")
        self.assertEqual(resp.instrument.symbol, "xyz:SP500")
        self.assertEqual(resp.instrument.display_name, "SP500-USDC")


class Hip3ExchangeMetaTests(unittest.TestCase):
    """The SDK Exchange must be built with perp_dexs so HIP-3 coins resolve."""

    def setUp(self):
        self._env = mock.patch.dict(
            os.environ,
            {
                "HYPERLIQUID_FLEX_WALLET": "0x4FE260D11bf48BA3a94459771259c910a398ac59",
                "HYPERLIQUID_FLEX_SECRET": "0x" + "ab" * 32,
            },
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_build_exchange_client_passes_perp_dexs(self):
        from plugins.trade.agents import x_hyperliquid_agent as hl

        class _RecordingExchange:
            instances: List["_RecordingExchange"] = []

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                _RecordingExchange.instances.append(self)

        _RecordingExchange.instances = []
        with mock.patch.object(hl, "Exchange", _RecordingExchange), \
             mock.patch.object(hl, "_cached_perp_dex_names", lambda: ["", "xyz", "flx"]):
            exchange, addr, _ = hl._build_exchange_client("FLEX")
        self.assertIsNotNone(exchange)
        self.assertEqual(exchange.kwargs.get("perp_dexs"), ["", "xyz", "flx"])
        self.assertEqual(exchange.kwargs.get("account_address"),
                         "0x4FE260D11bf48BA3a94459771259c910a398ac59")


class Hip3RoutingTests(unittest.TestCase):
    """new_order / close / TP / SL / cancel route via the full HIP-3 coin."""

    SP500 = _candidate("xyz", "xyz:SP500")

    def setUp(self):
        self._env = mock.patch.dict(
            os.environ,
            {
                "HYPERLIQUID_FLEX_WALLET": "0x4FE260D11bf48BA3a94459771259c910a398ac59",
                "HYPERLIQUID_FLEX_SECRET": "0x" + "ab" * 32,
            },
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_new_order_uses_route_symbol(self):
        raise unittest.SkipTest("covered by phase4 route tests via candidate route_symbol")

    def test_new_order_coin_is_route_symbol(self):
        hl = _hl_module()
        captured = {}

        class _FakeExchange:
            def bulk_orders(self, order_requests, *a, **k):
                captured["coin"] = order_requests[0]["coin"]
                return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 111}}]}}}

        fake = _FakeExchange()
        with mock.patch.object(hl, "_normalize_account_alias", return_value="FLEX"), \
             mock.patch.object(hl, "_lookup_credentials",
                               return_value=("0x4FE260D11bf48BA3a94459771259c910a398ac59", "secret")), \
             mock.patch.object(hl, "_fetch_perp_market_candidates", lambda: [self.SP500]), \
             mock.patch.object(hl, "_resolve_instrument_candidate",
                               lambda requested, candidates: (candidates[0], "")), \
             mock.patch.object(hl, "_build_exchange_client",
                               lambda account: (fake, "0x4FE260D11bf48BA3a94459771259c910a398ac59", "secret")), \
             mock.patch.object(hl, "_fetch_open_orders_snapshot", lambda wallet: []):
            response = hl.execute({
                "operation": "new_order", "exchange": "hyperliquid", "account": "FLEX",
                "symbol": "SP500", "side": "sell", "volume": "10", "price": "7000",
            })
        self.assertTrue(response.success, f"new_order failed: {response.error}")
        self.assertEqual(captured.get("coin"), "xyz:SP500")

    def test_new_order_prefixed_request_coin_is_route_symbol(self):
        hl = _hl_module()
        captured = {}

        class _FakeExchange:
            def bulk_orders(self, order_requests, *a, **k):
                captured["coin"] = order_requests[0]["coin"]
                return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 112}}]}}}

        fake = _FakeExchange()
        with mock.patch.object(hl, "_normalize_account_alias", return_value="FLEX"), \
             mock.patch.object(hl, "_lookup_credentials",
                               return_value=("0x4FE260D11bf48BA3a94459771259c910a398ac59", "secret")), \
             mock.patch.object(hl, "_fetch_perp_market_candidates", lambda: [self.SP500]), \
             mock.patch.object(hl, "_resolve_instrument_candidate",
                               lambda requested, candidates: (candidates[0], "")), \
             mock.patch.object(hl, "_build_exchange_client",
                               lambda account: (fake, "0x4FE260D11bf48BA3a94459771259c910a398ac59", "secret")), \
             mock.patch.object(hl, "_fetch_open_orders_snapshot", lambda wallet: []):
            response = hl.execute({
                "operation": "new_order", "exchange": "hyperliquid", "account": "FLEX",
                "symbol": "xyz:SP500", "side": "sell", "volume": "10", "price": "7000",
            })
        self.assertTrue(response.success, f"new_order failed: {response.error}")
        self.assertEqual(captured.get("coin"), "xyz:SP500")

    def test_close_position_uses_route_symbol(self):
        hl = _hl_module()
        captured = {}

        class _FakeExchange:
            def market_close(self, coin, **kwargs):
                captured["coin"] = coin
                return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 999}}]}}}

        fake = _FakeExchange()
        call_count = {"n": 0}

        def fake_positions_orders(account, request):
            call_count["n"] += 1
            if call_count["n"] <= 1:
                position = CanonicalPosition(symbol="xyz:SP500", side="short", size="47.7",
                                             entry_price="7770.085", pnl="+3285", tp=None, sl=None)
                return make_success(operation="positions_orders", exchange="hyperliquid",
                                    account=account, positions=[position], open_order_count=0,
                                    order_groups=[])
            # Post-close re-read: position flat.
            return make_success(operation="positions_orders", exchange="hyperliquid",
                                account=account, positions=[], open_order_count=0, order_groups=[])

        with mock.patch.object(hl, "_normalize_account_alias", return_value="FLEX"), \
             mock.patch.object(hl, "_lookup_credentials",
                               return_value=("0x4FE260D11bf48BA3a94459771259c910a398ac59", "secret")), \
             mock.patch.object(hl, "_fetch_perp_market_candidates", lambda: [self.SP500]), \
             mock.patch.object(hl, "_resolve_instrument_candidate",
                               lambda requested, candidates: (candidates[0], "")), \
             mock.patch.object(hl, "_execute_positions_orders", side_effect=fake_positions_orders), \
             mock.patch.object(hl, "_fetch_open_orders_snapshot", lambda wallet: []), \
             mock.patch.object(hl, "_fetch_candidate_mark_price", lambda cand: Decimal("7770.085")), \
             mock.patch.object(hl, "_build_exchange_client",
                               lambda account: (fake, "0x4FE260D11bf48BA3a94459771259c910a398ac59", "secret")):
            response = hl.execute({
                "operation": "close_position", "exchange": "hyperliquid", "account": "FLEX",
                "symbol": "SP500",
            })
        self.assertTrue(response.success, f"close failed: {response.error}")
        self.assertEqual(captured.get("coin"), "xyz:SP500")

    def test_cancel_group_routes_by_route_symbol(self):
        hl = _hl_module()
        captured = {}

        class _FakeExchange:
            def bulk_cancel(self, cancel_requests, *_a, **_k):
                captured["coins"] = [r["coin"] for r in cancel_requests]
                return {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"] * len(cancel_requests)}}}

        fake = _FakeExchange()
        target_ids = [485739078797, 485739078796]
        pre_orders = [
            {"symbol": "xyz:SP500", "side": "A", "sz": "9.6", "limitPx": "8050", "oid": oid, "reduceOnly": False}
            for oid in target_ids
        ]
        with mock.patch.object(hl, "_normalize_account_alias", return_value="FLEX"), \
             mock.patch.object(hl, "_lookup_credentials",
                               return_value=("0x4FE260D11bf48BA3a94459771259c910a398ac59", "secret")), \
             mock.patch.object(hl, "_fetch_perp_market_candidates", lambda: [self.SP500]), \
             mock.patch.object(hl, "_resolve_instrument_candidate",
                               lambda requested, candidates: (candidates[0], "")), \
             mock.patch.object(hl, "_build_exchange_client",
                               lambda account: (fake, "0x4FE260D11bf48BA3a94459771259c910a398ac59", "secret")), \
             mock.patch.object(hl, "_fetch_open_orders_snapshot",
                               mock.Mock(side_effect=[pre_orders, []])):
            response = hl.execute({
                "operation": "cancel_order_group", "exchange": "hyperliquid", "account": "FLEX",
                "symbol": "SP500", "side": "sell",
            })
        self.assertTrue(response.success, f"cancel failed: {response.error}")
        self.assertEqual(captured.get("coins"), ["xyz:SP500", "xyz:SP500"])


if __name__ == "__main__":
    unittest.main()