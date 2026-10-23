"""Per-side BUY/SELL VWAP aggregation tests for Top instruments.

VWAP rule under test:

    VWAP = SUM(normalized_underlying_quantity * entry_price)
           / SUM(normalized_underlying_quantity)

Every assertion here is about NOT producing a plausible-but-wrong number:
  * VWAP must be built from normalized quantities, never raw MT5 lots.
  * BUY and SELL must be averaged independently; net must never be used as
    the SELL quantity when BUY positions exist.
  * Both sides render as positive gross quantities.
  * A position with a missing quantity or entry price must be excluded and
    reported as incomplete coverage, not silently dropped from the average.
  * Positions with incompatible units must not share a VWAP.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Optional

from plugins.trade import portfolio_report as portfolio
from plugins.trade.portfolio_report import OpenPositionRow, PortfolioSnapshot


def pos(
    symbol: str,
    *,
    side: str = "SELL",
    size: str = "10",
    entry: Optional[str] = "100",
    exchange: str = "a",
    account: str = "1",
    quantity_unit: Optional[str] = None,
    contract_multiplier: Optional[str] = None,
    raw_lots: Optional[str] = None,
    market_type: Optional[str] = None,
) -> OpenPositionRow:
    key = portfolio.normalize_instrument_key(symbol, None)
    return OpenPositionRow(
        exchange=exchange,
        account=account,
        account_label=account,
        symbol=symbol,
        instrument_key=key,
        side=side,
        size=Decimal(size),
        entry_price=None if entry is None else Decimal(entry),
        mark=None,
        pnl=None,
        pnl_unit="USD",
        quantity_unit=quantity_unit,
        contract_multiplier=(
            None if contract_multiplier is None else Decimal(contract_multiplier)
        ),
        market_type=market_type,
        raw_lots=None if raw_lots is None else Decimal(raw_lots),
        identity=f"{exchange}|{account}|{symbol}|{side}|{size}|{entry}",
    )


def snap(rows) -> PortfolioSnapshot:
    return PortfolioSnapshot(fetched_at=0.0, accounts=[], positions=list(rows))


def only(rows) -> dict:
    result = portfolio.net_by_instrument(snap(rows))
    assert len(result) == 1, f"expected one group, got {len(result)}"
    return result[0]


class TestSideVwapMath(unittest.TestCase):
    def test_vwap_is_quantity_weighted_not_average_of_prices(self) -> None:
        # 1 @ 100 and 9 @ 200 -> (1*100 + 9*200)/10 = 190, NOT 150.
        row = only([
            pos("ETH", side="SELL", size="1", entry="100", account="1"),
            pos("ETH", side="SELL", size="9", entry="200", account="2"),
        ])
        self.assertEqual(row["short"], Decimal("10"))
        self.assertEqual(row["sell_vwap"]["vwap"], Decimal("190"))

    def test_sell_and_buy_vwap_are_independent(self) -> None:
        row = only([
            pos("ETH", side="BUY", size="5", entry="200", account="1"),
            pos("ETH", side="SELL", size="5", entry="50", account="2"),
        ])
        self.assertEqual(row["buy_vwap"]["vwap"], Decimal("200"))
        self.assertEqual(row["sell_vwap"]["vwap"], Decimal("50"))
        self.assertEqual(row["net"], Decimal("0"))

    def test_net_is_never_used_as_sell_quantity_when_buy_exists(self) -> None:
        # BUY 10 @ 200, SELL 30 @ 50 -> net = -20.  The SELL side must show 30,
        # not the -20 net.
        row = only([
            pos("ETH", side="BUY", size="10", entry="200", account="1"),
            pos("ETH", side="SELL", size="30", entry="50", account="2"),
        ])
        self.assertEqual(row["long"], Decimal("10"))
        self.assertEqual(row["short"], Decimal("30"))
        self.assertEqual(row["net"], Decimal("-20"))

    def test_both_sides_are_positive_gross(self) -> None:
        row = only([
            pos("ETH", side="BUY", size="7", entry="10", account="1"),
            pos("ETH", side="SELL", size="3", entry="20", account="2"),
        ])
        self.assertGreater(row["long"], 0)
        self.assertGreater(row["short"], 0)
        self.assertEqual(row["gross"], Decimal("10"))


class TestVwapUsesNormalizedQuantities(unittest.TestCase):
    def test_mt5_lot_row_vwap_uses_converted_quantity(self) -> None:
        # 0.44 lots x 100 = 44 HYPE at 85, and 2 HYPE at 95.
        # VWAP = (44*85 + 2*95)/46 = 84.304347...
        row = only([
            pos("HYPEUSD", side="SELL", size="44", entry="85", account="A",
                quantity_unit="HYPE", raw_lots="0.44", contract_multiplier="100"),
            pos("HYPE", side="SELL", size="2", entry="95", account="B",
                quantity_unit="HYPE"),
        ])
        vwap = row["sell_vwap"]["vwap"]
        expected = (Decimal("44") * Decimal("85") + Decimal("2") * Decimal("95")) / Decimal("46")
        self.assertEqual(vwap, expected)
        # A lot-weighted average would be 90; the quantity-weighted one is lower.
        self.assertLess(vwap, Decimal("90"))

    def test_raw_lots_never_enter_the_vwap(self) -> None:
        # Same exposure expressed two ways.  Using raw lots (0.44) instead of
        # the converted quantity (44) would drag the average toward the other
        # leg; the normalized figure must not.
        rows = [
            pos("ZECUSD", side="SELL", size="123.1", entry="1000", account="A",
                quantity_unit="ZEC", raw_lots="12.31", contract_multiplier="10"),
            pos("ZEC", side="SELL", size="0.9", entry="2000", account="B",
                quantity_unit="ZEC"),
        ]
        vwap = only(rows)["sell_vwap"]["vwap"]
        expected = (
            Decimal("123.1") * Decimal("1000") + Decimal("0.9") * Decimal("2000")
        ) / Decimal("124")
        self.assertEqual(vwap, expected)


class TestVwapCoverageHonesty(unittest.TestCase):
    def test_missing_entry_price_is_excluded_and_flagged(self) -> None:
        row = only([
            pos("ETH", side="SELL", size="10", entry="100", account="1"),
            pos("ETH", side="SELL", size="10", entry=None, account="2"),
        ])
        block = row["sell_vwap"]
        self.assertEqual(block["total"], 2)
        self.assertEqual(block["covered"], 1)
        self.assertFalse(block["complete"])
        # Average covers only the priced leg, and is explicitly incomplete.
        self.assertEqual(block["vwap"], Decimal("100"))

    def test_complete_side_reports_complete(self) -> None:
        row = only([pos("ETH", side="SELL", size="10", entry="100", account="1")])
        block = row["sell_vwap"]
        self.assertTrue(block["complete"])
        self.assertEqual(block["covered"], block["total"])

    def test_side_with_no_positions_has_no_vwap(self) -> None:
        row = only([pos("ETH", side="BUY", size="10", entry="100", account="1")])
        self.assertEqual(row["sell_vwap"]["vwap"], None)
        self.assertEqual(row["sell_vwap"]["total"], 0)
        self.assertFalse(row["sell_vwap"]["complete"])
        self.assertIsNotNone(row["buy_vwap"]["vwap"])

    def test_zero_quantity_position_cannot_pollute_average(self) -> None:
        row = only([
            pos("ETH", side="SELL", size="0", entry="999", account="1"),
            pos("ETH", side="SELL", size="10", entry="100", account="2"),
        ])
        block = row["sell_vwap"]
        self.assertEqual(block["vwap"], Decimal("100"))
        self.assertEqual(block["covered"], 1)
        self.assertEqual(block["total"], 2)


class TestVwapGrouping(unittest.TestCase):
    def test_incompatible_units_do_not_share_a_vwap(self) -> None:
        # Verified token row and an unverified lot row must stay in separate
        # groups, each with its own VWAP.
        rows = [
            pos("ETH", side="SELL", size="10", entry="2000", account="1",
                quantity_unit="ETH"),
            pos("ETHUSD", side="SELL", size="1", entry="2000", account="2",
                raw_lots="1"),
        ]
        result = portfolio.net_by_instrument(snap(rows))
        self.assertEqual(len(result), 2)
        for r in result:
            self.assertEqual(r["sell_vwap"]["vwap"], Decimal("2000"))

    def test_account_count_still_reported(self) -> None:
        row = only([
            pos("ETH", side="BUY", size="1", entry="10", account="1"),
            pos("ETH", side="SELL", size="1", entry="20", account="2"),
            pos("ETH", side="SELL", size="1", entry="30", account="3"),
        ])
        self.assertEqual(row["accounts"], 3)
        self.assertEqual(row["sell_vwap"]["vwap"], Decimal("25"))
        self.assertEqual(row["buy_vwap"]["vwap"], Decimal("10"))

    def test_pnl_units_preserved(self) -> None:
        row = only([pos("ETH", side="SELL", size="1", entry="10", account="1")])
        self.assertIn("pnl_units", row)


if __name__ == "__main__":
    unittest.main()