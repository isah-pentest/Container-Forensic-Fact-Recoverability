"""Scoring-time pipeline for one closed package (Blueprint v3.0, Sections 7.6, 8.3, 15.2).

1. stage:        copy acquired_raw/ and write questions.json + contract.json into
                 scoring/<RUN>/input/ -- the only thing the reconstructor can read;
2. reconstruct:  in a separate, isolated Python process (`python -I -m frx.cli reconstruct`);
3. hash:         fact_answers.json is hashed into lineage.json before truth is opened;
4. score:        only now sealed/truth/truth.json is read; variants for G2 are scored side by side;
5. close:        manifest over scoring/<RUN>/ and read-only lock. The package is never reopened for writing.
"""
from __future__ import annotations

import itertools
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..acquisition.store import sha256_path
from ..integrity import lock_readonly, write_manifest, write_once
from . import reconstruct
from .score import score_run


def _dump(path: Path, obj) -> None:
    write_once(path, (json.dumps(obj, indent=2, default=str) + "\n").encode())


def run_window(pkg: Path, epsilon_s: float, grace_s: float) -> dict:
    """[ACTION_REQUEST - epsilon, TERMINATION_CONFIRMED + g] in wall-clock ns from the timing journal."""
    a = t = None
    for line in open(pkg / "timing" / "event_journal.jsonl"):
        e = json.loads(line)
        if e["state"] == "ACTION_REQUEST":
            a = e["wall_ns"]
        elif e["state"] == "TERMINATION_CONFIRMED":
            t = e.get("response_wall_ns") or e["wall_ns"]
    if a is None or t is None:
        raise ValueError(f"{pkg.name}: no ACTION_REQUEST/TERMINATION_CONFIRMED in journal")
    return {"start_wall_ns": a - int(epsilon_s * 1e9), "end_wall_ns": t + int(grace_s * 1e9),
            "epsilon_s": epsilon_s, "grace_s": grace_s}


def variant_opts(qual: dict) -> list[dict]:
    sv = qual.get("score_variants", {})
    keys = sorted(sv)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(sv[k] for k in keys))] or [{}]


def score_package(pkg: Path, scoring_root: Path, contract: dict, variants: list[dict], primary: dict,
                  epsilon_s: float = 5.0, grace_s: float = 10.0, f5_tolerance_s: float = 1.0,
                  in_process: bool = False) -> dict:
    pkg = Path(pkg)
    status = json.loads((pkg / "run_status.json").read_text())
    ident = json.loads((pkg / "identity.json").read_text())
    if status.get("disposition") != "SCORED":
        return {"run_id": pkg.name, "skipped": f"disposition {status.get('disposition')}"}
    out = Path(scoring_root) / pkg.name
    out.mkdir(parents=True, exist_ok=False)
    staged = out / "input"
    access = []

    def log(action, path, **kw):
        access.append({"action": action, "path": str(path), "wall_ns": time.time_ns(), **kw})

    # 1. stage
    for src in ("audit", "tetragon"):
        (staged / "acquired_raw" / src).mkdir(parents=True)
        for p in sorted((pkg / "acquired_raw" / src).glob("*.raw")):
            shutil.copy2(p, staged / "acquired_raw" / src / p.name)
            log("staged", f"acquired_raw/{src}/{p.name}", sha256=sha256_path(p))
    window = run_window(pkg, epsilon_s, grace_s)
    identities = [{"role": "target", "namespace": ident["namespace"], "pod": ident["pod"]}]
    if ident.get("decoy_identity"):
        identities.append({"role": "decoy", **{k: ident["decoy_identity"][k] for k in ("namespace", "pod")}})
    questions = {"window": window, "identities": identities,
                 "questions": {f: contract["facts"][f]["question"] for f in ("F1", "F2", "F3", "F4")}}
    _dump(staged / "questions.json", questions)
    _dump(staged / "contract.json", contract)
    log("not_staged", "sealed/ ground_truth/ acquisition/ timing/ load/ system/ console/",
        reason="firewall (Section 7.6): reconstruction sees acquired_raw, identity names and the window only")

    # 2. reconstruct in isolation
    answers_path = out / "assessment" / "fact_answers.json"
    answers_path.parent.mkdir(parents=True)
    t0 = time.time_ns()
    if in_process:
        fa = reconstruct(staged, contract)
        warnings = fa.pop("_warnings")
        _dump(answers_path, fa)
        proc = {"mode": "in_process"}
    else:
        r = subprocess.run([sys.executable, "-I", "-m", "frx.cli", "reconstruct", str(staged),
                            "--out", str(answers_path), "--warnings", str(out / "transforms" / "parse_warnings.jsonl")],
                           capture_output=True, text=True, timeout=1800, cwd=str(staged))
        proc = {"mode": "subprocess", "returncode": r.returncode, "stderr": r.stderr[-4000:]}
        if r.returncode:
            raise RuntimeError(f"reconstruction failed for {pkg.name}: {r.stderr[-2000:]}")
        fa = json.loads(answers_path.read_text())
        warnings = None
    t1 = time.time_ns()
    (out / "transforms").mkdir(exist_ok=True)
    if warnings is not None:
        write_once(out / "transforms" / "parse_warnings.jsonl",
                   "".join(json.dumps(w) + "\n" for w in warnings).encode())
    answers_sha = sha256_path(answers_path)
    _dump(out / "transforms" / "lineage.json", {
        "output": "assessment/fact_answers.json", "output_sha256": answers_sha,
        "inputs": [{"path": a["path"], "sha256": a["sha256"]} for a in access if a["action"] == "staged"],
        "component": "frx.reconstruct", "commit": status.get("code_commit"), "process": proc,
        "started_wall_ns": t0, "finished_wall_ns": t1})
    _dump(out / "assessment" / "reconstruction_input_manifest.json",
          {"staged": [a for a in access if a["action"] == "staged"], "questions_sha256": sha256_path(staged / "questions.json"),
           "contract_sha256": sha256_path(staged / "contract.json")})
    _dump(out / "transforms" / "attribution_flags.json",
          {"container_id_fallback_uses": sum(1 for i in fa["identities"] for a in i["facts"]["F2"]["tetragon"]
                                              if a.get("attribution") != "pod_metadata")})

    # 3-4. only now open the sealed truth
    truth_raw = json.loads((pkg / "sealed" / "truth" / "truth.json").read_text())
    log("unsealed", "sealed/truth/truth.json", after_answers_sha256=answers_sha)
    is_control = bool(ident.get("is_control"))
    truth = None if is_control else {"pod": truth_raw["pod"], "uid": truth_raw["uid"],
                                     "executable": truth_raw["executable"], "arguments": truth_raw["arguments"],
                                     "harness_identity": truth_raw["harness_identity"]}
    scored = {}
    for v in variants:
        key = ",".join(f"{k}={v[k]}" for k in sorted(v)) or "default"
        scored[key] = score_run(fa, truth, {**v, "f5_tolerance_s": f5_tolerance_s}, is_control)
    pkey = ",".join(f"{k}={primary[k]}" for k in sorted(primary)) or "default"
    primary_scores = scored.get(pkey) or score_run(fa, truth, {**primary, "f5_tolerance_s": f5_tolerance_s}, is_control)
    _dump(out / "assessment" / "scores.json", {"run_id": pkg.name, "primary_variant": pkey,
                                               "primary": primary_scores, "variants": scored})
    decoys = [i for i in primary_scores["identities"] if i["role"] == "decoy"]
    _dump(out / "assessment" / "decoy_check.json", {
        "decoy_identities": [{"namespace": d["namespace"], "pod": d["pod"],
                              "false_attribution": d.get("false_attribution")} for d in decoys],
        "control_run": is_control,
        "control_false_attribution": primary_scores["identities"][0].get("false_attribution") if is_control else None})
    write_once(out / "transforms" / "access_log.jsonl", "".join(json.dumps(a) + "\n" for a in access).encode())
    h = write_manifest(out)
    lock_readonly(out)
    return {"run_id": pkg.name, "scoring_dir": str(out), "manifest_hash": h, "Y": primary_scores.get("Y"),
            "false_attribution": primary_scores["false_attribution"]}
