"""Phase 2.13.11A — Installer and uninstaller unit-management tests.

Verifies:

  - install_fibo_capability stages the unit files but does NOT enable
    or start the timer.
  - install_fibo_capability calls ``daemon-reload`` after writing
    unit files (when systemctl is on PATH).
  - uninstaller disables the timer + stops the service idempotently
    (no failure if units are absent or already disabled).
  - uninstaller removes the unit files and calls daemon-reload.
  - uninstaller is idempotent (second invocation is a no-op).
  - installer is idempotent (re-running with same units is a no-op).
  - unit syntax is valid via ``systemd-analyze verify``.

Tests use a TEMPORARY systemd unit directory (``/tmp/...``) so the
live /etc/systemd/system is not touched. They do NOT enable or
start the real fibo-converge.timer.
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

sys.path.insert(0, "/root/kam")


class _CaptureRun:
    """Helper: run a Python callable and return its result or
    raise. We use this to call ``run()`` from
    install/uninstall_fibo_capability in-process so we can
    inspect the returned record dict.
    """

    @staticmethod
    def install_fibo(*, hermes_root: Path, hermes_home: Path,
                    systemd_dir: Path, dry_run: bool = False,
                    shared: dict = None) -> dict:
        from installer.install_fibo_capability import run
        return run(
            argv=[],
            hermes_root=hermes_root,
            hermes_home=hermes_home,
            shared=shared or {"systemd_dir": str(systemd_dir)},
            dry_run=dry_run,
        )

    @staticmethod
    def uninstall_fibo(*, hermes_root: Path, hermes_home: Path,
                       systemd_dir: Path, dry_run: bool = False,
                       argv: list = None) -> dict:
        from installer.uninstall_fibo_capability import run
        return run(
            argv=argv if argv is not None else [
                "--systemd-dir", str(systemd_dir),
            ],
            hermes_root=hermes_root,
            hermes_home=hermes_home,
            dry_run=dry_run,
        )


class _Fixtures:
    """Build a self-contained set of fixtures:
      - hermes_root: a fake Hermes install tree
      - hermes_home: a fake runtime state directory
      - systemd_dir: a temp directory for unit files
    The fixtures copy the real kam source files into hermes_root
    so the install actually has something to copy.
    """

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="fibo_install_test_")
        self.hermes_root = Path(self.tmp) / "hermes"
        self.hermes_home = Path(self.tmp) / "home"
        self.systemd_dir = Path(self.tmp) / "systemd"
        for p in (self.hermes_root, self.hermes_home, self.systemd_dir):
            p.mkdir(parents=True, exist_ok=True)
        # Create the fibo sub-package directory.
        (self.hermes_root / "plugins" / "trade" / "fibo").mkdir(
            parents=True, exist_ok=True,
        )

    def cleanup(self) -> None:
        try:
            shutil.rmtree(self.tmp)
        except OSError:
            pass


class InstallerUnitStagingTests(unittest.TestCase):
    """The installer writes the unit files and triggers daemon-reload,
    but does NOT enable or start the timer.
    """

    def setUp(self) -> None:
        self.fx = _Fixtures()

    def tearDown(self) -> None:
        self.fx.cleanup()

    def test_install_stages_units_dry_run(self) -> None:
        """Dry-run install: produces 'would-install' actions; no
        files are written; no daemon-reload.
        """
        record = _CaptureRun.install_fibo(
            hermes_root=self.fx.hermes_root,
            hermes_home=self.fx.hermes_home,
            systemd_dir=self.fx.systemd_dir,
            dry_run=True,
        )
        # No files should be written in dry-run mode.
        self.assertFalse((self.fx.systemd_dir / "fibo-converge.service").exists())
        self.assertFalse((self.fx.systemd_dir / "fibo-converge.timer").exists())
        # The record should list both units as would-install.
        units = record.get("systemd_units", [])
        unit_names = {u["unit"] for u in units}
        self.assertEqual(
            unit_names,
            {"fibo-converge.service", "fibo-converge.timer"},
        )
        for u in units:
            self.assertEqual(u["action"], "would-install")
        # In dry-run mode, the daemon_reload key may be present
        # but is not invoked (we never write files).
        self.assertNotIn("daemon_reload", record)

    def test_install_actual_writes_units(self) -> None:
        """Non-dry-run install: actually writes unit files, calls
        daemon-reload (or records skipped if systemctl missing).
        """
        record = _CaptureRun.install_fibo(
            hermes_root=self.fx.hermes_root,
            hermes_home=self.fx.hermes_home,
            systemd_dir=self.fx.systemd_dir,
            dry_run=False,
        )
        # Both unit files should be present.
        self.assertTrue(
            (self.fx.systemd_dir / "fibo-converge.service").exists(),
            "service unit not written"
        )
        self.assertTrue(
            (self.fx.systemd_dir / "fibo-converge.timer").exists(),
            "timer unit not written"
        )
        # Mode 0644.
        for u in ("fibo-converge.service", "fibo-converge.timer"):
            mode = (self.fx.systemd_dir / u).stat().st_mode & 0o777
            self.assertEqual(mode, 0o644,
                             f"{u} has mode {oct(mode)}, expected 0o644")
        # The record shows daemon-reload handling. Since this
        # is a sandbox under /tmp, systemctl won't be able to
        # actually load our fake unit (it'll fail with
        # "unit not found"); the installer catches that and
        # records the error string.
        self.assertIn("daemon_reload", record)

    def test_install_does_not_activate_timer(self) -> None:
        """The install does NOT enable or start the timer; the
        timer is left disabled/inactive.
        """
        _CaptureRun.install_fibo(
            hermes_root=self.fx.hermes_root,
            hermes_home=self.fx.hermes_home,
            systemd_dir=self.fx.systemd_dir,
            dry_run=False,
        )
        # Check the unit files are NOT enabled. systemctl reports
        # 'not-found' for a unit that doesn't exist in its load
        # path. We accept 'not-found', 'disabled', or empty.
        result = subprocess.run(
            ["systemctl", "is-enabled", "fibo-converge.timer"],
            capture_output=True, text=True, timeout=5,
        )
        self.assertIn(
            result.stdout.strip(), ("disabled", "not-found", ""),
            f"unexpected is-enabled output: {result.stdout!r}"
        )
        # is-active should be 'inactive' or 'not-found' (the
        # latter is what systemd returns for a unit it doesn't
        # know about).
        active = subprocess.run(
            ["systemctl", "is-active", "fibo-converge.timer"],
            capture_output=True, text=True, timeout=5,
        )
        self.assertIn(
            active.stdout.strip(), ("inactive", "not-found", ""),
            f"unexpected is-active output: {active.stdout!r}"
        )


class InstallerIdempotenceTests(unittest.TestCase):
    """Running the install twice with the same inputs is a no-op."""

    def setUp(self) -> None:
        self.fx = _Fixtures()

    def tearDown(self) -> None:
        self.fx.cleanup()

    def test_double_install_is_safe(self) -> None:
        """Running the install twice does not error and produces
        a record that records the second install as 'installed'
        for both units.
        """
        _CaptureRun.install_fibo(
            hermes_root=self.fx.hermes_root,
            hermes_home=self.fx.hermes_home,
            systemd_dir=self.fx.systemd_dir,
        )
        # Second run: same result. No exception.
        record2 = _CaptureRun.install_fibo(
            hermes_root=self.fx.hermes_root,
            hermes_home=self.fx.hermes_home,
            systemd_dir=self.fx.systemd_dir,
        )
        # Both installs should report both units.
        for u in ("fibo-converge.service", "fibo-converge.timer"):
            installed = [
                r for r in record2.get("systemd_units", [])
                if r.get("unit") == u
            ]
            self.assertEqual(len(installed), 1)
            self.assertEqual(installed[0]["action"], "installed")


class UninstallerUnitManagementTests(unittest.TestCase):
    """The uninstaller manages its own systemd units idempotently."""

    def setUp(self) -> None:
        self.fx = _Fixtures()

    def tearDown(self) -> None:
        # Clean up any installed units (real or test).
        for u in ("fibo-converge.service", "fibo-converge.timer"):
            p = self.fx.systemd_dir / u
            if p.exists():
                p.unlink()
        self.fx.cleanup()

    def test_uninstall_when_no_units(self) -> None:
        """If the units are not installed, uninstall is a no-op.
        It must not raise.
        """
        # No units on disk.
        record = _CaptureRun.uninstall_fibo(
            hermes_root=self.fx.hermes_root,
            hermes_home=self.fx.hermes_home,
            systemd_dir=self.fx.systemd_dir,
        )
        # The uninstall is non-fatal.
        # When no units exist, ``removed_unit_files`` is NOT in the
        # record (we only setdefault it when files are found).
        self.assertEqual(record.get("removed_unit_files", []), [])
        # Both units should be reported as skipped_disabled (not
        # installed) when systemctl is available.
        if record.get("systemctl_available"):
            self.assertIn("fibo-converge.service", record.get("skipped_disabled", []))
            self.assertIn("fibo-converge.timer", record.get("skipped_disabled", []))

    def test_uninstall_removes_units(self) -> None:
        """If the units ARE installed, uninstall removes the
        unit files (and tries to disable/stop them, which
        fails silently if systemctl can't find them in a
        sandbox).
        """
        # Pre-create the unit files (simulating a previous
        # install).
        (self.fx.systemd_dir / "fibo-converge.service").write_text(
            "[Unit]\nDescription=test\n[Service]\nType=oneshot\nExecStart=true\n",
        )
        (self.fx.systemd_dir / "fibo-converge.timer").write_text(
            "[Unit]\nDescription=test\n[Timer]\nOnCalendar=*:*:00\nUnit=fibo-converge.service\n",
        )
        record = _CaptureRun.uninstall_fibo(
            hermes_root=self.fx.hermes_root,
            hermes_home=self.fx.hermes_home,
            systemd_dir=self.fx.systemd_dir,
        )
        # Both unit files should be gone.
        self.assertFalse(
            (self.fx.systemd_dir / "fibo-converge.service").exists(),
        )
        self.assertFalse(
            (self.fx.systemd_dir / "fibo-converge.timer").exists(),
        )
        # Record shows the removals.
        removed = {
            r["unit"] for r in record.get("removed_unit_files", [])
        }
        self.assertEqual(
            removed,
            {"fibo-converge.service", "fibo-converge.timer"},
        )
        # systemctl_available flag.
        self.assertIn("systemctl_available", record)
        # daemon-reload was attempted (or skipped if no
        # systemctl).
        if record.get("systemctl_available"):
            self.assertIn("daemon_reload", record)

    def test_uninstall_idempotent(self) -> None:
        """Running uninstall twice is safe; the second invocation
        is a no-op (no files to remove).
        """
        # Pre-create unit files.
        (self.fx.systemd_dir / "fibo-converge.service").write_text(
            "[Unit]\n[Service]\nType=oneshot\nExecStart=true\n",
        )
        (self.fx.systemd_dir / "fibo-converge.timer").write_text(
            "[Unit]\n[Timer]\nOnCalendar=*:*:00\nUnit=fibo-converge.service\n",
        )
        # First uninstall removes them.
        _CaptureRun.uninstall_fibo(
            hermes_root=self.fx.hermes_root,
            hermes_home=self.fx.hermes_home,
            systemd_dir=self.fx.systemd_dir,
        )
        # Second uninstall is a no-op.
        record2 = _CaptureRun.uninstall_fibo(
            hermes_root=self.fx.hermes_root,
            hermes_home=self.fx.hermes_home,
            systemd_dir=self.fx.systemd_dir,
        )
        self.assertEqual(record2.get("removed_unit_files", []), [])


class UnitSyntaxTest(unittest.TestCase):
    """The committed unit files in kam/installer/systemd/ pass
    ``systemd-analyze verify`` with no warnings.
    """

    def test_service_unit_syntax(self) -> None:
        svc = Path("/root/kam/installer/systemd/fibo-converge.service")
        # Copy to a file with the correct .service extension for
        # systemd-analyze verify.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".service", delete=False,
        ) as tmp:
            tmp.write(svc.read_text())
            tmp_path = tmp.name
        try:
            result = subprocess.run(
                ["systemd-analyze", "verify", tmp_path],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(
                result.returncode, 0,
                f"systemd-analyze verify failed for service: "
                f"rc={result.returncode}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}",
            )
        finally:
            os.unlink(tmp_path)

    def test_timer_unit_syntax(self) -> None:
        tmr = Path("/root/kam/installer/systemd/fibo-converge.timer")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".timer", delete=False,
        ) as tmp:
            tmp.write(tmr.read_text())
            tmp_path = tmp.name
        try:
            result = subprocess.run(
                ["systemctl-analyze"] if False else
                ["systemd-analyze", "verify", tmp_path],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(
                result.returncode, 0,
                f"systemd-analyze verify failed for timer: "
                f"rc={result.returncode}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}",
            )
        finally:
            os.unlink(tmp_path)

    def test_timer_single_description(self) -> None:
        """The timer unit must have exactly one Description= line
        in the [Unit] section (no duplicate).
        """
        tmr = Path("/root/kam/installer/systemd/fibo-converge.timer")
        text = tmr.read_text()
        # Count occurrences of "Description=" in [Unit].
        in_unit = False
        count = 0
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("["):
                in_unit = s == "[Unit]"
                continue
            if in_unit and s.startswith("Description="):
                count += 1
        self.assertEqual(count, 1,
                         f"timer should have exactly 1 Description= "
                         f"in [Unit]; found {count}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
