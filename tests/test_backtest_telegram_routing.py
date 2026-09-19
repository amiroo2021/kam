"""Regression tests for Telegram /backtest routing."""

from __future__ import annotations

import asyncio
import importlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class _DummyMsg:
    def __init__(self, text: str) -> None:
        self.text = text
        self.chat = SimpleNamespace(id=123)
        self.message_thread_id = None


class _DummyQuery:
    def __init__(self) -> None:
        self.answered = False
        self.edits: list[str] = []
        self.message = SimpleNamespace(chat=SimpleNamespace(id=123), message_thread_id=None)

    async def answer(self) -> None:
        self.answered = True

    async def edit_message_text(self, text: str, reply_markup=None) -> None:
        self.edits.append(text)


class _DummyAdapter(SimpleNamespace):
    def __init__(self) -> None:
        super().__init__(name="Telegram", send=None, send_inline_keyboard=None, send_image_file=None, send_document=None)


class BacktestTelegramRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_backtest_command_routes_to_wizard(self) -> None:
        from plugins.trade import backtest_wizard as wizard

        adapter = _DummyAdapter()
        msg = _DummyMsg("/backtest")
        with mock.patch.object(wizard, "_send_screen", new=mock.AsyncMock()) as send_screen:
            handled = await wizard.handle_backtest_command(adapter, msg)
        self.assertTrue(handled)
        send_screen.assert_awaited()

    async def test_backtest_callback_routes_to_callback_handler(self) -> None:
        from plugins.trade import backtest_wizard as wizard

        adapter = _DummyAdapter()
        query = _DummyQuery()
        with mock.patch.object(wizard._WIZARD, "_run_backtest", return_value=wizard.Screen("done", [], "done")) as run_bt:
            await wizard.handle_backtest_callback(adapter, query, "backtest:run")
        self.assertTrue(query.answered)
        self.assertTrue(run_bt.called)
        self.assertTrue(any("done" in text for text in query.edits), query.edits)

    def test_trade_and_backtest_share_registration_pattern(self) -> None:
        from plugins.trade import register as trade_register

        class _Ctx:
            def __init__(self) -> None:
                self.entries = {}

            def register_command(self, name, handler, description="", args_hint=""):
                self.entries[name] = handler

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "kam").mkdir(parents=True)
            (home / "kam" / "install_state.json").write_text('{"capabilities":{"trade":true,"fibo":true}}')
            old = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = str(home)
            try:
                ctx = _Ctx()
                trade_register(ctx)
            finally:
                if old is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = old

        self.assertIn("trade", ctx.entries)
        self.assertIn("backtest", ctx.entries)
        self.assertIn("fibo", ctx.entries)


if __name__ == "__main__":
    unittest.main()
