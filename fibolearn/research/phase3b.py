from __future__ import annotations

import json
import math
import re
import statistics
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from fibolearn.research.patterns import Pattern, PatternStatus
from fibolearn.research.validation import temporal_oos_split, walk_forward_validate, leave_one_symbol_out

REPORT_DIR = Path('/root/kam/fibolearn/reports')
DEFAULT_MIN_CELL_N = 30
DEFAULT_MIN_PATTERN_N = 30
DEFAULT_TRAIN_FRACTION = Decimal('0.7')


@dataclass(frozen=True)
class EpisodeFeatureRow:
    episode_key: str
    symbol: str
    percentage: str
    direction: str
    active_step: int
    cycle_id: str
    start_timestamp_ms: int
    end_timestamp_ms: int
    duration_ms: int
    terminal_event: str
    intrabar_order_ambiguous: bool
    order_classification: str
    progression: bool
    regression: bool
    censored: bool
    whole_ladder_vwap_condition_present: bool | None = None
    whole_ladder_vwap_condition_at_start: bool | None = None
    whole_ladder_vwap_cross_timestamp_ms: int | None = None
    whole_ladder_vwap_cross_fraction_elapsed: float | None = None
    whole_ladder_vwap_cross_timing_bin: str | None = None
    whole_ladder_vwap_distance_to_pn_at_cross: float | None = None
    whole_ladder_vwap_distance_to_vwap_at_cross: float | None = None
    feature_version: str = 'phase3b_v1'


def git_commit() -> str | None:
    try:
        out = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd='/root/kam', text=True).strip()
        return out or None
    except Exception:
        return None


def _load_json(value: str | None) -> Dict[str, Any]:
    if not value:
        return {}
    try:
        obj = json.loads(value)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _dec(x: Any) -> Decimal:
    return Decimal(str(x))


def _count_to_rate(numer: int, denom: int) -> float | None:
    if denom <= 0:
        return None
    return numer / denom


def wilson_ci(successes: int, n: int, z: float = 1.96) -> Tuple[float, float] | None:
    if n <= 0:
        return None
    p = successes / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def _log_or_ci(a: int, b: int, c: int, d: int, z: float = 1.96) -> Tuple[float, Tuple[float, float]] | None:
    # Haldane-Anscombe correction
    aa, bb, cc, dd = [x + 0.5 for x in (a, b, c, d)]
    or_value = (aa * dd) / (bb * cc)
    se = math.sqrt(1 / aa + 1 / bb + 1 / cc + 1 / dd)
    log_or = math.log(or_value)
    return or_value, (math.exp(log_or - z * se), math.exp(log_or + z * se))


def _two_prop_z_pvalue(a: int, n1: int, c: int, n0: int) -> float | None:
    if n1 <= 0 or n0 <= 0:
        return None
    p1 = a / n1
    p0 = c / n0
    pooled = (a + c) / (n1 + n0)
    se = math.sqrt(max(1e-12, pooled * (1 - pooled) * (1 / n1 + 1 / n0)))
    z = abs((p1 - p0) / se)
    # two-sided normal approximation
    return math.erfc(z / math.sqrt(2))


def load_episodes(store, *, include_ambiguous: bool = False) -> List[Dict[str, Any]]:
    episodes = store.episode_rows(include_ambiguous=True)
    out = []
    for ep in episodes:
        if not include_ambiguous and ep.get('intrabar_order_ambiguous'):
            continue
        out.append(ep)
    return out


def classify_episode(ep: Dict[str, Any]) -> str:
    term = ep.get('terminal_event')
    if term == 'pn_plus_1_before_tp':
        return 'progression'
    if term == 'tp_before_pn_plus_1':
        return 'regression'
    return 'censored'


def _obs_rows_for_episode(store, ep: Dict[str, Any]) -> List[Dict[str, Any]]:
    # Fetch by episode's timestamp window to avoid SQLite variable limits and
    # keep the replay aligned to stored observation chronology.
    start_ts = int(ep['start_timestamp_ms'])
    end_ts = int(ep['end_timestamp_ms'])
    pct = ep['percentage']
    side = ep['direction']
    with store._connect() as c:
        rows = c.execute(
            'select o.id as mid, o.timestamp_ms as ts, o.market_json, l.ladder_state_json '
            'from market_observations o join ladder_observations l on l.market_id=o.id '
            'where o.symbol=? and o.timestamp_ms between ? and ? and l.percentage=? and l.direction=? '
            'order by o.timestamp_ms asc, o.id asc',
            (ep['symbol'], start_ts, end_ts, pct, side),
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            'mid': int(r['mid']),
            'timestamp_ms': int(r['ts']),
            'market': _load_json(r['market_json']),
            'ladder': _load_json(r['ladder_state_json']),
        })
    return out


def _cross_scale_snapshot(store, ep: Dict[str, Any]) -> Dict[str, Any]:
    ts = int(ep['start_timestamp_ms'])
    symbol = ep['symbol']
    with store._connect() as c:
        rows = c.execute(
            'select o.timestamp_ms, l.percentage, l.direction, l.ladder_state_json '
            'from market_observations o join ladder_observations l on l.market_id=o.id '
            'where o.symbol=? and o.timestamp_ms=? order by l.percentage asc, l.direction asc',
            (symbol, ts),
        ).fetchall()
    out: Dict[str, Any] = {}
    for r in rows:
        lad = _load_json(r['ladder_state_json'])
        pct = str(r['percentage'])
        out[f'{pct}:{r["direction"]}'] = {
            'active_step': int(lad.get('active_step') or 0),
            'progression_state': lad.get('progression_state'),
            'progressing': bool(lad.get('progressing')),
            'distance_to_next_step_norm': lad.get('normalized_distance_to_next_step'),
            'distance_to_tp_norm': lad.get('normalized_distance_to_tp'),
            'distance_to_pn2_norm': lad.get('normalized_distance_to_pn_plus_2'),
        }
    return out


def compute_whole_ladder_vwap_metrics(ep: Dict[str, Any], obs_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    start_state = ep.get('start_state') or {}
    start_market = start_state.get('market') or {}
    start_price = _dec(start_market.get('price')) if start_market.get('price') is not None else None
    start_vwap = _dec(start_market.get('vwap')) if start_market.get('vwap') is not None else None
    # whole-ladder VWAP is a cumulative VWAP from episode start across the episode's observations
    cum_base = Decimal(0)
    cum_quote = Decimal(0)
    pn = None
    if obs_rows:
        pn = obs_rows[0]['ladder'].get('pn')
        if pn is not None:
            pn = _dec(pn)
    if pn is None:
        return {
            'feature_name': 'whole_ladder_vwap_v1',
            'available': False,
            'condition_present': None,
            'condition_at_start': None,
            'cross_timestamp_ms': None,
            'cross_fraction_elapsed': None,
            'cross_timing_bin': None,
            'distance_to_pn_at_cross': None,
            'distance_to_vwap_at_cross': None,
        }
    direction = ep['direction']
    condition_at_start = None
    cross_ts = None
    cross_fraction = None
    timing_bin = None
    distance_pn = None
    distance_vwap = None
    for idx, row in enumerate(obs_rows):
        market = row['market']
        price = _dec(market.get('price')) if market.get('price') is not None else None
        volume = _dec(market.get('volume')) if market.get('volume') is not None else None
        if price is None or volume is None:
            continue
        cum_quote += price * volume
        cum_base += volume
        vwap = cum_quote / cum_base if cum_base else price
        cond = vwap >= pn if direction == 'SELL' else vwap <= pn
        if idx == 0:
            condition_at_start = bool(cond)
        if cross_ts is None and cond:
            cross_ts = int(row['timestamp_ms'])
            duration = max(1, int(ep.get('duration_ms') or (int(ep['end_timestamp_ms']) - int(ep['start_timestamp_ms']))))
            cross_fraction = max(0.0, min(1.0, (cross_ts - int(ep['start_timestamp_ms'])) / duration))
            if cross_fraction <= (1 / 3):
                timing_bin = 'early'
            elif cross_fraction <= (2 / 3):
                timing_bin = 'middle'
            else:
                timing_bin = 'late'
            distance_pn = float(vwap - pn)
            distance_vwap = float(price - vwap)
            break
    return {
        'feature_name': 'whole_ladder_vwap_v1',
        'available': True,
        'condition_present': cross_ts is not None,
        'condition_at_start': condition_at_start,
        'cross_timestamp_ms': cross_ts,
        'cross_fraction_elapsed': cross_fraction,
        'cross_timing_bin': timing_bin,
        'distance_to_pn_at_cross': distance_pn,
        'distance_to_vwap_at_cross': distance_vwap,
        'start_price': float(start_price) if start_price is not None else None,
        'start_market_vwap': float(start_vwap) if start_vwap is not None else None,
    }


def episode_feature_row(store, ep: Dict[str, Any]) -> EpisodeFeatureRow:
    obs_rows = _obs_rows_for_episode(store, ep)
    vw = compute_whole_ladder_vwap_metrics(ep, obs_rows)
    cls = classify_episode(ep)
    return EpisodeFeatureRow(
        episode_key=ep['episode_key'],
        symbol=ep['symbol'],
        percentage=ep['percentage'],
        direction=ep['direction'],
        active_step=int(ep['active_step']),
        cycle_id=str(ep['cycle_id']),
        start_timestamp_ms=int(ep['start_timestamp_ms']),
        end_timestamp_ms=int(ep['end_timestamp_ms']),
        duration_ms=max(0, int(ep['end_timestamp_ms']) - int(ep['start_timestamp_ms'])),
        terminal_event=ep['terminal_event'],
        intrabar_order_ambiguous=bool(ep.get('intrabar_order_ambiguous')),
        order_classification=ep.get('intrabar_order_classification') or ('same_candle_ambiguous' if ep.get('intrabar_order_ambiguous') else 'definitely_reconstructable'),
        progression=cls == 'progression',
        regression=cls == 'regression',
        censored=cls == 'censored',
        whole_ladder_vwap_condition_present=vw['condition_present'],
        whole_ladder_vwap_condition_at_start=vw['condition_at_start'],
        whole_ladder_vwap_cross_timestamp_ms=vw['cross_timestamp_ms'],
        whole_ladder_vwap_cross_fraction_elapsed=vw['cross_fraction_elapsed'],
        whole_ladder_vwap_cross_timing_bin=vw['cross_timing_bin'],
        whole_ladder_vwap_distance_to_pn_at_cross=vw['distance_to_pn_at_cross'],
        whole_ladder_vwap_distance_to_vwap_at_cross=vw['distance_to_vwap_at_cross'],
    )


def episode_baseline_table(store, *, include_ambiguous: bool = False, min_n: int = DEFAULT_MIN_CELL_N) -> Dict[str, Any]:
    eps = load_episodes(store, include_ambiguous=include_ambiguous)
    cells: Dict[Tuple[str, str, str, int], Dict[str, Any]] = {}
    for ep in eps:
        key = (ep['symbol'], ep['percentage'], ep['direction'], int(ep['active_step']))
        cell = cells.setdefault(key, {'symbol': ep['symbol'], 'percentage': ep['percentage'], 'direction': ep['direction'], 'active_step': int(ep['active_step']), 'eligible_episodes': 0, 'progression': 0, 'regression': 0, 'censored': 0, 'ambiguous_excluded': 0, 'low_n': False, 'progression_rate': None, 'ci95': None})
        if ep.get('intrabar_order_ambiguous'):
            cell['ambiguous_excluded'] += 1
            continue
        cell['eligible_episodes'] += 1
        cls = classify_episode(ep)
        if cls == 'progression':
            cell['progression'] += 1
        elif cls == 'regression':
            cell['regression'] += 1
        else:
            cell['censored'] += 1
    for cell in cells.values():
        order_sensitive_n = cell['progression'] + cell['regression']
        cell['order_sensitive_n'] = order_sensitive_n
        cell['low_n'] = order_sensitive_n < min_n
        if order_sensitive_n >= min_n and order_sensitive_n > 0:
            cell['progression_rate'] = cell['progression'] / order_sensitive_n
            cell['ci95'] = wilson_ci(cell['progression'], order_sensitive_n)
        else:
            cell['progression_rate'] = None
            cell['ci95'] = None
    return {
        'feature_version': 'phase3b_baseline_v1',
        'min_n': min_n,
        'cells': list(cells.values()),
        'ambiguous_total': sum(c['ambiguous_excluded'] for c in cells.values()),
        'eligible_total': sum(c['eligible_episodes'] for c in cells.values()),
    }


def _split_train_oos(store, episodes: List[Dict[str, Any]], test_fraction: Decimal = DEFAULT_TRAIN_FRACTION) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    # group by cycle to avoid overlapping episodes from the same cycle spanning both sets
    groups = {}
    for ep in episodes:
        gk = (ep['symbol'], ep['percentage'], ep['direction'], str(ep['cycle_id']))
        grp = groups.setdefault(gk, {'timestamp_ms': int(ep['start_timestamp_ms']), 'episodes': []})
        if int(ep['start_timestamp_ms']) < grp['timestamp_ms']:
            grp['timestamp_ms'] = int(ep['start_timestamp_ms'])
        grp['episodes'].append(ep)
    grouped = sorted(groups.values(), key=lambda g: g['timestamp_ms'])
    if not grouped:
        return [], []
    cut = max(1, int(len(grouped) * (Decimal(1) - test_fraction)))
    train_groups = grouped[:cut]
    test_groups = grouped[cut:]
    train = [ep for g in train_groups for ep in g['episodes']]
    test = [ep for g in test_groups for ep in g['episodes']]
    return train, test


def _rate_and_ci(rows: List[Any]) -> Dict[str, Any]:
    n = len(rows)
    prog = sum(1 for r in rows if getattr(r, 'progression', False) or (isinstance(r, dict) and r.get('progression')))
    reg = sum(1 for r in rows if getattr(r, 'regression', False) or (isinstance(r, dict) and r.get('regression')))
    cen = sum(1 for r in rows if getattr(r, 'censored', False) or (isinstance(r, dict) and r.get('censored')))
    amb = sum(1 for r in rows if getattr(r, 'intrabar_order_ambiguous', False) or (isinstance(r, dict) and r.get('intrabar_order_ambiguous')))
    order_n = prog + reg
    rate = _count_to_rate(prog, order_n)
    ci = wilson_ci(prog, order_n) if order_n else None
    return {'n_episodes': n, 'progression': prog, 'regression': reg, 'censored': cen, 'ambiguous_excluded': amb, 'order_sensitive_n': order_n, 'progression_rate': rate, 'ci95': ci}


def fl_vwap_experiment(store, *, min_n: int = DEFAULT_MIN_CELL_N, train_fraction: Decimal = DEFAULT_TRAIN_FRACTION) -> Dict[str, Any]:
    episodes = [episode_feature_row(store, ep) for ep in load_episodes(store, include_ambiguous=False)]
    train, test = _split_train_oos(store, [ep.__dict__ for ep in episodes], test_fraction=train_fraction)
    train_rows = [EpisodeFeatureRow(**ep) for ep in train]
    test_rows = [EpisodeFeatureRow(**ep) for ep in test]

    by_cell: Dict[Tuple[str, str, str, int], Dict[str, Any]] = {}
    for ep in episodes:
        key = (ep.symbol, ep.percentage, ep.direction, ep.active_step)
        cell = by_cell.setdefault(key, {
            'symbol': ep.symbol, 'percentage': ep.percentage, 'direction': ep.direction, 'active_step': ep.active_step,
            'baseline': {'n_episodes': 0, 'progression': 0, 'regression': 0, 'censored': 0, 'ambiguous_excluded': 0, 'order_sensitive_n': 0, 'progression_rate': None, 'ci95': None},
            'conditional': {'n_episodes': 0, 'progression': 0, 'regression': 0, 'censored': 0, 'ambiguous_excluded': 0, 'order_sensitive_n': 0, 'progression_rate': None, 'ci95': None},
            'lift_pp': None, 'relative_risk': None, 'odds_ratio': None, 'or_ci95': None,
            'condition_timing': {'start': 0, 'early': 0, 'middle': 0, 'late': 0},
            'low_n': False,
        })
        cell['baseline']['n_episodes'] += 1
        cell['baseline']['ambiguous_excluded'] += int(ep.intrabar_order_ambiguous)
        if ep.progression:
            cell['baseline']['progression'] += 1
        elif ep.regression:
            cell['baseline']['regression'] += 1
        else:
            cell['baseline']['censored'] += 1
        if ep.whole_ladder_vwap_condition_present:
            cell['conditional']['n_episodes'] += 1
            if ep.whole_ladder_vwap_condition_at_start:
                cell['condition_timing']['start'] += 1
            elif ep.whole_ladder_vwap_cross_timing_bin in cell['condition_timing']:
                cell['condition_timing'][ep.whole_ladder_vwap_cross_timing_bin] += 1
            if ep.progression:
                cell['conditional']['progression'] += 1
            elif ep.regression:
                cell['conditional']['regression'] += 1
            else:
                cell['conditional']['censored'] += 1
    for cell in by_cell.values():
        for scope in ('baseline', 'conditional'):
            s = cell[scope]
            s['order_sensitive_n'] = s['progression'] + s['regression']
            if s['order_sensitive_n'] >= min_n and s['order_sensitive_n'] > 0:
                s['progression_rate'] = s['progression'] / s['order_sensitive_n']
                s['ci95'] = wilson_ci(s['progression'], s['order_sensitive_n'])
            else:
                s['progression_rate'] = None
                s['ci95'] = None
        base_n = cell['baseline']['order_sensitive_n']
        cond_n = cell['conditional']['order_sensitive_n']
        cell['low_n'] = base_n < min_n or cond_n < min_n
        if base_n and cond_n:
            base_rate = cell['baseline']['progression_rate'] or 0.0
            cond_rate = cell['conditional']['progression_rate'] or 0.0
            cell['lift_pp'] = cond_rate - base_rate
            cell['relative_risk'] = (cond_rate / base_rate) if base_rate > 0 else None
            or_stats = _log_or_ci(cell['conditional']['progression'], cell['conditional']['regression'], cell['baseline']['progression'], cell['baseline']['regression'])
            if or_stats:
                cell['odds_ratio'], cell['or_ci95'] = or_stats
    pooled = _rate_and_ci(episodes)
    train_eval = _rate_and_ci(train_rows)
    test_eval = _rate_and_ci(test_rows)
    return {
        'experiment_id': 'FL-VWAP-001',
        'feature_version': 'whole_ladder_vwap_v1',
        'dataset_version': 'phase3a_30d_f7b269d',
        'git_commit': git_commit(),
        'train_fraction': float(train_fraction),
        'eligibility_rules': {
            'exclude_intrabar_order_ambiguous': True,
            'unit': 'episode',
            'order_sensitive_outcome': 'P(n+1) before TP/cycle termination',
        },
        'pooled': pooled,
        'train': train_eval,
        'oos': test_eval,
        'cells': list(by_cell.values()),
    }


def discover_patterns(store, fl_result: Dict[str, Any], *, min_samples: int = DEFAULT_MIN_PATTERN_N) -> Dict[str, Any]:
    patterns: List[Dict[str, Any]] = []
    tested = 0
    for cell in fl_result['cells']:
        tested += 1
        base = cell['baseline']
        cond = cell['conditional']
        if base['order_sensitive_n'] < min_samples or cond['order_sensitive_n'] < min_samples:
            patterns.append({
                'name': f"VWAP-{cell['symbol']}-{cell['percentage']}-{cell['direction']}-P{cell['active_step']}",
                'status': PatternStatus.REJECTED.value,
                'reason': 'insufficient sample size',
                'sample_size': {'baseline': base['order_sensitive_n'], 'conditional': cond['order_sensitive_n']},
                'effect_pp': cell['lift_pp'],
            })
            continue
        p = Pattern.create(
            f"whole_ladder_vwap_v1::{cell['symbol']}::{cell['percentage']}::{cell['direction']}::P{cell['active_step']}",
            {'feature': 'whole_ladder_vwap_v1', 'symbol': cell['symbol'], 'percentage': cell['percentage'], 'direction': cell['direction'], 'active_step': cell['active_step']},
            {'dataset': 'phase3a_30d', 'train_fraction': 0.7},
            [cell['symbol']],
        )
        p.mark_backtested(cond['order_sensitive_n'], {'baseline_rate': base['progression_rate'], 'conditional_rate': cond['progression_rate'], 'lift_pp': cell['lift_pp']})
        p.status = PatternStatus.CANDIDATE
        patterns.append({
            'name': p.name,
            'status': p.status.value,
            'sample_size': p.sample_size,
            'baseline_rate': base['progression_rate'],
            'conditional_rate': cond['progression_rate'],
            'lift_pp': cell['lift_pp'],
            'relative_risk': cell['relative_risk'],
            'odds_ratio': cell['odds_ratio'],
            'or_ci95': cell['or_ci95'],
            'p_value': _two_prop_z_pvalue(cond['progression'], max(1, cond['order_sensitive_n']), base['progression'], max(1, base['order_sensitive_n'])),
        })
    return {'tested': tested, 'patterns': patterns}


def evaluate_oos(fl_result: Dict[str, Any], *, train_fraction: Decimal = DEFAULT_TRAIN_FRACTION) -> Dict[str, Any]:
    # The FL report already carries a train/test split, but expose a concise OOS summary.
    return {
        'train_fraction': float(train_fraction),
        'note': 'Temporal group split by cycle_id and start timestamp; no ambiguous episodes in order-sensitive OOS.',
        'cells_evaluated': len(fl_result['cells']),
        'train': fl_result['train'],
        'oos': fl_result['oos'],
    }


def question_answer_from_reports(question: str, report_dir: Path = REPORT_DIR) -> Dict[str, Any] | None:
    q = question.strip().lower()
    m = re.search(r'(btc|eth|sol|zec|paxg)\s+(buy|sell)\s+([0-9.]+)\s+p(\d+)', q)
    if not m:
        return None
    symbol, side, pct, step = m.group(1).upper(), m.group(2).upper(), m.group(3), int(m.group(4))
    report = report_dir / 'phase3b_fl_vwap_001.json'
    if not report.exists():
        return {'error': 'phase3b_fl_vwap_001.json not found'}
    data = json.loads(report.read_text())
    for cell in data.get('cells', []):
        if cell['symbol'] == symbol and cell['direction'] == side and str(cell['percentage']) == str(pct) and int(cell['active_step']) == step:
            return cell
    return {'error': 'no matching experiment cell found', 'symbol': symbol, 'direction': side, 'percentage': pct, 'active_step': step}


def run_phase3b(store, *, report_dir: Path = REPORT_DIR, train_fraction: Decimal = DEFAULT_TRAIN_FRACTION, min_baseline_n: int = DEFAULT_MIN_CELL_N, min_pattern_n: int = DEFAULT_MIN_PATTERN_N) -> Dict[str, Any]:
    report_dir.mkdir(parents=True, exist_ok=True)
    baselines = episode_baseline_table(store, min_n=min_baseline_n)
    fl = fl_vwap_experiment(store, min_n=min_baseline_n, train_fraction=train_fraction)
    patterns = discover_patterns(store, fl, min_samples=min_pattern_n)
    oos = evaluate_oos(fl, train_fraction=train_fraction)
    summary = {
        'git_commit': git_commit(),
        'dataset': {'symbol_count': 5, 'time_window': 'existing 30-day dataset only'},
        'baselines': {
            'cells': len(baselines['cells']),
            'ambiguous_total': baselines['ambiguous_total'],
            'eligible_total': baselines['eligible_total'],
        },
        'fl_vwap_001': {
            'cells': len(fl['cells']),
            'pooled': fl['pooled'],
            'train': fl['train'],
            'oos': fl['oos'],
        },
        'patterns': {
            'tested': patterns['tested'],
            'found': len(patterns['patterns']),
            'validated': sum(1 for p in patterns['patterns'] if p['status'] == PatternStatus.VALIDATED.value),
            'rejected': sum(1 for p in patterns['patterns'] if p['status'] == PatternStatus.REJECTED.value),
        },
        'notes': [
            'Order-sensitive analyses exclude intrabar_order_ambiguous=true episodes.',
            'Whole-ladder VWAP is derived from episode observation sequences, not UTC daily VWAP.',
            'No live GoldenFibo behavior was modified.',
        ],
    }
    (report_dir / 'phase3b_baselines.json').write_text(json.dumps(baselines, indent=2, sort_keys=True))
    (report_dir / 'phase3b_fl_vwap_001.json').write_text(json.dumps(fl, indent=2, sort_keys=True))
    (report_dir / 'phase3b_pattern_discovery.json').write_text(json.dumps(patterns, indent=2, sort_keys=True))
    (report_dir / 'phase3b_oos.json').write_text(json.dumps(oos, indent=2, sort_keys=True))
    (report_dir / 'phase3b_summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True))
    return {'baselines': baselines, 'fl': fl, 'patterns': patterns, 'oos': oos, 'summary': summary}
