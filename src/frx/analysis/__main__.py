"""python -m frx.analysis <command> [--evidence DIR]

  status         execution state from dispositions and blinded control checks only
  verify         re-verify the score lock, the analysis input and every package/scoring manifest
  frozen-diff    list changes since the protocol-v3.0.1-frozen tag; only the analysis layer may differ
  rescore-check  re-run the frozen reconstruction and scoring; locked answers and scores must match
  all            every analysis, table and figure -> <evidence>/analysis_outputs/<commit>/
  compare A B    byte comparison of two output sets (G7 clean-environment regeneration)
  audit-sample   seeded >= 10 % raw-to-fact audit worksheet, stratified by cell
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
                                       "audit-sample"])
    p.add_argument("paths", nargs="*")
    p.add_argument("--evidence", default="~/frx-evidence")
    p.add_argument("--out")
    p.add_argument("--draws", type=int, default=100_000)
    p.add_argument("--limit", type=int)
    p.add_argument("--allow-dirty", action="store_true", help="tests only; real outputs need a committed tree")
    a = p.parse_args(argv)
    from . import regenerate as rg
    if a.command == "frozen-diff":
        r = rg.frozen_diff()
        _print(r)
        return 0 if r["ok"] else 2
    if a.command == "compare":
        from .report import compare
        r = compare(Path(a.paths[0]), Path(a.paths[1]))
        _print(r)
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
