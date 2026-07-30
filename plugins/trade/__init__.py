"""Hermes /trade plugin package.

This package provides the Telegram trading wizard (Phase 1):
exchange-agnostic wizard + TradeDesk + x_<exchange>_agent.py
exchange agents.

The wizard's Telegram wiring lives in:

    plugins/trade/wizard.py        — exchange-agnostic state machine
    plugins/trade/tradedesk.py     — exchange-agnostic dispatcher
    plugins/trade/agents/          — per-exchange agent modules
        x_hyperliquid_agent.py    — Hyperliquid agent (Phase 1)

The integration with the Telegram adapter is **direct** — the
adapter imports ``plugins.trade.wizard.handle_trade_command`` and
``handle_trade_callback`` from its own dispatch paths.

In addition, this plugin registers ``/trade`` as a slash command via
``PluginContext.register_command`` so that the command appears in
Telegram's native slash-command menu (the menu is built from
``hermes_cli.commands._iter_plugin_command_entries``).  Telegram
routing itself still goes through the adapter's direct dispatch, so
the registered handler is only consulted on non-Telegram surfaces
(CLI / other platforms), where it returns a short pointer to the
Telegram wizard.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Slash-command handler (non-Telegram surfaces only)
# ---------------------------------------------------------------------------


def _handle_trade_slash(raw_args: str) -> str:
    """Handler invoked by the gateway when ``/trade`` is dispatched via the
    plugin-command registry.

    On Telegram the adapter's direct dispatch short-circuits the
    message before it reaches this code path, so this handler is only
    used from CLI or other platforms.  It simply tells the operator to
    open Hermes on Telegram — the wizard itself is Telegram-only.
    """
    suffix = (raw_args or "").strip()
    if suffix:
        return (
            "`/trade` is a Telegram-only wizard. "
            "Open Hermes on Telegram and type /trade to use it."
        )
    return (
        "`/trade` is a Telegram-only wizard. "
        "Open Hermes on Telegram and type /trade to start."
    )


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def register(ctx: Any) -> None:
    """Register the plugin with the Hermes plugin manager.

    Two side effects:

    1. Discovers and enables this plugin so it appears in
       ``hermes plugins list``.
    2. Registers the ``/trade`` slash command so it surfaces in
       Telegram's native slash-command menu.  The actual command
       routing on Telegram continues to happen via the adapter's
       direct dispatch path; this registration is purely so the
       command is discoverable in the Telegram ``/`` menu.
    """
    register_cmd = getattr(ctx, "register_command", None)
    if callable(register_cmd):
        # Plugin API: name, handler, description, args_hint.
        # No exchange names here — the wizard is exchange-agnostic and
        # discovers ``x_<exchange>_agent.py`` modules at runtime.
        register_cmd(
            "trade",
            handler=_handle_trade_slash,
            description="Open the trading wizard",
            args_hint="",
        )


__all__ = ["register", "_handle_trade_slash"]