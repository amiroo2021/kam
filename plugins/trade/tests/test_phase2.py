"""Deterministic tests for /trade Phase 2 positions & orders.

These tests are still fully offline except where the Hyperliquid agent is
monkeypatched with canned read-only payloads. They verify the new generic
Positions & Orders screen, the canonical request/response shape, and the
Hyperliquid normalization rules without introducing any exchange-specific
logic into the wizard or TradeDesk.
"""

from __future__ import annotations

import atexit
import importlib
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Module-level env state preservation for HYPERLIQUID_* keys.
# Pop HYPERLIQUID_* env vars only at module import time, and restore
# them at module teardown. The atexit hook was insufficient because
# it never fires between tests inside one unittest process.
_MODULE_PRESERVED_HYPERLIQUID_ENV: Dict[str, str] = {}
for _k in list(os.environ.keys()):
    if _k.startswith("HYPERLIQUID_"):
        _MODULE_PRESERVED_HYPERLIQUID_ENV[_k] = os.environ[_k]


def _restore_env() -> None:
    """Backward-compat no-op. Real restoration lives in tearDownModule."""
    pass


def setUpModule() -> None:
    for _k in list(os.environ.keys()):
        if _k.startswith("HYPERLIQUID_") and _k not in _MODULE_PRESERVED_HYPERLIQUID_ENV:
            _MODULE_PRESERVED_HYPERLIQUID_ENV[_k] = os.environ[_k]


def tearDownModule() -> None:
    for _k in list(os.environ.keys()):
        if _k.startswith("HYPERLIQUID_") and _k not in _MODULE_PRESERVED_HYPERLIQUID_ENV:
            os.environ.pop(_k, None)
    for _k, _v in _MODULE_PRESERVED_HYPERLIQUID_ENV.items():
        os.environ[_k] = _v



from plugins.trade.canonical import (  # noqa: E402
    GENERIC_ACTIONS,
    GENERIC_ACTION_LABELS,
    CanonicalOrderGroup,
    CanonicalPosition,
    CanonicalResponse,
    make_failure,
    make_success,
)
from plugins.trade.tradedesk import TradeDesk  # noqa: E402
from plugins.trade.wizard import TradeWizard  # noqa: E402


def _hl_module():
    return importlib.import_module("plugins.trade.agents.x_hyperliquid_agent")


def _market_meta_payload(*pairs: tuple[str, str]):
    return [
        {"universe": [{"name": symbol} for symbol, _mark_px in pairs]},
        [{"markPx": mark_px} for _symbol, mark_px in pairs],
    ]


class StubDesk:
    def __init__(self, response: CanonicalResponse):
        self.response = response
        self.requests: List[Dict[str, Any]] = []

    def list_exchanges(self) -> List[str]:
        return ["hyperliquid"]

    def list_accounts(self, exchange: str) -> List[str]:
        return ["FLEX"] if exchange == "hyperliquid" else []

    def execute(self, request: Dict[str, Any]) -> CanonicalResponse:
        self.requests.append(dict(request))
        return self.response


class TestActionPageLabelChange(unittest.TestCase):
    def test_positions_orders_replaces_orders_label(self):
        self.assertEqual(GENERIC_ACTION_LABELS["positions_orders"], "Positions & Orders")
        self.assertNotIn("orders", GENERIC_ACTION_LABELS)
        self.assertEqual(
            list(GENERIC_ACTIONS),
            [
                "balance",
                "positions_orders",
                "new_order",
                "ladder",
                "cancel_orders",
                "positions_management",
            ],
        )


class TestWizardCanonicalRequest(unittest.TestCase):
    def test_wizard_emits_positions_orders_request(self):
        response = make_success(
            operation="positions_orders",
            exchange="hyperliquid",
            account="FLEX",
            positions=[],
            open_order_count=0,
            order_groups=[],
        )
        desk = StubDesk(response)
        wizard = TradeWizard(tradedesk=desk)
        key = ("chat",)
        wizard.open(key)
        wizard.handle_callback(key, "exchange:hyperliquid")
        wizard.handle_callback(key, "account:FLEX")
        screen = wizard.handle_callback(key, "action:positions_orders")
        self.assertEqual(desk.requests[-1], {
            "operation": "positions_orders",
            "exchange": "hyperliquid",
            "account": "FLEX",
        })
        self.assertIn("Open Orders &", screen.text)
        self.assertIn("Positions", screen.text)


class TestTradeDeskRouting(unittest.TestCase):
    def test_positions_orders_routes_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="trade_phase2_") as tmp:
            agents_dir = Path(tmp) / "agents"
            agents_dir.mkdir()
            agent_path = agents_dir / "x_example_agent.py"
            agent_path.write_text(
                'name = "example"\n'
                'def list_accounts(): return ["alpha"]\n'
                'def capabilities(): return ["balance", "positions_orders"]\n'
                'def execute(request):\n'
                '    from plugins.trade.canonical import make_success\n'
                '    return make_success(operation=request["operation"], exchange="example", account=request["account"], positions=[], open_order_count=0, order_groups=[])\n'
            )
            desk = TradeDesk()
            with mock.patch("plugins.trade.tradedesk._agents_dir", return_value=agents_dir):
                response = desk.execute({
                    "operation": "positions_orders",
                    "exchange": "example",
                    "account": "alpha",
                    "extra": "keep-me",
                })
            self.assertTrue(response.success)
            self.assertEqual(response.operation, "positions_orders")
            self.assertEqual(response.exchange, "example")
            self.assertEqual(response.account, "alpha")
            self.assertEqual(response.to_dict()["positions"], [])


class TestHyperliquidNormalization(unittest.TestCase):
    def setUp(self):
        self._patches = []

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def patch(self, target: str, value: Any):
        p = mock.patch(target, value)
        self._patches.append(p)
        return p.start()

    def test_long_and_short_positions_normalize_and_group_orders(self):
        market_meta_payload = _market_meta_payload(("BTC", "64696.0"), ("HYPE", "58.87"))
        state_payload = {
            "assetPositions": [
                {
                    "position": {
                        "coin": "BTC",
                        "entryPx": "64491.5",
                        "szi": "82.81931",
                        "unrealizedPnl": "12539.01",
                    }
                },
                {
                    "position": {
                        "coin": "HYPE",
                        "entryPx": "70.858",
                        "szi": "-18015.19",
                        "unrealizedPnl": "215389.65",
                    }
                },
            ]
        }
        open_orders_payload = [
            {
                "coin": "BTC",
                "side": "B",
                "limitPx": "62101.74",
                "sz": "1",
                "origSz": "2",
                "oid": 1,
                "timestamp": 1,
                "isTrigger": False,
                "isPositionTpsl": False,
                "reduceOnly": False,
                "orderType": "Limit",
                "triggerCondition": "N/A",
                "triggerPx": "0.0",
            },
            {
                "coin": "BTC",
                "side": "B",
                "limitPx": "62101.86",
                "sz": "3",
                "origSz": "3",
                "oid": 2,
                "timestamp": 2,
                "isTrigger": False,
                "isPositionTpsl": False,
                "reduceOnly": False,
                "orderType": "Limit",
                "triggerCondition": "N/A",
                "triggerPx": "0.0",
            },
            {
                "coin": "BTC",
                "side": "A",
                "limitPx": "63726",
                "sz": "2",
                "origSz": "2",
                "oid": 3,
                "timestamp": 3,
                "isTrigger": False,
                "isPositionTpsl": False,
                "reduceOnly": False,
                "orderType": "Limit",
                "triggerCondition": "N/A",
                "triggerPx": "0.0",
            },
            {
                "coin": "HYPE",
                "side": "A",
                "limitPx": "74.931",
                "sz": "5",
                "origSz": "10",
                "oid": 4,
                "timestamp": 4,
                "isTrigger": False,
                "isPositionTpsl": False,
                "reduceOnly": False,
                "orderType": "Limit",
                "triggerCondition": "N/A",
                "triggerPx": "0.0",
            },
        ]

        def fake_lookup(alias: str):
            self.assertEqual(alias, "FLEX")
            return ("0x1111111111111111111111111111111111111111", "secret")

        def fake_post(payload: Dict[str, Any], timeout: int = 20):
            if payload["type"] == "clearinghouseState":
                return state_payload
            if payload["type"] == "frontendOpenOrders":
                return open_orders_payload
            if payload["type"] == "metaAndAssetCtxs":
                return market_meta_payload
            raise AssertionError(payload)

        self.patch("plugins.trade.agents.x_hyperliquid_agent._lookup_credentials", fake_lookup)
        self.patch("plugins.trade.agents.x_hyperliquid_agent._discover_perp_dex_names", lambda: [""])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._post_info", fake_post)
        response = _hl_module().execute({"operation": "positions_orders", "exchange": "hyperliquid", "account": "FLEX"})
        self.assertTrue(response.success)
        self.assertEqual(response.operation, "positions_orders")
        self.assertEqual(response.exchange, "hyperliquid")
        self.assertEqual(response.account, "FLEX")

        self.assertEqual(len(response.positions or []), 2)
        btc, hype = response.positions
        self.assertEqual(btc.side, "long")
        self.assertEqual(hype.side, "short")
        self.assertEqual(btc.size, "82.81931")
        self.assertEqual(hype.size, "18015.19")
        self.assertEqual(btc.pnl, "12539.01")
        self.assertEqual(hype.pnl, "215389.65")
        self.assertIsNone(btc.tp)
        self.assertIsNone(btc.sl)

        self.assertEqual(response.open_order_count, 4)
        self.assertEqual(len(response.order_groups or []), 3)
        groups = {(g.symbol, g.side): g for g in response.order_groups}
        self.assertEqual(groups[("BTC", "buy")].order_count, 2)
        self.assertEqual(groups[("BTC", "buy")].total_size, "4")
        self.assertEqual(groups[("BTC", "buy")].vwap, "62101.8")
        self.assertEqual(groups[("BTC", "buy")].min_price, "62101.74")
        self.assertEqual(groups[("BTC", "buy")].max_price, "62101.86")
        self.assertEqual(groups[("BTC", "sell")].order_count, 1)
        self.assertEqual(groups[("HYPE", "sell")].order_count, 1)
        self.assertEqual(groups[("HYPE", "sell")].total_size, "5")
        self.assertEqual(groups[("HYPE", "sell")].vwap, "74.93")
        self.assertEqual(groups[("HYPE", "sell")].min_price, "74.931")
        self.assertEqual(groups[("HYPE", "sell")].max_price, "74.931")

    def test_zero_positions_and_orders_render_empty(self):
        market_meta_payload = _market_meta_payload(("ABC", "1.00"))
        state_payload = {"assetPositions": []}
        open_orders_payload = []

        def fake_lookup(alias: str):
            return ("0x1111111111111111111111111111111111111111", "secret")

        def fake_post(payload: Dict[str, Any], timeout: int = 20):
            if payload["type"] == "clearinghouseState":
                return state_payload
            if payload["type"] == "frontendOpenOrders":
                return open_orders_payload
            if payload["type"] == "metaAndAssetCtxs":
                return market_meta_payload
            raise AssertionError(payload)

        self.patch("plugins.trade.agents.x_hyperliquid_agent._lookup_credentials", fake_lookup)
        self.patch("plugins.trade.agents.x_hyperliquid_agent._discover_perp_dex_names", lambda: [""])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._post_info", fake_post)
        response = _hl_module().execute({"operation": "positions_orders", "exchange": "hyperliquid", "account": "FLEX"})
        self.assertTrue(response.success)
        self.assertEqual(response.positions, [])
        self.assertEqual(response.open_order_count, 0)
        self.assertEqual(response.order_groups, [])

    def test_failure_is_generic(self):
        def fake_lookup(alias: str):
            return (None, None)
        self.patch("plugins.trade.agents.x_hyperliquid_agent._lookup_credentials", fake_lookup)
        response = _hl_module().execute({"operation": "positions_orders", "exchange": "hyperliquid", "account": "FLEX"})
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "ACCOUNT_NOT_CONFIGURED")
        self.assertNotIn("hyperliquid.xyz", response.error.message.lower())

    def test_final_vwap_uses_full_decimal_before_normalization(self):
        market_meta_payload = _market_meta_payload(("ABC", "1.00"))
        state_payload = {"assetPositions": []}
        open_orders_payload = [
            {
                "coin": "ABC",
                "side": "B",
                "limitPx": "1.111111",
                "sz": "2",
                "origSz": "2",
                "oid": 1,
                "timestamp": 1,
                "isTrigger": False,
                "isPositionTpsl": False,
                "reduceOnly": False,
                "orderType": "Limit",
                "triggerCondition": "N/A",
                "triggerPx": "0.0",
            },
            {
                "coin": "ABC",
                "side": "B",
                "limitPx": "2.222222",
                "sz": "1",
                "origSz": "1",
                "oid": 2,
                "timestamp": 2,
                "isTrigger": False,
                "isPositionTpsl": False,
                "reduceOnly": False,
                "orderType": "Limit",
                "triggerCondition": "N/A",
                "triggerPx": "0.0",
            },
        ]
        seen: List[Decimal] = []

        def fake_lookup(alias: str):
            return ("0x1111111111111111111111111111111111111111", "secret")

        def fake_post(payload: Dict[str, Any], timeout: int = 20):
            if payload["type"] == "clearinghouseState":
                return state_payload
            if payload["type"] == "frontendOpenOrders":
                return open_orders_payload
            if payload["type"] == "metaAndAssetCtxs":
                return market_meta_payload
            raise AssertionError(payload)

        module = _hl_module()
        original_format = module._format_decimal_places

        def spy_format(value: Decimal, places: int) -> str:
            seen.append(value)
            return original_format(value, places)

        self.patch("plugins.trade.agents.x_hyperliquid_agent._lookup_credentials", fake_lookup)
        self.patch("plugins.trade.agents.x_hyperliquid_agent._discover_perp_dex_names", lambda: [""])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._post_info", fake_post)
        self.patch("plugins.trade.agents.x_hyperliquid_agent._format_decimal_places", spy_format)
        response = module.execute({"operation": "positions_orders", "exchange": "hyperliquid", "account": "FLEX"})
        self.assertTrue(response.success)
        self.assertEqual(seen, [Decimal("1.481481333333333333333333333")])
        self.assertEqual(response.order_groups[0].vwap, "1.48")

    def test_no_float_conversion_occurs(self):
        market_meta_payload = _market_meta_payload(("ABC", "1.00"))
        state_payload = {"assetPositions": []}
        open_orders_payload = [
            {
                "coin": "ABC",
                "side": "B",
                "limitPx": "1.25",
                "sz": "1",
                "origSz": "1",
                "oid": 1,
                "timestamp": 1,
                "isTrigger": False,
                "isPositionTpsl": False,
                "reduceOnly": False,
                "orderType": "Limit",
                "triggerCondition": "N/A",
                "triggerPx": "0.0",
            }
        ]

        def fake_lookup(alias: str):
            return ("0x1111111111111111111111111111111111111111", "secret")

        def fake_post(payload: Dict[str, Any], timeout: int = 20):
            if payload["type"] == "clearinghouseState":
                return state_payload
            if payload["type"] == "frontendOpenOrders":
                return open_orders_payload
            if payload["type"] == "metaAndAssetCtxs":
                return market_meta_payload
            raise AssertionError(payload)

        self.patch("plugins.trade.agents.x_hyperliquid_agent._lookup_credentials", fake_lookup)
        self.patch("plugins.trade.agents.x_hyperliquid_agent._discover_perp_dex_names", lambda: [""])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._post_info", fake_post)

        def guard_float(*args, **kwargs):
            raise AssertionError("float() must not be called")

        with mock.patch("builtins.float", guard_float):
            response = _hl_module().execute({"operation": "positions_orders", "exchange": "hyperliquid", "account": "FLEX"})
        self.assertTrue(response.success)
        self.assertEqual(response.order_groups[0].vwap, "1.25")


class TestWizardPositionsOrdersRendering(unittest.TestCase):
    def test_positions_orders_screen_renders_generic_layout(self):
        response = make_success(
            operation="positions_orders",
            exchange="hyperliquid",
            account="FLEX",
            positions=[
                CanonicalPosition(
                    symbol="BTC",
                    side="long",
                    size="82.81931",
                    entry_price="64491.5",
                    pnl="12539.01",
                    tp=None,
                    sl=None,
                ),
                CanonicalPosition(
                    symbol="HYPE",
                    side="short",
                    size="18015.19",
                    entry_price="70.858",
                    pnl="-215389.65",
                    tp=None,
                    sl=None,
                ),
            ],
            open_order_count=4,
            order_groups=[
                CanonicalOrderGroup(
                    symbol="BTC",
                    side="buy",
                    order_count=2,
                    total_size="4",
                    vwap="61750",
                    min_price="61000",
                    max_price="62000",
                ),
                CanonicalOrderGroup(
                    symbol="BTC",
                    side="sell",
                    order_count=1,
                    total_size="2",
                    vwap="63726",
                    min_price="63726",
                    max_price="63726",
                ),
                CanonicalOrderGroup(
                    symbol="HYPE",
                    side="sell",
                    order_count=1,
                    total_size="5",
                    vwap="70.86",
                    min_price="70.86",
                    max_price="70.86",
                ),
            ],
        )
        desk = StubDesk(response)
        wizard = TradeWizard(tradedesk=desk)
        key = ("chat",)
        wizard.open(key)
        wizard.handle_callback(key, "exchange:hyperliquid")
        wizard.handle_callback(key, "account:FLEX")
        screen = wizard.handle_callback(key, "action:positions_orders")
        text = screen.text
        self.assertIn("📋 Open Orders & 💼 Positions", text)
        self.assertIn("hyperliquid / FLEX", text)
        self.assertIn("Current Positions", text)
        self.assertIn("🔵 BTC", text)
        self.assertIn("🔴 HYPE", text)
        self.assertIn("Open orders: 4", text)
        self.assertIn("VWAP", text)
        self.assertIn("—", text)
        self.assertNotIn("reduceOnly", text)
        self.assertNotIn("triggerPx", text)
        self.assertEqual(screen.buttons[0][0]["text"], "↻ Refresh")
        self.assertEqual(screen.buttons[1][0]["text"], "◀️ Back")
        self.assertEqual(screen.buttons[1][1]["text"], "✕ Exit")

    def test_empty_positions_and_orders_render_correctly(self):
        response = make_success(
            operation="positions_orders",
            exchange="hyperliquid",
            account="FLEX",
            positions=[],
            open_order_count=0,
            order_groups=[],
        )
        desk = StubDesk(response)
        wizard = TradeWizard(tradedesk=desk)
        key = ("chat",)
        wizard.open(key)
        wizard.handle_callback(key, "exchange:hyperliquid")
        wizard.handle_callback(key, "account:FLEX")
        screen = wizard.handle_callback(key, "action:positions_orders")
        self.assertIn("No open positions.", screen.text)
        self.assertIn("Open orders: 0", screen.text)
        self.assertIn("No open orders.", screen.text)

    def test_canonical_error_renders_generically(self):
        response = make_failure(
            operation="positions_orders",
            exchange="hyperliquid",
            account="FLEX",
            code="POSITIONS_ORDERS_UNAVAILABLE",
            message="Positions and orders unavailable.",
            exchange_reason="Signed action over weight limit while fetching orders.",
        )
        desk = StubDesk(response)
        wizard = TradeWizard(tradedesk=desk)
        key = ("chat",)
        wizard.open(key)
        wizard.handle_callback(key, "exchange:hyperliquid")
        wizard.handle_callback(key, "account:FLEX")
        screen = wizard.handle_callback(key, "action:positions_orders")
        self.assertIn("Error: Positions and orders unavailable.", screen.text)
        self.assertIn("Reason: Signed action over weight limit while fetching orders.", screen.text)
        self.assertIn("(POSITIONS_ORDERS_UNAVAILABLE)", screen.text)


class TestCanonicalContractExtensions(unittest.TestCase):
    def test_response_to_dict_includes_positions_and_orders(self):
        response = make_success(
            operation="positions_orders",
            exchange="hyperliquid",
            account="FLEX",
            positions=[CanonicalPosition("BTC", "long", "1", "10", "2")],
            open_order_count=1,
            order_groups=[CanonicalOrderGroup("BTC", "buy", 1, "1", "10", "10", "10")],
        )
        data = response.to_dict()
        self.assertIn("positions", data)
        self.assertIn("open_order_count", data)
        self.assertIn("order_groups", data)
        self.assertEqual(data["positions"][0]["symbol"], "BTC")
        self.assertEqual(data["order_groups"][0]["side"], "buy")


class TestBalanceStillWorks(unittest.TestCase):
    def test_balance_contract_unchanged(self):
        response = make_success(
            operation="balance",
            exchange="hyperliquid",
            account="FLEX",
            balance=None,
        )
        self.assertTrue(response.success)
        self.assertEqual(response.operation, "balance")


class TestGenericLayersAvoidPrecisionRules(unittest.TestCase):
    def test_wizard_and_tradedesk_do_not_hard_code_hyperliquid_precision(self):
        wizard_source = (_REPO_ROOT / "plugins" / "trade" / "wizard.py").read_text(encoding="utf-8")
        tradedesk_source = (_REPO_ROOT / "plugins" / "trade" / "tradedesk.py").read_text(encoding="utf-8")
        forbidden = [
            "markPx",
            "midPx",
            "prevDayPx",
            "oraclePx",
            "pricePrecision",
            "tickSize",
            "metaAndAssetCtxs",
            "l2Book",
            "szDecimals",
        ]
        for token in forbidden:
            self.assertNotIn(token, wizard_source)
            self.assertNotIn(token, tradedesk_source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
