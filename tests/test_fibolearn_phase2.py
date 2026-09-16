from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fibolearn.backtest.engine import replay_multiscale
from fibolearn.collector.live_collector import FiboLearnCollector, CollectorSettings
from fibolearn.config.defaults import DEFAULT_DIRECTIONS, DEFAULT_PERCENTAGES
from fibolearn.features.context import significant_context_for_ladder
from fibolearn.labels.outcomes import complete_observation_outcome
from fibolearn.research.discovery import discover_candidates
from fibolearn.research.study import study_setup
from fibolearn.research.validation import temporal_oos_split, walk_forward_validate
from fibolearn.storage.sqlite_store import FiboLearnStore
from fibolearn.telegram.wizard import FiboLearnWizard


def candles():
    out=[]
    prices=[100,101,102,101,103,104,105,103,102,106,107,108,106,109,110]
    for i,p in enumerate(prices):
        out.append([i*60_000, str(p), str(p+1), str(p-1), str(p), str(10+i), i*60_000+59_999, str((10+i)*p)])
    return out


def test_replay_reconstructs_all_8_states_with_goldenfibo_semantics():
    snaps = replay_multiscale("BTC", candles(), percentages=DEFAULT_PERCENTAGES, directions=DEFAULT_DIRECTIONS)
    last = snaps[-1]
    assert set(last.ladders) == {"1", "0.1", "0.01", "0.001"}
    assert all(set(sides) == {"BUY", "SELL"} for sides in last.ladders.values())
    for sides in last.ladders.values():
        for obs in sides.values():
            assert obs.pn is not None
            assert obs.pn_minus_1 is None or isinstance(obs.pn_minus_1, Decimal)
            assert obs.pn_plus_1 is not None and obs.pn_plus_2 is not None
            assert obs.fraction_to_next_step is not None
            assert obs.normalized_distance_to_next_step is not None
            assert obs.distance_to_tp is None or isinstance(obs.distance_to_tp, Decimal)


def test_historical_replay_is_no_lookahead_and_stores_market_structure():
    snaps = replay_multiscale("BTC", candles(), percentages=(Decimal("0.001"),), directions=("BUY", "SELL"))
    assert snaps[0].raw["source_candles"] == 1
    assert snaps[-1].raw["source_candles"] == len(candles())
    assert snaps[0].market["price"] == Decimal("100")
    assert "vwap" in snaps[-1].market
    assert "poc" in snaps[-1].market
    assert "vah" in snaps[-1].market
    assert "val" in snaps[-1].market
    assert "price_action" in snaps[-1].features


def test_pn1_pn2_significant_context_has_raw_distances():
    snap = replay_multiscale("BTC", candles(), percentages=(Decimal("0.001"),), directions=("BUY",))[5]
    obs = snap.ladders["0.001"]["BUY"]
    ctx = significant_context_for_ladder(obs, snap.market, snap.features)
    assert "pn_plus_1" in ctx and "pn_plus_2" in ctx
    for target in ("pn_plus_1", "pn_plus_2"):
        assert "VWAP" in ctx[target]
        assert "POC" in ctx[target]
        assert "VAH" in ctx[target]
        assert "VAL" in ctx[target]


def test_outcome_completion_preserves_event_ordering():
    snap = replay_multiscale("BTC", candles(), percentages=(Decimal("0.001"),), directions=("BUY",))[1]
    obs = snap.ladders["0.001"]["BUY"]
    outcome = complete_observation_outcome(obs, candles()[2:])
    assert isinstance(outcome.event_sequence, list)
    assert outcome.event_sequence
    assert outcome.mfe is not None
    assert outcome.mae is not None


def test_collector_collects_real_snapshots_to_store(tmp_path: Path):
    store = FiboLearnStore(tmp_path / "fibolearn.sqlite")
    collector = FiboLearnCollector(store, settings=CollectorSettings(interval_seconds=60, symbols=("BTC",), percentages=(Decimal("0.001"),)))
    count = collector.collect_historical("BTC", candles())
    assert count == len(candles())
    assert store.observation_counts_by_symbol()["BTC"] == len(candles())
    assert store.observation_counts_by_percentage()["0.001"] == len(candles()) * 2


def test_discovery_baseline_oos_walkforward_and_study_setup(tmp_path: Path):
    store = FiboLearnStore(tmp_path / "fibolearn.sqlite")
    collector = FiboLearnCollector(store, settings=CollectorSettings(symbols=("BTC",), percentages=(Decimal("0.001"),)))
    collector.collect_historical("BTC", candles())
    store.complete_outcomes_for_symbol("BTC")
    rows = store.dataset_rows()
    train, test = temporal_oos_split(rows, test_fraction=Decimal("0.3"))
    assert train and test
    wf = walk_forward_validate(rows, folds=3)
    assert wf["folds"] >= 1
    candidates = discover_candidates(rows, min_samples=3)
    assert candidates
    assert candidates[0].baseline_rate is not None
    report = study_setup(store, store.latest_observation("BTC"), min_matches=1)
    assert report.match_count >= 1
    assert report.pattern.status.value in {"CANDIDATE", "BACKTESTED", "REJECTED"}


def test_telegram_renders_actual_data_and_start_stop(tmp_path: Path):
    store = FiboLearnStore(tmp_path / "fibolearn.sqlite")
    FiboLearnCollector(store, settings=CollectorSettings(symbols=("BTC",), percentages=(Decimal("0.001"),))).collect_historical("BTC", candles())
    wizard = FiboLearnWizard(store=store)
    started = wizard.handle_callback("fibolearn:toggle:start")
    assert "running" in started.text.lower()
    live = wizard.handle_callback("fibolearn:live:BTC")
    assert "BTC — MULTI-SCALE" in live.text
    assert "0.001%" in live.text
    stopped = wizard.handle_callback("fibolearn:toggle:stop")
    assert "stopped" in stopped.text.lower()
    assert store.get_running_state() is False
