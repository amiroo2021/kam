"""TradeDesk-backed service layer for TradeMenu (read + write via TradeDesk)."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, is_dataclass
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from ..tradedesk import TradeDesk, get_tradedesk
from .formatting import format_pnl, format_price, format_size

logger = logging.getLogger("trademenu")

# Short in-process TTL so Positions + Orders share one agent fetch and
# rapid UI polls do not re-hit multi-dex Hyperliquid open-order fanout.
_POSITIONS_CACHE_TTL_SECONDS = 12.0
_RESOLVE_CACHE_TTL_SECONDS = 60.0


def _account_alias(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("account") or "").strip()
    return str(entry or "").strip()


def _account_label(entry: Any) -> str:
    if isinstance(entry, dict):
        label = str(entry.get("label") or entry.get("account") or "").strip()
        chain = str(entry.get("chain") or "").strip()
        if chain and chain.lower() not in label.lower():
            return f"{label} — {chain}" if label else chain
        return label or _account_alias(entry)
    return str(entry or "").strip()


def _to_plain(obj: Any) -> Any:
    if obj is None:
        return None
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    return obj


def _dec(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    return d


def _fmt_dec(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _derive_mark(side: str, entry: Any, size: Any, pnl: Any) -> Optional[str]:
    """Derive mark from entry/size/pnl when agent omits mark.

    long:  pnl = size * (mark - entry)  => mark = entry + pnl/size
    short: pnl = size * (entry - mark)  => mark = entry - pnl/size
    """
    e = _dec(entry)
    s = _dec(size)
    p = _dec(pnl)
    if e is None or s is None or p is None or s == 0:
        return None
    side_l = str(side or "").strip().lower()
    if side_l in {"long", "buy"}:
        mark = e + (p / s)
    elif side_l in {"short", "sell"}:
        mark = e - (p / s)
    else:
        return None
    return _fmt_dec(mark)


class TradeMenuService:
    """Thin facade over TradeDesk — no exchange-native secrets escape."""

    def __init__(self, desk: Optional[TradeDesk] = None, cache_ttl: float = _POSITIONS_CACHE_TTL_SECONDS) -> None:
        self.desk = desk or get_tradedesk()
        self.cache_ttl = float(cache_ttl)
        self.resolve_cache_ttl = _RESOLVE_CACHE_TTL_SECONDS
        self._lock = Lock()
        # key -> (expires_at, timing_ms, CanonicalResponse-like dict payload)
        self._po_cache: Dict[Tuple[str, str], Tuple[float, float, Dict[str, Any]]] = {}
        # (exchange, account, symbol_key) -> (expires_at, payload)
        self._resolve_cache: Dict[Tuple[str, str, str], Tuple[float, Dict[str, Any]]] = {}

    def invalidate_positions_cache(self, exchange: str = "", account: str = "") -> None:
        with self._lock:
            if exchange and account:
                self._po_cache.pop((str(exchange), str(account)), None)
            else:
                self._po_cache.clear()

    def list_exchanges(self) -> List[str]:
        return list(self.desk.list_exchanges())

    def list_accounts(self, exchange: str) -> List[Dict[str, str]]:
        ex = str(exchange or "").strip()
        if ex not in self.desk.list_exchanges():
            return []
        out: List[Dict[str, str]] = []
        for entry in self.desk.list_accounts(ex):
            alias = _account_alias(entry)
            if not alias:
                continue
            out.append({"account": alias, "label": _account_label(entry)})
        return out

    def validate_exchange_account(self, exchange: str, account: str) -> Optional[str]:
        ex = str(exchange or "").strip()
        acct = str(account or "").strip()
        if ex not in self.desk.list_exchanges():
            return "UNKNOWN_EXCHANGE"
        aliases = {_account_alias(a) for a in self.desk.list_accounts(ex)}
        if acct not in aliases:
            return "UNKNOWN_ACCOUNT"
        return None

    def resolve_instrument(self, exchange: str, account: str, symbol: str) -> Dict[str, Any]:
        err = self.validate_exchange_account(exchange, account)
        if err:
            return {"success": False, "error": {"code": err, "message": err.replace("_", " ").title()}}
        requested = str(symbol or "").strip()
        cache_key = (str(exchange), str(account), requested.casefold())
        now = time.time()
        with self._lock:
            hit = self._resolve_cache.get(cache_key)
            if hit and hit[0] > now:
                cached = dict(hit[1])
                cached["cache_hit"] = True
                timing = dict(cached.get("timing_ms") or {})
                timing["cached"] = True
                cached["timing_ms"] = timing
                return cached

        t0 = time.perf_counter()
        resp = self.desk.execute(
            {
                "operation": "resolve_instrument",
                "exchange": exchange,
                "account": account,
                "symbol": requested,
            }
        )
        desk_ms = (time.perf_counter() - t0) * 1000.0
        data: Dict[str, Any] = {
            "success": bool(resp.success),
            "exchange": exchange,
            "account": account,
            "requested_symbol": requested,
            "cache_hit": False,
            "timing_ms": {"tradedesk_ms": round(desk_ms, 1)},
        }
        if resp.instrument is not None:
            inst = _to_plain(resp.instrument)
            data["instrument"] = inst
            data["native_symbol"] = inst.get("symbol")
            data["display"] = f"{requested} → {inst.get('symbol')}"
            meta = {
                "price_increment": inst.get("price_increment") or inst.get("tick_size"),
                "size_increment": inst.get("size_increment") or inst.get("lot_size"),
                "price_decimals": inst.get("price_decimals"),
                "size_decimals": inst.get("size_decimals") or inst.get("sz_decimals"),
            }
            data["format_meta"] = {k: v for k, v in meta.items() if v is not None}
        payload = _to_plain(resp.data) if resp.data else None
        if isinstance(payload, dict):
            candidates = payload.get("candidates") or payload.get("instruments")
            if candidates:
                data["candidates"] = candidates
            data["data"] = {k: v for k, v in payload.items() if k not in {"candidates", "instruments"}}
        if resp.error is not None:
            data["error"] = _to_plain(resp.error)
            data["display"] = f"{requested} → unresolved"
        if resp.success and data.get("native_symbol"):
            with self._lock:
                self._resolve_cache[cache_key] = (now + self.resolve_cache_ttl, dict(data))
        return data

    def market_price(self, exchange: str, account: str, symbol: str) -> Dict[str, Any]:
        err = self.validate_exchange_account(exchange, account)
        if err:
            return {"success": False, "error": {"code": err, "message": err.replace("_", " ").title()}}
        t0 = time.perf_counter()
        resp = self.desk.execute(
            {
                "operation": "market_price",
                "exchange": exchange,
                "account": account,
                "symbol": symbol,
            }
        )
        desk_ms = (time.perf_counter() - t0) * 1000.0
        data: Dict[str, Any] = {
            "success": bool(resp.success),
            "exchange": exchange,
            "account": account,
            "symbol": symbol,
            "timing_ms": {"tradedesk_ms": round(desk_ms, 1)},
        }
        if resp.market_price is not None:
            mp = _to_plain(resp.market_price)
            data["market_price"] = mp
            data["price"] = mp.get("price") or mp.get("mark_price") or mp.get("last_external_price")
        if resp.error is not None:
            data["error"] = _to_plain(resp.error)
        return data

    def _fetch_positions_orders_payload(self, exchange: str, account: str) -> Dict[str, Any]:
        """Single TradeDesk positions_orders call with short TTL cache."""
        key = (str(exchange), str(account))
        now = time.time()
        with self._lock:
            hit = self._po_cache.get(key)
            if hit and hit[0] > now:
                payload = dict(hit[2])
                payload["cache_hit"] = True
                payload["timing_ms"] = {
                    "tradedesk_ms": 0.0,
                    "cached_tradedesk_ms": hit[1],
                    "cache_ttl_s": self.cache_ttl,
                }
                return payload

        caps = set(self.desk.capabilities(exchange))
        if "positions_orders" not in caps and "positions_management" not in caps:
            return {
                "success": False,
                "error": {"code": "UNSUPPORTED", "message": "Exchange does not expose positions/orders."},
                "positions": [],
                "order_groups": [],
                "open_order_count": 0,
                "cache_hit": False,
                "timing_ms": {"tradedesk_ms": 0.0},
            }

        # Prefer positions_orders so order_groups come from the same canonical
        # aggregation Telegram /trade already uses.
        op = "positions_orders" if "positions_orders" in caps else "positions_management"
        t0 = time.perf_counter()
        resp = self.desk.execute({"operation": op, "exchange": exchange, "account": account})
        desk_ms = (time.perf_counter() - t0) * 1000.0

        positions = []
        for p in resp.positions or []:
            row = _to_plain(p)
            mark = row.get("mark")
            if mark in (None, "", "—"):
                mark = _derive_mark(row.get("side"), row.get("entry_price"), row.get("size"), row.get("pnl"))
            # Lightweight meta from exchange_instrument if present
            meta: Dict[str, Any] = {}
            ei = row.get("exchange_instrument")
            if isinstance(ei, dict):
                meta = {
                    "price_increment": ei.get("price_increment") or ei.get("tick_size"),
                    "size_increment": ei.get("size_increment") or ei.get("lot_size"),
                    "price_decimals": ei.get("price_decimals"),
                    "size_decimals": ei.get("size_decimals") or ei.get("sz_decimals"),
                }
            positions.append(
                {
                    "symbol": row.get("symbol"),
                    "side": row.get("side"),
                    "size": row.get("size"),
                    "entry": row.get("entry_price"),
                    "mark": mark,
                    "pnl": row.get("pnl"),
                    "sl": row.get("sl"),
                    "tp": row.get("tp"),
                    "exchange_instrument": row.get("exchange_instrument"),
                    "format_meta": {k: v for k, v in meta.items() if v is not None},
                    "display": {
                        "size": format_size(row.get("size"), meta),
                        "entry": format_price(row.get("entry_price"), meta),
                        "mark": format_price(mark, meta),
                        "pnl": format_pnl(row.get("pnl"), meta),
                        "sl": format_price(row.get("sl"), meta),
                        "tp": format_price(row.get("tp"), meta),
                    },
                }
            )

        groups = []
        for g in resp.order_groups or []:
            plain = _to_plain(g)
            side = str(plain.get("side") or "").lower()
            # Telegram groups are side-based limit ladders; label as LIMIT.
            meta = {}
            groups.append(
                {
                    "symbol": plain.get("symbol"),
                    "side": side,
                    "type": (
                        "limit"
                        if str(plain.get("classification") or "entry_limit") == "entry_limit"
                        else str(plain.get("classification") or "other")
                    ),
                    "classification": plain.get("classification") or "entry_limit",
                    "display_type": plain.get("display_type")
                    or (
                        f"{side.upper()} LIMIT"
                        if str(plain.get("classification") or "entry_limit") == "entry_limit"
                        else str(plain.get("classification") or "LIMIT").replace("_", " ").upper()
                    ),
                    "count": int(plain.get("order_count") or 0),
                    "total_remaining_size": plain.get("total_size"),
                    "min_price": plain.get("min_price"),
                    "max_price": plain.get("max_price"),
                    "vwap": plain.get("vwap"),
                    "reduce_only": bool(plain.get("reduce_only")),
                    "trigger_price": plain.get("trigger_price"),
                    "limit_price": plain.get("limit_price"),
                    "order_ids": plain.get("order_ids"),
                    "display": {
                        "total_remaining_size": format_size(plain.get("total_size"), meta),
                        "min_price": format_price(plain.get("min_price"), meta),
                        "max_price": format_price(plain.get("max_price"), meta),
                        "vwap": format_price(plain.get("vwap"), meta),
                        "trigger_price": format_price(plain.get("trigger_price"), meta)
                        if plain.get("trigger_price") and "–" not in str(plain.get("trigger_price"))
                        else (plain.get("trigger_price") or "—"),
                        "limit_price": format_price(plain.get("limit_price"), meta)
                        if plain.get("limit_price") and "–" not in str(plain.get("limit_price"))
                        else (plain.get("limit_price") or "—"),
                        "range": (
                            f"{format_price(plain.get('min_price'), meta)}–"
                            f"{format_price(plain.get('max_price'), meta)}"
                            if plain.get("min_price") and plain.get("max_price")
                            else format_price(plain.get("min_price") or plain.get("max_price"), meta)
                        ),
                    },
                }
            )

        payload: Dict[str, Any] = {
            "success": bool(resp.success),
            "exchange": exchange,
            "account": account,
            "positions": positions,
            "order_groups": groups,
            "open_order_count": int(resp.open_order_count or sum(g["count"] for g in groups)),
            "cache_hit": False,
            "timing_ms": {"tradedesk_ms": round(desk_ms, 1), "cache_ttl_s": self.cache_ttl},
            "operation_used": op,
        }
        if resp.error is not None:
            payload["error"] = _to_plain(resp.error)

        if resp.success:
            with self._lock:
                self._po_cache[key] = (now + self.cache_ttl, round(desk_ms, 1), dict(payload))
        return payload

    def positions(self, exchange: str, account: str) -> Dict[str, Any]:
        err = self.validate_exchange_account(exchange, account)
        if err:
            return {
                "success": False,
                "error": {"code": err, "message": err.replace("_", " ").title()},
                "positions": [],
                "timing_ms": {"tradedesk_ms": 0.0},
            }
        payload = self._fetch_positions_orders_payload(exchange, account)
        return {
            "success": payload.get("success"),
            "exchange": exchange,
            "account": account,
            "positions": payload.get("positions") or [],
            "error": payload.get("error"),
            "timing_ms": payload.get("timing_ms"),
            "cache_hit": payload.get("cache_hit"),
            "operation_used": payload.get("operation_used"),
        }

    def orders(self, exchange: str, account: str) -> Dict[str, Any]:
        err = self.validate_exchange_account(exchange, account)
        if err:
            return {
                "success": False,
                "error": {"code": err, "message": err.replace("_", " ").title()},
                "groups": [],
                "open_order_count": 0,
                "timing_ms": {"tradedesk_ms": 0.0},
            }
        payload = self._fetch_positions_orders_payload(exchange, account)
        return {
            "success": payload.get("success"),
            "exchange": exchange,
            "account": account,
            "groups": payload.get("order_groups") or [],
            "open_order_count": payload.get("open_order_count") or 0,
            "error": payload.get("error"),
            "timing_ms": payload.get("timing_ms"),
            "cache_hit": payload.get("cache_hit"),
            "operation_used": payload.get("operation_used"),
        }

    def _safe_error(self, resp: Any) -> Dict[str, Any]:
        err = _to_plain(getattr(resp, "error", None)) or {}
        if not isinstance(err, dict):
            return {"code": "ERROR", "message": str(err)}
        msg = str(err.get("message") or err.get("code") or "Operation failed.")
        low = msg.lower()
        for bad in ("api key", "private key", "secret", "password", "signature", "authorization"):
            if bad in low:
                msg = str(err.get("code") or "OPERATION_FAILED")
                break
        return {"code": str(err.get("code") or "ERROR"), "message": msg}

    def _execute_write(
        self,
        operation: str,
        exchange: str,
        account: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        err = self.validate_exchange_account(exchange, account)
        if err:
            return {
                "success": False,
                "error": {"code": err, "message": err.replace("_", " ").title()},
                "operation": operation,
                "exchange": exchange,
                "account": account,
            }
        req: Dict[str, Any] = {
            "operation": operation,
            "exchange": exchange,
            "account": account,
        }
        if extra:
            req.update(extra)
        t0 = time.perf_counter()
        resp = self.desk.execute(req)
        desk_ms = (time.perf_counter() - t0) * 1000.0
        self.invalidate_positions_cache(exchange, account)
        out: Dict[str, Any] = {
            "success": bool(resp.success),
            "operation": operation,
            "exchange": exchange,
            "account": account,
            "timing_ms": {"tradedesk_ms": round(desk_ms, 1)},
        }
        if extra:
            for k in ("symbol", "side", "price", "order_type"):
                if k in extra:
                    out[k] = extra[k]
        if resp.success:
            if getattr(resp, "cancel_group", None) is not None:
                cg = _to_plain(resp.cancel_group)
                out["cancel_group"] = cg
                cancelled = int(cg.get("cancelled_order_count") or cg.get("cancelled") or 0)
                requested = int(
                    cg.get("targeted_order_count")
                    or cg.get("requested_order_count")
                    or cg.get("requested_cancel_count")
                    or cg.get("order_count")
                    or cancelled
                )
                confirmed_absent = int(cg.get("confirmed_absent_count") or cg.get("verified_cancel_count") or 0)
                remaining = int(cg.get("remaining_target_count") or 0)
                out["cancelled"] = cancelled
                out["requested"] = requested
                out["confirmed_absent"] = confirmed_absent
                out["remaining"] = remaining
                out["verified"] = bool(cg.get("verified"))
                if out["verified"] or remaining == 0:
                    out["partial"] = False
                    if confirmed_absent and confirmed_absent >= requested:
                        out["message"] = (
                            f"All {requested} targeted orders are no longer open"
                            + (f" ({cancelled} cancel acks)." if cancelled else ".")
                        )
                    else:
                        out["message"] = f"Cancelled {cancelled} orders; none of the targets remain open."
                elif requested and cancelled < requested:
                    out["partial"] = True
                    out["message"] = (
                        f"Cancelled {cancelled}/{requested} orders; "
                        f"{remaining} still open."
                    )
                else:
                    out["message"] = f"Cancelled {cancelled} orders."
            else:
                out["message"] = f"{operation} succeeded."
            logger.info(
                "TradeMenu write ok op=%s exchange=%s account=%s symbol=%s ms=%.1f",
                operation,
                exchange,
                account,
                (extra or {}).get("symbol") or "",
                desk_ms,
            )
        else:
            out["error"] = self._safe_error(resp)
            logger.info(
                "TradeMenu write fail op=%s exchange=%s account=%s symbol=%s code=%s",
                operation,
                exchange,
                account,
                (extra or {}).get("symbol") or "",
                out["error"].get("code"),
            )
        return out

    def set_tp(self, exchange: str, account: str, symbol: str, price: str) -> Dict[str, Any]:
        return self._execute_write(
            "set_tp",
            exchange,
            account,
            {"symbol": str(symbol or "").strip(), "price": str(price or "").strip()},
        )

    def set_sl(self, exchange: str, account: str, symbol: str, price: str) -> Dict[str, Any]:
        return self._execute_write(
            "set_sl",
            exchange,
            account,
            {"symbol": str(symbol or "").strip(), "price": str(price or "").strip()},
        )

    def close_position(self, exchange: str, account: str, symbol: str) -> Dict[str, Any]:
        return self._execute_write(
            "close_position",
            exchange,
            account,
            {"symbol": str(symbol or "").strip()},
        )

    def cancel_order_group(
        self,
        exchange: str,
        account: str,
        symbol: str,
        side: str,
        order_type: str = "limit",
        classification: str = "",
        order_ids: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        side_n = str(side or "").strip().lower()
        if side_n not in {"buy", "sell"}:
            return {
                "success": False,
                "error": {"code": "INVALID_SIDE", "message": "side must be buy or sell"},
                "operation": "cancel_order_group",
                "exchange": exchange,
                "account": account,
            }
        extra: Dict[str, Any] = {
            "symbol": str(symbol or "").strip(),
            "side": side_n,
        }
        cls = str(classification or order_type or "").strip()
        if cls:
            # Map UI type labels to canonical classification.
            low = cls.lower().replace(" ", "_")
            aliases = {
                "limit": "entry_limit",
                "buy_limit": "entry_limit",
                "sell_limit": "entry_limit",
                "entry_limit": "entry_limit",
                "take_profit": "take_profit",
                "tp": "take_profit",
                "stop_loss": "stop_loss",
                "sl": "stop_loss",
                "trigger": "trigger",
                "other": "other",
            }
            extra["classification"] = aliases.get(low, low)
        if order_ids:
            extra["order_ids"] = list(order_ids)
        return self._execute_write(
            "cancel_order_group",
            exchange,
            account,
            extra,
        )
