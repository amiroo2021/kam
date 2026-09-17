from __future__ import annotations

from decimal import Decimal

from fibolearn.backtest.engine import replay_multiscale
from fibolearn.storage.sqlite_store import FiboLearnStore, _episode_ordering_classification
import json
import time


def _insert_episode(store, *, episode_key, symbol='BTC', percentage='0.001', direction='BUY', cycle_id='1', active_step=1, start_ts=0, end_ts=60_000, evolution=None, terminal_event='tp_before_pn_plus_1'):
    evolution = evolution or {'event_ordering': []}
    outcome = {'terminal_event': terminal_event, 'terminal_timestamp_ms': end_ts, 'duration_ms': end_ts - start_ts, 'mfe': '0', 'mae': '0', 'event_ordering': evolution.get('event_ordering', [])}
    with store._connect() as c:
        c.execute(
            'insert or replace into episodes(episode_key,symbol,percentage,direction,cycle_id,active_step,start_timestamp_ms,end_timestamp_ms,terminal_event,observation_count,raw_observation_ids_json,start_state_json,end_state_json,evolution_json,outcome_json,intrabar_order_ambiguous,created_at) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (episode_key, symbol, percentage, direction, cycle_id, active_step, start_ts, end_ts, terminal_event, 1, json.dumps([1]), json.dumps({}), json.dumps({}), json.dumps(evolution), json.dumps(outcome), int(_episode_ordering_classification(evolution) == 'same_candle_ambiguous'), int(time.time() * 1000))
        )




def k(ts, o, h, l, c, v='10'):
    return [ts, str(o), str(h), str(l), str(c), v, ts + 59999, str(Decimal(str(c)) * Decimal(str(v))), 1, '0', '0', '0']


def test_same_timestamp_competing_events_become_ambiguous(tmp_path):
    store = FiboLearnStore(tmp_path / 'fibolearn.sqlite', use_optimized_layout=True)
    _insert_episode(
        store,
        episode_key='BTC|0.001|BUY|1|1',
        evolution={'event_ordering': [
            {'event': 'vwap_crossed_pn', 'timestamp_ms': 1000, 'elapsed_ms': 1000},
            {'event': 'poc_crossed_pn', 'timestamp_ms': 1000, 'elapsed_ms': 1000},
        ]},
    )
    rows = store.episode_rows()
    assert len(rows) == 1
    assert rows[0]['intrabar_order_ambiguous'] is True


def test_true_timestamp_reversal_is_detected_separately(tmp_path):
    store = FiboLearnStore(tmp_path / 'fibolearn.sqlite', use_optimized_layout=True)
    _insert_episode(
        store,
        episode_key='BTC|0.001|BUY|1|1',
        evolution={'event_ordering': [
            {'event': 'vwap_crossed_pn', 'timestamp_ms': 2000, 'elapsed_ms': 2000},
            {'event': 'poc_crossed_pn', 'timestamp_ms': 1000, 'elapsed_ms': 1000},
        ]},
    )
    rows = store.episode_rows()
    assert len(rows) == 1
    assert rows[0]['intrabar_order_classification'] == 'true_timestamp_reversal'
    assert rows[0]['intrabar_order_ambiguous'] is False


def test_unambiguous_episodes_remain_unchanged(tmp_path):
    store = FiboLearnStore(tmp_path / 'fibolearn.sqlite', use_optimized_layout=True)
    _insert_episode(
        store,
        episode_key='BTC|0.001|BUY|1|1',
        evolution={'event_ordering': [
            {'event': 'vwap_crossed_pn', 'timestamp_ms': 1000, 'elapsed_ms': 1000},
            {'event': 'poc_crossed_pn', 'timestamp_ms': 2000, 'elapsed_ms': 2000},
        ]},
    )
    rows = store.episode_rows()
    assert len(rows) == 1
    assert rows[0]['intrabar_order_ambiguous'] is False


def test_order_sensitive_analyses_exclude_ambiguous_episodes(tmp_path):
    store = FiboLearnStore(tmp_path / 'fibolearn.sqlite', use_optimized_layout=True)
    _insert_episode(store, episode_key='BTC|0.001|BUY|1|1', evolution={'event_ordering': [
        {'event': 'vwap_crossed_pn', 'timestamp_ms': 1000, 'elapsed_ms': 1000},
        {'event': 'poc_crossed_pn', 'timestamp_ms': 1000, 'elapsed_ms': 1000},
    ]})
    _insert_episode(store, episode_key='BTC|0.001|BUY|2|1', evolution={'event_ordering': [
        {'event': 'vwap_crossed_pn', 'timestamp_ms': 1000, 'elapsed_ms': 1000},
        {'event': 'poc_crossed_pn', 'timestamp_ms': 2000, 'elapsed_ms': 2000},
    ]},)
    baselines = store.episode_baselines(include_ambiguous=False)
    with_ambiguous = store.episode_baselines(include_ambiguous=True)
    cell = ('BTC', '0.001', 'BUY')
    assert baselines[cell]['episodes'] == 1
    assert with_ambiguous[cell]['episodes'] == 2
    assert baselines[cell]['completed_episodes'] == 1
    assert with_ambiguous[cell]['completed_episodes'] == 2


def test_non_order_sensitive_analyses_may_retain_ambiguous_episodes(tmp_path):
    store = FiboLearnStore(tmp_path / 'fibolearn.sqlite', use_optimized_layout=True)
    _insert_episode(store, episode_key='BTC|0.001|BUY|1|1', evolution={'event_ordering': [
        {'event': 'vwap_crossed_pn', 'timestamp_ms': 1000, 'elapsed_ms': 1000},
        {'event': 'poc_crossed_pn', 'timestamp_ms': 1000, 'elapsed_ms': 1000},
    ]})
    status = store.dataset_status_by_symbol()
    assert status['BTC']['episodes'] >= 1
