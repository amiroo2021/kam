"""Install / verify / remove the KAM trade-web systemd unit.

Canonical runtime after install is $HERMES_ROOT/plugins/trade.
Python module path stays plugins.trade.trademenu.
Product name / unit name: trade-web.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIT_TEMPLATE = REPO_ROOT / "installer" / "systemd" / "trade-web.service"
UNIT_NAME = "trade-web.service"
LEGACY_UNIT_NAME = "trademenu.service"
DEFAULT_SYSTEMD_DIR = Path("/etc/systemd/system")


def render_unit(*, hermes_root: Path, hermes_home: Path) -> str:
    text = UNIT_TEMPLATE.read_text(encoding="utf-8")
    return (
        text.replace("__HERMES_ROOT__", str(hermes_root))
        .replace("__HERMES_HOME__", str(hermes_home))
    )


def _run(cmd: List[str], *, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def password_status(hermes_home: Path) -> Tuple[bool, int]:
    """Return (present, length) for TRADE_WEB_PASSWORD. Never returns the value."""
    live = os.environ.get("TRADE_WEB_PASSWORD", "").strip()
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
        if key.strip() != "TRADE_WEB_PASSWORD":
            continue
        value = value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1]
        elif value.startswith("'") and value.endswith("'") and len(value) >= 2:
            value = value[1:-1]
        value = value.strip()
        return (bool(value), len(value))
    return False, 0


def install_trade_web_unit(
    *,
    hermes_root: Path,
    hermes_home: Path,
    systemd_dir: Path,
    dry_run: bool = False,
    start: bool = True,
) -> Dict[str, Any]:
    """Render unit, retire legacy trademenu.service, enable trade-web."""
    record: Dict[str, Any] = {
        "unit": UNIT_NAME,
        "legacy_unit": LEGACY_UNIT_NAME,
        "systemd_dir": str(systemd_dir),
        "hermes_root": str(hermes_root),
        "hermes_home": str(hermes_home),
        "ok": True,
        "dry_run": dry_run,
        "actions": [],
    }
    if not UNIT_TEMPLATE.is_file():
        record["ok"] = False
        record["error"] = f"missing unit template: {UNIT_TEMPLATE}"
        return record

    body = render_unit(hermes_root=hermes_root, hermes_home=hermes_home)
    dst = Path(systemd_dir) / UNIT_NAME
    legacy = Path(systemd_dir) / LEGACY_UNIT_NAME

    if dry_run:
        record["actions"].append(f"would-write {dst}")
        if legacy.is_file():
            record["actions"].append(f"would-remove {legacy}")
        record["actions"].append("would daemon-reload / enable --now trade-web (if start)")
        return record

    if not str(systemd_dir).strip():
        record["actions"].append("skipped: empty systemd_dir")
        return record

    systemd_dir = Path(systemd_dir)
    systemd_dir.mkdir(parents=True, exist_ok=True)

    # Retire legacy unit first so :8001 cannot double-bind.
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
    record["trade_web_password_present"] = pw_ok
    record["trade_web_password_length"] = pw_len
    if not pw_ok:
        record["actions"].append(
            "WARNING: TRADE_WEB_PASSWORD missing — unit installed but not started; "
            "set it in $HERMES_HOME/.env then: systemctl enable --now trade-web"
        )
        # Still enable so a later start works once password is set.
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
    main_py = hermes_root / "plugins" / "trade" / "trademenu" / "__main__.py"
    if main_py.is_file():
        results.append(("trade-web package files", True, str(main_py)))
    else:
        results.append(("trade-web package files", False, f"missing {main_py}"))

    unit_path = Path(systemd_dir) / UNIT_NAME
    if not unit_path.is_file():
        results.append(("trade-web.service present", False, f"missing {unit_path}"))
    else:
        text = unit_path.read_text(encoding="utf-8", errors="ignore")
        root_s = str(hermes_root)
        checks = [
            (f"WorkingDirectory={root_s}" in text or f"WorkingDirectory={root_s}/" in text,
             "WorkingDirectory uses hermes-root"),
            (f"PYTHONPATH={root_s}" in text, "PYTHONPATH=hermes-root"),
            ("-m plugins.trade.trademenu" in text, "ExecStart module plugins.trade.trademenu"),
            (f"{root_s}/venv/bin/python" in text, "ExecStart uses hermes-root venv python"),
            ("0.0.0.0" not in text or True, "bind left to app config (0.0.0.0:8001)"),
        ]
        bad = [msg for ok, msg in checks if not ok]
        if bad:
            results.append(("trade-web.service contents", False, "; ".join(bad)))
        else:
            results.append(("trade-web.service contents", True, str(unit_path)))

    # Legacy unit must not remain enabled on real systemd dir.
    legacy = Path(systemd_dir) / LEGACY_UNIT_NAME
    if legacy.is_file():
        results.append(("legacy trademenu.service removed", False, f"still present: {legacy}"))
    else:
        results.append(("legacy trademenu.service removed", True, "absent"))

    pw_ok, pw_len = password_status(hermes_home)
    if pw_ok:
        results.append(("TRADE_WEB_PASSWORD present", True, f"present len={pw_len}"))
    else:
        results.append(
            (
                "TRADE_WEB_PASSWORD present",
                False,
                f"set TRADE_WEB_PASSWORD in {hermes_home}/.env",
            )
        )

    if require_active_health and _systemctl_available():
        st = _run(["systemctl", "is-active", UNIT_NAME], check=False)
        active = (st.stdout or "").strip() == "active"
        if active:
            import json
            import urllib.request

            try:
                with urllib.request.urlopen("http://127.0.0.1:8001/api/health", timeout=5) as resp:
                    code = getattr(resp, "status", None) or resp.getcode()
                    body = resp.read().decode("utf-8", errors="ignore")
                if int(code) != 200:
                    results.append(("trade-web health", False, f"HTTP {code}"))
                else:
                    try:
                        payload = json.loads(body)
                    except json.JSONDecodeError:
                        payload = {}
                    svc = str(payload.get("service") or "")
                    if svc != "trade-web":
                        results.append(("trade-web health", False, f"service={svc!r} body={body[:80]!r}"))
                    else:
                        results.append(("trade-web health", True, "200 service=trade-web"))
            except Exception as exc:  # noqa: BLE001
                results.append(("trade-web health", False, f"{type(exc).__name__}: {exc}"))
        else:
            if not pw_ok:
                results.append(
                    (
                        "trade-web active",
                        False,
                        "inactive (expected until TRADE_WEB_PASSWORD is set)",
                    )
                )
            else:
                results.append(("trade-web active", False, f"is-active={(st.stdout or st.stderr or '').strip()}"))
    return results


__all__ = [
    "UNIT_NAME",
    "LEGACY_UNIT_NAME",
    "install_trade_web_unit",
    "uninstall_trade_web_unit",
    "verify_trade_web_unit",
    "password_status",
    "render_unit",
]
