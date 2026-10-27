"""Figures F1-F7 (Blueprint Section 17.2), generated from the analysis outputs only."""
from __future__ import annotations

import math

from .svg import ACCENT, ACCENT2, BAD, GRID, INK, MUTED, Axis, Canvas, shade, x_axis, y_axis

CELLS = ("LC", "LD", "MC", "MD")
CELL_LABEL = {"LC": "Low load, continuous", "LD": "Low load, delayed", "MC": "Moderate load, continuous",
              "MD": "Moderate load, delayed"}


def f1_architecture() -> str:
    c = Canvas(900, 330)
    c.text(450, 26, "F1. Producers, source-side stores, acquisition boundary and blinded scoring", 14, "middle", weight="bold")
    boxes = [(20, 70, "kube-apiserver", "audit log backend"), (20, 190, "Tetragon agent", "JSON export")]
    for x, y, a, b in boxes:
        c.rect(x, y, 150, 60, "#eef3f9", ACCENT, rx=6)
        c.text(x + 75, y + 26, a, 12, "middle", weight="bold")
        c.text(x + 75, y + 44, b, 11, "middle", MUTED)
    for y, name in ((70, "S_A: audit.json + k backups"), (190, "S_T: tetragon.log + k backups")):
        c.rect(220, y, 190, 60, "white", INK, rx=6)
        c.text(315, y + 26, name, 11, "middle")
        c.text(315, y + 44, "size rotation, oldest deleted", 10, "middle", MUTED)
        c.arrow(170, y + 30, 220, y + 30)
    c.line(440, 56, 440, 300, BAD, 1.5, "6 4")
    c.text(440, 50, "acquisition boundary", 11, "middle", BAD)
    c.rect(470, 60, 170, 50, "white", ACCENT, rx=6)
    c.text(555, 82, "C: follower from offset", 11, "middle")
    c.text(555, 98, "until T + g", 10, "middle", MUTED)
    c.rect(470, 200, 170, 50, "white", ACCENT2, rx=6)
    c.text(555, 222, "D: one-shot copy", 11, "middle")
    c.text(555, 238, "at T + δ", 10, "middle", MUTED)
    for y in (100, 220):
        c.arrow(410, y, 470, y)
    c.rect(680, 130, 190, 60, "#eef3f9", INK, rx=6)
    c.text(775, 156, "acquired_raw → blinded", 11, "middle")
    c.text(775, 172, "matcher → scores (locked)", 11, "middle")
    c.arrow(640, 85, 680, 150)
    c.arrow(640, 225, 680, 175)
    c.rect(220, 255, 190, 40, "#fbf5ec", ACCENT2, rx=6)
    c.text(315, 279, "sealed: growth, reference, snapshot", 10, "middle")
    return c.render()


def f2_timeline(tl: dict) -> str:
    """tl: {run_id, cell, t0_ns, states: {name: ns}, epsilon_s, grace_s, delay_s, rotations: [ns], deletions: [ns]}"""
    c = Canvas(900, 260)
    c.text(450, 24, f"F2. Run timeline ({tl['run_id']}, {tl['cell']}): actions, windows, rotations", 14, "middle", weight="bold")
    t0 = tl["t0_ns"]
    rel = lambda ns: (ns - t0) / 1e9
    end = rel(tl["states"]["TERMINATION_CONFIRMED"]) + tl["delay_s"] + 8
    ax = Axis(-tl["epsilon_s"] - 3, end, 70, 860)
    x_axis(c, ax, 214, [t for t in range(0, int(end) + 1, 10)], "{:.0f}", "seconds after ACTION_REQUEST")
    term = rel(tl["states"]["TERMINATION_CONFIRMED"])
    c.rect(ax(-tl["epsilon_s"]), 60, ax(term + tl["grace_s"]) - ax(-tl["epsilon_s"]), 26, "#eef3f9", "none")
    c.text(ax(-tl["epsilon_s"]) + 4, 78, "run window [A − ε, T + g]", 10, "start", MUTED)
    for i, (name, lab) in enumerate((("CREATE_CONFIRMED", "create"), ("EXEC_CONFIRMED", "exec"),
                                      ("TERMINATION_CONFIRMED", "404 (T)"))):
        if name in tl["states"]:
            x = ax(rel(tl["states"][name]))
            c.line(x, 96, x, 150 + 13 * i, INK, 1.2)
            c.text(x + 3, 160 + 13 * i, lab, 10, "start")
    c.line(ax(term + tl["grace_s"]), 96, ax(term + tl["grace_s"]), 150, ACCENT, 2)
    c.text(ax(term + tl["grace_s"]), 194, "C ends (T + g)", 10, "middle", ACCENT)
    c.line(ax(term + tl["delay_s"]), 96, ax(term + tl["delay_s"]), 150, ACCENT2, 2)
    c.text(ax(term + tl["delay_s"]), 194, "D reads (T + δ)", 10, "end", ACCENT2)
    for ns in tl.get("rotations", []):
        x = ax(rel(ns))
        if ax.p0 <= x <= ax.p1:
            c.line(x, 120, x, 140, MUTED, 1)
    for ns in tl.get("deletions", []):          # wall_ns = last sample that still saw the file (1 Hz)
        x0, x1 = ax(rel(ns)), ax(rel(ns) + 1.0)
        if ax.p0 <= x0 <= ax.p1:
            c.rect(x0, 126, max(x1 - x0, 2), 8, BAD, "none")
    c.text(860, 44, "| audit rotation (first sample on new file)   ▬ backup deleted within this 1 s sample interval", 10, "end", MUTED)
    return c.render()


def f3_cells(primary: dict) -> str:
    c = Canvas(640, 360)
    c.text(320, 24, "F3. Complete-record recovery (Y = 1) by cell, Wilson 95% CI", 14, "middle", weight="bold")
    ay = Axis(0, 1, 300, 50)
    y_axis(c, ay, 80, [0, 0.25, 0.5, 0.75, 1.0], "{:.2f}", "P(Y = 1)", grid_to=610)
    for i, cell in enumerate(CELLS):
        v = primary["cells"][cell]
        x = 140 + i * 130
        lo, hi = v["wilson95"]
        c.line(x, ay(lo), x, ay(hi), INK, 1.5)
        for yy in (lo, hi):
            c.line(x - 6, ay(yy), x + 6, ay(yy), INK, 1.5)
        c.circle(x, ay(v["p"]), 6, BAD if cell == "MD" else ACCENT)
        c.text(x, 322, cell, 12, "middle", weight="bold")
        c.text(x, 338, f"{v['x']}/{v['n']}", 11, "middle", MUTED)
    return c.render()


def f4_deltas(primary: dict, mie: float = -0.25) -> str:
    c = Canvas(860, 260)
    c.text(430, 24, "F4. Estimated effects with reported 95% CIs", 14, "middle", weight="bold")
    ests = [("Δ_A  (H1, confirmatory)", primary["H1"]), ("Δ_S  (H2, estimate only)", primary["H2"]),
            ("Δ_AS (E1, exploratory)", primary["E1_interaction"])]
    ax = Axis(-0.8, 0.4, 260, 660)
    x_axis(c, ax, 210, [-0.75, -0.5, -0.25, 0, 0.25], "{:.2f}", "difference in P(Y = 1)", grid_to=50)
    c.line(ax(0), 50, ax(0), 210, INK, 1)
    c.line(ax(mie), 50, ax(mie), 210, ACCENT2, 1, "5 4")
    c.text(ax(mie), 46, "MIE −0.25", 10, "middle", ACCENT2)
    for i, (lab, e) in enumerate(ests):
        y = 80 + i * 45
        lo, hi = e["ci_reported"]
        c.text(250, y + 4, lab, 12, "end")
        clo, chi = max(lo, ax.lo), min(hi, ax.hi)
        c.line(ax(clo), y, ax(chi), y, INK, 1.8)
        if lo < ax.lo:
            c.arrow(ax(clo) + 8, y, ax(clo), y)
        if hi > ax.hi:
            c.arrow(ax(chi) - 8, y, ax(chi), y)
        c.circle(ax(e["estimate"]), y, 5, BAD if i == 0 else MUTED)
        tested = e.get("tested", True) if i == 1 else i == 0
        p = f", p = {e['p_value']:.3f}" if tested and "p_value" in e else ("; not tested" if i == 1 else "")
        c.text(680, y + 4, f"{e['estimate']:+.3f} [{lo:+.3f}, {hi:+.3f}]{p}", 10, "start", MUTED)
    return c.render()


def f5_heatmap(secondary: dict, runs: list[dict]) -> str:
    rows = [("F1 · audit", "F1_audit"), ("F2 · audit", "F2_audit"), ("F2 · Tetragon", "F2_tetragon"),
            ("F3 · audit", "F3_audit"), ("F1 union", "F1_union"), ("F2 union", "F2_union"), ("F3 union", "F3_union")]
    c = Canvas(640, 70 + 34 * len(rows) + 40)
    c.text(320, 24, "F5. Fact × source recovery (CORRECT share) by cell", 14, "middle", weight="bold")
    for j, cell in enumerate(CELLS):
        c.text(230 + j * 95, 56, cell, 12, "middle", weight="bold")
    for i, (lab, key) in enumerate(rows):
        y = 66 + i * 34
        c.text(170, y + 21, lab, 12, "end")
        for j, cell in enumerate(CELLS):
            rs = [r for r in runs if r["cell"] == cell]
            x_ = sum(r[key] == "CORRECT" for r in rs)
            p = x_ / len(rs) if rs else 0
            c.rect(184 + j * 95, y, 92, 30, shade(p), "white")
            c.text(230 + j * 95, y + 20, f"{x_}/{len(rs)}", 11, "middle", "white" if p > 0.55 else INK)
    c.text(320, 70 + 34 * len(rows) + 20, "F1, F3: audit only (Tetragon not eligible); F2: either source (union rule)",
           10, "middle", MUTED)
    return c.render()


def f6_flow(validity: dict, primary: dict) -> str:
    acc = validity["M23_run_accounting"]["cell"]
    att = sum(v["attempts"] for v in acc.values())
    ab = sum(v.get("PRE_ACTION_ABORT", 0) for v in acc.values())
    un = sum(v.get("UNSCORABLE", 0) for v in acc.values())
    sc = sum(v.get("SCORED", 0) for v in acc.values())
    nc = acc.get("NC", {}).get("SCORED", 0)
    pos = sc - nc
    y1 = sum(v["x"] for v in primary["cells"].values())
    c = Canvas(900, 200)
    c.text(450, 24, "F6. Run flow", 14, "middle", weight="bold")
    steps = [("attempts", att), ("verified action", att - ab - un), ("scored (positive)", pos), ("Y = 1", y1)]
    for i, (lab, n) in enumerate(steps):
        x = 30 + i * 220
        c.rect(x, 60, 180, 60, "#eef3f9", ACCENT, rx=6)
        c.text(x + 90, 86, lab, 12, "middle")
        c.text(x + 90, 106, str(n), 14, "middle", weight="bold")
        if i:
            c.arrow(x - 40, 90, x, 90)
    c.text(250, 150, f"pre-action aborts {ab}, unscorable {un}", 11, "middle", MUTED)
    c.text(470, 150, f"negative controls {nc} (not in Y)", 11, "middle", MUTED)
    c.text(690, 150, f"Y = 0: {pos - y1}", 11, "middle", MUTED)
    return c.render()


def f7_calibration(e3: dict) -> str:
    c = Canvas(880, 440)
    c.text(440, 24, "F7. Observed vs predicted loss (Section 4 formula), Wilson 95% CI", 14, "middle", weight="bold")
    ax, ay = Axis(0, 1, 90, 620), Axis(0, 1, 360, 50)
    x_axis(c, ax, 360, [0, 0.25, 0.5, 0.75, 1.0], "{:.2f}", "predicted P(loss), uniform offset", grid_to=50)
    y_axis(c, ay, 90, [0, 0.25, 0.5, 0.75, 1.0], "{:.2f}", "observed loss", grid_to=620)
    c.line(ax(0), ay(0), ax(1), ay(1), MUTED, 1, "5 4")
    colors = {"F1-audit": BAD, "F3-audit": ACCENT2, "F2-audit": ACCENT, "F2-tetragon": INK}
    k = 0
    for key, d in e3.get("by_fact_source", {}).items():
        col = colors.get(key, INK)
        for lvl, v in d["by_level"].items():
            x, y = ax(v["mean_p_formula"]), ay(v["lost"] / v["n"])
            lo, hi = v["wilson95"]
            c.line(x, ay(lo), x, ay(hi), col, 1)
            c.circle(x, y, 3 + 2.2 * k, "none", col)
        c.circle(650, 70 + k * 18, 3 + 2.2 * k, "none", col)
        c.text(660, 74 + k * 18, f"{key} (Brier {d['M26_brier_formula']:.3f})", 11)
        k += 1
    c.text(355, 414, "one point per write-rate level (confirmatory L, M and CAL1–CAL4); delayed-read runs only", 10, "middle", MUTED)
    c.text(355, 428, "hollow rings of increasing size: coincident points of different series remain visible", 10, "middle", MUTED)
    return c.render()
