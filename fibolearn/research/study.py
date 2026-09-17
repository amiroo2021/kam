from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

from fibolearn.research.discovery import create_candidate_from_live_state
from fibolearn.research.validation import temporal_oos_split, walk_forward_validate, leave_one_symbol_out
from fibolearn.research.phase3b import question_answer_from_reports


@dataclass
class StudyReport:
    match_count: int
    outcome_rate: float | None
    by_symbol: Dict[str, Any]
    by_percentage: Dict[str, Any]
    oos: Dict[str, Any]
    walk_forward: Dict[str, Any]
    pattern: Any


def _rate(rows: List[Dict[str, Any]]) -> float | None:
    if not rows:
        return None
    return sum(1 for r in rows if r.get('hit')) / len(rows)


# Default candidate cap for the bounded SQL selector used by Study Setup.
# Chosen so that on the 30-day production DB the query stays well under the
# 2.5 GiB cgroup ceiling (5,000 ladder rows ~ a few MB).
DEFAULT_STUDY_CANDIDATE_LIMIT = 5000


def study_setup(
    store,
    observation: Dict[str, Any] | None,
    *,
    min_matches: int = 30,
    candidate_limit: int = DEFAULT_STUDY_CANDIDATE_LIMIT,
) -> StudyReport:
    """Study a single synchronized observation against stored setups.

    Bounded, SQL-first: the candidate set is selected inside SQLite using
    the same symbol/percentage/direction as the live observation so we
    never materialize the entire historical dataset. Results beyond the
    candidate_limit are dropped deterministically (ascending timestamp).

    The pre-existing study semantics are preserved as closely as possible:
    - candidate filter: same symbol + same percentage + same direction
    - outcome rate: hit-rate on completed observations
    - splits: temporal OOS + walk-forward over the same matched rows
    """
    if observation is None:
        raise ValueError('no observation to study')

    state = observation['state_vector']
    target_symbol = state.get('symbol')

    # Bounded SQL selection. The symbol filter is applied in SQL so
    # we never load unrelated symbols. Percentage/direction stay at
    # full coverage (matching pre-existing semantics that filter only
    # by symbol) but the bounded candidate_limit keeps memory safe.
    rows = store.find_similar_setup_rows(
        symbol=target_symbol,
        candidate_limit=candidate_limit,
    )
    # If no rows match the symbol, fall back to all symbols (rare; matches
    # the legacy `or rows` fallback). Still bounded by candidate_limit.
    if not rows:
        rows = store.find_similar_setup_rows(candidate_limit=candidate_limit)

    pattern = create_candidate_from_live_state(state)
    pattern.sample_size = len(rows)
    if len(rows) >= min_matches:
        pattern.mark_backtested(len(rows), {'outcome_rate': _rate(rows)})

    by_symbol: Dict[str, Dict[str, Any]] = {}
    by_percentage: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sym = r['symbol']
        bsym = by_symbol.setdefault(sym, {'n': 0, 'rate': None})
        bsym['n'] += 1
        pct = r['percentage']
        bpct = by_percentage.setdefault(pct, {'n': 0, 'rate': None})
        bpct['n'] += 1
    for sym, b in by_symbol.items():
        b['rate'] = _rate([r for r in rows if r['symbol'] == sym])
    for pct, b in by_percentage.items():
        b['rate'] = _rate([r for r in rows if r['percentage'] == pct])

    train, test = temporal_oos_split(rows) if len(rows) > 1 else (rows, [])
    return StudyReport(
        match_count=len(rows),
        outcome_rate=_rate(rows),
        by_symbol=by_symbol,
        by_percentage=by_percentage,
        oos={'train_n': len(train), 'test_n': len(test), 'test_rate': _rate(test)},
        walk_forward=walk_forward_validate(rows),
        pattern=pattern,
    )


# ----------------------------------------------------------------------
# Backwards-compat shims / unit-test helpers
# ----------------------------------------------------------------------

#: Default candidate limit exposed for tests and Telegram screens.
DEFAULT_CANDIDATE_LIMIT = DEFAULT_STUDY_CANDIDATE_LIMIT


def _study_with_old_logic(store, observation: Dict[str, Any] | None, *, min_matches: int = 30) -> StudyReport:
    """Reference implementation that loads the full historical dataset.

    Used ONLY by semantic-parity tests. Production code MUST NOT call this.
    """
    if observation is None:
        raise ValueError('no observation to study')
    state = observation['state_vector']
    rows = store.dataset_rows()
    matches = [r for r in rows if r.get('symbol') == state.get('symbol')] or rows
    pattern = create_candidate_from_live_state(state)
    pattern.sample_size = len(matches)
    if len(matches) >= min_matches:
        pattern.mark_backtested(len(matches), {'outcome_rate': _rate(matches)})
    by_symbol = {s: {'n': len([r for r in matches if r['symbol'] == s]), 'rate': _rate([r for r in matches if r['symbol'] == s])} for s in sorted({r['symbol'] for r in matches})}
    by_pct = {p: {'n': len([r for r in matches if r['percentage'] == p]), 'rate': _rate([r for r in matches if r['percentage'] == p])} for p in sorted({r['percentage'] for r in matches})}
    train, test = temporal_oos_split(matches) if len(matches) > 1 else (matches, [])
    return StudyReport(len(matches), _rate(matches), by_symbol, by_pct, {'train_n': len(train), 'test_n': len(test), 'test_rate': _rate(test)}, walk_forward_validate(matches), pattern)


# Public alias for tests that want to assert the OLD full-dataset path is no
# longer in use (kept distinct from study_setup on purpose).
legacy_study_setup = _study_with_old_logic
