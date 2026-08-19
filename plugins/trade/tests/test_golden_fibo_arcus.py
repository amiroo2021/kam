"""Offline GoldenFibo × Arcus adapter tests (no live orders)."""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest import mock

from plugins.trade.fibo_service import SUPPORTED_EXCHANGES
from plugins.trade.golden_fibo.arcus_adapter import ArcusGoldenFiboAdapter
from plugins.trade.golden_fibo.client_id_v2 import (
    ROLE_EMERGENCY_CLOSE,
    ROLE_LADDER_ENTRY,
    ROLE_SHARED_TP,
    ROLE_STEP0,
    decode_golden_fibo_client_id,
    encode_golden_fibo_client_id,
    is_golden_fibo_v2_client_id,
)
from plugins.trade.golden_fibo.config import (
    GoldenFiboConfig,
    golden_fibo_next_ladder_price,
    golden_fibo_tp_price,
    golden_fibo_volume,
)
from plugins.trade.golden_fibo.engine import GoldenFiboEngine
from plugins.trade.golden_fibo.state import (
    ROLE_ENTRY,
    ROLE_LADDER,
    ROLE_TP,
    SHUTDOWN_MODE_SMOOTH,
    STATUS_NEEDS_RECOVERY,
    STATUS_RUNNING,
    STATUS_SMOOTH_SHUTDOWN,
    SUBMISSION_ATTEMPTED,
    GoldenFiboState,
)


class _Ok:
    def __init__(self, **kw):
        self.success = True
        self.error = None
        for k, v in kw.items():
            setattr(self, k, v)


class _Fail:
    def __init__(self, code="ERR", message="x"):
        self.success = False

        class E:
            pass

        e = E()
        e.code = code
        e.message = message
        self.error = e


class FakeArcusAgent:
    """In-memory Arcus agent stand-in for adapter/engine tests."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.position = {"symbol": "BTC", "side": None, "size": "0", "entry_price": "0"}
        self.orders: Dict[int, Dict[str, Any]] = {}
        self._oid = 9000
        self.mark = "100.0"
        self.constraints = {
            "symbol": "BTC",
            "market_id": 1,
            "min_base_amount": "0.001",
            "min_quote_amount": "10",
            "size_decimals": 3,
            "price_decimals": 1,
            "tick_size": "0.1",
            "step_size": "0.001",
        }

    def _new_oid(self) -> int:
        self._oid += 1
        return self._oid

    def execute(self, request: Dict[str, Any]):
        self.calls.append(dict(request))
        op = request.get("operation")
        if op == "resolve_instrument":
            return _Ok(
                instrument={
                    "requested_symbol": request.get("symbol"),
                    "symbol": request.get("symbol"),
                    "display_name": request.get("symbol"),
                    "price_increment": "0.1",
                    "size_increment": "0.001",
                    "minimum_size": "0.001",
                }
            )
        if op == "market_constraints":
            return _Ok(order_state=dict(self.constraints))
        if op == "market_price":
            return _Ok(
                market_price={
                    "requested_symbol": request.get("symbol"),
                    "market": request.get("symbol"),
                    "mark_price": self.mark,
                    "price": self.mark,
                }
            )
        if op == "position_state":
            if self.position.get("side") and Decimal(str(self.position.get("size") or 0)) > 0:
                return _Ok(positions=[dict(self.position)])
            return _Ok(positions=[])
        if op == "new_order":
            oid = self._new_oid()
            cid = request.get("client_order_id") or request.get("client_order_index")
            otype = str(request.get("order_type") or "limit").lower()
            side = str(request.get("side") or "").lower()
            size = str(request.get("volume") or "0")
            price = str(request.get("price") or self.mark)
            rec = {
                "order_index": oid,
                "exchange_order_id": oid,
                "client_order_index": int(cid) if cid is not None else None,
                "client_order_id": cid,
                "symbol": request.get("symbol"),
                "side": side,
                "type": otype,
                "status": "filled" if otype == "market" else "open",
                "taxonomy": "FILLED" if otype == "market" else "ACTIVE",
                "requested_size": size,
                "filled_size": size if otype == "market" else "0",
                "remaining_size": "0" if otype == "market" else size,
                "requested_price": price,
                "actual_fill_price": self.mark if otype == "market" else None,
                "reduce_only": bool(request.get("reduce_only")),
            }
            self.orders[oid] = rec
            if otype == "market" and not request.get("reduce_only"):
                self.position = {
                    "symbol": request.get("symbol"),
                    "side": "long" if side == "buy" else "short",
                    "size": size,
                    "entry_price": self.mark,
                }
            return _Ok(
                order={
                    "symbol": request.get("symbol"),
                    "side": side,
                    "order_type": otype,
                    "requested_volume": size,
                    "requested_price": price,
                    "submitted_volume": size,
                    "submitted_price": price,
                    "verified": True,
                    "status": "success",
                    "exchange_order_id": oid,
                    "client_order_id": cid,
                }
            )
        if op == "set_tp":
            oid = self._new_oid()
            cid = request.get("client_order_id") or request.get("client_order_index")
            # cancel prior TP reduce-only
            for k, o in list(self.orders.items()):
                if o.get("reduce_only") and str(o.get("type")) in {"limit", "take-profit"}:
                    del self.orders[k]
            size = str(self.position.get("size") or request.get("size") or "0")
            self.orders[oid] = {
                "order_index": oid,
                "exchange_order_id": oid,
                "client_order_index": int(cid) if cid is not None else None,
                "side": "sell" if self.position.get("side") == "long" else "buy",
                "type": "take-profit",
                "status": "untriggered",
                "taxonomy": "ACTIVE",
                "requested_size": size,
                "filled_size": "0",
                "remaining_size": size,
                "requested_price": str(request.get("price")),
                "reduce_only": True,
            }
            return _Ok(
                position_action={
                    "operation": "set_tp",
                    "symbol": request.get("symbol"),
                    "verified": True,
                    "status": "success",
                    "price": str(request.get("price")),
                    "current_size": size,
                    "exchange_order_id": oid,
                }
            )
        if op == "get_order_state":
            oid = int(request.get("order_index"))
            return _Ok(order_state=dict(self.orders.get(oid) or {}))
        if op == "get_order_state_by_client_id":
            cid = int(request.get("client_order_index") or request.get("client_order_id"))
            for o in self.orders.values():
                if int(o.get("client_order_index") or -1) == cid:
                    return _Ok(order_state=dict(o))
            # synthetic filled from position for market entry
            if self.position.get("side") and Decimal(str(self.position.get("size") or 0)) > 0:
                return _Ok(
                    order_state={
                        "client_order_index": cid,
                        "status": "filled",
                        "taxonomy": "FILLED",
                        "side": "buy" if self.position["side"] == "long" else "sell",
                        "type": "market",
                        "filled_size": self.position["size"],
                        "requested_size": self.position["size"],
                        "actual_fill_price": self.position.get("entry_price") or self.mark,
                        "reduce_only": False,
                    }
                )
            return _Ok(order_state={})
        if op == "cancel_order":
            oid = int(request.get("order_index"))
            self.orders.pop(oid, None)
            return _Ok(order_state={"order_index": oid, "status": "canceled", "taxonomy": "CANCELED", "verified": True})
        if op == "close_position":
            cid = request.get("client_order_id")
            self.position = {"symbol": request.get("symbol"), "side": None, "size": "0"}
            # drop TP
            for k, o in list(self.orders.items()):
                if o.get("reduce_only"):
                    del self.orders[k]
            return _Ok(
                position_action={
                    "operation": "close_position",
                    "verified": True,
                    "status": "success",
                    "message": "closed",
                    "client_order_index": cid,
                }
            )
        return _Fail("NOT_IMPLEMENTED", op)


def _cfg(**kw):
    base = dict(
        exchange="arcus",
        account="main",
        instrument="BTC",
        direction="BUY",
        percentage=Decimal("0.001"),
        step0_volume=Decimal("0.2"),
    )
    base.update(kw)
    return GoldenFiboConfig(**base)


class SupportedExchangeTests(unittest.TestCase):
    def test_arcus_in_supported(self):
        self.assertIn("arcus", SUPPORTED_EXCHANGES)
        self.assertIn("lighter", SUPPORTED_EXCHANGES)


class AdapterForwardTests(unittest.TestCase):
    def setUp(self):
        self.agent = FakeArcusAgent()
        self.ad = ArcusGoldenFiboAdapter()
        self.patcher = mock.patch(
            "plugins.trade.golden_fibo.arcus_adapter.arcus_agent.execute",
            side_effect=self.agent.execute,
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_v2_step0_forwarded(self):
        cid = encode_golden_fibo_client_id(
            direction="BUY", role=ROLE_STEP0, cycle_uid=1, step=0, seq=0
        )
        out = self.ad.place_market(
            account="main", instrument="BTC", side="buy", size=Decimal("0.2"), client_order_id=cid
        )
        self.assertEqual(int(out["client_order_id"]), cid)
        last = self.agent.calls[-1]
        self.assertEqual(last["order_type"], "market")
        self.assertEqual(int(last["client_order_id"]), cid)

    def test_v2_ladder_forwarded(self):
        cid = encode_golden_fibo_client_id(
            direction="BUY", role=ROLE_LADDER_ENTRY, cycle_uid=1, step=1, seq=0
        )
        out = self.ad.place_limit(
            account="main",
            instrument="BTC",
            side="buy",
            size=Decimal("0.2"),
            price=Decimal("99.0"),
            client_order_id=cid,
        )
        self.assertEqual(int(out["client_order_id"]), cid)
        self.assertEqual(self.agent.calls[-1]["order_type"], "limit")

    def test_v2_tp_forwarded(self):
        self.agent.position = {"symbol": "BTC", "side": "long", "size": "0.2", "entry_price": "100"}
        cid = encode_golden_fibo_client_id(
            direction="BUY", role=ROLE_SHARED_TP, cycle_uid=1, step=0, seq=0
        )
        out = self.ad.set_shared_tp(
            account="main",
            instrument="BTC",
            price=Decimal("100.1"),
            side="sell",
            size=Decimal("0.2"),
            client_order_id=cid,
        )
        self.assertEqual(int(out["client_order_id"]), cid)
        self.assertEqual(self.agent.calls[-1]["operation"], "set_tp")
        self.assertEqual(int(self.agent.calls[-1]["client_order_id"]), cid)

    def test_v2_emergency_close_forwarded(self):
        self.agent.position = {"symbol": "BTC", "side": "long", "size": "0.2", "entry_price": "100"}
        cid = encode_golden_fibo_client_id(
            direction="BUY", role=ROLE_EMERGENCY_CLOSE, cycle_uid=1, step=0, seq=0
        )
        out = self.ad.close_position(account="main", instrument="BTC", client_order_id=cid)
        self.assertTrue(out["success"])
        self.assertEqual(int(self.agent.calls[-1]["client_order_id"]), cid)


class EngineArcusLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.agent = FakeArcusAgent()
        self.ad = ArcusGoldenFiboAdapter()
        self.patcher = mock.patch(
            "plugins.trade.golden_fibo.arcus_adapter.arcus_agent.execute",
            side_effect=self.agent.execute,
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def _engine(self, direction="BUY", step0="0.2", pct="0.001"):
        st = GoldenFiboState(
            registration_key=f"arcus/main/BTC/{direction}",
            exchange="arcus",
            account="main",
            instrument="BTC",
            direction=direction,
            step0_volume=Decimal(step0),
            percentage=Decimal(pct),
            client_id_version=2,
        )
        cfg = _cfg(direction=direction, step0_volume=Decimal(step0), percentage=Decimal(pct))
        return GoldenFiboEngine(cfg, st, self.ad), st

    def test_buy_step0_and_p0_from_fill(self):
        eng, st = self._engine("BUY")
        eng._start_fresh_cycle([])
        self.assertTrue(is_golden_fibo_v2_client_id(st.pending_order_client_id))
        d = decode_golden_fibo_client_id(st.pending_order_client_id)
        self.assertEqual(d.role_name, "STEP0")
        # confirm via service-like path
        eng.confirm_step0_filled(Decimal(self.agent.mark))
        self.assertEqual(st.highest_filled_step, 0)
        self.assertEqual(st.fill_prices[0], Decimal(self.agent.mark))

    def test_sell_step0(self):
        eng, st = self._engine("SELL")
        eng._start_fresh_cycle([])
        d = decode_golden_fibo_client_id(st.pending_order_client_id)
        self.assertEqual(d.direction_name, "SELL")
        self.assertEqual(d.role_name, "STEP0")

    def test_tp0_buy_and_sell_prices(self):
        p0 = Decimal("100")
        pct = Decimal("0.001")
        self.assertEqual(golden_fibo_tp_price("BUY", p0, pct), p0 * (1 + pct))
        self.assertEqual(golden_fibo_tp_price("SELL", p0, pct), p0 * (1 - pct))

    def test_step1_price_and_volume(self):
        p0 = Decimal("100")
        pct = Decimal("0.001")
        tp0 = golden_fibo_tp_price("BUY", p0, pct)
        p1 = golden_fibo_next_ladder_price("BUY", p0, tp0)
        eng, st = self._engine("BUY")
        eng._start_fresh_cycle([])
        eng.confirm_step0_filled(p0)
        # place TP + step1
        err = eng._rotate_tp(p0)
        self.assertIsNone(err)
        st.next_step = 1
        st.highest_filled_step = 0
        st.fill_prices = {0: p0}
        st.expected_cumulative_size = Decimal("0.2")
        self.agent.position = {"symbol": "BTC", "side": "long", "size": "0.2", "entry_price": str(p0)}
        err = eng._place_next_ladder()
        self.assertIsNone(err)
        self.assertEqual(st.pending_order_role, ROLE_LADDER)
        self.assertEqual(st.pending_requested_size, Decimal("0.2"))
        # volume step2 doubles
        self.assertEqual(golden_fibo_volume(Decimal("0.2"), 2), Decimal("0.4"))
        d = decode_golden_fibo_client_id(st.pending_order_client_id)
        self.assertEqual(d.role_name, "LADDER_ENTRY")
        self.assertEqual(d.step, 1)
        self.assertLess(p1, p0)  # BUY ladder down

    def test_exactly_one_pending_limit_after_setup(self):
        eng, st = self._engine("BUY")
        eng._start_fresh_cycle([])
        eng.confirm_step0_filled(Decimal("100"))
        self.agent.position = {"symbol": "BTC", "side": "long", "size": "0.2", "entry_price": "100"}
        eng._rotate_tp(Decimal("100"))
        st.next_step = 1
        st.highest_filled_step = 0
        st.fill_prices = {0: Decimal("100")}
        st.expected_cumulative_size = Decimal("0.2")
        eng._place_next_ladder()
        limits = [
            o
            for o in self.agent.orders.values()
            if o.get("type") == "limit" and not o.get("reduce_only")
        ]
        self.assertEqual(len(limits), 1)

    def test_partial_step1_updates_tp_size_only(self):
        eng, st = self._engine("BUY")
        st.cycle_uid = 5
        st.highest_cycle_uid = 5
        st.highest_filled_step = 0
        st.fill_prices = {0: Decimal("100")}
        st.expected_cumulative_size = Decimal("0.2")
        st.current_tp_price = Decimal("100.1")
        st.current_tp_size = Decimal("0.2")
        st.current_tp_order_id = 1
        st.current_tp_client_id = 1
        st.status = STATUS_RUNNING
        self.agent.orders[1] = {
            "order_index": 1,
            "status": "open",
            "taxonomy": "ACTIVE",
            "requested_size": "0.2",
            "reduce_only": True,
            "type": "limit",
        }
        # position grew partially
        self.agent.position = {"symbol": "BTC", "side": "long", "size": "0.3", "entry_price": "100"}
        eng._sync_tp_volume(Decimal("0.3"), [])
        self.assertEqual(st.current_tp_price, Decimal("100.1"))
        self.assertEqual(Decimal(str(st.current_tp_size)), Decimal("0.3"))

    def test_smooth_shutdown_no_new_step0(self):
        eng, st = self._engine("BUY")
        st.shutdown_mode = SHUTDOWN_MODE_SMOOTH
        st.status = STATUS_SMOOTH_SHUTDOWN
        res = eng._start_fresh_cycle([])
        # should complete smooth, not place market
        markets = [c for c in self.agent.calls if c.get("operation") == "new_order"]
        self.assertEqual(markets, [])

    def test_emergency_close_cleans(self):
        from plugins.trade.fibo_service import PersistentFiboService
        import tempfile
        from pathlib import Path

        self.agent.position = {"symbol": "BTC", "side": "long", "size": "0.2", "entry_price": "100"}
        # adapter close
        cid = encode_golden_fibo_client_id(
            direction="BUY", role=ROLE_EMERGENCY_CLOSE, cycle_uid=9, step=0, seq=0
        )
        out = self.ad.close_position(account="main", instrument="BTC", client_order_id=cid)
        self.assertTrue(out["verified"])
        self.assertIsNone(self.agent.position.get("side"))

    def test_restart_does_not_duplicate_attempted_step0(self):
        eng, st = self._engine("BUY")
        eng._start_fresh_cycle([])
        cid = st.pending_order_client_id
        st.submission_phase = SUBMISSION_ATTEMPTED
        st.submission_role = ROLE_ENTRY
        st.submission_step = 0
        st.submission_client_id = cid
        # second start_fresh should freeze not re-place
        res = eng._start_fresh_cycle([])
        self.assertEqual(st.status, STATUS_NEEDS_RECOVERY)
        markets = [c for c in self.agent.calls if c.get("operation") == "new_order"]
        self.assertEqual(len(markets), 1)

    def test_ownership_mismatch_style_close_still_targeted(self):
        # adapter close only targets instrument path — unrelated orders remain
        self.agent.orders[55] = {
            "order_index": 55,
            "symbol": "ETH",
            "side": "buy",
            "type": "limit",
            "status": "open",
            "taxonomy": "ACTIVE",
            "reduce_only": False,
        }
        self.agent.position = {"symbol": "BTC", "side": "long", "size": "0.2", "entry_price": "100"}
        self.ad.close_position(account="main", instrument="BTC", client_order_id=1)
        self.assertIn(55, self.agent.orders)


class TradeUnchangedSmoke(unittest.TestCase):
    def test_new_order_without_client_still_defaults(self):
        # /trade path: omit client_order_id → agent mints arcus-* id (unit-level)
        from plugins.trade.agents import x_arcus_agent as A

        cid = A._arcus_normalize_client_id(None)
        self.assertTrue(str(cid).startswith("arcus-"))
        cid2 = A._arcus_normalize_client_id(82738589916160)
        self.assertEqual(cid2, "82738589916160")


if __name__ == "__main__":
    unittest.main()
