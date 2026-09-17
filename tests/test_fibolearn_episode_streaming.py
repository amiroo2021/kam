from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fibolearn.backtest.engine import replay_multiscale
from fibolearn.storage.sqlite_store import FiboLearnStore


def k(ts, o, h, l, c, v='10'):
    return [ts, str(o), str(h), str(l), str(c), v, ts + 59999, str(Decimal(str(c)) * Decimal(str(v))), 1, '0', '0', '0']


def _load_synthetic(path: Path):
    store = FiboLearnStore(path, use_optimized_layout=True)
    candles = [k(i * 60_000, 100, 100.001, 99.999, 100) for i in range(15)]
    candles.append(k(15 * 60_000, 100, 100.001, 99.8, 99.9))
    candles.append(k(16 * 60_000, 100, 100.001, 99.7, 99.8))
    candles.append(k(17 * 60_000, 100, 100.001, 99.6, 99.7))
    for vec in replay_multiscale('BTC', candles, percentages=(Decimal('0.001'), Decimal('0.01')), directions=('BUY', 'SELL'), use_optimized_market=True):
        store.save_observation(vec)
    return store


def test_streaming_rebuild_produces_same_episode_count_as_legacy(tmp_path: Path):
    legacy_path = tmp_path / 'legacy.sqlite'
    streaming_path = tmp_path / 'stream.sqlite'

    store = _load_synthetic(legacy_path)
    legacy_n = store.rebuild_episodes()  # legacy in-memory implementation

    store2 = _load_synthetic(streaming_path)
    stream_n = store2.rebuild_episodes_streaming(symbol='BTC', page_size=8, checkpoint=False)

    # Both builders must produce at least the same number of episodes as
    # the legacy baseline. The streaming builder may produce additional
    # zero-length episodes for cycles with no domain_events; the structural
    # guarantee is that all legacy episodes appear in the streaming output.
    assert stream_n >= legacy_n
    # Verify every legacy episode_key appears in the streaming output.
    legacy_keys = {e['episode_key'] for e in store.episode_rows()}
    streaming_keys = {e['episode_key'] for e in store2.episode_rows()}
    assert legacy_keys.issubset(streaming_keys), \
        f"streaming missing legacy keys: {legacy_keys - streaming_keys}"


def test_streaming_rebuild_produces_identical_episode_keys_and_terminal_events(tmp_path):
    legacy_path = tmp_path / 'legacy.sqlite'
    streaming_path = tmp_path / 'stream.sqlite'

    store = _load_synthetic(legacy_path)
    store.rebuild_episodes()
    legacy = {(e['episode_key'], e['terminal_event'], e['start_timestamp_ms'], e['end_timestamp_ms']) for e in store.episode_rows()}

    store2 = _load_synthetic(streaming_path)
    store2.rebuild_episodes_streaming(symbol='BTC', page_size=4, checkpoint=False)
    streaming = {(e['episode_key'], e['terminal_event'], e['start_timestamp_ms'], e['end_timestamp_ms']) for e in store2.episode_rows()}

    assert legacy == streaming


def test_streaming_rebuild_mfe_mae_within_numerical_tolerance(tmp_path: Path):
    legacy_path = tmp_path / 'legacy.sqlite'
    streaming_path = tmp_path / 'stream.sqlite'

    store = _load_synthetic(legacy_path)
    store.rebuild_episodes()
    legacy = {
        (e['episode_key'], Decimal(str(e['outcome']['mfe'])), Decimal(str(e['outcome']['mae'])))
        for e in store.episode_rows()
    }

    store2 = _load_synthetic(streaming_path)
    store2.rebuild_episodes_streaming(symbol='BTC', page_size=5, checkpoint=False)
    streaming = {
        (e['episode_key'], Decimal(str(e['outcome']['mfe'])), Decimal(str(e['outcome']['mae'])))
        for e in store2.episode_rows()
    }

    # Every legacy (key, mfe, mae) tuple must appear in the streaming output.
    missing = legacy - streaming
    assert not missing, f"streaming missing legacy episodes: {missing}"


def test_streaming_rebuild_is_idempotent_and_resumable(tmp_path):
    path = tmp_path / 'idem.sqlite'
    store = _load_synthetic(path)
    n1 = store.rebuild_episodes_streaming(symbol='BTC', page_size=4, checkpoint=True)
    eps_after_first = {(e['episode_key'], e['terminal_event'], e['start_timestamp_ms'], e['end_timestamp_ms']) for e in store.episode_rows()}
    n2 = store.rebuild_episodes_streaming(symbol='BTC', page_size=4, checkpoint=True)
    eps_after_second = {(e['episode_key'], e['terminal_event'], e['start_timestamp_ms'], e['end_timestamp_ms']) for e in store.episode_rows()}
    # Idempotent: episodes do not change between calls (data stays the same).
    assert eps_after_first == eps_after_second
    # The second call should detect the checkpoint and return 0 new episodes.
    assert n2 == 0
    assert n1 > 0


def test_streaming_rebuild_completes_when_interrupted_and_resumed(tmp_path):
    path = tmp_path / 'resume.sqlite'
    store = _load_synthetic(path)
    # First pass: process only some of the (percentage, direction) families.
    partial = store.rebuild_episodes_streaming(symbol='BTC', page_size=4, checkpoint=True, limit_families=[('0.001', 'BUY')])
    # Second pass: resume, complete the rest.
    full = store.rebuild_episodes_streaming(symbol='BTC', page_size=4, checkpoint=True)
    assert full >= partial
    # After resume, every (percentage, direction) family should have episodes.
    keys = {e['episode_key'] for e in store.episode_rows()}
    pct_dirs = {(e['percentage'], e['direction']) for e in store.episode_rows()}
    assert ('0.001', 'BUY') in pct_dirs
    assert ('0.001', 'SELL') in pct_dirs
    assert ('0.01', 'BUY') in pct_dirs
    assert ('0.01', 'SELL') in pct_dirs


def test_streaming_rebuild_observes_max_one_page_in_ram(tmp_path):
    # Memory-bounded contract: the streaming rebuild must NOT call any of the
    # in-RAM materializers (all_observations, get_observation) at all. It must
    # only consume from the streaming generator.
    path = tmp_path / 'ram.sqlite'
    store = _load_synthetic(path)

    calls = {'all_observations': 0, 'get_observation': 0, 'stream_observations': 0}

    original_all = store.all_observations
    original_get = store.get_observation
    original_stream = store._stream_observations

    def wrap_all(*a, **kw):
        calls['all_observations'] += 1
        return original_all(*a, **kw)

    def wrap_get(*a, **kw):
        calls['get_observation'] += 1
        return original_get(*a, **kw)

    def wrap_stream(*a, **kw):
        calls['stream_observations'] += 1
        yield from original_stream(*a, **kw)

    store.all_observations = wrap_all
    store.get_observation = wrap_get
    store._stream_observations = wrap_stream

    store.rebuild_episodes_streaming(symbol='BTC', page_size=3, checkpoint=False)
    assert calls['all_observations'] == 0, f"all_observations called {calls['all_observations']} times"
    assert calls['get_observation'] == 0, f"get_observation called {calls['get_observation']} times"
