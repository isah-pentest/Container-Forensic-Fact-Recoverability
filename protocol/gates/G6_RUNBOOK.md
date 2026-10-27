# Gate G6 — confirmatory execution: runbook (protocol v3.0.1)

Blueprint Sections 8.2, 10.4, 15.3, 19 and amendment v3.0.1. 16 blocks × (LC, LD, MC, MD, NC) from
`protocol/randomization_schedule.csv`; ≈ 9.2 min per block (≈ 2.5 h in total). A block never spans
sessions; sessions can be of any size (e.g. 4 blocks ≈ 40 min).

## Before every session
```bash
cd ~/frx-recoverability && source .venv/bin/activate
python3 -m frx.cli freeze-verify            # "ok": true
./scripts/mount_replica.sh                  # self-test OK
./scripts/g2_harness_identity.sh            # fresh 12 h token
curl -s http://172.18.0.1:9880/; echo       # audit reference receiver answering (tmux window "ref")
```
Workstation on AC power, no pending Windows restart; the session runs inside tmux.

## Run a session
```bash
./scripts/g6_session.sh 4                   # next 4 blocks (omit the number for all remaining)
```
Between blocks the launcher checks, without opening any sealed truth: NC targets and decoys answered
by nothing (blinded reconstruction), per-cell abort/unscorable ceiling, collector-failure balance.
Any stop rule ends the session with "stop and audit"; write an audit note before resuming
(Section 10.4). Repeat sessions until no block remains.

## After the last block: unseal, score, lock
```bash
python3 -m frx.cli cohort-score             # refuses while any block is still open
SEED=$(python3 -c "import yaml;print(yaml.safe_load(open('protocol/parameters.yaml'))['design']['master_seed'])")
python3 -m frx.cli analyze ~/frx-evidence/analysis_outputs/scores.csv --seed "$SEED" --out ~/frx-evidence/analysis_outputs/primary.json
```

## Calibration series (after the score lock; ≈ 40 min)
```bash
python3 -m frx.cli calibration-plan
./scripts/g6_session.sh calibration
python3 -m frx.cli calibration-score
```
