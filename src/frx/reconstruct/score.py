"""Scoring reconstruction answers against sealed truth (Blueprint v3.0, Sections 7.5, 8.3, 11).

Runs only after fact_answers.json is written and hashed. Per (identity, fact, source):
  CORRECT       some answer matches truth on every required field and none contradicts it;
  FALSE         some answer contradicts truth on a required field it reports;
  ABSTAIN       no answer, or only answers missing a required field;
  NOT_ELIGIBLE  the source is not eligible for the fact (never a 0, never in a denominator).
Union rule (Section 7.5) and Y (Section 5) follow. Controls and decoys: any CORRECT or FALSE
answer for an identity that never acted is a false attribution (M14).
"""
from __future__ import annotations

from ..metrics import PRIMARY_FACTS

ELIGIBLE = {"F1": ("audit",), "F2": ("audit", "tetragon"), "F3": ("audit",),
            "F4": ("tetragon",), "F6": ("audit",)}


def _state(checks: list[dict]) -> str:
    """checks: one dict per answer, field -> True (match) / False (contradiction) / None (missing)."""
    if not checks:
        return "ABSTAIN"
    if any(v is False for c in checks for v in c.values()):
        return "FALSE"
    if any(c and all(v is True for v in c.values()) for c in checks):
        return "CORRECT"
    return "ABSTAIN"


def _cmp(got, want):
    if got is None or got == "" or got == []:
        return None
    return got == want


def field_checks(fact: str, source: str, ans: dict, truth: dict, opts: dict) -> dict:
    if fact == "F1":
        return {"pod_name": _cmp(ans.get("pod_name"), truth["pod"]),
                "pod_uid": _cmp(ans.get("pod_uid"), truth["uid"])}
    if fact == "F2":
        args = ans.get("arguments")
        if source == "tetragon" and isinstance(args, str):
            args = args.split()
        return {"executable": _cmp(ans.get("executable"), truth["executable"]),
                "arguments": _cmp(args, truth["arguments"])}
    if fact == "F3":
        c = {"pod_name": _cmp(ans.get("pod_name"), truth["pod"])}
        if opts.get("f3_uid_required"):
            c["pod_uid"] = _cmp(ans.get("pod_uid"), truth["uid"])
        return c
    if fact == "F4":
        return {"exited": True if ans.get("exited") else None}
    if fact == "F6":
        return {"actor": _cmp(ans.get("actor"), truth["harness_identity"])}
    raise ValueError(fact)


def union(states: dict) -> str:
    vals = [s for s in states.values() if s != "NOT_ELIGIBLE"]
    if any(s == "FALSE" for s in vals):
        return "CONFLICT"
    if any(s == "CORRECT" for s in vals):
        return "CORRECT"
    return "NOT_RECOVERED"


def score_identity(facts: dict, truth: dict | None, opts: dict) -> dict:
    """Score one identity's answers. truth=None means the identity never acted (NC or decoy)."""
    out = {"facts": {}}
    answered_any = False
    for fact in ("F1", "F2", "F3", "F4"):
        states, detail = {}, {}
        for src in ("audit", "tetragon"):
            if src not in ELIGIBLE[fact]:
                states[src] = "NOT_ELIGIBLE"
                continue
            answers = (facts.get(fact) or {}).get(src) or []
            if fact == "F3" and opts.get("f3_actor_required"):
                # a match condition, not an answer field: only the harness's own delete record counts
                answers = [x for x in answers if x.get("actor_is_harness")]
            if truth is None:
                states[src] = "FALSE" if answers else "ABSTAIN"     # any answer = false attribution
                answered_any |= bool(answers)
                detail[src] = {"answers": len(answers)}
                continue
            checks = [field_checks(fact, src, a, truth, opts) for a in answers]
            states[src] = _state(checks)
            detail[src] = {"answers": len(answers), "checks": checks}
        out["facts"][fact] = {"states": states, "union": union(states), "detail": detail}
    if truth is None:
        out["false_attribution"] = answered_any
        return out
    # F6 from the audit exec answers' actor; F5 order from the earliest answer of each fact
    f2a = (facts.get("F2") or {}).get("audit") or []
    f6 = _state([field_checks("F6", "audit", a, truth, opts) for a in f2a])
    out["facts"]["F6"] = {"states": {"audit": f6, "tetragon": "NOT_ELIGIBLE"}, "union": union({"audit": f6})}
    out["facts"]["F5"] = order_check(facts, opts.get("f5_tolerance_s", 1.0))
    ok = [out["facts"][f]["union"] == "CORRECT" for f in PRIMARY_FACTS]
    out["E"] = {f: int(v) for f, v in zip(PRIMARY_FACTS, ok)}
    out["Y"] = int(all(ok))
    out["C"] = sum(ok) / len(PRIMARY_FACTS)
    return out


def order_check(facts: dict, tol_s: float) -> dict:
    def first(fact, src):
        a = (facts.get(fact) or {}).get(src) or []
        ts = [x["evidence"]["ts_ns"] for x in a if x.get("evidence", {}).get("ts_ns")]
        return min(ts) if ts else None
    t1 = first("F1", "audit")
    t2 = [t for t in (first("F2", "audit"), first("F2", "tetragon")) if t]
    t3 = first("F3", "audit")
    if t1 is None or not t2 or t3 is None:
        return {"state": "ABSTAIN"}
    tol = int(tol_s * 1e9)
    ok = all(t1 <= t + tol for t in t2) and all(t <= t3 + tol for t in t2)
    return {"state": "CORRECT" if ok else "FALSE", "create_ns": t1, "exec_ns": t2, "delete_ns": t3}


def score_run(fact_answers: dict, truth: dict | None, opts: dict, is_control: bool) -> dict:
    """truth: {pod, uid, executable, arguments, harness_identity} for a positive run; None for NC."""
    res = {"opts": opts, "identities": []}
    for ident in fact_answers["identities"]:
        acted = ident["role"] == "target" and not is_control
        s = score_identity(ident["facts"], truth if acted else None, opts)
        s.update(role=ident["role"], namespace=ident["namespace"], pod=ident["pod"])
        res["identities"].append(s)
    tgt = next((i for i in res["identities"] if i["role"] == "target"), None)
    res["false_attribution"] = any(i.get("false_attribution") for i in res["identities"])
    if tgt is not None and not is_control:
        res["Y"], res["C"], res["E"] = tgt["Y"], tgt["C"], tgt["E"]
    return res
