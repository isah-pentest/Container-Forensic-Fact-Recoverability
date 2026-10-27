"""v3.0.1: confirmatory loop, blinded control check, cohort stop rules, scoring lock, calibration
schedule and the amendment chain (Sections 8.3, 10.4, 13, 16, 19)."""
import csv
import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from fakecluster import FakeCluster
from frx.contract import CONTRACT_PATH, load_yaml
from frx.freeze import PROTOCOL_FILES, amend_begin, check_ready_for_run_ids, write_freeze, write_registration
from frx.orchestrator.block import BlockContext, open_session
from frx.orchestrator.cohort import (calibration_schedule, cohort_stop_checks, control_check, run_confirmatory,
                                     score_cohort)
from frx.orchestrator.run import RunConfig
from frx.randomization import make_schedule

CON = load_yaml(CONTRACT_PATH)
RATE = {"audit": 20_000.0, "tetragon": 20_000.0}
PARAMS = {"load": {"replicas": 2, "rate_low_bytes_per_s": RATE, "rate_moderate_bytes_per_s": RATE,
                   "per_replica_exec_per_s": {"L": 1, "M": 2}, "per_replica_api_per_s": {"L": 1, "M": 2}},
          "timing": {"load_warmup_block_start_s": 0, "epsilon_s": 5.0, "grace_s": 0.5}}


@pytest.fixture
def env(tmp_path):
    fc = FakeCluster(tmp_path)
    cfg = RunConfig(out_root=tmp_path / "runs", ledger=tmp_path / "ledger.csv", delay_s=1.5, grace_s=0.5,
                    epsilon_s=5.0, delay_tolerance_s=1.0, settle_s=0.1, warmup_s=0.2, manipulation_window_s=2,
                    manipulation_tolerance=0.6, namespace_delete_timeout_s=5, control_idle_s=0.3,
                    mem_min_fraction=0.0, disk_min_bytes=0)
    (tmp_path / "runs").mkdir()
    sess = open_session(tmp_path / "sessions", fc, cfg.ledger, {"stable_hash": "h"}, "h", [], "test")
    ctx = BlockContext(prefix="RUN", cohort="confirmatory", cfg=cfg, plat=fc, params=PARAMS, session_dir=sess["dir"],
                       replica_runs=tmp_path / "replica" / "runs", progress=tmp_path / "progress.jsonl",
                       log=lambda *_: None)
    yield fc, ctx, tmp_path
    fc.stop()


def test_confirmatory_blocks_controls_score_lock(env):
    fc, ctx, tmp = env
    rows = make_schedule(11, 2, "BLOCK")
    res = run_confirmatory(rows, ctx, CON, PARAMS, tmp / "control.jsonl", max_blocks=1)
    assert res["stopped"] is None and res["remaining_blocks"] == ["BLOCK-02"]
    checks = [json.loads(l) for l in open(tmp / "control.jsonl")]
    assert len(checks) == 5 and not any(c["false_attribution"] for c in checks)
    assert sum(c["checked"] for c in checks) == 5               # NC target + 4 decoys, blinded
    with pytest.raises(RuntimeError, match="not closed"):        # no unsealing while blocks remain
        score_cohort(tmp / "progress.jsonl", rows, tmp / "scoring", CON, PARAMS, tmp / "analysis", tmp / "lock.json")
    assert not (tmp / "scoring").exists()
    res2 = run_confirmatory(rows, ctx, CON, PARAMS, tmp / "control.jsonl")
    assert res2["stopped"] is None and res2["remaining_blocks"] == []
    lock = score_cohort(tmp / "progress.jsonl", rows, tmp / "scoring", CON, PARAMS, tmp / "analysis", tmp / "lock.json")
    assert lock["n_scored"] == 10
    out = list(csv.DictReader(open(tmp / "analysis" / "scores.csv")))
    assert len(out) == 8 and all(r["Y"] == "1" and r["block_complete"] == "1" for r in out)
    assert Counter(r["cell"] for r in out) == {"LC": 2, "LD": 2, "MC": 2, "MD": 2}
    with pytest.raises(FileExistsError):
        score_cohort(tmp / "progress.jsonl", rows, tmp / "scoring", CON, PARAMS, tmp / "analysis", tmp / "lock.json")


def test_control_check_flags_planted_false_attribution(env):
    fc, ctx, tmp = env
    run_confirmatory(make_schedule(11, 1, "BLOCK"), ctx, CON, PARAMS, tmp / "control.jsonl")
    pos = next(r for r in ctx.runs if not r["is_control"])
    copy = tmp / "copy" / pos["run_id"]
    shutil.copytree(pos["package"], copy, copy_function=shutil.copyfile)
    ident = json.loads((copy / "identity.json").read_text())
    (copy / "identity.json").chmod(0o644)
    ident["decoy_identity"] = {"namespace": ident["namespace"], "pod": ident["pod"]}   # a pod that did act
    (copy / "identity.json").write_text(json.dumps(ident))
    assert control_check(copy, CON, 5.0, 0.5)["false_attribution"] is True


def test_cohort_stop_rules():
    run = lambda cell, disp, arm="C", code=None: {"event": "RUN", "cell": cell, "disposition": disp, "arm": arm,
                                                  "primary_failure_code": code}
    ok = [run("LC", "UNSCORABLE"), run("LC", "PRE_ACTION_ABORT")] + [run("LC", "SCORED")] * 16
    assert cohort_stop_checks(ok, 16) == []                      # 2/18 = 11 %
    bad = ok + [run("LC", "UNSCORABLE")]
    assert any("ceiling" in s for s in cohort_stop_checks(bad, 16))   # 3/19 = 15.8 %
    cf = [run("MC", "SCORED", "C", "CF-FOLLOW")] * 4
    assert any("imbalance" in s for s in cohort_stop_checks(cf, 16))


def test_calibration_schedule_is_seeded_and_balanced():
    a, b = calibration_schedule(864067897543811042), calibration_schedule(864067897543811042)
    assert a == b and len(a) == 16
    assert Counter(r["load"] for r in a) == {"CAL1": 4, "CAL2": 4, "CAL3": 4, "CAL4": 4}
    assert {r["arm"] for r in a} == {"D"} and [r["slot"] for r in a] == list(range(1, 17))


def test_amendment_chain(tmp_path):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    git("init", "-q"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    for f in PROTOCOL_FILES:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text(f"content of {f}\n")
    (tmp_path / "protocol" / "parameters.yaml").write_text('protocol_version: "3.0"\n')
    (tmp_path / "src").mkdir(); (tmp_path / "src" / "x.py").write_text("x = 1\n")
    git("add", "-A"); git("commit", "-qm", "base")
    write_freeze(tmp_path, check_readiness=False, run_tests=False)
    write_registration(tmp_path, "OSF", "https://osf.io/aaaaa", "2026-10-27T09:03:00Z")
    git("add", "-A"); git("commit", "-qm", "frozen v3.0")
    with pytest.raises(ValueError, match="protocol_version"):
        amend_begin(tmp_path)                                    # version must change first
    (tmp_path / "protocol" / "parameters.yaml").write_text('protocol_version: "3.0.1"\n')
    with pytest.raises(FileNotFoundError, match="AMENDMENT"):
        amend_begin(tmp_path)
    (tmp_path / "protocol" / "AMENDMENT_v3.0.1.md").write_text("reason\n")
    (tmp_path / "src" / "y.py").write_text("y = 1\n")
    moved = amend_begin(tmp_path)
    assert moved["superseded"] == "3.0" and not (tmp_path / "protocol" / "FREEZE.json").exists()
    git("add", "-A"); git("commit", "-qm", "amend")
    rec = write_freeze(tmp_path, check_readiness=False, run_tests=False)
    assert rec["protocol_version"] == "3.0.1" and rec["supersedes"]["version"] == "3.0"
    assert rec["supersedes"]["registration_url"] == "https://osf.io/aaaaa"
    assert "protocol/freeze_history/FREEZE_v3.0.json" in rec["protocol_files"]
    assert "protocol/AMENDMENT_v3.0.1.md" in rec["protocol_files"]
    assert not check_ready_for_run_ids(tmp_path)["ok"]           # v3.0.1 needs its own registration
    write_registration(tmp_path, "OSF", "https://osf.io/bbbbb", "2026-10-27T10:00:00Z")
    assert check_ready_for_run_ids(tmp_path)["ok"]
