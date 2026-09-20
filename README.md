# KAM — `/trade` + `/fibo` + webtrade add-on for Hermes

KAM is an installable `/trade` + `/fibo` add-on for an existing [Hermes](https://hermes-agent.nousresearch.com) node that is already connected to Telegram. It adds:

- `/trade`: the Telegram trading console wizard backed by a pluggable set of exchange agents
- `/fibo`: the Telegram Fibo control wizard (lightweight UI skeleton; future iterations will reuse the shared exchange-agent layer)
- **webtrade**: password-gated web UI on `http://<server-ip>:9001/` (same TradeDesk + agents as Telegram)

**There is no enable flag.** If the add-on is installed, `/trade` and/or `/fibo` is enabled. If you remove it, the commands are gone.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Existing Hermes installation | The installer refuses to run against anything that is not a real Hermes checkout |
| Working Telegram connection | KAM does not configure Telegram; it reuses your existing bot |
| Python 3.10+ | Uses the same interpreter your Hermes gateway runs |
| `git` | For clone and upgrade |
| Exchange credentials | Only for the exchanges you actually want to use — see [Credentials](#credentials) |
| `WEB_PASSWORD` in `$HERMES_HOME/.env` | Required for WebTrade on :9001; operator-supplied only (never auto-generated) |
| root / sudo | Required to write into the Hermes tree and manage systemd units |
| Firewall | Allow TCP 9001 from clients that should reach webtrade |

---

## Install

```bash
git clone https://github.com/amiroo2021/kam.git
cd kam
# Set WEB_PASSWORD in $HERMES_HOME/.env before or after install (required for a healthy :9001)
sudo ./install.sh --trade --hermes-root /usr/local/lib/hermes-agent
sudo ./verify.sh --trade --hermes-root /usr/local/lib/hermes-agent
```

If Hermes is in a standard location you may omit `--hermes-root` and let it auto-detect. If several installations are found, the installer stops and asks you to choose one explicitly.

After installation:

- Telegram: send `/trade` (gateway restart may be required unless `--no-restart`)
- Web: open `http://<server-public-ip>:9001/` (login at `/login`)
- Both UIs load code from `$HERMES_ROOT/plugins/trade` (not the git checkout)
- systemd unit: `webtrade.service` (legacy `webtrade.service` is disabled/removed)

### Options

| Flag | Effect |
|---|---|
| `--hermes-root PATH` | Target a specific Hermes installation |
| `--hermes-home PATH` | Persistent state dir (default `~/.hermes`) |
| `--dry-run` | Show everything that would happen; change nothing |
| `--no-restart` | Install and verify, but leave the gateway alone |
| `--skip-deps` | Do not touch pip (useful when deps are already managed) |
| `--trade` / `--fibo` | Capability selection (default no-flag = trade) |

### Fresh server checklist

1. Hermes installed and Telegram connected
2. Clone KAM and run `./install.sh --trade --hermes-root …`
3. Put exchange credentials + `WEB_PASSWORD` in `$HERMES_HOME/.env` (do not commit)
4. `systemctl enable --now webtrade` if password was added after install
5. Open `http://192.34.66.78:9001/` (example public IP) — bind is `0.0.0.0:9001`
6. `./verify.sh --trade --hermes-root …` must PASS

---

## Dry run

Always safe. Detects paths, lists planned file operations, shows the intended patches, runs source-side checks, and installs nothing.

```bash
sudo ./install.sh --trade --dry-run --hermes-root /path/to/hermes
```

---

## Verify

Offline and read-only for exchange APIs. Never places or cancels an order. webtrade checks include unit file contract, password presence (length only), and live `GET /api/health` when the unit is active.

```bash
./verify.sh --trade --hermes-root /path/to/hermes
```

Prints PASS or FAIL with the exact failed checks, and exits non-zero on failure. Missing `WEB_PASSWORD` is a hard FAIL with guidance to set it in `$HERMES_HOME/.env`.

---

## Upgrade

```bash
git pull
sudo ./install.sh --trade --hermes-root /path/to/hermes
```

The installer is idempotent. Re-running it will not duplicate handlers, imports, patch blocks, service units, or dependency entries, and will not reset your configuration or remove credentials. Unchanged components are reported as already installed.

---

## Uninstall

```bash
sudo ./uninstall.sh --trade --hermes-root /path/to/hermes
```

Removes only add-on-owned files and only the marked KAM blocks from shared Hermes files. Stops/disables `webtrade.service` and any legacy `webtrade.service`. It never deletes your `.env`, your credentials, unrelated plugins, or shared dependencies. Backups are preserved unless you pass `--purge-backups`. Supports `--dry-run` and `--no-restart`.

---

## Enabling behavior

There is no `TRADE_ENABLED` flag, and none is supported.

- Add-on installed → `/trade` and `/fibo` enabled
- Add-on removed → `/trade` and `/fibo` disabled

An exchange appears in the wizard when its agent file is present. An *account* appears when its credentials are present in your existing Hermes environment.

---

## Exchange agents

Exchange support is discovered at runtime by scanning the agents directory for files matching:

```
plugins/trade/agents/x_<exchange>_agent.py
```

The exchange name is derived from the filename. `__init__.py` and non-matching files are ignored. Each agent exposes `name`, `list_accounts()`, `capabilities()`, and `execute(request)`.

**Adding a new exchange requires no installer change** — drop in a new `x_<exchange>_agent.py` and it is picked up automatically. The installer contains no hardcoded exchange list.

Shipped agents:

| Exchange | Agent file |
|---|---|
| arcus | `x_arcus_agent.py` |
| hyperliquid | `x_hyperliquid_agent.py` |
| lighter | `x_lighter_agent.py` |
| raydium | `x_raydium_agent.py` |
| rise | `x_rise_agent.py` |

Actions offered in the wizard are capability-driven: an agent only shows the operations it advertises.

---

## Credentials

Credentials live in your **existing** Hermes environment file — normally `$HERMES_HOME/.env` (default `~/.hermes/.env`). They are never committed to this repository.

The installer never creates, copies, modifies, or prints `.env`. Installed exchange agents continue using the existing Hermes environment according to their current behaviour.

It also never modifies your Telegram token or chat IDs.

See [`.env.example`](.env.example) for the exact variable names each agent reads. That file is documentation only — it is never installed or read at runtime.

Accounts use the pattern `<EXCHANGE>_<ACCOUNT>_<FIELD>`, where `<ACCOUNT>` is an alias you choose. Account discovery is case-insensitive. An incomplete credential block is ignored: the account simply does not appear, and the gateway does not crash.

---

## Safety

- Installation verification is **offline**. It does not place or cancel orders.
- The verifier never contacts an exchange, sends a Telegram message, or prints a secret.
- The gateway is only restarted **after** verification passes, and never when `--no-restart` is supplied.
- Every shared Hermes file is backed up before it is patched, with SHA-256 recorded before and after.
- Patches are anchor-validated: if the expected surrounding code is missing or ambiguous, the installer **aborts and changes nothing** rather than guessing.
- Patched files are syntax-checked before being moved into place.

### What gets patched

KAM copies `plugins/trade/` and applies four small, marked insertions:

| File | Seams | Purpose |
|---|---|---|
| `plugins/platforms/telegram/adapter.py` | 3 | `/trade` command, `trade:` callbacks, wizard text interception |
| `hermes_cli/commands.py` | 1 | `/trade` appears in the Telegram command menu |

Every insertion is wrapped in markers:

```python
# BEGIN KAM TRADE PLUGIN (<seam>)
...
# END KAM TRADE PLUGIN (<seam>)
```

If a seam is already wired natively in your Hermes build, KAM detects it and leaves it untouched.

---

## Troubleshooting

**Hermes root not detected**
Pass it explicitly: `--hermes-root /path/to/hermes`. A directory is never accepted just because it is named "hermes" — it must contain `hermes_cli/main.py`, `hermes_cli/commands.py`, and `plugins/platforms/telegram/adapter.py`.

**Multiple Hermes installations found**
The installer stops on purpose. Re-run with `--hermes-root` naming the one you want.

**Telegram connected but `/trade` does not respond**
1. Restart the gateway: `systemctl restart hermes-gateway`
2. Run `./verify.sh --hermes-root /path/to/hermes`
3. Confirm the adapter seams are present:
   `grep -c 'plugins.trade.wizard' /path/to/hermes/plugins/platforms/telegram/adapter.py` → expect `3`

**`/trade` works when typed but is missing from the menu**
That is the `hermes_cli/commands.py` seam. Check for `CommandDef("trade"` in that file and re-run the installer.

**An exchange is missing from the wizard**
Confirm `plugins/trade/agents/x_<exchange>_agent.py` exists in the installed tree, then check the gateway log for `Failed to load agent` — usually a missing Python dependency.

**An exchange appears but has no accounts**
Credentials are missing or incomplete. Every variable in a block is required. Check `.env.example` for the exact names.

**Patch refused / "Refusing to patch"**
Your Hermes build differs from the verified baseline, so an anchor no longer matches uniquely. Nothing was changed. Report the Hermes commit so the anchors can be updated.

**Gateway restart failure**
```bash
systemctl status hermes-gateway
journalctl -u hermes-gateway -n 100 --no-pager
```
Then roll back by restoring from the newest backup directory (below), or run `sudo ./uninstall.sh`.

**Inspect installer backups**
```bash
ls -la /path/to/hermes/.kam-trade/backups/
cat  /path/to/hermes/.kam-trade/manifest.json
```
Each timestamped directory holds pre-change copies of every file KAM wrote or patched. `manifest.json` records copied files, patched files, SHA-256 before/after, versions, and the timestamp.

---

## Compatibility

Verified against:

| Item | Value |
|---|---|
| Hermes upstream | `NousResearch/hermes-agent` |
| Hermes commit | `e713518c45a3e518601321bf7d2d86431b97a78a` |
| Python | 3.11.15 |
| KAM version | 1.0.0 |
| Installer version | 1.0.0 |

Patch anchors were validated against this commit. On a different Hermes build the installer will still verify anchors before touching anything, and abort safely rather than guess.

---

## Repository layout

```
kam/
├── install.sh              # thin wrapper
├── verify.sh               # thin wrapper
├── uninstall.sh            # thin wrapper
├── .env.example            # documentation only
├── installer/
│   ├── install_trade.py
│   ├── install_fibo_capability.py
│   ├── verify_trade.py
│   ├── verify_fibo_capability.py
│   ├── uninstall_trade.py
│   ├── uninstall_fibo_capability.py
│   ├── kamlib.py           # discovery, patching, manifest
│   ├── patchspecs.py       # approved anchor definitions
│   ├── requirements.txt
│   └── manifest.json       # written on install
├── plugins/
│   └── trade/              # shipped verbatim, unmodified
│       ├── __init__.py     # no-op register(); direct dispatch is in the adapter
│       ├── plugin.yaml
│       ├── canonical.py
│       ├── tradedesk.py
│       ├── wizard.py       # /trade wizard
│       ├── fibo_wizard.py  # /fibo Telegram UI skeleton
│       ├── agents/
│       └── tests/
└── tests/
    └── test_installation.py
```

### Design note

There is deliberately **no `plugin.py` and no `router.py`**.

Hermes wires `/trade` by direct dispatch: the Telegram adapter imports `plugins.trade.wizard` from its own handlers. An earlier generic plugin-registration API existed upstream and was intentionally removed. Adding a synthetic entry point would invent an API that nothing calls. `plugins/trade/__init__.py` keeps a no-op `register()` purely so the plugin appears in `hermes plugins list`.

---

## Tests

```bash
# installer + packaging invariants
python -m pytest tests/ -q

# the shipped trade suite
python -m pytest plugins/trade/tests/ -q

# syntax
python -m compileall plugins installer tests
```

All tests are offline. None contacts an exchange or places an order.

---

## License

MIT — see [LICENSE](LICENSE).
