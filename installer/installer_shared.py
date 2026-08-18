"""Shared core installer for KAM.

Installed once whenever ANY capability is installed.

The shared core is:
  - the ``plugins/trade/agents/x_*_agent.py`` exchange agents
  - ``plugins/trade/canonical.py`` (canonical response models)
  - ``plugins/trade/agents/__init__.py`` (agent discovery)

These files live under the ``plugins/trade/`` plugin tree so Hermes'
existing discovery (``plugins.enabled``) keeps working, but they are
**SHARED** — they MUST exist before any capability can run.

Idempotency: each shared file is copied byte-for-byte; sha256 is checked
before copy so unchanged files are skipped.

No capability-specific files are touched by this module.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capabilities import (
    KNOWN_CAPABILITIES,
    SCHEMA_VERSION,
    capability_dir,
    resolve_hermes_home,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
SHARED_REL_PATHS = [
    Path("plugins") / "trade" / "__init__.py",
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


def install_shared(*, argv: List[str], hermes_home: Path, capabilities: List[str]) -> Dict[str, Any]:
    """Copy the SHARED core files into the Hermes installation.

    Returns a record suitable for ``by_capability[*]['shared']``.
    """
    # Locate hermes_root (the Hermes checkout containing plugins/trade/).
    # Convention: hermes_home is the .hermes data dir; hermes_root is the
    # source tree containing plugins/trade/. For now, install the shared
    # core files relative to hermes_home/../plugins/trade/agents/ and
    # hermes_home/../plugins/trade/canonical.py. This mirrors how the
    # proven monolithic installer writes them under <HERMES_ROOT>/plugins/.
    hermes_root_candidate = hermes_home.parent
    hermes_root = hermes_root_candidate  # ~/<hermes-home-parent>/plugins/...
    # Plugin target is hermes_root / "plugins" / "trade" / ...
    plugin_root = hermes_root / "plugins" / "trade"
    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "files": [],
        "ok": True,
    }
    for rel in SHARED_REL_PATHS:
        src = REPO_ROOT / rel
        dst = plugin_root / rel.relative_to(Path("plugins") / "trade")
        if not src.is_file():
            record["ok"] = False
            record["files"].append({"path": str(rel), "action": "missing-source"})
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        action = "copied"
        if dst.is_file() and _sha256_file(src) == _sha256_file(dst):
            action = "unchanged"
        else:
            shutil.copy2(src, dst)
        record["files"].append({
            "path": str(rel),
            "src_sha256": _sha256_file(src),
            "action": action,
        })
    return record


__all__ = ["install_shared", "SHARED_REL_PATHS"]