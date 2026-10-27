"""Qualification series Q and the inputs it needs (Blueprint v3.0, Sections 4.3, 13, 18, 22.2).

Order of work, all before any outcome is seen:
  1. load_map():          metadata-only measurement of store write rate against load-generator rates;
  2. operating_points():  Section 4.3 arithmetic -> r_L and Q-c test rates (and r_M, calibration);
  3. make_plan():         seeded order within each part (shakedown, Q-a, Q-b, Q-c);
  4. run_part():          one load Deployment per part, execute_run() per slot, gate after each run.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np

from .. import load as loadctl
from ..headroom import predicted_loss_probability
from ..monitors import GROWTH_HEADER, growth_from_rows, sample_stores
from .package import next_run_id
from .run import RunConfig, RunSpec, execute_run

# Per-replica ceilings of the load script (one exec loop, one API loop per replica).
MAX_EXEC_PER_REPLICA = 10.0
MAX_API_PER_REPLICA = 20.0


# ---------------------------------------------------------------------- 1. load map
def load_map(repo: Path, stores: dict, image: str, replicas: int, settings: list[dict],
             warmup_s: int = 20, measure_s: int = 60, first_warmup_s: int = 60, log=print) -> dict:
    """Measure bytes/s per store at each (exec_per_s, api_per_s) per-replica setting. Metadata only."""
    loadctl.deploy(repo, image, replicas, settings[0]["exec_per_s"], settings[0]["api_per_s"], level="MAP")
    time.sleep(first_warmup_s)
    rows_out = []
    try:
        for i, s in enumerate(settings):
            loadctl.set_rate(s["exec_per_s"], s["api_per_s"], f"MAP{i}")
            time.sleep(warmup_s)
            rows = []
            for _ in range(measure_s + 1):
                rows += [dict(zip(GROWTH_HEADER, r)) for r in sample_stores(stores)]
                time.sleep(1)
            g = growth_from_rows(rows)
            rec = {**s, "replicas": replicas,
                   "total_exec_per_s": s["exec_per_s"] * replicas, "total_api_per_s": s["api_per_s"] * replicas,
                   "bytes_per_s": {k: round(v["bytes_per_s"], 1) for k, v in g.items()}}
            rows_out.append(rec)
            log(json.dumps(rec))
    finally:
        loadctl.delete(wait=True)
    return {"replicas": replicas, "image": image, "measure_s": measure_s, "points": rows_out,
            "fit": fit_load_map(rows_out)}


def fit_load_map(points: list[dict]) -> dict:
    """Least squares: audit ~ a + b * total API GET/s; tetragon ~ a + b * total exec/s.

    Cross terms (audit vs exec, tetragon vs API) are fitted too and reported, to show the two
    knobs act on separate stores.
    """
    out = {}
    for store, own, other in (("audit", "total_api_per_s", "total_exec_per_s"),
                              ("tetragon", "total_exec_per_s", "total_api_per_s")):
        y = np.array([p["bytes_per_s"].get(store, 0.0) for p in points])
        X = np.column_stack([np.ones(len(points)), [p[own] for p in points], [p[other] for p in points]])
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        pred = X @ coef
        ss_res = float(((y - pred) ** 2).sum())
        ss_tot = float(((y - y.mean()) ** 2).sum()) or 1.0
        out[store] = {"knob": own, "intercept_bytes_per_s": float(coef[0]), "bytes_per_unit": float(coef[1]),
                      "cross_bytes_per_unit": float(coef[2]), "r2": 1 - ss_res / ss_tot}
    return out


# ---------------------------------------------------------------------- 2. operating points
def operating_points(load_fit: dict, stores_mb: dict, delay_s: float, action_s: float,
                     exec_age_offset_s: float, tolerance: float = 0.20, h_low: float = 3.0,
                     p_moderate: float = 0.5, q_c_h: tuple = (2.0, 1.0, 0.5),
                     cal_h: tuple = (2.0, 1.2, 0.8, 0.5)) -> dict:
    """Section 4.3 rules. Ages: oldest primary fact per store at the D read.

    audit:    F1 (pod create) is read delay_s + action_s after it was written;
    tetragon: F2 (exec) is read delay_s + (action_s - exec_age_offset_s) after it was written.
    r_L: worst-case headroom (B = kM) >= h_low even at +tolerance write rate.
    r_M, Q-c and calibration rates use the expected offset (B = kM + M/2).
    """
    age = {"audit": delay_s + action_s, "tetragon": delay_s + action_s - exec_age_offset_s}
    MB = 1024 * 1024        # lumberjack (audit log and Tetragon export) counts MaxSize in MiB
    levels: dict = {}
    per_store = {}
    for st in ("audit", "tetragon"):
        M = stores_mb[st]["max_mb"] * MB
        k = stores_mb[st]["backups"]
        if k < 1:
            raise ValueError("k >= 1 required (Section 4.3)")
        dt = age[st]
        r_low = k * M / (h_low * dt) / (1 + tolerance)
        r_mod = (k * M + p_moderate * M) / dt
        per_store[st] = {"M_bytes": M, "k": k, "age_s": dt, "r_L": r_low, "r_M": r_mod,
                         "QC": [(k * M + M / 2) / (h * dt) for h in q_c_h],
                         "CAL": [(k * M + M / 2) / (h * dt) for h in cal_h],
                         "p_loss_r_M_at_minus_tol": predicted_loss_probability(r_mod * (1 - tolerance), dt, M, k),
                         "p_loss_r_M_at_plus_tol": predicted_loss_probability(r_mod * (1 + tolerance), dt, M, k),
                         "worst_case_H_at_r_L_plus_tol": k * M / (r_low * (1 + tolerance) * dt)}
    names = {"L": ("r_L", None), "M": ("r_M", None)}
    names.update({f"QC{i + 1}": ("QC", i) for i in range(len(q_c_h))})
    names.update({f"CAL{i + 1}": ("CAL", i) for i in range(len(cal_h))})
    for lvl, (key, idx) in names.items():
        tgt = {st: (per_store[st][key] if idx is None else per_store[st][key][idx]) for st in per_store}
        levels[lvl] = {"target_bytes_per_s": {s: round(v, 1) for s, v in tgt.items()}}
    # knob totals from the fitted map
    problems = []
    for lvl, d in levels.items():
        tot = {}
        for st, knob in (("audit", "total_api_per_s"), ("tetragon", "total_exec_per_s")):
            f = load_fit[st]
            need = (d["target_bytes_per_s"][st] - f["intercept_bytes_per_s"]) / f["bytes_per_unit"]
            if need < 0:
                problems.append(f"{lvl}: {st} target {d['target_bytes_per_s'][st]:.0f} B/s is below the idle "
                                f"rate {f['intercept_bytes_per_s']:.0f} B/s (store too large for this rule)")
            tot[knob] = max(0.0, need)
        d["totals"] = tot
    top = max(levels.values(), key=lambda d: (d["totals"]["total_api_per_s"] / MAX_API_PER_REPLICA,
                                              d["totals"]["total_exec_per_s"] / MAX_EXEC_PER_REPLICA))
    replicas = max(1, math.ceil(max(top["totals"]["total_api_per_s"] / MAX_API_PER_REPLICA,
                                    top["totals"]["total_exec_per_s"] / MAX_EXEC_PER_REPLICA)))
    # the 1 Hz growth monitor measures write rate only if each file lives well over 1 s
    for st, v in per_store.items():
        fastest = max(levels[l]["target_bytes_per_s"][st] for l in levels)
        if v["M_bytes"] / fastest < 5.0:
            problems.append(f"{st}: a {v['M_bytes']} B file fills in {v['M_bytes'] / fastest:.1f} s at "
                            f"{fastest:.0f} B/s; need >= 5 s for 1 Hz rate measurement")
    for d in levels.values():
        api = d["totals"]["total_api_per_s"] / replicas
        if api < 1.0:            # the load script polls its rate at >= 1 GET/s per replica
            problems.append(f"API rate {api:.2f}/s per replica is below the script minimum of 1/s; "
                            "the audit target cannot be lowered that far with this replica count")
        d["knob"] = {"exec_per_s": round(d["totals"]["total_exec_per_s"] / replicas, 3),
                     "api_per_s": round(max(1.0, api), 3)}
    return {"stores": stores_mb, "ages_s": age, "replicas": replicas, "levels": levels,
            "per_store": per_store, "problems": sorted(set(problems)),
            "rule": {"h_low": h_low, "p_moderate": p_moderate, "tolerance": tolerance, "q_c_h": list(q_c_h),
                     "calibration_h": list(cal_h)}}


# ---------------------------------------------------------------------- 3. plan
def make_plan(seed: int, levels: dict) -> dict:
    """Seeded random order within each part (Section 18). The plan is written before any run."""
    rng = np.random.Generator(np.random.PCG64(seed))
    parts = {
        "shakedown": [{"arm": "C", "level": "L"}, {"arm": "D", "level": "L"},
                      {"arm": "C", "level": "L", "is_control": True}],
        "Q-a": [{"arm": "C", "level": "L"} for _ in range(12)],
        "Q-b": [{"arm": "D", "level": "L"} for _ in range(12)],
        "Q-c": [{"arm": "D", "level": f"QC{i}"} for i in (1, 2, 3) for _ in range(3)],
    }
    for name in ("Q-c",):
        order = rng.permutation(len(parts[name]))
        parts[name] = [parts[name][i] for i in order]
    missing = {s["level"] for p in parts.values() for s in p} - set(levels)
    if missing:
        raise ValueError(f"operating points lack levels {sorted(missing)}")
    return {"seed": seed, "created_wall_ns": time.time_ns(), "parts": parts,
            "note": "shakedown runs are archived QUAL- attempts but are not part of Q-a/Q-b/Q-c"}


# ---------------------------------------------------------------------- 4. run a part
def _progress(path: Path) -> list[dict]:
    return [json.loads(l) for l in open(path)] if path.exists() else []


def run_part(part: str, plan: dict, ops: dict, cfg: RunConfig, plat, repo: Path, progress_path: Path,
             max_replacements: int = 3, log=print, block_warmup_s: int = 60, keep_load: bool = False) -> dict:
    """Run (or resume) one part. Stops on a failed package gate or the part's replacement ceiling."""
    slots = plan["parts"][part]
    done = [p for p in _progress(progress_path) if p["part"] == part]
    scored = {p["slot"] for p in done if p["disposition"] == "SCORED"}
    replaced = sum(1 for p in done if p.get("replaces_run_id"))
    queue = [(i, s, None) for i, s in enumerate(slots) if i not in scored]
    # earlier non-scored attempts of unfinished slots are replaced at the end of the part
    for p in done:
        if p["disposition"] != "SCORED" and p["slot"] not in scored and not any(
                q.get("replaces_run_id") == p["run_id"] for q in done):
            queue = [q for q in queue if q[0] != p["slot"]] + [(p["slot"], slots[p["slot"]], p["run_id"])]
    if not queue:
        return {"part": part, "complete": True, "runs": []}
    first = ops["levels"][queue[0][1]["level"]]["knob"]
    loadctl.deploy(repo, plat.load_image, ops["replicas"], first["exec_per_s"], first["api_per_s"],
                   level=queue[0][1]["level"])
    log(f"load deployed ({ops['replicas']} replicas); block warm-up {block_warmup_s} s")
    time.sleep(block_warmup_s)
    prev = None
    last = [p for p in done if p.get("identity") and p["disposition"] in ("SCORED", "UNSCORABLE")
            and not plan["parts"][part][p["slot"]].get("is_control")]
    if last:
        prev = last[-1]["identity"]
    results, consecutive_oom = [], 0
    stopped = None
    try:
        while queue:
            idx, slot, replaces = queue.pop(0)
            lvl = ops["levels"][slot["level"]]
            spec = RunSpec(run_id=next_run_id("QUAL", cfg.ledger, Path(cfg.out_root)), cohort="qualification",
                           part=part, cell="NC" if slot.get("is_control") else "Q", arm=slot["arm"],
                           load_level=slot["level"], knob=lvl["knob"], target_rates=lvl["target_bytes_per_s"],
                           block=part, slot=idx + 1, is_control=bool(slot.get("is_control")),
                           replaces_run_id=replaces, prev_identity=prev)
            r = execute_run(spec, cfg, plat)
            disp = r["status"].get("disposition", "INVALID")
            rec = {"part": part, "slot": idx, "run_id": r["run_id"], "disposition": disp,
                   "primary_failure_code": r["status"].get("primary_failure_code"),
                   "replaces_run_id": replaces, "identity": r["identity"], "gate_ok": r["gate"]["ok"]}
            with open(progress_path, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
            results.append(rec)
            log(json.dumps(rec))
            if not spec.is_control and disp in ("SCORED", "UNSCORABLE"):
                prev = r["identity"]            # decoy for the next run: the last pod that existed
            if not r["gate"]["ok"]:
                stopped = f"PK-INCOMPLETE on {r['run_id']}: {r['gate']['problems']}"
                break
            consecutive_oom = consecutive_oom + 1 if r["status"].get("flags", {}).get("oom_event") else 0
            if consecutive_oom >= 2:
                stopped = "OOM in two consecutive runs"
                break
            if disp != "SCORED":
                replaced += 1
                if replaced > max_replacements:
                    stopped = f"replacement ceiling ({max_replacements}) exceeded in {part}"
                    break
                queue.append((idx, slot, r["run_id"]))
    finally:
        if not keep_load:
            loadctl.delete(wait=True)
    return {"part": part, "complete": not queue and stopped is None, "stopped": stopped, "runs": results}
