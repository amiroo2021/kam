"""Unit tests for Phemex agent (offline mocks)."""

from __future__ import annotations

import os
import unittest
from decimal import Decimal
from unittest import mock

from plugins.trade.agents import x_phemex_agent as phemex


class PhemexAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = os.environ.copy()
        for k in list(os.environ):
            if k.upper().startswith("PHEMEX_"):
                del os.environ[k]
        phemex._PRODUCT_CACHE["ts"] = 0.0
        phemex._PRODUCT_CACHE["by_symbol"] = {}
        phemex._PRODUCT_CACHE["by_base"] = {}

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def _creds_env(self) -> None:
        os.environ["PHEMEX_DRAMIROO_ID"] = "uuid-key"
        os.environ["PHEMEX_DRAMIROO_APISECRET"] = "secret-value"

    def test_list_accounts_requires_id_and_secret(self) -> None:
        self._creds_env()
        os.environ["PHEMEX_HALF_ID"] = "only-id"
        self.assertEqual(phemex.list_accounts(), ["dramiroo"])

    def test_capabilities_include_writes(self) -> None:
        caps = phemex.capabilities()
        for op in ("balance", "positions_orders", "new_order", "cancel_order_group"):
            self.assertIn(op, caps)

    def test_balance_success(self) -> None:
        self._creds_env()

        def fake_fetch(_creds):
            return {
                "account": {
                    "currency": "USDT",
                    "accountBalanceRv": "19273.0714387528",
                    "totalUsedBalanceRv": "10.5",
                    "bonusBalanceRv": "0",
                },
                "positions": [],
            }

        with mock.patch.object(phemex, "_fetch_account_positions", side_effect=fake_fetch):
            resp = phemex.execute(
                {"operation": "balance", "exchange": "phemex", "account": "DRAMIROO"}
            )
        self.assertTrue(resp.success, resp)
        assert resp.balance is not None
        assert resp.portfolio_summary is not None
        self.assertEqual(resp.balance.unit, "USDT")
        self.assertEqual(resp.balance.value, "19273.07")
        self.assertEqual(resp.portfolio_summary.margin_used, "10.50")
        self.assertEqual(resp.portfolio_summary.withdrawable, "19262.57")

    def test_positions_skip_zero_size(self) -> None:
        rows = phemex._normalize_positions(
            [
                {
                    "symbol": "BTCUSDT",
                    "side": "None",
                    "size": "0",
                    "avgEntryPriceRp": "0",
                    "unrealisedPnlRv": "0",
                },
                {
                    "symbol": "ETHUSDT",
                    "side": "Sell",
                    "size": "1.5",
                    "avgEntryPriceRp": "2500.1",
                    "unrealisedPnlRv": "-12.34",
                },
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].symbol, "ETH")
        self.assertEqual(rows[0].side, "short")
        self.assertEqual(rows[0].size, "1.5")

    def test_new_order_posts_limit_with_pos_side(self) -> None:
        self._creds_env()
        meta = {
            "symbol": "BTCUSDT",
            "type": "PerpetualV2",
            "display": "BTC / USDT",
            "tick_size": Decimal("0.1"),
            "qty_step": Decimal("0.001"),
            "min_qty": Decimal("0.001"),
            "min_notional": Decimal("1"),
            "max_qty": Decimal("1000"),
            "min_price": Decimal("0"),
            "max_price": Decimal("0"),
            "status": "Listed",
            "settle": "USDT",
            "qty_precision": 3,
            "price_precision": 1,
        }
        placed = {}

        def fake_signed(creds, method, path, query="", body="", auth=True):
            if method == "POST" and path == "/g-orders":
                placed["body"] = body
                return {
                    "code": 0,
                    "data": {
                        "orderID": "oid-1",
                        "clOrdID": "cid-1",
                        "ordStatus": "Created",
                    },
                }
            return {"code": 0, "data": {"rows": []}}

        with mock.patch.object(phemex, "_resolve_native_symbol", return_value=("BTCUSDT", meta)), \
             mock.patch.object(phemex, "_signed_request", side_effect=fake_signed), \
             mock.patch.object(phemex, "_fetch_active_orders_for_symbol", return_value=[{"orderID": "oid-1", "side": "Buy"}]):
            resp = phemex.execute(
                {
                    "operation": "new_order",
                    "exchange": "phemex",
                    "account": "dramiroo",
                    "symbol": "BTC",
                    "side": "buy",
                    "volume": "0.001",
                    "price": "70000.05",
                }
            )
        self.assertTrue(resp.success, resp)
        assert resp.order is not None
        self.assertEqual(resp.order.exchange_order_id, "oid-1")
        self.assertIn('"posSide":"Long"', placed["body"])
        self.assertIn('"side":"Buy"', placed["body"])
        self.assertEqual(resp.order.submitted_price, "70000.1")

    def test_cancel_order_group(self) -> None:
        self._creds_env()
        meta = {
            "symbol": "BTCUSDT",
            "type": "PerpetualV2",
            "display": "BTC / USDT",
            "tick_size": Decimal("0.1"),
            "qty_step": Decimal("0.001"),
            "min_qty": Decimal("0.001"),
            "min_notional": Decimal("1"),
            "max_qty": Decimal("1000"),
            "min_price": Decimal("0"),
            "max_price": Decimal("0"),
            "status": "Listed",
            "settle": "USDT",
            "qty_precision": 3,
            "price_precision": 1,
        }
        active_before = [
            {"orderID": "a", "side": "Buy", "priceRp": "1", "leavesQtyRq": "0.001", "symbol": "BTCUSDT"},
            {"orderID": "b", "side": "Sell", "priceRp": "2", "leavesQtyRq": "0.001", "symbol": "BTCUSDT"},
        ]
        calls = []

        def fake_signed(creds, method, path, query="", body="", auth=True):
            calls.append((method, path, query))
            return {"code": 0, "data": {}}

        with mock.patch.object(phemex, "_resolve_native_symbol", return_value=("BTCUSDT", meta)), \
             mock.patch.object(
                 phemex,
                 "_fetch_active_orders_for_symbol",
                 side_effect=[active_before, []],
             ), \
             mock.patch.object(phemex, "_signed_request", side_effect=fake_signed), \
             mock.patch("plugins.trade.agents.x_phemex_agent.time.sleep", return_value=None):
            resp = phemex.execute(
                {
                    "operation": "cancel_order_group",
                    "exchange": "phemex",
                    "account": "dramiroo",
                    "symbol": "BTC",
                    "side": "buy",
                }
            )
        self.assertTrue(resp.success, resp)
        assert resp.cancel_group is not None
        self.assertEqual(resp.cancel_group.targeted_order_count, 1)
        self.assertTrue(resp.cancel_group.verified)
        self.assertTrue(any(c[0] == "DELETE" and "posSide=Long" in c[2] for c in calls))

    def test_ladder_prices_and_sizes_uniform(self) -> None:
        prices = phemex._ladder_prices(Decimal("100"), Decimal("90"), 3, Decimal("0.1"))
        self.assertEqual(prices, [Decimal("100.0"), Decimal("95.0"), Decimal("90.0")])
        sizes = phemex._ladder_sizes(Decimal("0.009"), 3, Decimal("0.001"), "uniform", Decimal("0.001"))
        self.assertEqual(sum(sizes), Decimal("0.009"))
        self.assertEqual(len(sizes), 3)
        self.assertTrue(all(s >= Decimal("0.001") for s in sizes))

    def test_ladder_places_children(self) -> None:
        self._creds_env()
        meta = {
            "symbol": "BTCUSDT",
            "type": "PerpetualV2",
            "display": "BTC / USDT",
            "tick_size": Decimal("0.1"),
            "qty_step": Decimal("0.001"),
            "min_qty": Decimal("0.001"),
            "min_notional": Decimal("1"),
            "max_qty": Decimal("1000"),
            "min_price": Decimal("0"),
            "max_price": Decimal("0"),
            "status": "Listed",
            "settle": "USDT",
            "qty_precision": 3,
            "price_precision": 1,
        }
        n = {"i": 0}

        def fake_child(*_a, **_k):
            n["i"] += 1
            return {
                "ok": True,
                "order_id": f"oid-{n['i']}",
                "error": None,
                "price": Decimal("70000"),
                "size": Decimal("0.001"),
                "client_order_id": f"c{n['i']}",
            }

        with mock.patch.object(phemex, "_resolve_native_symbol", return_value=("BTCUSDT", meta)), \
             mock.patch.object(phemex, "_place_limit_child", side_effect=fake_child), \
             mock.patch.object(
                 phemex,
                 "_fetch_active_orders_for_symbol",
                 return_value=[{"orderID": "oid-1"}, {"orderID": "oid-2"}, {"orderID": "oid-3"}],
             ), \
             mock.patch("plugins.trade.agents.x_phemex_agent.time.sleep", return_value=None):
            resp = phemex.execute(
                {
                    "operation": "ladder",
                    "exchange": "phemex",
                    "account": "dramiroo",
                    "symbol": "BTC",
                    "side": "buy",
                    "distribution": "uniform",
                    "order_count": "3",
                    "total_volume": "0.003",
                    "start_price": "70000",
                    "end_price": "69000",
                }
            )
        self.assertTrue(resp.success, resp)
        assert resp.ladder is not None
        self.assertEqual(resp.ladder.submitted_order_count, 3)
        self.assertTrue(resp.ladder.verified)
        self.assertIn("ladder", phemex.capabilities())


if __name__ == "__main__":
    unittest.main()
