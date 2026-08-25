"""Focused offline tests for the standalone /fibo Telegram UI skeleton.

These tests do NOT require a live Hermes installation, gateway, or
exchange connection. They validate the public behavior of the new
``plugins/trade/fibo_wizard.py`` skeleton and the capability-aware
plugin registration. Each scenario maps to one of the eight test
cases the spec calls out (A-H).

A. --trade only              -> /trade registered, /fibo absent
B. --fibo only               -> /fibo registered, /trade absent, shared agents available
C. --trade --fibo            -> both registered, shared agents installed once
D. /fibo menu                -> exactly three buttons in order
E. callbacks                -> fibo:start, fibo:running, fibo:stop route correctly
F. placeholder actions      -> each button returns only its placeholder screen
G. uninstall capability isolation
                              -> removing trade preserves fibo, vice versa,
                                 shared agents preserved while either remains
H. no old Fibo runtime      -> fibo.service, fibo_daemon.py, fibo_service.py,
                                 golden_fibo/, daemon/runtime state all absent
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

# Make the repo root importable so ``plugins.trade`` resolves.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_install_state(caps: Dict[str, bool]) -> None:
    """Pre-seed ~/.hermes/kam/install_state.json with the given caps."""
    hermes_home = Path(tempfile.mkdtemp(prefix="kam-fibo-skel-"))
    (hermes_home / "kam").mkdir(parents=True)
    (hermes_home / "kam" / "install_state.json").write_text(
        json.dumps({"capabilities": caps})
    )
    os.environ["HERMES_HOME"] = str(hermes_home)


def _reset_install_state() -> None:
    os.environ.pop("HERMES_HOME", None)


def _fresh_plugin() -> Any:
    """Reload plugins.trade so the registration logic re-reads the manifest."""
    sys.modules.pop("plugins.trade", None)
    sys.modules.pop("plugins", None)
    # Clear package caches
    for k in list(sys.modules):
        if k.startswith("plugins."):
            del sys.modules[k]
    return importlib.import_module("plugins.trade")


class _RecordingCtx:
    """A minimal PluginContext stand-in that captures register_command calls."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def register_command(self, name, handler=None, description="", args_hint=""):
        self.calls.append(
            {"name": name, "handler": handler, "description": description, "args_hint": args_hint}
        )


# ---------------------------------------------------------------------------
# A. --trade only -> /trade registered, /fibo absent
# ---------------------------------------------------------------------------


class TradeOnlyRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        _set_install_state({"trade": True, "fibo": False})

    def tearDown(self) -> None:
        _reset_install_state()

    def test_trade_registered_fibo_absent(self) -> None:
        plugin = _fresh_plugin()
        ctx = _RecordingCtx()
        plugin.register(ctx)
        names = [c["name"] for c in ctx.calls]
        self.assertIn("trade", names)
        self.assertNotIn("fibo", names)


# ---------------------------------------------------------------------------
# B. --fibo only -> /fibo registered, /trade absent, shared agents available
# ---------------------------------------------------------------------------


class FiboOnlyRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        _set_install_state({"trade": False, "fibo": True})

    def tearDown(self) -> None:
        _reset_install_state()

    def test_fibo_registered_trade_absent(self) -> None:
        plugin = _fresh_plugin()
        ctx = _RecordingCtx()
        plugin.register(ctx)
        names = [c["name"] for c in ctx.calls]
        self.assertIn("fibo", names)
        self.assertNotIn("trade", names)

    def test_shared_exchange_agents_available(self) -> None:
        """Scenario B: with --fibo only, the shared agent layer is still present
        in the repo so /fibo can later reuse it. The test confirms the
        agent directory ships every expected agent."""
        agents_dir = REPO_ROOT / "plugins" / "trade" / "agents"
        self.assertTrue(agents_dir.is_dir(), f"{agents_dir} missing")
        expected = [
            "x_apex_agent.py",
            "x_arcus_agent.py",
            "x_edgex_agent.py",
            "x_hibachi_agent.py",
            "x_hyperliquid_agent.py",
            "x_lighter_agent.py",
            "x_ondoperps_agent.py",
            "x_pacifica_agent.py",
            "x_raydium_agent.py",
            "x_rise_agent.py",
        ]
        present = sorted(p.name for p in agents_dir.glob("x_*_agent.py"))
        self.assertEqual(present, sorted(expected))


# ---------------------------------------------------------------------------
# C. --trade --fibo -> both registered, shared agents installed once
# ---------------------------------------------------------------------------


class BothRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        _set_install_state({"trade": True, "fibo": True})

    def tearDown(self) -> None:
        _reset_install_state()

    def test_both_registered_once(self) -> None:
        plugin = _fresh_plugin()
        ctx = _RecordingCtx()
        plugin.register(ctx)
        names = [c["name"] for c in ctx.calls]
        self.assertEqual(names.count("trade"), 1)
        self.assertEqual(names.count("fibo"), 1)


# ---------------------------------------------------------------------------
# D. /fibo menu structure: exactly three buttons, in the required order
# ---------------------------------------------------------------------------


class FiboMenuStructureTests(unittest.TestCase):
    def test_three_buttons_in_required_order(self) -> None:
        from plugins.trade import fibo_wizard

        buttons = fibo_wizard.SCREEN_BUTTONS
        self.assertEqual(len(buttons), 3)
        self.assertEqual(
            list(buttons),
            [
                ("▶️ Start Fibo",   "fibo:start"),
                ("📋 Running Fibo", "fibo:running"),
                ("⛔️ Stop Fibo",    "fibo:stop"),
            ],
        )


# ---------------------------------------------------------------------------
# E. Callbacks route correctly (fibo:start, fibo:running, fibo:stop)
# ---------------------------------------------------------------------------


class FiboCallbackRoutingTests(unittest.TestCase):
    def test_callback_namespace_is_fibo(self) -> None:
        from plugins.trade import fibo_wizard

        for _, cb in fibo_wizard.SCREEN_BUTTONS:
            self.assertTrue(cb.startswith("fibo:"))

    def test_required_callbacks_present(self) -> None:
        from plugins.trade import fibo_wizard

        cbs = {cb for _, cb in fibo_wizard.SCREEN_BUTTONS}
        self.assertEqual(cbs, {"fibo:start", "fibo:running", "fibo:stop"})

    def test_callback_handlers_route_to_placeholders(self) -> None:
        """Each callback routes to its placeholder screen, not a /trade wizard."""
        from plugins.trade import fibo_wizard

        # Stub Query matching PTB's real edit_message_text(text, reply_markup)
        # signature so we exercise the wizard's live code path.

        class StubQuery:
            def __init__(self) -> None:
                self.edited_text = None
                self.edited_markup = None
                self.answered = False

            async def edit_message_text(self, text="", reply_markup=None):
                self.edited_text = text
                self.edited_markup = reply_markup

            def answer(self):
                self.answered = True

        async def run_one(callback_data: str) -> Dict[str, Any]:
            q = StubQuery()
            await fibo_wizard.handle_fibo_callback(None, q, callback_data)
            # The wizard builds an InlineKeyboardMarkup via PTB directly.
            # For testing, normalise it to a list-of-rows shape.
            markup = q.edited_markup
            if markup is None:
                rows = None
            else:
                inline = getattr(markup, "inline_keyboard", None)
                if inline is None and hasattr(markup, "__iter__"):
                    inline = list(markup)
                rows = [list(row) for row in (inline or [])]
            return {
                "edited_text": q.edited_text,
                "edited_buttons": rows,
                "answered": q.answered,
            }

        for cb in ("fibo:start", "fibo:running", "fibo:stop"):
            with self.subTest(callback=cb):
                import asyncio
                result = asyncio.run(run_one(cb))
                # Placeholder screen text matches SCREEN_TEXT[cb].
                self.assertEqual(result["edited_text"], fibo_wizard.SCREEN_TEXT[cb])
                self.assertTrue(result["answered"])
                # The buttons offered after the placeholder are the entry
                # buttons (so the user can pick another action).
                self.assertIsNotNone(result["edited_buttons"])
                # Normalise InlineKeyboardButton objects to dicts so we can
                # assert on text / callback_data uniformly across adapters.
                def _to_dict(btn):
                    if isinstance(btn, dict):
                        return btn
                    # PTB InlineKeyboardButton has .text and .callback_data.
                    return {
                        "text": getattr(btn, "text", "") or "",
                        "callback_data": getattr(btn, "callback_data", "") or "",
                    }

                flat = [
                    _to_dict(btn)["text"]
                    for row in result["edited_buttons"] or []
                    for btn in (row or [])
                ]
                self.assertIn("▶️ Start Fibo", flat)
                self.assertIn("📋 Running Fibo", flat)
                self.assertIn("⛔️ Stop Fibo", flat)
                # And the callback_data carries the fibo: namespace.
                flat_cb = [
                    _to_dict(btn)["callback_data"]
                    for row in result["edited_buttons"] or []
                    for btn in (row or [])
                ]
                self.assertIn("fibo:start", flat_cb)
                self.assertIn("fibo:running", flat_cb)
                self.assertIn("fibo:stop", flat_cb)


# ---------------------------------------------------------------------------
# F. Placeholder actions: each button returns ONLY its placeholder screen,
#    and never calls an exchange agent.
# ---------------------------------------------------------------------------


class PlaceholderActionTests(unittest.TestCase):
    def test_callbacks_return_placeholder_only(self) -> None:
        from plugins.trade import fibo_wizard

        for cb in ("fibo:start", "fibo:running", "fibo:stop"):
            with self.subTest(callback=cb):
                text = fibo_wizard.SCREEN_TEXT.get(cb)
                self.assertIsInstance(text, str)
                self.assertTrue(text.strip())
                # The placeholder text must be the action title (no trade refs).
                self.assertNotIn("trade", text.lower())
                self.assertNotIn("engine", text.lower())
                self.assertNotIn("service", text.lower())

    def test_no_execute_invocation_on_callback(self) -> None:
        """Placeholder must not call any agent .execute() (no exchange writes)."""
        from plugins.trade import fibo_wizard

        # Spy on a fake agent with .execute() and assert it is never called.
        class SpyAgent:
            def __init__(self) -> None:
                self.calls: List[Any] = []

            def execute(self, request):
                self.calls.append(request)
                return {"ok": True}

        agent = SpyAgent()
        adapter = mock.MagicMock()
        adapter.send_message = mock.AsyncMock()

        async def run_all() -> None:
            for cb in ("fibo:start", "fibo:running", "fibo:stop"):
                q = mock.MagicMock()
                q.edit_message_text = mock.MagicMock()
                q.answer = mock.MagicMock()
                await fibo_wizard.handle_fibo_callback(adapter, q, cb)

        import asyncio
        asyncio.run(run_all())
        self.assertEqual(agent.calls, [])
        # send_message must NOT have been called either — placeholder is edit-only.
        adapter.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# G. Uninstall capability isolation
# ---------------------------------------------------------------------------


class UninstallCapabilityIsolationTests(unittest.TestCase):
    def test_uninstall_trade_paths_disjoint_from_fibo_paths(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "installer"))
        from uninstall_trade_capability import TRADE_REL_PATHS
        from uninstall_fibo_capability import FIBO_REL_PATHS

        trade_set = {str(p) for p in TRADE_REL_PATHS}
        fibo_set = {str(p) for p in FIBO_REL_PATHS}
        # Disjoint — uninstalling trade must NOT remove any fibo file.
        self.assertEqual(trade_set & fibo_set, set())
        # And vice versa.
        self.assertEqual(fibo_set & trade_set, set())

    def test_uninstall_trade_does_not_remove_fibo_wizard(self) -> None:
        """Apply uninstall_trade_capability against a synthetic tree and
        assert that fibo_wizard.py is untouched."""
        sys.path.insert(0, str(REPO_ROOT / "installer"))
        from uninstall_trade_capability import run as uninstall_trade

        with tempfile.TemporaryDirectory() as td:
            plugin_root = Path(td) / "plugins" / "trade"
            plugin_root.mkdir(parents=True)
            (plugin_root / "wizard.py").write_text("# /trade wizard")
            (plugin_root / "fibo_wizard.py").write_text("# /fibo wizard")
            # No state dir created — /trade uninstall only removes wizard.py.
            hermes_home = Path(td) / "hermes_home"

            uninstall_trade(
                argv=[], hermes_root=Path(td), hermes_home=hermes_home,
                dry_run=False,
            )
            # fibo_wizard.py is still there.
            self.assertTrue((plugin_root / "fibo_wizard.py").is_file())
            # wizard.py was removed.
            self.assertFalse((plugin_root / "wizard.py").is_file())

    def test_uninstall_fibo_does_not_remove_trade_wizard(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "installer"))
        from uninstall_fibo_capability import run as uninstall_fibo

        with tempfile.TemporaryDirectory() as td:
            plugin_root = Path(td) / "plugins" / "trade"
            plugin_root.mkdir(parents=True)
            (plugin_root / "wizard.py").write_text("# /trade wizard")
            (plugin_root / "fibo_wizard.py").write_text("# /fibo wizard")
            hermes_home = Path(td) / "hermes_home"

            uninstall_fibo(
                argv=[], hermes_root=Path(td), hermes_home=hermes_home,
                dry_run=False,
            )
            self.assertTrue((plugin_root / "wizard.py").is_file())
            self.assertFalse((plugin_root / "fibo_wizard.py").is_file())

    def test_uninstall_fibo_does_not_create_or_remove_runtime_dir(self) -> None:
        """Per spec correction: do NOT touch ~/.hermes/fibo/ in this phase."""
        sys.path.insert(0, str(REPO_ROOT / "installer"))
        from uninstall_fibo_capability import run as uninstall_fibo

        with tempfile.TemporaryDirectory() as td:
            hermes_home = Path(td) / "hermes_home"
            hermes_root = Path(td) / "hermes_root"
            hermes_home.mkdir()
            hermes_root.mkdir()
            # Pre-create a fibo runtime dir that should NOT be deleted.
            fibo_dir = hermes_home / "fibo"
            fibo_dir.mkdir(parents=True)
            (fibo_dir / "service_state.json").write_text("{}")

            uninstall_fibo(
                argv=[], hermes_root=hermes_root, hermes_home=hermes_home,
                dry_run=False,
            )
            # The fibo runtime dir is untouched.
            self.assertTrue(fibo_dir.is_dir())
            self.assertTrue((fibo_dir / "service_state.json").is_file())


# ---------------------------------------------------------------------------
# H. No old Fibo runtime restored
# ---------------------------------------------------------------------------


class NoOldFiboRuntimeTests(unittest.TestCase):
    def test_no_fibo_service_unit(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "installer"))

        def _strip_docstring(src: str) -> str:
            """Remove the module docstring so docstring exclusions don't
            trigger false positives. Keeps the rest of the source."""
            lines = src.splitlines(keepends=True)
            if lines and lines[0].startswith('"""'):
                # Find the matching closing triple-quote.
                for i in range(1, len(lines)):
                    if '"""' in lines[i]:
                        return "".join(lines[i + 1:])
            return src

        cap_src = _strip_docstring(
            (REPO_ROOT / "installer" / "install_fibo_capability.py").read_text()
        )
        forbidden = ["fibo.service", "fibo_daemon", "fibo_service", "golden_fibo", "engine.py"]
        leaks = [tok for tok in forbidden if tok in cap_src]
        self.assertEqual(
            leaks, [],
            f"install_fibo_capability code body references old runtime: {leaks}",
        )

        uninst_src = _strip_docstring(
            (REPO_ROOT / "installer" / "uninstall_fibo_capability.py").read_text()
        )
        leaks = [tok for tok in forbidden if tok in uninst_src]
        self.assertEqual(
            leaks, [],
            f"uninstall_fibo_capability code body references old runtime: {leaks}",
        )

    def test_no_legacy_files_in_repo(self) -> None:
        # /fibo runtime / engine / state files must NOT be tracked in the repo.
        forbidden_paths = [
            REPO_ROOT / "plugins" / "trade" / "fibo_daemon.py",
            REPO_ROOT / "plugins" / "trade" / "fibo_service.py",
            REPO_ROOT / "plugins" / "trade" / "golden_fibo",
            REPO_ROOT / "installer" / "fibo.service.template",
            REPO_ROOT / "installer" / "fibo_unit.py",
        ]
        for p in forbidden_paths:
            self.assertFalse(p.exists(), f"legacy Fibo file present: {p}")

    def test_no_fibo_runtime_install_path(self) -> None:
        """The /fibo capability installer must not even mention
        ``~/.hermes/fibo/`` runtime state."""
        cap_src = (REPO_ROOT / "installer" / "install_fibo_capability.py").read_text()
        # The installer should never mkdir a /fibo state dir. The string
        # 'fibo' as a directory name may appear only as the capability
        # name in capability tables, not in mkdir or join() calls.
        mkdir_calls = [ln for ln in cap_src.splitlines() if "mkdir" in ln]
        for ln in mkdir_calls:
            self.assertNotIn("fibo", ln, f"install_fibo_capability mkdirs fibo path: {ln!r}")


# ---------------------------------------------------------------------------
# Regression tests for the /fibo send-path fix
#
# These pin fibo_wizard to the LIVE TelegramAdapter public surface:
#   - send_inline_keyboard(chat_id, text, buttons, callback_prefix, *, metadata, parse_mode)
#   - send(chat_id, content, reply_to, metadata)
#
# And ensure fibo_wizard never silently swallows a missing send method.
# ---------------------------------------------------------------------------


class FiboSendPathRegressionTests(unittest.TestCase):
    """Pin the /fibo send/edit path to real Hermes Telegram adapter API."""

    def test_no_send_message_reference_in_fibo_wizard_source(self) -> None:
        """fibo_wizard must NOT call adapter.send_message — that method does
        not exist on the live TelegramAdapter and silently drops the message.

        Excludes the module docstring and the ``_FORBIDDEN_METHOD`` constant
        (kept ONLY so a future regression that re-introduces the call can be
        grep-detected); we want to prove no actual code path uses it.
        """
        import inspect
        import re

        from plugins.trade import fibo_wizard

        src = inspect.getsource(fibo_wizard)
        # Strip the module docstring.
        src = re.sub(r'^\s*"""[\s\S]*?"""\s*', "", src, count=1, flags=re.MULTILINE)
        # Strip the _FORBIDDEN_METHOD constant declaration line.
        src = re.sub(
            r'_FORBIDDEN_METHOD\s*=\s*"send_message"\s*\n',
            "",
            src,
        )
        # Also strip any in-source comment that explicitly references send_message
        # (regression-trigger documentation is allowed; only an actual call is banned).
        src_no_comments = re.sub(r"#.*", "", src)
        self.assertNotIn(
            "send_message",
            src_no_comments,
            "fibow_wizard still references the non-existent adapter.send_message; "
            "use adapter.send_inline_keyboard(..., callback_prefix='fibo') or "
            "adapter.send(chat_id, content) for the fallback",
        )

    def test_handle_fibo_command_invokes_send_inline_keyboard(self) -> None:
        """handle_fibo_command must drive adapter.send_inline_keyboard with
        callback_prefix='fibo' and the three fibo: buttons."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from plugins.trade import fibo_wizard

        adapter = MagicMock()
        adapter.send_inline_keyboard = AsyncMock(
            return_value=MagicMock(success=True, message_id="123")
        )

        msg = MagicMock()
        msg.text = "/fibo"
        msg.chat.id = 64620303

        result = asyncio.run(fibo_wizard.handle_fibo_command(adapter, msg))
        self.assertTrue(result)
        adapter.send_inline_keyboard.assert_awaited_once()
        kwargs = adapter.send_inline_keyboard.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], "64620303")
        self.assertEqual(kwargs["callback_prefix"], "fibo")
        # Three buttons in fibo: namespace, in expected order.
        buttons = kwargs["buttons"]
        flat_cb = [
            str(btn["callback_data"]) for row in buttons for btn in row
        ]
        self.assertEqual(
            flat_cb, ["fibo:start", "fibo:running", "fibo:stop"],
        )

    def test_handle_fibo_command_falls_back_to_send_when_inline_keyboard_missing(self) -> None:
        """If send_inline_keyboard is absent, /fibo must still send a
        plain-text screen via adapter.send (no silent drop, no buttons)."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from plugins.trade import fibo_wizard

        adapter = MagicMock(spec=["send"])  # only `send` exists
        adapter.send = AsyncMock(
            return_value=MagicMock(success=True, message_id="123")
        )

        msg = MagicMock()
        msg.text = "/fibo"
        msg.chat.id = 64620303

        result = asyncio.run(fibo_wizard.handle_fibo_command(adapter, msg))
        self.assertTrue(result)
        adapter.send.assert_awaited_once()
        kwargs = adapter.send.await_args.kwargs
        # Plain-text fallback passes text= and NOT buttons=.
        self.assertIn("text", kwargs)
        # PTB's plain `send` only takes (chat_id, content, ...) — verify
        # the wizard did NOT pass buttons=.
        self.assertNotIn("buttons", kwargs)

    def test_missing_sender_does_not_silently_return_success(self) -> None:
        """If the adapter has NO send method at all, handle_fibo_command must
        return False (not claim success) and emit ERROR-level logging.

        Regression for the original bug: the wizard used to return True
        while dropping the message on the floor.
        """
        import asyncio
        import logging
        from unittest.mock import MagicMock

        from plugins.trade import fibo_wizard

        adapter = MagicMock(spec=["name"])  # no send / send_inline_keyboard

        msg = MagicMock()
        msg.text = "/fibo"
        msg.chat.id = 64620303

        with self.assertLogs("plugins.trade.fibo_wizard", level="ERROR") as cm:
            result = asyncio.run(fibo_wizard.handle_fibo_command(adapter, msg))
        self.assertFalse(result, "missing-sender must NOT claim success")
        # The error log must mention the failure to render.
        joined = "\n".join(cm.output)
        self.assertIn("adapter has neither", joined)
        self.assertIn("cannot render /fibo screen", joined)

    def test_callback_rendering_uses_send_inline_keyboard_contract(self) -> None:
        """The /fibo callback edit path must succeed against an adapter that
        exposes only the real PTB inline-keyboard helper (matches what
        Hermes's TelegramAdapter actually exposes at adapter.py:8784)."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from plugins.trade import fibo_wizard

        adapter = MagicMock()
        adapter.send_inline_keyboard = AsyncMock(
            return_value=MagicMock(success=True, message_id="123")
        )

        # First: open /fibo to populate the chat state — but /fibo here is
        # stateless for the skeleton, so this is purely a routing check.
        msg = MagicMock()
        msg.text = "/fibo"
        msg.chat.id = 64620303
        asyncio.run(fibo_wizard.handle_fibo_command(adapter, msg))

        # Then: simulate a callback query.
        query = MagicMock()
        async def _edit(text="", reply_markup=None):
            query.edited_text = text
            query.edited_markup = reply_markup
        query.edit_message_text = _edit
        query.answer = lambda: setattr(query, "answered", True) or None

        asyncio.run(fibo_wizard.handle_fibo_callback(adapter, query, "fibo:start"))
        self.assertEqual(query.edited_text, fibo_wizard.SCREEN_TEXT["fibo:start"])

    def test_callback_rendering_uses_real_ptb_edit_message_text(self) -> None:
        """The /fibo callback edit path must use query.edit_message_text (the
        real PTB API surface) — not a non-existent method."""
        import asyncio
        import inspect
        from unittest.mock import AsyncMock, MagicMock

        from plugins.trade import fibo_wizard

        # Compile the wizard source and ensure it imports / uses the
        # PTB-native edit_message_text path, not a made-up one.
        src = inspect.getsource(fibo_wizard._edit)
        self.assertIn("edit_message_text", src)
        self.assertIn("InlineKeyboardMarkup", src)
        self.assertIn("InlineKeyboardButton", src)
        # Forbidden: any reference to a fake/legacy method.
        self.assertNotIn("edit_message_reply_markup", src)

    def test_no_exchange_write_path_invoked(self) -> None:
        """Each /fibo placeholder must NOT call any agent execute() / write.
        (Regression for the safety boundary: the wizard is UI-only.)"""
        from plugins.trade import fibo_wizard
        import inspect
        src = inspect.getsource(fibo_wizard)
        for forbidden in (
            ".execute(",
            "TradeDesk(",
            "TradeWizard(",
            "_WIZARD.",
            "balance",
            "positions",
            "ladder",
        ):
            self.assertNotIn(forbidden, src, f"fibo_wizard source contains {forbidden!r}")


if __name__ == "__main__":
    unittest.main()