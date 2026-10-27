#!/usr/bin/env bash
# Run or resume one part of the qualification series Q (Blueprint v3.0, Section 18).
# Run inside tmux. Needs: operating_points.json and q_plan.json in the evidence store, a fresh
# harness token (scripts/g2_harness_identity.sh) and, if used, the audit reference receiver.
#
# Usage: ./scripts/g2_qual_part.sh PART      (shakedown | Q-a | Q-b | Q-c)
#   env: FRX_EVIDENCE (~/frx-evidence), FRX_STORES (~/frx-stores),
#        FRX_TARGET_IMAGE (digest; resolved from protocol/qualification.yaml tag if unset),
#        FRX_AUDIT_REFERENCE (~/frx-stores/reference/audit_webhook.jsonl; "none" to disable)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PART="${1:?part: shakedown | Q-a | Q-b | Q-c}"
EVID="${FRX_EVIDENCE:-$HOME/frx-evidence}"
STORES="${FRX_STORES:-$HOME/frx-stores}"
REF="${FRX_AUDIT_REFERENCE:-$STORES/reference/audit_webhook.jsonl}"
PY="$REPO/.venv/bin/python"
HK="$HOME/.frx/harness.kubeconfig"

[ "$(kubectl config current-context)" = "kind-frx" ] || { echo "kubectl context is not kind-frx"; exit 2; }
[ -z "${TMUX:-}" ] && echo "WARNING: not inside tmux; a dropped SSH session would stop this part."
[ -s "$HK" ] || { echo "no harness kubeconfig; run ./scripts/g2_harness_identity.sh"; exit 2; }
kubectl --kubeconfig "$HK" auth whoami >/dev/null 2>&1 || { echo "harness token expired; run ./scripts/g2_harness_identity.sh"; exit 2; }
[ -z "$(git -C "$REPO" status --porcelain --untracked-files=no)" ] || { echo "working tree modified; commit first"; exit 2; }

if [ -z "${FRX_TARGET_IMAGE:-}" ]; then
  TAG="$("$PY" -c 'import yaml;print(yaml.safe_load(open("'"$REPO"'/protocol/qualification.yaml"))["target_image_tag"])')"
  FRX_TARGET_IMAGE="$("$PY" -m frx.cli load-image "$TAG")"
fi
echo "target image: $FRX_TARGET_IMAGE"
# Section 6.3: images pre-pulled on the node (imagePullPolicy IfNotPresent), so no run waits on a pull
docker exec frx-control-plane crictl inspecti "$FRX_TARGET_IMAGE" >/dev/null 2>&1 \
  || docker exec frx-control-plane crictl pull "$FRX_TARGET_IMAGE"
REFARG=(); [ "$REF" != "none" ] && REFARG=(--audit-reference "$REF")

sudo -v
set +e
sudo env KUBECONFIG="$HOME/.kube/config" PATH="$PATH" "$PY" -m frx.cli qual-run --part "$PART" \
  --stores "$STORES" --harness-kubeconfig "$HK" --target-image "$FRX_TARGET_IMAGE" \
  --evidence "$EVID" "${REFARG[@]}"
RC=$?
set -e
sudo chown -R "$(id -u):$(id -g)" "$EVID/qualification" "$EVID/ledger" 2>/dev/null || true
echo "part $PART exit code: $RC (0 = part complete)"
exit $RC
