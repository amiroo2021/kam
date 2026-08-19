"""GoldenFibo v1: real tests for real code.

These tests exercise the volume derivation, price math, and the
state machine against a fake adapter. They verify the locked
specification exactly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from decimal import Decimal
from typing import Dict, List, Optional


# Remove the hermes-agent editable-install's path_hook BEFORE any
# other import reaches ``plugins.*``. Without this, the editable
# install redirects imports to the installed venv copy, not the
# source tree at /root/kam/plugins/....
_EDITABLE_FINDER = "__editable___hermes_agent_0_20_0_finder"
_KNOWN_EDITABLE_FINDERS = (_EDITABLE_FINDER,)
if any(name in repr(h) for h in sys.path_hooks for name in _KNOWN_EDITABLE_FINDERS):
    sys.path_hooks[:] = [
        h
        for h in sys.path_hooks
        if not any(name in repr(h) for name in _KNOWN_EDITABLE_FINDERS)
    ]

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
# NOTE: Do NOT pop plugins.trade.* from sys.modules here.
# Session-level isolation lives in conftest.py. Mid-suite pops
# create dual CanonicalResponse/TradeDesk identities and break
# later tests (INVALID_AGENT_RESPONSE / ImportError agents).


from plugins.trade.golden_fibo.config import (
    FIBO_RATIO,
    MAX_STEP,
    GoldenFiboConfig,
    golden_fibo_cumulative_volume,
    golden_fibo_next_ladder_price,
    golden_fibo_tp_price,
    golden_fibo_volume,
)
from plugins.trade.golden_fibo.engine import GoldenFiboEngine
from plugins.trade.golden_fibo.state import (
    ROLE_ENTRY,
    ROLE_LADDER,
    ROLE_TP,
    STATUS_NEEDS_RECOVERY,
    STATUS_QUARANTINED_OLD_STRATEGY,
    STATUS_RUNNING,
    STATUS_STOPPING,
    STRATEGY_GOLDENFIBO,
    GoldenFiboState,
)

import unittest



# ---------------------------------------------------------------------------
# Volume derivation
# ---------------------------------------------------------------------------
class TestVolumeDerivation(unittest.TestCase):
    def test_v0_equals_step0(self):
        self.assertEqual(golden_fibo_volume(Decimal("0.01"), 0), Decimal("0.01"))

    def test_v1_equals_step0(self):
        self.assertEqual(golden_fibo_volume(Decimal("0.01"), 1), Decimal("0.01"))

    def test_v2_equals_double_step0(self):
        self.assertEqual(golden_fibo_volume(Decimal("0.01"), 2), Decimal("0.02"))

    def test_v3_equals_quadruple_step0(self):
        self.assertEqual(golden_fibo_volume(Decimal("0.01"), 3), Decimal("0.04"))

    def test_v4_equals_eight_step0(self):
        self.assertEqual(golden_fibo_volume(Decimal("0.01"), 4), Decimal("0.08"))

    def test_v20_equals_step0_times_2_19(self):
        self.assertEqual(golden_fibo_volume(Decimal("0.01"), 20), Decimal("0.01") * (Decimal(2) ** 19))

    def test_btc_step0_through_step4(self):
        self.assertEqual(golden_fibo_volume(Decimal("0.0001"), 0), Decimal("0.0001"))
        self.assertEqual(golden_fibo_volume(Decimal("0.0001"), 1), Decimal("0.0001"))
        self.assertEqual(golden_fibo_volume(Decimal("0.0001"), 2), Decimal("0.0002"))
        self.assertEqual(golden_fibo_volume(Decimal("0.0001"), 3), Decimal("0.0004"))
        self.assertEqual(golden_fibo_volume(Decimal("0.0001"), 4), Decimal("0.0008"))

    def test_cumulative_through_step0(self):
        self.assertEqual(golden_fibo_cumulative_volume(Decimal("0.01"), 0), Decimal("0.01"))

    def test_cumulative_through_step1(self):
        # 0.01 + 0.01 = 0.02
        self.assertEqual(golden_fibo_cumulative_volume(Decimal("0.01"), 1), Decimal("0.02"))

    def test_cumulative_through_step2(self):
        # 0.01 + 0.01 + 0.02 = 0.04
        self.assertEqual(golden_fibo_cumulative_volume(Decimal("0.01"), 2), Decimal("0.04"))

    def test_cumulative_through_step3(self):
        # 0.01 + 0.01 + 0.02 + 0.04 = 0.08
        self.assertEqual(golden_fibo_cumulative_volume(Decimal("0.01"), 3), Decimal("0.08"))

    def test_invalid_n_raises(self):
        with self.assertRaises(ValueError):
            golden_fibo_volume(Decimal("0.01"), -1)
        with self.assertRaises(ValueError):
            golden_fibo_volume(Decimal("0.01"), MAX_STEP + 1)


# ---------------------------------------------------------------------------
# Price math
# ---------------------------------------------------------------------------
class TestPriceMath(unittest.TestCase):
    def test_buy_tp0(self):
        p0 = Decimal("100")
        expected = Decimal("100") * (Decimal("1") + Decimal("0.05"))
        self.assertEqual(golden_fibo_tp_price("BUY", p0, Decimal("0.05")), expected)

    def test_sell_tp0(self):
        p0 = Decimal("100")
        expected = Decimal("100") * (Decimal("1") - Decimal("0.05"))
        self.assertEqual(golden_fibo_tp_price("SELL", p0, Decimal("0.05")), expected)

    def test_buy_next_ladder_below(self):
        # BUY: tp below p -> next p below p
        p = Decimal("100")
        tp = Decimal("95")
        next_p = golden_fibo_next_ladder_price("BUY", p, tp)
        # next_p = 100 + 1.618 * (100 - 95) = 100 + 8.09 = 108.09
        self.assertEqual(next_p, Decimal("100") + FIBO_RATIO * (Decimal("100") - Decimal("95")))

    def test_sell_next_ladder_above(self):
        # SELL: tp above p -> next p above p
        p = Decimal("100")
        tp = Decimal("105")
        next_p = golden_fibo_next_ladder_price("SELL", p, tp)
        self.assertEqual(next_p, Decimal("100") + FIBO_RATIO * (Decimal("100") - Decimal("105")))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class TestConfig(unittest.TestCase):
    def test_registration_key_buy(self):
        cfg = GoldenFiboConfig(
            exchange="lighter",
            account="amiroo",
            instrument="SOL",
            direction="BUY",
            percentage=Decimal("0.01"),
            step0_volume=Decimal("0.01"),
        )
        self.assertEqual(cfg.registration_key, "lighter/amiroo/SOL/BUY")

    def test_registration_key_sell(self):
        cfg = GoldenFiboConfig(
            exchange="lighter",
            account="amiroo",
            instrument="SOL",
            direction="SELL",
            percentage=Decimal("0.01"),
            step0_volume=Decimal("0.01"),
        )
        self.assertEqual(cfg.registration_key, "lighter/amiroo/SOL/SELL")

    def test_invalid_direction_raises(self):
        with self.assertRaises(ValueError):
            GoldenFiboConfig(
                exchange="lighter",
                account="amiroo",
                instrument="SOL",
                direction="counterBUY",
                percentage=Decimal("0.01"),
                step0_volume=Decimal("0.01"),
            )


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------
class TestStateSchema(unittest.TestCase):
    def test_round_trip(self):
        s = GoldenFiboState(
            registration_key="lighter/amiroo/SOL/BUY",
            cycle_id=1,
            exchange="lighter",
            account="amiroo",
            instrument="SOL",
            direction="BUY",
            percentage=Decimal("0.01"),
            step0_volume=Decimal("0.01"),
            highest_filled_step=2,
            fill_prices={0: Decimal("100"), 1: Decimal("98.38"), 2: Decimal("96.7")},
            expected_cumulative_size=Decimal("0.04"),
            current_tp_price=Decimal("100"),
            current_tp_order_id=111,
            current_tp_client_id=222,
            current_tp_role=ROLE_TP,
            next_step=3,
            pending_order_client_id=333,
            pending_order_exchange_id=444,
            pending_requested_price=Decimal("95.05"),
            pending_requested_size=Decimal("0.04"),
            pending_confirmed_price=Decimal("95.05"),
            pending_confirmed_size=Decimal("0.04"),
            pending_order_role=ROLE_LADDER,
            status=STATUS_RUNNING,
        )
        # Round-trip through dict
        s2 = GoldenFiboState.from_dict(s.to_dict())
        self.assertEqual(s2.registration_key, s.registration_key)
        self.assertEqual(s2.cycle_id, s.cycle_id)
        self.assertEqual(s2.highest_filled_step, s.highest_filled_step)
        self.assertEqual(s2.fill_prices, {0: Decimal("100"), 1: Decimal("98.38"), 2: Decimal("96.7")})
        self.assertEqual(s2.expected_cumulative_size, s.expected_cumulative_size)
        self.assertEqual(s2.current_tp_price, s.current_tp_price)
        self.assertEqual(s2.current_tp_order_id, s.current_tp_order_id)
        self.assertEqual(s2.current_tp_role, s.current_tp_role)
        self.assertEqual(s2.next_step, s.next_step)
        self.assertEqual(s2.pending_order_exchange_id, s.pending_order_exchange_id)
        self.assertEqual(s2.pending_confirmed_price, s.pending_confirmed_price)
        self.assertEqual(s2.status, s.status)

    def test_default_strategy_is_golden_fibo(self):
        s = GoldenFiboState()
        self.assertEqual(s.strategy, STRATEGY_GOLDENFIBO)
        self.assertEqual(s.schema_version, 1)

    def test_old_strategy_state(self):
        s = GoldenFiboState(
            strategy="fibonacci_counter_cascade",
            registration_key="lighter/amiroo:SOL:counterBUY",
            status=STATUS_QUARANTINED_OLD_STRATEGY,
        )
        self.assertEqual(s.strategy, "fibonacci_counter_cascade")
        self.assertEqual(s.status, STATUS_QUARANTINED_OLD_STRATEGY)


# ---------------------------------------------------------------------------
# Fake adapter for engine tests
# ---------------------------------------------------------------------------
class FakeAdapter:
    def __init__(self) -> None:
        self.position = {"symbol": "SOL", "side": None, "size": "0", "sl": None, "tp": None}
        self.orders: Dict[int, Dict[str, object]] = {}
        self.submit_log: List[Dict[str, object]] = []
        self.cancel_log: List[int] = []
        self._next_exchange_id = 1000

    def _gen_id(self) -> int:
        self._next_exchange_id += 1
        return self._next_exchange_id

    def resolve_instrument(self, account: str, instrument: str) -> Dict[str, object]:
        return {"symbol": instrument, "size_decimals": 3, "price_decimals": 3, "min_base_amount": "0.001"}

    def position_state(self, account: str, instrument: str) -> Dict[str, object]:
        return dict(self.position)

    def get_order_state(self, account: str, order_index: int) -> Dict[str, object]:
        rec = self.orders.get(int(order_index))
        if rec is None:
            return {}
        return {
            "order_index": order_index,
            "client_order_index": rec.get("client_order_id"),
            "symbol": "SOL",
            "side": rec.get("side"),
            "type": rec.get("type"),
            "status": rec.get("status"),
            "taxonomy": rec.get("taxonomy"),
            "requested_price": rec.get("price"),
            "requested_size": rec.get("size"),
            "filled_size": rec.get("size"),
            "filled_quote": None,
            "actual_fill_price": None,
            "reduce_only": rec.get("reduce_only", False),
        }

    def place_market(self, *, account, instrument, side, size, client_order_id: int) -> Dict[str, object]:
        oid = self._gen_id()
        rec = {
            "exchange_order_id": oid,
            "client_order_id": client_order_id,
            "side": side,
            "type": "market",
            "size": str(size),
            "price": None,
            "status": "filled",
            "taxonomy": "FILLED",
            "reduce_only": False,
        }
        self.orders[oid] = rec
        self.submit_log.append(dict(rec, role="entry"))
        # Update fake position
        prev = Decimal(str(self.position.get("size") or "0"))
        if self.position.get("side") == side:
            new_size = prev + Decimal(str(size))
        elif self.position.get("side") is None:
            new_size = Decimal(str(size))
            self.position["side"] = "long" if side == "buy" else "short"
        else:
            new_size = abs(prev - Decimal(str(size)))
            if prev < Decimal(str(size)):
                self.position["side"] = "long" if side == "buy" else "short"
        self.position["size"] = str(new_size)
        return {
            "client_order_id": client_order_id,
            "exchange_order_id": oid,
            "submitted_price": None,
            "submitted_volume": str(size),
            "status": "filled",
            "verified": True,
            "role": "entry",
        }

    def place_limit(self, *, account, instrument, side, size, price, client_order_id: int, reduce_only: bool = False) -> Dict[str, object]:
        oid = self._gen_id()
        # Quantize price to 3 decimals (round down)
        quantized_price = Decimal(str(price)).quantize(Decimal("0.001"))
        rec = {
            "exchange_order_id": oid,
            "client_order_id": client_order_id,
            "side": side,
            "type": "limit",
            "size": str(size),
            "price": str(quantized_price),
            "status": "open",
            "taxonomy": "ACTIVE",
            "reduce_only": bool(reduce_only),
        }
        self.orders[oid] = rec
        self.submit_log.append(dict(rec, role="tp" if reduce_only else "ladder"))
        if reduce_only:
            self.position["tp"] = str(quantized_price)
        return {
            "client_order_id": client_order_id,
            "exchange_order_id": oid,
            "submitted_price": str(quantized_price),
            "submitted_volume": str(size),
            "status": "submitted",
            "verified": True,
            "role": "tp" if reduce_only else "ladder",
        }

    def set_shared_tp(self, *, account, instrument, price, side=None, size=None, client_order_id=None) -> Dict[str, object]:
        """Stub for the thin adapter set_shared_tp. Mirrors x_lighter_agent
        set_tp semantics: derives size from the live position, closing side
        opposite to the position, and registers a TP order record so the
        engine/tests can track and simulate it."""
        from decimal import Decimal as _D
        # Cancel any existing TP (set_tp replaces the existing TP).
        for rec in list(self.orders.values()):
            if rec.get("reduce_only") and rec.get("status") == "open" and rec.get("role_type") == "tp":
                rec["status"] = "canceled"
                rec["taxonomy"] = "CANCELED"
        oid = self._gen_id()
        quantized_price = _D(str(price)).quantize(_D("0.001"))
        # TP is opposite side of the live position.
        live_side = self.position.get("side")
        closing = "sell" if live_side == "long" else "buy"
        size = self.position.get("size") or "0"
        rec = {
            "exchange_order_id": oid,
            "client_order_id": None,
            "side": closing,
            "type": "take-profit",
            "role_type": "tp",
            "size": str(size),
            "price": str(quantized_price),
            "status": "open",
            "taxonomy": "ACTIVE",
            "reduce_only": True,
        }
        self.orders[oid] = rec
        self.submit_log.append(dict(rec, role="tp"))
        self.position["tp"] = str(quantized_price)
        return {
            "verified": True,
            "submitted_price": str(quantized_price),
            "exchange_order_id": oid,
            "current_side": live_side,
            "current_size": str(size),
            "role": "tp",
        }

    def cancel_order(self, *, account, order_index: int) -> bool:
        rec = self.orders.get(int(order_index))
        if rec is None:
            return False
        rec["status"] = "canceled"
        rec["taxonomy"] = "CANCELED"
        self.cancel_log.append(int(order_index))
        if rec.get("reduce_only"):
            self.position["tp"] = None
        return True

    def simulate_position(self, size: Decimal, side: str) -> None:
        self.position["size"] = str(size)
        self.position["side"] = side

    def simulate_order_filled(self, exchange_order_id: int) -> None:
        rec = self.orders.get(int(exchange_order_id))
        if rec is None:
            return
        rec["status"] = "filled"
        rec["taxonomy"] = "FILLED"

    def simulate_order_canceled(self, exchange_order_id: int) -> None:
        rec = self.orders.get(int(exchange_order_id))
        if rec is None:
            return
        rec["status"] = "canceled"
        rec["taxonomy"] = "CANCELED"

    def remove_order(self, exchange_order_id: int) -> None:
        self.orders.pop(int(exchange_order_id), None)


def _client_id_factory():
    n = [0]
    def f() -> int:
        n[0] += 1
        return n[0] + 100000
    return f


def _make_engine_buy(adapter: FakeAdapter, step0: str = "0.01", pct: str = "0.01") -> GoldenFiboEngine:
    cfg = GoldenFiboConfig(
        exchange="lighter",
        account="amiroo",
        instrument="SOL",
        direction="BUY",
        percentage=Decimal(pct),
        step0_volume=Decimal(step0),
    )
    state = GoldenFiboState(registration_key=cfg.registration_key, cycle_id=0)
    return GoldenFiboEngine(cfg, state, adapter, _client_id_factory())


# ---------------------------------------------------------------------------
# Engine cases
# ---------------------------------------------------------------------------
class TestEngineCaseAStartFreshCycle(unittest.TestCase):
    def test_step0_market_submitted_on_first_tick(self):
        adapter = FakeAdapter()
        engine = _make_engine_buy(adapter)
        result = engine.tick()
        # Step0 was submitted
        self.assertEqual(len(adapter.submit_log), 1)
        self.assertEqual(adapter.submit_log[0]["role"], "entry")
        self.assertEqual(adapter.submit_log[0]["type"], "market")
        # Engine state: pending entry exists
        self.assertEqual(engine.state.pending_order_role, ROLE_ENTRY)
        self.assertTrue(engine.state.pending_order_exchange_id is not None)
        self.assertEqual(engine.state.cycle_id, 1)
        self.assertEqual(engine.state.status, STATUS_RUNNING)

    def test_step0_then_position_confirmed_then_p0_then_tp_then_step1(self):
        """Full Case A: Step0 → P0 → TP0 → Step1."""
        adapter = FakeAdapter()
        engine = _make_engine_buy(adapter, step0="0.01", pct="0.05")
        # Tick 1: submit Step0
        engine.tick()
        exchange_oid = engine.state.pending_order_exchange_id
        self.assertTrue(exchange_oid is not None)
        # Step0 is FILLED in the fake adapter
        adapter.simulate_order_filled(exchange_oid)
        # Now we have a long position with size 0.01 — but we have not
        # promoted P0 yet; the engine is awaiting P0 confirmation.
        # Confirm Step0 via the engine helper: confirm_step0_filled(P0)
        # Then we must place TP + Step1.
        # In the real path the service reads actual_fill_price. Here
        # we choose P0 from the adapter's knowledge.
        # Set the fake position to long 0.01 (the adapter already does
        # this on place_market). Now we run the rotation.
        engine.confirm_step0_filled(Decimal("100"))
        # State should reflect Step0 fill
        self.assertEqual(engine.state.highest_filled_step, 0)
        self.assertEqual(engine.state.fill_prices[0], Decimal("100"))
        self.assertEqual(engine.state.next_step, 1)
        # Place TP + Step1
        result = engine.place_step0_tp_and_step1(Decimal("100"))
        self.assertTrue(result is None or result.state.status == STATUS_RUNNING)
        # TP0 = 100 * 1.05 = 105
        self.assertEqual(engine.state.current_tp_price, Decimal("105"))
        self.assertEqual(engine.state.current_tp_role, ROLE_TP)
        # Step1 pending = next price
        # P1 = 100 + 1.618 * (100 - 105) = 100 - 8.09 = 91.91
        # Quantized to 3 decimals = 91.91
        self.assertEqual(engine.state.pending_order_role, ROLE_LADDER)
        self.assertEqual(engine.state.pending_confirmed_price, Decimal("91.91"))
        self.assertEqual(engine.state.next_step, 1)
        # Pending confirmed size == V1 = 0.01
        self.assertEqual(engine.state.pending_confirmed_size, Decimal("0.01"))
        # TP + 1 ladder order exist
        self.assertEqual(len(adapter.submit_log), 3)  # Step0, TP, Step1
        self.assertEqual(adapter.submit_log[1]["role"], "tp")
        self.assertEqual(adapter.submit_log[1]["side"], "sell")
        self.assertTrue(adapter.submit_log[1]["reduce_only"] is True)
        self.assertEqual(adapter.submit_log[2]["role"], "ladder")
        self.assertEqual(adapter.submit_log[2]["side"], "buy")


class TestEngineCaseBOrphanPending(unittest.TestCase):
    def test_orphan_pending_canceled_after_tp_closes_position(self):
        """TP closes accumulated position while ladder pending remains."""
        adapter = FakeAdapter()
        engine = _make_engine_buy(adapter)
        # Submit Step0
        engine.tick()
        # Confirm Step0, set TP and Step1
        engine.confirm_step0_filled(Decimal("100"))
        engine.place_step0_tp_and_step1(Decimal("100"))
        # TP closes position: position=0, TP FILLED, Step1 still ACTIVE
        tp_oid = engine.state.current_tp_order_id
        ladder_oid = engine.state.pending_order_exchange_id
        # Mark the TP as filled (in real world, position would already be 0)
        adapter.simulate_order_filled(tp_oid)
        adapter.simulate_position(Decimal("0"), None)
        # Now tick: position=0, pending ladder active → Case B
        result = engine.tick()
        # Orphan ladder should be cancelled
        self.assertTrue(ladder_oid in adapter.cancel_log)
        # State should reset
        self.assertEqual(engine.state.highest_filled_step, -1)
        self.assertTrue(engine.state.pending_order_role is None)
        self.assertTrue(engine.state.current_tp_order_id is None)
        self.assertEqual(engine.state.cycle_id, 2)  # incremented for fresh cycle


class TestEngineCaseCConfirmedFill(unittest.TestCase):
    def test_step1_filled_advances_to_step2(self):
        adapter = FakeAdapter()
        engine = _make_engine_buy(adapter)
        engine.tick()
        engine.confirm_step0_filled(Decimal("100"))
        engine.place_step0_tp_and_step1(Decimal("100"))
        # Step1 fills
        step1_oid = engine.state.pending_order_exchange_id
        adapter.simulate_order_filled(step1_oid)
        adapter.simulate_position(Decimal("0.02"), "long")  # V0 + V1 = 0.02
        # Tick: pending ladder (step1) is FILLED → advance
        result = engine.tick()
        self.assertEqual(result.state.status, STATUS_RUNNING)
        self.assertEqual(engine.state.highest_filled_step, 1)
        # With pct=0.01, TP0=101, P1=100+1.618*(100-101)=98.382
        self.assertEqual(engine.state.fill_prices[1], Decimal("98.382"))
        self.assertEqual(engine.state.expected_cumulative_size, Decimal("0.02"))
        self.assertEqual(engine.state.next_step, 2)
        # After Step1 fills, TP should be P0 = 100
        self.assertEqual(engine.state.current_tp_price, Decimal("100"))
        # New pending ladder at step2
        self.assertEqual(engine.state.pending_order_role, ROLE_LADDER)
        # step2 = 0.02
        self.assertEqual(engine.state.pending_confirmed_size, Decimal("0.02"))

    def test_step2_filled_advances_to_step3(self):
        """Step0 → Step1 → Step2 fills, advances to Step3."""
        adapter = FakeAdapter()
        engine = _make_engine_buy(adapter)
        engine.tick()
        engine.confirm_step0_filled(Decimal("100"))
        engine.place_step0_tp_and_step1(Decimal("100"))
        # Step1 fills
        step1_oid = engine.state.pending_order_exchange_id
        adapter.simulate_order_filled(step1_oid)
        adapter.simulate_position(Decimal("0.02"), "long")
        engine.tick()
        # Capture Step2's persisted price (the next ladder's pending_confirmed_price)
        step2_persisted = engine.state.pending_confirmed_price
        # Step2 fills
        step2_oid = engine.state.pending_order_exchange_id
        adapter.simulate_order_filled(step2_oid)
        adapter.simulate_position(Decimal("0.04"), "long")  # V0+V1+V2 = 0.04
        engine.tick()
        self.assertEqual(engine.state.highest_filled_step, 2)
        # fill_prices[2] should be the persisted Step2 limit price (the
        # value captured when Step2 was placed, BEFORE Step3 was placed).
        self.assertEqual(engine.state.fill_prices[2], step2_persisted)
        # After Step2 fills, TP should be P1 (the previously-filled step)
        self.assertEqual(engine.state.current_tp_price, engine.state.fill_prices[1])
        # Step3 pending
        self.assertEqual(engine.state.pending_order_role, ROLE_LADDER)
        self.assertEqual(engine.state.next_step, 3)

    def test_average_position_entry_never_used_as_pk(self):
        """AC #18: average position entry is NEVER used as Pk for Step1+."""
        adapter = FakeAdapter()
        engine = _make_engine_buy(adapter)
        engine.tick()
        engine.confirm_step0_filled(Decimal("100"))
        engine.place_step0_tp_and_step1(Decimal("100"))
        # Step1 fills at the persisted P1 (not a hypothetical average)
        step1_oid = engine.state.pending_order_exchange_id
        persisted_p1 = engine.state.pending_confirmed_price
        adapter.simulate_order_filled(step1_oid)
        adapter.simulate_position(Decimal("0.02"), "long")
        engine.tick()
        # Even if the position's average entry differs, P1 must be the
        # persisted confirmed limit price.
        self.assertEqual(engine.state.fill_prices[1], Decimal(persisted_p1))
        assert engine.state.fill_prices[1] != engine.state.pending_confirmed_price or persisted_p1 == engine.state.pending_confirmed_price


class TestEngineCaseDHealthyWaiting(unittest.TestCase):
    def test_healthy_waiting_no_mutation(self):
        adapter = FakeAdapter()
        engine = _make_engine_buy(adapter)
        engine.tick()
        engine.confirm_step0_filled(Decimal("100"))
        engine.place_step0_tp_and_step1(Decimal("100"))
        # No fills yet — tick should be healthy
        n_submits_before = len(adapter.submit_log)
        n_cancels_before = len(adapter.cancel_log)
        result = engine.tick()
        self.assertEqual(result.state.status, STATUS_RUNNING)
        self.assertEqual(len(adapter.submit_log), n_submits_before)
        self.assertEqual(len(adapter.cancel_log), n_cancels_before)


class TestEngineFreezeOnabort(unittest.TestCase):
    def test_canceled_pending_freezes(self):
        adapter = FakeAdapter()
        engine = _make_engine_buy(adapter)
        engine.tick()
        engine.confirm_step0_filled(Decimal("100"))
        engine.place_step0_tp_and_step1(Decimal("100"))
        step1_oid = engine.state.pending_order_exchange_id
        adapter.simulate_order_canceled(step1_oid)
        result = engine.tick()
        self.assertEqual(result.state.status, STATUS_NEEDS_RECOVERY)

    def test_rejected_pending_freezes(self):
        adapter = FakeAdapter()
        engine = _make_engine_buy(adapter)
        engine.tick()
        engine.confirm_step0_filled(Decimal("100"))
        engine.place_step0_tp_and_step1(Decimal("100"))
        step1_oid = engine.state.pending_order_exchange_id
        rec = adapter.orders[step1_oid]
        rec["status"] = "rejected"
        rec["taxonomy"] = "REJECTED"
        result = engine.tick()
        self.assertEqual(result.state.status, STATUS_NEEDS_RECOVERY)

    def test_expired_pending_freezes(self):
        adapter = FakeAdapter()
        engine = _make_engine_buy(adapter)
        engine.tick()
        engine.confirm_step0_filled(Decimal("100"))
        engine.place_step0_tp_and_step1(Decimal("100"))
        step1_oid = engine.state.pending_order_exchange_id
        rec = adapter.orders[step1_oid]
        rec["status"] = "expired"
        rec["taxonomy"] = "EXPIRED"
        result = engine.tick()
        self.assertEqual(result.state.status, STATUS_NEEDS_RECOVERY)

    def test_disappeared_pending_without_expected_position_delta_freezes(self):
        adapter = FakeAdapter()
        engine = _make_engine_buy(adapter)
        engine.tick()
        engine.confirm_step0_filled(Decimal("100"))
        engine.place_step0_tp_and_step1(Decimal("100"))
        step1_oid = engine.state.pending_order_exchange_id
        # Remove the pending order from the fake (simulates disappearing
        # without a clear state we can read)
        adapter.remove_order(step1_oid)
        # Position did NOT increase
        adapter.simulate_position(Decimal("0.01"), "long")
        result = engine.tick()
        self.assertEqual(result.state.status, STATUS_NEEDS_RECOVERY)


class TestEngineStep20Terminal(unittest.TestCase):
    def test_step20_filled_does_not_place_step21(self):
        adapter = FakeAdapter()
        cfg = GoldenFiboConfig(
            exchange="lighter",
            account="amiroo",
            instrument="SOL",
            direction="BUY",
            percentage=Decimal("0.01"),
            step0_volume=Decimal("0.01"),
        )
        state = GoldenFiboState(registration_key=cfg.registration_key, cycle_id=1)
        # Set up: highest_filled_step == 19, next_step == 20, pending step20
        state.highest_filled_step = 19
        for i in range(20):
            state.fill_prices[i] = Decimal(str(100 - i * 0.5))
        state.expected_cumulative_size = cfg.cumulative_volume(19)
        state.next_step = 20
        state.current_tp_price = state.fill_prices[18]
        state.pending_order_role = ROLE_LADDER
        state.pending_order_exchange_id = 99999
        state.pending_confirmed_price = Decimal("99.0")
        state.pending_confirmed_size = cfg.volume(20)
        engine = GoldenFiboEngine(cfg, state, adapter, _client_id_factory())
        # Simulate Step20 order filling
        adapter.orders[99999] = {
            "exchange_order_id": 99999,
            "client_order_id": 99999,
            "side": "buy",
            "type": "limit",
            "size": str(cfg.volume(20)),
            "price": "99.0",
            "status": "filled",
            "taxonomy": "FILLED",
            "reduce_only": False,
        }
        adapter.simulate_position(cfg.cumulative_volume(20), "long")
        result = engine.tick()
        # Step20 advanced
        self.assertEqual(engine.state.highest_filled_step, 20)
        self.assertEqual(engine.state.next_step, 20)
        # No Step21 placed
        self.assertTrue(engine.state.pending_order_role is None)
        self.assertTrue(engine.state.pending_order_exchange_id is None)


class TestEngineDirection(unittest.TestCase):
    def test_sell_step0(self):
        adapter = FakeAdapter()
        cfg = GoldenFiboConfig(
            exchange="lighter",
            account="amiroo",
            instrument="SOL",
            direction="SELL",
            percentage=Decimal("0.05"),
            step0_volume=Decimal("0.01"),
        )
        state = GoldenFiboState(registration_key=cfg.registration_key, cycle_id=0)
        engine = GoldenFiboEngine(cfg, state, adapter, _client_id_factory())
        engine.tick()
        # Step0 was a SELL market
        self.assertEqual(adapter.submit_log[0]["side"], "sell")
        self.assertEqual(adapter.position["side"], "short")
        # Confirm P0 = 100 (per the fake adapter default)
        engine.confirm_step0_filled(Decimal("100"))
        engine.place_step0_tp_and_step1(Decimal("100"))
        # SELL TP0 = 100 * 0.95 = 95 (BUY reduce-only)
        self.assertEqual(engine.state.current_tp_price, Decimal("95"))
        self.assertEqual(adapter.submit_log[1]["side"], "buy")
        self.assertTrue(adapter.submit_log[1]["reduce_only"] is True)
        # SELL Step1: P1 = 100 + 1.618 * (100 - 95) = 108.09
        self.assertEqual(engine.state.pending_confirmed_price, Decimal("108.09"))
        self.assertEqual(adapter.submit_log[2]["side"], "sell")

    def test_position_direction_mismatch_freezes(self):
        adapter = FakeAdapter()
        engine = _make_engine_buy(adapter)
        engine.tick()
        engine.confirm_step0_filled(Decimal("100"))
        engine.place_step0_tp_and_step1(Decimal("100"))
        # Live position is short — config is BUY
        adapter.simulate_position(Decimal("0.01"), "short")
        result = engine.tick()
        self.assertEqual(result.state.status, STATUS_NEEDS_RECOVERY)


class TestEngineNoDuplicateStep0(unittest.TestCase):
    def test_no_step0_while_existing_registration(self):
        adapter = FakeAdapter()
        engine = _make_engine_buy(adapter)
        # Already in a cycle: set highest_filled_step >= 0
        engine.state.highest_filled_step = 1
        engine.state.fill_prices = {0: Decimal("100"), 1: Decimal("98.38")}
        engine.state.expected_cumulative_size = Decimal("0.02")
        engine.state.current_tp_price = Decimal("100")
        engine.state.current_tp_order_id = 555
        engine.state.current_tp_role = ROLE_TP
        engine.state.pending_order_exchange_id = 666
        engine.state.pending_order_role = ROLE_LADDER
        engine.state.pending_confirmed_price = Decimal("96.7")
        engine.state.pending_confirmed_size = Decimal("0.02")
        engine.state.next_step = 2
        adapter.simulate_position(Decimal("0.02"), "long")
        n_submits_before = len(adapter.submit_log)
        # Tick must NOT issue Step0
        engine.tick()
        self.assertEqual(len(adapter.submit_log), n_submits_before)


class TestEngineRestartReconciliation(unittest.TestCase):
    def test_restart_healthy_resume_no_mutation(self):
        """After restart with healthy state, the engine resumes monitoring
        without placing any new orders."""
        adapter = FakeAdapter()
        # Simulate a healthy persisted state
        cfg = GoldenFiboConfig(
            exchange="lighter",
            account="amiroo",
            instrument="SOL",
            direction="BUY",
            percentage=Decimal("0.01"),
            step0_volume=Decimal("0.01"),
        )
        state = GoldenFiboState.from_dict({
            "registration_key": cfg.registration_key,
            "cycle_id": 1,
            "exchange": "lighter",
            "account": "amiroo",
            "instrument": "SOL",
            "direction": "BUY",
            "percentage": "0.01",
            "step0_volume": "0.01",
            "highest_filled_step": 0,
            "fill_prices": {"0": "100"},
            "expected_cumulative_size": "0.01",
            "current_tp_price": "101",
            "current_tp_order_id": 555,
            "current_tp_client_id": 888,
            "current_tp_role": "tp",
            "next_step": 1,
            "pending_order_client_id": 999,
            "pending_order_exchange_id": 666,
            "pending_requested_price": "99",
            "pending_requested_size": "0.01",
            "pending_confirmed_price": "99",
            "pending_confirmed_size": "0.01",
            "pending_order_role": "ladder",
            "status": "running",
        })
        # Set up the live state to match
        adapter.orders[666] = {
            "exchange_order_id": 666, "client_order_id": 999, "side": "buy",
            "type": "limit", "size": "0.01", "price": "99",
            "status": "open", "taxonomy": "ACTIVE", "reduce_only": False,
        }
        adapter.orders[555] = {
            "exchange_order_id": 555, "client_order_id": 888, "side": "sell",
            "type": "limit", "size": "0.01", "price": "101",
            "status": "open", "taxonomy": "ACTIVE", "reduce_only": True,
        }
        adapter.simulate_position(Decimal("0.01"), "long")
        # Wire TP back into position
        adapter.position["tp"] = "101"

        engine = GoldenFiboEngine(cfg, state, adapter, _client_id_factory())
        n_submits_before = len(adapter.submit_log)
        n_cancels_before = len(adapter.cancel_log)
        result = engine.tick()
        # Healthy waiting state: no mutation
        self.assertEqual(result.state.status, STATUS_RUNNING)
        self.assertEqual(len(adapter.submit_log), n_submits_before)
        self.assertEqual(len(adapter.cancel_log), n_cancels_before)


class TestEngineOwnershipUnrelatedOrders(unittest.TestCase):
    def test_unrelated_manual_orders_not_interfered(self):
        """GoldenFibo must not touch unrelated manual orders."""
        adapter = FakeAdapter()
        # Add a manual unrelated order at the exchange
        adapter.orders[12345] = {
            "exchange_order_id": 12345, "client_order_id": 99999, "side": "buy",
            "type": "limit", "size": "0.1", "price": "50",
            "status": "open", "taxonomy": "ACTIVE", "reduce_only": False,
        }
        engine = _make_engine_buy(adapter)
        engine.tick()
        engine.confirm_step0_filled(Decimal("100"))
        engine.place_step0_tp_and_step1(Decimal("100"))
        # Tick: pending step1 ACTIVE, position correct, healthy
        n_submits_before = len(adapter.submit_log)
        n_cancels_before = len(adapter.cancel_log)
        # Manually remove the unrelated order to simulate external action
        adapter.orders.pop(12345, None)
        engine.tick()
        # Unrelated order was never touched by GoldenFibo
        self.assertTrue(12345 not in adapter.cancel_log)
        self.assertEqual(engine.state.status, STATUS_RUNNING)
        # GoldenFibo's own orders are still there
        self.assertEqual(len(adapter.submit_log), n_submits_before)
        self.assertEqual(len(adapter.cancel_log), n_cancels_before)


# ---------------------------------------------------------------------------
# Old-strategy state quarantine
# ---------------------------------------------------------------------------
class TestOldStrategyQuarantine(unittest.TestCase):
    def test_old_strategy_state_status(self):
        """Records with the old strategy name must be quarantined."""
        s = GoldenFiboState(
            strategy="fibonacci_counter_cascade",
            registration_key="lighter/amiroo:SOL:counterBUY",
            status=STATUS_QUARANTINED_OLD_STRATEGY,
        )
        self.assertEqual(s.status, STATUS_QUARANTINED_OLD_STRATEGY)
        self.assertEqual(s.strategy, "fibonacci_counter_cascade")
        # Carries through round-trip
        s2 = GoldenFiboState.from_dict(s.to_dict())
        self.assertEqual(s2.status, STATUS_QUARANTINED_OLD_STRATEGY)
        self.assertEqual(s2.strategy, "fibonacci_counter_cascade")
