"""Reference observers (Blueprint v3.0, Section 3.4 item 2; sealed, never used for scoring).

An independent second export per source shows whether a fact was EMITTED (Appendix D):
- Tetragon: a gRPC subscriber (`tetra getevents` inside the agent pod) filtered to the run's
  namespace, started at SOURCE_READY and stopped after acquisition closes.
- Audit: the API server's webhook backend (same policy, batch mode, <= 1 s) posting to a
  receiver on the host (`frx ref-receiver`, long-running). Each run keeps the receiver bytes
  written between SOURCE_READY and the end of the run.
Used for loss-stage attribution only if drop-free in every Q run (Section 22.2).
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _ns() -> dict:
    return {"mono_ns": time.monotonic_ns(), "wall_ns": time.time_ns()}


class TetragonGrpcObserver:
    """`kubectl exec ds/tetragon -- tetra getevents -o json --namespace <ns>` captured to a file."""

    def __init__(self, out_path: Path, namespace: str, kubectl: str = "kubectl",
                 extra_args: tuple[str, ...] = ()):
        self.out_path, self.namespace, self.kubectl = Path(out_path), namespace, kubectl
        self.cmd = [kubectl, "exec", "-n", "kube-system", "ds/tetragon", "-c", "tetragon", "--",
                    "tetra", "getevents", "-o", "json", "--namespace", namespace, *extra_args]
        self.proc = None
        self.started = self.stopped = None

    def start(self) -> None:
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self._out = open(self.out_path, "xb")
        self._err = open(self.out_path.with_suffix(".stderr.log"), "xb")
        self.proc = subprocess.Popen(self.cmd, stdout=self._out, stderr=self._err)
        self.started = _ns()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, flush_s: float = 2.0) -> dict:
        time.sleep(flush_s)             # let events of the last seconds arrive
        alive_at_stop = self.alive()
        if self.proc is not None and alive_at_stop:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self.stopped = _ns()
        for fh in (getattr(self, "_out", None), getattr(self, "_err", None)):
            if fh:
                fh.flush(); os.fsync(fh.fileno()); fh.close()
        size = self.out_path.stat().st_size if self.out_path.exists() else 0
        return {"observer": "tetragon_grpc", "cmd": self.cmd, "started": self.started,
                "stopped": self.stopped, "alive_at_stop": alive_at_stop,
                "returncode": self.proc.returncode if self.proc else None, "bytes": size,
                "healthy": bool(alive_at_stop)}


# ---------------------------------------------------------------------- audit webhook receiver
class _Handler(BaseHTTPRequestHandler):
    server_version = "frx-ref/1"

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        recv = time.time_ns()
        try:
            doc = json.loads(body)
            items = doc.get("items", []) if isinstance(doc, dict) else []
            lines = "".join(json.dumps({"_received_wall_ns": recv, **it}) + "\n" for it in items)
        except json.JSONDecodeError:
            lines = json.dumps({"_received_wall_ns": recv, "_unparsed_bytes": len(body)}) + "\n"
        with self.server.lock:          # whole lines only, so per-run slices cut at line ends
            self.server.out.write(lines.encode())
            self.server.out.flush()
            self.server.events += len(lines.splitlines())
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"events": self.server.events}).encode())

    def log_message(self, *a):        # keep stdout quiet; the output file is the record
        pass


def run_receiver(host: str, port: int, out_file: Path) -> None:
    """Long-running audit webhook receiver (start before the cluster; run in tmux)."""
    out_file.parent.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((host, port), _Handler)
    srv.lock = threading.Lock()
    srv.events = 0
    srv.out = open(out_file, "ab")
    print(json.dumps({"listening": f"http://{host}:{port}", "out": str(out_file)}), flush=True)
    try:
        srv.serve_forever()
    finally:
        srv.out.close()


class AuditWebhookSlice:
    """Per-run slice of the receiver file: bytes appended between start() and stop()."""

    def __init__(self, receiver_file: Path, out_path: Path):
        self.src, self.out_path = Path(receiver_file), Path(out_path)
        self.start_size = None
        self.started = None

    def start(self) -> None:
        self.start_size = self.src.stat().st_size if self.src.exists() else None
        self.started = _ns()

    def alive(self) -> bool:
        return self.start_size is not None

    def stop(self, flush_s: float = 3.0) -> dict:
        time.sleep(flush_s)             # batch max wait is 1 s; allow delivery and write
        res = {"observer": "audit_webhook", "receiver_file": str(self.src), "started": self.started,
               "start_size": self.start_size}
        if self.start_size is None or not self.src.exists():
            self.out_path.parent.mkdir(parents=True, exist_ok=True)
            self.out_path.write_bytes(b"")
            return {**res, "bytes": 0, "healthy": False, "reason": "receiver file missing"}
        end = self.src.stat().st_size
        with open(self.src, "rb") as fh:
            start = self.start_size
            if start > 0:
                fh.seek(start - 1)
                if fh.read(1) != b"\n":     # started mid-line: skip to the next line
                    fh.readline()
                    start = fh.tell()
            fh.seek(start)
            data = fh.read(max(0, end - start))
        cut = data.rfind(b"\n") + 1
        data = data[:cut]
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.out_path, "xb") as out:
            out.write(data)
            out.flush(); os.fsync(out.fileno())
        return {**res, "end_size": end, "bytes": len(data), "stopped": _ns(), "healthy": len(data) > 0}
