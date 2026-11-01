"""MT5Linux write-path helpers: pending orders, positions, ladders (RPyC).

Called only while the caller holds the shared terminal lock and has already
switched/verified the target account. Never logs secrets or credentials.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

# Match MetaTrader agent magic for cross-tool visibility where useful.
MT5LINUX_MAGIC = 26092601

# MT5 enums (mirror MetaTrader5 package constants)
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TYPE_BUY_LIMIT = 2
ORDER_TYPE_SELL_LIMIT = 3
ORDER_TYPE_BUY_STOP = 4
ORDER_TYPE_SELL_STOP = 5

TRADE_ACTION_DEAL = 1
TRADE_ACTION_PENDING = 5
TRADE_ACTION_SLTP = 6
TRADE_ACTION_MODIFY = 7
TRADE_ACTION_REMOVE = 8

ORDER_TIME_GTC = 0
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2

# Trade retcodes considered success
_RETCODE_DONE = {10008, 10009}  # PLACED, DONE
_RETCODE_DONE_PARTIAL = {10010}  # DONE_PARTIAL


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def ladder_store_dir() -> Path:
    d = _hermes_home() / "mt5linux" / "ladders"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dec(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        if isinstance(value, float):
            d = Decimal(str(value))
        elif isinstance(value, Decimal):
            d = value
        else:
            d = Decimal(str(value).strip().replace(",", ""))
        if not d.is_finite():
            return None
        return d
    except (InvalidOperation, ValueError, TypeError):
        return None


def _dec_text(value: Any) -> str:
    d = _dec(value)
    if d is None:
        return "0"
    return format(d, "f")


def _json_num(value: Decimal) -> float | int:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _short_comment(request_id: str) -> str:
    return "XL:" + str(request_id).replace("-", "")[:12]


def _coerce_request_id(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 8:
        return text
    return str(uuid.uuid4())


def remote_symbol_trade_meta(client: Any, symbol: str) -> Dict[str, Any]:
    """Fetch trade constraints for symbol (must run under lock)."""
    code = f"""
import MetaTrader5 as m
sym = {symbol!r}
info = m.symbol_info(sym)
if info is None:
    m.symbol_select(sym, True)
    info = m.symbol_info(sym)
tick = m.symbol_info_tick(sym)
meta = None
if info is not None:
    filling = int(getattr(info, 'filling_mode', 0) or 0)
    # Prefer RETURN, then IOC, then FOK based on symbol flags (bitmask).
    # SYMBOL_FILLING_FOK=1, IOC=2, RETURN=4 in many builds — try RETURN first.
    fill_mode = 2  # ORDER_FILLING_RETURN default try
    if filling & 4:
        fill_mode = 2
    elif filling & 2:
        fill_mode = 1
    elif filling & 1:
        fill_mode = 0
    meta = {{
        'name': str(info.name),
        'digits': int(info.digits),
        'point': float(info.point),
        'trade_tick_size': float(getattr(info, 'trade_tick_size', 0) or info.point or 0),
        'volume_min': float(getattr(info, 'volume_min', 0) or 0),
        'volume_max': float(getattr(info, 'volume_max', 0) or 0),
        'volume_step': float(getattr(info, 'volume_step', 0) or 0),
        'trade_stops_level': int(getattr(info, 'trade_stops_level', 0) or 0),
        'trade_freeze_level': int(getattr(info, 'trade_freeze_level', 0) or 0),
        'trade_contract_size': float(getattr(info, 'trade_contract_size', 0) or 0),
        'filling_mode_flags': filling,
        'filling_mode': fill_mode,
        'trade_mode': int(getattr(info, 'trade_mode', 0) or 0),
        'bid': float(tick.bid) if tick is not None else None,
        'ask': float(tick.ask) if tick is not None else None,
    }}
trade_meta = meta
"""
    client.execute(code)
    meta = client.eval("trade_meta")
    return meta if isinstance(meta, dict) else {}


def _normalize_volume(volume: Decimal, meta: Mapping[str, Any]) -> Decimal:
    step = _dec(meta.get("volume_step")) or Decimal("0.01")
    vmin = _dec(meta.get("volume_min")) or step
    vmax = _dec(meta.get("volume_max")) or Decimal("1000")
    if step <= 0:
        step = Decimal("0.01")
    units = (volume / step).to_integral_value(rounding=ROUND_DOWN)
    out = units * step
    if out < vmin:
        out = vmin
    if out > vmax:
        out = vmax
    return out


def _normalize_price(price: Decimal, meta: Mapping[str, Any]) -> Decimal:
    tick = _dec(meta.get("trade_tick_size")) or _dec(meta.get("point")) or Decimal("0.01")
    if tick <= 0:
        return price
    units = (price / tick).to_integral_value(rounding=ROUND_DOWN)
    # half-up via +0.5 tick when remainder large — keep ROUND_DOWN then optional adjust
    rem = price - units * tick
    if rem >= tick / 2:
        units += 1
    return units * tick


def _map_order_type(side: str, order_type: str) -> Optional[int]:
    side_u = side.upper()
    ot = order_type.upper().replace("-", "_").replace(" ", "_")
    if ot in {"LIMIT", "BUY_LIMIT", "SELL_LIMIT"}:
        if side_u == "BUY":
            return ORDER_TYPE_BUY_LIMIT
        if side_u == "SELL":
            return ORDER_TYPE_SELL_LIMIT
    if ot in {"STOP", "BUY_STOP", "SELL_STOP"}:
        if side_u == "BUY":
            return ORDER_TYPE_BUY_STOP
        if side_u == "SELL":
            return ORDER_TYPE_SELL_STOP
    if ot in {"BUY_LIMIT"}:
        return ORDER_TYPE_BUY_LIMIT
    if ot in {"SELL_LIMIT"}:
        return ORDER_TYPE_SELL_LIMIT
    if ot in {"BUY_STOP"}:
        return ORDER_TYPE_BUY_STOP
    if ot in {"SELL_STOP"}:
        return ORDER_TYPE_SELL_STOP
    return None


def order_send_pending(
    client: Any,
    *,
    symbol: str,
    side: str,
    order_type: str,
    volume: Decimal,
    price: Decimal,
    sl: Optional[Decimal] = None,
    tp: Optional[Decimal] = None,
    comment: str = "",
    magic: int = MT5LINUX_MAGIC,
    expiration: int = 0,
) -> Dict[str, Any]:
    """Place a pending LIMIT/STOP order. Returns result dict (never raises on broker reject)."""
    meta = remote_symbol_trade_meta(client, symbol)
    if not meta:
        return {"ok": False, "retcode": -1, "comment": "symbol not found", "error": "INSTRUMENT_NOT_FOUND"}
    otype = _map_order_type(side, order_type)
    if otype is None:
        return {"ok": False, "retcode": -1, "error": "UNSUPPORTED_ORDER_TYPE", "comment": f"bad type {order_type}"}
    vol = _normalize_volume(volume, meta)
    px = _normalize_price(price, meta)
    sl_v = float(_normalize_price(sl, meta)) if sl is not None and sl > 0 else 0.0
    tp_v = float(_normalize_price(tp, meta)) if tp is not None and tp > 0 else 0.0
    fill = int(meta.get("filling_mode") or ORDER_FILLING_RETURN)
    code = f"""
import MetaTrader5 as m
req = {{
    'action': {TRADE_ACTION_PENDING},
    'symbol': {str(meta.get('name') or symbol)!r},
    'volume': {float(vol)},
    'type': {int(otype)},
    'price': {float(px)},
    'sl': {sl_v},
    'tp': {tp_v},
    'deviation': 20,
    'magic': {int(magic)},
    'comment': {str(comment)[:31]!r},
    'type_time': {ORDER_TIME_GTC},
    'type_filling': {fill},
}}
if {int(expiration)} > 0:
    req['type_time'] = m.ORDER_TIME_SPECIFIED if hasattr(m, 'ORDER_TIME_SPECIFIED') else 1
    req['expiration'] = {int(expiration)}
result = m.order_send(req)
out = None
if result is None:
    out = {{'ok': False, 'retcode': -1, 'comment': str(m.last_error()), 'order': 0, 'deal': 0, 'request': req}}
else:
    out = {{
        'ok': bool(result.retcode in (m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_PLACED, 10008, 10009, 10010)),
        'retcode': int(result.retcode),
        'comment': str(result.comment),
        'order': int(result.order),
        'deal': int(getattr(result, 'deal', 0) or 0),
        'volume': float(getattr(result, 'volume', 0) or 0),
        'price': float(getattr(result, 'price', 0) or 0),
        'request_id': int(getattr(result, 'request_id', 0) or 0),
        'retcode_external': int(getattr(result, 'retcode_external', 0) or 0),
    }}
order_send_out = out
"""
    try:
        client.execute(code)
        out = client.eval("order_send_out")
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "retcode": -2,
            "error": "AMBIGUOUS_EXECUTION",
            "comment": f"order_send transport error: {type(exc).__name__}",
            "ambiguous": True,
        }
    if not isinstance(out, dict):
        return {"ok": False, "retcode": -1, "error": "MALFORMED_RESULT", "comment": "bad remote result"}
    out.setdefault("symbol", meta.get("name") or symbol)
    out.setdefault("side", side.upper())
    out.setdefault("order_type", order_type.upper())
    out.setdefault("requested_volume", float(volume))
    out.setdefault("submitted_volume", float(vol))
    out.setdefault("requested_price", float(price))
    out.setdefault("submitted_price", float(px))
    out.setdefault("magic", magic)
    out.setdefault("comment_sent", comment[:31])
    return out


def order_modify_pending(
    client: Any,
    *,
    ticket: int,
    price: Optional[Decimal] = None,
    sl: Optional[Decimal] = None,
    tp: Optional[Decimal] = None,
    volume: Optional[Decimal] = None,
) -> Dict[str, Any]:
    if volume is not None:
        return {
            "ok": False,
            "retcode": -1,
            "error": "UNSUPPORTED_PARAMETER",
            "comment": "Pending order volume modification is not supported via MT5 trade modify; cancel and replace.",
        }
    code = f"""
import MetaTrader5 as m
ticket = {int(ticket)}
orders = m.orders_get(ticket=ticket)
if not orders:
    order_mod_out = {{'ok': False, 'retcode': -1, 'error': 'ORDER_NOT_FOUND', 'comment': 'pending order not found'}}
else:
    o = orders[0]
    info = m.symbol_info(o.symbol)
    price = {float(price) if price is not None else 'None'}
    sl = {float(sl) if sl is not None else 'None'}
    tp = {float(tp) if tp is not None else 'None'}
    req = {{
        'action': {TRADE_ACTION_MODIFY},
        'order': ticket,
        'symbol': str(o.symbol),
        'volume': float(o.volume_current),
        'price': float(o.price_open) if price is None else float(price),
        'sl': float(o.sl) if sl is None else float(sl),
        'tp': float(o.tp) if tp is None else float(tp),
        'type_time': int(o.type_time),
        'type_filling': int(getattr(o, 'type_filling', 0) or 0),
        'magic': int(o.magic),
        'comment': str(o.comment)[:31],
    }}
    if int(o.time_expiration or 0) > 0:
        req['expiration'] = int(o.time_expiration)
    result = m.order_send(req)
    if result is None:
        order_mod_out = {{'ok': False, 'retcode': -1, 'comment': str(m.last_error())}}
    else:
        order_mod_out = {{
            'ok': bool(result.retcode in (m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_PLACED, 10008, 10009)),
            'retcode': int(result.retcode),
            'comment': str(result.comment),
            'order': int(result.order or ticket),
        }}
"""
    try:
        client.execute(code)
        out = client.eval("order_mod_out")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "retcode": -2, "error": "AMBIGUOUS_EXECUTION", "ambiguous": True, "comment": type(exc).__name__}
    return out if isinstance(out, dict) else {"ok": False, "error": "MALFORMED_RESULT"}


def order_cancel(client: Any, *, ticket: int) -> Dict[str, Any]:
    code = f"""
import MetaTrader5 as m
ticket = {int(ticket)}
req = {{'action': {TRADE_ACTION_REMOVE}, 'order': ticket}}
result = m.order_send(req)
if result is None:
    order_cancel_out = {{'ok': False, 'retcode': -1, 'comment': str(m.last_error()), 'order': ticket}}
else:
    order_cancel_out = {{
        'ok': bool(result.retcode in (m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_PLACED, 10008, 10009)),
        'retcode': int(result.retcode),
        'comment': str(result.comment),
        'order': int(result.order or ticket),
    }}
"""
    try:
        client.execute(code)
        out = client.eval("order_cancel_out")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "retcode": -2, "error": "AMBIGUOUS_EXECUTION", "ambiguous": True, "comment": type(exc).__name__}
    return out if isinstance(out, dict) else {"ok": False, "error": "MALFORMED_RESULT"}


def position_set_sltp(client: Any, *, ticket: int, sl: Optional[Decimal], tp: Optional[Decimal]) -> Dict[str, Any]:
    code = f"""
import MetaTrader5 as m
ticket = {int(ticket)}
positions = m.positions_get(ticket=ticket)
if not positions:
    pos_sltp_out = {{'ok': False, 'retcode': -1, 'error': 'POSITION_NOT_FOUND', 'comment': 'position not found'}}
else:
    p = positions[0]
    sl = {float(sl) if sl is not None else 'None'}
    tp = {float(tp) if tp is not None else 'None'}
    req = {{
        'action': {TRADE_ACTION_SLTP},
        'position': ticket,
        'symbol': str(p.symbol),
        'sl': float(p.sl) if sl is None else float(sl),
        'tp': float(p.tp) if tp is None else float(tp),
        'magic': int(p.magic),
    }}
    result = m.order_send(req)
    if result is None:
        pos_sltp_out = {{'ok': False, 'retcode': -1, 'comment': str(m.last_error())}}
    else:
        pos_sltp_out = {{
            'ok': bool(result.retcode in (m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_PLACED, 10008, 10009)),
            'retcode': int(result.retcode),
            'comment': str(result.comment),
            'position': ticket,
            'symbol': str(p.symbol),
            'side': 'BUY' if int(p.type)==0 else 'SELL',
            'sl': req['sl'],
            'tp': req['tp'],
        }}
"""
    try:
        client.execute(code)
        out = client.eval("pos_sltp_out")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "retcode": -2, "error": "AMBIGUOUS_EXECUTION", "ambiguous": True, "comment": type(exc).__name__}
    return out if isinstance(out, dict) else {"ok": False, "error": "MALFORMED_RESULT"}


def position_close(client: Any, *, ticket: int, volume: Optional[Decimal] = None) -> Dict[str, Any]:
    """Full or partial close by ticket (hedging-safe)."""
    vol_expr = "None" if volume is None else str(float(volume))
    code = f"""
import MetaTrader5 as m
ticket = {int(ticket)}
positions = m.positions_get(ticket=ticket)
if not positions:
    pos_close_out = {{'ok': False, 'retcode': -1, 'error': 'POSITION_NOT_FOUND', 'comment': 'position not found'}}
else:
    p = positions[0]
    vol = {vol_expr}
    if vol is None or vol <= 0 or vol >= float(p.volume):
        close_vol = float(p.volume)
    else:
        close_vol = float(vol)
    # opposite deal
    otype = m.ORDER_TYPE_SELL if int(p.type)==0 else m.ORDER_TYPE_BUY
    info = m.symbol_info(p.symbol)
    tick = m.symbol_info_tick(p.symbol)
    price = float(tick.bid if int(p.type)==0 else tick.ask) if tick is not None else 0.0
    filling = int(getattr(info, 'filling_mode', 0) or 0) if info is not None else 0
    fill_mode = 2 if filling & 4 else (1 if filling & 2 else 0)
    req = {{
        'action': {TRADE_ACTION_DEAL},
        'position': ticket,
        'symbol': str(p.symbol),
        'volume': close_vol,
        'type': otype,
        'price': price,
        'deviation': 30,
        'magic': int(p.magic),
        'comment': 'XL:close',
        'type_time': m.ORDER_TIME_GTC,
        'type_filling': fill_mode,
    }}
    result = m.order_send(req)
    if result is None:
        pos_close_out = {{'ok': False, 'retcode': -1, 'comment': str(m.last_error()), 'ambiguous': False}}
    else:
        pos_close_out = {{
            'ok': bool(result.retcode in (m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_PLACED, 10008, 10009, 10010)),
            'retcode': int(result.retcode),
            'comment': str(result.comment),
            'deal': int(getattr(result, 'deal', 0) or 0),
            'order': int(getattr(result, 'order', 0) or 0),
            'position': ticket,
            'symbol': str(p.symbol),
            'closed_volume': close_vol,
            'side': 'BUY' if int(p.type)==0 else 'SELL',
        }}
"""
    try:
        client.execute(code)
        out = client.eval("pos_close_out")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "retcode": -2, "error": "AMBIGUOUS_EXECUTION", "ambiguous": True, "comment": type(exc).__name__}
    return out if isinstance(out, dict) else {"ok": False, "error": "MALFORMED_RESULT"}


def reconcile_order(client: Any, ticket: int) -> Optional[Dict[str, Any]]:
    code = f"""
import MetaTrader5 as m
ticket = {int(ticket)}
orders = m.orders_get(ticket=ticket)
if orders:
    o = orders[0]
    recon = {{'present': True, 'ticket': int(o.ticket), 'symbol': str(o.symbol), 'type': int(o.type),
              'volume': float(o.volume_current), 'price': float(o.price_open), 'sl': float(o.sl), 'tp': float(o.tp)}}
else:
    recon = {{'present': False, 'ticket': ticket}}
"""
    try:
        client.execute(code)
        out = client.eval("recon")
        return out if isinstance(out, dict) else None
    except Exception:  # noqa: BLE001
        return None


def save_ladder_record(record: Mapping[str, Any]) -> Path:
    ladder_id = str(record.get("ladder_id") or uuid.uuid4())
    path = ladder_store_dir() / f"{ladder_id}.json"
    payload = dict(record)
    payload["ladder_id"] = ladder_id
    payload["updated_at"] = time.time()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return path


def load_ladder_record(ladder_id: str) -> Optional[Dict[str, Any]]:
    path = ladder_store_dir() / f"{ladder_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_account_orders(client: Any, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    code = f"""
import MetaTrader5 as m
sym = {symbol!r}
if sym:
    rows = m.orders_get(symbol=sym) or ()
else:
    rows = m.orders_get() or ()
ord_list = []
for o in rows:
    ord_list.append({{
        'ticket': int(o.ticket), 'symbol': str(o.symbol), 'type': int(o.type),
        'volume_current': float(o.volume_current), 'price_open': float(o.price_open),
        'sl': float(o.sl), 'tp': float(o.tp), 'magic': int(o.magic), 'comment': str(o.comment),
    }})
"""
    client.execute(code)
    out = client.eval("ord_list")
    return list(out) if isinstance(out, list) else []
