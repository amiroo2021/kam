"""WebTrade2 market-list migration tests for canonical get_tickers.

Phase D removes WebTrade2's Apex-specific ticker fan-out. These tests verify
that WebTrade2 prefers the canonical TradeDesk/agent get_tickers contract,
keeps a generic list_instruments fallback for agents not migrated yet, and
ranks only by quote/notional turnover metadata.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, "/root/kam")

from plugins.trade.canonical import CanonicalMarketPrice, CanonicalTickersBatch, make_success
from plugins.trade.webtrade2.service import WebTrade2Service


class FakeDesk:
    def __init__(self, caps: Dict[str, List[str]], responses: Dict[str, Any]) -> None:
        self._caps = caps
        self._responses = responses
        self.requests: List[Dict[str, Any]] = []

    def list_exchanges(self) -> List[str]:
        return sorted(self._caps)

    def list_accounts(self, exchange: str) -> List[Any]:
        return ["acct"]

    def capabilities(self, exchange: str) -> List[str]:
        return list(self._caps.get(exchange, []))

    def execute(self, request: Dict[str, Any]) -> Any:
        self.requests.append(dict(request))
        op = str(request.get("operation") or "")
        key = f"{request.get('exchange')}:{op}"
        if key not in self._responses:
            raise AssertionError(f"unexpected request: {request}")
        return self._responses[key]


def _mp(symbol: str, *, price: Any = "1", turnover: Any = None, quote_volume: Any = None,
        base_volume: Any = None, funding: Any = None, status_fields: bool = True) -> CanonicalMarketPrice:
    return CanonicalMarketPrice(
        requested_symbol=symbol,
        market=symbol,
        symbol=symbol,
        display_name=symbol,
        display_symbol=symbol,
        native_symbol=symbol,
        base=symbol.split("-", 1)[0] if "-" in symbol else symbol,
        quote="USDT",
        market_type="perp",
        price=price,
        mark_price=price,
        turnover_24h=turnover,
        volume_24h_quote=quote_volume,
        volume_24h_base=base_volume,
        funding_rate=funding,
    )


def _batch(tickers: Dict[str, CanonicalMarketPrice], **kw: Any):
    return make_success(
        operation="get_tickers",
        exchange="apex",
        account="acct",
        tickers_batch=CanonicalTickersBatch(tickers=tickers, **kw),
    )


class WebTrade2CanonicalMarketListTests(unittest.TestCase):
    def test_webtrade2_prefers_get_tickers_when_capability_exists(self) -> None:
        desk = FakeDesk(
            {"apex": ["list_instruments", "get_tickers"]},
            {"apex:get_tickers": _batch({"BTC-USDT": _mp("BTC-USDT", price="70000", turnover="1000")})},
        )
        out = WebTrade2Service(desk=desk).markets("apex", "acct", "futures")
        self.assertTrue(out["success"])
        self.assertEqual([r["symbol"] for r in out["markets"]], ["BTC-USDT"])
        self.assertEqual([r["price"] for r in out["markets"]], ["70000"])
        self.assertEqual([r["operation"] for r in desk.requests], ["get_tickers"])

    def test_apex_webtrade2_uses_canonical_get_tickers(self) -> None:
        desk = FakeDesk(
            {"apex": ["list_instruments", "get_tickers"]},
            {"apex:get_tickers": _batch({
                "ETH-USDT": _mp("ETH-USDT", price="3500", turnover="500", funding="0.0001"),
            })},
        )
        row = WebTrade2Service(desk=desk).markets("apex", "acct", "futures")["markets"][0]
        self.assertEqual(row["symbol"], "ETH-USDT")
        self.assertEqual(row["price"], "3500")
        self.assertEqual(row["turnover_24h"], "500")
        self.assertEqual(row["funding"], "0.0001")
        self.assertEqual(desk.requests[0]["operation"], "get_tickers")

    def test_hyperliquid_webtrade2_uses_canonical_get_tickers(self) -> None:
        resp = make_success(
            operation="get_tickers",
            exchange="hyperliquid",
            account="acct",
            tickers_batch=CanonicalTickersBatch(tickers={
                "BTC": _mp("BTC", price="70000", turnover="2000"),
                "ETH": _mp("ETH", price="3500", turnover="1000"),
            }),
        )
        desk = FakeDesk({"hyperliquid": ["list_instruments", "get_tickers"]}, {"hyperliquid:get_tickers": resp})
        out = WebTrade2Service(desk=desk).markets("hyperliquid", "acct", "futures")
        self.assertEqual([r["symbol"] for r in out["markets"]], ["BTC", "ETH"])
        self.assertEqual([r["operation"] for r in desk.requests], ["get_tickers"])

    def test_generic_list_instruments_fallback_without_get_tickers(self) -> None:
        list_resp = make_success(
            operation="list_instruments",
            exchange="legacy",
            account="acct",
            data={"instruments": [
                {"instrument": "BTC", "display_name": "BTC", "price": "65000", "volume_24h": "1000"},
                {"instrument": "ABC", "display_name": "ABC", "price": "1", "volume_24h": None},
            ]},
        )
        desk = FakeDesk({"legacy": ["list_instruments"]}, {"legacy:list_instruments": list_resp})
        out = WebTrade2Service(desk=desk).markets("legacy", "acct", "futures")
        self.assertEqual([r["symbol"] for r in out["markets"]], ["BTC", "ABC"])
        self.assertEqual([r["operation"] for r in desk.requests], ["list_instruments"])

    def test_apex_stale_preserved_values_display_normally(self) -> None:
        desk = FakeDesk(
            {"apex": ["get_tickers"]},
            {"apex:get_tickers": _batch(
                {"BTC-USDT": _mp("BTC-USDT", price="70000", turnover="1000")},
                stale_symbols=("BTC-USDT",),
                refresh_status="partial",
                source="sdk",
            )},
        )
        row = WebTrade2Service(desk=desk).markets("apex", "acct")["markets"][0]
        self.assertEqual(row["price"], "70000")
        self.assertEqual(row["ticker_status"], "stale")
        self.assertEqual(row["volume_24h"], "1000")

    def test_unavailable_apex_values_display_dash_semantics(self) -> None:
        desk = FakeDesk(
            {"apex": ["get_tickers"]},
            {"apex:get_tickers": _batch(
                {"MISSING-USDT": _mp("MISSING-USDT", price=None, turnover=None, quote_volume=None)},
                failed_symbols=("MISSING-USDT",),
                refresh_status="partial",
            )},
        )
        row = WebTrade2Service(desk=desk).markets("apex", "acct")["markets"][0]
        self.assertEqual(row["symbol"], "MISSING-USDT")
        self.assertIsNone(row["price"])
        self.assertIsNone(row["volume_24h"])
        self.assertEqual(row["ticker_status"], "unavailable")

    def test_turnover_then_quote_volume_ranking_and_base_volume_ignored(self) -> None:
        desk = FakeDesk(
            {"apex": ["get_tickers"]},
            {"apex:get_tickers": _batch({
                "BASE-HUGE": _mp("BASE-HUGE", price="1", turnover=None, quote_volume=None, base_volume="999999999"),
                "QUOTE-MID": _mp("QUOTE-MID", price="1", turnover=None, quote_volume="500"),
                "TURNOVER-HIGH": _mp("TURNOVER-HIGH", price="1", turnover="1000", quote_volume="1"),
                "UNKNOWN": _mp("UNKNOWN", price="1", turnover=None, quote_volume=None),
            })},
        )
        rows = WebTrade2Service(desk=desk).markets("apex", "acct")["markets"]
        self.assertEqual([r["symbol"] for r in rows], ["TURNOVER-HIGH", "QUOTE-MID", "BASE-HUGE", "UNKNOWN"])
        self.assertIsNone(rows[2]["volume_24h"], "base volume must not become ranking volume")

    def test_unknown_volume_tail_alphabetical_and_search_filtering(self) -> None:
        desk = FakeDesk(
            {"apex": ["get_tickers"]},
            {"apex:get_tickers": _batch({
                "ZZZ": _mp("ZZZ", price="1"),
                "AAA": _mp("AAA", price="1"),
                "MMM": _mp("MMM", price="1", turnover="100"),
            })},
        )
        svc = WebTrade2Service(desk=desk)
        self.assertEqual([r["symbol"] for r in svc.markets("apex", "acct")["markets"]], ["MMM", "AAA", "ZZZ"])
        self.assertEqual([r["symbol"] for r in svc.markets("apex", "acct", search="zz")["markets"]], ["ZZZ"])

    def test_offline_old_shape_vs_canonical_shape_parity_for_representatives(self) -> None:
        """Representative old-vs-new parity without live Apex calls.

        Old WebTrade2 rows used no-dash symbols plus markPrice / turnover24h /
        fundingRate fields. Canonical rows use dash symbols and normalized
        fields. The WebTrade2 presentation/ranking model should preserve
        identity, displayed price, ranking turnover, funding, and market type.
        """
        symbols = ["BTC", "ETH", "NVDA", "QQQ", "AAPL"]
        old_rows = [
            {
                "symbol": f"{s}USDT",
                "display_name": f"{s}USDT",
                "price": str(i * 100),
                "turnover24h": str(i * 1000),
                "fundingRate": "0.0001",
                "market_type": "perp",
            }
            for i, s in enumerate(symbols, start=1)
        ]
        old_by_base = {r["symbol"].replace("USDT", ""): r for r in old_rows}
        tickers = {
            f"{s}-USDT": _mp(
                f"{s}-USDT",
                price=str(i * 100),
                turnover=str(i * 1000),
                funding="0.0001",
            )
            for i, s in enumerate(symbols, start=1)
        }
        desk = FakeDesk(
            {"apex": ["get_tickers"]},
            {"apex:get_tickers": _batch(tickers)},
        )
        rows = WebTrade2Service(desk=desk).markets("apex", "acct")["markets"]
        new_by_base = {r["symbol"].replace("-USDT", ""): r for r in rows}

        for base in symbols:
            old = old_by_base[base]
            new = new_by_base[base]
            self.assertEqual(new["symbol"], f"{base}-USDT")
            self.assertEqual(new["price"], old["price"])
            self.assertEqual(new["turnover_24h"], old["turnover24h"])
            self.assertEqual(new["volume_24h"], old["turnover24h"])
            self.assertEqual(new["funding"], old["fundingRate"])
            self.assertEqual(new["market_type"], old["market_type"])

    def test_no_webtrade2_apex_direct_provider_references(self) -> None:
        service_text = Path("/root/kam/plugins/trade/webtrade2/service.py").read_text(encoding="utf-8")
        self.assertNotIn("_apex_enrich", service_text)
        self.assertNotIn("apex_ticker_cache", service_text)
        self.assertNotIn("pro.apex.exchange", service_text)


class WebTrade2Batch1CanonicalPathTests(unittest.TestCase):
    def test_batch1_agents_make_webtrade2_use_get_tickers_without_exchange_branches(self) -> None:
        agent_files = {
            "rise": Path("/root/kam/plugins/trade/agents/x_rise_agent.py"),
            "perpl": Path("/root/kam/plugins/trade/agents/x_perpl_agent.py"),
            "pacifica": Path("/root/kam/plugins/trade/agents/x_pacifica_agent.py"),
        }
        for exchange, path in agent_files.items():
            text = path.read_text(encoding="utf-8")
            self.assertIn('"get_tickers"', text, f"{exchange} must advertise canonical get_tickers")

        agent_caps = {
            "rise": ["list_instruments", "get_tickers"],
            "perpl": ["list_instruments", "get_tickers"],
            "pacifica": ["list_instruments", "get_tickers"],
        }
        responses = {
            "rise:get_tickers": make_success(
                operation="get_tickers",
                exchange="rise",
                account="acct",
                tickers_batch=CanonicalTickersBatch(tickers={
                    "RISE-HIGH": _mp("RISE-HIGH", price="10", turnover="1000"),
                }),
            ),
            "perpl:get_tickers": make_success(
                operation="get_tickers",
                exchange="perpl",
                account="acct",
                tickers_batch=CanonicalTickersBatch(tickers={
                    "PERPL-QUOTE": _mp("PERPL-QUOTE", price="20", quote_volume="500"),
                }),
            ),
            "pacifica:get_tickers": make_success(
                operation="get_tickers",
                exchange="pacifica",
                account="acct",
                tickers_batch=CanonicalTickersBatch(tickers={
                    "PACIFICA-BASE-HUGE": _mp("PACIFICA-BASE-HUGE", price="30", base_volume="999999"),
                    "PACIFICA-UNKNOWN-A": _mp("PACIFICA-UNKNOWN-A", price="1"),
                    "PACIFICA-UNKNOWN-Z": _mp("PACIFICA-UNKNOWN-Z", price="1"),
                }),
            ),
        }
        desk = FakeDesk(agent_caps, responses)
        svc = WebTrade2Service(desk=desk)

        self.assertEqual(svc.markets("rise", "acct")["markets"][0]["symbol"], "RISE-HIGH")
        self.assertEqual(svc.markets("perpl", "acct")["markets"][0]["volume_24h"], "500")
        pacifica_rows = svc.markets("pacifica", "acct")["markets"]
        self.assertEqual([r["symbol"] for r in pacifica_rows], [
            "PACIFICA-BASE-HUGE",
            "PACIFICA-UNKNOWN-A",
            "PACIFICA-UNKNOWN-Z",
        ])
        self.assertIsNone(pacifica_rows[0]["volume_24h"], "base volume must not become ranking volume")
        self.assertEqual([r["operation"] for r in desk.requests], ["get_tickers", "get_tickers", "get_tickers"])

        service_text = Path("/root/kam/plugins/trade/webtrade2/service.py").read_text(encoding="utf-8")
        for forbidden in ('exchange == "rise"', 'exchange == "perpl"', 'exchange == "pacifica"', "x_rise_agent", "x_perpl_agent", "x_pacifica_agent"):
            self.assertNotIn(forbidden, service_text)


if __name__ == "__main__":
    unittest.main()
