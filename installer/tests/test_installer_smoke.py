"""Real-script fresh-root smoke test driver.

Invokes the actual ./install.sh, ./verify.sh, ./uninstall.sh wrapper
scripts against an isolated HERMES_HOME in a temp directory.

Each scenario uses a fresh isolated root. The driver captures stdout/stderr
of each script invocation and asserts on the final filesystem layout.

This is the FINAL pre-merge gate per the modular-installer plan.

Matrix:
  Test A — TRADE ONLY
  Test B — FIBO ONLY
  Test C — BOTH
  Test D — ADDITIVE (--trade then --fibo, and --fibo then --trade)
  Test E — PARTIAL UNINSTALL (uninstall --trade from both, uninstall --fibo from both)
  Test F — LEGACY MIGRATION SMOKE (creates ~/.hermes/.kam-trade/, runs installer)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # <repo>
INSTALL_SH = REPO_ROOT / "install.sh"
VERIFY_SH = REPO_ROOT / "verify.sh"
UNINSTALL_SH = REPO_ROOT / "uninstall.sh"

# Subset of capability-scoped paths used in assertions.
SHARED_AGENT_NAMES = [
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
SHARED_FILES = [
    "agents/__init__.py",
    "canonical.py",
    "tradedesk.py",
    "__init__.py",
]
SHARED_PATHS = [f"agents/{n}" for n in SHARED_AGENT_NAMES] + SHARED_FILES

TRADE_ONLY_PATHS = ["wizard.py"]
FIBO_ONLY_PATHS = [
    "fibo_service.py",
    "fibo_daemon.py",
    "fibo_wizard.py",
    "golden_fibo/__init__.py",
    "golden_fibo/config.py",
    "golden_fibo/engine.py",
    "golden_fibo/lighter_adapter.py",
    "golden_fibo/preflight.py",
    "golden_fibo/state.py",
]


def _run(args: List[str], env: Dict[str, str], timeout: int = 300) -> Tuple[int, str, str]:
    """Run a shell script with the given args and env. Returns (rc, stdout, stderr)."""
    proc = subprocess.run(
        [str(INSTALL_SH.parent / Path(args[0]).name)] + args[1:] if False else args,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )
    return proc.returncode, proc.stdout, proc.stderr


def _run_script(script: Path, args: List[str], hermes_home: Path) -> Tuple[int, str, str]:
    """Invoke the wrapper script with HERMES_HOME overridden."""
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    proc = subprocess.run(
        [str(script)] + args,
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
        cwd=str(REPO_ROOT),
    )
    return proc.returncode, proc.stdout, proc.stderr


def _make_fresh_root() -> Tuple[Path, Path]:
    """Create a fresh isolated Hermes home and checkout. Returns (hermes_home, hermes_root)."""
    tmp = Path(tempfile.mkdtemp(prefix="kam-smoke-"))
    hermes_home = tmp / "hermes_home"
    hermes_home.mkdir(parents=True, exist_ok=True)
    hermes_root = hermes_home.parent  # installer writes to hermes_root/plugins/trade
    (hermes_root / "plugins").mkdir(exist_ok=True)
    return hermes_home, hermes_root


def _cleanup(tmp: Path) -> None:
    shutil.rmtree(tmp, ignore_errors=True)


def _load_manifest(hermes_home: Path) -> Dict[str, Any]:
    p = hermes_home / "kam" / "install_state.json"
    if not p.is_file():
        return {}
    return json.loads(p.read_text())


def _installed_plugin_paths(hermes_root: Path) -> List[str]:
    """List all paths under plugins/trade/ that exist after install."""
    pt = hermes_root / "plugins" / "trade"
    if not pt.is_dir():
        return []
    out = []
    for p in sorted(pt.rglob("*")):
        if p.is_file() and p.suffix == ".py":
            out.append(str(p.relative_to(pt)))
    return out


def _registered_commands(hermes_home: Path) -> List[str]:
    """Invoke plugins/trade/__init__.registered_commands() under the test HERMES_HOME."""
    import importlib.util
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    code = (
        "import importlib.util, json, os, sys\n"
        f"sys.path.insert(0, '{REPO_ROOT}')\n"
        f"spec = importlib.util.spec_from_file_location('plugins.trade.__init__', '{REPO_ROOT}/plugins/trade/__init__.py')\n"
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
        "hh = os.environ.get('HERMES_HOME', 'NONE')\n"
        "mp = os.path.join(hh, 'kam', 'install_state.json')\n"
        "exists = os.path.exists(mp)\n"
        "content = json.load(open(mp)) if exists else None\n"
        "print(json.dumps({'cmds': mod.registered_commands(), 'hh': hh, 'manifest_path': mp, 'exists': exists, 'caps': (content or {}).get('capabilities')}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, timeout=30,
    )
    if proc.returncode != 0:
        return []
    try:
        out = json.loads((proc.stdout or "{}").strip() or "{}")
    except json.JSONDecodeError:
        print(f"_registered_commands bad json: {proc.stdout!r}")
        return []
    return out.get('cmds', [])


class _FreshRoot(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="kam-smoke-"))
        self.hermes_home, self.hermes_root = _make_fresh_root()
        # self._tmp is the parent of hermes_home; hermes_root = self._tmp

    def tearDown(self) -> None:
        _cleanup(self._tmp)


class TestATradeOnly(_FreshRoot):
    """Test A — TRADE ONLY."""

    def test_A_trade_only(self):
        # Install --trade
        rc, out, err = _run_script(INSTALL_SH, ["--trade"], self.hermes_home)
        self.assertEqual(rc, 0, f"install --trade failed: {out}\n{err}")
        # Manifest
        m = _load_manifest(self.hermes_home)
        self.assertTrue(m["capabilities"]["trade"])
        self.assertFalse(m["capabilities"]["fibo"])
        # Trade folder exists; fibo folder absent
        self.assertTrue((self.hermes_home / "trade").is_dir())
        self.assertFalse((self.hermes_home / "fibo").is_dir())
        # Plugin files
        paths = _installed_plugin_paths(self.hermes_root)
        # Shared present (one copy)
        for sp in SHARED_PATHS:
            self.assertIn(sp, paths, f"missing shared path {sp}")
        # Trade wizard present
        self.assertIn("wizard.py", paths)
        # NO fibo files
        for fp in FIBO_ONLY_PATHS:
            self.assertNotIn(fp, paths, f"trade-only should NOT contain {fp}")
        # Exactly one copy of each shared agent (no duplicates)
        for n in SHARED_AGENT_NAMES:
            matches = [p for p in paths if p.endswith(f"agents/{n}")]
            self.assertEqual(len(matches), 1, f"duplicate shared agent {n}")
        # Registered commands
        cmds = _registered_commands(self.hermes_home)
        self.assertEqual(cmds, ["trade"])
        # Verify
        rc, out, err = _run_script(VERIFY_SH, ["--trade"], self.hermes_home)
        self.assertEqual(rc, 0, f"verify --trade failed: {out}\n{err}")
        # fibo.service absent (no /etc/systemd/system/fibo.service on this test root;
        # the modular installer only writes it when --fibo. On this machine
        # we don't touch the real systemd dir).
        # Uninstall --trade (last capability; should remove shared too)
        rc, out, err = _run_script(UNINSTALL_SH, ["--trade"], self.hermes_home)
        self.assertEqual(rc, 0, f"uninstall --trade failed: {out}\n{err}")
        # Trade folder gone, kam/ gone
        self.assertFalse((self.hermes_home / "trade").is_dir())
        self.assertFalse((self.hermes_home / "kam").is_dir())


class TestBFiboOnly(_FreshRoot):
    """Test B — FIBO ONLY."""

    def test_B_fibo_only(self):
        rc, out, err = _run_script(INSTALL_SH, ["--fibo"], self.hermes_home)
        self.assertEqual(rc, 0, f"install --fibo failed: {out}\n{err}")
        m = _load_manifest(self.hermes_home)
        self.assertFalse(m["capabilities"]["trade"])
        self.assertTrue(m["capabilities"]["fibo"])
        self.assertTrue((self.hermes_home / "fibo").is_dir())
        self.assertFalse((self.hermes_home / "trade").is_dir())
        paths = _installed_plugin_paths(self.hermes_root)
        # Shared present
        for sp in SHARED_PATHS:
            self.assertIn(sp, paths, f"missing shared path {sp}")
        # fibo present
        for fp in FIBO_ONLY_PATHS:
            self.assertIn(fp, paths, f"missing fibo path {fp}")
        # NO trade wizard
        self.assertNotIn("wizard.py", paths, "fibo-only should NOT contain wizard.py")
        cmds = _registered_commands(self.hermes_home)
        self.assertEqual(cmds, ["fibo"])
        rc, out, err = _run_script(VERIFY_SH, ["--fibo"], self.hermes_home)
        self.assertEqual(rc, 0, f"verify --fibo failed: {out}\n{err}")
        # Uninstall --fibo
        rc, out, err = _run_script(UNINSTALL_SH, ["--fibo"], self.hermes_home)
        self.assertEqual(rc, 0, f"uninstall --fibo failed: {out}\n{err}")
        self.assertFalse((self.hermes_home / "fibo").is_dir())
        self.assertFalse((self.hermes_home / "kam").is_dir())


class TestCBoth(_FreshRoot):
    """Test C — BOTH."""

    def test_C_both(self):
        rc, out, err = _run_script(INSTALL_SH, ["--trade", "--fibo"], self.hermes_home)
        self.assertEqual(rc, 0, f"install both failed: {out}\n{err}")
        m = _load_manifest(self.hermes_home)
        self.assertTrue(m["capabilities"]["trade"])
        self.assertTrue(m["capabilities"]["fibo"])
        self.assertTrue((self.hermes_home / "trade").is_dir())
        self.assertTrue((self.hermes_home / "fibo").is_dir())
        paths = _installed_plugin_paths(self.hermes_root)
        for sp in SHARED_PATHS:
            self.assertIn(sp, paths)
        self.assertIn("wizard.py", paths)
        for fp in FIBO_ONLY_PATHS:
            self.assertIn(fp, paths)
        # Exactly one copy of each shared agent
        for n in SHARED_AGENT_NAMES:
            matches = [p for p in paths if p.endswith(f"agents/{n}")]
            self.assertEqual(len(matches), 1, f"duplicate shared agent {n}")
        cmds = _registered_commands(self.hermes_home)
        self.assertEqual(sorted(cmds), ["fibo", "trade"])
        rc, out, err = _run_script(VERIFY_SH, ["--trade", "--fibo"], self.hermes_home)
        self.assertEqual(rc, 0, f"verify both failed: {out}\n{err}")


class TestDAdditive(unittest.TestCase):
    """Test D — ADDITIVE installs."""

    def _fresh(self) -> Tuple[Path, Path]:
        tmp = Path(tempfile.mkdtemp(prefix="kam-add-"))
        self.addCleanup(_cleanup, tmp)
        hermes_home = tmp / "hermes_home"
        hermes_home.mkdir(parents=True, exist_ok=True)
        hermes_root = hermes_home.parent
        (hermes_root / "plugins").mkdir(exist_ok=True)
        return hermes_home, hermes_root

    def test_D_trade_then_fibo(self):
        hermes_home, hermes_root = self._fresh()
        rc, _, _ = _run_script(INSTALL_SH, ["--trade"], hermes_home)
        self.assertEqual(rc, 0)
        # After install --trade only: commands = ["trade"]
        self.assertEqual(_registered_commands(hermes_home), ["trade"])
        paths_after_trade = _installed_plugin_paths(hermes_root)
        rc, _, _ = _run_script(INSTALL_SH, ["--fibo"], hermes_home)
        self.assertEqual(rc, 0)
        paths_after_both = _installed_plugin_paths(hermes_root)
        # fibo files added
        for fp in FIBO_ONLY_PATHS:
            self.assertNotIn(fp, paths_after_trade)
            self.assertIn(fp, paths_after_both)
        # Trade wizard preserved
        self.assertIn("wizard.py", paths_after_trade)
        self.assertIn("wizard.py", paths_after_both)
        # After install --fibo (additive): both commands
        rc, _, _ = _run_script(VERIFY_SH, ["--trade", "--fibo"], hermes_home)
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(_registered_commands(hermes_home)), ["fibo", "trade"])

    def test_D_fibo_then_trade(self):
        hermes_home, hermes_root = self._fresh()
        rc, _, _ = _run_script(INSTALL_SH, ["--fibo"], hermes_home)
        self.assertEqual(rc, 0)
        self.assertEqual(_registered_commands(hermes_home), ["fibo"])
        paths_after_fibo = _installed_plugin_paths(hermes_root)
        rc, _, _ = _run_script(INSTALL_SH, ["--trade"], hermes_home)
        self.assertEqual(rc, 0)
        paths_after_both = _installed_plugin_paths(hermes_root)
        # Trade wizard added
        self.assertNotIn("wizard.py", paths_after_fibo)
        self.assertIn("wizard.py", paths_after_both)
        # fibo preserved
        for fp in FIBO_ONLY_PATHS:
            self.assertIn(fp, paths_after_fibo)
            self.assertIn(fp, paths_after_both)
        rc, _, _ = _run_script(VERIFY_SH, ["--trade", "--fibo"], hermes_home)
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(_registered_commands(hermes_home)), ["fibo", "trade"])


class TestEPartialUninstall(unittest.TestCase):
    """Test E — PARTIAL UNINSTALL."""

    def _fresh(self) -> Tuple[Path, Path]:
        tmp = Path(tempfile.mkdtemp(prefix="kam-pun-"))
        self.addCleanup(_cleanup, tmp)
        hermes_home = tmp / "hermes_home"
        hermes_home.mkdir(parents=True, exist_ok=True)
        hermes_root = hermes_home.parent
        (hermes_root / "plugins").mkdir(exist_ok=True)
        return hermes_home, hermes_root

    def test_E_uninstall_trade_keeps_fibo(self):
        hermes_home, hermes_root = self._fresh()
        rc, _, _ = _run_script(INSTALL_SH, ["--trade", "--fibo"], hermes_home)
        self.assertEqual(rc, 0)
        # Uninstall trade
        rc, _, _ = _run_script(UNINSTALL_SH, ["--trade"], hermes_home)
        self.assertEqual(rc, 0)
        m = _load_manifest(hermes_home)
        self.assertFalse(m["capabilities"]["trade"])
        self.assertTrue(m["capabilities"]["fibo"])
        self.assertFalse((hermes_home / "trade").is_dir())
        self.assertTrue((hermes_home / "fibo").is_dir())
        self.assertTrue((hermes_home / "kam").is_dir())  # shared kam/ preserved
        paths = _installed_plugin_paths(hermes_root)
        # fibo files preserved
        for fp in FIBO_ONLY_PATHS:
            self.assertIn(fp, paths, f"fibo-only file {fp} should remain after uninstall --trade")
        # Trade wizard removed
        self.assertNotIn("wizard.py", paths)
        # shared preserved
        for sp in SHARED_PATHS:
            self.assertIn(sp, paths)
        # Command registration: only /fibo remains
        self.assertEqual(_registered_commands(hermes_home), ["fibo"])

    def test_E_uninstall_fibo_keeps_trade(self):
        hermes_home, hermes_root = self._fresh()
        rc, _, _ = _run_script(INSTALL_SH, ["--trade", "--fibo"], hermes_home)
        self.assertEqual(rc, 0)
        # Uninstall fibo
        rc, _, _ = _run_script(UNINSTALL_SH, ["--fibo"], hermes_home)
        self.assertEqual(rc, 0)
        m = _load_manifest(hermes_home)
        self.assertTrue(m["capabilities"]["trade"])
        self.assertFalse(m["capabilities"]["fibo"])
        self.assertTrue((hermes_home / "trade").is_dir())
        self.assertFalse((hermes_home / "fibo").is_dir())
        self.assertTrue((hermes_home / "kam").is_dir())
        paths = _installed_plugin_paths(hermes_root)
        # Trade wizard preserved
        self.assertIn("wizard.py", paths)
        # fibo files removed
        for fp in FIBO_ONLY_PATHS:
            self.assertNotIn(fp, paths, f"fibo file {fp} should be removed after uninstall --fibo")
        # shared preserved (trade still needs it)
        for sp in SHARED_PATHS:
            self.assertIn(sp, paths)
        # Command registration: only /trade remains
        self.assertEqual(_registered_commands(hermes_home), ["trade"])


class TestFLegacyMigration(unittest.TestCase):
    """Test F — Legacy .kam-trade/ migration."""

    def _fresh(self) -> Tuple[Path, Path]:
        tmp = Path(tempfile.mkdtemp(prefix="kam-leg-"))
        self.addCleanup(_cleanup, tmp)
        hermes_home = tmp / "hermes_home"
        hermes_home.mkdir(parents=True, exist_ok=True)
        hermes_root = hermes_home.parent
        (hermes_root / "plugins").mkdir(exist_ok=True)
        return hermes_home, hermes_root

    def test_F_legacy_kam_trade_migrated_safely(self):
        hermes_home, hermes_root = self._fresh()
        # Create realistic legacy .kam-trade/ with installer metadata.
        legacy_dir = hermes_home / ".kam-trade"
        legacy_dir.mkdir(parents=True)
        legacy_manifest = {
            "kam_version": "1.0.0",
            "installer_version": "1.0.0",
            "timestamp": "2026-08-18T00:00:00Z",
            "hermes_root": str(hermes_root),
            "compatible_hermes": "0.20.0",
            "copied_files": ["/some/path/file.py"],
            # NO credentials, just installer metadata
        }
        (legacy_dir / "manifest.json").write_text(json.dumps(legacy_manifest))
        backups = legacy_dir / "backups" / "20260818T000000Z"
        backups.mkdir(parents=True)
        (backups / "dummy.txt").write_text("legacy backup content")
        # Run install --trade
        rc, _, _ = _run_script(INSTALL_SH, ["--trade"], hermes_home)
        self.assertEqual(rc, 0)
        # .kam-trade must be retired (renamed, not deleted silently).
        # The migration moves it to .kam-trade-retired-<ts>/.
        retired = list(hermes_home.glob(".kam-trade-retired-*"))
        self.assertGreaterEqual(len(retired), 1, "legacy .kam-trade must be retired, not deleted")
        # Trade namespace created.
        self.assertTrue((hermes_home / "trade").is_dir())
        # Manifest doesn't contain secrets.
        m = _load_manifest(hermes_home)
        self.assertNotIn("api_key", json.dumps(m).lower())
        self.assertNotIn("secret", json.dumps(m).lower())
        # Legacy fibo artifacts must NOT be auto-claimed just because they exist
        # (this test doesn't create fibo artifacts, so fibo must remain absent).
        self.assertFalse(m["capabilities"]["fibo"])
        self.assertFalse((hermes_home / "fibo").is_dir())


if __name__ == "__main__":
    unittest.main()