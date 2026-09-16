from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "installer"))

import pytest

from fibolearn.agent.safety import ReadOnlyTradeGuard
from fibolearn.backtest.engine import replay_multiscale
from fibolearn.collector.state_adapter import LadderStateInput, extract_ladder_observation
from fibolearn.config.defaults import DEFAULT_DIRECTIONS, DEFAULT_PERCENTAGES, DEFAULT_SYMBOLS
from fibolearn.features.market import significant_level_distances, volume_profile, vwap_series
from fibolearn.features.multiscale import build_multiscale_vector, cross_scale_relationships
from fibolearn.features.price_action import price_action_features
from fibolearn.labels.outcomes import label_outcomes
from fibolearn.research.patterns import Pattern, PatternStatus
from fibolearn.research.validation import leave_one_symbol_out
from fibolearn.storage.sqlite_store import FiboLearnStore
from fibolearn.telegram.wizard import FiboLearnWizard
from patchspecs import specs_for_capabilities


def _candles():
    # open_time, open, high, low, close, volume, close_time, quote_volume
    return [
        [0, "100", "105", "99", "104", "10", 59_999, "1020"],
        [60_000, "104", "108", "103", "107", "20", 119_999, "2120"],
        [120_000, "107", "109", "101", "102", "30", 179_999, "3150"],
        [180_000, "102", "111", "100", "110", "40", 239_999, "4240"],
        [240_000, "110", "112", "109", "111", "50", 299_999, "5525"],
    ]


def test_default_universe_and_multiscale_requirements():
    assert DEFAULT_SYMBOLS == ("BTC", "ETH", "SOL", "ZEC", "PAXG")
    assert DEFAULT_PERCENTAGES == (Decimal("1"), Decimal("0.1"), Decimal("0.01"), Decimal("0.001"))
    assert DEFAULT_DIRECTIONS == ("BUY", "SELL")


def test_ladder_extraction_buy_and_sell_pn_context():
    buy = extract_ladder_observation(
        LadderStateInput(symbol="BTC", timestamp_ms=1, direction="BUY", percentage=Decimal("0.001"), cycle_id=7, p0=Decimal("100"), active_step=2, current_price=Decimal("100.25"), active_since_ms=0)
    )
    sell = extract_ladder_observation(
        LadderStateInput(symbol="BTC", timestamp_ms=1, direction="SELL", percentage=Decimal("0.001"), cycle_id=8, p0=Decimal("100"), active_step=2, current_price=Decimal("99.75"), active_since_ms=0)
    )
    assert buy.pn < buy.pn_minus_1
    assert buy.pn_plus_2 < buy.pn_plus_1 < buy.pn
    assert sell.pn > sell.pn_minus_1
    assert sell.pn_plus_2 > sell.pn_plus_1 > sell.pn
    assert buy.distance_to_pn == Decimal("100.25") - buy.pn


def test_vwap_calculation_and_migration():
    v = vwap_series(_candles())
    assert v[-1].vwap == Decimal("16055") / Decimal("150")
    assert v[-1].slope is not None
    assert v[-1].migration is not None


def test_volume_profile_poc_vah_val():
    profile = volume_profile(_candles(), bins=8)
    assert profile.poc is not None
    assert profile.val <= profile.poc <= profile.vah
    assert profile.high == Decimal("112")
    assert profile.low == Decimal("99")


def test_significant_level_distances_for_pn1_and_pn2():
    levels = {"VWAP": Decimal("107"), "POC": Decimal("108"), "VAH": Decimal("111"), "VAL": Decimal("101"), "swing_high": Decimal("112"), "swing_low": Decimal("99")}
    distances = significant_level_distances({"pn_plus_1": Decimal("108.5"), "pn_plus_2": Decimal("111.5")}, levels)
    assert distances["pn_plus_1"]["POC"] == Decimal("0.5")
    assert distances["pn_plus_2"]["VAH"] == Decimal("0.5")


def test_price_action_feature_generation():
    features = price_action_features(_candles(), active_pn=Decimal("107"), active_since_ms=60_000)
    assert features["higher_high"] is True
    assert features["breakout_above_pn"] is True
    assert features["pn_retest_count"] >= 1
    assert "volatility_range" in features


def test_outcome_labeling_keeps_rich_results_not_binary():
    obs = extract_ladder_observation(LadderStateInput(symbol="BTC", timestamp_ms=60_000, direction="BUY", percentage=Decimal("0.001"), cycle_id=1, p0=Decimal("100"), active_step=1, current_price=Decimal("104"), active_since_ms=60_000))
    out = label_outcomes(obs, _candles()[1:], cycle_closed_ts_ms=240_000)
    assert out.reached_pn_plus_1 is True
    assert out.time_to_pn_plus_1_ms is not None
    assert out.time_to_tp_closure_ms == 180_000
    assert out.mfe is not None and out.mae is not None


def test_no_lookahead_multiscale_replay_uses_only_current_candle():
    snapshots = replay_multiscale("BTC", _candles(), percentages=(Decimal("0.1"), Decimal("0.001")), directions=("BUY", "SELL"))
    assert len(snapshots) == len(_candles())
    first = snapshots[0]
    later = snapshots[-1]
    assert first.timestamp_ms == 0
    assert first.market["price"] == Decimal("104")
    assert later.market["price"] == Decimal("111")
    assert first.raw["source_candles"] == 1
    assert later.raw["source_candles"] == 5


def test_multiscale_vector_contains_all_percentages_and_buy_sell_relationships():
    vec = build_multiscale_vector("BTC", 240_000, Decimal("111"), percentages=(Decimal("1"), Decimal("0.1"), Decimal("0.01"), Decimal("0.001")))
    assert set(vec.ladders.keys()) == {"1", "0.1", "0.01", "0.001"}
    assert all(set(sides.keys()) == {"BUY", "SELL"} for sides in vec.ladders.values())
    rel = cross_scale_relationships(vec)
    assert "buy_sell_progression_imbalance" in rel
    assert "cross_percentage_active_depth" in rel


def test_storage_schema_version_and_observation_roundtrip(tmp_path: Path):
    store = FiboLearnStore(tmp_path / "fibolearn.sqlite")
    vec = build_multiscale_vector("BTC", 1, Decimal("100"), percentages=(Decimal("0.001"),))
    obs_id = store.save_observation(vec)
    loaded = store.get_observation(obs_id)
    assert loaded["schema_version"] >= 1
    assert loaded["symbol"] == "BTC"
    assert loaded["state_vector"]["ladders"]["0.001"]["BUY"]["percentage"] == "0.001"


def test_pattern_lifecycle_and_cross_symbol_validation():
    p = Pattern.create(name="pn2-near-vah", definition={"target": "pn_plus_2", "near": "VAH"}, discovery_range={"start": 1, "end": 2}, symbols=["BTC"])
    assert p.status is PatternStatus.DISCOVERED
    p.mark_backtested(sample_size=30, result={"hit_rate": 0.6})
    assert p.status is PatternStatus.BACKTESTED
    result = leave_one_symbol_out([{"symbol": "BTC", "hit": True}, {"symbol": "ETH", "hit": False}, {"symbol": "SOL", "hit": True}])
    assert set(result.keys()) == {"BTC", "ETH", "SOL"}


def test_telegram_fibolearn_callbacks_and_study_setup():
    wizard = FiboLearnWizard()
    screen = wizard.open()
    assert "/fibolearn" in screen.text
    assert any("Live Observer" in b["text"] for row in screen.buttons for b in row)
    live = wizard.handle_callback("fibolearn:live:BTC")
    assert "BTC" in live.text and "MULTI-SCALE" in live.text
    study = wizard.handle_callback("fibolearn:study:multiscale:BTC")
    assert "Research job" in study.text
    assert "CANDIDATE" in study.text


def test_patchspecs_include_fibolearn_when_fibo_capability_installs():
    specs = specs_for_capabilities(["fibo"])
    sentinels = {s.native_sentinel for s in specs}
    assert "from plugins.trade.fibolearn_wizard import handle_fibolearn_command" in sentinels
    assert "from plugins.trade.fibolearn_wizard import handle_fibolearn_callback" in sentinels


def test_read_only_guard_blocks_trading_actions():
    guard = ReadOnlyTradeGuard()
    for op in ("place_order", "cancel_order", "modify_order", "close_position", "start_goldenfibo_trade", "change_goldenfibo_parameters"):
        with pytest.raises(PermissionError):
            guard.execute({"operation": op})
    assert guard.execute({"operation": "market_price"})["allowed"] is True
