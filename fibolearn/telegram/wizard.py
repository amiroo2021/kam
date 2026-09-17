from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List
from fibolearn.config.defaults import DEFAULT_SYMBOLS, DEFAULT_PERCENTAGES
from fibolearn.storage.sqlite_store import FiboLearnStore
from fibolearn.features.context import significant_context_for_ladder, nearest_level

@dataclass
class Screen:
    text: str
    buttons: List[List[Dict[str,str]]] = field(default_factory=list)

def _b(text, cb): return {'text': text, 'callback_data': cb}

def _default_store():
    return FiboLearnStore(Path.home()/'.hermes'/'fibolearn'/'fibolearn.sqlite')

class FiboLearnWizard:
    def __init__(self, *, store: FiboLearnStore | None = None): self.store=store
    @property
    def _store(self):
        if self.store is None: self.store=_default_store()
        return self.store
    def open(self) -> Screen:
        running = self._store.get_running_state()
        return Screen('/fibolearn — FiboLearn research layer\n\nPhase 1/2 is READ-ONLY / OBSERVE / RESEARCH / BACKTEST. No trading actions are available.', [[_b('📡 Live Observer','fibolearn:live')],[_b('🔎 Pattern Discoveries','fibolearn:patterns'),_b('🧪 Backtests','fibolearn:backtests')],[_b('📊 Learning Report','fibolearn:report'),_b('🎯 Ask FiboLearn','fibolearn:ask')],[_b('⚙️ Settings','fibolearn:settings')],[_b('⏹️ Stop FiboLearn' if running else '▶️ Start FiboLearn', 'fibolearn:toggle:stop' if running else 'fibolearn:toggle:start')]])
    def handle_callback(self, data: str) -> Screen:
        suffix=data.split(':',1)[1] if data.startswith('fibolearn:') else data
        if suffix == 'live':
            return Screen('Live Observer — choose symbol', [[_b(s, f'fibolearn:live:{s}') for s in ('BTC','ETH','SOL')], [_b(s, f'fibolearn:live:{s}') for s in ('ZEC','PAXG','ALL')]])
        if suffix.startswith('toggle:'):
            running=suffix.endswith('start'); self._store.set_running_state(running)
            return Screen(f'FiboLearn collection/research is now {"running" if running else "stopped"}. This does not start/stop GoldenFibo trading or touch orders.', [[_b('◀️ Back','fibolearn:back')]])
        if suffix.startswith('live:'):
            sym=suffix.split(':',1)[1]
            return self._live_screen(sym)
        if suffix.startswith('study:'):
            from fibolearn.research.study import study_setup
            sym=suffix.rsplit(':',1)[-1]
            obs = self._store.latest_observation(sym)
            if obs is None:
                return Screen('Research job requires at least one stored synchronized observation. Status: CANDIDATE. Matches: 0')
            report=study_setup(self._store, obs, min_matches=1)
            return Screen(f'Research job created from frozen synchronized state. Status: {report.pattern.status.value}.\nCandidate class: CANDIDATE until independently validated.\nMatches: {report.match_count}\nOutcome rate: {report.outcome_rate if report.outcome_rate is not None else "insufficient evidence"}', [[_b('◀️ Back','fibolearn:live')]])
        if suffix == 'patterns':
            return Screen('Pattern Discoveries\nRecent Discoveries · Validated Patterns · Candidate Patterns · Rejected Patterns · Degraded Patterns · Patterns Being Watched')
        if suffix == 'backtests':
            return Screen('Backtests\nAutomatic Research · Test Live Pattern · Test Existing Pattern · Custom Hypothesis · Recent Tests')
        if suffix == 'report':
            return Screen('Learning Report\n\nDataset Status\nBaselines', [[_b('Dataset Status','fibolearn:report:dataset')], [_b('Baselines','fibolearn:report:baselines')]])
        if suffix == 'report:dataset':
            return self._dataset_status_screen()
        if suffix == 'report:baselines':
            return Screen('Baselines — choose symbol', [[_b(s, f'fibolearn:report:baseline:{s}:0.001:BUY') for s in ('BTC','ETH','SOL')], [_b(s, f'fibolearn:report:baseline:{s}:0.001:BUY') for s in ('ZEC','PAXG')]])
        if suffix.startswith('report:baseline:'):
            _,_,sym,pct,side = suffix.split(':',4)
            return self._baseline_screen(sym,pct,side)
        if suffix == 'report:coverage':
            return self._data_coverage_screen()
        if suffix == 'ask':
            return Screen('Ask FiboLearn\nQuestions must query stored observations/backtests. If no traceable result exists, FiboLearn reports insufficient evidence.')
        if suffix == 'settings':
            return Screen('Settings\nSymbols: BTC ETH SOL ZEC PAXG\nDirections: BUY SELL\nResearch: automatic discovery, backtests, cross-symbol, walk-forward\nAlerts: new/validated/strong/degraded patterns')
        return self.open()
    def _dataset_status_screen(self) -> Screen:
        from datetime import datetime, timezone
        def iso(ms):
            return datetime.fromtimestamp(int(ms)/1000, timezone.utc).isoformat().replace('+00:00','Z') if ms else '—'
        status=self._store.dataset_status_by_symbol()
        lines=['Dataset Status']
        for sym in ('BTC','ETH','SOL','ZEC','PAXG'):
            s=status.get(sym, {'candles':0,'observations':0,'episodes':0,'cycles':0})
            lines += ['', sym, f"Candles: {s.get('candles',0)}", f"Observations: {s.get('observations',0)}", f"Episodes: {s.get('episodes',0)}", f"Cycles: {s.get('cycles',0)}", f"Historical range: {iso(s.get('first_timestamp_ms'))} → {iso(s.get('last_timestamp_ms'))}"]
        return Screen('\n'.join(lines))

    def _data_coverage_screen(self) -> Screen:
        matrix = self._store.data_coverage_matrix()
        cells = matrix['cells']
        header = ['Symbol'.ljust(8), 'Side'.ljust(5), '1%'.rjust(7), '0.1%'.rjust(7), '0.01%'.rjust(7), '0.001%'.rjust(7)]
        lines = ['Data Coverage (completed episodes; * = insufficient data)', '', ' '.join(header)]
        for sym in ('BTC', 'ETH', 'SOL', 'ZEC', 'PAXG'):
            for side in ('BUY', 'SELL'):
                row = [sym.ljust(8), side.ljust(5)]
                for pct in ('1', '0.1', '0.01', '0.001'):
                    cell = cells[(sym, pct, side)]
                    n = cell['completed_episodes']
                    marker = '*' if not cell['sufficient'] else ''
                    row.append(f"{n}{marker}".rjust(7))
                lines.append(' '.join(row))
        lines.append('')
        lines.append(f"Threshold (min completed episodes): {matrix['min_completed_episodes']}; '*' marks insufficient cells.")
        return Screen('\n'.join(lines))

    def _baseline_screen(self, sym: str, pct: str, side: str) -> Screen:
        b=self._store.episode_baselines().get((sym,pct,side.upper()))
        if not b:
            return Screen(f'{sym} / {pct}% / {side.upper()}\nEpisodes: 0\nNo actual episode data stored yet.')
        n=max(1,b['episodes'])
        return Screen('\n'.join([
            f'{sym} / {pct}% / {side.upper()}',
            f"Episodes: {b['episodes']}",
            f"Raw observations: {b['raw_observations']}",
            f"P(n+1) before TP: {100*b['pn_plus_1_before_tp']/n:.2f}%",
            f"TP before P(n+1): {100*b['tp_before_pn_plus_1']/n:.2f}%",
            f"Censored: {100*b['other_censored']/n:.2f}%",
            f"Median episode duration: {b.get('median_duration_ms')} ms",
            f"Median MFE: {b.get('median_mfe')}",
            f"Median MAE: {b.get('median_mae')}",
        ]))

    def _live_screen(self, sym: str) -> Screen:
        obs=self._store.latest_observation(None if sym=='ALL' else sym)
        if not obs:
            return Screen(f'{sym} — MULTI-SCALE\n\nNo FiboLearn observations stored yet. Start FiboLearn or run historical collection first.', [[_b('▶️ Start FiboLearn','fibolearn:toggle:start')]])
        sv=obs['state_vector']; sym=sv['symbol']; lines=[f'{sym} — MULTI-SCALE', '', '              BUY             SELL']
        for pct in ('1','0.1','0.01','0.001'):
            sides=sv['ladders'].get(pct,{})
            b=sides.get('BUY',{}); s=sides.get('SELL',{})
            lines.append(f'{pct}%'.ljust(14)+f"P{b.get('active_step','?')}".ljust(16)+f"P{s.get('active_step','?')}")
        lines.append('')
        market=sv.get('market',{}); lines.append(f"VWAP {market.get('vwap','—')} · POC {market.get('poc','—')} · VAH {market.get('vah','—')} · VAL {market.get('val','—')}")
        target=sv['ladders'].get('0.001',{}).get('BUY') or next(iter(next(iter(sv['ladders'].values())).values()))
        import types, decimal
        obj=types.SimpleNamespace(**{k:(decimal.Decimal(str(v)) if k in {'pn','pn_plus_1','pn_plus_2','pn_minus_1','current_price'} and v is not None else v) for k,v in target.items()})
        ctx=significant_context_for_ladder(obj, market, sv.get('features',{})); n1=nearest_level(ctx.get('pn_plus_1',{})); n2=nearest_level(ctx.get('pn_plus_2',{}))
        lines += ['', f"P(n+1) nearest: {n1 if n1 else '—'}", f"P(n+2) nearest: {n2 if n2 else '—'}", 'FIBOLEARN: insufficient validated evidence unless reports show stored matches.']
        return Screen('\n'.join(lines), [[_b('🔬 Study This Setup', f'fibolearn:study:setup:{sym}')],[_b('🔬 Study Multi-Scale Setup', f'fibolearn:study:multiscale:{sym}')]])

async def handle_fibolearn_command(adapter, msg):
    text=(getattr(msg,'text','') or '').strip(); cmd=text.split(None,1)[0].lstrip('/').split('@',1)[0].lower() if text.startswith('/') else ''
    if cmd!='fibolearn': return False
    chat=getattr(msg,'chat',None); cid=getattr(chat,'id',None) if chat else None
    if cid is None: return False
    screen=FiboLearnWizard().open(); send=getattr(adapter,'send_inline_keyboard',None)
    if callable(send): await send(chat_id=str(cid), text=screen.text, buttons=screen.buttons, callback_prefix='')
    else: await adapter.send(str(cid), screen.text)
    return True
async def handle_fibolearn_callback(adapter, query, data):
    try:
        screen=FiboLearnWizard().handle_callback(data)
        await query.edit_message_text(screen.text)
        try: await query.answer()
        except Exception: pass
    except Exception:
        try: await query.answer()
        except Exception: pass
async def handle_fibolearn_text(adapter, msg): return False
