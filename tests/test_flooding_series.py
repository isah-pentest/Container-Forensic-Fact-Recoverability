"""Series F (post-termination flood) on synthetic fixtures: level arithmetic, schedule, G1 verdict, freeze
gate, flood controller, and the run -> score -> analysis chain through the frozen functions with a
simulated cluster whose producers the controller speeds up (synthetic only; never study data)."""
import json
import subprocess
import threading
import time

import pytest

from fakecluster import FakeCluster
from frx.analysis.inputs import Cohort
from frx.analysis.public import PARAMS, RATE, _cfg
from frx.contract import CONTRACT_PATH, PARAMETERS_PATH, load_yaml
from frx.flooding import design, series
from frx.flooding import freeze as ffreeze
from frx.flooding.analysis import flood_rows, pair_rows, results
from frx.flooding.controller import FloodController
from frx.orchestrator.block import BlockContext, level_settings, open_session, run_cohort
from frx.orchestrator.cohort import score_cohort

MIB = 1024 * 1024


def test_levels_meet_the_rules_with_frozen_settings():
    prm = load_yaml(PARAMETERS_PATH)
    lv = design.levels(prm)
    assert not lv["problems"], lv["problems"]
    L = lv["levels"]
    assert L["F0"]["flood"] is None
    assert [L[x]["flood"]["source_setting"] for x in ("F1", "F2", "F3")] == ["CAL1", "CAL3", "CAL4"]
    for x in ("F1", "F2", "F3"):          # flood knobs are the frozen calibration knobs, within the per-replica caps
        c = prm["load"]["calibration"][L[x]["flood"]["source_setting"]]
        assert L[x]["flood"]["knob"] == {"exec_per_s": c["exec_per_s"], "api_per_s": c["api_per_s"]}
        assert c["api_per_s"] <= 20 and c["exec_per_s"] <= 10
    for p in L["F1"]["predicted"].values():
        assert p["W_over_kM"] <= 0.8 and p["p_loss_formula"] == 0.0
    for p in L["F2"]["predicted"].values():
        assert 1.0 < p["W_over_kM"] and p["W_over_k1M"] < 1.0 and 0 < p["p_loss_formula"] < 1
    for p in L["F3"]["predicted"].values():
        assert p["W_over_k1M"] >= 1.1 and p["p_loss_formula"] == 1.0
    # hand check, audit F1 at F3: 8 s + 10 s at L, 50 s at CAL4
    w = 18 * 42834.0 + 50 * 462607.1
    assert L["F3"]["predicted"]["F1-audit"]["written_bytes"] == pytest.approx(w)


def test_schedule_is_seeded_and_balanced():
    a, b = design.schedule(123), design.schedule(123)
    assert a == b and len(a) == 26 and a != design.schedule(124)
    cells = [(r["cell"], r["arm"], r["load"], r["is_control"]) for r in a]
    for cell, arm, lvl, ctl, n in design.CELLS:
        assert cells.count((cell, arm, lvl, ctl)) == n
    assert {r["block"] for r in a} == {design.BLOCK} and design.SEED_OFFSET not in (13, 29)


def test_series_params_keep_the_low_setting_and_schema(tmp_path):
    import re
    frozen = load_yaml(PARAMETERS_PATH)
    prm = design.series_params(frozen, {"k": 1, "fingerprint_stable_hash": "abc"})
    for lvl in design.LEVELS:
        knob, tgt = level_settings(prm, lvl)
        assert knob == {"exec_per_s": frozen["load"]["per_replica_exec_per_s"]["L"],
                        "api_per_s": frozen["load"]["per_replica_api_per_s"]["L"]}
        assert tgt == frozen["load"]["rate_low_bytes_per_s"]
    assert "F0" not in frozen["load"]["calibration"]                        # copy only
    schema = json.loads((PARAMETERS_PATH.parent / "schemas" / "run_status.schema.json").read_text())["properties"]
    assert re.match(schema["run_id"]["pattern"], f"{design.PREFIX}-0001")
    assert design.COHORT in schema["cohort"]["enum"] and {"CAL", "NC"} <= set(schema["cell"]["enum"])


def test_g1_verdict(tmp_path):
    prm = load_yaml(PARAMETERS_PATH)
    pts = [{"exec_per_s": s["exec_per_s"], "api_per_s": s["api_per_s"],
            "bytes_per_s": {k: v * 1.1 for k, v in s["target_bytes_per_s"].items()}} for s in series.g1_settings(prm)]
    p = tmp_path / "lm.json"
    p.write_text(json.dumps({"replicas": prm["load"]["replicas"], "points": pts}))
    assert series.g1_evaluate(p, prm)["pass"]
    pts[5]["bytes_per_s"]["tetragon"] = pts[5]["bytes_per_s"]["tetragon"] / 1.1 * 0.79      # CAL4 -21 %
    p.write_text(json.dumps({"replicas": prm["load"]["replicas"], "points": pts}))
    g = series.g1_evaluate(p, prm)
    assert not g["pass"] and not g["settings"][5]["stores"]["tetragon"]["pass"]
    p.write_text(json.dumps({"replicas": 10, "points": pts}))
    assert any("replicas" in x for x in series.g1_evaluate(p, prm)["problems"])


def test_freeze_gate_requires_the_tag(tmp_path):
    def git(*x):
        subprocess.run(["git", "-C", str(tmp_path), *x], check=True, capture_output=True)
    git("init", "-q"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    d = tmp_path / ffreeze.DIR
    d.mkdir(parents=True)
    for f in ffreeze.FILES:
        (d / f).write_text(f"{f}\n")
    (tmp_path / "src").mkdir(); (tmp_path / "src" / "x.py").write_text("x = 1\n")
    git("add", "-A"); git("commit", "-qm", "base")
    assert not ffreeze.check(tmp_path)["ok"]
    ffreeze.write_freeze(tmp_path)
    with pytest.raises(FileExistsError):
        ffreeze.write_freeze(tmp_path)
    git("add", "-A"); git("commit", "-qm", "freeze")
    r = ffreeze.check(tmp_path)
    assert not r["ok"] and any("tag" in p for p in r["problems"])
    git("tag", ffreeze.TAG)
    assert ffreeze.check(tmp_path)["ok"], ffreeze.check(tmp_path)
    (d / "plan.json").write_text("changed\n")
    assert any("plan.json changed" in p for p in ffreeze.check(tmp_path)["problems"])
    (d / "plan.json").write_text("plan.json\n")
    (tmp_path / "src" / "x.py").write_text("x = 2\n")
    assert any("tracked code" in p for p in ffreeze.check(tmp_path)["problems"])


# ---------------------------------------------------------------------- controller unit tests
class _Run:
    def __init__(self, runs, run_id, level, arm):
        self.d = runs / run_id
        (self.d / "timing").mkdir(parents=True)
        (self.d / "identity.json").write_text(json.dumps({"run_id": run_id, "load_level": level, "arm": arm,
                                                           "is_control": False}))
        self.fh = open(self.d / "timing" / "event_journal.jsonl", "a")

    def J(self, state):
        rec = {"state": state, "mono_ns": time.monotonic_ns(), "wall_ns": time.time_ns()}
        self.fh.write(json.dumps(rec) + "\n"); self.fh.flush()
        return rec


def _controller(tmp_path, window_s=0.4):
    calls = []
    lock = threading.Lock()

    def fake(e, a, label):
        with lock:
            calls.append((time.monotonic_ns(), e, a, label))
        return {"label": label}
    lv = {"F0": None, "F3": {"knob": {"exec_per_s": 9.0, "api_per_s": 9.0}}}
    c = FloodController(tmp_path / "runs", tmp_path / "ctl", lv, {"knob": {"exec_per_s": 1.0, "api_per_s": 1.0}},
                        window_s, set_rate=fake, poll_s=0.01, log=lambda *_: None)
    return c, calls


def _events(tmp_path, run_id):
    return [json.loads(x)["event"] for x in (tmp_path / "ctl" / f"{run_id}.jsonl").read_text().splitlines()]


def test_controller_floods_for_the_window_then_restores(tmp_path):
    (tmp_path / "runs" / "CAL-0003").mkdir(parents=True)          # existed before the session: never handled
    c, calls = _controller(tmp_path)
    c.start()
    r = _Run(tmp_path / "runs", "CAL-0004", "F3", "D")
    r.J("RATE_SET"); time.sleep(0.05)
    t = r.J("TERMINATION_CONFIRMED")
    time.sleep(0.8)
    r.J("ACQ_START_D"); r.J("PACKAGE_CLOSED")
    time.sleep(0.2)
    c.stop(); c.join(2)
    assert [x[3] for x in calls] == ["F3-FLOOD", "F3"] and calls[0][1] == 9.0 and calls[1][1] == 1.0
    assert (calls[1][0] - t["mono_ns"]) / 1e9 == pytest.approx(0.4, abs=0.1)
    assert _events(tmp_path, "CAL-0004") == ["WATCH", "TERMINATION_SEEN", "FLOOD_SET", "RESTORED", "DONE"]
    assert "CAL-0003" not in c.handled and c.handled["CAL-0004"]["restored"] == "window elapsed" and not c.errors


def test_controller_restores_at_end_of_continuous_acquisition(tmp_path):
    c, calls = _controller(tmp_path, window_s=5.0)
    c.start()
    r = _Run(tmp_path / "runs", "CAL-0001", "F3", "C")
    r.J("TERMINATION_CONFIRMED"); time.sleep(0.2)
    r.J("ACQ_END_C"); time.sleep(0.2)
    r.J("PACKAGE_CLOSED"); time.sleep(0.2)
    c.stop(); c.join(2)
    assert [x[3] for x in calls] == ["F3-FLOOD", "F3"]
    assert c.handled["CAL-0001"]["restored"] == "run state ACQ_END_C"


def test_controller_no_flood_level_and_run_without_termination(tmp_path):
    c, calls = _controller(tmp_path)
    c.start()
    r = _Run(tmp_path / "runs", "CAL-0001", "F0", "D")
    r.J("TERMINATION_CONFIRMED"); r.J("ACQ_START_D"); r.J("PACKAGE_CLOSED"); time.sleep(0.2)
    r2 = _Run(tmp_path / "runs", "CAL-0002", "F3", "D")
    r2.J("PRE_ACTION_ABORT"); r2.J("PACKAGE_CLOSED"); time.sleep(0.2)
    c.stop(); c.join(2)
    assert calls == []
    assert "NO_FLOOD" in _events(tmp_path, "CAL-0001") and c.handled["CAL-0002"]["flooded"] is False


def test_controller_never_floods_across_runs(tmp_path):
    c, calls = _controller(tmp_path, window_s=5.0)
    c.start()
    r = _Run(tmp_path / "runs", "CAL-0001", "F3", "D")
    r.J("TERMINATION_CONFIRMED"); time.sleep(0.2)
    _Run(tmp_path / "runs", "CAL-0002", "F0", "D"); time.sleep(0.3)
    c.stop(); c.join(2)
    assert [x[3] for x in calls] == ["F3-FLOOD", "F3"] and c.handled["CAL-0001"]["restored"] == "later run started"


# ---------------------------------------------------------------------- chain on a simulated cluster
def test_series_chain_on_simulated_cluster(tmp_path):
    con, frozen = load_yaml(CONTRACT_PATH), load_yaml(PARAMETERS_PATH)
    m, k = 60_000, 1
    plan = {"k": k, "M_mib": m / MIB, "fingerprint_stable_hash": "h", "flood_window_s": 1.0}
    prm = design.series_params(frozen, plan)
    sim = {**PARAMS, "load": {**PARAMS["load"], "calibration": {**PARAMS["load"]["calibration"],
                                                                **{x: prm["load"]["calibration"][x] for x in design.LEVELS}}}}
    for x in design.LEVELS:     # the simulated runner applies the simulated low setting before the action
        sim["load"]["calibration"][x] = {"target_bytes_per_s": RATE, "exec_per_s": 1, "api_per_s": 1}
    root = tmp_path / "evidence"
    P = series.paths(root)
    fc = FakeCluster(tmp_path / "c", max_bytes=m, backups=k)

    def fake_rate(e, a, label):                 # knob units -> simulated bytes/s
        fc.set_rates(audit=a * 20_000, tetragon=e * 20_000)
        return {"label": label}
    ctl = FloodController(P["runs"], P["controller"], {"F0": None, "F3": {"knob": {"exec_per_s": 15, "api_per_s": 15}}},
                          {"knob": {"exec_per_s": 1, "api_per_s": 1}}, plan["flood_window_s"], set_rate=fake_rate,
                          poll_s=0.01, log=lambda *_: None)
    rows = [{"block": design.BLOCK, "slot": i + 1, "cell": c, "load": lvl, "arm": arm, "is_control": ctl_}
            for i, (c, arm, lvl, ctl_) in enumerate((("CAL", "D", "F0", 0), ("CAL", "D", "F3", 0),
                                                     ("CAL", "C", "F3", 0), ("NC", "D", "F3", 1)))]
    try:
        cfg = _cfg(root, P["runs"])
        sess = open_session(P["sessions"], fc, cfg.ledger, {"stable_hash": "h"}, "h", [], "t")
        ctx = BlockContext(prefix=design.PREFIX, cohort=design.COHORT, cfg=cfg, plat=fc, params=sim,
                           session_dir=sess["dir"], replica_runs=tmp_path / "replica",
                           progress=P["progress"], log=lambda *_: None)
        ctl.start()
        res = run_cohort(rows, ctx)
    finally:
        ctl.stop(); ctl.join(5)
        fc.stop()
    assert not res["stopped"] and not ctl.errors
    assert {v["level"]: v["flooded"] for v in ctl.handled.values()} == {"F0": False, "F3": True}
    rec = score_cohort(P["progress"], rows, P["scoring"], con, frozen, P["analysis_input"], P["lock"])
    assert rec["n_scored"] == 4
    coh = Cohort(design.COHORT, P["progress"], P["scoring"]).load()
    hr = pair_rows(coh, m, k)
    f0 = [r for r in hr if r["level"] == "F0"]
    f3 = [r for r in hr if r["level"] == "F3"]
    assert f0 and not any(r["observed_lost"] for r in f0)
    assert f3 and all(r["observed_lost"] for r in f3)
    assert all(r["H"] < 1 for r in f3 if r["H"] is not None)
    fr = flood_rows(coh, P["controller"], plan["flood_window_s"])
    assert {r["level"] for r in fr if r["controller_record"]} == {"F0", "F3"}
    out, _, _ = results(root, {**plan, "M_mib": m / MIB})
    assert out["G2"]["pass"] and out["F_T2"]["supported"] and out["F_C1"]["supported"]
    assert out["F_T1"]["pairs_with_H"] > 0
    # the confirmatory, calibration and series-R trees were never created
    assert not (root / "runs").exists() and not (root / "calibration").exists() and not (root / "robustness").exists()
