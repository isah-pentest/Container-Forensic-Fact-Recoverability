"""Series R analysis: the frozen mechanism functions (frx.analysis.mechanism) applied to the ROB-
packages with k = 2, the registered tests R-T1 to R-T4 and the k = 1 contrast R-C1.

Outputs go to <evidence>/analysis_outputs/robustness/<commit>/ (written once per commit); the cited
v3.0.1 output sets are not read or written.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from ..analysis import mechanism as mech
from ..analysis.inputs import Cohort
from ..analysis.report import git_state
from ..analysis.svg import ACCENT, ACCENT2, BAD, GRID, INK, MUTED, Axis, Canvas, x_axis, y_axis
from ..contract import CONTRACT_PATH, load_yaml
from ..freeze import REPO_ROOT
from ..integrity import verify_hashes
from ..stats import wilson
from . import design
from .freeze import DIR
from .series import frozen_params, paths

FS, FS_S = 13, 12


class _SeriesEvidence:
    """The attributes frx.analysis.mechanism reads, for series R only (k from the plan)."""

    def __init__(self, evidence: Path, plan: dict, k: int):
        P = paths(evidence)
        self.root = Path(evidence).expanduser()
        self.params = design.series_params(frozen_params(), plan)
        self.contract = load_yaml(CONTRACT_PATH)
        self.confirmatory = Cohort("none", P["root"] / "absent.jsonl", P["scoring"]).load()
        self.calibration = Cohort(design.COHORT, P["progress"], P["scoring"]).load()
        self._k = k
        self._m = plan.get("M_bytes") or plan["M_mib"] * 1024 * 1024

    def store_capacity(self) -> dict:
        return {s: {"M": self._m, "k": self._k} for s in ("audit", "tetragon")}


def verify(evidence: Path) -> dict:
    P = paths(evidence)
    problems = []
    if not P["lock"].exists():
        return {"ok": False, "problems": ["series R scores are not locked"]}
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
            "locked_scores": len(lock["scores_sha256"]), "lock_written": lock["locked_at"]}


def _tag(rows: list[dict], k: int) -> list[dict]:
    for r in rows:
        r["cohort"] = design.LABEL
        r["k_used"] = k
    return rows


def load_plan() -> dict:
    return json.loads((REPO_ROOT / DIR / "plan.json").read_text())


def results(evidence: Path, plan: dict | None = None) -> tuple:
    plan = plan or load_plan()
    ev2 = _SeriesEvidence(evidence, plan, plan["k"])
    ev1 = _SeriesEvidence(evidence, plan, 1)
    hr = _tag(mech.headroom_rows(ev2), plan["k"])
    hr_k1 = _tag(mech.headroom_rows(ev1), 1)
    e4 = mech.loss_stages(ev2)
    for it in e4["items"]:
        it["cohort"] = design.LABEL
    coh = ev2.calibration
    runs = [{"run_id": r["run_id"], "load": r["load"], "disposition": r["disposition"],
             "primary_failure_code": r.get("primary_failure_code"), "replaces_run_id": r.get("replaces_run_id")}
            for r in coh.runs]
    measured = [r for r in hr if r["H"] is not None]
    mis = [r for r in measured if (r["H"] < 1) != r["observed_lost"]]
    r1 = [r for r in hr if r["level"] == "R1"]
    t1 = {"pairs_with_H": len(measured), "pairs_without_H": len(hr) - len(measured), "misclassified": len(mis),
          "accuracy": (len(measured) - len(mis)) / len(measured) if measured else None,
          "misclassified_pairs": [{k: r[k] for k in ("run_id", "level", "fact", "source", "H", "state")} for r in mis],
          "supported": bool(measured) and not mis}
    t2 = {"pairs": len(r1), "lost": sum(r["observed_lost"] for r in r1), "supported": bool(r1) and not any(r["observed_lost"] for r in r1)}
    cal = mech.calibration_summary(hr)
    t3 = {}
    for lvl in design.LEVELS:
        for fact, src in design.PAIRS:
            rs = [r for r in hr if r["level"] == lvl and r["fact"] == fact and r["source"] == src]
            if rs:
                x = sum(r["observed_lost"] for r in rs)
                t3[f"{lvl}|{fact}-{src}"] = {"lost": x, "n": len(rs), "wilson95": wilson(x, len(rs)),
                                             "mean_p_formula": sum(r["p_loss_formula"] for r in rs) / len(rs),
                                             "predicted_p_plan": (plan["levels"][lvl]["predicted"].get(f"{fact}-{src}") or {}).get("p_loss_formula")}
    brier = sum((r["p_loss_formula"] - r["observed_lost"]) ** 2 for r in hr) / len(hr) if hr else None
    m1 = [r for r in hr_k1 if r["H"] is not None]
    c1 = {"accuracy_with_k1": (sum((r["H"] < 1) == r["observed_lost"] for r in m1) / len(m1)) if m1 else None,
          "pairs": len(m1)}
    out = {"series": "R", "k": plan["k"], "plan_sha256": hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
           if not (REPO_ROOT / DIR / "plan.json").exists() else hashlib.sha256((REPO_ROOT / DIR / "plan.json").read_bytes()).hexdigest(),
           "runs": runs, "dispositions": {d: sum(r["disposition"] == d for r in runs) for d in {r["disposition"] for r in runs}},
           "delay_observed_s": _delays(coh), "measured_rates": _rates(coh),
           "R_T1": t1, "R_T2": t2, "R_T3": {"cells": t3, "brier_all_pairs": brier, "summary": cal},
           "R_T4": {"counts": e4["M22_counts"], "non_correct_pairs": e4["non_correct_pairs"]}, "R_C1": c1}
    return out, hr, hr_k1, e4


def _delays(coh) -> dict:
    v = []
    for r in coh.scored():
        st = json.loads((Path(r["package"]) / "run_status.json").read_text())
        if st.get("delay_observed_s") is not None:
            v.append(st["delay_observed_s"])
    return {"n": len(v), "min": min(v, default=None), "max": max(v, default=None)}


def _rates(coh) -> dict:
    out: dict = {}
    for r in coh.scored():
        lc = json.loads((Path(r["package"]) / "load" / "load_check.json").read_text())
        for s, val in (lc.get("measured_bytes_per_s") or {}).items():
            out.setdefault(r["load"], {}).setdefault(s, []).append(val)
    return {lvl: {s: {"n": len(v), "mean": sum(v) / len(v), "min": min(v), "max": max(v)} for s, v in d.items()}
            for lvl, d in out.items()}


def _csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    b = io.StringIO()
    keys = list(dict.fromkeys(k for r in rows for k in r))
    w = csv.DictWriter(b, fieldnames=keys, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: (json.dumps(v) if isinstance(v, (list, dict)) else repr(v) if isinstance(v, float) else v)
                    for k, v in r.items()})
    return b.getvalue()


def table_tex(res: dict, plan: dict) -> str:
    """LaTeX table body: per level, target audit rate, predicted F1 headroom and P(loss), observed losses."""
    lines = [r"\begin{tabularx}{\textwidth}{lCCCCC}", r"\toprule",
             r"\textbf{Level} & \textbf{Audit rate (kB/s)} & \textbf{Predicted $H$, F1} & \textbf{Predicted $P$(loss), F1} & "
             r"\textbf{F1 lost, audit} & \textbf{F2 lost, Tetragon} \\", r"\midrule"]
    cells = res["R_T3"]["cells"]
    for lvl in design.LEVELS:
        d = plan["levels"][lvl]
        p = d["predicted"]["F1-audit"]
        a = cells.get(f"{lvl}|F1-audit", {})
        t = cells.get(f"{lvl}|F2-tetragon", {})
        lines.append(f"{lvl} & {d['target_bytes_per_s']['audit'] / 1000:.1f} & {p['H_expected_offset']:.2f} & "
                     f"{p['p_loss_formula']:.2f} & {a.get('lost', '--')}/{a.get('n', '--')} & {t.get('lost', '--')}/{t.get('n', '--')} \\\\")
    lines += [r"\bottomrule", r"\end{tabularx}"]
    return "\n".join(lines) + "\n"


def figure(hr: list[dict], res: dict) -> str:
    """(a) each series-R pair at its measured H (k = 2): lost vs retained, rule H = 1. (b) observed vs
    planned formula loss probability per level for audit F1 and Tetragon F2, Wilson 95% intervals."""
    c = Canvas(760, 318)
    c.text(8, 16, "(a)", FS, "start", weight="bold")
    rs = [r for r in hr if r["H"] is not None and r["H"] > 0]
    if rs:
        lo2 = math.floor(math.log2(min(r["H"] for r in rs)))
        hi2 = max(math.ceil(math.log2(max(r["H"] for r in rs))), lo2 + 2)
        ax = Axis(2.0 ** lo2, 2.0 ** hi2, 70, 360, log2=True)
        ylost, yret, band = 70, 200, 34
        if ax.lo < 1:
            c.rect(ax.p0, 30, ax(1.0) - ax.p0, 210, "#fbf5ec", "none")
        c.line(ax(1.0), 30, ax(1.0), 240, BAD, 1.4, "5 4")
        c.text(ax(1.0) + 4, 42, [("H", "it"), (" = 1", None)], FS_S, "start", BAD)
        ticks = [2.0 ** k for k in range(lo2, hi2 + 1)]
        x_axis(c, ax, 240, ticks, lambda t: (f"{t:g}" if t >= 1 else f"1/{int(round(1 / t))}"),
               [("Measured headroom ", None), ("H", "it"), (" (k = 2, log scale)", None)], size=FS_S, label_size=FS)
        c.text(62, ylost + 4, "lost", FS_S, "end")
        c.text(62, yret + 4, "retained", FS_S, "end")
        c.line(ax.p0, ylost + band + 8, ax.p1, ylost + band + 8, GRID, 0.8)
        cols = {"R1": "#7b8794", "R2": ACCENT, "R3": "#6b4fa0", "R4": ACCENT2}
        for r in rs:
            x = ax(min(max(r["H"], ax.lo), ax.hi))
            y = (ylost if r["observed_lost"] else yret) + mech_jitter(f"{r['run_id']}|{r['fact']}|{r['source']}") * band * 0.8
            col = cols.get(r["level"], INK)
            if r["source"] == "audit":
                c.circle(x, y, 3.6, "none", col, 1.2)
            else:
                c.polygon([(x, y - 4.5), (x - 4.2, y + 3.2), (x + 4.2, y + 3.2)], "none", col, 1.2)
        lt = [r for r in rs if r["H"] < 1]
        ge = [r for r in rs if r["H"] >= 1]
        c.text(360, 26, f"H < 1: {sum(r['observed_lost'] for r in lt)}/{len(lt)} lost;   "
                         f"H ≥ 1: {sum(r['observed_lost'] for r in ge)}/{len(ge)} lost", FS_S - 0.5, "end", MUTED)
        lx, ly = 70, 312
        for i, lvl in enumerate(design.LEVELS):
            c.rect(lx + i * 52, ly - 9, 10, 10, cols[lvl], "none")
            c.text(lx + i * 52 + 14, ly, lvl, FS_S - 1, "start", MUTED)
        c.circle(lx + 222, ly - 4, 3.6, "none", INK, 1.2)
        c.text(lx + 230, ly, "audit", FS_S - 1, "start", MUTED)
        c.polygon([(lx + 276, ly - 8.5), (lx + 271.8, ly - 0.8), (lx + 280.2, ly - 0.8)], "none", INK, 1.2)
        c.text(lx + 284, ly, "Tetragon", FS_S - 1, "start", MUTED)
    c.text(400, 16, "(b)", FS, "start", weight="bold")
    bx, by = Axis(0, 1, 455, 740), Axis(0, 1, 240, 30)
    x_axis(c, bx, 240, [0, 0.25, 0.5, 0.75, 1.0], "{:.2f}", "Planned loss probability", size=FS_S, label_size=FS)
    y_axis(c, by, 455, [0, 0.25, 0.5, 0.75, 1.0], lambda t: "" if t == 0 else f"{t:.2f}", "Observed loss proportion",
           grid_to=740, size=FS_S, label_size=FS, label_dx=46)
    c.line(bx(0), by(0), bx(1), by(1), MUTED, 1, "5 4")
    for key, v in res["R_T3"]["cells"].items():
        lvl, fs = key.split("|")
        if fs not in ("F1-audit", "F2-tetragon") or v.get("predicted_p_plan") is None:
            continue
        x, y = bx(v["predicted_p_plan"]), by(v["lost"] / v["n"])
        lo, hi = v["wilson95"]
        c.line(x, by(lo), x, by(hi), INK, 1)
        if fs.endswith("tetragon"):
            c.polygon([(x, y - 6.5), (x - 6, y + 4.5), (x + 6, y + 4.5)], "none", INK, 1.1)
        else:
            c.circle(x, y, 4.5, "none", INK, 1.1)
    c.circle(470, 308, 4.5, "none", INK, 1.1)
    c.text(480, 312, "audit, F1", FS_S - 1, "start", MUTED)
    c.polygon([(560, 302.5), (554.8, 311.2), (565.2, 311.2)], "none", INK, 1.1)
    c.text(572, 312, "Tetragon, F2", FS_S - 1, "start", MUTED)
    return c.render()


def mech_jitter(key: str) -> float:
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF * 2 - 1


def run_all(evidence: Path, out_root: Path | None = None, allow_dirty: bool = False, plan: dict | None = None) -> dict:
    gs = git_state()
    if gs["dirty"] and not allow_dirty:
        raise RuntimeError("commit the analysis code first: outputs are tied to an analysis commit")
    ver = verify(evidence)
    if not ver["ok"]:
        raise RuntimeError(f"series R integrity re-verification failed: {ver['problems'][:10]}")
    out = Path(out_root) if out_root else Path(evidence).expanduser() / "analysis_outputs" / "robustness" / gs["short"]
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"{out} exists; an output set is written once per commit")
    plan = plan or load_plan()
    res, hr, hr_k1, e4 = results(evidence, plan)
    res.update({"generated_at": datetime.now(timezone.utc).isoformat(), "analysis_commit": gs["commit"], "verify": ver})
    files = {"results_R.json": json.dumps(res, indent=2, default=str) + "\n", "pairs_R.csv": _csv(hr),
             "pairs_R_k1_contrast.csv": _csv(hr_k1), "loss_stages_R.csv": _csv(e4["items"]),
             "tables/latex/TR_robustness.tex": table_tex(res, plan), "figures/FR_robustness.svg": figure(hr, res)}
    out.mkdir(parents=True, exist_ok=True)
    sums = []
    for rel, text in sorted(files.items()):
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "x") as fh:
            fh.write(text)
        sums.append(f"{hashlib.sha256(text.encode()).hexdigest()}  {rel}\n")
    (out / "SHA256SUMS").write_text("".join(sums))
    return {"out": str(out), "commit": gs["short"], "verify_ok": ver["ok"],
            "R_T1": {k: res["R_T1"][k] for k in ("pairs_with_H", "misclassified", "accuracy", "supported")},
            "R_T2": res["R_T2"], "R_T4": res["R_T4"], "R_C1": res["R_C1"]}
