from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import mock

from plugins.trade.agents import x_hibachi_agent as hibachi


class HibachiClosePositionTests(unittest.TestCase):
    def test_capabilities_include_close_position(self) -> None:
        self.assertIn("close_position", hibachi.capabilities())

    def test_close_position_payload_is_reduce_only_and_reverses_side(self) -> None:
        creds = {"account_id": 30352, "private_key": "priv", "account": "main"}
        descriptor = {
            "symbol": "BTC/USDT-P",
            "id": 99,
            "underlying_decimals": 8,
            "settlement_decimals": 6,
        }
        market_payload = {"feeConfig": {"tradeTakerFeeRate": "0.00045"}}

        with mock.patch.object(hibachi, "_next_hibachi_nonce", return_value=123456789), mock.patch.object(
            hibachi, "_sign_hibachi_place_order", return_value="signed-payload"
        ) as sign:
            payload = hibachi._build_hibachi_close_position_payload(
                credentials=creds,
                descriptor=descriptor,
                current_side="long",
                current_size=Decimal("1.25"),
                market_payload=market_payload,
            )

        self.assertEqual(payload["symbol"], "BTC/USDT-P")
        self.assertEqual(payload["quantity"], "1.25")
        self.assertEqual(payload["side"], "ASK")
        self.assertEqual(payload["orderFlags"], "REDUCE_ONLY")
        self.assertEqual(payload["accountId"], 30352)
        self.assertEqual(payload["nonce"], 123456789)
        self.assertEqual(payload["signature"], "signed-payload")
        sign.assert_called_once()
        args, kwargs = sign.call_args
        self.assertEqual(kwargs["side"], "sell")
        self.assertEqual(kwargs["quantity"], Decimal("1.25"))

    def test_execute_close_position_dispatches_to_close_position_handler(self) -> None:
        context = {
            "credentials": {"account": "main", "account_id": 30352, "private_key": "priv"},
            "descriptor": {"symbol": "BTC/USDT-P", "id": 99, "underlying_decimals": 8, "settlement_decimals": 6},
            "market_payload": {"feeConfig": {"tradeTakerFeeRate": "0.00045"}},
            "current_side": "short",
            "current_size": Decimal("2.5"),
            "canonical_symbol": "BTC",
            "current_position": None,
        }

        find_side_effect = [(context, None)] + [(None, None)] * 4
        with mock.patch.object(hibachi, "_find_hibachi_position_context", side_effect=find_side_effect) as find_ctx, mock.patch.object(
            hibachi, "_cancel_order_group", return_value=None
        ) as cancel_group, mock.patch.object(hibachi, "_build_hibachi_close_position_payload", return_value={"payload": True}) as build, mock.patch.object(
            hibachi, "_submit_single_order", return_value={"orderId": "9001"}
        ) as submit:
            resp = hibachi.execute({"operation": "close_position", "account": "main", "exchange": "hibachi", "symbol": "BTC"})

        self.assertTrue(resp.success)
        self.assertEqual(resp.operation, "close_position")
        self.assertEqual(resp.exchange, "hibachi")
        self.assertIsNotNone(resp.position_action)
        action = resp.position_action
        self.assertEqual(action.operation, "close_position")
        self.assertEqual(action.symbol, "BTC")
        self.assertTrue(action.verified)
        self.assertEqual(find_ctx.call_count, 2)
        cancel_group.assert_called_once()
        build.assert_called_once()
        submit.assert_called_once_with(context["credentials"], {"payload": True})


if __name__ == "__main__":
    unittest.main()
