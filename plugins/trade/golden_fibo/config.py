"""GoldenFibo configuration and volume derivation.

V0 = V1 = step0
V2 = 2 * step0
V3 = 4 * step0
...
Vn = step0 * 2^(n-1) for n >= 2
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


MAX_STEP: int = 20
GOLDENFIBO_SCHEMA_VERSION: int = 1
FIBO_RATIO: Decimal = Decimal("1.618")


def golden_fibo_volume(step0_volume: Decimal, n: int) -> Decimal:
    """Derive the theoretical volume for step n from step0_volume.

    V0 = step0
    V1 = step0
    Vn = step0 * 2^(n-1) for n >= 2
    """
    if n < 0:
        raise ValueError("n must be >= 0")
    if n > MAX_STEP:
        raise ValueError(f"n must be <= MAX_STEP ({MAX_STEP})")
    if n == 0 or n == 1:
        return Decimal(step0_volume)
    return Decimal(step0_volume) * (Decimal(2) ** (n - 1))


def golden_fibo_cumulative_volume(step0_volume: Decimal, through_step: int) -> Decimal:
    """Sum of V0..V(through_step) inclusive."""
    if through_step < 0:
        return Decimal("0")
    return sum((golden_fibo_volume(step0_volume, n) for n in range(through_step + 1)), Decimal("0"))


def golden_fibo_tp_price(direction: str, pk: Decimal, percentage: Decimal) -> Decimal:
    """The TPk for Step k:

    BUY:  TPk = Pk * (1 + percentage)
    SELL: TPk = Pk * (1 - percentage)

    For k >= 1, TPk = P(k-1) (i.e. the previously-filled step's price).
    """
    if direction == "BUY":
        return Decimal(pk) * (Decimal("1") + Decimal(percentage))
    if direction == "SELL":
        return Decimal(pk) * (Decimal("1") - Decimal(percentage))
    raise ValueError(f"direction must be BUY or SELL, got {direction!r}")


def golden_fibo_next_ladder_price(direction: str, pk: Decimal, tpk: Decimal) -> Decimal:
    """P(k+1) = Pk + 1.618 * (Pk - TPk).

    For BUY: TPk < Pk, so P(k+1) < Pk (downward ladder).
    For SELL: TPk > Pk, so P(k+1) > Pk (upward ladder).
    """
    return Decimal(pk) + FIBO_RATIO * (Decimal(pk) - Decimal(tpk))


@dataclass(frozen=True)
class GoldenFiboConfig:
    """Static configuration of a GoldenFibo robot registration."""

    exchange: str
    account: str
    instrument: str
    direction: str  # "BUY" or "SELL"
    percentage: Decimal
    step0_volume: Decimal

    def __post_init__(self) -> None:
        if self.direction not in ("BUY", "SELL"):
            raise ValueError(f"direction must be BUY or SELL, got {self.direction!r}")
        if Decimal(self.percentage) <= 0:
            raise ValueError("percentage must be positive")
        if Decimal(self.step0_volume) <= 0:
            raise ValueError("step0_volume must be positive")

    @property
    def registration_key(self) -> str:
        return f"{self.exchange}/{self.account}/{self.instrument}/{self.direction}"

    def volume(self, n: int) -> Decimal:
        return golden_fibo_volume(self.step0_volume, n)

    def cumulative_volume(self, through_step: int) -> Decimal:
        return golden_fibo_cumulative_volume(self.step0_volume, through_step)

    def tp_price(self, pk: Decimal) -> Decimal:
        return golden_fibo_tp_price(self.direction, pk, self.percentage)

    def next_ladder_price(self, pk: Decimal, tpk: Decimal) -> Decimal:
        return golden_fibo_next_ladder_price(self.direction, pk, tpk)
