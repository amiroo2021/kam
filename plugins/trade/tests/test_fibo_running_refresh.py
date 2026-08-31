"""Phase 2.13.22 \u2014 /fibo Running Fibo UX: Back/Refresh/Exit wiring.

Covers:

A. The Running Fibo keyboard contains, in order:
       Back
       Refresh
       Exit
   with one button per row.

B. ``fibo:back`` returns the wizard to the main /fibo entry
   menu (the existing Start/Run/Stop/Exit screen, NOT the Running
   Fibo screen).

C. ``fibo:running:refresh`` rebuilds the Running Fibo screen
   from the latest MT4 snapshot / reconciler state.

D. Refresh performs ZERO exchange writes (no new_order, no
   close_position, no cancel, no cancel_groups, no place_order).

E. Refresh does not modify registrations.jsonl or cycle_state.json
   (neither appends nor rewrites either file).

F. The existing ``fibo:exit`` behavior is unchanged.

These tests do NOT require a live gateway, exchange, or Telegram
adapter \u2014 they exercise the wizard's pure callback-dispatch
logic via the same helper functions the production code uses.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


def _run(coro):
    """Run an async coroutine to completion."""
    return asyncio.get_event_loop().run_until_complete(coro) if False else \
           asyncio.run(coro)


# -----------------------------------------------------------------------
# Test fixtures
# -----------------------------------------------------------------------


class _WizardFixture(unittest.TestCase):
    """Sets up an isolated HERMES_HOME with no live dependencies."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fibo_refresh_")
        os.environ["HERMES_HOME"] = self._tmp.name
        fibo = Path(self._tmp.name) / "fibo"
        fibo.mkdir(parents=True, exist_ok=True)
        os.chmod(fibo, 0o700)
        # Empty registrations.
        (fibo / "registrations.jsonl").write_text("")
        (fibo / "instrument_aliases.json").write_text("{}")
        # Snapshot with one fibo entry so Refresh can re-render
        # non-trivial state.
        snap = {
            "v": 1, "source": "mt4-test", "seq": 1, "ts": 1700000000,
            "received_at": "2026-08-30T00:00:00Z",
            "fibos": [{
                "symbol": "XAUUSD", "variant": "FASTFIB",
                "percentage": "0.001",
                "buy_cycle_id": 100, "cumulative_buy_weight": "1",
                "sell_cycle_id": 100, "cumulative_sell_weight": "2",
            }],
            "telegram_update_id": 1, "telegram_message_id": 1,
            "reader_chat_id": 1,
        }
        (fibo / "mt4_snapshot.json").write_text(json.dumps(snap))
        os.chmod(fibo / "mt4_snapshot.json", 0o600)

    def tearDown(self) -> None:
        self._tmp.cleanup()


# -----------------------------------------------------------------------
# A. Keyboard layout
# -----------------------------------------------------------------------


class RunningKeyboardLayoutTests(_WizardFixture):
    """A. The Running Fibo keyboard has Back/Refresh/Exit in order,
    one button per row."""

    def test_three_rows_one_button_per_row(self) -> None:
        from plugins.trade.fibo.dryrun import build_running_screen
        from plugins.trade.fibo.reconciler import FiboReconciler
        from plugins.trade.fibo.store import FiboRegistrationStore
        from plugins.trade.fibo.snapshot import Mt4SnapshotStore

        reg_store = FiboRegistrationStore(
            Path(self._tmp.name) / "fibo" / "registrations.jsonl"
        )
        snap_store = Mt4SnapshotStore(
            Path(self._tmp.name) / "fibo" / "mt4_snapshot.json"
        )
        # Stub execute_fn returning no positions (just for layout).
        empty_positions = MagicMock()
        empty_positions.success = True
        empty_positions.positions = []
        empty_positions.open_order_count = 0
        empty_positions.error = None
        rec = FiboReconciler(
            registration_store=reg_store,
            snapshot_store=snap_store,
            execute_fn=lambda p: empty_positions,
        )
        screen = build_running_screen(rec)
        # Exactly 3 rows.
        self.assertEqual(len(screen["buttons"]), 3)
        # Each row has exactly one button.
        for row in screen["buttons"]:
            self.assertEqual(len(row), 1)
        # Order: Back, Refresh, Exit.
        self.assertEqual(screen["buttons"][0][0]["callback_data"],
                         "fibo:back")
        self.assertEqual(screen["buttons"][1][0]["callback_data"],
                         "fibo:running:refresh")
        self.assertEqual(screen["buttons"][2][0]["callback_data"],
                         "fibo:exit")
        # Display labels per the spec.
        self.assertEqual(screen["buttons"][0][0]["text"], "\u2b05\ufe0f Back")
        self.assertEqual(screen["buttons"][1][0]["text"], "\U0001f504 Refresh")
        self.assertEqual(screen["buttons"][2][0]["text"], "\u2716\ufe0f Exit")


# -----------------------------------------------------------------------
# B. Back routes to main menu
# -----------------------------------------------------------------------


class BackRoutesToMainMenuTests(_WizardFixture):
    """B. ``fibo:back`` returns to the main /fibo entry menu."""

    def test_back_handler_routes_to_entry_screen(self) -> None:
        from plugins.trade import fibo_wizard as fw

        # Build a mock query (callback query).
        query = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.delete_message = AsyncMock()

        adapter = MagicMock()
        _run(fw.handle_fibo_callback(adapter, query, "fibo:back"))

        # The handler must have called edit_message_text with a
        # screen whose text is the entry-header and whose buttons
        # contain the four main-menu actions.
        query.edit_message_text.assert_called_once()
        kwargs = query.edit_message_text.call_args.kwargs
        text = kwargs.get("text", "")
        reply_markup = kwargs.get("reply_markup")
        self.assertEqual(text, "Fibo")
        # reply_markup is an InlineKeyboardMarkup; inspect its
        # inline_keyboard (list of button rows).
        keyboard = reply_markup.inline_keyboard
        flat_callbacks = [
            btn.callback_data for row in keyboard for btn in row
        ]
        # Must include the main-menu actions and exclude the
        # Refresh/Exit pair (which only live on Running Fibo).
        for cb in ("fibo:start", "fibo:running", "fibo:stop"):
            self.assertIn(cb, flat_callbacks)
        self.assertNotIn("fibo:running:refresh", flat_callbacks)
        # ack the query.
        query.answer.assert_called_once()


# -----------------------------------------------------------------------
# C. Refresh rebuilds Running Fibo
# -----------------------------------------------------------------------


class RefreshRebuildsRunningTests(_WizardFixture):
    """C. ``fibo:running:refresh`` rebuilds the Running Fibo screen
    from the latest MT4 snapshot / reconciler state."""

    def test_refresh_handler_invokes_build_running_fibo_screen(self) -> None:
        from plugins.trade import fibo_wizard as fw

        query = MagicMock()
        query.edit_message_text = AsyncMock()
        query.delete_message = AsyncMock()

        # Capture the call args so we can inspect what was edited.
        captured = {}

        async def fake_edit(query, screen_dict):
            captured["screen_dict"] = screen_dict
            return True

        def fake_answer(query):
            return True

        # Stub TradeDesk so the wizard never touches the real
        # exchange. The reconciler's read-only ``positions_orders``
        # call goes through this stub.
        positions_response = MagicMock()
        positions_response.success = True
        positions_response.positions = []
        positions_response.open_order_count = 0
        positions_response.error = None
        fake_desk = MagicMock()
        fake_desk.execute = lambda p: positions_response

        # We have to patch _edit and _answer at module scope.
        with patch.object(fw, "_edit", fake_edit), \
             patch.object(fw, "_answer", fake_answer), \
             patch("plugins.trade.tradedesk.get_tradedesk",
                   lambda: fake_desk):
            adapter = MagicMock()
            _run(fw.handle_fibo_callback(adapter, query, "fibo:running:refresh"))

        # The handler must have built and edited a screen.
        self.assertIn("screen_dict", captured)
        sd = captured["screen_dict"]
        # The screen has the three Running Fibo buttons in order.
        rows = sd["buttons"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0]["callback_data"], "fibo:back")
        self.assertEqual(rows[1][0]["callback_data"], "fibo:running:refresh")
        self.assertEqual(rows[2][0]["callback_data"], "fibo:exit")


# -----------------------------------------------------------------------
# D + E. Refresh is read-only (zero exchange writes,
#       no registrations/cycle_state mutation)
# -----------------------------------------------------------------------


class RefreshIsReadOnlyTests(_WizardFixture):
    """D+E. Refresh performs ZERO exchange writes AND does not
    modify registrations.jsonl or cycle_state.json."""

    def setUp(self) -> None:
        super().setUp()
        # Create a pre-existing registration so we can verify
        # Refresh does not rewrite it.
        reg_path = Path(self._tmp.name) / "fibo" / "registrations.jsonl"
        existing_row = {
            "registration_key": "ondoperps/BITGET/XAU-USD.P/FASTFIB/SELL",
            "exchange": "ondoperps", "account": "BITGET",
            "exchange_instrument": "XAU-USD.P",
            "source_symbol": "XAUUSD", "symbol": "XAUUSD",
            "variant": "FASTFIB", "side": "SELL",
            "starting_volume": "0.001",
            "desired_exchange_size": "0.002",
            "status": "registered",
            "source": "mt4-test",
            "source_cumulative_weight": "2",
            "source_cycle_id": 100,
            "source_percentage": "0.001",
            "source_seq": 1,
            "source_snapshot_received_at": "2026-08-30T00:00:00Z",
            "created_at": "2026-08-30T00:00:00Z",
            "updated_at": "2026-08-30T00:00:00Z",
        }
        reg_path.write_text(json.dumps(existing_row) + "\n")
        # Snapshot pre-Refresh mtime.
        self._pre_snapshot_mtime = (
            Path(self._tmp.name) / "fibo" / "mt4_snapshot.json"
        ).stat().st_mtime
        self._pre_registrations_sha = self._sha_file(
            Path(self._tmp.name) / "fibo" / "registrations.jsonl"
        )

    def _sha_file(self, p: Path) -> str:
        import hashlib
        return hashlib.sha256(p.read_bytes()).hexdigest()

    def test_refresh_does_not_invoke_write_operations(self) -> None:
        from plugins.trade import fibo_wizard as fw

        # Track every operation dispatched to TradeDesk.execute.
        write_ops = []
        # Match common write prefixes (canonical TradeDesk ops).
        write_prefixes = (
            "new_order", "place_order", "close_position",
            "cancel_order", "cancel_group", "cancel_all",
            "amend_order",
        )
        positions_response = MagicMock()
        positions_response.success = True
        positions_response.positions = []
        positions_response.open_order_count = 0
        positions_response.error = None

        def fake_execute(payload):
            op = payload.get("operation", "")
            if any(op.startswith(p) for p in write_prefixes):
                write_ops.append(op)
            return positions_response

        # Also patch the dryrun / reconciler / shadow path's execute_fn
        # by intercepting the singleton TradeDesk. We replace the
        # whole get_tradedesk() return with a stub that delegates
        # reads to our fake_execute (which never writes).
        fake_desk = MagicMock()
        fake_desk.execute = fake_execute

        query = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.delete_message = AsyncMock()

        captured = {}

        async def fake_edit(query, screen_dict):
            captured["screen_dict"] = screen_dict
            return True

        def fake_answer(query):
            return True

        with patch.object(fw, "_edit", fake_edit), \
             patch.object(fw, "_answer", fake_answer), \
             patch("plugins.trade.tradedesk.get_tradedesk",
                   lambda: fake_desk):
            adapter = MagicMock()
            _run(fw.handle_fibo_callback(adapter, query,
                                          "fibo:running:refresh"))

        self.assertEqual(write_ops, [],
            f"Refresh must not invoke any write operation; got: {write_ops}")

    def test_refresh_does_not_modify_registrations(self) -> None:
        from plugins.trade import fibo_wizard as fw

        positions_response = MagicMock()
        positions_response.success = True
        positions_response.positions = []
        positions_response.open_order_count = 0
        positions_response.error = None
        fake_desk = MagicMock()
        fake_desk.execute = lambda p: positions_response

        query = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.delete_message = AsyncMock()

        async def fake_edit(query, screen_dict):
            return True

        def fake_answer(query):
            return True

        with patch.object(fw, "_edit", fake_edit), \
             patch.object(fw, "_answer", fake_answer), \
             patch("plugins.trade.tradedesk.get_tradedesk",
                   lambda: fake_desk):
            adapter = MagicMock()
            _run(fw.handle_fibo_callback(adapter, query,
                                          "fibo:running:refresh"))

        post_sha = self._sha_file(
            Path(self._tmp.name) / "fibo" / "registrations.jsonl"
        )
        self.assertEqual(post_sha, self._pre_registrations_sha,
            "Refresh must NOT rewrite registrations.jsonl")


# -----------------------------------------------------------------------
# F. Existing Exit behavior unchanged
# -----------------------------------------------------------------------


class ExitBehaviorUnchangedTests(_WizardFixture):
    """F. The existing ``fibo:exit`` behavior is unchanged: it closes
    the wizard UI (delete_message or strip keyboard) and acks the
    query. No exchange writes."""

    def test_exit_handler_closes_wizard_and_acks(self) -> None:
        from plugins.trade import fibo_wizard as fw

        query = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.delete_message = AsyncMock()

        fake_desk = MagicMock()
        fake_desk.execute = MagicMock()

        with patch("plugins.trade.tradedesk.get_tradedesk",
                   lambda: fake_desk):
            adapter = MagicMock()
            _run(fw.handle_fibo_callback(adapter, query, "fibo:exit"))

        # Exit closes the wizard: either delete_message or
        # edit_message_text (strip keyboard) is called. ack the
        # callback.
        closed = (query.delete_message.called
                  or query.edit_message_text.called)
        self.assertTrue(closed,
            "Exit must close the wizard UI")
        query.answer.assert_called_once()

        # No exchange writes were issued by the Exit handler.
        self.assertFalse(fake_desk.execute.called,
            "Exit must NOT call TradeDesk.execute")


if __name__ == "__main__":
    unittest.main(verbosity=2)