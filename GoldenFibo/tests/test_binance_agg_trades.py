"""Unit tests for Binance aggTrades range fetch (mocked pages)."""

from __future__ import annotations

from goldenfibo.marketdata.binance_agg_trades import fetch_agg_trades_range, parse_agg_trade_row
from goldenfibo.marketdata.binance_agg_trades import BINANCE_SPOT_REST, BINANCE_USDM_REST
from goldenfibo.marketdata.binance_agg_trades import fetch_agg_trades_page


def test_fetch_agg_trades_range_uses_spot_endpoint_by_default(monkeypatch):
    seen = {}
    def fake_page(symbol, **kwargs):
        seen.update(kwargs)
        return []
    out = fetch_agg_trades_range("BTCUSDT", 1, 2, fetch_page=fake_page, pause_s=0.0)
    assert out == []
    assert seen["base_url"] == BINANCE_SPOT_REST
    assert seen["path"] == "/api/v3/aggTrades"


def test_fetch_agg_trades_range_uses_usdm_futures_endpoint(monkeypatch):
    seen = {}
    def fake_page(symbol, **kwargs):
        seen.update(kwargs)
        return []
    out = fetch_agg_trades_range("HYPEUSDT", 1, 2, market="futures", fetch_page=fake_page, pause_s=0.0)
    assert out == []
    assert seen["base_url"] == BINANCE_USDM_REST
    assert seen["path"] == "/fapi/v1/aggTrades"


def test_fetch_agg_trades_range_does_not_silently_fallback_to_spot_for_futures(monkeypatch):
    seen = {}
    def fake_page(symbol, **kwargs):
        seen.update(kwargs)
        return []
    fetch_agg_trades_range("HYPEUSDT", 1, 2, market="futures", fetch_page=fake_page, pause_s=0.0)
    assert seen["base_url"] != BINANCE_SPOT_REST
    assert seen["path"] != "/api/v3/aggTrades"
from goldenfibo.metrics.trade_vap import AggTrade


def test_parse_agg_trade_row_dict_preserves_buyer_is_maker_side():
    t = parse_agg_trade_row({"a": 10, "p": "100.5", "q": "0.25", "T": 1234, "f": 1, "l": 1, "m": True})
    assert t == AggTrade(
        agg_id=10,
        price=100.5,
        qty=0.25,
        ts_ms=1234,
        first_trade_id=1,
        last_trade_id=1,
        id_domain="aggtrade",
        buyer_is_maker=True,
    )


def test_fetch_agg_trades_range_paginates_and_clips_window():
    pages = {
        "start": [
            AggTrade(1, 100.0, 1.0, 1000),
            AggTrade(2, 101.0, 1.0, 1100),
        ],
        3: [
            AggTrade(3, 102.0, 1.0, 1200),
            AggTrade(4, 103.0, 1.0, 2000),  # past end
        ],
    }

    def fake_page(symbol, **kwargs):
        assert symbol.upper() == "BTCUSDT"
        if kwargs.get("from_id") is None:
            return list(pages["start"])
        fid = int(kwargs["from_id"])
        return list(pages.get(fid, []))

    out = fetch_agg_trades_range(
        "BTCUSDT",
        start_ms=1000,
        end_ms=1500,
        fetch_page=fake_page,
        pause_s=0.0,
    )
    assert [t.agg_id for t in out] == [1, 2, 3]
    assert all(1000 <= t.ts_ms <= 1500 for t in out)


def test_fetch_does_not_invent_trades_on_empty():
    def empty_page(symbol, **kwargs):
        return []

    out = fetch_agg_trades_range("BTCUSDT", 1, 9999, fetch_page=empty_page, pause_s=0.0)
    assert out == []
