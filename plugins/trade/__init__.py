"""Hermes KAM trade plugin package — capability-aware bootstrap.

This package is installed whenever the KAM /trade capability is
installed. The package marker itself is the /trade capability marker.

Capability-aware slash-command registration:

  --trade installed  -> /trade registered

The authoritative source is ``~/.hermes/kam/install_state.json``'s
``capabilities`` map. This package's ``register`` function reads that
manifest at registration time and registers /trade for the
currently-installed capability.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List


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
# Capability manifest resolution (read-only)
# ---------------------------------------------------------------------------


def _resolve_hermes_home() -> Path:
    """Resolve the Hermes home directory from environment or default."""
    env = os.environ.get("HERMES_HOME")
    if env and env.strip():
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def _load_installed_capabilities(hermes_home: Path) -> Dict[str, bool]:
    """Read the authoritative install_state.json and return its capabilities.

    Returns an empty mapping on any failure (missing file, malformed
    JSON, or wrong schema). The caller treats missing-as-empty as
    "no capabilities installed" and registers no commands.
    """
    try:
        import json
        path = Path(hermes_home) / "kam" / "install_state.json"
        if not path.is_file():
            return {}
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    caps = data.get("capabilities")
    if not isinstance(caps, dict):
        return {}
    return {str(k): bool(v) for k, v in caps.items()}


def _installed_capabilities() -> Dict[str, bool]:
    """Read installed capabilities from the authoritative manifest."""
    return _load_installed_capabilities(_resolve_hermes_home())


# ---------------------------------------------------------------------------
# Capability-aware registration
# ---------------------------------------------------------------------------


def _try_register_command(ctx: Any, name: str, handler: Any, description: str) -> bool:
    """Register a slash command on the plugin context, if supported."""
    register_cmd = getattr(ctx, "register_command", None)
    if not callable(register_cmd):
        return False
    try:
        register_cmd(
            name,
            handler=handler,
            description=description,
            args_hint="",
        )
    except Exception:
        return False
    return True


def register(ctx: Any) -> None:
    """Register the plugin with the Hermes plugin manager.

    Capability-aware slash-command registration:

      - if ``capabilities.trade`` is True, register /trade.

    If the install manifest is missing or unreadable, NO commands are
    registered (the conservative-safe choice: don't expose a command
    that hasn't been installed).
    """
    caps = _installed_capabilities()
    if caps.get("trade"):
        _try_register_command(
            ctx,
            "trade",
            handler=_handle_trade_slash,
            description="Open the trading wizard",
        )


# ---------------------------------------------------------------------------
# Capability-aware registration for tests
# ---------------------------------------------------------------------------


def registered_commands() -> List[str]:
    """Return the list of slash commands this installation would register.

    Used by the modular installer tests to verify capability-aware
    registration behavior without requiring a live Hermes gateway.
    Returns an empty list if no capabilities are installed.
    """
    caps = _installed_capabilities()
    out: List[str] = []
    if caps.get("trade"):
        out.append("trade")
    return out


__all__ = [
    "register",
    "registered_commands",
    "_handle_trade_slash",
]
