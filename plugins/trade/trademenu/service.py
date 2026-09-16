"""TradeDesk-backed read-only service layer for TradeMenu."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Dict, List, Optional

from ..tradedesk import TradeDesk, get_tradedesk


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
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    return obj


class TradeMenuService:
    """Thin facade over TradeDesk — no exchange-native secrets escape."""

    def __init__(self, desk: Optional[TradeDesk] = None) -> None:
        self.desk = desk or get_tradedesk()

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
        resp = self.desk.execute(
            {
                "operation": "resolve_instrument",
                "exchange": exchange,
                "account": account,
                "symbol": symbol,
            }
        )
        data: Dict[str, Any] = {
            "success": bool(resp.success),
            "exchange": exchange,
            "account": account,
            "requested_symbol": symbol,
        }
        if resp.instrument is not None:
            data["instrument"] = _to_plain(resp.instrument)
        # Some agents return multiple candidates in data
        payload = _to_plain(resp.data) if resp.data else None
        if isinstance(payload, dict):
            candidates = payload.get("candidates") or payload.get("instruments")
            if candidates:
                data["candidates"] = candidates
            data["data"] = {k: v for k, v in payload.items() if k not in {"candidates", "instruments"}}
        if resp.error is not None:
            data["error"] = _to_plain(resp.error)
        return data

    def market_price(self, exchange: str, account: str, symbol: str) -> Dict[str, Any]:
        err = self.validate_exchange_account(exchange, account)
        if err:
            return {"success": False, "error": {"code": err, "message": err.replace("_", " ").title()}}
        resp = self.desk.execute(
            {
                "operation": "market_price",
                "exchange": exchange,
                "account": account,
                "symbol": symbol,
            }
        )
        data: Dict[str, Any] = {
            "success": bool(resp.success),
            "exchange": exchange,
            "account": account,
            "symbol": symbol,
        }
        if resp.market_price is not None:
            mp = _to_plain(resp.market_price)
            data["market_price"] = mp
            # convenience field
            data["price"] = mp.get("price") or mp.get("mark_price") or mp.get("last_external_price")
        if resp.error is not None:
            data["error"] = _to_plain(resp.error)
        return data

    def positions(self, exchange: str, account: str) -> Dict[str, Any]:
        err = self.validate_exchange_account(exchange, account)
        if err:
            return {"success": False, "error": {"code": err, "message": err.replace("_", " ").title()}, "positions": []}
        caps = set(self.desk.capabilities(exchange))
        op = "positions_management" if "positions_management" in caps else (
            "positions_orders" if "positions_orders" in caps else None
        )
        if op is None:
            return {
                "success": False,
                "error": {"code": "UNSUPPORTED", "message": "Exchange does not expose positions."},
                "positions": [],
            }
        resp = self.desk.execute({"operation": op, "exchange": exchange, "account": account})
        positions = []
        for p in resp.positions or []:
            row = _to_plain(p)
            # Normalize mark field if agents only provide pnl/entry
            row.setdefault("mark", None)
            positions.append(
                {
                    "symbol": row.get("symbol"),
                    "side": row.get("side"),
                    "size": row.get("size"),
                    "entry": row.get("entry_price"),
                    "mark": row.get("mark"),
                    "pnl": row.get("pnl"),
                    "sl": row.get("sl"),
                    "tp": row.get("tp"),
                    "exchange_instrument": row.get("exchange_instrument"),
                }
            )
        out: Dict[str, Any] = {
            "success": bool(resp.success),
            "exchange": exchange,
            "account": account,
            "positions": positions,
        }
        if resp.error is not None:
            out["error"] = _to_plain(resp.error)
        return out
