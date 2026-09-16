"""TradeMenu configuration — reads TRADE_WEB_* from environment / Hermes .env."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Optional, Tuple


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _load_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        elif value.startswith("'") and value.endswith("'") and len(value) >= 2:
            value = value[1:-1]
        out[key] = value
    return out


def _read_env(name: str) -> str:
    live = os.environ.get(name, "").strip()
    if live:
        return live
    dotenv = _load_dotenv(_hermes_home() / ".env")
    return str(dotenv.get(name, "")).strip()


def _persist_env_key(name: str, value: str) -> None:
    """Append key to Hermes .env if missing. Never logs the value."""
    path = _hermes_home() / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_dotenv(path)
    if existing.get(name, "").strip():
        return
    with path.open("a", encoding="utf-8") as fh:
        if path.exists() and path.stat().st_size > 0:
            fh.write("\n")
        fh.write(f"{name}={value}\n")
    os.environ[name] = value


class TradeMenuConfigError(RuntimeError):
    """Raised when TradeMenu cannot start safely (fail closed)."""


class TradeMenuConfig:
    def __init__(self) -> None:
        password = _read_env("TRADE_WEB_PASSWORD")
        if not password:
            raise TradeMenuConfigError(
                "TRADE_WEB_PASSWORD is missing. TradeMenu refuses to start unprotected."
            )
        self.password = password
        self.hint = _read_env("TRADE_WEB_HINT") or "Contact the operator for access."
        secret = _read_env("TRADE_WEB_SESSION_SECRET")
        if not secret:
            secret = secrets.token_urlsafe(48)
            _persist_env_key("TRADE_WEB_SESSION_SECRET", secret)
            # Also set for this process if dotenv write happened after import.
            secret = _read_env("TRADE_WEB_SESSION_SECRET") or secret
        self.session_secret = secret
        self.cookie_name = "trademenu_session"
        self.session_max_age_seconds = 60 * 60 * 12
        self.login_max_failures = 5
        self.login_lockout_seconds = 30
        self.host = "0.0.0.0"
        self.port = 8001


def load_config() -> TradeMenuConfig:
    return TradeMenuConfig()
