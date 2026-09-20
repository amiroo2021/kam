"""Shared ladder distribution math used by /trade (Hyperliquid agent) and WebTrade.

Keep Half-Gaussian and Uniform identical to x_hyperliquid_agent so Telegram
and WebTrade produce the same children from the same inputs.
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import List, Sequence, Tuple


def ladder_distribution_weights(order_count: int, distribution: str) -> List[Decimal]:
    if order_count <= 0:
        return []
    distribution_key = str(distribution or "").strip().lower()
    if distribution_key == "uniform":
        return [Decimal("1")] * order_count
    if distribution_key != "half_gaussian":
        raise ValueError("UNSUPPORTED_DISTRIBUTION")
    if order_count == 1:
        return [Decimal("1")]
    weights: List[Decimal] = []
    span = Decimal(order_count - 1)
    for index in range(order_count):
        z = Decimal("3") * (span - Decimal(index)) / span
        weight = math.exp(-(float(z) ** 2) / 2.0)
        weights.append(Decimal(str(weight)))
    return weights


def quantize_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        raise ValueError("INVALID_INCREMENT")
    units = (value / increment).to_integral_value(rounding=ROUND_HALF_UP)
    return units * increment


def allocate_ladder_sizes(
    total_volume: Decimal,
    order_count: int,
    increment: Decimal,
    distribution: str,
) -> Tuple[List[Decimal], Decimal]:
    if increment <= 0:
        raise ValueError("INVALID_INCREMENT")
    total_units = int((total_volume / increment).to_integral_value(rounding=ROUND_HALF_UP))
    if total_units < order_count:
        raise ValueError("INSUFFICIENT_VOLUME_FOR_ORDER_COUNT")
    weights = ladder_distribution_weights(order_count, distribution)
    if not weights:
        raise ValueError("INVALID_ORDER_COUNT")
    total_weight = sum(weights, Decimal("0"))
    if total_weight <= 0:
        raise ValueError("INVALID_DISTRIBUTION")
    raw_units = [Decimal(total_units) * weight / total_weight for weight in weights]
    base_units = [int(unit.to_integral_value(rounding=ROUND_DOWN)) for unit in raw_units]
    residual = total_units - sum(base_units)
    remainders = [raw_units[index] - Decimal(base_units[index]) for index in range(order_count)]
    allocation = list(base_units)
    if residual > 0:
        order_indices = sorted(range(order_count), key=lambda index: (remainders[index], -index), reverse=True)
        for index in order_indices[:residual]:
            allocation[index] += 1
    sizes = [Decimal(units) * increment for units in allocation]
    return sizes, Decimal(total_units) * increment


def build_ladder_prices_linear(
    start_price: Decimal,
    end_price: Decimal,
    order_count: int,
    price_increment: Decimal,
) -> List[Decimal]:
    """Linear price grid quantized to price_increment (exchange-agnostic)."""
    if order_count <= 0:
        return []
    if order_count == 1:
        mid = (start_price + end_price) / Decimal("2")
        return [quantize_to_increment(mid, price_increment) if price_increment > 0 else mid]
    step = (end_price - start_price) / Decimal(order_count - 1)
    prices: List[Decimal] = []
    for index in range(order_count):
        raw = start_price + (step * Decimal(index))
        px = quantize_to_increment(raw, price_increment) if price_increment > 0 else raw
        prices.append(px)
    if start_price <= end_price:
        for index in range(1, len(prices)):
            if prices[index] < prices[index - 1]:
                prices[index] = prices[index - 1]
    else:
        for index in range(1, len(prices)):
            if prices[index] > prices[index - 1]:
                prices[index] = prices[index - 1]
    return prices


def ladder_vwap(children: Sequence[dict]) -> Decimal:
    """VWAP from FINAL child price × size."""
    notional = Decimal("0")
    total = Decimal("0")
    for child in children:
        px = Decimal(str(child["price"]))
        sz = Decimal(str(child["size"]))
        notional += px * sz
        total += sz
    if total == 0:
        return Decimal("0")
    return notional / total


def build_ladder_children(
    *,
    side: str,
    distribution: str,
    order_count: int,
    total_volume: Decimal,
    start_price: Decimal,
    end_price: Decimal,
    size_increment: Decimal,
    price_increment: Decimal,
) -> Tuple[List[dict], Decimal, Decimal]:
    """Return (children[{price,size}], total_size, vwap)."""
    side_n = str(side or "").strip().lower()
    if side_n == "buy" and end_price >= start_price:
        raise ValueError("INVALID_LADDER_DIRECTION")
    if side_n == "sell" and end_price <= start_price:
        raise ValueError("INVALID_LADDER_DIRECTION")
    prices = build_ladder_prices_linear(start_price, end_price, order_count, price_increment)
    sizes, submitted = allocate_ladder_sizes(total_volume, order_count, size_increment, distribution)
    children: List[dict] = []
    for price, size in zip(prices, sizes):
        if size <= 0:
            continue
        children.append(
            {
                "price": format(price.normalize(), "f"),
                "size": format(size.normalize(), "f"),
            }
        )
    if not children:
        raise ValueError("INVALID_LADDER_REQUEST")
    vwap = ladder_vwap(children)
    return children, submitted, vwap
