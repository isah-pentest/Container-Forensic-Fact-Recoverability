# Series R: retention-capacity robustness extension (k = 2)

Extension of protocol v3.0.1. It is frozen (`protocol/robustness/FREEZE_R.json`) and its freeze
record is registered (`protocol/robustness/registration_R.json`) before the first series-R run. The
v3.0.1 freeze, registration, schedule, evidence, scores and cited output sets are not changed, and
no confirmatory result is re-analysed.

## 1. Purpose
The v3.0.1 headroom rule `H < 1` classified every delayed-read record correctly, but it was evaluated
retrospectively, at one retained capacity (k = 1 backup of M = 10 MiB) and on one platform. Series R
tests prospectively whether the same rule, with predictions fixed before any run, locates the loss
boundary when the retained capacity is doubled (k = 2), with everything else held constant.

## 2. What changes and what does not
Changed: both producer-side stores keep k = 2 rotated backups (kube-apiserver `--audit-log-maxbackup=2`;
Tetragon `exportFileMaxBackups: 2`). The platform fingerprint is re-taken for this cluster.

Unchanged: M = 10 MiB per file for both stores; delta = 60 s; g = 10 s; epsilon = 5 s; the action, the
harness identity, the target image, the evidence contract, the blinded reconstruction and scoring, the
load generator, its replica count (18) and image, the host, the acquisition procedure (delayed reads
only, as in the calibration series), the growth monitor, the reference observers, the integrity and
replication procedure, and the frozen code that runs, scores and analyses each attempt. Series R uses
these frozen functions unchanged; its own code (`src/frx/robustness/`) only supplies the series
parameters, gates and outputs.

## 3. Levels (computed from the model only, before any run)
Four write-rate levels, four delayed-acquisition runs each (16 runs), with the record ages assumed when
the v3.0.1 operating points were set (an 8 s action with the exec 5 s after its start: F1 68 s, Tetragon
F2 63 s at the read). Rates come from the frozen Section 4.3 rule (`frx.orchestrator.qual.operating_points`)
applied with k = 2 to the frozen load map; R1 is the confirmatory moderate level itself.

| Level | Rule | Audit target (B/s) | Tetragon target (B/s) | Expected-offset H, F1 and Tetragon F2 | Formula P(loss), k = 2 | Formula P(loss) if k were 1 |
|---|---|---|---|---|---|---|
| R1 | confirmatory moderate rate and knob (unchanged) | 231303.5 | 249661.0 | 1.667 | 0.000 | 0.500 |
| R2 | expected-offset H = 1.2 | 321254.9 | 346751.3 | 1.200 | 0.083 | 1.000 |
| R3 | expected-offset H = 1.0 | 385505.9 | 416101.6 | 1.000 | 0.500 | 1.000 |
| R4 | expected-offset H = 0.8 | 481882.4 | 520127.0 | 0.800 | 1.000 | 1.000 |

At R1 the worst-case headroom (record at the end of the active file) is 1.333, so no primary record is
predicted to be lost for any offset. The per-replica load-generator settings are computed from the
frozen load map by the same rule and recorded in `plan.json`; if any setting exceeded the per-replica
maximum of the load script (20 API requests/s, 10 process starts/s) the plan is refused and this
amendment is revised before the freeze.

## 4. Qualification before the freeze (engineering checks; no study outcome)
1. The cluster is rebuilt with k = 2 for both stores and the flags are verified in the API-server
   manifest and the Tetragon Helm values.
2. The platform fingerprint of this cluster is taken (as root, as for v3.0.1) and recorded in `plan.json`;
   a session refuses to open if it differs.
3. One 20 min capacity run at R4 with the v3.0.1 capacity criteria (no OOM termination, peak memory at
   most 75% of RAM, 1 min load average not above the vCPU count, no Tetragon drops or audit backend
   errors, gap-free followers, follower bytes within 1% of store growth) must pass.
4. A store check on that run's 1 Hz growth samples must show that each store held at most, and at some
   point exactly, an active file and two backups, and that surplus files were deleted.

## 5. Schedule and execution
One block of 16 delayed runs in a seeded random order (NumPy PCG64, seed = v3.0.1 master seed + 29),
written to `schedule.csv` with the plan. Execution uses the frozen block runner with the v3.0.1 per-run
gates, replacement rule (an aborted or unscorable attempt is repeated at the same level at the end of the block; a third such attempt stops the block), package gate, OOM rule,
off-host replication and session records. Attempts are calibration-type attempts in the frozen
run-status schema: they receive CAL- identifiers numbered on from the shared append-only attempt ledger
and are stored in their own tree (`<evidence>/robustness/`). A block that is interrupted is recorded
incomplete and not resumed; the series is then reported as incomplete.

## 6. Scoring
After the block has closed, every scored attempt is scored by the frozen blinded reconstruction and
scorer, the scores are locked (`scores_lock_R.json`), and only then are sealed diagnostics opened for the
headroom and loss-stage analyses.

## 7. Registered tests (decided before any run)
- **R-T1 (prospective, deterministic).** For every delayed-read record-source pair (audit F1, F2, F3;
  Tetragon F2), the record is lost (state not CORRECT) if and only if its measured headroom H < 1, with
  B = (M - o) + kM and k = 2, measured exactly as in v3.0.1. Supported if every pair with a measured H
  is classified correctly; every misclassified pair is reported with its run, fact, source and H.
- **R-T2 (prospective, deterministic).** At R1 no primary record is lost. Supported if 0 of the R1
  record-source pairs are lost. (Under v3.0.1, with k = 1, the same rate lost the complete record in 5
  of 16 runs.)
- **R-T3 (calibration, descriptive).** Per level and fact-source, the observed loss proportion with its
  Wilson 95% interval is compared with the planned formula probability; the Brier score is reported.
  No significance test.
- **R-T4 (descriptive).** Every unrecovered pair receives a loss stage by the frozen rules.
- **R-C1 (contrast, descriptive).** The H < 1 rule evaluated with k = 1 instead of the configured k = 2
  is reported next to R-T1.

All results are reported whatever their direction. Deviations from this amendment are reported as such.

## 8. Outputs
`python -m frx.robustness analyze` writes `<evidence>/analysis_outputs/robustness/<commit>/`
(results_R.json, pairs_R.csv, pairs_R_k1_contrast.csv, loss_stages_R.csv, a LaTeX table body and a
figure), separate from every v3.0.1 output set.
