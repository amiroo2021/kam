#!/usr/bin/env python3
"""Uninstall the KAM /trade add-on.

Removes only add-on-owned files and only the marked KAM blocks from shared
Hermes files. Never deletes ``.env``, credentials, unrelated plugins, or
dependencies. Idempotent.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kamlib as K  # noqa: E402
import kamconfig as C  # noqa: E402
from patchspecs import all_specs, legacy_commands_specs  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

NEVER_DELETE_NAMES = {".env", "auth.json"}


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    say(f"==> {msg}")


def ok(msg: str) -> None:
    say(f"    [ok] {msg}")


def skip(msg: str) -> None:
    say(f"    [--] {msg}")


def latest_backup(hermes_root: Path) -> Path | None:
    root = K.backups_root(hermes_root)
    if not root.is_dir():
        return None
    stamps = sorted(
        (p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True
    )
    return stamps[0] if stamps else None


def remove_files(
    hermes_root: Path, manifest: Dict[str, Any] | None, dry_run: bool
) -> List[str]:
    step("Remove plugin files")
    removed: List[str] = []

    if manifest and manifest.get("copied_files"):
        targets = [entry["path"] for entry in manifest["copied_files"]]
        source = "manifest"
    else:
        trade_root = hermes_root / K.PLUGIN_RELATIVE_ROOT
        if not trade_root.is_dir():
            skip("No installed plugin directory found")
            return removed
        targets = [
            str(K.PLUGIN_RELATIVE_ROOT / rel) for rel in K.iter_payload_files(trade_root)
        ]
        source = "filesystem scan (no manifest)"

    ok(f"Using {source}: {len(targets)} candidate files")

    for rel in targets:
        path = hermes_root / rel
        if path.name in NEVER_DELETE_NAMES:
            skip(f"protected, not removing: {rel}")
            continue
        if not path.exists():
            continue
        if not dry_run:
            path.unlink()
        removed.append(rel)

    # Drop now-empty directories inside plugins/trade only.
    trade_root = hermes_root / K.PLUGIN_RELATIVE_ROOT
    if trade_root.is_dir() and not dry_run:
        for directory in sorted(
            (p for p in trade_root.rglob("*") if p.is_dir()),
            key=lambda p: len(p.parts), reverse=True,
        ):
            if directory.name in K.COPY_EXCLUDE_DIR_NAMES:
                shutil.rmtree(directory, ignore_errors=True)
                continue
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            trade_root.rmdir()
            ok(f"Removed {K.PLUGIN_RELATIVE_ROOT}")
        except OSError:
            skip(f"{K.PLUGIN_RELATIVE_ROOT} not empty; left in place")

    verb = "would remove" if dry_run else "removed"
    ok(f"{len(removed)} files {verb}")
    return removed


def unpatch(
    hermes_root: Path, python_exe: Path, dry_run: bool, use_backup: bool
) -> List[Dict[str, Any]]:
    step("Remove integration patches")
    outcomes: List[Dict[str, Any]] = []

    by_file: Dict[Path, List] = {}
    # Include the retired commands.py spec so a block left by an older KAM
    # version is still found and cleanly removed.
    for spec in list(all_specs(hermes_root)) + list(legacy_commands_specs()):
        by_file.setdefault(spec.relative_path, []).append(spec)

    backup = latest_backup(hermes_root) if use_backup else None

    for rel, specs in by_file.items():
        target = hermes_root / rel
        if not target.is_file():
            skip(f"{rel} not present")
            continue

        text = target.read_text()
        mutated = False

        for spec in specs:
            text, removed = K.remove_patch(text, spec)
            if removed:
                mutated = True
                ok(f"{rel} [{spec.seam}] -> marker block removed")
                outcomes.append({"seam": spec.seam, "path": str(rel), "action": "unpatched"})
            elif spec.native_sentinel in text:
                skip(f"{rel} [{spec.seam}] -> native/unmarked, left untouched")
                outcomes.append({"seam": spec.seam, "path": str(rel), "action": "left-native"})
            else:
                skip(f"{rel} [{spec.seam}] -> not present")
                outcomes.append({"seam": spec.seam, "path": str(rel), "action": "absent"})

        if mutated and not dry_run:
            tmp = target.with_suffix(target.suffix + ".kamtmp")
            tmp.write_text(text)
            try:
                K.compile_check(python_exe, tmp)
            except K.InstallError:
                tmp.unlink(missing_ok=True)
                if backup and (backup / rel).is_file():
                    shutil.copy2(backup / rel, target)
                    ok(f"{rel} restored from backup after failed unpatch")
                    continue
                raise
            tmp.replace(target)
            ok(f"{rel} unpatched and syntax-checked")

    return outcomes


def revert_config(
    manifest: Dict[str, Any] | None, dry_run: bool
) -> Dict[str, Any]:
    """Remove the ``trade`` entry KAM added -- and only that.

    If the manifest records that ``trade`` was already enabled before KAM was
    installed, the entry is user-owned and is left alone.
    """
    step("Revert Hermes config")
    record = (manifest or {}).get("config") or {}
    config_path_text = record.get("path")

    if not config_path_text:
        skip("No config change recorded in manifest; nothing to revert")
        return {"action": "skipped", "reason": "not-recorded"}

    config_path = Path(config_path_text)
    if not config_path.is_file():
        skip(f"{config_path} no longer present")
        return {"action": "skipped", "reason": "config-missing"}

    was_already = bool(record.get("trade_was_already_enabled"))
    try:
        result = C.disable_trade(
            config_path,
            was_already_enabled=was_already,
            backup_dir=None,
            dry_run=dry_run,
        )
    except C.ConfigError as exc:
        say(f"    [!!] Config not reverted: {exc}")
        return {"action": "failed", "reason": str(exc)}

    if result["action"] == "preserved-user-owned":
        skip("trade was enabled before KAM installed it; left enabled")
    elif result["action"] in ("removed", "would-removed"):
        ok(f"trade removed from plugins.enabled ({config_path})")
        if result.get("other_plugins_preserved"):
            ok(f"preserved {len(result['other_plugins_preserved'])} other plugin(s)")
    else:
        skip(f"config: {result['action']}")
    return result


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Uninstall the KAM /trade add-on")
    parser.add_argument("--hermes-root", default=None)
    parser.add_argument("--systemd-dir", default="/etc/systemd/system")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-restart", action="store_true")
    parser.add_argument(
        "--purge-backups", action="store_true",
        help="Also delete the .kam-trade state directory (off by default)",
    )
    args = parser.parse_args(argv)

    say(f"KAM /trade uninstaller v{K.INSTALLER_VERSION}")
    if args.dry_run:
        say("DRY RUN - no changes will be made")
    say()

    try:
        hermes_root = K.resolve_hermes_root(args.hermes_root)
        python_exe = K.resolve_gateway_python(hermes_root)
        manifest_file = K.find_manifest(hermes_root)
        manifest = K.read_manifest(manifest_file) if manifest_file else None

        say(f"    hermes_root : {hermes_root}")
        say(f"    interpreter : {python_exe}")
        say(f"    manifest    : {'found' if manifest else 'not found (fallback scan)'}")
        say()

        remove_files(hermes_root, manifest, args.dry_run)
        say()
        unpatch(hermes_root, python_exe, args.dry_run, use_backup=True)
        say()
        revert_config(manifest, args.dry_run)
        say()

        step("Post-uninstall check")
        trade_pkg = hermes_root / K.PLUGIN_RELATIVE_ROOT / "wizard.py"
        if trade_pkg.exists() and not args.dry_run:
            say(f"    [!!] {trade_pkg} still present")
        else:
            ok("/trade wizard no longer installed")

        if args.purge_backups and not args.dry_run:
            shutil.rmtree(K.state_dir(hermes_root), ignore_errors=True)
            ok("Backups purged")
        else:
            skip("Backups preserved")
        say()

        step("Gateway restart")
        unit = K.find_service_unit()
        if unit is None:
            skip("No hermes-gateway.service found")
        elif args.no_restart:
            skip("--no-restart supplied")
        elif args.dry_run:
            ok("Would run: systemctl restart hermes-gateway")
        else:
            subprocess.run(["systemctl", "restart", "hermes-gateway"], check=True)
            ok("Gateway restarted")

        say()
        say("KAM /trade uninstall: PASS")
        if args.dry_run:
            say("(dry run - nothing was changed)")
        return 0

    except K.InstallError as exc:
        say()
        say(f"ERROR: {exc}")
        say("KAM /trade uninstall: FAIL")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
