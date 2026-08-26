"""Reusable fake TradeDesk for offline Fibo tests.

Phase 2.4: the production boundary is

    plugins.trade.fibo.discovery
        -> get_tradedesk()
        -> TradeDesk.execute({"operation": ...,
                              "exchange": ..., "account": ...,
                              "symbol": ...})

Tests must NEVER let an offline test reach a live venue. The cleanest
way to keep the boundary identical is to supply a fake ``TradeDesk``
that responds deterministically to ``execute({...})`` for each
operation the discovery layer issues:

    * resolve_instrument
    * list_instruments
    * market_price

Each operation handler returns a ``CanonicalResponse`` (built via
``make_success`` / ``make_failure``). Tests register the operations
they want; any unregistered operation returns the canonical
``NOT_IMPLEMENTED`` so the discovery layer behaves exactly like a
real exchange agent that has not been wired up.

The fake is also useful for ``TradeDesk.execute`` call counts and
argument inspection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from plugins.trade.canonical import CanonicalError, make_failure, make_success


# Resolver signature: (exchange, account, symbol) -> canonical str or None
ResolverFn = Callable[[str, str, str], Optional[str]]
# Catalog signature: (exchange, account) -> list[dict] (common schema)
CatalogFn = Callable[[str, str], List[Dict[str, Any]]]
# Price signature: (exchange, account, symbol) -> Decimal | None
PriceFn = Callable[[str, str, str], Optional[Decimal]]


def _make_not_implemented(operation: str, exchange: str, account: str) -> Any:
    return make_failure(
        operation=operation,
        exchange=exchange,
        account=account,
        code="NOT_IMPLEMENTED",
        message=f"{exchange} does not implement '{operation}'",
    )


def _make_canonical_not_implemented_error(operation: str, exchange: str, account: str) -> CanonicalError:
    """Return JUST the CanonicalError (mirrors what real agents embed)."""
    return CanonicalError(
        code="NOT_IMPLEMENTED",
        message=f"{exchange} does not implement '{operation}'",
    )


@dataclass
class FakeTradeDesk:
    """Deterministic, fully-offline TradeDesk replacement.

    The fake is intentionally a separate class — it does NOT
    import real exchange agents. This guarantees offline safety.
    """

    # resolve_instrument responder
    resolver: Optional[ResolverFn] = None
    # list_instruments responder
    catalog_fn: Optional[CatalogFn] = None
    # market_price responder
    price_fn: Optional[PriceFn] = None
    # Optional catalog / price caches (per key)
    catalog_map: Dict[Tuple[str, str], List[Dict[str, Any]]] = field(default_factory=dict)
    price_map: Dict[Tuple[str, str, str], Decimal] = field(default_factory=dict)
    # Call history
    calls: List[Dict[str, Any]] = field(default_factory=list)

    def execute(self, request: Dict[str, Any]) -> Any:
        if not isinstance(request, dict):
            raise TypeError(
                f"TradeDesk.execute expects a dict, got {type(request).__name__}"
            )
        op = str(request.get("operation") or "").strip()
        exchange = str(request.get("exchange") or "").strip()
        account = str(request.get("account") or "").strip()
        symbol = (
            str(request.get("symbol") or "").strip()
            or str(request.get("instrument") or "").strip()
        )
        # Capture a defensive copy of the call.
        self.calls.append({
            "operation": op,
            "exchange": exchange,
            "account": account,
            "symbol": symbol,
        })

        # MISSING / INVALID operation
        if not op:
            return make_failure(
                operation="",
                exchange=exchange, account=account,
                code="INVALID_REQUEST",
                message="Missing operation.",
            )
        if op == "resolve_instrument":
            return self._do_resolve(exchange, account, symbol)
        if op == "list_instruments":
            return self._do_list(exchange, account)
        if op == "market_price":
            return self._do_price(exchange, account, symbol)
        # Unknown operation: behave like a real agent that has NOT
        # implemented this op.
        return _make_not_implemented(op, exchange, account)

    # ---- per-op responders ----

    def _do_resolve(self, exchange: str, account: str, symbol: str) -> Any:
        if not exchange or not account:
            return make_failure(
                operation="resolve_instrument",
                exchange=exchange, account=account,
                code="MISSING_ACCOUNT",
                message="Missing exchange/account for resolve_instrument.",
            )
        if not symbol:
            return make_failure(
                operation="resolve_instrument",
                exchange=exchange, account=account,
                code="MISSING_SYMBOL",
                message="Symbol is required.",
            )
        if self.resolver is None:
            return _make_not_implemented("resolve_instrument", exchange, account)
        canonical = self.resolver(exchange, account, symbol)
        if canonical is None:
            return make_failure(
                operation="resolve_instrument",
                exchange=exchange, account=account,
                code="INSTRUMENT_NOT_FOUND",
                message=f"{exchange} has no instrument for '{symbol}'.",
            )
        return make_success(
            operation="resolve_instrument",
            exchange=exchange, account=account,
            instrument=_Builds.instrument(
                requested_symbol=symbol, canonical=canonical,
            ),
        )

    def _do_list(self, exchange: str, account: str) -> Any:
        if not exchange or not account:
            return make_failure(
                operation="list_instruments",
                exchange=exchange, account=account,
                code="MISSING_ACCOUNT",
                message="Account is required.",
            )
        # Pre-seeded explicit catalog → emit it.
        key = (exchange, account)
        if key in self.catalog_map:
            instruments = self.catalog_map[key]
            return make_success(
                operation="list_instruments",
                exchange=exchange, account=account,
                data={"instruments": list(instruments)},
            )
        # Programmable responder.
        if self.catalog_fn is not None:
            records = self.catalog_fn(exchange, account)
            return make_success(
                operation="list_instruments",
                exchange=exchange, account=account,
                data={"instruments": list(records)},
            )
        # Nothing registered — behave like an exchange that has
        # not implemented list_instruments yet.
        return _make_not_implemented("list_instruments", exchange, account)

    def _do_price(self, exchange: str, account: str, symbol: str) -> Any:
        if not exchange or not account:
            return make_failure(
                operation="market_price",
                exchange=exchange, account=account,
                code="MISSING_ACCOUNT",
                message="Account is required.",
            )
        if not symbol:
            return make_failure(
                operation="market_price",
                exchange=exchange, account=account,
                code="MISSING_SYMBOL",
                message="Symbol is required.",
            )
        # Pre-seeded explicit price → emit it.
        key = (exchange, account, symbol)
        if key in self.price_map:
            decimal_price = self.price_map[key]
            return make_success(
                operation="market_price",
                exchange=exchange, account=account,
                market_price=_Builds.market_price(
                    requested_symbol=symbol, price=decimal_price,
                ),
                instrument=_Builds.instrument(
                    requested_symbol=symbol, canonical=symbol,
                ),
            )
        if self.price_fn is not None:
            decimal_price = self.price_fn(exchange, account, symbol)
            if decimal_price is not None:
                return make_success(
                    operation="market_price",
                    exchange=exchange, account=account,
                    market_price=_Builds.market_price(
                        requested_symbol=symbol, price=decimal_price,
                    ),
                    instrument=_Builds.instrument(
                        requested_symbol=symbol, canonical=symbol,
                    ),
                )
            # Programmable responder returns None -> simulate
            # "agent has this op but the symbol has no price".
            return make_failure(
                operation="market_price",
                exchange=exchange, account=account,
                code="MARK_PRICE_NOT_FOUND",
                message=f"{exchange}: no price for '{symbol}'.",
            )
        return _make_not_implemented("market_price", exchange, account)


class _Builds:
    """Tiny helper that constructs the canonical domain objects.

    Kept private to this module so tests don't have to import the
    rest of the agent layer to fake a response.
    """

    @staticmethod
    def instrument(*, requested_symbol: str, canonical: str) -> Any:
        from plugins.trade.canonical import CanonicalInstrument
        return CanonicalInstrument(
            requested_symbol=requested_symbol,
            symbol=canonical,
            display_name=canonical,
        )

    @staticmethod
    def market_price(*, requested_symbol: str, price: Decimal) -> Any:
        from plugins.trade.canonical import CanonicalMarketPrice
        return CanonicalMarketPrice(
            requested_symbol=requested_symbol,
            market=requested_symbol,
            mark_price=str(price),
            price=str(price),
        )


__all__ = ["FakeTradeDesk"]
