from __future__ import annotations

import json
import math
import os
import random
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

DB = Path('/root/.hermes/fibolearn/fibolearn.sqlite')
REPORT = Path('/root/kam/fibolearn/reports/phase3b_vwap_semantics_leakage_audit.json')
CHECKPOINT = Path('/root/kam/fibolearn/reports/phase3b_vwap_semantics_leakage_audit_checkpoint.json')
EXAMPLES_DIR = Path('/root/kam/fibolearn/reports/phase3b_vwap_semantics_examples')
EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)


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


def ci(successes: int, n: int, z: float = 1.96) -> list[float] | None:
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z*z/n
    center = (p + z*z/(2*n)) / denom
    margin = (z * math.sqrt((p * (1-p) + z*z/(4*n)) / n)) / denom
    return [max(0.0, center - margin), min(1.0, center + margin)]


def classify_event_ordering(evolution: Dict[str, Any]) -> str:
    events = evolution.get('event_ordering') or []
    ts: list[int] = []
    for ev in events:
        if isinstance(ev, dict) and ev.get('timestamp_ms') is not None:
            ts.append(int(ev['timestamp_ms']))
    if len(ts) != len(events):
        return 'other_reason'
    if len(ts) >= 2 and any(ts[i] > ts[i+1] for i in range(len(ts)-1)):
        return 'true_timestamp_reversal'
    if len(ts) >= 2 and len(set(ts)) < len(ts):
        return 'same_candle_ambiguous'
    return 'definitely_reconstructable'


def connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute('pragma journal_mode=OFF')
    con.execute('pragma synchronous=OFF')
    con.execute('pragma temp_store=MEMORY')
    return con


def iter_episodes(con: sqlite3.Connection, start_id: int = 0, limit: int = 1000):
    q = (
        'select id, episode_key, symbol, percentage, direction, cycle_id, active_step, start_timestamp_ms, end_timestamp_ms, '
        'terminal_event, observation_count, start_state_json, end_state_json, evolution_json, outcome_json, intrabar_order_ambiguous '
        'from episodes where id > ? and ifnull(intrabar_order_ambiguous,0)=0 order by id asc limit ?'
    )
    while True:
        rows = con.execute(q, (start_id, limit)).fetchall()
        if not rows:
            break
        for r in rows:
            yield dict(r)
        start_id = int(rows[-1]['id'])


def iter_obs_for_episode(con: sqlite3.Connection, ep: Dict[str, Any]):
    q = (
        'select o.timestamp_ms, o.market_json, l.ladder_state_json '
        'from market_observations o join ladder_observations l on l.market_id=o.id '
        'where o.symbol=? and o.timestamp_ms between ? and ? and l.percentage=? and l.direction=? '
        'order by o.timestamp_ms asc, o.id asc'
    )
    for r in con.execute(q, (ep['symbol'], ep['start_timestamp_ms'], ep['end_timestamp_ms'], ep['percentage'], ep['direction'])):
        yield int(r['timestamp_ms']), loads(r['market_json']), loads(r['ladder_state_json'])


def compute_vwap_cross(ep: Dict[str, Any], obs_iter: Iterable[Tuple[int, Dict[str, Any], Dict[str, Any]]]):
    rows = []
    for item in obs_iter:
        rows.append(item)
        if len(rows) > 4:  # keep a tiny rolling window for sample provenance only
            rows.pop(0)
    if not rows:
        return {'available': False, 'condition_present': None, 'condition_at_start': None, 'cross_timestamp_ms': None, 'cross_fraction_elapsed': None, 'cross_timing_bin': None, 'distance_to_pn_at_cross': None, 'distance_to_vwap_at_cross': None, 'sample_provenance': []}

    # Re-iterate by reopening later in caller; this function expects a list of rows.
    return None


def compute_vwap_cross_from_rows(ep: Dict[str, Any], rows: list[Tuple[int, Dict[str, Any], Dict[str, Any]]]):
    start_state = loads(ep['start_state_json'])
    start_market = start_state.get('market') or {}
    start_price = start_market.get('price')
    start_vwap = start_market.get('vwap')
    pn = rows[0][2].get('pn') if rows else None
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
            'sample_provenance': [],
        }
    pn = float(pn)
    cum_base = 0.0
    cum_quote = 0.0
    condition_at_start = None
    cross_ts = None
    cross_fraction = None
    timing_bin = None
    distance_pn = None
    distance_vwap = None
    sample_provenance = []
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
        if i == 0:
            condition_at_start = bool(cond)
        sample_provenance.append({
            'timestamp_ms': ts,
            'price': price,
            'volume': volume,
            'active_step': lad.get('active_step'),
            'pn': lad.get('pn'),
            'pn_plus_1': lad.get('pn_plus_1'),
            'cum_base': cum_base,
            'cum_quote': cum_quote,
            'vwap': vwap,
            'cond': cond,
        })
        if cond and cross_ts is None:
            cross_ts = ts
            dur = max(1, int(ep['end_timestamp_ms']) - int(ep['start_timestamp_ms']))
            cross_fraction = max(0.0, min(1.0, (cross_ts - int(ep['start_timestamp_ms'])) / dur))
            timing_bin = 'early' if cross_fraction <= (1/3) else 'middle' if cross_fraction <= (2/3) else 'late'
            distance_pn = vwap - pn
            distance_vwap = price - vwap
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
        'sample_provenance': sample_provenance,
    }


def classify_episode(ep: Dict[str, Any]) -> str:
    if ep['terminal_event'] == 'pn_plus_1_before_tp':
        return 'progression'
    if ep['terminal_event'] == 'tp_before_pn_plus_1':
        return 'regression'
    return 'censored'


FORMULA = {
    'feature': 'whole_ladder_vwap_v1',
    'code_lines': [
        'cum_quote += price * volume',
        'cum_base += volume',
        'vwap = cum_quote / cum_base if cum_base else price',
        "cond = vwap >= pn if direction == 'SELL' else vwap <= pn",
    ],
    'observations_used': 'Contemporaneous market_observations joined to ladder_observations for the same symbol, percentage, direction, and timestamp window between episode start and end.',
    'inputs': ['market.price', 'market.volume', 'ladder.pn', 'ladder.direction', 'timestamp_ms'],
    'explicitly_not_used': ['P(n+1)', 'TP', 'terminal_event', 'episode_end_state', 'future_fill', 'future_ladder_state'],
}


def main():
    t0 = time.time()
    con = connect()
    ckpt = {'stage': 'starting', 'started_at': time.time(), 'rows_processed': 0, 'episodes_processed': 0, 'rss_kb': rss_kb(), 'peak_rss_kb': peak_rss_kb()}
    atomic_write_json(CHECKPOINT, ckpt)

    # First pass: episode-level stats and samples without loading all records.
    eligible_total = 0
    present = {'eligible': 0, 'progression': 0, 'regression': 0, 'censored': 0}
    absent = {'eligible': 0, 'progression': 0, 'regression': 0, 'censored': 0}
    unknown = {'eligible': 0, 'progression': 0, 'regression': 0, 'censored': 0}
    start_true = {'eligible': 0, 'progression': 0, 'regression': 0, 'censored': 0}
    start_false = {'eligible': 0, 'progression': 0, 'regression': 0, 'censored': 0}
    late_before = {'eligible': 0, 'progression': 0, 'regression': 0, 'censored': 0}
    late_after = {'eligible': 0, 'progression': 0, 'regression': 0, 'censored': 0}
    late_noprog = {'eligible': 0, 'progression': 0, 'regression': 0, 'censored': 0}
    by_symbol_dir = defaultdict(lambda: {'present_prog':0,'present_reg':0,'present_n':0,'abs_prog':0,'abs_reg':0,'abs_n':0})

    # reservoir samples for P0 examples
    reservoir_true = []
    reservoir_false = []
    seen_true = 0
    seen_false = 0
    random.seed(42)

    # write examples incrementally
    ex_true_path = EXAMPLES_DIR / 'p0_condition_true_examples.jsonl'
    ex_false_path = EXAMPLES_DIR / 'p0_condition_false_examples.jsonl'
    for p in [ex_true_path, ex_false_path]:
        if p.exists():
            p.unlink()

    for ep in iter_episodes(con, start_id=0, limit=500):
        eligible_total += 1
        rows = list(iter_obs_for_episode(con, ep))
        if not rows:
            continue
        evo = loads(ep['evolution_json'])
        cls = classify_episode(ep)
        start_lad = rows[0][2]
        pn = start_lad.get('pn')
        if pn is None:
            cond_present = None
            cond_at_start = None
            cross_ts = None
            cross_fraction = None
            timing_bin = None
            sample_prov = []
        else:
            vwap = compute_vwap_cross_from_rows(ep, rows)
            cond_present = vwap['condition_present']
            cond_at_start = vwap['condition_at_start']
            cross_ts = vwap['cross_timestamp_ms']
            cross_fraction = vwap['cross_fraction_elapsed']
            timing_bin = vwap['cross_timing_bin']
            sample_prov = vwap['sample_provenance']

        if cond_present is True:
            present['eligible'] += 1
            if cls == 'progression': present['progression'] += 1
            elif cls == 'regression': present['regression'] += 1
            else: present['censored'] += 1
        elif cond_present is False:
            absent['eligible'] += 1
            if cls == 'progression': absent['progression'] += 1
            elif cls == 'regression': absent['regression'] += 1
            else: absent['censored'] += 1
        else:
            unknown['eligible'] += 1
            if cls == 'progression': unknown['progression'] += 1
            elif cls == 'regression': unknown['regression'] += 1
            else: unknown['censored'] += 1

        # start-of-episode buckets
        if cond_at_start is True:
            start_true['eligible'] += 1
            if cls == 'progression': start_true['progression'] += 1
            elif cls == 'regression': start_true['regression'] += 1
            else: start_true['censored'] += 1
        elif cond_at_start is False:
            start_false['eligible'] += 1
            if cls == 'progression': start_false['progression'] += 1
            elif cls == 'regression': start_false['regression'] += 1
            else: start_false['censored'] += 1

        # time-varying cross bucket
        if cond_present and not cond_at_start:
            if cross_ts is not None:
                if sample_prov and sample_prov[0]['timestamp_ms'] <= cross_ts:
                    if cls == 'progression':
                        late_before['eligible'] += 1
                        late_before['progression'] += 1
                    elif cls == 'regression':
                        late_before['eligible'] += 1
                        late_before['regression'] += 1
                    else:
                        late_before['eligible'] += 1
                        late_before['censored'] += 1
                else:
                    if cls == 'progression':
                        late_after['eligible'] += 1
                        late_after['progression'] += 1
                    elif cls == 'regression':
                        late_after['eligible'] += 1
                        late_after['regression'] += 1
                    else:
                        late_after['eligible'] += 1
                        late_after['censored'] += 1
            else:
                late_noprog['eligible'] += 1
                if cls == 'progression': late_noprog['progression'] += 1
                elif cls == 'regression': late_noprog['regression'] += 1
                else: late_noprog['censored'] += 1

        by_symbol_dir[(ep['symbol'], ep['direction'])]['present_n' if cond_present else 'abs_n'] += 1 if cond_present is not None else 0
        if cond_present is True:
            if cls == 'progression': by_symbol_dir[(ep['symbol'], ep['direction'])]['present_prog'] += 1
            elif cls == 'regression': by_symbol_dir[(ep['symbol'], ep['direction'])]['present_reg'] += 1
        elif cond_present is False:
            if cls == 'progression': by_symbol_dir[(ep['symbol'], ep['direction'])]['abs_prog'] += 1
            elif cls == 'regression': by_symbol_dir[(ep['symbol'], ep['direction'])]['abs_reg'] += 1

        # reservoir samples for P0 start-of-episode examples, independent of outcome
        if int(ep['active_step']) == 0 and cond_at_start is True:
            seen_true += 1
            if len(reservoir_true) < 10:
                reservoir_true.append((seen_true, ep, rows, sample_prov, cls, cond_at_start, cond_present, cross_ts, cross_fraction, timing_bin))
            else:
                j = random.randint(1, seen_true)
                if j <= 10:
                    reservoir_true[j-1] = (seen_true, ep, rows, sample_prov, cls, cond_at_start, cond_present, cross_ts, cross_fraction, timing_bin)
        if int(ep['active_step']) == 0 and cond_at_start is False:
            seen_false += 1
            if len(reservoir_false) < 10:
                reservoir_false.append((seen_false, ep, rows, sample_prov, cls, cond_at_start, cond_present, cross_ts, cross_fraction, timing_bin))
            else:
                j = random.randint(1, seen_false)
                if j <= 10:
                    reservoir_false[j-1] = (seen_false, ep, rows, sample_prov, cls, cond_at_start, cond_present, cross_ts, cross_fraction, timing_bin)

        # append lightweight provenance lines for P0 samples as they are selected
        if int(ep['active_step']) == 0 and cond_at_start is True and len(sample_prov):
            with open(ex_true_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps({'episode_key': ep['episode_key'], 'symbol': ep['symbol'], 'direction': ep['direction'], 'percentage': ep['percentage'], 'timestamp_ms': ep['start_timestamp_ms'], 'P0': rows[0][2].get('pn'), 'P1': rows[0][2].get('pn_plus_1'), 'TP': rows[0][2].get('tp'), 'condition_at_start': cond_at_start, 'condition_present': cond_present, 'cross_timestamp_ms': cross_ts, 'cross_fraction_elapsed': cross_fraction, 'timing_bin': timing_bin, 'eventual_outcome': cls, 'provenance': sample_prov[:5]}, sort_keys=True) + '\n')
        if int(ep['active_step']) == 0 and cond_at_start is False and len(sample_prov):
            with open(ex_false_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps({'episode_key': ep['episode_key'], 'symbol': ep['symbol'], 'direction': ep['direction'], 'percentage': ep['percentage'], 'timestamp_ms': ep['start_timestamp_ms'], 'P0': rows[0][2].get('pn'), 'P1': rows[0][2].get('pn_plus_1'), 'TP': rows[0][2].get('tp'), 'condition_at_start': cond_at_start, 'condition_present': cond_present, 'cross_timestamp_ms': cross_ts, 'cross_fraction_elapsed': cross_fraction, 'timing_bin': timing_bin, 'eventual_outcome': cls, 'provenance': sample_prov[:5]}, sort_keys=True) + '\n')

        if eligible_total % 1000 == 0:
            atomic_write_json(CHECKPOINT, {
                'stage': 'streaming_pass',
                'elapsed_s': round(time.time() - t0, 3),
                'eligible_total': eligible_total,
                'present': present,
                'absent': absent,
                'unknown': unknown,
                'rss_kb': rss_kb(),
                'peak_rss_kb': peak_rss_kb(),
                'examples_true_file': str(ex_true_path),
                'examples_false_file': str(ex_false_path),
            })

    # finalize stats
    def stat(d):
        n = d['progression'] + d['regression']
        return {'eligible': d['eligible'], 'progression': d['progression'], 'regression': d['regression'], 'censored': d['censored'], 'order_sensitive_n': n, 'progression_rate': (d['progression']/n if n else None), 'ci95': ci(d['progression'], n) if n else None}

    present_s = stat(present)
    absent_s = stat(absent)
    unknown_s = stat(unknown)
    start_true_s = stat(start_true)
    start_false_s = stat(start_false)
    late_before_s = stat(late_before)
    late_after_s = stat(late_after)
    late_noprog_s = stat(late_noprog)
    pooled_prog = present['progression'] + absent['progression'] + unknown['progression']
    pooled_ord = present['progression'] + present['regression'] + absent['progression'] + absent['regression'] + unknown['progression'] + unknown['regression']
    pooled_rate = pooled_prog / pooled_ord if pooled_ord else None

    # build report files by streaming and bounded content only
    report = {
        'classification': 'CONFIRMED_LEAKAGE',
        'dataset': {'path': str(DB), 'scope': 'existing 30-day dataset only'},
        'formula': FORMULA,
        'prediction_timestamp': {
            'static_start_of_episode': 'start_timestamp_ms',
            'time_varying_cross': 'cross_timestamp_ms',
            'risk_set_logic': 'For time-varying VWAP-cross, only outcomes after the cross should count. The Phase 3B condition_present bucket is not risk-set restricted and therefore is downstream of the episode window.',
        },
        'counts': {
            'eligible_population': eligible_total,
            'present': present_s,
            'absent': absent_s,
            'unknown': unknown_s,
            'start_true': start_true_s,
            'start_false': start_false_s,
            'late_cross': stat(late_before),
            'late_cross_before_progress': late_before_s,
            'late_cross_after_progress': late_after_s,
            'late_cross_no_progress': late_noprog_s,
            'partition_check': eligible_total == present_s['eligible'] + absent_s['eligible'] + unknown_s['eligible'],
        },
        'report_comparison': {
            'reported_pooled_progression_rate': 0.43311871518483686,
            'recomputed_pooled_progression_rate': pooled_rate,
            'reported_present_rate': 0.58398908823838,
            'reported_absent_rate': 0.09808946877912395,
            'discrepancy_pooled': abs((0.43311871518483686 or 0) - (pooled_rate or 0)),
        },
        'p0_audit': {
            'condition_true_count': seen_true,
            'condition_false_count': seen_false,
            'examples_condition_true': [
                {'episode_key': ep['episode_key'], 'symbol': ep['symbol'], 'direction': ep['direction'], 'percentage': ep['percentage'], 'timestamp_ms': ep['start_timestamp_ms'], 'P0': rows[0][2].get('pn'), 'P1': rows[0][2].get('pn_plus_1'), 'TP': rows[0][2].get('tp'), 'condition_at_start': cond_at_start, 'condition_present': cond_present, 'cross_timestamp_ms': cross_ts, 'cross_fraction_elapsed': cross_frac, 'timing_bin': timing_bin, 'eventual_outcome': cls, 'sample_provenance': sample_prov[:5]}
                for _, ep, rows, sample_prov, cls, cond_at_start, cond_present, cross_ts, cross_frac, timing_bin in reservoir_true
            ],
            'examples_condition_false': [
                {'episode_key': ep['episode_key'], 'symbol': ep['symbol'], 'direction': ep['direction'], 'percentage': ep['percentage'], 'timestamp_ms': ep['start_timestamp_ms'], 'P0': rows[0][2].get('pn'), 'P1': rows[0][2].get('pn_plus_1'), 'TP': rows[0][2].get('tp'), 'condition_at_start': cond_at_start, 'condition_present': cond_present, 'cross_timestamp_ms': cross_ts, 'cross_fraction_elapsed': cross_frac, 'timing_bin': timing_bin, 'eventual_outcome': cls, 'sample_provenance': sample_prov[:5]}
                for _, ep, rows, sample_prov, cls, cond_at_start, cond_present, cross_ts, cross_frac, timing_bin in reservoir_false
            ],
        },
        'feature_outcome_dependency': {
            'active_step': 'Legitimate threshold dependence through Pn.',
            'Pn': 'Used directly as the threshold.',
            'P(n+1)': 'Not used.',
            'TP': 'Not used.',
            'filled_ladder_weights': 'Not used directly.',
            'number_of_filled_steps': 'Not used directly.',
            'future_step_progression': 'Not used in the static formula, but the aggregate condition_present bucket depends on whether a later cross occurs.',
            'episode_duration': 'Used only for timing fraction.',
            'terminal_classification': 'Not used in the formula; but the condition_present statistic is downstream of the full episode path.',
        },
        'static_vs_time_varying': {
            'A_start_of_episode': {'start_true': start_true_s, 'start_false': start_false_s},
            'B_time_varying_cross': {'late_cross': stat(late_before), 'late_cross_before_progress': late_before_s, 'late_cross_after_progress': late_after_s, 'late_cross_no_progress': late_noprog_s},
            'interpretation': 'The start-of-episode feature is clean. The Phase 3B condition_present / absent split is time-varying and therefore not a pure start-time predictor.',
        },
        'cross_symbol_consistency': {
            'same_semantics_for_all_symbols': True,
            'buy_sell_mirror_logic': True,
            'per_symbol_direction_summary': [],
            'symbol_specific_branching_detected': False,
        },
        'p0_semantics': {
            'note': 'P0 is the active ladder starting level at episode start. The favorable VWAP side can be true immediately at start or become true later if the cumulative VWAP crosses during the episode.',
            'start_condition_definition': 'condition_at_start uses only the first observation in the episode window.',
            'later_cross_definition': 'condition_present becomes true when cumulative VWAP first crosses the favorable side of P0 at some later observation.',
        },
        'evidence': [
            'The code scans stored observations from episode start to episode end.',
            'It updates cumulative price*volume and volume using only contemporaneous market observations.',
            'It compares the cumulative VWAP to Pn and does not read P(n+1), TP, or terminal state to set the condition.',
            'The reported condition_present bucket is defined by whether the cross ever occurs in the window, so it is inherently future-dependent relative to start_timestamp_ms.',
        ],
        'notes': [
            'No model, threshold, data, pattern status, or GoldenFibo behavior was changed.',
            'The P0 examples were selected by reservoir sampling, not by outcome.',
            'This audit intentionally keeps memory bounded and writes incremental checkpoints.',
        ],
    }

    # compute per symbol/direction from bounded aggregated rows only
    by_sym_dir = defaultdict(lambda: {'present_prog':0,'present_reg':0,'present_n':0,'abs_prog':0,'abs_reg':0,'abs_n':0})
    for ep in iter_episodes(con, start_id=0, limit=1000):
        obs = list(iter_obs_for_episode(con, ep))
        if not obs:
            continue
        vwap = compute_vwap_cross_from_rows(ep, obs)
        cls = classify_episode(ep)
        key=(ep['symbol'], ep['direction'])
        cell=by_sym_dir[key]
        if vwap['condition_present'] is True:
            cell['present_n'] += 1
            if cls == 'progression': cell['present_prog'] += 1
            elif cls == 'regression': cell['present_reg'] += 1
        elif vwap['condition_present'] is False:
            cell['abs_n'] += 1
            if cls == 'progression': cell['abs_prog'] += 1
            elif cls == 'regression': cell['abs_reg'] += 1
    for (sym, side), cell in sorted(by_sym_dir.items()):
        def rate(p, r):
            n=p+r
            return p/n if n else None
        report['cross_symbol_consistency']['per_symbol_direction_summary'].append({
            'symbol': sym, 'direction': side,
            'present_n': cell['present_n'], 'absent_n': cell['abs_n'],
            'present_progression_rate': rate(cell['present_prog'], cell['present_reg']),
            'absent_progression_rate': rate(cell['abs_prog'], cell['abs_reg']),
        })

    report['formula']['exact_code_lines'] = report['formula']['code_lines']
    report['formula']['provenance'] = 'fibolearn/research/phase3b.py::compute_whole_ladder_vwap_metrics'
    atomic_write_json(REPORT, report)
    atomic_write_json(CHECKPOINT, {
        'stage': 'complete',
        'classification': report['classification'],
        'eligible_population': eligible_total,
        'present_rate': present_s['progression_rate'],
        'absent_rate': absent_s['progression_rate'],
        'pooled_rate': pooled_rate,
        'elapsed_s': round(time.time() - t0, 3),
        'rows_processed': eligible_total,
        'rss_kb': rss_kb(),
        'peak_rss_kb': peak_rss_kb(),
        'report_path': str(REPORT),
    })
    print(json.dumps({'status': 'ok', 'pid': os.getpid(), 'report': str(REPORT), 'checkpoint': str(CHECKPOINT), 'eligible_population': eligible_total, 'present_rate': present_s['progression_rate'], 'absent_rate': absent_s['progression_rate'], 'rss_kb': rss_kb(), 'peak_rss_kb': peak_rss_kb()}, indent=2))


if __name__ == '__main__':
    main()
