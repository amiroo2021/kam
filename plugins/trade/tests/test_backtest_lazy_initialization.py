"""Backtest import must not open or migrate SQLite caches."""
from __future__ import annotations

import ast
import asyncio
import time
import unittest
from pathlib import Path
from unittest import mock

from plugins.trade import backtest_wizard as wizard


class BacktestLazyInitializationTests(unittest.TestCase):
    def test_cache_constructors_are_not_called_at_module_import(self) -> None:
        source = Path(__file__).resolve().parents[1] / "backtest_wizard.py"
        module = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        calls = []
        for statement in module.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            value = statement.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                if value.func.id in {"KlineCache", "AggTradeCache"}:
                    calls.append((value.func.id, statement.lineno))
        self.assertEqual([], calls, "SQLite caches must be initialized lazily, not during module import")

    def test_cache_initialization_timeout_returns_clear_unavailable_error(self) -> None:
        def blocked_initializer():
            time.sleep(0.1)

        with mock.patch.object(wizard, "_initialize_backtest_caches", side_effect=blocked_initializer), mock.patch.object(
            wizard, "_BACKTEST_CACHE_INITIALIZATION_TIMEOUT_SECONDS", 0.01
        ):
            with self.assertRaises(wizard.BacktestCacheUnavailable) as raised:
                asyncio.run(wizard.initialize_backtest_caches_off_loop())
        self.assertIn("timed out", str(raised.exception).lower())

    def test_blocked_initialization_does_not_block_event_loop(self) -> None:
        def blocked_initializer():
            time.sleep(0.1)

        async def exercise() -> float:
            with mock.patch.object(wizard, "_initialize_backtest_caches", side_effect=blocked_initializer), mock.patch.object(
                wizard, "_BACKTEST_CACHE_INITIALIZATION_TIMEOUT_SECONDS", 0.01
            ):
                started = asyncio.get_running_loop().time()
                task = asyncio.create_task(wizard.initialize_backtest_caches_off_loop())
                await asyncio.sleep(0.001)
                elapsed = asyncio.get_running_loop().time() - started
                with self.assertRaises(wizard.BacktestCacheUnavailable):
                    await task
                return elapsed

        self.assertLess(asyncio.run(exercise()), 0.05)


if __name__ == "__main__":
    unittest.main()
