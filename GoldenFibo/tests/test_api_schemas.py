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
    assert payload["p0"] == "2500"
    assert payload["n"] == 0
    assert Decimal(payload["shared_tp"]) == Decimal("2502.5")
    assert isinstance(payload["levels"], list)
    assert any(lv["role"] == "P0" for lv in payload["levels"])
    assert any(lv["role"] == "tp" for lv in payload["levels"])
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
    assert a["p0"] == b["p0"] == "100"
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
    assert "mergedTitle: \"VWAP\"" in js or 'mergedTitle: "VWAP"' in js
    assert "mergedTitle: \"POC\"" in js or 'mergedTitle: "POC"' in js
    assert "dedupeMarkers" in js
    assert "nearlyEqual" in js
