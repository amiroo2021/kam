"""Historical OHLC → ordered MarketEvents (legacy + strict ambiguity)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterator, List, Sequence

from ..engine.ambiguity import ambiguity_payload, dual_touch
from ..engine.config import EngineConfig, OhlcResolveMode, Side
from ..engine.engine import GoldenFiboEngine
from ..engine.events import MarketEvent, MarketEventKind
from ..engine.levels import ladder_step
from ..engine.state import EngineState


def _dec(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _parse_candle(candle: Sequence[Any]) -> tuple[int, Decimal, Decimal, Decimal, Decimal]:
    ts = int(candle[0])
    o, h, l, c = _dec(candle[1]), _dec(candle[2]), _dec(candle[3]), _dec(candle[4])
    return ts, o, h, l, c


class HistoricalOhlcFeeder:
    """Convert OHLC candles into MarketEvents.

    LEGACY: adverse progression touches first, then TP (recovered historical_replay).
    After TP, engine chains P0=TP; feeder ignores the rest of that candle.

    STRICT: if bar touches both TP and next progression, emit AMBIGUOUS_BAR only
    (no path mutation for that bar).
    """

    def __init__(
        self,
        candles: Sequence[Sequence[Any]],
        config: EngineConfig,
        *,
        mode: OhlcResolveMode = OhlcResolveMode.LEGACY,
    ) -> None:
        self.candles = list(candles)
        self.config = config
        self.mode = mode

    def iter_events(self) -> Iterator[MarketEvent]:
        shadow = GoldenFiboEngine(self.config)
        for candle in self.candles:
            yield from self._events_for_candle(candle, shadow)

    def collect(self) -> List[MarketEvent]:
        return list(self.iter_events())

    def _events_for_candle(self, candle: Sequence[Any], shadow: GoldenFiboEngine) -> Iterator[MarketEvent]:
        ts, o, h, l, c = _parse_candle(candle)
        cfg = self.config
        side = cfg.side
        st = shadow.state

        if not st.active or st.p0 is None:
            ev = MarketEvent(kind=MarketEventKind.SEED_P0, ts_ms=ts, price=o)
            shadow.on_event(ev)
            yield ev
            st = shadow.state

        assert st.p0 is not None and st.shared_tp is not None
        p0: Decimal = st.p0
        shared_tp: Decimal = st.shared_tp

        if self.mode is OhlcResolveMode.STRICT:
            next_p = st.next_p()
            if next_p is not None and dual_touch(side, high=h, low=l, shared_tp=shared_tp, next_p=next_p):
                meta = ambiguity_payload(
                    ts_ms=ts,
                    o=o,
                    h=h,
                    l=l,
                    c=c,
                    shared_tp=shared_tp,
                    next_p=next_p,
                    cycle_id=st.cycle_id,
                    highest_filled=st.highest_filled,
                    p0=p0,
                    side=side,
                )
                ev = MarketEvent(kind=MarketEventKind.AMBIGUOUS_BAR, ts_ms=ts, meta=meta)
                shadow.on_event(ev)
                yield ev
                return

        if side is Side.SELL:
            while st.highest_filled < cfg.max_step and st.p0 is not None:
                next_p, _ = ladder_step(
                    side, st.p0, st.highest_filled + 1, phi=cfg.phi, percentage=cfg.percentage
                )
                if h >= next_p:
                    ev = MarketEvent(
                        kind=MarketEventKind.PROGRESSION_TOUCH,
                        ts_ms=ts,
                        price=next_p,
                        step=st.highest_filled + 1,
                    )
                    shadow.on_event(ev)
                    yield ev
                    st = shadow.state
                else:
                    break
            if st.shared_tp is not None and l <= st.shared_tp:
                ev = MarketEvent(kind=MarketEventKind.TP_TOUCH, ts_ms=ts, price=st.shared_tp)
                shadow.on_event(ev)
                yield ev
                return
        else:
            while st.highest_filled < cfg.max_step and st.p0 is not None:
                next_p, _ = ladder_step(
                    side, st.p0, st.highest_filled + 1, phi=cfg.phi, percentage=cfg.percentage
                )
                if l <= next_p:
                    ev = MarketEvent(
                        kind=MarketEventKind.PROGRESSION_TOUCH,
                        ts_ms=ts,
                        price=next_p,
                        step=st.highest_filled + 1,
                    )
                    shadow.on_event(ev)
                    yield ev
                    st = shadow.state
                else:
                    break
            if st.shared_tp is not None and h >= st.shared_tp:
                ev = MarketEvent(kind=MarketEventKind.TP_TOUCH, ts_ms=ts, price=st.shared_tp)
                shadow.on_event(ev)
                yield ev
                return


def collect_ohlc_events(
    candles: Sequence[Sequence[Any]],
    config: EngineConfig,
    *,
    mode: OhlcResolveMode = OhlcResolveMode.LEGACY,
) -> List[MarketEvent]:
    return HistoricalOhlcFeeder(candles, config, mode=mode).collect()


def replay_ohlc_legacy(
    candles: Sequence[Sequence[Any]],
    *,
    side: Side | str = Side.SELL,
    percentage: Decimal | float | str = Decimal("0.001"),
    max_step: int = 20,
    symbol: str = "",
) -> EngineState:
    """Legacy OHLC path via feeder events applied once to the canonical engine."""
    cfg = EngineConfig(
        side=Side(side),
        percentage=Decimal(str(percentage)),
        max_step=max_step,
        symbol=symbol,
    )
    events = collect_ohlc_events(candles, cfg, mode=OhlcResolveMode.LEGACY)
    return GoldenFiboEngine(cfg).run(events).state
