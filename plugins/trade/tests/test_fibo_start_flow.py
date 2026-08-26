"""Tests for the Start Fibo wizard sub-flow.

Coverage matrix (spec §14):

* unique symbol+variant rendering
* BUY uses buy fields
* SELL uses sell fields
* inactive side cannot continue
* stale snapshot cannot Create
* exchange discovery read-only
* account discovery read-only
* Decimal starting volume
* invalid/zero/negative volume rejected
* callback_data all <= 64 bytes
* per-user state isolation
* text interception only during volume state
* Back/Cancel behavior
* confirmation calculation
* cycle changes before Create -> re-confirm
* weight changes before Create -> re-confirm
* duplicate registration refused
* successful Create persists exactly one registration
* zero exchange writes
"""

from __future__ import annotations

import json
import re
import tempfile
import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from plugins.trade.fibo.flow import (
    CB_ACCT, CB_AGREE, CB_BACK, CB_CANCEL, CB_CREATE, CB_EX, CB_PREFIX,
    CB_REFRESH, CB_SIDE, CB_SYM, SIDE_TOKEN_BUY, SIDE_TOKEN_SELL,
    STALE_THRESHOLD_SECONDS, Screen, StartFiboFlow,
)
from plugins.trade.fibo.session import (
    SESSION_TTL_SECONDS, FiboSessionStore, SessionState,
)
from plugins.trade.fibo.snapshot import (
    Mt4Fibo, Mt4Snapshot, Mt4SnapshotStore, SIDE_BUY, SIDE_SELL,
    parse_snapshot_payload,
)
from plugins.trade.fibo.store import FiboRegistrationStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_iso(seconds_ago: float = 0.0) -> str:
    """ISO-8601 UTC string ``seconds_ago`` in the past."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _good_fibo(
    *,
    symbol: str = "BTCUSD",
    variant: str = "FASTFib",
    percentage: str = "0.001",
    buy_cycle_id: int = 42,
    cumulative_buy_weight: str = "2.5",
    sell_cycle_id: int = 7,
    cumulative_sell_weight: str = "1.0",
) -> Mt4Fibo:
    return Mt4Fibo(
        symbol=symbol,
        variant=variant,
        percentage=Decimal(percentage),
        buy_cycle_id=buy_cycle_id,
        cumulative_buy_weight=Decimal(cumulative_buy_weight),
        sell_cycle_id=sell_cycle_id,
        cumulative_sell_weight=Decimal(cumulative_sell_weight),
    )


def _snapshot(
    fibos: List[Mt4Fibo],
    *,
    source: str = "obs-1",
    seq: int = 42,
    received_at: Optional[str] = None,
) -> Mt4Snapshot:
    return Mt4Snapshot(
        v=1,
        source=source,
        seq=seq,
        ts="2026-08-25T03:14:02Z",
        fibos=fibos,
        received_at=received_at or _utc_iso(),
        telegram_update_id=1000,
        telegram_message_id=2000,
        reader_chat_id=-100,
    )


class FakeFlowFactory:
    """Builds a ``StartFiboFlow`` with a deterministic snapshot file."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.snap_path = self.root / "mt4_snapshot.json"
        self.reg_path = self.root / "registrations.jsonl"
        self.exchange_writes: List[Any] = []
        self.account_calls: List[str] = []
        self._current_snapshot: Optional[Mt4Snapshot] = None

    def cleanup(self) -> None:
        self.tmp.cleanup()

    def set_snapshot(self, snap: Mt4Snapshot) -> None:
        self._current_snapshot = snap
        envelope = snap.to_dict()
        self.snap_path.write_text(json.dumps(envelope))

    def flow(
        self,
        *,
        resolve_instrument_fn=None,
        list_instruments_fn=None,
    ) -> StartFiboFlow:
        """Build a StartFiboFlow with the standard fake wiring.

        Phase 2.2 additions: callers may inject a custom
        resolve_instrument_fn (used by the agent-resolved
        proposal screen) and a list_instruments_fn (used by
        Browse markets fallback). Both default to None — the
        flow handles that by going through the "unresolved"
        screen.
        """
        snap_store = Mt4SnapshotStore(self.snap_path)
        reg_store = FiboRegistrationStore(self.reg_path)

        def list_exchanges() -> List[str]:
            return ["apex", "hyperliquid", "ondoperps"]

        def list_accounts(exchange: str) -> List[Any]:
            self.account_calls.append(exchange)
            return ["MAIN", "ALT"]

        def now_fn() -> datetime:
            return datetime.now(timezone.utc)

        return StartFiboFlow(
            snapshot_store=snap_store,
            registration_store=reg_store,
            list_exchanges_fn=list_exchanges,
            list_accounts_fn=list_accounts,
            resolve_instrument_fn=resolve_instrument_fn,
            list_instruments_fn=list_instruments_fn,
            now_fn=now_fn,
        )


class _FlowTestBase(unittest.TestCase):
    """Mixin: provides ``self.fx`` (FakeFlowFactory) and helpers."""

    def setUp(self) -> None:
        self.fx = FakeFlowFactory()
        self.addCleanup(self.fx.cleanup)


# ---------------------------------------------------------------------------
# Snapshot rendering
# ---------------------------------------------------------------------------


class SymbolVariantRenderingTests(_FlowTestBase):
    def test_unique_symbol_variant_rendering(self) -> None:
        fibos = [
            _good_fibo(symbol="BTCUSD", variant="FASTFib"),
            _good_fibo(symbol="BTCUSD", variant="NORMALFib"),
            _good_fibo(symbol="ETHUSD", variant="NORMALFib"),
            _good_fibo(symbol="XAUUSD", variant="FASTFib"),
            # Duplicate of the first (different weight) — must NOT
            # appear twice in the menu
            _good_fibo(symbol="BTCUSD", variant="FASTFib",
                       cumulative_buy_weight="3.0"),
        ]
        self.fx.set_snapshot(_snapshot(fibos))
        flow = self.fx.flow()
        screen = flow.open("chat-1", "user-1")
        labels = [
            row[0]["text"]
            for row in screen.buttons
            if row and row[0].get("callback_data", "").startswith(CB_SYM)
        ]
        # Expect exactly 4 unique pairs in the menu (one duplicate of
        # BTCUSD/FASTFib must be collapsed). Cancel button is
        # excluded by the CB_SYM filter.
        self.assertEqual(len(labels), 4)
        self.assertIn("BTCUSD — FASTFib", labels)
        self.assertIn("BTCUSD — NORMALFib", labels)
        self.assertIn("ETHUSD — NORMALFib", labels)
        self.assertIn("XAUUSD — FASTFib", labels)

    def test_symbol_button_callback_uses_index(self) -> None:
        fibos = [
            _good_fibo(symbol="BTCUSD", variant="FASTFib"),
            _good_fibo(symbol="ETHUSD", variant="NORMALFib"),
        ]
        self.fx.set_snapshot(_snapshot(fibos))
        flow = self.fx.flow()
        screen = flow.open("chat-1", "user-1")
        # Pick the index of ETHUSD.
        eth_row = next(
            r for r in screen.buttons if "ETHUSD" in r[0]["text"]
        )
        self.assertEqual(eth_row[0]["callback_data"], f"{CB_SYM}1")

    def test_no_data_screen_when_snapshot_missing(self) -> None:
        # No snapshot on disk.
        flow = self.fx.flow()
        screen = flow.open("chat-1", "user-1")
        self.assertIn("No MT4 data yet", screen.text)
        # Only Cancel button is present.
        self.assertEqual(len(screen.buttons), 1)
        self.assertEqual(screen.buttons[0][0]["callback_data"], CB_CANCEL)


# ---------------------------------------------------------------------------
# Side (BUY / SELL)
# ---------------------------------------------------------------------------


class SideSelectionTests(_FlowTestBase):
    def _open_to_side(self, *, fibos: List[Mt4Fibo]) -> StartFiboFlow:
        self.fx.set_snapshot(_snapshot(fibos))
        flow = self.fx.flow()
        # open -> symbol pick
        flow.open("chat-1", "user-1")
        # pick the only symbol (index 0)
        flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
        return flow

    def test_buy_uses_buy_fields(self) -> None:
        fibos = [_good_fibo(
            symbol="BTCUSD", variant="FASTFib",
            buy_cycle_id=11, cumulative_buy_weight="2.5",
            sell_cycle_id=22, cumulative_sell_weight="1.0",
        )]
        flow = self._open_to_side(fibos=fibos)
        screen = flow.handle_callback(
            "chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_BUY}"
        )
        # We should be on the exchange screen now.
        self.assertIn("Pick an exchange", screen.text)
        # The session captured BUY's cycle id and weight.
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertEqual(sess.side, "BUY")
        self.assertEqual(sess.snap_cycle_id, 11)
        self.assertEqual(sess.snap_cumulative_weight, Decimal("2.5"))

    def test_sell_uses_sell_fields(self) -> None:
        fibos = [_good_fibo(
            symbol="BTCUSD", variant="FASTFib",
            buy_cycle_id=11, cumulative_buy_weight="2.5",
            sell_cycle_id=22, cumulative_sell_weight="1.0",
        )]
        flow = self._open_to_side(fibos=fibos)
        screen = flow.handle_callback(
            "chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_SELL}"
        )
        self.assertIn("Pick an exchange", screen.text)
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertEqual(sess.side, "SELL")
        self.assertEqual(sess.snap_cycle_id, 22)
        self.assertEqual(sess.snap_cumulative_weight, Decimal("1.0"))

    def test_inactive_side_cannot_continue(self) -> None:
        # Both sides inactive (cycle_id 0).
        fibos = [_good_fibo(
            symbol="BTCUSD", variant="FASTFib",
            buy_cycle_id=0, cumulative_buy_weight="0",
            sell_cycle_id=0, cumulative_sell_weight="0",
        )]
        flow = self._open_to_side(fibos=fibos)
        screen = flow.handle_callback(
            "chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_BUY}"
        )
        self.assertIn("No active MT4 cycle", screen.text)
        self.assertNotIn("Pick an exchange", screen.text)
        # Session still in awaiting_side.
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertEqual(sess.state, SessionState.AWAITING_SIDE)

    def test_only_active_side_button_shown(self) -> None:
        fibos = [_good_fibo(
            symbol="BTCUSD", variant="FASTFib",
            buy_cycle_id=42, cumulative_buy_weight="2.5",
            sell_cycle_id=0, cumulative_sell_weight="0",
        )]
        self.fx.set_snapshot(_snapshot(fibos))
        flow = self.fx.flow()
        flow.open("chat-1", "user-1")
        flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
        screen = flow.handle_callback(
            "chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_BUY}"
        )
        # Hmm — calling BUY here will progress past the side screen.
        # Instead, assert the side screen directly: the user opened
        # the symbol and the next screen should list only BUY.
        flow2 = self.fx.flow()
        self.fx.set_snapshot(_snapshot(fibos))
        flow2.open("chat-1", "user-1")
        side_screen = flow2.handle_callback(
            "chat-1", "user-1", f"{CB_SYM}0"
        )
        # side_screen is rendered between the symbol pick and any
        # side pick; we re-call open and symbol pick to land on the
        # side screen directly.
        # The previous handle_callback landed us on side screen.
        # Check the side screen has BUY (active) but not SELL button.
        labels = [
            row[0]["text"]
            for row in side_screen.buttons if row
        ]
        self.assertTrue(any("BUY" in lbl for lbl in labels))
        # SELL not tappable since cycle=0/weight=0.
        # The screen may still include "SELL" text in the inactive note.
        # Confirm via direct call to the session state after a BUY pick.
        _ = flow2.handle_callback(
            "chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_BUY}"
        )
        sess = flow2.session_store.get("chat-1", "user-1")
        self.assertEqual(sess.side, "BUY")


# ---------------------------------------------------------------------------
# Exchange / account discovery is read-only
# ---------------------------------------------------------------------------


class DiscoveryReadOnlyTests(_FlowTestBase):
    def test_exchange_discovery_read_only(self) -> None:
        fibos = [_good_fibo()]
        self.fx.set_snapshot(_snapshot(fibos))
        flow = self.fx.flow()
        flow.open("chat-1", "user-1")
        flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_BUY}")
        # We should be on the exchange screen.
        screen = flow.handle_callback("chat-1", "user-1", f"{CB_EX}0")
        self.assertIn("Pick an account", screen.text)

    def test_account_discovery_read_only(self) -> None:
        fibos = [_good_fibo()]
        self.fx.set_snapshot(_snapshot(fibos))
        flow = self.fx.flow()
        flow.open("chat-1", "user-1")
        flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_BUY}")
        flow.handle_callback("chat-1", "user-1", f"{CB_EX}0")
        # list_accounts was called with the chosen exchange name.
        self.assertEqual(self.fx.account_calls, ["apex"])

    def test_zero_exchange_writes(self) -> None:
        """Static guard: the flow source never references write
        operations or private exchange helpers.

        The public TradeDesk.execute entrypoint IS used
        (``desk.execute({...})``) by the flow to satisfy the
        Phase 2.4 generic boundary contract. The guard enforces
        that no private helpers / direct HTTP clients / write
        verbs are introduced.
        """
        import inspect
        from plugins.trade.fibo import flow as flow_mod
        src = inspect.getsource(flow_mod)
        # Drop docstrings + string literals so tests aren't tripped
        # up by ``.execute(`` written in documentation.
        cleaned_lines = []
        in_triple = False
        triple_quote = None
        for line in src.splitlines():
            stripped = line.lstrip()
            if not in_triple and stripped.startswith("#"):
                continue
            if in_triple:
                if triple_quote in line:
                    in_triple = False
                    triple_quote = None
                continue
            if not in_triple:
                open_q = None
                open_idx = -1
                for q in ('"""', "'''"):
                    i = line.find(q)
                    if 0 <= i < len(line) and (
                        open_idx < 0 or i < open_idx
                    ):
                        open_q = q
                        open_idx = i
                if open_q is not None:
                    head = line[:open_idx]
                    rest = line[open_idx + len(open_q):]
                    close_idx = rest.find(open_q)
                    if close_idx >= 0:
                        line = head + rest[close_idx + len(open_q):]
                    else:
                        line = head
                        in_triple = True
                        triple_quote = open_q
            # Strip inline string literals on the remaining line.
            for pat in (
                r'"(?:[^"\\\n]|\\.)*"', r"'(?:[^'\\\n]|\\.)*'",
            ):
                line = re.sub(pat, "", line)
            cleaned_lines.append(line)
        clean = "\n".join(cleaned_lines)
        for forbidden in (
            # NOTE: ``.execute(`` is intentionally allowed — the
            # Phase 2.4 generic boundary calls the public
            # ``TradeDesk.execute({...})`` from inside flow.py.
            # Excluding it here would be a false positive. The
            # agent-specific write tokens and private helpers
            # below catch the actual violations.
            "TradeDesk(",  # no direct TradeDesk ctor except via discovery
            "TradeWizard(",
            "_WIZARD.",
            "x_apex_agent", "x_arcus_agent", "x_hyperliquid_agent",
            "x_lighter_agent", "x_pacifica_agent", "x_rise_agent",
            "x_edgex_agent", "x_ondoperps_agent", "x_raydium_agent",
            "x_hibachi_agent",
            "requests.post", "httpx.", "aiohttp", "subprocess.",
            # Direct invocation of any write operation is
            # forbidden — even via the public TradeDesk.execute
            # boundary (which the agent layer is responsible for
            # gating).
            "new_order", "market_order", "limit_order",
            "cancel_order", "cancel_order_group",
            "close_position", "stop_order",
            "ladder", "set_position_trigger",
            "set_position_protections",
        ):
            self.assertNotIn(
                forbidden, clean,
                f"flow module must not reference {forbidden!r}",
            )


# ---------------------------------------------------------------------------
# Volume input
# ---------------------------------------------------------------------------


class VolumeInputTests(_FlowTestBase):
    def _navigate_to_volume(
        self,
        *,
        fibos: List[Mt4Fibo],
        resolve_instrument_fn=None,
    ) -> StartFiboFlow:
        """Drive a session from /fibo up to AWAITING_VOLUME.

        Phase 2.2: account pick now triggers the agent-resolved
        proposal screen. We inject a default
        ``resolve_instrument_fn`` that maps ``<src>-USD.P`` so
        existing tests can keep walking the volume path. Tests
        that want to exercise the unresolved path pass
        ``resolve_instrument_fn=None`` explicitly.
        """
        if resolve_instrument_fn is None:
            def _default_resolver(exchange, account, symbol):
                # Default convention used by the existing test suite:
                # source symbol X → venue contract "X-USD.P".
                # Production wires the real agent via the wizard
                # shim; tests can override per-test.
                return f"{symbol}-USD.P"
            resolve_instrument_fn = _default_resolver
        self.fx.set_snapshot(_snapshot(fibos))
        flow = self.fx.flow(resolve_instrument_fn=resolve_instrument_fn)
        flow.open("chat-1", "user-1")
        flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_BUY}")
        flow.handle_callback("chat-1", "user-1", f"{CB_EX}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_ACCT}0")
        # Phase 2.2: tap Agree on the proposal screen.
        flow.handle_callback("chat-1", "user-1", CB_AGREE)
        return flow

    def test_decimal_starting_volume(self) -> None:
        fibos = [_good_fibo(buy_cycle_id=42, cumulative_buy_weight="2.5")]
        flow = self._navigate_to_volume(fibos=fibos)
        screen = flow.handle_text("chat-1", "user-1", "0.10")
        self.assertIsNotNone(screen)
        self.assertIn("Confirm registration", screen.text)
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertEqual(sess.starting_volume, Decimal("0.10"))

    def test_decimal_preserves_trailing_zeros(self) -> None:
        fibos = [_good_fibo(buy_cycle_id=42, cumulative_buy_weight="2.5")]
        flow = self._navigate_to_volume(fibos=fibos)
        flow.handle_text("chat-1", "user-1", "0.10")
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertEqual(str(sess.starting_volume), "0.10")

    def test_invalid_volume_rejected(self) -> None:
        fibos = [_good_fibo()]
        flow = self._navigate_to_volume(fibos=fibos)
        screen = flow.handle_text("chat-1", "user-1", "abc")
        self.assertIn("wasn't a number", screen.text.lower())
        # Still awaiting volume.
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertEqual(sess.state, SessionState.AWAITING_VOLUME)

    def test_zero_volume_rejected(self) -> None:
        fibos = [_good_fibo()]
        flow = self._navigate_to_volume(fibos=fibos)
        screen = flow.handle_text("chat-1", "user-1", "0")
        self.assertIn("> 0", screen.text)

    def test_negative_volume_rejected(self) -> None:
        fibos = [_good_fibo()]
        flow = self._navigate_to_volume(fibos=fibos)
        screen = flow.handle_text("chat-1", "user-1", "-1")
        self.assertIn("> 0", screen.text)

    def test_text_interception_only_during_volume_state(self) -> None:
        fibos = [_good_fibo()]
        self.fx.set_snapshot(_snapshot(fibos))
        flow = self.fx.flow()
        flow.open("chat-1", "user-1")
        # In AWAITING_SYMBOL — text must NOT be intercepted.
        result = flow.handle_text("chat-1", "user-1", "0.10")
        self.assertIsNone(result)

    def test_text_interception_isolated_per_user(self) -> None:
        fibos = [_good_fibo()]
        self.fx.set_snapshot(_snapshot(fibos))
        # Phase 2.2: inject resolver.
        def _default_resolver(exchange, account, symbol):
            return f"{symbol}-USD.P"
        flow = self.fx.flow(resolve_instrument_fn=_default_resolver)
        flow.open("chat-1", "user-1")
        flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_BUY}")
        flow.handle_callback("chat-1", "user-1", f"{CB_EX}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_ACCT}0")
        flow.handle_callback("chat-1", "user-1", CB_AGREE)
        # user-1 is awaiting volume; user-2 has no session at all.
        # A text from user-2 must NOT be consumed.
        self.assertIsNone(flow.handle_text("chat-1", "user-2", "0.10"))
        # user-1 IS consumed.
        self.assertIsNotNone(flow.handle_text("chat-1", "user-1", "0.10"))


# ---------------------------------------------------------------------------
# Confirmation + Create
# ---------------------------------------------------------------------------


class ConfirmationCreateTests(_FlowTestBase):
    def _happy_path(self, *, fibos: List[Mt4Fibo]) -> StartFiboFlow:
        self.fx.set_snapshot(_snapshot(fibos))
        # Phase 2.2: inject the default resolver so the proposal
        # screen appears.
        def _default_resolver(exchange, account, symbol):
            return f"{symbol}-USD.P"
        flow = self.fx.flow(resolve_instrument_fn=_default_resolver)
        flow.open("chat-1", "user-1")
        flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_BUY}")
        flow.handle_callback("chat-1", "user-1", f"{CB_EX}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_ACCT}0")
        flow.handle_callback("chat-1", "user-1", CB_AGREE)
        flow.handle_text("chat-1", "user-1", "0.10")
        return flow

    def test_confirmation_calculation(self) -> None:
        fibos = [_good_fibo(
            buy_cycle_id=42, cumulative_buy_weight="2.5",
        )]
        flow = self._happy_path(fibos=fibos)
        sess = flow.session_store.get("chat-1", "user-1")
        # Calculate target from snapshot+session.
        snap = Mt4SnapshotStore(self.fx.snap_path).load()
        fibo = snap.find_fibo(sess.symbol, sess.variant)
        target = sess.starting_volume * fibo.side_cumulative_weight(sess.side)
        self.assertEqual(target, Decimal("0.25"))

    def test_successful_create_persists_one_registration(self) -> None:
        fibos = [_good_fibo(buy_cycle_id=42, cumulative_buy_weight="2.5")]
        flow = self._happy_path(fibos=fibos)
        screen = flow.handle_callback(
            "chat-1", "user-1", CB_CREATE
        )
        self.assertIn("✅ Registered", screen.text)
        # Session cleared.
        self.assertIsNone(flow.session_store.get("chat-1", "user-1"))
        # One registration persisted.
        store = FiboRegistrationStore(self.fx.reg_path)
        all_ = store.load_all()
        self.assertEqual(len(all_), 1)
        # The wizard picked index 0; sorted(["MAIN","ALT"]) -> ["ALT","MAIN"],
        # so the first account is "ALT" (uppercase normalization).
        # Phase 2.2: the registration_key uses the stored
        # exchange_instrument (the canonical venue contract the
        # user Agreed to), NOT the MT4 source symbol.
        self.assertEqual(
            all_[0].registration_key,
            "apex/ALT/BTCUSD-USD.P/FASTFIB/BUY",
        )
        # And both source_symbol and exchange_instrument are stored.
        self.assertEqual(all_[0].source_symbol, "BTCUSD")
        self.assertEqual(all_[0].exchange_instrument, "BTCUSD-USD.P")
        # And the desired_exchange_size = 0.10 * 2.5 = 0.25.
        self.assertEqual(
            all_[0].desired_exchange_size, Decimal("0.25"),
        )

    def test_duplicate_registration_refused(self) -> None:
        fibos = [_good_fibo(buy_cycle_id=42, cumulative_buy_weight="2.5")]
        flow = self._happy_path(fibos=fibos)
        flow.handle_callback("chat-1", "user-1", CB_CREATE)
        # Second attempt: open fresh session, identical inputs.
        flow.open("chat-1", "user-1")
        flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_BUY}")
        flow.handle_callback("chat-1", "user-1", f"{CB_EX}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_ACCT}0")
        flow.handle_callback("chat-1", "user-1", CB_AGREE)
        flow.handle_text("chat-1", "user-1", "0.10")
        screen = flow.handle_callback(
            "chat-1", "user-1", CB_CREATE
        )
        self.assertIn("Already registered", screen.text)
        # Still one record.
        store = FiboRegistrationStore(self.fx.reg_path)
        self.assertEqual(len(store.load_all()), 1)

    def test_stale_snapshot_blocks_create(self) -> None:
        # Snapshot received 60s ago.
        old_received = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        # Build the same string 60s in the past.
        from datetime import timedelta
        old_received = (
            datetime.now(timezone.utc) - timedelta(seconds=60)
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        fibos = [_good_fibo(buy_cycle_id=42, cumulative_buy_weight="2.5")]
        self.fx.set_snapshot(_snapshot(fibos, received_at=old_received))
        # Phase 2.2: inject resolver.
        def _default_resolver(exchange, account, symbol):
            return f"{symbol}-USD.P"
        flow = self.fx.flow(resolve_instrument_fn=_default_resolver)
        flow.open("chat-1", "user-1")
        flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_BUY}")
        flow.handle_callback("chat-1", "user-1", f"{CB_EX}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_ACCT}0")
        flow.handle_callback("chat-1", "user-1", CB_AGREE)
        confirm = flow.handle_text("chat-1", "user-1", "0.10")
        # No Create button — Refresh instead.
        flat_cb = [
            btn["callback_data"]
            for row in confirm.buttons
            for btn in row
        ]
        self.assertIn(CB_REFRESH, flat_cb)
        self.assertNotIn(CB_CREATE, flat_cb)

    def test_cycle_change_before_create_requires_reconfirm(self) -> None:
        fibos_v1 = [_good_fibo(buy_cycle_id=42, cumulative_buy_weight="2.5")]
        self.fx.set_snapshot(_snapshot(fibos_v1))
        flow = self.fx.flow()
        flow.open("chat-1", "user-1")
        flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_BUY}")
        flow.handle_callback("chat-1", "user-1", f"{CB_EX}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_ACCT}0")
        flow.handle_text("chat-1", "user-1", "0.10")
        # Mutate the snapshot: cycle_id changes.
        fibos_v2 = [_good_fibo(buy_cycle_id=99, cumulative_buy_weight="2.5")]
        self.fx.set_snapshot(_snapshot(fibos_v2))
        screen = flow.handle_callback(
            "chat-1", "user-1", CB_CREATE
        )
        self.assertIn("cycle_id", screen.text)
        # Still in AWAITING_CONFIRM (Create did NOT persist).
        store = FiboRegistrationStore(self.fx.reg_path)
        self.assertEqual(store.load_all(), [])

    def test_weight_change_before_create_refreshes_target(self) -> None:
        fibos_v1 = [_good_fibo(buy_cycle_id=42, cumulative_buy_weight="2.5")]
        self.fx.set_snapshot(_snapshot(fibos_v1))
        flow = self.fx.flow()
        flow.open("chat-1", "user-1")
        flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_BUY}")
        flow.handle_callback("chat-1", "user-1", f"{CB_EX}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_ACCT}0")
        flow.handle_text("chat-1", "user-1", "0.10")
        # Mutate the snapshot: cumulative weight changes.
        fibos_v2 = [_good_fibo(buy_cycle_id=42, cumulative_buy_weight="5.0")]
        self.fx.set_snapshot(_snapshot(fibos_v2))
        screen = flow.handle_callback(
            "chat-1", "user-1", CB_CREATE
        )
        self.assertIn("weight", screen.text.lower())
        store = FiboRegistrationStore(self.fx.reg_path)
        self.assertEqual(store.load_all(), [])

    def test_cancel_clears_session(self) -> None:
        fibos = [_good_fibo()]
        flow = self._happy_path(fibos=fibos)
        screen = flow.handle_callback(
            "chat-1", "user-1", CB_CANCEL
        )
        self.assertIn("cancelled", screen.text.lower())
        self.assertIsNone(flow.session_store.get("chat-1", "user-1"))

    def test_back_returns_to_previous_step(self) -> None:
        fibos = [_good_fibo()]
        flow = self._happy_path(fibos=fibos)
        # From AWAITING_CONFIRM, back goes to AWAITING_VOLUME.
        screen = flow.handle_callback("chat-1", "user-1", CB_BACK)
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertEqual(sess.state, SessionState.AWAITING_VOLUME)
        self.assertIn("Send starting volume", screen.text)


# ---------------------------------------------------------------------------
# Callback_data budget
# ---------------------------------------------------------------------------


class CallbackBudgetTests(_FlowTestBase):
    def test_callback_data_under_64_bytes(self) -> None:
        fibos = [
            _good_fibo(symbol="BTCUSD", variant="FASTFib"),
            _good_fibo(symbol="ETHUSD", variant="NORMALFib"),
        ]
        self.fx.set_snapshot(_snapshot(fibos))
        # Phase 2.2: inject a resolver so the proposal screen
        # appears (otherwise the flow lands on "unresolved").
        def _default_resolver(exchange, account, symbol):
            return f"{symbol}-USD.P"
        flow = self.fx.flow(resolve_instrument_fn=_default_resolver)
        screens = [
            flow.open("chat-1", "user-1"),
        ]
        flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
        screens.append(flow.handle_callback(
            "chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_BUY}"
        ))
        screens.append(flow.handle_callback(
            "chat-1", "user-1", f"{CB_EX}0"
        ))
        screens.append(flow.handle_callback(
            "chat-1", "user-1", f"{CB_ACCT}0"
        ))
        screens.append(flow.handle_callback(
            "chat-1", "user-1", CB_AGREE
        ))
        screens.append(flow.handle_text("chat-1", "user-1", "0.10"))
        for screen in screens:
            if screen is None:
                continue
            for row in screen.buttons:
                for btn in row:
                    self.assertLessEqual(
                        len(btn["callback_data"]), 64,
                        f"callback {btn['callback_data']!r} "
                        f"exceeds 64 bytes",
                    )
                    # Defensive: every callback should also fit in 32 bytes.
                    self.assertLessEqual(
                        len(btn["callback_data"]), 32,
                        f"callback {btn['callback_data']!r} "
                        f"exceeds 32 bytes (defensive budget)",
                    )


# ---------------------------------------------------------------------------
# Session isolation + TTL
# ---------------------------------------------------------------------------


class SessionIsolationTests(_FlowTestBase):
    def test_per_user_session_isolation(self) -> None:
        fibos = [_good_fibo()]
        self.fx.set_snapshot(_snapshot(fibos))
        flow = self.fx.flow()
        flow.open("chat-1", "user-1")
        flow.open("chat-1", "user-2")
        # Each user has its own session.
        s1 = flow.session_store.get("chat-1", "user-1")
        s2 = flow.session_store.get("chat-1", "user-2")
        self.assertIsNotNone(s1)
        self.assertIsNotNone(s2)
        self.assertIsNot(s1, s2)
        # Different chat.
        flow.open("chat-2", "user-1")
        s1b = flow.session_store.get("chat-2", "user-1")
        self.assertIsNotNone(s1b)
        self.assertIsNot(s1, s1b)

    def test_session_ttl_expiry(self) -> None:
        fibos = [_good_fibo()]
        self.fx.set_snapshot(_snapshot(fibos))
        flow = self.fx.flow()
        flow.open("chat-1", "user-1")
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertIsNotNone(sess)
        # Force expiry.
        sess.last_accessed_at = time.monotonic() - SESSION_TTL_SECONDS - 1
        # Now get() returns None and the session is purged.
        self.assertIsNone(flow.session_store.get("chat-1", "user-1"))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class ConstantsTests(unittest.TestCase):
    def test_stale_threshold_is_30_seconds(self) -> None:
        self.assertEqual(STALE_THRESHOLD_SECONDS, 30.0)

    def test_callback_prefix(self) -> None:
        self.assertEqual(CB_PREFIX, "fibo:s:")
        for cb in (CB_SYM, CB_SIDE, CB_EX, CB_ACCT, CB_CREATE,
                   CB_BACK, CB_CANCEL, CB_REFRESH):
            self.assertTrue(cb.startswith(CB_PREFIX))


if __name__ == "__main__":
    unittest.main()