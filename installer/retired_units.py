"""Teardown for systemd units that belonged to retired KAM capabilities.

The ``fibo`` capability shipped three units:

* ``fibo-converge.service`` / ``fibo-converge.timer`` — a per-minute timer
  that ran the MT4 reader's converge pass.
* ``fibo-mt4-reader.service`` — the reader itself.

Those units were removed together with the capability, but a deployment that
installed the capability still has them armed.  The timer's ``ExecStart``
points at ``plugins/trade/fibo/converge_once.py``, a module that no longer
exists, so the timer keeps failing on every minute boundary and keeps the
host in a failed-unit state.

This module is idempotent and never raises: every systemctl failure degrades
to a recorded warning so an uninstall can never be blocked by systemd.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

# Units owned by the retired /fibo capability.
RETIRED_SYSTEMD_UNITS: List[str] = [
    "fibo-converge.service",
    "fibo-converge.timer",
    "fibo-mt4-reader.service",
]

# Units that are timers (and therefore need ``disable --now`` so the
# schedule is cancelled, not just the service stopped).
_RETIRED_TIMERS = {"fibo-converge.timer"}


def _systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def retire_retired_systemd_units(
    *,
    systemd_dir: Path,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Disable, stop and remove the units of retired capabilities.

    Never raises.  Returns a record describing what was (or would be) done.
    """
    record: Dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "systemd_dir": str(systemd_dir),
        "actions": [],
        "warnings": [],
    }
    if not str(systemd_dir).strip():
        record["actions"].append("skipped: empty systemd_dir")
        return record

    systemd_dir = Path(systemd_dir)
    removed_any = False
    for name in RETIRED_SYSTEMD_UNITS:
        path = systemd_dir / name
        unit_present = path.is_file()
        # A unit may also be installed outside systemd_dir (e.g. a distro
        # package unit path); ask systemd whether it knows the unit.
        known = False
        if _systemctl_available():
            try:
                proc = _run(["systemctl", "list-unit-files", name])
                known = proc.returncode == 0 and name in (proc.stdout or "")
            except Exception as exc:  # pragma: no cover - defensive
                record["warnings"].append(f"{name}: systemctl probe failed: {exc}")

        if not unit_present and not known:
            record["actions"].append(f"absent: {name}")
            continue

        if dry_run:
            verb = "disable --now" if name in _RETIRED_TIMERS else "stop"
            record["actions"].append(f"would {verb} / remove {name}")
            continue

        if _systemctl_available():
            try:
                if name in _RETIRED_TIMERS:
                    _run(["systemctl", "disable", "--now", name])
                    record["actions"].append(f"disable --now {name}")
                else:
                    _run(["systemctl", "stop", name])
                    record["actions"].append(f"stop {name}")
                _run(["systemctl", "reset-failed", name])
            except Exception as exc:  # pragma: no cover - defensive
                record["warnings"].append(f"{name}: systemctl teardown failed: {exc}")

        if path.is_file():
            try:
                path.unlink()
                removed_any = True
                record["actions"].append(f"removed {path}")
            except Exception as exc:
                record["warnings"].append(f"{name}: could not remove {path}: {exc}")

    if removed_any and _systemctl_available() and not dry_run:
        try:
            _run(["systemctl", "daemon-reload"])
            record["actions"].append("daemon-reload")
        except Exception as exc:  # pragma: no cover - defensive
            record["warnings"].append(f"daemon-reload failed: {exc}")

    return record


__all__ = ["RETIRED_SYSTEMD_UNITS", "retire_retired_systemd_units"]
