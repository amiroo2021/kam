"""Capability-specific installer: TRADE.

Installs ONLY the /trade capability:
  - ``plugins/trade/wizard.py`` (/trade wizard)

Takes EXPLICIT ``hermes_root`` (the installed app tree) and
``hermes_home`` (the persistent state). The two are independent.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capabilities import (
    SCHEMA_VERSION,
    capability_dir,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
TRADE_REL_PATHS = [
    Path("plugins") / "trade" / "wizard.py",
]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def run(
    *,
    argv: Sequence[str],
    hermes_root: Path,
    hermes_home: Path,
    shared: Dict[str, Any],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Install the /trade capability. Idempotent.

    If ``dry_run`` is True, no bytes are written and no directory is
    created.
    """
    plugin_root = hermes_root / "plugins" / "trade"
    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "files": [],
        "ok": True,
        "dry_run": dry_run,
        "target_plugin_root": str(plugin_root),
    }
    for rel in TRADE_REL_PATHS:
        src = REPO_ROOT / rel
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
                import shutil
                shutil.copy2(src, dst)
            entry["action"] = "copied" if not dry_run else "would-copy"
        record["files"].append(entry)
    # Ensure ~/.hermes/trade/ exists (owned state folder).
    own_dir = capability_dir(hermes_home, "trade")
    if dry_run:
        if not own_dir.is_dir():
            record.setdefault("actions", []).append(f"would-mkdir {own_dir}")
        else:
            record.setdefault("actions", []).append(f"keep-exists {own_dir}")
    else:
        own_dir.mkdir(parents=True, exist_ok=True)
    record["owned_dir"] = str(own_dir)
    return record


__all__ = ["run", "TRADE_REL_PATHS"]