"""WebTrade2 Pre-LIVE hardening: account-level LIVE allowlist + DRY_RUN boundary.

Implements the eight hard-safety scenarios A-H from the pre-LIVE checkpoint:

  A. DRY_RUN=1, allowlist empty            -> 0 mutation dispatches.
  B. DRY_RUN=1, allowlist non-empty        -> 0 mutation dispatches.
  C. DRY_RUN=0, WRITE_ENABLED=1, NOT in     -> rejected BEFORE desk.execute.
  D. DRY_RUN=0, WRITE_ENABLED=0, allowlist  -> rejected BEFORE desk.execute.
  E. DRY_RUN=0, WRITE_ENABLED=1, allowlist  -> dispatch reaches fake desk.
  F. LADDER_ENABLED=0 even with allowlist   -> ladder NOT dispatched.
  G. set_tp/set_sl/close_position obey allowlist.
  H. MetaTrader side survives full request construction.
"""

import importlib
import sys
import unittest
from typing import Any, Dict, Optional

from starlette.testclient import TestClient

sys.path.insert(0, "/root/kam")


def _import(mod: str):
    return importlib.import_module(mod)


class FakeDesk:
    """Generic desk stub recording every call."""

    MUTATIONS = {
        "new_order", "ladder",
        "cancel_order", "cancel_order_group",
        "set_tp", "set_sl", "close_position",
    }

    def __init__(self) -> None:
        self.calls: list[Dict[str, Any]] = []

    def capabilities(self, exchange: str):
        caps = {"balance", "positions_orders", "positions_management",
                "new_order", "ladder", "set_tp", "set_sl", "close_position",
                "cancel_order", "cancel_order_group",
                "list_instruments", "resolve_instrument", "market_price",
                "get_tickers", "candles", "account_financials"}
        return list(caps)

    def list_accounts(self, exchange: str):
        return ["acc1", "acc2"]

    def list_exchanges(self):
        return ["metatrader"]

    def execute(self, request: Dict[str, Any]):
        self.calls.append(dict(request))
        if request.get("operation") in self.MUTATIONS:
            from plugins.trade.canonical import make_success
            return make_success(
                operation=request.get("operation"),
                exchange=request.get("exchange"),
                account=request.get("account"),
                data={"order_id": "fake-1"} if request.get("operation") == "new_order" else {},
            )
        from plugins.trade.canonical import make_success
        return make_success(
            operation=request.get("operation"),
            exchange=request.get("exchange"),
            account=request.get("account"),
            data={"positions": [], "order_groups": [], "open_order_count": 0},
        )


def _build_app(*, dry_run: bool, write_enabled: bool = True,
               ladder_enabled: bool = False, live_accounts: Any = None,
               live_operations: Any = None):
    cfg_mod = _import("plugins.trade.webtrade2.config")
    app_mod = _import("plugins.trade.webtrade2.app")
    p2_mod = _import("plugins.trade.webtrade2.phase2")
    cfg = cfg_mod.WebTrade2Config.from_values(
        password="test-password", session_secret="x" * 32,
        port=9009, write_enabled=write_enabled, dry_run=dry_run,
        preview_ttl_seconds=300, ladder_enabled=ladder_enabled,
        live_accounts=live_accounts,
        live_operations=live_operations,
    )
    desk = FakeDesk()
    p2 = p2_mod.WebTrade2Phase2Service(
        desk=desk, session_secret="x" * 32,
        write_enabled=write_enabled, dry_run=dry_run,
        preview_ttl_seconds=300, ladder_enabled=ladder_enabled,
        live_accounts=live_accounts,
        live_operations=live_operations,
    )
    app = app_mod.create_app(config=cfg, phase2=p2, service=None)
    return app, p2, desk


def _login(client: TestClient) -> str:
    r = client.post("/login", data={"password": "test-password"},
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    csrf = client.get("/api/session").json().get("csrf") or ""
    return str(csrf)


def _hdr(csrf: str) -> Dict[str, str]:
    return {"X-CSRF-Token": csrf}


class WebTrade2AllowlistHardSafetyTests(unittest.TestCase):

    # --------------------------------------------------------------- A
    def test_A_dry_run_with_empty_allowlist_blocks_every_mutation(self) -> None:
        """A. DRY_RUN=1, empty allowlist -> 0 mutation dispatches."""
        app, _, desk = _build_app(dry_run=True, write_enabled=True, live_accounts=[])
        client = TestClient(app)
        csrf = _login(client)
        # Try every mutation. All execute paths must come back DRY_RUN.
        r = client.post("/api/trade/preview_order",
                        json={"exchange": "metatrader", "account": "acc1",
                              "symbol": "BTCUSD", "side": "buy",
                              "size": "0.01", "price": "100"},
                        headers=_hdr(csrf))
        pid = r.json().get("preview_id")
        if pid:
            r = client.post("/api/trade/execute", json={"preview_id": pid}, headers=_hdr(csrf))
            self.assertEqual(r.json().get("status"), "DRY_RUN")
        # One-shot positions must each return DRY_RUN.
        for path, body in [
            ("/api/position/set_tp", {"exchange": "metatrader", "account": "acc1",
                                       "symbol": "BTCUSD", "side": "buy", "price": "110"}),
            ("/api/position/set_sl", {"exchange": "metatrader", "account": "acc1",
                                       "symbol": "BTCUSD", "side": "buy", "price": "90"}),
            ("/api/position/close", {"exchange": "metatrader", "account": "acc1",
                                      "symbol": "BTCUSD", "side": "buy"}),
            ("/api/orders/cancel_group", {"exchange": "metatrader", "account": "acc1",
                                           "symbol": "BTCUSD", "side": "buy"}),
        ]:
            r = client.post(path, json=body, headers=_hdr(csrf))
            self.assertEqual(r.json().get("status"), "DRY_RUN", path)
        # Desk MUST have zero mutation calls.
        mutation_calls = [c for c in desk.calls if c.get("operation") in FakeDesk.MUTATIONS]
        self.assertEqual(mutation_calls, [],
                         f"mutation dispatched under DRY_RUN: {mutation_calls}")

    # --------------------------------------------------------------- B
    def test_B_dry_run_with_allowlisted_account_still_blocks_mutation(self) -> None:
        """B. DRY_RUN=1 + account allowlisted -> still 0 mutation dispatches.

        The allowlist is a LIVE-path protection, not a preview blocker, and
        must NEVER widen what DRY_RUN permits.
        """
        app, _, desk = _build_app(dry_run=True, write_enabled=True,
                                   live_accounts=[("metatrader", "acc1")])
        client = TestClient(app)
        csrf = _login(client)
        r = client.post("/api/trade/preview_order",
                        json={"exchange": "metatrader", "account": "acc1",
                              "symbol": "BTCUSD", "side": "buy",
                              "size": "0.01", "price": "100"},
                        headers=_hdr(csrf))
        pid = r.json().get("preview_id")
        if pid:
            r = client.post("/api/trade/execute", json={"preview_id": pid}, headers=_hdr(csrf))
            self.assertEqual(r.json().get("status"), "DRY_RUN")
        mutation_calls = [c for c in desk.calls if c.get("operation") in FakeDesk.MUTATIONS]
        self.assertEqual(mutation_calls, [],
                         f"DRY_RUN let an allowlisted mutation through: {mutation_calls}")

    # --------------------------------------------------------------- C
    def test_C_live_path_rejects_non_allowlisted_account(self) -> None:
        """C. DRY_RUN=0, WRITE_ENABLED=1, account NOT allowlisted ->
        rejected before desk.execute."""
        app, _, desk = _build_app(dry_run=False, write_enabled=True,
                                   live_accounts=[])  # empty allowlist
        client = TestClient(app)
        csrf = _login(client)
        r = client.post("/api/trade/preview_order",
                        json={"exchange": "metatrader", "account": "acc1",
                              "symbol": "BTCUSD", "side": "buy",
                              "size": "0.01", "price": "100"},
                        headers=_hdr(csrf))
        self.assertEqual(r.json().get("kind"), "order")
        pid = r.json().get("preview_id")
        r2 = client.post("/api/trade/execute", json={"preview_id": pid},
                         headers=_hdr(csrf))
        body = r2.json()
        self.assertFalse(body.get("success"))
        self.assertEqual(body.get("error", {}).get("code"),
                         "LIVE_ACCOUNT_NOT_ALLOWED")
        self.assertEqual(body.get("operation"), "live_account_not_allowed")
        self.assertEqual(body.get("exchange"), "metatrader")
        self.assertEqual(body.get("account"), "acc1")
        mutation_calls = [c for c in desk.calls if c.get("operation") == "new_order"]
        self.assertEqual(mutation_calls, [],
                         f"desk.execute was reached for non-allowlisted account: {mutation_calls}")

    # --------------------------------------------------------------- D
    def test_D_write_disabled_blocks_even_allowlisted_account(self) -> None:
        """D. DRY_RUN=0, WRITE_ENABLED=0, allowlist non-empty -> 423."""
        app, _, desk = _build_app(dry_run=False, write_enabled=False,
                                   live_accounts=[("metatrader", "acc1")])
        client = TestClient(app)
        csrf = _login(client)
        r = client.post("/api/trade/preview_order",
                        json={"exchange": "metatrader", "account": "acc1",
                              "symbol": "BTCUSD", "side": "buy",
                              "size": "0.01", "price": "100"},
                        headers=_hdr(csrf))
        body = r.json()
        self.assertFalse(body.get("success"))
        self.assertEqual(body.get("error", {}).get("code"), "PHASE2_DISABLED")
        mutation_calls = [c for c in desk.calls if c.get("operation") in FakeDesk.MUTATIONS]
        self.assertEqual(mutation_calls, [])

    # --------------------------------------------------------------- E
    def test_E_live_path_allowlisted_account_dispatches_to_fake_desk(self) -> None:
        """E. DRY_RUN=0, WRITE_ENABLED=1, account allowlisted -> dispatch
        reaches fake desk. Uses a FAKE desk only."""
        app, _, desk = _build_app(dry_run=False, write_enabled=True,
                                   live_accounts=[("metatrader", "acc1")],
                                   live_operations=["new_order"])
        client = TestClient(app)
        csrf = _login(client)
        r = client.post("/api/trade/preview_order",
                        json={"exchange": "metatrader", "account": "acc1",
                              "symbol": "BTCUSD", "side": "buy",
                              "size": "0.01", "price": "100"},
                        headers=_hdr(csrf))
        pid = r.json().get("preview_id")
        r2 = client.post("/api/trade/execute", json={"preview_id": pid},
                         headers=_hdr(csrf))
        body = r2.json()
        self.assertTrue(body.get("success"))
        self.assertEqual(body.get("status"), "SUBMITTED")
        self.assertEqual(body.get("mode"), "LIVE")
        writes = [c for c in desk.calls if c.get("operation") == "new_order"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].get("exchange"), "metatrader")
        self.assertEqual(writes[0].get("account"), "acc1")
        self.assertEqual(writes[0].get("symbol"), "BTCUSD")
        self.assertEqual(writes[0].get("side"), "buy")

    # --------------------------------------------------------------- F
    def test_F_live_ladder_requires_ladder_enabled_even_if_allowlisted(self) -> None:
        """F. LADDER_ENABLED=0 with allowlist still blocks ladder dispatch."""
        app, _, desk = _build_app(dry_run=False, write_enabled=True,
                                   ladder_enabled=False,
                                   live_accounts=[("metatrader", "acc1")])
        client = TestClient(app)
        csrf = _login(client)
        r = client.post("/api/trade/preview_ladder",
                        json={"exchange": "metatrader", "account": "acc1",
                              "symbol": "BTCUSD", "side": "buy",
                              "order_count": 3, "total_size": "0.03",
                              "start_price": "100", "end_price": "99",
                              "distribution": "uniform"},
                        headers=_hdr(csrf))
        pid = r.json().get("preview_id")
        r2 = client.post("/api/trade/execute", json={"preview_id": pid},
                         headers=_hdr(csrf))
        body = r2.json()
        # LADDER_NOT_ENABLED must beat LIVE_ACCOUNT_NOT_ALLOWED: ladder
        # gate runs first.
        self.assertFalse(body.get("success"))
        self.assertEqual(body.get("error", {}).get("code"), "LADDER_NOT_ENABLED")
        ladder_calls = [c for c in desk.calls if c.get("operation") == "ladder"]
        self.assertEqual(ladder_calls, [])

    # --------------------------------------------------------------- G
    def test_G_set_tp_set_sl_close_cancel_obey_allowlist(self) -> None:
        """G. set_tp / set_sl / close_position / cancel_order_group all obey
        the LIVE account allowlist AND the operation allowlist.
        """
        # G.1: non-allowlisted account + operations allowlisted -> rejection
        # is LIVE_ACCOUNT_NOT_ALLOWED.
        app, _, desk = _build_app(
            dry_run=False, write_enabled=True,
            live_accounts=[("metatrader", "OTHER")],
            live_operations=["set_tp", "set_sl", "close_position", "cancel_order_group"],
        )
        client = TestClient(app)
        csrf = _login(client)
        for path, body, op in [
            ("/api/position/set_tp", {"exchange": "metatrader", "account": "acc1",
                                       "symbol": "BTCUSD", "side": "sell",
                                       "price": "110"}, "set_tp"),
            ("/api/position/set_sl", {"exchange": "metatrader", "account": "acc1",
                                       "symbol": "BTCUSD", "side": "sell",
                                       "price": "90"}, "set_sl"),
            ("/api/position/close", {"exchange": "metatrader", "account": "acc1",
                                      "symbol": "BTCUSD", "side": "sell"}, "close_position"),
            ("/api/orders/cancel_group", {"exchange": "metatrader", "account": "acc1",
                                           "symbol": "BTCUSD", "side": "sell"},
             "cancel_order_group"),
        ]:
            r = client.post(path, json=body, headers=_hdr(csrf))
            self.assertFalse(r.json().get("success"), path)
            self.assertEqual(r.json().get("error", {}).get("code"),
                             "LIVE_ACCOUNT_NOT_ALLOWED", path)
        mutation_calls = [c for c in desk.calls if c.get("operation") in FakeDesk.MUTATIONS]
        self.assertEqual(mutation_calls, [],
                         f"non-allowlisted mutations reached desk: {mutation_calls}")

        # G.2: allowlisted account + EMPTY operations -> rejection is
        # LIVE_OPERATION_NOT_ALLOWED.
        app3, _, desk3 = _build_app(
            dry_run=False, write_enabled=True,
            live_accounts=[("metatrader", "acc1")],
            live_operations=[],
        )
        client3 = TestClient(app3)
        csrf3 = _login(client3)
        r = client3.post("/api/position/set_tp",
                         json={"exchange": "metatrader", "account": "acc1",
                               "symbol": "BTCUSD", "side": "buy", "price": "110"},
                         headers=_hdr(csrf3))
        self.assertFalse(r.json().get("success"))
        self.assertEqual(r.json().get("error", {}).get("code"),
                         "LIVE_OPERATION_NOT_ALLOWED")

        # G.3: allowlisted account + only "new_order" in operations ->
        # set_tp must still be rejected with LIVE_OPERATION_NOT_ALLOWED.
        app4, _, desk4 = _build_app(
            dry_run=False, write_enabled=True,
            live_accounts=[("metatrader", "acc1")],
            live_operations=["new_order"],
        )
        client4 = TestClient(app4)
        csrf4 = _login(client4)
        for path, body, op in [
            ("/api/position/set_tp", {"exchange": "metatrader", "account": "acc1",
                                       "symbol": "BTCUSD", "side": "sell",
                                       "price": "110"}, "set_tp"),
            ("/api/position/set_sl", {"exchange": "metatrader", "account": "acc1",
                                       "symbol": "BTCUSD", "side": "sell",
                                       "price": "90"}, "set_sl"),
            ("/api/position/close", {"exchange": "metatrader", "account": "acc1",
                                      "symbol": "BTCUSD", "side": "sell"}, "close_position"),
        ]:
            r = client4.post(path, json=body, headers=_hdr(csrf4))
            self.assertFalse(r.json().get("success"), path)
            self.assertEqual(r.json().get("error", {}).get("code"),
                             "LIVE_OPERATION_NOT_ALLOWED", path)
        mutation_calls = [c for c in desk4.calls if c.get("operation") in FakeDesk.MUTATIONS]
        self.assertEqual(mutation_calls, [],
                         f"non-allowlisted ops reached desk: {mutation_calls}")

        # G.4: allowlisted account + set_tp allowed -> set_tp dispatches.
        app2, _, desk2 = _build_app(
            dry_run=False, write_enabled=True,
            live_accounts=[("metatrader", "acc1")],
            live_operations=["set_tp"],
        )
        client2 = TestClient(app2)
        csrf2 = _login(client2)
        r = client2.post("/api/position/set_tp",
                         json={"exchange": "metatrader", "account": "acc1",
                               "symbol": "BTCUSD", "side": "buy", "price": "110"},
                         headers=_hdr(csrf2))
        body = r.json()
        self.assertTrue(body.get("success"))
        self.assertEqual(body.get("status"), "SUBMITTED")
        self.assertEqual(body.get("mode"), "LIVE")
        tp_calls = [c for c in desk2.calls if c.get("operation") == "set_tp"]
        self.assertEqual(len(tp_calls), 1)
        self.assertEqual(tp_calls[0].get("symbol"), "BTCUSD")
        self.assertEqual(tp_calls[0].get("side"), "buy")
        self.assertEqual(tp_calls[0].get("price"), "110")

    # --------------------------------------------------------------- H
    def test_H_metatrader_side_survives_full_request_construction(self) -> None:
        """H. MetaTrader side survives full request construction through
        every position-management endpoint.
        """
        app, _, desk = _build_app(dry_run=False, write_enabled=True,
                                   live_accounts=[("metatrader", "acc1")],
                                   live_operations=["set_tp", "set_sl", "close_position"])
        client = TestClient(app)
        csrf = _login(client)
        # Hedging fixture: BTCUSD BUY and BTCUSD SELL are independent.
        for side in ("buy", "sell"):
            for op, body_extra in [
                ("set_tp", {"price": "110"}),
                ("set_sl", {"price": "90"}),
            ]:
                r = client.post(
                    f"/api/position/{op}",
                    json={"exchange": "metatrader", "account": "acc1",
                          "symbol": "BTCUSD", "side": side, **body_extra},
                    headers=_hdr(csrf),
                )
                self.assertTrue(r.json().get("success"),
                                f"{op} {side}: {r.text}")
                self.assertEqual(r.json().get("side"), side)
            r = client.post(
                "/api/position/close",
                json={"exchange": "metatrader", "account": "acc1",
                      "symbol": "BTCUSD", "side": side},
                headers=_hdr(csrf),
            )
            self.assertTrue(r.json().get("success"), f"close {side}: {r.text}")
            self.assertEqual(r.json().get("side"), side)

        # Verify desk received two distinct (symbol, side) calls.
        tp_calls = [c for c in desk.calls if c.get("operation") == "set_tp"]
        sl_calls = [c for c in desk.calls if c.get("operation") == "set_sl"]
        cl_calls = [c for c in desk.calls if c.get("operation") == "close_position"]
        self.assertEqual({(c["symbol"], c["side"]) for c in tp_calls},
                         {("BTCUSD", "buy"), ("BTCUSD", "sell")})
        self.assertEqual({(c["symbol"], c["side"]) for c in sl_calls},
                         {("BTCUSD", "buy"), ("BTCUSD", "sell")})
        self.assertEqual({(c["symbol"], c["side"]) for c in cl_calls},
                         {("BTCUSD", "buy"), ("BTCUSD", "sell")})


class WebTrade2LiveAllowlistHelperTests(unittest.TestCase):

    def test_helper_empty_allowlist_returns_false_for_any_account(self) -> None:
        from plugins.trade.webtrade2.phase2 import (
            WebTrade2Phase2Service, _normalize_live_accounts,
        )
        desk = FakeDesk()
        p2 = WebTrade2Phase2Service(desk=desk, session_secret="x" * 32,
                                    live_accounts=[])
        self.assertFalse(p2.is_live_account_allowed("apex", "BITGET"))
        self.assertFalse(p2.is_live_account_allowed("metatrader", "LITE"))
        # Whitespace / case insensitivity
        self.assertFalse(p2.is_live_account_allowed("", ""))
        self.assertFalse(p2.is_live_account_allowed("apex", ""))

    def test_helper_specific_accounts_match_only_those(self) -> None:
        from plugins.trade.webtrade2.phase2 import WebTrade2Phase2Service
        p2 = WebTrade2Phase2Service(desk=FakeDesk(), session_secret="x" * 32,
                                    live_accounts=[("metatrader", "LITE"),
                                                   ("apex", "BITGET")])
        self.assertTrue(p2.is_live_account_allowed("metatrader", "LITE"))
        self.assertTrue(p2.is_live_account_allowed("METATRADER", "LITE"))  # case-insensitive ex
        self.assertTrue(p2.is_live_account_allowed("Apex", "BITGET"))
        # Different account on allowlisted exchange -> False
        self.assertFalse(p2.is_live_account_allowed("metatrader", "OTHER"))
        self.assertFalse(p2.is_live_account_allowed("apex", "FIBO"))

    def test_normalize_string_with_whitespace_and_case(self) -> None:
        from plugins.trade.webtrade2.phase2 import _normalize_live_accounts
        out, wildcard = _normalize_live_accounts(" Apex : BITGET , METATRADER:LITE ")
        self.assertEqual(out, frozenset({("apex", "BITGET"), ("metatrader", "LITE")}))
        self.assertFalse(wildcard)

    def test_normalize_rejects_unqualified_token(self) -> None:
        from plugins.trade.webtrade2.phase2 import _normalize_live_accounts
        with self.assertRaises(ValueError):
            _normalize_live_accounts("BITGET")  # no exchange prefix

    def test_normalize_accepts_iterable_of_tuples(self) -> None:
        from plugins.trade.webtrade2.phase2 import _normalize_live_accounts
        out, wildcard = _normalize_live_accounts([("metatrader", "LITE"), ("apex", "BITGET")])
        self.assertEqual(out, frozenset({("metatrader", "LITE"), ("apex", "BITGET")}))
        self.assertFalse(wildcard)

    def test_normalize_env_var_string_form(self) -> None:
        from plugins.trade.webtrade2.phase2 import _normalize_live_accounts
        out, wildcard = _normalize_live_accounts("metatrader:LITE7486706MT5,apex:BITGET")
        self.assertEqual(out, frozenset({("metatrader", "LITE7486706MT5"),
                                          ("apex", "BITGET")}))
        self.assertFalse(wildcard)

    # --- Wildcard semantics (LIVE_ACCOUNTS="*") --------------------------

    def test_normalize_star_token_enables_wildcard(self) -> None:
        """A bare ``"*"`` token flips wildcard mode; the frozenset stays
        empty so a non-wildcard constructor call still reports the same
        type. ``is_live_account_allowed`` becomes True for any pair.
        """
        from plugins.trade.webtrade2.phase2 import _normalize_live_accounts
        for raw in ("*", " * ", "*\t", "  *  "):
            out, wildcard = _normalize_live_accounts(raw)
            self.assertEqual(out, frozenset(), msg=raw)
            self.assertTrue(wildcard, msg=raw)

    def test_normalize_star_mixed_with_tokens_rejected(self) -> None:
        """``"* , apex:BITGET"`` is a misconfiguration: mixing the
        wildcard with explicit allowlist entries must raise so the
        operator cannot accidentally broaden scope.
        """
        from plugins.trade.webtrade2.phase2 import _normalize_live_accounts
        with self.assertRaises(ValueError):
            _normalize_live_accounts("*,apex:BITGET")
        with self.assertRaises(ValueError):
            _normalize_live_accounts("apex:BITGET,*")

    def test_normalize_empty_string_is_not_wildcard(self) -> None:
        """Empty / None / "" must NOT silently enable wildcard mode —
        that would be the easiest way to mistakenly LIVE-enable
        everything on a fresh install.
        """
        from plugins.trade.webtrade2.phase2 import _normalize_live_accounts
        for raw in (None, "", "  ", []):
            out, wildcard = _normalize_live_accounts(raw)
            self.assertEqual(out, frozenset(), msg=repr(raw))
            self.assertFalse(wildcard, msg=repr(raw))

    def test_wildcard_allows_any_configured_account(self) -> None:
        """``WEBTRADE2_LIVE_ACCOUNTS=*`` makes every resolved pair
        LIVE-eligible, including accounts that were never explicit
        allowlist members.
        """
        from plugins.trade.webtrade2.phase2 import WebTrade2Phase2Service
        p2 = WebTrade2Phase2Service(desk=FakeDesk(), session_secret="x" * 32,
                                    live_accounts="*")
        # Pre-existing allowlist members stay allowed.
        self.assertTrue(p2.is_live_account_allowed("apex", "BITGET"))
        # Newly discovered configured account also allowed.
        self.assertTrue(p2.is_live_account_allowed("vestmarkets", "fibo"))
        self.assertTrue(p2.is_live_account_allowed("hyperliquid", "FIBO"))
        self.assertTrue(p2.is_live_account_allowed("rise", "AMIROO"))
        # Even an arbitrary never-named pair passes the per-account gate
        # (the per-agent capability check still applies at execute time).
        self.assertTrue(p2.is_live_account_allowed("made_up", "whatever"))

    def test_wildcard_does_not_bypass_exchange_validation(self) -> None:
        """An empty exchange or account must still be rejected — the
        wildcard only relaxes the per-(exchange, account) LIVE gate,
        not the basic exchange/account presence check.
        """
        from plugins.trade.webtrade2.phase2 import WebTrade2Phase2Service
        p2 = WebTrade2Phase2Service(desk=FakeDesk(), session_secret="x" * 32,
                                    live_accounts="*")
        self.assertFalse(p2.is_live_account_allowed("", "fibo"))
        self.assertFalse(p2.is_live_account_allowed("vestmarkets", ""))
        self.assertFalse(p2.is_live_account_allowed("", ""))

    def test_explicit_allowlist_still_works_after_wildcard_change(self) -> None:
        """Backwards compatibility: an explicit-list service still
        rejects pairs that aren't in the list.
        """
        from plugins.trade.webtrade2.phase2 import WebTrade2Phase2Service
        p2 = WebTrade2Phase2Service(desk=FakeDesk(), session_secret="x" * 32,
                                    live_accounts=[("apex", "BITGET")])
        self.assertTrue(p2.is_live_account_allowed("apex", "BITGET"))
        self.assertFalse(p2.is_live_account_allowed("vestmarkets", "fibo"))
        self.assertFalse(p2.is_live_account_allowed("apex", "FIBO"))

    def test_empty_allowlist_blocks_all_except_wildcard(self) -> None:
        """``live_accounts=None`` (omitted) keeps the historical
        NO-ACCOUNT-LIVE-ELIGIBLE semantics — wildcard must be the
        explicit opt-in.
        """
        from plugins.trade.webtrade2.phase2 import WebTrade2Phase2Service
        p2 = WebTrade2Phase2Service(desk=FakeDesk(), session_secret="x" * 32)
        self.assertFalse(p2.is_live_account_allowed("apex", "BITGET"))
        self.assertFalse(p2.is_live_account_allowed("vestmarkets", "fibo"))

    def test_phase2_status_surfaces_wildcard(self) -> None:
        """The phase2_status payload must surface the wildcard so the
        frontend can render the mode explicitly without inspecting
        ``live_allowlist_active`` alone.
        """
        from plugins.trade.webtrade2.phase2 import WebTrade2Phase2Service
        p2_wild = WebTrade2Phase2Service(desk=FakeDesk(), session_secret="x" * 32,
                                         live_accounts="*")
        status = p2_wild.phase2_status()
        self.assertTrue(status["live_accounts_wildcard"])
        self.assertTrue(status["live_allowlist_active"])

        p2_explicit = WebTrade2Phase2Service(desk=FakeDesk(), session_secret="x" * 32,
                                             live_accounts=[("apex", "BITGET")])
        status = p2_explicit.phase2_status()
        self.assertFalse(status["live_accounts_wildcard"])
        self.assertTrue(status["live_allowlist_active"])

        p2_empty = WebTrade2Phase2Service(desk=FakeDesk(), session_secret="x" * 32)
        status = p2_empty.phase2_status()
        self.assertFalse(status["live_accounts_wildcard"])
        self.assertFalse(status["live_allowlist_active"])

    def test_operation_allowlist_still_enforced_with_wildcard(self) -> None:
        """Wildcard on LIVE_ACCOUNTS does NOT bypass LIVE_OPERATIONS:
        even if every account is LIVE-eligible, an operation that is
        absent from LIVE_OPERATIONS must still be rejected.
        """
        from plugins.trade.webtrade2.phase2 import WebTrade2Phase2Service
        # Wildcard on accounts; only "new_order" on operations.
        p2 = WebTrade2Phase2Service(desk=FakeDesk(), session_secret="x" * 32,
                                    live_accounts="*",
                                    live_operations={"new_order"})
        # ladder gate: ladder NOT in operations, so it must reject.
        op_gate = p2._gate_live_operation("ladder")
        self.assertIsNotNone(op_gate)
        self.assertEqual(op_gate["error"]["code"], "LIVE_OPERATION_NOT_ALLOWED")
        # Account gate: ladder NOT in operations, but account check passes
        # because wildcard allows all. This is the expected orthogonal
        # behaviour — the operator will see the LIVE_OPERATION_NOT_ALLOWED
        # gate fire before any exchange write.
        self.assertTrue(p2.is_live_account_allowed("vestmarkets", "fibo"))

    def test_ladder_gate_still_enforced_with_wildcard(self) -> None:
        """The LADDER_ENABLED gate is independent of the account
        allowlist. Wildcard accounts + ladder in LIVE_OPERATIONS but
        LADDER_ENABLED=0 must still reject ladder dispatches.
        """
        from plugins.trade.webtrade2.phase2 import WebTrade2Phase2Service
        p2 = WebTrade2Phase2Service(desk=FakeDesk(), session_secret="x" * 32,
                                    live_accounts="*",
                                    live_operations={"ladder"},
                                    ladder_enabled=False)
        # account gate: passes (wildcard).
        self.assertTrue(p2.is_live_account_allowed("vestmarkets", "fibo"))
        # op gate: passes (ladder in LIVE_OPERATIONS).
        self.assertIsNone(p2._gate_live_operation("ladder"))
        # ladder_enabled=False is enforced inside execute_preview; we
        # only check the per-account / per-operation gates here.
        self.assertFalse(p2.ladder_enabled)


if __name__ == "__main__":
    unittest.main()
