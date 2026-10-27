"""Q-series driver, scoring and G2/G3 report on the simulated cluster (Section 18)."""
import json
from pathlib import Path

import yaml

from fakecluster import FakeCluster
from frx.contract import CONTRACT_PATH, load_yaml
from frx.orchestrator import qual as Q
from frx.orchestrator.qual_report import report
from frx.reconstruct import effective_contract
from frx.reconstruct.pipeline import score_package, variant_opts
from frx.status import REPO_ROOT
from test_orchestrator_e2e import cfg

QUAL = yaml.safe_load((REPO_ROOT / "protocol" / "qualification.yaml").read_text())
CON = effective_contract(load_yaml(CONTRACT_PATH), QUAL["fills"])
CON["harness_identity"] = QUAL["harness_identity"]


def test_series_runs_scores_and_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(Q.loadctl, "deploy", lambda *a, **k: {"rollout": "ok"})
    monkeypatch.setattr(Q.loadctl, "delete", lambda *a, **k: {"rc": 0})
    fc = FakeCluster(tmp_path)
    try:
        c = cfg(tmp_path)
        rates = {"audit": 20_000, "tetragon": 20_000}
        ops = {"replicas": 1, "per_store": {"audit": {"M_bytes": 200_000, "k": 2}},
               "levels": {l: {"knob": {"exec_per_s": 1, "api_per_s": 1}, "target_bytes_per_s": rates}
                          for l in ("L", "QC1", "QC2", "QC3")}}
        plan = {"parts": {"Q-a": [{"arm": "C", "level": "L"}] * 2, "Q-b": [{"arm": "D", "level": "L"}] * 2,
                          "Q-c": [{"arm": "D", "level": "QC1"}],
                          "shakedown": [{"arm": "C", "level": "L", "is_control": True}]}}
        prog = tmp_path / "progress.jsonl"
        orig = Q.execute_run

        def flaky(spec, cfg_, plat):
            r = orig(spec, cfg_, plat)
            fc.fail_exec = False
            return r
        monkeypatch.setattr(Q, "execute_run", flaky)
        for part in ("shakedown", "Q-a", "Q-b", "Q-c"):
            fc.fail_exec = part == "Q-a"         # first Q-a attempt fails -> replaced at the end of the part
            res = Q.run_part(part, plan, ops, c, fc, REPO_ROOT, prog, block_warmup_s=0, log=lambda *_: None)
            assert res["complete"], res
        recs = [json.loads(l) for l in open(prog)]
        qa = [r for r in recs if r["part"] == "Q-a"]
        assert [r["disposition"] for r in qa] == ["UNSCORABLE", "SCORED", "SCORED"]
        assert qa[2]["replaces_run_id"] == qa[0]["run_id"] and qa[2]["slot"] == 0
        # resuming a finished part does nothing
        assert Q.run_part("Q-a", plan, ops, c, fc, REPO_ROOT, prog, block_warmup_s=0)["runs"] == []
        variants = variant_opts(QUAL)
        for r in recs:
            if r["disposition"] == "SCORED":
                score_package(Path(c.out_root) / r["run_id"], tmp_path / "scoring", CON, variants,
                              {"f3_actor_required": True, "f3_uid_required": False}, in_process=True)
        rep = report(prog, Path(c.out_root), tmp_path / "scoring", ops)
        assert rep["G2"]["fact_correct_Q_a"]["F1-audit"]["text"] == "2/2"
        assert rep["G2"]["fact_correct_Q_a"]["F2-tetragon"]["text"] == "2/2"
        assert rep["G2"]["uid_present_Q_a"]["F3"]["text"] == "2/2"
        assert rep["G2"]["false_attribution"]["pass"] and rep["G2"]["false_attribution"]["checks"] >= 4
        assert rep["G2"]["action_verified"]["text"] == "4/5"           # the failed exec counts against it
        assert rep["G2"]["timeouts_rule"]["readiness_timeout_s"] == 1
        assert rep["G3"]["Q_b_compliant"]["text"] == "2/2"
        assert rep["leak_check"]["pass"]
        assert rep["G3"]["reference_drop_free"]["use_reference"]
    finally:
        fc.stop()
