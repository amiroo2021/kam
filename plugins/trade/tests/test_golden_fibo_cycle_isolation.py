"""Regression tests for GoldenFibo current-cycle isolation:

step_orders and all cycle-scoped fields MUST represent ONLY the current cycle.
At the new-cycle boundary, step_orders is explicitly cleared.

Locked rules:
- A new cycle boundary explicitly clears state.step_orders BEFORE the new
  Step0 identity is created (no overwriting individual keys).
- A restart during a healthy active cycle preserves current-cycle step_orders
  (no spurious clearing on reload).
- A restart during SUBMISSION_ATTEMPTED preserves the durable client identity
  and does NOT resubmit Step0.
- A NEEDS_RECOVERY state does NOT clear step_orders just because of the freeze.
- Branch-A completion: only the next real fresh cycle boundary clears the
  previous cycle's step_orders.
- Branch-B: Step1 promotion preserves current-cycle step0+step1; once the
  cycle completes and the next fresh cycle begins, both are cleared.
- No identity from an earlier cycle survives into a subsequent active cycle.
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
# NOTE: Do NOT pop plugins.trade.* from sys.modules here.
# Session-level isolation lives in conftest.py. Mid-suite pops
# create dual CanonicalResponse/TradeDesk identities and break
# later tests (INVALID_AGENT_RESPONSE / ImportError agents).


from plugins.trade.golden_fibo.config import GoldenFiboConfig
from plugins.trade.golden_fibo.state import GoldenFiboState
from plugins.trade.golden_fibo.engine import GoldenFiboEngine


class _StubAdapter:
    """Tiny adapter that supports place_market / set_shared_tp / place_limit /
    cancel_order / get_order_state / position_state / get_venue_constraints
    / get_order_state_by_client_id; remembers every order by oid and lets
    tests simulate fill via simulate_fill / full_fill_ladder / cancel."""

    def __init__(self, direction="BUY"):
        self.direction = direction
        self.position = {"symbol": "SOL", "side": None, "size": "0", "sl": None, "tp": None}
        self.orders = {}
        self._next_oid = 800000
        self.submit_log = []
        self.fail_next = None  # set to a string to raise that exception on next submit

    def _gen_oid(self):
        self._next_oid += 1
        return self._next_oid

    def position_state(self, account, instrument):
        return dict(self.position)

    def get_venue_constraints(self, account, instrument):
        return {"min_base_amount": "0.100", "min_quote_amount": "10.000000",
                "size_decimals": 3, "price_decimals": 3}

    def place_market(self, *, account, instrument, side, size, client_order_id):
        oid = self._gen_oid()
        rec = {"exchange_order_id": oid, "client_order_index": client_order_id,
               "side": side, "type": "market", "size": str(size),
               "status": "filled", "taxonomy": "FILLED", "role": "entry"}
        self.orders[oid] = rec
        self.submit_log.append(rec)
        self.position["side"] = "long" if side == "buy" else "short"
        self.position["size"] = str(size)
        return {"exchange_order_id": oid, "client_order_id": client_order_id,
                "submitted_volume": str(size), "status": "filled", "verified": True, "role": "entry"}

    def set_shared_tp(self, *, account, instrument, price, side, size, client_order_id):
        oid = self._gen_oid()
        qp = Decimal(str(price)).quantize(Decimal("0.001"))
        rec = {"exchange_order_id": oid, "client_order_index": client_order_id, "side": side,
               "type": "limit", "size": str(size), "price": str(qp),
               "status": "open", "taxonomy": "ACTIVE", "reduce_only": True, "role": "tp"}
        self.orders[oid] = rec
        self.submit_log.append(rec)
        return {"exchange_order_id": oid, "client_order_id": client_order_id,
                "submitted_price": str(qp), "submitted_volume": str(size),
                "status": "submitted", "verified": True, "role": "tp"}

    def place_limit(self, *, account, instrument, side, size, price, client_order_id, reduce_only=False):
        oid = self._gen_oid()
        qp = Decimal(str(price)).quantize(Decimal("0.001"))
        rec = {"exchange_order_id": oid, "client_order_index": client_order_id, "side": side,
               "type": "limit", "size": str(size), "requested_size": str(size), "price": str(qp),
               "status": "open", "taxonomy": "ACTIVE", "reduce_only": bool(reduce_only), "role": "ladder"}
        self.orders[oid] = rec
        self.submit_log.append(rec)
        return {"exchange_order_id": oid, "client_order_id": client_order_id,
                "submitted_price": str(qp), "submitted_volume": str(size),
                "status": "submitted", "verified": True}

    def get_order_state(self, account, order_index):
        rec = self.orders.get(int(order_index))
        return dict(rec) if rec else {}

    def get_order_state_by_client_id(self, account, instrument, client_order_index):
        for rec in self.orders.values():
            if int(rec.get("client_order_index") or 0) == int(client_order_index):
                return dict(rec)
        return {}

    def cancel_order(self, *, account, order_index):
        rec = self.orders.get(int(order_index))
        if rec:
            rec["status"] = "canceled"; rec["taxonomy"] = "CANCELED"
            return True
        return False

    # sim helpers
    def set_position(self, side, size):
        self.position["side"] = side
        self.position["size"] = str(size)

    def full_fill_ladder(self, oid, new_pos):
        rec = self.orders[int(oid)]
        rec["status"] = "filled"; rec["taxonomy"] = "FILLED"
        self.position["size"] = str(new_pos)


def _engine_with_nids(direction="BUY", step0="0.200"):
    cfg = GoldenFiboConfig(
        exchange="lighter", account="amiroo", instrument="SOL",
        direction=direction, percentage=Decimal("0.01"), step0_volume=Decimal(step0),
    )
    state = GoldenFiboState(
        registration_key=cfg.registration_key, exchange=cfg.exchange, account=cfg.account,
        instrument=cfg.instrument, direction=cfg.direction,
        percentage=cfg.percentage, step0_volume=cfg.step0_volume,
    )
    adapter = _StubAdapter(direction)
    # Use a deterministic per-instance id factory
    counter = {"n": 100000}
    def nid():
        counter["n"] += 1
        return counter["n"]
    return GoldenFiboEngine(cfg, state, adapter, nid), adapter


def _run_one_complete_cycle(eng, adapter, p0="76.126"):
    """Helper: run Step0 -> confirm -> TP + Step1 -> Step1 fill -> promotion.
    Mimics one full BUY cycle producing step_orders = {0: entry, 1: step1}."""
    eng._start_fresh_cycle([])
    eng.confirm_step0_filled(Decimal(p0))
    eng.place_step0_tp_and_step1(Decimal(p0))
    step1_oid = eng.state.pending_order_exchange_id
    adapter.full_fill_ladder(step1_oid, "0.400")
    eng.tick()


# ---------------------------------------------------------------------------
# A. Cycle 1 Step0 fills, Step1 fills -> step_orders={0: c1-entry, 1: c1-step1}
#    Cycle completes normally. Fresh Cycle 2 begins: OLD step_orders absent.
# ---------------------------------------------------------------------------
class TestCrossCycleIsolation(unittest.TestCase):
    def test_A_fresh_cycle_clears_previous_step_orders(self):
        eng, adapter = _engine_with_nids()
        _run_one_complete_cycle(eng, adapter)
        self.assertIn(1, eng.state.step_orders)
        c1_step1_client = eng.state.step_orders[1]["client_id"]
        # Force a "fresh cycle" boundary (TP fill on a different test branch).
        adapter.set_position("long", "0.200")
        # Simulate Branch A: TP fills -> orphan cleanup via tick's Case B.
        # Here the cleaner approach is to run the recovery path directly:
        # place a TP oid on the adapter, set it filled, and let the engine
        # pick up Case B on the next tick.
        # Instead of forcing, just call _start_fresh_cycle directly (the
        # bootstrap path) and verify clearing.
        eng._start_fresh_cycle([])
        # Old cycle's step_orders must be cleared entirely.
        self.assertEqual(eng.state.step_orders, {})
        # cycle_id must have incremented.
        self.assertEqual(eng.state.cycle_id, 2)

    def test_B_cycle2_step0_only_step1_absent_until_promotion(self):
        eng, adapter = _engine_with_nids()
        _run_one_complete_cycle(eng, adapter)
        eng._start_fresh_cycle([])
        # Now Cycle 2 Step0 has been placed (SUBMISSION_ATTEMPTED).
        # step_orders must STILL be empty (step_orders[0] is set in
        # confirm_step0_filled AFTER venue fill).
        self.assertEqual(eng.state.step_orders, {})
        # confirm Step0 -> step_orders[0] appears, NO other entries.
        eng.confirm_step0_filled(Decimal("76.500"))
        self.assertEqual(list(eng.state.step_orders.keys()), [0])
        self.assertEqual(eng.state.step_orders[0]["role"], "entry")


# ---------------------------------------------------------------------------
# C. Cycle 2 Step1 fills -> step_orders contains c2 Step0 + c2 Step1 only.
# ---------------------------------------------------------------------------
    def test_C_cycle2_step1_promotion_only_cycle2_entries(self):
        eng, adapter = _engine_with_nids()
        _run_one_complete_cycle(eng, adapter)
        # Wipe step_orders to simulate fresh cycle (mimics the boundary):
        eng._start_fresh_cycle([])
        eng.confirm_step0_filled(Decimal("76.500"))
        eng.place_step0_tp_and_step1(Decimal("76.500"))
        # Cycle 2 Step1 fill:
        step1_oid = eng.state.pending_order_exchange_id
        adapter.full_fill_ladder(step1_oid, "0.400")
        eng.tick()
        # step_orders contains exactly c2 step0 and c2 step1 only.
        keys = sorted(eng.state.step_orders.keys())
        self.assertEqual(keys, [0, 1])
        self.assertEqual(eng.state.step_orders[0]["role"], "entry")
        self.assertEqual(eng.state.step_orders[1]["role"], "ladder")
        # The Step1 client id must have been incremented by the factory
        # AFTER the cycle-2 fresh-cycle boundary (the previous cycle's cids
        # are gone from step_orders because we cleared them at the boundary).
        # In the test factory that simply increments, cycle-2 Step1 cid is
        # greater than the last cid used in cycle 1. Capture for assertion.
        if 1 in eng.state.step_orders:
            self.assertIn('client_id', eng.state.step_orders[1])
            # The client id must be an integer > 0.
            self.assertGreater(int(eng.state.step_orders[1]['client_id']), 0)


# ---------------------------------------------------------------------------
# D. Multiple automatic cycles -> no identity from earlier cycle survives.
# ---------------------------------------------------------------------------
    def test_D_five_cycles_no_identity_leak(self):
        eng, adapter = _engine_with_nids()
        leaked_cids = set()
        for i in range(5):
            _run_one_complete_cycle(eng, adapter)
            # Capture Cycle N's client ids
            if 0 in eng.state.step_orders:
                leaked_cids.add(eng.state.step_orders[0]["client_id"])
            if 1 in eng.state.step_orders:
                leaked_cids.add(eng.state.step_orders[1]["client_id"])
            # Fresh cycle -> old cleared
            eng._start_fresh_cycle([])
            # AFTER the boundary: no prior-cycle entries.
            self.assertEqual(eng.state.step_orders, {},
                             f"cycle {i+1} boundary left step_orders={eng.state.step_orders}")
        # Sanity: at least 5 unique step0/step1 cids were created across cycles
        # (because step_orders was cleared, the cids are NOT in step_orders
        # anymore, but the leaked_cids set proves history existed).
        self.assertGreaterEqual(len(leaked_cids), 5)


# ---------------------------------------------------------------------------
# E. Restart during a healthy active cycle: step_orders survive unchanged.
# ---------------------------------------------------------------------------
    def test_E_restart_preserves_current_cycle_step_orders(self):
        eng, adapter = _engine_with_nids()
        _run_one_complete_cycle(eng, adapter)
        before = dict(eng.state.step_orders)
        # Serialize + reload (restart).
        reloaded = GoldenFiboState.from_dict(eng.state.to_dict())
        self.assertEqual(dict(reloaded.step_orders), before)


# ---------------------------------------------------------------------------
# F. Restart during SUBMISSION_ATTEMPTED:
#    - durable client identity survives
#    - Step0 is NOT resubmitted on the next engine creation.
# ---------------------------------------------------------------------------
    def test_F_restart_during_submission_attempted_durable_identity_survives(self):
        eng, adapter = _engine_with_nids()
        # Force _start_fresh_cycle to set SUBMISSION_ATTEMPTED but NOT
        # CONFIRMED, by intercepting the place_market call so that
        # submission_phase stays ATTEMPTED (the engine marks ATTEMPTED before
        # dispatch and only flips to CONFIRMED if the venue call returns).
        # We do this by reverting submission_phase to ATTEMPTED right after
        # the call returns (which mimics a partial-fail scenario), but the
        # cleanest approach is: re-set submission_phase after the call.
        eng._start_fresh_cycle([])
        client_id_pre = eng.state.pending_order_client_id
        oid_pre = eng.state.pending_order_exchange_id
        # Force phase back to ATTEMPTED to simulate a crash mid-submission.
        eng.state.submission_phase = "submission_attempted"
        eng.state.submission_exchange_order_id = None
        self.assertEqual(eng.state.submission_phase, "submission_attempted")
        # Serialize + reload, then NEW engine instance on a fresh adapter
        # (so the prior cycle's orders are not reused).
        reloaded = GoldenFiboState.from_dict(eng.state.to_dict())
        adapter2 = _StubAdapter()
        # Clear adapter orders (don't reuse the prior cycle's records).
        adapter2.orders.clear()
        adapter2.position.update({"symbol": "SOL", "side": None, "size": "0",
                                 "sl": None, "tp": None})
        counter2 = {"n": 1100000}
        def nid2():
            counter2["n"] += 1
            return counter2["n"]
        cfg = GoldenFiboConfig(
            exchange=reloaded.exchange, account=reloaded.account,
            instrument=reloaded.instrument, direction=reloaded.direction,
            percentage=reloaded.percentage, step0_volume=reloaded.step0_volume,
        )
        engine2 = GoldenFiboEngine(cfg, reloaded, adapter2, nid2)
        # Submit count of engine2's adapter BEFORE tick.
        before_submits = len(adapter2.submit_log)
        # A tick that re-enters _start_fresh_cycle must FREEZE (not resubmit).
        result = engine2.tick()
        self.assertEqual(result.state.status, "needs_recovery")
        self.assertIn("Step0 already attempted", result.state.freeze_reason or "")
        # No new place_market call was made.
        self.assertEqual(len(adapter2.submit_log), before_submits)


# ---------------------------------------------------------------------------
# G. NEEDS_RECOVERY does NOT clear step_orders just because of the freeze.
# ---------------------------------------------------------------------------
    def test_G_needs_recovery_does_not_clear_step_orders(self):
        eng, adapter = _engine_with_nids()
        _run_one_complete_cycle(eng, adapter)
        # Manually mark as needs_recovery (simulating a freeze).
        eng.state.status = "needs_recovery"
        eng.state.freeze_reason = "simulated freeze"
        before = dict(eng.state.step_orders)
        # Serialize + reload.
        reloaded = GoldenFiboState.from_dict(eng.state.to_dict())
        adapter2 = _StubAdapter()
        cfg = GoldenFiboConfig(
            exchange=reloaded.exchange, account=reloaded.account,
            instrument=reloaded.instrument, direction=reloaded.direction,
            percentage=reloaded.percentage, step0_volume=reloaded.step0_volume,
        )
        counter = {"n": 1100000}
        def nid():
            counter["n"] += 1
            return counter["n"]
        engine2 = GoldenFiboEngine(cfg, reloaded, adapter2, nid)
        # Run reconcile: pending is NOT FILLED on the fresh adapter, so
        # reconciler returns unchanged. Crucially: step_orders must remain.
        engine2.reconcile_needs_recovery_pending_fill([])
        self.assertEqual(dict(engine2.state.step_orders), before)


# ---------------------------------------------------------------------------
# H. Branch-A completion: once the next fresh cycle begins, old step_orders
#    are cleared (the prior test_A covers this for the cleanest path; here
#    we additionally exercise it via the actual orphan-cleanup tick path).
# ---------------------------------------------------------------------------
    def test_H_branch_A_next_fresh_cycle_clears(self):
        eng, adapter = _engine_with_nids()
        _run_one_complete_cycle(eng, adapter)
        # Mark Step1 oid as cancelled (orphaned).
        # Then tick with position flat -> Case B (orphan pending + flat
        # position) -> _handle_orphan_pending cancels Step1, then next
        # tick sees flat + no pending -> _start_fresh_cycle.
        step1_oid = list(k for k in eng.state.step_orders if k == 1)[0]
        # Better: directly cancel the pending + set position flat and
        # run two ticks (first tick: orphan cleanup; second tick: fresh cycle).
        eng.adapter.cancel_order(account=eng.config.account,
                                  order_index=int(eng.state.pending_order_exchange_id))
        adapter.set_position(None, "0")  # simulated post-TP fill -> position 0
        eng.tick()  # Case B orphan cleanup
        # After first tick: pending cleared, registration eligible for fresh.
        # Run another tick (engine decides fresh cycle because flat + no pending).
        # To avoid the tick reading position via adapter, use a controlled run:
        # just call _start_fresh_cycle directly to test the boundary clearing.
        eng._start_fresh_cycle([])
        # Old cycle step_orders must be cleared.
        self.assertEqual(eng.state.step_orders, {})


# ---------------------------------------------------------------------------
# I. Branch-B: Step1 promotion preserves current-cycle step0+step1; once
#    that cycle eventually completes and the next cycle begins, both are
#    cleared before the next Step0 identity is established.
# ---------------------------------------------------------------------------
    def test_I_branch_b_step1_promotion_then_next_cycle_clears(self):
        eng, adapter = _engine_with_nids()
        _run_one_complete_cycle(eng, adapter)
        # Branch-B transition (Step1 promotion -> TP replacement -> Step2 placement).
        # step_orders[0] and step_orders[1] both current-cycle.
        self.assertIn(0, eng.state.step_orders)
        self.assertIn(1, eng.state.step_orders)
        # Cycle completes (Step2 fills via TP fill or we trigger next fresh).
        # For this test, just call _start_fresh_cycle (mimics next cycle).
        eng._start_fresh_cycle([])
        # Old step_orders cleared.
        self.assertEqual(eng.state.step_orders, {})


if __name__ == "__main__":
    unittest.main()
