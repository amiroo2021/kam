#!/usr/bin/env bash
# create_bundle.sh — build REQUIRED FiboLearn migration data archive
# Safe defaults: never includes secrets, never deletes sources.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAGING="${ROOT}/.staging_bundle"
BUNDLES="${ROOT}/bundles"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="fibolearn_required_data_${TS}.tar.gz"
OUT="${BUNDLES}/${NAME}"

FIBOLEARN_DB="${FIBOLEARN_DB:-/root/.hermes/fibolearn/fibolearn.sqlite}"
VALIDATION_DB="${VALIDATION_DB:-/root/kam/fibolearn/data/fl_vwap_005_binance_validation.sqlite}"
KLINES_DB="${KLINES_DB:-/root/kam/GoldenFibo/data/binance_klines.sqlite}"
BACKTEST_KLINES_DB="${BACKTEST_KLINES_DB:-/root/kam/GoldenFibo/data/backtest_klines.sqlite}"

need_bytes=0
for f in "$FIBOLEARN_DB" "$VALIDATION_DB" "$KLINES_DB" "$BACKTEST_KLINES_DB"; do
  [[ -f "$f" ]] || { echo "MISSING required/optional source: $f" >&2; exit 1; }
  need_bytes=$((need_bytes + $(stat -c%s "$f")))
done
# staging + tar + margin
need_bytes=$((need_bytes * 3 / 2 + 2*1024*1024*1024))
free_bytes=$(df -PB1 "$ROOT" | awk 'NR==2{print $4}')
echo "expected_sources_bytes=$need_bytes free_bytes=$free_bytes"
if (( free_bytes < need_bytes )); then
  echo "ERROR: insufficient free disk under $ROOT" >&2
  exit 1
fi

rm -rf "$STAGING"
mkdir -p "$STAGING/data" "$STAGING/reports" "$BUNDLES"

# Prefer hardlink to avoid doubling during stage when same filesystem
hl() {
  local src="$1" dst="$2"
  if ln "$src" "$dst" 2>/dev/null; then
    echo "hardlink $src -> $dst"
  else
    echo "copy $src -> $dst"
    cp -a "$src" "$dst"
  fi
}

hl "$FIBOLEARN_DB" "$STAGING/data/fibolearn.sqlite"
hl "$VALIDATION_DB" "$STAGING/data/fl_vwap_005_binance_validation.sqlite"
hl "$KLINES_DB" "$STAGING/data/binance_klines.sqlite"
hl "$BACKTEST_KLINES_DB" "$STAGING/data/backtest_klines.sqlite"

# Small checkpoint reports (also in Git; included for offline restore)
REPORT_SRC="${REPORT_SRC:-/root/kam/fibolearn/reports}"
for r in \
  fl_vwap_corrected_development_outcome_accounting.json \
  fl_vwap_005_invalidation.json \
  fl_vwap_005_validation_freeze.json \
  fl_vwap_005_raw_data_manifest.json \
  fl_vwap_005_final_summary.json \
  fl_vwap_focused_prospective_primitive_verification_summary.json \
  fl_vwap_prospective_treated_semantics.json \
  fl_vwap_post_landmark_strict_time_fix.json
do
  if [[ -f "$REPORT_SRC/$r" ]]; then
    cp -a "$REPORT_SRC/$r" "$STAGING/reports/$r"
  fi
done

# Exclude patterns safety check
if find "$STAGING" -iname '*secret*' -o -iname '*token*' -o -name 'auth.json' -o -name '.env' | grep -q .; then
  echo "ERROR: refused — staging contains secret-like names" >&2
  exit 1
fi

echo "Creating $OUT ..."
tar -C "$STAGING" -czf "$OUT" data reports
SHA=$(sha256sum "$OUT" | awk '{print $1}')
SIZE=$(stat -c%s "$OUT")
echo "bundle_path=$OUT"
echo "bundle_size_bytes=$SIZE"
echo "bundle_sha256=$SHA"

# write sidecar
cat > "${OUT}.sha256" <<EOF
$SHA  $(basename "$OUT")
EOF

rm -rf "$STAGING"
echo "staging cleaned"
echo "DONE"
