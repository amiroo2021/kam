"""Canonical Binance symbol helpers."""

from __future__ import annotations


def canonical_binance_symbol(symbol: str) -> str:
    s = str(symbol or "").upper().replace("/", "")
    if s.endswith("USDT"):
        return s
    return s + "USDT"
