#!/usr/bin/env python3
"""Install the KAM /trade add-on into an existing Hermes installation.

Exchange-agnostic: this installer copies ``plugins/trade/`` verbatim and
applies the minimal set of approved integration patches. It never names an
exchange, never touches ``.env``, and never places a trade.
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
from patchspecs import all_specs  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(msg: str) -> None:
    say(f"==> {msg}")


def ok(msg: str) -> None:
    say(f"    [ok] {msg}")


def warn(msg: str) -> None:
    say(f"    [!!] {msg}")


def skip(msg: str) -> None:
    say(f"    [--] {msg}")


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

SECRET_PATTERNS = (
    ("telegram bot token", r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    ("ed25519/hex private key", r"\b[a-fA-F0-9]{64}\b"),
    ("openai-style key", r"\bsk-[A-Za-z0-9]{20,}\b"),
    ("mnemonic-like phrase", r"\b(?:[a-z]{3,8}\s+){11,}[a-z]{3,8}\b"),
)

# Public protocol constants that legitimately look like hex addresses.
SECRET_ALLOWLIST_SUBSTRINGS = (
    "VERIFYING_CONTRACT",
    "ROUTER_ADDRESS",
)


def scan_payload_for_secrets(payload_root: Path) -> List[str]:
    import re

    findings: List[str] = []
    for rel in K.iter_payload_files(payload_root):
        path = payload_root / rel
        if path.suffix not in {".py", ".yaml", ".yml", ".json", ".md", ".txt", ".sh"}:
            continue
        try:
            lines = path.read_text(errors="ignore").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, start=1):
            if any(token in line for token in SECRET_ALLOWLIST_SUBSTRINGS):
                continue
            for label, pattern in SECRET_PATTERNS:
                if re.search(pattern, line):
                    findings.append(f"{rel}:{lineno}: {label} (redacted)")
                    break
    return findings


def preflight(hermes_root: Path, payload_root: Path, python_exe: Path) -> None:
    step("Preflight")

    if not K.looks_like_hermes_root(hermes_root):
        raise K.InstallError(f"{hermes_root} is not a valid Hermes root")
    ok(f"Hermes root: {hermes_root}")

    if REPO_ROOT.resolve() == hermes_root.resolve():
        raise K.InstallError("Refusing to install the repository into itself")
    ok("Repository is not the Hermes root")

    if not payload_root.is_dir():
        raise K.InstallError(f"Payload missing: {payload_root}")
    files = K.iter_payload_files(payload_root)
    if not files:
        raise K.InstallError("Payload contains no files")
    ok(f"Payload: {len(files)} files under plugins/trade/")

    if not python_exe.exists():
        raise K.InstallError(f"Gateway interpreter not found: {python_exe}")
    ok(f"Gateway interpreter: {python_exe}")

    dest_parent = hermes_root / "plugins"
    if not dest_parent.is_dir():
        raise K.InstallError(f"Missing {dest_parent}")
    probe = dest_parent / ".kam-write-probe"
    try:
        probe.write_text("probe")
        probe.unlink()
    except OSError as exc:
        raise K.InstallError(f"{dest_parent} is not writable: {exc}") from exc
    ok(f"{dest_parent} is writable")

    for spec in all_specs():
        target = hermes_root / spec.relative_path
        if not target.is_file():
            raise K.InstallError(f"Integration target missing: {target}")
    ok("All integration targets present")

    findings = scan_payload_for_secrets(payload_root)
    if findings:
        for item in findings:
            warn(item)
        raise K.InstallError(
            f"{len(findings)} possible secret(s) in payload; refusing to install"
        )
    ok("Payload secret scan clean")

    # Compile the payload with the gateway interpreter (offline).
    proc = subprocess.run(
        [str(python_exe), "-m", "compileall", "-q", str(payload_root)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise K.InstallError(f"Payload failed to compile:\n{proc.stdout}\n{proc.stderr}")
    ok("Payload compiles")


# ---------------------------------------------------------------------------
# copy / patch
# ---------------------------------------------------------------------------

def copy_payload(
    payload_root: Path, hermes_root: Path, backup_dir: Path, dry_run: bool
) -> List[Dict[str, Any]]:
    step("Install plugin files")
    dest_root = hermes_root / K.PLUGIN_RELATIVE_ROOT
    records: List[Dict[str, Any]] = []
    changed = 0

    for rel in K.iter_payload_files(payload_root):
        src = payload_root / rel
        dst = dest_root / rel
        src_hash = K.sha256_file(src)
        before = K.sha256_file(dst) if dst.is_file() else None

        if before == src_hash:
            records.append({
                "path": str(K.PLUGIN_RELATIVE_ROOT / rel),
                "sha256_before": before,
                "sha256_after": src_hash,
                "action": "unchanged",
            })
            continue

        changed += 1
        if not dry_run:
            if dst.is_file():
                bk = backup_dir / K.PLUGIN_RELATIVE_ROOT / rel
                bk.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, bk)
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(dst.suffix + ".kamtmp")
            shutil.copy2(src, tmp)
            tmp.replace(dst)

        records.append({
            "path": str(K.PLUGIN_RELATIVE_ROOT / rel),
            "sha256_before": before,
            "sha256_after": src_hash,
            "action": "would-write" if dry_run else ("updated" if before else "created"),
        })

    verb = "would copy" if dry_run else "copied"
    ok(f"{len(records)} payload files ({changed} {verb}, {len(records) - changed} unchanged)")
    return records


def apply_patches(
    hermes_root: Path, backup_dir: Path, python_exe: Path, dry_run: bool
) -> List[K.PatchOutcome]:
    step("Apply integration patches")
    outcomes: List[K.PatchOutcome] = []

    by_file: Dict[Path, List] = {}
    for spec in all_specs():
        by_file.setdefault(spec.relative_path, []).append(spec)

    for rel, specs in by_file.items():
        target = hermes_root / rel
        original = target.read_text()
        before_hash = K.sha256_file(target)
        text = original
        mutated = False

        for spec in specs:
            text, action, detail = K.apply_patch(text, spec)
            if action == "patched":
                mutated = True
                if dry_run:
                    action = "would-patch"
            outcomes.append(K.PatchOutcome(
                seam=spec.seam,
                relative_path=str(rel),
                action=action,
                detail=detail,
                sha256_before=before_hash,
            ))
            symbol = {"patched": ok, "would-patch": ok,
                      "already-installed": skip, "native-present": skip}.get(action, say)
            symbol(f"{rel} [{spec.seam}] -> {action}: {detail}")

        if mutated and not dry_run:
            bk = backup_dir / rel
            bk.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, bk)

            tmp = target.with_suffix(target.suffix + ".kamtmp")
            tmp.write_text(text)
            try:
                K.compile_check(python_exe, tmp)
            except K.InstallError:
                tmp.unlink(missing_ok=True)
                raise
            tmp.replace(target)

            after_hash = K.sha256_file(target)
            for outcome in outcomes:
                if outcome.relative_path == str(rel) and outcome.action == "patched":
                    outcome.sha256_after = after_hash
            ok(f"{rel} patched and syntax-checked")
        elif mutated and dry_run:
            ok(f"{rel} would be patched (no write in dry-run)")

    return outcomes


def install_dependencies(python_exe: Path, dry_run: bool) -> Dict[str, Any]:
    step("Dependencies")
    req = REPO_ROOT / "installer" / "requirements.txt"
    if not req.is_file():
        skip("No requirements.txt; nothing to install")
        return {"requirements": None, "action": "skipped"}

    if dry_run:
        ok(f"Would run: {python_exe} -m pip install -r {req}")
        return {"requirements": str(req), "action": "would-install"}

    proc = subprocess.run(
        [str(python_exe), "-m", "pip", "install", "--no-input", "-r", str(req)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise K.InstallError(
            f"Dependency install failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
        )
    ok("Dependencies satisfied")
    return {"requirements": str(req), "action": "installed"}


def restart_gateway(dry_run: bool, no_restart: bool) -> Dict[str, Any]:
    step("Gateway restart")
    unit = K.find_service_unit()
    if unit is None:
        skip("No hermes-gateway.service found; restart manually if needed")
        return {"action": "skipped", "reason": "unit-not-found"}
    if no_restart:
        skip("--no-restart supplied; not restarting")
        return {"action": "skipped", "reason": "no-restart-flag"}
    if dry_run:
        ok("Would run: systemctl restart hermes-gateway")
        return {"action": "would-restart"}

    subprocess.run(["systemctl", "restart", "hermes-gateway"], check=True)
    proc = subprocess.run(
        ["systemctl", "is-active", "hermes-gateway"], capture_output=True, text=True
    )
    state = proc.stdout.strip()
    if state != "active":
        raise K.InstallError(f"Gateway did not return to active (state={state})")
    ok("Gateway active")
    return {"action": "restarted", "state": state}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="Install the KAM /trade add-on")
    parser.add_argument("--hermes-root", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-restart", action="store_true")
    parser.add_argument("--skip-deps", action="store_true")
    args = parser.parse_args(argv)

    say(f"KAM /trade installer v{K.INSTALLER_VERSION} (kam {K.KAM_VERSION})")
    if args.dry_run:
        say("DRY RUN - no changes will be made")
    say()

    try:
        hermes_root = K.resolve_hermes_root(args.hermes_root)
        python_exe = K.resolve_gateway_python(hermes_root)
        hermes_home = K.resolve_hermes_home()
        payload_root = REPO_ROOT / K.PLUGIN_RELATIVE_ROOT

        say(f"    hermes_root : {hermes_root}")
        say(f"    hermes_home : {hermes_home}")
        say(f"    interpreter : {python_exe}")
        say()

        preflight(hermes_root, payload_root, python_exe)
        say()

        stamp = K.backup_stamp()
        backup_dir = K.backups_root(hermes_root) / stamp
        if not args.dry_run:
            backup_dir.mkdir(parents=True, exist_ok=True)
            ok(f"Backup dir: {backup_dir}")
            say()

        copied = copy_payload(payload_root, hermes_root, backup_dir, args.dry_run)
        say()
        patches = apply_patches(hermes_root, backup_dir, python_exe, args.dry_run)
        say()

        deps = {"action": "skipped"} if args.skip_deps else install_dependencies(
            python_exe, args.dry_run
        )
        say()

        manifest = {
            "kam_version": K.KAM_VERSION,
            "installer_version": K.INSTALLER_VERSION,
            "timestamp": K.utc_timestamp(),
            "hermes_root": str(hermes_root),
            "hermes_home": str(hermes_home),
            "gateway_python": str(python_exe),
            "compatible_hermes": K.detect_hermes_version(hermes_root),
            "backup_dir": None if args.dry_run else str(backup_dir),
            "dry_run": args.dry_run,
            "copied_files": copied,
            "patched_files": [
                {
                    "seam": p.seam,
                    "path": p.relative_path,
                    "action": p.action,
                    "detail": p.detail,
                    "sha256_before": p.sha256_before,
                    "sha256_after": p.sha256_after,
                }
                for p in patches
            ],
            "dependencies": deps,
        }

        if not args.dry_run:
            K.write_manifest(K.manifest_path(REPO_ROOT), manifest)
            K.write_manifest(K.installed_manifest_path(hermes_root), manifest)
            ok(f"Manifest: {K.manifest_path(REPO_ROOT)}")
            ok(f"Manifest: {K.installed_manifest_path(hermes_root)}")
        else:
            ok("Manifest not written (dry run)")
        say()

        step("Verification")
        verifier = REPO_ROOT / "installer" / "verify_trade.py"
        proc = subprocess.run(
            [str(python_exe), str(verifier), "--hermes-root", str(hermes_root)]
            + (["--dry-run-source"] if args.dry_run else []),
            text=True,
        )
        if proc.returncode != 0:
            raise K.InstallError("Verification failed; not restarting the gateway")
        say()

        restart = restart_gateway(args.dry_run, args.no_restart)
        manifest["restart"] = restart
        if not args.dry_run:
            K.write_manifest(K.manifest_path(REPO_ROOT), manifest)
            K.write_manifest(K.installed_manifest_path(hermes_root), manifest)

        say()
        say("KAM /trade installation: PASS")
        if args.dry_run:
            say("(dry run - nothing was changed)")
        return 0

    except K.InstallError as exc:
        say()
        say(f"ERROR: {exc}")
        say("KAM /trade installation: FAIL")
        return 1
    except subprocess.CalledProcessError as exc:
        say()
        say(f"ERROR: command failed: {exc}")
        say("KAM /trade installation: FAIL")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
