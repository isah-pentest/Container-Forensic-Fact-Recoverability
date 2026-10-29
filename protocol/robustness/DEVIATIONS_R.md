# Series R: deviations from AMENDMENT_R.md (recorded before scoring)

1. Evidence root and attempt identifiers. The session was started with FRX_EVIDENCE set to
   ~/frx-evidence/robustness, so the series used that directory as its evidence root: packages are in
   ~/frx-evidence/robustness/robustness/runs/, sessions and progress in .../robustness/robustness/sessions/,
   and attempts were numbered in a new attempt ledger at ~/frx-evidence/robustness/ledger/attempt_ledger.csv
   instead of being appended to the shared ledger. The 16 series-R attempts therefore carry the identifiers
   CAL-0001 to CAL-0016, which are also used by the v3.0.1 calibration series in its own tree
   (~/frx-evidence/calibration/). Series-R attempts are identified by their tree and block (CAL-R-BLOCK-01);
   in reports they are referred to as series-R runs. No v3.0.1 file, package or ledger row was written.
   The schedule, levels, store configuration (k = 2), fingerprint, gates, stop rules and replication were as
   registered; the off-host replica is at <replica>/robustness/runs/.
2. Scoring and analysis are run with --evidence ~/frx-evidence/robustness accordingly.

Recorded on 2026-10-29, after the session closed and before the scores were unsealed.
