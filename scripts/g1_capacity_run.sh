#!/usr/bin/env bash
# Section 14.2 capacity run. G1: provisional ceiling load (defaults below). G3: re-run at the
# highest calibration rate. Run inside tmux: it takes MINUTES + about 3 minutes.
#
# Usage: ./scripts/g1_capacity_run.sh [MINUTES]
#   env: FRX_EVIDENCE (~/frx-evidence), FRX_STORES (~/frx-stores),
#        FRX_LOAD_IMAGE (python:3.14-slim), FRX_LOAD_REPLICAS (20),
#        FRX_LOAD_EXEC (10 per replica per s), FRX_LOAD_API (10 per replica per s),
#        FRX_CAPACITY_LABEL (G1-provisional-ceiling)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
EVID="${FRX_EVIDENCE:-$HOME/frx-evidence}"
STORES="${FRX_STORES:-$HOME/frx-stores}"
MINUTES="${1:-20}"
TAG="${FRX_LOAD_IMAGE:-python:3.14-slim}"
REPLICAS="${FRX_LOAD_REPLICAS:-20}"
EXEC="${FRX_LOAD_EXEC:-10}"
API="${FRX_LOAD_API:-10}"
LABEL="${FRX_CAPACITY_LABEL:-G1-provisional-ceiling}"
PY="$REPO/.venv/bin/python"
OUT_ROOT="$EVID/qualification/g1"

[ "$(kubectl config current-context)" = "kind-frx" ] || { echo "kubectl context is not kind-frx"; exit 2; }
[ -z "${TMUX:-}" ] && echo "WARNING: not inside tmux; a dropped SSH session would stop this run."
mkdir -p "$OUT_ROOT"

echo "== resolving load image digest for $TAG"
IMAGE="$("$PY" -m frx.cli load-image "$TAG")"
echo "load image: $IMAGE"

sudo -v
echo "== capacity run: $MINUTES min, $REPLICAS replicas x ($EXEC exec/s + $API api/s), label $LABEL"
set +e
sudo env KUBECONFIG="$HOME/.kube/config" PATH="$PATH" "$PY" -m frx.cli capacity \
  --stores "$STORES" --out-root "$OUT_ROOT" --minutes "$MINUTES" --image "$IMAGE" \
  --replicas "$REPLICAS" --exec-per-s "$EXEC" --api-per-s "$API" --label "$LABEL"
RC=$?
set -e
sudo chown -R "$(id -u):$(id -g)" "$OUT_ROOT"/capacity_* 2>/dev/null || true
echo "capacity exit code: $RC (0 = all Section 14.2 criteria passed)"
exit $RC
