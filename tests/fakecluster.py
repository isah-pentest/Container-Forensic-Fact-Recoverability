"""A simulated cluster for end-to-end tests of execute_run(): two lumberjack-style rotating
stores fed by background producers, and a harness whose actions emit the records a real API
server and Tetragon would. Synthetic fixtures only; never real study data."""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from frx.harness.kube import StepFailed

HARNESS = "system:serviceaccount:frx-system:frx-harness"


def ts(ns: int | None = None) -> str:
    ns = ns or time.time_ns()
    dt = datetime.fromtimestamp(ns // 10**9, timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ns % 10**9:09d}Z"


class Producer(threading.Thread):
    """Writes background lines at bytes_per_s, rotates at max_bytes, keeps `backups` backups."""

    def __init__(self, d: Path, active: str, max_bytes: int, backups: int, bytes_per_s: float, kind: str):
        super().__init__(daemon=True)
        self.d, self.active, self.max, self.k, self.rate, self.kind = d, active, max_bytes, backups, bytes_per_s, kind
        self.q: queue.Queue = queue.Queue()
        self._stop_evt = threading.Event()
        self.fh = open(d / active, "ab")
        self.n = 0

    def _bg(self) -> bytes:
        self.n += 1
        if self.kind == "audit":
            ev = {"kind": "Event", "auditID": str(uuid.uuid4()), "stage": "ResponseComplete", "verb": "get",
                  "objectRef": {"resource": "configmaps", "namespace": "frx-load", "name": "frx-load-rate"},
                  "user": {"username": "system:serviceaccount:frx-load:frx-load"},
                  "responseStatus": {"code": 200}, "stageTimestamp": ts(), "pad": "x" * 300}
        else:
            ev = {"process_exec": {"process": {"binary": "/bin/true", "arguments": "", "exec_id": str(uuid.uuid4()),
                                               "pod": {"namespace": "frx-load", "name": "frx-load-x"}}},
                  "time": ts(), "pad": "y" * 300}
        return (json.dumps(ev) + "\n").encode()

    def _write(self, line: bytes) -> None:
        if self.fh.tell() + len(line) > self.max and self.fh.tell() > 0:
            self.fh.close()
            stem, ext = os.path.splitext(self.active)
            name = f"{stem}-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%S.%f')}{ext}"
            os.rename(self.d / self.active, self.d / name)
            self.fh = open(self.d / self.active, "ab")
            backups = sorted(p for p in os.listdir(self.d) if p.startswith(stem + "-"))
            for old in backups[:-self.k] if len(backups) > self.k else []:
                os.remove(self.d / old)
        self.fh.write(line)
        self.fh.flush()

    def run(self) -> None:
        nxt = time.monotonic()
        while not self._stop_evt.is_set():
            while not self.q.empty():
                self._write(self.q.get())
            line = self._bg()
            self._write(line)
            nxt += len(line) / self.rate
            time.sleep(max(0.0, min(0.05, nxt - time.monotonic())))

    def emit(self, ev: dict) -> None:
        self.q.put((json.dumps(ev) + "\n").encode())

    def stop(self) -> None:
        self._stop_evt.set()
        self.join(5)
        self.fh.close()


class FakeHarness:
    def __init__(self, cluster, journal):
        self.c, self.journal = cluster, journal
        self.uids: dict = {}

    def _j(self, step, **kw):
        self.journal({"step": step, "mono_ns": time.monotonic_ns(), "wall_ns": time.time_ns(), **kw})

    def create_namespace(self, ns):
        self.c.namespaces.add(ns); self._j("namespace_create", code=201)
        return {"code": 201}

    def namespace_gone(self, ns):
        return ns not in self.c.namespaces

    def delete_namespace(self, ns, timeout_s):
        self.c.namespaces.discard(ns); self._j("namespace_delete", code=200)
        return {"code": 200, "gone": True}

    def create_pod_and_verify(self, ident):
        uid = str(uuid.uuid4())
        self.uids[ident.pod_name] = uid
        self.c.audit.emit({"kind": "Event", "auditID": str(uuid.uuid4()), "stage": "ResponseComplete", "verb": "create",
                           "objectRef": {"resource": "pods", "namespace": ident.namespace, "name": ident.pod_name},
                           "user": {"username": HARNESS}, "responseStatus": {"code": 201},
                           "responseObject": {"metadata": {"uid": uid}}, "stageTimestamp": ts()})
        self._j("create", code=201, uid=uid)
        time.sleep(0.2)
        return {"code": 201, "uid": uid, "create_to_ready_s": 0.2}

    def exec_nonce_and_verify(self, ident):
        marker = f"FRX-{ident.nonce_hex}"
        if self.c.fail_exec:
            raise StepFailed("UN-EXEC", "simulated exec failure", {"exit_code": 1, "stdout": "", "stderr": "boom"})
        aid = str(uuid.uuid4())
        uri = f"/api/v1/namespaces/{ident.namespace}/pods/{ident.pod_name}/exec?command=%2Fusr%2Fbin%2Fecho&command={quote(marker)}&container=target&stdout=true"
        for stage in ("ResponseStarted", "ResponseComplete"):
            self.c.audit.emit({"kind": "Event", "auditID": aid, "stage": stage, "verb": "get", "requestURI": uri,
                               "objectRef": {"resource": "pods", "subresource": "exec", "namespace": ident.namespace,
                                             "name": ident.pod_name},
                               "user": {"username": HARNESS}, "responseStatus": {"code": 101}, "stageTimestamp": ts()})
        self.c.tetragon.emit({"process_exec": {"process": {"binary": "/usr/bin/echo", "arguments": marker,
                                                           "exec_id": str(uuid.uuid4()),
                                                           "pod": {"namespace": ident.namespace, "name": ident.pod_name}}},
                              "time": ts()})
        res = {"executable": "/usr/bin/echo", "arguments": [marker], "exit_code": 0, "stdout": marker + "\n",
               "stderr": "", "command": ["/usr/bin/echo", marker]}
        self._j("exec", exit_code=0)
        return res

    def delete_pod_and_verify(self, ident):
        for user in (HARNESS, "system:node:frx-control-plane"):
            self.c.audit.emit({"kind": "Event", "auditID": str(uuid.uuid4()), "stage": "ResponseComplete", "verb": "delete",
                               "objectRef": {"resource": "pods", "namespace": ident.namespace, "name": ident.pod_name},
                               "user": {"username": user}, "responseStatus": {"code": 200},
                               "responseObject": {"metadata": {"uid": self.uids[ident.pod_name]}}, "stageTimestamp": ts()})
        self.c.tetragon.emit({"process_exit": {"process": {"binary": "/usr/bin/sleep", "arguments": "infinity",
                                                           "exec_id": str(uuid.uuid4()),
                                                           "pod": {"namespace": ident.namespace, "name": ident.pod_name}}},
                              "time": ts()})
        self._j("delete", code=200)
        return {"code": 200, "response_uid": None}

    def poll_until_404(self, ident, poll_journal):
        time.sleep(0.2)
        r = {"mono_ns": time.monotonic_ns(), "wall_ns": time.time_ns()}
        poll_journal({"n": 1, "code": 404, "received": r})
        return {"termination_confirmed": r, "polls": 1}


class FakeObserver:
    def __init__(self, out: Path):
        self.out = out

    def start(self):
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.out.write_text('{"observer":"fake"}\n')

    def alive(self):
        return True

    def stop(self):
        return {"observer": "fake", "bytes": self.out.stat().st_size, "healthy": True}


class FakeCluster:
    harness_identity = HARNESS
    load_image = "example/load@sha256:" + "0" * 64

    def __init__(self, root: Path, audit_rate=20_000, tet_rate=20_000, max_bytes=200_000, backups=2):
        self.stores = {"audit": root / "stores" / "audit", "tetragon": root / "stores" / "tetragon"}
        for d in self.stores.values():
            d.mkdir(parents=True)
        self.audit = Producer(self.stores["audit"], "audit.json", max_bytes, backups, audit_rate, "audit")
        self.tetragon = Producer(self.stores["tetragon"], "tetragon.log", max_bytes, backups, tet_rate, "tetragon")
        self.namespaces: set = set()
        self.fail_exec = False
        self.audit.start(); self.tetragon.start()

    def set_rates(self, audit=None, tetragon=None):
        if audit:
            self.audit.rate = audit
        if tetragon:
            self.tetragon.rate = tetragon

    def stop(self):
        self.audit.stop(); self.tetragon.stop()

    # Platform interface
    def code_commit(self): return "abc1234"
    def image_digests(self): return {"target": "example/t@sha256:" + "1" * 64, "load": self.load_image}
    def make_harness(self, journal): return FakeHarness(self, journal)
    def set_load(self, level, knob): return {"level": level, **knob}
    def metrics(self): return {"tetragon_metrics": "tetragon_events_total 1\n", "apiserver_metrics": "apiserver_x 1\n"}
    def source_health(self): return {"ok": True}
    def reference_observers(self, ns, d): return [FakeObserver(d / "fake_reference.jsonl")]
    def kubectl_state(self): return "# fake kubectl state\n"
    def kernel_log(self, a, b): return f"# fake kernel log {a}-{b}\n"
    def clock_status(self): return {"offset_s": 0.0, "ntp_synchronized": "yes"}
