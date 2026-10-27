import csv
import time
from pathlib import Path

from frx.capacity import counter_deltas, evaluate, summarise_resources
from frx.load import render
from frx.monitors import GrowthMonitor, ResourceMonitor, bytes_written
from frx.status import REPO_ROOT


def test_bytes_written_handles_rotation(tmp_path):
    g = tmp_path / "g.csv"
    rows = [
        # t, store, name, inode, size
        (1, "audit", "audit.json", 10, 100),
        (1, "audit", "audit-old.json", 9, 500),
        (2, "audit", "audit.json", 10, 900),
        (3, "audit", "audit-1.json", 10, 1000),    # rotated (same inode, renamed)
        (3, "audit", "audit.json", 11, 50),        # new active
        (4, "audit", "audit.json", 11, 300),
    ]
    with open(g, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["mono_ns", "wall_ns", "store", "name", "inode", "size", "mtime_ns"])
        for t, st, n, ino, sz in rows:
            w.writerow([t * 1_000_000_000, 0, st, n, ino, sz, 0])
    b = bytes_written(g)["audit"]
    assert b["bytes"] == (1000 - 100) + 300 and b["new_inodes"] == 1
    assert b["duration_s"] == 3


def test_monitors_write_rows(tmp_path):
    d = tmp_path / "store"; d.mkdir(); (d / "f.log").write_text("x")
    gm = GrowthMonitor({"s": d}, tmp_path / "g.csv", period_s=0.1)
    rm = ResourceMonitor(tmp_path / "r.csv", period_s=0.1)
    gm.start(); rm.start(); time.sleep(0.45); gm.stop(); rm.stop()
    assert gm.samples >= 3 and rm.samples >= 3 and not gm.errors and not rm.errors
    s = summarise_resources(tmp_path / "r.csv", vcpus=8)
    assert 0 < s["peak_mem_used_fraction"] < 1 and s["oom_kills_during_run"] == 0


def test_counter_deltas_selects_loss_counters():
    before = 'tetragon_missed_events_total{msg_op="5"} 0\ntetragon_events_total{type="X"} 10\napiserver_audit_error_total{plugin="log"} 0\n'
    after = 'tetragon_missed_events_total{msg_op="5"} 3\ntetragon_events_total{type="X"} 99\napiserver_audit_error_total{plugin="log"} 0\n'
    d = counter_deltas(before, after)
    assert d == {'tetragon_missed_events_total{msg_op="5"}': 3.0}


def test_evaluate_pass_and_fail():
    res = {"oom_kills_during_run": 0, "peak_mem_used_fraction": 0.5, "max_load1": 3.0, "vcpus": 8}
    fol = {"audit": {"bytes_written": 1000, "gaps": 0, "errors": [], "alive": False, "rotations": 2},
           "tetragon": {"bytes_written": 2000, "gaps": 0, "errors": [], "alive": False, "rotations": 4}}
    growth = {"audit": {"bytes": 1005}, "tetragon": {"bytes": 2000}}
    assert evaluate(res, {}, {}, fol, growth)["pass"]
    bad = evaluate(dict(res, peak_mem_used_fraction=0.8), {"x": 1}, {}, fol, {"audit": {"bytes": 2000}, "tetragon": {"bytes": 2000}})
    assert not bad["pass"]
    c = bad["criteria"]
    assert not c["peak_memory_le_75pct"]["pass"] and not c["zero_tetragon_drops"]["pass"]
    assert not c["follower_complete_within_1pct"]["pass"]


def test_load_render_pins_and_fills():
    img = "docker.io/library/python@sha256:" + "a" * 64
    text = render(REPO_ROOT, img, replicas=20, exec_per_s=10, api_per_s=10)
    assert "<G3>" not in text and img in text and "replicas: 20" in text
    assert "def api_loop" in text            # script embedded in its ConfigMap


def test_bytes_written_survives_inode_reuse(tmp_path):
    # inode 7 lives as file A (grows to 1000, rotated, later evicted), is absent one sample,
    # then is reused for a brand-new active file that grows to 400: two lives, 1400 bytes total.
    g = tmp_path / "g.csv"
    seq = [
        (1, [("audit.json", 7, 0)]),
        (2, [("audit.json", 7, 600)]),
        (3, [("audit-1.json", 7, 1000), ("audit.json", 8, 100)]),
        (4, [("audit.json", 8, 500)]),                      # inode 7 evicted (absent)
        (5, [("audit-2.json", 8, 900), ("audit.json", 7, 50)]),   # inode 7 reused
        (6, [("audit-2.json", 8, 900), ("audit.json", 7, 400)]),
    ]
    with open(g, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["mono_ns", "wall_ns", "store", "name", "inode", "size", "mtime_ns"])
        for t, files in seq:
            for name, ino, sz in files:
                w.writerow([t * 1_000_000_000, 0, "audit", name, ino, sz, 0])
    b = bytes_written(g)["audit"]
    assert b["bytes"] == 1000 + 900 + 400 and b["new_inodes"] == 2


def test_bytes_written_ignores_one_sample_rename_race(tmp_path):
    # a live file missing from one sample (rename race) must not be double counted
    g = tmp_path / "g.csv"
    seq = [(1, [("audit.json", 7, 0)]), (2, [("audit.json", 7, 500)]), (3, []),
           (4, [("audit-1.json", 7, 1000), ("audit.json", 8, 10)]), (5, [("audit.json", 8, 300)])]
    with open(g, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["mono_ns", "wall_ns", "store", "name", "inode", "size", "mtime_ns"])
        for t, files in seq:
            for name, ino, sz in files:
                w.writerow([t * 1_000_000_000, 0, "audit", name, ino, sz, 0])
            if not files:   # keep the timestamp present via another store's row
                w.writerow([t * 1_000_000_000, 0, "tetragon", "tetragon.log", 99, 0, 0])
    assert bytes_written(g)["audit"]["bytes"] == 1000 + 300
