from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from plugins.trade import backtest_wizard as wizard


class _DummyMsg:
    def __init__(self, text: str, thread_id=None) -> None:
        self.text = text
        self.chat = SimpleNamespace(id=123)
        self.message_thread_id = thread_id


class _DummyQuery:
    def __init__(self, msg: _DummyMsg) -> None:
        self.message = msg
        self.answered = False
        self.edits: list[str] = []

    async def answer(self) -> None:
        self.answered = True

    async def edit_message_text(self, text: str, reply_markup=None) -> None:
        self.edits.append(text)


class _DummyAdapter:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append((chat_id, text, metadata))

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)


class BacktestYearRoutingTests(unittest.TestCase):
    def _open_via_public_handler(self, msg: _DummyMsg) -> None:
        asyncio.run(wizard.handle_backtest_command(_DummyAdapter(), msg))

    def _drive_to_year_via_public_handlers(self, msg: _DummyMsg) -> _DummyQuery:
        self._open_via_public_handler(msg)
        query = _DummyQuery(msg)
        for suffix in [
            "ladder:sell",
            "market:futures",
            "symbol:BTC",
            "confirm:instrument",
            "pct:0.001",
            "day:1",
            "month:6",
        ]:
            asyncio.run(wizard.handle_backtest_callback(_DummyAdapter(), query, f"backtest:{suffix}"))
        self.assertEqual(wizard._WIZARD._state(wizard._chat_key_from_message(msg)).awaiting_text, "year")
        return query

    def test_year_input_advances_and_sends_confirm_screen(self) -> None:
        msg = _DummyMsg("/backtest", thread_id=456)
        self._drive_to_year_via_public_handlers(msg)
        key = wizard._chat_key_from_message(msg)
        adapter = _DummyAdapter()
        handled = asyncio.run(wizard.handle_backtest_text(adapter, _DummyMsg("2026", thread_id=456)))
        self.assertTrue(handled)
        self.assertEqual(wizard._WIZARD._state(key).year, 2026)
        self.assertIsNone(wizard._WIZARD._state(key).awaiting_text)
        self.assertTrue(adapter.sent)
        sent = adapter.sent[-1]
        self.assertEqual(sent[0], "123")
        self.assertIn("Run SELL backtest?", sent[1])
        self.assertIn("Start: 2026-06-01T00:01:00Z", sent[1])

    def test_invalid_year_stays_on_year_prompt(self) -> None:
        msg = _DummyMsg("/backtest", thread_id=456)
        self._drive_to_year_via_public_handlers(msg)
        key = wizard._chat_key_from_message(msg)
        screen = wizard._WIZARD.handle_text(key, "abcd")
        self.assertIsNotNone(screen)
        self.assertEqual(screen.state, "await_year")
        self.assertIn("Invalid year/date", screen.text)
        self.assertIsNone(wizard._WIZARD._state(key).year)
        self.assertEqual(wizard._WIZARD._state(key).awaiting_text, "year")

    def test_handle_backtest_text_routes_real_message(self) -> None:
        msg = _DummyMsg("2026", thread_id=456)
        key = wizard._chat_key_from_message(msg)
        self._drive_to_year_via_public_handlers(_DummyMsg("/backtest", thread_id=456))
        adapter = _DummyAdapter()
        handled = asyncio.run(wizard.handle_backtest_text(adapter, msg))
        self.assertTrue(handled)
        self.assertEqual(wizard._WIZARD._state(key).year, 2026)
        self.assertIsNone(wizard._WIZARD._state(key).awaiting_text)
        self.assertTrue(adapter.sent)
        sent = adapter.sent[-1]
        self.assertEqual(sent[0], "123")
        self.assertIn("Run SELL backtest?", sent[1])
        self.assertIn("Start: 2026-06-01T00:01:00Z", sent[1])

    def test_thread_none_and_threaded_sessions_do_not_collide(self) -> None:
        msg_none = _DummyMsg("/backtest", thread_id=None)
        msg_thread = _DummyMsg("/backtest", thread_id=456)
        self._drive_to_year_via_public_handlers(msg_none)
        self._drive_to_year_via_public_handlers(msg_thread)
        key_none = wizard._chat_key_from_message(msg_none)
        key_thread = wizard._chat_key_from_message(msg_thread)
        self.assertNotEqual(key_none, key_thread)
        self.assertEqual(wizard._WIZARD._state(key_none).awaiting_text, "year")
        self.assertEqual(wizard._WIZARD._state(key_thread).awaiting_text, "year")
        self.assertIsNot(wizard._WIZARD._state(key_none), wizard._WIZARD._state(key_thread))
        self.assertIsNotNone(wizard._WIZARD.handle_text(key_none, "2026"))
        self.assertEqual(wizard._WIZARD._state(key_none).year, 2026)
        self.assertIsNone(wizard._WIZARD._state(key_none).awaiting_text)
        self.assertEqual(wizard._WIZARD._state(key_thread).awaiting_text, "year")


if __name__ == "__main__":
    unittest.main()
