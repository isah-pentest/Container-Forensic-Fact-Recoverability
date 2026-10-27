"""Mechanism analyses after the score lock (Blueprint Sections 4, 11 M16/M19/M20/M22/M26, 12.2 E3/E4, 13).

E3: for every delayed-read run (confirmatory LD/MD and the CAL- series) and every primary fact the
source can hold (audit: F1, F2, F3; Tetragon: F2), measure from sealed metadata only
    o  = offset of the record in its file at write time   (1 Hz growth monitor, interpolated),
    W  = bytes written to the store between write and read (growth monitor),
    H  = ((M - o) + k M) / W                               (Section 4.1),
and compare the prediction H < 1 and the uniform-offset formula P(loss) with the observed state.

E4: every non-CORRECT (run, fact, source) gets a loss stage by Appendix D, using the reference
observer, a record-key lookup in the acquired bytes, and the growth monitor.
"""
from __future__ import annotations

import csv
import json
import shutil
import tempfile
from pathlib import Path

from ..acquisition.store import ACTIVE_NAMES
from ..headroom import predicted_loss_probability
from ..monitors import growth_from_rows
from ..reconstruct import reconstruct
from ..reconstruct.pipeline import contract_opts, run_window
from ..reconstruct.score import score_identity
from ..stats import wilson
from .inputs import Evidence, journal, read_jsonl

FACT_SOURCES = (("F1", "audit"), ("F2", "audit"), ("F3", "audit"), ("F2", "tetragon"))
H_BINS = ((0.0, 0.5), (0.5, 0.8), (0.8, 1.0), (1.0, 1.2), (1.2, 2.0), (2.0, float("inf")))


# ---------------------------------------------------------------------- growth-monitor helpers
def growth_rows(pkg: Path) -> list[dict]:
    with open(Path(pkg) / "sealed" / "growth_monitor_1hz.csv") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for k in ("mono_ns", "wall_ns", "inode", "size"):
            r[k] = int(r[k])
    return rows


def _samples(rows: list[dict], store: str) -> list[tuple[int, list[dict]]]:
    by: dict = {}
    for r in rows:
        if r["store"] == store:
            by.setdefault(r["wall_ns"], []).append(r)
    return sorted(by.items())


def store_rate(rows: list[dict], store: str, t0: int, t1: int) -> float:
    """Bytes/s written to one store over [t0, t1] (wall ns), from inode lives (frx.monitors)."""
    sel = [r for r in rows if r["store"] == store and t0 <= r["wall_ns"] <= t1]
    g = growth_from_rows(sel).get(store)
    return g["bytes_per_s"] if g and g["duration_s"] > 0 else 0.0


def record_position(rows: list[dict], store: str, t_rec: int, rate: float, m_bytes: int) -> dict:
    """Inode and byte offset of a record written at t_rec into the active file of `store`."""
    active = ACTIVE_NAMES[store]
    samp = [(t, next((r for r in rs if r["name"] == active), None)) for t, rs in _samples(rows, store)]
    samp = [(t, r) for t, r in samp if r is not None]
    before = [(t, r) for t, r in samp if t <= t_rec]
    after = [(t, r) for t, r in samp if t > t_rec]
    if not before:
        return {"inode": None, "offset": None, "note": "no growth sample before the record"}
    t0, r0 = before[-1]
    dt = (t_rec - t0) / 1e9
    if after and after[0][1]["inode"] == r0["inode"]:
        t1, r1 = after[0]
        frac = (t_rec - t0) / max(1, t1 - t0)
        return {"inode": r0["inode"], "offset": min(m_bytes, r0["size"] + frac * (r1["size"] - r0["size"])),
                "rotated_between_samples": False}
    est = r0["size"] + rate * dt
    if est < m_bytes or not after:
        return {"inode": r0["inode"], "offset": min(m_bytes, est), "rotated_between_samples": bool(after)}
    # the active file rotated between the two samples and before the record: the record is in the new file
    t_rot = t0 + (m_bytes - r0["size"]) / max(rate, 1e-9) * 1e9
    return {"inode": after[0][1]["inode"], "offset": max(0.0, rate * (t_rec - t_rot) / 1e9),
            "rotated_between_samples": True}


def deleted_before(rows: list[dict], store: str, inode: int | None, t_rec: int, t_read: int) -> bool | None:
    """Whether the file life holding `inode` at t_rec ended before t_read (absent in two consecutive
    samples, or its size dropped, i.e. the number now belongs to a new file)."""
    if inode is None:
        return None
    last, misses = -1, 0
    for t, rs in _samples(rows, store):
        if t < t_rec or t > t_read:
            continue
        hit = next((r for r in rs if r["inode"] == inode), None)
        if hit is None:
            misses += 1
            if misses >= 2:
                return True
            continue
        misses = 0
        if hit["size"] < last:
            return True
        last = hit["size"]
    return misses >= 1 and t_read > t_rec   # absent at the final sample before the read


def record_times(pkg: Path) -> dict:
    """Wall-clock write time of each fact's record, from the sealed harness journal (Section 7.1)."""
    hj = read_jsonl(Path(pkg) / "sealed" / "truth" / "harness_journal.jsonl")
    ex = json.loads((Path(pkg) / "sealed" / "truth" / "exec_result.json").read_text())

    def wall(e, key):            # API steps carry sent/received; simulated steps carry wall_ns directly
        return (e.get(key) or {}).get("wall_ns", e.get("wall_ns"))
    create = next(e for e in hj if e.get("step") == "create")
    delete = next(e for e in hj if e.get("step") == "delete")
    exec_ns = (ex.get("sent") or {}).get("wall_ns") or journal(pkg)["EXEC_CONFIRMED"]["wall_ns"]
    return {"F1": wall(create, "received"), "F3": wall(delete, "received"), "F2": exec_ns}


# ---------------------------------------------------------------------- E3
def headroom_rows(ev: Evidence) -> list[dict]:
    cap = ev.store_capacity()
    out = []
    for coh in (ev.confirmatory, ev.calibration):
        for r in coh.scored():
            if r["arm"] != "D":
                continue
            pkg = Path(r["package"])
            j = journal(pkg)
            t_read = j["ACQ_START_D"]["wall_ns"]
            t_act = j["ACTION_REQUEST"]["wall_ns"]
            rows = growth_rows(pkg)
            times = record_times(pkg)
            tgt = next(i for i in coh.scores(r["run_id"])["primary"]["identities"] if i["role"] == "target")
            for fact, src in FACT_SOURCES:
                M, k = cap[src]["M"], cap[src]["k"]
                t_rec = times[fact]
                rate = store_rate(rows, src, t_act - 15 * 10**9, t_read)
                pos = record_position(rows, src, t_rec, rate, M)
                age = (t_read - t_rec) / 1e9
                w = rate * age
                h = (((M - pos["offset"]) + k * M) / w) if (pos["offset"] is not None and w > 0) else None
                state = tgt["facts"][fact]["states"][src]
                out.append({"run_id": r["run_id"], "cohort": coh.name, "cell": r["cell"], "level": r["load"],
                            "fact": fact, "source": src, "age_s": age, "rate_bytes_per_s": rate,
                            "offset_bytes": pos["offset"], "written_bytes": w, "H": h,
                            "predicted_lost_H_lt_1": (h < 1) if h is not None else None,
                            "p_loss_formula": predicted_loss_probability(rate, age, M, k),
                            "file_deleted_before_read": deleted_before(rows, src, pos["inode"], t_rec, t_read),
                            "state": state, "observed_lost": state != "CORRECT"})
    return out


def calibration_summary(hr: list[dict]) -> dict:
    out: dict = {"n": len(hr), "by_fact_source": {}}
    for fact, src in FACT_SOURCES:
        rs = [r for r in hr if r["fact"] == fact and r["source"] == src and r["H"] is not None]
        if not rs:
            continue
        bins = []
        for lo, hi in H_BINS:
            b = [r for r in rs if lo <= r["H"] < hi]
            if b:
                x = sum(r["observed_lost"] for r in b)
                bins.append({"H_bin": [lo, hi], "lost": x, "n": len(b), "wilson95": wilson(x, len(b)),
                             "mean_p_formula": sum(r["p_loss_formula"] for r in b) / len(b)})
        tp = sum(r["predicted_lost_H_lt_1"] and r["observed_lost"] for r in rs)
        tn = sum((not r["predicted_lost_H_lt_1"]) and (not r["observed_lost"]) for r in rs)
        fp = sum(r["predicted_lost_H_lt_1"] and not r["observed_lost"] for r in rs)
        fn = sum((not r["predicted_lost_H_lt_1"]) and r["observed_lost"] for r in rs)
        levels = {}
        for lvl in sorted({r["level"] for r in rs}):
            lr = [r for r in rs if r["level"] == lvl]
            x = sum(r["observed_lost"] for r in lr)
            levels[lvl] = {"lost": x, "n": len(lr), "wilson95": wilson(x, len(lr)),
                           "mean_p_formula": sum(r["p_loss_formula"] for r in lr) / len(lr),
                           "share_H_lt_1": sum(r["predicted_lost_H_lt_1"] for r in lr) / len(lr)}
        out["by_fact_source"][f"{fact}-{src}"] = {
            "n": len(rs), "bins": bins, "by_level": levels,
            "M26_brier_formula": sum((r["p_loss_formula"] - r["observed_lost"]) ** 2 for r in rs) / len(rs),
            "H_rule_confusion": {"tp": tp, "tn": tn, "fp": fp, "fn": fn},
            "H_rule_accuracy": (tp + tn) / len(rs)}
    return out


# ---------------------------------------------------------------------- E4
def _stage_and_score(pkg: Path, src_file: Path, source: str, contract: dict, truth: dict, window: dict,
                     ident: dict) -> tuple[dict, dict]:
    with tempfile.TemporaryDirectory() as td:
        st = Path(td)
        for s in ("audit", "tetragon"):
            (st / "acquired_raw" / s).mkdir(parents=True)
        if src_file.exists():
            shutil.copyfile(src_file, st / "acquired_raw" / source / "reference.raw")
        (st / "questions.json").write_text(json.dumps(
            {"window": window, "identities": [{"role": "target", "namespace": ident["namespace"], "pod": ident["pod"]}]}))
        fa = reconstruct(st, contract)
    tgt = fa["identities"][0]
    return tgt, score_identity(tgt["facts"], truth, contract_opts(contract))


def _keys_in_acquired(pkg: Path, source: str) -> set:
    keys = set()
    for p in sorted((Path(pkg) / "acquired_raw" / source).glob("*.raw")):
        with open(p, "rb") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if source == "audit" and ev.get("auditID"):
                    keys.add(ev["auditID"])
                for kind in ("process_exec", "process_exit"):
                    pid = ((ev.get(kind) or {}).get("process") or {}).get("exec_id")
                    if pid:
                        keys.add(pid)
    return keys


def loss_stages(ev: Evidence) -> dict:
    t = ev.params["timing"]
    ref_names = {"audit": "audit_webhook", "tetragon": "tetragon_grpc"}
    ref_files = {"audit": "audit_webhook.jsonl", "tetragon": "tetragon_grpc.jsonl"}
    items, counts = [], {}
    for coh in (ev.confirmatory, ev.calibration):
        for r in coh.scored():
            pkg = Path(r["package"])
            tgt = next(i for i in coh.scores(r["run_id"])["primary"]["identities"] if i["role"] == "target")
            todo = [(f, s) for f, s in FACT_SOURCES if tgt["facts"][f]["states"][s] != "CORRECT"]
            if not todo:
                continue
            ident = json.loads((pkg / "identity.json").read_text())
            truth_raw = json.loads((pkg / "sealed" / "truth" / "truth.json").read_text())
            truth = {k: truth_raw[k] for k in ("pod", "uid", "executable", "arguments", "harness_identity")}
            window = run_window(pkg, t["epsilon_s"], t["grace_s"])
            exits = json.loads((pkg / "system" / "collector_exits.json").read_text())
            healthy = {o.get("observer"): bool(o.get("healthy")) for o in exits.get("reference_observers") or []}
            j = journal(pkg)
            t_read = (j.get("ACQ_START_D") or j.get("ACQ_END_C"))["wall_ns"]
            rows = growth_rows(pkg)
            times = record_times(pkg)
            for fact, src in todo:
                rec = {"run_id": r["run_id"], "cohort": coh.name, "cell": r["cell"], "fact": fact, "source": src,
                       "acquired_state": tgt["facts"][fact]["states"][src]}
                if not healthy.get(ref_names[src]):
                    rec["stage"] = "UNRESOLVED"
                else:
                    ans, scored = _stage_and_score(pkg, pkg / "sealed" / "reference" / ref_files[src], src,
                                                   ev.contract, truth, window, ident)
                    if scored["facts"][fact]["states"][src] != "CORRECT":
                        rec["stage"] = "NOT_EMITTED"
                    else:
                        keys = {a["evidence"].get("auditID") or a["evidence"].get("exec_id")
                                for a in ans["facts"][fact][src]}
                        if keys & _keys_in_acquired(pkg, src):
                            rec["stage"] = "NOT_MATCHED"
                        else:
                            M = ev.store_capacity()[src]["M"]
                            rate = store_rate(rows, src, times[fact] - 15 * 10**9, t_read)
                            pos = record_position(rows, src, times[fact], rate, M)
                            gone = deleted_before(rows, src, pos["inode"], times[fact], t_read)
                            rec["stage"] = "EVICTED" if gone else ("NOT_ACQUIRED" if gone is False else "UNRESOLVED")
                            rec["record_keys"] = sorted(k for k in keys if k)
                counts[rec["stage"]] = counts.get(rec["stage"], 0) + 1
                items.append(rec)
    n = len(items)
    return {"M22_counts": counts, "non_correct_pairs": n,
            "proportions": {k: {"x": v, "n": n, "wilson95": wilson(v, n)} for k, v in counts.items()},
            "items": items}
