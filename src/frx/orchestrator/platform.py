"""The real cluster behind execute_run(): kind + Tetragon on this host (Blueprint v3.0, Sections 3, 14)."""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import yaml

from .. import load as loadctl
from ..acquisition.reference import AuditWebhookSlice, TetragonGrpcObserver
from ..capacity import _kubectl_raw, _tetragon_metrics_path
from ..harness.kube import HARNESS_IDENTITY, ApiClient, KubeHarness


def _run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


class KindPlatform:
    def __init__(self, repo: Path, stores: Path, harness_kubeconfig: Path, target_image: str,
                 exec_binary: str, load_image: str, readiness_timeout_s: float,
                 audit_reference_file: Path | None = None, tetragon_reference: bool = True,
                 harness_identity: str = HARNESS_IDENTITY):
        self.repo = Path(repo)
        self.stores = {"audit": Path(stores) / "audit", "tetragon": Path(stores) / "tetragon"}
        self.client = ApiClient(harness_kubeconfig)
        self.pod_template = yaml.safe_load((self.repo / "cluster" / "manifests" / "target-pod.yaml").read_text())
        self.target_image, self.exec_binary, self.load_image = target_image, exec_binary, load_image
        self.readiness_timeout_s = readiness_timeout_s
        self.audit_reference_file = Path(audit_reference_file) if audit_reference_file else None
        self.tetragon_reference = tetragon_reference
        self.harness_identity = harness_identity
        self._commit = None

    # ---------------------------------------------------------------- identity, versions
    def code_commit(self) -> str:
        if self._commit is None:
            g = ["git", "-c", "safe.directory=*", "-C", str(self.repo)]
            dirty = _run(g + ["status", "--porcelain", "--untracked-files=no"]).stdout.strip()
            if dirty:
                raise RuntimeError(f"refusing to run from a modified working tree:\n{dirty}")
            self._commit = _run(g + ["rev-parse", "HEAD"]).stdout.strip()
        return self._commit

    def image_digests(self) -> dict:
        return {"target": self.target_image, "load": self.load_image}

    # ---------------------------------------------------------------- harness and load
    def make_harness(self, journal) -> KubeHarness:
        return KubeHarness(self.client, self.pod_template, self.target_image, self.exec_binary, journal,
                           self.readiness_timeout_s, identity=self.harness_identity)

    def set_load(self, level: str, knob: dict) -> dict:
        return loadctl.set_rate(knob["exec_per_s"], knob["api_per_s"], level)

    # ---------------------------------------------------------------- health and diagnostics
    def metrics(self) -> dict:
        tp = _tetragon_metrics_path()
        return {"tetragon_metrics": _kubectl_raw(tp) if tp else "# no tetragon pod\n",
                "apiserver_metrics": _kubectl_raw("/metrics")}

    def source_health(self) -> dict:
        ds = _run(["kubectl", "get", "ds", "tetragon", "-n", "kube-system", "-o",
                   "jsonpath={.status.numberReady}/{.status.desiredNumberScheduled}"]).stdout.strip()
        a = self.stores["audit"] / "audit.json"
        size0 = a.stat().st_size if a.exists() else -1
        time.sleep(2.0)
        size1 = a.stat().st_size if a.exists() else -1
        tet_ready = bool(ds) and ds.split("/")[0] == ds.split("/")[-1] and ds.split("/")[0] not in ("", "0")
        ref_ok = True
        ref_size = None
        if self.audit_reference_file is not None:
            ref_ok = self.audit_reference_file.exists()
            ref_size = self.audit_reference_file.stat().st_size if ref_ok else None
        ok = tet_ready and size1 > size0 >= 0 and ref_ok
        return {"ok": ok, "tetragon_ready": ds, "audit_size_change_2s": size1 - size0,
                "audit_reference_present": ref_ok, "audit_reference_size": ref_size}

    def reference_observers(self, namespace: str, sealed_ref_dir: Path) -> list:
        obs = []
        if self.tetragon_reference:
            obs.append(TetragonGrpcObserver(sealed_ref_dir / "tetragon_grpc.jsonl", namespace))
        if self.audit_reference_file is not None:
            obs.append(AuditWebhookSlice(self.audit_reference_file, sealed_ref_dir / "audit_webhook.jsonl"))
        return obs

    def kubectl_state(self) -> str:
        parts = []
        for args in (["get", "nodes", "-o", "yaml"], ["get", "namespaces", "-o", "yaml"],
                     ["get", "pods", "-A", "-o", "wide"]):
            r = _run(["kubectl", *args])
            parts.append(f"# kubectl {' '.join(args)} (rc={r.returncode})\n{r.stdout}{r.stderr}")
        return "\n---\n".join(parts)

    def kernel_log(self, since_wall_ns: int, until_wall_ns: int) -> str:
        s, u = since_wall_ns // 10**9, until_wall_ns // 10**9 + 1
        r = _run(["journalctl", "-k", "--no-pager", "-o", "short-iso-precise", "--since", f"@{s}", "--until", f"@{u}"])
        return f"# journalctl -k --since @{s} --until @{u} (rc={r.returncode})\n{r.stdout}{r.stderr}"

    def clock_status(self) -> dict:
        r = _run(["chronyc", "-n", "tracking"])
        text = r.stdout
        off = None
        m = re.search(r"System time\s*:\s*([0-9.]+) seconds (fast|slow)", text)
        if m:
            off = float(m.group(1)) * (1 if m.group(2) == "fast" else -1)
        sync = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]).stdout.strip()
        return {"offset_s": off, "ntp_synchronized": sync, "chronyc_tracking": text}
