# Residential IP (`/trade`)

Application-level HTTP CONNECT routing for KAM Telegram `/trade`, per exchange/account.

## Live Python import path

The supervised gateway (`hermes-gateway.service`) runs:

```text
/usr/local/lib/hermes-agent/venv/bin/python -m hermes_cli.main gateway run
```

`hermes_cli/main.py` inserts `/usr/local/lib/hermes-agent` at `sys.path[0]`. That **wins** over `PYTHONPATH` and `HERMES_PYTHON_SRC_ROOT`.

Telegram `/trade` therefore imports:

```text
/usr/local/lib/hermes-agent/plugins/trade/wizard.py
/usr/local/lib/hermes-agent/plugins/trade/tradedesk.py
/usr/local/lib/hermes-agent/plugins/trade/residential_proxy.py
/usr/local/lib/hermes-agent/plugins/trade/agents/x_*_agent.py
```

The mt5linux overlay (`HERMES_PYTHON_SRC_ROOT=.../hermes-gateway-src`) is **not** the live `plugins.trade` tree for this entrypoint. Deploy RIP changes to `/usr/local/lib/hermes-agent/plugins/trade/` (and restart the gateway) for Telegram to pick them up.

This Git tree is the source of record. It is not auto-loaded by the gateway.

## Behaviour

- 🌐 button on every exchange/account action menu (no allowlist). New `x_*_agent.py` modules get the menu automatically.
- Default Residential IP **OFF**. Activation only when `residential_capability = "verified"`.
- Unsupported venues (MT5 family): menu shown, enable blocked.
- Fail-closed: when ON, hooked HTTP/WS must use CONNECT; no silent VPS fallback.
- Submenu ipify probe is **not** exchange-route verification.
- Active Trading IP is the proxy egress associated with a successful CONNECT to the exchange for that account, not an IP the venue reported.
- Verification is per exchange/account, persisted under `$HERMES_HOME/kam/residential_ip.json` (mode 0600). Credentials are never stored there.

Do not commit `.env`, `residential_ip.json`, or proxy passwords.
