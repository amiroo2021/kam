"""Bot identity and sizing defaults."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class BotIdentity:
    name: str
    short: str
    tag: str
    magic: int
    side: Side
    state_dir: Path


# HL / paper-bot defaults
BASE_SIZE = 0.001
SIZE_STEP = 0.0001
MIN_LOT = 0.0001

# Tick / distance defaults (price units; venue-specific callers may override)
DEFAULT_TICK = 0.01
DEFAULT_MIN_STOP_DISTANCE = 0.5  # absolute price distance gate for pending placement

BGF = BotIdentity(
    name="buyGoldenFibo",
    short="BuyGF",
    tag="bgf",
    magic=20250907,
    side=Side.BUY,
    state_dir=Path("state/bgf"),
)

SGF = BotIdentity(
    name="sellGoldenFibo",
    short="SellGF",
    tag="sgf",
    magic=20250908,
    side=Side.SELL,
    state_dir=Path("state/sgf"),
)

BOTS = {"bgf": BGF, "sgf": SGF, "BuyGF": BGF, "SellGF": SGF}
