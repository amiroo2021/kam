"""Fresh-install packaging must stage lifecycle runtime modules.

Isolated empty destination — never touches /usr/local/lib/hermes-agent.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from installer.install_fibo_capability import FIBO_REL_PATHS, run as install_fibo  # noqa: E402
from installer.uninstall_fibo_capability import FIBO_REL_PATHS as UN_PATHS  # noqa: E402


REQUIRED = (
    "plugins/trade/fibo/timer_lifecycle.py",
    "plugins/trade/fibo/lifecycle.py",
)


class FreshInstallLifecyclePayloadTests(unittest.TestCase):
    def test_payload_lists_both_lifecycle_modules(self) -> None:
        names = {str(p).replace("\\", "/") for p in FIBO_REL_PATHS}
        for req in REQUIRED:
            self.assertIn(req, names, f"install payload missing {req}")
        un = {str(p).replace("\\", "/") for p in UN_PATHS}
        for req in REQUIRED:
            self.assertIn(req, un, f"uninstall payload missing {req}")

    def test_fresh_empty_destination_stages_and_imports(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fibo_fresh_") as td:
            hermes_root = Path(td) / "hermes"
            hermes_home = Path(td) / "home"
            hermes_root.mkdir()
            hermes_home.mkdir()
            # Empty destination — no pre-existing plugins.
            self.assertFalse((hermes_root / "plugins").exists())

            record = install_fibo(
                argv=[],
                hermes_root=hermes_root,
                hermes_home=hermes_home,
                shared={"systemd_dir": ""},  # skip systemd
                dry_run=False,
            )
            self.assertTrue(record.get("ok"), record)
            paths = {c["path"].replace("\\", "/") for c in record["copied_files"]}
            for req in REQUIRED:
                self.assertIn(req, paths)
                dst = hermes_root / req
                self.assertTrue(dst.is_file(), f"not staged: {dst}")
                # Must come from repo, not live tree.
                self.assertTrue(dst.read_text(encoding="utf-8"))

            # Import from the staged empty tree only.
            fibo_dir = hermes_root / "plugins" / "trade" / "fibo"
            # Minimal package shell so relative imports resolve.
            trade_dir = hermes_root / "plugins" / "trade"
            for pkg in (
                hermes_root / "plugins",
                trade_dir,
                fibo_dir,
            ):
                init = pkg / "__init__.py"
                if not init.exists():
                    init.write_text("# test package marker\n", encoding="utf-8")

            sys_path_prev = list(sys.path)
            # Drop live hermes install paths so import cannot cheat.
            cleaned = [
                p for p in sys.path
                if "hermes-agent" not in p and p != "/usr/local/lib/hermes-agent"
            ]
            cleaned.insert(0, str(hermes_root))
            # Keep repo only for installer, not for fibo package — remove repo
            # plugins.trade if present by using hermes_root first.
            sys.path[:] = cleaned
            # Clear cached plugins modules
            for k in list(sys.modules):
                if k == "plugins" or k.startswith("plugins."):
                    sys.modules.pop(k, None)
            try:
                import plugins.trade.fibo.timer_lifecycle as tl  # noqa: F401
                import plugins.trade.fibo.lifecycle as lc  # noqa: F401
                self.assertTrue(hasattr(tl, "reconcile_convergence_timer"))
                self.assertTrue(hasattr(lc, "lifecycle_append"))
            finally:
                sys.path[:] = sys_path_prev
                for k in list(sys.modules):
                    if k == "plugins" or k.startswith("plugins."):
                        sys.modules.pop(k, None)

    def test_dry_run_would_copy_lifecycle_modules(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fibo_dry_") as td:
            hermes_root = Path(td) / "hermes"
            hermes_home = Path(td) / "home"
            hermes_root.mkdir()
            hermes_home.mkdir()
            record = install_fibo(
                argv=[],
                hermes_root=hermes_root,
                hermes_home=hermes_home,
                shared={"systemd_dir": ""},
                dry_run=True,
            )
            self.assertTrue(record.get("ok"))
            paths = {c["path"].replace("\\", "/") for c in record["copied_files"]}
            for req in REQUIRED:
                self.assertIn(req, paths)
            # Dry-run must not create files.
            self.assertFalse((hermes_root / "plugins").exists())


if __name__ == "__main__":
    unittest.main()
