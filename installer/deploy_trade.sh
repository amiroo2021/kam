#!/usr/bin/env bash
# Hardened deployment of a single KAM trade-plugin agent into the live
# Hermes runtime. See ``installer/deploy_trade.py`` for full docs.
#
# Usage:
#   ./installer/deploy_trade.sh --agent vestmarkets --check
#   ./installer/deploy_trade.sh --agent vestmarkets --deploy
#   ./installer/deploy_trade.sh --agent vestmarkets --deploy --restart=none
#   ./installer/deploy_trade.sh --agent hyperliquid --check --json
#
# Exit codes:
#   0  ok / no deploy needed (check) / deployed (deploy)
#   1  check reports MISMATCH (operator did not ask for --deploy)
#   2  deploy attempted but failed (dirty tree, syntax error, etc.)
#   3  bad CLI args
set -euo pipefail

# Resolve repo root from this script's location, regardless of caller cwd.
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

# Prefer the hermes-agent venv's python (matches what webtrade.service uses).
PYTHON_BIN="${HERMES_AGENT_PYTHON:-/usr/local/lib/hermes-agent/venv/bin/python}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "FATAL: $PYTHON_BIN not found." >&2
  exit 3
fi

# Add the repo root to PYTHONPATH so the package ``installer.deploy_trade``
# resolves. ``installer/__init__.py`` already exposes it.
exec "${PYTHON_BIN}" -m installer.deploy_trade "$@"