"""MetaTrader grouped-cancel: deterministic regression tests.

These tests cover the Step E fix:
  - _execute_grouped_cancel must combine (side, base_type) into the full
    EA representation (BUY_LIMIT / SELL_LIMIT / BUY_STOP / SELL_STOP).
  - verified=True requires (targeted > 0) AND (succeeded == targeted)
    AND (failed == 0). Zero matches must NOT produce verified=True.
  - WebTrade2 phase2 surfaces NO_MATCH as success=False, status="NO_MATCH",
    error.code="CANCEL_NO_MATCH" with audit accepted=0 requested=1.

Uses a fake _bridge_post (no real broker, no mutations).

The 16 tests below match the user's required coverage list:
  1-4: side+type -> BUY_LIMIT / SELL_LIMIT / BUY_STOP / SELL_STOP
  5-6: missing side / unsupported order type fail closed
  7-9: matched=0/succeeded=0/failed=0 -> verified=False; full match -> verified=True;
       partial -> verified=False, partial=True
  10-11: phase2 zero match -> success=False/status=NO_MATCH/CANCEL_NO_MATCH
         + audit accepted=0/requested=1/error_code=CANCEL_NO_MATCH
  12-14: SOLUSD BUY LIMIT cannot match ZECUSD / SOLUSD SELL LIMIT /
         SOLUSD BUY STOP
  15:    multiple tickets in same group all targeted
  16:    unrelated groups unchanged
"""
from __future__ import annotations

import importlib
import sys
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

from fastapi.testclient import TestClient


def _import(name: str):
    if name in sys.modules:
        return sys.modules[name]
    return importlib.import_module(name)


# ---------------------------------------------------------------------------
# Fake bridge that records every dispatch and returns canned EA responses.
# ---------------------------------------------------------------------------

class FakeBridge:
    """Records every bridge post and returns canned responses.

    Use ``set_response(action, response_dict)`` to set the per-action reply.
    Use ``set_default(response_dict)`` for any unmatched action.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.responses: Dict[str, Dict[str, Any]] = {}
        self.default: Dict[str, Any] = {"ok": True, "matched": 0,
                                        "succeeded": 0, "failed": 0,
                                        "failures": []}

    def set_response(self, action: str, response: Dict[str, Any]) -> None:
        self.responses[action] = response

    def set_default(self, response: Dict[str, Any]) -> None:
        self.default = response

    def post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(dict(payload))
        action = str(payload.get("action") or "")
        if action in self.responses:
            return dict(self.responses[action])
        return dict(self.default)


# ---------------------------------------------------------------------------
# Phase2 / WebTrade2 app harness
# ---------------------------------------------------------------------------

def _build_app(*, dry_run: bool = False, write_enabled: bool = True,
               ladder_enabled: bool = False,
               live_accounts=None, live_operations=None):
    cfg_mod = _import("plugins.trade.webtrade2.config")
    app_mod = _import("plugins.trade.webtrade2.app")
    p2_mod = _import("plugins.trade.webtrade2.phase2")
    cfg = cfg_mod.WebTrade2Config.from_values(
        password="test-password", session_secret="x" * 32,
        port=9009, write_enabled=write_enabled, dry_run=dry_run,
        preview_ttl_seconds=300, ladder_enabled=ladder_enabled,
        live_accounts=live_accounts, live_operations=live_operations,
    )
    bridge = FakeBridge()
    # We patch _bridge_post AFTER agent is imported but BEFORE the app
    # constructs Phase2Service / TradeDesk, by monkey-patching the agent
    # module's _bridge_post.
    mt_mod = _import("plugins.trade.agents.x_metatrader_agent")
    # Build desk with the agent's execute, but route _bridge_post through
    # our fake.
    real_bridge_post = mt_mod._bridge_post

    def fake_bridge_post(payload, timeout_seconds=mt_mod._TIMEOUT_SECONDS):
        return bridge.post(payload)

    mt_mod._bridge_post = fake_bridge_post
    try:
        desk = mt_mod.TradeDesk() if hasattr(mt_mod, "TradeDesk") else None
    except Exception:
        desk = None
    # Use a RealTradeDesk with the patched _bridge_post so the agent
    # actually dispatches and we record the wire payload.
    from plugins.trade.tradedesk import TradeDesk as _TD
    desk = _TD()
    # Re-patch _bridge_post again because TradeDesk imports its own copy
    mt_mod._bridge_post = fake_bridge_post

    p2 = p2_mod.WebTrade2Phase2Service(
        desk=desk, session_secret="x" * 32,
        write_enabled=write_enabled, dry_run=dry_run,
        preview_ttl_seconds=300, ladder_enabled=ladder_enabled,
        live_accounts=live_accounts, live_operations=live_operations,
    )
    app = app_mod.create_app(config=cfg, phase2=p2, service=None)
    return app, p2, bridge, mt_mod


def _login(client: TestClient) -> str:
    r = client.get("/api/session")
    csrf = r.cookies.get("webtrade2_csrf")
    if not csrf:
        r2 = client.post("/login", data={"password": "test-password"}, follow_redirects=False)
        csrf = r2.cookies.get("webtrade2_csrf") or client.cookies.get("webtrade2_csrf")
    return csrf or ""


def _hdr(csrf: str) -> Dict[str, str]:
    return {"X-CSRF-Token": csrf}


def _set_account_env():
    """Set required MetaTrader env vars before agent module is used."""
    import os
    os.environ.setdefault("LINUX_WIN_HOST", "127.0.0.1")
    os.environ.setdefault("LINUX_WIN_PORT", "5050")
    os.environ.setdefault("MT_LITE7486706MT5_ACCOUNT", "7486706")


# Ensure env vars are set before any test runs
_set_account_env()


class TestGroupedCancelMatcher(unittest.TestCase):
    """Tests 1-9 + 12-16: agent-level wire format and semantics."""

    def setUp(self):
        _set_account_env()
        self.mt_mod = _import("plugins.trade.agents.x_metatrader_agent")

    def _patched(self, response=None):
        bridge = FakeBridge()
        if response is not None:
            bridge.set_response("cancel_orders", response)
        self.mt_mod._bridge_post = bridge.post
        return bridge

    # ---- Test 1: buy + limit -> BUY_LIMIT ----
    def test_01_buy_limit_sends_BUY_LIMIT(self):
        bridge = self._patched({"ok": True, "matched": 1, "succeeded": 1, "failed": 0})
        resp = self.mt_mod.execute({
            "operation": "cancel_order_group",
            "exchange": "metatrader", "account": "LITE7486706MT5",
            "symbol": "SOLUSD", "side": "buy", "order_type": "limit",
        })
        # Inspect the wire payload
        cancel_calls = [c for c in bridge.calls if c.get("action") == "cancel_orders"]
        self.assertEqual(len(cancel_calls), 1)
        wire = cancel_calls[0]
        self.assertEqual(wire.get("symbol"), "SOLUSD")
        self.assertEqual(wire.get("side"), "BUY")
        self.assertEqual(wire.get("order_type"), "BUY_LIMIT",
                         "Group cancel must send full side-prefixed type")

    # ---- Test 2: sell + limit -> SELL_LIMIT ----
    def test_02_sell_limit_sends_SELL_LIMIT(self):
        bridge = self._patched({"ok": True, "matched": 1, "succeeded": 1, "failed": 0})
        self.mt_mod.execute({
            "operation": "cancel_order_group",
            "exchange": "metatrader", "account": "LITE7486706MT5",
            "symbol": "SOLUSD", "side": "sell", "order_type": "limit",
        })
        cancel_calls = [c for c in bridge.calls if c.get("action") == "cancel_orders"]
        self.assertEqual(cancel_calls[0].get("order_type"), "SELL_LIMIT")

    # ---- Test 3: buy + stop -> BUY_STOP ----
    def test_03_buy_stop_sends_BUY_STOP(self):
        bridge = self._patched({"ok": True, "matched": 1, "succeeded": 1, "failed": 0})
        self.mt_mod.execute({
            "operation": "cancel_order_group",
            "exchange": "metatrader", "account": "LITE7486706MT5",
            "symbol": "SOLUSD", "side": "buy", "order_type": "stop",
        })
        cancel_calls = [c for c in bridge.calls if c.get("action") == "cancel_orders"]
        self.assertEqual(cancel_calls[0].get("order_type"), "BUY_STOP")

    # ---- Test 4: sell + stop -> SELL_STOP ----
    def test_04_sell_stop_sends_SELL_STOP(self):
        bridge = self._patched({"ok": True, "matched": 1, "succeeded": 1, "failed": 0})
        self.mt_mod.execute({
            "operation": "cancel_order_group",
            "exchange": "metatrader", "account": "LITE7486706MT5",
            "symbol": "SOLUSD", "side": "sell", "order_type": "stop",
        })
        cancel_calls = [c for c in bridge.calls if c.get("action") == "cancel_orders"]
        self.assertEqual(cancel_calls[0].get("order_type"), "SELL_STOP")

    # ---- Test 5: missing side fails closed ----
    def test_05_missing_side_fails_closed(self):
        bridge = self._patched()
        resp = self.mt_mod.execute({
            "operation": "cancel_order_group",
            "exchange": "metatrader", "account": "LITE7486706MT5",
            "symbol": "SOLUSD", "side": "", "order_type": "limit",
        })
        # Should never reach the bridge
        cancel_calls = [c for c in bridge.calls if c.get("action") == "cancel_orders"]
        self.assertEqual(cancel_calls, [], "No bridge call when side is missing")
        # Returns failure
        self.assertFalse(resp.success)
        err_code = self._err_code(resp)
        self.assertEqual(err_code, "MISSING_SIDE")

    # ---- Test 6: unsupported order type fails closed ----
    def test_06_unsupported_order_type_fails_closed(self):
        bridge = self._patched()
        resp = self.mt_mod.execute({
            "operation": "cancel_order_group",
            "exchange": "metatrader", "account": "LITE7486706MT5",
            "symbol": "SOLUSD", "side": "buy", "order_type": "MARKET",
        })
        cancel_calls = [c for c in bridge.calls if c.get("action") == "cancel_orders"]
        self.assertEqual(cancel_calls, [])
        self.assertFalse(resp.success)
        err_code = self._err_code(resp)
        self.assertEqual(err_code, "UNSUPPORTED_ORDER_TYPE")

    @staticmethod
    def _err_code(resp):
        err = resp.error
        if err is None:
            return ""
        if isinstance(err, dict):
            return err.get("code", "") or ""
        # CanonicalError has a .code attribute
        return getattr(err, "code", "") or ""

    # ---- Test 7: matched=0/succeeded=0/failed=0 -> verified=False ----
    def test_07_zero_match_verified_false(self):
        bridge = self._patched({"ok": True, "matched": 0,
                                "succeeded": 0, "failed": 0,
                                "failures": []})
        resp = self.mt_mod.execute({
            "operation": "cancel_order_group",
            "exchange": "metatrader", "account": "LITE7486706MT5",
            "symbol": "SOLUSD", "side": "buy", "order_type": "limit",
        })
        cg = resp.cancel_group
        self.assertIsNotNone(cg)
        self.assertEqual(cg.targeted_order_count, 0)
        self.assertEqual(cg.cancelled_order_count, 0)
        self.assertFalse(cg.verified,
                         "Zero matches must NOT produce verified=True")
        self.assertEqual(cg.status, "no_match")

    # ---- Test 8: full match -> verified=True with exact counts ----
    def test_08_full_match_verified_true_with_counts(self):
        bridge = self._patched({"ok": True, "matched": 5,
                                "succeeded": 5, "failed": 0,
                                "failures": []})
        resp = self.mt_mod.execute({
            "operation": "cancel_order_group",
            "exchange": "metatrader", "account": "LITE7486706MT5",
            "symbol": "SOLUSD", "side": "buy", "order_type": "limit",
        })
        cg = resp.cancel_group
        self.assertTrue(cg.verified)
        self.assertEqual(cg.targeted_order_count, 5)
        self.assertEqual(cg.cancelled_order_count, 5)
        self.assertEqual(cg.status, "success")
        self.assertFalse(cg.partial)

    # ---- Test 9: partial match -> verified=False, partial=True ----
    def test_09_partial_match_verified_false_partial_true(self):
        bridge = self._patched({"ok": True, "matched": 5,
                                "succeeded": 3, "failed": 2,
                                "failures": [{"ticket": 1, "error": "X"}]})
        resp = self.mt_mod.execute({
            "operation": "cancel_order_group",
            "exchange": "metatrader", "account": "LITE7486706MT5",
            "symbol": "SOLUSD", "side": "buy", "order_type": "limit",
        })
        cg = resp.cancel_group
        self.assertFalse(cg.verified)
        self.assertTrue(cg.partial)
        self.assertEqual(cg.targeted_order_count, 5)
        self.assertEqual(cg.cancelled_order_count, 3)
        self.assertEqual(cg.remaining_target_count, 2)


class TestPhase2CancelNoMatch(unittest.TestCase):
    """Tests 10-11: phase2 surfaces NO_MATCH + audit records actual outcome."""

    def test_10_phase2_zero_match_NO_MATCH(self):
        app, _, bridge, _ = _build_app(
            dry_run=False, write_enabled=True,
            live_accounts=[("metatrader", "LITE7486706MT5")],
            live_operations=["cancel_order_group"],
        )
        bridge.set_response("cancel_orders", {
            "ok": True, "matched": 0, "succeeded": 0, "failed": 0, "failures": [],
        })
        client = TestClient(app)
        csrf = _login(client)
        r = client.post("/api/orders/cancel_group",
                        json={"exchange": "metatrader",
                              "account": "LITE7486706MT5",
                              "symbol": "SOLUSD",
                              "side": "buy",
                              "order_type": "limit"},
                        headers=_hdr(csrf))
        body = r.json()
        self.assertFalse(body.get("success"),
                         "Zero match must be success=False")
        self.assertEqual(body.get("status"), "NO_MATCH")
        self.assertFalse(body.get("partial"))
        self.assertEqual(body.get("error", {}).get("code"),
                         "CANCEL_NO_MATCH")
        cg = body.get("cancel_group") or {}
        self.assertEqual(cg.get("targeted_order_count"), 0)
        self.assertEqual(cg.get("cancelled_order_count"), 0)
        self.assertFalse(cg.get("verified"))
        # requested/accepted at the top level must reflect reality
        self.assertEqual(body.get("requested"), 1)
        self.assertEqual(body.get("accepted"), 0)

    def test_11_phase2_NO_MATCH_audit(self):
        """Audit must report accepted=0 / requested=1 / error_code=CANCEL_NO_MATCH
        when the broker matched zero orders for an expected group cancel."""
        app, p2, bridge, _ = _build_app(
            dry_run=False, write_enabled=True,
            live_accounts=[("metatrader", "LITE7486706MT5")],
            live_operations=["cancel_order_group"],
        )
        bridge.set_response("cancel_orders", {
            "ok": True, "matched": 0, "succeeded": 0, "failed": 0, "failures": [],
        })
        audit_calls: List[Dict[str, Any]] = []

        original_audit = p2._audit

        def capturing_audit(*args, **kwargs):
            audit_calls.append({"args": args, "kwargs": kwargs})
            return original_audit(*args, **kwargs)

        with mock.patch.object(p2, "_audit", side_effect=capturing_audit):
            client = TestClient(app)
            csrf = _login(client)
            r = client.post("/api/orders/cancel_group",
                            json={"exchange": "metatrader",
                                  "account": "LITE7486706MT5",
                                  "symbol": "SOLUSD",
                                  "side": "buy",
                                  "order_type": "limit"},
                            headers=_hdr(csrf))
            self.assertEqual(r.json().get("status"), "NO_MATCH")

        # Find the NO_MATCH audit
        no_match_audits = [a for a in audit_calls
                          if a["kwargs"].get("error_code") == "CANCEL_NO_MATCH"]
        self.assertGreaterEqual(len(no_match_audits), 1,
                                "Must emit at least one CANCEL_NO_MATCH audit")
        last = no_match_audits[-1]["kwargs"]
        self.assertEqual(last.get("accepted"), 0)
        self.assertEqual(last.get("requested"), 1)
        self.assertEqual(last.get("status"), "REJECTED")


class TestGroupIsolation(unittest.TestCase):
    """Tests 12-16: SOLUSD BUY LIMIT must not affect other groups."""

    def setUp(self):
        _set_account_env()
        self.mt_mod = _import("plugins.trade.agents.x_metatrader_agent")

    def _patched(self, response):
        bridge = FakeBridge()
        bridge.set_response("cancel_orders", response)
        self.mt_mod._bridge_post = bridge.post
        return bridge

    # ---- Test 12: SOLUSD BUY LIMIT cancellation cannot match ZECUSD ----
    def test_12_solusd_buy_limit_cannot_match_zecusd(self):
        bridge = self._patched({"ok": True, "matched": 0,
                                "succeeded": 0, "failed": 0, "failures": []})
        resp = self.mt_mod.execute({
            "operation": "cancel_order_group",
            "exchange": "metatrader", "account": "LITE7486706MT5",
            "symbol": "SOLUSD", "side": "buy", "order_type": "limit",
        })
        # Inspect wire payload: symbol is SOLUSD, not ZECUSD.
        cancel_calls = [c for c in bridge.calls if c.get("action") == "cancel_orders"]
        self.assertEqual(len(cancel_calls), 1)
        self.assertEqual(cancel_calls[0]["symbol"], "SOLUSD",
                         "Symbol must be SOLUSD; cannot include ZECUSD")
        self.assertNotEqual(cancel_calls[0]["symbol"], "ZECUSD")

    # ---- Test 13: SOLUSD BUY LIMIT cannot match SOLUSD SELL LIMIT ----
    def test_13_solusd_buy_limit_cannot_match_solusd_sell_limit(self):
        bridge = self._patched({"ok": True, "matched": 0,
                                "succeeded": 0, "failed": 0, "failures": []})
        resp = self.mt_mod.execute({
            "operation": "cancel_order_group",
            "exchange": "metatrader", "account": "LITE7486706MT5",
            "symbol": "SOLUSD", "side": "buy", "order_type": "limit",
        })
        cancel_calls = [c for c in bridge.calls if c.get("action") == "cancel_orders"]
        # The wire payload uses BUY + LIMIT, never SELL_LIMIT.
        self.assertEqual(cancel_calls[0]["side"], "BUY")
        self.assertEqual(cancel_calls[0]["order_type"], "BUY_LIMIT")
        self.assertNotIn(cancel_calls[0]["order_type"], ["SELL_LIMIT", "SELL_STOP"])

    # ---- Test 14: SOLUSD BUY LIMIT cannot match SOLUSD BUY STOP ----
    def test_14_solusd_buy_limit_cannot_match_solusd_buy_stop(self):
        bridge = self._patched({"ok": True, "matched": 0,
                                "succeeded": 0, "failed": 0, "failures": []})
        resp = self.mt_mod.execute({
            "operation": "cancel_order_group",
            "exchange": "metatrader", "account": "LITE7486706MT5",
            "symbol": "SOLUSD", "side": "buy", "order_type": "limit",
        })
        cancel_calls = [c for c in bridge.calls if c.get("action") == "cancel_orders"]
        self.assertEqual(cancel_calls[0]["order_type"], "BUY_LIMIT")
        self.assertNotEqual(cancel_calls[0]["order_type"], "BUY_STOP")

    # ---- Test 15: multiple tickets in same group are all targeted ----
    def test_15_multiple_tickets_in_same_group_all_targeted(self):
        # Broker returns matched=4 (4 SOLUSD BUY LIMIT tickets)
        bridge = self._patched({"ok": True, "matched": 4,
                                "succeeded": 4, "failed": 0, "failures": []})
        resp = self.mt_mod.execute({
            "operation": "cancel_order_group",
            "exchange": "metatrader", "account": "LITE7486706MT5",
            "symbol": "SOLUSD", "side": "buy", "order_type": "limit",
        })
        cg = resp.cancel_group
        self.assertEqual(cg.targeted_order_count, 4)
        self.assertEqual(cg.cancelled_order_count, 4)
        self.assertTrue(cg.verified)
        self.assertEqual(cg.status, "success")

    # ---- Test 16: unrelated groups remain unchanged ----
    def test_16_unrelated_groups_remain_unchanged(self):
        """Cancel SOLUSD BUY LIMIT. Wire payload must NOT include
        ZECUSD or any other symbol, side, or order_type."""
        bridge = self._patched({"ok": True, "matched": 1,
                                "succeeded": 1, "failed": 0, "failures": []})
        resp = self.mt_mod.execute({
            "operation": "cancel_order_group",
            "exchange": "metatrader", "account": "LITE7486706MT5",
            "symbol": "SOLUSD", "side": "buy", "order_type": "limit",
        })
        cancel_calls = [c for c in bridge.calls if c.get("action") == "cancel_orders"]
        self.assertEqual(len(cancel_calls), 1)
        wire = cancel_calls[0]
        self.assertEqual(wire["symbol"], "SOLUSD")
        self.assertEqual(wire["side"], "BUY")
        self.assertEqual(wire["order_type"], "BUY_LIMIT")
        # Negative checks
        self.assertNotEqual(wire["symbol"], "ZECUSD")
        self.assertNotEqual(wire["symbol"], "BTCUSD")
        self.assertNotEqual(wire["side"], "SELL")
        self.assertNotEqual(wire["order_type"], "SELL_LIMIT")
        self.assertNotEqual(wire["order_type"], "BUY_STOP")
        self.assertNotEqual(wire["order_type"], "SELL_STOP")
        self.assertNotEqual(wire["order_type"], "LIMIT")
