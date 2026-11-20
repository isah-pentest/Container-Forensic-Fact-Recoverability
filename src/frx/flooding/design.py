"""Series F design: flood levels, predictions, schedule and series parameters (computed, never measured).

Every run applies the frozen Low setting (L) before the action, so the pre-termination conditions are
those of the confirmatory LD/LC cells. After TERMINATION_CONFIRMED the flood controller
(frx.flooding.controller) applies the flood setting of the run for a fixed window and then restores L:

  F0  no flood (L throughout)                          no-flood control
  F1  the frozen CAL1 setting during the window        retained for any file offset (W <= 0.8 k M)
  F2  the frozen CAL3 setting during the window        between k M and (k + 1) M: depends on the offset
  F3  the frozen CAL4 setting during the window        lost for any file offset (W >= 1.1 (k + 1) M)

All flood settings are frozen protocol v3.0.1 settings (parameters.yaml load.calibration) with the
frozen replica count, so no new knob is introduced and the equivalence gate G1 measures every setting
used. k = 1 and M = 10 MiB as in the confirmatory series.
"""
from __future__ import annotations

import copy

import numpy as np

from ..headroom import predicted_loss_probability

SERIES = "F"
PREFIX = "CAL"               # the frozen run-status schema admits only the v3.0.1 prefixes and cohorts
COHORT = "calibration"
BLOCK = "CAL-F-BLOCK-01"
LABEL = "flooding"
K_BACKUPS = 1
SEED_OFFSET = 31             # 13: calibration cohort, 29: series R
BASE_LEVEL = "L"
FLOOD_WINDOW_S = 50.0        # flood from TERMINATION_CONFIRMED to t_term + 50 s; the read is at t_term + 60 s
FLOOD_SOURCE = {"F0": None, "F1": "CAL1", "F2": "CAL3", "F3": "CAL4"}
LEVELS = ("F0", "F1", "F2", "F3")
ACTION_S = 8.0               # frozen operating-point assumptions (Section 4.3; Methods 3.5)
EXEC_OFFSET_S = 5.0
MIB = 1024 * 1024
PAIRS = (("F1", "audit"), ("F2", "audit"), ("F3", "audit"), ("F2", "tetragon"))
# seconds from each record to TERMINATION_CONFIRMED under the frozen assumptions
PRE_TERM_S = {"F1": ACTION_S, "F2": ACTION_S - EXEC_OFFSET_S, "F3": 0.0}
RULES = {"F1_max_W_over_kM": 0.8, "F3_min_W_over_k1M": 1.1}

# (cell, arm, flood level, is_control, runs)
CELLS = (("CAL", "D", "F0", 0, 4), ("CAL", "D", "F1", 0, 4), ("CAL", "D", "F2", 0, 4), ("CAL", "D", "F3", 0, 4),
         ("CAL", "C", "F3", 0, 6), ("NC", "D", "F3", 1, 4))


def _base(params: dict) -> dict:
    ld = params["load"]
    return {"knob": {"exec_per_s": float(ld["per_replica_exec_per_s"]["L"]),
                     "api_per_s": float(ld["per_replica_api_per_s"]["L"])},
            "target_bytes_per_s": {s: float(v) for s, v in ld["rate_low_bytes_per_s"].items()}}


def flood_setting(params: dict, level: str) -> dict | None:
    src = FLOOD_SOURCE[level]
    if src is None:
        return None
    c = params["load"]["calibration"][src]
    return {"source_setting": src, "knob": {"exec_per_s": float(c["exec_per_s"]), "api_per_s": float(c["api_per_s"])},
            "target_bytes_per_s": {s: float(v) for s, v in c["target_bytes_per_s"].items()}}


def predicted_written(params: dict, level: str, fact: str, store: str) -> dict:
    """Bytes written to `store` between the record of `fact` and the delayed read, under the frozen
    targets and timing assumptions (the analysis uses the measured value)."""
    delay = float(params["timing"]["delay_s"])
    r_l = _base(params)["target_bytes_per_s"][store]
    fl = flood_setting(params, level)
    r_f = fl["target_bytes_per_s"][store] if fl else r_l
    pre = PRE_TERM_S[fact]
    w = r_l * pre + r_f * FLOOD_WINDOW_S + r_l * (delay - FLOOD_WINDOW_S)
    return {"age_s": pre + delay, "written_bytes": w}


def levels(params: dict, k: int = K_BACKUPS) -> dict:
    m = params["stores"]["audit"]["max_size_mb"] * MIB
    if params["stores"]["tetragon"]["max_size_mb"] * MIB != m:
        raise ValueError("series F assumes the same M for both stores")
    out, problems = {}, []
    for lvl in LEVELS:
        fl = flood_setting(params, lvl)
        pred = {}
        for fact, src in PAIRS:
            p = predicted_written(params, lvl, fact, src)
            w = p["written_bytes"]
            pred[f"{fact}-{src}"] = {**p, "W_over_kM": w / (k * m), "W_over_k1M": w / ((k + 1) * m),
                                     "H_worst_offset": k * m / w, "H_best_offset": (k + 1) * m / w,
                                     "p_loss_formula": predicted_loss_probability(w / p["age_s"], p["age_s"], m, k)}
        wk = [v["W_over_kM"] for v in pred.values()]
        wk1 = [v["W_over_k1M"] for v in pred.values()]
        if lvl in ("F0", "F1") and max(wk) > RULES["F1_max_W_over_kM"]:
            problems.append(f"{lvl}: largest W / kM = {max(wk):.3f} > {RULES['F1_max_W_over_kM']}")
        if lvl == "F2" and not (min(wk) > 1.0 and max(wk1) < 1.0):
            problems.append(f"{lvl}: W not strictly between kM and (k+1)M for every pair")
        if lvl == "F3" and min(wk1) < RULES["F3_min_W_over_k1M"]:
            problems.append(f"{lvl}: smallest W / (k+1)M = {min(wk1):.3f} < {RULES['F3_min_W_over_k1M']}")
        out[lvl] = {"flood": fl, "predicted": pred,
                    "predicted_outcome": {"F0": "retained", "F1": "retained", "F2": "depends on offset",
                                          "F3": "lost"}[lvl]}
    return {"k": k, "M_bytes": m, "replicas": int(params["load"]["replicas"]), "base": _base(params),
            "flood_window_s": FLOOD_WINDOW_S, "levels": out, "problems": problems}


def schedule(master_seed: int) -> list[dict]:
    """26 runs in one seeded block: the cell order is a permutation drawn with PCG64(master_seed + 31)."""
    rng = np.random.Generator(np.random.PCG64(master_seed + SEED_OFFSET))
    slots = [(cell, arm, lvl, ctl) for cell, arm, lvl, ctl, n in CELLS for _ in range(n)]
    order = rng.permutation(len(slots))
    return [{"block": BLOCK, "slot": i + 1, "cell": slots[j][0], "load": slots[j][2], "arm": slots[j][1],
             "is_control": slots[j][3]} for i, j in enumerate(order)]


def series_params(params: dict, plan: dict) -> dict:
    """The frozen parameters with F0-F3 available to the frozen block runner as load levels whose
    setting is the frozen L setting (the flood is applied by the controller, not by the runner), the
    series' k and fingerprint recorded. The frozen parameters file itself is not changed."""
    p = copy.deepcopy(params)
    b = _base(params)
    for lvl in LEVELS:
        p["load"]["calibration"][lvl] = {"target_bytes_per_s": dict(b["target_bytes_per_s"]),
                                         "exec_per_s": b["knob"]["exec_per_s"], "api_per_s": b["knob"]["api_per_s"]}
    for s in ("audit", "tetragon"):
        p["stores"][s]["max_backups"] = plan["k"]
    p["host"]["fingerprint_stable_hash"] = plan["fingerprint_stable_hash"]
    return p
