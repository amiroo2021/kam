from __future__ import annotations

import json
import sqlite3
import statistics
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Tuple

from fibolearn.config.defaults import DEFAULT_PERCENTAGES
from fibolearn import SCHEMA_VERSION
from fibolearn.labels.outcomes import complete_observation_outcome


def _D(x):
    return Decimal(str(x))


def _crossed(vals: list[tuple[int, Any]], level) -> tuple[bool, int | None]:
    if level is None or len(vals) < 2:
        return False, None
    lv = _D(level)
    prev = _D(vals[0][1]) - lv
    for ts, v in vals[1:]:
        cur = _D(v) - lv
        if (prev <= 0 <= cur) or (prev >= 0 >= cur):
            return True, int(ts)
        prev = cur
    return False, None


def _episode_evolution(obs):
    start_ts = obs[0][0]
    start_lad = obs[0][2]
    pn = start_lad.get('pn')
    vwap = [(ts, sv.get('market', {}).get('vwap')) for ts, _, _, sv in obs if sv.get('market', {}).get('vwap') is not None]
    poc = [(ts, sv.get('market', {}).get('poc')) for ts, _, _, sv in obs if sv.get('market', {}).get('poc') is not None]
    vw_cross, vw_ts = _crossed(vwap, pn)
    poc_cross, poc_ts = _crossed(poc, pn)
    prices = [_D(sv.get('market', {}).get('price') or lad.get('current_price')) for ts, _, lad, sv in obs]
    higher_highs = sum(1 for a, b in zip(prices, prices[1:]) if b > a)
    lower_highs = sum(1 for a, b in zip(prices, prices[1:]) if b < a)
    end_lad = obs[-1][2]

    def prox(lad, sv, target):
        levels = sv.get('features', {}).get('significant_levels', {})
        vals = [abs(_D(lad.get(target)) - _D(v)) for v in levels.values() if v is not None]
        return str(min(vals)) if vals else None

    events = []
    if vw_cross:
        events.append({'event': 'vwap_crossed_pn', 'timestamp_ms': vw_ts, 'elapsed_ms': vw_ts - start_ts})
    if poc_cross:
        events.append({'event': 'poc_crossed_pn', 'timestamp_ms': poc_ts, 'elapsed_ms': poc_ts - start_ts})
    return {
        'vwap_crossed_pn_since_start': vw_cross,
        'time_to_vwap_cross_ms': (vw_ts - start_ts if vw_ts else None),
        'poc_crossed_pn_since_start': poc_cross,
        'time_to_poc_cross_ms': (poc_ts - start_ts if poc_ts else None),
        'pn_retest_count_since_activation': max([int(x[2].get('pn_retest_count') or 0) for x in obs] or [0]),
        'time_since_last_pn_touch_ms': int(obs[-1][2].get('time_since_active_step_changed_ms') or 0),
        'higher_high_count': higher_highs,
        'lower_high_count': lower_highs,
        'progression_velocity': (int(end_lad.get('active_step') or 0) - int(start_lad.get('active_step') or 0)) / max(1, (obs[-1][0] - start_ts) / 60000),
        'pn_plus_1_significant_level_proximity_change': {'start': prox(start_lad, obs[0][3], 'pn_plus_1'), 'end': prox(end_lad, obs[-1][3], 'pn_plus_1')},
        'pn_plus_2_significant_level_proximity_change': {'start': prox(start_lad, obs[0][3], 'pn_plus_2'), 'end': prox(end_lad, obs[-1][3], 'pn_plus_2')},
        'cross_scale_start': obs[0][3].get('features', {}).get('cross_percentage_active_depth'),
        'cross_scale_end': obs[-1][3].get('features', {}).get('cross_percentage_active_depth'),
        'event_ordering': sorted(events, key=lambda e: e['timestamp_ms']),
    }


def _episode_ordering_classification(evolution: Dict[str, Any]) -> str:
    events = evolution.get('event_ordering') or []
    ts = [int(ev.get('timestamp_ms')) for ev in events if isinstance(ev, dict) and ev.get('timestamp_ms') is not None]
    if len(ts) != len(events):
        return 'other_reason'
    if len(ts) >= 2 and any(ts[i] > ts[i + 1] for i in range(len(ts) - 1)):
        return 'true_timestamp_reversal'
    if len(ts) >= 2 and len(set(ts)) < len(ts):
        return 'same_candle_ambiguous'
    return 'definitely_reconstructable'

def _episode_intrabar_order_ambiguous(evolution: Dict[str, Any]) -> bool:
    return _episode_ordering_classification(evolution) == 'same_candle_ambiguous'


class FiboLearnStore:
    DEFAULT_MIN_EPISODES = 30
    DEFAULT_MIN_OOS_EPISODES = 15
    DEFAULT_MIN_PER_SYMBOL_EPISODES = 10

    def __init__(self, path: str | Path, *, use_optimized_layout: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.use_optimized_layout = use_optimized_layout
        self._init()

    def _connect(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL;')
        c.execute('PRAGMA synchronous=NORMAL;')
        return c

    def _init(self):
        with self._connect() as c:
            c.execute('create table if not exists schema_meta(version integer not null, applied_at integer not null)')
            if not c.execute('select 1 from schema_meta where version=?', (SCHEMA_VERSION,)).fetchone():
                c.execute('insert into schema_meta values (?,?)', (SCHEMA_VERSION, int(time.time() * 1000)))
            if self.use_optimized_layout:
                c.execute('create table if not exists market_observations(id integer primary key autoincrement, schema_version integer not null, symbol text not null, timestamp_ms integer not null, market_json text not null, feature_definition_json text not null, price_action_json text not null, significant_levels_versioned_json text not null, source_candles integer not null, created_at integer not null)')
                c.execute('create unique index if not exists idx_market_symbol_ts on market_observations(symbol, timestamp_ms)')
                c.execute('create table if not exists ladder_observations(id integer primary key autoincrement, market_id integer not null, percentage text not null, direction text not null, cycle_id text not null, active_step integer not null, ladder_state_json text not null, created_at integer not null)')
                c.execute('create index if not exists idx_ladder_market on ladder_observations(market_id)')
                c.execute('create index if not exists idx_ladder_key on ladder_observations(percentage, direction, cycle_id, active_step)')
            else:
                c.execute('create table if not exists observations(id integer primary key autoincrement, schema_version integer not null, symbol text not null, timestamp_ms integer not null, cycle_id text, state_vector_json text not null, raw_json text not null, created_at integer not null)')
                c.execute('create index if not exists idx_obs_symbol_time on observations(symbol,timestamp_ms)')
                c.execute('create unique index if not exists idx_obs_symbol_ts_unique on observations(symbol,timestamp_ms)')
            c.execute('create table if not exists outcomes(id integer primary key autoincrement, observation_id integer not null unique, outcome_json text not null, created_at integer not null)')
            c.execute('create table if not exists patterns(id integer primary key autoincrement, name text not null, status text not null, definition_json text not null, metrics_json text not null, created_at integer not null)')
            c.execute('create index if not exists idx_patterns_status on patterns(status)')
            c.execute('create table if not exists backtests(id integer primary key autoincrement, pattern_id integer, symbols_json text not null, result_json text not null, created_at integer not null)')
            c.execute('create table if not exists runtime_state(key text primary key, value_json text not null, updated_at integer not null)')
            c.execute('create table if not exists episodes(id integer primary key autoincrement, episode_key text not null unique, symbol text not null, percentage text not null, direction text not null, cycle_id text not null, active_step integer not null, start_timestamp_ms integer not null, end_timestamp_ms integer not null, terminal_event text not null, observation_count integer not null, raw_observation_ids_json text not null, start_state_json text not null, end_state_json text not null, evolution_json text not null, outcome_json text not null, intrabar_order_ambiguous integer not null default 0, created_at integer not null)')
            c.execute('create index if not exists idx_episodes_symbol_pct_dir on episodes(symbol,percentage,direction)')
            c.execute('create index if not exists idx_episodes_intrabar_ambiguous on episodes(intrabar_order_ambiguous)')
            c.execute('create table if not exists dataset_checkpoints(key text primary key, value_json text not null, updated_at integer not null)')
            self._migrate_intrabar_ambiguity_flag()

    def _migrate_intrabar_ambiguity_flag(self) -> None:
        with self._connect() as c:
            try:
                cols = [r['name'] for r in c.execute('pragma table_info(episodes)').fetchall()]
            except Exception:
                return
            if 'intrabar_order_ambiguous' not in cols:
                try:
                    c.execute('alter table episodes add column intrabar_order_ambiguous integer not null default 0')
                except Exception:
                    pass
            try:
                c.execute('create index if not exists idx_episodes_intrabar_ambiguous on episodes(intrabar_order_ambiguous)')
            except Exception:
                pass
            try:
                rows = c.execute('select id,evolution_json from episodes where ifnull(intrabar_order_ambiguous,0)=0').fetchall()
                for r in rows:
                    evo = json.loads(r['evolution_json']) if r['evolution_json'] else {}
                    c.execute('update episodes set intrabar_order_ambiguous=? where id=?', (1 if _episode_intrabar_order_ambiguous(evo) else 0, int(r['id'])))
            except Exception:
                pass

    def _serialize(self, obj) -> str:
        from fibolearn.backtest.engine import _D as _DD
        def ser(x):
            if isinstance(x, Decimal):
                return str(x)
            if isinstance(x, dict):
                return {k: ser(v) for k, v in x.items()}
            if isinstance(x, (list, tuple)):
                return [ser(v) for v in x]
            return x
        return json.dumps(ser(obj), sort_keys=True)

    def save_observation(self, vec) -> int:
        d = vec.to_dict()
        raw = d.get('raw', {})
        now = int(time.time() * 1000)
        market_dict = dict(d['market'])
        market_dict['domain_events'] = dict(raw.get('domain_events') or {})
        if self.use_optimized_layout:
            with self._connect() as c:
                r = c.execute('select id from market_observations where symbol=? and timestamp_ms=?', (d['symbol'], int(d['timestamp_ms']))).fetchone()
                if r:
                    market_id = int(r['id'])
                    c.execute('update market_observations set market_json=?, feature_definition_json=?, price_action_json=?, significant_levels_versioned_json=?, source_candles=?, created_at=? where id=?',
                              (self._serialize(market_dict), self._serialize(d.get('feature_definition', {})), self._serialize(d.get('features', {}).get('price_action', {})), self._serialize(d.get('features', {}).get('significant_levels_versioned', {})), int(raw.get('source_candles', 0)), now, market_id))
                else:
                    cur = c.execute('insert into market_observations(schema_version,symbol,timestamp_ms,market_json,feature_definition_json,price_action_json,significant_levels_versioned_json,source_candles,created_at) values (?,?,?,?,?,?,?,?,?)',
                                     (SCHEMA_VERSION, d['symbol'], int(d['timestamp_ms']), self._serialize(market_dict), self._serialize(d.get('feature_definition', {})), self._serialize(d.get('features', {}).get('price_action', {})), self._serialize(d.get('features', {}).get('significant_levels_versioned', {})), int(raw.get('source_candles', 0)), now))
                    market_id = int(cur.lastrowid)
                lid = None
                for pct, sides in d['ladders'].items():
                    for side, lad in sides.items():
                        s = dict(lad)
                        cycle_id = str(s.get('cycle_id', ''))
                        active_step = int(s.get('active_step', 0) or 0)
                        cur = c.execute('insert into ladder_observations(market_id,percentage,direction,cycle_id,active_step,ladder_state_json,created_at) values (?,?,?,?,?,?,?)',
                                         (market_id, pct, side, cycle_id, active_step, self._serialize(s), now))
                        if lid is None:
                            lid = int(cur.lastrowid)
                return market_id
        else:
            with self._connect() as c:
                r = c.execute('select id from observations where symbol=? and timestamp_ms=?', (d['symbol'], int(d['timestamp_ms']))).fetchone()
                if r:
                    c.execute('update observations set schema_version=?, cycle_id=?, state_vector_json=?, raw_json=?, created_at=? where id=?',
                              (SCHEMA_VERSION, None, self._serialize(d), self._serialize(raw), now, int(r['id'])))
                    return int(r['id'])
                cur = c.execute('insert into observations(schema_version,symbol,timestamp_ms,cycle_id,state_vector_json,raw_json,created_at) values (?,?,?,?,?,?,?)',
                                 (SCHEMA_VERSION, d['symbol'], int(d['timestamp_ms']), None, self._serialize(d), self._serialize(raw), now))
                return int(cur.lastrowid)

    def get_observation(self, oid: int) -> Dict[str, Any]:
        if not self.use_optimized_layout:
            with self._connect() as c:
                r = c.execute('select * from observations where id=?', (oid,)).fetchone()
            if not r:
                return {}
            d = dict(r); sv = json.loads(d.pop('state_vector_json')); d['raw'] = json.loads(d.pop('raw_json')); d['state_vector'] = sv; d['id'] = d['id']; return d
        with self._connect() as c:
            r = c.execute('select * from market_observations where id=?', (oid,)).fetchone()
            if not r:
                return {}
            market = json.loads(r['market_json'])
            fd = json.loads(r['feature_definition_json'])
            pa = json.loads(r['price_action_json'])
            slv = json.loads(r['significant_levels_versioned_json'])
            domain_events = market.pop('domain_events', {}) or {}
            ladders = {}
            for lr in c.execute('select * from ladder_observations where market_id=? order by id', (oid,)).fetchall():
                ls = json.loads(lr['ladder_state_json'])
                ladders.setdefault(lr['percentage'], {})[lr['direction']] = ls
            return {'id': int(r['id']), 'schema_version': int(r['schema_version']), 'symbol': r['symbol'], 'timestamp_ms': int(r['timestamp_ms']),
                    'state_vector': {'symbol': r['symbol'], 'timestamp_ms': int(r['timestamp_ms']), 'market': market, 'ladders': ladders, 'features': {'price_action': pa, 'significant_levels_versioned': slv}, 'feature_definition': fd, 'raw': {'domain_events': domain_events}},
                    'raw': {'domain_events': domain_events}}

    def latest_observation(self, symbol: str | None = None) -> Dict[str, Any] | None:
        table = 'market_observations' if self.use_optimized_layout else 'observations'
        q = f'select id from {table}'
        args = []
        if symbol:
            q += ' where symbol=?'; args.append(symbol)
        q += ' order by timestamp_ms desc, id desc limit 1'
        with self._connect() as c:
            r = c.execute(q, args).fetchone()
        return self.get_observation(int(r['id'])) if r else None

    def all_observations(self, symbol: str | None = None) -> list[Dict[str, Any]]:
        if not self.use_optimized_layout:
            q = 'select id from observations'
            args = []
            if symbol:
                q += ' where symbol=?'; args.append(symbol)
            q += ' order by timestamp_ms asc, id asc'
            with self._connect() as c:
                ids = [int(r['id']) for r in c.execute(q, args).fetchall()]
            return [self.get_observation(i) for i in ids]
        q = 'select id from market_observations'
        args = []
        if symbol:
            q += ' where symbol=?'; args.append(symbol)
        q += ' order by timestamp_ms asc, id asc'
        with self._connect() as c:
            ids = [int(r['id']) for r in c.execute(q, args).fetchall()]
        return [self.get_observation(i) for i in ids]

    def observation_counts_by_symbol(self) -> Dict[str, int]:
        table = 'market_observations' if self.use_optimized_layout else 'observations'
        with self._connect() as c:
            return {r['symbol']: int(r['n']) for r in c.execute(f'select symbol,count(*) n from {table} group by symbol')}

    def market_observation_count(self, symbol: str) -> int:
        with self._connect() as c:
            r = c.execute('select count(*) n from market_observations where symbol=?', (symbol,)).fetchone()
        return int(r['n']) if r else 0

    def observation_counts_by_percentage(self) -> Dict[str, int]:
        counts = {}
        for obs in self.all_observations():
            for pct, sides in obs['state_vector']['ladders'].items():
                counts[pct] = counts.get(pct, 0) + len(sides)
        return counts

    def save_outcome(self, observation_id: int, outcome) -> None:
        with self._connect() as c:
            c.execute('insert or replace into outcomes(observation_id,outcome_json,created_at) values (?,?,?)', (observation_id, json.dumps(outcome.to_dict(), sort_keys=True), int(time.time() * 1000)))

    def find_similar_setup_rows(self, *, symbol: str | None = None, percentages: tuple[str, ...] | list[str] | None = None, directions: tuple[str, ...] | list[str] | None = None, candidate_limit: int = 5000, result_limit: int | None = None) -> List[Dict[str, Any]]:
        """Bounded selector for Study Setup and similar interactive use cases.

        Filters at the SQL layer so Python never deserializes rows that
        will not be considered. The default candidate_limit is small
        enough to keep memory bounded even on the production 30-day DB.
        """
        if not self.use_optimized_layout:
            raise RuntimeError('find_similar_setup_rows requires optimized layout')
        params: list[Any] = []
        where = []
        if symbol:
            where.append('o.symbol = ?'); params.append(symbol)
        if percentages:
            placeholders = ','.join('?' for _ in percentages)
            where.append(f'l.percentage IN ({placeholders})'); params.extend(percentages)
        if directions:
            placeholders = ','.join('?' for _ in directions)
            where.append(f'l.direction IN ({placeholders})'); params.extend(directions)
        where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''
        cap = int(candidate_limit) if candidate_limit else 5000
        if result_limit:
            cap = min(cap, int(result_limit))
        sql = (
            'select o.id,o.symbol,o.timestamp_ms,l.percentage,l.direction,l.cycle_id,l.active_step, '
            'l.ladder_state_json, oc.outcome_json '
            'from market_observations o join ladder_observations l on l.market_id=o.id '
            'left join outcomes oc on oc.observation_id=l.id '
            f'{where_sql} '
            'order by o.timestamp_ms asc, l.id asc '
            'limit ?'
        )
        params.append(cap)
        out: List[Dict[str, Any]] = []
        with self._connect() as c:
            for r in c.execute(sql, params).fetchall():
                lad = json.loads(r['ladder_state_json'])
                outcome = json.loads(r['outcome_json']) if r['outcome_json'] else {}
                out.append({'observation_id': r['id'], 'symbol': r['symbol'], 'timestamp_ms': r['timestamp_ms'], 'percentage': r['percentage'], 'direction': r['direction'], 'active_step': lad.get('active_step'), 'progressing': lad.get('progressing'), 'normalized_distance_to_next_step': lad.get('normalized_distance_to_next_step'), 'hit': bool(outcome.get('reached_pn_plus_1')), 'outcome': outcome, 'state_vector': lad})
        return out

    def complete_outcomes_for_symbol(self, symbol: str, horizon: int = 20) -> int:
        obs = self.all_observations(symbol); n = 0
        for i, o in enumerate(obs):
            sv = o['state_vector']; future = [x['raw'].get('last_candle') for x in obs[i + 1:i + 1 + horizon] if x.get('raw', {}).get('last_candle')]
            if not future:
                continue
            ladders = sv['ladders']; pct = '0.001' if '0.001' in ladders else next(iter(ladders)); side = 'BUY' if 'BUY' in ladders[pct] else next(iter(ladders[pct]))
            d = ladders[pct][side]
            import types
            obj = types.SimpleNamespace(**{k: (_D(d[k]) if k in {'pn', 'pn_plus_1', 'pn_plus_2', 'pn_minus_1', 'current_price'} and d.get(k) is not None else d.get(k)) for k in d})
            outcome = complete_observation_outcome(obj, future)
            self.save_outcome(o['id'], outcome); n += 1
        return n

    def dataset_rows(self) -> list[Dict[str, Any]]:
        out = []
        if self.use_optimized_layout:
            with self._connect() as c:
                rows = c.execute('select o.id,o.symbol,o.timestamp_ms,l.percentage,l.direction,l.cycle_id,l.active_step,l.ladder_state_json, oc.outcome_json from market_observations o join ladder_observations l on l.market_id=o.id left join outcomes oc on oc.observation_id=l.id order by o.timestamp_ms').fetchall()
            for r in rows:
                lad = json.loads(r['ladder_state_json'])
                outcome = json.loads(r['outcome_json']) if r['outcome_json'] else {}
                out.append({'observation_id': r['id'], 'symbol': r['symbol'], 'timestamp_ms': r['timestamp_ms'], 'percentage': r['percentage'], 'direction': r['direction'], 'active_step': lad.get('active_step'), 'progressing': lad.get('progressing'), 'normalized_distance_to_next_step': lad.get('normalized_distance_to_next_step'), 'hit': bool(outcome.get('reached_pn_plus_1')), 'outcome': outcome, 'state_vector': lad})
        else:
            with self._connect() as c:
                rows = c.execute('select o.id,o.symbol,o.timestamp_ms,o.state_vector_json,oc.outcome_json from observations o left join outcomes oc on oc.observation_id=o.id order by o.timestamp_ms').fetchall()
            for r in rows:
                sv = json.loads(r['state_vector_json']); outcome = json.loads(r['outcome_json']) if r['outcome_json'] else {}
                for pct, sides in sv['ladders'].items():
                    for side, lad in sides.items():
                        out.append({'observation_id': r['id'], 'symbol': r['symbol'], 'timestamp_ms': r['timestamp_ms'], 'percentage': pct, 'direction': side, 'active_step': lad.get('active_step'), 'progressing': lad.get('progressing'), 'normalized_distance_to_next_step': lad.get('normalized_distance_to_next_step'), 'hit': bool(outcome.get('reached_pn_plus_1')), 'outcome': outcome, 'state_vector': sv})
        return out

    def rebuild_episodes(self, symbol: str | None = None) -> int:
        rows = self.all_observations(symbol); groups = {}
        for o in rows:
            sv = o['state_vector']; sid = o['id']; ts = int(sv['timestamp_ms']); sym = sv['symbol']
            for pct, sides in sv.get('ladders', {}).items():
                for side, lad in sides.items():
                    key = f"{sym}|{pct}|{side}|{lad.get('cycle_id')}|{lad.get('active_step')}"
                    g = groups.setdefault(key, {'episode_key': key, 'symbol': sym, 'percentage': pct, 'direction': side, 'cycle_id': str(lad.get('cycle_id')), 'active_step': int(lad.get('active_step') or 0), 'obs': []})
                    g['obs'].append((ts, sid, lad, sv))
        now = int(time.time() * 1000); n = 0
        with self._connect() as c:
            if symbol:
                c.execute('delete from episodes where symbol=?', (symbol,))
            else:
                c.execute('delete from episodes')
            for key, g in groups.items():
                obs = sorted(g['obs'], key=lambda x: x[0])
                start_ts, end_ts = obs[0][0], obs[-1][0]
                start_lad, end_lad = obs[0][2], obs[-1][2]
                start_sv, end_sv = obs[0][3], obs[-1][3]
                future = []
                for h in groups.values():
                    if h is g or h['symbol'] != g['symbol'] or h['percentage'] != g['percentage'] or h['direction'] != g['direction']:
                        continue
                    hobs = sorted(h['obs'], key=lambda x: x[0])
                    if hobs and hobs[0][0] > end_ts:
                        future.append((hobs[0][0], h))
                terminal = 'censored'
                terminal_ts = end_ts
                if future:
                    terminal_ts, nxt = min(future, key=lambda x: x[0])
                    evs = (nxt['obs'][0][3].get('raw', {}).get('domain_events', {}) or {}).get(f"{g['percentage']}:{g['direction']}", [])
                    if 'progression' in evs and ('tp_hit' not in evs or evs.index('progression') < evs.index('tp_hit')):
                        terminal = 'pn_plus_1_before_tp'
                    elif 'tp_hit' in evs or 'cycle_closed' in evs or int(nxt['cycle_id']) != int(g['cycle_id']):
                        terminal = 'tp_before_pn_plus_1'
                    elif int(nxt['active_step']) > int(g['active_step']):
                        terminal = 'pn_plus_1_before_tp'
                    elif int(nxt['active_step']) < int(g['active_step']):
                        terminal = 'tp_before_pn_plus_1'
                    else:
                        terminal = 'other_terminal'
                prices = [_D(x[3].get('market', {}).get('price') or x[2].get('current_price')) for x in obs]
                cur0 = _D(start_lad.get('current_price'))
                if g['direction'] == 'BUY':
                    mfe = max(prices) - cur0; mae = min(prices) - cur0
                else:
                    mfe = cur0 - min(prices); mae = cur0 - max(prices)
                evolution = _episode_evolution(obs)
                outcome = {'terminal_event': terminal, 'terminal_timestamp_ms': terminal_ts, 'duration_ms': end_ts - start_ts, 'mfe': str(mfe), 'mae': str(mae), 'event_ordering': evolution.get('event_ordering', [])}
                c.execute('insert or replace into episodes(episode_key,symbol,percentage,direction,cycle_id,active_step,start_timestamp_ms,end_timestamp_ms,terminal_event,observation_count,raw_observation_ids_json,start_state_json,end_state_json,evolution_json,outcome_json,created_at) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          (key, g['symbol'], g['percentage'], g['direction'], g['cycle_id'], g['active_step'], start_ts, end_ts, terminal, len(obs), self._serialize([x[1] for x in obs]), self._serialize(start_sv), self._serialize(end_sv), self._serialize(evolution), self._serialize(outcome), now))
                n += 1
        return n

    def episode_rows(self, symbol: str | None = None, *, include_ambiguous: bool = True) -> list[Dict[str, Any]]:
        q = 'select * from episodes'
        args = []
        if symbol:
            q += ' where symbol=?'; args.append(symbol)
        q += ' order by start_timestamp_ms, id'
        with self._connect() as c:
            rows = c.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ('raw_observation_ids_json', 'start_state_json', 'end_state_json', 'evolution_json', 'outcome_json'):
                if k.endswith('_json') and k in d:
                    d[k[:-5]] = json.loads(d.pop(k))
            d['intrabar_order_ambiguous'] = bool(int(d.pop('intrabar_order_ambiguous', 0) or 0))
            d['intrabar_order_classification'] = _episode_ordering_classification(d.get('evolution') or {})
            if include_ambiguous or not d['intrabar_order_ambiguous']:
                out.append(d)
        return out

    def ambiguous_episode_rows(self, symbol: str | None = None) -> list[Dict[str, Any]]:
        return [e for e in self.episode_rows(symbol, include_ambiguous=True) if e.get('intrabar_order_ambiguous')]

    def episode_baselines(self, *, include_ambiguous: bool = False) -> Dict[Tuple, Dict[str, Any]]:
        obs_counts = {}
        for r in self.dataset_rows():
            key = (r['symbol'], r['percentage'], r['direction']); obs_counts[key] = obs_counts.get(key, 0) + 1
        out = {}
        for e in self.episode_rows(include_ambiguous=include_ambiguous):
            key = (e['symbol'], e['percentage'], e['direction'])
            b = out.setdefault(key, {'symbol': e['symbol'], 'percentage': e['percentage'], 'direction': e['direction'], 'episodes': 0, 'completed_episodes': 0, 'raw_observations': obs_counts.get(key, 0), 'pn_plus_1_before_tp': 0, 'tp_before_pn_plus_1': 0, 'other_censored': 0, 'ambiguous_episodes': 0, 'durations_ms': [], 'mfes': [], 'maes': []})
            b['episodes'] += 1
            if e.get('intrabar_order_ambiguous'):
                b['ambiguous_episodes'] += 1
            term = e['terminal_event']
            if term in ('pn_plus_1_before_tp', 'tp_before_pn_plus_1'):
                b['completed_episodes'] += 1
            if term == 'pn_plus_1_before_tp':
                b['pn_plus_1_before_tp'] += 1
            elif term == 'tp_before_pn_plus_1':
                b['tp_before_pn_plus_1'] += 1
            else:
                b['other_censored'] += 1
            oc = e.get('outcome') or {}; b['durations_ms'].append(int(oc.get('duration_ms') or 0)); b['mfes'].append(float(oc.get('mfe') or 0)); b['maes'].append(float(oc.get('mae') or 0))
        for b in out.values():
            n = max(1, b['episodes']); b['episode_level_progression_rate'] = b['pn_plus_1_before_tp'] / n; b['tp_before_progression_rate'] = b['tp_before_pn_plus_1'] / n; b['censored_rate'] = b['other_censored'] / n
            rows = [r for r in self.dataset_rows() if (r['symbol'], r['percentage'], r['direction']) == (b['symbol'], b['percentage'], b['direction'])]
            b['observation_level_success_rate'] = (sum(1 for r in rows if r.get('hit')) / len(rows)) if rows else None
            b['median_duration_ms'] = statistics.median(b['durations_ms']) if b['durations_ms'] else None
            b['median_mfe'] = statistics.median(b['mfes']) if b['mfes'] else None
            b['median_mae'] = statistics.median(b['maes']) if b['maes'] else None
        return out

    def time_dependent_baselines(self, minutes=(1, 3, 5, 10, 15, 30, 60), *, include_ambiguous: bool = False) -> Dict[Tuple, Dict[int, Dict[str, Any]]]:
        out = {}
        for e in self.episode_rows(include_ambiguous=include_ambiguous):
            key = (e['symbol'], e['percentage'], e['direction']); out.setdefault(key, {})
            duration_min = (int(e['end_timestamp_ms']) - int(e['start_timestamp_ms'])) / 60000
            for m in minutes:
                d = out[key].setdefault(int(m), {'eligible_episodes': 0, 'pn_plus_1_before_tp': 0, 'tp_before_pn_plus_1': 0, 'other_censored': 0, 'ambiguous_episodes': 0, 'probability_pn_plus_1_before_tp': None})
                if duration_min >= m:
                    d['eligible_episodes'] += 1
                    if e.get('intrabar_order_ambiguous'):
                        d['ambiguous_episodes'] += 1
                    if e['terminal_event'] == 'pn_plus_1_before_tp':
                        d['pn_plus_1_before_tp'] += 1
                    elif e['terminal_event'] == 'tp_before_pn_plus_1':
                        d['tp_before_pn_plus_1'] += 1
                    else:
                        d['other_censored'] += 1
        for mset in out.values():
            for d in mset.values():
                effective = max(1, d['eligible_episodes'] - d.get('ambiguous_episodes', 0))
                if effective:
                    d['probability_pn_plus_1_before_tp'] = d['pn_plus_1_before_tp'] / effective
        return out

    def dataset_status_by_symbol(self) -> Dict[str, Dict[str, Any]]:
        out = {}
        table = 'market_observations' if self.use_optimized_layout else 'observations'
        with self._connect() as c:
            for r in c.execute(f'select symbol,count(*) candles,min(timestamp_ms) first_ts,max(timestamp_ms) last_ts from {table} group by symbol'):
                out[r['symbol']] = {'symbol': r['symbol'], 'candles': int(r['candles']), 'observations': int(r['candles']) * 8, 'first_timestamp_ms': r['first_ts'], 'last_timestamp_ms': r['last_ts'], 'episodes': 0, 'cycles': 0}
            for r in c.execute('select symbol,count(*) episodes,count(distinct cycle_id) cycles from episodes group by symbol'):
                out.setdefault(r['symbol'], {'symbol': r['symbol']}).update({'episodes': int(r['episodes']), 'cycles': int(r['cycles'])})
        return out

    def data_coverage_matrix(self, symbols=('BTC', 'ETH', 'SOL', 'ZEC', 'PAXG'), percentages=('1', '0.1', '0.01', '0.001'), directions=('BUY', 'SELL'), *, min_completed_episodes: int = DEFAULT_MIN_EPISODES) -> Dict[str, Any]:
        baselines = self.episode_baselines(include_ambiguous=True)
        cells: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for sym in symbols:
            for pct in percentages:
                for side in directions:
                    b = baselines.get((sym, pct, side), {})
                    ambiguous = int(b.get('ambiguous_episodes', 0))
                    completed = max(0, int(b.get('completed_episodes', 0)) - ambiguous)
                    completed = completed if completed >= 0 else 0
                    cells[(sym, pct, side)] = {
                        'symbol': sym, 'percentage': pct, 'direction': side,
                        'episodes': int(b.get('episodes', 0)),
                        'ambiguous_episodes': int(b.get('ambiguous_episodes', 0)),
                        'completed_episodes': completed,
                        'raw_observations': int(b.get('raw_observations', 0)),
                        'pn_plus_1_before_tp': int(b.get('pn_plus_1_before_tp', 0)),
                        'tp_before_pn_plus_1': int(b.get('tp_before_pn_plus_1', 0)),
                        'censored': int(b.get('other_censored', 0)),
                        'median_duration_ms': b.get('median_duration_ms'),
                        'sufficient': completed >= min_completed_episodes,
                    }
        return {'cells': cells, 'min_completed_episodes': min_completed_episodes, 'ambiguous_episodes_total': sum(v['ambiguous_episodes'] for v in cells.values())}

    def research_gate(self, symbol: str, percentage: str, direction: str, *, min_episodes: int = DEFAULT_MIN_EPISODES, min_oos_episodes: int = DEFAULT_MIN_OOS_EPISODES, min_per_symbol_episodes: int = DEFAULT_MIN_PER_SYMBOL_EPISODES) -> Dict[str, Any]:
        baselines = self.episode_baselines()
        b = baselines.get((symbol, percentage, direction), {})
        completed = int(b.get('completed_episodes', 0))
        raw = int(b.get('raw_observations', 0))
        oos_est = max(1, int(completed * 0.3)) if completed else 0
        per_symbol = int(b.get('episodes', 0))
        reasons = []
        if completed < min_episodes:
            reasons.append(f'completed_episodes {completed} < min_episodes {min_episodes}')
        if oos_est < min_oos_episodes:
            reasons.append(f'~oos_episodes {oos_est} < min_oos_episodes {min_oos_episodes}')
        if per_symbol < min_per_symbol_episodes:
            reasons.append(f'episodes {per_symbol} < min_per_symbol_episodes {min_per_symbol_episodes}')
        return {
            'symbol': symbol, 'percentage': percentage, 'direction': direction,
            'completed_episodes': completed, 'raw_observations': raw,
            'approx_oos_episodes': oos_est, 'min_episodes': min_episodes,
            'min_oos_episodes': min_oos_episodes, 'min_per_symbol_episodes': min_per_symbol_episodes,
            'permitted': not reasons,
            'reasons': reasons,
        }

    def set_checkpoint(self, key: str, value: Dict[str, Any]) -> None:
        with self._connect() as c:
            c.execute('insert or replace into dataset_checkpoints(key,value_json,updated_at) values (?,?,?)', (key, json.dumps(value, sort_keys=True), int(time.time() * 1000)))

    def get_checkpoint(self, key: str) -> Dict[str, Any] | None:
        with self._connect() as c:
            r = c.execute('select value_json from dataset_checkpoints where key=?', (key,)).fetchone()
        return json.loads(r['value_json']) if r else None

    def set_running_state(self, running: bool) -> None:
        with self._connect() as c:
            c.execute('insert or replace into runtime_state(key,value_json,updated_at) values (?,?,?)', ('running', json.dumps(bool(running)), int(time.time() * 1000)))

    def get_running_state(self) -> bool:
        with self._connect() as c:
            r = c.execute('select value_json from runtime_state where key=?', ('running',)).fetchone()
        return bool(json.loads(r['value_json'])) if r else False

    # ------------------------------------------------------------------
    # Streaming / incremental episode builder
    # ------------------------------------------------------------------

    def _stream_observations(self, symbol: str, percentages: List[str], directions: List[str], page_size: int = 1000, *, after_ms: int | None = None, after_id: int | None = None):
        """Yield rows of (market_id, timestamp_ms, ladder_id, percentage, direction, cycle_id, active_step, ladder_state_json, market_json).

        Strictly ascending by (timestamp_ms, ladder_id). Uses keyset paging so
        memory is bounded to page_size ladder rows.
        """
        if not percentages or not directions:
            return
        pct_ph = ','.join('?' for _ in percentages)
        dir_ph = ','.join('?' for _ in directions)
        base_params: list[Any] = [symbol] + list(percentages) + list(directions)
        def build_query(with_cursor: bool) -> tuple[str, list]:
            if with_cursor:
                ts_filter = ' and (o.timestamp_ms > ? or (o.timestamp_ms = ? and l.id > ?))'
            else:
                ts_filter = ''
            q = (
                'select o.id as market_id, o.timestamp_ms, l.id as ladder_id, l.percentage, l.direction, '
                'l.cycle_id, l.active_step, l.ladder_state_json, o.market_json, o.price_action_json, '
                'o.feature_definition_json, o.significant_levels_versioned_json '
                'from market_observations o join ladder_observations l on l.market_id=o.id '
                f'where o.symbol=? and l.percentage in ({pct_ph}) and l.direction in ({dir_ph}){ts_filter} '
                'order by o.timestamp_ms asc, l.id asc limit ?'
            )
            return q, list(base_params)
        # Initial query matches caller's intent (with or without cursor).
        cursor_ms = after_ms
        cursor_lid = after_id
        with_cursor = cursor_ms is not None
        with self._connect() as c:
            while True:
                query, page_params = build_query(with_cursor)
                if with_cursor and cursor_ms is not None:
                    page_params += [int(cursor_ms), int(cursor_ms), int(cursor_lid or 0)]
                page_params += [int(page_size)]
                rows = c.execute(query, page_params).fetchall()
                if not rows:
                    break
                for row in rows:
                    yield row
                if len(rows) < page_size:
                    break
                # Subsequent pages always use a cursor filter.
                with_cursor = True
                cursor_ms = int(rows[-1]['timestamp_ms'])
                cursor_lid = int(rows[-1]['ladder_id'])

    @staticmethod
    def _classify_terminal_from_events(evs: List[str], next_active_step: int, next_cycle_id: str, prev_active_step: int) -> str:
        if evs:
            if 'progression' in evs and ('tp_hit' not in evs or evs.index('progression') < evs.index('tp_hit')):
                return 'pn_plus_1_before_tp'
            if 'tp_hit' in evs or 'cycle_closed' in evs:
                return 'tp_before_pn_plus_1'
            try:
                if int(next_cycle_id) != int(prev_active_step) and next_active_step == prev_active_step:
                    return 'tp_before_pn_plus_1'
            except Exception:
                pass
        if next_active_step > prev_active_step:
            return 'pn_plus_1_before_tp'
        if next_active_step < prev_active_step:
            return 'tp_before_pn_plus_1'
        return 'other_terminal'

    def rebuild_episodes_streaming(self, symbol: str, *, page_size: int = 2000, checkpoint: bool = True, limit_families: List[Tuple[str, str]] | None = None, on_progress=None) -> int:
        """Memory-bounded, incremental, resumable episode builder.

        Reads ladder rows in (timestamp_ms, ladder_id) order and groups them
        into episodes keyed by (symbol, percentage, direction, cycle_id,
        active_step). An episode ends when the next observation in the
        same family has a different (cycle_id, active_step) pair, OR a
        domain_events-driven transition fires (tp_hit, progression,
        cycle_closed).

        This implementation does NOT replay the GoldenFibo engine. It
        trusts the persisted ladder_state, market_json.domain_events and
        cycle_id/active_step transitions to identify episode boundaries
        exactly. This is the correct semantic for episodes because the
        production build already drove the engine deterministically; replay
        would only duplicate work and could diverge if the engine has
        changed since the rows were written.
        """
        if not self.use_optimized_layout:
            raise RuntimeError('streaming rebuild requires optimized layout')
        families_all = [(str(p), d) for p in DEFAULT_PERCENTAGES for d in ('BUY', 'SELL')]
        if limit_families:
            target_families = [(p, d) for (p, d) in families_all if (p, d) in set(limit_families)]
        else:
            target_families = families_all

        cp_symbol_key = f'rebuild:{symbol}:done'
        cp_done = set()
        last_ts_per_family: Dict[Tuple[str, str], Tuple[int, int]] = {}
        if checkpoint:
            done = self.get_checkpoint(cp_symbol_key) or {}
            cp_done = set(tuple(x) for x in done.get('families_done', []))
            cursor_map = done.get('cursors', {})
            for fam, cur in cursor_map.items():
                last_ts_per_family[tuple(fam)] = (int(cur['ts_ms']), int(cur['ladder_id']))

        families = [f for f in target_families if f not in cp_done]

        open_state: Dict[Tuple[str, str], Dict[str, Any]] = {}
        now = int(time.time() * 1000)
        episodes_emitted = 0
        rows_processed = 0
        cursor: Dict[Tuple[str, str], Tuple[int, int]] = dict(last_ts_per_family)
        total_rows = 0
        ladder_processed = 0

        def _term_from_events(evs: List[str], next_active_step: int, prev_active_step: int) -> str:
            if evs:
                if 'progression' in evs and ('tp_hit' not in evs or evs.index('progression') < evs.index('tp_hit')):
                    return 'pn_plus_1_before_tp'
                if 'tp_hit' in evs or 'cycle_closed' in evs or 'cycle_reset' in evs:
                    return 'tp_before_pn_plus_1'
            if next_active_step > prev_active_step:
                return 'pn_plus_1_before_tp'
            if next_active_step < prev_active_step:
                return 'tp_before_pn_plus_1'
            return 'other_terminal'

        with self._connect() as c:
            for (pct, side) in families:
                c.execute('delete from episodes where symbol=? and percentage=? and direction=?', (symbol, pct, side))

            for pct in sorted({p for p, _ in families}):
                sides = [d for (p, d) in families if p == pct]
                if not sides:
                    continue
                start_ts_ms = 0
                start_lid = 0
                fam_cursors = [cursor.get((pct, d)) for d in sides if cursor.get((pct, d))]
                if fam_cursors:
                    cp_min = min(fam_cursors, key=lambda c: (c[0], c[1]))
                    start_ts_ms, start_lid = cp_min

                with self._connect() as c2:
                    cur_sql_params = [symbol, pct] + sides + [start_ts_ms, start_ts_ms, start_lid, int(page_size)]
                    sql = (
                        'select o.id as mid, o.timestamp_ms ts, l.id as lid, l.percentage, l.direction, '
                        'l.cycle_id, l.active_step, l.ladder_state_json, o.market_json '
                        'from market_observations o join ladder_observations l on l.market_id=o.id '
                        'where o.symbol=? and l.percentage=? and l.direction in (' + ','.join('?' for _ in sides) + ') '
                        'and (o.timestamp_ms > ? or (o.timestamp_ms = ? and l.id > ?)) '
                        'order by o.timestamp_ms asc, l.id asc limit ?'
                    )
                    cur = c2.execute(sql, cur_sql_params)
                    pending_params = list(cur_sql_params)
                    done_iter = False
                    while True:
                        rows = cur.fetchmany(page_size)
                        if not rows:
                            break
                        done_iter = True
                        for r in rows:
                            d = dict(r)
                            total_rows += 1
                            ladder_processed += 1
                            mid = int(d['mid'])
                            ts = int(d['ts'])
                            lid = int(d['lid'])
                            percentage = d['percentage']
                            direction = d['direction']
                            cycle_id = str(d['cycle_id'])
                            active_step = int(d['active_step'] or 0)
                            family = (percentage, direction)
                            ladder_state_s = d['ladder_state_json']
                            market_state_s = d['market_json']
                            ladder_state = json.loads(ladder_state_s) if ladder_state_s else {}
                            market_state = json.loads(market_state_s) if market_state_s else {}
                            domain_events = market_state.pop('domain_events', {}) or {}
                            events_list = (domain_events.get(f"{percentage}:{direction}", []) or [])

                            st = open_state.get(family)
                            if st is None or st['cycle_id'] != cycle_id or st['active_step'] != active_step:
                                if st is not None:
                                    terminal = _term_from_events(events_list, active_step, st['active_step'])
                                    self._persist_episode(c, symbol, st, terminal, ts, now, family)
                                    episodes_emitted += 1
                                cur0_v = ladder_state.get('current_price')
                                cur0 = _D(cur0_v) if cur0_v is not None else None
                                pn_v = ladder_state.get('pn')
                                pn = _D(pn_v) if pn_v is not None else None
                                open_state[family] = {
                                    'cycle_id': cycle_id, 'active_step': active_step,
                                    'percentage': family[0], 'direction': family[1],
                                    'start_timestamp_ms': ts, 'last_timestamp_ms': ts,
                                    'start_lad': ladder_state, 'end_lad': ladder_state,
                                    'start_sv': {'market': market_state, 'features': {}, 'feature_definition': {}, 'raw': {'domain_events': domain_events}},
                                    'end_sv': {'market': market_state, 'features': {}, 'feature_definition': {}, 'raw': {'domain_events': domain_events}},
                                    'pn': _D(pn_v) if pn_v is not None else None,
                                    'vwap_crossed': False, 'vwap_cross_ts': None,
                                    'poc_crossed': False, 'poc_cross_ts': None,
                                    'max_pn_retest_count': int(ladder_state.get('pn_retest_count') or 0),
                                    'hh_count': 0, 'lh_count': 0,
                                    'start_active_step': active_step,
                                    'last_price': cur0 if cur0 is not None else _D(0),
                                    'max_mfe': _D(0), 'min_mae': _D(0),
                                    'price_count': 0, 'event_ordering': [],
                                    'observation_ids': [mid],
                                    '_vwap_prev': _D(market_state['vwap']) if market_state.get('vwap') is not None else None,
                                    '_poc_prev': _D(market_state['poc']) if market_state.get('poc') is not None else None,
                                }
                            else:
                                prev_price = st['last_price']
                                new_price = _D(ladder_state.get('current_price'))
                                if new_price > prev_price:
                                    st['hh_count'] += 1
                                elif new_price < prev_price:
                                    st['lh_count'] += 1
                                st['last_price'] = new_price
                                st['last_timestamp_ms'] = ts
                                st['end_lad'] = ladder_state
                                st['max_pn_retest_count'] = max(st['max_pn_retest_count'], int(ladder_state.get('pn_retest_count') or 0))
                                if st['pn'] is not None:
                                    if market_state.get('vwap') is not None:
                                        vw_raw = market_state['vwap']
                                        vw = _D(vw_raw) if not isinstance(vw_raw, (int, float)) else _D(str(vw_raw))
                                        if not st['vwap_crossed']:
                                            prev_vw = st.get('_vwap_prev')
                                            if prev_vw is not None:
                                                cur_sign = 1 if vw >= st['pn'] else -1
                                                prev_sign = 1 if prev_vw >= st['pn'] else -1
                                                if cur_sign != prev_sign and (vw - st['pn']) * (prev_vw - st['pn']) <= 0:
                                                    st['vwap_crossed'] = True
                                                    st['vwap_cross_ts'] = ts
                                                    st['event_ordering'].append({'event': 'vwap_crossed_pn', 'timestamp_ms': ts, 'elapsed_ms': ts - st['start_timestamp_ms']})
                                        st['_vwap_prev'] = vw
                                    if market_state.get('poc') is not None:
                                        pc_raw = market_state['poc']
                                        pc = _D(pc_raw) if not isinstance(pc_raw, (int, float)) else _D(str(pc_raw))
                                        if not st['poc_crossed']:
                                            prev_pc = st.get('_poc_prev')
                                            if prev_pc is not None:
                                                cur_sign = 1 if pc >= st['pn'] else -1
                                                prev_sign = 1 if prev_pc >= st['pn'] else -1
                                                if cur_sign != prev_sign and (pc - st['pn']) * (prev_pc - st['pn']) <= 0:
                                                    st['poc_crossed'] = True
                                                    st['poc_cross_ts'] = ts
                                                    st['event_ordering'].append({'event': 'poc_crossed_pn', 'timestamp_ms': ts, 'elapsed_ms': ts - st['start_timestamp_ms']})
                                        st['_poc_prev'] = pc
                                cur0 = _D(st['start_lad']['current_price'])
                                if st['direction'] == 'BUY':
                                    hi = new_price - cur0; lo = new_price - cur0
                                else:
                                    hi = cur0 - new_price; lo = cur0 - new_price
                                if hi > st['max_mfe']:
                                    st['max_mfe'] = hi
                                if lo < st['min_mae']:
                                    st['min_mae'] = lo
                                st['observation_ids'].append(mid)
                                st['price_count'] += 1
                            cursor[family] = (ts, lid)
                            # Memory-hygiene: drop reference after each row
                            del d
                        # Advance keyset cursor using the last row of this page.
                        last_row = rows[-1]
                        last_ts = int(last_row['ts']); last_lid = int(last_row['lid'])
                        cur.close()
                        cur = c2.execute(sql, [symbol, pct] + sides + [last_ts, last_ts, last_lid, int(page_size)])
                # Persist and clear the families handled in this percentage.
                for family in [(pct, d) for d in sides]:
                    st = open_state.pop(family, None)
                    if st is not None:
                        self._persist_episode(c, symbol, st, 'censored', int(st['last_timestamp_ms']), now, family)
                        episodes_emitted += 1

            # Persist any families left over by early exit or partial runs.
            for family, st in list(open_state.items()):
                if st is not None:
                    self._persist_episode(c, symbol, st, 'censored', int(st['last_timestamp_ms']), now, family)
                    episodes_emitted += 1
                open_state.pop(family, None)

        if checkpoint:
            done = self.get_checkpoint(cp_symbol_key) or {'families_done': [], 'cursors': {}}
            new_done = set(tuple(x) for x in done.get('families_done', [])) | set(families)
            cursors = done.get('cursors', {})
            for fam, (ts, lid) in cursor.items():
                cursors[f"{fam[0]}|{fam[1]}"] = {'ts_ms': int(ts), 'ladder_id': int(lid)}
            self.set_checkpoint(cp_symbol_key, {'families_done': sorted([list(x) for x in new_done]), 'cursors': cursors, 'last_episodes_emitted': episodes_emitted, 'last_ladder_rows_processed': ladder_processed, 'finished': True, 'finished_at_ms': now})
        if on_progress:
            on_progress({'episodes': episodes_emitted, 'ladder_rows': ladder_processed, 'market_rows': total_rows, 'families_processed': len(families)})
        return episodes_emitted

    def _persist_episode(self, c, symbol: str, st: Dict[str, Any], terminal: str, terminal_ts_ms: int, now_ms: int, fam: Tuple[str, str]) -> None:
        start_ts = st['start_timestamp_ms']
        end_ts = st['last_timestamp_ms']
        duration_ms = end_ts - start_ts
        mfe = _D(st['max_mfe'])
        mae = _D(st['min_mae'])
        start_lad = st['start_lad']
        end_lad = st['end_lad']
        evolution = {
            'vwap_crossed_pn_since_start': st['vwap_crossed'],
            'time_to_vwap_cross_ms': (st['vwap_cross_ts'] - start_ts if st['vwap_crossed'] and st['vwap_cross_ts'] else None),
            'poc_crossed_pn_since_start': st['poc_crossed'],
            'time_to_poc_cross_ms': (st['poc_cross_ts'] - start_ts if st['poc_crossed'] and st['poc_cross_ts'] else None),
            'pn_retest_count_since_activation': st['max_pn_retest_count'],
            'time_since_last_pn_touch_ms': int(end_lad.get('time_since_active_step_changed_ms') or 0),
            'higher_high_count': st['hh_count'],
            'lower_high_count': st['lh_count'],
            'progression_velocity': (int(end_lad.get('active_step') or 0) - int(start_lad.get('active_step') or 0)) / max(1, duration_ms / 60000),
            'pn_plus_1_significant_level_proximity_change': {'start': self._prox(start_lad, st['start_sv'], 'pn_plus_1'), 'end': self._prox(end_lad, st['end_sv'], 'pn_plus_1')},
            'pn_plus_2_significant_level_proximity_change': {'start': self._prox(start_lad, st['start_sv'], 'pn_plus_2'), 'end': self._prox(end_lad, st['end_sv'], 'pn_plus_2')},
            'cross_scale_start': st['start_sv'].get('features', {}).get('cross_percentage_active_depth'),
            'cross_scale_end': st['end_sv'].get('features', {}).get('cross_percentage_active_depth'),
            'event_ordering': sorted(st['event_ordering'], key=lambda e: e['timestamp_ms']),
        }
        outcome = {'terminal_event': terminal, 'terminal_timestamp_ms': terminal_ts_ms, 'duration_ms': duration_ms, 'mfe': str(mfe), 'mae': str(mae), 'event_ordering': evolution.get('event_ordering', [])}
        pct, side = fam
        cycle_id = st['cycle_id']
        active_step = st['active_step']
        obs_ids = st['observation_ids']
        episode_key = f"{symbol}|{pct}|{side}|{cycle_id}|{active_step}"
        intrabar_ambiguous = _episode_intrabar_order_ambiguous(evolution)
        c.execute(
            'insert or replace into episodes(episode_key,symbol,percentage,direction,cycle_id,active_step,start_timestamp_ms,end_timestamp_ms,terminal_event,observation_count,raw_observation_ids_json,start_state_json,end_state_json,evolution_json,outcome_json,intrabar_order_ambiguous,created_at) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                episode_key, symbol, pct, side, cycle_id, active_step,
                start_ts, end_ts, terminal, len(obs_ids), self._serialize(obs_ids),
                self._serialize(st['start_sv']), self._serialize(st['end_sv']),
                self._serialize(evolution), self._serialize(outcome), int(intrabar_ambiguous), now_ms,
            ),
        )

    @staticmethod
    def _prox(lad: Dict[str, Any], sv: Dict[str, Any], target: str) -> str | None:
        levels = sv.get('features', {}).get('significant_levels', {})
        vals = []
        for v in levels.values():
            if v is None:
                continue
            try:
                vals.append(abs(_D(lad.get(target)) - _D(v)))
            except Exception:
                continue
        return str(min(vals)) if vals else None

