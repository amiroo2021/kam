from __future__ import annotations

from fibolearn.research.phase3b_cross_riskset import (
    canonicalize_episode_cross,
    classify_cross_eligibility,
    control_eligible_at_landmark,
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
        'partition': 'TRAIN',
    }


def _control(**overrides):
    base = {
        'episode_key': 'c1',
        'symbol': 'BTC',
        'percentage': '0.001',
        'direction': 'BUY',
        'active_step': 0,
        'episode_start_timestamp_ms': 0,
        'cross_timestamp_ms': None,
        'cross_elapsed_ms': None,
        'pn_plus_1_timestamp_ms': 420000,
        'tp_or_terminal_timestamp_ms': 420000,
        'post_cross_outcome': 'regression',
        'intrabar_order_ambiguous': False,
        'start_feature': {'condition_present': False},
        'partition': 'TRAIN',
        'obs_start_timestamp_ms': 0,
        'obs_end_timestamp_ms': 500000,
    }
    base.update(overrides)
    return base


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


def test_canonical_cross_uses_nested_cross_timestamp():
    raw = {'cross_feature': {'cross_timestamp_ms': 123, 'condition_present': True}, 'episode_key': 'x'}
    out = canonicalize_episode_cross(raw)
    assert out['cross_timestamp_ms'] == 123
    assert out['cross_elapsed_ms'] == 123


def test_control_landmark_equals_start_plus_t():
    treated = _treated()
    control = _control()
    out = match_controls_for_cross([treated], [control], k=1)
    assert out['matched_sets'][0]['control_landmark_ms'] == control['episode_start_timestamp_ms'] + treated['cross_elapsed_ms']


def test_control_eligible_if_alive_and_uncrossed_at_landmark():
    control = _control(cross_timestamp_ms=999999999, pn_plus_1_timestamp_ms=500000, tp_or_terminal_timestamp_ms=500000)
    out = control_eligible_at_landmark(control, landmark_ms=120000)
    assert out['eligible'] is True
    assert out['violations'] == []


def test_control_rejects_start_after_landmark():
    control = _control(episode_start_timestamp_ms=130000, obs_start_timestamp_ms=130000)
    out = control_eligible_at_landmark(control, landmark_ms=120000)
    assert out['eligible'] is False
    assert 'start_gt_landmark' in out['violations']


def test_control_rejects_terminal_before_landmark():
    control = _control(tp_or_terminal_timestamp_ms=119999, pn_plus_1_timestamp_ms=300000)
    out = control_eligible_at_landmark(control, landmark_ms=120000)
    assert out['eligible'] is False
    assert 'terminal_le_landmark' in out['violations']


def test_control_rejects_terminal_exactly_at_landmark():
    control = _control(tp_or_terminal_timestamp_ms=120000, pn_plus_1_timestamp_ms=300000)
    out = control_eligible_at_landmark(control, landmark_ms=120000)
    assert out['eligible'] is False
    assert 'terminal_le_landmark' in out['violations']


def test_control_rejects_cross_before_landmark():
    control = _control(cross_timestamp_ms=119999)
    out = control_eligible_at_landmark(control, landmark_ms=120000)
    assert out['eligible'] is False
    assert 'cross_le_landmark' in out['violations']


def test_control_rejects_cross_exactly_at_landmark():
    control = _control(cross_timestamp_ms=120000)
    out = control_eligible_at_landmark(control, landmark_ms=120000)
    assert out['eligible'] is False
    assert 'cross_le_landmark' in out['violations']


def test_control_rejects_outcome_before_landmark():
    control = _control(pn_plus_1_timestamp_ms=119999, tp_or_terminal_timestamp_ms=300000)
    out = control_eligible_at_landmark(control, landmark_ms=120000)
    assert out['eligible'] is False
    assert 'outcome_le_landmark' in out['violations']


def test_control_rejects_outcome_exactly_at_landmark():
    control = _control(pn_plus_1_timestamp_ms=120000, tp_or_terminal_timestamp_ms=300000)
    out = control_eligible_at_landmark(control, landmark_ms=120000)
    assert out['eligible'] is False
    assert 'outcome_le_landmark' in out['violations']


def test_control_rejects_missing_terminal_unless_alive_status_proven():
    control = _control(tp_or_terminal_timestamp_ms=None)
    out = control_eligible_at_landmark(control, landmark_ms=120000)
    assert out['eligible'] is False
    assert 'terminal_unknown' in out['violations']


def test_control_rejects_seconds_milliseconds_mismatch():
    control = _control(time_coordinate_unit='seconds', landmark_coordinate_unit='milliseconds')
    out = control_eligible_at_landmark(control, landmark_ms=120000)
    assert out['eligible'] is False
    assert 'time_coordinate_mismatch' in out['violations']


def test_control_accepts_plain_millisecond_coordinates():
    control = _control()
    out = control_eligible_at_landmark(control, landmark_ms=120000)
    assert out['eligible'] is True
    assert out['violations'] == []


def test_partition_mismatch_is_rejected():
    treated = _treated()
    control = _control(partition='OOS')
    out = match_controls_for_cross([treated], [control], k=1)
    assert out['matched_sets'] == []


def test_no_cross_through_landmark_is_eligible():
    control = _control(cross_timestamp_ms=None)
    out = control_eligible_at_landmark(control, landmark_ms=120000)
    assert out['eligible'] is True


def test_control_landmark_anchors_to_control_start():
    treated = _treated()
    treated['episode_start_timestamp_ms'] = 1000
    treated['cross_elapsed_ms'] = 500
    treated['cross_timestamp_ms'] = 1500
    control = _control(episode_key='c5000', episode_start_timestamp_ms=5000, cross_timestamp_ms=None, pn_plus_1_timestamp_ms=900000, tp_or_terminal_timestamp_ms=900000)
    out = match_controls_for_cross([treated], [control], k=1)
    assert out['matched_sets'][0]['control_landmark_ms'] == 5500
    assert out['matched_sets'][0]['control_landmark_ms'] != 1500


def test_two_controls_same_treated_get_different_absolute_landmarks():
    treated = _treated()
    treated['episode_start_timestamp_ms'] = 1000
    treated['cross_elapsed_ms'] = 500
    treated['cross_timestamp_ms'] = 1500
    c1 = _control(episode_key='c5000', episode_start_timestamp_ms=5000, cross_timestamp_ms=None, pn_plus_1_timestamp_ms=900000, tp_or_terminal_timestamp_ms=900000)
    c2 = _control(episode_key='c8000', episode_start_timestamp_ms=8000, cross_timestamp_ms=None, pn_plus_1_timestamp_ms=900000, tp_or_terminal_timestamp_ms=900000)
    out = match_controls_for_cross([treated], [c1, c2], k=2)
    assert len(out['matched_sets']) == 1
    landmarks = sorted(c['control_landmark_ms'] for c in out['matched_sets'][0]['controls'])
    assert landmarks == [5500, 8500]
    assert len({c['control_landmark_ms'] for c in out['matched_sets'][0]['controls']}) == 2
    assert all(c['control_landmark_ms'] - c['episode_start_timestamp_ms'] == treated['cross_elapsed_ms'] for c in out['matched_sets'][0]['controls'])


def test_control_landmark_survives_through_matcher_and_outcome_labeling():
    treated = _treated()
    treated['episode_start_timestamp_ms'] = 1000
    treated['cross_elapsed_ms'] = 500
    treated['cross_timestamp_ms'] = 1500
    control = _control(episode_key='c5000', episode_start_timestamp_ms=5000, cross_timestamp_ms=None, pn_plus_1_timestamp_ms=900000, tp_or_terminal_timestamp_ms=900000)
    out = match_controls_for_cross([treated], [control], k=1)
    ms = out['matched_sets'][0]['control_landmark_ms']
    assert ms == 5500
    assert out['matched_sets'][0]['controls'][0]['control_landmark_ms'] == ms
    label = landmark_outcome_after_cross(treated, out['matched_sets'][0]['controls'][0])
    assert label['control_landmark_ms'] == ms
    assert label['status'] == 'ok'


def test_changing_treated_absolute_start_does_not_change_established_control_landmark():
    treated = _treated()
    treated['episode_start_timestamp_ms'] = 1000
    treated['cross_elapsed_ms'] = 500
    treated['cross_timestamp_ms'] = 1500
    control = _control(episode_key='c5000', episode_start_timestamp_ms=5000, cross_timestamp_ms=None, pn_plus_1_timestamp_ms=900000, tp_or_terminal_timestamp_ms=900000)
    out1 = match_controls_for_cross([treated], [control], k=1)
    treated['episode_start_timestamp_ms'] = 999999
    out2 = match_controls_for_cross([treated], [control], k=1)
    assert out1['matched_sets'][0]['control_landmark_ms'] == out2['matched_sets'][0]['control_landmark_ms'] == 5500


def test_landmark_outcome_uses_post_landmark_measurement_only():
    treated = _treated()
    control = _control(cross_timestamp_ms=None, pn_plus_1_timestamp_ms=300000, tp_or_terminal_timestamp_ms=420000)
    out = landmark_outcome_after_cross(treated, control)
    assert out['control_landmark_ms'] == 120000
    assert out['status'] == 'ok'


def test_temporal_split_purges_overlap():
    rows = [
        {'cycle_key': 'A', 'start_timestamp_ms': 1},
        {'cycle_key': 'A', 'start_timestamp_ms': 2},
        {'cycle_key': 'B', 'start_timestamp_ms': 100},
    ]
    train, test = temporal_split_by_cycle(rows, train_fraction=0.5)
    assert all(r['cycle_key'] == 'A' for r in train)
    assert all(r['cycle_key'] == 'B' for r in test)
