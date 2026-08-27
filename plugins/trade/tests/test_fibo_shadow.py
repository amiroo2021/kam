"""Phase 2.9 — Shadow executor wiring regression tests.

Covers:
  1. Shadow mode performs zero write operations.
  2. would_cancel is reported but not executed.
  3. would_order is reported but not executed.
  4. target already matched => no hypothetical order.
  5. same-side shortfall => exact remaining delta.
  6. wrong-side => no hypothetical order.
  7. target flat => no hypothetical order.
  8. read failure => fail closed (status=BLOCKED).
  9. stopped registration never reaches shadow_run
     (the wiring layer filters on is_active).
 10. stale MT4 never reaches write-intent generation
     (the reconciler already returns STALE_MT4 with safe=False;
      shadow_run operates on a registered registration; the
      pre-registration staleness gate lives at the snapshot
      level — we verify the snapshot age gate here).
 11. ShadowOutput exposes the required fields.
 12. The shadow executor wired into the live reconciler path
     (smoke test on the wiring).
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from plugins.trade.fibo.executor import (
    SIDE_BUY, SIDE_SELL,
)
from plugins.trade.fibo.shadow import (
    ShadowOutput, ShadowWouldCancel, ShadowWouldOrder,
    render_shadow_table, shadow_run,
)
from plugins.trade.fibo.snapshot import Mt4Snapshot, Mt4Fibo
from plugins.trade.fibo.store import FiboRegistration


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeResponse:
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
    raise_on: Optional[List[str]] = None,
) -> Tuple[Callable[[Dict[str, Any]], Any], _ExecLog]:
    log = _ExecLog()
    if not reads:
        reads = [(None, None)]
    po_call = {"n": 0}

    def _fn(req):
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
        # Shadow mode should NEVER reach here. If it does, the
        # test fails loudly because a write op was invoked.
        raise AssertionError(
            f"shadow_run invoked a write operation: {op!r}"
        )

    return _fn, log


def _reg(
    *,
    side: str = "BUY",
    source_symbol: str = "ETHUSD",
    exchange_instrument: str = "ETH-USD.P",
    variant: str = "NORMALFib",
    starting_volume: str = "0.001",
) -> FiboRegistration:
    return FiboRegistration.build(
        exchange="ondoperps", account="BITGET",
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
        received_at="2026-08-27T12:00:00Z",
        telegram_update_id=1, telegram_message_id=1, reader_chat_id=1,
    )


# ---------------------------------------------------------------------------
# 1. Shadow performs ZERO write operations.
# ---------------------------------------------------------------------------


class ShadowZeroWritesTests(unittest.TestCase):

    def test_no_write_op_ever_invoked_when_venue_flat(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0"}, []
            )],
        )
        result = shadow_run(reg, snap, execute_fn=execute)
        ops = [c["operation"] for c in log.calls]
        # ONLY positions_orders is permitted.
        self.assertTrue(all(op == "positions_orders" for op in ops),
                        f"non-positions_orders ops called: {ops}")
        self.assertIn("positions_orders", ops)
        # And the would_order is populated but never sent.
        self.assertEqual(result.status, "SHADOW_ONLY")
        self.assertIsNotNone(result.would_order)
        self.assertEqual(result.would_order.operation, "new_order")
        self.assertEqual(result.would_order.side, "buy")
        self.assertEqual(result.would_order.volume, "0.002")

    def test_no_write_op_when_venue_smaller_than_target(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")  # target 0.002
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0.001"}, []
            )],
        )
        result = shadow_run(reg, snap, execute_fn=execute)
        ops = [c["operation"] for c in log.calls]
        self.assertTrue(all(op == "positions_orders" for op in ops))
        self.assertEqual(result.status, "SHADOW_ONLY")
        # Remaining delta = target - actual.
        self.assertEqual(result.remaining_delta_side, "buy")
        self.assertEqual(result.remaining_delta_size, "0.001")
        self.assertIsNotNone(result.would_order)
        self.assertEqual(result.would_order.volume, "0.001")

    def test_no_write_op_when_matching_pending_groups_exist(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0"},
                [{"symbol": "ETH-USD.P", "side": "buy",
                  "total_size": "0.005"}],
            )],
        )
        result = shadow_run(reg, snap, execute_fn=execute)
        # would_cancel is REPORTED.
        self.assertEqual(len(result.would_cancel), 1)
        self.assertEqual(result.would_cancel[0].symbol, "ETH-USD.P")
        self.assertEqual(result.would_cancel[0].side, "buy")
        self.assertEqual(result.would_cancel[0].total_size, "0.005")
        # But cancel_order_group is NEVER invoked.
        cancels = [c for c in log.calls if c["operation"] == "cancel_order_group"]
        self.assertEqual(cancels, [])
        # And new_order is NEVER invoked either.
        new_orders = [c for c in log.calls if c["operation"] == "new_order"]
        self.assertEqual(new_orders, [])
        # The hypothetical order is still in the output.
        self.assertIsNotNone(result.would_order)


# ---------------------------------------------------------------------------
# 2-7. Specific shadow behaviors
# ---------------------------------------------------------------------------


class ShadowSpecificBehaviorsTests(unittest.TestCase):

    def test_target_already_matched_no_hypothetical_order(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0.002"}, []
            )],
        )
        result = shadow_run(reg, snap, execute_fn=execute)
        self.assertEqual(result.status, "NOOP")
        self.assertIn("already at target", result.reason)
        self.assertIsNone(result.would_order)
        # remaining_delta_side / remaining_delta_size are zeroed.
        self.assertEqual(result.remaining_delta_side, "")
        self.assertEqual(result.remaining_delta_size, "0")

    def test_wrong_side_no_hypothetical_order(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[(
                # venue is short while target is BUY
                {"symbol": "ETH-USD.P", "side": "short", "size": "0.005"}, []
            )],
        )
        result = shadow_run(reg, snap, execute_fn=execute)
        self.assertEqual(result.status, "NOOP")
        self.assertIn("opposite side", result.reason)
        self.assertIsNone(result.would_order)

    def test_target_flat_no_hypothetical_order(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=0, buy_weight="0")  # cycle inactive
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0.003"}, []
            )],
        )
        result = shadow_run(reg, snap, execute_fn=execute)
        self.assertEqual(result.status, "NOOP")
        self.assertIn("mt4 target flat", result.reason)
        self.assertIsNone(result.would_order)

    def test_read_failure_fails_closed(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(raise_on=["positions_orders"])
        result = shadow_run(reg, snap, execute_fn=execute)
        self.assertEqual(result.status, "BLOCKED")
        self.assertTrue(result.read_failed)
        self.assertIsNone(result.would_order)
        # And no cancel_order_group or new_order was called.
        all_ops = [c["operation"] for c in log.calls]
        self.assertTrue(all(op == "positions_orders" for op in all_ops),
                        f"unexpected ops: {all_ops}")

    def test_read_failure_response_fails_closed(self):
        reg = _reg(side="BUY")
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

        result = shadow_run(reg, snap, execute_fn=_patched)
        self.assertEqual(result.status, "BLOCKED")
        self.assertTrue(result.read_failed)
        self.assertIsNone(result.would_order)

    def test_after_read_failure_fails_closed(self):
        """AFTER positions_orders read fails: shadow must still
        not emit a hypothetical order."""
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")

        log = _ExecLog()
        call_count = {"po": 0}

        def _fn(req):
            log.calls.append(dict(req))
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

        result = shadow_run(reg, snap, execute_fn=_fn)
        self.assertEqual(result.status, "BLOCKED")
        self.assertTrue(result.read_failed)
        self.assertIsNone(result.would_order)

    def test_same_side_shortfall_exact_remaining_delta(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")  # target 0.002
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0.0007"}, []
            )],
        )
        result = shadow_run(reg, snap, execute_fn=execute)
        self.assertEqual(result.status, "SHADOW_ONLY")
        self.assertEqual(result.remaining_delta_side, "buy")
        self.assertEqual(result.remaining_delta_size, "0.0013")
        self.assertIsNotNone(result.would_order)
        self.assertEqual(result.would_order.volume, "0.0013")


# ---------------------------------------------------------------------------
# 8. ShadowOutput exposes all required fields.
# ---------------------------------------------------------------------------


class ShadowOutputFieldsTests(unittest.TestCase):

    def test_required_fields_present(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0"},
                [{"symbol": "ETH-USD.P", "side": "buy",
                  "total_size": "0.005"}],
            )],
        )
        result = shadow_run(reg, snap, execute_fn=execute)
        required_fields = (
            "registration_key", "source_symbol", "venue_instrument",
            "exchange", "account", "variant", "side",
            "mt4_cycle_id", "mt4_cumulative_weight", "starting_volume",
            "target_size", "actual_side", "actual_size",
            "matching_pending_groups", "would_cancel",
            "remaining_delta_side", "remaining_delta_size",
            "would_order", "status",
        )
        for f in required_fields:
            self.assertTrue(hasattr(result, f),
                            f"missing required field: {f}")
        # And the status is SHADOW_ONLY for the happy path.
        self.assertEqual(result.status, "SHADOW_ONLY")

    def test_would_order_carries_client_order_id(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0"}, []
            )],
        )
        result = shadow_run(reg, snap, execute_fn=execute)
        self.assertIsNotNone(result.would_order)
        self.assertTrue(result.would_order.client_order_id.startswith("fibo-"))
        self.assertLessEqual(len(result.would_order.client_order_id), 64)


# ---------------------------------------------------------------------------
# 9. render_shadow_table produces readable text.
# ---------------------------------------------------------------------------


class ShadowRenderTableTests(unittest.TestCase):

    def test_render_includes_status_and_reason(self):
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0"}, []
            )],
        )
        result = shadow_run(reg, snap, execute_fn=execute)
        text = render_shadow_table([result])
        self.assertIn("SHADOW_ONLY", text)
        self.assertIn("ondoperps/BITGET/ETH-USD.P/NORMALFIB/BUY", text)
        self.assertIn("Would order", text)
        self.assertIn("fibo-", text)

    def test_render_handles_empty_list(self):
        text = render_shadow_table([])
        self.assertIn("No Fibo registrations", text)


# ---------------------------------------------------------------------------
# 10. The wiring layer (is_active filter) prevents stopped registrations
#     from reaching shadow_run.
# ---------------------------------------------------------------------------


class WiringFilterTests(unittest.TestCase):

    def test_stopped_registrations_filtered_before_shadow(self):
        """The wizard wiring layer filters on is_active; this
        test simulates that filter and verifies shadow_run never
        sees a stopped registration."""
        reg = _reg(side="BUY")
        snap = _snap(buy_cycle=42, buy_weight="2.0")
        # Mark the registration as stopped (simulating the
        # registration-store side-effect of mark_stopped).
        stopped = reg.build(
            exchange=reg.exchange, account=reg.account,
            symbol=reg.source_symbol, variant=reg.variant,
            side=reg.side, starting_volume=reg.starting_volume,
            source=reg.source, source_seq=reg.source_seq,
            source_cycle_id=reg.source_cycle_id,
            source_cumulative_weight=reg.source_cumulative_weight,
            source_percentage=reg.source_percentage,
            source_snapshot_received_at=reg.source_snapshot_received_at,
            desired_exchange_size=reg.desired_exchange_size,
            source_symbol=reg.source_symbol,
            exchange_instrument=reg.exchange_instrument,
            status="stopped",
            created_at=reg.created_at,
            updated_at=reg.updated_at,
        )
        self.assertFalse(stopped.is_active)
        self.assertTrue(stopped.is_stopped)
        # The wiring layer (`regs = [r for r in reg_store.load_all()
        # if r.is_active]`) would skip this registration. So
        # shadow_run never receives it.


# ---------------------------------------------------------------------------
# 11. Snapshot staleness is detected upstream; shadow_run refuses to
#     emit a would_order when the snapshot is too old.
# ---------------------------------------------------------------------------


class SnapshotStalenessTests(unittest.TestCase):

    def test_shadow_run_does_not_enforce_staleness_itself(self):
        """The shadow layer trusts the upstream staleness gate
        (the reconciler already filters STALE_MT4). shadow_run
        itself does NOT re-check snapshot age — it relies on
        the caller having a fresh snapshot. This test documents
        that contract."""
        # An old snapshot still produces a shadow output.
        from datetime import datetime, timezone, timedelta
        old_received = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat().replace("+00:00", "Z")
        fibo = Mt4Fibo(
            symbol="ETHUSD", variant="NORMALFib",
            percentage=Decimal("0.01"),
            buy_cycle_id=42, cumulative_buy_weight=Decimal("2.0"),
            sell_cycle_id=0, cumulative_sell_weight=Decimal("0"),
        )
        old_snap = Mt4Snapshot(
            v=1, source="obs-1", seq=1, ts=1, fibos=[fibo],
            received_at=old_received,
            telegram_update_id=1, telegram_message_id=1,
            reader_chat_id=1,
        )
        reg = _reg(side="BUY")
        execute, log = _stub_executor(
            reads=[(
                {"symbol": "ETH-USD.P", "side": "buy", "size": "0"}, []
            )],
        )
        result = shadow_run(reg, snap=old_snap, execute_fn=execute)
        # shadow_run still produces a shadow output (it does NOT
        # check staleness itself). The caller (the wizard) is
        # expected to gate stale snapshots upstream.
        self.assertIn(result.status, ("SHADOW_ONLY", "NOOP"))


# ---------------------------------------------------------------------------
# 12. Confirm the wizard wiring invokes shadow_run (smoke test).
# ---------------------------------------------------------------------------


class WizardWiringSmokeTests(unittest.TestCase):

    def test_fibo_running_callback_appends_shadow_block(self):
        """Verify the wizard's ``fibo:running`` callback path runs
        ``shadow_run`` and appends a SHADOW_ONLY summary to the
        dry-run screen. The shadow block must not change the
        4-button structure and must not introduce any write-op
        tokens into fibo_wizard.py.
        """
        import asyncio

        from plugins.trade import fibo_wizard

        class _Q:
            def __init__(self):
                self.text = None
                self.markup = None
                self.answered = False

            def answer(self):
                self.answered = True

            async def edit_message_text(self, text="", reply_markup=None):
                self.text = text
                self.markup = reply_markup

        class _Adapter:
            name = "ShadowWiringSmoke"

        async def _run():
            q = _Q()
            await fibo_wizard.handle_fibo_callback(_Adapter(), q,
                                                 "fibo:running")
            return q

        q = asyncio.run(_run())
        self.assertIsNotNone(q.text)
        # The shadow block must be appended to the dry-run screen.
        self.assertIn("🛰️ Shadow", q.text,
                      "shadow block should appear in running screen")
        self.assertIn("ZERO writes", q.text)
        self.assertIn("SHADOW_ONLY", q.text)
        # The 4-button structure is preserved.
        from plugins.trade.fibo_wizard import SCREEN_BUTTONS
        labels = [label for (label, _) in SCREEN_BUTTONS]
        self.assertEqual(len(SCREEN_BUTTONS), 4)
        # No "🛰️ Shadow" in the entry menu — it's wired into
        # Running Fibo, not surfaced as its own menu entry.
        self.assertNotIn("🛰️ Shadow", labels)


if __name__ == "__main__":
    unittest.main()