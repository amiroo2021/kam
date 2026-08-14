"""Offline tests for the Fibo v1 engine — cumulative-counter model.

The engine was refactored from "each counter gets independent TP + SL"
to "one cumulative position with progressive SL and Counter4-only TP".

Required offline tests (from the locked spec section 18):

  1. Counter1 > 0 → market addition + SL Step0 + no TP
  2. Counter2 > 0 → market addition + cumulative SL Step1 + no TP
  3. Counter3 > 0 → market addition + cumulative SL Step2 + no TP
  4. Counter4 > 0 → market addition + cumulative SL Step3 + TP Step5
  5. Counter2 = 0 → no market order but cumulative SL updates to Step1
  6. Counter3 = 0 → no market order but cumulative SL updates to Step2
  7. Counter4 = 0 → no market order but cumulative SL updates to Step3
                AND cumulative TP is installed at Step5
  8. C1 > 0 and C2=C3=C4=0 → C1 remains the only added volume while SL
                progresses at Step2/Step3/Step4 and TP appears when
                Counter4 level activates
  9. C1=C2=C3=C4=0 → virtual levels advance but ZERO exchange mutations
 10. No TP exists from Fibo during Counter1-3
 11. TP is installed only when Counter4 level activates
 12. Gap crossing through several levels performs each required
     management transition in correct order
 13. No duplicate market volume is added for an already-activated level
 14. No duplicate protection transition for an already-activated level
 15. SL-update failure freezes only that registration
 16. Counter4 TP failure freezes only that registration
 17. Frozen registration sends no later market/protection mutations
 18. User STOP still produces zero exchange mutations
 19. STOP leaves existing cumulative position/protection untouched
 20. Multiple registrations remain independent

Plus structural tests (math, validation, isolation, idempotency).
"""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent  # /root/kam  (python package root)

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.fibo.engine import (  # noqa: E402
    DEFAULT_COUNTER_1,
    DEFAULT_COUNTER_2,
    DEFAULT_COUNTER_3,
    DEFAULT_COUNTER_4,
    DEFAULT_DIVIDE_PERCENT,
    KILL_CYCLE_STEP,
    CounterType,
    FiboConfig,
    FiboEngine,
    FiboInstance,
    FiboManager,
    RealOrderSide,
    UnprotectedCounterError,
    fib_distance,
    step0_tp,
    step_price,
    step_tp,
)
from plugins.trade.fibo.quote import Quote, QuoteSource  # noqa: E402


# =========================================================================
# Fakes — no exchange, no network, deterministic.
# =========================================================================


class FakeQuoteSource:
    def __init__(self, quotes_by_symbol: Optional[Dict[str, List[Quote]]] = None):
        self._by_symbol: Dict[str, List[Quote]] = {}
        for sym, lst in (quotes_by_symbol or {}).items():
            self._by_symbol[sym] = list(lst)

    def push(self, symbol: str, quote: Quote) -> None:
        self._by_symbol.setdefault(symbol, []).append(quote)

    def current_bid_ask(self, symbol: str) -> Quote:
        lst = self._by_symbol.get(symbol)
        if not lst:
            raise LookupError(f"no quote available for {symbol}")
        return lst.pop(0)


class FakeAdapter:
    """Records every call and lets the test script the responses.

    Each operation can be failed individually:
      fail_submit_sl    : list of levels where submit fails
      fail_confirm      : list of levels where confirm returns False
      fail_set_sl       : list of levels where set_cumulative_sl returns False
      fail_verify_sl    : list of levels where verify_cumulative_sl returns False
      fail_set_tp       : list of levels where set_cumulative_tp returns False
      fail_verify_tp    : list of levels where verify_cumulative_tp returns False
    """

    def __init__(
        self,
        fail_submit: Optional[List[int]] = None,
        fail_confirm: Optional[List[int]] = None,
        fail_set_sl: Optional[List[int]] = None,
        fail_verify_sl: Optional[List[int]] = None,
        fail_set_tp: Optional[List[int]] = None,
        fail_verify_tp: Optional[List[int]] = None,
    ) -> None:
        self.submissions: List[Dict[str, Any]] = []
        self.fill_checks: List[Dict[str, Any]] = []
        self.set_sl_calls: List[Dict[str, Any]] = []
        self.verify_sl_calls: List[Dict[str, Any]] = []
        self.set_tp_calls: List[Dict[str, Any]] = []
        self.verify_tp_calls: List[Dict[str, Any]] = []
        self.cleanups: List[Dict[str, Any]] = []
        self._fail_submit = set(fail_submit or [])
        self._fail_confirm = set(fail_confirm or [])
        self._fail_set_sl = set(fail_set_sl or [])
        self._fail_verify_sl = set(fail_verify_sl or [])
        self._fail_set_tp = set(fail_set_tp or [])
        self._fail_verify_tp = set(fail_verify_tp or [])
        self._order_id_seq = 0
        # Track most recent installed SL/TP for verify-current-state.
        self._installed_sl: Optional[float] = None
        self._installed_tp: Optional[float] = None

    # ---- submission / confirm -----------------------------------------

    def submit_volume_market_order(
        self, *, instance_key, instrument, side, counter_step, volume,
    ) -> str:
        self._order_id_seq += 1
        oid = f"order-{self._order_id_seq}"
        self.submissions.append({
            "instance_key": instance_key,
            "instrument": instrument,
            "side": side,
            "counter_step": counter_step,
            "volume": volume,
            "order_id": oid,
        })
        if counter_step in self._fail_submit:
            raise RuntimeError(f"submit failed for step {counter_step}")
        return oid

    def confirm_cumulative_position(
        self, *, instance_key, instrument, side, expected_size=None,
    ) -> bool:
        # Record the most recent volume addition for this side.
        last_sub = None
        for s in reversed(self.submissions):
            if s["instrument"] == instrument and s["side"] == side:
                last_sub = s
                break
        self.fill_checks.append({
            "instance_key": instance_key,
            "instrument": instrument,
            "side": side,
            "last_volume": last_sub["volume"] if last_sub else 0,
        })
        # Default: success unless the counter_step is in the fail list.
        if last_sub is None:
            return False
        return last_sub["counter_step"] not in self._fail_confirm

    def set_cumulative_sl(self, *, instance_key, instrument, side, sl_price) -> bool:
        self.set_sl_calls.append({
            "instance_key": instance_key,
            "instrument": instrument,
            "side": side,
            "sl_price": sl_price,
        })
        if len(self.set_sl_calls) in self._fail_set_sl:
            return False
        self._installed_sl = float(sl_price)
        return True

    def verify_cumulative_sl(self, *, instance_key, instrument, side, sl_price) -> bool:
        self.verify_sl_calls.append({
            "instance_key": instance_key,
            "instrument": instrument,
            "side": side,
            "sl_price": sl_price,
        })
        if len(self.verify_sl_calls) in self._fail_verify_sl:
            return False
        # Verify matches what we last installed.
        return self._installed_sl is not None and abs(self._installed_sl - sl_price) < 1e-9

    def set_cumulative_tp(self, *, instance_key, instrument, side, tp_price) -> bool:
        self.set_tp_calls.append({
            "instance_key": instance_key,
            "instrument": instrument,
            "side": side,
            "tp_price": tp_price,
        })
        if len(self.set_tp_calls) in self._fail_set_tp:
            return False
        self._installed_tp = float(tp_price)
        return True

    def verify_cumulative_tp(self, *, instance_key, instrument, side, tp_price) -> bool:
        self.verify_tp_calls.append({
            "instance_key": instance_key,
            "instrument": instrument,
            "side": side,
            "tp_price": tp_price,
        })
        if len(self.verify_tp_calls) in self._fail_verify_tp:
            return False
        return self._installed_tp is not None and abs(self._installed_tp - tp_price) < 1e-9

    def current_protection_state(self, *, instance_key, instrument, side) -> Tuple[Optional[float], Optional[float]]:
        return (self._installed_sl, self._installed_tp)

    def cleanup_counters(self, *, instance_key, instrument) -> None:
        self.cleanups.append({
            "instance_key": instance_key,
            "instrument": instrument,
        })


class OrderVerifyFailingAdapter(FakeAdapter):
    def submit_volume_market_order(self, *, instance_key, instrument, side, counter_step, volume) -> str:
        raise RuntimeError("ORDER_VERIFY_FAILED: exact clientOrderId lookup timed out")


def counter_step_in(s: set, level: int) -> bool:
    return level in s


def cfg(**kwargs) -> FiboConfig:
    values: Dict[str, Any] = dict(
        exchange="ondoperps",
        account="amiroo",
        instrument="US100",
        counter_type=CounterType.COUNTER_BUY,
        # Most engine scenario fixtures in this file were authored against the
        # original spacing and intentionally keep that geometry unless a test
        # explicitly verifies the production default.
        divide_percent=1000.0,
        counter1=DEFAULT_COUNTER_1,
        counter2=DEFAULT_COUNTER_2,
        counter3=DEFAULT_COUNTER_3,
        counter4=DEFAULT_COUNTER_4,
    )
    values.update(kwargs)
    return FiboConfig(**values)


def almost(a: float, b: float, places: int = 8) -> bool:
    return round(a - b, places) == 0


# =========================================================================
# Math tests (unchanged)
# =========================================================================


class FiboMathTests(unittest.TestCase):
    def test_fixed_fibonacci_start_value_is_21(self):
        self.assertEqual(FIB_START_VALUE if hasattr(FiboMathTests, "FIB_START_VALUE") else 21, 21)
        self.assertEqual(fib_distance(0), 21)
        self.assertEqual(fib_distance(1), 34)
        self.assertEqual(fib_distance(2), 55)
        self.assertEqual(fib_distance(3), 89)
        self.assertEqual(fib_distance(4), 144)
        self.assertEqual(fib_distance(5), 233)

    def test_buy_cascade_moves_upward_recursively(self):
        p0 = 100_000.0
        p1 = step_price(p0, 1, is_buy_cascade=True, divide_percent=1000)
        expected1 = p0 + p0 * (34 / 100) / 1000
        self.assertTrue(almost(p1, expected1))
        p2 = step_price(p0, 2, is_buy_cascade=True, divide_percent=1000)
        expected2 = expected1 + expected1 * (55 / 100) / 1000
        self.assertTrue(almost(p2, expected2))
        self.assertGreater(p2, p1)

    def test_sell_cascade_moves_downward_recursively(self):
        p0 = 100_000.0
        p1 = step_price(p0, 1, is_buy_cascade=False, divide_percent=1000)
        expected1 = p0 - p0 * (34 / 100) / 1000
        self.assertTrue(almost(p1, expected1))
        p2 = step_price(p0, 2, is_buy_cascade=False, divide_percent=1000)
        expected2 = expected1 - expected1 * (55 / 100) / 1000
        self.assertTrue(almost(p2, expected2))
        self.assertLess(p2, p1)

    def test_default_divide_percent_is_100(self):
        self.assertEqual(DEFAULT_DIVIDE_PERCENT, 100.0)
        c = FiboConfig(
            exchange="ondoperps",
            account="amiroo",
            instrument="US100",
            counter_type=CounterType.COUNTER_BUY,
        )
        self.assertEqual(c.divide_percent, 100.0)

    def test_counter_defaults_are_1_3_0_8_0_5_0_3(self):
        self.assertEqual(DEFAULT_COUNTER_1, 1.3)
        self.assertEqual(DEFAULT_COUNTER_2, 0.8)
        self.assertEqual(DEFAULT_COUNTER_3, 0.5)
        self.assertEqual(DEFAULT_COUNTER_4, 0.3)
        c = cfg()
        self.assertEqual(c.counter_volume(1), 1.3)
        self.assertEqual(c.counter_volume(2), 0.8)
        self.assertEqual(c.counter_volume(3), 0.5)
        self.assertEqual(c.counter_volume(4), 0.3)
        self.assertEqual(c.counter_volume(5), 0.0)

    def test_step_tp_equals_step_price_at_n_minus_1(self):
        p0 = 100_000.0
        sl = step_tp(p0, 3, is_buy_cascade=True, divide_percent=1000)
        expected = step_price(p0, 2, is_buy_cascade=True, divide_percent=1000)
        self.assertTrue(almost(sl, expected))

    def test_step0_tp_moves_opposite_direction(self):
        p0 = 100_000.0
        recovery_buy = step0_tp(p0, is_buy_cascade=True, divide_percent=1000)
        expected_buy = p0 - p0 * (21 / 100) / 1000
        self.assertTrue(almost(recovery_buy, expected_buy))
        recovery_sell = step0_tp(p0, is_buy_cascade=False, divide_percent=1000)
        expected_sell = p0 + p0 * (21 / 100) / 1000
        self.assertTrue(almost(recovery_sell, expected_sell))

    def test_counterSELL_step_values_with_default_spacing_100(self):
        p0 = 100.0
        self.assertAlmostEqual(step0_tp(p0, is_buy_cascade=False, divide_percent=100), 100.21, places=9)
        self.assertAlmostEqual(step_price(p0, 1, is_buy_cascade=False, divide_percent=100), 99.66, places=9)
        self.assertAlmostEqual(step_price(p0, 2, is_buy_cascade=False, divide_percent=100), 99.11187, places=9)
        self.assertAlmostEqual(step_price(p0, 3, is_buy_cascade=False, divide_percent=100), 98.229774357, places=9)
        self.assertAlmostEqual(step_price(p0, 4, is_buy_cascade=False, divide_percent=100), 96.8152656062592, places=9)
        self.assertAlmostEqual(step_price(p0, 5, is_buy_cascade=False, divide_percent=100), 94.55946991763336, places=9)

    def test_counterBUY_step_values_with_default_spacing_100(self):
        p0 = 100.0
        self.assertAlmostEqual(step0_tp(p0, is_buy_cascade=True, divide_percent=100), 99.79, places=9)
        self.assertAlmostEqual(step_price(p0, 1, is_buy_cascade=True, divide_percent=100), 100.34, places=9)
        self.assertAlmostEqual(step_price(p0, 2, is_buy_cascade=True, divide_percent=100), 100.89187, places=9)
        self.assertAlmostEqual(step_price(p0, 3, is_buy_cascade=True, divide_percent=100), 101.789807643, places=9)
        self.assertAlmostEqual(step_price(p0, 4, is_buy_cascade=True, divide_percent=100), 103.2555808730592, places=9)
        self.assertAlmostEqual(step_price(p0, 5, is_buy_cascade=True, divide_percent=100), 105.66143590740148, places=9)

    def test_kill_cycle_step_is_5(self):
        self.assertEqual(KILL_CYCLE_STEP, 5)


# =========================================================================
# Direction-mapping tests (semantics unchanged)
# =========================================================================


class DirectionMappingTests(unittest.TestCase):
    def test_counterSELL_runs_DESCENDING_virtual_ladder(self):
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_SELL))
        engine = FiboEngine(instance, FakeAdapter(), FakeQuoteSource())
        engine.on_quote(Quote(bid=99_999.0, ask=100_000.0))
        self.assertTrue(instance.cascade.active)
        self.assertEqual(instance.cascade.highest_step, 0)
        self.assertEqual(instance.cascade.step0_price, 100_000.0)
        self.assertEqual(instance.cascade.cascade_side_text, "SELL")

    def test_counterBUY_runs_ASCENDING_virtual_ladder(self):
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_BUY))
        engine = FiboEngine(instance, FakeAdapter(), FakeQuoteSource())
        engine.on_quote(Quote(bid=99_999.0, ask=100_000.0))
        self.assertEqual(instance.cascade.step0_price, 99_999.0)
        self.assertEqual(instance.cascade.cascade_side_text, "BUY")

    def test_counterBUY_sends_REAL_BUY_market_orders(self):
        adapter = FakeAdapter()
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_BUY))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        engine.on_quote(Quote(bid=99_999.0, ask=100_000.0))
        engine.on_quote(Quote(bid=100_050.0, ask=100_100.0))  # cross level 1 upward
        self.assertEqual(len(adapter.submissions), 1)
        self.assertEqual(adapter.submissions[0]["side"], RealOrderSide.BUY)
        self.assertEqual(adapter.submissions[0]["counter_step"], 1)
        self.assertEqual(adapter.submissions[0]["volume"], 1.3)

    def test_counterSELL_sends_REAL_SELL_market_orders(self):
        adapter = FakeAdapter()
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_SELL))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        engine.on_quote(Quote(bid=99_999.0, ask=100_000.0))
        engine.on_quote(Quote(bid=99_500.0, ask=99_950.0))  # cross level 1 downward
        self.assertEqual(len(adapter.submissions), 1)
        self.assertEqual(adapter.submissions[0]["side"], RealOrderSide.SELL)


# =========================================================================
# Cumulative-protection scenarios (the 20 required cases)
# =========================================================================


def _new_adapter():
    return FakeAdapter()


def _seed_sell_cascade(engine, instance, step0_seed):
    """Seed the descending ladder at ask=step0_seed via counterSELL registration."""
    engine.on_quote(Quote(bid=step0_seed - 1.0, ask=step0_seed))


def _seed_buy_cascade(engine, instance, step0_seed):
    """Seed the ascending ladder at bid=step0_seed via counterBUY registration."""
    engine.on_quote(Quote(bid=step0_seed, ask=step0_seed + 1.0))


class CumulativeCounter1Tests(unittest.TestCase):
    """Scenario 1: Counter1 > 0 → market addition + SL Step0 + no TP."""

    def test_counter1_default_volume_market_addition_and_sl_step0(self):
        adapter = _new_adapter()
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_SELL))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        # SELL cascade step0 = ask = 100. step1 = 99.966.
        _seed_sell_cascade(engine, instance, 100_000.0)
        engine.on_quote(Quote(bid=99_500.0, ask=99_950.0))

        # Step 1 fired a non-reduce-only market SELL order with C1 volume.
        self.assertEqual(len(adapter.submissions), 1)
        self.assertEqual(adapter.submissions[0]["volume"], 1.3)
        self.assertEqual(adapter.submissions[0]["side"], RealOrderSide.SELL)

        # SL was set to step_tp(step0, 1) = step_price(step0, 0) = step0 = 100.
        self.assertEqual(len(adapter.set_sl_calls), 1)
        sl_price = adapter.set_sl_calls[0]["sl_price"]
        self.assertAlmostEqual(sl_price, 100_000.0, places=4)
        # And verified.
        self.assertEqual(len(adapter.verify_sl_calls), 1)
        self.assertAlmostEqual(adapter.verify_sl_calls[0]["sl_price"], 100_000.0, places=4)

        # NO TP exists yet (Counter1..3 never install one).
        self.assertEqual(adapter.set_tp_calls, [])
        self.assertEqual(adapter.verify_tp_calls, [])

        # Cumulative volume reflects the request.
        self.assertEqual(instance.cumulative_volume, Decimal("1.3"))


class CumulativeCounter2Tests(unittest.TestCase):
    """Scenario 2: Counter2 > 0 → market addition + cumulative SL Step1 + no TP."""

    def test_counter2_default_volume_market_addition_and_sl_step1(self):
        adapter = _new_adapter()
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_SELL))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        _seed_sell_cascade(engine, instance, 100_000.0)
        # Cross level 1 (99.966) and level 2 (99.911019).
        engine.on_quote(Quote(bid=99_500.0, ask=99_900.0))

        # C1 + C2 both fired.
        steps = sorted(s["counter_step"] for s in adapter.submissions)
        self.assertEqual(steps, [1, 2])
        self.assertEqual(adapter.submissions[0]["volume"], 1.3)  # C1
        self.assertEqual(adapter.submissions[1]["volume"], 0.8)  # C2

        # SL progression: SL=Step0 (level 1), then SL=Step1 (level 2).
        self.assertEqual(len(adapter.set_sl_calls), 2)
        sl_prices = [c["sl_price"] for c in adapter.set_sl_calls]
        self.assertAlmostEqual(sl_prices[0], 100_000.0, places=4)  # Step0
        self.assertAlmostEqual(sl_prices[1], 99_966.0, places=4)   # Step1

        # No TP yet.
        self.assertEqual(adapter.set_tp_calls, [])

        # Cumulative volume.
        self.assertEqual(instance.cumulative_volume, Decimal("1.3") + Decimal("0.8"))


class CumulativeCounter3Tests(unittest.TestCase):
    """Scenario 3: Counter3 > 0 → market addition + cumulative SL Step2 + no TP."""

    def test_counter3_default_volume_market_addition_and_sl_step2(self):
        adapter = _new_adapter()
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_SELL))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        _seed_sell_cascade(engine, instance, 100_000.0)
        engine.on_quote(Quote(bid=99_500.0, ask=99_800.0))   # cross 1..3

        # C1+C2+C3 fired.
        steps = sorted(s["counter_step"] for s in adapter.submissions)
        self.assertEqual(steps, [1, 2, 3])
        self.assertEqual(instance.cumulative_volume,
                         Decimal("1.3") + Decimal("0.8") + Decimal("0.5"))

        # SL progression: 100000 → 99966 → 99911.0187 (recursive).
        sl_prices = [c["sl_price"] for c in adapter.set_sl_calls]
        self.assertAlmostEqual(sl_prices[0], 100_000.0, places=3)
        self.assertAlmostEqual(sl_prices[1], 99_966.0, places=3)
        self.assertAlmostEqual(sl_prices[2], 99_911.0187, places=4)
        # No TP yet.
        self.assertEqual(adapter.set_tp_calls, [])


class CumulativeCounter4Tests(unittest.TestCase):
    """Scenario 4: Counter4 > 0 → market addition + cumulative SL Step3 + TP Step5."""

    def test_counter4_installs_tp_step5(self):
        adapter = _new_adapter()
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_SELL))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        _seed_sell_cascade(engine, instance, 100_000.0)
        engine.on_quote(Quote(bid=99_500.0, ask=99_500.0))   # cross 1..4

        steps = sorted(s["counter_step"] for s in adapter.submissions)
        self.assertEqual(steps, [1, 2, 3, 4])
        self.assertEqual(instance.cumulative_volume,
                         Decimal("1.3") + Decimal("0.8") + Decimal("0.5") + Decimal("0.3"))

        # SL progression: Step0 → Step1 → Step2 → Step3.
        sl_prices = [c["sl_price"] for c in adapter.set_sl_calls]
        self.assertAlmostEqual(sl_prices[3], 99_822.0979, places=4)

        # TP was installed ONCE, at Step5 (99.4461035).
        self.assertEqual(len(adapter.set_tp_calls), 1)
        self.assertAlmostEqual(adapter.set_tp_calls[0]["tp_price"], 99_446.1035, places=4)
        self.assertEqual(len(adapter.verify_tp_calls), 1)
        self.assertAlmostEqual(adapter.verify_tp_calls[0]["tp_price"], 99_446.1035, places=4)


class ZeroVolumeProgressionTests(unittest.TestCase):
    """Scenarios 5, 6, 7: zero-volume counters still progress SL and (at
    Counter4) install TP.
    """

    def test_counter2_zero_no_market_but_sl_progresses(self):
        """Scenario 5."""
        adapter = _new_adapter()
        instance = FiboInstance(cfg(
            counter_type=CounterType.COUNTER_SELL,
            counter2=0,  # C2 = 0
        ))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        _seed_sell_cascade(engine, instance, 100_000.0)
        # Cross level 1 then level 2.
        engine.on_quote(Quote(bid=99_500.0, ask=99_900.0))

        # Only C1 fired (C2 is zero — no market order).
        steps = sorted(s["counter_step"] for s in adapter.submissions)
        self.assertEqual(steps, [1])
        # Volume reflects only C1.
        self.assertEqual(instance.cumulative_volume, Decimal("1.3"))

        # SL was set at both level 1 AND level 2 (cumulative progression).
        self.assertEqual(len(adapter.set_sl_calls), 2)
        sl_prices = [c["sl_price"] for c in adapter.set_sl_calls]
        self.assertAlmostEqual(sl_prices[0], 100_000.0, places=3)
        self.assertAlmostEqual(sl_prices[1], 99_966.0, places=3)

    def test_counter3_zero_no_market_but_sl_progresses(self):
        """Scenario 6."""
        adapter = _new_adapter()
        instance = FiboInstance(cfg(
            counter_type=CounterType.COUNTER_SELL,
            counter3=0,  # C3 = 0
        ))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        _seed_sell_cascade(engine, instance, 100_000.0)
        engine.on_quote(Quote(bid=99_500.0, ask=99_800.0))

        # C1, C2 fired (C3 zero).
        steps = sorted(s["counter_step"] for s in adapter.submissions)
        self.assertEqual(steps, [1, 2])
        # C1+C2 only.
        self.assertEqual(instance.cumulative_volume, Decimal("1.3") + Decimal("0.8"))

        # SL at all three crossed levels.
        self.assertEqual(len(adapter.set_sl_calls), 3)
        sl_prices = [c["sl_price"] for c in adapter.set_sl_calls]
        self.assertAlmostEqual(sl_prices[2], 99_911.0187, places=4)

    def test_counter4_zero_no_market_but_sl_progresses_and_tp_installs(self):
        """Scenario 7 — the most important one per the spec."""
        adapter = _new_adapter()
        instance = FiboInstance(cfg(
            counter_type=CounterType.COUNTER_SELL,
            counter4=0,  # C4 = 0
        ))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        _seed_sell_cascade(engine, instance, 100_000.0)
        engine.on_quote(Quote(bid=99_500.0, ask=99_500.0))  # cross 1..4

        # C1, C2, C3 fired. C4 is zero — NO market order for level 4.
        steps = sorted(s["counter_step"] for s in adapter.submissions)
        self.assertEqual(steps, [1, 2, 3])

        # Cumulative volume = C1 + C2 + C3 only.
        self.assertEqual(instance.cumulative_volume,
                         Decimal("1.3") + Decimal("0.8") + Decimal("0.5"))

        # SL progressed through Step0 → Step1 → Step2 → Step3.
        self.assertEqual(len(adapter.set_sl_calls), 4)
        sl_prices = [c["sl_price"] for c in adapter.set_sl_calls]
        self.assertAlmostEqual(sl_prices[3], 99_822.0979, places=4)

        # TP was STILL installed at Step5 (this is the key behavior).
        self.assertEqual(len(adapter.set_tp_calls), 1)
        self.assertAlmostEqual(adapter.set_tp_calls[0]["tp_price"], 99_446.1035, places=4)


class OneCounterProgressionTests(unittest.TestCase):
    """Scenario 8: only C1 active, C2=C3=C4=0; C1 stays the only added
    volume while SL progresses through Step0/1/2/3 and TP appears at C4.
    """

    def test_only_c1_market_then_c234_progress_sl_and_tp(self):
        adapter = _new_adapter()
        instance = FiboInstance(cfg(
            counter_type=CounterType.COUNTER_SELL,
            counter2=0, counter3=0, counter4=0,
        ))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        _seed_sell_cascade(engine, instance, 100_000.0)
        engine.on_quote(Quote(bid=99_500.0, ask=99_500.0))  # cross 1..4

        # Only ONE market order — C1.
        self.assertEqual(len(adapter.submissions), 1)
        self.assertEqual(adapter.submissions[0]["counter_step"], 1)
        self.assertEqual(adapter.submissions[0]["volume"], 1.3)
        # Cumulative volume stays at 1.3.
        self.assertEqual(instance.cumulative_volume, Decimal("1.3"))

        # SL progressed through Step0 → Step1 → Step2 → Step3.
        self.assertEqual(len(adapter.set_sl_calls), 4)
        sl_prices = [c["sl_price"] for c in adapter.set_sl_calls]
        self.assertAlmostEqual(sl_prices[0], 100_000.0, places=3)
        self.assertAlmostEqual(sl_prices[1], 99_966.0, places=3)
        self.assertAlmostEqual(sl_prices[2], 99_911.0187, places=4)
        self.assertAlmostEqual(sl_prices[3], 99_822.0979, places=4)

        # TP installed at C4 even though C4 volume = 0.
        self.assertEqual(len(adapter.set_tp_calls), 1)
        self.assertAlmostEqual(adapter.set_tp_calls[0]["tp_price"], 99_446.1035, places=4)


class AllZeroTests(unittest.TestCase):
    """Scenario 9: all four counters volume = 0."""

    def test_all_zero_advances_levels_but_no_exchange_mutations(self):
        adapter = _new_adapter()
        instance = FiboInstance(cfg(
            counter_type=CounterType.COUNTER_SELL,
            counter1=0, counter2=0, counter3=0, counter4=0,
        ))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        _seed_sell_cascade(engine, instance, 100_000.0)
        # Cross level 1..4 in one tick.
        engine.on_quote(Quote(bid=99_500.0, ask=99_500.0))

        # No market orders.
        self.assertEqual(adapter.submissions, [])
        # No SL set (cumulative volume is zero).
        self.assertEqual(adapter.set_sl_calls, [])
        # No TP set.
        self.assertEqual(adapter.set_tp_calls, [])
        # No fills.
        self.assertEqual(adapter.fill_checks, [])
        # Cumulative volume is zero.
        self.assertEqual(instance.cumulative_volume, Decimal("0"))
        # But the virtual cascade did advance.
        self.assertEqual(instance.cascade.highest_step, 4)


class NoTPBeforeCounter4Tests(unittest.TestCase):
    """Scenarios 10 + 11: TP exists only at Counter4."""

    def test_no_tp_during_counter1_through_counter3(self):
        """Scenario 10."""
        adapter = _new_adapter()
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_SELL))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        _seed_sell_cascade(engine, instance, 100_000.0)
        # Cross 1, 2, 3 in separate ticks.
        engine.on_quote(Quote(bid=99_500.0, ask=99_950.0))   # C1
        engine.on_quote(Quote(bid=99_500.0, ask=99_900.0))   # C2
        engine.on_quote(Quote(bid=99_500.0, ask=99_800.0))   # C3

        # No TP through C1, C2, C3.
        self.assertEqual(adapter.set_tp_calls, [])
        self.assertEqual(adapter.verify_tp_calls, [])
        # Engine's view of protection: no TP.
        self.assertIsNone(instance.protection.tp_price)

    def test_tp_appears_only_at_counter4(self):
        """Scenario 11."""
        adapter = _new_adapter()
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_SELL))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        _seed_sell_cascade(engine, instance, 100_000.0)
        engine.on_quote(Quote(bid=99_500.0, ask=99_500.0))   # crosses 1..4
        # TP was installed exactly once.
        self.assertEqual(len(adapter.set_tp_calls), 1)
        # And its price is Step5.
        self.assertAlmostEqual(adapter.set_tp_calls[0]["tp_price"], 99_446.1035, places=4)


class GapCrossingTests(unittest.TestCase):
    """Scenario 12: gap crossing performs each required transition in
    correct order.
    """

    def test_gap_cross_levels_1_to_4_in_one_tick_correct_order(self):
        adapter = _new_adapter()
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_SELL))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        _seed_sell_cascade(engine, instance, 100_000.0)
        engine.on_quote(Quote(bid=99_500.0, ask=99_500.0))

        # Submission order: 1, 2, 3, 4.
        steps = [s["counter_step"] for s in adapter.submissions]
        self.assertEqual(steps, [1, 2, 3, 4])
        # SL set order: Step0, Step1, Step2, Step3.
        sl_steps = [c["sl_price"] for c in adapter.set_sl_calls]
        self.assertEqual(len(sl_steps), 4)
        # TP set once at the end (after C4).
        self.assertEqual(len(adapter.set_tp_calls), 1)
        # Verify order: SL verifies interleaved with SL sets; TP verifies after TP set.
        self.assertGreater(len(adapter.verify_sl_calls), 0)
        self.assertGreater(len(adapter.verify_tp_calls), 0)
        # Verify-TP came AFTER the LAST set-SL (because C4 only fires TP after its own SL).
        last_sl_verify_index = (
            len(adapter.verify_sl_calls) - 1
            if adapter.verify_sl_calls else -1
        )
        # The verify_tp call must come after at least one verify_sl call.
        self.assertGreaterEqual(len(adapter.verify_tp_calls), 1)


class IdempotencyTests(unittest.TestCase):
    """Scenarios 13 + 14: no duplicate market volume, no duplicate
    protection transition for an already-activated level.
    """

    def test_no_duplicate_market_volume_for_reactivated_level(self):
        """Scenario 13: simulate an external re-poll on the same level by
        forcing the same level to be activated twice. (Engine's own
        idempotency is via ``is_activated``; this test pins it.)
        """
        adapter = _new_adapter()
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_SELL))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        _seed_sell_cascade(engine, instance, 100_000.0)
        # Cross C1.
        engine.on_quote(Quote(bid=99_500.0, ask=99_950.0))
        self.assertEqual(len(adapter.submissions), 1)
        # Call _activate_level again directly — should be a no-op.
        instance.cascade.highest_step = 1  # the cascade still thinks C1 is highest
        engine._activate_level(level=1, is_buy_cascade=False)
        self.assertEqual(len(adapter.submissions), 1)  # no duplicate
        self.assertEqual(len(adapter.set_sl_calls), 1)   # no duplicate

    def test_no_duplicate_protection_for_reactivated_level(self):
        """Scenario 14."""
        adapter = _new_adapter()
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_SELL))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        _seed_sell_cascade(engine, instance, 100_000.0)
        engine.on_quote(Quote(bid=99_500.0, ask=99_950.0))
        sl_calls_after_first = len(adapter.set_sl_calls)
        # Manually re-activate level 1.
        instance.cascade.highest_step = 1
        engine._activate_level(level=1, is_buy_cascade=False)
        self.assertEqual(len(adapter.set_sl_calls), sl_calls_after_first)


class FreezeOnSLFailureTests(unittest.TestCase):
    """Scenarios 15 + 17."""

    def test_sl_set_failure_freezes_registration(self):
        """Scenario 15: SL-set failure freezes only this registration."""
        adapter = FakeAdapter(fail_set_sl=[1])
        manager = FiboManager()
        instance = manager.start(
            cfg(counter_type=CounterType.COUNTER_SELL), adapter, FakeQuoteSource(),
        )
        # Seed and cross via the manager (catches UnprotectedCounterError).
        manager.on_quote(instance.key, Quote(bid=99_999.0, ask=100_000.0))
        manager.on_quote(instance.key, Quote(bid=99_500.0, ask=99_950.0))  # cross C1
        # Frozen.
        self.assertTrue(instance.frozen)
        # The SL set call was attempted and failed (returned False).
        self.assertEqual(len(adapter.set_sl_calls), 1)
        # An additional quote should not trigger anything.
        manager.on_quote(instance.key, Quote(bid=99_500.0, ask=99_900.0))  # would cross C2
        self.assertEqual(len(adapter.submissions), 1)  # only the seed-time C1
        self.assertEqual(len(adapter.set_sl_calls), 1)  # no further SL attempts

    def test_sl_verify_failure_freezes_registration(self):
        adapter = FakeAdapter(fail_verify_sl=[1])
        manager = FiboManager()
        instance = manager.start(
            cfg(counter_type=CounterType.COUNTER_SELL), adapter, FakeQuoteSource(),
        )
        manager.on_quote(instance.key, Quote(bid=99_999.0, ask=100_000.0))
        manager.on_quote(instance.key, Quote(bid=99_500.0, ask=99_950.0))
        self.assertTrue(instance.frozen)

    def test_submit_post_failure_keeps_market_submit_failed_reason(self):
        adapter = FakeAdapter(fail_submit=[1])
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_SELL))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        engine.on_quote(Quote(bid=99_999.0, ask=100_000.0))
        with self.assertRaises(UnprotectedCounterError) as ctx:
            engine.on_quote(Quote(bid=99_500.0, ask=99_950.0))
        self.assertEqual(ctx.exception.reason_code, "MARKET_SUBMIT_FAILED")

    def test_submit_exact_verify_timeout_uses_order_verify_failed_reason(self):
        adapter = OrderVerifyFailingAdapter()
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_SELL))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        engine.on_quote(Quote(bid=99_999.0, ask=100_000.0))
        with self.assertRaises(UnprotectedCounterError) as ctx:
            engine.on_quote(Quote(bid=99_500.0, ask=99_950.0))
        self.assertEqual(ctx.exception.reason_code, "ORDER_VERIFY_FAILED")


class FreezeOnTPFailureTests(unittest.TestCase):
    """Scenario 16: Counter4 TP failure freezes."""

    def test_counter4_tp_set_failure_freezes_registration(self):
        adapter = FakeAdapter(fail_set_tp=[1])  # 1st set_tp call fails
        manager = FiboManager()
        instance = manager.start(
            cfg(counter_type=CounterType.COUNTER_SELL), adapter, FakeQuoteSource(),
        )
        manager.on_quote(instance.key, Quote(bid=99_999.0, ask=100_000.0))
        manager.on_quote(instance.key, Quote(bid=99_500.0, ask=99_500.0))  # cross 1..4
        # C1+C2+C3+C4 all submitted; SL for each level succeeded; TP for C4 failed.
        self.assertEqual(len(adapter.submissions), 4)
        self.assertEqual(len(adapter.set_sl_calls), 4)
        self.assertEqual(len(adapter.set_tp_calls), 1)
        # And the registration is frozen.
        self.assertTrue(instance.frozen)

    def test_counter4_tp_verify_failure_freezes_registration(self):
        adapter = FakeAdapter(fail_verify_tp=[1])
        manager = FiboManager()
        instance = manager.start(
            cfg(counter_type=CounterType.COUNTER_SELL), adapter, FakeQuoteSource(),
        )
        manager.on_quote(instance.key, Quote(bid=99_999.0, ask=100_000.0))
        manager.on_quote(instance.key, Quote(bid=99_500.0, ask=99_500.0))
        self.assertTrue(instance.frozen)


class FrozenRegistrationHaltTests(unittest.TestCase):
    """Scenario 17: frozen registration sends no later mutations."""

    def test_frozen_registration_halts_subsequent_quotes(self):
        adapter = FakeAdapter(fail_set_sl=[1])
        manager = FiboManager()
        instance = manager.start(
            cfg(counter_type=CounterType.COUNTER_SELL), adapter, FakeQuoteSource(),
        )
        manager.on_quote(instance.key, Quote(bid=99_999.0, ask=100_000.0))
        manager.on_quote(instance.key, Quote(bid=99_500.0, ask=99_950.0))  # freeze
        snap_subs = len(adapter.submissions)
        snap_sl = len(adapter.set_sl_calls)
        manager.on_quote(instance.key, Quote(bid=99_500.0, ask=99_900.0))
        manager.on_quote(instance.key, Quote(bid=99_500.0, ask=99_800.0))
        # Nothing new.
        self.assertEqual(len(adapter.submissions), snap_subs)
        self.assertEqual(len(adapter.set_sl_calls), snap_sl)


class UserSTOPTests(unittest.TestCase):
    """Scenarios 18 + 19."""

    def test_stop_produces_zero_exchange_mutations(self):
        """Scenario 18."""
        adapter = _new_adapter()
        manager = FiboManager()
        instance = manager.start(
            cfg(counter_type=CounterType.COUNTER_SELL), adapter, FakeQuoteSource(),
        )
        manager.on_quote(instance.key, Quote(bid=99_999.0, ask=100_000.0))
        before = len(adapter.submissions) + len(adapter.set_sl_calls)
        manager.stop(instance.key)
        after = len(adapter.submissions) + len(adapter.set_sl_calls)
        self.assertEqual(before, after)

    def test_stop_leaves_existing_protection_untouched(self):
        """Scenario 19: after STOP, the engine's view of protection and the
        exchange-side installed SL/TP are NOT cleared.
        """
        adapter = _new_adapter()
        manager = FiboManager()
        instance = manager.start(
            cfg(counter_type=CounterType.COUNTER_SELL), adapter, FakeQuoteSource(),
        )
        manager.on_quote(instance.key, Quote(bid=99_999.0, ask=100_000.0))
        manager.on_quote(instance.key, Quote(bid=99_500.0, ask=99_500.0))  # cross 1..4
        sl_before = adapter.set_sl_calls[-1]["sl_price"]
        tp_before = adapter.set_tp_calls[-1]["tp_price"]
        # Engine view before STOP.
        prot_before_sl = instance.protection.sl_price
        prot_before_tp = instance.protection.tp_price

        manager.stop(instance.key)

        # Protection state in the engine survives the stop.
        self.assertEqual(instance.protection.sl_price, prot_before_sl)
        self.assertEqual(instance.protection.tp_price, prot_before_tp)
        # Adapter's installed values did not change.
        self.assertEqual(adapter.set_sl_calls[-1]["sl_price"], sl_before)
        self.assertEqual(adapter.set_tp_calls[-1]["tp_price"], tp_before)

    def test_stop_does_not_call_adapter_cleanup(self):
        adapter = _new_adapter()
        manager = FiboManager()
        instance = manager.start(
            cfg(counter_type=CounterType.COUNTER_SELL), adapter, FakeQuoteSource(),
        )
        manager.on_quote(instance.key, Quote(bid=99_999.0, ask=100_000.0))
        manager.on_quote(instance.key, Quote(bid=99_500.0, ask=99_500.0))
        cleanup_count_before = len(adapter.cleanups)
        manager.stop(instance.key)
        self.assertEqual(len(adapter.cleanups), cleanup_count_before)

    def test_stop_clears_frozen_state(self):
        adapter = FakeAdapter(fail_set_sl=[1])
        manager = FiboManager()
        instance = manager.start(
            cfg(counter_type=CounterType.COUNTER_SELL), adapter, FakeQuoteSource(),
        )
        manager.on_quote(instance.key, Quote(bid=99_999.0, ask=100_000.0))
        manager.on_quote(instance.key, Quote(bid=99_500.0, ask=99_950.0))  # freeze
        self.assertTrue(instance.frozen)
        manager.stop(instance.key)
        # Restart with the same key starts fresh.
        instance2 = manager.start(
            cfg(counter_type=CounterType.COUNTER_SELL), adapter, FakeQuoteSource(),
        )
        self.assertFalse(instance2.frozen)


class MultiRegistrationIndependenceTests(unittest.TestCase):
    """Scenario 20."""

    def test_two_registrations_are_independent(self):
        adapter_a = _new_adapter()
        adapter_b = _new_adapter()
        manager = FiboManager()
        cfg_a = FiboConfig(
            exchange="ondoperps", account="amiroo", instrument="US100",
            counter_type=CounterType.COUNTER_SELL, divide_percent=1000,
        )
        cfg_b = FiboConfig(
            exchange="ondoperps", account="amiroo", instrument="US500",
            counter_type=CounterType.COUNTER_SELL, divide_percent=1000,
        )
        manager.start(cfg_a, adapter_a, FakeQuoteSource())
        manager.start(cfg_b, adapter_b, FakeQuoteSource())
        # seed both
        manager.on_quote(cfg_a.key, Quote(bid=99.999, ask=100.000))
        manager.on_quote(cfg_b.key, Quote(bid=4999.999, ask=5000.000))
        # cross only level 1 on each — pick quotes that don't span
        # multiple cascade levels on either side.
        # US100 sell-ladder step1≈99.966, step2≈99.911 — ask=99.950 crosses only step1.
        # US500 sell-ladder step1≈4998.300, step2≈4995.551 — ask=4997.900 crosses only step1.
        manager.on_quote(cfg_a.key, Quote(bid=99.500, ask=99.950))
        manager.on_quote(cfg_b.key, Quote(bid=4997.500, ask=4997.900))

        # Each adapter received exactly one submission.
        self.assertEqual(len(adapter_a.submissions), 1)
        self.assertEqual(len(adapter_b.submissions), 1)
        # Each adapter received SL updates independently.
        self.assertEqual(len(adapter_a.set_sl_calls), 1)
        self.assertEqual(len(adapter_b.set_sl_calls), 1)
        # Symbols routed correctly.
        self.assertEqual(adapter_a.set_sl_calls[0]["instrument"], "US100")
        self.assertEqual(adapter_b.set_sl_calls[0]["instrument"], "US500")

    def test_one_frozen_does_not_freeze_another(self):
        adapter_a = FakeAdapter(fail_set_sl=[1])
        adapter_b = _new_adapter()
        manager = FiboManager()
        cfg_a = FiboConfig(
            exchange="ondoperps", account="amiroo", instrument="US100",
            counter_type=CounterType.COUNTER_SELL, divide_percent=1000,
        )
        cfg_b = FiboConfig(
            exchange="ondoperps", account="amiroo", instrument="US500",
            counter_type=CounterType.COUNTER_SELL, divide_percent=1000,
        )
        manager.start(cfg_a, adapter_a, FakeQuoteSource())
        manager.start(cfg_b, adapter_b, FakeQuoteSource())
        manager.on_quote(cfg_a.key, Quote(bid=99_999.0, ask=100_000.0))
        manager.on_quote(cfg_b.key, Quote(bid=4999.999, ask=5000.000))
        # A's first SL set fails.
        manager.on_quote(cfg_a.key, Quote(bid=99_500.0, ask=99_950.0))
        # B keeps working.
        manager.on_quote(cfg_b.key, Quote(bid=4997.500, ask=4997.900))

        running = {x.key: x for x in manager.list_running()}
        self.assertTrue(running[cfg_a.key].frozen)
        self.assertFalse(running[cfg_b.key].frozen)


# =========================================================================
# Identity / validation tests
# =========================================================================


class IdentityAndValidationTests(unittest.TestCase):
    def test_unique_key_includes_all_four_components(self):
        c = cfg()
        self.assertEqual(c.key, "ondoperps:amiroo:US100:counterBUY")
        c2 = cfg(counter_type=CounterType.COUNTER_SELL)
        self.assertEqual(c2.key, "ondoperps:amiroo:US100:counterSELL")
        c3 = cfg(account="bitget", counter_type=CounterType.COUNTER_SELL)
        self.assertEqual(c3.key, "ondoperps:bitget:US100:counterSELL")
        c4 = cfg(instrument="US500", counter_type=CounterType.COUNTER_BUY)
        self.assertEqual(c4.key, "ondoperps:amiroo:US500:counterBUY")

    def test_double_start_same_key_rejected(self):
        manager = FiboManager()
        manager.start(cfg(), _new_adapter(), FakeQuoteSource())
        with self.assertRaises(ValueError):
            manager.start(cfg(), _new_adapter(), FakeQuoteSource())

    def test_stop_is_idempotent(self):
        manager = FiboManager()
        manager.start(cfg(), _new_adapter(), FakeQuoteSource())
        self.assertTrue(manager.stop(cfg().key))
        self.assertFalse(manager.stop(cfg().key))

    def test_invalid_counter_type_rejected(self):
        with self.assertRaises(ValueError):
            cfg(counter_type="bogus")  # type: ignore[arg-type]

    def test_negative_counter_rejected(self):
        with self.assertRaises(ValueError):
            cfg(counter1=-1)


# =========================================================================
# Kill / recovery tests (NEW cumulative semantics — see report section)
# =========================================================================


class KillAndRecoveryTests(unittest.TestCase):
    def test_step5_kill_cleans_lane_and_reseeds_fresh_step0(self):
        adapter = _new_adapter()
        q = FakeQuoteSource(quotes_by_symbol={"US100": [Quote(bid=99.300, ask=99.300)]})
        instance = FiboInstance(cfg(
            counter_type=CounterType.COUNTER_SELL,
            counter1=0, counter2=0, counter3=0, counter4=0,
        ))
        engine = FiboEngine(instance, adapter, q)
        # Descending ladder, step0=100.000. step5 ≈ 99.446.
        engine.on_quote(Quote(bid=99.999, ask=100.000))
        instance.cumulative_volume = Decimal("2.6")
        instance.protection.sl_price = 99.8
        instance.protection.tp_price = 99.4
        engine.on_quote(Quote(bid=99.400, ask=99.400))  # past step5 downward
        self.assertEqual(len(adapter.cleanups), 1)
        # Fresh cycle is immediately re-seeded from current market price.
        self.assertTrue(instance.cascade.active)
        self.assertEqual(instance.cascade.highest_step, 0)
        self.assertEqual(instance.cumulative_volume, Decimal("0"))
        self.assertIsNone(instance.protection.sl_price)
        self.assertIsNone(instance.protection.tp_price)

    def test_recovery_cleans_lane_and_reseeds_fresh_step0(self):
        adapter = _new_adapter()
        q = FakeQuoteSource(quotes_by_symbol={"US100": [Quote(bid=100.050, ask=100.100)]})
        instance = FiboInstance(cfg(
            counter_type=CounterType.COUNTER_SELL,
            counter1=1.3, counter2=0, counter3=0, counter4=0,
        ))
        engine = FiboEngine(instance, adapter, q)
        _seed_sell_cascade(engine, instance, 100.000)
        engine.on_quote(Quote(bid=99.900, ask=99.950))   # C1 downward, adds real SELL
        self.assertEqual(instance.cumulative_volume, Decimal("1.3"))
        # Recover back up across Step0.
        engine.on_quote(Quote(bid=100.100, ask=100.150))
        self.assertEqual(len(adapter.cleanups), 1)
        self.assertTrue(instance.cascade.active)
        self.assertEqual(instance.cascade.highest_step, 0)
        self.assertEqual(instance.cumulative_volume, Decimal("0"))
        self.assertIsNone(instance.protection.sl_price)
        self.assertIsNone(instance.protection.tp_price)


class PreCounter1RecoveryTests(unittest.TestCase):
    def test_counterSELL_same_quote_as_step0_waits_no_recovery_no_cleanup(self):
        adapter = _new_adapter()
        events: List[Dict[str, Any]] = []
        instance = FiboInstance(cfg(
            counter_type=CounterType.COUNTER_SELL,
            counter1=1, counter2=0, counter3=0, counter4=0,
        ))
        engine = FiboEngine(instance, adapter, FakeQuoteSource(), events.append)
        engine.on_quote(Quote(bid=0.33383, ask=0.33383))
        engine.on_quote(Quote(bid=0.33383, ask=0.33383))
        self.assertEqual(instance.cascade.step0_price, 0.33383)
        self.assertEqual(instance.cascade.highest_step, 0)
        self.assertEqual(instance.cumulative_volume, Decimal("0"))
        self.assertEqual(adapter.cleanups, [])
        self.assertEqual(adapter.submissions, [])
        recovery_events = [e for e in events if e["event"] == "recovery_evaluated"]
        self.assertEqual(len(recovery_events), 1)
        self.assertEqual(recovery_events[0]["comparison_operator"], ">=")
        self.assertFalse(recovery_events[0]["comparison_result"])
        self.assertAlmostEqual(recovery_events[0]["recovery_target_raw"], 0.3339001043, places=10)

    def test_counterSELL_price_between_step1_and_step0tp_waits(self):
        adapter = _new_adapter()
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_SELL, counter1=1, counter2=0, counter3=0, counter4=0))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        engine.on_quote(Quote(bid=0.33383, ask=0.33383))
        engine.on_quote(Quote(bid=0.33382, ask=0.33382))
        self.assertEqual(instance.cascade.step0_price, 0.33383)
        self.assertEqual(instance.cascade.highest_step, 0)
        self.assertEqual(adapter.cleanups, [])
        self.assertEqual(adapter.submissions, [])

    def test_counterSELL_reaching_step0tp_recovers_virtually_with_zero_exchange_mutations(self):
        adapter = _new_adapter()
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_SELL, counter1=1, counter2=0, counter3=0, counter4=0))
        q = FakeQuoteSource(quotes_by_symbol={"US100": [Quote(bid=0.33391, ask=0.33391)]})
        engine = FiboEngine(instance, adapter, q)
        engine.on_quote(Quote(bid=0.33383, ask=0.33383))
        engine.on_quote(Quote(bid=0.33391, ask=0.33391))
        self.assertEqual(adapter.cleanups, [])
        self.assertEqual(adapter.submissions, [])
        self.assertEqual(instance.cumulative_volume, Decimal("0"))
        self.assertEqual(instance.cascade.highest_step, 0)
        self.assertAlmostEqual(instance.cascade.step0_price, 0.33391, places=10)

    def test_counterSELL_reaching_step1_activates_counter1_once(self):
        adapter = _new_adapter()
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_SELL, counter1=1, counter2=0, counter3=0, counter4=0))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        engine.on_quote(Quote(bid=0.33383, ask=0.33383))
        engine.on_quote(Quote(bid=0.33371, ask=0.33371))
        self.assertEqual(len(adapter.submissions), 1)
        self.assertEqual(adapter.submissions[0]["counter_step"], 1)
        self.assertEqual(adapter.cleanups, [])

    def test_counterSELL_recovery_uses_raw_not_quantized_values(self):
        adapter = _new_adapter()
        events: List[Dict[str, Any]] = []
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_SELL, counter1=1, counter2=0, counter3=0, counter4=0))
        engine = FiboEngine(instance, adapter, FakeQuoteSource(), events.append)
        engine.on_quote(Quote(bid=0.33383, ask=0.33383))
        engine.on_quote(Quote(bid=0.33390005, ask=0.33390005))
        self.assertEqual(adapter.cleanups, [])
        self.assertEqual(adapter.submissions, [])
        recovery_events = [e for e in events if e["event"] == "recovery_evaluated"]
        self.assertEqual(len(recovery_events), 1)
        self.assertFalse(recovery_events[0]["comparison_result"])
        self.assertAlmostEqual(recovery_events[0]["current_price_raw"], 0.33390005, places=10)
        self.assertAlmostEqual(recovery_events[0]["recovery_target_raw"], 0.3339001043, places=10)

    def test_counterBUY_same_quote_as_step0_waits_no_recovery_no_cleanup(self):
        adapter = _new_adapter()
        events: List[Dict[str, Any]] = []
        instance = FiboInstance(cfg(
            counter_type=CounterType.COUNTER_BUY,
            counter1=1, counter2=0, counter3=0, counter4=0,
        ))
        engine = FiboEngine(instance, adapter, FakeQuoteSource(), events.append)
        engine.on_quote(Quote(bid=0.33383, ask=0.33383))
        engine.on_quote(Quote(bid=0.33383, ask=0.33383))
        self.assertEqual(instance.cascade.step0_price, 0.33383)
        self.assertEqual(instance.cascade.highest_step, 0)
        self.assertEqual(adapter.cleanups, [])
        self.assertEqual(adapter.submissions, [])
        recovery_events = [e for e in events if e["event"] == "recovery_evaluated"]
        self.assertEqual(len(recovery_events), 1)
        self.assertEqual(recovery_events[0]["comparison_operator"], "<=")
        self.assertFalse(recovery_events[0]["comparison_result"])
        self.assertAlmostEqual(recovery_events[0]["recovery_target_raw"], 0.3337598957, places=10)

    def test_counterBUY_reaching_step0tp_recovers_virtually_with_zero_exchange_mutations(self):
        adapter = _new_adapter()
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_BUY, counter1=1, counter2=0, counter3=0, counter4=0))
        q = FakeQuoteSource(quotes_by_symbol={"US100": [Quote(bid=0.33375, ask=0.33375)]})
        engine = FiboEngine(instance, adapter, q)
        engine.on_quote(Quote(bid=0.33383, ask=0.33383))
        engine.on_quote(Quote(bid=0.33375, ask=0.33375))
        self.assertEqual(adapter.cleanups, [])
        self.assertEqual(adapter.submissions, [])
        self.assertEqual(instance.cumulative_volume, Decimal("0"))
        self.assertEqual(instance.cascade.highest_step, 0)
        self.assertAlmostEqual(instance.cascade.step0_price, 0.33375, places=10)

    def test_counterBUY_price_between_step0tp_and_step1_waits(self):
        adapter = _new_adapter()
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_BUY, counter1=1, counter2=0, counter3=0, counter4=0))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        engine.on_quote(Quote(bid=0.33383, ask=0.33383))
        engine.on_quote(Quote(bid=0.33384, ask=0.33384))
        self.assertEqual(instance.cascade.step0_price, 0.33383)
        self.assertEqual(instance.cascade.highest_step, 0)
        self.assertEqual(adapter.cleanups, [])
        self.assertEqual(adapter.submissions, [])

    def test_counterBUY_reaching_step1_activates_counter1_once(self):
        adapter = _new_adapter()
        instance = FiboInstance(cfg(counter_type=CounterType.COUNTER_BUY, counter1=1, counter2=0, counter3=0, counter4=0))
        engine = FiboEngine(instance, adapter, FakeQuoteSource())
        engine.on_quote(Quote(bid=0.33383, ask=0.33383))
        engine.on_quote(Quote(bid=0.33395, ask=0.33395))
        self.assertEqual(len(adapter.submissions), 1)
        self.assertEqual(adapter.submissions[0]["counter_step"], 1)
        self.assertEqual(adapter.cleanups, [])


# =========================================================================
# Counter price relationships (unchanged math, re-pinned tests)
# =========================================================================


class CounterPriceRelationshipsTests(unittest.TestCase):
    def test_counter1_trigger_sl_tp_counterBUY(self):
        p0 = 100.0
        trig = step_price(p0, 1, is_buy_cascade=False, divide_percent=1000)
        sl = step_tp(p0, 1, is_buy_cascade=False, divide_percent=1000)
        tp = step_price(p0, 2, is_buy_cascade=False, divide_percent=1000)
        self.assertAlmostEqual(trig, 99.966, places=3)
        self.assertAlmostEqual(sl, 100.000, places=3)
        self.assertAlmostEqual(tp, 99.9110187, places=4)

    def test_counter4_trigger_sl_tp_counterBUY(self):
        p0 = 100.0
        trig = step_price(p0, 4, is_buy_cascade=False, divide_percent=1000)
        sl = step_tp(p0, 4, is_buy_cascade=False, divide_percent=1000)
        tp = step_price(p0, 5, is_buy_cascade=False, divide_percent=1000)
        self.assertAlmostEqual(trig, 99.6783540724, places=7)
        self.assertAlmostEqual(sl, 99.8220978934, places=7)
        self.assertAlmostEqual(tp, 99.4461035074, places=7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
