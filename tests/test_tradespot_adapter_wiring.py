"""Regression tests for tradespot adapter wiring upgrade scenario.

Uses the REAL production path:
apply_adapter_wiring -> specs_for_capabilities -> trade_adapter_specs
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import logging
import py_compile
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "installer"))

from adapter_wiring import apply_adapter_wiring
from patchspecs import trade_adapter_specs

PRE_PATCH_ADAPTER = Path(
    "/usr/local/lib/hermes-agent/plugins/platforms/telegram/"
    "adapter.py.pre-tradespot-activation.1790766300"
)
EXPECTED_BEFORE_SHA = "3e3876945d6fc2e4dd4192a7344e4de86a052953ca4803577a63901d2f392f5a"


def _build_hermes_tree(tmpdir: Path) -> Path:
    hermes_root = tmpdir / "hermes"
    (hermes_root / "plugins" / "platforms" / "telegram").mkdir(parents=True)
    (hermes_root / "config.yaml").write_text("plugins:\n  enabled:\n    - trade\n")
    return hermes_root


def _count_cmd_body_eq(source: str, value: str) -> int:
    tree = ast.parse(source)
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            left = node.test.left
            if isinstance(left, ast.Name) and left.id == "cmd_body":
                for comp in node.test.comparators:
                    if isinstance(comp, ast.Constant) and comp.value == value:
                        count += 1
    return count


def _handle_command_node(source: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef) and item.name == "_handle_command":
                    return item
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_command":
            return node
    raise AssertionError("_handle_command not found")


def _cmd_body_assigned_before_tradespot_read(source: str) -> None:
    method = _handle_command_node(source)
    assign_line = None
    read_line = None
    for node in ast.walk(method):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "cmd_body":
                    assign_line = node.lineno
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name):
            if node.left.id == "cmd_body":
                for comp in node.comparators:
                    if isinstance(comp, ast.Constant) and comp.value == "tradespot":
                        read_line = node.lineno
    if assign_line is None:
        raise AssertionError("cmd_body is never assigned in _handle_command")
    if read_line is None:
        raise AssertionError("no cmd_body == 'tradespot' comparison")
    if not assign_line < read_line:
        raise AssertionError(
            f"cmd_body assigned at {assign_line} but tradespot reads it at {read_line}"
        )


def _tradespot_nested_under_first_token(source: str) -> bool:
    method = _handle_command_node(source)
    for node in ast.walk(method):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if isinstance(test, ast.Name) and test.id == "first_token":
            for child in ast.walk(node):
                if isinstance(child, ast.If) and isinstance(child.test, ast.Compare):
                    left = child.test.left
                    if isinstance(left, ast.Name) and left.id == "cmd_body":
                        for comp in child.test.comparators:
                            if isinstance(comp, ast.Constant) and comp.value == "tradespot":
                                return True
    return False


def _count_data_startswith(source: str, prefix: str) -> int:
    tree = ast.parse(source)
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "startswith" and node.args:
                arg0 = node.args[0]
                if isinstance(arg0, ast.Constant) and arg0.value == prefix:
                    count += 1
    return count


def _apply_public(hermes_root: Path) -> dict:
    return apply_adapter_wiring(
        hermes_root=hermes_root,
        hermes_home=Path("/root/.hermes"),
        capabilities=["trade"],
        dry_run=False,
    )


class TestTradeshopAdapterUpgrade(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tradespot_adapter_")
        self.hermes_root = _build_hermes_tree(Path(self.tmp.name))
        self.fixture = (
            self.hermes_root / "plugins" / "platforms" / "telegram" / "adapter.py"
        )
        self.fixture.write_bytes(PRE_PATCH_ADAPTER.read_bytes())
        self.before_sha = hashlib.sha256(self.fixture.read_bytes()).hexdigest()
        if self.before_sha != EXPECTED_BEFORE_SHA:
            self.fail(f"Fixture SHA mismatch: {self.before_sha}")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_upgrade_from_trade_only_adds_tradespot_seams_exactly_once(self) -> None:
        result = _apply_public(self.hermes_root)
        self.assertTrue(result.get("ok"), result)
        text = self.fixture.read_text(errors="replace")
        ast.parse(text)
        py_compile.compile(str(self.fixture), doraise=True)

        self.assertEqual(_count_cmd_body_eq(text, "trade"), 1)
        self.assertEqual(_count_cmd_body_eq(text, "tradespot"), 1)
        self.assertEqual(_count_cmd_body_eq(text, "backtest"), 1)
        self.assertTrue(_tradespot_nested_under_first_token(text))
        _cmd_body_assigned_before_tradespot_read(text)

        self.assertEqual(_count_data_startswith(text, "trade:"), 1)
        self.assertEqual(_count_data_startswith(text, "tradespot:"), 1)
        self.assertEqual(
            text.count("from plugins.trade.wizard import handle_trade_text"), 1
        )
        self.assertEqual(
            text.count("from plugins.trade.tradespot_wizard import handle_tradespot_text"),
            1,
        )

        for marker in (
            "BEGIN KAM TRADE PLUGIN (tradespot slash command dispatch)",
            "BEGIN KAM TRADE PLUGIN (tradespot callback dispatch)",
            "BEGIN KAM TRADE PLUGIN (tradespot text interception)",
        ):
            self.assertEqual(text.count(marker), 1, marker)

        cmd_src = ast.get_source_segment(text, _handle_command_node(text)) or ""
        stray = [i + 1 for i, line in enumerate(cmd_src.splitlines()) if line.strip() == "\\"]
        self.assertEqual(stray, [], f"stray backslash in _handle_command: {stray}")

        self.assertEqual(
            text.count(
                "from plugins.trade.tradespot_wizard import handle_tradespot_command"
            ),
            1,
        )
        self.assertEqual(text.count("await _tradespot_cmd(self, msg)"), 1)
        self.assertNotIn("await handle_tradespot_command(self, msg)", text)

        self.assertEqual(
            text.count(
                "from plugins.trade.tradespot_wizard import handle_tradespot_callback as _tradespot_cb"
            ),
            1,
        )
        self.assertEqual(text.count("await _tradespot_cb(self, query, data)"), 1)
        self.assertNotIn("await handle_tradespot_callback(self, query, data)", text)

        self.assertEqual(
            text.count(
                "from plugins.trade.tradespot_wizard import handle_tradespot_text as _tradespot_tx"
            ),
            1,
        )
        self.assertEqual(text.count("if await _tradespot_tx(self, msg):"), 1)
        self.assertNotIn("if await handle_tradespot_text(self, msg):", text)

        for marker in (
            "BEGIN KAM TRADE PLUGIN (tradespot slash command dispatch)",
            "BEGIN KAM TRADE PLUGIN (tradespot callback dispatch)",
            "BEGIN KAM TRADE PLUGIN (tradespot text interception)",
        ):
            begin = text.find("# " + marker) if False else text.find(marker)
            end = text.find("END KAM TRADE PLUGIN", begin)
            block = text[begin:end]
            stray_block = [ln for ln in block.splitlines() if ln.strip() == "\\"]
            self.assertEqual(stray_block, [], marker)

    def test_tradespot_command_never_reads_cmd_body_before_assignment(self) -> None:
        _apply_public(self.hermes_root)
        text = self.fixture.read_text(errors="replace")
        _cmd_body_assigned_before_tradespot_read(text)
        self.assertTrue(_tradespot_nested_under_first_token(text))

    def test_second_application_is_idempotent(self) -> None:
        _apply_public(self.hermes_root)
        after1 = hashlib.sha256(self.fixture.read_bytes()).hexdigest()
        _apply_public(self.hermes_root)
        after2 = hashlib.sha256(self.fixture.read_bytes()).hexdigest()
        self.assertEqual(after1, after2, "second apply changed the file")
        text = self.fixture.read_text(errors="replace")
        self.assertEqual(_count_cmd_body_eq(text, "tradespot"), 1)
        self.assertEqual(_count_cmd_body_eq(text, "trade"), 1)


class TestTradeAdapterSpecsUniqueness(unittest.TestCase):
    def test_trade_adapter_specs_returns_no_duplicates(self) -> None:
        specs = trade_adapter_specs()
        names = [s.seam for s in specs]
        sentinels = [s.native_sentinel for s in specs]
        blocks = [s.block for s in specs]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(sentinels), len(set(sentinels)))
        self.assertEqual(len(blocks), len(set(blocks)))
        self.assertEqual(sum(1 for n in names if "tradespot" in n), 3)
        for s in specs:
            if "tradespot" in s.seam:
                token = s.native_sentinel.split()[-1]
                self.assertEqual(s.block.count(token), 1, s.seam)



    def test_callback_and_text_aliases_match(self) -> None:
        specs = {s.seam: s for s in trade_adapter_specs()}
        cb = specs["tradespot callback dispatch"].block
        self.assertIn("handle_tradespot_callback as _tradespot_cb", cb)
        self.assertIn("await _tradespot_cb(self, query, data)", cb)
        self.assertNotIn("await handle_tradespot_callback(", cb)
        tx = specs["tradespot text interception"].block
        self.assertIn("handle_tradespot_text as _tradespot_tx", tx)
        self.assertIn("await _tradespot_tx(self, msg)", tx)
        self.assertNotIn("await handle_tradespot_text(", tx)
        for s in specs.values():
            if "tradespot" in s.seam:
                self.assertFalse(
                    any(ln.strip() == "\\" for ln in s.block.splitlines()),
                    s.seam,
                )


class TestSyntheticCommandDispatch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tradespot_synth_")
        self.hermes_root = _build_hermes_tree(Path(self.tmp.name))
        self.fixture = (
            self.hermes_root / "plugins" / "platforms" / "telegram" / "adapter.py"
        )
        self.fixture.write_bytes(PRE_PATCH_ADAPTER.read_bytes())
        result = _apply_public(self.hermes_root)
        self.assertTrue(result.get("ok"), result)
        self.adapter_text = self.fixture.read_text(errors="replace")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_handle_command(self, text: str):
        sys.path.insert(0, "/usr/local/lib/hermes-agent")
        method = _handle_command_node(self.adapter_text)
        src = ast.get_source_segment(self.adapter_text, method)
        self.assertIsNotNone(src)

        calls = {"tradespot": 0, "trade": 0, "backtest": 0}

        async def fake_tradespot(adapter, msg):
            calls["tradespot"] += 1
            return True

        async def fake_trade(adapter, msg):
            calls["trade"] += 1
            return True

        async def fake_backtest(adapter, msg):
            calls["backtest"] += 1
            return True

        import plugins.trade.backtest_wizard as bw
        import plugins.trade.tradespot_wizard as tw
        import plugins.trade.wizard as w

        orig_ts, orig_tr, orig_bt = (
            tw.handle_tradespot_command,
            w.handle_trade_command,
            bw.handle_backtest_command,
        )
        tw.handle_tradespot_command = fake_tradespot  # type: ignore[assignment]
        w.handle_trade_command = fake_trade  # type: ignore[assignment]
        bw.handle_backtest_command = fake_backtest  # type: ignore[assignment]

        ns: dict = {
            "Update": object,
            "ContextTypes": SimpleNamespace(DEFAULT_TYPE=object),
            "MessageType": SimpleNamespace(COMMAND="COMMAND"),
            "logger": logging.getLogger("synth"),
        }
        exec("import asyncio\n" + src, ns)  # noqa: S102
        handle = ns["_handle_command"]

        class FakeAdapter:
            name = "telegram"
            _SPLIT_THRESHOLD = 10**9

            def _effective_update_message(self, update):
                return update.message

            def _should_process_message(self, msg, is_command=False):
                return True

            def _is_user_authorized_from_message(self, msg):
                return True

            def _log_blocked_user(self, msg):
                return None

            async def _ensure_forum_commands(self, msg):
                return None

            async def _build_triggered_event(self, msg, update, message_type):
                return SimpleNamespace(text=getattr(msg, "text", ""))

            async def handle_message(self, event):
                return None

            def _enqueue_text_event(self, event):
                return None

        FakeAdapter._handle_command = handle  # type: ignore[method-assign]
        adapter = FakeAdapter()
        msg = SimpleNamespace(text=text)
        update = SimpleNamespace(message=msg, update_id=1)
        try:
            asyncio.run(adapter._handle_command(update, None))
        finally:
            tw.handle_tradespot_command = orig_ts  # type: ignore[assignment]
            w.handle_trade_command = orig_tr  # type: ignore[assignment]
            bw.handle_backtest_command = orig_bt  # type: ignore[assignment]
        return calls

    def test_tradespot_dispatch(self) -> None:
        calls = self._run_handle_command("/tradespot")
        self.assertEqual(calls["tradespot"], 1)
        self.assertEqual(calls["trade"], 0)

    def test_tradespot_at_bot_dispatch(self) -> None:
        calls = self._run_handle_command("/tradespot@SomeBot")
        self.assertEqual(calls["tradespot"], 1)
        self.assertEqual(calls["trade"], 0)

    def test_trade_regression(self) -> None:
        calls = self._run_handle_command("/trade")
        self.assertEqual(calls["trade"], 1)
        self.assertEqual(calls["tradespot"], 0)

    def test_backtest_regression(self) -> None:
        calls = self._run_handle_command("/backtest")
        self.assertEqual(calls["backtest"], 1)
        self.assertEqual(calls["tradespot"], 0)



def _handle_callback_query_node(source: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef) and item.name == "_handle_callback_query":
                    return item
    raise AssertionError("_handle_callback_query not found")


def _handle_text_node(source: str) -> ast.AST:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef) and item.name in ("_handle_text", "_handle_text_message"):
                    return item
    raise AssertionError("_handle_text not found")


class TestSyntheticCallbackDispatch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="tradespot_cb_")
        self.hermes_root = _build_hermes_tree(Path(self.tmp.name))
        self.fixture = (
            self.hermes_root / "plugins" / "platforms" / "telegram" / "adapter.py"
        )
        self.fixture.write_bytes(PRE_PATCH_ADAPTER.read_bytes())
        result = _apply_public(self.hermes_root)
        self.assertTrue(result.get("ok"), result)
        self.adapter_text = self.fixture.read_text(errors="replace")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_callback(self, data: str):
        sys.path.insert(0, "/usr/local/lib/hermes-agent")
        method = _handle_callback_query_node(self.adapter_text)
        src = ast.get_source_segment(self.adapter_text, method)
        self.assertIsNotNone(src)
        calls = {"tradespot": 0, "trade": 0, "backtest": 0}
        async def fake_ts(adapter, query, data):
            calls["tradespot"] += 1
            return None
        async def fake_tr(adapter, query, data):
            calls["trade"] += 1
        async def fake_bt(adapter, query, data):
            calls["backtest"] += 1
        import plugins.trade.backtest_wizard as bw
        import plugins.trade.tradespot_wizard as tw
        import plugins.trade.wizard as w
        orig = (tw.handle_tradespot_callback, w.handle_trade_callback, bw.handle_backtest_callback)
        tw.handle_tradespot_callback = fake_ts  # type: ignore[assignment]
        w.handle_trade_callback = fake_tr  # type: ignore[assignment]
        bw.handle_backtest_callback = fake_bt  # type: ignore[assignment]
        ns = {"Update": object, "ContextTypes": SimpleNamespace(DEFAULT_TYPE=object), "logger": logging.getLogger("synth")}
        exec("from typing import Any, Dict\n" + src, ns)  # noqa: S102
        handle = ns["_handle_callback_query"]
        class FakeAdapter:
            name = "telegram"
            def _callback_ctx(self, query):
                return {}
        FakeAdapter._handle_callback_query = handle  # type: ignore[method-assign]
        adapter = FakeAdapter()
        query = SimpleNamespace(data=data, message=SimpleNamespace(chat_id=1))
        async def _answer():
            return None
        query.answer = _answer
        update = SimpleNamespace(callback_query=query)
        try:
            asyncio.run(adapter._handle_callback_query(update, None))
        finally:
            tw.handle_tradespot_callback, w.handle_trade_callback, bw.handle_backtest_callback = orig
        return calls

    def test_tradespot_exchange_mexc_callback(self) -> None:
        calls = self._run_callback("tradespot:exchange:mexc")
        self.assertEqual(calls["tradespot"], 1)
        self.assertEqual(calls["trade"], 0)

    def test_trade_callback_regression(self) -> None:
        calls = self._run_callback("trade:exchange:foo")
        self.assertEqual(calls["trade"], 1)
        self.assertEqual(calls["tradespot"], 0)

    def test_text_alias_invokes_tradespot_tx(self) -> None:
        sys.path.insert(0, "/usr/local/lib/hermes-agent")
        text = self.adapter_text
        self.assertIn("await _tradespot_tx(self, msg)", text)
        self.assertIn("handle_tradespot_text as _tradespot_tx", text)
        calls = {"tradespot": 0, "trade": 0}
        async def fake_tx(adapter, msg):
            calls["tradespot"] += 1
            return True
        async def fake_tr(adapter, msg):
            calls["trade"] += 1
            return False
        import plugins.trade.tradespot_wizard as tw
        import plugins.trade.wizard as w
        orig = (tw.handle_tradespot_text, w.handle_trade_text)
        tw.handle_tradespot_text = fake_tx  # type: ignore[assignment]
        w.handle_trade_text = fake_tr  # type: ignore[assignment]
        async def run():
            from plugins.trade.tradespot_wizard import handle_tradespot_text as _tradespot_tx
            from plugins.trade.wizard import handle_trade_text
            class A:
                name = "telegram"
            msg = SimpleNamespace(text="BTC")
            if await _tradespot_tx(A(), msg):
                return "tradespot"
            if await handle_trade_text(A(), msg):
                return "trade"
            return "none"
        try:
            result = asyncio.run(run())
        finally:
            tw.handle_tradespot_text, w.handle_trade_text = orig
        self.assertEqual(result, "tradespot")
        self.assertEqual(calls["tradespot"], 1)
        self.assertEqual(calls["trade"], 0)



class TestRealWizardCallbackAndText(unittest.TestCase):
    def test_mexc_callback_renders_amiroo(self) -> None:
        import types
        fake = types.ModuleType("plugins.platforms.telegram.adapter")
        class InlineKeyboardButton:
            def __init__(self, text, callback_data=None):
                self.text = text
                self.callback_data = callback_data
        class InlineKeyboardMarkup:
            def __init__(self, inline_keyboard=None):
                self.inline_keyboard = inline_keyboard or []
        fake.InlineKeyboardButton = InlineKeyboardButton
        fake.InlineKeyboardMarkup = InlineKeyboardMarkup
        sys.modules.setdefault("plugins.platforms", types.ModuleType("plugins.platforms"))
        sys.modules.setdefault("plugins.platforms.telegram", types.ModuleType("plugins.platforms.telegram"))
        sys.modules["plugins.platforms.telegram.adapter"] = fake
        sys.path.insert(0, "/root/kam")
        from plugins.trade.tradespot_wizard import handle_tradespot_callback, _WIZARD
        chat_key = ("1",)
        _WIZARD.reset(chat_key)
        _WIZARD.open(chat_key)
        edited = {}
        async def edit_message_text(text=None, reply_markup=None):
            edited["text"] = text
            edited["markup"] = reply_markup
        async def answer():
            return None
        query = SimpleNamespace(
            data="tradespot:exchange:mexc",
            message=SimpleNamespace(chat=SimpleNamespace(id=1), chat_id=1),
            edit_message_text=edit_message_text,
            answer=answer,
        )
        asyncio.run(handle_tradespot_callback(SimpleNamespace(name="telegram"), query, query.data))
        blob = (edited.get("text") or "") + " " + str(edited.get("markup"))
        self.assertIn("amiroo", blob.lower())

    def test_other_then_btc_text_path(self) -> None:
        sys.path.insert(0, "/root/kam")
        from plugins.trade.canonical import make_success, CanonicalMarketPrice
        from plugins.trade.tradespot_wizard import TradeSpotWizard

        class Desk:
            def list_exchanges(self):
                return ["mexc"]
            def list_accounts(self, exchange):
                return ["amiroo"]
            def capabilities(self, exchange, account=None):
                return ["balance", "list_instruments", "resolve_instrument", "market_price", "new_order"]
            def execute(self, request):
                op = str(request.get("operation") or "")
                if op == "list_instruments":
                    rows = [
                        {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT"},
                        {"symbol": "BTCUSDC", "baseAsset": "BTC", "quoteAsset": "USDC"},
                    ]
                    return make_success(op, "mexc", "amiroo", data={"instruments": rows, "count": 2})
                if op == "market_price":
                    return make_success(
                        op, "mexc", "amiroo",
                        market_price=CanonicalMarketPrice(
                            requested_symbol="BTCUSDT", market="BTCUSDT", mark_price="1", price="1"
                        ),
                        data={"symbol": "BTCUSDT", "price": "1"},
                    )
                return make_success(op, "mexc", "amiroo", data={})

        wiz = TradeSpotWizard(spotdesk=Desk())  # type: ignore[arg-type]
        key = ("synth", 99)
        wiz.open(key)
        wiz.handle_callback(key, "exchange:mexc")
        wiz.handle_callback(key, "account:amiroo")
        wiz.handle_callback(key, "action:new_order")
        screen = wiz.handle_callback(key, "asset:other")
        self.assertEqual(screen.state, "new_order_other")
        screen = wiz.handle_text(key, "BTC")
        blob = screen.text.upper() + " " + str(screen.buttons).upper()
        self.assertIn("BTC", blob)

if __name__ == "__main__":
    unittest.main()
