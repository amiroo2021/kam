#!/usr/bin/env bash
# restore_data.sh — restore REQUIRED data from bundle
# NEVER overwrites existing destination files unless --force is passed.
set -euo pipefail

BUNDLE=""
DEST_ROOT="/root"
FORCE=0
EXTRACT_TMP=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle) BUNDLE="$2"; shift 2 ;;
    --dest-root) DEST_ROOT="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --extract-tmp) EXTRACT_TMP="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$BUNDLE" && -f "$BUNDLE" ]] || {
  echo "Usage: $0 --bundle path.tar.gz [--dest-root /root] [--force]" >&2
  exit 2
}

DEST_ROOT="$(readlink -f "$DEST_ROOT")"
EXTRACT_TMP="${EXTRACT_TMP:-$DEST_ROOT/kam/.scratch/fibolearn-restore-$$}"
mkdir -p "$EXTRACT_TMP"
cleanup() { rm -rf "$EXTRACT_TMP"; }
trap cleanup EXIT

echo "Extracting to $EXTRACT_TMP"
tar -xzf "$BUNDLE" -C "$EXTRACT_TMP"

declare -A MAP=(
  ["data/fibolearn.sqlite"]="$DEST_ROOT/.hermes/fibolearn/fibolearn.sqlite"
  ["data/fl_vwap_005_binance_validation.sqlite"]="$DEST_ROOT/kam/fibolearn/data/fl_vwap_005_binance_validation.sqlite"
  ["data/binance_klines.sqlite"]="$DEST_ROOT/kam/GoldenFibo/data/binance_klines.sqlite"
  ["data/backtest_klines.sqlite"]="$DEST_ROOT/kam/GoldenFibo/data/backtest_klines.sqlite"
)

for rel in "${!MAP[@]}"; do
  src="$EXTRACT_TMP/$rel"
  dst="${MAP[$rel]}"
  if [[ ! -f "$src" ]]; then
    echo "skip missing member $rel"
    continue
  fi
  mkdir -p "$(dirname "$dst")"
  if [[ -e "$dst" && "$FORCE" -ne 1 ]]; then
    echo "REFUSE overwrite (exists): $dst  (pass --force to replace)"
    continue
  fi
  if [[ -e "$dst" && "$FORCE" -eq 1 ]]; then
    echo "FORCE replace $dst"
    rm -f "$dst"
  fi
  cp -a "$src" "$dst"
  echo "restored $rel -> $dst"
done

# optional reports into kam tree if missing
if [[ -d "$EXTRACT_TMP/reports" ]]; then
  mkdir -p "$DEST_ROOT/kam/fibolearn/reports"
  for f in "$EXTRACT_TMP/reports"/*; do
    [[ -f "$f" ]] || continue
    base=$(basename "$f")
    dst="$DEST_ROOT/kam/fibolearn/reports/$base"
    if [[ -e "$dst" && "$FORCE" -ne 1 ]]; then
      echo "report exists, skip $dst"
      continue
    fi
    cp -a "$f" "$dst"
    echo "restored report $base"
  done
fi

echo "RESTORE_DONE force=$FORCE"
