"""Phase 2.13.11 — singleton-lock regression tests.

Verifies the Fibo-owned ``fcntl.flock`` advisory lock:

  1. First invocation acquires the lock.
  2. Second concurrent invocation cannot acquire the lock.
  3. Second invocation performs ZERO TradeDesk calls.
  4. First invocation releases the lock on normal exit.
  5. Lock releases after exception / process exit.
  6. Subsequent invocation can acquire after release.

Tests use a TEMPORARY lock path (no touch on the live
``/root/.hermes/fibo/converge.lock``) by monkey-patching
``singleton_lock._lock_path`` to return a tmp-file path.

Tests use ``threading`` to spawn concurrent invocations within ONE
process. Because ``flock`` is per-inode and a separate ``open()`` per
thread creates a separate file descriptor, threads DO contend on the
kernel-level lock (Linux flock semantics: advisory lock per inode,
held by any fd that opened the file).
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "/root/kam")
from plugins.trade.fibo import singleton_lock as sl  # noqa: E402


class _PathOverride:
    """Context manager that patches ``singleton_lock._lock_path``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._original = None

    def __enter__(self) -> Path:
        self._original = sl._lock_path
        sl._lock_path = lambda: self.path  # type: ignore[assignment]
        return self.path

    def __exit__(self, exc_type, exc, tb) -> None:
        sl._lock_path = self._original  # type: ignore[assignment]


class SingletonLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="fibo_lock_test_")
        self.lock_path = Path(self.tmp) / "converge.lock"

    def tearDown(self) -> None:
        import shutil
        try:
            shutil.rmtree(self.tmp)
        except OSError:
            pass

    # --- 1. first invocation acquires the lock ---
    def test_first_invocation_acquires_lock(self) -> None:
        with _PathOverride(self.lock_path):
            with sl.acquire_singleton_lock() as lock:
                self.assertTrue(
                    lock.acquired,
                    f"first invocation should acquire; reason={lock.reason!r}",
                )
                self.assertEqual(lock.path, self.lock_path)
                self.assertIsNotNone(lock.fd)

    # --- 2. second concurrent invocation cannot acquire ---
    def test_second_concurrent_invocation_cannot_acquire(self) -> None:
        with _PathOverride(self.lock_path):
            # First thread holds the lock; second thread must fail.
            barrier_holder_held = threading.Event()
            barrier_second_attempted = threading.Event()
            second_result: dict = {}

            def holder_thread() -> None:
                with sl.acquire_singleton_lock() as lock:
                    if not lock.acquired:
                        second_result["holder"] = "FAILED_TO_ACQUIRE"
                        barrier_second_attempted.set()
                        return
                    barrier_holder_held.set()
                    # Hold the lock until the second thread has
                    # attempted (and failed) to acquire it.
                    barrier_second_attempted.wait(timeout=2.0)
                    # Exit releases the lock via __exit__.

            def contender_thread() -> None:
                barrier_holder_held.wait(timeout=2.0)
                with sl.acquire_singleton_lock() as lock:
                    second_result["acquired"] = lock.acquired
                    second_result["reason"] = lock.reason
                    second_result["path"] = lock.path
                    barrier_second_attempted.set()

            t1 = threading.Thread(target=holder_thread, daemon=True)
            t2 = threading.Thread(target=contender_thread, daemon=True)
            t1.start()
            t2.start()
            t2.join(timeout=5.0)
            t1.join(timeout=5.0)
            self.assertFalse(second_result.get("acquired", True))
            self.assertIn("progress", second_result.get("reason", ""))

    # --- 3. second invocation performs ZERO TradeDesk calls ---
    def test_second_invocation_performs_zero_tradedesk_calls(self) -> None:
        """The second invocation must exit cleanly BEFORE the
        TradeDesk acquisition. We use a no-op stand-in for
        TradeDesk and verify it is never called.
        """
        with _PathOverride(self.lock_path):
            tradedesk_calls: list = []

            def fake_tradedesk_call() -> None:
                tradedesk_calls.append("CALLED")

            def holder_thread() -> None:
                with sl.acquire_singleton_lock() as lock:
                    if lock.acquired:
                        time.sleep(0.5)
                    # No TradeDesk call from holder.

            def contender_thread() -> None:
                time.sleep(0.1)
                with sl.acquire_singleton_lock() as lock:
                    if lock.acquired:
                        # This branch must NOT execute; if it
                        # does, TradeDesk would be called.
                        fake_tradedesk_call()
                    # If not acquired, we exit immediately
                    # (zero TradeDesk calls).

            t1 = threading.Thread(target=holder_thread, daemon=True)
            t2 = threading.Thread(target=contender_thread, daemon=True)
            t1.start()
            t2.start()
            t2.join(timeout=5.0)
            t1.join(timeout=5.0)
            self.assertEqual(
                tradedesk_calls, [],
                f"second invocation must NOT call TradeDesk; got {tradedesk_calls!r}",
            )

    # --- 4. first invocation releases lock on normal exit ---
    def test_first_invocation_releases_on_normal_exit(self) -> None:
        with _PathOverride(self.lock_path):
            with sl.acquire_singleton_lock() as lock:
                self.assertTrue(lock.acquired)
            # After context exit, the fd is closed and the kernel
            # releases the lock. A subsequent acquisition should
            # succeed.
            with sl.acquire_singleton_lock() as lock2:
                self.assertTrue(
                    lock2.acquired,
                    f"second acquire after normal exit should succeed; reason={lock2.reason!r}",
                )

    # --- 5. lock releases after exception / process exit ---
    def test_lock_releases_after_exception(self) -> None:
        with _PathOverride(self.lock_path):
            with self.assertRaises(RuntimeError):
                with sl.acquire_singleton_lock() as lock:
                    self.assertTrue(lock.acquired)
                    raise RuntimeError("simulated failure inside lock block")
            # After exception, the lock should be released. The
            # subsequent acquire should succeed.
            with sl.acquire_singleton_lock() as lock2:
                self.assertTrue(
                    lock2.acquired,
                    f"after exception, lock should release; reason={lock2.reason!r}",
                )

    # --- 6. subsequent invocation can acquire after release ---
    def test_subsequent_acquire_after_release(self) -> None:
        with _PathOverride(self.lock_path):
            # First cycle
            with sl.acquire_singleton_lock() as lock1:
                self.assertTrue(lock1.acquired)
            # Second cycle
            with sl.acquire_singleton_lock() as lock2:
                self.assertTrue(
                    lock2.acquired,
                    f"second cycle should acquire; reason={lock2.reason!r}",
                )
            # Third cycle
            with sl.acquire_singleton_lock() as lock3:
                self.assertTrue(lock3.acquired)

    # --- Probe helper ---
    def test_is_lock_held_when_no_lock(self) -> None:
        with _PathOverride(self.lock_path):
            self.assertFalse(sl.is_lock_held())

    def test_is_lock_held_while_held(self) -> None:
        with _PathOverride(self.lock_path):
            with sl.acquire_singleton_lock() as lock:
                self.assertTrue(lock.acquired)
                # Probe from a different fd in the same process.
                self.assertTrue(sl.is_lock_held())
            self.assertFalse(sl.is_lock_held())


class SingletonLockSubprocessTests(unittest.TestCase):
    """Subprocess-based test that proves the lock is held across
    process boundaries. The flock is per-inode, so a separate
    process that opens the same path contends on the lock.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="fibo_lock_subproc_")
        self.lock_path = Path(self.tmp) / "converge.lock"

    def tearDown(self) -> None:
        import shutil
        try:
            shutil.rmtree(self.tmp)
        except OSError:
            pass

    def test_subprocess_contention(self) -> None:
        """Spawn a subprocess that holds the lock for 2s; spawn a
        second subprocess that attempts to acquire; verify the
        second subprocess observes ``acquired=False``.
        """
        # Write a tiny helper script that acquires the lock, holds
        # for 2s, then exits.
        holder_script = (
            "import sys, time, os\n"
            f"sys.path.insert(0, '/root/kam')\n"
            "from plugins.trade.fibo import singleton_lock as sl\n"
            f"sl._lock_path = lambda: __import__('pathlib').Path('{self.lock_path}')\n"
            "with sl.acquire_singleton_lock() as lock:\n"
            "    if not lock.acquired:\n"
            "        print('HOLDER_FAILED')\n"
            "        sys.exit(1)\n"
            "    print('HOLDER_ACQUIRED')\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(2.0)\n"
            "print('HOLDER_DONE')\n"
        )
        contender_script = (
            "import sys, time, os\n"
            f"sys.path.insert(0, '/root/kam')\n"
            "from plugins.trade.fibo import singleton_lock as sl\n"
            f"sl._lock_path = lambda: __import__('pathlib').Path('{self.lock_path}')\n"
            "with sl.acquire_singleton_lock() as lock:\n"
            "    print('CONTENDER_ACQUIRED', lock.acquired, lock.reason)\n"
        )
        import subprocess
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        # Wait for the holder to acquire.
        time.sleep(0.5)
        contender = subprocess.run(
            [sys.executable, "-c", contender_script],
            capture_output=True, text=True, timeout=5,
        )
        # Join the holder to clean up.
        holder_out, holder_err = holder.communicate(timeout=5)
        self.assertIn("CONTENDER_ACQUIRED False", contender.stdout)
        self.assertIn("HOLDER_ACQUIRED", holder_out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
