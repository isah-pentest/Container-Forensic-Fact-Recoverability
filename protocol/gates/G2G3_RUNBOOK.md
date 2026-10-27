# Gates G2 + G3 — qualification series Q: runbook

Blueprint v3.0, Sections 4.3, 7.4, 13, 14.2, 18 and 22.2. Run every step on the study VM, inside
tmux, from `~/frx-recoverability` with the venv active. Evidence goes to `~/frx-evidence/qualification/`.
Steps 1–4 look only at store metadata and settings; no Q outcome exists until step 8, and nothing
fixed in steps 4–6 may change after that.

## 1. Harness identity (every session)
```bash
./scripts/g2_harness_identity.sh        # 12 h token in ~/.frx/harness.kubeconfig (mode 600, never packaged)
```
Expect the identity `system:serviceaccount:frx-system:frx-harness` and `yes yes yes yes no`.

## 2. Pin images by digest (Section 6.3, 22.2)
```bash
TARGET=$(python3 -m frx.cli load-image debian:trixie-slim)
LOADIMG=$(python3 -m frx.cli load-image python:3.14-slim)
echo "$TARGET"; echo "$LOADIMG"
```

## 3. Load map (metadata only, ~8 min)
Store write rate against load-generator rates, with exec and API rates varied independently
(2×2 + centre), so the fit separates each knob's effect on each store.
```bash
python3 -m frx.cli load-map --stores ~/frx-stores --image "$LOADIMG" --replicas 20 \
  --out ~/frx-evidence/qualification/load_map.json
```

## 4. Store capacity (M_q, k_q) — a documented decision (Section 4.3 item 1)
Operating points follow from the store size by rule. Compute them for each candidate (offline, seconds):
```bash
mkdir -p ~/frx-evidence/qualification/candidates
for c in "10 5" "10 2" "10 1"; do set -- $c
  python3 -m frx.cli operating-points --load-map ~/frx-evidence/qualification/load_map.json \
    --audit-max-mb $1 --audit-backups $2 --tetragon-max-mb $1 --tetragon-backups $2 \
    --rationale "candidate ${1}MiBx$2" --out ~/frx-evidence/qualification/candidates/ops_${1}x$2.json
done
```
Choose the configuration with a cited rationale (pinned-version default, published guide, or a
stated operational budget) whose highest rate (H ≈ 0.5 for Q-c and calibration) the host can
sustain. Record the choice and reason in `protocol/gates/G3.md` before step 6.

## 5. Rebuild the cluster only if the sizes change (and, optionally, add the audit reference observer)
```bash
./scripts/g1_cluster_down.sh            # archives the stores; never deletes
# optional reference observer (Section 3.4): start the receiver first, in its own tmux window
python3 -m frx.cli ref-receiver --listen 172.18.0.1:9880      # kind network gateway; check with: docker network inspect kind
FRX_AUDIT_MAX_MB=.. FRX_AUDIT_BACKUPS=.. FRX_TETRAGON_MAX_MB=.. FRX_TETRAGON_BACKUPS=.. FRX_SIZES_FINAL=1 \
  FRX_AUDIT_WEBHOOK=http://172.18.0.1:9880 ./scripts/g1_cluster_up.sh 1.7.1
python3 -m frx.cli smoke --stores ~/frx-stores --out-dir ~/frx-evidence/qualification/g3 --image debian:trixie-slim
./scripts/g2_harness_identity.sh
```
Without the webhook, leave `FRX_AUDIT_WEBHOOK` unset and use `FRX_AUDIT_REFERENCE=none` in step 7;
loss stage then stays UNRESOLVED for audit (Section 21, limitation 5).

## 6. Final operating points and the Q plan (written once)
```bash
python3 -m frx.cli operating-points --load-map ~/frx-evidence/qualification/load_map.json \
  --audit-max-mb .. --audit-backups .. --tetragon-max-mb .. --tetragon-backups .. \
  --rationale "<cited reason>" --out ~/frx-evidence/qualification/operating_points.json
python3 -m frx.cli qual-plan --seed <recorded number>
```
`qual-run` refuses to start if `operating_points.json` changes after the plan is written.

## 7. Run Q (about 1.5–1.8 h of cluster time; resumable)
```bash
./scripts/g2_qual_part.sh shakedown     # 3 archived QUAL- attempts; not part of Q
./scripts/g2_qual_part.sh Q-a           # 12 low-load C runs
./scripts/g2_qual_part.sh Q-b           # 12 low-load D runs
./scripts/g2_qual_part.sh Q-c           # 9 D runs at H ~ 2, 1, 0.5 (seeded order)
```
Each part deploys one load Deployment, runs its slots, replaces non-scored attempts at the end
(at most 3 per part) and stops on any failed package gate or two consecutive OOM runs.
Re-running a part resumes it.

## 8. Score and evaluate
```bash
python3 -m frx.cli qual-score           # blinded reconstruction in an isolated process, then scoring
python3 -m frx.cli qual-report          # G2 and G3 criteria, each with numerator/denominator
```
G2 decisions (Section 7.4, 22.2) and G3 values (timeouts, reference observer, r_L, r_M, M_q, k_q,
calibration rates) are then written into `contract.yaml` / `parameters.yaml` and tagged `g3-qualified`,
after the G3 capacity re-run at the highest calibration rate (20 min, Section 14.2).

## Primary scoring variant for Q
F3 requires the harness identity as actor (`protocol/qualification.yaml`, `primary_variant`):
at G1 the kubelet also deleted the target pod about 4.5 s after the harness. All F3 variants are
scored side by side and reported.
