from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade import register  # noqa: E402


class FakeCtx:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def register_command(self, name: str, handler: Any, description: str, args_hint: str) -> None:
        self.calls.append(
            {
                "name": name,
                "handler": handler,
                "description": description,
                "args_hint": args_hint,
            }
        )


class FakeTradeDesk:
    def list_exchanges(self) -> List[str]:
        return ["ondoperps"]

    def list_accounts(self, exchange: str) -> List[str]:
        if exchange == "ondoperps":
            return ["amiroo", "bitget"]
        return []


class FakeFiboService:
    def __init__(self) -> None:
        self.commands: List[Dict[str, Any]] = []
        self.active = {
            "ondoperps:amiroo:ONDO:counterSELL": {
                "registration_key": "ondoperps:amiroo:ONDO:counterSELL",
                "exchange": "ondoperps",
                "account": "amiroo",
                "instrument": "ONDO",
                "counter_side": "counterSELL",
                "status": "running",
                "frozen": False,
            },
            "ondoperps:bitget:XAU:counterBUY": {
                "registration_key": "ondoperps:bitget:XAU:counterBUY",
                "exchange": "ondoperps",
                "account": "bitget",
                "instrument": "XAU",
                "counter_side": "counterBUY",
                "status": "frozen",
                "frozen": True,
            },
        }
        self.details = {
            "ondoperps:amiroo:ONDO:counterSELL": {
                "registration_key": "ondoperps:amiroo:ONDO:counterSELL",
                "status": "running",
                "exchange": "ondoperps",
                "account": "amiroo",
                "instrument": "ONDO",
                "counter_side": "counterSELL",
                "current_mark_price": 0.3274,
                "step0": 0.331011,
                "step0tp": 0.3317061231,
                "step1": 0.3298855626014,
                "step2": 0.3280640202638674,
                "step3": 0.3251177038634221,
                "step4": 0.3203543115451728,
                "step5": 0.31265192136599924,
                "highest_activated_level": 2,
                "activated_levels": [1, 2],
                "configured_c1": 1.0,
                "configured_c2": 0.0,
                "configured_c3": 0.0,
                "configured_c4": 0.0,
                "cumulative_real_volume": 1.0,
                "position_side": "short",
                "position_size": 1.0,
                "average_entry_price": 0.32966,
                "current_strategy_raw_sl": 0.3298855626014,
                "actual_exchange_sl": 0.32989,
                "current_strategy_raw_tp": None,
                "actual_exchange_tp": None,
                "frozen_reason": "",
                "completed_cycles": 1,
                "profitable_cycles": 1,
                "losing_cycles": 0,
                "total_realized_pnl": 0.00152,
                "fees": 0.000082,
            },
            "ondoperps:bitget:XAU:counterBUY": {
                "registration_key": "ondoperps:bitget:XAU:counterBUY",
                "status": "frozen",
                "exchange": "ondoperps",
                "account": "bitget",
                "instrument": "XAU",
                "counter_side": "counterBUY",
                "current_mark_price": 4372.286816,
                "step0": 4359.18,
                "step0tp": 4358.2645722,
                "step1": 4360.6621212,
                "step2": 4363.06048536666,
                "step3": 4366.943609198636,
                "step4": 4373.232007995883,
                "step5": 4383.421638574513,
                "highest_activated_level": 4,
                "activated_levels": [1, 2, 3, 4],
                "configured_c1": 0.01,
                "configured_c2": 0.0,
                "configured_c3": 0.0,
                "configured_c4": 0.0,
                "cumulative_real_volume": 0.01,
                "position_side": "long",
                "position_size": 0.01,
                "average_entry_price": 4360.86,
                "current_strategy_raw_sl": 4366.943609198636,
                "actual_exchange_sl": 4366.94,
                "current_strategy_raw_tp": 4383.421638574513,
                "actual_exchange_tp": 4383.42,
                "frozen_reason": "ORDER_VERIFY_FAILED",
                "completed_cycles": 3,
                "profitable_cycles": 2,
                "losing_cycles": 1,
                "total_realized_pnl": 0.0802,
                "fees": 0.032754,
            },
        }
        self.stop_close_results: Dict[str, Dict[str, Any]] = {
            "ondoperps:bitget:XAU:counterBUY": {
                "ok": True,
                "registration_key": "ondoperps:bitget:XAU:counterBUY",
                "verified_clean": True,
            }
        }

    def execute_command(self, command: Dict[str, Any]) -> Dict[str, Any]:
        self.commands.append(dict(command))
        op = command.get("op")
        if op == "start":
            key = f"{command['exchange']}:{command['account']}:{str(command['instrument']).upper()}:{command['counter_side']}"
            if key in self.active:
                return {"ok": False, "error": "DUPLICATE_REGISTRATION", "registration_key": key}
            self.active[key] = {
                "registration_key": key,
                "exchange": command["exchange"],
                "account": command["account"],
                "instrument": str(command["instrument"]).upper(),
                "counter_side": command["counter_side"],
                "status": "running",
                "frozen": False,
            }
            return {"ok": True, "registration_key": key}
        if op == "list":
            return {
                "ok": True,
                "registrations": [self.active[key] for key in sorted(self.active)],
            }
        if op == "detail":
            return {"ok": True, "detail": dict(self.details[command["registration_key"]])}
        if op == "stop_close":
            key = command["registration_key"]
            result = dict(self.stop_close_results.get(key, {"ok": False, "error": "OWNERSHIP_MISMATCH", "registration_key": key}))
            if result.get("ok"):
                self.active.pop(key, None)
            return result
        raise AssertionError(f"unexpected op: {op}")


class FakeAdapter:
    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    async def send_inline_keyboard(self, *, chat_id, text, buttons, callback_prefix, metadata=None):
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "buttons": buttons,
                "callback_prefix": callback_prefix,
                "metadata": metadata,
            }
        )


class FakeChat:
    def __init__(self, chat_id: str) -> None:
        self.id = chat_id


class FakeMessage:
    def __init__(self, text: str, chat_id: str = "chat", thread_id: int | None = None) -> None:
        self.text = text
        self.chat = FakeChat(chat_id)
        self.message_thread_id = thread_id


class PluginRegistrationTests(unittest.TestCase):
    def test_register_adds_fibo_slash_command(self) -> None:
        ctx = FakeCtx()
        register(ctx)
        names = [call["name"] for call in ctx.calls]
        self.assertIn("fibo", names)


class TelegramDispatchSourceTests(unittest.TestCase):
    def test_installed_telegram_adapter_contains_fibo_command_dispatch(self) -> None:
        src = Path("/usr/local/lib/hermes-agent/plugins/platforms/telegram/adapter.py").read_text(encoding="utf-8")
        self.assertIn("from plugins.trade.fibo_wizard import handle_fibo_command", src)
        self.assertIn("await handle_fibo_command(self, msg)", src)
        self.assertIn('if cmd_body == "fibo"', src)

    def test_installed_telegram_adapter_contains_fibo_callback_dispatch(self) -> None:
        src = Path("/usr/local/lib/hermes-agent/plugins/platforms/telegram/adapter.py").read_text(encoding="utf-8")
        self.assertIn('data.startswith("fibo:")', src)
        self.assertIn("from plugins.trade.fibo_wizard import handle_fibo_callback", src)
        self.assertIn("await handle_fibo_callback(self, query, data)", src)

    def test_installed_telegram_adapter_contains_fibo_text_dispatch(self) -> None:
        src = Path("/usr/local/lib/hermes-agent/plugins/platforms/telegram/adapter.py").read_text(encoding="utf-8")
        self.assertIn("from plugins.trade.fibo_wizard import handle_fibo_text", src)
        self.assertIn("await handle_fibo_text(self, msg)", src)


class FiboWizardFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        from plugins.trade.fibo_wizard import FiboWizard

        self.service = FakeFiboService()
        self.wizard = FiboWizard(tradedesk=FakeTradeDesk(), service=self.service)
        self.key = ("chat",)

    def test_main_menu_matches_new_architecture(self) -> None:
        screen = self.wizard.open(self.key)
        self.assertIn("🧬 Fibo Robot", screen.text)
        labels = [button["text"] for row in screen.buttons for button in row]
        self.assertEqual(labels, ["▶️ Start Fibo", "🔵 Running Fibo", "🛑 STOP Fibo", "✕ Exit"])
        self.assertNotIn("Connected Exchanges", screen.text)

    def test_start_fibo_sends_one_standardized_start_command(self) -> None:
        self.wizard.open(self.key)
        self.wizard.handle_callback(self.key, "menu:start")
        self.wizard.handle_callback(self.key, "exchange:ondoperps")
        self.wizard.handle_callback(self.key, "account:bitget")
        self.wizard.handle_callback(self.key, "symbol:ETH")
        self.wizard.handle_callback(self.key, "counter_side:counterBUY")
        self.wizard.handle_text(self.key, "1000")
        self.wizard.handle_text(self.key, "0.01")
        self.wizard.handle_text(self.key, "0")
        self.wizard.handle_callback(self.key, "default:counter3")
        self.wizard.handle_text(self.key, "0")
        result = self.wizard.handle_callback(self.key, "confirm_start")
        self.assertIn("started", result.text.lower())
        self.assertEqual(len(self.service.commands), 1)
        command = self.service.commands[0]
        self.assertEqual(command["op"], "start")
        self.assertEqual(command["exchange"], "ondoperps")
        self.assertEqual(command["account"], "bitget")
        self.assertEqual(command["instrument"], "ETH")
        self.assertEqual(command["counter_side"], "counterBUY")
        self.assertEqual(command["divide_percent"], 1000.0)
        self.assertEqual(command["counter1"], 0.01)
        self.assertEqual(command["counter2"], 0.0)
        self.assertEqual(command["counter3"], 0.5)
        self.assertEqual(command["counter4"], 0.0)
        self.assertEqual(command["poll_seconds"], 2.0)

    def test_duplicate_start_is_rejected(self) -> None:
        self.wizard.open(self.key)
        self.wizard.handle_callback(self.key, "menu:start")
        self.wizard.handle_callback(self.key, "exchange:ondoperps")
        self.wizard.handle_callback(self.key, "account:bitget")
        self.wizard.handle_callback(self.key, "symbol:XAU")
        self.wizard.handle_callback(self.key, "counter_side:counterBUY")
        self.wizard.handle_callback(self.key, "default:divide_percent")
        self.wizard.handle_callback(self.key, "default:counter1")
        self.wizard.handle_callback(self.key, "default:counter2")
        self.wizard.handle_callback(self.key, "default:counter3")
        self.wizard.handle_callback(self.key, "default:counter4")
        result = self.wizard.handle_callback(self.key, "confirm_start")
        self.assertIn("duplicate", result.text.lower())
        self.assertEqual(len(self.service.commands), 1)
        self.assertEqual(self.service.commands[0]["op"], "start")

    def test_running_fibo_comes_from_service_state_not_ps(self) -> None:
        screen = self.wizard.handle_callback(self.key, "menu:running")
        self.assertIn("Ondoperps / amiroo / ONDO / Counter SELL", screen.text)
        self.assertIn("Ondoperps / bitget / XAU / Counter BUY", screen.text)
        self.assertEqual(len(self.service.commands), 1)
        self.assertEqual(self.service.commands[0], {"op": "list"})

    def test_running_detail_targets_correct_registration_and_refresh_is_read_only(self) -> None:
        self.wizard.handle_callback(self.key, "menu:running")
        screen = self.wizard.handle_callback(self.key, "registration:ondoperps%3Abitget%3AXAU%3AcounterBUY")
        self.assertIn("Status: frozen", screen.text)
        self.assertIn("Exchange: Ondoperps", screen.text)
        self.assertIn("Account: bitget", screen.text)
        self.assertIn("Instrument: XAU", screen.text)
        self.assertIn("Counter Side: Counter BUY", screen.text)
        self.assertIn("Completed Fibo Cycles: 3", screen.text)
        self.assertIn("Total Realized P&L: 0.0802", screen.text)
        self.assertIn("Fees: 0.032754", screen.text)
        labels = {button["text"] for row in screen.buttons for button in row}
        self.assertEqual(labels, {"🔄 Refresh", "◀️ Back", "✕ Exit"})
        before = len(self.service.commands)
        refreshed = self.wizard.handle_callback(self.key, "refresh")
        self.assertIn("Status: frozen", refreshed.text)
        self.assertEqual(self.service.commands[before]["op"], "detail")
        self.assertEqual(self.service.commands[-1]["op"], "detail")
        self.assertFalse(any(cmd["op"] == "stop_close" for cmd in self.service.commands))

    def test_stop_menu_lists_running_registrations_globally(self) -> None:
        screen = self.wizard.handle_callback(self.key, "menu:stop")
        self.assertIn("Ondoperps / amiroo / ONDO / Counter SELL", screen.text)
        self.assertIn("Ondoperps / bitget / XAU / Counter BUY", screen.text)
        self.assertEqual(self.service.commands[-1], {"op": "list"})

    def test_stop_requires_explicit_confirmation(self) -> None:
        self.wizard.handle_callback(self.key, "menu:stop")
        confirm = self.wizard.handle_callback(self.key, "registration:ondoperps%3Abitget%3AXAU%3AcounterBUY")
        self.assertIn("STOP & CLOSE", confirm.text)
        self.assertIn("close the current position", confirm.text)
        self.assertIn("remove its TP", confirm.text)
        self.assertIn("remove its SL", confirm.text)
        self.assertEqual([cmd for cmd in self.service.commands if cmd["op"] == "stop_close"], [])
        result = self.wizard.handle_callback(self.key, "confirm_stop_close")
        stop_calls = [cmd for cmd in self.service.commands if cmd["op"] == "stop_close"]
        self.assertEqual(len(stop_calls), 1)
        self.assertEqual(stop_calls[0]["registration_key"], "ondoperps:bitget:XAU:counterBUY")
        self.assertIn("verified clean", result.text.lower())
        self.assertIn("ondoperps:amiroo:ONDO:counterSELL", self.service.active)
        self.assertNotIn("ondoperps:bitget:XAU:counterBUY", self.service.active)

    def test_stop_close_ownership_block_is_reported(self) -> None:
        self.service.stop_close_results["ondoperps:amiroo:ONDO:counterSELL"] = {
            "ok": False,
            "registration_key": "ondoperps:amiroo:ONDO:counterSELL",
            "error": "OWNERSHIP_MISMATCH",
            "message": "Refusing STOP & CLOSE: lane ownership is ambiguous.",
        }
        self.wizard.handle_callback(self.key, "menu:stop")
        self.wizard.handle_callback(self.key, "registration:ondoperps%3Aamiroo%3AONDO%3AcounterSELL")
        result = self.wizard.handle_callback(self.key, "confirm_stop_close")
        self.assertIn("ownership", result.text.lower())
        self.assertIn("ambiguous", result.text.lower())
        self.assertIn("ondoperps:amiroo:ONDO:counterSELL", self.service.active)

    def test_poll_interval_not_shown_in_wizard(self) -> None:
        self.wizard.open(self.key)
        self.wizard.handle_callback(self.key, "menu:start")
        self.wizard.handle_callback(self.key, "exchange:ondoperps")
        self.wizard.handle_callback(self.key, "account:amiroo")
        self.wizard.handle_callback(self.key, "symbol:ONDO")
        screen = self.wizard.handle_callback(self.key, "counter_side:counterSELL")
        self.assertIn("Divide Percent", screen.text)
        self.assertNotIn("poll", screen.text.lower())


class FiboCommandHandlerTests(unittest.TestCase):
    def test_handle_fibo_command_exact_match_and_callback_prefix(self) -> None:
        from plugins.trade import fibo_wizard as fibo_module

        fibo_module._WIZARD = fibo_module.FiboWizard(tradedesk=FakeTradeDesk(), service=FakeFiboService())
        adapter = FakeAdapter()

        async def _run() -> None:
            handled = await fibo_module.handle_fibo_command(adapter, FakeMessage("/fibo"))
            self.assertTrue(handled)
            self.assertEqual(len(adapter.sent), 1)
            self.assertEqual(adapter.sent[0]["callback_prefix"], "fibo")
            self.assertIn("🧬 Fibo Robot", adapter.sent[0]["text"])

            adapter.sent.clear()
            handled = await fibo_module.handle_fibo_command(adapter, FakeMessage("/fibo extra"))
            self.assertTrue(handled)
            self.assertEqual(adapter.sent[0]["callback_prefix"], "fibo")

            adapter.sent.clear()
            handled = await fibo_module.handle_fibo_command(adapter, FakeMessage("/fiber"))
            self.assertFalse(handled)
            self.assertEqual(adapter.sent, [])

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
