from __future__ import annotations

from collections import Counter
from pathlib import Path

from fibolearn.devtools.fl_vwap_005_fingerprint import canonical_validation_fingerprint
from fibolearn.research.phase3b_cross_riskset import connect, iter_episodes, iter_obs, match_controls_for_cross, stream_match_controls_for_cross

DB = Path('/root/kam/fibolearn/data/fl_vwap_005_binance_validation.sqlite')
FP = '75a7af08a21d9ffe1731079e37f198b1663fcd5c4da4d3dcedcb9d7d7480cd60'
SMOKE = ('ETH', '0.001', 'BUY', 1)


def _smoke_rows():
    con = connect()
    rows = []
    for ep in iter_episodes(con):
        if (ep['symbol'], ep['percentage'], ep['direction'], int(ep['active_step'])) != SMOKE:
            continue
        obs = list(iter_obs(con, ep))
        if not obs:
            continue
        start_ts = int(ep['start_timestamp_ms'])
        outcome = 'progression' if ep['terminal_event'] == 'pn_plus_1_before_tp' else 'regression' if ep['terminal_event'] == 'tp_before_pn_plus_1' else 'censored'
        cross_ts = None
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
            pn = lad.get('pn')
            if pn is None:
                continue
            pn = float(pn)
            if (vwap >= pn) if ep['direction'] == 'SELL' else (vwap <= pn):
                cross_ts = ts
                break
        valid_cross = cross_ts is not None and cross_ts > start_ts and outcome != 'censored'
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
            'episode_start_timestamp_ms': start_ts,
            'cross_timestamp_ms': cross_ts,
            'cross_elapsed_ms': None if cross_ts is None else int(cross_ts - start_ts),
            'pn_plus_1_timestamp_ms': prog_ts,
            'tp_or_terminal_timestamp_ms': int(ep['end_timestamp_ms']) if ep.get('end_timestamp_ms') is not None else None,
            'valid_cross': valid_cross,
            'terminal_event': ep['terminal_event'],
            'post_cross_outcome': outcome,
            'partition': ep.get('partition') or ep.get('temporal_partition'),
        })
    treated = [r for r in rows if r['valid_cross']]
    controls = [r for r in rows if not r['valid_cross']]
    return rows, treated, controls


def _flatten_old(matched):
    out = []
    for m in matched['matched_sets']:
        t = m['treated']
        for c in m['controls']:
            out.append((t['episode_key'], c['episode_key'], int(c['control_landmark_ms']), float(m['control_weight_each'])))
    return out


def test_streaming_matcher_matches_frozen_smoke_assignments():
    rows, treated, controls = _smoke_rows()
    old = match_controls_for_cross(treated, controls, k=5)
    new = list(stream_match_controls_for_cross(treated, controls, k=5))
    old_flat = _flatten_old(old)
    new_flat = [(r['treated_episode_key'], r['control_episode_key'], int(r['control_landmark_ms']), float(r['control_weight'])) for r in new]
    assert old_flat == new_flat
    assert len(old_flat) == 387
    assert Counter(k for k, _, _, _ in old_flat)


def test_streaming_matcher_is_deterministic_twice():
    _, treated, controls = _smoke_rows()
    from fibolearn.research.phase3b_cross_riskset import stream_match_controls_for_cross
    a = list(stream_match_controls_for_cross(treated, controls, k=5))
    b = list(stream_match_controls_for_cross(treated, controls, k=5))
    assert a == b


def test_validation_fingerprint_still_matches():
    assert canonical_validation_fingerprint(DB) == FP
