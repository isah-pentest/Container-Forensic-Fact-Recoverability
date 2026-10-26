"""Seeded randomized complete-block schedule (Section 8.2).

Each block has five slots {LC, LD, MC, MD, NC} in a uniformly random order. The NC slot's
load level and arm are each drawn with probability 1/2. Deterministic given the seed.
"""
from __future__ import annotations

import csv
import hashlib
from pathlib import Path

import numpy as np

SLOTS = ("LC", "LD", "MC", "MD", "NC")
CELL_FACTORS = {"LC": ("L", "C"), "LD": ("L", "D"), "MC": ("M", "C"), "MD": ("M", "D")}


def make_schedule(master_seed: int, n_blocks: int, cohort_prefix: str = "BLOCK") -> list[dict]:
    if n_blocks < 1:
        raise ValueError("n_blocks must be >= 1")
    rng = np.random.Generator(np.random.PCG64(master_seed))
    rows = []
    for b in range(1, n_blocks + 1):
        order = rng.permutation(len(SLOTS))
        nc_load = "L" if rng.random() < 0.5 else "M"
        nc_arm = "C" if rng.random() < 0.5 else "D"
        for slot_no, idx in enumerate(order, start=1):
            cell = SLOTS[idx]
            load, arm = CELL_FACTORS.get(cell, (nc_load, nc_arm))
            rows.append({
                "block": f"{cohort_prefix}-{b:02d}", "slot": slot_no, "cell": cell,
                "load": load, "arm": arm, "is_control": int(cell == "NC"),
            })
    return rows


def write_schedule(rows: list[dict], path: Path, master_seed: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        fh.write(f"# master_seed={master_seed}\n")
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return hashlib.sha256(path.read_bytes()).hexdigest()
