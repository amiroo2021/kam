"""Capability-aware installer state.

Authoritative manifest lives at::

    ~/.hermes/kam/install_state.json

Schema::

    {
      "schema_version": 1,
      "installer_version": "1.0.0",
      "kam_version": "1.0.0",
      "last_install_at": "2026-08-18T...",
      "capabilities": {
        "trade": true
      },
      "by_capability": {
        "trade": {...}
      },
      "shared": {...}
    }

The manifest is the authoritative source of "what is installed on this
server". The /trade capability owns exactly one folder under ``~/.hermes/``:

    ~/.hermes/trade/    -- /trade runtime/state

The folder is the owned runtime/state namespace; the manifest is the
authoritative installation status. Verifiers must check both for
consistency.

Atomic writes: write to ``<path>.tmp`` then ``os.replace``.
No credentials, no secrets.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


SCHEMA_VERSION = 1
INSTALLER_VERSION = "2.0.0"
KAM_VERSION = "2.0.0"

# Capability identifier -> owned state folder under ~/.hermes/.
KNOWN_CAPABILITIES: Dict[str, str] = {
    "trade": "trade",
}


# ---------------------------------------------------------------------------
# Hermes home resolution (persistent state directory)
# ---------------------------------------------------------------------------

def resolve_hermes_home() -> Path:
    """Return the Hermes home directory (parent of all capability folders).

    Resolution order:
      1. ``HERMES_HOME`` environment variable if set and non-empty.
      2. ``~/.hermes``.
    """
    env = os.environ.get("HERMES_HOME")
    if env and env.strip():
        return Path(env).expanduser()
    return Path.home() / ".hermes"


# ---------------------------------------------------------------------------
# Hermes root resolution (installed application tree)
# ---------------------------------------------------------------------------

# Default Hermes root (the installed Python/application tree).
DEFAULT_HERMES_ROOT = Path("/usr/local/lib/hermes-agent")


def resolve_hermes_root(explicit_root: Optional[str] = None) -> Path:
    """Return the Hermes root (the installed application tree).

    Resolution order:
      1. The ``explicit_root`` argument (typically from --hermes-root CLI).
      2. The ``HERMES_ROOT`` environment variable if set and non-empty.
      3. The DEFAULT_HERMES_ROOT (/usr/local/lib/hermes-agent).

    The Hermes root is the directory containing the installed
    ``plugins/`` tree. It is conceptually INDEPENDENT of the Hermes home
    (persistent state). Do not derive one from the other.
    """
    if explicit_root and explicit_root.strip():
        return Path(explicit_root).expanduser()
    env = os.environ.get("HERMES_ROOT")
    if env and env.strip():
        return Path(env).expanduser()
    return DEFAULT_HERMES_ROOT


def kam_root(hermes_home: Path) -> Path:
    """The ``~/.hermes/kam/`` directory (authoritative manifest location)."""
    return Path(hermes_home) / "kam"


def install_state_path(hermes_home: Path) -> Path:
    """The authoritative manifest path."""
    return kam_root(hermes_home) / "install_state.json"


def capability_dir(hermes_home: Path, capability: str) -> Path:
    """The owned folder for a capability (``~/.hermes/<cap>/``)."""
    if capability not in KNOWN_CAPABILITIES:
        raise ValueError(f"unknown capability: {capability!r}")
    return Path(hermes_home) / KNOWN_CAPABILITIES[capability]


# Legacy `.kam-trade/` directory (pre-modular installer metadata location).
LEGACY_TRADE_STATE_DIR_NAME = ".kam-trade"


def legacy_trade_state_dir(hermes_home: Path) -> Path:
    """Path to the legacy `.kam-trade/` directory if it exists."""
    return Path(hermes_home) / LEGACY_TRADE_STATE_DIR_NAME


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Write JSON atomically: tmp file in same dir, fsync, os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # tempfile in same directory guarantees os.replace is atomic on POSIX.
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True, default=str)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
    except Exception:
        # Cleanup tmp on failure.
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


# ---------------------------------------------------------------------------
# Manifest read / write
# ---------------------------------------------------------------------------

def _empty_manifest() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "installer_version": INSTALLER_VERSION,
        "kam_version": KAM_VERSION,
        "last_install_at": None,
        "capabilities": {"trade": False},
        "by_capability": {"trade": {}},
        "shared": {},
    }


def load_manifest(hermes_home: Path) -> Dict[str, Any]:
    """Load the authoritative manifest, returning an empty manifest if missing."""
    path = install_state_path(hermes_home)
    if not path.is_file():
        return _empty_manifest()
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _empty_manifest()
    if not isinstance(data, dict):
        return _empty_manifest()
    if data.get("schema_version") != SCHEMA_VERSION:
        # Different schema; treat as empty (callers should detect and migrate).
        return _empty_manifest()
    return data


def save_manifest(hermes_home: Path, manifest: Dict[str, Any]) -> Path:
    """Save the manifest atomically. Returns the path written."""
    payload = dict(manifest)
    payload["schema_version"] = SCHEMA_VERSION
    payload["last_install_at"] = datetime.now(timezone.utc).isoformat()
    path = install_state_path(hermes_home)
    _atomic_write_json(path, payload)
    return path


def set_capability(manifest: Dict[str, Any], capability: str, record: Dict[str, Any]) -> None:
    """Mark a capability installed and record its per-capability payload."""
    if capability not in KNOWN_CAPABILITIES:
        raise ValueError(f"unknown capability: {capability!r}")
    manifest.setdefault("capabilities", {})[capability] = True
    manifest.setdefault("by_capability", {})[capability] = dict(record)
    manifest["last_install_at"] = datetime.now(timezone.utc).isoformat()


def clear_capability(manifest: Dict[str, Any], capability: str) -> None:
    """Mark a capability uninstalled and clear its per-capability payload."""
    if capability not in KNOWN_CAPABILITIES:
        raise ValueError(f"unknown capability: {capability!r}")
    manifest.setdefault("capabilities", {})[capability] = False
    manifest.setdefault("by_capability", {})[capability] = {}


# ---------------------------------------------------------------------------
# Capability state queries
# ---------------------------------------------------------------------------

def is_installed(hermes_home: Path, capability: str) -> bool:
    """Return True iff the manifest says the capability is installed."""
    m = load_manifest(hermes_home)
    return bool(m.get("capabilities", {}).get(capability, False))


def any_capability_installed(hermes_home: Path) -> bool:
    """Return True iff at least one KAM capability is installed."""
    m = load_manifest(hermes_home)
    caps = m.get("capabilities", {})
    return any(bool(v) for v in caps.values())


def installed_capabilities(hermes_home: Path) -> List[str]:
    """Return the list of capabilities currently marked installed (in stable order)."""
    m = load_manifest(hermes_home)
    caps = m.get("capabilities", {})
    return [c for c in KNOWN_CAPABILITIES.keys() if bool(caps.get(c, False))]


def capability_folder_consistent(hermes_home: Path, capability: str) -> Tuple[bool, str]:
    """Verify the capability folder matches the manifest.

    Returns ``(consistent, message)``. If the manifest says installed but the
    folder is missing (or vice versa), the pair is inconsistent.

    A folder existing alone does NOT mean the capability is installed.
    """
    m = load_manifest(hermes_home)
    installed = bool(m.get("capabilities", {}).get(capability, False))
    folder = capability_dir(hermes_home, capability)
    folder_exists = folder.is_dir()
    if installed and not folder_exists:
        return False, f"manifest says {capability}=installed but {folder} is missing"
    if folder_exists and not installed:
        return False, f"{folder} exists but manifest says {capability}=not installed"
    return True, "consistent"


# ---------------------------------------------------------------------------
# CLI flag parsing helpers
# ---------------------------------------------------------------------------

CAPABILITY_FLAGS = ("--trade",)


def parse_capability_flags(argv: List[str]) -> Tuple[List[str], List[str]]:
    """Split argv into (capabilities, rest).

    Recognized flags: ``--trade``. Each may appear at most once.

    Returns:
        (capabilities, rest)
        capabilities: list of capability names (e.g. ``["trade"]``)
        rest: argv with capability flags removed

    Raises:
        SystemExit: on unknown capability flag or duplicate.
    """
    seen = set()
    caps: List[str] = []
    rest: List[str] = []
    flag_to_cap = {"--trade": "trade"}
    for arg in argv:
        if arg in flag_to_cap:
            cap = flag_to_cap[arg]
            if cap in seen:
                raise SystemExit(f"duplicate capability flag: {arg}")
            seen.add(cap)
            caps.append(cap)
        else:
            rest.append(arg)
    return caps, rest


def is_no_flag(argv: List[str]) -> bool:
    """True if argv contains no capability flag (legacy invocation)."""
    return not any(a in CAPABILITY_FLAGS for a in argv)


# ---------------------------------------------------------------------------
# Legacy `.kam-trade/` migration
# ---------------------------------------------------------------------------

def migrate_legacy_kam_trade(hermes_home: Path) -> Optional[Dict[str, Any]]:
    """Migrate legacy ``~/.hermes/.kam-trade/manifest.json`` if present.

    Returns the legacy manifest payload (if found) so callers can populate
    ``by_capability.trade`` from it. The legacy directory is renamed to
    ``.kam-trade-retired-<timestamp>/`` and never deleted silently.

    This function does NOT mutate the new ``install_state.json``. It only
    harvests legacy data and renames the directory.
    """
    legacy = legacy_trade_state_dir(hermes_home)
    legacy_manifest = legacy / "manifest.json"
    if not legacy_manifest.is_file():
        return None
    try:
        with legacy_manifest.open(encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        payload = None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    retired_name = f".kam-trade-retired-{ts}"
    retired = Path(hermes_home) / retired_name
    try:
        legacy.rename(retired)
    except OSError:
        # Rename failed; do not crash — return the payload anyway.
        pass
    return payload if isinstance(payload, dict) else None


def install_state_dir_for(hermes_home: Path) -> Path:
    """Where the new authoritative install_state.json lives."""
    return kam_root(hermes_home)


__all__ = [
    "SCHEMA_VERSION",
    "INSTALLER_VERSION",
    "KAM_VERSION",
    "KNOWN_CAPABILITIES",
    "LEGACY_TRADE_STATE_DIR_NAME",
    "resolve_hermes_home",
    "kam_root",
    "install_state_path",
    "capability_dir",
    "legacy_trade_state_dir",
    "load_manifest",
    "save_manifest",
    "set_capability",
    "clear_capability",
    "is_installed",
    "any_capability_installed",
    "installed_capabilities",
    "capability_folder_consistent",
    "CAPABILITY_FLAGS",
    "parse_capability_flags",
    "is_no_flag",
    "migrate_legacy_kam_trade",
    "install_state_dir_for",
]