#!/usr/bin/env bash
# Series R (k = 2 robustness extension): the 16-run session. Run inside tmux, on the k = 2 cluster.
# Needs: series R frozen and registered (python -m frx.robustness verify ok), fresh harness token,
# audit reference receiver running, off-host replica mounted (scripts/mount_replica.sh).
#   env: FRX_REPLICA (/mnt/frx-replica), FRX_EVIDENCE (~/frx-evidence), FRX_STORES (~/frx-stores),
#        FRX_AUDIT_REFERENCE (~/frx-stores/reference/audit_webhook.jsonl)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
EVID="${FRX_EVIDENCE:-$HOME/frx-evidence}"
STORES="${FRX_STORES:-$HOME/frx-stores}"
REF="${FRX_AUDIT_REFERENCE:-$STORES/reference/audit_webhook.jsonl}"
REPLICA="${FRX_REPLICA:-/mnt/frx-replica}"
PY="$REPO/.venv/bin/python"
HK="$HOME/.frx/harness.kubeconfig"

[ "$(kubectl config current-context)" = "kind-frx" ] || { echo "kubectl context is not kind-frx"; exit 2; }
[ -z "${TMUX:-}" ] && { echo "run inside tmux (an interrupted block is INCOMPLETE and never resumed)"; exit 2; }
"$PY" -m frx.robustness verify >/dev/null || { "$PY" -m frx.robustness verify; echo "series R not frozen/registered as committed"; exit 2; }
docker exec frx-control-plane grep -q -- "--audit-log-maxbackup=2" /etc/kubernetes/manifests/kube-apiserver.yaml \
  || { echo "the cluster does not keep 2 audit backups (rebuild with FRX_AUDIT_BACKUPS=2)"; exit 2; }
[ -s "$HK" ] || { echo "no harness kubeconfig; run ./scripts/g2_harness_identity.sh"; exit 2; }
kubectl --kubeconfig "$HK" auth whoami >/dev/null 2>&1 || { echo "harness token expired; run ./scripts/g2_harness_identity.sh"; exit 2; }
mountpoint -q "$REPLICA" || { echo "$REPLICA is not mounted; run ./scripts/mount_replica.sh"; exit 2; }
curl -sf "http://$(docker network inspect kind --format '{{range .IPAM.Config}}{{.Gateway}} {{end}}' | tr ' ' '\n' | grep -E '^[0-9.]+$'):9880/" >/dev/null \
  || { echo "audit reference receiver not answering"; exit 2; }
IMG="$("$PY" -c 'import yaml;print(yaml.safe_load(open("'"$REPO"'/protocol/parameters.yaml"))["target_pod"]["image_digest"])')"
docker exec frx-control-plane crictl inspecti "$IMG" >/dev/null 2>&1 || docker exec frx-control-plane crictl pull "$IMG"

sudo -v
set +e
sudo env KUBECONFIG="$HOME/.kube/config" PATH="$PATH" "$PY" -m frx.robustness run --stores "$STORES" \
  --harness-kubeconfig "$HK" --replica "$REPLICA" --audit-reference "$REF" --evidence "$EVID"
RC=$?
set -e
sudo chown -R "$(id -u):$(id -g)" "$EVID" 2>/dev/null || true
echo "series R exit code: $RC (0 = no stop rule fired)"
exit $RC
