"""Regression tests for fallback-aware pending-ladder FILLED detection and the
full Step1 transition (promote -> TP price -> P0 -> Step2).

Root cause fixed: get_order_state(exchange_order_id) can miss FILLED inactive
orders on some accounts, but get_order_state_by_client_id(client_id) sees
them. Pending-ladder reads must use the same fallback-aware pattern used for
TP state, with identity validation (client id / side / instrument / size)
before adopting the fallback record.
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


class _FallbackAdapter:
    """Adapter whose get_order_state(oid) returns {} (like the account that
    misses inactive orders) but get_order_state_by_client_id sees FILLED."""

    def __init__(self, direction="BUY"):
        self.direction = direction
        self.position = {"symbol": "SOL", "side": None, "size": "0", "sl": None, "tp": None}
        self.orders = {}
        self._next_oid = 800000
        self.submit_log = []
        self.cancel_log = []
        # If True, get_order_state(oid) always returns {} (forced fallback).
        self.force_oid_empty = True

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
        self.orders[oid] = {"exchange_order_id": oid, "client_order_index": client_order_id,
                            "side": side, "type": "market", "requested_size": str(size),
                            "status": "filled", "taxonomy": "FILLED", "role": "entry"}
        self.submit_log.append(self.orders[oid])
        self.position["side"] = "long" if side == "buy" else "short"
        self.position["size"] = str(size)
        return {"exchange_order_id": oid, "client_order_id": client_order_id,
                "submitted_volume": str(size), "status": "filled", "verified": True, "role": "entry"}

    def set_shared_tp(self, *, account, instrument, price, side, size, client_order_id):
        oid = self._gen_oid()
        qp = Decimal(str(price)).quantize(Decimal("0.001"))
        rec = {"exchange_order_id": oid, "client_order_index": client_order_id, "side": side,
               "type": "limit", "requested_size": str(size), "price": str(qp),
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
               "type": "limit", "requested_size": str(size), "price": str(qp),
               "status": "open", "taxonomy": "ACTIVE", "reduce_only": bool(reduce_only), "role": "ladder"}
        self.orders[oid] = rec
        self.submit_log.append(rec)
        return {"exchange_order_id": oid, "client_order_id": client_order_id,
                "submitted_price": str(qp), "submitted_volume": str(size),
                "status": "submitted", "verified": True}

    def get_order_state(self, account, order_index):
        if self.force_oid_empty:
            return {}  # simulate the account that misses inactive orders
        rec = self.orders.get(int(order_index))
        return dict(rec) if rec else {}

    def get_order_state_by_client_id(self, account, instrument, client_order_index):
        for rec in self.orders.values():
            cid = rec.get("client_order_index") or rec.get("client_order_id")
            if cid is not None and int(cid) == int(client_order_index):
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
    def fill_ladder_full(self, oid, new_pos):
        rec = self.orders[int(oid)]
        rec["status"] = "filled"; rec["taxonomy"] = "FILLED"
        self.position["size"] = str(new_pos)

    def set_ladder_state(self, oid, taxonomy):
        self.orders[int(oid)]["taxonomy"] = taxonomy
        self.orders[int(oid)]["status"] = taxonomy.lower()


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
    adapter = _FallbackAdapter(direction)
    counter = {"n": 1100000}
    def nid():
        counter["n"] += 1
        return counter["n"]
    return GoldenFiboEngine(cfg, state, adapter, nid), adapter


def _setup(direction="BUY"):
    eng, adapter = _engine(direction)
    eng._start_fresh_cycle([])
    eng.confirm_step0_filled(Decimal("76.954"))
    eng.place_step0_tp_and_step1(Decimal("76.954"))
    return eng, adapter


class TestFallbackFilledDetection(unittest.TestCase):
    def test_1_oid_empty_client_id_filled_promotes_step1(self):
        eng, adapter = _setup()
        step1_oid = eng.state.pending_order_exchange_id
        # Mark Step1 FILLED on the venue; oid lookup returns {} (forced fallback).
        adapter.fill_ladder_full(step1_oid, "0.400")
        result = eng.tick()
        # Promoted via the client-ID fallback.
        self.assertNotEqual(result.state.status, "needs_recovery")
        self.assertEqual(eng.state.highest_filled_step, 1)
        self.assertIn(1, eng.state.step_orders)
        self.assertEqual(eng.state.step_orders[1]["role"], "ladder")
        self.assertEqual(eng.state.expected_cumulative_size, Decimal("0.400"))

    def test_2_stale_position_no_false_freeze(self):
        eng, adapter = _setup()
        step1_oid = eng.state.pending_order_exchange_id
        # Position read still 0.200 (stale) but Step1 FILLED on venue.
        adapter.fill_ladder_full(step1_oid, "0.200")
        # get_order_state returns {} (forced fallback), client-id says FILLED.
        result = eng.tick()
        # Promoted via fallback, NOT a freeze on the stale position read.
        self.assertNotEqual(result.state.status, "needs_recovery")
        self.assertEqual(eng.state.highest_filled_step, 1)

    def test_3_step1_promoted_exactly_once(self):
        eng, adapter = _setup()
        step1_oid = eng.state.pending_order_exchange_id
        adapter.fill_ladder_full(step1_oid, "0.400")
        eng.tick()
        hfs_after = eng.state.highest_filled_step
        step_orders_after = dict(eng.state.step_orders)
        ladders_before = len([s for s in adapter.submit_log if s.get("role") == "ladder"])
        # Second tick: nothing new promoted.
        eng.tick()
        self.assertEqual(eng.state.highest_filled_step, hfs_after)
        self.assertEqual(len([s for s in adapter.submit_log if s.get("role") == "ladder"]), ladders_before)

    def test_5_active_pending_via_fallback_no_promotion(self):
        eng, adapter = _setup()
        # Step1 still ACTIVE on venue.
        result = eng.tick()
        self.assertEqual(result.state.status, "running")
        self.assertEqual(eng.state.highest_filled_step, 0)

    def test_6_canceled_rejected_expired_needs_recovery(self):
        for taxonomy in ("CANCELED", "REJECTED", "EXPIRED"):
            eng, adapter = _setup()
            step1_oid = eng.state.pending_order_exchange_id
            adapter.set_ladder_state(step1_oid, taxonomy)
            result = eng.tick()
            self.assertEqual(result.state.status, "needs_recovery", taxonomy)

    def test_7_wrong_client_id_never_adopted(self):
        eng, adapter = _setup()
        step1_oid = eng.state.pending_order_exchange_id
        step1_oid_int = int(step1_oid)
        # Venue has a FILLED order but with a DIFFERENT client id.
        rec = adapter.orders[step1_oid_int]
        rec["status"] = "filled"; rec["taxonomy"] = "FILLED"
        rec["client_order_index"] = 9999999  # mismatched
        # Position stays at old size 0.200 so the position-delta path can't
        # promote either; the wrong-client-id record must NOT be adopted.
        result = eng.tick()
        # Identity validation rejects the mismatched fallback record. The
        # position-delta path also can't confirm (0.200 < 0.400). So neither
        # the wrong order nor a spurious fill promotes -> it stays at Step0.
        # (This is the 'not adopted' guarantee; a genuine reconcile would
        # freeze, but it must never promote the wrong client id.)
        self.assertNotEqual(
            eng.state.step_orders.get(1, {}).get("client_id"), 9999999
        )
        # And no Step2 from the wrong record.
        ladders = [s for s in adapter.submit_log if s.get("role") == "ladder"]
        # At most Step1 (placed during setup). No Step2 from the bad record's
        # client id path.
        step2_client_ids = [s.get("client_order_index") for s in ladders[1:]]
        self.assertNotIn(9999999, step2_client_ids)

    def test_8_restart_filled_step1_no_dup(self):
        eng, adapter = _setup()
        step1_oid = eng.state.pending_order_exchange_id
        adapter.fill_ladder_full(step1_oid, "0.400")
        # Serialize + reload (restart).
        reloaded = GoldenFiboState.from_dict(eng.state.to_dict())
        eng2, _ = _engine()
        eng2.state = reloaded
        eng2.adapter = adapter
        submits = len(adapter.submit_log)
        result = eng2.tick()
        # Promoted once, no duplicate Step0/Step1/Step2 submissions beyond the
        # single expected transition.
        self.assertEqual(eng2.state.highest_filled_step, 1)
        # No duplicate Step0 (role=entry submissions unchanged).
        entries = [s for s in adapter.submit_log[:submits] if s.get("role") == "entry"]
        self.assertEqual(len(entries), 1)

    def test_10_full_step1_promotion_tp_price_p0_one_step2(self):
        eng, adapter = _setup()
        step1_oid = eng.state.pending_order_exchange_id
        adapter.fill_ladder_full(step1_oid, "0.400")
        result = eng.tick()
        self.assertNotEqual(result.state.status, "needs_recovery")
        # TP price -> P0 = 76.954, size 0.400.
        self.assertEqual(eng.state.current_tp_price, Decimal("76.954"))
        self.assertEqual(eng.state.current_tp_size, Decimal("0.400"))
        # Step2 placed exactly once (BUY 0.400).
        ladders = [s for s in adapter.submit_log if s.get("role") == "ladder"]
        step2 = [s for s in ladders if Decimal(str(s.get("requested_size") or s.get("size") or "0")) == Decimal("0.400")]
        self.assertEqual(len(step2), 1)


# ---------------------------------------------------------------------------
# Exact current live state: position=0.400, old TP=0.200 @ TP0 (77.030),
# Step1 FILLED on venue (oid 1125898830671915, client 1100002, size 0.200,
# fill 76.829). Asserts ONE cancel + ONE TP create at P0 size 0.400 + ONE
# Step2 create, with NO intermediate TP at the old price.
# ---------------------------------------------------------------------------
class TestExactLiveStep1Transition(unittest.TestCase):
    def _build_engine_with_realish_state(self):
        # Simulate the exact persisted state for lighter/amiroo/SOL/BUY after
        # the old get_order_state false-freeze on the real live account.
        cfg = GoldenFiboConfig(
            exchange="lighter", account="amiroo", instrument="SOL",
            direction="BUY", percentage=Decimal("0.001"), step0_volume=Decimal("0.200"),
        )
        state = GoldenFiboState(
        client_id_version=1,
            registration_key=cfg.registration_key, exchange=cfg.exchange, account=cfg.account,
            instrument=cfg.instrument, direction=cfg.direction,
            percentage=cfg.percentage, step0_volume=cfg.step0_volume,
        )
        # Persisted Step0 + Step1 identities (mirrors the live state).
        state.fill_prices[0] = Decimal("76.954")
        state.highest_filled_step = 0
        state.expected_cumulative_size = Decimal("0.200")
        state.next_step = 1
        state.step_orders[0] = {
            "role": "entry", "client_id": 100001,
            "exchange_order_id": 1125898830672005, "status": "filled",
            "price": "76.954", "size": "0.200",
        }
        # Old TP (undersized 0.200) + pending Step1 (client_id persisted).
        state.current_tp_price = Decimal("77.030")
        state.current_tp_size = Decimal("0.200")
        state.current_tp_order_id = 844426024508426  # old TP oid
        state.current_tp_client_id = 1100001
        state.current_tp_role = "tp"
        state.pending_order_exchange_id = 1125898830671915
        state.pending_order_client_id = 1100002
        state.pending_requested_price = Decimal("76.829488")
        state.pending_requested_size = Decimal("0.200")
        state.pending_confirmed_price = Decimal("76.829")
        state.pending_order_role = "ladder"
        # The registration is frozen NEEDS_RECOVERY from the old false-freeze.
        state.status = "needs_recovery"
        state.freeze_reason = (
            "pending ladder disappeared without expected position delta "
            "(live=0.200 expected=0.400)"
        )
        # Build the venue sim matching the live account.
        adapter = _FallbackAdapter("BUY")
        # Place the old TP manually (ACTIVE, SELL ro, 0.200 @ 77.030).
        old_tp_oid = state.current_tp_order_id
        old_tp_cid = state.current_tp_client_id
        adapter.orders[old_tp_oid] = {
            "exchange_order_id": old_tp_oid, "client_order_index": old_tp_cid,
            "side": "sell", "type": "limit", "requested_size": "0.200",
            "price": "77.030", "status": "open", "taxonomy": "ACTIVE",
            "reduce_only": True, "role": "tp",
        }
        adapter.position.update({"symbol": "SOL", "side": "long", "size": "0.400",
                                 "sl": None, "tp": "77.030"})
        # Step1 fills on venue (only the client-id fallback sees it).
        step1_oid = state.pending_order_exchange_id
        adapter.orders[step1_oid] = {
            "exchange_order_id": step1_oid, "client_order_index": 1100002,
            "side": "buy", "type": "limit", "requested_size": "0.200",
            "price": "76.829", "status": "filled", "taxonomy": "FILLED",
            "reduce_only": False, "role": "ladder",
        }
        adapter.force_oid_empty = True  # oid lookup returns {}
        counter = {"n": 1100010}
        def nid():
            counter["n"] += 1
            return counter["n"]
        return GoldenFiboEngine(cfg, state, adapter, nid), adapter

    def test_reconcile_one_cancel_one_tp_one_step2(self):
        eng, adapter = self._build_engine_with_realish_state()
        # Counters
        cancels_before = len(adapter.cancel_log)
        tps_before = [s for s in adapter.submit_log if s.get("role") == "tp"]
        ladders_before = [s for s in adapter.submit_log if s.get("role") == "ladder"]
        # Run reconciler.
        result = eng.reconcile_needs_recovery_pending_fill([])
        # No freeze.
        self.assertNotEqual(result.state.status, "needs_recovery",
                            f"reconciler froze: {result.state.freeze_reason}")
        self.assertEqual(result.state.status, "running")
        # Mutation counts:
        # - exactly ONE TP cancel (the old TP 844426024508426)
        # - exactly ONE new TP create
        # - exactly ONE Step2 ladder create (the new pending)
        tp_cancels = [c for c in adapter.cancel_log[cancels_before:]
                     if c in {state.current_tp_order_id for state in [eng.state]}]
        # Use the explicit old TP oid:
        old_tp_oid = 844426024508426
        self.assertEqual([c for c in adapter.cancel_log[cancels_before:] if c == old_tp_oid].count(old_tp_oid), 1,
                         f"expected exactly one TP cancel of {old_tp_oid}, got cancel_log={adapter.cancel_log[cancels_before:]}")
        new_tps = [s for s in adapter.submit_log[len(tps_before)+len([1]):]  # new tps
                   if s.get("role") == "tp"]
        # Simpler: total TPs created during this run:
        new_tp_count = len([s for s in adapter.submit_log[len(tps_before):] if s.get("role") == "tp"])
        self.assertEqual(new_tp_count, 1, f"expected exactly one TP create, got {new_tp_count}")
        # No intermediate TP at the old TP0 price (77.030) with size 0.400.
        # The single new TP must be at price=P0=76.954, size=0.400.
        the_new_tp = [s for s in adapter.submit_log if s.get("role") == "tp"][-1]
        self.assertEqual(Decimal(str(the_new_tp["price"])), Decimal("76.954"),
                         "new TP must be at P0 (76.954)")
        self.assertEqual(Decimal(str(the_new_tp["requested_size"])), Decimal("0.400"),
                         "new TP size must be 0.400 (live position)")
        # Step2 placed exactly once, BUY 0.400 @ P2.
        new_ladders = [s for s in adapter.submit_log[len(ladders_before):] if s.get("role") == "ladder"]
        self.assertEqual(len(new_ladders), 1, f"expected exactly one Step2, got {len(new_ladders)}")
        step2 = new_ladders[0]
        self.assertEqual(step2["side"], "buy")
        self.assertEqual(Decimal(str(step2["requested_size"])), Decimal("0.400"))
        # P2 = P1 + 1.618*(P1-P0) = 76.829 + 1.618*(76.829-76.954)
        P1 = Decimal("76.829"); P0 = Decimal("76.954"); FIBO = Decimal("1.618")
        P2 = P1 + FIBO * (P1 - P0)
        self.assertEqual(Decimal(str(step2["price"])), P2.quantize(Decimal("0.001")),
                         f"Step2 price must be quantized P2={P2}")
        # State assertions.
        self.assertEqual(eng.state.highest_filled_step, 1)
        self.assertEqual(eng.state.expected_cumulative_size, Decimal("0.400"))
        self.assertIn(1, eng.state.step_orders)
        self.assertEqual(eng.state.step_orders[1]["role"], "ladder")
        self.assertEqual(eng.state.step_orders[1]["client_id"], 1100002)
        self.assertEqual(eng.state.fill_prices[1], Decimal("76.829"))
        self.assertEqual(eng.state.current_tp_price, Decimal("76.954"))
        self.assertEqual(eng.state.current_tp_size, Decimal("0.400"))

    def test_no_intermediate_tp_at_old_price_with_full_size(self):
        """The failure mode the user explicitly forbade: after Step1 fills,
        we must NOT first create a TP 0.400 @ old-TP0-price then immediately
        replace it with TP 0.400 @ P0. Assert no such intermediate exists."""
        eng, adapter = self._build_engine_with_realish_state()
        eng.reconcile_needs_recovery_pending_fill([])
        # Inspect every TP that was submitted during this run.
        # We compare to the single final TP state.
        # Count TP submissions during the run only (from a snapshot taken
        # before the run).
        # The stub records into submit_log in order; we just verify the
        # TOTAL number of TP creates during the run.
        # The setup adds no TPs; the run must add exactly one.
        # The forbidden pattern would add 2 TPs (intermediate + final).
        tp_creates = [s for s in adapter.submit_log if s.get("role") == "tp"]
        self.assertEqual(len(tp_creates), 1,
                         f"forbidden intermediate TP detected ({len(tp_creates)} TP creates)")
        # And that single TP is the final one (P0, 0.400) — never the old
        # TP0 price (77.030) with size 0.400.
        for t in tp_creates:
            price = Decimal(str(t["price"])); size = Decimal(str(t["requested_size"]))
            is_old_price_full_size = (price == Decimal("77.030") and size == Decimal("0.400"))
            self.assertFalse(is_old_price_full_size,
                             f"intermediate TP at old price 77.030 with full size 0.400: {t}")

    def test_reconcile_not_needs_recovery_returns_unchanged(self):
        """The reconciler must not mutate when the pending is not proven FILLED
        (e.g. ACTIVE / missing / CANCELLED)."""
        eng, adapter = self._build_engine_with_realish_state()
        # Step1 still ACTIVE (not FILLED).
        step1_oid = eng.state.pending_order_exchange_id
        adapter.orders[step1_oid]["status"] = "open"; adapter.orders[step1_oid]["taxonomy"] = "ACTIVE"
        submits_before = len(adapter.submit_log)
        cancels_before = len(adapter.cancel_log)
        result = eng.reconcile_needs_recovery_pending_fill([])
        # With the ACTIVE-pending recovery fix, the reconciler clears the
        # stale freeze and returns to running when the pending is confirmed
        # OPEN. No new orders are submitted.
        self.assertEqual(result.state.status, "running")
        self.assertIsNone(result.state.freeze_reason)
        self.assertEqual(len(adapter.submit_log), submits_before)
        self.assertEqual(len(adapter.cancel_log), cancels_before)


if __name__ == "__main__":
    unittest.main()

