"""Sizing policies — independent of ladder geometry / path logic."""

from __future__ import annotations

from decimal import Decimal

from ..engine.config import EngineConfig, SizingPolicyName


def quantity_for_step(config: EngineConfig, step: int) -> Decimal:
    """Return order/leg quantity for logical step ``n``."""
    if step < 0:
        raise ValueError(f"step must be >= 0, got {step}")
    if config.sizing is SizingPolicyName.LINEAR_RESEARCH:
        return config.base_size + Decimal(step) * config.size_step
    if config.sizing is SizingPolicyName.EXPONENTIAL_LIVE:
        # V0 = V1 = step0; Vn = step0 * 2^(n-1) for n >= 2
        v0 = config.step0_volume
        if step in (0, 1):
            return v0
        return v0 * (Decimal(2) ** (step - 1))
    raise ValueError(f"unknown sizing policy {config.sizing!r}")
