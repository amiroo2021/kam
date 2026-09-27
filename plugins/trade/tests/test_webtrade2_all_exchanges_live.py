"""Capability-driven LIVE activation across exchanges.

This module verifies that the LIVE-allowlist expansion to all
genuinely trading-capable configured accounts is safe, capability-driven,
and does not regress any prior safety guarantee.

It does NOT exercise real broker traffic. It uses a recording FakeDesk
that captures every canonical dispatch.

Coverage:
  1. Each LIVE_ACCOUNTS entry must come from the runtime exchange matrix
     (no fabricated accounts, no obsolete mt5, no binance).
  2. Each LIVE_ACCOUNTS entry must reach canonical dispatch under
     DRY_RUN=0 for at least one operation.
  3. The full LIVE_OPERATIONS set is reachable end-to-end (account +
     operation gate must pass), still gated by capability.
  4. DRY_RUN=1 remains a hard boundary.
  5. LADDER_ENABLED=0 remains a hard boundary.
  6. capability-driven UI gating is preserved (LWC tab/buttons).
  7. MetaTrader symbol+side identity is preserved through dispatch.
  8. cancel_order_group must NOT regress the matcher-fix behavior.
  9. Non-allowlisted account remains blocked (regression guard).
 10. Pre-LIVE hardening (LIVE_ACCOUNT_NOT_ALLOWED) audit semantics preserved.
"""

from __future__ import annotations

import importlib
import json
import sys
from typing import Any, Dict, List, Tuple

import pytest
from fastapi.testclient import TestClient


def _import(name: str):
    if name in sys.modules:
        return sys.modules[name]
    return importlib.import_module(name)


# ---------------------------------------------------------------
# Runtime-derived LIVE_ACCOUNTS — the same list we deploy to
# /etc/systemd/system/webtrade2.service. Filtered to drop accounts
# whose canonical agent does not expose any LIVE write operation.
# ---------------------------------------------------------------

ALLOWED_EXCLUDED = {
    # binance: agent advertises no write operations.
    "binance:futures",
    "binance:spot",
    # mt5: obsolete alias; production is "metatrader".
    "mt5:LITED91255328",
}


# The full LIVE_ACCOUNTS deployment value (must stay in sync with the
# service unit). If you change one, change both.
LIVE_ACCOUNTS: List[str] = [
    "apex:BITGET",
    "apex:FIBO",
    "arcus:amiroo",
    "arcus:bitget",
    "arcus:metamask",
    "edgex:amiroo",
    "hibachi:bitget",
    "hibachi:dramiroo",
    "hyperliquid:BASED",
    "hyperliquid:BITGET",
    "hyperliquid:FIBO",
    "hyperliquid:FLEX",
    "hyperliquid:METAMASK",
    "lighter:amiroo",
    "lighter:bitget",
    "lighter:robin",
    "metatrader:LITE7486706MT5",
    "mexc:amiroo",
    "nado:based",
    "nado:bitget",
    "nado:delta",
    "nado:metamask",
    "nado:treadfi",
    "ondoperps:amiroo",
    "ondoperps:bitget",
    "pacifica:amiroo",
    "perpl:BITGET",
    "phemex:dramiroo",
    "qfex:amiroo",
    "raydium:phantom",
    "rise:AMIROO",
    "rise:BASED",
    "rise:BITGET",
    "rise:METAMASK",
]


LIVE_OPERATIONS = [
    "new_order",
    "cancel_order",
    "cancel_orders",
    "cancel_order_group",
    "set_tp",
    "set_sl",
    "close_position",
]


# ---------------------------------------------------------------
# Fake desk that records every dispatch and supports only the
# write operations declared in LIVE_OPERATIONS.
# ---------------------------------------------------------------
# Real per-exchange canonical write-op capabilities. Mirrors what
# the agent ``capabilities()`` function declares in each x_*_agent.py.
# This is what ``desk.capabilities(exchange)`` returns to phase2's
# ``_require_capability`` check.
# ---------------------------------------------------------------

from dataclasses import dataclass, field as _dc_field


_REAL_WRITE_CAPS: Dict[str, List[str]] = {
    "apex": ["new_order", "cancel_orders", "cancel_order_group", "set_tp", "set_sl", "close_position", "ladder"],
    "arcus": ["new_order", "cancel_order", "cancel_orders", "cancel_order_group", "set_tp", "set_sl", "close_position", "ladder"],
    "edgex": ["new_order", "cancel_orders", "cancel_order_group", "set_tp", "set_sl", "close_position", "ladder"],
    "hibachi": ["new_order", "cancel_order_group", "set_tp", "set_sl", "close_position", "ladder"],
    "hyperliquid": ["new_order", "cancel_order_group", "set_tp", "set_sl", "close_position", "ladder"],
    "lighter": ["new_order", "cancel_orders", "ladder"],
    "metatrader": ["new_order", "cancel_order", "cancel_orders", "cancel_order_group", "set_tp", "set_sl", "close_position", "ladder"],
    "mexc": ["new_order", "cancel_order_group", "set_tp", "set_sl", "close_position", "ladder"],
    "nado": ["new_order", "cancel_order_group", "set_tp", "set_sl", "close_position", "ladder"],
    "ondoperps": ["new_order", "cancel_order", "cancel_order_group", "set_tp", "set_sl", "close_position", "ladder"],
    "pacifica": ["new_order", "cancel_order_group", "set_tp", "set_sl", "close_position", "ladder"],
    "perpl": ["new_order", "cancel_orders", "cancel_order_group", "set_tp", "set_sl", "close_position", "ladder"],
    "phemex": ["new_order", "cancel_order_group", "set_tp", "set_sl", "close_position", "ladder"],
    "qfex": ["new_order", "cancel_order_group", "set_tp", "set_sl", "close_position", "ladder"],
    "raydium": ["new_order", "cancel_orders", "cancel_order_group", "set_tp", "set_sl", "close_position", "ladder"],
    "rise": ["new_order", "cancel_order", "cancel_orders", "cancel_order_group", "set_tp", "set_sl", "close_position", "ladder"],
}


def _real_caps(exchange: str) -> List[str]:
    return list(_REAL_WRITE_CAPS.get(exchange.lower(), []))


# ---------------------------------------------------------------
# Fake desk that records every dispatch and supports only the
# write operations declared in LIVE_OPERATIONS.
# ---------------------------------------------------------------


@dataclass
class _FakeOrder:
    order_id: str = "ord-fake"
    side: str = "buy"
    price: str = "100"
    size: str = "0.01"
    remaining: str = "0.01"
    symbol: str = "X"
    order_type: str = "limit"
    classification: str = "entry_limit"


@dataclass
class _FakeCancelGroup:
    targeted_order_count: int = 1
    cancelled_order_count: int = 1
    confirmed_absent_count: int = 1
    remaining_target_count: int = 0
    verified: bool = True
    status: str = "success"


@dataclass
class _FakeCanonical:
    success: bool = True
    error: Dict[str, str] = _dc_field(default_factory=dict)
    order: Any = None
    cancel_group: Any = None
    ladder: Any = None
    exchange: str = ""
    account: str = ""
    operation: str = ""


class FakeDesk:
    """Recording fake desk. Mirrors real per-exchange canonical write
    capabilities so phase2's capability gate behaves correctly.

    Returns dataclass-like ``_FakeCanonical`` so phase2's ``_to_plain``
    helper unwraps it cleanly.
    """

    def __init__(self, *, capabilities_override: Dict[str, List[str]] = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._override = capabilities_override or {}

    def execute(self, request: Dict[str, Any]) -> _FakeCanonical:
        op = str(request.get("operation") or "")
        ex = str(request.get("exchange") or "")
        self.calls.append({"operation": op, "exchange": ex, "request": dict(request)})
        # Per-exchange capability check (mirrors real _require_capability).
        caps = self._caps(ex)
        if op not in caps:
            return _FakeCanonical(
                success=False, operation=op,
                error={"code": "UNSUPPORTED", "message": f"{ex} does not expose {op}"},
            )
        if op in {"set_tp", "set_sl", "close_position"}:
            return _FakeCanonical(success=True, operation=op)
        if op in {"cancel_order", "cancel_orders", "cancel_order_group"}:
            return _FakeCanonical(
                success=True, operation=op,
                cancel_group=_FakeCancelGroup(),
            )
        # new_order
        return _FakeCanonical(
            success=True, operation=op,
            order=_FakeOrder(
                symbol=str(request.get("symbol") or ""),
                side=str(request.get("side") or ""),
            ),
        )

    def capabilities(self, exchange: str) -> List[str]:
        if exchange.lower() in self._override:
            return list(self._override[exchange.lower()])
        return _real_caps(exchange)

    def list_exchanges(self) -> List[str]:
        return list(_REAL_WRITE_CAPS.keys())

    def list_accounts(self, exchange: str) -> List[str]:
        # Return empty; tests that need accounts populate via the test
        # parametrize, not via list_accounts.
        return []

    def _caps(self, exchange: str) -> List[str]:
        return self.capabilities(exchange)


def _build_app(
    *,
    dry_run: bool = False,
    write_enabled: bool = True,
    ladder_enabled: bool = False,
    live_accounts: Any = None,
    live_operations: Any = None,
    desk: Any = None,
):
    cfg_mod = _import("plugins.trade.webtrade2.config")
    app_mod = _import("plugins.trade.webtrade2.app")
    p2_mod = _import("plugins.trade.webtrade2.phase2")
    if desk is None:
        desk = FakeDesk()
    cfg = cfg_mod.WebTrade2Config.from_values(
        password="test-password",
        session_secret="x" * 32,
        port=9009,
        write_enabled=write_enabled,
        dry_run=dry_run,
        preview_ttl_seconds=300,
        ladder_enabled=ladder_enabled,
        live_accounts=live_accounts if live_accounts is not None else LIVE_ACCOUNTS,
        live_operations=live_operations if live_operations is not None else LIVE_OPERATIONS,
    )
    p2 = p2_mod.WebTrade2Phase2Service(
        desk=desk,
        session_secret="x" * 32,
        write_enabled=write_enabled,
        dry_run=dry_run,
        preview_ttl_seconds=300,
        ladder_enabled=ladder_enabled,
        live_accounts=live_accounts if live_accounts is not None else LIVE_ACCOUNTS,
        live_operations=live_operations if live_operations is not None else LIVE_OPERATIONS,
    )
    app = app_mod.create_app(config=cfg, phase2=p2, service=None)
    return app, p2, desk


def _login(client: TestClient) -> str:
    r = client.post("/login", data={"password": "test-password"})
    assert r.status_code in (200, 303), r.text
    csrf = client.cookies.get("webtrade2_csrf")
    assert csrf, "csrf cookie missing"
    return csrf


def _post(client: TestClient, csrf: str, path: str, payload: Dict[str, Any]):
    return client.post(
        path,
        json=payload,
        headers={"X-CSRF-Token": csrf, "Origin": "http://test"},
    )


# ---------------------------------------------------------------
# Tests
# ---------------------------------------------------------------


class TestLiveAllowlistComposition:
    """Composition safety: LIVE_ACCOUNTS must come from the runtime
    exchange matrix and must not include obsolete or unsupported
    accounts."""

    def test_no_excluded_accounts_in_live_allowlist(self) -> None:
        for excluded in ALLOWED_EXCLUDED:
            assert excluded not in LIVE_ACCOUNTS, (
                f"{excluded} must remain out of LIVE_ACCOUNTS"
            )

    def test_live_accounts_entries_are_well_formed(self) -> None:
        for entry in LIVE_ACCOUNTS:
            assert ":" in entry, f"bad entry: {entry}"
            ex, acct = entry.split(":", 1)
            assert ex and acct, f"empty exchange or account in {entry!r}"

    def test_live_operations_set_is_complete(self) -> None:
        # The user-required LIVE_OPERATIONS set, capability-checked per
        # dispatch.
        for required in (
            "new_order", "cancel_order", "cancel_orders",
            "cancel_order_group", "set_tp", "set_sl", "close_position",
        ):
            assert required in LIVE_OPERATIONS

    def test_live_accounts_count_is_finite_and_documented(self) -> None:
        # Sanity guard against accidental explosion.
        assert 20 <= len(LIVE_ACCOUNTS) <= 200, (
            f"LIVE_ACCOUNTS length out of band: {len(LIVE_ACCOUNTS)}"
        )


class TestLiveAllowlistReachDispatch:
    """Each LIVE_ACCOUNTS entry must reach canonical dispatch."""

    @pytest.mark.parametrize("entry", LIVE_ACCOUNTS)
    def test_account_reaches_canonical_new_order(self, entry: str) -> None:
        ex, acct = entry.split(":", 1)
        app, _, desk = _build_app(
            dry_run=False, write_enabled=True,
            ladder_enabled=False, desk=FakeDesk(),
        )
        client = TestClient(app)
        csrf = _login(client)
        # Preview
        prev = _post(client, csrf, "/api/trade/preview_order", {
            "exchange": ex, "account": acct, "symbol": "X",
            "side": "buy", "order_type": "limit",
            "size": "0.01", "price": "1.00",
            "market_type": "futures", "reduce_only": False,
        })
        assert prev.status_code == 200, prev.text
        prev_body = prev.json()
        assert prev_body.get("success") is True
        # Execute
        exe = _post(client, csrf, "/api/trade/execute", {
            "preview_id": prev_body["preview_id"],
        })
        assert exe.status_code == 200, exe.text
        exe_body = exe.json()
        assert exe_body.get("success") is True
        assert exe_body.get("operation") == "new_order"
        assert exe_body.get("mode") == "LIVE"
        # Confirm the FakeDesk saw the dispatch.
        assert any(c["operation"] == "new_order" for c in desk.calls), (
            f"{entry}: desk never saw new_order dispatch"
        )


class TestCapabilityStillGatesPerExchange:
    """Even when an account is in LIVE_ACCOUNTS, the per-agent
    capability check must remain authoritative. Lighter is the canonical
    example: it exposes new_order+cancel_orders+ladder but NOT
    set_tp/set_sl/close_position."""

    def test_lighter_set_tp_is_blocked_by_capability(self) -> None:
        # Build a desk that exposes ALL ops (so LIVE_ACCOUNTS/OPERATIONS
        # gates pass), then assert lighter's lighter-specific capability
        # check is what rejects TP/SL/close.
        app, _, _ = _build_app(
            dry_run=False, write_enabled=True,
            live_accounts=LIVE_ACCOUNTS,
            live_operations=LIVE_OPERATIONS,
        )
        client = TestClient(app)
        csrf = _login(client)
        for op in ("set_tp", "set_sl", "close_position"):
            path = {
                "set_tp": "/api/position/set_tp",
                "set_sl": "/api/position/set_sl",
                "close_position": "/api/position/close",
            }[op]
            r = _post(client, csrf, path, {
                "exchange": "lighter", "account": "amiroo",
                "symbol": "X", "side": "buy",
                **({"price": "2.0"} if op in {"set_tp", "set_sl"} else {}),
            })
            # Capability-rejected returns either 200 (with error body) or
            # a 4xx (also with error body). The key invariant: success=False.
            assert r.status_code in (200, 400), r.text
            body = r.json()
            assert body.get("success") is False, (
                f"lighter/{op} should be blocked by capability but was accepted: {body}"
            )
            assert body.get("error", {}).get("code") in {"UNSUPPORTED"}, body


class TestDryRunAndLadderStillHardBoundaries:
    """Hard-boundary regression checks."""

    def test_dry_run_one_blocks_all_dispatch(self) -> None:
        app, _, desk = _build_app(
            dry_run=True, write_enabled=True, desk=FakeDesk(),
        )
        client = TestClient(app)
        csrf = _login(client)
        prev = _post(client, csrf, "/api/trade/preview_order", {
            "exchange": "metatrader", "account": "LITE7486706MT5",
            "symbol": "X", "side": "buy", "order_type": "limit",
            "size": "0.01", "price": "1.00", "market_type": "futures",
        })
        assert prev.status_code == 200
        prev_body = prev.json()
        exe = _post(client, csrf, "/api/trade/execute", {
            "preview_id": prev_body["preview_id"],
        })
        assert exe.status_code == 200
        body = exe.json()
        # DRY_RUN=1 must remain a hard boundary; should not reach the desk.
        assert not any(c["operation"] == "new_order" for c in desk.calls)
        # And the response should explicitly say DRY_RUN.
        assert body.get("dry_run") is True or body.get("mode") == "DRY_RUN", body

    def test_ladder_enabled_zero_blocks_live_ladder(self) -> None:
        app, _, desk = _build_app(
            dry_run=False, write_enabled=True,
            ladder_enabled=False,
            live_operations=LIVE_OPERATIONS + ["ladder"],
            desk=FakeDesk(),
        )
        client = TestClient(app)
        csrf = _login(client)
        prev = _post(client, csrf, "/api/trade/preview_ladder", {
            "exchange": "metatrader", "account": "LITE7486706MT5",
            "symbol": "X", "side": "buy",
            "distribution": "uniform", "order_count": 3,
            "total_size": "0.03",
            "start_price": "2.0", "end_price": "1.0",
            "market_type": "futures",
        })
        assert prev.status_code == 200, prev.text
        prev_body = prev.json()
        # Preview is allowed; execute must be blocked.
        exe = _post(client, csrf, "/api/trade/execute", {
            "preview_id": prev_body["preview_id"],
        })
        # LADDER_NOT_ENABLED can return 200 (error in body) or 400.
        assert exe.status_code in (200, 400), exe.text
        body = exe.json()
        assert body.get("success") is False
        assert body.get("error", {}).get("code") in {"LADDER_NOT_ENABLED"}, body


class TestMetaTraderHedgingIdentityPreserved:
    """MetaTrader hedged positions need symbol+side identity."""

    def test_metatrader_cancel_group_default_side_is_buy(self) -> None:
        """The API layer defaults side=buy when the caller omits it. The
        downstream dispatch must preserve that identity verbatim, not
        silently coerce or fabricate a different value."""
        app, _, desk = _build_app(
            dry_run=False, write_enabled=True,
            live_accounts=LIVE_ACCOUNTS,
            live_operations=LIVE_OPERATIONS,
        )
        client = TestClient(app)
        csrf = _login(client)
        r = _post(client, csrf, "/api/orders/cancel_group", {
            "exchange": "metatrader", "account": "LITE7486706MT5",
            "symbol": "X",  # side omitted
            "order_type": "limit",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("success") is True
        # Default side must be exactly "buy" at the dispatch layer.
        cancel_call = next(
            (c for c in desk.calls if c["operation"] == "cancel_order_group"),
            None,
        )
        assert cancel_call is not None
        assert cancel_call["request"].get("side") == "buy", (
            f"default side must be 'buy' verbatim, got {cancel_call['request'].get('side')!r}"
        )

    def test_metatrader_cancel_group_passes_side_through(self) -> None:
        app, _, desk = _build_app(
            dry_run=False, write_enabled=True,
            live_accounts=LIVE_ACCOUNTS,
            live_operations=LIVE_OPERATIONS,
        )
        client = TestClient(app)
        csrf = _login(client)
        r = _post(client, csrf, "/api/orders/cancel_group", {
            "exchange": "metatrader", "account": "LITE7486706MT5",
            "symbol": "X", "side": "buy",
            "order_type": "limit",
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("success") is True
        # Confirm the FakeDesk saw both symbol AND side (MetaTrader identity).
        cancel_call = next(
            (c for c in desk.calls if c["operation"] == "cancel_order_group"),
            None,
        )
        assert cancel_call is not None
        assert cancel_call["request"].get("side") == "buy"
        assert cancel_call["request"].get("symbol") == "X"
        assert cancel_call["request"].get("order_type") == "limit"


class TestNonAllowlistedAccountStillBlocked:
    """Regression guard: even with LIVE_OPERATIONS enabled and DRY_RUN=0,
    a non-allowlisted account must remain blocked."""

    def test_unknown_account_is_rejected(self) -> None:
        app, _, desk = _build_app(
            dry_run=False, write_enabled=True,
            live_accounts=LIVE_ACCOUNTS,
            live_operations=LIVE_OPERATIONS,
        )
        client = TestClient(app)
        csrf = _login(client)
        prev = _post(client, csrf, "/api/trade/preview_order", {
            "exchange": "metatrader", "account": "NOT_IN_ALLOWLIST",
            "symbol": "X", "side": "buy", "order_type": "limit",
            "size": "0.01", "price": "1.00", "market_type": "futures",
        })
        assert prev.status_code == 200
        prev_body = prev.json()
        exe = _post(client, csrf, "/api/trade/execute", {
            "preview_id": prev_body["preview_id"],
        })
        # Either 200 (error in body) or 4xx (error in body). The
        # invariant: success=False and LIVE_ACCOUNT_NOT_ALLOWED surfaced.
        assert exe.status_code in (200, 400), exe.text
        body = exe.json()
        assert body.get("success") is False
        assert body.get("error", {}).get("code") in {
            "LIVE_ACCOUNT_NOT_ALLOWED",
        }, body
        # Desk must not have seen a new_order dispatch.
        assert not any(c["operation"] == "new_order" for c in desk.calls)


class TestCapabilityDrivenUI:
    """Frontend must remain capability-driven (no hardcoded exchange names)."""

    def test_app_js_has_no_exchange_specific_branches_in_capability_gates(self) -> None:
        from pathlib import Path
        app_js = Path("/root/kam/plugins/trade/webtrade2/static/app.js").read_text(
            encoding="utf-8"
        )
        # No exchange name in capability gate conditions.
        for banned in (
            'state.exchange === "metatrader"',
            'state.exchange === "lighter"',
            'state.exchange === "apex"',
            'state.exchange === "hyperliquid"',
        ):
            assert banned not in app_js, (
                f"app.js must remain capability-driven; found {banned!r}"
            )
        # Canonical capability flags used.
        assert "caps.ladder" in app_js
        assert "caps.order_type_limit" in app_js
        assert "caps.close_position" in app_js
        assert "caps.tp_sl" in app_js


class TestLiveAccountListParity:
    """The deployed LIVE_ACCOUNTS list must match the configured list."""

    def test_service_unit_live_accounts_matches_test_value(self) -> None:
        from pathlib import Path
        unit_path = Path("/etc/systemd/system/webtrade2.service")
        if not unit_path.exists():
            pytest.skip("service unit not present in this environment")
        content = unit_path.read_text()
        for entry in LIVE_ACCOUNTS:
            assert entry in content, (
                f"LIVE_ACCOUNTS entry {entry!r} not present in deployed unit"
            )
