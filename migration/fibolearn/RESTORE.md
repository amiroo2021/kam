# Restore procedure (fresh Ubuntu server)

Assumes:

- Fresh Ubuntu 22.04+ / 24.04+ / 26.04
- GitHub SSH or HTTPS access to `amiroo2021/kam`
- Migration **data bundle** delivered separately (not in Git)
- Secrets supplied **out of band** (never from the bundle)

## 1. Clone repository

```bash
sudo mkdir -p /root/kam
sudo git clone git@github.com:amiroo2021/kam.git /root/kam
cd /root/kam
git fetch origin main
```

### Research checkpoint pin (FiboLearn accounting closed)

```bash
git rev-parse 1a074275f31ec4b804c1929c1e7431bbe2bcdbe1
# optional: work from a branch/tag at this commit for pure research replay
# git switch -c fibolearn-checkpoint-1a07427 1a074275f31ec4b804c1929c1e7431bbe2bcdbe1
```

`1a07427` is an ancestor of modern `main`. Prefer **current `origin/main`** for GoldenFibo/runtime code, and treat `1a07427` as the **research accounting checkpoint** that closed development prospective outcome counts.

Verify ancestor relationship:

```bash
git merge-base --is-ancestor 1a074275f31ec4b804c1929c1e7431bbe2bcdbe1 HEAD && echo OK
```

## 2. Install system packages

```bash
sudo apt-get update
sudo apt-get install -y python3.11 python3.11-venv git curl build-essential sqlite3
```

Install `uv` (used by GoldenFibo):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# or copy uv binary; clinic used: /root/.hermes/bin/uv
```

## 3. Install GoldenFibo Python env

```bash
cd /root/kam/GoldenFibo
uv venv --python python3.11 .venv
uv pip install -e ".[dev]"
./.venv/bin/python -c "import goldenfibo; print('ok')"
```

FiboLearn is plain package code under `/root/kam/fibolearn` (no separate setup.py required for most research scripts). Ensure `PYTHONPATH` includes monorepo root when needed:

```bash
export PYTHONPATH=/root/kam${PYTHONPATH:+:$PYTHONPATH}
```

## 4. Restore REQUIRED non-Git data

Copy the migration bundle to the new host (example path):

```bash
# on new server
mkdir -p /root/kam/migration/fibolearn/bundles
# scp/rsync the .tar.gz here
```

Verify free disk (**need ≥ 15 GiB free** for extract + copies):

```bash
df -h /root
```

Run restore (**refuses to overwrite existing targets unless forced**):

```bash
cd /root/kam/migration/fibolearn
bash scripts/restore_data.sh \
  --bundle bundles/fibolearn_required_data_YYYYMMDD.tar.gz \
  --dest-root /root
```

Force overwrite only when intentional:

```bash
bash scripts/restore_data.sh --bundle ... --dest-root /root --force
```

Expected destinations after restore:

| Bundle member | Destination |
|---------------|-------------|
| `data/fibolearn.sqlite` | `/root/.hermes/fibolearn/fibolearn.sqlite` |
| `data/fl_vwap_005_binance_validation.sqlite` | `/root/kam/fibolearn/data/fl_vwap_005_binance_validation.sqlite` |
| `data/binance_klines.sqlite` (optional GF) | `/root/kam/GoldenFibo/data/binance_klines.sqlite` |
| `data/backtest_klines.sqlite` (optional GF) | `/root/kam/GoldenFibo/data/backtest_klines.sqlite` |

## 5. Restore secrets manually (never from bundle)

See `SECRET_CHECKLIST.md`. Minimum for Hermes agent + Telegram:

- Hermes `auth.json` / provider OAuth or API keys
- `TELEGRAM_BOT_TOKEN`, allowlists/home channel
- Optional: Photon, exchange keys only if trading is needed

**Do not** paste secret values into Git, chat logs destined for Git, or the migration bundle.

Example locations on clinic (names only):

- `/root/.hermes/.env`
- `/root/.hermes/auth.json`
- `/root/.hermes/config.yaml` (recreate non-secret structure; inject secrets separately)

## 6. Hermes runtime (optional for pure FiboLearn offline research)

Clinic ran Hermes Agent **v0.21.2** from `/usr/local/lib/hermes-agent` via:

```text
python -m hermes_cli.main gateway run
```

with `HERMES_HOME=/root/.hermes`.

Install Hermes per current upstream docs: https://hermes-agent.nousresearch.com/docs

Separate:

- Hermes **runtime/config** (`~/.hermes/config.yaml`, `auth.json`, platform tokens)
- FiboLearn **research data** (`~/.hermes/fibolearn/`)

Do **not** blindly rsync entire `~/.hermes`.

## 7. GoldenFibo service (optional)

Unit template used on clinic (`/etc/systemd/system/goldenfibo.service`):

```ini
[Unit]
Description=GoldenFibo web application
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/kam/GoldenFibo
Environment=PYTHONUNBUFFERED=1
ExecStart=/root/kam/GoldenFibo/.venv/bin/goldenfibo-web
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Only enable if the new server should run live GoldenFibo.

## 8. Post-restore verification

```bash
cd /root/kam/migration/fibolearn
bash scripts/verify_bundle.sh --bundle bundles/<bundle>.tar.gz
python3 scripts/verify_checkpoint_counts.py \
  --accounting /root/kam/fibolearn/reports/fl_vwap_corrected_development_outcome_accounting.json
```

Then follow `VERIFY.md` for DB integrity and test commands.

## 9. What restore does NOT do

- Does not start FL-VWAP-006
- Does not calculate VWAP effects
- Does not modify GoldenFibo math
- Does not copy secrets
- Does not delete existing DBs unless `--force`
