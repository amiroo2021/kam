"""Hermes /trade plugin package.

This package provides the Telegram trading wizard (Phase 1):
exchange-agnostic wizard + TradeDesk + x_<exchange>_agent.py
exchange agents.

The wizard's Telegram wiring lives in:

    plugins/trade/wizard.py        — exchange-agnostic state machine
    plugins/trade/tradedesk.py     — exchange-agnostic dispatcher
    plugins/trade/agents/          — per-exchange agent modules
        x_hyperliquid_agent.py    — Hyperliquid agent (Phase 1)

The actual integration with the Telegram adapter is **direct** — the
adapter imports ``plugins.trade.wizard.handle_trade_command`` and
``handle_trade_callback`` from its own dispatch paths. This plugin
package does NOT register slash commands or callback prefixes
through a plugin-handler registry.

The plugin is loaded by the Hermes plugin manager (so it appears in
``hermes plugins list``) and contributes nothing else to the
runtime. The ``register`` function below is a no-op.
"""

from __future__ import annotations


def register(ctx) -> None:
    """No-op. Direct dispatch is wired in the Telegram adapter.

    The plugin is discovered and enabled so it shows up in
    ``hermes plugins list``, but it does not register any
    slash-command or callback hooks. The adapter calls
    ``plugins.trade.wizard.handle_trade_command`` and
    ``handle_trade_callback`` directly from its own dispatch paths.
    """
    return


__all__ = ["register"]
