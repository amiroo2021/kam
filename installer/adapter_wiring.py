"""Capability-aware Telegram adapter wiring for the modular installer.

The modular installer historically copied plugin payload files and wrote
``install_state.json`` but never applied the Telegram adapter dispatch
seams. On a fresh Hermes tree that left ``/trade`` and ``/fibo`` as
unknown commands even though the feature modules were present.

This module is the single place that:

* applies capability-scoped adapter patches (deterministic, idempotent)
* removes capability-scoped adapter patches on uninstall
* enables the ``trade`` plugin in Hermes config (plugin API registration)
* verifies that required seams are present for installed capabilities

It reuses the proven ``patchspecs`` / ``kamlib.apply_patch`` machinery
from the monolithic installer — it does not invent a second patch format.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kamlib as K  # noqa: E402
import kamconfig as C  # noqa: E402
from patchspecs import (  # noqa: E402
    TELEGRAM_ADAPTER,
    fibo_adapter_specs,
    helper_specs,
    specs_for_capabilities,
    trade_adapter_specs,
)


def _python_exe(hermes_root: Path) -> Path:
    venv_py = hermes_root / "venv" / "bin" / "python"
    if venv_py.is_file() and os_access_x(venv_py):
        return venv_py
    return Path(sys.executable)


def os_access_x(path: Path) -> bool:
    import os

    return os.access(path, os.X_OK)


def apply_adapter_wiring(
    *,
    hermes_root: Path,
    hermes_home: Path,
    capabilities: Sequence[str],
    dry_run: bool = False,
    backup_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Apply Telegram adapter seams + enable plugin for *capabilities*.

    Idempotent: re-running leaves already-installed KAM markers untouched.
    """
    caps = [c for c in ("trade", "fibo") if c in {str(x).lower() for x in capabilities}]
    record: Dict[str, Any] = {
        "capabilities": caps,
        "dry_run": dry_run,
        "patches": [],
        "config": None,
        "ok": True,
    }
    if not caps:
        record["detail"] = "no trade/fibo capabilities requested"
        return record

    specs = specs_for_capabilities(caps, hermes_root)
    adapter_path = hermes_root / TELEGRAM_ADAPTER
    if not adapter_path.is_file():
        record["ok"] = False
        record["error"] = f"missing Telegram adapter: {adapter_path}"
        return record

    python_exe = _python_exe(hermes_root)
    if backup_dir is None and not dry_run:
        backup_dir = hermes_home / "kam" / "backups" / "adapter_wiring"
        backup_dir.mkdir(parents=True, exist_ok=True)

    by_file: Dict[Path, List] = {}
    for spec in specs:
        by_file.setdefault(spec.relative_path, []).append(spec)

    for rel, file_specs in by_file.items():
        target = hermes_root / rel
        if not target.is_file():
            record["ok"] = False
            record["patches"].append(
                {"path": str(rel), "action": "missing-target", "seam": None}
            )
            continue
        original = target.read_text(encoding="utf-8")
        try:
            before_hash = K.sha256_file(target)
        except Exception:
            before_hash = None
        text = original
        mutated = False
        for spec in file_specs:
            try:
                text, action, detail = K.apply_patch(text, spec)
            except K.InstallError as exc:
                record["ok"] = False
                record["patches"].append(
                    {
                        "path": str(rel),
                        "seam": spec.seam,
                        "action": "error",
                        "detail": str(exc),
                    }
                )
                return record
            if action == "patched":
                mutated = True
                if dry_run:
                    action = "would-patch"
            record["patches"].append(
                {
                    "path": str(rel),
                    "seam": spec.seam,
                    "action": action,
                    "detail": detail,
                    "sha256_before": before_hash,
                }
            )
        if mutated and not dry_run:
            assert backup_dir is not None
            bk = backup_dir / rel
            bk.parent.mkdir(parents=True, exist_ok=True)
            if not bk.exists():
                shutil.copy2(target, bk)
            tmp = target.with_suffix(target.suffix + ".kamtmp")
            tmp.write_text(text, encoding="utf-8")
            try:
                K.compile_check(python_exe, tmp)
            except K.InstallError as exc:
                tmp.unlink(missing_ok=True)
                record["ok"] = False
                record["error"] = str(exc)
                return record
            tmp.replace(target)

    # Plugin API registration requires plugins.enabled to include "trade"
    # (the package name). Both /trade and /fibo handlers live in that package.
    # Also raise Telegram BotCommand menu capacity so published slash menus
    # include /trade and /fibo (dispatch alone is not enough).
    try:
        config_path = C.find_config(hermes_home)
        if config_path is None:
            record["config"] = {"action": "skipped", "reason": "config-not-found"}
            record["command_menu"] = {
                "action": "skipped",
                "reason": "config-not-found",
                "minimum_max_commands": getattr(C, "MINIMUM_TELEGRAM_MENU_MAX", 61),
            }
        else:
            cfg_backup = backup_dir if backup_dir is not None else hermes_home / "kam" / "backups"
            if not dry_run:
                cfg_backup.mkdir(parents=True, exist_ok=True)
            if dry_run:
                record["config"] = {"action": "would-enable", "path": str(config_path)}
            else:
                record["config"] = C.enable_trade(config_path, cfg_backup, dry_run=False)
            try:
                record["command_menu"] = C.ensure_telegram_menu_capacity(
                    config_path,
                    None if dry_run else cfg_backup,
                    dry_run=dry_run,
                )
            except C.ConfigError as exc:
                record["command_menu"] = {"action": "skipped", "reason": str(exc)}
    except C.ConfigError as exc:
        # Menu enablement is best-effort; adapter seams still make routing work.
        record["config"] = {"action": "skipped", "reason": str(exc)}
        record["command_menu"] = {"action": "skipped", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001
        record["config"] = {"action": "skipped", "reason": f"unexpected: {exc}"}
        record["command_menu"] = {"action": "skipped", "reason": f"unexpected: {exc}"}

    return record


def remove_adapter_wiring(
    *,
    hermes_root: Path,
    capabilities: Sequence[str],
    remaining_capabilities: Sequence[str],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Remove adapter seams for capabilities being uninstalled.

    Shared helper is removed only when neither trade nor fibo remains.
    """
    removing = {str(c).lower() for c in capabilities}
    remaining = {str(c).lower() for c in remaining_capabilities}
    record: Dict[str, Any] = {
        "removing": sorted(removing),
        "remaining": sorted(remaining),
        "dry_run": dry_run,
        "patches": [],
        "ok": True,
    }

    specs: List = []
    if "trade" in removing:
        specs.extend(trade_adapter_specs())
    if "fibo" in removing:
        specs.extend(fibo_adapter_specs())
    if not (remaining & {"trade", "fibo"}):
        # Last capability gone — drop helper too.
        specs.extend(helper_specs(hermes_root))

    if not specs:
        return record

    by_file: Dict[Path, List] = {}
    for spec in specs:
        by_file.setdefault(spec.relative_path, []).append(spec)

    python_exe = _python_exe(hermes_root)
    for rel, file_specs in by_file.items():
        target = hermes_root / rel
        if not target.is_file():
            record["patches"].append(
                {"path": str(rel), "action": "missing-target"}
            )
            continue
        text = target.read_text(encoding="utf-8")
        mutated = False
        for spec in file_specs:
            new_text, removed = K.remove_patch(text, spec)
            action = "removed" if removed else "absent"
            if removed:
                mutated = True
                if dry_run:
                    action = "would-remove"
                else:
                    text = new_text
            record["patches"].append(
                {"path": str(rel), "seam": spec.seam, "action": action}
            )
        if mutated and not dry_run:
            tmp = target.with_suffix(target.suffix + ".kamtmp")
            tmp.write_text(text, encoding="utf-8")
            try:
                K.compile_check(python_exe, tmp)
            except K.InstallError as exc:
                tmp.unlink(missing_ok=True)
                record["ok"] = False
                record["error"] = str(exc)
                return record
            tmp.replace(target)

    return record


# Sentinels that MUST appear in the installed adapter for each capability.
TRADE_ADAPTER_SENTINELS = {
    "command": "from plugins.trade.wizard import handle_trade_command",
    "callback": "from plugins.trade.wizard import handle_trade_callback",
    "text": "from plugins.trade.wizard import handle_trade_text",
    "namespace": 'data.startswith("trade:")',
}
FIBO_ADAPTER_SENTINELS = {
    "command": "from plugins.trade.fibo_wizard import handle_fibo_command",
    "callback": "from plugins.trade.fibo_wizard import handle_fibo_callback",
    "text": "from plugins.trade.fibo_wizard import handle_fibo_text",
    "namespace": 'data.startswith("fibo:")',
}


def verify_adapter_wiring(
    *,
    hermes_root: Path,
    capabilities: Sequence[str],
) -> Tuple[bool, List[str]]:
    """Return (ok, messages) for required Telegram adapter seams."""
    caps = {str(c).lower() for c in capabilities}
    messages: List[str] = []
    ok = True
    adapter = hermes_root / TELEGRAM_ADAPTER
    if not adapter.is_file():
        return False, [f"missing Telegram adapter: {adapter}"]
    text = adapter.read_text(encoding="utf-8")

    def _check(label: str, sentinels: Dict[str, str], required: bool) -> None:
        nonlocal ok
        for kind, needle in sentinels.items():
            present = needle in text
            if required and not present:
                ok = False
                messages.append(f"[FAIL] {label} {kind} seam missing: {needle}")
            elif required and present:
                messages.append(f"[ok] {label} {kind} seam present")
            elif (not required) and present:
                # Soft note only when checking absence is desired by caller.
                messages.append(f"[note] {label} {kind} seam still present")
            else:
                messages.append(f"[ok] {label} {kind} seam absent (as expected)")

    if "trade" in caps:
        for kind, needle in TRADE_ADAPTER_SENTINELS.items():
            if needle in text:
                messages.append(f"[ok] trade {kind} seam present")
            else:
                ok = False
                messages.append(f"[FAIL] trade {kind} seam missing: {needle}")
    if "fibo" in caps:
        for kind, needle in FIBO_ADAPTER_SENTINELS.items():
            if needle in text:
                messages.append(f"[ok] fibo {kind} seam present")
            else:
                ok = False
                messages.append(f"[FAIL] fibo {kind} seam missing: {needle}")
    return ok, messages


def assert_capability_seams(
    text: str,
    *,
    trade: Optional[bool] = None,
    fibo: Optional[bool] = None,
) -> List[str]:
    """Return list of assertion failures for presence/absence expectations.

    ``trade=True`` requires trade seams; ``trade=False`` requires them absent;
    ``trade=None`` skips the check. Same for ``fibo``.
    """
    failures: List[str] = []

    def _req(name: str, sentinels: Dict[str, str], want: bool) -> None:
        for kind, needle in sentinels.items():
            present = needle in text
            if want and not present:
                failures.append(f"{name} {kind} missing")
            if (not want) and present:
                failures.append(f"{name} {kind} unexpectedly present")

    if trade is not None:
        _req("trade", TRADE_ADAPTER_SENTINELS, trade)
    if fibo is not None:
        _req("fibo", FIBO_ADAPTER_SENTINELS, fibo)
    return failures


def verify_command_menu_publication(
    *,
    hermes_home: Path,
    capabilities: Sequence[str],
) -> Tuple[bool, List[str]]:
    """Verify plugin enablement + Telegram menu capacity for installed caps."""
    caps = {str(c).lower() for c in capabilities}
    messages: List[str] = []
    ok = True
    if not (caps & {"trade", "fibo"}):
        return True, ["[ok] no trade/fibo caps; menu publication N/A"]

    config_path = C.find_config(hermes_home)
    if config_path is None:
        return False, [
            f"[FAIL] no config.yaml under {hermes_home}; cannot publish BotCommands"
        ]

    try:
        cfg = C.parse_config(config_path)
    except C.ConfigError as exc:
        return False, [f"[FAIL] cannot parse config: {exc}"]

    if not C.is_trade_enabled(cfg):
        ok = False
        messages.append(
            "[FAIL] plugins.enabled does not include 'trade' "
            "(plugin register() will not run)"
        )
    else:
        messages.append("[ok] plugins.enabled includes 'trade'")

    max_cmds = C.get_telegram_menu_max_commands(cfg)
    minimum = getattr(C, "MINIMUM_TELEGRAM_MENU_MAX", 61)
    if max_cmds < minimum:
        ok = False
        messages.append(
            f"[FAIL] platforms.telegram.extra.command_menu.max_commands={max_cmds} "
            f"< {minimum}; /trade and/or /fibo may be trimmed from BotCommand menu"
        )
    else:
        messages.append(
            f"[ok] Telegram command_menu.max_commands={max_cmds} (>= {minimum})"
        )
    return ok, messages


__all__ = [
    "TRADE_ADAPTER_SENTINELS",
    "FIBO_ADAPTER_SENTINELS",
    "apply_adapter_wiring",
    "remove_adapter_wiring",
    "verify_adapter_wiring",
    "verify_command_menu_publication",
    "assert_capability_seams",
]
