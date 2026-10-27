"""Blocks, sessions, replication and the G4 report on the simulated cluster (Sections 8, 10.4, 15, 18)."""
import json
from pathlib import Path

import pytest

from fakecluster import FakeCluster
from frx.contract import CONTRACT_PATH, load_yaml
from frx.orchestrator.block import BlockContext, close_session, open_session, run_cohort
from frx.orchestrator.pilot_report import report
from frx.orchestrator.run import RunConfig
from frx.randomization import make_schedule
from frx.reconstruct.pipeline import contract_opts, score_package

CON = load_yaml(CONTRACT_PATH)
RATE = {"audit": 20_000.0, "tetragon": 20_000.0}
PARAMS = {"load": {"replicas": 2, "rate_low_bytes_per_s": RATE, "rate_moderate_bytes_per_s": RATE,
                   "per_replica_exec_per_s": {"L": 1, "M": 2}, "per_replica_api_per_s": {"L": 1, "M": 2}},
          "timing": {"load_warmup_block_start_s": 0}}


@pytest.fixture
def env(tmp_path):
    fc = FakeCluster(tmp_path)
    cfg = RunConfig(out_root=tmp_path / "runs", ledger=tmp_path / "ledger.csv", delay_s=1.5, grace_s=0.5,
                    epsilon_s=5.0, delay_tolerance_s=1.0, settle_s=0.1, warmup_s=0.2, manipulation_window_s=2,
                    manipulation_tolerance=0.6, namespace_delete_timeout_s=5, control_idle_s=0.3,
                    mem_min_fraction=0.0, disk_min_bytes=0)
    (tmp_path / "runs").mkdir()
    yield fc, cfg, tmp_path
    fc.stop()


def _ctx(fc, cfg, tmp, sess):
    return BlockContext(prefix="PILOT", cohort="pilot", cfg=cfg, plat=fc, params=PARAMS, session_dir=sess["dir"],
                        replica_runs=tmp / "replica" / "runs", progress=tmp / "progress.jsonl", log=lambda *_: None)


def test_pilot_block_end_to_end(env):
    fc, cfg, tmp = env
    rows = make_schedule(7, 1, "PILOT-BLOCK")
    sess = open_session(tmp / "sessions", fc, cfg.ledger, {"stable_hash": "h"}, "h", [CONTRACT_PATH], "test")
    ctx = _ctx(fc, cfg, tmp, sess)
    res = run_cohort(rows, ctx)
    close = close_session(sess["dir"], fc, cfg.ledger, [Path(r["package"]) for r in ctx.runs])
    assert res["stopped"] is None and len(res["blocks"]) == 1 and res["blocks"][0]["complete"]
    assert [r["cell"] for r in ctx.runs] == [r["cell"] for r in rows]
    assert all(r["disposition"] == "SCORED" and r["gate_ok"] for r in ctx.runs)
    assert fc.load_deployments == 1                                   # one Deployment per block
    rec = json.loads((sess["dir"] / "blocks" / "PILOT-BLOCK-01" / "replica_receipt.json").read_text())
    assert rec["ok"] and len(rec["packages"]) == 5
    assert close["hash_problems"] == {}
    opts = contract_opts(CON)
    assert opts == {"f3_actor_required": True, "f3_uid_required": True}
    for r in ctx.runs:
        score_package(Path(r["package"]), tmp / "scoring", CON, [opts], opts, in_process=True)
    rep = report(tmp / "progress.jsonl", tmp / "runs", tmp / "scoring", 1)
    assert rep["pass"], rep
    assert rep["false_attribution"] == {"runs": [], "checks": 5, "pass": True}   # NC + 4 decoys
    assert all(v["text"] == "1/1" for v in rep["cell_Y"].values())
    # a second invocation does not resume or re-run a closed block
    assert run_cohort(rows, ctx)["blocks"] == []


def test_unscorable_attempt_is_replaced_at_block_end(env):
    fc, cfg, tmp = env
    rows = make_schedule(7, 1, "PILOT-BLOCK")
    first_positive = next(r for r in rows if r["cell"] != "NC")
    fc.fail_exec_n = 1
    sess = open_session(tmp / "sessions", fc, cfg.ledger, {"stable_hash": "h"}, None, [], "test")
    ctx = _ctx(fc, cfg, tmp, sess)
    res = run_cohort(rows, ctx)
    assert res["stopped"] is None
    assert len(ctx.runs) == 6
    failed = next(r for r in ctx.runs if r["disposition"] == "UNSCORABLE")
    assert failed["cell"] == first_positive["cell"] and failed["primary_failure_code"] == "UN-EXEC"
    last = ctx.runs[-1]
    assert last["cell"] == failed["cell"] and last["replaces_run_id"] == failed["run_id"] and last["disposition"] == "SCORED"


def test_ceiling_stops_block(env):
    fc, cfg, tmp = env
    rows = make_schedule(7, 2, "PILOT-BLOCK")
    fc.fail_exec = True
    sess = open_session(tmp / "sessions", fc, cfg.ledger, {"stable_hash": "h"}, None, [], "test")
    res = run_cohort(rows, _ctx(fc, cfg, tmp, sess))
    assert res["stopped"].startswith("ceiling") and len(res["blocks"]) == 1   # second block never starts


def test_fingerprint_change_refuses_session(env):
    fc, cfg, tmp = env
    with pytest.raises(RuntimeError, match="fingerprint changed"):
        open_session(tmp / "sessions", fc, cfg.ledger, {"stable_hash": "new"}, "old", [], "test")
