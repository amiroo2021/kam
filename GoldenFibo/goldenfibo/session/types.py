"""Session modes, phases, and run metadata."""

from __future__ import annotations

from enum import Enum


class SessionMode(str, Enum):
    LIVE = "LIVE"
    BACKTEST = "BACKTEST"
    REPLAY_TO_LIVE = "REPLAY_TO_LIVE"


class SessionPhase(str, Enum):
    IDLE = "idle"
    LOADING = "loading_history"
    DOWNLOADING = "downloading_history"
    REPLAYING = "replaying"
    CATCHING_UP = "catching_up"
    LIVE = "live"
    BACKTEST_DONE = "backtest_done"
    ERROR = "error"
    STOPPED = "stopped"
