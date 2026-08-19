"""GoldenFibo v1 lifecycle parity tests — audit round 2026-08-18.

Targeted offline tests for lifecycle scenarios that the existing
test_golden_fibo*.py suite did not cover explicitly. All tests are
offline and use a fake adapter (no live venue calls).

Each test maps 1:1 to a scenario in the user's audit request:

  SELL partial-fill TP-volume sync               -> test_sell_partial_ladder_fill_tp_volume_syncs
  SELL full-fill Step1 -> Step2 rotation        -> test_sell_full_step1_fill_rotates_tp_to_p0_and_places_step2
  Restart scenario E: flat + orphan pending     -> test_restart_flat_with_orphan_pending_cancels_and_starts_fresh
  Restart scenario F: flat + no pending         -> test_restart_flat_no_pending_starts_fresh_step0
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


from plugins.trade.golden_fibo.config import GoldenFiboConfig
from plugins.trade.golden_fibo.state import (
    ROLE_ENTRY,
    ROLE_LADDER,
    ROLE_TP,
    GoldenFiboState,
)
from plugins.trade.golden_fibo.engine import GoldenFiboEngine


class _LifecycleAuditAdapter:
    """Minimal fake adapter for the lifecycle audit tests.

    Behaves like Lighter: place_market fills immediately, place_limit
    rests on the book until simulate_full_fill or simulate_partial_fill.
    TP orders are tracked separately for cancel-by-oid lookups.
    """

    def __init__(self, direction: str = "BUY"):
        self.direction = direction
        self.position = {"symbol": "SOL", "side": None, "size": "0",
                         "sl": None, "tp": None}
        self.orders = {}
        self.submit_log = []
        self.cancel_log = []
        self._next_oid = 700000

    def _gen_oid(self) -> int:
        self._next_oid += 1
        return self._next_oid

    def position_state(self, account, instrument):
        return dict(self.position)

    def get_venue_constraints(self, account, instrument):
        return {"min_base_amount": "0.100", "min_quote_amount": "10.000000",
                "size_decimals": 3, "price_decimals": 3}

    def set_shared_tp(self, *, account, instrument, price, side, size,
                      client_order_id):
        oid = self._gen_oid()
        rec = {"exchange_order_id": oid, "client_order_id": client_order_id,
               "side": side, "type": "limit", "size": str(size),
               "requested_size": str(size),  # the engine reads requested_size
               "price": str(price), "status": "open",
               "taxonomy": "ACTIVE", "reduce_only": True, "role": "tp"}
        self.orders[oid] = rec
        self.submit_log.append(rec)
        self.position["tp"] = str(price)
        return {"exchange_order_id": oid, "client_order_id": client_order_id,
                "submitted_price": str(price), "submitted_volume": str(size),
                "status": "submitted", "verified": True, "role": "tp"}

    def place_limit(self, *, account, instrument, side, size, price,
                    client_order_id, reduce_only=False):
        oid = self._gen_oid()
        rec = {"exchange_order_id": oid, "client_order_id": client_order_id,
               "side": side, "type": "limit", "size": str(size),
               "requested_size": str(size),
               "price": str(price), "status": "open",
               "taxonomy": "ACTIVE", "reduce_only": bool(reduce_only),
               "role": "ladder"}
        self.orders[oid] = rec
        self.submit_log.append(rec)
        return {"exchange_order_id": oid, "client_order_id": client_order_id,
                "submitted_price": str(price), "submitted_volume": str(size),
                "status": "submitted", "verified": True}

    def place_market(self, *, account, instrument, side, size,
                     client_order_id):
        oid = self._gen_oid()
        self.orders[oid] = {"exchange_order_id": oid,
                            "client_order_id": client_order_id,
                            "side": side, "type": "market",
                            "size": str(size), "status": "filled",
                            "taxonomy": "FILLED", "role": "entry"}
        self.submit_log.append(self.orders[oid])
        self.position["side"] = "long" if side == "buy" else "short"
        self.position["size"] = str(size)
        return {"exchange_order_id": oid, "client_order_id": client_order_id,
                "submitted_volume": str(size), "status": "filled",
                "verified": True, "role": "entry"}

    def get_order_state(self, account, order_index):
        rec = self.orders.get(int(order_index))
        return dict(rec) if rec else {}

    def get_order_state_by_client_id(self, account, instrument,
                                     client_order_index):
        for rec in self.orders.values():
            if int(rec.get("client_order_id") or 0) == int(client_order_index):
                return dict(rec)
        return {}

    def cancel_order(self, *, account, order_index):
        rec = self.orders.get(int(order_index))
        self.cancel_log.append(int(order_index))
        if rec:
            rec["status"] = "canceled"
            rec["taxonomy"] = "CANCELED"
            return True
        return False

    # --- simulation helpers ---
    def simulate_partial_fill(self, oid: int, new_position_size: Decimal) -> None:
        rec = self.orders[int(oid)]
        rec["filled_size"] = str(rec.get("filled_size") or "0")
        rec["taxonomy"] = "ACTIVE"
        rec["status"] = "open"
        self.position["size"] = str(new_position_size)

    def simulate_full_fill(self, oid: int, new_position_size: Decimal) -> None:
        rec = self.orders[int(oid)]
        rec["status"] = "filled"
        rec["taxonomy"] = "FILLED"
        self.position["size"] = str(new_position_size)


def _make_engine(direction: str = "BUY", step0: str = "0.200",
                 pct: str = "0.01"):
    cfg = GoldenFiboConfig(
        exchange="lighter", account="amiroo", instrument="SOL",
        direction=direction, percentage=Decimal(pct),
        step0_volume=Decimal(step0),
    )
    state = GoldenFiboState(
        registration_key=cfg.registration_key, exchange=cfg.exchange,
        account=cfg.account, instrument=cfg.instrument,
        direction=cfg.direction, percentage=cfg.percentage,
        step0_volume=cfg.step0_volume,
    )
    adapter = _LifecycleAuditAdapter(direction)
    counter = {"n": 100000}

    def nid() -> int:
        counter["n"] += 1
        return counter["n"]

    return GoldenFiboEngine(cfg, state, adapter, nid), adapter


def _setup_through_step1(direction: str = "BUY", step0: str = "0.200",
                          pct: str = "0.01"):
    """Step0 MARKET → confirm → TP + Step1 LIMIT."""
    eng, adapter = _make_engine(direction=direction, step0=step0, pct=pct)
    eng._start_fresh_cycle([])
    # For BUY with step0=0.200, pct=0.01, fake P0 = 76.126.
    p0 = Decimal("76.126")
    eng.confirm_step0_filled(p0)
    eng.place_step0_tp_and_step1(p0)
    return eng, adapter


def _client_id_factory():
    counter = {"n": 100000}

    def nid() -> int:
        counter["n"] += 1
        return counter["n"]

    return nid


# ---------------------------------------------------------------------------
# Scenario 1 (SELL): partial Step1 fill — TP volume syncs, TP price unchanged,
# no promotion to Step2, no Step2 place.
# ---------------------------------------------------------------------------
class TestSellPartialFillTpVolumeSync(unittest.TestCase):
    def test_sell_partial_ladder_fill_tp_volume_syncs(self):
        eng, adapter = _setup_through_step1(direction="SELL")
        tp_oid = eng.state.current_tp_order_id
        step1_oid = eng.state.pending_order_exchange_id
        tp_price_before = eng.state.current_tp_price
        tp_size_before = eng.state.current_tp_size
        submits_before = len(adapter.submit_log)
        cancels_before = len(adapter.cancel_log)

        # Partially fill Step1: SELL side, position grows from 0.200 → 0.214.
        adapter.simulate_partial_fill(step1_oid, Decimal("0.214"))

        result = eng.tick()

        # Running, not frozen.
        self.assertEqual(result.state.status, "running")
        # Step1 NOT promoted.
        self.assertEqual(eng.state.highest_filled_step, 0)
        self.assertEqual(eng.state.next_step, 1)
        # TP price unchanged.
        self.assertEqual(eng.state.current_tp_price, tp_price_before)
        # TP volume synced to live position (0.214). The old TP (oid tp_oid)
        # was canceled and a new one was placed at the SAME price for the new
        # volume — exactly one cancel + one TP submit.
        self.assertEqual(adapter.cancel_log.count(tp_oid), 1)
        new_tps = [s for s in adapter.submit_log[submits_before:]
                   if s.get("role") == "tp"]
        self.assertEqual(len(new_tps), 1)
        self.assertEqual(Decimal(new_tps[0]["price"]), tp_price_before)
        self.assertEqual(Decimal(new_tps[0]["size"]), Decimal("0.214"))
        self.assertEqual(new_tps[0]["side"], "buy")  # closing side for SELL
        self.assertTrue(new_tps[0]["reduce_only"])
        # No new ladder (Step2 not placed on partial).
        new_ladders = [s for s in adapter.submit_log[submits_before:]
                       if s.get("role") == "ladder"]
        self.assertEqual(len(new_ladders), 0)
        # new TP oid must differ from the old one.
        self.assertNotEqual(eng.state.current_tp_order_id, tp_oid)
        # Pending Step1 still alive (partial-fill remainder).
        self.assertEqual(eng.state.pending_order_exchange_id, step1_oid)
        # tp_size state updated.
        self.assertEqual(eng.state.current_tp_size, Decimal("0.214"))


# ---------------------------------------------------------------------------
# Scenario 2 (SELL): full Step1 fill — TP price rotates to P0 (= TP1), TP
# volume equals full live position, exactly one old TP cancel + one new TP
# place + one Step2 place.
# ---------------------------------------------------------------------------
class TestSellFullStep1FillTransition(unittest.TestCase):
    def test_sell_full_step1_fill_rotates_tp_to_p0_and_places_step2(self):
        eng, adapter = _setup_through_step1(direction="SELL")
        old_tp_oid = eng.state.current_tp_order_id
        step1_oid = eng.state.pending_order_exchange_id
        p0 = Decimal("76.126")
        submits_before = len(adapter.submit_log)

        # SELL: position grows from 0.200 → 0.400 on full Step1 fill.
        adapter.simulate_full_fill(step1_oid, Decimal("0.400"))

        result = eng.tick()

        # Not frozen.
        self.assertNotEqual(result.state.status, "needs_recovery")
        # Step1 promoted.
        self.assertEqual(eng.state.highest_filled_step, 1)
        self.assertEqual(eng.state.next_step, 2)
        # Exactly one old-TP cancel.
        self.assertEqual(adapter.cancel_log.count(old_tp_oid), 1)
        # Exactly one new TP at the new logical price (TP1 = P0 = 76.126) and
        # at the new full position size 0.400. Closing side for SELL = buy.
        new_tps = [s for s in adapter.submit_log[submits_before:]
                   if s.get("role") == "tp"]
        self.assertEqual(len(new_tps), 1)
        self.assertEqual(Decimal(new_tps[0]["price"]), p0)
        self.assertEqual(Decimal(new_tps[0]["size"]), Decimal("0.400"))
        self.assertEqual(new_tps[0]["side"], "buy")
        self.assertTrue(new_tps[0]["reduce_only"])
        # Exactly one Step2 ladder placed.
        new_ladders = [s for s in adapter.submit_log[submits_before:]
                       if s.get("role") == "ladder"]
        self.assertEqual(len(new_ladders), 1)
        # Step2 is a SELL LIMIT above P1 (for SELL). Volume = 2 * step0 =
        # 0.400 per the production doubling formula.
        self.assertEqual(Decimal(new_ladders[0]["size"]), Decimal("0.400"))
        self.assertEqual(new_ladders[0]["side"], "sell")
        self.assertFalse(new_ladders[0]["reduce_only"])
        # Engine state mirrors.
        self.assertEqual(eng.state.fill_prices[1], Decimal(adapter.orders[step1_oid]["price"]))
        self.assertEqual(eng.state.current_tp_price, p0)
        self.assertEqual(eng.state.current_tp_size, Decimal("0.400"))


# ---------------------------------------------------------------------------
# Scenario 7E: service restart into flat + orphan pending — robot must
# detect no position + pending ladder ACTIVE, cancel the orphan, and start
# a fresh Step0 MARKET cycle. Must NOT create a Step0 over the orphan.
# ---------------------------------------------------------------------------
class TestRestartFlatWithOrphanPending(unittest.TestCase):
    def test_restart_flat_with_orphan_pending_cancels_and_starts_fresh(self):
        # Build a state that represents: TP closed the position, Step1
        # ladder is still resting (orphan). This is the post-TP-exit state.
        adapter = _LifecycleAuditAdapter("BUY")
        cfg = GoldenFiboConfig(
            exchange="lighter", account="amiroo", instrument="SOL",
            direction="BUY", percentage=Decimal("0.01"),
            step0_volume=Decimal("0.200"),
        )
        # Persisted state: TP FILLED (live venue closed position), Step1
        # LIMIT still ACTIVE. highest_filled_step == 0, fill_prices[0]=P0,
        # expected cumulative == step0.
        persisted = GoldenFiboState.from_dict({
            "registration_key": cfg.registration_key,
            "cycle_id": 1,
            "exchange": "lighter",
            "account": "amiroo",
            "instrument": "SOL",
            "direction": "BUY",
            "percentage": "0.01",
            "step0_volume": "0.200",
            "highest_filled_step": 0,
            "fill_prices": {"0": "76.126"},
            "expected_cumulative_size": "0.200",
            "current_tp_price": None,  # TP is gone from venue
            "current_tp_size": None,
            "current_tp_order_id": None,
            "current_tp_client_id": None,
            "current_tp_role": None,
            "next_step": 1,
            "pending_order_client_id": 4242,
            "pending_order_exchange_id": 5001,
            "pending_requested_price": "73.472",
            "pending_requested_size": "0.200",
            "pending_confirmed_price": "73.472",
            "pending_confirmed_size": "0.200",
            "pending_order_role": "ladder",
            "status": "running",
        })

        # Live venue: no position, TP gone, Step1 LIMIT still ACTIVE.
        adapter.position = {"symbol": "SOL", "side": None, "size": "0",
                            "sl": None, "tp": None}
        adapter.orders[5001] = {
            "exchange_order_id": 5001, "client_order_id": 4242,
            "side": "buy", "type": "limit", "size": "0.200",
            "price": "73.472", "status": "open", "taxonomy": "ACTIVE",
            "reduce_only": False, "role": "ladder",
        }
        # Note: NO TP order in adapter.orders — venue has cleared it.

        engine = GoldenFiboEngine(cfg, persisted, adapter, _client_id_factory())

        # Tick 1: orphan detected → cancel → cycle reset.
        # Tick 2: with no position + no pending + no progress, Case A
        # triggers a fresh Step0 MARKET. The two-tick split is deliberate:
        # the engine never conflates a cancel with a Step0 submission in
        # one tick, so a cancel-venue-error does not leave a new Step0
        # sitting on top of an un-cancelled orphan.
        r1 = engine.tick()
        self.assertEqual(r1.state.status, "running")
        self.assertIn(5001, adapter.cancel_log)
        self.assertIsNone(engine.state.pending_order_exchange_id)
        self.assertEqual(engine.state.next_step, 0)
        self.assertEqual(engine.state.highest_filled_step, -1)
        self.assertGreaterEqual(engine.state.cycle_id, 2)
        # No Step0 yet on tick 1.
        tick1_submits = len(adapter.submit_log)
        self.assertEqual(
            [s.get("role") for s in adapter.submit_log], [],
            "tick 1 must NOT submit any new orders",
        )

        # Tick 2: fresh Step0 MARKET.
        r2 = engine.tick()
        self.assertEqual(r2.state.status, "running")
        new_entries = [s for s in adapter.submit_log if s.get("role") == "entry"]
        self.assertEqual(len(new_entries), 1)
        new_entry_oid = new_entries[0]["exchange_order_id"]
        self.assertNotEqual(new_entry_oid, 5001)
        # Engine state: pending is the new Step0.
        self.assertEqual(engine.state.pending_order_role, ROLE_ENTRY)
        self.assertEqual(engine.state.pending_order_exchange_id, new_entry_oid)
        # No Step1 over the orphan.
        new_ladders = [s for s in adapter.submit_log if s.get("role") == "ladder"]
        self.assertEqual(len(new_ladders), 0)
        # TP state still cleared (no TP set yet — that comes after P0 confirm).
        self.assertIsNone(engine.state.current_tp_order_id)
        self.assertIsNone(engine.state.current_tp_price)
        # cycle_id advanced once more on the new Step0 dispatch.
        self.assertGreaterEqual(engine.state.cycle_id, 3)


# ---------------------------------------------------------------------------
# Scenario 7F: service restart into flat + no pending — robot must auto-start
# a fresh Step0 MARKET cycle without manual /fibo input.
# ---------------------------------------------------------------------------
class TestRestartFlatNoPending(unittest.TestCase):
    def test_restart_flat_no_pending_starts_fresh_step0(self):
        adapter = _LifecycleAuditAdapter("BUY")
        cfg = GoldenFiboConfig(
            exchange="lighter", account="amiroo", instrument="SOL",
            direction="BUY", percentage=Decimal("0.01"),
            step0_volume=Decimal("0.200"),
        )
        # Persisted state: cycle_id=5 (any non-zero), no progress beyond
        # cycle start, no pending, no TP, no fill prices.
        persisted = GoldenFiboState.from_dict({
            "registration_key": cfg.registration_key,
            "cycle_id": 5,
            "exchange": "lighter",
            "account": "amiroo",
            "instrument": "SOL",
            "direction": "BUY",
            "percentage": "0.01",
            "step0_volume": "0.200",
            "highest_filled_step": -1,
            "fill_prices": {},
            "expected_cumulative_size": "0",
            "current_tp_price": None,
            "current_tp_size": None,
            "current_tp_order_id": None,
            "current_tp_client_id": None,
            "current_tp_role": None,
            "next_step": 0,
            "pending_order_client_id": None,
            "pending_order_exchange_id": None,
            "pending_requested_price": None,
            "pending_requested_size": None,
            "pending_confirmed_price": None,
            "pending_confirmed_size": None,
            "pending_order_role": None,
            "status": "running",
        })
        # Live venue: completely flat.
        adapter.position = {"symbol": "SOL", "side": None, "size": "0",
                            "sl": None, "tp": None}

        engine = GoldenFiboEngine(cfg, persisted, adapter, _client_id_factory())
        result = engine.tick()

        # A fresh Step0 MARKET was placed.
        new_entries = [s for s in adapter.submit_log if s.get("role") == "entry"]
        self.assertEqual(len(new_entries), 1)
        self.assertEqual(new_entries[0]["type"], "market")
        self.assertEqual(new_entries[0]["side"], "buy")
        # Volume is step0 (0.200).
        self.assertEqual(Decimal(new_entries[0]["size"]), Decimal("0.200"))
        # Engine state: pending entry created with the new oid.
        new_oid = new_entries[0]["exchange_order_id"]
        self.assertEqual(engine.state.pending_order_exchange_id, new_oid)
        self.assertEqual(engine.state.pending_order_role, ROLE_ENTRY)
        # Cycle was NOT incremented yet (it increments inside
        # _start_fresh_cycle once the new order is dispatched; on a tick
        # that dispatches, cycle_id will be one greater than persisted).
        self.assertGreaterEqual(engine.state.cycle_id, 6)
        # Status remains running (not needs_recovery).
        self.assertEqual(result.state.status, "running")
        # No TP submitted yet (TP0 comes after P0 confirmation).
        new_tps = [s for s in adapter.submit_log if s.get("role") == "tp"]
        self.assertEqual(len(new_tps), 0)
        # No ladder placed.
        new_ladders = [s for s in adapter.submit_log if s.get("role") == "ladder"]
        self.assertEqual(len(new_ladders), 0)


if __name__ == "__main__":
    unittest.main()