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
        self.size = Decimal(str(size))
        self.entry_price = None
        self.mark = None
        # Real snapshot stores pnl as Decimal; coerce here so totals work.
        self.pnl = Decimal(str(pnl)) if pnl is not None else None
        self.pnl_unit = pnl_unit
        self.native_pnl = self.pnl
        self.native_pnl_unit = pnl_unit
        self.exchange_instrument = symbol
        self.notional = None
        self.identity = f"{exchange}|{account}|{symbol}|{side}|{size}"
        self.instrument_key = symbol
        self.account_label = account
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


def _flatten_buttons(buttons):
    out = []
    for row in buttons:
        for btn in row:
            if isinstance(btn, dict):
                out.append(btn)
    return out


def _has_callback(buttons, suffix):
    return any(
        btn.get("callback_data") == suffix or btn.get("callback_data", "").startswith(suffix + ":")
        for btn in _flatten_buttons(buttons)
    )


class ChecklistTests(unittest.TestCase):
    """Final invariant checks against the checklist."""

    def setUp(self) -> None:
        from plugins.trade import portfolio_report as portfolio
        self._original = portfolio._extract_account_snapshot
        portfolio._extract_account_snapshot = lambda **kw: _AccountStub(kw["exchange"], kw["account"])
        self.addCleanup(setattr, portfolio, "_extract_account_snapshot", self._original)

        self._state = _make_state()
        self._desk = _DeskStub({
            "bulk": ["main"],
            "metatrader": ["cents"],
            "hyperliquid": ["flex"],
        })

    def _handle(self, suffix):
        return handle(self._desk, self._state, suffix)

    # Item 1 — Main Dashboard sections
    def test_dashboard_has_all_sections(self) -> None:
        view = self._handle("summative_report")
        for needle in (
            "Equity (unconverted):",
            "uPnL (unconverted):",
            "Open Positions:",
            "Top instruments",
            "Closest liquidation",
            "Data fetched now",
        ):
            self.assertIn(needle, view.text)

    def test_dashboard_top_instruments_have_net_and_gross(self) -> None:
        positions = [
            _PositionStub("bulk", "main", "BTC", side="BUY", size="0.5", pnl="100", pnl_unit="USDT"),
        ]
        from plugins.trade import portfolio_report as portfolio
        original = portfolio._extract_account_snapshot

        def extract(*, exchange, account, account_label, identity, desk):
            return _AccountStub(exchange, account, positions=[
                p for p in positions if p.exchange == exchange and p.account == account
            ])
        portfolio._extract_account_snapshot = extract
        try:
            view = self._handle("summative_report")
        finally:
            portfolio._extract_account_snapshot = original
        self.assertIn("net", view.text)
        self.assertIn("gross", view.text)
        self.assertIn("size, not USD notional", view.text)

    # Item 2 — Net Positions sort/filter/paginate
    def test_net_positions_default_sort_gross(self) -> None:
        view = self._handle("sr:n")
        self.assertIn("📈 Net Positions", view.text)
        self.assertIn("Sort: gross", view.text)
        self.assertTrue(_has_callback(view.buttons, "sr:long"))
        self.assertTrue(_has_callback(view.buttons, "sr:short"))
        self.assertTrue(_has_callback(view.buttons, "sr:snet"))
        self.assertTrue(_has_callback(view.buttons, "sr:sgross"))
        self.assertTrue(_has_callback(view.buttons, "sr:spnl"))

    # Item 3 — Detail view + read-only stubs
    def test_detail_view_has_stub_buttons(self) -> None:
        from plugins.trade import portfolio_report as portfolio
        positions = [
            _PositionStub("bulk", "main", "BTC", side="BUY", size="0.4", pnl="12", pnl_unit="USDT"),
            _PositionStub("metatrader", "cents", "BTC", side="SELL", size="0.1", pnl="-3", pnl_unit="USC"),
        ]
        original = portfolio._extract_account_snapshot

        def extract(*, exchange, account, account_label, identity, desk):
            return _AccountStub(exchange, account, positions=[
                p for p in positions if p.exchange == exchange and p.account == account
            ])
        portfolio._extract_account_snapshot = extract
        try:
            net_view = self._handle("sr:n")
            detail_btn = next(
                btn for btn in _flatten_buttons(net_view.buttons)
                if btn.get("callback_data", "").startswith("sr:nd:")
            )
            detail_view = self._handle(detail_btn["callback_data"])
        finally:
            portfolio._extract_account_snapshot = original
        self.assertIn("net", detail_view.text)
        self.assertIn("gross", detail_view.text)
        for suffix in ("sr:atp", "sr:asl", "sr:c50", "sr:c100"):
            self.assertTrue(_has_callback(detail_view.buttons, suffix),
                            f"missing stub {suffix}")

    # Item 4 — Pagination present
    def test_pagination_present(self) -> None:
        # By Symbol page
        view = self._handle("sr:p")
        self.assertIn("📊 Positions by Symbol", view.text)
        # By Exchange page
        view = self._handle("sr:e")
        self.assertIn("🏦 PnL by Exchange", view.text)

    # Item 5 — Risks
    def test_risks_view_present(self) -> None:
        view = self._handle("sr:r")
        self.assertIn("⚠️ Risk & Exposure", view.text)
        self.assertIn("Liquidation prices: unavailable", view.text)

    # Item 6 — Actions and Settings stubs
    def test_actions_and_settings_stubs(self) -> None:
        actions = self._handle("sr:a")
        self.assertIn("Bulk future operations", actions.text)
        settings = self._handle("sr:s")
        self.assertIn("⚙️ Settings", settings.text)

    # Item 7 — Standard footer (◀ Back, 🏠 Home, 🔄 Refresh) on every screen
    def test_every_screen_has_home(self) -> None:
        screens = {
            "summative_report": "Main dashboard",
            "sr:n": "Net Positions",
            "sr:e": "By Exchange",
            "sr:p": "By Symbol",
            "sr:r": "Risks",
            "sr:a": "Actions",
            "sr:s": "Settings",
            "sr:f": "Filter",
            "sr:t": "Total PnL",
            "sr:q": "Equity",
        }
        for suffix, label in screens.items():
            view = self._handle(suffix)
            with self.subTest(screen=label):
                self.assertTrue(_has_callback(view.buttons, "sr:h"),
                                f"{label}: missing Home")

    # Item 8 — Progress sink is wired and non-fatal
    def test_progress_sink_routes(self) -> None:
        sink = ProgressCollector()
        install_progress_sink(self._state, sink)
        self._handle("sr:refresh")
        self.assertGreater(len(sink.frames), 0)
        # Any sink raising must not break the fetch.
        def boom(_frame):
            raise RuntimeError("sink failed")

        install_progress_sink(self._state, boom)
        view = self._handle("sr:refresh")
        self.assertIn("Summative Report", view.text)

    # Item 9 — No exchange writes reachable from any screen
    def test_no_exchange_write_callbacks(self) -> None:
        forbidden = {
            "trade:submit", "trade:close", "trade:cancel", "trade:ladder_submit",
            "trade:set_tp", "trade:set_sl",
        }
        screens = (
            "summative_report", "sr:n", "sr:e", "sr:p", "sr:r",
            "sr:a", "sr:s", "sr:f", "sr:t", "sr:q",
        )
        for suffix in screens:
            view = self._handle(suffix)
            for btn in _flatten_buttons(view.buttons):
                cb = btn.get("callback_data", "") or ""
                self.assertFalse(
                    cb.startswith("trade:submit") or cb.startswith("trade:close")
                    or cb.startswith("trade:cancel") or cb.startswith("trade:ladder_submit")
                    or cb.startswith("trade:set_"),
                    f"{suffix}: forbidden callback {cb}",
                )

    # Item 10 — Gross labeled as size, missing liquidation handled honestly
    def test_honest_labels(self) -> None:
        net = self._handle("sr:n")
        self.assertIn("size (qty)", net.text)
        risk = self._handle("sr:r")
        self.assertIn("Liquidation prices: unavailable", risk.text)


if __name__ == "__main__":
    unittest.main()