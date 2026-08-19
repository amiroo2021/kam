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

Does NOT install /trade files (TradeDesk, wizard.py). Reuses the SHARED
agents (already installed by the shared core).

The fibo.service is rendered and installed ONLY when ``--fibo`` is
passed (i.e., this capability). The systemd unit file is generated from
``installer/fibo.service.template``.

Takes EXPLICIT ``hermes_root`` and ``hermes_home``. The two are
independent. ``--dry-run`` mode reports actions without writing.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

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


def _render_fibo_unit(
    *, hermes_root: Path, hermes_home: Path, python_exe: Path
) -> str:
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


def run(
    *,
    argv: Sequence[str],
    hermes_root: Path,
    hermes_home: Path,
    shared: Dict[str, Any],
    dry_run: bool = False,
    systemd_dir: Optional[Path] = None,
    no_restart: bool = False,
    backup_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Install the /fibo capability. Idempotent.

    If ``dry_run`` is True, no bytes are written and no directory is
    created.
    """
    from fibo_unit import (
        DEFAULT_SYSTEMD_DIR,
        activate_fibo_service,
        install_fibo_service_unit,
        resolve_python_exe,
    )

    plugin_root = hermes_root / "plugins" / "trade"
    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "files": [],
        "ok": True,
        "dry_run": dry_run,
        "target_plugin_root": str(plugin_root),
    }
    for rel in FIBO_REL_PATHS:
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
                shutil.copy2(src, dst)
            entry["action"] = "copied" if not dry_run else "would-copy"
        record["files"].append(entry)
    # Ensure ~/.hermes/fibo/ exists (owned runtime folder).
    own_dir = capability_dir(hermes_home, "fibo")
    if dry_run:
        if not own_dir.is_dir():
            record.setdefault("actions", []).append(f"would-mkdir {own_dir}")
        else:
            record.setdefault("actions", []).append(f"keep-exists {own_dir}")
    else:
        own_dir.mkdir(parents=True, exist_ok=True)
    record["owned_dir"] = str(own_dir)

    # Render + install fibo.service (required for /fibo IPC on fresh install).
    sd = Path(systemd_dir) if systemd_dir is not None else DEFAULT_SYSTEMD_DIR
    py = resolve_python_exe(hermes_root)
    bk = backup_dir if backup_dir is not None else (hermes_home / "kam" / "backups")
    try:
        unit_rec = install_fibo_service_unit(
            hermes_root=hermes_root,
            hermes_home=hermes_home,
            systemd_dir=sd,
            dry_run=dry_run,
            backup_dir=None if dry_run else bk,
            python_exe=py,
        )
        record["service_unit"] = unit_rec
        act_rec = activate_fibo_service(
            systemd_dir=sd, dry_run=dry_run, no_restart=no_restart
        )
        record["service_activation"] = act_rec
    except Exception as exc:  # noqa: BLE001
        record["ok"] = False
        record["service_error"] = str(exc)
    record["service_template"] = str(FIBO_SERVICE_TEMPLATE_PATH)
    return record


__all__ = ["run", "FIBO_REL_PATHS", "FIBO_SERVICE_TEMPLATE_PATH", "_render_fibo_unit"]