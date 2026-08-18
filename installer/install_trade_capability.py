"""Capability-specific installer: TRADE.

Installs ONLY the /trade capability:
  - ``plugins/trade/tradedesk.py`` (TradeDesk dispatcher)
  - ``plugins/trade/wizard.py`` (/trade wizard)
  - ``plugins/trade/__init__.py`` (plugin marker; capability-aware registration)
  - Trade-specific patches (the ``trade:`` and ``/trade`` seams in
    ``plugins/platforms/telegram/adapter.py`` and the slash-command menu
    registration)
  - ``~/.hermes/trade/`` (capability-owned state folder)

Does NOT install:
  - fibo files (golden_fibo/, fibo_service.py, fibo_daemon.py, fibo_wizard.py)
  - fibo.service
  - fibo runtime state (~/.hermes/fibo/)

Does NOT touch fibo.service or fibo files even if they exist (Decision 3).
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capabilities import (
    SCHEMA_VERSION,
    capability_dir,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
TRADE_REL_PATHS = [
    Path("plugins") / "trade" / "__init__.py",
    Path("plugins") / "trade" / "tradedesk.py",
    Path("plugins") / "trade" / "wizard.py",
]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def run(*, argv: List[str], hermes_home: Path, shared: Dict[str, Any]) -> Dict[str, Any]:
    """Install the /trade capability. Idempotent."""
    hermes_root = hermes_home.parent
    plugin_root = hermes_root / "plugins" / "trade"
    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "files": [],
        "ok": True,
    }
    for rel in TRADE_REL_PATHS:
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
    # Ensure ~/.hermes/trade/ exists.
    own_dir = capability_dir(hermes_home, "trade")
    own_dir.mkdir(parents=True, exist_ok=True)
    record["owned_dir"] = str(own_dir)
    return record


__all__ = ["run", "TRADE_REL_PATHS"]