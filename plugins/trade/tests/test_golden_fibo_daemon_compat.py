"""Regression tests for fibo_daemon → GoldenFibo service compatibility.

These tests pin the constructor signature that fibo_daemon.py relies on
when it instantiates PersistentFiboService. If the constructor regresses
and refuses `ledger=...` again, fibo.service will fail to start with
"TypeError: __init__() got an unexpected keyword argument 'ledger'".
This regression test was added when that defect was independently
reproduced during the controlled deployment of the GoldenFibo commit.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


# Hermetic module-resolution setup (mirrors other tests in this directory).
_EDITABLE_FINDER = "__editable___hermes_agent_0_20_0_finder"
_KNOWN_EDITABLE_FINDERS = (_EDITABLE_FINDER,)
if any(name in repr(h) for h in sys.path_hooks for name in _KNOWN_EDITABLE_FINDERS):
    sys.path_hooks[:] = [
        h
        for h in sys.path_hooks
        if not any(name in repr(h) for name in _KNOWN_EDITABLE_FINDERS)
    ]

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
for _cached in [k for k in list(sys.modules)
              if k.startswith("plugins.trade")
              and not k.startswith("plugins.trade.tests")]:
    sys.modules.pop(_cached, None)


from plugins.trade.fibo_service import (
    FiboCycleLedger,
    PersistentFiboService,
)


class TestFiboDaemonServiceCompatibility(unittest.TestCase):
    """Pin the contract fibo_daemon.py depends on."""

    def test_ledger_path_kwarg_accepted(self):
        """fibo_daemon may pass state_path/ledger_path/event_log_path."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = PersistentFiboService(
                state_path=Path(tmp) / "service_state.json",
                ledger_path=Path(tmp) / "service_ledger.jsonl",
                event_log_path=Path(tmp) / "events.jsonl",
            )
            self.assertIsInstance(svc.ledger, FiboCycleLedger)

    def test_ledger_object_kwarg_accepted(self):
        """fibo_daemon constructs a FiboCycleLedger and passes it via ledger=.

        This is the exact shape that fibo_daemon.main() uses:

            PersistentFiboService(
                state_path=Path(args.state_path),
                ledger=FiboCycleLedger(Path(args.ledger_path)),
                event_log_path=Path(args.event_log_path),
                start_thread=not args.check,
            )

        If this test fails, fibo.service cannot start.
        """
        with tempfile.TemporaryDirectory() as tmp:
            ledger = FiboCycleLedger(Path(tmp) / "service_ledger.jsonl")
            svc = PersistentFiboService(
                state_path=Path(tmp) / "service_state.json",
                ledger=ledger,
                event_log_path=Path(tmp) / "events.jsonl",
            )
            # The daemon-provided ledger must be used as-is.
            self.assertIs(svc.ledger, ledger)
            self.assertIsInstance(svc.ledger, FiboCycleLedger)

    def test_start_thread_kwarg_accepted(self):
        """Backward-compat with the daemon's start_thread= kwarg."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = PersistentFiboService(
                state_path=Path(tmp) / "service_state.json",
                ledger_path=Path(tmp) / "service_ledger.jsonl",
                event_log_path=Path(tmp) / "events.jsonl",
                start_thread=False,
            )
            self.assertIsInstance(svc.ledger, FiboCycleLedger)

    def test_full_daemon_invocation_signature(self):
        """Exact replica of fibo_daemon.main()'s PersistentFiboService call.

        If any kwarg is rejected, the daemon will fail to start the service
        and fibo.service will enter a restart loop.
        """
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "service_state.json"
            ledger_path = Path(tmp) / "service_ledger.jsonl"
            event_log_path = Path(tmp) / "events.jsonl"

            svc = PersistentFiboService(
                state_path=state_path,
                ledger=FiboCycleLedger(ledger_path),
                event_log_path=event_log_path,
                start_thread=False,
            )

            # Verify all the daemon-relevant paths are wired
            self.assertEqual(svc.state_path, state_path)
            self.assertEqual(svc.ledger_path, ledger_path)
            self.assertEqual(svc.event_log_path, event_log_path)
            self.assertIsInstance(svc.ledger, FiboCycleLedger)


if __name__ == "__main__":
    unittest.main()
