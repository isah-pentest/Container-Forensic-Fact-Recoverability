"""Descriptive (post hoc) sensitivity analysis of the formula probabilities, after series R.

Not registered and not a test: it leaves the registered predictions and results unchanged and reports
next to them. Inputs are only published, hash-verified output sets: the series-R set
(pairs_R.csv) and the cited v3.0.1 set (e3_headroom.csv), plus the registered plan.json.

1. Decomposition of the predicted loss probability per level and fact-source (audit F1, Tetragon F2,
   the two pairs with registered predictions): registered value (target rate, assumed age); with the
   observed record ages at the target rate; with the observed ages and measured rates (the formula
   evaluated per record, as in R-T3). For each: expected number of losses, observed losses, and the
   probability of observing no more losses than were observed if each record were lost independently
   with its probability (Poisson-binomial).
2. Offsets: the measured offset of each record in its file at write time, as a fraction of M, with the
   offset threshold above which the record is lost, o* / M = (k + 1) - W / M, and a Kolmogorov-Smirnov
   comparison of the offsets with the uniform distribution the formula assumes (audit F1 and Tetragon
   F2 only, one record per run and store), for series R and for the v3.0.1 delayed reads.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from scipy.stats import kstest

from ..headroom import predicted_loss_probability

MIB = 1024 * 1024
KEY_PAIRS = (("F1", "audit"), ("F2", "tetragon"))


def _verify_set(d: Path) -> list[str]:
    problems = []
    for line in (Path(d) / "SHA256SUMS").read_text().splitlines():
        h, rel = line.split("  ", 1)
        p = Path(d) / rel
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != h:
            problems.append(f"{d.name}/{rel} does not match SHA256SUMS")
    return problems


def _rows(p: Path) -> list[dict]:
    rows = list(csv.DictReader(open(p)))
    for r in rows:
        for k in ("age_s", "rate_bytes_per_s", "offset_bytes", "written_bytes", "p_loss_formula"):
            r[k] = float(r[k]) if r.get(k) not in (None, "") else None
        r["H"] = float(r["H"]) if r.get("H") not in (None, "", "None") else None
        r["observed_lost"] = r["observed_lost"] == "True"
    return rows


def poisson_binomial_cdf(ps: list[float], x: int) -> float:
    """P(number of losses <= x) for independent Bernoulli(p_i)."""
    dist = [1.0]
    for p in ps:
        nxt = [0.0] * (len(dist) + 1)
        for i, v in enumerate(dist):
            nxt[i] += v * (1 - p)
            nxt[i + 1] += v * p
        dist = nxt
    return sum(dist[: x + 1])


def decomposition(rows: list[dict], plan: dict) -> list[dict]:
    k = plan["k"]
    m = plan["M_mib"] * MIB
    out = []
    for lvl, d in plan["levels"].items():
        for fact, src in KEY_PAIRS:
            rs = [r for r in rows if r["level"] == lvl and r["fact"] == fact and r["source"] == src]
            if not rs:
                continue
            reg = d["predicted"][f"{fact}-{src}"]
            target = d["target_bytes_per_s"][src]
            p_age = [predicted_loss_probability(target, r["age_s"], m, k) for r in rs]
            p_obs = [r["p_loss_formula"] for r in rs]
            lost = sum(r["observed_lost"] for r in rs)
            out.append({
                "level": lvl, "fact": fact, "source": src, "n": len(rs), "lost": lost,
                "assumed_age_s": reg["age_s"], "observed_age_s": [round(r["age_s"], 2) for r in rs],
                "target_rate": target, "measured_rate": [round(r["rate_bytes_per_s"], 1) for r in rs],
                "p_registered": reg["p_loss_formula"], "expected_registered": len(rs) * reg["p_loss_formula"],
                "P_le_observed_registered": poisson_binomial_cdf([reg["p_loss_formula"]] * len(rs), lost),
                "p_observed_age_mean": sum(p_age) / len(p_age), "expected_observed_age": sum(p_age),
                "P_le_observed_observed_age": poisson_binomial_cdf(p_age, lost),
                "p_observed_age_rate_mean": sum(p_obs) / len(p_obs), "expected_observed_age_rate": sum(p_obs),
                "P_le_observed_observed_age_rate": poisson_binomial_cdf(p_obs, lost),
                "offset_fraction": [round(r["offset_bytes"] / m, 4) for r in rs],
                "loss_threshold_fraction": [round((k + 1) - r["written_bytes"] / m, 4) for r in rs],
                "H": [round(r["H"], 4) for r in rs]})
    return out


def offsets(rows: list[dict], m: int, label: str) -> dict:
    out = {}
    for fact, src in KEY_PAIRS:
        v = [r["offset_bytes"] / m for r in rows if r["fact"] == fact and r["source"] == src
             and r["offset_bytes"] is not None]
        v = [min(max(x, 0.0), 1.0) for x in v]
        if len(v) >= 2:
            ks = kstest(v, "uniform")
            out[f"{fact}-{src}"] = {"series": label, "n": len(v), "mean": sum(v) / len(v), "min": min(v),
                                    "max": max(v), "ks_D": float(ks.statistic), "ks_p": float(ks.pvalue)}
    return out


def table_tex(dec: list[dict]) -> str:
    lines = [r"\begin{tabularx}{\textwidth}{llCCCCC}", r"\toprule",
             r"\textbf{Level} & \textbf{Record} & \textbf{Lost} & \textbf{Expected, registered} & "
             r"\textbf{Expected, observed ages} & \textbf{Expected, observed ages and rates} & "
             r"\textbf{$P(\le$ lost$)$, observed ages and rates} \\", r"\midrule"]
    for d in dec:
        rec = "F1, audit" if d["source"] == "audit" else "F2, Tetragon"
        lines.append(f"{d['level']} & {rec} & {d['lost']}/{d['n']} & {d['expected_registered']:.2f} & "
                     f"{d['expected_observed_age']:.2f} & {d['expected_observed_age_rate']:.2f} & "
                     f"{d['P_le_observed_observed_age_rate']:.3f} \\\\")
    lines += [r"\bottomrule", r"\end{tabularx}"]
    return "\n".join(lines) + "\n"


def run(robustness_set: Path, cited_set: Path, plan_path: Path, out: Path) -> dict:
    robustness_set, cited_set, out = Path(robustness_set), Path(cited_set), Path(out)
    problems = _verify_set(robustness_set) + _verify_set(cited_set)
    if problems:
        raise RuntimeError(f"input output sets fail verification: {problems[:5]}")
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} exists (written once)")
    plan = json.loads(Path(plan_path).read_text())
    m = plan["M_mib"] * MIB
    r_rows = _rows(robustness_set / "pairs_R.csv")
    v_rows = _rows(cited_set / "e3_headroom.csv")
    dec = decomposition(r_rows, plan)
    res = {"kind": "descriptive post hoc sensitivity analysis (not registered; registered predictions unchanged)",
           "inputs": {"robustness_set": str(robustness_set), "robustness_SHA256SUMS":
                      hashlib.sha256((robustness_set / "SHA256SUMS").read_bytes()).hexdigest(),
                      "cited_set": str(cited_set), "cited_SHA256SUMS":
                      hashlib.sha256((cited_set / "SHA256SUMS").read_bytes()).hexdigest(),
                      "plan_sha256": hashlib.sha256(Path(plan_path).read_bytes()).hexdigest()},
           "decomposition": dec,
           "offsets": {"series_R": offsets(r_rows, m, "R (k = 2)"),
                       "v3_0_1_confirmatory_D": offsets([r for r in v_rows if r["cohort"] == "confirmatory"], m, "v3.0.1 LD/MD"),
                       "v3_0_1_calibration": offsets([r for r in v_rows if r["cohort"] == "calibration"], m, "v3.0.1 calibration")},
           "v3_0_1_MD": {}}
    for fact, src in KEY_PAIRS:
        rs = [r for r in v_rows if r["cohort"] == "confirmatory" and r["level"] == "M" and r["fact"] == fact
              and r["source"] == src]
        if rs:
            ps = [r["p_loss_formula"] for r in rs]
            lost = sum(r["observed_lost"] for r in rs)
            res["v3_0_1_MD"][f"{fact}-{src}"] = {"n": len(rs), "lost": lost, "expected_operating_point": 0.5 * len(rs),
                                                 "expected_observed_age_rate": sum(ps),
                                                 "P_le_observed_observed_age_rate": poisson_binomial_cdf(ps, lost),
                                                 "age_s_min": min(r["age_s"] for r in rs), "age_s_max": max(r["age_s"] for r in rs)}
    files = {"sensitivity_R.json": json.dumps(res, indent=2) + "\n", "tables/latex/TS5_sensitivity.tex": table_tex(dec)}
    b = io.StringIO()
    w = csv.DictWriter(b, fieldnames=list(dec[0].keys()), lineterminator="\n")
    w.writeheader()
    for d in dec:
        w.writerow({k: json.dumps(v) if isinstance(v, list) else v for k, v in d.items()})
    files["decomposition_R.csv"] = b.getvalue()
    out.mkdir(parents=True, exist_ok=True)
    sums = []
    for rel, text in sorted(files.items()):
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "x") as fh:
            fh.write(text)
        sums.append(f"{hashlib.sha256(text.encode()).hexdigest()}  {rel}\n")
    (out / "SHA256SUMS").write_text("".join(sums))
    return {"out": str(out), "decomposition": [{k: d[k] for k in ("level", "fact", "source", "lost", "n", "expected_registered",
                                                                    "expected_observed_age", "expected_observed_age_rate",
                                                                    "P_le_observed_observed_age_rate")} for d in dec],
            "offsets": res["offsets"], "v3_0_1_MD": res["v3_0_1_MD"]}
