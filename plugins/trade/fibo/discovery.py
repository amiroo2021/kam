"""Phase 2.4 — exchange-agnostic venue-instrument discovery.

Public API (callable by the Fibo flow):

* ``list_market_catalog(exchange, account)``
* ``get_market_price(exchange, account, instrument)``

Both functions go through the SINGLE public boundary — the
``TradeDesk.execute({...})`` operation handler on the relevant
``x_<exchange>_agent``. The agents implement:

  * ``resolve_instrument`` — already public across all agents.
  * ``list_instruments``    — public when the agent can enumerate
                              a real venue catalog.
  * ``market_price``        — public when the agent exposes a
                              reliable read.

If an agent does NOT advertise / implement a given operation the
TradeDesk returns the normal ``NOT_IMPLEMENTED`` / ``UNSUPPORTED``
canonical failure. We surface that here as:

  * ``list_market_catalog`` returns ``"unavailable"`` (a sentinel
    string, NOT ``[]``) so Fibo can distinguish "no catalog
    adapter implemented" from "the catalog is genuinely empty".

  * ``get_market_price`` returns ``None``.

Fibo itself never branches on exchange name — see
``plugins/trade/fibo/candidates.py`` and ``flow.py`` for the
downstream consumers.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


CATALOG_UNAVAILABLE = "unavailable"   # sentinel, NOT a list


# ---------------------------------------------------------------------------
# TradeDesk binding (overridable for tests)
# ---------------------------------------------------------------------------

# Production imports TradeDesk lazily so tests can monkey-patch
# ``plugins.trade.fibo.discovery._get_desk`` (or
# ``plugins.trade.fibo.discovery._td_module``) to substitute a fake
# desk and stay fully offline. No production path ever bypasses this
# indirection.
import plugins.trade.tradedesk as _td_module  # noqa: E402


def _get_desk():
    """Return the configured TradeDesk.

    Production: returns ``_td_module.get_tradedesk()`` which lazily
    builds the real, agent-registered TradeDesk singleton.

    Tests: monkey-patch ``discovery._get_desk`` (preferred) OR
    ``discovery._td_module.get_tradedesk`` (also supported) to a
    callable returning a fake desk. The fake must NOT import real
    exchange agents — see ``plugins/trade/tests/fake_tradedesk.py``.
    """
    return _td_module.get_tradedesk()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_market_catalog(
    exchange: str, account: str
) -> Union[List[Dict[str, Any]], str]:
    """Read-only fetch of the venue's instrument catalog.

    Returns a list of normalized records (common schema) on
    success, or ``CATALOG_UNAVAILABLE`` when the agent does not
    implement ``list_instruments`` for this exchange.

    Distinguishing these two outcomes is critical — Fibo needs
    to know whether to show the candidate picker (real list
    available) or fall back to the manual-resolution screen
    (no enumeration possible).

    The common record schema (only ``instrument`` is required):

        {
            "instrument": "<venue-native canonical id>",   # required
            "display_name": "...",                         # optional
            "description": "...",                          # optional
            "market_type": "...",                          # optional
            "base": "...",                                 # optional
            "quote": "...",                                # optional
            "price": "..."                                  # optional
        }
    """
    if not exchange or not account:
        return CATALOG_UNAVAILABLE
    desk = _get_desk()
    try:
        resp = desk.execute({
            "operation": "list_instruments",
            "exchange": exchange,
            "account": account,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fibo_discovery: list_instruments(%s/%s) raised: %s",
            exchange, account, exc,
        )
        return CATALOG_UNAVAILABLE
    if not getattr(resp, "success", False):
        # Distinguish: did the agent actually return
        # NOT_IMPLEMENTED, or did the catalog genuinely come back
        # empty? TradeDesk returns success=True with
        # data["instruments"]=[] for an empty successful list.
        err = getattr(resp, "error", None)
        if err is not None and getattr(err, "code", None) in (
            "NOT_IMPLEMENTED", "UNSUPPORTED_OPERATION",
        ):
            logger.info(
                "fibo_discovery: list_instruments unsupported on %s",
                exchange,
            )
            return CATALOG_UNAVAILABLE
        logger.warning(
            "fibo_discovery: list_instruments(%s/%s) failed: %s",
            exchange, account,
            getattr(err, "message", None) if err else None,
        )
        return CATALOG_UNAVAILABLE
    data = getattr(resp, "data", None)
    if not isinstance(data, dict):
        return CATALOG_UNAVAILABLE
    records = data.get("instruments")
    if not isinstance(records, list):
        return CATALOG_UNAVAILABLE
    return [_validate_record(r) for r in records]


def get_market_price(
    exchange: str, account: str, instrument: str
) -> Optional[Decimal]:
    """Read-only fetch of the live market price for ``instrument``.

    Returns the ``Decimal`` price on success, or ``None`` if the
    agent does not implement ``market_price`` or the read fails.
    Missing prices NEVER block candidate selection.
    """
    if not exchange or not account or not instrument:
        return None
    desk = _get_desk()
    try:
        resp = desk.execute({
            "operation": "market_price",
            "exchange": exchange,
            "account": account,
            "symbol": instrument,
        })
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fibo_discovery: market_price(%s/%s/%s) raised: %s",
            exchange, account, instrument, exc,
        )
        return None
    if not getattr(resp, "success", False):
        return None
    # The agent exposes the price in one of several places
    # depending on version:
    #   - market_price.mark_price  (newer agents)
    #   - market_price.price
    #   - instrument.price         (older agents)
    candidates: List[Any] = []
    mp_obj = getattr(resp, "market_price", None)
    if mp_obj is not None:
        candidates.append(getattr(mp_obj, "mark_price", None))
        candidates.append(getattr(mp_obj, "price", None))
    inst = getattr(resp, "instrument", None)
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
# Internals
# ---------------------------------------------------------------------------


def _validate_record(raw: Any) -> Dict[str, Any]:
    """Normalize a single catalog record to the common schema.

    Only ``instrument`` is required; everything else may be
    missing or empty and is replaced with ``None``.
    """
    if not isinstance(raw, dict):
        return {"instrument": ""}
    instrument = raw.get("instrument")
    if not isinstance(instrument, str) or not instrument:
        # Records without a canonical id are unusable for Fibo.
        # Keep the slot empty so the caller can decide to skip.
        return {"instrument": ""}
    out: Dict[str, Any] = {"instrument": instrument}
    for key in ("display_name", "description", "market_type",
                "base", "quote", "price"):
        val = raw.get(key)
        if isinstance(val, str) and val:
            out[key] = val
        elif val is not None:
            out[key] = val
        else:
            out[key] = None
    return out


__all__ = [
    "CATALOG_UNAVAILABLE",
    "list_market_catalog",
    "get_market_price",
]
