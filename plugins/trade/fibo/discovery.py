"""Read-only venue-instrument discovery.

Phase 2.1: the Start Fibo flow needs a per-exchange, per-account
list of actual venue instruments so the user can pick the exact
contract (e.g. Ondo's ``ETH-USD.P``) the registration will target.

Phase 2.3: we also expose the raw catalog (with display name /
longName / tags / pair) and a per-market price read. Both are pure
GETs via the existing agent helpers — no new HTTP clients.

This module is read-only. The only HTTP calls it makes are
``GET /v1/markets`` (catalog) and ``GET /v1/perps/mark_prices``
(price) via the existing ``_signed_get`` helper in the Ondo agent.
No ``new_order`` / ``market_order`` / ``limit_order`` / ``cancel`` /
``close_position`` / ``stop_order`` / POST / PUT / PATCH / DELETE
methods exist here.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ondo helpers — pure GETs via the agent boundary
# ---------------------------------------------------------------------------


def list_ondoperps_instruments(account: str) -> List[str]:
    """Read-only fetch of Ondo Perps instrument identifiers.

    Returns the list of ``market`` field values from
    ``GET /v1/markets`` for the given account. Empty list on any
    error (network, auth, empty payload).
    """
    return [m["market"] for m in list_ondoperps_markets(account)]


def list_ondoperps_markets(account: str) -> List[Dict[str, Any]]:
    """Phase 2.3: read-only fetch of the full Ondo Perps catalog.

    Returns the raw catalog entries (``market``, ``displayName``,
    ``longName``, ``pair``, ``underlyingMarket``, ``tags``, ...).
    The flow MUST NOT reach into the exchange-specific fields —
    it should use ``candidates.rank_candidates`` which is generic.

    Pure GET of ``/v1/markets``. No price field is populated here;
    call ``get_ondoperps_price`` separately and pass the result to
    ``candidates.attach_price`` if needed.
    """
    try:
        from plugins.trade.agents import x_ondoperps_agent as A
        creds = A._lookup_credentials(account)
        if creds is None:
            logger.warning(
                "fibo_discovery: no credentials for account=%s", account
            )
            return []
        payload = A._signed_get(creds, A._PATH_MARKETS)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fibo_discovery: Ondo market fetch failed: %s", exc
        )
        return []

    if not isinstance(payload, dict):
        return []
    perps = payload.get("perps")
    entries: List[Dict[str, Any]] = []
    if isinstance(perps, dict):
        for value in perps.values():
            if isinstance(value, dict):
                entries.append(value)
            elif isinstance(value, list):
                entries.extend(
                    item for item in value if isinstance(item, dict)
                )
    elif isinstance(perps, list):
        entries.extend(item for item in perps if isinstance(item, dict))

    return [e for e in entries if isinstance(e, dict) and e.get("market")]


def get_ondoperps_price(account: str, market: str) -> Optional[Decimal]:
    """Phase 2.3: read-only fetch of the live mark price for one
    market on Ondo Perps.

    Returns the ``markPrice`` Decimal from the agent's
    ``market_price`` operation, or ``None`` if the read fails or
    the market is not present.

    Uses ONLY the public agent boundary — no private helpers.

    Pure GET of ``/v1/perps/mark_prices``. No write methods.
    """
    market = (market or "").strip()
    if not market:
        return None
    try:
        from plugins.trade.tradedesk import get_tradedesk
        desk = get_tradedesk()
        resp = desk.execute({
            "operation": "market_price",
            "exchange": "ondoperps",
            "account": account,
            "symbol": market,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fibo_discovery: market_price(%r) failed: %s", market, exc
        )
        return None
    if not getattr(resp, "success", False):
        return None
    # The agent exposes the price in two locations depending on
    # version: ``market_price.mark_price`` (newer) and
    # ``instrument.price`` (older fallback).
    mp_obj = getattr(resp, "market_price", None)
    inst = getattr(resp, "instrument", None)
    candidates: List[Any] = []
    if mp_obj is not None:
        candidates.append(getattr(mp_obj, "mark_price", None))
        candidates.append(getattr(mp_obj, "price", None))
    if inst is not None:
        candidates.append(getattr(inst, "price", None))
    for raw in candidates:
        if raw is None:
            continue
        try:
            d = Decimal(str(raw))
        except Exception:  # noqa: BLE001
            continue
        if d.is_finite():
            return d
    return None


# ---------------------------------------------------------------------------
# Per-exchange dispatch (kept for backward compatibility with
# Phase 2.1 / 2.2 — they consume List[str]).
# ---------------------------------------------------------------------------


INSTRUMENT_LISTERS: Dict[str, Callable[[str], List[str]]] = {
    "ondoperps": list_ondoperps_instruments,
}


def list_instruments(exchange: str, account: str) -> List[str]:
    """Dispatch to the per-exchange instrument lister.

    Unknown exchanges return ``[]``. The flow must handle empty
    lists gracefully (e.g. show a "type the instrument name" prompt
    instead of a button grid).
    """
    lister = INSTRUMENT_LISTERS.get((exchange or "").strip().lower())
    if lister is None:
        return []
    try:
        return lister(account)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fibo_discovery: lister for %s failed: %s", exchange, exc
        )
        return []


# Phase 2.3: full catalog fetcher dispatch. Future exchanges plug in
# here.
MARKET_CATALOG_LISTERS: Dict[str, Callable[[str], List[Dict[str, Any]]]] = {
    "ondoperps": list_ondoperps_markets,
}


def list_market_catalog(exchange: str, account: str) -> List[Dict[str, Any]]:
    """Phase 2.3: dispatch to the per-exchange catalog fetcher.

    Returns raw exchange-specific catalog entries. The flow MUST
    pass these through ``candidates.rank_candidates`` rather than
    reach into exchange-specific fields.
    """
    lister = MARKET_CATALOG_LISTERS.get((exchange or "").strip().lower())
    if lister is None:
        return []
    try:
        return lister(account)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fibo_discovery: catalog lister for %s failed: %s",
            exchange, exc,
        )
        return []


def get_market_price(
    exchange: str, account: str, market: str
) -> Optional[Decimal]:
    """Phase 2.3: per-exchange price read. Returns None when the
    exchange / market has no price reader or the read fails."""
    key = (exchange or "").strip().lower()
    if key == "ondoperps":
        return get_ondoperps_price(account, market)
    return None


__all__ = [
    "list_ondoperps_instruments",
    "list_ondoperps_markets",
    "get_ondoperps_price",
    "list_instruments",
    "list_market_catalog",
    "get_market_price",
]
