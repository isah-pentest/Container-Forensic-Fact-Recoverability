"""Confirmatory-cohort analyses from the locked scores (Blueprint Sections 11, 12.1-12.3).

primary():      the frozen `frx analyze` command itself, compared with the locked primary.json;
run_table():    per-run fact/source states, union, Y, C (M1-M4) from the locked scores;
secondary():    M7, M8, E2 (M9, M10, McNemar), M11-M13, C_i contrast (Section 12.2);
validity():     M14, M15, M18, M19, M21, M23, M24 (Sections 10, 11, 17.1 T3/T6);
sensitivity():  S1-S9 (Section 12.3), each with its rule and what it excluded.
"""
from __future__ import annotations

import csv
import io
import json
import statistics
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

from ..metrics import (CELLS, PRIMARY_FACTS, block_contrasts, block_matrix, cell_proportions, f2_paired_table,
                       incremental_contribution, source_rates)
from ..reconstruct.score import order_check
from ..stats import block_t_ci, clopper_pearson, mcnemar_exact, mover_wilson, primary_analysis, wilson
from .inputs import Evidence, journal, read_jsonl

FACTS = ("F1", "F2", "F3")
SOURCES = ("audit", "tetragon")


def _ratio(x: int, n: int) -> dict:
    return {"x": x, "n": n, "p": (x / n) if n else None, "wilson95": wilson(x, n) if n else None,
            "text": f"{x}/{n}"}


def _quart(v: list[float]) -> dict:
    if not v:
        return {"n": 0}
    q = np.percentile(v, [25, 50, 75])
    return {"n": len(v), "min": min(v), "q1": float(q[0]), "median": float(q[1]), "q3": float(q[2]), "max": max(v)}


# ---------------------------------------------------------------------- primary (frozen command)
def primary(ev: Evidence, draws: int = 100_000) -> dict:
    from ..cli import main as frozen_cli
    seed = int(ev.params["design"]["master_seed"])
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "primary.json"
        with redirect_stdout(io.StringIO()):
            frozen_cli(["analyze", str(ev.scores_csv), "--seed", str(seed), "--draws", str(draws), "--out", str(out)])
        text = out.read_text()
    res = json.loads(text)
    locked = ev.root / "analysis_outputs" / "primary.json"
    res["_reproduction"] = {"command": f"frx analyze scores.csv --seed {seed} --draws {draws}",
                            "matches_locked_primary_json": locked.exists() and json.loads(locked.read_text()) == json.loads(text)}
    return res


# ---------------------------------------------------------------------- per-run table
def run_table(ev: Evidence, cohort=None) -> list[dict]:
    coh = cohort or ev.confirmatory
    rows = []
    for r in coh.scored():
        prim = coh.scores(r["run_id"])["primary"]
        tgt = next(i for i in prim["identities"] if i["role"] == "target")
        st = json.loads((Path(r["package"]) / "run_status.json").read_text())
        cf = (r.get("primary_failure_code") or "").startswith("CF-")
        row = {"run_id": r["run_id"], "block": r["block"], "slot": r["slot"], "cell": r["cell"], "arm": r["arm"],
               "load": r["load"], "collector_failure": cf, "delay_observed_s": st.get("delay_observed_s"),
               "secondary_codes": st.get("secondary_failure_codes") or [],
               "Y": 0 if cf else int(prim.get("Y") or 0), "C": 0.0 if cf else float(prim.get("C") or 0.0),
               "F5": tgt["facts"]["F5"]["state"], "F6": tgt["facts"]["F6"]["union"]}
        for f in ("F1", "F2", "F3", "F4"):
            row[f"{f}_union"] = tgt["facts"][f]["union"]
            for s in SOURCES:
                row[f"{f}_{s}"] = tgt["facts"][f]["states"][s]
        rows.append(row)
    return rows


# ---------------------------------------------------------------------- secondary (Section 12.2)
def secondary(ev: Evidence, rows: list[dict]) -> dict:
    by = {c: [r for r in rows if r["cell"] == c] for c in CELLS}
    out: dict = {"M7_per_fact_recovery": {}, "M8_source_subcontract_completeness": {}}
    for c, rs in by.items():
        out["M7_per_fact_recovery"][c] = {f: _ratio(sum(r[f"{f}_union"] == "CORRECT" and not r["collector_failure"]
                                                        for r in rs), len(rs)) for f in FACTS}
        ca = [sum(r[f"{f}_audit"] == "CORRECT" for f in FACTS) / 3 for r in rs]
        ct = [float(r["F2_tetragon"] == "CORRECT") for r in rs]
        out["M8_source_subcontract_completeness"][c] = {
            "audit_mean_over_F1_F3": statistics.fmean(ca) if ca else None,
            "tetragon_mean_over_F2": statistics.fmean(ct) if ct else None, "n": len(rs),
            "note": "not compared across sources as if the sets were equal (Section 11.3, M8)"}
    pairs_all = [(r["F2_audit"], r["F2_tetragon"]) for r in rows]
    e2 = {"pooled": f2_paired_table(pairs_all)}
    e2["pooled"]["mcnemar_exact_p"] = mcnemar_exact(e2["pooled"]["n10"], e2["pooled"]["n01"])
    e2["per_cell"] = {c: f2_paired_table([(r["F2_audit"], r["F2_tetragon"]) for r in rs]) for c, rs in by.items()}
    inc = incremental_contribution(pairs_all)
    e2["M10_incremental"] = {k: _ratio(*v) for k, v in inc.items()}
    out["E2_F2_sources"] = e2
    out["M11_conflict_rate"] = _ratio(sum(r["F2_union"] == "CONFLICT" for r in rows), len(rows))
    for s in SOURCES:
        states = [r[f"{f}_{s}"] for r in rows for f in FACTS]
        sr = source_rates(states)
        out.setdefault("M12_false_assertion_rate", {})[s] = _ratio(*sr["FALSE"])
        out.setdefault("M13_abstention_rate", {})[s] = _ratio(*sr["ABSTAIN"])
    # C_i: same contrast as Delta_A on fact completeness, block t interval (supplement)
    cmat = _block_matrix_of(rows, "C")
    if cmat is not None:
        d = block_contrasts(cmat)["A"]
        est, lo, hi = block_t_ci(d)
        out["C_i_delta_A"] = {"estimate": est, "ci_t": (lo, hi), "B": int(cmat.shape[0])}
    return out


def _block_matrix_of(rows: list[dict], key: str):
    by_block: dict = {}
    for r in rows:
        by_block.setdefault(r["block"], {})[r["cell"]] = float(r[key])
    full = [[b[c] for c in CELLS] for _, b in sorted(by_block.items()) if all(c in b for c in CELLS)]
    return np.array(full) if full else None


# ---------------------------------------------------------------------- validity and accounting
def validity(ev: Evidence, rows: list[dict]) -> dict:
    coh = ev.confirmatory
    out: dict = {}
    fa, n_checks = 0, 0
    for r in coh.runs:
        if r["disposition"] != "SCORED":
            continue
        prim = coh.scores(r["run_id"])["primary"]
        for i in prim["identities"]:
            if i["role"] == "decoy" or r["is_control"]:
                n_checks += 1
                fa += int(bool(i.get("false_attribution")))
    out["M14_false_attribution"] = {"x": fa, "n": n_checks, "clopper_pearson95": clopper_pearson(fa, n_checks),
                                    "blinded_control_checks": {"checked": sum(c["checked"] for c in ev.control_checks),
                                                               "false_attributions": [c["run_id"] for c in ev.control_checks
                                                                                      if c["false_attribution"]]}}
    out["M15_delay_observed_s"] = {c: _quart([r["delay_observed_s"] for r in rows if r["cell"] == c
                                              and r["delay_observed_s"] is not None]) for c in ("LD", "MD")}
    out["M15_delay_noncompliant"] = sum(1 for r in rows if "DV-DELAY" in r["secondary_codes"])
    # M23 run accounting by cell and block
    acc: dict = {}
    for r in coh.runs:
        for key in (("cell", r["cell"]), ("block", r["block"])):
            a = acc.setdefault(key[0], {}).setdefault(key[1], {"attempts": 0, "SCORED": 0, "PRE_ACTION_ABORT": 0,
                                                                  "UNSCORABLE": 0, "collector_failure": 0,
                                                                  "replacements": 0})
            a["attempts"] += 1
            a[r["disposition"]] = a.get(r["disposition"], 0) + 1
            a["collector_failure"] += int((r.get("primary_failure_code") or "").startswith("CF-"))
            a["replacements"] += int(bool(r.get("replaces_run_id")))
    out["M23_run_accounting"] = acc
    out["M21_drop_events"] = sum(1 for r in rows if "DV-DROP" in r["secondary_codes"])
    # M19 measured write rate (manipulation window) and M18 follower lag, M24 collector overhead
    rates: dict = {}
    lag: dict = {}
    over: dict = {}
    for r in coh.runs:
        if r["disposition"] != "SCORED":
            continue
        pkg = Path(r["package"])
        lc = json.loads((pkg / "load" / "load_check.json").read_text())
        for s, v in (lc.get("measured_bytes_per_s") or {}).items():
            rates.setdefault(r["load"], {}).setdefault(s, []).append(v)
        lf = pkg / "acquisition" / "follower_lag_1hz.csv"
        if lf.exists():
            vals = [float(x["lag_bytes"]) for x in csv.DictReader(open(lf)) if x["lag_bytes"] not in ("", None)]
            if vals:
                lag.setdefault(r["cell"], []).append({"mean": statistics.fmean(vals), "max": max(vals)})
        rf = pkg / "system" / "resources_1hz.csv"
        if rf.exists():
            rr = list(csv.DictReader(open(rf)))
            if len(rr) > 1:
                over.setdefault(r["cell"], []).append(
                    {"cpu_busy_mean": statistics.fmean(float(x["cpu_busy_pct"]) for x in rr[1:]),
                     "self_rss_kb_max": max(int(x["self_rss_kb"]) for x in rr),
                     "tetragon_rss_kb_max": max(int(x.get("rss_kb_tetragon") or 0) for x in rr)})
    out["M19_write_rate_bytes_per_s"] = {lvl: {s: _quart(v) for s, v in d.items()} for lvl, d in rates.items()}
    out["M18_follower_lag_bytes"] = {c: {"runs": len(v), "mean_of_means": statistics.fmean(x["mean"] for x in v),
                                         "max": max(x["max"] for x in v)} for c, v in lag.items()}
    out["M24_collector_overhead"] = {c: {"runs": len(v), "cpu_busy_mean": statistics.fmean(x["cpu_busy_mean"] for x in v),
                                         "orchestrator_rss_kb_max": max(x["self_rss_kb_max"] for x in v),
                                         "tetragon_rss_kb_max": max(x["tetragon_rss_kb_max"] for x in v)}
                                     for c, v in over.items()}
    return out


# ---------------------------------------------------------------------- sensitivity (Section 12.3)
def _analyse(rows: list[dict], seed: int, draws: int) -> dict:
    y, complete, incomplete = block_matrix(rows)
    res = {"complete_blocks": len(complete), "excluded_runs": None}
    if y.shape[0] >= 2:
        res["H1"] = primary_analysis(y, "A", n_draws=draws, seed=seed)
    props = cell_proportions(rows)
    xs = [props[c][0] for c in CELLS]
    ns = [props[c][1] for c in CELLS]
    res["mover_wilson_all_runs"] = mover_wilson(xs, ns, (-0.5, 0.5, -0.5, 0.5)) if all(ns) else None
    return res


def sensitivity(ev: Evidence, rows: list[dict], draws: int = 100_000) -> dict:
    seed = int(ev.params["design"]["master_seed"])
    base = [{"run_id": r["run_id"], "block": r["block"], "cell": r["cell"], "Y": r["Y"]} for r in rows]

    def subset(name, rule, keep):
        kept = [b for b, r in zip(base, rows) if keep(r)]
        res = _analyse(kept, seed, draws)
        res["rule"] = rule
        res["excluded_runs"] = [r["run_id"] for r in rows if not keep(r)]
        return res

    out = {
        "S1": subset("S1", "exclude collector-failure runs", lambda r: not r["collector_failure"]),
        "S2": subset("S2", "exclude DV-DELAY runs", lambda r: "DV-DELAY" not in r["secondary_codes"]),
        "S4": subset("S4", "exclude DV-DROP runs", lambda r: "DV-DROP" not in r["secondary_codes"]),
    }
    d_rows = [r for r in rows if r["arm"] == "D" and r["delay_observed_s"] is not None]
    out["S2"]["Y_vs_delay_D_runs"] = {"n": len(d_rows),
                                      "delay_range_s": [min((r["delay_observed_s"] for r in d_rows), default=None),
                                                        max((r["delay_observed_s"] for r in d_rows), default=None)],
                                      "spearman": _spearman([r["delay_observed_s"] for r in d_rows], [r["Y"] for r in d_rows])}
    y, complete, _ = block_matrix(base)
    order = [int(b.split("-")[-1]) for b in complete]
    out["S3"] = {"rule": "d^A_b against block order (session drift)",
                 "d_A_by_block": dict(zip(complete, block_contrasts(y)["A"].tolist())),
                 "spearman": _spearman(order, block_contrasts(y)["A"].tolist())}
    fa_blocks = {c["block"] for c in ev.control_checks if c["false_attribution"]}
    out["S5"] = subset("S5", "exclude blocks with any NC or decoy false attribution", lambda r: r["block"] not in fa_blocks)
    out["S6"] = _f5_tolerance(ev, rows)
    out["S7"] = subset("S7", "exclude runs with container-ID fallback attribution",
                       lambda r: not _fallback_used(ev, r["run_id"]))
    for src in SOURCES:
        alt = []
        for b, r in zip(base, rows):
            e2 = int(r[f"F2_{src}"] == "CORRECT")
            yv = 0 if r["collector_failure"] else int(r["F1_union"] == "CORRECT" and e2 and r["F3_union"] == "CORRECT")
            alt.append({**b, "Y": yv})
        res = _analyse(alt, seed, draws)
        res["rule"] = f"Y with F2 from {src} only (union over F1, F3 unchanged)"
        res["cells"] = {c: f"{sum(a['Y'] for a in alt if a['cell'] == c)}/{sum(1 for a in alt if a['cell'] == c)}"
                        for c in CELLS}
        out.setdefault("S8", {})[src] = res
    props = cell_proportions(base)
    out["S9"] = {"rule": "include incomplete blocks via MOVER-Wilson on cell proportions",
                 "incomplete_blocks": block_matrix(base)[2],
                 "mover_wilson": mover_wilson([props[c][0] for c in CELLS], [props[c][1] for c in CELLS],
                                              (-0.5, 0.5, -0.5, 0.5))}
    return out


def _spearman(x, y) -> dict:
    if len(x) < 3 or len(set(x)) < 2 or len(set(y)) < 2:
        return {"rho": None, "p": None, "note": "undefined (constant input or n < 3)"}
    r = spearmanr(x, y)
    return {"rho": float(r.statistic), "p": float(r.pvalue)}


def _fallback_used(ev: Evidence, run_id: str) -> bool:
    p = ev.root / "scoring" / run_id / "transforms" / "attribution_flags.json"
    return p.exists() and json.loads(p.read_text()).get("container_id_fallback_uses", 0) > 0


def _f5_tolerance(ev: Evidence, rows: list[dict]) -> dict:
    tol = float(ev.contract["facts"]["F5"]["tolerance_s"])
    out = {"rule": "F5 order state at 0.5x, 1x, 2x the G2 tolerance", "tolerance_s": tol, "by_multiplier": {}}
    for m in (0.5, 1.0, 2.0):
        counts: dict = {}
        for r in rows:
            tgt = next(i for i in ev.confirmatory.answers(r["run_id"])["identities"] if i["role"] == "target")
            s = order_check(tgt["facts"], tol * m)["state"]
            counts[s] = counts.get(s, 0) + 1
        out["by_multiplier"][str(m)] = counts
    return out
