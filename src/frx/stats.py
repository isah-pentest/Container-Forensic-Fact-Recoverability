"""Inference procedures (Blueprint v3.0, Section 12).

Primary: conditional randomization (sign-flip) test on Delta_A and a block-paired t interval,
with a structure-triggered MOVER-Wilson fallback. No model fitting, no machine learning.
"""
from __future__ import annotations

import itertools
import math
from typing import Sequence

import numpy as np
from scipy import stats as sps

from .metrics import block_contrasts

EXACT_LIMIT = 1_000_000  # enumerate when 4**B <= this


# ---------------------------------------------------------------- proportions

def wilson(x: int, n: int, level: float = 0.95) -> tuple[float, float]:
    """Wilson score interval for x successes in n trials."""
    if n <= 0:
        return (float("nan"), float("nan"))
    z = sps.norm.ppf(0.5 + level / 2)
    p = x / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def clopper_pearson(x: int, n: int, level: float = 0.95) -> tuple[float, float]:
    """Exact (Clopper-Pearson) interval."""
    if n <= 0:
        return (float("nan"), float("nan"))
    a = 1 - level
    lo = 0.0 if x == 0 else sps.beta.ppf(a / 2, x, n - x + 1)
    hi = 1.0 if x == n else sps.beta.ppf(1 - a / 2, x + 1, n - x)
    return (float(lo), float(hi))


def mover_wilson(xs: Sequence[int], ns: Sequence[int], coefs: Sequence[float],
                 level: float = 0.95) -> tuple[float, float, float]:
    """MOVER interval (Zou & Donner) for sum(c_k * p_k) using Wilson limits per proportion.

    Returns (estimate, lower, upper).
    """
    est, lo_sq, hi_sq = 0.0, 0.0, 0.0
    for x, n, c in zip(xs, ns, coefs):
        p = x / n
        l, u = wilson(x, n, level)
        cp, cl, cu = c * p, c * l, c * u
        lo_lim, hi_lim = min(cl, cu), max(cl, cu)
        est += cp
        lo_sq += (cp - lo_lim) ** 2
        hi_sq += (hi_lim - cp) ** 2
    return (est, est - math.sqrt(lo_sq), est + math.sqrt(hi_sq))


EFFECT_COEFS = {
    # order LC, LD, MC, MD
    "A": (-0.5, 0.5, -0.5, 0.5),
    "S": (-0.5, -0.5, 0.5, 0.5),
    "AS": (1.0, -1.0, -1.0, 1.0),
}


# ---------------------------------------------------------------- block statistics

def block_t_ci(d: np.ndarray, level: float = 0.95) -> tuple[float, float, float]:
    """Mean of block contrasts with a t interval (B-1 df). Returns (mean, lower, upper)."""
    d = np.asarray(d, dtype=float)
    b = d.size
    m = float(d.mean())
    if b < 2:
        return (m, float("nan"), float("nan"))
    se = float(d.std(ddof=1)) / math.sqrt(b)
    t = sps.t.ppf(0.5 + level / 2, b - 1)
    return (m, m - t * se, m + t * se)


def _pair_terms(y: np.ndarray, effect: str) -> tuple[np.ndarray, np.ndarray]:
    """The two within-pair differences whose signs are exchangeable under the sharp null."""
    lc, ld, mc, md = y[:, 0], y[:, 1], y[:, 2], y[:, 3]
    if effect == "A":   # swap C<->D within each load level
        return ld - lc, md - mc
    if effect == "S":   # swap L<->M within each arm
        return mc - lc, md - ld
    raise ValueError("randomization test defined for effects 'A' and 'S' only")


def randomization_test(y: np.ndarray, effect: str = "A", n_draws: int = 100_000,
                       seed: int = 0) -> dict:
    """Two-sided conditional randomization test of the sharp null for Delta_A or Delta_S.

    Under the null, each block's two pair differences have independently random signs.
    Statistic: mean over blocks of 0.5*(s1*a_b + s2*c_b). Exact when 4**B <= EXACT_LIMIT.
    """
    y = np.asarray(y, dtype=float)
    a, c = _pair_terms(y, effect)
    b = a.size
    obs = float(np.mean(0.5 * (a + c)))
    terms = np.concatenate([a, c]) * 0.5  # 2B terms, each with a random sign
    tol = 1e-12
    if 4 ** b <= EXACT_LIMIT:
        # enumerate signs only for nonzero terms; zero terms do not change the statistic
        nz = terms[terms != 0]
        k = nz.size
        if k == 0:
            return {"statistic": obs, "p_value": 1.0, "method": "exact", "n_assignments": 1}
        signs = np.array(list(itertools.product((-1.0, 1.0), repeat=k)))
        dist = signs @ nz / b
        p = float(np.mean(np.abs(dist) >= abs(obs) - tol))
        return {"statistic": obs, "p_value": p, "method": "exact", "n_assignments": int(2 ** k)}
    rng = np.random.Generator(np.random.PCG64(seed))
    hits = 0
    chunk = 20_000
    done = 0
    while done < n_draws:
        m = min(chunk, n_draws - done)
        s = rng.choice((-1.0, 1.0), size=(m, terms.size))
        dist = s @ terms / b
        hits += int(np.sum(np.abs(dist) >= abs(obs) - tol))
        done += m
    p = (1 + hits) / (1 + n_draws)
    return {"statistic": obs, "p_value": p, "method": "monte_carlo", "n_assignments": n_draws,
            "seed": seed}


def fallback_triggered(y: np.ndarray, effect: str = "A") -> bool:
    """Structure-only trigger: any cell proportion in {0,1} or < 5 blocks with nonzero contrast."""
    y = np.asarray(y, dtype=float)
    p = y.mean(axis=0)
    if np.any((p == 0) | (p == 1)):
        return True
    d = block_contrasts(y)[effect]
    return int(np.sum(d != 0)) < 5


def primary_analysis(y: np.ndarray, effect: str = "A", n_draws: int = 100_000,
                     seed: int = 0, level: float = 0.95) -> dict:
    """Full Section 12.1 procedure on a complete-block matrix (columns LC, LD, MC, MD)."""
    y = np.asarray(y, dtype=float)
    if y.ndim != 2 or y.shape[1] != 4 or y.shape[0] < 1:
        raise ValueError("expected a B x 4 matrix of complete blocks")
    if not np.all(np.isin(y, (0.0, 1.0))):
        raise ValueError("Y must be binary")
    d = block_contrasts(y)[effect]
    est, t_lo, t_hi = block_t_ci(d, level)
    xs = [int(v) for v in y.sum(axis=0)]
    ns = [int(y.shape[0])] * 4
    _, m_lo, m_hi = mover_wilson(xs, ns, EFFECT_COEFS[effect], level)
    trig = fallback_triggered(y, effect)
    out = {
        "effect": effect,
        "B": int(y.shape[0]),
        "estimate": est,
        "ci_t": (t_lo, t_hi),
        "ci_mover_wilson": (m_lo, m_hi),
        "fallback_triggered": trig,
        "ci_reported": (m_lo, m_hi) if trig else (t_lo, t_hi),
        "ci_rule": "MOVER-Wilson (structure-triggered)" if trig else "block t",
        "cells": {c: (x, n) for c, x, n in zip(("LC", "LD", "MC", "MD"), xs, ns)},
    }
    if effect in ("A", "S"):
        rt = randomization_test(y, effect, n_draws=n_draws, seed=seed)
        out["p_value"] = rt["p_value"]
        out["test"] = rt
    return out


def mcnemar_exact(n10: int, n01: int) -> float:
    """Two-sided exact McNemar p-value from discordant counts."""
    n = n10 + n01
    if n == 0:
        return 1.0
    return float(min(1.0, 2 * sps.binom.cdf(min(n10, n01), n, 0.5)))
