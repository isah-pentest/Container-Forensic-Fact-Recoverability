"""G7 tooling (Blueprint Sections 15.5-15.6, 18 G7): status, frozen-diff, re-scoring check and the
raw-to-fact audit sample. All read-only on the evidence store except where an output path is given."""
from __future__ import annotations

import fnmatch
import json
import math
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np

from ..freeze import CODE_DIRS, REPO_ROOT, protocol_file_list
from ..orchestrator.cohort import cohort_stop_checks
from ..reconstruct.pipeline import contract_opts, score_package
from .inputs import Evidence, read_jsonl

FROZEN_TAG = "protocol-v3.0.1-frozen"
# Changes allowed after execution: the analysis layer, its tests and the G7 notes and runbook.
ALLOWED_AFTER_FREEZE = ("src/frx/analysis/*", "tests/test_analysis_*.py", "protocol/gates/G7*.md",
                        "reproduce.sh", "README.md",
                        "protocol/registration.json")     # written after the freeze by design (it cites FREEZE.json)


# ---------------------------------------------------------------------- status (dispositions only)
def status(ev: Evidence) -> dict:
    coh = ev.confirmatory
    runs = coh.runs
    n_blocks = int(ev.params["design"]["n_blocks"])
    return {"blocks_closed": len(coh.blocks_closed), "blocks_planned": n_blocks,
            "complete": sum(1 for p in coh.blocks_closed if p.get("complete")),
            "replica_ok": sum(1 for p in coh.blocks_closed if p.get("replica_ok")),
            "incomplete": coh.incomplete, "attempts": len(runs),
            "dispositions": dict(Counter(r["disposition"] for r in runs)),
            "gates_ok": sum(bool(r["gate_ok"]) for r in runs),
            "failure_codes": dict(Counter(r["primary_failure_code"] for r in runs if r["primary_failure_code"])),
            "control_decoy_checks": sum(c["checked"] for c in ev.control_checks),
            "false_attributions": [c["run_id"] for c in ev.control_checks if c["false_attribution"]],
            "stop_rules": cohort_stop_checks(read_jsonl(coh.progress), n_blocks),
            "scores_locked": ev.lock_path.exists(),
            "calibration": {"attempts": len(ev.calibration.runs),
                            "dispositions": dict(Counter(r["disposition"] for r in ev.calibration.runs)),
                            "scored_dirs": sum(1 for r in ev.calibration.runs
                                               if (ev.calibration.scoring / r["run_id"]).exists())}}


# ---------------------------------------------------------------------- frozen diff
def frozen_diff(repo: Path = REPO_ROOT, tag: str = FROZEN_TAG) -> dict:
    g = ["git", "-c", "safe.directory=*", "-C", str(repo)]
    committed = subprocess.run(g + ["diff", "--name-status", tag, "HEAD"], capture_output=True, text=True,
                               check=True).stdout.split("\n")
    working = subprocess.run(g + ["status", "--porcelain", "--untracked-files=all"], capture_output=True,
                             text=True).stdout.split("\n")
    changed = sorted({l.split("\t")[-1] for l in committed if l.strip()} |
                     {l[3:].strip() for l in working if l.strip()})
    protocol = set(protocol_file_list(repo)) | {"protocol/FREEZE.json", "protocol/registration.json"}
    watched = [f for f in changed if f in protocol or f.split("/")[0] in CODE_DIRS]
    violations = [f for f in watched if not any(fnmatch.fnmatch(f, pat) for pat in ALLOWED_AFTER_FREEZE)]
    return {"tag": tag, "changed_since_freeze": changed, "violations": violations, "ok": not violations,
            "allowed": list(ALLOWED_AFTER_FREEZE)}


# ---------------------------------------------------------------------- re-scoring check
def rescore_check(ev: Evidence, limit: int | None = None) -> dict:
    """Re-run the frozen blinded reconstruction and scoring of every scored package into a scratch tree
    and require fact_answers.json and scores.json to be byte-identical to the locked ones."""
    opts = contract_opts(ev.contract)
    t = ev.params["timing"]
    problems, n = [], 0
    with tempfile.TemporaryDirectory() as td:
        for coh in (ev.confirmatory, ev.calibration):
            for r in [x for x in coh.runs if x["disposition"] == "SCORED"][:limit]:
                score_package(Path(r["package"]), Path(td) / coh.name, ev.contract, [opts], opts,
                              epsilon_s=t["epsilon_s"], grace_s=t["grace_s"],
                              f5_tolerance_s=ev.contract["facts"]["F5"]["tolerance_s"])
                for f in ("fact_answers.json", "scores.json"):
                    a = (coh.scoring / r["run_id"] / "assessment" / f).read_bytes()
                    b = (Path(td) / coh.name / r["run_id"] / "assessment" / f).read_bytes()
                    if a != b:
                        problems.append(f"{r['run_id']}: {f} differs")
                n += 1
    return {"ok": not problems, "rescored": n, "problems": problems}


# ---------------------------------------------------------------------- raw-to-fact audit sample
def audit_sample(ev: Evidence, fraction: float = 0.10) -> dict:
    """Seeded >= 10 % sample stratified by cell (incl. NC), with every matched record's raw line next to
    the sealed truth, for a human auditor (Section 15.6, G7)."""
    seed = int(ev.params["design"]["master_seed"]) + 7
    rng = np.random.Generator(np.random.PCG64(seed))
    coh = ev.confirmatory
    picked = []
    for cell in ("LC", "LD", "MC", "MD", "NC"):
        rs = sorted((r for r in coh.runs if r["disposition"] == "SCORED" and r["cell"] == cell), key=lambda r: r["run_id"])
        k = max(1, math.ceil(fraction * len(rs))) if rs else 0
        picked += [rs[i] for i in sorted(rng.choice(len(rs), size=k, replace=False))] if k else []
    items = []
    for r in picked:
        pkg = Path(r["package"])
        fa = coh.answers(r["run_id"])
        truth = json.loads((pkg / "sealed" / "truth" / "truth.json").read_text())
        st = coh.scores(r["run_id"])["primary"]
        for ident in fa["identities"]:
            sc = next(i for i in st["identities"] if i["role"] == ident["role"])
            for fact, by_src in ident["facts"].items():
                for src, answers in by_src.items():
                    for a in answers:
                        ev_ = a.get("evidence", {})
                        raw = _raw_line(pkg, src, ev_.get("file"), ev_.get("line"))
                        items.append({"run_id": r["run_id"], "cell": r["cell"], "role": ident["role"], "fact": fact,
                                      "source": src, "state": sc["facts"].get(fact, {}).get("states", {}).get(src),
                                      "answer": {k: v for k, v in a.items() if k != "evidence"},
                                      "truth": {k: truth.get(k) for k in ("pod", "uid", "executable", "arguments",
                                                                          "harness_identity")},
                                      "raw_file": ev_.get("file"), "raw_line": ev_.get("line"),
                                      "raw_record": raw, "auditor_verdict": "", "auditor_note": ""})
    return {"seed": seed, "fraction": fraction, "runs": [r["run_id"] for r in picked], "items": items}


def _raw_line(pkg: Path, src: str, fname: str | None, line: int | None) -> str | None:
    if not fname or not line:
        return None
    p = pkg / "acquired_raw" / src / fname
    with open(p, "rb") as fh:
        for i, l in enumerate(fh, 1):
            if i == line:
                return l.decode(errors="replace").strip()
    return None
