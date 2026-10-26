import json

from frx.smoke import analyse

NS, POD, NONCE, UID = "frx-smoke-ab12", "tgt-ab12", "0" * 32, "uid-123"
MARK = f"FRX-{NONCE}"


def audit_lines():
    ev = [
        {"kind": "Event", "auditID": "a1", "stage": "ResponseComplete", "verb": "create",
         "objectRef": {"resource": "pods", "namespace": NS, "name": POD},
         "responseStatus": {"code": 201}, "responseObject": {"metadata": {"uid": UID}},
         "user": {"username": "kubernetes-admin"}},
        {"kind": "Event", "auditID": "a2", "stage": "ResponseStarted", "verb": "create",
         "objectRef": {"resource": "pods", "namespace": NS, "name": POD, "subresource": "exec"},
         "requestURI": f"/api/v1/namespaces/{NS}/pods/{POD}/exec?command=%2Fusr%2Fbin%2Fecho&command={MARK}&container=target&stdout=true"},
        {"kind": "Event", "auditID": "a3", "stage": "ResponseComplete", "verb": "delete",
         "objectRef": {"resource": "pods", "namespace": NS, "name": POD},
         "responseStatus": {"code": 200}, "responseObject": {"metadata": {"uid": UID}}},
        {"kind": "Event", "verb": "get", "objectRef": {"resource": "pods", "namespace": "other"}},
    ]
    return [("audit.json", "\n".join(json.dumps(e) for e in ev) + "\nnot json\n")]


def tetragon_lines():
    pod = {"namespace": NS, "name": POD, "container": {"id": "containerd://c1"}}
    ev = [
        {"process_exec": {"process": {"binary": "/usr/bin/echo", "arguments": MARK, "pod": pod}}, "time": "t1"},
        {"process_exec": {"process": {"binary": "/usr/bin/true", "arguments": "", "pod": pod}}, "time": "t0"},
        {"process_exit": {"process": {"binary": "/usr/bin/sleep", "arguments": "infinity", "pod": pod}}, "time": "t2"},
    ]
    return [("tetragon.log", "\n".join(json.dumps(e) for e in ev))]


def test_all_facts_found():
    r = analyse(audit_lines(), tetragon_lines(), NS, POD, NONCE, UID)
    assert all(r["verdict"].values()), r["verdict"]
    assert r["F2_audit_exec"][0]["decoded_command"] == ["/usr/bin/echo", MARK]
    assert r["other_pod_tetragon_exec"] == 1


def test_missing_pod_metadata_detected():
    lines = [("tetragon.log", json.dumps(
        {"process_exec": {"process": {"binary": "/usr/bin/echo", "arguments": MARK, "pod": {}}}}))]
    v = analyse(audit_lines(), lines, NS, POD, NONCE, UID)["verdict"]
    assert v["F2 tetragon exec with nonce"] and not v["F2 tetragon exec has pod metadata"]
    assert not v["F4 tetragon exit in pod"]
