"""Optional MT5Linux Docker + client provisioning for KAM /trade.

Integration point
-----------------
Called from ``install_trade_capability.run`` **after** the ``plugins/trade``
payload is copied and **before** the capability record is returned.

Enablement (default ON with ``./install.sh --trade``):

1. Default: **enabled** (no credential check).
2. Explicit opt-out: CLI ``--no-mt5linux`` / ``force=False``.
3. ``--mt5linux`` remains accepted for backward compatibility (no-op when default on).

Credentials stay only in Hermes ``.env``. Docker create never receives
``MT5_LOGIN`` / ``MT5_PASSWORD`` / account passwords. Broker login is optional:
installation succeeds with zero accounts configured. Initial interactive login
via noVNC is only needed when operators later want live broker access.

Idempotency
-----------
* Healthy existing container (running + RPyC reachable) is **reused** —
  never recreated or restarted by default.
* Named volume ``mt5-wine`` is created once and kept on uninstall unless
  ``purge_volume=True``.
* Client venv under ``HERMES_HOME/mt5linux/venv`` is created/updated in place.
* Existing Docker Engine is reused; auto-install runs only when missing.

This module does not modify the Windows ``metatrader`` EA bridge agent.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INSTALLER_DIR = Path(__file__).resolve().parent
ASSETS_DIR = INSTALLER_DIR / "assets" / "mt5linux"
ENTRYPOINT_ASSET = ASSETS_DIR / "entrypoint.sh"

DEFAULT_CONTAINER_NAME = "mt5"
DEFAULT_VOLUME_NAME = "mt5-wine"
DEFAULT_IMAGE = "lprett/mt5linux:latest"
DEFAULT_RPYC_HOST = "127.0.0.1"
DEFAULT_RPYC_PORT = 18812
DEFAULT_NOVNC_HTTP_PORT = 8080
DEFAULT_NOVNC_WS_PORT = 5901

# Known-good client pins (clinic verified). Pip wheels of some tags ship
# broken multiline f-strings; we install then self-heal / vendor-copy.
PINNED_MT5LINUX = "mt5linux==1.1.1"
PINNED_RPYC = "rpyc==6.0.2"

_ACCOUNT_KEY_RE = re.compile(r"^mt5linux_([A-Za-z0-9_]+)_ACCOUNT$", re.IGNORECASE)
_SERVER_KEY_RE = re.compile(r"^mt5linux_([A-Za-z0-9_]+)_SERVER$", re.IGNORECASE)
_PASSWORD_KEY_RE = re.compile(r"^mt5linux_([A-Za-z0-9_]+)_PASSWORD$", re.IGNORECASE)

_SERVER_FALLBACKS = {
    "LITE7486706": "LiteFinance-MT5-Live",
    "PO5701520": "PoTrade-MT5",
    "POTRADE5701520": "PoTrade-MT5",
    "PUPRIME33849951": "PUPrime-Live2",
}
_ALIAS_CANONICAL = {"POTRADE5701520": "PO5701520"}

# Optional known-good site-packages trees to seed a broken pip install.
# Prefer installer-bundled client (reproducible); clinic workspace paths are last-resort only.
_VENDOR_CANDIDATES = (
    ASSETS_DIR / "python-client" / "mt5linux",
    Path("/root/workspace/.venv-mt5/lib/python3.11/site-packages/mt5linux"),
    Path.home() / "workspace/.venv-mt5/lib/python3.11/site-packages/mt5linux",
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _run(
    cmd: Sequence[str],
    *,
    check: bool = False,
    timeout: Optional[float] = 120,
    env: Optional[Mapping[str, str]] = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd),
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=dict(os.environ, **(env or {})),
    )


def _load_dotenv(path: Path) -> Dict[str, str]:
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        try:
            text = path.read_text(encoding="latin-1")
        except OSError:
            return {}
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def combined_env(hermes_home: Path) -> Dict[str, str]:
    values = _load_dotenv(Path(hermes_home) / ".env")
    for k, v in os.environ.items():
        values[k] = v
    return {str(k): str(v).strip() for k, v in values.items()}


def _canonicalize_alias(alias: str) -> str:
    raw = str(alias or "").strip().upper()
    return _ALIAS_CANONICAL.get(raw, raw)


def discover_configured_accounts(hermes_home: Path) -> Dict[str, Dict[str, Any]]:
    """Public metadata only — never includes password values."""
    env = combined_env(hermes_home)
    logins: Dict[str, int] = {}
    servers: Dict[str, str] = {}
    has_pw: Dict[str, bool] = {}
    login_prio: Dict[str, int] = {}
    server_prio: Dict[str, int] = {}

    def prio(key: str) -> int:
        if key.startswith("mt5linux_"):
            return 0
        if key.startswith("MT5LINUX_"):
            return 1
        return 2

    for key, value in env.items():
        m = _ACCOUNT_KEY_RE.fullmatch(key)
        if m:
            alias = _canonicalize_alias(m.group(1))
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", alias):
                continue
            if not re.fullmatch(r"[1-9][0-9]*", str(value).strip()):
                continue
            login = int(str(value).strip())
            p = prio(key)
            if alias not in logins or p < login_prio.get(alias, 99):
                logins[alias] = login
                login_prio[alias] = p
            continue
        m = _SERVER_KEY_RE.fullmatch(key)
        if m:
            alias = _canonicalize_alias(m.group(1))
            server = str(value or "").strip()
            if not server:
                continue
            p = prio(key)
            if alias not in servers or p < server_prio.get(alias, 99):
                servers[alias] = server
                server_prio[alias] = p
            continue
        m = _PASSWORD_KEY_RE.fullmatch(key)
        if m:
            alias = _canonicalize_alias(m.group(1))
            if str(value or ""):
                has_pw[alias] = True

    preferred = ("LITE7486706", "PO5701520", "PUPRIME33849951")

    def rank(a: str) -> Tuple[int, str]:
        try:
            return (preferred.index(a), a)
        except ValueError:
            return (len(preferred), a)

    by_login: Dict[int, str] = {}
    for alias, login in logins.items():
        cur = by_login.get(login)
        if cur is None or rank(alias) < rank(cur):
            by_login[login] = alias

    out: Dict[str, Dict[str, Any]] = {}
    for login, alias in sorted(by_login.items(), key=lambda kv: rank(kv[1])):
        server = servers.get(alias) or _SERVER_FALLBACKS.get(alias)
        if not server:
            continue
        out[alias] = {
            "login": int(login),
            "server": str(server),
            "has_password": bool(has_pw.get(alias)),
            "label": f"{alias} ({login}@{server})",
        }
    return out


def mt5linux_enabled(
    *,
    hermes_home: Path,
    force: Optional[bool] = None,
) -> Tuple[bool, str]:
    """Return (enabled, reason).

    Default is **enabled** for every ``/trade`` install. Credentials are not
    required. Only ``force=False`` (``--no-mt5linux``) disables provisioning.
    ``force=True`` (``--mt5linux``) is accepted for backward compatibility.
    """
    if force is False:
        return False, "forced --no-mt5linux"
    if force is True:
        return True, "forced --mt5linux (default-on; flag optional)"
    return True, "default-on with /trade"


def state_dir(hermes_home: Path) -> Path:
    return Path(hermes_home) / "mt5linux"


def client_venv_dir(hermes_home: Path) -> Path:
    return state_dir(hermes_home) / "venv"


def state_json_path(hermes_home: Path) -> Path:
    return state_dir(hermes_home) / "install_state.json"


def _write_state(hermes_home: Path, payload: Mapping[str, Any], *, dry_run: bool) -> None:
    path = state_json_path(hermes_home)
    # Strip any accidental secret-looking keys.
    clean = {k: v for k, v in dict(payload).items() if "pass" not in str(k).lower()}
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

def docker_available() -> Tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "docker binary not found on PATH"
    try:
        cp = _run(["docker", "info"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"docker info failed: {exc}"
    if cp.returncode != 0:
        err = (cp.stderr or cp.stdout or "").strip().splitlines()
        return False, err[-1] if err else f"docker info exit {cp.returncode}"
    return True, "docker ok"


def _read_os_release() -> Dict[str, str]:
    path = Path("/etc/os-release")
    out: Dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _dpkg_status(package: str) -> Optional[str]:
    """Return dpkg status string (e.g. install ok installed) or None if absent."""
    if shutil.which("dpkg-query") is None:
        return None
    cp = _run(["dpkg-query", "-W", "-f=${Status}", package], timeout=15)
    if cp.returncode != 0:
        return None
    return (cp.stdout or "").strip() or None


def detect_docker_package_state() -> Dict[str, Any]:
    """Non-destructive inventory of Docker-related packages (Debian/Ubuntu)."""
    packages = [
        "docker.io",
        "docker-ce",
        "docker-ce-cli",
        "docker-doc",
        "docker-compose",
        "docker-compose-v2",
        "docker-compose-plugin",
        "containerd",
        "containerd.io",
        "podman-docker",
        "runc",
    ]
    installed: Dict[str, str] = {}
    for pkg in packages:
        st = _dpkg_status(pkg)
        if st and "installed" in st:
            installed[pkg] = st
    return {
        "installed": installed,
        "has_docker_ce": "docker-ce" in installed or "docker-ce-cli" in installed,
        "has_docker_io": "docker.io" in installed,
        "has_podman_docker": "podman-docker" in installed,
    }


def ensure_docker(*, dry_run: bool = False) -> Dict[str, Any]:
    """Reuse existing Docker Engine, or install it when missing.

    Safety:
    * Never removes/replaces an existing working Docker.
    * Refuses destructive package purges when conflicting stacks are detected
      without a working ``docker info``.
    * Uses distro-supported packages where possible (Debian/Ubuntu: docker.io).
    """
    record: Dict[str, Any] = {"ok": False}
    ok, msg = docker_available()
    if ok:
        record.update({"action": "exists", "ok": True, "detail": msg})
        return record

    record["precheck"] = msg
    osrel = _read_os_release()
    distro_id = (osrel.get("ID") or "").lower()
    id_like = (osrel.get("ID_LIKE") or "").lower()
    record["distro"] = {"ID": distro_id, "ID_LIKE": id_like, "VERSION_ID": osrel.get("VERSION_ID")}

    pkg_state = detect_docker_package_state()
    record["packages"] = pkg_state

    # Conflicting / half-broken stacks: do not auto-purge.
    if pkg_state.get("has_podman_docker") and shutil.which("docker"):
        # podman-docker shim present but docker info failed
        record.update(
            {
                "action": "blocked-conflict",
                "ok": False,
                "error": "podman-docker provides a docker shim but `docker info` failed; "
                "refusing automatic replacement. Fix Docker/Podman manually.",
            }
        )
        return record
    if pkg_state.get("has_docker_ce") and not shutil.which("docker"):
        record.update(
            {
                "action": "blocked-conflict",
                "ok": False,
                "error": "docker-ce packages are installed but docker binary/info is unhealthy; "
                "refusing destructive reinstall. Repair docker-ce manually.",
            }
        )
        return record

    debian_like = distro_id in {"debian", "ubuntu"} or "debian" in id_like or "ubuntu" in id_like
    if not debian_like:
        record.update(
            {
                "action": "blocked-unsupported-distro",
                "ok": False,
                "error": f"Automatic Docker install not supported for distro ID={distro_id!r}. "
                "Install Docker Engine manually, then re-run.",
            }
        )
        return record

    # Debian/Ubuntu: install docker.io (distro package) — non-destructive add.
    apt = shutil.which("apt-get")
    if apt is None:
        record.update({"action": "blocked-no-apt", "ok": False, "error": "apt-get not found"})
        return record

    install_cmd = [
        apt,
        "install",
        "-y",
        "docker.io",
        "containerd",
        "runc",
    ]
    # compose plugin optional
    if _dpkg_status("docker-compose-v2") is not None or True:
        # try include compose v2 if available in repos; ignore failure later
        install_cmd_with_compose = install_cmd + ["docker-compose-v2"]
    else:
        install_cmd_with_compose = install_cmd

    record["install_cmd"] = install_cmd_with_compose
    if dry_run:
        record.update({"action": "would-install-docker.io", "ok": True})
        return record

    # Update package lists (best effort)
    _run([apt, "update"], timeout=300)
    cp = _run(install_cmd_with_compose, timeout=600)
    if cp.returncode != 0:
        # Retry without compose package
        cp = _run(install_cmd, timeout=600)
        record["install_cmd"] = install_cmd
    if cp.returncode != 0:
        record.update(
            {
                "action": "install-failed",
                "ok": False,
                "error": (cp.stderr or cp.stdout or "").strip()[-800:],
            }
        )
        return record

    # Enable/start docker service
    if shutil.which("systemctl"):
        _run(["systemctl", "enable", "--now", "docker"], timeout=60)
        _run(["systemctl", "start", "docker"], timeout=60)

    # Allow current user if non-root (best effort; root install is common for Hermes)
    # Do not force usermod in automated path.

    ok2, msg2 = docker_available()
    record["postcheck"] = msg2
    if ok2:
        record.update({"action": "installed-docker.io", "ok": True, "detail": msg2})
    else:
        record.update(
            {
                "action": "installed-but-unhealthy",
                "ok": False,
                "error": f"packages installed but docker info still failing: {msg2}",
            }
        )
    return record


def _container_inspect(name: str) -> Optional[Dict[str, Any]]:
    cp = _run(["docker", "inspect", name], timeout=30)
    if cp.returncode != 0:
        return None
    try:
        data = json.loads(cp.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not data:
        return None
    return data[0]


def _port_open(host: str, port: int, timeout_s: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def rpyc_healthy(host: str = DEFAULT_RPYC_HOST, port: int = DEFAULT_RPYC_PORT) -> Tuple[bool, str]:
    if not _port_open(host, port, timeout_s=2.0):
        return False, f"RPyC port {host}:{port} not accepting TCP"
    return True, f"RPyC TCP {host}:{port} open"


def container_is_healthy(
    name: str = DEFAULT_CONTAINER_NAME,
    *,
    host: str = DEFAULT_RPYC_HOST,
    port: int = DEFAULT_RPYC_PORT,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    info = _container_inspect(name)
    if info is None:
        return False, f"container {name!r} not found", None
    state = (info.get("State") or {})
    if not state.get("Running"):
        return False, f"container {name!r} not running (status={state.get('Status')})", info
    ok, msg = rpyc_healthy(host, port)
    if not ok:
        return False, msg, info
    return True, f"container {name!r} running and RPyC reachable", info


def ensure_volume(name: str = DEFAULT_VOLUME_NAME, *, dry_run: bool) -> Dict[str, Any]:
    cp = _run(["docker", "volume", "inspect", name], timeout=30)
    if cp.returncode == 0:
        return {"name": name, "action": "exists"}
    if dry_run:
        return {"name": name, "action": "would-create"}
    cp2 = _run(["docker", "volume", "create", name], timeout=60)
    if cp2.returncode != 0:
        return {"name": name, "action": "error", "error": (cp2.stderr or cp2.stdout or "").strip()}
    return {"name": name, "action": "created"}


def _public_novnc_host(env: Mapping[str, str]) -> str:
    for key in ("mt5linux_NOVNC_HOST", "MT5LINUX_NOVNC_HOST", "NOVNC_HOST"):
        val = str(env.get(key) or "").strip()
        if val:
            return val
    # Local-only default; operators override for remote browsers.
    return "127.0.0.1"


def _ui_password(env: Mapping[str, str]) -> str:
    """VNC UI password only — never a broker password. Empty → nopw."""
    for key in ("mt5linux_UI_PASSWORD", "MT5LINUX_UI_PASSWORD", "UI_PASSWORD"):
        val = str(env.get(key) or "").strip()
        if val:
            return val
    return ""


def ensure_entrypoint_on_host(hermes_home: Path, *, dry_run: bool) -> Path:
    dest = state_dir(hermes_home) / "entrypoint.sh"
    if dry_run:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = ENTRYPOINT_ASSET
    if not src.is_file():
        raise FileNotFoundError(f"missing entrypoint asset: {src}")
    shutil.copy2(src, dest)
    dest.chmod(0o755)
    return dest


def provision_container(
    *,
    hermes_home: Path,
    container_name: str = DEFAULT_CONTAINER_NAME,
    volume_name: str = DEFAULT_VOLUME_NAME,
    image: str = DEFAULT_IMAGE,
    host: str = DEFAULT_RPYC_HOST,
    port: int = DEFAULT_RPYC_PORT,
    dry_run: bool = False,
    allow_restart_unhealthy: bool = False,
) -> Dict[str, Any]:
    """Create or reuse the MT5Linux container. Never embeds broker credentials."""
    record: Dict[str, Any] = {
        "container": container_name,
        "volume": volume_name,
        "image": image,
        "rpyc": f"{host}:{port}",
        "credentials_in_docker": False,
    }
    healthy, reason, info = container_is_healthy(container_name, host=host, port=port)
    if healthy:
        record["action"] = "reused"
        record["detail"] = reason
        return record

    if info is not None and not allow_restart_unhealthy:
        # Container exists but unhealthy — do not destroy live data automatically.
        record["action"] = "exists-unhealthy"
        record["detail"] = reason
        record["ok"] = False
        record["hint"] = (
            "Container exists but RPyC is not healthy. Refusing to recreate "
            "(preserves Wine volume / saved logins). Repair manually or re-run "
            "with allow_restart_unhealthy after operator approval."
        )
        return record

    vol = ensure_volume(volume_name, dry_run=dry_run)
    record["volume_action"] = vol
    if vol.get("action") == "error":
        record["action"] = "error"
        record["error"] = vol.get("error")
        record["ok"] = False
        return record

    entrypoint = ensure_entrypoint_on_host(hermes_home, dry_run=dry_run)
    record["entrypoint"] = str(entrypoint)
    env = combined_env(hermes_home)
    novnc_host = _public_novnc_host(env)
    ui_pw = _ui_password(env)

    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "--restart",
        "unless-stopped",
        "-p",
        f"{DEFAULT_NOVNC_HTTP_PORT}:8080",
        "-p",
        f"{DEFAULT_NOVNC_WS_PORT}:5901",
        "-p",
        f"{port}:18812",
        "-v",
        f"{volume_name}:/opt/wineprefix",
        "-v",
        f"{entrypoint}:/entrypoint.sh:ro",
        "-e",
        f"NOVNC_HOST={novnc_host}",
        "-e",
        "NOVNC_PORT=5901",
        "-e",
        "NOVNC_PAGE_PORT=8080",
        "-e",
        "MT5_HOST=0.0.0.0",
        "-e",
        "RPYC_PORT=18812",
    ]
    if ui_pw:
        cmd.extend(["-e", f"UI_PASSWORD={ui_pw}"])
    # Explicitly DO NOT pass MT5_LOGIN / MT5_PASSWORD / broker secrets.
    cmd.extend(["--entrypoint", "/bin/sh", image, "/entrypoint.sh"])

    # Scrub command for records (drop UI password value if present).
    safe_cmd = []
    skip_next = False
    for tok in cmd:
        if skip_next:
            skip_next = False
            safe_cmd.append("UI_PASSWORD=<redacted>" if tok.startswith("UI_PASSWORD=") else tok)
            continue
        if tok == "-e":
            safe_cmd.append(tok)
            skip_next = True
            continue
        safe_cmd.append(tok)
    record["docker_run_cmd"] = safe_cmd

    if dry_run:
        record["action"] = "would-create"
        record["ok"] = True
        return record

    # Pull image (best-effort)
    _run(["docker", "pull", image], timeout=600)
    # Remove only if we were allowed to replace an unhealthy container.
    if info is not None and allow_restart_unhealthy:
        _run(["docker", "rm", "-f", container_name], timeout=60)

    cp = _run(cmd, timeout=120)
    if cp.returncode != 0:
        record["action"] = "error"
        record["error"] = (cp.stderr or cp.stdout or "").strip()
        record["ok"] = False
        return record

    # Wait for RPyC TCP
    deadline = time.time() + 120
    while time.time() < deadline:
        ok, msg = rpyc_healthy(host, port)
        if ok:
            record["action"] = "created"
            record["detail"] = msg
            record["ok"] = True
            record["novnc_url"] = (
                f"http://{novnc_host}:{DEFAULT_NOVNC_HTTP_PORT}/vnc.html"
                f"?host={novnc_host}&port={DEFAULT_NOVNC_WS_PORT}&path=websockify&autoconnect=1"
            )
            return record
        time.sleep(2)
    record["action"] = "created-but-rpyc-timeout"
    record["ok"] = False
    record["detail"] = "container started but RPyC did not open within 120s"
    record["novnc_url"] = (
        f"http://{novnc_host}:{DEFAULT_NOVNC_HTTP_PORT}/vnc.html"
        f"?host={novnc_host}&port={DEFAULT_NOVNC_WS_PORT}&path=websockify&autoconnect=1"
    )
    return record


# ---------------------------------------------------------------------------
# Python client
# ---------------------------------------------------------------------------

def _venv_python(venv: Path) -> Path:
    return venv / "bin" / "python"


def _collapse_multiline_fstrings(text: str) -> str:
    """Deterministic heal for mt5linux wheels that ship multiline f'...' assignments.

    Some tags break on ``code = f'mt5.copy_...({\\n`` style lines. Collapse any
    triple-or-more-line ``code = f'...'`` / ``code = f\"...\"`` assignment onto one line.
    """
    # Collapse f-strings that span lines after `code = f` (single or double quote).
    pattern = re.compile(
        r"^(?P<prefix>\s*code\s*=\s*f)(?P<q>['\"])(?P<body>.*?)(?P=q)\s*$",
        re.MULTILINE | re.DOTALL,
    )

    def repl(match: re.Match) -> str:
        body = match.group("body")
        if "\n" not in body:
            return match.group(0)
        flat = " ".join(part.strip() for part in body.splitlines() if part.strip())
        return f"{match.group('prefix')}{match.group('q')}{flat}{match.group('q')}"

    # Broader pass: any assignment `name = f'...multiline...'`
    pattern2 = re.compile(
        r"^(?P<prefix>\s*[A-Za-z_][A-Za-z0-9_]*\s*=\s*f)(?P<q>['\"])(?P<body>.*?)(?P=q)\s*$",
        re.MULTILINE | re.DOTALL,
    )

    def repl2(match: re.Match) -> str:
        body = match.group("body")
        if "\n" not in body:
            return match.group(0)
        # Only touch bodies that look like the mt5linux remote-code pattern.
        if "mt5." not in body and "MetaTrader" not in body:
            return match.group(0)
        flat = " ".join(part.strip() for part in body.splitlines() if part.strip())
        return f"{match.group('prefix')}{match.group('q')}{flat}{match.group('q')}"

    healed = pattern2.sub(repl2, text)
    return healed


def _fix_mt5linux_site_packages(site_packages: Path) -> Dict[str, Any]:
    """Ensure mt5linux imports: compile check → deterministic f-string heal → vendored tree."""
    target = site_packages / "mt5linux" / "metatrader5.py"
    dest_pkg = site_packages / "mt5linux"
    if not target.is_file():
        # No pip install result — try vendor wholesale.
        for vendor in _VENDOR_CANDIDATES:
            if vendor.is_dir():
                if dest_pkg.exists():
                    shutil.rmtree(dest_pkg)
                shutil.copytree(vendor, dest_pkg)
                return {"action": "vendored-missing-pip", "from": str(vendor), "path": str(dest_pkg)}
        return {"action": "missing", "path": str(target)}

    text = target.read_text(encoding="utf-8", errors="replace")
    try:
        compile(text, str(target), "exec")
        return {"action": "ok", "path": str(target)}
    except SyntaxError as exc1:
        first_err = f"{exc1}"

    healed = _collapse_multiline_fstrings(text)
    try:
        compile(healed, str(target), "exec")
        target.write_text(healed, encoding="utf-8")
        return {"action": "healed-fstrings", "path": str(target), "prior_error": first_err}
    except SyntaxError as exc2:
        heal_err = f"{exc2}"

    # Prefer installer-bundled / known-good tree.
    for vendor in _VENDOR_CANDIDATES:
        if vendor.is_dir():
            if dest_pkg.exists():
                shutil.rmtree(dest_pkg)
            shutil.copytree(vendor, dest_pkg)
            # verify
            try:
                compile((dest_pkg / "metatrader5.py").read_text(encoding="utf-8"), "metatrader5.py", "exec")
            except SyntaxError as exc3:
                continue
            return {
                "action": "vendored",
                "from": str(vendor),
                "path": str(dest_pkg),
                "prior_error": first_err,
                "heal_error": heal_err,
            }
    return {
        "action": "broken",
        "path": str(target),
        "error": "SyntaxError and no usable vendor tree",
        "prior_error": first_err,
        "heal_error": heal_err,
    }


def ensure_client_venv(
    *,
    hermes_home: Path,
    python_for_venv: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Isolated venv for mt5linux client (does not require hermes gateway restart)."""
    venv = client_venv_dir(hermes_home)
    record: Dict[str, Any] = {
        "venv": str(venv),
        "pins": [PINNED_MT5LINUX, PINNED_RPYC],
    }
    py_base = python_for_venv or Path(sys.executable)
    if dry_run:
        record["action"] = "would-ensure-venv"
        record["ok"] = True
        return record

    venv.parent.mkdir(parents=True, exist_ok=True)
    if not (venv / "bin" / "python").is_file():
        cp = _run([str(py_base), "-m", "venv", str(venv)], timeout=120)
        if cp.returncode != 0:
            # try uv
            if shutil.which("uv"):
                cp = _run(["uv", "venv", "--python", "python3.11", str(venv)], timeout=120)
            if cp.returncode != 0:
                record["action"] = "error"
                record["error"] = (cp.stderr or cp.stdout or "").strip()
                record["ok"] = False
                return record
        record["venv_created"] = True
    else:
        record["venv_created"] = False

    py = _venv_python(venv)
    # ensure pip
    _run([str(py), "-m", "ensurepip", "--upgrade"], timeout=120)
    cp = _run(
        [str(py), "-m", "pip", "install", "--upgrade", "pip", PINNED_RPYC, PINNED_MT5LINUX],
        timeout=300,
    )
    record["pip_exit"] = cp.returncode
    if cp.returncode != 0:
        record["pip_error"] = (cp.stderr or cp.stdout or "").strip()[-500:]
        # Still try vendor path
    # Locate site-packages
    cp2 = _run(
        [str(py), "-c", "import site; print(site.getsitepackages()[0])"],
        timeout=30,
    )
    site_packages = Path((cp2.stdout or "").strip() or (venv / "lib"))
    if not site_packages.is_dir():
        # python3.11 layout
        for cand in (venv / "lib").glob("python*/site-packages"):
            site_packages = cand
            break
    fix = _fix_mt5linux_site_packages(site_packages)
    record["package_fix"] = fix

    # Import probe
    cp3 = _run(
        [
            str(py),
            "-c",
            "from mt5linux import MetaTrader5; import importlib.metadata as m; "
            "print(m.version('mt5linux'), m.version('rpyc'))",
        ],
        timeout=60,
    )
    if cp3.returncode != 0:
        record["action"] = "error"
        record["import_error"] = (cp3.stderr or cp3.stdout or "").strip()[-500:]
        record["ok"] = False
        return record
    record["action"] = "ready"
    record["import_probe"] = (cp3.stdout or "").strip()
    record["site_packages"] = str(site_packages)
    record["ok"] = True
    return record


def ensure_gateway_import_path(
    *,
    hermes_home: Path,
    hermes_root: Path,
    client_record: Mapping[str, Any],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Make the hermes gateway able to import mt5linux without depending on a broken pip wheel.

    Strategy: copy the working package tree into the hermes venv site-packages
    when present; also record the isolated venv path for the agent candidate list.
    """
    record: Dict[str, Any] = {"ok": True}
    site = str(client_record.get("site_packages") or "")
    if not site:
        record["action"] = "skipped-no-site"
        return record
    src = Path(site) / "mt5linux"
    if not src.is_dir():
        record["action"] = "skipped-missing-package"
        record["ok"] = False
        return record

    hermes_py = Path(hermes_root) / "venv" / "bin" / "python"
    if not hermes_py.is_file():
        record["action"] = "skipped-no-hermes-venv"
        return record
    cp = _run(
        [str(hermes_py), "-c", "import site; print(site.getsitepackages()[0])"],
        timeout=30,
    )
    hermes_site = Path((cp.stdout or "").strip())
    if not hermes_site.is_dir():
        record["action"] = "skipped-no-hermes-site"
        return record
    dest = hermes_site / "mt5linux"
    if dry_run:
        record["action"] = "would-sync-to-hermes-venv"
        record["dest"] = str(dest)
        return record
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    # also ensure rpyc if missing
    cp2 = _run([str(hermes_py), "-c", "import rpyc, mt5linux; print('ok')"], timeout=30)
    record["action"] = "synced-to-hermes-venv"
    record["dest"] = str(dest)
    record["import_ok"] = cp2.returncode == 0
    if cp2.returncode != 0:
        # install rpyc into hermes venv
        _run([str(hermes_py), "-m", "pip", "install", PINNED_RPYC], timeout=180)
        cp3 = _run([str(hermes_py), "-c", "import rpyc, mt5linux; print('ok')"], timeout=30)
        record["import_ok"] = cp3.returncode == 0
        if cp3.returncode != 0:
            record["ok"] = False
            record["import_error"] = (cp3.stderr or cp3.stdout or "").strip()[-300:]
    return record


# ---------------------------------------------------------------------------
# Connectivity / account verification (read-only)
# ---------------------------------------------------------------------------

def verify_rpyc_initialize(
    *,
    host: str = DEFAULT_RPYC_HOST,
    port: int = DEFAULT_RPYC_PORT,
    python_exe: Optional[Path] = None,
) -> Dict[str, Any]:
    py = str(python_exe or sys.executable)
    code = (
        "from mt5linux import MetaTrader5\n"
        f"m=MetaTrader5(host={host!r}, port={int(port)})\n"
        "ok=bool(m.initialize())\n"
        "err=m.last_error()\n"
        "print('INIT', ok, err)\n"
        "m.execute('''\n"
        "import MetaTrader5 as mm\n"
        "ai=mm.account_info(); ti=mm.terminal_info()\n"
        "snap={'login': None if ai is None else int(ai.login),\n"
        " 'server': None if ai is None else str(ai.server),\n"
        " 'connected': None if ti is None else bool(ti.connected),\n"
        " 'currency': None if ai is None else str(ai.currency),\n"
        " 'balance': None if ai is None else float(ai.balance)}\n"
        "''')\n"
        "print('SNAP', m.eval('snap'))\n"
        "m.shutdown()\n"
    )
    cp = _run([py, "-c", code], timeout=60)
    out = (cp.stdout or "") + (cp.stderr or "")
    ok = cp.returncode == 0 and "INIT True" in out
    return {
        "ok": ok,
        "exit_code": cp.returncode,
        "output": out[-800:],
        "needs_manual_login": (not ok) or ("login': None" in out) or ("connected': False" in out),
    }


def verify_accounts_readonly(
    *,
    hermes_home: Path,
    hermes_root: Path,
    host: str = DEFAULT_RPYC_HOST,
    port: int = DEFAULT_RPYC_PORT,
) -> Dict[str, Any]:
    """Use the installed agent when available; never print passwords."""
    accounts = discover_configured_accounts(hermes_home)
    record: Dict[str, Any] = {"accounts_configured": list(accounts.keys()), "results": {}}
    if not accounts:
        record["ok"] = True
        record["detail"] = "no accounts configured"
        return record

    # Prefer agent path with hermes_root on sys.path
    sys_path_insert = str(hermes_root)
    code = f"""
import os, sys, json
sys.path.insert(0, {sys_path_insert!r})
os.environ['HERMES_HOME'] = {str(hermes_home)!r}
from plugins.trade.agents import x_mt5linux_agent as m
m._CLIENT=None; m._CLIENT_KEY=None
out={{}}
for alias in {list(accounts.keys())!r}:
    bal = m.execute({{'operation':'balance','account':alias}})
    pos = m.execute({{'operation':'positions_orders','account':alias}})
    row = {{
      'balance_ok': bool(bal.success),
      'positions_ok': bool(pos.success),
      'error': None if bal.success else (bal.error.code if bal.error else 'ERR'),
      'login': None if not bal.success else (bal.data or {{}}).get('account',{{}}).get('login'),
      'server': None if not bal.success else (bal.data or {{}}).get('account',{{}}).get('server'),
      'currency': None if not bal.success else (bal.balance.unit if bal.balance else None),
      'balance': None if not bal.success else (bal.balance.value if bal.balance else None),
      'positions_total': None if not pos.success else (pos.data or {{}}).get('positions_total'),
      'orders_total': None if not pos.success else pos.open_order_count,
    }}
    out[alias]=row
# restore preferred PUPrime if present
if 'PUPRIME33849951' in out:
    m.execute({{'operation':'ping','account':'PUPRIME33849951'}})
print(json.dumps(out))
"""
    py = Path(hermes_root) / "venv" / "bin" / "python"
    if not py.is_file():
        py = Path(sys.executable)
    cp = _run([str(py), "-c", code], timeout=300)
    if cp.returncode != 0:
        record["ok"] = False
        record["error"] = (cp.stderr or cp.stdout or "").strip()[-500:]
        return record
    try:
        # last json line
        lines = [ln for ln in (cp.stdout or "").splitlines() if ln.strip().startswith("{")]
        data = json.loads(lines[-1]) if lines else {}
    except Exception as exc:  # noqa: BLE001
        record["ok"] = False
        record["error"] = f"parse failed: {exc}"
        record["raw"] = (cp.stdout or "")[-500:]
        return record
    record["results"] = data
    record["ok"] = all(bool(r.get("balance_ok")) and bool(r.get("positions_ok")) for r in data.values()) if data else False
    # manual login needed if all failed with init/login style codes
    if not record["ok"]:
        codes = {r.get("error") for r in data.values()}
        if codes & {"MT5LINUX_INIT_FAILED", "MT5LINUX_LOGIN_FAILED", "MT5LINUX_SYNC_FAILED", "MT5LINUX_DISCONNECTED"}:
            record["needs_manual_login"] = True
    return record


# ---------------------------------------------------------------------------
# Install / uninstall / verify entrypoints
# ---------------------------------------------------------------------------

def install_mt5linux(
    *,
    hermes_root: Path,
    hermes_home: Path,
    dry_run: bool = False,
    force: Optional[bool] = None,
    allow_restart_unhealthy: bool = False,
    skip_account_verify: bool = False,
) -> Dict[str, Any]:
    """Idempotent MT5Linux provision step for /trade install (default-on).

    Installation health (Docker, container, RPyC, Python client) determines
    ``ok``. Empty account lists and broker auth failures are reported but do
    **not** fail installation.
    """
    record: Dict[str, Any] = {
        "schema": 1,
        "component": "mt5linux",
        "dry_run": dry_run,
        "ok": True,
        "broker_auth_required_for_install": False,
    }
    enabled, reason = mt5linux_enabled(hermes_home=hermes_home, force=force)
    record["enabled"] = enabled
    record["enable_reason"] = reason
    if not enabled:
        record["action"] = "skipped"
        return record

    accounts = discover_configured_accounts(hermes_home)
    record["accounts"] = {
        a: {k: v for k, v in meta.items()}
        for a, meta in accounts.items()
    }
    # has_password is boolean only — never the secret.
    record["accounts_configured_count"] = len(accounts)

    docker_rec = ensure_docker(dry_run=dry_run)
    record["docker"] = docker_rec
    if not docker_rec.get("ok", False):
        record["ok"] = False
        record["action"] = docker_rec.get("action") or "blocked-no-docker"
        record["hint"] = docker_rec.get("error") or docker_rec.get("detail")
        return record

    env = combined_env(hermes_home)
    host = str(env.get("mt5linux_HOST") or env.get("MT5LINUX_HOST") or DEFAULT_RPYC_HOST).strip()
    try:
        port = int(str(env.get("mt5linux_PORT") or env.get("MT5LINUX_PORT") or DEFAULT_RPYC_PORT))
    except ValueError:
        port = DEFAULT_RPYC_PORT

    container = provision_container(
        hermes_home=hermes_home,
        host=host,
        port=port,
        dry_run=dry_run,
        allow_restart_unhealthy=allow_restart_unhealthy,
    )
    record["container"] = container
    if container.get("action") == "reused":
        record.setdefault("container", {})["ok"] = True
    elif container.get("ok") is False:
        record["ok"] = False

    client = ensure_client_venv(hermes_home=hermes_home, dry_run=dry_run)
    record["client"] = client
    if not client.get("ok", False):
        record["ok"] = False

    gateway_path = ensure_gateway_import_path(
        hermes_home=hermes_home,
        hermes_root=hermes_root,
        client_record=client,
        dry_run=dry_run,
    )
    record["gateway_import"] = gateway_path
    if not gateway_path.get("ok", True):
        record["ok"] = False

    # Infrastructure probe (does not require broker accounts).
    if not dry_run and record.get("ok") and container.get("action") in {
        "reused",
        "created",
        "created-but-rpyc-timeout",
    }:
        py_client = (
            _venv_python(client_venv_dir(hermes_home))
            if _venv_python(client_venv_dir(hermes_home)).is_file()
            else (hermes_root / "venv" / "bin" / "python")
        )
        init = verify_rpyc_initialize(host=host, port=port, python_exe=py_client)
        record["rpyc_init"] = {k: v for k, v in init.items() if k != "output"} | {
            "output_tail": (init.get("output") or "")[-200:]
        }
        # RPyC TCP + client import already required; initialize() failure usually
        # means MT5 UI not logged in yet — infrastructure still counts as installed.
        if not init.get("ok"):
            record["mt5_terminal_ready"] = False
            record["manual_login_required"] = True
            novnc = container.get("novnc_url") or (
                f"http://{_public_novnc_host(env)}:{DEFAULT_NOVNC_HTTP_PORT}/vnc.html"
                f"?host={_public_novnc_host(env)}&port={DEFAULT_NOVNC_WS_PORT}&path=websockify&autoconnect=1"
            )
            record["manual_login_instructions"] = [
                f"Open noVNC: {novnc}",
                "In MT5: File → Login to Trade Account (or Open an Account to discover servers).",
                "Log into each live account once so the terminal can save credentials.",
                "Do not paste passwords into Docker env or installer CLI.",
                "Re-run verify after logins succeed.",
            ]
            # Do NOT set record["ok"] = False — install infrastructure succeeded.
        else:
            record["mt5_terminal_ready"] = True

        # Optional broker account probe — informational only.
        if not skip_account_verify:
            if not accounts:
                record["account_verify"] = {
                    "ok": True,
                    "detail": "no accounts configured (empty list is not an install error)",
                    "accounts_configured": [],
                }
                record["broker_auth_ok"] = None
            elif init.get("ok"):
                av = verify_accounts_readonly(
                    hermes_home=hermes_home,
                    hermes_root=hermes_root,
                    host=host,
                    port=port,
                )
                record["account_verify"] = av
                record["broker_auth_ok"] = bool(av.get("ok"))
                if not av.get("ok", False):
                    record["manual_login_required"] = True
                    # Still not an installation failure.
            else:
                record["account_verify"] = {
                    "ok": False,
                    "skipped": True,
                    "detail": "skipped until MT5 terminal initialize() succeeds",
                }
                record["broker_auth_ok"] = False

    _write_state(
        hermes_home,
        {
            "enabled": True,
            "container": container_name_safe(container),
            "volume": DEFAULT_VOLUME_NAME,
            "rpyc": f"{host}:{port}",
            "accounts": list(accounts.keys()),
            "client_venv": str(client_venv_dir(hermes_home)),
            "pins": [PINNED_MT5LINUX, PINNED_RPYC],
            "ok": record.get("ok"),
            "manual_login_required": bool(record.get("manual_login_required")),
            "broker_auth_ok": record.get("broker_auth_ok"),
        },
        dry_run=dry_run,
    )
    record["state_path"] = str(state_json_path(hermes_home))
    return record


def container_name_safe(container_record: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        k: container_record.get(k)
        for k in ("container", "action", "detail", "ok", "novnc_url", "rpyc", "credentials_in_docker")
        if k in container_record
    }


def uninstall_mt5linux(
    *,
    hermes_home: Path,
    dry_run: bool = False,
    remove_container: bool = True,
    purge_volume: bool = False,
) -> Dict[str, Any]:
    """Remove container/client by default; preserve Wine volume unless purge_volume."""
    record: Dict[str, Any] = {
        "ok": True,
        "purge_volume": purge_volume,
        "remove_container": remove_container,
        "dry_run": dry_run,
    }
    if remove_container:
        info = _container_inspect(DEFAULT_CONTAINER_NAME)
        if info is None:
            record["container"] = {"action": "absent"}
        elif dry_run:
            record["container"] = {"action": "would-remove", "name": DEFAULT_CONTAINER_NAME}
        else:
            cp = _run(["docker", "rm", "-f", DEFAULT_CONTAINER_NAME], timeout=60)
            record["container"] = {
                "action": "removed" if cp.returncode == 0 else "error",
                "error": None if cp.returncode == 0 else (cp.stderr or cp.stdout or "").strip(),
            }
            if cp.returncode != 0:
                record["ok"] = False

    if purge_volume:
        if dry_run:
            record["volume"] = {"action": "would-purge", "name": DEFAULT_VOLUME_NAME}
        else:
            cp = _run(["docker", "volume", "rm", DEFAULT_VOLUME_NAME], timeout=60)
            record["volume"] = {
                "action": "purged" if cp.returncode == 0 else "error-or-in-use",
                "detail": (cp.stderr or cp.stdout or "").strip()[-300:],
            }
    else:
        record["volume"] = {"action": "preserved", "name": DEFAULT_VOLUME_NAME}

    # Remove client venv + state (not .env credentials)
    venv = client_venv_dir(hermes_home)
    st = state_dir(hermes_home)
    if dry_run:
        record["state"] = {"action": "would-remove", "path": str(st)}
    else:
        if venv.is_dir():
            shutil.rmtree(venv, ignore_errors=True)
        # keep entrypoint? remove whole state dir except we already may want clean
        if st.is_dir():
            shutil.rmtree(st, ignore_errors=True)
        record["state"] = {"action": "removed", "path": str(st)}
    record["env_credentials"] = "preserved"
    return record


def verify_mt5linux(
    *,
    hermes_root: Path,
    hermes_home: Path,
    force: Optional[bool] = None,
) -> Tuple[bool, List[str]]:
    """Verify MT5Linux *installation health* separately from broker auth.

    Hard failures: Docker, container/RPyC, agent file, Python client import.
    Soft (WARN only): empty account list, broker login/auth problems.
    """
    messages: List[str] = []
    enabled, reason = mt5linux_enabled(hermes_home=hermes_home, force=force)
    if not enabled:
        messages.append(f"[ok] mt5linux skipped ({reason})")
        return True, messages

    messages.append(f"[ok] mt5linux enabled ({reason})")

    docker_ok, docker_msg = docker_available()
    if not docker_ok:
        messages.append(f"[FAIL] docker: {docker_msg}")
        return False, messages
    messages.append(f"[ok] docker: {docker_msg}")

    healthy, detail, info = container_is_healthy()
    if not healthy:
        messages.append(f"[FAIL] container: {detail}")
        return False, messages
    messages.append(f"[ok] container: {detail}")

    # Best-effort Wine/MT5 process hint from container inspect
    if info is not None:
        messages.append("[ok] container inspect reachable")

    agent = hermes_root / "plugins" / "trade" / "agents" / "x_mt5linux_agent.py"
    if not agent.is_file():
        messages.append(f"[FAIL] missing agent: {agent}")
        return False, messages
    messages.append("[ok] x_mt5linux_agent.py present")

    meta = hermes_root / "plugins" / "trade" / "agents" / "x_metatrader_agent.py"
    if meta.is_file():
        messages.append("[ok] x_metatrader_agent.py preserved (separate)")

    # Python client import (prefer isolated venv, then hermes venv)
    py_candidates = [
        client_venv_dir(hermes_home) / "bin" / "python",
        hermes_root / "venv" / "bin" / "python",
        Path(sys.executable),
    ]
    client_ok = False
    client_detail = ""
    for py in py_candidates:
        if not py.is_file():
            continue
        cp = _run(
            [str(py), "-c", "from mt5linux import MetaTrader5; import importlib.metadata as m; "
             "print(m.version('mt5linux'))"],
            timeout=30,
        )
        if cp.returncode == 0:
            client_ok = True
            client_detail = f"{py}: {(cp.stdout or '').strip()}"
            break
        client_detail = (cp.stderr or cp.stdout or "")[-200:]
    if not client_ok:
        messages.append(f"[FAIL] python mt5linux client import: {client_detail}")
        return False, messages
    messages.append(f"[ok] python client: {client_detail}")

    # Account discovery operational (empty is OK)
    accounts = discover_configured_accounts(hermes_home)
    messages.append(
        f"[ok] account discovery: {len(accounts)} configured "
        f"({', '.join(accounts) if accounts else 'empty list — not an install error'})"
    )

    if not accounts:
        messages.append("[ok] broker auth: skipped (no credentials configured)")
        return True, messages

    av = verify_accounts_readonly(hermes_home=hermes_home, hermes_root=hermes_root)
    if av.get("ok"):
        messages.append(f"[ok] broker auth: {', '.join(av.get('results', {}) or accounts)}")
        for alias, row in (av.get("results") or {}).items():
            messages.append(
                f"      {alias}: login={row.get('login')} {row.get('currency')} "
                f"bal={row.get('balance')} pos={row.get('positions_total')} ord={row.get('orders_total')}"
            )
    else:
        messages.append(
            f"[WARN] broker auth not ready: {av.get('error') or av.get('detail') or 'login/initialize failed'} "
            "(install health still OK)"
        )
        if av.get("needs_manual_login"):
            messages.append("[WARN] complete broker login via noVNC when ready")
    return True, messages


__all__ = [
    "install_mt5linux",
    "uninstall_mt5linux",
    "verify_mt5linux",
    "discover_configured_accounts",
    "mt5linux_enabled",
    "container_is_healthy",
    "docker_available",
    "ensure_docker",
    "detect_docker_package_state",
]
