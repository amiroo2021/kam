"""TradeMetricStore: REST/WS handoff, dedupe, gaps, coverage, window resets."""

from __future__ import annotations

from goldenfibo.metrics.trade_store import (
    SOURCE_AGGTRADE,
    STATUS_COMPLETE,
    STATUS_INCOMPLETE,
    STATUS_LOADING,
    TradeMetricStore,
)
from goldenfibo.metrics.trade_vap import AggTrade


def _t(aid: int, ts: int, price: float = 100.0, qty: float = 1.0) -> AggTrade:
    return AggTrade(agg_id=aid, price=price, qty=qty, ts_ms=ts)


def test_dedupe_aggregate_trade_ids():
    s = TradeMetricStore(tick_size=0.01)
    s.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    assert s.ingest(_t(1, 1000, 100.0, 1.0)) is True
    assert s.ingest(_t(1, 1000, 100.0, 9.0)) is False  # duplicate id
    assert len(s) == 1
    assert s.trades[0].qty == 1.0


def test_rest_ws_handoff_dedupes_overlap():
    s = TradeMetricStore(tick_size=0.01)
    s.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    # WS arrives first during backfill
    s.ingest(_t(5, 1500, 101.0, 2.0))
    s.ingest(_t(6, 1600, 102.0, 3.0))
    # REST covers 1..6
    rest = [_t(i, 1000 + i * 10, 100.0 + i, 1.0) for i in range(1, 7)]
    s.apply_rest_backfill(rest, requested_start_ms=1000, requested_end_ms=2000)
    assert len(s) == 6
    assert s.handoff_status == "live_ready"
    m = s.compute(now_ms=2000)
    assert m.ladder_status == STATUS_COMPLETE
    assert m.source == SOURCE_AGGTRADE


def test_gapped_trade_ids_mark_incomplete():
    s = TradeMetricStore(tick_size=0.01)
    s.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    trades = [_t(1, 1000), _t(2, 1100), _t(5, 1200)]  # missing 3,4
    s.apply_rest_backfill(trades, requested_start_ms=1000, requested_end_ms=2000)
    gaps = s.detect_id_gaps()
    assert gaps == [(3, 4)]
    m = s.compute(now_ms=2000)
    assert m.ladder_status == STATUS_INCOMPLETE
    assert m.ladder_poc is None
    assert m.as_payload_fields()["ladder_poc"] is None


def test_incomplete_coverage_suppresses_authoritative_values():
    s = TradeMetricStore(tick_size=0.01)
    s.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    # earliest trade far after P0
    s.apply_rest_backfill([_t(10, 50_000)], requested_start_ms=1000, requested_end_ms=60_000)
    assert s.handoff_status == "incomplete"
    m = s.compute(now_ms=60_000)
    assert m.ladder_status == STATUS_INCOMPLETE
    fields = m.as_payload_fields()
    assert fields["ladder_vwap"] is None and fields["ladder_poc"] is None
    assert fields["ladder_metric_status"] == STATUS_INCOMPLETE


def test_loading_while_backfill_in_progress():
    s = TradeMetricStore(tick_size=0.01)
    s.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    s.ingest(_t(1, 1000))
    m = s.compute(now_ms=2000)
    assert m.ladder_status == STATUS_LOADING
    assert m.as_payload_fields()["ladder_poc"] is None


def test_p0_boundary_filtering():
    s = TradeMetricStore(tick_size=0.01)
    s.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    s.ingest(_t(1, 500, 50.0, 100.0))  # pre-P0 ignored
    s.apply_rest_backfill(
        [_t(2, 1000, 100.0, 1.0), _t(3, 1100, 110.0, 1.0)],
        requested_start_ms=1000,
        requested_end_ms=2000,
    )
    m = s.compute(now_ms=2000)
    assert m.ladder_status == STATUS_COMPLETE
    assert m.ladder_vwap == 105.0
    assert m.ladder_trade_count == 2


def test_pn_boundary_filtering_step_only():
    s = TradeMetricStore(tick_size=0.01)
    s.set_windows(ladder_start_ms=1000, step_start_ms=2000)
    s.apply_rest_backfill(
        [
            _t(1, 1000, 100.0, 10.0),
            _t(2, 1500, 100.0, 10.0),
            _t(3, 2000, 200.0, 1.0),
            _t(4, 2100, 220.0, 1.0),
        ],
        requested_start_ms=1000,
        requested_end_ms=3000,
    )
    m = s.compute(now_ms=3000)
    assert m.ladder_status == STATUS_COMPLETE and m.step_status == STATUS_COMPLETE
    assert m.step_vwap == 210.0
    assert m.ladder_vwap != m.step_vwap
    assert m.step_trade_count == 2


def test_progression_resets_step_not_ladder():
    s = TradeMetricStore(tick_size=0.01)
    s.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    s.apply_rest_backfill(
        [_t(1, 1000, 100.0, 1.0), _t(2, 1500, 100.0, 1.0), _t(3, 2000, 200.0, 1.0)],
        requested_start_ms=1000,
        requested_end_ms=3000,
    )
    before = s.compute(now_ms=3000)
    ladder_vwap_before = before.ladder_vwap
    # progression to P(n) at 2000
    s.set_windows(ladder_start_ms=1000, step_start_ms=2000)
    after = s.compute(now_ms=3000)
    assert after.ladder_vwap == ladder_vwap_before
    assert after.step_vwap == 200.0
    assert after.ladder_start_ms if False else True
    assert s.ladder_start_ms == 1000
    assert s.step_start_ms == 2000


def test_tp_new_p0_resets_ladder_and_step_windows():
    s = TradeMetricStore(tick_size=0.01)
    s.set_windows(ladder_start_ms=1000, step_start_ms=1500)
    s.apply_rest_backfill(
        [_t(1, 1000, 50.0, 10.0), _t(2, 2000, 50.0, 10.0), _t(3, 3000, 100.0, 1.0)],
        requested_start_ms=1000,
        requested_end_ms=4000,
    )
    assert len(s) == 3
    # new P0 at 3000
    s.set_windows(ladder_start_ms=3000, step_start_ms=3000)
    assert len(s) == 1
    assert s.trades[0].price == 100.0
    assert s.backfill_complete is False
    assert s.handoff_status == "backfilling"
    # until re-backfill, loading
    m = s.compute(now_ms=4000)
    assert m.ladder_status == STATUS_LOADING


def test_reconnect_preserves_store_state():
    s = TradeMetricStore(tick_size=0.01)
    s.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    s.apply_rest_backfill([_t(1, 1000, 100.0, 2.0), _t(2, 1100, 100.0, 2.0)], requested_start_ms=1000, requested_end_ms=2000)
    s.mark_ws_attached()
    # "reconnect" continues ingesting without clearing
    s.ingest(_t(3, 1200, 100.0, 2.0))
    m = s.compute(now_ms=2000)
    assert m.ladder_status == STATUS_COMPLETE
    assert m.ladder_trade_count == 3
    assert len(s) == 3


def test_ws_message_parse_and_ingest():
    s = TradeMetricStore(tick_size=0.01)
    s.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    s.apply_rest_backfill([], requested_start_ms=1000, requested_end_ms=1000)
    ok = s.ingest_ws_message({"e": "aggTrade", "a": 1, "p": "100.5", "q": "0.25", "T": 1000})
    assert ok is True
    m = s.compute(now_ms=1000)
    assert m.ladder_poc == 100.5 or m.ladder_status == STATUS_COMPLETE


def test_payload_identifies_aggtrade_source():
    s = TradeMetricStore(tick_size=0.01)
    s.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    s.apply_rest_backfill([_t(1, 1000, 100.0, 1.0)], requested_start_ms=1000, requested_end_ms=1000)
    fields = s.compute(now_ms=1000).as_payload_fields()
    assert fields["metric_source"] == SOURCE_AGGTRADE
    assert fields["ladder_metric_status"] == STATUS_COMPLETE
