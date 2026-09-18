from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fibolearn.research.phase3b_cross_riskset import connect, iter_episodes, resolve_post_landmark_outcome_from_primitives


_CANONICAL_OUTCOME_LABELS = {
    'PN_PLUS_1_FIRST': 'PN_PLUS_1_FIRST',
    'pn_plus_1_before_tp': 'PN_PLUS_1_FIRST',
    'TP_FIRST': 'TP_FIRST',
    'tp_before_pn_plus_1': 'TP_FIRST',
    'OTHER_TERMINAL': 'OTHER_TERMINAL',
    'other_terminal': 'OTHER_TERMINAL',
    'CENSORED': 'CENSORED',
    'censored': 'CENSORED',
    'SAME_CANDLE_AMBIGUOUS': 'SAME_CANDLE_AMBIGUOUS',
    'same_candle_ambiguous': 'SAME_CANDLE_AMBIGUOUS',
    'OTHER_UNKNOWN': 'OTHER_UNKNOWN',
    'other_unknown': 'OTHER_UNKNOWN',
}


def canonical_outcome_label(label: str) -> str:
    if label not in _CANONICAL_OUTCOME_LABELS:
        raise KeyError(label)
    return _CANONICAL_OUTCOME_LABELS[label]


def maybe_canonical_outcome_label(label: str):
    return _CANONICAL_OUTCOME_LABELS.get(label)


def canonical_exact_match(expected_raw: str, resolver_raw: str) -> bool:
    expected = canonical_outcome_label(expected_raw)
    resolver = canonical_outcome_label(resolver_raw)
    return expected == resolver


def _load_episode(episode_key: str):
    con = connect()
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            'select episode_key, symbol, percentage, direction, active_step, start_timestamp_ms, end_timestamp_ms, terminal_event, evolution_json, outcome_json from episodes where episode_key=?',
            (episode_key,),
        ).fetchone()
        assert row is not None
        return dict(row)
    finally:
        con.close()


def _sample_episode(category: str):
    con = connect()
    con.row_factory = sqlite3.Row
    try:
        for row in iter_episodes(con):
            if category == 'PN_PLUS_1_FIRST' and row['terminal_event'] == 'pn_plus_1_before_tp':
                return row['episode_key']
            if category == 'TP_FIRST' and row['terminal_event'] == 'tp_before_pn_plus_1':
                return row['episode_key']
            if category == 'CENSORED' and row['terminal_event'] == 'censored':
                return row['episode_key']
            if category == 'OTHER_UNKNOWN' and row['terminal_event'] == 'other_terminal':
                return row['episode_key']
        return None
    finally:
        con.close()


def _trace_real_boundary(episode_key: str, landmark_ms: int):
    con = sqlite3.connect('/root/.hermes/fibolearn/fibolearn.sqlite')
    con.row_factory = sqlite3.Row
    try:
        ep = dict(con.execute('select episode_key,symbol,percentage,direction,cycle_id,active_step,start_timestamp_ms,end_timestamp_ms,terminal_event from episodes where episode_key=?',(episode_key,)).fetchone())
        q = '''
            select m.id as market_id, m.timestamp_ms, l.id as ladder_id, l.cycle_id, l.active_step, m.market_json, l.ladder_state_json
            from market_observations m join ladder_observations l on l.market_id=m.id
            where m.symbol=? and l.percentage=? and l.direction=? and m.timestamp_ms >= ?
            order by m.timestamp_ms asc, l.id asc
        '''
        prev_cycle = str(ep['cycle_id'])
        prev_step = int(ep['active_step'])
        selected = None
        for r in con.execute(q,(ep['symbol'],ep['percentage'],ep['direction'],landmark_ms)):
            market = json.loads(r['market_json'])
            evs = (market.get('domain_events') or {}).get(f"{ep['percentage']}:{ep['direction']}", []) or []
            cur_cycle = str(r['cycle_id'])
            cur_step = int(r['active_step'])
            if cur_cycle != prev_cycle or cur_step != prev_step:
                selected = {
                    'market_id': int(r['market_id']), 'ladder_id': int(r['ladder_id']), 'timestamp_ms': int(r['timestamp_ms']),
                    'cycle_id': cur_cycle, 'active_step': cur_step, 'domain_events': evs,
                    'previous_cycle_id': prev_cycle, 'previous_active_step': prev_step,
                    'transition_source': 'REAL',
                }
                break
            prev_cycle, prev_step = cur_cycle, cur_step
        return ep, selected
    finally:
        con.close()


def test_resolver_strictly_ignores_event_at_landmark_and_uses_first_post_t_tp():
    ep, selected = _trace_real_boundary('BTC|0.001|SELL|1|2', 1786990980000)
    assert selected is not None
    # The landmark/opening row is at T and must not be treated as post-landmark outcome.
    assert selected['timestamp_ms'] > 1786990980000
    assert resolve_post_landmark_outcome_from_primitives(ep, 1786990980000) == 'TP_FIRST'


def test_resolver_picks_pn_first_from_real_primitives_when_present():
    key = _sample_episode('PN_PLUS_1_FIRST')
    assert key is not None
    ep = _load_episode(key)
    ev = resolve_post_landmark_outcome_from_primitives(ep, int(ep['start_timestamp_ms']))
    assert ev == 'PN_PLUS_1_FIRST'


def test_resolver_picks_tp_first_from_real_primitives_when_present():
    key = _sample_episode('TP_FIRST')
    assert key is not None
    ep = _load_episode(key)
    ev = resolve_post_landmark_outcome_from_primitives(ep, int(ep['start_timestamp_ms']))
    assert ev == 'TP_FIRST'


def test_resolver_is_terminal_event_independent():
    key = _sample_episode('PN_PLUS_1_FIRST') or _sample_episode('TP_FIRST')
    assert key is not None
    ep = _load_episode(key)
    base = resolve_post_landmark_outcome_from_primitives(ep, int(ep['start_timestamp_ms']))
    ep2 = dict(ep)
    ep2['terminal_event'] = 'mutated'
    ep2['outcome_json'] = ep['outcome_json']
    ep2['evolution_json'] = ep['evolution_json']
    assert resolve_post_landmark_outcome_from_primitives(ep2, int(ep['start_timestamp_ms'])) == base


def test_resolver_missing_primitives_returns_other_unknown():
    ep = {
        'episode_key': 'fake',
        'symbol': 'ETH',
        'percentage': '0.001',
        'direction': 'BUY',
        'active_step': 1,
        'start_timestamp_ms': 1000,
        'end_timestamp_ms': 2000,
        'terminal_event': 'pn_plus_1_before_tp',
        'evolution_json': json.dumps({'event_ordering': []}),
        'outcome_json': json.dumps({}),
    }
    assert resolve_post_landmark_outcome_from_primitives(ep, 1500) == 'OTHER_UNKNOWN'


def test_canonical_outcome_label_map_equivalences():
    assert canonical_outcome_label('pn_plus_1_before_tp') == 'PN_PLUS_1_FIRST'
    assert canonical_outcome_label('PN_PLUS_1_FIRST') == 'PN_PLUS_1_FIRST'
    assert canonical_outcome_label('tp_before_pn_plus_1') == 'TP_FIRST'
    assert canonical_outcome_label('TP_FIRST') == 'TP_FIRST'
    assert canonical_outcome_label('other_terminal') == 'OTHER_TERMINAL'
    assert canonical_outcome_label('OTHER_TERMINAL') == 'OTHER_TERMINAL'
    assert canonical_outcome_label('censored') == 'CENSORED'
    assert canonical_outcome_label('CENSORED') == 'CENSORED'
    assert canonical_outcome_label('same_candle_ambiguous') == 'SAME_CANDLE_AMBIGUOUS'
    assert canonical_outcome_label('SAME_CANDLE_AMBIGUOUS') == 'SAME_CANDLE_AMBIGUOUS'
    assert canonical_outcome_label('other_unknown') == 'OTHER_UNKNOWN'
    assert canonical_outcome_label('OTHER_UNKNOWN') == 'OTHER_UNKNOWN'


def test_canonical_outcome_label_surfaces_unknown_labels():
    assert maybe_canonical_outcome_label('definitely_not_a_label') is None
    try:
        canonical_outcome_label('definitely_not_a_label')
    except KeyError:
        pass
    else:
        raise AssertionError('unknown label must not be coerced silently')


def test_canonical_exact_match_uses_canonical_labels():
    assert canonical_exact_match('other_terminal', 'OTHER_TERMINAL')
    assert canonical_exact_match('pn_plus_1_before_tp', 'PN_PLUS_1_FIRST')
    assert canonical_exact_match('tp_before_pn_plus_1', 'TP_FIRST')
    assert canonical_exact_match('censored', 'CENSORED')
