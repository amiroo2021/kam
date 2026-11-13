"""Wizard cancel-result rendering and live-confirm dry_run flag."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from plugins.trade.canonical import (  # noqa: E402
    CanonicalCancelGroupResult,
    CanonicalOrderGroup,
    make_success,
)
from plugins.trade.wizard import TradeWizard  # noqa: E402


class SequenceDesk:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: List[Dict[str, Any]] = []

    def list_exchanges(self) -> List[str]:
        return ["hyperliquid"]

    def list_accounts(self, exchange: str) -> List[str]:
        return ["TRADE"] if exchange == "hyperliquid" else []

    def execute(self, request: Dict[str, Any]):
        self.requests.append(dict(request))
        idx = min(len(self.requests) - 1, len(self.responses) - 1)
        return self.responses[idx]


def _group() -> CanonicalOrderGroup:
    return CanonicalOrderGroup(
        symbol="HYPE",
        side="sell",
        order_count=2,
        total_size="2",
        vwap="8",
        min_price="7",
        max_price="9",
    )


def _positions_response():
    return make_success(
        operation="positions_orders",
        exchange="hyperliquid",
        account="TRADE",
        open_order_count=2,
        order_groups=[_group()],
    )


def _cancel_result(*, verified: bool, cancelled: int, remaining: int, status: str, targeted: int = 2):
    return make_success(
        operation="cancel_order_group",
        exchange="hyperliquid",
        account="TRADE",
        cancel_group=CanonicalCancelGroupResult(
            symbol="HYPE",
            side="sell",
            targeted_order_count=targeted,
            cancelled_order_count=cancelled,
            confirmed_absent_count=cancelled,
            remaining_target_count=remaining,
            verified=verified,
            status=status,
            requested_cancel_count=targeted,
            verified_cancel_count=cancelled,
        ),
    )


def _confirm(desk: SequenceDesk):
    wizard = TradeWizard(tradedesk=desk)  # type: ignore[arg-type]
    key = ("chat",)
    wizard.open(key)
    wizard.handle_callback(key, "exchange:hyperliquid")
    wizard.handle_callback(key, "account:TRADE")
    wizard.handle_callback(key, "action:cancel_orders")
    wizard.handle_callback(key, "cancel_group:HYPE:sell")
    screen = wizard.handle_callback(key, "confirm")
    cancel_req = next(r for r in desk.requests if r.get("operation") == "cancel_order_group")
    return screen, cancel_req


class CancelGroupResultScreenTests(unittest.TestCase):
    def test_confirmed_cancellation_sends_dry_run_false(self) -> None:
        desk = SequenceDesk(
            [_positions_response(), _positions_response(), _cancel_result(verified=True, cancelled=2, remaining=0, status="submitted")]
        )
        _screen, cancel_req = _confirm(desk)
        self.assertEqual(cancel_req.get("dry_run"), False)
        self.assertEqual(cancel_req["symbol"], "HYPE")
        self.assertEqual(cancel_req["side"], "sell")
        self.assertEqual(cancel_req["account"], "TRADE")

    def test_dry_run_result_never_shows_cancellation_success(self) -> None:
        desk = SequenceDesk(
            [_positions_response(), _positions_response(), _cancel_result(verified=False, cancelled=0, remaining=2, status="dry_run")]
        )
        screen, _ = _confirm(desk)
        self.assertIn("🧪 Cancellation Preview", screen.text)
        self.assertNotIn("✅ Orders Cancelled", screen.text)
        self.assertIn("Targeted: 2", screen.text)
        self.assertIn("Cancelled: 0", screen.text)
        self.assertIn("Remaining: 2", screen.text)
        self.assertIn("Failed: 2", screen.text)

    def test_zero_cancelled_failed_verification_never_shows_success(self) -> None:
        desk = SequenceDesk(
            [_positions_response(), _positions_response(), _cancel_result(verified=False, cancelled=0, remaining=2, status="partial")]
        )
        screen, _ = _confirm(desk)
        self.assertIn("❌ Cancellation Failed", screen.text)
        self.assertNotIn("✅ Orders Cancelled", screen.text)
        self.assertIn("Targeted: 2", screen.text)
        self.assertIn("Cancelled: 0", screen.text)
        self.assertIn("Remaining: 2", screen.text)
        self.assertIn("Failed: 2", screen.text)

    def test_verified_cancellation_displays_success(self) -> None:
        desk = SequenceDesk(
            [_positions_response(), _positions_response(), _cancel_result(verified=True, cancelled=2, remaining=0, status="submitted")]
        )
        screen, _ = _confirm(desk)
        self.assertIn("✅ Orders Cancelled", screen.text)
        self.assertNotIn("🧪 Cancellation Preview", screen.text)
        self.assertNotIn("❌ Cancellation Failed", screen.text)
        self.assertIn("Targeted: 2", screen.text)
        self.assertIn("Cancelled: 2", screen.text)
        self.assertIn("Remaining: 0", screen.text)
        self.assertIn("Failed: 0", screen.text)

    def test_partial_remaining_shows_unverified(self) -> None:
        desk = SequenceDesk(
            [_positions_response(), _positions_response(), _cancel_result(verified=False, cancelled=1, remaining=1, status="partial")]
        )
        screen, _ = _confirm(desk)
        self.assertIn("⚠️ Cancellation Unverified", screen.text)
        self.assertNotIn("✅ Orders Cancelled", screen.text)
        self.assertIn("Targeted: 2", screen.text)
        self.assertIn("Cancelled: 1", screen.text)
        self.assertIn("Remaining: 1", screen.text)
        self.assertIn("Failed: 1", screen.text)


if __name__ == "__main__":
    unittest.main()
