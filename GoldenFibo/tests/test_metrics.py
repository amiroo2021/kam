"""Deterministic VWAP / POC / Value Area tests."""

from __future__ import annotations

from goldenfibo.metrics import (
    OhlcvBar,
    build_volume_profile,
    ladder_vwap,
    metrics_for_legs,
    volume_profile_poc,
    volume_profile_value_area,
)


def test_ladder_vwap_quote_over_base():
    bars = [
        OhlcvBar(1000, 10, 11, 9, 10, base_volume=2.0, quote_volume=20.0),
        OhlcvBar(2000, 10, 12, 10, 11, base_volume=3.0, quote_volume=33.0),
    ]
    assert ladder_vwap(bars, 1000) == (20 + 33) / (2 + 3)
    assert ladder_vwap(bars, 1500) == 33 / 3


def test_poc_is_max_volume_bin_center_synthetic():
    bars = [
        OhlcvBar(1, 100, 101, 100, 100.5, base_volume=1.0, quote_volume=100.0),
        OhlcvBar(2, 100, 101, 100, 100.5, base_volume=1.0, quote_volume=100.0),
        OhlcvBar(3, 109, 110, 109, 109.5, base_volume=100.0, quote_volume=10950.0),
    ]
    poc = volume_profile_poc(bars, 0, bins=20)
    assert poc is not None
    assert poc >= 108.0


def test_value_area_covers_seventy_percent_and_contains_poc():
    bars = [
        OhlcvBar(i, 100 + i * 0.1, 101 + i * 0.1, 99 + i * 0.1, 100 + i * 0.1, 10.0, 1000.0)
        for i in range(30)
    ]
    bars.append(OhlcvBar(1000, 105, 106, 104.5, 105.5, 500.0, 52500.0))
    prof = build_volume_profile(bars, 0, bins=40, value_area_pct=0.70)
    assert prof is not None
    assert prof.val is not None and prof.vah is not None
    assert prof.val <= prof.poc <= prof.vah
    left = int((prof.val - prof.lo) / prof.width) if prof.width else 0
    right = int((prof.vah - prof.lo) / prof.width) - 1 if prof.width else 0
    right = max(left, min(len(prof.vols) - 1, right))
    acc = sum(prof.vols[i] for i in range(left, right + 1))
    assert acc + 1e-9 >= 0.70 * sum(prof.vols)


def test_metrics_for_legs_windows():
    bars = [
        OhlcvBar(100, 1, 2, 1, 1.5, 1.0, 1.5),
        OhlcvBar(200, 1.5, 2.5, 1.4, 2.0, 2.0, 4.0),
        OhlcvBar(300, 2.0, 2.2, 1.9, 2.1, 1.0, 2.1),
    ]
    lv, sv, lp, sp, val, vah = metrics_for_legs(bars, ladder_start_ts_ms=100, step_start_ts_ms=300)
    assert lv is not None and sv is not None
    assert lp is not None and sp is not None
    assert val is not None and vah is not None
    assert val <= vah


def test_value_area_api():
    bars = [OhlcvBar(1, 100, 110, 100, 105, 10.0, 1050.0)]
    val, vah = volume_profile_value_area(bars, 0, bins=20)
    assert val is not None and vah is not None
    assert val <= vah
