# Amendment F: post-termination flooding series (v0.2, approved 20 November 2026; frozen by FREEZE_F.json)

Supplements protocol 3.0.1 (`protocol/parameters.yaml`) in the same way as Amendment R. It does not change the confirmatory series, the calibration series, series R, their code, their evidence or their analysis outputs. Code base: `main` at `96c7b5e276c4acac5445bc30e94ec9b4748fc7d2`; series F code on branch `flooding-series`. Frozen references that stay untouched: `v1.0.1-submission` (8292ab3), `series-R-frozen` (a5a9526), `v1.1-robustness` (4095c26).

Changes from v0.1 (draft of the same day): OSF registration dropped by author decision; the public record is the commit that adds `FREEZE_F.json`, tagged `series-F-frozen` and pushed to the public repository before the first counted run (§10). Flood settings fixed to frozen calibration settings (§4.3). Continuous-arm flood ends with continuous acquisition (§4.2). W measured directly (§8). F-D1 restricted to metadata; F-C2 added.

---

## 1. Purpose and questions

Round-1 review asked whether the retention mechanism reported in the manuscript holds when the log volume is increased deliberately after an action. Series R varied a constant background rate. Series F varies only the volume written **after** the action, in a burst that starts at termination.

- **F-Q1.** When the bytes written between a record and the delayed read arrive as a burst that starts at termination, does the rule "record lost if and only if measured headroom H < 1" still classify every record–source pair correctly? The rule depends on the bytes written, not on their distribution in time; series F tests that property with a rate profile that series R did not use.
- **F-Q2.** Does the continuous arm still recover every primary fact, with no follower gaps, when the burst starts at termination?
- **F-Q3.** Does the burst alone, without an action, produce a false attribution?

Series F does not re-test H < 1 classification under constant load (series R did) and makes no claim about the timing effect Δ_A.

## 2. Held constant

| Element | Value | Source |
|---|---|---|
| Kubernetes, kind, kubectl | v1.37.0, v0.33.0, v1.37.0 | parameters.yaml, bootstrap script |
| Tetragon chart | 1.7.1 | parameters.yaml |
| Store size M | 10 MiB, both stores | parameters.yaml |
| Backups k | 1, both stores (as the confirmatory series; series R used 2) | parameters.yaml |
| Delay δ, tolerance | 60 s, ±2 s | parameters.yaml |
| Grace g (continuous arm) | 10 s | parameters.yaml |
| Warm-up, settle, manipulation window | 20 s, 10 s, 15 s | parameters.yaml |
| Manipulation tolerance | ±20 % of target, both sources (locked) | parameters.yaml |
| Load-generator replicas | 18 for every run, including no-flood runs and controls | parameters.yaml |
| Pre-termination load | frozen Low (L) setting for every run | parameters.yaml |
| Run, block, scoring and mechanism code | frozen functions, unmodified | 96c7b5e |

Because the replica count is the same in every run, the number of load pods, and the pod-level telemetry they cause, does not differ between levels; only the rate read from the `frx-load-rate` ConfigMap changes.

## 3. Platform differences (recorded, not controlled)

Series F runs on frx-hv (Hyper-V guest). frx-ubuntu stays powered off and unmodified while the manuscript is under review.

| Item | Confirmatory and series R host | Series F host |
|---|---|---|
| Machine | frx-ubuntu | frx-hv (locked arm, fingerprint DC3FD076…) |
| Kernel | as recorded in the series-R fingerprint | 7.0.0-38-generic |
| Docker | as recorded | 29.1.3 |
| Helm | as recorded | 4.3.2 (snap, not pinned) |
| Clock | as recorded | chrony, PHC0 `/dev/ptp_hyperv` refclock |
| kdump | as recorded | purged |

The equivalence gate (§6) is the control for these differences.

## 4. Flood manipulation

### 4.1 Why new code is needed
`execute_run` sets the load rate once, before the action, and has no step after `TERMINATION_CONFIRMED`. Adding one would change frozen code. Series F adds a separate controller (`src/frx/flooding/controller.py`) and leaves `run.py` unchanged.

### 4.2 Controller
- A thread in the session process, beside the frozen block runner.
- Watches each new run package's `timing/event_journal.jsonl` (flushed per record) for `TERMINATION_CONFIRMED`, which is also written for negative-control runs.
- On detection calls the existing `frx.load.set_rate` with the run's flood setting; the load script reads it at its next ConfigMap GET.
- Restores the run's L setting at **t_term + 50 s** (the read is at t_term + 60 s), or earlier when the run ends its acquisition first: at `ACQ_END_C` in the continuous arm (t_term + g = 10 s), or at `ACQ_START_D`, `ERROR`, `PRE_ACTION_ABORT`, `SNAPSHOT_SEALED`, `CLEANUP` or `PACKAGE_CLOSED`, and at once if a later run starts. A flood never spans two runs.
- Records every step (detection time, patch acknowledgements, restore time and reason) in `<evidence>/flooding/controller/<run_id>.jsonl`, outside the write-once run packages.
- Never reads ground truth, acquired streams or store contents, and never writes to the stores.

### 4.3 Levels
All flood settings are frozen protocol v3.0.1 settings, so no new knob is introduced and G1 measures every setting used. Predicted W uses the frozen targets and timing assumptions (action 8 s, exec 5 s after create): W = r_L × (time from record to termination) + r_flood × 50 s + r_L × 10 s.

| Level | Flood setting | Rule (k = 1, M = 10 MiB) | Predicted W / (k+1)M, range over the four pairs | Predicted outcome |
|---|---|---|---|---|
| F0 | none (L throughout) | W ≤ 0.8 kM | 0.12–0.14 | retained |
| F1 | CAL1 | W ≤ 0.8 kM | 0.30–0.33 | retained for any offset |
| F2 | CAL3 | kM < W < (k+1)M | 0.71–0.77 | depends on the offset |
| F3 | CAL4 | W ≥ 1.1 (k+1)M | 1.12–1.22 | lost for any offset |

## 5. Qualification (before the freeze; not counted)

1. Cluster up on frx-hv with k = 1 for both stores; host fingerprint recorded.
2. Capacity run at the CAL4 setting with 18 replicas (`scripts/g1_capacity_run.sh`) and the k = 1 store check (`python -m frx.flooding store-check`).
3. Plan and schedule written once by `python -m frx.flooding plan`, after G1 passes.

## 6. Equivalence gates

- **G1 (before the plan).** `python -m frx.flooding g1` measures the frozen settings L, M, CAL1, CAL2, CAL3 and CAL4 on frx-hv with 18 replicas using the frozen `load_map` function. Each measured rate must lie within the locked ±20 % of its frozen target, for both stores. If G1 fails, no plan is written and series F stops; the failure is reported.
- **G2 (after scoring).** The four F0 runs recover every primary record. If G2 fails, F-T1 to F-C1 are reported descriptively with the failure, not as tests.

## 7. Design

| Cell | Arm | Flood | Action | Runs |
|---|---|---|---|---|
| CAL | delayed | F0 | yes | 4 |
| CAL | delayed | F1 | yes | 4 |
| CAL | delayed | F2 | yes | 4 |
| CAL | delayed | F3 | yes | 4 |
| CAL | continuous | F3 | yes | 6 |
| NC | delayed | F3 | none (negative control) | 4 |
| **Total** | | | | **26** |

- One block `CAL-F-BLOCK-01`; order drawn with numpy `PCG64(master_seed + 31)` (offsets 13 and 29 are used by the calibration series and series R).
- The frozen run-status schema admits only the v3.0.1 prefixes and cohorts, so series-F attempts are calibration-type attempts (`CAL-` IDs). They are numbered in a ledger under the series-F evidence root and are unique within that root; every output labels them series F.
- Aborted or unscorable attempts are repeated at the end of the block by the frozen block rules.

## 8. Registered tests and descriptive outputs

H = ((M − o) + kM) / W with k = 1. o is the record's offset at write time, interpolated with the pre-termination rate. W is the bytes written to the store between the 1 Hz growth-monitor samples that bracket the record and the read; it exceeds the bytes written between record and read by at most one second of writes at each end.

| ID | Statement | Decision |
|---|---|---|
| F-T1 | For every delayed record–source pair of the positive runs at F0–F3 (audit F1, F2, F3; Tetragon F2), the record is lost (state not CORRECT) if and only if H < 1. | Supported if every pair with a measured H is classified correctly; each misclassified pair is reported. |
| F-T2 | No primary record is lost at F0 or F1. | Supported if 0 pairs are lost. |
| F-T3 | Every F3 continuous-arm run recovers every primary fact (Y = 1) with zero follower gaps. | Supported if all scored runs meet it and no collector failure occurs. |
| F-C1 | No F3 negative-control run yields a false attribution. | Supported if none does. |
| F-D1 | Bytes written and files present per store during the flood window, per run (growth-monitor metadata). | Descriptive. |
| F-D2 | Controller latency (TERMINATION_CONFIRMED to rate patch acknowledged) and flood duration. | Descriptive. |
| F-C2 | H with the frozen series-R estimator (one mean rate over [action − 15 s, read] × age) next to F-T1. | Accuracy reported; not a test. |

The tests are deterministic predictions with small n. No p-value is computed and no loss proportion is generalized beyond these settings.

## 9. Disclosures

- **Actor stand-in.** The load generator stands in for an actor who floods the logs. Attribution of the flood to an actor is not tested.
- **Continuous-arm window.** The continuous arm reads until t_term + 10 s, so it is exposed to the first 10 s of the flood. Whether a rotation falls inside that window is reported per run; F-T3 is not a claim about long floods.
- **Single host.** Results are for frx-hv only.
- **Code digest.** The series-R `verify` command compares the tracked-code digest with the series-R freeze, so it reports a change on this branch; series R is verified at its own tag.

## 10. Isolation of evidence and order of operations

- Evidence root `~/frx-evidence-F`; replica share `frx-replica-F` mounted at `/mnt/frx-replica-F`. The session script refuses the v3.0.1/series-R evidence root and replica.
- The 31 October snapshot (SHA-256 `ffa98349…c559e`), `C:\frx-replica` and frx-ubuntu are not read or written.
- Order: G1 → capacity run and store check → plan → commit → freeze → commit → tag `series-F-frozen` → push → counted session → score → analyze → release `v1.2-flooding`. Deviations go to `protocol/flooding/DEVIATIONS_F.md`.
