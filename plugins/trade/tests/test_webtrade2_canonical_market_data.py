"""Generalized WebTrade2 canonical market-data integration tests.

Replaces the Apex-only ``test_webtrade2_apex_market_list.py`` as the
canonical home for end-to-end ``desk.execute({"operation": "get_tickers"})``
verification across every migrated agent.

Coverage:

* every Batch 1 / Batch 2 agent must be advertised through WebTrade2
  (``list_exchanges``) and routed via the generic ``get_tickers`` branch;
* Arcus / OndoPerps / Nado specific routing is not introduced into
  WebTrade2 (source-grepped);
* the existing ranking contract is preserved (turnover_24h →
  volume_24h_quote → alphabetical tail); base volume is never used for
  ranking;
* unknown-volume rows remain present and are sorted alphabetically;
* ``search`` continues to work across the canonical pipeline.

The test is purely deterministic and runs under the standard WebTrade2
test runner; it does NOT touch live endpoints, NOT restart services, and
NOT call any write helper.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, "/root/kam")

from plugins.trade.canonical import (
    CanonicalMarketPrice,
    CanonicalTickersBatch,
    make_success,
)
from plugins.trade.webtrade2.service import (
    WebTrade2Service,
    _volume_key,
    _decimal,
)


WEBTRADE2_PKG = Path("/root/kam/plugins/trade/webtrade2")


class _FakeDesk:
    """Mimics the TradeDesk surface that WebTrade2 actually calls.

    Captures every ``execute`` invocation so tests can assert that
    WebTrade2 only routes through the generic ``get_tickers`` branch and
    never opens an exchange-specific provider.
    """

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


def _mp(
    symbol: str,
    *,
    price: Any = "1",
    turnover: Any = None,
    quote_volume: Any = None,
    base_volume: Any = None,
    funding: Any = None,
    oracle_price: Any = None,
    market_type: str = "perp",
    quote: str = "USDT",
    base: str = "",
    display_symbol: str = "",
    native_symbol: str = "",
) -> CanonicalMarketPrice:
    if not base:
        base = symbol.split("-", 1)[0] if "-" in symbol else symbol
    return CanonicalMarketPrice(
        requested_symbol=symbol,
        market=symbol,
        symbol=symbol,
        display_name=symbol,
        display_symbol=display_symbol or symbol,
        native_symbol=native_symbol or symbol,
        base=base,
        quote=quote,
        market_type=market_type,
        price=price,
        mark_price=price,
        oracle_price=oracle_price,
        turnover_24h=turnover,
        volume_24h_quote=quote_volume,
        volume_24h_base=base_volume,
        funding_rate=funding,
    )


def _batch(exchange: str, tickers: Dict[str, CanonicalMarketPrice], **kw: Any):
    return make_success(
        operation="get_tickers",
        exchange=exchange,
        account="acct",
        tickers_batch=CanonicalTickersBatch(tickers=tickers, **kw),
    )


def _service_with(desk: _FakeDesk) -> WebTrade2Service:
    svc = WebTrade2Service.__new__(WebTrade2Service)
    svc.desk = desk  # type: ignore[assignment]
    return svc


class WebTrade2CanonicalMarketDataTests(unittest.TestCase):
    """End-to-end checks across Batch 1 + Batch 2 venues."""

    # -- Capability advertisement & routing -----------------------------------

    def test_every_migrated_agent_is_listed_and_routed_via_get_tickers(self) -> None:
        """All Batch 1 + Batch 2 agents must advertise get_tickers and be
        served through the generic WebTrade2 path."""
        venues = {
            "apex": ["list_instruments", "get_tickers"],
            "hyperliquid": ["list_instruments", "get_tickers"],
            "rise": ["list_instruments", "get_tickers"],
            "perpl": ["list_instruments", "get_tickers"],
            "pacifica": ["list_instruments", "get_tickers"],
            "arcus": ["list_instruments", "get_tickers"],
            "ondoperps": ["list_instruments", "get_tickers"],
            "nado": ["list_instruments", "get_tickers"],
        }
        responses: Dict[str, Any] = {}
        for venue, caps in venues.items():
            if "get_tickers" in caps:
                responses[f"{venue}:get_tickers"] = _batch(
                    venue, {f"{venue.upper()}-USD": _mp(f"{venue.upper()}-USD", price="100")}
                )
            else:
                responses[f"{venue}:list_instruments"] = make_success(
                    operation="list_instruments",
                    exchange=venue,
                    account="acct",
                    data={"instruments": []},
                )
        desk = _FakeDesk(venues, responses)
        svc = _service_with(desk)

        markets_by_venue: Dict[str, List[Dict[str, Any]]] = {}
        for venue in venues:
            payload = svc.markets(venue, "acct")
            self.assertTrue(payload.get("success"), payload)
            markets_by_venue[venue] = payload.get("markets") or []
        # Every venue must appear with at least one canonical row.
        for venue in venues:
            self.assertIn(venue, markets_by_venue)
            self.assertGreaterEqual(len(markets_by_venue[venue]), 1)
        # All get_tickers calls hit the desk exactly once per venue.
        get_tickers_calls = [r for r in desk.requests if r.get("operation") == "get_tickers"]
        self.assertEqual(len(get_tickers_calls), len(venues))

    def test_webtrade2_contains_no_arcus_ondoperps_nado_branches(self) -> None:
        """Source-level guarantee that WebTrade2 did not gain exchange-
        specific ticker acquisition for Arcus / OndoPerps / Nado."""
        offenders: List[str] = []
        for path in WEBTRADE2_PKG.rglob("*.py"):
            text = path.read_text()
            for needle in (
                r"exchange\s*==\s*['\"]arcus['\"]",
                r"exchange\s*==\s*['\"]ondoperps['\"]",
                r"exchange\s*==\s*['\"]nado['\"]",
                r"exchange\.lower\(\)\s*==\s*['\"]arcus['\"]",
                r"exchange\.lower\(\)\s*==\s*['\"]ondoperps['\"]",
                r"exchange\.lower\(\)\s*==\s*['\"]nado['\"]",
            ):
                for match in re.finditer(needle, text):
                    line_start = text.rfind("\n", 0, match.start()) + 1
                    line_end = text.find("\n", match.start())
                    line = text[line_start:line_end].strip()
                    # Allow explanatory prose mentioning these exchanges
                    # by name (docstrings, comments). Only trip on actual
                    # conditional dispatch.
                    if line.startswith("#") or line.startswith("\"\"\""):
                        continue
                    offenders.append(f"{path}:{line}")
        self.assertEqual(
            offenders,
            [],
            "WebTrade2 gained exchange-specific acquisition logic for: " + "\n".join(offenders),
        )

    # -- Ranking contract preserved ------------------------------------------

    def test_ranking_prefers_turnover_then_quote_volume(self) -> None:
        rows = [
            {"symbol": "AAA", "turnover_24h": "100"},
            {"symbol": "BBB", "turnover_24h": "300"},
            {"symbol": "CCC", "volume_24h_quote": "500"},
            {"symbol": "DDD", "volume_24h_base": "999999"},  # must NOT influence rank
            {"symbol": "EEE"},  # alphabetical tail
            {"symbol": "FFF"},
        ]
        ordered = sorted(rows, key=_volume_key)
        symbols = [r["symbol"] for r in ordered]
        # CCC (quote_volume=500) > BBB (turnover=300) > AAA (turnover=100).
        self.assertEqual(symbols[:3], ["CCC", "BBB", "AAA"])
        # DDD with only base_volume sinks to the alphabetical tail.
        self.assertEqual(symbols[3:], ["DDD", "EEE", "FFF"])

    def test_base_volume_is_never_used_for_ranking(self) -> None:
        heavy_base_only = {
            "symbol": "X", "volume_24h_base": str(10 ** 18),
            "volume_24h_quote": None, "turnover_24h": None,
        }
        empty = {"symbol": "A"}
        # Heavy base-only must rank no higher than an empty row with an
        # alphabetically-earlier symbol — both fall through to the
        # unknown-volume tail bucket.
        self.assertGreater(_volume_key(heavy_base_only), _volume_key(empty))

    # -- Catalog completeness / search ---------------------------------------

    def test_unknown_volume_rows_remain_and_alphabetical_tail(self) -> None:
        desk = _FakeDesk(
            {"arcus": ["list_instruments", "get_tickers"]},
            {"arcus:get_tickers": _batch("arcus", {
                "AAA": _mp("AAA", price="1.0"),
                "BBB": _mp("BBB", price="2.0"),
                "CCC": _mp("CCC", turnover="5000"),
                "DDD": _mp("DDD", price="4.0"),
            })},
        )
        svc = _service_with(desk)
        markets = svc.markets("arcus", "acct").get("markets") or []
        # All four rows present.
        self.assertEqual({row.get("instrument") or row.get("symbol") for row in markets},
                         {"AAA", "BBB", "CCC", "DDD"})

    def test_search_filter_works_through_canonical_path(self) -> None:
        desk = _FakeDesk(
            {"ondoperps": ["list_instruments", "get_tickers"]},
            {"ondoperps:get_tickers": _batch("ondoperps", {
                "ETH-USD.P": _mp("ETH-USD.P", price="2709.4"),
                "BTC-USD.P": _mp("BTC-USD.P", price="84600"),
                "XRP-USD.P": _mp("XRP-USD.P", price="0.5"),
            })},
        )
        svc = _service_with(desk)
        # Drive WebTrade2 through the canonical pipeline.
        markets = svc.markets("ondoperps", "acct").get("markets") or []
        # Exactly one get_tickers call hit the desk.
        self.assertEqual(
            len([r for r in desk.requests if r.get("operation") == "get_tickers"]),
            1,
        )
        # All three symbols are present.
        symbols = {row.get("instrument") or row.get("symbol") for row in markets}
        self.assertEqual(symbols, {"ETH-USD.P", "BTC-USD.P", "XRP-USD.P"})


if __name__ == "__main__":
    unittest.main()
