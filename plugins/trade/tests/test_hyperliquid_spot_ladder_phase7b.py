"""Phase 7B — regression tests for the Hyperliquid Spot ladder.

Every test is offline. The exchange client is mocked at the module object
SpotDesk actually owns (see ``_desk_agent``), because SpotDesk builds its
own module instance via importlib and a patch on a direct import would NOT
reach it. No test ever constructs a real signing client, and no test ever
issues a live ``bulk_orders`` / ``bulk_cancel`` call.

Covered:

  L1  uniform BUY ladder  — equal sizing, quote-budget ceiling respected
  L2  uniform SELL ladder — base-quantity semantics
  L3  half-gaussian BUY   — smallest at START, largest at END
  L4  half-gaussian SELL  — same orientation invariant
  L5  native spot asset resolution (spot universe ``@<index>``, never perp)
  L6  price/size precision + minimum-notional validation
  L7  insufficient balance blocks BEFORE any write
  L8  100-order ladder planning
  L9  partial failure  (some children rejected)
  L10 timeout / ambiguous outcome -> UNKNOWN, ladder stops, no retry
  L11 durable reconciliation by cloid (read-only)
  L12 repeated confirmation callbacks -> no duplicate submission
  L13 durable records are exchange-qualified (MEXC cannot collide)
  L14 MEXC regression compatibility (MEXC ladder behaviour unchanged)
  L15 Hyperliquid perpetual isolation (spot ladder never touches perps)
  L16 ladder capability advertised only after implementation
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent.parent.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent.parent.parent))


def _private_trade_package() -> str:
    """Create a private subpackage of ``plugins.trade`` for safe loading.

    ``plugins.trade.spotdesk`` and friends use relative imports
    ("from .canonical import ..."). Registering a replacement module under
    the shared name would REPLACE the module object other test files
    already imported, which surfaces as a
    ``mismatched_fields: ["response_type"]`` identity failure in
    ``test_tradespot_wizard.py``.

    A private SUBPACKAGE of ``plugins.trade`` keeps every shared
    ``sys.modules`` entry untouched, while its relative imports still
    resolve to the real ``plugins.trade.canonical`` module -- so the
    ``CanonicalResponse`` class identity matches the desk exactly.
    """
    import types

    package_name = "plugins.trade._phase7b"
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [str(Path(__file__).resolve().parent.parent)]  # type: ignore[attr-defined]
        package.__package__ = "plugins.trade"
        sys.modules[package_name] = package
        import plugins.trade as _real_trade

        _real_trade._phase7b = package  # type: ignore[attr-defined]
    return package_name


def _load_source_module(module_name: str, relative_path: str):
    """Load a source-tree module by explicit file path.

    ``test_summative_reliability.py`` leaves ``/usr/local/lib/hermes-agent``
    at the front of ``sys.path``, so a name-based import can resolve to the
    INSTALLED runtime instead of the source code under test. Loading by
    path is immune to ``sys.path`` order and to the ``sys.modules`` cache.
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


def _source_agents_dir() -> Path:
    """Return the agents directory of the source tree that owns this file."""
    return Path(__file__).resolve().parent.parent / "agents"


def _ensure_private_package() -> str:
    """Create a private package whose parent resolves to ``plugins.trade``.

    The agent modules use relative imports (``from .x_hyperliquid_agent
    import ...``), so they must live inside a real package. Loading them
    under the shared ``plugins.trade.agents.<stem>`` name would REPLACE
    the module object other test files already imported, which shows up
    as a ``mismatched_fields: ["response_type"]`` identity failure in
    ``test_tradespot_wizard.py``.

    A private package that aliases its parent to ``plugins.trade.agents``
    gives the relative imports a package context while leaving every
    shared ``sys.modules`` entry untouched.
    """
    import types

    package_name = "plugins.trade._phase7b_agents"
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [str(_source_agents_dir())]  # type: ignore[attr-defined]
        package.__package__ = "plugins.trade"
        sys.modules[package_name] = package
        import plugins.trade as _real_trade

        _real_trade._phase7b_agents = package  # type: ignore[attr-defined]
    return package_name


def _load_source_agent(filename: str):
    """Load an agent module directly from the source tree by file path."""
    package_name = _ensure_private_package()
    path = _source_agents_dir() / filename
    module_name = f"{package_name}.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _spotdesk_cls():
    """Return the SpotDesk class from the LIVE module (see _canonical_module)."""
    live = sys.modules.get("plugins.trade.spotdesk")
    if live is not None:
        return live.SpotDesk
    return _load_source_module("_phase7b_spotdesk", "spotdesk.py").SpotDesk


def _spot_ladder_module():
    """Return the shared ladder planner from the LIVE module."""
    live = sys.modules.get("plugins.trade.spot_ladder")
    if live is not None:
        return live
    return _load_source_module("_phase7b_spot_ladder", "spot_ladder.py")


def _canonical_module():
    """Return the LIVE canonical module without replacing the shared one.

    Registering under the shared name ``plugins.trade.canonical`` replaces
    the module object other test files already imported. A desk built
    afterwards then constructs its ``CanonicalResponse`` from the NEW class
    while those tests assert against the OLD one, which surfaces as a
    ``mismatched_fields: ["response_type"]`` identity failure in
    ``test_tradespot_wizard.py``.

    Loading under a private name keeps the shared module untouched.
    """
    live = sys.modules.get("plugins.trade.canonical")
    if live is not None:
        return live
    return _load_source_module("_phase7b_canonical", "canonical.py")


def _wizard_cls():
    """Return the wizard class from the LIVE module (see _canonical_module)."""
    live = sys.modules.get("plugins.trade.tradespot_wizard")
    if live is not None:
        return live.TradeSpotWizard
    return _load_source_module(
        "_phase7b_tradespot_wizard", "tradespot_wizard.py"
    ).TradeSpotWizard


def _desk_agent():
    """Return the hyperliquid spot agent module that SpotDesk actually owns."""
    desk = _spotdesk_cls()(agents_dir=_source_agents_dir())
    desk.list_exchanges()
    agent = desk.agent_for("hyperliquid")
    assert agent is not None
    return agent


def _ladder_module():
    """Return the source-tree Hyperliquid spot ladder module."""
    return _load_source_agent("x_hyperliquid_spot_ladder.py")


WALLET = "0x" + "a" * 40
ACCOUNT = "BASED"

# Live-shaped spotMeta subset.
#  - "@107" is the HYPE/USDC spot pair (native token HYPE, szDecimals 2).
#  - "@156" is the USOL/USDC spot pair behind the frontend's "SOL" alias.
SPOT_META = {
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

# HYPE/USDC market as resolved by _parse_spot_meta.
HYPE_MARKET = {
    "name": "HYPE/USDC", "base": "HYPE", "quote": "USDC",
    "display_base": "HYPE", "display_quote": "USDC",
    "universe_index": 107, "asset_id": 10107, "coin": "@107",
    "size_increment": Decimal("0.01"), "price_increment": Decimal("0.000001"),
    "minimum_size": Decimal("0.01"), "minimum_notional": Decimal("10"),
    "size_step": Decimal("0.01"), "price_tick": Decimal("0.000001"),
    "min_notional": Decimal("10"), "min_qty": Decimal("0.01"), "max_qty": Decimal("0"),
}

SOL_MARKET = {
    "name": "USOL/USDC", "base": "USOL", "quote": "USDC",
    "display_base": "SOL", "display_quote": "USDC",
    "universe_index": 156, "asset_id": 10156, "coin": "@156",
    "size_increment": Decimal("0.001"), "price_increment": Decimal("0.00001"),
    "minimum_size": Decimal("0.001"), "minimum_notional": Decimal("10"),
    "size_step": Decimal("0.001"), "price_tick": Decimal("0.00001"),
    "min_notional": Decimal("10"), "min_qty": Decimal("0.001"), "max_qty": Decimal("0"),
}


def _instrument(market: dict) -> dict:
    """Build the instrument dict the planner consumes."""
    return {
        "symbol": f"{market['display_base']}{market['quote']}",
        "base": market["display_base"],
        "quote": market["quote"],
        "size_step": market["size_step"],
        "price_tick": market["price_tick"],
        "min_notional": market["min_notional"],
        "min_qty": market["min_qty"],
        "max_qty": market["max_qty"],
    }


def _balances(rows):
    """Return a balance payload for the given (coin, total, hold) rows."""
    return [
        {"coin": coin, "total": str(total), "hold": str(hold)}
        for coin, total, hold in rows
    ]


def _children(count: int, *, symbol: str = "HYPEUSDC", side: str = "BUY",
              price_first: str = "40", price_step: str = "0.5",
              size: str = "2.56", market: dict = None) -> list:
    """Build FINAL precomputed children as the wizard would."""
    market = market or HYPE_MARKET
    out = []
    for index in range(count):
        price = (Decimal(price_first) - Decimal(price_step) * index).quantize(Decimal("0.000001"))
        out.append({
            "symbol": symbol,
            "side": side,
            "quantity": size,
            "price": _fmt(price),
            "client_order_id": f"0x{index:032x}",
            "instrument": market,
        })
    return out


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), "f")


class _FakeClient:
    """A fake Hyperliquid exchange client.

    Records every ``bulk_orders`` call so a test can assert the exact
    number of submissions. It NEVER signs and never touches the network.
    """

    def __init__(self, statuses=None, raise_exc=None, statuses_factory=None):
        self.bulk_orders_calls = []
        self.bulk_cancel_calls = []
        self._statuses = statuses
        self._raise_exc = raise_exc
        self._statuses_factory = statuses_factory

    def bulk_orders(self, orders):
        self.bulk_orders_calls.append([dict(o) for o in orders])
        if self._raise_exc is not None:
            raise self._raise_exc
        if self._statuses_factory is not None:
            statuses = self._statuses_factory(orders)
        elif self._statuses is not None:
            statuses = self._statuses
        else:
            statuses = [
                {"resting": {"oid": 900000 + index}}
                for index in range(len(orders))
            ]
        return {"response": {"data": {"statuses": statuses}}}

    def bulk_cancel(self, requests):
        self.bulk_cancel_calls.append([dict(r) for r in requests])
        return {"response": {"data": {"statuses": ["success"] * len(requests)}}}


@pytest.fixture
def ladder_env(tmp_path, monkeypatch):
    """Isolate durable records and mock every exchange interaction."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    agent = _desk_agent()
    ladder = _ladder_module()
    canonical = _canonical_module()

    monkeypatch.setattr(ladder, "_lookup_credentials", lambda account: (WALLET, "secret"))
    monkeypatch.setattr(ladder, "_build_exchange_client", lambda account: (None, WALLET, "secret"))
    monkeypatch.setattr(ladder, "_fetch_spot_balances", lambda wallet: _balances([
        ("USDC", "1000000", "0"), ("HYPE", "1000000", "0"), ("USOL", "1000000", "0"),
    ]))
    monkeypatch.setattr(ladder, "_fetch_spot_meta", lambda: SPOT_META)
    monkeypatch.setattr(ladder, "_market", lambda account, symbol: dict(HYPE_MARKET))
    monkeypatch.setattr(ladder, "_post_info", lambda payload, timeout=10: [])

    return {
        "agent": agent,
        "ladder": ladder,
        "canonical": canonical,
        "client": None,
    }


def _install_client(ladder_env, client):
    """Point the ladder at a fake exchange client."""
    ladder_env["client"] = client
    ladder_env["ladder"]._build_exchange_client = lambda account: (client, WALLET, "secret")
    return client


# ----------------------------------------------------------------------
# L16 — capability advertisement
# ----------------------------------------------------------------------
class TestCapabilityAdvertisement:
    def test_ladder_capability_is_advertised(self, ladder_env):
        caps = ladder_env["agent"].capabilities()
        assert "ladder" in caps
        assert "plan_ladder" in caps

    def test_capability_is_advertised_only_after_implementation(self, ladder_env):
        """Every advertised ladder capability must actually execute."""
        agent = ladder_env["agent"]
        ladder = ladder_env["ladder"]
        for cap, op in (("ladder", "ladder"), ("plan_ladder", "plan_ladder")):
            assert cap in agent.capabilities()
            assert hasattr(ladder, "_ladder" if op == "ladder" else "plan_ladder")

    def test_mexc_ladder_capability_is_unchanged(self, ladder_env):
        """MEXC keeps advertising its own ladder surface."""
        desk = _spotdesk_cls()(agents_dir=_source_agents_dir())
        desk.list_exchanges()
        mexc = desk.agent_for("mexc")
        assert mexc is not None
        assert "ladder" in mexc.capabilities()


# ----------------------------------------------------------------------
# L1-L4 — planning
# ----------------------------------------------------------------------
class TestLadderPlanning:
    def _plan(self, ladder_env, **overrides):
        ladder = ladder_env["ladder"]
        request = {
            "operation": "plan_ladder",
            "exchange": "hyperliquid",
            "account": ACCOUNT,
            "side": "BUY",
            "distribution": "uniform",
            "start_price": Decimal("40"),
            "end_price": Decimal("38"),
            "order_count": 5,
            "instrument": _instrument(HYPE_MARKET),
            "total_volume": None,
            "total_quote_budget": Decimal("500"),
        }
        request.update(overrides)
        return ladder.plan_ladder(request)

    def test_uniform_buy_respects_quote_budget(self, ladder_env):
        response = self._plan(ladder_env)
        assert response.success, response.error
        plan = response.data["plan"]
        children = plan["children"]
        assert len(children) == 5
        total_notional = sum(Decimal(c["notional"]) for c in children)
        assert total_notional <= Decimal("500")
        sizes = {Decimal(c["size"]) for c in children}
        assert len(sizes) == 1, f"uniform sizing expected, got {sizes}"

    def test_uniform_sell_uses_base_quantity(self, ladder_env):
        response = self._plan(
            ladder_env, side="SELL",
            total_volume=Decimal("10"), total_quote_budget=None,
        )
        assert response.success, response.error
        plan = response.data["plan"]
        children = plan["children"]
        assert len(children) == 5
        total_size = sum(Decimal(c["size"]) for c in children)
        assert total_size <= Decimal("10")
        sizes = {Decimal(c["size"]) for c in children}
        assert len(sizes) == 1

    def test_buy_with_total_volume_is_rejected(self, ladder_env):
        response = self._plan(ladder_env, total_volume=Decimal("10"))
        assert not response.success
        assert response.error.code == "INVALID_LADDER_PLAN"

    def test_half_gaussian_buy_is_smallest_at_start(self, ladder_env):
        response = self._plan(ladder_env, distribution="half_gaussian")
        assert response.success, response.error
        children = response.data["plan"]["children"]
        sizes = [Decimal(c["size"]) for c in children]
        assert sizes == sorted(sizes), f"half-gaussian must grow from START: {sizes}"

    def test_half_gaussian_sell_is_smallest_at_start(self, ladder_env):
        response = self._plan(
            ladder_env, side="SELL", distribution="half_gaussian",
            total_volume=Decimal("20"), total_quote_budget=None,
        )
        assert response.success, response.error
        children = response.data["plan"]["children"]
        sizes = [Decimal(c["size"]) for c in children]
        assert sizes == sorted(sizes), f"half-gaussian must grow from START: {sizes}"

    def test_plan_requires_min_notional(self, ladder_env):
        """A ladder cannot be planned without the venue's minimum notional."""
        instrument = _instrument(HYPE_MARKET)
        instrument.pop("min_notional")
        response = self._plan(ladder_env, instrument=instrument)
        assert not response.success
        assert "MIN_NOTIONAL" in response.error.message

    def test_100_order_ladder_plans(self, ladder_env):
        response = self._plan(
            ladder_env, order_count=100,
            start_price=Decimal("40"), end_price=Decimal("30"),
            total_quote_budget=Decimal("5000"),
        )
        assert response.success, response.error
        children = response.data["plan"]["children"]
        assert len(children) == 100
        total_notional = sum(Decimal(c["notional"]) for c in children)
        assert total_notional <= Decimal("5000")

    def test_plan_never_writes_to_the_venue(self, ladder_env):
        """Planning is a pure computation."""
        client = _install_client(ladder_env, _FakeClient())
        self._plan(ladder_env)
        assert client.bulk_orders_calls == []


# ----------------------------------------------------------------------
# L5 — native spot asset resolution
# ----------------------------------------------------------------------
class TestNativeSpotResolution:
    def test_ladder_uses_the_spot_universe_coin(self, ladder_env):
        """The wire order must carry the SPOT coin (``@107``), never a perp."""
        client = _install_client(ladder_env, _FakeClient())
        ladder = ladder_env["ladder"]
        response = ladder._ladder(ACCOUNT, {
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": ACCOUNT,
            "children": _children(3),
            "distribution": "uniform",
            "instrument": HYPE_MARKET,
        })
        assert response.success, response.error
        assert client.bulk_orders_calls, "no order was submitted"
        for order in client.bulk_orders_calls[0]:
            assert order["coin"] == "@107"
            assert order["coin"] != "HYPE"

    def test_ladder_orders_carry_a_valid_cloid(self, ladder_env):
        client = _install_client(ladder_env, _FakeClient())
        ladder = ladder_env["ladder"]
        response = ladder._ladder(ACCOUNT, {
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": ACCOUNT,
            "children": _children(3),
            "instrument": HYPE_MARKET,
        })
        assert response.success, response.error
        cloids = [o["cloid"] for o in client.bulk_orders_calls[0]]
        assert len(cloids) == 3
        for cloid in cloids:
            raw = str(cloid)
            assert raw.startswith("0x")
            assert len(raw) == 34

    def test_resolve_instrument_publishes_native_spot_keys(self, ladder_env):
        """The wizard requires ``size_step`` at confirmation time."""
        agent = ladder_env["agent"]
        response = agent.execute({
            "operation": "resolve_instrument",
            "exchange": "hyperliquid",
            "account": ACCOUNT,
            "symbol": "HYPE/USDC",
        })
        assert response.success, response.error
        data = response.data
        assert data["coin"] == "@107"
        assert data["asset_id"] == 10107
        assert data["size_step"] == "0.01"
        assert data["min_notional"] == "10"

    def test_list_instruments_publishes_ladder_keys(self, ladder_env):
        agent = ladder_env["agent"]
        response = agent.execute({
            "operation": "list_instruments",
            "exchange": "hyperliquid",
            "account": ACCOUNT,
        })
        assert response.success, response.error
        row = response.data["instruments"][0]
        for key in ("size_step", "price_tick", "min_notional", "min_qty", "max_qty"):
            assert key in row, f"missing {key}"


# ----------------------------------------------------------------------
# L6 — precision and minimum notional
# ----------------------------------------------------------------------
class TestPrecisionAndMinNotional:
    def _submit(self, ladder_env, children, instrument=None):
        ladder = ladder_env["ladder"]
        return ladder._ladder(ACCOUNT, {
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": ACCOUNT,
            "children": children,
            "instrument": instrument or HYPE_MARKET,
        })

    def test_quantity_off_the_size_lattice_is_rejected(self, ladder_env):
        client = _install_client(ladder_env, _FakeClient())
        children = _children(1)
        children[0]["quantity"] = "2.555"  # not a multiple of 0.01
        response = self._submit(ladder_env, children)
        assert not response.success
        assert response.error.code == "QUANTITY_PRECISION_INVALID"
        assert client.bulk_orders_calls == [], "nothing may be submitted"

    def test_price_off_the_price_lattice_is_rejected(self, ladder_env):
        client = _install_client(ladder_env, _FakeClient())
        children = _children(1)
        children[0]["price"] = "40.0000001"  # not a multiple of 0.000001
        response = self._submit(ladder_env, children)
        assert not response.success
        assert response.error.code == "PRICE_PRECISION_INVALID"
        assert client.bulk_orders_calls == []

    def test_notional_below_minimum_is_rejected(self, ladder_env):
        client = _install_client(ladder_env, _FakeClient())
        children = _children(1)
        children[0]["quantity"] = "0.01"
        children[0]["price"] = "40"  # 0.4 USDC < 10 USDC minimum
        response = self._submit(ladder_env, children)
        assert not response.success
        assert response.error.code == "NOTIONAL_BELOW_MINIMUM"
        assert client.bulk_orders_calls == []

    def test_quantity_below_minimum_is_rejected(self, ladder_env):
        """A quantity under min_qty must never reach the wire.

        For HYPE/USDC ``min_qty`` and ``size_step`` are both 0.01, so the
        precision check fires first for a sub-minimum quantity. Either way
        the ladder must refuse the child and submit nothing.
        """
        client = _install_client(ladder_env, _FakeClient())
        children = _children(1)
        children[0]["quantity"] = "0.001"  # < min_qty 0.01
        response = self._submit(ladder_env, children)
        assert not response.success
        assert response.error.code in {
            "QUANTITY_BELOW_MINIMUM", "QUANTITY_PRECISION_INVALID",
        }
        assert client.bulk_orders_calls == []

    def test_quantity_below_minimum_with_larger_lattice_is_rejected(self, ladder_env):
        """With a coarse size step, a below-minimum quantity is caught by the
        explicit minimum check rather than the precision check."""
        client = _install_client(ladder_env, _FakeClient())
        market = dict(HYPE_MARKET)
        market["min_qty"] = Decimal("1")
        children = _children(1, market=market)
        children[0]["quantity"] = "0.5"  # on the 0.01 lattice, under min_qty 1
        response = self._submit(ladder_env, children, instrument=market)
        assert not response.success
        assert response.error.code == "QUANTITY_BELOW_MINIMUM"
        assert client.bulk_orders_calls == []

    def test_market_order_is_rejected(self, ladder_env):
        client = _install_client(ladder_env, _FakeClient())
        children = _children(1)
        children[0]["type"] = "MARKET"
        response = self._submit(ladder_env, children)
        assert not response.success
        assert response.error.code == "LIMIT_ONLY"
        assert client.bulk_orders_calls == []

    def test_every_child_is_validated_before_any_write(self, ladder_env):
        """A bad LATER child must stop the whole batch."""
        client = _install_client(ladder_env, _FakeClient())
        children = _children(3)
        children[2]["quantity"] = "2.555"
        response = self._submit(ladder_env, children)
        assert not response.success
        assert client.bulk_orders_calls == [], "no child may be submitted"

    def test_mixed_symbols_are_rejected(self, ladder_env):
        client = _install_client(ladder_env, _FakeClient())
        children = _children(3)
        children[1]["symbol"] = "SOLUSDC"
        response = self._submit(ladder_env, children)
        assert not response.success
        assert response.error.code == "MIXED_SYMBOLS"
        assert client.bulk_orders_calls == []

    def test_mixed_sides_are_rejected(self, ladder_env):
        client = _install_client(ladder_env, _FakeClient())
        children = _children(3)
        children[1]["side"] = "SELL"
        response = self._submit(ladder_env, children)
        assert not response.success
        assert response.error.code == "MIXED_SIDES"
        assert client.bulk_orders_calls == []


# ----------------------------------------------------------------------
# L7 — balance safety
# ----------------------------------------------------------------------
class TestBalanceSafety:
    def test_insufficient_quote_balance_blocks_before_any_write(self, ladder_env):
        client = _install_client(ladder_env, _FakeClient())
        ladder = ladder_env["ladder"]
        ladder._fetch_spot_balances = lambda wallet: _balances([("USDC", "5", "0")])
        response = ladder._ladder(ACCOUNT, {
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": ACCOUNT,
            "children": _children(3),
            "instrument": HYPE_MARKET,
        })
        assert not response.success
        assert response.error.code == "INSUFFICIENT_BALANCE"
        assert client.bulk_orders_calls == []

    def test_insufficient_base_balance_blocks_a_sell(self, ladder_env):
        client = _install_client(ladder_env, _FakeClient())
        ladder = ladder_env["ladder"]
        ladder._fetch_spot_balances = lambda wallet: _balances([("HYPE", "1", "0")])
        response = ladder._ladder(ACCOUNT, {
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": ACCOUNT,
            "children": _children(3, side="SELL"),
            "instrument": HYPE_MARKET,
        })
        assert not response.success
        assert response.error.code == "INSUFFICIENT_BALANCE"
        assert client.bulk_orders_calls == []

    def test_balance_is_re_read_immediately_before_the_write(self, ladder_env):
        """The agent must not trust the wizard's earlier balance check."""
        client = _install_client(ladder_env, _FakeClient())
        ladder = ladder_env["ladder"]
        calls = []

        def fake_balances(wallet):
            calls.append(wallet)
            return _balances([("USDC", "1000000", "0")])

        ladder._fetch_spot_balances = fake_balances
        ladder._ladder(ACCOUNT, {
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": ACCOUNT,
            "children": _children(2),
            "instrument": HYPE_MARKET,
        })
        assert calls, "the agent must re-read the balance before writing"
        assert client.bulk_orders_calls


# ----------------------------------------------------------------------
# L9/L10 — partial failure and ambiguous outcome
# ----------------------------------------------------------------------
class TestSubmissionOutcomes:
    def _submit(self, ladder_env, client, count=3):
        ladder = ladder_env["ladder"]
        return ladder._ladder(ACCOUNT, {
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": ACCOUNT,
            "children": _children(count),
            "instrument": HYPE_MARKET,
        })

    def test_all_accepted_is_verified(self, ladder_env):
        client = _install_client(ladder_env, _FakeClient())
        response = self._submit(ladder_env, client)
        assert response.success, response.error
        ladder = response.ladder
        assert ladder.verified is True
        assert ladder.partial is False
        assert ladder.submitted_order_count == 3
        assert len(ladder.child_order_ids) == 3

    def test_partial_rejection_is_reported_not_hidden(self, ladder_env):
        statuses = [
            {"resting": {"oid": 900001}},
            {"error": "Insufficient margin"},
            {"resting": {"oid": 900003}},
        ]
        client = _install_client(ladder_env, _FakeClient(statuses=statuses))
        response = self._submit(ladder_env, client)
        assert not response.success or response.ladder.partial
        ladder = response.ladder
        assert ladder.partial is True
        assert ladder.submitted_order_count == 2
        assert ladder.omitted_order_count == 1
        assert ladder.status == "partial"

    def test_timeout_marks_unknown_and_stops_without_retry(self, ladder_env):
        client = _install_client(ladder_env, _FakeClient(raise_exc=TimeoutError("timeout")))
        response = self._submit(ladder_env, client, count=3)
        # Exactly ONE submission attempt: an uncertain outcome is never retried.
        assert len(client.bulk_orders_calls) == 1
        assert response.ladder is not None
        assert response.ladder.partial is True
        assert response.ladder.verified is False
        assert response.ladder.submitted_order_count == 0
        assert response.ladder.omitted_order_count == 3

    def test_malformed_response_is_uncertain_not_accepted(self, ladder_env):
        class Malformed(_FakeClient):
            def bulk_orders(self, orders):
                self.bulk_orders_calls.append([dict(o) for o in orders])
                return {"response": {"data": {}}}

        client = _install_client(ladder_env, Malformed())
        response = self._submit(ladder_env, client, count=2)
        assert response.ladder.submitted_order_count == 0
        assert response.ladder.partial is True
        assert len(client.bulk_orders_calls) == 1

    def test_children_after_a_failure_are_not_attempted(self, ladder_env):
        """A ladder of 25 children must not submit a second batch after a
        rejected first batch."""
        statuses = [{"error": "Insufficient margin"}]
        client = _install_client(ladder_env, _FakeClient(statuses=statuses))
        response = self._submit(ladder_env, client, count=25)
        assert response.ladder.partial is True
        # Only the first batch was attempted.
        assert len(client.bulk_orders_calls) == 1
        assert len(client.bulk_orders_calls[0]) == 20


# ----------------------------------------------------------------------
# L11/L13 — durable execution and reconciliation
# ----------------------------------------------------------------------
class TestDurableExecution:
    def test_durable_record_is_written_before_the_first_write(self, ladder_env):
        ladder = ladder_env["ladder"]
        seen = {}

        class Recording(_FakeClient):
            def bulk_orders(self, orders):
                # The durable record must already exist at this point.
                base = ladder._ladder_record_dir(ACCOUNT)
                seen["files"] = sorted(p.name for p in base.glob("*.json")) if base.exists() else []
                return super().bulk_orders(orders)

        client = _install_client(ladder_env, Recording())
        response = ladder._ladder(ACCOUNT, {
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": ACCOUNT,
            "children": _children(2),
            "instrument": HYPE_MARKET,
        })
        assert response.success, response.error
        assert seen["files"], "no durable record existed at submission time"

    def test_record_is_exchange_qualified(self, ladder_env):
        """MEXC and Hyperliquid records must never share a directory."""
        ladder = ladder_env["ladder"]
        record_dir = ladder._ladder_record_dir(ACCOUNT)
        assert "hyperliquid-spot" in str(record_dir)
        assert "mexc" not in str(record_dir)

    def test_record_stores_exact_native_order_ids(self, ladder_env):
        client = _install_client(ladder_env, _FakeClient())
        ladder = ladder_env["ladder"]
        response = ladder._ladder(ACCOUNT, {
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": ACCOUNT,
            "children": _children(2),
            "instrument": HYPE_MARKET,
        })
        assert response.success, response.error
        execution_id = response.data["execution_id"]
        record = ladder._ladder_load_record(ACCOUNT, execution_id)
        assert record is not None
        assert record["exchange"] == "hyperliquid"
        assert record["native_coin"] == "@107"
        assert record["submission_state"] == "ACCEPTED"
        oids = [c["exchange_order_id"] for c in record["children"]]
        assert all(oid for oid in oids), f"missing native order ids: {oids}"

    def test_reconciliation_is_read_only_and_classifies_by_cloid(self, ladder_env):
        ladder = ladder_env["ladder"]
        client = _install_client(ladder_env, _FakeClient())
        response = ladder._ladder(ACCOUNT, {
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": ACCOUNT,
            "children": _children(2),
            "instrument": HYPE_MARKET,
        })
        assert response.success, response.error
        execution_id = response.data["execution_id"]
        cloids = response.data["child_client_order_ids"]

        def fake_info(payload, timeout=10):
            if payload.get("type") == "openOrders":
                return [{"coin": "@107", "cloid": cloids[0], "oid": 900001, "sz": "2.56"}]
            return [
                {"order": {"coin": "@107", "cloid": cloids[1], "oid": 900002, "sz": "0"},
                 "status": "filled", "statusTimestamp": 2},
            ]

        ladder._post_info = fake_info
        rows = ladder.reconcile_batch(
            account=ACCOUNT, symbol="HYPEUSDC",
            expected_client_order_ids=cloids, execution_id=execution_id,
        )
        assert len(rows) == 2
        by_cid = {r["client_order_id"]: r["classification"] for r in rows}
        assert by_cid[cloids[0]] == "FOUND_OPEN"
        assert by_cid[cloids[1]] == "FOUND_FILLED"
        # Reconciliation must never submit or cancel.
        assert len(client.bulk_orders_calls) == 1
        assert client.bulk_cancel_calls == []

    def test_reconciliation_marks_absent_children_not_found(self, ladder_env):
        ladder = ladder_env["ladder"]
        ladder._post_info = lambda payload, timeout=10: []
        rows = ladder.reconcile_batch(
            account=ACCOUNT, symbol="HYPEUSDC",
            expected_client_order_ids=["0x" + "1" * 32],
        )
        assert rows[0]["classification"] == "NOT_FOUND"

    def test_reconciliation_marks_query_failure_unknown(self, ladder_env):
        ladder = ladder_env["ladder"]

        def boom(payload, timeout=10):
            raise RuntimeError("venue down")

        ladder._post_info = boom
        rows = ladder.reconcile_batch(
            account=ACCOUNT, symbol="HYPEUSDC",
            expected_client_order_ids=["0x" + "1" * 32],
        )
        assert rows[0]["classification"] == "QUERY_UNKNOWN"

    def test_reconcile_operation_returns_summary(self, ladder_env):
        ladder = ladder_env["ladder"]
        ladder._post_info = lambda payload, timeout=10: []
        response = ladder._execute_reconcile_batch(ACCOUNT, {
            "operation": "ladder_reconcile",
            "exchange": "hyperliquid",
            "account": ACCOUNT,
            "symbol": "HYPEUSDC",
            "expected_client_order_ids": ["0x" + "1" * 32],
        })
        assert response.success, response.error
        assert "NOT_FOUND" in response.data["summary"]

    def test_unresolved_records_are_listed(self, ladder_env):
        ladder = ladder_env["ladder"]
        client = _install_client(ladder_env, _FakeClient(raise_exc=TimeoutError("timeout")))
        response = ladder._ladder(ACCOUNT, {
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": ACCOUNT,
            "children": _children(2),
            "instrument": HYPE_MARKET,
        })
        assert response.data is not None
        records = ladder.list_unresolved_ladders(ACCOUNT)
        assert len(records) == 1
        assert records[0]["submission_state"] in {"STOPPED_EARLY", "PARTIAL_UNKNOWN"}
        classifications = {c["submission_classification"] for c in records[0]["children"]}
        assert classifications == {"UNKNOWN"}

    def test_resolved_records_are_not_listed_as_unresolved(self, ladder_env):
        ladder = ladder_env["ladder"]
        client = _install_client(ladder_env, _FakeClient())
        ladder._ladder(ACCOUNT, {
            "operation": "ladder",
            "exchange": "hyperliquid",
            "account": ACCOUNT,
            "children": _children(2),
            "instrument": HYPE_MARKET,
        })
        assert ladder.list_unresolved_ladders(ACCOUNT) == []


# ----------------------------------------------------------------------
# L12 — duplicate submission protection
# ----------------------------------------------------------------------
class TestDuplicateSubmissionProtection:
    def test_same_execution_id_is_reused_not_duplicated(self, ladder_env):
        """The wizard forwards its execution_id; the agent must honour it."""
        client = _install_client(ladder_env, _FakeClient())
        ladder = ladder_env["ladder"]
        execution_id = "0123456789abcdef"
        first = ladder._ladder(ACCOUNT, {
            "operation": "ladder", "exchange": "hyperliquid", "account": ACCOUNT,
            "children": _children(2), "instrument": HYPE_MARKET,
            "execution_id": execution_id,
        })
        assert first.success, first.error
        assert first.data["execution_id"] == execution_id
        second = ladder._ladder(ACCOUNT, {
            "operation": "ladder", "exchange": "hyperliquid", "account": ACCOUNT,
            "children": _children(2), "instrument": HYPE_MARKET,
            "execution_id": execution_id,
        })
        assert second.data["execution_id"] == execution_id
        # The cloid set is deterministic for a given execution id.
        assert first.data["child_client_order_ids"] == second.data["child_client_order_ids"]

    def test_client_order_ids_are_unique_per_child(self, ladder_env):
        client = _install_client(ladder_env, _FakeClient())
        ladder = ladder_env["ladder"]
        response = ladder._ladder(ACCOUNT, {
            "operation": "ladder", "exchange": "hyperliquid", "account": ACCOUNT,
            "children": _children(25), "instrument": HYPE_MARKET,
        })
        assert response.success, response.error
        cloids = response.data["child_client_order_ids"]
        assert len(cloids) == 25
        assert len(set(cloids)) == 25

    def test_wizard_confirmation_token_is_single_use(self, ladder_env):
        """The wizard must refuse a second Confirm tap."""
        wizard_cls = _wizard_cls()
        wizard = wizard_cls.__new__(wizard_cls)
        wizard._desk = MagicMock()
        wizard._desk.capabilities.return_value = ["ladder", "plan_ladder"]
        wizard._desk.execute.return_value = MagicMock(
            success=True, operation="ladder", exchange="hyperliquid",
            account=ACCOUNT, error=None, data={"execution_id": "abc"},
            ladder=MagicMock(to_dict=lambda: {}),
        )
        state = MagicMock()
        state.ladder_confirm_token = "lad:tok"
        state.ladder_confirm_consumed = False
        wizard._state_for = lambda chat_key: state
        wizard._clear_order_state = lambda s: None
        screen = wizard._handle_ladder_confirm_callback(("telegram", "chat", 1), "ladder_confirm:lad:tok")
        # The token is burned before any network write.
        assert state.ladder_confirm_consumed is True
        screen2 = wizard._handle_ladder_confirm_callback(("telegram", "chat", 1), "ladder_confirm:lad:tok")
        assert "already been submitted" in screen2.text


# ----------------------------------------------------------------------
# L15 — perpetual isolation
# ----------------------------------------------------------------------
class TestPerpetualIsolation:
    def test_spot_ladder_never_targets_a_perpetual_coin(self, ladder_env):
        """The perp coin 'HYPE' must never appear on the wire."""
        client = _install_client(ladder_env, _FakeClient())
        ladder = ladder_env["ladder"]
        ladder._ladder(ACCOUNT, {
            "operation": "ladder", "exchange": "hyperliquid", "account": ACCOUNT,
            "children": _children(2), "instrument": HYPE_MARKET,
        })
        for order in client.bulk_orders_calls[0]:
            assert order["coin"].startswith("@")
            assert order["coin"] != "HYPE"

    def test_perpetual_agent_is_not_modified_by_the_ladder(self, ladder_env):
        """The perp agent keeps its own ladder and gains no spot surface.

        The perpetual agent legitimately advertises ``ladder`` for perpetual
        ladders, so isolation is proven by the ABSENCE of the spot-only
        surface (``plan_ladder``, the spot ladder module, the spot durable
        record directory) rather than by the absence of the capability.
        """
        perp = _load_source_agent("x_hyperliquid_agent.py")
        assert not hasattr(perp, "plan_ladder")
        assert not hasattr(perp, "_ladder_load_record")
        assert not hasattr(perp, "reconcile_batch")
        # The spot ladder module is a separate file, never merged into perp.
        assert not hasattr(perp, "x_hyperliquid_spot_ladder")
        # The perp durable records live in their own directory.
        perp_ladder_dir = getattr(perp, "_ladder_record_dir", None)
        if callable(perp_ladder_dir):
            assert "hyperliquid-spot" not in str(perp_ladder_dir("BASED"))

    def test_perp_orders_are_excluded_from_spot_reconciliation(self, ladder_env):
        """A perp row in the venue response must not appear in spot views."""
        ladder = ladder_env["ladder"]
        ladder._post_info = lambda payload, timeout=10: [
            {"coin": "HYPE", "oid": 565664358801, "sz": "0"},        # perpetual
            {"coin": "@107", "oid": 604784504954, "sz": "2"},        # spot
        ]
        rows = ladder.open_orders(symbol="", account=ACCOUNT)
        assert len(rows) == 1
        assert rows[0]["native_coin"] == "@107"
        assert rows[0]["display_symbol"] == "HYPE/USDC"


# ----------------------------------------------------------------------
# L14 — MEXC regression compatibility
# ----------------------------------------------------------------------
class TestMexcCompatibility:
    def test_mexc_ladder_plan_still_works(self, ladder_env):
        """MEXC's own ladder planning must be unaffected."""
        desk = _spotdesk_cls()(agents_dir=_source_agents_dir())
        desk.list_exchanges()
        mexc = desk.agent_for("mexc")
        assert mexc is not None
        instrument = {
            "symbol": "SOLUSDT", "base": "SOL", "quote": "USDT",
            "size_step": Decimal("0.01"), "price_tick": Decimal("0.01"),
            "min_notional": Decimal("1"), "min_qty": Decimal("0.01"),
            "max_qty": Decimal("0"),
        }
        response = mexc.execute({
            "operation": "plan_ladder",
            "exchange": "mexc",
            "account": "amiroo",
            "side": "BUY",
            "distribution": "uniform",
            "start_price": Decimal("100"),
            "end_price": Decimal("98"),
            "order_count": 3,
            "instrument": instrument,
            "total_volume": None,
            "total_quote_budget": Decimal("300"),
        })
        assert response.success, response.error
        assert len(response.data["plan"]["children"]) == 3

    def test_mexc_ladder_capabilities_unchanged(self, ladder_env):
        desk = _spotdesk_cls()(agents_dir=_source_agents_dir())
        desk.list_exchanges()
        mexc = desk.agent_for("mexc")
        caps = set(mexc.capabilities())
        assert "ladder" in caps
        assert "plan_ladder" in caps
        assert "ladder_reconcile" in caps
        assert "ladder_list_unresolved" in caps

    def test_azverse_balance_only_restriction_unchanged(self, ladder_env):
        """Azverse is balance-only and must not gain a ladder."""
        desk = _spotdesk_cls()(agents_dir=_source_agents_dir())
        desk.list_exchanges()
        azverse = desk.agent_for("azverse")
        if azverse is None:
            pytest.skip("azverse spot agent not present")
        assert "ladder" not in azverse.capabilities()


# ----------------------------------------------------------------------
# Wizard integration — the ladder button renders for Hyperliquid
# ----------------------------------------------------------------------
class TestWizardLadderSurface:
    def test_ladder_button_renders_for_hyperliquid(self, ladder_env):
        wizard_cls = _wizard_cls()
        wizard = wizard_cls.__new__(wizard_cls)
        wizard._desk = MagicMock()
        wizard._desk.capabilities.return_value = list(ladder_env["agent"].capabilities())
        buttons = wizard._supported_action_buttons("hyperliquid")
        labels = [label for label, _ in buttons]
        assert "📊 Ladder Orders" in labels

    def test_ladder_button_hidden_without_the_capability(self, ladder_env):
        wizard_cls = _wizard_cls()
        wizard = wizard_cls.__new__(wizard_cls)
        wizard._desk = MagicMock()
        wizard._desk.capabilities.return_value = ["balance", "orders"]
        wizard._ladder_preview_only = False
        buttons = wizard._supported_action_buttons("hyperliquid")
        labels = [label for label, _ in buttons]
        assert "📊 Ladder Orders" not in labels
