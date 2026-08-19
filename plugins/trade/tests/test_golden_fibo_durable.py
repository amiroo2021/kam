"""Regression tests for the durable submission/ownership model.

Incident 2026-08-18: seven Step0 MARKET submissions were accepted by
the venue because the engine's binary submitted/not-submitted model
permitted each re-attempt after an ambiguous local failure.

These tests pin the durable phase machine:

  NOT_SUBMITTED → SUBMISSION_PREPARED → SUBMISSION_ATTEMPTED → CONFIRMED
  on exception after ATTEMPTED: -> NEEDS_RECOVERY, no resubmission.

And the STOP/START preflight that must block a fresh START on a lane
with unresolved ownership.
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


from plugins.trade.golden_fibo.config import GoldenFiboConfig
from plugins.trade.golden_fibo.state import (
    ROLE_ENTRY,
    ROLE_LADDER,
    ROLE_TP,
    STATUS_NEEDS_RECOVERY,
    STATUS_RUNNING,
    STATUS_STOPPING,
    SUBMISSION_ATTEMPTED,
    SUBMISSION_CONFIRMED,
    SUBMISSION_NEEDS_RECOVERY,
    SUBMISSION_NOT_SUBMITTED,
    SUBMISSION_PREPARED,
    GoldenFiboState,
)
from plugins.trade.golden_fibo.engine import GoldenFiboEngine
from plugins.trade.fibo_service import PersistentFiboService


# ---------------------------------------------------------------------------
# Fake adapter for engine-level tests
# ---------------------------------------------------------------------------
class _FakeAdapter:
    """Simulates venue behavior. Tracks submissions and position deltas."""

    def __init__(self) -> None:
        self.position = {"symbol": "SOL", "side": None, "size": "0", "sl": None, "tp": None}
        self.orders: dict = {}
        self.submit_log: list = []
        self.cancel_log: list = []
        self._next_id = 1000
        self.fail_next: bool = False        # raise exception on next submit
        self.fail_after_venue: bool = False  # accept venue then raise

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

    def _maybe_fail(self, *, accept_then_raise: bool):
        if self.fail_next and not accept_then_raise:
            raise RuntimeError("venue rejected")
        if accept_then_raise:
            # venue accepted, but local verification crashes
            raise RuntimeError("local verification NameError")

    def place_market(self, *, account, instrument, side, size, client_order_id: int) -> dict:
        if self.fail_next:
            raise RuntimeError("venue rejected")
        if self.fail_after_venue:
            # Simulate: venue accepted, then local verification crashes.
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
            # Update position to simulate the fill that would have
            # occurred on the real venue.
            prev = Decimal(str(self.position.get("size") or "0"))
            if self.position.get("side") is None:
                self.position["side"] = "long" if side == "buy" else "short"
                self.position["size"] = str(size)
            elif (self.position.get("side") == "long" and side == "buy") or \
                 (self.position.get("side") == "short" and side == "sell"):
                self.position["size"] = str(prev + Decimal(str(size)))
            self.submit_log.append(dict(rec, role="entry"))
            # Now raise the simulated verification failure (AFTER venue accepted).
            raise RuntimeError("name 'side' is not defined (simulated)")
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
        # Update position
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
        if self.fail_next:
            raise RuntimeError("venue rejected")
        if self.fail_after_venue:
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
            raise RuntimeError("verification after venue accept crashed (simulated)")
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


    def set_shared_tp(self, *, account, instrument, price, side=None, size=None, client_order_id=None) -> dict:
        """Stub: mirrors x_lighter_agent set_tp. Registers a TP order so the
        durable tests can count submissions and simulate verify crashes."""
        from decimal import Decimal as _D
        self._calls_tp = getattr(self, "_calls_tp", 0) + 1
        # Simulate verify-crash-after-venue-accept when fail_after_venue is set
        # and this is the TP placement the test is probing.
        if getattr(self, "fail_after_venue", False) and getattr(self, "_fail_tp_once", True):
            self._fail_tp_once = False
            oid = self._gen_id()
            qp = _D(str(price)).quantize(_D("0.001"))
            live_side = self.position.get("side")
            closing = "sell" if live_side == "long" else "buy"
            rec = {
                "exchange_order_id": oid,
                "client_order_id": None,
                "side": closing,
                "type": "take-profit",
                "size": str(self.position.get("size") or "0"),
                "price": str(qp),
                "status": "open",
                "taxonomy": "ACTIVE",
                "reduce_only": True,
                "role": "tp",
            }
            self.orders[oid] = rec
            self.submit_log.append(rec)
            raise RuntimeError("verification after venue accept crashed (simulated)")
        oid = self._gen_id()
        qp = _D(str(price)).quantize(_D("0.001"))
        live_side = self.position.get("side")
        closing = "sell" if live_side == "long" else "buy"
        rec = {
            "exchange_order_id": oid,
            "client_order_id": None,
            "side": closing,
            "type": "take-profit",
            "size": str(self.position.get("size") or "0"),
            "price": str(qp),
            "status": "open",
            "taxonomy": "ACTIVE",
            "reduce_only": True,
            "role": "tp",
        }
        self.orders[oid] = rec
        self.submit_log.append(rec)
        self.position["tp"] = str(qp)
        return {
            "verified": True,
            "submitted_price": str(qp),
            "exchange_order_id": oid,
            "current_side": live_side,
            "current_size": str(self.position.get("size") or "0"),
            "role": "tp",
        }

    def cancel_order(self, *, account, order_index: int) -> bool:
        rec = self.orders.get(int(order_index))
        if rec is None:
            return False
        rec["status"] = "canceled"
        rec["taxonomy"] = "CANCELED"
        self.cancel_log.append(int(order_index))
        if rec.get("reduce_only"):
            self.position["tp"] = None
        return True


def _cfg(direction="BUY") -> GoldenFiboConfig:
    return GoldenFiboConfig(
        exchange="lighter",
        account="amiroo",
        instrument="SOL",
        direction=direction,
        percentage=Decimal("0.01"),
        step0_volume=Decimal("0.100"),
    )


def _engine(cfg, adapter, next_id=iter(range(1, 1_000_000))):
    state = GoldenFiboState(
        client_id_version=1,
        registration_key=cfg.registration_key,
        exchange=cfg.exchange,
        account=cfg.account,
        instrument=cfg.instrument,
        direction=cfg.direction,
        percentage=cfg.percentage,
        step0_volume=cfg.step0_volume,
    )
    counter = {"n": 100000}
    def next_id_fn():
        counter["n"] += 1
        return counter["n"]
    return GoldenFiboEngine(cfg, state, adapter, next_id_fn)


# ---------------------------------------------------------------------------
# Engine-level durable submission tests
# ---------------------------------------------------------------------------
class TestDurableSubmissionMachine(unittest.TestCase):
    """Once SUBMISSION_ATTEMPTED is persisted, exceptions must not resubmit."""

    def test_step0_attempted_then_exception_no_resubmit(self):
        """venue accepts, local verification crashes -> one submission only."""
        cfg = _cfg("BUY")
        adapter = _FakeAdapter()
        adapter.fail_after_venue = True
        engine = _engine(cfg, adapter)
        result = engine.tick()

        # Exactly one submission happened (venue accepted, then local crash)
        self.assertEqual(len(adapter.submit_log), 1)

        # State is needs_recovery, and submission_phase is needs_recovery
        self.assertEqual(result.state.status, STATUS_NEEDS_RECOVERY)
        self.assertEqual(result.state.submission_phase, SUBMISSION_NEEDS_RECOVERY)

        # The durable record retains the deterministic client id + step + role
        self.assertEqual(result.state.submission_role, ROLE_ENTRY)
        self.assertEqual(result.state.submission_step, 0)

        # Subsequent tick must freeze without resubmitting.
        result2 = engine.tick()
        self.assertEqual(len(adapter.submit_log), 1)

    def test_step0_normal_success_clears_attempted_on_confirm(self):
        """On CONFIRMED Step0, the durable record is cleared for next order."""
        cfg = _cfg("BUY")
        adapter = _FakeAdapter()
        engine = _engine(cfg, adapter)
        result = engine.tick()
        self.assertEqual(result.state.submission_phase, SUBMISSION_CONFIRMED)
        self.assertEqual(result.state.status, STATUS_RUNNING)
        self.assertEqual(len(adapter.submit_log), 1)

    def test_step0_second_tick_does_not_resubmit_after_attempted(self):
        """Any re-entry into _start_fresh_cycle after ATTEMPTED freezes."""
        cfg = _cfg("BUY")
        adapter = _FakeAdapter()
        adapter.fail_after_venue = True
        engine = _engine(cfg, adapter)
        engine.tick()
        # Simulate what a poll loop would do.
        for _ in range(5):
            result = engine.tick()
            self.assertEqual(result.state.status, STATUS_NEEDS_RECOVERY)
        self.assertEqual(len(adapter.submit_log), 1)

    def test_tp_attempted_then_exception_no_resubmit(self):
        """TP accepted by venue, verification crashes -> one TP only."""
        cfg = _cfg("BUY")
        adapter = _FakeAdapter()
        engine = _engine(cfg, adapter)
        # Manually set up a filled-step-0 state so _rotate_tp runs.
        engine.state.highest_filled_step = 0
        engine.state.fill_prices[0] = Decimal("100.0")
        engine.state.submission_phase = SUBMISSION_NOT_SUBMITTED
        engine.state.submission_client_id = None
        engine.state.submission_step = None
        engine.state.submission_role = None

        adapter.fail_after_venue = True
        result = engine._rotate_tp(Decimal("100.0"))
        self.assertIsNotNone(result)
        self.assertEqual(len(adapter.submit_log), 1)
        self.assertEqual(result.state.submission_phase, SUBMISSION_NEEDS_RECOVERY)
        self.assertEqual(result.state.submission_role, ROLE_TP)

    def test_ladder_attempted_then_exception_no_resubmit(self):
        """Ladder accepted by venue, verification crashes -> one ladder only."""
        cfg = _cfg("BUY")
        adapter = _FakeAdapter()
        engine = _engine(cfg, adapter)
        # Set up so next_step=1 and P0 exists.
        engine.state.highest_filled_step = 0
        engine.state.fill_prices[0] = Decimal("100.0")
        engine.state.next_step = 1
        engine.state.submission_phase = SUBMISSION_NOT_SUBMITTED
        engine.state.submission_client_id = None
        engine.state.submission_step = None
        engine.state.submission_role = None

        adapter.fail_after_venue = True
        result = engine._place_next_ladder()
        self.assertIsNotNone(result)
        self.assertEqual(len(adapter.submit_log), 1)
        self.assertEqual(result.state.submission_phase, SUBMISSION_NEEDS_RECOVERY)
        self.assertEqual(result.state.submission_role, ROLE_LADDER)
        self.assertEqual(result.state.submission_step, 1)


# ---------------------------------------------------------------------------
# Service-level STOP/START preflight tests
# ---------------------------------------------------------------------------
class _ServiceStubAdapter(_FakeAdapter):
    pass


def _step0_submissions(log):
    """Count only the Step0 MARKET submissions (role='entry'), not TP/ladder."""
    return [e for e in log if e.get("role") == "entry"]




def _make_service(tmpdir: str, with_stub=True) -> PersistentFiboService:
    svc = PersistentFiboService(
        state_path=Path(tmpdir) / "service_state.json",
        ledger_path=Path(tmpdir) / "service_ledger.jsonl",
        event_log_path=Path(tmpdir) / "service-events.log",
        start_thread=False,  # disable background polling in tests
    )
    if with_stub:
        # The service's adapter_for uses LighterGoldenFiboAdapter by default;
        # for these tests we inject a stub adapter to avoid the venue.
        svc._adapters["lighter/amiroo/SOL/BUY"] = _ServiceStubAdapter()
    return svc


class TestStopStartPreflight(unittest.TestCase):
    """START must not blindly resubmit when lane has unresolved ownership."""

    def _start_inputs(self, direction="BUY", instrument="SOL"):
        return {
            "op": "start",
            "exchange": "lighter",
            "account": "amiroo",
            "instrument": instrument,
            "direction": direction,
            "percentage": "0.01",
            "step0_volume": "0.100",
        }

    def test_stop_preserves_tombstone_when_submission_attempted(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            # Manually inject a registration that is mid-submission.
            key = "lighter/amiroo/SOL/BUY"
            state = GoldenFiboState(
        client_id_version=1,
                registration_key=key,
                exchange="lighter",
                account="amiroo",
                instrument="SOL",
                direction="BUY",
                percentage=Decimal("0.01"),
                step0_volume=Decimal("0.100"),
                expected_cumulative_size=Decimal("0"),
                submission_phase=SUBMISSION_ATTEMPTED,
                submission_step=0,
                submission_role=ROLE_ENTRY,
                submission_client_id=100001,
                status=STATUS_RUNNING,
            )
            svc._states[key] = state
            # STOP should produce a tombstone, not erase the record.
            resp = svc.execute_command({"op": "stop", "registration_key": key})
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["status"], "stopped_with_tombstone")
            self.assertTrue(resp.get("tombstone"))
            # The registration still exists in _states with STOPPING status.
            self.assertIn(key, svc._states)
            self.assertEqual(svc._states[key].status, STATUS_STOPPING)

    def test_stop_erases_only_when_truly_flat(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            key = "lighter/amiroo/SOL/BUY"
            state = GoldenFiboState(
        client_id_version=1,
                registration_key=key,
                exchange="lighter",
                account="amiroo",
                instrument="SOL",
                direction="BUY",
                percentage=Decimal("0.01"),
                step0_volume=Decimal("0.100"),
                expected_cumulative_size=Decimal("0"),
                submission_phase=SUBMISSION_CONFIRMED,
                status=STATUS_RUNNING,
            )
            svc._states[key] = state
            resp = svc.execute_command({"op": "stop", "registration_key": key})
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["status"], "stopped")
            self.assertNotIn(key, svc._states)

    def test_start_rejected_after_tombstone(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            key = "lighter/amiroo/SOL/BUY"
            # Inject tombstone.
            state = GoldenFiboState(
        client_id_version=1,
                registration_key=key,
                exchange="lighter",
                account="amiroo",
                instrument="SOL",
                direction="BUY",
                percentage=Decimal("0.01"),
                step0_volume=Decimal("0.100"),
                submission_phase=SUBMISSION_ATTEMPTED,
                submission_step=0,
                submission_role=ROLE_ENTRY,
                submission_client_id=100001,
                status=STATUS_STOPPING,
            )
            svc._states[key] = state
            resp = svc.execute_command(self._start_inputs())
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error"], "LANE_TOMBSTONE")

    def test_start_rejected_when_live_position_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            # Stub adapter returns a live position.
            sock = _ServiceStubAdapter()
            sock.position = {"symbol": "SOL", "side": "long", "size": "0.100", "sl": None, "tp": None}
            svc._adapters["lighter/amiroo/SOL/BUY"] = sock
            resp = svc.execute_command(self._start_inputs())
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error"], "LANE_NOT_FLAT")

    def test_start_rejected_when_prior_needs_recovery_with_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            key = "lighter/amiroo/SOL/BUY"
            state = GoldenFiboState(
        client_id_version=1,
                registration_key=key,
                exchange="lighter",
                account="amiroo",
                instrument="SOL",
                direction="BUY",
                percentage=Decimal("0.01"),
                step0_volume=Decimal("0.100"),
                expected_cumulative_size=Decimal("0.100"),
                pending_order_exchange_id=123,
                status=STATUS_NEEDS_RECOVERY,
            )
            svc._states[key] = state
            resp = svc.execute_command(self._start_inputs())
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["error"], "LANE_NOT_FLAT")


# ---------------------------------------------------------------------------
# Incident regression: STOP+START repeated must not resubmit
# ---------------------------------------------------------------------------
class TestIncidentRegression(unittest.TestCase):
    """Scenario from 2026-08-18: STOP+START cycles must not resubmit Step0."""

    def test_stop_start_repeatedly_no_resubmit(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            key = "lighter/amiroo/SOL/BUY"
            adapter = _ServiceStubAdapter()
            adapter.fail_after_venue = False  # first attempt succeeds
            svc._adapters[key] = adapter

            # First START: creates the registration.
            resp1 = svc.execute_command({
                "op": "start",
                "exchange": "lighter",
                "account": "amiroo",
                "instrument": "SOL",
                "direction": "BUY",
                "percentage": "0.01",
                "step0_volume": "0.100",
            })
            self.assertTrue(resp1["ok"])

            # Drive a tick so the engine actually submits Step0 + TP + Step1.
            svc._drive_one(key)
            self.assertEqual(len(_step0_submissions(adapter.submit_log)), 1)

            # Fake a submitted-then-local-crash Step0: mark attempted.
            svc._states[key].submission_phase = SUBMISSION_ATTEMPTED
            svc._states[key].submission_role = ROLE_ENTRY
            svc._states[key].submission_step = 0
            svc._states[key].submission_client_id = 100001
            svc._states[key].status = STATUS_RUNNING
            # STOP -> tombstone (unresolved ownership).
            resp_stop = svc.execute_command({"op": "stop", "registration_key": key})
            self.assertTrue(resp_stop.get("tombstone"))

            # Any subsequent START must be rejected (tombstone).
            for _ in range(5):
                resp = svc.execute_command({
                    "op": "start",
                    "exchange": "lighter",
                    "account": "amiroo",
                    "instrument": "SOL",
                    "direction": "BUY",
                    "percentage": "0.01",
                    "step0_volume": "0.100",
                })
                self.assertFalse(resp["ok"])
                self.assertIn(resp["error"], ("LANE_TOMBSTONE", "LANE_NOT_FLAT"))

            # Exactly ONE Step0 submission happened (the first one).
            self.assertEqual(len(_step0_submissions(adapter.submit_log)), 1)

    def test_five_stop_start_cycles_total_submissions_one(self):
        """Exact incident scenario: 5 STOP+START -> total submissions = 1."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            key = "lighter/amiroo/SOL/BUY"
            adapter = _ServiceStubAdapter()
            adapter.fail_after_venue = False
            svc._adapters[key] = adapter

            # First START + one engine tick: one Step0 submission.
            resp1 = svc.execute_command({
                "op": "start",
                "exchange": "lighter",
                "account": "amiroo",
                "instrument": "SOL",
                "direction": "BUY",
                "percentage": "0.01",
                "step0_volume": "0.100",
            })
            self.assertTrue(resp1["ok"])
            svc._drive_one(key)
            self.assertEqual(len(_step0_submissions(adapter.submit_log)), 1)

            # Mark attempt unresolved.
            svc._states[key].submission_phase = SUBMISSION_ATTEMPTED
            svc._states[key].submission_role = ROLE_ENTRY
            svc._states[key].submission_step = 0
            svc._states[key].status = STATUS_RUNNING

            for _ in range(5):
                # STOP -> tombstone
                svc.execute_command({"op": "stop", "registration_key": key})
                # START -> rejected (tombstone), so no new submission.
                resp = svc.execute_command({
                    "op": "start",
                    "exchange": "lighter",
                    "account": "amiroo",
                    "instrument": "SOL",
                    "direction": "BUY",
                    "percentage": "0.01",
                    "step0_volume": "0.100",
                })
                self.assertFalse(resp["ok"])

            self.assertEqual(len(_step0_submissions(adapter.submit_log)), 1)


# ---------------------------------------------------------------------------
# Restart-after-uncertain-Step0 regression
# ---------------------------------------------------------------------------
class TestRestartSafety(unittest.TestCase):
    """After a service restart, an unresolved ATTEMPTED record must not resubmit."""

    def test_restart_loads_attempted_and_blocks_step0(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            key = "lighter/amiroo/SOL/BUY"
            # Persist an attempted record.
            state = GoldenFiboState(
        client_id_version=1,
                registration_key=key,
                exchange="lighter",
                account="amiroo",
                instrument="SOL",
                direction="BUY",
                percentage=Decimal("0.01"),
                step0_volume=Decimal("0.100"),
                submission_phase=SUBMISSION_ATTEMPTED,
                submission_step=0,
                submission_role=ROLE_ENTRY,
                submission_client_id=100001,
                status=STATUS_RUNNING,
            )
            svc._states[key] = state
            svc._save_state()

            # Recreate service from disk (simulated restart).
            svc2 = PersistentFiboService(
                state_path=Path(tmp) / "service_state.json",
                ledger_path=Path(tmp) / "service_ledger.jsonl",
                event_log_path=Path(tmp) / "service-events.log",
                start_thread=False,
            )
            # The engine's tick for this registration must freeze, not resubmit.
            svc2._adapters[key] = _ServiceStubAdapter()
            svc2._tick_once()
            self.assertEqual(len(svc2._adapters[key].submit_log), 0)
            # State should be frozen NEEDS_RECOVERY.
            self.assertEqual(svc2._states[key].status, STATUS_NEEDS_RECOVERY)


if __name__ == "__main__":
    unittest.main()
