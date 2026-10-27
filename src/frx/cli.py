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
    out = Path(a.out_dir); out.mkdir(parents=True, exist_ok=True)
    for sub in ("audit", "tetragon"):
        (Path(a.stores) / sub).mkdir(parents=True, exist_ok=True)
    (out / "kind-config.yaml").write_text(render_kind_config(REPO, Path(a.stores), sizes, a.node_image))
    (out / "tetragon-values.yaml").write_text(render_tetragon_values(sizes))
    print(_json({"written": [str(out / "kind-config.yaml"), str(out / "tetragon-values.yaml")],
                 "sizes": sizes.__dict__, "node_image": a.node_image}))
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

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
