"""Regression tests for /backtest callback failure handling."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from plugins.trade import backtest_wizard as wizard


class _DummyQuery:
    def __init__(self) -> None:
        self.edits: list[str] = []
        self.answered = False
        self.message = SimpleNamespace(chat=SimpleNamespace(id=123), message_thread_id=None)

    async def answer(self) -> None:
        self.answered = True

    async def edit_message_text(self, text: str, reply_markup=None) -> None:
        self.edits.append(text)


class BacktestCallbackFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_backtest_failure_is_visible_to_user(self) -> None:
        query = _DummyQuery()
        adapter = SimpleNamespace(send_inline_keyboard=None, send=None, send_image_file=None, send_document=None)
        with mock.patch.object(wizard, "initialize_backtest_caches_off_loop", new=mock.AsyncMock()), mock.patch.object(
            wizard._WIZARD, "_run_backtest", side_effect=RuntimeError("boom")
        ):
            await wizard.handle_backtest_callback(adapter, query, "backtest:run")
        self.assertTrue(query.answered)
        self.assertTrue(any("Backtest failed:" in text for text in query.edits), query.edits)

    async def test_cache_initialization_failure_is_visible_without_running_backtest(self) -> None:
        query = _DummyQuery()
        adapter = SimpleNamespace(send_inline_keyboard=None, send=None, send_image_file=None, send_document=None)
        unavailable = wizard.BacktestCacheUnavailable("Backtest cache initialization timed out. Please try again shortly.")
        with mock.patch.object(
            wizard, "initialize_backtest_caches_off_loop", new=mock.AsyncMock(side_effect=unavailable)
        ), mock.patch.object(wizard._WIZARD, "_run_backtest") as run_backtest:
            await wizard.handle_backtest_callback(adapter, query, "backtest:run")
        self.assertTrue(query.answered)
        self.assertFalse(run_backtest.called)
        self.assertTrue(any("cache initialization timed out" in text.lower() for text in query.edits), query.edits)


if __name__ == "__main__":
    unittest.main()
