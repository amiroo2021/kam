"""One-sided Golden Fibo book lifecycle for BUY-only or SELL-only bots."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from .broker import Fill, Order, OrderSide, OrderType, PaperBroker, PositionLeg
from .constants import (
    BASE_SIZE,
    BGF,
    DEFAULT_MIN_STOP_DISTANCE,
    DEFAULT_TICK,
    MIN_LOT,
    SGF,
    SIZE_STEP,
    BotIdentity,
    Side,
)
from .ladder import MAX_STEP, PERCENTAGE, PHI, ladder_step, lot_size


@dataclass
class CycleClose:
    symbol: str
    bot_tag: str
    side: str
    cycle_id: int
    exit_price: float
    closed_legs: int


@dataclass
class SideBookState:
    symbol: str
    bot_tag: str
    side: str
    cycle_id: int = 0
    p0: Optional[float] = None
    last_sync_tp: Optional[float] = None
    highest_filled: int = -1
    active: bool = False
    closed_cycles: List[dict] = field(default_factory=list)


class SideBook:
    """A single side/symbol Golden Fibo virtual book."""

    def __init__(
        self,
        symbol: str,
        identity: BotIdentity | str,
        broker: PaperBroker,
        *,
        base_size: float = BASE_SIZE,
        size_step: float = SIZE_STEP,
        min_lot: float = MIN_LOT,
        phi: float = PHI,
        percentage: float = PERCENTAGE,
        max_step: int = MAX_STEP,
        tick: float = DEFAULT_TICK,
        min_stop_distance: float = DEFAULT_MIN_STOP_DISTANCE,
        state_path: str | Path | None = None,
    ) -> None:
        if isinstance(identity, str):
            identity = BGF if identity.lower() == "bgf" else SGF if identity.lower() == "sgf" else None  # type: ignore[assignment]
            if identity is None:
                raise ValueError("identity must be bgf/sgf or BotIdentity")
        self.symbol = symbol
        self.identity = identity
        self.side = identity.side
        self.broker = broker
        self.base_size = base_size
        self.size_step = size_step
        self.min_lot = min_lot
        self.phi = phi
        self.percentage = percentage
        self.max_step = max_step
        self.tick = tick
        self.min_stop_distance = min_stop_distance
        self.state_path = Path(state_path) if state_path else None
        self.state = SideBookState(symbol=symbol, bot_tag=identity.tag, side=identity.side.value)
        if self.state_path and self.state_path.exists():
            self.load_state()

    # ----- math helpers -----
    def p_tp(self, n: int) -> tuple[float, float]:
        if self.state.p0 is None:
            raise RuntimeError("cycle not started")
        return ladder_step(self.side, self.state.p0, n, phi=self.phi, percentage=self.percentage)

    def qty(self, n: int) -> float:
        return max(self.min_lot, lot_size(n, base=self.base_size, step=self.size_step))

    def normalize_price(self, px: float) -> float:
        if self.tick <= 0:
            return px
        return round(round(px / self.tick) * self.tick, 10)

    @property
    def order_side(self) -> OrderSide:
        return OrderSide.BUY if self.side is Side.BUY else OrderSide.SELL

    # ----- lifecycle -----
    def open_step0(self) -> PositionLeg:
        q = self.broker.get_quote(self.symbol)
        if q is None:
            raise RuntimeError(f"no quote for {self.symbol}")
        px = q.ask if self.side is Side.BUY else q.bid
        self.state.cycle_id += 1
        self.state.p0 = px
        _, tp = ladder_step(self.side, px, 0, phi=self.phi, percentage=self.percentage)
        self.state.last_sync_tp = self.normalize_price(tp)
        self.state.highest_filled = 0
        self.state.active = True
        order = self.broker.place_order(
            self.symbol,
            self.order_side,
            OrderType.MARKET,
            self.qty(0),
            tag=f"{self.identity.tag}:step0",
            bot_tag=self.identity.tag,
            step=0,
        )
        leg = self.broker.open_leg(
            self.symbol,
            self.order_side,
            order.qty,
            px,
            self.state.last_sync_tp,
            0,
            self.identity.tag,
            self.state.cycle_id,
            order.order_id,
            tag=order.tag,
        )
        self.ensure_pending_next()
        self.save_state()
        return leg

    def ensure_started(self) -> None:
        if not self.state.active or self.state.highest_filled < 0:
            self.open_step0()

    def _correct_side_and_distance(self, step_price: float, reference_price: float) -> bool:
        if self.side is Side.BUY:
            return step_price < reference_price and abs(step_price - reference_price) >= self.min_stop_distance
        return step_price > reference_price and abs(step_price - reference_price) >= self.min_stop_distance

    def _cancel_step_pendings(self) -> None:
        self.broker.cancel_orders(self.symbol, bot_tag=self.identity.tag, tags_prefix=f"{self.identity.tag}:step")

    def ensure_pending_next(self) -> list[Order]:
        """
        Maintain pending order(s) for next valid step. Handles gaps by walking
        forward until a valid same-side LIMIT can rest away from market; if steps
        were skipped, optionally places catch-up STOP at previous step.
        """
        if not self.state.active or self.state.p0 is None:
            return []
        q = self.broker.get_quote(self.symbol)
        if q is None:
            return []

        hf = self.state.highest_filled
        if hf >= self.max_step:
            self._cancel_step_pendings()
            return []

        reference = q.ask if self.side is Side.BUY else q.bid
        n = hf + 1
        while n <= self.max_step:
            p, _ = self.p_tp(n)
            p = self.normalize_price(p)
            if self._correct_side_and_distance(p, reference):
                break
            n += 1
        if n > self.max_step:
            return []

        # Keep only fresh next-step pendings for this book. Filled/canceled history remains.
        self._cancel_step_pendings()
        orders: list[Order] = []
        limit = self.broker.place_order(
            self.symbol,
            self.order_side,
            OrderType.LIMIT,
            self.qty(n),
            p,
            tag=f"{self.identity.tag}:step{n}",
            bot_tag=self.identity.tag,
            step=n,
        )
        orders.append(limit)

        if n > hf + 1:
            # Catch-up STOP at skipped previous step if it can legally sit far enough away.
            stop_p, _ = self.p_tp(n - 1)
            stop_p = self.normalize_price(stop_p)
            if abs(stop_p - reference) >= self.min_stop_distance:
                stop = self.broker.place_order(
                    self.symbol,
                    self.order_side,
                    OrderType.STOP,
                    self.qty(n - 1),
                    stop_p,
                    tag=f"{self.identity.tag}:step{n-1}:catchup",
                    bot_tag=self.identity.tag,
                    step=n - 1,
                )
                orders.append(stop)
        return orders

    def on_fill(self, fill: Fill, *, synthetic: bool = False) -> Optional[PositionLeg]:
        """Apply a fill that belongs to this book. Other side/bot fills are ignored."""
        if fill.symbol != self.symbol or fill.bot_tag != self.identity.tag:
            return None
        if fill.step is None:
            return None
        if not self.state.active or self.state.p0 is None:
            return None
        if fill.step <= self.state.highest_filled:
            return None
        if fill.step > self.max_step:
            return None

        step = fill.step
        expected_p, shared_tp = self.p_tp(step)
        entry = self.normalize_price(expected_p if synthetic else fill.price)
        self.state.highest_filled = step
        self.state.last_sync_tp = self.normalize_price(shared_tp)

        order_id = fill.order_id or f"synthetic-{self.identity.tag}-{self.state.cycle_id}-{step}"
        leg = self.broker.open_leg(
            self.symbol,
            self.order_side,
            fill.qty,
            entry,
            self.state.last_sync_tp,
            step,
            self.identity.tag,
            self.state.cycle_id,
            order_id,
            tag=fill.tag or f"{self.identity.tag}:step{step}",
        )
        # Rewrite TP on every open leg in this cycle to the shared TP.
        self.broker.set_legs_tp(
            self.symbol,
            self.identity.tag,
            self.state.last_sync_tp,
            cycle_id=self.state.cycle_id,
        )
        # Drop stale stop/limit and place the next one.
        self.ensure_pending_next()
        self.save_state()
        return leg

    def backfill_crossed_steps(self) -> list[PositionLeg]:
        """Paper-only synthetic price-touch advance through all crossed steps."""
        if not self.state.active or self.state.p0 is None:
            return []
        q = self.broker.get_quote(self.symbol)
        if q is None:
            return []
        made: list[PositionLeg] = []
        while self.state.highest_filled < self.max_step:
            nxt = self.state.highest_filled + 1
            p, _ = self.p_tp(nxt)
            p = self.normalize_price(p)
            crossed = q.ask <= p if self.side is Side.BUY else q.bid >= p
            if not crossed:
                break
            fill = Fill(
                order_id=f"synthetic-{self.identity.tag}-{self.state.cycle_id}-{nxt}",
                symbol=self.symbol,
                side=self.order_side,
                qty=self.qty(nxt),
                price=p,
                tag=f"{self.identity.tag}:step{nxt}",
                bot_tag=self.identity.tag,
                step=nxt,
                ts=q.ts,
            )
            leg = self.on_fill(fill, synthetic=True)
            if leg is None:
                break
            made.append(leg)
        return made

    def process_broker_fills(self) -> list[PositionLeg]:
        new_legs: list[PositionLeg] = []
        fills = self.broker.process_fills(self.symbol)
        for f in fills:
            leg = self.on_fill(f)
            if leg is not None:
                new_legs.append(leg)
        return new_legs

    def maybe_take_profit(self) -> Optional[CycleClose]:
        if not self.state.active or self.state.last_sync_tp is None:
            return None
        q = self.broker.get_quote(self.symbol)
        if q is None:
            return None
        hit = q.bid >= self.state.last_sync_tp if self.side is Side.BUY else q.ask <= self.state.last_sync_tp
        if not hit:
            return None
        exit_px = self.state.last_sync_tp
        legs = self.broker.close_all_legs(
            self.symbol,
            self.identity.tag,
            cycle_id=self.state.cycle_id,
            exit_price=exit_px,
        )
        self.broker.cancel_orders(self.symbol, bot_tag=self.identity.tag)
        close = CycleClose(
            symbol=self.symbol,
            bot_tag=self.identity.tag,
            side=self.side.value,
            cycle_id=self.state.cycle_id,
            exit_price=exit_px,
            closed_legs=len(legs),
        )
        self.state.closed_cycles.append(asdict(close))
        self.state.active = False
        self.state.p0 = None
        self.state.last_sync_tp = None
        self.state.highest_filled = -1
        self.save_state()
        # Immediately open a new cycle per spec.
        self.open_step0()
        return close

    def on_tick(self, *, paper_backfill: bool = True) -> None:
        self.ensure_started()
        if paper_backfill:
            self.backfill_crossed_steps()
        self.process_broker_fills()
        self.maybe_take_profit()
        self.ensure_pending_next()
        self.save_state()

    def status_digest(self) -> str:
        if not self.state.active or self.state.p0 is None:
            return f"{self.symbol} {self.side.value} idle cycle={self.state.cycle_id}"
        hf = self.state.highest_filled
        next_p, _ = self.p_tp(min(hf + 1, self.max_step))
        shared = self.state.last_sync_tp
        return (
            f"{self.symbol} {self.side.value} step={hf} "
            f"sharedTP={self.normalize_price(shared or 0):.2f} "
            f"nextP={self.normalize_price(next_p):.2f} (P{min(hf + 1, self.max_step)}) "
            f"cycle={self.state.cycle_id}"
        )

    # ----- persistence -----
    def save_state(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(asdict(self.state), indent=2, sort_keys=True))

    def load_state(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        data = json.loads(self.state_path.read_text())
        self.state = SideBookState(**data)
