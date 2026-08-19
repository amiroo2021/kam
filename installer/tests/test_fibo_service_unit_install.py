"""Fresh-install fibo.service unit regression tests (Lodo missing unit)."""

from __future__ import annotations

import importlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "installer"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fibo_unit import (  # noqa: E402
    render_fibo_unit,
    verify_fibo_service_unit,
)
from pristine_telegram_adapter import PRISTINE_TELEGRAM_ADAPTER  # noqa: E402
from patchspecs import TELEGRAM_ADAPTER  # noqa: E402

INST = importlib.import_module("installer.installer")


def _fresh() -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="kam-fibo-unit-"))
    hermes_home = tmp / "hermes_home"
    hermes_root = tmp / "hermes_root"
    systemd_dir = tmp / "systemd"
    hermes_home.mkdir()
    hermes_root.mkdir()
    systemd_dir.mkdir()
    # fake venv python path (use real interpreter as the binary)
    venv_bin = hermes_root / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    py = venv_bin / "python"
    # symlink to real python so verify "exists" check passes
    if not py.exists():
        py.symlink_to(sys.executable)
    adapter = hermes_root / TELEGRAM_ADAPTER
    adapter.parent.mkdir(parents=True, exist_ok=True)
    adapter.write_text(PRISTINE_TELEGRAM_ADAPTER, encoding="utf-8")
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled: []\n"
        "platforms:\n  telegram:\n    extra:\n      command_menu:\n"
        "        max_commands: 60\n",
        encoding="utf-8",
    )
    return {
        "tmp": tmp,
        "hermes_home": hermes_home,
        "hermes_root": hermes_root,
        "systemd_dir": systemd_dir,
    }


def _run(action: str, caps: List[str], env: dict) -> int:
    argv = [f"--{c}" for c in caps]
    argv += [
        "--hermes-root",
        str(env["hermes_root"]),
        "--hermes-home",
        str(env["hermes_home"]),
        "--systemd-dir",
        str(env["systemd_dir"]),
        "--skip-deps",
        "--no-restart",
        "--action",
        action,
    ]
    return INST.main(argv)


class FiboServiceUnitInstallTests(unittest.TestCase):
    def tearDown(self) -> None:
        tmp = getattr(self, "_tmp", None)
        if tmp and Path(tmp).is_dir():
            shutil.rmtree(tmp, ignore_errors=True)

    def test_trade_only_does_not_install_unit(self):
        env = _fresh()
        self._tmp = env["tmp"]
        self.assertEqual(_run("install", ["trade"], env), 0)
        self.assertFalse((env["systemd_dir"] / "fibo.service").exists())

    def test_fibo_only_installs_unit(self):
        env = _fresh()
        self._tmp = env["tmp"]
        self.assertEqual(_run("install", ["fibo"], env), 0)
        unit = env["systemd_dir"] / "fibo.service"
        self.assertTrue(unit.is_file())
        text = unit.read_text()
        self.assertIn(str(env["hermes_root"]), text)
        self.assertIn(str(env["hermes_home"]), text)
        self.assertIn("plugins.trade.fibo_daemon", text)
        self.assertIn(str(env["hermes_home"] / "fibo" / "service.sock"), text)
        self.assertNotIn("/root/kam", text)
        self.assertNotIn("/tmp/kam-itest", text)
        ok, msgs = verify_fibo_service_unit(
            hermes_root=env["hermes_root"],
            hermes_home=env["hermes_home"],
            systemd_dir=env["systemd_dir"],
            require_active=False,
        )
        self.assertTrue(ok, msgs)
        self.assertEqual(_run("verify", ["fibo"], env), 0)

    def test_trade_and_fibo_installs_unit(self):
        env = _fresh()
        self._tmp = env["tmp"]
        self.assertEqual(_run("install", ["trade", "fibo"], env), 0)
        self.assertTrue((env["systemd_dir"] / "fibo.service").is_file())

    def test_repeated_install_idempotent(self):
        env = _fresh()
        self._tmp = env["tmp"]
        self.assertEqual(_run("install", ["fibo"], env), 0)
        unit = env["systemd_dir"] / "fibo.service"
        first = unit.read_bytes()
        self.assertEqual(_run("install", ["fibo"], env), 0)
        second = unit.read_bytes()
        self.assertEqual(first, second)

    def test_dry_run_no_unit(self):
        env = _fresh()
        self._tmp = env["tmp"]
        argv = [
            "--fibo",
            "--hermes-root",
            str(env["hermes_root"]),
            "--hermes-home",
            str(env["hermes_home"]),
            "--systemd-dir",
            str(env["systemd_dir"]),
            "--skip-deps",
            "--dry-run",
            "--action",
            "install",
        ]
        self.assertEqual(INST.main(argv), 0)
        self.assertFalse((env["systemd_dir"] / "fibo.service").exists())

    def test_uninstall_removes_unit_only_for_fibo(self):
        env = _fresh()
        self._tmp = env["tmp"]
        self.assertEqual(_run("install", ["trade", "fibo"], env), 0)
        self.assertTrue((env["systemd_dir"] / "fibo.service").is_file())
        self.assertEqual(_run("uninstall", ["fibo"], env), 0)
        self.assertFalse((env["systemd_dir"] / "fibo.service").exists())
        # trade payload still present
        self.assertTrue(
            (env["hermes_root"] / "plugins" / "trade" / "wizard.py").is_file()
        )

    def test_verify_fails_without_unit(self):
        env = _fresh()
        self._tmp = env["tmp"]
        self.assertEqual(_run("install", ["fibo"], env), 0)
        (env["systemd_dir"] / "fibo.service").unlink()
        self.assertNotEqual(_run("verify", ["fibo"], env), 0)

    def test_render_uses_hermes_paths_not_source_tree(self):
        env = _fresh()
        self._tmp = env["tmp"]
        text = render_fibo_unit(
            hermes_root=env["hermes_root"],
            hermes_home=env["hermes_home"],
            python_exe=env["hermes_root"] / "venv" / "bin" / "python",
        )
        self.assertIn(str(env["hermes_root"]), text)
        self.assertNotIn("{{", text)
        self.assertIn("-m plugins.trade.fibo_daemon", text)


if __name__ == "__main__":
    unittest.main()
