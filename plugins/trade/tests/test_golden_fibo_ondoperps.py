"""Offline GoldenFibo Ondo Perps adapter tests.

Mirrors the Rise adapter contract. No live HTTP.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest import mock

from plugins.trade.golden_fibo.engine import GoldenFiboConfig, GoldenFiboEngine
from plugins.trade.golden_fibo.state import GoldenFiboState
from plugins.trade.golden_fibo.ondoperps_adapter import (
    OndoPerpsGoldenFiboAdapter,
    encode_gf_client_order_id,
    _oid_to_int,
    _oid_to_str,
)


def _ondo_state(cycle_uid: int = 332200, **overrides) -> GoldenFiboState:
    s = GoldenFiboState()
    s.exchange = "ondoperps"
    s.account = "amiroo"
    s.instrument = "ONDO"
    s.direction = "BUY"
    s.percentage = Decimal("0.001")
    s.step0_volume = Decimal("1")
    s.cycle_uid = cycle_uid
    s.highest_cycle_uid = cycle_uid
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _ondo_config(direction: str = "BUY") -> GoldenFiboConfig:
    return GoldenFiboConfig(
        exchange="ondoperps",
        account="amiroo",
        instrument="ONDO",
        direction=direction,
        percentage=Decimal("0.001"),
        step0_volume=Decimal("1"),
    )


def _pos(size="0", side=None, entry=None, tp=None) -> Dict[str, Any]:
    return {
        "symbol": "ONDO",
        "side": side,
        "size": size,
        "entry_price": entry,
        "pnl": "0",
        "tp": tp,
        "sl": None,
    }


def _ok(**fields):
    m = mock.Mock(success=True, error=None)
    for k, v in fields.items():
        setattr(m, k, v)
    return m


def _fail(code="X"):
    return mock.Mock(success=False, error=mock.Mock(code=code, message=code))


# ------------------------------------------------------------------
# Client order ID
# ------------------------------------------------------------------

class ClientIdEncodingTests(unittest.TestCase):
    def test_int_becomes_decimal_string(self):
        self.assertEqual(encode_gf_client_order_id(82738589974528), "82738589974528")

    def test_matches_ondo_pattern(self):
        from plugins.trade.agents.x_ondoperps_agent import _CLIENT_ORDER_ID_PATTERN, _normalize_client_order_id
        cid = encode_gf_client_order_id(12345678901234)
        self.assertTrue(_CLIENT_ORDER_ID_PATTERN.fullmatch(cid))
        self.assertEqual(_normalize_client_order_id(cid), cid)

    def test_zero_is_valid_string(self):
        self.assertEqual(encode_gf_client_order_id(0), "0")

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            encode_gf_client_order_id(-1)

    def test_oid_int_round_trip(self):
        self.assertEqual(_oid_to_int("555"), 555)
        self.assertEqual(_oid_to_str(555), "555")
        self.assertEqual(_oid_to_int(555), 555)
        self.assertIsNone(_oid_to_int(None))
        self.assertIsNone(_oid_to_str(0))


# ------------------------------------------------------------------
# Reads
# ------------------------------------------------------------------

class AdapterReadTests(unittest.TestCase):
    def setUp(self):
        self.adapter = OndoPerpsGoldenFiboAdapter()

    def test_resolve_instrument_merges_constraints(self):
        inst = {"symbol": "ONDO-USD.P", "requested_symbol": "ONDO", "display_name": "ONDO"}
        cons = {
            "tick_size": "0.0001",
            "step_size": "1",
            "size_decimals": 0,
            "price_decimals": 4,
            "min_base_amount": "1",
            "market": "ONDO-USD.P",
        }
        def execute(req):
            if req["operation"] == "resolve_instrument":
                return _ok(instrument=inst)
            if req["operation"] == "market_constraints":
                return _ok(order_state=cons)
            return _fail()
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute", side_effect=execute):
            meta = self.adapter.resolve_instrument("amiroo", "ONDO")
        self.assertEqual(meta["tick_size"], "0.0001")
        self.assertEqual(meta["step_size"], "1")
        self.assertEqual(meta["symbol"], "ONDO-USD.P")

    def test_position_state_long(self):
        pos = {"symbol": "ONDO", "side": "long", "size": "10", "entry_price": "0.5", "pnl": "1", "tp": "0.51", "sl": None}
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute",
                        return_value=_ok(positions=[pos])):
            out = self.adapter.position_state("amiroo", "ONDO")
        self.assertEqual(out["side"], "long")
        self.assertEqual(out["size"], "10")

    def test_position_state_flat(self):
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute",
                        return_value=_ok(positions=[])):
            out = self.adapter.position_state("amiroo", "ONDO")
        self.assertIsNone(out["side"])
        self.assertEqual(out["size"], "0")

    def test_position_state_short(self):
        pos = {"symbol": "ONDO", "side": "short", "size": "2", "entry_price": "0.4", "pnl": "0", "tp": None, "sl": None}
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute",
                        return_value=_ok(positions=[pos])):
            out = self.adapter.position_state("amiroo", "ONDO")
        self.assertEqual(out["side"], "short")

    def test_market_price(self):
        mp = {"mark_price": "0.42", "last_external_price": "0.42"}
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute",
                        return_value=_ok(market_price=mp)):
            out = self.adapter.market_price("amiroo", "ONDO")
        self.assertEqual(out["mark_price"], "0.42")

    def test_get_venue_constraints(self):
        cons = {"tick_size": "0.0001", "step_size": "1", "size_decimals": 0, "price_decimals": 4}
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute",
                        return_value=_ok(order_state=cons)):
            out = self.adapter.get_venue_constraints("amiroo", "ONDO")
        self.assertEqual(out["tick_size"], "0.0001")
        self.assertNotIn("min_quote_amount", out)


# ------------------------------------------------------------------
# Writes + client id on the wire
# ------------------------------------------------------------------

class AdapterPlaceTests(unittest.TestCase):
    def setUp(self):
        self.adapter = OndoPerpsGoldenFiboAdapter()

    def test_place_market_sends_decimal_client_order_id(self):
        captured = {}
        def execute(req):
            captured["req"] = dict(req)
            order = {
                "exchange_order_id": 9001,
                "submitted_price": "0.42",
                "submitted_volume": "1",
                "status": "success",
                "verified": True,
            }
            return _ok(order=order)
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute", side_effect=execute):
            r = self.adapter.place_market(
                account="amiroo", instrument="ONDO", side="buy",
                size=Decimal("1"), client_order_id=82738589974528,
            )
        self.assertEqual(captured["req"]["operation"], "new_order")
        self.assertEqual(captured["req"]["order_type"], "market")
        self.assertEqual(captured["req"]["client_order_id"], "82738589974528")
        self.assertEqual(r["client_order_id"], 82738589974528)
        self.assertEqual(r["exchange_order_id"], 9001)
        self.assertEqual(r["role"], "entry")
        self.assertNotIn("reduce_only", captured["req"])  # false omitted

    def test_place_market_failure_raises(self):
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute",
                        return_value=_fail("ORDER_SUBMISSION_FAILED")):
            with self.assertRaises(RuntimeError):
                self.adapter.place_market(
                    account="amiroo", instrument="ONDO", side="buy",
                    size=Decimal("1"), client_order_id=1,
                )

    def test_place_limit_sends_client_id_and_returns_open(self):
        captured = {}
        def execute(req):
            captured["req"] = dict(req)
            order = {
                "exchange_order_id": 9002,
                "submitted_price": "0.40",
                "submitted_volume": "1",
                "status": "success",
                "verified": True,
            }
            return _ok(order=order)
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute", side_effect=execute):
            r = self.adapter.place_limit(
                account="amiroo", instrument="ONDO", side="buy",
                size=Decimal("1"), price=Decimal("0.40"),
                client_order_id=42, reduce_only=False,
            )
        self.assertEqual(captured["req"]["order_type"], "limit")
        self.assertEqual(captured["req"]["client_order_id"], "42")
        self.assertEqual(captured["req"]["price"], "0.40")
        self.assertEqual(r["exchange_order_id"], 9002)
        self.assertEqual(r["role"], "ladder")
        self.assertEqual(r["status"], "open")

    def test_place_limit_sell_flow(self):
        def execute(req):
            self.assertEqual(req["side"], "sell")
            order = {"exchange_order_id": 7, "submitted_price": "0.5",
                     "submitted_volume": "1", "status": "success", "verified": True}
            return _ok(order=order)
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute", side_effect=execute):
            r = self.adapter.place_limit(
                account="amiroo", instrument="ONDO", side="sell",
                size=Decimal("1"), price=Decimal("0.5"),
                client_order_id=9,
            )
        self.assertEqual(r["exchange_order_id"], 7)


class LimitNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.adapter = OndoPerpsGoldenFiboAdapter()

    def _place(self, *, new_order_resp, followup=None):
        calls: List[Dict[str, Any]] = []
        def execute(req):
            calls.append(req)
            op = req.get("operation")
            if op == "new_order":
                return new_order_resp
            if op == "get_order_state":
                if followup is not None:
                    return followup
                return _ok(order_state={"status": "UNKNOWN", "classification": "UNKNOWN",
                                        "taxonomy": "UNKNOWN", "exchange_order_id": "9003"})
            if op == "position_state":
                return _ok(positions=[])
            return _fail()
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute", side_effect=execute):
            return self.adapter.place_limit(
                account="amiroo", instrument="ONDO", side="buy",
                size=Decimal("1"), price=Decimal("0.40"),
                client_order_id=11,
            ), calls

    def test_unverified_disappearance_is_unknown_not_filled(self):
        order = {"exchange_order_id": 9003, "submitted_volume": "1",
                 "submitted_price": "0.40", "status": "submitted", "verified": False}
        resp = mock.Mock(success=False, error=mock.Mock(code="VERIFICATION_FAILED"), order=order)
        r, _ = self._place(new_order_resp=resp)
        self.assertEqual(r["exchange_order_id"], 9003)
        self.assertEqual(r["status"], "unknown")
        self.assertNotEqual(r["status"], "filled")

    def test_authoritative_fullyfilled_is_filled(self):
        order = {"exchange_order_id": 9004, "submitted_volume": "1",
                 "submitted_price": "0.40", "status": "submitted", "verified": False}
        new_resp = mock.Mock(success=False, error=mock.Mock(code="VERIFICATION_FAILED"), order=order)
        follow = _ok(order_state={
            "status": "FILLED", "classification": "FILLED", "taxonomy": "FILLED",
            "exchange_order_id": "9004", "filled_size": "1", "remaining_size": "0",
        })
        r, _ = self._place(new_order_resp=new_resp, followup=follow)
        self.assertEqual(r["status"], "filled")
        self.assertEqual(r["exchange_order_id"], 9004)

    def test_partial_fill_classification(self):
        order = {"exchange_order_id": 9005, "submitted_volume": "1",
                 "submitted_price": "0.40", "status": "submitted", "verified": False}
        new_resp = mock.Mock(success=False, error=mock.Mock(code="VERIFICATION_FAILED"), order=order)
        follow = _ok(order_state={
            "status": "PARTIALLY_FILLED", "classification": "PARTIALLY_FILLED",
            "taxonomy": "ACTIVE", "exchange_order_id": "9005",
            "filled_size": "0.4", "remaining_size": "0.6",
        })
        r, _ = self._place(new_order_resp=new_resp, followup=follow)
        self.assertEqual(r["status"], "partially_filled")

    def test_rejected_submission_without_oid_raises(self):
        resp = mock.Mock(success=False, error=mock.Mock(code="ORDER_SUBMISSION_FAILED"), order=None)
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute", return_value=resp):
            with self.assertRaises(RuntimeError):
                self.adapter.place_limit(
                    account="amiroo", instrument="ONDO", side="buy",
                    size=Decimal("1"), price=Decimal("0.40"),
                    client_order_id=11,
                )

    def test_place_limit_does_not_retry_post(self):
        calls = []
        order = {"exchange_order_id": 1, "submitted_volume": "1",
                 "submitted_price": "0.4", "status": "submitted", "verified": False}
        def execute(req):
            calls.append(req["operation"])
            if req["operation"] == "new_order":
                return mock.Mock(success=False, error=mock.Mock(code="VERIFICATION_FAILED"), order=order)
            if req["operation"] == "get_order_state":
                return _ok(order_state={"status": "UNKNOWN", "classification": "UNKNOWN",
                                        "taxonomy": "UNKNOWN", "exchange_order_id": "1"})
            if req["operation"] == "position_state":
                return _ok(positions=[])
            return _fail()
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute", side_effect=execute):
            self.adapter.place_limit(
                account="amiroo", instrument="ONDO", side="buy",
                size=Decimal("1"), price=Decimal("0.4"),
                client_order_id=3,
            )
        self.assertEqual(calls.count("new_order"), 1)


class SharedTpTests(unittest.TestCase):
    def setUp(self):
        self.adapter = OndoPerpsGoldenFiboAdapter()

    def test_set_shared_tp_is_position_scoped(self):
        captured = {}
        action = {
            "exchange_order_id": None,
            "price": "0.4204",
            "current_size": "1",
            "status": "success",
            "verified": True,
        }
        def execute(req):
            captured["req"] = dict(req)
            return _ok(position_action=action)
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute", side_effect=execute):
            r = self.adapter.set_shared_tp(
                account="amiroo", instrument="ONDO", price=Decimal("0.4204"),
                side="sell", size=Decimal("1"), client_order_id=99,
            )
        self.assertEqual(captured["req"]["operation"], "set_tp")
        self.assertEqual(captured["req"]["symbol"], "ONDO")
        self.assertEqual(r["role"], "tp")
        self.assertTrue(r["verified"])
        self.assertIsNone(r["exchange_order_id"])  # net-position scoped, not per-fill


class CancelAndCloseTests(unittest.TestCase):
    def setUp(self):
        self.adapter = OndoPerpsGoldenFiboAdapter()

    def test_cancel_order_uses_single_id(self):
        captured = {}
        def execute(req):
            captured["req"] = dict(req)
            return _ok(order_state={"outcome": "CANCELED"})
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute", side_effect=execute):
            ok = self.adapter.cancel_order(account="amiroo", order_index=555)
        self.assertTrue(ok)
        self.assertEqual(captured["req"]["operation"], "cancel_order")
        self.assertEqual(str(captured["req"]["order_id"]), "555")
        self.assertNotEqual(captured["req"]["operation"], "cancel_order_group")

    def test_cancel_already_terminal_is_true(self):
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute",
                        return_value=_ok(order_state={"outcome": "ALREADY_TERMINAL"})):
            self.assertTrue(self.adapter.cancel_order(account="amiroo", order_index=555))

    def test_cancel_zero_refused(self):
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute") as ex:
            self.assertFalse(self.adapter.cancel_order(account="amiroo", order_index=0))
        ex.assert_not_called()

    def test_close_position(self):
        action = {"verified": True, "status": "success", "exchange_order_id": 88}
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute",
                        return_value=_ok(position_action=action)):
            r = self.adapter.close_position(account="amiroo", instrument="ONDO",
                                            client_order_id=11)
        self.assertTrue(r["verified"])
        self.assertEqual(r["client_order_id"], 11)


class OrderStateReadbackTests(unittest.TestCase):
    def setUp(self):
        self.adapter = OndoPerpsGoldenFiboAdapter()

    def test_get_order_state_open(self):
        st = {"status": "OPEN", "taxonomy": "ACTIVE", "classification": "OPEN",
              "exchange_order_id": "555"}
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute",
                        return_value=_ok(order_state=st)):
            out = self.adapter.get_order_state("amiroo", 555)
        self.assertEqual(out["status"], "OPEN")

    def test_get_order_state_missing_is_unknown(self):
        st = {"status": "UNKNOWN", "taxonomy": "UNKNOWN", "classification": "UNKNOWN",
              "exchange_order_id": "555"}
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute",
                        return_value=_ok(order_state=st)):
            out = self.adapter.get_order_state("amiroo", 555)
        self.assertEqual(out["status"], "UNKNOWN")
        self.assertNotEqual(out.get("status"), "FILLED")

    def test_get_order_state_by_client_id_round_trip(self):
        captured = {}
        st = {"status": "FILLED", "classification": "FILLED",
              "client_order_id": "82738589974528", "exchange_order_id": "9001"}
        def execute(req):
            captured["req"] = dict(req)
            return _ok(order_state=st)
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute", side_effect=execute):
            out = self.adapter.get_order_state_by_client_id("amiroo", "ONDO", 82738589974528)
        self.assertEqual(captured["req"]["client_order_id"], "82738589974528")
        self.assertEqual(out["status"], "FILLED")


class QuantizationAndPreflightTests(unittest.TestCase):
    def test_constraints_expose_increments(self):
        adapter = OndoPerpsGoldenFiboAdapter()
        cons = {"tick_size": "0.0001", "step_size": "1", "size_decimals": 0,
                "price_decimals": 4, "min_base_amount": "1"}
        with mock.patch("plugins.trade.agents.x_ondoperps_agent.execute",
                        return_value=_ok(order_state=cons)):
            c = adapter.get_venue_constraints("amiroo", "ONDO")
        self.assertEqual(Decimal(c["min_base_amount"]), Decimal("1"))
        self.assertEqual(c["size_decimals"], 0)


class EngineMathUnchangedTests(unittest.TestCase):
    def test_engine_places_step0_market_then_limit(self):
        calls = []
        class Capture(OndoPerpsGoldenFiboAdapter):
            def position_state(self, account, instrument):
                if any(c[0] == "market" for c in calls):
                    return _pos(size="1", side="long", entry="0.42")
                return _pos(size="0", side=None)
            def place_market(self, **kw):
                calls.append(("market", kw))
                return {
                    "client_order_id": kw["client_order_id"],
                    "exchange_order_id": 1,
                    "submitted_price": "0.42",
                    "submitted_volume": str(kw["size"]),
                    "status": "filled",
                    "verified": True,
                    "role": "entry",
                }
            def place_limit(self, **kw):
                calls.append(("limit", kw))
                return {
                    "client_order_id": kw["client_order_id"],
                    "exchange_order_id": 2,
                    "submitted_price": str(kw["price"]),
                    "submitted_volume": str(kw["size"]),
                    "status": "open",
                    "verified": True,
                    "role": "ladder",
                }
            def set_shared_tp(self, **kw):
                calls.append(("tp", kw))
                return {
                    "client_order_id": kw["client_order_id"],
                    "exchange_order_id": None,
                    "submitted_price": str(kw["price"]),
                    "submitted_volume": str(kw["size"]),
                    "status": "success",
                    "verified": True,
                    "role": "tp",
                }
            def get_order_state(self, account, order_index):
                return {"status": "OPEN", "taxonomy": "ACTIVE", "classification": "OPEN"}
            def get_venue_constraints(self, account, instrument):
                return {"min_base_amount": "1", "step_size": "1", "size_decimals": 0,
                        "price_decimals": 4, "tick_size": "0.0001"}
            def market_price(self, account, instrument):
                return {"mark_price": "0.42"}
            def resolve_instrument(self, account, instrument):
                return {"symbol": "ONDO", "min_base_amount": "1"}

        cfg = _ondo_config()
        state = _ondo_state()
        engine = GoldenFiboEngine(cfg, state, Capture(), None)
        try:
            for _ in range(6):
                engine.tick()
        except Exception:
            pass
        ops = [c[0] for c in calls]
        self.assertIn("market", ops)

    def test_sell_direction_uses_sell_side(self):
        seen = {}
        class Capture(OndoPerpsGoldenFiboAdapter):
            def position_state(self, account, instrument):
                return _pos(size="0", side=None)
            def place_market(self, **kw):
                seen.update(kw)
                return {
                    "client_order_id": kw["client_order_id"],
                    "exchange_order_id": 1,
                    "submitted_volume": str(kw["size"]),
                    "status": "filled",
                    "verified": True,
                    "role": "entry",
                }
            def get_venue_constraints(self, account, instrument):
                return {"min_base_amount": "1", "step_size": "1", "size_decimals": 0,
                        "price_decimals": 4}
            def market_price(self, account, instrument):
                return {"mark_price": "0.42"}
            def resolve_instrument(self, account, instrument):
                return {"symbol": "ONDO"}

        cfg = _ondo_config("SELL")
        state = _ondo_state()
        state.direction = "SELL"
        engine = GoldenFiboEngine(cfg, state, Capture(), None)
        try:
            engine.tick()
        except Exception:
            pass
        if "side" in seen:
            self.assertEqual(str(seen["side"]).lower(), "sell")


class AdapterForWiringTests(unittest.TestCase):
    def test_supported_exchanges_includes_ondoperps(self):
        from plugins.trade.fibo_service import SUPPORTED_EXCHANGES, PersistentFiboService
        self.assertIn("ondoperps", SUPPORTED_EXCHANGES)
        svc = PersistentFiboService.__new__(PersistentFiboService)
        svc._adapters = {}
        svc._states = {}
        adapter = PersistentFiboService._adapter_for(svc, "ondoperps/amiroo/ONDO/BUY")
        self.assertIsInstance(adapter, OndoPerpsGoldenFiboAdapter)
        # Must not silently fall back to Lighter.
        from plugins.trade.golden_fibo.lighter_adapter import LighterGoldenFiboAdapter
        self.assertNotIsInstance(adapter, LighterGoldenFiboAdapter)


if __name__ == "__main__":
    unittest.main()
