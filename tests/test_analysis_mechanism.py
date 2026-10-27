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


def test_only_answers_that_score_correct_count():
    from frx.analysis.mechanism import correct_answers
    truth = {"pod": "p", "uid": "u", "executable": "/usr/bin/echo", "arguments": ["N"], "harness_identity": "h"}
    opts = {"f3_actor_required": True, "f3_uid_required": True}
    harness = {"pod_name": "p", "pod_uid": "u", "actor": "h", "actor_is_harness": True}
    kubelet = {"pod_name": "p", "pod_uid": "u", "actor": "system:node:n", "actor_is_harness": False}
    assert correct_answers("F3", "audit", [harness, kubelet], truth, opts) == [harness]
    create = {"pod_name": "p", "pod_uid": "u"}
    binding = {"pod_name": "p", "pod_uid": None}
    assert correct_answers("F1", "audit", [create, binding], truth, opts) == [create]
