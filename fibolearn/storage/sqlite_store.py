from __future__ import annotations
import json, sqlite3, time
from pathlib import Path
from typing import Any, Dict
from fibolearn import SCHEMA_VERSION

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
            c.execute('create table if not exists outcomes(id integer primary key autoincrement, observation_id integer not null, outcome_json text not null, created_at integer not null)')
            c.execute('create table if not exists patterns(id integer primary key autoincrement, name text not null, status text not null, definition_json text not null, metrics_json text not null, created_at integer not null)')
            c.execute('create index if not exists idx_patterns_status on patterns(status)')
            c.execute('create table if not exists backtests(id integer primary key autoincrement, pattern_id integer, symbols_json text not null, result_json text not null, created_at integer not null)')
    def save_observation(self, vec) -> int:
        d=vec.to_dict(); raw=d.get('raw',{})
        with self._connect() as c:
            cur=c.execute('insert into observations(schema_version,symbol,timestamp_ms,cycle_id,state_vector_json,raw_json,created_at) values (?,?,?,?,?,?,?)',(SCHEMA_VERSION,d['symbol'],int(d['timestamp_ms']),None,json.dumps(d,sort_keys=True),json.dumps(raw,sort_keys=True),int(time.time()*1000)))
            return int(cur.lastrowid)
    def get_observation(self, oid:int)->Dict[str,Any]:
        with self._connect() as c: r=c.execute('select * from observations where id=?',(oid,)).fetchone()
        d=dict(r); d['state_vector']=json.loads(d.pop('state_vector_json')); d['raw']=json.loads(d.pop('raw_json')); return d
