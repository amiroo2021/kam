"""Pure Golden Fibonacci ladder geometry (no I/O, no sizing)."""

from __future__ import annotations

from decimal import Decimal
from typing import List, Tuple

from .config import Side

PHI = Decimal("1.618")
DEFAULT_PERCENTAGE = Decimal("0.001")
MAX_STEP = 20


def _dec(x: Decimal | int | float | str) -> Decimal:
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def tp0(side: Side | str, p0: Decimal | float | str, percentage: Decimal | float | str = DEFAULT_PERCENTAGE) -> Decimal:
    side = Side(side)
    p0 = _dec(p0)
    percentage = _dec(percentage)
    if side is Side.BUY:
        return p0 * (Decimal("1") + percentage)
    return p0 * (Decimal("1") - percentage)


def ladder_step(
    side: Side | str,
    p0: Decimal | float | str,
    n: int,
    *,
    phi: Decimal | float | str = PHI,
    percentage: Decimal | float | str = DEFAULT_PERCENTAGE,
) -> Tuple[Decimal, Decimal]:
    """Return (P[n], TP[n]) for a cycle anchored at P0.

    Recurrence:
      P[n+1] = P[n] + PHI * (P[n] - TP[n])
      TP[n+1] = P[n]
    """
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    side = Side(side)
    p = _dec(p0)
    phi = _dec(phi)
    percentage = _dec(percentage)
    tp = tp0(side, p, percentage)
    for _ in range(n):
        p, tp = p + phi * (p - tp), p
    return p, tp


def levels_through(
    side: Side | str,
    p0: Decimal | float | str,
    max_n: int,
    *,
    phi: Decimal | float | str = PHI,
    percentage: Decimal | float | str = DEFAULT_PERCENTAGE,
) -> List[Tuple[int, Decimal, Decimal]]:
    """List of (n, P[n], TP[n]) for n = 0..max_n inclusive."""
    out: List[Tuple[int, Decimal, Decimal]] = []
    for n in range(max_n + 1):
        p, tp = ladder_step(side, p0, n, phi=phi, percentage=percentage)
        out.append((n, p, tp))
    return out


def next_entry_and_shared_tp(
    side: Side | str,
    p0: Decimal | float | str,
    highest_filled: int,
    *,
    phi: Decimal | float | str = PHI,
    percentage: Decimal | float | str = DEFAULT_PERCENTAGE,
) -> Tuple[Decimal, Decimal, Decimal]:
    """After highest_filled = hf: (next_p, shared_tp, p_hf)."""
    p_hf, shared_tp = ladder_step(side, p0, highest_filled, phi=phi, percentage=percentage)
    next_p, _ = ladder_step(side, p0, highest_filled + 1, phi=phi, percentage=percentage)
    return next_p, shared_tp, p_hf
