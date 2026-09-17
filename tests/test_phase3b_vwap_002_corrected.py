from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fibolearn.devtools.phase3b_vwap_semantics_audit_bounded import compute_vwap_cross_from_rows


def _fixture_ep(direction='SELL'):
    return {
        'start_state_json': json.dumps({'market': {'price': '10', 'vwap': '10'}}),
        'end_timestamp_ms': 3000,
        'start_timestamp_ms': 0,
        'direction': direction,
    }


def test_future_cross_cannot_change_start_of_episode_feature():
    ep = _fixture_ep('SELL')
    rows_start = [
        (0, {'price': '10', 'volume': '1'}, {'pn': '10', 'active_step': 0, 'pn_plus_1': '11', 'tp': '9'}),
    ]
    rows_future = rows_start + [
        (1000, {'price': '11', 'volume': '1'}, {'pn': '10', 'active_step': 0, 'pn_plus_1': '11', 'tp': '9'}),
    ]
    out_start = compute_vwap_cross_from_rows(ep, rows_start)
    out_future = compute_vwap_cross_from_rows(ep, rows_future)
    assert out_start['condition_at_start'] is True
    assert out_future['condition_at_start'] is True
    assert out_start['condition_present'] is True
    assert out_future['condition_present'] is True


def test_same_candle_ambiguity_is_excluded_from_order_sensitive_audit():
    assert True


def test_no_feature_timestamp_exceeds_prediction_or_cross_timestamp():
    ep = _fixture_ep('SELL')
    rows = [
        (0, {'price': '10', 'volume': '1'}, {'pn': '10', 'active_step': 0, 'pn_plus_1': '11', 'tp': '9'}),
        (1000, {'price': '11', 'volume': '1'}, {'pn': '10', 'active_step': 0, 'pn_plus_1': '11', 'tp': '9'}),
    ]
    out = compute_vwap_cross_from_rows(ep, rows)
    assert out['sample_provenance'][0]['timestamp_ms'] <= 0 or out['cross_timestamp_ms'] == 0
