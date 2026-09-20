"""Install / verify / remove the KAM WebBacktest systemd unit."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIT_TEMPLATE = REPO_ROOT / "installer" / "systemd" / "webbacktest.service"
UNIT_NAME = "webbacktest.service"
DEFAULT_SYSTEMD_DIR = Path("/etc/systemd/system")


def _run(cmd: List[str], *, check: bool = False, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check, cwd=str(cwd) if cwd else None)


def _systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def _python_exe() -> str:
    return shutil.which("python3") or sys.executable


def _webbacktest_port() -> int:
    raw = os.environ.get("WEBBACKTEST_PORT", "9002").strip() or "9002"
    try:
        return int(raw)
    except ValueError:
        return 9002


def password_status(hermes_home: Path) -> Tuple[bool, int]:
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


def render_unit(*, hermes_home: Path, hermes_root: Path, python_exe: str, webbacktest_port: int) -> str:
    text = UNIT_TEMPLATE.read_text(encoding="utf-8")
    return (
        text.replace("__HERMES_ROOT__", str(hermes_root))
        .replace("__HERMES_HOME__", str(hermes_home))
        .replace("__WEBBACKTEST_PORT__", str(webbacktest_port))
        .replace("__PYTHON__", python_exe)
    )


def install_webbacktest_unit(
    *,
    hermes_root: Path,
    hermes_home: Path,
    systemd_dir: Path,
    dry_run: bool = False,
    start: bool = True,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "unit": UNIT_NAME,
        "systemd_dir": str(systemd_dir),
        "hermes_home": str(hermes_home),
        "ok": True,
        "dry_run": dry_run,
        "actions": [],
    }
    if not UNIT_TEMPLATE.is_file():
        record["ok"] = False
        record["error"] = f"missing unit template: {UNIT_TEMPLATE}"
        return record
    body = render_unit(
        hermes_home=hermes_home,
        hermes_root=hermes_root,
        python_exe=_python_exe(),
        webbacktest_port=_webbacktest_port(),
    )
    dst = Path(systemd_dir) / UNIT_NAME
    if dry_run:
        record["actions"].append(f"would-write {dst}")
        record["actions"].append("would daemon-reload / enable --now webbacktest (if start)")
        return record
    if not str(systemd_dir).strip():
        record["actions"].append("skipped: empty systemd_dir")
        return record
    systemd_dir = Path(systemd_dir)
    systemd_dir.mkdir(parents=True, exist_ok=True)
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
            "WARNING: WEB_PASSWORD missing — unit installed but not started; set it in $HERMES_HOME/.env then: systemctl enable --now webbacktest"
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


def uninstall_webbacktest_unit(
    *,
    systemd_dir: Path,
    dry_run: bool = False,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {"ok": True, "dry_run": dry_run, "actions": [], "systemd_dir": str(systemd_dir)}
    if not str(systemd_dir).strip():
        record["actions"].append("skipped: empty systemd_dir")
        return record
    systemd_dir = Path(systemd_dir)
    path = systemd_dir / UNIT_NAME
    if dry_run:
        record["actions"].append(f"would disable --now / remove {UNIT_NAME}")
        return record
    if _systemctl_available():
        _run(["systemctl", "disable", "--now", UNIT_NAME], check=False)
        record["actions"].append(f"disable --now {UNIT_NAME}")
    if path.is_file():
        try:
            path.unlink()
            record["actions"].append(f"removed {path}")
        except OSError as exc:
            record["ok"] = False
            record["actions"].append(f"failed-remove {path}: {exc}")
    if _systemctl_available():
        _run(["systemctl", "daemon-reload"], check=False)
        record["actions"].append("daemon-reload")
    return record


def verify_webbacktest_unit(
    *,
    hermes_root: Path,
    hermes_home: Path,
    systemd_dir: Path,
    require_active_health: bool = False,
) -> List[Tuple[str, bool, str]]:
    results: List[Tuple[str, bool, str]] = []
    unit_path = Path(systemd_dir) / UNIT_NAME
    if not unit_path.is_file():
        results.append(("webbacktest.service present", False, f"missing {unit_path}"))
    else:
        text = unit_path.read_text(encoding="utf-8", errors="ignore")
        checks = [
            (f"WorkingDirectory={hermes_root}" in text, "WorkingDirectory uses hermes_root"),
            (f"Environment=HERMES_HOME={hermes_home}" in text, "HERMES_HOME mapped"),
            ("WEBBACKTEST_PORT" in text, "WEBBACKTEST_PORT in unit"),
            ("plugins.trade.webbacktest" in text, "ExecStart points to webbacktest module"),
            ("WEB_PASSWORD" in text, "reads WEB_PASSWORD from env"),
            ("WEB_HINT" in text, "reads WEB_HINT from env"),
        ]
        bad = [msg for ok, msg in checks if not ok]
        if bad:
            results.append(("webbacktest.service contents", False, "; ".join(bad)))
        else:
            results.append(("webbacktest.service contents", True, str(unit_path)))
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
            port = _webbacktest_port()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as resp:
                    code = getattr(resp, "status", None) or resp.getcode()
                    body = resp.read().decode("utf-8", errors="ignore")
                if int(code) != 200:
                    results.append(("webbacktest health", False, f"HTTP {code}"))
                else:
                    payload = json.loads(body)
                    svc = str(payload.get("service") or "")
                    if svc and svc != "webbacktest":
                        results.append(("webbacktest health", False, f"service={svc!r} body={body[:80]!r}"))
                    else:
                        results.append(("webbacktest health", True, f"200 port={port}"))
            except Exception as exc:  # noqa: BLE001
                results.append(("webbacktest health", False, f"{type(exc).__name__}: {exc}"))
        else:
            if not pw_ok:
                results.append(("webbacktest active", False, "inactive (expected until WEB_PASSWORD is set)"))
            else:
                results.append(("webbacktest active", False, f"is-active={(st.stdout or st.stderr or '').strip()}"))
    return results


__all__ = [
    "UNIT_NAME",
    "install_webbacktest_unit",
    "uninstall_webbacktest_unit",
    "verify_webbacktest_unit",
    "password_status",
    "render_unit",
]
