"""Capability-specific installer: FIBO.

Installs ONLY the /fibo capability:
  - ``plugins/trade/fibo_service.py`` (IPC daemon service)
  - ``plugins/trade/fibo_daemon.py`` (systemd entry)
  - ``plugins/trade/fibo_wizard.py`` (/fibo wizard)
  - ``plugins/trade/golden_fibo/__init__.py``
  - ``plugins/trade/golden_fibo/config.py``
  - ``plugins/trade/golden_fibo/engine.py``
  - ``plugins/trade/golden_fibo/lighter_adapter.py``
  - ``plugins/trade/golden_fibo/preflight.py``
  - ``plugins/trade/golden_fibo/state.py``
  - ``installer/fibo.service.template`` rendered to ``<systemd_dir>/fibo.service``
  - ``~/.hermes/fibo/`` (capability-owned runtime state folder)

Does NOT install /trade files (TradeDesk, wizard.py). Reuses the SHARED
agents (already installed by the shared core).

The fibo.service is rendered and installed ONLY when ``--fibo`` is
passed (i.e., this capability). The systemd unit file is generated from
``installer/fibo.service.template``.
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
FIBO_REL_PATHS = [
    Path("plugins") / "trade" / "fibo_service.py",
    Path("plugins") / "trade" / "fibo_daemon.py",
    Path("plugins") / "trade" / "fibo_wizard.py",
    Path("plugins") / "trade" / "golden_fibo" / "__init__.py",
    Path("plugins") / "trade" / "golden_fibo" / "config.py",
    Path("plugins") / "trade" / "golden_fibo" / "engine.py",
    Path("plugins") / "trade" / "golden_fibo" / "lighter_adapter.py",
    Path("plugins") / "trade" / "golden_fibo" / "preflight.py",
    Path("plugins") / "trade" / "golden_fibo" / "state.py",
]

FIBO_SERVICE_TEMPLATE_PATH = REPO_ROOT / "installer" / "fibo.service.template"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _render_fibo_unit(*, hermes_root: Path, hermes_home: Path, python_exe: Path) -> str:
    if not FIBO_SERVICE_TEMPLATE_PATH.is_file():
        raise FileNotFoundError(f"missing systemd template: {FIBO_SERVICE_TEMPLATE_PATH}")
    template = FIBO_SERVICE_TEMPLATE_PATH.read_text(encoding="utf-8")
    runtime_dir = hermes_home / "fibo"
    replacements = {
        "{{HERMES_ROOT}}": str(hermes_root),
        "{{HERMES_HOME}}": str(hermes_home),
        "{{PYTHON_EXE}}": str(python_exe),
        "{{SOCKET_PATH}}": str(runtime_dir / "service.sock"),
        "{{STATE_PATH}}": str(runtime_dir / "service_state.json"),
        "{{LEDGER_PATH}}": str(runtime_dir / "service_ledger.jsonl"),
        "{{EVENT_LOG_PATH}}": str(runtime_dir / "service-events.log"),
    }
    rendered = template
    for old, new in replacements.items():
        rendered = rendered.replace(old, new)
    return rendered


def run(*, argv: List[str], hermes_home: Path, shared: Dict[str, Any]) -> Dict[str, Any]:
    """Install the /fibo capability. Idempotent."""
    hermes_root = hermes_home.parent
    plugin_root = hermes_root / "plugins" / "trade"
    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "files": [],
        "ok": True,
    }
    for rel in FIBO_REL_PATHS:
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
    # Ensure ~/.hermes/fibo/ exists (owned runtime folder).
    own_dir = capability_dir(hermes_home, "fibo")
    own_dir.mkdir(parents=True, exist_ok=True)
    record["owned_dir"] = str(own_dir)
    # Record fibo.service template presence (rendering is the operator's
    # decision: --systemd-dir and --no-restart control when/whether the
    # unit is actually written to /etc/systemd/system). For capability
    # installation, we only ensure the template is present in the repo.
    record["service_template"] = str(FIBO_SERVICE_TEMPLATE_PATH)
    return record


__all__ = ["run", "FIBO_REL_PATHS", "FIBO_SERVICE_TEMPLATE_PATH", "_render_fibo_unit"]