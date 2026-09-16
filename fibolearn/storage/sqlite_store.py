from __future__ import annotations
import json, sqlite3, time, statistics
from pathlib import Path
from typing import Any, Dict, List
from fibolearn import SCHEMA_VERSION
from fibolearn.labels.outcomes import complete_observation_outcome

def _D(x):
    return __import__('decimal').Decimal(str(x))

def _crossed(vals, level):
    if level is None or len(vals)<2: return False, None
    lv=_D(level); prev=_D(vals[0][1]) - lv
    for ts,v in vals[1:]:
        cur=_D(v)-lv
        if (prev <= 0 <= cur) or (prev >= 0 >= cur): return True, int(ts)
        prev=cur
    return False, None

def _episode_evolution(obs):
    start_ts=obs[0][0]; start_lad=obs[0][2]
    pn=start_lad.get('pn')
    vwap=[(ts, sv.get('market',{}).get('vwap')) for ts,_,_,sv in obs if sv.get('market',{}).get('vwap') is not None]
    poc=[(ts, sv.get('market',{}).get('poc')) for ts,_,_,sv in obs if sv.get('market',{}).get('poc') is not None]
    vw_cross,vw_ts=_crossed(vwap,pn); poc_cross,poc_ts=_crossed(poc,pn)
    prices=[_D(sv.get('market',{}).get('price') or lad.get('current_price')) for ts,_,lad,sv in obs]
    higher_highs=sum(1 for a,b in zip(prices,prices[1:]) if b>a); lower_highs=sum(1 for a,b in zip(prices,prices[1:]) if b<a)
    end_lad=obs[-1][2]
    def prox(lad, sv, target):
        levels=sv.get('features',{}).get('significant_levels',{})
        vals=[abs(_D(lad.get(target))-_D(v)) for v in levels.values() if v is not None]
        return str(min(vals)) if vals else None
    events=[]
    if vw_cross: events.append({'event':'vwap_crossed_pn','timestamp_ms':vw_ts,'elapsed_ms':vw_ts-start_ts})
    if poc_cross: events.append({'event':'poc_crossed_pn','timestamp_ms':poc_ts,'elapsed_ms':poc_ts-start_ts})
    return {'vwap_crossed_pn_since_start':vw_cross,'time_to_vwap_cross_ms':(vw_ts-start_ts if vw_ts else None),'poc_crossed_pn_since_start':poc_cross,'time_to_poc_cross_ms':(poc_ts-start_ts if poc_ts else None),'pn_retest_count_since_activation':max([int(x[2].get('pn_retest_count') or 0) for x in obs] or [0]),'time_since_last_pn_touch_ms':int(obs[-1][2].get('time_since_active_step_changed_ms') or 0),'higher_high_count':higher_highs,'lower_high_count':lower_highs,'progression_velocity':(int(end_lad.get('active_step') or 0)-int(start_lad.get('active_step') or 0))/max(1,(obs[-1][0]-start_ts)/60000),'pn_plus_1_significant_level_proximity_change':{'start':prox(start_lad,obs[0][3],'pn_plus_1'),'end':prox(end_lad,obs[-1][3],'pn_plus_1')},'pn_plus_2_significant_level_proximity_change':{'start':prox(start_lad,obs[0][3],'pn_plus_2'),'end':prox(end_lad,obs[-1][3],'pn_plus_2')},'cross_scale_start':obs[0][3].get('features',{}).get('cross_percentage_active_depth'),'cross_scale_end':obs[-1][3].get('features',{}).get('cross_percentage_active_depth'),'event_ordering':sorted(events,key=lambda e:e['timestamp_ms'])}

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
            c.execute('create unique index if not exists idx_obs_symbol_ts_unique on observations(symbol,timestamp_ms)')
            c.execute('create table if not exists episodes(id integer primary key autoincrement, episode_key text not null unique, symbol text not null, percentage text not null, direction text not null, cycle_id text not null, active_step integer not null, start_timestamp_ms integer not null, end_timestamp_ms integer not null, terminal_event text not null, observation_count integer not null, raw_observation_ids_json text not null, start_state_json text not null, end_state_json text not null, evolution_json text not null, outcome_json text not null, created_at integer not null)')
            c.execute('create index if not exists idx_episodes_symbol_pct_dir on episodes(symbol,percentage,direction)')
            c.execute('create table if not exists dataset_checkpoints(key text primary key, value_json text not null, updated_at integer not null)')
    def save_observation(self, vec) -> int:
        d=vec.to_dict(); raw=d.get('raw',{}); now=int(time.time()*1000)
        with self._connect() as c:
            r=c.execute('select id from observations where symbol=? and timestamp_ms=?',(d['symbol'],int(d['timestamp_ms']))).fetchone()
            if r:
                c.execute('update observations set schema_version=?, cycle_id=?, state_vector_json=?, raw_json=?, created_at=? where id=?',(SCHEMA_VERSION,None,json.dumps(d,sort_keys=True),json.dumps(raw,sort_keys=True),now,int(r['id'])))
                return int(r['id'])
            cur=c.execute('insert into observations(schema_version,symbol,timestamp_ms,cycle_id,state_vector_json,raw_json,created_at) values (?,?,?,?,?,?,?)',(SCHEMA_VERSION,d['symbol'],int(d['timestamp_ms']),None,json.dumps(d,sort_keys=True),json.dumps(raw,sort_keys=True),now))
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

    def rebuild_episodes(self, symbol: str | None=None)->int:
        rows=self.all_observations(symbol); groups={}
        for o in rows:
            sv=o['state_vector']; sid=o['id']; ts=int(sv['timestamp_ms']); sym=sv['symbol']
            for pct,sides in sv.get('ladders',{}).items():
                for side,lad in sides.items():
                    key=f"{sym}|{pct}|{side}|{lad.get('cycle_id')}|{lad.get('active_step')}"
                    g=groups.setdefault(key, {'episode_key':key,'symbol':sym,'percentage':pct,'direction':side,'cycle_id':str(lad.get('cycle_id')),'active_step':int(lad.get('active_step') or 0),'obs':[]})
                    g['obs'].append((ts,sid,lad,sv))
        now=int(time.time()*1000); n=0
        with self._connect() as c:
            if symbol: c.execute('delete from episodes where symbol=?',(symbol,))
            else: c.execute('delete from episodes')
            for key,g in groups.items():
                obs=sorted(g['obs'], key=lambda x:x[0])
                start_ts,end_ts=obs[0][0],obs[-1][0]
                start_lad,end_lad=obs[0][2],obs[-1][2]
                start_sv,end_sv=obs[0][3],obs[-1][3]
                future=[]
                # next episode for same ladder family determines competing terminal event
                for h in groups.values():
                    if h is g or h['symbol']!=g['symbol'] or h['percentage']!=g['percentage'] or h['direction']!=g['direction']: continue
                    hobs=sorted(h['obs'], key=lambda x:x[0])
                    if hobs and hobs[0][0] > end_ts: future.append((hobs[0][0], h))
                terminal='censored'
                terminal_ts=end_ts
                if future:
                    terminal_ts, nxt = min(future, key=lambda x:x[0])
                    evs=(nxt['obs'][0][3].get('raw',{}).get('domain_events',{}) or {}).get(f"{g['percentage']}:{g['direction']}", [])
                    if 'progression' in evs and ('tp_hit' not in evs or evs.index('progression') < evs.index('tp_hit')):
                        terminal='pn_plus_1_before_tp'
                    elif 'tp_hit' in evs or 'cycle_closed' in evs or int(nxt['cycle_id']) != int(g['cycle_id']):
                        terminal='tp_before_pn_plus_1'
                    elif int(nxt['active_step']) > int(g['active_step']): terminal='pn_plus_1_before_tp'
                    elif int(nxt['active_step']) < int(g['active_step']): terminal='tp_before_pn_plus_1'
                    else: terminal='other_terminal'
                prices=[_D(x[3].get('market',{}).get('price') or x[2].get('current_price')) for x in obs]
                cur0=_D(start_lad.get('current_price'))
                if g['direction']=='BUY': mfe=max(prices)-cur0; mae=min(prices)-cur0
                else: mfe=cur0-min(prices); mae=cur0-max(prices)
                evolution=_episode_evolution(obs)
                outcome={'terminal_event':terminal,'terminal_timestamp_ms':terminal_ts,'duration_ms':end_ts-start_ts,'mfe':str(mfe),'mae':str(mae),'event_ordering': evolution.get('event_ordering',[])}
                c.execute('insert or replace into episodes(episode_key,symbol,percentage,direction,cycle_id,active_step,start_timestamp_ms,end_timestamp_ms,terminal_event,observation_count,raw_observation_ids_json,start_state_json,end_state_json,evolution_json,outcome_json,created_at) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (key,g['symbol'],g['percentage'],g['direction'],g['cycle_id'],g['active_step'],start_ts,end_ts,terminal,len(obs),json.dumps([x[1] for x in obs]),json.dumps(start_sv,sort_keys=True),json.dumps(end_sv,sort_keys=True),json.dumps(evolution,sort_keys=True),json.dumps(outcome,sort_keys=True),now))
                n+=1
        return n

    def episode_rows(self, symbol: str | None=None)->list[Dict[str,Any]]:
        q='select * from episodes'; args=[]
        if symbol: q+=' where symbol=?'; args.append(symbol)
        q+=' order by start_timestamp_ms, id'
        with self._connect() as c: rows=c.execute(q,args).fetchall()
        out=[]
        for r in rows:
            d=dict(r)
            for k in ('raw_observation_ids_json','start_state_json','end_state_json','evolution_json','outcome_json'):
                d[k[:-5] if k.endswith('_json') else k]=json.loads(d.pop(k))
            out.append(d)
        return out

    def episode_baselines(self)->Dict[tuple,Dict[str,Any]]:
        obs_counts={}
        for r in self.dataset_rows():
            key=(r['symbol'], r['percentage'], r['direction']); obs_counts[key]=obs_counts.get(key,0)+1
        out={}
        for e in self.episode_rows():
            key=(e['symbol'],e['percentage'],e['direction'])
            b=out.setdefault(key, {'symbol':e['symbol'],'percentage':e['percentage'],'direction':e['direction'],'episodes':0,'raw_observations':obs_counts.get(key,0),'pn_plus_1_before_tp':0,'tp_before_pn_plus_1':0,'other_censored':0,'durations_ms':[],'mfes':[],'maes':[]})
            b['episodes']+=1; term=e['terminal_event']
            if term=='pn_plus_1_before_tp': b['pn_plus_1_before_tp']+=1
            elif term=='tp_before_pn_plus_1': b['tp_before_pn_plus_1']+=1
            else: b['other_censored']+=1
            oc=e.get('outcome') or {}; b['durations_ms'].append(int(oc.get('duration_ms') or 0)); b['mfes'].append(float(oc.get('mfe') or 0)); b['maes'].append(float(oc.get('mae') or 0))
        for b in out.values():
            n=max(1,b['episodes']); b['episode_level_progression_rate']=b['pn_plus_1_before_tp']/n; b['tp_before_progression_rate']=b['tp_before_pn_plus_1']/n; b['censored_rate']=b['other_censored']/n
            rows=[r for r in self.dataset_rows() if (r['symbol'],r['percentage'],r['direction'])==(b['symbol'],b['percentage'],b['direction'])]
            b['observation_level_success_rate']=(sum(1 for r in rows if r.get('hit'))/len(rows)) if rows else None
            b['median_duration_ms']=statistics.median(b['durations_ms']) if b['durations_ms'] else None; b['median_mfe']=statistics.median(b['mfes']) if b['mfes'] else None; b['median_mae']=statistics.median(b['maes']) if b['maes'] else None
        return out

    def time_dependent_baselines(self, minutes=(1,3,5,10,15,30,60))->Dict[tuple,Dict[int,Dict[str,Any]]]:
        out={}
        for e in self.episode_rows():
            key=(e['symbol'],e['percentage'],e['direction']); out.setdefault(key,{})
            duration_min=(int(e['end_timestamp_ms'])-int(e['start_timestamp_ms']))/60000
            for m in minutes:
                d=out[key].setdefault(int(m), {'eligible_episodes':0,'pn_plus_1_before_tp':0,'tp_before_pn_plus_1':0,'other_censored':0,'probability_pn_plus_1_before_tp':None})
                if duration_min >= m:
                    d['eligible_episodes']+=1
                    if e['terminal_event']=='pn_plus_1_before_tp': d['pn_plus_1_before_tp']+=1
                    elif e['terminal_event']=='tp_before_pn_plus_1': d['tp_before_pn_plus_1']+=1
                    else: d['other_censored']+=1
        for mset in out.values():
            for d in mset.values():
                if d['eligible_episodes']: d['probability_pn_plus_1_before_tp']=d['pn_plus_1_before_tp']/d['eligible_episodes']
        return out

    def dataset_status_by_symbol(self)->Dict[str,Dict[str,Any]]:
        out={}
        with self._connect() as c:
            for r in c.execute('select symbol,count(*) candles,min(timestamp_ms) first_ts,max(timestamp_ms) last_ts from observations group by symbol'):
                out[r['symbol']]={'symbol':r['symbol'],'candles':int(r['candles']),'observations':int(r['candles'])*8,'first_timestamp_ms':r['first_ts'],'last_timestamp_ms':r['last_ts'],'episodes':0,'cycles':0}
            for r in c.execute('select symbol,count(*) episodes,count(distinct cycle_id) cycles from episodes group by symbol'):
                out.setdefault(r['symbol'], {'symbol':r['symbol']}).update({'episodes':int(r['episodes']),'cycles':int(r['cycles'])})
        return out

    def set_checkpoint(self, key: str, value: Dict[str,Any])->None:
        with self._connect() as c: c.execute('insert or replace into dataset_checkpoints(key,value_json,updated_at) values (?,?,?)',(key,json.dumps(value,sort_keys=True),int(time.time()*1000)))
    def get_checkpoint(self, key: str)->Dict[str,Any] | None:
        with self._connect() as c: r=c.execute('select value_json from dataset_checkpoints where key=?',(key,)).fetchone()
        return json.loads(r['value_json']) if r else None

    def set_running_state(self, running: bool)->None:
        with self._connect() as c: c.execute('insert or replace into runtime_state(key,value_json,updated_at) values (?,?,?)',('running',json.dumps(bool(running)),int(time.time()*1000)))
    def get_running_state(self)->bool:
        with self._connect() as c: r=c.execute('select value_json from runtime_state where key=?',('running',)).fetchone()
        return bool(json.loads(r['value_json'])) if r else False
