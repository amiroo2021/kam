"""Pure Golden Fibonacci ladder math (no broker / exchange I/O)."""

from __future__ import annotations

from typing import Iterable, List, Tuple

from .constants import BASE_SIZE, Side, SIZE_STEP

PHI = 1.618
PERCENTAGE = 0.001  # 0.1% — step-0 TP distance
MAX_STEP = 20


def lot_size(n: int, base: float = BASE_SIZE, step: float = SIZE_STEP) -> float:
    """Lot[n] = BASE_SIZE + n * SIZE_STEP."""
    if n < 0:
        raise ValueError(f"step n must be >= 0, got {n}")
    return base + n * step


def tp0(side: Side | str, p0: float, percentage: float = PERCENTAGE) -> float:
    side = Side(side)
    if side is Side.BUY:
        return p0 * (1.0 + percentage)
    return p0 * (1.0 - percentage)


def ladder_step(
    side: Side | str,
    p0: float,
    n: int,
    phi: float = PHI,
    percentage: float = PERCENTAGE,
) -> Tuple[float, float]:
    """
    Return (P[n], TP[n]) for side starting at fill price P0 of step 0.

    Recurrence:
      P[n+1]  = P[n] + PHI * (P[n] - TP[n])
      TP[n+1] = P[n]   # previous open becomes next shared TP
    """
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    side = Side(side)
    p = float(p0)
    tp = tp0(side, p, percentage)
    for _ in range(n):
        p, tp = p + phi * (p - tp), p
    return p, tp


def ladder_levels(
    side: Side | str,
    p0: float,
    max_n: int = MAX_STEP,
    phi: float = PHI,
    percentage: float = PERCENTAGE,
) -> List[Tuple[int, float, float]]:
    """List of (n, P[n], TP[n]) for n = 0..max_n inclusive."""
    out: List[Tuple[int, float, float]] = []
    for n in range(max_n + 1):
        p, tp = ladder_step(side, p0, n, phi=phi, percentage=percentage)
        out.append((n, p, tp))
    return out


def phi_ratio(p_n: float, p_n1: float, tp_n: float) -> float:
    """Signed step ratio; use abs(result) to validate PHI magnitude after tick normalize."""
    denom = p_n - tp_n
    if denom == 0:
        return float("nan")
    return (p_n - p_n1) / denom


def next_entry_and_shared_tp(
    side: Side | str,
    p0: float,
    highest_filled: int,
    phi: float = PHI,
    percentage: float = PERCENTAGE,
) -> Tuple[float, float, float]:
    """
    After highest_filled = hf:
      shared_tp = TP[hf]  (== P[hf-1] for hf>=1, else TP0)
      next_p    = P[hf+1]
    Returns (next_p, shared_tp, p_hf).
    """
    p_hf, shared_tp = ladder_step(side, p0, highest_filled, phi=phi, percentage=percentage)
    next_p, _ = ladder_step(side, p0, highest_filled + 1, phi=phi, percentage=percentage)
    return next_p, shared_tp, p_hf


def iter_steps(
    side: Side | str,
    p0: float,
    start: int,
    stop: int,
    phi: float = PHI,
    percentage: float = PERCENTAGE,
) -> Iterable[Tuple[int, float, float]]:
    for n in range(start, stop + 1):
        yield n, *ladder_step(side, p0, n, phi=phi, percentage=percentage)
