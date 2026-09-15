"""Sizing policy tests — independent of path geometry."""

from __future__ import annotations

from decimal import Decimal

from goldenfibo import EngineConfig, Side, SizingPolicyName
from goldenfibo.simulation.sizing import quantity_for_step


def test_linear_research_default_curve():
    cfg = EngineConfig(side=Side.BUY, sizing=SizingPolicyName.LINEAR_RESEARCH)
    assert quantity_for_step(cfg, 0) == Decimal("0.001")
    assert quantity_for_step(cfg, 1) == Decimal("0.0011")
    assert quantity_for_step(cfg, 2) == Decimal("0.0012")
    assert quantity_for_step(cfg, 5) == Decimal("0.0015")


def test_exponential_live_curve():
    cfg = EngineConfig(
        side=Side.BUY,
        sizing=SizingPolicyName.EXPONENTIAL_LIVE,
        step0_volume=Decimal("0.001"),
    )
    assert quantity_for_step(cfg, 0) == Decimal("0.001")
    assert quantity_for_step(cfg, 1) == Decimal("0.001")
    assert quantity_for_step(cfg, 2) == Decimal("0.002")
    assert quantity_for_step(cfg, 3) == Decimal("0.004")
    assert quantity_for_step(cfg, 4) == Decimal("0.008")
    assert quantity_for_step(cfg, 5) == Decimal("0.016")


def test_sizing_does_not_change_geometry():
    from goldenfibo import ladder_step

    p0 = Decimal("2500")
    a = ladder_step(Side.BUY, p0, 3)
    b = ladder_step(Side.BUY, p0, 3)
    assert a == b
    # quantities differ by policy but levels identical regardless of config sizing
    cfg_l = EngineConfig(side=Side.BUY, sizing=SizingPolicyName.LINEAR_RESEARCH)
    cfg_e = EngineConfig(side=Side.BUY, sizing=SizingPolicyName.EXPONENTIAL_LIVE)
    assert quantity_for_step(cfg_l, 3) != quantity_for_step(cfg_e, 3)
