""" /fibo discovery must match /trade TradeDesk discovery.

Discovery and GoldenFibo runtime support are separate:
  - exchange/account lists come from TradeDesk (same as /trade)
  - after account selection, unsupported GoldenFibo venues show a clean
    UI message and perform no service/trading mutation
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock


_EDITABLE_FINDER = "__editable___hermes_agent_0_20_0_finder"
_KNOWN_EDITABLE_FINDERS = (_EDITABLE_FINDER,)
if any(name in repr(h) for h in sys.path_hooks for name in _KNOWN_EDITABLE_FINDERS):
    sys.path_hooks[:] = [
        h
        for h in sys.path_hooks
        if not any(name in repr(h) for name in _KNOWN_EDITABLE_FINDERS)
    ]

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
# NOTE: Do NOT pop plugins.trade.* from sys.modules here.
# Session-level isolation lives in conftest.py.


from plugins.trade.fibo_wizard import (  # noqa: E402
    FiboWizard,
    _account_aliases_for_exchange,
    _discovered_exchanges,
    _golden_fibo_supported,
)
from plugins.trade.fibo_service import SUPPORTED_EXCHANGES  # noqa: E402
from plugins.trade.wizard import TradeWizard, _account_option_parts  # noqa: E402


class _FakeDesk:
    """Minimal TradeDesk stand-in with controllable discovery."""

    def __init__(
        self,
        exchanges: Optional[List[str]] = None,
        accounts: Optional[Dict[str, List[Any]]] = None,
    ) -> None:
        self._exchanges = list(exchanges or [])
        self._accounts = dict(accounts or {})
        self.list_accounts_calls: List[str] = []
        self.execute_calls: List[Dict[str, Any]] = []

    def list_exchanges(self) -> List[str]:
        return list(self._exchanges)

    def list_accounts(self, exchange: str) -> List[Any]:
        self.list_accounts_calls.append(str(exchange))
        return list(self._accounts.get(exchange, []))

    def execute(self, request: Dict[str, Any]) -> Any:
        self.execute_calls.append(dict(request))
        raise AssertionError("TradeDesk.execute must not be called during fibo discovery")


class _StubFiboService:
    def __init__(self) -> None:
        self.commands: List[Dict[str, Any]] = []

    def execute_command(self, command: Dict[str, Any]) -> Dict[str, Any]:
        self.commands.append(dict(command))
        return {"ok": True, "status": "running", "registration_key": "x"}


def _button_labels(screen) -> List[str]:
    return [b["text"] for row in screen.buttons for b in row]


def _button_data(screen) -> List[str]:
    return [b["callback_data"] for row in screen.buttons for b in row]


class TestFiboDiscoveryParityWithTrade(unittest.TestCase):
    def test_fibo_exchange_list_equals_tradedesk_list_exchanges(self):
        desk = _FakeDesk(
            exchanges=["rise", "lighter", "ondoperps", "apex", "arcus"],
            accounts={},
        )
        trade = TradeWizard(tradedesk=desk)
        fibo = FiboWizard(tradedesk=desk, service=_StubFiboService())

        trade_screen = trade.open(("t", 1))
        fibo_screen = fibo.handle_callback(("f", 1), "menu:start")

        trade_ex = sorted(
            d.split(":", 1)[1]
            for d in _button_data(trade_screen)
            if d.startswith("exchange:")
        )
        fibo_ex = sorted(
            d.split(":", 1)[1]
            for d in _button_data(fibo_screen)
            if d.startswith("exchange:")
        )
        self.assertEqual(trade_ex, fibo_ex)
        self.assertEqual(fibo_ex, sorted(desk.list_exchanges()))
        self.assertEqual(_discovered_exchanges(desk), desk.list_exchanges())

    def test_lighter_accounts_match_trade(self):
        desk = _FakeDesk(
            exchanges=["lighter"],
            accounts={
                "lighter": [
                    {"account": "amiroo", "label": "amiroo — Robinhood"},
                    {"account": "robin", "label": "robin — Arbitrum"},
                ]
            },
        )
        trade = TradeWizard(tradedesk=desk)
        fibo = FiboWizard(tradedesk=desk, service=_StubFiboService())

        # /trade path
        trade.handle_callback(("t", 1), "exchange:lighter")
        # TradeWizard uses handle_callback with full suffix after prefix strip;
        # open then select via internal handlers:
        t_state_key = ("t", 1)
        trade.open(t_state_key)
        t_acc = trade.handle_callback(t_state_key, "exchange:lighter")

        # /fibo path
        fibo.open(("f", 1))
        fibo.handle_callback(("f", 1), "menu:start")
        f_acc = fibo.handle_callback(("f", 1), "exchange:lighter")

        trade_aliases = sorted(
            d.split(":", 1)[1]
            for d in _button_data(t_acc)
            if d.startswith("account:")
        )
        fibo_aliases = sorted(
            d.split(":", 1)[1]
            for d in _button_data(f_acc)
            if d.startswith("account:")
        )
        self.assertEqual(trade_aliases, ["amiroo", "robin"])
        self.assertEqual(fibo_aliases, trade_aliases)
        self.assertEqual(
            _account_aliases_for_exchange(desk, "lighter"),
            ["amiroo", "robin"],
        )
        # Labels may include chain info for lighter structured entries.
        self.assertTrue(any("amiroo" in lab for lab in _button_labels(f_acc)))

    def test_ondoperps_accounts_match_trade(self):
        desk = _FakeDesk(
            exchanges=["ondoperps"],
            accounts={"ondoperps": ["amiroo", "bitget"]},
        )
        trade = TradeWizard(tradedesk=desk)
        fibo = FiboWizard(tradedesk=desk, service=_StubFiboService())

        trade.open(("t", 1))
        t_acc = trade.handle_callback(("t", 1), "exchange:ondoperps")
        fibo.open(("f", 1))
        fibo.handle_callback(("f", 1), "menu:start")
        f_acc = fibo.handle_callback(("f", 1), "exchange:ondoperps")

        trade_aliases = sorted(
            d.split(":", 1)[1]
            for d in _button_data(t_acc)
            if d.startswith("account:")
        )
        fibo_aliases = sorted(
            d.split(":", 1)[1]
            for d in _button_data(f_acc)
            if d.startswith("account:")
        )
        self.assertEqual(trade_aliases, ["amiroo", "bitget"])
        self.assertEqual(fibo_aliases, trade_aliases)

    def test_no_accounts_same_message_shape_as_trade(self):
        desk = _FakeDesk(exchanges=["lighter"], accounts={"lighter": []})
        trade = TradeWizard(tradedesk=desk)
        fibo = FiboWizard(tradedesk=desk, service=_StubFiboService())

        trade.open(("t", 1))
        t_acc = trade.handle_callback(("t", 1), "exchange:lighter")
        fibo.open(("f", 1))
        fibo.handle_callback(("f", 1), "menu:start")
        f_acc = fibo.handle_callback(("f", 1), "exchange:lighter")

        self.assertIn("No accounts are configured for this exchange", t_acc.text)
        self.assertIn("No accounts are configured for this exchange", f_acc.text)
        # No account: buttons on either side.
        self.assertFalse(any(d.startswith("account:") for d in _button_data(t_acc)))
        self.assertFalse(any(d.startswith("account:") for d in _button_data(f_acc)))

    def test_supported_exchange_proceeds_to_instrument(self):
        self.assertIn("lighter", SUPPORTED_EXCHANGES)
        desk = _FakeDesk(
            exchanges=["lighter", "arcus"],
            accounts={"lighter": ["amiroo"], "arcus": ["main"]},
        )
        svc = _StubFiboService()
        fibo = FiboWizard(tradedesk=desk, service=svc)
        fibo.open(("f", 1))
        fibo.handle_callback(("f", 1), "menu:start")
        fibo.handle_callback(("f", 1), "exchange:lighter")
        s = fibo.handle_callback(("f", 1), "account:amiroo")
        self.assertEqual(s.state, "instrument")
        self.assertIn("Select instrument", s.text)
        self.assertEqual(svc.commands, [])  # no start yet
        self.assertEqual(desk.execute_calls, [])

    def test_unsupported_exchange_graceful_no_mutation(self):
        self.assertFalse(_golden_fibo_supported("arcus"))
        desk = _FakeDesk(
            exchanges=["lighter", "arcus"],
            accounts={"arcus": ["main"], "lighter": ["amiroo"]},
        )
        svc = _StubFiboService()
        fibo = FiboWizard(tradedesk=desk, service=svc)
        fibo.open(("f", 1))
        s = fibo.handle_callback(("f", 1), "menu:start")
        self.assertIn("ARCUS", _button_labels(s))  # still shown
        fibo.handle_callback(("f", 1), "exchange:arcus")
        s = fibo.handle_callback(("f", 1), "account:main")
        self.assertEqual(s.state, "unsupported_exchange")
        self.assertIn("GoldenFibo is not yet available on Arcus", s.text)
        self.assertEqual(svc.commands, [])
        self.assertEqual(desk.execute_calls, [])
        # Back returns to exchange list cleanly.
        s = fibo.handle_callback(("f", 1), "back")
        self.assertEqual(s.state, "exchange")

    def test_trade_discovery_helpers_unchanged_by_fibo_path(self):
        """Calling fibo discovery must not alter TradeDesk outputs for /trade."""
        desk = _FakeDesk(
            exchanges=["hibachi", "lighter"],
            accounts={"hibachi": ["bitget"], "lighter": ["amiroo"]},
        )
        before_ex = desk.list_exchanges()
        before_hib = desk.list_accounts("hibachi")
        before_lit = desk.list_accounts("lighter")

        fibo = FiboWizard(tradedesk=desk, service=_StubFiboService())
        fibo.open(("f", 1))
        fibo.handle_callback(("f", 1), "menu:start")
        fibo.handle_callback(("f", 1), "exchange:hibachi")

        self.assertEqual(desk.list_exchanges(), before_ex)
        self.assertEqual(desk.list_accounts("hibachi"), before_hib)
        self.assertEqual(desk.list_accounts("lighter"), before_lit)

        trade = TradeWizard(tradedesk=desk)
        trade.open(("t", 1))
        t_ex = sorted(
            d.split(":", 1)[1]
            for d in _button_data(trade.open(("t", 2)))
            if d.startswith("exchange:")
        )
        self.assertEqual(t_ex, sorted(before_ex))

    def test_no_hardcoded_exchange_list_in_fibo_wizard_source(self):
        src = Path("/root/kam/plugins/trade/fibo_wizard.py").read_text(encoding="utf-8")
        # Must not hard-code the full trade discovery set as a local constant.
        forbidden_literals = (
            '("apex", "arcus", "edgex"',
            "['apex', 'arcus', 'edgex'",
            '("lighter", "ondoperps", "arcus"',
        )
        for lit in forbidden_literals:
            self.assertNotIn(lit, src)
        # Discovery must call TradeDesk.list_exchanges.
        self.assertIn("list_exchanges()", src)
        self.assertIn("_discovered_exchanges", src)
        # Must not filter the exchange screen by SUPPORTED_EXCHANGES alone.
        self.assertNotIn("return list(SUPPORTED_EXCHANGES)", src)


class TestAccountOptionPartsReuse(unittest.TestCase):
    def test_structured_and_string_entries(self):
        self.assertEqual(_account_option_parts("amiroo"), ("amiroo", "amiroo"))
        self.assertEqual(
            _account_option_parts({"account": "robin", "label": "robin — RH"}),
            ("robin", "robin — RH"),
        )


if __name__ == "__main__":
    unittest.main()
