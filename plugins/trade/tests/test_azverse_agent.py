from __future__ import annotations

import hashlib
import hmac
import json
import os
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
    def setUp(self) -> None:
        env_patch = mock.patch.dict(
            os.environ,
            {"AZVERSE_WRITE_ENABLED": "", "AZVERSE_DRY_RUN": "1"},
            clear=False,
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

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

    def test_capabilities_include_position_actions(self) -> None:
        caps = azverse.capabilities()
        self.assertEqual(
            caps,
            [
                "balance",
                "positions_orders",
                "positions_management",
                "new_order",
                "cancel_orders",
                "cancel_order_group",
                "list_instruments",
                "resolve_instrument",
                "market_price",
                "ladder",
                "close_position",
                "set_tp",
                "set_sl",
            ],
        )

    def test_unadvertised_mutations_are_not_implemented_without_http(self) -> None:
        with mock.patch.object(azverse, "_signed_get") as signed:
            for operation in ("transfer", "withdraw"):
                with self.subTest(operation=operation):
                    response = azverse.execute({"operation": operation, "exchange": "azverse", "account": "amiroo"})
                    self.assertFalse(response.success)
                    self.assertEqual(response.error.code, "NOT_IMPLEMENTED")
        signed.assert_not_called()

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
            if path == azverse.PROFIT_LIST_PATH:
                return {"page": 1, "ps": 100, "total": 0, "items": []}
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

    def test_positions_orders_overlays_tp_sl_from_profit_list_when_position_fields_are_empty(self) -> None:
        positions = [
            {
                "symbol": "zec_usdt",
                "positionSide": "SHORT",
                "positionSize": "50",
                "entryPrice": "1240.16",
                "markPrice": "1256",
                "profitId": None,
                "triggerProfitPrice": None,
                "triggerStopPrice": None,
            }
        ]
        profits = {
            "page": 1,
            "ps": 100,
            "total": 2,
            "items": [
                {
                    "profitId": "p-tp",
                    "symbol": "zec_usdt",
                    "positionSide": "SHORT",
                    "state": "NOT_TRIGGERED",
                    "triggerProfitPrice": "1000",
                    "triggerStopPrice": None,
                    "origQty": "50",
                },
                {
                    "profitId": "p-sl",
                    "symbol": "zec_usdt",
                    "positionSide": "SHORT",
                    "state": "NOT_TRIGGERED",
                    "triggerProfitPrice": None,
                    "triggerStopPrice": "1400",
                    "origQty": "50",
                },
            ],
        }
        orders = {"page": 1, "ps": 100, "total": 0, "items": []}
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}

        def fake_get(_creds, path, query=None):
            if path == azverse.POSITIONS_PATH:
                return positions
            if path == azverse.PROFIT_LIST_PATH:
                self.assertEqual(query.get("symbol"), "zec_usdt")
                self.assertEqual(query.get("state"), "UNFINISHED")
                return profits
            if path == azverse.ORDERS_PATH:
                return orders
            raise AssertionError(path)

        with mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_signed_get", side_effect=fake_get
        ):
            response = azverse.execute({"operation": "positions_orders", "exchange": "azverse", "account": "amiroo"})

        self.assertTrue(response.success, response)
        position = response.positions[0]
        self.assertIsNone(positions[0]["triggerProfitPrice"])
        self.assertEqual(position.tp, "1000")
        self.assertEqual(position.sl, "1400")
        self.assertEqual(position.tp_count, 1)
        self.assertEqual(position.sl_count, 1)

    def test_positions_orders_shows_multiple_profit_rows_on_same_side(self) -> None:
        positions = [
            {
                "symbol": "zec_usdt",
                "positionSide": "SHORT",
                "positionSize": "50",
                "entryPrice": "1240.16",
                "markPrice": "1256",
                "profitId": None,
                "triggerProfitPrice": None,
                "triggerStopPrice": None,
            }
        ]
        profits = {
            "page": 1,
            "ps": 100,
            "total": 2,
            "items": [
                {
                    "profitId": "p1",
                    "symbol": "zec_usdt",
                    "positionSide": "SHORT",
                    "state": "NOT_TRIGGERED",
                    "triggerProfitPrice": "1000",
                    "triggerStopPrice": "1400",
                    "origQty": "50",
                },
                {
                    "profitId": "p2",
                    "symbol": "zec_usdt",
                    "positionSide": "SHORT",
                    "state": "NOT_TRIGGERED",
                    "triggerProfitPrice": "1000",
                    "triggerStopPrice": "1400",
                    "origQty": "50",
                },
            ],
        }
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}

        def fake_get(_creds, path, query=None):
            if path == azverse.POSITIONS_PATH:
                return positions
            if path == azverse.PROFIT_LIST_PATH:
                return profits
            if path == azverse.ORDERS_PATH:
                return {"page": 1, "ps": 100, "total": 0, "items": []}
            raise AssertionError(path)

        with mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_signed_get", side_effect=fake_get
        ):
            response = azverse.execute({"operation": "positions_orders", "exchange": "azverse", "account": "amiroo"})

        position = response.positions[0]
        self.assertEqual(position.tp, "1000")
        self.assertEqual(position.sl, "1400")
        self.assertEqual(position.tp_count, 2)
        self.assertEqual(position.sl_count, 2)

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

    def _live_write_env(self) -> mock._patch:
        return mock.patch.dict(
            os.environ,
            {"AZVERSE_WRITE_ENABLED": "1", "AZVERSE_DRY_RUN": "0"},
            clear=False,
        )

    def test_new_order_dry_run_does_not_post(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        catalog = [{"symbol": "btc_usdt", "baseCoin": "btc", "quoteCoin": "usdt", "tradeSwitch": True, "isOpenApi": True, "contractType": "PERPETUAL"}]
        posted = []
        with mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_symbol_rows", return_value=catalog
        ), mock.patch.object(azverse, "_signed_request", side_effect=lambda *a, **k: posted.append((a, k)) or {}):
            response = azverse.execute(
                {
                    "operation": "new_order",
                    "exchange": "azverse",
                    "account": "amiroo",
                    "symbol": "BTC",
                    "side": "buy",
                    "order_type": "limit",
                    "volume": "0.01",
                    "price": "75000",
                }
            )
        self.assertTrue(response.success, response)
        self.assertEqual(response.order.status, "dry_run")
        self.assertFalse(response.order.verified)
        self.assertEqual(response.order.symbol, "BTC")
        self.assertEqual(response.order.side, "buy")
        self.assertEqual(posted, [])

    def test_new_order_rejects_market_and_invalid_numbers(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        catalog = [{"symbol": "btc_usdt", "baseCoin": "btc", "quoteCoin": "usdt", "tradeSwitch": True, "isOpenApi": True, "contractType": "PERPETUAL"}]
        cases = [
            {"order_type": "market", "volume": "1", "price": "100", "code": "UNSUPPORTED_ORDER_TYPE"},
            {"order_type": "limit", "volume": "0", "price": "100", "code": "INVALID_REQUEST"},
            {"order_type": "limit", "volume": "1", "price": "-1", "code": "INVALID_REQUEST"},
        ]
        for case in cases:
            with self.subTest(case=case), mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
                azverse, "_symbol_rows", return_value=catalog
            ), mock.patch.object(azverse, "_signed_request") as posted:
                response = azverse.execute(
                    {
                        "operation": "new_order",
                        "exchange": "azverse",
                        "account": "amiroo",
                        "symbol": "btc_usdt",
                        "side": "buy",
                        "order_type": case["order_type"],
                        "volume": case["volume"],
                        "price": case["price"],
                    }
                )
            self.assertFalse(response.success)
            self.assertEqual(response.error.code, case["code"])
            posted.assert_not_called()

    def test_new_order_live_posts_limit_create_body(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        catalog = [{"symbol": "btc_usdt", "baseCoin": "btc", "quoteCoin": "usdt", "tradeSwitch": True, "isOpenApi": True, "contractType": "PERPETUAL"}]
        calls = []

        def fake_request(_creds, method, path, query=None, body=None):
            calls.append({"method": method, "path": path, "query": query, "body": body})
            if method == "POST" and path == azverse.ORDER_CREATE_PATH:
                return {"orderId": "ord-1", "origQty": "0.01", "price": "75000.0"}
            raise AssertionError((method, path))

        with self._live_write_env(), mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_symbol_rows", return_value=catalog
        ), mock.patch.object(azverse, "_signed_request", side_effect=fake_request):
            response = azverse.execute(
                {
                    "operation": "new_order",
                    "exchange": "azverse",
                    "account": "amiroo",
                    "symbol": "BTC",
                    "side": "buy",
                    "order_type": "limit",
                    "volume": "0.01",
                    "price": "75000",
                }
            )
        self.assertTrue(response.success, response)
        self.assertEqual(response.order.status, "success")
        self.assertTrue(response.order.verified)
        self.assertEqual(response.order.exchange_order_id, "ord-1")
        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["path"], azverse.ORDER_CREATE_PATH)
        self.assertEqual(calls[0]["body"]["symbol"], "btc_usdt")
        self.assertEqual(calls[0]["body"]["orderSide"], "BUY")
        self.assertEqual(calls[0]["body"]["orderType"], "LIMIT")
        self.assertEqual(calls[0]["body"]["positionSide"], "LONG")
        self.assertEqual(calls[0]["body"]["origQty"], "0.01")
        self.assertEqual(calls[0]["body"]["price"], "75000")
        self.assertEqual(calls[0]["body"]["timeInForce"], "GTC")

    def test_cancel_order_group_dry_run_does_not_post(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        orders = {
            "items": [
                {
                    "orderId": "keep-sell",
                    "symbol": "btc_usdt",
                    "orderSide": "SELL",
                    "orderType": "LIMIT",
                    "origQty": "1",
                    "executedQty": "0",
                    "price": "76000",
                },
                {
                    "orderId": "cancel-buy",
                    "symbol": "btc_usdt",
                    "orderSide": "BUY",
                    "orderType": "LIMIT",
                    "origQty": "1",
                    "executedQty": "0",
                    "price": "74000",
                },
            ],
            "total": 2,
        }
        posted = []
        with mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_signed_get", return_value=orders
        ), mock.patch.object(azverse, "_signed_request", side_effect=lambda *a, **k: posted.append((a, k)) or {}):
            response = azverse.execute(
                {
                    "operation": "cancel_order_group",
                    "exchange": "azverse",
                    "account": "amiroo",
                    "symbol": "BTC",
                    "side": "buy",
                    "order_type": "LIMIT",
                }
            )
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "AZVERSE_DRY_RUN")
        self.assertEqual(posted, [])

    def test_cancel_order_group_live_cancels_matching_ids_only(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        open_before = {
            "items": [
                {
                    "orderId": "keep-sell",
                    "symbol": "btc_usdt",
                    "orderSide": "SELL",
                    "orderType": "LIMIT",
                    "origQty": "1",
                    "executedQty": "0",
                    "price": "76000",
                },
                {
                    "orderId": "cancel-buy",
                    "symbol": "btc_usdt",
                    "orderSide": "BUY",
                    "orderType": "LIMIT",
                    "origQty": "1",
                    "executedQty": "0",
                    "price": "74000",
                },
            ],
            "total": 2,
        }
        open_after = {"items": [open_before["items"][0]], "total": 1}
        calls = []
        gets = {"n": 0}

        def fake_get(_creds, path, query=None):
            self.assertEqual(path, azverse.ORDERS_PATH)
            gets["n"] += 1
            return open_before if gets["n"] == 1 else open_after

        def fake_request(_creds, method, path, query=None, body=None):
            calls.append({"method": method, "path": path, "body": body})
            if method == "POST" and path == azverse.ORDER_CANCEL_PATH:
                return {"orderId": body["orderId"]}
            raise AssertionError((method, path))

        with self._live_write_env(), mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_signed_get", side_effect=fake_get
        ), mock.patch.object(azverse, "_signed_request", side_effect=fake_request):
            response = azverse.execute(
                {
                    "operation": "cancel_orders",
                    "exchange": "azverse",
                    "account": "amiroo",
                    "symbol": "BTC",
                    "side": "buy",
                    "order_type": "LIMIT",
                }
            )
        self.assertTrue(response.success, response)
        self.assertEqual(response.cancel_group.cancelled_order_count, 1)
        self.assertTrue(response.cancel_group.verified)
        self.assertEqual(calls, [{"method": "POST", "path": azverse.ORDER_CANCEL_PATH, "body": {"orderId": "cancel-buy"}}])
        self.assertNotIn("/order/cancel-all", [item["path"] for item in calls])

    def test_cancel_treats_unfinished_row_with_canceled_state_as_gone(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        open_before = {
            "items": [
                {
                    "orderId": "689531369764414528",
                    "symbol": "sol_usdt",
                    "orderSide": "SELL",
                    "orderType": "LIMIT",
                    "origQty": "1",
                    "executedQty": "0",
                    "price": "80",
                    "state": "UNFINISHED",
                }
            ],
            "total": 1,
        }
        open_after = {
            "items": [
                {
                    **open_before["items"][0],
                    "state": "CANCELED",
                }
            ],
            "total": 1,
        }
        gets = {"n": 0}

        def fake_get(_creds, path, query=None):
            gets["n"] += 1
            return open_before if gets["n"] == 1 else open_after

        with self._live_write_env(), mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_signed_get", side_effect=fake_get
        ), mock.patch.object(azverse, "_signed_request", return_value={"orderId": "689531369764414528"}):
            response = azverse.execute(
                {
                    "operation": "cancel_order_group",
                    "exchange": "azverse",
                    "account": "amiroo",
                    "symbol": "SOL",
                    "side": "sell",
                    "order_type": "LIMIT",
                }
            )
        self.assertTrue(response.success, response)
        self.assertEqual(response.cancel_group.cancelled_order_count, 1)
        self.assertTrue(response.cancel_group.verified)
        self.assertEqual(response.cancel_group.remaining_target_count, 0)

    def test_cancel_ack_with_still_open_list_is_unknown_not_zero_partial(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        open_orders = {
            "items": [
                {
                    "orderId": "689531369764414528",
                    "symbol": "sol_usdt",
                    "orderSide": "SELL",
                    "orderType": "LIMIT",
                    "origQty": "1",
                    "executedQty": "0",
                    "price": "80",
                    "state": "UNFINISHED",
                }
            ],
            "total": 1,
        }
        posts = []

        def fake_request(_creds, method, path, query=None, body=None):
            posts.append(path)
            return {"orderId": body["orderId"]}

        with self._live_write_env(), mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_signed_get", return_value=open_orders
        ), mock.patch.object(azverse, "_signed_request", side_effect=fake_request), mock.patch.object(azverse.time, "sleep"):
            response = azverse.execute(
                {
                    "operation": "cancel_order_group",
                    "exchange": "azverse",
                    "account": "amiroo",
                    "symbol": "SOL",
                    "side": "sell",
                    "order_type": "LIMIT",
                }
            )
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "CANCEL_STATUS_UNKNOWN")
        self.assertNotEqual(response.error.code, "PARTIAL_CANCEL")
        self.assertIn("Do not retry", response.error.message)
        self.assertEqual(posts, [azverse.ORDER_CANCEL_PATH])

    def _btc_catalog(self):
        return [
            {
                "symbol": "btc_usdt",
                "baseCoin": "btc",
                "quoteCoin": "usdt",
                "tradeSwitch": True,
                "isOpenApi": True,
                "contractType": "PERPETUAL",
                "pricePrecision": 1,
                "quantityPrecision": 3,
                "minQty": "0.001",
                "minNotional": "0.5",
                "minStepPrice": "0.1",
            }
        ]

    def test_ladder_dry_run_builds_uniform_children_without_post(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        posted = []
        with mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_symbol_rows", return_value=self._btc_catalog()
        ), mock.patch.object(azverse, "_signed_request", side_effect=lambda *a, **k: posted.append((a, k)) or {}):
            response = azverse.execute(
                {
                    "operation": "ladder",
                    "exchange": "azverse",
                    "account": "amiroo",
                    "symbol": "BTC",
                    "side": "buy",
                    "distribution": "uniform",
                    "order_count": "3",
                    "total_volume": "0.03",
                    "start_price": "100",
                    "end_price": "90",
                }
            )
        self.assertTrue(response.success, response)
        self.assertEqual(response.ladder.status, "dry_run")
        self.assertFalse(response.ladder.verified)
        self.assertEqual(response.ladder.requested_order_count, 3)
        self.assertEqual(response.ladder.submitted_order_count, 3)
        self.assertEqual(response.ladder.side, "buy")
        self.assertEqual(response.ladder.distribution, "uniform")
        children = response.data["children"]
        self.assertEqual([c["price"] for c in children], ["100", "95", "90"])
        self.assertEqual([c["origQty"] for c in children], ["0.01", "0.01", "0.01"])
        self.assertEqual(posted, [])

    def test_ladder_rejects_invalid_inputs_without_post(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        posted = []
        cases = [
            {"side": "hold", "code": "INVALID_SIDE"},
            {"order_count": "0", "code": "INVALID_ORDER_COUNT"},
            {"total_volume": "0", "code": "INVALID_VOLUME"},
            {"end_price": "110", "code": "INVALID_LADDER_DIRECTION"},
            {"distribution": "random", "code": "INVALID_DISTRIBUTION"},
        ]
        base = {
            "operation": "ladder",
            "exchange": "azverse",
            "account": "amiroo",
            "symbol": "BTC",
            "side": "buy",
            "distribution": "uniform",
            "order_count": "3",
            "total_volume": "0.03",
            "start_price": "100",
            "end_price": "90",
        }
        with mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_symbol_rows", return_value=self._btc_catalog()
        ), mock.patch.object(azverse, "_signed_request", side_effect=lambda *a, **k: posted.append((a, k)) or {}):
            for case in cases:
                payload = dict(base)
                payload.update({k: v for k, v in case.items() if k != "code"})
                with self.subTest(case=case):
                    response = azverse.execute(payload)
                    self.assertFalse(response.success)
                    self.assertEqual(response.error.code, case["code"])
        self.assertEqual(posted, [])

    def test_ladder_live_posts_limit_children_only(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        calls = []

        def fake_request(_creds, method, path, query=None, body=None):
            calls.append({"method": method, "path": path, "body": body})
            return {"orderId": f"id-{len(calls)}"}

        with self._live_write_env(), mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_symbol_rows", return_value=self._btc_catalog()
        ), mock.patch.object(azverse, "_signed_request", side_effect=fake_request):
            response = azverse.execute(
                {
                    "operation": "ladder",
                    "exchange": "azverse",
                    "account": "amiroo",
                    "symbol": "BTC",
                    "side": "sell",
                    "distribution": "uniform",
                    "order_count": "2",
                    "total_volume": "0.02",
                    "start_price": "90",
                    "end_price": "100",
                }
            )
        self.assertTrue(response.success, response)
        self.assertEqual(response.ladder.submitted_order_count, 2)
        self.assertEqual(response.ladder.child_order_ids, ["id-1", "id-2"])
        self.assertEqual([item["path"] for item in calls], [azverse.ORDER_CREATE_PATH, azverse.ORDER_CREATE_PATH])
        self.assertEqual(calls[0]["body"]["symbol"], "btc_usdt")
        self.assertEqual(calls[0]["body"]["orderType"], "LIMIT")
        self.assertEqual(calls[0]["body"]["orderSide"], "SELL")
        self.assertEqual(calls[0]["body"]["positionSide"], "SHORT")
        self.assertNotIn("/order/cancel-all", [item["path"] for item in calls])

    def test_ladder_timeout_after_ack_is_unknown_and_does_not_retry(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        calls = []

        def fake_request(_creds, method, path, query=None, body=None):
            calls.append(path)
            if len(calls) == 1:
                return {"orderId": "ok-1"}
            raise RuntimeError("AZverse request failed: timed out")

        with self._live_write_env(), mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_symbol_rows", return_value=self._btc_catalog()
        ), mock.patch.object(azverse, "_signed_request", side_effect=fake_request):
            response = azverse.execute(
                {
                    "operation": "ladder",
                    "exchange": "azverse",
                    "account": "amiroo",
                    "symbol": "BTC",
                    "side": "buy",
                    "distribution": "uniform",
                    "order_count": "2",
                    "total_volume": "0.02",
                    "start_price": "100",
                    "end_price": "90",
                }
            )
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "ORDER_STATUS_UNKNOWN")
        self.assertEqual(calls, [azverse.ORDER_CREATE_PATH, azverse.ORDER_CREATE_PATH])
        self.assertEqual(response.ladder.submitted_order_count, 1)
        self.assertIn("Do not retry", response.error.message)

    def _short_zec_position(self, **overrides):
        row = {
            "symbol": "zec_usdt",
            "positionSide": "SHORT",
            "positionSize": "50",
            "entryPrice": "1240.16",
            "markPrice": "1256",
            "profitId": None,
            "triggerProfitPrice": None,
            "triggerStopPrice": None,
        }
        row.update(overrides)
        return row

    def test_close_position_dry_run_does_not_post(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        posted = []
        with mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_signed_get", return_value=[self._short_zec_position()]
        ), mock.patch.object(azverse, "_signed_request", side_effect=lambda *a, **k: posted.append((a, k)) or {}):
            response = azverse.execute(
                {"operation": "close_position", "exchange": "azverse", "account": "amiroo", "symbol": "ZEC", "side": "sell"}
            )
        self.assertTrue(response.success, response)
        self.assertEqual(response.position_action.status, "dry_run")
        self.assertFalse(response.position_action.verified)
        self.assertEqual(posted, [])

    def test_close_position_live_posts_reduce_only_market_not_close_all(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        calls = []
        gets = {"n": 0}

        def fake_get(_creds, path, query=None):
            gets["n"] += 1
            if gets["n"] == 1:
                return [self._short_zec_position()]
            return []

        def fake_request(_creds, method, path, query=None, body=None):
            calls.append({"method": method, "path": path, "body": body})
            return {"orderId": "close-1"}

        with self._live_write_env(), mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_signed_get", side_effect=fake_get
        ), mock.patch.object(azverse, "_signed_request", side_effect=fake_request):
            response = azverse.execute(
                {"operation": "close_position", "exchange": "azverse", "account": "amiroo", "symbol": "ZEC", "side": "sell"}
            )
        self.assertTrue(response.success, response)
        self.assertTrue(response.position_action.verified)
        self.assertEqual(calls[0]["path"], azverse.ORDER_CREATE_PATH)
        self.assertEqual(calls[0]["body"]["orderType"], "MARKET")
        self.assertEqual(calls[0]["body"]["orderSide"], "BUY")
        self.assertEqual(calls[0]["body"]["positionSide"], "SHORT")
        self.assertEqual(calls[0]["body"]["origQty"], "50")
        self.assertTrue(calls[0]["body"]["reduceOnly"])
        self.assertNotIn("/position/close-all", [item["path"] for item in calls])
        self.assertNotIn("/cancel-all", [item["path"] for item in calls])

    def test_set_tp_live_creates_profit_entrust_and_zero_cancels_id(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        calls = []

        def fake_get(_creds, path, query=None):
            if path == azverse.POSITIONS_PATH:
                return [self._short_zec_position()]
            if path == azverse.PROFIT_LIST_PATH:
                if any(item["path"] == azverse.PROFIT_CANCEL_PATH for item in calls):
                    return {"items": [], "total": 0}
                if any(item["path"] == azverse.PROFIT_CREATE_PATH for item in calls):
                    return {
                        "items": [
                            {
                                "profitId": "p1",
                                "symbol": "zec_usdt",
                                "positionSide": "SHORT",
                                "state": "NOT_TRIGGERED",
                                "triggerProfitPrice": "1100",
                                "triggerStopPrice": None,
                                "origQty": "50",
                            }
                        ],
                        "total": 1,
                    }
                return {"items": [], "total": 0}
            return {"items": [], "total": 0}

        def fake_request(_creds, method, path, query=None, body=None):
            calls.append({"path": path, "body": body})
            if path == azverse.PROFIT_CREATE_PATH:
                return {"profitId": "p1"}
            if path == azverse.PROFIT_CANCEL_PATH:
                return {"profitId": body["profitId"]}
            raise AssertionError(path)

        with self._live_write_env(), mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_signed_get", side_effect=fake_get
        ), mock.patch.object(azverse, "_signed_request", side_effect=fake_request):
            created = azverse.execute(
                {"operation": "set_tp", "exchange": "azverse", "account": "amiroo", "symbol": "ZEC", "side": "sell", "price": "1100"}
            )
            removed = azverse.execute(
                {"operation": "set_tp", "exchange": "azverse", "account": "amiroo", "symbol": "ZEC", "side": "sell", "price": "0"}
            )
        self.assertTrue(created.success, created)
        self.assertEqual(created.position_action.price, "1100")
        self.assertFalse(created.position_action.removed)
        self.assertEqual(calls[0]["path"], azverse.PROFIT_CREATE_PATH)
        self.assertEqual(calls[0]["body"]["triggerProfitPrice"], "1100")
        self.assertNotIn("triggerStopPrice", calls[0]["body"])
        self.assertTrue(removed.success, removed)
        self.assertTrue(removed.position_action.removed)
        self.assertEqual(calls[1]["path"], azverse.PROFIT_CANCEL_PATH)
        self.assertEqual(calls[1]["body"], {"profitId": "p1"})
        self.assertNotIn("cancel-all-profit-stop", [item["path"] for item in calls])

    def test_set_sl_updates_existing_profit_id_without_cancel_all(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        calls = []

        def fake_get(_creds, path, query=None):
            if path == azverse.POSITIONS_PATH:
                return [self._short_zec_position()]
            if path == azverse.PROFIT_LIST_PATH:
                if calls:
                    return {
                        "items": [
                            {
                                "profitId": "p1",
                                "symbol": "zec_usdt",
                                "positionSide": "SHORT",
                                "state": "NOT_TRIGGERED",
                                "triggerProfitPrice": "1100",
                                "triggerStopPrice": "1300",
                                "origQty": "50",
                            }
                        ],
                        "total": 1,
                    }
                return {
                    "items": [
                        {
                            "profitId": "p1",
                            "symbol": "zec_usdt",
                            "positionSide": "SHORT",
                            "state": "NOT_TRIGGERED",
                            "triggerProfitPrice": "1100",
                            "triggerStopPrice": None,
                            "origQty": "50",
                        }
                    ],
                    "total": 1,
                }
            return {"items": [], "total": 0}

        def fake_request(_creds, method, path, query=None, body=None):
            calls.append({"path": path, "body": body})
            return {"profitId": "p1"}

        with self._live_write_env(), mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_signed_get", side_effect=fake_get
        ), mock.patch.object(azverse, "_signed_request", side_effect=fake_request):
            response = azverse.execute(
                {"operation": "set_sl", "exchange": "azverse", "account": "amiroo", "symbol": "ZEC", "side": "sell", "price": "1300"}
            )
        self.assertTrue(response.success, response)
        self.assertEqual(calls[0]["path"], azverse.PROFIT_UPDATE_PATH)
        self.assertEqual(calls[0]["body"]["profitId"], "p1")
        self.assertEqual(calls[0]["body"]["triggerStopPrice"], "1300")
        self.assertEqual(calls[0]["body"]["triggerProfitPrice"], "1100")
        self.assertNotIn("cancel-all", "".join(item["path"] for item in calls))

    def test_set_tp_verifies_against_profit_list_when_position_fields_are_empty(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        posted = {"n": 0}

        def fake_get(_creds, path, query=None):
            if path == azverse.POSITIONS_PATH:
                return [self._short_zec_position()]
            if path == azverse.PROFIT_LIST_PATH:
                if posted["n"]:
                    return {
                        "items": [
                            {
                                "profitId": "689539800181236800",
                                "symbol": "zec_usdt",
                                "positionSide": "SHORT",
                                "state": "NOT_TRIGGERED",
                                "triggerProfitPrice": "1000",
                                "origQty": "50",
                            }
                        ],
                        "total": 1,
                    }
                return {"items": [], "total": 0}
            return {"items": [], "total": 0}

        def fake_request(_creds, method, path, query=None, body=None):
            posted["n"] += 1
            return {"profitId": "689539800181236800"}

        with self._live_write_env(), mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_signed_get", side_effect=fake_get
        ), mock.patch.object(azverse, "_signed_request", side_effect=fake_request):
            response = azverse.execute(
                {"operation": "set_tp", "exchange": "azverse", "account": "amiroo", "symbol": "ZEC", "side": "sell", "price": "1000"}
            )
        self.assertTrue(response.success, response)
        self.assertTrue(response.position_action.verified)
        self.assertEqual(response.position_action.price, "1000")

    def test_set_tp_ack_without_profit_list_row_is_unknown_not_verification_failed(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}

        def fake_get(_creds, path, query=None):
            if path == azverse.POSITIONS_PATH:
                return [self._short_zec_position()]
            return {"items": [], "total": 0}

        with self._live_write_env(), mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_signed_get", side_effect=fake_get
        ), mock.patch.object(azverse, "_signed_request", return_value={"profitId": "p1"}), mock.patch.object(azverse.time, "sleep"):
            response = azverse.execute(
                {"operation": "set_tp", "exchange": "azverse", "account": "amiroo", "symbol": "ZEC", "side": "sell", "price": "1000"}
            )
        self.assertFalse(response.success)
        self.assertEqual(response.error.code, "PROTECTION_STATUS_UNKNOWN")
        self.assertNotEqual(response.error.code, "VERIFICATION_FAILED")
        self.assertIn("Do not retry", response.error.message)

    def test_remove_sl_cancels_sl_profit_even_when_first_row_is_tp_only(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        calls = []

        def fake_get(_creds, path, query=None):
            if path == azverse.POSITIONS_PATH:
                return [self._short_zec_position()]
            if path == azverse.PROFIT_LIST_PATH:
                if any(item["path"] == azverse.PROFIT_CANCEL_PATH for item in calls):
                    return {
                        "items": [
                            {
                                "profitId": "p-tp",
                                "symbol": "zec_usdt",
                                "positionSide": "SHORT",
                                "state": "NOT_TRIGGERED",
                                "triggerProfitPrice": "1000",
                                "triggerStopPrice": None,
                                "origQty": "50",
                            }
                        ],
                        "total": 1,
                    }
                return {
                    "items": [
                        {
                            "profitId": "p-tp",
                            "symbol": "zec_usdt",
                            "positionSide": "SHORT",
                            "state": "NOT_TRIGGERED",
                            "triggerProfitPrice": "1000",
                            "triggerStopPrice": None,
                            "origQty": "50",
                        },
                        {
                            "profitId": "p-sl",
                            "symbol": "zec_usdt",
                            "positionSide": "SHORT",
                            "state": "NOT_TRIGGERED",
                            "triggerProfitPrice": None,
                            "triggerStopPrice": "1400",
                            "origQty": "50",
                        },
                    ],
                    "total": 2,
                }
            return {"items": [], "total": 0}

        def fake_request(_creds, method, path, query=None, body=None):
            calls.append({"path": path, "body": body})
            return {"profitId": body.get("profitId")}

        with self._live_write_env(), mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_signed_get", side_effect=fake_get
        ), mock.patch.object(azverse, "_signed_request", side_effect=fake_request):
            response = azverse.execute(
                {"operation": "set_sl", "exchange": "azverse", "account": "amiroo", "symbol": "ZEC", "side": "sell", "price": "0"}
            )
        self.assertTrue(response.success, response)
        self.assertTrue(response.position_action.removed)
        self.assertTrue(response.position_action.verified)
        self.assertEqual(response.position_action.operation, "set_sl")
        self.assertEqual([item["path"] for item in calls], [azverse.PROFIT_CANCEL_PATH])
        self.assertEqual(calls[0]["body"], {"profitId": "p-sl"})
        self.assertNotEqual(response.error.code if response.error else None, "PROTECTION_STATUS_UNKNOWN")

    def test_remove_sl_ack_treats_stale_profit_list_row_as_gone(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        calls = []
        stale = {
            "items": [
                {
                    "profitId": "p-tp",
                    "symbol": "zec_usdt",
                    "positionSide": "SHORT",
                    "state": "NOT_TRIGGERED",
                    "triggerProfitPrice": "1000",
                    "triggerStopPrice": None,
                    "origQty": "50",
                },
                {
                    "profitId": "p-sl",
                    "symbol": "zec_usdt",
                    "positionSide": "SHORT",
                    "state": "NOT_TRIGGERED",
                    "triggerProfitPrice": None,
                    "triggerStopPrice": "1400",
                    "origQty": "50",
                },
            ],
            "total": 2,
        }

        def fake_get(_creds, path, query=None):
            if path == azverse.POSITIONS_PATH:
                return [self._short_zec_position()]
            if path == azverse.PROFIT_LIST_PATH:
                return stale
            return {"items": [], "total": 0}

        def fake_request(_creds, method, path, query=None, body=None):
            calls.append({"path": path, "body": body})
            return {"profitId": body.get("profitId")}

        with self._live_write_env(), mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_signed_get", side_effect=fake_get
        ), mock.patch.object(azverse, "_signed_request", side_effect=fake_request), mock.patch.object(azverse.time, "sleep"):
            response = azverse.execute(
                {"operation": "set_sl", "exchange": "azverse", "account": "amiroo", "symbol": "ZEC", "side": "sell", "price": "0"}
            )
        self.assertTrue(response.success, response)
        self.assertTrue(response.position_action.removed)
        self.assertTrue(response.position_action.verified)
        self.assertEqual(calls[0]["body"], {"profitId": "p-sl"})
        self.assertNotEqual(getattr(response.error, "code", None), "PROTECTION_STATUS_UNKNOWN")

    def test_remove_tp_cancels_every_tp_row_not_only_the_first(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        calls = []

        def fake_get(_creds, path, query=None):
            if path == azverse.POSITIONS_PATH:
                return [self._short_zec_position()]
            if path == azverse.PROFIT_LIST_PATH:
                remaining = [
                    {
                        "profitId": "p-tp-1",
                        "symbol": "zec_usdt",
                        "positionSide": "SHORT",
                        "state": "NOT_TRIGGERED",
                        "triggerProfitPrice": "1000",
                        "triggerStopPrice": None,
                        "origQty": "50",
                    },
                    {
                        "profitId": "p-tp-2",
                        "symbol": "zec_usdt",
                        "positionSide": "SHORT",
                        "state": "NOT_TRIGGERED",
                        "triggerProfitPrice": "1000",
                        "triggerStopPrice": None,
                        "origQty": "50",
                    },
                ]
                cancelled = {item["body"]["profitId"] for item in calls if item["path"] == azverse.PROFIT_CANCEL_PATH}
                items = [row for row in remaining if row["profitId"] not in cancelled]
                return {"items": items, "total": len(items)}
            return {"items": [], "total": 0}

        def fake_request(_creds, method, path, query=None, body=None):
            calls.append({"path": path, "body": body})
            return {"profitId": body.get("profitId")}

        with self._live_write_env(), mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_signed_get", side_effect=fake_get
        ), mock.patch.object(azverse, "_signed_request", side_effect=fake_request), mock.patch.object(azverse.time, "sleep"):
            response = azverse.execute(
                {"operation": "set_tp", "exchange": "azverse", "account": "amiroo", "symbol": "ZEC", "side": "sell", "price": "0"}
            )
        self.assertTrue(response.success, response)
        self.assertTrue(response.position_action.verified)
        self.assertEqual([item["body"]["profitId"] for item in calls], ["p-tp-1", "p-tp-2"])
        self.assertTrue(all(item["path"] == azverse.PROFIT_CANCEL_PATH for item in calls))
        self.assertNotEqual(getattr(response.error, "code", None), "PROTECTION_STATUS_UNKNOWN")

    def test_signed_post_includes_json_body_in_hmac_and_rejects_redirects(self) -> None:
        creds = {"account": "amiroo", "api_key": "app-key", "secret": "super-secret", "api_base": azverse.DEFAULT_API_BASE}
        captured = {}

        class _Resp:
            def read(self):
                return json.dumps({"returnCode": 0, "result": {"orderId": "x"}}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_open(request, timeout):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            captured["body"] = request.data
            captured["timeout"] = timeout
            return _Resp()

        with mock.patch.object(azverse, "_open_no_redirect", side_effect=fake_open):
            result = azverse._signed_request(
                creds,
                "POST",
                azverse.ORDER_CREATE_PATH,
                body={"symbol": "btc_usdt", "orderSide": "BUY"},
            )
        self.assertEqual(result, {"orderId": "x"})
        body = captured["body"].decode("utf-8")
        self.assertEqual(body, '{"symbol":"btc_usdt","orderSide":"BUY"}')
        signature = captured["headers"]["validate-signature"]
        timestamp = captured["headers"]["validate-timestamp"]
        expected = hmac.new(
            b"super-secret",
            f"validate-appkey=app-key&validate-timestamp={timestamp}#{azverse.ORDER_CREATE_PATH}#{body}".encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(signature, expected)
        self.assertNotIn("super-secret", body)

    def test_list_resolve_and_market_price_use_public_catalog(self) -> None:
        creds = {"account": "amiroo", "api_key": "key", "secret": "secret", "api_base": azverse.DEFAULT_API_BASE}
        catalog = {
            "symbols": [
                {
                    "symbol": "btc_usdt",
                    "enName": "BTCUSDT Perpetual",
                    "baseCoin": "btc",
                    "quoteCoin": "usdt",
                    "tradeSwitch": True,
                    "isOpenApi": True,
                    "isDisplay": True,
                    "contractType": "PERPETUAL",
                    "minQty": "0.001",
                    "minNotional": "5",
                    "minStepPrice": "0.1",
                    "quantityPrecision": 3,
                    "pricePrecision": 1,
                }
            ]
        }
        ticker = {"s": "btc_usdt", "c": "75753.5", "mx": "75753.5"}
        mark = {"s": "btc_usdt", "p": "75754.1"}

        def fake_public(_path, query=None):
            if _path == azverse.SYMBOLS_PATH:
                return catalog
            if _path == azverse.TICKER_PATH:
                return ticker
            if _path == azverse.MARK_PATH:
                return mark
            raise AssertionError(_path)

        with mock.patch.object(azverse, "_lookup_credentials", return_value=creds), mock.patch.object(
            azverse, "_public_get", side_effect=fake_public
        ):
            listed = azverse.execute({"operation": "list_instruments", "account": "amiroo", "query": "BTC"})
            resolved = azverse.execute({"operation": "resolve_instrument", "account": "amiroo", "symbol": "BTC"})
            priced = azverse.execute({"operation": "market_price", "account": "amiroo", "symbol": "BTC"})
        self.assertTrue(listed.success, listed)
        self.assertEqual(listed.data["instruments"][0]["native_symbol"], "btc_usdt")
        self.assertTrue(resolved.success, resolved)
        self.assertEqual(resolved.instrument.symbol, "btc_usdt")
        self.assertTrue(priced.success, priced)
        self.assertEqual(priced.market_price.mark_price, "75754.1")
        self.assertEqual(priced.market_price.price, "75753.5")


if __name__ == "__main__":
    unittest.main()
