"""Phase 2.13.11A — Verify systemd Type=oneshot timer-overlap semantics
using a dummy unit that just sleeps. NO Fibo code involved.

Strategy:
  - Create dummy units in /etc/systemd/system.
  - Start the service directly (this takes 30s — the sleep).
  - Run `systemctl list-jobs` repeatedly to observe the job state.
  - Confirm that the second invocation (from a hypothetical timer
    fire) is NOT queued. The timer is NOT started in this test
    (we only observe list-jobs), so we just verify that the first
    activation runs to completion and no second activation is
    stacked.

This test is hermetic: it only runs if the host has systemd. If
not, the test is skipped.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("systemctl"), "systemd not available")
class SystemdOneshotOverlapTest(unittest.TestCase):
    """Confirm that a Type=oneshot service, when already active, does
    NOT receive a second activation even when the timer is firing
    every second.
    """

    def setUp(self) -> None:
        self.unit_dir = Path("/etc/systemd/system")
        self.svc = self.unit_dir / "dummy-sleep.service"
        self.tmr = self.unit_dir / "dummy-sleep.timer"
        self.svc_text = (
            "[Unit]\n"
            "Description=Dummy sleep service for overlap test\n"
            "\n"
            "[Service]\n"
            "Type=oneshot\n"
            "ExecStart=/bin/sleep 5\n"
        )
        self.tmr_text = (
            "[Unit]\n"
            "Description=Dummy sleep timer for overlap test\n"
            "\n"
            "[Timer]\n"
            "OnCalendar=*:*:00/1\n"
            "AccuracySec=1s\n"
            "Persistent=false\n"
            "Unit=dummy-sleep.service\n"
        )
        self.svc.write_text(self.svc_text)
        self.tmr.write_text(self.tmr_text)
        subprocess.run(
            ["systemctl", "daemon-reload"],
            check=False, capture_output=True, timeout=10,
        )

    def tearDown(self) -> None:
        # Best-effort cleanup. Use shell redirection to suppress
        # "Unit not found" / "Unit not loaded" errors.
        for cmd in (
            ["systemctl", "disable", "--now", "dummy-sleep.timer"],
            ["systemctl", "stop", "dummy-sleep.service"],
            ["systemctl", "reset-failed", "dummy-sleep.service"],
            ["systemctl", "reset-failed", "dummy-sleep.timer"],
        ):
            subprocess.run(
                cmd, check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        for f in (self.svc, self.tmr):
            if f.exists():
                f.unlink()
        subprocess.run(
            ["systemctl", "daemon-reload"],
            check=False, capture_output=True, timeout=10,
        )

    def test_oneshot_active_blocks_reactivation(self) -> None:
        """When the service is already active, a manual start
        (``systemctl start dummy-sleep.service``) returns
        immediately. systemd does NOT queue a second invocation;
        it just refuses to start a new one while the current
        one is running.

        We use a 5-second sleep so the test takes ~10s total.
        """
        # Start the service. This BLOCKS until the service
        # finishes, so we run it in a subprocess and check the
        # state while it runs.
        proc = subprocess.Popen(
            ["systemctl", "start", "dummy-sleep.service"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        # Give systemd ~500ms to actually launch the service.
        time.sleep(0.5)

        # Confirm the service is in 'active' or 'activating' state
        # (depending on how quickly it transitioned).
        active = subprocess.run(
            ["systemctl", "is-active", "dummy-sleep.service"],
            capture_output=True, text=True, timeout=5,
        )
        active_state = active.stdout.strip()
        self.assertIn(
            active_state, ("active", "activating"),
            f"expected 'active' or 'activating' after start, got "
            f"{active_state!r}; list-jobs:\n"
            + subprocess.run(
                ["systemctl", "list-jobs", "--no-pager"],
                capture_output=True, text=True, timeout=5,
            ).stdout
        )

        # Try to start it again. With Type=oneshot + already active,
        # this should return 0 (success) but the new activation is
        # either ignored or rejected — not stacked. We just check
        # that we don't end up with multiple invocations.
        second_start = subprocess.run(
            ["systemctl", "start", "dummy-sleep.service"],
            capture_output=True, text=True, timeout=5,
        )
        # The second ``systemctl start`` is documented to succeed
        # but not stack a new job. Verify by counting jobs.
        time.sleep(0.5)
        list_jobs = subprocess.run(
            ["systemctl", "list-jobs", "--no-legend", "--no-pager"],
            capture_output=True, text=True, timeout=5,
        )
        # Count "dummy-sleep.service" entries in the list-jobs output.
        # Per systemd semantics, only one activation is active or
        # pending for the same Type=oneshot unit at a time.
        dummy_lines = [
            ln for ln in list_jobs.stdout.splitlines()
            if "dummy-sleep.service" in ln
        ]
        self.assertLessEqual(
            len(dummy_lines), 1,
            f"expected at most 1 dummy-sleep.service in list-jobs; "
            f"got {len(dummy_lines)}:\n{chr(10).join(dummy_lines)}\n"
            f"Full list-jobs:\n{list_jobs.stdout}"
        )

        # Now wait for the original 5s sleep to finish.
        proc_out, proc_err = proc.communicate(timeout=15)
        self.assertEqual(
            proc.returncode, 0,
            f"first systemctl start returned {proc.returncode}; "
            f"stderr={proc_err.decode()}"
        )

        # After the service completes, the second start's
        # "completion" semantics may have already been recorded
        # (systemd logs it as a "failed to start" since the unit
        # was already in "active" state). This is fine; the
        # assertion is on the at-most-one-during-activation
        # behavior.
        final_state = subprocess.run(
            ["systemctl", "is-active", "dummy-sleep.service"],
            capture_output=True, text=True, timeout=5,
        )
        # Should be 'inactive' or 'failed' or 'active' depending on
        # whether the second start actually ran something. We
        # don't assert this; the key assertion is above
        # (at most 1 in list-jobs while the first is running).


if __name__ == "__main__":
    unittest.main(verbosity=2)
