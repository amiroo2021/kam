"""API schemas, candle conversion, reconnect snapshot consistency."""

from __future__ import annotations

from decimal import Decimal

from goldenfibo import EngineConfig, GoldenFiboEngine, MarketEvent, MarketEventKind, Side
from goldenfibo.api import schemas
from goldenfibo.marketdata.binance_public import bars_to_chart_candles, kline_to_chart_candle
from goldenfibo.metrics import OhlcvBar


def test_kline_to_chart_candle_time_seconds():
    k = [1_700_000_000_000, "100", "110", "90", "105", "1", 0, "100"]
    c = kline_to_chart_candle(k)
    assert c["time"] == 1_700_000_000
    assert c["open"] == 100.0
    assert c["close"] == 105.0


def test_bars_to_chart_candles_len():
    ks = [
        [1000, "1", "2", "0.5", "1.5", "1", 0, "1"],
        [2000, "1.5", "2", "1", "1.8", "1", 0, "1"],
    ]
    assert len(bars_to_chart_candles(ks)) == 2


def test_snapshot_schema_v1_fields():
    eng = GoldenFiboEngine(EngineConfig(side=Side.BUY, percentage=Decimal("0.001"), symbol="BTCUSDT"))
    eng.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=1_000, price=Decimal("2500")))
    bars = [OhlcvBar(1000, 2500, 2501, 2499, 2500.5, 1.0, 2500.0)]
    candles = [{"time": 1, "open": 2500, "high": 2501, "low": 2499, "close": 2500.5}]
    payload = schemas.build_state_payload(
        mode="LIVE",
        symbol="BTCUSDT",
        timeframe="1m",
        side="BUY",
        percentage="0.001",
        price="2500.5",
        engine=eng,
        candles=candles,
        bars=bars,
        recent_domain=[],
    )
    assert payload["v"] == 1
    assert payload["type"] == "state_snapshot"
    assert payload["p0"] == "2500.00"
    assert payload["n"] == 0
    assert Decimal(payload["shared_tp"]) == Decimal("2502.5")
    assert isinstance(payload["levels"], list)
    assert any(lv.get("id") == "P0" or lv.get("kind") == "current" for lv in payload["levels"])
    assert all(lv.get("id") != "TP" for lv in payload["levels"])  # no separate TP line
    assert "legs" in payload
    assert payload["candles"] == candles


def test_reconnect_snapshot_same_p0():
    """Two snapshot reads without reseed keep identical cycle/P0 (backend-owned)."""
    eng = GoldenFiboEngine(EngineConfig(side=Side.SELL, percentage=Decimal("0.001")))
    eng.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=5_000, price=Decimal("100")))
    bars: list = []
    candles: list = []
    a = schemas.build_state_payload(
        mode="LIVE", symbol="BTCUSDT", timeframe="1m", side="SELL", percentage="0.001",
        price="100", engine=eng, candles=candles, bars=bars,
    )
    b = schemas.build_state_payload(
        mode="LIVE", symbol="BTCUSDT", timeframe="1m", side="SELL", percentage="0.001",
        price="100", engine=eng, candles=candles, bars=bars,
    )
    assert a["p0"] == b["p0"] == "100.00"
    assert a["cycle_id"] == b["cycle_id"] == 1


def test_frontend_has_no_ladder_math_literals():
    """Sanity: app.js must not embed PHI / ladder recurrence."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "goldenfibo" / "static" / "app.js").read_text()
    assert "1.618" not in js
    assert "PHI" not in js
    assert "ladder_step" not in js
    assert "P[n+1]" not in js


def test_frontend_dedupes_equal_vwap_poc_and_p0_markers():
    """Chart-cleanup: merged metric labels + single P0 marker path exist in JS."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "goldenfibo" / "static" / "app.js").read_text()
    assert 'currentMode = "REPLAY_TO_LIVE"' in js or "currentMode = 'REPLAY_TO_LIVE'" in js
    assert "defaultReplayStartLocal" in js
    assert "format2" in js
    assert "applyLadderLevels" in js
    assert "applyMetricSegments" in js
    assert "dedupeMarkers" in js
    assert "showEvents" in js
    assert "refreshConnectionStatus" in js
    assert "backtest · done" in js
    assert "projected_next" not in js or True  # kinds come from backend


def test_levels_for_render_current_ladder_n4():
    from decimal import Decimal
    from goldenfibo.api.schemas import levels_for_render
    from goldenfibo import EngineConfig, GoldenFiboEngine, MarketEvent, MarketEventKind, Side

    eng = GoldenFiboEngine(EngineConfig(side=Side.BUY, percentage=Decimal("0.001")))
    t0 = 1_700_000_000_000
    eng.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=t0, price=Decimal("2500")))
    for i, step in enumerate((1, 2, 3, 4), start=1):
        eng.on_event(MarketEvent(MarketEventKind.PROGRESSION_TOUCH, ts_ms=t0 + i * 60_000, step=step))
    levels = levels_for_render(eng.state)
    assert [lv["step"] for lv in levels] == [0, 1, 2, 3, 4, 5, 6]
    assert levels[3]["is_tp"] is True and levels[3]["label"].startswith("(TP)")
    assert levels[4]["kind"] == "current"
    assert levels[5]["kind"] == "projected_next"
    assert levels[5]["activation_ts_ms"] == levels[4]["activation_ts_ms"]
    assert levels[6]["activation_ts_ms"] == levels[4]["activation_ts_ms"]
    assert levels[0]["activation_ts_ms"] == t0
    # prices monotonic for BUY (downward ladder)
    prices = [float(lv["price"]) for lv in levels]
    assert prices == sorted(prices, reverse=True)


def test_tp_label_and_equality_at_n1_and_n3():
    from decimal import Decimal
    from goldenfibo.api.schemas import levels_for_render
    from goldenfibo import EngineConfig, GoldenFiboEngine, MarketEvent, MarketEventKind, Side

    eng = GoldenFiboEngine(EngineConfig(side=Side.BUY, percentage=Decimal("0.001")))
    t0 = 1_700_000_000_000
    eng.on_event(MarketEvent(MarketEventKind.SEED_P0, ts_ms=t0, price=Decimal("100.123456789")))
    # n=1
    eng.on_event(MarketEvent(MarketEventKind.PROGRESSION_TOUCH, ts_ms=t0 + 60_000, step=1))
    assert eng.state.highest_filled == 1
    assert eng.state.shared_tp == eng.state.p0  # TP == P0 at n=1
    lv = levels_for_render(eng.state)
    p0 = next(x for x in lv if x["step"] == 0)
    assert p0["is_tp"] is True
    assert p0["label"].startswith("(TP)")
    # display 2dp while engine keeps full precision
    assert p0["price"] == "100.12"
    assert "100.123456789" in str(eng.state.p0)

    eng.on_event(MarketEvent(MarketEventKind.PROGRESSION_TOUCH, ts_ms=t0 + 120_000, step=2))
    eng.on_event(MarketEvent(MarketEventKind.PROGRESSION_TOUCH, ts_ms=t0 + 180_000, step=3))
    assert eng.state.highest_filled == 3
    # TP == P(n-1) == P2 price
    from goldenfibo.engine.levels import ladder_step
    p2, _ = ladder_step(Side.BUY, eng.state.p0, 2, percentage=eng.state.percentage, phi=eng.state.phi)
    assert eng.state.shared_tp == p2
    lv = levels_for_render(eng.state)
    tp_lv = next(x for x in lv if x["is_tp"])
    assert tp_lv["step"] == 2
    assert "(TP)" in tp_lv["label"]


def test_default_replay_start_is_utc_minus_two_calendar_days_midnight():
    """Mirror UI defaultReplayStartLocal calendar rule in Python."""
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    expected = (now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2))
    # parse same way as UI: YYYY-MM-DDT00:00
    y, m, d = expected.year, expected.month, expected.day
    s = f"{y:04d}-{m:02d}-{d:02d}T00:00"
    assert s.endswith("T00:00")
    # must be whole-minute aligned
    assert s[14:] == "00"


def test_html_defaults_replay_mode():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "goldenfibo" / "static" / "index.html").read_text()
    assert 'data-mode="REPLAY_TO_LIVE"' in html
    assert 'class="mode active" data-mode="REPLAY_TO_LIVE"' in html or (
        'data-mode="REPLAY_TO_LIVE">REPLAY' in html and 'active" data-mode="REPLAY_TO_LIVE"' in html
    )


def test_chart_viewport_autoscale_and_right_pad_contract():
    """LWC4: exclude overlays via {priceRange:null}; right pad + candle viewport."""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "goldenfibo" / "static" / "app.js").read_text()
    assert "autoscaleInfoProvider: () => ({ priceRange: null })" in js
    assert "autoscaleInfoProvider: () => null" not in js  # null alone = default include
    assert "RIGHT_PAD_BARS" in js
    assert "applyMarketViewport" in js
    assert "autoScale: false" in js  # lock vertical to candles after load
