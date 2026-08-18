"""Regression tests for durable per-step order-identity preservation.

Incident: the venue Step0 ENTRY client_id (100001) was overwritten by the
Step1 LADDER client_id (1100002) because confirm_step0_filled cleared the
generic pending/submission fields without preserving the ENTRY identity.

These tests pin the corrected schema (state.step_orders):
- Step0 ENTRY identity (client_id + exchange_order_id) preserved forever.
- Step1+ LADDER identity preserved forever.
- TP identity tracked in distinct current_tp_* fields (never overwrites
  ENTRY/LADDER records).
- Restart round-trip preserves all identities simultaneously.
- Step1 -> Step2 transition preserves Step0 + Step1 history and gives
  Step2 its own new identity.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


_EDITABLE_FINDER = "__editable___hermes_agent_0_20_0_finder"
_KNOWN_EDITABLE_FINDERS = (_EDITABLE_FINDER,)
if any(name in repr(h) for h in sys.path_hooks for name in _KNOWN_EDITABLE_FINDERS):
    sys.path_hooks[:] = [
        h for h in sys.path_hooks
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
    ROLE_ENTRY, ROLE_LADDER, ROLE_TP, GoldenFiboState,
)
from plugins.trade.golden_fibo.engine import GoldenFiboEngine
from plugins.trade.fibo_service import PersistentFiboService


# ---------------------------------------------------------------------------
# Stub adapter that issues deterministic ids and tracks orders
# ---------------------------------------------------------------------------
class _IdAdapter:
    """Issues deterministic client ids 100001 (entry), then 1100001 (tp),
    1100002 (ladder) like the live milestone, and records orders."""

    def __init__(self):
        self.position = {"symbol": "SOL", "side": None, "size": "0", "sl": None, "tp": None}
        self.orders = {}
        self._next_oid = 1125898831127290  # matches live Step0 oid
        self.submit_log = []

    def _gen_oid(self):
        oid = self._next_oid
        self._next_oid += 1
        return oid

    def position_state(self, account, instrument):
        return dict(self.position)

    def place_market(self, *, account, instrument, side, size, client_order_id):
        oid = self._gen_oid()
        self.orders[oid] = {"exchange_order_id": oid, "client_order_id": client_order_id,
                            "side": side, "type": "market", "size": str(size),
                            "status": "filled", "taxonomy": "FILLED", "role": "entry"}
        self.submit_log.append(self.orders[oid])
        # establish position
        self.position["side"] = "long" if side == "buy" else "short"
        self.position["size"] = str(size)
        return {"client_order_id": client_order_id, "exchange_order_id": oid,
                "submitted_price": None, "submitted_volume": str(size),
                "status": "filled", "verified": True, "role": "entry"}

    def set_shared_tp(self, *, account, instrument, price, side=None, size=None, client_order_id=None):
        oid = 844426024069104  # matches live TP0 oid
        qp = Decimal(str(price)).quantize(Decimal("0.001"))
        live_side = self.position.get("side")
        self.orders[oid] = {"exchange_order_id": oid, "client_order_id": None,
                            "side": "sell" if live_side == "long" else "buy",
                            "type": "take-profit", "size": str(self.position.get("size") or "0"),
                            "price": str(qp), "status": "open", "taxonomy": "ACTIVE", "role": "tp"}
        self.submit_log.append(self.orders[oid])
        self.position["tp"] = str(qp)
        return {"verified": True, "submitted_price": str(qp), "exchange_order_id": oid,
                "current_side": live_side, "current_size": str(self.position.get("size") or "0"), "role": "tp"}

    def place_limit(self, *, account, instrument, side, size, price, client_order_id, reduce_only=False):
        oid = 1125898831127248  # matches live Step1 oid
        qp = Decimal(str(price)).quantize(Decimal("0.001"))
        rec = {"exchange_order_id": oid, "client_order_id": client_order_id, "side": side,
               "type": "limit", "size": str(size), "price": str(qp), "status": "open",
               "taxonomy": "ACTIVE", "reduce_only": bool(reduce_only), "role": "ladder"}
        self.orders[oid] = rec
        self.submit_log.append(rec)
        return {"client_order_id": client_order_id, "exchange_order_id": oid,
                "submitted_price": str(qp), "submitted_volume": str(size),
                "status": "submitted", "verified": True}

    def get_order_state(self, account, order_index):
        rec = self.orders.get(int(order_index))
        return dict(rec) if rec else {}

    def get_order_state_by_client_id(self, account, instrument, client_order_index):
        for rec in self.orders.values():
            if int(rec.get("client_order_id") or 0) == int(client_order_index):
                return dict(rec)
        return {}

    def cancel_order(self, *, account, order_index):
        rec = self.orders.get(int(order_index))
        if rec:
            rec["status"] = "canceled"; rec["taxonomy"] = "CANCELED"
            return True
        return False

    def simulate_fill(self, oid):
        rec = self.orders.get(int(oid))
        if rec:
            rec["status"] = "filled"; rec["taxonomy"] = "FILLED"


def _engine_with_ids(adapter):
    """Engine with a deterministic id factory: entry=100001, then 1100001+."""
    cfg = GoldenFiboConfig(
        exchange="lighter", account="amiroo", instrument="SOL",
        direction="BUY", percentage=Decimal("0.01"), step0_volume=Decimal("0.200"),
    )
    state = GoldenFiboState(
        registration_key=cfg.registration_key, exchange=cfg.exchange, account=cfg.account,
        instrument=cfg.instrument, direction=cfg.direction,
        percentage=cfg.percentage, step0_volume=cfg.step0_volume,
    )
    ids = iter([100001] + list(range(1100001, 1100100)))
    return GoldenFiboEngine(cfg, state, adapter, lambda: next(ids))


class TestStepIdentityPreserved(unittest.TestCase):
    """Step0 ENTRY, TP, and Step1 LADDER identities must never overwrite."""

    def _run_initial_setup(self):
        adapter = _IdAdapter()
        eng = _engine_with_ids(adapter)
        # Step0
        eng._start_fresh_cycle([])
        # confirm Step0 (records step_orders[0])
        eng.confirm_step0_filled(Decimal("76.370"))
        # TP0 + Step1
        eng.place_step0_tp_and_step1(Decimal("76.370"))
        return eng, adapter

    def test_step0_entry_identity_preserved_after_step1(self):
        eng, adapter = self._run_initial_setup()
        so = eng.state.step_orders
        # Step0 ENTRY preserved
        self.assertIn(0, so)
        self.assertEqual(so[0]["role"], ROLE_ENTRY)
        self.assertEqual(so[0]["client_id"], 100001)
        self.assertEqual(so[0]["exchange_order_id"], 1125898831127290)
        self.assertEqual(so[0]["price"], "76.370")
        # Step1 LADDER is the CURRENT pending order (not yet filled), tracked
        # in the generic pending fields, NOT yet in step_orders.
        self.assertEqual(eng.state.pending_order_client_id, 1100002)
        self.assertEqual(eng.state.pending_order_exchange_id, 1125898831127248)
        self.assertEqual(eng.state.pending_order_role, ROLE_LADDER)
        # TP tracked in distinct current_tp_* fields
        self.assertEqual(eng.state.current_tp_order_id, 844426024069104)
        self.assertEqual(eng.state.current_tp_role, ROLE_TP)
        # Step0 identity NOT overwritten by Step1 client id
        self.assertEqual(so[0]["client_id"], 100001)

    def test_restart_round_trip_preserves_all_identities(self):
        eng, adapter = self._run_initial_setup()
        d = eng.state.to_dict()
        s2 = GoldenFiboState.from_dict(d)
        # Step0
        self.assertEqual(s2.step_orders[0]["client_id"], 100001)
        self.assertEqual(s2.step_orders[0]["exchange_order_id"], 1125898831127290)
        # TP
        self.assertEqual(s2.current_tp_order_id, 844426024069104)
        self.assertEqual(s2.current_tp_role, ROLE_TP)
        # Step1 pending
        self.assertEqual(s2.pending_order_client_id, 1100002)
        self.assertEqual(s2.pending_order_exchange_id, 1125898831127248)
        self.assertEqual(s2.pending_order_role, ROLE_LADDER)
        # All three identities simultaneously present, none overwritten
        self.assertEqual(s2.step_orders[0]["client_id"], 100001)
        self.assertEqual(s2.current_tp_order_id, 844426024069104)
        self.assertEqual(s2.pending_order_client_id, 1100002)

    def test_service_restart_preserves_identities_and_no_mutation(self):
        """Serialize via the real service, restart, reload, assert identities."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = PersistentFiboService(
                state_path=Path(tmp) / "service_state.json",
                ledger_path=Path(tmp) / "service_ledger.jsonl",
                event_log_path=Path(tmp) / "events.log",
                start_thread=False,
            )
            key = "lighter/amiroo/SOL/BUY"
            adapter = _IdAdapter()
            svc._adapters[key] = adapter
            eng = _engine_with_ids(adapter)
            svc._states[key] = eng.state
            # Run the initial setup through the engine against the service state
            eng._start_fresh_cycle([])
            eng.confirm_step0_filled(Decimal("76.370"))
            eng.place_step0_tp_and_step1(Decimal("76.370"))
            svc._states[key] = eng.state
            svc._save_state()

            # Restart the service from disk.
            svc2 = PersistentFiboService(
                state_path=Path(tmp) / "service_state.json",
                ledger_path=Path(tmp) / "service_ledger.jsonl",
                event_log_path=Path(tmp) / "events.log",
                start_thread=False,
            )
            svc2._adapters[key] = adapter
            s = svc2._states[key]
            self.assertEqual(s.step_orders[0]["client_id"], 100001)
            self.assertEqual(s.step_orders[0]["exchange_order_id"], 1125898831127290)
            self.assertEqual(s.current_tp_order_id, 844426024069104)
            self.assertEqual(s.pending_order_client_id, 1100002)
            self.assertEqual(s.pending_order_exchange_id, 1125898831127248)

            # Tick: engine recognizes Step1 ACTIVE, zero mutations.
            before = len(adapter.submit_log)
            svc2._tick_once()
            after = len(adapter.submit_log)
            self.assertEqual(before, after, "no new submissions on restart tick")
            self.assertEqual(s.status, "running")
            # No duplicate Step0 / TP / Step1
            self.assertEqual(len([e for e in adapter.submit_log if e.get("role") == "entry"]), 1)
            self.assertEqual(len([e for e in adapter.submit_log if e.get("role") == "tp"]), 1)
            self.assertEqual(len([e for e in adapter.submit_log if e.get("role") == "ladder"]), 1)


class TestStep1ToStep2Transition(unittest.TestCase):
    """Step1 fill must preserve Step0 + Step1 history and give Step2 a new id."""

    def test_step1_fill_transition(self):
        adapter = _IdAdapter()
        eng = _engine_with_ids(adapter)
        eng._start_fresh_cycle([])
        eng.confirm_step0_filled(Decimal("76.370"))
        eng.place_step0_tp_and_step1(Decimal("76.370"))

        # Step1 fills on the venue.
        step1_oid = eng.state.pending_order_exchange_id
        adapter.simulate_fill(step1_oid)
        # Grow position to 0.400 (V0 + V1).
        adapter.position["size"] = "0.400"

        result = eng._handle_confirmed_fill([])
        # Not frozen
        self.assertNotEqual(result.state.status, "needs_recovery")

        # Step0 ENTRY history preserved
        self.assertEqual(eng.state.step_orders[0]["client_id"], 100001)
        self.assertEqual(eng.state.step_orders[0]["exchange_order_id"], 1125898831127290)

        # Step1 LADDER now recorded as filled history
        self.assertIn(1, eng.state.step_orders)
        self.assertEqual(eng.state.step_orders[1]["role"], ROLE_LADDER)
        self.assertEqual(eng.state.step_orders[1]["client_id"], 1100002)
        self.assertEqual(eng.state.step_orders[1]["exchange_order_id"], 1125898831127248)
        self.assertEqual(eng.state.step_orders[1]["status"], "filled")
        self.assertEqual(eng.state.step_orders[1]["price"], "75.134")

        # expected position 0.400, logical P1 = 75.134
        self.assertEqual(eng.state.expected_cumulative_size, Decimal("0.400"))
        self.assertEqual(eng.state.fill_prices[1], Decimal("75.134"))

        # Shared TP moved to P0 = 76.370 (current_tp_price updated by _rotate_tp)
        self.assertEqual(eng.state.current_tp_price, Decimal("76.370"))

        # Step2 got its OWN new client/order identity, distinct from Step0/Step1
        step2_client = eng.state.pending_order_client_id
        self.assertIsNotNone(step2_client)
        self.assertNotEqual(step2_client, 100001)
        self.assertNotEqual(step2_client, 1100002)
        # Step2 identity does not overwrite Step0 or Step1 history
        self.assertEqual(eng.state.step_orders[0]["client_id"], 100001)
        self.assertEqual(eng.state.step_orders[1]["client_id"], 1100002)

class TestStateSerializationRoundTrip(unittest.TestCase):
    """highest_filled_step must survive JSON round-trip, including the valid
    value 0 (Step0 filled). Regression for the 'or -1' falsy-coercion bug
    that silently turned hfs=0 into hfs=-1 on every save/load cycle."""

    def test_highest_filled_step_zero_round_trips(self):
        s = GoldenFiboState(registration_key="k")
        s.highest_filled_step = 0
        d = s.to_dict()
        s2 = GoldenFiboState.from_dict(d)
        self.assertEqual(s2.highest_filled_step, 0)

    def test_highest_filled_step_positive_round_trips(self):
        s = GoldenFiboState(registration_key="k")
        s.highest_filled_step = 5
        self.assertEqual(GoldenFiboState.from_dict(s.to_dict()).highest_filled_step, 5)

    def test_highest_filled_step_minus_one_round_trips(self):
        s = GoldenFiboState(registration_key="k")
        s.highest_filled_step = -1
        self.assertEqual(GoldenFiboState.from_dict(s.to_dict()).highest_filled_step, -1)

    def test_highest_filled_step_json_round_trips(self):
        import json
        s = GoldenFiboState(registration_key="k")
        s.highest_filled_step = 0
        d = json.loads(json.dumps(s.to_dict()))
        self.assertEqual(GoldenFiboState.from_dict(d).highest_filled_step, 0)


if __name__ == "__main__":
    unittest.main()

