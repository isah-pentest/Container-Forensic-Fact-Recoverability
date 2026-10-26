"""G1 smoke test: does each source record the contract facts at all? (Blueprint v3.0, Sections 7, 18)

One create -> exec -> delete in a throw-away namespace, then a read of both source-side
stores. It reports, per fact, the candidate records and the fields G2 must decide on
(objectRef.name on create, response UID, exec requestURI decoding, Tetragon pod metadata,
binary path). It never writes into the evidence store's runs/ tree and is not a study run.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml


def _kubectl(args: list[str], stdin: str | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", *args], input=stdin, capture_output=True, text=True, timeout=timeout)


def _journal(log: list, state: str, **kw) -> None:
    log.append({"state": state, "mono_ns": time.monotonic_ns(), "wall_ns": time.time_ns(), **kw})


def _read_store(dir_path: Path, use_sudo: bool) -> list[tuple[str, str]]:
    """Return (file name, text) for every file in a store directory, oldest name first."""
    if use_sudo:
        ls = subprocess.run(["sudo", "ls", "-1", str(dir_path)], capture_output=True, text=True, check=True)
        names = sorted(n for n in ls.stdout.split() if n)
        out = []
        for n in names:
            r = subprocess.run(["sudo", "cat", str(dir_path / n)], capture_output=True, text=True)
            if r.returncode == 0:
                out.append((n, r.stdout))
        return out
    return [(p.name, p.read_text(errors="replace")) for p in sorted(dir_path.iterdir()) if p.is_file()]


def _jsonl(files: list[tuple[str, str]]):
    for name, text in files:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield name, json.loads(line)
            except json.JSONDecodeError:
                continue


def _get(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def analyse(audit_files, tetragon_files, ns: str, pod: str, nonce: str, uid: str) -> dict:
    marker = f"FRX-{nonce}"
    res: dict = {"F1_create": [], "F2_audit_exec": [], "F3_delete": [],
                 "F2_tetragon_exec": [], "F4_tetragon_exit": [], "other_pod_audit": 0}
    for fname, ev in _jsonl(audit_files):
        if ev.get("kind") != "Event":
            continue
        ref = ev.get("objectRef") or {}
        if ref.get("resource") != "pods" or ref.get("namespace") != ns:
            continue
        brief = {
            "file": fname, "auditID": ev.get("auditID"), "stage": ev.get("stage"), "verb": ev.get("verb"),
            "objectRef.name": ref.get("name"), "subresource": ref.get("subresource"),
            "code": _get(ev, "responseStatus.code"), "user": _get(ev, "user.username"),
            "responseObject.uid": _get(ev, "responseObject.metadata.uid"),
            "stageTimestamp": ev.get("stageTimestamp"),
        }
        brief["uid_matches_truth"] = brief["responseObject.uid"] == uid if brief["responseObject.uid"] else None
        if ref.get("subresource") == "exec":
            q = parse_qs(urlparse(ev.get("requestURI", "")).query)
            brief["decoded_command"] = q.get("command")
            brief["nonce_in_command"] = marker in (q.get("command") or [])
            res["F2_audit_exec"].append(brief)
        elif ref.get("subresource"):
            res["other_pod_audit"] += 1
        elif ev.get("verb") == "create":
            res["F1_create"].append(brief)
        elif ev.get("verb") == "delete":
            res["F3_delete"].append(brief)
        else:
            res["other_pod_audit"] += 1
    res["other_pod_tetragon_exec"] = 0
    res["other_pod_tetragon_exit"] = 0
    for fname, ev in _jsonl(tetragon_files):
        for key in ("process_exec", "process_exit"):
            body = ev.get(key)
            if not body:
                continue
            proc = body.get("process") or {}
            podinfo = proc.get("pod") or {}
            args = proc.get("arguments") or ""
            binary = proc.get("binary") or ""
            in_pod = podinfo.get("namespace") == ns and podinfo.get("name") == pod
            entry = {
                "file": fname, "time": ev.get("time"), "binary": binary, "arguments": args,
                "pod.namespace": podinfo.get("namespace"), "pod.name": podinfo.get("name"),
                "container.id": _get(podinfo, "container.id"), "nonce_in_arguments": marker in args,
                "pod_metadata_matches": in_pod,
            }
            if key == "process_exec":
                if marker in args:
                    res["F2_tetragon_exec"].append(entry)
                elif in_pod:
                    res["other_pod_tetragon_exec"] += 1
            else:
                # F4: exit of the container's main process (sleep infinity)
                if in_pod and binary.endswith("/sleep") and args.strip() == "infinity":
                    res["F4_tetragon_exit"].append(entry)
                elif in_pod:
                    res["other_pod_tetragon_exit"] += 1
    res["verdict"] = {
        "F1 audit create (201 + UID)": any(b["code"] == 201 and b["uid_matches_truth"] for b in res["F1_create"]),
        "F1 objectRef.name present on create": any(b["objectRef.name"] == pod for b in res["F1_create"]),
        "F2 audit exec with nonce": any(b["nonce_in_command"] for b in res["F2_audit_exec"]),
        "F2 tetragon exec with nonce": any(e["nonce_in_arguments"] for e in res["F2_tetragon_exec"]),
        "F2 tetragon exec has pod metadata": any(e["nonce_in_arguments"] and e["pod_metadata_matches"]
                                                for e in res["F2_tetragon_exec"]),
        "F3 audit delete (2xx)": any((b["code"] or 0) // 100 == 2 for b in res["F3_delete"]),
        "F3 response UID matches": any(b["uid_matches_truth"] for b in res["F3_delete"]),
        "F4 tetragon exit in pod": bool(res["F4_tetragon_exit"]),
    }
    return res


def run(repo: Path, stores: Path, out_dir: Path, image: str, exec_binary: str,
        settle_s: float = 5.0, use_sudo_for_audit: bool = True) -> dict:
    short = secrets.token_hex(4)
    nonce = secrets.token_hex(16)
    ns, pod = f"frx-smoke-{short}", f"tgt-{short}"
    log: list = []
    report: dict = {"namespace": ns, "pod": pod, "image": image, "exec_binary": exec_binary, "journal": log}
    manifest = yaml.safe_load((repo / "cluster" / "manifests" / "target-pod.yaml").read_text())
    manifest["metadata"]["name"], manifest["metadata"]["namespace"] = pod, ns
    manifest["spec"]["containers"][0]["image"] = image
    try:
        r = _kubectl(["create", "namespace", ns]); _journal(log, "NAMESPACE", rc=r.returncode, err=r.stderr)
        if r.returncode:
            raise RuntimeError(f"namespace create failed: {r.stderr}")
        _journal(log, "ACTION_REQUEST")
        r = _kubectl(["create", "-f", "-", "-o", "json"], stdin=json.dumps(manifest))
        if r.returncode:
            raise RuntimeError(f"pod create failed: {r.stderr}")
        uid = json.loads(r.stdout)["metadata"]["uid"]
        _journal(log, "CREATE_RESPONSE", uid=uid)
        r = _kubectl(["wait", "-n", ns, f"pod/{pod}", "--for=condition=Ready", "--timeout=180s"], timeout=200)
        if r.returncode:
            raise RuntimeError(f"pod not Ready: {r.stderr}")
        _journal(log, "CREATE_CONFIRMED")
        r = _kubectl(["exec", "-n", ns, pod, "-c", "target", "--", exec_binary, f"FRX-{nonce}"])
        ok = r.returncode == 0 and f"FRX-{nonce}" in r.stdout
        _journal(log, "EXEC_CONFIRMED" if ok else "EXEC_FAILED", rc=r.returncode,
                 stdout=r.stdout.strip(), stderr=r.stderr.strip())
        if not ok:
            raise RuntimeError("exec not verified")
        r = _kubectl(["delete", "pod", "-n", ns, pod, "--wait=false"])
        _journal(log, "DELETE_REQUESTED", rc=r.returncode, out=r.stdout.strip(), err=r.stderr.strip())
        if r.returncode:
            raise RuntimeError(f"delete failed: {r.stderr}")
        deadline = time.monotonic() + 120
        while True:
            g = _kubectl(["get", "pod", "-n", ns, pod, "-o", "name"], timeout=30)
            if g.returncode and "NotFound" in g.stderr:
                _journal(log, "TERMINATION_CONFIRMED")
                break
            if time.monotonic() > deadline:
                raise RuntimeError("termination not confirmed within 120 s")
            time.sleep(0.5)
        time.sleep(settle_s)
        _journal(log, "READ_STORES")
        audit = _read_store(stores / "audit", use_sudo_for_audit)
        tet = _read_store(stores / "tetragon", False)
        report["store_files"] = {"audit": [(n, len(t)) for n, t in audit], "tetragon": [(n, len(t)) for n, t in tet]}
        report["uid"] = uid
        report["results"] = analyse(audit, tet, ns, pod, nonce, uid)
        report["action_duration_s"] = (log[-2]["mono_ns"] - next(e for e in log if e["state"] == "ACTION_REQUEST")["mono_ns"]) / 1e9
    except Exception as e:  # report everything; never hide a failure
        report["error"] = repr(e)
    finally:
        _kubectl(["delete", "namespace", ns, "--wait=false"])
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"smoke_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{short}.json"
    path.write_text(json.dumps(report, indent=2))
    report["report_path"] = str(path)
    return report
