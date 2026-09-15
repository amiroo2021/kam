"""VWAP/POC metric tests (wizard semantics)."""

from __future__ import annotations

from goldenfibo.metrics import OhlcvBar, ladder_vwap, metrics_for_legs, volume_profile_poc


def test_ladder_vwap_quote_over_base():
    bars = [
        OhlcvBar(1000, 10, 11, 9, 10, base_volume=2.0, quote_volume=20.0),
        OhlcvBar(2000, 10, 12, 10, 11, base_volume=3.0, quote_volume=33.0),
    ]
    assert ladder_vwap(bars, 1000) == (20 + 33) / (2 + 3)
    assert ladder_vwap(bars, 1500) == 33 / 3


def test_poc_uniform_range_distribution():
    bars = [
        OhlcvBar(1, 100, 110, 100, 105, base_volume=10.0, quote_volume=1050.0),
    ]
    poc = volume_profile_poc(bars, 0, bins=20)
    assert poc is not None
    assert 100 <= poc <= 110


def test_metrics_for_legs_windows():
    bars = [
        OhlcvBar(100, 1, 2, 1, 1.5, 1.0, 1.5),
        OhlcvBar(200, 1.5, 2.5, 1.4, 2.0, 2.0, 4.0),
        OhlcvBar(300, 2.0, 2.2, 1.9, 2.1, 1.0, 2.1),
    ]
    lv, sv, lp, sp = metrics_for_legs(bars, ladder_start_ts_ms=100, step_start_ts_ms=300)
    assert lv is not None and sv is not None
    assert lp is not None and sp is not None
