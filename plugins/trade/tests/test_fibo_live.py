"""Phase 2.10 — Controlled live target convergence tests.

Covers:
  1. client_order_id semantics (Phase 2.10 corrections):
     - same intent across different snap.seq => SAME id
     - changed cycle => different id
     - changed target => different id
     - changed remaining delta => different id
     - id <= 64 chars
  2. Allowlist:
     - allowlisted registration => write permitted
     - non-allowlisted registration => zero writes
  3. Live convergence algorithm:
     - flat -> BUY exact target
     - LONG below target -> BUY exact difference
     - LONG equal target -> no order
     - LONG above target -> no reduction
     - SHORT actual -> no order, no flip
     - target zero -> no order, no close
     - BEFORE read failure -> zero writes
     - cancel failure -> zero new_order
     - AFTER read failure -> zero new_order
     - after cancel/re-read target already achieved -> zero new_order
     - at most one new_order per convergence call
  4. Static guard:
     - no close_position reachable
     - no TP/SL operations reachable
     - no ladder reachable
     - no market_order/limit_order reachable (single market order type)
"""

from __future__ import annotations

import re
import unittest
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from types import SimpleNamespace

from plugins.trade.fibo.executor import (
    _fibo_client_order_id, _format_decimal,
)
from plugins.trade.fibo.live import (
    ALLOWED_ACCOUNT, ALLOWED_EXCHANGE, ALLOWED_EXCHANGE_INSTRUMENT,
    ALLOWED_OPERATIONS, ALLOWED_SIDE_BUY, ALLOWED_VARIANT,
    is_allowlisted, live_converge, LiveConvergeResult,
)
from plugins.trade.fibo.snapshot import Mt4Snapshot, Mt4Fibo
from plugins.trade.fibo.store import FiboRegistration


# ---------------------------------------------------------------------------
# Test doubles
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
    calls: List[Dict[str, Any]] = field(default_factory=list)


def _stub_executor(
    *,
    reads: Optional[List[Tuple[Optional[Dict[str, Any]],
                              Optional[List[Dict[str, Any]]]]]] = None,
    new_order_result: Optional[_FakeResponse] = None,
    cancel_order_group_result: Optional[_FakeResponse] = None,
    raise_on: Optional[List[str]] = None,
) -> Tuple[Callable[[Dict[str, Any]], Any], _ExecLog]:
    log = _ExecLog()
    if not reads:
        reads = [(None, None)]
    po_call = {"n": 0}
    forbidden = (
        "new_order", "market_order", "limit_order", "ladder",
        "cancel_order", "close_position", "stop_order",
        "set_tp", "set_sl", "set_position_trigger",
        "set_position_protections",
    )

    def _fn(req: Dict[str, Any]) -> Any:
        log.calls.append(dict(req))
        op = req.get("operation")
        if raise_on and op in raise_on:
            raise RuntimeError(f"simulated failure on {op}")
        # ALLOWED ops from the executor's perspective. cancel_order
        # is NOT in the executor's allowed set; reject it loudly
        # so a regression is caught.
        if op in forbidden and op not in {"new_order",
                                          "cancel_order_group"}:
            raise AssertionError(
                f"live_converge invoked forbidden op: {op!r}"
            )
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
        raise AssertionError(f"unknown op {op!r}")

    return _fn, log


def _allowlisted_reg() -> FiboRegistration:
    return FiboRegistration.build(
        exchange="ondoperps", account="BITGET",
        symbol="ETHUSD", variant="NORMALFib", side="BUY",
        starting_volume="0.001",
        source="obs-1", source_seq=1, source_cycle_id=47022998,
        source_cumulative_weight="2.0", source_percentage="0.01",
        source_snapshot_received_at="2026-08-27T00:00:00Z",
        desired_exchange_size=Decimal("0.002"),
        source_symbol="ETHUSD",
        exchange_instrument="ETH-USD.P",
    )


def _non_allowlisted_reg(
    *, exchange="hyperliquid", account="BASED",
    instrument="SOL", variant="NORMALFib", side="SELL",
    starting_volume="0.15",
) -> FiboRegistration:
    return FiboRegistration.build(
        exchange=exchange, account=account,
        symbol="SOLUSD", variant=variant, side=side,
        starting_volume=starting_volume,
        source="obs-1", source_seq=1, source_cycle_id=47022523,
        source_cumulative_weight="8", source_percentage="0.01",
        source_snapshot_received_at="2026-08-27T00:00:00Z",
        desired_exchange_size=Decimal("1.20"),
        source_symbol="SOLUSD",
        exchange_instrument=instrument,
    )


def _snap(
    *,
    symbol: str = "ETHUSD",
    variant: str = "NORMALFib",
    buy_cycle: int = 47022998,
    buy_weight: str = "2.0",
    sell_cycle: int = 0,
    sell_weight: str = "0",
) -> Mt4Snapshot:
    fibo = Mt4Fibo(
        symbol=symbol, variant=variant,
        percentage=Decimal("0.01"),
        buy_cycle_id=buy_cycle,
        cumulative_buy_weight=Decimal(buy_weight),
        sell_cycle_id=sell_cycle,
        cumulative_sell_weight=Decimal(sell_weight),
    )
    return Mt4Snapshot(
        v=1, source="obs-1", seq=1, ts=1, fibos=[fibo],
        received_at="2026-08-27T00:00:00Z",
        telegram_update_id=1, telegram_message_id=1, reader_chat_id=1,
    )


# ---------------------------------------------------------------------------
# 1. client_order_id semantics
# ---------------------------------------------------------------------------


class ClientOrderIdSemanticsTests(unittest.TestCase):
    """Phase 2.10 corrected hash semantics: drop snap.seq."""

    def _make_delta(self):
        return SimpleNamespace(side="buy", size=Decimal("0.002"))

    def _target(self, side="buy", size="0.002"):
        return SimpleNamespace(side=side, size=Decimal(size))

    def test_same_intent_same_id_across_snapshot_seqs(self):
        """Phase 2.10: snap.seq MUST NOT change the id."""
        import dataclasses
        reg = _allowlisted_reg()
        target = self._target()
        delta = self._make_delta()
        cid1 = _fibo_client_order_id(
            reg, source="obs-1", cycle_id=42,
            target=target, delta=delta,
        )
        snap1 = _snap(buy_cycle=42, buy_weight="2.0")
        snap2 = dataclasses.replace(snap1, seq=snap1.seq + 5)
        cid2 = _fibo_client_order_id(
            reg, source=snap2.source, cycle_id=42,
            target=target, delta=delta,
        )
        self.assertEqual(cid1, cid2,
                         "snap.seq must NOT change the id")

    def test_changed_cycle_different_id(self):
        reg = _allowlisted_reg()
        target = self._target()
        delta = self._make_delta()
        cid1 = _fibo_client_order_id(
            reg, source="obs-1", cycle_id=42,
            target=target, delta=delta,
        )
        cid2 = _fibo_client_order_id(
            reg, source="obs-1", cycle_id=99,
            target=target, delta=delta,
        )
        self.assertNotEqual(cid1, cid2)

    def test_changed_target_different_id(self):
        reg = _allowlisted_reg()
        delta = self._make_delta()
        t1 = self._target(size="0.002")
        t2 = self._target(size="0.003")
        cid1 = _fibo_client_order_id(
            reg, source="obs-1", cycle_id=42,
            target=t1, delta=delta,
        )
        cid2 = _fibo_client_order_id(
            reg, source="obs-1", cycle_id=42,
            target=t2, delta=delta,
        )
        self.assertNotEqual(cid1, cid2)

    def test_changed_remaining_delta_different_id(self):
        reg = _allowlisted_reg()
        target = self._target()
        delta1 = SimpleNamespace(side="buy", size=Decimal("0.002"))
        delta2 = SimpleNamespace(side="buy", size=Decimal("0.0013"))
        cid1 = _fibo_client_order_id(
            reg, source="obs-1", cycle_id=42,
            target=target, delta=delta1,
        )
        cid2 = _fibo_client_order_id(
            reg, source="obs-1", cycle_id=42,
            target=target, delta=delta2,
        )
        self.assertNotEqual(cid1, cid2)

    def test_id_length_under_64(self):
        reg = _allowlisted_reg()
        target = self._target()
        delta = self._make_delta()
        cid = _fibo_client_order_id(
            reg, source="obs-1", cycle_id=42,
            target=target, delta=delta,
        )
        self.assertLessEqual(len(cid), 64)
        self.assertTrue(cid.startswith("fibo-"))


# ---------------------------------------------------------------------------
# 2. Allowlist
# ---------------------------------------------------------------------------


class AllowlistTests(unittest.TestCase):

    def test_allowlisted_registration_matches(self):
        reg = _allowlisted_reg()
        self.assertTrue(is_allowlisted(reg))

    def test_non_allowlisted_registration_rejected(self):
        # Different exchange.
        reg = _non_allowlisted_reg(exchange="hyperliquid",
                                     account="BASED",
                                     instrument="SOL")
        self.assertFalse(is_allowlisted(reg))

    def test_wrong_account_rejected(self):
        reg = _non_allowlisted_reg(exchange="ondoperps",
                                     account="other_acct",
                                     instrument="ETH-USD.P")
        self.assertFalse(is_allowlisted(reg))

    def test_wrong_instrument_rejected(self):
        reg = _non_allowlisted_reg(exchange="ondoperps",
                                     account="BITGET",
                                     instrument="ETH-USDC.P")
        self.assertFalse(is_allowlisted(reg))

    def test_wrong_variant_rejected(self):
        reg = _non_allowlisted_reg(exchange="ondoperps",
                                     account="BITGET",
                                     instrument="ETH-USD.P",
                                     variant="FASTFib")
        self.assertFalse(is_allowlisted(reg))

    def test_wrong_side_rejected(self):
        reg = _non_allowlisted_reg(exchange="ondoperps",
                                     account="BITGET",
                                     instrument="ETH-USD.P",
                                     variant="NORMALFib",
                                     side="BUY")  # Same side — fails other axis
        # Use the allowlisted reg but mutate side to SELL.
        reg_sell = _allowlisted_reg()
        # Re-build with side=SELL:
        from plugins.trade.fibo.store import FiboRegistration
        new = FiboRegistration.build(
            exchange="ondoperps", account="BITGET",
            symbol="ETHUSD", variant="NORMALFib", side="SELL",
            starting_volume="0.5",
            source="obs-1", source_seq=1, source_cycle_id=46871101,
            source_cumulative_weight="4", source_percentage="0.01",
            source_snapshot_received_at="2026-08-27T00:00:00Z",
            desired_exchange_size=Decimal("2.0"),
            source_symbol="ETHUSD",
            exchange_instrument="ETH-USD.P",
        )
        self.assertFalse(is_allowlisted(new))


# ---------------------------------------------------------------------------
# 3. Live convergence algorithm
# ---------------------------------------------------------------------------


class LiveConvergeFlatToOpenTests(unittest.TestCase):
    """Flat venue, MT4 active -> BUY exact target."""

    def test_flat_buys_exact_target(self):
        reg = _allowlisted_reg()
        snap = _snap(buy_cycle=42, buy_weight="2.0")  # target 0.002
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0"}, []
            )],
        )
        result = live_converge(reg, snap, execute_fn=execute)
        self.assertTrue(result.allowlisted)
        self.assertTrue(result.placed_live_order)
        self.assertIsNotNone(result.placed_request)
        req = result.placed_request
        self.assertEqual(req["operation"], "new_order")
        self.assertEqual(req["exchange"], "ondoperps")
        self.assertEqual(req["account"], "BITGET")
        self.assertEqual(req["symbol"], "ETH-USD.P")
        self.assertEqual(req["side"], "buy")
        self.assertEqual(req["order_type"], "market")
        self.assertEqual(req["volume"], "0.002")
        self.assertEqual(req["reduce_only"], False)
        self.assertTrue(req["client_order_id"].startswith("fibo-"))
        self.assertLessEqual(len(req["client_order_id"]), 64)
        # Exactly one new_order placed.
        new_orders = [c for c in log.calls
                      if c["operation"] == "new_order"]
        self.assertEqual(len(new_orders), 1)


class LiveConvergeLongBelowTargetTests(unittest.TestCase):
    """LONG below target -> BUY the difference."""

    def test_long_below_buys_difference(self):
        reg = _allowlisted_reg()
        snap = _snap(buy_cycle=42, buy_weight="2.0")  # target 0.002
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0.0005"},
                [],
            )],
        )
        result = live_converge(reg, snap, execute_fn=execute)
        self.assertTrue(result.placed_live_order)
        self.assertEqual(result.placed_request["volume"], "0.0015")
        self.assertEqual(result.placed_request["side"], "buy")


class LiveConvergeLongEqualTargetTests(unittest.TestCase):
    """LONG equal target -> no order."""

    def test_long_equal_no_order(self):
        reg = _allowlisted_reg()
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0.002"}, []
            )],
        )
        result = live_converge(reg, snap, execute_fn=execute)
        self.assertFalse(result.placed_live_order)
        self.assertEqual(result.placed_request, None)
        new_orders = [c for c in log.calls
                      if c["operation"] == "new_order"]
        self.assertEqual(new_orders, [])


class LiveConvergeLongAboveTargetTests(unittest.TestCase):
    """LONG above target -> NO reduction."""

    def test_long_above_no_reduction(self):
        reg = _allowlisted_reg()
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0.003"}, []
            )],
        )
        result = live_converge(reg, snap, execute_fn=execute)
        self.assertFalse(result.placed_live_order)
        new_orders = [c for c in log.calls
                      if c["operation"] == "new_order"]
        self.assertEqual(new_orders, [])


class LiveConvergeShortActualTests(unittest.TestCase):
    """SHORT actual -> no order, no flip."""

    def test_short_actual_no_flip(self):
        reg = _allowlisted_reg()
        snap = _snap(buy_cycle=42, buy_weight="2.0")  # target BUY 0.002
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "short", "size": "0.005"},
                [],
            )],
        )
        result = live_converge(reg, snap, execute_fn=execute)
        self.assertFalse(result.placed_live_order)
        new_orders = [c for c in log.calls
                      if c["operation"] == "new_order"]
        self.assertEqual(new_orders, [])
        self.assertIn("opposite", result.blocked_reason)


class LiveConvergeTargetZeroTests(unittest.TestCase):
    """target zero -> no order, no close."""

    def test_target_zero_no_flatten(self):
        reg = _allowlisted_reg()
        snap = _snap(buy_cycle=0, buy_weight="0")  # cycle inactive
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0.003"}, []
            )],
        )
        result = live_converge(reg, snap, execute_fn=execute)
        self.assertFalse(result.placed_live_order)
        new_orders = [c for c in log.calls
                      if c["operation"] == "new_order"]
        self.assertEqual(new_orders, [])
        self.assertIn("target flat", result.blocked_reason)
        cancels = [c for c in log.calls
                   if c["operation"] == "cancel_order_group"]
        self.assertEqual(cancels, [])


class LiveConvergeReadFailureTests(unittest.TestCase):
    """BEFORE / AFTER read failure -> zero writes."""

    def test_before_read_failure_blocks(self):
        reg = _allowlisted_reg()
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(raise_on=["positions_orders"])
        result = live_converge(reg, snap, execute_fn=execute)
        self.assertTrue(result.read_failed)
        self.assertFalse(result.placed_live_order)
        new_orders = [c for c in log.calls
                      if c["operation"] == "new_order"]
        self.assertEqual(new_orders, [])

    def test_before_read_failure_response_blocks(self):
        reg = _allowlisted_reg()
        snap = _snap(buy_cycle=42, buy_weight="2.0")

        def _patched(req):
            op = req.get("operation")
            if op == "positions_orders":
                return _FakeResponse(
                    success=False, operation=op,
                    error={"code": "RATE_LIMITED",
                           "message": "too_many_requests"},
                )
            raise AssertionError(f"unexpected op {op}")

        result = live_converge(reg, snap, execute_fn=_patched)
        self.assertTrue(result.read_failed)
        self.assertFalse(result.placed_live_order)

    def test_after_read_failure_blocks(self):
        reg = _allowlisted_reg()
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        call_count = {"po": 0}

        def _fn(req):
            op = req.get("operation")
            if op == "positions_orders":
                call_count["po"] += 1
                if call_count["po"] == 1:
                    return _FakeResponse(
                        success=True, operation=op,
                        positions=[{"symbol": "ETH-USD.P",
                                    "side": "buy", "size": "0"}],
                        order_groups=[],
                    )
                raise RuntimeError("AFTER read failure")
            raise AssertionError(f"unexpected op {op}")

        result = live_converge(reg, snap, execute_fn=_fn)
        self.assertTrue(result.read_failed)
        self.assertFalse(result.placed_live_order)


class LiveConvergeCancelFailureTests(unittest.TestCase):
    """Cancel failure -> zero new_order."""

    def test_cancel_failure_blocks_new_order(self):
        reg = _allowlisted_reg()
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0"},
                [{"symbol": "ETH-USD.P", "side": "buy",
                  "total_size": "0.005"}],
            )],
            cancel_order_group_result=_FakeResponse(
                success=False, operation="cancel_order_group",
                error={"code": "UNKNOWN", "message": "noop"},
            ),
        )
        result = live_converge(reg, snap, execute_fn=execute)
        self.assertTrue(result.cancel_failed)
        self.assertFalse(result.placed_live_order)
        new_orders = [c for c in log.calls
                      if c["operation"] == "new_order"]
        self.assertEqual(new_orders, [])


class LiveConvergeTargetAchievedTests(unittest.TestCase):
    """After cancel/re-read, target already achieved -> zero new_order."""

    def test_target_achieved_after_cancel(self):
        reg = _allowlisted_reg()
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        # Simulate: BEFORE flat + matching pending BUY 0.005.
        # After cancel, a parallel order fills the gap (rival
        # executor) and we're already at target.
        execute, log = _stub_executor(
            reads=[
                ({"symbol": "ETH-USD.P", "side": "buy", "size": "0"},
                 [{"symbol": "ETH-USD.P", "side": "buy",
                   "total_size": "0.005"}]),
                # After cancel + re-read: position is already
                # LONG 0.002 (a parallel fill happened).
                ({"symbol": "ETH-USD.P", "side": "buy", "size": "0.002"},
                 []),
            ],
        )
        result = live_converge(reg, snap, execute_fn=execute)
        self.assertFalse(result.placed_live_order)
        new_orders = [c for c in log.calls
                      if c["operation"] == "new_order"]
        self.assertEqual(new_orders, [])


class LiveConvergeAtMostOneOrderTests(unittest.TestCase):
    """At most one new_order per convergence call."""

    def test_one_order_only(self):
        reg = _allowlisted_reg()
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0"}, []
            )],
        )
        result = live_converge(reg, snap, execute_fn=execute)
        new_orders = [c for c in log.calls
                      if c["operation"] == "new_order"]
        self.assertEqual(len(new_orders), 1,
                         "MUST place at most ONE new_order per call")


class NonAllowlistedTests(unittest.TestCase):
    """Non-allowlisted registration -> zero writes (no TradeDesk)."""

    def test_hyperliquid_reg_blocked_no_calls(self):
        reg = _non_allowlisted_reg()
        snap = _snap(symbol="SOLUSD", buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor()
        result = live_converge(reg, snap, execute_fn=execute)
        self.assertFalse(result.allowlisted)
        self.assertFalse(result.placed_live_order)
        # No TradeDesk calls whatsoever.
        self.assertEqual(log.calls, [])


# ---------------------------------------------------------------------------
# 4. Static guard: no write op tokens reachable from live.py
# ---------------------------------------------------------------------------


class LiveStaticGuardTests(unittest.TestCase):

    def test_no_close_position_token(self):
        """Static guard: ``live.py`` must NOT contain tokens for
        close_position, set_tp/sl, set_position_protections, ladder,
        stop_order, or single cancel_order (cancel_order_group is
        allowed). The docstring may LEGITIMATELY mention these
        tokens as part of the Deliberately NOT implemented list.
        The guard scans only module-level executable code."""
        import inspect
        import re
        from plugins.trade import fibo
        import os
        path = os.path.join(
            os.path.dirname(fibo.__file__),
            "live.py",
        )
        text = open(path).read()
        # Strip the module docstring so the "NOT implemented" list
        # inside it does not trip the guard.
        with open(path) as f:
            full = f.read()
        text_no_doc = re.sub(r'^""".*?"""', "", full, count=1, flags=re.DOTALL)
        for forbidden in (
            "close_position",
            "set_tp",
            "set_sl",
            "set_position_trigger",
            "set_position_protections",
            "ladder",
            "stop_order",
            "market_order",   # not a registered op; redundant guard
            "limit_order",    # not a registered op; redundant guard
            "cancel_order ",  # single cancel_order — cancel_order_group is allowed
        ):
            self.assertNotIn(
                forbidden, text_no_doc,
                f"forbidden token {forbidden!r} found in live.py code",
            )

    def test_allowed_operations_set_is_exact(self):
        # The static guard MUST be exactly the three allowed ops.
        # If a future refactor adds an op to this set, the test
        # below forces the developer to acknowledge it explicitly.
        self.assertEqual(
            ALLOWED_OPERATIONS,
            frozenset(
                {"positions_orders", "cancel_order_group", "new_order"}
            ),
        )


if __name__ == "__main__":
    unittest.main()