#!/usr/bin/env python3
"""Install the KAM /trade add-on into an existing Hermes installation.

Exchange-agnostic: this installer copies ``plugins/trade/`` verbatim and
applies the minimal set of approved integration patches. It never names an
exchange, never touches ``.env``, and never places a trade.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from packaging.requirements import Requirement
from packaging.version import InvalidVersion, Version

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kamlib as K  # noqa: E402
import kamconfig as C  # noqa: E402
from patchspecs import all_specs  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPENDENCY_MANIFEST_PATH = REPO_ROOT / "installer" / "sdk_dependencies.json"


def _normalize_dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name).strip()).lower()


def _pip_list_versions(python_exe: Path) -> Dict[str, str]:
    proc = subprocess.run(
        [str(python_exe), "-m", "pip", "list", "--format=json"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise K.InstallError(
            f"Package snapshot failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
        )
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise K.InstallError(f"Package snapshot was not valid JSON: {exc}") from exc
    out: Dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        version = row.get("version")
        if name and version:
            out[_normalize_dist_name(str(name))] = str(version)
    return out


def load_dependency_manifest(manifest_path: Path | None = None) -> Dict[str, Any]:
    path = manifest_path or DEPENDENCY_MANIFEST_PATH
    if not path.is_file():
        raise K.InstallError(f"Dependency manifest missing: {path}")
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise K.InstallError(f"Dependency manifest is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise K.InstallError("Dependency manifest root must be an object")
    if payload.get("schema_version") != 1:
        raise K.InstallError(
            f"Unsupported dependency manifest schema_version: {payload.get('schema_version')!r}"
        )
    exchanges = payload.get("exchanges")
    if not isinstance(exchanges, list):
        raise K.InstallError("Dependency manifest must contain exchanges[]")
    return payload


def iter_sdk_dependencies(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    exchanges = manifest.get("exchanges")
    if not isinstance(exchanges, list):
        raise K.InstallError("Dependency manifest must contain exchanges[]")
    for exchange_entry in exchanges:
        if not isinstance(exchange_entry, dict):
            raise K.InstallError("Each exchange manifest entry must be an object")
        exchange_name = str(exchange_entry.get("exchange") or "").strip()
        if not exchange_name:
            raise K.InstallError("Exchange manifest entry missing exchange name")
        sdks = exchange_entry.get("sdks")
        if not isinstance(sdks, list) or not sdks:
            raise K.InstallError(f"Exchange '{exchange_name}' must declare at least one sdk")
        for sdk in sdks:
            if not isinstance(sdk, dict):
                raise K.InstallError(f"Exchange '{exchange_name}' has a non-object sdk entry")
            package = str(sdk.get("package") or "").strip()
            version = str(sdk.get("version") or "").strip()
            import_probe = str(sdk.get("import_probe") or "").strip()
            install_args = sdk.get("install_args") or []
            runtime_dependencies = sdk.get("runtime_dependencies") or []
            if not package or not version or not import_probe:
                raise K.InstallError(
                    f"Exchange '{exchange_name}' sdk entry must declare package, version, and import_probe"
                )
            if not isinstance(install_args, list) or any(not isinstance(arg, str) for arg in install_args):
                raise K.InstallError(
                    f"Exchange '{exchange_name}' sdk '{package}' install_args must be a list of strings"
                )
            if not isinstance(runtime_dependencies, list):
                raise K.InstallError(
                    f"Exchange '{exchange_name}' sdk '{package}' runtime_dependencies must be a list"
                )
            normalized_runtime_dependencies = [
                _normalize_runtime_dependency(exchange_name, package, dep)
                for dep in runtime_dependencies
            ]
            record = dict(sdk)
            record["exchange"] = exchange_name
            record["package"] = package
            record["version"] = version
            record["import_probe"] = import_probe
            record["install_args"] = [str(arg) for arg in install_args]
            record["runtime_dependencies"] = normalized_runtime_dependencies
            records.append(record)
    return records


def _normalize_runtime_dependency(
    exchange_name: str,
    sdk_package: str,
    dep: Any,
) -> Dict[str, Any]:
    if not isinstance(dep, dict):
        raise K.InstallError(
            f"Exchange '{exchange_name}' sdk '{sdk_package}' has a non-object runtime dependency entry"
        )
    package = str(dep.get("package") or "").strip()
    version = str(dep.get("version") or "").strip()
    version_spec = str(dep.get("version_spec") or "").strip()
    install_args = dep.get("install_args") or []
    if not package:
        raise K.InstallError(
            f"Exchange '{exchange_name}' sdk '{sdk_package}' runtime dependency missing package"
        )
    if version and version_spec:
        raise K.InstallError(
            f"Exchange '{exchange_name}' sdk '{sdk_package}' runtime dependency '{package}' cannot declare both version and version_spec"
        )
    if not isinstance(install_args, list) or any(not isinstance(arg, str) for arg in install_args):
        raise K.InstallError(
            f"Exchange '{exchange_name}' sdk '{sdk_package}' runtime dependency '{package}' install_args must be a list of strings"
        )
    record = dict(dep)
    record["package"] = package
    record["version"] = version or None
    record["version_spec"] = version_spec or None
    record["install_args"] = [str(arg) for arg in install_args]
    record["install_if_missing_only"] = bool(dep.get("install_if_missing_only", True))
    return record


def _dependency_requirement_string(dep: Dict[str, Any]) -> str:
    if dep.get("version"):
        return f"{dep['package']}=={dep['version']}"
    if dep.get("version_spec"):
        return f"{dep['package']}{dep['version_spec']}"
    return str(dep["package"])


def _sdk_requirement_string(spec: Dict[str, Any]) -> str:
    return f"{spec['package']}=={spec['version']}"


def _sdk_install_command(python_exe: Path, spec: Dict[str, Any]) -> List[str]:
    return [
        str(python_exe),
        "-m",
        "pip",
        "install",
        "--no-input",
        *[str(arg) for arg in spec.get("install_args") or []],
        _sdk_requirement_string(spec),
    ]


def _runtime_dependency_install_command(python_exe: Path, dep: Dict[str, Any]) -> List[str]:
    return [
        str(python_exe),
        "-m",
        "pip",
        "install",
        "--no-input",
        *[str(arg) for arg in dep.get("install_args") or []],
        _dependency_requirement_string(dep),
    ]


def _installed_version_satisfies(installed_version: str | None, dep: Dict[str, Any]) -> bool:
    if not installed_version:
        return False
    try:
        if dep.get("version"):
            return installed_version == str(dep["version"])
        if dep.get("version_spec"):
            req = Requirement(f"placeholder{dep['version_spec']}")
            return Version(installed_version) in req.specifier
        return True
    except (InvalidVersion, ValueError):
        return False


def _minimum_required_version(dep: Dict[str, Any]) -> tuple[Version, bool] | None:
    try:
        if dep.get("version"):
            return Version(str(dep["version"])), False
        if not dep.get("version_spec"):
            return None
        req = Requirement(f"placeholder{dep['version_spec']}")
        minimum: tuple[Version, bool] | None = None
        for specifier in req.specifier:
            if specifier.operator not in {">=", ">", "==", "~=", "==="}:
                continue
            try:
                version = Version(specifier.version)
            except InvalidVersion:
                continue
            candidate = (version, specifier.operator in {">", "~="})
            if minimum is None or version > minimum[0] or (version == minimum[0] and candidate[1] and not minimum[1]):
                minimum = candidate
        return minimum
    except ValueError:
        return None


def _installed_version_is_below_minimum(installed_version: str | None, dep: Dict[str, Any]) -> bool:
    if not installed_version:
        return True
    minimum = _minimum_required_version(dep)
    if minimum is None:
        return False
    try:
        installed = Version(installed_version)
    except InvalidVersion:
        return False
    minimum_version, exclusive = minimum
    if installed < minimum_version:
        return True
    if exclusive and installed == minimum_version:
        return True
    return False


def _runtime_dependency_action(installed_version: str | None, dep: Dict[str, Any]) -> str:
    if not installed_version:
        return "install"
    if _installed_version_satisfies(installed_version, dep):
        return "already-satisfied"
    if _installed_version_is_below_minimum(installed_version, dep):
        return "install"
    return "preserved-newer-installed-version"


def _plan_runtime_dependency_actions(
    current_versions: Dict[str, str],
    spec: Dict[str, Any],
) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    for dep in spec.get("runtime_dependencies") or []:
        installed_version = current_versions.get(_normalize_dist_name(dep["package"]))
        action = _runtime_dependency_action(installed_version, dep)
        action_record = {
            "package": dep["package"],
            "version": dep.get("version"),
            "version_spec": dep.get("version_spec"),
            "requirement": _dependency_requirement_string(dep),
            "manifest_requirement": _dependency_requirement_string(dep),
            "installed_version": installed_version,
            "install_args": list(dep.get("install_args") or []),
            "install_command": _runtime_dependency_install_command(Path("/unused/python"), dep),
            "action": action,
        }
        if action == "preserved-newer-installed-version":
            action_record["report"] = (
                "Preserved newer installed version. No downgrade performed. "
                "Import probe will determine compatibility with current Hermes environment."
            )
        actions.append(action_record)
    return actions


def _bind_runtime_dependency_commands(
    python_exe: Path,
    runtime_dependency_actions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    bound: List[Dict[str, Any]] = []
    for action in runtime_dependency_actions:
        item = dict(action)
        item["install_command"] = _runtime_dependency_install_command(python_exe, item)
        bound.append(item)
    return bound


def _verify_import_probe(
    python_exe: Path,
    spec: Dict[str, Any],
    runtime_dependency_reports: List[Dict[str, Any]] | None = None,
) -> None:
    proc = subprocess.run(
        [str(python_exe), "-c", str(spec.get("import_probe") or "")],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        preserved = [
            dep for dep in (runtime_dependency_reports or [])
            if dep.get("action") == "preserved-newer-installed-version"
        ]
        details = []
        for dep in preserved:
            details.append(
                f"dependency={dep.get('package')} installed={dep.get('installed_version')} manifest={dep.get('manifest_requirement')}"
            )
        preserved_block = ""
        if details:
            preserved_block = "\nPreserved runtime dependencies:\n" + "\n".join(details)
        raise K.InstallError(
            f"SDK import probe failed for {spec.get('package')}=={spec.get('version')}:"
            f"{preserved_block}\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
        )


def _assert_dependency_state_preserved(
    before: Dict[str, str],
    after: Dict[str, str],
    policy: Dict[str, Any] | None,
    allowed_changed_packages: set[str] | None = None,
) -> Dict[str, Any]:
    policy = dict(policy or {})
    preserve_existing_versions = bool(policy.get("preserve_existing_versions", True))
    protected_packages = [
        _normalize_dist_name(name)
        for name in (policy.get("protected_packages") or [])
        if str(name).strip()
    ]
    allowed_changed = {
        _normalize_dist_name(name)
        for name in (allowed_changed_packages or set())
        if str(name).strip()
    }

    protected_changes = [
        {
            "package": name,
            "before": before.get(name),
            "after": after.get(name),
        }
        for name in protected_packages
        if before.get(name) is not None and before.get(name) != after.get(name)
    ]
    if protected_changes:
        detail = ", ".join(
            f"{item['package']}: {item['before']} -> {item['after']}" for item in protected_changes
        )
        raise K.InstallError(f"Protected package(s) changed during dependency installation: {detail}")

    changed = [
        {"package": name, "before": before[name], "after": after.get(name)}
        for name in sorted(before)
        if name in after and before[name] != after[name] and name not in allowed_changed
    ]
    removed = [name for name in sorted(before) if name not in after]
    if preserve_existing_versions and (changed or removed):
        details: List[str] = []
        if changed:
            details.append(
                "changed packages: " + ", ".join(
                    f"{item['package']}: {item['before']} -> {item['after']}" for item in changed
                )
            )
        if removed:
            details.append("removed packages: " + ", ".join(removed))
        raise K.InstallError(
            "Dependency preservation check failed; Hermes-managed packages changed. " + " | ".join(details)
        )
    return {
        "preserve_existing_versions": preserve_existing_versions,
        "protected_packages": protected_packages,
        "allowed_changed_packages": sorted(allowed_changed),
        "changed_packages": changed,
        "removed_packages": removed,
        "ok": True,
    }


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

    for spec in all_specs(hermes_root):
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
    for spec in all_specs(hermes_root):
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


def enable_plugin_in_config(
    hermes_home: Path, backup_dir: Path, dry_run: bool
) -> Dict[str, Any]:
    """Enable the trade plugin via ``plugins.enabled`` in the Hermes config.

    This is how ``/trade`` reaches Telegram's slash-command menu: the plugin's
    ``register()`` calls ``PluginContext.register_command``, and that only runs
    when the plugin is enabled. It replaces the old ``hermes_cli/commands.py``
    patch, which emitted a ``gateway_platforms`` keyword that some Hermes builds
    reject -- taking the whole command registry down with it.
    """
    step("Enable trade plugin in Hermes config")
    config_path = C.find_config(hermes_home)
    if config_path is None:
        warn(f"No config.yaml/config.yml under {hermes_home}")
        warn("/trade will still route, but may not appear in the Telegram menu.")
        return {"action": "skipped", "reason": "config-not-found"}

    ok(f"Config: {config_path}")
    try:
        record = C.enable_trade(
            config_path,
            None if dry_run else backup_dir,
            dry_run=dry_run,
        )
    except C.ConfigError as exc:
        # Never fail the whole install for an optional menu entry.
        warn(f"Config not edited: {exc}")
        warn("/trade will still route via the adapter seams.")
        return {"action": "skipped", "reason": str(exc)}

    if record["action"] == "already-enabled":
        skip("trade already present in plugins.enabled")
    else:
        ok(f"plugins.enabled -> {record['action']}")
    if record.get("other_plugins_preserved"):
        ok(f"preserved {len(record['other_plugins_preserved'])} other enabled plugin(s)")
    if record.get("trade_was_already_enabled"):
        ok("recorded: trade was user-enabled before install (uninstall will keep it)")
    return record


def install_dependencies(
    python_exe: Path,
    dry_run: bool,
    manifest_path: Path | None = None,
) -> Dict[str, Any]:
    step("Dependencies")
    req = REPO_ROOT / "installer" / "requirements.txt"
    req_command = [str(python_exe), "-m", "pip", "install", "--no-input", "-r", str(req)]
    manifest = load_dependency_manifest(manifest_path)
    sdk_specs = iter_sdk_dependencies(manifest)

    if dry_run:
        if req.is_file():
            ok(f"Would run: {' '.join(req_command)}")
        else:
            skip("No requirements.txt; skipping base dependency install")
        sdk_reports = []
        for spec in sdk_specs:
            command = _sdk_install_command(python_exe, spec)
            ok(
                f"Would run SDK install [{spec['exchange']}:{spec.get('id') or spec['package']}] "
                f"{' '.join(command)}"
            )
            runtime_dependency_actions = _bind_runtime_dependency_commands(
                python_exe,
                _plan_runtime_dependency_actions({}, spec),
            )
            for dep_action in runtime_dependency_actions:
                ok(
                    f"Would run runtime dependency install [{spec['exchange']}:{spec.get('id') or spec['package']}] "
                    f"{' '.join(dep_action['install_command'])}"
                )
            sdk_reports.append({
                "exchange": spec["exchange"],
                "id": spec.get("id") or spec["package"],
                "package": spec["package"],
                "version": spec["version"],
                "install_args": list(spec.get("install_args") or []),
                "install_command": command,
                "runtime_dependencies": runtime_dependency_actions,
                "import_probe": spec["import_probe"],
                "import_verification": "pending",
                "preservation": dict(spec.get("preservation_policy") or {}),
                "action": "would-install",
            })
        return {
            "manifest": str(manifest_path or DEPENDENCY_MANIFEST_PATH),
            "requirements": str(req) if req.is_file() else None,
            "requirements_command": req_command,
            "sdks": sdk_reports,
            "action": "would-install",
        }

    before = _pip_list_versions(python_exe)

    if req.is_file():
        proc = subprocess.run(req_command, capture_output=True, text=True)
        if proc.returncode != 0:
            raise K.InstallError(
                f"Dependency install failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
            )
    else:
        skip("No requirements.txt; skipping base dependency install")

    sdk_reports = []
    current_versions = before
    for spec in sdk_specs:
        command = _sdk_install_command(python_exe, spec)
        proc = subprocess.run(command, capture_output=True, text=True)
        if proc.returncode != 0:
            raise K.InstallError(
                f"SDK install failed for {spec['package']}=={spec['version']}:\n"
                f"{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
            )
        after_sdk = _pip_list_versions(python_exe)
        preservation = _assert_dependency_state_preserved(
            current_versions,
            after_sdk,
            spec.get("preservation_policy"),
        )
        runtime_dependency_actions = _bind_runtime_dependency_commands(
            python_exe,
            _plan_runtime_dependency_actions(after_sdk, spec),
        )
        runtime_dependency_reports = []
        runtime_versions = after_sdk
        for dep_action in runtime_dependency_actions:
            dep_report = dict(dep_action)
            if dep_action["action"] == "install":
                proc = subprocess.run(dep_action["install_command"], capture_output=True, text=True)
                if proc.returncode != 0:
                    raise K.InstallError(
                        f"Runtime dependency install failed for {dep_action['requirement']}:\n"
                        f"{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
                    )
                after_dep = _pip_list_versions(python_exe)
                dep_report["preservation"] = _assert_dependency_state_preserved(
                    runtime_versions,
                    after_dep,
                    spec.get("preservation_policy"),
                    allowed_changed_packages={dep_action["package"]},
                )
                dep_report["action"] = "installed"
                runtime_versions = after_dep
            elif dep_action["action"] == "already-satisfied":
                dep_report["preservation"] = {
                    "ok": True,
                    "reason": "already-satisfied",
                    "installed_version": dep_action.get("installed_version"),
                }
            else:
                dep_report["preservation"] = {
                    "ok": True,
                    "reason": "preserved-newer-installed-version",
                    "installed_version": dep_action.get("installed_version"),
                    "manifest_requirement": dep_action.get("manifest_requirement"),
                }
            runtime_dependency_reports.append(dep_report)
        _verify_import_probe(python_exe, spec, runtime_dependency_reports)
        sdk_reports.append({
            "exchange": spec["exchange"],
            "id": spec.get("id") or spec["package"],
            "package": spec["package"],
            "version": spec["version"],
            "install_args": list(spec.get("install_args") or []),
            "install_command": command,
            "runtime_dependencies": runtime_dependency_reports,
            "import_probe": spec["import_probe"],
            "import_verification": "passed",
            "preservation": preservation,
            "action": "installed",
        })
        ok(
            f"SDK installed: {spec['package']}=={spec['version']} "
            f"args={spec.get('install_args') or []} import=passed preservation=passed"
        )
        current_versions = runtime_versions

    ok("Dependencies satisfied")
    return {
        "manifest": str(manifest_path or DEPENDENCY_MANIFEST_PATH),
        "requirements": str(req) if req.is_file() else None,
        "requirements_command": req_command,
        "sdks": sdk_reports,
        "versions_before": before,
        "versions_after": current_versions,
        "action": "installed",
    }


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
        config_record = enable_plugin_in_config(hermes_home, backup_dir, args.dry_run)
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
            "config": config_record,
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
