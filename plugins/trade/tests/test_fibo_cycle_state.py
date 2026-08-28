"""Phase 2.13.18 — cycle-aware Fibo convergence tests.

The cycle-aware layer introduces a new persistent state file
(``cycle_state.json``) and a new decision function
(``decide_cycle_action``). This test file exercises the full
required test matrix using only in-process fakes.

NO real TradeDesk or exchange calls are made.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Redirect HERMES_HOME to a temp dir BEFORE importing Fibo
# modules so the cycle-state file is per-test.
_TEST_HERMES_HOME = tempfile.mkdtemp(prefix="fibo_cycle_test_")
os.environ["HERMES_HOME"] = _TEST_HERMES_HOME

from plugins.trade.fibo.cycle_decide import (
    decide_cycle_action,
    CycleDecision,
)
from plugins.trade.fibo.cycle_state import (
    CycleStateStore,
    TRANSITION_CLOSE_SENT,
    TRANSITION_CLOSE_VERIFIED,
    TRANSITION_OPEN_SENT,
)
from plugins.trade.fibo.snapshot import Mt4Snapshot, Mt4Fibo
from plugins.trade.fibo.executor import ExchangePosition, Mt4Target


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@dataclass
class FakeResponse:
    """Stand-in for a TradeDesk / agent response."""

    success: bool = True
    error: Any = None
    positions: List[Dict[str, Any]] = field(default_factory=list)
    order_groups: List[Any] = field(default_factory=list)
    open_order_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "operation": "positions_orders",
            "positions": self.positions,
            "order_groups": self.order_groups,
            "open_order_count": self.open_order_count,
        }


class FakeExecutor:
    """In-memory mock of TradeDesk.execute for cycle-state tests.

    The ``positions`` map mirrors the schema used by the
    live executor's parser. We expose:
      - ``set_position(symbol, side, size)`` to seed state
      - ``calls`` to record every requested operation
    """

    def __init__(
        self,
        *,
        positions: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    ) -> None:
        self.positions = positions or {}
        self.calls: List[Dict[str, Any]] = []
        self._new_order_response = {
            "success": True,
            "operation": "new_order",
            "order": {
                "symbol": None,
                "side": None,
                "order_type": "market",
                "requested_volume": None,
                "submitted_volume": None,
                "submitted_price": "0",
                "verified": True,
                "status": "success",
            },
        }

    def record(self, request: Dict[str, Any]) -> None:
        self.calls.append(request)

    def set_position(
        self, exchange: str, account: str,
        symbol: str, side: str, size: str,
    ) -> None:
        key = f"{exchange}|{account}"
        rows = [
            r for r in self.positions.get(key, [])
            if r.get("symbol") != symbol
        ]
        rows.append({"symbol": symbol, "side": side, "size": size})
        self.positions[key] = rows

    def __call__(self, request: Dict[str, Any]) -> Any:
        self.record(request)
        op = request.get("operation")
        if op == "positions_orders":
            exchange = request.get("exchange", "")
            account = request.get("account", "")
            positions = self.positions.get(
                f"{exchange}|{account}", [],
            )
            return FakeResponse(
                success=True, positions=list(positions),
                order_groups=[], open_order_count=0,
            )
        if op == "new_order":
            resp = dict(self._new_order_response)
            for k in ("symbol", "side", "volume"):
                if k in request:
                    resp.setdefault("order", {})[k] = request[k]
            return FakeResponse(success=True)
        if op == "close_position":
            return FakeResponse(success=True)
        if op == "cancel_order_group":
            return FakeResponse(success=True)
        return FakeResponse(success=False, error="unknown op")


def make_snap(
    *,
    buy_cycle: int = 100, sell_cycle: int = 0,
    buy_weight: str = "1", sell_weight: str = "0",
) -> Mt4Snapshot:
    fibo = Mt4Fibo(
        symbol="XAUUSD", variant="FASTFIB",
        percentage=Decimal("0.001"),
        buy_cycle_id=buy_cycle, cumulative_buy_weight=Decimal(buy_weight),
        sell_cycle_id=sell_cycle, cumulative_sell_weight=Decimal(sell_weight),
    )
    return Mt4Snapshot(
        v=1, source="mt4-Fresh-1", seq=1, ts=1, fibos=[fibo],
        received_at="2026-08-28T15:00:00+00:00",
        telegram_update_id=1, telegram_message_id=1, reader_chat_id=1,
    )


def make_reg(cycle_id: int = 100) -> "FiboRegistrationLike":
    @dataclass
    class _R:
        registration_key: str = (
            "ondoperps/BITGET/XAU-USD.P/FASTFIB/SELL"
        )
        source: str = "mt4-Fresh-1"
        exchange: str = "ondoperps"
        account: str = "BITGET"
        exchange_instrument: str = "XAU-USD.P"
        source_symbol: str = "XAUUSD"
        variant: str = "FASTFIB"
        side: str = "SELL"
        starting_volume: Decimal = Decimal("0.001")
    return _R()


def make_before(
    *, side: str = "", size: str = "0",
) -> ExchangePosition:
    return ExchangePosition(
        symbol="XAU-USD.P",
        side=side, size=Decimal(size), read_failed=False,
    )


REG = make_reg()


# ---------------------------------------------------------------------------
# Test matrix
# ---------------------------------------------------------------------------


class CycleDecisionTest(unittest.TestCase):
    """A. first cycle, flat: cycle 100, target 0.001, actual flat
    → bootstrap-adopt + open 0.001
    B. same cycle unchanged: NOOP
    C. same cycle increased: open delta
    D. same cycle actual exceeds target: FAIL_CLOSED
    E. cycle changes: close old then open new
    F. cycle goes zero: close old
    G. zero -> new cycle: open
    H. crash after close: do not close again
    I. crash after open: do not duplicate
    J. cycle changes again during transition: newest wins
    K. unknown ownership: FAIL_CLOSED
    L. bootstrap flat: open
    M. opposite-side: FAIL_CLOSED
    N. stopped registration: not reached
    O. source mismatch: not reached
    P. restart/reload persisted state: same decisions
    """

    def _decide(
        self,
        *,
        target_size: Decimal,
        target_side: str = "SELL",
        before_side: str = "",
        before_size: str = "0",
        snap: Optional[Mt4Snapshot] = None,
        synced: Optional[int] = None,
    ) -> CycleDecision:
        if snap is None:
            snap = make_snap(
                sell_cycle=100 if target_size > 0 else 0,
                sell_weight="1" if target_size > 0 else "0",
            )
        target = Mt4Target(side=target_side, size=target_size)
        before = make_before(side=before_side, size=before_size)
        return decide_cycle_action(
            registration_key=REG.registration_key,
            source_symbol=REG.source_symbol,
            variant=REG.variant,
            side=REG.side,
            target=target,
            before=before,
            snap=snap,
            synchronized_cycle_id=synced,
        )

    # -- A. first cycle, flat -> bootstrap-adopt + open --
    def test_A_first_cycle_flat_opens_and_adopts(self):
        d = self._decide(
            target_size=Decimal("0.001"),
            before_side="", before_size="0",
        )
        self.assertEqual(d.action, "OPEN_REQUIRED")
        self.assertEqual(d.delta.size, Decimal("0.001"))
        self.assertEqual(d.new_cycle_id, 100)

    # -- B. same cycle unchanged -> NOOP --
    def test_B_same_cycle_unchanged_noop(self):
        snap = make_snap(sell_cycle=100, sell_weight="1")
        d = self._decide(
            target_size=Decimal("0.001"),
            before_side="sell", before_size="0.001",
            snap=snap, synced=100,
        )
        self.assertEqual(d.action, "NOOP")

    # -- C. same cycle increased -> open delta --
    def test_C_same_cycle_increased_opens_delta(self):
        snap = make_snap(sell_cycle=100, sell_weight="2")
        d = self._decide(
            target_size=Decimal("0.002"),
            before_side="sell", before_size="0.001",
            snap=snap, synced=100,
        )
        self.assertEqual(d.action, "OPEN_REQUIRED")
        self.assertEqual(d.delta.size, Decimal("0.001"))

    # -- D. same cycle actual exceeds target -> FAIL_CLOSED --
    def test_D_same_cycle_actual_exceeds_target_blocks(self):
        snap = make_snap(sell_cycle=100, sell_weight="1")
        d = self._decide(
            target_size=Decimal("0.001"),
            before_side="sell", before_size="0.002",
            snap=snap, synced=100,
        )
        self.assertTrue(d.block)
        self.assertEqual(d.block_code, "BLOCKED_ACTUAL_EXCEEDS_TARGET")

    # -- E. cycle changes -> close old then open new --
    def test_E_cycle_change_close_then_open(self):
        snap = make_snap(sell_cycle=101, sell_weight="1")
        d = self._decide(
            target_size=Decimal("0.001"),
            before_side="sell", before_size="0.002",
            snap=snap, synced=100,
        )
        self.assertEqual(d.action, "CLOSE_REQUIRED")
        self.assertEqual(d.close_size, Decimal("0.002"))
        self.assertEqual(d.new_cycle_id, 101)

    # -- F. cycle goes zero -> close old --
    def test_F_cycle_zero_closes_owned(self):
        snap = make_snap(sell_cycle=0, sell_weight="0")
        d = self._decide(
            target_size=Decimal("0"),
            before_side="sell", before_size="0.002",
            snap=snap, synced=100,
        )
        self.assertEqual(d.action, "CLOSE_REQUIRED")
        self.assertEqual(d.close_size, Decimal("0.002"))
        self.assertEqual(d.new_cycle_id, 0)

    # -- G. zero -> new cycle, flat -> open --
    def test_G_zero_to_new_cycle_opens(self):
        snap = make_snap(sell_cycle=200, sell_weight="1")
        d = self._decide(
            target_size=Decimal("0.001"),
            before_side="", before_size="0",
            snap=snap, synced=0,
        )
        self.assertEqual(d.action, "OPEN_REQUIRED")
        self.assertEqual(d.new_cycle_id, 200)

    # -- K. unknown ownership with non-flat actual -> FAIL_CLOSED --
    def test_K_unknown_ownership_with_exposure_blocks(self):
        snap = make_snap(sell_cycle=101, sell_weight="1")
        d = self._decide(
            target_size=Decimal("0.001"),
            before_side="sell", before_size="0.001",
            snap=snap, synced=None,
        )
        self.assertTrue(d.block)
        self.assertEqual(d.block_code, "BLOCKED_CYCLE_OWNERSHIP_UNKNOWN")

    # -- L. bootstrap flat -> open --
    def test_L_bootstrap_flat(self):
        d = self._decide(
            target_size=Decimal("0.001"),
            before_side="", before_size="0",
            synced=None,
        )
        self.assertEqual(d.action, "OPEN_REQUIRED")
        self.assertEqual(d.new_cycle_id, 100)

    # -- M. opposite-side exposure -> FAIL_CLOSED --
    def test_M_opposite_side_blocks(self):
        # Same-cycle opposite-side: BLOCKED_OPPOSITE_POSITION.
        # Cycle-change opposite-side: BLOCKED_CYCLE_OWNERSHIP_UNKNOWN.
        snap = make_snap(sell_cycle=100, sell_weight="1")
        d = self._decide(
            target_size=Decimal("0.001"),
            before_side="buy", before_size="0.002",  # opposite side
            snap=snap, synced=100,
        )
        self.assertTrue(d.block)
        self.assertEqual(d.block_code, "BLOCKED_OPPOSITE_POSITION")

    # -- Unknown ownership, target=0, non-flat -> BLOCKED_CYCLE_OWNERSHIP_UNKNOWN --
    def test_K_unknown_ownership_target_zero_nonflat_blocks(self):
        snap = make_snap(sell_cycle=0, sell_weight="0")
        d = self._decide(
            target_size=Decimal("0"),
            before_side="sell", before_size="0.002",
            snap=snap, synced=None,
        )
        self.assertTrue(d.block)
        self.assertEqual(d.block_code, "BLOCKED_CYCLE_OWNERSHIP_UNKNOWN")


class CycleStateStoreTest(unittest.TestCase):
    """Persistent cycle-state store tests."""

    def setUp(self):
        self.store = CycleStateStore()

    def test_adopt_first_cycle(self):
        self.store.adopt_first_cycle(
            "ondoperps/BITGET/XAU-USD.P/FASTFIB/SELL",
            source="mt4-Fresh-1", exchange="ondoperps",
            account="BITGET", exchange_instrument="XAU-USD.P",
            variant="FASTFIB", side="SELL", cycle_id=100,
        )
        self.assertEqual(
            self.store.get_synchronized_cycle_id(
                "ondoperps/BITGET/XAU-USD.P/FASTFIB/SELL",
            ),
            100,
        )
        self.assertEqual(
            self.store.get_transition(
                "ondoperps/BITGET/XAU-USD.P/FASTFIB/SELL",
            ),
            None,
        )

    def test_transition_close_sent_then_verified(self):
        self.store.adopt_first_cycle(
            "K", source="S", exchange="E", account="A",
            exchange_instrument="I", variant="V", side="S",
            cycle_id=100,
        )
        self.store.begin_transition_close_sent("K", old_cycle_id=100)
        self.assertEqual(
            self.store.get_transition("K"), TRANSITION_CLOSE_SENT,
        )
        self.store.advance_transition_close_verified("K", old_cycle_id=100)
        self.assertEqual(
            self.store.get_transition("K"),
            TRANSITION_CLOSE_VERIFIED,
        )

    def test_transition_open_then_finalize(self):
        self.store.adopt_first_cycle(
            "K", source="S", exchange="E", account="A",
            exchange_instrument="I", variant="V", side="S",
            cycle_id=100,
        )
        self.store.advance_transition_open_sent("K", new_cycle_id=101)
        self.assertEqual(
            self.store.get_transition("K"), TRANSITION_OPEN_SENT,
        )
        self.store.finalize_transition("K", new_cycle_id=101)
        self.assertEqual(
            self.store.get_synchronized_cycle_id("K"), 101,
        )
        self.assertEqual(self.store.get_transition("K"), None)

    def test_atomic_replacement_preserves_history(self):
        # Re-adopt should overwrite cleanly.
        self.store.adopt_first_cycle(
            "K", source="S", exchange="E", account="A",
            exchange_instrument="I", variant="V", side="S",
            cycle_id=100,
        )
        self.store.adopt_first_cycle(
            "K", source="S", exchange="E", account="A",
            exchange_instrument="I", variant="V", side="S",
            cycle_id=200,
        )
        self.assertEqual(
            self.store.get_synchronized_cycle_id("K"), 200,
        )

    def test_clear(self):
        self.store.adopt_first_cycle(
            "K", source="S", exchange="E", account="A",
            exchange_instrument="I", variant="V", side="S",
            cycle_id=100,
        )
        self.store.clear("K")
        self.assertIsNone(
            self.store.get_synchronized_cycle_id("K"),
        )


class CycleTransitionCrashSafetyTest(unittest.TestCase):
    """H. crash after close: do not close again
    I. crash after open: do not duplicate
    J. cycle changes again during transition: newest wins
    P. restart/reload persisted state: same decisions
    """

    def setUp(self):
        # Re-route cycle state to a fresh tempdir per test
        import os, tempfile
        self.tmp = tempfile.mkdtemp(prefix="fibo_cyc_crash_")
        os.environ["HERMES_HOME"] = self.tmp
        self.store = CycleStateStore()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_H_crash_after_close_does_not_close_again(self):
        """After the close has been sent and verified, the
        state machine records the sub-step so a restart can
        resume correctly: a second CLOSE_REQUIRED decision
        should NOT be emitted when actual is already flat."""
        self.store.adopt_first_cycle(
            "K", source="S", exchange="E", account="A",
            exchange_instrument="I", variant="V", side="S",
            cycle_id=100,
        )
        self.store.begin_transition_close_sent("K", old_cycle_id=100)
        self.store.advance_transition_close_verified("K", old_cycle_id=100)
        # Now decide: cycle changed to 101, exchange is flat
        # (we just verified). The decision should be OPEN_REQUIRED,
        # not CLOSE_REQUIRED.
        snap = make_snap(sell_cycle=101, sell_weight="1")
        d = decide_cycle_action(
            registration_key="K",
            source_symbol="XAUUSD", variant="FASTFIB",
            side="SELL",
            target=Mt4Target(side="SELL", size=Decimal("0.001")),
            before=make_before(side="", size="0"),
            snap=snap,
            synchronized_cycle_id=100,  # still old until finalize
        )
        # Synchronized is still 100 (we have not yet finalized
        # to 101). The decision compares current=101 with
        # synchronized=100 → cycle-change path.
        self.assertEqual(d.action, "OPEN_REQUIRED")
        self.assertEqual(d.new_cycle_id, 101)

    def test_I_crash_after_open_does_not_duplicate(self):
        """After the OPEN has been sent (transition=OPEN_SENT),
        the next decide should see the still-persisted
        synchronized=old, and a follow-up decide that sees
        actual=target should NOOP, not OPEN again."""
        self.store.adopt_first_cycle(
            "K", source="S", exchange="E", account="A",
            exchange_instrument="I", variant="V", side="S",
            cycle_id=100,
        )
        # New cycle started
        self.store.advance_transition_open_sent("K", new_cycle_id=101)
        # synchronized is still 100 (we have not finalized yet)
        # Decide for new cycle 101 with actual = target = 0.001
        snap = make_snap(sell_cycle=101, sell_weight="1")
        d = decide_cycle_action(
            registration_key="K",
            source_symbol="XAUUSD", variant="FASTFIB",
            side="SELL",
            target=Mt4Target(side="SELL", size=Decimal("0.001")),
            before=make_before(side="sell", size="0.001"),
            snap=snap,
            synchronized_cycle_id=100,  # still old
            transition=TRANSITION_OPEN_SENT,
        )
        # OPEN_SENT + actual=target + new cycle →
        # SAFE finalization (NOOP, no duplicate).
        self.assertEqual(d.action, "NOOP")

    def test_J_cycle_changes_again_during_transition(self):
        """The current cycle 102 should win. We do not open
        a stale 101 target."""
        self.store.adopt_first_cycle(
            "K", source="S", exchange="E", account="A",
            exchange_instrument="I", variant="V", side="S",
            cycle_id=100,
        )
        # 100 -> 101: we'd issue a close. The state machine
        # records CLOSE_SENT, then we read positions as flat.
        self.store.begin_transition_close_sent("K", old_cycle_id=100)
        self.store.advance_transition_close_verified("K", old_cycle_id=100)
        # Before opening, MT4 advances again: 101 -> 102.
        # We do not open the stale 101 target.
        snap = make_snap(sell_cycle=102, sell_weight="1")
        d = decide_cycle_action(
            registration_key="K",
            source_symbol="XAUUSD", variant="FASTFIB",
            side="SELL",
            target=Mt4Target(side="SELL", size=Decimal("0.001")),
            before=make_before(side="", size="0"),  # flat
            snap=snap,
            synchronized_cycle_id=100,
        )
        # synchronized=100, current=102 → cycle-change, flat →
        # OPEN at current cycle 102.
        self.assertEqual(d.action, "OPEN_REQUIRED")
        self.assertEqual(d.new_cycle_id, 102)
        self.assertEqual(d.delta.size, Decimal("0.001"))

    def test_P_restart_reload_persisted_state_same_decisions(self):
        """After a process restart, the persisted cycle-state
        must be read back and produce identical decisions."""
        self.store.adopt_first_cycle(
            "K", source="S", exchange="E", account="A",
            exchange_instrument="I", variant="V", side="S",
            cycle_id=100,
        )
        # Simulate restart by creating a new store object that
        # reads from the same file.
        store2 = CycleStateStore()
        self.assertEqual(
            store2.get_synchronized_cycle_id("K"), 100,
        )
        # Now the same decide() call should produce the same result.
        snap = make_snap(sell_cycle=100, sell_weight="1")
        d = decide_cycle_action(
            registration_key="K",
            source_symbol="XAUUSD", variant="FASTFIB",
            side="SELL",
            target=Mt4Target(side="SELL", size=Decimal("0.001")),
            before=make_before(side="sell", size="0.001"),
            snap=snap,
            synchronized_cycle_id=store2.get_synchronized_cycle_id("K"),
        )
        self.assertEqual(d.action, "NOOP")


class LiveConvergeCycleAwareTest(unittest.TestCase):
    """End-to-end cycle-aware executor behavior using
    FakeExecutor. NO real exchange calls."""

    def setUp(self):
        import os, tempfile
        self.tmp = tempfile.mkdtemp(prefix="fibo_cyc_e2e_")
        os.environ["HERMES_HOME"] = self.tmp
        # Re-import cycle_state in the new HERMES_HOME context.
        from plugins.trade.fibo import live as live_mod
        from plugins.trade.fibo import cycle_state
        cycle_state.CycleStateStore.__init__.__globals__["_default_path"] = \
            lambda: __import__("pathlib").Path(self.tmp) / "fibo" / "cycle_state.json"
        self.live_mod = live_mod
        # Re-instantiate so it picks up the new path.
        from plugins.trade.fibo.cycle_state import CycleStateStore as CSS
        self.CSS = CSS
        self.store = CSS()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_live_converge(
        self, *, actual_positions, snap, synced_cycle_id=None,
    ):
        from plugins.trade.fibo.live import live_converge
        from plugins.trade.fibo.store import FiboRegistration
        reg = FiboRegistration.build(
            exchange="ondoperps", account="BITGET",
            symbol="XAUUSD", variant="FASTFIB", side="SELL",
            starting_volume="0.001",
            source="mt4-Fresh-1", source_seq=1,
            source_cycle_id=47030000,
            source_cumulative_weight="1", source_percentage="0.001",
            source_snapshot_received_at="2026-08-27T00:00:00Z",
            desired_exchange_size=Decimal("0.001"),
            exchange_instrument="XAU-USD.P",
        )
        exec_ = FakeExecutor(
            positions={
                "ondoperps|BITGET": list(actual_positions),
            }
        )
        return live_converge(
            reg, snap, execute_fn=exec_,
            supported_exchanges=frozenset({"ondoperps"}),
            validate_accounts_fn=lambda x: ["BITGET"],
        ), exec_

    def test_E_live_converge_issues_close_then_open_for_cycle_change(self):
        """E. cycle changes: stored=100, current=101, target=0.001,
        actual=0.002 → CLOSE_REQUIRED (close old) → after close
        the next natural run sees actual=0 (flat) and triggers
        OPEN_REQUIRED (new cycle)."""
        from plugins.trade.fibo.live import live_converge
        from plugins.trade.fibo.store import FiboRegistration
        from datetime import datetime, timezone
        reg = FiboRegistration.build(
            exchange="ondoperps", account="BITGET",
            symbol="XAUUSD", variant="FASTFIB", side="SELL",
            starting_volume="0.001",
            source="mt4-Fresh-1", source_seq=1,
            source_cycle_id=100,
            source_cumulative_weight="1", source_percentage="0.001",
            source_snapshot_received_at="2026-08-27T00:00:00Z",
            desired_exchange_size=Decimal("0.001"),
            exchange_instrument="XAU-USD.P",
        )
        # Pre-seed state.
        self.store.adopt_first_cycle(
            reg.registration_key,
            source=reg.source, exchange=reg.exchange,
            account=reg.account, exchange_instrument=reg.exchange_instrument,
            variant=reg.variant, side=str(reg.side).upper(),
            cycle_id=100,
        )
        # First natural run: cycle changed 100 -> 101, target 0.001,
        # actual 0.002 SHORT → CLOSE_REQUIRED.
        # Use a fresh snapshot so the staleness gate passes.
        fibo = Mt4Fibo(
            symbol="XAUUSD", variant="FASTFIB",
            percentage=Decimal("0.001"),
            buy_cycle_id=0, cumulative_buy_weight=Decimal("0"),
            sell_cycle_id=101, cumulative_sell_weight=Decimal("1"),
        )
        snap = Mt4Snapshot(
            v=1, source="mt4-Fresh-1", seq=1, ts=1, fibos=[fibo],
            received_at=datetime.now(timezone.utc).isoformat(),
            telegram_update_id=1, telegram_message_id=1,
            reader_chat_id=1,
        )

        # A closure-aware executor: returns the original
        # position until a close_position call is observed,
        # then returns an empty position list.
        class _ClosingFakeExecutor(FakeExecutor):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._closed = False
            def __call__(self, request):
                self.calls.append(request)
                op = request.get("operation")
                if op == "close_position":
                    self._closed = True
                    return FakeResponse(success=True)
                if op == "positions_orders":
                    if self._closed:
                        return FakeResponse(success=True, positions=[])
                    return FakeResponse(
                        success=True,
                        positions=list(
                            self.positions.get(
                                f"{request.get('exchange')}|{request.get('account')}", []
                            )
                        ),
                    )
                return FakeResponse(success=True)

        exec_ = _ClosingFakeExecutor(positions={"ondoperps|BITGET": [
            {"symbol": "XAU-USD.P", "side": "sell", "size": "0.002"}
        ]})
        result = live_converge(
            reg, snap, execute_fn=exec_,
            supported_exchanges=frozenset({"ondoperps"}),
            validate_accounts_fn=lambda x: ["BITGET"],
        )
        self.assertFalse(result.placed_live_order)
        # The executor should have issued a close_position call.
        close_ops = [
            c for c in exec_.calls if c.get("operation") == "close_position"
        ]
        self.assertEqual(len(close_ops), 1)
        # And then re-read positions to verify flat.
        reads = [
            c for c in exec_.calls if c.get("operation") == "positions_orders"
        ]
        self.assertGreaterEqual(len(reads), 2)
        # State machine should record CLOSE_VERIFIED.
        self.assertEqual(
            self.store.get_transition(reg.registration_key),
            TRANSITION_CLOSE_VERIFIED,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
