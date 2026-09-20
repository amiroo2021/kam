"""Shared instrument candidate picker used by Telegram /trade and WebTrade.

Mirrors TradeWizard._build_priced_candidates / catalog ranking / market_price
enrichment so both surfaces show the same native symbols and prices for a
given (exchange, account, query). Does not invent venue instrument ids.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Single shared safety cap for Telegram /trade and WebTrade candidate chips.
# Must stay identical so both surfaces show the same native list (e.g. silver → 5).
INSTRUMENT_PICK_MAX = 5
# Back-compat aliases (same value — never diverge).
INSTRUMENT_PICK_MAX_TELEGRAM = INSTRUMENT_PICK_MAX
INSTRUMENT_PICK_MAX_TRADEMENU = INSTRUMENT_PICK_MAX

_CONCEPT_GROUPS: Tuple[frozenset, ...] = (
    frozenset({"GOLD", "XAU", "XAUUSD", "XAUUSDT", "XAUUST"}),
    frozenset({"SILVER", "XAG", "XAGUSD", "XAGUSDT"}),
    frozenset({"OIL", "WTI", "BRENT", "CRUDE", "CL", "OILUSD", "CRUDEOIL"}),
    frozenset({"NATGAS", "NG", "GAS", "HENRY", "NATGASUSD"}),
    frozenset({"BTC", "BITCOIN", "XBT", "BTCUSD", "BTCUSDT"}),
    frozenset({"ETH", "ETHEREUM", "ETHER", "ETHUSD", "ETHUSDT"}),
    frozenset({"SOL", "SOLANA", "SOLUSD", "SOLUSDT"}),
)
_QUOTE_NOISE = frozenset({"USD", "USDT", "USDC", "PERP", "P", "USDTM", "USDCM"})


def candidate_symbol(item: Any) -> str:
    if isinstance(item, dict):
        return str(
            item.get("symbol")
            or item.get("native_symbol")
            or item.get("instrument")
            or item.get("market")
            or item.get("route_symbol")
            or ""
        ).strip()
    return str(item or "").strip()


def _tokenize_instrument(text: str) -> List[str]:
    raw = str(text or "").strip().upper()
    if not raw:
        return []
    parts = [p for p in re.split(r"[^A-Z0-9]+", raw) if p]
    out: List[str] = []
    for p in parts:
        if p in _QUOTE_NOISE:
            continue
        out.append(p)
        for suffix in ("USDT", "USDC", "USD", "PERP"):
            if p.endswith(suffix) and len(p) > len(suffix):
                base = p[: -len(suffix)]
                if base and base not in out:
                    out.append(base)
    return out


def symbol_search_hints(requested: str) -> List[str]:
    raw = str(requested or "").strip().upper()
    if not raw:
        return []
    hints: List[str] = [raw]
    hints.extend(_tokenize_instrument(raw))
    for suffix in ("USDT", "USDC", "USD", "PERP"):
        if raw.endswith(suffix) and len(raw) > len(suffix):
            prefix = raw[: -len(suffix)]
            if prefix and prefix not in hints:
                hints.append(prefix)
    expanded: List[str] = []
    for seed in list(hints):
        for group in _CONCEPT_GROUPS:
            if seed in group:
                for token in group:
                    if token not in hints and token not in expanded:
                        expanded.append(token)
    hints.extend(expanded)
    seen: set[str] = set()
    out: List[str] = []
    for h in hints:
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out


def list_instruments(desk: Any, exchange: str, account: str) -> List[Dict[str, Any]]:
    try:
        response = desk.execute(
            {
                "operation": "list_instruments",
                "exchange": exchange,
                "account": account,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_instruments failed: %s", exc)
        return []
    if not getattr(response, "success", False):
        return []
    data = getattr(response, "data", None)
    if not isinstance(data, dict):
        return []
    records = data.get("instruments")
    if not isinstance(records, list):
        return []
    return [item for item in records if isinstance(item, dict)]


def market_price_text(desk: Any, exchange: str, account: str, symbol: str) -> Optional[str]:
    try:
        response = desk.execute(
            {
                "operation": "market_price",
                "exchange": exchange,
                "account": account,
                "symbol": symbol,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("market_price(%s) failed: %s", symbol, exc)
        return None
    if not getattr(response, "success", False):
        return None
    candidates: List[Any] = []
    mp_obj = getattr(response, "market_price", None)
    if mp_obj is not None:
        if isinstance(mp_obj, dict):
            candidates.extend(
                [mp_obj.get("mark_price"), mp_obj.get("price"), mp_obj.get("last_external_price")]
            )
        else:
            candidates.extend(
                [
                    getattr(mp_obj, "mark_price", None),
                    getattr(mp_obj, "price", None),
                    getattr(mp_obj, "last_external_price", None),
                ]
            )
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        candidates.extend([data.get("mark_price"), data.get("price"), data.get("mark")])
    for raw in candidates:
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        try:
            dec = Decimal(text)
        except Exception:  # noqa: BLE001
            continue
        if dec.is_finite():
            return format(dec.normalize(), "f")
    return None


def rank_catalog_candidates(catalog: List[Dict[str, Any]], requested: str) -> List[Dict[str, Any]]:
    if not catalog:
        return []
    try:
        from plugins.trade.fibo.candidates import rank_candidates

        ranked = rank_candidates(catalog, requested)
        out: List[Dict[str, Any]] = []
        for cand in ranked:
            if int(getattr(cand, "score", 0) or 0) <= 0:
                continue
            sym = str(getattr(cand, "instrument", "") or "").strip()
            if not sym:
                continue
            price = getattr(cand, "price", None)
            entry: Dict[str, Any] = {
                "symbol": sym,
                "display_name": str(getattr(cand, "display_name", "") or "").strip(),
                "score": int(getattr(cand, "score", 0) or 0),
            }
            if price is not None:
                entry["price"] = (
                    format(price.normalize(), "f") if hasattr(price, "normalize") else str(price)
                )
            out.append(entry)
        if out:
            best = max(int(e.get("score") or 0) for e in out)
            floor = 40 if best >= 40 else max(15, int(best * 0.5))
            strong = [e for e in out if int(e.get("score") or 0) >= floor]
            return strong or out[: min(3, len(out))]
    except Exception as exc:  # noqa: BLE001
        logger.warning("rank_candidates fallback: %s", exc)

    req = str(requested or "").strip().upper()
    hints = symbol_search_hints(req)
    hint_set = {h.upper() for h in hints}
    scored: List[Tuple[int, float, str, Dict[str, Any]]] = []

    for raw in catalog:
        if not isinstance(raw, dict):
            continue
        sym = candidate_symbol(raw)
        if not sym:
            continue
        up = sym.upper()
        base = str(raw.get("base") or "").strip().upper()
        display = str(raw.get("display_name") or raw.get("displayName") or "").strip().upper()
        desc = str(
            raw.get("description") or raw.get("long_name") or raw.get("longName") or ""
        ).strip().upper()
        tokens = set(_tokenize_instrument(up))
        if base:
            tokens.update(_tokenize_instrument(base))
        if display:
            tokens.update(_tokenize_instrument(display))
        if desc:
            tokens.update(_tokenize_instrument(desc))

        score = 0
        if up == req:
            score += 100
        if base and base == req:
            score += 80
        overlap = tokens & hint_set
        if overlap:
            score += 90 if any(t == req or t in hint_set for t in overlap) else 60
            if any(len(t) >= 3 and t in tokens for t in hint_set):
                score += 10
        blob = " ".join(x for x in (up, base, display, desc) if x)
        for h in hint_set:
            if len(h) >= 2 and h in blob:
                score += 25
                break
        best_ratio = 0.0
        for cand_txt in (up, base, display, *tokens):
            if not cand_txt:
                continue
            r = SequenceMatcher(None, req, cand_txt).ratio()
            if r > best_ratio:
                best_ratio = r
            for h in hint_set:
                if h == req:
                    continue
                r2 = SequenceMatcher(None, h, cand_txt).ratio()
                if r2 > best_ratio:
                    best_ratio = r2
        if best_ratio >= 0.75:
            score += 40
        elif best_ratio >= 0.55:
            score += 20
        elif best_ratio >= 0.4:
            score += 8

        entry = {
            "symbol": sym,
            "display_name": str(raw.get("display_name") or raw.get("displayName") or "").strip(),
            "score": score,
            "fuzzy": round(best_ratio, 3),
        }
        if raw.get("price") is not None:
            entry["price"] = str(raw.get("price"))
        scored.append((score, best_ratio, up, entry))

    positive = [t for t in scored if t[0] > 0]
    positive.sort(key=lambda t: (-t[0], -t[1], t[2]))
    if positive:
        best = positive[0][0]
        floor = 40 if best >= 40 else max(15, int(best * 0.5))
        keep = [t for t in positive if t[0] >= floor or (t[0] >= 25 and t[1] >= 0.7)]
        if not keep:
            keep = positive[: min(3, len(positive))]
        return [t[3] for t in keep]

    fuzzy = [t for t in scored if t[1] >= 0.45]
    fuzzy.sort(key=lambda t: (-t[1], t[2]))
    out_fuzzy: List[Dict[str, Any]] = []
    for _score, ratio, _up, entry in fuzzy[:INSTRUMENT_PICK_MAX_TELEGRAM]:
        row = dict(entry)
        row["score"] = max(1, int(ratio * 100))
        out_fuzzy.append(row)
    return out_fuzzy


def enrich_candidate_prices(
    desk: Any,
    exchange: str,
    account: str,
    candidates: List[Dict[str, Any]],
    *,
    limit: int = INSTRUMENT_PICK_MAX_TELEGRAM,
    capabilities: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    caps = set(capabilities or [])
    supports_price = (not caps) or ("market_price" in caps)
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        sym = candidate_symbol(item)
        if not sym:
            continue
        key = sym.upper()
        if key in seen:
            continue
        seen.add(key)
        entry = dict(item) if isinstance(item, dict) else {"symbol": sym}
        entry["symbol"] = sym
        # Normalize native field for WebTrade / Telegram consumers.
        entry["native_symbol"] = sym
        if entry.get("display_name") in (None, ""):
            entry["display_name"] = sym
        if not entry.get("price") and supports_price:
            price = market_price_text(desk, exchange, account, sym)
            if price:
                entry["price"] = price
                entry["last_price"] = price
        elif entry.get("price") is not None:
            entry["last_price"] = entry.get("price")
        out.append(entry)
        if len(out) >= limit:
            break
    return out


def build_priced_candidates(
    desk: Any,
    exchange: str,
    account: str,
    requested: str,
    *,
    primary_native: Optional[str] = None,
    agent_candidates: Optional[List[Any]] = None,
    limit: int = INSTRUMENT_PICK_MAX_TELEGRAM,
    capabilities: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Same merge order as TradeWizard._build_priced_candidates."""
    caps = set(capabilities or [])
    if not caps:
        try:
            caps = set(desk.capabilities(exchange) or [])
        except Exception:  # noqa: BLE001
            caps = set()

    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def _push(sym: str, **extra: Any) -> None:
        key = sym.upper()
        if not sym or key in seen:
            return
        seen.add(key)
        row = {"symbol": sym, "native_symbol": sym}
        row.update({k: v for k, v in extra.items() if v is not None and v != ""})
        merged.append(row)

    if primary_native:
        _push(str(primary_native).strip(), score=1000, primary=True)

    for item in agent_candidates or []:
        sym = candidate_symbol(item)
        if not sym:
            continue
        extra: Dict[str, Any] = {}
        if isinstance(item, dict):
            if item.get("price") is not None:
                extra["price"] = item.get("price")
            if item.get("display_name"):
                extra["display_name"] = item.get("display_name")
            if item.get("price_increment") is not None:
                extra["price_increment"] = item.get("price_increment")
            if item.get("size_increment") is not None:
                extra["size_increment"] = item.get("size_increment")
        _push(sym, score=900, **extra)

    if "list_instruments" in caps:
        catalog = list_instruments(desk, exchange, account)
        for ranked in rank_catalog_candidates(catalog, requested):
            _push(
                str(ranked.get("symbol") or "").strip(),
                score=ranked.get("score"),
                display_name=ranked.get("display_name"),
                price=ranked.get("price"),
            )

    if not merged and primary_native:
        _push(str(primary_native).strip(), score=1000, primary=True)

    return enrich_candidate_prices(
        desk, exchange, account, merged, limit=limit, capabilities=list(caps)
    )


def resolve_with_candidates(
    desk: Any,
    exchange: str,
    account: str,
    symbol: str,
    *,
    limit: int = INSTRUMENT_PICK_MAX,
) -> Dict[str, Any]:
    """One-shot resolve for WebTrade: unique | ambiguous (priced) | not found.

    Returns a plain dict consumable by WebTradeService /api/instruments/resolve.
    """
    requested = str(symbol or "").strip()
    try:
        caps = list(desk.capabilities(exchange) or [])
    except Exception:  # noqa: BLE001
        caps = []

    primary_native: Optional[str] = None
    agent_error_candidates: List[Any] = []
    resolve_error_code: Optional[str] = None
    resolve_error_message: Optional[str] = None
    instrument_payload: Optional[Dict[str, Any]] = None
    format_meta: Dict[str, Any] = {}

    if "resolve_instrument" in caps:
        try:
            response = desk.execute(
                {
                    "operation": "resolve_instrument",
                    "exchange": exchange,
                    "account": account,
                    "symbol": requested,
                }
            )
        except Exception as exc:  # noqa: BLE001
            resolve_error_code = "AGENT_EXCEPTION"
            resolve_error_message = str(exc)
            response = None
        if response is not None:
            if getattr(response, "success", False):
                inst = getattr(response, "instrument", None)
                if inst is not None:
                    if hasattr(inst, "to_dict"):
                        instrument_payload = inst.to_dict()
                    elif isinstance(inst, dict):
                        instrument_payload = dict(inst)
                    else:
                        instrument_payload = {
                            "symbol": str(getattr(inst, "symbol", "") or "").strip(),
                            "display_name": str(getattr(inst, "display_name", "") or "").strip(),
                            "requested_symbol": str(getattr(inst, "requested_symbol", "") or "").strip(),
                            "price_increment": getattr(inst, "price_increment", None),
                            "size_increment": getattr(inst, "size_increment", None),
                        }
                    primary_native = str((instrument_payload or {}).get("symbol") or "").strip() or None
                    if instrument_payload:
                        format_meta = {
                            k: instrument_payload.get(k)
                            for k in (
                                "price_increment",
                                "size_increment",
                                "price_decimals",
                                "size_decimals",
                            )
                            if instrument_payload.get(k) is not None
                        }
                if not primary_native:
                    resolve_error_code = "INSTRUMENT_NOT_FOUND"
                    resolve_error_message = "Instrument not found."
            else:
                err = getattr(response, "error", None)
                resolve_error_code = str(getattr(err, "code", "") or "INSTRUMENT_NOT_FOUND")
                resolve_error_message = str(
                    getattr(err, "message", "") or "Instrument not found."
                )
                data = getattr(response, "data", None)
                if isinstance(data, dict):
                    raw_cands = data.get("candidates")
                    if isinstance(raw_cands, list):
                        agent_error_candidates = list(raw_cands)

    # Unique success with no need to pick — same as Telegram continuing after Agree.
    if primary_native and resolve_error_code is None:
        # Still build candidates for optional confirm UI; WebTrade may open chart directly.
        candidates = build_priced_candidates(
            desk,
            exchange,
            account,
            requested,
            primary_native=primary_native,
            agent_candidates=agent_error_candidates,
            limit=limit,
            capabilities=caps,
        )
        return {
            "success": True,
            "status": "resolved",
            "query": requested,
            "requested_symbol": requested,
            "native_symbol": primary_native,
            "instrument": instrument_payload or {"symbol": primary_native},
            "display": f"{requested} → {primary_native}",
            "format_meta": format_meta,
            "candidates": candidates,
        }

    candidates = build_priced_candidates(
        desk,
        exchange,
        account,
        requested,
        primary_native=None,
        agent_candidates=agent_error_candidates,
        limit=limit,
        capabilities=caps,
    )

    if candidates:
        # Ambiguous or catalog-only hits — user must pick (never auto-guess).
        return {
            "success": False,
            "status": "ambiguous",
            "query": requested,
            "requested_symbol": requested,
            "native_symbol": None,
            "display": f"{requested} → multiple matches",
            "error": {
                "code": resolve_error_code or "INSTRUMENT_AMBIGUOUS",
                "message": resolve_error_message
                or f'Multiple instruments match "{requested}".',
            },
            "candidates": candidates,
        }

    return {
        "success": False,
        "status": "not_found",
        "query": requested,
        "requested_symbol": requested,
        "native_symbol": None,
        "display": f"{requested} → unresolved",
        "error": {
            "code": resolve_error_code or "INSTRUMENT_NOT_FOUND",
            "message": resolve_error_message or "Instrument not found.",
        },
        "candidates": [],
    }
