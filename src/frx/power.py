"""Monte Carlo power simulation for the randomized complete-block design (Section 12.5).

Synthetic B x 4 tables of 0/1 outcomes are drawn from assumed cell probabilities with a
random block intercept on the logit scale, then analysed with the exact primary test.
Nothing here reads, writes or influences real evidence.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np
from scipy.special import expit, logit

from .stats import randomization_test, fallback_triggered

CELL_ORDER = ("LC", "LD", "MC", "MD")


@dataclass(frozen=True)
class Scenario:
    name: str
    p: Mapping[str, float]  # cell -> probability of Y = 1


def _clip(p: float) -> float:
    return min(max(p, 1e-6), 1 - 1e-6)


def simulate_table(rng: np.random.Generator, p: Mapping[str, float], b: int,
                   sigma_b: float) -> np.ndarray:
    base = np.array([logit(_clip(p[c])) for c in CELL_ORDER])
    u = rng.normal(0.0, sigma_b, size=(b, 1)) if sigma_b > 0 else np.zeros((b, 1))
    prob = expit(base[None, :] + u)
    return (rng.random((b, 4)) < prob).astype(float)


def power_at(scenario: Scenario, b: int, sigma_b: float, n_sims: int, n_draws: int,
             seed: int, alpha: float = 0.05) -> dict:
    rng = np.random.Generator(np.random.PCG64(seed))
    rejections, fallbacks = 0, 0
    for k in range(n_sims):
        y = simulate_table(rng, scenario.p, b, sigma_b)
        res = randomization_test(y, "A", n_draws=n_draws, seed=seed + 1 + k)
        rejections += int(res["p_value"] < alpha)
        fallbacks += int(fallback_triggered(y, "A"))
    pw = rejections / n_sims
    return {
        "scenario": scenario.name, "B": b, "sigma_b": sigma_b, "n_sims": n_sims,
        "n_draws": n_draws, "power": pw, "mc_se": math.sqrt(pw * (1 - pw) / n_sims),
        "fallback_rate": fallbacks / n_sims, "seed": seed,
    }


def run_grid(scenarios: Iterable[Scenario], b_values: Iterable[int],
             sigmas: Iterable[float], n_sims: int, n_draws: int, seed: int,
             out_csv: Path | None = None) -> list[dict]:
    rows = []
    for si, sc in enumerate(scenarios):
        for b in b_values:
            for sg_i, sg in enumerate(sigmas):
                cell_seed = seed + 1_000_003 * si + 10_007 * b + 101 * sg_i
                rows.append(power_at(sc, b, sg, n_sims, n_draws, cell_seed))
    if out_csv is not None:
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        with open(out_csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    return rows


def choose_b(rows: list[dict], primary: str, conservative: str, sigma_b: float = 0.5,
             target_primary: float = 0.80, target_conservative: float = 0.70,
             b_max: int = 24) -> int | None:
    """Smallest B meeting the Section 12.5 rule, or None (H1 withdrawn)."""
    def pw(name, b):
        for r in rows:
            if r["scenario"] == name and r["B"] == b and abs(r["sigma_b"] - sigma_b) < 1e-9:
                return r["power"]
        return None
    for b in sorted({r["B"] for r in rows}):
        if b > b_max:
            break
        p1, p2 = pw(primary, b), pw(conservative, b)
        if p1 is not None and p2 is not None and p1 >= target_primary and p2 >= target_conservative:
            return b
    return None
