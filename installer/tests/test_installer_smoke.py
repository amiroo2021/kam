"""Real-script fresh-root smoke tests (post root-fix).

Invokes the actual ./install.sh, ./verify.sh, ./uninstall.sh wrapper
scripts against fresh isolated ``HERMES_ROOT`` and ``HERMES_HOME`` in
temp directories.

The fix enforces:
  - explicit --hermes-root (default /usr/local/lib/hermes-agent)
  - explicit --hermes-home (default ~/.hermes)
  - --help prints usage and exits 0, zero mutation
  - unknown flags exit 2 with zero mutation
  - --dry-run reports actions without writing

Each test uses a fresh isolated root pair. The driver captures
stdout/stderr and asserts on the final filesystem layout.

Matrix (A matches the user's gate requirements; B–J are new):
  A. fresh --trade
  B. legacy .kam-trade/ migration
  C. --help: zero mutations, exit 0
  D. unknown flag: exit 2, zero mutations
  E. --dry-run: zero mutations, prints plan
  F. explicit --hermes-root routes files to that root
  G. hermes-root + isolated hermes-home go to two separate trees
  H. uninstall uses the same explicit hermes-root
  I. verify uses the same explicit hermes-root
  J. additive installs with explicit root are idempotent
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
from typing import Any, Dict, List, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"
VERIFY_SH = REPO_ROOT / "verify.sh"
UNINSTALL_SH = REPO_ROOT / "uninstall.sh"

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


def _run_script(
    script: Path, args: List[str], hermes_root: Path, hermes_home: Path
) -> Tuple[int, str, str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    # Fresh-root smoke tests validate file payload + wiring, not pip/SDK
    # installs (those need a real Hermes venv). Always pass --skip-deps for
    # install.sh invocations unless the caller already set it.
    final_args = list(args)
    if script.name == "install.sh" and "--skip-deps" not in final_args:
        final_args.append("--skip-deps")
    # Isolate systemd unit writes under the fixture root.
    systemd_dir = hermes_root.parent / "systemd"
    systemd_dir.mkdir(parents=True, exist_ok=True)
    if "--systemd-dir" not in final_args:
        final_args.extend(["--systemd-dir", str(systemd_dir)])
    if script.name in ("install.sh", "uninstall.sh") and "--no-restart" not in final_args:
        final_args.append("--no-restart")
    proc = subprocess.run(
        [str(script)] + final_args + ["--hermes-root", str(hermes_root), "--hermes-home", str(hermes_home)],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
        cwd=str(REPO_ROOT),
    )
    return proc.returncode, proc.stdout, proc.stderr


def _make_fresh_root_pair() -> Tuple[Path, Path, Path]:
    """Return (tmpdir, hermes_root, hermes_home) for a fresh isolated run.

    Plants a pristine Telegram adapter (no /trade seams) so the modular
    installer can apply capability-aware dispatch wiring the same way it
    must on a genuine fresh Hermes install (Lodo contract).
    """
    tmp = Path(tempfile.mkdtemp(prefix="kam-smoke-"))
    hermes_root = tmp / "hermes_root"
    hermes_home = tmp / "hermes_home"
    hermes_root.mkdir(parents=True, exist_ok=True)
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_root / "plugins").mkdir(exist_ok=True)
    # Pristine adapter + minimal config for plugins.enabled.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pristine_telegram_adapter import PRISTINE_TELEGRAM_ADAPTER  # noqa: WPS433

    adapter = hermes_root / "plugins" / "platforms" / "telegram" / "adapter.py"
    adapter.parent.mkdir(parents=True, exist_ok=True)
    adapter.write_text(PRISTINE_TELEGRAM_ADAPTER, encoding="utf-8")
    (hermes_home / "config.yaml").write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    return tmp, hermes_root, hermes_home


def _load_manifest(hermes_home: Path) -> Dict[str, Any]:
    p = hermes_home / "kam" / "install_state.json"
    if not p.is_file():
        return {}
    return json.loads(p.read_text())


def _installed_plugin_paths(hermes_root: Path) -> List[str]:
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
        f"spec = importlib.util.spec_from_file_location('plugins.trade.__init__', '{REPO_ROOT}/plugins/trade/__init__.py')\n"
        "if spec is None or spec.loader is None:\n"
        "    print('[]'); raise SystemExit(0)\n"
        "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
        "hh = os.environ.get('HERMES_HOME', 'NONE')\n"
        "mp = os.path.join(hh, 'kam', 'install_state.json')\n"
        "exists = os.path.exists(mp)\n"
        "content = json.load(open(mp)) if exists else None\n"
        "print(json.dumps({'cmds': mod.registered_commands(), 'hh': hh, 'exists': exists, 'caps': (content or {}).get('capabilities')}))"
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
        return []
    return out.get("cmds", [])


class _FreshRoot(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.hermes_root, self.hermes_home = _make_fresh_root_pair()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


# ============================================================================
# Test A — TRADE ONLY
# ============================================================================
class TestATradeOnly(_FreshRoot):
    def test_A_trade_only(self):
        rc, out, err = _run_script(INSTALL_SH, ["--trade"], self.hermes_root, self.hermes_home)
        self.assertEqual(rc, 0, f"install --trade failed: {out}\n{err}")
        m = _load_manifest(self.hermes_home)
        self.assertTrue(m["capabilities"]["trade"])
        self.assertTrue((self.hermes_home / "trade").is_dir())
        paths = _installed_plugin_paths(self.hermes_root)
        for sp in SHARED_PATHS:
            self.assertIn(sp, paths, f"missing shared path {sp}")
        self.assertIn("wizard.py", paths)
        for n in SHARED_AGENT_NAMES:
            matches = [p for p in paths if p.endswith(f"agents/{n}")]
            self.assertEqual(len(matches), 1, f"duplicate shared agent {n}")
        self.assertEqual(_registered_commands(self.hermes_home), ["trade"])
        # verify
        rc, _, _ = _run_script(VERIFY_SH, ["--trade"], self.hermes_root, self.hermes_home)
        self.assertEqual(rc, 0)
        # uninstall --trade (last cap, removes shared)
        rc, _, _ = _run_script(UNINSTALL_SH, ["--trade"], self.hermes_root, self.hermes_home)
        self.assertEqual(rc, 0)
        self.assertFalse((self.hermes_home / "trade").is_dir())
        self.assertFalse((self.hermes_home / "kam").is_dir())


# ============================================================================
# Test B — LEGACY MIGRATION
# ============================================================================
class TestBLegacyMigration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.hermes_root, self.hermes_home = _make_fresh_root_pair()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_B_legacy_kam_trade_migrated_safely(self):
        legacy_dir = self.hermes_home / ".kam-trade"
        legacy_dir.mkdir(parents=True)
        legacy_manifest = {
            "kam_version": "1.0.0",
            "installer_version": "1.0.0",
            "timestamp": "2026-08-18T00:00:00Z",
            "hermes_root": str(self.hermes_root),
            "copied_files": ["/some/path/file.py"],
        }
        (legacy_dir / "manifest.json").write_text(json.dumps(legacy_manifest))
        backups = legacy_dir / "backups" / "20260818T000000Z"
        backups.mkdir(parents=True)
        (backups / "dummy.txt").write_text("legacy backup content")
        rc, _, _ = _run_script(INSTALL_SH, ["--trade"], self.hermes_root, self.hermes_home)
        self.assertEqual(rc, 0)
        retired = list(self.hermes_home.glob(".kam-trade-retired-*"))
        self.assertGreaterEqual(len(retired), 1, "legacy .kam-trade must be retired, not deleted")
        self.assertTrue((self.hermes_home / "trade").is_dir())
        m = _load_manifest(self.hermes_home)
        self.assertNotIn("api_key", json.dumps(m).lower())
        self.assertNotIn("secret", json.dumps(m).lower())


# ============================================================================
# Test C — --help: zero mutations, exit 0
# ============================================================================
class TestCHelp(_FreshRoot):
    def test_C_help_zero_mutation(self):
        # Pre-state: nothing installed.
        paths_before = _installed_plugin_paths(self.hermes_root)
        rc, out, err = _run_script(INSTALL_SH, ["--help"], self.hermes_root, self.hermes_home)
        self.assertEqual(rc, 0, f"install --help should exit 0: {out}\n{err}")
        # No files installed.
        paths_after = _installed_plugin_paths(self.hermes_root)
        self.assertEqual(paths_after, paths_before)
        # No kam directory.
        self.assertFalse((self.hermes_home / "kam").is_dir())
        # Same for verify and uninstall.
        rc, _, _ = _run_script(VERIFY_SH, ["--help"], self.hermes_root, self.hermes_home)
        self.assertEqual(rc, 0)
        rc, _, _ = _run_script(UNINSTALL_SH, ["--help"], self.hermes_root, self.hermes_home)
        self.assertEqual(rc, 0)


# ============================================================================
# Test D — unknown flag: exit 2, zero mutations
# ============================================================================
class TestDUnknownFlag(_FreshRoot):
    def test_D_unknown_flag_exits_2_zero_mutation(self):
        paths_before = _installed_plugin_paths(self.hermes_root)
        rc, out, err = _run_script(INSTALL_SH, ["--garbage"], self.hermes_root, self.hermes_home)
        self.assertEqual(rc, 2, f"install --garbage should exit 2: {out}\n{err}")
        paths_after = _installed_plugin_paths(self.hermes_root)
        self.assertEqual(paths_after, paths_before)
        self.assertFalse((self.hermes_home / "kam").is_dir())
        # Same for verify and uninstall.
        rc, _, _ = _run_script(VERIFY_SH, ["--unknown"], self.hermes_root, self.hermes_home)
        self.assertEqual(rc, 2)
        rc, _, _ = _run_script(UNINSTALL_SH, ["--unknown"], self.hermes_root, self.hermes_home)
        self.assertEqual(rc, 2)


# ============================================================================
# Test E — --dry-run: zero mutations, prints plan
# ============================================================================
class TestEDryRun(_FreshRoot):
    def test_E_dry_run_zero_mutation(self):
        paths_before = _installed_plugin_paths(self.hermes_root)
        rc, out, err = _run_script(INSTALL_SH, ["--trade", "--dry-run"], self.hermes_root, self.hermes_home)
        self.assertEqual(rc, 0, f"install --dry-run should exit 0: {out}\n{err}")
        self.assertIn("DRY", out.upper())
        # No files installed.
        paths_after = _installed_plugin_paths(self.hermes_root)
        self.assertEqual(paths_after, paths_before)
        # No kam directory written.
        self.assertFalse((self.hermes_home / "kam").is_dir())
        # No capability folders.
        self.assertFalse((self.hermes_home / "trade").is_dir())


# ============================================================================
# Test F — explicit --hermes-root routes to that root
# ============================================================================
class TestFExplicitHermesRoot(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.hermes_root, self.hermes_home = _make_fresh_root_pair()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # A DIFFERENT root that should NOT receive files.
        self.other_root = self.tmp / "other_root"
        self.other_root.mkdir(parents=True, exist_ok=True)
        (self.other_root / "plugins").mkdir(exist_ok=True)

    def test_F_explicit_hermes_root(self):
        rc, _, _ = _run_script(INSTALL_SH, ["--trade"], self.hermes_root, self.hermes_home)
        self.assertEqual(rc, 0)
        # Files exist under the explicit hermes_root.
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "wizard.py").is_file())
        # Files do NOT exist under the other root.
        self.assertFalse((self.other_root / "plugins" / "trade" / "wizard.py").is_file())
        # Files do NOT exist under hermes_home.parent (the OLD broken
        # path that previously broke the installer).
        self.assertFalse((self.hermes_home.parent / "plugins" / "trade" / "wizard.py").is_file())


# ============================================================================
# Test G — explicit hermes-root + isolated hermes-home go to two trees
# ============================================================================
class TestGTwoTrees(_FreshRoot):
    def test_G_two_trees(self):
        rc, _, _ = _run_script(INSTALL_SH, ["--trade"], self.hermes_root, self.hermes_home)
        self.assertEqual(rc, 0)
        # Code under hermes_root.
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "wizard.py").is_file())
        # State under hermes_home.
        self.assertTrue((self.hermes_home / "kam" / "install_state.json").is_file())
        self.assertTrue((self.hermes_home / "trade").is_dir())
        # State does NOT exist under hermes_root.
        self.assertFalse((self.hermes_root / "kam").is_dir())
        # Code does NOT exist under hermes_home.
        self.assertFalse((self.hermes_home / "plugins").is_dir())


# ============================================================================
# Test H — uninstall uses the same explicit hermes-root
# ============================================================================
class TestHUninstallRoot(_FreshRoot):
    def test_H_uninstall_explicit_root(self):
        rc, _, _ = _run_script(INSTALL_SH, ["--trade"], self.hermes_root, self.hermes_home)
        self.assertEqual(rc, 0)
        rc, _, _ = _run_script(UNINSTALL_SH, ["--trade"], self.hermes_root, self.hermes_home)
        self.assertEqual(rc, 0)
        # Plugin files removed from hermes_root.
        self.assertFalse((self.hermes_root / "plugins" / "trade" / "wizard.py").is_file())
        # State removed from hermes_home.
        self.assertFalse((self.hermes_home / "trade").is_dir())


# ============================================================================
# Test I — verify uses the same explicit hermes-root
# ============================================================================
class TestIVerifyRoot(_FreshRoot):
    def test_I_verify_explicit_root(self):
        rc, _, _ = _run_script(INSTALL_SH, ["--trade"], self.hermes_root, self.hermes_home)
        self.assertEqual(rc, 0)
        rc, _, _ = _run_script(VERIFY_SH, ["--trade"], self.hermes_root, self.hermes_home)
        self.assertEqual(rc, 0)


# ============================================================================
# Test J — additive installs with explicit root are idempotent
# ============================================================================
class TestJIdempotentExplicit(_FreshRoot):
    def test_J_additive_idempotent(self):
        # Install --trade twice. The second run must be idempotent
        # (no new files, no errors, byte-identical tree).
        rc, _, _ = _run_script(INSTALL_SH, ["--trade"], self.hermes_root, self.hermes_home)
        self.assertEqual(rc, 0)
        paths_after_first = sorted(_installed_plugin_paths(self.hermes_root))
        rc, _, _ = _run_script(INSTALL_SH, ["--trade"], self.hermes_root, self.hermes_home)
        self.assertEqual(rc, 0)
        paths_after_second = sorted(_installed_plugin_paths(self.hermes_root))
        self.assertEqual(paths_after_second, paths_after_first)
        self.assertIn("wizard.py", paths_after_second)


if __name__ == "__main__":
    unittest.main()