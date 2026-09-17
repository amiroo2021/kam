from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fibolearn.backtest.engine import replay_multiscale
from fibolearn.storage.sqlite_store import FiboLearnStore


def k(ts, o, h, l, c, v='10'):
    return [ts, str(o), str(h), str(l), str(c), v, ts + 59999, str(Decimal(str(c)) * Decimal(str(v))), 1, '0', '0', '0']


def test_optimized_replay_matches_legacy_replay_for_all_eight_ladder_states(tmp_path):
    legacy_path = tmp_path / 'legacy.sqlite'
    optimized_path = tmp_path / 'opt.sqlite'
    candles = [k(i * 60_000, 100, 100.001, 99.999, 100) for i in range(20)]
    candles.append(k(20 * 60_000, 100, 100.001, 99.8, 99.9))
    candles.append(k(21 * 60_000, 100, 100.001, 99.7, 99.8))

    store = FiboLearnStore(legacy_path, use_optimized_layout=False)
    for vec in replay_multiscale('BTC', candles, percentages=(Decimal('0.001'),), directions=('BUY',), use_optimized_market=False):
        store.save_observation(vec)
    store.rebuild_episodes()
    legacy = store.all_observations()
    legacy_baselines = store.episode_baselines()

    store2 = FiboLearnStore(optimized_path, use_optimized_layout=True)
    for vec in replay_multiscale('BTC', candles, percentages=(Decimal('0.001'),), directions=('BUY',), use_optimized_market=True):
        store2.save_observation(vec)
    store2.rebuild_episodes()
    opt = store2.all_observations()
    opt_baselines = store2.episode_baselines()

    def fingerprint(rows):
        out = []
        for o in rows:
            sv = o['state_vector']
            for pct, sides in sv['ladders'].items():
                for side, lad in sides.items():
                    key = (lad.get('active_step'), lad.get('pn'), lad.get('pn_plus_1'), lad.get('pn_plus_2'), lad.get('current_price'), lad.get('progressing'))
                    out.append((o['id'], pct, side, key))
        return sorted(out)

    assert fingerprint(legacy) == fingerprint(opt)
    assert legacy_baselines[('BTC', '0.001', 'BUY')]['completed_episodes'] == opt_baselines[('BTC', '0.001', 'BUY')]['completed_episodes']
    assert legacy_baselines[('BTC', '0.001', 'BUY')]['pn_plus_1_before_tp'] + legacy_baselines[('BTC', '0.001', 'BUY')]['tp_before_pn_plus_1'] == opt_baselines[('BTC', '0.001', 'BUY')]['pn_plus_1_before_tp'] + opt_baselines[('BTC', '0.001', 'BUY')]['tp_before_pn_plus_1']


def test_market_observations_are_stored_once_per_timestamp(tmp_path):
    store = FiboLearnStore(tmp_path / 'fibolearn.sqlite', use_optimized_layout=True)
    candles = [k(i * 60_000, 100, 100.001, 99.999, 100) for i in range(5)]
    for vec in replay_multiscale('BTC', candles, percentages=(Decimal('0.001'),), directions=('BUY',), use_optimized_market=True):
        store.save_observation(vec)
    market_count = store.market_observation_count('BTC')
    assert market_count == 5


def test_minimum_research_sample_thresholds_permit_or_deny_analysis(tmp_path):
    store = FiboLearnStore(tmp_path / 'fibolearn.sqlite', use_optimized_layout=True)
    base = [k(i * 60_000, 100, 100.001, 99.999, 100) for i in range(6)]
    candles = base + [k(6 * 60_000, 100, 100.001, 99.8, 99.9)]
    for vec in replay_multiscale('BTC', candles, percentages=(Decimal('0.001'),), directions=('BUY',), use_optimized_market=True):
        store.save_observation(vec)
    store.rebuild_episodes()
    res = store.research_gate('BTC', '0.001', 'BUY', min_episodes=30, min_oos_episodes=15, min_per_symbol_episodes=10)
    assert res['permitted'] is False
    assert res['completed_episodes'] < res['min_episodes']

    res2 = store.research_gate('BTC', '0.001', 'BUY', min_episodes=1, min_oos_episodes=1, min_per_symbol_episodes=1)
    assert res2['permitted'] is True

def test_data_coverage_matrix_renders_completed_episodes_per_symbol_pct_side(tmp_path):
    store = FiboLearnStore(tmp_path / 'fibolearn.sqlite', use_optimized_layout=True)
    candles = [k(i * 60_000, 100, 100.001, 99.999, 100) for i in range(6)]
    candles.append(k(6 * 60_000, 100, 100.001, 99.8, 99.9))
    candles.append(k(7 * 60_000, 100, 100.001, 99.7, 99.8))
    for vec in replay_multiscale('BTC', candles, percentages=(Decimal('0.001'),), directions=('BUY',), use_optimized_market=True):
        store.save_observation(vec)
    store.rebuild_episodes()
    matrix = store.data_coverage_matrix()
    assert matrix['cells'][('BTC', '0.001', 'BUY')]['completed_episodes'] >= 1
    assert matrix['cells'][('BTC', '0.001', 'SELL')]['completed_episodes'] == 0


def test_time_dependent_baselines_include_eligible_n(tmp_path):
    store = FiboLearnStore(tmp_path / 'fibolearn.sqlite', use_optimized_layout=True)
    candles = [k(i * 60_000, 100, 100.001, 99.999, 100) for i in range(10)]
    candles.append(k(10 * 60_000, 100, 100.001, 99.8, 99.9))
    for vec in replay_multiscale('BTC', candles, percentages=(Decimal('0.001'),), directions=('BUY',), use_optimized_market=True):
        store.save_observation(vec)
    store.rebuild_episodes()
    td = store.time_dependent_baselines(minutes=(1, 3, 5))
    cell = td[('BTC', '0.001', 'BUY')][5]
    assert 'eligible_episodes' in cell
    assert cell['eligible_episodes'] == 1


def test_telegram_data_coverage_screen_renders_matrix(tmp_path):
    store = FiboLearnStore(tmp_path / 'fibolearn.sqlite', use_optimized_layout=True)
    candles = [k(i * 60_000, 100, 100.001, 99.999, 100) for i in range(8)]
    for vec in replay_multiscale('BTC', candles, percentages=(Decimal('0.001'),), directions=('BUY',), use_optimized_market=True):
        store.save_observation(vec)
    store.rebuild_episodes()
    from fibolearn.telegram.wizard import FiboLearnWizard
    wiz = FiboLearnWizard(store=store)
    screen = wiz.handle_callback('fibolearn:report:coverage')
    text = screen.text
    assert 'Data Coverage' in text
    assert 'BTC' in text and 'ETH' in text
    assert '1%' in text and '0.001%' in text
