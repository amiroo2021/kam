"""Phase 2.4 test helpers.

Shared utilities for setting up a `StartFiboFlow` with a
fully-offline ``FakeTradeDesk`` installed at
``plugins.trade.fibo.discovery._get_desk``.

Tests should use this so they NEVER reach real exchange APIs.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Stable imports — these are the same modules the production code
# imports.
from plugins.trade.fibo import discovery as fibo_discovery
from plugins.trade.fibo.flow import StartFiboFlow
from plugins.trade.fibo.session import FiboSessionStore
from plugins.trade.fibo.snapshot import Mt4Fibo, Mt4Snapshot, Mt4SnapshotStore
from plugins.trade.fibo.store import FiboRegistrationStore
from plugins.trade.tests.fake_tradedesk import FakeTradeDesk


def _good_fibo(
    *,
    symbol: str = "ETHUSD",
    variant: str = "NORMALFib",
    buy: int = 46626815,
    sell: int = 0,
    weight: str = "2",
    buy_weight: str = "",
    sell_weight: str = "",
    percentage: str = "0.01",
) -> Mt4Fibo:
    """Default: BUY active, SELL inactive."""
    if not buy_weight and not sell_weight:
        buy_weight = weight if buy else "0"
        sell_weight = weight if sell and not buy else "0"
    if buy and not buy_weight:
        buy_weight = "2"
    if sell and not sell_weight:
        sell_weight = "2"
    return Mt4Fibo(
        symbol=symbol,
        variant=variant,
        percentage=Decimal(percentage),
        buy_cycle_id=buy,
        cumulative_buy_weight=Decimal(buy_weight or "0"),
        sell_cycle_id=sell,
        cumulative_sell_weight=Decimal(sell_weight or "0"),
    )


def make_snapshot(fibos: List[Mt4Fibo]) -> Mt4Snapshot:
    """Build a fresh MT4 snapshot."""
    from datetime import datetime, timezone
    now = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    return Mt4Snapshot(
        v=1, source="mt4-Fresh-1", seq=42, ts=0, fibos=fibos,
        received_at=now, telegram_update_id=0,
        telegram_message_id=0, reader_chat_id=-100,
    )


@contextmanager
def fake_desk_installed(desk: FakeTradeDesk):
    """Install a FakeTradeDesk as discovery's desk for the duration
    of a ``with`` block. Tests that forget to enter this context
    can never see the real desk because the helper returns the
    mock context manager which patches the module binding.
    """
    prior = fibo_discovery._get_desk
    fibo_discovery._get_desk = lambda: desk
    try:
        yield desk
    finally:
        fibo_discovery._get_desk = prior


class OfflineFlowTestCase(unittest.TestCase):
    """Base class for Fibo flow tests that must NEVER reach a live
    exchange. Sets up a fake ``TradeDesk`` and routes the Fibo
    discovery through it.

    Subclasses can either:
    - accept the default ``FakeTradeDesk`` (no resolver / catalog /
      price responders); the wizard then shows the manual Other
      fallback for any source symbol that fails direct resolve.
    - override ``build_fake_desk()`` to return a fully configured
      fake with resolvers / catalogs / prices.
    """

    def setUp(self) -> None:
        super().setUp()
        # Per-test temp dir for snapshot + registrations.
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.snap_path = self.root / "mt4_snapshot.json"
        self.reg_path = self.root / "registrations.jsonl"
        self.addCleanup(self.tmp.cleanup)

        # Build a default fake desk and install it BEFORE constructing
        # the flow.
        self.desk = self.build_fake_desk()
        self._desk_ctx = fake_desk_installed(self.desk)
        self._desk_ctx.__enter__()
        self.addCleanup(self._desk_ctx.__exit__, None, None, None)

    def build_fake_desk(self) -> FakeTradeDesk:
        """Override to register resolvers / catalogs / prices."""
        return FakeTradeDesk()

    # ---- fixtures ----

    def make_flow(
        self,
        *,
        fibo: Optional[Mt4Fibo] = None,
        exchanges: Optional[List[str]] = None,
        accounts: Optional[List[str]] = None,
        alias_memory: Optional[Any] = None,
    ) -> StartFiboFlow:
        """Build a StartFiboFlow rooted at this test's temp dir.

        No ``resolve_instrument_fn`` parameter is passed — Fibo
        uses the public ``TradeDesk.execute({...})`` boundary via
        ``discovery._get_desk``, which is patched to the fake.
        """
        exchanges = exchanges or ["ondoperps"]
        accounts = accounts or ["MAIN", "BITGET"]
        if fibo is not None:
            self.snap_path.write_text(
                json.dumps(make_snapshot([fibo]).to_dict())
            )
        return StartFiboFlow(
            snapshot_store=Mt4SnapshotStore(self.snap_path),
            registration_store=FiboRegistrationStore(self.reg_path),
            list_exchanges_fn=lambda: list(exchanges),
            list_accounts_fn=lambda ex: list(accounts),
            list_instruments_fn=lambda ex, ac: [],
            resolve_instrument_fn=None,
            alias_memory=alias_memory,
        )
