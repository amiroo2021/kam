# GoldenFibo

Isolated Golden Fibonacci research / live visualization platform.

**Canonical rule:** one `GoldenFiboEngine`. Historical, replay, and live only differ by
`MarketEvent` feeders. The browser **never** computes ladder levels.

## Phases

| Phase | Status |
|---|---|
| 0–1 Engine + parity | done |
| 2 Live chart MVP | done (this tree) |
| 3 Replay / backtest UI | not started |
| Live exchange execution | **out of scope** |

## Layout

```text
GoldenFibo/
  goldenfibo/
    engine/          # pure strategy
    feeders/         # OHLC historical
    live/            # ordered price → MarketEvents
    marketdata/      # Binance public REST/WS adapters
    metrics/         # VWAP/POC (wizard semantics)
    api/             # FastAPI + session + WS
    static/          # Lightweight Charts UI
  reference/legacy_research/   # frozen 0.1.0 oracle
  tests/
```

## Run live visualization (Ubuntu)

```bash
cd /home/parallels/kam/GoldenFibo
uv venv .venv
uv pip install -e ".[dev]"
uv run goldenfibo-web
# or:
uv run uvicorn goldenfibo.api.app:app --host 0.0.0.0 --port 8000
```

On the MacBook browser (Parallels shared network):

```bash
# on Ubuntu, discover IP:
hostname -I
# then open e.g.:
http://10.211.55.6:8000
```

Local-only development. No public exposure, no API keys, no orders.

## Live P0 rule

At session start, after public REST kline seed:

**P0 = open of the latest 1m kline returned by Binance** (current forming bar open).

Historical candles fill the chart only; the engine does **not** replay the full history
into the ladder. Reconnect reuses backend state and does **not** reseed P0 while the
process is alive. Backend restart resets the in-memory paper session.

## Tests

```bash
cd GoldenFibo
uv run pytest
```
