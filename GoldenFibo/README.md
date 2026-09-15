# GoldenFibo

Isolated Golden Fibonacci research / visualization platform.

**Canonical rule:** one `GoldenFiboEngine`. Historical, replay, and live only differ by
how they produce `MarketEvent`s. The browser never computes ladder levels.

## Modes (Phase 3)

| Mode | Behavior |
|---|---|
| **LIVE** | Seed P0 at current forming bar open → live Binance public streams |
| **BACKTEST** | Start→End historical OHLC (LEGACY) → **stop** (no live) |
| **REPLAY→LIVE** | Start→now historical OHLC → **same engine** → live streams |

### Historical OHLC vs live aggTrade

Default historical resolver is **LEGACY OHLC** (deterministic intrabar assumptions:
adverse progression first, then TP). This is **not** tick-perfect and is **not** claimed
to equal continuous aggTrade over the same period. Dual-touch bars are counted as
`ambiguity_count` and marked with `AMBIGUOUS_BAR` domain events while LEGACY still
applies its path rule.

### Start/End time

Must be **UTC** and **aligned to the selected timeframe** (e.g. 1m → whole minutes).
P0 for BACKTEST/REPLAY = open of the first closed bar at/after Start.

## Run

```bash
cd /home/parallels/kam/GoldenFibo
uv venv .venv
uv pip install -e ".[dev]"
uv run goldenfibo-web
# open http://$(hostname -I | awk '{print $1}'):8000
```

## Tests

```bash
uv run pytest
```

## Layout

```text
goldenfibo/
  engine/           # pure strategy (do not fork per mode)
  feeders/          # OHLC → MarketEvents
  live/             # aggTrade price → MarketEvents
  marketdata/       # Binance public + pagination + source protocols
  session/          # SessionController LIVE/BACKTEST/REPLAY_TO_LIVE
  api/              # FastAPI + WS
  static/           # chart UI (renderer only)
  metrics/          # VWAP/POC (wizard semantics)
reference/legacy_research/  # frozen oracle
```
