"""Phase 2.8 — Stateless target-convergence executor tests.

Covers:
  1. Target-side resolve: BUY/SELL → venue-side mapping.
  2. Position parse: from a CanonicalResponse with positions.
  3. Pending-group parse + filter: only same (symbol, side) groups.
  4. Cancel dispatch: cancel_order_group called per matching group.
  5. Delta math:
        - venue flat → OPEN at target_size
        - same-side smaller → INCREASE (target - actual)
        - same-side equal → no-op
        - same-side larger → no-op (no REDUCE; operator handles)
        - wrong side → no-op (no flip; operator handles)
        - target flat → no-op (no auto-flatten)
  6. Top-level converge() flow:
        - cancel pending → re-read → recompute → place.
  7. Idempotency: two consecutive converge() calls with no
     exchange change place exactly one order (second is no-op).
  8. Partial-fill recovery: NOT IMPLEMENTED — unfilled remainder
     corrected on next cycle. Verified by simulating a partial
     fill and confirming the executor places a follow-up order
     for the remaining gap on the next call (NOT a retry of the
     same cycle).
  9. Wrong side: never places an opposite-side order.
 10. Auto-flatten: never places a close_position when target=0.
 11. Never invokes TP / SL / set_position_protections / cancel
     beyond the matching (symbol, side) groups.
 12. New-order arguments use order_type=market, reduce_only=False,
     client_order_id prefix=fibo-.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from plugins.trade.fibo.executor import (
    SIDE_BUY, SIDE_SELL, _compute_remaining_delta, _normalize_actual_side,
    _parse_open_groups, _pending_groups_for_target, _place_market_order,
    _reg_mt4_side, _resolve_mt4_target, converge,
    ExchangePosition, Mt4Target, _fibo_to_venue_side,
)
from plugins.trade.fibo.snapshot import Mt4Snapshot, Mt4Fibo
from plugins.trade.fibo.store import FiboRegistration


# ---------------------------------------------------------------------------
# Test doubles — pure-Python duck types so the executor stays
# dependency-injected and never hits the network.
# ---------------------------------------------------------------------------


@dataclass
class _FakeResponse:
    """Minimal CanonicalResponse-compatible surface."""
    success: bool = True
    operation: str = ""
    exchange: str = ""
    account: str = ""
    positions: Optional[List[Dict[str, Any]]] = None
    order_groups: Optional[List[Dict[str, Any]]] = None
    open_order_count: Optional[int] = 0
    order: Optional[Dict[str, Any]] = None
    error: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "success": self.success,
            "operation": self.operation,
            "exchange": self.exchange,
            "account": self.account,
        }
        if self.positions is not None:
            d["positions"] = self.positions
        if self.order_groups is not None:
            d["order_groups"] = self.order_groups
        if self.open_order_count is not None:
            d["open_order_count"] = self.open_order_count
        if self.order is not None:
            d["order"] = self.order
        if self.error is not None:
            d["error"] = self.error
        return d


@dataclass
class _ExecLog:
    """Captures every call the executor makes."""
    calls: List[Dict[str, Any]] = field(default_factory=list)


def _stub_executor(
    *,
    reads: Optional[List[Tuple[Optional[Dict[str, Any]],
                              Optional[List[Dict[str, Any]]]]]] = None,
    new_order_result: Optional[_FakeResponse] = None,
    cancel_order_group_result: Optional[_FakeResponse] = None,
    raise_on: Optional[List[str]] = None,
) -> Tuple[Callable[[Dict[str, Any]], Any], _ExecLog]:
    """Build a stub execute_fn that records all calls.

    ``reads`` is an ordered list of ``(position_dict, order_groups_list)``
    tuples returned by each successive ``positions_orders`` call.
    If the executor makes more reads than provided, the last
    element is repeated.

    The cancel-precheck re-reads positions_orders, so tests
    exercising cancellation must pass at least 2 read entries
    (initial + post-cancel). Tests exercising only one-shot
    convergence can pass a single-element list.
    """
    log = _ExecLog()
    if not reads:
        reads = [(None, None)]
    po_call = {"n": 0}

    def _fn(req: Dict[str, Any]) -> _FakeResponse:
        log.calls.append(dict(req))
        op = req.get("operation")
        if raise_on and op in raise_on:
            raise RuntimeError(f"simulated failure on {op}")
        if op == "positions_orders":
            idx = min(po_call["n"], len(reads) - 1)
            po_call["n"] += 1
            position, groups = reads[idx]
            return _FakeResponse(
                success=True, operation="positions_orders",
                positions=[position] if position else [],
                order_groups=groups or [],
            )
        if op == "cancel_order_group":
            return cancel_order_group_result or _FakeResponse(
                success=True, operation="cancel_order_group",
            )
        if op == "new_order":
            return new_order_result or _FakeResponse(
                success=True, operation="new_order",
                order={
                    "symbol": req.get("symbol"),
                    "side": req.get("side"),
                    "submitted_volume": req.get("volume"),
                    "client_order_id": req.get("client_order_id"),
                },
            )
        return _FakeResponse(success=False, operation=op or "",
                             error={"code": "UNKNOWN_OP",
                                    "message": f"unknown op {op!r}"})

    return _fn, log


def _reg(
    *,
    side: str = "BUY",
    source_symbol: str = "ETHUSD",
    exchange_instrument: str = "ETH-USD.P",
    variant: str = "NORMALFib",
    starting_volume: str = "0.001",
    exchange: str = "ondoperps",
    account: str = "BITGET",
) -> FiboRegistration:
    """Build a minimal FiboRegistration for tests."""
    return FiboRegistration.build(
        exchange=exchange, account=account,
        symbol=source_symbol, variant=variant, side=side,
        starting_volume=starting_volume,
        source="obs-1", source_seq=1, source_cycle_id=47022998,
        source_cumulative_weight="2.0", source_percentage="0.01",
        source_snapshot_received_at="2026-08-27T00:00:00Z",
        desired_exchange_size=Decimal("0.002"),
        source_symbol=source_symbol,
        exchange_instrument=exchange_instrument,
    )


def _snap(
    *,
    symbol: str = "ETHUSD",
    variant: str = "NORMALFib",
    buy_cycle: int = 47022998,
    buy_weight: str = "2.0",
    sell_cycle: int = 0,
    sell_weight: str = "0",
    percentage: str = "0.01",
) -> Mt4Snapshot:
    """Build a minimal Mt4Snapshot for tests."""
    fibo = Mt4Fibo(
        symbol=symbol, variant=variant,
        percentage=Decimal(percentage),
        buy_cycle_id=buy_cycle, cumulative_buy_weight=Decimal(buy_weight),
        sell_cycle_id=sell_cycle, cumulative_sell_weight=Decimal(sell_weight),
    )
    return Mt4Snapshot(
        v=1, source="obs-1", seq=1, ts=1, fibos=[fibo],
        received_at="2026-08-27T12:00:00Z",
        telegram_update_id=1, telegram_message_id=1, reader_chat_id=1,
    )


# ---------------------------------------------------------------------------
# Side helpers
# ---------------------------------------------------------------------------


class SideHelpersTests(unittest.TestCase):

    def test_normalize_actual_side_buy(self):
        self.assertEqual(_normalize_actual_side("buy"), "buy")
        self.assertEqual(_normalize_actual_side("BUY"), "buy")
        self.assertEqual(_normalize_actual_side("long"), "buy")
        self.assertEqual(_normalize_actual_side("LONG"), "buy")

    def test_normalize_actual_side_sell(self):
        self.assertEqual(_normalize_actual_side("sell"), "sell")
        self.assertEqual(_normalize_actual_side("SELL"), "sell")
        self.assertEqual(_normalize_actual_side("short"), "sell")
        self.assertEqual(_normalize_actual_side("SHORT"), "sell")

    def test_normalize_actual_side_unknown(self):
        self.assertEqual(_normalize_actual_side(""), "")
        self.assertEqual(_normalize_actual_side("neutral"), "")
        self.assertEqual(_normalize_actual_side(None), "")

    def test_reg_mt4_side(self):
        self.assertEqual(_reg_mt4_side(_reg(side="BUY")), "BUY")
        self.assertEqual(_reg_mt4_side(_reg(side="SELL")), "SELL")

    def test_fibo_to_venue_side(self):
        self.assertEqual(_fibo_to_venue_side("BUY"), "buy")
        self.assertEqual(_fibo_to_venue_side("SELL"), "sell")
        self.assertEqual(_fibo_to_venue_side(""), "")


# ---------------------------------------------------------------------------
# Target resolve
# ---------------------------------------------------------------------------


class ResolveTargetTests(unittest.TestCase):

    def test_target_buy_uses_buy_weight_times_starting(self):
        reg = _reg(side="BUY", starting_volume="0.001")
        snap = _snap(buy_cycle=42, buy_weight="2.5", percentage="0.01")
        t = _resolve_mt4_target(reg, snap)
        self.assertEqual(t.side, "buy")
        self.assertEqual(t.size, Decimal("0.0025"))  # 0.001 * 2.5

    def test_target_sell_uses_sell_weight(self):
        reg = _reg(side="SELL", starting_volume="0.5")
        snap = _snap(buy_cycle=0, buy_weight="0",
                     sell_cycle=99, sell_weight="1.5",
                     percentage="0.01")
        t = _resolve_mt4_target(reg, snap)
        self.assertEqual(t.side, "sell")
        self.assertEqual(t.size, Decimal("0.75"))  # 0.5 * 1.5

    def test_target_inactive_cycle_is_flat(self):
        reg = _reg(side="BUY", starting_volume="0.001")
        snap = _snap(buy_cycle=0, buy_weight="0")
        t = _resolve_mt4_target(reg, snap)
        self.assertEqual(t.size, Decimal("0"))
        self.assertTrue(t.is_flat)

    def test_target_missing_fibo_is_flat(self):
        reg = _reg(side="BUY", source_symbol="ZZZZ")
        snap = _snap()  # has ETHUSD only
        t = _resolve_mt4_target(reg, snap)
        self.assertEqual(t.size, Decimal("0"))


# ---------------------------------------------------------------------------
# Delta math
# ---------------------------------------------------------------------------


class DeltaMathTests(unittest.TestCase):

    def test_open_from_flat(self):
        actual = ExchangePosition("ETH", "buy", Decimal("0"))
        target = Mt4Target("buy", Decimal("0.5"))
        d = _compute_remaining_delta(actual, target)
        self.assertIsNotNone(d)
        self.assertEqual(d.action, "OPEN")
        self.assertEqual(d.side, "buy")
        self.assertEqual(d.size, Decimal("0.5"))

    def test_increase_when_smaller(self):
        actual = ExchangePosition("ETH", "buy", Decimal("0.2"))
        target = Mt4Target("buy", Decimal("0.5"))
        d = _compute_remaining_delta(actual, target)
        self.assertIsNotNone(d)
        self.assertEqual(d.action, "INCREASE")
        self.assertEqual(d.side, "buy")
        self.assertEqual(d.size, Decimal("0.3"))

    def test_no_op_when_at_target(self):
        actual = ExchangePosition("ETH", "buy", Decimal("0.5"))
        target = Mt4Target("buy", Decimal("0.5"))
        self.assertIsNone(_compute_remaining_delta(actual, target))

    def test_no_op_when_over_target(self):
        actual = ExchangePosition("ETH", "buy", Decimal("0.6"))
        target = Mt4Target("buy", Decimal("0.5"))
        self.assertIsNone(_compute_remaining_delta(actual, target))

    def test_no_op_when_wrong_side(self):
        actual = ExchangePosition("ETH", "sell", Decimal("0.3"))
        target = Mt4Target("buy", Decimal("0.5"))
        self.assertIsNone(_compute_remaining_delta(actual, target))

    def test_no_op_when_target_flat(self):
        actual = ExchangePosition("ETH", "buy", Decimal("0.3"))
        target = Mt4Target("buy", Decimal("0"))
        self.assertIsNone(_compute_remaining_delta(actual, target))


# ---------------------------------------------------------------------------
# Pending group parse / filter
# ---------------------------------------------------------------------------


class PendingGroupTests(unittest.TestCase):

    def test_parse_groups_ignores_non_resting(self):
        resp = _FakeResponse(
            order_groups=[
                {"symbol": "ETH", "side": "buy", "total_size": "0.3"},
                {"symbol": "ETH", "side": "buy", "total_size": "0"},
                {"symbol": "X", "side": "buy", "total_size": "0.1"},
                {"symbol": "ETH", "side": "weird", "total_size": "0.1"},
            ],
        )
        groups = _parse_open_groups(resp)
        # Two real resting orders: ETH buy 0.3 and X buy 0.1.
        # The size=0 and side="weird" entries are filtered.
        self.assertEqual(len(groups), 2)
        symbols = sorted(g.symbol for g in groups)
        self.assertEqual(symbols, ["ETH", "X"])
        eth = next(g for g in groups if g.symbol == "ETH")
        self.assertEqual(eth.side, "buy")
        self.assertEqual(eth.total_size, Decimal("0.3"))

    def test_filter_same_symbol_and_side(self):
        from plugins.trade.fibo.executor import _OpenOrderGroup
        groups = [
            _OpenOrderGroup(symbol="ETH", side="buy",
                            total_size=Decimal("0.3")),
            _OpenOrderGroup(symbol="ETH", side="sell",
                            total_size=Decimal("0.4")),
            _OpenOrderGroup(symbol="BTC", side="buy",
                            total_size=Decimal("0.1")),
        ]
        out = _pending_groups_for_target(
            groups, target_symbol="ETH", target_side="buy",
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].side, "buy")
        self.assertEqual(out[0].symbol, "ETH")


# ---------------------------------------------------------------------------
# Top-level converge
# ---------------------------------------------------------------------------


class ConvergeFlatToOpenTests(unittest.TestCase):
    """Phase A — venue flat, MT4 active: open new position."""

    def test_places_one_market_order(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0"},
                [],
            )],
        )
        result = converge(reg, snap, execute_fn=execute)

        self.assertEqual(result.mt4_target.side, "buy")
        self.assertEqual(result.mt4_target.size, Decimal("0.002"))

        # BEFORE / AFTER both flat.
        self.assertTrue(result.exchange_position_before.is_flat)
        self.assertTrue(result.exchange_position_after.is_flat)

        # No pending orders to cancel.
        self.assertEqual(result.cancelled_groups, ())

        # One new_order placed.
        self.assertIsNotNone(result.placed_order)
        new_orders = [c for c in log.calls if c["operation"] == "new_order"]
        self.assertEqual(len(new_orders), 1)
        no = new_orders[0]
        self.assertEqual(no["symbol"], "ETH-USD.P")
        self.assertEqual(no["side"], "buy")
        self.assertEqual(no["order_type"], "market")
        self.assertEqual(no["reduce_only"], False)
        self.assertEqual(no["volume"], "0.002")
        self.assertTrue(no["client_order_id"].startswith("fibo-"))


class ConvergeIncreaseTests(unittest.TestCase):
    """Phase B — venue smaller than target: increase."""

    def test_places_remaining_delta(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")  # target 0.002
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0.001"},
                [],
            )],
        )
        result = converge(reg, snap, execute_fn=execute)
        new_orders = [c for c in log.calls if c["operation"] == "new_order"]
        self.assertEqual(len(new_orders), 1)
        self.assertEqual(new_orders[0]["volume"], "0.001")  # remaining
        self.assertEqual(new_orders[0]["side"], "buy")


class ConvergeWrongSideTests(unittest.TestCase):
    """Venue on opposite side: no flip."""

    def test_no_new_order_on_wrong_side(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")  # target BUY 0.002
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "short", "size": "0.005"},
                [],
            )],
        )
        result = converge(reg, snap, execute_fn=execute)
        new_orders = [c for c in log.calls if c["operation"] == "new_order"]
        self.assertEqual(new_orders, [],
                         "no flip — executor must NEVER auto-flip")
        self.assertIn("opposite side", result.reason)


class ConvergeNoOpTests(unittest.TestCase):
    """Venue already at or above target: no-op."""

    def test_no_new_order_when_at_target(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")  # target 0.002
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0.002"},
                [],
            )],
        )
        result = converge(reg, snap, execute_fn=execute)
        new_orders = [c for c in log.calls if c["operation"] == "new_order"]
        self.assertEqual(new_orders, [])
        self.assertIn("already at target", result.reason)

    def test_no_new_order_when_above_target(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0.003"},
                [],
            )],
        )
        result = converge(reg, snap, execute_fn=execute)
        new_orders = [c for c in log.calls if c["operation"] == "new_order"]
        self.assertEqual(new_orders, [])


class ConvergeTargetFlatTests(unittest.TestCase):
    """MT4 cycle inactive: no auto-flatten."""

    def test_no_close_when_target_is_flat(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=0, buy_weight="0")  # inactive
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0.003"},
                [],
            )],
        )
        result = converge(reg, snap, execute_fn=execute)
        new_orders = [c for c in log.calls if c["operation"] == "new_order"]
        cancel_calls = [c for c in log.calls
                        if c["operation"] == "cancel_order_group"]
        self.assertEqual(new_orders, [])
        self.assertEqual(cancel_calls, [],
                         "no flatten — executor must NEVER auto-close")
        self.assertIn("mt4 target flat", result.reason)


class CancelPendingAdjustmentTests(unittest.TestCase):
    """Before placing, cancel any pending same-(symbol,side) orders."""

    def test_cancels_pending_then_places(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[
                ({"symbol": "ETH-USD.P", "side": "buy", "size": "0"},
                 [{"symbol": "ETH-USD.P", "side": "buy",
                   "total_size": "0.005"}]),
                # After-cancel: groups cleared, venue still flat.
                ({"symbol": "ETH-USD.P", "side": "buy", "size": "0"}, []),
            ],
        )
        result = converge(reg, snap, execute_fn=execute)
        cancels = [c for c in log.calls
                   if c["operation"] == "cancel_order_group"]
        new_orders = [c for c in log.calls if c["operation"] == "new_order"]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0]["symbol"], "ETH-USD.P")
        self.assertEqual(cancels[0]["side"], "buy")
        self.assertEqual(len(new_orders), 1)
        self.assertIn(("ETH-USD.P", "buy"), result.cancelled_groups)

    def test_does_not_cancel_other_side_or_symbol(self):
        reg = _reg(side="BUY", source_symbol="ETHUSD",
                   exchange_instrument="ETH-USD.P")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[
                ({"symbol": "ETH-USD.P", "side": "buy", "size": "0"},
                 # Wrong side — must NOT be cancelled.
                 [{"symbol": "ETH-USD.P", "side": "sell",
                   "total_size": "0.1"},
                  # Wrong symbol — must NOT be cancelled.
                  {"symbol": "BTC-USD.P", "side": "buy",
                   "total_size": "0.01"}]),
                # After-cancel: still flat.
                ({"symbol": "ETH-USD.P", "side": "buy", "size": "0"},
                 [{"symbol": "ETH-USD.P", "side": "sell",
                   "total_size": "0.1"},
                  {"symbol": "BTC-USD.P", "side": "buy",
                   "total_size": "0.01"}]),
            ],
        )
        result = converge(reg, snap, execute_fn=execute)
        cancels = [c for c in log.calls
                   if c["operation"] == "cancel_order_group"]
        self.assertEqual(cancels, [])
        self.assertEqual(result.cancelled_groups, ())

    def test_no_cancel_when_target_is_flat_or_wrong_side(self):
        """If executor will not place a new order, it must NOT
        cancel pending orders either."""
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=0, buy_weight="0")  # target flat
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0.001"},
                [{"symbol": "ETH-USD.P", "side": "buy",
                  "total_size": "0.005"}],
            )],
        )
        result = converge(reg, snap, execute_fn=execute)
        cancels = [c for c in log.calls
                   if c["operation"] == "cancel_order_group"]
        self.assertEqual(cancels, [],
                         "must not cancel when target is flat (no auto-flatten)")


class IdempotencyTests(unittest.TestCase):
    """Two consecutive converge() calls with no exchange change
    must produce exactly ONE order total."""

    def test_second_call_is_no_op(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0"}, []
            )],
        )
        first = converge(reg, snap, execute_fn=execute)
        self.assertIsNotNone(first.placed_order)
        # The second call observes the venue is at the target.
        execute2, log2 = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0.002"},
                [],
            )],
        )
        second = converge(reg, snap, execute_fn=execute2)
        self.assertIsNone(second.placed_order)
        self.assertIn("already at target", second.reason)
        new_orders2 = [c for c in log2.calls
                       if c["operation"] == "new_order"]
        self.assertEqual(new_orders2, [])


class PartialFillRecoveryTests(unittest.TestCase):
    """No partial-fill recovery. The unfilled remainder is
    corrected on the NEXT cycle by the executor's own logic."""

    def test_follow_up_cycle_fills_remainder(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")  # target 0.002
        # First cycle: venue flat → place 0.002.
        execute1, log1 = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0"}, []
            )],
        )
        converge(reg, snap, execute_fn=execute1)
        placed1 = [c for c in log1.calls
                   if c["operation"] == "new_order"]
        self.assertEqual(len(placed1), 1)
        self.assertEqual(placed1[0]["volume"], "0.002")

        # Second cycle (no cancel because positions_orders returns
        # 0 groups after fill). The venue only filled 0.001 of the
        # 0.002. The executor places the remaining 0.001.
        execute2, log2 = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0.001"},
                [],
            )],
        )
        result2 = converge(reg, snap, execute_fn=execute2)
        placed2 = [c for c in log2.calls
                   if c["operation"] == "new_order"]
        self.assertEqual(len(placed2), 1)
        self.assertEqual(placed2[0]["volume"], "0.001")
        # Critical: the executor did NOT retry the original 0.002.
        # It placed exactly the gap (target - actual).

    def test_no_retry_within_same_cycle(self):
        """If a cycle places an order and the SAME cycle's
        re-read still shows the unfilled remainder, the executor
        must NOT place a second order in the same call. The
        executor only places ONE order per converge() invocation."""
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0"}, []
            )],
        )
        converge(reg, snap, execute_fn=execute)
        new_orders = [c for c in log.calls
                      if c["operation"] == "new_order"]
        self.assertEqual(len(new_orders), 1,
                         "executor MUST place at most ONE order per cycle")


class NoForbiddenOperationsTests(unittest.TestCase):
    """The executor must NEVER invoke TP/SL/close/cancel/position_management."""

    def test_only_positions_orders_cancel_order_group_new_order(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[
                ({"symbol": "ETH-USD.P", "side": "buy", "size": "0"},
                 [{"symbol": "ETH-USD.P", "side": "buy",
                   "total_size": "0.005"}]),
                ({"symbol": "ETH-USD.P", "side": "buy", "size": "0"}, []),
            ],
        )
        converge(reg, snap, execute_fn=execute)
        allowed = {"positions_orders", "cancel_order_group", "new_order"}
        forbidden_called = [
            c for c in log.calls if c["operation"] not in allowed
        ]
        self.assertEqual(forbidden_called, [],
                         f"forbidden ops called: "
                         f"{[c['operation'] for c in forbidden_called]}")
        # And NO reduce-only orders.
        ros = [c for c in log.calls
               if c.get("operation") == "new_order" and c.get("reduce_only")]
        self.assertEqual(ros, [])


class NewOrderArgsContractTests(unittest.TestCase):

    def test_new_order_args(self):
        reg = _reg(side="BUY", starting_volume="0.001")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0"}, []
            )],
        )
        converge(reg, snap, execute_fn=execute)
        placed = [c for c in log.calls
                  if c["operation"] == "new_order"]
        self.assertEqual(len(placed), 1)
        no = placed[0]
        self.assertEqual(no["operation"], "new_order")
        self.assertEqual(no["exchange"], "ondoperps")
        self.assertEqual(no["account"], "BITGET")
        self.assertEqual(no["symbol"], "ETH-USD.P")
        self.assertEqual(no["side"], "buy")
        self.assertEqual(no["order_type"], "market")
        self.assertEqual(no["reduce_only"], False)
        self.assertEqual(no["volume"], "0.002")
        self.assertTrue(no["client_order_id"].startswith("fibo-"))
        self.assertLessEqual(len(no["client_order_id"]), 64)


class ClientOrderIdUniquenessTests(unittest.TestCase):
    """The client_order_id must be unique to the adjustment
    intent — never reused across the lifetime of the
    registration — but deterministic for the same inputs."""

    def _capture_client_order_id(self, snap):
        reg = _reg(side="BUY")
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0"},
                [],
            )],
        )
        converge(reg, snap, execute_fn=execute)
        new_orders = [c for c in log.calls
                      if c["operation"] == "new_order"]
        self.assertEqual(len(new_orders), 1)
        return new_orders[0]["client_order_id"]

    def test_same_inputs_same_id(self):
        snap1 = _snap(buy_cycle=42, buy_weight="2.0")
        snap2 = _snap(buy_cycle=42, buy_weight="2.0")
        cid1 = self._capture_client_order_id(snap1)
        cid2 = self._capture_client_order_id(snap2)
        self.assertEqual(cid1, cid2)

    def test_different_snapshot_seq_same_id(self):
        """Phase 2.10: snap.seq MUST NOT change the id. Two MT4
        snapshots observing the same underlying cycle + weight
        must produce the same client_order_id for the same
        intended adjustment. The venue's idempotency layer
        relies on this stability across rapid observer ticks.
        """
        import dataclasses
        snap1 = _snap(buy_cycle=42, buy_weight="2.0")
        snap2 = _snap(buy_cycle=42, buy_weight="2.0")
        # Bump the second snapshot's seq.
        snap2 = dataclasses.replace(snap2, seq=snap1.seq + 1)
        cid1 = self._capture_client_order_id(snap1)
        cid2 = self._capture_client_order_id(snap2)
        self.assertEqual(cid1, cid2,
                         "snap.seq must NOT change the id (Phase 2.10)")

    def test_different_mt4_cycle_different_id(self):
        snap1 = _snap(buy_cycle=42, buy_weight="2.0")
        snap2 = _snap(buy_cycle=99, buy_weight="2.0")
        cid1 = self._capture_client_order_id(snap1)
        cid2 = self._capture_client_order_id(snap2)
        self.assertNotEqual(cid1, cid2)

    def test_different_weight_different_id(self):
        snap1 = _snap(buy_cycle=42, buy_weight="2.0")
        snap2 = _snap(buy_cycle=42, buy_weight="3.0")
        cid1 = self._capture_client_order_id(snap1)
        cid2 = self._capture_client_order_id(snap2)
        self.assertNotEqual(cid1, cid2)

    def test_id_length_under_64(self):
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        cid = self._capture_client_order_id(snap)
        self.assertLessEqual(len(cid), 64)
        self.assertTrue(cid.startswith("fibo-"))


class CancelErrorToleranceTests(unittest.TestCase):

    def test_cancel_failure_blocks_convergence_no_order(self):
        """If any matching pending adjustment cancel does not
        positively succeed, the executor MUST refuse to place
        a new_order in the same cycle. A leftover resting order
        could still fill and cause overexposure."""
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[
                ({"symbol": "ETH-USD.P", "side": "buy", "size": "0"},
                 [{"symbol": "ETH-USD.P", "side": "buy",
                   "total_size": "0.005"}]),
                ({"symbol": "ETH-USD.P", "side": "buy", "size": "0"}, []),
            ],
            cancel_order_group_result=_FakeResponse(
                success=False, operation="cancel_order_group",
                error={"code": "UNKNOWN", "message": "noop"},
            ),
        )
        result = converge(reg, snap, execute_fn=execute)
        # Cancel was attempted and failed.
        self.assertTrue(result.cancel_failed,
                        "cancel_failed must be True")
        # BUT no order placed, even though the venue state
        # would otherwise justify OPEN.
        self.assertIsNone(result.placed_order,
                          "must NOT place order after failed cancel")
        # The cancel WAS attempted (logged at least).
        cancels = [c for c in log.calls
                   if c["operation"] == "cancel_order_group"]
        self.assertEqual(len(cancels), 1)
        # And no new_order at all.
        new_orders = [c for c in log.calls if c["operation"] == "new_order"]
        self.assertEqual(new_orders, [])

    def test_positions_orders_before_read_failure_blocks_convergence(self):
        """If the BEFORE positions_orders read fails (exception
        or success=False), the executor MUST refuse to cancel
        any pending orders AND refuse to place a new order.
        It must set read_failed=True on the result."""
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[],  # no successful reads
            raise_on=["positions_orders"],
        )
        result = converge(reg, snap, execute_fn=execute)
        self.assertTrue(result.read_failed,
                        "read_failed must be True on BEFORE exception")
        self.assertIsNone(result.placed_order)
        cancels = [c for c in log.calls
                   if c["operation"] == "cancel_order_group"]
        self.assertEqual(cancels, [])
        new_orders = [c for c in log.calls
                      if c["operation"] == "new_order"]
        self.assertEqual(new_orders, [])

    def test_positions_orders_before_read_failure_response_blocks(self):
        """positions_orders returning success=False also blocks
        convergence."""
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[(None, None)],
        )
        # Override the stub to return success=False on the read.
        # We do that by patching the stub directly:
        original_fn = execute

        def patched(req):
            op = req.get("operation")
            if op == "positions_orders":
                return _FakeResponse(
                    success=False, operation="positions_orders",
                    error={"code": "RATE_LIMITED",
                           "message": "too_many_requests"},
                )
            return original_fn(req)

        result = converge(reg, snap, execute_fn=patched)
        self.assertTrue(result.read_failed)
        self.assertIsNone(result.placed_order)
        new_orders = [c for c in log.calls
                      if c["operation"] == "new_order"]
        self.assertEqual(new_orders, [])

    def test_positions_orders_after_read_failure_blocks_convergence(self):
        """If the AFTER re-read fails, the executor refuses to
        place the order even though the BEFORE read succeeded
        and the cancel succeeded."""
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        # Build a stub where the first read works, the second raises.
        log = _ExecLog()
        call_count = {"po": 0}

        def _fn(req):
            log.calls.append(dict(req))
            op = req.get("operation")
            if op == "positions_orders":
                call_count["po"] += 1
                if call_count["po"] == 1:
                    return _FakeResponse(
                        success=True, operation="positions_orders",
                        positions=[{"symbol": "ETH-USD.P", "side": "buy",
                                    "size": "0"}],
                        order_groups=[],
                    )
                # Subsequent reads fail.
                raise RuntimeError("simulated AFTER read failure")
            if op == "cancel_order_group":
                return _FakeResponse(
                    success=True, operation="cancel_order_group",
                )
            if op == "new_order":
                return _FakeResponse(
                    success=True, operation="new_order",
                    order={"symbol": req.get("symbol"),
                           "side": req.get("side"),
                           "submitted_volume": req.get("volume")},
                )
            return _FakeResponse(
                success=False, operation=op or "",
                error={"code": "UNKNOWN_OP",
                       "message": f"unknown op {op!r}"},
            )

        result = converge(reg, snap, execute_fn=_fn)
        self.assertTrue(result.read_failed)
        self.assertIsNone(result.placed_order)
        new_orders = [c for c in log.calls
                      if c["operation"] == "new_order"]
        self.assertEqual(new_orders, [],
                         "AFTER-read-failure must NOT place order")


if __name__ == "__main__":
    unittest.main()