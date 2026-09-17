from __future__ import annotations

from decimal import Decimal
from pathlib import Path


def _seed_store(path: Path):
    from fibolearn.storage.sqlite_store import FiboLearnStore
    from fibolearn.features.multiscale import build_multiscale_vector
    store = FiboLearnStore(path, use_optimized_layout=True)
    # Seed a small set of synchronized observations
    for i in range(10):
        vec = build_multiscale_vector(
            "BTC",
            1_700_000_000_000 + i * 60_000,
            Decimal("100") + Decimal(i),
            percentages=(Decimal("0.001"), Decimal("0.01")),
            directions=("BUY", "SELL"),
        )
        store.save_observation(vec)
    return store


def _seed_store_and_get_obs(path: Path):
    from fibolearn.features.multiscale import build_multiscale_vector
    store = _seed_store(path)
    vec = build_multiscale_vector("BTC", 1_700_000_000_000, Decimal("100"), percentages=(Decimal("0.001"),), directions=("BUY",))
    store.save_observation(vec)
    return store


def test_study_setup_never_calls_dataset_rows(tmp_path: Path):
    """Production study_setup must use the bounded SQL selector, not dataset_rows()."""
    store = _seed_store(tmp_path / "fibolearn.sqlite")
    from fibolearn.telegram.wizard import FiboLearnWizard

    called = {"dataset_rows": 0, "all_observations": 0, "rebuild_episodes": 0, "rebuild_episodes_streaming": 0, "find_similar_setup_rows": 0}

    orig_dr = store.dataset_rows
    orig_ao = store.all_observations
    orig_re = store.rebuild_episodes
    orig_res = getattr(store, "rebuild_episodes_streaming", None)
    orig_find = store.find_similar_setup_rows

    def wrap_dr(*a, **kw):
        called["dataset_rows"] += 1
        return orig_dr(*a, **kw)

    def wrap_ao(*a, **kw):
        called["all_observations"] += 1
        return orig_ao(*a, **kw)

    def wrap_re(*a, **kw):
        called["rebuild_episodes"] += 1
        return orig_re(*a, **kw)

    def wrap_res(*a, **kw):
        called["rebuild_episodes_streaming"] += 1
        if orig_res is None:
            return None
        return orig_res(*a, **kw)

    def wrap_find(*a, **kw):
        called["find_similar_setup_rows"] += 1
        return orig_find(*a, **kw)

    store.dataset_rows = wrap_dr
    store.all_observations = wrap_ao
    store.rebuild_episodes = wrap_re
    if orig_res is not None:
        store.rebuild_episodes_streaming = wrap_res
    store.find_similar_setup_rows = wrap_find

    wiz = FiboLearnWizard(store=store)
    wiz.handle_callback("fibolearn:study:multiscale:BTC")

    assert called["dataset_rows"] == 0, f"dataset_rows called {called['dataset_rows']} times"
    assert called["all_observations"] == 0, f"all_observations called {called['all_observations']} times"
    assert called["rebuild_episodes"] == 0, f"rebuild_episodes called {called['rebuild_episodes']} times"
    assert called["rebuild_episodes_streaming"] == 0, f"rebuild_episodes_streaming called {called['rebuild_episodes_streaming']} times"
    assert called["find_similar_setup_rows"] >= 1, "find_similar_setup_rows should be the path"


def test_study_setup_respects_candidate_limit(tmp_path: Path):
    store = _seed_store_and_get_obs(tmp_path / "fibolearn.sqlite")
    from fibolearn.research.study import study_setup
    obs = store.latest_observation("BTC")
    report_cap1 = study_setup(store, obs, candidate_limit=1)
    report_cap500 = study_setup(store, obs, candidate_limit=500)
    assert report_cap1.match_count <= 1
    assert report_cap500.match_count <= 500
    # Larger cap should return at least as many rows (deterministic ordering).
    assert report_cap500.match_count >= report_cap1.match_count


def test_study_setup_filters_at_sql_layer(tmp_path: Path):
    """The bounded selector must filter symbol in SQL."""
    store = _seed_store_and_get_obs(tmp_path / "fibolearn.sqlite")
    sqls: list = []

    # Hook SQLite's trace to capture every query on every connection
    # used by this store. This works regardless of context-manager
    # implementation details.
    original_connect = store._connect

    def traced_connect():
        conn = original_connect()
        conn.set_trace_callback(lambda q: sqls.append(q))
        return conn

    store._connect = traced_connect
    try:
        from fibolearn.research.study import study_setup
        obs = store.latest_observation("BTC")
        study_setup(store, obs)
    finally:
        store._connect = original_connect
    find_sqls = [s for s in sqls if "from market_observations" in s and "l.percentage" in s]
    assert find_sqls, f"expected find_similar_setup_rows SQL; saw {sqls}"
    sql = find_sqls[-1].lower()
    assert "where" in sql
    assert "limit" in sql


def test_study_setup_semantic_parity_with_legacy_on_small_data(tmp_path: Path):
    """Bounded vs legacy must yield the same matches when the full set fits the cap."""
    store = _seed_store_and_get_obs(tmp_path / "fibolearn.sqlite")
    from fibolearn.research.study import study_setup, legacy_study_setup
    obs = store.latest_observation("BTC")
    new_report = study_setup(store, obs, candidate_limit=1000)
    legacy_report = legacy_study_setup(store, obs)
    assert new_report.match_count == legacy_report.match_count
    # outcome_rate should match (same numerator/denominator on the same rows)
    assert (new_report.outcome_rate or 0.0) == (legacy_report.outcome_rate or 0.0)
    # by_symbol counts should match
    for sym in legacy_report.by_symbol:
        assert new_report.by_symbol[sym]['n'] == legacy_report.by_symbol[sym]['n']


def test_telegram_wizard_uses_injected_store_only(tmp_path: Path, monkeypatch):
    """FiboLearnWizard must use the injected store and never the default DB."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store = _seed_store_and_get_obs(tmp_path / "fibolearn.sqlite")
    from fibolearn.telegram.wizard import FiboLearnWizard
    wiz = FiboLearnWizard(store=store)
    assert wiz._store is store
    screen = wiz.open()
    assert "/fibolearn" in screen.text
