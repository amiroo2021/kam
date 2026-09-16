from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List
from fibolearn.config.defaults import DEFAULT_SYMBOLS, DEFAULT_PERCENTAGES

@dataclass
class Screen:
    text: str
    buttons: List[List[Dict[str,str]]] = field(default_factory=list)

def _b(text, cb): return {'text': text, 'callback_data': cb}

class FiboLearnWizard:
    def open(self) -> Screen:
        return Screen('/fibolearn — FiboLearn research layer\n\nPhase 1 is READ-ONLY / OBSERVE / RESEARCH / BACKTEST. No trading actions are available.', [[_b('📡 Live Observer','fibolearn:live')],[_b('🔎 Pattern Discoveries','fibolearn:patterns'),_b('🧪 Backtests','fibolearn:backtests')],[_b('📊 Learning Report','fibolearn:report'),_b('🎯 Ask FiboLearn','fibolearn:ask')],[_b('⚙️ Settings','fibolearn:settings')],[_b('▶️ Start FiboLearn / ⏹️ Stop FiboLearn','fibolearn:toggle')]])
    def handle_callback(self, data: str) -> Screen:
        suffix=data.split(':',1)[1] if data.startswith('fibolearn:') else data
        if suffix == 'live':
            return Screen('Live Observer — choose symbol', [[_b(s, f'fibolearn:live:{s}') for s in ('BTC','ETH','SOL')], [_b(s, f'fibolearn:live:{s}') for s in ('ZEC','PAXG','ALL')], [_b('MULTI-SCALE','fibolearn:live:BTC')]])
        if suffix.startswith('live:'):
            sym=suffix.split(':',1)[1]
            rows=[[_b('🔬 Study This Setup', f'fibolearn:study:setup:{sym}')],[_b('🔬 Study Multi-Scale Setup', f'fibolearn:study:multiscale:{sym}')]]
            return Screen(f'{sym} — MULTI-SCALE STATE\n\n             BUY             SELL\n1%           P0 ACTIVE       P0 ACTIVE\n0.1%         P0 ACTIVE       P0 ACTIVE\n0.01%        P0 ACTIVE       P0 ACTIVE\n0.001%       P0 ACTIVE       P0 ACTIVE\n\nFIBOLEARN: insufficient evidence until observations/backtests exist.', rows)
        if suffix.startswith('study:'):
            return Screen('Research job created from frozen synchronized state. Status: CANDIDATE. Historical analog search/backtest can run without trading.')
        if suffix == 'patterns':
            return Screen('Pattern Discoveries\nRecent Discoveries · Validated Patterns · Candidate Patterns · Rejected Patterns · Degraded Patterns · Patterns Being Watched')
        if suffix == 'backtests':
            return Screen('Backtests\nAutomatic Research · Test Live Pattern · Test Existing Pattern · Custom Hypothesis · Recent Tests')
        if suffix == 'report':
            return Screen('Learning Report\nDaily · 7 Days · 30 Days · All Time')
        if suffix == 'ask':
            return Screen('Ask FiboLearn\nQuestions must query stored observations/backtests. If no traceable result exists, FiboLearn reports insufficient evidence.')
        if suffix == 'settings':
            return Screen('Settings\nSymbols: BTC ETH SOL ZEC PAXG\nDirections: BUY SELL\nResearch: automatic discovery, backtests, cross-symbol, walk-forward\nAlerts: new/validated/strong/degraded patterns')
        return self.open()

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
