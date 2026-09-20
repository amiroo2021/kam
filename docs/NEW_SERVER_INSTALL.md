# New server install for KAM + Hermes

This document describes how to reproduce the working old-server setup on a clean Ubuntu machine from GitHub plus private config/secrets.

## 1. Base OS dependencies

Install the host packages needed by Hermes, KAM, systemd units, and Python builds.

Example baseline:

- git
- python3
- python3-venv
- python3-pip
- build-essential
- curl
- ca-certificates
- jq
- unzip
- libsqlite3-dev
- libssl-dev
- libffi-dev
- pkg-config
- systemd
- ufw or your firewall tool

Recommended persistent swap:

- 4 GB swap minimum
- keep it enabled permanently on the new server

## 2. Repository

Clone the repo:

- `git clone <repo-url> /root/kam`
- `cd /root/kam`
- `git fetch origin main`
- `git checkout main`
- `git reset --hard origin/main`

## 3. Python environment(s)

Hermes and KAM currently run from the Hermes install tree at:

- `/usr/local/lib/hermes-agent`

That tree contains the active venv:

- `/usr/local/lib/hermes-agent/venv/bin/python`

WebChat uses the upstream Hermes WebUI checkout at:

- `/opt/hermes-webui`

GoldenFibo uses its own project venv when developed or tested in-place:

- `/root/kam/GoldenFibo/.venv`

## 4. Hermes Agent dependencies

Hermes gateway / platform services must already exist and be healthy.

Required live service on the old server:

- `hermes-gateway.service` (user service)

Important runtime path:

- `HERMES_HOME=/root/.hermes`

## 5. Telegram /trade

Install the trade capability into the Hermes tree, not the git checkout copy.

Source of truth for installed code:

- `/usr/local/lib/hermes-agent/plugins/trade/`

Relevant runtime pieces:

- Telegram adapter seam in the Hermes install tree
- `plugins/trade/wizard.py`
- `plugins/trade/tradedesk.py`
- `plugins/trade/candles.py`
- `plugins/trade/canonical.py`
- `plugins/trade/ladder_math.py`
- exchange agents under `plugins/trade/agents/`

Verification command:

- `./verify.sh --trade --hermes-root /usr/local/lib/hermes-agent`

## 6. WebTrade

WebTrade runs as:

- systemd unit: `webtrade.service`
- port: `9001`

Runtime path:

- `/usr/local/lib/hermes-agent/plugins/trade/webtrade`

Unit template source:

- `installer/systemd/webtrade.service`

Environment:

- `HERMES_HOME=/root/.hermes`
- `WEB_PASSWORD=<set manually>`
- optional `WEB_HINT=<set manually>`
- optional `WEB_SESSION_SECRET=<set manually>`

Notes:

- WebTrade is the password-gated web UI.
- It must fail closed when `WEB_PASSWORD` is missing.
- Open firewall TCP 9001 only to the users who should access it.

Health check:

- `curl -fsS http://127.0.0.1:9001/api/health`

## 7. GoldenFibo

GoldenFibo is used by the backtest and charting paths.

Important repo paths:

- `GoldenFibo/goldenfibo/`
- `GoldenFibo/tests/`

Key fixes currently in source:

- canonical cached 1-minute candles drive calculations
- display timeframe is display-only
- continuous resampling is used for charts
- live 1m updates merge into the selected display bucket
- REPLAY→LIVE keeps the selected display timeframe
- Step VWAP / Step POC / Ladder VWAP / Ladder POC / Ladder VAL/VAH are preserved

## 8. Telegram /backtest

Backtest source lives under:

- `plugins/trade/backtest_wizard.py`

Behavior to preserve:

- canonical cached 1-minute candles drive the run
- detailed text summary is returned
- ladder image is returned
- no Step Value Area requirement

## 9. WebBacktest

WebBacktest runs as:

- systemd unit: `webbacktest.service`
- port: `9002`

Runtime path:

- `/usr/local/lib/hermes-agent/plugins/trade/webbacktest`

Unit template source:

- `installer/systemd/webbacktest.service`

Environment:

- `HERMES_HOME=/root/.hermes`
- `WEBBACKTEST_PORT=9002`
- `WEB_PASSWORD=<set manually>`
- `WEB_HINT=<set manually>`

Health check:

- `curl -fsS http://127.0.0.1:9002/api/health`

## 10. WebChat / Hermes WebUI

WebChat is the clean upstream Hermes WebUI clone:

- systemd unit: `webchat.service`
- port: `9000`
- checkout root: `/opt/hermes-webui`

Unit template source:

- `installer/systemd/webchat.service`

Environment:

- `HERMES_HOME=/root/.hermes`
- `WEBCHAT_PORT=9000`
- `WEB_PASSWORD=<set manually>`
- `WEB_HINT=<set manually>`
- `HERMES_WEBUI_STATE_DIR=/root/.hermes/webui`

Health check:

- `curl -fsS http://127.0.0.1:9000/health`

## 11. Required systemd services

Install and enable these services on the new server:

- `hermes-gateway.service` user service
- `webtrade.service`
- `webbacktest.service`
- `webchat.service`

Do not enable the deprecated `trade-web.service` unless you have confirmed you still need it. On the old server it is disabled and not part of the active setup.

## 12. Directories and permissions

Expected live directories:

- `/usr/local/lib/hermes-agent`
- `/opt/hermes-webui`
- `/root/.hermes`
- `/root/.hermes/webui`
- `/root/.hermes/trade`
- `/root/.hermes/kam`
- `/root/.hermes/cache`
- `/root/.hermes/sessions`
- `/root/.hermes/logs`

Permissions should allow the service user/root context used by the existing installation to read and write these paths.

## 13. Market-data and cache directories

Preserve or recreate if you want history available immediately:

- `GoldenFibo/data/`
- `GoldenFibo/data/aggtrades.sqlite`
- `GoldenFibo/data/backtest_klines.sqlite`
- `GoldenFibo/data/binance_klines.sqlite`
- Hermes caches under `/root/.hermes/cache`
- WebUI state under `/root/.hermes/webui`

## 14. Firewall / ports

Open only what you need:

- 9000 TCP for WebChat
- 9001 TCP for WebTrade
- 9002 TCP for WebBacktest

Keep Telegram gateway egress/network access as required by Hermes.

## 15. Post-install verification

Run these after installation:

- `./verify.sh --trade --hermes-root /usr/local/lib/hermes-agent`
- confirm `webtrade.service` is active and `:9001/api/health` returns 200
- confirm `webbacktest.service` is active and `:9002/api/health` returns 200
- confirm `webchat.service` is active and `:9000/health` returns 200
- confirm Hermes gateway is running
- confirm `/trade` and `/backtest` respond in Telegram

## 16. Config vs code

Code is in GitHub.

Manual configuration stays outside GitHub and must be recreated on the new server:

- `.env`
- API keys
- exchange credentials
- Telegram bot credentials
- web passwords / hints
- WebUI session secret
- any host-specific service overrides

Secrets must never be committed.

## 17. Old-server state to preserve in GitHub

The current GitHub source already contains the live /trade, WebTrade, /backtest, WebBacktest, and WebChat integration code and the installer/systemd templates that recreate the services. The new-server job is to supply the private config and persistent data.
