"""Capability-specific installer: FIBO (lightweight UI skeleton).

Installs ONLY the /fibo Telegram wizard skeleton and the
Telegram adapter seams that route ``/fibo``, ``fibo:`` callbacks,
and (when supported) /fibo free-text interception.

This module installs the shared agent layer only via the caller's
``shared`` record (produced by ``installer_shared.install_shared``);
this module itself owns ZERO shared files. The /trade installer is
responsible for ``wizard.py``; /fibo is responsible for
``fibo_wizard.py`` only.

It does NOT install:

* ``fibo.service`` (no systemd unit),
* ``fibo_daemon.py`` (no runtime daemon),
* ``fibo_service.py`` (no runtime state machine),
* ``golden_fibo/`` (no old strategy engine),
* ``~/.hermes/fibo/`` (no runtime directory yet — empty phase).

Future iterations that bring up a real Fibo strategy may extend this
module to also install those files. For now, the wizard is a static
placeholder.

Takes EXPLICIT ``hermes_root`` (the installed app tree) and
``hermes_home`` (the persistent state). The two are independent.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))


REPO_ROOT = Path(__file__).resolve().parent.parent

# Files owned exclusively by the /fibo capability.
# Wizard lives alongside the /trade wizard so the package stays a
# single Python import path under ``plugins.trade``.
# Phase 1 also ships the ``fibo/`` sub-package (Start Fibo sub-flow,
# MT4 Reader, snapshot/store/session/flow modules). Each file is
# listed explicitly so the install/uninstall capability mirrors
# without walking the tree.
FIBO_REL_PATHS = [
    Path("plugins") / "trade" / "fibo_wizard.py",
    Path("plugins") / "trade" / "fibo" / "__init__.py",
    Path("plugins") / "trade" / "fibo" / "_atomic.py",
    Path("plugins") / "trade" / "fibo" / "snapshot.py",
    Path("plugins") / "trade" / "fibo" / "store.py",
    Path("plugins") / "trade" / "fibo" / "session.py",
    Path("plugins") / "trade" / "fibo" / "flow.py",
    Path("plugins") / "trade" / "fibo" / "mt4_reader.py",
    # Phase 2.x additions: identity split (Phase 2.1), alias
    # memory + instrument translation (Phase 2.2), ranked candidate
    # discovery + price evidence (Phase 2.3).
    Path("plugins") / "trade" / "fibo" / "alias_memory.py",
    Path("plugins") / "trade" / "fibo" / "candidates.py",
    Path("plugins") / "trade" / "fibo" / "discovery.py",
    Path("plugins") / "trade" / "fibo" / "dryrun.py",
    Path("plugins") / "trade" / "fibo" / "reconciler.py",
    # Phase 2.8 — stateless target-convergence executor.
    Path("plugins") / "trade" / "fibo" / "executor.py",
    # Phase 2.9 — shadow-mode executor wiring (read-only).
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
    # from the deployed tree.
    Path("plugins") / "trade" / "agents" / "tests" / "__init__.py",
    Path("plugins") / "trade" / "agents" / "tests"
    / "test_x_ondoperps_market_price.py",
]


def run(
    *,
    argv: Sequence[str],
    hermes_root: Path,
    hermes_home: Path,
    shared: Dict[str, Any],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Install /fibo capability payload into *hermes_root*.

    The caller (dispatcher) has already invoked
    ``installer_shared.install_shared`` for the shared agent layer; we
    only copy fibo-specific files.
    """
    plugin_root = hermes_root / "plugins" / "trade"
    record: Dict[str, Any] = {
        "copied_files": [],
        "dry_run": dry_run,
        "ok": True,
    }
    for rel in FIBO_REL_PATHS:
        try:
            rel_under_plugin_trade = rel.relative_to(Path("plugins") / "trade")
        except ValueError:
            rel_under_plugin_trade = rel
        src = REPO_ROOT / rel
        dst = plugin_root / rel_under_plugin_trade
        if not src.is_file():
            record["ok"] = False
            record.setdefault("missing", []).append(str(rel))
            continue
        if dry_run:
            record["copied_files"].append({"path": str(rel), "action": "would-copy"})
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            record["copied_files"].append({"path": str(rel), "action": "copied"})

    # Phase 2.13.11 — install the Fibo-owned systemd units
    # (fibo-converge.service + fibo-converge.timer). These live in
    # ``kam/installer/systemd/`` and are copied to the target
    # systemd unit directory (``--systemd-dir``, default
    # ``/etc/systemd/system``). The install does NOT enable or start
    # the timer; that requires a separate operator action
    # (``systemctl enable --now fibo-converge.timer``). The units
    # are intentionally NOT installed if ``--systemd-dir`` is the
    # empty string (caller signals "skip systemd").
    #
    # Phase 2.13.22 — also install ``fibo-mt4-reader.service``.
    # The MT4 reader is a separate Fibo-owned daemon with its own
    # lifecycle. It MUST NOT be coupled to hermes-gateway.service
    # (the previous detached-launcher pattern was SIGKILLed during
    # a gateway restart, causing a 21m46s snapshot outage; this
    # dedicated unit eliminates that class of incident).
    #
    # After unit-file installation, the installer invokes
    # ``systemctl daemon-reload`` so systemd picks up the new
    # units. If the unit files were already present and identical,
    # the install is a no-op for the unit layer and the reload
    # is still safe (it just re-reads the existing unit files).
    # The install does NOT enable or start the timer; the operator
    # is responsible for that. The Fibo flock provides the
    # primary concurrency safety; the timer is a fixed-cadence
    # trigger.
    systemd_dir_str = ""
    try:
        # The dispatcher passes ``systemd_dir`` via ``shared`` (the
        # resolved Path from ``installer.py::main``).
        systemd_dir_str = str(shared.get("systemd_dir", "") or "")
    except Exception:  # noqa: BLE001
        systemd_dir_str = ""
    if systemd_dir_str:
        systemd_dir = Path(systemd_dir_str)
        record["systemd_dir"] = str(systemd_dir)
        record["systemd_units"] = []
        units_written = False
        for unit_name in (
            "fibo-converge.service", "fibo-converge.timer",
            "fibo-mt4-reader.service",
        ):
            src_unit = REPO_ROOT / "installer" / "systemd" / unit_name
            if not src_unit.is_file():
                record["ok"] = False
                record.setdefault("missing_units", []).append(unit_name)
                continue
            if dry_run:
                record["systemd_units"].append({
                    "unit": unit_name,
                    "action": "would-install",
                    "target": str(systemd_dir / unit_name),
                })
            else:
                systemd_dir.mkdir(parents=True, exist_ok=True)
                dst_unit = systemd_dir / unit_name
                dst_unit.write_text(
                    src_unit.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
                dst_unit.chmod(0o644)
                record["systemd_units"].append({
                    "unit": unit_name,
                    "action": "installed",
                    "target": str(dst_unit),
                })
                units_written = True
        if units_written and not dry_run:
            # Run ``systemctl daemon-reload`` so systemd picks up
            # the new unit files. If systemctl is not available
            # (e.g., a chroot/builder environment), the operator
            # must run daemon-reload manually. We log a warning
            # but do not abort the install.
            import shutil
            import subprocess
            if shutil.which("systemctl") is not None:
                try:
                    subprocess.run(
                        ["systemctl", "daemon-reload"],
                        check=False,
                        capture_output=True,
                        timeout=10,
                    )
                    record["daemon_reload"] = "ok"
                except Exception as exc:  # noqa: BLE001
                    record["daemon_reload"] = f"error: {exc}"
                # /fibo Start/Running menus require a live MT4 snapshot.
                # The reader is a Fibo-owned independent unit (must NOT
                # be tied to hermes-gateway). Enable + start it on
                # install so a fresh --fibo does not leave the wizard
                # stuck on "No MT4 data yet". Converge timer stays
                # operator-opt-in (not auto-started here).
                try:
                    en = subprocess.run(
                        ["systemctl", "enable", "--now", "fibo-mt4-reader.service"],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    record["mt4_reader"] = {
                        "action": "enable --now",
                        "returncode": int(en.returncode),
                        "stderr": (en.stderr or "")[-400:],
                    }
                except Exception as exc:  # noqa: BLE001
                    record["mt4_reader"] = {"action": "enable --now", "error": str(exc)}
            else:
                record["daemon_reload"] = (
                    "skipped: systemctl not on PATH; operator must "
                    "run 'systemctl daemon-reload' manually"
                )
                record["mt4_reader"] = {
                    "action": "skipped",
                    "reason": "systemctl not on PATH; run "
                    "'systemctl enable --now fibo-mt4-reader.service'",
                }

    return record


__all__ = ["run", "FIBO_REL_PATHS"]