"""Command line: frx <command>. See README for the gate at which each command is used."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from . import contract as contract_mod
from .headroom import predicted_loss_probability, rate_for_headroom, rate_for_loss_probability
from .integrity import load_required, verify_block, verify_package
from .metrics import block_matrix, cell_proportions, deltas
from .power import Scenario, choose_b, run_grid
from .randomization import make_schedule, write_schedule
from .stats import primary_analysis, wilson
from .status import validate_status
from .cluster import KIND_V0_33_DEFAULT_IMAGE, StoreSizes, render_kind_config, render_tetragon_values

REPO = Path(__file__).resolve().parents[2]


def _json(obj) -> str:
    return json.dumps(obj, indent=2, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))


def cmd_schedule(a) -> int:
    rows = make_schedule(a.seed, a.blocks, a.prefix)
    digest = write_schedule(rows, Path(a.out), a.seed)
    print(_json({"written": a.out, "sha256": digest, "blocks": a.blocks, "slots": len(rows)}))
    return 0


def cmd_power(a) -> int:
    spec = json.loads(Path(a.scenarios).read_text())
    scenarios = [Scenario(s["name"], s["p"]) for s in spec["scenarios"]]
    rows = run_grid(scenarios, a.b_values, a.sigmas, a.n_sims, a.n_draws, a.seed,
                    Path(a.out) if a.out else None)
    chosen = None
    if a.primary and a.conservative:
        chosen = choose_b(rows, a.primary, a.conservative)
    print(_json({"rows": rows, "chosen_B": chosen}))
    return 0


def _read_scores(path: Path) -> list[dict]:
    with open(path) as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["Y"] = int(r["Y"])
    return rows


def cmd_analyze(a) -> int:
    runs = _read_scores(Path(a.scores))
    props = cell_proportions(runs)
    y, complete, incomplete = block_matrix(runs)
    out = {
        "cells": {c: {"x": x, "n": n, "p": p, "wilson95": wilson(x, n)} for c, (x, n, p) in props.items()},
        "deltas_all_scored_runs": deltas({c: v[2] for c, v in props.items()}),
        "complete_blocks": complete, "incomplete_blocks": incomplete,
    }
    if y.shape[0] >= 2:
        out["H1"] = primary_analysis(y, "A", n_draws=a.draws, seed=a.seed)
        h2 = primary_analysis(y, "S", n_draws=a.draws, seed=a.seed + 1)
        h2["tested"] = bool(out["H1"]["p_value"] < 0.05)   # fixed sequence
        if not h2["tested"]:
            h2["note"] = "H1 not rejected: H2 reported as estimate only (fixed sequence)"
        out["H2"] = h2
        out["E1_interaction"] = primary_analysis(y, "AS", seed=a.seed + 2)
    text = _json(out)
    if a.out:
        Path(a.out).write_text(text)
    print(text)
    return 0


def cmd_verify_package(a) -> int:
    spec = load_required(REPO / "protocol" / "required_files.yaml")
    res = verify_package(Path(a.package), spec, validate_status)
    print(_json(res))
    return 0 if res["ok"] else 2


def cmd_verify_block(a) -> int:
    res = verify_block([Path(p) for p in a.packages], Path(a.replica))
    print(_json(res))
    return 0 if res["ok"] else 2


def cmd_freeze_check(a) -> int:
    res = contract_mod.freeze_readiness()
    print(_json(res))
    blocking = res["contract_problems"] or res["parameter_problems"] or res["unresolved"]
    return 2 if blocking else 0


def cmd_headroom(a) -> int:
    out = {
        "r_low_bytes_per_s (worst-case H = target)": rate_for_headroom(a.h_low, a.elapsed, a.m, a.k),
        "r_moderate_bytes_per_s (P(loss)=target)": rate_for_loss_probability(a.p_mod, a.elapsed, a.m, a.k),
    }
    if a.rate is not None:
        out["predicted_loss_at_rate"] = predicted_loss_probability(a.rate, a.elapsed, a.m, a.k)
    print(_json(out))
    return 0


def cmd_cluster_config(a) -> int:
    sizes = StoreSizes(a.audit_max_mb, a.audit_backups, a.tetragon_max_mb, a.tetragon_backups,
                       provisional=not a.final)
    from .cluster import render_audit_webhook_kubeconfig
    out = Path(a.out_dir); out.mkdir(parents=True, exist_ok=True)
    for sub in ("audit", "tetragon"):
        (Path(a.stores) / sub).mkdir(parents=True, exist_ok=True)
    written = []
    wh = None
    if a.audit_webhook:
        wh = out / "audit-webhook.kubeconfig"
        wh.write_text(render_audit_webhook_kubeconfig(a.audit_webhook))
        written.append(str(wh))
    (out / "kind-config.yaml").write_text(render_kind_config(REPO, Path(a.stores), sizes, a.node_image, wh))
    (out / "tetragon-values.yaml").write_text(render_tetragon_values(sizes))
    written += [str(out / "kind-config.yaml"), str(out / "tetragon-values.yaml")]
    print(_json({"written": written, "sizes": sizes.__dict__, "node_image": a.node_image,
                 "audit_webhook": a.audit_webhook}))
    return 0


def cmd_fingerprint(a) -> int:
    from .fingerprint import collect
    fp = collect(REPO)
    text = _json(fp)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text)
        print(_json({"written": a.out, "stable_hash": fp["stable_hash"]}))
    else:
        print(text)
    return 0


def cmd_smoke(a) -> int:
    import subprocess
    from .smoke import run
    ctx = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True).stdout.strip()
    if ctx != "kind-frx":
        print(f"refusing: current kubectl context is '{ctx}', expected 'kind-frx'")
        return 2
    rep = run(REPO, Path(a.stores), Path(a.out_dir), a.image, a.exec_binary, a.settle)
    summary = {"report": rep.get("report_path"), "error": rep.get("error"),
               "verdict": (rep.get("results") or {}).get("verdict"),
               "action_duration_s": rep.get("action_duration_s"),
               "store_files": rep.get("store_files")}
    print(_json(summary))
    return 0 if not rep.get("error") else 2


def cmd_load_image(a) -> int:
    from .load import resolve_image_digest
    print(resolve_image_digest(a.ref))
    return 0


def cmd_capacity(a) -> int:
    from .capacity import run
    rep = run(REPO, Path(a.stores), Path(a.out_root), a.minutes, a.image, a.replicas,
              a.exec_per_s, a.api_per_s, warmup_s=a.warmup, snapshot_every_s=a.snapshot_every,
              label=a.label, keep_load=a.keep_load)
    brief = {"out_dir": rep["out_dir"], "pass": rep["pass"],
             "criteria": {k: v["pass"] for k, v in rep["criteria"].items()},
             "store_growth_bytes_per_s": {k: round(v["bytes_per_s"]) for k, v in rep["store_growth"].items()},
             "peak_mem_used_fraction": rep["resources"].get("peak_mem_used_fraction"),
             "max_load1": rep["resources"].get("max_load1")}
    print(_json(brief))
    return 0 if rep["pass"] else 2


def cmd_capacity_reevaluate(a) -> int:
    from .capacity import reevaluate
    rep = reevaluate(Path(a.run_dir))
    print(_json({"written": rep["written"], "pass": rep["pass"],
                 "criteria": {k: v["pass"] for k, v in rep["criteria"].items()},
                 "completeness": rep["criteria"]["follower_complete_within_1pct"]["observed"],
                 "store_growth": {k: {"bytes_per_s": round(v["bytes_per_s"]), "new_files": v["new_inodes"]}
                                  for k, v in rep["store_growth"].items()}}))
    return 0 if rep["pass"] else 2


# ---------------------------------------------------------------------- G2/G3 qualification
def _evid(a) -> dict:
    e = Path(a.evidence).expanduser()
    q = e / "qualification"
    return {"root": e, "q": q, "runs": q / "runs", "scoring": q / "scoring", "ledger": e / "ledger" / "attempt_ledger.csv",
            "plan": q / "q_plan.json", "ops": q / "operating_points.json", "progress": q / "q_progress.jsonl"}


def _qual_files():
    import yaml
    qual = yaml.safe_load((REPO / "protocol" / "qualification.yaml").read_text())
    con = contract_mod.load_yaml(contract_mod.CONTRACT_PATH)
    prm = contract_mod.load_yaml(contract_mod.PARAMETERS_PATH)
    from .reconstruct import effective_contract
    eff = effective_contract(con, qual["fills"])
    eff["harness_identity"] = qual["harness_identity"]
    return qual, eff, prm


def cmd_ref_receiver(a) -> int:
    from .acquisition.reference import run_receiver
    host, port = a.listen.rsplit(":", 1)
    run_receiver(host, int(port), Path(a.out).expanduser())
    return 0


def cmd_reconstruct(a) -> int:
    from .reconstruct import reconstruct
    staged = Path(a.staged)
    con = json.loads((staged / "contract.json").read_text())
    fa = reconstruct(staged, con)
    warnings = fa.pop("_warnings")
    from .integrity import write_once
    write_once(Path(a.out), (json.dumps(fa, indent=2) + "\n").encode())
    if a.warnings:
        Path(a.warnings).parent.mkdir(parents=True, exist_ok=True)
        write_once(Path(a.warnings), "".join(json.dumps(w) + "\n" for w in warnings).encode())
    return 0


def cmd_load_map(a) -> int:
    from .orchestrator.qual import load_map
    stores = {"audit": Path(a.stores) / "audit", "tetragon": Path(a.stores) / "tetragon"}
    if len(a.exec_rates) != len(a.api_rates) or len(a.exec_rates) < 4:
        print("refusing: --exec-rates and --api-rates need the same length (>= 4 settings)")
        return 2
    settings = [{"exec_per_s": e, "api_per_s": p} for e, p in zip(a.exec_rates, a.api_rates)]
    res = load_map(REPO, stores, a.image, a.replicas, settings, measure_s=a.measure)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(_json(res))
    print(_json({"written": a.out, "fit": res["fit"]}))
    return 0


def cmd_operating_points(a) -> int:
    from .orchestrator.qual import operating_points
    lm = json.loads(Path(a.load_map).read_text())
    stores = {"audit": {"max_mb": a.audit_max_mb, "backups": a.audit_backups},
              "tetragon": {"max_mb": a.tetragon_max_mb, "backups": a.tetragon_backups}}
    _, _, prm = _qual_files()
    t = prm["timing"]
    ops = operating_points(lm["fit"], stores, t["delay_s"], a.action_s, a.exec_offset_s,
                           tolerance=prm["load"]["manipulation_tolerance"])
    ops.update(load_image=lm["image"], load_map=str(a.load_map), rationale=a.rationale)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(_json(ops))
    print(_json({"written": a.out, "replicas": ops["replicas"], "problems": ops["problems"],
                 "levels": {k: {"target": v["target_bytes_per_s"], "knob": v["knob"]} for k, v in ops["levels"].items()},
                 "p_loss_at_r_M_+-20pct": {s: [round(v["p_loss_r_M_at_minus_tol"], 2), round(v["p_loss_r_M_at_plus_tol"], 2)]
                                           for s, v in ops["per_store"].items()}}))
    return 0 if not ops["problems"] else 2


def cmd_qual_plan(a) -> int:
    from .orchestrator.qual import make_plan
    E = _evid(a)
    ops = json.loads(E["ops"].read_text())
    if E["plan"].exists():
        print(f"refusing: {E['plan']} exists (a plan is written once)")
        return 2
    plan = make_plan(a.seed, ops["levels"])
    plan["operating_points_sha256"] = __import__("hashlib").sha256(E["ops"].read_bytes()).hexdigest()
    from .integrity import write_once
    write_once(E["plan"], (_json(plan) + "\n").encode())
    print(_json({"written": str(E["plan"]), "parts": {k: len(v) for k, v in plan["parts"].items()}}))
    return 0


def cmd_qual_run(a) -> int:
    import hashlib
    from .orchestrator.platform import KindPlatform
    from .orchestrator.qual import run_part
    from .orchestrator.run import RunConfig
    E = _evid(a)
    qual, _, prm = _qual_files()
    plan = json.loads(E["plan"].read_text())
    if plan["operating_points_sha256"] != hashlib.sha256(E["ops"].read_bytes()).hexdigest():
        print("refusing: operating_points.json changed after the plan was written")
        return 2
    ops = json.loads(E["ops"].read_text())
    t = prm["timing"]
    E["runs"].mkdir(parents=True, exist_ok=True)
    cfg = RunConfig(out_root=E["runs"], ledger=E["ledger"], protocol_version=str(prm["protocol_version"]),
                    delay_s=t["delay_s"], grace_s=t["grace_s"], epsilon_s=t["epsilon_s"],
                    delay_tolerance_s=t["delay_tolerance_s"], settle_s=t["settle_s"], warmup_s=t["load_warmup_s"],
                    manipulation_window_s=t["manipulation_window_s"],
                    manipulation_tolerance=prm["load"]["manipulation_tolerance"],
                    namespace_delete_timeout_s=qual["namespace_delete_timeout_s"],
                    control_idle_s=qual["control_idle_s"])
    plat = KindPlatform(REPO, Path(a.stores).expanduser(), Path(a.harness_kubeconfig).expanduser(),
                        a.target_image, qual["exec_binary"], ops["load_image"], qual["readiness_timeout_s"],
                        audit_reference_file=Path(a.audit_reference).expanduser() if a.audit_reference else None,
                        tetragon_reference=not a.no_tetragon_reference, harness_identity=qual["harness_identity"])
    plat.code_commit()          # refuses a modified working tree before anything runs
    res = run_part(a.part, plan, ops, cfg, plat, REPO, E["progress"], block_warmup_s=prm["timing"]["load_warmup_block_start_s"])
    print(_json({k: v for k, v in res.items() if k != "runs"}))
    return 0 if res.get("complete") else 2


def cmd_qual_score(a) -> int:
    from .reconstruct.pipeline import score_package, variant_opts
    E = _evid(a)
    qual, eff, prm = _qual_files()
    variants = variant_opts(qual)
    primary = {"f3_actor_required": bool(qual["primary_variant"]["f3_actor_required"]),
               "f3_uid_required": bool(eff["facts"]["F3"]["audit"]["uid_required"])}
    if primary["f3_uid_required"] != bool(qual["primary_variant"]["f3_uid_required"]):
        print("refusing: primary_variant.f3_uid_required disagrees with fills F3.uid_required")
        return 2
    done = []
    for rec in (json.loads(l) for l in open(E["progress"])):
        if rec["disposition"] != "SCORED" or (E["scoring"] / rec["run_id"]).exists():
            continue
        r = score_package(E["runs"] / rec["run_id"], E["scoring"], eff, variants, primary,
                          epsilon_s=prm["timing"]["epsilon_s"], grace_s=prm["timing"]["grace_s"],
                          f5_tolerance_s=eff["facts"]["F5"]["tolerance_s"])
        done.append(r)
        print(_json(r))
    print(_json({"scored_now": len(done)}))
    return 0


def cmd_qual_report(a) -> int:
    import time
    from .orchestrator.qual_report import report
    E = _evid(a)
    ops = json.loads(E["ops"].read_text())
    rep = report(E["progress"], E["runs"], E["scoring"], ops)
    path = E["q"] / f"g2g3_report_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    path.write_text(_json(rep))
    brief = {"written": str(path), "by_part": rep["by_part"],
             "G2": {k: rep["G2"][k] for k in ("pass", "action_verified", "fact_correct_Q_a", "uid_present_Q_a",
                                                "tetragon_route", "timeouts_rule", "action_duration_s",
                                                "false_attribution")},
             "G3": {k: rep["G3"].get(k) for k in ("pass", "Q_b_compliant", "Q_c_direction_ok", "Q_c_within_band",
                                                    "reference_drop_free")},
             "leak_check": rep["leak_check"]["pass"]}
    print(_json(brief))
    return 0


# ---------------------------------------------------------------------- G4 pilot
def _pilot(a) -> dict:
    e = Path(a.evidence).expanduser()
    d = e / "qualification" / "pilot"
    return {"root": e, "dir": d, "runs": d / "runs", "scoring": d / "scoring", "sessions": d / "sessions",
            "schedule": d / "pilot_schedule.csv", "progress": d / "pilot_progress.jsonl",
            "ledger": e / "ledger" / "attempt_ledger.csv"}


def _read_schedule(path: Path) -> list[dict]:
    lines = [l for l in open(path) if not l.startswith("#")]
    return list(csv.DictReader(lines))


def cmd_pilot_plan(a) -> int:
    P = _pilot(a)
    if P["schedule"].exists():
        print(f"refusing: {P['schedule']} exists (written once)")
        return 2
    rows = make_schedule(a.seed, a.blocks, "PILOT-BLOCK")
    digest = write_schedule(rows, P["schedule"], a.seed)
    print(_json({"written": str(P["schedule"]), "sha256": digest, "blocks": a.blocks,
                 "order": {b: [r["cell"] for r in rows if r["block"] == b] for b in sorted({r["block"] for r in rows})}}))
    return 0


def cmd_pilot_run(a) -> int:
    from .fingerprint import collect
    from .orchestrator.block import BlockContext, close_session, open_session, run_cohort, run_config
    from .orchestrator.platform import KindPlatform
    P = _pilot(a)
    prm = contract_mod.load_yaml(contract_mod.PARAMETERS_PATH)
    con = contract_mod.load_yaml(contract_mod.CONTRACT_PATH)
    unresolved = [u for u in contract_mod.unresolved(con) + contract_mod.unresolved(prm) if "<G5>" not in u]
    if unresolved:
        print(_json({"refusing": "values not fixed before the pilot", "unresolved": unresolved}))
        return 2
    replica = Path(a.replica).expanduser()
    if not replica.is_dir():
        print(f"refusing: replica root {replica} is not a mounted directory")
        return 2
    rows = _read_schedule(P["schedule"])
    t = prm["timing"]
    plat = KindPlatform(REPO, Path(a.stores).expanduser(), Path(a.harness_kubeconfig).expanduser(),
                        prm["target_pod"]["image_digest"], prm["target_pod"]["exec_binary"],
                        prm["load"]["image_digest"], t["readiness_timeout_s"],
                        audit_reference_file=Path(a.audit_reference).expanduser() if a.audit_reference else None,
                        tetragon_reference=bool(prm["stores"]["reference_observer"]["tetragon_grpc"]),
                        harness_identity=con["harness_identity"], termination_timeout_s=t["termination_timeout_s"])
    plat.code_commit()                       # refuses a modified working tree
    P["runs"].mkdir(parents=True, exist_ok=True)
    cfg = run_config(prm, P["runs"], P["ledger"])
    sess = open_session(P["sessions"], plat, P["ledger"], collect(REPO), prm["host"]["fingerprint_stable_hash"],
                        [contract_mod.CONTRACT_PATH, contract_mod.PARAMETERS_PATH, P["schedule"]], "G4 pilot")
    ctx = BlockContext(prefix="PILOT", cohort="pilot", cfg=cfg, plat=plat, params=prm, session_dir=sess["dir"],
                       replica_runs=replica / "qualification" / "pilot" / "runs", progress=P["progress"])
    res = {"stopped": "not started"}
    try:
        res = run_cohort(rows, ctx, max_blocks=a.max_blocks)
    finally:
        close_session(sess["dir"], plat, P["ledger"], [Path(r["package"]) for r in ctx.runs])
    print(_json({"session": sess["session"], **res}))
    return 0 if not res.get("stopped") else 2


def cmd_pilot_score(a) -> int:
    from .reconstruct.pipeline import contract_opts, score_package
    P = _pilot(a)
    con = contract_mod.load_yaml(contract_mod.CONTRACT_PATH)
    prm = contract_mod.load_yaml(contract_mod.PARAMETERS_PATH)
    opts = contract_opts(con)
    n = 0
    for rec in (json.loads(l) for l in open(P["progress"])):
        if rec.get("event") != "RUN" or rec["disposition"] != "SCORED" or (P["scoring"] / rec["run_id"]).exists():
            continue
        r = score_package(Path(rec["package"]), P["scoring"], con, [opts], opts,
                          epsilon_s=prm["timing"]["epsilon_s"], grace_s=prm["timing"]["grace_s"],
                          f5_tolerance_s=con["facts"]["F5"]["tolerance_s"])
        print(_json({k: r[k] for k in ("run_id", "Y", "false_attribution") if k in r}))
        n += 1
    print(_json({"scored_now": n}))
    return 0


def cmd_pilot_report(a) -> int:
    import time
    from .orchestrator.pilot_report import report
    P = _pilot(a)
    n_blocks = len({r["block"] for r in _read_schedule(P["schedule"])})
    rep = report(P["progress"], P["runs"], P["scoring"], n_blocks)
    path = P["dir"] / f"g4_report_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    path.write_text(_json(rep))
    print(_json({"written": str(path), **rep}))
    return 0 if rep["pass"] else 2


# ---------------------------------------------------------------------- G5 freeze
PARAMS_FILE = REPO / "protocol" / "parameters.yaml"


def _set_param_placeholder(key: str, value) -> None:
    """Replace one '<G5>' placeholder line in parameters.yaml (text edit keeps comments)."""
    text = PARAMS_FILE.read_text()
    old = f'  {key}: "<G5>"'
    if text.count(old) != 1:
        raise ValueError(f"parameters.yaml has no open placeholder for {key}")
    PARAMS_FILE.write_text(text.replace(old, f"  {key}: {value}"))


def cmd_power_confirm(a) -> int:
    from .power import confirm
    spec = json.loads(Path(a.scenarios).read_text())
    sc = {s["name"]: Scenario(s["name"], s["p"]) for s in spec["scenarios"]}
    res = confirm(sc, a.b, a.seed, a.primary, a.conservative, a.calibration, a.nulls)
    Path(a.out).write_text(_json(res) + "\n")
    print(_json({"written": a.out, "B": a.b, "pass": res["pass"], "checks": res["checks"],
                 "power": {k: round(v["power"], 4) for k, v in res["rows"].items()}}))
    return 0 if res["pass"] else 2


def cmd_g5_set_blocks(a) -> int:
    conf = json.loads(Path(a.confirm).read_text())
    if not conf["pass"] or conf["B"] != a.n_blocks:
        print(f"refusing: confirmation at B={conf['B']} pass={conf['pass']} does not support n_blocks={a.n_blocks}")
        return 2
    if a.n_blocks > 24:
        print("refusing: B_max is 24 (Section 12.5)")
        return 2
    _set_param_placeholder("n_blocks", a.n_blocks)
    print(_json({"n_blocks": a.n_blocks}))
    return 0


def cmd_seed_record(a) -> int:
    import hashlib
    out = REPO / "protocol" / "master_seed.json"
    if out.exists():
        print("refusing: protocol/master_seed.json exists (written once)")
        return 2
    raw = Path(a.pulse_json).read_bytes()
    pulse = json.loads(raw)["pulse"]
    ov = pulse["outputValue"]
    from .freeze import seed_from_output_value
    seed = seed_from_output_value(ov)
    rec = {"rule": "seed = int(first 16 hex digits of NIST Beacon 2.0 pulse outputValue)",
           "source": "NIST Randomness Beacon 2.0", "pulse_uri": pulse.get("uri"),
           "pulse_timestamp": pulse.get("timeStamp"), "output_value": ov, "master_seed": seed,
           "pulse_json_sha256": hashlib.sha256(raw).hexdigest(), "pulse": pulse}
    out.write_text(json.dumps(rec, indent=2) + "\n")
    _set_param_placeholder("master_seed", seed)
    print(_json({k: rec[k] for k in ("pulse_uri", "pulse_timestamp", "master_seed")}))
    return 0


def cmd_freeze(a) -> int:
    from .freeze import write_freeze
    rec = write_freeze()
    print(_json({"written": "protocol/FREEZE.json", "base_commit": rec["base_commit"], "code": rec["code"]}))
    return 0


def cmd_register(a) -> int:
    from .freeze import write_registration
    rec = write_registration(REPO, a.venue, a.url, a.registered_at)
    print(_json(rec))
    return 0


def cmd_freeze_verify(a) -> int:
    from .freeze import check_ready_for_run_ids
    r = check_ready_for_run_ids()
    print(_json(r))
    return 0 if r["ok"] else 2


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="frx", description="FRX protocol tools (Blueprint v3.0)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("schedule", help="write the seeded block schedule (G5)")
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--blocks", type=int, required=True)
    s.add_argument("--prefix", default="BLOCK")
    s.add_argument("--out", default=str(REPO / "protocol" / "randomization_schedule.csv"))
    s.set_defaults(fn=cmd_schedule)

    s = sub.add_parser("power", help="Monte Carlo power grid (G5)")
    s.add_argument("--scenarios", required=True, help="JSON file with {scenarios:[{name,p:{LC,LD,MC,MD}}]}")
    s.add_argument("--b-values", type=int, nargs="+", default=[6, 8, 10, 12, 14, 16, 20, 24])
    s.add_argument("--sigmas", type=float, nargs="+", default=[0.0, 0.5, 1.0])
    s.add_argument("--n-sims", type=int, default=1000)
    s.add_argument("--n-draws", type=int, default=1000)
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--primary", help="scenario name used for the 0.80 rule")
    s.add_argument("--conservative", help="scenario name used for the 0.70 rule")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_power)

    s = sub.add_parser("analyze", help="primary/secondary analysis from a scores CSV (run_id,block,cell,Y)")
    s.add_argument("scores")
    s.add_argument("--draws", type=int, default=100_000)
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--out")
    s.set_defaults(fn=cmd_analyze)

    s = sub.add_parser("verify-package", help="per-run completeness gate")
    s.add_argument("package")
    s.set_defaults(fn=cmd_verify_package)

    s = sub.add_parser("verify-block", help="block replica gate")
    s.add_argument("--replica", required=True)
    s.add_argument("packages", nargs="+")
    s.set_defaults(fn=cmd_verify_block)

    s = sub.add_parser("freeze-check", help="list what still blocks freeze (placeholders, structure)")
    s.set_defaults(fn=cmd_freeze_check)

    s = sub.add_parser("headroom", help="operating-point arithmetic (Section 4.3)")
    s.add_argument("--m", type=float, required=True, help="rotation size M in bytes")
    s.add_argument("--k", type=int, required=True, help="retained backups k (>= 1)")
    s.add_argument("--elapsed", type=float, default=60.0, help="elapsed seconds (delta + action age)")
    s.add_argument("--h-low", type=float, default=3.0)
    s.add_argument("--p-mod", type=float, default=0.5)
    s.add_argument("--rate", type=float)
    s.set_defaults(fn=cmd_headroom)

    s = sub.add_parser("cluster-config", help="render kind config and Tetragon values (G1, G3)")
    s.add_argument("--stores", required=True, help="host directory holding stores/audit and stores/tetragon")
    s.add_argument("--out-dir", required=True)
    s.add_argument("--audit-max-mb", type=int, default=10)
    s.add_argument("--audit-backups", type=int, default=5)
    s.add_argument("--tetragon-max-mb", type=int, default=10)
    s.add_argument("--tetragon-backups", type=int, default=5)
    s.add_argument("--node-image", default=KIND_V0_33_DEFAULT_IMAGE)
    s.add_argument("--final", action="store_true", help="sizes fixed by the Section 4.3 rule (G3)")
    s.add_argument("--audit-webhook", help="reference receiver URL, e.g. http://172.18.0.1:9880 (G3)")
    s.set_defaults(fn=cmd_cluster_config)

    s = sub.add_parser("fingerprint", help="platform fingerprint JSON (G1 and every session open)")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_fingerprint)

    s = sub.add_parser("smoke", help="G1 smoke test: one create-exec-delete, then read both stores")
    s.add_argument("--stores", required=True)
    s.add_argument("--out-dir", required=True)
    s.add_argument("--image", required=True, help="target image (tag allowed at G1; digest required from G2)")
    s.add_argument("--exec-binary", default="/usr/bin/echo")
    s.add_argument("--settle", type=float, default=5.0)
    s.set_defaults(fn=cmd_smoke)

    s = sub.add_parser("load-image", help="pull an image tag and print its digest reference")
    s.add_argument("ref")
    s.set_defaults(fn=cmd_load_image)

    s = sub.add_parser("capacity", help="Section 14.2 capacity run (use scripts/g1_capacity_run.sh)")
    s.add_argument("--stores", required=True)
    s.add_argument("--out-root", required=True)
    s.add_argument("--minutes", type=float, default=20)
    s.add_argument("--image", required=True, help="load image pinned by digest")
    s.add_argument("--replicas", type=int, default=20)
    s.add_argument("--exec-per-s", type=float, default=10, help="per replica")
    s.add_argument("--api-per-s", type=float, default=10, help="per replica")
    s.add_argument("--warmup", type=int, default=60)
    s.add_argument("--snapshot-every", type=int, default=300)
    s.add_argument("--label", default="G1-provisional-ceiling")
    s.add_argument("--keep-load", action="store_true")
    s.set_defaults(fn=cmd_capacity)

    s = sub.add_parser("capacity-reevaluate", help="recompute a capacity verdict from preserved files")
    s.add_argument("run_dir")
    s.set_defaults(fn=cmd_capacity_reevaluate)

    def _ev(sp):
        sp.add_argument("--evidence", default="~/frx-evidence")

    s = sub.add_parser("ref-receiver", help="audit webhook reference receiver (long-running; start before the cluster)")
    s.add_argument("--listen", required=True, help="HOST:PORT, e.g. 172.18.0.1:9880 (kind network gateway)")
    s.add_argument("--out", default="~/frx-stores/reference/audit_webhook.jsonl")
    s.set_defaults(fn=cmd_ref_receiver)

    s = sub.add_parser("reconstruct", help="blinded reconstruction of one staged input (used by qual-score)")
    s.add_argument("staged")
    s.add_argument("--out", required=True)
    s.add_argument("--warnings")
    s.set_defaults(fn=cmd_reconstruct)

    s = sub.add_parser("load-map", help="store write rate vs load-generator rates (metadata only, G3 input)")
    s.add_argument("--stores", required=True)
    s.add_argument("--image", required=True, help="load image pinned by digest")
    s.add_argument("--replicas", type=int, default=10)
    # 2x2 factorial + centre: exec and API vary independently, so the fit can separate each
    # knob's effect on each store (paired settings that rise together would be collinear)
    s.add_argument("--exec-rates", type=float, nargs="+", default=[0, 10, 0, 10, 5], help="per replica")
    s.add_argument("--api-rates", type=float, nargs="+", default=[1, 1, 20, 20, 10], help="per replica")
    s.add_argument("--measure", type=int, default=60)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_load_map)

    s = sub.add_parser("operating-points", help="Section 4.3 rates from a load map and store sizes")
    s.add_argument("--load-map", required=True)
    s.add_argument("--audit-max-mb", type=int, required=True)
    s.add_argument("--audit-backups", type=int, required=True)
    s.add_argument("--tetragon-max-mb", type=int, required=True)
    s.add_argument("--tetragon-backups", type=int, required=True)
    s.add_argument("--action-s", type=float, default=8.0, help="ACTION_REQUEST to TERMINATION_CONFIRMED (s)")
    s.add_argument("--exec-offset-s", type=float, default=5.0, help="ACTION_REQUEST to exec (s)")
    s.add_argument("--rationale", required=True, help="cited reason for the store sizes (Section 4.3 item 1)")
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_operating_points)

    s = sub.add_parser("qual-plan", help="write the seeded Q plan once (needs operating_points.json)")
    s.add_argument("--seed", type=int, required=True)
    _ev(s)
    s.set_defaults(fn=cmd_qual_plan)

    s = sub.add_parser("qual-run", help="run or resume one Q part (use scripts/g2_qual_part.sh)")
    s.add_argument("--part", required=True, choices=["shakedown", "Q-a", "Q-b", "Q-c"])
    s.add_argument("--stores", required=True)
    s.add_argument("--harness-kubeconfig", required=True)
    s.add_argument("--target-image", required=True, help="target image pinned by digest")
    s.add_argument("--audit-reference", help="receiver file of the audit webhook reference observer")
    s.add_argument("--no-tetragon-reference", action="store_true")
    _ev(s)
    s.set_defaults(fn=cmd_qual_run)

    s = sub.add_parser("qual-score", help="blinded reconstruction and scoring of every unscored Q package")
    _ev(s)
    s.set_defaults(fn=cmd_qual_score)

    s = sub.add_parser("qual-report", help="G2 and G3 criteria from the Q packages and scores")
    _ev(s)
    s.set_defaults(fn=cmd_qual_report)

    s = sub.add_parser("pilot-plan", help="write the seeded pilot schedule once (G4)")
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--blocks", type=int, default=3)
    _ev(s)
    s.set_defaults(fn=cmd_pilot_plan)

    s = sub.add_parser("pilot-run", help="run pilot blocks in one session (use scripts/g4_pilot.sh)")
    s.add_argument("--stores", required=True)
    s.add_argument("--harness-kubeconfig", required=True)
    s.add_argument("--replica", required=True, help="mounted off-host replica root (Section 15.3)")
    s.add_argument("--audit-reference", help="receiver file of the audit webhook reference observer")
    s.add_argument("--max-blocks", type=int)
    _ev(s)
    s.set_defaults(fn=cmd_pilot_run)

    s = sub.add_parser("pilot-score", help="blinded reconstruction and scoring of pilot packages (contract only)")
    _ev(s)
    s.set_defaults(fn=cmd_pilot_score)

    s = sub.add_parser("pilot-report", help="G4 criteria and pilot estimates")
    _ev(s)
    s.set_defaults(fn=cmd_pilot_report)

    s = sub.add_parser("power-confirm", help="Section 12.5 confirmation stage at one B (G5)")
    s.add_argument("--scenarios", default=str(REPO / "protocol" / "power" / "scenarios.json"))
    s.add_argument("--b", type=int, required=True)
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--primary", default="pilot_informed")
    s.add_argument("--conservative", default="conservative")
    s.add_argument("--calibration", default="null_calibration")
    s.add_argument("--nulls", nargs="+", default=["null_calibration", "null"])
    s.add_argument("--out", default=str(REPO / "protocol" / "power" / "confirm.json"))
    s.set_defaults(fn=cmd_power_confirm)

    s = sub.add_parser("g5-set-blocks", help="write the confirmed B into parameters.yaml (G5)")
    s.add_argument("--n-blocks", type=int, required=True)
    s.add_argument("--confirm", default=str(REPO / "protocol" / "power" / "confirm.json"))
    s.set_defaults(fn=cmd_g5_set_blocks)

    s = sub.add_parser("seed-record", help="master seed from a NIST Beacon 2.0 pulse (G5; written once)")
    s.add_argument("--pulse-json", required=True)
    s.set_defaults(fn=cmd_seed_record)

    s = sub.add_parser("freeze", help="write protocol/FREEZE.json (G5; committed tree, no placeholders)")
    s.set_defaults(fn=cmd_freeze)

    s = sub.add_parser("register", help="record the registration of the frozen protocol (G5, before any RUN- ID)")
    s.add_argument("--venue", required=True)
    s.add_argument("--url", required=True)
    s.add_argument("--registered-at", required=True, help="timestamp shown by the registry")
    s.set_defaults(fn=cmd_register)

    s = sub.add_parser("freeze-verify", help="check the working tree against FREEZE.json and registration.json")
    s.set_defaults(fn=cmd_freeze_verify)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
