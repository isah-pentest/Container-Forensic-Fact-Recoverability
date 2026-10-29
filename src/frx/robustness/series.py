"""Series R: plan, store check, execution and scoring, all through the frozen v3.0.1 functions.

Evidence layout (separate from the confirmatory and calibration trees, which are never written):
  <evidence>/ledger/attempt_ledger.csv               the shared append-only attempt ledger: series-R
                                                     attempts are appended (CAL- IDs numbered on from
                                                     the calibration series); existing rows never change
  <evidence>/robustness/runs/CAL-nnnn/               write-once packages
  <evidence>/robustness/sessions/                    session records and progress log
  <evidence>/robustness/scoring/                     blinded scores and scores_lock_R.json
  <replica>/robustness/runs/                         off-host replica
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .. import contract as contract_mod
from ..freeze import REPO_ROOT
from ..randomization import write_schedule
from . import design
from .freeze import DIR


def paths(evidence: Path) -> dict:
    e = Path(evidence).expanduser() / "robustness"
    return {"root": e, "runs": e / "runs", "sessions": e / "sessions", "scoring": e / "scoring",
            "progress": e / "sessions" / "robustness_progress.jsonl",
            "ledger": Path(evidence).expanduser() / "ledger" / "attempt_ledger.csv",
            "lock": e / "scoring" / "scores_lock_R.json", "analysis_input": e / "analysis_input"}


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def frozen_params() -> dict:
    return contract_mod.load_yaml(contract_mod.PARAMETERS_PATH)


# ---------------------------------------------------------------------- qualification (before the freeze)
def store_check(capacity_dir: Path, k: int = design.K_BACKUPS) -> dict:
    """From a capacity run's 1 Hz growth samples: the store never held more than k backups, held k at
    some point, and deleted files (so rotation with immediate deletion of surplus backups works at k)."""
    rows = list(csv.DictReader(open(Path(capacity_dir) / "growth_monitor_1hz.csv")))
    out, ok = {}, True
    for store in ("audit", "tetragon"):
        by: dict = {}
        for r in rows:
            if r["store"] == store:
                by.setdefault(r["wall_ns"], set()).add(r["inode"])
        counts = [len(v) for v in by.values()]
        inodes = set().union(*by.values()) if by else set()
        last = by[max(by)] if by else set()
        deleted = len(inodes - last)
        s = {"samples": len(by), "max_files_present": max(counts, default=0), "distinct_files": len(inodes),
             "files_deleted": deleted,
             "pass": bool(by) and max(counts) == k + 1 and deleted >= 1}
        ok &= s["pass"]
        out[store] = s
    return {"k": k, "capacity_run": str(capacity_dir), "stores": out, "pass": ok,
            "rule": f"max files present == k + 1 = {k + 1} (active + k backups) and at least one file deleted"}


def write_plan(load_map: Path, fingerprint_hash: str, capacity_report: Path, store_check_json: Path,
               repo: Path = REPO_ROOT) -> dict:
    d = Path(repo) / DIR
    plan_p, sched_p = d / "plan.json", d / "schedule.csv"
    if plan_p.exists() or sched_p.exists():
        raise FileExistsError("plan.json / schedule.csv exist (written once)")
    prm = frozen_params()
    lm = json.loads(Path(load_map).read_text())
    lv = design.levels(lm["fit"], prm)
    if lv["problems"]:
        raise ValueError(f"levels not feasible: {lv['problems']}")
    cap = json.loads(Path(capacity_report).read_text())
    sc = json.loads(Path(store_check_json).read_text())
    if not cap.get("pass") or not sc.get("pass"):
        raise ValueError("the capacity run at R4 and the k = 2 store check must both pass before the plan")
    seed = int(prm["design"]["master_seed"])
    plan = {"series": design.SERIES, "created_at": datetime.now(timezone.utc).isoformat(),
            "supplements_protocol": prm["protocol_version"], "k": design.K_BACKUPS,
            "M_mib": prm["stores"]["audit"]["max_size_mb"], "delay_s": prm["timing"]["delay_s"],
            "action_s_assumed": design.ACTION_S, "exec_offset_s_assumed": design.EXEC_OFFSET_S,
            "runs_per_level": design.RUNS_PER_LEVEL, "replicas": lv["replicas"], "ages_s": lv["ages_s"],
            "levels": lv["levels"], "expected_losses": None,
            "fingerprint_stable_hash": fingerprint_hash,
            "inputs": {"load_map": {"path": str(load_map), "sha256": _sha(load_map)},
                       "capacity_report": {"path": str(capacity_report), "sha256": _sha(capacity_report),
                                           "pass": cap.get("pass")},
                       "store_check": {"path": str(store_check_json), "sha256": _sha(store_check_json),
                                       "pass": sc.get("pass")}},
            "schedule": {"seed": seed + design.SEED_OFFSET, "file": str(DIR / "schedule.csv")},
            "registered_tests": REGISTERED_TESTS}
    plan["expected_losses"] = design.expected_losses(plan)
    d.mkdir(parents=True, exist_ok=True)
    plan_p.write_text(json.dumps(plan, indent=2) + "\n")
    digest = write_schedule(design.schedule(seed), sched_p, seed + design.SEED_OFFSET)
    return {"plan": str(plan_p), "plan_sha256": _sha(plan_p), "schedule": str(sched_p), "schedule_sha256": digest,
            "levels": {k: {"target": v["target_bytes_per_s"], "knob": v["knob"],
                           "H_F1_expected": round(v["predicted"]["F1-audit"]["H_expected_offset"], 3),
                           "p_loss_F1": round(v["predicted"]["F1-audit"]["p_loss_formula"], 3)}
                       for k, v in plan["levels"].items()}}


REGISTERED_TESTS = [
    {"id": "R-T1", "kind": "prospective, deterministic",
     "statement": "For every delayed-read record-source pair of series R (audit F1, F2, F3; Tetragon F2), the "
                  "record is lost (state not CORRECT) if and only if its measured headroom H < 1, with "
                  "B = (M - o) + k M and k = 2.",
     "decision": "supported if the rule classifies every pair with a measured H correctly; any misclassified "
                 "pair is reported as a failure of the rule, with its run, fact, source and H."},
    {"id": "R-T2", "kind": "prospective, deterministic",
     "statement": "At R1 (the confirmatory moderate rate) no primary record is lost: with k = 2 the model "
                  "predicts survival for every file offset (worst-case H > 1 at the assumed ages).",
     "decision": "supported if 0 of the R1 record-source pairs are lost."},
    {"id": "R-T3", "kind": "prospective, calibration (descriptive)",
     "statement": "Per level and fact-source, the observed loss proportion is compared with the mean formula "
                  "probability P(loss) = clip((r dt - k M) / M, 0, 1) with k = 2.",
     "decision": "reported with Wilson 95% intervals and the Brier score; no significance test."},
    {"id": "R-T4", "kind": "descriptive",
     "statement": "Every unrecovered record-source pair is assigned a loss stage by the frozen rules; the model "
                  "attributes loss to eviction.",
     "decision": "reported as counts per stage."},
    {"id": "R-C1", "kind": "contrast (descriptive)",
     "statement": "The same H < 1 rule evaluated with k = 1 instead of the configured k = 2 is reported next to "
                  "R-T1, to show the capacity term is what the rule depends on.",
     "decision": "accuracy with k = 1 reported; not a test."},
]


# ---------------------------------------------------------------------- execution (after freeze and registration)
def run_series(a) -> int:
    from ..cli import _json, _kind_platform, _read_schedule
    from ..fingerprint import collect
    from ..orchestrator.block import BlockContext, close_session, open_session, run_cohort, run_config
    from .freeze import check
    r = check(REPO_ROOT)
    if not r["ok"]:
        print(_json({"refusing": "series R is not frozen and registered as committed", **r}))
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
             REPO_ROOT / DIR / "schedule.csv", REPO_ROOT / DIR / "FREEZE_R.json", REPO_ROOT / DIR / "registration_R.json"]
    sess = open_session(P["sessions"], plat, P["ledger"], collect(REPO_ROOT), plan["fingerprint_stable_hash"],
                        files, "series R (k = 2)")
    ctx = BlockContext(prefix=design.PREFIX, cohort=design.COHORT, cfg=cfg, plat=plat, params=prm,
                       session_dir=sess["dir"], replica_runs=replica / "robustness" / "runs", progress=P["progress"])
    res = {"stopped": "not started"}
    try:
        res = run_cohort(_read_schedule(REPO_ROOT / DIR / "schedule.csv"), ctx)
    finally:
        close_session(sess["dir"], plat, P["ledger"], [Path(x["package"]) for x in ctx.runs])
    print(_json({"session": sess["session"], **res}))
    return 0 if not res.get("stopped") else 2


def score_series(evidence: Path) -> dict:
    """Blinded scoring of every scored ROB- package with the frozen scorer, then the score lock."""
    from ..cli import _read_schedule
    from ..orchestrator.cohort import score_cohort
    P = paths(evidence)
    plan = json.loads((REPO_ROOT / DIR / "plan.json").read_text())
    prm = design.series_params(frozen_params(), plan)
    rows = _read_schedule(REPO_ROOT / DIR / "schedule.csv")
    rec = score_cohort(P["progress"], rows, P["scoring"], contract_mod.load_yaml(contract_mod.CONTRACT_PATH), prm,
                       P["analysis_input"], P["lock"])
    return {k: v for k, v in rec.items() if k != "scores_sha256"}
