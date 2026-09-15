"""Single-symbol engine for one Golden Fibo side."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .broker import PaperBroker, Quote
from .constants import BGF, SGF, BotIdentity
from .side_book import SideBook


@dataclass
class EngineConfig:
    symbol: str
    bot: str = "bgf"  # bgf or sgf
    state_root: Path = Path("state")
    spread: float = 0.0


def resolve_bot(bot: str | BotIdentity) -> BotIdentity:
    if isinstance(bot, BotIdentity):
        return bot
    b = bot.lower()
    if b in ("bgf", "buygf", "buygoldenfibo"):
        return BGF
    if b in ("sgf", "sellgf", "sellgoldenfibo"):
        return SGF
    raise ValueError(f"unknown bot {bot!r}")


class GoldenFiboEngine:
    """One symbol + one side enabled for bgf or sgf."""

    def __init__(self, config: EngineConfig, broker: PaperBroker | None = None) -> None:
        self.config = config
        self.identity = resolve_bot(config.bot)
        self.broker = broker or PaperBroker()
        state_path = config.state_root / self.identity.tag / f"{config.symbol}.json"
        self.book = SideBook(config.symbol, self.identity, self.broker, state_path=state_path)

    def set_quote(self, *, mid: float | None = None, bid: float | None = None, ask: float | None = None) -> Quote:
        return self.broker.set_quote(self.config.symbol, mid=mid, bid=bid, ask=ask, spread=self.config.spread)

    def tick(self, *, paper_backfill: bool = True) -> str:
        self.book.on_tick(paper_backfill=paper_backfill)
        return self.book.status_digest()
