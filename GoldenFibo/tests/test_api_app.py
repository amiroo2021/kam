"""FastAPI app tests with mocked session (no live Binance)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from goldenfibo import EngineConfig, GoldenFiboEngine, MarketEvent, MarketEventKind, Side
import goldenfibo.api.app as app_mod
from goldenfibo.api.app import app
from goldenfibo.api.session import LiveSession
from goldenfibo.marketdata.symbols import canonical_binance_symbol


@pytest.fixture()
def client(monkeypatch):
    """Use a session that does not open Binance WS or REST."""

    class FakeSession(LiveSession):
        async def start(self) -> None:
            self.feed_status = "seeded"
            self._p0_seeded = True
            self.engine = GoldenFiboEngine(
                EngineConfig(side=Side.BUY, percentage=Decimal("0.001"), symbol="BTCUSDT")
            )
            self.engine.on_event(
                MarketEvent(MarketEventKind.SEED_P0, ts_ms=1_700_000_000_000, price=Decimal("2500"))
            )
            self.chart_candles = [
                {"time": 1_700_000_000, "open": 2500, "high": 2501, "low": 2499, "close": 2500.5}
            ]
            self.last_price = "2500.5"
            self.bars = []

        async def stop(self) -> None:
            self.feed_status = "stopped"

        async def reconfigure(self, **kwargs) -> None:
            if kwargs.get("side") is not None:
                self.side = kwargs["side"]
            if kwargs.get("percentage") is not None:
                self.percentage = kwargs["percentage"]
            self.engine = GoldenFiboEngine(
                EngineConfig(side=self.side, percentage=self.percentage, symbol=self.symbol)
            )
            self.engine.on_event(
                MarketEvent(MarketEventKind.SEED_P0, ts_ms=1_700_000_000_000, price=Decimal("2500"))
            )
            self._p0_seeded = True

    fake = FakeSession()
    monkeypatch.setattr(app_mod, "get_session", lambda: fake)

    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["symbol"] == "BTCUSDT"


def test_state_endpoint(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert body["v"] == 1
    assert body["p0"] == "2500.00"
    assert body["type"] == "state_snapshot"
    assert body.get("market") in (None, 'spot', 'futures')


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "backtest-web" in r.text
    assert "lightweight-charts" in r.text


def test_ws_snapshot_on_connect(client):
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["v"] == 1
        assert msg["type"] == "state_snapshot"
        assert msg["p0"] == "2500.00"
        # second snapshot request without reseed
        ws.send_json({"op": "snapshot"})
        msg2 = ws.receive_json()
        assert msg2["p0"] == msg["p0"]
        assert msg2["cycle_id"] == msg["cycle_id"]


def test_state_endpoint_includes_market(client):
    r = client.get('/api/state')
    assert r.status_code == 200
    body = r.json()
    assert body.get('market') in ('spot', 'futures', None)


def test_session_start_accepts_market(client):
    r = client.post('/api/session/start', json={
        'mode': 'LIVE',
        'symbol': 'BTCUSDT',
        'timeframe': '1m',
        'market': 'futures',
        'side': 'BUY',
        'percentage': '0.001',
    })
    assert r.status_code == 200
    body = r.json()
    assert body['symbol'] == 'BTCUSDT'
    assert body.get('market') in ('spot', 'futures', None)



def test_canonical_symbol_resolution_general():
    assert canonical_binance_symbol('BTC') == 'BTCUSDT'
    assert canonical_binance_symbol('ETH') == 'ETHUSDT'
    assert canonical_binance_symbol('BTCUSDT') == 'BTCUSDT'
    assert canonical_binance_symbol('BTC') == 'BTCUSDT'
    assert canonical_binance_symbol('HYPE') == 'HYPEUSDT'
    assert canonical_binance_symbol('HYPEUSDT') == 'HYPEUSDT'


def test_new_run_clears_stale_last_price(client):
    # First run seeds a non-BTC price.
    r1 = client.post('/api/session/start', json={
        'mode': 'REPLAY_TO_LIVE', 'symbol': 'HYPE', 'market': 'futures',
        'timeframe': '1m', 'side': 'SELL', 'percentage': '0.001',
        'start_time': '2026-09-01T00:00:00Z',
    })
    assert r1.status_code == 200
    # Second genuinely new run should not inherit that price.
    r2 = client.post('/api/session/start', json={
        'mode': 'REPLAY_TO_LIVE', 'symbol': 'BTC', 'market': 'spot',
        'timeframe': '1m', 'side': 'SELL', 'percentage': '0.001',
        'start_time': '2026-09-01T00:00:00Z',
    })
    assert r2.status_code == 200
    body = r2.json()
    assert body.get('price') is None or body.get('price') == '—'
