"""Display formatting for TradeMenu prices and sizes."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional


def _dec(value: Any) -> Optional[Decimal]:
    if value is None or value == "" or value == "—":
        return None
    try:
        d = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    return d


def _tick_decimals(tick: Any) -> Optional[int]:
    d = _dec(tick)
    if d is None or d <= 0:
        return None
    exp = d.normalize().as_tuple().exponent
    if isinstance(exp, int) and exp < 0:
        return -exp
    return 0


def price_decimals_from_meta(meta: Optional[dict]) -> int:
    """Prefer tick/price_increment; fallback display decimals."""
    if not isinstance(meta, dict):
        return 1
    for key in ("price_increment", "tick_size", "tickSize", "price_tick"):
        decs = _tick_decimals(meta.get(key))
        if decs is not None:
            return min(max(decs, 0), 8)
    for key in ("price_decimals", "priceDecimals", "px_decimals"):
        try:
            n = int(meta.get(key))
            if 0 <= n <= 8:
                return n
        except (TypeError, ValueError):
            pass
    return 1


def size_decimals_from_meta(meta: Optional[dict]) -> int:
    if not isinstance(meta, dict):
        return 6
    for key in ("size_increment", "lot_size", "step_size", "sz_step"):
        decs = _tick_decimals(meta.get(key))
        if decs is not None:
            return min(max(decs, 0), 8)
    for key in ("size_decimals", "sz_decimals"):
        try:
            n = int(meta.get(key))
            if 0 <= n <= 8:
                return n
        except (TypeError, ValueError):
            pass
    return 6


def format_price(value: Any, meta: Optional[dict] = None) -> str:
    d = _dec(value)
    if d is None:
        return "—"
    decs = price_decimals_from_meta(meta)
    q = Decimal(1).scaleb(-decs)
    quantized = d.quantize(q, rounding=ROUND_HALF_UP)
    # thousands separators
    sign = "-" if quantized < 0 else ""
    quantized = abs(quantized)
    text = f"{quantized:.{decs}f}"
    whole, _, frac = text.partition(".")
    whole = f"{int(whole):,}"
    if decs == 0:
        return f"{sign}{whole}"
    # trim trailing zeros but keep at least one decimal if tick requires? keep fixed decs for stability
    return f"{sign}{whole}.{frac}"


def format_size(value: Any, meta: Optional[dict] = None) -> str:
    d = _dec(value)
    if d is None:
        return "—"
    decs = size_decimals_from_meta(meta)
    q = Decimal(1).scaleb(-decs)
    quantized = d.quantize(q, rounding=ROUND_HALF_UP)
    text = format(quantized.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    # add grouping on whole part
    if "." in text:
        whole, frac = text.split(".", 1)
        neg = whole.startswith("-")
        whole = whole.lstrip("-")
        whole = f"{int(whole):,}"
        return f"{'-' if neg else ''}{whole}.{frac}"
    neg = text.startswith("-")
    whole = text.lstrip("-")
    return f"{'-' if neg else ''}{int(whole):,}"


def format_pnl(value: Any, meta: Optional[dict] = None) -> str:
    d = _dec(value)
    if d is None:
        return "—"
    # PnL display: 2 decimals typically (quote currency)
    q = Decimal("0.01")
    quantized = d.quantize(q, rounding=ROUND_HALF_UP)
    sign = "-" if quantized < 0 else ""
    quantized = abs(quantized)
    whole, _, frac = f"{quantized:.2f}".partition(".")
    return f"{sign}{int(whole):,}.{frac}"
