# GoldenFibo

Isolated research / backtesting foundation for the Golden Fibonacci ladder strategy.

**Phase 0–1 only:** one deterministic `GoldenFiboEngine` driven by ordered `MarketEvent`s,
legacy OHLC feeder compatibility, strict ambiguity detection, and dual sizing policies.
No chart UI, FastAPI, live WebSocket trading, or production Hermes integration yet.

## Layout

```text
GoldenFibo/
  goldenfibo/           # canonical runtime package
    engine/             # pure strategy: events, levels, state, engine
    feeders/            # MarketEvent producers (historical OHLC, …)
    simulation/         # sizing policies (not path logic)
  reference/
    legacy_research/    # frozen recovered golden-fibo 0.1.0 snapshot (oracle only)
  tests/
```

## Canonical rule

There is **one** calculation engine. Historical, replay, and future live paths only differ
by how they produce `MarketEvent`s. UI must never compute ladder levels.

## Geometry (verified vs legacy)

```text
PHI = 1.618
TP0(BUY)  = P0 * (1 + percentage)
TP0(SELL) = P0 * (1 - percentage)
P[n+1]    = P[n] + PHI * (P[n] - TP[n])
TP[n]     = P[n-1]   for n >= 1
```

## Run tests

```bash
cd GoldenFibo
python -m pytest
```

## Sizing

Configurable policies — **not** baked into geometry:

- `linear_research` — `Lot[n] = base + n * step` (default for legacy parity)
- `exponential_live` — `V0=V1=step0`, `Vn=step0*2^(n-1)` for `n>=2`

Default product sizing is undecided; choose explicitly per run.
