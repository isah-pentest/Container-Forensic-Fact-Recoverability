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
from frx.analysis.public import demo_evidence, environment, registered_freeze
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
@pytest.fixture(scope="module")
def evidence(tmp_path_factory):
    # the same synthetic store that `python -m frx.analysis demo` builds (frx.analysis.public)
    return demo_evidence(tmp_path_factory.mktemp("evidence"))


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


def test_compare_ignores_only_the_commit_identifier(evidence, tmp_path):
    import shutil
    a = Path(run_all(evidence, tmp_path / "a", draws=2000, allow_dirty=True)["out"])
    b = tmp_path / "b"
    shutil.copytree(a, b)
    c = json.loads((a / "results.json").read_text())["analysis_commit"]
    other = "f" * 40
    for p in b.rglob("*"):
        if p.is_file():
            p.write_bytes(p.read_bytes().replace(c.encode(), other.encode()).replace(c[:12].encode(), other[:12].encode()))
    assert not compare(a, b)["ok"] or c == other
    assert compare(a, b, ignore_commit=True)["ok"]
    t = b / "tables" / "latex" / "T4_effects.tex"
    t.write_text(t.read_text().replace("0", "9", 1))
    assert not compare(a, b, ignore_commit=True)["ok"]                          # a changed number is still caught


def test_registered_freeze_and_withheld_files(tmp_path):
    from frx.freeze import REPO_ROOT
    r = registered_freeze()
    if not r["ok"] and any("not found" in p for p in r["problems"]):
        pytest.skip("frozen tag not present in this clone")
    assert r["ok"], r["problems"]
    # the public history withholds only the internal design document (protocol/BLUEPRINT_v3.0.*)
    assert all(x["file"].startswith("protocol/BLUEPRINT_v3.0.") for x in r["protocol_files_withheld"])
    # a public history without one protocol file: reported as withheld with its hash, nothing else fails
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--no-local", str(REPO_ROOT), str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "fetch", "-q", "origin", "refs/tags/*:refs/tags/*"], check=True)
    env = {**__import__("os").environ, "FILTER_BRANCH_SQUELCH_WARNING": "1"}
    subprocess.run(["git", "-C", str(clone), "filter-branch", "-f", "--index-filter",
                    "git rm -q --cached --ignore-unmatch protocol/master_seed.json", "--tag-name-filter", "cat",
                    "--", "--all"], check=True, capture_output=True, env=env)
    (clone / "protocol" / "registration.json").write_text((REPO_ROOT / "protocol" / "registration.json").read_text())
    w = registered_freeze(clone)
    assert w["ok"], w["problems"]
    withheld = [x["file"] for x in w["protocol_files_withheld"]]
    assert "protocol/master_seed.json" in withheld
    assert all(f == "protocol/master_seed.json" or f.startswith("protocol/BLUEPRINT_v3.0.") for f in withheld)


def test_environment_record():
    e = environment()
    assert e["python"] and any(d.lower().startswith("numpy==") for d in e["distributions"])
