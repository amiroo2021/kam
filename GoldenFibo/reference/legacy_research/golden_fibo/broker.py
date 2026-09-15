"""Paper broker: real quotes in, local fills/orders/positions out. No live orders."""

from __future__ import annotations

import itertools
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELED = "CANCELED"


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    mid: float
    ts: float = field(default_factory=time.time)

    @classmethod
    def from_mid(cls, symbol: str, mid: float, spread: float = 0.0, ts: float | None = None) -> "Quote":
        half = spread / 2.0
        return cls(
            symbol=symbol,
            bid=mid - half,
            ask=mid + half,
            mid=mid,
            ts=time.time() if ts is None else ts,
        )


@dataclass
class Order:
    order_id: str
    symbol: str
    side: OrderSide
    otype: OrderType
    qty: float
    price: Optional[float]  # limit/stop price; None for market
    tag: str = ""
    bot_tag: str = ""  # bgf / sgf
    step: Optional[int] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_price: Optional[float] = None
    filled_ts: Optional[float] = None
    created_ts: float = field(default_factory=time.time)


@dataclass
class PositionLeg:
    leg_id: str
    symbol: str
    side: OrderSide  # direction of the open position (BUY long / SELL short)
    qty: float
    entry: float
    tp: float
    step: int
    bot_tag: str
    cycle_id: int
    order_id: str
    tag: str = ""


@dataclass
class Fill:
    order_id: str
    symbol: str
    side: OrderSide
    qty: float
    price: float
    tag: str
    bot_tag: str
    step: Optional[int]
    ts: float


class PaperBroker:
    """
    Local order book + positions. Quotes come from venue polls.
    Fills are simulated on price touch — never sends live orders.
    """

    def __init__(self) -> None:
        self._quotes: Dict[str, Quote] = {}
        self._orders: Dict[str, Order] = {}
        self._legs: Dict[str, PositionLeg] = {}
        self._fills: List[Fill] = []
        self._id_seq = itertools.count(1)

    # ----- quotes -----
    def set_quote(
        self,
        symbol: str,
        *,
        mid: float | None = None,
        bid: float | None = None,
        ask: float | None = None,
        spread: float = 0.0,
        ts: float | None = None,
    ) -> Quote:
        if mid is not None and bid is None and ask is None:
            q = Quote.from_mid(symbol, mid, spread=spread, ts=ts)
        else:
            if bid is None or ask is None:
                raise ValueError("provide mid, or both bid and ask")
            m = mid if mid is not None else (bid + ask) / 2.0
            q = Quote(symbol=symbol, bid=bid, ask=ask, mid=m, ts=ts or time.time())
        self._quotes[symbol] = q
        return q

    def get_quote(self, symbol: str) -> Optional[Quote]:
        return self._quotes.get(symbol)

    # ----- orders -----
    def _new_id(self, prefix: str = "o") -> str:
        return f"{prefix}-{next(self._id_seq)}-{uuid.uuid4().hex[:6]}"

    def place_order(
        self,
        symbol: str,
        side: OrderSide | str,
        otype: OrderType | str,
        qty: float,
        price: float | None = None,
        *,
        tag: str = "",
        bot_tag: str = "",
        step: int | None = None,
        fill_market_now: bool = True,
    ) -> Order:
        side = OrderSide(side)
        otype = OrderType(otype)
        if qty <= 0:
            raise ValueError("qty must be > 0")
        if otype is not OrderType.MARKET and price is None:
            raise ValueError(f"{otype} requires price")

        order = Order(
            order_id=self._new_id("ord"),
            symbol=symbol,
            side=side,
            otype=otype,
            qty=qty,
            price=price,
            tag=tag,
            bot_tag=bot_tag,
            step=step,
        )
        self._orders[order.order_id] = order

        if otype is OrderType.MARKET and fill_market_now:
            self._fill_market(order)
        return order

    def cancel_order(self, order_id: str) -> bool:
        o = self._orders.get(order_id)
        if o is None or o.status is not OrderStatus.PENDING:
            return False
        o.status = OrderStatus.CANCELED
        return True

    def cancel_orders(
        self,
        symbol: str,
        *,
        bot_tag: str | None = None,
        tags_prefix: str | None = None,
    ) -> int:
        n = 0
        for o in list(self._orders.values()):
            if o.symbol != symbol or o.status is not OrderStatus.PENDING:
                continue
            if bot_tag is not None and o.bot_tag != bot_tag:
                continue
            if tags_prefix is not None and not o.tag.startswith(tags_prefix):
                continue
            o.status = OrderStatus.CANCELED
            n += 1
        return n

    def pending_orders(
        self,
        symbol: str | None = None,
        *,
        bot_tag: str | None = None,
    ) -> List[Order]:
        out = []
        for o in self._orders.values():
            if o.status is not OrderStatus.PENDING:
                continue
            if symbol is not None and o.symbol != symbol:
                continue
            if bot_tag is not None and o.bot_tag != bot_tag:
                continue
            out.append(o)
        return out

    def get_order(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    # ----- positions / legs -----
    def open_leg(
        self,
        symbol: str,
        side: OrderSide | str,
        qty: float,
        entry: float,
        tp: float,
        step: int,
        bot_tag: str,
        cycle_id: int,
        order_id: str,
        tag: str = "",
    ) -> PositionLeg:
        leg = PositionLeg(
            leg_id=self._new_id("leg"),
            symbol=symbol,
            side=OrderSide(side),
            qty=qty,
            entry=entry,
            tp=tp,
            step=step,
            bot_tag=bot_tag,
            cycle_id=cycle_id,
            order_id=order_id,
            tag=tag,
        )
        self._legs[leg.leg_id] = leg
        return leg

    def legs(
        self,
        symbol: str | None = None,
        *,
        bot_tag: str | None = None,
        cycle_id: int | None = None,
    ) -> List[PositionLeg]:
        out = []
        for leg in self._legs.values():
            if symbol is not None and leg.symbol != symbol:
                continue
            if bot_tag is not None and leg.bot_tag != bot_tag:
                continue
            if cycle_id is not None and leg.cycle_id != cycle_id:
                continue
            out.append(leg)
        return out

    def set_legs_tp(
        self,
        symbol: str,
        bot_tag: str,
        tp: float,
        *,
        cycle_id: int | None = None,
    ) -> int:
        n = 0
        for leg in self.legs(symbol, bot_tag=bot_tag, cycle_id=cycle_id):
            leg.tp = tp
            n += 1
        return n

    def close_all_legs(
        self,
        symbol: str,
        bot_tag: str,
        *,
        cycle_id: int | None = None,
        exit_price: float | None = None,
    ) -> List[PositionLeg]:
        closed = []
        for leg in list(self.legs(symbol, bot_tag=bot_tag, cycle_id=cycle_id)):
            closed.append(leg)
            del self._legs[leg.leg_id]
        return closed

    def position_qty(self, symbol: str, bot_tag: str) -> float:
        """Net qty: BUY legs positive, SELL legs negative."""
        q = 0.0
        for leg in self.legs(symbol, bot_tag=bot_tag):
            if leg.side is OrderSide.BUY:
                q += leg.qty
            else:
                q -= leg.qty
        return q

    # ----- fill engine -----
    def process_fills(self, symbol: str | None = None) -> List[Fill]:
        """Evaluate pending LIMIT/STOP against current quotes. Returns new fills."""
        new_fills: List[Fill] = []
        symbols = [symbol] if symbol else list(self._quotes.keys())
        for sym in symbols:
            q = self._quotes.get(sym)
            if q is None:
                continue
            for o in list(self.pending_orders(sym)):
                if self._try_fill(o, q):
                    fill = Fill(
                        order_id=o.order_id,
                        symbol=o.symbol,
                        side=o.side,
                        qty=o.qty,
                        price=o.filled_price or 0.0,
                        tag=o.tag,
                        bot_tag=o.bot_tag,
                        step=o.step,
                        ts=o.filled_ts or time.time(),
                    )
                    self._fills.append(fill)
                    new_fills.append(fill)
        return new_fills

    def _fill_market(self, order: Order) -> None:
        q = self._quotes.get(order.symbol)
        if q is None:
            raise RuntimeError(f"no quote for {order.symbol}")
        px = q.ask if order.side is OrderSide.BUY else q.bid
        order.status = OrderStatus.FILLED
        order.filled_price = px
        order.filled_ts = time.time()
        fill = Fill(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            price=px,
            tag=order.tag,
            bot_tag=order.bot_tag,
            step=order.step,
            ts=order.filled_ts,
        )
        self._fills.append(fill)

    def _try_fill(self, order: Order, q: Quote) -> bool:
        if order.status is not OrderStatus.PENDING or order.price is None:
            return False
        px = order.price
        filled = False
        fill_px = px

        if order.otype is OrderType.LIMIT:
            if order.side is OrderSide.BUY and q.ask <= px:
                filled = True
                fill_px = min(px, q.ask)  # price improvement to ask
            elif order.side is OrderSide.SELL and q.bid >= px:
                filled = True
                fill_px = max(px, q.bid)
        elif order.otype is OrderType.STOP:
            # stop triggers when market trades through stop price, fills at trigger/market
            if order.side is OrderSide.BUY and q.ask >= px:
                filled = True
                fill_px = max(px, q.ask)
            elif order.side is OrderSide.SELL and q.bid <= px:
                filled = True
                fill_px = min(px, q.bid)

        if not filled:
            return False
        order.status = OrderStatus.FILLED
        order.filled_price = fill_px
        order.filled_ts = time.time()
        return True

    def recent_fills(self, n: int = 50) -> List[Fill]:
        return self._fills[-n:]

    def snapshot(self, symbol: str, bot_tag: str) -> dict:
        return {
            "quote": self._quotes.get(symbol),
            "pending": [o.__dict__.copy() for o in self.pending_orders(symbol, bot_tag=bot_tag)],
            "legs": [leg.__dict__.copy() for leg in self.legs(symbol, bot_tag=bot_tag)],
            "net_qty": self.position_qty(symbol, bot_tag),
        }
