"""Tests for the native MT5Linux /trade agent (read-only).

Offline by default: terminal I/O is mocked. Live Docker tests are opt-in via
MT5LINUX_LIVE_TEST=1 and require the local mt5 container on 18812.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _clear_mt5linux_env() -> None:
    for key in list(os.environ):
        if key.upper().startswith("MT5LINUX_") or key.startswith("mt5linux_"):
            os.environ.pop(key, None)


class _MT5LinuxEnvMixin:
    def setUp(self) -> None:
        self._saved = {
            k: v
            for k, v in os.environ.items()
            if k.upper().startswith("MT5LINUX_") or k.startswith("mt5linux_") or k == "HERMES_HOME"
        }
        _clear_mt5linux_env()
        self.home = tempfile.mkdtemp(prefix="mt5linux_agent_test_")
        os.environ["HERMES_HOME"] = self.home
        Path(self.home, ".env").write_text("", encoding="utf-8")
        os.environ["MT5LINUX_LOCK_PATH"] = str(Path(self.home) / "terminal.lock")
        from plugins.trade.agents import x_mt5linux_agent as m

        m._CLIENT = None
        m._CLIENT_KEY = None

    def tearDown(self) -> None:
        _clear_mt5linux_env()
        os.environ.pop("HERMES_HOME", None)
        os.environ.update(self._saved)
        from plugins.trade.agents import x_mt5linux_agent as m

        m._CLIENT = None
        m._CLIENT_KEY = None

    def _write_dotenv(self, text: str) -> None:
        Path(self.home, ".env").write_text(text, encoding="utf-8")

    def _basic_accounts(self) -> None:
        # Canonical lowercase convention.
        os.environ["mt5linux_HOST"] = "127.0.0.1"
        os.environ["mt5linux_PORT"] = "18812"
        os.environ["mt5linux_LITE7486706_ACCOUNT"] = "7486706"
        os.environ["mt5linux_LITE7486706_SERVER"] = "LiteFinance-MT5-Live"
        os.environ["mt5linux_LITE7486706_PASSWORD"] = "secret-lite"
        os.environ["mt5linux_PO5701520_ACCOUNT"] = "5701520"
        os.environ["mt5linux_PO5701520_SERVER"] = "PoTrade-MT5"
        os.environ["mt5linux_PO5701520_PASSWORD"] = "secret-po"
        os.environ["mt5linux_PUPRIME33849951_ACCOUNT"] = "33849951"
        os.environ["mt5linux_PUPRIME33849951_SERVER"] = "PUPrime-Live2"
        os.environ["mt5linux_PUPRIME33849951_PASSWORD"] = "secret-pup"


class DiscoveryTests(_MT5LinuxEnvMixin, unittest.TestCase):
    def test_lowercase_prefix_and_server_fallback(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        os.environ["mt5linux_LITE7486706_ACCOUNT"] = "7486706"
        # no server -> fallback
        os.environ["mt5linux_UNKNOWN999_ACCOUNT"] = "999"
        discovered = m.discover_accounts()
        self.assertEqual(set(discovered), {"LITE7486706"})
        self.assertEqual(discovered["LITE7486706"]["server"], "LiteFinance-MT5-Live")
        self.assertFalse(discovered["LITE7486706"]["has_password"])
        # Internal password key must not appear empty-only; list_accounts public shape:
        public = m.list_accounts()
        self.assertEqual(public[0]["account"], "LITE7486706")
        self.assertNotIn("password", public[0])
        self.assertNotIn("_password", public[0])

    def test_uppercase_backward_compat(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        os.environ["MT5LINUX_MAIN_ACCOUNT"] = "111"
        os.environ["MT5LINUX_MAIN_SERVER"] = "Demo-Server"
        self.assertIn("MAIN", m.discover_accounts())

    def test_po_alias_canonical_and_no_duplicate(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        os.environ["mt5linux_PO5701520_ACCOUNT"] = "5701520"
        os.environ["mt5linux_PO5701520_SERVER"] = "PoTrade-MT5"
        os.environ["MT5LINUX_POTRADE5701520_ACCOUNT"] = "5701520"
        os.environ["MT5LINUX_POTRADE5701520_SERVER"] = "PoTrade-MT5"
        discovered = m.discover_accounts()
        self.assertEqual(set(discovered), {"PO5701520"})
        self.assertNotIn("POTRADE5701520", discovered)
        # Legacy request alias still resolves via canonicalize
        self.assertEqual(m._canonicalize_alias("POTRADE5701520"), "PO5701520")

    def test_lowercase_preferred_over_uppercase_same_alias(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        os.environ["MT5LINUX_LITE7486706_ACCOUNT"] = "1111111"
        os.environ["MT5LINUX_LITE7486706_SERVER"] = "Wrong-Server"
        os.environ["mt5linux_LITE7486706_ACCOUNT"] = "7486706"
        os.environ["mt5linux_LITE7486706_SERVER"] = "LiteFinance-MT5-Live"
        d = m.discover_accounts()["LITE7486706"]
        self.assertEqual(d["login"], 7486706)
        self.assertEqual(d["server"], "LiteFinance-MT5-Live")

    def test_password_loaded_but_not_listed(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        self._basic_accounts()
        d = m.discover_accounts()
        self.assertTrue(d["PO5701520"]["has_password"])
        self.assertEqual(d["PO5701520"]["_password"], "secret-po")
        for row in m.list_accounts():
            blob = str(row)
            self.assertNotIn("secret-", blob)

    def test_dotenv_and_env_override(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        self._write_dotenv(
            "mt5linux_HOST=10.0.0.1\n"
            "mt5linux_PORT=18812\n"
            "mt5linux_MAIN_ACCOUNT=111\n"
            "mt5linux_MAIN_SERVER=Demo-Server\n"
        )
        os.environ["mt5linux_HOST"] = "127.0.0.1"
        self.assertEqual(m.connection_endpoint(), ("127.0.0.1", 18812))
        self.assertIn("MAIN", m.discover_accounts())

    def test_name_and_capabilities_include_writes(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        self.assertEqual(m.name, "mt5linux")
        self.assertEqual(m.display_name, "MT5Linux")
        caps = set(m.capabilities())
        self.assertIn("balance", caps)
        self.assertIn("positions_orders", caps)
        self.assertIn("new_order", caps)
        self.assertIn("ladder", caps)
        self.assertIn("cancel_orders", caps)
        self.assertIn("close_position", caps)
        self.assertIn("set_tp", caps)
        self.assertIn("set_sl", caps)
        self.assertNotIn("place_order", caps)

    def test_market_place_order_blocked(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        self._basic_accounts()
        resp = m.execute({"operation": "place_order", "account": "LITE7486706"})
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "UNSUPPORTED_OPERATION")

    def test_new_order_validation_without_terminal(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        self._basic_accounts()
        resp = m.execute(
            {
                "operation": "new_order",
                "account": "LITE7486706",
                "symbol": "XAUUSD",
                "side": "buy",
                "order_type": "limit",
                # missing volume/price
            }
        )
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "INVALID_VOLUME")

    def test_missing_account_alias(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        self._basic_accounts()
        resp = m.execute({"operation": "balance", "account": "UNKNOWN"})
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "ACCOUNT_NOT_CONFIGURED")

    def test_source_guardrails(self) -> None:
        src = (_REPO_ROOT / "plugins" / "trade" / "agents" / "x_mt5linux_agent.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("LINUX_WIN_HOST", src)
        self.assertNotIn("LINUX_WIN_PORT", src)
        self.assertIn('name = "mt5linux"', src)
        self.assertIn('display_name = "MT5Linux"', src)
        self.assertIn("PO5701520", src)
        self.assertIn("_PASSWORD_KEY_RE", src)


class ExecuteMockTests(_MT5LinuxEnvMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._basic_accounts()

    def _patch_terminal(self, snap: dict):
        from plugins.trade.agents import x_mt5linux_agent as m

        client = mock.MagicMock()
        client.initialize.return_value = True
        client.login.return_value = True
        client.last_error.return_value = (1, "Success")

        def fake_with_account(alias, fn):
            accounts = m.discover_accounts()
            alias_u = m._canonicalize_alias(alias.upper())
            meta = accounts[alias_u]
            return fn(client, snap, alias_u, meta["login"], meta["server"])

        return mock.patch.object(m, "_with_account", side_effect=fake_with_account)

    def test_balance_and_usc_raw_preserved(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        snap = {
            "account": {
                "login": 33849951,
                "server": "PUPrime-Live2",
                "currency": "USC",
                "balance": 100000.0,
                "equity": 85000.0,
                "profit": -15000.0,
                "margin": 70000.0,
                "margin_free": 15000.0,
            },
            "terminal": {"connected": True},
            "positions_total": 0,
            "orders_total": 0,
            "positions": [],
            "orders": [],
        }
        with self._patch_terminal(snap):
            resp = m.execute({"operation": "balance", "account": "PUPRIME33849951"})
        self.assertTrue(resp.success)
        assert resp.balance is not None
        self.assertEqual(resp.balance.unit, "USC")
        self.assertEqual(resp.balance.value, "100000.00")
        assert resp.portfolio_summary is not None
        self.assertEqual(resp.portfolio_summary.unit, "USC")
        assert resp.data is not None
        self.assertEqual(resp.data.get("currency"), "USC")
        self.assertAlmostEqual(float(resp.data.get("balance_usd")), 1000.0)
        self.assertNotIn("_password", resp.data)
        self.assertNotIn("password", str(resp.data))

    def test_positions_orders_grouping(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        snap = {
            "account": {
                "login": 7486706,
                "server": "LiteFinance-MT5-Live",
                "currency": "USD",
                "balance": 2000.0,
                "equity": 3000.0,
                "profit": 1000.0,
                "margin": 100.0,
                "margin_free": 2900.0,
            },
            "terminal": {"connected": True},
            "positions_total": 2,
            "orders_total": 2,
            "positions": [
                {
                    "ticket": 1,
                    "symbol": "HYPEUSD",
                    "type": 1,
                    "volume": 0.1,
                    "price_open": 96.0,
                    "price_current": 90.0,
                    "profit": 60.0,
                    "sl": 0.0,
                    "tp": 0.0,
                },
                {
                    "ticket": 2,
                    "symbol": "HYPEUSD",
                    "type": 1,
                    "volume": 0.1,
                    "price_open": 97.0,
                    "price_current": 90.0,
                    "profit": 70.0,
                    "sl": 0.0,
                    "tp": 0.0,
                },
            ],
            "orders": [
                {
                    "ticket": 10,
                    "symbol": "HYPEUSD",
                    "type": 3,
                    "volume_current": 0.1,
                    "price_open": 98.0,
                    "sl": 0.0,
                    "tp": 0.0,
                },
                {
                    "ticket": 11,
                    "symbol": "HYPEUSD",
                    "type": 3,
                    "volume_current": 0.1,
                    "price_open": 99.0,
                    "sl": 0.0,
                    "tp": 0.0,
                },
            ],
        }
        with self._patch_terminal(snap):
            resp = m.execute({"operation": "positions_orders", "account": "LITE7486706"})
        self.assertTrue(resp.success)
        assert resp.positions is not None
        self.assertEqual(len(resp.positions), 1)
        pos = resp.positions[0]
        self.assertEqual(pos.symbol, "HYPEUSD")
        self.assertEqual(pos.side, "SELL")
        self.assertEqual(pos.count, 2)
        self.assertEqual(pos.size, "0.2")
        self.assertEqual(resp.open_order_count, 2)
        assert resp.order_groups is not None
        self.assertEqual(len(resp.order_groups), 1)
        self.assertEqual(resp.order_groups[0].display_type, "SELL_LIMIT")

    def test_login_prefers_saved_then_password(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        client = mock.MagicMock()
        client.last_error.return_value = (0, "fail")

        def login_side_effect(*args, **kwargs):
            # First call(s) without real password fail; password kw succeeds.
            if kwargs.get("password") and kwargs.get("password") not in ("", None):
                return True
            return False

        client.login.side_effect = login_side_effect
        m._login_account(client, 5701520, "PoTrade-MT5", password="secret-po")
        # At least one passwordless attempt and one password attempt.
        self.assertGreaterEqual(client.login.call_count, 2)
        pw_calls = [c for c in client.login.call_args_list if c.kwargs.get("password") == "secret-po"]
        self.assertEqual(len(pw_calls), 1)

    def test_lock_serializes_parallel_calls(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        hold = threading.Event()
        started = threading.Event()
        order: list[str] = []

        def slow_fn(alias: str) -> None:
            order.append(f"enter:{alias}")
            started.set()
            hold.wait(timeout=2)
            order.append(f"leave:{alias}")

        def worker(alias: str) -> None:
            with m._terminal_lock(timeout_s=5):
                slow_fn(alias)

        t1 = threading.Thread(target=worker, args=("A",))
        t2 = threading.Thread(target=worker, args=("B",))
        t1.start()
        self.assertTrue(started.wait(timeout=2))
        t2.start()
        time.sleep(0.2)
        self.assertEqual(order, ["enter:A"])
        hold.set()
        t1.join(timeout=2)
        t2.join(timeout=2)
        self.assertEqual(order, ["enter:A", "leave:A", "enter:B", "leave:B"])

    def test_new_order_limit_success_mocked(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m
        from plugins.trade.agents import _mt5linux_orders as orders

        snap = {
            "account": {
                "login": 7486706,
                "server": "LiteFinance-MT5-Live",
                "currency": "USD",
                "balance": 1000.0,
                "equity": 1000.0,
                "profit": 0.0,
                "margin": 0.0,
                "margin_free": 1000.0,
            },
            "terminal": {"connected": True},
            "positions": [],
            "orders": [],
        }
        with self._patch_terminal(snap):
            with mock.patch.object(
                orders,
                "order_send_pending",
                return_value={
                    "ok": True,
                    "retcode": 10009,
                    "order": 555001,
                    "deal": 0,
                    "comment": "done",
                    "symbol": "XAUUSD",
                    "submitted_volume": "0.10",
                    "submitted_price": "2400.00",
                },
            ):
                with mock.patch.object(orders, "reconcile_order", return_value={"present": True, "ticket": 555001}):
                    resp = m.execute(
                        {
                            "operation": "new_order",
                            "account": "LITE7486706",
                            "symbol": "XAUUSD",
                            "side": "buy",
                            "order_type": "limit",
                            "volume": "0.1",
                            "price": "2400",
                        }
                    )
        self.assertTrue(resp.success, msg=str(getattr(resp, "error", None)))
        assert resp.order is not None
        self.assertEqual(str(resp.order.exchange_order_id), "555001")
        self.assertEqual(resp.order.order_type, "limit")
        assert resp.data is not None
        self.assertEqual(resp.data.get("login"), 7486706)
        self.assertEqual(resp.data.get("server"), "LiteFinance-MT5-Live")

    def test_ladder_uniform_mocked(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m
        from plugins.trade.agents import _mt5linux_orders as orders

        snap = {
            "account": {
                "login": 7486706,
                "server": "LiteFinance-MT5-Live",
                "currency": "USD",
                "balance": 1000.0,
                "equity": 1000.0,
                "profit": 0.0,
                "margin": 0.0,
                "margin_free": 1000.0,
            },
            "terminal": {"connected": True},
            "positions": [],
            "orders": [],
        }
        tickets = iter([7001, 7002, 7003])

        def fake_send(*_args, **kwargs):
            t = next(tickets)
            return {
                "ok": True,
                "retcode": 10009,
                "order": t,
                "deal": 0,
                "comment": "done",
                "symbol": kwargs.get("symbol"),
                "submitted_volume": str(kwargs.get("volume")),
                "submitted_price": str(kwargs.get("price")),
            }

        with self._patch_terminal(snap):
            with mock.patch.object(
                orders,
                "remote_symbol_trade_meta",
                return_value={
                    "name": "XAUUSD",
                    "volume_min": 0.01,
                    "volume_step": 0.01,
                    "volume_max": 100.0,
                    "digits": 2,
                    "point": 0.01,
                    "trade_tick_size": 0.01,
                    "filling_mode": 1,
                },
            ):
                with mock.patch.object(orders, "order_send_pending", side_effect=fake_send):
                    with mock.patch.object(m, "_fetch_snapshot", return_value=snap):
                        resp = m.execute(
                            {
                                "operation": "ladder",
                                "account": "LITE7486706",
                                "symbol": "XAUUSD",
                                "side": "buy",
                                "order_type": "limit",
                                "order_count": 3,
                                "total_volume": "0.3",
                                "start_price": "2400",
                                "end_price": "2390",
                                "distribution": "uniform",
                            }
                        )
        self.assertTrue(resp.success, msg=str(getattr(resp, "error", None)))
        assert resp.ladder is not None
        self.assertEqual(resp.ladder.requested_order_count, 3)
        self.assertEqual(resp.ladder.submitted_order_count, 3)
        self.assertTrue(resp.ladder.verified)
        assert resp.data is not None
        self.assertEqual(resp.data.get("login"), 7486706)

    def test_cancel_orders_group_mocked(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m
        from plugins.trade.agents import _mt5linux_orders as orders

        snap = {
            "account": {
                "login": 7486706,
                "server": "LiteFinance-MT5-Live",
                "currency": "USD",
                "balance": 1.0,
                "equity": 1.0,
                "profit": 0.0,
                "margin": 0.0,
                "margin_free": 1.0,
            },
            "terminal": {"connected": True},
            "positions": [],
            "orders": [],
        }
        with self._patch_terminal(snap):
            with mock.patch.object(
                orders,
                "list_account_orders",
                return_value=[
                    {"ticket": 10, "symbol": "XAUUSD", "type": 2, "volume_current": 0.1, "price_open": 2400.0},
                    {"ticket": 11, "symbol": "XAUUSD", "type": 2, "volume_current": 0.1, "price_open": 2390.0},
                ],
            ):
                with mock.patch.object(
                    orders,
                    "order_cancel",
                    return_value={"ok": True, "retcode": 10009, "order": 10, "comment": "done"},
                ):
                    resp = m.execute(
                        {
                            "operation": "cancel_orders",
                            "account": "LITE7486706",
                            "symbol": "XAUUSD",
                            "side": "buy",
                            "order_type": "LIMIT",
                        }
                    )
        self.assertTrue(resp.success)
        assert resp.cancel_group is not None
        self.assertEqual(resp.cancel_group.targeted_order_count, 2)
        self.assertEqual(resp.cancel_group.cancelled_order_count, 2)

    def test_close_position_requires_ticket_when_ambiguous(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        snap = {
            "account": {
                "login": 7486706,
                "server": "LiteFinance-MT5-Live",
                "currency": "USD",
                "balance": 1.0,
                "equity": 1.0,
                "profit": 0.0,
                "margin": 0.0,
                "margin_free": 1.0,
            },
            "terminal": {"connected": True},
            "positions": [
                {"ticket": 1, "symbol": "XAUUSD", "type": 0, "volume": 0.1},
                {"ticket": 2, "symbol": "XAUUSD", "type": 0, "volume": 0.2},
            ],
            "orders": [],
        }
        with self._patch_terminal(snap):
            resp = m.execute(
                {
                    "operation": "close_position",
                    "account": "LITE7486706",
                    "symbol": "XAUUSD",
                    "side": "buy",
                }
            )
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "AMBIGUOUS_POSITION")

    def test_set_tp_by_ticket_mocked(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m
        from plugins.trade.agents import _mt5linux_orders as orders

        snap = {
            "account": {
                "login": 7486706,
                "server": "LiteFinance-MT5-Live",
                "currency": "USD",
                "balance": 1.0,
                "equity": 1.0,
                "profit": 0.0,
                "margin": 0.0,
                "margin_free": 1.0,
            },
            "terminal": {"connected": True},
            "positions": [{"ticket": 99, "symbol": "XAUUSD", "type": 0, "volume": 0.1}],
            "orders": [],
        }
        with self._patch_terminal(snap):
            with mock.patch.object(
                orders,
                "position_set_sltp",
                return_value={
                    "ok": True,
                    "retcode": 10009,
                    "ticket": 99,
                    "symbol": "XAUUSD",
                    "side": "BUY",
                    "tp": 2500.0,
                    "sl": 2300.0,
                    "comment": "done",
                },
            ):
                resp = m.execute(
                    {
                        "operation": "set_tp",
                        "account": "LITE7486706",
                        "position_ticket": 99,
                        "tp": "2500",
                    }
                )
        self.assertTrue(resp.success)
        assert resp.position_action is not None
        self.assertEqual(resp.position_action.operation, "set_tp")

    def test_orders_module_no_password_literal(self) -> None:
        src = (_REPO_ROOT / "plugins" / "trade" / "agents" / "_mt5linux_orders.py").read_text(encoding="utf-8")
        self.assertNotRegex(src, r"(?i)password")
        self.assertNotRegex(src, r"(?i)passwd")

    def test_market_price_rejects_zero_quote(self) -> None:
        """Regression: unselected ETHUSD must not surface price 0."""
        from plugins.trade.agents import x_mt5linux_agent as m

        self._basic_accounts()
        fake_payload = {
            "info": {
                "name": "ETHUSD",
                "point": 0.01,
                "trade_tick_size": 0.01,
                "volume_min": 0.01,
                "volume_step": 0.01,
                "trade_contract_size": 1.0,
                "digits": 2,
                "bid": 0.0,
                "ask": 0.0,
                "last": 0.0,
            },
            "tick": {"bid": 0.0, "ask": 0.0, "last": 0.0, "time": 0, "rate_close": None},
        }
        with mock.patch.object(m, "_with_account") as wa:
            def _side(account, fn):
                snap = {
                    "account": {
                        "login": 7486706,
                        "server": "LiteFinance-MT5-Live",
                        "currency": "USD",
                        "balance": 1,
                    },
                    "terminal": {"connected": True},
                }
                return fn(object(), snap, "LITE7486706", 7486706, "LiteFinance-MT5-Live")

            wa.side_effect = _side
            with mock.patch.object(m, "_symbol_info_remote", return_value=fake_payload):
                resp = m.execute(
                    {"operation": "market_price", "account": "LITE7486706", "symbol": "ETHUSD"}
                )
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "QUOTE_UNAVAILABLE")

    def test_market_price_uses_bid_when_last_zero(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        self._basic_accounts()
        fake_payload = {
            "info": {
                "name": "ETHUSD",
                "point": 0.01,
                "trade_tick_size": 0.01,
                "volume_min": 0.01,
                "volume_step": 0.01,
                "trade_contract_size": 1.0,
                "digits": 2,
                "bid": 2270.41,
                "ask": 2270.59,
                "last": 0.0,
            },
            "tick": {"bid": 2270.41, "ask": 2270.59, "last": 0.0, "time": 1, "rate_close": None},
        }
        with mock.patch.object(m, "_with_account") as wa:
            def _side(account, fn):
                snap = {
                    "account": {
                        "login": 7486706,
                        "server": "LiteFinance-MT5-Live",
                        "currency": "USD",
                        "balance": 1,
                    },
                    "terminal": {"connected": True},
                }
                return fn(object(), snap, "LITE7486706", 7486706, "LiteFinance-MT5-Live")

            wa.side_effect = _side
            with mock.patch.object(m, "_symbol_info_remote", return_value=fake_payload):
                resp = m.execute(
                    {"operation": "market_price", "account": "LITE7486706", "symbol": "ETHUSD"}
                )
        self.assertTrue(resp.success)
        assert resp.market_price is not None
        self.assertEqual(resp.market_price.price, "2270.41")
        self.assertNotEqual(resp.market_price.price, "0")

    def test_new_order_not_read_only(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        self.assertIn("new_order", m.capabilities())
        self._basic_accounts()
        resp = m.execute(
            {
                "operation": "new_order",
                "account": "LITE7486706",
                "symbol": "ETHUSD",
                "side": "sell",
                "price": "2600",
                "order_type": "limit",
            }
        )
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertNotEqual(resp.error.code, "READ_ONLY")

    def test_trade_expert_false_blocks_writes(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        self._basic_accounts()
        snap = {
            "account": {
                "login": 5701520,
                "server": "PoTrade-MT5",
                "currency": "USD",
                "balance": 1,
                "trade_expert": False,
                "trade_allowed": True,
            },
            "terminal": {"connected": True, "trade_allowed": True, "tradeapi_disabled": False},
        }

        def _side(account, fn):
            return fn(object(), snap, "PO5701520", 5701520, "PoTrade-MT5")

        with mock.patch.object(m, "_with_account") as wa:
            wa.side_effect = _side
            resp = m.execute(
                {
                    "operation": "new_order",
                    "account": "PO5701520",
                    "symbol": "SOLUSD",
                    "side": "sell",
                    "volume": "0.01",
                    "price": "50",
                    "order_type": "limit",
                }
            )
        self.assertFalse(resp.success)
        assert resp.error is not None
        self.assertEqual(resp.error.code, "BROKER_AUTOTRADING_DISABLED")
        self.assertIn("trade_expert=False", resp.error.message)
        with mock.patch.object(m._orders, "order_send_pending") as send:
            with mock.patch.object(m, "_with_account") as wa2:
                wa2.side_effect = _side
                m.execute(
                    {
                        "operation": "new_order",
                        "account": "PO5701520",
                        "symbol": "SOLUSD",
                        "side": "sell",
                        "volume": "0.01",
                        "price": "50",
                        "order_type": "limit",
                    }
                )
            send.assert_not_called()


class PortfolioUscTests(unittest.TestCase):
    def test_portfolio_normalizes_mt5linux_usc(self) -> None:
        from decimal import Decimal

        from plugins.trade.portfolio_report import _monetary_normalization

        unit, factor = _monetary_normalization("mt5linux", "USC")
        self.assertEqual(unit, "USD")
        self.assertEqual(factor, Decimal("0.01"))


class TradeDeskDiscoveryTests(_MT5LinuxEnvMixin, unittest.TestCase):
    def test_tradedesk_discovers_exact_three_aliases(self) -> None:
        self._basic_accounts()
        from plugins.trade.tradedesk import TradeDesk

        desk = TradeDesk()
        exchanges = desk.list_exchanges()
        self.assertIn("mt5linux", exchanges)
        self.assertIn("metatrader", exchanges)
        accounts = desk.list_accounts("mt5linux")
        aliases = {(a["account"] if isinstance(a, dict) else a) for a in accounts}
        self.assertEqual(aliases, {"LITE7486706", "PO5701520", "PUPRIME33849951"})
        self.assertNotIn("POTRADE5701520", aliases)


@unittest.skipUnless(os.environ.get("MT5LINUX_LIVE_TEST") == "1", "live MT5 opt-in")
class LiveDockerTests(_MT5LinuxEnvMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._basic_accounts()

    def test_three_accounts_balance_and_positions(self) -> None:
        from plugins.trade.agents import x_mt5linux_agent as m

        for alias in ("LITE7486706", "PO5701520", "PUPRIME33849951"):
            bal = m.execute({"operation": "balance", "account": alias})
            pos = m.execute({"operation": "positions_orders", "account": alias})
            self.assertTrue(bal.success, msg=str(getattr(bal, "error", None)))
            self.assertTrue(pos.success, msg=str(getattr(pos, "error", None)))
            assert bal.data is not None
            meta = m.discover_accounts()[alias]
            self.assertEqual(bal.data["account"]["login"], meta["login"])
            self.assertEqual(bal.data["account"]["server"], meta["server"])
        ping = m.execute({"operation": "ping", "account": "PUPRIME33849951"})
        self.assertTrue(ping.success)


if __name__ == "__main__":
    unittest.main()
