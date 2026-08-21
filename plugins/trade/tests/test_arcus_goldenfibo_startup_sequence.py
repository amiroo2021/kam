"""Arcus GoldenFibo startup sequence regression (offline).

Happy path: exactly one Step0 MARKET, fill confirm, shared TP, one Step1
LIMIT, then WAIT. Failure path: submitted Step0 + unknown position must
not place a second market or Step1.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional

from plugins.trade.fibo_service import PersistentFiboService, STATUS_NEEDS_RECOVERY
from plugins.trade.golden_fibo.state import (
    ROLE_ENTRY,
    ROLE_LADDER,
    STATUS_RUNNING,
    SUBMISSION_CONFIRMED,
    GoldenFiboState,
)


class _RecordingArcusAdapter:
    name = "golden_fibo_arcus"

    def __init__(self) -> None:
        self.ops: List[str] = []
        self.place_market_calls: List[Dict[str, Any]] = []
        self.place_limit_calls: List[Dict[str, Any]] = []
        self.set_tp_calls: List[Dict[str, Any]] = []
        self._oid = 8000
        self.position: Dict[str, Any] = {"symbol": "SOL", "side": None, "size": "0"}
        self.orders: Dict[int, Dict[str, Any]] = {}
        self.fill_after_market = True
        self.ambiguous_after_market = False

    def _next_oid(self) -> int:
        self._oid += 1
        return self._oid

    def resolve_instrument(self, account, instrument):
        self.ops.append("resolve_instrument")
        return {
            "symbol": "SOL-USD",
            "requested_symbol": instrument,
            "tick_size": "0.001",
            "step_size": "0.000001",
            "min_base_amount": "0.000001",
            "min_quote_amount": "0",
            "size_decimals": 6,
            "price_decimals": 3,
        }

    def get_venue_constraints(self, account, instrument):
        self.ops.append("get_venue_constraints")
        return {
            "min_base_amount": "0.000001",
            "min_quote_amount": "0",
            "size_decimals": 6,
            "price_decimals": 3,
            "tick_size": "0.001",
            "step_size": "0.000001",
        }

    def market_price(self, account, instrument):
        self.ops.append("market_price")
        return {"mark_price": "80", "last_external_price": "80"}

    def position_state(self, account, instrument):
        self.ops.append("position_state")
        return dict(self.position)

    def place_market(self, **kw):
        self.ops.append("place_market")
        self.place_market_calls.append(dict(kw))
        oid = self._next_oid()
        cid = int(kw["client_order_id"])
        size = str(kw["size"])
        rec = {
            "exchange_order_id": oid,
            "client_order_index": cid,
            "client_order_id": cid,
            "status": "filled",
            "taxonomy": "FILLED",
            "side": str(kw.get("side") or "buy").lower(),
            "filled_size": size,
            "requested_size": size,
            "actual_fill_price": "80",
            "symbol": "SOL-USD",
        }
        if self.ambiguous_after_market:
            self.position = {"symbol": "SOL", "side": None, "size": "0"}
            rec = {}
        elif self.fill_after_market:
            self.orders[oid] = rec
            self.position = {
                "symbol": "SOL-USD",
                "side": "long" if str(kw.get("side")).lower() == "buy" else "short",
                "size": size,
                "entry_price": "80",
            }
        return {
            "client_order_id": cid,
            "exchange_order_id": oid,
            "submitted_volume": size,
            "status": "filled",
            "verified": True,
            "role": "entry",
        }

    def get_order_state(self, account, order_index):
        self.ops.append("get_order_state")
        return dict(self.orders.get(int(order_index)) or {})

    def get_order_state_by_client_id(self, account, instrument, client_order_index):
        self.ops.append("get_order_state_by_client_id")
        want = int(client_order_index)
        for rec in self.orders.values():
            if int(rec.get("client_order_index") or 0) == want:
                return dict(rec)
        return {}

    def set_shared_tp(self, **kw):
        self.ops.append("set_shared_tp")
        self.set_tp_calls.append(dict(kw))
        oid = self._next_oid()
        rec = {
            "exchange_order_id": oid,
            "status": "open",
            "taxonomy": "ACTIVE",
            "requested_size": str(kw["size"]),
            "side": str(kw.get("side") or "sell").lower(),
        }
        self.orders[oid] = rec
        self.position["tp"] = str(kw["price"])
        return {
            "client_order_id": kw["client_order_id"],
            "exchange_order_id": oid,
            "submitted_price": str(kw["price"]),
            "submitted_volume": str(kw["size"]),
            "status": "submitted",
            "verified": True,
            "role": "tp",
        }

    def place_limit(self, **kw):
        self.ops.append("place_limit")
        self.place_limit_calls.append(dict(kw))
        oid = self._next_oid()
        rec = {
            "exchange_order_id": oid,
            "client_order_index": int(kw["client_order_id"]),
            "status": "open",
            "taxonomy": "ACTIVE",
            "requested_size": str(kw["size"]),
            "side": str(kw.get("side") or "buy").lower(),
        }
        self.orders[oid] = rec
        return {
            "client_order_id": kw["client_order_id"],
            "exchange_order_id": oid,
            "submitted_price": str(kw["price"]),
            "submitted_volume": str(kw["size"]),
            "status": "open",
            "verified": True,
            "role": "ladder",
        }

    def cancel_order(self, **kw):
        self.ops.append("cancel_order")
        return True

    def close_position(self, **kw):
        self.ops.append("close_position")
        return {"success": True, "verified": True}


def _fresh_svc_and_state(tmp: str, adapter: _RecordingArcusAdapter):
    svc = PersistentFiboService(
        state_path=Path(tmp) / "s.json",
        ledger_path=Path(tmp) / "l.jsonl",
        event_log_path=Path(tmp) / "e.log",
        start_thread=False,
    )
    key = "arcus/metamask/SOL/BUY"
    st = GoldenFiboState(
        registration_key=key,
        exchange="arcus",
        account="metamask",
        instrument="SOL",
        direction="BUY",
        percentage=Decimal("0.01"),
        step0_volume=Decimal("0.2"),
        status=STATUS_RUNNING,
        cycle_id=0,
        highest_filled_step=-1,
        next_step=0,
    )
    svc._states[key] = st
    svc._adapters[key] = adapter
    svc._save_state()
    return svc, key, st


class ArcusStartupSequenceTests(unittest.TestCase):
    def test_happy_path_one_step0_tp_one_step1_then_wait(self):
        adapter = _RecordingArcusAdapter()
        with TemporaryDirectory() as tmp:
            svc, key, _ = _fresh_svc_and_state(tmp, adapter)
            svc._drive_one(key)
            st = svc._states[key]
            self.assertEqual(len(adapter.place_market_calls), 1)
            self.assertEqual(str(adapter.place_market_calls[0]["size"]), "0.2")
            self.assertEqual(str(adapter.place_market_calls[0]["side"]).lower(), "buy")
            self.assertEqual(st.highest_filled_step, 0)
            self.assertEqual(st.next_step, 1)
            self.assertEqual(len(adapter.set_tp_calls), 1)
            self.assertIsNotNone(st.current_tp_order_id)
            self.assertEqual(len(adapter.place_limit_calls), 1)
            self.assertFalse(adapter.place_limit_calls[0].get("reduce_only"))
            self.assertEqual(st.pending_order_role, ROLE_LADDER)
            self.assertIsNotNone(st.pending_order_exchange_id)
            ops_after_start = list(adapter.ops)
            self.assertEqual(ops_after_start.count("place_market"), 1)
            self.assertEqual(ops_after_start.count("set_shared_tp"), 1)
            self.assertEqual(ops_after_start.count("place_limit"), 1)
            market_i = ops_after_start.index("place_market")
            tp_i = ops_after_start.index("set_shared_tp")
            limit_i = ops_after_start.index("place_limit")
            self.assertLess(market_i, tp_i)
            self.assertLess(tp_i, limit_i)
            for _ in range(5):
                svc._drive_one(key)
            self.assertEqual(len(adapter.place_market_calls), 1)
            self.assertEqual(len(adapter.place_limit_calls), 1)
            self.assertEqual(len(adapter.set_tp_calls), 1)
            self.assertEqual(svc._states[key].status, STATUS_RUNNING)
            self.assertEqual(svc._states[key].highest_filled_step, 0)
            self.assertEqual(svc._states[key].pending_order_role, ROLE_LADDER)

    def test_step0_submitted_unknown_position_does_not_repeat(self):
        adapter = _RecordingArcusAdapter()
        adapter.ambiguous_after_market = True
        with TemporaryDirectory() as tmp:
            svc, key, _ = _fresh_svc_and_state(tmp, adapter)
            svc._drive_one(key)
            self.assertEqual(len(adapter.place_market_calls), 1)
            st = svc._states[key]
            self.assertEqual(st.submission_phase, SUBMISSION_CONFIRMED)
            self.assertEqual(st.highest_filled_step, -1)
            self.assertNotEqual(st.pending_order_role, ROLE_LADDER)
            for _ in range(6):
                svc._drive_one(key)
            self.assertEqual(len(adapter.place_market_calls), 1)
            self.assertEqual(len(adapter.place_limit_calls), 0)
            self.assertEqual(len(adapter.set_tp_calls), 0)
            st = svc._states[key]
            self.assertEqual(st.status, STATUS_NEEDS_RECOVERY)
            self.assertEqual(st.highest_filled_step, -1)


if __name__ == "__main__":
    unittest.main()
