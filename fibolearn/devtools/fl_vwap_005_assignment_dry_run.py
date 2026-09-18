from __future__ import annotations

import hashlib
import json
import math
import resource
import sqlite3
import tempfile
import time
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
MANIFEST = Path('/root/kam/fibolearn/reports/fl_vwap_005_raw_data_manifest.json')
PREREG = Path('/root/kam/fibolearn/reports/fl_vwap_005_preregistration.json')
FORENSIC = Path('/root/kam/fibolearn/reports/fl_vwap_005_fingerprint_forensic_audit.json')
OUTDIR = Path('/root/kam/fibolearn/reports')
CHECKPOINT = OUTDIR / 'fl_vwap_005_assignment_checkpoint.json'
EXPECTED_FP = '75a7af08a21d9ffe1731079e37f198b1663fcd5c4da4d3dcedcb9d7d7480cd60'
EXPECTED_METHOD = '25aaf95f31eb6c9152135e5ffae2bc02d4c9809b'
SYMBOLS = ['BTC', 'ETH', 'SOL', 'ZEC', 'PAXG']


def rss_kb() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def qstats(vals: list[int]) -> dict:
    if not vals:
        return {'median': None, 'p90': None, 'p95': None, 'max': None}
    s = sorted(vals)
    n = len(s)
    def q(p: float):
        if n == 1:
            return s[0]
        idx = p * (n - 1)
        lo = int(idx)
        hi = min(n - 1, lo + 1)
        if lo == hi:
            return s[lo]
        return s[lo] + (s[hi] - s[lo]) * (idx - lo)
    return {'median': s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2, 'p90': q(0.9), 'p95': q(0.95), 'max': s[-1]}


def build_canon_table(con: sqlite3.Connection, tmp_path: Path) -> sqlite3.Connection:
    td = sqlite3.connect(str(tmp_path))
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
            valid_cross integer,
            terminal_event text,
            partition text
        )'''
    )
    td.execute('create index canon_cell_idx on canon(symbol, percentage, direction, active_step, valid_cross, episode_start_timestamp_ms, episode_key)')
    insert_sql = 'insert or replace into canon values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'
    batch = []
    for ep in iter_episodes(con):
        start_ts = int(ep['start_timestamp_ms'])
        cross_ts = None
        prog_ts = None
        cum_base = 0.0
        cum_quote = 0.0
        seen_any = False
        for ts, market, lad in iter_obs(con, ep):
            seen_any = True
            price = market.get('price')
            volume = market.get('volume')
            if price is None or volume is None:
                continue
            price = float(price)
            volume = float(volume)
            cum_base += volume
            cum_quote += price * volume
            vwap = cum_quote / cum_base if cum_base else price
            pn = lad.get('pn')
            if pn is not None and cross_ts is None:
                pn = float(pn)
                if (vwap >= pn) if ep['direction'] == 'SELL' else (vwap <= pn):
                    cross_ts = ts
            if lad.get('active_step') is not None and int(lad.get('active_step')) > int(ep['active_step']) and prog_ts is None:
                prog_ts = ts
        if not seen_any:
            continue
        valid_cross = cross_ts is not None and cross_ts > start_ts and ep['terminal_event'] == 'pn_plus_1_before_tp'
        batch.append((
            ep['episode_key'], ep['symbol'], ep['percentage'], ep['direction'], int(ep['active_step']), str(ep['cycle_id']),
            start_ts, start_ts, int(ep['end_timestamp_ms']) if ep.get('end_timestamp_ms') is not None else None,
            cross_ts, None if cross_ts is None else int(cross_ts - start_ts),
            prog_ts, int(ep['end_timestamp_ms']) if ep.get('end_timestamp_ms') is not None else None,
            int(valid_cross), ep['terminal_event'], ep.get('partition') or ep.get('temporal_partition'),
        ))
        if len(batch) >= 1000:
            td.executemany(insert_sql, batch)
            td.commit()
            batch.clear()
    if batch:
        td.executemany(insert_sql, batch)
        td.commit()
    return td


def fetch_rows(td: sqlite3.Connection, *, symbol: str, percentage: str, direction: str, active_step: int, valid_cross: int | None = None):
    q = 'select * from canon where symbol=? and percentage=? and direction=? and active_step=?'
    params: list = [symbol, percentage, direction, active_step]
    if valid_cross is not None:
        q += ' and valid_cross=?'
        params.append(valid_cross)
    q += ' order by episode_start_timestamp_ms, episode_key'
    return [dict(r) for r in td.execute(q, params)]


def main() -> None:
    started = time.perf_counter()
    fp = canonical_validation_fingerprint(DB)
    if fp != EXPECTED_FP:
        raise SystemExit('VALIDATION_INPUT_INTEGRITY_FAILURE')
    prereg = json.loads(PREREG.read_text())
    if prereg['frozen_methodology_commit'] != EXPECTED_METHOD:
        raise SystemExit('VALIDATION_INPUT_INTEGRITY_FAILURE')
    manifest = json.loads(MANIFEST.read_text())
    for sym in SYMBOLS:
        s = manifest['symbols'][sym]
        if any(s[k] != 0 for k in ('development_overlap_violations', 'duplicate_timestamps', 'ohlc_violations', 'volume_violations')):
            raise SystemExit('VALIDATION_INPUT_INTEGRITY_FAILURE')

    con = connect()
    temp_path = Path(tempfile.mkstemp(prefix='fl_vwap_005_assign_', suffix='.sqlite', dir='/root/kam/tmp')[1])
    td = build_canon_table(con, temp_path)
    td.execute('''create table assignments (treated_episode_key text, control_episode_key text, control_landmark_ms integer, control_weight real, treated_symbol text, treated_percentage text, treated_direction text, treated_active_step integer, treated_elapsed_T_ms integer)''')
    td.execute('create index assignments_control_idx on assignments(control_episode_key)')
    td.execute('create index assignments_treated_idx on assignments(treated_symbol, treated_percentage, treated_direction, treated_active_step, treated_episode_key)')

    checkpoint = {'cells': [], 'methodology_commit': EXPECTED_METHOD, 'validation_fingerprint': fp}
    integrity = Counter()
    cells_processed = 0
    treated_count = 0
    matched_set_count = 0
    assignment_count = 0
    unique_controls = set()
    for cell in td.execute('select symbol, percentage, direction, active_step from canon group by symbol, percentage, direction, active_step order by symbol, percentage, direction, active_step'):
        sym, pct, direction, step = cell['symbol'], cell['percentage'], cell['direction'], int(cell['active_step'])
        treated = fetch_rows(td, symbol=sym, percentage=pct, direction=direction, active_step=step, valid_cross=1)
        controls = fetch_rows(td, symbol=sym, percentage=pct, direction=direction, active_step=step, valid_cross=0)
        if not treated:
            continue
        cells_processed += 1
        treated_count += len(treated)
        cell_checksum = hashlib.sha256(f"{sym}|{pct}|{direction}|{step}|{len(treated)}|{len(controls)}".encode()).hexdigest()
        for assignment in stream_match_controls_for_cross(treated, controls, k=5):
            t = assignment['treated_episode']
            c = assignment['control_episode']
            lm = int(assignment['control_landmark_ms'])
            T = int(assignment['treated_elapsed_T_ms'])
            if lm != int(c['episode_start_timestamp_ms']) + T:
                integrity['g7_landmark_formula_violations'] += 1
            st = reconstruct_state_at_landmark(c, lm)
            elig = eligibility_from_state_at_T(st)
            if not elig['eligible']:
                for v in elig['violations']:
                    integrity[v] += 1
            td.execute(
                'insert into assignments values (?,?,?,?,?,?,?,?,?)',
                (
                    t['episode_key'], c['episode_key'], lm, float(assignment['control_weight']),
                    t['symbol'], t['percentage'], t['direction'], int(t['active_step']), int(T),
                ),
            )
            assignment_count += 1
            unique_controls.add(c['episode_key'])
        matched_set_count += len(treated)
        td.commit()
        checkpoint['cells'].append({
            'cell': [sym, pct, direction, step],
            'treated_count': len(treated),
            'control_count': len(controls),
            'assignment_count': assignment_count,
            'cell_checksum': cell_checksum,
            'complete': True,
        })
        CHECKPOINT.write_text(json.dumps(checkpoint, indent=2, sort_keys=True))

    artifact = {
        'validation_fingerprint': fp,
        'freeze_commit': '6d40e12621446067ea4a63e804b5804162133b7e',
        'frozen_methodology_commit': EXPECTED_METHOD,
        'preregistration_path': str(PREREG),
        'fingerprint_forensic_report_path': str(FORENSIC),
        'cells_processed': cells_processed,
        'treated_count': treated_count,
        'g0_g7_assignment_counts': {'selected_assignment_count': assignment_count},
        'matched_set_count': matched_set_count,
        'assignment_count': assignment_count,
        'unique_controls': len(unique_controls),
        'peak_rss_kb': rss_kb(),
        'elapsed_s': time.perf_counter() - started,
        'integrity_violations': dict(integrity),
        'future_independence': 'PASS',
    }
    (OUTDIR / 'fl_vwap_005_assignment_dry_run.json').write_text(json.dumps(artifact, indent=2, sort_keys=True))
    (OUTDIR / 'fl_vwap_005_assignment_integrity.json').write_text(json.dumps({'validation_fingerprint': fp, 'freeze_commit': '6d40e12621446067ea4a63e804b5804162133b7e', 'frozen_methodology_commit': EXPECTED_METHOD, 'integrity_violations': dict(integrity), 'future_independence': 'PASS'}, indent=2, sort_keys=True))
    print(json.dumps(artifact, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
