"""TradeMenu New Order + Ladder preview/execute tests (FakeDesk only)."""

from __future__ import annotations

import unittest
from decimal import Decimal
from typing import Any, Dict, List
from unittest import mock

from fastapi.testclient import TestClient

from plugins.trade.canonical import (
    CanonicalInstrument,
    CanonicalLadderResult,
    CanonicalOrderResult,
    make_failure,
    make_success,
)
from plugins.trade.ladder_math import build_ladder_children, ladder_vwap
from plugins.trade.trademenu.app import create_app
from plugins.trade.trademenu.config import TradeMenuConfig
from plugins.trade.trademenu.preview_plans import PreviewPlanStore
from plugins.trade.trademenu.service import TradeMenuService


def _cfg() -> TradeMenuConfig:
    cfg = object.__new__(TradeMenuConfig)
    cfg.password = "test-password-xyz"
    cfg.hint = "unit-test-hint"
    cfg.session_secret = "unit-test-session-secret-value-32b"
    cfg.cookie_name = "trademenu_session"
    cfg.session_max_age_seconds = 3600
    cfg.login_max_failures = 3
    cfg.login_lockout_seconds = 2
    cfg.host = "0.0.0.0"
    cfg.port = 8001
    return cfg


class TradeDeskFake:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._exchanges = ["hyperliquid"]
        self._accounts = {"hyperliquid": ["FLEX", "FIBO"]}

    def list_exchanges(self) -> List[str]:
        return list(self._exchanges)

    def list_accounts(self, exchange: str) -> List[Any]:
        return list(self._accounts.get(exchange, []))

    def capabilities(self, exchange: str) -> List[str]:
        return [
            "resolve_instrument",
            "market_price",
            "positions_orders",
            "new_order",
            "ladder",
            "set_tp",
            "set_sl",
            "close_position",
            "cancel_order_group",
        ]

    def execute(self, request: Dict[str, Any]):
        self.calls.append(dict(request))
        op = str(request.get("operation") or "")
        ex = str(request.get("exchange") or "")
        acct = str(request.get("account") or "")
        if op == "resolve_instrument":
            return make_success(
                op,
                ex,
                acct,
                instrument=CanonicalInstrument(
                    requested_symbol=str(request.get("symbol") or ""),
                    symbol="BTC",
                    display_name="BTC",
                    price_increment="0.1",
                    size_increment="0.001",
                ),
            )
        if op == "new_order":
            return make_success(
                op,
                ex,
                acct,
                order=CanonicalOrderResult(
                    symbol=str(request.get("symbol") or ""),
                    side=str(request.get("side") or ""),
                    order_type="limit",
                    requested_volume=str(request.get("volume") or ""),
                    requested_price=str(request.get("price") or ""),
                    submitted_volume=str(request.get("volume") or ""),
                    submitted_price=str(request.get("price") or ""),
                    verified=True,
                ),
            )
        if op == "ladder":
            n = int(request.get("order_count") or 0)
            return make_success(
                op,
                ex,
                acct,
                ladder=CanonicalLadderResult(
                    symbol=str(request.get("symbol") or ""),
                    side=str(request.get("side") or ""),
                    distribution=str(request.get("distribution") or ""),
                    requested_order_count=n,
                    submitted_order_count=n,
                    requested_volume=str(request.get("total_volume") or ""),
                    submitted_volume=str(request.get("total_volume") or ""),
                    batch_count=1,
                    verified=True,
                    accepted_child_count=n,
                ),
            )
        return make_failure(op, ex, acct, "NOT_IMPLEMENTED", "nope")


class LadderMathTests(unittest.TestCase):
    def test_uniform_equal_sizes_and_vwap(self) -> None:
        children, total, vwap = build_ladder_children(
            side="buy",
            distribution="uniform",
            order_count=4,
            total_volume=Decimal("1.0"),
            start_price=Decimal("100"),
            end_price=Decimal("90"),
            size_increment=Decimal("0.01"),
            price_increment=Decimal("1"),
        )
        self.assertEqual(len(children), 4)
        sizes = [Decimal(c["size"]) for c in children]
        self.assertTrue(all(s == sizes[0] for s in sizes))
        self.assertEqual(sum(sizes), total)
        # Independent VWAP check
        self.assertEqual(vwap, ladder_vwap(children))
        # Uniform prices equal weight → mid-ish
        self.assertGreater(vwap, Decimal("90"))
        self.assertLess(vwap, Decimal("100"))

    def test_half_gaussian_unequal_and_buy_direction(self) -> None:
        children, total, vwap = build_ladder_children(
            side="buy",
            distribution="half_gaussian",
            order_count=10,
            total_volume=Decimal("5"),
            start_price=Decimal("75000"),
            end_price=Decimal("74000"),
            size_increment=Decimal("0.001"),
            price_increment=Decimal("1"),
        )
        self.assertEqual(len(children), 10)
        sizes = [Decimal(c["size"]) for c in children]
        self.assertEqual(sum(sizes), total)
        # Half-gaussian: size grows toward end (lower price for BUY)
        self.assertLess(sizes[0], sizes[-1])
        self.assertEqual(vwap, ladder_vwap(children))
        # Weighted VWAP must not be simple midpoint
        mid = (Decimal("75000") + Decimal("74000")) / 2
        self.assertNotEqual(vwap, mid)

    def test_sell_half_gaussian_direction_validation(self) -> None:
        with self.assertRaises(ValueError):
            build_ladder_children(
                side="sell",
                distribution="half_gaussian",
                order_count=5,
                total_volume=Decimal("1"),
                start_price=Decimal("100"),
                end_price=Decimal("90"),  # invalid for sell
                size_increment=Decimal("0.01"),
                price_increment=Decimal("1"),
            )


class TradePreviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = _cfg()
        self.desk = TradeDeskFake()
        self.service = TradeMenuService(
            desk=self.desk,  # type: ignore[arg-type]
            session_secret=self.cfg.session_secret,
            preview_store=PreviewPlanStore(self.cfg.session_secret, ttl_seconds=300),
        )
        self.app = create_app(config=self.cfg, service=self.service)
        self.client = TestClient(self.app)

    def _login(self):
        return self.client.post("/login", data={"password": "test-password-xyz"}, follow_redirects=False)

    def _csrf(self) -> str:
        self._login()
        r = self.client.get("/api/session")
        return r.json()["csrf"]

    def test_preview_order_buy_limit_and_execute_once(self) -> None:
        csrf = self._csrf()
        r = self.client.post(
            "/api/trade/preview_order",
            headers={"X-CSRF-Token": csrf},
            json={
                "exchange": "hyperliquid",
                "account": "FLEX",
                "symbol": "BTCUSD",
                "side": "buy",
                "order_type": "limit",
                "size": "0.123456",
                "price": "75123.456",
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["native_symbol"], "BTC")
        self.assertEqual(body["side"], "buy")
        pid = body["preview_id"]
        self.assertTrue(pid)
        # Execute
        r2 = self.client.post(
            "/api/trade/execute",
            headers={"X-CSRF-Token": csrf},
            json={"preview_id": pid},
        )
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertTrue(r2.json()["success"])
        new_orders = [c for c in self.desk.calls if c.get("operation") == "new_order"]
        self.assertEqual(len(new_orders), 1)
        self.assertEqual(new_orders[0]["price"], body["final_price"])
        self.assertEqual(new_orders[0]["volume"], body["final_size"])
        # Double execute blocked
        r3 = self.client.post(
            "/api/trade/execute",
            headers={"X-CSRF-Token": csrf},
            json={"preview_id": pid},
        )
        self.assertEqual(r3.status_code, 400)
        self.assertEqual(r3.json()["error"]["code"], "PREVIEW_CONSUMED")
        self.assertEqual(len([c for c in self.desk.calls if c.get("operation") == "new_order"]), 1)

    def test_preview_order_requires_csrf(self) -> None:
        self._login()
        r = self.client.post(
            "/api/trade/preview_order",
            json={
                "exchange": "hyperliquid",
                "account": "FLEX",
                "symbol": "BTC",
                "side": "buy",
                "size": "1",
                "price": "1",
            },
        )
        self.assertEqual(r.status_code, 403)

    def test_preview_ladder_half_gaussian_children_vwap(self) -> None:
        csrf = self._csrf()
        r = self.client.post(
            "/api/trade/preview_ladder",
            headers={"X-CSRF-Token": csrf},
            json={
                "exchange": "hyperliquid",
                "account": "FLEX",
                "symbol": "BTCUSD",
                "side": "buy",
                "distribution": "half_gaussian",
                "order_count": 8,
                "total_size": "2",
                "start_price": "75000",
                "end_price": "74000",
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["success"], body)
        kids = body["children"]
        self.assertEqual(len(kids), body["order_count"])
        # Independent VWAP
        vwap = ladder_vwap(kids)
        self.assertEqual(format(Decimal(body["vwap"]).normalize(), "f"), format(vwap.normalize(), "f"))
        # sizes increase toward end
        sizes = [Decimal(c["size"]) for c in kids]
        self.assertLessEqual(sizes[0], sizes[-1])
        # execute ladder
        r2 = self.client.post(
            "/api/trade/execute",
            headers={"X-CSRF-Token": csrf},
            json={"preview_id": body["preview_id"]},
        )
        self.assertEqual(r2.status_code, 200, r2.text)
        ladders = [c for c in self.desk.calls if c.get("operation") == "ladder"]
        self.assertEqual(len(ladders), 1)
        self.assertEqual(ladders[0]["distribution"], "half_gaussian")
        self.assertEqual(int(ladders[0]["order_count"]), 8)

    def test_invalid_buy_ladder_direction(self) -> None:
        csrf = self._csrf()
        r = self.client.post(
            "/api/trade/preview_ladder",
            headers={"X-CSRF-Token": csrf},
            json={
                "exchange": "hyperliquid",
                "account": "FLEX",
                "symbol": "BTC",
                "side": "buy",
                "distribution": "uniform",
                "order_count": 5,
                "total_size": "1",
                "start_price": "74000",
                "end_price": "75000",
            },
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "INVALID_LADDER_DIRECTION")

    def test_unknown_account_rejected(self) -> None:
        csrf = self._csrf()
        r = self.client.post(
            "/api/trade/preview_order",
            headers={"X-CSRF-Token": csrf},
            json={
                "exchange": "hyperliquid",
                "account": "NOPE",
                "symbol": "BTC",
                "side": "sell",
                "size": "1",
                "price": "1",
            },
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "UNKNOWN_ACCOUNT")


if __name__ == "__main__":
    unittest.main()
