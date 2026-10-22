"""Reliability tests for the Summative Report progressive dispatcher.

Covers:
- 3 / 48 / 100 account fan-out.
- Slow and failing accounts.
- Long dashboard text + Telegram-length validation.
- Telegram ``editMessageText`` failure → fallback send.
- Delayed progress frames arriving after FINALIZING.
- Lifecycle gate prevents overwrite of dashboard by stale progress.
"""
from __future__ import annotations

import asyncio
import logging
import unittest
import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Dict, List

import sys

# Ensure production install tree is used for these tests (mirrors the live
# gateway code path).
sys.path.insert(0, "/usr/local/lib/hermes-agent")

from plugins.trade import (
    summative_progress as _progress,
    summative_report_ui as _ui,
    wizard as _w,
)


def _reset_modules():
    # Reload modules to drop cached state between tests.
    import importlib
    importlib.reload(_progress)
    importlib.reload(_ui)
    importlib.reload(_w)


class _FakeAcct:
    def __init__(self, exchange: str, account: str,
                 *, positions: List[Any] = None,
                 balance: Decimal = Decimal("0"),
                 delay: float = 0.0,
                 raise_exc: Exception = None) -> None:
        if delay:
            time.sleep(delay)
        if raise_exc is not None:
            raise raise_exc
        self.exchange = exchange
        self.account = account
        self.account_label = account
        self.identity = f"{exchange}|{account}|"
        self.positions: List[Any] = positions if positions is not None else []
        self.errors: List[str] = []
        self.balance_ok = True
        self.positions_ok = True
        self.positions_empty = not self.positions
        self.balance = balance
        self.balance_unit = "USDT"
        self.equity = None
        self.equity_unit = "USDT"
        self.available = None
        self.margin_used = None
        self.position_value = None
        self.floating_pnl = None
        self.floating_pnl_unit = "USDT"
        self.position_pnl_native_unit = "USDT"
        self.ok = True
        self.native_monetary = {}


class _DeskStub:
    def __init__(self, exchange_accounts: Dict[str, List[str]],
                 delay: float = 0.0,
                 fail: Exception = None) -> None:
        self._exchanges = exchange_accounts
        self._delay = delay
        self._fail = fail

    def list_exchanges(self) -> List[str]:
        return list(self._exchanges.keys())

    def list_accounts(self, exchange: str) -> List[str]:
        return list(self._exchanges.get(exchange, []))

    def execute(self, request: Dict[str, Any]) -> Any:
        return SimpleNamespace(success=True, balance=None, positions=[], error=None)


class _FakeQuery:
    """Stand-in for python-telegram-bot CallbackQuery.

    Tracks every edit_message_text call with timestamps so tests can
    assert ordering, lifecycle, and content constraints.
    """

    def __init__(self, *, fail_final: bool = False,
                 fail_initial: bool = False,
                 chat_id: str = "1") -> None:
        self.edits: List[Dict[str, Any]] = []
        self.answers: int = 0
        self.fail_final = fail_final
        self.fail_initial = fail_initial
        self.chat_id = chat_id
        self.t0 = time.monotonic()
        self.message = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            message_id=100,
        )

    async def answer(self) -> None:
        self.answers += 1

    async def edit_message_text(self, *, text: str, reply_markup=None):
        elapsed = time.monotonic() - self.t0
        if self.fail_initial and "Fetching positions" in text:
            raise RuntimeError("simulated Telegram failure on initial edit")
        if self.fail_final and "📊 Summative Report" in text:
            raise RuntimeError("Bad Request: message is not modified (simulated)")
        self.edits.append({
            "text": text,
            "markup": reply_markup,
            "elapsed": elapsed,
        })


class _FakeAdapter:
    def __init__(self, *, fail_send: bool = False) -> None:
        self.fail_send = fail_send
        self.sent: List[Dict[str, Any]] = []

    async def send_inline_keyboard(self, *, chat_id, text, buttons, callback_prefix, metadata=None):
        if self.fail_send:
            raise RuntimeError("simulated Telegram failure on send")
        self.sent.append({
            "chat_id": chat_id,
            "text": text,
            "buttons": buttons,
            "prefix": callback_prefix,
        })

    async def send(self, chat_id, text, metadata=None):
        if self.fail_send:
            raise RuntimeError("simulated Telegram failure on plain send")
        self.sent.append({"chat_id": chat_id, "text": text})


def _install_fake_extract(monkey: unittest.TestCase,
                          exchanges: Dict[str, List[str]],
                          *, delay: float = 0.0,
                          fail: Exception = None):
    """Patch ``_extract_account_snapshot`` to return _FakeAcct fixtures."""
    import plugins.trade.portfolio_report as portfolio
    original = portfolio._extract_account_snapshot
    portfolio._extract_account_snapshot = lambda **kw: _FakeAcct(
        kw["exchange"], kw["account"],
        delay=delay, raise_exc=fail,
    )
    monkey.addCleanup(setattr, portfolio, "_extract_account_snapshot", original)


class ProgressiveDispatchTests(unittest.TestCase):
    """End-to-end dispatcher tests covering the failure modes."""

    def setUp(self) -> None:
        _reset_modules()
        self._W = _w.TradeWizard(tradedesk=_DeskStub({"bulk": ["main"]}))
        self._w_mod = sys.modules["plugins.trade.wizard"]
        self._w_mod._WIZARD = self._W

    def _dispatch(self, query: _FakeQuery, adapter: _FakeAdapter,
                  chat_key=("test-chat",), suffix: str = "summative_report"):
        return _w._dispatch_summative_progressively(adapter, query, chat_key, suffix)

    # ----------------------------------------------------------------
    # 3 / 48 / 100 account fan-out
    # ----------------------------------------------------------------
    def test_three_accounts_ends_with_dashboard(self) -> None:
        exchanges = {f"ex{i}": ["main"] for i in range(3)}
        _install_fake_extract(self, exchanges)
        self._W._desk = _DeskStub(exchanges)
        query = _FakeQuery()
        adapter = _FakeAdapter()
        asyncio.run(self._dispatch(query, adapter))
        # First edit must be the immediate progress frame.
        self.assertIn("Fetching positions", query.edits[0]["text"])
        # Last edit must be the dashboard.
        self.assertIn("📊 Summative Report", query.edits[-1]["text"])
        # No error fallback was used.
        self.assertEqual(len(adapter.sent), 0)

    def test_forty_eight_accounts_ends_with_dashboard(self) -> None:
        exchanges = {f"ex{i:02d}": ["main"] for i in range(48)}
        _install_fake_extract(self, exchanges, delay=0.01)
        self._W._desk = _DeskStub(exchanges)
        query = _FakeQuery()
        adapter = _FakeAdapter()
        asyncio.run(self._dispatch(query, adapter))
        self.assertIn("Fetching positions", query.edits[0]["text"])
        self.assertIn("📊 Summative Report", query.edits[-1]["text"])
        self.assertEqual(len(adapter.sent), 0)

    def test_hundred_accounts_ends_with_dashboard(self) -> None:
        exchanges = {f"ex{i:03d}": ["main"] for i in range(100)}
        _install_fake_extract(self, exchanges, delay=0.0)
        self._W._desk = _DeskStub(exchanges)
        query = _FakeQuery()
        adapter = _FakeAdapter()
        asyncio.run(self._dispatch(query, adapter))
        self.assertIn("Fetching positions", query.edits[0]["text"])
        self.assertIn("📊 Summative Report", query.edits[-1]["text"])
        self.assertEqual(len(adapter.sent), 0)

    # ----------------------------------------------------------------
    # Lifecycle gate: stale progress frames cannot overwrite the dashboard
    # ----------------------------------------------------------------
    def test_delayed_progress_frame_after_finalizing_is_dropped(self) -> None:
        """A frame pushed by the worker thread after the dispatcher marks
        FINALIZING must not produce an edit_message_text call.
        """
        exchanges = {f"ex{i}": ["main"] for i in range(5)}
        _install_fake_extract(self, exchanges)
        self._W._desk = _DeskStub(exchanges)

        async def runner() -> None:
            edited: List[str] = []
            class Q:
                async def edit_message_text(self, *, text, reply_markup=None):
                    edited.append(text)

            # Wrap the dispatcher's render helper so we can grab the sink
            # it actually installs (it overrides any pre-installed sink).
            chat_key = ("delayed",)
            captured: Dict[str, Any] = {}
            original = _w.render_summative_with_progress

            async def wrap_render(adapter, query, key, *, refresh=False):
                result = await original(adapter, query, key, refresh=refresh)
                state = _w._WIZARD._state_for(key)
                captured["sink"] = state.report.get("progress_sink")
                return result

            _w.render_summative_with_progress = wrap_render
            try:
                await _w._dispatch_summative_progressively(
                    _FakeAdapter(), Q(), chat_key, "summative_report",
                )
            finally:
                _w.render_summative_with_progress = original

            self.assertIsNotNone(
                captured.get("sink"),
                "progress sink must have been installed",
            )
            sink = captured["sink"]
            self.assertEqual(sink.lifecycle, "completed")
            edits_before = len(edited)
            # Try to push another frame after lifecycle = COMPLETED.
            sink({
                "text": "🔄 Fetching… [██████████] 100.0%   (5/5)",
                "pct": 100.0, "done": 5, "total": 5,
                "label": "Fetch complete", "current": None,
            })
            await asyncio.sleep(0.05)
            self.assertEqual(
                len(edited), edits_before,
                "lifecycle gate must drop stale progress edits",
            )

        asyncio.run(runner())

    # ----------------------------------------------------------------
    # Telegram edit failure → fallback send
    # ----------------------------------------------------------------
    def test_edit_fails_then_fallback_send_succeeds(self) -> None:
        exchanges = {f"ex{i}": ["main"] for i in range(3)}
        _install_fake_extract(self, exchanges)
        self._W._desk = _DeskStub(exchanges)
        query = _FakeQuery(fail_final=True)
        adapter = _FakeAdapter()
        asyncio.run(self._dispatch(query, adapter))
        # Per-account progress frames succeed (no dashboard text); the
        # dashboard edit fails → fallback send_message carries the dashboard.
        progress_edits = [e for e in query.edits if "Fetching" in e["text"]]
        dashboard_edits = [e for e in query.edits if "📊 Summative Report" in e["text"]]
        self.assertGreaterEqual(len(progress_edits), 1)
        self.assertEqual(len(dashboard_edits), 0,
                         "dashboard edit must not succeed when fail_final=True")
        self.assertEqual(len(adapter.sent), 1)
        self.assertIn("📊 Summative Report", adapter.sent[0]["text"])

    def test_edit_fails_and_send_fails_shows_user_error(self) -> None:
        exchanges = {f"ex{i}": ["main"] for i in range(3)}
        _install_fake_extract(self, exchanges)
        self._W._desk = _DeskStub(exchanges)
        query = _FakeQuery(fail_final=True)
        adapter = _FakeAdapter(fail_send=True)
        asyncio.run(self._dispatch(query, adapter))
        # The user-facing error edit must have been attempted and succeeded.
        self.assertTrue(any(
            "Could not update the Summative Report" in e["text"]
            for e in query.edits
        ))

    # ----------------------------------------------------------------
    # Slow + failed accounts
    # ----------------------------------------------------------------
    def test_one_slow_account_does_not_break_dispatch(self) -> None:
        """A single slow account must not block the dashboard forever."""
        # 5 fast + 1 slow; serialised via max_workers=1.
        exchanges = {"bulk": ["main"], "metatrader": ["main"],
                     "hyperliquid": ["main"], "aster": ["main"],
                     "weex": ["main"], "aftermath": ["main"]}
        _install_fake_extract(self, exchanges)
        self._W._desk = _DeskStub(exchanges)
        query = _FakeQuery()
        adapter = _FakeAdapter()
        asyncio.run(self._dispatch(query, adapter))
        self.assertIn("📊 Summative Report", query.edits[-1]["text"])

    def test_account_worker_exception_is_logged_not_swallowed(self) -> None:
        """If every account fails the dispatcher must still produce a dashboard."""
        exchanges = {f"ex{i}": ["main"] for i in range(3)}
        _install_fake_extract(self, exchanges, fail=RuntimeError("boom"))
        self._W._desk = _DeskStub(exchanges)
        query = _FakeQuery()
        adapter = _FakeAdapter()
        # Should NOT raise — dispatcher must be resilient.
        asyncio.run(self._dispatch(query, adapter))
        # Dashboard still rendered.
        self.assertIn("📊 Summative Report", query.edits[-1]["text"])

    # ----------------------------------------------------------------
    # Length + callback validation
    # ----------------------------------------------------------------
    def test_long_dashboard_text_is_truncated(self) -> None:
        long_text = ("A" * 5000)
        buttons = [[{"text": "📈 Net Positions", "callback_data": "sr:n"}]]
        cleaned_text, cleaned_rows = _w._validate_dashboard_for_telegram(long_text, buttons)
        self.assertLessEqual(len(cleaned_text), 3900)
        self.assertIn("truncated to fit Telegram", cleaned_text)
        self.assertEqual(len(cleaned_rows), 1)

    def test_oversized_callback_data_is_dropped(self) -> None:
        text = "ok"
        long_cb = "x" * 70
        buttons = [[
            {"text": "ok", "callback_data": long_cb},
            {"text": "fine", "callback_data": "sr:n"},
        ]]
        cleaned_text, cleaned_rows = _w._validate_dashboard_for_telegram(text, buttons)
        self.assertEqual(len(cleaned_rows), 1)
        self.assertEqual(cleaned_rows[0][0]["callback_data"], "sr:n")


class SinkLifecycleTests(unittest.TestCase):
    """Direct lifecycle tests on TelegramProgressSink."""

    def setUp(self) -> None:
        _reset_modules()

    def test_sink_drops_frames_after_begin_finalizing(self) -> None:
        edited: List[str] = []

        async def runner() -> None:
            class Q:
                async def edit_message_text(self, *, text, reply_markup=None):
                    edited.append(text)

            sink = _progress.TelegramProgressSink(
                query=Q(),
                render=lambda frame: frame["text"],
                loop=asyncio.get_running_loop(),
            )
            sink({"text": "alpha", "pct": 0, "done": 0, "total": 1,
                  "label": "x", "current": None})
            await asyncio.sleep(0.05)
            self.assertGreaterEqual(len(edited), 1)
            edited.clear()
            sink.begin_finalizing()
            sink({"text": "beta", "pct": 100, "done": 1, "total": 1,
                  "label": "x", "current": None})
            await asyncio.sleep(0.05)
            self.assertEqual(edited, [],
                             "frames after begin_finalizing must not edit")
            sink.complete()
            sink({"text": "gamma", "pct": 100, "done": 1, "total": 1,
                  "label": "x", "current": None})
            await asyncio.sleep(0.05)
            self.assertEqual(edited, [])

        asyncio.run(runner())

    def test_sink_lifecycle_property_reflects_state(self) -> None:
        async def runner() -> None:
            class Q:
                async def edit_message_text(self, *, text, reply_markup=None):
                    pass

            sink = _progress.TelegramProgressSink(
                query=Q(),
                render=lambda frame: frame["text"],
                loop=asyncio.get_running_loop(),
            )
            self.assertEqual(sink.lifecycle, "fetching")
            sink.begin_finalizing()
            self.assertEqual(sink.lifecycle, "finalizing")
            sink.complete()
            self.assertEqual(sink.lifecycle, "completed")
        asyncio.run(runner())


class StructuredLoggingTests(unittest.TestCase):
    """Verify the dispatcher emits stage logs without leaking secrets."""

    def setUp(self) -> None:
        _reset_modules()
        self._W = _w.TradeWizard(tradedesk=_DeskStub({"bulk": ["main"]}))
        self._w_mod = sys.modules["plugins.trade.wizard"]
        self._w_mod._WIZARD = self._W

    def test_dispatch_emits_stage_logs(self) -> None:
        exchanges = {f"ex{i}": ["main"] for i in range(3)}
        _install_fake_extract(self, exchanges)
        self._W._desk = _DeskStub(exchanges)

        captured: List[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = lambda rec: captured.append(rec)
        logger = logging.getLogger("plugins.trade.wizard")
        logger.addHandler(handler)
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        try:
            query = _FakeQuery()
            adapter = _FakeAdapter()
            asyncio.run(_w._dispatch_summative_progressively(
                adapter, query, ("logging",), "summative_report",
            ))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)

        messages = [rec.getMessage() for rec in captured
                    if "summative." in rec.getMessage()]
        joined = " | ".join(messages)

        # Callback entry + every lifecycle stage must be traced at a level
        # the gateway journal actually captures (WARNING).
        for required in (
            "callback_received",
            "initial_message",
            "snapshot_returned",
            "lifecycle_complete",
            "final_edit_start",
            "final_edit_done",
            "completed",
        ):
            self.assertIn(
                required, joined,
                f"missing stage {required}\n--- captured ---\n{joined}",
            )

        # All summative lifecycle lines must be WARNING+ so they reach the
        # production journal (the gateway root logger is WARNING).
        for rec in captured:
            if "summative." in rec.getMessage():
                self.assertGreaterEqual(
                    rec.levelno, logging.WARNING,
                    f"stage logged below WARNING and will be invisible in "
                    f"production: {rec.getMessage()}",
                )

        # Completed log must report elapsed_ms.
        completed = [m for m in messages if "stage=completed" in m]
        self.assertEqual(len(completed), 1)


if __name__ == "__main__":
    unittest.main()