"""Publication tables as LaTeX floats for the MDPI template, generated from the analysis results only.

Each file holds only the ``tabularx`` body (header and data rows) and is written to ``tables/latex/*.tex``;
captions, labels and notes belong to the manuscript, which is kept outside this repository. Main-text tables:
design (tab:design), fact contract (tab:contract), run accounting and complete recovery (tab:recovery),
effect estimates (tab:effects), source contribution and validity (tab:sources), loss mechanism
(tab:mechanism). Supplementary: sensitivity (tab:S-sensitivity), operational diagnostics
(tab:S-operation), integrity and reproducibility record (tab:S-integrity).
Only ASCII and LaTeX commands are emitted (pdfLaTeX-safe).
"""
from __future__ import annotations

CELLS = ("LC", "LD", "MC", "MD")
FS = (("F1", "audit"), ("F2", "audit"), ("F2", "tetragon"), ("F3", "audit"))
SRC = {"audit": "Audit", "tetragon": "Tetragon"}
L_COL = r">{\raggedright\arraybackslash}X"


def _n(v: float, d: int = 3) -> str:
    """Number with a typographic minus."""
    s = f"{v:.{d}f}"
    return s.replace("-", "$-$") if s.startswith("-") else s


def _ci(t, d: int = 3) -> str:
    return f"[{_n(t[0], d)}, {_n(t[1], d)}]" if t else "--"


def _p(p: float) -> str:
    return f"{p:.4f}" if p >= 0.0001 else "$<$0.0001"


def _kbs(v: float) -> str:
    return f"{v / 1000:.1f}"


def _float(colspec: str, head: list[str], body: list[str]) -> str:
    """The tabularx body only (data). Captions, labels, notes and placement are manuscript content and are
    written in the manuscript, not here."""
    lines = [rf"\begin{{tabularx}}{{\textwidth}}{{{colspec}}}", r"\toprule",
             " & ".join(rf"\textbf{{{h}}}" for h in head) + r" \\", r"\midrule"]
    lines += body
    lines += [r"\bottomrule", r"\end{tabularx}", ""]
    return "\n".join(lines)


def _row(*cells) -> str:
    return " & ".join(str(c) for c in cells) + r" \\"


def _group(title: str, ncol: int) -> str:
    return rf"\multicolumn{{{ncol}}}{{l}}{{\textit{{{title}}}}} \\"


# ---------------------------------------------------------------------- main-text tables
def t_design(res: dict) -> str:
    prm, val = res["params"], res["validity"]
    t, st, ld = prm["timing"], prm["stores"], prm["load"]
    dl = val["M15_delay_observed_s"]
    rates = val.get("M19_write_rate_bytes_per_s") or {}
    n_cal = res.get("n_calibration_packages", 0)
    cal = ld.get("calibration") or {}

    def achieved(level: str, src: str) -> str:
        q = (rates.get(level) or {}).get(src) or {}
        if not q.get("n"):
            return "--"
        return f"{_kbs(q['median'])} [{_kbs(q['q1'])}, {_kbs(q['q3'])}]"

    dmed = [v for v in dl.values() if v.get("n")]
    dtxt = (f", achieved {min(v['min'] for v in dmed):.3f}--{max(v['max'] for v in dmed):.3f}~s") if dmed else ""
    span = lambda txt: rf"\multicolumn{{2}}{{l}}{{{txt}}}"
    general = [
        r"\midrule",
        _row("Design", span(rf"{prm['design']['n_blocks']} blocks of LC, LD, MC, MD and NC")),
        _row("Continuous (C)", span(rf"follower until $T + g$, $g = {t['grace_s']:g}$~s")),
        _row("Delayed (D)", span(rf"one-shot copy at $T + \delta$, $\delta = {t['delay_s']:g}$~s{dtxt}")),
        _row("Record window", span(rf"$[A - \varepsilon,\, T + g]$, $\varepsilon = {t['epsilon_s']:g}$~s")),
    ]
    body = [
        _row(r"Store size $M \times k$",
             rf"{st['audit']['max_size_mb']}~MiB $\times$ {st['audit']['max_backups']}",
             rf"{st['tetragon']['max_size_mb']}~MiB $\times$ {st['tetragon']['max_backups']}"),
        _row("Low load, target rate", _kbs(ld["rate_low_bytes_per_s"]["audit"]), _kbs(ld["rate_low_bytes_per_s"]["tetragon"])),
        _row("Low load, achieved", achieved("L", "audit"), achieved("L", "tetragon")),
        _row("Moderate load, target rate", _kbs(ld["rate_moderate_bytes_per_s"]["audit"]),
             _kbs(ld["rate_moderate_bytes_per_s"]["tetragon"])),
        _row("Moderate load, achieved", achieved("M", "audit"), achieved("M", "tetragon")),
    ]
    if cal:
        lv = sorted(cal)
        body.append(_row(f"Calibration, {lv[0]}--{lv[-1]} targets",
                         ", ".join(_kbs(cal[k]["target_bytes_per_s"]["audit"]) for k in lv),
                         ", ".join(_kbs(cal[k]["target_bytes_per_s"]["tetragon"]) for k in lv)))
    body += general
    return _float("lCC",
                  ["Item", "Audit", "Tetragon"], body)


def t_contract(res: dict) -> str:
    con = res["contract"]["facts"]
    qual = (res.get("qualification") or {}).get("G2") or {}
    fq = qual.get("fact_correct_Q_a") or {}
    truth = {"F1": "Name and UID in the create response",
             "F2": "Issued command and nonce; exit status and output",
             "F3": "Delete response; 404 on termination polling"}
    body = []
    for f in ("F1", "F2", "F3"):
        q = con[f]["question"].replace("{ns}", "the run namespace").replace("{pod}", "the target pod")
        el = ", ".join(SRC[s] for s in con[f]["eligible"])
        qa = "; ".join(f"{SRC[k.split('-')[1]]} {v['text']}" for k, v in fq.items() if k.startswith(f + "-")) or "--"
        body.append(_row(f, q, el, truth[f], qa))
    return _float(rf"c{L_COL}l{L_COL}l", ["Fact", "Question answered", "Eligible", "Ground truth", "Qualification"], body)


def t_recovery(res: dict) -> str:
    acc = res["validity"]["M23_run_accounting"]["cell"]
    cells = res["primary"]["cells"]
    body, tot = [], {"att": 0, "exc": 0, "cf": 0, "sc": 0}
    rep = sum(a.get("replacements", 0) for a in acc.values())
    for c in CELLS + ("NC",):
        a = acc.get(c, {})
        exc = a.get("PRE_ACTION_ABORT", 0) + a.get("UNSCORABLE", 0)
        vals = (a.get("attempts", 0), exc, a.get("collector_failure", 0), a.get("SCORED", 0))
        for k, v in zip(tot, vals):
            tot[k] += v
        if c in cells:
            v = cells[c]
            body.append(_row(c, *vals, f"{v['x']}/{v['n']}", _ci(v["wilson95"])))
        else:
            body.append(_row(c, *vals, "--", "--"))
    body += [r"\midrule", _row("Total", *tot.values(), f"{sum(v['x'] for v in cells.values())}/"
                                                      f"{sum(v['n'] for v in cells.values())}", "")]
    return _float("lccCccl",
                  ["Condition", "Attempts", "Excluded", "Collector failure", "Scored", r"$Y = 1$", "95\\% CI"], body)


def t_effects(res: dict) -> str:
    prim = res["primary"]
    h1, h2, e1 = prim["H1"], prim["H2"], prim["E1_interaction"]
    test = h1.get("test") or {}
    meth = ("Monte Carlo, " + f"{test.get('n_assignments', 0):,}".replace(",", "\\,") + " draws") \
        if test.get("method") == "monte_carlo" else f"exact, {test.get('n_assignments', '')} assignments"
    alpha = 0.05
    rej = h1["p_value"] < alpha
    rows = [
        (r"$\Delta_A$, timing (D $-$ C)", "confirmatory", h1, rf"$p = {_p(h1['p_value'])}$ ({meth})",
         "Rejected" if rej else "Not rejected"),
        (r"$\Delta_S$, load (M $-$ L)", "secondary", h2,
         rf"$p = {_p(h2['p_value'])}$" if h2.get("tested") else "Not tested",
         ("Rejected" if h2["p_value"] < alpha else "Not rejected") if h2.get("tested") else "Estimate only"),
        (r"$\Delta_{AS}$, interaction", "exploratory", e1, "--", "Estimate only"),
    ]
    short = lambda r: "MOVER-Wilson" if r.startswith("MOVER") else r
    body = [_row(rf"{lab}; {role}", _n(e["estimate"]), _ci(e["ci_reported"]), short(e["ci_rule"]), tst, dec)
            for lab, role, e, tst, dec in rows]
    return _float(f"{L_COL}cll{L_COL}l",
                  ["Effect", "Estimate", "95\\% CI", "CI method", "Test", "Decision"], body)


def t_sources(res: dict) -> str:
    sec, val = res["secondary"], res["validity"]
    e2 = sec["E2_F2_sources"]
    pooled = e2["pooled"]
    fa = val["M14_false_attribution"]
    m12, m13 = sec["M12_false_assertion_rate"], sec["M13_abstention_rate"]
    ia, it = e2["M10_incremental"]["I_audit"], e2["M10_incremental"]["I_tetragon"]
    ver = res["verify"]
    body = [
        _group("F2 recovered by audit and Tetragon in the same run (positive runs)", 2),
        _row("Both sources correct", pooled["n11"]),
        _row("Audit only", pooled["n10"]),
        _row("Tetragon only", pooled["n01"]),
        _row("Neither source", pooled["n00"]),
        _row("Exact McNemar test (discordant pairs)", rf"$p = {_p(pooled['mcnemar_exact_p'])}$"),
        _row("Union F2 recovered only through audit", it["text"]),
        _row("Union F2 recovered only through Tetragon", ia["text"]),
        r"\midrule",
        _group("Answer errors and validity checks", 2),
        _row("False assertions, audit / Tetragon (eligible run--fact pairs)", f"{m12['audit']['text']} / {m12['tetragon']['text']}"),
        _row("Abstentions, audit / Tetragon", f"{m13['audit']['text']} / {m13['tetragon']['text']}"),
        _row("Conflicting F2 answers between sources", sec["M11_conflict_rate"]["text"]),
        _row("False attribution, NC runs and decoy identities",
             f"{fa['x']}/{fa['n']} (95\\% CI {_ci(fa['clopper_pearson95'])})"),
        _row("Runs with source drop events", val["M21_drop_events"]),
        _row(r"Delayed runs outside the $\pm$2~s delay tolerance", val["M15_delay_noncompliant"]),
        _row("Evidence packages verified before analysis", f"{ver['packages_checked']}/{ver['packages_checked']}"
             if ver["ok"] else f"failed: {len(ver['problems'])} problems"),
    ]
    return _float(rf"{L_COL}l",
                  ["Quantity", "Value"], body)


def t_mechanism(res: dict) -> str:
    e3 = (res.get("e3") or {}).get("by_fact_source") or {}
    items = (res.get("e4") or {}).get("items") or []
    stages = ("EVICTED", "NOT_EMITTED", "NOT_ACQUIRED", "NOT_MATCHED", "UNRESOLVED")
    body = []
    for f, s in FS:
        d = e3.get(f"{f}-{s}")
        if not d:
            continue
        cf = d["H_rule_confusion"]
        lost_lt = f"{cf['tp']}/{cf['tp'] + cf['fp']}"
        lost_ge = f"{cf['fn']}/{cf['fn'] + cf['tn']}"
        its = [i for i in items if i["fact"] == f and i["source"] == s]
        ev = sum(i["stage"] == "EVICTED" for i in its)
        other = len(its) - ev
        conf = sum(i["stage"] == "EVICTED" and i["cohort"] == "confirmatory" for i in its)
        cal = ev - conf
        body.append(_row(f"{f}, {SRC[s]}", d["n"], lost_lt, lost_ge, f"{d['M26_brier_formula']:.3f}",
                         f"{ev}/{len(its)} ({conf} / {cal})", other))
    tot = len(items)
    counts = {st: sum(i["stage"] == st for i in items) for st in stages}
    return _float("lCCCCcC",
                  ["Fact, source", "Pairs", r"Lost ($H < 1$)", r"Lost ($H \geq 1$)", "Brier", "Evicted", "Other"], body)


# ---------------------------------------------------------------------- supplementary tables
def t_sensitivity(res: dict) -> str:
    sens, prim = res["sensitivity"], res["primary"]
    lab = {"S1": "Exclude runs with collector failures", "S2": "Exclude delayed runs outside the delay tolerance",
           "S4": "Exclude runs with source drop events", "S5": "Exclude blocks with any false attribution",
           "S7": "Exclude runs attributed by container-ID fallback"}
    body = [_row("Primary analysis", "--", _n(prim["H1"]["estimate"]), rf"$p = {_p(prim['H1']['p_value'])}$")]
    for k in ("S1", "S2", "S4", "S5", "S7"):
        h = sens[k].get("H1") or {}
        body.append(_row(f"{k}: {lab[k]}", len(sens[k]["excluded_runs"]),
                         _n(h["estimate"]) if "estimate" in h else "--",
                         rf"$p = {_p(h['p_value'])}$" if "p_value" in h else "--"))
    for src, v in sens["S8"].items():
        h = v.get("H1") or {}
        body.append(_row(f"S8: F2 from {SRC.get(src, src)} only", "--", _n(h["estimate"]), rf"$p = {_p(h['p_value'])}$"))
    s3 = sens["S3"]["spearman"]
    if s3.get("rho") is not None:
        body.append(_row("S3: block contrast vs block order (Spearman)", "--", rf"$\rho = {_n(s3['rho'])}$",
                         rf"$p = {_p(s3['p'])}$"))
    s6 = sens["S6"]["by_multiplier"]
    body.append(_row(r"S6: F5 ordering at 0.5$\times$, 1$\times$, 2$\times$ tolerance (CORRECT)", "--",
                     " / ".join(str(d.get("CORRECT", 0)) for d in s6.values()), ""))
    mw = sens["S9"]["mover_wilson"]
    body.append(_row("S9: include incomplete blocks (MOVER-Wilson)", len(sens["S9"]["incomplete_blocks"] or []),
                     _n(mw[0]), _ci(mw[1:])))
    body.append(_row("Block-paired interval for $\\Delta_A$ (not used; rule triggered MOVER-Wilson)", "--", "",
                     _ci(prim["H1"]["ci_t"])))
    pw = (res.get("power") or {}).get("rows") or {}
    if pw:
        body.append(_row("Prospective power at the chosen $B$ (pilot-informed / conservative)", "--",
                         f"{pw.get('pilot_informed', {}).get('power', 0):.3f} / {pw.get('conservative', {}).get('power', 0):.3f}", ""))
    return _float(rf"{L_COL}ccc",
                  ["Analysis", "Runs excluded", "Estimate", "Test or 95\\% CI"], body)


def t_operation(res: dict) -> str:
    val = res["validity"]
    lag, over = val.get("M18_follower_lag_bytes") or {}, val.get("M24_collector_overhead") or {}
    body = []
    for c in CELLS:
        lg, ov = lag.get(c), over.get(c)
        body.append(_row(c, f"{lg['mean_of_means'] / 1000:.1f}" if lg else "--", f"{lg['max'] / 1000:.1f}" if lg else "--",
                         f"{ov['cpu_busy_mean']:.1f}" if ov else "--",
                         f"{ov['tetragon_rss_kb_max'] / 1024:.0f}" if ov else "--"))
    return _float("lCCCC",
                  ["Condition", "Follower lag, mean (kB)", "Follower lag, max (kB)", "Host CPU busy (\\%)",
                   "Tetragon RSS, max (MiB)"], body)


def t_integrity(res: dict) -> str:
    ver, prim = res["verify"], res["primary"]
    body = [_row("Evidence packages (confirmatory / calibration)", f"{len(res.get('runs_all') or res['runs'])} / "
                                                                    f"{res.get('n_calibration_packages', 0)}"),
            _row("Packages verified against their manifests", f"{ver['packages_checked']} (ok)" if ver["ok"] else "failed"),
            _row("Locked scores", ver.get("locked_scores", "--")),
            _row("Score lock written (UTC)", str(ver.get("lock_written", "--")).replace("+00:00", "")),
            _row("Analysis input SHA-256", rf"\texttt{{{str(ver.get('analysis_input_sha256', ''))[:16]}\ldots}}"),
            _row("Primary analysis reproduced by the frozen command",
                 "yes" if prim.get("_reproduction", {}).get("matches_locked_primary_json") else "no"),
            _row("Analysis commit", rf"\texttt{{{res['analysis_commit'][:12]}}}")]
    return _float(rf"{L_COL}{L_COL}", ["Item", "Value"], body)


def build(res: dict) -> dict[str, str]:
    return {"tables/latex/T1_design.tex": t_design(res), "tables/latex/T2_contract.tex": t_contract(res),
            "tables/latex/T3_recovery.tex": t_recovery(res), "tables/latex/T4_effects.tex": t_effects(res),
            "tables/latex/T5_sources.tex": t_sources(res), "tables/latex/T6_mechanism.tex": t_mechanism(res),
            "tables/latex/S1_sensitivity.tex": t_sensitivity(res), "tables/latex/S2_operation.tex": t_operation(res),
            "tables/latex/S3_integrity.tex": t_integrity(res)}
