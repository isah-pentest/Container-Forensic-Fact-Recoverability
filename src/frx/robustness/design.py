"""Series R design: levels, predictions, schedule and series parameters (computed, never measured).

Levels (k = 2, M = 10 MiB, delta = 60 s; rates per store chosen by the frozen Section 4.3 rule of
frx.orchestrator.qual.operating_points from the frozen load map):
  R1  the confirmatory moderate rate itself (the frozen MD knob, unchanged): at k = 2 the model
      predicts no loss of any primary record for any file offset;
  R2, R3, R4  rates at which the expected-offset headroom of the oldest primary record per store is
      1.2, 1.0 and 0.8 (as the calibration series did for k = 1 with 2.0, 1.2, 0.8, 0.5).
Four delayed runs per level in one seeded block (seed = master_seed + 29).
"""
from __future__ import annotations

import copy
import math

import numpy as np

from ..headroom import predicted_loss_probability
from ..orchestrator.qual import MAX_API_PER_REPLICA, MAX_EXEC_PER_REPLICA, operating_points

SERIES = "R"
# The frozen run-status schema admits only the v3.0.1 cohorts, ID prefixes and cells, so series-R
# attempts are calibration-type attempts (CAL- IDs, cell CAL) numbered on from the shared append-only
# attempt ledger; they live in their own tree and block, and the analysis labels them series R.
PREFIX = "CAL"
COHORT = "calibration"
CELL = "CAL"
BLOCK = "CAL-R-BLOCK-01"
LABEL = "robustness"
K_BACKUPS = 2
H_TARGETS = {"R2": 1.2, "R3": 1.0, "R4": 0.8}
LEVELS = ("R1", "R2", "R3", "R4")
RUNS_PER_LEVEL = 4
SEED_OFFSET = 29
ACTION_S = 8.0               # the frozen operating-point assumptions (Section 4.3; Methods 3.5)
EXEC_OFFSET_S = 5.0
MIB = 1024 * 1024
PAIRS = (("F1", "audit"), ("F2", "audit"), ("F3", "audit"), ("F2", "tetragon"))


def store_spec(params: dict, k: int = K_BACKUPS) -> dict:
    st = params["stores"]
    return {"audit": {"max_mb": st["audit"]["max_size_mb"], "backups": k},
            "tetragon": {"max_mb": st["tetragon"]["max_size_mb"], "backups": k}}


def _predict(rate: float, age: float, m: int, k: int) -> dict:
    w = rate * age
    return {"age_s": age, "written_bytes": w, "H_expected_offset": (k * m + m / 2) / w,
            "H_worst_offset": k * m / w, "H_best_offset": (k + 1) * m / w,
            "p_loss_formula": predicted_loss_probability(rate, age, m, k),
            "p_loss_formula_k1": predicted_loss_probability(rate, age, m, 1)}


def levels(load_fit: dict, params: dict, k: int = K_BACKUPS) -> dict:
    """Targets, per-replica knobs and model predictions for R1-R4 (deterministic)."""
    t = params["timing"]
    replicas = int(params["load"]["replicas"])
    ops = operating_points(load_fit, store_spec(params, k), t["delay_s"], ACTION_S, EXEC_OFFSET_S,
                           tolerance=params["load"]["manipulation_tolerance"],
                           cal_h=tuple(H_TARGETS[x] for x in ("R2", "R3", "R4")))
    out, problems = {}, [p for p in ops["problems"] if "below the idle" in p or "1 Hz" in p]
    ld = params["load"]
    out["R1"] = {"rule": "confirmatory moderate rate and knob (protocol v3.0.1, unchanged)",
                 "target_bytes_per_s": {s: float(v) for s, v in ld["rate_moderate_bytes_per_s"].items()},
                 "knob": {"exec_per_s": float(ld["per_replica_exec_per_s"]["M"]),
                          "api_per_s": float(ld["per_replica_api_per_s"]["M"])}}
    for i, lvl in enumerate(("R2", "R3", "R4")):
        d = ops["levels"][f"CAL{i + 1}"]
        tot = d["totals"]
        knob = {"exec_per_s": round(tot["total_exec_per_s"] / replicas, 3),
                "api_per_s": round(max(1.0, tot["total_api_per_s"] / replicas), 3)}
        if knob["exec_per_s"] > MAX_EXEC_PER_REPLICA or knob["api_per_s"] > MAX_API_PER_REPLICA:
            problems.append(f"{lvl}: knob {knob} exceeds the per-replica maximum "
                            f"({MAX_EXEC_PER_REPLICA} exec/s, {MAX_API_PER_REPLICA} API/s)")
        out[lvl] = {"rule": f"expected-offset headroom {H_TARGETS[lvl]} for the oldest primary record per store",
                    "target_bytes_per_s": d["target_bytes_per_s"], "knob": knob,
                    "totals": {"api_per_s": tot["total_api_per_s"], "exec_per_s": tot["total_exec_per_s"]}}
    ages = {"F1": t["delay_s"] + ACTION_S, "F2": t["delay_s"] + ACTION_S - EXEC_OFFSET_S}
    for lvl, d in out.items():
        d["predicted"] = {}
        for fact, src in (("F1", "audit"), ("F2", "tetragon")):
            m = store_spec(params, k)[src]["max_mb"] * MIB
            d["predicted"][f"{fact}-{src}"] = _predict(d["target_bytes_per_s"][src], ages[fact], m, k)
    return {"k": k, "replicas": replicas, "ages_s": ages, "levels": out, "problems": sorted(set(problems)),
            "load_fit": load_fit}


def schedule(master_seed: int, runs_per_level: int = RUNS_PER_LEVEL) -> list[dict]:
    """16 delayed runs, 4 per level, seeded order, one block (as the calibration series)."""
    rng = np.random.Generator(np.random.PCG64(master_seed + SEED_OFFSET))
    lv = [lvl for lvl in LEVELS for _ in range(runs_per_level)]
    order = rng.permutation(len(lv))
    return [{"block": BLOCK, "slot": i + 1, "cell": CELL, "load": lv[j], "arm": "D", "is_control": 0}
            for i, j in enumerate(order)]


def series_params(params: dict, plan: dict) -> dict:
    """The frozen parameters with the R levels available to the frozen block runner (as load levels
    next to the calibration levels) and the series' store spec and fingerprint recorded. The frozen
    parameters file itself is not changed."""
    p = copy.deepcopy(params)
    # protocol_version stays the frozen "3.0.1": the run-status schema admits only "3.x[.y]", and the
    # attempts are run by the v3.0.1 code; the series is identified by its block, tree and plan.
    for lvl, d in plan["levels"].items():
        p["load"]["calibration"][lvl] = {"target_bytes_per_s": d["target_bytes_per_s"],
                                         "exec_per_s": d["knob"]["exec_per_s"], "api_per_s": d["knob"]["api_per_s"]}
    for s in ("audit", "tetragon"):
        p["stores"][s]["max_backups"] = plan["k"]
    p["host"]["fingerprint_stable_hash"] = plan["fingerprint_stable_hash"]
    return p


def expected_losses(plan: dict) -> dict:
    """Formula-expected number of lost records per level and fact-source over the planned runs."""
    n = plan["runs_per_level"]
    return {lvl: {key: round(n * v["p_loss_formula"], 3) for key, v in d["predicted"].items()}
            for lvl, d in plan["levels"].items()}


def isfinite_all(x) -> bool:
    if isinstance(x, dict):
        return all(isfinite_all(v) for v in x.values())
    if isinstance(x, (list, tuple)):
        return all(isfinite_all(v) for v in x)
    if isinstance(x, float):
        return math.isfinite(x)
    return True
