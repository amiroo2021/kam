from __future__ import annotations

import importlib
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

webbacktest_app = importlib.import_module("plugins.trade.webbacktest.app")


class WebBacktestPhase1Tests(unittest.TestCase):
    def test_imports_and_default_port(self):
        from plugins.trade.webbacktest.config import WebBacktestConfig
        cfg = WebBacktestConfig()
        self.assertEqual(cfg.port, 9002)

    def test_root_is_directly_accessible(self):
        app = webbacktest_app.create_app()
        client = TestClient(app)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("webbacktest", r.text)
        self.assertIn("START BACKTEST", r.text)
        self.assertNotIn("Login", r.text)

    def test_static_assets_are_served(self):
        app = webbacktest_app.create_app()
        client = TestClient(app)
        self.assertEqual(client.get("/static/app.js").status_code, 200)
        self.assertEqual(client.get("/static/style.css").status_code, 200)

    def test_start_session_request_validation(self):
        app = webbacktest_app.create_app()
        client = TestClient(app)
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
        )
        self.assertEqual(r.status_code, 200)

    def test_cache_path_is_shared(self):
        from goldenfibo.marketdata.kline_cache import KlineCache
        cache = KlineCache()
        self.assertTrue(str(cache.path).endswith("/data/backtest_klines.sqlite"))

    def test_backtest_ui_has_no_end_date(self):
        html = (Path(webbacktest_app.STATIC_DIR) / "index.html").read_text(encoding="utf-8")
        self.assertIn('webbacktest', html)
        self.assertIn('START BACKTEST', html)
        self.assertNotIn('id="endWrap"', html)


if __name__ == "__main__":
    unittest.main()
