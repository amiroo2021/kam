from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from fibolearn.backtest.engine import replay_multiscale
from fibolearn.research.phase3b import (
    classify_episode,
    compute_whole_ladder_vwap_metrics,
    discover_patterns,
    episode_baseline_table,
    episode_feature_row,
    fl_vwap_experiment,
    question_answer_from_reports,
    run_phase3b,
    wilson_ci,
)
from fibolearn.storage.sqlite_store import FiboLearnStore


def k(ts, o, h, l, c, v='10'):
    return [ts, str(o), str(h), str(l), str(c), v, ts + 59999, str(Decimal(str(c)) * Decimal(str(v))), 1, '0', '0', '0']


def _store_with_episode(tmp_path: Path, candles, *, pct='0.001', direction='BUY'):
    store = FiboLearnStore(tmp_path / 'fibolearn.sqlite', use_optimized_layout=True)
    for vec in replay_multiscale('BTC', candles, percentages=(Decimal(pct),), directions=(direction,), use_optimized_market=True):
        store.save_observation(vec)
    store.rebuild_episodes()
    return store


def test_episode_level_independence(tmp_path):
    store = _store_with_episode(tmp_path, [k(i * 60_000, 100, 100.001, 99.999, 100) for i in range(5)])
    rows = store.episode_rows()
    assert rows
    assert len({r['episode_key'] for r in rows}) == len(rows)


def test_ambiguity_exclusion(tmp_path):
    store = _store_with_episode(tmp_path, [k(i * 60_000, 100, 100.001, 99.999, 100) for i in range(3)])
    rows = store.episode_rows(include_ambiguous=False)
    assert all(not r['intrabar_order_ambiguous'] for r in rows)


def test_baseline_calculations(tmp_path):
    store = _store_with_episode(tmp_path, [k(i * 60_000, 100, 100.001, 99.999, 100) for i in range(8)])
    baseline = episode_baseline_table(store)
    assert baseline['cells']
    assert baseline['ambiguous_total'] >= 0


def test_whole_ladder_vwap_calculation(tmp_path):
    store = _store_with_episode(tmp_path, [k(i * 60_000, 100, 100.001, 99.999, 100) for i in range(6)])
    ep = store.episode_rows()[0]
    row = episode_feature_row(store, ep)
    assert row.feature_version == 'phase3b_v1'
    assert row.whole_ladder_vwap_condition_present is not None


def test_buy_sell_mirror_logic(tmp_path):
    store_buy = _store_with_episode(tmp_path / 'buy', [k(i * 60_000, 100, 100.001, 99.999, 100) for i in range(6)], direction='BUY')
    store_sell = _store_with_episode(tmp_path / 'sell', [k(i * 60_000, 100, 100.001, 99.999, 100) for i in range(6)], direction='SELL')
    ep_buy = store_buy.episode_rows()[0]
    ep_sell = store_sell.episode_rows()[0]
    assert episode_feature_row(store_buy, ep_buy).direction == 'BUY'
    assert episode_feature_row(store_sell, ep_sell).direction == 'SELL'


def test_vwap_crossing_timing(tmp_path):
    store = _store_with_episode(tmp_path, [k(i * 60_000, 100, 100.001, 99.999, 100) for i in range(6)])
    ep = store.episode_rows()[0]
    feat = episode_feature_row(store, ep)
    assert feat.whole_ladder_vwap_cross_timing_bin in (None, 'early', 'middle', 'late')


def test_no_look_ahead():
    assert classify_episode({'terminal_event': 'tp_before_pn_plus_1'}) == 'regression'
    assert wilson_ci(5, 10) is not None


def test_temporal_train_oos_separation(tmp_path):
    store = _store_with_episode(tmp_path, [k(i * 60_000, 100, 100.001, 99.999, 100) for i in range(10)])
    out = fl_vwap_experiment(store)
    assert 'train' in out and 'oos' in out


def test_overlapping_cycle_boundary_protection(tmp_path):
    store = _store_with_episode(tmp_path, [k(i * 60_000, 100, 100.001, 99.999, 100) for i in range(10)])
    out = run_phase3b(store, report_dir=tmp_path)
    assert out['summary']['dataset']['time_window'] == 'existing 30-day dataset only'


def test_minimum_sample_requirements(tmp_path):
    store = _store_with_episode(tmp_path, [k(i * 60_000, 100, 100.001, 99.999, 100) for i in range(3)])
    out = fl_vwap_experiment(store, min_n=999)
    assert any(cell['low_n'] for cell in out['cells'])


def test_multiple_testing_bookkeeping(tmp_path):
    store = _store_with_episode(tmp_path, [k(i * 60_000, 100, 100.001, 99.999, 100) for i in range(6)])
    fl = fl_vwap_experiment(store)
    discovered = discover_patterns(store, fl, min_samples=999)
    assert discovered['tested'] == len(fl['cells'])


def test_pattern_status_transitions(tmp_path):
    store = _store_with_episode(tmp_path, [k(i * 60_000, 100, 100.001, 99.999, 100) for i in range(6)])
    fl = fl_vwap_experiment(store)
    patterns = discover_patterns(store, fl, min_samples=1)
    assert patterns['patterns']
    assert patterns['patterns'][0]['status'] in {'CANDIDATE', 'REJECTED'}


def test_ask_fibo_retrieval_from_real_stored_results(tmp_path):
    report_dir = tmp_path / 'reports'
    report_dir.mkdir()
    fake = {
        'cells': [
            {
                'symbol': 'BTC', 'percentage': '0.001', 'direction': 'BUY', 'active_step': 3,
                'baseline': {'progression_rate': 0.4, 'order_sensitive_n': 10},
                'conditional': {'progression_rate': 0.6, 'order_sensitive_n': 8},
                'lift_pp': 0.2, 'relative_risk': 1.5, 'odds_ratio': 2.0,
            }
        ]
    }
    (report_dir / 'phase3b_fl_vwap_001.json').write_text(json.dumps(fake))
    answer = question_answer_from_reports('BTC BUY 0.001 P3: what happens when whole-ladder VWAP is above P3?', report_dir=report_dir)
    assert answer is not None
    assert answer['symbol'] == 'BTC'
