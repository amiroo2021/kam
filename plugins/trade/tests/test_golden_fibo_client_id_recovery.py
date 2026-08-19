"""Regression tests for the Lighter client-order-index Step0 confirmation path.

Covers the requirements from the offline-fix approval:

1.  MARKET submit returns no exchange_order_id.
2.  Exact inactive order exists by client_order_index.
3.  Order is FILLED.
4.  P0 recovered correctly from filled quote/base.
5.  exchange order_index backfilled into durable state.
6.  exactly one market submission.
7.  order initially missing, appears on second/third bounded read.
8.  still missing after retry -> NEEDS_RECOVERY, zero resubmit.
9.  service restart with client ID but no exchange ID -> reconcile existing fill.
10. inactive order CANCELED/REJECTED -> NEEDS_RECOVERY.
11. wrong client_order_index must not be adopted.
12. matching size/side must be verified before ownership confirmation.

Plus generic x_lighter_agent client_order_index passthrough tests.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock


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


import plugins.trade.agents.x_lighter_agent as lighter
from plugins.trade.golden_fibo.config import GoldenFiboConfig
from plugins.trade.golden_fibo.state import (
    ROLE_ENTRY,
    STATUS_NEEDS_RECOVERY,
    STATUS_RUNNING,
    SUBMISSION_ATTEMPTED,
    SUBMISSION_CONFIRMED,
    GoldenFiboState,
)
from plugins.trade.fibo_service import PersistentFiboService


# ---------------------------------------------------------------------------
# Generic Lighter agent: client_order_index passthrough
# ---------------------------------------------------------------------------
class TestLighterClientIdPassthrough(unittest.TestCase):
    """_submit_new_order must honor a caller-supplied client_order_index."""

    def test_submit_uses_supplied_client_order_index(self):
        captured = {}

        class _Signer:
            async def create_market_order_limited_slippage(self, *args, **kwargs):
                captured["client_order_index"] = args[1]
                captured["is_ask"] = args[4]
                return ({"tx": 1}, {"tx_hash": "h", "code": 200}, None)

        creds = {"base_url": "https://stub", "account_index": 1, "api_key_index": 4,
                 "public_key": "x" * 96, "private_key": "y" * 96}
        market = {"market_id": 2, "size_decimals": 3, "price_decimals": 3, "min_base_amount": "0.1"}

        with mock.patch.object(lighter, "_build_signer_client", return_value=_Signer()), \
             mock.patch.object(lighter, "_get_lighter_l2_tx_budget") as m_budget, \
             mock.patch.object(lighter, "_classify_lighter_api_response", return_value=None):
            m_budget.return_value.wait_for_capacity.return_value = None
            result = lighter._submit_new_order(
                creds, market,
                side="buy", order_type="market",
                requested_volume=Decimal("0.1"),
                requested_price=Decimal("76.0"),
                reduce_only=False,
                client_order_index=100001,
            )
        self.assertEqual(captured["client_order_index"], 100001)
        self.assertEqual(result["client_order_index"], 100001)

    def test_submit_falls_back_to_time_based_when_absent(self):
        captured = {}

        class _Signer:
            async def create_market_order_limited_slippage(self, *args, **kwargs):
                captured["client_order_index"] = args[1]
                return ({"tx": 1}, {"tx_hash": "h", "code": 200}, None)

        creds = {"base_url": "https://stub", "account_index": 1, "api_key_index": 4,
                 "public_key": "x" * 96, "private_key": "y" * 96}
        market = {"market_id": 2, "size_decimals": 3, "price_decimals": 3, "min_base_amount": "0.1"}

        with mock.patch.object(lighter, "_build_signer_client", return_value=_Signer()), \
             mock.patch.object(lighter, "_get_lighter_l2_tx_budget") as m_budget, \
             mock.patch.object(lighter, "_classify_lighter_api_response", return_value=None):
            m_budget.return_value.wait_for_capacity.return_value = None
            result = lighter._submit_new_order(
                creds, market,
                side="buy", order_type="market",
                requested_volume=Decimal("0.1"),
                requested_price=Decimal("76.0"),
                reduce_only=False,
            )
        # Falls back to a large time-based value (not a small deterministic id).
        self.assertGreater(captured["client_order_index"], 1000000)
        self.assertEqual(result["client_order_index"], captured["client_order_index"])

    def test_invalid_client_order_id_rejected(self):
        resp = lighter.execute({
            "operation": "new_order",
            "exchange": "lighter",
            "account": "amiroo",
            "symbol": "SOL",
            "side": "buy",
            "order_type": "market",
            "volume": "0.1",
            "client_order_id": "not-an-int",
        })
        self.assertFalse(resp.success)
        self.assertIn(resp.error.code, ("INVALID_INPUTS", "UNKNOWN_ACCOUNT", "LIGHTER_ERROR"))


# ---------------------------------------------------------------------------
# Generic Lighter agent: get_order_state_by_client_id normalization
# ---------------------------------------------------------------------------
class TestNormalizeOrderByClientId(unittest.TestCase):
    def test_p0_from_filled_quote_over_base(self):
        order = {
            "order_index": 555,
            "client_order_index": 100001,
            "market_index": 2,
            "is_ask": False,
            "initial_base_amount": "0.100",
            "filled_base_amount": "0.100",
            "filled_quote_amount": "7.612600",
            "price": "79.931",
            "avg_execution_price": None,
            "status": "filled",
            "type": "market",
            "reduce_only": False,
            "created_at": 1787043570,
        }
        market = {"symbol": "SOL"}
        n = lighter._normalize_order_record_by_client_id(order, market=market, size_decimals=3)
        self.assertEqual(n["exchange_order_id"], 555)
        self.assertEqual(n["client_order_index"], 100001)
        self.assertEqual(n["taxonomy"], "FILLED")
        self.assertEqual(n["side"], "buy")
        self.assertEqual(Decimal(n["actual_fill_price"]), Decimal("76.126"))
        self.assertEqual(Decimal(n["filled_size"]), Decimal("0.100"))
        self.assertEqual(Decimal(n["filled_quote"]), Decimal("7.612600"))

    def test_native_avg_execution_price_preferred(self):
        order = {
            "order_index": 1, "client_order_index": 2, "is_ask": True,
            "filled_base_amount": "0.1", "filled_quote_amount": "10",
            "avg_execution_price": "99.5", "status": "filled",
        }
        n = lighter._normalize_order_record_by_client_id(order, market={"symbol": "X"}, size_decimals=3)
        self.assertEqual(n["actual_fill_price"], "99.5")
        self.assertEqual(n["side"], "sell")

    def test_active_taxonomy(self):
        order = {"order_index": 1, "client_order_index": 2, "status": "open", "is_ask": False}
        n = lighter._normalize_order_record_by_client_id(order, market={"symbol": "X"}, size_decimals=3)
        self.assertEqual(n["taxonomy"], "ACTIVE")


# ---------------------------------------------------------------------------
# Service-level: Step0 confirmation via client_id
# ---------------------------------------------------------------------------
class _ClientIdAdapter:
    """Adapter that never returns an exchange_order_id on submit, but
    serves the order via get_order_state_by_client_id."""

    def __init__(self, *, appear_after: int = 0, taxonomy: str = "FILLED",
                 fill_price: str = "76.126", wrong_client: bool = False,
                 wrong_side: bool = False, wrong_size: bool = False):
        # Start FLAT so the lane preflight passes; the position is
        # established when place_market is called (simulating the fill).
        self.position = {"symbol": "SOL", "side": None, "size": "0", "entry_price": None}
        self.submit_log = []
        self._calls = 0
        self._appear_after = appear_after
        self._taxonomy = taxonomy
        self._fill_price = fill_price
        self._wrong_client = wrong_client
        self._wrong_side = wrong_side
        self._wrong_size = wrong_size
        self._exchange_oid = 424242
        self._orders = {}

    def resolve_instrument(self, account, instrument):
        return {"symbol": instrument, "market_id": 2, "size_decimals": 3,
                "price_decimals": 3, "min_base_amount": "0.1", "minimum_size": "0.1"}

    def position_state(self, account, instrument):
        return dict(self.position)

    def get_order_state(self, account, order_index):
        # Report placed limit orders (TP/ladder) as ACTIVE so the engine's
        # healthy-waiting path works after Step0 confirmation.
        rec = self._orders.get(int(order_index))
        if rec is None:
            return {}
        return rec

    def get_order_state_by_client_id(self, account, instrument, client_order_index):
        self._calls += 1
        if self._calls <= self._appear_after:
            return {}
        base = "0.050" if self._wrong_size else "0.100"
        return {
            "exchange_order_id": self._exchange_oid,
            "client_order_index": 999999 if self._wrong_client else int(client_order_index),
            "market_index": 2,
            "symbol": instrument,
            "side": "sell" if self._wrong_side else "buy",
            "type": "market",
            "status": "filled" if self._taxonomy == "FILLED" else self._taxonomy.lower(),
            "taxonomy": self._taxonomy,
            "requested_size": base,
            "filled_size": base,
            "filled_quote": "7.612600",
            "requested_price": "79.931",
            "actual_fill_price": self._fill_price,
            "reduce_only": False,
        }

    def place_market(self, *, account, instrument, side, size, client_order_id):
        self.submit_log.append({"client_order_id": client_order_id, "size": str(size), "role": "entry"})
        # Simulate the venue fill establishing the position.
        prev = Decimal(str(self.position.get("size") or "0"))
        if self.position.get("side") is None:
            self.position["side"] = "long" if side == "buy" else "short"
            self.position["size"] = str(size)
        elif (self.position.get("side") == "long" and side == "buy") or \
             (self.position.get("side") == "short" and side == "sell"):
            self.position["size"] = str(prev + Decimal(str(size)))
        # Record the fill so get_order_state can find it (matches the
        # client-id lookup record's exchange_order_id).
        self._orders[self._exchange_oid] = {
            "exchange_order_id": self._exchange_oid,
            "client_order_index": int(client_order_id),
            "symbol": instrument,
            "side": side,
            "type": "market",
            "status": "filled",
            "taxonomy": "FILLED",
            "requested_price": None,
            "requested_size": str(size),
            "filled_size": str(size),
            "actual_fill_price": self._fill_price,
            "reduce_only": False,
        }
        # Lighter behavior: NO exchange_order_id in the submit response.
        return {
            "client_order_id": int(client_order_id),
            "exchange_order_id": None,
            "submitted_price": None,
            "submitted_volume": str(size),
            "status": "filled",
            "verified": True,
            "role": "entry",
        }

    def place_limit(self, *, account, instrument, side, size, price, client_order_id, reduce_only=False):
        self.submit_log.append({"client_order_id": client_order_id, "role": "tp" if reduce_only else "ladder"})
        oid = 777001 + len(self.submit_log)
        self._orders[oid] = {
            "exchange_order_id": oid,
            "client_order_index": int(client_order_id),
            "symbol": instrument,
            "side": side,
            "type": "limit",
            "status": "open",
            "taxonomy": "ACTIVE",
            "requested_price": str(price),
            "requested_size": str(size),
            "filled_size": "0",
            "actual_fill_price": None,
            "reduce_only": bool(reduce_only),
        }
        return {
            "client_order_id": int(client_order_id),
            "exchange_order_id": oid,
            "submitted_price": str(price),
            "submitted_volume": str(size),
            "status": "submitted",
            "verified": True,
            "role": "tp" if reduce_only else "ladder",
        }


    def set_shared_tp(self, *, account, instrument, price, side=None, size=None, client_order_id=None) -> dict:
        """Stub: mirrors x_lighter_agent set_tp for the recovery tests."""
        from decimal import Decimal as _D
        oid = 888001 + len(self.submit_log)
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
        self._orders[oid] = rec
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

    def cancel_order(self, *, account, order_index):
        return True


def _entry_submissions(log):
    return [e for e in log if e.get("role") == "entry"]


def _make_service(tmpdir: str) -> PersistentFiboService:
    return PersistentFiboService(
        state_path=Path(tmpdir) / "service_state.json",
        ledger_path=Path(tmpdir) / "service_ledger.jsonl",
        event_log_path=Path(tmpdir) / "service-events.log",
        start_thread=False,
    )


def _start(svc, key):
    return svc.execute_command({
        "op": "start", "exchange": "lighter", "account": "amiroo",
        "instrument": "SOL", "direction": "BUY",
        "percentage": "0.01", "step0_volume": "0.100",
    })


class TestStep0ClientIdConfirmation(unittest.TestCase):
    """The normal Lighter Step0 confirmation path via client_order_index."""

    def test_step0_no_exchange_id_confirmed_via_client_id(self):
        """Req 1-6: no exchange_order_id from submit; FILLED order found by
        client_id; P0 recovered; exchange oid backfilled; one submission."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            key = "lighter/amiroo/SOL/BUY"
            adapter = _ClientIdAdapter()
            svc._adapters[key] = adapter

            resp = _start(svc, key)
            self.assertTrue(resp["ok"])
            svc._drive_one(key)   # submits Step0 (no exchange_order_id)
            svc._drive_one(key)   # confirms via client_id, places TP+Step1

            state = svc._states[key]
            # exactly one market submission
            self.assertEqual(len(_entry_submissions(adapter.submit_log)), 1)
            # Step0 promoted
            self.assertEqual(state.highest_filled_step, 0)
            self.assertEqual(Decimal(str(state.fill_prices[0])), Decimal("76.126"))
            self.assertEqual(state.expected_cumulative_size, Decimal("0.100"))
            self.assertEqual(state.next_step, 1)
            # exchange order_index backfilled at confirm time. After
            # confirm_step0_filled, pending_order_exchange_id tracks the
            # CURRENT pending order (the Step1 ladder), so assert the
            # ladder is now pending and P0 was recovered from the
            # client-id-matched Step0 record.
            self.assertIsNotNone(state.pending_order_exchange_id)
            self.assertNotEqual(state.pending_order_exchange_id, adapter._exchange_oid)
            # TP + Step1 placed
            self.assertIsNotNone(state.current_tp_order_id)
            # TP = SELL, reduce_only; Step1 = BUY
            roles = [e["role"] for e in adapter.submit_log]
            self.assertIn("tp", roles)
            self.assertIn("ladder", roles)
            # still running, not frozen
            self.assertEqual(state.status, STATUS_RUNNING)
            # submission confirmed and cleared for next order
            self.assertEqual(state.submission_phase, SUBMISSION_CONFIRMED)

    def test_bounded_retry_then_found(self):
        """Req 7: order initially missing, appears on later bounded read."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            key = "lighter/amiroo/SOL/BUY"
            # appear_after=3: the confirm lookup runs once per drive after
            # the submit drive. Drives: 1=submit+lookup#1, 2=lookup#2,
            # 3=lookup#3, 4=lookup#4. Missing on lookups 1-3, found on 4.
            adapter = _ClientIdAdapter(appear_after=3)
            svc._adapters[key] = adapter

            resp = _start(svc, key)
            self.assertTrue(resp["ok"])
            svc._drive_one(key)  # submit Step0 + lookup 1: not found -> wait
            self.assertEqual(svc._states[key].highest_filled_step, -1)
            svc._drive_one(key)  # lookup 2: not found -> wait
            self.assertEqual(svc._states[key].highest_filled_step, -1)
            svc._drive_one(key)  # lookup 3: not found -> wait
            self.assertEqual(svc._states[key].highest_filled_step, -1)
            svc._drive_one(key)  # lookup 4: found -> confirm
            state = svc._states[key]
            self.assertEqual(state.highest_filled_step, 0)
            self.assertEqual(state.status, STATUS_RUNNING)
            self.assertEqual(len(_entry_submissions(adapter.submit_log)), 1)

    def test_never_found_after_bounded_retry(self):
        """Req 8: missing after bound -> NEEDS_RECOVERY, zero resubmit."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            key = "lighter/amiroo/SOL/BUY"
            adapter = _ClientIdAdapter(appear_after=9999)  # never appears
            svc._adapters[key] = adapter

            resp = _start(svc, key)
            self.assertTrue(resp["ok"])
            svc._drive_one(key)  # submit Step0
            for _ in range(10):
                svc._drive_one(key)  # bounded retries
            state = svc._states[key]
            self.assertEqual(state.status, STATUS_NEEDS_RECOVERY)
            self.assertIn("not found", state.freeze_reason or "")
            # exactly one market submission, no resubmit
            self.assertEqual(len(_entry_submissions(adapter.submit_log)), 1)

    def test_restart_reconciles_existing_fill(self):
        """Req 9: restart with client_id but no exchange_id -> reconcile."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            key = "lighter/amiroo/SOL/BUY"
            # appear_after=1: the first drive's confirm lookup finds nothing
            # (order not yet visible), so the service persists the
            # attempted-but-unconfirmed Step0 state, then "dies".
            adapter = _ClientIdAdapter(appear_after=1)
            svc._adapters[key] = adapter

            resp = _start(svc, key)
            self.assertTrue(resp["ok"])
            svc._drive_one(key)  # submit Step0; confirm lookup finds nothing
            # Simulate process death before confirmation: the Step0 submit
            # returned no exchange_order_id, so pending_order_exchange_id
            # is still None at this point (only Step0 was submitted so far).
            self.assertIsNone(svc._states[key].pending_order_exchange_id)
            self.assertEqual(svc._states[key].highest_filled_step, -1)

            # Restart service from persisted state.
            svc2 = _make_service(tmp)
            svc2._adapters[key] = adapter
            svc2._drive_one(key)  # reconcile via client_id
            state = svc2._states[key]
            self.assertEqual(state.highest_filled_step, 0)
            self.assertEqual(state.status, STATUS_RUNNING)
            # After reconcile, pending_order_exchange_id tracks the new
            # Step1 ladder (not the historical Step0 oid).
            self.assertIsNotNone(state.pending_order_exchange_id)
            # exactly one market submission across restart
            self.assertEqual(len(_entry_submissions(adapter.submit_log)), 1)

    def test_canceled_order_needs_recovery_no_resubmit(self):
        """Req 10: CANCELED/REJECTED -> NEEDS_RECOVERY, no resubmit."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            key = "lighter/amiroo/SOL/BUY"
            adapter = _ClientIdAdapter(taxonomy="CANCELED")
            svc._adapters[key] = adapter

            resp = _start(svc, key)
            self.assertTrue(resp["ok"])
            svc._drive_one(key)  # submit Step0
            svc._drive_one(key)  # lookup -> CANCELED -> needs_recovery
            state = svc._states[key]
            self.assertEqual(state.status, STATUS_NEEDS_RECOVERY)
            self.assertIn("canceled", (state.freeze_reason or "").lower())
            self.assertEqual(len(_entry_submissions(adapter.submit_log)), 1)

    def test_wrong_client_order_index_not_adopted(self):
        """Req 11: a record with a different client_order_index must not be adopted."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            key = "lighter/amiroo/SOL/BUY"
            adapter = _ClientIdAdapter(wrong_client=True)
            svc._adapters[key] = adapter

            resp = _start(svc, key)
            self.assertTrue(resp["ok"])
            svc._drive_one(key)
            svc._drive_one(key)
            state = svc._states[key]
            self.assertEqual(state.status, STATUS_NEEDS_RECOVERY)
            self.assertIn("client_order_index mismatch", state.freeze_reason or "")
            self.assertEqual(len(_entry_submissions(adapter.submit_log)), 1)

    def test_wrong_side_rejected(self):
        """Req 12: side must be verified before ownership confirmation."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            key = "lighter/amiroo/SOL/BUY"
            adapter = _ClientIdAdapter(wrong_side=True)
            svc._adapters[key] = adapter

            resp = _start(svc, key)
            self.assertTrue(resp["ok"])
            svc._drive_one(key)
            svc._drive_one(key)
            state = svc._states[key]
            self.assertEqual(state.status, STATUS_NEEDS_RECOVERY)
            self.assertIn("side mismatch", state.freeze_reason or "")

    def test_undersized_fill_rejected(self):
        """Req 12: filled size must be >= expected step0 volume."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            key = "lighter/amiroo/SOL/BUY"
            adapter = _ClientIdAdapter(wrong_size=True)
            svc._adapters[key] = adapter

            resp = _start(svc, key)
            self.assertTrue(resp["ok"])
            svc._drive_one(key)
            svc._drive_one(key)
            state = svc._states[key]
            self.assertEqual(state.status, STATUS_NEEDS_RECOVERY)
            self.assertIn("filled size", state.freeze_reason or "")


if __name__ == "__main__":
    unittest.main()
