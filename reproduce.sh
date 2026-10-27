#!/usr/bin/env bash
# Regenerate everything from preserved evidence and compare (Blueprint Sections 15.6, 18 G7).
# Stops at the first failure. Usage: ./reproduce.sh EVIDENCE_DIR [OUTPUT_DIR]
#   1. unit tests          2. frozen-diff: only the post-execution analysis layer differs from the freeze
#   3. integrity: score lock, analysis input, every package and scoring manifest
#   4. re-run the frozen blinded reconstruction and scoring; locked answers and scores must match byte for byte
#   5. regenerate every analysis, table and figure into OUTPUT_DIR and compare with the committed output set
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
EVID="${1:?usage: ./reproduce.sh EVIDENCE_DIR [OUTPUT_DIR]}"
OUT="${2:-$(mktemp -d)/regenerated}"
export PYTHONPATH="$HERE/src${PYTHONPATH:+:$PYTHONPATH}"
A="python3 -m frx.analysis"

echo "== 1. unit tests";                         python3 -m pytest -q -p no:cacheprovider "$HERE/tests"
echo "== 2. changes since the frozen protocol";  $A frozen-diff > /dev/null || { $A frozen-diff; exit 2; }
echo "== 3. integrity re-verification";          $A verify --evidence "$EVID" > /dev/null || { $A verify --evidence "$EVID"; exit 3; }
echo "== 4. re-scoring from acquired bytes";     $A rescore-check --evidence "$EVID" > /dev/null || { $A rescore-check --evidence "$EVID"; exit 4; }
echo "== 5. regenerate analyses, tables, figures"
$A all --evidence "$EVID" --out "$OUT" > /dev/null
COMMIT="$(git -C "$HERE" rev-parse HEAD | cut -c1-12)"
REF="$EVID/analysis_outputs/$COMMIT"
if [ -d "$REF" ]; then
  $A compare "$REF" "$OUT" > /dev/null || { $A compare "$REF" "$OUT"; exit 5; }
  echo "regenerated output set is identical to $REF"
else
  echo "no committed output set for $COMMIT in $EVID/analysis_outputs; regenerated set left at $OUT"
fi
echo "== reproduction complete"
