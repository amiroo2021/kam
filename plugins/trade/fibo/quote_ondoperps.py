"""OndoPerps-backed Fibo QuoteSource using agent-level market_price.

The quote source MUST NOT talk to Ondo endpoints directly. It requests the
reusable ``market_price`` operation from ``x_ondoperps_agent.py``, which:

  1. resolves the canonical symbol via the agent's own mapping,
  2. calls ``GET /v1/perps/mark_prices``,
  3. returns the native market + mark/oracle/external price fields.

For Fibo v1 we synthesize:

    Quote(bid=markPrice, ask=markPrice)

This works even when the selected account has NO position in the instrument,
because ``/v1/perps/mark_prices`` is an instrument-level venue price source,
not an account-position read.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .quote import Quote, QuoteSource


class OndoPerpsQuoteSource:
    def __init__(
        self,
        exchange_name: str,
        account_alias: str,
        agent: Any,
    ) -> None:
        self.exchange_name = str(exchange_name)
        self.account_alias = str(account_alias)
        self._agent = agent

    def current_bid_ask(self, symbol: str) -> Quote:
        target_symbol = str(symbol).strip().upper()
        response = self._agent.execute({
            "operation": "market_price",
            "exchange": self.exchange_name,
            "account": self.account_alias,
            "symbol": target_symbol,
        })
        if not _is_success(response):
            raise LookupError(
                f"OndoPerps market_price unavailable for "
                f"{self.exchange_name}:{self.account_alias}:{target_symbol}"
            )
        payload = _market_price_payload(response)
        if payload is None:
            raise LookupError(
                f"OndoPerps market_price returned no payload for {target_symbol}"
            )
        mark_text = _payload_field(payload, "mark_price") or _payload_field(payload, "markPrice")
        if not mark_text:
            mark_text = _payload_field(payload, "price")
        if not mark_text:
            raise LookupError(
                f"OndoPerps market_price carries no markPrice/price for {target_symbol}"
            )
        try:
            mark = Decimal(str(mark_text))
        except Exception as exc:  # noqa: BLE001
            raise LookupError(
                f"OndoPerps market_price for {target_symbol} was not parseable: {mark_text!r}"
            ) from exc
        if mark <= 0:
            raise LookupError(
                f"OndoPerps market_price for {target_symbol} is non-positive"
            )
        return Quote(bid=float(mark), ask=float(mark))


def _is_success(response: Any) -> bool:
    if response is None:
        return False
    if isinstance(response, dict):
        return bool(response.get("success"))
    return bool(getattr(response, "success", False))


def _market_price_payload(response: Any) -> Any:
    if response is None:
        return None
    if isinstance(response, dict):
        return response.get("market_price")
    return getattr(response, "market_price", None)


def _payload_field(payload: Any, name: str) -> str | None:
    if payload is None:
        return None
    if isinstance(payload, dict):
        value = payload.get(name)
    else:
        value = getattr(payload, name, None)
    text = str(value or "").strip()
    return text or None
