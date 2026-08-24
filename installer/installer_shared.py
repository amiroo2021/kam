"""Shared core installer for KAM.

Installed once whenever ANY capability is installed.

Takes EXPLICIT ``hermes_root`` and ``hermes_home`` arguments. NEVER
derives one from the other (Decision: hermes_root and hermes_home are
independent concepts). The hermes_root is the installed application tree
(typically /usr/local/lib/hermes-agent); the hermes_home is the
persistent user state directory (typically ~/.hermes).

The shared core consists of:
  - ``plugins/trade/agents/x_*_agent.py`` exchange agents
  - ``plugins/trade/canonical.py`` (canonical response models)
  - ``plugins/trade/tradedesk.py`` (exchange/account dispatcher)
  - ``plugins/trade/__init__.py`` (capability-aware plugin bootstrap)

These files live under the ``plugins/trade/`` plugin tree so Hermes'
existing discovery (``plugins.enabled``) keeps working, but they are
**SHARED** — they MUST exist before any capability can run.

Idempotency: each shared file is copied byte-for-byte; sha256 is checked
before copy so unchanged files are skipped. No capability-specific files
are touched by this module.

``--dry-run`` mode: every copy is reported but ZERO bytes are written.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capabilities import (
    KNOWN_CAPABILITIES,
    SCHEMA_VERSION,
    capability_dir,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
SHARED_REL_PATHS = [
    Path("plugins") / "trade" / "__init__.py",
    # REQUIRED for Hermes PluginManager discovery. Without plugin.yaml the
    # package may import and adapter seams may dispatch, but discover_plugins()
    # never loads `trade` → get_plugin_commands() stays empty → Telegram
    # setMyCommands never publishes /trade (Lodo 2026-08-19).
    Path("plugins") / "trade" / "plugin.yaml",
    Path("plugins") / "trade" / "agents" / "__init__.py",
    Path("plugins") / "trade" / "agents" / "x_apex_agent.py",
    Path("plugins") / "trade" / "agents" / "x_arcus_agent.py",
    Path("plugins") / "trade" / "agents" / "x_edgex_agent.py",
    Path("plugins") / "trade" / "agents" / "x_hibachi_agent.py",
    Path("plugins") / "trade" / "agents" / "x_hyperliquid_agent.py",
    Path("plugins") / "trade" / "agents" / "x_lighter_agent.py",
    Path("plugins") / "trade" / "agents" / "x_ondoperps_agent.py",
    Path("plugins") / "trade" / "agents" / "x_pacifica_agent.py",
    Path("plugins") / "trade" / "agents" / "x_raydium_agent.py",
    Path("plugins") / "trade" / "agents" / "x_rise_agent.py",
    Path("plugins") / "trade" / "canonical.py",
    Path("plugins") / "trade" / "tradedesk.py",
]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _resolve_plugin_root(hermes_root: Path) -> Path:
    """Return ``<hermes_root>/plugins/trade/`` for the shared plugin tree."""
    return hermes_root / "plugins" / "trade"


def install_shared(
    *,
    argv: Sequence[str],
    hermes_root: Path,
    hermes_home: Path,
    capabilities: List[str],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Copy the SHARED core files into the Hermes installation.

    Returns a record suitable for ``by_capability[*]['shared']``.

    If ``dry_run`` is True, every copy is reported but no bytes are
    written and no directory is created.
    """
    plugin_root = _resolve_plugin_root(hermes_root)
    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "files": [],
        "ok": True,
        "dry_run": dry_run,
        "target_plugin_root": str(plugin_root),
    }
    for rel in SHARED_REL_PATHS:
        src = REPO_ROOT / rel
        # Strip "plugins/trade/" prefix; install under hermes_root/plugins/trade/.
        try:
            rel_under_plugin_trade = rel.relative_to(Path("plugins") / "trade")
        except ValueError:
            rel_under_plugin_trade = rel
        dst = plugin_root / rel_under_plugin_trade
        entry: Dict[str, Any] = {"path": str(rel)}
        if not src.is_file():
            record["ok"] = False
            entry["action"] = "missing-source"
            record["files"].append(entry)
            continue
        if dst.is_file() and _sha256_file(src) == _sha256_file(dst):
            entry["action"] = "unchanged"
        else:
            entry["src_sha256"] = _sha256_file(src)
            if not dry_run:
                if not dst.parent.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            entry["action"] = "copied" if not dry_run else "would-copy"
        record["files"].append(entry)
    return record


__all__ = ["install_shared", "SHARED_REL_PATHS"]