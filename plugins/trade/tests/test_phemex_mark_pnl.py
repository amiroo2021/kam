"""Phemex mark / unrealized PnL + candle normalization tests."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import mock

from plugins.trade.agents import x_phemex_agent as ph
from plugins.trade.trademenu.marketdata import fetch_phemex_candles
from plugins.trade.trademenu.service import TradeMenuService, _derive_mark


def _pos_row(
    *,
    symbol: str = "BTCUSDT",
    side: str = "Buy",
    pos_side: str = "Long",
    size: str = "3.698",
    entry: str = "76975.0",
    mark: str = "76000.0",
    currency: str = "USDT",
    unrealised: str | None = None,
) -> dict:
    row = {
        "symbol": symbol,
        "side": side,
        "posSide": pos_side,
        "size": size,
        "avgEntryPriceRp": entry,
        "avgEntryPrice": entry,
        "markPriceRp": mark,
        "currency": currency,
        "positionStatus": "Normal",
    }
    if unrealised is not None:
        row["unrealisedPnlRv"] = unrealised
    return row


class PhemexNormalizeMarkPnlTests(unittest.TestCase):
    def test_long_loss_uses_mark_and_computed_pnl(self) -> None:
        rows = [_pos_row(mark="76000.0", entry="76975.0", size="2")]
        out = ph._normalize_positions(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].mark, "76000")
        self.assertNotEqual(out[0].mark, out[0].entry_price)
        # (76000-76975)*2 = -1950
        self.assertEqual(Decimal(out[0].pnl), Decimal("-1950"))

    def test_long_profit(self) -> None:
        rows = [_pos_row(mark="78000", entry="77000", size="1")]
        out = ph._normalize_positions(rows)
        self.assertEqual(Decimal(out[0].pnl), Decimal("1000"))
        self.assertEqual(out[0].mark, "78000")

    def test_short_profit(self) -> None:
        rows = [
            _pos_row(
                symbol="ETHUSDT",
                side="Sell",
                pos_side="Short",
                size="0.16",
                entry="2597.6",
                mark="2400",
            )
        ]
        out = ph._normalize_positions(rows)
        self.assertEqual(out[0].side, "short")
        # (2597.6-2400)*0.16
        self.assertAlmostEqual(float(out[0].pnl), (2597.6 - 2400) * 0.16, places=6)
        self.assertEqual(out[0].mark, "2400")

    def test_short_loss(self) -> None:
        rows = [
            _pos_row(
                side="Sell",
                pos_side="Short",
                size="1",
                entry="100",
                mark="110",
            )
        ]
        out = ph._normalize_positions(rows)
        self.assertEqual(Decimal(out[0].pnl), Decimal("-10"))

    def test_authoritative_unrealised_preferred(self) -> None:
        rows = [_pos_row(mark="76000", entry="77000", size="1", unrealised="-123.45")]
        out = ph._normalize_positions(rows)
        self.assertEqual(out[0].pnl, "-123.45")

    def test_missing_mark_does_not_become_entry(self) -> None:
        row = _pos_row(mark="0", entry="76975", size="1")
        row["markPriceRp"] = "0"
        out = ph._normalize_positions([row])
        # mark 0 treated as absent
        self.assertIsNone(out[0].mark)
        # no mark ⇒ no invented zero pnl
        self.assertEqual(out[0].pnl, "")

    def test_missing_pnl_fields_not_zero_without_mark(self) -> None:
        row = {
            "symbol": "BTCUSDT",
            "side": "Buy",
            "posSide": "Long",
            "size": "1",
            "avgEntryPriceRp": "70000",
            "currency": "USDT",
        }
        out = ph._normalize_positions([row])
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0].mark)
        self.assertEqual(out[0].pnl, "")

    def test_zero_size_skipped(self) -> None:
        rows = [_pos_row(size="0", mark="76000")]
        self.assertEqual(ph._normalize_positions(rows), [])

    def test_enrich_preserves_mark_and_tp_sl(self) -> None:
        positions = ph._normalize_positions([_pos_row(mark="76100", entry="77000", size="1")])
        with mock.patch.object(ph, "_fetch_active_orders_for_symbol", return_value=[]):
            out = ph._enrich_positions_with_protections({"account": "x"}, positions)
        self.assertEqual(out[0].mark, "76100")
        self.assertEqual(out[0].entry_price, positions[0].entry_price)


class TradeMenuDeriveMarkTests(unittest.TestCase):
    def test_blank_pnl_does_not_yield_entry_as_mark(self) -> None:
        self.assertIsNone(_derive_mark("long", "76975", "3.698", ""))
        self.assertIsNone(_derive_mark("long", "76975", "3.698", None))
        self.assertIsNone(_derive_mark("long", "76975", "3.698", "—"))

    def test_real_zero_pnl_still_derives_entry_mark(self) -> None:
        # Genuine zero PnL ⇒ mark equals entry (valid).
        m = _derive_mark("long", "76975", "1", "0")
        self.assertEqual(Decimal(m), Decimal("76975"))


class TradeMenuServicePhemexMarkTests(unittest.TestCase):
    def test_service_uses_agent_mark(self) -> None:
        from plugins.trade.canonical import CanonicalPosition, make_success

        class Desk:
            def list_exchanges(self):
                return ["phemex"]

            def list_accounts(self, exchange):
                return ["dramiroo"]

            def capabilities(self, exchange):
                return ["positions_orders"]

            def execute(self, req):
                return make_success(
                    "positions_orders",
                    "phemex",
                    "dramiroo",
                    positions=[
                        CanonicalPosition(
                            symbol="BTC",
                            side="long",
                            size="3.698",
                            entry_price="76975",
                            pnl="-3584.4",
                            mark="76005.9",
                            tp="77500",
                            sl="74500",
                            exchange_instrument="BTCUSDT",
                        )
                    ],
                    order_groups=[],
                    open_order_count=0,
                )

        svc = TradeMenuService(desk=Desk(), cache_ttl=0.01)  # type: ignore[arg-type]
        out = svc.positions("phemex", "dramiroo")
        self.assertTrue(out["success"])
        p = out["positions"][0]
        self.assertEqual(p["mark"], "76005.9")
        self.assertNotEqual(p["mark"], p["entry"])
        self.assertEqual(p["tp"], "77500")
        self.assertEqual(p["sl"], "74500")


class PhemexCandleFetchTests(unittest.TestCase):
    def test_kline_last_btcusdt_rp_prices(self) -> None:
        sample = {
            "code": 0,
            "msg": "OK",
            "data": {
                "rows": [
                    [1700000000, 900, "100", "100", "110", "90", "105", "1.5", "0", "BTCUSDT"],
                    [1700000900, 900, "105", "105", "120", "100", "115", "2", "0", "BTCUSDT"],
                ]
            },
        }
        with mock.patch(
            "plugins.trade.trademenu.marketdata._http_json", return_value=sample
        ):
            candles = fetch_phemex_candles("BTCUSDT", "15m", limit=10)
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[-1]["close"], 115.0)
        self.assertEqual(candles[0]["open"], 100.0)

    def test_empty_raises(self) -> None:
        with mock.patch(
            "plugins.trade.trademenu.marketdata._http_json",
            return_value={"code": 0, "data": {"rows": []}},
        ):
            with self.assertRaises(RuntimeError):
                fetch_phemex_candles("BTCUSDT", "15m", limit=5)

    def test_friendly_btcusd_maps_to_usdt_kline(self) -> None:
        seen = {}

        def fake_http(url, **kwargs):
            seen["url"] = url
            return {
                "code": 0,
                "data": {
                    "rows": [[1700000000, 900, "1", "1", "2", "1", "1.5", "1", "0", "BTCUSDT"]]
                },
            }

        with mock.patch("plugins.trade.trademenu.marketdata._http_json", side_effect=fake_http):
            candles = fetch_phemex_candles("BTCUSD", "15m", limit=3)
        self.assertIn("symbol=BTCUSDT", seen["url"])
        self.assertEqual(len(candles), 1)


if __name__ == "__main__":
    unittest.main()
