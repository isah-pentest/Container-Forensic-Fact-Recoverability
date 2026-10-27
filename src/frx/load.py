"""Persistent rate-controlled load generator control (Blueprint v3.0, Section 6.2).

Renders cluster/manifests/load-generator.yaml with a digest-pinned image, the replica count
and the load script, applies it, switches its rate through ConfigMap frx-load-rate, and
deletes it. Rates are per replica: exec_per_s starts /bin/true; api_per_s GETs the ConfigMap.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

NAMESPACE = "frx-load"
RATE_CM = "frx-load-rate"


def _run(cmd: list[str], stdin: str | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)


def resolve_image_digest(ref: str) -> str:
    """Pull an image tag with docker and return the registry digest reference."""
    r = _run(["docker", "pull", ref], timeout=900)
    if r.returncode:
        raise RuntimeError(f"docker pull {ref} failed: {r.stderr.strip()}")
    r = _run(["docker", "inspect", "--format", "{{index .RepoDigests 0}}", ref])
    digest_ref = r.stdout.strip()
    if r.returncode or "@sha256:" not in digest_ref:
        raise RuntimeError(f"no registry digest for {ref}: {r.stderr.strip() or digest_ref}")
    name, digest = digest_ref.split("@", 1)
    if "/" not in name:
        name = f"docker.io/library/{name}"
    elif name.count("/") == 1 and "." not in name.split("/")[0]:
        name = f"docker.io/{name}"
    return f"{name}@{digest}"


def render(repo: Path, image: str, replicas: int, exec_per_s: float, api_per_s: float,
           level: str = "CEILING") -> str:
    if "@sha256:" not in image:
        raise ValueError("load image must be pinned by digest")
    if replicas < 1:
        raise ValueError("replicas must be >= 1")
    docs = list(yaml.safe_load_all((repo / "cluster" / "manifests" / "load-generator.yaml").read_text()))
    script = (repo / "cluster" / "load" / "loadgen.py").read_text()
    for d in docs:
        kind, name = d.get("kind"), (d.get("metadata") or {}).get("name")
        if kind == "ConfigMap" and name == RATE_CM:
            d["data"] = {"level": str(level), "exec_per_s": str(exec_per_s), "api_per_s": str(api_per_s)}
        elif kind == "ConfigMap" and name == "frx-loadgen-script":
            d["data"] = {"loadgen.py": script}
        elif kind == "Deployment":
            d["spec"]["replicas"] = int(replicas)
            d["spec"]["template"]["spec"]["containers"][0]["image"] = image
    return yaml.safe_dump_all(docs, sort_keys=False)


def deploy(repo: Path, image: str, replicas: int, exec_per_s: float, api_per_s: float,
           level: str = "CEILING", ready_timeout_s: int = 300) -> dict:
    manifest = render(repo, image, replicas, exec_per_s, api_per_s, level)
    r = _run(["kubectl", "apply", "-f", "-"], stdin=manifest)
    if r.returncode:
        raise RuntimeError(f"apply failed: {r.stderr.strip()}")
    w = _run(["kubectl", "rollout", "status", "-n", NAMESPACE, "deploy/frx-load",
              f"--timeout={ready_timeout_s}s"], timeout=ready_timeout_s + 30)
    if w.returncode:
        raise RuntimeError(f"load generator not ready: {w.stderr.strip() or w.stdout.strip()}")
    return {"applied": r.stdout.strip().splitlines(), "rollout": w.stdout.strip(), "manifest": manifest}


def set_rate(exec_per_s: float, api_per_s: float, level: str) -> dict:
    patch = json.dumps({"data": {"level": str(level), "exec_per_s": str(exec_per_s),
                                 "api_per_s": str(api_per_s)}})
    r = _run(["kubectl", "patch", "configmap", RATE_CM, "-n", NAMESPACE, "--type", "merge", "-p", patch])
    if r.returncode:
        raise RuntimeError(f"rate patch failed: {r.stderr.strip()}")
    return {"level": level, "exec_per_s": exec_per_s, "api_per_s": api_per_s, "out": r.stdout.strip()}


def delete(wait: bool = True) -> dict:
    r = _run(["kubectl", "delete", "namespace", NAMESPACE, f"--wait={'true' if wait else 'false'}",
              "--ignore-not-found"], timeout=600)
    return {"rc": r.returncode, "out": r.stdout.strip(), "err": r.stderr.strip()}
