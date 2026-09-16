from __future__ import annotations
import json, sqlite3, time
from pathlib import Path
from typing import Any, Dict, List
from fibolearn import SCHEMA_VERSION
from fibolearn.labels.outcomes import complete_observation_outcome

class FiboLearnStore:
    def __init__(self, path: str | Path):
        self.path=Path(path); self.path.parent.mkdir(parents=True, exist_ok=True); self._init()
    def _connect(self):
        c=sqlite3.connect(self.path); c.row_factory=sqlite3.Row; return c
    def _init(self):
        with self._connect() as c:
            c.execute('create table if not exists schema_meta(version integer not null, applied_at integer not null)')
            if not c.execute('select 1 from schema_meta where version=?',(SCHEMA_VERSION,)).fetchone(): c.execute('insert into schema_meta values (?,?)',(SCHEMA_VERSION,int(time.time()*1000)))
            c.execute('create table if not exists observations(id integer primary key autoincrement, schema_version integer not null, symbol text not null, timestamp_ms integer not null, cycle_id text, state_vector_json text not null, raw_json text not null, created_at integer not null)')
            c.execute('create index if not exists idx_obs_symbol_time on observations(symbol,timestamp_ms)')
            c.execute('create table if not exists outcomes(id integer primary key autoincrement, observation_id integer not null unique, outcome_json text not null, created_at integer not null)')
            c.execute('create table if not exists patterns(id integer primary key autoincrement, name text not null, status text not null, definition_json text not null, metrics_json text not null, created_at integer not null)')
            c.execute('create index if not exists idx_patterns_status on patterns(status)')
            c.execute('create table if not exists backtests(id integer primary key autoincrement, pattern_id integer, symbols_json text not null, result_json text not null, created_at integer not null)')
            c.execute('create table if not exists runtime_state(key text primary key, value_json text not null, updated_at integer not null)')
    def save_observation(self, vec) -> int:
        d=vec.to_dict(); raw=d.get('raw',{})
        with self._connect() as c:
            cur=c.execute('insert into observations(schema_version,symbol,timestamp_ms,cycle_id,state_vector_json,raw_json,created_at) values (?,?,?,?,?,?,?)',(SCHEMA_VERSION,d['symbol'],int(d['timestamp_ms']),None,json.dumps(d,sort_keys=True),json.dumps(raw,sort_keys=True),int(time.time()*1000)))
            return int(cur.lastrowid)
    def get_observation(self, oid:int)->Dict[str,Any]:
        with self._connect() as c: r=c.execute('select * from observations where id=?',(oid,)).fetchone()
        d=dict(r); d['state_vector']=json.loads(d.pop('state_vector_json')); d['raw']=json.loads(d.pop('raw_json')); return d
    def latest_observation(self, symbol: str | None=None) -> Dict[str,Any] | None:
        q='select id from observations'; args=[]
        if symbol: q+=' where symbol=?'; args.append(symbol)
        q+=' order by timestamp_ms desc, id desc limit 1'
        with self._connect() as c: r=c.execute(q,args).fetchone()
        return self.get_observation(int(r['id'])) if r else None
    def all_observations(self, symbol: str | None=None) -> list[Dict[str,Any]]:
        q='select id from observations'; args=[]
        if symbol: q+=' where symbol=?'; args.append(symbol)
        q+=' order by timestamp_ms asc, id asc'
        with self._connect() as c: ids=[int(r['id']) for r in c.execute(q,args).fetchall()]
        return [self.get_observation(i) for i in ids]
    def observation_counts_by_symbol(self)->Dict[str,int]:
        with self._connect() as c: return {r['symbol']: int(r['n']) for r in c.execute('select symbol,count(*) n from observations group by symbol')}
    def observation_counts_by_percentage(self)->Dict[str,int]:
        counts={}
        for obs in self.all_observations():
            for pct,sides in obs['state_vector']['ladders'].items(): counts[pct]=counts.get(pct,0)+len(sides)
        return counts
    def save_outcome(self, observation_id:int, outcome)->None:
        with self._connect() as c: c.execute('insert or replace into outcomes(observation_id,outcome_json,created_at) values (?,?,?)',(observation_id,json.dumps(outcome.to_dict(),sort_keys=True),int(time.time()*1000)))
    def complete_outcomes_for_symbol(self, symbol: str, horizon: int=20)->int:
        obs=self.all_observations(symbol); n=0
        for i,o in enumerate(obs):
            sv=o['state_vector']; future=[x['raw']['last_candle'] for x in obs[i+1:i+1+horizon] if x.get('raw',{}).get('last_candle')]
            if not future: continue
            # Complete target 0.001 BUY if present, else first ladder.
            ladders=sv['ladders']; pct='0.001' if '0.001' in ladders else next(iter(ladders)); side='BUY' if 'BUY' in ladders[pct] else next(iter(ladders[pct]))
            from fibolearn.collector.state_adapter import LadderObservation
            d=ladders[pct][side]; dec={k: d.get(k) for k in d}
            # reconstruct via minimal object without dataclass strictness
            import types
            obj=types.SimpleNamespace(**{k: (__import__('decimal').Decimal(str(v)) if k in {'pn','pn_plus_1','pn_plus_2','pn_minus_1','current_price'} and v is not None else v) for k,v in d.items()})
            outcome=complete_observation_outcome(obj, future)
            self.save_outcome(o['id'], outcome); n+=1
        return n
    def dataset_rows(self)->list[Dict[str,Any]]:
        out=[]
        with self._connect() as c:
            rows=c.execute('select o.id,o.symbol,o.timestamp_ms,o.state_vector_json,oc.outcome_json from observations o left join outcomes oc on oc.observation_id=o.id order by o.timestamp_ms').fetchall()
        for r in rows:
            sv=json.loads(r['state_vector_json']); outcome=json.loads(r['outcome_json']) if r['outcome_json'] else {}
            for pct,sides in sv['ladders'].items():
                for side,lad in sides.items():
                    out.append({'observation_id':r['id'],'symbol':r['symbol'],'timestamp_ms':r['timestamp_ms'],'percentage':pct,'direction':side,'active_step':lad.get('active_step'), 'progressing': lad.get('progressing'), 'normalized_distance_to_next_step': lad.get('normalized_distance_to_next_step'), 'hit': bool(outcome.get('reached_pn_plus_1')), 'outcome': outcome, 'state_vector': sv})
        return out
    def set_running_state(self, running: bool)->None:
        with self._connect() as c: c.execute('insert or replace into runtime_state(key,value_json,updated_at) values (?,?,?)',('running',json.dumps(bool(running)),int(time.time()*1000)))
    def get_running_state(self)->bool:
        with self._connect() as c: r=c.execute('select value_json from runtime_state where key=?',('running',)).fetchone()
        return bool(json.loads(r['value_json'])) if r else False
