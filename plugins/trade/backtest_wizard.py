"""Telegram /backtest wizard for GoldenFibo Binance Spot/Futures historical replay."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# GoldenFibo package currently lives outside Hermes' install tree.
_GF_ROOT = Path("/root/golden_fibo")
if str(_GF_ROOT) not in sys.path:
    sys.path.insert(0, str(_GF_ROOT))

from golden_fibo.constants import Side  # noqa: E402
from golden_fibo.historical_replay import levels_p0_to_pn, replay_ohlc, iso  # noqa: E402
from golden_fibo.ladder import ladder_step  # noqa: E402
from plugins.trade.tradedesk import TradeDesk  # noqa: E402

logger = logging.getLogger(__name__)

_SPOT = "https://api.binance.com"
_FUTURES = "https://fapi.binance.com"
_TIMEOUT = 30
_LIMIT_SPOT = 1000
_LIMIT_FUTURES = 1500
_OUT = Path("/root/golden_fibo/outputs/backtest_wizard")
_OUT.mkdir(parents=True, exist_ok=True)


def _button(text: str, cb: str) -> Dict[str, str]:
    return {"text": text, "callback_data": cb}


@dataclass
class Screen:
    text: str
    buttons: List[List[Dict[str, str]]] = field(default_factory=list)
    state: str = ""
    attachments: List[str] = field(default_factory=list)


@dataclass
class State:
    step: str = "mode"
    ladder: str = ""
    market: str = ""
    requested: str = ""
    symbol: str = ""
    display: str = ""
    price: str = ""
    percentage: float = 0.001
    day: Optional[int] = None
    month: Optional[int] = None
    year: Optional[int] = None
    awaiting_text: Optional[str] = None


class BacktestWizard:
    def __init__(self) -> None:
        self._states: Dict[Tuple[Any, ...], State] = {}
        self._desk = TradeDesk()

    def _state(self, key: Tuple[Any, ...]) -> State:
        return self._states.setdefault(key, State())

    def reset(self, key: Tuple[Any, ...]) -> None:
        self._states.pop(key, None)

    def _nav(self, back: str | None = None) -> List[Dict[str, str]]:
        row: List[Dict[str, str]] = []
        if back:
            row.append(_button("⬅️ Back", f"back:{back}"))
        row.append(_button("✖️ Exit", "exit"))
        return row

    def open(self, key: Tuple[Any, ...]) -> Screen:
        self._states[key] = State()
        return self._screen_ladder()

    def _screen_ladder(self) -> Screen:
        return Screen(
            "Choose GoldenFibo backtest side:",
            [
                [_button("🔵 Buy Ladder", "ladder:buy")],
                [_button("🔴 Sell Ladder", "ladder:sell")],
                [_button("⚪ Both", "ladder:both")],
                self._nav(),
            ],
            "ladder",
        )

    def _screen_market(self) -> Screen:
        return Screen("Choose Binance market:", [[_button("Spot", "market:spot"), _button("Futures", "market:futures")], self._nav("ladder")], "market")

    def _screen_symbol(self) -> Screen:
        return Screen(
            "Choose instrument:",
            [[_button("BTC", "symbol:BTC"), _button("ETH", "symbol:ETH"), _button("SOL", "symbol:SOL")], [_button("HYPE", "symbol:HYPE"), _button("Other", "symbol:other")], self._nav("market")],
            "symbol",
        )

    def _resolve_and_price(self, st: State, requested: str) -> Screen:
        st.requested = requested.strip().upper()
        resp = self._desk.execute({"operation": "resolve_instrument", "exchange": "binance", "account": st.market, "market_type": st.market, "symbol": st.requested})
        if not getattr(resp, "success", False) or getattr(resp, "instrument", None) is None:
            msg = getattr(getattr(resp, "error", None), "message", None) or "Instrument not found."
            return Screen(f"Instrument not found: {st.requested}\n{msg}", [self._nav("symbol")], "symbol_error")
        inst = resp.instrument
        st.symbol = inst.symbol
        st.display = inst.display_name
        px_resp = self._desk.execute({"operation": "market_price", "exchange": "binance", "account": st.market, "market_type": st.market, "symbol": st.symbol})
        px = None
        if getattr(px_resp, "success", False):
            mp = getattr(px_resp, "market_price", None)
            px = getattr(mp, "price", None) or getattr(mp, "mark_price", None) if mp is not None else None
            if not px and isinstance(getattr(px_resp, "data", None), dict):
                px = px_resp.data.get("price")
        st.price = str(px or "?")
        return Screen(
            f"Confirm instrument:\n\n{st.display}\nPrice: {st.price}\nMarket: {st.market}",
            [[_button(f"✅ {st.symbol} @ {st.price}", "confirm:instrument")], self._nav("symbol")],
            "confirm_instrument",
        )

    def _screen_percentage(self) -> Screen:
        return Screen(
            "Choose ladder percentage (step-0 TP distance):",
            [
                [_button("Default: 0.001", "pct:0.001")],
                [_button("0.0001", "pct:0.0001"), _button("0.001", "pct:0.001"), _button("0.01", "pct:0.01")],
                [_button("0.1", "pct:0.1"), _button("1", "pct:1"), _button("Other", "pct:other")],
                self._nav("instrument"),
            ],
            "percentage",
        )

    def handle_callback(self, key: Tuple[Any, ...], suffix: str) -> Screen:
        st = self._state(key)
        if suffix == "exit":
            self.reset(key); return Screen("Backtest closed.", [], "closed")
        if suffix.startswith("back:"):
            target = suffix.split(":", 1)[1]
            if target == "symbol": return self._screen_symbol()
            if target == "instrument" and st.symbol:
                return Screen(
                    f"Confirm instrument:\n\n{st.display}\nPrice: {st.price}\nMarket: {st.market}",
                    [[_button(f"✅ {st.symbol} @ {st.price}", "confirm:instrument")], self._nav("symbol")],
                    "confirm_instrument",
                )
            if target == "pct": return self._screen_percentage()
            if target == "day":
                st.awaiting_text = "day"
                return Screen("Enter start day of month (1–31):", [self._nav("pct")], "await_day")
            if target == "market": return self._screen_market()
            return self._screen_ladder()
        if suffix.startswith("ladder:"):
            st.ladder = suffix.split(":",1)[1]
            return self._screen_market()
        if suffix.startswith("market:"):
            st.market = suffix.split(":",1)[1]
            return self._screen_symbol()
        if suffix.startswith("symbol:"):
            sym = suffix.split(":",1)[1]
            if sym == "other":
                st.awaiting_text = "symbol"
                return Screen("Type the Binance instrument symbol/base (example: BTC, BTCUSDT, ETH).", [self._nav("symbol")], "await_symbol")
            return self._resolve_and_price(st, sym)
        if suffix == "confirm:instrument":
            return self._screen_percentage()
        if suffix.startswith("pct:"):
            raw = suffix.split(":",1)[1]
            if raw == "other":
                st.awaiting_text = "percentage"
                return Screen("Type custom ladder percentage (example: 0.001):", [self._nav("pct")], "await_percentage")
            try:
                st.percentage = float(raw)
                if st.percentage <= 0:
                    raise ValueError
            except Exception:
                return self._screen_percentage()
            st.awaiting_text = "day"
            return Screen(f"Percentage set to {st.percentage:g}.\n\nEnter start day of month (1–31):", [self._nav("pct")], "await_day")
        if suffix.startswith("month:"):
            st.month = int(suffix.split(":",1)[1])
            st.awaiting_text = "year"
            return Screen("Enter start year (example: 2026):", [self._nav("day")], "await_year")
        if suffix == "run":
            return self._run_backtest(st)
        return self._screen_ladder()

    def handle_text(self, key: Tuple[Any, ...], text: str) -> Optional[Screen]:
        st = self._state(key)
        what = st.awaiting_text
        if not what:
            return None
        text = (text or "").strip()
        if what == "symbol":
            st.awaiting_text = None
            return self._resolve_and_price(st, text)
        if what == "percentage":
            try:
                st.percentage = float(text)
                if st.percentage <= 0:
                    raise ValueError
            except Exception:
                return Screen("Invalid percentage. Type a positive number like 0.001:", [self._nav("pct")], "await_percentage")
            st.awaiting_text = "day"
            return Screen(f"Percentage set to {st.percentage:g}.\n\nEnter start day of month (1–31):", [self._nav("pct")], "await_day")
        if what == "day":
            try:
                day = int(text)
                if not 1 <= day <= 31: raise ValueError
            except Exception:
                return Screen("Invalid day. Enter 1–31:", [self._nav("pct")], "await_day")
            st.day = day; st.awaiting_text = None
            rows=[]
            for a in range(1,13,4):
                rows.append([_button(str(m), f"month:{m}") for m in range(a,a+4)])
            rows.append(self._nav("day"))
            return Screen("Choose start month:", rows, "month")
        if what == "year":
            try:
                year = int(text)
                if not 2017 <= year <= datetime.now(timezone.utc).year: raise ValueError
                datetime(year, st.month or 1, st.day or 1, 0, 1, tzinfo=timezone.utc)
            except Exception:
                return Screen("Invalid year/date. Enter a valid year (example: 2026):", [self._nav("day")], "await_year")
            st.year = year; st.awaiting_text = None
            start = datetime(st.year, st.month or 1, st.day or 1, 0, 1, tzinfo=timezone.utc).isoformat().replace("+00:00","Z")
            return Screen(f"Run {st.ladder.upper()} backtest?\n{st.symbol} {st.market}\nPercentage: {st.percentage:g}\nStart: {start}", [[_button("▶️ Run backtest", "run")], self._nav("day")], "confirm_run")
        return None

    def _run_backtest(self, st: State) -> Screen:
        start_dt = datetime(st.year or 2026, st.month or 1, st.day or 1, 0, 1, tzinfo=timezone.utc)
        sides = [Side.BUY, Side.SELL] if st.ladder == "both" else ([Side.BUY] if st.ladder == "buy" else [Side.SELL])
        candles = _fetch_klines(st.symbol, st.market, int(start_dt.timestamp()*1000))
        if not candles:
            return Screen("No Binance candles returned for that request.", [self._nav("symbol")], "error")
        parts=[f"Backtest complete: {st.symbol} {st.market}\nPercentage: {st.percentage:g}\nData: {iso(int(candles[0][0]))} → {iso(int(candles[-1][0]))}\nCandles: {len(candles):,}\n"]
        attachments=[]
        for side in sides:
            result = _summarize_side(candles, side, st.symbol, st.market, st.percentage)
            parts.append(result["text"])
            attachments.append(result["svg"])
        self.reset(_DUMMY_KEY) if False else None
        return Screen("\n\n".join(parts), [[_button("New backtest", "restart"), _button("Exit", "exit")]], "done", attachments)


_DUMMY_KEY=("dummy",)
_WIZARD = BacktestWizard()


def _fetch_json(base: str, path: str, params: Dict[str, Any]):
    qs=urllib.parse.urlencode({k:v for k,v in params.items() if v is not None})
    req=urllib.request.Request(f"{base}{path}?{qs}", headers={"User-Agent":"HermesBacktest/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 public HTTPS API
        return json.loads(resp.read().decode())


def _fetch_klines(symbol: str, market: str, start_ms: int):
    base = _FUTURES if market == "futures" else _SPOT
    path = "/fapi/v1/klines" if market == "futures" else "/api/v3/klines"
    limit = _LIMIT_FUTURES if market == "futures" else _LIMIT_SPOT
    end = int(datetime.now(timezone.utc).timestamp()*1000)
    out=[]; t=start_ms
    while t <= end:
        data=_fetch_json(base,path,{"symbol":symbol,"interval":"1m","startTime":t,"endTime":end,"limit":limit})
        if not data: break
        out.extend(data)
        last=int(data[-1][0]); nt=last+60000
        if nt<=t: break
        t=nt
        if len(data)<limit: break
        time.sleep(0.015)
    by={int(k[0]):k for k in out if start_ms<=int(k[0])<=end}
    return [by[k] for k in sorted(by)]


def _vwap(candles, ts):
    rel=[k for k in candles if int(k[0])>=ts]
    base=sum(float(k[5]) for k in rel); quote=sum(float(k[7]) for k in rel)
    return quote/base if base else float("nan")


def _poc(candles, ts, bins: int = 160):
    """Approximate volume-profile point of control from OHLCV candles.

    Binance 1m candles do not expose tick-level volume-at-price, so this
    distributes each candle's base volume uniformly over its high-low range
    into fixed bins for the requested window and returns the max-volume bin
    center.
    """
    rel=[k for k in candles if int(k[0])>=ts]
    if not rel:
        return float("nan")
    lo=min(float(k[3]) for k in rel); hi=max(float(k[2]) for k in rel)
    if hi <= lo:
        return float(rel[-1][4])
    bins=max(20, int(bins))
    width=(hi-lo)/bins
    vols=[0.0 for _ in range(bins)]
    for k in rel:
        h=float(k[2]); l=float(k[3]); v=float(k[5])
        if v <= 0:
            continue
        if h <= l:
            idx=min(bins-1,max(0,int((float(k[4])-lo)/width)))
            vols[idx]+=v
            continue
        a=max(0,int((l-lo)/width)); b=min(bins-1,int((h-lo)/width))
        count=max(1,b-a+1); share=v/count
        for idx in range(a,b+1):
            vols[idx]+=share
    idx=max(range(bins), key=lambda i: vols[i])
    return lo + (idx + 0.5) * width


def _fmt(x: float) -> str:
    return f"{x:,.2f}"


def _summarize_side(candles, side: Side, symbol: str, market: str, percentage: float):
    state=replay_ohlc(candles, side=side, percentage=percentage)
    n=state.highest_filled
    pn,_=ladder_step(side,state.p0,n, percentage=percentage)
    pn1,_=ladder_step(side,state.p0,min(n+1,20), percentage=percentage)
    pn2,_=ladder_step(side,state.p0,min(n+2,20), percentage=percentage)
    pnm1,_=ladder_step(side,state.p0,n-1, percentage=percentage) if n>=1 else (state.shared_tp,None)
    ladder_vwap=_vwap(candles,state.legs[0].ts)
    step_vwap=_vwap(candles,state.legs[-1].ts)
    ladder_poc=_poc(candles,state.legs[0].ts)
    step_poc=_poc(candles,state.legs[-1].ts)
    last=float(candles[-1][4])
    levels=levels_p0_to_pn(state,min(n+2,20), percentage=percentage)
    jpg=_draw_jpg(symbol, market, side, levels, n, ladder_vwap, step_vwap, ladder_poc, step_poc, last)
    label="BUY" if side is Side.BUY else "SELL"
    lines=[f"{label} ladder", f"Cycle: {state.cycle}  Completed: {len(state.closed)}", f"P0: {_fmt(state.p0)}", f"Current step: P{n} = {_fmt(pn)}", f"TP/P(n-1): {_fmt(pnm1)}", f"Next P{n+1}: {_fmt(pn1)}", f"P{n+2}: {_fmt(pn2)}", f"Ladder VWAP: {_fmt(ladder_vwap)}", f"Step VWAP: {_fmt(step_vwap)}", f"Ladder POC: {_fmt(ladder_poc)}", f"Step POC: {_fmt(step_poc)}", f"Last close: {_fmt(last)}"]
    return {"text":"\n".join(lines), "svg":jpg}


def _draw_jpg(symbol, market, side, levels, n, ladder_vwap, step_vwap, ladder_poc, step_poc, current):
    """Draw visual ladder summary directly to JPEG for Telegram photo delivery."""
    from PIL import Image, ImageDraw, ImageFont

    W,H=1200,1600; left,right=170,1100; top,bottom=110,1440
    prices=[float(x['price']) for x in levels]+[ladder_vwap,step_vwap,ladder_poc,step_poc,current]
    pmin=min(prices); pmax=max(prices); pad=(pmax-pmin)*0.07 or 1; pmin-=pad; pmax+=pad
    def y(p): return bottom-(float(p)-pmin)/(pmax-pmin)*(bottom-top)
    def font(path, size):
        try: return ImageFont.truetype(path, size)
        except Exception: return ImageFont.load_default()
    fb=font('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',30)
    fm=font('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',18)
    fmb=font('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',18)
    fs=font('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',15)
    fmono=font('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf',13)
    img=Image.new('RGB',(W,H),'#fbfbf8'); d=ImageDraw.Draw(img)
    label='BUY' if side is Side.BUY else 'SELL'
    d.text((60,35),f'{symbol} {market.upper()} {label} GoldenFibo Backtest',fill='#111',font=fb)
    d.text((60,72),'Visual summary: P0 at bottom, active P(n), VWAPs, and current price',fill='#555',font=fs)
    d.rectangle([left,top,right,bottom],fill='white',outline='#ddd')
    for j in range(8):
        price=pmin+(pmax-pmin)*j/7; yy=y(price)
        d.line([left,yy,right,yy],fill='#eee',width=1)
        txt=_fmt(price); bbox=d.textbbox((0,0),txt,font=fmono); d.text((left-12-bbox[2],yy-7),txt,fill='#888',font=fmono)
    line_color='#77aaff' if side is Side.BUY else '#ff7a7a'; active='#0058ff' if side is Side.BUY else '#d71920'
    xmid=left+65
    d.line([xmid,y(float(levels[0]['price'])),xmid,y(float(levels[-1]['price']))],fill=line_color,width=3)
    for item in levels:
        i=int(str(item['level'])[1:]); p=float(item['price']); yy=y(p); is_active=i==n
        col=active if is_active else line_color
        d.line([left,yy,right,yy],fill=col,width=5 if is_active else 2)
        d.ellipse([xmid-7,yy-7,xmid+7,yy+7],fill=col,outline='white',width=2)
        role=item.get('role') or ''
        txt=f"{item['level']} {_fmt(p)}" + (f"  {role}" if role else '')
        f=fmb if is_active else fm
        bbox=d.textbbox((0,0),txt,font=f)
        d.text((right-12-bbox[2],yy-25),txt,fill=active if is_active else '#933',font=f)
    def dashed(x1, yy, x2, color, width=4, dash=12, gap=8):
        x=x1
        while x<x2:
            d.line([x,yy,min(x+dash,x2),yy],fill=color,width=width); x+=dash+gap
    for name,p,color,dot in [('Ladder VWAP',ladder_vwap,'#f2c500',False),('Step VWAP',step_vwap,'#f2c500',True),('Current',current,'#111',False)]:
        yy=y(p)
        if dot: dashed(left,yy,right,color)
        else: d.line([left,yy,right,yy],fill=color,width=5)
        d.rounded_rectangle([left+12,yy-30,left+390,yy-4],radius=6,fill='white')
        d.text((left+22,yy-28),f'{name}: {_fmt(p)}',fill='#9a7800' if color=='#f2c500' else color,font=fmb)
    # Point of Control overlays: ladder POC solid green, active-step POC dotted green.
    for name,p,dot in [('Ladder POC',ladder_poc,False),('Step POC',step_poc,True)]:
        yy=y(p)
        if dot:
            dashed(left,yy,right,'#138a36',width=3,dash=8,gap=8)
        else:
            d.line([left,yy,right,yy],fill='#138a36',width=3)
        d.rounded_rectangle([left+410,yy-30,left+750,yy-4],radius=6,fill='white')
        d.text((left+420,yy-28),f'{name}: {_fmt(p)}',fill='#138a36',font=fmb)
    pn=float(levels[n]['price']); tp=float(levels[n-1]['price']) if n>=1 else float(levels[0]['price'])
    pn1=float(levels[n+1]['price']) if n+1 < len(levels) else pn
    pn2=float(levels[n+2]['price']) if n+2 < len(levels) else pn1
    d.text((60,1500),f'Active: P{n}={_fmt(pn)} · TP=P{max(n-1,0)}={_fmt(tp)} · Next P{n+1}={_fmt(pn1)} · P{n+2}={_fmt(pn2)}',fill='#222',font=fmb)
    d.text((60,1532),f'VWAP: ladder={_fmt(ladder_vwap)} · active step={_fmt(step_vwap)} · POC: ladder={_fmt(ladder_poc)} · step={_fmt(step_poc)} · current={_fmt(current)}',fill='#222',font=fmb)
    out=_OUT/f"backtest_{symbol}_{market}_{side.value.lower()}_{int(time.time()*1000)}.jpg"
    img.save(out, 'JPEG', quality=92, optimize=True)
    return str(out)


def _draw_svg(symbol, market, side, levels, n, ladder_vwap, step_vwap, current):
    W,H=1200,1600; left,right=170,1100; top,bottom=110,1440
    prices=[float(x['price']) for x in levels]+[ladder_vwap,step_vwap,current]
    pmin=min(prices); pmax=max(prices); pad=(pmax-pmin)*0.07 or 1; pmin-=pad; pmax+=pad
    def y(p): return bottom-(float(p)-pmin)/(pmax-pmin)*(bottom-top)
    title=f"{symbol} {market.upper()} {'BUY' if side is Side.BUY else 'SELL'} GoldenFibo Backtest"
    chunks=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}"><rect width="100%" height="100%" fill="#fbfbf8"/>']
    chunks.append(f'<text x="60" y="55" font-size="30" font-family="Arial" font-weight="800" fill="#111">{title}</text>')
    chunks.append(f'<rect x="{left}" y="{top}" width="{right-left}" height="{bottom-top}" fill="#fff" stroke="#ddd"/>')
    line_color="#77aaff" if side is Side.BUY else "#ff7a7a"; active="#0058ff" if side is Side.BUY else "#d71920"
    for item in levels:
        i=int(str(item['level'])[1:]); p=float(item['price']); yy=y(p); is_active=i==n
        chunks.append(f'<line x1="{left}" x2="{right}" y1="{yy:.2f}" y2="{yy:.2f}" stroke="{active if is_active else line_color}" stroke-width="{5 if is_active else 2}" opacity="{1 if is_active else .62}"/>')
        role=item.get('role') or ''
        fill_color = active if is_active else "#933"
        level_name = item["level"]
        chunks.append(f'<text x="{right-12}" y="{yy-7:.2f}" text-anchor="end" font-family="Arial" font-size="18" font-weight="{800 if is_active else 500}" fill="{fill_color}">{level_name} {_fmt(p)} {role}</text>')
    for name,p,color,dash in [("Ladder VWAP",ladder_vwap,"#f2c500",""),("Step VWAP",step_vwap,"#f2c500","10 8"),("Current",current,"#111","")]:
        yy=y(p); extra=f' stroke-dasharray="{dash}"' if dash else ''
        chunks.append(f'<line x1="{left}" x2="{right}" y1="{yy:.2f}" y2="{yy:.2f}" stroke="{color}" stroke-width="5"{extra}/><text x="{left+20}" y="{yy-8:.2f}" font-family="Arial" font-size="18" font-weight="800" fill="{color}">{name}: {_fmt(p)}</text>')
    chunks.append('</svg>')
    out=_OUT/f"backtest_{symbol}_{market}_{side.value.lower()}_{int(time.time()*1000)}.svg"
    out.write_text("".join(chunks))
    return str(out)


def _chat_key_from_message(msg: Any) -> Tuple[Any, ...]:
    chat=getattr(msg,"chat",None); chat_id=getattr(chat,"id",None) if chat is not None else None; thread=getattr(msg,"message_thread_id",None)
    return (str(chat_id), thread) if thread is not None else (str(chat_id),)

def _chat_id_from_message(msg: Any) -> Optional[str]:
    chat=getattr(msg,"chat",None); cid=getattr(chat,"id",None) if chat is not None else None
    return str(cid) if cid is not None else None

def _metadata_from_message(msg: Any) -> Optional[Dict[str, Any]]:
    thread=getattr(msg,"message_thread_id",None)
    return {"thread_id": thread} if thread is not None else None

async def _send_screen(adapter: Any, chat_id: str, screen: Screen, *, metadata: Optional[Dict[str, Any]]=None) -> None:
    send=getattr(adapter,"send_inline_keyboard",None)
    if callable(send):
        await send(chat_id=chat_id, text=screen.text, buttons=screen.buttons, callback_prefix="backtest", metadata=metadata)
    else:
        await adapter.send(chat_id, screen.text, metadata=metadata)
    # Send visual attachments. JPEG/PNG go as native Telegram photos; others as documents.
    for path in screen.attachments:
        lower = str(path).lower()
        if lower.endswith((".jpg", ".jpeg", ".png")):
            send_img = getattr(adapter, "send_image_file", None)
            if callable(send_img):
                await send_img(chat_id=chat_id, image_path=path, caption=Path(path).name, metadata=metadata)
                continue
        doc=getattr(adapter,"send_document",None)
        if callable(doc):
            await doc(chat_id=chat_id, file_path=path, caption=Path(path).name, metadata=metadata)

async def handle_backtest_command(adapter: Any, msg: Any) -> bool:
    text=(getattr(msg,"text","") or "").strip()
    if not text.startswith("/"): return False
    cmd=text.split(None,1)[0].lstrip('/').split('@',1)[0].lower()
    if cmd != "backtest": return False
    key=_chat_key_from_message(msg); screen=_WIZARD.open(key); cid=_chat_id_from_message(msg)
    if cid: await _send_screen(adapter,cid,screen,metadata=_metadata_from_message(msg))
    return True

async def handle_backtest_callback(adapter: Any, query: Any, data: str) -> None:
    try:
        suffix=data[len("backtest:"):] if data.startswith("backtest:") else data
        try: await query.answer()
        except Exception: pass
        msg=getattr(query,"message",None); key=_chat_key_from_message(msg)
        if suffix == "restart": screen=_WIZARD.open(key)
        elif suffix == "run":
            try: await query.edit_message_text("⏳ Running Binance backtest… this can take a minute.", reply_markup=None)
            except Exception: pass
            screen=await asyncio.to_thread(_WIZARD.handle_callback,key,suffix)
        else:
            screen=await asyncio.to_thread(_WIZARD.handle_callback,key,suffix)
        from plugins.platforms.telegram.adapter import InlineKeyboardButton, InlineKeyboardMarkup
        rows=[]
        for row in screen.buttons:
            br=[]
            for b in row:
                br.append(InlineKeyboardButton(str(b.get('text','')), callback_data=f"backtest:{b.get('callback_data','')}"))
            if br: rows.append(br)
        kb=InlineKeyboardMarkup(rows) if rows else None
        try:
            await query.edit_message_text(screen.text, reply_markup=kb)
        except Exception:
            cid=_chat_id_from_message(msg)
            if cid: await _send_screen(adapter,cid,screen,metadata=_metadata_from_message(msg))
        cid=_chat_id_from_message(msg)
        if cid:
            for path in screen.attachments:
                lower = str(path).lower()
                if lower.endswith((".jpg", ".jpeg", ".png")):
                    send_img = getattr(adapter, "send_image_file", None)
                    if callable(send_img):
                        await send_img(chat_id=cid, image_path=path, caption=Path(path).name, metadata=_metadata_from_message(msg))
                        continue
                doc=getattr(adapter,"send_document",None)
                if callable(doc): await doc(chat_id=cid,file_path=path,caption=Path(path).name,metadata=_metadata_from_message(msg))
    except Exception as exc:
        logger.error("backtest callback failed: %s", exc, exc_info=True)

async def handle_backtest_text(adapter: Any, msg: Any) -> bool:
    key=_chat_key_from_message(msg); screen=await asyncio.to_thread(_WIZARD.handle_text,key,getattr(msg,"text","") or "")
    if screen is None: return False
    cid=_chat_id_from_message(msg)
    if cid: await _send_screen(adapter,cid,screen,metadata=_metadata_from_message(msg))
    return True

__all__=["handle_backtest_command","handle_backtest_callback","handle_backtest_text","BacktestWizard"]
