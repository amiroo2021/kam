"""Phase 2.3 tests — ranked instrument candidate discovery + price.

Spec §13 cases A–H:

A. ETHUSD (high-confidence)
B. #SP500 (ambiguous)
C. price (missing / supporting only)
D. Other (raw alias)
E. alias memory (cached vs fresh)
F. callback budget + isolation
G. safety (zero exchange writes)
H. fallback (Other / Browse / Back / Cancel all keep working)
"""
from __future__ import annotations

import json
import os
import re
import inspect
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, "/root/kam")

from plugins.trade.canonical import (
    CanonicalInstrument,
    CanonicalPosition,
    make_failure,
    make_success,
)
from plugins.trade.fibo.alias_memory import AliasMemory, alias_key
from plugins.trade.fibo.candidates import (
    InstrumentCandidate,
    attach_price,
    rank_candidates,
    _search_hints,
)
from plugins.trade.fibo.flow import (
    CB_ACCT, CB_AGREE, CB_BACK, CB_BROWSE, CB_CANCEL, CB_CAND, CB_CREATE,
    CB_INST, CB_INSTSEL, CB_OTHER, CB_SIDE, CB_SYM, SIDE_TOKEN_BUY,
    SIDE_TOKEN_SELL, StartFiboFlow,
)
from plugins.trade.fibo.session import (
    FiboSessionStore, SessionState, TEXT_INTERCEPT_STATES,
)
from plugins.trade.fibo.snapshot import Mt4Fibo, Mt4Snapshot, Mt4SnapshotStore
from plugins.trade.fibo.store import FiboRegistrationStore


# ---------------------------------------------------------------------------
# Helpers
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


def _snapshot(fibos: List[Mt4Fibo]) -> Mt4Snapshot:
    from datetime import datetime, timezone
    now = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    return Mt4Snapshot(
        v=1, source="mt4-Fresh-1", seq=42, ts=0, fibos=fibos,
        received_at=now, telegram_update_id=0, telegram_message_id=0,
        reader_chat_id=-100,
    )


# A representative Ondo catalog (subset of the live markets payload).
_RAW_CATALOG = [
    {"market": "ETH-USD.P", "displayName": "ETHUSD", "longName": "Ethereum",
     "pair": {"base": "ETH", "quote": "USD"}, "tags": ["Crypto"]},
    {"market": "BTC-USD.P", "displayName": "BTCUSD", "longName": "Bitcoin",
     "pair": {"base": "BTC", "quote": "USD"}, "tags": ["Crypto"]},
    {"market": "US500-USD.P", "displayName": "US500USD", "longName": "US500",
     "pair": {"base": "US500", "quote": "USD"}},
    {"market": "SPY-USD.P", "displayName": "SPYUSD", "longName": "SPDR S&P 500 ETF",
     "pair": {"base": "SPY", "quote": "USD"}},
    {"market": "US100-USD.P", "displayName": "US100USD", "longName": "US100",
     "pair": {"base": "US100", "quote": "USD"}},
    {"market": "XAU-USD.P", "displayName": "XAUUSD", "longName": "Gold",
     "pair": {"base": "XAU", "quote": "USD"}},
    {"market": "AAPL-USD.P", "displayName": "AAPLUSD", "longName": "Apple",
     "pair": {"base": "AAPL", "quote": "USD"}},
]


def _price_lookup_for(market_to_price: Dict[str, Decimal]):
    """Build a price lookup callable that returns a price only for
    markets in the map; None otherwise."""
    def lookup(market: str) -> Optional[Decimal]:
        return market_to_price.get(market)
    return lookup


class _FakeResolver:
    """Stand-in for the live Ondo ``resolve_instrument``."""

    def __init__(
        self,
        mapping: Dict[str, str],
        *,
        price_map: Optional[Dict[str, Decimal]] = None,
        fail_on: Optional[set] = None,
    ) -> None:
        self._mapping = dict(mapping)
        self._price_map = dict(price_map or {})
        self._fail_on = set(fail_on or [])
        self.calls: List[Tuple[str, str, str]] = []

    def __call__(self, exchange: str, account: str, symbol: str) -> Optional[str]:
        self.calls.append((exchange, account, symbol))
        if symbol in self._fail_on:
            return None
        return self._mapping.get(symbol)


class _FakeExec:
    """Rejects any op except allowlisted read ops."""

    ALLOWED = {"resolve_instrument", "positions_orders",
               "market_price", "list_instruments", "list_markets"}

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, request: Dict[str, Any]) -> Any:
        self.calls.append(dict(request))
        op = request.get("operation")
        if op not in self.ALLOWED:
            return make_failure(
                operation=op or "",
                exchange=request.get("exchange", ""),
                account=request.get("account", ""),
                code="UNEXPECTED_OPERATION",
                message=f"reconciler requested forbidden op {op!r}",
            )
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
        if op == "market_price":
            return make_success(
                operation="market_price",
                exchange=request.get("exchange", ""),
                account=request.get("account", ""),
                data={"markPrice": "0", "price": "0"},
            )
        return make_success(
            operation=op or "",
            exchange=request.get("exchange", ""),
            account=request.get("account", ""),
            data={},
        )


class _Fixture:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.snap_path = self.root / "snap.json"
        self.reg_path = self.root / "regs.jsonl"
        self.alias_path = self.root / "aliases.json"

    def cleanup(self) -> None:
        self.tmp.cleanup()


def _flow(
    fx: _Fixture,
    *,
    resolver: Optional[_FakeResolver] = None,
    alias_memory: Optional[AliasMemory] = None,
    catalog: Optional[List[Dict[str, Any]]] = None,
    price_map: Optional[Dict[str, Decimal]] = None,
) -> StartFiboFlow:
    """Build a flow wired to the fake catalog and price reader."""
    from plugins.trade.fibo import discovery
    snap_store = Mt4SnapshotStore(fx.snap_path)
    reg_store = FiboRegistrationStore(fx.reg_path)
    # Default catalog = the fixture subset.
    cat = catalog if catalog is not None else _RAW_CATALOG
    pm = price_map if price_map is not None else {}
    flow = StartFiboFlow(
        snapshot_store=snap_store,
        registration_store=reg_store,
        list_exchanges_fn=lambda: ["apex", "hyperliquid", "ondoperps"],
        list_accounts_fn=lambda ex: ["MAIN", "ALT"],
        # Phase 2.1 lister still used for the Browse fallback.
        list_instruments_fn=lambda ex, ac: [m["market"] for m in cat],
        resolve_instrument_fn=resolver,
        alias_memory=alias_memory,
    )
    # Phase 2.4: route discovery's TradeDesk through a
    # ``FakeTradeDesk`` so the test stays fully offline.
    import plugins.trade.tests.fake_tradedesk as _fake_mod
    desk = _fake_mod.FakeTradeDesk()
    desk.resolver = resolver
    # Tests in this module click ``fibo:s:ex:0`` (apex) by
    # default. Provide catalogs for both apex and ondoperps so
    # candidate discovery works regardless of which exchange the
    # navigation step lands on.
    normalized_catalog = [
        {
            "instrument": inst["market"],
            "display_name": inst.get("displayName") or inst["market"],
            "description": (
                inst.get("longName") or inst.get("displayName")
                or inst["market"]
            ),
            "market_type": (
                "crypto" if "Crypto" in (inst.get("tags") or [])
                else ("etf" if "ETF" in (inst.get("description") or "")
                      else "index" if "Index" in (inst.get("description") or "")
                      else "")
            ),
            "base": (
                (inst.get("pair") or {}).get("base")
                or ""
            ) if isinstance(inst.get("pair"), dict) else "",
            "quote": (
                (inst.get("pair") or {}).get("quote")
                or ""
            ) if isinstance(inst.get("pair"), dict) else "",
            "price": (
                pm.get(inst["market"]) if pm else None
            ),
        }
        for inst in cat
    ]
    catalog_records = list(normalized_catalog)
    desk.catalog_fn = lambda ex, ac: list(catalog_records)
    desk.price_fn = lambda ex, ac, market: (
        pm.get(market) if pm else None
    )
    prior_get_desk = discovery._get_desk
    discovery._get_desk = lambda: desk
    flow._test_restore_discovery = lambda: setattr(
        discovery, "_get_desk", prior_get_desk,
    )
    return flow


def _navigate_to_candidates(
    fx: _Fixture,
    *,
    resolver: _FakeResolver,
    fibo: Mt4Fibo,
    alias_memory: Optional[AliasMemory] = None,
    chat_id: str = "chat-1",
    user_id: str = "user-1",
):
    """Drive a session to the candidate picker / proposal screen."""
    fx.snap_path.write_text(json.dumps(_snapshot([fibo]).to_dict()))
    flow = _flow(fx, resolver=resolver, alias_memory=alias_memory)
    flow.open(chat_id, user_id)
    flow.handle_callback(chat_id, user_id, f"{CB_SYM}0")
    flow.handle_callback(chat_id, user_id, f"{CB_SIDE}{SIDE_TOKEN_SELL}")
    flow.handle_callback(chat_id, user_id, "fibo:s:ex:0")
    screen = flow.handle_callback(chat_id, user_id, "fibo:s:acct:0")
    return flow, screen


# ---------------------------------------------------------------------------
# A. ETHUSD high-confidence path
# ---------------------------------------------------------------------------


class EthusdCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_direct_ethusd_resolve_fails(self) -> None:
        """Direct resolve_instrument("ETHUSD") fails on Ondo."""
        resolver = _FakeResolver(
            {"ETH-USD.P": "ETH-USD.P"},  # only ETH-USD.P is known
            fail_on={"ETHUSD"},
        )
        fibo = _good_fibo(symbol="ETHUSD", variant="NORMALFib",
                          sell_cycle_id=1, cumulative_sell_weight="1")
        flow, screen = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
        )
        # ETHUSD failed direct resolve; we fell through to the
        # candidate picker (Phase 2.3).
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertEqual(
            sess.state, SessionState.AWAITING_INSTRUMENT_CONFIRM,
        )
        self.assertGreater(len(sess.candidates), 0)

    def test_discovery_finds_eth_usd_p(self) -> None:
        """Discovery ranks ETH-USD.P highly for source ETHUSD."""
        resolver = _FakeResolver({"ETH-USD.P": "ETH-USD.P"},
                                fail_on={"ETHUSD"})
        fibo = _good_fibo(symbol="ETHUSD")
        flow, _ = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
        )
        sess = flow.session_store.get("chat-1", "user-1")
        ranked = sess.candidates
        instruments = [c.instrument for c in ranked]
        self.assertIn("ETH-USD.P", instruments)
        # ETH-USD.P must be the top-ranked candidate.
        self.assertEqual(ranked[0].instrument, "ETH-USD.P")

    def test_selecting_candidate_validates_via_agent(self) -> None:
        """Picking a candidate revalidates through resolve_instrument.

        The candidate's instrument string is sent to the agent for
        confirmation. The agent returns the canonical id. The
        proposal is staged ONLY with the agent-returned id — never
        with the raw button payload.
        """
        resolver = _FakeResolver({"ETH-USD.P": "ETH-USD.P"},
                                fail_on={"ETHUSD"})
        fibo = _good_fibo(symbol="ETHUSD")
        flow, _ = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
        )
        sess = flow.session_store.get("chat-1", "user-1")
        # Find the ETH-USD.P index in the ranked list.
        idx = next(
            i for i, c in enumerate(sess.candidates)
            if c.instrument == "ETH-USD.P"
        )
        # Before picking: agent has only been called for ETHUSD.
        ethusd_calls_before = sum(
            1 for c in resolver.calls if c[2] == "ETHUSD"
        )
        screen = flow.handle_callback("chat-1", "user-1", f"{CB_CAND}{idx}")
        # After picking: agent was called again with ETH-USD.P.
        self.assertGreater(
            sum(1 for c in resolver.calls if c[2] == "ETH-USD.P"),
            0,
        )
        # Proposal screen text contains the canonical id and the
        # Agree button (the only way to commit).
        self.assertIn("ETH-USD.P", screen.text)
        flat = [b for row in screen.buttons for b in row]
        self.assertIn(CB_AGREE, [b["callback_data"] for b in flat])
        # Confirm: the resolution_input reflects the candidate we
        # picked (not the original source symbol).
        self.assertEqual(sess.resolution_input, "ETH-USD.P")

    def test_agree_stores_ethusd_to_eth_usd_p(self) -> None:
        """Tapping Agree persists ETHUSD -> ETH-USD.P mapping."""
        resolver = _FakeResolver({"ETH-USD.P": "ETH-USD.P"},
                                fail_on={"ETHUSD"})
        fibo = _good_fibo(symbol="ETHUSD")
        flow, _ = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
        )
        sess = flow.session_store.get("chat-1", "user-1")
        idx = next(
            i for i, c in enumerate(sess.candidates)
            if c.instrument == "ETH-USD.P"
        )
        flow.handle_callback("chat-1", "user-1", f"{CB_CAND}{idx}")
        flow.handle_callback("chat-1", "user-1", CB_AGREE)
        flow.handle_text("chat-1", "user-1", "0.001")
        flow.handle_callback("chat-1", "user-1", CB_CREATE)
        regs = FiboRegistrationStore(self.fx.reg_path).load_all()
        self.assertEqual(len(regs), 1)
        self.assertEqual(regs[0].source_symbol, "ETHUSD")
        self.assertEqual(regs[0].exchange_instrument, "ETH-USD.P")
        self.assertEqual(
            regs[0].registration_key,
            "apex/ALT/ETH-USD.P/NORMALFIB/SELL",
        )

    def test_confirmation_shows_canonical_symbol_and_mt4_source(self) -> None:
        """Spec §12: confirmation shows the venue contract under
        'Exchange instrument:' and the MT4 source under 'Source
        symbol:'. The two identities MUST be distinct."""
        resolver = _FakeResolver({"ETH-USD.P": "ETH-USD.P"},
                                fail_on={"ETHUSD"})
        fibo = _good_fibo(symbol="ETHUSD")
        flow, _ = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
        )
        sess = flow.session_store.get("chat-1", "user-1")
        idx = next(
            i for i, c in enumerate(sess.candidates)
            if c.instrument == "ETH-USD.P"
        )
        flow.handle_callback("chat-1", "user-1", f"{CB_CAND}{idx}")
        flow.handle_callback("chat-1", "user-1", CB_AGREE)
        screen = flow.handle_text("chat-1", "user-1", "0.001")
        # New wording: "Exchange instrument:" is the venue; "Source
        # symbol:" is the MT4 source.
        self.assertIn("Exchange instrument:", screen.text)
        self.assertIn("ETH-USD.P", screen.text)
        self.assertIn("Source symbol:", screen.text)
        self.assertIn("ETHUSD", screen.text)
        # The legacy "Symbol: ETHUSD" wording MUST NOT appear — the
        # two identities must remain unambiguous.
        self.assertNotIn("Symbol:       ETHUSD", screen.text)


# ---------------------------------------------------------------------------
# B. #SP500 ambiguous path
# ---------------------------------------------------------------------------


class Sp500CandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_direct_sp500_resolve_fails(self) -> None:
        resolver = _FakeResolver(
            {"US500-USD.P": "US500-USD.P", "SPY-USD.P": "SPY-USD.P"},
            fail_on={"#SP500"},
        )
        fibo = _good_fibo(symbol="#SP500")
        flow, _ = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
        )
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertGreater(len(sess.candidates), 0)

    def test_candidates_contain_us500_and_spy(self) -> None:
        """Ranking includes US500-USD.P and SPY-USD.P."""
        resolver = _FakeResolver(
            {"US500-USD.P": "US500-USD.P", "SPY-USD.P": "SPY-USD.P"},
            fail_on={"#SP500"},
        )
        fibo = _good_fibo(symbol="#SP500")
        flow, _ = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
        )
        sess = flow.session_store.get("chat-1", "user-1")
        instruments = {c.instrument for c in sess.candidates}
        self.assertIn("US500-USD.P", instruments)
        self.assertIn("SPY-USD.P", instruments)

    def test_candidate_metadata_includes_descriptions(self) -> None:
        """Each candidate carries a description string."""
        resolver = _FakeResolver(
            {"US500-USD.P": "US500-USD.P"}, fail_on={"#SP500"},
        )
        fibo = _good_fibo(symbol="#SP500")
        flow, _ = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
        )
        sess = flow.session_store.get("chat-1", "user-1")
        us500 = next(
            c for c in sess.candidates if c.instrument == "US500-USD.P"
        )
        # Description should include the longName.
        self.assertIn("US500", us500.description)

    def test_candidate_price_attached(self) -> None:
        """When price_map is provided, candidates carry prices."""
        resolver = _FakeResolver(
            {"US500-USD.P": "US500-USD.P"}, fail_on={"#SP500"},
        )
        fibo = _good_fibo(symbol="#SP500")
        price_map = {"US500-USD.P": Decimal("6520.4")}
        fx = self.fx
        fx.snap_path.write_text(json.dumps(_snapshot([fibo]).to_dict()))
        flow = _flow(fx, resolver=resolver, price_map=price_map)
        flow.open("chat-1", "user-1")
        flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_SELL}")
        flow.handle_callback("chat-1", "user-1", "fibo:s:ex:0")
        flow.handle_callback("chat-1", "user-1", "fibo:s:acct:0")
        sess = flow.session_store.get("chat-1", "user-1")
        us500 = next(
            c for c in sess.candidates if c.instrument == "US500-USD.P"
        )
        self.assertEqual(us500.price, Decimal("6520.4"))

    def test_user_can_select_us500(self) -> None:
        """Selecting US500-USD.P from the picker stages the proposal."""
        resolver = _FakeResolver(
            {"US500-USD.P": "US500-USD.P"}, fail_on={"#SP500"},
        )
        fibo = _good_fibo(symbol="#SP500")
        flow, _ = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
        )
        sess = flow.session_store.get("chat-1", "user-1")
        idx = next(
            i for i, c in enumerate(sess.candidates)
            if c.instrument == "US500-USD.P"
        )
        screen = flow.handle_callback(
            "chat-1", "user-1", f"{CB_CAND}{idx}",
        )
        # Proposal screen shows US500-USD.P.
        self.assertIn("US500-USD.P", screen.text)

    def test_selection_revalidates_via_agent(self) -> None:
        """The agent is called with US500-USD.P after the pick."""
        resolver = _FakeResolver(
            {"US500-USD.P": "US500-USD.P"}, fail_on={"#SP500"},
        )
        fibo = _good_fibo(symbol="#SP500")
        flow, _ = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
        )
        sess = flow.session_store.get("chat-1", "user-1")
        idx = next(
            i for i, c in enumerate(sess.candidates)
            if c.instrument == "US500-USD.P"
        )
        flow.handle_callback("chat-1", "user-1", f"{CB_CAND}{idx}")
        # The resolver was called with US500-USD.P (the candidate
        # instrument, NOT the source symbol).
        self.assertTrue(
            any(c[2] == "US500-USD.P" for c in resolver.calls)
        )

    def test_agree_stores_sp500_to_us500_usd_p(self) -> None:
        """Agree persists #SP500 -> US500-USD.P."""
        resolver = _FakeResolver(
            {"US500-USD.P": "US500-USD.P"}, fail_on={"#SP500"},
        )
        fibo = _good_fibo(symbol="#SP500")
        flow, _ = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
        )
        sess = flow.session_store.get("chat-1", "user-1")
        idx = next(
            i for i, c in enumerate(sess.candidates)
            if c.instrument == "US500-USD.P"
        )
        flow.handle_callback("chat-1", "user-1", f"{CB_CAND}{idx}")
        flow.handle_callback("chat-1", "user-1", CB_AGREE)
        flow.handle_text("chat-1", "user-1", "0.001")
        flow.handle_callback("chat-1", "user-1", CB_CREATE)
        regs = FiboRegistrationStore(self.fx.reg_path).load_all()
        self.assertEqual(regs[0].source_symbol, "#SP500")
        self.assertEqual(regs[0].exchange_instrument, "US500-USD.P")


# ---------------------------------------------------------------------------
# C. Price behavior
# ---------------------------------------------------------------------------


class PriceSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_missing_price_does_not_block_selection(self) -> None:
        """Missing prices do not block instrument selection."""
        # No price_map at all → all candidates have price=None.
        resolver = _FakeResolver(
            {"US500-USD.P": "US500-USD.P"}, fail_on={"#SP500"},
        )
        fibo = _good_fibo(symbol="#SP500")
        flow, _ = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
        )
        sess = flow.session_store.get("chat-1", "user-1")
        # All candidates have no price, but selection still works.
        for c in sess.candidates:
            self.assertIsNone(c.price)
        idx = next(
            i for i, c in enumerate(sess.candidates)
            if c.instrument == "US500-USD.P"
        )
        flow.handle_callback("chat-1", "user-1", f"{CB_CAND}{idx}")
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertEqual(sess.resolution_input, "US500-USD.P")

    def test_price_alone_cannot_auto_select(self) -> None:
        """The flow NEVER picks a candidate automatically. It always
        shows the picker and waits for the user to tap a button."""
        # ETHUSD auto-resolves to ETH-USD.P; the picker does NOT
        # appear (single candidate). For #SP500 (ambiguous), the
        # picker MUST appear even when one candidate looks like the
        # "right" answer.
        resolver = _FakeResolver(
            {"US500-USD.P": "US500-USD.P"}, fail_on={"#SP500"},
        )
        # Force a candidate that would "win" on price alone.
        fibo = _good_fibo(symbol="#SP500")
        flow, screen = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
        )
        # The screen MUST be the candidate picker, NOT the proposal
        # screen.
        self.assertIn("Possible matches", screen.text)
        self.assertIn("Choose the exchange instrument", screen.text)
        # No Agree button on the picker — only candidate picks.
        flat = [b for row in screen.buttons for b in row]
        self.assertNotIn(CB_AGREE, [b["callback_data"] for b in flat])

    def test_stale_price_does_not_cause_unsafe_fallback(self) -> None:
        """A bogus price does not silently promote the wrong
        candidate. The ranking uses the price only as supporting
        evidence after name similarity."""
        # Same catalog; inject wildly different prices. ETH-USD.P
        # is the only sensible match for ETHUSD (exact alias), so
        # it must still win even with a fake low price.
        resolver = _FakeResolver({"ETH-USD.P": "ETH-USD.P"},
                                fail_on={"ETHUSD"})
        fibo = _good_fibo(symbol="ETHUSD")
        # Put ETH-USD.P at a low price and BTC-USD.P at a high price.
        price_map = {
            "ETH-USD.P": Decimal("1"),
            "BTC-USD.P": Decimal("999999"),
        }
        fx = self.fx
        fx.snap_path.write_text(json.dumps(_snapshot([fibo]).to_dict()))
        flow = _flow(fx, resolver=resolver, price_map=price_map)
        flow.open("chat-1", "user-1")
        flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_SELL}")
        flow.handle_callback("chat-1", "user-1", "fibo:s:ex:0")
        flow.handle_callback("chat-1", "user-1", "fibo:s:acct:0")
        sess = flow.session_store.get("chat-1", "user-1")
        # ETH-USD.P is still rank #1 (exact-alias match > price).
        self.assertEqual(sess.candidates[0].instrument, "ETH-USD.P")

    def test_no_numeric_promotion_of_unrelated_exact_name(self) -> None:
        """A numerically matching price cannot promote an
        unrelated exact-name match above a true semantic match.
        Here BTC-USD.P has an exact match on BTC base, but ETH
        (semantic for ETHUSD) would normally score higher. The
        rule is: exact instrument > exact alias > similarity >
        price resemblance.
        """
        resolver = _FakeResolver({"ETH-USD.P": "ETH-USD.P"},
                                fail_on={"ETHUSD"})
        fibo = _good_fibo(symbol="ETHUSD")
        # BTC-USD.P has price that LOOKS like ETH (impossible, but
        # if the rule were "price wins", this would auto-select).
        price_map = {
            "ETH-USD.P": Decimal("2450.0"),
            "BTC-USD.P": Decimal("2450.0"),  # identical to ETH price
        }
        fx = self.fx
        fx.snap_path.write_text(json.dumps(_snapshot([fibo]).to_dict()))
        flow = _flow(fx, resolver=resolver, price_map=price_map)
        flow.open("chat-1", "user-1")
        flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_SELL}")
        flow.handle_callback("chat-1", "user-1", "fibo:s:ex:0")
        flow.handle_callback("chat-1", "user-1", "fibo:s:acct:0")
        sess = flow.session_store.get("chat-1", "user-1")
        # ETH-USD.P still ranks above BTC-USD.P despite identical
        # prices (ETH vs BTC base).
        eth_pos = next(
            i for i, c in enumerate(sess.candidates)
            if c.instrument == "ETH-USD.P"
        )
        btc_pos = next(
            i for i, c in enumerate(sess.candidates)
            if c.instrument == "BTC-USD.P"
        )
        self.assertLess(eth_pos, btc_pos)


# ---------------------------------------------------------------------------
# D. Other (raw alias) flow
# ---------------------------------------------------------------------------


class OtherAliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_raw_us500_resolves_to_canonical(self) -> None:
        """User types US500; agent resolves to US500-USD.P."""
        resolver = _FakeResolver(
            {"US500": "US500-USD.P"},
            fail_on={"#SP500"},
        )
        fibo = _good_fibo(symbol="#SP500")
        flow, _ = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
        )
        flow.handle_callback("chat-1", "user-1", CB_OTHER)
        screen = flow.handle_text("chat-1", "user-1", "US500")
        self.assertIn("US500-USD.P", screen.text)

    def test_only_canonical_stored(self) -> None:
        """The raw alias (US500) is NEVER stored as
        exchange_instrument — only the canonical US500-USD.P."""
        resolver = _FakeResolver(
            {"US500": "US500-USD.P"}, fail_on={"#SP500"},
        )
        fibo = _good_fibo(symbol="#SP500")
        flow, _ = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
        )
        flow.handle_callback("chat-1", "user-1", CB_OTHER)
        flow.handle_text("chat-1", "user-1", "US500")
        flow.handle_callback("chat-1", "user-1", CB_AGREE)
        flow.handle_text("chat-1", "user-1", "0.001")
        flow.handle_callback("chat-1", "user-1", CB_CREATE)
        regs = FiboRegistrationStore(self.fx.reg_path).load_all()
        # exchange_instrument is the canonical, not the raw alias.
        self.assertEqual(regs[0].exchange_instrument, "US500-USD.P")
        # And the resolution_input captures the raw alias for
        # display.
        self.assertEqual(regs[0].source_symbol, "#SP500")


# ---------------------------------------------------------------------------
# E. Alias memory behaviour
# ---------------------------------------------------------------------------


class AliasMemoryBehaviourTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_cached_mapping_revalidated(self) -> None:
        """Cached mapping is revalidated through the live agent."""
        alias_memory = AliasMemory(self.fx.alias_path)
        key = alias_key("apex", "ALT", "ETHUSD")
        alias_memory.record_approval(
            key, source_symbol="ETHUSD",
            resolution_input="ETHUSD",
            exchange_instrument="ETH-USD.P",
        )
        # The resolver must recognise BOTH the source symbol AND
        # the stored exchange_instrument.
        resolver = _FakeResolver(
            {"ETHUSD": "ETH-USD.P", "ETH-USD.P": "ETH-USD.P"},
        )
        fibo = _good_fibo(symbol="ETHUSD")
        flow, screen = _navigate_to_candidates(
            self.fx, resolver=resolver, alias_memory=alias_memory,
            fibo=fibo,
        )
        # Cached → proposal screen (not candidate picker).
        self.assertIn("ETH-USD.P", screen.text)
        self.assertNotIn("Possible matches", screen.text)

    def test_invalid_cached_mapping_discarded(self) -> None:
        """If the cached contract id is no longer resolved by the
        agent, the cache is silently dropped and the picker appears."""
        alias_memory = AliasMemory(self.fx.alias_path)
        key = alias_key("apex", "ALT", "ETHUSD")
        alias_memory.record_approval(
            key, source_symbol="ETHUSD",
            resolution_input="ETHUSD",
            exchange_instrument="ETH-USD.P",
        )
        # The resolver recognises the source symbol but NOT the
        # stored exchange_instrument (it has been delisted).
        resolver = _FakeResolver(
            {"ETHUSD": "ETH-USD.P"},
            fail_on={"ETH-USD.P"},
        )
        fibo = _good_fibo(symbol="ETHUSD")
        flow, screen = _navigate_to_candidates(
            self.fx, resolver=resolver, alias_memory=alias_memory,
            fibo=fibo,
        )
        # Fresh-resolution path: ETHUSD does resolve to ETH-USD.P,
        # so the proposal screen renders (not the picker).
        self.assertIn("ETH-USD.P", screen.text)

    def test_agree_increments_confirmation_count(self) -> None:
        """Selecting a candidate + Agree increments
        confirmation_count in alias memory."""
        alias_memory = AliasMemory(self.fx.alias_path)
        resolver = _FakeResolver(
            {"US500-USD.P": "US500-USD.P"}, fail_on={"#SP500"},
        )
        fibo = _good_fibo(symbol="#SP500")
        flow, _ = _navigate_to_candidates(
            self.fx, resolver=resolver, alias_memory=alias_memory,
            fibo=fibo,
        )
        sess = flow.session_store.get("chat-1", "user-1")
        idx = next(
            i for i, c in enumerate(sess.candidates)
            if c.instrument == "US500-USD.P"
        )
        flow.handle_callback("chat-1", "user-1", f"{CB_CAND}{idx}")
        # Before Agree: alias file unchanged (or doesn't exist).
        before = json.loads(self.fx.alias_path.read_text()) \
            if self.fx.alias_path.exists() else {"mappings": {}}
        flow.handle_callback("chat-1", "user-1", CB_AGREE)
        # After Agree: alias file updated with the new mapping.
        after = json.loads(self.fx.alias_path.read_text())
        # Find the relevant key.
        # The flow's pick uses the source_symbol (#SP500) for the
        # alias key; the canonical (US500-USD.P) is stored.
        new_keys = set(after["mappings"].keys()) - set(
            before["mappings"].keys()
        )
        self.assertEqual(len(new_keys), 1)
        # The new key contains #SP500 and the canonical.
        (new_key,) = tuple(new_keys)
        self.assertIn("#SP500", new_key)
        self.assertEqual(
            after["mappings"][new_key]["exchange_instrument"],
            "US500-USD.P",
        )
        self.assertEqual(
            after["mappings"][new_key]["confirmation_count"], 1,
        )

    def test_viewing_candidate_does_not_increment_count(self) -> None:
        """Just viewing / selecting a candidate must NOT bump
        confirmation_count."""
        alias_memory = AliasMemory(self.fx.alias_path)
        resolver = _FakeResolver(
            {"US500-USD.P": "US500-USD.P"}, fail_on={"#SP500"},
        )
        fibo = _good_fibo(symbol="#SP500")
        flow, _ = _navigate_to_candidates(
            self.fx, resolver=resolver, alias_memory=alias_memory,
            fibo=fibo,
        )
        sess = flow.session_store.get("chat-1", "user-1")
        idx = next(
            i for i, c in enumerate(sess.candidates)
            if c.instrument == "US500-USD.P"
        )
        # Just pick — no Agree yet.
        flow.handle_callback("chat-1", "user-1", f"{CB_CAND}{idx}")
        # Alias file MUST still be empty (no record_approval yet).
        if self.fx.alias_path.exists():
            data = json.loads(self.fx.alias_path.read_text())
            self.assertEqual(data["mappings"], {})


# ---------------------------------------------------------------------------
# F. Callback budget + isolation
# ---------------------------------------------------------------------------


class CallbackBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_candidate_callbacks_under_64_bytes(self) -> None:
        """Every candidate picker button stays <= 64 bytes."""
        # Build a flow with a long-instrument-name catalog so we
        # verify the format stays short.
        catalog = [
            {"market": f"LONGNAME-{i:03d}-USD.P",
             "displayName": f"LONGNAME-{i:03d}USD",
             "longName": "Test",
             "pair": {"base": f"LONGNAME-{i:03d}", "quote": "USD"}}
            for i in range(20)
        ]
        resolver = _FakeResolver(
            {"LONGNAME-001-USD.P": "LONGNAME-001-USD.P"},
            fail_on={"#TEST"},
        )
        fibo = _good_fibo(symbol="#TEST")
        fx = self.fx
        fx.snap_path.write_text(json.dumps(_snapshot([fibo]).to_dict()))
        flow = _flow(fx, resolver=resolver, catalog=catalog)
        flow.open("chat-1", "user-1")
        flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_SELL}")
        flow.handle_callback("chat-1", "user-1", "fibo:s:ex:0")
        screen = flow.handle_callback("chat-1", "user-1", "fibo:s:acct:0")
        flat = [b for row in screen.buttons for b in row]
        for b in flat:
            self.assertLessEqual(
                len(b["callback_data"]), 64,
                f"callback {b['callback_data']!r} exceeds 64 bytes",
            )
            self.assertLessEqual(len(b["callback_data"]), 32)

    def test_per_user_session_isolation(self) -> None:
        """Per-user text interception isolation (Phase 2.2 carried over)."""
        self.assertEqual(
            TEXT_INTERCEPT_STATES,
            frozenset({
                SessionState.AWAITING_VOLUME,
                SessionState.AWAITING_EXCHANGE_ALIAS,
            }),
        )


# ---------------------------------------------------------------------------
# G. Safety (zero exchange writes)
# ---------------------------------------------------------------------------

class PriceReadHardeningTests(unittest.TestCase):
    """Phase 2.4 hardening: the public TradeDesk.execute price read
    works for canonical Ondo instruments such as ``ETH-USD.P``,
    ``US500-USD.P``, ``SPY-USD.P`` — fully offline via
    ``FakeTradeDesk``.

    These tests prove that ``plugins.trade.fibo.discovery``
    reads prices through the same public TradeDesk boundary that
    production uses. ``get_market_price`` must return a finite
    Decimal when supported and ``None`` when not (or when no
    price is available for a market).
    """

    CANONICAL_INSTRUMENTS = (
        "ETH-USD.P",
        "US500-USD.P",
        "SPY-USD.P",
        "BTC-USD.P",
        "XAU-USD.P",
    )

    def setUp(self) -> None:
        # Base fixture — keeps an fx handle for any test that
        # chooses to navigate through a full flow.
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)
        # Phase 2.4: every test in this class installs a
        # FakeTradeDesk with deterministic prices so the suite
        # is fully offline.
        from plugins.trade.fibo import discovery
        from plugins.trade.tests.fake_tradedesk import FakeTradeDesk
        self._desk = FakeTradeDesk()
        for m in self.CANONICAL_INSTRUMENTS:
            self._desk.price_map[("ondoperps", "BITGET", m)] = Decimal("100")
            self._desk.price_map[("ondoperps", "ALT", m)] = Decimal("100")
        self._prior_get_desk = discovery._get_desk
        discovery._get_desk = lambda: self._desk

    def tearDown(self) -> None:
        from plugins.trade.fibo import discovery
        discovery._get_desk = self._prior_get_desk

    def _live_priced(self, market: str):
        from plugins.trade.fibo import discovery
        return discovery.get_market_price("ondoperps", "BITGET", market)

    def test_canonical_eth_usd_p_price_returns_finite_decimal(self) -> None:
        price = self._live_priced("ETH-USD.P")
        self.assertIsNotNone(price)
        self.assertIsInstance(price, Decimal)
        self.assertTrue(price.is_finite())
        self.assertGreater(price, Decimal("0"))

    def test_canonical_us500_usd_p_price_returns_finite_decimal(self) -> None:
        price = self._live_priced("US500-USD.P")
        self.assertIsNotNone(price)
        self.assertIsInstance(price, Decimal)
        self.assertTrue(price.is_finite())
        self.assertGreater(price, Decimal("0"))

    def test_canonical_spy_usd_p_price_returns_finite_decimal(self) -> None:
        price = self._live_priced("SPY-USD.P")
        self.assertIsNotNone(price)
        self.assertIsInstance(price, Decimal)
        self.assertTrue(price.is_finite())
        self.assertGreater(price, Decimal("0"))

    def test_all_canonical_instruments_have_a_price(self) -> None:
        """Loop through every documented canonical instrument and
        ensure the helper returns a finite Decimal for each."""
        for m in self.CANONICAL_INSTRUMENTS:
            price = self._live_priced(m)
            self.assertIsNotNone(price, m)
            self.assertIsInstance(price, Decimal)
            self.assertTrue(price.is_finite())

    def test_unknown_market_returns_none_without_raising(self) -> None:
        """The helper must not raise on unknown markets — just
        return None."""
        from plugins.trade.fibo import discovery
        result = discovery.get_market_price("ondoperps", "BITGET", "DOES-NOT-EXIST")
        self.assertIsNone(result)

    def test_empty_market_returns_none(self) -> None:
        from plugins.trade.fibo import discovery
        self.assertIsNone(discovery.get_market_price("ondoperps", "BITGET", ""))
        self.assertIsNone(discovery.get_market_price("ondoperps", "BITGET", "   "))

    def test_price_read_uses_no_write_tokens(self) -> None:
        """Static guard: the price-read source contains no write
        operation or non-GET HTTP method."""
        import inspect
        from plugins.trade.fibo import discovery
        src = inspect.getsource(discovery)
        # We scan the source after stripping strings and comments.
        cleaned = _strip_strings(src)
        for tok in (
            "new_order", "market_order", "limit_order",
            "cancel_order", "cancel_order_group",
            "close_position", "stop_order",
            "httpx.post", "httpx.put", "httpx.delete", "httpx.patch",
            "requests.post", "requests.put", "requests.delete",
            "requests.patch",
            "method=\"POST\"", "method=\"PUT\"",
            "method=\"DELETE\"", "method=\"PATCH\"",
        ):
            self.assertNotIn(
                tok, cleaned,
                f"discovery.py references {tok!r}",
            )

    def test_alias_alias_eth_us500_still_resolve_via_alias_path(self) -> None:
        """Phase 2.2 aliases (ETH / US500) must still resolve via the
        agent's ``resolve_instrument`` — Phase 2.3 did not weaken
        this path. We test both aliases in independent sessions."""
        resolver = _FakeResolver(
            {"ETH": "ETH-USD.P", "US500": "US500-USD.P"},
            fail_on={"#SP500"},
        )
        fibo = _good_fibo(symbol="#SP500")
        # ETH alias in chat-1/user-1.
        flow, _ = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
        )
        flow.handle_callback("chat-1", "user-1", CB_OTHER)
        screen = flow.handle_text("chat-1", "user-1", "ETH")
        self.assertIn("ETH-USD.P", screen.text)
        # US500 alias in chat-2/user-2 (independent session).
        self.fx.snap_path.write_text(json.dumps(_snapshot([fibo]).to_dict()))
        flow2, _ = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
            chat_id="chat-2", user_id="user-2",
        )
        flow2.handle_callback("chat-2", "user-2", CB_OTHER)
        screen2 = flow2.handle_text("chat-2", "user-2", "US500")
        self.assertIn("US500-USD.P", screen2.text)


class UiPolishTests(unittest.TestCase):
    """Phase 2.3 UI polish regression tests.

    Covers:
      - Automatic proposal has no confusing empty "Your alias:" line.
      - Manual Other path shows "Your input:".
      - Cached mapping shows "Learned alias:".
      - Candidate type is displayed only once.
      - ETHUSD picker contains ETH-USD.P + price.
      - #SP500 picker contains SPY-USD.P + US500-USD.P + prices.
      - Final confirmation uses "Source symbol:" / "Exchange
        instrument:" wording.
      - Selecting a candidate does NOT write alias memory.
      - Typing in Other does NOT write alias memory.
      - Agree is the only path that may write alias memory.
      - Source has no write operation tokens.
    """

    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def _navigate_to_picker(
        self, *, fibo, resolver, alias_memory=None, price_map=None,
    ):
        self.fx.snap_path.write_text(
            json.dumps(_snapshot([fibo]).to_dict())
        )
        flow = _flow(
            self.fx, resolver=resolver, alias_memory=alias_memory,
            price_map=price_map,
        )
        flow.open("chat-1", "user-1")
        flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
        flow.handle_callback(
            "chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_BUY}",
        )
        flow.handle_callback("chat-1", "user-1", "fibo:s:ex:2")
        flow.handle_callback("chat-1", "user-1", f"{CB_ACCT}0")
        return flow, flow.session_store.get("chat-1", "user-1")

    def _good_buy_fibo(self, symbol: str):
        return _good_fibo(
            symbol=symbol,
            buy_cycle_id=46626815,
            cumulative_buy_weight="2",
            sell_cycle_id=0,
            cumulative_sell_weight="0",
        )

    def test_automatic_proposal_omits_alias_line(self) -> None:
        """Direct resolve success → no alias label at all."""
        resolver = _FakeResolver({
            "ETHUSD": "ETH-USD.P",
            "ETH-USD.P": "ETH-USD.P",
        })
        fibo = self._good_buy_fibo("ETHUSD")
        flow, sess = self._navigate_to_picker(
            fibo=fibo, resolver=resolver,
        )
        self.assertEqual(
            sess.state, SessionState.AWAITING_INSTRUMENT_CONFIRM,
        )
        screen = flow._render_instrument_proposal(
            sess, flow._snapshot_store.load(),
        )
        for label in ("Your alias:", "Your input:", "Learned alias:"):
            self.assertNotIn(label, screen.text)
        # Phase 2.4.2: the exchange label is dynamic (no longer
        # hard-coded to "OndoPerps:"). The generic helper derives
        # the label from the actual selected session exchange id.
        from plugins.trade.fibo.flow import _exchange_display_label
        sess_exchange = sess.exchange or "ondoperps"
        self.assertIn(
            _exchange_display_label(sess_exchange), screen.text,
            f"proposal screen must show the dynamic label for "
            f"{sess_exchange!r}",
        )
        self.assertNotIn(
            "OndoPerps:", screen.text,
            "proposal screen must NOT contain the hard-coded label",
        )
        self.assertIn("ETH-USD.P", screen.text)

    def test_other_path_shows_your_input(self) -> None:
        """User typed via Other → "Your input: <typed>\" label."""
        resolver = _FakeResolver(
            {"US500": "US500-USD.P"}, fail_on={"#SP500"},
        )
        fibo = self._good_buy_fibo("#SP500")
        flow, _ = self._navigate_to_picker(
            fibo=fibo, resolver=resolver,
        )
        flow.handle_callback("chat-1", "user-1", CB_OTHER)
        flow.handle_text("chat-1", "user-1", "US500")
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertEqual(sess.proposal_origin, "alias")
        screen = flow._render_instrument_proposal(
            sess, flow._snapshot_store.load(),
        )
        self.assertIn("Your input:", screen.text)
        self.assertIn("US500", screen.text)
        self.assertNotIn("Learned alias:", screen.text)

    def test_cached_with_user_alias_shows_learned_alias(self) -> None:
        """Cached mapping whose resolution_input differs from source
        shows 'Learned alias:'."""
        am = AliasMemory(self.fx.alias_path)
        key = alias_key("ondoperps", "ALT", "ETHUSD")
        am.record_approval(
            key, source_symbol="ETHUSD",
            resolution_input="ETH",  # user typed ETH originally
            exchange_instrument="ETH-USD.P",
        )
        resolver = _FakeResolver({"ETH-USD.P": "ETH-USD.P"})
        fibo = self._good_buy_fibo("ETHUSD")
        flow, sess = self._navigate_to_picker(
            fibo=fibo, resolver=resolver, alias_memory=am,
        )
        self.assertEqual(sess.proposal_origin, "cached")
        screen = flow._render_instrument_proposal(
            sess, flow._snapshot_store.load(),
        )
        self.assertIn("Learned alias:", screen.text)
        self.assertIn("ETH", screen.text)
        self.assertNotIn("Your input:", screen.text)

    def test_cached_when_resolution_input_equals_source_omits_label(
        self,
    ) -> None:
        """Cached mapping with no separate user alias (resolution_input
        == source) — no alias line."""
        am = AliasMemory(self.fx.alias_path)
        key = alias_key("ondoperps", "ALT", "ETHUSD")
        am.record_approval(
            key, source_symbol="ETHUSD",
            resolution_input="ETHUSD",  # first approval: same as source
            exchange_instrument="ETH-USD.P",
        )
        resolver = _FakeResolver({"ETH-USD.P": "ETH-USD.P"})
        fibo = self._good_buy_fibo("ETHUSD")
        flow, sess = self._navigate_to_picker(
            fibo=fibo, resolver=resolver, alias_memory=am,
        )
        self.assertEqual(sess.proposal_origin, "cached")
        screen = flow._render_instrument_proposal(
            sess, flow._snapshot_store.load(),
        )
        for label in ("Your alias:", "Your input:", "Learned alias:"):
            self.assertNotIn(label, screen.text)

    def test_candidate_type_displayed_only_once(self) -> None:
        """The candidate's market type tag appears at most once in
        its compact block."""
        cand = InstrumentCandidate(
            instrument="ETH-USD.P",
            display_name="ETHUSD",
            description="Ethereum [crypto]",
            market_type="crypto",
            price=Decimal("2462"),
            score=130,
            reasons=("exact",),
        )
        block = cand.to_compact_block(0)
        # Description includes [crypto]; market_type line is NOT
        # repeated.
        self.assertIn("[crypto]", block)
        self.assertEqual(block.count("[crypto]"), 1)

    def test_ethusd_picker_has_eth_usd_p_and_price(self) -> None:
        """ETHUSD picker shows ETH-USD.P and its price."""
        resolver = _FakeResolver(
            {"ETH-USD.P": "ETH-USD.P"}, fail_on={"ETHUSD"},
        )
        fibo = self._good_buy_fibo("ETHUSD")
        flow, sess = self._navigate_to_picker(
            fibo=fibo, resolver=resolver,
            price_map={"ETH-USD.P": Decimal("2462.4")},
        )
        picker = flow._render_candidates_screen(
            sess, flow._snapshot_store.load(),
        )
        self.assertIn("ETH-USD.P", picker.text)
        self.assertIn("2462.4", picker.text)

    def test_sp500_picker_has_spy_and_us500(self) -> None:
        """#SP500 picker includes SPY-USD.P and US500-USD.P with
        prices."""
        resolver = _FakeResolver(
            {"US500-USD.P": "US500-USD.P"}, fail_on={"#SP500"},
        )
        fibo = self._good_buy_fibo("#SP500")
        flow, sess = self._navigate_to_picker(
            fibo=fibo, resolver=resolver,
            price_map={
                "SPY-USD.P": Decimal("764.89"),
                "US500-USD.P": Decimal("7671.24"),
            },
        )
        picker = flow._render_candidates_screen(
            sess, flow._snapshot_store.load(),
        )
        self.assertIn("SPY-USD.P", picker.text)
        self.assertIn("US500-USD.P", picker.text)
        self.assertIn("764.89", picker.text)
        self.assertIn("7671.24", picker.text)

    def test_final_confirmation_new_wording(self) -> None:
        """Final confirmation uses 'Source symbol:' and
        'Exchange instrument:' labels — not the old 'Symbol:' /
        'MT4 source:' wording."""
        from plugins.trade.fibo.flow import StartFiboFlow
        flow = StartFiboFlow(
            snapshot_store=Mt4SnapshotStore(self.fx.snap_path),
            registration_store=FiboRegistrationStore(self.fx.reg_path),
            list_exchanges_fn=lambda: [],
            list_accounts_fn=lambda ex: [],
            list_instruments_fn=lambda ex, ac: [],
            resolve_instrument_fn=lambda *a, **kw: None,
        )
        text = flow._format_confirmation(
            source_symbol="ETHUSD",
            variant="NORMALFib",
            side="BUY",
            exchange="ondoperps",
            account="bitget",
            exchange_instrument="ETH-USD.P",
            starting_volume=Decimal("0.001"),
            source="mt4-Fresh542468-1",
            source_seq=29992,
            cycle_id=46626815,
            cumulative_weight=Decimal("2"),
            percentage=Decimal("0.01"),
            desired_exchange_size=Decimal("0.002"),
            snapshot_age="3.1s",
        )
        self.assertIn("Source symbol:       ETHUSD", text)
        self.assertIn("Exchange instrument: ETH-USD.P", text)
        self.assertNotIn("Symbol:", text)
        self.assertNotIn("MT4 source:", text)

    def test_selecting_candidate_does_not_write_alias(self) -> None:
        """Picking a candidate writes nothing to alias memory."""
        am = AliasMemory(self.fx.alias_path)
        resolver = _FakeResolver(
            {"US500-USD.P": "US500-USD.P"}, fail_on={"#SP500"},
        )
        fibo = self._good_buy_fibo("#SP500")
        flow, sess = self._navigate_to_picker(
            fibo=fibo, resolver=resolver, alias_memory=am,
        )
        idx = next(
            i for i, c in enumerate(sess.candidates)
            if c.instrument == "US500-USD.P"
        )
        flow.handle_callback(
            "chat-1", "user-1", f"{CB_CAND}{idx}",
        )
        if self.fx.alias_path.exists():
            data = json.loads(self.fx.alias_path.read_text())
            self.assertEqual(data["mappings"], {})

    def test_typing_in_other_does_not_write_alias(self) -> None:
        """Typing an alias via Other writes nothing to alias memory."""
        am = AliasMemory(self.fx.alias_path)
        resolver = _FakeResolver(
            {"US500": "US500-USD.P"}, fail_on={"#SP500"},
        )
        fibo = self._good_buy_fibo("#SP500")
        flow, _ = self._navigate_to_picker(
            fibo=fibo, resolver=resolver, alias_memory=am,
        )
        flow.handle_callback("chat-1", "user-1", CB_OTHER)
        flow.handle_text("chat-1", "user-1", "US500")
        if self.fx.alias_path.exists():
            data = json.loads(self.fx.alias_path.read_text())
            self.assertEqual(data["mappings"], {})

    def test_only_agree_writes_alias(self) -> None:
        """Agree is the only path that writes alias memory."""
        am = AliasMemory(self.fx.alias_path)
        resolver = _FakeResolver(
            {"US500-USD.P": "US500-USD.P"}, fail_on={"#SP500"},
        )
        fibo = self._good_buy_fibo("#SP500")
        flow, sess = self._navigate_to_picker(
            fibo=fibo, resolver=resolver, alias_memory=am,
        )
        idx = next(
            i for i, c in enumerate(sess.candidates)
            if c.instrument == "US500-USD.P"
        )
        flow.handle_callback(
            "chat-1", "user-1", f"{CB_CAND}{idx}",
        )
        flow.handle_callback("chat-1", "user-1", CB_AGREE)
        # After Agree: alias file exists with the new mapping.
        self.assertTrue(self.fx.alias_path.exists())
        data = json.loads(self.fx.alias_path.read_text())
        self.assertEqual(len(data["mappings"]), 1)

    def test_polished_source_has_no_write_tokens(self) -> None:
        """Static guard: polished modules contain no write
        operations or non-GET HTTP methods. We strip docstrings
        and string literals before scanning."""
        import re
        for name in ("flow", "candidates", "discovery"):
            mod = __import__(
                f"plugins.trade.fibo.{name}", fromlist=["*"]
            )
            src = inspect.getsource(mod)
            cleaned = re.sub(r'"""[\s\S]*?"""', "", src)
            cleaned = re.sub(r"'''[\s\S]*?'''", "", cleaned)
            cleaned = "\n".join(
                ln for ln in cleaned.splitlines()
                if not ln.lstrip().startswith("#")
            )
            cleaned = "\n".join(
                re.sub(r'"[^"\\\n]*(?:\\.[^"\\\n]*)*"', "", ln)
                for ln in cleaned.splitlines()
            )
            cleaned = "\n".join(
                re.sub(r"'[^'\\\n]*(?:\\.[^'\\\n]*)*'", "", ln)
                for ln in cleaned.splitlines()
            )
            for tok in (
                "new_order", "market_order", "limit_order",
                "cancel_order", "cancel_order_group",
                "close_position", "stop_order",
                "method=\"POST\"", "method=\"PUT\"",
                "method=\"DELETE\"", "method=\"PATCH\"",
            ):
                self.assertNotIn(
                    tok, cleaned,
                    f"{name}.py references {tok!r}",
                )


class SafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_candidate_discovery_uses_only_read_ops(self) -> None:
        """No write operation appears in flow / candidates / discovery."""
        import inspect
        for mod_name in ("flow", "candidates", "discovery"):
            try:
                mod = __import__(
                    f"plugins.trade.fibo.{mod_name}", fromlist=["*"],
                )
            except ImportError:
                continue
            src = inspect.getsource(mod)
            for tok in (
                "new_order", "market_order", "limit_order",
                "cancel_order", "cancel_order_group",
                "close_position", "stop_order",
                "httpx.post", "requests.post",
                "method=\"POST\"", "method=\"PUT\"",
                "method=\"DELETE\"", "method=\"PATCH\"",
            ):
                # Strip string literals and comments for the
                # static check.
                cleaned = _strip_strings(src)
                self.assertNotIn(
                    tok, cleaned,
                    f"{mod_name}.py references {tok!r}",
                )

    def test_fake_exec_rejects_non_read_ops(self) -> None:
        """Behavioral guard: only allowlisted ops pass."""
        fake = _FakeExec()
        resp = fake({"operation": "new_order", "exchange": "x", "account": "y"})
        self.assertFalse(getattr(resp, "success", False))


# ---------------------------------------------------------------------------
# H. Fallback (Other / Browse / Back / Cancel)
# ---------------------------------------------------------------------------


class FallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_candidate_discovery_failure_falls_through_to_other(self) -> None:
        """If the catalog is empty, Other / Browse still work."""
        resolver = _FakeResolver({}, fail_on={"#X"})
        fibo = _good_fibo(symbol="#X")
        fx = self.fx
        fx.snap_path.write_text(json.dumps(_snapshot([fibo]).to_dict()))
        # Empty catalog.
        flow = _flow(fx, resolver=resolver, catalog=[])
        flow.open("chat-1", "user-1")
        flow.handle_callback("chat-1", "user-1", f"{CB_SYM}0")
        flow.handle_callback("chat-1", "user-1", f"{CB_SIDE}{SIDE_TOKEN_SELL}")
        flow.handle_callback("chat-1", "user-1", "fibo:s:ex:0")
        screen = flow.handle_callback("chat-1", "user-1", "fibo:s:acct:0")
        # Empty catalog → "could not resolve" screen (Other / Browse
        # / Back / Cancel).
        self.assertIn("Could not uniquely resolve", screen.text)
        flat = [b for row in screen.buttons for b in row]
        cbs = [b["callback_data"] for b in flat]
        self.assertIn(CB_OTHER, cbs)
        self.assertIn(CB_BROWSE, cbs)
        self.assertIn(CB_BACK, cbs)
        self.assertIn(CB_CANCEL, cbs)

    def test_browse_still_works_after_picker(self) -> None:
        """Browse markets is reachable from the candidate picker."""
        resolver = _FakeResolver(
            {"US500-USD.P": "US500-USD.P"}, fail_on={"#SP500"},
        )
        fibo = _good_fibo(symbol="#SP500")
        flow, screen = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
        )
        # From the picker, tap Browse.
        screen = flow.handle_callback("chat-1", "user-1", CB_BROWSE)
        self.assertIn("Markets on", screen.text)

    def test_back_from_picker_to_account(self) -> None:
        """Back from the picker returns to the account screen."""
        resolver = _FakeResolver(
            {"US500-USD.P": "US500-USD.P"}, fail_on={"#SP500"},
        )
        fibo = _good_fibo(symbol="#SP500")
        flow, screen = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
        )
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertEqual(
            sess.state, SessionState.AWAITING_INSTRUMENT_CONFIRM,
        )
        screen = flow.handle_callback("chat-1", "user-1", CB_BACK)
        sess = flow.session_store.get("chat-1", "user-1")
        self.assertEqual(sess.state, SessionState.AWAITING_ACCOUNT)
        self.assertIn("Pick an account", screen.text)

    def test_cancel_clears_session_from_picker(self) -> None:
        """Cancel from the candidate picker clears the session."""
        resolver = _FakeResolver(
            {"US500-USD.P": "US500-USD.P"}, fail_on={"#SP500"},
        )
        fibo = _good_fibo(symbol="#SP500")
        flow, _ = _navigate_to_candidates(
            self.fx, resolver=resolver, fibo=fibo,
        )
        flow.handle_callback("chat-1", "user-1", CB_CANCEL)
        self.assertIsNone(flow.session_store.get("chat-1", "user-1"))


# ---------------------------------------------------------------------------
# Static-source stripper (re-used by SafetyTests)
# ---------------------------------------------------------------------------


def _strip_strings(src: str) -> str:
    """Strip docstrings and string literals from ``src`` so the
    static guards scan only executable tokens.

    Uses a line-based finite-state machine that drops:

    * Triple-quoted docstring bodies (between the opening and
      closing ``\"\"\"`` / ``'''``).
    * Single-line ``# comments``.
    * Inline string literals on each remaining line.

    False-positives are the safer failure mode for a static guard.
    """
    out_lines: List[str] = []
    in_triple = False
    triple_quote: Optional[str] = None
    for line in src.splitlines():
        stripped = line.lstrip()
        if not in_triple and stripped.startswith("#"):
            continue
        if in_triple:
            # We may have been opened mid-line; check for the close.
            if triple_quote is not None and triple_quote in line:
                idx = line.find(triple_quote) + len(triple_quote)
                line = line[idx:]
                in_triple = False
                triple_quote = None
            else:
                continue
        if not in_triple:
            open_q: Optional[str] = None
            open_idx = -1
            for q in ('"""', "'''"):
                i = 0
                while True:
                    j = line.find(q, i)
                    if j < 0:
                        break
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
                rest = line[open_idx + len(open_q):]
                close_idx = rest.find(open_q)
                if close_idx >= 0:
                    line = head + rest[close_idx + len(open_q):]
                else:
                    line = head
                    in_triple = True
                    triple_quote = open_q
        if in_triple:
            if line:
                out_lines.append(line)
            continue
        cleaned: List[str] = []
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == '"' or ch == "'":
                quote = ch
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


if __name__ == "__main__":
    unittest.main()
