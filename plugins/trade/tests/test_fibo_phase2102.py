"""Phase 2.10.2 — Fibo re-entry safety tests.

Use realistic CanonicalPosition fixtures with the new
``exchange_instrument`` field set (as OndoPerps now produces).

Covers:
  CASE A: actual matches target -> NO new_order (live stub).
  CASE B: target=0.004 actual=0.002 -> delta=0.002 BUY.
  CASE C: target=0.002 actual=0.003 -> no reduction.
  CASE D: target=0.002 actual SHORT -> no auto-flip.
  CASE E: target=0      actual=0.002 -> no auto-flatten.
"""
from __future__ import annotations

import dataclasses
import unittest
from decimal import Decimal
from types import SimpleNamespace

from plugins.trade.fibo.executor import (
    _read_actual_position_from_response,
    _compute_remaining_delta,
    _resolve_mt4_target,
)
from plugins.trade.fibo.shadow import shadow_run
from plugins.trade.fibo.live import live_converge
from plugins.trade.fibo.snapshot import Mt4Snapshot, Mt4Fibo
from plugins.trade.fibo.store import FiboRegistration


@dataclasses.dataclass(frozen=True)
class FakeCanonicalPosition:
    symbol: str
    side: str
    size: str
    entry_price: str = "0"
    pnl: str = "0"
    tp: Optional[str] = None
    sl: Optional[str] = None
    tp_count: Optional[int] = None
    sl_count: Optional[int] = None
    exchange_instrument: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class FakeCanonicalResponse:
    success: bool = True
    operation: str = "positions_orders"
    positions: Optional[List] = None
    order_groups: Optional[List] = None
    open_order_count: Optional[int] = 0
    order: Optional[Dict] = None
    error: Optional[Any] = None

    def to_dict(self):
        return {
            "success": self.success,
            "operation": self.operation,
            "positions": list(self.positions or []),
            "order_groups": list(self.order_groups or []),
            "open_order_count": self.open_order_count,
            "order": self.order,
            "error": self.error,
        }


def _make_reg(
    *, exchange="ondoperps", account="BITGET",
    exchange_instrument="ETH-USD.P",
    source_symbol="ETHUSD",
    variant="NORMALFib", side="BUY",
    starting_volume="0.001",
    source_cycle_id=47022998,
    source_cumulative_weight="2.0",
):
    return FiboRegistration.build(
        exchange=exchange, account=account,
        symbol=source_symbol, variant=variant, side=side,
        starting_volume=starting_volume,
        source="obs-1", source_seq=1,
        source_cycle_id=source_cycle_id,
        source_cumulative_weight=source_cumulative_weight,
        source_percentage="0.01",
        source_snapshot_received_at="2026-08-27T00:00:00Z",
        desired_exchange_size=Decimal("0.002"),
        source_symbol=source_symbol,
        exchange_instrument=exchange_instrument,
    )


def _make_snap(*, buy_cycle=47022998, buy_weight="2.0"):
    fibo = Mt4Fibo(
        symbol="ETHUSD", variant="NORMALFib",
        percentage=Decimal("0.01"),
        buy_cycle_id=buy_cycle,
        cumulative_buy_weight=Decimal(buy_weight),
        sell_cycle_id=0,
        cumulative_sell_weight=Decimal("0"),
    )
    return Mt4Snapshot(
        v=1, source="obs-1", seq=1, ts=1, fibos=[fibo],
        received_at="2026-08-27T00:00:00Z",
        telegram_update_id=1, telegram_message_id=1, reader_chat_id=1,
    )


def _eth_position(side: str, size: str) -> FakeCanonicalPosition:
    """Build a FakeCanonicalPosition that mirrors the OndoPerps agent's
    output: ``symbol`` is the display name ``ETH``, and
    ``exchange_instrument`` is the full canonical ``ETH-USD.P``."""
    return FakeCanonicalPosition(
        symbol="ETH", side=side, size=size,
        exchange_instrument="ETH-USD.P",
    )


class ExecutorMatcherPhase2102Tests(unittest.TestCase):
    """The matcher must find the venue position via the new
    ``exchange_instrument`` field, not by ``symbol`` stripping."""

    def test_finds_position_via_exchange_instrument(self):
        reg = _make_reg()
        snap = _make_snap()
        resp = FakeCanonicalResponse(
            success=True,
            positions=[_eth_position("long", "0.002")],
        )
        actual = _read_actual_position_from_response(reg, resp)
        self.assertEqual(actual.symbol, "ETH-USD.P")
        self.assertEqual(actual.side, "buy",  # venue 'long' → Fibo 'buy'
                         "venue long should normalize to Fibo buy")
        self.assertEqual(actual.size, Decimal("0.002"))
        self.assertFalse(actual.is_flat)
        self.assertFalse(actual.read_failed)

    def test_target_achieved_no_delta(self):
        reg = _make_reg()
        snap = _make_snap()
        resp = FakeCanonicalResponse(
            positions=[_eth_position("long", "0.002")],
        )
        target = _resolve_mt4_target(reg, snap)
        actual = _read_actual_position_from_response(reg, resp)
        self.assertEqual(target.size, Decimal("0.002"))
        self.assertEqual(actual.size, Decimal("0.002"))
        delta = _compute_remaining_delta(actual, target)
        self.assertIsNone(
            delta,
            "actual=target=0.002 must yield delta=None "
            "(Phase 2.10 no-reduction)",
        )

    def test_oversized_position_no_reduction(self):
        reg = _make_reg()
        snap = _make_snap()
        resp = FakeCanonicalResponse(
            positions=[_eth_position("long", "0.003")],
        )
        target = _resolve_mt4_target(reg, snap)
        actual = _read_actual_position_from_response(reg, resp)
        delta = _compute_remaining_delta(actual, target)
        self.assertIsNone(delta)

    def test_short_actual_no_flip(self):
        reg = _make_reg()
        snap = _make_snap()
        resp = FakeCanonicalResponse(
            positions=[_eth_position("short", "0.005")],
        )
        target = _resolve_mt4_target(reg, snap)
        actual = _read_actual_position_from_response(reg, resp)
        # Wrong-side scenario: actual side != target side.
        # _compute_remaining_delta returns None (no-op).
        delta = _compute_remaining_delta(actual, target)
        self.assertIsNone(delta)

    def test_below_target_produces_correct_delta(self):
        reg = _make_reg()
        snap = _make_snap()
        resp = FakeCanonicalResponse(
            positions=[_eth_position("long", "0.0005")],
        )
        target = _resolve_mt4_target(reg, snap)
        actual = _read_actual_position_from_response(reg, resp)
        delta = _compute_remaining_delta(actual, target)
        self.assertIsNotNone(delta)
        self.assertEqual(delta.side, "buy")
        self.assertEqual(delta.size, Decimal("0.0015"))

    def test_target_flat_no_delta(self):
        # Make target 0 by setting buy_weight=0.
        reg = _make_reg()
        snap = _make_snap(buy_cycle=0, buy_weight="0")
        resp = FakeCanonicalResponse(
            positions=[_eth_position("long", "0.002")],
        )
        target = _resolve_mt4_target(reg, snap)
        actual = _read_actual_position_from_response(reg, resp)
        self.assertEqual(target.size, Decimal("0"))
        delta = _compute_remaining_delta(actual, target)
        # target=0 -> no delta.
        self.assertIsNone(delta)

    def test_flat_venue_opens_to_target(self):
        reg = _make_reg()
        snap = _make_snap()
        resp = FakeCanonicalResponse(positions=[])  # venue flat
        target = _resolve_mt4_target(reg, snap)
        actual = _read_actual_position_from_response(reg, resp)
        self.assertTrue(actual.is_flat)
        delta = _compute_remaining_delta(actual, target)
        self.assertIsNotNone(delta)
        self.assertEqual(delta.action, "OPEN")
        self.assertEqual(delta.side, "buy")
        self.assertEqual(delta.size, Decimal("0.002"))

    def test_unrelated_position_not_matched(self):
        """A BTC position must NOT match ETH-USD.P."""
        reg = _make_reg()
        snap = _make_snap()
        btc = FakeCanonicalPosition(
            symbol="BTC", side="short", size="0.135",
            exchange_instrument="BTC-USD.P",
        )
        resp = FakeCanonicalResponse(positions=[btc])
        actual = _read_actual_position_from_response(reg, resp)
        self.assertTrue(actual.is_flat)
        self.assertEqual(actual.size, Decimal("0"))

    def test_unknown_exchange_instrument_does_not_match(self):
        """An unknown exchange_instrument must not accidentally
        match the controlled registration. No guessing by stripped
        base symbol."""
        reg = _make_reg()
        snap = _make_snap()
        unknown = FakeCanonicalPosition(
            symbol="ETH", side="long", size="0.002",
            exchange_instrument="UNKNOWN-INSTRUMENT",
        )
        resp = FakeCanonicalResponse(positions=[unknown])
        actual = _read_actual_position_from_response(reg, resp)
        self.assertTrue(
            actual.is_flat,
            "unknown exchange_instrument MUST NOT match — no guessing",
        )
        self.assertEqual(actual.size, Decimal("0"))

    def test_eth_position_with_unrelated_canonical_no_match(self):
        """An ETH display symbol under a different canonical identity
        must not match the ETH-USD.P registration. The matcher must
        prefer canonical identity, not display name."""
        reg = _make_reg()
        snap = _make_snap()
        # Symbol 'ETH' but canonical 'BTC-ETH' (hypothetical).
        weird = FakeCanonicalPosition(
            symbol="ETH", side="long", size="0.002",
            exchange_instrument="BTC-ETH",  # NOT ETH-USD.P
        )
        resp = FakeCanonicalResponse(positions=[weird])
        actual = _read_actual_position_from_response(reg, resp)
        self.assertTrue(
            actual.is_flat,
            "non-matching canonical identity must not match — "
            "matcher prefers exchange_instrument, not symbol",
        )

    def test_falls_back_to_symbol_when_no_exchange_instrument(self):
        """For agents that don't expose exchange_instrument, the
        matcher still works via the symbol field (existing
        behavior)."""
        reg = _make_reg()
        snap = _make_snap()
        eth_legacy = FakeCanonicalPosition(
            symbol="ETH-USD.P", side="long", size="0.002",
            exchange_instrument=None,  # legacy agents
        )
        resp = FakeCanonicalResponse(positions=[eth_legacy])
        actual = _read_actual_position_from_response(reg, resp)
        self.assertFalse(actual.is_flat)
        self.assertEqual(actual.size, Decimal("0.002"))


class ShadowPhase2102Tests(unittest.TestCase):
    """Shadow mode must now report ``would_order=None`` when the
    venue position matches target via canonical identity."""

    def test_case_a_target_achieved_no_would_order(self):
        reg = _make_reg()
        snap = _make_snap()
        resp = FakeCanonicalResponse(
            positions=[_eth_position("long", "0.002")],
        )
        calls = []

        def _fn(req):
            calls.append(dict(req))
            return resp

        result = shadow_run(reg, snap, execute_fn=_fn)
        self.assertIsNone(result.would_order,
                          f"expected would_order=None, got {result.would_order}")
        self.assertEqual(result.actual_size, "0.002")
        self.assertEqual(result.actual_side, "buy")
        self.assertEqual(result.target_size, "0.002")

    def test_case_b_delta_below_target(self):
        reg = _make_reg()
        snap = _make_snap(buy_weight="4.0")  # target = 0.001 * 4 = 0.004
        resp = FakeCanonicalResponse(
            positions=[_eth_position("long", "0.002")],
        )
        calls = []

        def _fn(req):
            calls.append(dict(req))
            return resp

        result = shadow_run(reg, snap, execute_fn=_fn)
        self.assertIsNotNone(result.would_order)
        self.assertEqual(result.would_order.volume, "0.002")
        self.assertEqual(result.would_order.side, "buy")

    def test_case_c_oversized_no_would_order(self):
        reg = _make_reg()
        snap = _make_snap()
        resp = FakeCanonicalResponse(
            positions=[_eth_position("long", "0.003")],
        )
        calls = []

        def _fn(req):
            calls.append(dict(req))
            return resp

        result = shadow_run(reg, snap, execute_fn=_fn)
        self.assertIsNone(result.would_order)

    def test_case_d_short_no_flip(self):
        reg = _make_reg()
        snap = _make_snap()
        resp = FakeCanonicalResponse(
            positions=[_eth_position("short", "0.005")],
        )
        calls = []

        def _fn(req):
            calls.append(dict(req))
            return resp

        result = shadow_run(reg, snap, execute_fn=_fn)
        self.assertIsNone(result.would_order)
        # actual_side is reported in Fibo-side canonical form:
        # venue 'short' → Fibo 'sell'.
        self.assertEqual(result.actual_side, "sell")

    def test_case_e_target_flat_no_flatten(self):
        reg = _make_reg()
        snap = _make_snap(buy_cycle=0, buy_weight="0")
        resp = FakeCanonicalResponse(
            positions=[_eth_position("long", "0.002")],
        )
        calls = []

        def _fn(req):
            calls.append(dict(req))
            return resp

        result = shadow_run(reg, snap, execute_fn=_fn)
        self.assertIsNone(result.would_order)
        self.assertEqual(result.would_cancel, ())


class LiveStubPhase2102Tests(unittest.TestCase):
    """Stubbed live_converge() must NOT issue a new_order when the
    venue position matches target via canonical identity."""

    def test_case_a_target_achieved_no_new_order(self):
        reg = _make_reg()
        snap = _make_snap()
        resp = FakeCanonicalResponse(
            positions=[_eth_position("long", "0.002")],
        )
        calls = []

        def _fn(req):
            calls.append(dict(req))
            return resp

        result = live_converge(reg, snap, execute_fn=_fn)
        self.assertFalse(result.placed_live_order)
        self.assertIsNone(result.placed_request)
        new_orders = [c for c in calls if c["operation"] == "new_order"]
        self.assertEqual(new_orders, [])

    def test_case_b_delta_below_target_place_one(self):
        reg = _make_reg()
        snap = _make_snap(buy_weight="4.0")  # target = 0.004
        resp = FakeCanonicalResponse(
            positions=[_eth_position("long", "0.002")],
        )
        calls = []

        def _fn(req):
            calls.append(dict(req))
            return resp

        result = live_converge(reg, snap, execute_fn=_fn)
        self.assertTrue(result.placed_live_order)
        self.assertEqual(result.placed_request["volume"], "0.002")

    def test_case_c_oversized_no_reduction(self):
        reg = _make_reg()
        snap = _make_snap()
        resp = FakeCanonicalResponse(
            positions=[_eth_position("long", "0.003")],
        )
        calls = []

        def _fn(req):
            calls.append(dict(req))
            return resp

        result = live_converge(reg, snap, execute_fn=_fn)
        self.assertFalse(result.placed_live_order)
        new_orders = [c for c in calls if c["operation"] == "new_order"]
        self.assertEqual(new_orders, [])

    def test_case_d_short_no_flip(self):
        reg = _make_reg()
        snap = _make_snap()
        resp = FakeCanonicalResponse(
            positions=[_eth_position("short", "0.005")],
        )
        calls = []

        def _fn(req):
            calls.append(dict(req))
            return resp

        result = live_converge(reg, snap, execute_fn=_fn)
        self.assertFalse(result.placed_live_order)

    def test_case_e_target_flat_no_flatten(self):
        reg = _make_reg()
        snap = _make_snap(buy_cycle=0, buy_weight="0")
        resp = FakeCanonicalResponse(
            positions=[_eth_position("long", "0.002")],
        )
        calls = []

        def _fn(req):
            calls.append(dict(req))
            return resp

        result = live_converge(reg, snap, execute_fn=_fn)
        self.assertFalse(result.placed_live_order)
        cancels = [c for c in calls if c["operation"] == "cancel_order_group"]
        self.assertEqual(cancels, [])
        new_orders = [c for c in calls if c["operation"] == "new_order"]
        self.assertEqual(new_orders, [])


if __name__ == "__main__":
    unittest.main()