# Amendment v3.0.1 (engineering) — confirmatory launcher, cohort scoring, calibration launcher

Date: 2026-10-27, before any confirmatory (RUN-) ID existed. Supersedes the v3.0 freeze
(`protocol/freeze_history/FREEZE_v3.0.json`, registered as a hash commitment on OSF, see
`protocol/freeze_history/registration_v3.0.json`).

## Reason
The v3.0 freeze contained every module that defines a run, a block, scoring and the analysis, but no
command that issues RUN- IDs: the frozen block runner was reachable only through the pilot command
(PILOT- IDs, pilot folders). Adding the launcher changes tracked code, so under Section 16 it is a new
protocol version, frozen and registered before execution.

## What changes
Engineering only. New code, no edits to any module that decides an outcome:
- `src/frx/orchestrator/cohort.py` (new): confirmatory loop over the frozen schedule, one block per call
  to the frozen block runner; cohort-level stop rules of Section 10.4 evaluated from dispositions
  only (per-cell ceiling as x / (B + x) > 0.15; collector-failure imbalance > 3); blinded NC/decoy
  false-attribution check between blocks from reconstruction answers alone (Section 8.3: any answer
  for an identity that never acted), so no sealed truth is opened during execution; cohort scoring
  only after every block has closed, score lock (`scoring/scores_lock.json`) and analysis export
  (`analysis_outputs/scores.csv`, collector failures as Y = 0 per Section 10.1); calibration
  schedule (16 D runs, 4 per level CAL1–CAL4, seed = master_seed + 13, Section 13).
- `src/frx/cli.py`: commands `amend-begin`, `cohort-run` (calls `require_freeze()` first),
  `cohort-score`, `calibration-plan`, `calibration-run` (refuse before the score lock),
  `calibration-score`.
- `src/frx/freeze.py`: amendment chain: the superseded freeze and registration move to
  `protocol/freeze_history/`; the new FREEZE.json records what it supersedes and hashes the history
  files and this note; protocol version read from parameters.yaml.
- `scripts/g6_session.sh`, `protocol/gates/G6_RUNBOOK.md`, tests.
- `protocol/parameters.yaml`: `protocol_version` 3.0 → 3.0.1 (no other value changes).

## What does not change
Design, factors and levels, operating points, B = 16, master seed and randomization schedule,
contract, scoring states and union rule, estimands, statistical analysis and stop rules, package
layout and gates. The run, block, platform, harness, acquisition, reconstruction and scoring
modules are byte-identical to v3.0 (verifiable: `git diff protocol-v3.0-frozen protocol-v3.0.1-frozen --stat`).
