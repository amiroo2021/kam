"""Read-only WebTrade2 service layer backed by TradeDesk.

This module deliberately routes only read operations through TradeDesk. Phase 1
has no execution/cancel/position-modification methods.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional

from ..ladder_math import build_ladder_children, ladder_vwap
from ..tradedesk import TradeDesk, get_tradedesk
from ..webtrade.preview_plans import PreviewPlanStore

log = logging.getLogger("webtrade2.service")

_WRITE_CAPS = {"new_order", "cancel_order_group", "set_tp", "set_sl", "close_position", "positions_management"}


def _plain(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return value


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        d = Decimal(str(value if value is not None and value != "" else default))
    except (InvalidOperation, ValueError):
        return Decimal(default)
    return d if d.is_finite() else Decimal(default)


def format_compact_volume(raw: Any) -> Optional[str]:
    """Render a 24h notional volume as ``$3.24B`` / ``$428.1M`` / ``$7.3M`` / ``$842K``.

    Returns ``None`` when the value is missing/empty — never substitutes a synthetic zero.
    """
    if raw is None or raw == "":
        return None
    try:
        n = float(str(raw))
    except (TypeError, ValueError):
        return None
    if not n or not (n == n):  # NaN guard
        return None
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1e9:
        text = f"{n / 1e9:.2f}".rstrip("0").rstrip(".")
        return f"{sign}${text}B"
    if n >= 1e6:
        text = f"{n / 1e6:.1f}".rstrip("0").rstrip(".")
        return f"{sign}${text}M"
    if n >= 1e3:
        text = f"{n / 1e3:.1f}".rstrip("0").rstrip(".")
        return f"{sign}${text}K"
    text = f"{n:.2f}".rstrip("0").rstrip(".")
    return f"{sign}${text or '0'}"


def format_dynamic_price(raw: Any) -> str:
    """Dynamic price precision so BTC ~ 79,425.23, ETH ~ 2,485.31, small caps keep extra digits."""
    if raw is None or raw == "":
        return "—"
    try:
        n = float(str(raw))
    except (TypeError, ValueError):
        return "—"
    if not (n == n):
        return "—"
    abs_n = abs(n)
    if abs_n >= 1000:
        digits = 2
    elif abs_n >= 1:
        digits = 2
    else:
        # Sub-unit prices need enough precision to remain meaningful
        # (e.g. 0.25169, 0.00012). 5 fractional digits covers all of these.
        digits = 5
    formatted = f"{n:.{digits}f}"
    # Comma-separate the integer part without altering the fractional digits
    if "." in formatted:
        head, _, tail = formatted.partition(".")
        head = f"{int(head):,}"
        formatted = f"{head}.{tail}"
    # Only strip trailing zeros from the fractional part for ≥1 magnitudes;
    # keep small magnitudes verbatim to preserve meaningful precision.
    if abs_n >= 1 and "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
        if not formatted:
            formatted = "0"
    return formatted


def format_pct_change(raw: Any) -> str:
    if raw is None or raw == "":
        return "—"
    try:
        n = float(str(raw).rstrip("%"))
    except (TypeError, ValueError):
        return "—"
    if not (n == n):
        return "—"
    sign = "+" if n > 0 else ""
    return f"{sign}{n:.2f}%"


def _volume_key(row: Mapping[str, Any]) -> tuple[int, Decimal, str]:
    # Ranking uses comparable quote/notional volume only:
    #   1. turnover_24h
    #   2. volume_24h_quote
    #   3. legacy volume_24h / quote-volume aliases
    # Never use volume_24h_base as ranking volume.
    raw = (
        row.get("turnover_24h")
        or row.get("turnover24h")
        or row.get("volume_24h_quote")
        or row.get("volume_24h")
        or row.get("quote_volume")
        or row.get("trading_volume_24h")
    )
    symbol = str(row.get("symbol") or row.get("instrument") or "")
    if raw is None or raw == "":
        return (1, Decimal("0"), symbol)
    vol = _decimal(raw)
    return (0, -vol, symbol)


def _bool_feature(caps: set[str], name: str) -> bool:
    return name in caps


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        if key in mapping and mapping.get(key) is not None and mapping.get(key) != "":
            return str(mapping.get(key))
    return None


def _sum_position_pnl(positions: Iterable[Any]) -> Optional[str]:
    seen = False
    total = Decimal("0")
    for pos in positions or []:
        if not isinstance(pos, Mapping):
            continue
        raw = pos.get("pnl") if pos.get("pnl") is not None else pos.get("unrealized_pnl")
        if raw is None or raw == "":
            continue
        seen = True
        total += _decimal(raw)
    if not seen:
        return None
    return format(total.normalize(), "f")


class WebTrade2Service:
    def __init__(self, desk: Optional[TradeDesk] = None, *, session_secret: str = "webtrade2-preview") -> None:
        self.desk = desk or get_tradedesk()
        self.preview_store = PreviewPlanStore(session_secret or "webtrade2-preview", ttl_seconds=300)

    def exchanges(self) -> Dict[str, Any]:
        items = []
        for exchange in self.desk.list_exchanges():
            accounts = [_plain(a) for a in self.desk.list_accounts(exchange)]
            items.append({"exchange": exchange, "accounts": accounts, "capabilities": self.capability_description(exchange)})
        return {"success": True, "exchanges": items}

    def accounts(self, exchange: str) -> Dict[str, Any]:
        return {"success": True, "exchange": exchange, "accounts": [_plain(a) for a in self.desk.list_accounts(exchange)]}

    def capability_description(self, exchange: str) -> Dict[str, Any]:
        caps = {str(c).strip() for c in (self.desk.capabilities(exchange) or []) if str(c).strip()}
        account_markets = set()
        for a in (self.desk.list_accounts(exchange) or []):
            if isinstance(a, Mapping):
                account_markets.add(str(a.get("account") or "").strip().lower())
                account_markets.add(str(a.get("label") or "").strip().lower())
                account_markets.add(str(a.get("market_type") or "").strip().lower())
            else:
                account_markets.add(str(a or "").strip().lower())
        account_markets.discard("")
        market_types: List[str] = []
        if account_markets:
            if "futures" in account_markets or "future" in account_markets:
                market_types.append("futures")
            if "spot" in account_markets or "spot" in caps:
                market_types.append("spot")
        else:
            if "spot" in caps:
                market_types.append("spot")
            if caps - {"spot"}:
                market_types.insert(0, "futures")
        if not market_types:
            market_types = ["futures"]
        market_types = list(dict.fromkeys(market_types))
        features = {
            "market_price": _bool_feature(caps, "market_price"),
            "candles": _bool_feature(caps, "candles"),
            "volume_24h": _bool_feature(caps, "list_instruments"),
            "limit_orders": _bool_feature(caps, "new_order"),
            "market_orders": False,
            "ladder": _bool_feature(caps, "ladder"),
            "uniform_ladder": _bool_feature(caps, "ladder"),
            "half_gaussian_ladder": _bool_feature(caps, "ladder"),
            "reduce_only": "reduce_only" in caps,
            "tp_sl": "set_tp" in caps and "set_sl" in caps,
            "close_position": _bool_feature(caps, "close_position"),
            "leverage": _bool_feature(caps, "leverage"),
            "fills": _bool_feature(caps, "fills"),
            "balance": _bool_feature(caps, "balance"),
            "positions": _bool_feature(caps, "positions_orders"),
            "open_orders": _bool_feature(caps, "positions_orders"),
        }
        return {"exchange": exchange, "market_types": market_types, "features": features, "raw_capabilities": sorted(caps - _WRITE_CAPS)}

    def _execute_read(self, request: Dict[str, Any]) -> Any:
        op = str(request.get("operation") or "")
        if op in _WRITE_CAPS:
            raise ValueError("WRITE_OPERATION_DISABLED")
        return self.desk.execute(request)

    def markets(self, exchange: str, account: str, market_type: str = "futures", search: str = "") -> Dict[str, Any]:
        caps = set(self.desk.capabilities(exchange) or [])
        rows: List[Dict[str, Any]] = []

        if "get_tickers" in caps:
            resp = self._execute_read({
                "operation": "get_tickers",
                "exchange": exchange,
                "account": account,
                "market_type": market_type,
            })
            data = resp.to_dict() if hasattr(resp, "to_dict") else _plain(resp)
            rows = self._rows_from_tickers_batch(data, market_type=market_type)
        else:
            if "list_instruments" not in caps:
                return {"success": False, "error": {"code": "UNSUPPORTED", "message": "Exchange does not expose instrument lists."}}
            resp = self._execute_read({"operation": "list_instruments", "exchange": exchange, "account": account, "market_type": market_type})
            data = resp.to_dict() if hasattr(resp, "to_dict") else _plain(resp)
            rows = self._rows_from_instrument_list(data, market_type=market_type)

        ranked = self._rank_markets(rows, search=search)
        return {"success": True, "exchange": exchange, "account": account, "market_type": market_type, "markets": ranked}

    def _rows_from_instrument_list(self, data: Mapping[str, Any], *, market_type: str) -> List[Dict[str, Any]]:
        """Temporary generic fallback for agents without get_tickers.

        This is exchange-agnostic and preserves the pre-Phase-D behavior
        for agents that have not advertised the canonical ticker contract yet.
        """
        rows: List[Dict[str, Any]] = []
        source = data.get("data", {}) if isinstance(data, Mapping) else {}
        for raw in source.get("instruments") or source.get("markets") or []:
            if not isinstance(raw, Mapping):
                continue
            symbol = str(raw.get("symbol") or raw.get("instrument") or raw.get("market") or "").strip()
            if not symbol:
                continue
            turnover = raw.get("turnover_24h") or raw.get("turnover24h")
            quote_volume = raw.get("volume_24h_quote") or raw.get("volume_24h") or raw.get("volume") or raw.get("quote_volume") or raw.get("trading_volume_24h")
            ranking_volume = turnover or quote_volume
            funding = None if str(market_type).lower() == "spot" else (
                raw.get("funding") or raw.get("funding_rate") or raw.get("fundingRate")
            )
            rows.append({
                "symbol": symbol,
                "display_name": raw.get("display_name") or raw.get("display_symbol") or raw.get("name") or symbol,
                "native_symbol": raw.get("native_symbol") or symbol,
                "base": raw.get("base"),
                "quote": raw.get("quote"),
                "market_type": raw.get("market_type") or market_type,
                "price": raw.get("price") or raw.get("mark_price") or raw.get("last_price"),
                "mark_price": raw.get("mark_price"),
                "price_increment": raw.get("price_increment"),
                "size_increment": raw.get("size_increment"),
                "minimum_size": raw.get("minimum_size"),
                "minimum_notional": raw.get("minimum_notional"),
                "change_24h": raw.get("change_24h") or raw.get("price_change_24h") or raw.get("price24hPcnt"),
                "turnover_24h": turnover,
                "volume_24h_quote": quote_volume,
                "volume_24h_base": raw.get("volume_24h_base"),
                # Keep legacy key so existing frontend/tests can keep using it;
                # its value is quote/notional-ranking volume, never base volume.
                "volume_24h": ranking_volume,
                "funding": funding,
                "funding_rate": raw.get("funding_rate") or raw.get("fundingRate") or raw.get("funding"),
                "volume_unit": raw.get("volume_unit") or raw.get("quote") or "quote",
                "ticker_status": "fallback_list_instruments",
            })
        return rows

    def _rows_from_tickers_batch(self, data: Mapping[str, Any], *, market_type: str) -> List[Dict[str, Any]]:
        """Map CanonicalTickersBatch into the stable WebTrade2 market model."""
        batch = data.get("tickers_batch", {}) if isinstance(data, Mapping) else {}
        if not isinstance(batch, Mapping):
            batch = {}
        stale = {str(s) for s in (batch.get("stale_symbols") or [])}
        failed = {str(s) for s in (batch.get("failed_symbols") or [])}
        status = str(batch.get("refresh_status") or "ok")
        source = batch.get("source")
        tickers = batch.get("tickers") or {}
        rows: List[Dict[str, Any]] = []
        if not isinstance(tickers, Mapping):
            return rows
        for symbol_key, raw in tickers.items():
            if not isinstance(raw, Mapping):
                continue
            symbol = str(raw.get("symbol") or raw.get("market") or symbol_key or "").strip()
            if not symbol:
                continue
            turnover = raw.get("turnover_24h")
            quote_volume = raw.get("volume_24h_quote")
            ranking_volume = turnover or quote_volume
            funding = None if str(market_type).lower() == "spot" else raw.get("funding_rate")
            row_status = "stale" if symbol in stale or str(symbol_key) in stale else ("unavailable" if symbol in failed or str(symbol_key) in failed else status)
            rows.append({
                "symbol": symbol,
                "display_name": raw.get("display_symbol") or raw.get("display_name") or symbol,
                "native_symbol": raw.get("native_symbol") or raw.get("market") or symbol,
                "base": raw.get("base"),
                "quote": raw.get("quote"),
                "market_type": raw.get("market_type") or market_type,
                "price": raw.get("price") or raw.get("mark_price"),
                "mark_price": raw.get("mark_price"),
                "price_increment": raw.get("price_increment"),
                "size_increment": raw.get("size_increment"),
                "minimum_size": raw.get("minimum_size"),
                "minimum_notional": raw.get("minimum_notional"),
                "turnover_24h": turnover,
                "volume_24h_quote": quote_volume,
                "volume_24h_base": raw.get("volume_24h_base"),
                # Legacy/ranking key: quote/notional only. Never base volume.
                "volume_24h": ranking_volume,
                "funding": funding,
                "funding_rate": raw.get("funding_rate"),
                "volume_unit": raw.get("quote") or "quote",
                "ticker_status": row_status,
                "ticker_source": source,
                "ticker_refresh_status": status,
            })
        return rows

    def _rank_markets(self, rows: List[Dict[str, Any]], search: str = "") -> List[Dict[str, Any]]:
        """Sort the market universe by 24h notional volume descending with unknown-volume alphabetical tail.

        Filtering preserves rank order; known-volume rows are emitted before unknown-volume ones.
        """
        filtered = list(rows or [])
        q = (search or "").strip().lower()
        if q:
            filtered = [r for r in filtered if q in str(r.get("symbol") or "").lower() or q in str(r.get("display_name") or "").lower()]
        filtered.sort(key=_volume_key)
        return filtered

    def resolve_instrument(self, exchange: str, account: str, symbol: str, market_type: str = "futures") -> Dict[str, Any]:
        resp = self._execute_read({"operation": "resolve_instrument", "exchange": exchange, "account": account, "symbol": symbol, "market_type": market_type})
        data = resp.to_dict() if hasattr(resp, "to_dict") else _plain(resp)
        return data if isinstance(data, dict) else {"success": False, "error": {"code": "BAD_RESPONSE", "message": "Bad response"}}

    def market_price(self, exchange: str, account: str, symbol: str, market_type: str = "futures") -> Dict[str, Any]:
        resp = self._execute_read({"operation": "market_price", "exchange": exchange, "account": account, "symbol": symbol, "market_type": market_type})
        data = resp.to_dict() if hasattr(resp, "to_dict") else _plain(resp)
        return data if isinstance(data, dict) else {"success": False, "error": {"code": "BAD_RESPONSE", "message": "Bad response"}}

    def account_state(self, exchange: str, account: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "success": True,
            "exchange": exchange,
            "account": account,
            "balance": None,
            "account_summary": {"equity": None, "available": None, "unrealized_pnl": None, "unit": None},
            "positions": [],
            "order_groups": [],
            "fills": [],
        }
        caps = set(self.desk.capabilities(exchange) or [])
        if "balance" in caps:
            balance_data = _plain(self._execute_read({"operation": "balance", "exchange": exchange, "account": account}))
            out["balance"] = balance_data
            if isinstance(balance_data, Mapping):
                summary = balance_data.get("portfolio_summary") if isinstance(balance_data.get("portfolio_summary"), Mapping) else {}
                bal = balance_data.get("balance") if isinstance(balance_data.get("balance"), Mapping) else {}
                positions_for_pnl = balance_data.get("positions") if isinstance(balance_data.get("positions"), list) else []
                equity = _first_present(summary, "account_value", "equity", "total_equity")
                if equity is None:
                    equity = _first_present(bal, "value", "account_value", "equity")
                available = _first_present(summary, "withdrawable", "available", "available_balance", "free")
                out["account_summary"] = {
                    "equity": equity,
                    "available": available,
                    "unrealized_pnl": _sum_position_pnl(positions_for_pnl),
                    "unit": _first_present(summary, "unit") or _first_present(bal, "unit"),
                }
        if "positions_orders" in caps:
            resp = self._execute_read({"operation": "positions_orders", "exchange": exchange, "account": account})
            data = resp.to_dict() if hasattr(resp, "to_dict") else _plain(resp)
            if isinstance(data, dict):
                out["positions"] = data.get("positions") or []
                out["order_groups"] = data.get("order_groups") or []
                out["open_order_count"] = data.get("open_order_count") or 0
                if out["account_summary"].get("unrealized_pnl") is None:
                    out["account_summary"]["unrealized_pnl"] = _sum_position_pnl(out["positions"])
        return out

    def candles(self, exchange: str, account: str, symbol: str, interval: str = "1h", limit: int = 120, market_type: str = "futures") -> Dict[str, Any]:
        resp = self._execute_read({"operation": "candles", "exchange": exchange, "account": account, "symbol": symbol, "interval": interval, "limit": limit, "market_type": market_type})
        data = resp.to_dict() if hasattr(resp, "to_dict") else _plain(resp)
        return data if isinstance(data, dict) else {"success": False, "error": {"code": "BAD_RESPONSE", "message": "Bad response"}}

    def preview_ladder(
        self,
        *,
        exchange: str,
        account: str,
        market_type: str,
        symbol: str,
        side: str,
        distribution: str,
        order_count: int,
        total_size: str,
        start_price: str,
        end_price: str,
    ) -> Dict[str, Any]:
        caps = set(self.desk.capabilities(exchange) or [])
        if "ladder" not in caps:
            return {"success": False, "error": {"code": "UNSUPPORTED", "message": "Exchange does not support ladder previews."}}
        resolved = self.resolve_instrument(exchange, account, symbol, market_type)
        inst = resolved.get("instrument") if isinstance(resolved, dict) else None
        if not isinstance(inst, dict):
            inst = {}
        price_increment = _decimal(inst.get("price_increment") or inst.get("tick_size") or "0.01", "0.01")
        size_increment = _decimal(inst.get("size_increment") or inst.get("lot_size") or "0.000001", "0.000001")
        children, submitted, vwap = build_ladder_children(
            side=side,
            distribution=distribution,
            order_count=int(order_count),
            total_volume=_decimal(total_size),
            start_price=_decimal(start_price),
            end_price=_decimal(end_price),
            size_increment=size_increment,
            price_increment=price_increment,
        )
        display_children = children if len(children) <= 10 else children[:5] + [{"ellipsis": True}] + children[-5:]
        plan = {
            "phase": 1,
            "read_only": True,
            "operation": "ladder_preview_only",
            "exchange": exchange,
            "account": account,
            "market_type": market_type,
            "symbol": str(inst.get("symbol") or symbol).upper(),
            "side": side,
            "distribution": distribution,
            "order_count": len(children),
            "total_size": format(submitted.normalize(), "f"),
            "start_price": str(start_price),
            "end_price": str(end_price),
            "children": children,
        }
        token = self.preview_store.issue(plan)
        return {
            "success": True,
            "read_only": True,
            "exchange": exchange,
            "account": account,
            "market_type": market_type,
            "symbol": plan["symbol"],
            "side": side,
            "start_price": str(start_price),
            "end_price": str(end_price),
            "order_count": len(children),
            "total_size": plan["total_size"],
            "distribution": distribution,
            "vwap": format(vwap.normalize(), "f"),
            "children": children,
            "display_children": display_children,
            "preview_id": token,
        }
