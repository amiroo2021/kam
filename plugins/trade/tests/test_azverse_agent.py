from __future__ import annotations

import hashlib
import hmac
import sys
import traceback
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.agents import x_azverse_agent as azverse  # noqa: E402
from plugins.trade.tradedesk import _exchange_name_from_filename  # noqa: E402


class AzverseAgentTests(unittest.TestCase):
    def test_trade_filename_parser_accepts_azverse_agent(self) -> None:
        self.assertEqual(_exchange_name_from_filename("x_azverse_agent.py"), "azverse")
        self.assertIsNone(_exchange_name_from_filename("x_azverse_agent_spot.py"))

    def test_discovers_only_complete_key_secret_pairs(self) -> None:
        env = {
            "AZVERSE_AMIROO_KEY": "api-key",
            "AZVERSE_AMIROO_SECRET": "api-secret",
            "AZVERSE_HALF_KEY": "key-without-secret",
        }
        combined = {key: (key, value, "env") for key, value in env.items()}
        with mock.patch.object(azverse, "_combined_azverse_env", return_value=combined):
            self.assertEqual(azverse.list_accounts(), ["amiroo"])

    def test_environment_source_wins_across_credential_aliases(self) -> None:
        combined = {
            "AZVERSE_MAIN_KEY": ("AZVERSE_MAIN_KEY", "env-key", "env"),
            "AZVERSE_MAIN_API_KEY": ("AZVERSE_MAIN_API_KEY", "dotenv-key", "dotenv"),
            "AZVERSE_MAIN_SECRET": ("AZVERSE_MAIN_SECRET", "env-secret", "env"),
            "AZVERSE_MAIN_SECRET_KEY": ("AZVERSE_MAIN_SECRET_KEY", "dotenv-secret", "dotenv"),
        }
        with mock.patch.object(azverse, "_combined_azverse_env", return_value=combined):
            self.assertEqual(azverse._discover_credential_map()["main"], {"key": "env-key", "secret": "env-secret"})

    def test_capabilities_are_read_only_balance_positions_and_orders(self) -> None:
        self.assertEqual(
            azverse.capabilities(),
            ["balance", "positions_orders", "positions_management"],
        )
        for forbidden in ("new_order", "cancel_orders", "ladder", "close_position", "set_tp", "set_sl"):
            self.assertNotIn(forbidden, azverse.capabilities())

    def test_balance_maps_live_futures_fields(self) -> None:
        payload = [
            {
                "coin": "usdt",
                "walletBalance": "9989.51327982",
                "availableBalance": "6901.14496235",
                "openOrderMarginFrozen": "0",
                "isolatedMargin": "0",
                "crossedMargin": "3088.36831747",
            }
        ]
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        with mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_signed_get", return_value=payload
        ) as signed:
            response = azverse.execute({"operation": "balance", "exchange": "azverse", "account": "amiroo"})

        self.assertTrue(response.success, response)
        self.assertIsNotNone(response.balance)
        self.assertEqual(response.balance.value, "9989.51")
        self.assertEqual(response.balance.unit, "USDT")
        self.assertIsNotNone(response.portfolio_summary)
        self.assertEqual(response.portfolio_summary.withdrawable, "6901.14")
        self.assertEqual(response.portfolio_summary.margin_used, "3088.37")
        self.assertEqual(response.data["assets"][0]["total"], "9989.51327982")
        signed.assert_called_once_with(creds, azverse.BALANCE_PATH)

    def test_balance_rejects_missing_or_invalid_wallet_balance(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        for payload in ([{"coin": "usdt", "availableBalance": "1"}], [{"coin": "usdt", "walletBalance": "invalid", "availableBalance": "1"}]):
            with self.subTest(payload=payload), mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
                azverse, "_signed_get", return_value=payload
            ):
                response = azverse.execute({"operation": "balance", "exchange": "azverse", "account": "amiroo"})
            self.assertFalse(response.success)
            self.assertEqual(response.error.code, "AZVERSE_BALANCE_ERROR")

    def test_balance_rejects_missing_or_invalid_available_balance(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        for payload in ([{"coin": "usdt", "walletBalance": "1"}], [{"coin": "usdt", "walletBalance": "1", "availableBalance": "invalid"}]):
            with self.subTest(payload=payload), mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
                azverse, "_signed_get", return_value=payload
            ):
                response = azverse.execute({"operation": "balance", "exchange": "azverse", "account": "amiroo"})
            self.assertFalse(response.success)
            self.assertEqual(response.error.code, "AZVERSE_BALANCE_ERROR")

    def test_positions_orders_maps_position_and_groups_entry_orders(self) -> None:
        positions = [
            {
                "symbol": "zec_usdt",
                "positionSide": "SHORT",
                "positionSize": "50",
                "entryPrice": "1240.16",
                "markPrice": "1256",
                "triggerProfitPrice": None,
                "triggerStopPrice": None,
            }
        ]
        orders = {
            "page": 1,
            "ps": 100,
            "total": 2,
            "items": [
                {
                    "orderId": "o1",
                    "symbol": "zec_usdt",
                    "orderSide": "BUY",
                    "orderType": "LIMIT",
                    "origQty": "2",
                    "executedQty": "0.5",
                    "price": "1200",
                    "reduceOnly": False,
                },
                {
                    "orderId": "o2",
                    "symbol": "zec_usdt",
                    "orderSide": "BUY",
                    "orderType": "LIMIT",
                    "origQty": "1",
                    "executedQty": "0",
                    "price": "1210",
                    "reduceOnly": False,
                },
            ],
        }
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}

        def fake_get(_creds, path, query=None):
            if path == azverse.POSITIONS_PATH:
                return positions
            if path == azverse.ORDERS_PATH:
                return orders
            raise AssertionError(path)

        with mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_signed_get", side_effect=fake_get
        ):
            response = azverse.execute({"operation": "positions_orders", "exchange": "azverse", "account": "amiroo"})

        self.assertTrue(response.success, response)
        self.assertEqual(len(response.positions), 1)
        position = response.positions[0]
        self.assertEqual(position.symbol, "ZEC")
        self.assertEqual(position.side, "short")
        self.assertEqual(position.size, "50")
        self.assertEqual(position.pnl, "-792")
        self.assertEqual(position.mark, "1256")
        self.assertEqual(response.open_order_count, 2)
        self.assertEqual(len(response.order_groups), 1)
        group = response.order_groups[0]
        self.assertEqual(group.symbol, "ZEC")
        self.assertEqual(group.side, "buy")
        self.assertEqual(group.order_count, 2)
        self.assertEqual(group.total_size, "2.5")
        self.assertEqual(group.vwap, "1204")
        self.assertEqual(group.order_ids, ["o1", "o2"])

    def test_positions_management_is_positions_only_and_does_not_fetch_orders(self) -> None:
        positions = [{"symbol": "btc_usdt", "positionSide": "LONG", "positionSize": "0", "entryPrice": "0", "markPrice": "0"}]
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        with mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_signed_get", return_value=positions
        ) as signed:
            response = azverse.execute({"operation": "positions_management", "exchange": "azverse", "account": "amiroo"})

        self.assertTrue(response.success, response)
        self.assertEqual(response.positions, [])
        signed.assert_called_once_with(creds, azverse.POSITIONS_PATH)

    def test_open_orders_requests_all_unfinished_states(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        with mock.patch.object(azverse, "_signed_get", return_value={"page": 1, "ps": 100, "total": 0, "items": []}) as signed:
            self.assertEqual(azverse._fetch_open_orders(creds), [])
        signed.assert_called_once_with(
            creds,
            azverse.ORDERS_PATH,
            {"page": 1, "size": 100, "state": "UNFINISHED"},
        )

    def test_return_code_200_is_accepted_as_success(self) -> None:
        result = [{"coin": "usdt"}]
        self.assertEqual(
            azverse._unwrap_envelope({"returnCode": 200, "msgInfo": "success", "result": result}),
            result,
        )

    def test_missing_mark_does_not_fabricate_mark_or_pnl(self) -> None:
        positions = azverse._normalize_positions(
            [{"symbol": "zec_usdt", "positionSide": "SHORT", "positionSize": "50", "entryPrice": "1240.16"}]
        )
        self.assertEqual(len(positions), 1)
        self.assertIsNone(positions[0].mark)
        self.assertEqual(positions[0].pnl, "")

    def test_explicit_unrealized_pnl_is_used_when_mark_is_missing(self) -> None:
        positions = azverse._normalize_positions(
            [
                {
                    "symbol": "zec_usdt",
                    "positionSide": "SHORT",
                    "positionSize": "50",
                    "entryPrice": "1240.16",
                    "unrealizedPnl": "-12.34",
                }
            ]
        )
        self.assertIsNone(positions[0].mark)
        self.assertEqual(positions[0].pnl, "-12.34")

    def test_nonzero_position_without_valid_entry_price_fails_closed(self) -> None:
        for entry in (None, "invalid"):
            row = {
                "symbol": "zec_usdt",
                "positionSide": "SHORT",
                "positionSize": "2",
                "markPrice": "100",
                "entryPrice": entry,
            }
            with self.subTest(entry=entry), self.assertRaisesRegex(ValueError, "entryPrice"):
                azverse._normalize_positions([row])

    def test_generated_signature_is_redacted_in_common_error_formats(self) -> None:
        signature = "a" * 64
        credentials = {"api_key": "key", "secret": "secret"}
        messages = (
            f"validate-signature={signature}",
            f"validate-signature: {signature}",
            f'{{"validate-signature":"{signature}"}}',
            f"upstream reflected authentication material {signature}",
        )
        for message in messages:
            with self.subTest(message=message):
                redacted = azverse._redact(message, credentials, extra_secrets=(signature,))
                self.assertNotIn(signature, redacted)
                self.assertIn("***", redacted)

    def test_unexpected_transport_exception_cannot_leak_generated_signature(self) -> None:
        creds = {"account": "amiroo", "api_key": "api-key", "secret": "api-secret", "api_base": azverse.DEFAULT_API_BASE}
        timestamp = "1000"
        presign = f"validate-appkey=api-key&validate-timestamp={timestamp}#{azverse.BALANCE_PATH}"
        signature = hmac.new(b"api-secret", presign.encode(), hashlib.sha256).hexdigest()
        with mock.patch.object(azverse.time, "time", return_value=1.0), mock.patch.object(
            azverse, "_open_no_redirect", side_effect=RuntimeError(f"transport reflected {signature}")
        ):
            with self.assertRaises(RuntimeError) as caught:
                azverse._signed_get(creds, azverse.BALANCE_PATH)
        self.assertNotIn(signature, str(caught.exception))
        self.assertIn("***", str(caught.exception))

    def test_authenticated_requests_refuse_all_redirects(self) -> None:
        handler = azverse._NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://other.example/path"))

    def test_read_failure_returns_none_lists_and_redacts_credentials(self) -> None:
        creds = {
            "account": "amiroo",
            "api_key": "az-key-secret-value",
            "secret": "az-private-secret-value",
            "api_base": azverse.DEFAULT_API_BASE,
        }
        with mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse,
            "_signed_get",
            side_effect=RuntimeError("bad az-key-secret-value / az-private-secret-value"),
        ):
            response = azverse.execute({"operation": "positions_orders", "exchange": "azverse", "account": "amiroo"})

        self.assertFalse(response.success)
        self.assertIsNone(response.positions)
        self.assertIsNone(response.order_groups)
        self.assertIsNone(response.open_order_count)
        self.assertNotIn(creds["api_key"], response.error.message)
        self.assertNotIn(creds["secret"], response.error.message)

    def test_mutating_operations_are_not_implemented_without_http(self) -> None:
        with mock.patch.object(azverse, "_signed_get") as signed:
            response = azverse.execute({"operation": "new_order", "exchange": "azverse", "account": "amiroo"})
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "NOT_IMPLEMENTED")
        signed.assert_not_called()


    class _ExplodingErrorBody:
        def __init__(self, message: str) -> None:
            self.message = message

        def read(self, *_args, **_kwargs):
            raise RuntimeError(self.message)

        def close(self) -> None:
            return None

    def test_http_error_body_read_failure_cannot_leak_generated_signature(self) -> None:
        credentials = {
            "account": "main",
            "api_key": "api-key",
            "secret": "api-secret",
            "api_base": azverse.DEFAULT_API_BASE,
        }
        timestamp = "1000"
        presign = f"validate-appkey=api-key&validate-timestamp={timestamp}#{azverse.BALANCE_PATH}"
        signature = hmac.new(b"api-secret", presign.encode(), hashlib.sha256).hexdigest()
        error = urllib.error.HTTPError(
            f"{azverse.DEFAULT_API_BASE}{azverse.BALANCE_PATH}",
            500,
            "Server Error",
            {},
            self._ExplodingErrorBody(f"api-key api-secret {signature}"),
        )
        with mock.patch.object(azverse.time, "time", return_value=1.0), mock.patch.object(
            azverse, "_open_no_redirect", side_effect=error
        ), mock.patch.object(azverse, "_lookup_credentials", return_value=credentials):
            response = azverse.execute({"operation": "balance", "exchange": "azverse", "account": "main"})
        self.assertFalse(response.success)
        self.assertNotIn("api-key", response.error.message)
        self.assertNotIn("api-secret", response.error.message)
        self.assertNotIn(signature, response.error.message)

    def test_balance_rejects_present_but_invalid_margin_fields(self) -> None:
        credentials = {"account": "main", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        for field in ("crossedMargin", "isolatedMargin", "openOrderMarginFrozen", "openOrderFeeFrozen", "unrealizedPnl"):
            row = {"coin": "USDT", "walletBalance": "10", "availableBalance": "8", field: "bad"}
            with self.subTest(field=field), mock.patch.object(azverse, "_lookup_credentials", return_value=credentials), mock.patch.object(
                azverse, "_signed_get", return_value=[row]
            ):
                response = azverse.execute({"operation": "balance", "exchange": "azverse", "account": "main"})
            self.assertFalse(response.success)
            self.assertEqual(response.error.code, "AZVERSE_BALANCE_ERROR")

    def test_malformed_position_size_fails_closed_instead_of_disappearing(self) -> None:
        credentials = {"account": "main", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        row = {"symbol": "BTC-USDT", "positionSize": "bad", "entryPrice": "100", "markPrice": "110"}
        with mock.patch.object(azverse, "_lookup_credentials", return_value=credentials), mock.patch.object(
            azverse, "_signed_get", return_value=[row]
        ):
            response = azverse.execute({"operation": "positions_management", "exchange": "azverse", "account": "main"})
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "AZVERSE_POSITIONS_ORDERS_ERROR")

    def test_malformed_position_payload_and_identity_fail_closed(self) -> None:
        for payload in ({"items": []}, ["corrupt"]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                azverse._normalize_positions(payload)
        base = {"symbol": "BTC-USDT", "positionSide": "LONG", "positionSize": "1", "entryPrice": "100"}
        for changes in ({"symbol": ""}, {"positionSide": ""}, {"positionSide": "SIDEWAYS"}, {"triggerProfitPrice": "bad"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                azverse._normalize_positions([{**base, **changes}])

    def test_redacted_transport_exception_drops_secret_bearing_cause_and_traceback(self) -> None:
        credentials = {"account": "main", "api_key": "api-key", "secret": "api-secret", "api_base": azverse.DEFAULT_API_BASE}
        timestamp = "1000"
        presign = f"validate-appkey=api-key&validate-timestamp={timestamp}#{azverse.BALANCE_PATH}"
        signature = hmac.new(b"api-secret", presign.encode(), hashlib.sha256).hexdigest()
        with mock.patch.object(azverse.time, "time", return_value=1.0), mock.patch.object(
            azverse, "_open_no_redirect", side_effect=RuntimeError(f"api-key api-secret {signature}")
        ):
            with self.assertRaises(RuntimeError) as caught:
                azverse._signed_get(credentials, azverse.BALANCE_PATH)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("api-key", rendered)
        self.assertNotIn("api-secret", rendered)
        self.assertNotIn(signature, rendered)

    def test_malformed_order_numeric_fields_fail_closed(self) -> None:
        base = {
            "symbol": "BTC-USDT",
            "orderSide": "BUY",
            "orderType": "LIMIT",
            "origQty": "2",
            "executedQty": "0.5",
            "price": "100",
        }
        for field, value in (("origQty", "bad"), ("origQty", "NaN"), ("executedQty", "bad"), ("price", "Infinity")):
            row = {**base, field: value}
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                azverse._normalize_order_groups([row])
        missing_original = {key: value for key, value in base.items() if key != "origQty"}
        with self.assertRaises(ValueError):
            azverse._normalize_order_groups([missing_original])

    def test_malformed_order_total_and_rows_fail_closed(self) -> None:
        row = {"symbol": "BTC-USDT", "orderSide": "BUY", "origQty": "1", "executedQty": "0", "price": "100"}
        for total in ("bad", "1.5", -1):
            with self.subTest(total=total), self.assertRaises(ValueError):
                azverse._order_items({"items": [row], "total": total})
        with self.assertRaises(ValueError):
            azverse._order_items({"items": [row, "corrupt"], "total": 2})
        with self.assertRaises(ValueError):
            azverse._order_items({"total": 0})

    def test_missing_order_type_and_limit_price_fail_closed(self) -> None:
        base = {"symbol": "BTC-USDT", "orderSide": "BUY", "origQty": "1", "executedQty": "0", "price": "100"}
        with self.assertRaises(ValueError):
            azverse._normalize_order_groups([base])
        with self.assertRaises(ValueError):
            azverse._normalize_order_groups([{**base, "orderType": "LIMIT", "price": ""}])

    def test_order_pagination_rejects_total_changes_overshoot_and_duplicates(self) -> None:
        def rows(start: int, stop: int):
            return [
                {
                    "orderId": str(index),
                    "symbol": "BTC-USDT",
                    "orderSide": "BUY",
                    "orderType": "LIMIT",
                    "origQty": "1",
                    "executedQty": "0",
                    "price": "100",
                }
                for index in range(start, stop)
            ]

        credentials = {"account": "main", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        scenarios = (
            [{"items": rows(0, 100), "total": 150}, {"items": rows(100, 160), "total": 150}],
            [{"items": rows(0, 100), "total": 150}, {"items": rows(100, 150), "total": 151}],
            [{"items": rows(0, 100), "total": 150}, {"items": rows(50, 100), "total": 150}],
        )
        for pages in scenarios:
            with self.subTest(pages=len(pages)), mock.patch.object(azverse, "_signed_get", side_effect=pages), self.assertRaises(ValueError):
                azverse._fetch_open_orders(credentials)


    def test_balance_rejects_mixed_malformed_rows_and_missing_coin(self) -> None:
        credentials = {"account": "main", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        valid = {"coin": "USDT", "walletBalance": "10", "availableBalance": "8"}
        for payload in ([valid, "corrupt"], [valid, {"walletBalance": "1", "availableBalance": "1"}]):
            with self.subTest(payload=payload), mock.patch.object(azverse, "_lookup_credentials", return_value=credentials), mock.patch.object(
                azverse, "_signed_get", return_value=payload
            ):
                response = azverse.execute({"operation": "balance", "exchange": "azverse", "account": "main"})
            self.assertFalse(response.success)
            self.assertEqual(response.error.code, "AZVERSE_BALANCE_ERROR")

    def test_order_envelope_requires_mapping_items_and_total(self) -> None:
        row = {"symbol": "BTC-USDT", "orderSide": "BUY", "orderType": "LIMIT", "origQty": "1", "executedQty": "0", "price": "100"}
        for payload in ([row], {"total": 0}, {"items": [row]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                azverse._order_items(payload)

    def test_order_pagination_rejects_short_nonfinal_page(self) -> None:
        def rows(start: int, stop: int):
            return [
                {
                    "orderId": str(index),
                    "symbol": "BTC-USDT",
                    "orderSide": "BUY",
                    "orderType": "LIMIT",
                    "origQty": "1",
                    "executedQty": "0",
                    "price": "100",
                }
                for index in range(start, stop)
            ]

        credentials = {"account": "main", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        pages = [
            {"items": rows(0, 100), "total": 250},
            {"items": rows(100, 150), "total": 250},
            {"items": rows(150, 250), "total": 250},
        ]
        with mock.patch.object(azverse, "_signed_get", side_effect=pages), self.assertRaises(ValueError):
            azverse._fetch_open_orders(credentials)


if __name__ == "__main__":
    unittest.main()
