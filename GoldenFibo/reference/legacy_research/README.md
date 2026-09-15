# Golden Fibo Paper Bots

Virtual/paper Golden Fibonacci grid bots for two independent one-sided fastfib books:

- `buyGoldenFibo` / `BuyGF` / `bgf` — BUY-only, magic `20250907`
- `sellGoldenFibo` / `SellGF` / `sgf` — SELL-only, magic `20250908`

No live orders are sent. Quotes come from the venue; fills, positions, pending orders, TP sync, and cycle state are simulated locally by `PaperBroker`.

## Defaults

```py
PHI = 1.618
PERCENTAGE = 0.001
MAX_STEP = 20
Lot[n] = BASE_SIZE + n * SIZE_STEP
BASE_SIZE = 0.001
SIZE_STEP = 0.0001
MIN_LOT = 0.0001
```

## Layout

```text
golden_fibo/
  ladder.py        # pure Fibonacci ladder math
  broker.py        # PaperBroker quotes/orders/positions/fills
  side_book.py     # one side: OpenStep0, pending, fill, TP sync, close/reopen
  engine.py        # one symbol + one side
  multi_engine.py  # many symbols in one bgf or sgf process
  hl_paper.py      # Hyperliquid public mids polling loop

tests/test_acceptance.py
```

## Run tests

```bash
cd /root/golden_fibo
. .venv/bin/activate
pytest -q
```

## One-symbol demo

```py
from golden_fibo.engine import EngineConfig, GoldenFiboEngine

e = GoldenFiboEngine(EngineConfig("BTC", "bgf"))
e.set_quote(bid=2499.99, ask=2500.00)
print(e.tick())
# BTC BUY step=0 sharedTP=2502.50 nextP=2495.96 (P1) cycle=1
```

## Hyperliquid paper loop

```bash
cd /root/golden_fibo
. .venv/bin/activate
python -m golden_fibo.hl_paper --bot bgf --symbols BTC ETH SOL --interval 60
python -m golden_fibo.hl_paper --bot sgf --symbols BTC ETH SOL --interval 60
```

Paper mode may run both BGF and SGF for the same coin. Do not run live BUY+SELL on a netting exchange for the same coin.

## Important behavior

- Step 0 opens at market: BUY uses ask, SELL uses bid.
- Each side keeps its own `P0`, `highest_filled`, `last_sync_tp`, tickets, tags, and state directory.
- When a deeper step fills, all open legs for that side/cycle get TP rewritten to `TP[highest_filled]`.
- TP hit closes only that side, cancels only that side’s pendings, logs the close, then immediately opens a new step 0.
- Paper-only `backfill_crossed_steps()` synthetically fills crossed intermediate steps so a gap through P1–P4 advances to step 4 instead of leaving the book stuck at step 0.
