"""Focused regression tests for Pacifica ladder orientation.

Run with:

    python3 -m unittest plugins.trade.tests.test_pacifica_agent -v
"""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from plugins.trade.agents import x_pacifica_agent as pacifica  # noqa: E402


class TestPacificaLadderOrientation(unittest.TestCase):
    def _children(
        self,
        *,
        side: str,
        distribution: str,
        order_count: int = 5,
        total_volume: str,
        start_price: str,
        end_price: str,
        size_increment: str = "0.01",
        price_increment: str = "0.01",
    ) -> tuple[list[dict[str, Any]], Decimal, int, int]:
        return pacifica._ladder_build_children(
            symbol="SOL",
            side=side,
            distribution=distribution,
            order_count=order_count,
            total_volume=Decimal(total_volume),
            start_price=Decimal(start_price),
            end_price=Decimal(end_price),
            size_increment=Decimal(size_increment),
            price_increment=Decimal(price_increment),
        )

    def test_sell_half_gaussian_prices_progress_and_sizes_increase_from_start_to_end(self) -> None:
        children, kept_volume, omitted_below_minimum, kept_count = self._children(
            side="sell",
            distribution="half_gaussian",
            total_volume="24.07",
            start_price="81.38",
            end_price="82.12",
        )
        self.assertEqual(kept_count, 5)
        self.assertEqual(omitted_below_minimum, 0)
        self.assertEqual(kept_volume, Decimal("24.07"))

        prices = [Decimal(child["price"]) for child in children]
        sizes = [Decimal(child["size"]) for child in children]
        self.assertEqual(prices, [
            Decimal("81.38"),
            Decimal("81.57"),
            Decimal("81.75"),
            Decimal("81.94"),
            Decimal("82.12"),
        ])
        self.assertLess(sizes[0], sizes[-1])
        self.assertTrue(all(left <= right for left, right in zip(sizes, sizes[1:])))
        self.assertEqual(sum(sizes, Decimal("0")), Decimal("24.07"))

    def test_buy_half_gaussian_remains_small_to_large_in_start_to_end_progression(self) -> None:
        children, kept_volume, omitted_below_minimum, kept_count = self._children(
            side="buy",
            distribution="half_gaussian",
            total_volume="30.02",
            start_price="66.60",
            end_price="65.86",
        )
        self.assertEqual(kept_count, 5)
        self.assertEqual(omitted_below_minimum, 0)
        self.assertEqual(kept_volume, Decimal("30.02"))

        prices = [Decimal(child["price"]) for child in children]
        sizes = [Decimal(child["size"]) for child in children]
        self.assertEqual(prices, [
            Decimal("66.60"),
            Decimal("66.42"),
            Decimal("66.23"),
            Decimal("66.05"),
            Decimal("65.86"),
        ])
        self.assertLess(sizes[0], sizes[-1])
        self.assertTrue(all(left <= right for left, right in zip(sizes, sizes[1:])))
        self.assertEqual(sum(sizes, Decimal("0")), Decimal("30.02"))

    def test_uniform_buy_and_sell_preserve_total_and_equal_sizes(self) -> None:
        sell_children, sell_kept, sell_omitted, sell_count = self._children(
            side="sell",
            distribution="uniform",
            total_volume="10.00",
            start_price="81.00",
            end_price="82.00",
        )
        buy_children, buy_kept, buy_omitted, buy_count = self._children(
            side="buy",
            distribution="uniform",
            total_volume="10.00",
            start_price="66.00",
            end_price="65.00",
        )
        self.assertEqual((sell_count, sell_omitted, sell_kept), (5, 0, Decimal("10.00")))
        self.assertEqual((buy_count, buy_omitted, buy_kept), (5, 0, Decimal("10.00")))
        self.assertEqual([Decimal(child["size"]) for child in sell_children], [Decimal("2.00")] * 5)
        self.assertEqual([Decimal(child["size"]) for child in buy_children], [Decimal("2.00")] * 5)

    def test_execute_ladder_preserves_builder_orientation_in_final_payload_sell_and_buy(self) -> None:
        creds = {"account": "amiroo", "address": "addr", "private_key": "sk"}
        market = {"symbol": "SOL", "tick_size": "0.01", "lot_size": "0.01"}

        def run_case(*, side: str, total_volume: str, start_price: str, end_price: str) -> list[dict[str, Any]]:
            posted: list[dict[str, Any]] = []

            def fake_post_signed(credentials: dict[str, Any], path: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
                posted.append(dict(payload))
                return {"i": str(1000 + len(posted))}

            def fake_get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                return {"data": [{"client_order_id": row["client_order_id"]} for row in posted]}

            with mock.patch.object(pacifica, "_lookup_credentials", return_value=creds), \
                 mock.patch.object(pacifica, "_get_market_info", return_value=market), \
                 mock.patch.object(pacifica, "_post_signed", side_effect=fake_post_signed), \
                 mock.patch.object(pacifica, "_http_get_json", side_effect=fake_get_json), \
                 mock.patch.object(pacifica.time, "sleep"):
                response = pacifica.execute({
                    "operation": "ladder",
                    "submission_mode": "sequential",  # preserve legacy transport contract
                    "exchange": "pacifica",
                    "account": "amiroo",
                    "symbol": "SOL",
                    "side": side,
                    "distribution": "half_gaussian",
                    "order_count": 5,
                    "total_volume": total_volume,
                    "start_price": start_price,
                    "end_price": end_price,
                })
            self.assertTrue(response.success)
            self.assertEqual(len(posted), 5)
            return posted

        sell_payloads = run_case(side="sell", total_volume="24.07", start_price="81.38", end_price="82.12")
        buy_payloads = run_case(side="buy", total_volume="30.02", start_price="66.60", end_price="65.86")

        self.assertEqual(
            [(Decimal(row["price"]), Decimal(row["amount"])) for row in sell_payloads],
            [
                (Decimal("81.38"), Decimal("0.13")),
                (Decimal("81.57"), Decimal("0.88")),
                (Decimal("81.75"), Decimal("3.60")),
                (Decimal("81.94"), Decimal("8.37")),
                (Decimal("82.12"), Decimal("11.09")),
            ],
        )
        self.assertEqual(
            [(Decimal(row["price"]), Decimal(row["amount"])) for row in buy_payloads],
            [
                (Decimal("66.60"), Decimal("0.16")),
                (Decimal("66.42"), Decimal("1.10")),
                (Decimal("66.23"), Decimal("4.49")),
                (Decimal("66.05"), Decimal("10.44")),
                (Decimal("65.86"), Decimal("13.83")),
            ],
        )


class TestPacificaLadderOutcomes(unittest.TestCase):
    def _run(self, count: int, submit: Any, snapshot: Any) -> Any:
        with mock.patch.object(pacifica, "_lookup_credentials", return_value={"account": "example", "address": "addr"}), \
             mock.patch.object(pacifica, "_get_market_info", return_value={"tick_size": "0.01", "lot_size": "0.01"}), \
             mock.patch.object(pacifica, "_post_signed", side_effect=submit), \
             mock.patch.object(pacifica, "_http_get_json", side_effect=snapshot), \
             mock.patch.object(pacifica.time, "sleep"):
            return pacifica.execute({
                "operation": "ladder", "submission_mode": "sequential",
                "account": "example", "symbol": "SOL",
                "side": "sell", "distribution": "uniform", "order_count": count,
                "total_volume": str(count), "start_price": "100", "end_price": "101",
            })

    def test_250_children_with_scattered_rejections_are_partial_not_success(self) -> None:
        attempts: list[dict[str, Any]] = []
        accepted: list[dict[str, Any]] = []

        def submit(_creds: dict[str, Any], _path: str, _op: str, payload: dict[str, Any]) -> dict[str, Any]:
            attempts.append(dict(payload))
            if len(attempts) % 2 == 0 or len(attempts) > 242:
                raise pacifica._PacificaHTTPError(status=422, path="/orders/create", body="insufficient balance")
            accepted.append(dict(payload))
            return {"i": str(len(attempts))}

        response = self._run(250, submit, lambda _url, params=None: {
            "data": [{"client_order_id": row["client_order_id"]} for row in accepted]
        })
        self.assertEqual((len(attempts), len(accepted)), (250, 121))
        self.assertEqual(len({row["client_order_id"] for row in attempts}), 250)
        self.assertEqual(response.ladder.status, "partial_success")
        self.assertFalse(response.success)
        self.assertEqual(response.ladder.accepted_child_count, 121)
        self.assertEqual(response.ladder.rejected_child_count, 129)
        self.assertEqual(response.ladder.uncertain_child_count, 0)
        self.assertEqual(
            response.ladder.accepted_child_count + response.ladder.rejected_child_count
            + response.ladder.uncertain_child_count, 250,
        )
        self.assertEqual(response.ladder.batches[1]["index"], 1)

    def test_timeout_reconciled_by_client_id_without_retry(self) -> None:
        attempts: list[dict[str, Any]] = []

        def submit(_creds: dict[str, Any], _path: str, _op: str, payload: dict[str, Any]) -> dict[str, Any]:
            attempts.append(dict(payload))
            if len(attempts) == 2:
                raise TimeoutError("read timed out")
            return {"i": str(len(attempts))}

        response = self._run(3, submit, lambda _url, params=None: {
            "data": [{"client_order_id": row["client_order_id"]} for row in attempts]
        })
        self.assertEqual(len(attempts), 3)
        self.assertEqual(response.ladder.accepted_child_count, 3)
        self.assertEqual(response.ladder.uncertain_child_count, 0)
        self.assertEqual(response.ladder.status, "success")


    def test_rate_limit_is_rejected_without_unsafe_retry(self) -> None:
        attempts: list[dict[str, Any]] = []

        def submit(_creds: dict[str, Any], _path: str, _op: str, payload: dict[str, Any]) -> dict[str, Any]:
            attempts.append(dict(payload))
            if len(attempts) == 2:
                raise pacifica._PacificaHTTPError(status=429, path="/orders/create", body="rate limited")
            return {"i": str(len(attempts))}

        response = self._run(3, submit, lambda _url, params=None: {
            "data": [{"client_order_id": row["client_order_id"]} for i, row in enumerate(attempts) if i != 1]
        })
        self.assertEqual(len(attempts), 3)
        self.assertEqual(response.ladder.status, "partial_success")
        self.assertEqual(response.ladder.rejected_child_count, 1)
        self.assertTrue(response.ladder.rate_limited)

    def test_unresolved_timeout_is_not_counted_as_rejection(self) -> None:
        attempts: list[dict[str, Any]] = []

        def submit(_creds: dict[str, Any], _path: str, _op: str, payload: dict[str, Any]) -> dict[str, Any]:
            attempts.append(dict(payload))
            if len(attempts) == 2:
                raise TimeoutError("read timed out")
            return {"i": str(len(attempts))}

        response = self._run(3, submit, lambda _url, params=None: {
            "data": [{"client_order_id": row["client_order_id"]} for i, row in enumerate(attempts) if i != 1]
        })
        self.assertEqual(len(attempts), 3)
        self.assertEqual(response.ladder.status, "partial_success")
        self.assertEqual(response.ladder.accepted_child_count, 2)
        self.assertEqual(response.ladder.rejected_child_count, 0)
        self.assertEqual(response.ladder.uncertain_child_count, 1)


    def test_timeout_found_in_order_history_counts_as_accepted(self) -> None:
        attempts: list[dict[str, Any]] = []

        def submit(_creds: dict[str, Any], _path: str, _op: str, payload: dict[str, Any]) -> dict[str, Any]:
            attempts.append(dict(payload))
            if len(attempts) == 2:
                raise TimeoutError("read timed out")
            return {"i": str(len(attempts))}

        def read(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            if url.endswith("/orders/history"):
                return {"data": [{"client_order_id": attempts[1]["client_order_id"],
                                  "order_status": "filled", "order_id": 902}], "has_more": False}
            return {"data": [{"client_order_id": row["client_order_id"]}
                             for i, row in enumerate(attempts) if i != 1]}

        response = self._run(3, submit, read)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(response.ladder.accepted_child_count, 3)
        self.assertEqual(response.ladder.uncertain_child_count, 0)
        self.assertEqual(response.ladder.status, "success")


    def test_scattered_failures_emit_per_child_diagnostic_indices(self) -> None:
        posted: list[dict[str, Any]] = []

        def submit(_creds: dict[str, Any], _path: str, _op: str, payload: dict[str, Any]) -> dict[str, Any]:
            posted.append(dict(payload))
            if len(posted) == 2:
                raise pacifica._PacificaHTTPError(status=422, path="/orders/create", body="insufficient balance")
            return {"i": str(len(posted))}

        with self.assertLogs(pacifica.logger, level="WARNING") as captured:
            self._run(3, submit, lambda _url, params=None: {
                "data": [{"client_order_id": row["client_order_id"]} for i, row in enumerate(posted) if i != 1]
            })
        self.assertTrue(any("index=1" in line and "insufficient balance" in line for line in captured.output))
        self.assertTrue(any("index=0" in line and "outcome=accepted" in line for line in captured.output))


    def test_all_explicit_rejections_are_failed_with_exchange_reason(self) -> None:
        def reject(*_args: Any) -> dict[str, Any]:
            raise pacifica._PacificaHTTPError(status=422, path="/orders/create", body="insufficient balance")

        response = self._run(3, reject, lambda _url, params=None: {"data": []})
        self.assertEqual(response.ladder.status, "failed")
        self.assertFalse(response.ladder.partial)
        self.assertEqual(response.ladder.rejected_child_count, 3)
        self.assertIn("insufficient balance", response.ladder.exchange_reason)


    def test_history_rejected_timeout_is_rejected_not_uncertain(self) -> None:
        attempts: list[dict[str, Any]] = []

        def submit(_creds: dict[str, Any], _path: str, _op: str, payload: dict[str, Any]) -> dict[str, Any]:
            attempts.append(dict(payload))
            raise TimeoutError("read timed out")

        def read(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            if url.endswith("/orders/history"):
                return {"data": [{"client_order_id": row["client_order_id"],
                                  "order_status": "rejected", "order_id": i,
                                  "reason": "margin unavailable"}
                                 for i, row in enumerate(attempts)], "has_more": False}
            return {"data": []}

        response = self._run(3, submit, read)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(response.ladder.status, "failed")
        self.assertEqual(response.ladder.rejected_child_count, 3)
        self.assertEqual(response.ladder.uncertain_child_count, 0)
        self.assertIn("margin unavailable", response.ladder.exchange_reason)


class TestPacificaWizardReporting(unittest.TestCase):
    def test_partial_ladder_shows_counts_and_rejection_reason(self) -> None:
        from plugins.trade.wizard import TradeWizard

        ladder = pacifica.CanonicalLadderResult(
            symbol="SOL", side="sell", distribution="uniform",
            requested_order_count=250, submitted_order_count=121,
            requested_volume="250", submitted_volume="121", batch_count=250,
            verified=True, partial=True, status="partial_success",
            accepted_child_count=121, rejected_child_count=129,
            uncertain_child_count=0, exchange_reason="insufficient balance",
        )
        response = pacifica.make_failure(
            operation="ladder", exchange="pacifica", account="example",
            code="LADDER_PARTIAL_SUCCESS", message="Partial submission", ladder=ladder,
        )
        desk = mock.Mock()
        desk.execute.return_value = response
        wizard = TradeWizard(tradedesk=desk)
        key = ("offline",)
        state = wizard._state_for(key)
        state.exchange, state.account = "pacifica", "example"
        state.ladder.update({
            "symbol": "SOL", "side": "sell", "distribution": "uniform",
            "order_count": "250", "total_volume": "250",
            "start_price": "100", "end_price": "101",
        })
        screen = wizard._handle_ladder_confirm(key, "confirm")
        self.assertIn("Status: partial_success", screen.text)
        self.assertIn("Accepted: 121", screen.text)
        self.assertIn("Rejected: 129", screen.text)
        self.assertIn("Uncertain: 0", screen.text)
        self.assertIn("insufficient balance", screen.text)
        self.assertNotIn("Submitted.\n", screen.text)


class TestPacificaCancelOrderGroupSide(unittest.TestCase):
    def test_cancel_group_keeps_canonical_buy_sell_side(self) -> None:
        group = pacifica.CanonicalOrderGroup(
            symbol="ETH",
            side="buy",
            order_count=173,
            total_size="42.9872",
            vwap="3066.7",
            min_price="2667.2",
            max_price="3242.5",
        )
        # This is the contract the wizard expects: buy/sell, not bid/ask.
        self.assertEqual(group.side, "buy")

    def test_cancel_order_group_accepts_sell_side(self) -> None:
        open_orders = [
            {"symbol": "ZEC", "side": "ask", "order_id": 191},
        ]

        with mock.patch.object(pacifica, "_lookup_credentials", return_value={"account": "amiroo", "address": "addr"}), \
             mock.patch.object(pacifica, "_canonical_market_symbol", return_value="ZEC"), \
             mock.patch.object(pacifica, "_get_open_orders", side_effect=[open_orders, []]), \
             mock.patch.object(pacifica, "_signed_cancel_order", return_value=(True, "ok")) as signed_cancel:
            resp = pacifica.execute({
                "operation": "cancel_order_group",
                "account": "amiroo",
                "symbol": "ZEC",
                "side": "sell",
            })

        self.assertTrue(resp.success)
        self.assertIsNotNone(resp.cancel_group)
        cg = resp.cancel_group
        assert cg is not None
        self.assertEqual(cg.side, "sell")
        self.assertEqual(cg.targeted_order_count, 1)
        self.assertEqual(cg.confirmed_absent_count, 1)
        signed_cancel.assert_called_once()


if __name__ == "__main__":
    unittest.main()
