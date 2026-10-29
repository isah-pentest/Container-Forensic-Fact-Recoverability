"""G7 analysis layer end to end on a synthetic evidence store built with the simulated cluster
(synthetic fixtures only; never real study data)."""
import csv
import io
import json
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from fakecluster import FakeCluster
from frx.analysis import mechanism as mech
from frx.analysis.inputs import Evidence
from frx.analysis.regenerate import audit_sample, rescore_check, status
from frx.analysis.report import compare, run_all
from frx.contract import CONTRACT_PATH, PARAMETERS_PATH, load_yaml
from frx.orchestrator.block import BlockContext, open_session, run_cohort
from frx.orchestrator.cohort import run_confirmatory, score_cohort
from frx.orchestrator.run import RunConfig
from frx.randomization import make_schedule
from frx.reconstruct.pipeline import contract_opts, score_package

CON = load_yaml(CONTRACT_PATH)
FROZEN = load_yaml(PARAMETERS_PATH)        # scoring uses the frozen protocol parameters, as in production
RATE = {"audit": 20_000.0, "tetragon": 20_000.0}
PARAMS = {"load": {"replicas": 2, "rate_low_bytes_per_s": RATE, "rate_moderate_bytes_per_s": RATE,
                   "per_replica_exec_per_s": {"L": 1, "M": 2}, "per_replica_api_per_s": {"L": 1, "M": 2},
                   "calibration": {"CAL1": {"target_bytes_per_s": RATE, "exec_per_s": 1, "api_per_s": 1},
                                   "CAL4": {"target_bytes_per_s": RATE, "exec_per_s": 2, "api_per_s": 2}}},
          "timing": {"load_warmup_block_start_s": 0, "epsilon_s": 5.0, "grace_s": 0.5}}


def _cfg(root: Path, out: Path) -> RunConfig:
    out.mkdir(parents=True, exist_ok=True)
    return RunConfig(out_root=out, ledger=root / "ledger" / "attempt_ledger.csv", delay_s=1.5, grace_s=0.5,
                     epsilon_s=5.0, delay_tolerance_s=1.0, settle_s=0.1, warmup_s=0.2, manipulation_window_s=2,
                     manipulation_tolerance=0.6, namespace_delete_timeout_s=5, control_idle_s=0.3,
                     mem_min_fraction=0.0, disk_min_bytes=0)


@pytest.fixture(scope="module")
def evidence(tmp_path_factory):
    root = tmp_path_factory.mktemp("evidence")
    (root / "ledger").mkdir()
    # confirmatory: 2 blocks, scored and locked, frozen primary analysis written as in the runbook
    fc = FakeCluster(root / "c")
    try:
        cfg = _cfg(root, root / "runs")
        sess = open_session(root / "sessions", fc, cfg.ledger, {"stable_hash": "h"}, "h", [], "test")
        ctx = BlockContext(prefix="RUN", cohort="confirmatory", cfg=cfg, plat=fc, params=PARAMS,
                           session_dir=sess["dir"], replica_runs=root / "replica" / "runs",
                           progress=root / "sessions" / "confirmatory_progress.jsonl", log=lambda *_: None)
        rows = make_schedule(11, 2, "BLOCK")
        run_confirmatory(rows, ctx, CON, PARAMS, root / "sessions" / "control_checks.jsonl")
    finally:
        fc.stop()
    score_cohort(root / "sessions" / "confirmatory_progress.jsonl", rows, root / "scoring", CON, FROZEN,
                 root / "analysis_outputs", root / "scoring" / "scores_lock.json")
    from frx.cli import main as frozen_cli
    seed = int(load_yaml(Path(__file__).resolve().parents[1] / "protocol" / "parameters.yaml")["design"]["master_seed"])
    with redirect_stdout(io.StringIO()):
        frozen_cli(["analyze", str(root / "analysis_outputs" / "scores.csv"), "--seed", str(seed), "--draws", "2000",
                    "--out", str(root / "analysis_outputs" / "primary.json")])
    # calibration: a tiny store (15 kB x 1) so that delayed reads do lose records
    fc2 = FakeCluster(root / "k", max_bytes=15_000, backups=1)
    try:
        cfg2 = _cfg(root, root / "calibration" / "runs")
        sess2 = open_session(root / "calibration" / "sessions", fc2, cfg2.ledger, {"stable_hash": "h"}, "h", [], "t")
        cal_rows = [{"block": "CAL-BLOCK-01", "slot": i + 1, "cell": "CAL", "load": lvl, "arm": "D", "is_control": 0}
                    for i, lvl in enumerate(["CAL1", "CAL4", "CAL1", "CAL4"])]
        ctx2 = BlockContext(prefix="CAL", cohort="calibration", cfg=cfg2, plat=fc2, params=PARAMS,
                            session_dir=sess2["dir"], replica_runs=root / "replica" / "cal",
                            progress=root / "calibration" / "calibration_progress.jsonl", log=lambda *_: None)
        run_cohort(cal_rows, ctx2)
    finally:
        fc2.stop()
    opts = contract_opts(CON)
    for r in ctx2.runs:
        if r["disposition"] == "SCORED":
            score_package(Path(r["package"]), root / "calibration" / "scoring", CON, [opts], opts,
                          epsilon_s=FROZEN["timing"]["epsilon_s"], grace_s=FROZEN["timing"]["grace_s"],
                          f5_tolerance_s=CON["facts"]["F5"]["tolerance_s"], in_process=True)
    return root


def test_status_and_verify(evidence):
    ev = Evidence(evidence)
    s = status(ev)
    assert s["blocks_closed"] == 2 and s["attempts"] == 10 and s["gates_ok"] == 10 and s["scores_locked"]
    v = ev.verify()
    assert v["ok"], v["problems"][:5]
    assert v["packages_checked"] == 14


def test_verify_detects_tampering(evidence, tmp_path):
    import shutil
    copy = tmp_path / "ev"
    shutil.copytree(evidence, copy, copy_function=shutil.copyfile)
    ev = Evidence(copy)
    r = ev.confirmatory.scored()[0]["run_id"]
    p = copy / "scoring" / r / "assessment" / "scores.json"
    p.chmod(0o644)
    p.write_text(p.read_text().replace('"Y": 1', '"Y": 0', 1))
    assert any("locked score changed" in x for x in ev.verify()["problems"])


def test_rescore_reproduces_locked_scores(evidence):
    r = rescore_check(Evidence(evidence))
    assert r["ok"], r["problems"]
    assert r["rescored"] == 14


def test_mechanism_rows_and_loss_stages(evidence, monkeypatch):
    ev = Evidence(evidence)
    monkeypatch.setattr(Evidence, "store_capacity", lambda self: {"audit": {"M": 15_000, "k": 1},
                                                                    "tetragon": {"M": 15_000, "k": 1}})
    hr = mech.headroom_rows(ev)
    assert hr and {r["cohort"] for r in hr} >= {"calibration"}
    assert all(r["age_s"] > 0 and r["rate_bytes_per_s"] > 0 for r in hr)
    cal = [r for r in hr if r["cohort"] == "calibration" and r["fact"] == "F1"]
    assert cal and all(r["H"] is not None for r in cal)
    summary = mech.calibration_summary(hr)
    assert "F1-audit" in summary["by_fact_source"]
    ls = mech.loss_stages(ev)
    assert ls["non_correct_pairs"] == sum(ls["M22_counts"].values())


def test_all_outputs_are_complete_and_reproducible(evidence, tmp_path):
    a = run_all(evidence, tmp_path / "a", draws=2000, allow_dirty=True)
    assert a["verify_ok"] and a["primary_reproduced"]
    out = Path(a["out"])
    for rel in ("results.json", "runs.csv", "e3_headroom.csv", "claims_register.csv", "tables/tables.md",
                "tables/T4_primary.csv", "figures/F3_cells.svg", "figures/F4_effects.svg", "figures/F7_calibration.svg",
                "figures/F2_timeline.svg", "SHA256SUMS"):
        assert (out / rel).exists(), rel
    for name in ("T1_design", "T2_contract", "T3_recovery", "T4_effects", "T5_sources", "T6_mechanism",
                 "S1_sensitivity", "S2_operation", "S3_integrity"):
        tex = (out / "tables" / "latex" / f"{name}.tex").read_text()
        assert tex.isascii() and tex.count("\\begin{tabularx}") == 1, name
        assert "\\caption" not in tex and "\\begin{table}" not in tex, name   # manuscript text stays out of the repo
    import re as _re
    for name in ("T4_effects", "S1_sensitivity"):   # effect estimates at 4 decimals, matching the manuscript text
        first = (out / "tables" / "latex" / f"{name}.tex").read_text().split("\\midrule", 1)[1].splitlines()[1]
        assert _re.search(r"\d\.\d{4}(?!\d)", first), (name, first)
    res = json.loads((out / "results.json").read_text())
    assert set(res["sensitivity"]) >= {"S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9"}
    assert res["validity"]["M14_false_attribution"]["x"] == 0
    with pytest.raises(FileExistsError):
        run_all(evidence, tmp_path / "a", draws=2000, allow_dirty=True)          # written once
    b = run_all(evidence, tmp_path / "b", draws=2000, allow_dirty=True)
    assert compare(out, Path(b["out"]))["ok"]                                    # deterministic regeneration
    claims = list(csv.DictReader(open(out / "claims_register.csv")))
    assert {c["claim_id"] for c in claims} >= {"C-LC", "C-MD", "C-H1", "C-FA"}


def test_audit_sample_is_seeded_stratified(evidence):
    ev = Evidence(evidence)
    a, b = audit_sample(ev), audit_sample(ev)
    assert a["runs"] == b["runs"] and len(a["runs"]) == 5            # >= 10 % of each cell, at least 1
    assert a["items"] and all(i["raw_record"] for i in a["items"] if i["role"] == "target")


def test_cli_entry_point(evidence):
    r = subprocess.run([sys.executable, "-m", "frx.analysis", "status", "--evidence", str(evidence)],
                       capture_output=True, text=True)
    assert r.returncode == 0 and json.loads(r.stdout)["blocks_closed"] == 2


def test_run_level_dataset_rebuilds_published_quantities(evidence, tmp_path):
    from frx.analysis import dataset as ds
    a = run_all(evidence, tmp_path / "d", draws=2000, allow_dirty=True)
    assert a["run_level_rebuild_ok"]
    out = Path(a["out"])
    rows = list(csv.DictReader(open(out / "data" / "run_level.csv")))
    dic = list(csv.DictReader(open(out / "data" / "data_dictionary.csv")))
    assert [d["column"] for d in dic] == list(rows[0].keys()) == ds.COLUMNS     # every column documented
    assert all(d["type"] and d["definition"] and d["recorded_source"] for d in dic)
    assert len(rows) == 14                                                        # every attempt, both cohorts
    assert {r["cohort"] for r in rows} == {"confirmatory", "calibration"}
    assert all(r["H_F1_audit"] for r in rows if r["arm"] == "D" and r["is_control"] == "0")
    assert not any(r["H_F1_audit"] for r in rows if r["arm"] == "C")
    rb = json.loads((out / "data" / "rebuild_check.json").read_text())
    assert rb["ok"] and len(rb["checks"]) >= 15, rb["failed"]
    # standalone check from the two files alone
    cmd = [sys.executable, "-m", "frx.analysis", "rebuild-check", str(out / "data" / "run_level.csv"), str(out / "results.json")]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0 and json.loads(r.stdout)["ok"], r.stdout[-2000:]
    # an edited observation is detected
    text = (out / "data" / "run_level.csv").read_text()
    i = next(k for k, x in enumerate(rows) if x["cohort"] == "confirmatory" and x["is_control"] == "0")
    rows[i]["Y"] = "0" if rows[i]["Y"] == "1" else "1"
    b = io.StringIO()
    w = csv.DictWriter(b, fieldnames=ds.COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    bad = ds.rebuild(b.getvalue(), json.loads((out / "results.json").read_text()))
    assert not bad["ok"] and any("primary" in f for f in bad["failed"])
    assert ds.rebuild(text, json.loads((out / "results.json").read_text()))["ok"]
