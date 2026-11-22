"""Phase 7G.1 regression tests: Hyperliquid Spot ladder result accounting.

For a 30-child request every child must be CLASSIFIED -- accepted,
rejected, unknown, or not attempted -- or the result must explicitly
report an unresolved accounting failure. Missing counts must never be
read as a zero-success completion.

Each test drives the real ladder agent with an injected fake client and
a fake exchange, so no venue is contacted.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_AGENTS_DIR = _HERE.parent / "agents"
_LADDER_PATH = _AGENTS_DIR / "x_hyperliquid_spot_ladder.py"


_PKG_NAME = "plugins.trade._p7g1_agents"


def _ensure_package():
    """Create a private subpackage whose __path__ is the agents dir.

    The ladder module imports a SIBLING agent module with a relative
    import (``from .x_hyperliquid_agent import _post_info``), so the
    private package's path must be the agents directory -- otherwise the
    sibling cannot be resolved.
    """
    package = sys.modules.get(_PKG_NAME)
    if package is None:
        package = types.ModuleType(_PKG_NAME)
        package.__path__ = [str(_AGENTS_DIR)]  # type: ignore[attr-defined]
        package.__package__ = "plugins.trade"
        sys.modules[_PKG_NAME] = package
        import plugins.trade as _real_trade

        _real_trade._p7g1_agents = package  # type: ignore[attr-defined]
    return package


def _load_module(path: Path, name: str):
    """Load a module by path, registered in sys.modules for the exec.

    ``dataclasses`` resolves field annotations through
    ``sys.modules[cls.__module__]`` while the class body executes, so the
    module MUST be registered before ``exec_module``.
    """
    _ensure_package()
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    return module


def _load_ladder():
    return _load_module(_LADDER_PATH,
                        f"{_PKG_NAME}.x_hyperliquid_spot_ladder")


class _FakeExchange:
    def __init__(self) -> None:
        self.batches: List[List[Dict[str, Any]]] = []

    def bulk_orders(self, orders: List[Dict[str, Any]]):
        self.batches.append(list(orders))
        return {"response": {"data": {"statuses": []}}}


class _FakeClient:
    """Fake exchange client with a programmable bulk_orders outcome."""

    def __init__(self, exchange: _FakeExchange, statuses=None,
                 raise_exc: Optional[Exception] = None,
                 statuses_factory=None) -> None:
        self._exchange = exchange
        self._statuses = statuses
        self._raise_exc = raise_exc
        self._statuses_factory = statuses_factory

    def bulk_orders(self, orders: List[Dict[str, Any]]):
        self._exchange.batches.append(list(orders))
        if self._raise_exc is not None:
            raise self._raise_exc
        if self._statuses_factory is not None:
            statuses = self._statuses_factory(orders)
        elif self._statuses is not None:
            statuses = self._statuses
        else:
            statuses = [{"resting": {"oid": 1 + i}} for i in range(len(orders))]
        return {"response": {"data": {"statuses": statuses}}}


class _PatchSet:
    """Restore patched attributes after the test."""

    def __init__(self) -> None:
        self._saved: List[Any] = []

    def set(self, obj: Any, attr: str, value: Any) -> None:
        missing = object()
        old = getattr(obj, attr, missing)
        self._saved.append((obj, attr, old))
        setattr(obj, attr, value)

    def restore(self) -> None:
        for obj, attr, old in reversed(self._saved):
            if old is not _MISSING:
                setattr(obj, attr, old)
            else:
                try:
                    delattr(obj, attr)
                except AttributeError:
                    pass
        self._saved.clear()


_MISSING = object()


def _children(count: int = 30) -> List[Dict[str, Any]]:
    """A 30-child ladder whose prices are ON the venue's price grid.

    Prices run 60 down to 31 and each quantity clears the 10 USDC
    minimum notional at its own price, on the 0.001 size lattice.
    """
    children: List[Dict[str, Any]] = []
    for idx in range(count):
        price = Decimal(60) - (Decimal(idx) * Decimal("1"))
        # 10 / price, rounded UP to the 0.001 size lattice, with a
        # little headroom so the minimum notional always clears.
        quantity = (Decimal(10) / price * 1000).to_integral_value(
            rounding="ROUND_CEILING") / 1000 + Decimal("0.001")
        children.append({
            "symbol": "SOL/USDC",
            "side": "BUY",
            "type": "LIMIT",
            "quantity": _text(quantity),
            "price": _text(price),
            "client_order_id": "0x" + ("%032x" % idx),
        })
    return children


def _text(value: Any) -> str:
    d = Decimal(str(value)).normalize()
    return format(d, "f")


class Phase7G1AccountingBase(unittest.TestCase):
    def setUp(self) -> None:
        self.ladder = _load_ladder()
        self.patches = _PatchSet()
        self.exchange = _FakeExchange()
        self.addCleanup(self.patches.restore)
        self.patches.set(self.ladder, "_lookup_credentials",
                         lambda account: ("0xwallet", "secret"))
        self.patches.set(self.ladder, "_market",
                         lambda account, symbol: {
                             "name": "USOL/USDC",
                             "base": "USOL", "quote": "USDC",
                             "display_base": "SOL", "display_quote": "USDC",
                             "coin": "@156", "asset_id": 10156,
                             "size_increment": "0.001", "size_step": "0.001",
                             "price_tick": "0.00001", "price_increment": "0.00001",
                             "min_notional": "10", "min_qty": "0.001",
                         })
        self.patches.set(self.ladder, "_fetch_spot_balances",
                         lambda wallet: [{"coin": "USDC", "token": 0,
                                          "total": "100000", "hold": "0"}])
        self.patches.set(self.ladder, "_ladder_new_execution_id",
                         lambda: "b" * 16)
        self.patches.set(self.ladder, "_ladder_record_path",
                         lambda account, execution_id: Path("/nonexistent-p7g1/rec.json"))
        self.patches.set(self.ladder, "_ladder_persist_atomic",
                         lambda path, payload: None)
        self.patches.set(self.ladder, "_ladder_new_client_order_id",
                         lambda exec_id, idx: "0x" + ("%032x" % idx))

    def _install(self, statuses=None, raise_exc=None, statuses_factory=None):
        client = _FakeClient(self.exchange, statuses=statuses,
                             raise_exc=raise_exc,
                             statuses_factory=statuses_factory)
        self.patches.set(self.ladder, "_build_exchange_client",
                         lambda account: (client, None, None))
        return client

    def _run(self, children: Optional[List[Dict[str, Any]]] = None):
        return self.ladder._ladder("BITGET", {
            "operation": "ladder", "account": "BITGET",
            "symbol": "SOL/USDC", "side": "BUY",
            "distribution": "half_gaussian",
            # The ladder needs the NATIVE base/quote metadata for its
            # balance gate; the agent resolves the spot universe entry.
            "base": "USOL", "quote": "USDC",
            "instrument": {
                "coin": "@156", "asset_id": 10156,
                "size_increment": "0.001", "size_step": "0.001",
                "price_tick": "0.00001", "price_increment": "0.00001",
                "min_notional": "10", "min_qty": "0.001",
            },
            "children": children if children is not None else _children(),
        })


class Phase7G1AllAcceptedTests(Phase7G1AccountingBase):
    def test_every_child_accepted(self) -> None:
        self._install()
        response = self._run()
        self.assertTrue(response.success, getattr(response, "message", ""))
        data = response.data or {}
        self.assertEqual(data.get("accepted_count"), 30)
        self.assertEqual(data.get("rejected_count"), 0)
        self.assertEqual(data.get("unknown_count"), 0)
        self.assertEqual(data.get("not_attempted_count"), 0)
        # The bare spellings the wizard reads must agree.
        self.assertEqual(data.get("accepted"), 30)
        self.assertEqual(data.get("rejected"), 0)
        self.assertEqual(data.get("unknown"), 0)
        self.assertEqual(data.get("not_attempted"), 0)


class Phase7G1AllRejectedTests(Phase7G1AccountingBase):
    def test_every_child_rejected(self) -> None:
        self._install(statuses=[{"error": "Insufficient margin"}] * 30)
        response = self._run()
        self.assertTrue(response.success, getattr(response, "message", ""))
        data = response.data or {}
        # The first batch (20) is rejected -> the ladder STOPS, so the
        # remaining 10 children are never attempted.
        self.assertEqual(data.get("rejected_count"), 20)
        self.assertEqual(data.get("accepted_count"), 0)
        self.assertEqual(data.get("unknown_count"), 0)
        self.assertEqual(data.get("not_attempted_count"), 10)
        self.assertEqual(data.get("rejected"), 20)
        classified = (data.get("accepted_count", 0) + data.get("rejected_count", 0)
                      + data.get("unknown_count", 0) + data.get("not_attempted_count", 0))
        self.assertEqual(classified, 30)


class Phase7G1PartialTests(Phase7G1AccountingBase):
    def test_partial_acceptance_is_classified_per_child(self) -> None:
        def factory(orders):
            out = []
            for i, _ in enumerate(orders):
                if i < 10:
                    out.append({"resting": {"oid": 1000 + i}})
                elif i < 15:
                    out.append({"error": "Post only order rejected."})
                else:
                    out.append({"error": "Insufficient margin"})
            return out

        self._install(statuses_factory=factory)
        response = self._run()
        self.assertTrue(response.success, getattr(response, "message", ""))
        data = response.data or {}
        # The first batch holds 20 children: 10 accepted, 10 rejected.
        # The rejection STOPS the ladder, so the second batch's 10
        # children are NOT ATTEMPTED.
        self.assertEqual(data.get("accepted_count"), 10)
        self.assertEqual(data.get("rejected_count"), 10)
        self.assertEqual(data.get("unknown_count"), 0)
        self.assertEqual(data.get("not_attempted_count"), 10)
        classified = (data.get("accepted_count", 0) + data.get("rejected_count", 0)
                      + data.get("unknown_count", 0) + data.get("not_attempted_count", 0))
        self.assertEqual(classified, 30)

    def test_partial_rejection_stops_the_ladder(self) -> None:
        """A rejected child stops the ladder: no second batch."""
        def factory(orders):
            return ([{"resting": {"oid": 1}}] +
                    [{"error": "Insufficient margin"}] * (len(orders) - 1))

        self._install(statuses_factory=factory)
        response = self._run()
        self.assertTrue(response.success, getattr(response, "message", ""))
        data = response.data or {}
        # One child is accepted, the other 19 in that batch are rejected.
        self.assertEqual(data.get("accepted_count"), 1)
        self.assertEqual(data.get("rejected_count"), 19)
        # The ladder then stops: the whole second batch is NOT_ATTEMPTED.
        self.assertEqual(data.get("not_attempted_count"), 10)
        classified = (data.get("accepted_count", 0) + data.get("rejected_count", 0)
                      + data.get("unknown_count", 0) + data.get("not_attempted_count", 0))
        self.assertEqual(classified, 30)
        self.assertEqual(len(self.exchange.batches), 1)


class Phase7G1UnknownTests(Phase7G1AccountingBase):
    def test_unknown_exchange_response_marks_every_child(self) -> None:
        """A timeout marks the affected children UNKNOWN and STOPS.

        The ladder splits 30 children into a 20-child batch and a
        10-child batch. When the first batch times out the outcome is
        ambiguous, so those 20 children are UNKNOWN and the remaining 10
        are NOT ATTEMPTED -- the ladder never continues to the next
        batch and never retries.
        """
        self._install(raise_exc=TimeoutError("read timed out"))
        response = self._run()
        self.assertTrue(response.success, getattr(response, "message", ""))
        data = response.data or {}
        self.assertEqual(data.get("unknown_count"), 20)
        self.assertEqual(data.get("accepted_count"), 0)
        self.assertEqual(data.get("not_attempted_count"), 10)
        # Every one of the 30 children is accounted for.
        classified = (data.get("accepted_count", 0) + data.get("rejected_count", 0)
                      + data.get("unknown_count", 0) + data.get("not_attempted_count", 0))
        self.assertEqual(classified, 30)
        # Never retried, and the second batch was never sent.
        self.assertEqual(len(self.exchange.batches), 1)


class Phase7G1PreflightTests(Phase7G1AccountingBase):
    def test_preflight_failure_writes_nothing(self) -> None:
        """A preflight rejection must not reach the venue at all."""
        bad = _children()
        bad[5]["quantity"] = "0.0001"   # below the 0.001 size lattice
        response = self._run(bad)
        self.assertFalse(response.success)
        err = getattr(response, "error", None)
        self.assertEqual(getattr(err, "code", None), "QUANTITY_PRECISION_INVALID")
        self.assertEqual(self.exchange.batches, [])

    def test_preflight_normalizes_an_off_grid_price(self) -> None:
        """An off-grid price is NORMALIZED, not rejected.

        The planner emits full-precision prices (57.93103 has 7
        significant figures). The ladder normalizes BEFORE preflight, so
        preflight validates exactly the price that will reach the wire.
        The price that WOULD have been rejected by the venue is repaired
        here instead of failing the whole ladder.
        """
        off_grid = _children()
        off_grid[3]["price"] = "57.93103"     # 7 significant figures
        self._install()
        response = self._run(off_grid)
        self.assertTrue(response.success, getattr(response, "message", ""))
        sent = self.exchange.batches[0]
        wire_price = Decimal(str(sent[3]["limit_px"]))
        self.assertNotEqual(wire_price, Decimal("57.93103"))
        self.assertLessEqual(len(wire_price.normalize().as_tuple().digits), 5)

    def test_preflight_rejects_a_price_it_cannot_normalize(self) -> None:
        """A price that CANNOT be put on the venue's grid is refused."""
        bad = _children()
        bad[3]["price"] = "0"            # not positive -> unnormalizable
        self._install()
        response = self._run(bad)
        self.assertFalse(response.success)
        err = getattr(response, "error", None)
        self.assertEqual(getattr(err, "code", None), "PRICE_PRECISION_INVALID")
        self.assertEqual(self.exchange.batches, [])


class Phase7G1DurableRecordFailureTests(Phase7G1AccountingBase):
    def test_durable_record_failure_stops_before_any_write(self) -> None:
        """If the durable record cannot be written, nothing is sent."""
        def boom(path, payload):
            raise OSError("disk full")

        self.patches.set(self.ladder, "_ladder_persist_atomic", boom)
        self._install()
        response = self._run()
        self.assertFalse(response.success)
        self.assertEqual(self.exchange.batches, [])


class Phase7G1MalformedResponseTests(Phase7G1AccountingBase):
    def test_empty_status_list_is_uncertain_not_success(self) -> None:
        """An empty status list must not read as zero-success."""
        self._install(statuses=[])
        response = self._run()
        self.assertTrue(response.success, getattr(response, "message", ""))
        data = response.data or {}
        self.assertEqual(data.get("unknown_count"), 20)
        self.assertEqual(data.get("accepted_count"), 0)
        self.assertEqual(data.get("not_attempted_count"), 10)
        classified = (data.get("accepted_count", 0) + data.get("rejected_count", 0)
                      + data.get("unknown_count", 0) + data.get("not_attempted_count", 0))
        self.assertEqual(classified, 30)

    def test_batch_rejection_without_child_statuses_is_unresolved(self) -> None:
        """The venue rejected the ACTION and returned no per-child status.

        This is the exact 2026-11-14 shape: one action-level error and no
        per-child statuses. Every child must be classified, and none may
        be reported as a zero-success completion.
        """
        self._install(statuses=[{"error": "Price must be divisible by tick size."}])
        response = self._run()
        self.assertTrue(response.success, getattr(response, "message", ""))
        data = response.data or {}
        classified = (data.get("accepted_count", 0) + data.get("rejected_count", 0)
                      + data.get("unknown_count", 0) + data.get("not_attempted_count", 0))
        self.assertEqual(classified, 30,
                         "every child must be classified or the accounting "
                         "must be reported as unresolved")
        self.assertNotEqual(data.get("accepted_count"), 30)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
