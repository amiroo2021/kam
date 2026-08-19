"""Regression tests for GoldenFibo TP lifecycle semantics (resting LIMIT TP).

Covers the locked behaviors:
- partial Step1 fill causes ZERO TP mutation (no resize, no move, no promote)
- full Step1 fill causes exactly one old-TP cancel + one new TP + one Step2
- unexpected TP cancellation -> NEEDS_RECOVERY, no automatic re-arm
- TP fill (position closes) -> remaining ladder canceled (orphan cleanup)
- restart with healthy TP + partial ladder -> zero mutation
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
from plugins.trade.golden_fibo.state import (
    ROLE_ENTRY, ROLE_LADDER, ROLE_TP, GoldenFiboState,
)
from plugins.trade.golden_fibo.engine import GoldenFiboEngine


class _LifecycleAdapter:
    """Stateful adapter simulating venue orders for TP-lifecycle tests."""

    def __init__(self, direction="BUY"):
        self.direction = direction
        self.position = {"symbol": "SOL", "side": None, "size": "0", "sl": None, "tp": None}
        self.orders = {}
        self._next_oid = 500000
        self.submit_log = []
        self.cancel_log = []

    def _gen_oid(self):
        self._next_oid += 1
        return self._next_oid

    def position_state(self, account, instrument):
        return dict(self.position)

    def get_venue_constraints(self, account, instrument):
        return {"min_base_amount": "0.100", "min_quote_amount": "10.000000",
                "size_decimals": 3, "price_decimals": 3}

    def set_shared_tp(self, *, account, instrument, price, side, size, client_order_id):
        oid = self._gen_oid()
        qp = Decimal(str(price)).quantize(Decimal("0.001"))
        rec = {"exchange_order_id": oid, "client_order_id": client_order_id, "side": side,
               "type": "limit", "size": str(size), "price": str(qp), "status": "open",
               "taxonomy": "ACTIVE", "reduce_only": True, "role": "tp"}
        self.orders[oid] = rec
        self.submit_log.append(rec)
        self.position["tp"] = str(qp)
        return {"exchange_order_id": oid, "client_order_id": client_order_id,
                "submitted_price": str(qp), "submitted_volume": str(size),
                "status": "submitted", "verified": True, "role": "tp"}

    def place_limit(self, *, account, instrument, side, size, price, client_order_id, reduce_only=False):
        oid = self._gen_oid()
        qp = Decimal(str(price)).quantize(Decimal("0.001"))
        rec = {"exchange_order_id": oid, "client_order_id": client_order_id, "side": side,
               "type": "limit", "size": str(size), "price": str(qp), "status": "open",
               "taxonomy": "ACTIVE", "reduce_only": bool(reduce_only), "role": "ladder"}
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

    def cancel_order(self, *, account, order_index):
        rec = self.orders.get(int(order_index))
        self.cancel_log.append(int(order_index))
        if rec:
            rec["status"] = "canceled"; rec["taxonomy"] = "CANCELED"
            return True
        return False

    # --- simulation helpers ---
    def partial_fill(self, oid, filled_size, new_pos_size):
        rec = self.orders[int(oid)]
        rec["filled_size"] = str(filled_size)
        # still resting (partially filled) -> taxonomy stays ACTIVE
        self.position["size"] = str(new_pos_size)

    def full_fill(self, oid, new_pos_size):
        rec = self.orders[int(oid)]
        rec["status"] = "filled"; rec["taxonomy"] = "FILLED"
        self.position["size"] = str(new_pos_size)

    def cancel_on_venue(self, oid):
        rec = self.orders[int(oid)]
        rec["status"] = "canceled"; rec["taxonomy"] = "CANCELED"

    def tp_fills_close_position(self, oid):
        rec = self.orders[int(oid)]
        rec["status"] = "filled"; rec["taxonomy"] = "FILLED"
        self.position["size"] = "0"
        self.position["side"] = None


def _engine(direction="BUY", step0="0.200"):
    cfg = GoldenFiboConfig(
        exchange="lighter", account="amiroo", instrument="SOL",
        direction=direction, percentage=Decimal("0.01"), step0_volume=Decimal(step0),
    )
    state = GoldenFiboState(
        client_id_version=1,
        registration_key=cfg.registration_key, exchange=cfg.exchange, account=cfg.account,
        instrument=cfg.instrument, direction=cfg.direction,
        percentage=cfg.percentage, step0_volume=cfg.step0_volume,
    )
    adapter = _LifecycleAdapter(direction)
    counter = {"n": 100000}
    def nid():
        counter["n"] += 1
        return counter["n"]
    return GoldenFiboEngine(cfg, state, adapter, nid), adapter


def _setup_through_step1(direction="BUY"):
    """Run Step0 -> confirm -> TP0 + Step1. Returns (engine, adapter)."""
    eng, adapter = _engine(direction)
    eng._start_fresh_cycle([])
    eng.confirm_step0_filled(Decimal("76.126"))
    eng.place_step0_tp_and_step1(Decimal("76.126"))
    return eng, adapter


class TestPartialLadderFillNoTpMutation(unittest.TestCase):
    def test_partial_step1_fill_zero_tp_mutation(self):
        eng, adapter = _setup_through_step1()
        tp_oid = eng.state.current_tp_order_id
        step1_oid = eng.state.pending_order_exchange_id
        tp_price_before = eng.state.current_tp_price
        submits_before = len(adapter.submit_log)
        cancels_before = len(adapter.cancel_log)

        # Partially fill Step1 (0.014 of 0.200) -> position 0.214.
        adapter.partial_fill(step1_oid, "0.014", "0.214")

        result = eng.tick()
        # Not frozen, still running, healthy waiting.
        self.assertEqual(result.state.status, "running")
        # ZERO TP mutation: no new submit, no cancel, TP price + oid unchanged.
        self.assertEqual(len(adapter.submit_log), submits_before)
        self.assertEqual(len(adapter.cancel_log), cancels_before)
        self.assertEqual(eng.state.current_tp_order_id, tp_oid)
        self.assertEqual(eng.state.current_tp_price, tp_price_before)
        # Step1 NOT promoted.
        self.assertEqual(eng.state.highest_filled_step, 0)
        self.assertEqual(eng.state.next_step, 1)


class TestFullStep1FillTransition(unittest.TestCase):
    def test_full_step1_fill_one_cancel_one_tp_one_step2(self):
        eng, adapter = _setup_through_step1()
        old_tp_oid = eng.state.current_tp_order_id
        step1_oid = eng.state.pending_order_exchange_id
        submits_before = len(adapter.submit_log)

        # Fully fill Step1 -> position 0.400.
        adapter.full_fill(step1_oid, "0.400")
        result = eng.tick()

        self.assertNotEqual(result.state.status, "needs_recovery")
        # Promoted.
        self.assertEqual(eng.state.highest_filled_step, 1)
        self.assertEqual(eng.state.expected_cumulative_size, Decimal("0.400"))
        # Exactly one old-TP cancel.
        self.assertEqual(adapter.cancel_log.count(old_tp_oid), 1)
        # Exactly one new TP (role=tp) placed after the cancel.
        new_tps = [s for s in adapter.submit_log[submits_before:] if s.get("role") == "tp"]
        self.assertEqual(len(new_tps), 1)
        # New TP: size 0.400, price = P0 = 76.126, reduce_only, opposite side.
        self.assertEqual(Decimal(new_tps[0]["size"]), Decimal("0.400"))
        self.assertEqual(Decimal(new_tps[0]["price"]), Decimal("76.126"))
        self.assertTrue(new_tps[0]["reduce_only"])
        self.assertEqual(new_tps[0]["side"], "sell")
        # Exactly one Step2 ladder placed.
        new_ladders = [s for s in adapter.submit_log[submits_before:] if s.get("role") == "ladder"]
        self.assertEqual(len(new_ladders), 1)
        self.assertEqual(Decimal(new_ladders[0]["size"]), Decimal("0.400"))
        self.assertFalse(new_ladders[0]["reduce_only"])


class TestUnexpectedTpCancellation(unittest.TestCase):
    def test_tp_canceled_on_venue_needs_recovery_no_rearm(self):
        eng, adapter = _setup_through_step1()
        tp_oid = eng.state.current_tp_order_id
        submits_before = len(adapter.submit_log)

        # Venue cancels the TP (slippage) but position still open.
        adapter.cancel_on_venue(tp_oid)

        result = eng.tick()
        # NEEDS_RECOVERY, and NO automatic re-arm (no new TP submitted).
        self.assertEqual(result.state.status, "needs_recovery")
        self.assertIn("unprotected", (result.state.freeze_reason or "").lower())
        self.assertEqual(len(adapter.submit_log), submits_before)


class TestTpFillCancelsLadder(unittest.TestCase):
    def test_tp_fill_closes_position_cancels_ladder(self):
        eng, adapter = _setup_through_step1()
        tp_oid = eng.state.current_tp_order_id
        step1_oid = eng.state.pending_order_exchange_id

        # TP fills -> position closes to 0. Step1 still resting (orphan).
        adapter.tp_fills_close_position(tp_oid)

        result = eng.tick()
        # Orphan ladder canceled.
        self.assertIn(int(step1_oid), adapter.cancel_log)
        # Cycle reset for a fresh start.
        self.assertEqual(eng.state.highest_filled_step, -1)
        self.assertEqual(eng.state.next_step, 0)


class TestRestartHealthyTpPartialLadder(unittest.TestCase):
    def test_restart_healthy_tp_partial_ladder_zero_mutation(self):
        eng, adapter = _setup_through_step1()
        step1_oid = eng.state.pending_order_exchange_id
        # Partial fill Step1 -> position 0.214.
        adapter.partial_fill(step1_oid, "0.014", "0.214")

        # Serialize + reload (restart).
        from plugins.trade.golden_fibo.state import GoldenFiboState as GS
        reloaded = GS.from_dict(eng.state.to_dict())
        eng2, adapter2 = _engine()
        eng2.state = reloaded
        eng2.adapter = adapter  # same venue

        submits_before = len(adapter.submit_log)
        cancels_before = len(adapter.cancel_log)
        result = eng2.tick()
        # Healthy TP (ACTIVE) + partial ladder -> zero mutation.
        self.assertEqual(result.state.status, "running")
        self.assertEqual(len(adapter.submit_log), submits_before)
        self.assertEqual(len(adapter.cancel_log), cancels_before)


if __name__ == "__main__":
    unittest.main()
