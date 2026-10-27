"""Action harness against the Kubernetes API (Blueprint v3.0, Section 7.1).

Runs under a dedicated identity (ServiceAccount frx-system/frx-harness, short-lived token,
cluster/manifests/harness-rbac.yaml), so its actions are distinguishable from the cluster's own.
Create, delete and every poll go straight to the REST API, which gives exact response codes
and timestamps; exec uses kubectl with the same identity (exit code, stdout, stderr).
Nothing secret is written: the token stays in memory; responses carry no tokens because the
target pod does not mount one.
"""
from __future__ import annotations

import base64
import json
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import RunIdentity

HARNESS_IDENTITY = "system:serviceaccount:frx-system:frx-harness"


class StepFailed(RuntimeError):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code, self.detail = code, detail or {}


def _ns() -> dict:
    return {"mono_ns": time.monotonic_ns(), "wall_ns": time.time_ns()}


@dataclass
class Response:
    code: int
    body: dict | None
    sent: dict
    received: dict


class ApiClient:
    """Minimal bearer-token REST client built from a kubeconfig (current context)."""

    def __init__(self, kubeconfig: Path):
        cfg = yaml.safe_load(Path(kubeconfig).read_text())
        ctx_name = cfg.get("current-context")
        ctx = next(c["context"] for c in cfg["contexts"] if c["name"] == ctx_name)
        cluster = next(c["cluster"] for c in cfg["clusters"] if c["name"] == ctx["cluster"])
        user = next(u["user"] for u in cfg["users"] if u["name"] == ctx["user"])
        self.server = cluster["server"].rstrip("/")
        cadata = base64.b64decode(cluster["certificate-authority-data"]).decode()
        self._ssl = ssl.create_default_context(cadata=cadata)
        token = user.get("token") or (Path(user["tokenFile"]).read_text().strip() if user.get("tokenFile") else None)
        if not token:
            raise ValueError("harness kubeconfig must carry a bearer token (scripts/g2_harness_identity.sh)")
        self._token = token
        self.kubeconfig = Path(kubeconfig)

    def request(self, method: str, path: str, body: dict | None = None, timeout: float = 30) -> Response:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.server + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}", "Accept": "application/json",
            **({"Content-Type": "application/json"} if data else {})})
        sent = _ns()
        try:
            with urllib.request.urlopen(req, context=self._ssl, timeout=timeout) as r:
                raw = r.read()
                code = r.status
        except urllib.error.HTTPError as e:
            raw, code = e.read(), e.code
        received = _ns()
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = {"_unparsed": raw[:2000].decode(errors="replace")}
        return Response(code, parsed, sent, received)


def _brief(body: dict | None) -> dict:
    if not isinstance(body, dict):
        return {}
    md = body.get("metadata") or {}
    out = {"kind": body.get("kind"), "uid": md.get("uid"), "name": md.get("name"),
           "namespace": md.get("namespace"), "resourceVersion": md.get("resourceVersion")}
    if body.get("kind") == "Status":
        out.update(status=body.get("status"), reason=body.get("reason"), message=body.get("message"))
    if body.get("kind") == "Pod":
        st = body.get("status") or {}
        out["phase"] = st.get("phase")
        out["deletionTimestamp"] = md.get("deletionTimestamp")
    return {k: v for k, v in out.items() if v is not None}


class KubeHarness:
    """Implements frx.harness.Harness. journal(dict) appends to ground_truth and sealed/truth."""

    def __init__(self, client: ApiClient, pod_template: dict, image: str, exec_binary: str,
                 journal, readiness_timeout_s: float, termination_timeout_s: float = 120.0,
                 poll_s: float = 0.5, kubectl: str = "kubectl", identity: str = HARNESS_IDENTITY):
        if "@sha256:" not in image:
            raise ValueError("target image must be pinned by digest")
        self.c, self.tpl, self.image, self.binary = client, pod_template, image, exec_binary
        self.journal, self.ready_to, self.term_to, self.poll_s = journal, readiness_timeout_s, termination_timeout_s, poll_s
        self.kubectl, self.identity = kubectl, identity

    # ------------------------------------------------------------------ helpers
    def _call(self, step: str, method: str, path: str, body: dict | None = None, full: bool = False) -> Response:
        r = self.c.request(method, path, body)
        rec = {"step": step, "method": method, "path": path, "code": r.code, "sent": r.sent,
               "received": r.received, "response": r.body if full else _brief(r.body)}
        self.journal(rec)
        return r

    def pod_manifest(self, ident: RunIdentity) -> dict:
        m = json.loads(json.dumps(self.tpl))
        m["metadata"]["name"], m["metadata"]["namespace"] = ident.pod_name, ident.namespace
        m["spec"]["containers"][0]["image"] = self.image
        return m

    # ------------------------------------------------------------------ namespace (setup, cleanup)
    def create_namespace(self, ns: str, timeout_s: float = 60) -> dict:
        r = self._call("namespace_create", "POST", "/api/v1/namespaces",
                       {"apiVersion": "v1", "kind": "Namespace",
                        "metadata": {"name": ns, "labels": {"app.kubernetes.io/part-of": "frx-target"}}})
        if r.code != 201:
            raise StepFailed("PA-ISO", f"namespace create returned {r.code}")
        deadline = time.monotonic() + timeout_s
        while True:   # pods are rejected until the namespace's default ServiceAccount exists
            g = self._call("namespace_sa_wait", "GET", f"/api/v1/namespaces/{ns}/serviceaccounts/default")
            if g.code == 200:
                return {"code": r.code, "uid": _brief(r.body).get("uid"), "ready": g.received}
            if time.monotonic() > deadline:
                raise StepFailed("PA-ISO", "default ServiceAccount not created in time")
            time.sleep(self.poll_s)

    def namespace_gone(self, ns: str) -> bool:
        return self._call("namespace_check", "GET", f"/api/v1/namespaces/{ns}").code == 404

    def delete_namespace(self, ns: str, timeout_s: float) -> dict:
        r = self._call("namespace_delete", "DELETE", f"/api/v1/namespaces/{ns}")
        deadline = time.monotonic() + timeout_s
        while not self.namespace_gone(ns):
            if time.monotonic() > deadline:
                return {"code": r.code, "gone": False}
            time.sleep(1.0)
        return {"code": r.code, "gone": True, **_ns()}

    # ------------------------------------------------------------------ Section 7.1 steps
    def create_pod_and_verify(self, ident: RunIdentity) -> dict:
        r = self._call("create", "POST", f"/api/v1/namespaces/{ident.namespace}/pods",
                       self.pod_manifest(ident), full=True)
        uid = ((r.body or {}).get("metadata") or {}).get("uid")
        if r.code != 201 or not uid:
            raise StepFailed("UN-CREATE", f"create returned {r.code}", {"code": r.code})
        deadline = time.monotonic() + self.ready_to
        polls = 0
        while True:
            g = self._call("ready_wait", "GET", f"/api/v1/namespaces/{ident.namespace}/pods/{ident.pod_name}")
            polls += 1
            conds = (((g.body or {}).get("status") or {}).get("conditions") or [])
            if g.code == 200 and any(c.get("type") == "Ready" and c.get("status") == "True" for c in conds):
                break
            if time.monotonic() > deadline:
                raise StepFailed("UN-CREATE", f"pod not Ready within {self.ready_to} s", {"code": r.code, "uid": uid})
            time.sleep(self.poll_s)
        ready = g.received
        return {"code": r.code, "uid": uid, "request_sent": r.sent, "response_received": r.received,
                "ready_observed": ready, "ready_polls": polls,
                "create_to_ready_s": (ready["mono_ns"] - r.sent["mono_ns"]) / 1e9}

    def exec_nonce_and_verify(self, ident: RunIdentity) -> dict:
        marker = f"FRX-{ident.nonce_hex}"
        cmd = [self.kubectl, "--kubeconfig", str(self.c.kubeconfig), "exec", "-n", ident.namespace,
               ident.pod_name, "-c", "target", "--", self.binary, marker]
        sent = _ns()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            rc, out, err = p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired as e:
            partial = e.stdout or ""
            rc, out, err = None, partial.decode(errors="replace") if isinstance(partial, bytes) else partial, "timeout after 60 s"
        done = _ns()
        res = {"command": [self.binary, marker], "executable": self.binary, "arguments": [marker],
               "exit_code": rc, "stdout": out, "stderr": err, "sent": sent, "returned": done,
               "identity": self.identity}
        self.journal({"step": "exec", "kubectl_args": cmd[3:], "exit_code": rc,
                      "stdout": out, "stderr": err, "sent": sent, "received": done})
        if rc != 0 or marker not in (out or ""):
            raise StepFailed("UN-EXEC", f"exec exit {rc}, nonce in stdout: {marker in (out or '')}", res)
        return res

    def delete_pod_and_verify(self, ident: RunIdentity) -> dict:
        r = self._call("delete", "DELETE", f"/api/v1/namespaces/{ident.namespace}/pods/{ident.pod_name}",
                       {"apiVersion": "v1", "kind": "DeleteOptions"}, full=True)
        if r.code not in (200, 202):
            raise StepFailed("UN-DELETE", f"delete returned {r.code}", {"code": r.code})
        uid = ((r.body or {}).get("metadata") or {}).get("uid")
        return {"code": r.code, "response_uid": uid, "request_sent": r.sent, "response_received": r.received}

    def poll_until_404(self, ident: RunIdentity, poll_journal) -> dict:
        deadline = time.monotonic() + self.term_to
        n = 0
        while True:
            r = self.c.request("GET", f"/api/v1/namespaces/{ident.namespace}/pods/{ident.pod_name}")
            n += 1
            poll_journal({"n": n, "code": r.code, "sent": r.sent, "received": r.received,
                          "phase": ((r.body or {}).get("status") or {}).get("phase")})
            if r.code == 404:
                return {"termination_confirmed": r.received, "polls": n}
            if time.monotonic() > deadline:
                raise StepFailed("UN-TERM", f"no 404 within {self.term_to} s", {"polls": n})
            time.sleep(self.poll_s)
