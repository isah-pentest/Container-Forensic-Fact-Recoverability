"""Platform fingerprint (Blueprint v3.0, Sections 6.3, 10.4, 14.1).

Collects versions, image digests, audit flags and Helm values into one JSON document.
Read-only. Taken at G1 and at every session open; a changed `stable_hash` between sessions
is the "platform fingerprint changed" stop condition.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import time
from pathlib import Path

from .cluster import CLUSTER_NAME

NODE = f"{CLUSTER_NAME}-control-plane"


def _run(cmd: list[str], timeout: int = 30) -> dict:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"cmd": cmd, "rc": p.returncode, "out": p.stdout.strip(), "err": p.stderr.strip()[:2000]}
    except Exception as e:  # tool missing or timed out
        return {"cmd": cmd, "rc": None, "out": "", "err": repr(e)}


def _json_out(cmd: list[str]):
    r = _run(cmd)
    try:
        return json.loads(r["out"]) if r["rc"] == 0 else {"error": r["err"] or r["out"]}
    except json.JSONDecodeError:
        return {"error": "non-JSON output", "raw": r["out"][:2000]}


def _sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def collect(repo: Path) -> dict:
    fp: dict = {"taken_at_ns": time.time_ns(), "schema": "frx-fingerprint/1"}
    osr = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                osr[k] = v.strip('"')
    except OSError:
        pass
    fp["host"] = {
        "kernel": platform.release(), "machine": platform.machine(),
        "os": osr.get("PRETTY_NAME"), "python": platform.python_version(),
        "wsl": "microsoft" in platform.release().lower(),
        "mem_total_kib": next((int(l.split()[1]) for l in open("/proc/meminfo") if l.startswith("MemTotal")), None),
        "cpus": os.cpu_count(),
        "date_impl": _run(["date", "--version"])["out"].splitlines()[:1],
    }
    wslconfig = None
    for cand in Path("/mnt/c/Users").glob("*/.wslconfig") if Path("/mnt/c/Users").exists() else []:
        try:
            wslconfig = {"path": str(cand), "content": cand.read_text()}
            break
        except OSError:
            continue
    fp["host"]["wslconfig"] = wslconfig
    fp["tools"] = {
        "docker": _run(["docker", "version", "--format", "{{json .}}"])["out"],
        "kind": _run(["kind", "version"])["out"],
        "kubectl_client": _json_out(["kubectl", "version", "--client", "-o", "json"]),
        "helm": _run(["helm", "version", "--short"])["out"],
        "git_commit": _run(["git", "-C", str(repo), "describe", "--tags", "--always", "--dirty"])["out"],
    }
    fp["cluster"] = {
        "node_container_image": _run(["docker", "inspect", NODE, "--format", "{{.Config.Image}}"])["out"],
        "node_container_image_id": _run(["docker", "inspect", NODE, "--format", "{{.Image}}"])["out"],
        "server_version": _json_out(["kubectl", "version", "-o", "json"]).get("serverVersion"),
        "node_info": (_json_out(["kubectl", "get", "node", NODE, "-o", "json"]).get("status", {}) or {}).get("nodeInfo"),
        "apiserver_audit_args": [l.strip() for l in _run(
            ["docker", "exec", NODE, "grep", "audit", "/etc/kubernetes/manifests/kube-apiserver.yaml"])["out"].splitlines()],
        "tetragon_release": _json_out(["helm", "list", "-n", "kube-system", "-f", "^tetragon$", "-o", "json"]),
        "tetragon_values_all": _json_out(["helm", "get", "values", "tetragon", "-n", "kube-system", "--all", "-o", "json"]),
        "tetragon_image_ids": _run(["kubectl", "get", "pods", "-n", "kube-system", "-l", "app.kubernetes.io/name=tetragon",
                                    "-o", "jsonpath={range .items[*].status.containerStatuses[*]}{.name}={.imageID}{\"\\n\"}{end}"])["out"].splitlines(),
    }
    fp["files"] = {rel: _sha(repo / rel) for rel in (
        "cluster/audit-policy.yaml", "protocol/contract.yaml", "protocol/parameters.yaml",
        "protocol/required_files.yaml", "protocol/schemas/run_status.schema.json")}
    # stable part: everything except timestamps and live counters
    stable = {k: fp[k] for k in ("host", "tools", "cluster", "files")}
    stable = json.loads(json.dumps(stable, sort_keys=True))
    stable["tools"].pop("git_commit", None)
    fp["stable_hash"] = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()
    return fp
