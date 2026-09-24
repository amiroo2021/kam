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

    @classmethod
    def from_values(
        cls,
        *,
        password: str,
        session_secret: str,
        port: int = 9009,
        host: str = "127.0.0.1",
    ) -> "WebTrade2Config":
        return cls(password=password, session_secret=session_secret, port=port, host=host)
