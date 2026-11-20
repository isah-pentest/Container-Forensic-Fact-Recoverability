"""Series F analysis after the score lock: registered tests F-T1, F-T2, F-T3, F-C1, descriptive F-D1,
F-D2 and the contrast F-C2. Metadata and locked scores only; the frozen mechanism helpers are reused.

Under a burst the write rate is not constant, so W is measured directly as the bytes written to the
store between the growth-monitor samples that bracket the record and the read, and the record's
offset o is interpolated with the pre-termination rate. The frozen series-R estimator (one mean rate over
[action - 15 s, read] multiplied by the age) is reported next to it as F-C2.

Outputs go to <evidence>/analysis_outputs/flooding/<commit>/ (written once per commit).
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from ..analysis.inputs import Cohort, journal, read_jsonl
from ..analysis.mechanism import deleted_before, growth_rows, record_position, record_times, store_rate
from ..analysis.report import git_state
from ..freeze import REPO_ROOT
from ..integrity import verify_hashes
from . import design
from .freeze import DIR
from .series import paths


def verify(evidence: Path) -> dict:
    P = paths(evidence)
    problems = []
    if not P["lock"].exists():
        return {"ok": False, "problems": ["series F scores are not locked"]}
    lock = json.loads(P["lock"].read_text())
    for run_id, h in lock["scores_sha256"].items():
        p = P["scoring"] / run_id / "assessment" / "scores.json"
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != h:
            problems.append(f"locked score changed or missing: {run_id}")
    coh = Cohort(design.COHORT, P["progress"], P["scoring"]).load()
    for r in coh.runs:
        problems += [f"{r['run_id']} package: {b}" for b in verify_hashes(Path(r["package"]))]
        sd = P["scoring"] / r["run_id"]
        if sd.exists():
            problems += [f"{r['run_id']} scoring: {b}" for b in verify_hashes(sd)]
    return {"ok": not problems, "problems": problems, "packages_checked": len(coh.runs),
            "locked_scores": len(lock["scores_sha256"])}


def _bracket_bytes(rows: list[dict], store: str, t0: int, t1: int) -> float:
    """Bytes written to `store` between the 1 Hz samples that bracket [t0, t1] (last sample at or before
    t0, first at or after t1). The bracket adds at most one sample period at each end, so W is an upper
    bound that exceeds the bytes written in [t0, t1] by at most one second of writes at each end."""
    ts = sorted({r["wall_ns"] for r in rows if r["store"] == store})
    a = max((t for t in ts if t <= t0), default=ts[0] if ts else t0)
    b = min((t for t in ts if t >= t1), default=ts[-1] if ts else t1)
    return store_rate(rows, store, a, b) * (b - a) / 1e9


def pair_rows(coh: Cohort, m: int, k: int) -> list[dict]:
    """One row per (positive delayed run, fact, source) with the burst-aware and the frozen-estimator H."""
    out = []
    for r in coh.scored():
        if r["arm"] != "D":
            continue
        pkg = Path(r["package"])
        j = journal(pkg)
        t_read, t_act, t_term = j["ACQ_START_D"]["wall_ns"], j["ACTION_REQUEST"]["wall_ns"], j["TERMINATION_CONFIRMED"]["wall_ns"]
        rows = growth_rows(pkg)
        times = record_times(pkg)
        tgt = next(i for i in coh.scores(r["run_id"])["primary"]["identities"] if i["role"] == "target")
        for fact, src in design.PAIRS:
            t_rec = times[fact]
            age = (t_read - t_rec) / 1e9
            r_pre = store_rate(rows, src, t_act - 15 * 10**9, t_term)
            pos = record_position(rows, src, t_rec, r_pre, m)
            w = _bracket_bytes(rows, src, t_rec, t_read)
            h = ((m - pos["offset"]) + k * m) / w if (pos["offset"] is not None and w > 0) else None
            r_frz = store_rate(rows, src, t_act - 15 * 10**9, t_read)
            pos_f = record_position(rows, src, t_rec, r_frz, m)
            w_f = r_frz * age
            h_f = ((m - pos_f["offset"]) + k * m) / w_f if (pos_f["offset"] is not None and w_f > 0) else None
            state = tgt["facts"][fact]["states"][src]
            out.append({"run_id": r["run_id"], "level": r["load"], "fact": fact, "source": src, "age_s": age,
                        "pre_rate_bytes_per_s": r_pre, "offset_bytes": pos["offset"], "written_bytes": w, "H": h,
                        "H_frozen_estimator": h_f,
                        "file_deleted_before_read": deleted_before(rows, src, pos["inode"], t_rec, t_read),
                        "state": state, "observed_lost": state != "CORRECT"})
    return out


def flood_rows(coh: Cohort, controller_dir: Path, window_s: float) -> list[dict]:
    """F-D1 and F-D2 per scored run (positive and control)."""
    out = []
    for r in coh.scored(positive_only=False):
        pkg = Path(r["package"])
        j = journal(pkg)
        if "TERMINATION_CONFIRMED" not in j:
            continue
        t0 = j["TERMINATION_CONFIRMED"]["wall_ns"]
        t1 = t0 + int(window_s * 1e9)
        rows = growth_rows(pkg)
        d1 = {}
        for src in ("audit", "tetragon"):
            sel = [x for x in rows if x["store"] == src and t0 <= x["wall_ns"] <= t1]
            rate = store_rate(rows, src, t0, t1)
            span = (max(x["wall_ns"] for x in sel) - min(x["wall_ns"] for x in sel)) / 1e9 if sel else 0.0
            d1[src] = {"bytes_per_s": rate, "bytes_in_samples": rate * span, "sampled_s": span,
                       "distinct_files": len({x["inode"] for x in sel})}
        c = {e["event"]: e for e in read_jsonl(Path(controller_dir) / f"{r['run_id']}.jsonl")} \
            if (Path(controller_dir) / f"{r['run_id']}.jsonl").exists() else {}
        out.append({"run_id": r["run_id"], "level": r["load"], "arm": r["arm"], "is_control": r["is_control"],
                    "stores": d1,
                    "detect_latency_s": (c.get("TERMINATION_SEEN") or {}).get("detect_latency_s"),
                    "onset_latency_s": (c.get("FLOOD_SET") or {}).get("onset_latency_s"),
                    "flood_s_controller": (c.get("RESTORED") or {}).get("flood_s_controller"),
                    "restore_reason": (c.get("RESTORED") or {}).get("reason"),
                    "controller_record": bool(c)})
    return out


def results(evidence: Path, plan: dict) -> tuple[dict, list[dict], list[dict]]:
    P = paths(evidence)
    coh = Cohort(design.COHORT, P["progress"], P["scoring"]).load()
    m, k = plan["M_mib"] * 1024 * 1024, plan["k"]
    hr = pair_rows(coh, m, k)
    fr = flood_rows(coh, P["controller"], plan["flood_window_s"])
    measured = [r for r in hr if r["H"] is not None]
    mis = [r for r in measured if (r["H"] < 1) != r["observed_lost"]]
    t1 = {"pairs_with_H": len(measured), "pairs_without_H": len(hr) - len(measured), "misclassified": len(mis),
          "misclassified_pairs": [{x: r[x] for x in ("run_id", "level", "fact", "source", "H", "state")} for r in mis],
          "supported": bool(measured) and not mis}
    low = [r for r in hr if r["level"] in ("F0", "F1")]
    t2 = {"pairs": len(low), "lost": sum(r["observed_lost"] for r in low),
          "supported": bool(low) and not any(r["observed_lost"] for r in low)}
    t3_runs = []
    for r in coh.scored():
        if r["arm"] != "C":
            continue
        fol = journal(Path(r["package"])).get("ACQ_END_C", {}).get("followers", {})
        t3_runs.append({"run_id": r["run_id"], "level": r["load"], "Y": coh.scores(r["run_id"])["primary"].get("Y"),
                        "gaps": {s: v.get("gaps") for s, v in fol.items()},
                        "rotations": {s: v.get("rotations") for s, v in fol.items()}})
    cf = [r for r in coh.runs if r["arm"] == "C" and (r.get("primary_failure_code") or "").startswith("CF-")]
    t3 = {"runs": t3_runs, "collector_failures": [r["run_id"] for r in cf],
          "supported": bool(t3_runs) and not cf and all(x["Y"] == 1 and not any(x["gaps"].values()) for x in t3_runs)}
    nc = [r for r in coh.runs if r["is_control"] and r["disposition"] == "SCORED"]
    c1_runs = [{"run_id": r["run_id"], "false_attribution": bool(coh.scores(r["run_id"])["primary"].get("false_attribution"))}
               for r in nc]
    c1 = {"runs": c1_runs, "supported": bool(c1_runs) and not any(x["false_attribution"] for x in c1_runs)}
    f0 = [r for r in hr if r["level"] == "F0"]
    g2 = {"pairs": len(f0), "lost": sum(r["observed_lost"] for r in f0),
          "pass": bool(f0) and not any(r["observed_lost"] for r in f0),
          "note": "if G2 fails, F-T1 to F-C1 are reported descriptively (Amendment F, Section 6)"}
    mf = [r for r in hr if r["H_frozen_estimator"] is not None]
    c2 = {"pairs": len(mf), "misclassified_frozen_estimator": sum((r["H_frozen_estimator"] < 1) != r["observed_lost"] for r in mf)}
    runs = [{"run_id": r["run_id"], "cell": r["cell"], "arm": r["arm"], "level": r["load"], "disposition": r["disposition"],
             "primary_failure_code": r.get("primary_failure_code"), "replaces_run_id": r.get("replaces_run_id")}
            for r in coh.runs]
    out = {"series": "F", "k": k, "plan_sha256": hashlib.sha256((REPO_ROOT / DIR / "plan.json").read_bytes()).hexdigest()
           if (REPO_ROOT / DIR / "plan.json").exists() else None,
           "runs": runs, "dispositions": {d: sum(r["disposition"] == d for r in runs) for d in {r["disposition"] for r in runs}},
           "G2": g2, "F_T1": t1, "F_T2": t2, "F_T3": t3, "F_C1": c1, "F_C2": c2,
           "F_D2": {"onset_latency_s": [r["onset_latency_s"] for r in fr if r["onset_latency_s"] is not None],
                    "runs_without_controller_record": [r["run_id"] for r in fr if not r["controller_record"]]}}
    return out, hr, fr


def run_all(evidence: Path, out: Path | None = None) -> dict:
    gs = git_state()
    if gs["dirty"]:
        raise RuntimeError("commit the analysis code first: outputs are tied to an analysis commit")
    v = verify(evidence)
    if not v["ok"]:
        raise RuntimeError(f"verification failed: {v['problems'][:5]}")
    plan = json.loads((REPO_ROOT / DIR / "plan.json").read_text())
    res, hr, fr = results(evidence, plan)
    out = out or Path(evidence).expanduser() / "analysis_outputs" / "flooding" / gs["short"]
    out.mkdir(parents=True, exist_ok=False)
    res.update(analysis_commit=gs["commit"], generated_at=datetime.now(timezone.utc).isoformat(), verify=v)
    (out / "results_F.json").write_text(json.dumps(res, indent=2, default=str) + "\n")
    with open(out / "pairs_F.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(hr[0].keys()) if hr else ["run_id"])
        w.writeheader()
        w.writerows(hr)
    (out / "flood_F.json").write_text(json.dumps(fr, indent=2, default=str) + "\n")
    return {"out": str(out), **{k: res[k] for k in ("dispositions", "G2", "F_T1", "F_T2", "F_C2")},
            "F_T3_supported": res["F_T3"]["supported"], "F_C1_supported": res["F_C1"]["supported"]}
