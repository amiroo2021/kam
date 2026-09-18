from __future__ import annotations

import sqlite3
from pathlib import Path

from fibolearn.devtools.fl_vwap_005_fingerprint import canonical_validation_fingerprint
from fibolearn.devtools.fl_vwap_005_validation_check import EXPECTED_FINGERPRINT, validate_inputs
from fibolearn.research.phase3b_cross_riskset import classify_post_landmark_outcome, determine_treated_eligibility_at_T


class _FakeRow(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def _row(**kwargs):
    return _FakeRow(kwargs)


def test_treated_eligibility_does_not_depend_on_terminal_event_for_future_only_fields():
    base = _row(
        episode_key='ep1',
        symbol='ETH',
        percentage='0.001',
        direction='BUY',
        active_step=1,
        start_timestamp_ms=1000,
        episode_start_timestamp_ms=1000,
        cross_feature={'cross_timestamp_ms': 2000},
        intrabar_order_ambiguous=False,
    )
    a = determine_treated_eligibility_at_T(base)
    b = determine_treated_eligibility_at_T(_row(**{**base, 'terminal_event': 'pn_plus_1_before_tp'}))
    c = determine_treated_eligibility_at_T(_row(**{**base, 'terminal_event': 'tp_before_pn_plus_1'}))
    assert a['eligible'] is True
    assert b['eligible'] is True
    assert c['eligible'] is True
    assert a == b == c


def test_treated_eligibility_blocks_same_candle_ambiguous():
    r = _row(
        episode_key='ep2',
        symbol='ETH',
        percentage='0.001',
        direction='BUY',
        active_step=1,
        start_timestamp_ms=1000,
        episode_start_timestamp_ms=1000,
        cross_feature={'cross_timestamp_ms': 2000},
        intrabar_order_ambiguous=True,
    )
    out = determine_treated_eligibility_at_T(r)
    assert out['eligible'] is False
    assert 'same_candle_ambiguous' in out['violations']


def test_negative_treated_tp_first_and_censored_and_ambiguous_outcomes():
    base = {'episode_started': True, 'coverage_through_landmark': True, 'alive_at_landmark': True, 'pn_plus_1_reached_by_landmark': False, 'tp_reached_by_landmark': False, 'favorable_vwap_crossed_by_landmark': False}
    tp_first = classify_post_landmark_outcome(base, primitive_events_after_landmark=[{'timestamp_ms': 2001, 'event': 'tp_hit'}, {'timestamp_ms': 3000, 'event': 'reached_pn_plus_1'}])
    censored = classify_post_landmark_outcome(base, primitive_events_after_landmark=[])
    ambiguous = classify_post_landmark_outcome(base, primitive_events_after_landmark=[{'timestamp_ms': 2001, 'event': 'tp_hit'}, {'timestamp_ms': 2001, 'event': 'reached_pn_plus_1'}])
    assert tp_first['post_landmark_outcome'] == 'TP_BEFORE_PN_PLUS_1'
    assert censored['post_landmark_outcome'] == 'CENSORED'
    assert ambiguous['post_landmark_outcome'] == 'SAME_CANDLE_AMBIGUOUS'


def test_future_independence_same_pre_t_but_different_future_labels_do_not_change_treated_eligibility():
    base = _row(
        episode_key='ep3',
        symbol='SOL',
        percentage='0.001',
        direction='SELL',
        active_step=2,
        start_timestamp_ms=1000,
        episode_start_timestamp_ms=1000,
        cross_feature={'cross_timestamp_ms': 2000},
        intrabar_order_ambiguous=False,
    )
    a = determine_treated_eligibility_at_T(_row(**{**base, 'terminal_event': 'pn_plus_1_before_tp', 'terminal_timestamp_ms': 4000, 'final_episode_label': 'progression'}))
    b = determine_treated_eligibility_at_T(_row(**{**base, 'terminal_event': 'tp_before_pn_plus_1', 'terminal_timestamp_ms': 4000, 'final_episode_label': 'regression'}))
    c = determine_treated_eligibility_at_T(_row(**{**base, 'terminal_event': 'censored', 'terminal_timestamp_ms': 4000, 'final_episode_label': 'censored'}))
    assert a['eligible'] is True and b['eligible'] is True and c['eligible'] is True
    assert a == b == c


def _make_db(path: Path, rows: list[tuple]) -> None:
    con = sqlite3.connect(str(path))
    con.execute(
        '''
        create table candles(
            symbol text not null,
            open_time integer not null,
            open text not null,
            high text not null,
            low text not null,
            close text not null,
            volume text not null,
            close_time integer not null,
            quote_volume text not null,
            trades integer not null,
            taker_buy_base text not null,
            taker_buy_quote text not null,
            source text not null,
            market text not null,
            timeframe text not null
        )
        '''
    )
    con.executemany('insert into candles values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
    con.commit()
    con.close()


def _rows():
    return [
        ('BTC', 1, '1', '2', '0.5', '1.5', '10', 60, '15', 1, '5', '7', 'binance', 'spot', '1m'),
        ('BTC', 61, '1.5', '2.5', '1', '2', '20', 120, '30', 1, '10', '15', 'binance', 'spot', '1m'),
        ('ETH', 1, '10', '11', '9', '10.5', '3', 60, '31.5', 1, '1', '2', 'binance', 'spot', '1m'),
    ]


def test_validation_db_matches_canonical_fingerprint():
    db = Path('/root/kam/fibolearn/data/fl_vwap_005_binance_validation.sqlite')
    assert validate_inputs(db)['fingerprint_match'] is True
    assert canonical_validation_fingerprint(db) == EXPECTED_FINGERPRINT


def test_identical_logical_rows_same_fingerprint_across_physical_layouts():
    scratch = Path('/root/kam/tmp/fl_vwap_005_fingerprint_tests')
    scratch.mkdir(parents=True, exist_ok=True)
    a = scratch / 'a.sqlite'
    b = scratch / 'b.sqlite'
    rows = _rows()
    _make_db(a, rows)
    _make_db(b, rows)
    con = sqlite3.connect(str(b))
    con.execute('pragma journal_mode=WAL')
    con.execute('pragma synchronous=NORMAL')
    con.execute('vacuum')
    con.close()
    assert canonical_validation_fingerprint(a) == canonical_validation_fingerprint(b)


def test_single_primitive_field_change_changes_fingerprint():
    scratch = Path('/root/kam/tmp/fl_vwap_005_fingerprint_tests')
    scratch.mkdir(parents=True, exist_ok=True)
    a = scratch / 'c.sqlite'
    b = scratch / 'd.sqlite'
    rows = _rows()
    _make_db(a, rows)
    changed = []
    for i, r in enumerate(rows):
        if i == 0:
            rr = list(r)
            rr[1] = 2
            changed.append(tuple(rr))
        else:
            changed.append(r)
    _make_db(b, changed)
    assert canonical_validation_fingerprint(a) != canonical_validation_fingerprint(b)
