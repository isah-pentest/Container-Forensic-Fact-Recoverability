"""KubeHarness against realistic API responses (Status bodies on 404/409, Pod bodies on 200)."""
import pytest

from frx.harness import RunIdentity
from frx.harness.kube import KubeHarness, Response, StepFailed

IMG = "docker.io/library/debian@sha256:" + "a" * 64
TPL = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "x", "namespace": "y"},
       "spec": {"containers": [{"name": "target", "image": "z"}]}}
ID = RunIdentity("QUAL-0001", "frx-1", "tgt-1", "ab" * 16)


def status_404(name):
    return {"kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Failure",
            "message": f'pods "{name}" not found', "reason": "NotFound",
            "details": {"name": name, "kind": "pods"}, "code": 404}


def pod(phase, ready, uid="u-1", deleting=False):
    md = {"name": "tgt-1", "namespace": "frx-1", "uid": uid}
    if deleting:
        md["deletionTimestamp"] = "2026-10-27T04:30:18Z"
    return {"kind": "Pod", "apiVersion": "v1", "metadata": md,
            "status": {"phase": phase, "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


class FakeClient:
    def __init__(self, script):
        self.script, self.calls, self.kubeconfig = list(script), [], "/dev/null"

    def request(self, method, path, body=None, timeout=30):
        self.calls.append((method, path))
        code, b = self.script.pop(0)
        return Response(code, b, {"mono_ns": 1, "wall_ns": 1}, {"mono_ns": 2, "wall_ns": 2})


def harness(script, **kw):
    j = []
    return KubeHarness(FakeClient(script), TPL, IMG, "/usr/bin/echo", j.append, readiness_timeout_s=5,
                       poll_s=0.0, **kw), j


def test_poll_until_404_handles_status_body():
    h, _ = harness([(200, pod("Running", True, deleting=True)), (200, pod("Running", False, deleting=True)),
                    (404, status_404("tgt-1"))])
    polls = []
    r = h.poll_until_404(ID, polls.append)
    assert r["polls"] == 3
    assert polls[0]["phase"] == "Running" and polls[2]["phase"] is None and polls[2]["reason"] == "NotFound"


def test_create_waits_for_ready_and_tolerates_transient_status_body():
    h, j = harness([(201, pod("Pending", False)), (404, status_404("tgt-1")), (200, pod("Pending", False)),
                    (200, pod("Running", True))])
    r = h.create_pod_and_verify(ID)
    assert r["uid"] == "u-1" and r["ready_polls"] == 3


def test_create_conflict_is_un_create():
    h, _ = harness([(409, {"kind": "Status", "status": "Failure", "reason": "AlreadyExists", "code": 409})])
    with pytest.raises(StepFailed) as e:
        h.create_pod_and_verify(ID)
    assert e.value.code == "UN-CREATE"


def test_delete_records_response_uid():
    h, _ = harness([(200, pod("Running", True, uid="u-9", deleting=True))])
    assert h.delete_pod_and_verify(ID)["response_uid"] == "u-9"


def test_poll_timeout_is_un_term():
    h, _ = harness([(200, pod("Running", True, deleting=True))] * 50)
    h.term_to = 0.0
    with pytest.raises(StepFailed) as e:
        h.poll_until_404(ID, lambda _: None)
    assert e.value.code == "UN-TERM"
