"""Parity: recovered legacy research package vs canonical GoldenFiboEngine."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

# Frozen oracle — reference snapshot only (not runtime dependency of package).
_LEGACY_ROOT = Path(__file__).resolve().parents[1] / "reference" / "legacy_research"
sys.path.insert(0, str(_LEGACY_ROOT))

from golden_fibo.constants import Side as LegacySide  # noqa: E402
from golden_fibo.historical_replay import replay_ohlc as legacy_replay  # noqa: E402
from golden_fibo.ladder import ladder_step as legacy_ladder_step  # noqa: E402

from goldenfibo import Side, ladder_step  # noqa: E402
from goldenfibo.feeders.historical_ohlc import replay_ohlc_legacy  # noqa: E402


def ms(dt: str) -> int:
    return int(datetime.fromisoformat(dt.replace("Z", "+00:00")).timestamp() * 1000)


def _f(x) -> float:
    return float(x)


def assert_ladder_parity(side_new: Side, side_leg: LegacySide, p0: float, n: int, percentage: float = 0.001):
    """Geometry is identical; float legacy vs Decimal canonical differs at ~1e-12+ after deep steps."""
    np, ntp = ladder_step(side_new, Decimal(str(p0)), n, percentage=Decimal(str(percentage)))
    lp, ltp = legacy_ladder_step(side_leg, p0, n, percentage=percentage)
    # 1e-9 is far tighter than any tick; captures IEEE vs Decimal recurrence drift only.
    assert _f(np) == pytest.approx(lp, rel=0, abs=1e-9)
    assert _f(ntp) == pytest.approx(ltp, rel=0, abs=1e-9)


@pytest.mark.parametrize("n", list(range(0, 8)))
def test_geometry_parity_buy(n):
    assert_ladder_parity(Side.BUY, LegacySide.BUY, 2500.0, n)


@pytest.mark.parametrize("n", list(range(0, 8)))
def test_geometry_parity_sell(n):
    assert_ladder_parity(Side.SELL, LegacySide.SELL, 2500.0, n)


@pytest.mark.parametrize("pct", [0.001, 0.01, 0.05])
def test_geometry_parity_custom_percentage(pct):
    assert_ladder_parity(Side.BUY, LegacySide.BUY, 100.0, 3, percentage=pct)
    assert_ladder_parity(Side.SELL, LegacySide.SELL, 100.0, 4, percentage=pct)


def _assert_replay_parity(candles, side_new: Side, side_leg: LegacySide, percentage: float = 0.001):
    legacy = legacy_replay(candles, side=side_leg, percentage=percentage)
    modern = replay_ohlc_legacy(candles, side=side_new, percentage=percentage)

    assert modern.cycle_id == legacy.cycle
    assert modern.highest_filled == legacy.highest_filled
    assert _f(modern.p0) == pytest.approx(legacy.p0, abs=1e-12)
    assert _f(modern.shared_tp) == pytest.approx(legacy.shared_tp, abs=1e-12)
    assert _f(modern.initial_p0) == pytest.approx(legacy.initial_p0, abs=1e-12)

    assert len(modern.legs) == len(legacy.legs)
    for ml, ll in zip(modern.legs, legacy.legs):
        assert ml.step == ll.step
        assert ml.ts_ms == ll.ts
        assert _f(ml.entry) == pytest.approx(ll.entry, abs=1e-12)

    assert len(modern.closed) == len(legacy.closed)
    for mc, lc in zip(modern.closed, legacy.closed):
        assert mc.cycle_id == lc.cycle
        assert mc.ts_ms == lc.ts
        assert mc.highest_step == lc.highest_step
        assert mc.open_legs == lc.open_legs
        assert _f(mc.exit) == pytest.approx(lc.exit, abs=1e-12)


def test_replay_parity_step0_tp_chain():
    candles = [[ms("2026-01-01T00:01:00Z"), "100", "100.01", "99.90", "100", "0", 0, "0"]]
    _assert_replay_parity(candles, Side.SELL, LegacySide.SELL)


def test_replay_parity_deep_cycle():
    candles = [[ms("2026-01-01T00:01:00Z"), "100", "100.90", "100.42", "100.50", "0", 0, "0"]]
    _assert_replay_parity(candles, Side.SELL, LegacySide.SELL)


def test_replay_parity_no_same_candle_reprocess():
    candles = [[ms("2026-01-01T00:01:00Z"), "100", "100.10", "99.90", "100.05", "0", 0, "0"]]
    _assert_replay_parity(candles, Side.SELL, LegacySide.SELL)


def test_replay_parity_multi_candle():
    candles = [
        [ms("2026-01-01T00:01:00Z"), "100", "100.20", "99.90", "100", "0", 0, "0"],
        [ms("2026-01-01T00:02:00Z"), "99.9", "100.2", "99.80", "100", "0", 0, "0"],
        [ms("2026-01-01T00:03:00Z"), "99.8", "100.3", "99.70", "100", "0", 0, "0"],
    ]
    _assert_replay_parity(candles, Side.SELL, LegacySide.SELL)


def test_replay_parity_buy_side():
    candles = [
        [ms("2026-01-01T00:01:00Z"), "100", "100.20", "99.50", "99.80", "0", 0, "0"],
        [ms("2026-01-01T00:02:00Z"), "99.80", "100.5", "99.40", "100.1", "0", 0, "0"],
    ]
    _assert_replay_parity(candles, Side.BUY, LegacySide.BUY)


def test_replay_parity_custom_percentage():
    candles = [[ms("2026-01-01T00:01:00Z"), "100", "100.50", "99.00", "100", "0", 0, "0"]]
    _assert_replay_parity(candles, Side.SELL, LegacySide.SELL, percentage=0.01)
