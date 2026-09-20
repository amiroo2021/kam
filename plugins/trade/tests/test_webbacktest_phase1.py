from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

import importlib
webbacktest_app = importlib.import_module("plugins.trade.webbacktest.app")
from plugins.trade.webbacktest.config import WebBacktestConfig
from plugins.trade.webbacktest.auth import SessionManager


class FakeSession:
    def __init__(self):
        self.calls = []
        self.phase = SimpleNamespace(value="idle")
        self.mode = SimpleNamespace(value="REPLAY_TO_LIVE")
        self.market = "spot"
        self.symbol = "BTCUSDT"
        self.timeframe = "1m"
        self.side = SimpleNamespace(value="BUY")
        self.percentage = 0.001
        self.kline_cache = SimpleNamespace(stats_for=lambda *a, **k: {"path": "/root/kam/GoldenFibo/data/backtest_klines.sqlite", "bars": 0})

    async def start_run(self, **kwargs):
        self.calls.append(kwargs)
        clean = dict(kwargs)
        if clean.get("percentage") is not None:
            clean["percentage"] = str(clean["percentage"])
        if clean.get("side") is not None:
            clean["side"] = getattr(clean["side"], "value", str(clean["side"]))
        if clean.get("mode") is not None:
            clean["mode"] = getattr(clean["mode"], "value", str(clean["mode"]))
        return {"ok": True, "called": clean}

    async def stop(self):
        self.calls.append({"stop": True})

    def snapshot_dict(self):
        return {"v": 1, "type": "state_snapshot", "phase": self.phase.value, "mode": self.mode.value, "symbol": self.symbol, "timeframe": self.timeframe, "market": self.market, "percentage": str(self.percentage)}

    def register_client(self, ws):
        pass

    def unregister_client(self, ws):
        pass


def _make_cfg(tmp_path: Path) -> WebBacktestConfig:
    old_home = os.environ.get("HERMES_HOME")
    old_pw = os.environ.get("WEB_PASSWORD")
    old_hint = os.environ.get("WEB_HINT")
    old_port = os.environ.get("WEBBACKTEST_PORT")
    old_secret = os.environ.get("WEBBACKTEST_SESSION_SECRET")
    os.environ["HERMES_HOME"] = str(tmp_path)
    os.environ["WEB_PASSWORD"] = "test-password"
    os.environ["WEB_HINT"] = "hint"
    os.environ["WEBBACKTEST_PORT"] = "9002"
    os.environ.pop("WEBBACKTEST_SESSION_SECRET", None)
    try:
        return WebBacktestConfig()
    finally:
        if old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_home
        if old_pw is None:
            os.environ.pop("WEB_PASSWORD", None)
        else:
            os.environ["WEB_PASSWORD"] = old_pw
        if old_hint is None:
            os.environ.pop("WEB_HINT", None)
        else:
            os.environ["WEB_HINT"] = old_hint
        if old_port is None:
            os.environ.pop("WEBBACKTEST_PORT", None)
        else:
            os.environ["WEBBACKTEST_PORT"] = old_port
        if old_secret is None:
            os.environ.pop("WEBBACKTEST_SESSION_SECRET", None)
        else:
            os.environ["WEBBACKTEST_SESSION_SECRET"] = old_secret


class WebBacktestPhase1Tests(unittest.TestCase):
    def test_imports_and_default_port(self):
        cfg = _make_cfg(Path(tempfile.mkdtemp()))
        self.assertEqual(cfg.port, 9002)

    def test_port_override(self):
        old = os.environ.get("WEBBACKTEST_PORT")
        os.environ["WEBBACKTEST_PORT"] = "9017"
        try:
            cfg = WebBacktestConfig()
            self.assertEqual(cfg.port, 9017)
        finally:
            if old is None:
                os.environ.pop("WEBBACKTEST_PORT", None)
            else:
                os.environ["WEBBACKTEST_PORT"] = old

    def test_auth_login_and_root_page(self):
        cfg = _make_cfg(Path(tempfile.mkdtemp()))
        fake = FakeSession()
        with mock.patch.object(webbacktest_app, "get_session", return_value=fake):
            app = webbacktest_app.create_app(config=cfg)
            client = TestClient(app)
            r = client.get("/")
            self.assertEqual(r.status_code, 200)
            self.assertIn("webbacktest", r.text)
            self.assertIn("Password", r.text)
            login = client.post("/login", data={"password": "test-password"}, follow_redirects=False)
            self.assertIn(login.status_code, (302, 303))
            r2 = client.get("/")
            self.assertEqual(r2.status_code, 200)
            self.assertIn("webbacktest", r2.text)

    def test_request_validation_and_session_start(self):
        cfg = _make_cfg(Path(tempfile.mkdtemp()))
        fake = FakeSession()
        with mock.patch.object(webbacktest_app, "get_session", return_value=fake):
            app = webbacktest_app.create_app(config=cfg)
            client = TestClient(app)
            client.post("/login", data={"password": "test-password"}, follow_redirects=False)
            session = client.get("/api/session")
            csrf = session.json()["csrf"]
            client.cookies.set("webbacktest_session", session.cookies.get("webbacktest_session"), path="/")
            client.cookies.set("webbacktest_csrf", csrf, path="/")
            r = client.post(
                "/api/session/start",
                json={
                    "mode": "REPLAY_TO_LIVE",
                    "symbol": "BTCUSDT",
                    "timeframe": "1m",
                    "market": "futures",
                    "side": "SELL",
                    "percentage": "0.001",
                    "start_time": "2026-09-15T00:00:00Z",
                },
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(r.status_code, 200)
            self.assertTrue(fake.calls)
            self.assertEqual(fake.calls[-1]["mode"].value, "REPLAY_TO_LIVE")
            self.assertEqual(fake.calls[-1]["symbol"], "BTCUSDT")
            self.assertEqual(fake.calls[-1]["market"], "futures")

    def test_cache_path_is_shared(self):
        from goldenfibo.marketdata.kline_cache import KlineCache
        cache = KlineCache()
        self.assertTrue(str(cache.path).endswith("/data/backtest_klines.sqlite"))

    def test_backtest_ui_has_no_end_date(self):
        html = (Path(webbacktest_app.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
        self.assertIn('webbacktest', html)
        self.assertIn('START BACKTEST', html)
        self.assertNotIn('id="endWrap"', html)
