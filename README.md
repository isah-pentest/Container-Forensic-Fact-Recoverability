# frx-recoverability

Protocol tooling for **Forensic Fact Recoverability After Pod Termination: Effects of Acquisition Timing and Background Event Load on Retained Kubernetes Audit and eBPF Telemetry** — Blueprint Manual v3.0 (design-locked), in `protocol/`.

**Status:** `g0-scaffold`. Design locked; parameters are set at gates G1–G5 by the rules in Blueprint Section 22. No confirmatory (`RUN-`) IDs may be issued until `protocol/FREEZE.json` exists.

## Ground rules

- **Self-contained.** This repository was initialized empty. Nothing is copied from any earlier repository, run, dataset or script, and no earlier result is an input to any step.
- **No machine learning.** Operating points come from a closed-form formula, randomization is a seeded PRNG, reconstruction is a deterministic rule-based matcher, and inference uses randomization and classical statistics.
- **Evidence never enters git.** Evidence lives in a write-once store with an off-host replica (Blueprint Section 15). `evidence/` and `stores/` are git-ignored.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock && pip install -e .
pytest                      # 41 tests on synthetic fixtures
frx freeze-check            # lists what still blocks freeze (expected: G1-G5 placeholders)
frx headroom --m 1000000 --k 1 --elapsed 60   # Section 4.3 operating-point arithmetic
```

## What is implemented now vs. at gates

| Module | Status | Blueprint section |
| --- | --- | --- |
| `frx.metrics` — union rule, Y, C, cell proportions, Δ, block contrasts, F2 tables | Done, tested | 11 |
| `frx.stats` — randomization test (exact / Monte Carlo), block t CI, MOVER-Wilson, Wilson, Clopper–Pearson, McNemar, fallback trigger | Done, tested | 12 |
| `frx.power` — Monte Carlo power grid and the B-selection rule | Done, tested against the exact binomial value | 12.5 |
| `frx.randomization` — seeded five-slot block schedule | Done, tested | 8.2 |
| `frx.headroom` — H, predicted loss, operating-point rates | Done, tested | 4 |
| `frx.integrity` — write-once files, manifests, read-only lock, package and block gates, ledger | Done, tested | 15 |
| `frx.status` — disposition rules and `run_status.json` schema | Done, tested | 10 |
| `frx.contract` — contract/parameter checks and freeze readiness | Done, tested | 6, 7, 22 |
| `frx.harness`, `frx.acquisition`, `frx.orchestrator`, `frx.reconstruct` | Interfaces only; build and test on fixtures, then qualify | 7, 3, 9/19, 7 → G1–G3 |
| `cluster/` — kind config, audit policy, Tetragon values, target pod, load generator | Drafts; confirm key names against pinned versions | 3, 6 → G1–G3 |

## Gate workflow

| Gate | What you do | Repo commands / files | Tag |
| --- | --- | --- | --- |
| G0 | Literature matrix; this protocol | `protocol/` | `g0-scaffold` |
| G1 | Pin host, kind, images; capacity run (20 min) | fill `<G1>` values; `software/` in the evidence store | `g1-platform` |
| G2 + G3 | Qualification series Q (33 `QUAL-` runs); fix contract details, store capacity, rates | `frx headroom ...`; fill `<G2>`/`<G3>` values | `g3-qualified` |
| G4 | 3 pilot blocks (`PILOT-`) | `frx verify-package`, `frx verify-block` | `g4-pilot` |
| G5 | Power simulation; seed; schedule; FREEZE.json; registration | `frx power ...`, `frx schedule ...`, `frx freeze-check` must exit 0 | `protocol-v3.0-frozen` |
| G6 | Confirmatory blocks, then calibration series (`CAL-`) | orchestrator; gates after every run and block | — |
| G7 | Clean-environment regeneration, ≥ 10% raw-to-fact audit | `./reproduce.sh <evidence>` ; `frx analyze` | `g7-analysis` |
| G8 | Sanitized release | `release/` in the evidence store | `g8-release` |

## Analysis input format

`frx analyze scores.csv --seed <frozen>` expects one row per scored confirmatory run:

```csv
run_id,block,cell,Y
RUN-0001,BLOCK-01,LD,1
```

Controls (`NC`) may be present and are excluded automatically. It reports cell proportions with Wilson intervals, H1 (randomization p, block t CI or the structure-triggered MOVER-Wilson CI), H2 under the fixed-sequence rule, and the exploratory interaction.

## Notes for negative-control runs

For `NC` runs, `action_verified` means the harness verified that **no** action was performed, and `termination_verified` is true by definition. Controls are never in the denominator of Y.

## Licence

To be chosen by the authors before public release (Blueprint Section 22.2).
