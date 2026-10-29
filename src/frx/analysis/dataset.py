"""Run-level dataset and data dictionary for independent assessment (G7 reporting).

run_level():    one row per attempt of the confirmatory cohort (positive and NC runs) and of the
                calibration series: block, slot, treatment assignment, disposition, measured write
                rates, observed acquisition delay, every fact-source state, the union states, Y, the
                control checks, and, for each delayed read, the retention headroom H, the formula loss
                probability and the loss stage of every primary fact-source pair.
DICTIONARY:     name, type, unit, allowed values, definition and recorded source of every column.
rebuild():      recomputes the published quantities from the run-level CSV alone (strings as read
                from the file), using the frozen statistics code for the confirmatory test, and
                compares them with results.json. Needs no evidence store:

    python -m frx.analysis rebuild-check <output-set>/data/run_level.csv <output-set>/results.json
"""
from __future__ import annotations

import csv
import io
import json
import math
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from . import confirmatory as cf
from .inputs import Evidence

SOURCES = ("audit", "tetragon")
FACTS_ALL = ("F1", "F2", "F3", "F4")
PAIRS = (("F1", "audit"), ("F2", "audit"), ("F2", "tetragon"), ("F3", "audit"))
STATE_VALUES = "CORRECT | FALSE | ABSTAIN | NOT_ELIGIBLE"
UNION_VALUES = "CORRECT | NOT_RECOVERED | CONFLICT"
STAGES = "EVICTED | NOT_ACQUIRED | NOT_MATCHED | NOT_EMITTED | UNRESOLVED"

# (name, type, unit, allowed values, definition, recorded source)
DICTIONARY: list[tuple[str, str, str, str, str, str]] = [
    ("run_id", "string", "", "RUN-nnnn | CAL-nnnn", "Attempt identifier, issued once per attempt.", "progress log (RUN event)"),
    ("cohort", "string", "", "confirmatory | calibration", "Series the attempt belongs to; only confirmatory positive runs enter H1/H2.", "progress log"),
    ("block", "string", "", "", "Randomized block (confirmatory) or calibration block.", "progress log"),
    ("slot", "integer", "", "1-5", "Position of the attempt in its block's randomized order.", "progress log / schedule"),
    ("cell", "string", "", "LC | LD | MC | MD | NC | calibration cell", "Assigned condition: load L/M with continuous (C) or delayed (D) acquisition; NC, negative control.", "schedule"),
    ("arm", "string", "", "C | D", "Assigned acquisition arm: continuous follower or one-shot read after the delay.", "schedule"),
    ("load", "string", "", "L | M | CAL1-CAL4", "Assigned background write-load level.", "schedule"),
    ("is_control", "integer", "", "0 | 1", "1 for a negative-control run (no action; scored for false attribution only).", "schedule"),
    ("disposition", "string", "", "SCORED | PRE_ACTION_ABORT | UNSCORABLE", "Outcome-blind disposition of the attempt.", "progress log"),
    ("primary_failure_code", "string", "", "", "Primary failure code of the attempt; empty if none. CF- codes are collector failures.", "run_status.json"),
    ("replaces_run_id", "string", "", "", "Attempt replaced by this one; empty if not a replacement.", "progress log"),
    ("collector_failure", "integer", "", "0 | 1", "1 if the assigned acquisition failed (counted as Y = 0).", "primary_failure_code"),
    ("target_rate_audit_Bps", "float", "bytes/s", "", "Target write rate of the audit store for the assigned load level.", "load/load_check.json"),
    ("target_rate_tetragon_Bps", "float", "bytes/s", "", "Target write rate of the Tetragon store for the assigned load level.", "load/load_check.json"),
    ("measured_rate_audit_Bps", "float", "bytes/s", "", "Audit store write rate measured in the pre-action manipulation window (1 Hz size samples).", "load/load_check.json"),
    ("measured_rate_tetragon_Bps", "float", "bytes/s", "", "Tetragon store write rate measured in the pre-action manipulation window.", "load/load_check.json"),
    ("rate_within_tolerance", "integer", "", "0 | 1", "1 if both measured rates were within the prespecified tolerance of target.", "load/load_check.json"),
    ("delay_observed_s", "float", "s", "", "Observed delay from confirmed termination to the start of the delayed read (D runs); empty for C runs.", "run_status.json"),
    ("secondary_codes", "JSON list", "", "", "Secondary deviation codes (e.g. DV-DELAY, DV-DROP) flagged for sensitivity analyses.", "run_status.json"),
] + [(f"{f}_{s}", "string", "", STATE_VALUES, f"Scored state of fact {f} from the {s} source (blind reconstruction vs sealed truth); empty if not scored or NC.", "scoring/<run>/assessment/scores.json")
     for f in FACTS_ALL for s in SOURCES] + [
    (f"{f}_union", "string", "", UNION_VALUES, f"Union-rule state of fact {f} over its eligible sources.", "scores.json") for f in FACTS_ALL] + [
    ("F5", "string", "", STATE_VALUES, "State of the secondary ordering fact F5 (create < exec < delete within tolerance).", "scores.json"),
    ("F6", "string", "", UNION_VALUES, "Union state of the secondary fact F6 (exec actor).", "scores.json"),
    ("Y", "integer", "", "0 | 1", "Primary outcome: 1 if F1, F2 and F3 were all recovered under the union rule; 0 for a collector failure; empty for NC.", "scores.json"),
    ("C", "float", "", "0-1", "Fact completeness: share of primary facts recovered; empty for NC.", "scores.json"),
    ("control_checks", "integer", "", "", "Identities checked for false attribution in this run (decoy identity; for NC also the never-used identity).", "scores.json"),
    ("false_attributions", "integer", "", "", "Checked identities for which any answer was returned although they never acted.", "scores.json"),
    ("rate_to_read_audit_Bps", "float", "bytes/s", "", "Mean audit write rate from 15 s before the action request to the delayed read (rate used for H); D runs only.", "sealed/growth_monitor_1hz.csv"),
    ("rate_to_read_tetragon_Bps", "float", "bytes/s", "", "Mean Tetragon write rate from 15 s before the action request to the delayed read; D runs only.", "sealed/growth_monitor_1hz.csv"),
] + [x for f, s in PAIRS for x in (
    (f"H_{f}_{s}", "float", "", "> 0", f"Measured retention headroom of the {f} record in the {s} store at the delayed read; D runs only.", "sealed growth monitor and harness journal"),
    (f"p_loss_{f}_{s}", "float", "", "0-1", f"Loss probability of the {f} {s} record from the uniform-offset formula; D runs only.", "rate, record age and store capacity"),
    (f"loss_stage_{f}_{s}", "string", "", STAGES, f"Lifecycle stage at which an unrecovered {f} {s} record was lost; empty if recovered.", "reference observer, acquired bytes and growth monitor"))]

COLUMNS = [d[0] for d in DICTIONARY]


def _b(v) -> int | None:
    return None if v is None else int(bool(v))


def run_level(ev: Evidence, e3_rows: list[dict], e4_items: list[dict]) -> list[dict]:
    pos = {r["run_id"]: r for coh in (ev.confirmatory, ev.calibration) for r in cf.run_table(ev, coh)}
    h = {(r["run_id"], r["fact"], r["source"]): r for r in e3_rows}
    stage = {(r["run_id"], r["fact"], r["source"]): r["stage"] for r in e4_items}
    out = []
    for coh in (ev.confirmatory, ev.calibration):
        for r in coh.runs:
            pkg = Path(r["package"])
            row = {c: None for c in COLUMNS}
            code = r.get("primary_failure_code") or ""
            row.update({"run_id": r["run_id"], "cohort": coh.name, "block": r["block"], "slot": r.get("slot"),
                        "cell": r["cell"], "arm": r["arm"], "load": r["load"], "is_control": _b(r["is_control"]),
                        "disposition": r["disposition"], "primary_failure_code": code,
                        "replaces_run_id": r.get("replaces_run_id") or "", "collector_failure": int(code.startswith("CF-"))})
            lc_p = pkg / "load" / "load_check.json"
            if lc_p.exists():
                lc = json.loads(lc_p.read_text())
                for s in SOURCES:
                    row[f"target_rate_{s}_Bps"] = (lc.get("target_bytes_per_s") or {}).get(s)
                    row[f"measured_rate_{s}_Bps"] = (lc.get("measured_bytes_per_s") or {}).get(s)
                if lc.get("within_tolerance"):
                    row["rate_within_tolerance"] = int(all(lc["within_tolerance"].values()))
            st_p = pkg / "run_status.json"
            if st_p.exists():
                st = json.loads(st_p.read_text())
                row["delay_observed_s"] = st.get("delay_observed_s")
                row["secondary_codes"] = st.get("secondary_failure_codes") or []
            if r["disposition"] == "SCORED":
                prim = coh.scores(r["run_id"])["primary"]
                ids = [i for i in prim["identities"] if i["role"] == "decoy" or r["is_control"]]
                row["control_checks"] = len(ids)
                row["false_attributions"] = sum(int(bool(i.get("false_attribution"))) for i in ids)
            p = pos.get(r["run_id"])
            if p:
                for k in [f"{f}_{s}" for f in FACTS_ALL for s in SOURCES] + [f"{f}_union" for f in FACTS_ALL] + ["F5", "F6", "Y", "C"]:
                    row[k] = p[k]
            for f, s in PAIRS:
                e = h.get((r["run_id"], f, s))
                if e:
                    row[f"rate_to_read_{s}_Bps"] = e["rate_bytes_per_s"]
                    row[f"H_{f}_{s}"] = e["H"]
                    row[f"p_loss_{f}_{s}"] = e["p_loss_formula"]
                row[f"loss_stage_{f}_{s}"] = stage.get((r["run_id"], f, s), "")
            out.append(row)
    return out


def _text(rows: list[dict], columns: list[str]) -> str:
    b = io.StringIO()
    w = csv.DictWriter(b, fieldnames=columns, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else json.dumps(r[k]) if isinstance(r[k], (list, dict))
                        else repr(r[k]) if isinstance(r[k], float) else r[k]) for k in columns})
    return b.getvalue()


def run_level_csv(rows: list[dict]) -> str:
    return _text(rows, COLUMNS)


def dictionary_csv() -> str:
    keys = ["column", "type", "unit", "allowed_values", "definition", "recorded_source"]
    return _text([dict(zip(keys, d)) for d in DICTIONARY], keys)


# ---------------------------------------------------------------------- rebuild from the CSV alone
def _f(v: str) -> float | None:
    return None if v == "" else float(v)


def _norm(x):
    return json.loads(json.dumps(x, default=str))


def _same(a, b) -> bool:
    a, b = _norm(a), _norm(b)
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
    return a == b


def rebuild(csv_text: str, results: dict) -> dict:
    """Recompute the published quantities from the run-level CSV and compare with results.json."""
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert rows and list(rows[0].keys()) == COLUMNS, "unexpected run-level columns"
    conf = [r for r in rows if r["cohort"] == "confirmatory"]
    scored = [r for r in conf if r["disposition"] == "SCORED"]
    pos = [r for r in scored if r["is_control"] == "0"]
    checks = []

    def check(name, mine, published):
        checks.append({"quantity": name, "ok": _same(mine, published), "from_csv": _norm(mine), "published": _norm(published)})

    # primary analysis: the frozen `frx analyze` command on the CSV's block, cell and Y
    from ..cli import main as frozen_cli
    prim = results["primary"]
    seed = int(results["params"]["design"]["master_seed"])
    draws = int(prim["H1"]["test"]["n_assignments"]) if prim["H1"]["test"]["method"] == "monte_carlo" else 100_000
    with tempfile.TemporaryDirectory() as td:
        sc = Path(td) / "scores.csv"
        sc.write_text(_text([{"run_id": r["run_id"], "block": r["block"], "cell": r["cell"], "Y": r["Y"]} for r in pos],
                            ["run_id", "block", "cell", "Y"]))
        with redirect_stdout(io.StringIO()):
            frozen_cli(["analyze", str(sc), "--seed", str(seed), "--draws", str(draws), "--out", str(Path(td) / "p.json")])
        mine = json.loads((Path(td) / "p.json").read_text())
    check("primary analysis (cells, deltas, H1, H2, interaction)", mine, {k: v for k, v in prim.items() if k != "_reproduction"})

    # every scored positive confirmatory run: states, union states, Y and C as in results.json
    keys = [f"{f}_{s}" for f in FACTS_ALL for s in SOURCES] + [f"{f}_union" for f in FACTS_ALL] + ["F5", "F6"]
    mine_runs = {r["run_id"]: {**{k: r[k] for k in keys}, "block": r["block"], "cell": r["cell"], "Y": int(r["Y"]),
                               "C": float(r["C"]), "delay_observed_s": _f(r["delay_observed_s"])} for r in pos}
    pub_runs = {r["run_id"]: {**{k: r[k] for k in keys}, "block": r["block"], "cell": r["cell"], "Y": r["Y"],
                              "C": r["C"], "delay_observed_s": r["delay_observed_s"]} for r in results["runs"]}
    check("per-run states, Y, C and delay of every scored positive run", mine_runs, pub_runs)

    # run accounting by cell (T3)
    acc = {}
    for r in conf:
        a = acc.setdefault(r["cell"], {"attempts": 0, "SCORED": 0, "PRE_ACTION_ABORT": 0, "UNSCORABLE": 0,
                                       "collector_failure": 0, "replacements": 0})
        a["attempts"] += 1
        a[r["disposition"]] = a.get(r["disposition"], 0) + 1
        a["collector_failure"] += int(r["collector_failure"])
        a["replacements"] += int(bool(r["replaces_run_id"]))
    check("run accounting by cell", acc, results["validity"]["M23_run_accounting"]["cell"])

    # per-fact union recovery by cell, F2 source pairing, false assertions and abstentions (Figure 5, T5)
    sec = results["secondary"]
    m7 = {c: {f: [sum(r[f"{f}_union"] == "CORRECT" and r["collector_failure"] == "0" for r in pos if r["cell"] == c),
                  sum(r["cell"] == c for r in pos)] for f in ("F1", "F2", "F3")} for c in ("LC", "LD", "MC", "MD")}
    check("per-fact union recovery by cell", m7, {c: {f: [v["x"], v["n"]] for f, v in d.items()} for c, d in sec["M7_per_fact_recovery"].items()})
    t = {"n11": 0, "n10": 0, "n01": 0, "n00": 0}
    for r in pos:
        t[f"n{int(r['F2_audit'] == 'CORRECT')}{int(r['F2_tetragon'] == 'CORRECT')}"] += 1
    pub = sec["E2_F2_sources"]["pooled"]
    check("F2 paired source table", t, {k: pub[k] for k in t})
    for s in SOURCES:
        el = [r[f"{f}_{s}"] for r in pos for f in ("F1", "F2", "F3") if r[f"{f}_{s}"] != "NOT_ELIGIBLE"]
        check(f"false assertions, {s}", [el.count("FALSE"), len(el)],
              [sec["M12_false_assertion_rate"][s]["x"], sec["M12_false_assertion_rate"][s]["n"]])
        check(f"abstentions, {s}", [el.count("ABSTAIN"), len(el)],
              [sec["M13_abstention_rate"][s]["x"], sec["M13_abstention_rate"][s]["n"]])
    check("F2 conflicts", sum(r["F2_union"] == "CONFLICT" for r in pos), sec["M11_conflict_rate"]["x"])

    # controls, delay and write rates (T5, T1)
    val = results["validity"]
    check("false attribution (x, n)", [sum(int(r["false_attributions"]) for r in scored), sum(int(r["control_checks"]) for r in scored)],
          [val["M14_false_attribution"]["x"], val["M14_false_attribution"]["n"]])
    for c in ("LD", "MD"):
        d = [float(r["delay_observed_s"]) for r in pos if r["cell"] == c and r["delay_observed_s"] != ""]
        check(f"observed delay {c} (min, max)", [min(d), max(d)], [val["M15_delay_observed_s"][c]["min"], val["M15_delay_observed_s"][c]["max"]])
    check("delayed runs outside tolerance", sum("DV-DELAY" in json.loads(r["secondary_codes"] or "[]") for r in pos), val["M15_delay_noncompliant"])
    check("runs with source drop events", sum("DV-DROP" in json.loads(r["secondary_codes"] or "[]") for r in pos), val["M21_drop_events"])
    rates = {}
    for r in scored:
        for s in SOURCES:
            if r[f"measured_rate_{s}_Bps"] != "":
                rates.setdefault(r["load"], {}).setdefault(s, []).append(float(r[f"measured_rate_{s}_Bps"]))
    check("measured write rate by level (quartiles)", {l: {s: cf._quart(v) for s, v in d.items()} for l, d in rates.items()},
          val["M19_write_rate_bytes_per_s"])

    # retention headroom and loss stage (T6, Figure 7)
    e3 = {}
    for f, s in PAIRS:
        rs = [r for r in rows if r["disposition"] == "SCORED" and r["is_control"] == "0" and r[f"H_{f}_{s}"] != ""]
        if not rs:
            continue
        lost = [r[f"{f}_{s}"] != "CORRECT" for r in rs]
        pred = [float(r[f"H_{f}_{s}"]) < 1 for r in rs]
        brier = sum((float(r[f"p_loss_{f}_{s}"]) - o) ** 2 for r, o in zip(rs, lost)) / len(rs)
        levels = {}
        for lvl in sorted({r["load"] for r in rs}):
            lr = [(r, o) for r, o in zip(rs, lost) if r["load"] == lvl]
            levels[lvl] = {"lost": sum(o for _, o in lr), "n": len(lr),
                           "mean_p_formula": sum(float(r[f"p_loss_{f}_{s}"]) for r, _ in lr) / len(lr)}
        e3[f"{f}-{s}"] = {"n": len(rs), "H_rule_accuracy": sum(p == o for p, o in zip(pred, lost)) / len(rs),
                          "M26_brier_formula": brier, "by_level": levels}
    check("headroom rule accuracy, Brier score and loss by write-rate level", e3,
          {k: {"n": v["n"], "H_rule_accuracy": v["H_rule_accuracy"], "M26_brier_formula": v["M26_brier_formula"],
               "by_level": {l: {x: d[x] for x in ("lost", "n", "mean_p_formula")} for l, d in v["by_level"].items()}}
           for k, v in results["e3"]["by_fact_source"].items()})
    stages = {}
    for r in rows:
        if r["disposition"] != "SCORED":
            continue
        for f, s in PAIRS:
            if r[f"loss_stage_{f}_{s}"]:
                stages[r[f"loss_stage_{f}_{s}"]] = stages.get(r[f"loss_stage_{f}_{s}"], 0) + 1
    check("loss-stage counts", stages, results["e4"]["M22_counts"])
    return {"ok": all(c["ok"] for c in checks), "rows": len(rows), "checks": checks,
            "failed": [c["quantity"] for c in checks if not c["ok"]]}
