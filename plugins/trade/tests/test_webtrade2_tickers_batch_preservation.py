"""Generic WebTrade2 CanonicalTickersBatch row-preservation tests.

These tests drive ``_rows_from_tickers_batch`` and ``_rank_markets`` directly.
They intentionally avoid live HTTP, exchange agents, canonical.py edits, and any
write/trading path.
"""

from __future__ import annotations

import sys
import unittest
from typing import Any, Dict, Mapping

sys.path.insert(0, "/root/kam")

from plugins.trade.canonical import CanonicalMarketPrice, CanonicalTickersBatch
from plugins.trade.webtrade2.service import WebTrade2Service


CANONICAL_FIELDS = (
    "symbol",
    "native_symbol",
    "display_symbol",
    "display_name",
    "base",
    "quote",
    "market_type",
    "price",
    "mark_price",
    "oracle_price",
    "last_external_price",
    "funding_rate",
    "open_interest",
    "turnover_24h",
    "volume_24h_quote",
    "volume_24h_base",
    "change_24h_pct",
    "price_increment",
    "size_increment",
    "minimum_size",
    "minimum_notional",
    "last_updated_time",
)


def _service() -> WebTrade2Service:
    return WebTrade2Service.__new__(WebTrade2Service)


def _row_from(raw: Mapping[str, Any], *, refresh_status: str = "ok") -> Dict[str, Any]:
    data = {
        "tickers_batch": {
            "tickers": {"ROW": dict(raw)},
            "refresh_status": refresh_status,
            "source": "unit",
            "failed_symbols": [],
            "stale_symbols": [],
        }
    }
    rows = _service()._rows_from_tickers_batch(data, market_type="futures")
    assert len(rows) == 1
    return rows[0]


def _canonical_row(**overrides: Any) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {
        "requested_symbol": "BTC-USDT",
        "market": "BTC-USDT",
        "symbol": "BTC-USDT",
        "native_symbol": "BTCUSDT",
        "display_symbol": "BTC/USDT",
        "display_name": "Bitcoin Perp",
        "base": "BTC",
        "quote": "USDT",
        "market_type": "perp",
        "price": "100.1",
        "mark_price": "100.2",
        "oracle_price": "100.3",
        "last_external_price": "100.4",
        "funding_rate": "0.0001",
        "open_interest": "12345",
        "turnover_24h": "1000000",
        "volume_24h_quote": "999999",
        "volume_24h_base": "10",
        "change_24h_pct": "2.5",
        "price_increment": "0.1",
        "size_increment": "0.001",
        "minimum_size": "0.001",
        "minimum_notional": "5",
        "last_updated_time": "2026-09-27T00:00:00Z",
    }
    kwargs.update(overrides)
    return CanonicalMarketPrice(**kwargs).to_dict()


class WebTrade2TickersBatchPreservationTests(unittest.TestCase):
    def test_A_open_interest_survives(self) -> None:
        self.assertEqual(_row_from(_canonical_row(open_interest="777"))["open_interest"], "777")

    def test_B_change_24h_pct_survives(self) -> None:
        self.assertEqual(_row_from(_canonical_row(change_24h_pct="-1.25"))["change_24h_pct"], "-1.25")

    def test_C_oracle_price_survives(self) -> None:
        self.assertEqual(_row_from(_canonical_row(oracle_price="101.01"))["oracle_price"], "101.01")

    def test_D_last_external_price_survives(self) -> None:
        self.assertEqual(_row_from(_canonical_row(last_external_price="102.02"))["last_external_price"], "102.02")

    def test_E_funding_rate_survives_independent_of_legacy_funding(self) -> None:
        row = _row_from({**_canonical_row(funding_rate="0.00042"), "funding": "legacy-other"})
        self.assertEqual(row["funding_rate"], "0.00042")
        self.assertEqual(row["funding"], "0.00042")

    def test_F_turnover_24h_survives(self) -> None:
        self.assertEqual(_row_from(_canonical_row(turnover_24h="123456"))["turnover_24h"], "123456")

    def test_G_volume_24h_quote_survives(self) -> None:
        self.assertEqual(_row_from(_canonical_row(volume_24h_quote="654321"))["volume_24h_quote"], "654321")

    def test_H_volume_24h_base_survives_but_is_not_ranking_fallback(self) -> None:
        row = _row_from(_canonical_row(turnover_24h=None, volume_24h_quote=None, volume_24h_base="999999999"))
        self.assertEqual(row["volume_24h_base"], "999999999")
        ranked = _service()._rank_markets([
            {"symbol": "BASEONLY", "turnover_24h": None, "volume_24h_quote": None, "volume_24h": None, "volume_24h_base": "999999999"},
            {"symbol": "KNOWN", "turnover_24h": "1", "volume_24h_base": None},
        ])
        self.assertEqual([r["symbol"] for r in ranked], ["KNOWN", "BASEONLY"])

    def test_I_last_updated_time_survives(self) -> None:
        self.assertEqual(_row_from(_canonical_row(last_updated_time="1234567890"))["last_updated_time"], "1234567890")

    def test_J_none_remains_none_and_price_is_not_fabricated_from_mark_price(self) -> None:
        raw = _canonical_row(
            price=None,
            mark_price="55",
            oracle_price=None,
            last_external_price=None,
            funding_rate=None,
            open_interest=None,
            turnover_24h=None,
            volume_24h_quote=None,
            volume_24h_base=None,
            change_24h_pct=None,
            last_updated_time=None,
        )
        row = _row_from(raw)
        for field in (
            "price",
            "oracle_price",
            "last_external_price",
            "funding_rate",
            "open_interest",
            "turnover_24h",
            "volume_24h_quote",
            "volume_24h_base",
            "change_24h_pct",
            "last_updated_time",
        ):
            self.assertIsNone(row[field], field)
        self.assertEqual(row["mark_price"], "55")

    def test_K_missing_optional_fields_remain_absent_or_none(self) -> None:
        row = _row_from({"symbol": "MINIMAL"})
        for field in CANONICAL_FIELDS:
            self.assertIn(field, row)
        for field in set(CANONICAL_FIELDS) - {"symbol", "display_name"}:
            self.assertIsNone(row[field], field)
        self.assertEqual(row["display_name"], "MINIMAL")

    def test_L_quote_turnover_ranking_remains_descending(self) -> None:
        ranked = _service()._rank_markets([
            {"symbol": "LOW", "turnover_24h": "10"},
            {"symbol": "HIGH", "turnover_24h": "100"},
            {"symbol": "MID", "volume_24h_quote": "50"},
        ])
        self.assertEqual([r["symbol"] for r in ranked], ["HIGH", "MID", "LOW"])

    def test_M_unknown_turnover_rows_remain_alphabetical_tail(self) -> None:
        ranked = _service()._rank_markets([
            {"symbol": "ZZZ", "turnover_24h": None, "volume_24h": None},
            {"symbol": "AAA", "turnover_24h": None, "volume_24h": None},
            {"symbol": "KNOWN", "turnover_24h": "1"},
        ])
        self.assertEqual([r["symbol"] for r in ranked], ["KNOWN", "AAA", "ZZZ"])

    def test_N_huge_base_volume_never_outranks_valid_quote_turnover(self) -> None:
        ranked = _service()._rank_markets([
            {"symbol": "BASEONLY", "turnover_24h": None, "volume_24h_quote": None, "volume_24h": None, "volume_24h_base": "1000000000000"},
            {"symbol": "KNOWN", "turnover_24h": "100", "volume_24h_quote": None, "volume_24h_base": None},
        ])
        self.assertEqual([r["symbol"] for r in ranked], ["KNOWN", "BASEONLY"])

    def test_batch_refresh_status_is_preserved_on_rows(self) -> None:
        batch = CanonicalTickersBatch(tickers={"BTC-USDT": CanonicalMarketPrice(**_canonical_row())}, refresh_status="partial")
        rows = _service()._rows_from_tickers_batch({"tickers_batch": batch.to_dict()}, market_type="futures")
        self.assertEqual(rows[0]["ticker_refresh_status"], "partial")


if __name__ == "__main__":
    unittest.main()
