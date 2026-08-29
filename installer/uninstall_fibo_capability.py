"""Capability-specific uninstaller: FIBO.

Removes the /fibo capability payload:

* ``plugins/trade/fibo_wizard.py`` (the wizard itself)
* ``plugins/trade/fibo/`` (the Phase 1 sub-package: Start Fibo
  sub-flow, MT4 Reader, snapshot/store/session modules, atomic
  write helper, package marker)

It does NOT:

* remove ``plugins/trade/__init__.py`` (still the /trade marker),
* remove any ``plugins/trade/agents/x_*_agent.py`` file (shared with
  /trade),
* remove any other /trade-owned file.

In Phase 1 there is no ``~/.hermes/fibo/`` runtime directory
created by the capability — the snapshot/state/registrations files
under ``~/.hermes/fibo/`` are produced by the Reader process (which
the user launches manually) and by the wizard as user data. The
uninstall does NOT delete those files — they are user data.

Phase 2.13.11 — also manages the Fibo-owned systemd units
(``fibo-converge.service`` and ``fibo-converge.timer``) idempotently:

  1. ``systemctl disable --now fibo-converge.timer`` (if it exists;
     missing/inactive/never-enabled is a no-op).
  2. ``systemctl stop fibo-converge.service`` (if active; missing/
     inactive is a no-op).
  3. ``systemctl reset-failed fibo-converge.{service,timer}`` (clears
     any leftover failed state in the unit state machine; idempotent).
  4. Remove the unit files.
  5. ``systemctl daemon-reload`` (only after a unit file is actually
     removed; if neither was present we skip the reload to avoid
     unnecessary churn).

The uninstall does NOT touch:

* ``~/.hermes/fibo/converge.lock`` (runtime state, harmless; will be
  cleaned up by the next converge_once or by the operator).
* ``~/.hermes/fibo/registrations.jsonl`` (user data).
* ``~/.hermes/fibo/instrument_aliases.json`` (user data).
* ``~/.hermes/fibo/mt4_snapshot.json`` (user data).
* ``~/.hermes/.env`` (user data; not Fibo-owned).
* Any other capability's units (telegram, gateway, etc.).

If ``systemctl`` is not available (e.g., a containerized test env),
the unit-management steps degrade gracefully: missing file =
silent skip, ``systemctl`` failure = warning + continue.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent

# Files owned exclusively by the /fibo capability.
# Must mirror ``FIBO_REL_PATHS`` in install_fibo_capability.py.
FIBO_REL_PATHS = [
    Path("plugins") / "trade" / "fibo_wizard.py",
    Path("plugins") / "trade" / "fibo" / "__init__.py",
    Path("plugins") / "trade" / "fibo" / "_atomic.py",
    Path("plugins") / "trade" / "fibo" / "snapshot.py",
    Path("plugins") / "trade" / "fibo" / "store.py",
    Path("plugins") / "trade" / "fibo" / "session.py",
    Path("plugins") / "trade" / "fibo" / "flow.py",
    Path("plugins") / "trade" / "fibo" / "mt4_reader.py",
    # Phase 2.x additions (must mirror install_fibo_capability).
    Path("plugins") / "trade" / "fibo" / "alias_memory.py",
    Path("plugins") / "trade" / "fibo" / "candidates.py",
    Path("plugins") / "trade" / "fibo" / "discovery.py",
    Path("plugins") / "trade" / "fibo" / "dryrun.py",
    Path("plugins") / "trade" / "fibo" / "reconciler.py",
    # Phase 2.8 — stateless target-convergence executor.
    Path("plugins") / "trade" / "fibo" / "executor.py",
    # Phase 2.8 — shadow convergence.
    Path("plugins") / "trade" / "fibo" / "shadow.py",
    # Phase 2.10 — controlled live target convergence (allowlist-gated).
    Path("plugins") / "trade" / "fibo" / "live.py",
    # Phase 2.11 — autonomous convergence script (invoked by the
    # gateway's cron ticker AND/OR by a Fibo-owned systemd timer).
    Path("plugins") / "trade" / "fibo" / "converge_once.py",
    # Phase 2.13.11 — Fibo-owned fcntl.flock singleton lock. Ensures
    # only ONE local converge_once enters TradeDesk at a time,
    # regardless of launcher (systemd timer, manual run,
    # accidental second shell, old gateway cron).
    Path("plugins") / "trade" / "fibo" / "singleton_lock.py",
    # Phase 2.3 agent-side regression tests live under
    # plugins/trade/agents/tests. They are pure unit tests and are
    # copied alongside the agent so the verifier can import them
    # without an installed package tree.
    Path("plugins") / "trade" / "agents" / "tests" / "test_fibo_skeleton.py",
    Path("plugins") / "trade" / "agents" / "tests" /
        "test_x_ondoperps_canonical_identity.py",
    Path("plugins") / "trade" / "agents" / "tests" /
        "test_x_ondoperps_market_price.py",
]

# Phase 2.13.11 — Fibo-owned systemd unit names.
# Phase 2.13.22 — added fibo-mt4-reader.service.
SYSTEMD_UNITS = (
    "fibo-converge.service", "fibo-converge.timer",
    "fibo-mt4-reader.service",
)


def _systemctl_available() -> bool:
    """Return True iff ``systemctl`` is on PATH.

    In containerized test environments (no systemd), the
    uninstall must NOT fail. We treat missing ``systemctl`` as
    a signal to skip unit management entirely.
    """
    return shutil.which("systemctl") is not None


def _run_systemctl(args: Sequence[str], timeout: float = 10.0) -> Dict[str, Any]:
    """Run ``systemctl <args>`` and capture outcome.

    Returns a dict with keys: rc (int), stdout, stderr, error
    (None on success, str on failure). Never raises.
    """
    cmd = ["systemctl", *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout,
        )
        return {
            "rc": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "error": None,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "rc": -1,
            "stdout": "",
            "stderr": str(exc),
            "error": "timeout",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "rc": -1,
            "stdout": "",
            "stderr": str(exc),
            "error": type(exc).__name__,
        }


def _unit_installed(unit_name: str) -> bool:
    """Check whether a systemd unit is installed (unit file present
    on disk). Returns False on any error (e.g., systemctl not
    available).
    """
    res = _run_systemctl(["cat", unit_name])
    if res["rc"] == 0:
        return True
    # ``systemctl cat`` exits 1 with stderr "Failed to ..." when
    # the unit is unknown. Anything else (e.g., no systemd) is
    # also treated as "not installed" for safety.
    return False


def _unit_active(unit_name: str) -> bool:
    """Return True iff the unit is currently in 'active' state."""
    res = _run_systemctl(["is-active", unit_name])
    return res["rc"] == 0 and res["stdout"].strip() == "active"


def _unit_enabled(unit_name: str) -> bool:
    """Return True iff the unit is currently enabled."""
    res = _run_systemctl(["is-enabled", unit_name])
    return res["rc"] == 0 and res["stdout"].strip() == "enabled"


def _disable_and_stop_unit(unit_name: str, record: Dict[str, Any]) -> None:
    """Idempotently disable and stop a systemd unit.

    ``systemctl disable`` exits 0 if the unit is now disabled
    (whether it was enabled before or already disabled). It exits
    non-zero only if the unit is not installed.

    ``systemctl stop`` exits 0 if the unit is now inactive
    (whether it was active before or already inactive). It exits
    non-zero only if the unit is not installed.

    Both commands are run with a hard 10-second timeout and
    never raise; failures are recorded in ``record`` and do not
    abort the uninstall.
    """
    # 1. Disable the unit (timer or service).
    res = _run_systemctl(["disable", unit_name])
    record.setdefault("disable_results", {})[unit_name] = {
        "rc": res["rc"],
        "stderr": res["stderr"].strip()[:200] if res["stderr"] else "",
    }
    # 2. If it's a timer, also pass --now to stop any active
    # activations immediately. For services, use ``stop`` separately.
    if unit_name.endswith(".timer"):
        res = _run_systemctl(["disable", "--now", unit_name])
        record.setdefault("disable_now_results", {})[unit_name] = {
            "rc": res["rc"],
            "stderr": res["stderr"].strip()[:200] if res["stderr"] else "",
        }
    else:
        res = _run_systemctl(["stop", unit_name])
        record.setdefault("stop_results", {})[unit_name] = {
            "rc": res["rc"],
            "stderr": res["stderr"].strip()[:200] if res["stderr"] else "",
        }


def _reset_failed_units(unit_names: Sequence[str], record: Dict[str, Any]) -> None:
    """Call ``systemctl reset-failed`` on each unit. Idempotent."""
    for unit_name in unit_names:
        res = _run_systemctl(["reset-failed", unit_name])
        record.setdefault("reset_failed_results", {})[unit_name] = {
            "rc": res["rc"],
            "stderr": res["stderr"].strip()[:200] if res["stderr"] else "",
        }


def _daemon_reload(record: Dict[str, Any]) -> None:
    """Run ``systemctl daemon-reload``. Idempotent."""
    res = _run_systemctl(["daemon-reload"])
    record["daemon_reload"] = {
        "rc": res["rc"],
        "stderr": res["stderr"].strip()[:200] if res["stderr"] else "",
    }


def _remove_unit_files(systemd_dir: Path, record: Dict[str, Any],
                      dry_run: bool) -> bool:
    """Remove the systemd unit FILES from systemd_dir.

    Returns True iff at least one file was removed. The caller
    uses this to decide whether a daemon-reload is needed.
    """
    any_removed = False
    for unit_name in SYSTEMD_UNITS:
        unit_path = systemd_dir / unit_name
        if unit_path.is_file():
            record.setdefault("removed_unit_files", []).append({
                "unit": unit_name,
                "target": str(unit_path),
            })
            if not dry_run:
                try:
                    os.remove(unit_path)
                except OSError as exc:
                    record.setdefault("unit_removal_errors", []).append(
                        {"unit": unit_name, "error": str(exc)}
                    )
                    continue
            any_removed = True
    return any_removed


def run(
    *,
    argv: Sequence[str],  # noqa: ARG001
    hermes_root: Path,
    hermes_home: Path,  # noqa: ARG001
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Uninstall the /fibo capability payload.

    Returns a record listing the removed files. Does NOT remove
    ``~/.hermes/fibo/`` (it is the user's runtime state, owned by
    the Reader process and the wizard; it is preserved across
    uninstall).

    Phase 2.13.11 — also manages the Fibo-owned systemd units
    idempotently: disable the timer (and stop the service), reset
    failed state, remove the unit files, and reload systemd. The
    function is safe to invoke when:
      - systemd is not available (graceful skip with a warning)
      - the units are not installed (idempotent no-op)
      - the units are already disabled (idempotent)
      - the units are inactive (idempotent)
    The uninstall does NOT touch user data, other capabilities,
    or the lock file.
    """
    plugin_root = hermes_root / "plugins" / "trade"
    record: Dict[str, Any] = {
        "removed_files": [],
        "removed_dirs": [],
        "systemd_units": {},
        "dry_run": dry_run,
    }

    # ---- Remove the fibo plugin files ----
    for rel in FIBO_REL_PATHS:
        try:
            rel_under_plugin_trade = rel.relative_to(Path("plugins") / "trade")
        except ValueError:
            rel_under_plugin_trade = rel
        dst = plugin_root / rel_under_plugin_trade
        if dst.is_file():
            record["removed_files"].append(str(rel))
            if not dry_run:
                dst.unlink()

    # ---- Manage the systemd units (idempotent) ----
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--systemd-dir", default=None)
    parsed, _ = parser.parse_known_args(list(argv) if argv else [])
    systemd_dir_str = parsed.systemd_dir or "/etc/systemd/system"
    systemd_dir = Path(systemd_dir_str)
    record["systemd_dir"] = str(systemd_dir)

    if not _systemctl_available():
        # No systemd (e.g., containerized test env). Just remove
        # the unit files; skip disable/stop/reload.
        record["systemctl_available"] = False
        record["note"] = (
            "systemctl not available; only unit files were removed "
            "(no disable/stop/reload performed). This is expected in "
            "containerized test environments."
        )
        any_removed = _remove_unit_files(systemd_dir, record, dry_run)
        return record
    record["systemctl_available"] = True

    # 1. Disable and stop each unit (idempotent).
    #    Skip units that are not installed (systemctl returns
    #    non-zero for ``disable``/``stop`` on unknown units; we
    #    detect via ``_unit_installed`` first).
    for unit_name in SYSTEMD_UNITS:
        if not _unit_installed(unit_name):
            record.setdefault("skipped_disabled", []).append(unit_name)
            continue
        _disable_and_stop_unit(unit_name, record)

    # 2. reset-failed for any unit that may have been in failed
    #    state. Safe even if not in failed state.
    _reset_failed_units(SYSTEMD_UNITS, record)

    # 3. Remove the unit files. Only call daemon-reload if at
    #    least one file was actually removed (avoiding
    #    unnecessary reloads for idempotent re-runs).
    any_removed = _remove_unit_files(systemd_dir, record, dry_run)
    if any_removed and not dry_run:
        _daemon_reload(record)
    elif any_removed and dry_run:
        record["would_daemon_reload"] = True

    return record


__all__ = ["run", "FIBO_REL_PATHS", "SYSTEMD_UNITS"]
