"""Emergency STOP post-close verification tests (Arcus-focused, offline)."""

from __future__ import annotations

import time
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional
from unittest import mock

from plugins.trade.fibo_service import (
    SHUTDOWN_MODE_EMERGENCY,
    STATUS_NEEDS_RECOVERY,
    STATUS_RUNNING,
    STATUS_STOPPING,
    PersistentFiboService,
)
from plugins.trade.golden_fibo.state import GoldenFiboState


class _AdapterBase:
    name = "golden_fibo_arcus"

    def __init__(self) -> None:
        self.closes = 0
        self.close_ids: List[Any] = []
        self.cancels: List[Any] = []
        self.pos_reads = 0
        self.orders: Dict[int, Dict[str, Any]] = {}
        self.pos_side: Optional[str] = "long"
        self.pos_size: Decimal = Decimal("0.2")
        self.close_verified_after = 0  # pos reads after close before flat

    def position_state(self, account, instrument):
        self.pos_reads += 1
        if self.closes and self.pos_reads > self.close_verified_after + self._reads_before_close():
            return {"symbol": instrument, "side": None, "size": "0"}
        if self.pos_side and self.pos_size > 0:
            return {
                "symbol": instrument,
                "side": self.pos_side,
                "size": str(self.pos_size),
            }
        return {"symbol": instrument, "side": None, "size": "0"}

    def _reads_before_close(self) -> int:
        # initial + post_cancel before close
        return 2

    def cancel_order(self, **kw):
        oid = kw.get("order_index")
        self.cancels.append(oid)
        self.orders.pop(int(oid), None)
        return False  # force idempotent path unless overridden

    def close_position(self, **kw):
        self.closes += 1
        self.close_ids.append(kw.get("client_order_id"))
        return {
            "success": True,
            "verified": False,
            "status": "submitted",
            "client_order_id": kw.get("client_order_id"),
            "raw": {
                "position_action": {
                    "exchange_order_id": 999,
                    "status": "submitted",
                    "verified": False,
                }
            },
        }

    def get_order_state(self, account, order_index):
        return dict(self.orders.get(int(order_index)) or {})


class EmergencyVerifyTests(unittest.TestCase):
    def _svc(self, tmp: str) -> PersistentFiboService:
        return PersistentFiboService(
            state_path=Path(tmp) / "s.json",
            ledger_path=Path(tmp) / "l.jsonl",
            event_log_path=Path(tmp) / "e.log",
            start_thread=False,
        )

    def _reg(self, svc, ad, key="arcus/metamask/SOL-USD/BUY"):
        st = GoldenFiboState(
            registration_key=key,
            exchange="arcus",
            account="metamask",
            instrument="SOL-USD",
            direction="BUY",
            status=STATUS_RUNNING,
            pending_order_exchange_id=111,
            current_tp_order_id=222,
            cycle_uid=42,
            highest_filled_step=0,
            client_id_version=2,
            step0_volume=Decimal("0.2"),
            percentage=Decimal("0.001"),
        )
        ad.orders[111] = {"taxonomy": "ACTIVE", "status": "open"}
        ad.orders[222] = {"taxonomy": "ACTIVE", "status": "untriggered"}
        svc._states[key] = st
        svc._adapters[key] = ad
        return key, st

    def test_close_submit_then_flat_second_read_succeeds(self):
        with TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            ad = _AdapterBase()
            ad.close_verified_after = 1  # one flat_wait after close still open
            key, st = self._reg(svc, ad)
            with mock.patch("plugins.trade.fibo_service.time.sleep", return_value=None):
                resp = svc.execute_command({"op": "emergency_stop", "registration_key": key})
            self.assertTrue(resp.get("ok"), resp)
            self.assertEqual(ad.closes, 1)
            self.assertNotIn(key, svc._states)

    def test_close_submit_429_then_flat_no_second_close(self):
        class FlakyPos(_AdapterBase):
            def __init__(self):
                super().__init__()
                self._n = 0

            def position_state(self, account, instrument):
                self._n += 1
                self.pos_reads += 1
                # After close: first flat_wait 429, then flat
                if self.closes:
                    if self._n == self._reads_before_close() + 1:
                        raise RuntimeError("429 Client Error: Too Many Requests")
                    return {"symbol": instrument, "side": None, "size": "0"}
                return super().position_state(account, instrument)

        with TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            ad = FlakyPos()
            key, st = self._reg(svc, ad)
            with mock.patch("plugins.trade.fibo_service.time.sleep", return_value=None):
                resp = svc.execute_command({"op": "emergency_stop", "registration_key": key})
            self.assertTrue(resp.get("ok"), resp)
            self.assertEqual(ad.closes, 1)

    def test_close_submit_timeout_needs_recovery_one_close(self):
        class AlwaysOpen(_AdapterBase):
            def position_state(self, account, instrument):
                self.pos_reads += 1
                return {"symbol": instrument, "side": "long", "size": "0.2"}

        with TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            ad = AlwaysOpen()
            key, st = self._reg(svc, ad)
            with mock.patch("plugins.trade.fibo_service.time.sleep", return_value=None):
                # Shrink timeout for test speed
                with mock.patch.object(
                    PersistentFiboService,
                    "_cmd_emergency_stop",
                    wraps=svc._cmd_emergency_stop,
                ):
                    # Monkeypatch wait by reducing delays via sleep=0 already;
                    # still may take several iterations — force total_timeout small
                    import plugins.trade.fibo_service as fs

                    orig = svc._cmd_emergency_stop

                    def wrapped(cmd):
                        # Call original but we can't easily inject timeout;
                        # instead make AlwaysOpen and patch time.time to expire.
                        return orig(cmd)

                    # Use time advancement
                    t0 = {"v": 1000.0}

                    def fake_time():
                        t0["v"] += 20.0
                        return t0["v"]

                    with mock.patch("plugins.trade.fibo_service.time.time", side_effect=fake_time):
                        resp = svc.execute_command({"op": "emergency_stop", "registration_key": key})
            self.assertFalse(resp.get("ok"))
            self.assertEqual(ad.closes, 1)
            st2 = svc._states[key]
            self.assertEqual(st2.emergency_close_phase, "submitted")
            self.assertEqual(st2.shutdown_mode, SHUTDOWN_MODE_EMERGENCY)
            self.assertIsNotNone(st2.emergency_close_client_id)

    def test_cancel_false_but_absent_is_success(self):
        class GoneOrders(_AdapterBase):
            def cancel_order(self, **kw):
                self.cancels.append(kw.get("order_index"))
                return False

            def get_order_state(self, account, order_index):
                return {}  # absent

            def position_state(self, account, instrument):
                self.pos_reads += 1
                if self.closes:
                    return {"symbol": instrument, "side": None, "size": "0"}
                return {"symbol": instrument, "side": "long", "size": "0.2"}

        with TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            ad = GoneOrders()
            key, st = self._reg(svc, ad)
            with mock.patch("plugins.trade.fibo_service.time.sleep", return_value=None):
                resp = svc.execute_command({"op": "emergency_stop", "registration_key": key})
            self.assertTrue(resp.get("ok"), resp)
            # pending + tp cancels attempted
            self.assertIn(111, ad.cancels)
            self.assertIn(222, ad.cancels)

    def test_tp_gone_after_flat_noop(self):
        class TpGone(_AdapterBase):
            def cancel_order(self, **kw):
                self.cancels.append(kw.get("order_index"))
                return False

            def get_order_state(self, account, order_index):
                return {}

            def position_state(self, account, instrument):
                self.pos_reads += 1
                if self.closes:
                    return {"symbol": instrument, "side": None, "size": "0"}
                return {"symbol": instrument, "side": "long", "size": "0.2"}

        with TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            ad = TpGone()
            key, st = self._reg(svc, ad)
            with mock.patch("plugins.trade.fibo_service.time.sleep", return_value=None):
                resp = svc.execute_command({"op": "emergency_stop", "registration_key": key})
            self.assertTrue(resp.get("ok"), resp)

    def test_tp_remains_after_flat_is_canceled(self):
        class TpStays(_AdapterBase):
            def __init__(self):
                super().__init__()
                self.tp_cancel_calls = 0

            def cancel_order(self, **kw):
                oid = int(kw.get("order_index"))
                self.cancels.append(oid)
                if oid == 222:
                    self.tp_cancel_calls += 1
                    if self.tp_cancel_calls >= 1:
                        self.orders.pop(222, None)
                        return True
                    return False
                self.orders.pop(oid, None)
                return True

            def get_order_state(self, account, order_index):
                return dict(self.orders.get(int(order_index)) or {})

            def position_state(self, account, instrument):
                self.pos_reads += 1
                if self.closes:
                    return {"symbol": instrument, "side": None, "size": "0"}
                return {"symbol": instrument, "side": "long", "size": "0.2"}

        with TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            ad = TpStays()
            ad.orders[222] = {"taxonomy": "ACTIVE", "status": "open"}
            key, st = self._reg(svc, ad)
            with mock.patch("plugins.trade.fibo_service.time.sleep", return_value=None):
                resp = svc.execute_command({"op": "emergency_stop", "registration_key": key})
            self.assertTrue(resp.get("ok"), resp)
            self.assertIn(222, ad.cancels)

    def test_emergency_blocks_normal_poll(self):
        with TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            key = "arcus/metamask/SOL-USD/BUY"
            st = GoldenFiboState(
                registration_key=key,
                exchange="arcus",
                account="metamask",
                instrument="SOL-USD",
                direction="BUY",
                status=STATUS_STOPPING,
                shutdown_mode=SHUTDOWN_MODE_EMERGENCY,
                emergency_close_phase="submitted",
            )
            svc._states[key] = st
            called = {"n": 0}

            def boom(k):
                called["n"] += 1

            svc._drive_one = boom  # type: ignore
            svc._tick_once()
            self.assertEqual(called["n"], 0)

    def test_close_id_once_and_resume_no_duplicate(self):
        class Resume(_AdapterBase):
            def position_state(self, account, instrument):
                self.pos_reads += 1
                # Always flat after any close attempt / resume
                if self.closes or True:
                    # first two reads open, then after submitted phase resume we are flat
                    if self.pos_reads <= 2 and self.closes == 0:
                        return {"symbol": instrument, "side": "long", "size": "0.2"}
                    return {"symbol": instrument, "side": None, "size": "0"}
                return {"symbol": instrument, "side": "long", "size": "0.2"}

        with TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            ad = Resume()
            key, st = self._reg(svc, ad)
            # First emergency: submit close, force timeout-ish by always open during wait
            ad2 = _AdapterBase()
            ad2.pos_side = "long"
            ad2.pos_size = Decimal("0.2")

            class NeverFlat(ad2.__class__):
                def position_state(self, account, instrument):
                    self.pos_reads += 1
                    if not self.closes:
                        return {"symbol": instrument, "side": "long", "size": "0.2"}
                    # stay open during wait
                    return {"symbol": instrument, "side": "long", "size": "0.2"}

            ad_nf = NeverFlat()
            ad_nf.orders = {111: {"taxonomy": "ACTIVE", "status": "open"}, 222: {"taxonomy": "ACTIVE", "status": "open"}}
            svc._adapters[key] = ad_nf
            t0 = {"v": 0.0}

            def fake_time():
                t0["v"] += 30.0
                return t0["v"]

            with mock.patch("plugins.trade.fibo_service.time.sleep", return_value=None):
                with mock.patch("plugins.trade.fibo_service.time.time", side_effect=fake_time):
                    resp1 = svc.execute_command({"op": "emergency_stop", "registration_key": key})
            self.assertFalse(resp1.get("ok"))
            self.assertEqual(ad_nf.closes, 1)
            cid = svc._states[key].emergency_close_client_id
            self.assertIsNotNone(cid)
            phase = svc._states[key].emergency_close_phase
            self.assertEqual(phase, "submitted")

            # Resume: now flats immediately, must NOT close again
            class FlatNow(NeverFlat):
                def position_state(self, account, instrument):
                    self.pos_reads += 1
                    return {"symbol": instrument, "side": None, "size": "0"}

            ad_flat = FlatNow()
            ad_flat.closes = ad_nf.closes
            ad_flat.close_ids = list(ad_nf.close_ids)
            ad_flat.orders = {}
            svc._adapters[key] = ad_flat
            # keep same emergency_close_client_id on state
            with mock.patch("plugins.trade.fibo_service.time.sleep", return_value=None):
                resp2 = svc.execute_command({"op": "emergency_stop", "registration_key": key})
            self.assertTrue(resp2.get("ok"), resp2)
            self.assertEqual(ad_flat.closes, 1)  # no new close
            self.assertEqual(ad_nf.close_ids[0], cid)


if __name__ == "__main__":
    unittest.main()
