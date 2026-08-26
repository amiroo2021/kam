"""Clean-install integration test for the --fibo capability.

Phase1 invariant #3:

    ./install.sh --fibo against a synthetic Hermes root must:
      * install fibo_wizard.py
      * install all 7 plugins/trade/fibo/*.py files
      * install the required shared TradeDesk/agents/discovery files
      * NOT install the trade wizard
      * NOT install fibo.service / fibo_daemon / fibo_service / golden_fibo
      * all new Fibo modules must import successfully from the
        installed tree

    ./verify.sh --fibo against the same tree must succeed.

    ./uninstall.sh --fibo (or the equivalent uninstall capability
    module) must:
      * remove the Fibo source capability files
      * preserve shared files needed by other capabilities
      * NOT delete ~/.hermes/fibo/ runtime/cache/registration data
        (those are user data)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # /root/kam


def _hermes_python() -> str:
    """Resolve the python interpreter that matches the live Hermes venv.

    The install/verify scripts use ``/usr/local/lib/hermes-agent/venv/bin/python``
    if available. For this test we use the active Python (matching the
    project's test environment) — same major version, same stdlib, so
    the install logic runs identically.
    """
    return sys.executable


def _ensure_fiboskeleton_module() -> None:
    """Import the fibo module from the repo source so the install's
    import-list (which validates the install) works without depending
    on a live Hermes tree."""
    sys.path.insert(0, str(REPO_ROOT))


class CleanFiboInstallTests(unittest.TestCase):
    """Phase1 invariant #3: clean --fibo install on a synthetic tree."""

    def setUp(self) -> None:
        _ensure_fiboskeleton_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "hermes-agent"
        self.hermes_home = Path(self.tmp.name) / "hermes-home"
        self.root.mkdir()
        self.hermes_home.mkdir()
        # Pre-create kam/ so installer can write install_state.json
        (self.hermes_home / "kam").mkdir()
        # The verifier requires a Telegram adapter and a config.yaml
        # to pass adapter_wiring + command_menu publication checks.
        # The live Hermes install ships those; for the synthetic tree
        # we copy the real files from the live install (read-only) so
        # the verifier's checks fire against real wiring rather than
        # stub-mocked paths. HERMES_HOME may be overridden via env for
        # non-standard layouts (CI, etc.).
        live_root = Path(os.environ.get("KAM_TEST_HERMES_ROOT", "/usr/local/lib/hermes-agent"))
        live_home = Path(os.environ.get("HERMES_HOME", "/root/.hermes"))
        adapter_src = live_root / "plugins" / "platforms" / "telegram" / "adapter.py"
        if adapter_src.is_file():
            adapter_dst = self.root / "plugins" / "platforms" / "telegram"
            adapter_dst.mkdir(parents=True, exist_ok=True)
            adapter_dst.joinpath("adapter.py").write_bytes(
                adapter_src.read_bytes()
            )
        live_config = live_home / "config.yaml"
        if live_config.is_file():
            # Copy the live config.yaml so publish_BotCommands finds it.
            self.hermes_home.joinpath("config.yaml").write_bytes(
                live_config.read_bytes()
            )

    def _run_installer(self, *args: str) -> subprocess.CompletedProcess:
        cmd = [
            _hermes_python(),
            str(REPO_ROOT / "installer" / "installer.py"),
            "--action", "install",
            "--hermes-root", str(self.root),
            "--hermes-home", str(self.hermes_home),
            *args,
        ]
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )

    def _run_verify(self, *args: str) -> subprocess.CompletedProcess:
        cmd = [
            _hermes_python(),
            str(REPO_ROOT / "installer" / "installer.py"),
            "--action", "verify",
            "--hermes-root", str(self.root),
            "--hermes-home", str(self.hermes_home),
            *args,
        ]
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )

    def _run_uninstall(self, *args: str) -> subprocess.CompletedProcess:
        cmd = [
            _hermes_python(),
            str(REPO_ROOT / "installer" / "installer.py"),
            "--action", "uninstall",
            "--hermes-root", str(self.root),
            "--hermes-home", str(self.hermes_home),
            *args,
        ]
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )

    # ---- clean --fibo install ----

    def test_clean_fibo_install_succeeds(self) -> None:
        result = self._run_installer("--fibo")
        self.assertEqual(
            result.returncode, 0,
            msg=f"install --fibo failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )

    def test_clean_fibo_install_copies_fibo_wizard(self) -> None:
        self._run_installer("--fibo")
        self.assertTrue(
            (self.root / "plugins" / "trade" / "fibo_wizard.py").is_file(),
            "fibo_wizard.py was not installed",
        )

    def test_clean_fibo_install_copies_all_seven_fibo_subpackage_files(self) -> None:
        self._run_installer("--fibo")
        sub = self.root / "plugins" / "trade" / "fibo"
        self.assertTrue(sub.is_dir(), f"{sub} missing after install")
        for fname in (
            "__init__.py",
            "_atomic.py",
            "snapshot.py",
            "store.py",
            "session.py",
            "flow.py",
            "mt4_reader.py",
        ):
            fpath = sub / fname
            self.assertTrue(
                fpath.is_file(),
                f"fibo/{fname} was not installed",
            )

    def test_clean_fibo_install_copies_shared_tradedesk_and_agents(self) -> None:
        self._run_installer("--fibo")
        shared = [
            self.root / "plugins" / "trade" / "tradedesk.py",
            self.root / "plugins" / "trade" / "canonical.py",
            self.root / "plugins" / "trade" / "__init__.py",
        ]
        agents_dir = self.root / "plugins" / "trade" / "agents"
        for p in shared:
            self.assertTrue(p.is_file(), f"shared file missing: {p}")
        self.assertTrue(agents_dir.is_dir(), f"agents/ missing: {agents_dir}")
        # At least the 10 known agents are installed.
        for agent in (
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
        ):
            self.assertTrue(
                (agents_dir / agent).is_file(),
                f"shared agent missing: {agent}",
            )

    def test_clean_fibo_install_does_not_install_trade_wizard(self) -> None:
        self._run_installer("--fibo")
        # wizard.py is owned by the /trade capability. A --fibo-only
        # install must NOT copy it.
        self.assertFalse(
            (self.root / "plugins" / "trade" / "wizard.py").is_file(),
            "/trade wizard.py was installed by --fibo (it should not be)",
        )

    def test_clean_fibo_install_does_not_install_old_runtime(self) -> None:
        self._run_installer("--fibo")
        forbidden = [
            self.root / "plugins" / "trade" / "fibo_daemon.py",
            self.root / "plugins" / "trade" / "fibo_service.py",
            self.root / "plugins" / "trade" / "golden_fibo",
            self.root / "fibo.service",
            self.root / "fibo.service.template",
            self.root / "plugins" / "trade" / "fibo_unit.py",
        ]
        for p in forbidden:
            self.assertFalse(
                p.exists(),
                f"forbidden runtime artifact present: {p}",
            )

    def test_clean_fibo_install_modules_import_successfully(self) -> None:
        """After install, all new Fibo modules must import cleanly from
        the installed tree when added to sys.path."""
        self._run_installer("--fibo")
        # Drop the repo source from sys.path so we exercise the
        # installed files (this proves the installer copied them
        # verbatim and they are self-sufficient).
        repo_path = str(REPO_ROOT)
        saved = [p for p in sys.path if p == repo_path]
        for p in saved:
            sys.path.remove(p)
        # Add the installed tree.
        installed_root = str(self.root)
        sys.path.insert(0, installed_root)
        try:
            from plugins.trade import fibo_wizard  # noqa: F401
            from plugins.trade.fibo import (
                __init__ as fibo_pkg,  # noqa: F401
            )
            from plugins.trade.fibo import snapshot, store, session, flow
            from plugins.trade.fibo import mt4_reader
            from plugins.trade.fibo import _atomic
            # Sanity: module has the expected public surface.
            self.assertTrue(hasattr(flow, "StartFiboFlow"))
            self.assertTrue(hasattr(snapshot, "Mt4SnapshotStore"))
            self.assertTrue(hasattr(store, "FiboRegistrationStore"))
            self.assertTrue(hasattr(session, "FiboSessionStore"))
            self.assertTrue(hasattr(mt4_reader, "Mt4ReaderProcess"))
            self.assertTrue(hasattr(_atomic, "atomic_write_bytes"))
        finally:
            for p in saved:
                sys.path.insert(0, p)
            try:
                sys.path.remove(installed_root)
            except ValueError:
                pass

    # ---- verify ----

    def test_clean_fibo_verify_succeeds(self) -> None:
        self._run_installer("--fibo")
        result = self._run_verify("--fibo")
        self.assertEqual(
            result.returncode, 0,
            msg=f"verify --fibo failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )

    # ---- uninstall ----

    def test_clean_fibo_uninstall_removes_capability_files(self) -> None:
        self._run_installer("--fibo")
        result = self._run_uninstall("--fibo")
        self.assertEqual(
            result.returncode, 0,
            msg=f"uninstall --fibo failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        # All fibo capability files are removed.
        self.assertFalse(
            (self.root / "plugins" / "trade" / "fibo_wizard.py").exists(),
            "fibo_wizard.py was not removed on uninstall",
        )
        sub = self.root / "plugins" / "trade" / "fibo"
        if sub.exists():
            # If the directory is left behind, it must be empty.
            leftover = list(sub.glob("*"))
            self.assertEqual(
                leftover, [],
                f"fibo/ subdir left non-empty after uninstall: {leftover}",
            )

    def test_clean_fibo_uninstall_preserves_shared_files(self) -> None:
        """Spec invariant #3: --fibo uninstall must preserve shared
        files needed by other capabilities. When --fibo is the ONLY
        installed capability, the installer also tears the shared
        layer (``plugins/trade/__init__.py``, ``tradedesk.py``,
        ``canonical.py``, ``agents/``). The assertion here is therefore:

          * when --trade and --fibo are both installed, uninstalling
            --fibo preserves ``plugins/trade/__init__.py``,
            ``tradedesk.py``, ``canonical.py``, and all agents.

          * when --fibo is the only capability, the shared layer is
            cleanly removed; a re-install of --fibo or --trade must
            succeed.

        We exercise the first case (the spec's actual safety
        invariant) by installing both capabilities then uninstalling
        only --fibo.
        """
        self._run_installer("--fibo")
        # Now also install --trade so both capabilities are present.
        result = self._run_installer("--trade")
        self.assertEqual(
            result.returncode, 0,
            msg=f"install --trade failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        # Uninstall only --fibo. Trade remains, so shared layer is kept.
        result = self._run_uninstall("--fibo")
        self.assertEqual(
            result.returncode, 0,
            msg=f"uninstall --fibo failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        # Shared files needed by /trade remain.
        for rel in (
            "plugins/trade/__init__.py",
            "plugins/trade/tradedesk.py",
            "plugins/trade/canonical.py",
            "plugins/trade/wizard.py",  # /trade's own wizard
            "plugins/trade/agents/x_hyperliquid_agent.py",
        ):
            self.assertTrue(
                (self.root / rel).is_file(),
                f"shared file removed despite /trade still installed: {rel}",
            )
        # /fibo capability files are gone.
        self.assertFalse(
            (self.root / "plugins" / "trade" / "fibo_wizard.py").exists(),
            "fibo_wizard.py was not removed",
        )

    def test_clean_fibo_uninstall_does_not_delete_user_data(self) -> None:
        """Phase1 invariant: uninstall must NOT touch ~/.hermes/fibo/
        (snapshot, reader state, registrations are user data)."""
        # First install.
        self._run_installer("--fibo")
        # Drop a fake user-data file at the standard hermes-home path.
        fibo_data = self.hermes_home / "fibo"
        fibo_data.mkdir(parents=True, exist_ok=True)
        user_snapshot = fibo_data / "mt4_snapshot.json"
        sentinel = "USER_DATA_SENTINEL_NOT_TOUCHED_BY_UNINSTALL"
        user_snapshot.write_text(json.dumps({"sentinel": sentinel}))
        user_state = fibo_data / "mt4_reader_state.json"
        user_state.write_text(json.dumps({"last_update_id": 42}))
        user_regs = fibo_data / "registrations.jsonl"
        user_regs.write_text("USER_REGISTRATIONS_NOT_TOUCHED_BY_UNINSTALL\n")
        # Now uninstall.
        result = self._run_uninstall("--fibo")
        self.assertEqual(
            result.returncode, 0,
            msg=f"uninstall --fibo failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        # All user-data files must still exist with their original
        # content.
        self.assertTrue(user_snapshot.exists())
        self.assertTrue(user_state.exists())
        self.assertTrue(user_regs.exists())
        self.assertEqual(
            json.loads(user_snapshot.read_text()).get("sentinel"),
            sentinel,
            "user snapshot was modified by uninstall",
        )
        self.assertEqual(
            user_state.read_text(),
            json.dumps({"last_update_id": 42}),
            "user reader state was modified by uninstall",
        )
        self.assertEqual(
            user_regs.read_text(),
            "USER_REGISTRATIONS_NOT_TOUCHED_BY_UNINSTALL\n",
            "user registrations were modified by uninstall",
        )


if __name__ == "__main__":
    unittest.main()