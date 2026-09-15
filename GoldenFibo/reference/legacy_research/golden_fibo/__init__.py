"""Golden Fibo virtual (paper) trade — BUY/SELL one-sided Fibonacci ladders."""

from .ladder import PHI, PERCENTAGE, MAX_STEP, ladder_step, ladder_levels, lot_size
from .constants import BGF, SGF, Side

__all__ = [
    "PHI",
    "PERCENTAGE",
    "MAX_STEP",
    "ladder_step",
    "ladder_levels",
    "lot_size",
    "BGF",
    "SGF",
    "Side",
]

__version__ = "0.1.0"
