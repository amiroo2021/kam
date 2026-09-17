from __future__ import annotations

from fibolearn.research.phase3b_cross_riskset import (
    reconstruct_state_at_landmark,
    eligibility_from_state_at_T,
    classify_post_landmark_outcome,
    canonical_rows,
    connect,
)


def canonical_treated_row_fixture():
    con = connect()
    rows = canonical_rows(con)
    con.close()
    for row in rows:
        if row.get('valid_cross') and row.get('cross_timestamp_ms') is not None:
            return row
    raise AssertionError('No canonical treated row found')


def test_canonical_treated_row_remains_valid_in_treated_builder():
    row = canonical_treated_row_fixture()
    assert row['valid_cross'] is True
    assert row['cross_timestamp_ms'] is not None
    assert row['cross_timestamp_ms'] >= row['episode_start_timestamp_ms']
    valid_treated = [r for r in [row] if r.get('valid_cross')]
    assert valid_treated == [row]
    # Old raw-row selector would have failed because canonical rows are not raw rows.
    from fibolearn.research.phase3b_cross_riskset import classify_cross_eligibility
    assert classify_cross_eligibility(row)['status'] == 'missing_timestamp'


def episode_template(**overrides):
    base = {
        'episode_key': 'ep',
        'episode_start_timestamp_ms': 1000,
        'tp_or_terminal_timestamp_ms': 5000,
        'time_to_vwap_cross_ms': None,
        'evolution_json': {'event_ordering': []},
        'outcome_json': {'event_ordering': []},
    }
    base.update(overrides)
    return base


def test_vwap_cross_before_t_true():
    ep = episode_template(time_to_vwap_cross_ms=1000)
    s = reconstruct_state_at_landmark(ep, 2500)
    assert s['favorable_vwap_crossed_by_landmark'] is True
    assert s['first_vwap_cross_timestamp_ms'] == 2000


def test_vwap_cross_exactly_at_t_true():
    ep = episode_template(time_to_vwap_cross_ms=1000)
    s = reconstruct_state_at_landmark(ep, 2000)
    assert s['favorable_vwap_crossed_by_landmark'] is True


def test_vwap_cross_after_t_false():
    ep = episode_template(time_to_vwap_cross_ms=4000)
    s = reconstruct_state_at_landmark(ep, 2000)
    assert s['favorable_vwap_crossed_by_landmark'] is False


def test_no_vwap_cross_complete_coverage_false():
    ep = episode_template(time_to_vwap_cross_ms=None, evolution_json={'event_ordering': []}, outcome_json={'event_ordering': []})
    s = reconstruct_state_at_landmark(ep, 2000)
    assert s['favorable_vwap_crossed_by_landmark'] is False


def test_progression_before_t_true():
    ep = episode_template(evolution_json={'event_ordering': [{'event': 'reached_pn_plus_1', 'timestamp_ms': 1500}]}, outcome_json={'event_ordering': [{'event': 'reached_pn_plus_1', 'timestamp_ms': 1500}]})
    s = reconstruct_state_at_landmark(ep, 2000)
    assert s['pn_plus_1_reached_by_landmark'] is True


def test_progression_after_t_complete_coverage_false():
    ep = episode_template(evolution_json={'event_ordering': [{'event': 'reached_pn_plus_1', 'timestamp_ms': 4500}]}, outcome_json={'event_ordering': [{'event': 'reached_pn_plus_1', 'timestamp_ms': 4500}]})
    s = reconstruct_state_at_landmark(ep, 2000)
    assert s['pn_plus_1_reached_by_landmark'] is False


def test_tp_before_t_true():
    ep = episode_template(evolution_json={'event_ordering': [{'event': 'tp_cycle_closed', 'timestamp_ms': 1500}]}, outcome_json={'event_ordering': [{'event': 'tp_cycle_closed', 'timestamp_ms': 1500}]})
    s = reconstruct_state_at_landmark(ep, 2000)
    assert s['tp_reached_by_landmark'] is True


def test_tp_after_t_complete_coverage_false():
    ep = episode_template(evolution_json={'event_ordering': [{'event': 'tp_cycle_closed', 'timestamp_ms': 4500}]}, outcome_json={'event_ordering': [{'event': 'tp_cycle_closed', 'timestamp_ms': 4500}]})
    s = reconstruct_state_at_landmark(ep, 2000)
    assert s['tp_reached_by_landmark'] is False


def test_future_tp_does_not_make_alive_false():
    ep = episode_template(evolution_json={'event_ordering': [{'event': 'tp_cycle_closed', 'timestamp_ms': 4500}]}, outcome_json={'event_ordering': [{'event': 'tp_cycle_closed', 'timestamp_ms': 4500}]})
    s = reconstruct_state_at_landmark(ep, 2000)
    assert s['alive_at_landmark'] is True


def test_same_candle_ambiguity_flagged():
    ep = episode_template(evolution_json={'event_ordering': [{'event': 'reached_pn_plus_1', 'timestamp_ms': 2000}, {'event': 'tp_cycle_closed', 'timestamp_ms': 2000}]}, outcome_json={'event_ordering': [{'event': 'reached_pn_plus_1', 'timestamp_ms': 2000}, {'event': 'tp_cycle_closed', 'timestamp_ms': 2000}]})
    s = reconstruct_state_at_landmark(ep, 2000)
    assert s['same_candle_ambiguity_at_landmark'] is True


def test_future_independence_identical_history_through_t():
    pre = [{'event': 'vwap_crossed_pn', 'timestamp_ms': 1500}]
    a = episode_template(
        evolution_json={'event_ordering': pre},
        outcome_json={'event_ordering': pre + [{'event': 'reached_pn_plus_1', 'timestamp_ms': 4500}]},
    )
    b = episode_template(
        evolution_json={'event_ordering': pre},
        outcome_json={'event_ordering': pre + [{'event': 'tp_cycle_closed', 'timestamp_ms': 4500}]},
    )
    sa = reconstruct_state_at_landmark(a, 2000)
    sb = reconstruct_state_at_landmark(b, 2000)
    assert sa['episode_started'] == sb['episode_started']
    assert sa['coverage_through_landmark'] == sb['coverage_through_landmark']
    assert sa['alive_at_landmark'] == sb['alive_at_landmark']
    assert sa['pn_plus_1_reached_by_landmark'] == sb['pn_plus_1_reached_by_landmark']
    assert sa['tp_reached_by_landmark'] == sb['tp_reached_by_landmark']
    assert sa['favorable_vwap_crossed_by_landmark'] == sb['favorable_vwap_crossed_by_landmark']
    assert sa['first_vwap_cross_timestamp_ms'] == sb['first_vwap_cross_timestamp_ms']
    assert eligibility_from_state_at_T(sa) == eligibility_from_state_at_T(sb)
    assert classify_post_landmark_outcome(sa, primitive_events_after_landmark=[{'event': 'reached_pn_plus_1', 'timestamp_ms': 4500}])['post_landmark_outcome'] == 'PN_PLUS_1_BEFORE_TP'
    assert classify_post_landmark_outcome(sb, primitive_events_after_landmark=[{'event': 'tp_cycle_closed', 'timestamp_ms': 4500}])['post_landmark_outcome'] == 'TP_BEFORE_PN_PLUS_1'


def test_null_convenience_fields_but_reconstructable_primitive_history():
    ep = episode_template(time_to_vwap_cross_ms=None, evolution_json={'event_ordering': [{'event': 'vwap_crossed_pn', 'timestamp_ms': 1500}]}, outcome_json={'event_ordering': [{'event': 'vwap_crossed_pn', 'timestamp_ms': 1500}]})
    s = reconstruct_state_at_landmark(ep, 2000)
    assert s['favorable_vwap_crossed_by_landmark'] is True


def test_unknown_when_incomplete_coverage_and_no_cross():
    ep = episode_template(tp_or_terminal_timestamp_ms=None, evolution_json={'event_ordering': []}, outcome_json={'event_ordering': []})
    s = reconstruct_state_at_landmark(ep, 2000)
    assert s['coverage_through_landmark'] is None or s['coverage_through_landmark'] is False
    assert eligibility_from_state_at_T(s)['eligible'] is False
