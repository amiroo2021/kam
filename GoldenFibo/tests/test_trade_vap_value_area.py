"""Trade VAP value area (VAL/VAH) from same profile as L-POC."""

from __future__ import annotations

from goldenfibo.metrics.trade_store import SOURCE_AGGTRADE, STATUS_COMPLETE, TradeMetricStore
from goldenfibo.metrics.trade_vap import AggTrade, trade_vap_profile, trade_value_area


def _t(aid, ts, price, qty=1.0):
    return AggTrade(agg_id=aid, price=price, qty=qty, ts_ms=ts)


def test_value_area_from_same_profile_as_poc():
    trades = [
        _t(1, 1000, 100.00, 10.0),
        _t(2, 1000, 100.01, 1.0),
        _t(3, 1000, 100.02, 1.0),
        _t(4, 1000, 99.90, 1.0),
        _t(5, 1000, 100.50, 0.5),
    ]
    prof = trade_vap_profile(trades, 1000, bin_size=0.01)
    assert prof is not None
    assert prof.poc_price == 100.00
    val, vah = trade_value_area(prof, value_area_pct=0.70)
    assert val <= prof.poc_price <= vah
    # 70% of 13.5 = 9.45; POC alone is 10 >= 70% so VA is single bin
    assert val == 100.00
    assert vah == 100.01


def test_value_area_expands_to_cover_seventy_percent():
    # equal small bins need expansion
    trades = [_t(i, 1000, 100.0 + i * 0.01, 1.0) for i in range(10)]
    # boost middle
    trades.append(_t(100, 1000, 100.05, 5.0))
    prof = trade_vap_profile(trades, 1000, bin_size=0.01)
    assert prof is not None
    val, vah = trade_value_area(prof, value_area_pct=0.70)
    assert val <= prof.poc_price < vah or val <= prof.poc_price <= vah
    # covered volume fraction
    bins = sorted(prof.vols.keys())
    left = bins.index(val)
    right = max(i for i, b in enumerate(bins) if b + prof.bin_size <= vah + 1e-12 or b < vah)
    # simpler: sum vols where bin in [val, vah)
    covered = sum(v for b, v in prof.vols.items() if val - 1e-12 <= b < vah + 1e-12)
    assert covered + 1e-9 >= 0.70 * prof.total_volume


def test_ladder_store_exposes_val_vah_on_complete():
    s = TradeMetricStore(tick_size=0.01)
    s.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    s.apply_rest_backfill(
        [_t(1, 1000, 100.0, 5.0), _t(2, 1100, 100.0, 5.0), _t(3, 1200, 101.0, 1.0)],
        requested_start_ms=1000,
        requested_end_ms=2000,
    )
    m = s.compute(now_ms=2000)
    assert m.ladder_status == STATUS_COMPLETE
    assert m.source == SOURCE_AGGTRADE
    assert m.ladder_val is not None and m.ladder_vah is not None
    assert m.ladder_val <= m.ladder_poc <= m.ladder_vah
    fields = m.as_payload_fields()
    assert fields["ladder_val"] is not None and fields["ladder_vah"] is not None


def test_incomplete_suppresses_val_vah():
    s = TradeMetricStore(tick_size=0.01)
    s.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    s.apply_rest_backfill([_t(1, 50_000, 100.0, 1.0)], requested_start_ms=1000, requested_end_ms=60_000)
    m = s.compute(now_ms=60_000)
    assert m.ladder_status != STATUS_COMPLETE
    fields = m.as_payload_fields()
    assert fields["ladder_val"] is None and fields["ladder_vah"] is None


def test_pre_p0_excluded_from_value_area():
    s = TradeMetricStore(tick_size=0.01)
    s.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    s.apply_rest_backfill(
        [_t(1, 1000, 100.0, 10.0), _t(2, 1100, 100.0, 1.0)],
        requested_start_ms=1000,
        requested_end_ms=2000,
    )
    # attempt inject pre-P0 (should be ignored by ingest)
    s.ingest(_t(0, 500, 50.0, 1000.0))
    m = s.compute(now_ms=2000)
    assert m.ladder_poc == 100.0
    assert m.ladder_val is not None
    assert m.ladder_val >= 99.0


def test_progression_does_not_reset_ladder_val_vah_window():
    s = TradeMetricStore(tick_size=0.01)
    s.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    s.apply_rest_backfill(
        [
            _t(1, 1000, 100.0, 10.0),
            _t(2, 1500, 100.0, 10.0),
            _t(3, 2000, 90.0, 1.0),
            _t(4, 2100, 90.0, 1.0),
        ],
        requested_start_ms=1000,
        requested_end_ms=3000,
    )
    before = s.compute(now_ms=3000)
    s.set_windows(ladder_start_ms=1000, step_start_ms=2000)  # P1
    after = s.compute(now_ms=3000)
    assert after.ladder_val == before.ladder_val
    assert after.ladder_vah == before.ladder_vah
    assert after.ladder_poc == before.ladder_poc
    assert s.ladder_start_ms == 1000
    assert s.step_start_ms == 2000


def test_new_p0_resets_value_area_window():
    s = TradeMetricStore(tick_size=0.01)
    s.set_windows(ladder_start_ms=1000, step_start_ms=1000)
    s.apply_rest_backfill(
        [_t(1, 1000, 50.0, 10.0), _t(2, 3000, 100.0, 10.0)],
        requested_start_ms=1000,
        requested_end_ms=4000,
    )
    s.set_windows(ladder_start_ms=3000, step_start_ms=3000)
    assert s.backfill_complete is False
    m = s.compute(now_ms=4000)
    # until re-backfill: loading → no authoritative VA
    assert m.as_payload_fields()["ladder_val"] is None
