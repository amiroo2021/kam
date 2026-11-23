"""Regression tests for the merged instrument-lookup infrastructure errors.

Covers:
  * ``is_instrument_lookup_unavailable`` (production-only helper merged into
    ``plugins/trade/instrument_picker.py``).
  * ``TradeWizard._instrument_lookup_capabilities`` (production-only helper
    merged into ``plugins/trade/wizard.py``).
  * The ``/trade`` instrument-resolution flow rendering "lookup unavailable"
    instead of "not found" when the venue lookup itself failed.

Offline only. No live exchange calls, no network.
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

from plugins.trade import instrument_picker  # noqa: E402
from plugins.trade.instrument_picker import (  # noqa: E402
    INSTRUMENT_LOOKUP_INFRASTRUCTURE_ERRORS,
    is_instrument_lookup_unavailable,
)
from plugins.trade.wizard import TradeWizard  # noqa: E402


class InstrumentLookupUnavailableTests(unittest.TestCase):
    def test_infrastructure_error_codes_are_recognised(self) -> None:
        for code in ("AGENT_EXCEPTION", "ACCOUNT_MISMATCH", "INVALID_AGENT_RESPONSE",
                     "UNKNOWN_EXCHANGE", "ACCOUNT_NOT_CONFIGURED"):
            self.assertTrue(
                is_instrument_lookup_unavailable(code),
                f"{code} must be treated as a lookup infrastructure failure",
            )
            self.assertIn(code, INSTRUMENT_LOOKUP_INFRASTRUCTURE_ERRORS)

    def test_code_matching_is_case_and_whitespace_insensitive(self) -> None:
        self.assertTrue(is_instrument_lookup_unavailable("  agent_exception "))
        self.assertTrue(is_instrument_lookup_unavailable("AGENT_EXCEPTION"))

    def test_not_found_is_not_an_infrastructure_failure(self) -> None:
        self.assertFalse(is_instrument_lookup_unavailable("INSTRUMENT_NOT_FOUND"))
        self.assertFalse(is_instrument_lookup_unavailable("SYMBOL_UNKNOWN"))
        self.assertFalse(is_instrument_lookup_unavailable(None))
        self.assertFalse(is_instrument_lookup_unavailable(""))

    def test_data_lookup_performed_false_is_unavailable(self) -> None:
        self.assertTrue(is_instrument_lookup_unavailable("ANY_CODE", {"lookup_performed": False}))
        self.assertFalse(is_instrument_lookup_unavailable("ANY_CODE", {"lookup_performed": True}))

    def test_data_status_unavailable_is_unavailable(self) -> None:
        self.assertTrue(is_instrument_lookup_unavailable("ANY_CODE", {"status": "Unavailable"}))
        self.assertFalse(is_instrument_lookup_unavailable("ANY_CODE", {"status": "ok"}))

    def test_data_flag_overrides_a_non_infrastructure_code(self) -> None:
        # A venue-level "not found" that also reports the lookup never ran is
        # an infrastructure failure, not a genuine missing instrument.
        self.assertTrue(
            is_instrument_lookup_unavailable("INSTRUMENT_NOT_FOUND", {"lookup_performed": False})
        )


class InstrumentLookupCapabilityTests(unittest.TestCase):
    def _wizard(self, caps: Any, raises: Optional[Exception] = None) -> TradeWizard:
        wiz = TradeWizard()
        desk = mock.Mock()
        if raises is not None:
            desk.capabilities.side_effect = raises
        else:
            desk.capabilities.return_value = caps
        wiz._desk = desk
        return wiz

    def test_capabilities_are_returned_for_a_known_exchange(self) -> None:
        wiz = self._wizard(["resolve_instrument", "new_order"])
        found, error = wiz._instrument_lookup_capabilities("hyperliquid")
        self.assertIsNone(error)
        self.assertEqual(found, ["resolve_instrument", "new_order"])

    def test_missing_exchange_yields_no_capabilities_and_no_error(self) -> None:
        wiz = self._wizard([])
        found, error = wiz._instrument_lookup_capabilities(None)
        self.assertEqual(found, [])
        self.assertIsNone(error)

    def test_agent_exception_becomes_a_capability_error(self) -> None:
        wiz = self._wizard(None, raises=RuntimeError("boom"))
        found, error = wiz._instrument_lookup_capabilities("hyperliquid")
        self.assertEqual(found, [])
        self.assertIsNotNone(error)
        assert error is not None
        code, message = error
        self.assertEqual(code, "AGENT_EXCEPTION")
        self.assertIn("boom", message)

    def test_invalid_capability_response_becomes_a_capability_error(self) -> None:
        wiz = self._wizard({"not": "a list"})
        found, error = wiz._instrument_lookup_capabilities("hyperliquid")
        self.assertEqual(found, [])
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error[0], "INVALID_AGENT_RESPONSE")

    def test_non_string_capabilities_are_filtered_out(self) -> None:
        wiz = self._wizard(["resolve_instrument", 7, None, "list_instruments"])
        found, error = wiz._instrument_lookup_capabilities("hyperliquid")
        self.assertIsNone(error)
        self.assertEqual(found, ["resolve_instrument", "list_instruments"])


class InstrumentLookupRenderingTests(unittest.TestCase):
    def test_unresolved_screen_reports_lookup_unavailable(self) -> None:
        wiz = TradeWizard()
        state = wiz._state_for(("chat", 1))
        state.resolved_instrument = {
            "requested_symbol": "BTC",
            "error_code": "AGENT_EXCEPTION",
            "error_message": "lookup failed",
        }
        with mock.patch.object(
            TradeWizard, "_flow_title", return_value="Trade"
        ), mock.patch.object(TradeWizard, "_candidate_symbol", return_value=None):
            screen = wiz._render_instrument_unresolved(("chat", 1))
        self.assertIn("lookup unavailable", screen.text)
        self.assertNotIn("Resolved: (not found)", screen.text)

    def test_unresolved_screen_reports_not_found_for_genuine_misses(self) -> None:
        wiz = TradeWizard()
        state = wiz._state_for(("chat", 2))
        state.resolved_instrument = {
            "requested_symbol": "NOPE",
            "error_code": "INSTRUMENT_NOT_FOUND",
            "error_message": "not found",
        }
        with mock.patch.object(
            TradeWizard, "_flow_title", return_value="Trade"
        ), mock.patch.object(TradeWizard, "_candidate_symbol", return_value=None):
            screen = wiz._render_instrument_unresolved(("chat", 2))
        self.assertIn("Resolved: (not found)", screen.text)

    def test_helper_is_exported_from_instrument_picker(self) -> None:
        self.assertTrue(hasattr(instrument_picker, "is_instrument_lookup_unavailable"))
        self.assertTrue(hasattr(instrument_picker, "INSTRUMENT_LOOKUP_INFRASTRUCTURE_ERRORS"))


if __name__ == "__main__":
    unittest.main()
