from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import importlib

from goldenfibo.backtest import run_finite_backtest
from goldenfibo.engine.config import OhlcResolveMode, Side
from goldenfibo.feeders.historical_ohlc import apply_ohlc_to_engine, collect_ohlc_events
from goldenfibo.metrics import build_volume_profile, ladder_vwap, step_vwap, volume_profile_poc, volume_profile_value_area
from goldenfibo.session.runner import run_ohlc_on_engine

import sys
from pathlib import Path

ROOT = Path('/root/kam')
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.trade import backtest_wizard as wizard


def _candles_buy_sell():
    t0 = 1_700_000_000_000
    return [
        [t0, "100", "100.15", "99.95", "100.05", "1", t0 + 59_999, "100"],
        [t0 + 60_000, "100.05", "100.30", "100.00", "100.25", "1.5", t0 + 119_999, "150"],
        [t0 + 120_000, "100.25", "100.80", "100.10", "100.70", "2", t0 + 179_999, "200"],
        [t0 + 180_000, "100.70", "100.90", "99.70", "100.10", "2.5", t0 + 239_999, "250"],
        [t0 + 240_000, "100.10", "101.20", "100.05", "101.00", "3", t0 + 299_999, "300"],
    ]


def _snapshot(klines, side, pct):
    result = run_finite_backtest(klines, side=side, percentage=pct, symbol="BTCUSDT")
    return result.state_payload, result


def test_finite_helper_matches_canonical_engine_and_metrics():
    candles = _candles_buy_sell()
    payload, result = _snapshot(candles, Side.SELL, Decimal("0.001"))
    assert payload["mode"] == "BACKTEST"
    assert payload["symbol"] == "BTCUSDT"
    assert payload["n"] >= 0
    assert payload["closed_count"] >= 0
    assert result.bars_processed == len(candles)
    assert len(result.chart_candles) == len(candles)

    # Canonical engine parity with the feeder/runner seam.
    cfg = result.engine.config
    events = collect_ohlc_events(candles, cfg, mode=OhlcResolveMode.LEGACY)
    eng = result.engine.__class__(cfg)
    eng.run(events)
    assert eng.state.comparable() == result.engine.state.comparable()


def test_buy_sell_percentages_parity_against_canonical_helper():
    candles = _candles_buy_sell()
    for side in (Side.BUY, Side.SELL):
        for pct in (Decimal("1"), Decimal("0.1"), Decimal("0.01"), Decimal("0.001")):
            payload, result = _snapshot(candles, side, pct)
            assert payload["side"] == side.value
            assert result.engine.config.percentage == pct
            assert payload["n"] == result.engine.state.highest_filled
            assert payload["cycle_id"] == result.engine.state.cycle_id
            assert payload["p0"] is not None
            assert payload["shared_tp"] is not None


def test_wizard_production_path_uses_canonical_helper(monkeypatch):
    called = {"n": 0}

    def fake_run(*args, **kwargs):
        called["n"] += 1
        return run_finite_backtest(*args, **kwargs)

    monkeypatch.setattr(wizard, "run_finite_backtest", fake_run)
    candles = _candles_buy_sell()
    state = wizard.State(ladder="sell", market="spot", symbol="BTCUSDT", percentage=0.001)
    state.year = 2026
    state.month = 1
    state.day = 1
    monkeypatch.setattr(wizard, "_draw_jpg", lambda *a, **k: "jpg")
    screen = wizard._summarize_side(candles, Side.SELL, "BTCUSDT", "spot", 0.001)
    assert called["n"] == 1
    assert "Cycle:" in screen["text"]
    assert "Ladder VWAP:" in screen["text"]
    assert screen["svg"] == "jpg"
    assert "P0:" in screen["text"]
    assert "Current step:" in screen["text"]


def test_finite_backtest_does_not_use_web_singleton():
    import goldenfibo.session.controller as controller
    s1 = controller.reset_session_for_tests()
    before = id(s1)
    candles = _candles_buy_sell()
    payload, _ = _snapshot(candles, Side.BUY, Decimal("0.001"))
    after = id(controller.get_session())
    assert before == after
    assert payload["mode"] == "BACKTEST"
    assert payload["type"] == "state_snapshot"


def test_legacy_replay_module_not_required_by_production_path():
    # Production telegram path should depend on canonical helper, not legacy replay math.
    src = Path(wizard.__file__).read_text()
    assert "from goldenfibo.backtest import run_finite_backtest" in src
    assert "golden_fibo.historical_replay" not in src
    assert "levels_p0_to_pn" not in src
    assert "replay_ohlc(" not in src


def test_metric_helpers_match_payload_windows():
    candles = _candles_buy_sell()
    payload, result = _snapshot(candles, Side.SELL, Decimal("0.001"))
    bars = result.bars
    start_ts = int(payload["metric_windows"]["ladder_start_ts_ms"])
    step_ts = int(payload["metric_windows"]["step_start_ts_ms"])
    assert float(payload["ladder_vwap"]) == ladder_vwap(bars, start_ts)
    assert float(payload["step_vwap"]) == step_vwap(bars, step_ts)
    prof_l = build_volume_profile(bars, start_ts)
    prof_s = build_volume_profile(bars, step_ts)
    assert prof_l is not None and prof_s is not None
    assert float(payload["ladder_poc"]) == round(prof_l.poc, 2)
    assert float(payload["step_poc"]) == round(prof_s.poc, 2)
    val, vah = volume_profile_value_area(bars, start_ts)
    assert float(payload["ladder_val"]) == round(val, 2)
    assert float(payload["ladder_vah"]) == round(vah, 2)
    assert round(volume_profile_poc(bars, start_ts), 2) == round(prof_l.poc, 2)


def test_existing_feeder_and_runner_seams_still_work():
    candles = _candles_buy_sell()
    cfg = run_finite_backtest(candles, side=Side.SELL, percentage=Decimal("0.001"), symbol="BTCUSDT").engine.config
    eng = run_finite_backtest(candles, side=Side.SELL, percentage=Decimal("0.001"), symbol="BTCUSDT").engine.__class__(cfg)
    events = collect_ohlc_events(candles, cfg, mode=OhlcResolveMode.LEGACY)
    run = run_ohlc_on_engine(eng, candles, mode=OhlcResolveMode.LEGACY)
    assert run.bars_processed == len(candles)
    apply_ohlc_to_engine(eng, candles, mode=OhlcResolveMode.LEGACY)
    assert events
