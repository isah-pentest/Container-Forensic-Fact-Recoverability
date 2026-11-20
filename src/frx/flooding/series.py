"""Series F: equivalence gate G1, plan, execution and scoring, through the frozen v3.0.1 functions.

Evidence layout (its own root, default ~/frx-evidence-F; the 31 October snapshot, the confirmatory,
calibration and series-R trees are never read or written):
  <evidence>/ledger/attempt_ledger.csv      series-F attempt ledger (CAL- IDs, unique within this root)
  <evidence>/flooding/qualification/        G1 load map and verdict, capacity run, store check
  <evidence>/flooding/runs/CAL-nnnn/        write-once packages
  <evidence>/flooding/controller/           flood-controller records, one JSONL per run
  <evidence>/flooding/sessions/             session records and progress log
  <evidence>/flooding/scoring/              blinded scores and scores_lock_F.json
  <replica>/flooding/runs/                  off-host replica (a share other than the v3.0.1/series-R share)
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .. import contract as contract_mod
from ..freeze import REPO_ROOT
from ..randomization import write_schedule
from . import design
from .freeze import DIR

G1_SETTINGS = ("L", "M", "CAL1", "CAL2", "CAL3", "CAL4")


def paths(evidence: Path) -> dict:
    e = Path(evidence).expanduser() / "flooding"
    return {"root": e, "runs": e / "runs", "sessions": e / "sessions", "scoring": e / "scoring",
            "controller": e / "controller", "qualification": e / "qualification",
            "progress": e / "sessions" / "flooding_progress.jsonl",
            "ledger": Path(evidence).expanduser() / "ledger" / "attempt_ledger.csv",
            "lock": e / "scoring" / "scores_lock_F.json", "analysis_input": e / "analysis_input"}


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def frozen_params() -> dict:
    return contract_mod.load_yaml(contract_mod.PARAMETERS_PATH)


def _setting(prm: dict, name: str) -> dict:
    ld = prm["load"]
    if name in ("L", "M"):
        rate = ld["rate_low_bytes_per_s" if name == "L" else "rate_moderate_bytes_per_s"]
        return {"exec_per_s": float(ld["per_replica_exec_per_s"][name]), "api_per_s": float(ld["per_replica_api_per_s"][name]),
                "target_bytes_per_s": {k: float(v) for k, v in rate.items()}}
    c = ld["calibration"][name]
    return {"exec_per_s": float(c["exec_per_s"]), "api_per_s": float(c["api_per_s"]),
            "target_bytes_per_s": {k: float(v) for k, v in c["target_bytes_per_s"].items()}}


# ---------------------------------------------------------------------- G1 (first counted step)
def g1_settings(prm: dict | None = None) -> list[dict]:
    prm = prm or frozen_params()
    return [{"name": n, **_setting(prm, n)} for n in G1_SETTINGS]


def g1_evaluate(load_map_json: Path, prm: dict | None = None) -> dict:
    """Each frozen setting, measured on this host with the frozen replica count, lies within the locked
    manipulation tolerance of its frozen target, for both stores."""
    prm = prm or frozen_params()
    tol = float(prm["load"]["manipulation_tolerance"])
    lm = json.loads(Path(load_map_json).read_text())
    out, ok, problems = [], True, []
    if int(lm["replicas"]) != int(prm["load"]["replicas"]):
        problems.append(f"replicas {lm['replicas']} != frozen {prm['load']['replicas']}")
    want = g1_settings(prm)
    if len(lm["points"]) != len(want):
        problems.append(f"{len(lm['points'])} measured points, {len(want)} frozen settings")
    for s, pt in zip(want, lm["points"]):
        if abs(pt["exec_per_s"] - s["exec_per_s"]) > 1e-9 or abs(pt["api_per_s"] - s["api_per_s"]) > 1e-9:
            problems.append(f"{s['name']}: measured knob {pt['exec_per_s']}/{pt['api_per_s']} is not the frozen knob")
        row = {"setting": s["name"], "knob": {"exec_per_s": s["exec_per_s"], "api_per_s": s["api_per_s"]}, "stores": {}}
        for st, tgt in s["target_bytes_per_s"].items():
            m = float(pt["bytes_per_s"].get(st, 0.0))
            ratio = m / tgt
            p = abs(ratio - 1.0) <= tol
            ok &= p
            row["stores"][st] = {"target": tgt, "measured": m, "ratio": round(ratio, 4), "pass": p}
        out.append(row)
    ok = ok and not problems
    return {"gate": "G1", "tolerance": tol, "load_map": str(load_map_json), "load_map_sha256": _sha(load_map_json),
            "settings": out, "problems": problems, "pass": ok,
            "rule": "every frozen setting (L, M, CAL1-CAL4) within +/- tolerance of its frozen target, both stores"}


# ---------------------------------------------------------------------- plan (after G1, before the freeze)
def write_plan(g1_json: Path, fingerprint_hash: str, capacity_report: Path, store_check_json: Path,
               repo: Path = REPO_ROOT) -> dict:
    d = Path(repo) / DIR
    plan_p, sched_p = d / "plan.json", d / "schedule.csv"
    if plan_p.exists() or sched_p.exists():
        raise FileExistsError("plan.json / schedule.csv exist (written once)")
    prm = frozen_params()
    g1 = json.loads(Path(g1_json).read_text())
    cap = json.loads(Path(capacity_report).read_text())
    sc = json.loads(Path(store_check_json).read_text())
    if not g1.get("pass"):
        raise ValueError("G1 failed: series F stops (Amendment F, Section 6)")
    if not cap.get("pass") or not sc.get("pass"):
        raise ValueError("the capacity run at the F3 (CAL4) setting and the k = 1 store check must both pass")
    lv = design.levels(prm)
    if lv["problems"]:
        raise ValueError(f"levels do not meet the rules: {lv['problems']}")
    seed = int(prm["design"]["master_seed"])
    plan = {"series": design.SERIES, "created_at": datetime.now(timezone.utc).isoformat(),
            "supplements_protocol": prm["protocol_version"], "k": design.K_BACKUPS,
            "M_mib": prm["stores"]["audit"]["max_size_mb"], "delay_s": prm["timing"]["delay_s"],
            "flood_window_s": design.FLOOD_WINDOW_S, "base_level": design.BASE_LEVEL,
            "replicas": lv["replicas"], "base": lv["base"], "levels": lv["levels"], "rules": design.RULES,
            "cells": [dict(zip(("cell", "arm", "level", "is_control", "runs"), c)) for c in design.CELLS],
            "fingerprint_stable_hash": fingerprint_hash,
            "inputs": {"g1": {"path": str(g1_json), "sha256": _sha(g1_json), "pass": g1["pass"]},
                       "capacity_report": {"path": str(capacity_report), "sha256": _sha(capacity_report),
                                           "pass": cap.get("pass")},
                       "store_check": {"path": str(store_check_json), "sha256": _sha(store_check_json),
                                       "pass": sc.get("pass")}},
            "schedule": {"seed": seed + design.SEED_OFFSET, "file": str(DIR / "schedule.csv")},
            "registered_tests": REGISTERED_TESTS}
    d.mkdir(parents=True, exist_ok=True)
    plan_p.write_text(json.dumps(plan, indent=2) + "\n")
    digest = write_schedule(design.schedule(seed), sched_p, seed + design.SEED_OFFSET)
    return {"plan": str(plan_p), "plan_sha256": _sha(plan_p), "schedule": str(sched_p), "schedule_sha256": digest,
            "levels": {k: {"flood": (v["flood"] or {}).get("source_setting"),
                           "W_over_k1M": {p: round(x["W_over_k1M"], 3) for p, x in v["predicted"].items()}}
                       for k, v in plan["levels"].items()}}


REGISTERED_TESTS = [
    {"id": "F-T1", "kind": "prospective, deterministic",
     "statement": "For every delayed-read record-source pair of the positive runs at F0-F3 (audit F1, F2, F3; "
                  "Tetragon F2), the record is lost (state not CORRECT) if and only if its measured headroom "
                  "H < 1, with H = ((M - o) + k M) / W, k = 1, o the offset of the record at write time and W the "
                  "bytes written to the store between the record and the read, both measured by the 1 Hz growth monitor.",
     "decision": "supported if every pair with a measured H is classified correctly; any misclassified pair is "
                 "reported with its run, level, fact, source and H."},
    {"id": "F-T2", "kind": "prospective, deterministic",
     "statement": "No primary record is lost at F0 (no flood) or F1 (W <= 0.8 k M under the frozen targets).",
     "decision": "supported if 0 of the F0 and F1 record-source pairs are lost."},
    {"id": "F-T3", "kind": "prospective, deterministic",
     "statement": "Every continuous-arm run with the F3 flood recovers every primary fact (Y = 1) with zero follower gaps.",
     "decision": "supported if all F3-C runs scored have Y = 1 and zero gaps; collector failures are reported."},
    {"id": "F-C1", "kind": "prospective, deterministic",
     "statement": "No negative-control run with the F3 flood and no action yields a false attribution.",
     "decision": "supported if no F3-NC run scored has false_attribution = true."},
    {"id": "F-D1", "kind": "descriptive",
     "statement": "Bytes written and files rotated per store during the flood window, per run (growth monitor metadata).",
     "decision": "reported; not a test."},
    {"id": "F-D2", "kind": "descriptive",
     "statement": "Flood-controller latency: TERMINATION_CONFIRMED to rate patch acknowledged, and flood duration.",
     "decision": "reported; not a test."},
    {"id": "F-C2", "kind": "contrast (descriptive)",
     "statement": "H computed with the frozen series-R estimator (one mean rate over [action - 15 s, read]) next to "
                  "the burst-aware H of F-T1, to show the effect of assuming a constant rate under a burst.",
     "decision": "accuracy reported; not a test."},
]


# ---------------------------------------------------------------------- execution (after the freeze)
def run_series(a) -> int:
    from ..cli import _json, _kind_platform, _read_schedule
    from ..fingerprint import collect
    from ..orchestrator.block import BlockContext, close_session, open_session, run_cohort, run_config
    from .controller import FloodController
    from .freeze import check
    r = check(REPO_ROOT)
    if not r["ok"]:
        print(_json({"refusing": "series F is not frozen and tagged as committed", **r}))
        return 2
    P = paths(a.evidence)
    plan = json.loads((REPO_ROOT / DIR / "plan.json").read_text())
    prm = design.series_params(frozen_params(), plan)
    con = contract_mod.load_yaml(contract_mod.CONTRACT_PATH)
    replica = Path(a.replica).expanduser()
    if not replica.is_dir():
        print(f"refusing: replica root {replica} is not a mounted directory")
        return 2
    plat = _kind_platform(a, prm, con)
    plat.code_commit()
    P["runs"].mkdir(parents=True, exist_ok=True)
    P["ledger"].parent.mkdir(parents=True, exist_ok=True)
    cfg = run_config(prm, P["runs"], P["ledger"])
    files = [contract_mod.CONTRACT_PATH, contract_mod.PARAMETERS_PATH, REPO_ROOT / DIR / "plan.json",
             REPO_ROOT / DIR / "schedule.csv", REPO_ROOT / DIR / "FREEZE_F.json"]
    sess = open_session(P["sessions"], plat, P["ledger"], collect(REPO_ROOT), plan["fingerprint_stable_hash"],
                        files, "series F (post-termination flood, k = 1)")
    ctx = BlockContext(prefix=design.PREFIX, cohort=design.COHORT, cfg=cfg, plat=plat, params=prm,
                       session_dir=sess["dir"], replica_runs=replica / "flooding" / "runs", progress=P["progress"])
    levels = {lvl: (d["flood"] and {"knob": d["flood"]["knob"]}) for lvl, d in plan["levels"].items()}
    ctl = FloodController(P["runs"], P["controller"], levels, {"knob": plan["base"]["knob"]}, plan["flood_window_s"])
    ctl.start()
    res = {"stopped": "not started"}
    try:
        res = run_cohort(_read_schedule(REPO_ROOT / DIR / "schedule.csv"), ctx)
    finally:
        ctl.stop()
        ctl.join(timeout=120)
        close_session(sess["dir"], plat, P["ledger"], [Path(x["package"]) for x in ctx.runs])
    print(_json({"session": sess["session"], "controller_errors": ctl.errors,
                 "controller_runs": len(ctl.handled), **res}))
    return 0 if not res.get("stopped") and not ctl.errors else 2


def score_series(evidence: Path) -> dict:
    """Blinded scoring of every scored series-F package with the frozen scorer, then the score lock."""
    from ..cli import _read_schedule
    from ..orchestrator.cohort import score_cohort
    P = paths(evidence)
    plan = json.loads((REPO_ROOT / DIR / "plan.json").read_text())
    prm = design.series_params(frozen_params(), plan)
    rows = _read_schedule(REPO_ROOT / DIR / "schedule.csv")
    rec = score_cohort(P["progress"], rows, P["scoring"], contract_mod.load_yaml(contract_mod.CONTRACT_PATH), prm,
                       P["analysis_input"], P["lock"])
    return {k: v for k, v in rec.items() if k != "scores_sha256"}
