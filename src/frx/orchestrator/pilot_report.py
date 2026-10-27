"""G4 pilot evaluation (Blueprint v3.0, Sections 10.4, 12.4, 14.3-14.4, 18).

Reads the pilot progress log, closed packages and their scoring trees; writes nothing into them.
Every proportion is printed with its numerator and denominator.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

POSITIVE = ("LC", "LD", "MC", "MD")


def _load(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


def _size(pkg: Path) -> int:
    return sum(f.stat().st_size for f in pkg.rglob("*") if f.is_file())


def _r(n, d):
    return {"n": n, "of": d, "text": f"{n}/{d}"}


def report(progress: Path, runs_root: Path, scoring_root: Path, n_blocks_planned: int) -> dict:
    prog = [json.loads(l) for l in open(progress)]
    runs = [p for p in prog if p.get("event") == "RUN"]
    closed = {p["block"]: p for p in prog if p.get("event") == "BLOCK_CLOSED"}
    opened = {p["block"]: p for p in prog if p.get("event") == "BLOCK_OPEN"}
    incomplete = [p["block"] for p in prog if p.get("event") == "BLOCK_INCOMPLETE"]
    out: dict = {"blocks_planned": n_blocks_planned, "blocks_closed": len(closed), "blocks_incomplete": incomplete}

    # run accounting (M23) and ceilings (Section 10.4)
    acc: dict = {}
    per_block_cell: dict = {}
    for r in runs:
        a = acc.setdefault(r["cell"], {"attempts": 0, "SCORED": 0, "PRE_ACTION_ABORT": 0, "UNSCORABLE": 0,
                                       "collector_failure": 0, "replacements": 0})
        a["attempts"] += 1
        a[r["disposition"]] = a.get(r["disposition"], 0) + 1
        if (r.get("primary_failure_code") or "").startswith("CF-"):
            a["collector_failure"] += 1
        if r.get("replaces_run_id"):
            a["replacements"] += 1
        if r["disposition"] in ("PRE_ACTION_ABORT", "UNSCORABLE"):
            k = (r["block"], r["cell"])
            per_block_cell[k] = per_block_cell.get(k, 0) + 1
    out["run_accounting"] = acc
    ceil_cell = {c: _r(a["PRE_ACTION_ABORT"] + a["UNSCORABLE"], a["attempts"]) for c, a in acc.items()}
    out["abort_unscorable_by_cell"] = ceil_cell
    out["ceiling_respected"] = (all(v["n"] <= 0.15 * v["of"] for v in ceil_cell.values())
                                and all(v <= 2 for v in per_block_cell.values()))
    cf = {arm: sum(1 for r in runs if r["arm"] == arm and (r.get("primary_failure_code") or "").startswith("CF-"))
          for arm in ("C", "D")}
    out["collector_failures_by_arm"] = cf
    out["collector_failure_imbalance_ok"] = abs(cf["C"] - cf["D"]) <= 3

    # gates (Section 15.4)
    out["package_gates"] = _r(sum(1 for r in runs if r["gate_ok"]), len(runs))
    out["block_replicas"] = _r(sum(1 for b in closed.values() if b.get("replica_ok")), len(closed))

    # scores: false attribution (NC + decoys) and cell estimates
    fa_runs, checks = [], 0
    cells: dict = {c: {"Y1": 0, "n": 0} for c in POSITIVE}
    facts: dict = {}
    for r in runs:
        if r["disposition"] != "SCORED":
            continue
        sc = _load(scoring_root / r["run_id"] / "assessment" / "scores.json")
        if sc is None:
            out.setdefault("unscored_packages", []).append(r["run_id"])
            continue
        prim = sc["primary"]
        checks += sum(1 for i in prim["identities"] if i["role"] == "decoy" or r["is_control"])
        if prim.get("false_attribution"):
            fa_runs.append(r["run_id"])
        if r["is_control"]:
            continue
        cf_run = (r.get("primary_failure_code") or "").startswith("CF-")
        y = 0 if cf_run else int(prim.get("Y") or 0)          # Section 10.1: collector failure counts Y = 0
        cells[r["cell"]]["n"] += 1
        cells[r["cell"]]["Y1"] += y
        tgt = next(i for i in prim["identities"] if i["role"] == "target")
        for f in ("F1", "F2", "F3"):
            d = facts.setdefault(r["cell"], {}).setdefault(f, {"CORRECT": 0, "n": 0})
            d["n"] += 1
            d["CORRECT"] += int(tgt["facts"][f]["union"] == "CORRECT" and not cf_run)
    out["false_attribution"] = {"runs": fa_runs, "checks": checks, "pass": not fa_runs}
    out["cell_Y"] = {c: _r(v["Y1"], v["n"]) for c, v in cells.items()}
    out["cell_fact_recovery"] = {c: {f: _r(v["CORRECT"], v["n"]) for f, v in fs.items()} for c, fs in facts.items()}
    ps = [v["Y1"] / v["n"] for v in cells.values() if v["n"]]
    out["deterministic_pattern"] = bool(ps) and all(p in (0.0, 1.0) for p in ps)

    # timing, load and size estimates for G4/G5
    delays, rates, sizes, durations = [], {}, [], []
    for r in runs:
        pkg = Path(r["package"]) if r.get("package") else runs_root / r["run_id"]
        st = _load(pkg / "run_status.json") or {}
        if r["arm"] == "D" and st.get("delay_observed_s") is not None:
            delays.append(st["delay_observed_s"])
        lc = _load(pkg / "load" / "load_check.json") or {}
        if lc.get("measured_bytes_per_s"):
            rates.setdefault(r["load"], []).append(lc["measured_bytes_per_s"])
        sizes.append(_size(pkg))
    for b, c in closed.items():
        o = opened.get(b)
        if o:
            durations.append((datetime.fromisoformat(c["closed_at"]) - datetime.fromisoformat(o["at"])).total_seconds())
    out["delay_observed_s"] = {"n": len(delays), "min": min(delays, default=None), "max": max(delays, default=None),
                               "noncompliant": sum(1 for d in delays if abs(d - 60.0) > 2.0)}
    out["measured_write_rate_bytes_per_s"] = {
        lvl: {s: {"min": round(min(x[s] for x in v)), "max": round(max(x[s] for x in v))} for s in ("audit", "tetragon")}
        for lvl, v in rates.items()}
    mx = max(sizes, default=0)
    out["package_bytes"] = {"n": len(sizes), "max": mx, "mean": round(sum(sizes) / len(sizes)) if sizes else None,
                            "total": sum(sizes)}
    out["disk_free_min_bytes_rule"] = 3 * mx + 10 * 10**9          # Section 14.3: 3 x package size + 10 GB
    out["block_duration_s"] = {"n": len(durations), "min": min(durations, default=None),
                               "max": max(durations, default=None)}
    out["pass"] = bool(out["false_attribution"]["pass"] and out["ceiling_respected"]
                       and out["package_gates"]["n"] == out["package_gates"]["of"]
                       and out["block_replicas"]["n"] == out["block_replicas"]["of"] == n_blocks_planned
                       and not incomplete and not out.get("unscored_packages"))
    return out
