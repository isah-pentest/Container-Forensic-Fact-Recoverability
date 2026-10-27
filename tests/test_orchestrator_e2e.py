"""End-to-end: execute_run -> package -> gate -> ledger -> blinded reconstruction -> scoring,
on the simulated cluster in fakecluster.py (Sections 9, 10, 15, 7.5, 8.3)."""
import json
from pathlib import Path

import pytest

from fakecluster import FakeCluster
from frx.contract import CONTRACT_PATH, load_yaml
from frx.orchestrator.package import next_run_id
from frx.orchestrator.run import RunConfig, RunSpec, execute_run
from frx.reconstruct import effective_contract
from frx.reconstruct.pipeline import score_package
from frx.status import REPO_ROOT

import yaml

QUAL = yaml.safe_load((REPO_ROOT / "protocol" / "qualification.yaml").read_text())
CON = effective_contract(load_yaml(CONTRACT_PATH), QUAL["fills"])
CON["harness_identity"] = QUAL["harness_identity"]
VARIANTS = [{"f3_actor_required": a, "f3_uid_required": u} for a in (False, True) for u in (False, True)]
PRIMARY = {"f3_actor_required": True, "f3_uid_required": False}


def cfg(tmp, **kw):
    base = dict(out_root=tmp / "runs", ledger=tmp / "ledger.csv", delay_s=2.0, grace_s=0.5, epsilon_s=5.0,
                delay_tolerance_s=1.0, settle_s=0.1, warmup_s=0.2, manipulation_window_s=2,
                manipulation_tolerance=0.6, namespace_delete_timeout_s=5, control_idle_s=0.3,
                mem_min_fraction=0.0, disk_min_bytes=0)
    base.update(kw)
    (tmp / "runs").mkdir(exist_ok=True)
    return RunConfig(**base)


def spec(c, arm, tmp, rates, **kw):
    return RunSpec(run_id=next_run_id("QUAL", c.ledger, c.out_root), cohort="qualification", part="test",
                   cell=kw.pop("cell", "Q"), arm=arm, load_level="L", knob={"exec_per_s": 1, "api_per_s": 1},
                   target_rates=rates, **kw)


@pytest.fixture
def cluster(tmp_path):
    fc = FakeCluster(tmp_path)
    yield fc
    fc.stop()


def _score(tmp, r):
    return score_package(Path(r["package"]), tmp / "scoring", CON, VARIANTS, PRIMARY, in_process=True)


def test_c_and_d_runs_recover_everything_at_low_load(tmp_path, cluster):
    c = cfg(tmp_path)
    rates = {"audit": 20_000, "tetragon": 20_000}
    rc = execute_run(spec(c, "C", tmp_path, rates), c, cluster)
    assert rc["gate"]["ok"], rc["gate"]["problems"]
    assert rc["status"]["disposition"] == "SCORED" and rc["status"]["flags"]["collector_complete"]
    rd = execute_run(spec(c, "D", tmp_path, rates, prev_identity=rc["identity"]), c, cluster)
    assert rd["gate"]["ok"], rd["gate"]["problems"]
    assert abs(rd["status"]["delay_observed_s"] - 2.0) < 1.0
    for r in (rc, rd):
        s = _score(tmp_path, r)
        assert s["Y"] == 1 and not s["false_attribution"]
        scores = json.loads((tmp_path / "scoring" / r["run_id"] / "assessment" / "scores.json").read_text())
        tgt = scores["primary"]["identities"][0]
        assert tgt["facts"]["F2"]["states"] == {"audit": "CORRECT", "tetragon": "CORRECT"}
        assert tgt["facts"]["F4"]["union"] == "CORRECT" and tgt["facts"]["F6"]["union"] == "CORRECT"
        assert tgt["facts"]["F5"]["state"] == "CORRECT"
        for key, v in scores["variants"].items():   # every F3 variant is CORRECT with the harness record
            assert v["identities"][0]["facts"]["F3"]["union"] == "CORRECT", key
    # acquired files carry neutral content-hash names, one per store in both arms
    for r in (rc, rd):
        for src in ("audit", "tetragon"):
            names = [p.name for p in (Path(r["package"]) / "acquired_raw" / src).iterdir()]
            assert len(names) == 1 and len(names[0]) == 64 + 4
    # decoy (previous run's identity) never matches inside the current window
    dc = json.loads((tmp_path / "scoring" / rd["run_id"] / "assessment" / "decoy_check.json").read_text())
    assert dc["decoy_identities"] and not dc["decoy_identities"][0]["false_attribution"]
    # firewall: only acquired_raw was staged before truth was unsealed
    log = [json.loads(l) for l in open(tmp_path / "scoring" / rd["run_id"] / "transforms" / "access_log.jsonl")]
    staged = [e for e in log if e["action"] == "staged"]
    assert staged and all(e["path"].startswith("acquired_raw/") for e in staged)
    assert [e["action"] for e in log][-1] == "unsealed"


def test_delayed_arm_loses_oldest_fact_when_store_overflows(tmp_path):
    # files must live longer than the 1 Hz growth sampling (M / r > 1 s), as on the real stores
    fc = FakeCluster(tmp_path, audit_rate=150_000, tet_rate=150_000, max_bytes=200_000, backups=1)
    try:
        c = cfg(tmp_path, delay_s=3.0)
        r = execute_run(spec(c, "D", tmp_path, {"audit": 150_000, "tetragon": 150_000}), c, fc)
        assert r["gate"]["ok"], r["gate"]["problems"]
        assert r["status"]["disposition"] == "SCORED"
        s = _score(tmp_path, r)
        assert s["Y"] == 0                       # create record evicted before the delayed read
        sc = json.loads((tmp_path / "scoring" / r["run_id"] / "assessment" / "scores.json").read_text())
        assert sc["primary"]["identities"][0]["facts"]["F1"]["states"]["audit"] == "ABSTAIN"
        # the continuous arm under the same load keeps everything
        r2 = execute_run(spec(c, "C", tmp_path, {"audit": 150_000, "tetragon": 150_000}), c, fc)
        assert r2["gate"]["ok"] and r2["status"]["disposition"] == "SCORED", r2["status"]
        assert _score(tmp_path, r2)["Y"] == 1
    finally:
        fc.stop()


def test_negative_control_has_no_false_attribution(tmp_path, cluster):
    c = cfg(tmp_path)
    r = execute_run(spec(c, "C", tmp_path, {"audit": 20_000, "tetragon": 20_000}, cell="NC", is_control=True), c, cluster)
    assert r["gate"]["ok"], r["gate"]["problems"]
    s = _score(tmp_path, r)
    assert s["false_attribution"] is False and s["Y"] is None


def test_load_check_failure_is_pre_action_abort(tmp_path, cluster):
    c = cfg(tmp_path, manipulation_tolerance=0.2)
    r = execute_run(spec(c, "D", tmp_path, {"audit": 500_000, "tetragon": 20_000}), c, cluster)
    assert r["status"]["disposition"] == "PRE_ACTION_ABORT"
    assert r["status"]["primary_failure_code"] == "PA-LOAD"
    assert r["gate"]["ok"], r["gate"]["problems"]
    assert not (Path(r["package"]) / "ground_truth" / "exec_result.json").exists()


def test_exec_failure_is_unscorable_and_packaged(tmp_path, cluster):
    cluster.fail_exec = True
    c = cfg(tmp_path)
    r = execute_run(spec(c, "C", tmp_path, {"audit": 20_000, "tetragon": 20_000}), c, cluster)
    assert r["status"]["disposition"] == "UNSCORABLE" and r["status"]["primary_failure_code"] == "UN-EXEC"
    assert r["gate"]["ok"], r["gate"]["problems"]
    assert _score(tmp_path, r).get("skipped")


def test_run_ids_are_never_reused(tmp_path, cluster):
    c = cfg(tmp_path)
    ids = [execute_run(spec(c, "C", tmp_path, {"audit": 20_000, "tetragon": 20_000}), c, cluster)["run_id"]
           for _ in range(2)]
    assert ids == ["QUAL-0001", "QUAL-0002"]
    ledger = (tmp_path / "ledger.csv").read_text().splitlines()
    assert len(ledger) == 3 and ledger[1].startswith("QUAL-0001")
