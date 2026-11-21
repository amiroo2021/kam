"""Regression tests for the Pacifica ladder result rendering.

The Pacifica ladder block was live in the deployed wizard but absent
from ``HEAD``: the Telegram ladder-result screen showed the generic
"Submitted." summary for Pacifica, which hid the per-child accounting
(status, accepted, rejected, uncertain, not attempted) the Pacifica
agent already computes in its ``CanonicalLadderResult``.

These tests pin the rendering contract:

  * Pacifica renders the per-child breakdown.
  * An unverified or uncertain ladder prints the warning note.
  * Exchange errors are surfaced.
  * Other exchanges keep the generic summary.
  * ``ladder_obj`` is bound before use (a NameError here would break
    every Pacifica ladder result).

Offline only. The desk is stubbed, so no exchange is contacted.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.canonical import (  # noqa: E402
    CanonicalLadderResult,
    CanonicalResponse,
)
from plugins.trade.wizard import TradeWizard  # noqa: E402


def _ladder_result(**overrides: Any) -> CanonicalLadderResult:
    base: Dict[str, Any] = dict(
        symbol="SOL-PERP",
        side="buy",
        distribution="uniform",
        requested_order_count=250,
        submitted_order_count=250,
        requested_volume="100.0",
        submitted_volume="100.0",
        batch_count=5,
        verified=True,
        partial=False,
        status="success",
        accepted_child_count=250,
        rejected_child_count=0,
        uncertain_child_count=0,
        not_attempted_child_count=0,
    )
    base.update(overrides)
    return CanonicalLadderResult(**base)


class _StubDesk:
    """Minimal desk that returns one canned CanonicalResponse."""

    def __init__(self, response: CanonicalResponse) -> None:
        self._response = response
        self.requests: List[Dict[str, Any]] = []

    def execute(self, request: Dict[str, Any]) -> CanonicalResponse:
        self.requests.append(request)
        return self._response

    def capabilities(self, exchange: str) -> List[str]:
        return ["resolve_instrument", "ladder", "list_instruments"]

    def list_exchanges(self) -> List[str]:
        return ["pacifica", "lighter"]


class PacificaLadderResultTests(unittest.TestCase):
    """Rendering of the Pacifica ladder result screen."""

    def _render(self, response: CanonicalResponse) -> str:
        desk = _StubDesk(response)
        wizard = TradeWizard(desk)  # type: ignore[arg-type]
        state = wizard._state_for(("chat", 1))
        state.exchange = "pacifica"
        state.account = "amiroo"
        state.state = "ladder_result"
        state.ladder = {
            "symbol": "SOL-PERP",
            "side": "buy",
            "distribution": "uniform",
            "order_count": 250,
            "start_price": "55.0",
            "end_price": "30.1",
            "total_volume": "100.0",
        }
        # Render the ladder-result screen through the confirm handler's
        # reporting path.
        screen = wizard._handle_ladder_confirm(("chat", 1), "confirm")
        return screen.text

    def test_pacifica_reports_per_child_counts(self) -> None:
        response = CanonicalResponse(
            success=True,
            operation="ladder",
            exchange="pacifica",
            account="amiroo",
            ladder=_ladder_result(),
        )
        text = self._render(response)
        self.assertIn("Status: success", text)
        self.assertIn("Requested: 250", text)
        self.assertIn("Accepted: 250", text)
        self.assertIn("Rejected: 0", text)
        self.assertIn("Uncertain: 0", text)
        self.assertIn("Not attempted: 0", text)

    def test_uncertain_ladder_prints_the_warning_note(self) -> None:
        response = CanonicalResponse(
            success=False,
            operation="ladder",
            exchange="pacifica",
            account="amiroo",
            ladder=_ladder_result(
                status="partial_success",
                verified=False,
                accepted_child_count=120,
                rejected_child_count=1,
                uncertain_child_count=3,
                not_attempted_child_count=126,
                submitted_order_count=120,
            ),
        )
        text = self._render(response)
        self.assertIn("Accepted: 120", text)
        self.assertIn("Uncertain: 3", text)
        self.assertIn("Not attempted: 126", text)
        self.assertIn("Some orders were not verified", text)

    def test_exchange_errors_are_surfaced(self) -> None:
        response = CanonicalResponse(
            success=False,
            operation="ladder",
            exchange="pacifica",
            account="amiroo",
            ladder=_ladder_result(
                status="failed",
                verified=False,
                accepted_child_count=0,
                rejected_child_count=250,
                submitted_order_count=0,
                exchange_reason="insufficient margin",
            ),
        )
        text = self._render(response)
        self.assertIn("Exchange errors: insufficient margin", text)

    def test_ladder_obj_is_bound_before_use(self) -> None:
        """A NameError here breaks every Pacifica ladder result."""
        import inspect

        from plugins.trade import wizard as wizard_mod

        source = inspect.getsource(wizard_mod.TradeWizard._handle_ladder_confirm)
        bind = source.find('ladder_obj = getattr(response, "ladder", None)')
        use = source.find("ladder_obj is not None")
        self.assertGreater(bind, -1, "ladder_obj is never bound")
        self.assertGreater(use, -1, "ladder_obj is never used")
        self.assertLess(
            bind, use,
            "ladder_obj must be bound BEFORE it is used",
        )

    def test_other_exchanges_keep_the_generic_summary(self) -> None:
        desk = _StubDesk(
            CanonicalResponse(
                success=True,
                operation="ladder",
                exchange="lighter",
                account="bitget",
                ladder=_ladder_result(),
            )
        )
        wizard = TradeWizard(desk)  # type: ignore[arg-type]
        state = wizard._state_for(("chat", 2))
        state.exchange = "lighter"
        state.account = "bitget"
        state.state = "ladder_result"
        state.ladder = {
            "symbol": "SOL-PERP",
            "side": "buy",
            "distribution": "uniform",
            "order_count": 250,
            "start_price": "55.0",
            "end_price": "30.1",
            "total_volume": "100.0",
        }
        screen = wizard._handle_ladder_confirm(("chat", 2), "confirm")
        text = screen.text
        self.assertIn("Submitted.", text)
        self.assertNotIn("Not attempted:", text)


if __name__ == "__main__":
    unittest.main()
