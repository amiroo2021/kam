"""WebBacktest configuration — reads only WEBBACKTEST_* from env."""

from __future__ import annotations

import os
from pathlib import Path


def _read_env(name: str) -> str:
    return os.environ.get(name, "").strip()


class WebBacktestConfigError(RuntimeError):
    """Raised when WebBacktest cannot start safely."""


class WebBacktestConfig:
    def __init__(self) -> None:
        self.host = "0.0.0.0"
        self.port = int(_read_env("WEBBACKTEST_PORT") or "9002")


def load_config() -> WebBacktestConfig:
    return WebBacktestConfig()
