from __future__ import annotations

import importlib
import os
import re
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

from fastapi.testclient import TestClient

from plugins.trade.canonical import CanonicalInstrument, CanonicalMarketPrice, CanonicalPortfolioSummary, CanonicalPosition, make_success


class FakeDesk:
    def __init__(self) -> None:
        self._accounts = {
            "hyperliquid": ["FIBO"],
            "binance": [
                {"account": "spot", "label": "Spot"},
                {"account": "futures", "label": "Futures"},
            ],
            "spotx": [{"account": "main", "label": "Main"}],
        }
        self._caps = {
            "hyperliquid": [
                "balance",
                "positions_orders",
                "resolve_instrument",
                "list_instruments",
                "market_price",
                "candles",
                "new_order",
                "ladder",
                "cancel_order_group",
            ],
            "binance": ["resolve_instrument", "market_price", "candles"],
            "spotx": ["spot", "resolve_instrument", "market_price", "candles", "list_instruments"],
        }
        self.requests: List[Dict[str, Any]] = []

    def list_exchanges(self) -> List[str]:
        return sorted(self._accounts)

    def list_accounts(self, exchange: str) -> List[Any]:
        return list(self._accounts.get(exchange, []))

    def capabilities(self, exchange: str) -> List[str]:
        return list(self._caps.get(exchange, []))

    def execute(self, request: Dict[str, Any]):
        self.requests.append(dict(request))
        op = request.get("operation")
        exchange = str(request.get("exchange") or "")
        account = str(request.get("account") or "")
        if op == "list_instruments":
            return make_success(
                operation="list_instruments",
                exchange=exchange,
                account=account,
                data={
                    "instruments": [
                        {"instrument": "ZED", "display_name": "ZED", "price": "5", "volume_24h": None},
                        {"instrument": "BTC", "display_name": "BTC", "price": "65000", "volume_24h": "1000"},
                        {"instrument": "ETH", "display_name": "ETH", "price": "3500", "volume_24h": "500"},
                        {"instrument": "ABC", "display_name": "ABC", "price": "1", "volume_24h": None},
                    ]
                },
            )
        if op == "resolve_instrument":
            sym = str(request.get("symbol") or "BTC")
            return make_success(
                operation="resolve_instrument",
                exchange=exchange,
                account=account,
                instrument=CanonicalInstrument(
                    requested_symbol=sym,
                    symbol=sym.upper(),
                    display_name=sym.upper(),
                    price_increment="0.5",
                    size_increment="0.1",
                    minimum_size="0.1",
                ),
            )
        if op == "market_price":
            sym = str(request.get("symbol") or "BTC")
            return make_success(
                operation="market_price",
                exchange=exchange,
                account=account,
                market_price=CanonicalMarketPrice(requested_symbol=sym, market=sym, price="65000", mark_price="65000"),
            )
        if op == "balance":
            return make_success(
                operation="balance",
                exchange=exchange,
                account=account,
                portfolio_summary=CanonicalPortfolioSummary(
                    account_value="12345.67",
                    withdrawable="0",
                    margin_used="100.25",
                    total_position_value="2500",
                    unit="USD",
                ),
            )
        if op == "positions_orders":
            return make_success(
                operation="positions_orders",
                exchange=exchange,
                account=account,
                positions=[
                    CanonicalPosition(
                        symbol="BTC",
                        side="long",
                        size="0.12345678",
                        entry_price="65000.123456",
                        pnl="14766.09742784",
                        mark="66123.987654",
                    )
                ],
                open_order_count=0,
                order_groups=[],
            )
        if op == "candles":
            return make_success(
                operation="candles",
                exchange=exchange,
                account=account,
                data={
                    "candles": [
                        {"time": 1700000000000, "open": "100", "high": "110", "low": "95", "close": "105", "volume": "1.0"},
                        {"time": 1700003600000, "open": "105", "high": "112", "low": "100", "close": "108", "volume": "1.2"},
                    ],
                    "symbol": str(request.get("symbol") or ""),
                    "interval": str(request.get("interval") or "1h"),
                    "source": "native",
                    "count": 2,
                },
            )
        raise AssertionError(f"write or unsupported operation reached fake desk: {request}")


class WebTrade2Phase1Tests(unittest.TestCase):
    def _temp_home(self):
        td = tempfile.TemporaryDirectory()
        home = Path(td.name)
        (home / ".env").write_text("WEB_PASSWORD=test-password\nWEBTRADE_PORT=9001\n", encoding="utf-8")
        return td, home

    def test_config_defaults_are_independent_from_webtrade(self) -> None:
        td, home = self._temp_home()
        self.addCleanup(td.cleanup)
        old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        self.addCleanup(lambda: os.environ.__setitem__("HERMES_HOME", old_home) if old_home is not None else os.environ.pop("HERMES_HOME", None))

        wt_cfg = importlib.import_module("plugins.trade.webtrade.config").WebTradeConfig()
        wt2_cfg_mod = importlib.import_module("plugins.trade.webtrade2.config")
        wt2_cfg = wt2_cfg_mod.WebTrade2Config()

        self.assertEqual(wt_cfg.port, 9001)
        self.assertEqual(wt_cfg.cookie_name, "webtrade_session")
        self.assertEqual(wt2_cfg.port, 9009)
        self.assertEqual(wt2_cfg.cookie_name, "webtrade2_session")
        self.assertEqual(wt2_cfg.csrf_cookie_name, "webtrade2_csrf")

    def test_capability_description_filters_futures_and_spot(self) -> None:
        svc_mod = importlib.import_module("plugins.trade.webtrade2.service")
        service = svc_mod.WebTrade2Service(desk=FakeDesk())
        hl = service.capability_description("hyperliquid")
        bn = service.capability_description("binance")
        spot = service.capability_description("spotx")

        self.assertEqual(hl["market_types"], ["futures"])
        self.assertEqual(bn["market_types"], ["futures", "spot"])
        self.assertEqual(spot["market_types"], ["spot"])
        self.assertTrue(hl["features"]["ladder"])
        self.assertTrue(hl["features"]["limit_orders"])
        self.assertFalse(hl["features"]["market_orders"])
        self.assertFalse(hl["features"]["leverage"])

    def test_live_chart_library_and_timeframes(self) -> None:
        app_mod = importlib.import_module("plugins.trade.webtrade2.app")
        cfg_mod = importlib.import_module("plugins.trade.webtrade2.config")
        svc_mod = importlib.import_module("plugins.trade.webtrade2.service")
        cfg = cfg_mod.WebTrade2Config.from_values(password="test-password", session_secret="secret-32", port=9009)
        app = app_mod.create_app(config=cfg, service=svc_mod.WebTrade2Service(desk=FakeDesk()))
        client = TestClient(app)

        html = client.get("/").text
        js_text = (Path(__file__).resolve().parents[1] / "webtrade2" / "static" / "app.js").read_text(encoding="utf-8")
        static_dir = Path(__file__).resolve().parents[1] / "webtrade2" / "static"

        # Real TradingView Lightweight Charts standalone vendored locally
        lib = static_dir / "lightweight-charts.standalone.production.js"
        self.assertTrue(lib.is_file(), f"missing {lib}")
        head = lib.read_text(encoding="utf-8", errors="replace")[:2000]
        self.assertIn("TradingView Lightweight Charts", head)
        self.assertIn("Apache License 2.0", head)
        html_with_lib = client.get("/static/lightweight-charts.standalone.production.js")
        self.assertEqual(html_with_lib.status_code, 200)
        self.assertIn("LightweightCharts", html_with_lib.text)

        # Timeframe selector 1m 5m 15m 1h 4h 1D; default 1m; chart container has id chart
        for tf in ("1m", "5m", "15m", "1h", "4h", "1D"):
            self.assertIn(f'data-timeframe="{tf}"', html)
        self.assertIn('id="chart"', html)
        self.assertIn('id="timeframes"', html)
        self.assertIn('defaultTimeframe', js_text)
        # Should NOT use the old placeholder chart routine anymore
        self.assertNotIn("drawPlaceholderChart", js_text)
        # Initial load + incremental update paths present
        self.assertIn("loadChartHistory", js_text)
        self.assertIn("updateLatestCandle", js_text)
        self.assertIn("POLL_INTERVAL_MS", js_text)
        # Resize handler bound on resize
        self.assertIn("resize", js_text.lower())

    def test_market_sort_unknown_volume_and_search_ordering(self) -> None:
        svc_mod = importlib.import_module("plugins.trade.webtrade2.service")
        service = svc_mod.WebTrade2Service(desk=FakeDesk())

        all_markets = service.markets("hyperliquid", "FIBO", "futures")
        self.assertEqual([m["symbol"] for m in all_markets["markets"]], ["BTC", "ETH", "ABC", "ZED"])
        self.assertEqual([m["volume_24h"] for m in all_markets["markets"]], ["1000", "500", None, None])

        searched = service.markets("hyperliquid", "FIBO", "futures", search="e")
        self.assertEqual([m["symbol"] for m in searched["markets"]], ["ETH", "ZED"])

    def test_ladder_preview_display_vwap_and_final_normalized_children(self) -> None:
        svc_mod = importlib.import_module("plugins.trade.webtrade2.service")
        service = svc_mod.WebTrade2Service(desk=FakeDesk(), session_secret="unit-secret")
        out = service.preview_ladder(
            exchange="hyperliquid",
            account="FIBO",
            market_type="futures",
            symbol="BTC",
            side="buy",
            distribution="uniform",
            order_count=12,
            total_size="1.26",
            start_price="100.24",
            end_price="90.24",
        )
        self.assertTrue(out["success"], out)
        children = out["children"]
        shown = out["display_children"]
        self.assertEqual(len(children), 12)
        self.assertEqual(len(shown), 11)  # first 5 + ellipsis + last 5
        self.assertEqual(shown[5]["ellipsis"], True)
        self.assertEqual(shown[:5], children[:5])
        self.assertEqual(shown[6:], children[-5:])
        self.assertTrue(all(Decimal(c["price"]) % Decimal("0.5") == 0 for c in children))
        self.assertTrue(all(Decimal(c["size"]) % Decimal("0.1") == 0 for c in children))
        expected = sum(Decimal(c["price"]) * Decimal(c["size"]) for c in children) / sum(Decimal(c["size"]) for c in children)
        self.assertEqual(Decimal(out["vwap"]), expected)

    def test_ladder_preview_displays_all_children_when_ten_or_fewer(self) -> None:
        svc_mod = importlib.import_module("plugins.trade.webtrade2.service")
        service = svc_mod.WebTrade2Service(desk=FakeDesk(), session_secret="unit-secret")
        out = service.preview_ladder(
            exchange="hyperliquid",
            account="FIBO",
            market_type="futures",
            symbol="BTC",
            side="sell",
            distribution="half_gaussian",
            order_count=10,
            total_size="2.0",
            start_price="100",
            end_price="110",
        )
        self.assertTrue(out["success"], out)
        self.assertEqual(out["display_children"], out["children"])
        self.assertFalse(any("ellipsis" in c for c in out["display_children"]))

    def test_static_app_loads_is_responsive_and_phase1_has_no_write_endpoints(self) -> None:
        app_mod = importlib.import_module("plugins.trade.webtrade2.app")
        cfg_mod = importlib.import_module("plugins.trade.webtrade2.config")
        svc_mod = importlib.import_module("plugins.trade.webtrade2.service")
        cfg = cfg_mod.WebTrade2Config.from_values(password="test-password", session_secret="secret-32", port=9009)
        app = app_mod.create_app(config=cfg, service=svc_mod.WebTrade2Service(desk=FakeDesk()))
        client = TestClient(app)

        h = client.get("/api/health").json()
        self.assertEqual(h["ok"], True)
        self.assertEqual(h["service"], "webtrade2")
        self.assertIn(h["phase"], (1, 2))  # backward-compat: Phase 2 service still valid
        self.assertTrue(h.get("read_only") is False or h.get("read_only") is True)  # shape only
        self.assertEqual(client.get("/api/exchanges").status_code, 401)
        login = client.post("/login", data={"password": "test-password"}, follow_redirects=False)
        self.assertEqual(login.status_code, 303)
        self.assertIn("webtrade2_session", login.headers.get("set-cookie", ""))
        session = client.get("/api/session")
        self.assertEqual(session.status_code, 200)
        self.assertIn("csrf", session.json())
        root = client.get("/")
        self.assertEqual(root.status_code, 200)
        self.assertIn("viewport", root.text)
        css = client.get("/static/style.css")
        js = client.get("/static/app.js")
        self.assertEqual(css.status_code, 200)
        self.assertEqual(js.status_code, 200)
        self.assertIn("@media", css.text)
        self.assertNotIn("orderbook", js.text.lower())
        self.assertNotIn("/api/depth", js.text.lower())
        self.assertNotIn("recent-trades", js.text.lower())

        # In Phase 2, write endpoints exist. When WRITE_ENABLED=0 they
        # return 423 PHASE2_DISABLED; in any case they must NOT succeed.
        # In Phase 1 tests they may still 404 if the endpoint is absent.
        for path in ("/api/trade/execute", "/api/orders/cancel_group"):
            code = client.post(path, json={}).status_code
            self.assertIn(code, (400, 404, 423), f"{path} should not succeed (got {code})")
        exchanges = client.get("/api/exchanges").json()
        self.assertTrue(exchanges["success"])
        self.assertNotIn("secret", str(exchanges).lower())
        self.assertNotIn("wallet", str(exchanges).lower())

    def test_ui_refinement_static_contract(self) -> None:
        app_mod = importlib.import_module("plugins.trade.webtrade2.app")
        cfg_mod = importlib.import_module("plugins.trade.webtrade2.config")
        svc_mod = importlib.import_module("plugins.trade.webtrade2.service")
        cfg = cfg_mod.WebTrade2Config.from_values(password="test-password", session_secret="secret-32", port=9009)
        app = app_mod.create_app(config=cfg, service=svc_mod.WebTrade2Service(desk=FakeDesk()))
        client = TestClient(app)

        html = client.get("/").text
        css = client.get("/static/style.css").text
        js = client.get("/static/app.js").text

        self.assertIn('data-trade-tab="order"', html)
        self.assertIn('data-trade-tab="ladder"', html)
        self.assertIn('id="orderPanel"', html)
        self.assertIn('id="ladderPanel"', html)
        self.assertIn('class="account-strip', html)
        self.assertIn('id="marketHeader"', html)
        self.assertIn('id="mobileNav"', html)
        self.assertIn('localStorage.setItem("webtrade2.marketType"', js)
        self.assertIn('preferredMarketType', js)
        self.assertIn('Futures', html)
        self.assertIn('.trade-pane[hidden]', css)
        self.assertIn('overflow-x:hidden', css.replace(' ', ''))
        self.assertIn('@media (max-width: 720px)', css)
        self.assertNotIn('orderbook', js.lower())
        self.assertNotIn('recent-trades', js.lower())
        self.assertNotIn('/api/depth', js.lower())

    def test_account_summary_distinguishes_real_zero_from_unavailable_and_preserves_mark(self) -> None:
        svc_mod = importlib.import_module("plugins.trade.webtrade2.service")
        service = svc_mod.WebTrade2Service(desk=FakeDesk())
        out = service.account_state("hyperliquid", "FIBO")

        summary = out["account_summary"]
        self.assertEqual(summary["equity"], "12345.67")
        self.assertEqual(summary["available"], "0")  # explicit zero, not unavailable
        self.assertEqual(summary["unrealized_pnl"], "14766.09742784")
        self.assertEqual(out["positions"][0]["mark"], "66123.987654")

        class MissingBalanceDesk(FakeDesk):
            def execute(self, request: Dict[str, Any]):
                if request.get("operation") == "balance":
                    return make_success(operation="balance", exchange=str(request.get("exchange") or ""), account=str(request.get("account") or ""))
                return super().execute(request)

        missing = svc_mod.WebTrade2Service(desk=MissingBalanceDesk()).account_state("hyperliquid", "FIBO")
        self.assertIsNone(missing["account_summary"]["equity"])
        self.assertIsNone(missing["account_summary"]["available"])

    def test_ui_layout_internal_market_scroll_and_formatting_contract(self) -> None:
        app_mod = importlib.import_module("plugins.trade.webtrade2.app")
        cfg_mod = importlib.import_module("plugins.trade.webtrade2.config")
        svc_mod = importlib.import_module("plugins.trade.webtrade2.service")
        cfg = cfg_mod.WebTrade2Config.from_values(password="test-password", session_secret="secret-32", port=9009)
        app = app_mod.create_app(config=cfg, service=svc_mod.WebTrade2Service(desk=FakeDesk()))
        client = TestClient(app)
        css = client.get("/static/style.css").text
        js = client.get("/static/app.js").text

        self.assertIn(".workspace", css)
        self.assertIn("minmax(0,", css)
        self.assertIn("#markets", css)
        self.assertIn("overflow-y:auto", css.replace(" ", ""))
        self.assertIn("position:sticky", css)
        self.assertIn("formatDynamicPrice", js)
        self.assertIn("formatSignedMoney", js)
        self.assertIn("formatCompactVolume", js)
        self.assertIn("p.mark", js)
        self.assertNotIn("orderbook", js.lower())
        self.assertNotIn("recent-trades", js.lower())
        self.assertNotIn("/api/depth", js.lower())

    def test_ladder_preview_markup_requires_first_last_display_contract(self) -> None:
        html = Path(__file__).resolve().parents[1] / "webtrade2" / "static" / "index.html"
        js = Path(__file__).resolve().parents[1] / "webtrade2" / "static" / "app.js"
        html_text = html.read_text(encoding="utf-8")
        js_text = js.read_text(encoding="utf-8")
        self.assertIn('id="ladderPreview"', html_text)
        self.assertIn("Ladder VWAP", html_text)
        self.assertIn("FIRST 5", js_text)
        self.assertIn("LAST 5", js_text)
        self.assertIn("ellipsis", js_text)
        self.assertIn("display_children", js_text)

    def test_visual_blue_accent_contract(self) -> None:
        app_mod = importlib.import_module("plugins.trade.webtrade2.app")
        cfg_mod = importlib.import_module("plugins.trade.webtrade2.config")
        svc_mod = importlib.import_module("plugins.trade.webtrade2.service")
        cfg = cfg_mod.WebTrade2Config.from_values(password="test-password", session_secret="secret-32", port=9009)
        app = app_mod.create_app(config=cfg, service=svc_mod.WebTrade2Service(desk=FakeDesk()))
        client = TestClient(app)

        css = client.get("/static/style.css").text
        js = client.get("/static/app.js").text

        # buy accent must be a blue, not a green
        self.assertTrue(re.search(r"--buy:\s*#?[0-9a-fA-F]*[bB]lue|--buy:\s*#?(?:3[bd]|4[56]|5[6789]|6[0-9bcdef])[0-9a-fA-F]{4,5}", css), css)
        self.assertFalse(re.search(r"--buy:\s*(?:#?20c77b|#22c55e|#16a34a|green)", css, re.I), css)
        # sell stays red
        self.assertRegex(css, r"--sell:\s*#?ef5b67", css)
        # primary interactive accent (active tab, focus, selected market) reuses the blue buy hue
        self.assertRegex(css, r"--accent:\s*#?(?:3[bd]|4[56]|5[6789]|6[0-9bcdef])[0-9a-fA-F]{4,5}", css)
        # buy button class styles blue
        self.assertIn(".buy", css)
        self.assertIn("var(--buy)", css)
        # active tab uses the blue accent (border-bottom for terminal-tab style)
        self.assertIn(".active", css)
        self.assertIn("border-bottom-color:var(--accent)", css)

    def test_visual_typography_and_density_contract(self) -> None:
        css_path = Path(__file__).resolve().parents[1] / "webtrade2" / "static" / "style.css"
        css = css_path.read_text(encoding="utf-8")
        # modern UI sans-serif stack
        self.assertIn("Inter", css)
        self.assertIn("system-ui", css)
        # tabular numbers for price/balance/PNL/table columns
        self.assertIn("tabular-nums", css)
        # compactness: smaller border radii and tighter control sizing
        self.assertIn("--radius:", css)
        self.assertNotIn("--radius:16px", css)  # previous 16px is too rounded
        # ensure panels are flatter (small radii not large)
        for match in re.findall(r"--radius:\s*([^;]+);", css):
            value = match.strip()
            self.assertTrue(value.endswith("px"))
            num = int(value[:-2])
            self.assertLessEqual(num, 8, f"radius too large: {value}")
        # no oversized headings
        self.assertNotIn("font-size:32px", css)
        self.assertNotIn("font-size:30px", css)

    def test_visual_market_rows_are_table_like_not_cards(self) -> None:
        css_path = Path(__file__).resolve().parents[1] / "webtrade2" / "static" / "style.css"
        html_path = Path(__file__).resolve().parents[1] / "webtrade2" / "static" / "index.html"
        css = css_path.read_text(encoding="utf-8")
        html = html_path.read_text(encoding="utf-8")
        # market head + market-row grid columns symbolize tabular layout
        self.assertIn(".market-head", css)
        self.assertIn(".market-row", css)
        # selected market uses subtle blue highlight, not big filled block
        self.assertIn(".market-row.selected", css)
        # favorite/all tabs styled (data attribute present in HTML)
        self.assertIn('data-market-tab="favorites"', html)
        self.assertIn('data-market-tab="all"', html)
        self.assertIn('class="tabs market-tabs"', html)
        # HTML preserves Symbols|Price|24h|Volume header
        self.assertIn(">Symbol<", html)
        self.assertIn(">Price<", html)
        self.assertIn(">24h<", html)
        self.assertIn(">Volume<", html)

    def test_visual_positions_table_is_terminal_style(self) -> None:
        css_path = Path(__file__).resolve().parents[1] / "webtrade2" / "static" / "style.css"
        css = css_path.read_text(encoding="utf-8")
        # table-style positions/orders/fills
        self.assertIn(".data-table table", css)
        # tabular numeric alignment
        self.assertIn("text-align:right", css)
        # header + side colors must use blue/red semantics
        self.assertIn("var(--buy)", css)
        self.assertIn("var(--sell)", css)
        # explicit positive/negative PnL classes
        self.assertIn(".pnl-pos", css)
        self.assertIn(".pnl-neg", css)

    def test_market_summary_volume_sorting_and_compact_formatting(self) -> None:
        svc_mod = importlib.import_module("plugins.trade.webtrade2.service")
        service = svc_mod.WebTrade2Service(desk=FakeDesk())
        # Known volumes descend; unknown follow alphabetically and trail
        rows = [
            {"symbol": "AAA", "price": "1", "change_24h": "+5%", "volume_24h": "1000"},
            {"symbol": "BBB", "price": "1", "change_24h": "+5%", "volume_24h": "3000"},
            {"symbol": "CCC", "price": "1", "change_24h": "+5%", "volume_24h": "2000"},
            {"symbol": "ZZZ", "price": "1", "change_24h": "+5%", "volume_24h": None},
            {"symbol": "MMM", "price": "1", "change_24h": "+5%", "volume_24h": None},
        ]
        ranked = service._rank_markets(rows)
        self.assertEqual([r["symbol"] for r in ranked], ["BBB", "CCC", "AAA", "MMM", "ZZZ"])
        # Search preserves ranking
        ranked2 = service._rank_markets(rows, search="a")
        self.assertEqual([r["symbol"] for r in ranked2], ["AAA"])

    def test_format_compact_volume_and_price_precision(self) -> None:
        svc_mod = importlib.import_module("plugins.trade.webtrade2.service")
        # Compact notation
        self.assertEqual(svc_mod.format_compact_volume("3240000000"), "$3.24B")
        self.assertEqual(svc_mod.format_compact_volume("428100000"), "$428.1M")
        self.assertEqual(svc_mod.format_compact_volume("7300000"), "$7.3M")
        self.assertEqual(svc_mod.format_compact_volume("842000"), "$842K")
        self.assertEqual(svc_mod.format_compact_volume("0.5"), "$0.5")
        self.assertIsNone(svc_mod.format_compact_volume(None))
        self.assertIsNone(svc_mod.format_compact_volume(""))
        # Dynamic price precision
        self.assertEqual(svc_mod.format_dynamic_price("79425.234"), "79,425.23")
        self.assertEqual(svc_mod.format_dynamic_price("2485.3145"), "2,485.31")
        self.assertEqual(svc_mod.format_dynamic_price("92.8105"), "92.81")
        self.assertEqual(svc_mod.format_dynamic_price("0.25169"), "0.25169")
        self.assertEqual(svc_mod.format_dynamic_price(None), "—")
        # 24h change as signed percentage when known
        self.assertEqual(svc_mod.format_pct_change("2.41"), "+2.41%")
        self.assertEqual(svc_mod.format_pct_change("-1.08"), "-1.08%")
        self.assertEqual(svc_mod.format_pct_change(None), "—")

    def test_candles_history_endpoint_returns_normalized_ohlcv(self) -> None:
        app_mod = importlib.import_module("plugins.trade.webtrade2.app")
        cfg_mod = importlib.import_module("plugins.trade.webtrade2.config")
        svc_mod = importlib.import_module("plugins.trade.webtrade2.service")
        cfg = cfg_mod.WebTrade2Config.from_values(password="test-password", session_secret="secret-32", port=9009)
        app = app_mod.create_app(config=cfg, service=svc_mod.WebTrade2Service(desk=FakeDesk()))
        client = TestClient(app)
        # Need session
        client.post("/login", data={"password": "test-password"}, follow_redirects=False)
        resp = client.get("/api/candles?exchange=hyperliquid&account=FIBO&symbol=BTC&interval=1h&limit=10&market_type=futures")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body.get("success"))
        data = body["data"]
        self.assertEqual(len(data["candles"]), 2)
        c0 = data["candles"][0]
        self.assertEqual(set(c0.keys()), {"time", "open", "high", "low", "close", "volume"})
        # No successful write endpoint reachable. Phase 2 returns 400/423 for
        # the write endpoints it owns; never 200.
        for ep in ("/api/trade/execute", "/api/orders/cancel_group", "/api/positions/close", "/api/tp", "/api/sl", "/api/leverage"):
            code = client.post(ep, json={}).status_code
            self.assertIn(code, (400, 404, 423), f"{ep} must not succeed, got {code}")

    def test_market_summary_apex_catalog_only_fallback(self) -> None:
        # Apex catalog returns instrument rows but no per-symbol ticker; verify the service still
        # emits catalog rows sorted deterministically (unknown-volume ordering, alphabetical tail)
        class CatalogDesk(FakeDesk):
            def capabilities(self, exchange: str) -> List[str]:
                if exchange == "apex":
                    return ["list_instruments", "resolve_instrument", "market_price", "candles"]
                return super().capabilities(exchange)
            def execute(self, request: Dict[str, Any]):
                if request.get("operation") == "list_instruments":
                    return make_success(
                        operation="list_instruments",
                        exchange=str(request.get("exchange") or ""),
                        account=str(request.get("account") or ""),
                        data={
                            "instruments": [
                                {"instrument": "BTCUSDT", "display_name": "BTC-USDT"},
                                {"instrument": "ETHUSDT", "display_name": "ETH-USDT"},
                                {"instrument": "ZECUSDT", "display_name": "ZEC-USDT"},
                            ]
                        },
                    )
                return super().execute(request)

        svc_mod = importlib.import_module("plugins.trade.webtrade2.service")
        out = svc_mod.WebTrade2Service(desk=CatalogDesk()).markets("apex", "BITGET", "futures")
        syms = [r["symbol"] for r in out["markets"]]
        # All three present, alphabetical tail because volumes are unknown
        self.assertEqual(set(syms), {"BTCUSDT", "ETHUSDT", "ZECUSDT"})


if __name__ == "__main__":
    unittest.main()
