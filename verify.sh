#!/usr/bin/env bash
# KAM modular verifier dispatcher.
#
# Usage:
#   ./verify.sh --trade
#   ./verify.sh --fibo
#   ./verify.sh --trade --fibo
#   ./verify.sh                           # no-flag = --trade
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

pick_python() {
  local root=""
  local prev=""
  for arg in "$@"; do
    if [ "$prev" = "--hermes-root" ]; then root="$arg"; fi
    case "$arg" in --hermes-root=*) root="${arg#*=}" ;; esac
    prev="$arg"
  done
  if [ -n "$root" ] && [ -x "$root/venv/bin/python" ]; then
    echo "$root/venv/bin/python"; return
  fi
  for candidate in /usr/local/lib/hermes-agent/venv/bin/python python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"; return
    fi
    if [ -x "$candidate" ]; then echo "$candidate"; return; fi
  done
  echo "ERROR: no usable Python interpreter found" >&2
  exit 1
}

PY="$(pick_python "$@")"
exec "$PY" "$REPO_DIR/installer/installer.py" --action verify "$@"