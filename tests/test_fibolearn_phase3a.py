from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fibolearn.backtest.engine import replay_multiscale
from fibolearn.storage.sqlite_store import FiboLearnStore


def k(ts, o, h, l, c, v='10'):
    return [ts, str(o), str(h), str(l), str(c), v, ts + 59999, str(Decimal(str(c))*Decimal(str(v))), 1, '0', '0', '0']


def test_episode_layer_deduplicates_many_minute_snapshots_of_one_active_pn(tmp_path):
    store = FiboLearnStore(tmp_path / 'fibolearn.sqlite')
    candles = [
        k(0, 100, 100.01, 99.99, 100),
        k(60_000, 100, 100.01, 99.99, 100),
        k(120_000, 100, 100.01, 99.99, 100),
        k(180_000, 100, 100.01, 99.99, 100),
    ]
    for vec in replay_multiscale('BTC', candles, percentages=(Decimal('0.001'),), directions=('BUY',)):
        store.save_observation(vec)

    built = store.rebuild_episodes()
    rows = store.dataset_rows()
    episodes = store.episode_rows()

    assert built >= 1
    assert len(rows) == 4
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep['observation_count'] == 4
    assert ep['episode_key'].startswith('BTC|0.001|BUY|')
    assert ep['start_timestamp_ms'] == 0
    assert ep['end_timestamp_ms'] == 180_000


def test_episode_baselines_are_event_level_not_observation_level(tmp_path):
    store = FiboLearnStore(tmp_path / 'fibolearn.sqlite')
    candles = [k(i*60_000, 100, 100.001, 99.999, 100) for i in range(5)]
    candles.append(k(5*60_000, 100, 100.001, 99.8, 99.9))  # BUY progression lower level
    for vec in replay_multiscale('BTC', candles, percentages=(Decimal('0.001'),), directions=('BUY',)):
        store.save_observation(vec)
    store.rebuild_episodes()

    obs = store.dataset_rows()
    baselines = store.episode_baselines()

    assert len(obs) == 6
    b = baselines[('BTC', '0.001', 'BUY')]
    assert b['episodes'] == 2  # P0 episode then progressed P1 episode
    assert b['raw_observations'] == 6
    assert b['pn_plus_1_before_tp'] + b['tp_before_pn_plus_1'] + b['other_censored'] == b['episodes']
    assert b['observation_level_success_rate'] != b['episode_level_progression_rate'] or b['raw_observations'] != b['episodes']


def test_feature_definitions_and_significant_levels_are_versioned(tmp_path):
    store = FiboLearnStore(tmp_path / 'fibolearn.sqlite')
    vec = replay_multiscale('BTC', [k(0, 100, 101, 99, 100)], percentages=(Decimal('0.001'),), directions=('BUY','SELL'))[0]
    oid = store.save_observation(vec)
    obs = store.get_observation(oid)['state_vector']

    assert obs['feature_definition']['version']
    assert obs['market']['vwap_definition'] == 'utc_daily_cumulative_quote_volume_over_base_volume_v1'
    levels = obs['features']['significant_levels_versioned']
    for key in ['VWAP', 'POC', 'VAH', 'VAL', 'swing_high', 'swing_low', 'previous_day_high', 'previous_day_low']:
        assert key in levels
        assert levels[key]['feature_version']
        assert levels[key]['calculation_definition']


def test_time_dependent_baseline_conditions_on_episode_elapsed_time(tmp_path):
    store = FiboLearnStore(tmp_path / 'fibolearn.sqlite')
    candles = [k(i*60_000, 100, 100.001, 99.999, 100) for i in range(10)]
    candles.append(k(10*60_000, 100, 100.001, 99.8, 99.9))
    for vec in replay_multiscale('BTC', candles, percentages=(Decimal('0.001'),), directions=('BUY',)):
        store.save_observation(vec)
    store.rebuild_episodes()

    td = store.time_dependent_baselines(minutes=(1,3,5))
    assert ('BTC', '0.001', 'BUY') in td
    assert td[('BTC', '0.001', 'BUY')][5]['eligible_episodes'] == 1
    assert td[('BTC', '0.001', 'BUY')][5]['pn_plus_1_before_tp'] + td[('BTC', '0.001', 'BUY')][5]['tp_before_pn_plus_1'] + td[('BTC', '0.001', 'BUY')][5]['other_censored'] == td[('BTC', '0.001', 'BUY')][5]['eligible_episodes']


def test_telegram_learning_report_has_dataset_status_and_baselines(tmp_path):
    store = FiboLearnStore(tmp_path / 'fibolearn.sqlite')
    candles = [k(i*60_000, 100, 100.001, 99.999, 100) for i in range(3)]
    for vec in replay_multiscale('BTC', candles, percentages=(Decimal('0.001'),), directions=('BUY',)):
        store.save_observation(vec)
    store.rebuild_episodes()

    from fibolearn.telegram.wizard import FiboLearnWizard
    wiz = FiboLearnWizard(store=store)
    report = wiz.handle_callback('fibolearn:report')
    assert 'Dataset Status' in report.text and 'Baselines' in report.text
    ds = wiz.handle_callback('fibolearn:report:dataset')
    assert 'BTC' in ds.text and 'Candles:' in ds.text and 'Episodes:' in ds.text and 'Historical range:' in ds.text
    base = wiz.handle_callback('fibolearn:report:baseline:BTC:0.001:BUY')
    assert 'BTC / 0.001% / BUY' in base.text
    assert 'Episodes:' in base.text
