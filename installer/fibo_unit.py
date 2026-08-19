"""fibo.service systemd unit install/activate/remove/verify.

Extracted contract from the proven monolithic install_trade.py path so the
modular installer actually writes /etc/systemd/system/fibo.service (Lodo
fresh-install bug: only the template was checked).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kamlib as K  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
FIBO_SERVICE_TEMPLATE_PATH = REPO_ROOT / "installer" / "fibo.service.template"
DEFAULT_SYSTEMD_DIR = Path("/etc/systemd/system")
SUSPICIOUS_UNIT_PATH_TOKENS = (
    "/tmp/",
    "/var/tmp/",
    "kam-itest",
    "kam-ik-itest",
    "kam-fibo-verify",
    "kam-smoke-",
    "kam-adapter-itest",
    "kam-menu-agents",
)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def resolve_python_exe(hermes_root: Path) -> Path:
    venv_py = Path(hermes_root) / "venv" / "bin" / "python"
    if venv_py.is_file():
        return venv_py
    return Path(sys.executable)


def render_fibo_unit(
    *,
    hermes_root: Path,
    hermes_home: Path,
    python_exe: Optional[Path] = None,
) -> str:
    if not FIBO_SERVICE_TEMPLATE_PATH.is_file():
        raise FileNotFoundError(f"missing systemd template: {FIBO_SERVICE_TEMPLATE_PATH}")
    if python_exe is None:
        python_exe = resolve_python_exe(hermes_root)
    template = FIBO_SERVICE_TEMPLATE_PATH.read_text(encoding="utf-8")
    runtime_dir = Path(hermes_home) / "fibo"
    replacements = {
        "{{HERMES_ROOT}}": str(hermes_root),
        "{{HERMES_HOME}}": str(hermes_home),
        "{{PYTHON_EXE}}": str(python_exe),
        "{{SOCKET_PATH}}": str(runtime_dir / "service.sock"),
        "{{STATE_PATH}}": str(runtime_dir / "service_state.json"),
        "{{LEDGER_PATH}}": str(runtime_dir / "service_ledger.jsonl"),
        "{{EVENT_LOG_PATH}}": str(runtime_dir / "service-events.log"),
    }
    rendered = template
    for old, new in replacements.items():
        rendered = rendered.replace(old, new)
    if "{{" in rendered:
        raise ValueError("unresolved template placeholders remain in fibo.service")
    return rendered


def is_real_systemd_dir(systemd_dir: Path) -> bool:
    try:
        return Path(systemd_dir).expanduser().resolve() == DEFAULT_SYSTEMD_DIR.resolve()
    except Exception:
        return str(systemd_dir) == str(DEFAULT_SYSTEMD_DIR)


def validate_production_fibo_unit(rendered: str, *, systemd_dir: Path) -> None:
    if not is_real_systemd_dir(systemd_dir):
        return
    lowered = rendered.lower()
    hits = [token for token in SUSPICIOUS_UNIT_PATH_TOKENS if token.lower() in lowered]
    if hits:
        raise K.InstallError(
            "Refusing to install fibo.service into the real production systemd directory "
            f"because the rendered unit contains suspicious temporary/test paths: {', '.join(hits)}"
        )


def install_fibo_service_unit(
    *,
    hermes_root: Path,
    hermes_home: Path,
    systemd_dir: Path,
    dry_run: bool = False,
    backup_dir: Optional[Path] = None,
    python_exe: Optional[Path] = None,
) -> Dict[str, Any]:
    """Render and write fibo.service. Idempotent."""
    systemd_dir = Path(systemd_dir)
    target = systemd_dir / "fibo.service"
    if python_exe is None:
        python_exe = resolve_python_exe(hermes_root)
    rendered = render_fibo_unit(
        hermes_root=hermes_root, hermes_home=hermes_home, python_exe=python_exe
    )
    validate_production_fibo_unit(rendered, systemd_dir=systemd_dir)
    before = _sha256_file(target)
    after = _sha256_text(rendered)
    runtime_dir = Path(hermes_home) / "fibo"
    runtime_paths = [
        runtime_dir,
        runtime_dir / "service_state.json",
        runtime_dir / "service_ledger.jsonl",
        runtime_dir / "service-events.log",
        runtime_dir / "service.sock",
    ]
    if dry_run:
        action = "unchanged" if before == after else ("would-update" if before else "would-create")
        return {
            "action": action,
            "path": str(target),
            "sha256_before": before,
            "sha256_after": after,
            "runtime_dir": str(runtime_dir),
            "runtime_paths": [str(p) for p in runtime_paths],
            "python_exe": str(python_exe),
            "dry_run": True,
        }

    systemd_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    if target.is_file() and before != after and backup_dir is not None:
        bk = Path(backup_dir) / "systemd" / "fibo.service"
        bk.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, bk)
    target.write_text(rendered, encoding="utf-8")
    action = "unchanged" if before == after else ("updated" if before else "created")
    return {
        "action": action,
        "path": str(target),
        "sha256_before": before,
        "sha256_after": after,
        "runtime_dir": str(runtime_dir),
        "runtime_paths": [str(p) for p in runtime_paths],
        "python_exe": str(python_exe),
        "dry_run": False,
    }


def activate_fibo_service(
    *,
    systemd_dir: Path,
    dry_run: bool = False,
    no_restart: bool = False,
) -> Dict[str, Any]:
    """daemon-reload + enable + start fibo.service on real systemd only."""
    target = Path(systemd_dir) / "fibo.service"
    if not target.is_file():
        return {"action": "skipped", "reason": "unit-not-installed", "path": str(target)}
    if no_restart:
        return {"action": "skipped", "reason": "no-restart-flag", "path": str(target)}
    if not is_real_systemd_dir(systemd_dir):
        # Isolated test systemd dirs: unit file is enough; no systemctl.
        return {
            "action": "skipped",
            "reason": "non-production-systemd-dir",
            "path": str(target),
        }
    if dry_run:
        return {"action": "would-activate", "path": str(target)}
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "fibo.service"], check=True)
    subprocess.run(["systemctl", "restart", "fibo.service"], check=True)
    proc = subprocess.run(
        ["systemctl", "is-active", "fibo.service"], capture_output=True, text=True
    )
    state = (proc.stdout or "").strip()
    if state != "active":
        raise K.InstallError(f"fibo.service did not return to active (state={state})")
    return {"action": "activated", "state": state, "path": str(target)}


def remove_fibo_service_unit(
    *,
    systemd_dir: Path,
    dry_run: bool = False,
    no_restart: bool = False,
) -> Dict[str, Any]:
    """Stop/disable (real systemd) and remove fibo.service unit file."""
    target = Path(systemd_dir) / "fibo.service"
    record: Dict[str, Any] = {"path": str(target), "dry_run": dry_run}
    if not target.is_file():
        record["action"] = "absent"
        return record
    if dry_run:
        record["action"] = "would-remove"
        return record
    if is_real_systemd_dir(systemd_dir) and not no_restart:
        subprocess.run(["systemctl", "stop", "fibo.service"], check=False)
        subprocess.run(["systemctl", "disable", "fibo.service"], check=False)
    target.unlink(missing_ok=True)
    if is_real_systemd_dir(systemd_dir) and not no_restart:
        subprocess.run(["systemctl", "daemon-reload"], check=False)
    record["action"] = "removed"
    return record


def verify_fibo_service_unit(
    *,
    hermes_root: Path,
    hermes_home: Path,
    systemd_dir: Path,
    require_active: bool = False,
) -> tuple[bool, List[str]]:
    """Return (ok, messages) for installed fibo.service unit content."""
    messages: List[str] = []
    ok = True
    target = Path(systemd_dir) / "fibo.service"
    if not target.is_file():
        return False, [f"[FAIL] missing fibo.service unit: {target}"]

    text = target.read_text(encoding="utf-8")
    messages.append(f"[ok] unit exists: {target}")

    required_tokens = [
        str(hermes_root),
        str(hermes_home),
        "plugins.trade.fibo_daemon",
        "--socket-path",
        str(Path(hermes_home) / "fibo" / "service.sock"),
        str(Path(hermes_home) / "fibo" / "service_state.json"),
        "WorkingDirectory=",
        "ExecStart=",
    ]
    for tok in required_tokens:
        if tok not in text:
            ok = False
            messages.append(f"[FAIL] unit missing required token: {tok}")
        else:
            messages.append(f"[ok] unit contains {tok}")

    # Stale/temp path regression.
    lowered = text.lower()
    for token in SUSPICIOUS_UNIT_PATH_TOKENS:
        if token.lower() in lowered and is_real_systemd_dir(systemd_dir):
            ok = False
            messages.append(f"[FAIL] production unit contains suspicious path token: {token}")

    # Python path in ExecStart should exist when using real install.
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            parts = line.split("=", 1)[1].strip().split()
            if parts:
                py = Path(parts[0])
                if py.is_file():
                    messages.append(f"[ok] ExecStart python exists: {py}")
                else:
                    # In fake fixtures python may be sys.executable still ok if exists
                    if py.exists():
                        messages.append(f"[ok] ExecStart python path exists: {py}")
                    else:
                        ok = False
                        messages.append(f"[FAIL] ExecStart python missing: {py}")
            break

    if "/root/kam" in text and str(hermes_root) != "/root/kam":
        ok = False
        messages.append("[FAIL] unit points at source tree /root/kam")

    if require_active and is_real_systemd_dir(systemd_dir):
        proc = subprocess.run(
            ["systemctl", "is-active", "fibo.service"], capture_output=True, text=True
        )
        state = (proc.stdout or "").strip()
        if state != "active":
            ok = False
            messages.append(f"[FAIL] fibo.service not active (state={state})")
        else:
            messages.append("[ok] fibo.service active")
        sock = Path(hermes_home) / "fibo" / "service.sock"
        if sock.exists():
            messages.append(f"[ok] socket present: {sock}")
        else:
            # Soft: may take a moment after start
            messages.append(f"[note] socket not yet present: {sock}")

    return ok, messages


__all__ = [
    "DEFAULT_SYSTEMD_DIR",
    "FIBO_SERVICE_TEMPLATE_PATH",
    "activate_fibo_service",
    "install_fibo_service_unit",
    "is_real_systemd_dir",
    "remove_fibo_service_unit",
    "render_fibo_unit",
    "resolve_python_exe",
    "verify_fibo_service_unit",
]
