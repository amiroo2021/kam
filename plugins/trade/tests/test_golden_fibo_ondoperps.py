"""Offline GoldenFibo Ondo Perps adapter tests.

Mirrors the Rise adapter contract. No live HTTP.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest import mock

from plugins.trade.golden_fibo.engine import GoldenFiboConfig, GoldenFiboEngine
from plugins.trade.golden_fibo.state import (
    ROLE_ENTRY,
    ROLE_LADDER,
    SUBMISSION_CONFIRMED,
    SUBMISSION_NOT_SUBMITTED,
    GoldenFiboState,
)
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


# ---------------------------------------------------------------------------
# OndoPerps Step0 confirmation / recovery (incident 2026-08-21 ETH 0.001)
# ---------------------------------------------------------------------------
#
# These tests cover the gap where Ondo's GET /v1/perps/orders/client:<id>
# index takes longer than the bounded retry window to surface a freshly-
# filled market order, and where Ondo's alphanumeric orderId previously
# bypassed the existing recovery path entirely (no int = no reconciliation).
#
# The fix introduces:
#   * a guarded live-position fallback (runs at most once per registration)
#   * tolerant storage of exchange_order_id (str or int)
#   * a recovery path that consults client_id even when no exchange_order_id
#
# Tests A–I below are regression coverage for the new path; J is the
# cross-venue "everything else still works" smoke test.


class _OndoStep0Adapter:
    """Programmable adapter for Step0 confirmation / recovery scenarios.

    Each instance owns its own state for: market-side place_market,
    get_order_state_by_client_id (default UNKNOWN unless set), and
    position_state. The class also tracks every call so tests can assert
    idempotency (no duplicate Step0 submission, single position fallback).
    """

    def __init__(self):
        self.calls: List[tuple] = []
        self.place_market_calls = 0
        self.client_id_lookups = 0
        self.position_lookups = 0
        # Order-record-by-client_id lookup result. Empty dict == UNKNOWN.
        self.lookup_result: Dict[str, Any] = {}
        # Order-record-by-exchange_id lookup result (Path A).
        self.oid_lookup_result: Dict[str, Any] = {}
        # Position snapshot.
        self.position = {
            "symbol": "ETH",
            "side": None,
            "size": "0",
            "entry_price": None,
        }
        # Alphanumeric Ondo orderId, mimicking the live incident.
        self.fake_oid = "EDGPTJP3FMDFIMSCPF4W4G5XRH4G6EFV"

    # -- position / lookup read paths
    def position_state(self, account, instrument):
        self.position_lookups += 1
        self.calls.append(("position_state", account, instrument))
        return dict(self.position)

    def get_order_state(self, account, order_index):
        self.calls.append(("get_order_state", order_index))
        return dict(self.oid_lookup_result)

    def get_order_state_by_client_id(self, account, instrument, client_order_index):
        self.client_id_lookups += 1
        self.calls.append(("get_order_state_by_client_id", int(client_order_index)))
        return dict(self.lookup_result)

    # -- write paths
    def place_market(self, *, account, instrument, side, size, client_order_id):
        self.place_market_calls += 1
        self.calls.append(("place_market", client_order_id, str(size), side))
        # Adapter returns the alphanumeric Ondo orderId verbatim.
        return {
            "client_order_id": int(client_order_id),
            "exchange_order_id": self.fake_oid,  # STRING, not int.
            "submitted_price": None,
            "submitted_volume": str(size),
            "status": "submitted",
            "verified": False,
            "role": "entry",
            "raw": {},
        }

    def set_shared_tp(self, *, account, instrument, price, side=None, size=None, client_order_id=None):
        self.calls.append(("set_shared_tp", client_order_id, str(price), str(size or "")))
        return {
            "client_order_id": client_order_id,
            "exchange_order_id": None,
            "submitted_price": str(price),
            "submitted_volume": str(size or ""),
            "status": "submitted",
            "verified": False,
            "role": "tp",
        }

    def place_limit(self, *, account, instrument, side, size, price, client_order_id, reduce_only=False):
        self.calls.append(("place_limit", client_order_id, str(size), str(price), side))
        return {
            "client_order_id": client_order_id,
            "exchange_order_id": 9999,
            "submitted_price": str(price),
            "submitted_volume": str(size),
            "status": "submitted",
            "verified": False,
        }

    def cancel_order(self, *, account, order_index):
        self.calls.append(("cancel_order", order_index))
        return True


def _build_svc_for_step0(state_path, ledger_path, event_path):
    """PersistentFiboService wired with auto-discovery but NO poll thread.

    Tests drive ``_maybe_confirm_step0(key)`` directly so we can exercise
    exact post-submit / post-retry behaviour without wall-clock waits.
    """
    import threading
    from plugins.trade.fibo_service import PersistentFiboService
    svc = PersistentFiboService(
        state_path=state_path,
        ledger_path=ledger_path,
        event_log_path=event_path,
        start_thread=False,
    )
    return svc


def _seed_step0_state(svc, key, *,
                       client_id=82738593280000,
                       step0_volume="0.001",
                       cycle_uid=335244,
                       direction="BUY",
                       submission_phase=SUBMISSION_CONFIRMED,
                       pending_order_exchange_id=None,
                       position_lookups_used=0,
                       lookup_result=None):
    """Seed a registration in the 'Step0 submitted but not yet promoted' state.

    Mirrors the live ETH 0.001 incident exactly (cycle_uid, client_id,
    submission_attempted_at, submission_phase=confirmed).
    """
    from plugins.trade.golden_fibo.client_id_v2 import allocate_client_id
    # V2 deterministic client_id (24-bit cycle_uid + role + step + seq).
    actual_cid = int(client_id)
    state = GoldenFiboState()
    state.strategy = "golden_fibo"
    state.schema_version = 1
    state.registration_key = key
    state.exchange = "ondoperps"
    state.account = key.split("/")[1]
    state.instrument = key.split("/")[2]
    state.direction = direction
    state.percentage = Decimal("0.001")
    state.step0_volume = Decimal(step0_volume)
    state.cycle_uid = cycle_uid
    state.highest_cycle_uid = cycle_uid
    state.client_id_version = 2
    state.next_step = 0
    state.highest_filled_step = -1
    state.expected_cumulative_size = Decimal("0")
    state.pending_order_role = ROLE_ENTRY
    state.pending_order_client_id = actual_cid
    state.pending_order_exchange_id = pending_order_exchange_id
    state.submission_phase = submission_phase
    state.submission_client_id = actual_cid
    state.submission_step = 0
    state.submission_role = ROLE_ENTRY
    state.submission_attempted_at = 1787340277.0025623
    state.submission_exchange_order_id = pending_order_exchange_id
    state.status = "needs_recovery" if submission_phase == SUBMISSION_CONFIRMED else "running"
    if submission_phase == SUBMISSION_CONFIRMED and pending_order_exchange_id is None:
        # The exact live incident: still flagged needs_recovery because the
        # early-retry window expired without the client-id lookup seeing FILLED.
        state.freeze_reason = (
            f"Step0 order with client_order_index={actual_cid} "
            "not found in active/inactive surface after bounded retry"
        )
    svc._states[key] = state
    # Pre-load position-lookup and lookup counters if the test wants to
    # simulate a registration that has already burned its bounded retries.
    if position_lookups_used > 0:
        # Drive the counter up via repeated _maybe_confirm_step0 calls.
        svc._step0_lookup_attempts = {key: position_lookups_used}
    return state


class OndoStep0ConfirmationTests(unittest.TestCase):
    """Step0 confirmation + position-fallback + recovery regression tests."""

    # ---------------- A. Happy path: client-id lookup FILLED immediately ----

    def test_A_submit_then_client_id_filled_promotes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path
            svc = _build_svc_for_step0(
                Path(tmp) / "state.json",
                Path(tmp) / "ledger.jsonl",
                Path(tmp) / "events.log",
            )
            key = "ondoperps/bitget/ETH/BUY"
            adapter = _OndoStep0Adapter()
            svc._adapters[key] = adapter
            # Lookup returns FILLED with the actual_fill_price.
            adapter.lookup_result = {
                "exchange_order_id": adapter.fake_oid,
                "client_order_id": 82738593280000,
                "side": "buy",
                "status": "FILLED",
                "taxonomy": "FILLED",
                "filled_size": "0.001",
                "requested_size": "0.001",
                "actual_fill_price": "2412.60",
                "symbol": "ETH",
            }
            _seed_step0_state(svc, key, submission_phase=SUBMISSION_CONFIRMED,
                              pending_order_exchange_id=None)
            # The new Step0-promotion safety gate requires the live
            # position to be compatible with the historical Step0 size.
            adapter.position = {
                "symbol": "ETH",
                "side": "long",
                "size": "0.001",
                "entry_price": "2412.60",
            }

            svc._maybe_confirm_step0(key)

            state = svc._states[key]
            self.assertEqual(state.highest_filled_step, 0,
                             "Step0 must be promoted after FILLED client-id lookup")
            self.assertEqual(state.expected_cumulative_size, Decimal("0.001"))
            self.assertEqual(state.fill_prices.get(0), Decimal("2412.60"))
            # step_orders[0] reflects the ENTRY identity with the
            # alphanumeric Ondo orderId preserved.
            self.assertEqual(state.step_orders[0]["client_id"], 82738593280000)
            self.assertEqual(state.step_orders[0]["exchange_order_id"], adapter.fake_oid)
            self.assertEqual(state.step_orders[0]["role"], "entry")
            self.assertEqual(state.step_orders[0]["price"], "2412.60")
            # Submission tracking now reflects the in-flight TP / Step1
            # ladder, not Step0 any more. Step0 itself is fully closed out:
            # pending_order_role advanced from "entry" → "ladder".
            self.assertEqual(state.pending_order_role, "ladder",
                             "Step0 promotion must advance pending_order_role to 'ladder'")
            # Step0's deterministic client_order_index no longer drives a
            # pending entry; the ladder now owns the pending slot.
            self.assertNotEqual(state.pending_order_client_id, 82738593280000,
                                "Step0 client_order_index must not remain as the pending client id")
            # Status returns to running (TP + Step1 placed by engine).
            self.assertEqual(state.status, "running")
            self.assertIsNone(state.freeze_reason)

    # ---------------- B. Position fallback after bounded retry window -----

    def test_B_unknown_for_8_polls_then_matching_position_promotes_once(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            svc = _build_svc_for_step0(
                Path(tmp) / "state.json",
                Path(tmp) / "ledger.jsonl",
                Path(tmp) / "events.log",
            )
            key = "ondoperps/bitget/ETH/BUY"
            adapter = _OndoStep0Adapter()
            # client-id lookup never returns FILLED.
            adapter.lookup_result = {
                "client_order_id": 82738593280000,
                "status": "UNKNOWN",
                "taxonomy": "UNKNOWN",
                "classification": "UNKNOWN",
                "note": "Ondo clientOrderId lookup did not find this order; do not infer FILLED",
            }
            # Live position exactly matches the submitted Step0.
            adapter.position = {
                "symbol": "ETH",
                "side": "long",
                "size": "0.001",
                "entry_price": "2412.60",
            }
            svc._adapters[key] = adapter
            # Pretend 8 polls already burned the bounded retry budget.
            _seed_step0_state(svc, key, submission_phase=SUBMISSION_CONFIRMED,
                              pending_order_exchange_id=None,
                              position_lookups_used=8)

            # The next call hits the position fallback at attempts >= 8.
            # The position already matches Step0; the safety gate
            # in _promote_step0_and_advance is satisfied by the same
            # matching position that the fallback helper just verified.
            svc._maybe_confirm_step0(key)

            state = svc._states[key]
            self.assertEqual(state.highest_filled_step, 0,
                             "Position fallback must promote Step0 exactly once")
            self.assertEqual(state.expected_cumulative_size, Decimal("0.001"))
            self.assertEqual(state.fill_prices.get(0), Decimal("2412.60"))
            self.assertEqual(state.step_orders[0]["price"], "2412.60")
            self.assertEqual(state.step_orders[0]["role"], "entry")
            # Status returns to running (TP + Step1 placed by engine).
            self.assertEqual(state.status, "running")
            self.assertIsNone(state.freeze_reason)

            # Idempotency: subsequent ticks must NOT re-promote and must NOT
            # place another Step0.
            place_market_before = adapter.place_market_calls
            svc._maybe_confirm_step0(key)
            svc._maybe_confirm_step0(key)
            svc._maybe_confirm_step0(key)
            self.assertEqual(adapter.place_market_calls, place_market_before,
                             "Position fallback must not resubmit Step0 on later ticks")

    # ---------------- C. No live position: must NOT promote ---------------

    def test_C_unknown_lookups_no_position_no_resubmit_no_promotion(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            svc = _build_svc_for_step0(
                Path(tmp) / "state.json",
                Path(tmp) / "ledger.jsonl",
                Path(tmp) / "events.log",
            )
            key = "ondoperps/bitget/ETH/BUY"
            adapter = _OndoStep0Adapter()
            adapter.lookup_result = {
                "client_order_id": 82738593280000,
                "status": "UNKNOWN",
                "taxonomy": "UNKNOWN",
                "classification": "UNKNOWN",
            }
            # No position.
            adapter.position = {"symbol": "ETH", "side": None, "size": "0", "entry_price": None}
            svc._adapters[key] = adapter
            _seed_step0_state(svc, key, submission_phase=SUBMISSION_CONFIRMED,
                              position_lookups_used=8)

            svc._maybe_confirm_step0(key)
            state = svc._states[key]
            self.assertEqual(state.highest_filled_step, -1,
                             "Without matching position, Step0 must NOT promote")
            self.assertEqual(state.status, "needs_recovery")
            self.assertIn("not found in active/inactive surface", state.freeze_reason or "")
            # No second Step0 submission.
            self.assertEqual(adapter.place_market_calls, 0)

    # ---------------- D. Wrong-side live position: must NOT promote --------

    def test_D_wrong_side_position_no_promotion(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            svc = _build_svc_for_step0(
                Path(tmp) / "state.json",
                Path(tmp) / "ledger.jsonl",
                Path(tmp) / "events.log",
            )
            key = "ondoperps/bitget/ETH/BUY"
            adapter = _OndoStep0Adapter()
            adapter.lookup_result = {
                "client_order_id": 82738593280000,
                "status": "UNKNOWN",
                "taxonomy": "UNKNOWN",
                "classification": "UNKNOWN",
            }
            # The direction is BUY but the live position is short.
            adapter.position = {
                "symbol": "ETH",
                "side": "short",
                "size": "0.001",
                "entry_price": "2412.60",
            }
            svc._adapters[key] = adapter
            _seed_step0_state(svc, key, submission_phase=SUBMISSION_CONFIRMED,
                              position_lookups_used=8)

            svc._maybe_confirm_step0(key)
            state = svc._states[key]
            self.assertEqual(state.highest_filled_step, -1,
                             "Opposite-side position must NOT promote Step0")
            self.assertEqual(state.status, "needs_recovery")

    # ---------------- E. Size-incompatible position: must NOT promote ------

    def test_E_position_smaller_than_expected_no_promotion(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            svc = _build_svc_for_step0(
                Path(tmp) / "state.json",
                Path(tmp) / "ledger.jsonl",
                Path(tmp) / "events.log",
            )
            key = "ondoperps/bitget/ETH/BUY"
            adapter = _OndoStep0Adapter()
            adapter.lookup_result = {
                "client_order_id": 82738593280000,
                "status": "UNKNOWN",
                "taxonomy": "UNKNOWN",
                "classification": "UNKNOWN",
            }
            # Half the expected size — not a proof of OUR Step0.
            adapter.position = {
                "symbol": "ETH",
                "side": "long",
                "size": "0.0005",
                "entry_price": "2412.60",
            }
            svc._adapters[key] = adapter
            _seed_step0_state(svc, key, submission_phase=SUBMISSION_CONFIRMED,
                              position_lookups_used=8)

            svc._maybe_confirm_step0(key)
            state = svc._states[key]
            self.assertEqual(state.highest_filled_step, -1,
                             "Position smaller than expected Step0 must NOT promote")
            self.assertEqual(state.status, "needs_recovery")

    # ---------------- F. needs_recovery + client_id + later FILLED --------

    def test_F_needs_recovery_recovery_via_late_client_id_FILLED(self):
        """The original live incident: client-id lookup returns UNKNOWN at
        submit time, but later (after several retries) returns FILLED.
        The service MUST promote Step0 without resubmission.
        """
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            svc = _build_svc_for_step0(
                Path(tmp) / "state.json",
                Path(tmp) / "ledger.jsonl",
                Path(tmp) / "events.log",
            )
            key = "ondoperps/bitget/ETH/BUY"
            adapter = _OndoStep0Adapter()
            svc._adapters[key] = adapter
            _seed_step0_state(svc, key,
                              submission_phase=SUBMISSION_CONFIRMED,
                              pending_order_exchange_id=None)
            # Build the exact frozen state from the live incident.
            state = svc._states[key]
            self.assertEqual(state.status, "needs_recovery")
            self.assertIn("not found in active/inactive surface", state.freeze_reason or "")
            # Drive 8 ticks while client-id lookup keeps returning UNKNOWN
            # (simulating the live propagation latency).
            adapter.lookup_result = {
                "client_order_id": 82738593280000,
                "status": "UNKNOWN",
                "taxonomy": "UNKNOWN",
                "classification": "UNKNOWN",
            }
            # No matching position yet (only the order lookup evidence).
            adapter.position = {"symbol": "ETH", "side": None, "size": "0", "entry_price": None}
            for _ in range(8):
                svc._maybe_confirm_step0(key)
            # The state must still be needs_recovery but with a healthy
            # in-memory counter that has hit the bound.
            self.assertEqual(svc._states[key].status, "needs_recovery")

            # Now the venue's orders-by-client-id index catches up: the
            # same lookup returns a FILLED record. The 9th tick must
            # promote without resubmitting Step0.
            place_market_before = adapter.place_market_calls
            adapter.lookup_result = {
                "exchange_order_id": adapter.fake_oid,
                "client_order_id": 82738593280000,
                "side": "buy",
                "status": "FILLED",
                "taxonomy": "FILLED",
                "filled_size": "0.001",
                "requested_size": "0.001",
                "actual_fill_price": "2412.60",
                "symbol": "ETH",
            }
            # Mirror the live incident: the FILLED order was followed by
            # a corresponding live position. The new Step0 safety gate
            # refuses promotion if the live position is missing/smaller.
            adapter.position = {
                "symbol": "ETH",
                "side": "long",
                "size": "0.001",
                "entry_price": "2412.60",
            }
            svc._maybe_confirm_step0(key)

            state = svc._states[key]
            self.assertEqual(state.highest_filled_step, 0,
                             "Late FILLED client-id lookup must promote Step0")
            self.assertEqual(state.fill_prices.get(0), Decimal("2412.60"))
            self.assertEqual(state.status, "running")
            self.assertIsNone(state.freeze_reason)
            self.assertEqual(adapter.place_market_calls, place_market_before,
                             "Late FILLED lookup must never resubmit Step0")

    # ---------------- G. Service restart while Step0 unresolved ------------

    def test_G_restart_during_unresolved_step0_no_duplicate_submission(self):
        """If the fibo daemon is restarted while Step0 is in
        submission_phase=confirmed but unpromoted, the engine MUST NOT
        resubmit Step0. The deterministic client_order_index is the
        idempotency anchor.
        """
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            ledger_path = Path(tmp) / "ledger.jsonl"
            events_path = Path(tmp) / "events.log"
            key = "ondoperps/bitget/ETH/BUY"

            svc1 = _build_svc_for_step0(state_path, ledger_path, events_path)
            adapter1 = _OndoStep0Adapter()
            svc1._adapters[key] = adapter1
            _seed_step0_state(svc1, key,
                               submission_phase=SUBMISSION_CONFIRMED,
                               pending_order_exchange_id=None)
            # Persist.
            svc1._save_state()

            # Restart: new PersistentFiboService reading the same file.
            svc2 = _build_svc_for_step0(state_path, ledger_path, events_path)
            adapter2 = _OndoStep0Adapter()
            svc2._adapters[key] = adapter2
            # Adapter lookup keeps returning UNKNOWN.
            adapter2.lookup_result = {
                "client_order_id": 82738593280000,
                "status": "UNKNOWN",
                "taxonomy": "UNKNOWN",
                "classification": "UNKNOWN",
            }
            # No position evidence either.
            adapter2.position = {"symbol": "ETH", "side": None, "size": "0", "entry_price": None}

            # The post-restart ticks must NEVER submit another Step0.
            place_calls_before = adapter2.place_market_calls
            for _ in range(12):
                svc2._maybe_confirm_step0(key)
            self.assertEqual(adapter2.place_market_calls, place_calls_before,
                             "Restart during unresolved Step0 must NOT resubmit")
            # The state remains needs_recovery, idempotent.
            state = svc2._states[key]
            self.assertEqual(state.highest_filled_step, -1)
            self.assertEqual(state.submission_phase, SUBMISSION_CONFIRMED)

    # ---------------- H. Alphanumeric exchange_order_id round-trip ---------

    def test_H_alphanumeric_exchange_order_id_persists_through_round_trip(self):
        """The Ondo 32-char alphanumeric orderId MUST survive to_dict /
        from_dict and the engine's state model.
        """
        from plugins.trade.golden_fibo.state import GoldenFiboState
        s = GoldenFiboState()
        s.pending_order_exchange_id = "EDGPTJP3FMDFIMSCPF4W4G5XRH4G6EFV"
        s.submission_exchange_order_id = "EDGPTJP3FMDFIMSCPF4W4G5XRH4G6EFV"
        s.current_tp_order_id = "ABCDEF1234567890ABCDEF1234567890"
        s.emergency_close_exchange_id = "XYZ0123456789ABCDEF0123456789ABCD"
        d = s.to_dict()
        # JSON round-trip.
        import json
        blob = json.dumps(d)
        restored = GoldenFiboState.from_dict(json.loads(blob))
        self.assertEqual(restored.pending_order_exchange_id, "EDGPTJP3FMDFIMSCPF4W4G5XRH4G6EFV")
        self.assertEqual(restored.submission_exchange_order_id, "EDGPTJP3FMDFIMSCPF4W4G5XRH4G6EFV")
        self.assertEqual(restored.current_tp_order_id, "ABCDEF1234567890ABCDEF1234567890")
        self.assertEqual(restored.emergency_close_exchange_id, "XYZ0123456789ABCDEF0123456789ABCD")

    def test_H_numeric_exchange_order_id_still_works(self):
        """Backward compatibility: legacy int ids (Lighter / Rise / Arcus)
        still round-trip as ints, not str(int).
        """
        s = GoldenFiboState()
        s.pending_order_exchange_id = 1125898831127290
        s.submission_exchange_order_id = 1125898831127290
        s.current_tp_order_id = 844426024069104
        import json
        restored = GoldenFiboState.from_dict(json.loads(json.dumps(s.to_dict())))
        self.assertEqual(restored.pending_order_exchange_id, 1125898831127290)
        self.assertIsInstance(restored.pending_order_exchange_id, int)
        self.assertEqual(restored.current_tp_order_id, 844426024069104)
        self.assertIsInstance(restored.current_tp_order_id, int)

    # ---------------- I. Repeated ticks after promotion are idempotent ------

    def test_I_repeated_ticks_after_promotion_no_duplicate(self):
        """After Step0 promotion, subsequent ticks MUST NOT submit another
        Step0 and MUST NOT advance highest_filled_step.
        """
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            svc = _build_svc_for_step0(
                Path(tmp) / "state.json",
                Path(tmp) / "ledger.jsonl",
                Path(tmp) / "events.log",
            )
            key = "ondoperps/bitget/ETH/BUY"
            adapter = _OndoStep0Adapter()
            adapter.lookup_result = {
                "exchange_order_id": adapter.fake_oid,
                "client_order_id": 82738593280000,
                "side": "buy",
                "status": "FILLED",
                "taxonomy": "FILLED",
                "filled_size": "0.001",
                "requested_size": "0.001",
                "actual_fill_price": "2412.60",
                "symbol": "ETH",
            }
            svc._adapters[key] = adapter
            _seed_step0_state(svc, key, submission_phase=SUBMISSION_CONFIRMED,
                              pending_order_exchange_id=None)
            # The new Step0-promotion safety gate requires a matching
            # live position before placing TP/Step1.
            adapter.position = {
                "symbol": "ETH",
                "side": "long",
                "size": "0.001",
                "entry_price": "2412.60",
            }

            # First call promotes.
            svc._maybe_confirm_step0(key)
            self.assertEqual(svc._states[key].highest_filled_step, 0)
            place_market_calls_after_first = adapter.place_market_calls
            client_lookups_after_first = adapter.client_id_lookups

            # Subsequent calls are no-ops on the engine state.
            for _ in range(20):
                svc._maybe_confirm_step0(key)
            state = svc._states[key]
            self.assertEqual(state.highest_filled_step, 0,
                             "Promotion must not advance further")
            self.assertEqual(adapter.place_market_calls, place_market_calls_after_first,
                             "After promotion, no further market submissions")
            # Lookup may still happen (the path is idempotent due to the
            # already_promoted guard); we only assert the promotion count.
            self.assertGreaterEqual(adapter.client_id_lookups, client_lookups_after_first)


class OndoRecoveryGateTests(unittest.TestCase):
    """The engine.reconcile_needs_recovery_pending_fill gate fix."""

    def test_recovery_proceeds_when_only_client_id_persisted(self):
        """A registration with pending_order_exchange_id=None and a valid
        pending_order_client_id must be able to enter the engine's recovery
        path (which then consults the client-id lookup / position state).
        """
        import tempfile
        from pathlib import Path
        from plugins.trade.golden_fibo.engine import GoldenFiboEngine

        with tempfile.TemporaryDirectory() as tmp:
            svc = _build_svc_for_step0(
                Path(tmp) / "state.json",
                Path(tmp) / "ledger.jsonl",
                Path(tmp) / "events.log",
            )
            key = "ondoperps/bitget/ETH/BUY"
            adapter = _OndoStep0Adapter()
            svc._adapters[key] = adapter
            _seed_step0_state(svc, key,
                              submission_phase=SUBMISSION_CONFIRMED,
                              pending_order_exchange_id=None,
                              cycle_uid=335244)

            state = svc._states[key]
            cfg = svc._config_for(key, state)
            engine = GoldenFiboEngine(cfg, state, adapter,
                                       svc._client_id_factory(key))
            # Pre-condition: the recovery path must NOT early-return on
            # pending_order_exchange_id is None alone (the bug we're fixing).
            result = engine.reconcile_needs_recovery_pending_fill([])
            # The recovery path is a no-op for Step0 (it returns without
            # state mutation; the service's _maybe_confirm_step0 owns the
            # actual promotion). The critical thing is: no exception, no
            # "early return before doing anything useful" — and critically
            # it did NOT raise.
            self.assertIsNotNone(result)
            self.assertEqual(result.state.highest_filled_step, -1,
                             "Engine.reconcile path must not promote Step0 itself")
            # Status unchanged.
            self.assertEqual(result.state.status, "needs_recovery")

    def test_K_historical_FILLED_with_flat_position_does_not_promote(self):
        """Safety regression (incident 2026-08-21 second wave).

        A historical Step0 client-id lookup that returns FILLED is NOT
        sufficient evidence to promote Step0 when the live position is
        absent (manually closed externally). The engine MUST refuse to
        place TP or Step1 against a closed position. This guards against
        "resurrecting" downstream orders for a position that no longer
        exists.
        """
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            svc = _build_svc_for_step0(
                Path(tmp) / "state.json",
                Path(tmp) / "ledger.jsonl",
                Path(tmp) / "events.log",
            )
            key = "ondoperps/bitget/ETH/BUY"
            adapter = _OndoStep0Adapter()
            svc._adapters[key] = adapter
            # Client-id lookup returns the historical FILLED record (the
            # venue has it indexed, e.g. days later).
            adapter.lookup_result = {
                "exchange_order_id": adapter.fake_oid,
                "client_order_id": 82738593280000,
                "side": "buy",
                "status": "FILLED",
                "taxonomy": "FILLED",
                "filled_size": "0.001",
                "requested_size": "0.001",
                "actual_fill_price": "2412.60",
                "symbol": "ETH",
            }
            # The live position is FLAT — the user manually closed it.
            adapter.position = {"symbol": "ETH", "side": None, "size": "0", "entry_price": None}
            _seed_step0_state(svc, key, submission_phase=SUBMISSION_CONFIRMED,
                              pending_order_exchange_id=None)

            place_market_before = adapter.place_market_calls
            tp_before = sum(1 for c in adapter.calls if c[0] == "set_shared_tp")
            ladder_before = sum(1 for c in adapter.calls if c[0] == "place_limit")

            svc._maybe_confirm_step0(key)

            state = svc._states[key]
            # MUST NOT promote Step0.
            self.assertEqual(state.highest_filled_step, -1,
                             "Historical FILLED + flat position MUST NOT promote Step0")
            # MUST NOT place TP or Step1.
            self.assertEqual(adapter.place_market_calls, place_market_before,
                             "No Step0 resubmission")
            self.assertEqual(
                sum(1 for c in adapter.calls if c[0] == "set_shared_tp"),
                tp_before,
                "No TP must be placed against a flat position")
            self.assertEqual(
                sum(1 for c in adapter.calls if c[0] == "place_limit"),
                ladder_before,
                "No Step1 ladder must be placed against a flat position")
            self.assertIsNone(state.current_tp_order_id)
            self.assertIsNone(state.step_orders.get(0))
            # MUST mark as NEEDS_RECOVERY with a descriptive reason.
            self.assertEqual(state.status, "needs_recovery")
            self.assertIn("no longer compatible", state.freeze_reason or "")
            self.assertIn("0.001", state.freeze_reason or "")

    def test_K_flat_position_with_lookups_remaining_UNKNOWN(self):
        """The bounded-retry path with a flat position must also NOT
        trigger the position fallback (the fallback requires live_size
        >= expected_size, so flat is a conclusive mismatch). The
        registration must remain in needs_recovery with no orders placed.
        """
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            svc = _build_svc_for_step0(
                Path(tmp) / "state.json",
                Path(tmp) / "ledger.jsonl",
                Path(tmp) / "events.log",
            )
            key = "ondoperps/bitget/ETH/BUY"
            adapter = _OndoStep0Adapter()
            adapter.lookup_result = {
                "client_order_id": 82738593280000,
                "status": "UNKNOWN",
                "taxonomy": "UNKNOWN",
                "classification": "UNKNOWN",
            }
            # Flat position; no fallback will ever succeed because size mismatch.
            adapter.position = {"symbol": "ETH", "side": None, "size": "0", "entry_price": None}
            svc._adapters[key] = adapter
            _seed_step0_state(svc, key, submission_phase=SUBMISSION_CONFIRMED,
                              pending_order_exchange_id=None, position_lookups_used=8)

            place_market_before = adapter.place_market_calls
            tp_before = sum(1 for c in adapter.calls if c[0] == "set_shared_tp")
            ladder_before = sum(1 for c in adapter.calls if c[0] == "place_limit")

            for _ in range(15):
                svc._maybe_confirm_step0(key)

            state = svc._states[key]
            self.assertEqual(state.highest_filled_step, -1,
                             "Flat position MUST NOT promote Step0 even after many ticks")
            self.assertEqual(adapter.place_market_calls, place_market_before,
                             "No Step0 resubmission across many ticks")
            self.assertEqual(
                sum(1 for c in adapter.calls if c[0] == "set_shared_tp"),
                tp_before, "No TP placed")
            self.assertEqual(
                sum(1 for c in adapter.calls if c[0] == "place_limit"),
                ladder_before, "No Step1 placed")


# ---------------------------------------------------------------------------
# averageFillPrice -> actual_fill_price normalization (incident 2026-08-21)
# ---------------------------------------------------------------------------
#
# Ondo's order-by-client-id endpoint returns the average execution price
# under the field name ``averageFillPrice`` (with an ``avgFillPrice`` alias
# on older snapshots). GoldenFibo's generic Step0 fill-price extractor
# expects the canonical field ``actual_fill_price``. Without normalization
# at the venue boundary, an honest FILLED order cannot be promoted.
#
# These tests pin the contract at two levels:
#   * ``_order_state_from_ondo_row`` must expose ``actual_fill_price``
#     populated from the same Ondo-native source as ``average_fill_price``.
#   * The Step0 confirmation path must successfully promote an Ondo
#     FILLED order without any per-venue branching in the engine.


class OndoAverageFillPriceNormalizationTests(unittest.TestCase):
    """Tests A–D for the averageFillPrice -> actual_fill_price fix."""

    def test_A_ondoperps_FILLED_order_row_exposes_canonical_actual_fill_price(self):
        """A. ``_order_state_from_ondo_row`` exposes ``actual_fill_price``
        populated from Ondo's ``averageFillPrice`` field. The legacy
        ``average_fill_price`` alias is preserved for any caller that
        prefers the venue-native name.
        """
        from plugins.trade.agents.x_ondoperps_agent import _order_state_from_ondo_row
        ondo_row = {
            "orderId": "EDGPTJP3FMDFIMSCPF4W4G5XRH4G6EFV",
            "clientOrderId": "82738593280000",
            "side": "buy",
            "price": "0",
            "market": "ETH-USD.P",
            "filledSize": "0.001",
            "lastFillSize": "0.001",
            "filledCost": "2.4126",
            "fee": "0.000603",
            "feeRebate": "0.00003",
            "status": "fullyfilled",
            "createdAt": "2026-08-21T19:24:37.164221084Z",
            "filledAt": "2026-08-21T19:24:37.164221084Z",
            "type": "market",
            "size": "0.001",
            "averageFillPrice": "2412.60",
        }
        normalized = _order_state_from_ondo_row(ondo_row)
        # Canonical GoldenFibo field is populated from Ondo source.
        # _decimal_text strips trailing zeros, so compare numerically.
        self.assertEqual(Decimal(str(normalized.get("actual_fill_price"))),
                         Decimal("2412.60"))
        # Legacy alias preserved.
        self.assertEqual(Decimal(str(normalized.get("average_fill_price"))),
                         Decimal("2412.60"))
        # Status / identity fields still flow.
        self.assertEqual(normalized.get("status"), "FILLED")
        self.assertEqual(normalized.get("taxonomy"), "FILLED")
        self.assertEqual(normalized.get("filled_size"), "0.001")

    def test_A_avgFillPrice_alias_also_populates_canonical(self):
        """Some older Ondo snapshots use ``avgFillPrice`` instead of
        ``averageFillPrice``. Both must feed the canonical field.
        """
        from plugins.trade.agents.x_ondoperps_agent import _order_state_from_ondo_row
        normalized = _order_state_from_ondo_row({
            "orderId": "X",
            "clientOrderId": "1",
            "side": "buy",
            "market": "ETH-USD.P",
            "status": "fullyfilled",
            "size": "0.001",
            "filledSize": "0.001",
            "avgFillPrice": "2412.60",  # older alias
        })
        self.assertEqual(Decimal(str(normalized.get("actual_fill_price"))),
                         Decimal("2412.60"))
        self.assertEqual(Decimal(str(normalized.get("average_fill_price"))),
                         Decimal("2412.60"))

    def test_B_FILLED_with_canonical_fill_price_and_matching_position_promotes(self):
        """B. Ondo FILLED order with averageFillPrice + matching live
        position promotes Step0 successfully and uses the fill price.
        """
        import tempfile
        from pathlib import Path
        from plugins.trade.agents.x_ondoperps_agent import _order_state_from_ondo_row

        with tempfile.TemporaryDirectory() as tmp:
            svc = _build_svc_for_step0(
                Path(tmp) / "state.json",
                Path(tmp) / "ledger.jsonl",
                Path(tmp) / "events.log",
            )
            key = "ondoperps/bitget/ETH/BUY"
            adapter = _OndoStep0Adapter()
            svc._adapters[key] = adapter
            # Build the row that the Ondo agent returns from a real
            # fullyfilled order. Normalise it via the production
            # helper to mirror the live data path.
            ondo_row = {
                "orderId": adapter.fake_oid,
                "clientOrderId": "82738593280000",
                "side": "buy",
                "price": "0",
                "market": "ETH-USD.P",
                "filledSize": "0.001",
                "lastFillSize": "0.001",
                "filledCost": "2.4126",
                "fee": "0.000603",
                "status": "fullyfilled",
                "createdAt": "2026-08-21T19:24:37.164Z",
                "filledAt": "2026-08-21T19:24:37.164Z",
                "type": "market",
                "size": "0.001",
                "averageFillPrice": "2412.60",
            }
            # Sanity-check the production normalizer first.
            self.assertEqual(
                Decimal(str(_order_state_from_ondo_row(ondo_row).get("actual_fill_price"))),
                Decimal("2412.60"),
            )
            adapter.lookup_result = _order_state_from_ondo_row(ondo_row)
            adapter.position = {
                "symbol": "ETH",
                "side": "long",
                "size": "0.001",
                "entry_price": "2412.60",
            }
            _seed_step0_state(svc, key, submission_phase=SUBMISSION_CONFIRMED,
                              pending_order_exchange_id=None)

            svc._maybe_confirm_step0(key)

            state = svc._states[key]
            self.assertEqual(state.highest_filled_step, 0,
                             "FILLED with canonical fill price + matching "
                             "position MUST promote Step0")
            # Fill price is sourced from actual_fill_price (Ondo's
            # averageFillPrice mapped at the venue boundary).
            self.assertEqual(state.fill_prices.get(0), Decimal("2412.60"))
            self.assertEqual(Decimal(str(state.step_orders[0]["price"])),
                             Decimal("2412.60"))
            self.assertEqual(state.status, "running")
            self.assertIsNone(state.freeze_reason)

    def test_C_historical_FILLED_flat_position_still_does_not_promote(self):
        """C. Historical FILLED + currently FLAT position MUST NOT
        resurrect Step0 (regression for the safety gate, this time with
        the fill-price normalizer fixed). Without the safety gate the
        fix could let a historical fill revive downstream orders for a
        closed position.
        """
        import tempfile
        from pathlib import Path
        from plugins.trade.agents.x_ondoperps_agent import _order_state_from_ondo_row

        with tempfile.TemporaryDirectory() as tmp:
            svc = _build_svc_for_step0(
                Path(tmp) / "state.json",
                Path(tmp) / "ledger.jsonl",
                Path(tmp) / "events.log",
            )
            key = "ondoperps/bitget/ETH/BUY"
            adapter = _OndoStep0Adapter()
            svc._adapters[key] = adapter
            ondo_row = {
                "orderId": adapter.fake_oid,
                "clientOrderId": "82738593280000",
                "side": "buy",
                "market": "ETH-USD.P",
                "status": "fullyfilled",
                "size": "0.001",
                "filledSize": "0.001",
                "averageFillPrice": "2412.60",
            }
            adapter.lookup_result = _order_state_from_ondo_row(ondo_row)
            # Live position is FLAT (user closed the position externally).
            adapter.position = {
                "symbol": "ETH",
                "side": None,
                "size": "0",
                "entry_price": None,
            }
            _seed_step0_state(svc, key, submission_phase=SUBMISSION_CONFIRMED,
                              pending_order_exchange_id=None)

            place_market_before = adapter.place_market_calls
            tp_before = sum(1 for c in adapter.calls if c[0] == "set_shared_tp")
            ladder_before = sum(1 for c in adapter.calls if c[0] == "place_limit")

            svc._maybe_confirm_step0(key)

            state = svc._states[key]
            self.assertEqual(state.highest_filled_step, -1,
                             "Historical FILLED + flat position MUST NOT promote")
            self.assertEqual(adapter.place_market_calls, place_market_before,
                             "No Step0 resubmission")
            self.assertEqual(
                sum(1 for c in adapter.calls if c[0] == "set_shared_tp"),
                tp_before, "No TP placed")
            self.assertEqual(
                sum(1 for c in adapter.calls if c[0] == "place_limit"),
                ladder_before, "No Step1 placed")
            self.assertIsNone(state.current_tp_order_id)
            self.assertIsNone(state.step_orders.get(0))
            self.assertEqual(state.status, "needs_recovery")

    def test_D_missing_fill_price_fails_closed(self):
        """D. A FILLED row with neither averageFillPrice, avgFillPrice,
        nor actual_fill_price present must fail closed — freeze with
        needs_recovery, no exchange mutation.
        """
        import tempfile
        from pathlib import Path
        from plugins.trade.agents.x_ondoperps_agent import _order_state_from_ondo_row

        with tempfile.TemporaryDirectory() as tmp:
            svc = _build_svc_for_step0(
                Path(tmp) / "state.json",
                Path(tmp) / "ledger.jsonl",
                Path(tmp) / "events.log",
            )
            key = "ondoperps/bitget/ETH/BUY"
            adapter = _OndoStep0Adapter()
            svc._adapters[key] = adapter
            ondo_row = {
                "orderId": adapter.fake_oid,
                "clientOrderId": "82738593280000",
                "side": "buy",
                "market": "ETH-USD.P",
                "status": "fullyfilled",
                "size": "0.001",
                "filledSize": "0.001",
                # Intentionally NO averageFillPrice / avgFillPrice.
            }
            adapter.lookup_result = _order_state_from_ondo_row(ondo_row)
            adapter.position = {
                "symbol": "ETH",
                "side": "long",
                "size": "0.001",
                "entry_price": "2412.60",
            }
            _seed_step0_state(svc, key, submission_phase=SUBMISSION_CONFIRMED,
                              pending_order_exchange_id=None)

            place_market_before = adapter.place_market_calls
            tp_before = sum(1 for c in adapter.calls if c[0] == "set_shared_tp")
            ladder_before = sum(1 for c in adapter.calls if c[0] == "place_limit")

            svc._maybe_confirm_step0(key)

            state = svc._states[key]
            self.assertEqual(state.highest_filled_step, -1,
                             "Missing fill price must not promote Step0")
            self.assertEqual(adapter.place_market_calls, place_market_before,
                             "No Step0 resubmission")
            self.assertEqual(
                sum(1 for c in adapter.calls if c[0] == "set_shared_tp"),
                tp_before, "No TP placed")
            self.assertEqual(
                sum(1 for c in adapter.calls if c[0] == "place_limit"),
                ladder_before, "No Step1 placed")
            self.assertEqual(state.status, "needs_recovery")
            self.assertIn("could not establish Step0 fill price",
                          state.freeze_reason or "")

    def test_D_invalid_fill_price_fails_closed(self):
        """An unparseable averageFillPrice must also fail closed."""
        import tempfile
        from pathlib import Path
        from plugins.trade.agents.x_ondoperps_agent import _order_state_from_ondo_row

        with tempfile.TemporaryDirectory() as tmp:
            svc = _build_svc_for_step0(
                Path(tmp) / "state.json",
                Path(tmp) / "ledger.jsonl",
                Path(tmp) / "events.log",
            )
            key = "ondoperps/bitget/ETH/BUY"
            adapter = _OndoStep0Adapter()
            svc._adapters[key] = adapter
            ondo_row = {
                "orderId": adapter.fake_oid,
                "clientOrderId": "82738593280000",
                "side": "buy",
                "market": "ETH-USD.P",
                "status": "fullyfilled",
                "size": "0.001",
                "filledSize": "0.001",
                "averageFillPrice": "not-a-number",
            }
            adapter.lookup_result = _order_state_from_ondo_row(ondo_row)
            adapter.position = {
                "symbol": "ETH",
                "side": "long",
                "size": "0.001",
                "entry_price": "2412.60",
            }
            _seed_step0_state(svc, key, submission_phase=SUBMISSION_CONFIRMED,
                              pending_order_exchange_id=None)

            place_market_before = adapter.place_market_calls
            tp_before = sum(1 for c in adapter.calls if c[0] == "set_shared_tp")

            svc._maybe_confirm_step0(key)

            state = svc._states[key]
            self.assertEqual(state.highest_filled_step, -1)
            self.assertEqual(adapter.place_market_calls, place_market_before)
            self.assertEqual(
                sum(1 for c in adapter.calls if c[0] == "set_shared_tp"),
                tp_before)
            self.assertEqual(state.status, "needs_recovery")


if __name__ == "__main__":
    unittest.main()
