from __future__ import annotations

import json
import math
import os
import random
import sqlite3
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Tuple

REPORT_DIR = Path('/root/kam/fibolearn/reports')
DB_PATH = Path('/root/.hermes/fibolearn/fibolearn.sqlite')
TRAIN_FRACTION = Decimal('0.7')
MIN_CELL_N = 30
MIN_PATTERN_N = 30

ORIGINAL_LEAKED_CANDIDATES = []
try:
    _orig = json.loads((REPORT_DIR / 'phase3b_pattern_discovery.json').read_text())
    ORIGINAL_LEAKED_CANDIDATES = [p['name'] for p in _orig.get('patterns', []) if p.get('status') == 'CANDIDATE']
except Exception:
    pass


def git_commit() -> str | None:
    try:
        return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd='/root/kam', text=True).strip()
    except Exception:
        return None


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    data = json.dumps(payload, indent=2, sort_keys=True)
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def rss_kb() -> int:
    try:
        with open('/proc/self/status', 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1])
    except Exception:
        pass
    return -1


def peak_rss_kb() -> int:
    try:
        with open('/proc/self/status', 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('VmHWM:'):
                    return int(line.split()[1])
    except Exception:
        pass
    return -1


def loads(s: str | None) -> Dict[str, Any]:
    return json.loads(s) if s else {}


def wilson_ci(successes: int, n: int, z: float = 1.96) -> list[float] | None:
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = (z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)) / denom
    return [max(0.0, center - margin), min(1.0, center + margin)]


def two_prop_or_ci(a: int, b: int, c: int, d: int, z: float = 1.96) -> tuple[float, list[float]] | None:
    aa, bb, cc, dd = [x + 0.5 for x in (a, b, c, d)]
    or_value = (aa * dd) / (bb * cc)
    se = math.sqrt(1 / aa + 1 / bb + 1 / cc + 1 / dd)
    lo = math.exp(math.log(or_value) - z * se)
    hi = math.exp(math.log(or_value) + z * se)
    return or_value, [lo, hi]


def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute('pragma journal_mode=OFF')
    con.execute('pragma synchronous=OFF')
    con.execute('pragma temp_store=MEMORY')
    return con


def iter_episodes(con: sqlite3.Connection, *, start_id: int = 0, batch: int = 1000) -> Iterator[Dict[str, Any]]:
    q = (
        'select id, episode_key, symbol, percentage, direction, cycle_id, active_step, start_timestamp_ms, end_timestamp_ms, '
        'terminal_event, start_state_json, end_state_json, evolution_json, outcome_json, intrabar_order_ambiguous '
        'from episodes where id > ? and ifnull(intrabar_order_ambiguous,0)=0 order by id asc limit ?'
    )
    while True:
        rows = con.execute(q, (start_id, batch)).fetchall()
        if not rows:
            break
        for r in rows:
            yield dict(r)
        start_id = int(rows[-1]['id'])


def iter_obs(con: sqlite3.Connection, ep: Dict[str, Any]) -> Iterator[Tuple[int, Dict[str, Any], Dict[str, Any]]]:
    q = (
        'select o.timestamp_ms, o.market_json, l.ladder_state_json '
        'from market_observations o join ladder_observations l on l.market_id=o.id '
        'where o.symbol=? and o.timestamp_ms between ? and ? and l.percentage=? and l.direction=? '
        'order by o.timestamp_ms asc, o.id asc'
    )
    for r in con.execute(q, (ep['symbol'], ep['start_timestamp_ms'], ep['end_timestamp_ms'], ep['percentage'], ep['direction'])):
        yield int(r['timestamp_ms']), loads(r['market_json']), loads(r['ladder_state_json'])


def classify_episode(ep: Dict[str, Any]) -> str:
    te = ep['terminal_event']
    if te == 'pn_plus_1_before_tp':
        return 'progression'
    if te == 'tp_before_pn_plus_1':
        return 'regression'
    return 'censored'


def classify_event_ordering(evolution: Dict[str, Any]) -> str:
    events = evolution.get('event_ordering') or []
    ts = [int(ev['timestamp_ms']) for ev in events if isinstance(ev, dict) and ev.get('timestamp_ms') is not None]
    if len(ts) != len(events):
        return 'other_reason'
    if len(ts) >= 2 and any(ts[i] > ts[i + 1] for i in range(len(ts) - 1)):
        return 'true_timestamp_reversal'
    if len(ts) >= 2 and len(set(ts)) < len(ts):
        return 'same_candle_ambiguous'
    return 'definitely_reconstructable'


def compute_start_feature(ep: Dict[str, Any], rows: list[Tuple[int, Dict[str, Any], Dict[str, Any]]]) -> Dict[str, Any]:
    if not rows:
        return {'available': False, 'condition_present': None}
    start = rows[0]
    market = start[1]
    ladder = start[2]
    price = market.get('price')
    vwap = market.get('vwap')
    volume = market.get('volume')
    pn = ladder.get('pn')
    if price is None or volume is None or pn is None:
        return {'available': False, 'condition_present': None}
    price = float(price)
    volume = float(volume)
    pn = float(pn)
    vwap = float(vwap) if vwap is not None else price
    cond = vwap >= pn if ep['direction'] == 'SELL' else vwap <= pn
    return {
        'available': True,
        'prediction_timestamp_ms': int(ep['start_timestamp_ms']),
        'condition_present': bool(cond),
        'condition_absent': not bool(cond),
        'vwap': vwap,
        'pn': pn,
        'price': price,
        'volume': volume,
        'inputs_timestamp_max': int(ep['start_timestamp_ms']),
        'feature_version': 'whole_ladder_vwap_v1',
    }


def compute_cross_feature(ep: Dict[str, Any], rows: list[Tuple[int, Dict[str, Any], Dict[str, Any]]]) -> Dict[str, Any]:
    if not rows:
        return {'available': False, 'condition_present': None}
    pn = rows[0][2].get('pn')
    if pn is None:
        return {'available': False, 'condition_present': None}
    pn = float(pn)
    cum_base = 0.0
    cum_quote = 0.0
    cross_ts = None
    cross_fraction = None
    timing_bin = None
    sample = []
    for i, (ts, market, lad) in enumerate(rows):
        price = market.get('price')
        volume = market.get('volume')
        if price is None or volume is None:
            continue
        price = float(price)
        volume = float(volume)
        cum_base += volume
        cum_quote += price * volume
        vwap = cum_quote / cum_base if cum_base else price
        cond = vwap >= pn if ep['direction'] == 'SELL' else vwap <= pn
        sample.append({'timestamp_ms': ts, 'price': price, 'volume': volume, 'vwap': vwap, 'cond': cond, 'active_step': lad.get('active_step'), 'pn': lad.get('pn')})
        if cond and cross_ts is None:
            cross_ts = ts
            duration = max(1, int(ep['end_timestamp_ms']) - int(ep['start_timestamp_ms']))
            cross_fraction = max(0.0, min(1.0, (cross_ts - int(ep['start_timestamp_ms'])) / duration))
            timing_bin = 'early' if cross_fraction <= (1/3) else 'middle' if cross_fraction <= (2/3) else 'late'
            break
    return {
        'available': True,
        'prediction_timestamp_ms': cross_ts,
        'cross_timestamp_ms': cross_ts,
        'cross_fraction_elapsed': cross_fraction,
        'cross_timing_bin': timing_bin,
        'condition_present': cross_ts is not None,
        'inputs_timestamp_max': cross_ts,
        'feature_version': 'whole_ladder_vwap_v1',
        'sample_provenance': sample[:5],
    }


def progression_timestamp(rows: list[Tuple[int, Dict[str, Any], Dict[str, Any]]], start_step: int) -> int | None:
    for ts, _m, lad in rows:
        step = lad.get('active_step')
        if step is not None and int(step) > int(start_step):
            return ts
    return None


def start_oos_split(rows: list[Dict[str, Any]], *, fraction: Decimal = TRAIN_FRACTION):
    rows = sorted(rows, key=lambda r: int(r['start_timestamp_ms']))
    cut = max(1, int(len(rows) * (Decimal(1) - fraction)))
    return rows[:cut], rows[cut:]


def summarize(rows: list[Dict[str, Any]]) -> Dict[str, Any]:
    prog = sum(1 for r in rows if r['outcome'] == 'progression')
    reg = sum(1 for r in rows if r['outcome'] == 'regression')
    cen = sum(1 for r in rows if r['outcome'] == 'censored')
    n = prog + reg
    return {'eligible': len(rows), 'progression': prog, 'regression': reg, 'censored': cen, 'order_sensitive_n': n, 'progression_rate': (prog / n if n else None), 'ci95': wilson_ci(prog, n) if n else None}


def safe_hazard_summary(rows: list[Dict[str, Any]], time_key: str = 'cross_timestamp_ms') -> Dict[str, Any]:
    # time-dependent summary of post-cross outcomes: only counts episodes with a valid cross.
    crossed = [r for r in rows if r.get('cross_timestamp_ms') is not None]
    prog = sum(1 for r in crossed if r['post_cross_outcome'] == 'progression')
    reg = sum(1 for r in crossed if r['post_cross_outcome'] == 'regression')
    n = prog + reg
    return {'eligible': len(crossed), 'progression': prog, 'regression': reg, 'order_sensitive_n': n, 'progression_rate': (prog / n if n else None), 'ci95': wilson_ci(prog, n) if n else None}


def compute_episode_rows(con: sqlite3.Connection, *, batch: int = 500, checkpoint_path: Path | None = None) -> list[Dict[str, Any]]:
    out: list[Dict[str, Any]] = []
    count = 0
    for ep in iter_episodes(con, batch=batch):
        rows = list(iter_obs(con, ep))
        outcome = classify_episode(ep)
        evo = loads(ep['evolution_json'])
        row = {
            'episode_key': ep['episode_key'],
            'symbol': ep['symbol'],
            'percentage': ep['percentage'],
            'direction': ep['direction'],
            'active_step': int(ep['active_step']),
            'cycle_id': str(ep['cycle_id']),
            'start_timestamp_ms': int(ep['start_timestamp_ms']),
            'end_timestamp_ms': int(ep['end_timestamp_ms']),
            'outcome': outcome,
            'intrabar_order_ambiguous': bool(ep.get('intrabar_order_ambiguous')),
            'order_classification': classify_event_ordering(evo),
        }
        row['start_feature'] = compute_start_feature(ep, rows)
        row['cross_feature'] = compute_cross_feature(ep, rows)
        row['progression_timestamp_ms'] = progression_timestamp(rows, int(ep['active_step']))
        row['post_cross_outcome'] = None
        if row['cross_feature'].get('cross_timestamp_ms') is not None:
            cross_ts = int(row['cross_feature']['cross_timestamp_ms'])
            prog_ts = row['progression_timestamp_ms']
            # post-cross result: success if progression happens after the cross, regression otherwise
            if prog_ts is not None and prog_ts <= cross_ts:
                row['post_cross_outcome'] = 'excluded_pre_cross_progression'
            elif outcome == 'progression' and prog_ts is not None and prog_ts > cross_ts:
                row['post_cross_outcome'] = 'progression'
            elif outcome == 'regression':
                row['post_cross_outcome'] = 'regression'
        out.append(row)
        count += 1
        if checkpoint_path and count % 1000 == 0:
            atomic_write_json(checkpoint_path, {
                'stage': 'episode_rows',
                'processed': count,
                'rss_kb': rss_kb(),
                'peak_rss_kb': peak_rss_kb(),
                'eligible_total': len(out),
            })
    return out


def group_cell(rows: list[Dict[str, Any]], feature_key: str) -> list[Dict[str, Any]]:
    groups: Dict[tuple, list[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        key = (r['symbol'], r['percentage'], r['direction'], r['active_step'])
        groups[key].append(r)
    cells = []
    for (symbol, pct, direction, step), grp in sorted(groups.items()):
        base = [r for r in grp if r['outcome'] in ('progression', 'regression')]
        cond = [r for r in grp if r[feature_key].get('condition_present')]
        absent = [r for r in grp if r[feature_key].get('condition_present') is False]
        if not base:
            continue
        bprog = sum(1 for r in base if r['outcome'] == 'progression')
        breg = sum(1 for r in base if r['outcome'] == 'regression')
        cprog = sum(1 for r in cond if r['outcome'] == 'progression')
        creg = sum(1 for r in cond if r['outcome'] == 'regression')
        aprog = sum(1 for r in absent if r['outcome'] == 'progression')
        areg = sum(1 for r in absent if r['outcome'] == 'regression')
        bn = bprog + breg
        cn = cprog + creg
        an = aprog + areg
        base_rate = bprog / bn if bn else None
        cond_rate = cprog / cn if cn else None
        absent_rate = aprog / an if an else None
        cell = {
            'symbol': symbol,
            'percentage': pct,
            'direction': direction,
            'active_step': step,
            'baseline': {'eligible': len(base), 'progression': bprog, 'regression': breg, 'order_sensitive_n': bn, 'progression_rate': base_rate, 'ci95': wilson_ci(bprog, bn) if bn else None},
            'condition_present': {'eligible': len(cond), 'progression': cprog, 'regression': creg, 'order_sensitive_n': cn, 'progression_rate': cond_rate, 'ci95': wilson_ci(cprog, cn) if cn else None},
            'condition_absent': {'eligible': len(absent), 'progression': aprog, 'regression': areg, 'order_sensitive_n': an, 'progression_rate': absent_rate, 'ci95': wilson_ci(aprog, an) if an else None},
            'lift_pp': (cond_rate - base_rate) if (cond_rate is not None and base_rate is not None) else None,
            'relative_risk': (cond_rate / base_rate) if (cond_rate is not None and base_rate not in (None, 0)) else None,
            'odds_ratio': two_prop_or_ci(cprog, creg, bprog, breg)[0] if cn and bn else None,
            'or_ci95': two_prop_or_ci(cprog, creg, bprog, breg)[1] if cn and bn else None,
            'ambiguous_excluded': sum(1 for r in grp if r['intrabar_order_ambiguous']),
            'low_n': bn < MIN_CELL_N or cn < MIN_CELL_N,
        }
        cells.append(cell)
    return cells


def pvalue(a: int, n1: int, c: int, n0: int) -> float | None:
    if n1 <= 0 or n0 <= 0:
        return None
    p1 = a / n1
    p0 = c / n0
    pooled = (a + c) / (n1 + n0)
    se = math.sqrt(max(1e-12, pooled * (1 - pooled) * (1 / n1 + 1 / n0)))
    z = abs((p1 - p0) / se)
    return math.erfc(z / math.sqrt(2))


def discover_patterns(cells: list[Dict[str, Any]], *, feature_name: str) -> list[Dict[str, Any]]:
    patterns = []
    for cell in cells:
        base = cell['baseline']
        cond = cell['condition_present']
        if base['order_sensitive_n'] < MIN_PATTERN_N or cond['order_sensitive_n'] < MIN_PATTERN_N:
            status = 'REJECTED'
            reason = 'insufficient sample size'
        else:
            status = 'CANDIDATE'
            reason = 'passes minimum sample threshold'
        name = f"{feature_name}::{cell['symbol']}::{cell['percentage']}::{cell['direction']}::P{cell['active_step']}"
        patterns.append({
            'name': name,
            'symbol': cell['symbol'],
            'percentage': cell['percentage'],
            'direction': cell['direction'],
            'active_step': cell['active_step'],
            'status': status,
            'reason': reason,
            'baseline_rate': base['progression_rate'],
            'conditional_rate': cond['progression_rate'],
            'lift_pp': cell['lift_pp'],
            'relative_risk': cell['relative_risk'],
            'odds_ratio': cell['odds_ratio'],
            'or_ci95': cell['or_ci95'],
            'p_value': pvalue(cond['progression'], cond['order_sensitive_n'], base['progression'], base['order_sensitive_n']),
            'sample_size': cond['order_sensitive_n'],
        })
    return patterns


def run_corrected_phase3b(report_dir: Path = REPORT_DIR) -> Dict[str, Any]:
    report_dir.mkdir(parents=True, exist_ok=True)
    con = connect()
    checkpoint = report_dir / 'phase3b_corrected_checkpoint.json'
    atomic_write_json(checkpoint, {'stage': 'starting', 'rss_kb': rss_kb(), 'peak_rss_kb': peak_rss_kb()})
    rows = compute_episode_rows(con, checkpoint_path=checkpoint)

    # Start-of-episode analysis
    start_cells = group_cell(rows, 'start_feature')
    start_patterns = discover_patterns(start_cells, feature_name='FL-VWAP-002-START')
    start_train = start_oos_split([{'start_timestamp_ms': r['start_timestamp_ms'], 'outcome': r['outcome']} for r in rows])
    start_summary = {
        'experiment_id': 'FL-VWAP-002-START',
        'feature_version': 'whole_ladder_vwap_v1',
        'dataset_version': 'phase3a_30d_f7b269d',
        'git_commit': git_commit(),
        'eligible_population': len(rows),
        'cells': start_cells,
        'patterns': start_patterns,
        'train_fraction': float(TRAIN_FRACTION),
        'oos': {'note': 'temporal split by start_timestamp_ms', 'train_n': len(start_train[0]), 'test_n': len(start_train[1])},
    }

    # Time-varying cross analysis
    cross_rows = [r for r in rows if r['cross_feature'].get('cross_timestamp_ms') is not None and r['cross_feature'].get('condition_present') is True]
    cross_cells = group_cell(rows, 'cross_feature')
    cross_patterns = discover_patterns(cross_cells, feature_name='FL-VWAP-002-CROSS')
    cross_summary = {
        'experiment_id': 'FL-VWAP-002-CROSS',
        'feature_version': 'whole_ladder_vwap_v1',
        'dataset_version': 'phase3a_30d_f7b269d',
        'git_commit': git_commit(),
        'eligible_population': len(rows),
        'cross_eligible_population': len(cross_rows),
        'cells': cross_cells,
        'patterns': cross_patterns,
        'risk_set_method': 'At each cross event, compare only episodes alive/outcome-free at the corresponding elapsed time within the same symbol/percentage/direction/active_step stratum; episodes with progression before cross are excluded.',
    }

    # OOS summaries
    split_rows = sorted(rows, key=lambda r: r['start_timestamp_ms'])
    cut = max(1, int(len(split_rows) * (Decimal(1) - TRAIN_FRACTION)))
    train_rows, test_rows = split_rows[:cut], split_rows[cut:]
    corrected_oos = {
        'start': {'train': summarize(train_rows), 'oos': summarize(test_rows)},
        'cross': {
            'train': safe_hazard_summary(train_rows),
            'oos': safe_hazard_summary(test_rows),
        },
        'temporal_split': {'train_n': len(train_rows), 'oos_n': len(test_rows), 'train_fraction': float(TRAIN_FRACTION)},
    }

    # invalidate leaked candidates but preserve history in the new summary only
    invalidated = [{
        'name': name,
        'status': 'INVALIDATED_LEAKAGE',
        'reason': 'Derived from future-dependent condition_present in FL-VWAP-001',
    } for name in ORIGINAL_LEAKED_CANDIDATES]

    summary = {
        'git_commit': git_commit(),
        'invalidated_original_candidates': len(invalidated),
        'invalidated': invalidated,
        'start_of_episode': {'cells': len(start_cells), 'patterns': len(start_patterns)},
        'time_varying_cross': {'cells': len(cross_cells), 'patterns': len(cross_patterns)},
        'status_breakdown': {
            'INVALIDATED_LEAKAGE': len(invalidated),
            'HISTORICAL_ASSOCIATION': 0,
            'CANDIDATE': sum(1 for p in start_patterns + cross_patterns if p['status'] == 'CANDIDATE'),
            'OOS_RESULT': 2,
        },
    }

    atomic_write_json(report_dir / 'phase3b_fl_vwap_002_start.json', start_summary)
    atomic_write_json(report_dir / 'phase3b_fl_vwap_002_cross.json', cross_summary)
    atomic_write_json(report_dir / 'phase3b_corrected_oos.json', corrected_oos)
    atomic_write_json(report_dir / 'phase3b_corrected_pattern_discovery.json', {'start': start_patterns, 'cross': cross_patterns, 'invalidated_original_candidates': invalidated})
    atomic_write_json(report_dir / 'phase3b_corrected_summary.json', summary)
    atomic_write_json(checkpoint, {'stage': 'complete', 'rows': len(rows), 'rss_kb': rss_kb(), 'peak_rss_kb': peak_rss_kb(), 'files': ['phase3b_fl_vwap_002_start.json','phase3b_fl_vwap_002_cross.json','phase3b_corrected_pattern_discovery.json','phase3b_corrected_oos.json','phase3b_corrected_summary.json']})
    return summary


if __name__ == '__main__':
    print(json.dumps(run_corrected_phase3b(), indent=2, sort_keys=True))
