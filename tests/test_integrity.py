import json
import shutil

import pytest

from frx import integrity as ig
from frx.status import REPO_ROOT, validate_status
from test_status import make

SPEC = ig.load_required(REPO_ROOT / "protocol" / "required_files.yaml")


def build_pkg(root, status, extra_skip=()):
    pkg = root / status["run_id"]
    files = list(SPEC["always"]) + list(SPEC["scored_positive"]) + list(SPEC["arm_" + status["arm"]])
    for pattern in files:
        rel = pattern.replace("*", "part-0001.raw")
        if rel in extra_skip or rel == "run_status.json":
            continue
        ig.write_once(pkg / rel, b"x\n")
    ig.write_once(pkg / "run_status.json", json.dumps(status).encode())
    ig.write_manifest(pkg)
    return pkg


def test_complete_package_passes(tmp_path):
    pkg = build_pkg(tmp_path, make())
    res = ig.verify_package(pkg, SPEC, validate_status)
    assert res["ok"], res["problems"]


def test_missing_and_tampered(tmp_path):
    pkg = build_pkg(tmp_path, make(), extra_skip=("system/kernel_log.txt",))
    res = ig.verify_package(pkg, SPEC, validate_status)
    assert any("kernel_log" in p for p in res["problems"])
    pkg2 = build_pkg(tmp_path / "b", make())
    (pkg2 / "timing" / "clock_status.json").write_bytes(b"changed\n")
    res2 = ig.verify_package(pkg2, SPEC, validate_status)
    assert any("hash mismatch" in p for p in res2["problems"])


def test_collector_failure_allows_absent_raw_with_receipt(tmp_path):
    f = dict(make()["flags"], collector_complete=False)
    st = make(flags=f, primary_failure_code="CF-READ")
    pkg = build_pkg(tmp_path, st, extra_skip=("acquired_raw/audit/part-0001.raw",
                                              "acquired_raw/tetragon/part-0001.raw"))
    assert ig.verify_package(pkg, SPEC, validate_status)["ok"]


def test_write_once_and_lock(tmp_path):
    p = tmp_path / "a.txt"
    ig.write_once(p, b"1")
    with pytest.raises(FileExistsError):
        ig.write_once(p, b"2")


def test_ledger_never_reuses_ids(tmp_path):
    led = tmp_path / "ledger.csv"
    ig.append_ledger(led, {"run_id": "RUN-0001", "disposition": "SCORED"})
    with pytest.raises(ValueError):
        ig.append_ledger(led, {"run_id": "RUN-0001", "disposition": "SCORED"})


def test_block_replica_gate(tmp_path):
    pkg = build_pkg(tmp_path / "local", make())
    rep = tmp_path / "replica"
    shutil.copytree(pkg, rep / pkg.name)
    assert ig.verify_block([pkg], rep)["ok"]
    (rep / pkg.name / "identity.json").write_bytes(b"other\n")
    assert not ig.verify_block([pkg], rep)["ok"]
