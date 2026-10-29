"""Descriptive (post hoc) diagnostics of series R, after the registered analysis.

Not registered and not a test: the registered predictions and results are unchanged. Inputs are
only published, hash-verified output sets (the series-R set: results_R.json, pairs_R.csv; optionally
the cited v3.0.1 set: e3_headroom.csv) and the registered schedule.csv.

1. Execution structure: the order in which the attempts were run (the attempt ledger order in
   results_R.json) against the registered seeded schedule (slots 1-16 of one block), replacements
   and the slot positions of each level.
2. Agreement of the rule H < 1 by run and by level (R-T1 counts per run and per level).
3. Measurement resolution near H = 1: the offset of a record is interpolated between two 1 Hz size
   samples of the active file, which differ by about one second of writing, so the offset is known to
   within about r * s bytes (s = 1 s) and H = ((M - o) + kM) / W to within r * s / W = s / age. Each
   pair's band [H - s/age, H + s/age] is reported, and pairs whose band contains 1 are listed. The
   band covers the sampling resolution of the offset only; W uses the mean rate over the window and
   the record time comes from the harness journal, and their errors are not part of the band.
4. Serial dependence: for audit F1 and Tetragon F2, the lag-1 autocorrelation in execution order of
   the offset fraction o / M and of the residual (observed loss - formula probability), with a seeded
   permutation p-value (run order permuted), the Spearman correlation of offset with run position,
   and the within-run correlation of the audit F1 and Tetragon F2 offsets.
5. Dependence bounds: for each level and record type, the probability of observing no loss in its
   runs if each record were lost independently with its formula probability (observed ages and
   rates), next to the Frechet bounds for that probability under any dependence between runs,
   [max(0, sum q_i - (n - 1)), min q_i], q_i = 1 - p_i.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

from .design import LEVELS
from .sensitivity import KEY_PAIRS, MIB, _rows, _verify_set, poisson_binomial_cdf

SAMPLE_S = 1.0          # growth monitor interval (sealed/growth_monitor_1hz.csv)
N_PERM = 20_000
SEED = 20261029


def _schedule(path: Path) -> list[dict]:
    from ..cli import _read_schedule          # the frozen reader (skips the "# master_seed=" line)
    return _read_schedule(Path(path))


def execution_order(results: dict, schedule: list[dict]) -> dict:
    runs = results["runs"]
    sched = sorted(schedule, key=lambda r: int(r["slot"]))
    first = [r for r in runs if not r.get("replaces_run_id")]
    seq_exec = [r["load"] for r in first]
    seq_sched = [r["load"] for r in sched]
    blocks = sorted({r["block"] for r in sched})
    return {"attempts": len(runs), "replacements": [r["run_id"] for r in runs if r.get("replaces_run_id")],
            "dispositions": results.get("dispositions"), "blocks_in_schedule": blocks,
            "schedule_levels_by_slot": seq_sched, "executed_levels_in_order": seq_exec,
            "executed_order_matches_schedule": seq_exec == seq_sched,
            "slots_by_level": {lvl: [i + 1 for i, v in enumerate(seq_sched) if v == lvl] for lvl in LEVELS},
            "run_ids_in_order": [r["run_id"] for r in runs]}


def agreement(rows: list[dict], order: list[str]) -> dict:
    meas = [r for r in rows if r["H"] is not None]
    by_run = []
    for rid in order:
        rs = [r for r in meas if r["run_id"] == rid]
        if not rs:
            continue
        ok = sum((r["H"] < 1) == r["observed_lost"] for r in rs)
        by_run.append({"run_id": rid, "position": order.index(rid) + 1, "level": rs[0]["level"], "pairs": len(rs),
                       "correct": ok, "lost": sum(r["observed_lost"] for r in rs),
                       "H_min": min(r["H"] for r in rs), "H_max": max(r["H"] for r in rs)})
    by_level = {}
    for lvl in LEVELS:
        rs = [r for r in meas if r["level"] == lvl]
        if not rs:
            continue
        runs = [x for x in by_run if x["level"] == lvl]
        by_level[lvl] = {"runs": len(runs), "runs_all_correct": sum(x["correct"] == x["pairs"] for x in runs),
                         "pairs": len(rs), "correct": sum((r["H"] < 1) == r["observed_lost"] for r in rs),
                         "lost": sum(r["observed_lost"] for r in rs),
                         "H_range_by_pair": {f"{f}-{s}": [min(r["H"] for r in rs if r["fact"] == f and r["source"] == s),
                                                          max(r["H"] for r in rs if r["fact"] == f and r["source"] == s)]
                                             for f, s in sorted({(r["fact"], r["source"]) for r in rs})}}
    return {"pairs": len(meas), "correct": sum((r["H"] < 1) == r["observed_lost"] for r in meas),
            "runs": len(by_run), "runs_all_correct": sum(x["correct"] == x["pairs"] for x in by_run),
            "by_run": by_run, "by_level": by_level}


def resolution(rows: list[dict], s: float = SAMPLE_S) -> dict:
    meas = [r for r in rows if r["H"] is not None and r["age_s"]]
    items = []
    for r in meas:
        half = s / r["age_s"]       # (r * s) / (r * age)
        items.append({"run_id": r["run_id"], "level": r.get("level"), "fact": r["fact"], "source": r["source"],
                      "H": r["H"], "band": [r["H"] - half, r["H"] + half], "half_width": half,
                      "observed_lost": r["observed_lost"], "classified_correctly": (r["H"] < 1) == r["observed_lost"],
                      "band_contains_1": r["H"] - half <= 1 <= r["H"] + half})
    near = [i for i in items if i["band_contains_1"]]
    resolved = [i for i in items if not i["band_contains_1"]]
    return {"sample_interval_s": s, "pairs": len(items),
            "half_width_range": [min(i["half_width"] for i in items), max(i["half_width"] for i in items)] if items else None,
            "pairs_band_contains_1": len(near), "near_threshold": near,
            "resolved_pairs": len(resolved), "resolved_correct": sum(i["classified_correctly"] for i in resolved),
            "min_abs_H_minus_1": min((abs(i["H"] - 1) for i in items), default=None)}


def _lag1(x: np.ndarray) -> float:
    x = np.asarray(x, float) - np.mean(x)
    den = float(np.sum(x * x))
    return float(np.sum(x[1:] * x[:-1]) / den) if den > 0 else float("nan")


def _perm_p(x: np.ndarray, rng: np.random.Generator, n: int = N_PERM) -> float:
    obs = _lag1(x)
    if not math.isfinite(obs):
        return float("nan")
    hits = sum(abs(_lag1(rng.permutation(x))) >= abs(obs) - 1e-12 for _ in range(n))
    return (1 + hits) / (1 + n)


def serial(rows: list[dict], order: list[str], m: float, seed: int = SEED) -> dict:
    rng = np.random.Generator(np.random.PCG64(seed))
    out = {}
    pos = {rid: i for i, rid in enumerate(order)}
    off = {}
    for fact, src in KEY_PAIRS:
        rs = sorted([r for r in rows if r["fact"] == fact and r["source"] == src and r["offset_bytes"] is not None],
                    key=lambda r: pos[r["run_id"]])
        o = np.array([r["offset_bytes"] / m for r in rs])
        res = np.array([float(r["observed_lost"]) - r["p_loss_formula"] for r in rs])
        off[f"{fact}-{src}"] = {r["run_id"]: r["offset_bytes"] / m for r in rs}
        sp = spearmanr(np.arange(len(o)), o)
        out[f"{fact}-{src}"] = {"n": len(rs), "offset_fraction_in_order": [round(v, 4) for v in o],
                                "lag1_offset": _lag1(o), "lag1_offset_perm_p": _perm_p(o, rng),
                                "lag1_residual": _lag1(res), "lag1_residual_perm_p": _perm_p(res, rng),
                                "spearman_offset_vs_position": float(sp.statistic), "spearman_p": float(sp.pvalue)}
    a, t = off.get("F1-audit", {}), off.get("F2-tetragon", {})
    common = [rid for rid in order if rid in a and rid in t]
    if len(common) >= 3:
        sp = spearmanr([a[r] for r in common], [t[r] for r in common])
        out["within_run_audit_F1_vs_tetragon_F2"] = {
            "n": len(common), "pearson": float(np.corrcoef([a[r] for r in common], [t[r] for r in common])[0, 1]),
            "spearman": float(sp.statistic), "spearman_p": float(sp.pvalue)}
    out["n_permutations"], out["seed"] = N_PERM, seed
    return out


def dependence_bounds(rows: list[dict]) -> list[dict]:
    out = []
    for lvl in LEVELS:
        for fact, src in KEY_PAIRS:
            rs = [r for r in rows if r["level"] == lvl and r["fact"] == fact and r["source"] == src]
            if not rs:
                continue
            ps = [r["p_loss_formula"] for r in rs]
            q = [1 - p for p in ps]
            lost = sum(r["observed_lost"] for r in rs)
            item = {"level": lvl, "fact": fact, "source": src, "n": len(rs), "lost": lost,
                    "P_le_observed_independent": poisson_binomial_cdf(ps, lost)}
            if lost == 0:
                item["P_none_lost_frechet"] = [max(0.0, sum(q) - (len(q) - 1)), min(q)]
            out.append(item)
    return out


def table_tex(agr: dict) -> str:
    lines = [r"\begin{tabularx}{\textwidth}{lCCCCl}", r"\toprule",
             r"\textbf{Level} & \textbf{Runs, all pairs correct} & \textbf{Pairs correct} & \textbf{Pairs lost} & "
             r"\textbf{$H$ range, audit F1} & \textbf{$H$ range, Tetragon F2} \\", r"\midrule"]
    for lvl, d in agr["by_level"].items():
        a = d["H_range_by_pair"].get("F1-audit")
        t = d["H_range_by_pair"].get("F2-tetragon")
        fmt = lambda v: f"{v[0]:.3f}--{v[1]:.3f}" if v else "--"     # noqa: E731
        lines.append(f"{lvl} & {d['runs_all_correct']}/{d['runs']} & {d['correct']}/{d['pairs']} & "
                     f"{d['lost']}/{d['pairs']} & {fmt(a)} & {fmt(t)} \\\\")
    lines += [r"\midrule", f"All & {agr['runs_all_correct']}/{agr['runs']} & {agr['correct']}/{agr['pairs']} & "
              f"{sum(d['lost'] for d in agr['by_level'].values())}/{agr['pairs']} & & \\\\", r"\bottomrule", r"\end{tabularx}"]
    return "\n".join(lines) + "\n"


def run(robustness_set: Path, plan_path: Path, schedule_path: Path, out: Path, cited_set: Path | None = None) -> dict:
    robustness_set, out = Path(robustness_set), Path(out)
    problems = _verify_set(robustness_set) + (_verify_set(Path(cited_set)) if cited_set else [])
    if problems:
        raise RuntimeError(f"input output sets fail verification: {problems[:5]}")
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} exists (written once)")
    results = json.loads((robustness_set / "results_R.json").read_text())
    rows = _rows(robustness_set / "pairs_R.csv")
    plan = json.loads(Path(plan_path).read_text())
    m = plan["M_mib"] * MIB
    ex = execution_order(results, _schedule(Path(schedule_path)))
    order = ex["run_ids_in_order"]
    agr = agreement(rows, order)
    res = {"kind": "descriptive post hoc diagnostics (not registered; registered predictions and results unchanged)",
           "inputs": {"robustness_set": str(robustness_set), "robustness_SHA256SUMS":
                      hashlib.sha256((robustness_set / "SHA256SUMS").read_bytes()).hexdigest(),
                      "plan_sha256": hashlib.sha256(Path(plan_path).read_bytes()).hexdigest(),
                      "schedule_sha256": hashlib.sha256(Path(schedule_path).read_bytes()).hexdigest()},
           "execution": ex, "agreement": agr, "resolution_R": resolution(rows),
           "serial": serial(rows, order, m), "dependence_bounds": dependence_bounds(rows)}
    if cited_set:
        cited_set = Path(cited_set)
        v = [r for r in _rows(cited_set / "e3_headroom.csv")]
        res["inputs"]["cited_set"] = str(cited_set)
        res["inputs"]["cited_SHA256SUMS"] = hashlib.sha256((cited_set / "SHA256SUMS").read_bytes()).hexdigest()
        res["resolution_v3_0_1"] = resolution(v)
    files = {"diagnostics_R.json": json.dumps(res, indent=2) + "\n", "tables/latex/TS7_agreement.tex": table_tex(agr)}
    b = io.StringIO()
    w = csv.DictWriter(b, fieldnames=list(agr["by_run"][0].keys()), lineterminator="\n")
    w.writeheader()
    w.writerows(agr["by_run"])
    files["agreement_by_run_R.csv"] = b.getvalue()
    out.mkdir(parents=True, exist_ok=True)
    sums = []
    for rel, text in sorted(files.items()):
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "x") as fh:
            fh.write(text)
        sums.append(f"{hashlib.sha256(text.encode()).hexdigest()}  {rel}\n")
    (out / "SHA256SUMS").write_text("".join(sums))
    summary = {"out": str(out), "execution": {k: ex[k] for k in ("attempts", "replacements", "blocks_in_schedule",
                                                                 "executed_order_matches_schedule", "slots_by_level")},
               "agreement": {k: agr[k] for k in ("pairs", "correct", "runs", "runs_all_correct")} |
               {"by_level": {l: {k: d[k] for k in ("runs_all_correct", "runs", "correct", "pairs", "lost")}
                             for l, d in agr["by_level"].items()}},
               "resolution_R": {k: res["resolution_R"][k] for k in ("half_width_range", "pairs_band_contains_1",
                                                                    "resolved_pairs", "resolved_correct", "min_abs_H_minus_1")}
               | {"near_threshold": [{k: i[k] for k in ("run_id", "level", "fact", "source", "H", "band", "observed_lost")}
                                     for i in res["resolution_R"]["near_threshold"]]},
               "serial": {k: ({kk: vv for kk, vv in v.items() if kk != "offset_fraction_in_order"} if isinstance(v, dict) else v)
                          for k, v in res["serial"].items()},
               "dependence_bounds": res["dependence_bounds"]}
    if cited_set:
        summary["resolution_v3_0_1"] = {k: res["resolution_v3_0_1"][k] for k in ("half_width_range", "pairs",
                                        "pairs_band_contains_1", "resolved_pairs", "resolved_correct", "min_abs_H_minus_1")} \
            | {"near_threshold": [{k: i[k] for k in ("run_id", "fact", "source", "H", "band", "observed_lost")}
                                  for i in res["resolution_v3_0_1"]["near_threshold"]]}
    return summary
