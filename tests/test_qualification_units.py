"""Unit tests for the G2/G3 machinery on synthetic fixtures (Sections 3.3, 4.3, 7, 18)."""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

from frx.acquisition.reference import AuditWebhookSlice, run_receiver
from frx.acquisition.store import one_shot_copy, rotation_events
from frx.orchestrator.qual import fit_load_map, make_plan, operating_points
from frx.reconstruct import rfc3339_ns
from frx.reconstruct.score import score_identity, union


def test_one_shot_copy_orders_backups_then_active_with_byte_ranges(tmp_path):
    d = tmp_path / "s"; d.mkdir()
    (d / "audit-2026-10-27T01-00-00.000.json").write_bytes(b"a1\n")
    (d / "audit-2026-10-27T02-00-00.000.json").write_bytes(b"b22\n")
    (d / "audit.json").write_bytes(b"c333\n")
    rec = []
    res = one_shot_copy(d, "audit.json", tmp_path / "out.part", "audit", rec.append)
    assert (tmp_path / "out.part").read_bytes() == b"a1\nb22\nc333\n"
    assert [r["name"] for r in rec] == ["audit-2026-10-27T01-00-00.000.json", "audit-2026-10-27T02-00-00.000.json", "audit.json"]
    assert [(r["stream_start"], r["stream_end"]) for r in rec] == [(0, 3), (3, 7), (7, 12)]
    assert res["ok"] and res["files"] == 3 and not res["vanished"]


def test_rfc3339_nanoseconds_and_offsets():
    a = rfc3339_ns("2026-10-27T02:31:15.123456789Z")
    b = rfc3339_ns("2026-10-27T02:31:15.123456Z")
    c = rfc3339_ns("2026-10-26T21:31:15.123456789-05:00")
    assert a - b == 789 and a == c
    assert rfc3339_ns("garbage") is None and rfc3339_ns(None) is None


def test_operating_points_follow_section_4_3():
    MB = 1024 * 1024
    fit = {"audit": {"intercept_bytes_per_s": 3000.0, "bytes_per_unit": 1400.0, "cross_bytes_per_unit": 0, "r2": 1},
           "tetragon": {"intercept_bytes_per_s": 2000.0, "bytes_per_unit": 2300.0, "cross_bytes_per_unit": 0, "r2": 1}}
    ops = operating_points(fit, {"audit": {"max_mb": 2, "backups": 2}, "tetragon": {"max_mb": 5, "backups": 2}},
                           delay_s=60, action_s=8, exec_age_offset_s=5)
    a = ops["per_store"]["audit"]
    assert a["age_s"] == 68
    # worst case (B = kM) headroom is exactly 3 at +20 % above r_L
    assert a["worst_case_H_at_r_L_plus_tol"] == pytest.approx(3.0)
    # r_M gives P(loss) = 0.5 for F1: r*dt = kM + M/2
    assert a["r_M"] * 68 == pytest.approx(2.5 * 2 * MB)
    # Q-c targets: H (expected offset) 2, 1, 0.5 -> predicted loss 0, 0.5, 1
    from frx.headroom import predicted_loss_probability as P
    assert [round(P(r, 68, 2 * MB, 2), 6) for r in a["QC"]] == [0.0, 0.5, 1.0]
    for lvl in ("L", "M", "QC1", "QC2", "QC3", "CAL1", "CAL4"):
        assert ops["levels"][lvl]["knob"]["api_per_s"] >= 1.0
    assert ops["replicas"] >= 1


def test_operating_points_flag_targets_below_idle_rate():
    fit = {"audit": {"intercept_bytes_per_s": 50_000.0, "bytes_per_unit": 1400.0, "cross_bytes_per_unit": 0, "r2": 1},
           "tetragon": {"intercept_bytes_per_s": 2000.0, "bytes_per_unit": 2300.0, "cross_bytes_per_unit": 0, "r2": 1}}
    ops = operating_points(fit, {"audit": {"max_mb": 1, "backups": 1}, "tetragon": {"max_mb": 5, "backups": 2}},
                           delay_s=60, action_s=8, exec_age_offset_s=5)
    assert any("below the idle rate" in p for p in ops["problems"])


def test_load_map_fit_recovers_linear_model():
    pts = [{"total_api_per_s": a, "total_exec_per_s": e,
            "bytes_per_s": {"audit": 3000 + 1400 * a, "tetragon": 2000 + 2300 * e}}
           for a, e in ((10, 0), (50, 20), (100, 50), (200, 100))]
    f = fit_load_map(pts)
    assert f["audit"]["bytes_per_unit"] == pytest.approx(1400) and f["audit"]["intercept_bytes_per_s"] == pytest.approx(3000)
    assert f["tetragon"]["bytes_per_unit"] == pytest.approx(2300)
    assert abs(f["audit"]["cross_bytes_per_unit"]) < 1e-6


def test_plan_is_seeded_and_complete():
    levels = {k: {} for k in ("L", "QC1", "QC2", "QC3")}
    a, b = make_plan(7, levels), make_plan(7, levels)
    assert a["parts"] == b["parts"]
    assert [len(a["parts"][p]) for p in ("Q-a", "Q-b", "Q-c")] == [12, 12, 9]
    assert sorted(s["level"] for s in a["parts"]["Q-c"]) == ["QC1"] * 3 + ["QC2"] * 3 + ["QC3"] * 3
    assert all(s["arm"] == "C" for s in a["parts"]["Q-a"]) and all(s["arm"] == "D" for s in a["parts"]["Q-b"])
    with pytest.raises(ValueError):
        make_plan(7, {"L": {}})


def test_scoring_states_and_union():
    truth = {"pod": "tgt-1", "uid": "u1", "executable": "/usr/bin/echo", "arguments": ["FRX-ab"],
             "harness_identity": "h"}
    ev = {"ts_ns": 1}
    facts = {"F1": {"audit": [{"pod_name": "tgt-1", "pod_uid": "u2", "evidence": ev}]},          # wrong UID
             "F2": {"audit": [{"executable": "/usr/bin/echo", "arguments": ["FRX-ab"], "actor": "h", "evidence": ev}],
                    "tetragon": []},
             "F3": {"audit": [{"pod_name": "tgt-1", "pod_uid": None, "actor": "k", "actor_is_harness": False,
                               "evidence": ev}]},
             "F4": {"tetragon": []}}
    s = score_identity(facts, truth, {"f3_uid_required": True})
    assert s["facts"]["F1"]["states"]["audit"] == "FALSE" and s["facts"]["F1"]["union"] == "CONFLICT"
    assert s["facts"]["F2"]["states"] == {"audit": "CORRECT", "tetragon": "ABSTAIN"}
    assert s["facts"]["F2"]["union"] == "CORRECT"
    assert s["facts"]["F3"]["states"]["audit"] == "ABSTAIN"          # UID required but missing
    assert s["Y"] == 0 and s["C"] == pytest.approx(1 / 3)
    s2 = score_identity(facts, truth, {"f3_actor_required": True})
    assert s2["facts"]["F3"]["states"]["audit"] == "ABSTAIN"         # kubelet's record does not count
    assert union({"audit": "NOT_ELIGIBLE", "tetragon": "ABSTAIN"}) == "NOT_RECOVERED"
    # an identity that never acted: any answer is a false attribution
    nc = score_identity({"F1": {"audit": [{"pod_name": "x"}]}}, None, {})
    assert nc["false_attribution"] is True


def test_rotation_events_from_growth_rows():
    rows = []
    for i, (ino, size) in enumerate([(1, 10), (1, 20), (2, 5), (2, 9), (2, 12)]):
        rows.append({"mono_ns": i, "wall_ns": i, "store": "audit", "name": "audit.json", "inode": ino, "size": size})
    rows.append({"mono_ns": 2, "wall_ns": 2, "store": "audit", "name": "audit-x.json", "inode": 1, "size": 20})
    ev = rotation_events(rows, {"audit": "audit.json"})
    assert [e["event"] for e in ev if e["event"] == "rotation"] == ["rotation"]
    assert any(e["event"] == "deleted" and e["inode"] == 1 for e in ev)


def test_audit_webhook_receiver_and_slice(tmp_path):
    out = tmp_path / "ref" / "audit_webhook.jsonl"
    port = 18000 + os.getpid() % 1000
    th = threading.Thread(target=run_receiver, args=("127.0.0.1", port, out), daemon=True)
    th.start()
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            break
        except OSError:
            time.sleep(0.1)

    def post(n):
        body = json.dumps({"kind": "EventList", "items": [{"auditID": f"a{i}"} for i in range(n)]}).encode()
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/", data=body, method="POST"), timeout=5)
    post(2)
    sl = AuditWebhookSlice(out, tmp_path / "slice.jsonl")
    sl.start()
    post(3)
    res = sl.stop(flush_s=0.2)
    lines = (tmp_path / "slice.jsonl").read_text().splitlines()
    assert res["healthy"] and [json.loads(l)["auditID"] for l in lines] == ["a0", "a1", "a2"]


def test_reconstruct_cli_runs_isolated(tmp_path):
    if subprocess.run([sys.executable, "-I", "-c", "import frx"], capture_output=True).returncode:
        pytest.skip("frx not installed in this interpreter (pip install -e .)")
    st = tmp_path / "staged"
    for s in ("audit", "tetragon"):
        (st / "acquired_raw" / s).mkdir(parents=True)
        (st / "acquired_raw" / s / ("0" * 64 + ".raw")).write_bytes(b'{"kind":"Event","stageTimestamp":"2026-10-27T00:00:00Z"}\nnot json\n')
    (st / "questions.json").write_text(json.dumps({"window": {"start_wall_ns": 0, "end_wall_ns": 2 * 10**18},
                                                   "identities": [{"role": "target", "namespace": "n", "pod": "p"}]}))
    import yaml
    from frx.contract import CONTRACT_PATH, load_yaml
    from frx.reconstruct import effective_contract
    from frx.status import REPO_ROOT
    q = yaml.safe_load((REPO_ROOT / "protocol" / "qualification.yaml").read_text())
    (st / "contract.json").write_text(json.dumps(effective_contract(load_yaml(CONTRACT_PATH), q["fills"])))
    r = subprocess.run([sys.executable, "-I", "-m", "frx.cli", "reconstruct", str(st), "--out", str(tmp_path / "fa.json"),
                        "--warnings", str(tmp_path / "w.jsonl")], capture_output=True, text=True, cwd=st)
    assert r.returncode == 0, r.stderr
    fa = json.loads((tmp_path / "fa.json").read_text())
    assert fa["parse_warnings"] == 2 and fa["identities"][0]["facts"]["F1"]["audit"] == []


def test_load_map_fit_separates_knobs_with_factorial_design():
    from frx.orchestrator.qual import fit_load_map
    pts = []
    for e, a in zip([0, 10, 0, 10, 5], [1, 1, 20, 20, 10]):      # the CLI default design
        te, ta = 20 * e, 20 * a
        pts.append({"total_exec_per_s": te, "total_api_per_s": ta,
                    "bytes_per_s": {"audit": 4000 + 1400 * ta + 30 * te, "tetragon": 200 + 4700 * te + 5 * ta}})
    f = fit_load_map(pts)
    assert abs(f["audit"]["bytes_per_unit"] - 1400) < 1e-6 and abs(f["audit"]["cross_bytes_per_unit"] - 30) < 1e-6
    assert abs(f["tetragon"]["bytes_per_unit"] - 4700) < 1e-6 and abs(f["tetragon"]["intercept_bytes_per_s"] - 200) < 1e-6


def test_audit_growing_counts_rotation_as_growth():
    from frx.orchestrator.platform import audit_growing
    assert audit_growing((10, 1000), (10, 5000))            # grew
    assert not audit_growing((10, 1000), (10, 1000))        # stalled
    assert audit_growing((10, 10_400_000), (11, 150_000))   # rotated: size fell, new inode (QUAL-0033)
    assert not audit_growing(None, (11, 5))                 # file missing at first sample
    assert not audit_growing((10, 5), None)
