"""Figures F1-F7 (Blueprint Section 17.2), generated from the analysis outputs only."""
from __future__ import annotations

import math

from .svg import ACCENT, ACCENT2, BAD, GRID, INK, MUTED, Axis, Canvas, shade, x_axis, y_axis

CELLS = ("LC", "LD", "MC", "MD")
CELL_LABEL = {"LC": "Low load, continuous", "LD": "Low load, delayed", "MC": "Moderate load, continuous",
              "MD": "Moderate load, delayed"}


# Publication figures carry no embedded title (the caption does). Canvas widths are chosen for the
# intended print width so that text stays >= 7 pt: F1, F2, F7 at full page width (~17 cm, 760 px);
# F3, F4, F5 at text width (~13.5 cm, 520-640 px).
FS, FS_S = 13, 12          # label and secondary text size (px)


def _box(c, x, y, w, h, title, lines=(), fill="white", stroke=INK, dash=None):
    if dash:
        c.items.append(f"<rect x='{x:.1f}' y='{y:.1f}' width='{w:.1f}' height='{h:.1f}' rx='6' fill='{fill}' "
                       f"stroke='{stroke}' stroke-width='1.2' stroke-dasharray='{dash}'/>")
    else:
        c.rect(x, y, w, h, fill, stroke, 1.2, rx=6)
    n = 1 + len(lines)
    y0 = y + h / 2 - (n - 1) * 8 + 4
    c.text(x + w / 2, y0, title, FS, "middle", weight="bold")
    for i, ln in enumerate(lines):
        c.text(x + w / 2, y0 + 16 * (i + 1), ln, FS_S - 0.5, "middle", MUTED)


def f1_architecture() -> str:
    """Schematic (conceptual, not data): producers, finite rotating stores, acquisition boundary, assigned
    acquisition, write-once archive, blinded reconstruction, scoring against independent truth, and the
    sealed diagnostic layer used only for mechanism analysis."""
    c = Canvas(760, 372)
    # producers and producer-side stores
    _box(c, 10, 40, 150, 60, "API server", ["audit log (file backend)"], "#eef3f9", ACCENT)
    _box(c, 10, 130, 150, 60, "Tetragon (eBPF)", ["process events (JSON)"], "#eef3f9", ACCENT)
    _box(c, 185, 40, 165, 60, "Audit store", ["active file + k backups", "oldest deleted on rotation"])
    _box(c, 185, 130, 165, 60, "Tetragon store", ["active file + k backups", "oldest deleted on rotation"])
    c.arrow(160, 70, 185, 70)
    c.arrow(160, 160, 185, 160)
    # acquisition boundary
    c.line(372, 22, 372, 222, BAD, 1.6, "6 4")
    c.text(372, 16, "acquisition boundary", FS_S, "middle", BAD)
    # assigned acquisition (one arm per run, both stores)
    c.rect(392, 40, 178, 150, "white", INK, 1.2, rx=6)
    c.text(481, 62, "Assigned acquisition", FS, "middle", weight="bold")
    c.text(481, 78, "(one arm per run)", FS_S - 0.5, "middle", MUTED)
    c.rect(402, 88, 158, 42, "#eef3f9", ACCENT, 1.0, rx=4)
    c.text(481, 105, "C: continuous follower", FS_S, "middle")
    c.text(481, 121, [("from start offset to T + ", None), ("g", "it")], FS_S - 0.5, "middle", MUTED)
    c.rect(402, 138, 158, 42, "#fbf5ec", ACCENT2, 1.0, rx=4)
    c.text(481, 155, "D: one-shot copy", FS_S, "middle")
    c.text(481, 171, [("of retained files at T + ", None), ("δ", "it")], FS_S - 0.5, "middle", MUTED)
    c.arrow(350, 70, 392, 90)
    c.arrow(350, 160, 392, 140)
    # archive -> reconstruction -> scoring <- truth
    _box(c, 595, 85, 155, 60, "Evidence archive", ["write-once, SHA-256"])
    c.arrow(570, 115, 595, 115)
    _box(c, 595, 235, 155, 60, "Blinded reconstruction", ["rule-based matcher"])
    c.arrow(672, 145, 672, 235)
    _box(c, 392, 235, 178, 60, "Scoring", ["CORRECT · FALSE · ABSTAIN"])
    c.arrow(595, 265, 570, 265)
    _box(c, 392, 312, 178, 52, "Independent ground truth", ["sealed until answers hashed"], "#f3f4f6", MUTED)
    c.arrow(481, 312, 481, 295)
    # sealed diagnostic layer
    _box(c, 10, 248, 340, 72, "Sealed diagnostics (not visible to reconstruction)",
         ["1 Hz store-growth monitor, reference observers,", "store snapshot: used for headroom and loss stages"],
         "#fafafa", MUTED, dash="5 4")
    c.items.append("<line x1='267' y1='190' x2='267' y2='248' stroke='#616e7c' stroke-width='1.2' stroke-dasharray='4 3'/>")
    c.text(273, 224, "read-only copy", FS_S - 1.5, "start", MUTED)
    return c.render()


def f2_timeline(tl: dict) -> str:
    """One observed delayed-acquisition run (not a schematic).
    tl: {run_id, cell, t0_ns, states: {name: ns}, epsilon_s, grace_s, delay_s, rotations: [ns], deletions: [ns]}"""
    c = Canvas(760, 236)
    t0 = tl["t0_ns"]
    rel = lambda ns: (ns - t0) / 1e9
    term = rel(tl["states"]["TERMINATION_CONFIRMED"])
    end = term + tl["delay_s"] + 6
    ax = Axis(-tl["epsilon_s"] - 2, end, 40, 745)
    x_axis(c, ax, 184, list(range(0, int(end) + 1, 10)), "{:.0f}", "Time since action request (s)", size=FS_S,
           label_size=FS)
    # matching window [A - eps, T + g]
    c.rect(ax(-tl["epsilon_s"]), 28, ax(term + tl["grace_s"]) - ax(-tl["epsilon_s"]), 22, "#eef3f9", "none")
    c.text(ax(-tl["epsilon_s"]) + 4, 43, [("record window [", None), ("A", "it"), (" − ", None), ("ε", "it"),
                                          (", ", None), ("T", "it"), (" + ", None), ("g", "it"), ("]", None)],
           FS_S - 0.5, "start", MUTED)
    for i, (name, lab) in enumerate((("CREATE_CONFIRMED", "pod created"), ("EXEC_CONFIRMED", "command executed"),
                                      ("TERMINATION_CONFIRMED", "termination confirmed (T)"))):
        if name in tl["states"]:
            x = ax(rel(tl["states"][name]))
            c.line(x, 60, x, 122 + 15 * i, INK, 1.2)
            c.text(x + 4, 126 + 15 * i, lab, FS_S - 0.5, "start")
    c.line(ax(term + tl["grace_s"]), 60, ax(term + tl["grace_s"]), 112, ACCENT, 1.4, "4 3")
    c.text(ax(term + tl["grace_s"]) + 4, 70, [("T", "it"), (" + ", None), ("g", "it")], FS_S - 0.5, "start", ACCENT)
    c.line(ax(term + tl["delay_s"]), 60, ax(term + tl["delay_s"]), 150, ACCENT2, 2)
    c.text(ax(term + tl["delay_s"]) - 4, 164, [("delayed read, ", None), ("T", "it"), (" + ", None), ("δ", "it")],
           FS_S - 0.5, "end", ACCENT2)
    for ns in tl.get("rotations", []):
        x = ax(rel(ns))
        if ax.p0 <= x <= ax.p1:
            c.line(x, 84, x, 104, INK, 1.6)
    for ns in tl.get("deletions", []):          # wall_ns = last 1 Hz sample that still saw the file
        x0, x1 = ax(rel(ns)), ax(rel(ns) + 1.0)
        if ax.p0 <= x0 <= ax.p1:
            c.rect(x0, 89, max(x1 - x0, 3), 10, BAD, "none")
    # legend (drawn symbols, no glyphs)
    lx = 470
    c.line(lx, 34, lx, 48, INK, 1.6)
    c.text(lx + 6, 45, "audit store rotation", FS_S - 0.5, "start", MUTED)
    c.rect(lx + 150, 36, 8, 10, BAD, "none")
    c.text(lx + 164, 45, "backup file deleted", FS_S - 0.5, "start", MUTED)
    return c.render()


def f3_cells(primary: dict) -> str:
    c = Canvas(520, 292)
    ay = Axis(0, 1, 240, 22)
    y_axis(c, ay, 78, [0, 0.25, 0.5, 0.75, 1.0], "{:.2f}", [("Complete recovery, ", None), ("P", "it"), ("(", None),
                                                           ("Y", "it"), (" = 1)", None)],
           grid_to=505, size=FS_S, label_size=FS, label_dx=50)
    for i, cell in enumerate(CELLS):
        v = primary["cells"][cell]
        x = 140 + i * 105
        lo, hi = v["wilson95"]
        c.line(x, ay(lo), x, ay(hi), INK, 1.5)
        for yy in (lo, hi):
            c.line(x - 7, ay(yy), x + 7, ay(yy), INK, 1.5)
        c.circle(x, ay(v["p"]), 6.5, ACCENT2 if cell.endswith("D") else ACCENT)
        c.text(x, 262, cell, FS + 1, "middle", weight="bold")
        c.text(x, 282, f"{v['x']}/{v['n']}", FS_S, "middle", MUTED)
    return c.render()


def f4_deltas(primary: dict, mie: float = -0.25) -> str:
    c = Canvas(640, 214)
    rows = [([("Timing, Δ", None), ("A", "sub")], "confirmatory", primary["H1"], True),
            ([("Load, Δ", None), ("S", "sub")], "secondary", primary["H2"], primary["H2"].get("tested", False)),
            ([("Interaction, Δ", None), ("AS", "sub")], "exploratory", primary["E1_interaction"], False)]
    ax = Axis(-0.75, 0.25, 175, 450)
    x_axis(c, ax, 160, [-0.75, -0.5, -0.25, 0, 0.25], lambda t: f"{t:.2f}".replace("-", "−"),
           [("Difference in ", None), ("P", "it"), ("(", None), ("Y", "it"), (" = 1)", None)], grid_to=14,
           size=FS_S, label_size=FS)
    c.line(ax(0), 14, ax(0), 160, INK, 1)
    c.line(ax(mie), 14, ax(mie), 160, ACCENT2, 1, "5 4")
    c.text(ax(mie) - 4, 26, "MIE", FS_S - 1, "end", ACCENT2)
    fmt = lambda v: f"{v:+.3f}".replace("-", "−")
    for i, (lab, role, e, tested) in enumerate(rows):
        y = 42 + i * 44
        lo, hi = e["ci_reported"]
        c.text(165, y - 2, lab, FS, "end")
        c.text(165, y + 13, role, FS_S - 1, "end", MUTED)
        clo, chi = max(lo, ax.lo), min(hi, ax.hi)
        c.line(ax(clo), y, ax(chi), y, INK, 1.8)
        if lo < ax.lo:
            c.arrow(ax(clo) + 8, y, ax(clo), y)
        if hi > ax.hi:
            c.arrow(ax(chi) - 8, y, ax(chi), y)
        c.circle(ax(e["estimate"]), y, 5.5, ACCENT2 if i == 0 else MUTED)
        c.text(462, y - 2, f"{fmt(e['estimate'])} [{fmt(lo)}, {fmt(hi)}]", FS_S, "start")
        note = (f"p = {e['p_value']:.3f}" if tested and "p_value" in e else
                ("not tested (fixed sequence)" if i == 1 else "estimate only"))
        c.text(462, y + 13, note, FS_S - 1, "start", MUTED)
    return c.render()


def f5_heatmap(secondary: dict, runs: list[dict]) -> str:
    """Fact x source recovery by condition; Tetragon cells for F1 and F3 are marked not eligible
    (an eligibility rule, not an observed failure)."""
    rows = [("F1", "audit", "F1_audit"), ("F1", "Tetragon", None),
            ("F2", "audit", "F2_audit"), ("F2", "Tetragon", "F2_tetragon"), ("F2", "union", "F2_union"),
            ("F3", "audit", "F3_audit"), ("F3", "Tetragon", None)]
    names = {"F1": "F1 pod creation", "F2": "F2 command execution", "F3": "F3 pod deletion"}
    W, x0, cw, rh, top = 560, 210, 86, 28, 30
    c = Canvas(W, top + rh * len(rows) + 3 * 8 + 18)
    for j, cell in enumerate(CELLS):
        c.text(x0 + j * cw + cw / 2, 20, cell, FS + 1, "middle", weight="bold")
    y, prev = top, None
    for fact, src, key in rows:
        if prev and fact != prev:
            y += 8
        if fact != prev:
            c.text(8, y + 18, names[fact], FS, "start", weight="bold")
        c.text(x0 - 8, y + 18, src, FS_S, "end", MUTED if key is None else INK)
        for j, cell in enumerate(CELLS):
            x = x0 + j * cw
            if key is None:
                c.rect(x + 1, y + 1, cw - 2, rh - 2, "#f3f4f6", "#d9dee4")
                c.text(x + cw / 2, y + 18, "not eligible", FS_S - 2, "middle", MUTED)
                continue
            rs = [r for r in runs if r["cell"] == cell]
            n_ok = sum(r[key] == "CORRECT" for r in rs)
            p = n_ok / len(rs) if rs else 0
            c.rect(x + 1, y + 1, cw - 2, rh - 2, shade(p), "white")
            c.text(x + cw / 2, y + 18, f"{n_ok}/{len(rs)}", FS_S, "middle", "white" if p > 0.55 else INK)
        y += rh
        prev = fact
    return c.render()


def f6_flow(validity: dict, primary: dict) -> str:
    """Attempt disposition (kept for the supplement; with no exclusions it adds little to Table 3)."""
    acc = validity["M23_run_accounting"]["cell"]
    att = sum(v["attempts"] for v in acc.values())
    ab = sum(v.get("PRE_ACTION_ABORT", 0) for v in acc.values())
    un = sum(v.get("UNSCORABLE", 0) for v in acc.values())
    sc = sum(v.get("SCORED", 0) for v in acc.values())
    nc = acc.get("NC", {}).get("SCORED", 0)
    pos = sc - nc
    y1 = sum(v["x"] for v in primary["cells"].values())
    c = Canvas(640, 130)
    steps = [("attempts", att), ("action verified", att - ab - un), ("scored, positive", pos), ("Y = 1", y1)]
    for i, (lab, n) in enumerate(steps):
        x = 10 + i * 160
        c.rect(x, 14, 130, 54, "#eef3f9", ACCENT, rx=6)
        c.text(x + 65, 36, lab, FS_S, "middle")
        c.text(x + 65, 57, str(n), FS + 2, "middle", weight="bold")
        if i:
            c.arrow(x - 30, 41, x, 41)
    c.text(170, 96, f"aborted {ab}, unscorable {un}", FS_S - 1, "middle", MUTED)
    c.text(330, 96, f"negative controls {nc}", FS_S - 1, "middle", MUTED)
    c.text(490, 96, f"Y = 0: {pos - y1}", FS_S - 1, "middle", MUTED)
    return c.render()


def _jitter(key: str) -> float:
    """Deterministic vertical jitter in [-1, 1] from a record key (no random state)."""
    import hashlib
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF * 2 - 1


def f7_calibration(e3: dict, rows: list[dict] | None = None) -> str:
    """(a) Each delayed-read (record, source) pair at its measured headroom H, lost vs retained, with the
    H < 1 rule; confirmatory and calibration series distinguished. (b) Observed vs formula-predicted loss
    per write-rate level with Wilson 95% intervals."""
    c = Canvas(760, 318)
    # ---- panel (a)
    rs = [r for r in (rows or []) if r.get("H") is not None and r["H"] > 0]
    c.text(8, 16, "(a)", FS, "start", weight="bold")
    if rs:
        lo2 = math.floor(math.log2(min(r["H"] for r in rs)))
        hi2 = math.ceil(math.log2(max(r["H"] for r in rs)))
        hi2 = max(hi2, lo2 + 2)
        ax = Axis(2.0 ** lo2, 2.0 ** hi2, 70, 360, log2=True)
        ylost, yret, band = 70, 200, 34
        c.rect(ax.p0, 30, ax(1.0) - ax.p0, 210, "#fbf5ec", "none") if ax.lo < 1 else None
        c.line(ax(1.0), 30, ax(1.0), 240, BAD, 1.4, "5 4")
        c.text(ax(1.0) + 4, 42, [("H", "it"), (" = 1", None)], FS_S, "start", BAD)
        ticks = [2.0 ** k for k in range(lo2, hi2 + 1)]
        x_axis(c, ax, 240, ticks, lambda t: (f"{t:g}" if t >= 1 else f"1/{int(round(1 / t))}"),
               [("Measured headroom ", None), ("H", "it"), (" (log scale)", None)], size=FS_S, label_size=FS)
        c.text(62, ylost + 4, "lost", FS_S, "end")
        c.text(62, yret + 4, "retained", FS_S, "end")
        c.line(ax.p0, ylost + band + 8, ax.p1, ylost + band + 8, GRID, 0.8)
        for r in rs:
            x = ax(min(max(r["H"], ax.lo), ax.hi))
            y = (ylost if r["observed_lost"] else yret) + _jitter(f"{r['run_id']}|{r['fact']}|{r['source']}") * band * 0.8
            col = ACCENT if r["cohort"] == "confirmatory" else ACCENT2
            if r["source"] == "audit":
                c.circle(x, y, 3.6, "none", col, 1.2)
            else:
                c.polygon([(x, y - 4.5), (x - 4.2, y + 3.2), (x + 4.2, y + 3.2)], "none", col, 1.2)
        n_lt = [r for r in rs if r["H"] < 1]
        n_ge = [r for r in rs if r["H"] >= 1]
        c.text(360, 26, f"H < 1: {sum(r['observed_lost'] for r in n_lt)}/{len(n_lt)} lost;   "
                         f"H ≥ 1: {sum(r['observed_lost'] for r in n_ge)}/{len(n_ge)} lost", FS_S - 0.5, "end", MUTED)
        # legend
        lx, ly = 70, 312
        for k, (lab, col) in enumerate((("confirmatory", ACCENT), ("calibration", ACCENT2))):
            c.rect(lx + k * 110, ly - 9, 10, 10, col, "none")
            c.text(lx + k * 110 + 14, ly, lab, FS_S - 1, "start", MUTED)
        c.circle(lx + 236, ly - 4, 3.6, "none", INK, 1.2)
        c.text(lx + 244, ly, "audit", FS_S - 1, "start", MUTED)
        c.polygon([(lx + 290, ly - 8.5), (lx + 285.8, ly - 0.8), (lx + 294.2, ly - 0.8)], "none", INK, 1.2)
        c.text(lx + 298, ly, "Tetragon", FS_S - 1, "start", MUTED)
    # ---- panel (b)
    c.text(400, 16, "(b)", FS, "start", weight="bold")
    bx, by = Axis(0, 1, 455, 740), Axis(0, 1, 240, 30)
    x_axis(c, bx, 240, [0, 0.25, 0.5, 0.75, 1.0], "{:.2f}", "Predicted loss probability", size=FS_S, label_size=FS)
    y_axis(c, by, 455, [0, 0.25, 0.5, 0.75, 1.0], "{:.2f}", "Observed loss proportion", grid_to=740, size=FS_S,
           label_size=FS, label_dx=46)
    c.line(bx(0), by(0), bx(1), by(1), MUTED, 1, "5 4")
    shapes = {"F1-audit": 0, "F2-audit": 1, "F3-audit": 2, "F2-tetragon": 3}
    for key, d in e3.get("by_fact_source", {}).items():
        k = shapes.get(key, 0)
        col = ACCENT2 if key.endswith("tetragon") else ACCENT
        for lvl, v in d["by_level"].items():
            x, y = bx(v["mean_p_formula"]), by(v["lost"] / v["n"])
            lo, hi = v["wilson95"]
            c.line(x, by(lo), x, by(hi), col, 1)
            c.circle(x, y, 3 + 1.8 * k, "none", col, 1.1)
    c.circle(470, 308, 3.6, "none", ACCENT, 1.1)
    c.text(478, 312, "audit, F1–F3 (ring size by fact)", FS_S - 1, "start", MUTED)
    c.circle(672, 308, 3.6, "none", ACCENT2, 1.1)
    c.text(680, 312, "Tetragon, F2", FS_S - 1, "start", MUTED)
    return c.render()
