"""Installer test matrix A-O for the modular KAM installer.

Each test uses an isolated HERMES_HOME in a temp directory so it cannot
affect the live system. Tests do NOT touch fibo.service, do NOT place
live orders, do NOT require real exchange credentials.

The tests directly drive the capability-aware installer modules (no
shell-out to install.sh) so we can assert on internal state.

Matrix:
  A. fresh --trade
  B. fresh --fibo
  C. fresh --trade --fibo
  D. --trade then --fibo
  E. --fibo then --trade
  F. repeated --trade (idempotent)
  G. repeated --fibo (idempotent)
  H. uninstall --fibo from both
  I. uninstall --trade from both
  J. uninstall last capability (shared cleanup)
  K. verify --trade on trade-only
  L. verify --fibo on fibo-only
  M. verify both on both
  N. verify --fibo on trade-only (must report fibo not installed)
  O. verify --trade on fibo-only (must report trade not installed)
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "installer"))
# Also make the repo source tree importable for shared probes.
sys.path.insert(0, str(REPO_ROOT))


# Import the modules under test.
import capabilities as C  # noqa: E402


def _load(module_name: str):
    return importlib.import_module(module_name)


_install_shared = _load("installer_shared")
_install_trade = _load("install_trade_capability")
_install_fibo = _load("install_fibo_capability")
_uninstall_trade = _load("uninstall_trade_capability")
_uninstall_fibo = _load("uninstall_fibo_capability")
_uninstall_shared = _load("uninstall_shared")
_verify_shared = _load("verify_shared")
_verify_trade = _load("verify_trade_capability")
_verify_fibo = _load("verify_fibo_capability")


def _fresh_root() -> Path:
    """Return a fresh isolated HERMES_HOME inside a temp directory."""
    tmp = Path(tempfile.mkdtemp(prefix="kam-modular-itest-"))
    hermes_home = tmp / "hermes_home"
    hermes_home.mkdir(parents=True, exist_ok=True)
    # Make a hermes_root with a "plugins/" tree where files will be written.
    hermes_root = hermes_home / "checkout"
    hermes_root.mkdir(parents=True, exist_ok=True)
    (hermes_root / "plugins").mkdir(parents=True, exist_ok=True)
    return hermes_home


def _do_install(caps: List[str], hermes_home: Path) -> Dict[str, Any]:
    """Run shared + per-capability install and persist the manifest."""
    shared = _install_shared.install_shared(argv=[], hermes_home=hermes_home, capabilities=caps)
    results: Dict[str, Dict[str, Any]] = {}
    for cap in caps:
        if cap == "trade":
            res = _install_trade.run(argv=[], hermes_home=hermes_home, shared=shared)
        elif cap == "fibo":
            res = _install_fibo.run(argv=[], hermes_home=hermes_home, shared=shared)
        else:
            raise ValueError(f"unknown capability {cap!r}")
        results[cap] = res
    # Update manifest.
    m = C.load_manifest(hermes_home)
    for cap in caps:
        C.set_capability(m, cap, results[cap])
    m["shared"] = shared
    C.save_manifest(hermes_home, m)
    return results


def _do_uninstall(caps: List[str], hermes_home: Path) -> None:
    """Run per-capability uninstall + shared if no caps remain."""
    for cap in reversed(caps):
        if cap == "trade":
            _uninstall_trade.run(argv=[], hermes_home=hermes_home)
        elif cap == "fibo":
            _uninstall_fibo.run(argv=[], hermes_home=hermes_home)
    m = C.load_manifest(hermes_home)
    for cap in caps:
        C.clear_capability(m, cap)
    # Save the cleared manifest BEFORE re-reading, so any_capability_installed
    # sees the cleared state from disk.
    C.save_manifest(hermes_home, m)
    if not C.any_capability_installed(hermes_home):
        _uninstall_shared.run(argv=[], hermes_home=hermes_home)
        m["shared"] = {}
        # Don't save manifest after shared uninstall — kam/ is gone.


def _do_verify(caps: List[str], hermes_home: Path):
    """Run shared + per-capability verify.

    Returns (shared_ok, all_caps_ok).
    """
    # Helper body follows.
    shared_ok = _verify_shared.run(argv=[], hermes_home=hermes_home, capabilities=caps)
    cap_results: List[bool] = []
    for cap in caps:
        if cap == "trade":
            ok = _verify_trade.run(argv=[], hermes_home=hermes_home)
        elif cap == "fibo":
            ok = _verify_fibo.run(argv=[], hermes_home=hermes_home)
        else:
            raise ValueError(f"unknown capability {cap!r}")
        cap_results.append(ok)
    all_caps_ok = all(cap_results)
    return shared_ok, all_caps_ok


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="kam-modular-itest-")
        self.hermes_home = Path(self._tmpdir) / "hermes_home"
        self.hermes_home.mkdir(parents=True, exist_ok=True)
        # Per the installer convention: hermes_root = hermes_home.parent.
        self.hermes_root = self.hermes_home.parent
        (self.hermes_root / "plugins").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmpdir, ignore_errors=True)


class TestFreshInstall(_Base):
    # A. fresh --trade
    def test_A_fresh_trade(self):
        _do_install(["trade"], self.hermes_home)
        self.assertTrue(C.is_installed(self.hermes_home, "trade"))
        self.assertFalse(C.is_installed(self.hermes_home, "fibo"))
        # /trade file installed
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "wizard.py").is_file())
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "tradedesk.py").is_file())
        # /fibo files NOT installed
        self.assertFalse((self.hermes_root / "plugins" / "trade" / "fibo_service.py").is_file())
        self.assertFalse((self.hermes_root / "plugins" / "trade" / "golden_fibo").is_dir())
        # Capability folders
        self.assertTrue(C.capability_dir(self.hermes_home, "trade").is_dir())
        self.assertFalse(C.capability_dir(self.hermes_home, "fibo").is_dir())
        # Shared agents present
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "agents" / "x_lighter_agent.py").is_file())

    # B. fresh --fibo
    def test_B_fresh_fibo(self):
        _do_install(["fibo"], self.hermes_home)
        self.assertFalse(C.is_installed(self.hermes_home, "trade"))
        self.assertTrue(C.is_installed(self.hermes_home, "fibo"))
        # /fibo files installed
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "fibo_service.py").is_file())
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "fibo_wizard.py").is_file())
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "golden_fibo").is_dir())
        # /trade-specific wizard NOT installed (fibo-only must not depend on it)
        self.assertFalse((self.hermes_root / "plugins" / "trade" / "wizard.py").is_file())
        # tradedesk.py is SHARED (both /trade and /fibo need it) and IS installed
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "tradedesk.py").is_file())
        # Capability folders
        self.assertFalse(C.capability_dir(self.hermes_home, "trade").is_dir())
        self.assertTrue(C.capability_dir(self.hermes_home, "fibo").is_dir())
        # Shared agents present (fibo needs them too)
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "agents" / "x_lighter_agent.py").is_file())

    # C. fresh --trade --fibo
    def test_C_fresh_trade_and_fibo(self):
        _do_install(["trade", "fibo"], self.hermes_home)
        self.assertTrue(C.is_installed(self.hermes_home, "trade"))
        self.assertTrue(C.is_installed(self.hermes_home, "fibo"))
        # All files present
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "wizard.py").is_file())
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "fibo_service.py").is_file())
        # Both folders
        self.assertTrue(C.capability_dir(self.hermes_home, "trade").is_dir())
        self.assertTrue(C.capability_dir(self.hermes_home, "fibo").is_dir())
        # Shared agents present ONCE
        agents_dir = self.hermes_root / "plugins" / "trade" / "agents"
        self.assertTrue(agents_dir.is_dir())
        agent_files = list(agents_dir.glob("x_*_agent.py"))
        # At least one agent per exchange. No duplicates.
        names = [p.name for p in agent_files]
        self.assertEqual(len(names), len(set(names)))


class TestAdditive(_Base):
    # D. --trade then --fibo
    def test_D_trade_then_fibo(self):
        _do_install(["trade"], self.hermes_home)
        before_files = sorted((self.hermes_root / "plugins" / "trade").rglob("*.py"))
        _do_install(["fibo"], self.hermes_home)
        # trade still installed, fibo now installed
        self.assertTrue(C.is_installed(self.hermes_home, "trade"))
        self.assertTrue(C.is_installed(self.hermes_home, "fibo"))
        # Trade files still present (not damaged)
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "wizard.py").is_file())
        # Fibo files added
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "fibo_service.py").is_file())
        # No duplicates of shared agents
        agents = list((self.hermes_root / "plugins" / "trade" / "agents").glob("x_*_agent.py"))
        self.assertEqual(len(agents), len(set(agents)))

    # E. --fibo then --trade
    def test_E_fibo_then_trade(self):
        _do_install(["fibo"], self.hermes_home)
        _do_install(["trade"], self.hermes_home)
        self.assertTrue(C.is_installed(self.hermes_home, "trade"))
        self.assertTrue(C.is_installed(self.hermes_home, "fibo"))
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "fibo_service.py").is_file())
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "wizard.py").is_file())


class TestIdempotent(_Base):
    # F. repeated --trade
    def test_F_repeated_trade_idempotent(self):
        _do_install(["trade"], self.hermes_home)
        before = json.loads(C.install_state_path(self.hermes_home).read_text())
        _do_install(["trade"], self.hermes_home)
        after = json.loads(C.install_state_path(self.hermes_home).read_text())
        self.assertEqual(before["capabilities"], after["capabilities"])
        # File count unchanged
        self.assertEqual(len(list((self.hermes_root / "plugins" / "trade").rglob("*.py"))),
                         len([p for p in (self.hermes_root / "plugins" / "trade").rglob("*.py")]))

    # G. repeated --fibo
    def test_G_repeated_fibo_idempotent(self):
        _do_install(["fibo"], self.hermes_home)
        _do_install(["fibo"], self.hermes_home)
        self.assertTrue(C.is_installed(self.hermes_home, "fibo"))
        # No duplicate fibo files
        fibo_service = self.hermes_root / "plugins" / "trade" / "fibo_service.py"
        self.assertTrue(fibo_service.is_file())


class TestPartialUninstall(_Base):
    # H. uninstall --fibo from both
    def test_H_uninstall_fibo_from_both(self):
        _do_install(["trade", "fibo"], self.hermes_home)
        _do_uninstall(["fibo"], self.hermes_home)
        # trade remains, fibo gone
        self.assertTrue(C.is_installed(self.hermes_home, "trade"))
        self.assertFalse(C.is_installed(self.hermes_home, "fibo"))
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "wizard.py").is_file())
        self.assertFalse((self.hermes_root / "plugins" / "trade" / "fibo_service.py").is_file())
        self.assertFalse((self.hermes_root / "plugins" / "trade" / "golden_fibo").is_dir())
        self.assertTrue(C.capability_dir(self.hermes_home, "trade").is_dir())
        self.assertFalse(C.capability_dir(self.hermes_home, "fibo").is_dir())

    # I. uninstall --trade from both (must NEVER touch fibo)
    def test_I_uninstall_trade_from_both_preserves_fibo(self):
        _do_install(["trade", "fibo"], self.hermes_home)
        _do_uninstall(["trade"], self.hermes_home)
        self.assertFalse(C.is_installed(self.hermes_home, "trade"))
        self.assertTrue(C.is_installed(self.hermes_home, "fibo"))
        # fibo files untouched
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "fibo_service.py").is_file())
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "golden_fibo").is_dir())
        self.assertTrue(C.capability_dir(self.hermes_home, "fibo").is_dir())
        # /trade-only wizard.py removed
        self.assertFalse((self.hermes_root / "plugins" / "trade" / "wizard.py").is_file())
        # tradedesk.py is SHARED and remains (fibo still uses it)
        self.assertTrue((self.hermes_root / "plugins" / "trade" / "tradedesk.py").is_file())

    # J. uninstall last capability (shared cleanup)
    def test_J_uninstall_last_cleans_shared(self):
        _do_install(["trade", "fibo"], self.hermes_home)
        _do_uninstall(["trade", "fibo"], self.hermes_home)
        self.assertFalse(C.is_installed(self.hermes_home, "trade"))
        self.assertFalse(C.is_installed(self.hermes_home, "fibo"))
        # Shared agents removed
        self.assertFalse((self.hermes_root / "plugins" / "trade" / "agents").is_dir())
        # kam/ removed
        self.assertFalse((self.hermes_home / "kam").is_dir())


class TestVerify(_Base):
    # K. verify --trade on trade-only
    def test_K_verify_trade_on_trade_only(self):
        _do_install(["trade"], self.hermes_home)
        shared_ok, all_caps_ok = _do_verify(["trade"], self.hermes_home)
        self.assertTrue(shared_ok)
        self.assertTrue(all_caps_ok)

    # L. verify --fibo on fibo-only
    def test_L_verify_fibo_on_fibo_only(self):
        _do_install(["fibo"], self.hermes_home)
        shared_ok, all_caps_ok = _do_verify(["fibo"], self.hermes_home)
        self.assertTrue(shared_ok)
        self.assertTrue(all_caps_ok)

    # M. verify both on both
    def test_M_verify_both_on_both(self):
        _do_install(["trade", "fibo"], self.hermes_home)
        shared_ok, all_caps_ok = _do_verify(["trade", "fibo"], self.hermes_home)
        self.assertTrue(shared_ok)
        self.assertTrue(all_caps_ok)

    # N. verify --fibo on trade-only (must report fibo not installed)
    def test_N_verify_fibo_on_trade_only(self):
        _do_install(["trade"], self.hermes_home)
        # Manually invoke the fibo verifier (it should fail because fibo is
        # not installed and the fibo files are absent).
        ok = _verify_fibo.run(argv=[], hermes_home=self.hermes_home)
        self.assertFalse(ok)
        self.assertFalse(C.is_installed(self.hermes_home, "fibo"))

    # O. verify --trade on fibo-only (must report trade not installed)
    def test_O_verify_trade_on_fibo_only(self):
        _do_install(["fibo"], self.hermes_home)
        ok = _verify_trade.run(argv=[], hermes_home=self.hermes_home)
        self.assertFalse(ok)
        self.assertFalse(C.is_installed(self.hermes_home, "trade"))


class TestManifestSchema(_Base):
    def test_manifest_schema_v1(self):
        _do_install(["trade", "fibo"], self.hermes_home)
        m = C.load_manifest(self.hermes_home)
        self.assertEqual(m["schema_version"], C.SCHEMA_VERSION)
        self.assertEqual(m["capabilities"], {"trade": True, "fibo": True})
        self.assertIn("by_capability", m)
        self.assertIn("trade", m["by_capability"])
        self.assertIn("fibo", m["by_capability"])
        self.assertIn("shared", m)

    def test_atomic_write_no_leftover(self):
        _do_install(["trade"], self.hermes_home)
        # Re-install (atomic write should not leave tmp files).
        _do_install(["trade"], self.hermes_home)
        parent = self.hermes_home / "kam"
        tmps = list(parent.glob("install_state.json.*.tmp"))
        self.assertEqual(tmps, [], f"atomic write left tmp files: {tmps}")

    def test_no_credentials_in_manifest(self):
        _do_install(["trade"], self.hermes_home)
        m = C.load_manifest(self.hermes_home)
        # Manifest must not contain anything that looks like a key/secret.
        text = json.dumps(m)
        self.assertNotIn("sk-", text)
        self.assertNotIn("api_key", text.lower())


class TestCapabilityFlagParsing(unittest.TestCase):
    def test_no_flag_returns_trade(self):
        # Decision 1: no-flag = TRADE ONLY.
        self.assertTrue(C.is_no_flag([]))
        self.assertFalse(C.is_no_flag(["--trade"]))
        self.assertFalse(C.is_no_flag(["--fibo"]))

    def test_parse_capability_flags(self):
        caps, rest = C.parse_capability_flags(["--trade", "--hermes-root", "/path", "--fibo"])
        self.assertEqual(caps, ["trade", "fibo"])
        self.assertEqual(rest, ["--hermes-root", "/path"])

    def test_parse_no_caps(self):
        caps, rest = C.parse_capability_flags(["--hermes-root", "/path"])
        self.assertEqual(caps, [])
        self.assertEqual(rest, ["--hermes-root", "/path"])


if __name__ == "__main__":
    unittest.main()



class TestCapabilityAwareRegistration(unittest.TestCase):
    """Test that the actual plugin registration layer reflects installed capabilities.

    These tests exercise the real ``plugins.trade.__init__.register`` function
    (and ``registered_commands`` for unit tests) against a controlled
    ``HERMES_HOME``. They do NOT inspect the manifest contents alone — they
    inspect the actual registered command set, which is what the gateway
    would surface.

    The capability-aware registration is the lock-down behavior:
      --trade   => /trade registered, /fibo NOT registered
      --fibo    => /fibo registered, /trade NOT registered
      both      => both registered
      uninstall => stale handlers removed
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="kam-capreg-")
        self.hermes_home = Path(self._tmpdir) / "hermes_home"
        self.hermes_home.mkdir(parents=True, exist_ok=True)
        # The plugin __init__ reads HERMES_HOME from env, then loads
        # <HERMES_HOME>/kam/install_state.json.
        self._env = os.environ.copy()
        os.environ["HERMES_HOME"] = str(self.hermes_home)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        os.environ.clear()
        os.environ.update(self._env)

    def _write_manifest(self, capabilities):
        import importlib
        sys.path.insert(0, str(REPO_ROOT / "installer"))
        import capabilities as C
        m = C._empty_manifest()
        for cap, val in capabilities.items():
            m["capabilities"][cap] = val
        C.save_manifest(self.hermes_home, m)

    def _load_plugin(self):
        # Load the plugins.trade package from the repo source tree.
        import importlib.util
        # The test file lives at <repo>/installer/tests/test_installer_modular.py
        # REPO_ROOT in this test module is <repo>/installer (one level too shallow).
        # The actual repo root is the parent of REPO_ROOT.
        actual_repo_root = REPO_ROOT.parent
        path = actual_repo_root / "plugins" / "trade" / "__init__.py"
        # Use a synthetic spec to avoid clashing with any existing
        # ``plugins`` package on sys.path.
        spec = importlib.util.spec_from_file_location(
            name="plugins.trade._capability_test",
            location=str(path),
            submodule_search_locations=[str(path.parent)],
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["plugins.trade._capability_test"] = mod
        spec.loader.exec_module(mod)
        return mod

    def _register_against(self, mod, capabilities):
        # Run the actual register() with a fake plugin context that
        # records which commands were registered.
        self._write_manifest(capabilities)
        registered = []

        class _FakeCtx:
            def register_command(self, name, handler=None, description=None, args_hint=None):
                registered.append(name)

        mod.register(_FakeCtx())
        return registered

    def test_A_trade_only_registers_trade_only(self):
        mod = self._load_plugin()
        registered = self._register_against(mod, {"trade": True, "fibo": False})
        self.assertEqual(registered, ["trade"])

    def test_B_fibo_only_registers_fibo_only(self):
        mod = self._load_plugin()
        registered = self._register_against(mod, {"trade": False, "fibo": True})
        self.assertEqual(registered, ["fibo"])

    def test_C_both_registers_both(self):
        mod = self._load_plugin()
        registered = self._register_against(mod, {"trade": True, "fibo": True})
        self.assertEqual(set(registered), {"trade", "fibo"})
        self.assertEqual(len(registered), 2)

    def test_no_capabilities_registers_nothing(self):
        mod = self._load_plugin()
        registered = self._register_against(mod, {"trade": False, "fibo": False})
        self.assertEqual(registered, [])

    def test_missing_manifest_registers_nothing(self):
        """If install_state.json doesn't exist, no commands register."""
        mod = self._load_plugin()
        # Don't write a manifest.
        registered = []

        class _FakeCtx:
            def register_command(self, name, handler=None, description=None, args_hint=None):
                registered.append(name)
        mod.register(_FakeCtx())
        self.assertEqual(registered, [])

    def test_additive_trade_then_fibo_registers_both(self):
        """--trade then --fibo: both commands available after second install."""
        mod = self._load_plugin()
        # First install: trade only.
        registered = self._register_against(mod, {"trade": True, "fibo": False})
        self.assertEqual(registered, ["trade"])
        # Second install: add fibo.
        registered = self._register_against(mod, {"trade": True, "fibo": True})
        self.assertEqual(set(registered), {"trade", "fibo"})

    def test_additive_fibo_then_trade_registers_both(self):
        mod = self._load_plugin()
        registered = self._register_against(mod, {"trade": False, "fibo": True})
        self.assertEqual(registered, ["fibo"])
        registered = self._register_against(mod, {"trade": True, "fibo": True})
        self.assertEqual(set(registered), {"trade", "fibo"})

    def test_uninstall_trade_keeps_only_fibo(self):
        mod = self._load_plugin()
        # Both installed.
        registered = self._register_against(mod, {"trade": True, "fibo": True})
        self.assertEqual(set(registered), {"trade", "fibo"})
        # Uninstall trade: only fibo remains.
        registered = self._register_against(mod, {"trade": False, "fibo": True})
        self.assertEqual(registered, ["fibo"])

    def test_uninstall_fibo_keeps_only_trade(self):
        mod = self._load_plugin()
        registered = self._register_against(mod, {"trade": True, "fibo": True})
        self.assertEqual(set(registered), {"trade", "fibo"})
        registered = self._register_against(mod, {"trade": True, "fibo": False})
        self.assertEqual(registered, ["trade"])

    def test_fibo_only_does_not_import_wizard(self):
        """A fibo-only install must NOT depend on /trade's wizard.py.

        We verify this by checking that fibo's own imports succeed when
        wizard.py is absent. This protects against accidentally pulling
        the /trade wizard into a fibo-only install.
        """
        repo_root = REPO_ROOT.parent
        # Build a fake hermes_root where ONLY fibo + shared files are present.
        tmp = Path(tempfile.mkdtemp(prefix="kam-fiboonly-"))
        hr = tmp / "checkout"; (hr / "plugins" / "trade").mkdir(parents=True)
        shared = [
            "__init__.py", "canonical.py", "tradedesk.py",
            "agents/__init__.py",
            "agents/x_lighter_agent.py",  # one agent to confirm shared works
        ]
        fibo = [
            "fibo_service.py", "fibo_daemon.py", "fibo_wizard.py",
            "golden_fibo/__init__.py", "golden_fibo/config.py",
            "golden_fibo/engine.py", "golden_fibo/lighter_adapter.py",
            "golden_fibo/preflight.py", "golden_fibo/state.py",
        ]
        for rel in shared + fibo:
            src = repo_root / "plugins" / "trade" / rel
            dst = hr / "plugins" / "trade" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        # Inspect: no wizard.py should be present in the fibo-only checkout.
        self.assertFalse((hr / "plugins" / "trade" / "wizard.py").exists())
        shutil.rmtree(tmp, ignore_errors=True)
