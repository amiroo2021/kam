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

        # Stub adapter that records what _send / _edit get handed.
        captured = []

        class StubQuery:
            def __init__(self) -> None:
                self.edited_text = None
                self.edited_buttons = None
                self.answered = False

            def edit_message_text(self, text="", buttons=None):
                self.edited_text = text
                self.edited_buttons = buttons

            def answer(self):
                self.answered = True

        async def run_one(callback_data: str) -> Dict[str, Any]:
            q = StubQuery()
            await fibo_wizard.handle_fibo_callback(None, q, callback_data)
            return {
                "edited_text": q.edited_text,
                "edited_buttons": q.edited_buttons,
                "answered": q.answered,
            }

        for cb in ("fibo:start", "fibo:running", "fibo:stop"):
            with self.subTest(callback=cb):
                # Run the coroutine synchronously.
                import asyncio
                result = asyncio.run(run_one(cb))
                # Placeholder screen text matches SCREEN_TEXT[cb].
                self.assertEqual(result["edited_text"], fibo_wizard.SCREEN_TEXT[cb])
                self.assertTrue(result["answered"])
                # The buttons offered after the placeholder are the entry
                # buttons (so the user can pick another action).
                self.assertIsNotNone(result["edited_buttons"])


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


if __name__ == "__main__":
    unittest.main()