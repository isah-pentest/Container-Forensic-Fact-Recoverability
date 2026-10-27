"""Blinded rule-based reconstruction (Blueprint v3.0, Sections 7.2-7.6, 8.3, 9).

Input is a staged directory and nothing else:
    <staged>/acquired_raw/{audit,tetragon}/<sha256>.raw   byte-exact acquired streams
    <staged>/questions.json   {"window": {...wall ns...}, "identities": [{"role", "namespace", "pod"}]}
plus the frozen contract. Never the nonce, UID, arm, load level or any sealed file.
One parser and one matcher serve every cell; records are selected only by the contract's
match rules (no keyword search of raw data). Output: candidate answers per identity, fact
and source, each with its evidence location. Scoring is a separate step (score.py).
"""
from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SOURCES = ("audit", "tetragon")


# ---------------------------------------------------------------------- contract
def effective_contract(contract: dict, fills: dict | None) -> dict:
    """Contract with <Gx> placeholders replaced from a fills tree (qualification candidates)."""
    out = copy.deepcopy(contract)

    def merge(dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = copy.deepcopy(v)
    if fills:
        merge(out, fills)
    return out


def _need(spec: dict, key: str):
    v = spec.get(key)
    if isinstance(v, str) and v.startswith("<G"):
        raise ValueError(f"contract value {key} is still a placeholder ({v})")
    return v


# ---------------------------------------------------------------------- parsing
def rfc3339_ns(s: str | None) -> int | None:
    """RFC 3339 timestamp with up to nanosecond fraction -> Unix ns (UTC)."""
    if not s or not isinstance(s, str):
        return None
    try:
        main, frac, tz = s, "", "+00:00"
        if s.endswith("Z"):
            main = s[:-1]
        elif len(s) > 6 and s[-6] in "+-" and s[-3] == ":":
            main, tz = s[:-6], s[-6:]
        if "." in main:
            main, frac = main.split(".", 1)
        dt = datetime.fromisoformat(main + tz)
        ns = int(dt.timestamp()) * 1_000_000_000
        return ns + int((frac + "000000000")[:9]) if frac else ns
    except ValueError:
        return None


def _get(d, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def iter_records(raw_dir: Path, warnings: list):
    """(file name, line number, object) for every JSON line of every raw file, in name order."""
    for p in sorted(Path(raw_dir).glob("*.raw")):
        with open(p, "rb") as fh:
            for i, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield p.name, i, json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    warnings.append({"file": p.name, "line": i, "error": type(e).__name__,
                                     "bytes": len(line)})


def _fill(v, ns: str, pod: str):
    return v.replace("{ns}", ns).replace("{pod}", pod) if isinstance(v, str) else v


def _matches(rec: dict, rule: dict, ns: str, pod: str) -> bool:
    for k, v in rule.items():
        want = _fill(v, ns, pod)
        got = _get(rec, k)
        if k == "responseStatus.code":
            if got is None or int(got) != int(want):
                return False
        elif got != want:
            return False
    return True


# ---------------------------------------------------------------------- matching
def _audit_answers(records, c: dict, ns: str, pod: str, harness_identity: str | None) -> dict:
    f1 = c["facts"]["F1"]["audit"]
    f2 = c["facts"]["F2"]["audit"]
    f3 = c["facts"]["F3"]["audit"]
    verbs = _need(f2, "verbs_accepted")
    stages = _need(f2, "stages_accepted")
    codes3 = [int(x) for x in f3.get("success_codes", [200, 202])]
    out = {"F1": {}, "F2": {}, "F3": {}}
    for fname, line, ev, ts in records:
        aid = ev.get("auditID")
        evid = {"file": fname, "line": line, "auditID": aid, "stage": ev.get("stage"), "verb": ev.get("verb"),
                "ts_ns": ts, "user": _get(ev, "user.username"), "code": _get(ev, "responseStatus.code")}
        if _matches(ev, f1["match"], ns, pod):
            out["F1"].setdefault(aid, {"pod_name": _get(ev, "objectRef.name"),
                                       "pod_uid": _get(ev, f1.get("uid_path", "responseObject.metadata.uid")),
                                       "evidence": evid})
        if _matches(ev, f2["match"], ns, pod) and ev.get("verb") in verbs and ev.get("stage") in stages:
            cmd = parse_qs(urlparse(ev.get("requestURI", "")).query).get("command") or []
            if aid not in out["F2"]:
                out["F2"][aid] = {"executable": cmd[0] if cmd else None, "arguments": cmd[1:],
                                  "actor": _get(ev, "user.username"), "evidence": evid}
        if _matches(ev, f3["match"], ns, pod) and (_get(ev, "responseStatus.code") or 0) in codes3:
            out["F3"].setdefault(aid, {"pod_name": _get(ev, "objectRef.name"),
                                       "pod_uid": _get(ev, "responseObject.metadata.uid"),
                                       "actor": _get(ev, "user.username"),
                                       "actor_is_harness": _get(ev, "user.username") == harness_identity,
                                       "evidence": evid})
    return {k: sorted(v.values(), key=lambda a: a["evidence"]["ts_ns"] or 0) for k, v in out.items()}


def _tetragon_answers(records, c: dict, ns: str, pod: str) -> dict:
    f2 = c["facts"]["F2"]["tetragon"]
    rule = {k: v for k, v in f2["match"].items()}
    for k, v in rule.items():
        if isinstance(v, str) and v.startswith("<G"):
            raise ValueError(f"F2.tetragon.match.{k} is still a placeholder")
    fallback = _need(f2, "fallback_attribution")
    f4 = c["facts"].get("F4", {}).get("tetragon") or {}
    out = {"F2": {}, "F4": {}}
    for fname, line, ev, ts in records:
        for key in ("process_exec", "process_exit"):
            body = ev.get(key)
            if not body:
                continue
            proc = body.get("process") or {}
            evid = {"file": fname, "line": line, "ts_ns": ts, "exec_id": proc.get("exec_id")}
            in_pod = _get(proc, "pod.namespace") == ns and _get(proc, "pod.name") == pod
            if key == "process_exec" and f2.get("event", "process_exec") == "process_exec":
                rec = {"process": proc}
                if _matches(rec, rule, ns, pod):
                    out["F2"].setdefault(proc.get("exec_id") or (fname, line), {
                        "executable": proc.get("binary"), "arguments": proc.get("arguments") or "",
                        "attribution": "pod_metadata", "evidence": evid})
                elif fallback not in (None, "none"):
                    raise NotImplementedError("container-ID fallback is declared only at G2 if needed")
            if key == "process_exit" and f4 and in_pod and proc.get("binary") == f4.get("binary") \
                    and (proc.get("arguments") or "").strip() == str(f4.get("arguments", "")):
                out["F4"].setdefault(proc.get("exec_id") or (fname, line),
                                     {"exited": True, "evidence": evid})
    return {k: sorted(v.values(), key=lambda a: a["evidence"]["ts_ns"] or 0) for k, v in out.items()}


def reconstruct(staged: Path, contract: dict) -> dict:
    """Answer the question list from the staged acquired bytes only."""
    staged = Path(staged)
    q = json.loads((staged / "questions.json").read_text())
    w0, w1 = int(q["window"]["start_wall_ns"]), int(q["window"]["end_wall_ns"])
    harness_identity = contract.get("harness_identity")
    warnings: list = []
    parsed = {}
    counts = {}
    for src, ts_key in (("audit", "stageTimestamp"), ("tetragon", "time")):
        recs, n_all, n_out = [], 0, 0
        for fname, line, ev in iter_records(staged / "acquired_raw" / src, warnings):
            n_all += 1
            ts = rfc3339_ns(ev.get(ts_key)) if isinstance(ev, dict) else None
            if ts is None or not (w0 <= ts <= w1):
                n_out += 1
                continue          # outside the run window: kept in acquired_raw, cannot match
            recs.append((fname, line, ev, ts))
        parsed[src] = recs
        counts[src] = {"records": n_all, "in_window": n_all - n_out}
    answers = []
    for ident in q["identities"]:
        ns, pod = ident["namespace"], ident["pod"]
        a = _audit_answers(parsed["audit"], contract, ns, pod, harness_identity)
        t = _tetragon_answers(parsed["tetragon"], contract, ns, pod)
        answers.append({"role": ident["role"], "namespace": ns, "pod": pod, "facts": {
            "F1": {"audit": a["F1"]},
            "F2": {"audit": a["F2"], "tetragon": t["F2"]},
            "F3": {"audit": a["F3"]},
            "F4": {"tetragon": t["F4"]},
        }})
    return {"window": q["window"], "record_counts": counts, "parse_warnings": len(warnings),
            "identities": answers, "_warnings": warnings}
