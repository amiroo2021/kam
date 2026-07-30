"""Deterministic tests for /trade Phase 3A new-order symbol selection.

These tests keep the new-order flow generic at the wizard/Telegram layer
and verify that Hyperliquid-specific instrument resolution stays inside
plugins.trade.agents.x_hyperliquid_agent.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_PRESERVED_ENV: Dict[str, str] = {}
for _k in list(os.environ.keys()):
    if _k.startswith("HYPERLIQUID_"):
        _PRESERVED_ENV[_k] = os.environ[_k]
        os.environ.pop(_k, None)


def _restore_env() -> None:
    for k in list(os.environ.keys()):
        if k.startswith("HYPERLIQUID_") and k not in _PRESERVED_ENV:
            os.environ.pop(k, None)
    for k, v in _PRESERVED_ENV.items():
        os.environ[k] = v


import atexit

atexit.register(_restore_env)

from plugins.trade.canonical import CanonicalInstrument, CanonicalLadderResult, CanonicalOrderResult, make_success  # noqa: E402
from plugins.trade.tradedesk import TradeDesk  # noqa: E402
from plugins.trade.wizard import TradeWizard, handle_trade_text  # noqa: E402


def _hl_module():
    return importlib.import_module("plugins.trade.agents.x_hyperliquid_agent")


class StubDesk:
    def __init__(self, response):
        self.response = response
        self.requests: List[Dict[str, Any]] = []

    def list_exchanges(self) -> List[str]:
        return ["hyperliquid"]

    def list_accounts(self, exchange: str) -> List[str]:
        return ["FLEX"] if exchange == "hyperliquid" else []

    def execute(self, request: Dict[str, Any]):
        self.requests.append(dict(request))
        return self.response


class FakeAdapter:
    def __init__(self):
        self.inline_messages: List[Dict[str, Any]] = []

    async def send_inline_keyboard(self, *, chat_id, text, buttons, callback_prefix, metadata=None):
        self.inline_messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "buttons": buttons,
                "callback_prefix": callback_prefix,
                "metadata": metadata,
            }
        )


class TestPhase3NewOrderWizard(unittest.TestCase):
    def _open_new_order(self, desk):
        wizard = TradeWizard(tradedesk=desk)  # type: ignore[arg-type]
        key = ("chat",)
        wizard.open(key)
        wizard.handle_callback(key, "exchange:hyperliquid")
        wizard.handle_callback(key, "account:FLEX")
        screen = wizard.handle_callback(key, "action:new_order")
        return wizard, key, screen

    def _success_response(self):
        return make_success(
            operation="new_order",
            exchange="hyperliquid",
            account="FLEX",
            order=CanonicalOrderResult(
                symbol="BTC",
                side="buy",
                order_type="limit",
                requested_volume="0.01",
                requested_price="61000",
                submitted_volume="0.01",
                submitted_price="61000",
                verified=True,
                status="success",
                exchange_order_id=12345,
            ),
        )

    def test_symbol_buttons_open_side_screen_without_calling_desk(self):
        desk = StubDesk(self._success_response())
        wizard, key, screen = self._open_new_order(desk)
        self.assertIn("Select Symbol:", screen.text)
        for symbol in ["BTC", "ETH", "HYPE", "SOL"]:
            with self.subTest(symbol=symbol):
                desk.requests.clear()
                side = wizard.handle_callback(key, f"symbol:{symbol}")
                self.assertIn("Select Side:", side.text)
                self.assertIn(f"Symbol: {symbol}", side.text)
                self.assertEqual(desk.requests, [])
                wizard.handle_callback(key, "back")
                wizard.handle_callback(key, "action:new_order")

    def test_other_enters_awaiting_symbol_without_calling_desk(self):
        desk = StubDesk(self._success_response())
        wizard, key, _screen = self._open_new_order(desk)
        screen = wizard.handle_callback(key, "symbol:other")
        self.assertIn("Enter Symbol:", screen.text)
        self.assertEqual(desk.requests, [])

    def test_typed_other_symbol_is_stored_without_resolution_and_advances(self):
        desk = StubDesk(self._success_response())
        wizard, key, _screen = self._open_new_order(desk)
        wizard.handle_callback(key, "symbol:other")
        screen = wizard.handle_text(key, "  gold  ")
        assert screen is not None
        self.assertIn("Symbol: GOLD", screen.text)
        self.assertIn("Select Side:", screen.text)
        self.assertEqual(desk.requests, [])

    def test_new_order_confirmation_is_canonical_and_trade_desk_only_runs_on_confirm(self):
        desk = StubDesk(self._success_response())
        wizard, key, _screen = self._open_new_order(desk)
        wizard.handle_callback(key, "symbol:BTC")
        wizard.handle_callback(key, "side:buy")
        volume_screen = wizard.handle_text(key, "0.01")
        assert volume_screen is not None
        self.assertIn("Enter Price:", volume_screen.text)
        self.assertEqual(desk.requests, [])
        price_screen = wizard.handle_text(key, "61000")
        assert price_screen is not None
        self.assertIn("⚠️ Confirm New Order", price_screen.text)
        self.assertIn("Symbol: BTC", price_screen.text)
        self.assertIn("Side: 🔵 Buy", price_screen.text)
        self.assertIn("Volume: 0.01", price_screen.text)
        self.assertIn("Price: 61,000", price_screen.text)
        self.assertEqual(desk.requests, [])

        result = wizard.handle_callback(key, "confirm")
        self.assertEqual(len(desk.requests), 1)
        self.assertEqual(
            desk.requests[0],
            {
                "operation": "new_order",
                "exchange": "hyperliquid",
                "account": "FLEX",
                "symbol": "BTC",
                "side": "buy",
                "order_type": "limit",
                "volume": "0.01",
                "price": "61000",
            },
        )
        self.assertIn("✅ Order Submitted", result.text)
        self.assertIn("Verified on exchange: Yes", result.text)
        self.assertNotIn("Requested volume:", result.text)
        self.assertNotIn("Requested price:", result.text)
        self.assertNotIn("Submitted volume:", result.text)
        self.assertNotIn("Submitted price:", result.text)

    def _ladder_success_response(self):
        return make_success(
            operation="ladder",
            exchange="hyperliquid",
            account="FLEX",
            ladder=CanonicalLadderResult(
                symbol="BTC",
                side="buy",
                distribution="half_gaussian",
                requested_order_count=3,
                submitted_order_count=3,
                requested_volume="10",
                submitted_volume="10",
                batch_count=1,
                verified=True,
                partial=False,
                status="success",
                accepted_child_count=3,
                child_order_ids=[1, 2, 3],
                batches=[{"batch_index": 1, "submitted_order_count": 3, "accepted_child_count": 3, "child_order_ids": [1, 2, 3]}],
            ),
        )

    def _open_ladder(self, desk):
        wizard = TradeWizard(tradedesk=desk)  # type: ignore[arg-type]
        key = ("chat",)
        wizard.open(key)
        wizard.handle_callback(key, "exchange:hyperliquid")
        wizard.handle_callback(key, "account:FLEX")
        screen = wizard.handle_callback(key, "action:ladder")
        return wizard, key, screen

    def test_ladder_flow_collects_generic_intent_and_defers_submission_until_confirm(self):
        desk = StubDesk(self._ladder_success_response())
        wizard, key, screen = self._open_ladder(desk)
        self.assertIn("Select Distribution:", screen.text)
        screen = wizard.handle_callback(key, "distribution:half_gaussian")
        self.assertIn("Select Symbol:", screen.text)
        screen = wizard.handle_callback(key, "symbol:BTC")
        self.assertIn("Select Side:", screen.text)
        screen = wizard.handle_callback(key, "side:buy")
        self.assertIn("Enter Number of Orders:", screen.text)
        self.assertEqual(desk.requests, [])
        screen = wizard.handle_text(key, "3")
        self.assertIn("Enter Total Volume:", screen.text)
        screen = wizard.handle_text(key, "10")
        self.assertIn("Enter Start Price:", screen.text)
        screen = wizard.handle_text(key, "64000")
        self.assertIn("Enter End Price:", screen.text)
        self.assertEqual(desk.requests, [])
        screen = wizard.handle_text(key, "60000")
        self.assertIn("⚠️ Confirm Ladder", screen.text)
        self.assertIn("Distribution: Half Gaussian", screen.text)
        self.assertIn("Orders: 3", screen.text)
        self.assertIn("Total Volume: 10", screen.text)
        self.assertIn("Start Price: 64,000", screen.text)
        self.assertIn("End Price: 60,000", screen.text)
        self.assertEqual(desk.requests, [])

        result = wizard.handle_callback(key, "confirm")
        self.assertEqual(len(desk.requests), 1)
        self.assertEqual(
            desk.requests[0],
            {
                "operation": "ladder",
                "exchange": "hyperliquid",
                "account": "FLEX",
                "symbol": "BTC",
                "side": "buy",
                "distribution": "half_gaussian",
                "order_count": "3",
                "total_volume": "10",
                "start_price": "64000",
                "end_price": "60000",
            },
        )
        self.assertIn("Ladder", result.text)
        self.assertIn("Status: success", result.text)
        self.assertIn("Verified: Yes", result.text)

    def test_ladder_other_symbol_is_generic_and_canonical(self):
        desk = StubDesk(self._ladder_success_response())
        wizard, key, _screen = self._open_ladder(desk)
        wizard.handle_callback(key, "distribution:uniform")
        wizard.handle_callback(key, "other")
        screen = wizard.handle_text(key, "gold")
        self.assertIn("Select Side:", screen.text)
        self.assertIn("Symbol: GOLD", screen.text)
        wizard.handle_callback(key, "side:sell")
        wizard.handle_text(key, "2")
        wizard.handle_text(key, "10")
        wizard.handle_text(key, "100")
        confirm = wizard.handle_text(key, "110")
        self.assertIn("⚠️ Confirm Ladder", confirm.text)
        final = wizard.handle_callback(key, "confirm")
        self.assertIn("Verified: Yes", final.text)
        self.assertEqual(desk.requests[0]["symbol"], "GOLD")

    def test_ladder_direction_validation_is_generic(self):
        desk = StubDesk(self._ladder_success_response())
        wizard, key, _screen = self._open_ladder(desk)
        wizard.handle_callback(key, "distribution:uniform")
        wizard.handle_callback(key, "symbol:BTC")
        wizard.handle_callback(key, "side:buy")
        wizard.handle_text(key, "2")
        wizard.handle_text(key, "10")
        wizard.handle_text(key, "100")
        screen = wizard.handle_text(key, "110")
        self.assertIn("For a BUY ladder, End Price must be lower than Start Price.", screen.text)
        wizard.handle_callback(key, "back")
        wizard.handle_callback(key, "back")
        wizard.handle_callback(key, "back")
        wizard.handle_callback(key, "back")
        wizard.handle_callback(key, "back")
        self.assertEqual(desk.requests, [])

    def test_trade_desk_forwards_resolve_instrument_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="trade_phase3_") as tmp:
            agents_dir = Path(tmp) / "agents"
            agents_dir.mkdir()
            agent_path = agents_dir / "x_example_agent.py"
            agent_path.write_text(
                'name = "example"\n'
                'def list_accounts(): return ["alpha"]\n'
                'def capabilities(): return ["balance", "positions_orders", "resolve_instrument"]\n'
                'def execute(request):\n'
                '    from plugins.trade.canonical import make_success, CanonicalInstrument\n'
                '    return make_success(operation=request["operation"], exchange="example", account=request["account"], instrument=CanonicalInstrument(requested_symbol=request["symbol"], symbol=request["symbol"], display_name=request["symbol"] + "-USDC", price_increment=None, size_increment=None, minimum_size=None))\n'
            )
            desk = TradeDesk()
            with mock.patch("plugins.trade.tradedesk._agents_dir", return_value=agents_dir):
                response = desk.execute({
                    "operation": "resolve_instrument",
                    "exchange": "example",
                    "account": "alpha",
                    "symbol": "GOLD",
                    "extra": "keep-me",
                })
            self.assertTrue(response.success)
            self.assertIsNotNone(response.instrument)
            self.assertEqual(response.operation, "resolve_instrument")
            self.assertEqual(response.exchange, "example")
            self.assertEqual(response.account, "alpha")
            self.assertEqual(response.instrument.symbol, "GOLD")
            self.assertEqual(response.to_dict()["instrument"]["display_name"], "GOLD-USDC")

    def test_wizard_and_tradedesk_do_not_contain_hyperliquid_mapping_literals(self):
        wizard_text = Path(_REPO_ROOT / "plugins/trade/wizard.py").read_text()
        desk_text = Path(_REPO_ROOT / "plugins/trade/tradedesk.py").read_text()
        forbidden = [
            "GOLD",
            "SILVER",
            "SP500",
            "XYZ100",
            "assetToStreamingOiCap",
            "xyz:",
            "WTI",
        ]
        for needle in forbidden:
            self.assertNotIn(needle, wizard_text)
            self.assertNotIn(needle, desk_text)


class TestHyperliquidInstrumentResolution(unittest.TestCase):
    def setUp(self):
        self._patches = []

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def patch(self, target: str, value: Any):
        p = mock.patch(target, value)
        self._patches.append(p)
        return p.start()

    def test_resolve_instrument_uses_only_read_only_metadata(self):
        hl = _hl_module()
        requests: List[Dict[str, Any]] = []

        def fake_lookup(alias: str):
            self.assertEqual(alias, "FLEX")
            return ("0x1111111111111111111111111111111111111111", "secret")

        def fake_post(payload: Dict[str, Any], timeout: int = 20):
            requests.append(dict(payload))
            if payload["type"] == "perpDexs":
                return [None, {"name": "xyz"}]
            if payload["type"] == "metaAndAssetCtxs" and payload.get("dex", "") == "":
                return [
                    {"universe": [{"name": "BTC", "szDecimals": 5}]},
                    [{"markPx": "65000.1"}],
                ]
            if payload["type"] == "metaAndAssetCtxs" and payload.get("dex") == "xyz":
                return [
                    {"universe": [{"name": "xyz:GOLD", "szDecimals": 2}]},
                    [{"markPx": "2000.50"}],
                ]
            raise AssertionError(f"unexpected payload: {payload}")

        self.patch("plugins.trade.agents.x_hyperliquid_agent._lookup_credentials", fake_lookup)
        self.patch("plugins.trade.agents.x_hyperliquid_agent._post_info", fake_post)
        self.patch("plugins.trade.agents.x_hyperliquid_agent._normalize_account_alias", lambda alias: alias)

        response = hl.execute({
            "operation": "resolve_instrument",
            "exchange": "hyperliquid",
            "account": "FLEX",
            "symbol": "gold-usdc",
        })

        self.assertTrue(response.success)
        self.assertEqual([req["type"] for req in requests], ["perpDexs", "metaAndAssetCtxs", "metaAndAssetCtxs"])
        self.assertIsNotNone(response.instrument)
        self.assertEqual(response.instrument.requested_symbol, "gold-usdc")
        self.assertEqual(response.instrument.symbol, "GOLD")
        self.assertEqual(response.instrument.display_name, "GOLD-USDC")
        self.assertEqual(response.instrument.price_increment, "0.01")
        self.assertEqual(response.instrument.size_increment, "0.01")

    def test_alias_spx_resolves_to_sp500(self):
        hl = _hl_module()

        def fake_lookup(alias: str):
            return ("0x1111111111111111111111111111111111111111", "secret")

        def fake_post(payload: Dict[str, Any], timeout: int = 20):
            if payload["type"] == "perpDexs":
                return [None]
            if payload["type"] == "metaAndAssetCtxs":
                return [
                    {"universe": [{"name": "SP500", "szDecimals": 0}]},
                    [{"markPx": "5600"}],
                ]
            raise AssertionError(payload)

        self.patch("plugins.trade.agents.x_hyperliquid_agent._lookup_credentials", fake_lookup)
        self.patch("plugins.trade.agents.x_hyperliquid_agent._post_info", fake_post)
        self.patch("plugins.trade.agents.x_hyperliquid_agent._normalize_account_alias", lambda alias: alias)

        response = hl.execute({
            "operation": "resolve_instrument",
            "exchange": "hyperliquid",
            "account": "FLEX",
            "symbol": "SPX",
        })

        self.assertTrue(response.success)
        self.assertIsNotNone(response.instrument)
        self.assertEqual(response.instrument.symbol, "SP500")
        self.assertEqual(response.instrument.display_name, "SP500-USDC")

    def test_resolve_instrument_prioritizes_canonical_first_class_markets(self):
        hl = _hl_module()
        candidate = lambda dex, internal: {
            "dex": dex,
            "dex_index": {"": 0, "xyz": 1, "flx": 2, "hyna": 3, "cash": 4}.get(dex, 99),
            "internal_name": internal,
            "public_symbol": internal.split(":", 1)[1] if ":" in internal else internal,
            "public_key": _hl_module()._symbol_key(internal.split(":", 1)[1] if ":" in internal else internal),
            "internal_key": _hl_module()._symbol_key(internal),
            "display_name": (internal.split(":", 1)[1] if ":" in internal else internal) + "-USDC",
            "price_increment": "0.1",
            "size_increment": "0.01",
        }

        cases = [
            ("BTC", [candidate("", "BTC"), candidate("hyna", "hyna:BTC"), candidate("cash", "cash:BTC")], "BTC", ""),
            ("ETH", [candidate("", "ETH"), candidate("hyna", "hyna:ETH"), candidate("cash", "cash:ETH")], "ETH", ""),
            ("HYPE", [candidate("", "HYPE"), candidate("hyna", "hyna:HYPE")], "HYPE", ""),
            ("SOL", [candidate("", "SOL"), candidate("hyna", "hyna:SOL")], "SOL", ""),
            ("GOLD", [candidate("xyz", "xyz:GOLD"), candidate("flx", "flx:GOLD"), candidate("hyna", "hyna:GOLD")], "GOLD", "xyz"),
            ("SPX", [candidate("xyz", "xyz:SP500")], "SP500", "xyz"),
            ("XYZ100", [candidate("xyz", "xyz:XYZ100")], "XYZ100", "xyz"),
            ("OIL", [candidate("flx", "flx:OIL")], "OIL", "flx"),
            ("WTI", [candidate("cash", "cash:WTI")], "WTI", "cash"),
        ]

        for requested, candidates, expected_symbol, expected_dex in cases:
            with self.subTest(requested=requested):
                chosen, error = hl._resolve_instrument_candidate(requested, candidates)
                self.assertEqual(error, "")
                self.assertIsNotNone(chosen)
                self.assertEqual(chosen["public_symbol"], expected_symbol)
                self.assertEqual(chosen["dex"], expected_dex)

    def test_resolve_instrument_rejects_missing_symbol(self):
        hl = _hl_module()
        self.patch("plugins.trade.agents.x_hyperliquid_agent._normalize_account_alias", lambda alias: alias)
        self.patch("plugins.trade.agents.x_hyperliquid_agent._lookup_credentials", lambda alias: ("0x1111111111111111111111111111111111111111", "secret"))
        response = hl.execute({
            "operation": "resolve_instrument",
            "exchange": "hyperliquid",
            "account": "FLEX",
            "symbol": "   ",
        })
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "MISSING_SYMBOL")


if __name__ == "__main__":
    unittest.main()
