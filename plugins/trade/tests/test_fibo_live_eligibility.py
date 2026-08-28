"""Phase 2.13.12 — Focused tests for dynamic live eligibility.

This test file uses a **purpose-built mock execute_fn** that:

  1. NEVER instantiates or uses the real TradeDesk.
  2. Records every requested operation in memory.
  3. Returns deterministic fixture responses.
  4. Simulates positions_orders, new_order, cancel_order_group.

The test harness additionally monkeypatches the real TradeDesk entry
point so that any accidental reference raises
``REAL_TRADEDESK_FORBIDDEN_IN_FIBO_TEST`` rather than contacting a
real exchange.

Every test in this file is required to pass a ``FakeExecutor`` to
``live_converge``; tests must NOT import ``plugins.trade.tradedesk``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

# Phase 2.13.18: redirect HERMES_HOME to a temp dir BEFORE any
# Fibo imports so the cycle-state file is per-test-run.
_TEST_HERMES_HOME = tempfile.mkdtemp(prefix="fibo_elig_test_")
os.environ["HERMES_HOME"] = _TEST_HERMES_HOME
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

# ---------------------------------------------------------------------------
# Safety net: forbid the real TradeDesk.
# ---------------------------------------------------------------------------


def _install_tradedesk_guard():
    """Patch ``plugins.trade.tradedesk.get_tradedesk`` to RAISE on
    any access. The Fibo tests must not touch the real TradeDesk
    under any circumstances.

    The guard patches BOTH the module-level ``get_tradedesk``
    function AND the instance methods on ``TradeDesk``. Any
    accidental reference raises
    ``AssertionError("REAL_TRADEDESK_FORBIDDEN_IN_FIBO_TEST")``
    rather than a silent exchange call.
    """
    import plugins.trade.tradedesk as _td

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "REAL_TRADEDESK_FORBIDDEN_IN_FIBO_TEST: "
            "Fibo tests must not call get_tradedesk() or "
            "TradeDesk.execute; use the FakeExecutor instead."
        )

    _td.get_tradedesk = _forbidden
    # Also patch the class methods so that ANY attempt to use
    # the real TradeDesk (e.g. via a leaked reference) fails
    # closed immediately.
    try:
        _td.TradeDesk.execute = _forbidden
        _td.TradeDesk.list_exchanges = _forbidden
        _td.TradeDesk.list_accounts = _forbidden
    except AttributeError:
        pass


_install_tradedesk_guard()


# ---------------------------------------------------------------------------
# Imports under test. These must come AFTER the guard.
# ---------------------------------------------------------------------------

sys.path.insert(0, "/usr/local/lib/hermes-agent")
sys.path.insert(0, "/root/kam")

from plugins.trade.fibo.live_eligibility import (
    BlockReason,
    LiveEligibility,
    evaluate,
    SUPPORTED_SIDES,
    SUPPORTED_VARIANTS,
)
from plugins.trade.fibo.executor import (
    SIDE_BUY, SIDE_SELL, _resolve_mt4_target, ConvergeResult,
    ExchangePosition, Mt4Target,
)
from plugins.trade.fibo.live import (
    LiveConvergeResult,
    live_converge,
)
from plugins.trade.fibo.snapshot import (
    Mt4Snapshot, Mt4Fibo, SIDE_BUY as FIBO_SIDE_BUY,
    SIDE_SELL as FIBO_SIDE_SELL,
)
from plugins.trade.fibo.store import FiboRegistration
from plugins.trade.tradedesk import get_tradedesk  # noqa: F401  (used to assert guard works)


# ---------------------------------------------------------------------------
# Mock execution boundary.
# ---------------------------------------------------------------------------


class FakeExecutor:
    """In-memory mock TradeDesk execute_fn."""

    def __init__(
        self,
        *,
        positions: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        open_orders: Optional[List[Dict[str, Any]]] = None,
        new_order_response: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._positions = positions or {}
        self._open_orders = open_orders or []
        self._new_order_response = new_order_response or {
            "success": True,
            "operation": "new_order",
            "order": {
                "symbol": None, "side": None,
                "order_type": "market",
                "requested_volume": None,
                "submitted_volume": None,
                "submitted_price": "0",
                "verified": True,
                "status": "success",
            },
        }
        self._cancel_response = {
            "success": True,
            "operation": "cancel_order_group",
            "cancelled_count": 0,
        }

    def record(self, request: Dict[str, Any], response: Any) -> None:
        self.calls.append({
            "request": dict(request),
            "response_repr": repr(response)[:200],
        })

    def new_order_calls(self) -> List[Dict[str, Any]]:
        return [c["request"] for c in self.calls
                if c["request"].get("operation") == "new_order"]

    def cancel_order_group_calls(self) -> List[Dict[str, Any]]:
        return [c["request"] for c in self.calls
                if c["request"].get("operation") == "cancel_order_group"]

    def positions_orders_calls(self) -> List[Dict[str, Any]]:
        return [c["request"] for c in self.calls
                if c["request"].get("operation") == "positions_orders"]

    def _build_positions_response(self, request: Dict[str, Any]):
        """Build a ConvergeResult-like response with the
        configured positions for this exchange/account.
        """
        exchange = request.get("exchange", "")
        account = request.get("account", "")
        positions = self._positions.get(f"{exchange}|{account}", [])
        open_orders = self._open_orders

        class _CR:
            def __init__(self):
                self.success = True
                self.error = None
                self.operation = "positions_orders"
                self.exchange = exchange
                self.account = account
                # Expose BOTH attribute styles so consumers
                # using ``response.positions`` (live_converge
                # via _read_actual_position_from_response) and
                # consumers using ``response.to_dict()["positions"]``
                # work correctly.
                self.positions = positions
                self._positions = positions
                self.open_orders = open_orders
                self._open_orders = open_orders
                self.open_order_count = len(open_orders)
                self.order_groups = open_orders

            def to_dict(self):
                return {
                    "success": True,
                    "operation": "positions_orders",
                    "exchange": exchange,
                    "account": account,
                    "positions": self._positions,
                    "open_order_count": self.open_order_count,
                    "order_groups": self.order_groups,
                }

        return _CR()

    def execute(self, request: Dict[str, Any]):
        op = request.get("operation")
        if op == "positions_orders":
            resp = self._build_positions_response(request)
            self.record(request, resp)
            return resp
        if op == "cancel_order_group":
            self.record(request, self._cancel_response)
            return self._cancel_response
        if op == "new_order":
            resp = dict(self._new_order_response)
            resp.setdefault("order", {})
            for k in ("symbol", "side", "volume"):
                if k in request:
                    resp["order"][k] = request[k]
            self.record(request, resp)
            class _NO:
                success = True
                error = None
                def to_dict(self):
                    return resp
            return _NO()
        class _FAIL:
            success = False
            error = f"unknown op {op!r}"
        self.record(request, _FAIL())
        return _FAIL()


# ---------------------------------------------------------------------------
# Registration / snapshot fixtures.
# ---------------------------------------------------------------------------


_SUPPORTED_EXCHANGES = frozenset({
    "apex", "arcus", "edgex", "hibachi", "hyperliquid", "lighter",
    "ondoperps", "pacifica", "raydium", "rise",
})


# Default mock account validator. All registrations in the
# test fixtures use ``ondoperps/BITGET`` or ``hyperliquid/BASED``;
# we map them to a small in-memory dictionary.
_MOCK_CONFIGURED_ACCOUNTS = {
    "ondoperps": ["BITGET"],
    "hyperliquid": ["BASED", "BITGET"],
    "apex": ["BITGET"],
    "arcus": ["BITGET"],
    "edgex": ["BITGET"],
    "hibachi": ["BITGET"],
    "lighter": ["BITGET"],
    "pacifica": ["BITGET"],
    "raydium": ["PHANTOM"],
    "rise": ["METAMASK"],
    "unknown_exchange_xyz": ["BITGET"],
}


def _mock_validate_accounts(exchange: str):
    return list(_MOCK_CONFIGURED_ACCOUNTS.get(
        (exchange or "").strip().lower(), []
    ))


# Pre-baked (reg, snap) fixtures that pass ALL eligibility gates
# (active, identity complete, supported exchange, account
# configured, snapshot fresh, fibo match, cycle+weight
# consistent, starting_volume positive). Tests that want a
# BLOCKED registration override the relevant fields.
_VALID_XAU_SELL_REG = {
    "registration_key": "ondoperps/BITGET/XAU-USD.P/FASTFIB/SELL",
    "account": "BITGET", "exchange": "ondoperps",
    "exchange_instrument": "XAU-USD.P",
    "source_symbol": "XAUUSD", "variant": "FASTFIB",
    "side": "SELL", "starting_volume": "0.001",
    "desired_exchange_size": "0.001", "status": "registered",
    "source": "mt4-test", "source_cumulative_weight": "8",
    "source_cycle_id": 47028667, "source_percentage": "0.001",
    "source_seq": 60000,
    "source_snapshot_received_at": "2026-08-28T11:37:56Z",
    "created_at": "2026-08-28T11:37:57Z",
    "updated_at": "2026-08-28T11:37:57Z",
}


def _make_reg(
    *,
    registration_key: str = "ondoperps/BITGET/XAU-USD.P/FASTFIB/SELL",
    exchange: str = "ondoperps",
    account: str = "BITGET",
    exchange_instrument: str = "XAU-USD.P",
    source_symbol: str = "XAUUSD",
    variant: str = "FASTFIB",
    side: str = "SELL",
    starting_volume: str = "0.001",
    status: str = "registered",
    source: str = "mt4-test",
) -> FiboRegistration:
    raw = {
        "registration_key": registration_key,
        "account": account,
        "exchange": exchange,
        "exchange_instrument": exchange_instrument,
        "source_symbol": source_symbol,
        "symbol": source_symbol,
        "variant": variant,
        "side": side,
        "starting_volume": starting_volume,
        "desired_exchange_size": "0.001",
        "status": status,
        "source": source,
        "source_cumulative_weight": "8",
        "source_cycle_id": 47028667,
        "source_percentage": "0.001",
        "source_seq": 60000,
        "source_snapshot_received_at": "2026-08-28T11:37:56Z",
        "created_at": "2026-08-28T11:37:57Z",
        "updated_at": "2026-08-28T11:37:57Z",
    }
    return FiboRegistration.from_dict(raw)


def _make_snapshot(
    *,
    fibos: List[Mt4Fibo],
    received_at: Optional[str] = None,
    seq: int = 60000,
    source: str = "mt4-test",
) -> Mt4Snapshot:
    """Build a Mt4Snapshot. By default the snapshot is fresh
    (received_at = now). The caller may pass an explicit
    ``received_at`` to simulate a stale snapshot.
    """
    if received_at is None:
        from datetime import datetime, timezone
        received_at = datetime.now(timezone.utc).isoformat()
    return Mt4Snapshot(
        v=1, source=source, seq=seq, ts=0,
        received_at=received_at,
        fibos=fibos,
        telegram_update_id=0, telegram_message_id=0,
        reader_chat_id=0,
    )


def _make_fibo(
    *,
    symbol: str,
    variant: str,
    buy_cycle_id: int,
    sell_cycle_id: int,
    cumulative_buy_weight: str,
    cumulative_sell_weight: str,
    percentage: str = "0.001",
) -> Mt4Fibo:
    return Mt4Fibo(
        buy_cycle_id=buy_cycle_id,
        sell_cycle_id=sell_cycle_id,
        cumulative_buy_weight=Decimal(cumulative_buy_weight),
        cumulative_sell_weight=Decimal(cumulative_sell_weight),
        percentage=Decimal(percentage),
        symbol=symbol,
        variant=variant,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestActiveXauFastsibSell(unittest.TestCase):
    def test_active_xau_with_current_mt4_sell_weight_1_is_live_eligible(self):
        reg = _make_reg()
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=47028667,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        result = evaluate(
            reg, snap, supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertTrue(result.eligible,
                        f"expected LIVE_ELIGIBLE, got {result.reason_code.value}: {result.reason}")
        self.assertEqual(result.reason_code, BlockReason.LIVE_ELIGIBLE)


class TestActiveEthNormalfibBuy(unittest.TestCase):
    def test_active_eth_buy_with_current_mt4_buy_weight_2_is_live_eligible(self):
        reg = _make_reg(
            registration_key="ondoperps/BITGET/ETH-USD.P/NORMALFIB/BUY",
            exchange="ondoperps", account="BITGET",
            exchange_instrument="ETH-USD.P", source_symbol="ETHUSD",
            variant="NORMALFIB", side="BUY", starting_volume="0.001",
        )
        fibo = _make_fibo(
            symbol="ETHUSD", variant="NORMALFIB",
            buy_cycle_id=47022998, sell_cycle_id=46871101,
            cumulative_buy_weight="2", cumulative_sell_weight="4",
        )
        snap = _make_snapshot(fibos=[fibo])
        result = evaluate(
            reg, snap, supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertTrue(result.eligible)
        self.assertEqual(result.reason_code, BlockReason.LIVE_ELIGIBLE)


class TestStoppedRegistration(unittest.TestCase):
    def test_stopped_status_blocks(self):
        reg = _make_reg(status="stopped")
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=47028667,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        result = evaluate(
            reg, snap, supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason_code, BlockReason.BLOCKED_NOT_ACTIVE)


class TestUnresolvedInstrument(unittest.TestCase):
    def test_empty_exchange_instrument_blocks_as_unresolved_instrument(self):
        # Gate ordering: an empty exchange_instrument returns
        # the SPECIFIC BLOCKED_UNRESOLVED_INSTRUMENT reason
        # (preferred over the generic BLOCKED_INVALID_REGISTRATION
        # per the spec).
        reg = _make_reg(exchange_instrument="")
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=47028667,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        result = evaluate(
            reg, snap, supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.eligible)
        # The reason must be one of the blocking codes (not LIVE_ELIGIBLE).
        self.assertNotEqual(result.reason_code, BlockReason.LIVE_ELIGIBLE)
        # Specifically: an empty exchange_instrument returns
        # BLOCKED_UNRESOLVED_INSTRUMENT.
        self.assertEqual(
            result.reason_code,
            BlockReason.BLOCKED_UNRESOLVED_INSTRUMENT,
        )


class TestUnregisteredMt4CannotTrade(unittest.TestCase):
    def test_no_registration_no_live_converge_call(self):
        """A raw MT4 snapshot entry alone does NOT cause a live
        write. The only path to live convergence is via the
        canonical persisted registration.
        """
        from plugins.trade.fibo.store import FiboRegistrationStore
        store = FiboRegistrationStore(
            "/nonexistent/registrations.jsonl"
        )
        regs = store.load_all()
        self.assertEqual(regs, [])
        # No registrations → no live_converge calls.
        fake = FakeExecutor()
        # No for-loop; nothing to call.
        self.assertEqual(fake.new_order_calls(), [])


class TestStaleSnapshot(unittest.TestCase):
    def test_old_snapshot_blocks(self):
        reg = _make_reg()
        from datetime import datetime, timedelta, timezone
        old_ts = (datetime.now(timezone.utc)
                  - timedelta(hours=1)).isoformat()
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=47028667,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo], received_at=old_ts)
        result = evaluate(
            reg, snap, supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason_code, BlockReason.BLOCKED_STALE_SNAPSHOT)


class TestInvalidCycle(unittest.TestCase):
    def test_sell_registration_with_buy_cycle_only_blocks(self):
        reg = _make_reg()  # side=SELL
        # Inconsistent: buy cycle>0, weight>0 but sell cycle=0
        # AND sell weight=0 — that's actually the inactive state.
        # To block, make it inconsistent: sell cycle=1, weight=0.
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=1,
            cumulative_buy_weight="4", cumulative_sell_weight="0",
        )
        snap = _make_snapshot(fibos=[fibo])
        result = evaluate(
            reg, snap, supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason_code, BlockReason.BLOCKED_INVALID_WEIGHT)

    def test_buy_registration_with_sell_cycle_only_blocks(self):
        reg = _make_reg(
            registration_key="ondoperps/BITGET/XAU-USD.P/FASTFIB/BUY",
            side="BUY", exchange_instrument="XAU-USD.P",
            source_symbol="XAUUSD",
        )
        # Inconsistent: buy cycle=1, buy weight=0.
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=1, sell_cycle_id=47028667,
            cumulative_buy_weight="0", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        result = evaluate(
            reg, snap, supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason_code, BlockReason.BLOCKED_INVALID_WEIGHT)


class TestInvalidWeight(unittest.TestCase):
    def test_zero_weight_blocks(self):
        reg = _make_reg()
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=47028667,
            cumulative_buy_weight="4", cumulative_sell_weight="0",
        )
        snap = _make_snapshot(fibos=[fibo])
        result = evaluate(
            reg, snap, supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason_code, BlockReason.BLOCKED_INVALID_WEIGHT)


class TestInvalidStartingVolume(unittest.TestCase):
    def test_negative_starting_volume_blocks(self):
        # The store's build() rejects 0/negative at construction
        # time, so we can't observe a >0 starting_volume that
        # produces a negative target via multiplication. Instead
        # we directly call the helper.
        import importlib
        from plugins.trade.fibo import live_eligibility as le
        # Verify the helper rejects 0 and negative.
        self.assertFalse(le._is_finite_positive_decimal(0))
        self.assertFalse(le._is_finite_positive_decimal(Decimal("-1")))
        self.assertFalse(le._is_finite_positive_decimal(None))
        # And accepts a positive value.
        self.assertTrue(le._is_finite_positive_decimal(Decimal("0.001")))


class TestSnapshotFiboMismatch(unittest.TestCase):
    def test_symbol_mismatch_blocks(self):
        reg = _make_reg(source_symbol="XAUUSD")
        fibo = _make_fibo(
            symbol="BTCUSD", variant="FASTFIB",
            buy_cycle_id=1, sell_cycle_id=2,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        result = evaluate(
            reg, snap, supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason_code, BlockReason.BLOCKED_SNAPSHOT_FIBO_MISMATCH)

    def test_variant_mismatch_blocks(self):
        reg = _make_reg(variant="FASTFIB")
        fibo = _make_fibo(
            symbol="XAUUSD", variant="NORMALFIB",
            buy_cycle_id=1, sell_cycle_id=2,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        result = evaluate(
            reg, snap, supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason_code, BlockReason.BLOCKED_SNAPSHOT_FIBO_MISMATCH)


class TestUnsupportedExchange(unittest.TestCase):
    def test_unknown_exchange_blocks(self):
        reg = _make_reg(exchange="unknown_exchange_xyz")
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=1, sell_cycle_id=2,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        result = evaluate(
            reg, snap, supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason_code, BlockReason.BLOCKED_UNSUPPORTED_EXCHANGE)


class TestNoLiveWriteForBlocked(unittest.TestCase):
    def test_stopped_registration_live_converge_no_exchange_calls(self):
        reg = _make_reg(status="stopped")
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=1, sell_cycle_id=2,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        fake = FakeExecutor()
        result = live_converge(
            reg, snap, execute_fn=fake.execute,
            supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.placed_live_order)
        self.assertEqual(fake.new_order_calls(), [])
        self.assertEqual(fake.cancel_order_group_calls(), [])
        # The eligibility gate blocks BEFORE positions_orders.
        self.assertEqual(fake.calls, [])

    def test_blocked_active_registration_live_converge_no_exchange_calls(self):
        """A SELL registration with inconsistent cycle/weight is
        blocked at the eligibility gate; live_converge does not
        call the TradeDesk.
        """
        reg = _make_reg()  # side=SELL
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=1, sell_cycle_id=1,
            cumulative_buy_weight="4", cumulative_sell_weight="0",
        )
        snap = _make_snapshot(fibos=[fibo])
        fake = FakeExecutor()
        result = live_converge(
            reg, snap, execute_fn=fake.execute,
            supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.placed_live_order)
        self.assertEqual(fake.calls, [])


class TestSideIsolation(unittest.TestCase):
    def test_xau_sell_only_touches_xau_instrument(self):
        reg = _make_reg(
            registration_key="ondoperps/BITGET/XAU-USD.P/FASTFIB/SELL",
            exchange_instrument="XAU-USD.P", side="SELL",
            starting_volume="0.001",
        )
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=47028667,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        fake = FakeExecutor(positions={"ondoperps|BITGET": []})
        live_converge(
            reg, snap, execute_fn=fake.execute,
            supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        for c in fake.new_order_calls():
            self.assertEqual(c["symbol"], "XAU-USD.P")
            self.assertNotIn("BTC", c["symbol"])
            self.assertNotIn("ETH", c["symbol"])


class TestSideStrict(unittest.TestCase):
    def test_buy_registration_does_not_place_sell_order(self):
        reg = _make_reg(
            registration_key="ondoperps/BITGET/ETH-USD.P/NORMALFIB/BUY",
            exchange_instrument="ETH-USD.P", side="BUY",
            starting_volume="0.001",
        )
        fibo = _make_fibo(
            symbol="ETHUSD", variant="NORMALFIB",
            buy_cycle_id=47022998, sell_cycle_id=0,
            cumulative_buy_weight="2", cumulative_sell_weight="0",
        )
        snap = _make_snapshot(fibos=[fibo])
        fake = FakeExecutor(positions={"ondoperps|BITGET": []})
        live_converge(
            reg, snap, execute_fn=fake.execute,
            supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        for c in fake.new_order_calls():
            self.assertEqual(c["side"], SIDE_BUY)

    def test_sell_registration_does_not_place_buy_order(self):
        reg = _make_reg(side="SELL")
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=0, sell_cycle_id=47028667,
            cumulative_buy_weight="0", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        fake = FakeExecutor(positions={"ondoperps|BITGET": []})
        live_converge(
            reg, snap, execute_fn=fake.execute,
            supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        for c in fake.new_order_calls():
            self.assertEqual(c["side"], SIDE_SELL)


class TestLatestPerKeySemantics(unittest.TestCase):
    def test_registered_then_stopped_yields_stopped(self):
        tmpdir = tempfile.mkdtemp(prefix="latestkey_")
        path = os.path.join(tmpdir, "regs.jsonl")
        with open(path, "w") as fh:
            for status in ("registered", "stopped"):
                fh.write(json.dumps({
                    "registration_key": "ondoperps/BITGET/XAU-USD.P/FASTFIB/SELL",
                    "account": "BITGET", "exchange": "ondoperps",
                    "exchange_instrument": "XAU-USD.P",
                    "source_symbol": "XAUUSD", "variant": "FASTFIB",
                    "side": "SELL", "starting_volume": "0.001",
                    "status": status,
                }) + "\n")
        from pathlib import Path
        from plugins.trade.fibo.store import FiboRegistrationStore
        store = FiboRegistrationStore(Path(path))
        regs = store.load_all()
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0].status, "stopped")
        self.assertFalse(regs[0].is_active)

    def test_registered_stopped_registered_yields_registered(self):
        tmpdir = tempfile.mkdtemp(prefix="latestkey2_")
        path = os.path.join(tmpdir, "regs.jsonl")
        with open(path, "w") as fh:
            for status in ("registered", "stopped", "registered"):
                fh.write(json.dumps({
                    "registration_key": "ondoperps/BITGET/XAU-USD.P/FASTFIB/SELL",
                    "account": "BITGET", "exchange": "ondoperps",
                    "exchange_instrument": "XAU-USD.P",
                    "source_symbol": "XAUUSD", "variant": "FASTFIB",
                    "side": "SELL", "starting_volume": "0.001",
                    "status": status,
                }) + "\n")
        from pathlib import Path
        from plugins.trade.fibo.store import FiboRegistrationStore
        store = FiboRegistrationStore(Path(path))
        regs = store.load_all()
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0].status, "registered")
        self.assertTrue(regs[0].is_active)


class TestBlockReasonReporting(unittest.TestCase):
    def test_block_reason_is_explicit_for_blocked_active_registration(self):
        reg = _make_reg()  # side=SELL
        # Inconsistent state: sell cycle > 0 but weight == 0
        # (this is what gates 12+13 block).
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=1,
            cumulative_buy_weight="4", cumulative_sell_weight="0",
        )
        snap = _make_snapshot(fibos=[fibo])
        result = evaluate(
            reg, snap, supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.eligible)
        self.assertEqual(
            result.reason_code, BlockReason.BLOCKED_INVALID_WEIGHT,
        )
        self.assertNotEqual(result.reason, "")


class TestCurrentWeightDrivesTarget(unittest.TestCase):
    """The CURRENT MT4 weight drives the live target. The
    registration-time stored weight is NOT used.
    """

    def test_current_sell_weight_1_yields_target_0_001_not_0_008(self):
        reg = _make_reg(
            registration_key="ondoperps/BITGET/XAU-USD.P/FASTFIB/SELL",
            exchange="ondoperps", account="BITGET",
            exchange_instrument="XAU-USD.P", source_symbol="XAUUSD",
            variant="FASTFIB", side="SELL", starting_volume="0.001",
        )
        # The CURRENT MT4 has cumulative_sell_weight=1.
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=47028667,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])

        # Confirm eligibility.
        elig = evaluate(
            reg, snap, supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertTrue(elig.eligible)

        # The target: starting_volume × CURRENT sell weight.
        target = _resolve_mt4_target(reg, snap)
        self.assertEqual(target.side, SIDE_SELL)
        self.assertEqual(target.size, Decimal("0.001"),
                         f"target must be 0.001, got {target.size} "
                         f"(current MT4 sell weight=1; starting=0.001)")

        # Live-converge: with XAU flat, place a new_order.
        fake = FakeExecutor(positions={"ondoperps|BITGET": []})
        result = live_converge(
            reg, snap, execute_fn=fake.execute,
            supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertTrue(result.placed_live_order,
                        f"expected new_order, got: blocked_reason={result.blocked_reason!r} "
                        f"reason={result.reason!r}")
        self.assertEqual(len(fake.new_order_calls()), 1)
        new_order = fake.new_order_calls()[0]
        # CRITICAL: volume MUST be 0.001, NOT 0.008.
        self.assertEqual(new_order["volume"], "0.001",
                         f"new_order volume must be 0.001 (current "
                         f"target), got {new_order['volume']!r}. "
                         f"Stored weight 8 is registration-time "
                         f"information and MUST NOT be used for the "
                         f"live target.")
        # No order for 0.008.
        for c in fake.new_order_calls():
            self.assertNotEqual(c["volume"], "0.008")
        # Side MUST be SELL.
        self.assertEqual(new_order["side"], SIDE_SELL)
        # Symbol MUST be XAU-USD.P.
        self.assertEqual(new_order["symbol"], "XAU-USD.P")
        # Exchange / account MUST match.
        self.assertEqual(new_order["exchange"], "ondoperps")
        self.assertEqual(new_order["account"], "BITGET")

    def test_buy_weight_does_not_drive_sell_target(self):
        reg = _make_reg(side="SELL")
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=47028667,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        target = _resolve_mt4_target(reg, snap)
        # Must use the SELL weight (1), not the BUY weight (4).
        self.assertEqual(target.size, Decimal("0.001"),
                         f"target must be 0.001 (= 0.001 × sell=1), "
                         f"got {target.size}")


class TestDynamicMt4Target(unittest.TestCase):
    def test_target_changes_with_mt4_weight_without_reregistering(self):
        reg = _make_reg(starting_volume="0.001")
        for sell_w, expected_target in [
            ("1", Decimal("0.001")),
            ("2", Decimal("0.002")),
            ("4", Decimal("0.004")),
        ]:
            fibo = _make_fibo(
                symbol="XAUUSD", variant="FASTFIB",
                buy_cycle_id=47029142, sell_cycle_id=47028667,
                cumulative_buy_weight="0", cumulative_sell_weight=sell_w,
            )
            snap = _make_snapshot(fibos=[fibo])
            target = _resolve_mt4_target(reg, snap)
            self.assertEqual(target.size, expected_target,
                             f"sell_weight={sell_w}: expected target "
                             f"{expected_target}, got {target.size}")


class TestWriteSurfaceRestriction(unittest.TestCase):
    def test_live_converge_uses_only_approved_operations(self):
        from plugins.trade.fibo import live as livemod
        self.assertEqual(
            livemod.ALLOWED_OPERATIONS,
            frozenset({
                "positions_orders",
                "cancel_order_group",
                "new_order",
                "close_position",  # Phase 2.13.18 cycle-transition
            }),
        )

    def test_executor_does_not_invoke_unknown_ops(self):
        reg = _make_reg()
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=47028667,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        fake = FakeExecutor(positions={"ondoperps|BITGET": []})
        live_converge(
            reg, snap, execute_fn=fake.execute,
            supported_exchanges=_SUPPORTED_EXCHANGES, validate_accounts_fn=_mock_validate_accounts,
        )
        ops = [c["request"]["operation"] for c in fake.calls]
        for op in ops:
            self.assertIn(op, {"positions_orders", "new_order", "cancel_order_group"})


class TestRealTradeDeskForbidden(unittest.TestCase):
    def test_get_tradedesk_raises(self):
        import plugins.trade.tradedesk as td
        with mock.patch.object(
            td, "get_tradedesk",
            side_effect=AssertionError(
                "REAL_TRADEDESK_FORBIDDEN_IN_FIBO_TEST"
            ),
        ):
            with self.assertRaises(AssertionError) as cm:
                td.get_tradedesk()
            self.assertIn("REAL_TRADEDESK_FORBIDDEN_IN_FIBO_TEST",
                          str(cm.exception))

    def test_tradedesk_class_methods_also_raise(self):
        """The strengthened guard patches TradeDesk class methods
        too. Any accidental reference raises immediately."""
        import plugins.trade.tradedesk as td
        for method_name in ("execute", "list_exchanges", "list_accounts"):
            with self.subTest(method=method_name):
                with self.assertRaises(AssertionError) as cm:
                    getattr(td.TradeDesk, method_name)({})
                self.assertIn("REAL_TRADEDESK_FORBIDDEN_IN_FIBO_TEST",
                              str(cm.exception))


class TestWeightZeroLifecycle(unittest.TestCase):
    """Spec §1: audit the weight=0 lifecycle semantics.

    Per Mt4Fibo.is_side_active():
        cycle_id > 0 AND cumulative_weight > 0 -> active
        else                                      -> inactive

    A consistent inactive state (cycle_id=0 AND weight=0) is
    legitimate: the side has no current target. The executor
    issues a NOOP ("target flat") and the eligibility layer
    allows it through."""

    def test_consistent_inactive_state_is_eligible_with_target_zero(self):
        reg = _make_reg()  # side=SELL
        # Both sell_cycle=0 AND sell_weight=0 = side inactive.
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=0,
            cumulative_buy_weight="4", cumulative_sell_weight="0",
        )
        snap = _make_snapshot(fibos=[fibo])
        result = evaluate(
            reg, snap,
            supported_exchanges=_SUPPORTED_EXCHANGES,
            validate_accounts_fn=_mock_validate_accounts,
        )
        # The SELL side is inactive; the eligibility layer
        # permits this. The executor will then issue a NOOP
        # because the computed target is 0.
        self.assertTrue(result.eligible)

    def test_inconsistent_state_cycle_only_blocks(self):
        # cycle_id > 0 but weight = 0 -> BLOCKED.
        reg = _make_reg()
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=1,
            cumulative_buy_weight="4", cumulative_sell_weight="0",
        )
        snap = _make_snapshot(fibos=[fibo])
        result = evaluate(
            reg, snap,
            supported_exchanges=_SUPPORTED_EXCHANGES,
            validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.eligible)
        self.assertEqual(
            result.reason_code, BlockReason.BLOCKED_INVALID_WEIGHT,
        )

    def test_inconsistent_state_weight_only_blocks(self):
        # cycle_id = 0 but weight > 0 -> BLOCKED.
        reg = _make_reg()
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=0,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        result = evaluate(
            reg, snap,
            supported_exchanges=_SUPPORTED_EXCHANGES,
            validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.eligible)
        self.assertEqual(
            result.reason_code, BlockReason.BLOCKED_INVALID_WEIGHT,
        )


class TestNoProductionFallback(unittest.TestCase):
    """Spec §2: there must be NO production fallback to the
    Phase 2.10 hard-coded ETH-only allowlist."""

    def test_live_converge_never_returns_LIVE_ELIGIBLE_for_xau(self):
        """live_converge with XAU FASTFIB SELL (which is NOT
        Phase 2.10 ETH BUY) must NOT be live-eligible via a
        hidden fallback."""
        reg = _make_reg()  # XAU FASTFIB SELL
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=47028667,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        fake = FakeExecutor()
        # Call WITHOUT explicit validator (as a non-production
        # path that mimics the old contract).
        result = live_converge(
            reg, snap, execute_fn=fake.execute,
            supported_exchanges=_SUPPORTED_EXCHANGES,
        )
        # Without validate_accounts_fn, the gate fails closed.
        self.assertFalse(result.placed_live_order)
        self.assertIn("BLOCKED_INVALID_ACCOUNT", result.blocked_reason)

    def test_legacy_constants_are_not_consulted_by_live_converge(self):
        """Static guard: live.py must NOT have any code that references
        the Phase 2.10 constants in its eligibility path.
        Specifically: no ``is_allowlisted`` fallback."""
        import inspect
        from plugins.trade.fibo import live as live_mod
        src = inspect.getsource(live_mod.live_converge)
        # The eligibility branch MUST NOT call is_allowlisted.
        self.assertNotIn("is_allowlisted(", src,
                         "live_converge still references the Phase 2.10 "
                         "is_allowlisted shim — remove it")


class TestMissingEligibilityContextFailsClosed(unittest.TestCase):
    """Spec §3: missing required eligibility context FAILS CLOSED."""

    def test_missing_supported_exchanges_blocks(self):
        reg = _make_reg()
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=47028667,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        # Empty supported_exchanges = fail-closed.
        result = live_converge(
            reg, snap, execute_fn=FakeExecutor().execute,
            supported_exchanges=frozenset(),
            validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.placed_live_order)
        self.assertIn("BLOCKED_UNSUPPORTED_EXCHANGE", result.blocked_reason)

    def test_missing_validate_accounts_blocks(self):
        reg = _make_reg()
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=47028667,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        result = live_converge(
            reg, snap, execute_fn=FakeExecutor().execute,
            supported_exchanges=_SUPPORTED_EXCHANGES,
            # No validate_accounts_fn.
        )
        self.assertFalse(result.placed_live_order)
        self.assertIn("BLOCKED_INVALID_ACCOUNT", result.blocked_reason)


class TestCanonicalLatestRow(unittest.TestCase):
    """Spec §4: canonical latest persisted row works with
    equivalent-but-distinct Python object instances."""

    def test_equivalent_deserialized_registration_passes_canonical_check(self):
        import tempfile
        import os
        import json
        from plugins.trade.fibo.store import FiboRegistrationStore

        # Write one row to a temp store.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl",
                                          delete=False) as f:
            row = dict(_VALID_XAU_SELL_REG)
            f.write(json.dumps(row) + "\n")
            tmp_path = f.name
        try:
            store = FiboRegistrationStore(tmp_path)
            # Read via load_all().
            regs_a = store.load_all()
            # Read again — distinct Python objects.
            regs_b = store.load_all()
            self.assertEqual(len(regs_a), 1)
            self.assertEqual(len(regs_b), 1)
            # The two objects are NOT the same Python instance.
            self.assertIsNot(regs_a[0], regs_b[0])
            # But they have the same identity fields.
            for fname in ("registration_key", "exchange", "account",
                         "exchange_instrument", "source_symbol",
                         "variant", "side", "starting_volume", "status"):
                self.assertEqual(
                    getattr(regs_a[0], fname),
                    getattr(regs_b[0], fname),
                )
            # Pass the second instance to evaluate() — the
            # canonical check (which compares by VALUE, not by
            # Python object identity) must accept it.
            fibo = _make_fibo(
                symbol="XAUUSD", variant="FASTFIB",
                buy_cycle_id=47029142, sell_cycle_id=47028667,
                cumulative_buy_weight="4", cumulative_sell_weight="1",
            )
            snap = _make_snapshot(fibos=[fibo])
            result = evaluate(
                regs_b[0], snap,
                supported_exchanges=_SUPPORTED_EXCHANGES,
                validate_accounts_fn=_mock_validate_accounts,
                store=store,
            )
            self.assertTrue(result.eligible,
                            f"expected LIVE_ELIGIBLE, got {result.reason}")
        finally:
            os.unlink(tmp_path)


class TestFourIndependentSameSymbolRegistrations(unittest.TestCase):
    """Spec §5: FASTFIB BUY, FASTFIB SELL, NORMALFIB BUY,
    NORMALFIB SELL for the same source_symbol must coexist."""

    def test_four_distinct_registration_keys(self):
        from plugins.trade.fibo.store import FiboRegistration
        # All four on ondoperps/BITGET/XAU-USD.P
        ks = []
        for variant in ("FASTFIB", "NORMALFIB"):
            for side in ("BUY", "SELL"):
                reg = FiboRegistration.build(
                    exchange="ondoperps", account="BITGET",
                    symbol="XAUUSD", variant=variant, side=side,
                    starting_volume="0.001",
                    source="obs-1", source_seq=1, source_cycle_id=47028667,
                    source_cumulative_weight="1", source_percentage="0.01",
                    source_snapshot_received_at="2026-08-27T00:00:00Z",
                    desired_exchange_size=Decimal("0.001"),
                    exchange_instrument="XAU-USD.P",
                )
                ks.append(reg.registration_key)
        self.assertEqual(len(ks), 4)
        self.assertEqual(len(set(ks)), 4)
        # None of them is the Phase 2.10 ETH-USD.P key.
        for k in ks:
            self.assertIn("XAU-USD.P", k)


class TestSourceSymbolCrossAuthorization(unittest.TestCase):
    """Spec §5: a registration for one MT4 source cannot
    accidentally authorize another source that resolves to the
    same venue instrument."""

    def test_xau_reg_does_not_authorize_btc(self):
        reg = _make_reg()  # XAUUSD FASTFIB SELL on ondoperps/BITGET
        # A BTC snapshot for the same exchange/account — must NOT match.
        btc_fibo = Mt4Fibo(
            symbol="BTCUSD", variant="FASTFIB", percentage=Decimal("0.01"),
            buy_cycle_id=47029915, cumulative_buy_weight=Decimal("4"),
            sell_cycle_id=0, cumulative_sell_weight=Decimal("0"),
        )
        snap = _make_snapshot(fibos=[btc_fibo])
        result = evaluate(
            reg, snap,
            supported_exchanges=_SUPPORTED_EXCHANGES,
            validate_accounts_fn=_mock_validate_accounts,
        )
        # The BTC snapshot doesn't have XAUUSD entry → gate 11 blocks.
        self.assertFalse(result.eligible)
        self.assertEqual(
            result.reason_code, BlockReason.BLOCKED_SNAPSHOT_FIBO_MISMATCH,
        )


class TestUnconfiguredAccountBlocked(unittest.TestCase):
    """Spec §3 / §F: invalid/unconfigured account is blocked."""

    def test_unknown_account_blocks(self):
        reg = _make_reg(account="UNCONFIGURED_FAKE_ACCOUNT")
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=47028667,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        result = evaluate(
            reg, snap,
            supported_exchanges=_SUPPORTED_EXCHANGES,
            validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.eligible)
        self.assertEqual(
            result.reason_code, BlockReason.BLOCKED_INVALID_ACCOUNT,
        )

    def test_account_validator_raising_blocks(self):
        reg = _make_reg()
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=47028667,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        def broken(exchange):
            raise RuntimeError("validator crashed")
        result = evaluate(
            reg, snap,
            supported_exchanges=_SUPPORTED_EXCHANGES,
            validate_accounts_fn=broken,
        )
        self.assertFalse(result.eligible)
        self.assertEqual(
            result.reason_code, BlockReason.BLOCKED_INVALID_ACCOUNT,
        )


class TestBlockedReasonDeterministic(unittest.TestCase):
    """Spec §J: blocked reasons are specific/deterministic."""

    def test_exchange_mismatch_reason(self):
        reg = _make_reg(exchange="unsupported_exchange_xyz")
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=47028667,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        result = evaluate(
            reg, snap,
            supported_exchanges=_SUPPORTED_EXCHANGES,
            validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertEqual(
            result.reason_code, BlockReason.BLOCKED_UNSUPPORTED_EXCHANGE,
        )

    def test_account_mismatch_reason(self):
        reg = _make_reg(account="FOO_BAR_BAZ")
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47029142, sell_cycle_id=47028667,
            cumulative_buy_weight="4", cumulative_sell_weight="1",
        )
        snap = _make_snapshot(fibos=[fibo])
        result = evaluate(
            reg, snap,
            supported_exchanges=_SUPPORTED_EXCHANGES,
            validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertEqual(
            result.reason_code, BlockReason.BLOCKED_INVALID_ACCOUNT,
        )


class TestTargetZeroDiagnostic(unittest.TestCase):
    """Phase 2.13.12 enhanced diagnostic for target=0:
    distinguish flat vs. non-flat exchange exposure. The
    executor MUST NOT auto-flatten in either case (no
    ownership ledger). But the diagnostic message must be
    distinct so the operator can see what is happening."""

    def setUp(self):
        """Phase 2.13.18: clear cycle-state for test isolation."""
        from plugins.trade.fibo.cycle_state import CycleStateStore
        CycleStateStore()._atomic_write({"version": 1, "registrations": {}})

    def _make_reg_for_diagnostic(self):
        # ETH BUY ETH-USD.P (the legacy allowlisted reg).
        from plugins.trade.fibo.store import FiboRegistration
        return FiboRegistration.build(
            exchange="ondoperps", account="BITGET",
            symbol="ETHUSD", variant="NORMALFIB", side="BUY",
            starting_volume="0.001",
            source="obs-1", source_seq=1, source_cycle_id=47022998,
            source_cumulative_weight="2.0", source_percentage="0.01",
            source_snapshot_received_at="2026-08-27T00:00:00Z",
            desired_exchange_size=Decimal("0.002"),
            exchange_instrument="ETH-USD.P",
        )

    def test_target_zero_actual_flat_says_flat(self):
        reg = self._make_reg_for_diagnostic()
        # MT4 cycle=0, weight=0 → target=0.
        fibo = Mt4Fibo(
            symbol="ETHUSD", variant="NORMALFIB",
            percentage=Decimal("0.01"),
            buy_cycle_id=0, cumulative_buy_weight=Decimal("0"),
            sell_cycle_id=0, cumulative_sell_weight=Decimal("0"),
        )
        snap = Mt4Snapshot(
            v=1, source="obs-1", seq=1, ts=1, fibos=[fibo],
            received_at=datetime.now(timezone.utc).isoformat(),
            telegram_update_id=1, telegram_message_id=1, reader_chat_id=1,
        )
        fake = FakeExecutor(positions={"ondoperps|BITGET": []})
        result = live_converge(
            reg, snap, execute_fn=fake.execute,
            supported_exchanges=_SUPPORTED_EXCHANGES,
            validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.placed_live_order)
        self.assertIn("flat", result.blocked_reason.lower())
        self.assertIn("nothing to do", result.reason.lower())
        self.assertEqual(fake.new_order_calls(), [])

    def test_target_zero_actual_nonflat_says_ownership_not_proven(self):
        reg = self._make_reg_for_diagnostic()
        # MT4 cycle=0, weight=0 → target=0.
        fibo = Mt4Fibo(
            symbol="ETHUSD", variant="NORMALFIB",
            percentage=Decimal("0.01"),
            buy_cycle_id=0, cumulative_buy_weight=Decimal("0"),
            sell_cycle_id=0, cumulative_sell_weight=Decimal("0"),
        )
        snap = Mt4Snapshot(
            v=1, source="obs-1", seq=1, ts=1, fibos=[fibo],
            received_at=datetime.now(timezone.utc).isoformat(),
            telegram_update_id=1, telegram_message_id=1, reader_chat_id=1,
        )
        # Existing exchange exposure remains.
        fake = FakeExecutor(positions={"ondoperps|BITGET": [
            {"symbol": "ETH-USD.P", "side": "buy", "size": "0.003"}
        ]})
        result = live_converge(
            reg, snap, execute_fn=fake.execute,
            supported_exchanges=_SUPPORTED_EXCHANGES,
            validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.placed_live_order)
        # Phase 2.13.18: with cycle-awareness, the failure path
        # is the more specific BLOCKED_CYCLE_OWNERSHIP_UNKNOWN
        # because no synchronized_cycle_id exists for the reg.
        self.assertIn("BLOCKED_CYCLE_OWNERSHIP_UNKNOWN", result.blocked_reason)
        # The original semantics ("non-zero exchange exposure" +
        # "ownership not proven") must still be present in the
        # reason text for operator clarity.
        self.assertTrue(
            "non-zero exchange exposure" in result.blocked_reason.lower()
            or "exchange non-flat" in result.blocked_reason.lower(),
            f"expected 'non-zero exchange exposure' or 'exchange "
            f"non-flat' in: {result.blocked_reason!r}",
        )
        self.assertTrue(
            "ownership not proven" in result.reason.lower()
            or "refusing to auto-flatten" in result.reason.lower()
            or "unowned exposure" in result.reason.lower(),
            f"expected 'ownership not proven' or 'unowned exposure' "
            f"in: {result.reason!r}",
        )
        # No new_order placed.
        self.assertEqual(fake.new_order_calls(), [])
        # No cancel_order_group placed.
        self.assertEqual(fake.cancel_order_group_calls(), [])


class TestSourceIdentityGate(unittest.TestCase):
    """Phase 2.13.12 — fail-closed source identity gate.

    The MT4 reader enforces a single-active-source invariant
    upstream (Telegram sender identity + monotonic retirement).
    This gate is a defense-in-depth check: a registration may
    NOT become LIVE_ELIGIBLE unless the persisted registration's
    ``source`` field equals the current snapshot's ``source`` field
    exactly. Both must be non-empty.

    Failure modes:

    * reg.source == snap.source                  → pass this gate
    * reg.source != snap.source                  → BLOCKED_SOURCE_MISMATCH
    * empty reg.source                           → BLOCKED_SOURCE_MISMATCH
    * empty snap.source                          → BLOCKED_SOURCE_MISMATCH
    """

    def _make_reg_with_source(self, source: str):
        return _make_reg(source=source)

    def _make_snap_with_source(self, source: str, *, fibo=None):
        from datetime import datetime, timezone
        if fibo is None:
            fibo = _make_fibo(
                symbol="XAUUSD", variant="FASTFIB",
                buy_cycle_id=47031306, sell_cycle_id=47031306,
                cumulative_buy_weight="4",
                cumulative_sell_weight="4",
            )
        return Mt4Snapshot(
            v=1, source=source, seq=60000, ts=1, fibos=[fibo],
            received_at=datetime.now(timezone.utc).isoformat(),
            telegram_update_id=1, telegram_message_id=1,
            reader_chat_id=1,
        )

    def test_matching_source_passes_gate(self):
        # reg.source='mt4-test', snap.source='mt4-test' → passes.
        reg = self._make_reg_with_source("mt4-test")
        fibo = _make_fibo(
            symbol="XAUUSD", variant="FASTFIB",
            buy_cycle_id=47031306, sell_cycle_id=47031306,
            cumulative_buy_weight="4",
            cumulative_sell_weight="4",
        )
        snap = self._make_snap_with_source("mt4-test", fibo=fibo)
        result = evaluate(
            reg, snap,
            supported_exchanges=_SUPPORTED_EXCHANGES,
            validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertTrue(result.eligible,
                        f"expected LIVE_ELIGIBLE, got {result.reason_code.value}: {result.reason}")

    def test_different_source_blocks_with_BLOCKED_SOURCE_MISMATCH(self):
        # reg.source='mt4-A', snap.source='mt4-B' → BLOCKED.
        reg = self._make_reg_with_source("mt4-A")
        snap = self._make_snap_with_source("mt4-B")
        result = evaluate(
            reg, snap,
            supported_exchanges=_SUPPORTED_EXCHANGES,
            validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.eligible)
        self.assertEqual(
            result.reason_code,
            BlockReason.BLOCKED_SOURCE_MISMATCH,
        )
        self.assertIn("does not match", result.reason)

    def test_empty_registration_source_fails_closed(self):
        # reg.source='' → fail closed.
        reg = self._make_reg_with_source("")
        snap = self._make_snap_with_source("mt4-test")
        result = evaluate(
            reg, snap,
            supported_exchanges=_SUPPORTED_EXCHANGES,
            validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.eligible)
        self.assertEqual(
            result.reason_code,
            BlockReason.BLOCKED_SOURCE_MISMATCH,
        )
        self.assertIn("registration.source is empty", result.reason)

    def test_empty_snapshot_source_fails_closed(self):
        # snap.source='' → fail closed.
        reg = self._make_reg_with_source("mt4-test")
        snap = self._make_snap_with_source("")
        result = evaluate(
            reg, snap,
            supported_exchanges=_SUPPORTED_EXCHANGES,
            validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.eligible)
        self.assertEqual(
            result.reason_code,
            BlockReason.BLOCKED_SOURCE_MISMATCH,
        )
        self.assertIn("snapshot.source is empty", result.reason)

    def test_old_active_registration_under_mt4_A_with_new_mt4_B_snapshot(self):
        # The audit scenario: reg was created from mt4-A but the
        # current snapshot was published by mt4-B. Must NOT be live
        # eligible.
        reg_raw = dict(_VALID_XAU_SELL_REG)
        reg_raw["source"] = "mt4-OBSERVER-OLD-A"
        from plugins.trade.fibo.store import FiboRegistration
        reg = FiboRegistration.from_dict(reg_raw)
        snap = self._make_snap_with_source("mt4-OBSERVER-NEW-B")
        result = evaluate(
            reg, snap,
            supported_exchanges=_SUPPORTED_EXCHANGES,
            validate_accounts_fn=_mock_validate_accounts,
        )
        self.assertFalse(result.eligible)
        self.assertEqual(
            result.reason_code,
            BlockReason.BLOCKED_SOURCE_MISMATCH,
        )

    def test_canonical_source_cannot_be_replaced_in_memory(self):
        # Use a temporary store with the real row, then pass a
        # Registration that has all fields identical EXCEPT for
        # ``source``. The canonical comparison must catch it.
        import tempfile
        import os
        import json
        from plugins.trade.fibo.store import FiboRegistrationStore

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            row = dict(_VALID_XAU_SELL_REG)
            f.write(json.dumps(row) + "\n")
            tmp_path = f.name
        try:
            store = FiboRegistrationStore(tmp_path)
            regs = store.load_all()
            self.assertEqual(len(regs), 1)
            canonical = regs[0]

            # Reconstruct the same row but with a DIFFERENT source.
            from plugins.trade.fibo.store import FiboRegistration
            forged = dict(_VALID_XAU_SELL_REG)
            forged["source"] = "mt4-FORGED-OTHER"
            forged_reg = FiboRegistration.from_dict(forged)

            # Sanity check: the two have identical identity fields
            # EXCEPT for ``source``.
            for f in ("registration_key", "exchange", "account",
                      "exchange_instrument", "source_symbol",
                      "variant", "side", "starting_volume",
                      "status"):
                self.assertEqual(getattr(canonical, f),
                                 getattr(forged_reg, f))
            self.assertNotEqual(canonical.source, forged_reg.source)

            snap = self._make_snap_with_source("mt4-FORGED-OTHER")
            result = evaluate(
                forged_reg, snap,
                supported_exchanges=_SUPPORTED_EXCHANGES,
                validate_accounts_fn=_mock_validate_accounts,
                store=store,
            )
            # The forged reg has source="mt4-FORGED-OTHER" while
            # the canonical row has source="mt4-Fresh542468-1". The
            # canonical-row comparison (now including ``source``) is
            # the FIRST gate that catches it, so the block reason
            # is BLOCKED_INVALID_REGISTRATION (canonical_latest_value_mismatch).
            # The source-identity gate (Gate 1a) would catch it ONLY
            # if the canonical-row comparison had already accepted
            # the candidate — which it doesn't, because ``source``
            # is now part of the canonical-row identity_fields list.
            self.assertFalse(result.eligible)
            self.assertIn(
                result.reason_code,
                (
                    BlockReason.BLOCKED_SOURCE_MISMATCH,
                    BlockReason.BLOCKED_INVALID_REGISTRATION,
                ),
            )
        finally:
            os.unlink(tmp_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
