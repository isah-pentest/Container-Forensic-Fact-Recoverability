"""G2 and G3 evaluation from the qualification series (Blueprint v3.0, Sections 7.4, 18, 22.2).

Reads only closed packages, their scoring trees and the operating-points file. Writes nothing
into packages. Each criterion is printed with its numerator and denominator.
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

from scipy.stats import binom

from ..headroom import predicted_loss_probability
from ..monitors import growth_from_rows


def _journal(pkg: Path) -> dict:
    out = {}
    for line in open(pkg / "timing" / "event_journal.jsonl"):
        e = json.loads(line)
        out.setdefault(e["state"], e)
    return out


def _load(pkg: Path, rel: str):
    p = pkg / rel
    return json.loads(p.read_text()) if p.exists() else None


def _harness_steps(pkg: Path) -> list[dict]:
    p = pkg / "ground_truth" / "harness_journal.jsonl"
    return [json.loads(l) for l in open(p)] if p.exists() else []


def collect(progress_path: Path, runs_root: Path, scoring_root: Path) -> list[dict]:
    rows = []
    for rec in (json.loads(l) for l in open(progress_path)):
        pkg = runs_root / rec["run_id"]
        st = _load(pkg, "run_status.json") or {}
        sc = _load(scoring_root / rec["run_id"], "assessment/scores.json")
        fa = _load(scoring_root / rec["run_id"], "assessment/fact_answers.json")
        rows.append({**rec, "pkg": pkg, "status": st, "scores": sc, "answers": fa,
                     "journal": _journal(pkg), "identity": _load(pkg, "identity.json")})
    return rows


def _ratio(n, d):
    return {"n": n, "of": d, "text": f"{n}/{d}"}


def g2(rows: list[dict]) -> dict:
    qa = [r for r in rows if r["part"] == "Q-a" and r["disposition"] == "SCORED"]
    qab = [r for r in rows if r["part"] in ("Q-a", "Q-b")]
    reached = [r for r in qab if r["disposition"] != "PRE_ACTION_ABORT"]
    verified = [r for r in reached if r["disposition"] == "SCORED"]
    out = {"action_verified": _ratio(len(verified), len(reached)),
           "scored_slots": {"Q-a": len(qa), "Q-b": sum(1 for r in qab if r["part"] == "Q-b" and r["disposition"] == "SCORED")}}
    out["action_verified_pass"] = len(verified) == len(reached) and out["scored_slots"] == {"Q-a": 12, "Q-b": 12}

    def state(r, variant, fact, src):
        v = r["scores"]["variants"][variant] if variant else r["scores"]["primary"]
        tgt = next(i for i in v["identities"] if i["role"] == "target")
        return tgt["facts"][fact]["states"][src]

    per = {}
    for fact, src in (("F1", "audit"), ("F2", "audit"), ("F2", "tetragon"), ("F3", "audit")):
        n = sum(1 for r in qa if r["scores"] and state(r, None, fact, src) == "CORRECT")
        per[f"{fact}-{src}"] = _ratio(n, len(qa))
    out["fact_correct_Q_a"] = per
    out["audit_rule_7_4_1"] = all(per[k]["n"] >= 11 for k in ("F1-audit", "F2-audit", "F3-audit"))
    out["tetragon_rule_7_4_2"] = per["F2-tetragon"]["n"] >= 11

    def present(r, fact, field):
        tgt = next(i for i in r["answers"]["identities"] if i["role"] == "target")
        ans = tgt["facts"][fact]["audit"]
        return any(a.get(field) for a in ans)
    f3uid = sum(1 for r in qa if r["answers"] and present(r, "F3", "pod_uid"))
    f1uid = sum(1 for r in qa if r["answers"] and present(r, "F1", "pod_uid"))
    out["uid_present_Q_a"] = {"F1": _ratio(f1uid, len(qa)), "F3": _ratio(f3uid, len(qa))}
    out["rule_uid_required_F3"] = f3uid >= 11
    for key in sorted({k for r in qa if r["scores"] for k in r["scores"]["variants"]}):
        n = sum(1 for r in qa if r["scores"] and key in r["scores"]["variants"]
                and state(r, key, "F3", "audit") == "CORRECT")
        out.setdefault("F3_variants_Q_a", {})[key] = _ratio(n, len(qa))
    verbs, stages = {}, {}
    for r in qa:
        if not r["answers"]:
            continue
        tgt = next(i for i in r["answers"]["identities"] if i["role"] == "target")
        for a in tgt["facts"]["F2"]["audit"]:
            verbs[a["evidence"].get("verb")] = verbs.get(a["evidence"].get("verb"), 0) + 1
            stages[a["evidence"].get("stage")] = stages.get(a["evidence"].get("stage"), 0) + 1
    out["F2_audit_observed"] = {"verbs": verbs, "first_stage_per_auditID": stages}
    # Section 22.2 timeouts: 3 x the maximum observed in Q-a/Q-b, rounded up
    ready, term, nsdel = [], [], []
    for r in qab:
        j = r["journal"]
        if "CREATE_CONFIRMED" in j and j["CREATE_CONFIRMED"].get("create_to_ready_s") is not None:
            ready.append(j["CREATE_CONFIRMED"]["create_to_ready_s"])
        if "DELETE_REQUESTED" in j and "TERMINATION_CONFIRMED" in j:
            term.append((j["TERMINATION_CONFIRMED"].get("response_mono_ns", j["TERMINATION_CONFIRMED"]["mono_ns"])
                         - j["DELETE_REQUESTED"]["mono_ns"]) / 1e9)
        steps = _harness_steps(r["pkg"])
        d = [s for s in steps if s.get("step") == "namespace_delete"]
        c = [s for s in steps if s.get("step") == "namespace_check" and s.get("code") == 404]
        if d and c:
            nsdel.append((c[-1]["received"]["mono_ns"] - d[0]["sent"]["mono_ns"]) / 1e9)
    out["timeouts_rule"] = {
        "readiness_max_s": max(ready, default=None), "readiness_timeout_s": math.ceil(3 * max(ready)) if ready else None,
        "termination_max_s": max(term, default=None), "termination_timeout_s": math.ceil(3 * max(term)) if term else None,
        "namespace_delete_max_s": max(nsdel, default=None),
        "namespace_delete_timeout_s": math.ceil(3 * max(nsdel)) if nsdel else None}
    fa = [r for r in rows if r["scores"] and r["scores"]["primary"].get("false_attribution")]
    checks = sum(1 for r in rows if r["scores"] for i in r["scores"]["primary"]["identities"]
                 if i["role"] == "decoy" or r["identity"].get("is_control"))
    out["false_attribution"] = {"runs": [r["run_id"] for r in fa], "checks": checks, "pass": not fa}
    out["pass"] = bool(out["action_verified_pass"] and out["audit_rule_7_4_1"] and out["false_attribution"]["pass"])
    out["tetragon_route"] = "pod_metadata" if out["tetragon_rule_7_4_2"] else "decide per Section 7.4 rules 3-4"
    return out


def _rate_in_window(pkg: Path, t0_mono: int, t1_mono: int) -> dict:
    with open(pkg / "sealed" / "growth_monitor_1hz.csv") as fh:
        rows = [r for r in csv.DictReader(fh) if t0_mono <= int(r["mono_ns"]) <= t1_mono]
    g = growth_from_rows(rows)
    return {k: v["bytes_per_s"] for k, v in g.items()}


def g3(rows: list[dict], ops: dict, delay_tolerance_s: float = 2.0) -> dict:
    out = {}
    qb = [r for r in rows if r["part"] == "Q-b" and r["disposition"] == "SCORED"]
    def delay_ok(r):
        d = (_load(r["pkg"], "acquisition/acquirer_config.json") or {}).get("delay_s")
        obs = r["status"].get("delay_observed_s")
        return d is not None and obs is not None and abs(obs - d) <= delay_tolerance_s
    comp = [r for r in qb if r["status"]["flags"]["arm_compliant"] and delay_ok(r)
            and (r["pkg"] / "system" / "process_table_pre_action.txt").exists()]
    out["Q_b_compliant"] = _ratio(len(comp), len(qb))
    out["Q_b_delay_observed_s"] = sorted(round(r["status"]["delay_observed_s"], 3) for r in qb
                                         if r["status"].get("delay_observed_s") is not None)
    # Q-c: observed F1 loss against the formula
    qc = [r for r in rows if r["part"] == "Q-c" and r["disposition"] == "SCORED" and r["scores"]]
    st = ops["per_store"]["audit"]
    levels = {}
    for r in qc:
        lvl = r["status"]["load"]
        j = r["journal"]
        a, e = j["ACTION_REQUEST"]["mono_ns"], j.get("ACQ_START_D", {}).get("mono_ns")
        rate = _rate_in_window(r["pkg"], a - 15 * 10**9, e or a).get("audit", 0.0)
        age = (e - a) / 1e9 if e else None
        tgt = next(i for i in r["scores"]["primary"]["identities"] if i["role"] == "target")
        lost = tgt["facts"]["F1"]["states"]["audit"] != "CORRECT"
        p = predicted_loss_probability(rate, age, st["M_bytes"], st["k"]) if age else None
        levels.setdefault(lvl, []).append({"run_id": r["run_id"], "lost_F1": lost, "rate": rate, "age_s": age, "p_pred": p})
    verdict = {}
    for lvl, rs in sorted(levels.items()):
        n, k = len(rs), sum(x["lost_F1"] for x in rs)
        p = sum(x["p_pred"] for x in rs if x["p_pred"] is not None) / n
        lo, hi = binom.ppf(0.025, n, p), binom.ppf(0.975, n, p)
        verdict[lvl] = {"lost": _ratio(k, n), "p_pred_mean": round(p, 3), "band": [int(lo), int(hi)],
                        "within_band": bool(lo <= k <= hi), "runs": rs}
    order = sorted(verdict)                       # QC1 (H~2) .. QC3 (H~0.5)
    fr = [verdict[l]["lost"]["n"] / verdict[l]["lost"]["of"] for l in order]
    out["Q_c"] = verdict
    out["Q_c_direction_ok"] = all(a <= b for a, b in zip(fr, fr[1:]))
    out["Q_c_within_band"] = sum(v["within_band"] for v in verdict.values())
    out["Q_c_pass"] = out["Q_c_direction_ok"] and out["Q_c_within_band"] >= 2
    # reference observers and monitors drop-free in every Q run
    bad = []
    for r in rows:
        if r["part"] not in ("Q-a", "Q-b", "Q-c") or r["disposition"] != "SCORED":
            continue
        ce = _load(r["pkg"], "system/collector_exits.json") or {}
        refs = ce.get("reference_observers") or []
        if not refs or not all(o.get("healthy") for o in refs) or ce.get("growth_monitor_errors") \
                or any(ce.get("drop_counter_deltas", {}).values()):
            bad.append(r["run_id"])
    out["reference_drop_free"] = {"failing_runs": bad, "use_reference": not bad}
    out["pass"] = bool(out["Q_b_compliant"]["n"] == 12 and out["Q_b_compliant"]["of"] == 12 and out["Q_c_pass"])
    return out


def leak_check(rows: list[dict], scoring_root: Path) -> dict:
    bad = []
    for r in rows:
        p = scoring_root / r["run_id"] / "transforms" / "access_log.jsonl"
        if not p.exists():
            continue
        unsealed = False
        for e in (json.loads(l) for l in open(p)):
            if e["action"] == "unsealed":
                unsealed = True
            if e["action"] == "staged" and (unsealed or not e["path"].startswith("acquired_raw/")):
                bad.append({"run_id": r["run_id"], "entry": e})
    return {"violations": bad, "pass": not bad}


def report(progress_path: Path, runs_root: Path, scoring_root: Path, ops: dict) -> dict:
    rows = collect(progress_path, runs_root, scoring_root)
    out = {"runs": len(rows), "by_part": {}, "G2": g2(rows), "G3": g3(rows, ops),
           "leak_check": leak_check(rows, scoring_root)}
    out["G3"]["pass"] = out["G3"]["pass"] and out["leak_check"]["pass"]
    for r in rows:
        d = out["by_part"].setdefault(r["part"], {})
        d[r["disposition"]] = d.get(r["disposition"], 0) + 1
    return out
