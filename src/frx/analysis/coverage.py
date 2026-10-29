"""Descriptive (post hoc) check of the confirmatory Delta_A interval under the blocked design.

Not registered and not a test: the registered interval rule (block t, or MOVER-Wilson when the
structure rule triggers; Section 12.1) and the reported interval are unchanged. This module

1. recomputes, from the published run-level dataset of the cited output set (hash-verified), the
   registered interval and two block-aware sensitivity intervals for Delta_A:
   - MOVER-Wilson with the between-cell correlations estimated across blocks (the correlated form
     of the MOVER limits, Zou and Donner 2008), which reduces to the registered interval when the
     sample correlations are zero or undefined;
   - the test-inversion interval of the registered randomization test: the set of Delta_0 for which
     the exact sign-flip test of the shifted pair differences (a_b - Delta_0, c_b - Delta_0) does not
     reject at the 5% level, under a constant additive shift of each within-block difference;
2. estimates by Monte Carlo the coverage of each interval for the true (population-averaged) Delta_A
   under the registered power scenarios and the observed cell proportions, B = 16 blocks and a
   random block intercept on the logit scale (frx.power.simulate_table), for sigma_b in
   {0, 0.5, 1, 2}. The true value is computed by Gauss-Hermite quadrature of the marginal cell
   probabilities under the same model.
"""
from __future__ import annotations

import csv
import hashlib
import io
import itertools
import json
import math
from pathlib import Path

import numpy as np
from scipy.special import expit, logit

from ..metrics import block_contrasts, block_matrix
from ..power import CELL_ORDER, Scenario, _clip, simulate_table
from ..stats import EFFECT_COEFS, block_t_ci, fallback_triggered, mover_wilson, wilson

SEED = 20261029
SIGMAS = (0.0, 0.5, 1.0, 2.0)
LEVEL = 0.95
SCENARIOS_FILE = Path("protocol/power/scenarios.json")


# ------------------------------------------------------------------------------- intervals

def mover_wilson_correlated(y: np.ndarray, coefs=EFFECT_COEFS["A"], level: float = LEVEL) -> tuple:
    """MOVER-Wilson for sum(c_k p_k) with the cell correlations estimated across blocks.

    L = theta - sqrt(sum_k e_k^2 + 2 sum_{j<k} r_jk e_j e_k), e_k = c_k p_k - min(c_k l_k, c_k u_k),
    and likewise for U with f_k = max(c_k l_k, c_k u_k) - c_k p_k; r_jk is the correlation of
    c_j p_j and c_k p_k, i.e. sign(c_j c_k) times the Pearson correlation of columns j and k over
    blocks (0 when a column is constant). The radicand is floored at 0.
    """
    y = np.asarray(y, dtype=float)
    b = y.shape[0]
    xs = [int(v) for v in y.sum(axis=0)]
    est, e, f = 0.0, [], []
    for x, c in zip(xs, coefs):
        p = x / b
        l, u = wilson(x, b, level)
        cp, lo, hi = c * p, min(c * l, c * u), max(c * l, c * u)
        est += cp
        e.append(cp - lo)
        f.append(hi - cp)
    r = np.zeros((4, 4))
    sd = y.std(axis=0)
    for j in range(4):
        for k in range(j + 1, 4):
            if sd[j] > 0 and sd[k] > 0:
                r[j, k] = float(np.corrcoef(y[:, j], y[:, k])[0, 1]) * math.copysign(1.0, coefs[j] * coefs[k])
    lo_sq = sum(v * v for v in e) + 2 * sum(r[j, k] * e[j] * e[k] for j in range(4) for k in range(j + 1, 4))
    hi_sq = sum(v * v for v in f) + 2 * sum(r[j, k] * f[j] * f[k] for j in range(4) for k in range(j + 1, 4))
    return (est, est - math.sqrt(max(lo_sq, 0.0)), est + math.sqrt(max(hi_sq, 0.0)))


def _shift_pvalue(terms: np.ndarray, b: int, delta0: float) -> float:
    """Exact two-sided p-value of the sign-flip test of mean(0.5 * (a + c)) = delta0.

    terms are the 2B pair differences (a_b, c_b); under the shifted sharp null each t - delta0 has a
    random sign. The statistic takes few distinct magnitudes, so its exact distribution is the
    convolution of one binomial per distinct magnitude."""
    v = terms - delta0
    obs = float(np.sum(v)) * 0.5 / b
    mags, counts = np.unique(np.round(np.abs(v), 12), return_counts=True)
    keep = mags > 0
    mags, counts = mags[keep], counts[keep]
    if mags.size == 0:
        return 1.0
    grids = [np.arange(n + 1) for n in counts]
    probs = [np.array([math.comb(int(n), int(i)) for i in range(int(n) + 1)], float) / 2.0 ** n for n in counts]
    stat = np.zeros(1)
    pr = np.ones(1)
    for m, n, g, q in zip(mags, counts, grids, probs):
        stat = (stat[:, None] + m * (2 * g[None, :] - n)).ravel()
        pr = (pr[:, None] * q[None, :]).ravel()
    stat = stat * 0.5 / b
    return float(pr[np.abs(stat) >= abs(obs) - 1e-12].sum())


def inversion_interval(y: np.ndarray, effect: str = "A", alpha: float = 1 - LEVEL, tol: float = 1e-5) -> tuple:
    """Test-inversion interval of the registered sign-flip test under a constant additive shift.

    Each limit is found by bisection between the estimate (accepted, or the nearest accepted point)
    and +-1 (rejected); the acceptance region is taken to be an interval."""
    y = np.asarray(y, dtype=float)
    lc, ld, mc, md = y[:, 0], y[:, 1], y[:, 2], y[:, 3]
    a, c = (ld - lc, md - mc) if effect == "A" else (mc - lc, md - ld)
    b = a.size
    terms = np.concatenate([a, c])
    est = float(np.mean(0.5 * (a + c)))
    acc = lambda d0: _shift_pvalue(terms, b, d0) > alpha      # noqa: E731
    centre = est
    if not acc(centre):
        grid = np.linspace(-1, 1, 401)
        ok = [g for g in grid if acc(g)]
        if not ok:
            return (est, float("nan"), float("nan"))
        centre = min(ok, key=lambda g: abs(g - est))

    def edge(inner: float, outer: float) -> float:
        if acc(outer):
            return outer
        while abs(outer - inner) > tol:
            mid = 0.5 * (inner + outer)
            inner, outer = (mid, outer) if acc(mid) else (inner, mid)
        return inner
    lo, hi = edge(centre, -1.0), edge(centre, 1.0)
    return (est, round(lo, 4) + 0.0, round(hi, 4) + 0.0)     # limits to the bisection tolerance


def registered_interval(y: np.ndarray, effect: str = "A") -> dict:
    d = block_contrasts(y)[effect]
    est, t_lo, t_hi = block_t_ci(d)
    xs = [int(v) for v in y.sum(axis=0)]
    ns = [int(y.shape[0])] * 4
    _, m_lo, m_hi = mover_wilson(xs, ns, EFFECT_COEFS[effect])
    trig = fallback_triggered(y, effect)
    return {"estimate": est, "block_t": (t_lo, t_hi), "mover_wilson": (m_lo, m_hi),
            "fallback_triggered": bool(trig), "registered": (m_lo, m_hi) if trig else (t_lo, t_hi)}


def all_intervals(y: np.ndarray, effect: str = "A") -> dict:
    reg = registered_interval(y, effect)
    _, cm_lo, cm_hi = mover_wilson_correlated(y, EFFECT_COEFS[effect])
    _, i_lo, i_hi = inversion_interval(y, effect)
    return {**reg, "mover_wilson_correlated": (cm_lo, cm_hi), "test_inversion": (i_lo, i_hi)}


# ------------------------------------------------------------------------------- coverage

def marginal_p(p: float, sigma_b: float, nodes: int = 80) -> float:
    """E[expit(logit(p) + u)], u ~ N(0, sigma_b^2), by Gauss-Hermite quadrature."""
    if sigma_b == 0:
        return _clip(p)
    x, w = np.polynomial.hermite_e.hermegauss(nodes)
    return float(np.sum(w * expit(logit(_clip(p)) + sigma_b * x)) / math.sqrt(2 * math.pi))


def true_delta(p: dict, sigma_b: float, effect: str = "A") -> float:
    return float(sum(c * marginal_p(p[k], sigma_b) for c, k in zip(EFFECT_COEFS[effect], CELL_ORDER)))


METHODS = ("registered", "block_t", "mover_wilson", "mover_wilson_correlated", "test_inversion")


def coverage_at(sc: Scenario, b: int, sigma_b: float, n_sims: int, seed: int, effect: str = "A") -> dict:
    rng = np.random.Generator(np.random.PCG64(seed))
    truth = true_delta(sc.p, sigma_b, effect)
    hit = {m: 0 for m in METHODS}
    width = {m: 0.0 for m in METHODS}
    below = {m: 0 for m in METHODS}       # true value below the lower limit (interval too high)
    trig = 0
    for _ in range(n_sims):
        y = simulate_table(rng, sc.p, b, sigma_b)
        iv = all_intervals(y, effect)
        trig += int(iv["fallback_triggered"])
        for m in METHODS:
            lo, hi = iv[m]
            if not (math.isfinite(lo) and math.isfinite(hi)):
                continue
            hit[m] += int(lo - 1e-12 <= truth <= hi + 1e-12)
            below[m] += int(truth < lo - 1e-12)
            width[m] += hi - lo
    out = {"scenario": sc.name, "B": b, "sigma_b": sigma_b, "n_sims": n_sims, "seed": seed,
           "true_delta": truth, "fallback_rate": trig / n_sims}
    for m in METHODS:
        cov = hit[m] / n_sims
        out[f"coverage_{m}"] = cov
        out[f"mc_se_{m}"] = math.sqrt(cov * (1 - cov) / n_sims)
        out[f"miss_low_{m}"] = below[m] / n_sims
        out[f"mean_width_{m}"] = width[m] / n_sims
    return out


def scenarios(repo: Path, observed: dict) -> list[Scenario]:
    js = json.loads((repo / SCENARIOS_FILE).read_text())
    out = [Scenario(s["name"], s["p"]) for s in js["scenarios"]]
    out.append(Scenario("observed", observed))
    return out


def coverage_grid(scs: list[Scenario], b: int, n_sims: int, seed: int = SEED, sigmas=SIGMAS) -> list[dict]:
    rows = []
    for i, sc in enumerate(scs):
        for j, sg in enumerate(sigmas):
            rows.append(coverage_at(sc, b, sg, n_sims, seed + 1_000_003 * i + 101 * j))
    return rows


# ------------------------------------------------------------------------------- run

def _verify_set(d: Path) -> list[str]:
    problems = []
    for line in (Path(d) / "SHA256SUMS").read_text().splitlines():
        h, rel = line.split("  ", 1)
        p = Path(d) / rel
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != h:
            problems.append(f"{rel} does not match SHA256SUMS")
    return problems


def block_matrix_from_run_level(text: str) -> tuple[np.ndarray, list]:
    rows = [r for r in csv.DictReader(io.StringIO(text)) if r["cohort"] == "confirmatory"
            and r["cell"] in CELL_ORDER and r["Y"] not in ("", None)]
    y, complete, _ = block_matrix([{"block": r["block"], "cell": r["cell"], "Y": int(r["Y"])} for r in rows])
    return y, complete


def table_tex(rows: list[dict]) -> str:
    lab = {"null": "Null (0.833)", "null_calibration": "Null (0.5)", "formula_predicted": "Formula-predicted",
           "pilot_informed": "Pilot-informed", "conservative": "Conservative", "observed": "Observed proportions"}
    lines = [r"\begin{tabularx}{\textwidth}{lcCCCCC}", r"\toprule",
             r"\textbf{Scenario} & \textbf{$\sigma_b$} & \textbf{Registered rule} & \textbf{Block $t$} & "
             r"\textbf{MOVER-Wilson} & \textbf{MOVER-Wilson, correlated} & \textbf{Test inversion} \\", r"\midrule"]
    for r in rows:
        lines.append(f"{lab.get(r['scenario'], r['scenario'])} & {r['sigma_b']:g} & " + " & ".join(
            f"{r[f'coverage_{m}']:.3f}" for m in METHODS) + r" \\")
    lines += [r"\bottomrule", r"\end{tabularx}"]
    return "\n".join(lines) + "\n"


def run(cited_set: Path, repo: Path, out: Path, n_sims: int = 4000, seed: int = SEED) -> dict:
    cited_set, out = Path(cited_set), Path(out)
    problems = _verify_set(cited_set)
    if problems:
        raise RuntimeError(f"cited output set fails verification: {problems[:5]}")
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} exists (written once)")
    rl = (cited_set / "data" / "run_level.csv").read_text()
    y, complete = block_matrix_from_run_level(rl)
    b = int(y.shape[0])
    obs_p = {c: float(v) for c, v in zip(CELL_ORDER, y.mean(axis=0))}
    observed = {"B": b, "blocks": complete, "cells": {c: int(v) for c, v in zip(CELL_ORDER, y.sum(axis=0))},
                "intervals_A": all_intervals(y, "A"),
                "test_inversion_p_at_0": _shift_pvalue(np.concatenate([y[:, 1] - y[:, 0], y[:, 3] - y[:, 2]]), b, 0.0)}
    grid = coverage_grid(scenarios(Path(repo), obs_p), b, n_sims, seed)
    res = {"kind": "descriptive post hoc interval check (not registered; registered interval and rule unchanged)",
           "inputs": {"cited_set": str(cited_set), "cited_SHA256SUMS":
                      hashlib.sha256((cited_set / "SHA256SUMS").read_bytes()).hexdigest(),
                      "run_level_sha256": hashlib.sha256(rl.encode()).hexdigest(),
                      "scenarios_sha256": hashlib.sha256((Path(repo) / SCENARIOS_FILE).read_bytes()).hexdigest()},
           "level": LEVEL, "n_sims": n_sims, "seed": seed, "sigmas": list(SIGMAS),
           "observed": observed, "coverage": grid}
    b_csv = io.StringIO()
    w = csv.DictWriter(b_csv, fieldnames=list(grid[0].keys()), lineterminator="\n")
    w.writeheader()
    w.writerows(grid)
    files = {"interval_coverage.json": json.dumps(res, indent=2) + "\n", "interval_coverage.csv": b_csv.getvalue(),
             "tables/latex/TS6_coverage.tex": table_tex(grid)}
    out.mkdir(parents=True, exist_ok=True)
    sums = []
    for rel, text in sorted(files.items()):
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "x") as fh:
            fh.write(text)
        sums.append(f"{hashlib.sha256(text.encode()).hexdigest()}  {rel}\n")
    (out / "SHA256SUMS").write_text("".join(sums))
    return {"out": str(out), "observed": observed,
            "coverage": [{k: r[k] for k in ("scenario", "sigma_b", "true_delta", "fallback_rate",
                                            *[f"coverage_{m}" for m in METHODS])} for r in grid]}
