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
    failing_raw_price: Optional[Decimal] = None
    percentage: Optional[Decimal] = None
    max_positive_percentage: Optional[Decimal] = None
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
    """ONE-STEP-AHEAD GoldenFibo Lighter preflight.

    The robot operates sequentially like the MQ4 EA: start Step0, place
    Step1, and only after each fill calculate/place the next step. START
    preflight therefore only proves the IMMEDIATE initial sequence is valid:

      1. Step0 MARKET volume meets min base size.
      2. TP0 is a positive, valid venue price (set via x_lighter_agent
         set_tp; the ordinary LIMIT min-quote rule is NOT applied to the TP).
      3. Step1 LIMIT has positive valid price, valid size, valid price
         increment, and notional >= venue minimum.

    Deeper hypothetical steps are NOT evaluated here; each is validated at
    placement time (see validate_next_ladder_step). Never changes the
    requested volume; either accepts or rejects with a reported safe minimum.
    """
    direction = str(direction).upper()
    percentage = Decimal(str(percentage))
    step0_volume = Decimal(str(step0_volume))
    estimated_p0 = Decimal(str(estimated_p0))
    min_base_amount = Decimal(str(min_base_amount or "0"))
    min_quote_amount = Decimal(str(min_quote_amount or "0"))

    estimated_tp0 = golden_fibo_tp_price(direction, estimated_p0, percentage)
    estimated_p1 = golden_fibo_next_ladder_price(direction, estimated_p0, estimated_tp0)

    # --- Step0 MARKET: base-size only (no min-quote LIMIT rule) ---
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

    # --- TP0: positive, valid venue price. TP uses set_tp, so the ordinary
    # LIMIT min-quote rule is NOT applied here. ---
    if estimated_tp0 <= 0:
        return PreflightResult(
            ok=False,
            error="TP0_PRICE_NON_POSITIVE",
            detail=f"TP0 would be {estimated_tp0} (non-positive).",
            estimated_p0=estimated_p0,
            estimated_tp0=estimated_tp0,
            estimated_p1=estimated_p1,
            min_quote_amount=min_quote_amount,
            min_base_amount=min_base_amount,
            size_decimals=size_decimals,
            price_decimals=price_decimals,
        )
    quantized_tp0 = _quantize_down(estimated_tp0, price_decimals)
    if quantized_tp0 <= 0:
        return PreflightResult(
            ok=False,
            error="TP0_PRICE_BELOW_INCREMENT",
            detail=(
                f"TP0 price {estimated_tp0} quantizes to {quantized_tp0}, "
                f"below the venue's minimum price increment."
            ),
            estimated_p0=estimated_p0,
            estimated_tp0=estimated_tp0,
            estimated_p1=estimated_p1,
            min_quote_amount=min_quote_amount,
            min_base_amount=min_base_amount,
            size_decimals=size_decimals,
            price_decimals=price_decimals,
        )

    # --- Step1 LIMIT: positive valid price, valid size, valid increment,
    #     notional >= venue minimum. ---
    step1 = validate_next_ladder_step(
        direction=direction,
        pk=estimated_p0,
        tpk=estimated_tp0,
        volume=step0_volume,
        min_base_amount=min_base_amount,
        min_quote_amount=min_quote_amount,
        size_decimals=size_decimals,
        price_decimals=price_decimals,
        step_n=1,
    )
    reports = [step1.report] if step1.report is not None else []
    if not step1.ok:
        return PreflightResult(
            ok=False,
            error=step1.error,
            detail=step1.detail,
            estimated_p0=estimated_p0,
            estimated_tp0=estimated_tp0,
            estimated_p1=estimated_p1,
            min_quote_amount=min_quote_amount,
            min_base_amount=min_base_amount,
            size_decimals=size_decimals,
            price_decimals=price_decimals,
            safe_min_step0_volume=step1.safe_min_volume,
            failing_step=1,
            failing_raw_price=step1.raw_price,
            percentage=percentage,
            step_reports=reports,
        )

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


@dataclass
class NextStepValidation:
    ok: bool
    error: Optional[str] = None
    detail: Optional[str] = None
    raw_price: Optional[Decimal] = None
    quantized_price: Optional[Decimal] = None
    volume: Optional[Decimal] = None
    notional: Optional[Decimal] = None
    safe_min_volume: Optional[Decimal] = None
    report: Optional[Dict[str, Any]] = None


def validate_next_ladder_step(
    *,
    direction: str,
    pk: Decimal,
    tpk: Decimal,
    volume: Decimal,
    min_base_amount: Decimal,
    min_quote_amount: Decimal,
    size_decimals: int,
    price_decimals: int,
    step_n: int,
) -> NextStepValidation:
    """Validate the NEXT ladder LIMIT order at placement time.

    Used for Step1 at START preflight AND for each later Step(k+1) right
    before it is placed. Checks positive valid price, valid size, valid
    price increment, and notional >= venue minimum. Returns a clear failure
    (do not place) rather than speculating about further steps.
    """
    direction = str(direction).upper()
    pk = Decimal(str(pk))
    tpk = Decimal(str(tpk))
    volume = Decimal(str(volume))
    min_base_amount = Decimal(str(min_base_amount or "0"))
    min_quote_amount = Decimal(str(min_quote_amount or "0"))

    raw_price = golden_fibo_next_ladder_price(direction, pk, tpk)
    quantized_price = _quantize_down(raw_price, price_decimals)
    notional = volume * quantized_price
    report = {
        "step": step_n,
        "volume": str(volume),
        "raw_price": str(raw_price),
        "price": str(quantized_price),
        "notional": str(notional),
    }

    if raw_price <= 0:
        return NextStepValidation(
            ok=False,
            error="LADDER_PRICE_NON_POSITIVE",
            detail=(
                f"Step{step_n} ladder price would be {raw_price} (non-positive); "
                f"not placing the order."
            ),
            raw_price=raw_price,
            quantized_price=quantized_price,
            volume=volume,
            notional=notional,
            report=report,
        )
    if quantized_price <= 0:
        return NextStepValidation(
            ok=False,
            error="LADDER_PRICE_BELOW_INCREMENT",
            detail=(
                f"Step{step_n} ladder price {raw_price} quantizes to "
                f"{quantized_price}, below the venue's minimum price increment."
            ),
            raw_price=raw_price,
            quantized_price=quantized_price,
            volume=volume,
            notional=notional,
            report=report,
        )
    if min_base_amount > 0 and volume < min_base_amount:
        return NextStepValidation(
            ok=False,
            error="LADDER_BELOW_MIN_BASE",
            detail=(
                f"Step{step_n} volume {volume} is below Lighter minimum base "
                f"size {min_base_amount}."
            ),
            raw_price=raw_price,
            quantized_price=quantized_price,
            volume=volume,
            notional=notional,
            report=report,
        )
    if min_quote_amount > 0 and notional < min_quote_amount:
        safe_min = _ceil_to_increment(min_quote_amount / quantized_price, size_decimals)
        err = "STEP1_BELOW_MIN_QUOTE" if step_n == 1 else "LADDER_BELOW_MIN_QUOTE"
        return NextStepValidation(
            ok=False,
            error=err,
            detail=(
                f"Step{step_n} volume {volume} at price {quantized_price} gives "
                f"notional {notional}, below Lighter minimum {min_quote_amount} "
                f"for a LIMIT order. Minimum volume for this step at the current "
                f"estimated price is approximately {safe_min}."
            ),
            raw_price=raw_price,
            quantized_price=quantized_price,
            volume=volume,
            notional=notional,
            safe_min_volume=safe_min,
            report=report,
        )
    report["meets_min_quote"] = True
    return NextStepValidation(
        ok=True,
        raw_price=raw_price,
        quantized_price=quantized_price,
        volume=volume,
        notional=notional,
        report=report,
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

def compute_max_positive_ladder_percentage(
    *,
    direction: str,
    estimated_p0: Decimal,
    max_percentage: Decimal = Decimal("0.5"),
    iterations: int = 60,
) -> Decimal:
    """Binary-search the maximum percentage whose full Step1..Step20 ladder
    keeps every price positive for the given estimated P0.

    Generic (any instrument/direction). For SELL the ladder price rises, so
    the maximum is effectively unbounded within the search range; for BUY
    the ladder price descends and the maximum can be very small.
    """
    direction = str(direction).upper()
    estimated_p0 = Decimal(str(estimated_p0))

    def _ladder_positive(pct: Decimal) -> bool:
        tp = golden_fibo_tp_price(direction, estimated_p0, pct)
        p = estimated_p0
        for _n in range(1, MAX_STEP + 1):
            p_next = p + FIBO_RATIO * (p - tp)
            if p_next <= 0:
                return False
            tp = p
            p = p_next
        return True

    lo = Decimal("0")
    hi = Decimal(str(max_percentage))
    if not _ladder_positive(hi):
        # Narrow the bracket if even the upper bound fails.
        pass
    for _ in range(int(iterations)):
        mid = (lo + hi) / 2
        if _ladder_positive(mid):
            lo = mid
        else:
            hi = mid
    return lo

