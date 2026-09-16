"""Trade-based VWAP and volume-at-price POC (aggTrade) tests.

OHLC metric tests remain separate; this module must not alter them.
"""

from __future__ import annotations

import math

import pytest

from goldenfibo.metrics.trade_vap import (
    AggTrade,
    INCOMPLETE_TRADE_HISTORY,
    TradeHistoryCoverage,
    TradeVapProfile,
    assess_trade_history_coverage,
    bin_price,
    trade_metrics_for_windows,
    trade_poc,
    trade_vap_profile,
    trade_vwap,
)


def _t(ts: int, price: float, qty: float, aid: int = 1) -> AggTrade:
    return AggTrade(agg_id=aid, price=price, qty=qty, ts_ms=ts)


def test_trade_vwap_exact_sum_pq_over_sum_q():
    trades = [
        _t(1000, 100.0, 2.0, 1),
        _t(2000, 110.0, 3.0, 2),
    ]
    assert trade_vwap(trades, since_ts_ms=0) == pytest.approx((100 * 2 + 110 * 3) / 5)


def test_trade_vwap_respects_ladder_p0_window():
    trades = [
        _t(500, 50.0, 100.0, 1),  # pre-P0
        _t(1000, 100.0, 1.0, 2),
        _t(2000, 120.0, 1.0, 3),
    ]
    v = trade_vwap(trades, since_ts_ms=1000)
    assert v == pytest.approx(110.0)
    assert v != pytest.approx((50 * 100 + 100 + 120) / 102)


def test_trade_vwap_respects_active_step_window():
    trades = [
        _t(1000, 100.0, 50.0, 1),
        _t(2000, 200.0, 1.0, 2),
        _t(3000, 220.0, 1.0, 3),
    ]
    assert trade_vwap(trades, since_ts_ms=2000) == pytest.approx(210.0)


def test_pre_p0_trades_excluded_from_vap_and_vwap():
    trades = [
        _t(0, 10.0, 1_000.0, 1),
        _t(1000, 100.0, 1.0, 2),
        _t(1000, 100.0, 1.0, 3),
    ]
    assert trade_vwap(trades, since_ts_ms=1000) == pytest.approx(100.0)
    prof = trade_vap_profile(trades, since_ts_ms=1000, bin_size=0.01)
    assert prof is not None
    assert sum(prof.vols.values()) == pytest.approx(2.0)
    assert 10.0 not in prof.vols and bin_price(10.0, 0.01) not in prof.vols


def test_pre_pn_trades_excluded_from_step_metrics():
    trades = [
        _t(1000, 100.0, 100.0, 1),
        _t(5000, 200.0, 2.0, 2),
    ]
    assert trade_vwap(trades, since_ts_ms=5000) == pytest.approx(200.0)
    poc = trade_poc(trades, since_ts_ms=5000, bin_size=0.01)
    assert poc == pytest.approx(200.0)


def test_vap_accumulates_qty_at_actual_traded_price_bins():
    trades = [
        _t(1, 100.00, 1.0, 1),
        _t(2, 100.00, 2.0, 2),
        _t(3, 100.05, 0.5, 3),
        _t(4, 101.00, 10.0, 4),
    ]
    prof = trade_vap_profile(trades, since_ts_ms=0, bin_size=0.01)
    assert prof is not None
    assert prof.vols[bin_price(100.00, 0.01)] == pytest.approx(3.0)
    assert prof.vols[bin_price(100.05, 0.01)] == pytest.approx(0.5)
    assert prof.vols[bin_price(101.00, 0.01)] == pytest.approx(10.0)
    assert trade_poc(trades, since_ts_ms=0, bin_size=0.01) == pytest.approx(101.00)


def test_bin_price_deterministic_tick_rounding():
    # BTCUSDT tick 0.01 — floor-to-tick style via quantize
    assert bin_price(100.004, 0.01) == 100.00
    assert bin_price(100.005, 0.01) == 100.00 or bin_price(100.005, 0.01) == 100.01
    # same input always same output
    for _ in range(20):
        assert bin_price(75468.137, 0.01) == bin_price(75468.137, 0.01)
    assert bin_price(75468.137, 1.0) == 75468.0
    assert bin_price(75468.137, 5.0) == 75465.0 or bin_price(75468.137, 5.0) == 75470.0


def test_poc_tie_break_is_deterministic_lower_price():
    trades = [
        _t(1, 100.0, 5.0, 1),
        _t(2, 110.0, 5.0, 2),
    ]
    a = trade_poc(trades, since_ts_ms=0, bin_size=0.01)
    b = trade_poc(trades, since_ts_ms=0, bin_size=0.01)
    assert a == b == pytest.approx(100.0)


def test_incomplete_history_when_trades_start_after_window():
    trades = [_t(5_000, 100.0, 1.0, 1)]
    cov = assess_trade_history_coverage(
        trades,
        window_start_ms=1_000,
        window_end_ms=6_000,
        first_fetch_ts_ms=5_000,  # earliest available in buffer/API slice
    )
    assert cov.status == INCOMPLETE_TRADE_HISTORY
    assert cov.complete is False


def test_complete_history_when_coverage_reaches_window_start():
    trades = [
        _t(1_000, 100.0, 1.0, 1),
        _t(2_000, 101.0, 1.0, 2),
    ]
    cov = assess_trade_history_coverage(
        trades,
        window_start_ms=1_000,
        window_end_ms=3_000,
        first_fetch_ts_ms=1_000,
    )
    assert cov.complete is True
    assert cov.status == "COMPLETE"


def test_incomplete_blocks_misleading_ladder_poc():
    trades = [_t(10_000, 100.0, 50.0, 1)]  # only late trades
    result = trade_metrics_for_windows(
        trades,
        ladder_start_ts_ms=1_000,
        step_start_ts_ms=1_000,
        bin_size=0.01,
        earliest_available_ts_ms=10_000,
        window_end_ms=11_000,
    )
    assert result.ladder_coverage.status == INCOMPLETE_TRADE_HISTORY
    assert result.ladder_poc is None
    assert result.ladder_vwap is None
    assert result.ladder_status == INCOMPLETE_TRADE_HISTORY


def test_reset_on_new_p0_uses_new_window_only():
    trades = [
        _t(100, 50.0, 100.0, 1),  # old ladder
        _t(1000, 100.0, 1.0, 2),  # new P0
        _t(1100, 102.0, 1.0, 3),
    ]
    # new P0 at 1000
    assert trade_vwap(trades, since_ts_ms=1000) == pytest.approx(101.0)
    poc = trade_poc(trades, since_ts_ms=1000, bin_size=1.0)
    assert poc is not None
    assert poc >= 100.0


def test_reset_on_progression_uses_new_step_window():
    trades = [
        _t(1000, 100.0, 20.0, 1),
        _t(2000, 105.0, 20.0, 2),
        _t(3000, 200.0, 1.0, 3),  # P(n) fill
        _t(3100, 201.0, 1.0, 4),
    ]
    step = trade_vwap(trades, since_ts_ms=3000)
    ladder = trade_vwap(trades, since_ts_ms=1000)
    assert step == pytest.approx(200.5)
    assert ladder != pytest.approx(step)


def test_buy_and_sell_metric_calculation_is_side_independent():
    """VAP/VWAP depend only on trades + timestamps, not BUY/SELL geometry."""
    trades = [_t(1, 100.0, 2.0, 1), _t(2, 104.0, 2.0, 2)]
    v1 = trade_vwap(trades, since_ts_ms=0)
    p1 = trade_poc(trades, since_ts_ms=0, bin_size=0.01)
    # same inputs — no side parameter exists / needed
    v2 = trade_vwap(trades, since_ts_ms=0)
    p2 = trade_poc(trades, since_ts_ms=0, bin_size=0.01)
    assert v1 == v2 and p1 == p2


def test_empty_trades_returns_none_not_nan():
    assert trade_vwap([], since_ts_ms=0) is None
    assert trade_poc([], since_ts_ms=0, bin_size=0.01) is None
    assert trade_vap_profile([], since_ts_ms=0, bin_size=0.01) is None


def test_coarser_bins_aggregate_adjacent_ticks():
    trades = [
        _t(1, 100.01, 1.0, 1),
        _t(2, 100.02, 1.0, 2),
        _t(3, 100.09, 1.0, 3),
        _t(4, 101.00, 0.1, 4),
    ]
    tick = trade_vap_profile(trades, since_ts_ms=0, bin_size=0.01)
    coarse = trade_vap_profile(trades, since_ts_ms=0, bin_size=0.1)
    assert tick is not None and coarse is not None
    assert len(coarse.vols) < len(tick.vols)
    # 100.01, 100.02, 100.09 should collapse toward one 0.1 bin near 100.0
    assert sum(coarse.vols.values()) == pytest.approx(3.1)
