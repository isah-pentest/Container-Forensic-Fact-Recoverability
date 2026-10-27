# Gate G7 — independent analysis: runbook

Blueprint Sections 11, 12, 15.5–15.6, 17, 18 (G7). Everything is a repository command; nothing is
typed into a shell as an ad-hoc script. All commands read the locked evidence store and write only
where stated. Run from `~/frx-recoverability` with the venv active.

## 0. Preconditions
- Confirmatory scores locked (`scoring/scores_lock.json`) and the calibration series scored
  (`python3 -m frx.cli calibration-score`).
- The analysis layer is committed (outputs are tied to the analysis commit).

```bash
python3 -m frx.analysis status              # execution state from dispositions only
python3 -m frx.analysis frozen-diff         # "ok": true: only src/frx/analysis/* (and registration.json) changed since the freeze
python3 -m frx.analysis verify              # score lock, analysis input, every package and scoring manifest
```

## 1. Re-scoring check (frozen reconstruction and scoring, byte comparison)
```bash
python3 -m frx.analysis rescore-check       # every locked fact_answers.json and scores.json reproduced exactly
```

## 2. Every analysis, table and figure (one immutable set per analysis commit)
```bash
python3 -m frx.analysis all                 # -> ~/frx-evidence/analysis_outputs/<commit>/
```
Contents: `results.json` (primary via the frozen `frx analyze`, secondary M7–M13 and E2, validity
M14–M24, sensitivity S1–S9, E3 headroom calibration, E4 loss stages, qualification, pilot, power),
`runs.csv`, `e3_headroom.csv`, `e4_loss_stages.csv`, `claims_register.csv`, `tables/T1–T8`
(CSV and Markdown), `figures/F1–F7.svg`, `SHA256SUMS`.

## 3. Raw-to-fact audit (≥ 10 % of runs, stratified by cell; by a person)
```bash
python3 -m frx.analysis audit-sample        # -> analysis_outputs/raw_to_fact_audit.csv (seed = master_seed + 7)
```
For each sampled record, compare `raw_record` with `truth` and fill `auditor_verdict`
(MATCH / MISMATCH) and `auditor_note`. Keep the completed worksheet beside the blank one.

## 4. Clean-environment regeneration
On a second machine or a fresh clone and venv, with the evidence store (or the replica) mounted:
```bash
git clone <repo> && cd frx-recoverability && git checkout <analysis commit>
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.lock && pip install -e .
./reproduce.sh /path/to/frx-evidence
```
G7 passes when every stage succeeds (identical outputs) and the audit has no unexplained mismatch.
Tag `g7-analysis`.
