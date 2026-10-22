"""MetaTrader contract-size normalization tests.

Covers Stage 1 (agent read-action passthrough) and Stage 2 (report-side
quantity normalization):

  * PUPrime HYPEUSD 0.44 lots x 100 = 44 HYPE
  * PoTrade  ZECUSD 0.06 lots x 10  = 0.6 ZEC
  * PUPrime  ZECUSD contract size 100 vs LiteFinance 10
  * USC conversion applies to money only, never to quantity
  * missing / invalid contract specifications stay unverified (never 1)
  * unsupported CFD calc modes are not converted
  * lots never merge with tokens across exchanges
"""
import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional

from plugins.trade import portfolio_report as P
from plugins.trade.portfolio_report import (
    AccountSnapshot,
    OpenPositionRow,
    PortfolioSnapshot,
    resolve_contract_spec,
    contract_spec_is_linear,
    underlying_quantity_from_lots,
    net_by_instrument,
)


# ---------------------------------------------------------------- agent stage
class TestAgentPassthrough(unittest.TestCase):
    def test_new_read_actions_dispatch(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt
        for op in ("symbol_info", "account_info", "positions_detail",
                   "orders_detail", "health", "capabilities", "symbol_tick"):
            self.assertIn(op, mt.capabilities(), op)

    def test_calc_actions_advertised(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt
        for op in ("calc_profit", "calc_margin"):
            self.assertIn(op, mt.capabilities(), op)

    def test_existing_actions_preserved(self) -> None:
        from plugins.trade.agents import x_metatrader_agent as mt
        for op in ("balance", "positions_orders", "ticker", "candles",
                   "symbols", "new_order", "set_tp", "set_sl", "ladder"):
            self.assertIn(op, mt.capabilities(), op)

    def test_calc_uses_ea_argument_names(self) -> None:
        from unittest import mock
        from plugins.trade.agents import x_metatrader_agent as mt
        captured: Dict[str, Any] = {}

        def fake_post(payload, timeout_seconds=30):
            captured.update(payload)
            return {"request_id": payload["request_id"], "account": payload["account"],
                    "ok": True, "calculated_profit": 1.0, "currency": "USD"}

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            with mock.patch.dict("os.environ", {
                "MT_TESTACCOUNT_ACCOUNT": "12345", "LINUX_WIN_HOST": "h",
                "LINUX_WIN_PORT": "5050"}, clear=False):
                resp = mt.execute({
                    "operation": "calc_profit", "account": "TESTACCOUNT",
                    "symbol": "HYPEUSD", "side": "SELL", "volume": "0.01",
                    "open_price": "90", "close_price": "80"})
        self.assertTrue(resp.success)
        # The EA's exact names, not direction/entry_price.
        self.assertEqual(captured["side"], "SELL")
        self.assertEqual(captured["open_price"], 90.0)
        self.assertEqual(captured["close_price"], 80.0)
        self.assertEqual(captured["volume"], 0.01)
        self.assertNotIn("entry_price", captured)

    def test_calc_margin_uses_price(self) -> None:
        from unittest import mock
        from plugins.trade.agents import x_metatrader_agent as mt
        captured: Dict[str, Any] = {}

        def fake_post(payload, timeout_seconds=30):
            captured.update(payload)
            return {"request_id": payload["request_id"], "account": payload["account"],
                    "ok": True, "calculated_margin": 3.0, "currency": "USD"}

        with mock.patch.object(mt, "_bridge_post", side_effect=fake_post):
            with mock.patch.dict("os.environ", {
                "MT_TESTACCOUNT_ACCOUNT": "12345", "LINUX_WIN_HOST": "h",
                "LINUX_WIN_PORT": "5050"}, clear=False):
                resp = mt.execute({
                    "operation": "calc_margin", "account": "TESTACCOUNT",
                    "symbol": "ZECUSD", "side": "BUY", "volume": "0.01",
                    "price": "1500"})
        self.assertTrue(resp.success)
        self.assertEqual(captured["price"], 1500.0)
        self.assertNotIn("open_price", captured)

    def test_calc_rejects_bad_arguments_before_transport(self) -> None:
        from unittest import mock
        from plugins.trade.agents import x_metatrader_agent as mt
        with mock.patch.object(mt, "_bridge_post") as post:
            with mock.patch.dict("os.environ", {
                "MT_TESTACCOUNT_ACCOUNT": "12345", "LINUX_WIN_HOST": "h",
                "LINUX_WIN_PORT": "5050"}, clear=False):
                bad_side = mt.execute({
                    "operation": "calc_profit", "account": "TESTACCOUNT",
                    "symbol": "HYPEUSD", "side": "SIDEWAYS", "volume": "1",
                    "open_price": "1", "close_price": "2"})
                bad_vol = mt.execute({
                    "operation": "calc_profit", "account": "TESTACCOUNT",
                    "symbol": "HYPEUSD", "side": "BUY", "volume": "-1",
                    "open_price": "1", "close_price": "2"})
                no_symbol = mt.execute({"operation": "symbol_info",
                                        "account": "TESTACCOUNT"})
        self.assertFalse(bad_side.success)
        self.assertFalse(bad_vol.success)
        self.assertFalse(no_symbol.success)
        post.assert_not_called()


# ------------------------------------------------------------ spec resolution
class _Resp:
    def __init__(self, success: bool, data: Optional[Dict[str, Any]] = None):
        self.success = success
        self.data = data or {}


class _Desk:
    """Stub desk that serves only the operations the resolver may call."""

    def __init__(self, specs: Dict[str, Dict[str, Any]], fail: bool = False):
        self.specs = specs
        self.fail = fail
        self.calls: List[str] = []

    def execute(self, request: Any) -> Any:
        op = str(request.get("operation"))
        self.calls.append(op)
        if op not in ("balance", "positions_orders", "symbol_info"):
            raise AssertionError(f"unexpected operation {op!r}")
        if op == "symbol_info":
            if self.fail:
                return _Resp(False)
            key = f"{request['account']}|{request['symbol']}"
            spec = self.specs.get(key)
            if spec is None:
                return _Resp(False)
            return _Resp(True, spec)
        return _Resp(True, {"positions": [], "balance": None})


PUPRIME_HYPE = {
    "trade_contract_size": 100.0, "trade_calc_mode": 2,
    "currency_base": "USD", "currency_profit": "USD", "currency_margin": "USD",
    "server": "PUPrime-Live2", "company": "PU Prime Ltd",
}
PUPRIME_ZEC = {
    "trade_contract_size": 100.0, "trade_calc_mode": 5,
    "currency_base": "ZEC", "currency_profit": "USD", "currency_margin": "ZEC",
    "server": "PUPrime-Live2", "company": "PU Prime Ltd",
}
POTRADE_ZEC = {
    "trade_contract_size": 10.0, "trade_calc_mode": 2,
    "currency_base": "USD", "currency_profit": "USD", "currency_margin": "USD",
    "server": "PoTrade-MT5", "company": "Po Trade Ltd.",
}
LITE_ZEC = {
    "trade_contract_size": 10.0, "trade_calc_mode": 2,
    "currency_base": "USD", "currency_profit": "USD", "currency_margin": "USD",
    "server": "LiteFinance-MT5-Live", "company": "LiteFinance Global LLC",
}


class TestContractSpecResolution(unittest.TestCase):
    def setUp(self) -> None:
        P._CONTRACT_SPEC_CACHE.clear()

    def test_puprime_hype_contract_size(self) -> None:
        desk = _Desk({"PUPRIME33849951MT5|HYPEUSD": PUPRIME_HYPE})
        spec = resolve_contract_spec(desk, "metatrader", "PUPRIME33849951MT5", "HYPEUSD")
        self.assertEqual(spec["trade_contract_size"], Decimal("100.0"))
        self.assertTrue(contract_spec_is_linear(spec))
        self.assertEqual(spec["server"], "PUPrime-Live2")

    def test_potrade_zec_contract_size(self) -> None:
        desk = _Desk({"PO5701520MT5|ZECUSD": POTRADE_ZEC})
        spec = resolve_contract_spec(desk, "metatrader", "PO5701520MT5", "ZECUSD")
        self.assertEqual(spec["trade_contract_size"], Decimal("10.0"))
        self.assertEqual(spec["server"], "PoTrade-MT5")

    def test_same_symbol_different_brokers_different_sizes(self) -> None:
        desk = _Desk({
            "PUPRIME33849951MT5|ZECUSD": PUPRIME_ZEC,
            "LITE7486706MT5|ZECUSD": LITE_ZEC,
        })
        a = resolve_contract_spec(desk, "metatrader", "PUPRIME33849951MT5", "ZECUSD")
        b = resolve_contract_spec(desk, "metatrader", "LITE7486706MT5", "ZECUSD")
        self.assertEqual(a["trade_contract_size"], Decimal("100.0"))
        self.assertEqual(b["trade_contract_size"], Decimal("10.0"))
        self.assertNotEqual(a["trade_contract_size"], b["trade_contract_size"])
        self.assertNotEqual(a["server"], b["server"])

    def test_lookup_is_per_account_not_global(self) -> None:
        desk = _Desk({"PUPRIME33849951MT5|ZECUSD": PUPRIME_ZEC})
        found = resolve_contract_spec(desk, "metatrader", "PUPRIME33849951MT5", "ZECUSD")
        missing = resolve_contract_spec(desk, "metatrader", "LITE7486706MT5", "ZECUSD")
        self.assertIsNotNone(found)
        self.assertIsNone(missing)

    def test_failed_lookup_returns_none_never_default(self) -> None:
        desk = _Desk({}, fail=True)
        spec = resolve_contract_spec(desk, "metatrader", "PUPRIME33849951MT5", "HYPEUSD")
        self.assertIsNone(spec)
        self.assertFalse(contract_spec_is_linear(spec))
        self.assertIsNone(underlying_quantity_from_lots(Decimal("0.44"), spec))

    def test_invalid_contract_size_is_rejected(self) -> None:
        for bad in ({"trade_contract_size": 0, "trade_calc_mode": 2},
                    {"trade_contract_size": -5, "trade_calc_mode": 2},
                    {"trade_contract_size": None, "trade_calc_mode": 2}):
            desk = _Desk({"A|S": bad})
            spec = resolve_contract_spec(desk, "metatrader", "A", "S")
            self.assertIsNone(spec, bad)

    def test_unknown_calc_mode_is_not_linear(self) -> None:
        desk = _Desk({"A|S": {"trade_contract_size": 100, "trade_calc_mode": None}})
        spec = resolve_contract_spec(desk, "metatrader", "A", "S")
        self.assertFalse(contract_spec_is_linear(spec))
        self.assertIsNone(underlying_quantity_from_lots(Decimal("1"), spec))

    def test_unsupported_cfd_calc_mode_not_converted(self) -> None:
        # Calc modes outside the verified linear set must not be converted.
        desk = _Desk({"A|S": {"trade_contract_size": 1, "trade_calc_mode": 7}})
        spec = resolve_contract_spec(desk, "metatrader", "A", "S")
        self.assertFalse(contract_spec_is_linear(spec))
        self.assertIsNone(underlying_quantity_from_lots(Decimal("1"), spec))


class TestVerifiedConversions(unittest.TestCase):
    def test_puprime_hype_044_lots_is_44_hype(self) -> None:
        spec = dict(PUPRIME_HYPE, trade_contract_size=Decimal("100"))
        self.assertEqual(
            underlying_quantity_from_lots(Decimal("0.44"), spec), Decimal("44.000"))

    def test_potrade_zec_006_lots_is_06_zec(self) -> None:
        spec = dict(POTRADE_ZEC, trade_contract_size=Decimal("10"))
        self.assertEqual(
            underlying_quantity_from_lots(Decimal("0.06"), spec), Decimal("0.600"))

    def test_same_lots_different_brokers_differ(self) -> None:
        a = dict(PUPRIME_ZEC, trade_contract_size=Decimal("100"))
        b = dict(LITE_ZEC, trade_contract_size=Decimal("10"))
        self.assertEqual(underlying_quantity_from_lots(Decimal("1"), a), Decimal("100"))
        self.assertEqual(underlying_quantity_from_lots(Decimal("1"), b), Decimal("10"))

    def test_usc_never_applies_to_quantity(self) -> None:
        # Account currency USC must not scale the underlying quantity; only
        # monetary values convert.  0.44 lots x 100 stays 44, not 4400.
        spec = dict(PUPRIME_HYPE, trade_contract_size=Decimal("100"))
        qty = underlying_quantity_from_lots(Decimal("0.44"), spec)
        self.assertEqual(qty, Decimal("44.000"))
        self.assertNotEqual(qty, Decimal("4400"))

    def test_zero_lots_converts_to_zero(self) -> None:
        spec = dict(PUPRIME_HYPE, trade_contract_size=Decimal("100"))
        self.assertEqual(underlying_quantity_from_lots(Decimal("0"), spec), Decimal("0"))


# ----------------------------------------------------------------- aggregation
def mt_row(symbol: str, key: str, size: str, *, exchange: str = "metatrader",
           account: str = "PUPRIME33849951MT5", side: str = "SELL",
           quantity_unit: Optional[str] = None, multiplier: Optional[str] = None,
           spec_id: Optional[str] = None, raw_lots: Optional[str] = None,
           pnl: Optional[str] = None, pnl_unit: str = "USD") -> OpenPositionRow:
    return OpenPositionRow(
        exchange=exchange, account=account, account_label=account,
        symbol=symbol, instrument_key=key, side=side, size=Decimal(size),
        entry_price=Decimal("100"), mark=None,
        pnl=None if pnl is None else Decimal(pnl), pnl_unit=pnl_unit,
        identity=f"{exchange}|{account}|{symbol}|{side}|{size}",
        quantity_unit=quantity_unit,
        market_type=None,
        contract_multiplier=None if multiplier is None else Decimal(multiplier),
        contract_spec=spec_id,
        raw_lots=None if raw_lots is None else Decimal(raw_lots),
    )


def snap(rows: List[OpenPositionRow]) -> PortfolioSnapshot:
    return PortfolioSnapshot(fetched_at=0.0, accounts=[], positions=rows)


class TestAggregationWithConvertedLots(unittest.TestCase):
    def test_converted_mt5_merges_with_token_venue(self) -> None:
        rows = [
            mt_row("HYPEUSD", "HYPE", "44", quantity_unit="HYPE",
                   multiplier="100", spec_id="PUPrime-Live2", raw_lots="0.44"),
            # Token-denominated venue, no contract provenance.
            OpenPositionRow(exchange="hyperliquid", account="FLEX",
                            account_label="FLEX", symbol="HYPE",
                            instrument_key="HYPE", side="SELL",
                            size=Decimal("100"), entry_price=Decimal("10"),
                            mark=None, pnl=None, pnl_unit="USD",
                            identity="hyperliquid|FLEX|HYPE"),
        ]
        result = net_by_instrument(snap(rows))
        self.assertEqual(len(result), 1, result)
        self.assertEqual(result[0]["gross"], Decimal("144"))
        self.assertEqual(result[0]["accounts"], 2)

    def test_unconverted_lots_never_merge_with_tokens(self) -> None:
        rows = [
            mt_row("HYPEUSD", "HYPE", "0.44", quantity_unit=None,
                   raw_lots="0.44"),   # spec missing -> stays in lots
            OpenPositionRow(exchange="hyperliquid", account="FLEX",
                            account_label="FLEX", symbol="HYPE",
                            instrument_key="HYPE", side="SELL",
                            size=Decimal("100"), entry_price=Decimal("10"),
                            mark=None, pnl=None, pnl_unit="USD",
                            identity="hyperliquid|FLEX|HYPE"),
        ]
        result = net_by_instrument(snap(rows))
        self.assertEqual(len(result), 2, "lots must not sum with tokens")
        gross = sorted(r["gross"] for r in result)
        self.assertEqual(gross, [Decimal("0.44"), Decimal("100")])

    def test_different_brokers_normalize_and_merge(self) -> None:
        rows = [
            mt_row("ZECUSD", "ZEC", "44", account="PUPRIME33849951MT5",
                   quantity_unit="ZEC", multiplier="100",
                   spec_id="PUPrime-Live2", raw_lots="0.44"),
            mt_row("ZECUSD", "ZEC", "10", account="LITE7486706MT5",
                   quantity_unit="ZEC", multiplier="10",
                   spec_id="LiteFinance-MT5-Live", raw_lots="1"),
        ]
        result = net_by_instrument(snap(rows))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["gross"], Decimal("54"))
        self.assertEqual(result[0]["accounts"], 2)

    def test_mixed_brokers_plus_token_venue_all_merge(self) -> None:
        rows = [
            mt_row("ZECUSD", "ZEC", "44", account="PUPRIME33849951MT5",
                   quantity_unit="ZEC", multiplier="100",
                   spec_id="PUPrime-Live2", raw_lots="0.44"),
            mt_row("ZECUSD", "ZEC", "10", account="LITE7486706MT5",
                   quantity_unit="ZEC", multiplier="10",
                   spec_id="LiteFinance-MT5-Live", raw_lots="1"),
            OpenPositionRow(exchange="mexc", account="amiroo",
                            account_label="amiroo", symbol="ZEC",
                            instrument_key="ZEC", side="SELL",
                            size=Decimal("36"), entry_price=Decimal("1500"),
                            mark=None, pnl=None, pnl_unit="USDT",
                            identity="mexc|amiroo|ZEC"),
        ]
        result = net_by_instrument(snap(rows))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["gross"], Decimal("90"))
        self.assertEqual(result[0]["accounts"], 3)

    def test_buy_and_sell_remain_separate(self) -> None:
        rows = [
            mt_row("ZECUSD", "ZEC", "44", side="BUY", quantity_unit="ZEC",
                   multiplier="100", spec_id="PUPrime-Live2", raw_lots="0.44"),
            mt_row("ZECUSD", "ZEC", "10", side="SELL", quantity_unit="ZEC",
                   multiplier="100", spec_id="PUPrime-Live2", raw_lots="0.1"),
        ]
        row = net_by_instrument(snap(rows))[0]
        self.assertEqual(row["long"], Decimal("44"))
        self.assertEqual(row["short"], Decimal("10"))
        self.assertEqual(row["net"], Decimal("34"))
        self.assertEqual(row["gross"], Decimal("54"))

    def test_pnl_stays_currency_separated(self) -> None:
        rows = [
            mt_row("ZECUSD", "ZEC", "44", quantity_unit="ZEC", multiplier="100",
                   spec_id="PUPrime-Live2", raw_lots="0.44",
                   pnl="297.68", pnl_unit="USD"),
            mt_row("ZECUSD", "ZEC", "10", account="PO5701520MT5",
                   quantity_unit="ZEC", multiplier="10",
                   spec_id="PoTrade-MT5", raw_lots="1",
                   pnl="30", pnl_unit="USDT"),
        ]
        row = net_by_instrument(snap(rows))[0]
        self.assertEqual(row["pnl_units"]["USD"], Decimal("297.68"))
        self.assertEqual(row["pnl_units"]["USDT"], Decimal("30"))

    def test_per_account_contributions_visible(self) -> None:
        rows = [
            mt_row("ZECUSD", "ZEC", "44", account="PUPRIME33849951MT5",
                   quantity_unit="ZEC", multiplier="100",
                   spec_id="PUPrime-Live2", raw_lots="0.44"),
            mt_row("ZECUSD", "ZEC", "10", account="LITE7486706MT5",
                   quantity_unit="ZEC", multiplier="10",
                   spec_id="LiteFinance-MT5-Live", raw_lots="1"),
        ]
        self.assertEqual(net_by_instrument(snap(rows))[0]["accounts"], 2)

    def test_labels_unique_when_groups_split(self) -> None:
        rows = [
            mt_row("ZECUSD", "ZEC", "44", quantity_unit="ZEC", multiplier="100",
                   spec_id="PUPrime-Live2", raw_lots="0.44"),
            mt_row("ZECUSD", "ZEC", "0.44", quantity_unit=None, raw_lots="0.44"),
        ]
        labels = [r["display_symbol"] for r in net_by_instrument(snap(rows))]
        self.assertEqual(len(labels), len(set(labels)), labels)


class TestUSCConversionExactlyOnce(unittest.TestCase):
    def test_usc_pnl_converts_once_and_quantity_untouched(self) -> None:
        # PUPrime reports pnl in USC.  Money converts 1:1 to USD by /100;
        # quantity is unaffected.
        lots = Decimal("0.44")
        contract_size = Decimal("100")
        qty = lots * contract_size          # 44 HYPE
        entry, current = Decimal("85.37545455"), Decimal("78.61")
        predicted_usd = qty * (entry - current)
        self.assertEqual(qty, Decimal("44"))
        self.assertAlmostEqual(float(predicted_usd), 297.68, places=2)
        broker_usc = Decimal("29768.0")
        self.assertAlmostEqual(float(predicted_usd * 100), float(broker_usc), places=2)

    def test_usd_account_is_not_converted(self) -> None:
        # PoTrade ZEC: 0.6 ZEC * (1471.45 - 1247.74) = 134.23 USD exactly.
        qty = Decimal("0.06") * Decimal("10")
        predicted = qty * (Decimal("1471.45") - Decimal("1247.74"))
        self.assertEqual(qty, Decimal("0.600"))
        self.assertAlmostEqual(float(predicted), 134.23, places=2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()