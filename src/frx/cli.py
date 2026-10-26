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

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
