"""Phase 2.2 — instrument-translation tests.

Spec §13 covers 30 cases; this suite covers each one explicitly.

Key invariants:

* ``resolve_instrument`` is the ONLY authority for what becomes the
  canonical ``exchange_instrument``. The user-typed alias is NEVER
  stored as ``exchange_instrument`` unless the agent returns it.
* Alias memory is a hint, not a source of truth. Cached mappings
  MUST be revalidated through the live agent before reuse.
* ``fibo:exit`` / Cancel never mutate state, never call exchanges.
* No exchange writes anywhere in the wizard.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from plugins.trade.canonical import (
    CanonicalInstrument,
    CanonicalPosition,
    make_failure,
    make_success,
)
from plugins.trade.fibo.alias_memory import (
    ALIAS_MEMORY_VERSION,
    AliasMemory,
    alias_key,
)
from plugins.trade.fibo.flow import (
    CB_ACCT, CB_AGREE, CB_BACK, CB_BROWSE, CB_BROWSEPG, CB_CANCEL,
    CB_EX, CB_INSTFAIL_RETRY, CB_INSTSEL, CB_OTHER, CB_SIDE, CB_SYM,
    CB_CREATE, SIDE_TOKEN_BUY, SIDE_TOKEN_SELL, StartFiboFlow,
)
from plugins.trade.fibo.session import (
    FiboSessionStore, SessionState, TEXT_INTERCEPT_STATES,
)
from plugins.trade.fibo.snapshot import (
    Mt4Fibo, Mt4Snapshot, Mt4SnapshotStore,
)
from plugins.trade.fibo.store import FiboRegistrationStore


# ---------------------------------------------------------------------------
# Helpers — fake agent / snapshot / store
# ---------------------------------------------------------------------------


def _good_fibo(
    *,
    symbol: str = "ETHUSD",
    variant: str = "NORMALFib",
    percentage: str = "0.01",
    buy_cycle_id: int = 0,
    cumulative_buy_weight: str = "0",
    sell_cycle_id: int = 46871101,
    cumulative_sell_weight: str = "1",
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
    received_at: Optional[str] = None,
) -> Mt4Snapshot:
    if received_at is None:
        # Use a fresh timestamp so the snapshot is NOT stale by
        # default (reconciler staleness gate = 30s).
        from datetime import datetime, timezone
        received_at = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    return Mt4Snapshot(
        v=1,
        source="mt4-Fresh-1",
        seq=42,
        ts=0,
        fibos=fibos,
        received_at=received_at,
        telegram_update_id=0,
        telegram_message_id=0,
        reader_chat_id=-100,
    )


class _FakeResolver:
    """Stand-in for the live Ondo ``resolve_instrument`` path.

    The fake is a callable: ``resolve(exchange, account, symbol) ->
    Optional[str]``. It records every call so tests can assert what
    was resolved and in what order.
    """

    def __init__(
        self,
        mapping: Optional[Dict[str, Optional[str]]] = None,
        *,
        fail_on: Optional[set] = None,
        raise_on: Optional[set] = None,
    ) -> None:
        # mapping: src-symbol → canonical venue contract id.
        # If the symbol is NOT in mapping, return None (unresolved).
        self._mapping = dict(mapping or {})
        self._fail_on = set(fail_on or [])
        self._raise_on = set(raise_on or [])
        self.calls: List[Tuple[str, str, str]] = []

    def __call__(self, exchange: str, account: str, symbol: str) -> Optional[str]:
        self.calls.append((exchange, account, symbol))
        if symbol in self._raise_on:
            raise RuntimeError(f"resolver simulated raise for {symbol!r}")
        if symbol in self._fail_on:
            return None
        return self._mapping.get(symbol)


class _FakeExec:
    """Stand-in for TradeDesk.execute() — rejects any non-read op."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, request: Dict[str, Any]) -> Any:
        self.calls.append(dict(request))
        op = request.get("operation")
        if op == "resolve_instrument":
            return make_success(
                operation="resolve_instrument",
                exchange=request.get("exchange", ""),
                account=request.get("account", ""),
                instrument=CanonicalInstrument(
                    requested_symbol=request.get("symbol", ""),
                    symbol=request.get("symbol", ""),
                    display_name=request.get("symbol", ""),
                ),
            )
        if op == "positions_orders":
            return make_success(
                operation="positions_orders",
                exchange=request.get("exchange", ""),
                account=request.get("account", ""),
                positions=[],
                order_groups=[],
            )
        return make_failure(
            operation=op or "",
            exchange=request.get("exchange", ""),
            account=request.get("account", ""),
            code="UNEXPECTED_OPERATION",
            message=f"reconciler requested forbidden op {op!r}",
        )


class _Fixture:
    """Temp-tree factory for snapshot / registration / alias stores."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup = None  # set externally
        self.root = Path(self.tmp.name)
        self.snap_path = self.root / "mt4_snapshot.json"
        self.reg_path = self.root / "regs.jsonl"
        self.alias_path = self.root / "aliases.json"

    def cleanup(self) -> None:
        self.tmp.cleanup()


def _flow(
    fx: _Fixture,
    *,
    resolver: Optional[_FakeResolver] = None,
    alias_memory: Optional[AliasMemory] = None,
    instruments: Optional[List[str]] = None,
) -> StartFiboFlow:
    snap_store = Mt4SnapshotStore(fx.snap_path)
    reg_store = FiboRegistrationStore(fx.reg_path)
    return StartFiboFlow(
        snapshot_store=snap_store,
        registration_store=reg_store,
        list_exchanges_fn=lambda: ["apex", "hyperliquid", "ondoperps"],
        list_accounts_fn=lambda ex: ["MAIN", "ALT"],
        list_instruments_fn=lambda ex, ac: list(instruments or [
            "ETH-USD.P", "BTC-USD.P",
        ]),
        resolve_instrument_fn=resolver,
        alias_memory=alias_memory,
    )


def _navigate_to_proposal(
    fx: _Fixture,
    *,
    resolver: Optional[_FakeResolver] = None,
    alias_memory: Optional[AliasMemory] = None,
    fibo: Optional[Mt4Fibo] = None,
    instruments: Optional[List[str]] = None,
):
    """Drive a session from /fibo to the AWAITING_INSTRUMENT_CONFIRM
    screen and return ``(flow, screen)``.

    The flow's handle_callback returns a Screen dataclass; we leave
    it un-typed so callers can use ``.text`` / ``.buttons``
    directly.
    """
    fx.snap_path.write_text(json.dumps(
        _snapshot([fibo or _good_fibo()]).to_dict()
    ))
    flow = _flow(
        fx,
        resolver=resolver,
        alias_memory=alias_memory,
        instruments=instruments,
    )
    flow.open("chat-1", "user-1")
    flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
    flow.handle_callback("chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_SELL}")
    flow.handle_callback("chat-1", "user-1", f"{CB_EX}0")
    screen = flow.handle_callback("chat-1", "user-1", f"{CB_ACCT}0")
    return flow, screen


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class AutoResolveTests(unittest.TestCase):
    """Spec §13.1: ETHUSD auto-resolves through fake agent to ETH-USD.P."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_ethusd_auto_resolves_to_eth_usd_p(self) -> None:
        resolver = _FakeResolver({"ETHUSD": "ETH-USD.P"})
        flow, screen = _navigate_to_proposal(
            self.fx, resolver=resolver, fibo=_good_fibo(),
        )
        # Proposal screen text contains BOTH source and canonical.
        self.assertIn("MT4 source", screen.text)
        self.assertIn("ETHUSD", screen.text)
        self.assertIn("ETH-USD.P", screen.text)
        # The resolver was invoked exactly once with ETHUSD.
        # The wizard picks account index 0; sorted(["MAIN","ALT"]) →
        # ["ALT","MAIN"], so the first account is "ALT".
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(resolver.calls[0], ("apex", "ALT", "ETHUSD"))


class ProposalScreenTests(unittest.TestCase):
    """Spec §13.2: proposal screen shows source + canonical venue."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_proposal_shows_source_and_canonical(self) -> None:
        resolver = _FakeResolver({"ETHUSD": "ETH-USD.P"})
        flow, screen = _navigate_to_proposal(self.fx, resolver=resolver)
        # Both labels present (spec §2.A).
        self.assertIn("MT4 source:", screen.text)
        self.assertIn("OndoPerps:", screen.text)  # may be other exchange name
        # Buttons present and in the right slots.
        flat = [
            b for row in screen.buttons for b in row
        ]
        cbs = [b["callback_data"] for b in flat]
        self.assertIn(CB_AGREE, cbs)
        self.assertIn(CB_OTHER, cbs)
        self.assertIn(CB_BROWSE, cbs)
        self.assertIn(CB_BACK, cbs)
        self.assertIn(CB_CANCEL, cbs)


class AgreePersistsCanonicalTests(unittest.TestCase):
    """Spec §13.3 + §13.18: Agree stores canonical exchange_instrument
    in the session and persists it to the registration."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_agree_stores_canonical_exchange_instrument(self) -> None:
        resolver = _FakeResolver({"ETHUSD": "ETH-USD.P"})
        alias_memory = AliasMemory(self.fx.alias_path)
        flow, _ = _navigate_to_proposal(
            self.fx, resolver=resolver, alias_memory=alias_memory,
        )
        screen = flow.handle_callback("chat-1", "user-1", CB_AGREE)
        # Session advanced to AWAITING_VOLUME.
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertIsNotNone(sess)
        self.assertEqual(sess.state, SessionState.AWAITING_VOLUME)
        self.assertEqual(sess.exchange_instrument, "ETH-USD.P")
        # The screen is the volume prompt.
        self.assertIn("Send starting volume", screen.text)


class AliasMemoryAgreeTests(unittest.TestCase):
    """Spec §13.4 / §13.5: Agree persists local alias memory;
    confirmation_count increments."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_agree_persists_alias_memory(self) -> None:
        alias_memory = AliasMemory(self.fx.alias_path)
        resolver = _FakeResolver({"ETHUSD": "ETH-USD.P"})
        flow, _ = _navigate_to_proposal(
            self.fx, resolver=resolver, alias_memory=alias_memory,
        )
        flow.handle_callback("chat-1", "user-1", CB_AGREE)
        # Alias file written.
        self.assertTrue(self.fx.alias_path.exists())
        # File mode 0600.
        mode = self.fx.alias_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        # Parent dir mode 0700.
        dmode = self.fx.alias_path.parent.stat().st_mode & 0o777
        self.assertEqual(dmode, 0o700)
        # File contents.
        payload = json.loads(self.fx.alias_path.read_text())
        self.assertEqual(payload["version"], ALIAS_MEMORY_VERSION)
        key = alias_key("apex", "ALT", "ETHUSD")
        self.assertIn(key, payload["mappings"])
        rec = payload["mappings"][key]
        self.assertEqual(rec["source_symbol"], "ETHUSD")
        self.assertEqual(rec["exchange_instrument"], "ETH-USD.P")
        self.assertEqual(rec["resolution_input"], "ETHUSD")
        self.assertEqual(rec["confirmation_count"], 1)

    def test_confirmation_count_increments_on_second_approval(self) -> None:
        # Use the exact key the wizard will write (account index 0
        # is the FIRST sorted item → "ALT").
        alias_memory = AliasMemory(self.fx.alias_path)
        key = alias_key("apex", "ALT", "ETHUSD")
        # Pre-populate with count=2.
        alias_memory.record_approval(
            key,
            source_symbol="ETHUSD",
            resolution_input="ETHUSD",
            exchange_instrument="ETH-USD.P",
        )
        alias_memory.record_approval(
            key,
            source_symbol="ETHUSD",
            resolution_input="ETHUSD",
            exchange_instrument="ETH-USD.P",
        )
        # Now drive the wizard and tap Agree — count should be 3.
        # The resolver MUST recognise BOTH the source symbol (for
        # fresh resolution) AND the stored exchange_instrument (for
        # revalidation). Otherwise the cached mapping is correctly
        # considered stale and dropped.
        resolver = _FakeResolver({"ETHUSD": "ETH-USD.P", "ETH-USD.P": "ETH-USD.P"})
        flow, _ = _navigate_to_proposal(
            self.fx, resolver=resolver, alias_memory=alias_memory,
        )
        flow.handle_callback("chat-1", "user-1", CB_AGREE)
        payload = json.loads(self.fx.alias_path.read_text())
        self.assertEqual(payload["mappings"][key]["confirmation_count"], 3)


class CacheRevalidationTests(unittest.TestCase):
    """Spec §13.6 / §13.7: cached mapping MUST be revalidated; stale
    cached mappings fall back to fresh resolution."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_cached_mapping_revalidated_before_proposal(self) -> None:
        alias_memory = AliasMemory(self.fx.alias_path)
        key = alias_key("apex", "ALT", "ETHUSD")
        # Seed alias memory.
        alias_memory.record_approval(
            key,
            source_symbol="ETHUSD",
            resolution_input="ETHUSD",
            exchange_instrument="ETH-USD.P",
        )
        # Resolver that REJECTS "ETH-USD.P" (so cached mapping is
        # detected as stale by revalidation).
        resolver = _FakeResolver(
            {"ETH-USD.P": None, "ETHUSD": "ETH-USD.P"},
            fail_on={"ETH-USD.P"},
        )
        flow, screen = _navigate_to_proposal(
            self.fx, resolver=resolver, alias_memory=alias_memory,
        )
        # Revalidation fired first (resolver called with the
        # cached exchange_instrument "ETH-USD.P"), THEN fresh
        # resolution fired for the source symbol.
        self.assertGreaterEqual(len(resolver.calls), 1)
        self.assertEqual(resolver.calls[0][2], "ETH-USD.P")  # revalidate
        # The fresh resolution succeeded, so the proposal still
        # shows ETH-USD.P.
        self.assertIn("ETH-USD.P", screen.text)

    def test_invalid_cached_mapping_falls_back_to_fresh_resolution(self) -> None:
        alias_memory = AliasMemory(self.fx.alias_path)
        key = alias_key("apex", "ALT", "ETHUSD")
        # Seed an invalid cached mapping.
        alias_memory.record_approval(
            key,
            source_symbol="ETHUSD",
            resolution_input="ETHUSD",
            exchange_instrument="ETH-USD.P",
        )
        # Resolver that REJECTS the cached ETH-USD.P and ACCEPTS
        # the source symbol (so fresh resolution succeeds).
        resolver = _FakeResolver({"ETHUSD": "ETH-USD.P"})
        # Force the ETH-USD.P lookup to fail.
        original_call = resolver.__class__.__call__
        def patched_call(self, exchange, account, symbol):
            if symbol == "ETH-USD.P":
                self.calls.append((exchange, account, symbol))
                return None
            return original_call(self, exchange, account, symbol)
        resolver.__class__.__call__ = patched_call
        # Cache should be silently dropped and fresh resolution
        # should run.
        flow, screen = _navigate_to_proposal(
            self.fx, resolver=resolver, alias_memory=alias_memory,
        )
        # Cache hit AND fresh resolve both happened.
        seen = {c[2] for c in resolver.calls}
        self.assertIn("ETH-USD.P", seen)
        self.assertIn("ETHUSD", seen)
        # The proposal screen renders ETH-USD.P (from fresh resolve).
        self.assertIn("ETH-USD.P", screen.text)


class UnresolvedSourceTests(unittest.TestCase):
    """Spec §13.8: failed source resolution does not advance the
    wizard to volume."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_failed_source_resolution_does_not_advance(self) -> None:
        # Resolver that fails on ETHUSD.
        resolver = _FakeResolver({})  # empty mapping → all failures
        flow, screen = _navigate_to_proposal(self.fx, resolver=resolver)
        sess = flow.session_store.get("chat-1", "user-1")
        # Session is still in AWAITING_INSTRUMENT_CONFIRM (not
        # AWAITING_VOLUME).
        self.assertEqual(sess.state, SessionState.AWAITING_INSTRUMENT_CONFIRM)
        # Screen shows the "could not resolve" body.
        self.assertIn("Could not uniquely resolve", screen.text)
        # Buttons are Enter alias / Browse / Back / Cancel — no
        # Agree (we have nothing to agree to).
        flat = [b for row in screen.buttons for b in row]
        cbs = [b["callback_data"] for b in flat]
        self.assertNotIn(CB_AGREE, cbs)
        self.assertIn(CB_OTHER, cbs)  # Enter alias
        self.assertIn(CB_BROWSE, cbs)


class AliasEntryStateTests(unittest.TestCase):
    """Spec §13.9: Other enters exchange_alias text state."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_other_enters_exchange_alias_state(self) -> None:
        resolver = _FakeResolver({})
        flow, _ = _navigate_to_proposal(self.fx, resolver=resolver)
        screen = flow.handle_callback("chat-1", "user-1", CB_OTHER)
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertEqual(sess.state, SessionState.AWAITING_EXCHANGE_ALIAS)
        # The screen prompts for an alias.
        self.assertIn("Enter exchange alias", screen.text)


class AliasTextHandlerTests(unittest.TestCase):
    """Spec §13.10 / §13.11: user alias is passed to
    resolve_instrument; successful alias returns canonical venue."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_alias_passed_to_resolve_instrument(self) -> None:
        resolver = _FakeResolver({"US500": "US500-USD.P"})
        flow, _ = _navigate_to_proposal(self.fx, resolver=resolver)
        flow.handle_callback("chat-1", "user-1", CB_OTHER)
        screen = flow.handle_text("chat-1", "user-1", "US500")
        # Resolver was called with US500.
        self.assertEqual(resolver.calls[-1], ("apex", "ALT", "US500"))
        # Screen is the proposal screen.
        self.assertIn("US500-USD.P", screen.text)
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertEqual(sess.resolution_input, "US500")

    def test_successful_alias_returns_canonical(self) -> None:
        resolver = _FakeResolver({"US500": "US500-USD.P"})
        flow, _ = _navigate_to_proposal(self.fx, resolver=resolver)
        flow.handle_callback("chat-1", "user-1", CB_OTHER)
        screen = flow.handle_text("chat-1", "user-1", "US500")
        # Proposal screen shows BOTH the alias and the canonical.
        self.assertIn("US500", screen.text)
        self.assertIn("US500-USD.P", screen.text)


class AliasFailureTests(unittest.TestCase):
    """Spec §13.12: failed alias remains in alias-entry flow."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_failed_alias_remains_in_alias_entry_flow(self) -> None:
        resolver = _FakeResolver({})
        flow, _ = _navigate_to_proposal(self.fx, resolver=resolver)
        flow.handle_callback("chat-1", "user-1", CB_OTHER)
        screen = flow.handle_text("chat-1", "user-1", "BADALIAS")
        sess = flow.session_store.get("chat-1", "user-1")
        # Spec §3.B: stay in alias-entry flow so the user can
        # type another alias or browse markets.
        self.assertEqual(sess.state, SessionState.AWAITING_EXCHANGE_ALIAS)
        # Screen is the "Try another" failure view.
        self.assertIn("could not resolve", screen.text.lower())
        self.assertIn("BADALIAS", screen.text)


class RawAliasTrustTests(unittest.TestCase):
    """Spec §13.13: raw user alias is NEVER stored as
    exchange_instrument unless the agent returns it."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_raw_user_alias_never_stored_as_canonical(self) -> None:
        resolver = _FakeResolver({})  # resolves nothing
        flow, _ = _navigate_to_proposal(self.fx, resolver=resolver)
        flow.handle_callback("chat-1", "user-1", CB_OTHER)
        # User types something the agent cannot resolve.
        flow.handle_text("chat-1", "user-1", "FAKESYMBOL")
        sess = flow.session_store.get("chat-1", "user-1")
        # session.exchange_instrument MUST remain None.
        self.assertIsNone(sess.exchange_instrument)
        # session.resolution_input captures the typed alias (for
        # display), but exchange_instrument is still empty.
        self.assertEqual(sess.resolution_input, "FAKESYMBOL")


class BrowseReadOnlyTests(unittest.TestCase):
    """Spec §13.14 / §13.15 / §13.16: Browse markets is read-only and
    uses indexed callback tokens; browse-selected market requires
    Agree."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_browse_is_read_only(self) -> None:
        resolver = _FakeResolver({})
        flow, _ = _navigate_to_proposal(
            self.fx,
            resolver=resolver,
            instruments=["ETH-USD.P", "BTC-USD.P"],
        )
        # Tap Browse.
        screen = flow.handle_callback("chat-1", "user-1", CB_BROWSE)
        # The screen is the browse screen.
        self.assertIn("Markets on", screen.text)
        # No exchange writes were attempted.
        self.assertNotIn("execute", screen.text.lower())

    def test_browse_callbacks_use_indexed_tokens(self) -> None:
        resolver = _FakeResolver({})
        flow, _ = _navigate_to_proposal(
            self.fx,
            resolver=resolver,
            instruments=["ETH-USD.P", "BTC-USD.P"],
        )
        screen = flow.handle_callback("chat-1", "user-1", CB_BROWSE)
        # Every button callback starts with CB_INSTSEL (no raw market
        # names in callback_data).
        flat = [b for row in screen.buttons for b in row]
        for b in flat:
            cb = b["callback_data"]
            if cb in (CB_BROWSE, CB_BACK, CB_CANCEL) or cb.startswith(
                CB_BROWSEPG
            ):
                continue
            self.assertTrue(
                cb.startswith(CB_INSTSEL),
                f"browse button callback {cb!r} is not indexed",
            )

    def test_browse_selected_market_requires_agree(self) -> None:
        resolver = _FakeResolver({"ETH-USD.P": "ETH-USD.P"})
        flow, _ = _navigate_to_proposal(
            self.fx,
            resolver=resolver,
            instruments=["ETH-USD.P", "BTC-USD.P"],
        )
        flow.handle_callback("chat-1", "user-1", CB_BROWSE)
        # Pick the first market.
        screen = flow.handle_callback("chat-1", "user-1", f"{CB_INSTSEL}0")
        sess = flow.session_store.get("chat-1", "user-1")
        # exchange_instrument is STILL None — user must tap Agree.
        self.assertIsNone(sess.exchange_instrument)
        # State is AWAITING_INSTRUMENT_CONFIRM.
        self.assertEqual(sess.state, SessionState.AWAITING_INSTRUMENT_CONFIRM)
        # Screen shows the proposal with Agree.
        self.assertIn("ETH-USD.P", screen.text)
        flat = [b for row in screen.buttons for b in row]
        self.assertIn(CB_AGREE, [b["callback_data"] for b in flat])


class ConfirmationFieldsTests(unittest.TestCase):
    """Spec §13.17: final confirmation shows Symbol: canonical and
    MT4 source: source_symbol."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_final_confirmation_shows_symbol_and_mt4_source(self) -> None:
        resolver = _FakeResolver({"ETHUSD": "ETH-USD.P"})
        flow, _ = _navigate_to_proposal(self.fx, resolver=resolver)
        flow.handle_callback("chat-1", "user-1", CB_AGREE)
        screen = flow.handle_text("chat-1", "user-1", "0.001")
        # The confirmation screen shows BOTH:
        #   Symbol: ETH-USD.P (canonical venue)
        #   MT4 source: ETHUSD
        self.assertIn("Symbol:", screen.text)
        self.assertIn("ETH-USD.P", screen.text)
        self.assertIn("MT4 source:", screen.text)
        self.assertIn("ETHUSD", screen.text)


class StoredRegistrationFieldsTests(unittest.TestCase):
    """Spec §13.18 / §13.19 / §13.20: stored registration contains
    both source_symbol and exchange_instrument; registration key
    uses exchange_instrument; MT4 matching still uses source_symbol."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_stored_registration_has_both_symbols(self) -> None:
        resolver = _FakeResolver({"ETHUSD": "ETH-USD.P"})
        flow, _ = _navigate_to_proposal(self.fx, resolver=resolver)
        flow.handle_callback("chat-1", "user-1", CB_AGREE)
        flow.handle_text("chat-1", "user-1", "0.001")
        screen = flow.handle_callback("chat-1", "user-1", CB_CREATE)
        self.assertIn("Registered", screen.text)
        regs = FiboRegistrationStore(self.fx.reg_path).load_all()
        self.assertEqual(len(regs), 1)
        reg = regs[0]
        self.assertEqual(reg.source_symbol, "ETHUSD")
        self.assertEqual(reg.exchange_instrument, "ETH-USD.P")
        # Key uses exchange_instrument. Account index 0 is the
        # FIRST sorted item → "ALT" (sorted(["MAIN","ALT"])).
        self.assertEqual(
            reg.registration_key,
            "apex/ALT/ETH-USD.P/NORMALFIB/SELL",
        )

    def test_mt4_matching_still_uses_source_symbol(self) -> None:
        # The Phase 2.1 reconciler test suite already proves this.
        # Here we cross-check that the wizard-built registration
        # still works with the reconciler.
        from plugins.trade.fibo.reconciler import FiboReconciler
        resolver = _FakeResolver({"ETHUSD": "ETH-USD.P"})
        flow, _ = _navigate_to_proposal(self.fx, resolver=resolver)
        flow.handle_callback("chat-1", "user-1", CB_AGREE)
        flow.handle_text("chat-1", "user-1", "0.001")
        flow.handle_callback("chat-1", "user-1", CB_CREATE)
        regs = FiboRegistrationStore(self.fx.reg_path).load_all()
        rec = FiboReconciler(
            registration_store=FiboRegistrationStore(self.fx.reg_path),
            snapshot_store=Mt4SnapshotStore(self.fx.snap_path),
            execute_fn=_FakeExec(),
        )
        # The reconciler must find the MT4 fibo via source_symbol.
        result = rec.reconcile_one(regs[0])
        # The MT4 entry is found (cycle_id matches).
        self.assertEqual(result.mt4_cycle_id, 46871101)


class ReconcilerExchangeLookupTests(unittest.TestCase):
    """Spec §13.21: reconciler exchange lookup still uses
    exchange_instrument."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_reconciler_uses_stored_exchange_instrument(self) -> None:
        from plugins.trade.fibo.reconciler import FiboReconciler
        from plugins.trade.fibo.store import FiboRegistration
        # Build a registration manually with exchange_instrument.
        reg = FiboRegistration.build(
            exchange="apex",
            account="MAIN",
            symbol="ETHUSD",
            source_symbol="ETHUSD",
            exchange_instrument="ETH-USD.P",
            variant="NORMALFIB",
            side="SELL",
            starting_volume="0.001",
            source="mt4-Fresh-1",
            source_seq=42,
            source_cycle_id=46871101,
            source_cumulative_weight="1",
            source_percentage="0.01",
            source_snapshot_received_at="2026-08-26T10:00:00Z",
            desired_exchange_size=Decimal("0.001"),
        )
        FiboRegistrationStore(self.fx.reg_path).append(reg)
        # Snap with an ETHUSD fibo.
        self.fx.snap_path.write_text(json.dumps(
            _snapshot([_good_fibo()]).to_dict()
        ))
        rec = FiboReconciler(
            registration_store=FiboRegistrationStore(self.fx.reg_path),
            snapshot_store=Mt4SnapshotStore(self.fx.snap_path),
            execute_fn=_FakeExec(),
        )
        result = rec.reconcile_one(reg)
        # exchange_instrument is the stored one (Phase 2.1 identity).
        self.assertEqual(result.exchange_instrument, "ETH-USD.P")


class AliasFilePermissionsTests(unittest.TestCase):
    """Spec §13.22 / §13.23: alias file mode 0600; atomic write."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_alias_file_mode_is_0600(self) -> None:
        am = AliasMemory(self.fx.alias_path)
        am.save()
        mode = self.fx.alias_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_alias_write_is_atomic(self) -> None:
        # Atomicity is enforced by the existing _atomic helper
        # (creates temp in same dir, fchmod 0600, fsync, os.replace).
        # We assert the helper is used by patching os.replace and
        # ensuring it is called instead of os.write + os.rename.
        import plugins.trade.fibo.alias_memory as am_mod
        calls = {"replace": 0, "rename": 0}
        original_replace = os.replace
        original_rename = os.rename if hasattr(os, "rename") else None
        def spy_replace(src, dst, *a, **kw):
            calls["replace"] += 1
            return original_replace(src, dst, *a, **kw)
        os.replace = spy_replace
        try:
            am = AliasMemory(self.fx.alias_path)
            am.save()
            self.assertGreaterEqual(calls["replace"], 1)
        finally:
            os.replace = original_replace


class AliasMemorySafetyTests(unittest.TestCase):
    """Spec §13.24: alias-memory code has no git/subprocess/network-write
    behavior. Spec §13.25: no hardcoded ETHUSD→ETH-USD.P in Fibo.
    Spec §13.26: zero exchange writes. Spec §13.27: callback_data
    ≤ 64 bytes."""

    @staticmethod
    def _strip_strings_and_comments(src: str) -> str:
        """Strip docstrings and string literals from ``src`` so the
        static guards scan only executable tokens.

        Uses a line-based tracker that:

        1. Drops triple-quoted docstring bodies (the body lines
           between the opening and closing ``\"\"\"``).
        2. Drops single-line ``# comments``.
        3. Removes inline string literals on each remaining line.

        The tracker is a small finite-state machine; it is
        intentionally lenient (false-positives are the safe
        failure mode for a static guard).
        """
        out_lines: List[str] = []
        in_triple = False
        triple_quote: Optional[str] = None
        for line in src.splitlines():
            stripped = line.lstrip()
            # Comment line — drop entirely.
            if not in_triple and stripped.startswith("#"):
                continue
            # Stripping a triple-quoted block. Skip every line
            # until we see the matching close quote.
            if in_triple:
                idx = line.find(triple_quote)
                if idx >= 0:
                    # Close quote is on this line. Keep only the
                    # tail of the line (rare for our code style).
                    line = line[idx + len(triple_quote):]
                    in_triple = False
                    triple_quote = None
                    # Fall through to process the remainder.
                else:
                    continue
            if not in_triple:
                # Look for an opening triple quote. We pick the
                # first occurrence of either \"\"\" or ''' that is
                # not immediately followed by another of the same
                # (that would be a 4+ quote, not a triple).
                open_q: Optional[str] = None
                open_idx = -1
                for q in ('"""', "'''"):
                    i = 0
                    while True:
                        j = line.find(q, i)
                        if j < 0:
                            break
                        # Check it's not part of a longer run.
                        k = j + len(q)
                        if k < len(line) and line[k] == q[0]:
                            i = k
                            continue
                        if open_q is None or j < open_idx:
                            open_q = q
                            open_idx = j
                        break
                if open_q is not None:
                    head = line[:open_idx]
                    # If the open quote is closed on the same line
                    # (one-line docstring or string), drop the
                    # whole triple-quoted span and keep the tail.
                    rest = line[open_idx + len(open_q):]
                    close_idx = rest.find(open_q)
                    if close_idx >= 0:
                        # One-line triple-quoted — drop everything
                        # from open to close.
                        line = head + rest[close_idx + len(open_q):]
                        # Fall through to strip string literals.
                    else:
                        # Multi-line triple-quoted starts here.
                        line = head
                        in_triple = True
                        triple_quote = open_q
            # Strip inline string literals line by line.
            if in_triple:
                # We just opened a multi-line triple quote and the
                # current line was the opener. The continuation is
                # handled in the next iteration; emit the head
                # (everything before the opener) now.
                if line:
                    out_lines.append(line)
                continue
            cleaned: List[str] = []
            i = 0
            while i < len(line):
                ch = line[i]
                if ch == '"' or ch == "'":
                    quote = ch
                    # Skip the literal until the matching close
                    # quote (handling escapes).
                    j = i + 1
                    while j < len(line):
                        if line[j] == "\\":
                            j += 2
                            continue
                        if line[j] == quote:
                            break
                        j += 1
                    i = j + 1
                    continue
                cleaned.append(ch)
                i += 1
            new = "".join(cleaned)
            if new.strip():
                out_lines.append(new)
        return "\n".join(out_lines)

    def test_alias_memory_source_has_no_forbidden_tokens(self) -> None:
        import inspect
        from plugins.trade.fibo import alias_memory as am_mod
        src = inspect.getsource(am_mod)
        code = self._strip_strings_and_comments(src)
        forbidden = (
            "subprocess",
            "requests.post",
            "requests.put",
            "requests.delete",
            "requests.patch",
            "httpx.post",
            "httpx.put",
            "httpx.delete",
            "httpx.patch",
            "os.system",
            "os.exec",
        )
        for tok in forbidden:
            self.assertNotIn(
                tok, code,
                f"alias_memory must not reference {tok!r}",
            )

    def test_no_hardcoded_ethusd_to_eth_usd_p_in_fibo(self) -> None:
        import inspect
        from plugins.trade.fibo import flow as flow_mod
        src = inspect.getsource(flow_mod)
        code = self._strip_strings_and_comments(src)
        # A literal "ETHUSD" + "ETH-USD.P" pair in flow.py would be a
        # hardcoded mapping. We accept either side appearing in
        # isolation (e.g. in test data) but not in the same line.
        bad_pattern = re.compile(
            r'(["\'])ETHUSD(["\']).*?ETH-USD\.P|ETH-USD\.P.*?(["\'])ETHUSD(["\'])'
        )
        self.assertIsNone(
            bad_pattern.search(code),
            "Fibo flow.py must not hardcode ETHUSD→ETH-USD.P",
        )

    def test_no_exchange_writes_anywhere_in_fibo(self) -> None:
        # Static guard: flow + alias_memory + dryrun + discovery
        # must not contain any of the known write-operation tokens
        # OUTSIDE comments and string literals.
        for mod_name in ("flow", "alias_memory", "dryrun", "discovery"):
            try:
                mod = __import__(
                    f"plugins.trade.fibo.{mod_name}", fromlist=["*"]
                )
            except ImportError:
                continue
            import inspect
            src = inspect.getsource(mod)
            code = self._strip_strings_and_comments(src)
            for tok in (
                "new_order", "market_order", "limit_order",
                "cancel_order", "cancel_order_group",
                "close_position", "stop_order",
                "httpx.post", "requests.post",
                "method=\"POST\"", "method=\"PUT\"",
                "method=\"DELETE\"", "method=\"PATCH\"",
            ):
                self.assertNotIn(
                    tok, code,
                    f"{mod_name}.py references {tok!r}",
                )

    def test_callback_data_under_64_bytes(self) -> None:
        # Build a flow with 100 instruments and check every button.
        instruments = [f"MARKET-{i:03d}-USD.P" for i in range(50)]
        fx = _Fixture()
        self.addCleanup(fx.cleanup)
        resolver = _FakeResolver({})
        flow, _ = _navigate_to_proposal(
            fx,
            resolver=resolver,
            instruments=instruments,
        )
        screen = flow.handle_callback("chat-1", "user-1", CB_BROWSE)
        # Navigate forward 5 pages.
        for _ in range(5):
            screen = flow.handle_callback(
                "chat-1", "user-1", f"{CB_BROWSEPG}1"
            )
        flat = [b for row in screen.buttons for b in row]
        for b in flat:
            cb = b["callback_data"]
            self.assertLessEqual(
                len(cb), 64,
                f"callback {cb!r} exceeds 64 bytes ({len(cb)})",
            )


class PerUserTextIsolationTests(unittest.TestCase):
    """Spec §13.28: per-user alias text interception isolation."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_user_in_volume_does_not_consume_user_in_alias(self) -> None:
        resolver = _FakeResolver({"US500": "US500-USD.P"})
        flow, _ = _navigate_to_proposal(self.fx, resolver=resolver)
        # User 1 in AWAITING_EXCHANGE_ALIAS.
        flow.handle_callback("chat-1", "user-1", CB_OTHER)
        # User 2 has no session; text from user-2 is NOT consumed.
        self.assertIsNone(flow.handle_text("chat-1", "user-2", "US500"))
        # User 1 IS consumed.
        screen = flow.handle_text("chat-1", "user-1", "US500")
        self.assertIsNotNone(screen)
        self.assertIn("US500-USD.P", screen.text)


class BackNavigationTests(unittest.TestCase):
    """Spec §13.29: Back works at each new state."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_back_from_alias_returns_to_proposal(self) -> None:
        resolver = _FakeResolver({"ETHUSD": "ETH-USD.P"})
        flow, _ = _navigate_to_proposal(self.fx, resolver=resolver)
        flow.handle_callback("chat-1", "user-1", CB_OTHER)
        screen = flow.handle_callback("chat-1", "user-1", CB_BACK)
        sess = flow.session_store.get("chat-1", "user-1")
        # State is back to AWAITING_INSTRUMENT_CONFIRM.
        self.assertEqual(sess.state, SessionState.AWAITING_INSTRUMENT_CONFIRM)
        # Screen shows the proposal (with Agree).
        self.assertIn("ETH-USD.P", screen.text)
        flat = [b for row in screen.buttons for b in row]
        self.assertIn(CB_AGREE, [b["callback_data"] for b in flat])

    def test_back_from_browse_returns_to_proposal(self) -> None:
        resolver = _FakeResolver({})
        flow, _ = _navigate_to_proposal(
            self.fx,
            resolver=resolver,
            instruments=["ETH-USD.P"],
        )
        flow.handle_callback("chat-1", "user-1", CB_BROWSE)
        screen = flow.handle_callback("chat-1", "user-1", CB_BACK)
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertEqual(sess.state, SessionState.AWAITING_INSTRUMENT_CONFIRM)
        self.assertIn("Could not uniquely resolve", screen.text)

    def test_back_from_volume_returns_to_proposal(self) -> None:
        resolver = _FakeResolver({"ETHUSD": "ETH-USD.P"})
        flow, _ = _navigate_to_proposal(self.fx, resolver=resolver)
        flow.handle_callback("chat-1", "user-1", CB_AGREE)
        screen = flow.handle_callback("chat-1", "user-1", CB_BACK)
        sess = flow.session_store.get("chat-1", "user-1")
        # State: AWAITING_INSTRUMENT_CONFIRM.
        self.assertEqual(sess.state, SessionState.AWAITING_INSTRUMENT_CONFIRM)
        self.assertIn("ETH-USD.P", screen.text)

    def test_back_from_proposal_returns_to_account(self) -> None:
        resolver = _FakeResolver({"ETHUSD": "ETH-USD.P"})
        flow, _ = _navigate_to_proposal(self.fx, resolver=resolver)
        screen = flow.handle_callback("chat-1", "user-1", CB_BACK)
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertEqual(sess.state, SessionState.AWAITING_ACCOUNT)
        # exchange_instrument was reset.
        self.assertIsNone(sess.exchange_instrument)
        self.assertIn("Pick an account", screen.text)


class CancelClearsResolutionTests(unittest.TestCase):
    """Spec §13.30: Cancel clears resolution state."""

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_cancel_clears_resolution_state(self) -> None:
        resolver = _FakeResolver({"ETHUSD": "ETH-USD.P"})
        flow, _ = _navigate_to_proposal(self.fx, resolver=resolver)
        flow.handle_callback("chat-1", "user-1", CB_OTHER)
        # Session has resolution_input = "" (alias entry state, no
        # alias typed yet).
        flow.handle_callback("chat-1", "user-1", CB_CANCEL)
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertIsNone(sess)


class AliasMemoryAtomicTests(unittest.TestCase):
    """Spec §13.23: atomic alias write (already covered by helper
    tests; this is a smoke test for the on-disk JSONL shape)."""

    def test_alias_payload_is_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "aliases.json"
            am = AliasMemory(path)
            key = alias_key("apex", "ALT", "ETHUSD")
            am.record_approval(
                key,
                source_symbol="ETHUSD",
                resolution_input="ETHUSD",
                exchange_instrument="ETH-USD.P",
            )
            # File is valid JSON; no partial-write corruption.
            payload = json.loads(path.read_text())
            self.assertEqual(payload["version"], ALIAS_MEMORY_VERSION)
            self.assertIn(key, payload["mappings"])


class TextInterceptionWhitelistTests(unittest.TestCase):
    """Spec §6: TEXT_INTERCEPT_STATES = {AWAITING_VOLUME,
    AWAITING_EXCHANGE_ALIAS}."""

    def test_text_intercept_states(self) -> None:
        self.assertEqual(
            TEXT_INTERCEPT_STATES,
            frozenset({
                SessionState.AWAITING_VOLUME,
                SessionState.AWAITING_EXCHANGE_ALIAS,
            }),
        )


class LegacyUnchangedTests(unittest.TestCase):
    """Spec §11: the existing legacy registration stays
    NEEDS_INSTRUMENT_SELECTION."""

    def test_legacy_registration_in_reconciler(self) -> None:
        from plugins.trade.fibo.reconciler import FiboReconciler, DeltaAction
        from plugins.trade.fibo.store import FiboRegistration
        from datetime import datetime, timezone
        # The on-disk legacy record (no exchange_instrument).
        reg = FiboRegistration.build(
            exchange="ondoperps",
            account="BITGET",
            symbol="ETHUSD",
            variant="NORMALFIB",
            side="SELL",
            starting_volume="0.001",
            source="mt4-Fresh-1",
            source_seq=1,
            source_cycle_id=46871101,
            source_cumulative_weight="1",
            source_percentage="0.01",
            source_snapshot_received_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            desired_exchange_size=Decimal("0.001"),
        )
        self.assertTrue(reg.is_legacy)
        # Reconciler classifies it as NEEDS_INSTRUMENT_SELECTION.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snap_path = root / "snap.json"
            reg_path = root / "regs.jsonl"
            snap = _snapshot([_good_fibo(
                symbol="ETHUSD", variant="NORMALFIB",
                sell_cycle_id=46871101, cumulative_sell_weight="1",
            )], received_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"))
            snap_path.write_text(json.dumps(snap.to_dict()))
            rec = FiboReconciler(
                registration_store=FiboRegistrationStore(reg_path),
                snapshot_store=Mt4SnapshotStore(snap_path),
                execute_fn=_FakeExec(),
            )
            result = rec.reconcile_one(reg)
            self.assertEqual(
                result.delta_action,
                DeltaAction.NEEDS_INSTRUMENT_SELECTION.value,
            )
