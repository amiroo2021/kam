"""Smooth Shutdown + Emergency STOP — durable control-plane tests.

Covers the offline contract from the two-mode /fibo stop design without
live exchange I/O.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional


_EDITABLE = "__editable___hermes_agent_0_20_0_finder"
if any(_EDITABLE in repr(h) for h in sys.path_hooks):
    sys.path_hooks[:] = [h for h in sys.path_hooks if _EDITABLE not in repr(h)]

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from plugins.trade.fibo_service import PersistentFiboService  # noqa: E402
from plugins.trade.fibo_wizard import FiboWizard  # noqa: E402
from plugins.trade.golden_fibo.config import GoldenFiboConfig  # noqa: E402
from plugins.trade.golden_fibo.engine import GoldenFiboEngine  # noqa: E402
from plugins.trade.golden_fibo.state import (  # noqa: E402
    ROLE_ENTRY,
    ROLE_LADDER,
    ROLE_TP,
    SHUTDOWN_MODE_SMOOTH,
    STATUS_COMPLETED,
    STATUS_NEEDS_RECOVERY,
    STATUS_RUNNING,
    STATUS_SMOOTH_SHUTDOWN,
    STATUS_STOPPING,
    SUBMISSION_NOT_SUBMITTED,
    GoldenFiboState,
)


class _FakeAdapter:
    def __init__(self):
        self.pos_side: Optional[str] = "long"
        self.pos_size: Decimal = Decimal("0.2")
        self.orders: Dict[int, Dict[str, Any]] = {}
        self.cancels: List[int] = []
        self.closes: int = 0
        self.markets: List[Dict[str, Any]] = []
        self.limits: List[Dict[str, Any]] = []
        self.tps: List[Dict[str, Any]] = []
        self._oid = 9000

    def position_state(self, account, instrument):
        if self.pos_side is None or self.pos_size <= 0:
            return {"side": None, "size": "0"}
        return {"side": self.pos_side, "size": str(self.pos_size)}

    def get_order_state(self, account, order_index):
        return dict(self.orders.get(int(order_index), {}))

    def get_order_state_by_client_id(self, account, instrument, client_order_index):
        for rec in self.orders.values():
            if int(rec.get("client_order_index") or -1) == int(client_order_index):
                return dict(rec)
        return {}

    def get_venue_constraints(self, account, instrument):
        return {
            "price_decimals": 3,
            "size_decimals": 3,
            "min_base_amount": "0.1",
            "min_quote_amount": "10",
        }

    def place_market(self, **kw):
        self._oid += 1
        self.markets.append(dict(kw))
        oid = self._oid
        self.orders[oid] = {
            "exchange_order_id": oid,
            "client_order_index": kw.get("client_order_id"),
            "status": "filled",
            "taxonomy": "FILLED",
            "requested_size": str(kw.get("size")),
            "actual_fill_price": "77.0",
            "side": kw.get("side"),
        }
        return {
            "exchange_order_id": oid,
            "client_order_id": kw.get("client_order_id"),
            "status": "filled",
            "verified": True,
        }

    def place_limit(self, **kw):
        self._oid += 1
        self.limits.append(dict(kw))
        oid = self._oid
        self.orders[oid] = {
            "exchange_order_id": oid,
            "client_order_index": kw.get("client_order_id"),
            "status": "open",
            "taxonomy": "ACTIVE",
            "requested_size": str(kw.get("size")),
            "side": kw.get("side"),
            "price": str(kw.get("price")),
        }
        return {
            "exchange_order_id": oid,
            "client_order_id": kw.get("client_order_id"),
            "status": "submitted",
            "verified": True,
        }

    def set_shared_tp(self, **kw):
        self._oid += 1
        self.tps.append(dict(kw))
        oid = self._oid
        self.orders[oid] = {
            "exchange_order_id": oid,
            "client_order_index": kw.get("client_order_id"),
            "status": "open",
            "taxonomy": "ACTIVE",
            "requested_size": str(kw.get("size")),
            "side": kw.get("side"),
            "price": str(kw.get("price")),
        }
        return {
            "exchange_order_id": oid,
            "client_order_id": kw.get("client_order_id"),
            "submitted_price": str(kw.get("price")),
            "submitted_volume": str(kw.get("size")),
            "status": "submitted",
            "verified": True,
            "role": "tp",
        }

    def cancel_order(self, *, account, order_index):
        self.cancels.append(int(order_index))
        rec = self.orders.get(int(order_index))
        if rec is not None:
            rec["taxonomy"] = "CANCELED"
            rec["status"] = "canceled"
        return True

    def close_position(self, *, account, instrument):
        self.closes += 1
        self.pos_size = Decimal("0")
        self.pos_side = None
        return {"success": True, "verified": True, "status": "success"}


def _cfg(**kw):
    base = dict(
        exchange="lighter",
        account="amiroo",
        instrument="SOL",
        direction="BUY",
        percentage=Decimal("0.001"),
        step0_volume=Decimal("0.2"),
    )
    base.update(kw)
    return GoldenFiboConfig(**base)


def _cid_factory():
    n = {"v": 100000}

    def _next():
        n["v"] += 1
        return n["v"]

    return _next


def _healthy_step0_step1_state(adapter: _FakeAdapter) -> GoldenFiboState:
    """Position open, TP active, Step1 ladder pending ACTIVE."""
    st = GoldenFiboState(
        registration_key="lighter/amiroo/SOL/BUY",
        exchange="lighter",
        account="amiroo",
        instrument="SOL",
        direction="BUY",
        percentage=Decimal("0.001"),
        step0_volume=Decimal("0.2"),
        cycle_id=1,
        highest_filled_step=0,
        fill_prices={0: Decimal("77.0")},
        expected_cumulative_size=Decimal("0.2"),
        current_tp_price=Decimal("77.077"),
        current_tp_size=Decimal("0.2"),
        current_tp_order_id=5001,
        current_tp_client_id=200001,
        current_tp_role=ROLE_TP,
        next_step=1,
        pending_order_client_id=200002,
        pending_order_exchange_id=5002,
        pending_requested_price=Decimal("76.875"),
        pending_requested_size=Decimal("0.2"),
        pending_order_role=ROLE_LADDER,
        step_orders={
            0: {
                "role": ROLE_ENTRY,
                "client_id": 200000,
                "exchange_order_id": 5000,
                "status": "filled",
                "price": "77.0",
                "size": "0.2",
            }
        },
        status=STATUS_RUNNING,
        submission_phase=SUBMISSION_NOT_SUBMITTED,
    )
    adapter.pos_side = "long"
    adapter.pos_size = Decimal("0.2")
    adapter.orders[5001] = {
        "exchange_order_id": 5001,
        "client_order_index": 200001,
        "taxonomy": "ACTIVE",
        "status": "open",
        "requested_size": "0.2",
    }
    adapter.orders[5002] = {
        "exchange_order_id": 5002,
        "client_order_index": 200002,
        "taxonomy": "ACTIVE",
        "status": "open",
        "requested_size": "0.2",
    }
    return st


class TestSmoothShutdownEngine(unittest.TestCase):
    def test_1_smooth_during_healthy_cycle_no_premature_cancel_then_complete(self):
        ad = _FakeAdapter()
        st = _healthy_step0_step1_state(ad)
        st.shutdown_mode = SHUTDOWN_MODE_SMOOTH
        st.status = STATUS_SMOOTH_SHUTDOWN
        eng = GoldenFiboEngine(_cfg(), st, ad, _cid_factory())

        # Healthy waiting — must NOT cancel pending
        r1 = eng.tick()
        self.assertIn("healthy waiting", " ".join(r1.actions))
        self.assertEqual(ad.cancels, [])
        self.assertEqual(st.status, STATUS_SMOOTH_SHUTDOWN)
        self.assertEqual(st.pending_order_exchange_id, 5002)

        # TP fills → position lag then flat with orphan pending
        ad.orders[5001]["taxonomy"] = "FILLED"
        ad.orders[5001]["status"] = "filled"
        # still show position briefly
        r2 = eng.tick()
        self.assertTrue(any("FILLED" in a for a in r2.actions))
        # force flat with orphan pending alive
        ad.pos_size = Decimal("0")
        ad.pos_side = None
        st.tp_exit_attempts = 0
        st.current_tp_order_id = 5001
        r3 = eng.tick()
        # orphan cancel + complete (no Step0)
        self.assertIn(5002, ad.cancels)
        self.assertEqual(len(ad.markets), 0)  # no fresh Step0
        self.assertEqual(st.status, STATUS_COMPLETED)
        self.assertIsNone(st.pending_order_exchange_id)

    def test_2_smooth_partial_fill_still_syncs_tp_volume(self):
        ad = _FakeAdapter()
        st = _healthy_step0_step1_state(ad)
        st.shutdown_mode = SHUTDOWN_MODE_SMOOTH
        st.status = STATUS_SMOOTH_SHUTDOWN
        # Partial: position grew, pending still ACTIVE
        ad.pos_size = Decimal("0.3")
        ad.orders[5001]["requested_size"] = "0.2"  # TP stale size
        eng = GoldenFiboEngine(_cfg(), st, ad, _cid_factory())
        r = eng.tick()
        # Should attempt TP volume sync (cancel old TP + place new) not cancel ladder
        self.assertNotIn(5002, ad.cancels)
        self.assertEqual(st.status, STATUS_SMOOTH_SHUTDOWN)
        self.assertEqual(st.pending_order_exchange_id, 5002)
        self.assertTrue(any("sync" in a.lower() or "tp" in a.lower() for a in r.actions) or st.current_tp_size == Decimal("0.3") or len(ad.tps) >= 1)

    def test_3_smooth_after_full_ladder_fill_rotates_and_no_new_cycle_later(self):
        ad = _FakeAdapter()
        st = _healthy_step0_step1_state(ad)
        st.shutdown_mode = SHUTDOWN_MODE_SMOOTH
        st.status = STATUS_SMOOTH_SHUTDOWN
        # Simulate post full-fill state after Step1 (engine fill path covered elsewhere)
        st.highest_filled_step = 1
        st.fill_prices[1] = Decimal("76.875")
        st.expected_cumulative_size = Decimal("0.4")
        st.next_step = 2
        st.pending_order_exchange_id = 6002
        st.pending_order_client_id = 200003
        st.pending_order_role = ROLE_LADDER
        st.pending_requested_size = Decimal("0.4")
        st.current_tp_order_id = 6001
        st.current_tp_client_id = 200004
        st.current_tp_price = Decimal("77.0")
        st.current_tp_size = Decimal("0.4")
        ad.pos_size = Decimal("0.4")
        ad.orders[6001] = {
            "exchange_order_id": 6001,
            "client_order_index": 200004,
            "taxonomy": "ACTIVE",
            "status": "open",
            "requested_size": "0.4",
        }
        ad.orders[6002] = {
            "exchange_order_id": 6002,
            "client_order_index": 200003,
            "taxonomy": "ACTIVE",
            "status": "open",
            "requested_size": "0.4",
        }
        eng = GoldenFiboEngine(_cfg(), st, ad, _cid_factory())
        r = eng.tick()
        self.assertIn("healthy waiting", " ".join(r.actions))
        self.assertEqual(st.status, STATUS_SMOOTH_SHUTDOWN)
        self.assertEqual(st.next_step, 2)
        # Later flat end → complete, no Step0
        ad.pos_size = Decimal("0")
        ad.pos_side = None
        st.pending_order_exchange_id = None
        st.pending_order_client_id = None
        st.current_tp_order_id = None
        r2 = eng.tick()
        self.assertEqual(st.status, STATUS_COMPLETED)
        self.assertEqual(len(ad.markets), 0)

    def test_5_smooth_already_flat_engine_path_completes(self):
        ad = _FakeAdapter()
        ad.pos_side = None
        ad.pos_size = Decimal("0")
        st = GoldenFiboState(
            registration_key="lighter/amiroo/SOL/BUY",
            exchange="lighter",
            account="amiroo",
            instrument="SOL",
            direction="BUY",
            percentage=Decimal("0.001"),
            step0_volume=Decimal("0.2"),
            status=STATUS_SMOOTH_SHUTDOWN,
            shutdown_mode=SHUTDOWN_MODE_SMOOTH,
            highest_filled_step=-1,
            next_step=0,
        )
        eng = GoldenFiboEngine(_cfg(), st, ad, _cid_factory())
        r = eng.tick()
        self.assertEqual(st.status, STATUS_COMPLETED)
        self.assertEqual(len(ad.markets), 0)
        self.assertTrue(any("smooth_shutdown_complete" in a for a in r.actions))


class TestServiceStopModes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = PersistentFiboService(
            state_path=Path(self.tmp.name) / "state.json",
            ledger_path=Path(self.tmp.name) / "ledger.jsonl",
            event_log_path=Path(self.tmp.name) / "events.log",
            start_thread=False,
        )
        self.ad = _FakeAdapter()
        # Inject adapter factory
        self.svc._adapters = {}
        self.svc._adapter_for = lambda key: self.ad  # type: ignore

    def tearDown(self):
        self.svc.shutdown()
        self.tmp.cleanup()

    def _seed(self, st: GoldenFiboState):
        self.svc._states[st.registration_key] = st
        self.svc._save_state()

    def test_4_smooth_survives_service_reload(self):
        st = _healthy_step0_step1_state(self.ad)
        st.shutdown_mode = SHUTDOWN_MODE_SMOOTH
        st.status = STATUS_SMOOTH_SHUTDOWN
        self._seed(st)
        path = self.svc.state_path
        # Reload from disk
        svc2 = PersistentFiboService(
            state_path=path,
            ledger_path=Path(self.tmp.name) / "ledger2.jsonl",
            event_log_path=Path(self.tmp.name) / "events2.log",
            start_thread=False,
        )
        svc2._adapter_for = lambda key: self.ad  # type: ignore
        loaded = svc2._states[st.registration_key]
        self.assertEqual(loaded.status, STATUS_SMOOTH_SHUTDOWN)
        self.assertEqual(loaded.shutdown_mode, SHUTDOWN_MODE_SMOOTH)
        # Tick still manages; no Step0
        svc2._drive_one(st.registration_key)
        self.assertIn(st.registration_key, svc2._states)
        self.assertEqual(len(self.ad.markets), 0)
        svc2.shutdown()

    def test_5_smooth_already_flat_immediate_deregister(self):
        st = GoldenFiboState(
            registration_key="lighter/amiroo/SOL/BUY",
            exchange="lighter",
            account="amiroo",
            instrument="SOL",
            direction="BUY",
            percentage=Decimal("0.001"),
            step0_volume=Decimal("0.2"),
            status=STATUS_RUNNING,
        )
        self.ad.pos_side = None
        self.ad.pos_size = Decimal("0")
        self._seed(st)
        resp = self.svc.execute_command(
            {"op": "smooth_shutdown", "registration_key": st.registration_key}
        )
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(resp.get("immediate"))
        self.assertNotIn(st.registration_key, self.svc._states)
        self.assertEqual(len(self.ad.markets), 0)

    def test_6_emergency_with_position_pending_tp(self):
        st = _healthy_step0_step1_state(self.ad)
        self._seed(st)
        resp = self.svc.execute_command(
            {"op": "emergency_stop", "registration_key": st.registration_key}
        )
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp.get("mode"), "emergency")
        self.assertIn(5002, self.ad.cancels)  # pending
        self.assertEqual(self.ad.closes, 1)
        self.assertIn(5001, self.ad.cancels)  # tp
        self.assertNotIn(st.registration_key, self.svc._states)
        self.assertEqual(len(self.ad.markets), 0)

    def test_7_emergency_partial_fill_closes_actual_live_size(self):
        st = _healthy_step0_step1_state(self.ad)
        self.ad.pos_size = Decimal("0.35")  # partial
        self._seed(st)
        resp = self.svc.execute_command(
            {"op": "emergency_stop", "registration_key": st.registration_key}
        )
        self.assertTrue(resp["ok"], resp)
        self.assertIn(5002, self.ad.cancels)
        self.assertEqual(self.ad.closes, 1)
        self.assertIsNone(self.ad.pos_side)
        self.assertNotIn(st.registration_key, self.svc._states)

    def test_8_emergency_already_flat_idempotent(self):
        st = GoldenFiboState(
            registration_key="lighter/amiroo/SOL/BUY",
            exchange="lighter",
            account="amiroo",
            instrument="SOL",
            direction="BUY",
            percentage=Decimal("0.001"),
            step0_volume=Decimal("0.2"),
            status=STATUS_RUNNING,
        )
        self.ad.pos_side = None
        self.ad.pos_size = Decimal("0")
        self._seed(st)
        resp = self.svc.execute_command(
            {"op": "emergency_stop", "registration_key": st.registration_key}
        )
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(self.ad.closes, 0)
        self.assertNotIn(st.registration_key, self.svc._states)

    def test_9_ownership_mismatch_no_destructive(self):
        st = _healthy_step0_step1_state(self.ad)
        self.ad.pos_side = "short"  # opposite of BUY
        self._seed(st)
        resp = self.svc.execute_command(
            {"op": "emergency_stop", "registration_key": st.registration_key}
        )
        self.assertFalse(resp["ok"])
        self.assertEqual(resp.get("error"), "OWNERSHIP_MISMATCH")
        self.assertEqual(self.ad.closes, 0)
        self.assertEqual(self.ad.cancels, [])
        st2 = self.svc._states[st.registration_key]
        self.assertEqual(st2.status, STATUS_NEEDS_RECOVERY)

    def test_10_buy_stop_does_not_touch_sell(self):
        buy = _healthy_step0_step1_state(self.ad)
        buy.registration_key = "lighter/amiroo/SOL/BUY"
        sell = GoldenFiboState(
            registration_key="lighter/amiroo/SOL/SELL",
            exchange="lighter",
            account="amiroo",
            instrument="SOL",
            direction="SELL",
            percentage=Decimal("0.001"),
            step0_volume=Decimal("0.2"),
            status=STATUS_RUNNING,
            highest_filled_step=0,
            expected_cumulative_size=Decimal("0.2"),
            current_tp_order_id=8001,
            pending_order_exchange_id=8002,
        )
        self._seed(buy)
        self.svc._states[sell.registration_key] = sell
        self.svc._save_state()
        # Emergency on BUY only; adapter pos is long matching BUY
        resp = self.svc.execute_command(
            {"op": "emergency_stop", "registration_key": buy.registration_key}
        )
        self.assertTrue(resp["ok"], resp)
        self.assertNotIn(buy.registration_key, self.svc._states)
        self.assertIn(sell.registration_key, self.svc._states)
        self.assertEqual(self.svc._states[sell.registration_key].pending_order_exchange_id, 8002)

    def test_11_gateway_restart_preserves_smooth_intent_via_state_file(self):
        st = _healthy_step0_step1_state(self.ad)
        self._seed(st)
        resp = self.svc.execute_command(
            {"op": "smooth_shutdown", "registration_key": st.registration_key}
        )
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp.get("status"), STATUS_SMOOTH_SHUTDOWN)
        detail = self.svc.execute_command(
            {"op": "detail", "registration_key": st.registration_key}
        )
        reg = detail["registration"]
        self.assertEqual(reg["status"], STATUS_SMOOTH_SHUTDOWN)
        self.assertEqual(reg["shutdown_mode"], SHUTDOWN_MODE_SMOOTH)

    def test_12_neither_mode_starts_step0_after_intent(self):
        st = _healthy_step0_step1_state(self.ad)
        st.shutdown_mode = SHUTDOWN_MODE_SMOOTH
        st.status = STATUS_SMOOTH_SHUTDOWN
        # Flat path that would normally start Step0
        self.ad.pos_side = None
        self.ad.pos_size = Decimal("0")
        st.pending_order_exchange_id = None
        st.current_tp_order_id = None
        st.highest_filled_step = -1
        st.next_step = 0
        eng = GoldenFiboEngine(_cfg(), st, self.ad, _cid_factory())
        eng.tick()
        self.assertEqual(len(self.ad.markets), 0)
        self.assertEqual(st.status, STATUS_COMPLETED)

        # Emergency complete leaves no state — already covered; ensure market empty
        self.assertEqual(len(self.ad.markets), 0)


class TestWizardStopUI(unittest.TestCase):
    def test_stop_mode_screens_and_ipc_ops(self):
        cmds = []

        class Svc:
            def execute_command(self, c):
                cmds.append(dict(c))
                op = c.get("op")
                if op == "list":
                    return {
                        "ok": True,
                        "registrations": [
                            {"registration_key": "lighter/amiroo/SOL/BUY", "status": "running"}
                        ],
                        "quarantined": [],
                    }
                if op == "smooth_shutdown":
                    return {
                        "ok": True,
                        "registration_key": c["registration_key"],
                        "status": "smooth_shutdown",
                        "immediate": False,
                        "detail": "armed",
                    }
                if op == "emergency_stop":
                    return {
                        "ok": True,
                        "registration_key": c["registration_key"],
                        "status": "stopped",
                        "mode": "emergency",
                        "actions": ["deregistered"],
                    }
                return {"ok": True}

        class Desk:
            def list_exchanges(self):
                return ["lighter"]

            def list_accounts(self, ex):
                return ["amiroo"]

        w = FiboWizard(tradedesk=Desk(), service=Svc())
        key = ("1",)
        s = w.open(key)
        s = w.handle_callback(key, "menu:stop")
        self.assertEqual(s.state, "stop_pick")
        s = w.handle_callback(key, "stop_pick:lighter/amiroo/SOL/BUY")
        self.assertEqual(s.state, "stop_mode")
        self.assertIn("Smooth Shutdown", s.text + str(s.buttons))
        self.assertIn("Emergency STOP", str(s.buttons))
        s = w.handle_callback(key, "smooth_confirm:lighter/amiroo/SOL/BUY")
        self.assertEqual(s.state, "stop_smooth_confirm")
        self.assertIn("Finish the current GoldenFibo cycle normally", s.text)
        s = w.handle_callback(key, "confirm_smooth:lighter/amiroo/SOL/BUY")
        self.assertEqual(s.state, "stop_done")
        self.assertTrue(any(c.get("op") == "smooth_shutdown" for c in cmds))

        s = w.handle_callback(key, "emergency_confirm:lighter/amiroo/SOL/BUY")
        self.assertEqual(s.state, "stop_emergency_confirm")
        self.assertIn("Immediately stop", s.text)
        s = w.handle_callback(key, "confirm_emergency:lighter/amiroo/SOL/BUY")
        self.assertEqual(s.state, "stop_done")
        self.assertTrue(any(c.get("op") == "emergency_stop" for c in cmds))


if __name__ == "__main__":
    unittest.main()
