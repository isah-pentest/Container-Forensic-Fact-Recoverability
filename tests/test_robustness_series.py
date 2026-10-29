"""Series R (k = 2 robustness extension) on synthetic fixtures: design arithmetic, schedule, freeze
gate, and the full run -> score -> analysis chain through the frozen functions with a simulated
cluster whose stores keep two backups (synthetic only; never study data)."""
import json
import subprocess

import pytest

from fakecluster import FakeCluster
from frx.analysis.public import PARAMS, RATE, _cfg
from frx.contract import CONTRACT_PATH, PARAMETERS_PATH, load_yaml
from frx.orchestrator.block import BlockContext, open_session, run_cohort
from frx.orchestrator.cohort import score_cohort
from frx.robustness import design
from frx.robustness import freeze as rfreeze
from frx.robustness.analysis import results, run_all
from frx.robustness.series import store_check

MIB = 1024 * 1024
FIT = {"audit": {"intercept_bytes_per_s": 3000.0, "bytes_per_unit": 1347.0},
       "tetragon": {"intercept_bytes_per_s": 1500.0, "bytes_per_unit": 4708.0}}


def test_levels_follow_the_model():
    prm = load_yaml(PARAMETERS_PATH)
    lv = design.levels(FIT, prm)
    assert not lv["problems"], lv["problems"]
    L = lv["levels"]
    m = 10 * MIB
    assert L["R1"]["target_bytes_per_s"] == {"audit": 231303.5, "tetragon": 249661.0}      # the frozen MD rate
    assert L["R3"]["target_bytes_per_s"]["audit"] == pytest.approx(2.5 * m / 68, abs=0.1)  # H = 1 at k = 2
    p1 = L["R1"]["predicted"]["F1-audit"]
    assert p1["p_loss_formula"] == 0.0 and p1["H_worst_offset"] > 1 and p1["p_loss_formula_k1"] > 0
    assert L["R3"]["predicted"]["F1-audit"]["p_loss_formula"] == pytest.approx(0.5, abs=1e-3)
    assert L["R4"]["predicted"]["F1-audit"]["H_expected_offset"] == pytest.approx(0.8, abs=1e-3)
    for d in L.values():
        assert d["knob"]["api_per_s"] <= 20 and d["knob"]["exec_per_s"] <= 10


def test_series_params_satisfy_the_frozen_run_status_schema(tmp_path):
    import re
    from frx.orchestrator.block import level_settings, run_config
    frozen = load_yaml(PARAMETERS_PATH)
    lv = design.levels(FIT, frozen)
    plan = {"k": 2, "levels": lv["levels"], "fingerprint_stable_hash": "abc"}
    prm = design.series_params(frozen, plan)
    schema = json.loads((PARAMETERS_PATH.parent / "schemas" / "run_status.schema.json").read_text())["properties"]
    assert re.match(schema["protocol_version"]["pattern"], str(prm["protocol_version"]))
    assert re.match(schema["run_id"]["pattern"], f"{design.PREFIX}-0017")
    assert design.COHORT in schema["cohort"]["enum"] and design.CELL in schema["cell"]["enum"]
    cfg = run_config(prm, tmp_path / "runs", tmp_path / "ledger.csv")
    assert cfg.protocol_version == frozen["protocol_version"] and cfg.delay_s == frozen["timing"]["delay_s"]
    knob, tgt = level_settings(prm, "R4")
    assert knob == lv["levels"]["R4"]["knob"] and tgt == lv["levels"]["R4"]["target_bytes_per_s"]
    assert prm["stores"]["audit"]["max_backups"] == 2 and frozen["stores"]["audit"]["max_backups"] == 1   # copy only
    assert prm["host"]["fingerprint_stable_hash"] == "abc"


def test_schedule_is_seeded_and_balanced():
    a, b = design.schedule(123), design.schedule(123)
    assert a == b and len(a) == 16 and a != design.schedule(124)
    assert all(sum(r["load"] == lvl for r in a) == 4 for lvl in design.LEVELS)
    assert {r["arm"] for r in a} == {"D"} and {r["block"] for r in a} == {design.BLOCK}


def test_freeze_gate(tmp_path):
    def git(*x):
        subprocess.run(["git", "-C", str(tmp_path), *x], check=True, capture_output=True)
    git("init", "-q"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    d = tmp_path / rfreeze.DIR
    d.mkdir(parents=True)
    for f in rfreeze.FILES:
        (d / f).write_text(f"{f}\n")
    (tmp_path / "src").mkdir(); (tmp_path / "src" / "x.py").write_text("x = 1\n")
    git("add", "-A"); git("commit", "-qm", "base")
    assert not rfreeze.check(tmp_path)["ok"]
    rfreeze.write_freeze(tmp_path)
    with pytest.raises(FileExistsError):
        rfreeze.write_freeze(tmp_path)
    r = rfreeze.check(tmp_path)
    assert not r["ok"] and any("registration_R" in p for p in r["problems"])
    rfreeze.write_registration("https://osf.io/xxxxx", "2026-10-29T12:00:00Z", tmp_path)
    assert rfreeze.check(tmp_path)["ok"]
    (d / "plan.json").write_text("changed\n")
    assert any("plan.json changed" in p for p in rfreeze.check(tmp_path)["problems"])
    (d / "plan.json").write_text("plan.json\n")
    (tmp_path / "src" / "x.py").write_text("x = 2\n")
    assert any("tracked code" in p for p in rfreeze.check(tmp_path)["problems"])


def test_series_chain_on_simulated_k2_cluster(tmp_path):
    con, frozen = load_yaml(CONTRACT_PATH), load_yaml(PARAMETERS_PATH)
    m, k = 15_000, 2
    plan = {"k": k, "M_mib": 10, "M_bytes": m, "runs_per_level": 1, "fingerprint_stable_hash": "h",
            "levels": {lvl: {"target_bytes_per_s": RATE, "knob": {"exec_per_s": e, "api_per_s": e},
                             "predicted": {"F1-audit": {"p_loss_formula": 0.5, "H_expected_offset": 1.0},
                                           "F2-tetragon": {"p_loss_formula": 0.5, "H_expected_offset": 1.0}}}
                       for lvl, e in (("R1", 1), ("R2", 1), ("R3", 2), ("R4", 2))}}
    prm = design.series_params(frozen, plan)
    sim = {**PARAMS, "load": {**PARAMS["load"], "calibration": prm["load"]["calibration"]}}
    root = tmp_path / "evidence"
    (root / "ledger").mkdir(parents=True)
    (root / "ledger" / "attempt_ledger.csv").write_text("run_id\nCAL-0016\n")   # the shared ledger after calibration
    fc = FakeCluster(tmp_path / "c", max_bytes=m, backups=k)
    try:
        cfg = _cfg(root, root / "robustness" / "runs")
        sess = open_session(root / "robustness" / "sessions", fc, cfg.ledger, {"stable_hash": "h"}, "h", [], "t")
        ctx = BlockContext(prefix=design.PREFIX, cohort=design.COHORT, cfg=cfg, plat=fc, params=sim,
                           session_dir=sess["dir"], replica_runs=tmp_path / "replica",
                           progress=root / "robustness" / "sessions" / "robustness_progress.jsonl", log=lambda *_: None)
        rows = [r for r in design.schedule(5) if r["load"] in design.LEVELS][:4]
        rows = [{**r, "slot": i + 1} for i, r in enumerate(rows)]
        res = run_cohort(rows, ctx)
    finally:
        fc.stop()
    assert not res["stopped"]
    assert [r["run_id"] for r in ctx.runs] == ["CAL-0017", "CAL-0018", "CAL-0019", "CAL-0020"]   # numbered on
    rec = score_cohort(root / "robustness" / "sessions" / "robustness_progress.jsonl", rows,
                       root / "robustness" / "scoring", con, frozen, root / "robustness" / "analysis_input",
                       root / "robustness" / "scoring" / "scores_lock_R.json")
    assert rec["n_scored"] == 4
    out, hr, hr_k1, e4 = results(root, plan)
    assert out["R_T1"]["pairs_with_H"] == len([r for r in hr if r["H"] is not None]) > 0
    assert {r["k_used"] for r in hr} == {2} and {r["k_used"] for r in hr_k1} == {1}
    assert out["R_T4"]["non_correct_pairs"] == sum(out["R_T4"]["counts"].values())
    a = run_all(root, tmp_path / "out", allow_dirty=True, plan=plan)
    assert a["verify_ok"]
    for rel in ("results_R.json", "pairs_R.csv", "pairs_R_k1_contrast.csv", "tables/latex/TR_robustness.tex",
                "figures/FR_robustness.svg", "SHA256SUMS"):
        assert (tmp_path / "out" / rel).exists(), rel
    tex = (tmp_path / "out" / "tables/latex/TR_robustness.tex").read_text()
    assert tex.count("\\begin{tabularx}") == 1 and "\\caption" not in tex
    # the confirmatory and calibration trees were never created or written
    assert not (root / "runs").exists() and not (root / "calibration").exists() and not (root / "scoring").exists()


def test_store_check(tmp_path):
    rows = ["mono_ns,wall_ns,store,name,inode,size,mtime_ns"]
    for t, files in enumerate([[1], [1, 2], [1, 2, 3], [2, 3, 4], [3, 4, 5]]):
        for store in ("audit", "tetragon"):
            rows += [f"0,{t},{store},f{i},{i},10,0" for i in files]
    (tmp_path / "growth_monitor_1hz.csv").write_text("\n".join(rows) + "\n")
    r = store_check(tmp_path, k=2)
    assert r["pass"] and r["stores"]["audit"]["max_files_present"] == 3 and r["stores"]["audit"]["files_deleted"] == 2
    assert not store_check(tmp_path, k=1)["pass"]
