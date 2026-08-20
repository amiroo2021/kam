"""Deterministic tests for /trade cancel-order-group flow.

These tests keep cancellation exact and exchange-owned:
- the wizard only groups open orders by Instrument + Side
- TradeDesk stays blind and only routes canonical requests
- the Hyperliquid agent selects exact target IDs and verifies by readback
"""

from __future__ import annotations

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



from plugins.trade.canonical import CanonicalOrderGroup, CanonicalPosition, CanonicalPositionActionResult, make_success  # noqa: E402
from plugins.trade.tradedesk import TradeDesk  # noqa: E402
from plugins.trade.wizard import TradeWizard  # noqa: E402


def _hl_module():
    return importlib.import_module("plugins.trade.agents.x_hyperliquid_agent")


class SequenceDesk:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: List[Dict[str, Any]] = []

    def list_exchanges(self) -> List[str]:
        return ["hyperliquid"]

    def list_accounts(self, exchange: str) -> List[str]:
        return ["FIBO"] if exchange == "hyperliquid" else []

    def execute(self, request: Dict[str, Any]):
        self.requests.append(dict(request))
        idx = min(len(self.requests) - 1, len(self.responses) - 1)
        return self.responses[idx]


class FakeExchange:
    def __init__(self, responses: List[Dict[str, Any]]):
        self.responses = list(responses)
        self.requests: List[List[Dict[str, Any]]] = []

    def bulk_cancel(self, cancel_requests):
        batch = [
            {
                "coin": req.get("coin"),
                "oid": req.get("oid"),
            }
            for req in cancel_requests
        ]
        self.requests.append(batch)
        idx = min(len(self.requests) - 1, len(self.responses) - 1)
        return self.responses[idx]

    def bulk_orders(self, order_requests, builder=None, grouping="na"):
        batch = [dict(req) for req in order_requests]
        self.requests.append(batch)
        idx = min(len(self.requests) - 1, len(self.responses) - 1)
        return self.responses[idx]

    def bulk_modify_orders_new(self, modify_requests):
        batch = [dict(req) for req in modify_requests]
        self.requests.append(batch)
        idx = min(len(self.requests) - 1, len(self.responses) - 1)
        return self.responses[idx]

    def market_close(self, coin, sz=None, px=None, slippage=None, builder=None):
        batch = [{"coin": coin, "sz": sz, "px": px, "slippage": slippage}]
        self.requests.append(batch)
        idx = min(len(self.requests) - 1, len(self.responses) - 1)
        return self.responses[idx]


def _group(symbol: str, side: str, count: int, total_size: str, vwap: str, min_price: str, max_price: str):
    return CanonicalOrderGroup(
        symbol=symbol,
        side=side,
        order_count=count,
        total_size=total_size,
        vwap=vwap,
        min_price=min_price,
        max_price=max_price,
    )


def _orders(symbol: str, side: str, oids: List[int]) -> List[Dict[str, Any]]:
    return [{"symbol": symbol, "side": side, "oid": oid} for oid in oids]


def _position_action_response(operation: str, symbol: str, *, price: str | None = None, removed: bool | None = None, verified: bool = True, message: str | None = None, status: str = "success", current_side: str | None = None, current_size: str | None = None, exchange_order_id: int | None = None):
    return make_success(
        operation=operation,
        exchange="hyperliquid",
        account="FIBO",
        position_action=CanonicalPositionActionResult(
            operation=operation,
            symbol=symbol,
            verified=verified,
            price=price,
            removed=removed,
            status=status,
            exchange_order_id=exchange_order_id,
            current_side=current_side,
            current_size=current_size,
            message=message,
        ),
    )


class TestCancelOrdersWizard(unittest.TestCase):
    def test_cancel_orders_menu_is_grouped_and_refreshes_freshly(self):
        first = make_success(
            operation="positions_orders",
            exchange="hyperliquid",
            account="FIBO",
            open_order_count=3,
            order_groups=[
                _group("BTC", "buy", 1, "1", "65000", "65000", "65000"),
                _group("HYPE", "sell", 2, "2", "8", "7", "9"),
            ],
        )
        second = make_success(
            operation="positions_orders",
            exchange="hyperliquid",
            account="FIBO",
            open_order_count=1,
            order_groups=[
                _group("SOL", "buy", 1, "1", "50", "50", "50"),
            ],
        )
        desk = SequenceDesk([first, second])
        wizard = TradeWizard(tradedesk=desk)  # type: ignore[arg-type]
        key = ("chat",)
        wizard.open(key)
        wizard.handle_callback(key, "exchange:hyperliquid")
        wizard.handle_callback(key, "account:FIBO")

        screen = wizard.handle_callback(key, "action:cancel_orders")
        self.assertIn("Cancel Orders", screen.text)
        self.assertIn("Open orders: 3", screen.text)
        self.assertIn("BTC buy", screen.text)
        self.assertIn("HYPE sell", screen.text)
        self.assertNotIn("Cancel All", screen.text)
        labels = [btn["text"] for row in screen.buttons for btn in row]
        self.assertEqual(labels.count("↻ Refresh"), 0)
        self.assertEqual(len([label for label in labels if "orders" in label]), 2)
        self.assertEqual(desk.requests[-1], {
            "operation": "positions_orders",
            "exchange": "hyperliquid",
            "account": "FIBO",
        })

        refreshed = wizard.handle_callback(key, "refresh")
        self.assertIn("Open orders: 1", refreshed.text)
        self.assertIn("SOL buy", refreshed.text)
        self.assertNotIn("BTC buy", refreshed.text)
        self.assertEqual(len(desk.requests), 2)

    def test_cancel_orders_menu_handles_empty_state(self):
        response = make_success(
            operation="positions_orders",
            exchange="hyperliquid",
            account="FIBO",
            open_order_count=0,
            order_groups=[],
        )
        desk = SequenceDesk([response])
        wizard = TradeWizard(tradedesk=desk)  # type: ignore[arg-type]
        key = ("chat",)
        wizard.open(key)
        wizard.handle_callback(key, "exchange:hyperliquid")
        wizard.handle_callback(key, "account:FIBO")

        screen = wizard.handle_callback(key, "action:cancel_orders")
        self.assertIn("❌ Cancel Orders -- hyperliquid / FIBO", screen.text)
        self.assertIn("Open orders: 0", screen.text)
        self.assertIn("No open orders.", screen.text)
        self.assertEqual(len([btn for row in screen.buttons for btn in row if btn["text"] == "↻ Refresh"]), 0)

    def test_cancel_group_requires_confirmation_before_submit(self):
        response = make_success(
            operation="positions_orders",
            exchange="hyperliquid",
            account="FIBO",
            open_order_count=2,
            order_groups=[_group("HYPE", "sell", 2, "2", "8", "7", "9")],
        )
        desk = SequenceDesk([response, response])
        wizard = TradeWizard(tradedesk=desk)  # type: ignore[arg-type]
        key = ("chat",)
        wizard.open(key)
        wizard.handle_callback(key, "exchange:hyperliquid")
        wizard.handle_callback(key, "account:FIBO")
        wizard.handle_callback(key, "action:cancel_orders")

        selected = wizard.handle_callback(key, "cancel_group:HYPE:sell")
        self.assertIn("Confirm Cancellation", selected.text)
        self.assertIn("🔴 HYPE sell", selected.text)
        self.assertIn("Orders: 2", selected.text)
        self.assertIn("Total size: 2", selected.text)
        self.assertIn("VWAP: 8", selected.text)
        self.assertIn("Range: 7-9", selected.text)
        self.assertIn("Cancel this HYPE Sell group?", selected.text)
        self.assertEqual(desk.requests[-1]["operation"], "positions_orders")

        confirmed = wizard.handle_callback(key, "confirm")
        self.assertIn("Cancel Orders", confirmed.text)
        self.assertEqual(desk.requests[-1], {
            "operation": "cancel_order_group",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "HYPE",
            "side": "sell",
        })

    def test_cancel_menu_never_exposes_raw_order_ids(self):
        response = make_success(
            operation="positions_orders",
            exchange="hyperliquid",
            account="FIBO",
            open_order_count=1,
            order_groups=[_group("HYPE", "sell", 1, "1", "8", "8", "8")],
        )
        desk = SequenceDesk([response])
        wizard = TradeWizard(tradedesk=desk)  # type: ignore[arg-type]
        key = ("chat",)
        wizard.open(key)
        wizard.handle_callback(key, "exchange:hyperliquid")
        wizard.handle_callback(key, "account:FIBO")
        screen = wizard.handle_callback(key, "action:cancel_orders")
        for token in ["123456", "987654", "oid", "order id"]:
            self.assertNotIn(token, screen.text.lower())
        button_texts = " ".join(btn["text"] for row in screen.buttons for btn in row)
        self.assertNotIn("oid", button_texts.lower())


class TestPositionsManagementWizard(unittest.TestCase):
    def _open_positions_management(self, desk):
        wizard = TradeWizard(tradedesk=desk)  # type: ignore[arg-type]
        key = ("chat",)
        wizard.open(key)
        wizard.handle_callback(key, "exchange:hyperliquid")
        wizard.handle_callback(key, "account:FIBO")
        return wizard, key

    def _positions_response(self, positions):
        return make_success(
            operation="positions_management",
            exchange="hyperliquid",
            account="FIBO",
            positions=positions,
        )

    def test_positions_management_lists_positions_and_buttons(self):
        response = self._positions_response([
            CanonicalPosition(symbol="HYPE", side="short", size="319.84", entry_price="71.075", pnl="+3573.91", tp=None, sl=None),
            CanonicalPosition(symbol="BTC", side="long", size="2.15", entry_price="64491.5", pnl="+1253.40", tp="67500", sl="62000"),
        ])
        desk = SequenceDesk([response])
        wizard, key = self._open_positions_management(desk)

        screen = wizard.handle_callback(key, "action:positions_management")
        self.assertIn("💼 Positions Management — hyperliquid / FIBO", screen.text)
        self.assertIn("Current Positions", screen.text)
        self.assertIn("🔴 HYPE", screen.text)
        self.assertIn("🔵 BTC", screen.text)
        self.assertIn("TP: —", screen.text)
        self.assertIn("SL: —", screen.text)
        labels = [btn["text"] for row in screen.buttons for btn in row]
        self.assertIn("🔴 HYPE Short", labels)
        self.assertIn("🔵 BTC Long", labels)
        self.assertIn("◀️ Back", labels)
        self.assertIn("✕ Exit", labels)
        self.assertEqual(desk.requests[-1], {
            "operation": "positions_management",
            "exchange": "hyperliquid",
            "account": "FIBO",
        })

    def test_positions_management_handles_empty_state(self):
        desk = SequenceDesk([self._positions_response([])])
        wizard, key = self._open_positions_management(desk)

        screen = wizard.handle_callback(key, "action:positions_management")
        self.assertIn("No open positions.", screen.text)
        labels = [btn["text"] for row in screen.buttons for btn in row]
        self.assertEqual(labels, ["◀️ Back", "✕ Exit"])

    def test_positions_management_opens_detail_screen(self):
        desk = SequenceDesk([self._positions_response([
            CanonicalPosition(symbol="HYPE", side="short", size="319.84", entry_price="71.075", pnl="+3573.91", tp=None, sl=None),
            CanonicalPosition(symbol="BTC", side="long", size="2.15", entry_price="64491.5", pnl="+1253.40", tp="67500", sl="62000"),
        ])])
        wizard, key = self._open_positions_management(desk)
        wizard.handle_callback(key, "action:positions_management")

        detail = wizard.handle_callback(key, "position:HYPE:short")
        self.assertIn("💼 HYPE Position — hyperliquid / FIBO", detail.text)
        self.assertIn("🔴 Short", detail.text)
        self.assertIn("Size: 319.84", detail.text)
        self.assertIn("Entry: 71.075", detail.text)
        self.assertIn("PnL: +3573.91", detail.text)
        self.assertIn("TP: —", detail.text)
        self.assertIn("SL: —", detail.text)
        detail_labels = [btn["text"] for row in detail.buttons for btn in row]
        self.assertIn("Set TP", detail_labels)
        self.assertIn("Set SL", detail_labels)
        self.assertIn("Close Position", detail_labels)

    def test_positions_management_tp_entry_confirmation_writes_and_verifies(self):
        read = self._positions_response([
            CanonicalPosition(symbol="HYPE", side="short", size="319.84", entry_price="71.075", pnl="+3573.91", tp=None, sl=None),
        ])
        write = _position_action_response(
            "set_tp",
            "HYPE",
            price="65",
            current_side="short",
            current_size="319.84",
            message="Take Profit updated.",
        )
        desk = SequenceDesk([read, write])
        wizard, key = self._open_positions_management(desk)
        wizard.handle_callback(key, "action:positions_management")
        wizard.handle_callback(key, "position:HYPE:short")

        entry = wizard.handle_callback(key, "set_tp")
        self.assertIn("Set Take Profit", entry.text)
        self.assertIn("Enter TP Price:", entry.text)
        self.assertIn("Enter 0 to remove TP.", entry.text)
        self.assertIsNotNone(wizard.handle_text(key, "invalid"))

        confirm = wizard.handle_text(key, "65")
        self.assertIn("⚠️ Confirm Take Profit", confirm.text)
        self.assertIn("Current TP: —", confirm.text)
        self.assertIn("New TP: 65", confirm.text)
        result = wizard.handle_callback(key, "confirm")
        self.assertIn("✅ Take Profit Updated", result.text)
        self.assertIn("Verified: Passed", result.text)
        self.assertIn("TP: 65", result.text)
        self.assertEqual(len(desk.requests), 2)
        self.assertEqual(desk.requests[-1], {
            "operation": "set_tp",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "HYPE",
            "price": "65",
        })

    def test_positions_management_tp_zero_removal_writes_and_verifies(self):
        read = self._positions_response([
            CanonicalPosition(symbol="HYPE", side="short", size="319.84", entry_price="71.075", pnl="+3573.91", tp="65", sl=None),
        ])
        write = _position_action_response(
            "set_tp",
            "HYPE",
            removed=True,
            current_side="short",
            current_size="319.84",
            message="Take Profit removed.",
        )
        desk = SequenceDesk([read, write])
        wizard, key = self._open_positions_management(desk)
        wizard.handle_callback(key, "action:positions_management")
        wizard.handle_callback(key, "position:HYPE:short")
        wizard.handle_callback(key, "set_tp")

        confirm = wizard.handle_text(key, "0")
        self.assertIn("⚠️ Confirm Remove Take Profit", confirm.text)
        self.assertIn("Current TP: 65", confirm.text)
        self.assertIn("Confirm Remove", [btn["text"] for row in confirm.buttons for btn in row])
        result = wizard.handle_callback(key, "confirm")
        self.assertIn("✅ Take Profit Removed", result.text)
        self.assertIn("Verified: Passed", result.text)
        self.assertEqual(len(desk.requests), 2)
        self.assertEqual(desk.requests[-1], {
            "operation": "set_tp",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "HYPE",
            "price": "0",
        })

    def test_positions_management_sl_entry_confirmation_writes_and_verifies(self):
        read = self._positions_response([
            CanonicalPosition(symbol="BTC", side="long", size="2.15", entry_price="64491.5", pnl="+1253.40", tp="67500", sl=None),
        ])
        write = _position_action_response(
            "set_sl",
            "BTC",
            price="62000",
            current_side="long",
            current_size="2.15",
            message="Stop Loss updated.",
        )
        desk = SequenceDesk([read, write])
        wizard, key = self._open_positions_management(desk)
        wizard.handle_callback(key, "action:positions_management")
        wizard.handle_callback(key, "position:BTC:long")

        entry = wizard.handle_callback(key, "set_sl")
        self.assertIn("Set Stop Loss", entry.text)
        self.assertIsNotNone(wizard.handle_text(key, "not-a-number"))

        confirm = wizard.handle_text(key, "62000")
        self.assertIn("⚠️ Confirm Stop Loss", confirm.text)
        self.assertIn("Current SL: —", confirm.text)
        self.assertIn("New SL: 62000", confirm.text)
        result = wizard.handle_callback(key, "confirm")
        self.assertIn("✅ Stop Loss Updated", result.text)
        self.assertIn("Verified: Passed", result.text)
        self.assertEqual(len(desk.requests), 2)
        self.assertEqual(desk.requests[-1], {
            "operation": "set_sl",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "BTC",
            "price": "62000",
        })

    def test_positions_management_sl_zero_removal_writes_and_verifies(self):
        read = self._positions_response([
            CanonicalPosition(symbol="BTC", side="long", size="2.15", entry_price="64491.5", pnl="+1253.40", tp="67500", sl="62000"),
        ])
        write = _position_action_response(
            "set_sl",
            "BTC",
            removed=True,
            current_side="long",
            current_size="2.15",
            message="Stop Loss removed.",
        )
        desk = SequenceDesk([read, write])
        wizard, key = self._open_positions_management(desk)
        wizard.handle_callback(key, "action:positions_management")
        wizard.handle_callback(key, "position:BTC:long")
        wizard.handle_callback(key, "set_sl")

        confirm = wizard.handle_text(key, "0")
        self.assertIn("⚠️ Confirm Remove Stop Loss", confirm.text)
        self.assertIn("Current SL: 62,000", confirm.text)
        self.assertIn("Confirm Remove", [btn["text"] for row in confirm.buttons for btn in row])
        result = wizard.handle_callback(key, "confirm")
        self.assertIn("✅ Stop Loss Removed", result.text)
        self.assertIn("Verified: Passed", result.text)
        self.assertEqual(len(desk.requests), 2)
        self.assertEqual(desk.requests[-1], {
            "operation": "set_sl",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "BTC",
            "price": "0",
        })

    def test_positions_management_close_confirmation_writes_and_refreshes(self):
        read = self._positions_response([
            CanonicalPosition(symbol="HYPE", side="short", size="319.84", entry_price="71.075", pnl="+3573.91", tp=None, sl=None),
        ])
        write = _position_action_response(
            "close_position",
            "HYPE",
            current_side="short",
            current_size="319.84",
            message="Position closed.",
        )
        desk = SequenceDesk([read, write])
        wizard, key = self._open_positions_management(desk)
        wizard.handle_callback(key, "action:positions_management")
        wizard.handle_callback(key, "position:HYPE:short")

        confirm = wizard.handle_callback(key, "close_position")
        self.assertIn("⚠️ Confirm Close Position", confirm.text)
        self.assertIn("Displayed Size: 319.84", confirm.text)
        self.assertIn("Confirm Close", [btn["text"] for row in confirm.buttons for btn in row])
        result = wizard.handle_callback(key, "confirm")
        self.assertIn("✅ Position Closed", result.text)
        self.assertIn("Verified: Passed", result.text)
        self.assertEqual(len(desk.requests), 2)
        self.assertEqual(desk.requests[-1], {
            "operation": "close_position",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "HYPE",
        })

    def test_positions_management_text_only_intercepts_tp_and_sl_states(self):
        desk = SequenceDesk([self._positions_response([
            CanonicalPosition(symbol="HYPE", side="short", size="319.84", entry_price="71.075", pnl="+3573.91", tp=None, sl=None),
        ])])
        wizard, key = self._open_positions_management(desk)
        wizard.handle_callback(key, "action:positions_management")
        wizard.handle_callback(key, "position:HYPE:short")
        self.assertIsNone(wizard.handle_text(key, "65"))
        wizard.handle_callback(key, "set_tp")
        self.assertIsNotNone(wizard.handle_text(key, "65"))
        wizard.handle_callback(key, "back")
        wizard.handle_callback(key, "set_sl")
        self.assertIsNotNone(wizard.handle_text(key, "75"))

    def setUp(self):
        self._patches = []

    def tearDown(self):
        for patch in reversed(self._patches):
            patch.stop()

    def patch(self, target: str, value: Any):
        patch = mock.patch(target, value)
        self._patches.append(patch)
        return patch.start()

    def test_exact_target_ids_only_hype_sell_are_cancelled(self):
        hl = _hl_module()
        candidate = {
            "dex": "",
            "dex_index": 0,
            "internal_name": "HYPE",
            "public_symbol": "HYPE",
            "public_key": "HYPE",
            "internal_key": "HYPE",
            "display_name": "HYPE-USDC",
            "price_increment": "0.1",
            "size_increment": "0.01",
        }
        target_ids = list(range(1, 186))
        pre_orders = (
            _orders("HYPE", "sell", target_ids)
            + _orders("HYPE", "buy", [2001, 2002])
            + _orders("BTC", "buy", [3001])
            + _orders("BTC", "sell", [3002])
            + _orders("SOL", "buy", [4001])
        )
        post_orders = _orders("HYPE", "buy", [2001, 2002]) + _orders("BTC", "buy", [3001]) + _orders("BTC", "sell", [3002]) + _orders("SOL", "buy", [4001])
        fake_exchange = FakeExchange([
            {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"] * 185}}}
        ])

        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", mock.Mock(side_effect=[pre_orders, post_orders]))

        response = hl.execute({"operation": "cancel_order_group", "exchange": "hyperliquid", "account": "FIBO", "symbol": "HYPE", "side": "sell"})
        self.assertTrue(response.success)
        self.assertIsNotNone(response.cancel_group)
        self.assertTrue(response.cancel_group.verified)
        self.assertEqual(response.cancel_group.targeted_order_count, 185)
        self.assertEqual(response.cancel_group.cancelled_order_count, 185)
        self.assertEqual(response.cancel_group.remaining_target_count, 0)
        self.assertEqual(len(fake_exchange.requests), 1)
        self.assertEqual(len(fake_exchange.requests[0]), 185)
        self.assertTrue(all(req["coin"] == "HYPE" for req in fake_exchange.requests[0]))
        self.assertEqual({req["oid"] for req in fake_exchange.requests[0]}, set(target_ids))

    def test_chunking_stops_after_failure_and_no_retry_occurs(self):
        hl = _hl_module()
        candidate = {
            "dex": "",
            "dex_index": 0,
            "internal_name": "HYPE",
            "public_symbol": "HYPE",
            "public_key": "HYPE",
            "internal_key": "HYPE",
            "display_name": "HYPE-USDC",
            "price_increment": "0.1",
            "size_increment": "0.01",
        }
        target_ids = list(range(1, 202))
        pre_orders = _orders("HYPE", "sell", target_ids)
        post_orders = _orders("HYPE", "sell", [201])
        fake_exchange = FakeExchange([
            {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"] * 200}}},
            {"status": "ok", "response": {"type": "cancel", "data": {"statuses": [{"error": "Order was never placed, already canceled, or filled."}]}}},
        ])

        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", mock.Mock(side_effect=[pre_orders, post_orders]))

        response = hl.execute({"operation": "cancel_order_group", "exchange": "hyperliquid", "account": "FIBO", "symbol": "HYPE", "side": "sell"})
        self.assertFalse(response.success)
        self.assertIsNotNone(response.cancel_group)
        self.assertTrue(response.cancel_group.partial)
        self.assertEqual(response.cancel_group.batch_count, 2)
        self.assertEqual(len(fake_exchange.requests), 2)
        self.assertEqual(len(fake_exchange.requests[0]), 200)
        self.assertEqual(len(fake_exchange.requests[1]), 1)
        self.assertEqual(response.cancel_group.remaining_target_count, 1)
        self.assertFalse(response.cancel_group.verified)

    def test_no_cancel_all_strings_exist_in_wizard_or_tradedesk(self):
        wizard_text = (Path(_REPO_ROOT) / "plugins/trade/wizard.py").read_text()
        desk_text = (Path(_REPO_ROOT) / "plugins/trade/tradedesk.py").read_text()
        self.assertNotIn("cancel_all_orders", wizard_text)
        self.assertNotIn("cancel_all_orders", desk_text)
        self.assertNotIn("Cancel All", wizard_text)
        self.assertNotIn("Cancel All", desk_text)

    def test_partial_verification_failure_is_not_reported_as_success(self):
        hl = _hl_module()
        candidate = {
            "dex": "",
            "dex_index": 0,
            "internal_name": "HYPE",
            "public_symbol": "HYPE",
            "public_key": "HYPE",
            "internal_key": "HYPE",
            "display_name": "HYPE-USDC",
            "price_increment": "0.1",
            "size_increment": "0.01",
        }
        pre_orders = _orders("HYPE", "sell", [1, 2, 3]) + _orders("BTC", "buy", [9])
        post_orders = _orders("HYPE", "sell", []) + _orders("BTC", "buy", [])
        fake_exchange = FakeExchange([
            {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success", "success", "success"]}}}
        ])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", mock.Mock(side_effect=[pre_orders, post_orders]))
        response = hl.execute({"operation": "cancel_order_group", "exchange": "hyperliquid", "account": "FIBO", "symbol": "HYPE", "side": "sell"})
        self.assertFalse(response.success)
        self.assertIsNotNone(response.cancel_group)
        self.assertFalse(response.cancel_group.verified)
        self.assertEqual(response.cancel_group.confirmed_absent_count, 3)
        self.assertEqual(response.cancel_group.remaining_target_count, 0)


class TestLadderExecution(unittest.TestCase):
    def setUp(self):
        self._patches = []

    def tearDown(self):
        for patcher in reversed(self._patches):
            patcher.stop()

    def patch(self, target: str, value: Any):
        patcher = mock.patch(target, value)
        self._patches.append(patcher)
        return patcher.start()

    def test_positions_management_alias_returns_canonical_positions(self):
        hl = _hl_module()
        positions = [
            CanonicalPosition(symbol="HYPE", side="short", size="319.84", entry_price="71.075", pnl="+3573.91", tp="65", sl=None),
            CanonicalPosition(symbol="BTC", side="long", size="2.15", entry_price="64491.5", pnl="+1253.40", tp="67500", sl="62000"),
        ]
        self.patch(
            "plugins.trade.agents.x_hyperliquid_agent._execute_positions_orders",
            lambda account, request: make_success(
                operation="positions_orders",
                exchange="hyperliquid",
                account=account,
                positions=positions,
                open_order_count=0,
                order_groups=[],
            ),
        )
        response = hl.execute({"operation": "positions_management", "exchange": "hyperliquid", "account": "FIBO"})
        self.assertTrue(response.success)
        self.assertEqual(response.operation, "positions_management")
        self.assertEqual(response.exchange, "hyperliquid")
        self.assertEqual(response.account, "FIBO")
        self.assertIsNotNone(response.positions)
        self.assertEqual([(p.symbol, p.side, p.tp, p.sl) for p in response.positions], [("HYPE", "short", "65", None), ("BTC", "long", "67500", "62000")])

    def _candidate(self, price_increment: str = "0.1", size_increment: str = "0.01") -> Dict[str, Any]:
        return {
            "dex": "",
            "dex_index": 0,
            "internal_name": "BTC",
            "public_symbol": "BTC",
            "public_key": "BTC",
            "internal_key": "BTC",
            "display_name": "BTC-USDC",
            "price_increment": price_increment,
            "size_increment": size_increment,
            "sz_decimals": 2,
        }

    def _response_for_orders(self, order_count: int, prefix: str = "oid", oid_offset: int = 10_000) -> Dict[str, Any]:
        return {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [{"resting": {"oid": oid_offset + i}} for i in range(order_count)]
                },
            },
        }

    def _snapshot_from_exchange(self, exchange: FakeExchange) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for batch_index, batch in enumerate(exchange.requests):
            for order_index, order in enumerate(batch):
                rows.append(
                    {
                        "symbol": order["coin"],
                        "side": "buy" if order["is_buy"] else "sell",
                        "oid": 10_000 + (batch_index * 1_000) + order_index,
                        "sz": str(order["sz"]),
                        "limitPx": str(order["limit_px"]),
                    }
                )
        return rows

    def _snapshot_from_first_batch(self, exchange: FakeExchange, oid_offset: int = 10_000, as_strings: bool = False) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        if not exchange.requests:
            return rows
        for index, order in enumerate(exchange.requests[0]):
            oid: Any = oid_offset + index
            if as_strings:
                oid = str(oid)
            rows.append(
                {
                    "symbol": order["coin"],
                    "side": "buy" if order["is_buy"] else "sell",
                    "oid": oid,
                    "sz": str(order["sz"]),
                    "limitPx": str(order["limit_px"]),
                }
            )
        return rows

    def _sol_candidate(self, price_increment: str = "0.1", size_increment: str = "0.01") -> Dict[str, Any]:
        return {
            "dex": "",
            "dex_index": 0,
            "internal_name": "SOL",
            "public_symbol": "SOL",
            "public_key": "SOL",
            "internal_key": "SOL",
            "display_name": "SOL-USDC",
            "price_increment": price_increment,
            "size_increment": size_increment,
            "sz_decimals": 2,
        }

    def test_uniform_ladder_quantizes_and_preserves_total_volume(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([self._response_for_orders(5)])
        candidate = self._candidate(price_increment="0.1", size_increment="0.01")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", lambda wallet: self._snapshot_from_exchange(fake_exchange))
        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "BTC",
            "side": "buy",
            "distribution": "uniform",
            "order_count": 5,
            "total_volume": "10",
            "start_price": "10",
            "end_price": "8",
        })
        self.assertTrue(response.success)
        self.assertIsNotNone(response.ladder)
        self.assertEqual(response.ladder.batch_count, 1)
        self.assertEqual(response.ladder.submitted_order_count, 5)
        self.assertEqual(response.ladder.submitted_volume, "10.00")
        self.assertEqual(response.ladder.accepted_child_count, 5)
        self.assertEqual(len(fake_exchange.requests), 1)
        batch = fake_exchange.requests[0]
        self.assertEqual(len(batch), 5)
        sizes = [Decimal(str(order["sz"])) for order in batch]
        prices = [Decimal(str(order["limit_px"])) for order in batch]
        self.assertEqual(sum(sizes), Decimal("10.00"))
        self.assertTrue(all(size == Decimal("2.00") for size in sizes))
        self.assertEqual(prices, [Decimal("10.0"), Decimal("9.5"), Decimal("9.0"), Decimal("8.5"), Decimal("8.0")])

    def test_ladder_accepts_response_objects_with_json_method(self):
        hl = _hl_module()

        class JsonResponse:
            def json(self):
                return {"response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 12345}}, {"resting": {"oid": 12346}}]}}}

        class StrictExchange:
            def __init__(self):
                self.requests: List[List[Dict[str, Any]]] = []

            def bulk_orders(self, orders):
                self.requests.append(orders)
                return JsonResponse()

        fake_exchange = StrictExchange()
        candidate = self._candidate(price_increment="0.1", size_increment="0.01")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", lambda wallet: [
            {"symbol": order["coin"], "side": "buy" if order["is_buy"] else "sell", "oid": 12345 + i, "sz": str(order["sz"]), "limitPx": str(order["limit_px"])}
            for i, order in enumerate(fake_exchange.requests[0])
        ] if fake_exchange.requests else [])
        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "SOL",
            "side": "sell",
            "distribution": "uniform",
            "order_count": 2,
            "total_volume": "2",
            "start_price": "100",
            "end_price": "120",
        })
        self.assertTrue(response.success)
        self.assertIsNotNone(response.ladder)
        self.assertEqual(response.ladder.submitted_order_count, 2)
        self.assertEqual(len(fake_exchange.requests), 1)
        self.assertEqual(len(fake_exchange.requests[0]), 2)

    def test_ladder_serializes_numeric_payloads_for_exchange_client(self):
        hl = _hl_module()

        class StrictExchange:
            def __init__(self):
                self.requests: List[List[Dict[str, Any]]] = []

            def bulk_orders(self, orders):
                self.requests.append(orders)
                for order in orders:
                    assert isinstance(order["sz"], float)
                    assert isinstance(order["limit_px"], float)
                return {"response": {"data": {"statuses": [{"resting": {"oid": 9001 + i}} for i in range(len(orders))]}}}

        fake_exchange = StrictExchange()
        candidate = self._candidate(price_increment="0.1", size_increment="0.01")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", lambda wallet: [
            {"symbol": order["coin"], "side": "buy" if order["is_buy"] else "sell", "oid": 9001 + i, "sz": str(order["sz"]), "limitPx": str(order["limit_px"])}
            for i, order in enumerate(fake_exchange.requests[0] if fake_exchange.requests else [])
        ])
        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "SOL",
            "side": "sell",
            "distribution": "uniform",
            "order_count": 2,
            "total_volume": "2",
            "start_price": "100",
            "end_price": "120",
        })
        self.assertTrue(response.success)
        self.assertEqual(len(fake_exchange.requests), 1)
        self.assertTrue(all(isinstance(order["sz"], float) for order in fake_exchange.requests[0]))
        self.assertTrue(all(isinstance(order["limit_px"], float) for order in fake_exchange.requests[0]))

    def test_half_gaussian_ladder_biases_size_toward_end_price(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([self._response_for_orders(5)])
        candidate = self._candidate(price_increment="0.1", size_increment="0.01")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", lambda wallet: self._snapshot_from_exchange(fake_exchange))
        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "BTC",
            "side": "sell",
            "distribution": "half_gaussian",
            "order_count": 5,
            "total_volume": "100",
            "start_price": "80",
            "end_price": "100",
        })
        self.assertTrue(response.success)
        batch = fake_exchange.requests[0]
        sizes = [Decimal(str(order["sz"])) for order in batch]
        self.assertEqual(sum(sizes), Decimal("100.00"))
        self.assertLess(sizes[0], sizes[-1])
        self.assertTrue(all(left <= right for left, right in zip(sizes, sizes[1:])))

    def test_half_gaussian_orientation_is_the_same_for_buy_and_sell(self):
        hl = _hl_module()
        candidate = self._candidate(price_increment="0.1", size_increment="0.01")
        scenarios = [
            {"side": "buy", "start_price": "100", "end_price": "80"},
            {"side": "sell", "start_price": "80", "end_price": "100"},
        ]
        for scenario in scenarios:
            with self.subTest(side=scenario["side"]):
                fake_exchange = FakeExchange([self._response_for_orders(5)])
                self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account, fx=fake_exchange: (fx, "0x1111111111111111111111111111111111111111", "secret"))
                self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates, cand=candidate: (cand, ""))
                self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", lambda wallet, fx=fake_exchange: self._snapshot_from_exchange(fx))
                response = hl.execute({
                    "operation": "ladder",
                    "exchange": "hyperliquid",
                    "account": "FIBO",
                    "symbol": "BTC",
                    "side": scenario["side"],
                    "distribution": "half_gaussian",
                    "order_count": 5,
                    "total_volume": "100",
                    **scenario,
                })
                self.assertTrue(response.success)
                batch = fake_exchange.requests[0]
                sizes = [Decimal(str(order["sz"])) for order in batch]
                self.assertTrue(all(left <= right for left, right in zip(sizes, sizes[1:])))
                self.assertLess(sizes[0], sizes[-1])

    def test_duplicate_quantized_prices_are_merged_deterministically(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([self._response_for_orders(2)])
        candidate = self._candidate(price_increment="1", size_increment="0.01")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", lambda wallet: self._snapshot_from_exchange(fake_exchange))
        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "BTC",
            "side": "sell",
            "distribution": "uniform",
            "order_count": 4,
            "total_volume": "4",
            "start_price": "10.00004",
            "end_price": "10.00045",
        })
        self.assertTrue(response.success)
        self.assertIsNotNone(response.ladder)
        self.assertEqual(response.ladder.submitted_order_count, 2)
        self.assertEqual(response.ladder.accepted_child_count, 2)
        self.assertEqual(len(fake_exchange.requests), 1)
        self.assertEqual(len(fake_exchange.requests[0]), 2)
        self.assertEqual(Decimal(str(fake_exchange.requests[0][0]["sz"])), Decimal("3.00"))
        self.assertEqual(Decimal(str(fake_exchange.requests[0][1]["sz"])), Decimal("1.00"))
        self.assertEqual([Decimal(str(order["limit_px"])) for order in fake_exchange.requests[0]], [Decimal("10.0"), Decimal("10.001")])

    def test_ladder_batches_at_boundaries(self):
        hl = _hl_module()
        candidate = self._candidate(price_increment="1", size_increment="1")
        cases = [
            (199, [199]),
            (200, [200]),
            (201, [200, 1]),
            (313, [200, 113]),
            (400, [200, 200]),
        ]
        for order_count, expected_batches in cases:
            with self.subTest(order_count=order_count):
                fake_exchange = FakeExchange([self._response_for_orders(size, oid_offset=10_000 + (batch_index * 1_000)) for batch_index, size in enumerate(expected_batches)])
                self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account, fx=fake_exchange: (fx, "0x1111111111111111111111111111111111111111", "secret"))
                self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates, cand=candidate: (cand, ""))
                self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", lambda wallet, fx=fake_exchange: self._snapshot_from_exchange(fx))
                response = hl.execute({
                    "operation": "ladder",
                    "exchange": "hyperliquid",
                    "account": "FIBO",
                    "symbol": "BTC",
                    "side": "buy",
                    "distribution": "uniform",
                    "order_count": order_count,
                    "total_volume": str(order_count),
                    "start_price": str(order_count * 10),
                    "end_price": "10",
                })
                self.assertTrue(response.success)
                self.assertEqual(response.ladder.batch_count, len(expected_batches))
                self.assertEqual(response.ladder.submitted_order_count, order_count)
                self.assertEqual(len(fake_exchange.requests), len(expected_batches))
                self.assertEqual([len(batch) for batch in fake_exchange.requests], expected_batches)

    def test_ladder_preserves_definite_exchange_rejection_reason(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([
            {"status": "err", "response": "Insufficient margin for order placement."}
        ])
        candidate = self._candidate(price_increment="1", size_increment="1")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", lambda wallet: [])
        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "BTC",
            "side": "sell",
            "distribution": "uniform",
            "order_count": 2,
            "total_volume": "10",
            "start_price": "10",
            "end_price": "11",
        })
        self.assertFalse(response.success)
        self.assertIsNotNone(response.error)
        self.assertEqual(response.error.code, "INSUFFICIENT_MARGIN")
        self.assertEqual(response.error.message, "Hyperliquid rejected the ladder.")
        self.assertEqual(response.error.exchange_reason, "Insufficient margin for order placement.")
        self.assertIsNotNone(response.ladder)
        self.assertFalse(response.ladder.partial)
        self.assertEqual(len(fake_exchange.requests), 1)

    def test_ladder_maps_weight_limit_rejection_and_preserves_reason(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([
            {"status": "err", "response": "Signed action over weight limit while submitting batch."}
        ])
        candidate = self._candidate(price_increment="1", size_increment="1")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "BTC",
            "side": "sell",
            "distribution": "uniform",
            "order_count": 2,
            "total_volume": "10",
            "start_price": "10",
            "end_price": "11",
        })
        self.assertFalse(response.success)
        self.assertIsNotNone(response.error)
        self.assertEqual(response.error.code, "BATCH_LIMIT_EXCEEDED")
        self.assertEqual(response.error.exchange_reason, "Signed action over weight limit while submitting batch.")
        self.assertEqual(len(fake_exchange.requests), 1)

    def test_ladder_preserves_partial_child_error_reason(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([
            {"status": "ok", "response": {"data": {"statuses": [
                {"resting": {"oid": 1}},
                {"error": "Order must have minimum value 10 USDC."},
                {"resting": {"oid": 2}},
            ]}}}
        ])
        candidate = self._candidate(price_increment="1", size_increment="1")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", lambda wallet: [])
        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "BTC",
            "side": "sell",
            "distribution": "uniform",
            "order_count": 3,
            "total_volume": "30",
            "start_price": "100",
            "end_price": "120",
        })
        self.assertFalse(response.success)
        self.assertIsNotNone(response.error)
        self.assertEqual(response.error.code, "ORDER_BELOW_MINIMUM")
        self.assertEqual(response.error.exchange_reason, "Order must have minimum value 10 USDC.")
        self.assertIsNotNone(response.ladder)
        self.assertTrue(response.ladder.partial)
        self.assertEqual(response.ladder.accepted_child_count, 1)
        self.assertEqual(len(fake_exchange.requests), 1)

    def test_ladder_is_ambiguous_only_when_statuses_are_missing(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([
            {"response": {"data": {}}}
        ])
        candidate = self._candidate(price_increment="1", size_increment="1")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "BTC",
            "side": "sell",
            "distribution": "uniform",
            "order_count": 2,
            "total_volume": "10",
            "start_price": "10",
            "end_price": "11",
        })
        self.assertFalse(response.success)
        self.assertIsNotNone(response.error)
        self.assertEqual(response.error.code, "AMBIGUOUS_LADDER_RESPONSE")
        self.assertIsNone(response.error.exchange_reason)
        self.assertEqual(len(fake_exchange.requests), 1)

    def test_ladder_single_tick_error_is_definite_invalid_price(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([
            {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": "Price must be divisible by tick size. asset=5"}]}}}
        ])
        candidate = self._sol_candidate(price_increment="0.001", size_increment="0.01")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", lambda wallet: [])
        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "SOL",
            "side": "sell",
            "distribution": "half_gaussian",
            "order_count": 50,
            "total_volume": "30",
            "start_price": "100",
            "end_price": "120",
        })
        self.assertFalse(response.success)
        self.assertIsNotNone(response.error)
        self.assertEqual(response.error.code, "INVALID_PRICE_TICK")
        self.assertEqual(response.error.message, "Hyperliquid rejected the ladder.")
        self.assertEqual(response.error.exchange_reason, "Price must be divisible by tick size. asset=5")
        self.assertIsNotNone(response.ladder)
        self.assertEqual(response.ladder.accepted_child_count, 0)
        self.assertEqual(response.ladder.submitted_order_count, 0)
        self.assertEqual(len(fake_exchange.requests), 1)
        self.assertEqual(len(fake_exchange.requests[0]), 39)

    def test_new_order_uses_hyperliquid_price_normalization(self):
        hl = _hl_module()

        class StrictExchange:
            def __init__(self):
                self.requests: List[List[Dict[str, Any]]] = []

            def bulk_orders(self, orders):
                self.requests.append(orders)
                return {"response": {"data": {"statuses": [{"resting": {"oid": 999}}]}}}

        fake_exchange = StrictExchange()
        candidate = self._sol_candidate(price_increment="0.001", size_increment="0.01")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", lambda wallet: [])
        response = hl.execute({
            "operation": "new_order",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "SOL",
            "side": "sell",
            "order_type": "limit",
            "volume": "1",
            "price": "104.49999",
        })
        self.assertTrue(response.success)
        self.assertEqual(len(fake_exchange.requests), 1)
        self.assertAlmostEqual(float(fake_exchange.requests[0][0]["limit_px"]), 104.5)

    def test_ladder_submission_exception_preserves_sanitized_message(self):
        hl = _hl_module()

        class ThrowExchange:
            def __init__(self):
                self.requests: List[List[Dict[str, Any]]] = []

            def bulk_orders(self, order_requests, builder=None, grouping="na"):
                self.requests.append([dict(req) for req in order_requests])
                raise RuntimeError("Signed action over weight limit for batch")

        fake_exchange = ThrowExchange()
        candidate = self._candidate(price_increment="1", size_increment="1")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "BTC",
            "side": "sell",
            "distribution": "uniform",
            "order_count": 2,
            "total_volume": "10",
            "start_price": "10",
            "end_price": "11",
        })
        self.assertFalse(response.success)
        self.assertIsNotNone(response.error)
        self.assertEqual(response.error.code, "EXCHANGE_SUBMISSION_ERROR")
        self.assertIn("Signed action over weight limit for batch", response.error.exchange_reason)
        self.assertEqual(len(fake_exchange.requests), 1)


    def test_ladder_verification_requires_open_orders(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([self._response_for_orders(2)])
        candidate = self._candidate(price_increment="1", size_increment="1")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", lambda wallet: [])
        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "BTC",
            "side": "buy",
            "distribution": "uniform",
            "order_count": 2,
            "total_volume": "10",
            "start_price": "10",
            "end_price": "9",
        })
        self.assertFalse(response.success)
        self.assertIsNotNone(response.error)
        self.assertEqual(response.error.code, "VERIFICATION_FAILED")
        self.assertEqual(len(fake_exchange.requests), 1)

    def test_ladder_verification_uses_resting_oids_and_polls_until_visible(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([self._response_for_orders(5)])
        candidate = self._sol_candidate()
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        snapshot_state = {"calls": 0}

        def snapshot(wallet: str):
            snapshot_state["calls"] += 1
            if snapshot_state["calls"] == 1:
                return []
            return self._snapshot_from_first_batch(fake_exchange, oid_offset=10_000)

        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", mock.Mock(side_effect=snapshot))
        self.patch("plugins.trade.agents.x_hyperliquid_agent.time.sleep", lambda *_: None)

        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "SOL",
            "side": "sell",
            "distribution": "half_gaussian",
            "order_count": 5,
            "total_volume": "20",
            "start_price": "100",
            "end_price": "120",
        })

        self.assertTrue(response.success)
        self.assertIsNotNone(response.ladder)
        self.assertTrue(response.ladder.verified)
        self.assertEqual(response.ladder.submitted_order_count, 5)
        self.assertEqual(response.ladder.accepted_child_count, 5)
        self.assertEqual(len(fake_exchange.requests), 1)
        self.assertEqual(snapshot_state["calls"], 2)
        self.assertEqual(response.ladder.child_order_ids, [10_000, 10_001, 10_002, 10_003, 10_004])

    def test_ladder_live_verified_50_to_39_regression(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([self._response_for_orders(39, oid_offset=503453581593)])
        candidate = self._sol_candidate(price_increment="0.001", size_increment="0.01")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", lambda wallet: self._snapshot_from_first_batch(fake_exchange, oid_offset=503453581593))

        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "SOL",
            "side": "sell",
            "distribution": "half_gaussian",
            "order_count": 50,
            "total_volume": "30",
            "start_price": "100",
            "end_price": "120",
        })

        self.assertTrue(response.success)
        self.assertIsNotNone(response.ladder)
        self.assertTrue(response.ladder.verified)
        self.assertEqual(response.ladder.requested_order_count, 50)
        self.assertEqual(response.ladder.omitted_order_count, 11)
        self.assertEqual(response.ladder.submitted_order_count, 39)
        self.assertEqual(response.ladder.submitted_volume, "30.00")
        self.assertEqual(response.ladder.batch_count, 1)
        self.assertEqual(len(fake_exchange.requests), 1)
        self.assertEqual(len(fake_exchange.requests[0]), 39)
        self.assertEqual(response.ladder.child_order_ids, list(range(503453581593, 503453581632)))
        snapshot = self._snapshot_from_first_batch(fake_exchange, oid_offset=503453581593)
        self.assertEqual([row["oid"] for row in snapshot], list(range(503453581593, 503453581632)))

    def test_ladder_verification_normalizes_oid_types(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([self._response_for_orders(3)])
        candidate = self._sol_candidate()
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", lambda wallet: self._snapshot_from_first_batch(fake_exchange, oid_offset=10_000, as_strings=True))

        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "SOL",
            "side": "sell",
            "distribution": "uniform",
            "order_count": 3,
            "total_volume": "6",
            "start_price": "100",
            "end_price": "120",
        })

        self.assertTrue(response.success)
        self.assertTrue(response.ladder.verified)
        self.assertEqual(len(fake_exchange.requests), 1)
        self.assertEqual(response.ladder.child_order_ids, [10_000, 10_001, 10_002])

    def test_ladder_verification_rejects_same_price_size_without_expected_oids(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([self._response_for_orders(2)])
        candidate = self._sol_candidate()
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        self.patch("plugins.trade.agents.x_hyperliquid_agent.time.sleep", lambda *_: None)

        def snapshot(wallet: str):
            if not fake_exchange.requests:
                return []
            rows = []
            for index, order in enumerate(fake_exchange.requests[0]):
                rows.append(
                    {
                        "symbol": order["coin"],
                        "side": "sell",
                        "oid": 50_000 + index,
                        "sz": str(order["sz"]),
                        "limitPx": str(order["limit_px"]),
                    }
                )
            return rows

        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", snapshot)

        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "SOL",
            "side": "sell",
            "distribution": "uniform",
            "order_count": 2,
            "total_volume": "4",
            "start_price": "100",
            "end_price": "120",
        })

        self.assertFalse(response.success)
        self.assertIsNotNone(response.error)
        self.assertEqual(response.error.code, "VERIFICATION_FAILED")
        self.assertEqual(len(fake_exchange.requests), 1)
        self.assertEqual(response.ladder.accepted_child_count, 2)

    def test_ladder_verification_does_not_require_filled_child_in_open_orders(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([
            {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"oid": 77_001}}, {"resting": {"oid": 77_002}}]}}}
        ])
        candidate = self._sol_candidate()
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))

        def snapshot(wallet: str):
            if not fake_exchange.requests:
                return []
            first_batch = fake_exchange.requests[0]
            if len(first_batch) < 2:
                return []
            return [
                {
                    "symbol": first_batch[1]["coin"],
                    "side": "sell",
                    "oid": 77_002,
                    "sz": str(first_batch[1]["sz"]),
                    "limitPx": str(first_batch[1]["limit_px"]),
                }
            ]

        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", snapshot)

        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "SOL",
            "side": "sell",
            "distribution": "uniform",
            "order_count": 2,
            "total_volume": "4",
            "start_price": "100",
            "end_price": "120",
        })

        self.assertTrue(response.success)
        self.assertTrue(response.ladder.verified)
        self.assertEqual(len(fake_exchange.requests), 1)
        self.assertEqual(response.ladder.accepted_child_count, 2)

    def test_ladder_verification_requires_primary_oids_over_unrelated_existing_orders(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([self._response_for_orders(2)])
        candidate = self._sol_candidate()
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        self.patch("plugins.trade.agents.x_hyperliquid_agent.time.sleep", lambda *_: None)

        def snapshot(wallet: str):
            if not fake_exchange.requests:
                return []
            rows = []
            for index, order in enumerate(fake_exchange.requests[0]):
                rows.append(
                    {
                        "symbol": order["coin"],
                        "side": "sell",
                        "oid": 99_000 + index,
                        "sz": str(order["sz"]),
                        "limitPx": str(order["limit_px"]),
                    }
                )
            return rows

        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", snapshot)

        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "SOL",
            "side": "sell",
            "distribution": "uniform",
            "order_count": 2,
            "total_volume": "4",
            "start_price": "100",
            "end_price": "120",
        })

        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "VERIFICATION_FAILED")
        self.assertEqual(len(fake_exchange.requests), 1)


    def test_ladder_rejects_insufficient_volume_for_requested_order_count(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([])
        candidate = self._candidate(price_increment="0.1", size_increment="1")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "BTC",
            "side": "buy",
            "distribution": "uniform",
            "order_count": 2,
            "total_volume": "0.5",
            "start_price": "10",
            "end_price": "9",
        })
        self.assertFalse(response.success)
        self.assertIsNotNone(response.error)
        self.assertEqual(response.error.code, "INSUFFICIENT_VOLUME_FOR_ORDER_COUNT")
        self.assertEqual(fake_exchange.requests, [])

    def test_ladder_omits_children_below_hyperliquid_minimum_and_reconciles_volume(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([self._response_for_orders(9)])
        candidate = self._sol_candidate(price_increment="0.001", size_increment="0.01")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", lambda wallet: self._snapshot_from_exchange(fake_exchange))
        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "SOL",
            "side": "sell",
            "distribution": "half_gaussian",
            "order_count": 10,
            "total_volume": "30",
            "start_price": "100",
            "end_price": "120",
        })
        self.assertTrue(response.success)
        self.assertIsNotNone(response.ladder)
        self.assertEqual(response.ladder.status, "success")
        self.assertEqual(response.ladder.requested_order_count, 10)
        self.assertEqual(response.ladder.submitted_order_count, 9)
        self.assertEqual(response.ladder.omitted_order_count, 1)
        self.assertEqual(response.ladder.omitted_below_minimum, 1)
        self.assertEqual(response.ladder.requested_volume, "30")
        self.assertEqual(response.ladder.submitted_volume, "30.00")
        self.assertEqual(response.ladder.accepted_child_count, 9)
        self.assertTrue(response.ladder.verified)
        self.assertEqual(len(fake_exchange.requests), 1)
        self.assertEqual(len(fake_exchange.requests[0]), 9)
        self.assertTrue(all(Decimal(str(order["sz"])) % Decimal("0.01") == 0 for order in fake_exchange.requests[0]))
        self.assertTrue(all(Decimal(str(order["limit_px"])) * Decimal(str(order["sz"])) >= Decimal("10") for order in fake_exchange.requests[0]))

    def test_ladder_omits_multiple_sub_minimum_children_and_reconciles_volume(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([self._response_for_orders(18)])
        candidate = self._sol_candidate(price_increment="0.001", size_increment="0.01")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", lambda wallet: self._snapshot_from_exchange(fake_exchange))
        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "SOL",
            "side": "sell",
            "distribution": "half_gaussian",
            "order_count": 20,
            "total_volume": "30",
            "start_price": "100",
            "end_price": "120",
        })
        self.assertTrue(response.success)
        self.assertIsNotNone(response.ladder)
        self.assertEqual(response.ladder.requested_order_count, 20)
        self.assertEqual(response.ladder.submitted_order_count, 18)
        self.assertEqual(response.ladder.omitted_order_count, 2)
        self.assertEqual(response.ladder.omitted_below_minimum, 2)
        self.assertEqual(response.ladder.submitted_volume, "30.00")
        self.assertEqual(len(fake_exchange.requests), 1)
        self.assertEqual(len(fake_exchange.requests[0]), 18)
        self.assertTrue(all(Decimal(str(order["limit_px"])) * Decimal(str(order["sz"])) >= Decimal("10") for order in fake_exchange.requests[0]))

    def test_ladder_rejects_when_all_children_are_invalid(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([])
        candidate = self._sol_candidate(price_increment="0.001", size_increment="0.01")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "SOL",
            "side": "sell",
            "distribution": "uniform",
            "order_count": 2,
            "total_volume": "0.1",
            "start_price": "100",
            "end_price": "101",
        })
        self.assertFalse(response.success)
        self.assertIsNotNone(response.error)
        self.assertEqual(response.error.code, "LADDER_NO_VALID_CHILDREN")
        self.assertEqual(fake_exchange.requests, [])
        self.assertIsNotNone(response.ladder)
        self.assertEqual(response.ladder.status, "failed")
        self.assertEqual(response.ladder.submitted_order_count, 0)

    def test_ladder_rejects_when_only_one_valid_child_remains(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([])
        candidate = self._sol_candidate(price_increment="0.001", size_increment="0.01")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "SOL",
            "side": "sell",
            "distribution": "uniform",
            "order_count": 2,
            "total_volume": "0.2",
            "start_price": "50",
            "end_price": "150",
        })
        self.assertFalse(response.success)
        self.assertIsNotNone(response.error)
        self.assertEqual(response.error.code, "LADDER_TOO_FEW_VALID_CHILDREN")
        self.assertEqual(fake_exchange.requests, [])
        self.assertIsNotNone(response.ladder)
        self.assertEqual(response.ladder.status, "failed")

    def test_uniform_ladder_uses_same_omit_and_reconcile_policy(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([self._response_for_orders(2)])
        candidate = self._sol_candidate(price_increment="0.001", size_increment="0.01")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", lambda wallet: self._snapshot_from_exchange(fake_exchange))
        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "SOL",
            "side": "sell",
            "distribution": "uniform",
            "order_count": 3,
            "total_volume": "0.3",
            "start_price": "50",
            "end_price": "150",
        })
        self.assertTrue(response.success)
        self.assertIsNotNone(response.ladder)
        self.assertEqual(response.ladder.requested_order_count, 3)
        self.assertEqual(response.ladder.submitted_order_count, 2)
        self.assertEqual(response.ladder.omitted_order_count, 1)
        self.assertEqual(response.ladder.omitted_below_minimum, 1)
        self.assertEqual(response.ladder.submitted_volume, "0.30")
        self.assertEqual(len(fake_exchange.requests), 1)
        self.assertEqual(len(fake_exchange.requests[0]), 2)
        self.assertTrue(all(Decimal(str(order["limit_px"])) * Decimal(str(order["sz"])) >= Decimal("10") for order in fake_exchange.requests[0]))

    def test_ladder_accepts_5_order_control_path_after_preflight(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([self._response_for_orders(5)])
        candidate = self._sol_candidate(price_increment="0.001", size_increment="0.01")
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [candidate])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (fake_exchange, "0x1111111111111111111111111111111111111111", "secret"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidate, ""))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", lambda wallet: self._snapshot_from_exchange(fake_exchange))
        response = hl.execute({
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": "FIBO",
            "symbol": "SOL",
            "side": "sell",
            "distribution": "half_gaussian",
            "order_count": 5,
            "total_volume": "30",
            "start_price": "100",
            "end_price": "120",
        })
        self.assertTrue(response.success)
        self.assertIsNotNone(response.ladder)
        self.assertEqual(response.ladder.submitted_order_count, 5)
        self.assertEqual(response.ladder.accepted_child_count, 5)
        self.assertEqual(response.ladder.submitted_volume, "30.00")
        self.assertEqual(len(fake_exchange.requests), 1)


    def test_ladder_child_preflight_accepts_exact_minimum_notional_boundary(self):
        hl = _hl_module()
        candidate = self._sol_candidate(price_increment="0.001", size_increment="0.01")
        order_requests = [{"limit_px": Decimal("100.000"), "sz": Decimal("0.10")}]
        rows = hl._validate_final_ladder_children(order_requests, Decimal(candidate["sz_decimals"]), Decimal(candidate["size_increment"]))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["valid"])
        self.assertTrue(rows[0]["price_precision_ok"])
        self.assertTrue(rows[0]["size_precision_ok"])
        self.assertTrue(rows[0]["minimum_size_ok"])
        self.assertTrue(rows[0]["minimum_notional_ok"])


class TestNewOrderExecution(unittest.TestCase):
    def setUp(self):
        self._patches = []

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()

    def patch(self, target: str, value: Any):
        p = mock.patch(target, value)
        self._patches.append(p)
        return p.start()

    def test_new_order_resting_response_is_success_even_before_readback(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([
            {"response": {"data": {"statuses": [{"resting": {"oid": 777}}]}}}
        ])
        self.patch(
            "plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates",
            lambda: [
                {
                    "name": "SOL",
                    "public_symbol": "SOL",
                    "price_increment": "0.1",
                    "size_increment": "0.01",
                    "minimum_size": "0.01",
                }
            ],
        )
        self.patch(
            "plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate",
            lambda symbol, candidates: (candidates[0], ""),
        )
        self.patch(
            "plugins.trade.agents.x_hyperliquid_agent._build_exchange_client",
            lambda account: (fake_exchange, object(), None),
        )
        self.patch(
            "plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot",
            lambda wallet: [],
        )

        response = hl.execute(
            {
                "operation": "new_order",
                "exchange": "hyperliquid",
                "account": "FIBO",
                "symbol": "SOL",
                "side": "sell",
                "order_type": "limit",
                "volume": "1",
                "price": "100",
            }
        )

        self.assertTrue(response.success)
        self.assertIsNotNone(response.order)
        self.assertTrue(response.order.verified)
        self.assertEqual(response.order.exchange_order_id, 777)
        self.assertEqual(response.order.symbol, "SOL")
        self.assertEqual(fake_exchange.requests[0][0]["coin"], "SOL")

    def test_new_order_resolves_instrument_privately_and_verifies(self):
        hl = _hl_module()
        fake_exchange = FakeExchange([
            {"response": {"data": {"statuses": [{"resting": {"oid": 777}}]}}}
        ])
        self.patch(
            "plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates",
            lambda: [
                {
                    "name": "xyz:GOLD",
                    "public_symbol": "xyz:GOLD",
                    "price_increment": "0.1",
                    "size_increment": "0.01",
                    "minimum_size": "0.01",
                }
            ],
        )
        self.patch(
            "plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate",
            lambda symbol, candidates: (candidates[0], ""),
        )
        self.patch(
            "plugins.trade.agents.x_hyperliquid_agent._build_exchange_client",
            lambda account: (fake_exchange, object(), None),
        )
        self.patch(
            "plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot",
            lambda wallet: [
                {"symbol": "xyz:GOLD", "coin": "xyz:GOLD", "side": "buy", "oid": 777, "sz": "1", "limitPx": "2000.5"}
            ],
        )

        response = hl.execute(
            {
                "operation": "new_order",
                "exchange": "hyperliquid",
                "account": "FIBO",
                "symbol": "GOLD",
                "side": "buy",
                "order_type": "limit",
                "volume": "1",
                "price": "2000.5",
            }
        )

        self.assertTrue(response.success)
        self.assertIsNotNone(response.order)
        self.assertEqual(response.order.symbol, "GOLD")
        self.assertEqual(response.order.side, "buy")
        self.assertTrue(response.order.verified)
        self.assertEqual(len(fake_exchange.requests), 1)
        self.assertEqual(fake_exchange.requests[0][0]["coin"], "xyz:GOLD")

    def test_unsupported_instrument_returns_canonical_failure(self):
        hl = _hl_module()
        self.patch(
            "plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates",
            lambda: [],
        )
        self.patch(
            "plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate",
            lambda symbol, candidates: (None, "INSTRUMENT_NOT_FOUND"),
        )

        response = hl.execute(
            {
                "operation": "new_order",
                "exchange": "hyperliquid",
                "account": "FIBO",
                "symbol": "ABCXYZ",
                "side": "sell",
                "order_type": "limit",
                "volume": "1",
                "price": "1",
            }
        )

        self.assertFalse(response.success)
        self.assertIsNotNone(response.error)
        self.assertEqual(response.error.code, "INSTRUMENT_NOT_FOUND")
        self.assertEqual(response.error.message, "Instrument not found.")
        self.assertIsNone(response.order)


class TestPositionManagementWrites(unittest.TestCase):
    def setUp(self):
        self._patches = []

    def tearDown(self):
        for patcher in reversed(self._patches):
            patcher.stop()

    def patch(self, target: str, value: Any):
        patcher = mock.patch(target, value)
        self._patches.append(patcher)
        return patcher.start()

    def _position_response(self, positions):
        return make_success(operation="positions_orders", exchange="hyperliquid", account="FIBO", positions=positions, open_order_count=0, order_groups=[])

    def _candidate(self, symbol: str = "HYPE", price_increment: str = "0.1", size_increment: str = "0.01"):
        return {
            "dex": "",
            "dex_index": 0,
            "internal_name": symbol,
            "public_symbol": symbol,
            "public_key": symbol,
            "internal_key": symbol,
            "display_name": f"{symbol}-USDC",
            "price_increment": price_increment,
            "size_increment": size_increment,
            "sz_decimals": 2,
        }

    def _live_tp_order(self, oid: int, price: str, size: str = "82.81931", symbol: str = "BTC", side: str = "A"):
        return {
            "coin": symbol,
            "side": side,
            "limitPx": price,
            "triggerPx": price,
            "sz": size,
            "origSz": size,
            "oid": oid,
            "triggerCondition": f"Price above {price}",
            "isTrigger": True,
            "isPositionTpsl": False,
            "reduceOnly": True,
            "orderType": "Take Profit Market",
        }

    def _live_sl_order(self, oid: int, price: str, size: str = "82.81931", symbol: str = "BTC", side: str = "A"):
        return {
            "coin": symbol,
            "side": side,
            "limitPx": price,
            "triggerPx": price,
            "sz": size,
            "origSz": size,
            "oid": oid,
            "triggerCondition": f"Price below {price}",
            "isTrigger": True,
            "isPositionTpsl": False,
            "reduceOnly": True,
            "orderType": "Stop Market",
        }

    def _patch_context(self, positions_side_effect, open_orders_side_effect, exchange):
        self.patch("plugins.trade.agents.x_hyperliquid_agent._execute_positions_orders", mock.Mock(side_effect=positions_side_effect))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_open_orders_snapshot", mock.Mock(side_effect=open_orders_side_effect))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_perp_market_candidates", lambda: [self._candidate()])
        self.patch("plugins.trade.agents.x_hyperliquid_agent._resolve_instrument_candidate", lambda requested, candidates: (candidates[0], ""))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_candidate_mark_price", lambda candidate: Decimal("100"))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (exchange, "0x1111111111111111111111111111111111111111", "secret"))

    def test_set_tp_ambiguous_native_response_is_verified_by_post_read(self):
        hl = _hl_module()
        exchange = FakeExchange([{"response": {"data": {}}}])
        pre_positions = [CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp=None, sl=None)]
        post_positions = [CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp="75000", sl=None)]
        pre_orders = [{"symbol": "BTC", "side": "sell", "oid": oid, "reduce_only": False, "price": "65000"} for oid in range(6001, 6004)]
        post_orders = pre_orders + [{"coin": "BTC", "side": "A", "limitPx": "75000.0", "sz": "82.81931", "oid": 503559177280, "origSz": "82.81931", "reduceOnly": True}]
        self._patch_context([self._position_response(pre_positions), self._position_response(post_positions)], [pre_orders, post_orders], exchange)

        response = hl.execute({"operation": "set_tp", "exchange": "hyperliquid", "account": "FIBO", "symbol": "BTC", "price": "75000"})
        self.assertTrue(response.success)
        self.assertIsNotNone(response.position_action)
        self.assertTrue(response.position_action.verified)
        self.assertEqual(response.position_action.status, "success")
        self.assertEqual(response.position_action.price, "75000")
        self.assertEqual(response.position_action.exchange_order_id, 503559177280)
        self.assertEqual(response.position_action.current_size, "82.81931")
        self.assertEqual(len(exchange.requests), 1)

    def test_set_tp_ambiguous_native_response_without_new_tp_is_ambiguous(self):
        hl = _hl_module()
        exchange = FakeExchange([{"response": {"data": {}}}])
        pre_positions = [CanonicalPosition(symbol="HYPE", side="short", size="319.84", entry_price="71.075", pnl="+3592.77838", tp=None, sl=None)]
        pre_orders = [{"symbol": "HYPE", "side": "sell", "oid": oid} for oid in range(488362783478, 488362783663)]
        self._patch_context([self._position_response(pre_positions), self._position_response(pre_positions)], [pre_orders, pre_orders], exchange)

        response = hl.execute({"operation": "set_tp", "exchange": "hyperliquid", "account": "FIBO", "symbol": "HYPE", "price": "59.25"})
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "POSITION_ACTION_RESPONSE_AMBIGUOUS")
        self.assertEqual(len(exchange.requests), 1)

    def test_set_tp_ambiguous_response_does_not_prove_existing_same_price_tp(self):
        hl = _hl_module()
        exchange = FakeExchange([])
        pre_positions = [CanonicalPosition(symbol="HYPE", side="short", size="319.84", entry_price="71.075", pnl="+3592.77838", tp=None, sl=None)]
        pre_orders = [
            {"symbol": "HYPE", "side": "buy", "oid": 901, "reduce_only": True, "is_position_tpsl": True, "tpsl": "tp", "price": "59.25", "triggerPx": "59.25"},
            {"symbol": "HYPE", "side": "sell", "oid": 488362783478, "reduce_only": False, "is_position_tpsl": False, "price": "77"},
        ]
        self._patch_context([self._position_response(pre_positions), self._position_response(pre_positions)], [pre_orders, pre_orders], exchange)

        result = hl._verify_position_protection_submission(
            "set_tp",
            "FIBO",
            "HYPE",
            "tp",
            Decimal("59.25"),
            None,
            {901, 488362783478},
            True,
        )
        self.assertIsNone(result[0])
        self.assertIsNotNone(result[1])
        self.assertEqual(result[1].error.code, "POSITION_ACTION_RESPONSE_AMBIGUOUS")
        self.assertIsNone(result[2])

    def test_set_sl_ambiguous_native_response_is_verified_by_post_read(self):
        hl = _hl_module()
        exchange = FakeExchange([{"response": {"data": {}}}])
        pre_positions = [CanonicalPosition(symbol="HYPE", side="long", size="2.15", entry_price="100", pnl="+1", tp=None, sl=None)]
        post_positions = [CanonicalPosition(symbol="HYPE", side="long", size="2.15", entry_price="100", pnl="+1", tp=None, sl="80")]
        pre_orders = [{"symbol": "HYPE", "side": "sell", "oid": 200 + idx, "reduce_only": False, "price": "105"} for idx in range(2)]
        post_orders = pre_orders + [{"coin": "HYPE", "side": "A", "limitPx": "80.0", "sz": "2.15", "oid": 78, "origSz": "2.15", "reduceOnly": True}]
        self._patch_context([self._position_response(pre_positions), self._position_response(post_positions)], [pre_orders, post_orders], exchange)

        response = hl.execute({"operation": "set_sl", "exchange": "hyperliquid", "account": "FIBO", "symbol": "HYPE", "price": "80"})
        self.assertTrue(response.success)
        self.assertIsNotNone(response.position_action)
        self.assertTrue(response.position_action.verified)
        self.assertEqual(response.position_action.status, "success")
        self.assertEqual(response.position_action.price, "80")
        self.assertEqual(response.position_action.exchange_order_id, 78)
        self.assertEqual(response.position_action.current_size, "2.15")
        self.assertEqual(len(exchange.requests), 1)

    def test_set_sl_ambiguous_native_response_is_verified_by_post_read_live_shape_without_position_sl(self):
        hl = _hl_module()
        exchange = FakeExchange([{"response": {"data": {}}}])
        pre_positions = [CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp=None, sl=None)]
        post_positions = [CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp=None, sl=None)]
        pre_orders = [
            {"symbol": "BTC", "side": "sell", "oid": 6001, "reduce_only": False, "price": "76000"},
            {"symbol": "BTC", "side": "sell", "oid": 6002, "reduce_only": False, "price": "77000"},
        ]
        post_orders = pre_orders + [
            {
                "coin": "BTC",
                "side": "A",
                "limitPx": "55000.0",
                "sz": "82.81931",
                "origSz": "82.81931",
                "oid": 503651661973,
                "triggerCondition": "Price below 55000",
                "isTrigger": True,
                "triggerPx": "55000.0",
                "isPositionTpsl": False,
                "reduceOnly": True,
                "orderType": "Stop Market",
            }
        ]
        self._patch_context([self._position_response(pre_positions), self._position_response(post_positions)], [pre_orders, post_orders], exchange)
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_candidate_mark_price", lambda candidate: Decimal("64595"))

        response = hl.execute({"operation": "set_sl", "exchange": "hyperliquid", "account": "FIBO", "symbol": "BTC", "price": "55000"})
        self.assertTrue(response.success)
        self.assertIsNotNone(response.position_action)
        self.assertTrue(response.position_action.verified)
        self.assertEqual(response.position_action.status, "success")
        self.assertEqual(response.position_action.price, "55000")
        self.assertEqual(response.position_action.exchange_order_id, 503651661973)
        self.assertEqual(response.position_action.current_size, "82.81931")
        self.assertEqual(len(exchange.requests), 1)

    def test_positions_orders_enriches_live_tp_sl_counts_from_open_orders(self):
        hl = _hl_module()
        positions_raw = {
            "assetPositions": [
                {
                    "position": {
                        "coin": "BTC",
                        "szi": "82.81931",
                        "entryPx": "64491.5",
                        "unrealizedPnl": "14857.95012",
                    }
                }
            ]
        }
        open_orders_raw = [
            self._live_tp_order(503559177280, "75000.0"),
            self._live_sl_order(503651661973, "55000.0"),
            self._live_sl_order(503651661974, "55000.0"),
            self._live_sl_order(503651661975, "55000.0"),
            self._live_sl_order(503651661976, "56000.0"),
        ]
        with mock.patch.object(hl, "_normalize_account_alias", return_value="FLEX"), mock.patch.object(hl, "_lookup_credentials", return_value=("0x1111111111111111111111111111111111111111", "secret")), mock.patch.object(hl, "_post_info", side_effect=[positions_raw, open_orders_raw, {}]):
            response = hl._execute_positions_orders("FIBO", {"operation": "positions_orders", "exchange": "hyperliquid", "account": "FIBO"})
        self.assertTrue(response.success)
        self.assertEqual(response.open_order_count, 5)
        self.assertEqual(len(response.positions), 1)
        position = response.positions[0]
        self.assertEqual(position.symbol, "BTC")
        self.assertEqual(position.tp, "75000")
        self.assertEqual(position.tp_count, 1)
        self.assertEqual(position.sl, "55000")
        self.assertEqual(position.sl_count, 4)

    def test_positions_management_marks_duplicate_protection_counts_in_display(self):
        response = make_success(
            operation="positions_management",
            exchange="hyperliquid",
            account="FIBO",
            positions=[
                CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp="75000", sl="55000", tp_count=1, sl_count=4),
            ],
        )
        wizard = TradeWizard(tradedesk=SequenceDesk([response]))  # type: ignore[arg-type]
        key = ("telegram", "duplicate-sl-display")
        state = wizard._state_for(key)
        state.positions = [position.to_dict() for position in (response.positions or [])]
        screen = wizard._render_positions_management(key, False)
        self.assertIn("TP: 75,000", screen.text)
        self.assertIn("SL: 55,000 (multiple)", screen.text)

    def test_set_sl_duplicate_live_shaped_orders_fail_without_writes(self):
        hl = _hl_module()
        exchange = FakeExchange([])
        positions = [CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp="75000", sl=None, tp_count=1, sl_count=None)]
        open_orders = [
            self._live_tp_order(503559177280, "75000.0"),
            self._live_sl_order(503651661973, "55000.0"),
            self._live_sl_order(503651661974, "55000.0"),
        ]
        self._patch_context([self._position_response(positions), self._position_response(positions)], [open_orders, open_orders], exchange)
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_candidate_mark_price", lambda candidate: Decimal("64595"))

        response = hl.execute({"operation": "set_sl", "exchange": "hyperliquid", "account": "FIBO", "symbol": "BTC", "price": "55000"})
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "AMBIGUOUS_PROTECTION_STATE")
        self.assertEqual(len(exchange.requests), 0)

    def test_set_sl_single_live_shaped_order_replaces_existing_order(self):
        hl = _hl_module()
        exchange = FakeExchange([{"response": {"data": {}}}])
        positions = [CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp="75000", sl=None, tp_count=1, sl_count=1)]
        open_orders = [
            self._live_tp_order(503559177280, "75000.0"),
            self._live_sl_order(503651661973, "55000.0"),
        ]
        self._patch_context([self._position_response(positions), self._position_response(positions)], [open_orders, open_orders], exchange)
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_candidate_mark_price", lambda candidate: Decimal("64595"))

        response = hl.execute({"operation": "set_sl", "exchange": "hyperliquid", "account": "FIBO", "symbol": "BTC", "price": "56000"})
        self.assertTrue(response.success)
        self.assertEqual(len(exchange.requests), 1)
        self.assertEqual(exchange.requests[0][0]["oid"], 503651661973)
        self.assertEqual(exchange.requests[0][0]["order"]["order_type"]["trigger"]["triggerPx"], 56000.0)

    def test_set_sl_no_existing_live_shaped_order_creates_new_order(self):
        hl = _hl_module()
        exchange = FakeExchange([{"response": {"data": {}}}])
        positions = [CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp="75000", sl=None, tp_count=1, sl_count=None)]
        pre_orders = [self._live_tp_order(503559177280, "75000.0")]
        post_orders = pre_orders + [self._live_sl_order(503651661973, "55000.0")]
        self._patch_context([self._position_response(positions), self._position_response(positions)], [pre_orders, post_orders], exchange)
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_candidate_mark_price", lambda candidate: Decimal("64595"))

        response = hl.execute({"operation": "set_sl", "exchange": "hyperliquid", "account": "FIBO", "symbol": "BTC", "price": "55000"})
        self.assertTrue(response.success)
        self.assertEqual(len(exchange.requests), 1)
        self.assertNotIn("oid", exchange.requests[0][0])

    def test_set_tp_duplicate_live_shaped_orders_fail_without_writes(self):
        hl = _hl_module()
        exchange = FakeExchange([])
        positions = [CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp=None, sl="55000", sl_count=1)]
        open_orders = [
            self._live_tp_order(503559177280, "75000.0"),
            self._live_tp_order(503559177281, "75000.0"),
            self._live_sl_order(503651661973, "55000.0"),
        ]
        self._patch_context([self._position_response(positions), self._position_response(positions)], [open_orders, open_orders], exchange)
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_candidate_mark_price", lambda candidate: Decimal("64595"))

        response = hl.execute({"operation": "set_tp", "exchange": "hyperliquid", "account": "FIBO", "symbol": "BTC", "price": "75000"})
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "AMBIGUOUS_PROTECTION_STATE")
        self.assertEqual(len(exchange.requests), 0)

    def test_set_tp_single_live_shaped_order_replaces_existing_order(self):
        hl = _hl_module()
        exchange = FakeExchange([{"response": {"data": {}}}])
        positions = [CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp=None, sl="55000", sl_count=1)]
        open_orders = [
            self._live_tp_order(503559177280, "75000.0"),
            self._live_sl_order(503651661973, "55000.0"),
        ]
        self._patch_context([self._position_response(positions), self._position_response(positions)], [open_orders, open_orders], exchange)
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_candidate_mark_price", lambda candidate: Decimal("64595"))

        response = hl.execute({"operation": "set_tp", "exchange": "hyperliquid", "account": "FIBO", "symbol": "BTC", "price": "76000"})
        self.assertTrue(response.success)
        self.assertEqual(len(exchange.requests), 1)
        self.assertEqual(exchange.requests[0][0]["oid"], 503559177280)
        self.assertEqual(exchange.requests[0][0]["order"]["order_type"]["trigger"]["triggerPx"], 76000.0)


    def test_set_sl_native_rejection_preserves_exchange_reason(self):
        hl = _hl_module()
        exchange = FakeExchange([{"response": {"data": {"statuses": [{"error": "Too many open orders asset=0"}]}}}])
        positions = [CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp=None, sl=None)]
        open_orders = []
        self._patch_context([self._position_response(positions), self._position_response(positions)], [open_orders, open_orders], exchange)
        self.patch("plugins.trade.agents.x_hyperliquid_agent._fetch_candidate_mark_price", lambda candidate: Decimal("64595"))

        response = hl.execute({"operation": "set_sl", "exchange": "hyperliquid", "account": "FIBO", "symbol": "BTC", "price": "55000"})
        self.assertFalse(response.success)
        self.assertIsNotNone(response.error)
        self.assertNotEqual(response.error.code, "POSITION_ACTION_RESPONSE_AMBIGUOUS")
        self.assertEqual(response.error.exchange_reason, "Too many open orders asset=0")
        self.assertEqual(len(exchange.requests), 1)

    def test_set_tp_zero_removes_normalized_shape_tp_without_touching_ladder_orders(self):
        hl = _hl_module()
        exchange = FakeExchange([{}])
        btc_tp_oid = 503559177280
        btc_sl_oid = 503559177281
        btc_ladder_oids = [6001, 6002, 6003]
        hype_oids = [7001, 7002]
        positions_before = [CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp="75000", sl=None)]
        positions_after = [CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp=None, sl=None)]
        pre_orders = [
            {"symbol": "BTC", "side": "sell", "size": Decimal("82.81931"), "price": Decimal("75000.0"), "oid": btc_tp_oid, "reduce_only": True, "is_trigger": True, "is_position_tpsl": False, "tp": "75000", "sl": None},
            {"symbol": "BTC", "side": "sell", "size": Decimal("82.81931"), "price": Decimal("64000.0"), "oid": btc_sl_oid, "reduce_only": True, "is_trigger": True, "is_position_tpsl": False, "tp": None, "sl": "64000"},
            {"symbol": "BTC", "side": "sell", "size": Decimal("1"), "price": Decimal("76000.0"), "oid": btc_ladder_oids[0], "reduce_only": False, "is_trigger": False, "is_position_tpsl": False},
            {"symbol": "BTC", "side": "sell", "size": Decimal("1"), "price": Decimal("77000.0"), "oid": btc_ladder_oids[1], "reduce_only": False, "is_trigger": False, "is_position_tpsl": False},
            {"symbol": "BTC", "side": "buy", "size": Decimal("1"), "price": Decimal("78000.0"), "oid": btc_ladder_oids[2], "reduce_only": False, "is_trigger": False, "is_position_tpsl": False},
            {"symbol": "HYPE", "side": "sell", "size": Decimal("10"), "price": Decimal("8.0"), "oid": hype_oids[0], "reduce_only": False, "is_trigger": False, "is_position_tpsl": False},
            {"symbol": "HYPE", "side": "buy", "size": Decimal("10"), "price": Decimal("7.5"), "oid": hype_oids[1], "reduce_only": False, "is_trigger": False, "is_position_tpsl": False},
        ]
        post_orders = [order for order in pre_orders if order["oid"] != btc_tp_oid]
        context_before = {
            "alias": "FIBO",
            "account": "FIBO",
            "wallet": "0x1111111111111111111111111111111111111111",
            "candidate": self._candidate("BTC"),
            "positions_response": self._position_response(positions_before),
            "current_position": positions_before[0],
            "open_orders": pre_orders,
            "reference_price": Decimal("64901"),
            "current_size": Decimal("82.81931"),
            "current_side": "long",
            "closing_side": "sell",
        }
        context_after = {**context_before, "positions_response": self._position_response(positions_after), "current_position": positions_after[0], "open_orders": post_orders}
        self.patch("plugins.trade.agents.x_hyperliquid_agent._current_position_management_context", mock.Mock(side_effect=[(context_before, None), (context_after, None)]))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (exchange, "0x1111111111111111111111111111111111111111", "secret"))

        response = hl.execute({"operation": "set_tp", "exchange": "hyperliquid", "account": "FIBO", "symbol": "BTC", "price": "0"})
        self.assertTrue(response.success)
        self.assertIsNotNone(response.position_action)
        self.assertTrue(response.position_action.removed)
        self.assertTrue(response.position_action.verified)
        self.assertEqual(response.position_action.exchange_order_id, btc_tp_oid)
        self.assertEqual(response.position_action.message, "Take Profit removed.")
        self.assertEqual(len(exchange.requests), 1)
        self.assertEqual(exchange.requests[0], [{"coin": "BTC", "oid": btc_tp_oid}])
        self.assertEqual({order["oid"] for order in post_orders}, {503559177281, 6001, 6002, 6003, 7001, 7002})

    def test_set_sl_zero_removes_normalized_shape_sl_without_touching_ladder_orders(self):
        hl = _hl_module()
        exchange = FakeExchange([{}])
        btc_sl_oid = 503559177282
        btc_tp_oid = 503559177280
        btc_ladder_oids = [6001, 6002, 6003]
        hype_oids = [7001, 7002]
        positions_before = [CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp=None, sl="64000")]
        positions_after = [CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp=None, sl=None)]
        pre_orders = [
            {"symbol": "BTC", "side": "sell", "size": Decimal("82.81931"), "price": Decimal("75000.0"), "oid": btc_tp_oid, "reduce_only": True, "is_trigger": True, "is_position_tpsl": False, "tp": "75000", "sl": None},
            {"symbol": "BTC", "side": "sell", "size": Decimal("82.81931"), "price": Decimal("64000.0"), "oid": btc_sl_oid, "reduce_only": True, "is_trigger": True, "is_position_tpsl": False, "tp": None, "sl": "64000"},
            {"symbol": "BTC", "side": "sell", "size": Decimal("1"), "price": Decimal("76000.0"), "oid": btc_ladder_oids[0], "reduce_only": False, "is_trigger": False, "is_position_tpsl": False},
            {"symbol": "BTC", "side": "sell", "size": Decimal("1"), "price": Decimal("77000.0"), "oid": btc_ladder_oids[1], "reduce_only": False, "is_trigger": False, "is_position_tpsl": False},
            {"symbol": "BTC", "side": "buy", "size": Decimal("1"), "price": Decimal("78000.0"), "oid": btc_ladder_oids[2], "reduce_only": False, "is_trigger": False, "is_position_tpsl": False},
            {"symbol": "HYPE", "side": "sell", "size": Decimal("10"), "price": Decimal("8.0"), "oid": hype_oids[0], "reduce_only": False, "is_trigger": False, "is_position_tpsl": False},
            {"symbol": "HYPE", "side": "buy", "size": Decimal("10"), "price": Decimal("7.5"), "oid": hype_oids[1], "reduce_only": False, "is_trigger": False, "is_position_tpsl": False},
        ]
        post_orders = [order for order in pre_orders if order["oid"] != btc_sl_oid]
        context_before = {
            "alias": "FIBO",
            "account": "FIBO",
            "wallet": "0x1111111111111111111111111111111111111111",
            "candidate": self._candidate("BTC"),
            "positions_response": self._position_response(positions_before),
            "current_position": positions_before[0],
            "open_orders": pre_orders,
            "reference_price": Decimal("64901"),
            "current_size": Decimal("82.81931"),
            "current_side": "long",
            "closing_side": "sell",
        }
        context_after = {**context_before, "positions_response": self._position_response(positions_after), "current_position": positions_after[0], "open_orders": post_orders}
        self.patch("plugins.trade.agents.x_hyperliquid_agent._current_position_management_context", mock.Mock(side_effect=[(context_before, None), (context_after, None)]))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (exchange, "0x1111111111111111111111111111111111111111", "secret"))

        response = hl.execute({"operation": "set_sl", "exchange": "hyperliquid", "account": "FIBO", "symbol": "BTC", "price": "0"})
        self.assertTrue(response.success)
        self.assertIsNotNone(response.position_action)
        self.assertTrue(response.position_action.removed)
        self.assertTrue(response.position_action.verified)
        self.assertEqual(response.position_action.exchange_order_id, btc_sl_oid)
        self.assertEqual(response.position_action.message, "Stop Loss removed.")
        self.assertEqual(len(exchange.requests), 1)
        self.assertEqual(exchange.requests[0], [{"coin": "BTC", "oid": btc_sl_oid}])
        self.assertEqual({order["oid"] for order in post_orders}, {503559177280, 6001, 6002, 6003, 7001, 7002})

    def test_set_tp_zero_helper_recognizes_raw_and_normalized_rows(self):
        hl = _hl_module()
        position = CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp="75000", sl=None)
        raw_rows = [
            {"coin": "BTC", "side": "A", "limitPx": "75000.0", "sz": "82.81931", "oid": 503559177280, "origSz": "82.81931", "reduceOnly": True},
        ]
        normalized_rows = [
            {"symbol": "BTC", "side": "sell", "size": Decimal("82.81931"), "price": Decimal("75000.0"), "oid": 503559177280, "reduce_only": True, "is_trigger": True, "is_position_tpsl": False, "tp": "75000", "sl": None},
        ]
        raw_match, raw_error = hl._find_position_protection_removal_order(raw_rows, "BTC", "sell", Decimal("75000"))
        normalized_match, normalized_error = hl._find_position_protection_removal_order(normalized_rows, "BTC", "sell", Decimal("75000"))
        self.assertIsNone(raw_error)
        self.assertIsNone(normalized_error)
        self.assertEqual(raw_match["oid"], 503559177280)
        self.assertEqual(normalized_match["oid"], 503559177280)
        self.assertEqual(position.tp, "75000")

    def test_set_tp_zero_position_tp_present_but_no_removable_target_fails(self):
        hl = _hl_module()
        exchange = FakeExchange([])
        positions = [CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp="75000", sl=None)]
        open_orders = [{"symbol": "BTC", "side": "sell", "size": Decimal("82.81931"), "price": Decimal("76000.0"), "oid": 6001, "reduce_only": False, "is_trigger": False, "is_position_tpsl": False}]
        context = {
            "alias": "FIBO",
            "account": "FIBO",
            "wallet": "0x1111111111111111111111111111111111111111",
            "candidate": self._candidate("BTC"),
            "positions_response": self._position_response(positions),
            "current_position": positions[0],
            "open_orders": open_orders,
            "reference_price": Decimal("64901"),
            "current_size": Decimal("82.81931"),
            "current_side": "long",
            "closing_side": "sell",
        }
        self.patch("plugins.trade.agents.x_hyperliquid_agent._current_position_management_context", mock.Mock(side_effect=[(context, None)]))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (exchange, "0x1111111111111111111111111111111111111111", "secret"))

        response = hl.execute({"operation": "set_tp", "exchange": "hyperliquid", "account": "FIBO", "symbol": "BTC", "price": "0"})
        self.assertFalse(response.success)
        self.assertIsNotNone(response.error)
        self.assertIn(response.error.code, {"TP_REMOVAL_TARGET_NOT_FOUND", "AMBIGUOUS_PROTECTION_STATE"})
        self.assertEqual(exchange.requests, [])

    def test_set_tp_zero_noop_only_when_position_tp_absent(self):
        hl = _hl_module()
        exchange = FakeExchange([])
        positions = [CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp=None, sl=None)]
        open_orders = [{"symbol": "BTC", "side": "sell", "size": Decimal("82.81931"), "price": Decimal("76000.0"), "oid": 6001, "reduce_only": False, "is_trigger": False, "is_position_tpsl": False}]
        context = {
            "alias": "FIBO",
            "account": "FIBO",
            "wallet": "0x1111111111111111111111111111111111111111",
            "candidate": self._candidate("BTC"),
            "positions_response": self._position_response(positions),
            "current_position": positions[0],
            "open_orders": open_orders,
            "reference_price": Decimal("64901"),
            "current_size": Decimal("82.81931"),
            "current_side": "long",
            "closing_side": "sell",
        }
        self.patch("plugins.trade.agents.x_hyperliquid_agent._current_position_management_context", mock.Mock(side_effect=[(context, None)]))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (exchange, "0x1111111111111111111111111111111111111111", "secret"))

        response = hl.execute({"operation": "set_tp", "exchange": "hyperliquid", "account": "FIBO", "symbol": "BTC", "price": "0"})
        self.assertTrue(response.success)
        self.assertIsNotNone(response.position_action)
        self.assertFalse(response.position_action.removed)
        self.assertEqual(response.position_action.message, "No Take Profit was set.")
        self.assertEqual(exchange.requests, [])

    def test_set_sl_zero_position_sl_present_but_no_removable_target_fails(self):
        hl = _hl_module()
        exchange = FakeExchange([])
        positions = [CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp=None, sl="64000")]
        open_orders = [{"symbol": "BTC", "side": "sell", "size": Decimal("82.81931"), "price": Decimal("76000.0"), "oid": 6001, "reduce_only": False, "is_trigger": False, "is_position_tpsl": False}]
        context = {
            "alias": "FIBO",
            "account": "FIBO",
            "wallet": "0x1111111111111111111111111111111111111111",
            "candidate": self._candidate("BTC"),
            "positions_response": self._position_response(positions),
            "current_position": positions[0],
            "open_orders": open_orders,
            "reference_price": Decimal("64901"),
            "current_size": Decimal("82.81931"),
            "current_side": "long",
            "closing_side": "sell",
        }
        self.patch("plugins.trade.agents.x_hyperliquid_agent._current_position_management_context", mock.Mock(side_effect=[(context, None)]))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (exchange, "0x1111111111111111111111111111111111111111", "secret"))

        response = hl.execute({"operation": "set_sl", "exchange": "hyperliquid", "account": "FIBO", "symbol": "BTC", "price": "0"})
        self.assertFalse(response.success)
        self.assertIsNotNone(response.error)
        self.assertIn(response.error.code, {"SL_REMOVAL_TARGET_NOT_FOUND", "AMBIGUOUS_PROTECTION_STATE"})
        self.assertEqual(exchange.requests, [])

    def test_set_sl_zero_noop_only_when_position_sl_absent(self):
        hl = _hl_module()
        exchange = FakeExchange([])
        positions = [CanonicalPosition(symbol="BTC", side="long", size="82.81931", entry_price="64491.5", pnl="+14857.95012", tp=None, sl=None)]
        open_orders = [{"symbol": "BTC", "side": "sell", "size": Decimal("82.81931"), "price": Decimal("76000.0"), "oid": 6001, "reduce_only": False, "is_trigger": False, "is_position_tpsl": False}]
        context = {
            "alias": "FIBO",
            "account": "FIBO",
            "wallet": "0x1111111111111111111111111111111111111111",
            "candidate": self._candidate("BTC"),
            "positions_response": self._position_response(positions),
            "current_position": positions[0],
            "open_orders": open_orders,
            "reference_price": Decimal("64901"),
            "current_size": Decimal("82.81931"),
            "current_side": "long",
            "closing_side": "sell",
        }
        self.patch("plugins.trade.agents.x_hyperliquid_agent._current_position_management_context", mock.Mock(side_effect=[(context, None)]))
        self.patch("plugins.trade.agents.x_hyperliquid_agent._build_exchange_client", lambda account: (exchange, "0x1111111111111111111111111111111111111111", "secret"))

        response = hl.execute({"operation": "set_sl", "exchange": "hyperliquid", "account": "FIBO", "symbol": "BTC", "price": "0"})
        self.assertTrue(response.success)
        self.assertIsNotNone(response.position_action)
        self.assertFalse(response.position_action.removed)
        self.assertEqual(response.position_action.message, "No Stop Loss was set.")
        self.assertEqual(exchange.requests, [])

    def test_close_position_uses_current_size_and_verifies_absent(self):
        hl = _hl_module()
        exchange = FakeExchange([{"response": {"data": {"statuses": [{"resting": {"oid": 500}}]}}}])
        pre_positions = [CanonicalPosition(symbol="HYPE", side="short", size="250", entry_price="71.075", pnl="+1", tp=None, sl=None)]
        # Post-submit re-read returns NO positions for HYPE — the close
        # actually took effect (HYPE leg was fully closed).
        post_positions = []
        open_orders = [{"symbol": "BTC", "side": "buy", "oid": 88, "reduce_only": False, "is_position_tpsl": False, "price": "65000"}]
        self._patch_context([self._position_response(pre_positions), self._position_response(post_positions)], [open_orders, open_orders], exchange)

        response = hl.execute({"operation": "close_position", "exchange": "hyperliquid", "account": "FIBO", "symbol": "HYPE"})
        self.assertTrue(response.success)
        self.assertIsNotNone(response.position_action)
        self.assertEqual(response.position_action.status, "success")
        self.assertEqual(len(exchange.requests), 1)
        self.assertEqual(exchange.requests[0][0]["coin"], "HYPE")
        self.assertEqual(exchange.requests[0][0]["sz"], 250.0)

    def test_close_position_missing_current_position_zero_writes(self):
        hl = _hl_module()
        exchange = FakeExchange([])
        pre_positions = [CanonicalPosition(symbol="BTC", side="long", size="1", entry_price="65000", pnl="+1", tp=None, sl=None)]
        self._patch_context([self._position_response(pre_positions)], [[{"symbol": "BTC", "side": "buy", "oid": 88, "reduce_only": False, "is_position_tpsl": False, "price": "65000"}]], exchange)

        response = hl.execute({"operation": "close_position", "exchange": "hyperliquid", "account": "FIBO", "symbol": "HYPE"})
        self.assertFalse(response.success)
        self.assertIsNotNone(response.error)
        self.assertEqual(response.error.code, "POSITION_NOT_FOUND")
        self.assertEqual(exchange.requests, [])

    def test_invalid_tp_and_sl_prices_reject_before_write(self):
        hl = _hl_module()
        exchange = FakeExchange([])
        positions = [CanonicalPosition(symbol="HYPE", side="short", size="250", entry_price="100", pnl="+1", tp=None, sl=None)]
        open_orders = []
        self._patch_context([self._position_response(positions), self._position_response(positions)], [open_orders, open_orders], exchange)

        tp_response = hl.execute({"operation": "set_tp", "exchange": "hyperliquid", "account": "FIBO", "symbol": "HYPE", "price": "120"})
        self.assertFalse(tp_response.success)
        self.assertEqual(tp_response.error.code, "INVALID_TP_PRICE")
        self.assertEqual(exchange.requests, [])

        sl_response = hl.execute({"operation": "set_sl", "exchange": "hyperliquid", "account": "FIBO", "symbol": "HYPE", "price": "90"})
        self.assertFalse(sl_response.success)
        self.assertEqual(sl_response.error.code, "INVALID_SL_PRICE")
        self.assertEqual(exchange.requests, [])

    def test_set_tp_exchange_rejection_does_not_retry(self):
        hl = _hl_module()
        exchange = FakeExchange([{"response": {"data": {"statuses": [{"error": "rejected"}]}}}])
        positions = [CanonicalPosition(symbol="HYPE", side="short", size="250", entry_price="100", pnl="+1", tp=None, sl=None)]
        open_orders = []
        self._patch_context([self._position_response(positions), self._position_response(positions)], [open_orders, open_orders], exchange)

        response = hl.execute({"operation": "set_tp", "exchange": "hyperliquid", "account": "FIBO", "symbol": "HYPE", "price": "65"})
        self.assertFalse(response.success)
        self.assertEqual(len(exchange.requests), 1)

    def test_ambiguous_protection_state_rejects_without_write(self):
        hl = _hl_module()
        exchange = FakeExchange([])
        positions = [CanonicalPosition(symbol="HYPE", side="short", size="250", entry_price="100", pnl="+1", tp=None, sl=None)]
        open_orders = [
            {"symbol": "HYPE", "side": "buy", "oid": 1, "reduce_only": True, "is_position_tpsl": True, "tpsl": "tp", "price": "65", "triggerPx": "65"},
            {"symbol": "HYPE", "side": "buy", "oid": 2, "reduce_only": True, "is_position_tpsl": True, "tpsl": "tp", "price": "66", "triggerPx": "66"},
        ]
        self._patch_context([self._position_response(positions), self._position_response(positions)], [open_orders, open_orders], exchange)

        response = hl.execute({"operation": "set_tp", "exchange": "hyperliquid", "account": "FIBO", "symbol": "HYPE", "price": "65"})
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "AMBIGUOUS_PROTECTION_STATE")
        self.assertEqual(exchange.requests, [])

    def test_wizard_and_tradedesk_remain_exchange_agnostic_and_blind(self):
        wizard_text = (Path(_REPO_ROOT) / "plugins/trade/wizard.py").read_text()
        desk_text = (Path(_REPO_ROOT) / "plugins/trade/tradedesk.py").read_text()
        self.assertNotIn("triggerPx", wizard_text)
        self.assertNotIn("reduce_only", wizard_text)
        self.assertNotIn("market_close", wizard_text)
        self.assertNotIn("triggerPx", desk_text)
        self.assertNotIn("reduce_only", desk_text)
        self.assertNotIn("market_close", desk_text)


class TestTradeDeskAndCanonicalSanity(unittest.TestCase):
    def test_trade_desk_still_routes_canonical_requests(self):
        with tempfile.TemporaryDirectory(prefix="trade_phase4_") as tmp:
            agents_dir = Path(tmp) / "agents"
            agents_dir.mkdir()
            agent_path = agents_dir / "x_example_agent.py"
            agent_path.write_text(
                'name = "example"\n'
                'def list_accounts(): return ["alpha"]\n'
                'def capabilities(): return ["balance", "positions_orders", "cancel_order_group"]\n'
                'def execute(request):\n'
                '    from plugins.trade.canonical import make_success\n'
                '    return make_success(operation=request["operation"], exchange="example", account=request["account"], open_order_count=0, order_groups=[])\n'
            )
            desk = TradeDesk()
            with mock.patch("plugins.trade.tradedesk._agents_dir", return_value=agents_dir):
                response = desk.execute({"operation": "cancel_order_group", "exchange": "example", "account": "alpha", "symbol": "HYPE", "side": "sell"})
            self.assertTrue(response.success)
            self.assertEqual(response.operation, "cancel_order_group")
            self.assertEqual(response.exchange, "example")
            self.assertEqual(response.account, "alpha")


if __name__ == "__main__":
    unittest.main(verbosity=2)
