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
                 mock.patch.object(pacifica, "_http_get_json", side_effect=fake_get_json):
                response = pacifica.execute({
                    "operation": "ladder",
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


if __name__ == "__main__":
    unittest.main()
