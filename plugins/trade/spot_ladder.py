"""Offline /tradespot MEXC ladder planner.

Pure Decimal arithmetic. No live HTTP calls. The wizard consumes this module to
build preview children before any submission. The submission path is gated on a
separate `ladder` capability, which the agent does not advertise yet.

The exchange minimum-notional rule is encoded here as a constant. The minimum
is enforced per child, not per ladder, because MEXC rejects underweight
children with code 30002 "The minimum transaction volume cannot be less than
X USDT".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Dict, List, Mapping, Optional

# MEXC spot enforces a minimum notional per child order, denominated in the
# QUOTE asset. Empirically CCXT reports cost.min = 1.0 USDT/USDC for every
# spot pair tested. MEXC's batchOrders documentation confirms that the error
# is raised in quote currency ("0.5 USDT", "1 USDT", ...). Until MEXC publishes
# a per-symbol minimum, 1 USDC/USDT is the conservative floor.
EXCHANGE_MIN_NOTIONAL_USD = Decimal("1")

_VALID_DISTRIBUTIONS = ("uniform", "half_gaussian")
_VALID_SIDES = ("BUY", "SELL")


@dataclass
class LadderChild:
    price: Decimal
    size: Decimal
    notional: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        self.notional = self.price * self.size


@dataclass
class LadderPlan:
    side: str
    distribution: str
    children: List[LadderChild] = field(default_factory=list)
    total_size: Decimal = Decimal("0")
    total_notional: Decimal = Decimal("0")
    vwap: Decimal = Decimal("0")
    max_valid_children: int = 0
    notes: List[str] = field(default_factory=list)


def _decimal_step(value: Any) -> Decimal:
    text = str(value or "").strip()
    if not text:
        return Decimal("0")
    try:
        inc = Decimal(text)
    except Exception:  # noqa: BLE001
        return Decimal("0")
    return inc if inc > 0 else Decimal("0")


def _places_increment(value: Any) -> Decimal:
    text = str(value or "").strip()
    if not text:
        return Decimal("0")
    try:
        places = int(text)
    except Exception:  # noqa: BLE001
        return Decimal("0")
    if places < 0:
        return Decimal("0")
    return Decimal("1").scaleb(-places)


def _format_decimal(value: Decimal) -> str:
    if value <= 0:
        return ""
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _quantize_down(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        return value
    steps = (value / increment).to_integral_value(rounding=ROUND_DOWN)
    return steps * increment


def _quantize_up(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        return value
    steps = (value / increment).to_integral_value(rounding=ROUND_HALF_UP)
    if (steps * increment) < value:
        steps += 1
    return steps * increment


def _side_direction_ok(side: str, start_price: Decimal, end_price: Decimal) -> bool:
    if side == "BUY":
        return end_price < start_price
    return end_price > start_price


def _ladder_distribution_weights(order_count: int, distribution: str) -> List[Decimal]:
    """Smallest child at START, largest at END.

    Uniform returns equal weights. half_gaussian returns
    ``exp(-((3 * (count-1-i) / (count-1))²) / 2)``. i=0 (START) ⇒ z=3 ⇒
    weight ≈ 0.011 (smallest); i=count-1 (END) ⇒ z=0 ⇒ weight = 1 (largest).
    Matches `ladder_math.ladder_distribution_weights` used by other agents.
    """
    if order_count <= 0:
        return []
    key = str(distribution or "").strip().lower()
    if key not in _VALID_DISTRIBUTIONS:
        raise ValueError(f"INVALID_DISTRIBUTION:{distribution}")
    if key == "uniform":
        return [Decimal("1")] * order_count
    if order_count == 1:
        return [Decimal("1")]
    import math  # imported here so non-half_gaussian code stays pure

    span = Decimal(order_count - 1)
    weights: List[Decimal] = []
    for index in range(order_count):
        z = Decimal("3") * (span - Decimal(index)) / span
        weight = math.exp(-(float(z) ** 2) / 2.0)
        weights.append(Decimal(str(weight)))
    return weights


def _price_grid(
    start_price: Decimal,
    end_price: Decimal,
    order_count: int,
    price_tick: Decimal,
) -> List[Decimal]:
    """Generate N inclusive prices quantized to price_tick with monotonicity."""
    if order_count <= 0:
        return []
    if order_count == 1:
        mid = (start_price + end_price) / Decimal("2")
        return [_quantize_down(mid, price_tick) if price_tick > 0 else mid]
    step = (end_price - start_price) / Decimal(order_count - 1)
    raw = [start_price + step * Decimal(i) for i in range(order_count)]
    prices = [
        _quantize_down(value, price_tick) if price_tick > 0 else value for value in raw
    ]
    # Enforce monotonic direction.
    if start_price <= end_price:
        for i in range(1, len(prices)):
            if prices[i] < prices[i - 1]:
                prices[i] = prices[i - 1]
    else:
        for i in range(1, len(prices)):
            if prices[i] > prices[i - 1]:
                prices[i] = prices[i - 1]
    return prices


def _enforce_min_notional(
    children: List[LadderChild],
    size_step: Decimal,
    price_tick: Decimal,
    min_notional: Decimal,
    min_qty: Decimal,
    side: str,
    distribution: str,
) -> List[LadderChild]:
    """Deterministic per-child min-notional correction.

    Strategy:
    1. For each child whose notional is below ``min_notional``:
       - Compute ``min_qty_at_price = ceil(min_notional / price / size_step) * size_step``
         and apply ``min_qty`` if it is higher.
       - Allocate the deficit by REDUCING the children that already have the
         LARGEST notional surplus (most buffer above the floor). Those are the
         children whose notional is far above ``min_notional`` regardless of
         position in the ladder. This preserves both total requested base
         quantity and distribution ordering: the END children keep their
         priority because they typically have the most surplus when the user
         wants the most weight at END.
    2. Children that still fail after the redistribution are dropped from the
       plan; their slots are not replaced.
    """
    if size_step <= 0 or price_tick <= 0:
        return children
    weights = _ladder_distribution_weights(len(children), distribution)
    out: List[LadderChild] = [child for child in children]
    for i, child in enumerate(out):
        deficit = min_notional - child.notional
        if deficit <= 0:
            continue
        required_size = min_notional / child.price
        required_size = _quantize_up(required_size, size_step)
        if min_qty > 0 and required_size < min_qty:
            required_size = _quantize_up(min_qty, size_step)
        bump = required_size - child.size
        if bump <= 0:
            continue
        # Pull from siblings ranked by largest notional surplus first, breaking
        # ties toward END (matches our half-Gaussian convention: END children
        # are largest, but in the BUY direction the END children also have the
        # most surplus above the notional floor, so we still take from them).
        sibling_order = sorted(
            range(len(out)),
            key=lambda j: (out[j].notional - min_notional, j),
            reverse=True,
        )
        for j in sibling_order:
            if j == i:
                continue
            take = _quantize_down(bump, size_step)
            if take <= 0:
                continue
            # Never take more than the surplus above the notional floor.
            surplus = out[j].notional - min_notional
            if surplus <= 0:
                continue
            max_take_size = _quantize_down(surplus / out[j].price, size_step)
            take = min(take, max_take_size)
            if take <= 0:
                continue
            out[j] = LadderChild(
                price=out[j].price,
                size=out[j].size - take,
                notional=(out[j].size - take) * out[j].price,
            )
            out[i] = LadderChild(
                price=child.price,
                size=child.size + take,
                notional=(child.size + take) * child.price,
            )
            bump -= take
            child = out[i]
            if bump <= 0:
                break
    # Drop children still failing.
    final = [c for c in out if c.notional >= min_notional and c.size > 0]
    return final


def compute_ladder(
    *,
    side: str,
    distribution: str,
    total_volume: Decimal,
    start_price: Decimal,
    end_price: Decimal,
    order_count: int,
    instrument: Mapping[str, Any],
    exchange_min_quote: Optional[Decimal] = None,
) -> LadderPlan:
    """Build a deterministic ladder plan. Raises ValueError on invalid input."""
    side = str(side or "").strip().upper()
    distribution = str(distribution or "").strip().lower()
    if side not in _VALID_SIDES:
        raise ValueError(f"INVALID_SIDE:{side}")
    if distribution not in _VALID_DISTRIBUTIONS:
        raise ValueError(f"INVALID_DISTRIBUTION:{distribution}")
    if order_count <= 0:
        raise ValueError("INVALID_ORDER_COUNT")
    if total_volume <= 0:
        raise ValueError("INVALID_TOTAL_VOLUME")
    if not _side_direction_ok(side, start_price, end_price):
        raise ValueError("INVALID_LADDER_DIRECTION")
    size_step = _decimal_step(
        instrument.get("size_step") or instrument.get("size_increment")
    )
    if size_step <= 0:
        size_step = _decimal_step(
            instrument.get("step_size") or instrument.get("baseSizePrecision")
        )
    if size_step <= 0:
        size_step = _places_increment(instrument.get("baseAssetPrecision"))
    if size_step <= 0:
        raise ValueError("INSTRUMENT_MISSING_SIZE_STEP")
    price_tick = _decimal_step(
        instrument.get("price_tick") or instrument.get("price_increment")
    )
    if price_tick <= 0:
        price_tick = _decimal_step(instrument.get("tick_size"))
    if price_tick <= 0:
        price_tick = _places_increment(instrument.get("quotePrecision"))
    if price_tick <= 0:
        raise ValueError("INSTRUMENT_MISSING_PRICE_TICK")
    min_qty = _decimal_step(instrument.get("min_qty"))
    max_qty = _decimal_step(instrument.get("max_qty"))
    raw_min_notional = _decimal_step(instrument.get("min_notional"))
    if raw_min_notional > 0:
        min_notional = raw_min_notional
    else:
        min_notional = exchange_min_quote if exchange_min_quote and exchange_min_quote > 0 else EXCHANGE_MIN_NOTIONAL_USD

    # Quantize price grid, dedupe while preserving order.
    raw_prices = _price_grid(start_price, end_price, order_count, price_tick)
    seen: Dict[Decimal, int] = {}
    prices: List[Decimal] = []
    for price in raw_prices:
        if price in seen:
            continue
        seen[price] = 1
        prices.append(price)
    if not prices:
        raise ValueError("INVALID_PRICE_LADDER")
    if len(prices) > order_count:
        # More unique levels than requested: keep the first `order_count` along the
        # ladder (preserves START→END ordering).
        prices = prices[:order_count]
    if len(prices) < order_count:
        raise ValueError(
            f"Only {len(prices)} unique price levels available at this instrument's price tick; "
            f"requested {order_count} orders."
        )

    weights = _ladder_distribution_weights(len(prices), distribution)
    total_weight = sum(weights, Decimal("0"))
    if total_weight <= 0:
        raise ValueError("INVALID_DISTRIBUTION_WEIGHTS")

    # Convert total_volume into discrete size_step units.
    total_units = int((total_volume / size_step).to_integral_value(rounding=ROUND_DOWN))
    if total_units <= 0:
        raise ValueError("INSUFFICIENT_VOLUME_FOR_SIZE_STEP")
    if total_units < len(prices):
        # Cap to achievable child count instead of raising: still surface a note
        # so the wizard can tell the user.
        notes_cap = (
            f"Only {total_units} valid size_steps fit at {size_step}; planning "
            f"{total_units} children instead of {len(prices)}."
        )
        prices = prices[:total_units]
        notes: List[str] = [notes_cap]
    elif total_units < order_count:
        notes_cap = (
            f"Requested {order_count} orders but total volume {total_volume} only "
            f"yields {total_units} valid size_steps at {size_step} per child. "
            f"Planning {total_units} children instead."
        )
        prices = prices[:total_units]
        notes: List[str] = [notes_cap]
    else:
        notes = []

    raw_units = [Decimal(total_units) * w / total_weight for w in weights]
    base_units = [int(u.to_integral_value(rounding=ROUND_DOWN)) for u in raw_units]
    # Allocate residual to children with the largest fractional remainder,
    # but BREAK TIES toward the END of the ladder so the distribution bias
    # mirrors "largest weights at END".
    remainders = [raw_units[i] - Decimal(base_units[i]) for i in range(len(raw_units))]
    residual = total_units - sum(base_units)
    if residual > 0:
        order_indices = sorted(
            range(len(prices)),
            key=lambda i: (remainders[i], i),
            reverse=True,
        )
        for idx in order_indices[:residual]:
            base_units[idx] += 1
    sizes = [Decimal(units) * size_step for units in base_units]

    # Enforce max_qty per child.
    if max_qty > 0:
        for i in range(len(sizes)):
            if sizes[i] > max_qty:
                sizes[i] = _quantize_down(max_qty, size_step)

    children: List[LadderChild] = []
    for price, size in zip(prices, sizes):
        if size <= 0:
            continue
        children.append(LadderChild(price=price, size=size))

    if not children:
        raise ValueError("INVALID_LADDER_REQUEST")

    # Per-child min_notional correction (residual redistribution; the
    # function never increases total volume).
    children = _enforce_min_notional(
        children,
        size_step=size_step,
        price_tick=price_tick,
        min_notional=min_notional,
        min_qty=min_qty,
        side=side,
        distribution=distribution,
    )

    # Drop any zero-size children that may have been created by the redistribution.
    children = [c for c in children if c.size > 0]
    if not children:
        raise ValueError(
            f"No valid children could be constructed: every price level yields a notional "
            f"below the {min_notional} {instrument.get('quote','USDC')} minimum."
        )

    # Final sanity: total_size <= requested total_volume.
    total_size = sum((c.size for c in children), Decimal("0"))
    if total_size > total_volume:
        total_size = total_volume

    total_notional = sum((c.notional for c in children), Decimal("0"))
    vwap = total_notional / total_size if total_size > 0 else Decimal("0")

    if len(children) < order_count:
        notes.append(
            f"Only {len(children)} of the requested {order_count} children could be "
            f"constructed while honoring the minimum order value."
        )

    return LadderPlan(
        side=side,
        distribution=distribution,
        children=children,
        total_size=total_size,
        total_notional=total_notional,
        vwap=vwap,
        max_valid_children=len(children),
        notes=notes,
    )


def plan_as_dict(plan: LadderPlan) -> Dict[str, Any]:
    """JSON-friendly shape used by the wizard preview."""
    return {
        "side": plan.side,
        "distribution": plan.distribution,
        "total_size": _format_decimal(plan.total_size),
        "total_notional": _format_decimal(plan.total_notional),
        "vwap": _format_decimal(plan.vwap),
        "max_valid_children": plan.max_valid_children,
        "notes": list(plan.notes),
        "children": [
            {
                "price": _format_decimal(c.price),
                "size": _format_decimal(c.size),
                "notional": _format_decimal(c.notional),
            }
            for c in plan.children
        ],
    }