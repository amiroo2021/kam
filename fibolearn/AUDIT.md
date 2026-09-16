# FiboLearn discovery / integration audit

1. `/fibo` Telegram wizard is implemented in `plugins/trade/fibo_wizard.py`; the deeper Start Fibo flow lives under `plugins/trade/fibo/flow.py` with stores in `plugins/trade/fibo/store.py` and snapshot helpers in `plugins/trade/fibo/snapshot.py`.
2. Telegram routing/buttons/callbacks are patched through `installer/patchspecs.py` into Hermes Telegram adapter seams. `/trade` lives in `plugins/trade/wizard.py`, `/fibo` in `plugins/trade/fibo_wizard.py`, and `/backtest` in `plugins/trade/backtest_wizard.py`. Inline keyboard transport is the `send_inline_keyboard` helper installed by patchspecs.
3. GoldenFibo ladder state is stored by the committed GoldenFibo app in `GoldenFibo/goldenfibo/session/controller.py` on `SessionController.engine.state`; API serialization is in `GoldenFibo/goldenfibo/api/schemas.py`.
4. P0/P1...Pn are represented by GoldenFibo `EngineState` legs/highest_filled and rendered via `levels_for_render()` in `GoldenFibo/goldenfibo/api/schemas.py`.
5. BUY vs SELL cycles are represented by GoldenFibo `Side`/`EngineConfig.side` and separate engine state per run. FiboLearn snapshots both directions and all required percentages independently.
6. Existing market-price feeds include GoldenFibo Binance public REST/WebSocket modules under `GoldenFibo/goldenfibo/marketdata/` and trade agents such as `plugins/trade/agents/x_binance_agent.py` for market_price reads.
7. Historical data/backtest infrastructure exists in GoldenFibo `marketdata/kline_cache.py`, `session/runner.py`, and `api/app.py`; an older Telegram `/backtest` helper exists in `plugins/trade/backtest_wizard.py`. FiboLearn should prefer GoldenFibo cache/marketdata for Binance research.
8. Existing VWAP/volume-profile/POC/VAH/VAL code is in `GoldenFibo/goldenfibo/metrics/__init__.py` and older approximations in `plugins/trade/backtest_wizard.py`.
9. Existing state stores are JSONL/JSON under `~/.hermes/fibo/` for the Telegram Fibo capability, SQLite kline cache at `GoldenFibo/data/binance_klines.sqlite`, and no committed large datasets. FiboLearn uses its own SQLite schema for observations/outcomes/patterns/backtests.
10. Symbol resolution exists in `plugins/trade/canonical.py`, `plugins/trade/tradedesk.py`, agent `resolve_instrument`, and `plugins/trade/fibo/discovery.py`.
11. No project-specific AI/LLM statistics integration was found in Kam; Ask FiboLearn is therefore implemented as a data-query/reporting interface skeleton that must not hallucinate numbers.
12. Best non-invasive integration points: read GoldenFibo API/session snapshots, reuse GoldenFibo kline cache/marketdata, reuse Telegram direct-dispatch pattern with a separate `fibolearn:` namespace, and keep all FiboLearn write activity inside FiboLearn's own SQLite database.
