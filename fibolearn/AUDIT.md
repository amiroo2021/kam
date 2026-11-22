# FiboLearn discovery / integration audit

1. FiboLearn is an independent research and reporting subsystem with its own `fibolearn:` Telegram namespace and SQLite database.
2. Telegram routing, buttons, and callbacks are patched through `installer/patchspecs.py`; `/trade` lives in `plugins/trade/wizard.py`, `/backtest` in `plugins/trade/backtest_wizard.py`, and FiboLearn in `plugins/trade/fibolearn_wizard.py`.
3. GoldenFibo ladder state is stored by the committed GoldenFibo app in `GoldenFibo/goldenfibo/session/controller.py` on `SessionController.engine.state`; API serialization is in `GoldenFibo/goldenfibo/api/schemas.py`.
4. P0/P1...Pn are represented by GoldenFibo `EngineState` legs/highest_filled and rendered via `levels_for_render()` in `GoldenFibo/goldenfibo/api/schemas.py`.
5. BUY and SELL cycles are represented by GoldenFibo `Side`/`EngineConfig.side` and separate engine state per run. FiboLearn snapshots both directions and all required percentages independently.
6. Existing market-price feeds include GoldenFibo public REST/WebSocket modules under `GoldenFibo/goldenfibo/marketdata/` and read-only market-data operations exposed by trade agents.
7. Historical data and backtest infrastructure exists in GoldenFibo `marketdata/kline_cache.py`, `session/runner.py`, and `api/app.py`; the Telegram `/backtest` helper exists in `plugins/trade/backtest_wizard.py`.
8. Existing VWAP, volume-profile, POC, VAH, and VAL code is in `GoldenFibo/goldenfibo/metrics/__init__.py`, with older approximations in `plugins/trade/backtest_wizard.py`.
9. GoldenFibo uses its SQLite kline cache at `GoldenFibo/data/binance_klines.sqlite`; FiboLearn uses its own SQLite schema for observations, outcomes, patterns, and backtests.
10. Symbol resolution exists in `plugins/trade/canonical.py`, `plugins/trade/tradedesk.py`, and each agent's `resolve_instrument` operation.
11. No project-specific AI/LLM statistics integration was found in KAM. Ask FiboLearn is therefore a data-query/reporting interface that must not hallucinate numbers.
12. Non-invasive integration points are GoldenFibo API/session snapshots, GoldenFibo kline cache/marketdata, and Telegram direct dispatch through the separate `fibolearn:` namespace. FiboLearn write activity stays inside its own SQLite database.
