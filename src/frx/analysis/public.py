"""Public-release tooling (G7 reporting): a synthetic example evidence store, verification of the
registered freeze from git objects, and a record of the analysis environment.

demo_evidence(root)   builds a small synthetic evidence store with the simulated cluster used by the
                      test suite (tests/fakecluster.py): 2 confirmatory blocks (10 attempts) and a
                      4-run calibration series, scored and locked exactly as in production. Its
                      numbers are meaningless; it exists so that every analysis command can be run
                      without the study's evidence store.
registered_freeze()   checks protocol/FREEZE.json at the frozen tag against the SHA-256 registered on
                      OSF (protocol/registration.json), every protocol file listed in it, and the
                      code digest, all read from git objects at that tag. Files listed in the manifest
                      but absent from the public history are reported as withheld, with their hash.
environment()         Python version, platform and every installed distribution with its version.
"""
from __future__ import annotations

import hashlib
import io
import json
import platform
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

from ..freeze import CODE_DIRS, REPO_ROOT

FROZEN_TAG = "protocol-v3.0.1-frozen"
TESTS_DIR = REPO_ROOT / "tests"

# simulated-cluster settings of the synthetic store (identical to the test-suite fixture)
RATE = {"audit": 20_000.0, "tetragon": 20_000.0}
PARAMS = {"load": {"replicas": 2, "rate_low_bytes_per_s": RATE, "rate_moderate_bytes_per_s": RATE,
                   "per_replica_exec_per_s": {"L": 1, "M": 2}, "per_replica_api_per_s": {"L": 1, "M": 2},
                   "calibration": {"CAL1": {"target_bytes_per_s": RATE, "exec_per_s": 1, "api_per_s": 1},
                                   "CAL4": {"target_bytes_per_s": RATE, "exec_per_s": 2, "api_per_s": 2}}},
          "timing": {"load_warmup_block_start_s": 0, "epsilon_s": 5.0, "grace_s": 0.5}}


def _cfg(root: Path, out: Path):
    from ..orchestrator.run import RunConfig
    out.mkdir(parents=True, exist_ok=True)
    return RunConfig(out_root=out, ledger=root / "ledger" / "attempt_ledger.csv", delay_s=1.5, grace_s=0.5,
                     epsilon_s=5.0, delay_tolerance_s=1.0, settle_s=0.1, warmup_s=0.2, manipulation_window_s=2,
                     manipulation_tolerance=0.6, namespace_delete_timeout_s=5, control_idle_s=0.3,
                     mem_min_fraction=0.0, disk_min_bytes=0)


def demo_evidence(root: Path) -> Path:
    """Build the synthetic evidence store under `root` (must be empty or absent)."""
    root = Path(root)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"{root} is not empty")
    if str(TESTS_DIR) not in sys.path:
        sys.path.insert(0, str(TESTS_DIR))
    from fakecluster import FakeCluster                      # tests/fakecluster.py (simulated cluster)
    from ..cli import main as frozen_cli
    from ..contract import CONTRACT_PATH, PARAMETERS_PATH, load_yaml
    from ..orchestrator.block import BlockContext, open_session, run_cohort
    from ..orchestrator.cohort import run_confirmatory, score_cohort
    from ..randomization import make_schedule
    from ..reconstruct.pipeline import contract_opts, score_package
    con, frozen = load_yaml(CONTRACT_PATH), load_yaml(PARAMETERS_PATH)
    (root / "ledger").mkdir(parents=True)
    fc = FakeCluster(root / "c")
    try:
        cfg = _cfg(root, root / "runs")
        sess = open_session(root / "sessions", fc, cfg.ledger, {"stable_hash": "h"}, "h", [], "synthetic")
        ctx = BlockContext(prefix="RUN", cohort="confirmatory", cfg=cfg, plat=fc, params=PARAMS,
                           session_dir=sess["dir"], replica_runs=root / "replica" / "runs",
                           progress=root / "sessions" / "confirmatory_progress.jsonl", log=lambda *_: None)
        rows = make_schedule(11, 2, "BLOCK")
        run_confirmatory(rows, ctx, con, PARAMS, root / "sessions" / "control_checks.jsonl")
    finally:
        fc.stop()
    score_cohort(root / "sessions" / "confirmatory_progress.jsonl", rows, root / "scoring", con, frozen,
                 root / "analysis_outputs", root / "scoring" / "scores_lock.json")
    seed = int(frozen["design"]["master_seed"])
    with redirect_stdout(io.StringIO()):
        frozen_cli(["analyze", str(root / "analysis_outputs" / "scores.csv"), "--seed", str(seed), "--draws", "2000",
                    "--out", str(root / "analysis_outputs" / "primary.json")])
    fc2 = FakeCluster(root / "k", max_bytes=15_000, backups=1)     # tiny stores, so delayed reads lose records
    try:
        cfg2 = _cfg(root, root / "calibration" / "runs")
        sess2 = open_session(root / "calibration" / "sessions", fc2, cfg2.ledger, {"stable_hash": "h"}, "h", [], "synthetic")
        cal_rows = [{"block": "CAL-BLOCK-01", "slot": i + 1, "cell": "CAL", "load": lvl, "arm": "D", "is_control": 0}
                    for i, lvl in enumerate(["CAL1", "CAL4", "CAL1", "CAL4"])]
        ctx2 = BlockContext(prefix="CAL", cohort="calibration", cfg=cfg2, plat=fc2, params=PARAMS,
                            session_dir=sess2["dir"], replica_runs=root / "replica" / "cal",
                            progress=root / "calibration" / "calibration_progress.jsonl", log=lambda *_: None)
        run_cohort(cal_rows, ctx2)
    finally:
        fc2.stop()
    opts = contract_opts(con)
    for r in ctx2.runs:
        if r["disposition"] == "SCORED":
            score_package(Path(r["package"]), root / "calibration" / "scoring", con, [opts], opts,
                          epsilon_s=frozen["timing"]["epsilon_s"], grace_s=frozen["timing"]["grace_s"],
                          f5_tolerance_s=con["facts"]["F5"]["tolerance_s"], in_process=True)
    (root / "SYNTHETIC_EVIDENCE.txt").write_text(
        "Synthetic evidence built by `python -m frx.analysis demo` with the simulated cluster of the test suite.\n"
        "It is not study data and its numbers have no meaning.\n")
    return root


def demo(out: Path, draws: int = 2000) -> dict:
    """Synthetic store, then every analysis on it, then the run-level rebuild check."""
    from .dataset import rebuild
    from .report import run_all
    out = Path(out).expanduser()
    ev = demo_evidence(out / "evidence")
    r = run_all(ev, out / "analysis_outputs", draws=draws, allow_dirty=True)
    o = Path(r["out"])
    rb = rebuild((o / "data" / "run_level.csv").read_text(), json.loads((o / "results.json").read_text()))
    return {"synthetic": True, "evidence": str(ev), "outputs": str(o), "files": r["files"],
            "verify_ok": r["verify_ok"], "primary_reproduced": r["primary_reproduced"],
            "rebuild_ok": rb["ok"], "rebuild_checks": len(rb["checks"])}


# ---------------------------------------------------------------------- registered freeze from git objects
def _git(repo: Path, *a: str, binary: bool = False):
    r = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(repo), *a], capture_output=True, check=False)
    return (r.stdout if binary else r.stdout.decode()) if r.returncode == 0 else None


def registered_freeze(repo: Path = REPO_ROOT, ref: str = FROZEN_TAG) -> dict:
    repo = Path(repo)
    commit = (_git(repo, "rev-list", "-n", "1", ref) or "").strip()
    if not commit:
        return {"ok": False, "problems": [f"ref {ref} not found"]}
    freeze_bytes = _git(repo, "show", f"{commit}:protocol/FREEZE.json", binary=True)
    if freeze_bytes is None:
        return {"ok": False, "problems": [f"protocol/FREEZE.json not in {ref}"]}
    rec = json.loads(freeze_bytes)
    reg_p = repo / "protocol" / "registration.json"
    reg = json.loads(reg_p.read_text()) if reg_p.exists() else {}
    problems, matched, withheld = [], [], []
    freeze_sha = hashlib.sha256(freeze_bytes).hexdigest()
    if reg.get("freeze_json_sha256") != freeze_sha:
        problems.append("FREEZE.json at the frozen tag does not match the registered SHA-256")
    for f, h in rec["protocol_files"].items():
        b = _git(repo, "show", f"{commit}:{f}", binary=True)
        if b is None:
            withheld.append({"file": f, "sha256": h})
        elif hashlib.sha256(b).hexdigest() == h:
            matched.append(f)
        else:
            problems.append(f"{f} differs from FREEZE.json")
    files = sorted((_git(repo, "ls-tree", "-r", "--name-only", commit, "--", *CODE_DIRS) or "").split())
    joined = "".join(f"{hashlib.sha256(_git(repo, 'show', f'{commit}:{f}', binary=True)).hexdigest()}  {f}\n"
                     for f in files)
    code = {"files": len(files), "sha256": hashlib.sha256(joined.encode()).hexdigest()}
    if code != {"files": rec["code"]["files"], "sha256": rec["code"]["sha256"]}:
        problems.append("code digest at the frozen tag differs from FREEZE.json")
    return {"ok": not problems, "ref": ref, "commit": commit, "freeze_json_sha256": freeze_sha,
            "registered_sha256": reg.get("freeze_json_sha256"), "registration_url": reg.get("url"),
            "registered_at": reg.get("registered_at"), "protocol_files_matched": len(matched),
            "protocol_files_withheld": withheld, "code_digest": code, "problems": problems}


# ---------------------------------------------------------------------- environment record
def environment() -> dict:
    from importlib.metadata import distributions
    dists = sorted({(d.metadata["Name"], d.version) for d in distributions() if d.metadata["Name"]},
                   key=lambda x: x[0].lower())
    return {"python": sys.version.split()[0], "implementation": platform.python_implementation(),
            "platform": platform.platform(), "machine": platform.machine(),
            "distributions": [f"{n}=={v}" for n, v in dists]}
