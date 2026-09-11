"""Unit tests for Nado agent (offline)."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from plugins.trade.agents import x_nado_agent as nado


class NadoAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = os.environ.copy()
        for k in list(os.environ):
            if k.upper().startswith("NADO_"):
                del os.environ[k]
        nado._symbols_cache.update({"ts": 0.0, "by_symbol": {}, "by_pid": {}})
        nado._contracts_cache.update({"ts": 0.0, "data": None})

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def test_list_accounts_requires_owner(self) -> None:
        os.environ["NADO_BITGET_SUBACCOUNT_OWNER"] = "0x" + "ab" * 20
        os.environ["NADO_BITGET_PRIVATE_KEY"] = "0x" + "cd" * 32
        os.environ["NADO_HALF_PRIVATE_KEY"] = "0x" + "11" * 32
        self.assertEqual(nado.list_accounts(), ["bitget"])

    def test_subaccount_bytes32_default(self) -> None:
        owner = "0x7a5ec2748e9065794491a8d29dcf3f9edb8d7c43"
        got = nado._subaccount_bytes32(owner, "default")
        self.assertEqual(
            got,
            "0x7a5ec2748e9065794491a8d29dcf3f9edb8d7c4364656661756c740000000000",
        )

    def test_capabilities_include_write_ops(self) -> None:
        caps = nado.capabilities()
        for op in ("balance", "positions_orders", "new_order", "ladder", "cancel_order_group"):
            self.assertIn(op, caps)

    def test_ladder_math(self) -> None:
        from decimal import Decimal

        prices = nado._ladder_prices(Decimal("100"), Decimal("90"), 3, Decimal("1"))
        self.assertEqual(prices, [Decimal("100"), Decimal("95"), Decimal("90")])
        sizes = nado._ladder_sizes(Decimal("0.03"), 3, Decimal("0.01"), "uniform", Decimal("0"))
        self.assertEqual(sum(sizes), Decimal("0.03"))

    def test_balance_success(self) -> None:
        os.environ["NADO_BITGET_SUBACCOUNT_OWNER"] = "0x" + "ab" * 20
        os.environ["NADO_BITGET_SUBACCOUNT_NAME"] = "default"

        def fake_query(_creds, payload):
            self.assertEqual(payload["type"], "subaccount_info")
            return {
                "status": "success",
                "data": {
                    "exists": True,
                    "healths": [
                        {
                            "assets": str(int(1500.25 * 1e18)),
                            "liabilities": "0",
                            "health": str(int(1500.25 * 1e18)),
                        }
                    ],
                    "spot_balances": [
                        {
                            "product_id": 0,
                            "balance": {"amount": "1200000000000000000000"},
                        }
                    ],
                    "perp_balances": [],
                    "spot_count": 1,
                    "perp_count": 0,
                },
            }

        with mock.patch.object(nado, "_gateway_query", side_effect=fake_query):
            resp = nado.execute(
                {"operation": "balance", "exchange": "nado", "account": "BITGET"}
            )
        self.assertTrue(resp.success, resp)
        assert resp.balance is not None
        assert resp.portfolio_summary is not None
        self.assertEqual(resp.balance.unit, "USDT0")
        self.assertEqual(resp.balance.value, "1500.25")
        self.assertEqual(resp.portfolio_summary.withdrawable, "1200.00")

    def test_fetch_open_orders_product_orders_shape(self) -> None:
        creds = {
            "account": "bitget",
            "owner": "0x" + "ab" * 20,
            "private_key": "",
            "subaccount_name": "default",
            "gateway_url": "https://api.prod.nado.xyz/gateway/v1",
        }

        def fake_query(_c, payload):
            self.assertEqual(payload["type"], "orders")
            return {
                "status": "success",
                "data": {
                    "sender": "0xabc",
                    "product_orders": [
                        {
                            "product_id": 2,
                            "orders": [
                                {
                                    "product_id": 2,
                                    "amount": "1000000000000000000",
                                    "unfilled_amount": "1000000000000000000",
                                    "price_x18": "50000000000000000000000",
                                    "digest": "0xdead",
                                }
                            ],
                        }
                    ],
                },
            }

        with mock.patch.object(nado, "_gateway_query", side_effect=fake_query):
            rows = nado._fetch_open_orders(creds, product_ids=[2])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["digest"], "0xdead")

    def test_not_implemented(self) -> None:
        os.environ["NADO_BITGET_SUBACCOUNT_OWNER"] = "0x" + "ab" * 20
        resp = nado.execute(
            {"operation": "set_tp", "exchange": "nado", "account": "bitget"}
        )
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "NOT_IMPLEMENTED")


if __name__ == "__main__":
    unittest.main()
