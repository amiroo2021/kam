from __future__ import annotations

from fibolearn.research.phase3b_cross_riskset import (
    canonicalize_episode_cross,
    classify_cross_eligibility,
    match_controls_for_cross,
    landmark_outcome_after_cross,
    temporal_split_by_cycle,
)


def _treated():
    return {
        'episode_key': 't1',
        'symbol': 'BTC',
        'percentage': '0.001',
        'direction': 'BUY',
        'active_step': 0,
        'episode_start_timestamp_ms': 0,
        'cross_timestamp_ms': 120000,
        'cross_elapsed_ms': 120000,
        'pn_plus_1_timestamp_ms': 240000,
        'tp_or_terminal_timestamp_ms': 300000,
        'post_cross_outcome': 'progression',
        'intrabar_order_ambiguous': False,
        'start_feature': {'condition_present': False},
        'cross_feature': {'cross_timestamp_ms': 120000},
        'start_timestamp_ms': 0,
    }


def _control(ok=True, cross_before=False, outcome_after=True):
    return {
        'episode_key': 'c1',
        'symbol': 'BTC',
        'percentage': '0.001',
        'direction': 'BUY',
        'active_step': 0,
        'episode_start_timestamp_ms': 0,
        'cross_timestamp_ms': 180000 if cross_before else 999999999,
        'cross_elapsed_ms': 180000,
        'pn_plus_1_timestamp_ms': 420000 if outcome_after else 90000,
        'tp_or_terminal_timestamp_ms': 420000 if outcome_after else 90000,
        'post_cross_outcome': 'regression' if outcome_after else 'excluded_pre_cross_progression',
        'intrabar_order_ambiguous': False,
        'start_feature': {'condition_present': False},
    }


def test_outcome_before_cross_is_excluded():
    ep = _treated()
    ep['tp_or_terminal_timestamp_ms'] = 60000
    ep['post_cross_outcome'] = 'excluded_pre_cross_progression'
    assert classify_cross_eligibility(ep)['status'] == 'outcome_before_cross'


def test_same_candle_ambiguous_is_excluded():
    ep = _treated()
    ep['cross_timestamp_ms'] = 120000
    ep['tp_or_terminal_timestamp_ms'] = 120000
    assert classify_cross_eligibility(ep)['status'] == 'same_candle_ambiguous'


def test_control_landmark_and_post_time_only():
    treated = _treated()
    control = _control()
    out = match_controls_for_cross([treated], [control], k=1)
    assert out['matched_sets'][0]['control_landmark_ms'] == control['episode_start_timestamp_ms'] + treated['cross_elapsed_ms']
    assert out['matched_sets'][0]['control_post_outcome'] in {'progression', 'regression'}


def test_temporal_split_purges_overlap():
    rows = [
        {'cycle_key': 'A', 'start_timestamp_ms': 1},
        {'cycle_key': 'A', 'start_timestamp_ms': 2},
        {'cycle_key': 'B', 'start_timestamp_ms': 100},
    ]
    train, test = temporal_split_by_cycle(rows, train_fraction=0.5)
    assert all(r['cycle_key'] == 'A' for r in train)
    assert all(r['cycle_key'] == 'B' for r in test)


def test_canonical_cross_uses_nested_cross_timestamp():
    raw = {'cross_feature': {'cross_timestamp_ms': 123, 'condition_present': True}, 'episode_key': 'x'}
    out = canonicalize_episode_cross(raw)
    assert out['cross_timestamp_ms'] == 123
    assert out['cross_elapsed_ms'] == 123
