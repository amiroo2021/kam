"""Engine path tests: progression, TP, cycle chain, domain events."""

from __future__ import annotations

from decimal import Decimal

from goldenfibo import (
    DomainEventKind,
    EngineConfig,
    GoldenFiboEngine,
    MarketEvent,
    MarketEventKind,
    Side,
    SizingPolicyName,
    ladder_step,
)


def _cfg(**kw):
    base = dict(side=Side.BUY, percentage=Decimal("0.001"))
    base.update(kw)
    return EngineConfig(**base)


def test_seed_opens_step0_and_shared_tp():
    eng = GoldenFiboEngine(_cfg())
    r = eng.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1, price=Decimal("2500")))
    assert eng.state.cycle_id == 1
    assert eng.state.highest_filled == 0
    assert eng.state.p0 == Decimal("2500")
    assert eng.state.shared_tp == Decimal("2502.5")
    kinds = [e.kind for e in r.events]
    assert DomainEventKind.CYCLE_STARTED in kinds
    assert DomainEventKind.STEP_FILLED in kinds
    assert DomainEventKind.P0_RESET in kinds


def test_progression_updates_shared_tp_to_previous_p():
    eng = GoldenFiboEngine(_cfg())
    eng.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1, price=Decimal("2500")))
    eng.on_event(MarketEvent(MarketEventKind.PROGRESSION_TOUCH, ts_ms=2, step=1))
    eng.on_event(MarketEvent(MarketEventKind.PROGRESSION_TOUCH, ts_ms=3, step=2))
    p1, _ = ladder_step(Side.BUY, Decimal("2500"), 1)
    p2, tp2 = ladder_step(Side.BUY, Decimal("2500"), 2)
    assert eng.state.highest_filled == 2
    assert eng.state.current_p() == p2
    assert eng.state.shared_tp == tp2 == p1
    assert eng.state.next_p() == ladder_step(Side.BUY, Decimal("2500"), 3)[0]


def test_tp_hit_closes_and_chains_p0_to_exact_tp():
    eng = GoldenFiboEngine(_cfg(side=Side.SELL, percentage=Decimal("0.001")))
    eng.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1, price=Decimal("100")))
    tp0 = eng.state.shared_tp
    assert tp0 == Decimal("99.9")
    r = eng.on_event(MarketEvent(MarketEventKind.TP_TOUCH, ts_ms=2))
    kinds = [e.kind for e in r.events]
    assert DomainEventKind.TP_HIT in kinds
    assert DomainEventKind.CYCLE_CLOSED in kinds
    assert DomainEventKind.CYCLE_STARTED in kinds
    assert eng.state.cycle_id == 2
    assert eng.state.p0 == tp0
    assert eng.state.highest_filled == 0
    assert eng.state.initial_p0 == Decimal("100")
    assert len(eng.state.closed) == 1
    assert eng.state.closed[0].exit == tp0


def test_multiple_consecutive_cycles():
    eng = GoldenFiboEngine(_cfg(side=Side.SELL))
    eng.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1, price=Decimal("100")))
    for i in range(3):
        eng.on_event(MarketEvent(MarketEventKind.TP_TOUCH, ts_ms=10 + i))
    assert eng.state.cycle_id == 4
    assert len(eng.state.closed) == 3
    # each new p0 equals previous exit
    assert eng.state.closed[0].exit == Decimal("99.9")
    assert eng.state.p0 == eng.state.closed[-1].exit


def test_deep_ladder_fill_to_step_5():
    eng = GoldenFiboEngine(_cfg(side=Side.BUY))
    eng.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1, price=Decimal("2500")))
    for n in range(1, 6):
        eng.on_event(MarketEvent(MarketEventKind.PROGRESSION_TOUCH, ts_ms=n + 1, step=n))
    assert eng.state.highest_filled == 5
    assert len(eng.state.legs) == 6
    assert eng.state.legs[-1].step == 5


def test_linear_qty_on_legs():
    eng = GoldenFiboEngine(_cfg(sizing=SizingPolicyName.LINEAR_RESEARCH))
    eng.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1, price=Decimal("100")))
    eng.on_event(MarketEvent(MarketEventKind.PROGRESSION_TOUCH, ts_ms=2, step=1))
    assert eng.state.legs[0].qty == Decimal("0.001")
    assert eng.state.legs[1].qty == Decimal("0.0011")


def test_exponential_qty_on_legs():
    eng = GoldenFiboEngine(
        _cfg(sizing=SizingPolicyName.EXPONENTIAL_LIVE, step0_volume=Decimal("0.01"))
    )
    eng.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1, price=Decimal("100")))
    eng.on_event(MarketEvent(MarketEventKind.PROGRESSION_TOUCH, ts_ms=2, step=1))
    eng.on_event(MarketEvent(MarketEventKind.PROGRESSION_TOUCH, ts_ms=3, step=2))
    assert eng.state.legs[0].qty == Decimal("0.01")
    assert eng.state.legs[1].qty == Decimal("0.01")
    assert eng.state.legs[2].qty == Decimal("0.02")


def test_snapshot_exposes_chart_fields_without_ui_math():
    eng = GoldenFiboEngine(_cfg())
    eng.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1, price=Decimal("2500")))
    eng.on_event(MarketEvent(MarketEventKind.PROGRESSION_TOUCH, ts_ms=2, step=1))
    snap = eng.snapshot()
    assert snap.n == 1
    assert snap.current_p == str(ladder_step(Side.BUY, Decimal("2500"), 1)[0])
    assert snap.shared_tp == "2500"
    assert snap.next_p == str(ladder_step(Side.BUY, Decimal("2500"), 2)[0])
    assert snap.further_p == str(ladder_step(Side.BUY, Decimal("2500"), 3)[0])
    assert snap.ladder_vwap is None  # phase 1 extension point
