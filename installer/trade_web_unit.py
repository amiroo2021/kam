"""Install / verify / remove the KAM WebChat systemd unit.

WebChat is the clean upstream Hermes WebUI deployed separately from KAM
source. KAM only provides the wrapper, systemd unit, and environment mapping.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIT_TEMPLATE = REPO_ROOT / "installer" / "systemd" / "webchat.service"
UNIT_NAME = "webchat.service"
LEGACY_UNIT_NAME = "hermes-web-chat.service"
DEFAULT_SYSTEMD_DIR = Path("/etc/systemd/system")
DEFAULT_WEBUI_ROOT = Path("/opt/hermes-webui")
UPSTREAM_REPO = "https://github.com/nesquena/hermes-webui.git"


def _run(cmd: List[str], *, check: bool = False, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check, cwd=str(cwd) if cwd else None)


def _systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def _python_exe() -> str:
    return shutil.which("python3") or sys.executable


def _webchat_port() -> int:
    raw = os.environ.get("WEBCHAT_PORT", "9000").strip() or "9000"
    try:
        return int(raw)
    except ValueError:
        return 9000


def password_status(hermes_home: Path) -> Tuple[bool, int]:
    """Return (present, length) for WEB_PASSWORD. Never returns the value."""
    live = os.environ.get("WEB_PASSWORD", "").strip()
    if live:
        return True, len(live)
    env_path = Path(hermes_home).expanduser() / ".env"
    if not env_path.is_file():
        return False, 0
    try:
        lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return False, 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != "WEB_PASSWORD":
            continue
        value = value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1]
        elif value.startswith("'") and value.endswith("'") and len(value) >= 2:
            value = value[1:-1]
        value = value.strip()
        return (bool(value), len(value))
    return False, 0


def ensure_webui_checkout(webui_root: Path, dry_run: bool = False) -> Tuple[bool, str]:
    """Clone the upstream Hermes WebUI repo if needed."""
    if webui_root.is_dir():
        bootstrap = webui_root / "bootstrap.py"
        if bootstrap.is_file():
            return True, f"present {webui_root}"
        return False, f"existing path is not a Hermes WebUI checkout: {webui_root}"
    if dry_run:
        return True, f"would clone {UPSTREAM_REPO} -> {webui_root}"
    webui_root.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(["git", "clone", "--depth", "1", UPSTREAM_REPO, str(webui_root)], check=False)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "git clone failed").strip()
    return True, f"cloned {webui_root}"


def render_unit(*, hermes_home: Path, webui_root: Path, python_exe: str, webchat_port: int) -> str:
    text = UNIT_TEMPLATE.read_text(encoding="utf-8")
    return (
        text.replace("__WEBUI_ROOT__", str(webui_root))
        .replace("__HERMES_HOME__", str(hermes_home))
        .replace("__WEBCHAT_PORT__", str(webchat_port))
        .replace("__PYTHON__", python_exe)
    )


def install_trade_web_unit(
    *,
    hermes_root: Path,
    hermes_home: Path,
    systemd_dir: Path,
    dry_run: bool = False,
    start: bool = True,
) -> Dict[str, Any]:
    """Render unit, retire legacy Hermes WebUI unit, enable webchat."""
    webui_root = Path(os.environ.get("WEBCHAT_WEBUI_ROOT", str(DEFAULT_WEBUI_ROOT))).expanduser()
    webchat_port = _webchat_port()
    record: Dict[str, Any] = {
        "unit": UNIT_NAME,
        "legacy_unit": LEGACY_UNIT_NAME,
        "systemd_dir": str(systemd_dir),
        "webui_root": str(webui_root),
        "hermes_home": str(hermes_home),
        "ok": True,
        "dry_run": dry_run,
        "actions": [],
    }
    if not UNIT_TEMPLATE.is_file():
        record["ok"] = False
        record["error"] = f"missing unit template: {UNIT_TEMPLATE}"
        return record

    checkout_ok, checkout_msg = ensure_webui_checkout(webui_root, dry_run=dry_run)
    record["actions"].append(checkout_msg)
    if not checkout_ok:
        record["ok"] = False
        record["error"] = checkout_msg
        return record

    body = render_unit(
        hermes_home=hermes_home,
        webui_root=webui_root,
        python_exe=_python_exe(),
        webchat_port=webchat_port,
    )
    dst = Path(systemd_dir) / UNIT_NAME
    legacy = Path(systemd_dir) / LEGACY_UNIT_NAME

    if dry_run:
        record["actions"].append(f"would-write {dst}")
        if legacy.is_file():
            record["actions"].append(f"would-remove {legacy}")
        record["actions"].append("would daemon-reload / enable --now webchat (if start)")
        return record

    if not str(systemd_dir).strip():
        record["actions"].append("skipped: empty systemd_dir")
        return record

    systemd_dir = Path(systemd_dir)
    systemd_dir.mkdir(parents=True, exist_ok=True)

    # Retire legacy upstream unit first so :9000 cannot double-bind.
    if _systemctl_available():
        _run(["systemctl", "disable", "--now", LEGACY_UNIT_NAME], check=False)
        record["actions"].append(f"disable --now {LEGACY_UNIT_NAME}")
    if legacy.is_file():
        try:
            legacy.unlink()
            record["actions"].append(f"removed {legacy}")
        except OSError as exc:
            record["actions"].append(f"failed-remove-legacy: {exc}")

    tmp = dst.with_suffix(dst.suffix + ".kamtmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(dst)
    record["actions"].append(f"wrote {dst}")
    record["unit_path"] = str(dst)

    if not _systemctl_available():
        record["actions"].append("systemctl unavailable; unit file installed only")
        return record

    _run(["systemctl", "daemon-reload"], check=False)
    record["actions"].append("daemon-reload")

    pw_ok, pw_len = password_status(hermes_home)
    record["web_password_present"] = pw_ok
    record["web_password_length"] = pw_len
    if not pw_ok:
        record["actions"].append(
            "WARNING: WEB_PASSWORD missing — unit installed but not started; "
            "set it in $HERMES_HOME/.env then: systemctl enable --now webchat"
        )
        _run(["systemctl", "enable", UNIT_NAME], check=False)
        record["actions"].append(f"enable {UNIT_NAME} (not started: no password)")
        return record

    if start:
        proc = _run(["systemctl", "enable", "--now", UNIT_NAME], check=False)
        record["actions"].append(f"enable --now {UNIT_NAME} rc={proc.returncode}")
        if proc.returncode != 0:
            record["ok"] = False
            record["error"] = (proc.stderr or proc.stdout or "enable --now failed")[-500:]
    else:
        _run(["systemctl", "enable", UNIT_NAME], check=False)
        record["actions"].append(f"enable {UNIT_NAME} (start skipped)")
    return record


def uninstall_trade_web_unit(
    *,
    systemd_dir: Path,
    dry_run: bool = False,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "actions": [],
        "systemd_dir": str(systemd_dir),
    }
    if not str(systemd_dir).strip():
        record["actions"].append("skipped: empty systemd_dir")
        return record
    systemd_dir = Path(systemd_dir)
    for name in (UNIT_NAME, LEGACY_UNIT_NAME):
        path = systemd_dir / name
        if dry_run:
            if path.is_file() or True:
                record["actions"].append(f"would disable --now / remove {name}")
            continue
        if _systemctl_available():
            _run(["systemctl", "disable", "--now", name], check=False)
            record["actions"].append(f"disable --now {name}")
        if path.is_file():
            try:
                path.unlink()
                record["actions"].append(f"removed {path}")
            except OSError as exc:
                record["ok"] = False
                record["actions"].append(f"failed-remove {path}: {exc}")
    if not dry_run and _systemctl_available():
        _run(["systemctl", "daemon-reload"], check=False)
        record["actions"].append("daemon-reload")
    return record


def verify_trade_web_unit(
    *,
    hermes_root: Path,
    hermes_home: Path,
    systemd_dir: Path,
    require_active_health: bool = True,
) -> List[Tuple[str, bool, str]]:
    """Return list of (name, ok, detail). Never includes secret values."""
    results: List[Tuple[str, bool, str]] = []
    webui_root = Path(os.environ.get("WEBCHAT_WEBUI_ROOT", str(DEFAULT_WEBUI_ROOT))).expanduser()
    main_py = webui_root / "bootstrap.py"
    if main_py.is_file():
        results.append(("upstream hermes-webui clone", True, str(main_py)))
    else:
        results.append(("upstream hermes-webui clone", False, f"missing {main_py}"))

    unit_path = Path(systemd_dir) / UNIT_NAME
    if not unit_path.is_file():
        results.append(("webchat.service present", False, f"missing {unit_path}"))
    else:
        text = unit_path.read_text(encoding="utf-8", errors="ignore")
        checks = [
            (f"WorkingDirectory={webui_root}" in text or f"WorkingDirectory={webui_root}/" in text,
             "WorkingDirectory uses webui root"),
            (f"EnvironmentFile=-{hermes_home}/.env" in text, "reads KAM .env"),
            ("WEBCHAT_PORT" in text, "WEBCHAT_PORT in unit"),
            ("HERMES_WEBUI_PORT" in text, "maps WEBCHAT_PORT to HERMES_WEBUI_PORT"),
            ("HERMES_WEBUI_PASSWORD" in text, "maps WEB_PASSWORD to HERMES_WEBUI_PASSWORD"),
            ("HERMES_WEBUI_HINT" in text, "maps WEB_HINT to HERMES_WEBUI_HINT"),
            ("0.0.0.0" in text, "binds externally"),
            ("bootstrap.py" in text, "ExecStart points to upstream bootstrap"),
        ]
        bad = [msg for ok, msg in checks if not ok]
        if bad:
            results.append(("webchat.service contents", False, "; ".join(bad)))
        else:
            results.append(("webchat.service contents", True, str(unit_path)))

    legacy = Path(systemd_dir) / LEGACY_UNIT_NAME
    if legacy.is_file():
        results.append(("legacy hermes-web-chat.service removed", False, f"still present: {legacy}"))
    else:
        results.append(("legacy hermes-web-chat.service removed", True, "absent"))

    pw_ok, pw_len = password_status(hermes_home)
    if pw_ok:
        results.append(("WEB_PASSWORD present", True, f"present len={pw_len}"))
    else:
        results.append(("WEB_PASSWORD present", False, f"set WEB_PASSWORD in {hermes_home}/.env"))

    if require_active_health and _systemctl_available():
        st = _run(["systemctl", "is-active", UNIT_NAME], check=False)
        active = (st.stdout or "").strip() == "active"
        if active:
            import json
            import urllib.request

            port = _webchat_port()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
                    code = getattr(resp, "status", None) or resp.getcode()
                    body = resp.read().decode("utf-8", errors="ignore")
                if int(code) != 200:
                    results.append(("webchat health", False, f"HTTP {code}"))
                else:
                    try:
                        payload = json.loads(body)
                    except json.JSONDecodeError:
                        payload = {}
                    svc = str(payload.get("service") or "")
                    if svc and svc != "webchat":
                        results.append(("webchat health", False, f"service={svc!r} body={body[:80]!r}"))
                    else:
                        results.append(("webchat health", True, f"200 port={port}"))
            except Exception as exc:  # noqa: BLE001
                results.append(("webchat health", False, f"{type(exc).__name__}: {exc}"))
        else:
            if not pw_ok:
                results.append(("webchat active", False, "inactive (expected until WEB_PASSWORD is set)"))
            else:
                results.append(("webchat active", False, f"is-active={(st.stdout or st.stderr or '').strip()}"))
    return results


__all__ = [
    "UNIT_NAME",
    "LEGACY_UNIT_NAME",
    "install_trade_web_unit",
    "uninstall_trade_web_unit",
    "verify_trade_web_unit",
    "password_status",
    "render_unit",
    "ensure_webui_checkout",
]
