"""Regression: fibo_daemon must exit promptly on SIGTERM/SIGINT.

Production bug (2026-08-19/20): systemd stop sent SIGTERM, the handler
called ``host.shutdown()`` on the same thread as ``serve_forever()``,
``ThreadingUnixStreamServer.shutdown()`` blocked forever, and systemd
SIGKILLed after TimeoutStopUSec=90s (Result=timeout).

These tests drive the real daemon entrypoint in a subprocess so the
signal is delivered the same way systemd does.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent


def _wait_socket(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(0.2)
                    sock.connect(str(path))
                return
            except OSError:
                pass
        time.sleep(0.05)
    raise TimeoutError(f"daemon socket not ready: {path}")


class FiboDaemonSigtermTests(unittest.TestCase):
    def _spawn(self, tmp: Path) -> subprocess.Popen:
        socket_path = tmp / "service.sock"
        cmd = [
            sys.executable,
            "-m",
            "plugins.trade.fibo_daemon",
            "--socket-path",
            str(socket_path),
            "--state-path",
            str(tmp / "service_state.json"),
            "--ledger-path",
            str(tmp / "service_ledger.jsonl"),
            "--event-log-path",
            str(tmp / "service-events.log"),
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["HERMES_HOME"] = str(tmp)
        proc = subprocess.Popen(
            cmd,
            cwd=str(_REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_socket(socket_path, timeout=8.0)
        except Exception:
            proc.kill()
            out, err = proc.communicate(timeout=2)
            raise RuntimeError(
                f"daemon failed to start\nstdout={out!r}\nstderr={err!r}"
            )
        return proc

    def test_sigterm_exits_promptly_and_unlinks_socket(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            socket_path = tmp / "service.sock"
            proc = self._spawn(tmp)
            self.assertTrue(socket_path.exists())
            t0 = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
                self.fail("fibo_daemon did not exit within 5s of SIGTERM (shutdown deadlock)")
            elapsed = time.monotonic() - t0
            self.assertLess(
                elapsed,
                5.0,
                f"SIGTERM shutdown took {elapsed:.2f}s; must be well under systemd TimeoutStopUSec=90s",
            )
            self.assertIsNotNone(proc.returncode)
            self.assertFalse(
                socket_path.exists(),
                "Unix socket must be unlinked after graceful stop",
            )

    def test_repeated_sigterm_is_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            socket_path = tmp / "service.sock"
            proc = self._spawn(tmp)
            proc.send_signal(signal.SIGTERM)
            proc.send_signal(signal.SIGTERM)
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
                self.fail("repeated SIGTERM/SIGINT left fibo_daemon running")
            self.assertFalse(socket_path.exists())


class FiboHostRequestStopFromServeThreadTests(unittest.TestCase):
    """In-process: request_stop() from the serve_forever thread must not deadlock."""

    def test_request_stop_from_service_actions_exits_promptly(self):
        from plugins.trade.fibo_service import (
            FiboSocketServiceHost,
            PersistentFiboService,
        )

        self.assertTrue(
            hasattr(FiboSocketServiceHost, "request_stop"),
            "FiboSocketServiceHost.request_stop is required so SIGTERM never "
            "calls blocking shutdown() on the serve_forever thread",
        )

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            socket_path = tmp / "service.sock"
            service = PersistentFiboService(
                state_path=tmp / "service_state.json",
                ledger_path=tmp / "service_ledger.jsonl",
                event_log_path=tmp / "events.log",
                start_thread=False,
            )
            host = FiboSocketServiceHost(service=service, socket_path=socket_path)

            class _SelfStopHost(FiboSocketServiceHost):
                def service_actions(self):  # type: ignore[override]
                    self.request_stop()

            # Rebind only service_actions on the live instance.
            host.service_actions = _SelfStopHost.service_actions.__get__(host, type(host))

            thread = threading.Thread(target=host.serve_forever, name="fibo-serve-test")
            thread.start()
            thread.join(timeout=5.0)
            self.assertFalse(
                thread.is_alive(),
                "serve_forever did not return after request_stop from serve thread",
            )
            self.assertFalse(socket_path.exists())
            service.shutdown()


if __name__ == "__main__":
    unittest.main()
