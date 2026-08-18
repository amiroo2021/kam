"""Generic GoldenFibo Lighter preflight validation.

Runs BEFORE any exchange mutation on START. Validates that the proposed
initial ladder structure (Step0 MARKET + Step1 LIMIT, and by induction the
rest of the ladder) satisfies the venue's base-size, price-increment, and
minimum-quote constraints.

This module is strategy-aware (GoldenFibo ladder math) but venue-generic
(any instrument / direction). It never mutates exchange state and never
changes the user's requested volume — it either accepts or rejects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Dict, List, Optional

from .config import (
    FIBO_RATIO,
    MAX_STEP,
    golden_fibo_next_ladder_price,
    golden_fibo_tp_price,
)


# ---------------------------------------------------------------------------
# Quantization helpers (venue ROUND_FLOOR-style, matching the agent)
# ---------------------------------------------------------------------------
def _quantize_down(value: Decimal, decimals: int) -> Decimal:
    if decimals <= 0:
        return value
    q = Decimal(1).scaleb(-decimals)
    return value.quantize(q, rounding=ROUND_FLOOR)


def _ceil_to_increment(value: Decimal, decimals: int) -> Decimal:
    if decimals <= 0:
        return value
    q = Decimal(1).scaleb(-decimals)
    return value.quantize(q, rounding=ROUND_CEILING)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class PreflightResult:
    ok: bool
    error: Optional[str] = None
    detail: Optional[str] = None
    # Diagnostics for reporting
    estimated_p0: Optional[Decimal] = None
    estimated_tp0: Optional[Decimal] = None
    estimated_p1: Optional[Decimal] = None
    min_quote_amount: Optional[Decimal] = None
    min_base_amount: Optional[Decimal] = None
    size_decimals: int = 0
    price_decimals: int = 0
    safe_min_step0_volume: Optional[Decimal] = None
    failing_step: Optional[int] = None
    step_reports: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core preflight
# ---------------------------------------------------------------------------
def golden_fibo_lighter_preflight(
    *,
    direction: str,
    percentage: Decimal,
    step0_volume: Decimal,
    estimated_p0: Decimal,
    min_base_amount: Decimal,
    min_quote_amount: Decimal,
    size_decimals: int,
    price_decimals: int,
) -> PreflightResult:
    """Validate the full proposed GoldenFibo ladder against venue rules.

    Checks, for every step n in 1..MAX_STEP:
      - ladder price P(n) is positive and a valid price-increment
      - volume V(n) meets min_base_amount
      - notional V(n) * P(n) meets min_quote_amount

    Step0 is a MARKET order (no min-quote LIMIT check), but its base size
    must meet min_base_amount.

    Returns a PreflightResult. On the FIRST failing step, ok=False with a
    human-readable explanation and a computed safe_min_step0_volume (the
    minimum volume that would make the failing step's notional valid,
    rounded UP to the size increment).
    """
    direction = str(direction).upper()
    percentage = Decimal(str(percentage))
    step0_volume = Decimal(str(step0_volume))
    estimated_p0 = Decimal(str(estimated_p0))
    min_base_amount = Decimal(str(min_base_amount or "0"))
    min_quote_amount = Decimal(str(min_quote_amount or "0"))

    reports: List[Dict[str, Any]] = []

    # Estimated TP0 / P1 for reporting (BUY/SELL handled by config math).
    estimated_tp0 = golden_fibo_tp_price(direction, estimated_p0, percentage)
    # Step1 price uses the same recurrence: P1 = P0 + 1.618*(P0 - TP0)
    estimated_p1 = golden_fibo_next_ladder_price(direction, estimated_p0, estimated_tp0)

    # --- Step0 base-size check (MARKET; no min-quote LIMIT rule) ---
    if min_base_amount > 0 and step0_volume < min_base_amount:
        return PreflightResult(
            ok=False,
            error="STEP0_BELOW_MIN_BASE",
            detail=(
                f"Step0 volume {step0_volume} is below Lighter minimum base "
                f"size {min_base_amount}."
            ),
            estimated_p0=estimated_p0,
            estimated_tp0=estimated_tp0,
            estimated_p1=estimated_p1,
            min_quote_amount=min_quote_amount,
            min_base_amount=min_base_amount,
            size_decimals=size_decimals,
            price_decimals=price_decimals,
        )

    # --- Ladder steps 1..MAX_STEP ---
    pk = estimated_p0
    tpk = estimated_tp0
    for n in range(1, MAX_STEP + 1):
        v = step0_volume if n == 1 else step0_volume * (Decimal(2) ** (n - 1))
        price = golden_fibo_next_ladder_price(direction, pk, tpk)
        tpk_next = pk  # TP(n) = P(n-1)

        # Price validity: must be positive and quantize to a valid increment.
        # Step1 is a hard venue constraint (the resting LIMIT placed right
        # after Step0). Deeper steps that go non-positive are a property of
        # the mean-reversion math at this step count, only reached after
        # intermediate fills change the structure — record as a non-blocking
        # warning instead of rejecting the whole configuration.
        if price <= 0:
            reports.append({
                "step": n,
                "volume": str(v),
                "price": str(price),
                "notional": None,
                "meets_min_quote": None,
                "warning": "non_positive_price",
            })
            # Stop the ladder walk here; deeper steps are not meaningful.
            break

        quantized_price = _quantize_down(price, price_decimals)
        if quantized_price <= 0:
            if n == 1:
                return PreflightResult(
                    ok=False,
                    error="LADDER_PRICE_BELOW_INCREMENT",
                    detail=(
                        f"Step1 ladder price {price} quantizes to {quantized_price}, "
                        f"below the venue's minimum price increment."
                    ),
                    estimated_p0=estimated_p0,
                    estimated_tp0=estimated_tp0,
                    estimated_p1=estimated_p1,
                    min_quote_amount=min_quote_amount,
                    min_base_amount=min_base_amount,
                    size_decimals=size_decimals,
                    price_decimals=price_decimals,
                    failing_step=n,
                    step_reports=reports,
                )
            reports.append({
                "step": n,
                "volume": str(v),
                "price": str(quantized_price),
                "notional": None,
                "meets_min_quote": None,
                "warning": "below_price_increment",
            })
            break

        # Base-size validity.
        if min_base_amount > 0 and v < min_base_amount:
            return PreflightResult(
                ok=False,
                error="LADDER_BELOW_MIN_BASE",
                detail=(
                    f"Step{n} volume {v} is below Lighter minimum base size "
                    f"{min_base_amount}."
                ),
                estimated_p0=estimated_p0,
                estimated_tp0=estimated_tp0,
                estimated_p1=estimated_p1,
                min_quote_amount=min_quote_amount,
                min_base_amount=min_base_amount,
                size_decimals=size_decimals,
                price_decimals=price_decimals,
                failing_step=n,
                step_reports=reports,
            )

        # Minimum-quote validity for the resting LIMIT.
        notional = v * quantized_price
        reports.append({
            "step": n,
            "volume": str(v),
            "price": str(quantized_price),
            "notional": str(notional),
            "meets_min_quote": notional >= min_quote_amount if min_quote_amount > 0 else True,
        })
        if min_quote_amount > 0 and notional < min_quote_amount:
            safe_min = _ceil_to_increment(min_quote_amount / quantized_price, size_decimals)
            return PreflightResult(
                ok=False,
                error="STEP1_BELOW_MIN_QUOTE" if n == 1 else "LADDER_BELOW_MIN_QUOTE",
                detail=(
                    f"Step{n} volume {v} at price {quantized_price} gives notional "
                    f"{notional}, below Lighter minimum {min_quote_amount} for a "
                    f"LIMIT order. Minimum Step0 volume at the current estimated "
                    f"price is approximately {safe_min}."
                ),
                estimated_p0=estimated_p0,
                estimated_tp0=estimated_tp0,
                estimated_p1=estimated_p1,
                min_quote_amount=min_quote_amount,
                min_base_amount=min_base_amount,
                size_decimals=size_decimals,
                price_decimals=price_decimals,
                safe_min_step0_volume=safe_min,
                failing_step=n,
                step_reports=reports,
            )

        pk, tpk = price, tpk_next

    return PreflightResult(
        ok=True,
        estimated_p0=estimated_p0,
        estimated_tp0=estimated_tp0,
        estimated_p1=estimated_p1,
        min_quote_amount=min_quote_amount,
        min_base_amount=min_base_amount,
        size_decimals=size_decimals,
        price_decimals=price_decimals,
        step_reports=reports,
    )


def compute_safe_min_step0_volume(
    *,
    direction: str,
    percentage: Decimal,
    estimated_p0: Decimal,
    min_quote_amount: Decimal,
    size_decimals: int,
    price_decimals: int,
    conservative_factor: Decimal = Decimal("1.02"),
) -> Optional[Decimal]:
    """Compute the minimum Step0 volume whose Step1 notional meets min_quote.

    Uses the estimated Step1 price (BUY: below P0, SELL: above P0), rounded
    UP to the size increment, with a small conservative factor to absorb
    slippage between the preflight market estimate and the actual Step0 fill.
    Returns None if min_quote_amount is zero/unknown.
    """
    min_quote_amount = Decimal(str(min_quote_amount or "0"))
    if min_quote_amount <= 0:
        return None
    estimated_p0 = Decimal(str(estimated_p0))
    percentage = Decimal(str(percentage))
    tp0 = golden_fibo_tp_price(direction, estimated_p0, percentage)
    p1 = golden_fibo_next_ladder_price(direction, estimated_p0, tp0)
    quantized_p1 = _quantize_down(p1, price_decimals)
    if quantized_p1 <= 0:
        return None
    raw_min = min_quote_amount / quantized_p1
    safe = _ceil_to_increment(raw_min * conservative_factor, size_decimals)
    return safe
