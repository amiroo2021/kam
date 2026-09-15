"""Run many symbols in one bgf or sgf paper process."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Mapping

from .broker import PaperBroker
from .constants import BGF, SGF, BotIdentity
from .engine import EngineConfig, GoldenFiboEngine, resolve_bot


@dataclass
class MultiEngineConfig:
    symbols: list[str]
    bot: str = "bgf"
    state_root: Path = Path("state")
    spread: float = 0.0


class GoldenFiboMultiEngine:
    """Shared PaperBroker, many independent SideBooks for one bot identity."""

    def __init__(self, config: MultiEngineConfig, broker: PaperBroker | None = None) -> None:
        self.config = config
        self.identity = resolve_bot(config.bot)
        self.broker = broker or PaperBroker()
        self.engines: Dict[str, GoldenFiboEngine] = {
            sym: GoldenFiboEngine(
                EngineConfig(sym, self.identity.tag, config.state_root, config.spread),
                broker=self.broker,
            )
            for sym in config.symbols
        }

    def set_quotes_from_mids(self, mids: Mapping[str, float]) -> None:
        for sym, mid in mids.items():
            if sym in self.engines:
                self.engines[sym].set_quote(mid=mid)

    def tick_all(self, *, paper_backfill: bool = True) -> list[str]:
        return [engine.tick(paper_backfill=paper_backfill) for engine in self.engines.values()]
