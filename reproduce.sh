#!/usr/bin/env bash
# Regenerate everything from preserved evidence (Blueprint v3.0, Section 15.6).
# Stops at the first failure. Usage: ./reproduce.sh [EVIDENCE_DIR]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
EVID="${1:-$HERE/evidence}"
export PYTHONPATH="$HERE/src${PYTHONPATH:+:$PYTHONPATH}"

echo "== 1. unit tests"
python3 -m pytest -q "$HERE/tests"

echo "== 2. protocol structure and freeze readiness"
python3 -m frx.cli freeze-check || { echo "protocol not frozen yet (placeholders remain) — stopping"; exit 2; }

[ -d "$EVID" ] || { echo "no evidence directory at $EVID — nothing more to reproduce"; exit 0; }

echo "== 3. verify every package hash and completeness"
for pkg in "$EVID"/qualification/QUAL-* "$EVID"/qualification/PILOT-* "$EVID"/runs/RUN-* "$EVID"/calibration/CAL-*; do
  [ -d "$pkg" ] || continue
  python3 -m frx.cli verify-package "$pkg" > /dev/null || { echo "FAILED: $pkg"; exit 3; }
done

echo "== 4. re-score, re-analyse and regenerate tables/figures"
# Implemented with the reconstruction and reporting modules before G7:
#   frx reconstruct --all ; frx score --all ; frx analyze evidence/scoring/scores.csv --seed <frozen>
#   frx report --out evidence/analysis_outputs/<commit>/
echo "stage 4 commands are added before G7"
