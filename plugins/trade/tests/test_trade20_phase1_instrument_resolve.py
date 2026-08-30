"""Trade 2.0 Phase 1 — wire /trade to existing exchange-agent resolvers.

Offline only. Never modifies x_*_agent.py. Never hits live exchanges.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.canonical import (  # noqa: E402
    CanonicalInstrument,
    make_failure,
    make_success,
)
from plugins.trade.wizard import TradeWizard  # noqa: E402


class ResolvingStubDesk:
    """TradeDesk stub that advertises and handles resolve_instrument."""

    def __init__(
        self,
        *,
        resolve_map: Optional[Dict[str, Any]] = None,
        exchanges: Optional[List[str]] = None,
        accounts: Optional[Dict[str, List[str]]] = None,
        caps: Optional[List[str]] = None,
    ) -> None:
        self.resolve_map = dict(resolve_map or {})
        self._exchanges = list(exchanges or ["ondoperps"])
        self._accounts = dict(accounts or {"ondoperps": ["bitget"]})
        self._caps = list(caps if caps is not None else ["resolve_instrument", "new_order", "ladder"])
        self.requests: List[Dict[str, Any]] = []

    def list_exchanges(self) -> List[str]:
        return list(self._exchanges)

    def list_accounts(self, exchange: str) -> List[str]:
        return list(self._accounts.get(exchange, []))

    def capabilities(self, exchange: str) -> List[str]:
        return list(self._caps)

    def execute(self, request: Dict[str, Any]):
        req = dict(request)
        self.requests.append(req)
        op = str(req.get("operation") or "")
        exchange = str(req.get("exchange") or "")
        account = str(req.get("account") or "")
        symbol = str(req.get("symbol") or "").strip().upper()

        if op != "resolve_instrument":
            return make_failure(
                operation=op,
                exchange=exchange,
                account=account,
                code="NOT_IMPLEMENTED",
                message=f"stub only implements resolve_instrument, got {op}",
            )

        outcome = self.resolve_map.get(symbol)
        if outcome is None:
            return make_failure(
                operation="resolve_instrument",
                exchange=exchange,
                account=account,
                code="INSTRUMENT_NOT_FOUND",
                message=f"Ondo Perps has no market for symbol '{symbol}'.",
            )
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, str) and outcome.startswith("FAIL:"):
            code, _, msg = outcome[5:].partition(":")
            return make_failure(
                operation="resolve_instrument",
                exchange=exchange,
                account=account,
                code=code or "INSTRUMENT_NOT_FOUND",
                message=msg or "Instrument not found.",
            )
        if isinstance(outcome, dict) and outcome.get("_fail"):
            return make_failure(
                operation="resolve_instrument",
                exchange=exchange,
                account=account,
                code=str(outcome.get("code") or "INSTRUMENT_NOT_FOUND"),
                message=str(outcome.get("message") or "Instrument not found."),
            )
        native = str(outcome)
        return make_success(
            operation="resolve_instrument",
            exchange=exchange,
            account=account,
            instrument=CanonicalInstrument(
                requested_symbol=symbol,
                symbol=native,
                display_name=native,
            ),
        )


class TestTrade20Phase1InstrumentResolve(unittest.TestCase):
    def _open_new_order(self, desk: ResolvingStubDesk):
        wizard = TradeWizard(tradedesk=desk)  # type: ignore[arg-type]
        key = ("chat",)
        wizard.open(key)
        wizard.handle_callback(key, "exchange:ondoperps")
        wizard.handle_callback(key, "account:bitget")
        wizard.handle_callback(key, "action:new_order")
        return wizard, key

    def test_ondoperps_resolver_detected_and_called_with_xauusd(self) -> None:
        """XAUUSD is passed to the existing resolver path (via TradeDesk)."""
        desk = ResolvingStubDesk(
            # Mirror real OndoPerps behavior: XAUUSD does not match XAU-USD.P
            resolve_map={},
        )
        wizard, key = self._open_new_order(desk)
        wizard.handle_callback(key, "symbol:other")
        screen = wizard.handle_text(key, "XAUUSD")
        assert screen is not None

        resolve_calls = [r for r in desk.requests if r.get("operation") == "resolve_instrument"]
        self.assertEqual(len(resolve_calls), 1)
        self.assertEqual(resolve_calls[0]["exchange"], "ondoperps")
        self.assertEqual(resolve_calls[0]["account"], "bitget")
        self.assertEqual(resolve_calls[0]["symbol"], "XAUUSD")
        self.assertEqual(screen.state, "instrument_unresolved")
        self.assertIn("Instrument Resolution", screen.text)
        self.assertIn("Source: XAUUSD", screen.text)
        self.assertIn("INSTRUMENT_NOT_FOUND", screen.text)
        # Never guess a native symbol into session state.
        st = wizard._state_for(key)
        self.assertIsNone(st.symbol)
        self.assertIsNone(st.order.get("symbol"))

    def test_successful_resolution_stored_and_used_after_agree(self) -> None:
        desk = ResolvingStubDesk(resolve_map={"XAU": "XAU-USD.P", "ETH": "ETH-USD.P"})
        wizard, key = self._open_new_order(desk)
        wizard.handle_callback(key, "symbol:other")
        screen = wizard.handle_text(key, "XAU")
        assert screen is not None
        self.assertEqual(screen.state, "instrument_confirm")
        self.assertIn("Select Instrument", screen.text)
        self.assertIn("Source: XAU", screen.text)
        self.assertIn("XAU-USD.P", screen.text)
        # Not committed yet.
        st = wizard._state_for(key)
        self.assertIsNone(st.symbol)
        labels = [b["text"] for row in screen.buttons for b in row]
        self.assertTrue(any("XAU-USD.P" in t for t in labels))
        self.assertTrue(any(t.startswith("Other") for t in labels))

        side = wizard.handle_callback(key, "resolve:pick:0")
        self.assertIn("Select Side:", side.text)
        self.assertIn("Symbol: XAU-USD.P", side.text)
        st = wizard._state_for(key)
        self.assertEqual(st.symbol, "XAU-USD.P")
        self.assertEqual(st.order.get("symbol"), "XAU-USD.P")
        self.assertEqual(st.requested_symbol, "XAU")

    def test_other_re_resolves_typed_symbol(self) -> None:
        """Other... free text still goes through resolve when the desk can look up."""
        desk = ResolvingStubDesk(
            resolve_map={"XAU": "XAU-USD.P", "XAU-USD.P": "XAU-USD.P"}
        )
        wizard, key = self._open_new_order(desk)
        wizard.handle_callback(key, "symbol:other")
        wizard.handle_text(key, "XAU")
        screen = wizard.handle_callback(key, "resolve:other")
        self.assertEqual(screen.state, "awaiting_native_symbol")
        self.assertIn("exchange-native", screen.text.lower())

        before = len(desk.requests)
        screen2 = wizard.handle_text(key, "XAU-USD.P")
        assert screen2 is not None
        # Re-resolve path: another resolve_instrument call.
        self.assertGreater(len(desk.requests), before)
        self.assertEqual(screen2.state, "instrument_confirm")
        self.assertIn("XAU-USD.P", screen2.text)
        side = wizard.handle_callback(key, "resolve:pick:0")
        self.assertIn("Symbol: XAU-USD.P", side.text)
        st = wizard._state_for(key)
        self.assertEqual(st.symbol, "XAU-USD.P")
        self.assertEqual(st.order.get("symbol"), "XAU-USD.P")

    def test_other_unknown_stays_unresolved_no_blind_commit(self) -> None:
        desk = ResolvingStubDesk(resolve_map={"XAU": "XAU-USD.P"})
        wizard, key = self._open_new_order(desk)
        wizard.handle_callback(key, "symbol:other")
        wizard.handle_text(key, "XAU")
        wizard.handle_callback(key, "resolve:other")
        screen = wizard.handle_text(key, "MANUAL-NATIVE.P")
        assert screen is not None
        self.assertEqual(screen.state, "instrument_unresolved")
        st = wizard._state_for(key)
        self.assertIsNone(st.symbol)
        self.assertIsNone(st.order.get("symbol"))

    def test_exact_native_still_shows_picker(self) -> None:
        """Exact venue id still requires a priced/pickable confirm."""
        desk = ResolvingStubDesk(resolve_map={"ETH-USD.P": "ETH-USD.P"})
        wizard, key = self._open_new_order(desk)
        wizard.handle_callback(key, "symbol:other")
        screen = wizard.handle_text(key, "ETH-USD.P")
        assert screen is not None
        self.assertEqual(screen.state, "instrument_confirm")
        self.assertIn("Select Instrument", screen.text)
        self.assertIn("ETH-USD.P", screen.text)
        st = wizard._state_for(key)
        self.assertIsNone(st.symbol)
        side = wizard.handle_callback(key, "resolve:pick:0")
        self.assertIn("Select Side:", side.text)
        self.assertIn("Symbol: ETH-USD.P", side.text)

    def test_ambiguous_never_guesses(self) -> None:
        desk = ResolvingStubDesk(
            resolve_map={
                "BTC": {
                    "_fail": True,
                    "code": "INSTRUMENT_AMBIGUOUS",
                    "message": "Multiple instruments match this symbol.",
                }
            }
        )
        wizard, key = self._open_new_order(desk)
        screen = wizard.handle_callback(key, "symbol:BTC")
        self.assertEqual(screen.state, "instrument_unresolved")
        self.assertIn("INSTRUMENT_AMBIGUOUS", screen.text)
        st = wizard._state_for(key)
        self.assertIsNone(st.symbol)
        labels = [b["text"] for row in screen.buttons for b in row]
        self.assertIn("Retry", labels)
        self.assertTrue(any(t.startswith("Other") for t in labels))
        self.assertTrue(any("Back" in t for t in labels))

    def test_legacy_desk_without_capabilities_unchanged(self) -> None:
        """Existing StubDesk-style desks (no capabilities) keep pass-through."""

        class LegacyDesk:
            def __init__(self) -> None:
                self.requests: List[Dict[str, Any]] = []

            def list_exchanges(self) -> List[str]:
                return ["hyperliquid"]

            def list_accounts(self, exchange: str) -> List[str]:
                return ["FLEX"]

            def execute(self, request: Dict[str, Any]):
                self.requests.append(dict(request))
                return make_failure(
                    operation=str(request.get("operation") or ""),
                    exchange=str(request.get("exchange") or ""),
                    account=str(request.get("account") or ""),
                    code="NOT_USED",
                    message="n/a",
                )

        desk = LegacyDesk()
        wizard = TradeWizard(tradedesk=desk)  # type: ignore[arg-type]
        key = ("chat",)
        wizard.open(key)
        wizard.handle_callback(key, "exchange:hyperliquid")
        wizard.handle_callback(key, "account:FLEX")
        wizard.handle_callback(key, "action:new_order")
        side = wizard.handle_callback(key, "symbol:BTC")
        self.assertIn("Select Side:", side.text)
        self.assertIn("Symbol: BTC", side.text)
        self.assertEqual(desk.requests, [])

    def test_ondoperps_existing_match_market_xauusd_is_none(self) -> None:
        """Exact existing agent result for XAUUSD — DO NOT fix the agent."""
        from plugins.trade.agents.x_ondoperps_agent import _match_market

        payload = {
            "perps": {
                "XAU-USD.P": {"market": "XAU-USD.P", "displayName": "Gold"},
                "ETH-USD.P": {"market": "ETH-USD.P", "displayName": "ETH"},
            }
        }
        self.assertIsNone(_match_market(payload, "XAUUSD"))
        # Control: short form still works via existing agent logic.
        inst = _match_market(payload, "XAU")
        assert inst is not None
        self.assertEqual(inst.symbol, "XAU-USD.P")

    def test_ondoperps_agent_exposes_resolve_instrument(self) -> None:
        from plugins.trade.agents import x_ondoperps_agent as ondo

        self.assertIn("resolve_instrument", ondo.capabilities())
        # execute dispatches resolve_instrument without us modifying the agent.
        src = Path(ondo.__file__).read_text(encoding="utf-8")
        self.assertIn('if operation == "resolve_instrument"', src)
        self.assertIn("def _resolve_instrument", src)


class TestTrade20Phase1WizardCallsRealAgentPath(unittest.TestCase):
    """Wizard → TradeDesk.execute(resolve_instrument) → agent.execute (mocked)."""

    def test_wizard_calls_agent_resolve_via_tradedesk_shape(self) -> None:
        desk = ResolvingStubDesk(resolve_map={"ETH": "ETH-USD.P"})
        wizard = TradeWizard(tradedesk=desk)  # type: ignore[arg-type]
        key = ("t",)
        wizard.open(key)
        wizard.handle_callback(key, "exchange:ondoperps")
        wizard.handle_callback(key, "account:bitget")
        wizard.handle_callback(key, "action:new_order")
        wizard.handle_callback(key, "symbol:other")
        wizard.handle_text(key, "eth")
        self.assertEqual(
            desk.requests[-1],
            {
                "operation": "resolve_instrument",
                "exchange": "ondoperps",
                "account": "bitget",
                "symbol": "ETH",
            },
        )


if __name__ == "__main__":
    unittest.main()
