"""Fresh-install Telegram adapter wiring regression tests (Lodo bug).

Proves that the modular installer applies capability-aware Telegram
adapter dispatch seams on a pristine Hermes tree with NO pre-existing
/trade or /fibo wiring.

Coverage:
  - --trade only: trade seams present, fibo seams absent
  - --fibo only: fibo seams present, trade seams absent
  - --trade --fibo: both present
  - repeated install: idempotent, no duplicate markers
  - partial uninstall: other capability seams survive
  - verify fails when seams are missing (Lodo gate)
"""

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

import adapter_wiring as AW  # noqa: E402
import capabilities as C  # noqa: E402
from patchspecs import TELEGRAM_ADAPTER  # noqa: E402

# Fixture lives next to this test module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pristine_telegram_adapter import PRISTINE_TELEGRAM_ADAPTER  # noqa: E402

INST = importlib.import_module("installer.installer")


def _fresh_hermes() -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="kam-adapter-itest-"))
    hermes_home = tmp / "hermes_home"
    hermes_root = tmp / "hermes_root"
    hermes_home.mkdir(parents=True)
    hermes_root.mkdir(parents=True)
    adapter = hermes_root / TELEGRAM_ADAPTER
    adapter.parent.mkdir(parents=True, exist_ok=True)
    adapter.write_text(PRISTINE_TELEGRAM_ADAPTER, encoding="utf-8")
    # Minimal config so plugins.enabled can be edited.
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled: []\n",
        encoding="utf-8",
    )
    return {"tmp": tmp, "hermes_home": hermes_home, "hermes_root": hermes_root}


def _adapter_text(hermes_root: Path) -> str:
    return (hermes_root / TELEGRAM_ADAPTER).read_text(encoding="utf-8")


def _run_install(caps: List[str], hermes_root: Path, hermes_home: Path) -> int:
    argv = []
    for c in caps:
        argv.append(f"--{c}")
    argv.extend(
        [
            "--hermes-root",
            str(hermes_root),
            "--hermes-home",
            str(hermes_home),
            "--skip-deps",  # unit tests cover files/wiring; deps are separate
            "--action",
            "install",
        ]
    )
    return INST.main(argv)


def _run_verify(caps: List[str], hermes_root: Path, hermes_home: Path) -> int:
    argv = []
    for c in caps:
        argv.append(f"--{c}")
    argv.extend(
        [
            "--hermes-root",
            str(hermes_root),
            "--hermes-home",
            str(hermes_home),
            "--action",
            "verify",
        ]
    )
    return INST.main(argv)


def _run_uninstall(caps: List[str], hermes_root: Path, hermes_home: Path) -> int:
    argv = []
    for c in caps:
        argv.append(f"--{c}")
    argv.extend(
        [
            "--hermes-root",
            str(hermes_root),
            "--hermes-home",
            str(hermes_home),
            "--action",
            "uninstall",
        ]
    )
    return INST.main(argv)


class FreshInstallAdapterWiringTests(unittest.TestCase):
    def tearDown(self) -> None:
        tmp = getattr(self, "_tmp", None)
        if tmp and Path(tmp).is_dir():
            shutil.rmtree(tmp, ignore_errors=True)

    def _boot(self):
        env = _fresh_hermes()
        self._tmp = env["tmp"]
        self.assertNotIn("handle_trade_command", _adapter_text(env["hermes_root"]))
        self.assertNotIn("handle_fibo_command", _adapter_text(env["hermes_root"]))
        return env

    def test_trade_only_wires_trade_not_fibo(self):
        env = self._boot()
        rc = _run_install(["trade"], env["hermes_root"], env["hermes_home"])
        self.assertEqual(rc, 0)
        text = _adapter_text(env["hermes_root"])
        fails = AW.assert_capability_seams(text, trade=True, fibo=False)
        self.assertEqual(fails, [], fails)
        self.assertEqual(
            text.count("from plugins.trade.wizard import handle_trade_command"), 1
        )
        self.assertEqual(
            _run_verify(["trade"], env["hermes_root"], env["hermes_home"]), 0
        )

    def test_fibo_only_wires_fibo_not_trade(self):
        env = self._boot()
        rc = _run_install(["fibo"], env["hermes_root"], env["hermes_home"])
        self.assertEqual(rc, 0)
        text = _adapter_text(env["hermes_root"])
        fails = AW.assert_capability_seams(text, trade=False, fibo=True)
        self.assertEqual(fails, [], fails)
        self.assertEqual(
            text.count("from plugins.trade.fibo_wizard import handle_fibo_command"), 1
        )
        self.assertEqual(
            _run_verify(["fibo"], env["hermes_root"], env["hermes_home"]), 0
        )

    def test_trade_and_fibo_wires_both(self):
        env = self._boot()
        rc = _run_install(["trade", "fibo"], env["hermes_root"], env["hermes_home"])
        self.assertEqual(rc, 0)
        text = _adapter_text(env["hermes_root"])
        fails = AW.assert_capability_seams(text, trade=True, fibo=True)
        self.assertEqual(fails, [], fails)
        # Both namespaces
        self.assertIn('data.startswith("trade:")', text)
        self.assertIn('data.startswith("fibo:")', text)
        self.assertEqual(
            _run_verify(["trade", "fibo"], env["hermes_root"], env["hermes_home"]), 0
        )

    def test_repeated_install_is_idempotent(self):
        env = self._boot()
        self.assertEqual(
            _run_install(["trade", "fibo"], env["hermes_root"], env["hermes_home"]), 0
        )
        text1 = _adapter_text(env["hermes_root"])
        self.assertEqual(
            _run_install(["trade", "fibo"], env["hermes_root"], env["hermes_home"]), 0
        )
        text2 = _adapter_text(env["hermes_root"])
        self.assertEqual(text1, text2, "second install must not mutate adapter")
        self.assertEqual(
            text2.count("from plugins.trade.wizard import handle_trade_command"), 1
        )
        self.assertEqual(
            text2.count("from plugins.trade.fibo_wizard import handle_fibo_command"), 1
        )
        # Markers once each
        self.assertEqual(text2.count("BEGIN KAM TRADE PLUGIN (slash command dispatch)"), 1)
        self.assertEqual(text2.count("BEGIN KAM TRADE PLUGIN (fibo slash command dispatch)"), 1)

    def test_uninstall_trade_keeps_fibo_seams(self):
        env = self._boot()
        self.assertEqual(
            _run_install(["trade", "fibo"], env["hermes_root"], env["hermes_home"]), 0
        )
        self.assertEqual(
            _run_uninstall(["trade"], env["hermes_root"], env["hermes_home"]), 0
        )
        text = _adapter_text(env["hermes_root"])
        fails = AW.assert_capability_seams(text, trade=False, fibo=True)
        self.assertEqual(fails, [], fails)
        # fibo still verifies
        self.assertEqual(
            _run_verify(["fibo"], env["hermes_root"], env["hermes_home"]), 0
        )

    def test_uninstall_fibo_keeps_trade_seams(self):
        env = self._boot()
        self.assertEqual(
            _run_install(["trade", "fibo"], env["hermes_root"], env["hermes_home"]), 0
        )
        self.assertEqual(
            _run_uninstall(["fibo"], env["hermes_root"], env["hermes_home"]), 0
        )
        text = _adapter_text(env["hermes_root"])
        fails = AW.assert_capability_seams(text, trade=True, fibo=False)
        self.assertEqual(fails, [], fails)

    def test_verify_fails_when_adapter_unwired(self):
        """The Lodo failure mode: payload installed, adapter pristine."""
        env = self._boot()
        # Install payload files only (bypass adapter wiring).
        import installer_shared as IS
        import install_trade_capability as IT
        import install_fibo_capability as IF

        shared = IS.install_shared(
            argv=[],
            hermes_root=env["hermes_root"],
            hermes_home=env["hermes_home"],
            capabilities=["trade", "fibo"],
            dry_run=False,
        )
        IT.run(
            argv=[],
            hermes_root=env["hermes_root"],
            hermes_home=env["hermes_home"],
            shared=shared,
            dry_run=False,
        )
        IF.run(
            argv=[],
            hermes_root=env["hermes_root"],
            hermes_home=env["hermes_home"],
            shared=shared,
            dry_run=False,
        )
        # Mark capabilities installed in manifest without wiring.
        man = C.load_manifest(env["hermes_home"])
        C.set_capability(man, "trade", {"ok": True})
        C.set_capability(man, "fibo", {"ok": True})
        C.save_manifest(env["hermes_home"], man)
        # Adapter still pristine → verify MUST fail.
        self.assertNotIn("handle_trade_command", _adapter_text(env["hermes_root"]))
        rc = _run_verify(["trade", "fibo"], env["hermes_root"], env["hermes_home"])
        self.assertNotEqual(rc, 0, "verify must fail when adapter seams missing")


if __name__ == "__main__":
    unittest.main()
