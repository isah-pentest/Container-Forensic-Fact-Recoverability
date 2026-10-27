"""Confirmatory cohort and calibration series (Blueprint v3.0.1 amendment; Sections 8.3, 10.4, 13, 19).

Thin layer over the frozen run, block and scoring modules (unchanged):
- cohort_stop_checks(): cohort-level stop rules from dispositions only (no outcome is read);
- control_check():      blinded false-attribution check of NC targets and decoys from reconstruction
                        answers alone (any answer for an identity that never acted is a false
                        attribution, Section 8.3), so no sealed truth is opened during execution;
- run_confirmatory():   require_freeze(), one session, blocks one at a time with the checks between;
- score_cohort():       after every block has closed: unseal, score, lock, export analysis input;
- calibration_schedule(): the 16-run CAL- series (Section 13), run only after scores are locked.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ..reconstruct import reconstruct
from ..reconstruct.pipeline import run_window
from .block import BlockContext, blocks_from_schedule, read_progress, run_cohort

POSITIVE = ("LC", "LD", "MC", "MD")
CAL_LEVELS = ("CAL1", "CAL2", "CAL3", "CAL4")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


# ---------------------------------------------------------------------- stop rules (Section 10.4)
def cohort_stop_checks(progress: list[dict], n_blocks: int, ceiling_fraction: float = 0.15,
                       max_cf_imbalance: int = 3) -> list[str]:
    """Cohort-level stop rules evaluated from dispositions only.

    Ceiling: (aborts + unscorable) / attempts of a cell over the cohort must stay <= 15 %; with B
    planned scored runs per cell, the cohort ends with B + x attempts, so the rule fires when
    x / (B + x) > 0.15. Collector failures: |C - D| <= 3.
    """
    runs = [p for p in progress if p.get("event") == "RUN"]
    out = []
    for cell in (*POSITIVE, "NC"):
        x = sum(1 for r in runs if r["cell"] == cell and r["disposition"] in ("PRE_ACTION_ABORT", "UNSCORABLE", "INVALID"))
        if x and x / (n_blocks + x) > ceiling_fraction:
            out.append(f"ceiling: {x} aborted/unscorable attempts of {cell} exceeds 15 % of {n_blocks + x}")
    cf = {a: sum(1 for r in runs if r["arm"] == a and (r.get("primary_failure_code") or "").startswith("CF-"))
          for a in ("C", "D")}
    if abs(cf["C"] - cf["D"]) > max_cf_imbalance:
        out.append(f"collector-failure imbalance C={cf['C']} D={cf['D']} exceeds {max_cf_imbalance}")
    return out


# ---------------------------------------------------------------------- blinded control check
def control_check(pkg: Path, contract: dict, epsilon_s: float, grace_s: float) -> dict:
    """Blinded reconstruction of a closed package's NC target and decoy identities (no truth opened)."""
    pkg = Path(pkg)
    ident = json.loads((pkg / "identity.json").read_text())
    ids = []
    if ident.get("is_control"):
        ids.append({"role": "control_target", "namespace": ident["namespace"], "pod": ident["pod"]})
    if ident.get("decoy_identity"):
        ids.append({"role": "decoy", **{k: ident["decoy_identity"][k] for k in ("namespace", "pod")}})
    if not ids:
        return {"run_id": pkg.name, "checked": 0, "false_attribution": False, "answers": {}}
    with tempfile.TemporaryDirectory() as td:
        st = Path(td)
        for src in ("audit", "tetragon"):
            (st / "acquired_raw" / src).mkdir(parents=True)
            for p in sorted((pkg / "acquired_raw" / src).glob("*.raw")):
                shutil.copyfile(p, st / "acquired_raw" / src / p.name)
        q = {"window": run_window(pkg, epsilon_s, grace_s),
             "identities": [{"role": i["role"], "namespace": i["namespace"], "pod": i["pod"]} for i in ids]}
        (st / "questions.json").write_text(json.dumps(q))
        fa = reconstruct(st, contract)
    counts = {}
    for i in fa["identities"]:
        n = sum(len(v) for f in i["facts"].values() for v in f.values())
        counts[f"{i['role']}:{i['namespace']}/{i['pod']}"] = n
    return {"run_id": pkg.name, "checked": len(ids), "answers": counts,
            "false_attribution": any(n > 0 for n in counts.values()), "checked_at": _now()}


# ---------------------------------------------------------------------- confirmatory execution
def run_confirmatory(schedule_rows: list[dict], ctx: BlockContext, contract: dict, params: dict,
                     control_log: Path, max_blocks: int | None = None) -> dict:
    """Run the next blocks of the frozen schedule, one block per call to the frozen block runner,
    with the cohort stop rules and the blinded control check between blocks."""
    n_blocks = len(blocks_from_schedule(schedule_rows))
    t = params["timing"]
    done, out = 0, []
    while max_blocks is None or done < max_blocks:
        before = len(ctx.runs)
        res = run_cohort(schedule_rows, ctx, max_blocks=1)
        if not res["blocks"]:
            return {"blocks": out, "stopped": res.get("stopped"), "remaining_blocks": []}
        out += res["blocks"]
        done += 1
        if res.get("stopped"):
            return {"blocks": out, "stopped": res["stopped"]}
        fa_runs = []
        for r in ctx.runs[before:]:
            if r["disposition"] != "SCORED":
                continue
            chk = control_check(Path(r["package"]), contract, t["epsilon_s"], t["grace_s"])
            chk["block"] = r["block"]
            _append(control_log, chk)
            if chk["false_attribution"]:
                fa_runs.append(r["run_id"])
        if fa_runs:
            return {"blocks": out, "stopped": f"stop and audit: false attribution in NC/decoy of {fa_runs}"}
        stops = cohort_stop_checks(read_progress(ctx.progress), n_blocks)
        if stops:
            return {"blocks": out, "stopped": "stop and audit: " + "; ".join(stops)}
    remaining = [b for b in blocks_from_schedule(schedule_rows)
                 if b not in {p["block"] for p in read_progress(ctx.progress) if p.get("event") == "BLOCK_OPEN"}]
    return {"blocks": out, "stopped": None, "remaining_blocks": remaining}


# ---------------------------------------------------------------------- scoring, lock, export
def score_cohort(progress_path: Path, schedule_rows: list[dict], scoring_root: Path, contract: dict,
                 params: dict, analysis_dir: Path, lock_path: Path, allow_open_blocks: bool = False) -> dict:
    """Unseal and score every SCORED run after all blocks have closed; lock; export analysis input."""
    from ..reconstruct.pipeline import contract_opts, score_package
    prog = read_progress(progress_path)
    planned = list(blocks_from_schedule(schedule_rows))
    closed = {p["block"] for p in prog if p.get("event") in ("BLOCK_CLOSED", "BLOCK_INCOMPLETE")}
    open_blocks = [b for b in planned if b not in closed]
    if open_blocks and not allow_open_blocks:
        raise RuntimeError(f"blocks not closed yet: {open_blocks} (scores are unsealed only after execution)")
    if lock_path.exists():
        raise FileExistsError("scores are already locked")
    opts = contract_opts(contract)
    t = params["timing"]
    runs = [p for p in prog if p.get("event") == "RUN" and p["disposition"] == "SCORED"]
    for r in runs:
        if not (scoring_root / r["run_id"]).exists():
            score_package(Path(r["package"]), scoring_root, contract, [opts], opts,
                          epsilon_s=t["epsilon_s"], grace_s=t["grace_s"],
                          f5_tolerance_s=contract["facts"]["F5"]["tolerance_s"])
    complete = {p["block"] for p in prog if p.get("event") == "BLOCK_CLOSED" and p.get("complete")}
    rows, lock = [], {}
    for r in runs:
        sc_path = scoring_root / r["run_id"] / "assessment" / "scores.json"
        lock[r["run_id"]] = hashlib.sha256(sc_path.read_bytes()).hexdigest()
        if r["is_control"]:
            continue
        prim = json.loads(sc_path.read_text())["primary"]
        cf = (r.get("primary_failure_code") or "").startswith("CF-")
        st = json.loads((Path(r["package"]) / "run_status.json").read_text())
        rows.append({"run_id": r["run_id"], "block": r["block"], "cell": r["cell"], "arm": r["arm"], "load": r["load"],
                     "Y": 0 if cf else int(prim.get("Y") or 0),           # Section 10.1: collector failure -> Y = 0
                     "collector_failure": int(cf), "block_complete": int(r["block"] in complete),
                     "dv_delay": int("DV-DELAY" in (st.get("secondary_failure_codes") or [])),
                     "dv_drop": int("DV-DROP" in (st.get("secondary_failure_codes") or [])),
                     "delay_observed_s": st.get("delay_observed_s"),
                     "E_F1": prim["E"]["F1"], "E_F2": prim["E"]["F2"], "E_F3": prim["E"]["F3"]})
    analysis_dir.mkdir(parents=True, exist_ok=True)
    out_csv = analysis_dir / "scores.csv"
    with open(out_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["run_id", "block", "cell", "Y"])
        w.writeheader()
        w.writerows(rows)
    rec = {"locked_at": _now(), "n_scored": len(runs), "open_blocks_at_lock": open_blocks,
           "scores_sha256": lock, "analysis_input": str(out_csv),
           "analysis_input_sha256": hashlib.sha256(out_csv.read_bytes()).hexdigest()}
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "x") as fh:
        fh.write(json.dumps(rec, indent=2) + "\n")
    return rec


# ---------------------------------------------------------------------- calibration series (Section 13)
def calibration_schedule(master_seed: int, runs_per_level: int = 4) -> list[dict]:
    """16 D runs, 4 per calibration level, seeded order (seed = master_seed + 13), one block."""
    rng = np.random.Generator(np.random.PCG64(master_seed + 13))
    levels = [lvl for lvl in CAL_LEVELS for _ in range(runs_per_level)]
    order = rng.permutation(len(levels))
    return [{"block": "CAL-BLOCK-01", "slot": i + 1, "cell": "CAL", "load": levels[j], "arm": "D", "is_control": 0}
            for i, j in enumerate(order)]
