"""python -m frx.robustness <command>

Before the freeze (engineering qualification on the k = 2 cluster; no study outcome is produced):
  levels --load-map PATH           R1-R4 targets, per-replica knobs and model predictions (prints only)
  store-check CAPACITY_RUN_DIR     the store held exactly k = 2 backups and deleted surplus files
  plan --load-map PATH --fingerprint-hash H --capacity-report PATH --store-check PATH
                                   write protocol/robustness/plan.json and schedule.csv (once)
Freeze and registration:
  freeze                           write protocol/robustness/FREEZE_R.json (committed tree required)
  register --url URL --registered-at ISO   write protocol/robustness/registration_R.json
  verify                           ready to issue ROB- IDs?
Execution and analysis (after registration):
  run --stores DIR --harness-kubeconfig F --replica DIR [--audit-reference F] [--evidence DIR]
  score [--evidence DIR]           blinded scoring with the frozen scorer, then the series-R score lock
  analyze [--evidence DIR]         registered tests R-T1..R-T4, contrast R-C1, table, figure
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _print(o) -> None:
    print(json.dumps(o, indent=2, default=str))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m frx.robustness", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["levels", "store-check", "plan", "freeze", "register", "verify", "run",
                                       "score", "analyze"])
    p.add_argument("paths", nargs="*")
    p.add_argument("--evidence", default="~/frx-evidence")
    p.add_argument("--load-map")
    p.add_argument("--fingerprint-hash")
    p.add_argument("--capacity-report")
    p.add_argument("--store-check")
    p.add_argument("--url")
    p.add_argument("--registered-at")
    p.add_argument("--out")
    p.add_argument("--stores")
    p.add_argument("--harness-kubeconfig")
    p.add_argument("--replica")
    p.add_argument("--audit-reference")
    a = p.parse_args(argv)
    from . import design, series
    from .freeze import check, write_freeze, write_registration
    if a.command == "levels":
        lm = json.loads(Path(a.load_map).read_text())
        lv = design.levels(lm["fit"], series.frozen_params())
        _print({k: v for k, v in lv.items() if k != "load_fit"})
        return 0 if not lv["problems"] else 2
    if a.command == "store-check":
        r = series.store_check(Path(a.paths[0]))
        if a.out:
            Path(a.out).write_text(json.dumps(r, indent=2) + "\n")
        _print(r)
        return 0 if r["pass"] else 2
    if a.command == "plan":
        _print(series.write_plan(Path(a.load_map), a.fingerprint_hash, Path(a.capacity_report), Path(a.store_check)))
        return 0
    if a.command == "freeze":
        _print(write_freeze())
        return 0
    if a.command == "register":
        _print(write_registration(a.url, a.registered_at))
        return 0
    if a.command == "verify":
        r = check()
        _print(r)
        return 0 if r["ok"] else 2
    if a.command == "run":
        return series.run_series(a)
    if a.command == "score":
        _print(series.score_series(Path(a.evidence)))
        return 0
    if a.command == "analyze":
        from .analysis import run_all
        _print(run_all(Path(a.evidence), Path(a.out) if a.out else None))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
