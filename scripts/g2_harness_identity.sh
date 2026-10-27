#!/usr/bin/env bash
# Create (or refresh) the harness identity and a short-lived token kubeconfig (Sections 7.1, 15.6).
# Run at the start of every session. The kubeconfig is kept outside the repository and the
# evidence store (mode 600) and is never copied into a package.
#
# Usage: ./scripts/g2_harness_identity.sh [DURATION]   (default 12h)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DUR="${1:-12h}"
OUT="$HOME/.frx/harness.kubeconfig"
[ "$(kubectl config current-context)" = "kind-frx" ] || { echo "kubectl context is not kind-frx"; exit 2; }
kubectl apply -f "$REPO/cluster/manifests/harness-rbac.yaml"
TOKEN="$(kubectl create token frx-harness -n frx-system --duration="$DUR")"
SERVER="$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.server}')"
CA="$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"
mkdir -p "$HOME/.frx"; chmod 700 "$HOME/.frx"
umask 077
cat > "$OUT" <<KCFG
apiVersion: v1
kind: Config
current-context: frx-harness
clusters:
- name: kind-frx
  cluster: {server: "$SERVER", certificate-authority-data: "$CA"}
users:
- name: frx-harness
  user: {token: "$TOKEN"}
contexts:
- name: frx-harness
  context: {cluster: kind-frx, user: frx-harness}
KCFG
chmod 600 "$OUT"
echo "== identity"
kubectl --kubeconfig "$OUT" auth whoami -o jsonpath='{.status.userInfo.username}'; echo
echo "== permissions (expect yes, yes, yes, yes, no)"
for a in "create namespaces" "create pods -n default" "delete pods -n default" "create pods/exec -n default" "list secrets -A"; do
  printf '%-32s ' "$a"; kubectl --kubeconfig "$OUT" auth can-i $a || true
done
echo "token valid for $DUR; kubeconfig: $OUT"
