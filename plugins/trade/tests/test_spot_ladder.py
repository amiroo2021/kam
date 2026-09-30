"""Unit tests for the offline /tradespot MEXC ladder planner.

Pure Decimal arithmetic. No live exchange or Telegram calls.
"""
from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.spot_ladder import (  # noqa: E402
    LadderPlan,
    LadderChild,
    compute_ladder,
    compute_ladder_with_min_notional,
)


def _inst(
    symbol: str,
    base: str,
    quote: str,
    *,
    size_step: str = "0.000001",
    price_tick: str = "0.01",
    min_qty: str = "",
    max_qty: str = "",
    min_notional: str = "1",
) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "base": base,
        "quote": quote,
        "baseAsset": base,
        "quoteAsset": quote,
        "size_step": size_step,
        "price_tick": price_tick,
        "min_qty": min_qty,
        "max_qty": max_qty,
        "min_notional": min_notional,
        "display_name": f"{base}/{quote}",
    }


class SpotLadderUniformTests(unittest.TestCase):
    def test_uniform_buy_sol_usdc_default(self) -> None:
        plan = compute_ladder(
            side="BUY",
            distribution="uniform",
            total_volume=Decimal("10"),
            start_price=Decimal("100"),
            end_price=Decimal("73"),
            order_count=10,
            instrument=_inst(
                "SOLUSDC",
                "SOL",
                "USDC",
                size_step="0.000001",
                price_tick="0.01",
            ),
        )
        self.assertIsInstance(plan, LadderPlan)
        self.assertEqual(plan.side, "BUY")
        self.assertEqual(plan.distribution, "uniform")
        self.assertEqual(len(plan.children), 10)
        # All children equal to 1.0 SOL (10/10) since size_step is small enough
        for child in plan.children:
            self.assertEqual(child.size, Decimal("1"))
            self.assertGreater(child.notional, Decimal("0"))
        self.assertEqual(plan.total_size, Decimal("10"))
        # VWAP should be the unweighted average of (100 + 91 + ... + 73) / 10 = 86.5
        self.assertEqual(plan.vwap, Decimal("86.5"))
        # First/last prices
        self.assertEqual(plan.children[0].price, Decimal("100"))
        self.assertEqual(plan.children[-1].price, Decimal("73"))

    def test_uniform_sell_sol_usdc(self) -> None:
        plan = compute_ladder(
            side="SELL",
            distribution="uniform",
            total_volume=Decimal("15"),
            start_price=Decimal("120"),
            end_price=Decimal("250"),
            order_count=8,
            instrument=_inst("SOLUSDC", "SOL", "USDC", size_step="0.000001", price_tick="0.01"),
        )
        self.assertEqual(len(plan.children), 8)
        self.assertEqual(plan.children[0].price, Decimal("120"))
        self.assertEqual(plan.children[-1].price, Decimal("250"))
        # Total notional must be roughly 15 * 185 = 2775 (uniform qty 1.875 at 15/8 = 1.875)
        # But qty must be quantized to size_step 0.000001 — fine.
        self.assertAlmostEqual(float(plan.total_size), 15.0, places=6)


class SpotLadderHalfGaussianTests(unittest.TestCase):
    def test_half_gaussian_smallest_at_start_largest_at_end(self) -> None:
        plan = compute_ladder(
            side="BUY",
            distribution="half_gaussian",
            total_volume=Decimal("10"),
            start_price=Decimal("100"),
            end_price=Decimal("73"),
            order_count=10,
            instrument=_inst("SOLUSDC", "SOL", "USDC", size_step="0.000001", price_tick="0.01"),
        )
        self.assertEqual(len(plan.children), 10)
        # First child should have smaller size than last child
        first = plan.children[0].size
        last = plan.children[-1].size
        self.assertLess(first, last, f"first={first} last={last}")

    def test_half_gaussian_sell_smallest_at_start(self) -> None:
        plan = compute_ladder(
            side="SELL",
            distribution="half_gaussian",
            total_volume=Decimal("15"),
            start_price=Decimal("120"),
            end_price=Decimal("250"),
            order_count=8,
            instrument=_inst("SOLUSDC", "SOL", "USDC", size_step="0.000001", price_tick="0.01"),
        )
        self.assertEqual(len(plan.children), 8)
        first = plan.children[0].size
        last = plan.children[-1].size
        self.assertLess(first, last)


class SpotLadderSizeStepTests(unittest.TestCase):
    def test_sui_size_step_0_01(self) -> None:
        # 10 SUI cannot fit 10 children each clearing $1 at price 0.80;
        # use 20 SUI and assert exactly 10 children produced.
        plan = compute_ladder(
            side="BUY",
            distribution="uniform",
            total_volume=Decimal("20"),
            start_price=Decimal("1.20"),
            end_price=Decimal("0.80"),
            order_count=10,
            instrument=_inst("SUIUSDC", "SUI", "USDC", size_step="0.01", price_tick="0.0001"),
        )
        self.assertEqual(len(plan.children), 10)
        for child in plan.children:
            # Each child qty must be multiple of 0.01
            self.assertEqual(child.size, child.size.quantize(Decimal("0.01")))
        # Every child must independently clear the minimum notional.
        for child in plan.children:
            self.assertGreaterEqual(child.notional, Decimal("1"))

    def test_hype_size_step_0_01(self) -> None:
        plan = compute_ladder(
            side="BUY",
            distribution="uniform",
            total_volume=Decimal("5"),
            start_price=Decimal("60"),
            end_price=Decimal("40"),
            order_count=5,
            instrument=_inst("HYPEUSDC", "HYPE", "USDC", size_step="0.01", price_tick="0.01"),
        )
        self.assertEqual(len(plan.children), 5)
        for child in plan.children:
            self.assertEqual(child.size, child.size.quantize(Decimal("0.01")))

    def test_sol_size_step_0_000001(self) -> None:
        plan = compute_ladder(
            side="BUY",
            distribution="uniform",
            total_volume=Decimal("0.5"),
            start_price=Decimal("120"),
            end_price=Decimal("80"),
            order_count=10,
            instrument=_inst("SOLUSDC", "SOL", "USDC", size_step="0.000001", price_tick="0.01"),
        )
        # Sum of sizes must be ROUND_DOWN total to size_step
        total = sum((c.size for c in plan.children), Decimal("0"))
        self.assertLessEqual(total, Decimal("0.5"))


class SpotLadderPriceTickTests(unittest.TestCase):
    def test_price_tick_rounding(self) -> None:
        plan = compute_ladder(
            side="BUY",
            distribution="uniform",
            total_volume=Decimal("10"),
            start_price=Decimal("100.123"),
            end_price=Decimal("73.987"),
            order_count=5,
            instrument=_inst("SOLUSDC", "SOL", "USDC", size_step="0.000001", price_tick="0.01"),
        )
        for child in plan.children:
            # Quantized to price_tick 0.01
            self.assertEqual(child.price, child.price.quantize(Decimal("0.01")))

    def test_duplicate_price_prevention(self) -> None:
        # 100 orders over a 0.01 tick range that only fits 30 unique levels.
        plan = compute_ladder(
            side="BUY",
            distribution="uniform",
            total_volume=Decimal("10"),
            start_price=Decimal("100"),
            end_price=Decimal("73"),
            order_count=100,
            instrument=_inst("SOLUSDC", "SOL", "USDC", size_step="0.000001", price_tick="0.01"),
        )
        # Actual child count should be capped by unique price levels
        unique_prices = {child.price for child in plan.children}
        self.assertEqual(len(plan.children), len(unique_prices))

    def test_requested_count_beyond_unique_prices_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            compute_ladder(
                side="BUY",
                distribution="uniform",
                total_volume=Decimal("10"),
                start_price=Decimal("100"),
                end_price=Decimal("73"),
                order_count=10**6,
                instrument=_inst(
                    "SOLUSDC",
                    "SOL",
                    "USDC",
                    size_step="0.000001",
                    price_tick="0.01",
                ),
            )
        self.assertIn("unique", str(ctx.exception).lower())


class SpotLadderMinNotionalTests(unittest.TestCase):
    def test_min_notional_per_child_is_one_quote(self) -> None:
        # Each child independently must clear the 1 USDC min notional.
        plan = compute_ladder_with_min_notional(
            side="BUY",
            distribution="uniform",
            total_volume=Decimal("10"),
            start_price=Decimal("100"),
            end_price=Decimal("73"),
            order_count=10,
            instrument=_inst("SOLUSDC", "SOL", "USDC", size_step="0.000001", price_tick="0.01"),
            min_notional=Decimal("1"),
        )
        for child in plan.children:
            self.assertGreaterEqual(child.notional, Decimal("1"))

    def test_min_notional_min_qty_redistributes_when_below_minimum(self) -> None:
        # Build a child whose notional would be below 1 USDT and confirm the planner
        # bails out with a useful error instead of submitting a small order.
        with self.assertRaises(ValueError):
            compute_ladder(
                side="BUY",
                distribution="uniform",
                total_volume=Decimal("0.001"),
                start_price=Decimal("100"),
                end_price=Decimal("73"),
                order_count=100,
                instrument=_inst(
                    "SOLUSDC",
                    "SOL",
                    "USDC",
                    size_step="0.000001",
                    price_tick="0.01",
                ),
            )


class SpotLadderBalanceTests(unittest.TestCase):
    def test_buy_required_quote_is_sum_of_notional(self) -> None:
        plan = compute_ladder(
            side="BUY",
            distribution="uniform",
            total_volume=Decimal("10"),
            start_price=Decimal("100"),
            end_price=Decimal("73"),
            order_count=10,
            instrument=_inst("SOLUSDC", "SOL", "USDC", size_step="0.000001", price_tick="0.01"),
        )
        expected = sum((c.notional for c in plan.children), Decimal("0"))
        self.assertEqual(plan.total_notional, expected)

    def test_sell_required_base_is_total_size(self) -> None:
        plan = compute_ladder(
            side="SELL",
            distribution="uniform",
            total_volume=Decimal("15"),
            start_price=Decimal("120"),
            end_price=Decimal("250"),
            order_count=8,
            instrument=_inst("SOLUSDC", "SOL", "USDC", size_step="0.000001", price_tick="0.01"),
        )
        self.assertEqual(plan.total_size, sum((c.size for c in plan.children), Decimal("0")))


class SpotLadderVwapTests(unittest.TestCase):
    def test_vwap_is_size_weighted_average(self) -> None:
        plan = compute_ladder(
            side="BUY",
            distribution="uniform",
            total_volume=Decimal("10"),
            start_price=Decimal("100"),
            end_price=Decimal("73"),
            order_count=10,
            instrument=_inst("SOLUSDC", "SOL", "USDC", size_step="0.000001", price_tick="0.01"),
        )
        # Compute expected VWAP using a different formula path (single reduce)
        expected = sum(
            (c.price * c.size for c in plan.children), Decimal("0")
        ) / sum((c.size for c in plan.children), Decimal("0"))
        self.assertEqual(plan.vwap, expected.quantize(Decimal("0.01")))


class SpotLadderValidationTests(unittest.TestCase):
    def test_invalid_side_raises(self) -> None:
        with self.assertRaises(ValueError):
            compute_ladder(
                side="MARKET",
                distribution="uniform",
                total_volume=Decimal("10"),
                start_price=Decimal("100"),
                end_price=Decimal("73"),
                order_count=10,
                instrument=_inst("SOLUSDC", "SOL", "USDC"),
            )

    def test_invalid_direction_buy_end_greater_or_equal_start_raises(self) -> None:
        with self.assertRaises(ValueError):
            compute_ladder(
                side="BUY",
                distribution="uniform",
                total_volume=Decimal("10"),
                start_price=Decimal("73"),
                end_price=Decimal("100"),
                order_count=10,
                instrument=_inst("SOLUSDC", "SOL", "USDC"),
            )

    def test_invalid_direction_sell_end_less_or_equal_start_raises(self) -> None:
        with self.assertRaises(ValueError):
            compute_ladder(
                side="SELL",
                distribution="uniform",
                total_volume=Decimal("10"),
                start_price=Decimal("250"),
                end_price=Decimal("120"),
                order_count=10,
                instrument=_inst("SOLUSDC", "SOL", "USDC"),
            )

    def test_invalid_distribution_raises(self) -> None:
        with self.assertRaises(ValueError):
            compute_ladder(
                side="BUY",
                distribution="weibull",
                total_volume=Decimal("10"),
                start_price=Decimal("100"),
                end_price=Decimal("73"),
                order_count=10,
                instrument=_inst("SOLUSDC", "SOL", "USDC"),
            )


class SpotLadderMaxValidChildrenTests(unittest.TestCase):
    def test_max_valid_children_when_lot_size_limited(self) -> None:
        # 10 SOL with size_step 1 SOL → at most 10 children. The user asked for
        # 50, so the planner must REJECT with a max-valid message rather than
        # silently capping.
        with self.assertRaises(ValueError) as ctx:
            compute_ladder(
                side="BUY",
                distribution="uniform",
                total_volume=Decimal("10"),
                start_price=Decimal("100"),
                end_price=Decimal("73"),
                order_count=50,
                instrument=_inst(
                    "FOOUSDC",
                    "FOO",
                    "USDC",
                    size_step="1",
                    price_tick="0.01",
                ),
            )
        self.assertIn("50", str(ctx.exception))
        self.assertIn("Maximum valid orders: 10", str(ctx.exception))

    def test_min_notional_caps_max_valid_children(self) -> None:
        # SOL/USDC 0.01 step + 1 USDC min notional at prices 73..100 means each
        # child must hold at least ceil(1/price)*0.01 = at least 0.02 SOL at
        # $73. With only 0.05 SOL total volume (5 size_steps), we cannot fit
        # 50 children — planner rejects with a max-valid reason.
        with self.assertRaises(ValueError) as ctx:
            compute_ladder(
                side="BUY",
                distribution="uniform",
                total_volume=Decimal("0.05"),
                start_price=Decimal("100"),
                end_price=Decimal("73"),
                order_count=50,
                instrument=_inst(
                    "SOLUSDC",
                    "SOL",
                    "USDC",
                    size_step="0.01",
                    price_tick="0.01",
                ),
            )
        self.assertIn("Maximum valid orders: 5", str(ctx.exception))


class SpotLadderSuiExplicitTests(unittest.TestCase):
    """Real SUI/USDC cases — both distributions, both directions.

    At price=0.80, each child must be ≥ 1.25 SUI to clear the 1 USDC
    min notional (size_step 0.01). For 10 children to fit at prices
    1.20 → 0.80, total volume must be ≥ ~13 SUI. We use 20 SUI for
    comfortable headroom.
    """

    def _sui(self, **fields):
        kw = {"size_step": "0.01", "price_tick": "0.0001"}
        kw.update(fields)
        return _inst("SUIUSDC", "SUI", "USDC", **kw)

    def test_sui_uniform_buy(self) -> None:
        plan = compute_ladder(
            side="BUY",
            distribution="uniform",
            total_volume=Decimal("20"),
            start_price=Decimal("1.20"),
            end_price=Decimal("0.80"),
            order_count=10,
            instrument=self._sui(),
        )
        # Exactly 10 children or raise — never silently fewer.
        self.assertEqual(len(plan.children), 10)
        for child in plan.children:
            self.assertGreaterEqual(child.notional, Decimal("1"))
            self.assertEqual(child.size, child.size.quantize(Decimal("0.01")))
            self.assertEqual(child.price, child.price.quantize(Decimal("0.0001")))

    def test_sui_half_gaussian_buy(self) -> None:
        plan = compute_ladder(
            side="BUY",
            distribution="half_gaussian",
            total_volume=Decimal("20"),
            start_price=Decimal("1.20"),
            end_price=Decimal("0.80"),
            order_count=10,
            instrument=self._sui(),
        )
        self.assertEqual(len(plan.children), 10)
        for child in plan.children:
            self.assertGreaterEqual(child.notional, Decimal("1"))
        # Monotonic sizing: every successive child >= previous.
        for prev, cur in zip(plan.children, plan.children[1:]):
            self.assertLessEqual(prev.size, cur.size)

    def test_sui_uniform_sell(self) -> None:
        plan = compute_ladder(
            side="SELL",
            distribution="uniform",
            total_volume=Decimal("20"),
            start_price=Decimal("0.80"),
            end_price=Decimal("1.20"),
            order_count=10,
            instrument=self._sui(),
        )
        self.assertEqual(len(plan.children), 10)
        for child in plan.children:
            self.assertGreaterEqual(child.notional, Decimal("1"))

    def test_sui_half_gaussian_sell_rejected_due_to_min_notional_monotonic_conflict(self) -> None:
        # SELL half-Gaussian with START=0.80 (low price). Min-notional floor
        # requires LARGER size at lower prices (1.25 SUI at $0.80 vs 0.84 SUI
        # at $1.20). The half-Gaussian convention wants SMALLEST at START.
        # These two conflict and the planner must REJECT (per spec).
        with self.assertRaises(ValueError) as ctx:
            compute_ladder(
                side="SELL",
                distribution="half_gaussian",
                total_volume=Decimal("20"),
                start_price=Decimal("0.80"),
                end_price=Decimal("1.20"),
                order_count=10,
                instrument=self._sui(),
            )
        self.assertIn("monotonic", str(ctx.exception).lower())

    def test_sui_too_few_children_rejected(self) -> None:
        # 5 SUI at price 0.80 cannot fit 10 children each meeting $1
        # min notional — planner must REJECT with max-valid message.
        with self.assertRaises(ValueError) as ctx:
            compute_ladder(
                side="SELL",
                distribution="uniform",
                total_volume=Decimal("5"),
                start_price=Decimal("0.80"),
                end_price=Decimal("1.20"),
                order_count=10,
                instrument=self._sui(),
            )
        self.assertIn("Maximum valid orders:", str(ctx.exception))


class SpotLadderInvariantsTests(unittest.TestCase):
    def test_uniform_quantities_differ_by_at_most_one_size_step(self) -> None:
        plan = compute_ladder(
            side="BUY",
            distribution="uniform",
            total_volume=Decimal("10"),
            start_price=Decimal("100"),
            end_price=Decimal("73"),
            order_count=10,
            instrument=_inst("SOLUSDC", "SOL", "USDC", size_step="0.000001", price_tick="0.01"),
        )
        sizes = [c.size for c in plan.children]
        max_diff = max(sizes) - min(sizes)
        self.assertLessEqual(max_diff, Decimal("0.000001"))

    def test_half_gaussian_final_children_are_monotonic(self) -> None:
        plan = compute_ladder(
            side="BUY",
            distribution="half_gaussian",
            total_volume=Decimal("10"),
            start_price=Decimal("100"),
            end_price=Decimal("73"),
            order_count=10,
            instrument=_inst("SOLUSDC", "SOL", "USDC", size_step="0.000001", price_tick="0.01"),
        )
        sizes = [c.size for c in plan.children]
        for prev, cur in zip(sizes, sizes[1:]):
            self.assertLessEqual(prev, cur, f"size not monotonic: {sizes}")

    def test_half_gaussian_monotonic_after_min_notional_correction(self) -> None:
        # SUI 0.01 step + 1 USDC min notional triggers per-child correction.
        # Use 20 SUI so all 10 children fit comfortably.
        plan = compute_ladder(
            side="BUY",
            distribution="half_gaussian",
            total_volume=Decimal("20"),
            start_price=Decimal("1.20"),
            end_price=Decimal("0.80"),
            order_count=10,
            instrument=_inst(
                "SUIUSDC", "SUI", "USDC", size_step="0.01", price_tick="0.0001"
            ),
        )
        self.assertEqual(len(plan.children), 10)
        sizes = [c.size for c in plan.children]
        for prev, cur in zip(sizes, sizes[1:]):
            self.assertLessEqual(prev, cur)

    def test_vwap_matches_final_children(self) -> None:
        plan = compute_ladder(
            side="BUY",
            distribution="half_gaussian",
            total_volume=Decimal("20"),
            start_price=Decimal("1.20"),
            end_price=Decimal("0.80"),
            order_count=10,
            instrument=_inst(
                "SUIUSDC", "SUI", "USDC", size_step="0.01", price_tick="0.0001"
            ),
        )
        # VWAP is computed from the final children (post min-notional
        # correction) using full Decimal precision. Assert it matches
        # the independent computation exactly.
        expected = sum(
            (c.price * c.size for c in plan.children), Decimal("0")
        ) / sum((c.size for c in plan.children), Decimal("0"))
        self.assertEqual(plan.vwap, expected)

    def test_no_zero_children_no_duplicate_prices(self) -> None:
        plan = compute_ladder(
            side="BUY",
            distribution="uniform",
            total_volume=Decimal("10"),
            start_price=Decimal("100"),
            end_price=Decimal("73"),
            order_count=20,
            instrument=_inst("SOLUSDC", "SOL", "USDC", size_step="0.000001", price_tick="0.01"),
        )
        self.assertEqual(len(plan.children), 20)
        for c in plan.children:
            self.assertGreater(c.size, Decimal("0"))
            self.assertGreater(c.notional, Decimal("0"))
        prices = [c.price for c in plan.children]
        self.assertEqual(len(prices), len(set(prices)))

    def test_total_size_does_not_exceed_requested(self) -> None:
        plan = compute_ladder(
            side="BUY",
            distribution="uniform",
            total_volume=Decimal("10"),
            start_price=Decimal("100"),
            end_price=Decimal("73"),
            order_count=10,
            instrument=_inst("SOLUSDC", "SOL", "USDC", size_step="0.000001", price_tick="0.01"),
        )
        self.assertLessEqual(plan.total_size, Decimal("10"))


class SpotLadderPriceDirectionTests(unittest.TestCase):
    """Verify price direction is preserved after tick rounding."""

    def _direction(self, prices):
        return ("increasing" if prices[-1] > prices[0] else "decreasing") if len(prices) >= 2 else "n/a"

    def test_buy_100_to_73_preserved(self) -> None:
        plan = compute_ladder(
            side="BUY",
            distribution="uniform",
            total_volume=Decimal("10"),
            start_price=Decimal("100"),
            end_price=Decimal("73"),
            order_count=10,
            instrument=_inst("SOLUSDC", "SOL", "USDC", size_step="0.000001", price_tick="0.01"),
        )
        prices = [c.price for c in plan.children]
        self.assertEqual(prices[0], Decimal("100"))
        self.assertEqual(prices[-1], Decimal("73"))
        self.assertEqual(self._direction(prices), "decreasing")

    def test_buy_73_to_100_rejected(self) -> None:
        with self.assertRaises(ValueError):
            compute_ladder(
                side="BUY",
                distribution="uniform",
                total_volume=Decimal("10"),
                start_price=Decimal("73"),
                end_price=Decimal("100"),
                order_count=10,
                instrument=_inst("SOLUSDC", "SOL", "USDC", size_step="0.000001", price_tick="0.01"),
            )

    def test_sell_100_to_120_preserved(self) -> None:
        plan = compute_ladder(
            side="SELL",
            distribution="uniform",
            total_volume=Decimal("10"),
            start_price=Decimal("100"),
            end_price=Decimal("120"),
            order_count=10,
            instrument=_inst("SOLUSDC", "SOL", "USDC", size_step="0.000001", price_tick="0.01"),
        )
        prices = [c.price for c in plan.children]
        self.assertEqual(prices[0], Decimal("100"))
        self.assertEqual(prices[-1], Decimal("120"))
        self.assertEqual(self._direction(prices), "increasing")

    def test_sell_120_to_100_rejected(self) -> None:
        with self.assertRaises(ValueError):
            compute_ladder(
                side="SELL",
                distribution="uniform",
                total_volume=Decimal("10"),
                start_price=Decimal("120"),
                end_price=Decimal("100"),
                order_count=10,
                instrument=_inst("SOLUSDC", "SOL", "USDC", size_step="0.000001", price_tick="0.01"),
            )


class SpotLadderLargeCountsTests(unittest.TestCase):
    """20/21/50/100/200/500 — exact count or explicit reject."""

    def _plan(self, n: int):
        return compute_ladder(
            side="BUY",
            distribution="uniform",
            total_volume=Decimal("10"),
            start_price=Decimal("100"),
            end_price=Decimal("73"),
            order_count=n,
            instrument=_inst("SOLUSDC", "SOL", "USDC", size_step="0.000001", price_tick="0.01"),
        )

    def _check(self, n: int) -> None:
        plan = self._plan(n)
        self.assertEqual(len(plan.children), n, f"expected {n} children, got {len(plan.children)}")
        prices = [c.price for c in plan.children]
        self.assertEqual(len(prices), len(set(prices)), "duplicate prices present")
        for c in plan.children:
            self.assertGreater(c.size, Decimal("0"))
            self.assertEqual(c.size, c.size.quantize(Decimal("0.000001")))
            self.assertEqual(c.price, c.price.quantize(Decimal("0.01")))
            self.assertGreaterEqual(c.notional, Decimal("1"))
        self.assertLessEqual(plan.total_size, Decimal("10"))

    def test_20(self) -> None:
        self._check(20)

    def test_21(self) -> None:
        self._check(21)

    def test_50(self) -> None:
        self._check(50)

    def test_100(self) -> None:
        self._check(100)

    def test_200(self) -> None:
        self._check(200)

    def test_500(self) -> None:
        # 100..73 over price_tick=0.01 → 2700 unique levels; 500 fits.
        self._check(500)


class SpotLadderExchangeNeutralityTests(unittest.TestCase):
    """spot_ladder must be exchange-neutral — caller supplies min_notional."""

    def test_no_exchange_constant_fallback_used(self) -> None:
        # When the resolved instrument has no min_notional field, planner
        # must RAISE rather than silently apply an exchange-specific default.
        with self.assertRaises(ValueError):
            compute_ladder(
                side="BUY",
                distribution="uniform",
                total_volume=Decimal("10"),
                start_price=Decimal("100"),
                end_price=Decimal("73"),
                order_count=10,
                instrument=_inst("XUSDC", "X", "USDC", size_step="0.000001", price_tick="0.01", min_notional=""),
            )

    def test_caller_supplied_min_notional_drives_planner(self) -> None:
        plan = compute_ladder_with_min_notional(
            side="BUY",
            distribution="uniform",
            total_volume=Decimal("10"),
            start_price=Decimal("100"),
            end_price=Decimal("73"),
            order_count=10,
            instrument=_inst("XUSDC", "X", "USDC", size_step="0.000001", price_tick="0.01"),
            min_notional=Decimal("1"),
        )
        self.assertEqual(len(plan.children), 10)
        for c in plan.children:
            self.assertGreaterEqual(c.notional, Decimal("1"))


if __name__ == "__main__":
    unittest.main()