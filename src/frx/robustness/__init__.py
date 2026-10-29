"""Robustness series R: a separately registered extension of protocol v3.0.1.

The series repeats the delayed-acquisition procedure of the calibration series with the retained
backup count changed from k = 1 to k = 2, everything else held constant (M = 10 MiB, delta = 60 s,
action, contract, scoring, load generator and host). Its predictions are computed from the
retention-headroom model alone and are frozen and registered before the first run, so the series is
a prospective test of the model at a second retention capacity.

Nothing here changes the frozen v3.0.1 code, protocol files, evidence or cited output sets: the
series uses the frozen run, block, scoring and mechanism functions unchanged, with its own
parameters (protocol/robustness/plan.json), its own freeze and registration records
(protocol/robustness/), its own evidence subtree (<evidence>/robustness/; attempts are appended to the shared append-only attempt ledger) and its own outputs
(<evidence>/analysis_outputs/robustness/<commit>/).

    python -m frx.robustness --help
"""
