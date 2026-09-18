from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

DB_PATH = Path('/root/.hermes/fibolearn/fibolearn.sqlite')
REPORT_DIR = Path('/root/kam/fibolearn/reports')
CHECKPOINT = REPORT_DIR / 'phase3b_fl_vwap_002_cross_riskset_checkpoint.json'
K_MATCH = 5
TRAIN_FRACTION = 0.7
MIN_N = 30


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
        'terminal_event, evolution_json, intrabar_order_ambiguous '
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


def classify_event_ordering(evolution: Dict[str, Any]) -> str:
    events = evolution.get('event_ordering') or []
    ts = [int(ev['timestamp_ms']) for ev in events if isinstance(ev, dict) and ev.get('timestamp_ms') is not None]
    if len(ts) != len(events):
        return 'other_excluded'
    if len(ts) >= 2 and (any(ts[i] > ts[i + 1] for i in range(len(ts) - 1)) or len(set(ts)) < len(ts)):
        return 'same_candle_ambiguous'
    return 'definitely_reconstructable'


def canonicalize_episode_cross(row: Dict[str, Any]) -> Dict[str, Any]:
    cf = row.get('cross_feature') or {}
    start = int(row.get('start_timestamp_ms') or row.get('episode_start_timestamp_ms') or 0)
    cross_ts = cf.get('cross_timestamp_ms')
    if cross_ts is not None:
        cross_ts = int(cross_ts)
    elapsed = None if cross_ts is None else max(0, cross_ts - start)
    return {
        'episode_key': row.get('episode_key'),
        'symbol': row.get('symbol'),
        'percentage': row.get('percentage'),
        'direction': row.get('direction'),
        'active_step': row.get('active_step'),
        'episode_start_timestamp_ms': start,
        'cross_timestamp_ms': cross_ts,
        'cross_elapsed_ms': elapsed,
        'pn_plus_1_timestamp_ms': row.get('pn_plus_1_timestamp_ms'),
        'tp_or_terminal_timestamp_ms': row.get('tp_or_terminal_timestamp_ms'),
        'post_cross_outcome': row.get('post_cross_outcome'),
        'intrabar_order_ambiguous': bool(row.get('intrabar_order_ambiguous')),
        'valid_cross': cross_ts is not None and cross_ts > start,
    }


def classify_cross_eligibility(row: Dict[str, Any]) -> Dict[str, Any]:
    c = canonicalize_episode_cross(row)
    if c['intrabar_order_ambiguous']:
        return {'status': 'same_candle_ambiguous', **c}
    if c['cross_timestamp_ms'] is None:
        return {'status': 'missing_timestamp', **c}
    if c['cross_timestamp_ms'] <= c['episode_start_timestamp_ms']:
        return {'status': 'cross_before_start', **c}
    outcome_ts = c['tp_or_terminal_timestamp_ms']
    if outcome_ts is None:
        return {'status': 'missing_timestamp', **c}
    if outcome_ts <= c['episode_start_timestamp_ms']:
        return {'status': 'cross_after_terminal', **c}
    if outcome_ts < c['cross_timestamp_ms']:
        return {'status': 'outcome_before_cross', **c}
    if outcome_ts == c['cross_timestamp_ms']:
        return {'status': 'same_candle_ambiguous', **c}
    return {'status': 'valid', **c}


def reconstruct_state_at_landmark(episode: Dict[str, Any], landmark_timestamp_ms: int) -> Dict[str, Any]:
    """Pure landmark reconstructor using primitive episode history only."""
    start = episode.get('episode_start_timestamp_ms')
    start_ts = int(start) if start is not None else None
    end_raw = episode.get('tp_or_terminal_timestamp_ms')
    end_ts = int(end_raw) if end_raw is not None else None
    episode_started_state = True if start_ts is not None and start_ts <= landmark_timestamp_ms else False if start_ts is not None else None
    coverage_state = None if start_ts is None or end_ts is None else bool(start_ts <= landmark_timestamp_ms <= end_ts)

    evo = episode.get('evolution_json') if isinstance(episode.get('evolution_json'), dict) else {}
    out = episode.get('outcome_json') if isinstance(episode.get('outcome_json'), dict) else {}
    evs = list((evo or {}).get('event_ordering') or [])
    if not evs and isinstance(out, dict):
        evs = list(out.get('event_ordering') or [])

    by_ts = defaultdict(list)
    for ev in evs:
        if isinstance(ev, dict) and ev.get('timestamp_ms') is not None:
            by_ts[int(ev['timestamp_ms'])].append(ev.get('event'))

    ambiguous = any(len(names) > 1 for names in by_ts.values())
    first_pn = None
    first_tp = None
    first_vwap = None
    for ts in sorted(by_ts):
        names = by_ts[ts]
        if first_pn is None and any(n in ('reached_pn_plus_1', 'progression') for n in names):
            first_pn = ts
        if first_tp is None and any(n in ('tp_cycle_closed', 'tp_before_pn_plus_1', 'tp_hit', 'cycle_closed') for n in names):
            first_tp = ts
        if first_vwap is None and any(n in ('vwap_crossed_pn', 'vwap_crossed_pn_since_start') for n in names):
            first_vwap = ts

    # Direct primitive timestamps are allowed when present on the row.
    if first_vwap is None and episode.get('cross_timestamp_ms') is not None:
        first_vwap = int(episode['cross_timestamp_ms'])
    if first_vwap is None and episode.get('time_to_vwap_cross_ms') is not None and start_ts is not None:
        first_vwap = start_ts + int(episode['time_to_vwap_cross_ms'])
    if first_pn is None and episode.get('pn_plus_1_timestamp_ms') is not None:
        first_pn = int(episode['pn_plus_1_timestamp_ms'])
    if first_tp is None and episode.get('tp_or_terminal_timestamp_ms') is not None:
        first_tp = int(episode['tp_or_terminal_timestamp_ms'])

    if coverage_state is True:
        alive = False if first_tp is not None and first_tp <= landmark_timestamp_ms else True
    elif coverage_state is False:
        alive = None
    else:
        alive = None

    def tri_state(first_ts: int | None) -> bool | None:
        if coverage_state is None:
            return None
        if first_ts is None:
            return False
        return True if first_ts <= landmark_timestamp_ms else False

    pn_state = tri_state(first_pn)
    tp_state = tri_state(first_tp)
    vwap_state = tri_state(first_vwap)

    provenance = {
        'events_considered': sorted(by_ts.items()),
        'used_time_to_vwap_cross_ms': episode.get('time_to_vwap_cross_ms') is not None and start_ts is not None and first_vwap == (start_ts + int(episode['time_to_vwap_cross_ms'])),
    }
    return {
        'episode_started': episode_started_state,
        'coverage_through_landmark': coverage_state,
        'alive_at_landmark': alive,
        'pn_plus_1_reached_by_landmark': pn_state,
        'tp_reached_by_landmark': tp_state,
        'favorable_vwap_crossed_by_landmark': vwap_state,
        'first_pn_plus_1_timestamp_ms': first_pn,
        'first_tp_timestamp_ms': first_tp,
        'first_vwap_cross_timestamp_ms': first_vwap,
        'same_candle_ambiguity_at_landmark': ambiguous,
        'landmark_timestamp_ms': landmark_timestamp_ms,
        'provenance': provenance,
    }


def eligibility_from_state_at_T(state: Dict[str, Any]) -> Dict[str, Any]:
    violations = []
    if state.get('episode_started') is not True:
        violations.append('start_gt_landmark')
    if state.get('coverage_through_landmark') is not True:
        violations.append('coverage_through_landmark')
    if state.get('alive_at_landmark') is not True:
        violations.append('alive_at_landmark')
    if state.get('pn_plus_1_reached_by_landmark') is not False:
        violations.append('outcome_le_landmark' if state.get('pn_plus_1_reached_by_landmark') is True else 'pn_plus_1_unknown')
    if state.get('tp_reached_by_landmark') is not False:
        violations.append('terminal_le_landmark' if state.get('tp_reached_by_landmark') is True else 'terminal_unknown')
    if state.get('favorable_vwap_crossed_by_landmark') is not False:
        violations.append('cross_le_landmark' if state.get('favorable_vwap_crossed_by_landmark') is True else 'cross_unknown')
    eligible = len(violations) == 0
    return {'eligible': eligible, 'reason': 'ok' if eligible else 'violations', 'violations': violations}


def classify_post_landmark_outcome(state: Dict[str, Any], *, primitive_events_after_landmark: list[dict]) -> Dict[str, Any]:
    names_by_ts = defaultdict(list)
    for ev in primitive_events_after_landmark:
        if isinstance(ev, dict) and ev.get('timestamp_ms') is not None:
            names_by_ts[int(ev['timestamp_ms'])].append(ev.get('event'))
    if any(len(v) > 1 for v in names_by_ts.values()):
        return {'post_landmark_outcome': 'SAME_CANDLE_AMBIGUOUS', 'detail': names_by_ts}
    if not names_by_ts:
        return {'post_landmark_outcome': 'CENSORED'}
    p = None
    t = None
    for ts in sorted(names_by_ts):
        names = names_by_ts[ts]
        if p is None and any(n in ('reached_pn_plus_1', 'progression') for n in names):
            p = ts
        if t is None and any(n in ('tp_cycle_closed', 'tp_before_pn_plus_1', 'tp_hit', 'cycle_closed') for n in names):
            t = ts
    if p is None and t is None:
        return {'post_landmark_outcome': 'CENSORED'}
    if p is not None and t is not None:
        return {'post_landmark_outcome': 'PN_PLUS_1_BEFORE_TP' if p < t else 'TP_BEFORE_PN_PLUS_1' if t < p else 'SAME_CANDLE_AMBIGUOUS'}
    if p is not None:
        return {'post_landmark_outcome': 'PN_PLUS_1_BEFORE_TP'}
    return {'post_landmark_outcome': 'TP_BEFORE_PN_PLUS_1'}


def control_eligible_at_landmark(control_row: Dict[str, Any], landmark_ms: int) -> Dict[str, Any]:
    violations = []
    unit = control_row.get('time_coordinate_unit')
    landmark_unit = control_row.get('landmark_coordinate_unit')
    if unit is not None and landmark_unit is not None and unit != landmark_unit:
        violations.append('time_coordinate_mismatch')
    state = reconstruct_state_at_landmark(control_row, landmark_ms)
    elig = eligibility_from_state_at_T(state)
    violations.extend(elig.get('violations', []))
    eligible = not violations
    return {'eligible': eligible, 'violations': violations, 'landmark_ms': landmark_ms, 'state': state}




def landmark_outcome_after_cross(treated_row: Dict[str, Any], control_row: Dict[str, Any]) -> Dict[str, Any]:
    if treated_row.get('cross_timestamp_ms') is None:
        return {'control_landmark_ms': None, 'control_post_outcome': None, 'status': 'missing_timestamp'}
    landmark = int(control_row['episode_start_timestamp_ms']) + int(treated_row['cross_elapsed_ms'])
    state = reconstruct_state_at_landmark(control_row, landmark)
    elig = eligibility_from_state_at_T(state)
    if not elig['eligible']:
        return {'control_landmark_ms': landmark, 'control_post_outcome': None, 'status': 'ineligible', 'state': state, 'eligibility': elig}
    # future label only after eligibility is frozen
    future_events = []
    return {'control_landmark_ms': landmark, 'control_post_outcome': classify_post_landmark_outcome(state, primitive_events_after_landmark=future_events)['post_landmark_outcome'], 'status': 'ok', 'state': state, 'eligibility': elig}


def temporal_split_by_cycle(rows: list[Dict[str, Any]], train_fraction: float = TRAIN_FRACTION):
    rows = sorted(rows, key=lambda r: (r.get('episode_start_timestamp_ms') or r.get('start_timestamp_ms') or 0, r.get('cycle_id') or ''))
    cycles = []
    seen = set()
    for r in rows:
        cy = r.get('cycle_id') or r.get('cycle_key')
        if cy not in seen:
            seen.add(cy)
            cycles.append(cy)
    cut = max(1, int(len(cycles) * train_fraction))
    train_cycles = set(cycles[:cut])
    train = [r for r in rows if (r.get('cycle_id') or r.get('cycle_key')) in train_cycles]
    test = [r for r in rows if (r.get('cycle_id') or r.get('cycle_key')) not in train_cycles]
    return train, test


def _safe_rate(prog: float, reg: float) -> float | None:
    n = prog + reg
    return prog / n if n else None


def _or_ci(tprog: float, treg: float, cprog: float, creg: float) -> tuple[float, list[float]] | None:
    if min(tprog + treg, cprog + creg) <= 0:
        return None
    aa, bb, cc, dd = [x + 0.5 for x in (tprog, treg, cprog, creg)]
    orv = (aa * dd) / (bb * cc)
    se = math.sqrt(1 / aa + 1 / bb + 1 / cc + 1 / dd)
    lo = math.exp(math.log(orv) - 1.96 * se)
    hi = math.exp(math.log(orv) + 1.96 * se)
    return orv, [lo, hi]


def summarize(rows: list[Dict[str, Any]]) -> Dict[str, Any]:
    prog = sum(1 for r in rows if r['post_cross_outcome'] == 'progression')
    reg = sum(1 for r in rows if r['post_cross_outcome'] == 'regression')
    n = prog + reg
    return {'eligible': len(rows), 'progression': prog, 'regression': reg, 'order_sensitive_n': n, 'progression_rate': _safe_rate(prog, reg), 'ci95': None}


def safe_hazard_summary(rows: list[Dict[str, Any]]) -> Dict[str, Any]:
    crossed = [r for r in rows if r.get('cross_status') == 'valid']
    prog = sum(1 for r in crossed if r.get('post_cross_outcome') == 'progression')
    reg = sum(1 for r in crossed if r.get('post_cross_outcome') == 'regression')
    n = prog + reg
    return {'eligible': len(crossed), 'progression': prog, 'regression': reg, 'order_sensitive_n': n, 'progression_rate': _safe_rate(prog, reg), 'ci95': None}


def match_controls_for_cross(treated_rows: list[Dict[str, Any]], control_rows: list[Dict[str, Any]], k: int = K_MATCH, seed: str = 'fl-vwap-002-cross') -> Dict[str, Any]:
    matched_sets = []
    reuse = Counter()
    groups = defaultdict(list)
    for c in control_rows:
        groups[(c['symbol'], c['percentage'], c['direction'], c['active_step'])].append(c)
    for key in groups:
        groups[key] = sorted(groups[key], key=lambda r: (r['episode_start_timestamp_ms'], r['episode_key']))
    for t in treated_rows:
        t_partition = t.get('partition') or t.get('temporal_partition')
        key = (t['symbol'], t['percentage'], t['direction'], t['active_step'])
        candidates = []
        for c in groups.get(key, []):
            if t_partition is not None and c.get('partition') is not None and c.get('partition') != t_partition:
                continue
            if c['episode_key'] == t['episode_key']:
                continue
            control_landmark = int(c['episode_start_timestamp_ms']) + int(t['cross_elapsed_ms'])
            state = reconstruct_state_at_landmark(c, control_landmark)
            elig = eligibility_from_state_at_T(state)
            if not elig['eligible']:
                continue
            candidates.append(c)
        scored = []
        for c in candidates:
            h = hashlib.sha256(f"{seed}|{t['episode_key']}|{c['episode_key']}".encode()).hexdigest()
            scored.append((h, c))
        scored.sort(key=lambda x: x[0])
        selected = [c for _, c in scored[:k]]
        if not selected:
            continue
        w = 1.0 / len(selected)
        for c in selected:
            reuse[c['episode_key']] += 1
        matched_controls = []
        eligible = True
        violations = Counter()
        for c in selected:
            lm = landmark_outcome_after_cross(t, c)
            if lm.get('status') != 'ok':
                eligible = False
                for v in lm.get('violations', []):
                    violations[v] += 1
                continue
            assert lm.get('control_landmark_ms') is not None
            assert int(c['episode_start_timestamp_ms']) + int(t['cross_elapsed_ms']) == int(lm['control_landmark_ms'])
            matched_controls.append({**c, **lm})
        if not eligible or not matched_controls:
            continue
        control_post = matched_controls[0].get('control_post_outcome') if matched_controls else None
        matched_sets.append({'treated': t, 'controls': matched_controls, 'treated_weight': 1.0, 'control_weight_each': w, 'control_landmark_ms': matched_controls[0]['control_landmark_ms'], 'control_post_outcome': control_post, 'control_violations': dict(violations)})
    control_weights = Counter()
    for m in matched_sets:
        for c in m['controls']:
            control_weights[c['episode_key']] += m['control_weight_each']
    return {'matched_sets': matched_sets, 'control_reuse': dict(reuse), 'control_weights': dict(control_weights)}


def stream_match_controls_for_cross(treated_rows: list[Dict[str, Any]], control_rows: list[Dict[str, Any]], k: int = K_MATCH, seed: str = 'fl-vwap-002-cross'):
    """Yield matched assignments one at a time in frozen deterministic order."""
    groups = defaultdict(list)
    for c in control_rows:
        groups[(c['symbol'], c['percentage'], c['direction'], c['active_step'])].append(c)
    for key in groups:
        groups[key] = sorted(groups[key], key=lambda r: (r['episode_start_timestamp_ms'], r['episode_key']))
    for t in treated_rows:
        t_partition = t.get('partition') or t.get('temporal_partition')
        key = (t['symbol'], t['percentage'], t['direction'], t['active_step'])
        candidates = []
        for c in groups.get(key, []):
            if t_partition is not None and c.get('partition') is not None and c.get('partition') != t_partition:
                continue
            if c['episode_key'] == t['episode_key']:
                continue
            control_landmark = int(c['episode_start_timestamp_ms']) + int(t['cross_elapsed_ms'])
            state = reconstruct_state_at_landmark(c, control_landmark)
            elig = eligibility_from_state_at_T(state)
            if not elig['eligible']:
                continue
            candidates.append(c)
        scored = []
        for c in candidates:
            h = hashlib.sha256(f"{seed}|{t['episode_key']}|{c['episode_key']}".encode()).hexdigest()
            scored.append((h, c))
        scored.sort(key=lambda x: x[0])
        selected = [c for _, c in scored[:k]]
        if not selected:
            continue
        w = 1.0 / len(selected)
        for c in selected:
            control_landmark_ms = int(c['episode_start_timestamp_ms']) + int(t['cross_elapsed_ms'])
            yield {
                'treated_episode_key': t['episode_key'],
                'treated_symbol': t['symbol'],
                'treated_percentage': t['percentage'],
                'treated_direction': t['direction'],
                'treated_active_step': int(t['active_step']),
                'control_episode_key': c['episode_key'],
                'control_landmark_ms': control_landmark_ms,
                'control_weight': w,
                'treated_elapsed_T_ms': int(t['cross_elapsed_ms']),
                'treated_partition': t_partition,
                'control_partition': c.get('partition') or c.get('temporal_partition'),
                'selection_rank': scored.index((next(h for h, cc in scored if cc['episode_key'] == c['episode_key']), c)) if False else None,
                'treated_episode': t,
                'control_episode': c,
            }


def _pairwise_rates(matched_sets: list[Dict[str, Any]]) -> Dict[str, Any]:
    treated_prog = treated_reg = 0.0
    ctrl_prog = ctrl_reg = 0.0
    for m in matched_sets:
        t = m['treated']
        if t['post_cross_outcome'] == 'progression':
            treated_prog += 1.0
        elif t['post_cross_outcome'] == 'regression':
            treated_reg += 1.0
        w = m['control_weight_each']
        for c in m['controls']:
            if c['post_cross_outcome'] == 'progression':
                ctrl_prog += w
            elif c['post_cross_outcome'] == 'regression':
                ctrl_reg += w
    tn = treated_prog + treated_reg
    cn = ctrl_prog + ctrl_reg
    tr = treated_prog / tn if tn else None
    cr = ctrl_prog / cn if cn else None
    return {'treated': {'progression': treated_prog, 'regression': treated_reg, 'order_sensitive_n': tn, 'progression_rate': tr}, 'control': {'progression': ctrl_prog, 'regression': ctrl_reg, 'order_sensitive_n': cn, 'progression_rate': cr}}


def canonical_rows(con: sqlite3.Connection) -> list[Dict[str, Any]]:
    rows = []
    for ep in iter_episodes(con):
        obs = list(iter_obs(con, ep))
        if not obs:
            continue
        evo = loads(ep['evolution_json'])
        start = int(ep['start_timestamp_ms'])
        outcome = 'progression' if ep['terminal_event'] == 'pn_plus_1_before_tp' else 'regression' if ep['terminal_event'] == 'tp_before_pn_plus_1' else 'censored'
        start_lad = obs[0][2]
        start_cond = None
        if start_lad.get('pn') is not None and obs[0][1].get('vwap') is not None:
            vwap = float(obs[0][1].get('vwap'))
            pn = float(start_lad.get('pn'))
            start_cond = vwap >= pn if ep['direction'] == 'SELL' else vwap <= pn
        cross_ts = None
        cross_idx = None
        cum_base = 0.0
        cum_quote = 0.0
        for i, (ts, market, lad) in enumerate(obs):
            price = market.get('price'); volume = market.get('volume')
            if price is None or volume is None:
                continue
            price = float(price); volume = float(volume)
            cum_base += volume; cum_quote += price * volume
            vwap = cum_quote / cum_base if cum_base else price
            pn = lad.get('pn')
            if pn is None:
                continue
            pn = float(pn)
            cond = vwap >= pn if ep['direction'] == 'SELL' else vwap <= pn
            if cond and cross_ts is None:
                cross_ts = ts
                cross_idx = i
                break
        prog_ts = None
        for ts, market, lad in obs:
            if lad.get('active_step') is not None and int(lad.get('active_step')) > int(ep['active_step']):
                prog_ts = ts
                break
        rows.append({
            'episode_key': ep['episode_key'],
            'symbol': ep['symbol'],
            'percentage': ep['percentage'],
            'direction': ep['direction'],
            'active_step': int(ep['active_step']),
            'cycle_id': str(ep['cycle_id']),
            'episode_start_timestamp_ms': start,
            'cross_timestamp_ms': cross_ts,
            'cross_elapsed_ms': None if cross_ts is None else cross_ts - start,
            'pn_plus_1_timestamp_ms': prog_ts,
            'tp_or_terminal_timestamp_ms': int(ep['end_timestamp_ms']),
            'post_cross_outcome': outcome,
            'intrabar_order_ambiguous': bool(ep.get('intrabar_order_ambiguous')),
            'start_condition_present': start_cond,
            'order_classification': classify_event_ordering(evo),
            'valid_cross': cross_ts is not None and cross_ts > start and outcome != 'censored',
            'cross_index': cross_idx,
            'total_obs': len(obs),
        })
    return rows


def run_cross_riskset(report_dir: Path = REPORT_DIR) -> Dict[str, Any]:
    report_dir.mkdir(parents=True, exist_ok=True)
    con = connect()
    atomic_write_json(CHECKPOINT, {'stage': 'starting', 'rss_kb': rss_kb(), 'peak_rss_kb': peak_rss_kb()})
    rows = canonical_rows(con)
    atomic_write_json(CHECKPOINT, {'stage': 'canonicalized', 'rows': len(rows), 'rss_kb': rss_kb(), 'peak_rss_kb': peak_rss_kb()})

    reasons = Counter()
    valid = []
    excluded = []
    for r in rows:
        status = None
        if r['intrabar_order_ambiguous']:
            status = 'same_candle_ambiguous'
        elif r['cross_timestamp_ms'] is None:
            status = 'missing_timestamp'
        elif r['cross_timestamp_ms'] <= r['episode_start_timestamp_ms']:
            status = 'cross_before_start'
        elif r['tp_or_terminal_timestamp_ms'] <= r['episode_start_timestamp_ms']:
            status = 'cross_after_terminal'
        elif r['tp_or_terminal_timestamp_ms'] < r['cross_timestamp_ms']:
            status = 'outcome_before_cross'
        elif r['tp_or_terminal_timestamp_ms'] == r['cross_timestamp_ms']:
            status = 'same_candle_ambiguous'
        else:
            status = 'valid'
        r['cross_status'] = status
        reasons[status] += 1
        if status == 'valid':
            valid.append(r)
        else:
            excluded.append(r)
    atomic_write_json(CHECKPOINT, {'stage': 'classified', 'rows': len(rows), 'valid': len(valid), 'excluded': len(excluded), 'reasons': dict(reasons), 'rss_kb': rss_kb(), 'peak_rss_kb': peak_rss_kb()})

    old_present = {r['episode_key'] for r in rows if r['start_condition_present']}
    new_valid = {r['episode_key'] for r in valid}
    old_only = old_present - new_valid
    new_only = new_valid - old_present
    intersection = old_present & new_valid

    start_groups = defaultdict(list)
    cross_groups = defaultdict(list)
    for r in rows:
        k = (r['symbol'], r['percentage'], r['direction'], r['active_step'])
        start_groups[k].append(r)
        if r['cross_status'] == 'valid':
            cross_groups[k].append(r)
    start_cells = []
    cross_cells = []
    for k, grp in sorted(start_groups.items()):
        sym, pct, d, step = k
        base = [r for r in grp if r['post_cross_outcome'] in ('progression', 'regression')]
        present = [r for r in grp if r['start_condition_present']]
        absent = [r for r in grp if r['start_condition_present'] is False]
        bprog = sum(1 for r in base if r['post_cross_outcome'] == 'progression')
        breg = sum(1 for r in base if r['post_cross_outcome'] == 'regression')
        cprog = sum(1 for r in present if r['post_cross_outcome'] == 'progression')
        creg = sum(1 for r in present if r['post_cross_outcome'] == 'regression')
        aprog = sum(1 for r in absent if r['post_cross_outcome'] == 'progression')
        areg = sum(1 for r in absent if r['post_cross_outcome'] == 'regression')
        bn = bprog + breg; cn = cprog + creg; an = aprog + areg
        start_cells.append({'symbol': sym,'percentage': pct,'direction': d,'active_step': step,'baseline': {'eligible': len(base),'progression': bprog,'regression': breg,'order_sensitive_n': bn,'progression_rate': _safe_rate(bprog, breg),'ci95': None}, 'condition_present': {'eligible': len(present),'progression': cprog,'regression': creg,'order_sensitive_n': cn,'progression_rate': _safe_rate(cprog, creg),'ci95': None}, 'condition_absent': {'eligible': len(absent),'progression': aprog,'regression': areg,'order_sensitive_n': an,'progression_rate': _safe_rate(aprog, areg),'ci95': None}, 'lift_pp': (_safe_rate(cprog, creg) - _safe_rate(bprog, breg)) if bn and cn else None, 'relative_risk': (_safe_rate(cprog, creg) / _safe_rate(bprog, breg)) if bn and cn and _safe_rate(bprog, breg) else None, 'odds_ratio': None, 'or_ci95': None, 'ambiguous_excluded': sum(1 for r in grp if r['intrabar_order_ambiguous']),'low_n': bn < MIN_N or cn < MIN_N})
    eligible_controls = [r for r in rows if r['cross_status'] != 'valid']
    for k, grp in sorted(cross_groups.items()):
        sym, pct, d, step = k
        pool = [r for r in eligible_controls if r['symbol']==sym and r['percentage']==pct and r['direction']==d and r['active_step']==step]
        matched_sets = match_controls_for_cross(grp, pool, k=K_MATCH)
        rr = _pairwise_rates(matched_sets['matched_sets'])
        cross_cells.append({'symbol': sym,'percentage': pct,'direction': d,'active_step': step,'treated': rr['treated'],'control': rr['control'],'matched_sets': len(matched_sets['matched_sets']),'unique_treated': len({m['treated']['episode_key'] for m in matched_sets['matched_sets']}),'unique_control': len(set(matched_sets['control_weights'])), 'control_reuse': matched_sets['control_reuse'], 'control_weights': matched_sets['control_weights']})

    patterns = []
    for cell in cross_cells:
        tr = cell['treated']; cr = cell['control']
        status = 'CANDIDATE' if tr['order_sensitive_n'] >= MIN_N and cr['order_sensitive_n'] >= MIN_N else 'PENDING_RISK_SET_VERIFICATION'
        patterns.append({'name': f"FL-VWAP-002-CROSS-RISKSET::{cell['symbol']}::{cell['percentage']}::{cell['direction']}::P{cell['active_step']}", 'symbol': cell['symbol'], 'percentage': cell['percentage'], 'direction': cell['direction'], 'active_step': cell['active_step'], 'status': status, 'treated_rate': tr['progression_rate'], 'control_rate': cr['progression_rate'], 'lift_pp': (tr['progression_rate'] - cr['progression_rate']) if tr['progression_rate'] is not None and cr['progression_rate'] is not None else None})

    train, oos = temporal_split_by_cycle(rows, train_fraction=TRAIN_FRACTION)
    def split_analysis(subrows):
        valid_sub = [r for r in subrows if r['cross_status'] == 'valid']
        controls_sub = [r for r in subrows if r['cross_status'] != 'valid']
        matched = match_controls_for_cross(valid_sub, controls_sub, k=K_MATCH)
        rr = _pairwise_rates(matched['matched_sets'])
        return {'treated': rr['treated'], 'control': rr['control'], 'matched_sets': len(matched['matched_sets']), 'unique_treated': len({m['treated']['episode_key'] for m in matched['matched_sets']}), 'unique_control': len(set(matched['control_weights'])), 'control_reuse': matched['control_reuse'], 'control_weights': matched['control_weights']}
    train_res = split_analysis(train)
    oos_res = split_analysis(oos)

    def subset(name_symbol, pct, direction, step):
        return [r for r in rows if r['symbol']==name_symbol and r['percentage']==pct and r['direction']==direction and r['active_step']==step]
    p0_audit = []
    for sym, direction in [('ZEC','BUY'),('SOL','BUY'),('ETH','BUY'),('SOL','SELL'),('BTC','BUY')]:
        grp = subset(sym, '0.001', direction, 0)
        valid_grp = [r for r in grp if r['cross_status']=='valid']
        old_present = [r for r in grp if r['start_condition_present']]
        matched = match_controls_for_cross(valid_grp, [r for r in rows if r not in valid_grp and r['symbol']==sym and r['percentage']=='0.001' and r['direction']==direction and r['active_step']==0], k=K_MATCH)
        rr = _pairwise_rates(matched['matched_sets'])
        p0_audit.append({'symbol': sym, 'direction': direction, 'episode_n': len(grp), 'old_condition_present_n': len(old_present), 'valid_cross_n': len(valid_grp), 'outcome_before_cross_n': sum(1 for r in grp if r['cross_status']=='outcome_before_cross'), 'same_candle_ambiguous_n': sum(1 for r in grp if r['cross_status']=='same_candle_ambiguous'), 'median_cross_elapsed_ms': sorted([r['cross_elapsed_ms'] for r in valid_grp])[len(valid_grp)//2] if valid_grp else None, 'treated_rate': rr['treated']['progression_rate'], 'control_rate': rr['control']['progression_rate'], 'train': train_res['treated']['progression_rate'], 'oos': oos_res['treated']['progression_rate']})
    btc_sell = []
    for step in [1,2,3]:
        grp = subset('BTC', '0.001', 'SELL', step)
        valid_grp = [r for r in grp if r['cross_status']=='valid']
        matched = match_controls_for_cross(valid_grp, [r for r in rows if r not in valid_grp and r['symbol']=='BTC' and r['percentage']=='0.001' and r['direction']=='SELL' and r['active_step']==step], k=K_MATCH)
        rr = _pairwise_rates(matched['matched_sets'])
        btc_sell.append({'active_step': step, 'episode_n': len(grp), 'valid_cross_n': len(valid_grp), 'treated_rate': rr['treated']['progression_rate'], 'control_rate': rr['control']['progression_rate']})

    report_verification = {
        'classification': 'PENDING_RISK_SET_VERIFICATION',
        'git_commit': git_commit(),
        'counts': dict(reasons),
        'old_condition_present_n': len(old_present),
        'new_valid_cross_n': len(new_valid),
        'intersection_n': len(intersection),
        'old_only_n': len(old_only),
        'new_only_n': len(new_only),
        'old_only_reasons': {'outcome_before_cross': sum(1 for r in rows if r['episode_key'] in old_only and r['cross_status']=='outcome_before_cross'), 'same_candle_ambiguous': sum(1 for r in rows if r['episode_key'] in old_only and r['cross_status']=='same_candle_ambiguous'), 'cross_after_terminal': sum(1 for r in rows if r['episode_key'] in old_only and r['cross_status']=='cross_after_terminal'), 'missing_timestamp': sum(1 for r in rows if r['episode_key'] in old_only and r['cross_status']=='missing_timestamp')},
        'same_candle_ambiguous_n': reasons.get('same_candle_ambiguous', 0),
        'cross_after_terminal_n': reasons.get('cross_after_terminal', 0),
        'missing_timestamp_n': reasons.get('missing_timestamp', 0),
        'risk_set_method': 'Deterministic hash-based matched controls up to K=5 per treated cross within the same symbol/percentage/direction/active_step stratum; controls must be alive and outcome-free at the landmark time T.',
        'matched_control_reuse_distribution': dict(Counter(matched['control_reuse'])) if 'matched' in locals() else {},
        'max_control_reuse': max(matched['control_reuse'].values()) if 'matched' in locals() and matched['control_reuse'] else 0,
        'effective_weighted_control_n': sum(matched['control_weights'].values()) if 'matched' in locals() and matched['control_weights'] else 0,
        'pooled': {'treated': train_res['treated'], 'control': train_res['control']},
        'train': train_res,
        'oos': oos_res,
        'p0_audit': p0_audit,
        'btc_sell_0_001': btc_sell,
        'patterns': patterns,
        'cross_population': {'treated_unique': len(new_valid), 'control_unique': len(set(matched['control_weights'])) if 'matched' in locals() else 0},
    }

    atomic_write_json(report_dir / 'fl_vwap_003_riskset_integrity.json', report_verification)
    atomic_write_json(report_dir / 'fl_vwap_003_riskset_results.json', {'pooled': report_verification['pooled'], 'train': train_res, 'oos': oos_res, 'p0_audit': p0_audit, 'btc_sell_0_001': btc_sell})
    atomic_write_json(report_dir / 'fl_vwap_003_riskset_train_oos.json', {'train': train_res, 'oos': oos_res, 'temporal_split': {'train_n': len(train), 'oos_n': len(oos), 'train_fraction': TRAIN_FRACTION}})
    atomic_write_json(report_dir / 'fl_vwap_003_riskset_patterns.json', {'patterns': patterns})
    atomic_write_json(CHECKPOINT, {'stage': 'complete', 'rows': len(rows), 'valid_cross': len(valid), 'rss_kb': rss_kb(), 'peak_rss_kb': peak_rss_kb()})
    return report_verification


if __name__ == '__main__':
    print(json.dumps(run_cross_riskset(), indent=2, sort_keys=True))
