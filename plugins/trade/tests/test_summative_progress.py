"""End-to-end tests for the Summative Report progress adapter."""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any, Dict, List

from decimal import Decimal
from plugins.trade.portfolio_report import collect_portfolio_snapshot
from plugins.trade.summative_progress import (
    ConsoleProgressSink,
    TelegramProgressSink,
    await_progress,
)
from plugins.trade.summative_report_ui import (
    ProgressCollector,
    handle,
    install_progress_sink,
    last_progress,
    progress_bar,
    progress_text,
    push_progress,
)


class _AccountStub:
    def __init__(self, exchange: str, account: str, positions: List[Any] = None,
                 balance: str = "0", unit: str = "USDT") -> None:
        self.exchange = exchange
        self.account = account
        self.account_label = account
        self.identity = f"{exchange}|{account}|"
        self.positions: List[Any] = positions if positions is not None else []
        self.errors: List[str] = []
        self.balance_ok = True
        self.positions_ok = bool(positions is not None)
        self.positions_empty = not self.positions
        self.balance = Decimal(balance) if isinstance(balance, str) else balance
        self.balance_unit = unit
        self.equity = None
        self.equity_unit = unit
        self.available = None
        self.margin_used = None
        self.position_value = None
        self.floating_pnl = None
        self.floating_pnl_unit = unit
        self.position_pnl_native_unit = unit
        self.ok = True
        self.native_monetary = {}


class _PositionStub:
    def __init__(self, exchange: str, account: str, symbol: str,
                 side: str = "BUY", size: Any = "0.1",
                 pnl: Any = None, pnl_unit: str = "USDT") -> None:
        self.exchange = exchange
        self.account = account
        self.symbol = symbol
        self.side = side
        self.size = size
        self.entry_price = None
        self.mark = None
        self.pnl = pnl
        self.pnl_unit = pnl_unit
        self.native_pnl = pnl
        self.native_pnl_unit = pnl_unit
        self.exchange_instrument = symbol
        self.notional = None
        self.identity = f"{exchange}|{account}|{symbol}|{side}|{size}"
        self.instrument_key = symbol
        # Used by the dashboard formatter.
        self.to_dict = lambda: {
            "symbol": symbol, "side": side, "size": size,
            "pnl": pnl, "pnl_unit": pnl_unit,
        }


class _DeskStub:
    def __init__(self, exchanges: Dict[str, List[str]]) -> None:
        self._exchanges = exchanges

    def list_exchanges(self) -> List[str]:
        return list(self._exchanges.keys())

    def list_accounts(self, exchange: str) -> List[str]:
        return list(self._exchanges.get(exchange, []))

    def execute(self, request: Dict[str, Any]) -> Any:
        return SimpleNamespace(success=True, balance=None, positions=[], error=None)


def _make_state() -> Any:
    return SimpleNamespace(report={})


class ProgressHelpersTests(unittest.TestCase):
    def test_progress_bar_clamps_and_fills(self) -> None:
        self.assertEqual(progress_bar(0), "[░░░░░░░░░░]")
        self.assertEqual(progress_bar(50), "[█████░░░░░]")
        self.assertEqual(progress_bar(100), "[██████████]")
        self.assertEqual(progress_bar(-5), "[░░░░░░░░░░]")
        self.assertEqual(progress_bar(150), "[██████████]")

    def test_progress_text_includes_current(self) -> None:
        text = progress_text("Fetching positions", 40.0, done=2, total=5,
                             current="Hyperliquid/main")
        self.assertIn("Fetching positions", text)
        self.assertIn("40.0%", text)
        self.assertIn("2/5", text)
        self.assertIn("Hyperliquid/main", text)

    def test_push_progress_records_and_routes(self) -> None:
        state = _make_state()
        sink = ProgressCollector()
        install_progress_sink(state, sink)
        push_progress(state, label="Fetch", done=1, total=4, current="A/a")
        push_progress(state, label="Fetch", done=3, total=4, current="A/b")
        self.assertEqual(len(sink.frames), 2)
        self.assertEqual(sink.frames[-1]["pct"], 75.0)
        self.assertEqual(last_progress(state)["current"], "A/b")


class PortfolioProgressTests(unittest.TestCase):
    def _desk(self) -> _DeskStub:
        positions = [
            _PositionStub("bulk", "main", "BTC", side="BUY", size="0.1", pnl="12", pnl_unit="USDT"),
            _PositionStub("metatrader", "cents", "BTC", side="SELL", size="0.05", pnl="-3", pnl_unit="USC"),
            _PositionStub("hyperliquid", "flex", "SOL", side="BUY", size="2", pnl="40", pnl_unit="USDC"),
        ]
        return _DeskStub({
            "bulk": ["main"],
            "metatrader": ["cents"],
            "hyperliquid": ["flex"],
        })

    def _build_account_snapshot(self, exchange: str, account: str) -> _AccountStub:
        positions = [
            pos for pos in [
                _PositionStub("bulk", "main", "BTC", side="BUY", size="0.1"),
                _PositionStub("metatrader", "cents", "BTC", side="SELL", size="0.05"),
                _PositionStub("hyperliquid", "flex", "SOL", side="BUY", size="2"),
            ] if pos.exchange == exchange and pos.account == account
        ]
        return _AccountStub(exchange, account, positions=positions)

    def test_collect_portfolio_snapshot_emits_progress(self) -> None:
        desk = self._desk()
        frames: List[Dict[str, Any]] = []

        def progress(done: int, total: int, current: Any) -> None:
            frames.append({"done": done, "total": total, "current": current})

        # Patch _extract_account_snapshot so we don't need real exchange data.
        from plugins.trade import portfolio_report as portfolio
        original = portfolio._extract_account_snapshot

        def fake_extract(*, exchange, account, account_label, identity, desk):
            return self._build_account_snapshot(exchange, account)

        portfolio._extract_account_snapshot = fake_extract
        try:
            snap = collect_portfolio_snapshot(desk, progress=progress)
        finally:
            portfolio._extract_account_snapshot = original

        self.assertGreater(len(frames), 0)
        # First frame: (0, total, None); last: (total, total, last account).
        self.assertEqual(frames[0]["done"], 0)
        self.assertEqual(frames[-1]["done"], frames[-1]["total"])
        self.assertTrue(any(frame["current"] for frame in frames))

    def test_collect_portfolio_snapshot_without_progress(self) -> None:
        desk = self._desk()
        from plugins.trade import portfolio_report as portfolio
        original = portfolio._extract_account_snapshot
        portfolio._extract_account_snapshot = lambda **kw: _AccountStub(kw["exchange"], kw["account"])
        try:
            snap = collect_portfolio_snapshot(desk)
        finally:
            portfolio._extract_account_snapshot = original
        self.assertEqual(snap.accounts_seen, 3)


class HandleFlowTests(unittest.TestCase):
    def test_menu_emits_default_progress_frames(self) -> None:
        state = _make_state()
        from plugins.trade import portfolio_report as portfolio
        original = portfolio._extract_account_snapshot
        portfolio._extract_account_snapshot = lambda **kw: _AccountStub(kw["exchange"], kw["account"])
        try:
            view = handle(_DeskStub({}), state, "summative_report")
        finally:
            portfolio._extract_account_snapshot = original
        self.assertIn("Summative Report", view.text)
        # The wizard install a default collector; frames should be present.
        history = state.report.get("progress_history") or []
        self.assertGreaterEqual(len(history), 1)
        last = history[-1]
        self.assertEqual(last["label"], "Fetch complete")
        self.assertAlmostEqual(last["pct"], 100.0)

    def test_installed_sink_receives_frames(self) -> None:
        state = _make_state()
        sink = ProgressCollector()
        install_progress_sink(state, sink)
        from plugins.trade import portfolio_report as portfolio
        original = portfolio._extract_account_snapshot
        portfolio._extract_account_snapshot = lambda **kw: _AccountStub(kw["exchange"], kw["account"])
        try:
            handle(_DeskStub({"bulk": ["main"]}), state, "sr:refresh")
        finally:
            portfolio._extract_account_snapshot = original
        self.assertGreater(len(sink.frames), 0)
        self.assertTrue(any("[" in frame["text"] and "]" in frame["text"] for frame in sink.frames))


class ConsoleSinkTests(unittest.TestCase):
    def test_console_sink_records_frames(self) -> None:
        sink = ConsoleProgressSink()
        state = _make_state()
        install_progress_sink(state, sink)
        push_progress(state, label="Fetch", done=1, total=2)
        push_progress(state, label="Fetch", done=2, total=2)
        self.assertEqual(len(sink.frames), 2)


class TelegramSinkTests(unittest.TestCase):
    def test_sink_dedupes_identical_text(self) -> None:
        edits: List[str] = []

        class _Query:
            async def edit_message_text(self, *, text: str, reply_markup: Any) -> None:
                edits.append(text)

        # Run on a loop so call_soon_threadsafe dispatches.
        async def runner() -> None:
            sink = TelegramProgressSink(
                query=_Query(),
                render=lambda frame: frame["text"],
            )
            loop = asyncio.get_event_loop()
            sink._loop = loop  # already equal via get_event_loop, but explicit.
            frame = {"text": "same", "pct": 10, "done": 1, "total": 10,
                     "current": "A", "label": "x"}
            sink(frame)
            sink(frame)
            # Yield to let the scheduled coroutine run.
            await asyncio.sleep(0.05)

        asyncio.run(runner())
        # Two identical texts should produce at most one edit.
        self.assertLessEqual(len(edits), 1)
        if edits:
            self.assertEqual(edits[0], "same")


class AwaitProgressTests(unittest.TestCase):
    def test_predicate_resolves_before_timeout(self) -> None:
        seen = {"ready": False}

        def predicate() -> bool:
            return seen["ready"]

        seen["ready"] = True
        self.assertTrue(await_progress(predicate, timeout=0.2, poll_interval=0.01))


if __name__ == "__main__":
    unittest.main()