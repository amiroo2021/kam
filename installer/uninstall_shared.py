"""Shared uninstaller.

Removes the SHARED core ONLY when no KAM capabilities remain installed.
The shared core consists of:
  - ``plugins/trade/agents/x_*_agent.py`` exchange agents
  - ``plugins/trade/agents/__init__.py``
  - ``plugins/trade/canonical.py``
  - ``plugins/trade/tradedesk.py``
  - ``plugins/trade/__init__.py`` (capability-aware plugin bootstrap)
  - ``~/.hermes/kam/`` authoritative manifest directory

Takes EXPLICIT ``hermes_root`` and ``hermes_home`` arguments. The
function ``run`` is a no-op when at least one capability is still
installed; this is enforced by the dispatcher.

NEVER touches ~/.hermes/trade/ -- that belongs to
the per-capability uninstaller.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capabilities import kam_root


REPO_ROOT = Path(__file__).resolve().parent.parent
SHARED_REL_PATHS = [
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
    Path("plugins") / "trade" / "__init__.py",
    Path("plugins") / "trade" / "plugin.yaml",
]


def run(
    *,
    argv: Sequence[str],
    hermes_root: Path,
    hermes_home: Path,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Remove shared core (idempotent; safe to call even when already removed)."""
    plugin_root = hermes_root / "plugins" / "trade"
    record: Dict[str, Any] = {
        "removed_files": [],
        "removed_dirs": [],
        "dry_run": dry_run,
    }
    for rel in SHARED_REL_PATHS:
        try:
            rel_under_plugin_trade = rel.relative_to(Path("plugins") / "trade")
        except ValueError:
            rel_under_plugin_trade = rel
        dst = plugin_root / rel_under_plugin_trade
        if dst.is_file():
            record["removed_files"].append(str(rel))
            if not dry_run:
                dst.unlink()
    # Remove the now-empty agents/ directory.
    agents_dir = plugin_root / "agents"
    if agents_dir.is_dir():
        try:
            is_empty = not any(agents_dir.iterdir())
        except OSError:
            is_empty = False
        if is_empty:
            record["removed_dirs"].append(str(agents_dir))
            if not dry_run:
                agents_dir.rmdir()
    # kam/ directory.
    kam_dir = kam_root(hermes_home)
    if kam_dir.is_dir():
        record["removed_dirs"].append(str(kam_dir))
        if not dry_run:
            shutil.rmtree(kam_dir)
    return record


__all__ = ["run", "SHARED_REL_PATHS"]