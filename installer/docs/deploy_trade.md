# Trade plugin deployment (`installer/deploy_trade.sh`)

Single, deterministic mechanism for syncing a KAM trade-plugin agent
into the live Hermes runtime.

## Background

Two services / trade plugins are running on this host:

| service | port | PYTHONPATH | resolves `plugins.trade.agents.x_vestmarkets_agent` to |
|---|---|---|---|
| `webtrade.service`  | 9001 | `/usr/local/lib/hermes-agent` | `/usr/local/lib/hermes-agent/plugins/trade/agents/x_vestmarkets_agent.py` |
| `webtrade2.service` | 9009 | `/root/kam`                  | `/root/kam/plugins/trade/agents/x_vestmarkets_agent.py`                  |

`webtrade.service` consumes the **runtime copy** at `/usr/local/lib/hermes-agent/plugins/trade/...`,
which is populated by the KAM installer (`installer/install_trade_capability.py`)
via `shutil.copy2`. Before this script existed, deployments were done with
a manual `cp` from the developer and a `systemctl restart webtrade`. That
workflow is brittle: nothing forces the operator to verify SHAs match, no
guard against dirty working-tree files, and no consistent report.

`installer/deploy_trade.sh` replaces that with a per-agent, allowlist-driven
operation that:

1. Computes the SHA256 of the source file under `/root/kam/plugins/trade/...`.
2. Computes the SHA256 of the runtime file under
   `/usr/local/lib/hermes-agent/plugins/trade/...`.
3. Reports MATCH YES/NO without copying (when `--check` is passed).
4. Atomically copies (temp file + `os.replace`) and clears the matching
   `__pycache__` entries (when `--deploy` is passed).
5. Restarts only the systemd units that actually load the changed module.
6. Re-checks SHA256 post-deploy and confirms the live interpreter's
   `importlib.util.find_spec` resolves to the runtime copy.

## Safety properties

* **Dirty-tree guard.** Uncommitted tracked edits AND untracked files
  under `plugins/trade/` are rejected unless `--allow-dirty` is passed.
  This protects against accidentally deploying in-progress work the
  developer did not intend to ship.
* **Allowlist.** `--agent vestmarkets` only touches
  `plugins/trade/agents/x_vestmarkets_agent.py` and its companion test.
  Unrelated dirty files (`x_apex_agent.py`, `canonical.py`, `wizard.py`,
  ...) are NEVER copied regardless of working-tree state.
* **Atomic write.** New files are written to `<dst>.<random>.tmp` then
  `os.replace`d, so a concurrent `import` cannot observe a half-written
  file.
* **Syntax preflight.** Each source file is `ast.parse`d before copy; a
  syntax error halts the operation.
* **No secret leakage.** FORBIDDEN_PATH_TOKENS rejects `/.env`, `.key`,
  `.pem`, `.sqlite`, `.db`, `__pycache__`, `.venv`, `node_modules`, etc.
* **No silent restart.** Restart defaults to "auto" (services listed in
  the deployment allowlist). Use `--restart=none` for offline staging.
* **Verification of the live interpreter.** Post-deploy, the script runs
  `importlib.util.find_spec('plugins.trade.agents.x_<agent>_agent')`
  inside a Python that has only `runtime_pythonpath` on `sys.path` and
  reports the resolved origin. That path MUST point at the runtime copy.

## Usage

```bash
# 1. CHECK — does the live runtime match the KAM repo?
./installer/deploy_trade.sh --agent vestmarkets --check

# 2. DEPLOY — copy source files atomically and restart relevant services.
./installer/deploy_trade.sh --agent vestmarkets --deploy

# 3. DEPLOY without restarting (offline staging).
./installer/deploy_trade.sh --agent vestmarkets --deploy --restart=none

# 4. Machine-readable JSON for scripting.
./installer/deploy_trade.sh --agent vestmarkets --check --json
```

### Check-mode output

```
== KAM trade deploy :: check ==
Git commit:         1790870347abc1234...
Source:             /root/kam
Runtime target:     /usr/local/lib/hermes-agent
Runtime PYTHONPATH: /usr/local/lib/hermes-agent
Agent:              x_vestmarkets_agent
Services (target):  webtrade.service, webtrade2.service
Runtime import path: /usr/local/lib/hermes-agent/plugins/trade/agents/x_vestmarkets_agent.py
Dirty source files: (none)

relpath                                                    src SHA-256   runtime SHA-256 match
--------------------------------------------------------------------------------------------------------------
plugins/trade/agents/x_vestmarkets_agent.py                abcdef123456  abcdef123456    YES
plugins/trade/agents/tests/test_x_vestmarkets_agent.py     012345abcdef  012345abcdef    YES

Notes:
- check mode: nothing copied, no service restarted.

Deployment needed: NO
```

### Deploy-mode output

```
== KAM trade deploy :: deploy ==
Git commit:         ...
...
Services:
  webtrade.service                   restarted               active    running
  webtrade2.service                  restarted               active    running
...
Deploy ok: True
```

## Exit codes

| code | meaning |
|---|---|
| 0 | OK / check-report MATCHED / deploy succeeded |
| 1 | check reports MISMATCH (operator did not ask to deploy) |
| 2 | deploy attempted but failed (dirty tree, syntax error, copy error, restart error) |
| 3 | bad CLI arguments |

## Adding a new agent

No new deploy code is needed. Pass `--agent=<name>` and the script resolves
`plugins/trade/agents/x_<name>_agent.py` automatically. The companion test
file at `plugins/trade/agents/tests/test_x_<name>_agent.py` is included if
present.

## Common pitfalls

* "Dirty source files present, deploy blocked" — your working tree has
  uncommitted edits. Either commit them, discard them, or pass
  `--allow-dirty`.
* "runtime SHA-256: -" — the runtime file does not exist yet; first deploy
  populates it.
* "source has syntax errors" — fix the file first, then re-run.
* "Services: webtrade.service failed: ..." — check `journalctl -u
  webtrade.service` for the real cause.

## Where this lives in the workflow

```
developer edits /root/kam/plugins/trade/agents/x_<name>_agent.py
                  │
                  ▼
           commits to git
                  │
                  ▼
   ./installer/deploy_trade.sh --agent <name> --check
                  │   (CI / pre-push safety)
                  │
                  ▼
  ./installer/deploy_trade.sh --agent <name> --deploy
                  │
                  ▼
   runtime SHA256 == source SHA256, services restarted, healthy
```