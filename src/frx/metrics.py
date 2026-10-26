"""Fact-level outcome metrics (Blueprint v3.0, Section 11).

All functions are pure and deterministic. Cell codes: LC, LD, MC, MD.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Sequence

import numpy as np

STATES = ("CORRECT", "FALSE", "ABSTAIN", "NOT_ELIGIBLE")
UNION_STATES = ("CORRECT", "CONFLICT", "NOT_RECOVERED")
PRIMARY_FACTS = ("F1", "F2", "F3")
CELLS = ("LC", "LD", "MC", "MD")


def union_state(states: Mapping[str, str]) -> str:
    """Union rule (Section 7.5) over the sources of one (run, fact).

    `states` maps source -> state. NOT_ELIGIBLE sources are ignored.
    CORRECT if >=1 eligible source is CORRECT and none is FALSE;
    CONFLICT if any eligible source is FALSE; otherwise NOT_RECOVERED.
    """
    for s in states.values():
        if s not in STATES:
            raise ValueError(f"unknown state {s!r}")
    eligible = [s for s in states.values() if s != "NOT_ELIGIBLE"]
    if not eligible:
        raise ValueError("fact has no eligible source")
    if "FALSE" in eligible:
        return "CONFLICT"
    if "CORRECT" in eligible:
        return "CORRECT"
    return "NOT_RECOVERED"


def fact_recovered(states: Mapping[str, str]) -> int:
    """E_ij: 1 if union state is CORRECT, else 0."""
    return int(union_state(states) == "CORRECT")


def run_outcome(fact_states: Mapping[str, Mapping[str, str]],
                primary: Sequence[str] = PRIMARY_FACTS) -> tuple[int, float]:
    """Return (Y_i, C_i) for one run.

    `fact_states` maps fact id -> {source: state}. Every primary fact must be present.
    """
    missing = [f for f in primary if f not in fact_states]
    if missing:
        raise ValueError(f"missing primary facts: {missing}")
    e = [fact_recovered(fact_states[f]) for f in primary]
    return int(all(e)), sum(e) / len(primary)


def cell_proportions(runs: Iterable[Mapping]) -> dict[str, tuple[int, int, float]]:
    """p_sa per cell. `runs` items need keys 'cell' and 'Y'. Returns {cell: (x, n, p)}."""
    acc = {c: [0, 0] for c in CELLS}
    for r in runs:
        c = r["cell"]
        if c not in acc:
            continue  # controls and other cohorts are never in the denominator
        acc[c][0] += int(r["Y"])
        acc[c][1] += 1
    out = {}
    for c, (x, n) in acc.items():
        out[c] = (x, n, (x / n) if n else float("nan"))
    return out


def deltas(p: Mapping[str, float]) -> dict[str, float]:
    """Delta_A, Delta_S, Delta_AS from cell probabilities (delayed - continuous; moderate - low)."""
    d_a = 0.5 * ((p["LD"] - p["LC"]) + (p["MD"] - p["MC"]))
    d_s = 0.5 * ((p["MC"] - p["LC"]) + (p["MD"] - p["LD"]))
    d_as = (p["MD"] - p["MC"]) - (p["LD"] - p["LC"])
    return {"delta_A": d_a, "delta_S": d_s, "delta_AS": d_as}


def block_matrix(runs: Iterable[Mapping]) -> tuple[np.ndarray, list, list]:
    """Build a B x 4 matrix of Y (columns LC, LD, MC, MD) from complete blocks only.

    `runs` items need 'block', 'cell', 'Y'. A block with more than one scored run in a
    cell is an error (replacements must replace, not add). Returns (Y, complete_ids, incomplete_ids).
    """
    by_block: dict = {}
    for r in runs:
        if r["cell"] not in CELLS:
            continue
        cells = by_block.setdefault(r["block"], {})
        if r["cell"] in cells:
            raise ValueError(f"block {r['block']} has two scored runs in cell {r['cell']}")
        cells[r["cell"]] = int(r["Y"])
    complete, incomplete, rows = [], [], []
    for b in sorted(by_block):
        cells = by_block[b]
        if all(c in cells for c in CELLS):
            complete.append(b)
            rows.append([cells[c] for c in CELLS])
        else:
            incomplete.append(b)
    y = np.array(rows, dtype=float).reshape(-1, 4)
    return y, complete, incomplete


def block_contrasts(y: np.ndarray) -> dict[str, np.ndarray]:
    """Per-block contrasts d^A_b, d^S_b, d^AS_b from a B x 4 matrix (LC, LD, MC, MD)."""
    lc, ld, mc, md = y[:, 0], y[:, 1], y[:, 2], y[:, 3]
    return {
        "A": 0.5 * ((ld - lc) + (md - mc)),
        "S": 0.5 * ((mc - lc) + (md - ld)),
        "AS": (md - mc) - (ld - lc),
    }


def source_rates(fact_source_states: Iterable[str]) -> dict[str, tuple[int, int]]:
    """False-assertion and abstention counts over eligible (run, fact) pairs for one source.

    Returns {'FALSE': (count, eligible_n), 'ABSTAIN': (count, eligible_n), 'CORRECT': (...)}.
    """
    eligible = [s for s in fact_source_states if s != "NOT_ELIGIBLE"]
    n = len(eligible)
    return {k: (sum(1 for s in eligible if s == k), n) for k in ("CORRECT", "FALSE", "ABSTAIN")}


def f2_paired_table(pairs: Iterable[tuple[str, str]]) -> dict[str, int]:
    """Paired (audit, tetragon) F2 states -> n11, n10, n01, n00 of CORRECT indicators."""
    t = {"n11": 0, "n10": 0, "n01": 0, "n00": 0}
    for a, b in pairs:
        ka, kb = int(a == "CORRECT"), int(b == "CORRECT")
        t[f"n{ka}{kb}"] += 1
    return t


def incremental_contribution(pairs: Iterable[tuple[str, str]]) -> dict[str, tuple[int, int]]:
    """I_A and I_T: runs where union F2 is CORRECT but the named source is not CORRECT."""
    pairs = list(pairs)
    union_ok = [(a, b) for a, b in pairs if union_state({"audit": a, "tetragon": b}) == "CORRECT"]
    n = len(union_ok)
    i_a = sum(1 for a, _ in union_ok if a != "CORRECT")
    i_t = sum(1 for _, b in union_ok if b != "CORRECT")
    return {"I_audit": (i_a, n), "I_tetragon": (i_t, n)}
