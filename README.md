# KAM — `/trade` + `/fibo` add-on for Hermes

KAM is an installable `/trade` + `/fibo` add-on for an existing [Hermes](https://hermes-agent.nousresearch.com) node that is already connected to Telegram. It adds:

- `/trade`: the Telegram trading console wizard backed by a pluggable set of exchange agents
- `/fibo`: the Telegram Fibo control wizard backed by a persistent multi-registration `fibo.service`

**There is no enable flag.** If the add-on is installed, `/trade` is enabled. If you remove it, `/trade` is gone.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Existing Hermes installation | The installer refuses to run against anything that is not a real Hermes checkout |
| Working Telegram connection | KAM does not configure Telegram; it reuses your existing bot |
| Python 3.10+ | Uses the same interpreter your Hermes gateway runs |
| `git` | For clone and upgrade |
| Exchange credentials | Only for the exchanges you actually want to use — see [Credentials](#credentials) |
| root / sudo | Required to write into the Hermes tree and restart the service |

---

## Install

```bash
git clone https://github.com/amiroo2021/kam.git
cd kam
sudo ./install.sh --hermes-root /path/to/hermes
```

If Hermes is in a standard location you may omit `--hermes-root` and let it auto-detect. If several installations are found, the installer stops and asks you to choose one explicitly.

After installation and a gateway restart, send `/trade` or `/fibo` in Telegram. `fibo.service` is installed from the repository, enabled on a normal install, and owns all Fibo registrations independently of Telegram session lifetime.

### Options

| Flag | Effect |
|---|---|
| `--hermes-root PATH` | Target a specific Hermes installation |
| `--dry-run` | Show everything that would happen; change nothing |
| `--no-restart` | Install and verify, but leave the gateway alone |
| `--skip-deps` | Do not touch pip (useful when deps are already managed) |

---

## Dry run

Always safe. Detects paths, lists planned file operations, shows the intended patches, runs source-side checks, and installs nothing.

```bash
sudo ./install.sh --dry-run --hermes-root /path/to/hermes
```

---

## Verify

Offline and read-only. Never contacts an exchange, never places or cancels an order, never queries a balance or position, never sends a Telegram message.

```bash
./verify.sh --hermes-root /path/to/hermes
```

Prints `KAM /trade installation: PASS` or `FAIL` with the exact failed checks, and exits non-zero on failure.

---

## Upgrade

```bash
git pull
sudo ./install.sh --hermes-root /path/to/hermes
```

The installer is idempotent. Re-running it will not duplicate handlers, imports, patch blocks, service units, or dependency entries, and will not reset your configuration or remove credentials. Unchanged components are reported as already installed.

---

## Uninstall

```bash
sudo ./uninstall.sh --hermes-root /path/to/hermes
```

Removes only add-on-owned files and only the marked KAM blocks from shared Hermes files. It never deletes your `.env`, your credentials, unrelated plugins, or shared dependencies. Backups are preserved unless you pass `--purge-backups`. Supports `--dry-run` and `--no-restart`.

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
│   ├── verify_trade.py
│   ├── uninstall_trade.py
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
│       ├── wizard.py
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
