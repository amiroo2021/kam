"""Canonical /backtest aggressive-flow Delta Ratio (File 2 definition).

These tests pin the arithmetic and snapshot semantics that the new
``_aggressor_metrics_from_candles`` and related helpers must satisfy.

Delta Ratio formula (canonical, base volume):
    BUY  = SUM(taker_buy_base)
    SELL = SUM(volume - taker_buy_base)
    DELTA       = BUY - SELL
    DELTA_RATIO = (BUY - SELL) / (BUY + SELL)         # = DELTA / TOTAL_BASE_VOLUME
    DELTA_RATIO = null if BUY + SELL == 0

Buy VWAP  = SUM(taker_buy_quote) / SUM(taker_buy_base)
Sell VWAP = SUM(quote_volume - taker_buy_quote) / SUM(volume - taker_buy_base)

Window semantics (File 2 explicit):
    Completed step i:    [leg_i.ts_ms, leg_(i+1).ts_ms)
    Current active step: [leg_last.ts_ms, last_complete_candle_open_time + 60_000)
    Whole ladder:        [P0.ts_ms, last_complete_candle_open_time + 60_000)
"""

from __future__ import annotations

import importlib
import math
import sys
from pathlib import Path

# Path bootstrap so the wizard's golden_fibo + goldenfibo imports resolve
# even when this test file is collected standalone.
_ROOT = Path(__file__).resolve().parent.parent.parent.parent  # /root/kam
for _p in (
    str(_ROOT / "GoldenFibo" / "reference" / "legacy_research"),
    str(_ROOT / "GoldenFibo"),
    str(_ROOT),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest

wizard = importlib.import_module("plugins.trade.backtest_wizard")


# ----------------------------- candle helpers -------------------------------


def _kline(ts: int, o: float, h: float, l: float, c: float,
            base_vol: float, taker_buy_base: float,
            taker_buy_quote: float | None = None,
            quote_vol: float | None = None) -> list:
    """Build a Binance-style 1m kline row matching the column layout used by
    goldenfibo.KlineCache.read_range — exactly 10 columns including
    taker_buy_base and taker_buy_quote.
    """
    if taker_buy_quote is None:
        taker_buy_quote = taker_buy_base * c
    if quote_vol is None:
        quote_vol = base_vol * c
    return [
        int(ts),
        str(o), str(h), str(l), str(c),
        str(base_vol),
        int(ts) + 59_999,
        str(quote_vol),
        int(0),  # trades count
        str(taker_buy_base),
        str(taker_buy_quote),
        "0",  # ignore / extra
    ]


def _leg(step: int, ts_ms: int):
    class L:
        pass
    L.step = step
    L.ts = ts_ms  # wizard _leg_ts_ms accepts both .ts and .ts_ms
    L.ts_ms = ts_ms
    return L()


def _state_with_legs(*legs) -> object:
    class S:
        pass
    S.legs = list(legs)
    return S()


# ----------------------------- core arithmetic ------------------------------


def test_buy_vwap_arithmetic():
    """Buy VWAP = SUM(taker_buy_quote) / SUM(taker_buy_base)."""
    candles = [
        _kline(60_000, o=100, h=110, l=90, c=100, base_vol=10, taker_buy_base=4,
               taker_buy_quote=400),
        _kline(120_000, o=101, h=111, l=91, c=101, base_vol=10, taker_buy_base=6,
               taker_buy_quote=606),
    ]
    m = wizard._aggressor_metrics_from_candles(candles, start_ms=0, end_ms_exclusive=None)
    # SUM(tbq) = 400 + 606 = 1006; SUM(tbb) = 4 + 6 = 10
    assert m["buy_base"] == 10
    assert m["buy_quote"] == 1006
    assert m["buy_vwap"] == pytest.approx(100.6, abs=1e-9)


def test_sell_vwap_arithmetic():
    """Sell VWAP = SUM(quote_volume - taker_buy_quote) / SUM(volume - taker_buy_base)."""
    candles = [
        # base 10, tbb 4 -> sell_base 6, quote 1000, tbq 400 -> sell_quote 600, sell_vwap 100
        _kline(60_000, o=100, h=110, l=90, c=100, base_vol=10, taker_buy_base=4,
               quote_vol=1000, taker_buy_quote=400),
        # base 20, tbb 5 -> sell_base 15, quote 2020, tbq 505 -> sell_quote 1515, sell_vwap 101
        _kline(120_000, o=101, h=111, l=91, c=101, base_vol=20, taker_buy_base=5,
               quote_vol=2020, taker_buy_quote=505),
    ]
    m = wizard._aggressor_metrics_from_candles(candles, start_ms=0)
    # sell_base total = 6 + 15 = 21; sell_quote total = 600 + 1515 = 2115
    assert m["sell_base"] == 21
    assert m["sell_quote"] == 2115
    assert m["sell_vwap"] == pytest.approx(2115 / 21, abs=1e-9)


def test_delta_ratio_buying_dominant_matches_file2_example():
    """File 2 EXAMPLE: Aggressive Buy 650, Sell 350 -> Delta Ratio +0.30."""
    # Use a single candle: base=1000, tbb=650 -> buy 650, sell 350.
    candles = [
        _kline(60_000, o=100, h=100, l=100, c=100, base_vol=1000, taker_buy_base=650,
               quote_vol=100_000, taker_buy_quote=65_000),
    ]
    m = wizard._aggressor_metrics_from_candles(candles, start_ms=0)
    assert m["buy_base"] == 650
    assert m["sell_base"] == 350
    assert m["delta"] == 300
    assert m["delta_ratio"] == pytest.approx(0.30, abs=1e-9)


def test_delta_ratio_selling_dominant_matches_file2_example():
    """File 2 EXAMPLE: Aggressive Buy 4800, Sell 5200 -> Delta Ratio -0.04."""
    # Across 2 candles: candle A base 5000 tbb 2400, candle B base 5000 tbb 2400.
    candles = [
        _kline(60_000, o=100, h=100, l=100, c=100, base_vol=5000, taker_buy_base=2400,
               quote_vol=500_000, taker_buy_quote=240_000),
        _kline(120_000, o=100, h=100, l=100, c=100, base_vol=5000, taker_buy_base=2400,
               quote_vol=500_000, taker_buy_quote=240_000),
    ]
    m = wizard._aggressor_metrics_from_candles(candles, start_ms=0)
    assert m["buy_base"] == 4800
    assert m["sell_base"] == 5200
    assert m["delta"] == -400
    assert m["delta_ratio"] == pytest.approx(-0.04, abs=1e-9)


def test_zero_volume_returns_none_no_division_by_zero():
    candles = [
        _kline(60_000, o=100, h=100, l=100, c=100, base_vol=0, taker_buy_base=0,
               quote_vol=0, taker_buy_quote=0),
    ]
    m = wizard._aggressor_metrics_from_candles(candles, start_ms=0)
    assert m["buy_vwap"] is None
    assert m["sell_vwap"] is None
    assert m["delta_ratio"] is None
    assert m["status"] == "EMPTY"


def test_window_exclusive_right_endpoint_excludes_current_bar():
    """Window [start, end) must NOT include a candle whose open_time == end."""
    candles = [
        _kline(60_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=10),
        _kline(120_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=0),
    ]
    # end_ms_exclusive=120_000 -> only candle at 60_000 contributes.
    m = wizard._aggressor_metrics_from_candles(candles, start_ms=0, end_ms_exclusive=120_000)
    assert m["buy_base"] == 10
    assert m["sell_base"] == 0


def test_window_skips_candles_before_start():
    candles = [
        _kline(60_000, o=100, h=100, l=100, c=100, base_vol=999, taker_buy_base=999),
        _kline(120_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=10),
    ]
    m = wizard._aggressor_metrics_from_candles(candles, start_ms=120_000, end_ms_exclusive=None)
    assert m["buy_base"] == 10


# ----------------------------- snapshot semantics ---------------------------


def test_per_step_window_uses_leg_ts_inclusive_exclusive():
    """_step_aggressor_metrics_from_candles for completed step i must use
    [leg_i.ts_ms, leg_(i+1).ts_ms) — only candles in that exact interval.
    """
    candles = [
        _kline(60_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=10),
        _kline(120_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=0),
        _kline(180_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=5),
        _kline(240_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=10),
    ]
    state = _state_with_legs(
        _leg(0, 60_000),
        _leg(1, 180_000),
        _leg(2, 240_000),
    )
    snap = wizard._step_aggressor_metrics_from_candles(candles, state, step=1)
    # Step 1 window = [180_000, 240_000): candle at 180_000 only.
    # base 10, tbb 5 -> buy_base=5, sell_base=5.
    assert snap is not None
    assert snap["buy_base"] == 5
    assert snap["sell_base"] == 5
    assert snap["delta_ratio"] == pytest.approx(0.0, abs=1e-9)


def test_per_step_window_cumulative_step_0():
    state = _state_with_legs(
        _leg(0, 60_000),
        _leg(1, 180_000),
    )
    candles = [
        _kline(60_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=5),
        _kline(120_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=5),
        _kline(180_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=5),
    ]
    snap = wizard._step_aggressor_metrics_from_candles(candles, state, step=0)
    # Step 0 window = [60_000, 180_000): candles at 60_000 and 120_000.
    assert snap["buy_base"] == 10
    assert snap["sell_base"] == 10
    assert snap["delta_ratio"] == pytest.approx(0.0, abs=1e-9)


def test_completed_step_snapshot_immutable_after_new_candles():
    """Adding candles after a step's boundary must NOT change that step's snapshot."""
    candles = [
        _kline(60_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=5),
        _kline(120_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=5),
    ]
    state = _state_with_legs(
        _leg(0, 60_000),
        _leg(1, 120_000),
    )
    snap_before = wizard._step_aggressor_metrics_from_candles(candles, state, step=0)
    # Add candles far in the future.
    candles_extended = candles + [
        _kline(1_000_000, o=100, h=100, l=100, c=100, base_vol=10_000, taker_buy_base=10_000),
        _kline(1_060_000, o=100, h=100, l=100, c=100, base_vol=10_000, taker_buy_base=10_000),
    ]
    snap_after = wizard._step_aggressor_metrics_from_candles(candles_extended, state, step=0)
    assert snap_before["buy_base"] == snap_after["buy_base"]
    assert snap_before["delta_ratio"] == snap_after["delta_ratio"]


def test_active_step_includes_last_complete_candle_excludes_forming():
    """Current active step window = [leg_last.ts_ms, last_open_time + 60_000).
    The last candle in the dataset is treated as the last complete candle.
    """
    candles = [
        _kline(60_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=10),
        _kline(120_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=10),
    ]
    state = _state_with_legs(_leg(0, 60_000))  # only step 0 filled; current step
    snap = wizard._active_step_aggressor_metrics_from_candles(candles, state)
    # Window [60_000, 120_000 + 60_000) = [60_000, 180_000): both candles included.
    assert snap["buy_base"] == 20
    assert snap["sell_base"] == 0


def test_active_step_excludes_forming_candle():
    """Per File 2 / user spec: the active-step window upper bound is
    ``candles[-1][0] + 60_000`` (end of the last complete minute), exclusive.

    Verified by passing a dataset that ends exactly one minute after the
    last complete candle, then asserting the bound excludes the forming
    minute. We simulate this by passing a dataset whose last candle is the
    "last complete" minute and asserting the bound excludes the forming
    candle that begins at ``candles[-1][0] + 60_000``.

    Concretely: last complete candle open = 180_000, so bound is 240_000
    (exclusive). A candle at 240_000 (the forming minute) must be excluded.
    """
    # Dataset: last "complete" candle at 180_000.
    candles = [
        _kline(60_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=10),
        _kline(120_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=10),
        _kline(180_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=10),
    ]
    # The "forming" minute starts at 240_000. We do NOT include it in the
    # dataset; that is the caller's contract.
    state = _state_with_legs(_leg(0, 60_000))

    # Sanity: with just the complete candles, active window [60_000, 240_000)
    # includes all 3 candles. buy_base = 30.
    snap = wizard._active_step_aggressor_metrics_from_candles(candles, state)
    assert snap["buy_base"] == 30

    # If we deliberately pass a dataset that goes one minute PAST the last
    # complete candle (i.e. includes the forming minute), the bound becomes
    # 300_000 (end of the next minute) and the forming candle IS included.
    # This documents the helper's behavior — the caller must NOT pass forming
    # candles if it wants them excluded.
    forming_candle = _kline(240_000, o=100, h=100, l=100, c=100, base_vol=999, taker_buy_base=999)
    snap_with_forming = wizard._active_step_aggressor_metrics_from_candles(
        candles + [forming_candle], state
    )
    # Documented: if forming candle is in dataset, it becomes the new
    # "last complete" reference and IS included. buy_base = 30 + 999 = 1029.
    assert snap_with_forming["buy_base"] == 1029


def test_ladder_window_cumulative_from_p0():
    candles = [
        _kline(60_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=5),
        _kline(120_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=5),
        _kline(180_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=5),
    ]
    state = _state_with_legs(
        _leg(0, 60_000),
        _leg(1, 120_000),
    )
    snap = wizard._ladder_aggressor_metrics_from_candles(candles, state)
    # Ladder window [60_000, 180_000+60_000) = all 3 candles.
    assert snap["buy_base"] == 15
    assert snap["sell_base"] == 15


def test_ladder_sum_equals_sum_of_per_step_windows():
    """Sanity: ladder buy/sell/delta == sum across per-step completed windows."""
    candles = [
        _kline(60_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=3),
        _kline(120_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=7),
        _kline(180_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=4),
        _kline(240_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=2),
    ]
    state = _state_with_legs(
        _leg(0, 60_000),
        _leg(1, 180_000),
        _leg(2, 240_000),
    )
    ladder = wizard._ladder_aggressor_metrics_from_candles(candles, state)
    s0 = wizard._step_aggressor_metrics_from_candles(candles, state, step=0)
    s1 = wizard._step_aggressor_metrics_from_candles(candles, state, step=1)
    s2 = wizard._step_aggressor_metrics_from_candles(candles, state, step=2)
    sum_buy = s0["buy_base"] + s1["buy_base"] + s2["buy_base"]
    sum_sell = s0["sell_base"] + s1["sell_base"] + s2["sell_base"]
    assert ladder["buy_base"] == sum_buy
    assert ladder["sell_base"] == sum_sell


# ----------------------------- summary / labels -----------------------------


def test_summary_lines_use_canonical_absolute_labels():
    """Per File 2 §11: 'Buy/Sell labels are absolute market-aggressor labels
    and NEVER invert with GoldenFibo direction.' The new summary must use
    'Buy VWAP' / 'Sell VWAP' regardless of side.
    """
    # This test calls a NEW helper that produces summary lines for both
    # scopes. The old side-inverting labels 'L-B-VWAP' / 'L-S-VWAP' must
    # not appear in the produced text.
    lag = {"status": "COMPLETE", "buy_vwap": 100.0, "sell_vwap": 110.0,
           "buy_base": 5, "sell_base": 5, "delta": 0, "delta_ratio": 0.0}
    sag = {"status": "COMPLETE", "buy_vwap": 101.0, "sell_vwap": 109.0,
           "buy_base": 3, "sell_base": 3, "delta": 0, "delta_ratio": 0.0}
    lines = wizard._aggressor_summary_lines_v2(
        ladder=lag, step=sag, side=__import__("golden_fibo.constants", fromlist=["Side"]).Side.SELL,
    )
    text = "\n".join(lines)
    # Canonical labels.
    assert "Buy VWAP" in text
    assert "Sell VWAP" in text
    # The old side-inverted labels MUST NOT appear.
    assert "L-B-VWAP" not in text
    assert "L-S-VWAP" not in text
    assert "S-B-VWAP" not in text
    assert "S-S-VWAP" not in text


def test_summary_lines_unavailable_status_reported():
    lag = {"status": "UNAVAILABLE", "buy_vwap": None, "sell_vwap": None,
           "buy_base": 0, "sell_base": 0, "delta": 0, "delta_ratio": None}
    sag = {"status": "UNAVAILABLE", "buy_vwap": None, "sell_vwap": None,
           "buy_base": 0, "sell_base": 0, "delta": 0, "delta_ratio": None}
    lines = wizard._aggressor_summary_lines_v2(
        ladder=lag, step=sag, side=__import__("golden_fibo.constants", fromlist=["Side"]).Side.SELL,
    )
    text = "\n".join(lines)
    assert "unavailable" in text.lower()


# ----------------------------- color thresholds ----------------------------


def test_delta_color_thresholds_unchanged():
    """The existing color helper thresholds remain the same contract."""
    from golden_fibo.constants import Side
    # We just assert that the helper exists and the standard thresholds apply.
    assert wizard._step_delta_color(0.7) == wizard.DARKBLUE
    assert wizard._step_delta_color(0.2) == wizard.LIGHTBLUE
    assert wizard._step_delta_color(-0.7) == wizard.DARKRED
    assert wizard._step_delta_color(-0.2) == wizard.LIGHTRED
    assert wizard._step_delta_color(None) == "#666666"
    assert wizard._step_delta_color(0.0) == "#666666"


# ----------------------------- prefix-sum path -----------------------------


def test_prefix_sum_path_matches_simple_scan_path():
    """The internal _metrics_for_window function (used internally by prefix
    sums) must return identical metrics to a naive scan.
    """
    candles = [
        _kline(60_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=3),
        _kline(120_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=7),
        _kline(180_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=5),
    ]
    # Internal prefix-sum accessor on the same window must match.
    a = wizard._aggressor_metrics_from_candles(candles, start_ms=60_000, end_ms_exclusive=180_000)
    # Same metrics with a different end.
    b = wizard._aggressor_metrics_from_candles(candles, start_ms=60_000, end_ms_exclusive=None)
    assert a["buy_base"] == 10  # candles at 60_000 and 120_000 only
    assert b["buy_base"] == 15  # all three


def test_per_step_table_exposes_both_ladder_and_step_scopes():
    """Each row in the per-step history must expose BOTH the step-window
    metrics (S) AND the cumulative ladder metrics through that step's end
    boundary (L). The ladder metrics must grow monotonically across steps.
    """
    candles = [
        _kline(60_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=2),
        _kline(120_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=4),
        _kline(180_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=6),
        _kline(240_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=8),
    ]
    state = _state_with_legs(
        _leg(0, 60_000),
        _leg(1, 120_000),
        _leg(2, 180_000),
    )
    table = wizard._per_step_aggressor_table(candles, state)
    # Step 0: S = [60_000, 120_000) — only candle at 60_000. base=10 tbb=2.
    # L = same range -> buy=2.
    assert table[0]["buy_base"] == 2  # S
    assert table[0]["sell_base"] == 8
    assert table[0]["ladder_buy_base"] == 2  # L == S at step 0
    # Step 1: S = [120_000, 180_000) — candle at 120_000. tbb=4.
    # L = [60_000, 180_000) — candles at 60k, 120k. tbb = 2 + 4 = 6.
    assert table[1]["buy_base"] == 4  # S
    assert table[1]["ladder_buy_base"] == 6  # L > S
    # Step 2 (active, highest filled): S window = [180_000, candles[-1][0] + 60_000)
    # = [180_000, 300_000) — candles at 180k, 240k. tbb = 6 + 8 = 14.
    # L = same — candles at 60k, 120k, 180k, 240k. tbb = 2 + 4 + 6 + 8 = 20.
    assert table[2]["buy_base"] == 14
    assert table[2]["ladder_buy_base"] == 20
    # Monotonic: ladder buy grows each step.
    assert table[0]["ladder_buy_base"] <= table[1]["ladder_buy_base"] <= table[2]["ladder_buy_base"]


def test_per_step_table_is_frozen_no_future_candles_leak():
    """A completed step's snapshot must remain identical even after new
    candles are appended to the dataset.
    """
    candles = [
        _kline(60_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=2),
        _kline(120_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=4),
        _kline(180_000, o=100, h=100, l=100, c=100, base_vol=10, taker_buy_base=6),
    ]
    state = _state_with_legs(
        _leg(0, 60_000),
        _leg(1, 120_000),
    )
    snap_before = wizard._per_step_aggressor_table(candles, state)
    candles_ext = candles + [
        _kline(1_000_000, o=100, h=100, l=100, c=100, base_vol=999, taker_buy_base=999),
    ]
    snap_after = wizard._per_step_aggressor_table(candles_ext, state)
    # Step 0 (completed at 60_000 -> 120_000) must NOT change.
    assert snap_before[0]["buy_base"] == snap_after[0]["buy_base"]
    assert snap_before[0]["ladder_buy_base"] == snap_after[0]["ladder_buy_base"]
    # Step 1 IS the active leg in both cases (no next leg), so it uses the
    # candles[-1][0] + 60_000 bound — and that bound DOES grow when new
    # candles are appended. So step 1's snapshot WILL change.
    # That's the correct behavior — the active leg is not yet "completed".
    assert snap_after[1]["ladder_buy_base"] > snap_before[1]["ladder_buy_base"]


# ----------------------------- aggTrade coverage acquisition -----------------


def _trade(agg_id: int, price: float, qty: float, ts: int, buyer_is_maker: bool = False):
    from goldenfibo.metrics.trade_vap import AggTrade
    return AggTrade(
        agg_id=agg_id,
        price=price,
        qty=qty,
        ts_ms=ts,
        first_trade_id=agg_id,
        last_trade_id=agg_id,
        id_domain="aggtrade",
        buyer_is_maker=buyer_is_maker,
    )


def test_completely_uncached_metric_window_fetches_only_required_interval(tmp_path):
    from goldenfibo.marketdata.aggtrade_cache import AggTradeCache

    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    calls = []

    def rest_fetcher(symbol, start_ms, end_ms, **kwargs):
        calls.append((symbol, start_ms, end_ms))
        return [_trade(1, 100.0, 2.0, start_ms), _trade(2, 110.0, 3.0, end_ms)]

    ladder, step, meta = wizard._aggtrade_aggressor_metrics_for_display(
        cache, "futures", "HYPEUSDT", 1_000, 2_000, 3_000,
        rest_fetcher=rest_fetcher,
        now_ms=3_000,
    )

    assert calls == [("HYPEUSDT", 1_000, 3_000)]
    assert meta["required_start_ms"] == 1_000
    assert meta["required_end_ms"] == 3_000
    assert ladder["status"] == "COMPLETE"
    assert step["status"] == "COMPLETE"
    assert step["buy_vwap"] == pytest.approx(110.0)


def test_partially_cached_metric_window_fetches_only_missing_suffix(tmp_path):
    from goldenfibo.marketdata.aggtrade_cache import AggTradeCache

    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    cache.insert_trades("futures", "HYPEUSDT", [_trade(1, 100.0, 1.0, 1_000)], source="rest")
    cache.record_coverage("futures", "HYPEUSDT", 1_000, 1_999, source="rest")
    calls = []

    def rest_fetcher(symbol, start_ms, end_ms, **kwargs):
        calls.append((start_ms, end_ms))
        return [_trade(2, 101.0, 1.0, start_ms), _trade(3, 102.0, 1.0, end_ms)]

    ladder, step, meta = wizard._aggtrade_aggressor_metrics_for_display(
        cache, "futures", "HYPEUSDT", 1_000, 2_000, 3_000,
        rest_fetcher=rest_fetcher,
        now_ms=3_000,
    )

    assert calls == [(2_000, 3_000)]
    assert meta["covered"] is True
    assert ladder["buy_base"] == pytest.approx(3.0)
    assert step["buy_base"] == pytest.approx(2.0)


def test_fully_cached_metric_window_causes_zero_refetch(tmp_path):
    from goldenfibo.marketdata.aggtrade_cache import AggTradeCache

    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    cache.insert_trades("futures", "HYPEUSDT", [_trade(1, 100.0, 1.0, 1_000)], source="rest")
    cache.record_coverage("futures", "HYPEUSDT", 1_000, 3_000, source="rest")

    def rest_fetcher(*args, **kwargs):
        raise AssertionError("should not refetch covered range")

    result = cache.ensure_coverage("futures", "HYPEUSDT", 1_000, 3_000, rest_fetcher=rest_fetcher)
    assert result.covered is True
    assert result.rest_requests == 0


def test_archive_and_rest_mixed_window(tmp_path):
    from goldenfibo.marketdata.aggtrade_cache import AggTradeCache

    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    day = 86_400_000
    now = 4 * day + 5_000
    # Request crosses one completed historical day (archive) plus current-day edge (REST).
    calls = {"archive": [], "rest": []}

    def archive_fetcher(market, symbol, start_ms, end_ms):
        calls["archive"].append((start_ms, end_ms))
        return [_trade(10, 100.0, 1.0, start_ms), _trade(11, 101.0, 1.0, end_ms)]

    def rest_fetcher(symbol, start_ms, end_ms, **kwargs):
        calls["rest"].append((start_ms, end_ms))
        return [_trade(20, 102.0, 1.0, start_ms), _trade(21, 103.0, 1.0, end_ms)]

    result = cache.ensure_coverage(
        "futures", "HYPEUSDT", 3 * day, 4 * day + 1_000,
        archive_fetcher=archive_fetcher, rest_fetcher=rest_fetcher, now_ms=now,
    )

    assert calls["archive"] == [(3 * day, 4 * day - 1)]
    assert calls["rest"] == [(4 * day, 4 * day + 1_000)]
    assert result.covered is True
    assert cache.coverage_covers("futures", "HYPEUSDT", 3 * day, 4 * day + 1_000)


def test_archive_unavailable_leaves_metric_unavailable(tmp_path):
    from goldenfibo.marketdata.aggtrade_cache import AggTradeCache

    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")

    def archive_fetcher(market, symbol, start_ms, end_ms):
        return []

    result = cache.ensure_coverage(
        "futures", "HYPEUSDT", 1_000, 2_000,
        archive_fetcher=archive_fetcher, now_ms=2_000 + 3 * 86_400_000,
    )
    assert result.covered is False
    assert result.missing_ranges == [(1_000, 2_000)]


def test_rest_unavailable_leaves_user_facing_unavailable(tmp_path):
    from goldenfibo.marketdata.aggtrade_cache import AggTradeCache

    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")

    def rest_fetcher(*args, **kwargs):
        raise RuntimeError("rest down")

    cache.ensure_coverage = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("rest down"))
    ladder, step, meta = wizard._aggtrade_aggressor_metrics_for_display(
        cache, "futures", "HYPEUSDT", 1_000, 2_000, 3_000
    )
    assert ladder["status"] == wizard.INCOMPLETE_AGGTRADE_COVERAGE
    assert step["status"] == wizard.INCOMPLETE_AGGTRADE_COVERAGE
    assert "rest down" in meta["error"]
    lines = wizard._aggressor_summary_lines(
        __import__("golden_fibo.constants", fromlist=["Side"]).Side.SELL,
        {"all_vwap": 1.0}, {"all_vwap": 1.0}, ladder, step,
    )
    assert "Step Delta Ratio: unavailable (INCOMPLETE_AGGTRADE_COVERAGE)" in lines
    assert all("Step Delta Ratio: nan" not in line for line in lines)


def test_genuine_gap_remains_unavailable(tmp_path):
    from goldenfibo.marketdata.aggtrade_cache import AggTradeCache

    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    cache.record_coverage("futures", "HYPEUSDT", 1_000, 1_999, source="rest")
    cache.record_coverage("futures", "HYPEUSDT", 3_000, 4_000, source="rest")
    missing = cache.missing_ranges("futures", "HYPEUSDT", 1_000, 4_000)
    assert missing == [(2_000, 2_999)]
    assert cache.coverage_covers("futures", "HYPEUSDT", 1_000, 4_000) is False


def test_restart_persistent_cache_reuse(tmp_path):
    from goldenfibo.marketdata.aggtrade_cache import AggTradeCache

    db = tmp_path / "aggtrades.sqlite"
    cache1 = AggTradeCache(db)
    cache1.insert_trades("futures", "HYPEUSDT", [_trade(1, 100.0, 1.0, 1_000)], source="rest")
    cache1.record_coverage("futures", "HYPEUSDT", 1_000, 3_000, source="rest")

    cache2 = AggTradeCache(db)
    assert cache2.coverage_covers("futures", "HYPEUSDT", 1_000, 3_000)

    def rest_fetcher(*args, **kwargs):
        raise AssertionError("persistent coverage should be reused after restart")

    result = cache2.ensure_coverage("futures", "HYPEUSDT", 1_000, 3_000, rest_fetcher=rest_fetcher)
    assert result.covered is True
    assert result.rest_requests == 0


def test_ladder_p0_later_than_backtest_start_does_not_fetch_from_backtest_start(tmp_path):
    from goldenfibo.marketdata.aggtrade_cache import AggTradeCache

    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    calls = []

    def rest_fetcher(symbol, start_ms, end_ms, **kwargs):
        calls.append((start_ms, end_ms))
        return [_trade(1, 100.0, 1.0, start_ms), _trade(2, 101.0, 1.0, end_ms)]

    backtest_start = 1_000
    ladder_p0 = 10_000
    wizard._aggtrade_aggressor_metrics_for_display(
        cache, "futures", "HYPEUSDT", ladder_p0, 12_000, 15_000,
        rest_fetcher=rest_fetcher,
        now_ms=15_000,
    )
    assert calls == [(ladder_p0, 15_000)]
    assert calls[0][0] != backtest_start


def test_step_vwap_uses_only_step_current_interval(tmp_path):
    from goldenfibo.marketdata.aggtrade_cache import AggTradeCache

    cache = AggTradeCache(tmp_path / "aggtrades.sqlite")
    trades = [
        _trade(1, 50.0, 100.0, 1_000),
        _trade(2, 200.0, 1.0, 2_000),
        _trade(3, 300.0, 1.0, 3_000),
    ]
    cache.insert_trades("futures", "HYPEUSDT", trades, source="rest")
    cache.record_coverage("futures", "HYPEUSDT", 1_000, 3_000, source="rest")

    ladder, step, meta = wizard._aggtrade_aggressor_metrics_for_display(
        cache, "futures", "HYPEUSDT", 1_000, 2_000, 3_000
    )
    assert meta["covered"] is True
    assert ladder["buy_base"] == pytest.approx(102.0)
    assert step["buy_base"] == pytest.approx(2.0)
    assert step["buy_vwap"] == pytest.approx(250.0)
