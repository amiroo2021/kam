"""Shared helpers for the KAM /trade add-on installer.

This module is deliberately exchange-agnostic. It knows how to:

* discover an existing Hermes installation,
* resolve the Python interpreter the Hermes gateway actually runs,
* copy the trade plugin payload,
* apply anchor-validated patches to shared Hermes files,
* record and verify a manifest.

It contains **no exchange names**. Exchange availability is decided at
runtime by ``plugins/trade/tradedesk.py`` scanning ``agents/x_*_agent.py``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

INSTALLER_VERSION = "1.0.0"
KAM_VERSION = "1.0.0"

# Files/dirs copied verbatim into <HERMES_ROOT>/plugins/trade/.
PLUGIN_RELATIVE_ROOT = Path("plugins") / "trade"

# Bytecode / caches never ship.
COPY_EXCLUDE_DIR_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
COPY_EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".pyd"}

# Markers wrapping every inserted block in a shared Hermes file.
MARKER_BEGIN = "BEGIN KAM TRADE PLUGIN"
MARKER_END = "END KAM TRADE PLUGIN"

# Add-on state lives under a single directory in the Hermes root:
#
#   <HERMES_ROOT>/.kam-trade/manifest.json
#   <HERMES_ROOT>/.kam-trade/backups/<timestamp>/...
#
# Keeping the manifest and the backups under one parent means a single
# directory fully describes what the add-on did to this installation.
STATE_DIR_NAME = ".kam-trade"
BACKUPS_SUBDIR = "backups"

# Legacy layout from earlier builds; still read so an upgrade can find and
# reuse a manifest written by a previous installer version.
LEGACY_BACKUP_DIR_NAME = ".kam-trade-backups"

# Files that prove a directory really is a Hermes checkout.
HERMES_SIGNATURE_PATHS = (
    Path("hermes_cli"),
    Path("hermes_cli") / "main.py",
    Path("hermes_cli") / "commands.py",
    Path("plugins") / "platforms" / "telegram" / "adapter.py",
)

DEFAULT_HERMES_CANDIDATES = (
    Path("/usr/local/lib/hermes-agent"),
    Path("/opt/hermes-agent"),
    Path("/usr/lib/hermes-agent"),
    Path.home() / "hermes-agent",
)

SERVICE_UNIT_CANDIDATES = (
    Path("/etc/systemd/system/hermes-gateway.service"),
    Path("/lib/systemd/system/hermes-gateway.service"),
    Path("/usr/lib/systemd/system/hermes-gateway.service"),
)


class InstallError(RuntimeError):
    """Fatal, actionable installer error."""


# ---------------------------------------------------------------------------
# hashing / fs helpers
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def backup_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def iter_payload_files(payload_root: Path) -> List[Path]:
    """Return sorted relative paths of every file that should be installed."""
    results: List[Path] = []
    for path in sorted(payload_root.rglob("*")):
        if path.is_dir():
            continue
        if any(part in COPY_EXCLUDE_DIR_NAMES for part in path.parts):
            continue
        if path.suffix in COPY_EXCLUDE_SUFFIXES:
            continue
        results.append(path.relative_to(payload_root))
    return results


# ---------------------------------------------------------------------------
# Hermes discovery
# ---------------------------------------------------------------------------

def looks_like_hermes_root(candidate: Path) -> bool:
    if not candidate.is_dir():
        return False
    return all((candidate / rel).exists() for rel in HERMES_SIGNATURE_PATHS)


def discover_hermes_roots(explicit: Optional[str] = None) -> List[Path]:
    """Resolve candidate Hermes roots, most explicit first.

    Priority: ``--hermes-root`` > ``HERMES_ROOT`` env > known locations.
    A directory is never accepted merely because of its name; it must
    contain every path in :data:`HERMES_SIGNATURE_PATHS`.
    """
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not looks_like_hermes_root(root):
            raise InstallError(
                f"--hermes-root {root} does not look like a Hermes installation "
                f"(missing one of: {', '.join(str(p) for p in HERMES_SIGNATURE_PATHS)})"
            )
        return [root]

    env_root = os.environ.get("HERMES_ROOT")
    if env_root:
        root = Path(env_root).expanduser().resolve()
        if not looks_like_hermes_root(root):
            raise InstallError(
                f"HERMES_ROOT={root} does not look like a Hermes installation"
            )
        return [root]

    found: List[Path] = []
    for candidate in DEFAULT_HERMES_CANDIDATES:
        resolved = candidate.expanduser()
        if looks_like_hermes_root(resolved) and resolved.resolve() not in {p.resolve() for p in found}:
            found.append(resolved.resolve())
    return found


def resolve_hermes_root(explicit: Optional[str] = None) -> Path:
    roots = discover_hermes_roots(explicit)
    if not roots:
        raise InstallError(
            "No Hermes installation found. Re-run with --hermes-root /path/to/hermes"
        )
    if len(roots) > 1:
        listing = "\n".join(f"  - {r}" for r in roots)
        raise InstallError(
            "Multiple Hermes installations found; pass --hermes-root to choose one:\n"
            + listing
        )
    return roots[0]


def find_service_unit() -> Optional[Path]:
    for unit in SERVICE_UNIT_CANDIDATES:
        if unit.is_file():
            return unit
    return None


def resolve_gateway_python(hermes_root: Path) -> Path:
    """Resolve the interpreter the gateway actually runs.

    Resolution order:

    1. An in-tree virtualenv under *hermes_root* — authoritative, because it
       is the environment belonging to the installation we are targeting.
    2. The ``ExecStart`` interpreter of a systemd unit, but only when that
       unit really refers to *hermes_root*. Without this guard a host unit
       for a different installation would hijack the resolution.
    3. The current interpreter.

    Paths are deliberately **not** ``resolve()``-d. A venv's ``bin/python``
    is typically a symlink to a base interpreter, and following it discards
    the virtualenv context — the resulting interpreter cannot import the
    venv's site-packages. Only ``expanduser`` + absolute-ness is applied.
    """

    def _keep_venv_path(path: Path) -> Path:
        expanded = path.expanduser()
        return expanded if expanded.is_absolute() else (Path.cwd() / expanded)

    for rel in (Path("venv") / "bin" / "python", Path(".venv") / "bin" / "python"):
        candidate = hermes_root / rel
        if candidate.exists():
            return _keep_venv_path(candidate)

    unit = find_service_unit()
    if unit is not None:
        try:
            unit_text = unit.read_text(errors="ignore")
        except OSError:
            unit_text = ""
        # Only trust the unit when it points at the root we are installing into.
        if str(hermes_root) in unit_text:
            for line in unit_text.splitlines():
                stripped = line.strip()
                if not stripped.startswith("ExecStart="):
                    continue
                command = stripped.split("=", 1)[1].strip()
                first = command.split()[0] if command.split() else ""
                candidate = Path(first)
                if candidate.name.startswith("python") and candidate.exists():
                    return _keep_venv_path(candidate)

    return Path(sys.executable)


def resolve_hermes_home() -> Path:
    """Resolve HERMES_HOME (where .env and user plugins live)."""
    unit = find_service_unit()
    if unit is not None:
        try:
            for line in unit.read_text(errors="ignore").splitlines():
                stripped = line.strip()
                if stripped.startswith('Environment="HERMES_HOME='):
                    value = stripped.split("=", 1)[1]
                    value = value.split("=", 1)[1].rstrip('"')
                    return Path(value).expanduser().resolve()
        except (OSError, IndexError):
            pass
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    return (Path.home() / ".hermes").resolve()


# ---------------------------------------------------------------------------
# anchor-validated patching
# ---------------------------------------------------------------------------

@dataclass
class PatchSpec:
    """One anchor-validated insertion into a shared Hermes file."""

    seam: str
    relative_path: Path
    anchor_before: str
    anchor_after: str
    block: str
    insertion_indent: str
    # If this literal already exists without a KAM marker, the seam is
    # considered natively present and is left alone.
    native_sentinel: str
    ast_validator: Optional[Callable[[str, "PatchSpec"], None]] = None

    def marker_begin(self) -> str:
        return f"{MARKER_BEGIN} ({self.seam})"

    def marker_end(self) -> str:
        return f"{MARKER_END} ({self.seam})"


@dataclass
class PatchOutcome:
    seam: str
    relative_path: str
    action: str  # "patched" | "already-installed" | "native-present" | "would-patch"
    detail: str = ""
    sha256_before: Optional[str] = None
    sha256_after: Optional[str] = None


def _ast_validate_python(text: str, spec: PatchSpec) -> None:
    try:
        ast.parse(text, filename=str(spec.relative_path))
    except SyntaxError as exc:
        message = exc.msg or "invalid syntax"
        line = exc.lineno or "?"
        raise InstallError(
            f"[{spec.seam}] AST validation failed for {spec.relative_path}: {message} at line {line}. Refusing to patch."
        ) from exc
    if spec.ast_validator is not None:
        spec.ast_validator(text, spec)


def apply_patch(text: str, spec: PatchSpec) -> Tuple[str, str, str]:
    """Return ``(new_text, action, detail)``.

    Aborts by raising :class:`InstallError` unless every invariant holds:
    both anchors unique, correct order, and result compiles at the caller.
    """
    if spec.marker_begin() in text:
        return text, "already-installed", "KAM marker already present"

    if spec.native_sentinel in text:
        return text, "native-present", "seam already wired natively (left untouched)"

    if not spec.insertion_indent:
        raise InstallError(f"[{spec.seam}] insertion_indent is empty; refusing to patch.")

    before_count = text.count(spec.anchor_before)
    if before_count != 1:
        raise InstallError(
            f"[{spec.seam}] anchor_before matched {before_count} times "
            f"(expected exactly 1) in {spec.relative_path}. Refusing to patch."
        )
    after_count = text.count(spec.anchor_after)
    if after_count != 1:
        raise InstallError(
            f"[{spec.seam}] anchor_after matched {after_count} times "
            f"(expected exactly 1) in {spec.relative_path}. Refusing to patch."
        )

    idx_before = text.index(spec.anchor_before)
    idx_after = text.index(spec.anchor_after)
    if idx_before >= idx_after:
        raise InstallError(
            f"[{spec.seam}] anchor ordering invalid in {spec.relative_path} "
            f"({idx_before} >= {idx_after}). Refusing to patch."
        )

    lines = text.splitlines(keepends=True)
    target_line_index: Optional[int] = None
    running = 0
    for i, line in enumerate(lines):
        if running <= idx_before < running + len(line):
            target_line_index = i
            break
        running += len(line)
    if target_line_index is None:
        raise InstallError(f"[{spec.seam}] could not locate anchor line; refusing to patch.")

    indent = spec.insertion_indent
    marker_open = f"{indent}# {spec.marker_begin()}\n"
    marker_close = f"{indent}# {spec.marker_end()}\n"
    code = ""
    for raw in spec.block.strip("\n").splitlines():
        code += (f"{indent}{raw}\n" if raw.strip() else "\n")
    insertion = "\n" + marker_open + code + marker_close

    new_text = (
        "".join(lines[: target_line_index + 1])
        + insertion
        + "".join(lines[target_line_index + 1 :])
    )

    if new_text.index(spec.marker_begin()) >= new_text.index(spec.anchor_after):
        raise InstallError(
            f"[{spec.seam}] post-insert ordering check failed; refusing to keep patch."
        )
    if spec.relative_path.suffix == ".py":
        _ast_validate_python(new_text, spec)
    return new_text, "patched", "inserted after anchor_before"


def remove_patch(text: str, spec: PatchSpec) -> Tuple[str, bool]:
    """Remove the marked block for *spec*. Returns ``(text, removed)``.

    :func:`apply_patch` inserts ``"\\n" + marker_open + code + marker_close``
    immediately after the anchor line, i.e. it adds one blank separator line
    *before* the block. Removal must therefore consume that leading blank
    line as well, otherwise each install/uninstall cycle would leave a
    stray blank line behind and the file would not be restored
    byte-for-byte.
    """
    begin = spec.marker_begin()
    end = spec.marker_end()
    pattern = re.compile(
        # optional leading blank line inserted by apply_patch, then the
        # marker-delimited block including its trailing newline.
        r"\n?[ \t]*#[ \t]*" + re.escape(begin)
        + r".*?#[ \t]*" + re.escape(end) + r"[ \t]*\n",
        re.DOTALL,
    )
    new_text, count = pattern.subn("", text)
    return new_text, count > 0


def compile_check(python_exe: Path, path: Path) -> None:
    proc = subprocess.run(
        [str(python_exe), "-m", "py_compile", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise InstallError(
            f"Syntax check failed for {path}:\n{proc.stderr.strip()[:2000]}"
        )


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------

def manifest_path(repo_root: Path) -> Path:
    return repo_root / "installer" / "manifest.json"


def state_dir(hermes_root: Path) -> Path:
    """Directory holding all add-on state for this installation."""
    return hermes_root / STATE_DIR_NAME


def backups_root(hermes_root: Path) -> Path:
    """Parent directory for timestamped backup snapshots."""
    return state_dir(hermes_root) / BACKUPS_SUBDIR


def installed_manifest_path(hermes_root: Path) -> Path:
    return state_dir(hermes_root) / "manifest.json"


def legacy_manifest_path(hermes_root: Path) -> Path:
    return hermes_root / LEGACY_BACKUP_DIR_NAME / "manifest.json"


def find_manifest(hermes_root: Path) -> Optional[Path]:
    """Return the current manifest path, falling back to the legacy layout."""
    current = installed_manifest_path(hermes_root)
    if current.is_file():
        return current
    legacy = legacy_manifest_path(hermes_root)
    if legacy.is_file():
        return legacy
    return None


def read_manifest(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_manifest(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def detect_hermes_version(hermes_root: Path) -> Dict[str, str]:
    """Best-effort, non-fatal Hermes version/commit detection."""
    info: Dict[str, str] = {}
    try:
        proc = subprocess.run(
            ["git", "-C", str(hermes_root), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            info["commit"] = proc.stdout.strip()
    except OSError:
        pass
    try:
        proc = subprocess.run(
            ["git", "-C", str(hermes_root), "describe", "--tags", "--always"],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            info["describe"] = proc.stdout.strip()
    except OSError:
        pass
    adapter = hermes_root / "plugins" / "platforms" / "telegram" / "adapter.py"
    if adapter.is_file():
        info["adapter_sha256"] = sha256_file(adapter)
    return info
