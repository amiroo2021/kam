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
from decimal import Decimal
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
# NOTE: Do NOT pop plugins.trade.* from sys.modules here.
# Session-level isolation lives in conftest.py. Mid-suite pops
# create dual CanonicalResponse/TradeDesk identities and break
# later tests (INVALID_AGENT_RESPONSE / ImportError agents).


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
                start_thread=False,
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
                start_thread=False,
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

class TestServiceReconcileNeedsRecovery(unittest.TestCase):
    """The fibo.service drives a NEEDS_RECOVERY registration whose pending
    ladder is proven FILLED via the fallback-aware lookup -> the explicit
    reconcile path runs (NOT a normal tick), the full-step transition
    completes (one cancel + one TP at P0 size 0.400 + one Step2)."""

    def test_service_reconciles_needs_recovery_to_running(self):
        import json as _j
        import tempfile
        from pathlib import Path
        from plugins.trade.golden_fibo.config import GoldenFiboConfig
        from plugins.trade.golden_fibo.state import GoldenFiboState
        # Do NOT pop plugins.trade.* from sys.modules — that creates dual
        # CanonicalResponse/TradeDesk identities and breaks later tests.
        from plugins.trade.fibo_service import PersistentFiboService

        with tempfile.TemporaryDirectory() as tmp:
            svc = PersistentFiboService(
                state_path=Path(tmp) / "service_state.json",
                ledger_path=Path(tmp) / "service_ledger.jsonl",
                event_log_path=Path(tmp) / "events.log",
                start_thread=False,
            )
            # Build the exact live NEEDS_RECOVERY state for lighter/amiroo/SOL/BUY.
            key = "lighter/amiroo/SOL/BUY"
            cfg = GoldenFiboConfig(
                exchange="lighter", account="amiroo", instrument="SOL",
                direction="BUY", percentage=Decimal("0.001"), step0_volume=Decimal("0.200"),
            )
            state = GoldenFiboState(
                registration_key=key, exchange=cfg.exchange, account=cfg.account,
                instrument=cfg.instrument, direction=cfg.direction,
                percentage=cfg.percentage, step0_volume=cfg.step0_volume,
            )
            state.fill_prices[0] = Decimal("76.954")
            state.highest_filled_step = 0
            state.expected_cumulative_size = Decimal("0.200")
            state.next_step = 1
            state.step_orders[0] = {
                "role": "entry", "client_id": 100001,
                "exchange_order_id": 1125898830672005, "status": "filled",
                "price": "76.954", "size": "0.200",
            }
            state.current_tp_price = Decimal("77.030")
            state.current_tp_size = Decimal("0.200")
            state.current_tp_order_id = 844426024508426
            state.current_tp_client_id = 1100001
            state.current_tp_role = "tp"
            state.pending_order_exchange_id = 1125898830671915
            state.pending_order_client_id = 1100002
            state.pending_requested_price = Decimal("76.829488")
            state.pending_requested_size = Decimal("0.200")
            state.pending_confirmed_price = Decimal("76.829")
            state.pending_order_role = "ladder"
            state.status = "needs_recovery"
            state.freeze_reason = ("pending ladder disappeared without expected "
                                   "position delta (live=0.200 expected=0.400)")
            svc._states[key] = state
            svc._save_state()

            # Build the matching _FallbackAdapter and register it.
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from test_golden_fibo_pending_fallback import _FallbackAdapter
            adapter = _FallbackAdapter("BUY")
            adapter.orders[state.current_tp_order_id] = {
                "exchange_order_id": state.current_tp_order_id,
                "client_order_index": state.current_tp_client_id,
                "side": "sell", "type": "limit", "requested_size": "0.200",
                "price": "77.030", "status": "open", "taxonomy": "ACTIVE",
                "reduce_only": True, "role": "tp",
            }
            adapter.position.update({"symbol": "SOL", "side": "long",
                                     "size": "0.400", "sl": None, "tp": "77.030"})
            adapter.orders[state.pending_order_exchange_id] = {
                "exchange_order_id": state.pending_order_exchange_id,
                "client_order_index": 1100002, "side": "buy", "type": "limit",
                "requested_size": "0.200", "price": "76.829",
                "status": "filled", "taxonomy": "FILLED",
                "reduce_only": False, "role": "ladder",
            }
            adapter.force_oid_empty = True
            svc._adapters[key] = adapter

            # Drive the service once: reconcile path runs (NOT normal tick).
            before_cancels = len(adapter.cancel_log)
            before_submits = len(adapter.submit_log)
            svc._drive_one(key)
            r = svc._states[key]
            # Status recovered to running; freeze_reason cleared.
            self.assertEqual(r.status, "running", f"status={r.status} freeze={r.freeze_reason}")
            self.assertIsNone(r.freeze_reason)
            # One TP cancel of the old TP.
            self.assertEqual(
                adapter.cancel_log.count(844426024508426), 1,
                "old TP must be canceled exactly once",
            )
            # One new TP create during this drive.
            new_tps = [s for s in adapter.submit_log[before_submits:] if s.get("role") == "tp"]
            self.assertEqual(len(new_tps), 1, "must create exactly ONE new TP")
            self.assertEqual(Decimal(str(new_tps[0]["price"])), Decimal("76.954"))
            self.assertEqual(Decimal(str(new_tps[0]["requested_size"])), Decimal("0.400"))
            # One Step2 placed.
            new_ladders = [s for s in adapter.submit_log[before_submits:] if s.get("role") == "ladder"]
            self.assertEqual(len(new_ladders), 1, "must place exactly ONE Step2")
            self.assertEqual(Decimal(str(new_ladders[0]["requested_size"])), Decimal("0.400"))
            # Step1 promoted.
            self.assertEqual(r.highest_filled_step, 1)
            self.assertIn(1, r.step_orders)
            self.assertEqual(r.step_orders[1]["client_id"], 1100002)


if __name__ == "__main__":
    unittest.main()
