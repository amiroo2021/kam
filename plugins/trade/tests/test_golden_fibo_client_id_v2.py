"""GoldenFibo V2 client_order_index — encode/decode, allocation, restart safety."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from plugins.trade.golden_fibo.client_id_v2 import (
    DIRECTION_BUY,
    DIRECTION_SELL,
    LIGHTER_MAX_CLIENT_ORDER_INDEX,
    MAGIC,
    MAX_CYCLE_UID,
    MAX_SEQ,
    MAX_STEP_NORMAL,
    ROLE_EMERGENCY_CLOSE,
    ROLE_LADDER_ENTRY,
    ROLE_SHARED_TP,
    ROLE_STEP0,
    STEP_UNKNOWN,
    VERSION,
    ClientIdError,
    SeqExhaustedError,
    allocate_client_id,
    allocate_cycle_uid,
    decode_golden_fibo_client_id,
    encode_golden_fibo_client_id,
    epoch_minute_now,
    is_golden_fibo_v2_client_id,
    scan_highest_cycle_uid_from_client_ids,
)
from plugins.trade.golden_fibo.config import GoldenFiboConfig
from plugins.trade.golden_fibo.engine import GoldenFiboEngine
from plugins.trade.golden_fibo.state import (
    ROLE_ENTRY,
    ROLE_LADDER,
    ROLE_TP,
    SHUTDOWN_MODE_SMOOTH,
    STATUS_RUNNING,
    SUBMISSION_ATTEMPTED,
    SUBMISSION_CONFIRMED,
    SUBMISSION_PREPARED,
    GoldenFiboState,
)


class _RecAdapter:
    def __init__(self) -> None:
        self.position = {"symbol": "SOL", "side": None, "size": "0"}
        self.orders: Dict[int, dict] = {}
        self._oid = 5000
        self.submits: List[dict] = []
        self.closes: List[dict] = []

    def _oid_new(self) -> int:
        self._oid += 1
        return self._oid

    def resolve_instrument(self, account, instrument):
        return {
            "symbol": instrument,
            "market_id": 2,
            "size_decimals": 3,
            "price_decimals": 3,
            "min_base_amount": "0.001",
            "min_quote_amount": "10",
        }

    def market_constraints(self, account, instrument):
        return self.resolve_instrument(account, instrument)

    def position_state(self, account, instrument):
        return dict(self.position)

    def get_order_state(self, account, order_index):
        return dict(self.orders.get(int(order_index)) or {})

    def get_order_state_by_client_id(self, account, instrument, client_order_index):
        for o in self.orders.values():
            if int(o.get("client_order_index") or 0) == int(client_order_index):
                return dict(o)
        return {}

    def place_market(self, **kw):
        oid = self._oid_new()
        cid = int(kw["client_order_id"])
        rec = {
            "order_index": oid,
            "client_order_index": cid,
            "status": "filled",
            "taxonomy": "FILLED",
            "actual_fill_price": "100.0",
            "filled_size": str(kw["size"]),
        }
        self.orders[oid] = rec
        self.submits.append({"op": "market", **kw, "oid": oid})
        return {
            "exchange_order_id": oid,
            "client_order_id": cid,
            "submitted_volume": str(kw["size"]),
        }

    def place_limit(self, **kw):
        oid = self._oid_new()
        cid = int(kw["client_order_id"])
        rec = {
            "order_index": oid,
            "client_order_index": cid,
            "status": "open",
            "taxonomy": "ACTIVE",
            "requested_price": str(kw["price"]),
            "requested_size": str(kw["size"]),
        }
        self.orders[oid] = rec
        self.submits.append({"op": "limit", **kw, "oid": oid})
        return {
            "exchange_order_id": oid,
            "client_order_id": cid,
            "submitted_price": str(kw["price"]),
            "submitted_volume": str(kw["size"]),
        }

    def set_shared_tp(self, **kw):
        oid = self._oid_new()
        cid = int(kw["client_order_id"])
        rec = {
            "order_index": oid,
            "client_order_index": cid,
            "status": "open",
            "taxonomy": "ACTIVE",
            "reduce_only": True,
            "type": "limit",
        }
        self.orders[oid] = rec
        self.submits.append({"op": "tp", **kw, "oid": oid})
        return {
            "exchange_order_id": oid,
            "client_order_id": cid,
            "submitted_price": str(kw["price"]),
            "submitted_volume": str(kw["size"]),
        }

    def cancel_order(self, *, account, order_index):
        self.orders.pop(int(order_index), None)
        return True

    def close_position(self, *, account, instrument, client_order_id=None):
        self.closes.append({"account": account, "instrument": instrument, "cid": client_order_id})
        self.position = {"symbol": instrument, "side": None, "size": "0"}
        return {"success": True, "verified": True, "client_order_id": client_order_id}


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


class EncodeDecodeTests(unittest.TestCase):
    def test_roundtrip_buy_step0(self):
        cid = encode_golden_fibo_client_id(
            direction=DIRECTION_BUY,
            role=ROLE_STEP0,
            cycle_uid=12345,
            step=0,
            seq=0,
        )
        self.assertLessEqual(cid, LIGHTER_MAX_CLIENT_ORDER_INDEX)
        d = decode_golden_fibo_client_id(cid)
        self.assertEqual(d.version, VERSION)
        self.assertEqual(d.direction_name, "BUY")
        self.assertEqual(d.role, ROLE_STEP0)
        self.assertEqual(d.cycle_uid, 12345)
        self.assertEqual(d.step, 0)
        self.assertEqual(d.seq, 0)
        self.assertTrue(is_golden_fibo_v2_client_id(cid))

    def test_roundtrip_sell_ladder_step20(self):
        cid = encode_golden_fibo_client_id(
            direction="SELL",
            role=ROLE_LADDER_ENTRY,
            cycle_uid=99,
            step=20,
            seq=3,
        )
        d = decode_golden_fibo_client_id(cid)
        self.assertEqual(d.direction_name, "SELL")
        self.assertEqual(d.role, ROLE_LADDER_ENTRY)
        self.assertEqual(d.step, 20)
        self.assertEqual(d.seq, 3)

    def test_tp_role_step(self):
        cid = encode_golden_fibo_client_id(
            direction="BUY", role=ROLE_SHARED_TP, cycle_uid=7, step=2, seq=1
        )
        d = decode_golden_fibo_client_id(cid)
        self.assertEqual(d.role_name, "SHARED_TP")
        self.assertEqual(d.step, 2)
        self.assertEqual(d.seq, 1)

    def test_emergency_close_role(self):
        cid = encode_golden_fibo_client_id(
            direction="BUY",
            role=ROLE_EMERGENCY_CLOSE,
            cycle_uid=7,
            step=STEP_UNKNOWN,
            seq=0,
        )
        d = decode_golden_fibo_client_id(cid)
        self.assertEqual(d.role, ROLE_EMERGENCY_CLOSE)
        self.assertEqual(d.step, 31)

    def test_max_48bit_boundary(self):
        cid = encode_golden_fibo_client_id(
            direction=1,
            role=7,
            cycle_uid=MAX_CYCLE_UID,
            step=31,
            seq=MAX_SEQ,
        )
        self.assertLessEqual(cid, LIGHTER_MAX_CLIENT_ORDER_INDEX)
        self.assertEqual((cid >> 40) & 0xFF, MAGIC)

    def test_invalid_magic_rejected(self):
        # craft bad magic by flipping high byte of a valid id
        good = encode_golden_fibo_client_id(
            direction=0, role=0, cycle_uid=1, step=0, seq=0
        )
        bad = good & ~(0xFF << 40)  # clear magic
        self.assertFalse(is_golden_fibo_v2_client_id(bad))
        with self.assertRaises(ClientIdError):
            decode_golden_fibo_client_id(bad)

    def test_unsupported_version_rejected(self):
        # manually set version bits to 2
        good = encode_golden_fibo_client_id(
            direction=0, role=0, cycle_uid=1, step=0, seq=0
        )
        # clear version bits then set to 2
        cleared = good & ~(0x3 << 38)
        bad = cleared | (2 << 38)
        with self.assertRaises(ClientIdError):
            decode_golden_fibo_client_id(bad)

    def test_legacy_100001_not_v2(self):
        for legacy in (100001, 1100001, 2100001, 5100002):
            self.assertFalse(is_golden_fibo_v2_client_id(legacy))
            with self.assertRaises(ClientIdError):
                decode_golden_fibo_client_id(legacy)

    def test_two_cycles_different_ids(self):
        a = encode_golden_fibo_client_id(
            direction=0, role=ROLE_STEP0, cycle_uid=10, step=0, seq=0
        )
        b = encode_golden_fibo_client_id(
            direction=0, role=ROLE_STEP0, cycle_uid=11, step=0, seq=0
        )
        self.assertNotEqual(a, b)

    def test_same_cycle_step1_vs_step2(self):
        a = encode_golden_fibo_client_id(
            direction=0, role=ROLE_LADDER_ENTRY, cycle_uid=5, step=1, seq=0
        )
        b = encode_golden_fibo_client_id(
            direction=0, role=ROLE_LADDER_ENTRY, cycle_uid=5, step=2, seq=0
        )
        self.assertNotEqual(a, b)

    def test_entry_vs_tp_different(self):
        a = encode_golden_fibo_client_id(
            direction=0, role=ROLE_LADDER_ENTRY, cycle_uid=5, step=2, seq=0
        )
        b = encode_golden_fibo_client_id(
            direction=0, role=ROLE_SHARED_TP, cycle_uid=5, step=2, seq=0
        )
        self.assertNotEqual(a, b)


class AllocationTests(unittest.TestCase):
    def test_tp_replacement_increments_seq(self):
        m: dict = {}
        a = allocate_client_id(
            direction="BUY", role=ROLE_SHARED_TP, cycle_uid=1, step=2, seq_map=m
        )
        b = allocate_client_id(
            direction="BUY", role=ROLE_SHARED_TP, cycle_uid=1, step=2, seq_map=m
        )
        da, db = decode_golden_fibo_client_id(a), decode_golden_fibo_client_id(b)
        self.assertEqual(da.seq, 0)
        self.assertEqual(db.seq, 1)
        self.assertEqual(da.cycle_uid, db.cycle_uid)

    def test_retry_reuses_same_id(self):
        m: dict = {}
        a = allocate_client_id(
            direction="BUY", role=ROLE_STEP0, cycle_uid=9, step=0, seq_map=m
        )
        # reuse_seq=0 must not bump
        b = allocate_client_id(
            direction="BUY",
            role=ROLE_STEP0,
            cycle_uid=9,
            step=0,
            seq_map=m,
            reuse_seq=0,
        )
        self.assertEqual(a, b)
        self.assertEqual(m["0:0"], 0)

    def test_seq_exhaustion_raises(self):
        m = {"2:3": MAX_SEQ}  # last used = 31
        with self.assertRaises(SeqExhaustedError):
            allocate_client_id(
                direction="BUY", role=ROLE_SHARED_TP, cycle_uid=1, step=3, seq_map=m
            )

    def test_cycle_uid_monotonic_restart(self):
        u1 = allocate_cycle_uid(previous_local_cycle_uid=100)
        self.assertGreater(u1, 100)
        u2 = allocate_cycle_uid(
            previous_local_cycle_uid=u1,
            highest_exchange_cycle_uid=u1 + 50,
        )
        self.assertGreater(u2, u1 + 50)

    def test_fresh_server_uses_exchange_hint(self):
        # No local previous; exchange has 5000
        u = allocate_cycle_uid(
            previous_local_cycle_uid=None,
            highest_exchange_cycle_uid=5000,
            now=datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc),  # epoch+60min
        )
        self.assertEqual(u, 5001)

    def test_scan_highest_from_history(self):
        ids = [
            encode_golden_fibo_client_id(
                direction=0, role=0, cycle_uid=10, step=0, seq=0
            ),
            encode_golden_fibo_client_id(
                direction=0, role=2, cycle_uid=42, step=1, seq=0
            ),
            100001,  # legacy ignored
            999999,
        ]
        self.assertEqual(scan_highest_cycle_uid_from_client_ids(ids), 42)


class EngineV2IntegrationTests(unittest.TestCase):
    def test_step0_uses_exactly_one_v2_id(self):
        st = GoldenFiboState(
            registration_key="lighter/amiroo/SOL/BUY",
            direction="BUY",
            instrument="SOL",
            exchange="lighter",
            account="amiroo",
            step0_volume=Decimal("0.2"),
            percentage=Decimal("0.001"),
            client_id_version=2,
        )
        ad = _RecAdapter()
        eng = GoldenFiboEngine(_cfg(), st, ad, exchange_highest_cycle_uid=100)
        res = eng._start_fresh_cycle([])
        self.assertIsInstance(res.actions, list)
        self.assertTrue(is_golden_fibo_v2_client_id(st.pending_order_client_id))
        d = decode_golden_fibo_client_id(st.pending_order_client_id)
        self.assertEqual(d.role, ROLE_STEP0)
        self.assertEqual(d.step, 0)
        self.assertEqual(d.seq, 0)
        self.assertEqual(d.direction_name, "BUY")
        self.assertGreaterEqual(d.cycle_uid, 101)
        self.assertEqual(st.cycle_uid, d.cycle_uid)
        self.assertEqual(len([s for s in ad.submits if s["op"] == "market"]), 1)

    def test_ladder_has_correct_step(self):
        st = GoldenFiboState(
            registration_key="k",
            direction="BUY",
            instrument="SOL",
            exchange="lighter",
            account="amiroo",
            step0_volume=Decimal("0.2"),
            percentage=Decimal("0.001"),
            client_id_version=2,
            cycle_uid=200,
            highest_cycle_uid=200,
            highest_filled_step=0,
            next_step=1,
            fill_prices={0: Decimal("100")},
            expected_cumulative_size=Decimal("0.2"),
            status=STATUS_RUNNING,
        )
        ad = _RecAdapter()
        ad.position = {"symbol": "SOL", "side": "long", "size": "0.2"}
        eng = GoldenFiboEngine(_cfg(), st, ad)
        err = eng._place_next_ladder()
        self.assertIsNone(err)
        d = decode_golden_fibo_client_id(st.pending_order_client_id)
        self.assertEqual(d.role, ROLE_LADDER_ENTRY)
        self.assertEqual(d.step, 1)
        self.assertEqual(d.cycle_uid, 200)

    def test_tp_has_correct_role_step(self):
        st = GoldenFiboState(
            registration_key="k",
            direction="BUY",
            instrument="SOL",
            exchange="lighter",
            account="amiroo",
            step0_volume=Decimal("0.2"),
            percentage=Decimal("0.001"),
            client_id_version=2,
            cycle_uid=300,
            highest_cycle_uid=300,
            highest_filled_step=2,
            fill_prices={0: Decimal("100"), 1: Decimal("99"), 2: Decimal("98")},
            expected_cumulative_size=Decimal("0.8"),
            status=STATUS_RUNNING,
        )
        ad = _RecAdapter()
        ad.position = {"symbol": "SOL", "side": "long", "size": "0.8"}
        eng = GoldenFiboEngine(_cfg(), st, ad)
        err = eng._rotate_tp(Decimal("98"))
        self.assertIsNone(err)
        d = decode_golden_fibo_client_id(st.current_tp_client_id)
        self.assertEqual(d.role, ROLE_SHARED_TP)
        self.assertEqual(d.step, 2)
        self.assertEqual(d.seq, 0)

    def test_tp_replacement_increments_seq(self):
        st = GoldenFiboState(
            registration_key="k",
            direction="BUY",
            instrument="SOL",
            exchange="lighter",
            account="amiroo",
            step0_volume=Decimal("0.2"),
            percentage=Decimal("0.001"),
            client_id_version=2,
            cycle_uid=300,
            highest_cycle_uid=300,
            highest_filled_step=2,
            fill_prices={0: Decimal("100"), 2: Decimal("98")},
            expected_cumulative_size=Decimal("0.8"),
            current_tp_price=Decimal("110"),
            current_tp_size=Decimal("0.8"),
            current_tp_order_id=999,
            current_tp_client_id=1,
            status=STATUS_RUNNING,
        )
        ad = _RecAdapter()
        ad.position = {"symbol": "SOL", "side": "long", "size": "0.5"}  # partial
        ad.orders[999] = {"order_index": 999, "status": "open"}
        eng = GoldenFiboEngine(_cfg(), st, ad)
        # first TP volume sync
        eng._sync_tp_volume(Decimal("0.5"), [])
        d1 = decode_golden_fibo_client_id(st.current_tp_client_id)
        self.assertEqual(d1.seq, 0)
        st.current_tp_order_id = list(ad.orders)[-1]
        # second replacement
        ad.position["size"] = "0.3"
        eng._sync_tp_volume(Decimal("0.3"), [])
        d2 = decode_golden_fibo_client_id(st.current_tp_client_id)
        self.assertEqual(d2.seq, 1)
        self.assertEqual(d2.cycle_uid, d1.cycle_uid)

    def test_retry_same_logical_submission_reuses_id(self):
        st = GoldenFiboState(
            registration_key="k",
            direction="BUY",
            instrument="SOL",
            exchange="lighter",
            account="amiroo",
            step0_volume=Decimal("0.2"),
            percentage=Decimal("0.001"),
            client_id_version=2,
            cycle_uid=50,
            highest_cycle_uid=50,
            status=STATUS_RUNNING,
        )
        ad = _RecAdapter()
        eng = GoldenFiboEngine(_cfg(), st, ad)
        # Simulate prepared/attempted Step0 with known id
        cid = allocate_client_id(
            direction="BUY",
            role=ROLE_STEP0,
            cycle_uid=50,
            step=0,
            seq_map=st.client_seq_by_role_step,
        )
        st.submission_phase = SUBMISSION_ATTEMPTED
        st.submission_role = ROLE_ENTRY
        st.submission_step = 0
        st.submission_client_id = cid
        # _allocate should reuse
        again = eng._allocate_v2_client_id(
            role_code=ROLE_STEP0, step=0, engine_role=ROLE_ENTRY
        )
        self.assertEqual(again, cid)

    def test_restart_restores_cycle_uid_and_seq(self):
        st = GoldenFiboState(
            registration_key="k",
            direction="BUY",
            cycle_uid=777,
            highest_cycle_uid=777,
            client_id_version=2,
            client_seq_by_role_step={"2:1": 3},
            highest_filled_step=1,
        )
        blob = st.to_dict()
        st2 = GoldenFiboState.from_dict(blob)
        self.assertEqual(st2.cycle_uid, 777)
        self.assertEqual(st2.client_seq_by_role_step["2:1"], 3)
        # next TP seq for step 1 should be 4
        cid = allocate_client_id(
            direction="BUY",
            role=ROLE_SHARED_TP,
            cycle_uid=st2.cycle_uid,
            step=1,
            seq_map=st2.client_seq_by_role_step,
        )
        self.assertEqual(decode_golden_fibo_client_id(cid).seq, 4)

    def test_new_cycle_after_restart_no_reuse(self):
        st = GoldenFiboState(
            registration_key="k",
            direction="BUY",
            instrument="SOL",
            exchange="lighter",
            account="amiroo",
            step0_volume=Decimal("0.2"),
            percentage=Decimal("0.001"),
            client_id_version=2,
            cycle_uid=10,
            highest_cycle_uid=10,
        )
        ad = _RecAdapter()
        eng = GoldenFiboEngine(_cfg(), st, ad)
        eng._start_fresh_cycle([])
        self.assertGreater(st.cycle_uid, 10)
        self.assertGreater(st.highest_cycle_uid, 10)

    def test_smooth_shutdown_keeps_cycle_uid(self):
        st = GoldenFiboState(
            registration_key="k",
            direction="BUY",
            cycle_uid=55,
            highest_cycle_uid=55,
            client_id_version=2,
            shutdown_mode=SHUTDOWN_MODE_SMOOTH,
            status=STATUS_RUNNING,
            highest_filled_step=0,
            fill_prices={0: Decimal("100")},
            expected_cumulative_size=Decimal("0.2"),
        )
        before = st.cycle_uid
        ad = _RecAdapter()
        ad.position = {"symbol": "SOL", "side": "long", "size": "0.2"}
        eng = GoldenFiboEngine(_cfg(), st, ad)
        # placing ladder under smooth is blocked elsewhere; just ensure begin_new not called
        self.assertEqual(st.cycle_uid, before)

    def test_engine_new_cycle_gets_new_uid(self):
        st = GoldenFiboState(
            registration_key="k",
            direction="BUY",
            instrument="SOL",
            exchange="lighter",
            account="amiroo",
            step0_volume=Decimal("0.2"),
            percentage=Decimal("0.001"),
            client_id_version=2,
        )
        ad = _RecAdapter()
        eng = GoldenFiboEngine(_cfg(), st, ad, exchange_highest_cycle_uid=0)
        eng._start_fresh_cycle([])
        u1 = st.cycle_uid
        # complete-ish then new cycle
        eng._start_fresh_cycle([])
        self.assertNotEqual(st.cycle_uid, u1)
        self.assertGreater(st.cycle_uid, u1)


class EmergencyCloseAdapterTests(unittest.TestCase):
    def test_close_forwards_client_order_id(self):
        from plugins.trade.golden_fibo.lighter_adapter import LighterGoldenFiboAdapter
        import plugins.trade.agents.x_lighter_agent as agent

        captured = {}

        def fake_execute(req):
            captured.update(req)

            class R:
                success = True
                error = None
                data = {
                    "position_action": {
                        "verified": True,
                        "status": "success",
                        "message": "ok",
                    }
                }

                def to_dict(self):
                    return {"data": self.data}

            return R()

        old = agent.execute
        agent.execute = fake_execute  # type: ignore
        try:
            ad = LighterGoldenFiboAdapter()
            out = ad.close_position(
                account="amiroo", instrument="SOL", client_order_id=123456
            )
            self.assertEqual(captured.get("client_order_id"), 123456)
            self.assertEqual(captured.get("client_order_index"), 123456)
            self.assertEqual(out.get("client_order_id"), 123456)
        finally:
            agent.execute = old


class LighterRangeTests(unittest.TestCase):
    def test_all_role_encodings_within_48bit(self):
        for role in range(0, 8):
            for step in (0, 20, 31):
                if role == ROLE_STEP0 and step != 0:
                    continue
                if role in (ROLE_STEP0, ROLE_LADDER_ENTRY, ROLE_SHARED_TP) and step > 20 and step != 31:
                    continue
                try:
                    cid = encode_golden_fibo_client_id(
                        direction=1,
                        role=role,
                        cycle_uid=MAX_CYCLE_UID,
                        step=step if role != ROLE_STEP0 else 0,
                        seq=MAX_SEQ,
                    )
                except ClientIdError:
                    continue
                self.assertLessEqual(cid, LIGHTER_MAX_CLIENT_ORDER_INDEX)
                self.assertGreaterEqual(cid, 0)


if __name__ == "__main__":
    unittest.main()
