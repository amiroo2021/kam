from __future__ import annotations
from decimal import Decimal

DEFAULT_SYMBOLS = ("BTC", "ETH", "SOL", "ZEC", "PAXG")
DEFAULT_PERCENTAGES = (Decimal("1"), Decimal("0.1"), Decimal("0.01"), Decimal("0.001"))
DEFAULT_DIRECTIONS = ("BUY", "SELL")
SYMBOL_TO_BINANCE_SPOT = {s: f"{s}USDT" for s in DEFAULT_SYMBOLS}
SYMBOL_TO_BINANCE_SPOT["PAXG"] = "PAXGUSDT"
SCHEMA_VERSION = 1
