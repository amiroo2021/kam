"""Exchange-neutral offline ladder planner for spot LIMIT orders.

The planner is exchange-neutral. The caller MUST supply a per-child
minimum notional in the QUOTE asset. Exchanges (e.g. MEXC spot) are
expected to compute that minimum and stamp it on the resolved instrument
record before calling into here. If the caller doesn't supply one, the
planner raises rather than silently applying an exchange-specific default.

The wizard and the agent both invoke ``compute_ladder_with_min_notional``
which takes ``min_notional`` as a top-level argument. A thin
``compute_ladder`` shim is kept for backwards compatibility: it requires
the instrument dict to carry ``min_notional`` and raises otherwise.

INVARIANTS
----------
1. The planner must produce exactly the requested ``order_count`` of
   children or raise a ValueError that explicitly surfaces the
   maximum-valid-child-count. Never silently reduce the count.
2. Sizes are deterministic Decimal results, no floating point.
3. Uniform distribution: each child's size differs from any other by
   no more than one ``size_step``.
4. Half-Gaussian distribution: child sizes are monotonic non-decreasing
   (smallest at START, largest at END) AFTER per-child min-notional
   correction. If correction would break monotonicity, the planner
   raises rather than producing a malformed ladder.
5. The planner never increases total volume beyond the user's request;
   it may redistribute within it.
6. Per-child notional >= min_notional for every child that survives.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Dict, List, Mapping

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
    import math

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
    if start_price <= end_price:
        for i in range(1, len(prices)):
            if prices[i] < prices[i - 1]:
                prices[i] = prices[i - 1]
    else:
        for i in range(1, len(prices)):
            if prices[i] > prices[i - 1]:
                prices[i] = prices[i - 1]
    return prices


def _resolve_steps(instrument: Mapping[str, Any]) -> tuple[Decimal, Decimal]:
    size_step = _decimal_step(instrument.get("size_step") or instrument.get("size_increment"))
    if size_step <= 0:
        size_step = _decimal_step(instrument.get("step_size") or instrument.get("baseSizePrecision"))
    if size_step <= 0:
        size_step = _places_increment(instrument.get("baseAssetPrecision"))
    if size_step <= 0:
        raise ValueError("INSTRUMENT_MISSING_SIZE_STEP")
    price_tick = _decimal_step(instrument.get("price_tick") or instrument.get("price_increment"))
    if price_tick <= 0:
        price_tick = _decimal_step(instrument.get("tick_size"))
    if price_tick <= 0:
        price_tick = _places_increment(instrument.get("quotePrecision"))
    if price_tick <= 0:
        price_tick = _places_increment(instrument.get("quoteAssetPrecision"))
    if price_tick <= 0:
        raise ValueError("INSTRUMENT_MISSING_PRICE_TICK")
    return size_step, price_tick


def _max_valid_children(
    *,
    size_step: Decimal,
    total_volume: Decimal,
    min_qty: Decimal,
    max_qty: Decimal,
    prices: List[Decimal],
) -> int:
    """Compute the maximum number of valid children that can be constructed.

    Considers size_step, min_qty, and max_qty. Per-child min-notional is
    enforced by redistribution after the children are sized; if it can't be
    satisfied, the planner raises separately. This function only bounds the
    lattice (size_step × total_volume) and per-pair quantity caps.
    """
    if size_step <= 0 or not prices:
        return 0
    total_units = int((total_volume / size_step).to_integral_value(rounding=ROUND_DOWN))
    if total_units <= 0:
        return 0
    max_units_per_child = None
    if max_qty > 0:
        max_units_per_child = int((max_qty / size_step).to_integral_value(rounding=ROUND_DOWN))
        if max_units_per_child <= 0:
            return 0
    min_units_per_child = 0
    if min_qty > 0:
        min_units_per_child = int((min_qty / size_step).to_integral_value(rounding=ROUND_HALF_UP))
        if min_units_per_child < 1:
            min_units_per_child = 1
    if min_units_per_child > 0 and max_units_per_child is not None and min_units_per_child > max_units_per_child:
        return 0
    if min_units_per_child > total_units:
        return 0
    return min(len(prices), total_units)


def _enforce_min_notional(
    children: List[LadderChild],
    size_step: Decimal,
    min_notional: Decimal,
    min_qty: Decimal,
    max_qty: Decimal,
) -> List[LadderChild]:
    """Deterministic per-child min-notional correction.

    For each child whose notional is below ``min_notional``, compute the
    required size and pull the deficit from siblings ranked by largest
    notional surplus, ties toward the END. Children that still fail are
    dropped.
    """
    if size_step <= 0:
        return children
    out: List[LadderChild] = [child for child in children]
    for i, child in enumerate(out):
        deficit = min_notional - child.notional
        if deficit <= 0:
            continue
        required_size = min_notional / child.price if child.price > 0 else Decimal("0")
        required_size = _quantize_up(required_size, size_step)
        if min_qty > 0 and required_size < min_qty:
            required_size = _quantize_up(min_qty, size_step)
        if max_qty > 0 and required_size > max_qty:
            required_size = max_qty
        bump = required_size - child.size
        if bump <= 0:
            continue
        sibling_order = sorted(
            range(len(out)),
            key=lambda j: (out[j].notional - min_notional, j),
            reverse=True,
        )
        for j in sibling_order:
            if j == i:
                continue
            surplus = out[j].notional - min_notional
            if surplus <= 0:
                continue
            max_take_size = _quantize_down(surplus / out[j].price, size_step)
            take = min(bump, max_take_size)
            if take <= 0:
                continue
            new_j = LadderChild(
                price=out[j].price,
                size=out[j].size - take,
                notional=(out[j].size - take) * out[j].price,
            )
            new_i = LadderChild(
                price=child.price,
                size=child.size + take,
                notional=(child.size + take) * child.price,
            )
            out[j] = new_j
            out[i] = new_i
            bump -= take
            child = out[i]
            if bump <= 0:
                break
    final = [c for c in out if c.notional >= min_notional and c.size > 0]
    return final


def _uniform_variance_ok(children: List[LadderChild], size_step: Decimal) -> bool:
    if not children or size_step <= 0:
        return True
    sizes = [c.size for c in children]
    return (max(sizes) - min(sizes)) <= size_step


def _monotonic_non_decreasing(children: List[LadderChild]) -> bool:
    sizes = [c.size for c in children]
    return all(prev <= cur for prev, cur in zip(sizes, sizes[1:]))


def _instrument_pair(instrument: Mapping[str, Any]) -> str:
    display = str(instrument.get("display_name") or "").strip()
    if display:
        return display
    base = str(instrument.get("base") or instrument.get("baseAsset") or "").strip().upper()
    quote = str(instrument.get("quote") or instrument.get("quoteAsset") or "").strip().upper()
    if base and quote:
        return f"{base}/{quote}"
    return str(instrument.get("symbol") or "spot pair" or "spot pair").strip().upper()


def _too_few_children_error(
    requested: int,
    max_valid: int,
    *,
    instrument: Mapping[str, Any] | None = None,
    context: str = "",
) -> ValueError:
    pair = _instrument_pair(instrument or {})
    msg = (
        f"Cannot create {requested} valid {pair} orders with the requested "
        f"quantity, price range, and current MEXC constraints.\n"
        f"Maximum valid orders: {max_valid}\n"
        f"Reasons may include:\n"
        f"- minimum 1 USDC/USDT notional per child\n"
        f"- size_step\n"
        f"- min_qty\n"
        f"- max_qty\n"
        f"- available unique price levels at price_tick"
    )
    if context:
        msg += f"\n{context}"
    return ValueError(msg)


def compute_ladder_with_min_notional(
    *,
    side: str,
    distribution: str,
    total_volume: Decimal,
    start_price: Decimal,
    end_price: Decimal,
    order_count: int,
    instrument: Mapping[str, Any],
    min_notional: Decimal,
) -> LadderPlan:
    """Build a deterministic ladder plan. Produces exactly ``order_count``
    children or raises ValueError with a max-valid-child-count message.
    """
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

    size_step, price_tick = _resolve_steps(instrument)

    if min_notional is None or min_notional <= 0:
        raise ValueError(
            "MISSING_MIN_NOTIONAL: caller must supply min_notional in the "
            "QUOTE asset (exchange-specific; e.g. MEXC spot = 1 USDC/USDT)."
        )

    min_qty = _decimal_step(instrument.get("min_qty"))
    max_qty = _decimal_step(instrument.get("max_qty"))

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
    if len(prices) < order_count:
        raise _too_few_children_error(order_count, len(prices), instrument=instrument)

    weights = _ladder_distribution_weights(len(prices), distribution)
    total_weight = sum(weights, Decimal("0"))
    if total_weight <= 0:
        raise ValueError("INVALID_DISTRIBUTION_WEIGHTS")

    total_units = int((total_volume / size_step).to_integral_value(rounding=ROUND_DOWN))
    if total_units <= 0:
        raise ValueError(
            f"INSUFFICIENT_VOLUME_FOR_SIZE_STEP: {total_volume} at {size_step} yields 0 size_steps."
        )

    max_valid = _max_valid_children(
        size_step=size_step,
        total_volume=total_volume,
        min_qty=min_qty,
        max_qty=max_qty,
        prices=prices,
    )
    if max_valid < order_count:
        raise _too_few_children_error(order_count, max_valid, instrument=instrument)

    raw_units = [Decimal(total_units) * w / total_weight for w in weights]
    base_units = [int(u.to_integral_value(rounding=ROUND_DOWN)) for u in raw_units]
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

    if max_qty > 0:
        for i in range(len(sizes)):
            if sizes[i] > max_qty:
                sizes[i] = _quantize_down(max_qty, size_step)

    children: List[LadderChild] = []
    for price, size in zip(prices, sizes):
        if size <= 0:
            continue
        children.append(LadderChild(price=price, size=size))

    children = _enforce_min_notional(
        children,
        size_step=size_step,
        min_notional=min_notional,
        min_qty=min_qty,
        max_qty=max_qty,
    )
    if len(children) != order_count:
        raise _too_few_children_error(
            order_count,
            len(children),
            instrument=instrument,
            context="per-child minimum-notional correction reduced the count",
        )

    if distribution == "half_gaussian":
        if not _monotonic_non_decreasing(children):
            raise ValueError(
                "Half-Gaussian correction violated the monotonic sizing "
                "invariant (smallest at START, largest at END)."
            )
    else:
        if not _uniform_variance_ok(children, size_step):
            raise ValueError(
                f"Uniform correction violated the equal-sizing invariant "
                f"(max variance > size_step {size_step})."
            )

    if not children:
        raise ValueError(
            f"No valid children could be constructed: every price level yields a notional "
            f"below the {min_notional} {instrument.get('quote','USDC')} minimum."
        )

    total_size = sum((c.size for c in children), Decimal("0"))
    if total_size > total_volume:
        total_size = total_volume
    total_notional = sum((c.notional for c in children), Decimal("0"))
    vwap = total_notional / total_size if total_size > 0 else Decimal("0")

    return LadderPlan(
        side=side,
        distribution=distribution,
        children=children,
        total_size=total_size,
        total_notional=total_notional,
        vwap=vwap,
        notes=[],
    )


def compute_ladder(
    *,
    side: str,
    distribution: str,
    total_volume: Decimal,
    start_price: Decimal,
    end_price: Decimal,
    order_count: int,
    instrument: Mapping[str, Any],
) -> LadderPlan:
    """Backwards-compatible wrapper. Reads ``min_notional`` from the
    resolved instrument dict and forwards to
    :func:`compute_ladder_with_min_notional`. Raises if the instrument
    does not carry ``min_notional``.
    """
    mn = _decimal_step(instrument.get("min_notional"))
    if mn <= 0:
        raise ValueError(
            "MISSING_MIN_NOTIONAL: instrument must carry min_notional in "
            "the QUOTE asset (exchange-specific; e.g. MEXC spot = 1 USDC/USDT)."
        )
    return compute_ladder_with_min_notional(
        side=side,
        distribution=distribution,
        total_volume=total_volume,
        start_price=start_price,
        end_price=end_price,
        order_count=order_count,
        instrument=instrument,
        min_notional=mn,
    )


def plan_as_dict(plan: LadderPlan) -> Dict[str, Any]:
    """JSON-friendly shape used by the wizard preview."""
    return {
        "side": plan.side,
        "distribution": plan.distribution,
        "total_size": _format_decimal(plan.total_size),
        "total_notional": _format_decimal(plan.total_notional),
        "vwap": _format_decimal(plan.vwap),
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
