"""Tables T1-T8 (Blueprint Section 17.1) as Markdown and CSV, generated from the analysis outputs only."""
from __future__ import annotations

import csv
import io

CELLS = ("LC", "LD", "MC", "MD")


def _md(head: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


def _csv(head: list[str], rows: list[list]) -> str:
    b = io.StringIO()
    w = csv.writer(b, lineterminator="\n")
    w.writerow(head)
    w.writerows(rows)
    return b.getvalue()


def _ci(t) -> str:
    return f"[{t[0]:.3f}, {t[1]:.3f}]" if t else ""


def build(res: dict) -> dict[str, tuple[list[str], list[list]]]:
    prm, prim, sec, val, sens = res["params"], res["primary"], res["secondary"], res["validity"], res["sensitivity"]
    T: dict = {}
    ld = prm["load"]
    T["T1_protocol"] = (["Item", "Value"], [
        ["Design", "randomized complete blocks; cells LC, LD, MC, MD + 1 negative control per block"],
        ["Blocks B", prm["design"]["n_blocks"]],
        ["δ (delay) / g (grace) / ε", f"{prm['timing']['delay_s']} s / {prm['timing']['grace_s']} s / {prm['timing']['epsilon_s']} s"],
        ["Store M × k (audit, Tetragon)", f"{prm['stores']['audit']['max_size_mb']} MiB × {prm['stores']['audit']['max_backups']}; "
                                          f"{prm['stores']['tetragon']['max_size_mb']} MiB × {prm['stores']['tetragon']['max_backups']}"],
        ["r_L (audit, Tetragon) B/s", f"{ld['rate_low_bytes_per_s']['audit']}, {ld['rate_low_bytes_per_s']['tetragon']}"],
        ["r_M (audit, Tetragon) B/s", f"{ld['rate_moderate_bytes_per_s']['audit']}, {ld['rate_moderate_bytes_per_s']['tetragon']}"],
        ["Action", "create pod → exec nonce-tagged /usr/bin/echo → delete; harness-verified"],
        ["Sources", "API-server audit (file backend), Tetragon JSON export"],
        ["Master seed", prm["design"]["master_seed"]]])
    qual = res.get("qualification") or {}
    fq = (qual.get("G2") or {}).get("fact_correct_Q_a") or {}
    T["T2_contract"] = (["Fact", "Question", "Eligible", "Q-a CORRECT"], [
        [f, res["contract"]["facts"][f]["question"], ", ".join(res["contract"]["facts"][f]["eligible"]),
         "; ".join(f"{k.split('-')[1]} {v['text']}" for k, v in fq.items() if k.startswith(f)) or ""]
        for f in ("F1", "F2", "F3")])
    acc = val["M23_run_accounting"]["cell"]
    T["T3_accounting"] = (["Cell", "Attempts", "Scored", "Pre-action abort", "Unscorable", "Collector failure",
                           "Replacements"],
                          [[c, a["attempts"], a.get("SCORED", 0), a.get("PRE_ACTION_ABORT", 0), a.get("UNSCORABLE", 0),
                            a["collector_failure"], a["replacements"]] for c, a in sorted(acc.items())])
    h1 = prim["H1"]
    dl = val["M15_delay_observed_s"]
    T["T4_primary"] = (["Cell", "Y = 1 (x/n)", "p̂", "Wilson 95% CI"],
                       [[c, f"{prim['cells'][c]['x']}/{prim['cells'][c]['n']}", f"{prim['cells'][c]['p']:.3f}",
                         _ci(prim["cells"][c]["wilson95"])] for c in CELLS] +
                       [["Δ_A (H1)", f"{h1['estimate']:+.4f}", f"p = {h1['p_value']:.4f} ({h1['test']['method']})",
                         f"{_ci(h1['ci_reported'])} ({h1['ci_rule']}); block t {_ci(h1['ci_t'])}"],
                        ["δ_obs (D runs)", "", "", "; ".join(f"{c}: median {v['median']:.4f} s, range {v['min']:.4f}–{v['max']:.4f}"
                                                         for c, v in dl.items() if v.get("n"))]])
    m7 = sec["M7_per_fact_recovery"]
    e2 = sec["E2_F2_sources"]
    T["T5_secondary"] = (["Quantity", "Value"],
                         [["Δ_S (H2, estimate only)", f"{prim['H2']['estimate']:+.4f} {_ci(prim['H2']['ci_reported'])}"],
                          ["Δ_AS (E1, exploratory)", f"{prim['E1_interaction']['estimate']:+.4f} {_ci(prim['E1_interaction']['ci_reported'])}"]] +
                         [[f"π {f} in {c}", m7[c][f]["text"]] for c in CELLS for f in ("F1", "F2", "F3")] +
                         [["F2 paired (n11, n10, n01, n00)", f"{e2['pooled']['n11']}, {e2['pooled']['n10']}, {e2['pooled']['n01']}, {e2['pooled']['n00']}"],
                          ["F2 McNemar exact p", f"{e2['pooled']['mcnemar_exact_p']:.4f}"],
                          ["I_audit (union CORRECT, audit not)", e2["M10_incremental"]["I_audit"]["text"]],
                          ["I_tetragon (union CORRECT, Tetragon not)", e2["M10_incremental"]["I_tetragon"]["text"]],
                          ["False assertions audit / Tetragon", f"{sec['M12_false_assertion_rate']['audit']['text']} / {sec['M12_false_assertion_rate']['tetragon']['text']}"],
                          ["Abstentions audit / Tetragon", f"{sec['M13_abstention_rate']['audit']['text']} / {sec['M13_abstention_rate']['tetragon']['text']}"]])
    fa = val["M14_false_attribution"]
    e4 = res.get("e4") or {}
    T["T6_validity"] = (["Quantity", "Value"],
                        [["False attribution (NC + decoys)", f"{fa['x']}/{fa['n']} (Clopper–Pearson {_ci(fa['clopper_pearson95'])})"],
                         ["Conflicts (F2)", sec["M11_conflict_rate"]["text"]],
                         ["Drop events (DV-DROP)", val["M21_drop_events"]],
                         ["Delay non-compliant (DV-DELAY)", val["M15_delay_noncompliant"]],
                         ["Integrity re-verification", f"{res['verify']['packages_checked']} packages, ok = {res['verify']['ok']}"]] +
                        [[f"Loss stage {k}", f"{v}/{e4.get('non_correct_pairs', 0)}"] for k, v in (e4.get("M22_counts") or {}).items()])
    s_rows = []
    for k in ("S1", "S2", "S4", "S5", "S7"):
        h = sens[k].get("H1") or {}
        s_rows.append([k, sens[k]["rule"], len(sens[k]["excluded_runs"]), f"{h.get('estimate', float('nan')):+.4f}",
                       f"{h.get('p_value', float('nan')):.4f}"])
    for src, v in sens["S8"].items():
        h = v.get("H1") or {}
        s_rows.append([f"S8-{src}", v["rule"], "—", f"{h.get('estimate', float('nan')):+.4f}", f"{h.get('p_value', float('nan')):.4f}"])
    s3 = sens["S3"]["spearman"]
    s_rows.append(["S3", sens["S3"]["rule"], "—", f"ρ = {s3['rho']}", f"{s3['p']}"])
    s_rows.append(["S6", sens["S6"]["rule"], "—", str(sens["S6"]["by_multiplier"]), ""])
    s_rows.append(["S9", sens["S9"]["rule"], "—", f"{sens['S9']['mover_wilson'][0]:+.4f} {_ci(sens['S9']['mover_wilson'][1:])}", ""])
    pw = res.get("power") or {}
    s_rows.append(["Power at B", f"pilot_informed {pw.get('rows', {}).get('pilot_informed', {}).get('power')}, "
                                 f"conservative {pw.get('rows', {}).get('conservative', {}).get('power')}", "", "", ""])
    T["T7_sensitivity"] = (["ID", "Rule", "Excluded", "Δ_A", "p"], s_rows)
    T["T8_artifacts"] = (["Item", "Value"], [
        ["Confirmatory packages", len([r for r in res["runs_all"]])],
        ["Calibration packages", res.get("n_calibration_packages", 0)],
        ["Locked scores", res["verify"]["locked_scores"]],
        ["Score lock written", res["verify"]["lock_written"]],
        ["Analysis input sha256", res["verify"]["analysis_input_sha256"]],
        ["Primary reproduced from frozen command", prim["_reproduction"]["matches_locked_primary_json"]],
        ["Analysis commit", res["analysis_commit"]]])
    return T


def render(tables: dict) -> dict[str, str]:
    files = {}
    md = []
    for name, (head, rows) in tables.items():
        files[f"tables/{name}.csv"] = _csv(head, rows)
        md.append(f"## {name}\n\n" + _md(head, rows))
    files["tables/tables.md"] = "\n".join(md)
    return files
