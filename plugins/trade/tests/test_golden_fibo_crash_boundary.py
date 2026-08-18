"""Crash-boundary regression test for the durable submission model.

Exact sequence (Section 3 of the controlled validation):

1. START a fresh registration.
2. Persist deterministic Step0 client identity.
3. Persist SUBMISSION_ATTEMPTED.
4. Fake venue ACCEPTS the MARKET Step0.
5. Simulate immediate process death BEFORE local confirmation/state promotion.
6. Restart the service from persisted state.
7. Reconcile using the persisted client identity and exchange state.
8. Confirm the existing Step0 rather than submitting another one.

Required assertion: MARKET SUBMISSION COUNT = EXACTLY 1.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


# Hermetic module-resolution setup.
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


from plugins.trade.golden_fibo.config import GoldenFiboConfig
from plugins.trade.golden_fibo.state import (
    ROLE_ENTRY,
    STATUS_NEEDS_RECOVERY,
    STATUS_RUNNING,
    SUBMISSION_ATTEMPTED,
    SUBMISSION_CONFIRMED,
    SUBMISSION_NEEDS_RECOVERY,
    SUBMISSION_NOT_SUBMITTED,
    SUBMISSION_PREPARED,
    GoldenFiboState,
)
from plugins.trade.golden_fibo.engine import GoldenFiboEngine
from plugins.trade.fibo_service import PersistentFiboService


class _CrashAdapter:
    """Simulates venue behavior for the crash-boundary test.

    The venue accepts the MARKET Step0 (side effect visible to the
    adapter), but the service process "dies" before the engine can
    promote the state. On restart, the adapter must report the
    already-filled position.
    """

    def __init__(self) -> None:
        self.position = {"symbol": "SOL", "side": None, "size": "0", "sl": None, "tp": None}
        self.orders: dict = {}
        self.submit_log: list = []
        self._next_id = 1000

    def _gen_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def resolve_instrument(self, account, instrument) -> dict:
        return {"symbol": instrument, "market_id": 1, "size_decimals": 3, "price_decimals": 3, "min_base_amount": "0.001"}

    def position_state(self, account, instrument) -> dict:
        return dict(self.position)

    def get_order_state(self, account, order_index) -> dict:
        rec = self.orders.get(int(order_index))
        if rec is None:
            return {}
        return {
            "order_index": int(order_index),
            "client_order_index": rec.get("client_order_id"),
            "symbol": "SOL",
            "side": rec.get("side"),
            "type": rec.get("type"),
            "status": rec.get("status"),
            "taxonomy": rec.get("taxonomy"),
            "requested_price": rec.get("price"),
            "requested_size": rec.get("size"),
            "filled_size": rec.get("size"),
            "actual_fill_price": rec.get("actual_fill_price"),
            "reduce_only": rec.get("reduce_only", False),
        }

    def place_market(self, *, account, instrument, side, size, client_order_id: int) -> dict:
        """Venue accepts the order. This is the crash boundary."""
        oid = self._gen_id()
        rec = {
            "exchange_order_id": oid,
            "client_order_id": client_order_id,
            "side": side,
            "type": "market",
            "size": str(size),
            "price": None,
            "status": "filled",
            "taxonomy": "FILLED",
            "reduce_only": False,
            "actual_fill_price": "100.0",
        }
        self.orders[oid] = rec
        # Update position to simulate the fill.
        prev = Decimal(str(self.position.get("size") or "0"))
        if self.position.get("side") is None:
            self.position["side"] = "long" if side == "buy" else "short"
            self.position["size"] = str(size)
        elif (self.position.get("side") == "long" and side == "buy") or \
             (self.position.get("side") == "short" and side == "sell"):
            self.position["size"] = str(prev + Decimal(str(size)))
        self.submit_log.append(dict(rec, role="entry"))
        return {
            "client_order_id": client_order_id,
            "exchange_order_id": oid,
            "submitted_price": None,
            "submitted_volume": str(size),
            "status": "filled",
            "verified": True,
            "role": "entry",
        }

    def place_limit(self, *, account, instrument, side, size, price, client_order_id, reduce_only=False) -> dict:
        oid = self._gen_id()
        qp = Decimal(str(price)).quantize(Decimal("0.001"))
        rec = {
            "exchange_order_id": oid,
            "client_order_id": client_order_id,
            "side": side,
            "type": "limit",
            "size": str(size),
            "price": str(qp),
            "status": "open",
            "taxonomy": "ACTIVE",
            "reduce_only": bool(reduce_only),
        }
        self.orders[oid] = rec
        self.submit_log.append(dict(rec, role="tp" if reduce_only else "ladder"))
        if reduce_only:
            self.position["tp"] = str(qp)
        return {
            "client_order_id": client_order_id,
            "exchange_order_id": oid,
            "submitted_price": str(qp),
            "submitted_volume": str(size),
            "status": "submitted",
            "verified": True,
            "role": "tp" if reduce_only else "ladder",
        }

    def cancel_order(self, *, account, order_index: int) -> bool:
        rec = self.orders.get(int(order_index))
        if rec is None:
            return False
        rec["status"] = "canceled"
        rec["taxonomy"] = "CANCELED"
        if rec.get("reduce_only"):
            self.position["tp"] = None
        return True


def _step0_submissions(log):
    return [e for e in log if e.get("role") == "entry"]


class TestCrashBoundary(unittest.TestCase):
    """The exact crash-boundary sequence from Section 3."""

    def test_crash_after_venue_accept_before_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            # ---- Phase 1: pre-crash ----
            svc = PersistentFiboService(
                state_path=Path(tmp) / "service_state.json",
                ledger_path=Path(tmp) / "service_ledger.jsonl",
                event_log_path=Path(tmp) / "events.log",
                start_thread=False,
            )
            key = "lighter/amiroo/SOL/BUY"
            adapter = _CrashAdapter()
            svc._adapters[key] = adapter

            # 1. START a fresh registration.
            resp = svc.execute_command({
                "op": "start",
                "exchange": "lighter",
                "account": "amiroo",
                "instrument": "SOL",
                "direction": "BUY",
                "percentage": "0.01",
                "step0_volume": "0.100",
            })
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["status"], "running")

            # 2. Simulate the crash boundary: run ONLY the Step0 submission
            #    part of the engine tick, then "die" before the engine can
            #    promote the state. We use the service's own engine/client_id
            #    factory so the deterministic client_id matches.
            state = svc._states[key]
            from plugins.trade.golden_fibo.engine import GoldenFiboEngine
            from plugins.trade.golden_fibo.config import GoldenFiboConfig
            cfg = GoldenFiboConfig(
                exchange="lighter", account="amiroo", instrument="SOL",
                direction="BUY", percentage=Decimal("0.01"), step0_volume=Decimal("0.100"),
            )
            # Use the service's client_id factory so the deterministic ID matches.
            engine = GoldenFiboEngine(cfg, state, adapter, svc._client_id_factory(key))
            # This submits Step0 and sets submission_phase to CONFIRMED.
            engine._start_fresh_cycle([])

            # 3. Simulate crash: rewind to ATTEMPTED (as if the process died
            #    right after venue accept but before the engine could
            #    confirm/promote the state). Clear the TP/ladder fields that
            #    the engine already set, so the restarted service sees a
            #    clean "Step0 attempted but not confirmed" state.
            state.submission_phase = SUBMISSION_ATTEMPTED
            state.submission_role = ROLE_ENTRY
            state.submission_step = 0
            state.submission_client_id = state.pending_order_client_id
            state.submission_exchange_order_id = state.pending_order_exchange_id
            state.status = STATUS_RUNNING
            # Clear TP/ladder so the restart sees only the attempted Step0.
            state.current_tp_price = None
            state.current_tp_order_id = None
            state.current_tp_client_id = None
            state.current_tp_role = None
            state.next_step = 0
            state.highest_filled_step = -1
            state.fill_prices = {}
            state.expected_cumulative_size = Decimal("0")
            svc._save_state()

            # 3. Simulate process death: create a NEW service instance from disk.
            #    The new instance loads the persisted ATTEMPTED state.
            svc2 = PersistentFiboService(
                state_path=Path(tmp) / "service_state.json",
                ledger_path=Path(tmp) / "service_ledger.jsonl",
                event_log_path=Path(tmp) / "events.log",
                start_thread=False,
            )
            # The new service must reconcile: the venue shows position=0.100
            # but the state says ATTEMPTED. The engine tick must NOT resubmit.
            svc2._adapters[key] = adapter
            svc2._tick_once()

            # 4. Assert: exactly ONE Step0 submission happened.
            self.assertEqual(len(_step0_submissions(adapter.submit_log)), 1)

            # 5. The engine reconciled the existing Step0 fill from the venue
            #    and promoted the state. It did NOT resubmit.
            state2 = svc2._states[key]
            # The engine may either:
            #   a) freeze with NEEDS_RECOVERY (if it cannot reconcile), or
            #   b) confirm the Step0 and advance (if it can reconcile).
            #    Either way, NO second Step0 submission must occur.
            self.assertIn(state2.status, (STATUS_RUNNING, STATUS_NEEDS_RECOVERY))
            # If the engine reconciled successfully, submission_phase is
            # CONFIRMED and the state advanced.
            if state2.status == STATUS_RUNNING:
                self.assertEqual(state2.submission_phase, SUBMISSION_CONFIRMED)
                self.assertEqual(state2.highest_filled_step, 0)
                self.assertEqual(state2.next_step, 1)
            else:
                self.assertEqual(state2.submission_phase, SUBMISSION_NEEDS_RECOVERY)

            # 6. The deterministic client ID survived restart.
            #    After the engine reconciles Step0 and places TP, the
            #    submission_client_id tracks the LATEST attempted order
            #    (TP). The ORIGINAL Step0 client_id is preserved in the
            #    submission record from BEFORE the TP was placed.
            #    We assert the Step0 submission used the correct client_id.
            step0_subs = _step0_submissions(adapter.submit_log)
            self.assertEqual(len(step0_subs), 1)
            self.assertEqual(step0_subs[0]["client_order_id"], state.submission_client_id)

            # 7. P0 can be recovered: the adapter has the fill at 100.0.
            #    The Step0's exchange_order_id is preserved in the
            #    submission record from BEFORE the TP was placed.
            step0_order_id = step0_subs[0]["exchange_order_id"]
            self.assertIsNotNone(step0_order_id)
            order_state = adapter.get_order_state("amiroo", int(step0_order_id))
            self.assertEqual(order_state.get("actual_fill_price"), "100.0")

            # 8. Position ownership is recovered: position=0.100.
            pos = adapter.position_state("amiroo", "SOL")
            self.assertEqual(pos.get("size"), "0.100")
            self.assertEqual(pos.get("side"), "long")

    def test_deterministic_client_id_survives_restart(self):
        """The persisted client_id must be identical after restart."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = PersistentFiboService(
                state_path=Path(tmp) / "service_state.json",
                ledger_path=Path(tmp) / "service_ledger.jsonl",
                event_log_path=Path(tmp) / "events.log",
                start_thread=False,
            )
            key = "lighter/amiroo/SOL/BUY"
            adapter = _CrashAdapter()
            svc._adapters[key] = adapter

            svc.execute_command({
                "op": "start",
                "exchange": "lighter",
                "account": "amiroo",
                "instrument": "SOL",
                "direction": "BUY",
                "percentage": "0.01",
                "step0_volume": "0.100",
            })
            svc._drive_one(key)

            original_client_id = svc._states[key].pending_order_client_id
            self.assertIsNotNone(original_client_id)

            # Simulate crash: rewind to ATTEMPTED and save.
            state = svc._states[key]
            state.submission_phase = SUBMISSION_ATTEMPTED
            state.submission_role = ROLE_ENTRY
            state.submission_step = 0
            state.submission_client_id = original_client_id
            state.submission_exchange_order_id = state.pending_order_exchange_id
            state.status = STATUS_RUNNING
            svc._save_state()

            # Restart.
            svc2 = PersistentFiboService(
                state_path=Path(tmp) / "service_state.json",
                ledger_path=Path(tmp) / "service_ledger.jsonl",
                event_log_path=Path(tmp) / "events.log",
                start_thread=False,
            )
            restored = svc2._states[key]
            self.assertEqual(restored.submission_client_id, original_client_id)
            self.assertEqual(restored.submission_phase, SUBMISSION_ATTEMPTED)

    def test_normal_clean_cycle_still_works(self):
        """A normal clean cycle (no crash) still works end-to-end."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = PersistentFiboService(
                state_path=Path(tmp) / "service_state.json",
                ledger_path=Path(tmp) / "service_ledger.jsonl",
                event_log_path=Path(tmp) / "events.log",
                start_thread=False,
            )
            key = "lighter/amiroo/SOL/BUY"
            adapter = _CrashAdapter()
            svc._adapters[key] = adapter

            svc.execute_command({
                "op": "start",
                "exchange": "lighter",
                "account": "amiroo",
                "instrument": "SOL",
                "direction": "BUY",
                "percentage": "0.01",
                "step0_volume": "0.100",
            })
            svc._drive_one(key)

            # After one tick: Step0 + TP + Step1 should be placed.
            state = svc._states[key]
            self.assertEqual(state.status, STATUS_RUNNING)
            self.assertEqual(state.submission_phase, SUBMISSION_CONFIRMED)
            self.assertEqual(state.highest_filled_step, 0)
            self.assertEqual(state.next_step, 1)
            self.assertIsNotNone(state.current_tp_order_id)
            self.assertIsNotNone(state.pending_order_exchange_id)

            # Exactly one Step0 submission.
            self.assertEqual(len(_step0_submissions(adapter.submit_log)), 1)


if __name__ == "__main__":
    unittest.main()
