"""Loss-stage key matching: a key counts as acquired only when the same record kind was acquired."""
import json

from frx.analysis.mechanism import _keys_in_acquired, _record_kind, _ref_keys


def _proc(kind, exec_id, binary="/usr/bin/echo"):
    return {kind: {"process": {"exec_id": exec_id, "binary": binary}}}


def test_exit_record_does_not_count_as_acquired_exec(tmp_path):
    pkg = tmp_path / "pkg"
    (pkg / "acquired_raw" / "tetragon").mkdir(parents=True)
    (pkg / "acquired_raw" / "tetragon" / "a.raw").write_text(json.dumps(_proc("process_exit", "E1")) + "\n")
    ref = tmp_path / "ref.jsonl"
    ref.write_text(json.dumps(_proc("process_exec", "E1")) + "\n" + json.dumps(_proc("process_exit", "E1")) + "\n")
    refk = _ref_keys(ref, [{"evidence": {"line": 1, "exec_id": "E1"}}])
    acq = _keys_in_acquired(pkg, "tetragon")
    assert refk == {("E1", "process_exec")}
    assert [acq[k] for k in refk if k in acq] == []                   # exec record itself not acquired
    assert acq[("E1", "process_exit")] == "a.raw:1"                   # only the exit record was


def test_audit_kind_includes_stage(tmp_path):
    started = {"auditID": "A1", "stage": "ResponseStarted"}
    complete = {"auditID": "A1", "stage": "ResponseComplete"}
    assert _record_kind(started) != _record_kind(complete)
    pkg = tmp_path / "pkg"
    (pkg / "acquired_raw" / "audit").mkdir(parents=True)
    (pkg / "acquired_raw" / "audit" / "a.raw").write_text(json.dumps(complete) + "\n")
    ref = tmp_path / "ref.jsonl"
    ref.write_text(json.dumps(started) + "\n")
    refk = _ref_keys(ref, [{"evidence": {"line": 1, "auditID": "A1"}}])
    acq = _keys_in_acquired(pkg, "audit")
    assert not [k for k in refk if k in acq] and ("A1", "audit:ResponseComplete") in acq
