"""Live ordered price path → engine events (no Binance)."""

from __future__ import annotations

from decimal import Decimal

from goldenfibo import EngineConfig, GoldenFiboEngine, MarketEvent, MarketEventKind, Side, ladder_step
from goldenfibo.live.price_path import apply_price_to_engine, market_events_for_price


def test_buy_progression_before_tp_on_ordered_prices():
    eng = GoldenFiboEngine(EngineConfig(side=Side.BUY, percentage=Decimal("0.001")))
    eng.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1, price=Decimal("2500")))
    p1, _ = ladder_step(Side.BUY, Decimal("2500"), 1)
    # price hits P1 (below) — progression, not TP
    evs = market_events_for_price(eng.state, price=p1, ts_ms=2)
    assert any(e.kind is MarketEventKind.PROGRESSION_TOUCH for e in evs)
    assert not any(e.kind is MarketEventKind.TP_TOUCH for e in evs)
    apply_price_to_engine(eng, p1, 2)
    assert eng.state.highest_filled == 1


def test_buy_tp_after_step0_without_progression():
    eng = GoldenFiboEngine(EngineConfig(side=Side.BUY, percentage=Decimal("0.001")))
    eng.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1, price=Decimal("2500")))
    tp = eng.state.shared_tp
    assert tp == Decimal("2502.5")
    apply_price_to_engine(eng, tp, 3)
    assert eng.state.cycle_id == 2
    assert eng.state.p0 == tp


def test_sell_progression_then_tp_order():
    eng = GoldenFiboEngine(EngineConfig(side=Side.SELL, percentage=Decimal("0.001")))
    eng.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1, price=Decimal("100")))
    p1, _ = ladder_step(Side.SELL, Decimal("100"), 1)
    apply_price_to_engine(eng, p1, 2)
    assert eng.state.highest_filled == 1
    shared = eng.state.shared_tp
    apply_price_to_engine(eng, shared, 3)
    assert eng.state.p0 == shared


def test_gap_fills_multiple_buy_progressions():
    eng = GoldenFiboEngine(EngineConfig(side=Side.BUY, percentage=Decimal("0.001")))
    eng.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1, price=Decimal("2500")))
    p3, _ = ladder_step(Side.BUY, Decimal("2500"), 3)
    # one tick deep through P3
    apply_price_to_engine(eng, p3, 5)
    assert eng.state.highest_filled == 3


def test_sell_gap_multiple_progressions():
    eng = GoldenFiboEngine(EngineConfig(side=Side.SELL, percentage=Decimal("0.001")))
    eng.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1, price=Decimal("100")))
    p3, _ = ladder_step(Side.SELL, Decimal("100"), 3)
    apply_price_to_engine(eng, p3, 5)
    assert eng.state.highest_filled == 3
