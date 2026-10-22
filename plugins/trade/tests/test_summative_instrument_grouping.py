"""Instrument-grouping regression tests.

Covers the duplicate-display defect and the provenance/compatibility rules:

  * HYPE / ZEC reported with a contract-address exchange identifier merge
    into the canonical symbol group.
  * Equivalent symbols across exchanges share one row.
  * Different contract multipliers, different quantity units, and spot vs
    perp stay separate AND get distinguishable labels.
  * Missing provenance does not fabricate equivalence.
  * SPY vs SPY-USD normalizes consistently.
  * No two emitted rows ever carry an identical display label.
  * BUY/SELL separation, gross, account dedup and per-currency PnL survive.
"""
import unittest
from decimal import Decimal
from typing import List, Optional

from plugins.trade import portfolio_report as portfolio
from plugins.trade.portfolio_report import OpenPositionRow, PortfolioSnapshot

HYPE_ADDR = "0x98accf0e005744bebabb894f17972ab0b2ae0b415452f31a1ba7e4548edba43c"
ZEC_ADDR = "0x435987c9e1b8f61a4cdfa220751b3324ebf5a7bfd1250b25c79412a71a20392f"


def pos(
    symbol: str,
    *,
    exchange: str = "ex",
    account: str = "main",
    size: str = "1",
    side: str = "SELL",
    pnl: Optional[str] = None,
    pnl_unit: str = "USD",
    exchange_instrument: Optional[str] = None,
    quantity_unit: Optional[str] = None,
    market_type: Optional[str] = None,
    contract_multiplier: Optional[str] = None,
    contract_spec: Optional[str] = None,
    raw_lots: Optional[str] = None,
) -> OpenPositionRow:
    key = portfolio.normalize_instrument_key(symbol, exchange_instrument)
    ident = f"{exchange}|{account}|{symbol}|{side}|{size}"
    return OpenPositionRow(
        exchange=exchange,
        account=account,
        account_label=account,
        symbol=symbol,
        instrument_key=key,
        side=side,
        size=Decimal(size),
        entry_price=Decimal("100"),
        mark=Decimal("110"),
        pnl=None if pnl is None else Decimal(pnl),
        pnl_unit=pnl_unit,
        exchange_instrument=exchange_instrument,
        quantity_unit=quantity_unit,
        market_type=market_type,
        contract_multiplier=None if contract_multiplier is None else Decimal(contract_multiplier),
        contract_spec=contract_spec,
        raw_lots=None if raw_lots is None else Decimal(raw_lots),
        identity=ident,
    )


def snap(rows: List[OpenPositionRow]) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        fetched_at=0.0,
        accounts=[],
        positions=rows,
        accounts_seen=0,
    )


class TestContractAddressNormalization(unittest.TestCase):
    def test_address_identifier_is_opaque(self) -> None:
        self.assertTrue(portfolio.is_opaque_exchange_identifier(HYPE_ADDR))
        self.assertTrue(portfolio.is_opaque_exchange_identifier(ZEC_ADDR))

    def test_ticker_is_not_opaque(self) -> None:
        for value in ("HYPE", "HYPE-USD", "PERP_ZEC_USDC", "SPY-USD.P", ""):
            self.assertFalse(portfolio.is_opaque_exchange_identifier(value), value)

    def test_hype_with_contract_address_uses_symbol(self) -> None:
        self.assertEqual(
            portfolio.normalize_instrument_key("HYPE", HYPE_ADDR), "HYPE"
        )

    def test_zec_with_contract_address_uses_symbol(self) -> None:
        self.assertEqual(portfolio.normalize_instrument_key("ZEC", ZEC_ADDR), "ZEC")

    def test_address_without_symbol_keeps_own_group(self) -> None:
        key = portfolio.normalize_instrument_key("", HYPE_ADDR)
        self.assertEqual(key, HYPE_ADDR.upper())
        self.assertNotEqual(key, "HYPE")

    def test_spy_variants_normalize_consistently(self) -> None:
        for sym, ei in (("SPY", None), ("SPY-USD", None), ("SPY", "SPY-USD"),
                        ("SPY-USD.P", None), ("spy", "SPY-USD.P")):
            self.assertEqual(portfolio.normalize_instrument_key(sym, ei), "SPY", (sym, ei))


class TestDuplicateDisplayMerge(unittest.TestCase):
    def test_hype_address_and_ticker_merge_into_one_row(self) -> None:
        rows = [
            pos("HYPE", exchange="bulk", account="a", size="1000", exchange_instrument="HYPE-USD"),
            pos("HYPE", exchange="aftermath", account="b", size="88.27",
                exchange_instrument=HYPE_ADDR),
        ]
        result = portfolio.net_by_instrument(snap(rows))
        self.assertEqual(len(result), 1, "HYPE must be a single row")
        row = result[0]
        self.assertEqual(row["instrument"], "HYPE")
        self.assertEqual(row["display_symbol"], "HYPE")
        self.assertEqual(row["gross"], Decimal("1088.27"))
        self.assertEqual(row["net"], Decimal("-1088.27"))
        self.assertEqual(row["accounts"], 2)

    def test_zec_address_and_ticker_merge_into_one_row(self) -> None:
        rows = [
            pos("ZEC", exchange="bulk", account="a", size="500", exchange_instrument="ZEC-USD"),
            pos("ZEC", exchange="aftermath", account="b", size="19.41",
                exchange_instrument=ZEC_ADDR),
        ]
        result = portfolio.net_by_instrument(snap(rows))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["gross"], Decimal("519.41"))
        self.assertEqual(result[0]["accounts"], 2)

    def test_equivalent_symbols_across_exchanges_share_one_row(self) -> None:
        rows = [
            pos("HYPE", exchange="bulk", account="a", size="10", exchange_instrument="HYPE-USD"),
            pos("HYPE", exchange="mexc", account="b", size="5", exchange_instrument="HYPE_USDC"),
            pos("HYPE", exchange="raydium", account="c", size="2",
                exchange_instrument="PERP_HYPE_USDC"),
            pos("HYPE", exchange="rise", account="d", size="3", exchange_instrument="HYPE"),
        ]
        result = portfolio.net_by_instrument(snap(rows))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["gross"], Decimal("20"))
        self.assertEqual(result[0]["accounts"], 4)

    def test_address_only_instrument_does_not_merge_with_ticker(self) -> None:
        # No canonical symbol -> must stay its own group, not fold into HYPE.
        rows = [
            pos("HYPE", exchange="bulk", account="a", size="10"),
            pos("", exchange="aftermath", account="b", size="5", exchange_instrument=HYPE_ADDR),
        ]
        rows[1] = OpenPositionRow(**{**rows[1].to_dict(),
                                     "size": Decimal("5"),
                                     "instrument_key": HYPE_ADDR.upper(),
                                     "symbol": HYPE_ADDR})
        result = portfolio.net_by_instrument(snap(rows))
        self.assertEqual(len(result), 2)
        keys = {r["instrument"] for r in result}
        self.assertEqual(keys, {"HYPE", HYPE_ADDR.upper()})
        labels = [r["display_symbol"] for r in result]
        self.assertEqual(len(labels), len(set(labels)), "labels must be unique")


class TestIncompatibleContractsStaySeparate(unittest.TestCase):
    def test_different_contract_multipliers_merge_after_normalization(self) -> None:
        # A contract multiplier is the INPUT to converting lots into an
        # underlying quantity, not a property of the resulting amount.  Once
        # both rows are normalized to ETH tokens they are summable.
        rows = [
            pos("ETH", exchange="a", size="10", contract_multiplier="1",
                quantity_unit="ETH", market_type=None, raw_lots="10"),
            pos("ETH", exchange="b", size="10", contract_multiplier="0.01",
                quantity_unit="ETH", market_type=None, raw_lots="1000"),
        ]
        result = portfolio.net_by_instrument(snap(rows))
        self.assertEqual(len(result), 1, "normalized underlying quantities merge")
        self.assertEqual(result[0]["gross"], Decimal("20"))
        self.assertEqual(result[0]["accounts"], 2)

    def test_unverified_lot_rows_never_merge_with_tokens(self) -> None:
        # No verified spec -> stays in its own unit, never summed with tokens
        # and never defaulted to a multiplier of 1.
        rows = [
            pos("ETH", exchange="a", size="10", quantity_unit="ETH",
                market_type=None, raw_lots="10"),
            pos("ETH", exchange="b", size="10", raw_lots="10"),
        ]
        result = portfolio.net_by_instrument(snap(rows))
        self.assertEqual(len(result), 2, "unverified lots must stay separate")
        self.assertEqual(sorted(r["gross"] for r in result),
                         [Decimal("10"), Decimal("10")])

    def test_different_quantity_units_stay_separate(self) -> None:
        rows = [
            pos("BTC", exchange="a", size="1", quantity_unit="BTC"),
            pos("BTC", exchange="b", size="1", quantity_unit="BTCUSDT"),
        ]
        result = portfolio.net_by_instrument(snap(rows))
        self.assertEqual(len(result), 2, "mismatched quantity units must not merge")
        labels = [r["display_symbol"] for r in result]
        self.assertEqual(len(labels), len(set(labels)))
        # Net must not be summed across the two denominations.
        nets = sorted(r["net"] for r in result)
        self.assertEqual(nets, [Decimal("-1"), Decimal("-1")])

    def test_spot_vs_perp_stays_separate(self) -> None:
        rows = [
            pos("SOL", exchange="a", size="5", market_type="perp", quantity_unit="SOL"),
            pos("SOL", exchange="b", size="7", market_type="spot", quantity_unit="SOL"),
        ]
        result = portfolio.net_by_instrument(snap(rows))
        self.assertEqual(len(result), 2, "spot and perp must not merge")
        labels = [r["display_symbol"] for r in result]
        self.assertEqual(len(labels), len(set(labels)))
        self.assertTrue(any("perp" in lb for lb in labels), labels)
        self.assertTrue(any("spot" in lb for lb in labels), labels)

    def test_compatibility_helper_rejects_mismatch(self) -> None:
        rows = [
            pos("ETH", market_type="perp"),
            pos("ETH", market_type="spot"),
        ]
        self.assertFalse(portfolio.positions_are_quantity_compatible(rows))

    def test_compatibility_helper_accepts_match(self) -> None:
        rows = [
            pos("ETH", market_type="perp", quantity_unit="ETH", contract_multiplier="1"),
            pos("ETH", market_type="perp", quantity_unit="ETH", contract_multiplier="1"),
        ]
        self.assertTrue(portfolio.positions_are_quantity_compatible(rows))

    def test_unknown_metadata_is_not_fabricated_equivalence(self) -> None:
        # Verified unit vs missing unit must NOT be treated as compatible.
        rows = [
            pos("ETH", exchange="a", quantity_unit="ETH", market_type="perp"),
            pos("ETH", exchange="b", quantity_unit=None, market_type=None),
        ]
        result = portfolio.net_by_instrument(snap(rows))
        self.assertEqual(len(result), 2, "unknown must not merge with verified")
        labels = [r["display_symbol"] for r in result]
        self.assertEqual(len(labels), len(set(labels)))

    def test_all_unknown_still_aggregates(self) -> None:
        # No provenance anywhere: nothing contradicts, so keep one row.
        rows = [
            pos("ETH", exchange="a", account="1", size="3"),
            pos("ETH", exchange="b", account="2", size="4"),
        ]
        result = portfolio.net_by_instrument(snap(rows))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["gross"], Decimal("7"))


class TestNoIndistinguishableLabels(unittest.TestCase):
    def test_duplicate_display_labels_never_emitted(self) -> None:
        rows = [
            pos("HYPE", exchange="a", account="1", size="10", exchange_instrument=HYPE_ADDR),
            pos("HYPE", exchange="b", account="2", size="5",
                market_type="spot", quantity_unit="HYPE"),
            pos("HYPE", exchange="c", account="3", size="7",
                market_type="perp", quantity_unit="HYPE"),
            pos("ZEC", exchange="d", account="4", size="2"),
            pos("ZEC", exchange="e", account="5", size="3", exchange_instrument=ZEC_ADDR),
        ]
        result = portfolio.net_by_instrument(snap(rows))
        labels = [r["display_symbol"] for r in result]
        self.assertEqual(len(labels), len(set(labels)), f"duplicate labels: {labels}")

    def test_same_ticker_incompatible_keys_are_distinguishable(self) -> None:
        rows = [
            pos("XRP", exchange="a", size="10", market_type="perp", quantity_unit="XRP"),
            pos("XRP", exchange="b", size="4", market_type="spot", quantity_unit="XRP"),
        ]
        result = portfolio.net_by_instrument(snap(rows))
        self.assertEqual(len(result), 2)
        a, b = sorted(r["display_symbol"] for r in result)
        self.assertNotEqual(a, b)


class TestAggregationInvariantsPreserved(unittest.TestCase):
    def test_long_and_short_stay_separate(self) -> None:
        rows = [
            pos("ETH", exchange="a", account="1", size="10", side="BUY"),
            pos("ETH", exchange="b", account="2", size="4", side="SELL"),
        ]
        row = portfolio.net_by_instrument(snap(rows))[0]
        self.assertEqual(row["long"], Decimal("10"))
        self.assertEqual(row["short"], Decimal("4"))
        self.assertEqual(row["net"], Decimal("6"))
        self.assertEqual(row["gross"], Decimal("14"))

    def test_account_dedup_preserved(self) -> None:
        rows = [
            pos("ETH", exchange="ex", account="dup", size="5", exchange_instrument="ETH-USD"),
            pos("ETH", exchange="ex", account="dup", size="5", exchange_instrument="ETH-USD"),
        ]
        row = portfolio.net_by_instrument(snap(rows))[0]
        self.assertEqual(row["accounts"], 1, "same exchange|account counts once")

    def test_pnl_stays_separated_by_currency(self) -> None:
        rows = [
            pos("ETH", exchange="a", account="1", size="1", pnl="10", pnl_unit="USD"),
            pos("ETH", exchange="b", account="2", size="1", pnl="20", pnl_unit="USDT"),
        ]
        row = portfolio.net_by_instrument(snap(rows))[0]
        self.assertEqual(row["pnl_units"]["USD"], Decimal("10"))
        self.assertEqual(row["pnl_units"]["USDT"], Decimal("20"))
        self.assertEqual(len(row["pnl_units"]), 2, "currencies must not combine")

    def test_net_only_from_compatible_rows(self) -> None:
        rows = [
            pos("ETH", exchange="a", size="10", market_type="perp", quantity_unit="ETH"),
            pos("ETH", exchange="b", size="99", market_type="spot", quantity_unit="ETH"),
        ]
        result = portfolio.net_by_instrument(snap(rows))
        nets = sorted(r["net"] for r in result)
        self.assertEqual(nets, sorted([Decimal("-10"), Decimal("-99")]),
                         "incompatible groups must not net together")
        self.assertNotIn(Decimal("-109"), nets,
                         "the two groups must never sum into one net")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()