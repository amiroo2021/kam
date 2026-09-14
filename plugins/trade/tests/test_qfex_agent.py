"""Unit tests for QFEX agent (offline mocks)."""

from __future__ import annotations

import hmac
import os
import unittest
from unittest import mock

from plugins.trade.agents import x_qfex_agent as qfex
from plugins.trade import tradedesk


class QfexAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = os.environ.copy()
        for key in list(os.environ):
            if key.upper().startswith("QFEX_"):
                del os.environ[key]

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def _creds(self) -> None:
        os.environ["QFEX_AMIROO_PUBLIC_KEY"] = "public-key"
        os.environ["QFEX_AMIROO_SECRET_KEY"] = "secret-key"

    def test_list_accounts_requires_public_and_secret_key(self) -> None:
        self._creds()
        os.environ["QFEX_HALF_PUBLIC_KEY"] = "public-only"
        self.assertEqual(qfex.list_accounts(), ["amiroo"])

    def test_tradedesk_discovers_qfex(self) -> None:
        self.assertIn("qfex", tradedesk.TradeDesk().list_exchanges())

    def test_capabilities_include_trade_operations(self) -> None:
        for op in ("balance", "positions_orders", "positions_management", "new_order", "cancel_order_group", "resolve_instrument", "market_price", "ladder", "close_position", "set_tp", "set_sl"):
            self.assertIn(op, qfex.capabilities())

    def test_auth_headers_use_nonce_timestamp_hmac_signature(self) -> None:
        self._creds()
        creds = qfex._lookup_credentials("AMIROO")
        assert creds is not None
        with mock.patch.object(qfex.secrets, "token_hex", return_value="abc123"), \
             mock.patch.object(qfex.time, "time", return_value=1700000000):
            headers = qfex._auth_headers(creds)
        expected = hmac.new(b"secret-key", b"abc123:1700000000", "sha256").hexdigest()
        self.assertEqual(headers["x-qfex-public-key"], "public-key")
        self.assertEqual(headers["x-qfex-nonce"], "abc123")
        self.assertEqual(headers["x-qfex-timestamp"], "1700000000")
        self.assertEqual(headers["x-qfex-hmac-signature"], expected)


    def test_resolve_instrument_returns_qfex_symbol_for_confirmation(self) -> None:
        self._creds()

        def fake_public(path, query=""):
            self.assertEqual(path, "/refdata")
            return {
                "data": [
                    {
                        "symbol": "ETH-USD",
                        "base_asset": "ETH",
                        "quote_asset": "USD",
                        "tick_size": "0.01",
                        "lot_size": "0.001",
                        "min_quantity": "0.01",
                    }
                ]
            }

        with mock.patch.object(qfex, "_public_request", side_effect=fake_public):
            resp = qfex.execute({"operation": "resolve_instrument", "exchange": "qfex", "account": "AMIROO", "symbol": "eth"})

        self.assertTrue(resp.success, resp)
        assert resp.instrument is not None
        self.assertEqual(resp.instrument.requested_symbol, "eth")
        self.assertEqual(resp.instrument.symbol, "ETH-USD")
        self.assertEqual(resp.instrument.display_name, "ETH-USD")
        self.assertEqual(resp.instrument.price_increment, "0.01")
        self.assertEqual(resp.instrument.size_increment, "0.001")

    def test_market_price_returns_last_price_for_confirmation(self) -> None:
        self._creds()

        def fake_public(path, query=""):
            self.assertEqual(path, "/md/contracts")
            return {
                "data": [
                    {
                        "ticker_id": "ETH-USD",
                        "last_price": "2510.5",
                        "index_price": "2509.9",
                    }
                ]
            }

        with mock.patch.object(qfex, "_public_request", side_effect=fake_public):
            resp = qfex.execute({"operation": "market_price", "exchange": "qfex", "account": "AMIROO", "symbol": "ETH"})

        self.assertTrue(resp.success, resp)
        assert resp.market_price is not None
        self.assertEqual(resp.market_price.market, "ETH-USD")
        self.assertEqual(resp.market_price.price, "2510.5")
        self.assertEqual(resp.market_price.mark_price, "2509.9")

    def test_balance_rolls_up_available_balances(self) -> None:
        self._creds()

        def fake_request(creds, method, path, query=""):
            self.assertEqual(method, "GET")
            self.assertEqual(path, "/user/subaccounts/balance")
            return {
                "accounts": [
                    {"account_id": "master", "available_balance": 100.125, "is_master": True},
                    {"account_id": "sub", "available_balance": "50.125", "is_master": False},
                ]
            }

        with mock.patch.object(qfex, "_signed_request", side_effect=fake_request):
            resp = qfex.execute({"operation": "balance", "exchange": "qfex", "account": "AMIROO"})
        self.assertTrue(resp.success, resp)
        assert resp.balance is not None
        assert resp.portfolio_summary is not None
        self.assertEqual(resp.balance.unit, "USDT")
        self.assertEqual(resp.balance.value, "150.25")
        self.assertEqual(resp.portfolio_summary.withdrawable, "150.25")

    def test_positions_orders_normalizes_positions_and_open_orders(self) -> None:
        self._creds()

        def fake_rest(_creds, method, path, query=""):
            self.assertEqual(method, "GET")
            self.assertEqual(path, "/user/positions")
            return {
                "positions": [
                    {
                        "symbol": "ETH-USD",
                        "position": -2.5,
                        "average_price": 2400,
                        "unrealised_pnl": "12.5",
                        "realised_pnl": "1.5",
                    },
                    {"symbol": "BTC-USD", "position": 0, "average_price": 0},
                ],
                "balance": {"available_balance": 123},
            }

        def fake_ws(_creds, command, expect=None):
            self.assertEqual(command["type"], "get_user_orders")
            return {
                "all_orders_response": {
                    "orders": [
                        {
                            "order_id": "o1",
                            "symbol": "ETH-USD",
                            "side": "BUY",
                            "status": "ACK",
                            "quantity_remaining": 1.5,
                            "quantity": 2,
                            "price": 2300,
                        },
                        {
                            "order_id": "o2",
                            "symbol": "ETH-USD",
                            "side": "SELL",
                            "status": "ACK",
                            "quantity_remaining": 0.5,
                            "quantity": 0.5,
                            "price": 2600,
                        },
                    ]
                }
            }

        with mock.patch.object(qfex, "_signed_request", side_effect=fake_rest), \
             mock.patch.object(qfex, "_ws_command", side_effect=fake_ws):
            resp = qfex.execute({"operation": "positions_orders", "exchange": "qfex", "account": "AMIROO"})

        self.assertTrue(resp.success, resp)
        self.assertEqual(resp.open_order_count, 2)
        assert resp.positions is not None
        self.assertEqual(len(resp.positions), 1)
        self.assertEqual(resp.positions[0].symbol, "ETH")
        self.assertEqual(resp.positions[0].side, "short")
        self.assertEqual(resp.positions[0].size, "2.5")
        assert resp.order_groups is not None
        self.assertEqual([(g.symbol, g.side, g.order_count) for g in resp.order_groups], [("ETH", "buy", 1), ("ETH", "sell", 1)])


    def test_close_position_uses_qfex_close_position_command_and_verifies_flat(self) -> None:
        self._creds()
        rest_calls = []
        ws_calls = []

        def fake_rest(_creds, method, path, query=""):
            rest_calls.append(path)
            self.assertEqual(method, "GET")
            self.assertEqual(path, "/user/positions")
            if len(rest_calls) == 1:
                return {"positions": [{"symbol": "MSTR-USD", "position": 2, "average_price": 120, "unrealised_pnl": "5", "realised_pnl": "0"}]}
            return {"positions": []}

        def fake_ws(_creds, command, expect=None):
            ws_calls.append(command)
            self.assertEqual(command["type"], "close_position")
            self.assertEqual(command["params"]["symbol"], "MSTR-USD")
            self.assertIn("client_order_id", command["params"])
            return {"position_response": {"symbol": "MSTR-USD", "position": 0}}

        with mock.patch.object(qfex, "_signed_request", side_effect=fake_rest), \
             mock.patch.object(qfex, "_ws_command", side_effect=fake_ws):
            resp = qfex.execute({"operation": "close_position", "exchange": "qfex", "account": "AMIROO", "symbol": "MSTR"})

        self.assertTrue(resp.success, resp)
        self.assertEqual(len(ws_calls), 1)
        assert resp.position_action is not None
        self.assertEqual(resp.position_action.operation, "close_position")
        self.assertEqual(resp.position_action.symbol, "MSTR")
        self.assertTrue(resp.position_action.verified)
        self.assertEqual(resp.position_action.current_size, "0")


    def test_set_tp_submits_qfex_take_profit_reduce_order(self) -> None:
        self._creds()
        rest_calls = []
        ws_calls = []

        def fake_rest(_creds, method, path, query=""):
            rest_calls.append(path)
            return {"positions": [{"symbol": "MSTR-USD", "position": -0.1, "average_price": 135.5, "unrealised_pnl": "0", "realised_pnl": "0"}]}

        def fake_refdata(symbol):
            return {"symbol": "MSTR-USD", "tick_size": "0.01", "lot_size": "0.01", "min_quantity": "0.01"}

        def fake_ws(_creds, command, expect=None):
            ws_calls.append(command)
            self.assertEqual(command["type"], "add_order")
            self.assertEqual(command["params"]["symbol"], "MSTR-USD")
            self.assertEqual(command["params"]["side"], "BUY")
            self.assertEqual(command["params"]["order_type"], "TAKE_PROFIT")
            self.assertEqual(command["params"]["quantity"], 0.1)
            self.assertEqual(command["params"]["price"], 120.0)
            self.assertEqual(command["params"]["take_profit"], 120.0)
            return {"order_response": {"order_id": "tp-1", "symbol": "MSTR-USD", "side": "BUY", "type": "TAKE_PROFIT", "status": "ACK", "quantity": 0.1, "price": 120, "client_order_id": command["params"]["client_order_id"]}}

        with mock.patch.object(qfex, "_signed_request", side_effect=fake_rest), \
             mock.patch.object(qfex, "_find_refdata_symbol", side_effect=fake_refdata), \
             mock.patch.object(qfex, "_ws_command", side_effect=fake_ws):
            resp = qfex.execute({"operation": "set_tp", "exchange": "qfex", "account": "AMIROO", "symbol": "MSTR", "price": "120.001"})

        self.assertTrue(resp.success, resp)
        self.assertEqual(len(ws_calls), 1)
        assert resp.position_action is not None
        self.assertEqual(resp.position_action.operation, "set_tp")
        self.assertEqual(resp.position_action.price, "120")
        self.assertTrue(resp.position_action.verified)

    def test_set_sl_submits_qfex_stop_loss_reduce_order(self) -> None:
        self._creds()
        ws_calls = []

        def fake_rest(_creds, method, path, query=""):
            return {"positions": [{"symbol": "MSTR-USD", "position": -0.1, "average_price": 135.5, "unrealised_pnl": "0", "realised_pnl": "0"}]}

        def fake_refdata(symbol):
            return {"symbol": "MSTR-USD", "tick_size": "0.01", "lot_size": "0.01", "min_quantity": "0.01"}

        def fake_ws(_creds, command, expect=None):
            ws_calls.append(command)
            self.assertEqual(command["params"]["side"], "BUY")
            self.assertEqual(command["params"]["order_type"], "STOP_LOSS")
            self.assertEqual(command["params"]["stop_loss"], 150.0)
            return {"order_response": {"order_id": "sl-1", "symbol": "MSTR-USD", "side": "BUY", "type": "STOP_LOSS", "status": "ACK", "quantity": 0.1, "price": 150, "client_order_id": command["params"]["client_order_id"]}}

        with mock.patch.object(qfex, "_signed_request", side_effect=fake_rest), \
             mock.patch.object(qfex, "_find_refdata_symbol", side_effect=fake_refdata), \
             mock.patch.object(qfex, "_ws_command", side_effect=fake_ws):
            resp = qfex.execute({"operation": "set_sl", "exchange": "qfex", "account": "AMIROO", "symbol": "MSTR", "price": "150"})

        self.assertTrue(resp.success, resp)
        self.assertEqual(len(ws_calls), 1)
        assert resp.position_action is not None
        self.assertEqual(resp.position_action.operation, "set_sl")
        self.assertEqual(resp.position_action.price, "150")
        self.assertTrue(resp.position_action.verified)


    def test_positions_orders_merges_qfex_tp_sl_from_open_orders(self) -> None:
        self._creds()

        def fake_rest(_creds, method, path, query=""):
            return {
                "positions": [
                    {
                        "symbol": "MSTR-USD",
                        "position": -0.21,
                        "average_price": 136.2,
                        "unrealised_pnl": "-0.1",
                        "realised_pnl": "0",
                    }
                ]
            }

        def fake_ws(_creds, command, expect=None):
            return {
                "all_orders_response": {
                    "orders": [
                        {
                            "order_id": "tp-1",
                            "symbol": "MSTR-USD",
                            "side": "BUY",
                            "type": "TAKE_PROFIT",
                            "status": "ACK",
                            "quantity": 0.21,
                            "quantity_remaining": 0.21,
                            "price": 100,
                        },
                        {
                            "order_id": "sl-1",
                            "symbol": "MSTR-USD",
                            "side": "BUY",
                            "type": "STOP_LOSS",
                            "status": "ACK",
                            "quantity": 0.21,
                            "quantity_remaining": 0.21,
                            "price": 150,
                        },
                        {
                            "order_id": "limit-1",
                            "symbol": "MSTR-USD",
                            "side": "SELL",
                            "type": "LIMIT",
                            "status": "ACK",
                            "quantity_remaining": 0.1,
                            "price": 160,
                        },
                    ]
                }
            }

        with mock.patch.object(qfex, "_signed_request", side_effect=fake_rest), \
             mock.patch.object(qfex, "_ws_command", side_effect=fake_ws):
            resp = qfex.execute({"operation": "positions_management", "exchange": "qfex", "account": "AMIROO"})

        self.assertTrue(resp.success, resp)
        assert resp.positions is not None
        self.assertEqual(len(resp.positions), 1)
        pos = resp.positions[0]
        self.assertEqual(pos.symbol, "MSTR")
        self.assertEqual(pos.side, "short")
        self.assertEqual(pos.tp, "100")
        self.assertEqual(pos.tp_count, 1)
        self.assertEqual(pos.sl, "150")
        self.assertEqual(pos.sl_count, 1)

    def test_new_order_sends_limit_order_over_websocket(self) -> None:
        self._creds()
        sent = []

        def fake_ws(_creds, command, expect=None):
            sent.append(command)
            return {
                "order_response": {
                    "order_id": "oid-1",
                    "client_order_id": command["params"]["client_order_id"],
                    "symbol": "ETH-USD",
                    "side": "BUY",
                    "type": "LIMIT",
                    "status": "ACK",
                    "quantity": 1.25,
                    "price": 2500,
                    "quantity_remaining": 1.25,
                }
            }

        with mock.patch.object(qfex, "_ws_command", side_effect=fake_ws):
            resp = qfex.execute(
                {
                    "operation": "new_order",
                    "exchange": "qfex",
                    "account": "AMIROO",
                    "symbol": "ETH",
                    "side": "buy",
                    "volume": "1.25",
                    "price": "2500",
                }
            )

        self.assertTrue(resp.success, resp)
        self.assertEqual(sent[0]["type"], "add_order")
        self.assertEqual(sent[0]["params"]["symbol"], "ETH-USD")
        self.assertEqual(sent[0]["params"]["side"], "BUY")
        self.assertEqual(sent[0]["params"]["order_type"], "LIMIT")
        assert resp.order is not None
        self.assertEqual(resp.order.exchange_order_id, "oid-1")
        self.assertEqual(resp.order.status, "success")


    def test_new_order_verifies_by_client_order_id_after_timeout(self) -> None:
        self._creds()
        calls = []

        def fake_ws(_creds, command, expect=None):
            calls.append(command["type"])
            if command["type"] == "add_order":
                raise RuntimeError("Connection timed out")
            if command["type"] == "get_user_orders":
                return {
                    "all_orders_response": {
                        "orders": [
                            {
                                "order_id": "oid-late",
                                "client_order_id": "fixed-client-id",
                                "symbol": "MSTR-USD",
                                "side": "SELL",
                                "status": "ACK",
                                "quantity": 1,
                                "price": 150,
                                "quantity_remaining": 1,
                            }
                        ]
                    }
                }
            raise AssertionError(command)

        with mock.patch.object(qfex.uuid, "uuid4", return_value=type("U", (), {"hex": "fixed-client-id"})()), \
             mock.patch.object(qfex, "_ws_command", side_effect=fake_ws):
            resp = qfex.execute(
                {
                    "operation": "new_order",
                    "exchange": "qfex",
                    "account": "AMIROO",
                    "symbol": "MSTR-USD",
                    "side": "sell",
                    "volume": "1",
                    "price": "150",
                }
            )

        self.assertTrue(resp.success, resp)
        self.assertEqual(calls, ["add_order", "get_user_orders"])
        assert resp.order is not None
        self.assertEqual(resp.order.exchange_order_id, "oid-late")
        self.assertTrue(resp.order.verified)


    def test_ladder_submits_multiple_qfex_limit_orders(self) -> None:
        self._creds()
        commands = []

        def fake_ws(_creds, command, expect=None):
            commands.append(command)
            idx = len([c for c in commands if c["type"] == "add_order"])
            return {
                "order_response": {
                    "order_id": f"ladder-{idx}",
                    "client_order_id": command["params"]["client_order_id"],
                    "symbol": command["params"]["symbol"],
                    "side": command["params"]["side"],
                    "status": "ACK",
                    "quantity": command["params"]["quantity"],
                    "price": command["params"]["price"],
                    "quantity_remaining": command["params"]["quantity"],
                }
            }

        with mock.patch.object(qfex, "_ws_command", side_effect=fake_ws):
            resp = qfex.execute(
                {
                    "operation": "ladder",
                    "exchange": "qfex",
                    "account": "AMIROO",
                    "symbol": "MSTR-USD",
                    "side": "sell",
                    "distribution": "uniform",
                    "order_count": "3",
                    "total_volume": "6",
                    "start_price": "140",
                    "end_price": "160",
                }
            )

        self.assertTrue(resp.success, resp)
        self.assertEqual([c["params"]["price"] for c in commands], [140.0, 150.0, 160.0])
        self.assertEqual([c["params"]["quantity"] for c in commands], [2.0, 2.0, 2.0])
        self.assertTrue(all(c["params"]["symbol"] == "MSTR-USD" for c in commands))
        assert resp.ladder is not None
        self.assertEqual(resp.ladder.requested_order_count, 3)
        self.assertEqual(resp.ladder.submitted_order_count, 3)
        self.assertEqual(resp.ladder.submitted_volume, "6")
        self.assertTrue(resp.ladder.verified)



    def test_ladder_allows_up_to_100_orders(self) -> None:
        self._creds()
        commands = []

        def fake_refdata(symbol):
            return {"symbol": "MSTR-USD", "tick_size": "0.01", "lot_size": "0.01", "min_quantity": "0.01"}

        def fake_ws(_creds, command, expect=None):
            commands.append(command)
            return {
                "order_response": {
                    "order_id": f"ladder-{len(commands)}",
                    "client_order_id": command["params"]["client_order_id"],
                    "symbol": command["params"]["symbol"],
                    "side": command["params"]["side"],
                    "status": "ACK",
                    "quantity": command["params"]["quantity"],
                    "price": command["params"]["price"],
                    "quantity_remaining": command["params"]["quantity"],
                }
            }

        with mock.patch.object(qfex, "_find_refdata_symbol", side_effect=fake_refdata), \
             mock.patch.object(qfex, "_ws_command", side_effect=fake_ws):
            resp = qfex.execute(
                {
                    "operation": "ladder",
                    "exchange": "qfex",
                    "account": "AMIROO",
                    "symbol": "MSTR-USD",
                    "side": "sell",
                    "distribution": "uniform",
                    "order_count": "100",
                    "total_volume": "1",
                    "start_price": "140",
                    "end_price": "150",
                }
            )

        self.assertTrue(resp.success, resp)
        self.assertEqual(len(commands), 100)
        assert resp.ladder is not None
        self.assertEqual(resp.ladder.requested_order_count, 100)
        self.assertEqual(resp.ladder.submitted_order_count, 100)

    def test_ladder_rejects_more_than_100_orders(self) -> None:
        self._creds()
        resp = qfex.execute(
            {
                "operation": "ladder",
                "exchange": "qfex",
                "account": "AMIROO",
                "symbol": "MSTR-USD",
                "side": "sell",
                "distribution": "uniform",
                "order_count": "101",
                "total_volume": "1",
                "start_price": "140",
                "end_price": "150",
            }
        )
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "INVALID_REQUEST")
        self.assertIn("100", resp.error.message)

    def test_ladder_quantizes_prices_and_sizes_to_qfex_increments(self) -> None:
        self._creds()
        commands = []

        def fake_refdata(symbol):
            self.assertEqual(symbol, "MSTR-USD")
            return {"symbol": "MSTR-USD", "tick_size": "0.01", "lot_size": "0.01", "min_quantity": "0.1"}

        def fake_ws(_creds, command, expect=None):
            commands.append(command)
            return {
                "order_response": {
                    "order_id": f"ladder-{len(commands)}",
                    "client_order_id": command["params"]["client_order_id"],
                    "symbol": command["params"]["symbol"],
                    "side": command["params"]["side"],
                    "status": "ACK",
                    "quantity": command["params"]["quantity"],
                    "price": command["params"]["price"],
                    "quantity_remaining": command["params"]["quantity"],
                }
            }

        with mock.patch.object(qfex, "_find_refdata_symbol", side_effect=fake_refdata), \
             mock.patch.object(qfex, "_ws_command", side_effect=fake_ws):
            resp = qfex.execute(
                {
                    "operation": "ladder",
                    "exchange": "qfex",
                    "account": "AMIROO",
                    "symbol": "MSTR-USD",
                    "side": "sell",
                    "distribution": "uniform",
                    "order_count": "3",
                    "total_volume": "1",
                    "start_price": "140.001",
                    "end_price": "140.029",
                }
            )

        self.assertTrue(resp.success, resp)
        self.assertEqual([c["params"]["price"] for c in commands], [140.0, 140.02, 140.03])
        self.assertEqual([c["params"]["quantity"] for c in commands], [0.34, 0.33, 0.33])
        assert resp.ladder is not None
        self.assertEqual(resp.ladder.submitted_volume, "1")

    def test_ladder_verifies_child_after_qfex_timeout(self) -> None:
        self._creds()
        calls = []

        def fake_ws(_creds, command, expect=None):
            calls.append(command["type"])
            if command["type"] == "add_order":
                raise RuntimeError("Connection timed out")
            if command["type"] == "get_user_orders":
                return {
                    "all_orders_response": {
                        "orders": [
                            {
                                "order_id": "late-child",
                                "client_order_id": command["params"].get("client_order_id", "fixed-ladder-child"),
                                "symbol": "MSTR-USD",
                                "side": "SELL",
                                "status": "ACK",
                                "quantity": 1,
                                "price": 140,
                                "quantity_remaining": 1,
                            }
                        ]
                    }
                }
            raise AssertionError(command)

        with mock.patch.object(qfex.uuid, "uuid4", return_value=type("U", (), {"hex": "fixed-ladder-child"})()), \
             mock.patch.object(qfex, "_ws_command", side_effect=fake_ws):
            resp = qfex.execute(
                {
                    "operation": "ladder",
                    "exchange": "qfex",
                    "account": "AMIROO",
                    "symbol": "MSTR-USD",
                    "side": "sell",
                    "distribution": "uniform",
                    "order_count": "1",
                    "total_volume": "1",
                    "start_price": "140",
                    "end_price": "140",
                }
            )

        self.assertTrue(resp.success, resp)
        self.assertEqual(calls, ["add_order", "get_user_orders"])
        assert resp.ladder is not None
        self.assertEqual(resp.ladder.child_order_ids, ["late-child"])
        self.assertTrue(resp.ladder.verified)

    def test_cancel_order_group_cancels_matching_symbol_and_side(self) -> None:
        self._creds()
        commands = []

        def fake_ws(_creds, command, expect=None):
            commands.append(command)
            if command["type"] == "get_user_orders":
                return {
                    "all_orders_response": {
                        "orders": [
                            {"order_id": "buy-1", "symbol": "ETH-USD", "side": "BUY", "status": "ACK", "quantity_remaining": 1, "price": 2400},
                            {"order_id": "sell-1", "symbol": "ETH-USD", "side": "SELL", "status": "ACK", "quantity_remaining": 1, "price": 2600},
                            {"order_id": "btc-1", "symbol": "BTC-USD", "side": "BUY", "status": "ACK", "quantity_remaining": 1, "price": 90000},
                        ]
                    }
                }
            if command["type"] == "cancel_order":
                return {"order_response": {"order_id": command["params"]["order_id"], "symbol": command["params"]["symbol"], "status": "CANCELLED"}}
            raise AssertionError(command)

        with mock.patch.object(qfex, "_ws_command", side_effect=fake_ws):
            resp = qfex.execute(
                {"operation": "cancel_order_group", "exchange": "qfex", "account": "AMIROO", "symbol": "ETH", "side": "buy"}
            )

        self.assertTrue(resp.success, resp)
        cancel_commands = [c for c in commands if c["type"] == "cancel_order"]
        self.assertEqual(len(cancel_commands), 1)
        self.assertEqual(cancel_commands[0]["params"]["order_id"], "buy-1")
        assert resp.cancel_group is not None
        self.assertEqual(resp.cancel_group.targeted_order_count, 1)
        self.assertEqual(resp.cancel_group.cancelled_order_count, 1)
        self.assertTrue(resp.cancel_group.verified)

    def test_unsupported_operation_returns_canonical_failure(self) -> None:
        self._creds()
        resp = qfex.execute({"operation": "not_a_real_operation", "exchange": "qfex", "account": "amiroo"})
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "NOT_IMPLEMENTED")



    def test_cancel_order_group_verifies_absent_after_timeout(self) -> None:
        self._creds()
        calls = []

        def fake_ws(_creds, command, expect=None):
            calls.append(command["type"])
            if command["type"] == "get_user_orders" and calls.count("get_user_orders") == 1:
                return {
                    "all_orders_response": {
                        "orders": [
                            {
                                "order_id": "mstr-140",
                                "symbol": "MSTR-USD",
                                "side": "SELL",
                                "status": "ACK",
                                "quantity_remaining": 1,
                                "price": 140,
                            }
                        ]
                    }
                }
            if command["type"] == "cancel_order":
                raise RuntimeError("Connection timed out")
            if command["type"] == "get_user_orders" and calls.count("get_user_orders") == 2:
                return {"all_orders_response": {"orders": []}}
            raise AssertionError(command)

        with mock.patch.object(qfex, "_ws_command", side_effect=fake_ws):
            resp = qfex.execute(
                {
                    "operation": "cancel_order_group",
                    "exchange": "qfex",
                    "account": "AMIROO",
                    "symbol": "MSTR-USD",
                    "side": "sell",
                }
            )

        self.assertTrue(resp.success, resp)
        self.assertEqual(calls, ["get_user_orders", "cancel_order", "get_user_orders"])
        assert resp.cancel_group is not None
        self.assertEqual(resp.cancel_group.cancelled_order_count, 1)
        self.assertEqual(resp.cancel_group.confirmed_absent_count, 1)
        self.assertTrue(resp.cancel_group.verified)


if __name__ == "__main__":
    unittest.main()
