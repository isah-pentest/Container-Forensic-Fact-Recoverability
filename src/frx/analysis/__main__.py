"""python -m frx.analysis <command> [--evidence DIR]

  status         execution state from dispositions and blinded control checks only
  verify         re-verify the score lock, the analysis input and every package/scoring manifest
  frozen-diff    list changes since the protocol-v3.0.1-frozen tag; only the analysis layer may differ
  rescore-check  re-run the frozen reconstruction and scoring; locked answers and scores must match
  all            every analysis, table and figure -> <evidence>/analysis_outputs/<commit>/
  compare A B    byte comparison of two output sets (G7 clean-environment regeneration)
  audit-sample   seeded >= 10 % raw-to-fact audit worksheet, stratified by cell
  compare A B --ignore-commit
                 the same, accepting differences that are only each set's own analysis commit
  demo           build a synthetic evidence store (simulated cluster) in --out and run every analysis on it
  registered-freeze
                 FREEZE.json at the frozen tag vs the registered SHA-256, each protocol file and the code
                 digest, from git objects; files absent from the public history are listed as withheld
  environment    Python version, platform and installed distributions (the analysis environment record)
  rebuild-check CSV RESULTS
                 recompute the published quantities from data/run_level.csv alone and compare them
                 with results.json (no evidence store needed)
  interval-coverage CITED_SET [--out DIR] [--sims N]
                 descriptive post hoc check of the Delta_A interval: registered interval, two block-aware
                 sensitivity intervals and their Monte Carlo coverage under the blocked design
                 (registered interval and rule unchanged)
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m frx.analysis", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["status", "verify", "frozen-diff", "rescore-check", "all", "compare",
                                       "audit-sample", "rebuild-check", "demo", "registered-freeze",
                                       "environment", "interval-coverage"])
    p.add_argument("paths", nargs="*")
    p.add_argument("--evidence", default="~/frx-evidence")
    p.add_argument("--out")
    p.add_argument("--draws", type=int, default=100_000)
    p.add_argument("--limit", type=int)
    p.add_argument("--allow-dirty", action="store_true", help="tests only; real outputs need a committed tree")
    p.add_argument("--ignore-commit", action="store_true", help="compare: ignore each set's own analysis commit")
    p.add_argument("--sims", type=int, default=4000, help="interval-coverage: simulated tables per scenario")
    p.add_argument("--ref", default="protocol-v3.0.1-frozen", help="registered-freeze: the frozen tag")
    a = p.parse_args(argv)
    from . import regenerate as rg
    if a.command == "frozen-diff":
        r = rg.frozen_diff()
        _print(r)
        return 0 if r["ok"] else 2
    if a.command == "compare":
        from .report import compare
        r = compare(Path(a.paths[0]), Path(a.paths[1]), ignore_commit=a.ignore_commit)
        _print(r)
        return 0 if r["ok"] else 2
    if a.command == "registered-freeze":
        from .public import registered_freeze
        r = registered_freeze(ref=a.ref)
        _print(r)
        return 0 if r["ok"] else 2
    if a.command == "environment":
        from .public import environment
        _print(environment())
        return 0
    if a.command == "demo":
        if not a.out:
            print("demo needs --out DIR (an empty or new directory)")
            return 2
        from .public import demo
        r = demo(Path(a.out), draws=min(a.draws, 2000))
        _print(r)
        return 0 if r["rebuild_ok"] and r["verify_ok"] else 2
    if a.command == "interval-coverage":
        from ..freeze import REPO_ROOT
        from .coverage import run as coverage
        from .report import git_state
        gs = git_state()
        if gs["dirty"] and not a.allow_dirty:
            print("commit the analysis code first: outputs are tied to an analysis commit")
            return 2
        cited = Path(a.paths[0]).expanduser().resolve()
        out = Path(a.out) if a.out else cited.parent / "interval_coverage" / gs["short"]
        _print(coverage(cited, REPO_ROOT, out, n_sims=a.sims))
        return 0
    if a.command == "rebuild-check":
        from .dataset import rebuild
        r = rebuild(Path(a.paths[0]).read_text(), json.loads(Path(a.paths[1]).read_text()))
        _print({k: v for k, v in r.items() if k != "checks"} |
               {"checks": [{"quantity": c["quantity"], "ok": c["ok"]} for c in r["checks"]]})
        return 0 if r["ok"] else 2
    from .inputs import Evidence
    ev = Evidence(Path(a.evidence))
    if a.command == "status":
        _print(rg.status(ev))
        return 0
    if a.command == "verify":
        r = ev.verify()
        _print({k: v for k, v in r.items() if k != "problems"} | {"problems": r["problems"][:50]})
        return 0 if r["ok"] else 2
    if a.command == "rescore-check":
        r = rg.rescore_check(ev, a.limit)
        _print(r)
        return 0 if r["ok"] else 2
    if a.command == "all":
        from .report import run_all
        _print(run_all(ev.root, Path(a.out) if a.out else None, a.draws, a.allow_dirty))
        return 0
    if a.command == "audit-sample":
        r = rg.audit_sample(ev)
        out = Path(a.out) if a.out else ev.root / "analysis_outputs" / "raw_to_fact_audit.csv"
        if out.exists():
            print(f"refusing: {out} exists (the audit worksheet is written once)")
            return 2
        b = io.StringIO()
        keys = list(r["items"][0].keys()) if r["items"] else ["run_id"]
        w = csv.DictWriter(b, fieldnames=keys, lineterminator="\n")
        w.writeheader()
        for it in r["items"]:
            w.writerow({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in it.items()})
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(b.getvalue())
        _print({"written": str(out), "seed": r["seed"], "runs": r["runs"], "records": len(r["items"])})
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
