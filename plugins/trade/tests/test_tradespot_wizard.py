"""Unit tests for the Telegram /tradespot SPOT wizard.

Offline only. No live exchange or Telegram calls.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.canonical import (  # noqa: E402
    CanonicalBalance,
    CanonicalOrderGroup,
    CanonicalResponse,
    make_success,
)
from plugins.trade.spotdesk import (  # noqa: E402
    SpotDesk,
    _exchange_name_from_filename,
    _iter_agent_files,
)
from plugins.trade.tradedesk import _exchange_name_from_filename as trade_exchange_name_from_filename  # noqa: E402
from plugins.trade.tradespot_wizard import TradeSpotWizard, handle_tradespot_command  # noqa: E402


def _labels(screen: Any) -> List[str]:
    return [btn["text"] for row in screen.buttons for btn in row]


def _callbacks(screen: Any) -> List[str]:
    return [btn["callback_data"] for row in screen.buttons for btn in row]


class SpotDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="spot_agents_")
        self.agents = Path(self.tmp.name)

    def tearDown(self) -> None:
        for name in list(sys.modules):
            if name.startswith("plugins.trade.agents.x_") and name.endswith("_agent_spot"):
                sys.modules.pop(name, None)
        self.tmp.cleanup()

    def _write_agent(self, filename: str, name: str, caps: List[str] | None = None) -> None:
        caps = caps or ["balance"]
        Path(self.agents, filename).write_text(
            "from plugins.trade.canonical import CanonicalBalance, make_success\n"
            f"name = {name!r}\n"
            "def list_accounts(): return ['amiroo', {'account': 'bitget', 'label': 'bitget'}]\n"
            f"def capabilities(): return {caps!r}\n"
            "def execute(request):\n"
            "    return make_success(request.get('operation',''), name, request.get('account',''), balance=CanonicalBalance('1.00', 'USDC'))\n",
            encoding="utf-8",
        )

    def test_spot_filename_parser_accepts_exact_spot_convention(self) -> None:
        self.assertEqual(_exchange_name_from_filename("x_raydium_agent_spot.py"), "raydium")
        self.assertEqual(_exchange_name_from_filename("x_aftermath_agent_spot.py"), "aftermath")
        self.assertEqual(_exchange_name_from_filename("x_mexc_agent_spot.py"), "mexc")

    def test_spot_filename_parser_rejects_normal_agents_and_malformed_files(self) -> None:
        self.assertIsNone(_exchange_name_from_filename("x_hyperliquid_agent.py"))
        self.assertIsNone(_exchange_name_from_filename("x__agent_spot.py"))
        self.assertIsNone(_exchange_name_from_filename("x_42bad_agent_spot.py"))
        self.assertIsNone(_exchange_name_from_filename("helper.py"))

    def test_spotdesk_discovers_only_valid_spot_agents(self) -> None:
        self._write_agent("x_raydium_agent_spot.py", "raydium")
        self._write_agent("x_aftermath_agent_spot.py", "aftermath")
        self._write_agent("x_mexc_agent.py", "mexc")  # normal /trade file, must be ignored
        Path(self.agents, "x_bad_agent_spot.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        Path(self.agents, "x_42bad_agent_spot.py").write_text("name='bad'\n", encoding="utf-8")
        desk = SpotDesk(agents_dir=self.agents)
        self.assertEqual(desk.list_exchanges(), ["aftermath", "raydium"])

    def test_iter_agent_files_ignores_normal_agents_tests_helpers_and_malformed(self) -> None:
        for filename in [
            "x_raydium_agent_spot.py",
            "x_hyperliquid_agent.py",
            "test_spot.py",
            "_helper.py",
            "x_42bad_agent_spot.py",
        ]:
            Path(self.agents, filename).write_text("# test\n", encoding="utf-8")
        self.assertEqual([p.name for p in _iter_agent_files(self.agents)], ["x_raydium_agent_spot.py"])

    def test_spot_agents_are_not_accidentally_added_to_trade_discovery(self) -> None:
        self.assertIsNone(trade_exchange_name_from_filename("x_raydium_agent_spot.py"))
        self.assertEqual(trade_exchange_name_from_filename("x_hyperliquid_agent.py"), "hyperliquid")


class FakeSpotDesk:
    def __init__(self, caps: List[str] | None = None) -> None:
        self.requests: List[Dict[str, Any]] = []
        self._caps = caps if caps is not None else ["balance", "orders"]

    def list_exchanges(self) -> List[str]:
        return ["aftermath", "raydium"]

    def list_accounts(self, exchange: str) -> List[Any]:
        if exchange == "raydium":
            return ["amiroo", {"account": "bitget", "label": "bitget"}]
        if exchange == "aftermath":
            return ["wallet"]
        return []

    def capabilities(self, exchange: str) -> List[str]:
        return list(self._caps) if exchange in {"aftermath", "raydium"} else []

    def execute(self, request: Dict[str, Any]) -> CanonicalResponse:
        self.requests.append(dict(request))
        op = str(request.get("operation") or "")
        ex = str(request.get("exchange") or "")
        acct = str(request.get("account") or "")
        if op == "balance":
            return make_success(op, ex, acct, balance=CanonicalBalance("12.34", "USDC"), data={"assets": [{"asset": "SOL", "amount": "2"}]})
        if op == "orders":
            return make_success(op, ex, acct, open_order_count=1, order_groups=[CanonicalOrderGroup(symbol="SOL/USDC", side="buy", order_count=1, total_size="2", vwap="10", min_price="10", max_price="10")])
        if op == "positions_orders":
            return make_success(op, ex, acct, open_order_count=0, order_groups=[])
        return make_success(op, ex, acct, data={"ok": True})


class TradeSpotWizardNavigationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.desk = FakeSpotDesk()
        self.wizard = TradeSpotWizard(spotdesk=self.desk)  # type: ignore[arg-type]
        self.key = ("chat",)

    def test_exchange_selection_works(self) -> None:
        screen = self.wizard.open(self.key)
        self.assertIn("🟦 Spot Trading", screen.text)
        self.assertIn("aftermath", _labels(screen))
        self.assertIn("raydium", _labels(screen))
        self.assertNotIn("hyperliquid", _labels(screen))
        self.assertTrue(all(not cb.startswith("trade:") for cb in _callbacks(screen)))

    def test_account_selection_works(self) -> None:
        self.wizard.open(self.key)
        screen = self.wizard.handle_callback(self.key, "exchange:raydium")
        self.assertIn("Exchange: raydium", screen.text)
        self.assertIn("amiroo", _labels(screen))
        self.assertIn("bitget", _labels(screen))
        screen = self.wizard.handle_callback(self.key, "account:amiroo")
        self.assertIn("Exchange: raydium", screen.text)
        self.assertIn("Account: amiroo", screen.text)

    def test_back_and_close_work(self) -> None:
        self.wizard.open(self.key)
        self.wizard.handle_callback(self.key, "exchange:raydium")
        screen = self.wizard.handle_callback(self.key, "back")
        self.assertIn("Select Exchange", screen.text)
        closed = self.wizard.handle_callback(self.key, "close")
        self.assertEqual(closed.state, "closed")
        self.assertEqual(closed.buttons, [])

    def test_capability_filtering_hides_unsupported_operations(self) -> None:
        desk = FakeSpotDesk(caps=["balance"])
        wizard = TradeSpotWizard(spotdesk=desk)  # type: ignore[arg-type]
        wizard.open(self.key)
        wizard.handle_callback(self.key, "exchange:raydium")
        screen = wizard.handle_callback(self.key, "account:amiroo")
        labels = _labels(screen)
        self.assertIn("💰 Balance", labels)
        self.assertNotIn("📋 Orders", labels)
        self.assertNotIn("➕ New Order", labels)
        self.assertNotIn("🪜 Ladder", labels)
        self.assertNotIn("❌ Cancel Orders", labels)

    def test_balance_and_orders_route_to_spotdesk(self) -> None:
        self.wizard.open(self.key)
        self.wizard.handle_callback(self.key, "exchange:raydium")
        self.wizard.handle_callback(self.key, "account:amiroo")
        balance = self.wizard.handle_callback(self.key, "action:balance")
        self.assertIn("12.34 USDC", balance.text)
        self.assertEqual(self.desk.requests[-1]["operation"], "balance")
        orders = self.wizard.handle_callback(self.key, "back")
        self.assertEqual(orders.state, "action")
        orders = self.wizard.handle_callback(self.key, "action:orders")
        self.assertIn("Open orders: 1", orders.text)
        self.assertEqual(self.desk.requests[-1]["operation"], "orders")

    def test_callback_namespace_does_not_collide_with_trade(self) -> None:
        screen = self.wizard.open(self.key)
        self.assertTrue(all(not cb.startswith("trade:") for cb in _callbacks(screen)))
        self.assertTrue(all(not cb.startswith("tradespot:") for cb in _callbacks(screen)))


class TradeSpotCommandRegistrationTests(unittest.TestCase):
    def test_tradespot_command_is_registered_with_trade_capability(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kam_home_") as home:
            state_dir = Path(home) / "kam"
            state_dir.mkdir(parents=True)
            (state_dir / "install_state.json").write_text('{"capabilities":{"trade":true}}', encoding="utf-8")
            old_home = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = home
            try:
                import plugins.trade as trade_plugin

                importlib.reload(trade_plugin)
                self.assertIn("tradespot", trade_plugin.registered_commands())
            finally:
                if old_home is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = old_home

    def test_tradespot_command_handler_uses_tradespot_prefix(self) -> None:
        class Chat:
            id = 123

        class Msg:
            text = "/tradespot"
            chat = Chat()
            message_thread_id = None

        class Adapter:
            def __init__(self) -> None:
                self.sent: List[Dict[str, Any]] = []

            async def send_inline_keyboard(self, **kwargs: Any) -> None:
                self.sent.append(dict(kwargs))

        adapter = Adapter()
        handled = asyncio.run(handle_tradespot_command(adapter, Msg()))
        self.assertTrue(handled)
        self.assertEqual(adapter.sent[-1]["callback_prefix"], "tradespot")
        self.assertIn("Spot Trading", adapter.sent[-1]["text"])


if __name__ == "__main__":
    unittest.main()
