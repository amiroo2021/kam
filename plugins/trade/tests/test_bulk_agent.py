from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

bulk = importlib.import_module("plugins.trade.agents.x_bulk_agent")


class TestBulkAgent(unittest.TestCase):
    def test_discovers_complete_env_pairs_only(self) -> None:
        env = {
            "BULK_MAIN_ACCOUNT": "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7",
            "BULK_MAIN_AGENT_PRIVATE_KEY": "secret-main",
            "BULK_INCOMPLETE_ACCOUNT": "5Am6JkEHAjYG1itNWRMGpQrxvY8AaqkXCo1TZvenqVux",
            "BULK_NOPUB_AGENT_PRIVATE_KEY": "secret-only",
        }
        with mock.patch.object(bulk, "_combined_bulk_env", return_value={k: (k, v, "env") for k, v in env.items()}):
            self.assertEqual(bulk.list_accounts(), ["main"])

    def test_unknown_account_fails_without_secret_leak(self) -> None:
        with mock.patch.object(bulk, "_discover_credential_map", return_value={}):
            resp = bulk.execute({"operation": "balance", "exchange": "bulk", "account": "missing"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "UNKNOWN_ACCOUNT")
        self.assertNotIn("PRIVATE", resp.error.message.upper())

    def test_balance_maps_margin_summary(self) -> None:
        creds = {
            "account": "main",
            "account_pubkey": "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7",
            "agent_private_key": "secret-main",
            "base_url": bulk.DEFAULT_API_BASE,
        }
        payload = [{"fullAccount": {"kind": "Master", "margin": {
            "totalBalance": 1234.567,
            "availableBalance": 1000,
            "marginUsed": 12.345,
            "notional": 99.9,
            "unrealizedPnl": 7.89,
        }, "positions": [], "openOrders": []}}]
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_account_query", return_value=payload):
            resp = bulk.execute({"operation": "balance", "exchange": "bulk", "account": "main"})
        self.assertTrue(resp.success)
        self.assertEqual(resp.balance.value, "1234.57")
        self.assertEqual(resp.balance.unit, "USDC")
        self.assertEqual(resp.portfolio_summary.withdrawable, "1000.00")
        self.assertEqual(resp.data["position_count"], 0)
        self.assertNotIn("secret-main", str(resp.to_dict()))

    def test_positions_orders_maps_signed_sizes_and_groups_orders(self) -> None:
        creds = {
            "account": "main",
            "account_pubkey": "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7",
            "agent_private_key": "secret-main",
            "base_url": bulk.DEFAULT_API_BASE,
        }
        payload = [{"fullAccount": {
            "kind": "Master",
            "margin": {"totalBalance": 10, "availableBalance": 8, "marginUsed": 2, "notional": 40},
            "positions": [
                {"symbol": "BTC-USD", "size": -0.25, "price": 100000, "fairPrice": 99000, "unrealizedPnl": 250},
                {"symbol": "ETH-USD", "size": 0, "price": 3000, "fairPrice": 3001, "unrealizedPnl": 0},
            ],
            "openOrders": [
                {"symbol": "BTC-USD", "orderId": "o1", "price": 98000, "size": -0.1, "originalSize": -0.2, "orderType": "limit", "reduceOnly": False},
                {"symbol": "BTC-USD", "orderId": "o2", "price": 97000, "size": -0.3, "originalSize": -0.3, "orderType": "limit", "reduceOnly": False},
                {"symbol": "ETH-USD", "orderId": "o3", "price": 3500, "size": 1.5, "orderType": "takeProfit", "reduceOnly": True},
            ],
        }}]
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_account_query", return_value=payload):
            resp = bulk.execute({"operation": "positions_orders", "exchange": "bulk", "account": "main"})
        self.assertTrue(resp.success)
        self.assertEqual(len(resp.positions), 1)
        self.assertEqual(resp.positions[0].symbol, "BTC")
        self.assertEqual(resp.positions[0].side, "short")
        self.assertEqual(resp.positions[0].size, "0.25")
        self.assertEqual(resp.positions[0].mark, "99000")
        self.assertEqual(resp.open_order_count, 3)
        self.assertEqual(len(resp.order_groups), 2)
        btc = next(g for g in resp.order_groups if g.symbol == "BTC")
        self.assertEqual(btc.side, "sell")
        self.assertEqual(btc.order_count, 2)
        self.assertEqual(btc.total_size, "0.4")
        self.assertEqual(btc.order_ids, ["o1", "o2"])
        eth = next(g for g in resp.order_groups if g.symbol == "ETH")
        self.assertEqual(eth.classification, "take_profit")
        self.assertTrue(eth.reduce_only)

    def test_positions_management_aliases_positions_snapshot(self) -> None:
        creds = {
            "account": "main",
            "account_pubkey": "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7",
            "agent_private_key": "secret-main",
            "base_url": bulk.DEFAULT_API_BASE,
        }
        # zero / long / short / multi covered via table
        cases = [
            ([], 0, None),
            ([{"symbol": "BTC-USD", "size": 1.5, "price": 100, "fairPrice": 101, "unrealizedPnl": 1.5}], 1, ("BTC", "long", "1.5", "100", "101", "1.5")),
            ([{"symbol": "ETH-USD", "size": -2, "price": 3000, "fairPrice": 2900, "unrealizedPnl": 200}], 1, ("ETH", "short", "2", "3000", "2900", "200")),
            (
                [
                    {"symbol": "BTC-USD", "size": 1, "price": 100, "fairPrice": 110, "unrealizedPnl": 10},
                    {"symbol": "SOL-USD", "size": -3, "price": 150, "fairPrice": 140, "unrealizedPnl": 30},
                    {"symbol": "ZERO-USD", "size": 0, "price": 1, "fairPrice": 1, "unrealizedPnl": 0},
                ],
                2,
                None,
            ),
        ]
        for positions, expected_count, single in cases:
            payload = [{"fullAccount": {
                "kind": "Master",
                "margin": {"totalMargin": 10, "transferableBalance": 8, "marginUsed": 2, "notional": 40},
                "positions": positions,
                "openOrders": [],
            }}]
            with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
                 mock.patch.object(bulk, "_account_query", return_value=payload):
                resp = bulk.execute({"operation": "positions_management", "exchange": "bulk", "account": "main"})
            self.assertTrue(resp.success, msg=str(positions))
            self.assertEqual(resp.operation, "positions_management")
            self.assertEqual(len(resp.positions or []), expected_count)
            self.assertNotEqual(getattr(resp.error, "code", None), "NOT_IMPLEMENTED")
            if single:
                p = resp.positions[0]
                self.assertEqual((p.symbol, p.side, p.size, p.entry_price, p.mark, p.pnl), single)

        self.assertIn("positions_management", bulk.capabilities())
        self.assertNotIn("set_tp", bulk.capabilities())
        self.assertNotIn("set_sl", bulk.capabilities())
        self.assertNotIn("close_position", bulk.capabilities())

        # Unsupported mutations stay NOT_IMPLEMENTED (not shown by wizard when caps known).
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds):
            for op in ("set_tp", "set_sl", "close_position"):
                bad = bulk.execute({"operation": op, "exchange": "bulk", "account": "main", "symbol": "BTC", "side": "long", "price": "1"})
                self.assertFalse(bad.success)
                self.assertEqual(bad.error.code, "NOT_IMPLEMENTED")
                self.assertNotIn("read-only adapter", bad.error.message)

    def test_positions_management_attaches_tp_sl_from_open_orders(self) -> None:
        creds = {
            "account": "main",
            "account_pubkey": "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7",
            "agent_private_key": "secret-main",
            "base_url": bulk.DEFAULT_API_BASE,
        }
        payload = [{"fullAccount": {
            "kind": "Master",
            "margin": {"totalMargin": 10},
            "positions": [
                {"symbol": "BTC-USD", "size": 1, "price": 100, "fairPrice": 105, "unrealizedPnl": 5},
            ],
            "openOrders": [
                # protective sell TP on long
                {"symbol": "BTC-USD", "orderId": "tp1", "price": 120, "size": -1, "orderType": "takeProfit", "reduceOnly": True},
                # protective sell SL on long
                {"symbol": "BTC-USD", "orderId": "sl1", "price": 90, "size": -1, "orderType": "stop", "reduceOnly": True},
            ],
        }}]
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_account_query", return_value=payload):
            resp = bulk.execute({"operation": "positions_management", "exchange": "bulk", "account": "main"})
        self.assertTrue(resp.success)
        self.assertEqual(resp.positions[0].tp, "120")
        self.assertEqual(resp.positions[0].sl, "90")
        self.assertEqual(resp.positions[0].tp_count, 1)
        self.assertEqual(resp.positions[0].sl_count, 1)

    def test_instrument_lookup_and_market_price(self) -> None:
        creds = {"account": "main", "account_pubkey": "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7", "agent_private_key": "secret-main", "base_url": bulk.DEFAULT_API_BASE}
        markets = [{"symbol": "BTC-USD", "baseAsset": "BTC", "quoteAsset": "USD", "tickSize": 0.5, "lotSize": 0.001, "minNotional": 10}]
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_exchange_info", return_value=markets), \
             mock.patch.object(bulk, "_ticker", return_value={"markPrice": 101.25}):
            resolved = bulk.execute({"operation": "resolve_instrument", "exchange": "bulk", "account": "main", "symbol": "BTC"})
            price = bulk.execute({"operation": "market_price", "exchange": "bulk", "account": "main", "symbol": "BTC"})
        self.assertTrue(resolved.success)
        self.assertEqual(resolved.instrument.symbol, "BTC-USD")
        self.assertEqual(resolved.data["price"], "101.25")
        self.assertTrue(price.success)
        self.assertEqual(price.market_price.price, "101.25")

    def test_new_order_submits_signed_limit_and_verifies_readback(self) -> None:
        creds = {"account": "main", "account_pubkey": "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7", "agent_private_key": "secret-main", "base_url": bulk.DEFAULT_API_BASE}
        after = {"openOrders": [{"symbol": "BTC-USD", "orderId": "oid1", "size": 1, "price": 100}]}
        market = {"symbol": "BTC-USD", "baseAsset": "BTC", "quoteAsset": "USD", "tickSize": 0.5, "lotSize": 0.001}
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_find_market", return_value=market), \
             mock.patch.object(bulk, "_submit_order", return_value={"status": "ok"}) as submit, \
             mock.patch.object(bulk, "_live_account", return_value=after):
            resp = bulk.execute({
                "operation": "new_order", "exchange": "bulk", "account": "main",
                "symbol": "BTC", "side": "buy", "volume": "1", "price": "100",
            })
        self.assertTrue(resp.success)
        self.assertEqual(resp.order.exchange_order_id, "oid1")
        submit.assert_called_once()
        self.assertEqual(submit.call_args.args[1][0]["l"]["c"], "BTC-USD")

    def test_ladder_submits_multiple_limit_children(self) -> None:
        creds = {"account": "main", "account_pubkey": "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7", "agent_private_key": "secret-main", "base_url": bulk.DEFAULT_API_BASE}
        market = {"symbol": "BTC-USD", "baseAsset": "BTC", "quoteAsset": "USD", "tickSize": 1, "lotSize": 0.1}
        after = {"openOrders": [
            {"symbol": "BTC-USD", "orderId": "o1", "size": 0.5, "price": 100},
            {"symbol": "BTC-USD", "orderId": "o2", "size": 0.5, "price": 101},
        ]}
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_find_market", return_value=market), \
             mock.patch.object(bulk, "_submit_order", return_value={"status": "ok"}) as submit, \
             mock.patch.object(bulk, "_live_account", return_value=after), \
             mock.patch.object(bulk.time, "sleep", return_value=None):
            resp = bulk.execute({"operation": "ladder", "exchange": "bulk", "account": "main", "symbol": "BTC", "side": "buy", "distribution": "uniform", "order_count": "2", "total_volume": "1", "start_price": "100", "end_price": "101"})
        self.assertTrue(resp.success)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(len(submit.call_args.args[1]), 2)
        self.assertEqual(resp.ladder.requested_order_count, 2)
        self.assertEqual(resp.ladder.submitted_order_count, 2)
        self.assertEqual(resp.ladder.batch_count, 1)
        self.assertEqual(resp.data["batch_plan"], [{"index": 0, "size": 2}])

    def test_ladder_splits_into_batches_of_50(self) -> None:
        creds = {"account": "main", "account_pubkey": "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7", "agent_private_key": "secret-main", "base_url": bulk.DEFAULT_API_BASE}
        market = {"symbol": "BTC-USD", "baseAsset": "BTC", "quoteAsset": "USD", "tickSize": 1, "lotSize": 0.001}
        cases = [
            (70, [50, 20]),
            (110, [50, 50, 10]),
            (50, [50]),
            (51, [50, 1]),
        ]
        for order_count, expected_sizes in cases:
            after = {
                "openOrders": [
                    {"symbol": "BTC-USD", "orderId": f"o{i}", "size": 1, "price": 100 + i}
                    for i in range(order_count)
                ]
            }
            with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
                 mock.patch.object(bulk, "_find_market", return_value=market), \
                 mock.patch.object(bulk, "_submit_order", return_value={"status": "ok"}) as submit, \
                 mock.patch.object(bulk, "_live_account", return_value=after), \
                 mock.patch.object(bulk.time, "sleep", return_value=None):
                resp = bulk.execute({
                    "operation": "ladder",
                    "exchange": "bulk",
                    "account": "main",
                    "symbol": "BTC",
                    "side": "buy",
                    "distribution": "uniform",
                    "order_count": str(order_count),
                    "total_volume": str(order_count),
                    "start_price": "100",
                    "end_price": str(100 + order_count - 1),
                })
            self.assertTrue(resp.success, msg=f"count={order_count}")
            self.assertEqual(submit.call_count, len(expected_sizes), msg=f"count={order_count}")
            actual_sizes = [len(call.args[1]) for call in submit.call_args_list]
            self.assertEqual(actual_sizes, expected_sizes, msg=f"count={order_count}")
            self.assertEqual(resp.ladder.batch_count, len(expected_sizes), msg=f"count={order_count}")
            self.assertEqual(resp.data["batch_size"], 50)
            self.assertEqual(
                resp.data["batch_plan"],
                [{"index": i, "size": size} for i, size in enumerate(expected_sizes)],
                msg=f"count={order_count}",
            )
            self.assertEqual(resp.ladder.accepted_child_count, order_count, msg=f"count={order_count}")

    def test_ladder_rate_limit_on_batch_is_explicit(self) -> None:
        creds = {"account": "main", "account_pubkey": "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7", "agent_private_key": "secret-main", "base_url": bulk.DEFAULT_API_BASE}
        market = {"symbol": "BTC-USD", "baseAsset": "BTC", "quoteAsset": "USD", "tickSize": 1, "lotSize": 0.1}
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_find_market", return_value=market), \
             mock.patch.object(bulk, "_submit_order", side_effect=RuntimeError("HTTP 429: HTTP Error 429: Too Many Requests")), \
             mock.patch.object(bulk.time, "sleep", return_value=None):
            resp = bulk.execute({"operation": "ladder", "exchange": "bulk", "account": "main", "symbol": "BTC", "side": "buy", "distribution": "uniform", "order_count": "2", "total_volume": "1", "start_price": "100", "end_price": "101"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "BULK_RATE_LIMITED")
        self.assertTrue(resp.ladder.rate_limited)

    def test_ladder_stops_after_partial_rate_limit(self) -> None:
        creds = {"account": "main", "account_pubkey": "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7", "agent_private_key": "secret-main", "base_url": bulk.DEFAULT_API_BASE}
        market = {"symbol": "BTC-USD", "baseAsset": "BTC", "quoteAsset": "USD", "tickSize": 1, "lotSize": 0.001}
        after = {"openOrders": [{"symbol": "BTC-USD", "orderId": f"o{i}", "size": 1, "price": 100 + i} for i in range(50)]}
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_find_market", return_value=market), \
             mock.patch.object(
                 bulk,
                 "_submit_order",
                 side_effect=[{"status": "ok"}, RuntimeError("HTTP 429: HTTP Error 429: Too Many Requests")],
             ) as submit, \
             mock.patch.object(bulk, "_live_account", return_value=after), \
             mock.patch.object(bulk.time, "sleep", return_value=None):
            resp = bulk.execute({
                "operation": "ladder",
                "exchange": "bulk",
                "account": "main",
                "symbol": "BTC",
                "side": "buy",
                "distribution": "uniform",
                "order_count": "70",
                "total_volume": "70",
                "start_price": "100",
                "end_price": "169",
            })
        self.assertTrue(resp.success)
        self.assertEqual(submit.call_count, 2)
        self.assertEqual([len(c.args[1]) for c in submit.call_args_list], [50, 20])
        self.assertEqual(resp.ladder.accepted_child_count, 50)
        self.assertEqual(resp.ladder.status, "partial")
        self.assertTrue(resp.ladder.rate_limited)
        self.assertEqual(resp.data["failed"], 20)

    def test_ladder_verification_rate_limit_returns_submitted(self) -> None:
        creds = {"account": "main", "account_pubkey": "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7", "agent_private_key": "secret-main", "base_url": bulk.DEFAULT_API_BASE}
        market = {"symbol": "BTC-USD", "baseAsset": "BTC", "quoteAsset": "USD", "tickSize": 1, "lotSize": 0.1}
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_find_market", return_value=market), \
             mock.patch.object(bulk, "_submit_order", return_value={"status": "ok"}), \
             mock.patch.object(bulk, "_live_account", side_effect=RuntimeError("HTTP 429: HTTP Error 429: Too Many Requests")), \
             mock.patch.object(bulk.time, "sleep", return_value=None):
            resp = bulk.execute({"operation": "ladder", "exchange": "bulk", "account": "main", "symbol": "BTC", "side": "buy", "distribution": "uniform", "order_count": "2", "total_volume": "1", "start_price": "100", "end_price": "101"})
        self.assertTrue(resp.success)
        self.assertEqual(resp.ladder.status, "submitted")
        self.assertFalse(resp.ladder.verified)
        self.assertTrue(resp.data["verification_delayed"])

    def test_cancel_group_submits_target_order_ids(self) -> None:
        creds = {"account": "main", "account_pubkey": "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7", "agent_private_key": "secret-main", "base_url": bulk.DEFAULT_API_BASE}
        market = {"symbol": "BTC-USD", "baseAsset": "BTC", "quoteAsset": "USD", "tickSize": 0.5, "lotSize": 0.001}
        before = {"openOrders": [{"symbol": "BTC-USD", "orderId": "oid1", "size": 1, "price": 100}]}
        after = {"openOrders": []}
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_find_market", return_value=market), \
             mock.patch.object(bulk, "_live_account", side_effect=[before, after]), \
             mock.patch.object(bulk, "_submit_order", return_value={"status": "ok"}) as submit, \
             mock.patch.object(bulk.time, "sleep", return_value=None):
            resp = bulk.execute({"operation": "cancel_order_group", "exchange": "bulk", "account": "main", "symbol": "BTC", "side": "buy"})
        self.assertTrue(resp.success)
        self.assertEqual(resp.cancel_group.cancelled_order_count, 1)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(submit.call_args.args[1][0]["cx"]["oid"], "oid1")
        self.assertEqual(resp.cancel_group.batch_count, 1)

    def test_cancel_group_batches_of_50(self) -> None:
        creds = {"account": "main", "account_pubkey": "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7", "agent_private_key": "secret-main", "base_url": bulk.DEFAULT_API_BASE}
        market = {"symbol": "BTC-USD", "baseAsset": "BTC", "quoteAsset": "USD", "tickSize": 0.5, "lotSize": 0.001}
        before = {
            "openOrders": [
                {"symbol": "BTC-USD", "orderId": f"oid{i}", "size": 1, "price": 100 + i}
                for i in range(72)
            ]
        }
        after = {"openOrders": []}
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_find_market", return_value=market), \
             mock.patch.object(bulk, "_live_account", side_effect=[before, after]), \
             mock.patch.object(bulk, "_submit_order", return_value={"status": "ok"}) as submit, \
             mock.patch.object(bulk.time, "sleep", return_value=None):
            resp = bulk.execute({"operation": "cancel_order_group", "exchange": "bulk", "account": "main", "symbol": "BTC", "side": "buy"})
        self.assertTrue(resp.success)
        self.assertEqual(submit.call_count, 2)
        self.assertEqual([len(c.args[1]) for c in submit.call_args_list], [50, 22])
        self.assertEqual(resp.cancel_group.batch_count, 2)
        self.assertEqual(resp.cancel_group.cancelled_order_count, 72)
        self.assertEqual(resp.data["batch_plan"], [{"index": 0, "size": 50}, {"index": 1, "size": 22}])
        # side-filtered cx, not symbol-wide cxa
        self.assertIn("cx", submit.call_args_list[0].args[1][0])
        self.assertNotIn("cxa", submit.call_args_list[0].args[1][0])

    def test_cancel_group_rate_limit_is_explicit(self) -> None:
        creds = {"account": "main", "account_pubkey": "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7", "agent_private_key": "secret-main", "base_url": bulk.DEFAULT_API_BASE}
        market = {"symbol": "BTC-USD", "baseAsset": "BTC", "quoteAsset": "USD", "tickSize": 0.5, "lotSize": 0.001}
        before = {"openOrders": [{"symbol": "BTC-USD", "orderId": "oid1", "size": 1, "price": 100}]}
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_find_market", return_value=market), \
             mock.patch.object(bulk, "_live_account", return_value=before), \
             mock.patch.object(bulk, "_submit_order", side_effect=RuntimeError("HTTP 429: HTTP Error 429: Too Many Requests")), \
             mock.patch.object(bulk.time, "sleep", return_value=None):
            resp = bulk.execute({"operation": "cancel_order_group", "exchange": "bulk", "account": "main", "symbol": "BTC", "side": "buy"})
        self.assertFalse(resp.success)
        self.assertEqual(resp.error.code, "BULK_RATE_LIMITED")
        self.assertTrue(resp.cancel_group.rate_limited)

    def test_cancel_group_partial_after_second_batch_rate_limit(self) -> None:
        creds = {"account": "main", "account_pubkey": "FuueqefENiGEW6uMqZQgmwjzgpnb85EgUcZa5Em4PQh7", "agent_private_key": "secret-main", "base_url": bulk.DEFAULT_API_BASE}
        market = {"symbol": "BTC-USD", "baseAsset": "BTC", "quoteAsset": "USD", "tickSize": 0.5, "lotSize": 0.001}
        before = {
            "openOrders": [
                {"symbol": "BTC-USD", "orderId": f"oid{i}", "size": 1, "price": 100 + i}
                for i in range(72)
            ]
        }
        after = {
            "openOrders": [
                {"symbol": "BTC-USD", "orderId": f"oid{i}", "size": 1, "price": 100 + i}
                for i in range(50, 72)
            ]
        }
        with mock.patch.object(bulk, "_lookup_credentials", return_value=creds), \
             mock.patch.object(bulk, "_find_market", return_value=market), \
             mock.patch.object(bulk, "_live_account", side_effect=[before, after]), \
             mock.patch.object(
                 bulk,
                 "_submit_order",
                 side_effect=[{"status": "ok"}, RuntimeError("HTTP 429: HTTP Error 429: Too Many Requests")],
             ) as submit, \
             mock.patch.object(bulk.time, "sleep", return_value=None):
            resp = bulk.execute({"operation": "cancel_order_group", "exchange": "bulk", "account": "main", "symbol": "BTC", "side": "buy"})
        self.assertTrue(resp.success)
        self.assertEqual(submit.call_count, 2)
        self.assertEqual([len(c.args[1]) for c in submit.call_args_list], [50, 22])
        self.assertEqual(resp.cancel_group.cancelled_order_count, 50)
        self.assertEqual(resp.cancel_group.remaining_target_count, 22)
        self.assertEqual(resp.cancel_group.status, "partial")
        self.assertTrue(resp.cancel_group.rate_limited)


if __name__ == "__main__":
    unittest.main()
