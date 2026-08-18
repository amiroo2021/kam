"""Regression tests for the GoldenFibo TP-volume synchronization model.

Locked behavior:
- TP PRICE changes ONLY when the pending Step(n) fully fills (TP(n)=P(n-1)).
- TP VOLUME is synchronized to the ACTUAL live position size on EVERY poll,
  independent of logical-step completion (partial ladder fills grow the
  position; the TP must cover the full live exposure at the SAME TP price).
- A partially filled Step(n) does NOT promote, does NOT change TP price,
  does NOT place Step(n+1). Only TP VOLUME follows the position.
- TP-first-during-partial: the volume-synced TP closes the full position;
  the orphaned remaining ladder is canceled.
- A FILLED TP is the legitimate exit (not a failure).
- TP liveness: unexpected CANCELED/missing TP while position open ->
  NEEDS_RECOVERY (no auto re-arm).
"""

from __future__ import annotations

import sys
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


class _SyncAdapter:
    """Stateful venue sim for TP-volume-sync tests."""

    def __init__(self, direction="BUY"):
        self.direction = direction
        self.position = {"symbol": "SOL", "side": None, "size": "0", "sl": None, "tp": None}
        self.orders = {}
        self._next_oid = 700000
        self.submit_log = []
        self.cancel_log = []
        self.fail_next_tp_place = False

    def _gen_oid(self):
        self._next_oid += 1
        return self._next_oid

    def position_state(self, account, instrument):
        return dict(self.position)

    def get_venue_constraints(self, account, instrument):
        return {"min_base_amount": "0.100", "min_quote_amount": "10.000000",
                "size_decimals": 3, "price_decimals": 3}

    def set_shared_tp(self, *, account, instrument, price, side, size, client_order_id):
        if self.fail_next_tp_place:
            self.fail_next_tp_place = False
            raise RuntimeError("simulated TP placement failure")
        oid = self._gen_oid()
        qp = Decimal(str(price)).quantize(Decimal("0.001"))
        rec = {"exchange_order_id": oid, "client_order_id": client_order_id, "side": side,
               "type": "limit", "size": str(size), "requested_size": str(size), "price": str(qp),
               "status": "open", "taxonomy": "ACTIVE", "reduce_only": True, "role": "tp"}
        self.orders[oid] = rec
        self.submit_log.append(rec)
        return {"exchange_order_id": oid, "client_order_id": client_order_id,
                "submitted_price": str(qp), "submitted_volume": str(size),
                "status": "submitted", "verified": True, "role": "tp"}

    def place_limit(self, *, account, instrument, side, size, price, client_order_id, reduce_only=False):
        oid = self._gen_oid()
        qp = Decimal(str(price)).quantize(Decimal("0.001"))
        rec = {"exchange_order_id": oid, "client_order_id": client_order_id, "side": side,
               "type": "limit", "size": str(size), "requested_size": str(size), "price": str(qp),
               "status": "open", "taxonomy": "ACTIVE", "reduce_only": bool(reduce_only), "role": "ladder"}
        self.orders[oid] = rec
        self.submit_log.append(rec)
        return {"exchange_order_id": oid, "client_order_id": client_order_id,
                "submitted_price": str(qp), "submitted_volume": str(size),
                "status": "submitted", "verified": True}

    def place_market(self, *, account, instrument, side, size, client_order_id):
        oid = self._gen_oid()
        self.orders[oid] = {"exchange_order_id": oid, "client_order_id": client_order_id,
                            "side": side, "type": "market", "size": str(size),
                            "status": "filled", "taxonomy": "FILLED", "role": "entry"}
        self.submit_log.append(self.orders[oid])
        self.position["side"] = "long" if side == "buy" else "short"
        self.position["size"] = str(size)
        return {"exchange_order_id": oid, "client_order_id": client_order_id,
                "submitted_volume": str(size), "status": "filled", "verified": True, "role": "entry"}

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
        self.cancel_log.append(int(order_index))
        if rec:
            rec["status"] = "canceled"; rec["taxonomy"] = "CANCELED"
            return True
        return False

    # --- sim helpers ---
    def partial_fill_ladder(self, oid, filled, new_pos):
        rec = self.orders[int(oid)]
        rec["filled_size"] = str(filled)
        self.position["size"] = str(new_pos)

    def full_fill_ladder(self, oid, new_pos):
        rec = self.orders[int(oid)]
        rec["status"] = "filled"; rec["taxonomy"] = "FILLED"
        self.position["size"] = str(new_pos)

    def fill_tp(self, oid, new_pos):
        rec = self.orders[int(oid)]
        rec["status"] = "filled"; rec["taxonomy"] = "FILLED"
        self.position["size"] = str(new_pos)
        if float(new_pos) == 0:
            self.position["side"] = None

    def cancel_on_venue(self, oid):
        rec = self.orders[int(oid)]
        rec["status"] = "canceled"; rec["taxonomy"] = "CANCELED"


def _engine(direction="BUY", step0="0.200"):
    cfg = GoldenFiboConfig(
        exchange="lighter", account="amiroo", instrument="SOL",
        direction=direction, percentage=Decimal("0.01"), step0_volume=Decimal(step0),
    )
    state = GoldenFiboState(
        registration_key=cfg.registration_key, exchange=cfg.exchange, account=cfg.account,
        instrument=cfg.instrument, direction=cfg.direction,
        percentage=cfg.percentage, step0_volume=cfg.step0_volume,
    )
    adapter = _SyncAdapter(direction)
    counter = {"n": 100000}
    def nid():
        counter["n"] += 1
        return counter["n"]
    return GoldenFiboEngine(cfg, state, adapter, nid), adapter


def _setup(direction="BUY"):
    eng, adapter = _engine(direction)
    eng._start_fresh_cycle([])
    eng.confirm_step0_filled(Decimal("76.126"))
    eng.place_step0_tp_and_step1(Decimal("76.126"))
    return eng, adapter


class TestTpVolumeSync(unittest.TestCase):
    def test_1_step0_tp_volume_matches_no_mutation(self):
        eng, adapter = _setup()
        submits = len(adapter.submit_log); cancels = len(adapter.cancel_log)
        result = eng.tick()
        self.assertEqual(result.state.status, "running")
        self.assertEqual(len(adapter.submit_log), submits)
        self.assertEqual(len(adapter.cancel_log), cancels)

    def test_2_partial_step1_fill_syncs_tp_volume_same_price(self):
        eng, adapter = _setup()
        old_tp_oid = eng.state.current_tp_order_id
        tp_price = eng.state.current_tp_price
        step1_oid = eng.state.pending_order_exchange_id
        # Partial fill Step1 0.014 -> position 0.214.
        adapter.partial_fill_ladder(step1_oid, "0.014", "0.214")
        result = eng.tick()
        self.assertEqual(result.state.status, "running")
        # Old TP canceled, new TP placed at SAME price for 0.214.
        self.assertIn(old_tp_oid, adapter.cancel_log)
        new_tps = [s for s in adapter.submit_log if s.get("role") == "tp" and s["exchange_order_id"] != old_tp_oid]
        self.assertEqual(len(new_tps), 1)
        self.assertEqual(Decimal(new_tps[0]["size"]), Decimal("0.214"))
        self.assertEqual(Decimal(new_tps[0]["price"]), tp_price)
        self.assertEqual(eng.state.current_tp_size, Decimal("0.214"))
        self.assertEqual(eng.state.current_tp_price, tp_price)

    def test_3_second_partial_fill_syncs_again(self):
        eng, adapter = _setup()
        step1_oid = eng.state.pending_order_exchange_id
        adapter.partial_fill_ladder(step1_oid, "0.014", "0.214")
        eng.tick()
        tp_price = eng.state.current_tp_price
        # Another partial fill -> 0.250.
        adapter.partial_fill_ladder(step1_oid, "0.050", "0.250")
        result = eng.tick()
        self.assertEqual(result.state.status, "running")
        self.assertEqual(eng.state.current_tp_size, Decimal("0.250"))
        self.assertEqual(eng.state.current_tp_price, tp_price)

    def test_4_partial_fill_no_step_promotion_no_price_change_no_step2(self):
        eng, adapter = _setup()
        step1_oid = eng.state.pending_order_exchange_id
        tp_price = eng.state.current_tp_price
        adapter.partial_fill_ladder(step1_oid, "0.014", "0.214")
        eng.tick()
        self.assertEqual(eng.state.highest_filled_step, 0)
        self.assertEqual(eng.state.next_step, 1)
        self.assertEqual(eng.state.current_tp_price, tp_price)
        # No Step2 placed (only the volume-sync TP was added).
        ladders = [s for s in adapter.submit_log if s.get("role") == "ladder"]
        self.assertEqual(len(ladders), 1)  # only Step1

    def test_5_full_step1_fill_changes_price_and_places_step2(self):
        eng, adapter = _setup()
        step1_oid = eng.state.pending_order_exchange_id
        adapter.full_fill_ladder(step1_oid, "0.400")
        result = eng.tick()
        self.assertNotEqual(result.state.status, "needs_recovery")
        self.assertEqual(eng.state.highest_filled_step, 1)
        self.assertEqual(eng.state.expected_cumulative_size, Decimal("0.400"))
        # TP price -> P0 = 76.126, size 0.400.
        self.assertEqual(eng.state.current_tp_price, Decimal("76.126"))
        self.assertEqual(eng.state.current_tp_size, Decimal("0.400"))
        # Step2 placed once.
        ladders = [s for s in adapter.submit_log if s.get("role") == "ladder"]
        self.assertEqual(len(ladders), 2)  # Step1 + Step2

    def test_6_tp_fills_during_partial_closes_full_position_cancels_orphan(self):
        eng, adapter = _setup()
        step1_oid = eng.state.pending_order_exchange_id
        # Partial fill Step1 -> 0.214, sync TP to 0.214.
        adapter.partial_fill_ladder(step1_oid, "0.014", "0.214")
        eng.tick()
        tp_oid = eng.state.current_tp_order_id
        self.assertEqual(eng.state.current_tp_size, Decimal("0.214"))
        # TP fills -> position 0. Step1 (0.186 remaining) orphaned.
        adapter.fill_tp(tp_oid, "0")
        result = eng.tick()
        # Orphan Step1 canceled, cycle reset.
        self.assertIn(int(step1_oid), adapter.cancel_log)
        self.assertEqual(eng.state.highest_filled_step, -1)
        self.assertEqual(eng.state.next_step, 0)

    def test_7_tp_disappears_unexpectedly_needs_recovery(self):
        eng, adapter = _setup()
        tp_oid = eng.state.current_tp_order_id
        submits = len(adapter.submit_log)
        adapter.cancel_on_venue(tp_oid)
        result = eng.tick()
        self.assertEqual(result.state.status, "needs_recovery")
        self.assertIn("unprotected", (result.state.freeze_reason or "").lower())
        self.assertEqual(len(adapter.submit_log), submits)  # no auto re-arm

    def test_8_sync_cancel_succeeds_place_fails_needs_recovery_no_dup(self):
        eng, adapter = _setup()
        step1_oid = eng.state.pending_order_exchange_id
        old_tp_oid = eng.state.current_tp_order_id
        adapter.partial_fill_ladder(step1_oid, "0.014", "0.214")
        adapter.fail_next_tp_place = True
        result = eng.tick()
        self.assertEqual(result.state.status, "needs_recovery")
        # Old TP was canceled, new TP failed -> no duplicate TP active.
        self.assertIn(old_tp_oid, adapter.cancel_log)
        active_tps = [o for o in adapter.orders.values() if o.get("role") == "tp" and o.get("taxonomy") == "ACTIVE"]
        self.assertEqual(active_tps, [])
        # Durable recovery state preserved.
        self.assertEqual(result.state.submission_phase, "needs_recovery")

    def test_9_restart_during_sync_reconciles_tp_identity_no_dup(self):
        eng, adapter = _setup()
        step1_oid = eng.state.pending_order_exchange_id
        adapter.partial_fill_ladder(step1_oid, "0.014", "0.214")
        eng.tick()  # syncs TP to 0.214
        tp_oid_after_sync = eng.state.current_tp_order_id
        # Serialize + reload (restart).
        reloaded = GoldenFiboState.from_dict(eng.state.to_dict())
        eng2, _ = _engine()
        eng2.state = reloaded
        eng2.adapter = adapter
        submits = len(adapter.submit_log)
        result = eng2.tick()
        # Position 0.214 == TP size 0.214 -> no further mutation.
        self.assertEqual(result.state.status, "running")
        self.assertEqual(len(adapter.submit_log), submits)
        self.assertEqual(eng2.state.current_tp_order_id, tp_oid_after_sync)

    def test_10_filled_tp_is_successful_exit_not_failure(self):
        eng, adapter = _setup()
        tp_oid = eng.state.current_tp_order_id
        # TP fills -> position 0 (legitimate exit).
        adapter.fill_tp(tp_oid, "0")
        result = eng.tick()
        # NOT a freeze for "unprotected"; the position-close path runs.
        # Orphan Step1 canceled, cycle reset.
        self.assertNotEqual(result.state.status, "needs_recovery")
        self.assertEqual(eng.state.highest_filled_step, -1)

    def test_11_tp_partial_fill_freezes_for_reconciliation(self):
        eng, adapter = _setup()
        tp_oid = eng.state.current_tp_order_id
        # TP partially fills: taxonomy FILLED but position still > 0.
        # (sim: TP marked filled, position still 0.100)
        rec = adapter.orders[tp_oid]
        rec["status"] = "filled"; rec["taxonomy"] = "FILLED"
        adapter.position["size"] = "0.100"  # residual position
        result = eng.tick()
        # Engine treats FILLED TP as exit-in-progress (no freeze, no ladder
        # mutation this tick); it does NOT promote or place Step2.
        self.assertEqual(eng.state.highest_filled_step, 0)
        ladders = [s for s in adapter.submit_log if s.get("role") == "ladder"]
        self.assertEqual(len(ladders), 1)  # only Step1, no Step2

class TestBoundedTpExitReconciliation(unittest.TestCase):
    """Bounded rule: a FILLED TP whose position read stays >0 must NOT wait
    forever. After TP_EXIT_MAX_POLLS it freezes TP_PARTIAL_EXIT_NOT_FLAT."""

    def _filled_tp_residual(self):
        eng, adapter = _setup()
        tp_oid = eng.state.current_tp_order_id
        rec = adapter.orders[tp_oid]
        rec["status"] = "filled"; rec["taxonomy"] = "FILLED"
        adapter.position["size"] = "0.100"  # residual position, not flat
        return eng, adapter, tp_oid

    def test_within_bound_no_freeze_no_ladder_mutation(self):
        eng, adapter, tp_oid = self._filled_tp_residual()
        from plugins.trade.golden_fibo.engine import TP_EXIT_MAX_POLLS
        # Poll fewer than the bound: no freeze, no Step2.
        for i in range(TP_EXIT_MAX_POLLS - 1):
            result = eng.tick()
            self.assertNotEqual(result.state.status, "needs_recovery",
                                f"poll {i+1} should not freeze within bound")
        ladders = [s for s in adapter.submit_log if s.get("role") == "ladder"]
        self.assertEqual(len(ladders), 1)  # only Step1
        self.assertEqual(eng.state.tp_exit_attempts, TP_EXIT_MAX_POLLS - 1)

    def test_at_bound_freezes_tp_partial_exit_not_flat(self):
        eng, adapter, tp_oid = self._filled_tp_residual()
        from plugins.trade.golden_fibo.engine import TP_EXIT_MAX_POLLS
        result = None
        for i in range(TP_EXIT_MAX_POLLS):
            result = eng.tick()
        self.assertEqual(result.state.status, "needs_recovery")
        self.assertIn("TP_PARTIAL_EXIT_NOT_FLAT", result.state.freeze_reason or "")
        self.assertEqual(eng.state.tp_exit_attempts, TP_EXIT_MAX_POLLS)

    def test_position_goes_flat_within_bound_normal_cleanup(self):
        eng, adapter = _setup()
        tp_oid = eng.state.current_tp_order_id
        step1_oid = eng.state.pending_order_exchange_id
        rec = adapter.orders[tp_oid]
        rec["status"] = "filled"; rec["taxonomy"] = "FILLED"
        adapter.position["size"] = "0.100"  # lagging read
        # First poll: exit in progress.
        r1 = eng.tick()
        self.assertNotEqual(r1.state.status, "needs_recovery")
        self.assertEqual(eng.state.tp_exit_attempts, 1)
        # Position now reads flat.
        adapter.position["size"] = "0"
        adapter.position["side"] = None
        r2 = eng.tick()
        # Orphan Step1 canceled, cycle reset, counter reset.
        self.assertIn(int(step1_oid), adapter.cancel_log)
        self.assertEqual(eng.state.highest_filled_step, -1)
        self.assertEqual(eng.state.tp_exit_attempts, 0)

    def test_counter_resets_on_healthy_tp(self):
        eng, adapter = _setup()
        eng.state.tp_exit_attempts = 2  # stale counter
        result = eng.tick()  # TP is ACTIVE/healthy
        self.assertEqual(result.state.status, "running")
        self.assertEqual(eng.state.tp_exit_attempts, 0)


if __name__ == "__main__":
    unittest.main()

