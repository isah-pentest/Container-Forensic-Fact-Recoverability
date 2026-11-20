#!/usr/bin/env bash
# Series F (post-termination flood, k = 1): the 26-run session. Run inside tmux, on the k = 1 cluster.
# Needs: series F frozen, committed and tagged series-F-frozen (python -m frx.flooding verify ok),
# fresh harness token, audit reference receiver running, the series-F replica share mounted:
#   FRX_REPLICA=/mnt/frx-replica-F FRX_REPLICA_SHARE=frx-replica-F ./scripts/mount_replica.sh
#   env: FRX_REPLICA (/mnt/frx-replica-F), FRX_EVIDENCE (~/frx-evidence-F), FRX_STORES (~/frx-stores),
#        FRX_AUDIT_REFERENCE (~/frx-stores/reference/audit_webhook.jsonl)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
EVID="${FRX_EVIDENCE:-$HOME/frx-evidence-F}"
STORES="${FRX_STORES:-$HOME/frx-stores}"
REF="${FRX_AUDIT_REFERENCE:-$STORES/reference/audit_webhook.jsonl}"
REPLICA="${FRX_REPLICA:-/mnt/frx-replica-F}"
PY="$REPO/.venv/bin/python"
HK="$HOME/.frx/harness.kubeconfig"

[ "$(kubectl config current-context)" = "kind-frx" ] || { echo "kubectl context is not kind-frx"; exit 2; }
[ -z "${TMUX:-}" ] && { echo "run inside tmux (an interrupted block is INCOMPLETE and never resumed)"; exit 2; }
case "$REPLICA" in */frx-replica) echo "refusing: $REPLICA is the v3.0.1/series-R replica"; exit 2;; esac
case "$EVID" in */frx-evidence) echo "refusing: $EVID is the v3.0.1/series-R evidence root"; exit 2;; esac
"$PY" -m frx.flooding verify >/dev/null || { "$PY" -m frx.flooding verify; echo "series F not frozen/tagged as committed"; exit 2; }
docker exec frx-control-plane grep -q -- "--audit-log-maxbackup=1" /etc/kubernetes/manifests/kube-apiserver.yaml \
  || { echo "the cluster does not keep 1 audit backup (rebuild with FRX_AUDIT_BACKUPS=1 FRX_TETRAGON_BACKUPS=1)"; exit 2; }
[ -s "$HK" ] || { echo "no harness kubeconfig; run ./scripts/g2_harness_identity.sh"; exit 2; }
kubectl --kubeconfig "$HK" auth whoami >/dev/null 2>&1 || { echo "harness token expired; run ./scripts/g2_harness_identity.sh"; exit 2; }
mountpoint -q "$REPLICA" || { echo "$REPLICA is not mounted (see the header of this script)"; exit 2; }
curl -sf "http://$(docker network inspect kind --format '{{range .IPAM.Config}}{{.Gateway}} {{end}}' | tr ' ' '\n' | grep -E '^[0-9.]+$'):9880/" >/dev/null \
  || { echo "audit reference receiver not answering"; exit 2; }
IMG="$("$PY" -c 'import yaml;print(yaml.safe_load(open("'"$REPO"'/protocol/parameters.yaml"))["target_pod"]["image_digest"])')"
docker exec frx-control-plane crictl inspecti "$IMG" >/dev/null 2>&1 || docker exec frx-control-plane crictl pull "$IMG"

sudo -v
set +e
sudo env KUBECONFIG="$HOME/.kube/config" PATH="$PATH" "$PY" -m frx.flooding run --stores "$STORES" \
  --harness-kubeconfig "$HK" --replica "$REPLICA" --audit-reference "$REF" --evidence "$EVID"
RC=$?
set -e
sudo chown -R "$(id -u):$(id -g)" "$EVID" 2>/dev/null || true
echo "series F exit code: $RC (0 = no stop rule fired and no controller error)"
exit $RC
