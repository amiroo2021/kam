"""Geometry tests — BUY/SELL ladder, custom percentage, phi recurrence."""

from __future__ import annotations

from decimal import Decimal

import pytest

from goldenfibo import PHI, Side, ladder_step
from goldenfibo.engine.levels import levels_through, tp0


def test_buy_ladder_acceptance_p0_2500():
    p0 = Decimal("2500")
    pct = Decimal("0.001")
    assert ladder_step(Side.BUY, p0, 0, percentage=pct) == (
        Decimal("2500"),
        Decimal("2502.5"),
    )
    p1, tp1 = ladder_step(Side.BUY, p0, 1, percentage=pct)
    assert p1 == Decimal("2495.955")
    assert tp1 == Decimal("2500")
    p2, tp2 = ladder_step(Side.BUY, p0, 2, percentage=pct)
    assert abs(p2 - Decimal("2489.41019")) < Decimal("0.00001")
    assert tp2 == p1


def test_sell_ladder_acceptance_p0_2500():
    p0 = Decimal("2500")
    pct = Decimal("0.001")
    assert ladder_step(Side.SELL, p0, 0, percentage=pct) == (
        Decimal("2500"),
        Decimal("2497.5"),
    )
    p1, tp1 = ladder_step(Side.SELL, p0, 1, percentage=pct)
    assert p1 == Decimal("2504.045")
    assert tp1 == Decimal("2500")


def test_phi_magnitude_buy_and_sell():
    p0 = Decimal("2500")
    b1 = ladder_step(Side.BUY, p0, 1)
    b2 = ladder_step(Side.BUY, p0, 2)
    assert abs((b2[0] - b1[0]) / (b1[0] - b1[1])) == PHI

    s1 = ladder_step(Side.SELL, p0, 1)
    s2 = ladder_step(Side.SELL, p0, 2)
    assert abs((s2[0] - s1[0]) / (s1[0] - s1[1])) == PHI


def test_tp0_custom_percentage():
    assert tp0(Side.BUY, Decimal("100"), Decimal("0.01")) == Decimal("101")
    assert tp0(Side.SELL, Decimal("100"), Decimal("0.01")) == Decimal("99")


def test_tp_n_equals_p_n_minus_1_for_n_ge_1():
    p0 = Decimal("100")
    pct = Decimal("0.01")
    for n in range(1, 6):
        p_n, tp_n = ladder_step(Side.BUY, p0, n, percentage=pct)
        p_prev, _ = ladder_step(Side.BUY, p0, n - 1, percentage=pct)
        assert tp_n == p_prev
        assert p_n != p_prev


def test_levels_through_length():
    levels = levels_through(Side.BUY, Decimal("100"), 5)
    assert len(levels) == 6
    assert levels[0][0] == 0
