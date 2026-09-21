#!/usr/bin/env bash
# verify_bundle.sh — read-only verification of migration archive
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE=""
EXTRACT_ROOT="${EXTRACT_ROOT:-/root/kam/.scratch/fibolearn-migration-verify}"
MANIFEST="${ROOT}/MIGRATION_MANIFEST.json"
CLEAN=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle) BUNDLE="$2"; shift 2 ;;
    --extract-root) EXTRACT_ROOT="$2"; shift 2 ;;
    --no-clean) CLEAN=0; shift ;;
    --manifest) MANIFEST="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$BUNDLE" && -f "$BUNDLE" ]] || { echo "Usage: $0 --bundle path/to/bundle.tar.gz" >&2; exit 2; }

echo "=== archive members ==="
tar -tzf "$BUNDLE" | sed -n '1,80p'
echo "(total members: $(tar -tzf "$BUNDLE" | wc -l))"

for need in \
  data/fibolearn.sqlite \
  data/fl_vwap_005_binance_validation.sqlite
do
  if ! tar -tzf "$BUNDLE" | grep -F "$need" >/dev/null; then
    echo "MISSING member $need" >&2
    exit 1
  fi
  echo "member_ok $need"
done

BSHA=$(sha256sum "$BUNDLE" | awk '{print $1}')
echo "bundle_sha256=$BSHA"
if [[ -f "$MANIFEST" ]]; then
  python3 - "$MANIFEST" "$BSHA" <<'PY'
import json,sys
m=json.load(open(sys.argv[1]))
exp=m.get("bundle",{}).get("sha256")
got=sys.argv[2]
if exp and exp!=got:
    print("FAIL bundle sha mismatch", exp, got); sys.exit(1)
print("manifest_bundle_sha_ok", bool(exp))
PY
fi

rm -rf "$EXTRACT_ROOT"
mkdir -p "$EXTRACT_ROOT"
tar -xzf "$BUNDLE" -C "$EXTRACT_ROOT"

EXTRACT_ROOT="$EXTRACT_ROOT" MANIFEST="$MANIFEST" python3 <<'PY'
import hashlib, json, os, sqlite3, sys
from pathlib import Path
extract = Path(os.environ["EXTRACT_ROOT"])
manifest_path = Path(os.environ["MANIFEST"])
m = json.load(open(manifest_path)) if manifest_path.exists() else {}
files = {f["bundle_member"]: f for f in m.get("files", []) if f.get("bundle_member")}

def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

required = [
    "data/fibolearn.sqlite",
    "data/fl_vwap_005_binance_validation.sqlite",
]
rc = 0
for rel in required:
    p = extract / rel
    if not p.exists():
        print("FAIL missing", rel); rc = 1; continue
    digest = sha256(p)
    print(f"physical_sha256 {rel}={digest} size={p.stat().st_size}")
    meta = files.get(rel)
    if meta and meta.get("physical_sha256") and meta["physical_sha256"] != digest:
        print("FAIL sha mismatch for", rel, "expected", meta["physical_sha256"]); rc = 1
    else:
        print("sha_ok", rel)
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    ic = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    print("integrity", rel, ic)
    if ic != "ok":
        rc = 1
for rel in ["data/binance_klines.sqlite", "data/backtest_klines.sqlite"]:
    p = extract / rel
    if p.exists():
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        ic = con.execute("PRAGMA integrity_check").fetchone()[0]
        con.close()
        print("integrity", rel, ic)
        if ic != "ok":
            rc = 1
sys.exit(rc)
PY

if [[ "$CLEAN" -eq 1 ]]; then
  rm -rf "$EXTRACT_ROOT"
  echo "cleaned extract root $EXTRACT_ROOT"
fi
echo "VERIFY_BUNDLE_OK"
