#!/usr/bin/env bash
# G1: create the single-node kind cluster with a file-backed rotating audit log and install
# Tetragon with a rotating JSON export file (Blueprint v3.0, Sections 3.1, 6.3, 14).
# Everything it renders, logs and fingerprints is kept under the evidence store.
#
# Usage: ./scripts/g1_cluster_up.sh [TETRAGON_CHART_VERSION]
#   env: FRX_EVIDENCE (default ~/frx-evidence), FRX_STORES (default ~/frx-stores)
# Store sizes are provisional (10 MB x 5 backups) until G3 fixes them by the Section 4.3 rule.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
EVID="${FRX_EVIDENCE:-$HOME/frx-evidence}"
STORES="${FRX_STORES:-$HOME/frx-stores}"
TV="${1:-}"
TS="$(python3 -c 'import time; print(time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))')"
OUT="$EVID/qualification/g1/cluster_$TS"
export PYTHONPATH="$REPO/src${PYTHONPATH:+:$PYTHONPATH}"
NODE=frx-control-plane
step() { printf '\n== %s\n' "$*"; }

case "$STORES$EVID" in /mnt/*|*OneDrive*) echo "stores/evidence must be on the WSL filesystem"; exit 2;; esac
if kind get clusters 2>/dev/null | grep -qx frx; then
  echo "cluster 'frx' already exists. Run ./scripts/g1_cluster_down.sh first."; exit 2
fi
if [ -n "$(ls -A "$STORES/audit" 2>/dev/null)$(ls -A "$STORES/tetragon" 2>/dev/null)" ]; then
  echo "stores at $STORES are not empty. Run ./scripts/g1_cluster_down.sh (it archives them)."; exit 2
fi
mkdir -p "$OUT" "$STORES/audit" "$STORES/tetragon"
sudo -v   # audit files are root-only (0600); one password prompt up front

step "render configs"
python3 -m frx.cli cluster-config --stores "$STORES" --out-dir "$OUT" | tee "$OUT/render.json"

step "create kind cluster (pinned node image)"
kind create cluster --config "$OUT/kind-config.yaml" --wait 180s 2>&1 | tee "$OUT/kind_create.log"
[ "$(kubectl config current-context)" = "kind-frx" ] || { echo "context is not kind-frx"; exit 3; }

step "verify audit flags reached the API server"
docker exec "$NODE" cat /etc/kubernetes/manifests/kube-apiserver.yaml > "$OUT/kube-apiserver.yaml"
for f in audit-policy-file audit-log-path audit-log-format audit-log-maxsize audit-log-maxbackup audit-log-maxage; do
  grep -q -- "--$f=" "$OUT/kube-apiserver.yaml" || { echo "MISSING apiserver flag --$f"; exit 4; }
done
grep -- "--audit" "$OUT/kube-apiserver.yaml" | sed 's/^ *//' | tee "$OUT/audit_flags.txt"

step "verify the audit store is being written on the host"
sleep 5
sudo ls -la "$STORES/audit" | tee "$OUT/audit_store_ls.txt"
sudo test -s "$STORES/audit/audit.json" || { echo "audit.json missing or empty in $STORES/audit"; exit 5; }

step "install Tetragon"
helm repo add cilium https://helm.cilium.io >/dev/null 2>&1 || true
helm repo update cilium | tee "$OUT/helm_repo_update.log"
if [ -z "$TV" ]; then
  TV="$(helm search repo cilium/tetragon -o json | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["version"])')"
  echo "no chart version given; latest is $TV (recorded; pass it explicitly from now on)"
fi
echo "$TV" > "$OUT/tetragon_chart_version.txt"
helm install tetragon cilium/tetragon -n kube-system --version "$TV" -f "$OUT/tetragon-values.yaml" 2>&1 | tee "$OUT/helm_install.log"
kubectl rollout status -n kube-system ds/tetragon --timeout=300s | tee -a "$OUT/helm_install.log"

step "verify the Tetragon store is being written on the host"
sleep 15
ls -la "$STORES/tetragon" | tee "$OUT/tetragon_store_ls.txt"
test -s "$STORES/tetragon/tetragon.log" || { echo "tetragon.log missing or empty in $STORES/tetragon"; exit 6; }

step "fingerprint"
python3 -m frx.cli fingerprint --out "$OUT/fingerprint.json"

step "done"
echo "cluster up. Evidence for this bring-up: $OUT"
echo "next: python3 -m frx.cli smoke --stores $STORES --out-dir $EVID/qualification/g1 --image debian:trixie-slim"
