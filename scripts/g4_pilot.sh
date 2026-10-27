#!/usr/bin/env bash
# G4 pilot: run the next pilot block(s) in one session (Blueprint v3.0, Sections 12.4, 15.3, 18).
# Run inside tmux. Needs: pilot_schedule.csv (frx pilot-plan), a fresh harness token
# (scripts/g2_harness_identity.sh), the audit reference receiver, and a MOUNTED off-host replica.
#
# Usage: ./scripts/g4_pilot.sh [MAX_BLOCKS]        (default: all remaining pilot blocks)
#   env: FRX_REPLICA (required: mounted replica root), FRX_EVIDENCE (~/frx-evidence),
#        FRX_STORES (~/frx-stores), FRX_AUDIT_REFERENCE (~/frx-stores/reference/audit_webhook.jsonl)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
EVID="${FRX_EVIDENCE:-$HOME/frx-evidence}"
STORES="${FRX_STORES:-$HOME/frx-stores}"
REF="${FRX_AUDIT_REFERENCE:-$STORES/reference/audit_webhook.jsonl}"
REPLICA="${FRX_REPLICA:?set FRX_REPLICA to the mounted off-host replica root}"
PY="$REPO/.venv/bin/python"
HK="$HOME/.frx/harness.kubeconfig"
MAXB=(); [ -n "${1:-}" ] && MAXB=(--max-blocks "$1")

[ "$(kubectl config current-context)" = "kind-frx" ] || { echo "kubectl context is not kind-frx"; exit 2; }
[ -z "${TMUX:-}" ] && echo "WARNING: not inside tmux; a dropped SSH session would interrupt a block (it is then INCOMPLETE)."
[ -s "$HK" ] || { echo "no harness kubeconfig; run ./scripts/g2_harness_identity.sh"; exit 2; }
kubectl --kubeconfig "$HK" auth whoami >/dev/null 2>&1 || { echo "harness token expired; run ./scripts/g2_harness_identity.sh"; exit 2; }
[ -z "$(git -C "$REPO" status --porcelain --untracked-files=no)" ] || { echo "working tree modified; commit first"; exit 2; }
mountpoint -q "$REPLICA" || { echo "$REPLICA is not a mount point (off-host replica not mounted)"; exit 2; }
curl -sf "http://$(docker network inspect kind --format '{{range .IPAM.Config}}{{.Gateway}} {{end}}' | tr ' ' '\n' | grep -E '^[0-9.]+$'):9880/" >/dev/null \
  || { echo "audit reference receiver not answering; start it (see runbook)"; exit 2; }

IMG="$("$PY" -c 'import yaml;print(yaml.safe_load(open("'"$REPO"'/protocol/parameters.yaml"))["target_pod"]["image_digest"])')"
docker exec frx-control-plane crictl inspecti "$IMG" >/dev/null 2>&1 || docker exec frx-control-plane crictl pull "$IMG"

sudo -v
set +e
sudo env KUBECONFIG="$HOME/.kube/config" PATH="$PATH" "$PY" -m frx.cli pilot-run --stores "$STORES" \
  --harness-kubeconfig "$HK" --replica "$REPLICA" --audit-reference "$REF" --evidence "$EVID" "${MAXB[@]}"
RC=$?
set -e
sudo chown -R "$(id -u):$(id -g)" "$EVID/qualification/pilot" "$EVID/ledger" 2>/dev/null || true
echo "pilot exit code: $RC (0 = no stop rule fired)"
exit $RC
