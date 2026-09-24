"""Independent configuration for WebTrade2.

WebTrade2 intentionally does not reuse WebTrade cookie names, CSRF names, or
port defaults so both apps can run side by side.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _parse_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    if name in os.environ:
        return os.environ.get(name)
    return _parse_env_file(_hermes_home() / ".env").get(name, default)


@dataclass
class WebTrade2Config:
    password: str = ""
    session_secret: str = ""
    host: str = "0.0.0.0"
    port: int = 9009
    cookie_name: str = "webtrade2_session"
    csrf_cookie_name: str = "webtrade2_csrf"
    session_max_age_seconds: int = 12 * 60 * 60
    login_max_failures: int = 5
    login_lockout_seconds: int = 30
    preview_ttl_seconds: int = 300
    # Phase 2: hard server-side kill switch + dry-run flag. Defaults to
    # disabled read-only for safety; tests override explicitly.
    write_enabled: bool = False
    dry_run: bool = True
    # Step 7: LIVE ladder activation is opt-in. Defaults to False; the
    # server refuses LIVE ladder dispatches unless WEBTRADE2_LADDER_ENABLED=1.
    ladder_enabled: bool = False

    def __post_init__(self) -> None:
        self.password = self.password or str(_env("WEBTRADE2_PASSWORD") or _env("TRADE_WEB_PASSWORD") or _env("WEB_PASSWORD") or "")
        self.session_secret = self.session_secret or str(
            _env("WEBTRADE2_SESSION_SECRET")
            or _env("TRADE_WEB_SESSION_SECRET")
            or _env("WEB_SESSION_SECRET")
            or secrets.token_urlsafe(32)
        )
        self.host = str(_env("WEBTRADE2_HOST", self.host) or self.host)
        self.port = int(_env("WEBTRADE2_PORT", str(self.port)) or self.port)
        self.cookie_name = str(_env("WEBTRADE2_COOKIE_NAME", self.cookie_name) or self.cookie_name)
        self.csrf_cookie_name = str(_env("WEBTRADE2_CSRF_COOKIE_NAME", self.csrf_cookie_name) or self.csrf_cookie_name)
        self.preview_ttl_seconds = int(_env("WEBTRADE2_PREVIEW_TTL_SECONDS", str(self.preview_ttl_seconds)) or self.preview_ttl_seconds)
        self.write_enabled = _env_bool("WEBTRADE2_WRITE_ENABLED", self.write_enabled)
        self.dry_run = _env_bool("WEBTRADE2_DRY_RUN", self.dry_run)
        self.ladder_enabled = _env_bool("WEBTRADE2_LADDER_ENABLED", self.ladder_enabled)

    @classmethod
    def from_values(
        cls,
        *,
        password: str,
        session_secret: str,
        port: int = 9009,
        host: str = "127.0.0.1",
        write_enabled: bool = False,
        dry_run: bool = True,
        preview_ttl_seconds: int = 300,
        ladder_enabled: bool = False,
    ) -> "WebTrade2Config":
        return cls(
            password=password,
            session_secret=session_secret,
            port=port,
            host=host,
            write_enabled=write_enabled,
            dry_run=dry_run,
            preview_ttl_seconds=preview_ttl_seconds,
            ladder_enabled=ladder_enabled,
        )

    @classmethod
    def from_env(cls) -> "WebTrade2Config":
        """Build a config from current process env + ~/.hermes/.env.

        Used by tests to spin up an app with the same env shape as
        production.
        """
        return cls()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return bool(default)
    s = str(raw).strip().lower()
    if s in {"1", "true", "yes", "on", "y"}:
        return True
    if s in {"0", "false", "no", "off", "n", ""}:
        return False
    return bool(default)
