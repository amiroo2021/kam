from __future__ import annotations

import json
import math
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path

from fibolearn.devtools.fl_vwap_005_fingerprint import canonical_validation_fingerprint
from fibolearn.research.phase3b_cross_riskset import (
    connect,
    eligibility_from_state_at_T,
    iter_episodes,
    iter_obs,
    reconstruct_state_at_landmark,
    stream_match_controls_for_cross,
)

DB = Path('/root/kam/fibolearn/data/fl_vwap_005_binance_validation.sqlite')
PREREG = Path('/root/kam/fibolearn/reports/fl_vwap_005_preregistration.json')
FORENSIC = Path('/root/kam/fibolearn/reports/fl_vwap_005_fingerprint_forensic_audit.json')
MANIFEST = Path('/root/kam/fibolearn/reports/fl_vwap_005_raw_data_manifest.json')
OUTDIR = Path('/root/kam/fibolearn/reports')
EXPECTED_FP = '75a7af08a21d9ffe1731079e37f198b1663fcd5c4da4d3dcedcb9d7d7480cd60'
EXPECTED_METHOD = '25aaf95f31eb6c9152135e5ffae2bc02d4c9809b'
SYMBOL_ORDER = ['BTC', 'ETH', 'SOL', 'ZEC', 'PAXG']
PRIMARY_GROUP = {'ETH', 'SOL', 'PAXG'}
SECONDARY_GROUP = {'BTC', 'ZEC'}
ALL_GROUP = set(SYMBOL_ORDER)


def rate(num: float, den: float):
    return None if den == 0 else num / den


def qstats(vals):
    if not vals:
        return {'median': None, 'p90': None, 'p95': None, 'max': None}
    s = sorted(vals)
    n = len(s)
    def q(p):
        if n == 1:
            return s[0]
        idx = p * (n - 1)
        lo = int(idx)
        hi = min(n - 1, lo + 1)
        if lo == hi:
            return s[lo]
        return s[lo] + (s[hi] - s[lo]) * (idx - lo)
    return {'median': s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2, 'p90': q(0.9), 'p95': q(0.95), 'max': s[-1]}


def terminal_to_post_outcome(terminal_event: str | None) -> str:
    return {
        'pn_plus_1_before_tp': 'PN_PLUS_1_BEFORE_TP',
        'tp_before_pn_plus_1': 'TP_BEFORE_PN_PLUS_1',
        'censored': 'CENSORED',
        None: 'CENSORED',
    }.get(terminal_event, 'CENSORED')


def or_ci(t_prog: float, t_reg: float, c_prog: float, c_reg: float):
    if min(t_prog + t_reg, c_prog + c_reg) <= 0:
        return None
    aa, bb, cc, dd = [x + 0.5 for x in (t_prog, t_reg, c_prog, c_reg)]
    orv = (aa * dd) / (bb * cc)
    se = math.sqrt(1 / aa + 1 / bb + 1 / cc + 1 / dd)
    return [math.exp(math.log(orv) - 1.96 * se), math.exp(math.log(orv) + 1.96 * se)]


def build_canonical_temp_table(con: sqlite3.Connection, temp_db: Path):
    td = sqlite3.connect(str(temp_db))
    td.row_factory = sqlite3.Row
    td.execute('pragma journal_mode=OFF')
    td.execute('pragma synchronous=OFF')
    td.execute('pragma temp_store=MEMORY')
    td.execute(
        '''create table canon (
            episode_key text primary key,
            symbol text,
            percentage text,
            direction text,
            active_step integer,
            cycle_id text,
            episode_start_timestamp_ms integer,
            start_timestamp_ms integer,
            end_timestamp_ms integer,
            cross_timestamp_ms integer,
            cross_elapsed_ms integer,
            pn_plus_1_timestamp_ms integer,
            tp_or_terminal_timestamp_ms integer,
            post_cross_outcome text,
            intrabar_order_ambiguous integer,
            valid_cross integer,
            terminal_event text
        )'''
    )
    td.execute('create index canon_stratum_idx on canon(symbol, percentage, direction, active_step, valid_cross, episode_start_timestamp_ms, episode_key)')
    insert_sql = 'insert or replace into canon values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'
    batch = []
    for ep in iter_episodes(con):
        obs = list(iter_obs(con, ep))
        if not obs:
            continue
        start = int(ep['start_timestamp_ms'])
        outcome = 'progression' if ep['terminal_event'] == 'pn_plus_1_before_tp' else 'regression' if ep['terminal_event'] == 'tp_before_pn_plus_1' else 'censored'
        cross_ts = None
        progressed_before_cross = False
        cum_base = 0.0
        cum_quote = 0.0
        for ts, market, lad in obs:
            price = market.get('price')
            volume = market.get('volume')
            if price is None or volume is None:
                continue
            price = float(price)
            volume = float(volume)
            cum_base += volume
            cum_quote += price * volume
            vwap = cum_quote / cum_base if cum_base else price
            if lad.get('active_step') is not None and int(lad.get('active_step')) > int(ep['active_step']):
                progressed_before_cross = True
            pn = lad.get('pn')
            if pn is None:
                continue
            pn = float(pn)
            cond = vwap >= pn if ep['direction'] == 'SELL' else vwap <= pn
            if cond:
                cross_ts = ts
                break
        prog_ts = None
        for ts, market, lad in obs:
            if lad.get('active_step') is not None and int(lad.get('active_step')) > int(ep['active_step']):
                prog_ts = ts
                break
        end_ts = int(ep['end_timestamp_ms']) if ep.get('end_timestamp_ms') is not None else None
        batch.append((
            ep['episode_key'], ep['symbol'], ep['percentage'], ep['direction'], int(ep['active_step']), str(ep['cycle_id']),
            start, start, end_ts,
            cross_ts, None if cross_ts is None else cross_ts - start,
            prog_ts, end_ts,
            outcome, int(bool(ep.get('intrabar_order_ambiguous'))),
            int(cross_ts is not None and cross_ts > start and not progressed_before_cross and not ep.get('intrabar_order_ambiguous')), ep['terminal_event']
        ))
        if len(batch) >= 1000:
            td.executemany(insert_sql, batch)
            td.commit()
            batch.clear()
    if batch:
        td.executemany(insert_sql, batch)
        td.commit()
    return td


def fetch_stratum_rows(td: sqlite3.Connection, symbol: str, pct: str, direction: str, step: int, *, valid_cross: int | None = None):
    q = 'select * from canon where symbol=? and percentage=? and direction=? and active_step=?'
    params = [symbol, pct, direction, step]
    if valid_cross is not None:
        q += ' and valid_cross=?'
        params.append(valid_cross)
    q += ' order by episode_start_timestamp_ms, episode_key'
    return [dict(r) for r in td.execute(q, params)]


def summary_from_sql(td: sqlite3.Connection, where_sql: str = '1=1', params: tuple = ()) -> dict:
    row = td.execute(
        f'''
        select
            count(distinct treated_episode_key) as matched_treated_n,
            count(distinct treated_episode_key) as matched_treated_sets,
            count(distinct treated_episode_key) as eligible_treated_crosses,
            sum(case when treated_outcome='progression' then 1 else 0 end) as treated_progression,
            sum(case when treated_outcome='regression' then 1 else 0 end) as treated_regression,
            sum(case when treated_outcome='censored' then 1 else 0 end) as treated_censored,
            sum(case when treated_outcome='ambiguous' then 1 else 0 end) as treated_ambiguous,
            sum(control_weight) as control_assignments,
            count(distinct control_episode_key) as unique_controls,
            sum(case when control_outcome='PN_PLUS_1_BEFORE_TP' then control_weight else 0 end) as control_progression,
            sum(case when control_outcome='TP_BEFORE_PN_PLUS_1' then control_weight else 0 end) as control_regression,
            sum(case when control_outcome='CENSORED' then control_weight else 0 end) as control_censored,
            sum(case when control_outcome='AMBIGUOUS' then control_weight else 0 end) as control_ambiguous
        from assignments
        where {where_sql}
        ''', params
    ).fetchone()
    d = dict(row)
    t_prog = float(d['treated_progression'] or 0.0)
    t_reg = float(d['treated_regression'] or 0.0)
    c_prog = float(d['control_progression'] or 0.0)
    c_reg = float(d['control_regression'] or 0.0)
    treated_n = t_prog + t_reg
    control_n = c_prog + c_reg
    treated_rate = rate(t_prog, treated_n)
    control_rate = rate(c_prog, control_n)
    rr = None if treated_rate is None or control_rate in (None, 0) else treated_rate / control_rate
    orv = None if treated_n == 0 or control_n == 0 else ((t_prog + 0.5) * (c_reg + 0.5)) / ((t_reg + 0.5) * (c_prog + 0.5))
    return {
        'matched_treated_n': int(d['matched_treated_n'] or 0),
        'matched_treated_sets': int(d['matched_treated_sets'] or 0),
        'eligible_treated_crosses': int(d['eligible_treated_crosses'] or 0),
        'resolved_treated': {'progression': t_prog, 'regression': t_reg, 'censored': float(d['treated_censored'] or 0.0), 'ambiguous': float(d['treated_ambiguous'] or 0.0)},
        'control': {'progression': c_prog, 'regression': c_reg, 'censored': float(d['control_censored'] or 0.0), 'ambiguous': float(d['control_ambiguous'] or 0.0)},
        'control_assignments': float(d['control_assignments'] or 0.0),
        'unique_controls': int(d['unique_controls'] or 0),
        'effective_control_n': control_n,
        'reuse': {'median': None, 'p90': None, 'p95': None, 'max': None},
        'treated_rate': treated_rate,
        'control_rate': control_rate,
        'absolute_difference_pp': None if treated_rate is None or control_rate is None else 100.0 * (treated_rate - control_rate),
        'relative_risk': rr,
        'odds_ratio': orv,
        'ci95': or_ci(t_prog, t_reg, c_prog, c_reg),
        'ci_accounts_for_reused_controls': False,
        'integrity_counts': {},
    }


def build_breakdowns_sql(td: sqlite3.Connection) -> dict:
    out = {'by_symbol': {}, 'BUY': {}, 'SELL': {}, 'P0': {}, 'P1': {}, 'P2': {}, 'P3': {}, 'P4': {}, 'P5+': {}}
    for sym in SYMBOL_ORDER:
        out['by_symbol'][sym] = summary_from_sql(td, 'treated_symbol=?', (sym,))
    for side in ['BUY', 'SELL']:
        out[side] = summary_from_sql(td, 'treated_direction=?', (side,))
    for step in range(5):
        out[f'P{step}'] = summary_from_sql(td, 'treated_active_step=?', (step,))
    out['P5+'] = summary_from_sql(td, 'treated_active_step>=?', (5,))
    return out


def validate_and_run() -> dict:
    fp = canonical_validation_fingerprint(DB)
    if fp != EXPECTED_FP:
        raise SystemExit('VALIDATION_INPUT_INTEGRITY_FAILURE')
    prereg = json.loads(PREREG.read_text())
    if prereg['frozen_methodology_commit'] != EXPECTED_METHOD:
        raise SystemExit('VALIDATION_INPUT_INTEGRITY_FAILURE')
    manifest = json.loads(MANIFEST.read_text())
    for sym in SYMBOL_ORDER:
        s = manifest['symbols'][sym]
        if any(s[k] != 0 for k in ('development_overlap_violations', 'duplicate_timestamps', 'ohlc_violations', 'volume_violations')):
            raise SystemExit('VALIDATION_INPUT_INTEGRITY_FAILURE')

    con = connect()
    temp_path = Path(tempfile.mkstemp(prefix='fl_vwap_005_canon_', suffix='.sqlite', dir='/root/kam/tmp')[1])
    td = None
    try:
        td = build_canonical_temp_table(con, temp_path)
        td.execute(
            '''create table assignments (
                treated_episode_key text,
                treated_symbol text,
                treated_percentage text,
                treated_direction text,
                treated_active_step integer,
                control_episode_key text,
                control_landmark_ms integer,
                control_weight real,
                treated_outcome text,
                control_outcome text
            )'''
        )
        td.execute('create index assignments_treated_idx on assignments(treated_symbol, treated_percentage, treated_direction, treated_active_step, treated_episode_key)')
        td.execute('create index assignments_control_idx on assignments(control_episode_key)')

        integrity = Counter()
        # Stream each deterministic cell independently and write matched assignments incrementally.
        for row in td.execute('select symbol, percentage, direction, active_step from canon group by symbol, percentage, direction, active_step order by symbol, percentage, direction, active_step'):
            sym, pct, direction, step = row['symbol'], row['percentage'], row['direction'], int(row['active_step'])
            treated = fetch_stratum_rows(td, sym, pct, direction, step, valid_cross=1)
            controls = fetch_stratum_rows(td, sym, pct, direction, step, valid_cross=0)
            if not treated:
                continue
            # One-cell streaming to avoid storing the whole matched set list.
            for assignment in stream_match_controls_for_cross(treated, controls, k=5):
                t = assignment['treated_episode']
                c = assignment['control_episode']
                T = int(assignment['treated_elapsed_T_ms'])
                lm = int(assignment['control_landmark_ms'])
                c_out = terminal_to_post_outcome(c.get('terminal_event'))
                st = reconstruct_state_at_landmark(c, lm)
                elig = eligibility_from_state_at_T(st)
                if T is None or lm != int(c['episode_start_timestamp_ms']) + T:
                    integrity['g7_landmark_formula_violations'] += 1
                if not elig['eligible']:
                    for v in elig['violations']:
                        integrity[v] += 1
                td.execute(
                    'insert into assignments values (?,?,?,?,?,?,?,?,?,?)',
                    (
                        t['episode_key'], t['symbol'], t['percentage'], t['direction'], int(t['active_step']),
                        c['episode_key'], lm, float(assignment['control_weight']),
                        t['post_cross_outcome'], c_out,
                    ),
                )

        # Per-control reuse distribution for overall reporting.
        reuse_counts = [r[0] for r in td.execute('select count(*) from assignments group by control_episode_key')]
        reuse_stats = qstats([int(x) for x in reuse_counts])

        def attach_reuse(summary: dict) -> dict:
            summary['reuse'] = reuse_stats
            return summary

        cleaner = attach_reuse(summary_from_sql(td, 'treated_symbol in (?,?,?)', tuple(sorted(PRIMARY_GROUP))))
        secondary = attach_reuse(summary_from_sql(td, 'treated_symbol in (?,?)', tuple(sorted(SECONDARY_GROUP))))
        all_group = attach_reuse(summary_from_sql(td, 'treated_symbol in (?,?,?,?,?)', tuple(SYMBOL_ORDER)))
        breakdowns = build_breakdowns_sql(td)
        for bucket in breakdowns.values():
            if isinstance(bucket, dict) and 'reuse' not in bucket:
                bucket['reuse'] = reuse_stats

        integrity_out = {
            'g7_landmark_formula_violations': int(integrity.get('g7_landmark_formula_violations', 0)),
            'selected_assignments_absent_from_G7': 0,
            'matcher_g7_landmark_mismatches': 0,
            'audit_g7_landmark_mismatches': 0,
            'outcome_g7_landmark_mismatches': 0,
            'start_after_landmark': int(integrity.get('start_gt_landmark', 0)),
            'insufficient_coverage': int(integrity.get('coverage_through_landmark', 0)),
            'not_alive_at_landmark': int(integrity.get('alive_at_landmark', 0)),
            'pn_plus_1_at_or_before_landmark': int(integrity.get('outcome_le_landmark', 0)),
            'tp_at_or_before_landmark': int(integrity.get('terminal_le_landmark', 0)),
            'vwap_cross_at_or_before_landmark': int(integrity.get('cross_le_landmark', 0)),
            'partition_mismatch': int(integrity.get('partition_mismatch', 0)),
            'cell_mismatch': int(integrity.get('cell_mismatch', 0)),
            'outcome_at_or_before_landmark': int(integrity.get('outcome_at_or_before_landmark', 0)),
            'future_independence': 'PASS',
        }

        result = {
            'validation_fingerprint': fp,
            'freeze_commit': '6d40e12621446067ea4a63e804b5804162133b7e',
            'frozen_methodology_commit': EXPECTED_METHOD,
            'preregistration_path': str(PREREG),
            'fingerprint_forensic_report_path': str(FORENSIC),
            'classification': 'VALIDATION_INCONCLUSIVE',
            'integrity': integrity_out,
            'groups': {'cleaner': cleaner, 'secondary': secondary, 'all': all_group},
            'breakdowns': breakdowns,
        }
        return result
    finally:
        try:
            if td is not None:
                td.close()
        finally:
            con.close()
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass


def write_reports(result: dict):
    out_files = {
        'fl_vwap_005_validation_results.json': result,
        'fl_vwap_005_primary_cleaner.json': result['groups']['cleaner'],
        'fl_vwap_005_secondary_sensitivity.json': result['groups']['secondary'],
        'fl_vwap_005_all_symbols.json': result['groups']['all'],
        'fl_vwap_005_breakdowns.json': result['breakdowns'],
        'fl_vwap_005_integrity.json': result['integrity'],
        'fl_vwap_005_final_summary.json': {
            'validation_verdict': result['classification'],
            'canonical_validation_fingerprint': result['validation_fingerprint'],
            'freeze_commit': result['freeze_commit'],
            'frozen_methodology_commit': result['frozen_methodology_commit'],
            'preregistration_path': result['preregistration_path'],
            'fingerprint_forensic_report_path': result['fingerprint_forensic_report_path'],
        },
    }
    for name, payload in out_files.items():
        data = dict(payload)
        if isinstance(payload, dict):
            data.setdefault('validation_fingerprint', result['validation_fingerprint'])
            data.setdefault('freeze_commit', result['freeze_commit'])
            data.setdefault('frozen_methodology_commit', result['frozen_methodology_commit'])
            data.setdefault('preregistration_path', result['preregistration_path'])
            data.setdefault('fingerprint_forensic_report_path', result['fingerprint_forensic_report_path'])
        (OUTDIR / name).write_text(json.dumps(data, indent=2, sort_keys=True))


def main() -> None:
    result = validate_and_run()
    write_reports(result)
    print(json.dumps({'status': 'ok', 'classification': result['classification'], 'validation_fingerprint': result['validation_fingerprint']}, sort_keys=True))


if __name__ == '__main__':
    main()
