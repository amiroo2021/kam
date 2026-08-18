"""Shared uninstaller.

Removes the SHARED core ONLY when no KAM capabilities remain installed.
The shared core consists of:
  - ``plugins/trade/agents/x_*_agent.py`` exchange agents
  - ``plugins/trade/agents/__init__.py``
  - ``plugins/trade/canonical.py``
  - ``~/.hermes/kam/`` authoritative manifest directory

The function ``run`` is a no-op when at least one capability is still
installed; this is enforced by the dispatcher.

NEVER touches ~/.hermes/trade/ or ~/.hermes/fibo/ — those belong to
the per-capability uninstallers.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capabilities import (
    KNOWN_CAPABILITIES,
    install_state_path,
    kam_root,
)


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
]


def run(*, argv, hermes_home: Path) -> Dict[str, Any]:
    """Remove shared core (idempotent; safe to call even when already removed)."""
    hermes_root = hermes_home.parent
    plugin_root = hermes_root / "plugins" / "trade"
    record: Dict[str, Any] = {"removed_files": [], "removed_dirs": []}
    for rel in SHARED_REL_PATHS:
        dst = plugin_root / rel.relative_to(Path("plugins") / "trade")
        if dst.is_file():
            dst.unlink()
            record["removed_files"].append(str(rel))
    # Remove the now-empty agents/ directory.
    agents_dir = plugin_root / "agents"
    if agents_dir.is_dir() and not any(agents_dir.iterdir()):
        agents_dir.rmdir()
        record["removed_dirs"].append(str(agents_dir))
    # kam/ directory.
    kam_dir = kam_root(hermes_home)
    if kam_dir.is_dir():
        shutil.rmtree(kam_dir)
        record["removed_dirs"].append(str(kam_dir))
    return record


__all__ = ["run", "SHARED_REL_PATHS"]