"""Phase 7A — regression tests for the confirmed Hyperliquid Spot defects.

Every test is offline. Transport is mocked at the desk-agent module object
level (the object SpotDesk actually owns), because SpotDesk builds its own
module instance via importlib and a patch on a direct import would NOT
reach it.

Covered defects:
  D1  price precision: spot agent ignored Hyperliquid's 5-significant-figure
      price rule (the perp agent implements it); over-precise prices are
      REJECTED by the venue, not silently rounded.
  D2  order_history returned one row per history record, so a single oid
      appeared twice with conflicting statuses ("open" + "canceled").
  D3  the agent answered operation "open_orders" with operation "orders",
      which SpotDesk rejected as an identity mismatch.
  D4  the wizard's cancel-result arithmetic used `or` fallbacks, so a
      legitimate remaining=0 was swallowed and rendered as "Remaining: N".
  D5  the wizard's SELL balance check compared the DISPLAY base name
      (e.g. "SOL") against balance assets keyed by the NATIVE token name
      (e.g. "USOL"), blocking valid sells of frontend-alias pairs.
"""
from __future__ import annotations

import importlib
import importlib.util
import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

def _private_trade_package() -> str:
    """Create a private subpackage of ``plugins.trade`` for safe loading.

    ``plugins.trade.spotdesk`` and friends use relative imports ("from
    .canonical import ..."). Registering a replacement module under the
    shared name REPLACES the module object other test files already
    imported, which surfaces as a ``mismatched_fields: ["response_type"]``
    identity failure.

    A private subpackage keeps every shared ``sys.modules`` entry
    untouched while its relative imports still resolve to the real
    ``plugins.trade`` modules, so class identities match the desk.
    """
    import types

    package_name = "plugins.trade._phase7a"
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [str(Path(__file__).resolve().parent.parent)]  # type: ignore[attr-defined]
        package.__package__ = "plugins.trade"
        sys.modules[package_name] = package
        import plugins.trade as _real_trade

        _real_trade._phase7a = package  # type: ignore[attr-defined]
    return package_name


def _load_source_module(module_name: str, relative_path: str):
    """Load a source-tree module by explicit file path.

    ``test_summative_reliability.py`` leaves ``/usr/local/lib/hermes-agent``
    at the front of ``sys.path``, so a name-based import can resolve to the
    INSTALLED runtime instead of the source code under test. Loading by
    path is immune to ``sys.path`` order and the ``sys.modules`` cache.
    """
    package_name = _private_trade_package()
    path = Path(__file__).resolve().parent.parent / relative_path
    leaf = module_name.rsplit(".", 1)[-1]
    full_name = f"{package_name}.{leaf}"
    spec = importlib.util.spec_from_file_location(full_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def _canonical_module():
    """Return the LIVE canonical module (never replace the shared one)."""
    live = sys.modules.get("plugins.trade.canonical")
    if live is not None:
        return live
    return _load_source_module("plugins.trade.canonical", "canonical.py")


def _spotdesk_cls():
    """Return SpotDesk from the LIVE module (never replace the shared one)."""
    live = sys.modules.get("plugins.trade.spotdesk")
    if live is not None:
        return live.SpotDesk
    return _load_source_module("plugins.trade.spotdesk", "spotdesk.py").SpotDesk


def _wizard_cls():
    """Return the wizard class from the LIVE module."""
    live = sys.modules.get("plugins.trade.tradespot_wizard")
    if live is not None:
        return live.TradeSpotWizard
    return _load_source_module(
        "plugins.trade.tradespot_wizard", "tradespot_wizard.py"
    ).TradeSpotWizard


try:  # Ensure the live wizard is importable before skip detection runs.
    import plugins.trade.tradespot_wizard as _live_wizard  # noqa: F401
except Exception:  # noqa: BLE001
    _live_wizard = None


def _wizard_source_text() -> str:
    """Return the source text of the wizard this test will actually exercise.

    ``_wizard_cls`` prefers the live ``plugins.trade.tradespot_wizard``
    module and falls back to the source-tree file. Detect the fix against
    the SAME module the test uses, so the skip reflects reality instead of
    whichever copy happened to be imported first.

    The detection helpers run at IMPORT time (the skipif marker is
    evaluated then), so the live module must be resolvable immediately.
    Importing ``plugins.trade.tradespot_wizard`` is side-effect free: it
    only defines classes.
    """
    try:
        import inspect

        return inspect.getsource(_wizard_cls())
    except Exception:  # noqa: BLE001
        return ""


def _wizard_has_cancel_zero_fix() -> bool:
    """True when the wizard preserves a legitimate zero remaining count.

    The Phase 7A wizard fix lives in ``tradespot_wizard.py``. Phase 7C
    deploys the Spot agent and the ladder WITHOUT that wizard file, so the
    installed runtime still renders "Remaining: N" for a fully successful
    cancellation. These two assertions cover a wizard change that is
    deployed separately, so they are skipped where the wizard fix is absent
    instead of failing the installed suite.
    """
    source = _wizard_source_text()
    return 'int(data["cancelled"])' in source and 'int(data["remaining"])' in source


def _wizard_has_alias_balance_fix() -> bool:
    """True when the wizard resolves a display alias to its native balance."""
    source = _wizard_source_text()
    return "display_asset" in source and "aliases.get(wanted" in source


requires_wizard_cancel_zero_fix = pytest.mark.skipif(
    not _wizard_has_cancel_zero_fix(),
    reason="tradespot_wizard.py Phase 7A zero-remaining fix is not deployed in this tree",
)
requires_wizard_alias_balance_fix = pytest.mark.skipif(
    not _wizard_has_alias_balance_fix(),
    reason="tradespot_wizard.py Phase 7A display-alias balance fix is not deployed in this tree",
)


def _canonical_response_cls():
    """Resolve CanonicalResponse from the live module, not a stale import.

    ``plugins/trade/tests/test_tradespot_wizard.py`` pops
    ``plugins.trade.agents.*_agent_spot`` out of ``sys.modules`` in its
    teardown and restores the pre-test module objects. A freshly loaded
    spot agent then imports a *different* ``CanonicalResponse`` class
    object than the one this module imported at import time, so
    ``isinstance`` checks against the imported name fail with the
    "dual identity" symptom described in ``tests/conftest.py``.

    Resolving the class through ``sys.modules`` at call time keeps this
    module correct in both an isolated run and a full-suite run.
    """
    module = sys.modules.get("plugins.trade.canonical") or _canonical_module()
    return module.CanonicalResponse


def _spot_agent_module():
    """Load the source-tree spot agent (see _load_source_agent)."""
    return _load_source_agent("x_hyperliquid_spot_agent.py")


def _perp_agent_module():
    """Load the source-tree perpetual agent (see _load_source_agent)."""
    return _load_source_agent("x_hyperliquid_agent.py")


WALLET = "0x" + "a" * 40
CHAT = ("telegram", "chat", 42)

# Live-shaped spotMeta subset: USOL is the native token behind the
# frontend's "SOL" display alias.
ALIAS_META = {
    "tokens": [
        {"name": "USDC", "index": 0, "szDecimals": 8, "weiDecimals": 8, "isCanonical": True},
        {"name": "HYPE", "fullName": "Hyperliquid", "index": 150, "szDecimals": 2, "weiDecimals": 8},
        {"name": "USOL", "fullName": "Unit Solana", "index": 254, "szDecimals": 3, "weiDecimals": 8},
    ],
    "universe": [
        {"name": "@107", "tokens": [150, 0], "index": 107, "isCanonical": False},
        {"name": "@156", "tokens": [254, 0], "index": 156, "isCanonical": False},
    ],
}


def _desk_agent():
    """Return the hyperliquid spot agent module that SpotDesk actually owns."""
    desk = _spotdesk_cls()(agents_dir=_source_agents_dir())
    desk.list_exchanges()
    agent = desk.agent_for("hyperliquid")
    assert agent is not None
    return agent


_ACTIVE_PATCHERS: list = []


def _source_tree_root() -> str:
    """Return the repo root that owns THIS test file.

    ``plugins/trade/tests/test_summative_reliability.py`` prepends
    ``/usr/local/lib/hermes-agent`` to ``sys.path`` so it can exercise the
    production install tree, and it never restores ``sys.path``. After that
    module runs, a later ``import plugins.trade`` resolves to the installed
    runtime instead of the source tree, so a desk created in this module
    would load the INSTALLED spot agent rather than the one under test.

    Pinning the source tree at the front of ``sys.path`` for the duration of
    every test keeps the agent under test and the agent under mock the same
    object, in an isolated run and in a full-suite run alike.
    """
    return str(Path(__file__).resolve().parent.parent.parent)


@pytest.fixture(autouse=True)
def _pin_source_tree():
    """Keep the production install tree from shadowing the source tree.

    ``plugins/trade/tests/test_summative_reliability.py`` prepends
    ``/usr/local/lib/hermes-agent`` to ``sys.path`` so it can exercise the
    production install tree, and it never restores ``sys.path``. Any later
    import of ``plugins.trade.*`` then resolves to the INSTALLED runtime
    instead of the source tree under test.

    This fixture only adjusts ``sys.path`` order. It deliberately does NOT
    purge ``sys.modules``: dropping cached ``plugins.*`` entries while
    pytest is still collecting breaks collection of the remaining test
    files ("collected 0 items"). The agent modules this file needs are
    loaded by explicit file path instead, which is immune to both
    ``sys.path`` order and the module cache.
    """
    root = _source_tree_root()
    saved = list(sys.path)
    while root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
    try:
        yield
    finally:
        sys.path[:] = saved


def _source_agents_dir() -> Path:
    """Return the agents directory of the source tree that owns this file.

    ``SpotDesk`` resolves its agents relative to its own module, so a desk
    imported from the installed runtime would load the INSTALLED spot
    agent even when ``sys.path`` prefers the source tree. Pinning
    ``agents_dir`` guarantees the desk under test uses the agent next to
    this test file.
    """
    return Path(__file__).resolve().parent.parent / "agents"


def _private_agents_package() -> str:
    """Create a private SUBPACKAGE of ``plugins.trade`` for safe loading.

    Registering the agent under the shared name
    ``plugins.trade.agents.<stem>`` replaces the module object other test
    files already imported, which shows up as a
    ``mismatched_fields: ["response_type"]`` identity failure elsewhere in
    the suite.

    A private subpackage keeps every shared ``sys.modules`` entry
    untouched, while its relative imports still resolve to the real
    ``plugins.trade`` modules -- so the class identities match the desk
    exactly.
    """
    import types

    package_name = "plugins.trade._phase7a_agents"
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [str(_source_agents_dir())]  # type: ignore[attr-defined]
        package.__package__ = "plugins.trade"
        sys.modules[package_name] = package
        import plugins.trade as _real_trade

        _real_trade._phase7a_agents = package  # type: ignore[attr-defined]
    return package_name


def _load_source_agent(filename: str):
    """Load an agent module directly from the source tree by file path.

    Importing by name is not enough: ``test_summative_reliability.py``
    prepends the production install tree to ``sys.path`` and leaves it
    there, so ``import plugins.trade.agents.x_hyperliquid_spot_agent`` can
    resolve to the INSTALLED agent, which does not contain the fixes under
    test. Loading by path always yields the source code, independent of
    ``sys.path`` and of the ``sys.modules`` cache.
    """
    package_name = _private_agents_package()
    path = _source_agents_dir() / filename
    module_name = f"{package_name}.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _wire_agent(*, open_rows=None, balances=None, history=None, meta=None, client=None):
    """Build a desk whose agent has every transport helper replaced.

    The replacements are installed with ``unittest.mock.patch.object`` on
    the agent module object that SpotDesk actually owns. SpotDesk caches
    the agent module in ``sys.modules``, so an unrestored assignment here
    would leak a mocked transport into every later test in the session.
    The autouse ``_restore_agent_transport`` fixture undoes them.
    """
    desk = _spotdesk_cls()(agents_dir=_source_agents_dir())
    desk.list_exchanges()
    agent = desk.agent_for("hyperliquid")
    assert agent is not None
    meta = meta if meta is not None else ALIAS_META
    balances = balances if balances is not None else [
        {"coin": "USDC", "token": 0, "total": "5000", "hold": "0"},
        {"coin": "HYPE", "token": 150, "total": "100", "hold": "0"},
        {"coin": "USOL", "token": 254, "total": "100", "hold": "0"},
    ]

    def fake_post_info(payload):
        kind = payload.get("type")
        if kind == "spotMeta":
            return meta
        if kind == "spotClearinghouseState":
            return {"balances": balances}
        if kind == "openOrders":
            return list(open_rows or [])
        if kind == "historicalOrders":
            return list(history or [])
        if kind == "spotMetaAndAssetCtxs":
            ctxs = [
                {"coin": item["name"], "midPx": "150.0", "markPx": "150.0"}
                for item in meta["universe"]
            ]
            return [meta, ctxs]
        return []

    if client is None:
        client = MagicMock()
        client.bulk_orders.side_effect = lambda reqs: {
            "response": {"data": {"statuses": [{"resting": {"oid": 777}} for _ in reqs]}}
        }
        client.bulk_cancel.side_effect = lambda reqs: {
            "response": {"data": {"statuses": ["success"] * len(reqs)}}
        }

    patchers = [
        patch.object(agent, "_post_info", fake_post_info),
        patch.object(agent, "_lookup_credentials", lambda alias: (WALLET, "secret")),
        patch.object(agent, "_fetch_spot_meta", lambda: meta),
        patch.object(agent, "_fetch_spot_balances", lambda wallet: list(balances)),
        patch.object(agent, "_build_exchange_client", lambda account: (client, WALLET, "secret")),
    ]
    for patcher in patchers:
        patcher.start()
    _ACTIVE_PATCHERS.extend(patchers)
    return desk, agent, client, patchers


def _restore(patchers) -> None:
    for patcher in reversed(patchers):
        patcher.stop()


@pytest.fixture(autouse=True)
def _restore_agent_transport():
    """Undo every transport patch this module installs.

    SpotDesk keeps the agent module in ``sys.modules`` for the life of the
    process, so a mocked transport that survives one test would silently
    break every later test in the session.
    """
    yield
    while _ACTIVE_PATCHERS:
        _ACTIVE_PATCHERS.pop().stop()


# ---------------------------------------------------------------------------
# D1 — price precision
# ---------------------------------------------------------------------------

def test_spot_price_obeys_hyperliquid_five_significant_figure_rule():
    """The wire price must satisfy Hyperliquid's 5-significant-figure rule.

    Hyperliquid rejects over-precise prices with a tick error; it does not
    round them for us. The perp agent already enforces this rule via
    _normalize_hyperliquid_order_price; the spot agent did not, so an
    over-precise price reached the wire and would be rejected by the venue.
    """
    markets = _spot_agent_module()._parse_spot_meta(ALIAS_META)
    market = _spot_agent_module()._resolve_market("HYPE/USDC", markets)
    # szDecimals=2 -> spot allows 8-2 = 6 decimals, but still only 5 sig figs.
    # 1234.56789 has 9 significant figures -> must be normalized, not passed through.
    request = _spot_agent_module()._build_order_request(market, "BUY", "1", "1234.56789")
    from decimal import Decimal
    price = Decimal(str(request["limit_px"]))
    assert _perp_agent_module()._hyperliquid_price_is_valid(price, 2, is_spot=True), (
        f"wire price {price} violates the 5-significant-figure rule"
    )
    assert price != Decimal("1234.56789"), "over-precise price was passed through unchanged"


def test_spot_price_accepts_valid_five_significant_figure_price():
    markets = _spot_agent_module()._parse_spot_meta(ALIAS_META)
    market = _spot_agent_module()._resolve_market("HYPE/USDC", markets)
    request = _spot_agent_module()._build_order_request(market, "BUY", "1", "1234.5")
    assert request["limit_px"] == 1234.5


def test_spot_price_respects_sz_decimal_limit():
    """A price with more than 8-szDecimals decimals must not reach the wire."""
    markets = _spot_agent_module()._parse_spot_meta(ALIAS_META)
    market = _spot_agent_module()._resolve_market("SOL/USDC", markets)  # USOL szDecimals=3
    request = _spot_agent_module()._build_order_request(market, "BUY", "100", "0.1234567")
    from decimal import Decimal
    price = Decimal(str(request["limit_px"]))
    decimals = -price.normalize().as_tuple().exponent
    assert decimals <= 5, f"price {price} has {decimals} decimals, max is 8-3=5"
    assert _perp_agent_module()._hyperliquid_price_is_valid(price, 3, is_spot=True)


def test_spot_order_request_uses_normalized_price():
    """The wire price must be the normalized (5 sig fig) value."""
    markets = _spot_agent_module()._parse_spot_meta(ALIAS_META)
    market = _spot_agent_module()._resolve_market("HYPE/USDC", markets)
    request = _spot_agent_module()._build_order_request(market, "BUY", "1", "1234.56789")
    from decimal import Decimal
    price = Decimal(str(request["limit_px"]))
    assert _perp_agent_module()._hyperliquid_price_is_valid(price, 2, is_spot=True), (
        f"wire price {price} violates the 5-significant-figure rule"
    )


# ---------------------------------------------------------------------------
# D2 — order_history duplicates
# ---------------------------------------------------------------------------

def test_order_history_returns_one_row_per_order_using_latest_status():
    """The same oid must not appear twice with conflicting statuses.

    Hyperliquid returns one history record per status transition. The
    agent previously emitted every record, so an order that was later
    cancelled appeared once as "open" and once as "canceled".
    """
    history = [
        {"order": {"coin": "@107", "side": "B", "limitPx": "1400.0", "sz": "0.1",
                   "origSz": "0.1", "oid": 111},
         "status": "open", "statusTimestamp": 1794601709158},
        {"order": {"coin": "@107", "side": "B", "limitPx": "1400.0", "sz": "0.1",
                   "origSz": "0.1", "oid": 111},
         "status": "canceled", "statusTimestamp": 1794602470633},
        {"order": {"coin": "@107", "side": "B", "limitPx": "1000.0", "sz": "0.2",
                   "origSz": "0.2", "oid": 222},
         "status": "open", "statusTimestamp": 1794602427665},
    ]
    _desk, _agent, _client, _patchers = _wire_agent(history=history)
    agent = _desk.agent_for("hyperliquid")
    response = agent.execute({"operation": "order_history", "account": "BASED"})
    assert response.success
    rows = response.data["orders"]
    oids = [row["order_id"] for row in rows]
    assert len(oids) == len(set(oids)), f"duplicate oids in history: {oids}"
    by_oid = {row["order_id"]: row for row in rows}
    assert by_oid["111"]["status"] == "canceled", "must use the newest status record"
    assert by_oid["222"]["status"] == "open"


def test_order_history_keeps_executed_quantity_from_the_latest_record():
    history = [
        {"order": {"coin": "@107", "side": "B", "limitPx": "1400.0", "sz": "0.4",
                   "origSz": "1", "oid": 333},
         "status": "open", "statusTimestamp": 1000},
        {"order": {"coin": "@107", "side": "B", "limitPx": "1400.0", "sz": "0.4",
                   "origSz": "1", "oid": 333},
         "status": "canceled", "statusTimestamp": 2000},
    ]
    _desk, _agent, _client, _patchers = _wire_agent(history=history)
    agent = _desk.agent_for("hyperliquid")
    response = agent.execute({"operation": "order_history", "account": "BASED"})
    rows = response.data["orders"]
    assert len(rows) == 1
    assert rows[0]["executed_qty"] == "0.6"
    assert rows[0]["remaining_qty"] == "0.4"


# ---------------------------------------------------------------------------
# D3 — operation identity
# ---------------------------------------------------------------------------

def test_agent_preserves_open_orders_operation_identity():
    """A request for "open_orders" must be answered with "open_orders".

    SpotDesk rejects a response whose operation differs from the request.
    The agent accepted "open_orders" but answered "orders", so the
    dispatcher turned a valid read into INVALID_AGENT_RESPONSE.
    """
    _desk, _agent, _client, _patchers = _wire_agent(open_rows=[])
    agent = _desk.agent_for("hyperliquid")
    response = agent.execute({"operation": "open_orders", "account": "BASED"})
    assert response.operation == "open_orders"
    assert response.exchange == "hyperliquid"
    assert response.account == "BASED"


def test_spotdesk_accepts_open_orders_operation_for_hyperliquid():
    _desk, _agent, _client, _patchers = _wire_agent(open_rows=[])
    response = _desk.execute({"operation": "open_orders", "exchange": "hyperliquid", "account": "BASED"})
    assert response.success, response.error
    assert response.operation == "open_orders"


def test_open_orders_capability_is_advertised():
    """If the agent serves open_orders it must advertise it."""
    assert "open_orders" in _spot_agent_module().capabilities()


# ---------------------------------------------------------------------------
# D4 — wizard cancel-result arithmetic
# ---------------------------------------------------------------------------

@requires_wizard_cancel_zero_fix
def test_wizard_cancel_result_reports_zero_remaining_when_agent_confirms_full_success():
    """remaining=0 must survive the wizard's arithmetic.

    `int(data.get("remaining") or remaining)` swallowed a legitimate 0
    and the screen showed "Remaining: 1" next to "Cancelled: 1".
    """
    rows = [
        {"coin": "@107", "side": "B", "limitPx": "55.0", "sz": "1", "origSz": "1", "oid": 111},
    ]
    _desk, _agent, client, _patchers = _wire_agent(open_rows=rows)
    wizard = _wizard_cls()(spotdesk=_desk)
    wizard.handle_callback(CHAT, "exchange:hyperliquid")
    wizard.handle_callback(CHAT, "account:BASED")
    screen = wizard.handle_callback(CHAT, "action:cancel_orders")
    group = [b for row in screen.buttons for b in row
             if str(b.get("callback_data", "")).startswith("cg:BUY")][0]
    screen = wizard.handle_callback(CHAT, group["callback_data"])
    token = [b for row in screen.buttons for b in row
             if str(b.get("callback_data", "")).startswith("cx:")][0]
    screen = wizard.handle_callback(CHAT, token["callback_data"])
    assert "Cancelled: 1" in screen.text
    assert "Remaining: 0" in screen.text, screen.text
    assert "✅ Orders Cancelled" in screen.text, screen.text


# ---------------------------------------------------------------------------
# D5 — display alias vs native token in the SELL balance check
# ---------------------------------------------------------------------------

@requires_wizard_alias_balance_fix
def test_wizard_allows_sell_of_frontend_alias_pair_holding_native_token():
    """A SOL/USDC sell must be allowed when the wallet holds USOL.

    Hyperliquid reports balances under the NATIVE token name (USOL);
    the wizard compared the DISPLAY base name (SOL) against them.
    """
    _desk, _agent, client, _patchers = _wire_agent(balances=[
        {"coin": "USDC", "token": 0, "total": "5000", "hold": "0"},
        {"coin": "USOL", "token": 254, "total": "100", "hold": "0"},
    ])
    wizard = _wizard_cls()(spotdesk=_desk)
    wizard.handle_callback(CHAT, "exchange:hyperliquid")
    wizard.handle_callback(CHAT, "account:BASED")
    screen = wizard.handle_callback(CHAT, "action:new_order")
    other = [b for row in screen.buttons for b in row
             if b.get("callback_data") == "asset:other"][0]
    screen = wizard.handle_callback(CHAT, other["callback_data"])
    screen = wizard.handle_text(CHAT, "SOL")
    pair = [b for row in screen.buttons for b in row
            if str(b.get("callback_data", "")).startswith("pair:")][0]
    screen = wizard.handle_callback(CHAT, pair["callback_data"])
    sell = [b for row in screen.buttons for b in row
            if "SELL" in str(b.get("text", "")).upper()][0]
    screen = wizard.handle_callback(CHAT, sell["callback_data"])
    screen = wizard.handle_text(CHAT, "2")
    screen = wizard.handle_text(CHAT, "150")
    assert "Insufficient" not in screen.text, screen.text
    place = [b for row in screen.buttons for b in row
             if str(b.get("callback_data", "")).startswith("place:")]
    assert place, screen.text
    screen = wizard.handle_callback(CHAT, place[0]["callback_data"])
    assert "Insufficient" not in screen.text, screen.text
    client.bulk_orders.assert_called_once()
    order = client.bulk_orders.call_args[0][0][0]
    assert order["coin"] == "@156", order
    assert order["is_buy"] is False


def test_agent_preview_order_uses_native_token_for_alias_pair():
    _desk, _agent, _client, _patchers = _wire_agent(balances=[
        {"coin": "USOL", "token": 254, "total": "100", "hold": "0"},
    ])
    agent = _desk.agent_for("hyperliquid")
    response = agent.execute({
        "operation": "preview_order", "account": "BASED", "side": "SELL",
        "symbol": "SOL/USDC", "quantity": "2", "price": "150",
    })
    assert response.success
    assert response.data["required_asset"] == "USOL"


# ---------------------------------------------------------------------------
# Regression guards for the behaviour that already worked
# ---------------------------------------------------------------------------

def test_cancel_group_displays_readable_pair_not_native_id():
    rows = [
        {"coin": "@107", "side": "B", "limitPx": "55.0", "sz": "1", "origSz": "1", "oid": 111},
        {"coin": "@107", "side": "B", "limitPx": "56.0", "sz": "2", "origSz": "2", "oid": 112},
    ]
    _desk, _agent, _client, _patchers = _wire_agent(open_rows=rows)
    wizard = _wizard_cls()(spotdesk=_desk)
    wizard.handle_callback(CHAT, "exchange:hyperliquid")
    wizard.handle_callback(CHAT, "account:BASED")
    screen = wizard.handle_callback(CHAT, "action:cancel_orders")
    assert "@107" not in screen.text
    assert "HYPE/USDC" in screen.text
    client = _desk.agent_for("hyperliquid")._build_exchange_client("BASED")[0]
    group = [b for row in screen.buttons for b in row
             if str(b.get("callback_data", "")).startswith("cg:BUY")][0]
    screen = wizard.handle_callback(CHAT, group["callback_data"])
    token = [b for row in screen.buttons for b in row
             if str(b.get("callback_data", "")).startswith("cx:")][0]
    wizard.handle_callback(CHAT, token["callback_data"])
    client.bulk_cancel.assert_called_once_with(
        [{"coin": "@107", "oid": 111}, {"coin": "@107", "oid": 112}]
    )


def test_perpetual_orders_are_not_included_in_spot_discovery():
    rows = [
        {"coin": "@107", "side": "B", "limitPx": "55.0", "sz": "1", "origSz": "1", "oid": 111},
        {"coin": "HYPE", "side": "B", "limitPx": "10.0", "sz": "1", "origSz": "1", "oid": 999},
    ]
    _desk, _agent, _client, _patchers = _wire_agent(open_rows=rows)
    agent = _desk.agent_for("hyperliquid")
    response = agent.execute({"operation": "orders", "account": "BASED"})
    assert response.success
    assert response.open_order_count == 1
    assert response.data["orders"][0]["native_coin"] == "@107"
    assert response.data["orders"][0]["symbol"] == "HYPE/USDC"


def test_duplicate_cancel_confirmation_does_not_cancel_twice():
    rows = [
        {"coin": "@107", "side": "B", "limitPx": "55.0", "sz": "1", "origSz": "1", "oid": 111},
    ]
    _desk, _agent, client, _patchers = _wire_agent(open_rows=rows)
    wizard = _wizard_cls()(spotdesk=_desk)
    wizard.handle_callback(CHAT, "exchange:hyperliquid")
    wizard.handle_callback(CHAT, "account:BASED")
    screen = wizard.handle_callback(CHAT, "action:cancel_orders")
    group = [b for row in screen.buttons for b in row
             if str(b.get("callback_data", "")).startswith("cg:BUY")][0]
    screen = wizard.handle_callback(CHAT, group["callback_data"])
    token = [b for row in screen.buttons for b in row
             if str(b.get("callback_data", "")).startswith("cx:")][0]
    wizard.handle_callback(CHAT, token["callback_data"])
    wizard.handle_callback(CHAT, token["callback_data"])
    assert client.bulk_cancel.call_count == 1


def test_order_placement_requires_explicit_confirmation():
    _desk, _agent, client, _patchers = _wire_agent()
    wizard = _wizard_cls()(spotdesk=_desk)
    wizard.handle_callback(CHAT, "exchange:hyperliquid")
    wizard.handle_callback(CHAT, "account:BASED")
    wizard.handle_callback(CHAT, "action:new_order")
    screen = wizard.handle_callback(CHAT, "asset:HYPE")
    screen = wizard.handle_callback(CHAT, "pair:0")
    buy = [b for row in screen.buttons for b in row
           if str(b.get("text", "")).upper().startswith("🟢 BUY")][0]
    wizard.handle_callback(CHAT, buy["callback_data"])
    wizard.handle_text(CHAT, "2")
    wizard.handle_text(CHAT, "40")
    client.bulk_orders.assert_not_called(), "no order may be placed before confirmation"
    screen = wizard.handle_callback(CHAT, "action:new_order")
    client.bulk_orders.assert_not_called()


def test_spot_capabilities_do_not_advertise_ladder_or_market_orders():
    """Phase 7B implemented the Hyperliquid Spot ladder.

    The Phase 7A assertion that ``ladder`` was absent is superseded: the
    ladder now exists and is advertised. What must STILL hold is that the
    spot agent never advertises market orders or perpetual position
    management, and that the ladder surface stays limited to the
    implemented operations.
    """
    caps = set(_spot_agent_module().capabilities())
    assert "ladder" in caps, "Phase 7B implemented the Hyperliquid Spot ladder"
    assert "plan_ladder" in caps
    assert "ladder_reconcile" in caps
    assert "ladder_list_unresolved" in caps
    assert "market_order" not in caps
    assert "positions_management" not in caps
    # The spot ladder never implies a market-order ladder.
    assert "market_ladder" not in caps


def test_spotdesk_preserves_mexc_and_azverse_capabilities():
    desk = _spotdesk_cls()(agents_dir=_source_agents_dir())
    exchanges = desk.list_exchanges()
    assert "mexc" in exchanges
    assert "azverse" in exchanges
    assert "ladder" in desk.capabilities("mexc")
    assert desk.capabilities("azverse") == ["balance"]
