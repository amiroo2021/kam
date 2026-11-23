"""Backtest import must not open or migrate SQLite caches."""
from __future__ import annotations

import ast
import asyncio
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from plugins.trade import backtest_wizard as wizard


class BacktestLazyInitializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_cache = wizard._BACKTEST_CACHE
        wizard._BACKTEST_CACHE = None
        wizard._BACKTEST_INITIALIZATION_TASK = None

    def tearDown(self) -> None:
        wizard._BACKTEST_CACHE = self._previous_cache
        wizard._BACKTEST_INITIALIZATION_TASK = None

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

        with mock.patch.object(wizard, "_initialize_backtest_kline_cache", side_effect=blocked_initializer), mock.patch.object(
            wizard, "_BACKTEST_CACHE_INITIALIZATION_TIMEOUT_SECONDS", 0.01
        ):
            with self.assertRaises(wizard.BacktestCacheUnavailable) as raised:
                asyncio.run(wizard.initialize_backtest_caches_off_loop())
        self.assertIn("timed out", str(raised.exception).lower())

    def test_blocked_initialization_does_not_block_event_loop(self) -> None:
        def blocked_initializer():
            time.sleep(0.1)

        async def exercise() -> float:
            with mock.patch.object(wizard, "_initialize_backtest_kline_cache", side_effect=blocked_initializer), mock.patch.object(
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

    def test_concurrent_initialization_shares_worker_after_first_timeout(self) -> None:
        calls = 0
        started = threading.Event()
        release = threading.Event()

        def blocked_initializer():
            nonlocal calls
            calls += 1
            started.set()
            release.wait(1.0)
            return "kline"

        async def exercise() -> None:
            with mock.patch.object(wizard, "_initialize_backtest_kline_cache", side_effect=blocked_initializer), mock.patch.object(
                wizard, "_BACKTEST_CACHE_INITIALIZATION_TIMEOUT_SECONDS", 0.01
            ):
                first = asyncio.create_task(wizard.initialize_backtest_caches_off_loop())
                await asyncio.to_thread(started.wait, 1.0)
                with self.assertRaises(wizard.BacktestCacheUnavailable):
                    await first

                # A timed-out caller must not cancel the worker or start a second one.
                with mock.patch.object(wizard, "_BACKTEST_CACHE_INITIALIZATION_TIMEOUT_SECONDS", 1.0):
                    second = asyncio.create_task(wizard.initialize_backtest_caches_off_loop())
                    await asyncio.sleep(0.01)
                    release.set()
                    self.assertEqual("kline", await second)
            self.assertEqual(1, calls)

        asyncio.run(exercise())

    def test_kline_initializer_never_constructs_aggtrade_cache(self) -> None:
        fake_kline = object()
        with mock.patch.object(wizard, "KlineCache", return_value=fake_kline) as kline_ctor, mock.patch.object(
            wizard, "AggTradeCache", side_effect=AssertionError("AggTradeCache must not be opened")
        ) as agg_ctor:
            wizard._BACKTEST_CACHE = None
            self.assertIs(fake_kline, wizard._initialize_backtest_kline_cache())
            kline_ctor.assert_called_once_with(wizard._BACKTEST_CACHE_PATH)
            agg_ctor.assert_not_called()

    def test_kline_initialization_succeeds_when_aggtrade_path_is_inaccessible(self) -> None:
        fake_kline = object()
        with mock.patch.object(wizard, "KlineCache", return_value=fake_kline), mock.patch.object(
            wizard, "AggTradeCache", side_effect=PermissionError("aggtrades.sqlite inaccessible")
        ):
            wizard._BACKTEST_CACHE = None
            self.assertIs(fake_kline, wizard._initialize_backtest_kline_cache())

    def test_buy_sell_and_both_replays_never_construct_aggtrade_cache(self) -> None:
        candles = [
            [1_700_000_000_000 + i * 60_000, "100", "101", "99", "100", "1", 0, "100"]
            for i in range(180)
        ]
        fake_cache = mock.Mock()
        fake_cache.path = Path("/tmp/kline-only.sqlite")
        fake_cache.stats_for.return_value = {
            "path": str(fake_cache.path),
            "bars": len(candles),
        }
        with mock.patch.object(wizard, "KlineCache", return_value=fake_cache), mock.patch.object(
            wizard, "AggTradeCache", side_effect=AssertionError("AggTradeCache must not be opened")
        ), mock.patch.object(wizard, "_fetch_klines", return_value=candles), mock.patch.object(
            wizard, "_draw_jpg", return_value="/tmp/backtest.jpg"
        ):
            wizard._BACKTEST_CACHE = None
            for ladder in ("buy", "sell", "both"):
                screen = wizard._WIZARD._run_backtest(
                    wizard.State(ladder=ladder, market="spot", symbol="BTCUSDT", percentage=0.001)
                )
                self.assertEqual("done", screen.state)
                self.assertIn("Backtest complete:", screen.text)
                self.assertIn("candle-derived OHLCV estimate", screen.text)


if __name__ == "__main__":
    unittest.main()
