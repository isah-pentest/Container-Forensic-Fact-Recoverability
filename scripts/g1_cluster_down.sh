#!/usr/bin/env bash
# Delete the kind cluster and ARCHIVE (never delete) the store contents into the evidence store.
# Usage: ./scripts/g1_cluster_down.sh   env: FRX_EVIDENCE, FRX_STORES
set -euo pipefail
EVID="${FRX_EVIDENCE:-$HOME/frx-evidence}"
STORES="${FRX_STORES:-$HOME/frx-stores}"
TS="$(python3 -c 'import time; print(time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))')"
kind delete cluster --name frx || true
if [ -d "$STORES" ] && sudo find "$STORES" -type f 2>/dev/null | grep -q .; then
  DEST="$EVID/qualification/g1/stores_archived_$TS"
  mkdir -p "$DEST"
  sudo mv "$STORES/audit" "$STORES/tetragon" "$DEST/"
  sudo chown -R "$(id -u):$(id -g)" "$DEST"
  (cd "$DEST" && find . -type f -exec sha256sum {} + > SHA256SUMS)
  echo "stores archived to $DEST"
fi
mkdir -p "$STORES/audit" "$STORES/tetragon"
echo "cluster deleted; empty stores ready at $STORES"
