from __future__ import annotations

import json
from pathlib import Path

from fibolearn.research.phase3b_corrected import compute_start_feature, compute_cross_feature, classify_event_ordering


def test_future_cross_cannot_change_start_feature():
    ep = {'start_timestamp_ms': 0, 'end_timestamp_ms': 2000, 'direction': 'SELL'}
    rows1 = [(0, {'price': '10', 'volume': '1'}, {'pn': '10'})]
    rows2 = rows1 + [(1000, {'price': '11', 'volume': '1'}, {'pn': '10'})]
    a = compute_start_feature(ep, rows1)
    b = compute_start_feature(ep, rows2)
    assert a['condition_present'] == b['condition_present']


def test_post_cross_feature_uses_cross_timestamp_only():
    ep = {'start_timestamp_ms': 0, 'end_timestamp_ms': 2000, 'direction': 'SELL'}
    rows = [
        (0, {'price': '10', 'volume': '1'}, {'pn': '10', 'active_step': 0}),
        (1000, {'price': '11', 'volume': '1'}, {'pn': '10', 'active_step': 0}),
    ]
    out = compute_cross_feature(ep, rows)
    assert out['cross_timestamp_ms'] == 0
    assert out['inputs_timestamp_max'] == 0


def test_same_candle_ambiguity_rule():
    assert classify_event_ordering({'event_ordering': [{'timestamp_ms': 1}, {'timestamp_ms': 1}]}) == 'same_candle_ambiguous'
