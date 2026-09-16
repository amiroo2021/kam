"""TradeDesk-backed service layer for TradeMenu (read + write via TradeDesk)."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, is_dataclass
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from ..tradedesk import TradeDesk, get_tradedesk
from ..ladder_math import build_ladder_children, ladder_vwap, quantize_to_increment
from .formatting import format_money, format_pnl, format_price, format_size
from .preview_plans import PreviewPlanStore

logger = logging.getLogger("trademenu")

# Short in-process TTL so Positions + Orders share one agent fetch and
# rapid UI polls do not re-hit multi-dex Hyperliquid open-order fanout.
# Must exceed cold HL multi-dex fanout (~8–15s) or the entry expires before reuse.
# Keep short enough that mark/PnL do not look frozen after a few reloads.
_POSITIONS_CACHE_TTL_SECONDS = 20.0
_RESOLVE_CACHE_TTL_SECONDS = 60.0
# Balance / portfolio is cheaper than multi-dex positions; keep a short TTL so
# exchange/account switches and Reload all stay snappy without thrashing.
_BALANCE_CACHE_TTL_SECONDS = 20.0


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

    Only used when PnL is present and authoritative. A missing/blank PnL
    must NOT yield mark=entry (that looked like a frozen market).
    """
    e = _dec(entry)
    s = _dec(size)
    p = _dec(pnl)
    if e is None or s is None or p is None or s == 0:
        return None
    # Explicit blank/"—" pnl means unavailable — do not invent mark.
    if pnl in ("", "—", None):
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

    def __init__(
        self,
        desk: Optional[TradeDesk] = None,
        cache_ttl: float = _POSITIONS_CACHE_TTL_SECONDS,
        preview_store: Optional[PreviewPlanStore] = None,
        session_secret: str = "trademenu-dev-secret",
    ) -> None:
        self.desk = desk or get_tradedesk()
        self.cache_ttl = float(cache_ttl)
        self.resolve_cache_ttl = _RESOLVE_CACHE_TTL_SECONDS
        self.balance_cache_ttl = _BALANCE_CACHE_TTL_SECONDS
        self._lock = Lock()
        # key -> (expires_at, timing_ms, CanonicalResponse-like dict payload)
        self._po_cache: Dict[Tuple[str, str], Tuple[float, float, Dict[str, Any]]] = {}
        # (exchange, account) -> (expires_at, timing_ms, normalized financials)
        self._bal_cache: Dict[Tuple[str, str], Tuple[float, float, Dict[str, Any]]] = {}
        # (exchange, account, symbol_key) -> (expires_at, payload)
        self._resolve_cache: Dict[Tuple[str, str, str], Tuple[float, Dict[str, Any]]] = {}
        self.previews = preview_store or PreviewPlanStore(session_secret, ttl_seconds=300)

    def invalidate_positions_cache(self, exchange: str = "", account: str = "") -> None:
        with self._lock:
            if exchange and account:
                self._po_cache.pop((str(exchange), str(account)), None)
            else:
                self._po_cache.clear()

    def invalidate_balance_cache(self, exchange: str = "", account: str = "") -> None:
        with self._lock:
            if exchange and account:
                self._bal_cache.pop((str(exchange), str(account)), None)
            else:
                self._bal_cache.clear()

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
        # Shared picker path with Telegram /trade (same TradeDesk resolve +
        # list_instruments rank + market_price enrichment).
        from plugins.trade.instrument_picker import (
            INSTRUMENT_PICK_MAX_TRADEMENU,
            resolve_with_candidates,
        )

        picked = resolve_with_candidates(
            self.desk,
            exchange,
            account,
            requested,
            limit=INSTRUMENT_PICK_MAX_TRADEMENU,
        )
        desk_ms = (time.perf_counter() - t0) * 1000.0
        data: Dict[str, Any] = dict(picked)
        data["exchange"] = exchange
        data["account"] = account
        data["cache_hit"] = False
        data["timing_ms"] = {"tradedesk_ms": round(desk_ms, 1)}

        # Normalize unique success instrument + format_meta (legacy consumers).
        if data.get("success") and data.get("native_symbol"):
            inst = data.get("instrument") if isinstance(data.get("instrument"), dict) else {}
            native = str(data.get("native_symbol") or inst.get("symbol") or "").strip()
            if native:
                inst = dict(inst or {})
                inst["symbol"] = native
                data["instrument"] = inst
                data["display"] = data.get("display") or f"{requested} → {native}"
                meta = data.get("format_meta") if isinstance(data.get("format_meta"), dict) else {}
                if not meta:
                    meta = {
                        "price_increment": inst.get("price_increment") or inst.get("tick_size"),
                        "size_increment": inst.get("size_increment") or inst.get("lot_size"),
                        "price_decimals": inst.get("price_decimals"),
                        "size_decimals": inst.get("size_decimals") or inst.get("sz_decimals"),
                    }
                    data["format_meta"] = {k: v for k, v in meta.items() if v is not None}
            with self._lock:
                # Only cache unique resolved natives — never cache ambiguity.
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

    def account_financials(
        self,
        exchange: str,
        account: str,
        *,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Normalized account money for the toolbar (TradeDesk ``balance`` only).

        Fields are omitted when the agent does not supply them — never fabricate
        Available / Equity from position notionals. ``available`` maps to
        canonical ``portfolio_summary.withdrawable`` (same field Telegram /trade
        labels Withdrawable).
        """
        err = self.validate_exchange_account(exchange, account)
        if err:
            return {
                "success": False,
                "error": {"code": err, "message": err.replace("_", " ").title()},
                "exchange": exchange,
                "account": account,
            }
        key = (str(exchange), str(account))
        now = time.time()
        if not force:
            with self._lock:
                hit = self._bal_cache.get(key)
                if hit and hit[0] > now:
                    payload = dict(hit[2])
                    payload["cache_hit"] = True
                    payload["timing_ms"] = {
                        "tradedesk_ms": 0.0,
                        "cached_tradedesk_ms": hit[1],
                        "cache_ttl_s": self.balance_cache_ttl,
                    }
                    return payload

        caps = set(self.desk.capabilities(exchange))
        if "balance" not in caps:
            return {
                "success": False,
                "error": {
                    "code": "UNSUPPORTED",
                    "message": "Exchange does not expose balance.",
                },
                "exchange": exchange,
                "account": account,
                "fields": [],
            }

        t0 = time.perf_counter()
        resp = self.desk.execute(
            {
                "operation": "balance",
                "exchange": exchange,
                "account": account,
            }
        )
        desk_ms = (time.perf_counter() - t0) * 1000.0
        data = self._normalize_account_financials(
            resp,
            exchange=exchange,
            account=account,
            desk_ms=desk_ms,
        )
        if data.get("success"):
            with self._lock:
                self._bal_cache[key] = (now + self.balance_cache_ttl, desk_ms, dict(data))
        return data

    def _normalize_account_financials(
        self,
        resp: Any,
        *,
        exchange: str,
        account: str,
        desk_ms: float,
    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "success": bool(getattr(resp, "success", False)),
            "exchange": exchange,
            "account": account,
            "cache_hit": False,
            "timing_ms": {"tradedesk_ms": round(desk_ms, 1)},
            "fields": [],
        }
        if not out["success"]:
            out["error"] = self._safe_error(resp)
            return out

        bal = getattr(resp, "balance", None)
        bal_plain = _to_plain(bal) if bal is not None else None
        currency = ""
        balance_raw: Optional[str] = None
        if isinstance(bal_plain, dict):
            balance_raw = bal_plain.get("value")
            currency = str(bal_plain.get("unit") or "").strip()
        elif bal_plain is not None:
            balance_raw = str(bal_plain)

        summary = getattr(resp, "portfolio_summary", None)
        summary_plain = _to_plain(summary) if summary is not None else None
        if isinstance(summary_plain, dict) and not currency:
            currency = str(summary_plain.get("unit") or "").strip()

        fields: List[Dict[str, Any]] = []
        balance_disp = format_money(balance_raw)
        if balance_disp is not None:
            out["balance"] = str(balance_raw)
            out["balance_display"] = balance_disp
            fields.append({"key": "balance", "label": "Balance", "value": str(balance_raw), "display": balance_disp})

        equity_raw = None
        if isinstance(summary_plain, dict):
            equity_raw = summary_plain.get("account_value")
        equity_disp = format_money(equity_raw)
        if equity_disp is not None:
            out["equity"] = str(equity_raw)
            out["equity_display"] = equity_disp
            fields.append({"key": "equity", "label": "Equity", "value": str(equity_raw), "display": equity_disp})

        # Canonical portfolio_summary.withdrawable — same source Telegram shows as Withdrawable.
        # Do not invent "Available Margin" from balance − positions.
        available_raw = None
        if isinstance(summary_plain, dict):
            available_raw = summary_plain.get("withdrawable")
        available_disp = format_money(available_raw)
        if available_disp is not None:
            available_label = "Withdrawable"
            out["available"] = str(available_raw)
            out["available_display"] = available_disp
            out["available_label"] = available_label
            fields.append(
                {
                    "key": "available",
                    "label": available_label,
                    "short_label": "Available",
                    "value": str(available_raw),
                    "display": available_disp,
                    "title": "Withdrawable (canonical portfolio_summary.withdrawable)",
                }
            )

        if currency:
            out["currency"] = currency
        if isinstance(summary_plain, dict):
            # Pass through for tooltips/debug; UI should prefer fields[].
            out["portfolio_summary"] = {
                k: summary_plain.get(k)
                for k in ("account_value", "withdrawable", "margin_used", "total_position_value", "unit")
                if summary_plain.get(k) is not None
            }
        out["fields"] = fields
        if not fields:
            out["success"] = False
            out["error"] = {
                "code": "BALANCE_UNAVAILABLE",
                "message": "Balance unavailable.",
            }
        return out

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
                # Derive only from real PnL — never substitute entry for mark.
                mark = _derive_mark(row.get("side"), row.get("entry_price"), row.get("size"), row.get("pnl"))
            pnl_raw = row.get("pnl")
            # Blank pnl from agents means "unavailable" (not zero).
            if pnl_raw in ("", "—"):
                pnl_raw = None
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
                    "pnl": pnl_raw,
                    "sl": row.get("sl"),
                    "tp": row.get("tp"),
                    "exchange_instrument": row.get("exchange_instrument"),
                    "native_symbol": row.get("exchange_instrument") or row.get("native_symbol"),
                    "format_meta": {k: v for k, v in meta.items() if v is not None},
                    "display": {
                        "size": format_size(row.get("size"), meta),
                        "entry": format_price(row.get("entry_price"), meta),
                        "mark": format_price(mark, meta) if mark not in (None, "", "—") else "—",
                        "pnl": format_pnl(pnl_raw, meta) if pnl_raw is not None else "—",
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
            native = plain.get("exchange_instrument") or plain.get("native_symbol")
            groups.append(
                {
                    "symbol": plain.get("symbol"),
                    "native_symbol": native,
                    "exchange_instrument": native,
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
                # Expire from *now* (post-fetch). Using the pre-fetch timestamp
                # made a 10s HL fanout expire the entry immediately.
                self._po_cache[key] = (time.time() + self.cache_ttl, round(desk_ms, 1), dict(payload))
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
        code = str(err.get("code") or "ERROR")
        msg = str(err.get("message") or err.get("code") or "Operation failed.")
        low = msg.lower()
        for bad in ("api key", "private key", "secret", "password", "signature", "authorization"):
            if bad in low:
                msg = code
                break
        # Never leak raw HTTP client traces / URLs to the browser.
        if "http" in low and ("error" in low or "://" in low or "status" in low):
            reason = str(err.get("exchange_reason") or "").strip()
            if reason and "http" not in reason.lower() and "://" not in reason:
                msg = reason
            else:
                msg = "Order rejected by exchange." if "order" in low else "Exchange request failed."
        # Collapse urllib-style messages
        for needle in (" for url:", "Client Error:", "Server Error:", "HTTPSConnectionPool"):
            if needle.lower() in low:
                msg = "Order rejected by exchange." if "order" in low else "Exchange request failed."
                break
        return {"code": code, "message": msg}

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
        self.invalidate_balance_cache(exchange, account)
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

    # --- New order / ladder preview + execute ---------------------------------

    def _meta_increments(self, resolved: Dict[str, Any]) -> Tuple[Decimal, Decimal]:
        meta = resolved.get("format_meta") or {}
        inst = resolved.get("instrument") or {}
        px_inc = _dec(meta.get("price_increment") or inst.get("price_increment") or "0.1") or Decimal("0.1")
        sz_inc = _dec(meta.get("size_increment") or inst.get("size_increment") or "0.001") or Decimal("0.001")
        if px_inc <= 0:
            px_inc = Decimal("0.1")
        if sz_inc <= 0:
            sz_inc = Decimal("0.001")
        return px_inc, sz_inc

    def _round_order_price(self, exchange: str, price: Decimal, resolved: Dict[str, Any]) -> Decimal:
        px_inc, _ = self._meta_increments(resolved)
        if str(exchange).lower() == "hyperliquid":
            try:
                from ..agents.x_hyperliquid_agent import _normalize_hyperliquid_order_price

                # szDecimals is size precision; HL price normalizer still needs it.
                inst = resolved.get("instrument") or {}
                sz_decimals = inst.get("size_decimals") or inst.get("sz_decimals")
                return _normalize_hyperliquid_order_price(price, sz_decimals)
            except Exception:
                pass
        return quantize_to_increment(price, px_inc)

    def _round_order_size(self, size: Decimal, resolved: Dict[str, Any]) -> Decimal:
        _, sz_inc = self._meta_increments(resolved)
        return quantize_to_increment(size, sz_inc)

    def preview_order(
        self,
        exchange: str,
        account: str,
        symbol: str,
        side: str,
        order_type: str,
        size: str,
        price: str = "",
    ) -> Dict[str, Any]:
        err = self.validate_exchange_account(exchange, account)
        if err:
            return {"success": False, "error": {"code": err, "message": err.replace("_", " ").title()}}
        side_n = str(side or "").strip().lower()
        if side_n not in {"buy", "sell"}:
            return {"success": False, "error": {"code": "INVALID_SIDE", "message": "Side must be buy or sell."}}
        ot = str(order_type or "limit").strip().lower() or "limit"
        if ot != "limit":
            # Match Hyperliquid agent: only limit is safely supported via TradeDesk today.
            return {
                "success": False,
                "error": {"code": "INVALID_ORDER_TYPE", "message": "Only LIMIT orders are supported via TradeMenu."},
            }
        caps = set(self.desk.capabilities(exchange))
        if "new_order" not in caps:
            return {
                "success": False,
                "error": {"code": "UNSUPPORTED", "message": f"{exchange} does not expose new_order."},
            }
        resolved = self.resolve_instrument(exchange, account, symbol)
        if not resolved.get("success") or not resolved.get("native_symbol"):
            return {
                "success": False,
                "error": resolved.get("error") or {"code": "INSTRUMENT_NOT_FOUND", "message": "Unresolved symbol."},
                "display": resolved.get("display"),
            }
        native = str(resolved["native_symbol"])
        req_size = _dec(size)
        req_price = _dec(price)
        if req_size is None or req_size <= 0:
            return {"success": False, "error": {"code": "INVALID_SIZE", "message": "Size must be positive."}}
        if req_price is None or req_price <= 0:
            return {"success": False, "error": {"code": "INVALID_PRICE", "message": "Price must be positive."}}
        try:
            final_size = self._round_order_size(req_size, resolved)
            final_price = self._round_order_price(exchange, req_price, resolved)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": {"code": "INVALID_ORDER_PRECISION", "message": str(exc)}}
        if final_size <= 0 or final_price <= 0:
            return {"success": False, "error": {"code": "INVALID_ORDER_PRECISION", "message": "Rounded size/price invalid."}}
        notional = final_price * final_size
        meta = resolved.get("format_meta") or {}
        plan = {
            "kind": "order",
            "exchange": exchange,
            "account": account,
            "requested_symbol": str(symbol or "").strip(),
            "native_symbol": native,
            "side": side_n,
            "order_type": "limit",
            "requested_price": format(req_price.normalize(), "f"),
            "requested_size": format(req_size.normalize(), "f"),
            "final_price": format(final_price.normalize(), "f"),
            "final_size": format(final_size.normalize(), "f"),
            "notional": format(notional.normalize(), "f"),
        }
        preview_id = self.previews.issue(plan)
        return {
            "success": True,
            "preview_id": preview_id,
            "kind": "order",
            "exchange": exchange,
            "account": account,
            "requested_symbol": plan["requested_symbol"],
            "native_symbol": native,
            "display": resolved.get("display") or f"{symbol} → {native}",
            "side": side_n,
            "order_type": "limit",
            "requested_price": plan["requested_price"],
            "final_price": plan["final_price"],
            "requested_size": plan["requested_size"],
            "final_size": plan["final_size"],
            "notional": plan["notional"],
            "format_meta": meta,
            "expires_in_s": self.previews.ttl_seconds,
            "summary": f"{side_n.upper()} {native} LIMIT @ {plan['final_price']} × {plan['final_size']}",
        }

    def preview_ladder(
        self,
        exchange: str,
        account: str,
        symbol: str,
        side: str,
        distribution: str,
        order_count: Any,
        total_size: str,
        start_price: str,
        end_price: str,
    ) -> Dict[str, Any]:
        err = self.validate_exchange_account(exchange, account)
        if err:
            return {"success": False, "error": {"code": err, "message": err.replace("_", " ").title()}}
        side_n = str(side or "").strip().lower()
        if side_n not in {"buy", "sell"}:
            return {"success": False, "error": {"code": "INVALID_SIDE", "message": "Side must be buy or sell."}}
        dist = str(distribution or "").strip().lower()
        if dist not in {"uniform", "half_gaussian"}:
            return {
                "success": False,
                "error": {"code": "UNSUPPORTED_DISTRIBUTION", "message": "Distribution must be uniform or half_gaussian."},
            }
        caps = set(self.desk.capabilities(exchange))
        if "ladder" not in caps:
            return {"success": False, "error": {"code": "UNSUPPORTED", "message": f"{exchange} does not expose ladder."}}
        try:
            n = int(str(order_count).strip())
        except Exception:
            n = 0
        if n <= 0:
            return {"success": False, "error": {"code": "INVALID_ORDER_COUNT", "message": "Order count must be positive."}}
        if n > 200:
            return {"success": False, "error": {"code": "INVALID_ORDER_COUNT", "message": "Order count too large (max 200)."}}
        tv = _dec(total_size)
        sp = _dec(start_price)
        ep = _dec(end_price)
        if tv is None or tv <= 0:
            return {"success": False, "error": {"code": "INVALID_SIZE", "message": "Total size must be positive."}}
        if sp is None or ep is None or sp <= 0 or ep <= 0:
            return {"success": False, "error": {"code": "INVALID_PRICE", "message": "Start and end price must be positive."}}
        if side_n == "buy" and ep >= sp:
            return {
                "success": False,
                "error": {
                    "code": "INVALID_LADDER_DIRECTION",
                    "message": "BUY ladders require end price below start price.",
                },
            }
        if side_n == "sell" and ep <= sp:
            return {
                "success": False,
                "error": {
                    "code": "INVALID_LADDER_DIRECTION",
                    "message": "SELL ladders require end price above start price.",
                },
            }
        resolved = self.resolve_instrument(exchange, account, symbol)
        if not resolved.get("success") or not resolved.get("native_symbol"):
            return {
                "success": False,
                "error": resolved.get("error") or {"code": "INSTRUMENT_NOT_FOUND", "message": "Unresolved symbol."},
            }
        native = str(resolved["native_symbol"])
        px_inc, sz_inc = self._meta_increments(resolved)
        # Hyperliquid uses specialized price normalizer for each step.
        if str(exchange).lower() == "hyperliquid":
            try:
                from ..agents.x_hyperliquid_agent import (
                    _build_ladder_order_requests,
                    _candidate_sz_decimals,
                    _decimal_from_request,
                )

                # Prefer agent builder so children match Telegram exactly.
                inst = resolved.get("instrument") or {}
                # Build a candidate-like dict for sz decimals
                candidate = {
                    "public_symbol": native,
                    "size_increment": str(sz_inc),
                    "szDecimals": inst.get("sz_decimals") or inst.get("size_decimals"),
                }
                sz_decimals = None
                try:
                    from ..agents import x_hyperliquid_agent as hl

                    # Use public builder with HL price rules when possible
                    order_requests, submitted = _build_ladder_order_requests(
                        symbol=native,
                        side=side_n,
                        distribution=dist,
                        order_count=n,
                        total_volume=tv,
                        start_price=sp,
                        end_price=ep,
                        sz_decimals=candidate.get("szDecimals"),
                        size_increment=sz_inc,
                    )
                    children = [
                        {
                            "price": format((_decimal_from_request(r.get("limit_px")) or Decimal("0")).normalize(), "f"),
                            "size": format((_decimal_from_request(r.get("sz")) or Decimal("0")).normalize(), "f"),
                        }
                        for r in order_requests
                    ]
                    submitted_vol = submitted
                    vwap = ladder_vwap(children)
                except ValueError as exc:
                    code = str(exc) or "INVALID_LADDER_REQUEST"
                    return {
                        "success": False,
                        "error": {"code": code, "message": code.replace("_", " ").title()},
                    }
            except Exception:
                try:
                    children, submitted_vol, vwap = build_ladder_children(
                        side=side_n,
                        distribution=dist,
                        order_count=n,
                        total_volume=tv,
                        start_price=sp,
                        end_price=ep,
                        size_increment=sz_inc,
                        price_increment=px_inc,
                    )
                except ValueError as exc:
                    code = str(exc) or "INVALID_LADDER_REQUEST"
                    msg = {
                        "INVALID_LADDER_DIRECTION": (
                            "BUY ladders require end price below start price."
                            if side_n == "buy"
                            else "SELL ladders require end price above start price."
                        )
                    }.get(code, code.replace("_", " ").title())
                    return {"success": False, "error": {"code": code, "message": msg}}
        else:
            try:
                children, submitted_vol, vwap = build_ladder_children(
                    side=side_n,
                    distribution=dist,
                    order_count=n,
                    total_volume=tv,
                    start_price=sp,
                    end_price=ep,
                    size_increment=sz_inc,
                    price_increment=px_inc,
                )
            except ValueError as exc:
                code = str(exc) or "INVALID_LADDER_REQUEST"
                msg = {
                    "INVALID_LADDER_DIRECTION": (
                        "BUY ladders require end price below start price."
                        if side_n == "buy"
                        else "SELL ladders require end price above start price."
                    )
                }.get(code, code.replace("_", " ").title())
                return {"success": False, "error": {"code": code, "message": msg}}

        prices = [Decimal(c["price"]) for c in children]
        sizes = [Decimal(c["size"]) for c in children]
        plan = {
            "kind": "ladder",
            "exchange": exchange,
            "account": account,
            "requested_symbol": str(symbol or "").strip(),
            "native_symbol": native,
            "side": side_n,
            "distribution": dist,
            "order_count": n,
            "requested_total_size": format(tv.normalize(), "f"),
            "final_total_size": format(sum(sizes).normalize(), "f"),
            "start_price": format(sp.normalize(), "f"),
            "end_price": format(ep.normalize(), "f"),
            "min_price": format(min(prices).normalize(), "f"),
            "max_price": format(max(prices).normalize(), "f"),
            "vwap": format(vwap.normalize(), "f"),
            "children": children,
            # Execution uses TradeDesk ladder inputs (deterministic) + children for audit.
            "exec": {
                "symbol": native,
                "side": side_n,
                "distribution": dist,
                "order_count": n,
                "total_volume": format(tv.normalize(), "f"),
                "start_price": format(sp.normalize(), "f"),
                "end_price": format(ep.normalize(), "f"),
            },
        }
        preview_id = self.previews.issue(plan)
        meta = resolved.get("format_meta") or {}
        return {
            "success": True,
            "preview_id": preview_id,
            "kind": "ladder",
            "exchange": exchange,
            "account": account,
            "requested_symbol": plan["requested_symbol"],
            "native_symbol": native,
            "display": resolved.get("display") or f"{symbol} → {native}",
            "side": side_n,
            "distribution": dist,
            "order_count": len(children),
            "requested_order_count": n,
            "total_size": plan["final_total_size"],
            "min_price": plan["min_price"],
            "max_price": plan["max_price"],
            "vwap": plan["vwap"],
            "children": children,
            "format_meta": meta,
            "expires_in_s": self.previews.ttl_seconds,
            "summary": (
                f"{side_n.upper()} {native} LADDER · {dist} · {len(children)} orders · "
                f"VWAP {plan['vwap']}"
            ),
        }

    def execute_preview(self, preview_id: str) -> Dict[str, Any]:
        plan, err = self.previews.consume(str(preview_id or ""))
        if err or not plan:
            return {
                "success": False,
                "error": {
                    "code": err or "PREVIEW_INVALID",
                    "message": {
                        "PREVIEW_EXPIRED": "Preview expired. Generate a new preview.",
                        "PREVIEW_CONSUMED": "Preview already used. Generate a new preview.",
                        "PREVIEW_INVALID": "Invalid preview. Generate a new preview.",
                    }.get(err or "", "Invalid preview."),
                },
            }
        exchange = str(plan.get("exchange") or "")
        account = str(plan.get("account") or "")
        verr = self.validate_exchange_account(exchange, account)
        if verr:
            return {"success": False, "error": {"code": verr, "message": verr.replace("_", " ").title()}}
        kind = str(plan.get("kind") or "")
        if kind == "order":
            req = {
                "operation": "new_order",
                "exchange": exchange,
                "account": account,
                "symbol": plan.get("native_symbol") or plan.get("requested_symbol"),
                "side": plan.get("side"),
                "order_type": "limit",
                "volume": plan.get("final_size"),
                "price": plan.get("final_price"),
            }
            t0 = time.perf_counter()
            resp = self.desk.execute(req)
            desk_ms = (time.perf_counter() - t0) * 1000.0
            self.invalidate_positions_cache(exchange, account)
            self.invalidate_balance_cache(exchange, account)
            out: Dict[str, Any] = {
                "success": bool(resp.success),
                "kind": "order",
                "operation": "new_order",
                "exchange": exchange,
                "account": account,
                "symbol": plan.get("native_symbol"),
                "side": plan.get("side"),
                "final_price": plan.get("final_price"),
                "final_size": plan.get("final_size"),
                "timing_ms": {"tradedesk_ms": round(desk_ms, 1)},
            }
            if resp.success:
                out["message"] = (
                    f"{str(plan.get('side')).upper()} {plan.get('native_symbol')} LIMIT submitted."
                )
                logger.info(
                    "TradeMenu order ok exchange=%s account=%s symbol=%s side=%s",
                    exchange,
                    account,
                    plan.get("native_symbol"),
                    plan.get("side"),
                )
            else:
                out["error"] = self._safe_error(resp)
            if getattr(resp, "order", None) is not None:
                out["order"] = _to_plain(resp.order)
            return out

        if kind == "ladder":
            exec_body = plan.get("exec") or {}
            req = {
                "operation": "ladder",
                "exchange": exchange,
                "account": account,
                "symbol": exec_body.get("symbol") or plan.get("native_symbol"),
                "side": exec_body.get("side") or plan.get("side"),
                "distribution": exec_body.get("distribution") or plan.get("distribution"),
                "order_count": exec_body.get("order_count") or plan.get("order_count"),
                "total_volume": exec_body.get("total_volume") or plan.get("requested_total_size"),
                "start_price": exec_body.get("start_price") or plan.get("start_price"),
                "end_price": exec_body.get("end_price") or plan.get("end_price"),
                # Audit only — agents may ignore; execution still uses same inputs.
                "preview_children": plan.get("children"),
            }
            t0 = time.perf_counter()
            resp = self.desk.execute(req)
            desk_ms = (time.perf_counter() - t0) * 1000.0
            self.invalidate_positions_cache(exchange, account)
            self.invalidate_balance_cache(exchange, account)
            out = {
                "success": bool(resp.success),
                "kind": "ladder",
                "operation": "ladder",
                "exchange": exchange,
                "account": account,
                "symbol": plan.get("native_symbol"),
                "side": plan.get("side"),
                "distribution": plan.get("distribution"),
                "preview_order_count": len(plan.get("children") or []),
                "preview_vwap": plan.get("vwap"),
                "timing_ms": {"tradedesk_ms": round(desk_ms, 1)},
            }
            ladder = _to_plain(getattr(resp, "ladder", None)) if getattr(resp, "ladder", None) else None
            if ladder:
                out["ladder"] = ladder
                accepted = int(ladder.get("accepted_child_count") or ladder.get("submitted_order_count") or 0)
                requested = int(ladder.get("requested_order_count") or plan.get("order_count") or 0)
                out["accepted"] = accepted
                out["requested"] = requested
                if ladder.get("partial") or (requested and accepted < requested):
                    out["partial"] = True
                    out["message"] = f"{accepted} of {requested} orders accepted."
                else:
                    out["message"] = f"Ladder submitted ({accepted} orders)."
            if resp.success:
                logger.info(
                    "TradeMenu ladder ok exchange=%s account=%s symbol=%s children=%s",
                    exchange,
                    account,
                    plan.get("native_symbol"),
                    len(plan.get("children") or []),
                )
            else:
                out["error"] = self._safe_error(resp)
                if ladder and int(ladder.get("accepted_child_count") or 0) > 0:
                    accepted = int(ladder.get("accepted_child_count") or 0)
                    requested = int(ladder.get("requested_order_count") or plan.get("order_count") or 0)
                    out["partial"] = True
                    out["accepted"] = accepted
                    out["requested"] = requested
                    # Surface partial success to the UI; do not auto-retry.
                    out["success"] = True
                    out["message"] = (
                        f"Ladder partially placed: {accepted} / {requested} accepted. "
                        f"No automatic retry."
                    )
                    if out["error"].get("message"):
                        # Keep a short reason without raw HTTP URL noise.
                        reason = str(out["error"].get("message") or "")
                        if reason and "http" not in reason.lower() and "://" not in reason:
                            out["message"] = f"{out['message']} Last error: {reason}"
            return out

        return {"success": False, "error": {"code": "PREVIEW_INVALID", "message": "Unknown preview kind."}}
