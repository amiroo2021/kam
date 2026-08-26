"""KAM /fibo Phase 1 capability package.

This package owns the standalone /fibo Telegram wizard sub-flows:

* MT4 Observer snapshot reader (writes ``~/.hermes/fibo/mt4_snapshot.json``)
* Wizard-session manager (``FiboSession`` + TTL container)
* Start-Fibo sub-flow state machine (``StartFiboFlow``)
* Local Fibo registration store (JSONL at
  ``~/.hermes/fibo/registrations.jsonl``)

Phase 1 is local-only and read-only with respect to exchanges:

- No order placement, position modification, TP/SL changes, or any
  other exchange write operation.
- TradeDesk's ``list_exchanges()`` and ``list_accounts(exchange)`` are
  used purely for read-only discovery of supported exchanges and
  configured account aliases. Both are pure-env reads in every
  ``x_*_agent.py`` — no network, no write.
- The MT4 Reader module is the SOLE consumer of
  ``MT4_READER_BOT_TOKEN``'s ``getUpdates`` endpoint. The wizard never
  polls Telegram.

Public surface (consumed by ``plugins.trade.fibo_wizard``):

    from plugins.trade.fibo.snapshot import (
        Mt4Snapshot,
        Mt4SnapshotStore,
        Mt4Fibo,
    )
    from plugins.trade.fibo.store import (
        FiboRegistration,
        FiboRegistrationStore,
    )
    from plugins.trade.fibo.session import (
        FiboSession,
        FiboSessionStore,
    )
    from plugins.trade.fibo.flow import StartFiboFlow
"""

from __future__ import annotations

__all__ = [
    "snapshot",
    "store",
    "session",
    "flow",
    "mt4_reader",
]