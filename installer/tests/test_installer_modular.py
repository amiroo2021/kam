"""Installer unit tests for the modular KAM installer (post root-fix).

These tests use EXPLICIT ``hermes_root`` and ``hermes_home`` arguments.
The hermes_root is the installed application tree (plugins/trade/); the
hermes_home is the persistent state directory (~/.hermes/kam/,
~/.hermes/trade/).

Matrix (test classes):
  A. fresh --trade
  B. additive installs
  C. last-capability uninstall
  D. capability-aware command registration
  E. capability flag parsing
  F. manifest schema
  G. unknown-flag / help / dry-run
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List


# Use a stable, repo-relative Python sys.path.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "installer"))


import capabilities as C  # noqa: E402
import installer_shared as IS  # noqa: E402
import install_trade_capability as IT  # noqa: E402
import uninstall_trade_capability as UT  # noqa: E402
import uninstall_shared as US  # noqa: E402
import verify_shared as VS  # noqa: E402
import verify_trade_capability as VT  # noqa: E402
# Import the installer dispatcher via its module name (avoids collision
# with the ``installer`` namespace package on sys.path).
import importlib
_INSTALLER_MOD = importlib.import_module("installer.installer")
INST = _INSTALLER_MOD



def _fresh() -> Dict[str, Path]:
    """Return fresh isolated hermes_root and hermes_home in a temp dir."""
    tmp = Path(tempfile.mkdtemp(prefix="kam-itest-"))
    hermes_home = tmp / "hermes_home"
    hermes_root = tmp / "hermes_root"
    hermes_home.mkdir(parents=True, exist_ok=True)
    hermes_root.mkdir(parents=True, exist_ok=True)
    (hermes_root / "plugins").mkdir(parents=True, exist_ok=True)
    return {"tmp": tmp, "hermes_home": hermes_home, "hermes_root": hermes_root}


def _do_install(caps: List[str], hermes_root: Path, hermes_home: Path) -> Dict[str, Any]:
    shared = IS.install_shared(
        argv=[], hermes_root=hermes_root, hermes_home=hermes_home,
        capabilities=caps, dry_run=False,
    )
    results: Dict[str, Dict[str, Any]] = {}
    for cap in caps:
        if cap == "trade":
            results[cap] = IT.run(
                argv=[], hermes_root=hermes_root, hermes_home=hermes_home,
                shared=shared, dry_run=False,
            )
        else:
            raise ValueError(f"unknown capability {cap!r}")
    m = C.load_manifest(hermes_home)
    for cap in caps:
        C.set_capability(m, cap, results[cap])
    m["shared"] = shared
    C.save_manifest(hermes_home, m)
    return results


def _do_uninstall(caps: List[str], hermes_root: Path, hermes_home: Path) -> None:
    for cap in reversed(caps):
        if cap == "trade":
            UT.run(argv=[], hermes_root=hermes_root, hermes_home=hermes_home, dry_run=False)
    m = C.load_manifest(hermes_home)
    for cap in caps:
        C.clear_capability(m, cap)
    C.save_manifest(hermes_home, m)
    if not C.any_capability_installed(hermes_home):
        US.run(argv=[], hermes_root=hermes_root, hermes_home=hermes_home, dry_run=False)
        # Don't re-save manifest after shared removes kam/.


def _do_verify(caps: List[str], hermes_root: Path, hermes_home: Path):
    shared_ok = VS.run(
        argv=[], hermes_root=hermes_root, hermes_home=hermes_home,
        capabilities=caps, dry_run=False,
    )
    cap_results: List[bool] = []
    for cap in caps:
        if cap == "trade":
            ok = VT.run(argv=[], hermes_root=hermes_root, hermes_home=hermes_home)
        else:
            raise ValueError(f"unknown capability {cap!r}")
        cap_results.append(ok)
    return shared_ok, all(cap_results)


class TestFreshInstall(unittest.TestCase):
    def setUp(self) -> None:
        self.env = _fresh()
        self.addCleanup(shutil.rmtree, self.env["tmp"], ignore_errors=True)

    # A. fresh --trade
    def test_A_fresh_trade(self):
        _do_install(["trade"], self.env["hermes_root"], self.env["hermes_home"])
        self.assertTrue(C.is_installed(self.env["hermes_home"], "trade"))
        self.assertTrue((self.env["hermes_home"] / "trade").is_dir())
        self.assertTrue((self.env["hermes_root"] / "plugins" / "trade" / "wizard.py").is_file())
        # Shared agents present
        self.assertTrue((self.env["hermes_root"] / "plugins" / "trade" / "agents" / "x_lighter_agent.py").is_file())


class TestIdempotent(unittest.TestCase):
    def setUp(self) -> None:
        self.env = _fresh()
        self.addCleanup(shutil.rmtree, self.env["tmp"], ignore_errors=True)

    def test_F_repeated_trade_idempotent(self):
        _do_install(["trade"], self.env["hermes_root"], self.env["hermes_home"])
        before = json.loads(C.install_state_path(self.env["hermes_home"]).read_text())
        _do_install(["trade"], self.env["hermes_root"], self.env["hermes_home"])
        after = json.loads(C.install_state_path(self.env["hermes_home"]).read_text())
        self.assertEqual(before["capabilities"], after["capabilities"])


class TestPartialUninstall(unittest.TestCase):
    def setUp(self) -> None:
        self.env = _fresh()
        self.addCleanup(shutil.rmtree, self.env["tmp"], ignore_errors=True)

    def test_J_uninstall_last_cleans_shared(self):
        _do_install(["trade"], self.env["hermes_root"], self.env["hermes_home"])
        _do_uninstall(["trade"], self.env["hermes_root"], self.env["hermes_home"])
        self.assertFalse(C.is_installed(self.env["hermes_home"], "trade"))
        self.assertFalse((self.env["hermes_root"] / "plugins" / "trade" / "agents").is_dir())
        self.assertFalse((self.env["hermes_home"] / "kam").is_dir())


class TestCapabilityAwareRegistration(unittest.TestCase):
    """The actual plugin registration layer reflects installed capabilities."""

    def setUp(self) -> None:
        self.env = _fresh()
        self.addCleanup(shutil.rmtree, self.env["tmp"], ignore_errors=True)
        self._env = os.environ.copy()
        os.environ["HERMES_HOME"] = str(self.env["hermes_home"])

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def _load_plugin(self):
        spec = importlib.util.spec_from_file_location(
            name="plugins.trade._capability_test",
            location=str(REPO_ROOT / "plugins" / "trade" / "__init__.py"),
            submodule_search_locations=[str((REPO_ROOT / "plugins" / "trade").resolve())],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot build spec for plugins.trade.__init__")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["plugins.trade._capability_test"] = mod
        spec.loader.exec_module(mod)
        return mod

    def _write_manifest(self, capabilities):
        m = C._empty_manifest()
        for cap, val in capabilities.items():
            m["capabilities"][cap] = val
        C.save_manifest(self.env["hermes_home"], m)

    def _register_against(self, mod, capabilities):
        self._write_manifest(capabilities)
        registered = []

        class _FakeCtx:
            def register_command(self, name, handler=None, description=None, args_hint=None):
                registered.append(name)
        mod.register(_FakeCtx())
        return registered

    def test_trade_only_registers_trade_only(self):
        mod = self._load_plugin()
        registered = self._register_against(mod, {"trade": True})
        self.assertEqual(registered, ["trade"])

    def test_no_capabilities_registers_nothing(self):
        mod = self._load_plugin()
        registered = self._register_against(mod, {"trade": False})
        self.assertEqual(registered, [])

    def test_missing_manifest_registers_nothing(self):
        mod = self._load_plugin()
        registered = []

        class _FakeCtx:
            def register_command(self, name, handler=None, description=None, args_hint=None):
                registered.append(name)
        mod.register(_FakeCtx())
        self.assertEqual(registered, [])


class TestCapabilityFlagParsing(unittest.TestCase):
    def test_no_flag_returns_trade(self):
        self.assertTrue(C.is_no_flag([]))
        self.assertFalse(C.is_no_flag(["--trade"]))

    def test_parse_capability_flags(self):
        caps, rest = C.parse_capability_flags(["--trade", "--hermes-root", "/path"])
        self.assertEqual(caps, ["trade"])
        self.assertEqual(rest, ["--hermes-root", "/path"])


class TestManifestSchema(unittest.TestCase):
    def setUp(self) -> None:
        self.env = _fresh()
        self.addCleanup(shutil.rmtree, self.env["tmp"], ignore_errors=True)

    def test_manifest_schema_v1(self):
        _do_install(["trade"], self.env["hermes_root"], self.env["hermes_home"])
        m = C.load_manifest(self.env["hermes_home"])
        self.assertEqual(m["schema_version"], C.SCHEMA_VERSION)
        self.assertEqual(m["capabilities"], {"trade": True})
        self.assertIn("by_capability", m)

    def test_atomic_write_no_leftover(self):
        _do_install(["trade"], self.env["hermes_root"], self.env["hermes_home"])
        _do_install(["trade"], self.env["hermes_root"], self.env["hermes_home"])
        tmps = list((self.env["hermes_home"] / "kam").glob("install_state.json.*.tmp"))
        self.assertEqual(tmps, [])

    def test_no_credentials_in_manifest(self):
        _do_install(["trade"], self.env["hermes_root"], self.env["hermes_home"])
        m = C.load_manifest(self.env["hermes_home"])
        text = json.dumps(m)
        self.assertNotIn("sk-", text)
        self.assertNotIn("api_key", text.lower())


class TestDispatcherArgParsing(unittest.TestCase):
    """Verify --help and unknown-flag handling at the dispatcher level."""

    def test_help_prints_usage_and_exits_zero(self):
        with self.assertRaises(SystemExit) as cm:
            INST._parse_args(["--help"], "install")
        self.assertEqual(cm.exception.code, 0)

    def test_dash_h_prints_usage_and_exits_zero(self):
        with self.assertRaises(SystemExit) as cm:
            INST._parse_args(["-h"], "verify")
        self.assertEqual(cm.exception.code, 0)

    def test_unknown_flag_exits_nonzero(self):
        with self.assertRaises(SystemExit) as cm:
            INST._parse_args(["--garbage"], "install")
        self.assertEqual(cm.exception.code, 2)

    def test_unknown_flag_with_dry_run_exits_nonzero(self):
        # The --dry-run flag is recognized, but --garbage is not.
        with self.assertRaises(SystemExit) as cm:
            INST._parse_args(["--dry-run", "--garbage"], "install")
        self.assertEqual(cm.exception.code, 2)

    def test_known_flags_parse(self):
        args = INST._parse_args(["--trade", "--hermes-root", "/x", "--dry-run"], "install")
        self.assertTrue(args.trade)
        self.assertEqual(args.hermes_root, "/x")
        self.assertTrue(args.dry_run)


if __name__ == "__main__":
    unittest.main()