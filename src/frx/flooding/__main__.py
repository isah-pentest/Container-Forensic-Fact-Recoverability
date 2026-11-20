"""python -m frx.flooding <command>

Before the freeze (qualification on the k = 1 cluster; no study outcome is produced):
  levels                           F0-F3 flood settings and model predictions (prints only)
  g1-settings                      the frozen settings G1 measures (L, M, CAL1-CAL4), per replica
  g1 --stores DIR [--evidence DIR] G1 load map on this host with the frozen settings and replicas, and verdict
  g1-evaluate LOAD_MAP_JSON        G1 verdict from a load map already written
  store-check CAPACITY_RUN_DIR     the store held exactly k = 1 backup and deleted surplus files
  plan --g1 PATH --fingerprint-hash H --capacity-report PATH --store-check PATH
                                   write protocol/flooding/plan.json and schedule.csv (once)
Freeze:
  freeze                           write protocol/flooding/FREEZE_F.json (committed tree required)
  verify                           frozen, unchanged and tagged series-F-frozen?
Execution and analysis (after the freeze commit is tagged and pushed):
  run --stores DIR --harness-kubeconfig F --replica DIR [--audit-reference F] [--evidence DIR]
  score [--evidence DIR]           blinded scoring with the frozen scorer, then the series-F score lock
  analyze [--evidence DIR]         G2, F-T1..F-T3, F-C1, F-C2, F-D1, F-D2
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _print(o) -> None:
    print(json.dumps(o, indent=2, default=str))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m frx.flooding", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["levels", "g1-settings", "g1", "g1-evaluate", "store-check", "plan",
                                       "freeze", "verify", "run", "score", "analyze"])
    p.add_argument("paths", nargs="*")
    p.add_argument("--evidence", default="~/frx-evidence-F")
    p.add_argument("--g1")
    p.add_argument("--fingerprint-hash")
    p.add_argument("--capacity-report")
    p.add_argument("--store-check")
    p.add_argument("--out")
    p.add_argument("--stores")
    p.add_argument("--measure", type=int, default=60)
    p.add_argument("--harness-kubeconfig")
    p.add_argument("--replica")
    p.add_argument("--audit-reference")
    a = p.parse_args(argv)
    from . import design, series
    from .freeze import check, write_freeze
    prm = series.frozen_params()
    if a.command == "levels":
        lv = design.levels(prm)
        _print(lv)
        return 0 if not lv["problems"] else 2
    if a.command == "g1-settings":
        _print({"replicas": prm["load"]["replicas"], "settings": series.g1_settings(prm)})
        return 0
    if a.command == "g1":
        from ..freeze import REPO_ROOT
        from ..orchestrator.qual import load_map
        P = series.paths(a.evidence)
        P["qualification"].mkdir(parents=True, exist_ok=True)
        lm_p, g1_p = P["qualification"] / "load_map_F.json", P["qualification"] / "g1_F.json"
        if lm_p.exists() or g1_p.exists():
            print(f"refusing: {lm_p} or {g1_p} exists (G1 is measured once)")
            return 2
        st = Path(a.stores).expanduser()
        settings = [{"exec_per_s": s["exec_per_s"], "api_per_s": s["api_per_s"]} for s in series.g1_settings(prm)]
        res = load_map(REPO_ROOT, {"audit": st / "audit", "tetragon": st / "tetragon"}, prm["load"]["image_digest"],
                       int(prm["load"]["replicas"]), settings, measure_s=a.measure)
        lm_p.write_text(json.dumps(res, indent=2) + "\n")
        g1 = series.g1_evaluate(lm_p, prm)
        g1_p.write_text(json.dumps(g1, indent=2) + "\n")
        _print({"written": [str(lm_p), str(g1_p)], **g1})
        return 0 if g1["pass"] else 2
    if a.command == "g1-evaluate":
        g1 = series.g1_evaluate(Path(a.paths[0]), prm)
        _print(g1)
        return 0 if g1["pass"] else 2
    if a.command == "store-check":
        from ..robustness.series import store_check
        r = store_check(Path(a.paths[0]), k=design.K_BACKUPS)
        if a.out:
            Path(a.out).write_text(json.dumps(r, indent=2) + "\n")
        _print(r)
        return 0 if r["pass"] else 2
    if a.command == "plan":
        _print(series.write_plan(Path(a.g1), a.fingerprint_hash, Path(a.capacity_report), Path(a.store_check)))
        return 0
    if a.command == "freeze":
        _print(write_freeze())
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
