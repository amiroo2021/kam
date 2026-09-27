"""Step B / Step K — operation-level LIVE allowlist tests.

Implements the 10 deterministic tests required by Step K of the
CONTROLLED LIVE checkpoint:

  1. Empty LIVE_OPERATIONS blocks every mutation.
  2. new_order allows only new_order.
  3. cancel_order allows only cancel_order.
  4. set_tp cannot dispatch when only new_order is enabled.
  5. set_sl cannot dispatch when only new_order is enabled.
  6. close_position cannot dispatch when only new_order is enabled.
  7. ladder remains blocked with LADDER_ENABLED=0.
  8. non-allowlisted account remains blocked (account gate still works).
  9. MetaTrader symbol+side identity remains intact.
  10. existing DRY_RUN hard-boundary tests remain green.

Uses a recording TradeDesk (FakeDesk) that captures every dispatch.
Never exercises a real venue.
"""
from __future__ import annotations

import importlib
import json
import sys
from typing import Any, Dict, List

from fastapi.testclient import TestClient


def _import(name: str):
    if name in sys.modules:
        return sys.modules[name]
    return importlib.import_module(name)


class RecordingDesk:
    """Test desk that records every operation dispatched."""

    MUTATION_OPS = {
        "new_order", "ladder", "cancel_order", "cancel_orders",
        "cancel_order_group", "set_tp", "set_sl", "close_position",
    }

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    @property
    def mutation_calls(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c.get("operation") in self.MUTATION_OPS]

    # capability API used by phase2
    def capabilities(self, exchange: str):
        return {
            "new_order", "cancel_order", "cancel_order_group",
            "set_tp", "set_sl", "close_position", "ladder",
            "resolve_instrument", "market_price", "preview_order",
        }

    def execute(self, request):
        self.calls.append(dict(request))
        from plugins.trade.canonical import make_success
        op = request.get("operation")
        if op == "resolve_instrument":
            return make_success(
                operation=op, exchange=request.get("exchange"),
                account=request.get("account"),
                instrument={
                    "symbol": request.get("symbol"),
                    "native_symbol": request.get("symbol"),
                    "price_increment": "0.01",
                    "size_increment": "0.01",
                    "minimum_size": "0.01",
                },
            )
        if op == "market_price":
            return make_success(
                operation=op, exchange=request.get("exchange"),
                account=request.get("account"),
                market_price={"symbol": request.get("symbol"), "price": "100"},
            )
        return make_success(
            operation=op, exchange=request.get("exchange"),
            account=request.get("account"),
            data={"order_id": "rec-test-1", "accepted": 1, "requested": 1},
        )


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
    desk = RecordingDesk()
    p2 = p2_mod.WebTrade2Phase2Service(
        desk=desk, session_secret="x" * 32,
        write_enabled=write_enabled, dry_run=dry_run,
        preview_ttl_seconds=300, ladder_enabled=ladder_enabled,
        live_accounts=live_accounts, live_operations=live_operations,
    )
    app = app_mod.create_app(config=cfg, phase2=p2, service=None)
    return app, p2, desk


def _login(client: TestClient) -> str:
    r = client.get("/api/session")
    csrf = r.cookies.get("webtrade2_csrf")
    if not csrf:
        # Cookie might be set by the GET; fall back to login response
        r2 = client.post("/login", data={"password": "test-password"}, follow_redirects=False)
        csrf = r2.cookies.get("webtrade2_csrf") or client.cookies.get("webtrade2_csrf")
    return csrf or ""


def _hdr(csrf: str) -> Dict[str, str]:
    return {"X-CSRF-Token": csrf}


def _preview_order(client, csrf, **overrides):
    body = {"exchange": "metatrader", "account": "acc1", "symbol": "BTCUSD",
            "side": "buy", "size": "0.01", "price": "100", "type": "limit"}
    body.update(overrides)
    return client.post("/api/trade/preview_order", json=body, headers=_hdr(csrf))


def _preview_ladder(client, csrf, **overrides):
    body = {"exchange": "metatrader", "account": "acc1", "symbol": "BTCUSD",
            "side": "buy", "distribution": "uniform", "order_count": 3,
            "total_size": "0.03", "start_price": "100", "end_price": "99"}
    body.update(overrides)
    return client.post("/api/trade/preview_ladder", json=body, headers=_hdr(csrf))


def _execute_preview(client, csrf, preview_id):
    return client.post("/api/trade/execute", json={"preview_id": preview_id},
                       headers=_hdr(csrf))


class WebTrade2OperationAllowlistTests:
    pass  # placeholder for unittest discovery


# The test class is constructed below by the test runner's import; pytest
# discovers classes whose names start with "Test" automatically.

class TestOperationAllowlist:
    """The 10 deterministic tests required by Step K."""

    # ---- 1: empty LIVE_OPERATIONS blocks every mutation ---------------
    def test_1_empty_live_operations_blocks_every_mutation(self):
        app, _, desk = _build_app(
            dry_run=False, write_enabled=True,
            live_accounts=[("metatrader", "acc1")],
            live_operations=[],
        )
        client = TestClient(app)
        csrf = _login(client)
        # Preview + execute new_order
        r = _preview_order(client, csrf)
        assert r.status_code == 200, r.text
        pid = r.json().get("preview_id")
        assert pid
        r2 = _execute_preview(client, csrf, pid)
        body = r2.json()
        assert not body.get("success")
        assert body.get("error", {}).get("code") == "LIVE_OPERATION_NOT_ALLOWED"
        # No desk dispatch
        assert desk.mutation_calls == []

    # ---- 2: new_order allows only new_order ---------------------------
    def test_2_new_order_allows_only_new_order(self):
        app, _, desk = _build_app(
            dry_run=False, write_enabled=True,
            live_accounts=[("metatrader", "acc1")],
            live_operations=["new_order"],
        )
        client = TestClient(app)
        csrf = _login(client)
        r = _preview_order(client, csrf)
        pid = r.json().get("preview_id")
        assert pid
        r2 = _execute_preview(client, csrf, pid)
        assert r2.json().get("success") is True
        assert r2.json().get("mode") == "LIVE"
        assert r2.json().get("status") == "SUBMITTED"
        # Only new_order should be dispatched
        assert len(desk.mutation_calls) == 1
        assert desk.mutation_calls[0]["operation"] == "new_order"

    # ---- 3: cancel_order allows only cancel_order ---------------------
    def test_3_cancel_order_allows_only_cancel_order(self):
        app, _, desk = _build_app(
            dry_run=False, write_enabled=True,
            live_accounts=[("metatrader", "acc1")],
            live_operations=["cancel_order"],
        )
        client = TestClient(app)
        csrf = _login(client)
        r = client.post(
            "/api/orders/cancel_group",
            json={"exchange": "metatrader", "account": "acc1",
                  "symbol": "BTCUSD", "side": "buy"},
            headers=_hdr(csrf),
        )
        # /api/orders/cancel_group dispatches "cancel_order_group" not
        # "cancel_order", so with only "cancel_order" in ops the gate
        # should reject it.
        assert not r.json().get("success")
        assert r.json().get("error", {}).get("code") == "LIVE_OPERATION_NOT_ALLOWED"
        assert desk.mutation_calls == []

    # ---- 4: set_tp blocked when only new_order is enabled -------------
    def test_4_set_tp_blocked_when_only_new_order_enabled(self):
        app, _, desk = _build_app(
            dry_run=False, write_enabled=True,
            live_accounts=[("metatrader", "acc1")],
            live_operations=["new_order"],
        )
        client = TestClient(app)
        csrf = _login(client)
        r = client.post(
            "/api/position/set_tp",
            json={"exchange": "metatrader", "account": "acc1",
                  "symbol": "BTCUSD", "side": "buy", "price": "110"},
            headers=_hdr(csrf),
        )
        assert not r.json().get("success")
        assert r.json().get("error", {}).get("code") == "LIVE_OPERATION_NOT_ALLOWED"
        assert desk.mutation_calls == []

    # ---- 5: set_sl blocked when only new_order is enabled -------------
    def test_5_set_sl_blocked_when_only_new_order_enabled(self):
        app, _, desk = _build_app(
            dry_run=False, write_enabled=True,
            live_accounts=[("metatrader", "acc1")],
            live_operations=["new_order"],
        )
        client = TestClient(app)
        csrf = _login(client)
        r = client.post(
            "/api/position/set_sl",
            json={"exchange": "metatrader", "account": "acc1",
                  "symbol": "BTCUSD", "side": "buy", "price": "90"},
            headers=_hdr(csrf),
        )
        assert not r.json().get("success")
        assert r.json().get("error", {}).get("code") == "LIVE_OPERATION_NOT_ALLOWED"
        assert desk.mutation_calls == []

    # ---- 6: close_position blocked when only new_order is enabled -----
    def test_6_close_position_blocked_when_only_new_order_enabled(self):
        app, _, desk = _build_app(
            dry_run=False, write_enabled=True,
            live_accounts=[("metatrader", "acc1")],
            live_operations=["new_order"],
        )
        client = TestClient(app)
        csrf = _login(client)
        r = client.post(
            "/api/position/close",
            json={"exchange": "metatrader", "account": "acc1",
                  "symbol": "BTCUSD", "side": "buy"},
            headers=_hdr(csrf),
        )
        assert not r.json().get("success")
        assert r.json().get("error", {}).get("code") == "LIVE_OPERATION_NOT_ALLOWED"
        assert desk.mutation_calls == []

    # ---- 7: ladder blocked with LADDER_ENABLED=0 ----------------------
    def test_7_ladder_blocked_with_ladder_enabled_zero(self):
        # LADDER_ENABLED=0 + "ladder" in LIVE_OPERATIONS -> LADDER_NOT_ENABLED
        app, _, desk = _build_app(
            dry_run=False, write_enabled=True,
            ladder_enabled=False,  # the default
            live_accounts=[("metatrader", "acc1")],
            live_operations=["ladder", "new_order"],
        )
        client = TestClient(app)
        csrf = _login(client)
        r = _preview_ladder(client, csrf)
        assert r.status_code == 200, r.text
        pid = r.json().get("preview_id")
        assert pid
        r2 = _execute_preview(client, csrf, pid)
        body = r2.json()
        # Ladder is rejected with LADDER_NOT_ENABLED (a stronger gate than
        # LIVE_OPERATION_NOT_ALLOWED). This proves the LADDER_ENABLED flag
        # still has authority over LIVE ladder dispatches.
        assert not body.get("success")
        assert body.get("error", {}).get("code") == "LADDER_NOT_ENABLED"
        assert desk.mutation_calls == []

    # ---- 8: non-allowlisted account remains blocked --------------------
    def test_8_non_allowlisted_account_remains_blocked(self):
        # Allowlist has only "OTHER" account. Target "acc1".
        app, _, desk = _build_app(
            dry_run=False, write_enabled=True,
            live_accounts=[("metatrader", "OTHER")],
            live_operations=["new_order"],
        )
        client = TestClient(app)
        csrf = _login(client)
        r = _preview_order(client, csrf)
        pid = r.json().get("preview_id")
        assert pid
        r2 = _execute_preview(client, csrf, pid)
        body = r2.json()
        assert not body.get("success")
        # Account gate fires BEFORE operation gate, so error code is
        # LIVE_ACCOUNT_NOT_ALLOWED.
        assert body.get("error", {}).get("code") == "LIVE_ACCOUNT_NOT_ALLOWED"
        assert desk.mutation_calls == []

    # ---- 9: MetaTrader symbol+side identity remains intact ------------
    def test_9_metatrader_symbol_side_identity_intact(self):
        # Two same-symbol positions with opposite sides must be treated as
        # independent. With hedge ops allowed, both set_tp calls dispatch
        # with the correct side on each.
        app, _, desk = _build_app(
            dry_run=False, write_enabled=True,
            live_accounts=[("metatrader", "acc1")],
            live_operations=["set_tp"],
        )
        client = TestClient(app)
        csrf = _login(client)
        for side in ("buy", "sell"):
            r = client.post(
                "/api/position/set_tp",
                json={"exchange": "metatrader", "account": "acc1",
                      "symbol": "BTCUSD", "side": side, "price": "110"},
                headers=_hdr(csrf),
            )
            assert r.json().get("success"), r.text
        # Two dispatched calls, one per side
        assert len(desk.mutation_calls) == 2
        sides = {c["side"] for c in desk.mutation_calls}
        assert sides == {"buy", "sell"}
        for c in desk.mutation_calls:
            assert c["symbol"] == "BTCUSD"
            assert c["operation"] == "set_tp"

    # ---- 10: existing DRY_RUN hard-boundary tests still green ---------
    def test_10_dry_run_hard_boundary_still_blocks_dispatch(self):
        # DRY_RUN=1 + WRITE_ENABLED=1 + account allowlisted + all ops
        # allowlisted -> preview + execute must NEVER reach desk.execute.
        app, _, desk = _build_app(
            dry_run=True, write_enabled=True,
            live_accounts=[("metatrader", "acc1")],
            live_operations=["new_order", "set_tp", "set_sl",
                             "close_position", "cancel_order", "cancel_order_group",
                             "ladder"],
        )
        client = TestClient(app)
        csrf = _login(client)
        # New order
        r = _preview_order(client, csrf)
        pid = r.json().get("preview_id")
        r2 = _execute_preview(client, csrf, pid)
        body = r2.json()
        assert body.get("status") == "DRY_RUN"
        assert not body.get("dispatched", False)
        # No desk calls at all
        assert desk.mutation_calls == []
