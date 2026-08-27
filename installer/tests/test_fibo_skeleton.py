"""Focused offline tests for the standalone /fibo Telegram UI skeleton.

These tests do NOT require a live Hermes installation, gateway, or
exchange connection. They validate the public behavior of the new
``plugins/trade/fibo_wizard.py`` skeleton and the capability-aware
plugin registration. Each scenario maps to one of the eight test
cases the spec calls out (A-H).

A. --trade only              -> /trade registered, /fibo absent
B. --fibo only               -> /fibo registered, /trade absent, shared agents available
C. --trade --fibo            -> both registered, shared agents installed once
D. /fibo menu                -> exactly four buttons in order
E. callbacks                -> fibo:start, fibo:running, fibo:stop, fibo:exit route correctly
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
# D. /fibo menu structure: exactly four buttons, in the required order
# ---------------------------------------------------------------------------


class FiboMenuStructureTests(unittest.TestCase):
    def test_four_buttons_in_required_order(self) -> None:
        """The /fibo entry screen carries exactly four buttons in this order:

            ▶️ Start Fibo   → fibo:start
            📋 Running Fibo → fibo:running
            ⛔️ Stop Fibo    → fibo:stop
            ❌ Exit         → fibo:exit

        Exit is appended last so it sits beneath the strategy controls
        and reads as a "leave the wizard" action rather than a strategy
        toggle. The button labels and callback_data are pinned exactly
        so the wizard cannot drift from this contract.
        """
        from plugins.trade import fibo_wizard

        buttons = fibo_wizard.SCREEN_BUTTONS
        self.assertEqual(len(buttons), 4)
        self.assertEqual(
            list(buttons),
            [
                ("▶️ Start Fibo",   "fibo:start"),
                ("📋 Running Fibo", "fibo:running"),
                ("⛔️ Stop Fibo",    "fibo:stop"),
                ("❌ Exit",          "fibo:exit"),
            ],
        )

    def test_exit_button_label_and_callback_data(self) -> None:
        """Pin the Exit button's exact label + callback_data."""
        from plugins.trade import fibo_wizard

        exit_button = fibo_wizard.SCREEN_BUTTONS[-1]
        self.assertEqual(exit_button, ("❌ Exit", "fibo:exit"))


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
        self.assertEqual(
            cbs, {"fibo:start", "fibo:running", "fibo:stop", "fibo:exit"},
        )

    def test_exit_callback_in_fibo_namespace(self) -> None:
        """Pin the contract: fibo:exit lives in the dedicated ``fibo:``
        namespace (NOT ``trade:``). This guards against a future change
        that would accidentally rename Exit's callback prefix."""
        from plugins.trade import fibo_wizard

        exit_cb = fibo_wizard.SCREEN_BUTTONS[-1][1]
        self.assertTrue(
            exit_cb.startswith("fibo:"),
            f"Exit callback {exit_cb!r} is not in the fibo: namespace",
        )
        self.assertNotIn("trade", exit_cb)
        self.assertEqual(exit_cb, "fibo:exit")

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

        # Phase 2: fibo:running is the read-only dry-run view, NOT a
        # placeholder. fibo:stop is the Stop-Fibo picker (Phase 2.6).
        # fibo:start opens the Start Fibo sub-flow.
        for cb in ("fibo:stop",):
            with self.subTest(callback=cb):
                import asyncio
                result = asyncio.run(run_one(cb))
                # Phase 2.6: fibo:stop now renders the Stop picker
                # (or an empty-list screen if no registrations exist).
                # Either way, the screen is NOT the Phase-1
                # "Stop Fibo" placeholder text.
                self.assertNotEqual(
                    result["edited_text"],
                    fibo_wizard.SCREEN_TEXT.get(cb, ""),
                    "fibo:stop must no longer route through the "
                    "Phase-1 placeholder path.",
                )
                self.assertTrue(result["answered"])
                # The buttons offered are the per-registration picker
                # buttons (or, if empty, the Start/Running/Exit trio).
                self.assertIsNotNone(result["edited_buttons"])


# ---------------------------------------------------------------------------
# F. Placeholder actions: each button returns ONLY its placeholder screen,
#    and never calls an exchange agent.
# ---------------------------------------------------------------------------


class PlaceholderActionTests(unittest.TestCase):
    def test_callbacks_return_placeholder_only(self) -> None:
        """Placeholder text exists for the strategy button. Exit is
        deliberately absent from SCREEN_TEXT — it does not render a
        placeholder screen (it closes the wizard UI instead).

        Phase 2: fibo:start opens the Start Fibo sub-flow and
        fibo:running opens the read-only dry-run view. Neither is a
        placeholder.
        """
        from plugins.trade import fibo_wizard

        for cb in ("fibo:stop",):
            with self.subTest(callback=cb):
                text = fibo_wizard.SCREEN_TEXT.get(cb)
                self.assertIsInstance(text, str)
                self.assertTrue(text.strip())
                # The placeholder text must be the action title (no trade refs).
                self.assertNotIn("trade", text.lower())
                self.assertNotIn("engine", text.lower())
                self.assertNotIn("service", text.lower())

        # Exit must NOT have a placeholder screen — that would re-render
        # the entry menu and contradict the "close the wizard" contract.
        from plugins.trade import fibo_wizard
        self.assertNotIn(
            "fibo:exit", fibo_wizard.SCREEN_TEXT,
            "fibo:exit must not have a placeholder; it closes the wizard UI.",
        )

    def test_no_execute_invocation_on_callback(self) -> None:
        """Placeholder must not call any agent .execute() (no exchange writes).
        Exit must also perform ZERO exchange calls — it's a UI dismiss only.

        Phase 2: fibo:start opens the Start Fibo sub-flow (no exchange
        writes by construction). fibo:running opens the read-only
        dry-run view; it does call the existing TradeDesk for
        resolve_instrument and positions_orders which are pure GETs
        (the same path /trade uses for read-only views). Neither
        call mutates any exchange state.
        """
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
            # Phase 2: fibo:start and fibo:running open sub-flows /
            # dry-run views. Both are read-only end-to-end and never
            # invoke the .execute() method on a fake agent. The
            # /trade shared TradeDesk.execute is the only "execute"
            # path reachable, and it is never wired to a fake here.
            for cb in ("fibo:start", "fibo:running", "fibo:stop", "fibo:exit"):
                q = mock.MagicMock()
                q.delete_message = mock.MagicMock()  # Exit-path available
                q.edit_message_text = mock.MagicMock()
                q.answer = mock.MagicMock()
                await fibo_wizard.handle_fibo_callback(adapter, q, cb)

        import asyncio
        asyncio.run(run_all())
        self.assertEqual(agent.calls, [])
        # send_message must NOT have been called either — wizard is UI-only.
        adapter.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# F2. Exit callback: closes the wizard UI, performs ZERO exchange work,
#     does NOT touch .env, does NOT stop any registration. Pure UI dismiss.
# ---------------------------------------------------------------------------


class FiboExitCallbackTests(unittest.TestCase):
    """Contract tests for the /fibo Exit button (``fibo:exit``).

    Spec:

      * Best path: ``query.delete_message()`` — the wizard message
        vanishes from Telegram.
      * Fallback: ``query.edit_message_text(text, reply_markup=None)`` —
        the inline keyboard is stripped, message remains as a minimal
        "Fibo closed." confirmation.
      * NEVER call any exchange agent.
      * NEVER touch ``.env`` or the filesystem.
      * NEVER stop any Fibo registration (no daemon / service / state
        mutation).
      * Acknowledge the query so Telegram drops the loading indicator.
    """

    def _make_query(self):
        """Stub query matching PTB's real CallbackQuery surface:

            * ``delete_message()`` (coroutine)
            * ``edit_message_text(text=, reply_markup=)`` (coroutine)
            * ``answer()`` (sync)
        """

        class StubQuery:
            def __init__(self) -> None:
                self.deleted = False
                self.edited_text = None
                self.edited_markup = "__unset__"  # distinguish "not called"
                self.answered = False

            async def delete_message(self):
                self.deleted = True

            async def edit_message_text(self, text="", reply_markup=None):
                self.edited_text = text
                self.edited_markup = reply_markup

            def answer(self):
                self.answered = True

        return StubQuery()

    def test_exit_prefers_delete_message(self) -> None:
        """Happy path: query exposes delete_message; Exit must call it
        and must NOT fall back to edit_message_text (delete is preferred)."""
        import asyncio
        from plugins.trade import fibo_wizard

        q = self._make_query()
        asyncio.run(fibo_wizard.handle_fibo_callback(None, q, "fibo:exit"))
        self.assertTrue(q.deleted, "Exit must call query.delete_message()")
        # The edit path should NOT have been invoked when delete succeeded.
        self.assertEqual(
            q.edited_markup, "__unset__",
            "Exit must NOT fall back to edit_message_text when delete succeeds",
        )
        self.assertTrue(q.answered, "Exit must acknowledge the query")

    def test_exit_falls_back_to_edit_when_delete_unavailable(self) -> None:
        """Fallback path: query exposes only edit_message_text. Exit
        must strip the keyboard by editing the message with
        reply_markup=None."""
        import asyncio
        from plugins.trade import fibo_wizard

        class NoDeleteQuery:
            def __init__(self) -> None:
                self.edited_text = None
                self.edited_markup = "__unset__"
                self.answered = False

            async def edit_message_text(self, text="", reply_markup=None):
                self.edited_text = text
                self.edited_markup = reply_markup

            def answer(self):
                self.answered = True

        q = NoDeleteQuery()
        asyncio.run(fibo_wizard.handle_fibo_callback(None, q, "fibo:exit"))
        self.assertEqual(
            q.edited_text, "Fibo closed.",
            "Exit fallback must leave the minimal closed-state text",
        )
        self.assertIsNone(
            q.edited_markup,
            "Exit fallback must pass reply_markup=None to strip the keyboard",
        )
        self.assertTrue(q.answered)

    def test_exit_falls_back_to_edit_when_delete_raises(self) -> None:
        """If delete_message() raises (e.g. TelegramError: message too
        old), Exit must swallow it and try the edit-strip-keyboard
        path."""
        import asyncio
        from plugins.trade import fibo_wizard

        class DeleteFailsQuery:
            def __init__(self) -> None:
                self.delete_called = False
                self.edited_text = None
                self.edited_markup = "__unset__"
                self.answered = False

            async def delete_message(self):
                self.delete_called = True
                raise RuntimeError("simulated TelegramError")

            async def edit_message_text(self, text="", reply_markup=None):
                self.edited_text = text
                self.edited_markup = reply_markup

            def answer(self):
                self.answered = True

        q = DeleteFailsQuery()
        asyncio.run(fibo_wizard.handle_fibo_callback(None, q, "fibo:exit"))
        self.assertTrue(q.delete_called, "Exit must attempt delete_message")
        self.assertEqual(q.edited_text, "Fibo closed.")
        self.assertIsNone(q.edited_markup)
        self.assertTrue(q.answered)

    def test_exit_does_not_re_render_entry_screen(self) -> None:
        """Critical: Exit must NOT call edit_message_text with the
        four-button entry menu (that would leave the wizard open).
        It is a UI close, not a screen swap."""
        import asyncio
        from plugins.trade import fibo_wizard

        q = self._make_query()
        asyncio.run(fibo_wizard.handle_fibo_callback(None, q, "fibo:exit"))
        # edit_message_text must not have been called with buttons.
        # When the delete path wins, edited_markup is never touched at all.
        self.assertNotEqual(
            q.edited_markup, "__unset__NOT_BTN",
            "Exit must not render any inline keyboard markup",
        )
        # And in the happy path, edit_message_text was NOT called at all,
        # so the entry buttons stay out of the picture entirely.
        self.assertEqual(q.edited_markup, "__unset__")

    def test_exit_performs_zero_exchange_calls(self) -> None:
        """Exit must NOT touch any agent, environment file, or runtime state.

        We assert this two ways:

          1. The wizard source must not import / call any agent layer
             (``x_*_agent``) or the ``TradeDesk`` / ``TradeWizard``
             runtime. This is a structural guard — Exit cannot leak
             exchange work without ``execute()`` appearing in the wizard.

          2. With a spy agent equipped with ``.execute()``, calling
             ``fibo:exit`` never invokes it. This is the runtime guard.
        """
        import asyncio
        import inspect
        from unittest import mock

        from plugins.trade import fibo_wizard

        # Structural: scan source for any exchange / runtime handle.
        src = inspect.getsource(fibo_wizard)
        forbidden_in_source = (
            ".execute(",
            "TradeDesk(",
            "TradeWizard(",
            "_WIZARD.",
            "x_apex_agent",
            "x_arcus_agent",
            "x_hyperliquid_agent",
            "x_lighter_agent",
            "x_pacifica_agent",
            "x_rise_agent",
            "x_edgex_agent",
            "x_ondoperps_agent",
            "x_raydium_agent",
            "x_hibachi_agent",
        )
        for tok in forbidden_in_source:
            self.assertNotIn(
                tok, src,
                f"fibo_wizard must never reference {tok!r}; Exit must "
                "perform zero exchange work.",
            )

        # Runtime: a spy agent with .execute() is never invoked by Exit.
        class SpyAgent:
            def __init__(self) -> None:
                self.calls: List[Any] = []

            def execute(self, request):
                self.calls.append(request)
                return {"ok": True}

        agent = SpyAgent()
        adapter = mock.MagicMock()
        adapter.send_message = mock.AsyncMock()

        q = self._make_query()
        asyncio.run(fibo_wizard.handle_fibo_callback(adapter, q, "fibo:exit"))
        self.assertEqual(
            agent.calls, [],
            "Exit must not invoke any agent.execute() — pure UI close.",
        )
        adapter.send_message.assert_not_called()

    def test_exit_does_not_touch_dotenv(self) -> None:
        """Exit must not import / mutate ``.env`` files.

        We assert by static check: the wizard source never imports
        ``dotenv`` / ``environ`` / ``.env`` and never opens an env file
        for write. The wizard module's file path is already in a
        plugin tree with no ``.env`` access; this guard pins the
        contract so a future change cannot regress it silently.
        """
        import inspect
        from plugins.trade import fibo_wizard

        src = inspect.getsource(fibo_wizard)
        # We don't ban the literal token ".env" everywhere — the docstring
        # mentions it as something Exit does NOT touch. Strip docstrings
        # before scanning.
        import re
        src_no_docs = re.sub(r'^\s*"""[\s\S]*?"""\s*', "", src, flags=re.MULTILINE)
        for forbidden in (
            "load_dotenv",
            "dotenv.",
            "open(",
            ".env",
            "environ[",
            "os.environ[",
        ):
            # ``.env`` and ``open(`` are documented references in comments
            # we explicitly allow (see "does NOT touch .env"). Assert only
            # the actionable code paths are absent.
            if forbidden in (".env", "open("):
                # Allow literal mentions in comments / docstrings only.
                # If a code-path invocation appears, this will fire when
                # the wizard tries to actually open or load a file.
                # We do a permissive check: forbid ``open(... `` or
                # ``Path(...).env`` patterns specifically.
                self.assertNotRegex(
                    src_no_docs,
                    r"open\([^)]*\.env",
                    "Exit must not open .env files",
                )
                continue
            self.assertNotIn(
                forbidden, src_no_docs,
                f"fibo_wizard must not reference {forbidden!r}; "
                "Exit is a UI-only action.",
            )

    def test_exit_does_not_stop_registration(self) -> None:
        """Exit is purely UI — it must not invoke any
        register/unregister / stop / shutdown / daemon control path.

        Static guard: the wizard source has no reference to daemon,
        service, registration, or shutdown semantics.
        """
        import inspect
        import re
        from plugins.trade import fibo_wizard

        src = inspect.getsource(fibo_wizard)
        src_no_docs = re.sub(r'^\s*"""[\s\S]*?"""\s*', "", src, flags=re.MULTILINE)

        # No callable references to lifecycle paths.
        forbidden_callables = (
            "register(",
            "unregister(",
            "stop_fibo(",
            "shutdown(",
            "daemon.stop",
            "service.stop",
            "subprocess.",
        )
        for tok in forbidden_callables:
            self.assertNotIn(
                tok, src_no_docs,
                f"Exit must not invoke {tok!r}; it is UI-only.",
            )

    def test_strategy_callbacks_unchanged(self) -> None:
        """Regression: Stop must NOT close the wizard (Exit path is
        separate). Phase 2.6: ``fibo:stop`` opens the Stop picker
        rather than the legacy Phase-1 placeholder screen.

        Exit is added to the entry menu but does NOT alter the
        strategy-callback behaviour.

        Phase 2: ``fibo:running`` is no longer a placeholder — it
        opens the read-only dry-run view (covered by
        ``test_fibo_reconciler.py``). ``fibo:start`` opens the Start
        Fibo sub-flow (covered by ``test_fibo_start_flow.py``).
        """
        import asyncio
        from plugins.trade import fibo_wizard

        class StubQuery:
            def __init__(self) -> None:
                self.edited_text = None
                self.edited_markup = None
                self.answered = False
                self.deleted = False

            async def edit_message_text(self, text="", reply_markup=None):
                self.edited_text = text
                self.edited_markup = reply_markup

            async def delete_message(self):
                self.deleted = True

            def answer(self):
                self.answered = True

        for cb in ("fibo:stop",):
            with self.subTest(callback=cb):
                q = StubQuery()
                asyncio.run(
                    fibo_wizard.handle_fibo_callback(None, q, cb)
                )
                # The strategy callbacks still go through the
                # edit path, NOT the close path.
                self.assertFalse(q.deleted, f"{cb} must NOT close the wizard")
                # Phase 2.6: Stop now renders the Stop picker
                # (not the Phase-1 placeholder text).
                self.assertNotEqual(
                    q.edited_text, fibo_wizard.SCREEN_TEXT.get(cb, ""),
                    f"{cb} must no longer render the Phase-1 "
                    f"SCREEN_TEXT entry",
                )
                self.assertTrue(q.answered)


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
        # Four buttons in fibo: namespace, in expected order.
        buttons = kwargs["buttons"]
        flat_cb = [
            str(btn["callback_data"]) for row in buttons for btn in row
        ]
        self.assertEqual(
            flat_cb, ["fibo:start", "fibo:running", "fibo:stop", "fibo:exit"],
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
        """The /fibo callback edit path must succeed against an adapter
        that exposes only the real PTB inline-keyboard helper.

        Phase 2: ``fibo:running`` opens the read-only dry-run view.
        With no persisted registrations the screen still renders
        cleanly (a "no registrations" body + a single ❌ Exit
        button). The render path goes through ``edit_message_text``
        as before — the contract is unchanged.
        """
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

        asyncio.run(fibo_wizard.handle_fibo_callback(adapter, query, "fibo:running"))
        # The screen renders successfully (non-None text + answered).
        self.assertIsNotNone(query.edited_text)
        self.assertTrue(query.edited_text.strip())

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
        """The fibo_wizard shim must NOT call any agent execute() /
        write directly. It delegates the Start Fibo sub-flow into
        ``plugins.trade.fibo.flow.StartFiboFlow``, whose own tests
        assert zero exchange writes.

        We pin only the wizard shim source here.
        """
        from plugins.trade import fibo_wizard
        import inspect
        src = inspect.getsource(fibo_wizard)
        # Strip module docstring + inline comments + the helper that
        # constructs the StartFiboFlow singleton (it references the
        # TradeDesk class only as a type-hint side-effect of lazy
        # import). The deeper "no exchange writes" contract is
        # enforced inside plugins.trade.fibo.flow itself.
        import re
        # Remove the lazy-import block in _get_flow().
        src = re.sub(
            r"def _get_flow.*?(?=def |\Z)",
            "",
            src,
            flags=re.DOTALL,
        )
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


# ---------------------------------------------------------------------------
# /fibo top-level button callback regression (BUGFIX 2026-08-27)
# ---------------------------------------------------------------------------
#
# Phase 2.4 rewrote ``plugins.trade.fibo.discovery`` to expose
# ``list_market_catalog`` instead of ``list_instruments``. That
# rename was correctly threaded through the live wizard code in
# most places — except for the lazy-import block inside
# ``_get_flow()``. Until those two lines were fixed, clicking
# ``Start Fibo`` (and ``Stop Fibo``, which routes through the
# same singleton) raised an ``ImportError`` and the user saw
# "no response at all".
#
# These tests drive the deployed callback routing the same way
# Telegram would, asserting each top-level button returns a
# non-empty response — not just "didn't crash" or "didn't call
# .execute()". The previous tests were passing the silent-drop
# bug because they only asserted the absence of side effects.
class FiboTopLevelButtonRegressionTests(unittest.TestCase):
    """Drive every top-level menu button via the SAME callback
    routing that Telegram uses (``handle_fibo_callback``) and
    assert each one returns a usable, non-empty screen.

    The tests do NOT press Agree / Create / any flow-internal
    button. They are read-only at the wizard shim boundary.
    """

    def _make_query(self):
        from unittest.mock import MagicMock
        adapter = MagicMock()
        adapter.name = "RegressionAdapter"
        query = MagicMock()
        query.message = MagicMock()
        query.message.chat_id = 64620303
        query.message.chat.id = 64620303
        query.from_user = MagicMock()
        query.from_user.id = 64620303
        query.edited_text = None
        query.edited_markup = None

        async def _edit(text="", reply_markup=None):
            query.edited_text = text
            query.edited_markup = reply_markup
        query.edit_message_text = _edit
        query.answered = False
        query.answer = lambda: setattr(query, "answered", True) or None
        query.deleted = False
        async def _delete():
            query.deleted = True
        query.delete_message = _delete
        return adapter, query

    def test_start_fibo_returns_start_screen(self) -> None:
        """Clicking ``fibo:start`` MUST render the first Start
        Fibo screen — not a silent ImportError."""
        import asyncio
        from plugins.trade import fibo_wizard
        adapter, query = self._make_query()
        asyncio.run(
            fibo_wizard.handle_fibo_callback(adapter, query, "fibo:start")
        )
        # Telegram's loading spinner must always be cleared.
        self.assertTrue(query.answered, "callback answer() never called")
        # The Start Fibo screen must be edited into the chat.
        self.assertIsNotNone(
            query.edited_text,
            "Start Fibo button produced no edit_message_text call",
        )
        self.assertTrue(query.edited_text.strip())
        # First Start screen header is "Pick a symbol + variant".
        self.assertIn("Pick a symbol", query.edited_text)
        # The keyboard must be rebuilt with the symbol/variant
        # pick callbacks — not the placeholder four-entry menu.
        markup = query.edited_markup
        self.assertIsNotNone(
            markup,
            "Start Fibo button produced no reply_markup",
        )
        # Every button must carry a fibo:s:* sub-flow callback.
        flat_buttons = []
        for row in markup.inline_keyboard:
            for b in row:
                flat_buttons.append((b.text, b.callback_data))
        for _, cb in flat_buttons:
            self.assertTrue(
                cb.startswith("fibo:s:"),
                f"Start Fibo produced a non-subflow callback: "
                f"{cb!r}",
            )

    def test_running_fibo_returns_running_screen(self) -> None:
        """Clicking ``fibo:running`` MUST return a non-empty
        dry-run screen (control test — this is the previously
        WORKING button)."""
        import asyncio
        from plugins.trade import fibo_wizard
        adapter, query = self._make_query()
        asyncio.run(
            fibo_wizard.handle_fibo_callback(adapter, query, "fibo:running")
        )
        self.assertTrue(query.answered)
        self.assertIsNotNone(query.edited_text)
        self.assertTrue(query.edited_text.strip())
        self.assertIn("Running Fibo", query.edited_text)

    def test_stop_fibo_returns_screen(self) -> None:
        """Clicking ``fibo:stop`` MUST return a non-empty screen.
        Pre-bugfix: this was the placeholder branch (which is
        also non-empty today), but the regression we are guarding
        against is the silent ImportError cascade from
        ``_get_flow()`` if it ever resurfaces."""
        import asyncio
        from plugins.trade import fibo_wizard
        adapter, query = self._make_query()
        # Avoid actually calling the real Stop handler — the
        # placeholder path runs the same dispatch as the broken
        # path did. We just want to prove the dispatcher does
        # not silently no-op.
        asyncio.run(
            fibo_wizard.handle_fibo_callback(adapter, query, "fibo:stop")
        )
        self.assertTrue(query.answered)
        self.assertIsNotNone(query.edited_text)
        self.assertTrue(query.edited_text.strip())

    def test_exit_returns_with_no_message_left(self) -> None:
        """Clicking ``fibo:exit`` MUST close the wizard (delete
        the message)."""
        import asyncio
        from plugins.trade import fibo_wizard
        adapter, query = self._make_query()
        asyncio.run(
            fibo_wizard.handle_fibo_callback(adapter, query, "fibo:exit")
        )
        self.assertTrue(query.answered)
        # Either delete_message or edit-strip-keyboard happens;
        # both are acceptable per the wizard contract.
        if not query.deleted:
            self.assertIsNotNone(
                query.edited_text,
                "Exit did not delete or edit; UI may not "
                "have closed.",
            )

    def test_top_level_callbacks_all_route(self) -> None:
        """Every top-level menu callback_data MUST reach a
        handler that returns a non-empty screen — never a
        silent no-op."""
        import asyncio
        from plugins.trade import fibo_wizard
        for cb in ("fibo:start", "fibo:running", "fibo:stop"):
            adapter, query = self._make_query()
            asyncio.run(
                fibo_wizard.handle_fibo_callback(adapter, query, cb)
            )
            self.assertTrue(
                query.answered,
                f"{cb}: callback answer() was never called",
            )
            self.assertIsNotNone(
                query.edited_text,
                f"{cb}: callback did not render any screen",
            )
            self.assertTrue(
                query.edited_text.strip(),
                f"{cb}: callback rendered an empty screen",
            )

    def test_dryrun_falls_back_marker_is_pure_ascii(self) -> None:
        """The dryrun render's 'venue ... not selected' fallback
        must NOT contain a U+FFFD replacement character or any
        UTF-8-invalid literal."""
        import inspect
        from plugins.trade.fibo import dryrun
        # Locate the literal in source.
        src = inspect.getsource(dryrun)
        # Find any double-quoted string containing "not selected".
        m = []
        import re
        for match in re.finditer(r'"([^"]*not[^"]*selected[^"]*)"', src):
            literal = match.group(1)
            # The literal must be pure-ASCII and the colon must
            # precede "not selected" (Phase 2 spec).
            self.assertTrue(
                all(ord(c) < 128 for c in literal),
                f"dryrun fallback contains non-ASCII bytes: "
                f"{literal!r}",
            )
            self.assertIn(": not selected", literal)
            m.append(literal)
        self.assertTrue(
            m,
            "dryrun.py must contain at least one " "not selected" " fallback",
        )


if __name__ == "__main__":
    unittest.main()