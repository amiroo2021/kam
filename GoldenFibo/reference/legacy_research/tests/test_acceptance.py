import math

from golden_fibo.broker import Fill, OrderSide, PaperBroker
from golden_fibo.constants import BGF, SGF, Side
from golden_fibo.ladder import PHI, ladder_step
from golden_fibo.side_book import SideBook


def test_ladder_examples_match_acceptance():
    p0 = 2500
    buy0 = ladder_step(Side.BUY, p0, 0)
    buy1 = ladder_step(Side.BUY, p0, 1)
    buy2 = ladder_step(Side.BUY, p0, 2)
    sell0 = ladder_step(Side.SELL, p0, 0)
    sell1 = ladder_step(Side.SELL, p0, 1)

    assert buy0 == pytest_tuple((2500, 2502.5), abs=0.00001)
    assert buy1 == pytest_tuple((2495.955, 2500), abs=0.00001)
    assert buy2[0] == pytest_approx(2489.41019, abs=0.00001)
    assert buy2[1] == pytest_approx(2495.955, abs=0.00001)
    assert sell0 == pytest_tuple((2500, 2497.5), abs=0.00001)
    assert sell1 == pytest_tuple((2504.045, 2500), abs=0.00001)

    p1, tp1 = buy1
    p2, _ = buy2
    # Direction-aware PHI magnitude: valid for both BUY and SELL.
    # BUY has P[n] - TP[n] < 0; SELL has P[n] - TP[n] > 0.
    assert abs((p2 - p1) / (p1 - tp1)) == pytest_approx(PHI, rel=1e-12)

    s1, stp1 = sell1
    s2, _ = ladder_step(Side.SELL, p0, 2)
    assert abs((s2 - s1) / (s1 - stp1)) == pytest_approx(PHI, rel=1e-12)


def test_after_step2_fill_all_open_ticket_tps_equal_p1():
    broker = PaperBroker()
    broker.set_quote("BTC", bid=2499.99, ask=2500.00)
    book = SideBook("BTC", BGF, broker, min_stop_distance=0, tick=0.00001)
    book.open_step0()

    p1, _ = book.p_tp(1)
    p2, _ = book.p_tp(2)
    book.on_fill(Fill("manual1", "BTC", OrderSide.BUY, book.qty(1), p1, "bgf:step1", "bgf", 1, 1.0))
    book.on_fill(Fill("manual2", "BTC", OrderSide.BUY, book.qty(2), p2, "bgf:step2", "bgf", 2, 2.0))

    legs = broker.legs("BTC", bot_tag="bgf", cycle_id=book.state.cycle_id)
    assert len(legs) == 3
    assert {leg.step for leg in legs} == {0, 1, 2}
    assert all(leg.tp == pytest_approx(round(p1, 5)) for leg in legs)


def test_shared_tp_hit_closes_side_deletes_pendings_and_reopens_step0():
    broker = PaperBroker()
    broker.set_quote("BTC", bid=2499.99, ask=2500.00)
    book = SideBook("BTC", BGF, broker, min_stop_distance=0, tick=0.00001)
    book.open_step0()
    first_cycle = book.state.cycle_id
    tp = book.state.last_sync_tp
    assert tp is not None
    assert broker.legs("BTC", bot_tag="bgf")
    assert broker.pending_orders("BTC", bot_tag="bgf")

    broker.set_quote("BTC", bid=tp, ask=tp + 0.01)
    close = book.maybe_take_profit()

    assert close is not None
    assert close.closed_legs == 1
    assert book.state.cycle_id == first_cycle + 1
    assert book.state.highest_filled == 0
    assert broker.legs("BTC", bot_tag="bgf", cycle_id=first_cycle) == []
    assert broker.pending_orders("BTC", bot_tag="bgf")  # new cycle's next pending exists
    assert all(o.tag.startswith("bgf:step") for o in broker.pending_orders("BTC", bot_tag="bgf"))


def test_paper_gap_through_p1_to_p4_synthetic_backfills_to_step4():
    broker = PaperBroker()
    broker.set_quote("BTC", bid=2499.99, ask=2500.00)
    book = SideBook("BTC", BGF, broker, min_stop_distance=0, tick=0.00001)
    book.open_step0()
    p4, _ = book.p_tp(4)
    p5, _ = book.p_tp(5)

    p4_touch = book.normalize_price(p4)
    broker.set_quote("BTC", bid=p4_touch - 0.01, ask=p4_touch)  # ask crosses P1..P4 but not P5
    made = book.backfill_crossed_steps()

    assert len(made) == 4
    assert book.state.highest_filled == 4
    assert book.status_digest().startswith("BTC BUY step=4")
    assert p5 < p4
    assert all(leg.step in {0, 1, 2, 3, 4} for leg in broker.legs("BTC", bot_tag="bgf"))


def test_buy_book_ignores_sell_fills_and_sell_book_ignores_buy_fills():
    broker = PaperBroker()
    broker.set_quote("BTC", bid=2499.99, ask=2500.00)
    buy = SideBook("BTC", BGF, broker, min_stop_distance=0, tick=0.00001)
    sell = SideBook("BTC", SGF, broker, min_stop_distance=0, tick=0.00001)
    buy.open_step0()
    sell.open_step0()

    buy_hf = buy.state.highest_filled
    sell_hf = sell.state.highest_filled
    p1_sell, _ = sell.p_tp(1)
    p1_buy, _ = buy.p_tp(1)

    assert buy.on_fill(Fill("s1", "BTC", OrderSide.SELL, sell.qty(1), p1_sell, "sgf:step1", "sgf", 1, 1.0)) is None
    assert sell.on_fill(Fill("b1", "BTC", OrderSide.BUY, buy.qty(1), p1_buy, "bgf:step1", "bgf", 1, 1.0)) is None
    assert buy.state.highest_filled == buy_hf
    assert sell.state.highest_filled == sell_hf


def pytest_approx(*args, **kwargs):
    import pytest
    return pytest.approx(*args, **kwargs)


def pytest_tuple(values, **kwargs):
    import pytest
    return tuple(pytest.approx(v, **kwargs) for v in values)
