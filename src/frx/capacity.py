"""Capacity qualification run (Blueprint v3.0, Section 14.2; G1 at a provisional ceiling load,
repeated at G3 at the highest calibration rate).

Runs the load generator for a fixed duration with both producers, a continuous follower per
store, the 1 Hz growth and resource monitors and periodic full-store snapshots, then judges
the Section 14.2 criteria from preserved files. Needs read access to the audit store, so it
is started through scripts/g1_capacity_run.sh (sudo, with the user's kubeconfig).
"""
from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import load as loadctl
from .acquisition.follower import Follower
from .monitors import GrowthMonitor, ResourceMonitor, bytes_written

DROP_RE = re.compile(r"(missed|lost|drop|audit_error|rejected)", re.I)


def _kubectl_raw(path: str) -> str:
    r = subprocess.run(["kubectl", "get", "--raw", path], capture_output=True, text=True, timeout=60)
    return r.stdout if r.returncode == 0 else f"# ERROR {r.stderr.strip()}\n"


def _tetragon_metrics_path() -> str | None:
    r = subprocess.run(["kubectl", "get", "pod", "-n", "kube-system", "-l", "app.kubernetes.io/name=tetragon",
                        "-o", "jsonpath={.items[0].metadata.name}"], capture_output=True, text=True, timeout=30)
    pod = r.stdout.strip()
    return f"/api/v1/namespaces/kube-system/pods/{pod}:2112/proxy/metrics" if pod else None


def parse_prom(text: str) -> dict[str, float]:
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        try:
            key, val = line.rsplit(" ", 1)
            out[key] = float(val)
        except ValueError:
            continue
    return out


def counter_deltas(before: str, after: str, pattern=DROP_RE) -> dict[str, float]:
    b, a = parse_prom(before), parse_prom(after)
    return {k: a[k] - b.get(k, 0.0) for k in a if pattern.search(k.split("{", 1)[0]) and a[k] - b.get(k, 0.0) != 0}


def summarise_resources(res_csv: Path, vcpus: int) -> dict:
    rows = list(csv.DictReader(open(res_csv)))
    if not rows:
        return {"samples": 0}
    total = int(rows[0]["mem_total_kb"])
    used_frac = [1 - int(r["mem_available_kb"]) / total for r in rows]
    load1 = [float(r["load1"]) for r in rows]
    busy = [float(r["cpu_busy_pct"]) for r in rows[1:]] or [0.0]
    ooms = [int(r["oom_kill_total"]) for r in rows]
    return {
        "samples": len(rows), "mem_total_kb": total,
        "peak_mem_used_fraction": round(max(used_frac), 4),
        "mean_mem_used_fraction": round(sum(used_frac) / len(used_frac), 4),
        "max_load1": max(load1), "mean_load1": round(sum(load1) / len(load1), 3), "vcpus": vcpus,
        "max_cpu_busy_pct": max(busy), "mean_cpu_busy_pct": round(sum(busy) / len(busy), 2),
        "oom_kills_during_run": ooms[-1] - ooms[0],
    }


def evaluate(resources: dict, drops_tetragon: dict, drops_apiserver: dict, followers: dict,
             growth: dict, peak_mem_max: float = 0.75) -> dict:
    """Section 14.2 pass criteria, each with the observed value."""
    fol_ok = all(not f["errors"] and f["gaps"] == 0 and not f["alive"] for f in followers.values())
    completeness = {}
    for store, f in followers.items():
        gb = growth.get(store, {}).get("bytes", 0)
        completeness[store] = (f["bytes_written"] / gb) if gb else None
    crit = {
        "zero_oom": (resources.get("oom_kills_during_run", 1) == 0, resources.get("oom_kills_during_run")),
        "peak_memory_le_75pct": (resources.get("peak_mem_used_fraction", 1) <= peak_mem_max,
                                 resources.get("peak_mem_used_fraction")),
        "load1_le_vcpus": (resources.get("max_load1", 1e9) <= resources.get("vcpus", 0), resources.get("max_load1")),
        "zero_tetragon_drops": (not drops_tetragon, drops_tetragon),
        "zero_audit_errors": (not drops_apiserver, drops_apiserver),
        "followers_clean": (fol_ok, {k: {x: v[x] for x in ("gaps", "errors", "rotations")} for k, v in followers.items()}),
        # follower bytes must match bytes the store grew by (within 1 %) - a loss check on the acquisition path
        "follower_complete_within_1pct": (all(c is not None and abs(1 - c) <= 0.01 for c in completeness.values()),
                                          completeness),
    }
    return {"pass": all(v[0] for v in crit.values()),
            "criteria": {k: {"pass": v[0], "observed": v[1]} for k, v in crit.items()}}


def run(repo: Path, stores: Path, out_root: Path, minutes: float, image: str, replicas: int,
        exec_per_s: float, api_per_s: float, warmup_s: int = 60, snapshot_every_s: int = 300,
        label: str = "G1-provisional-ceiling", keep_load: bool = False, vcpus: int | None = None) -> dict:
    import os
    vcpus = vcpus or os.cpu_count()
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = Path(out_root) / f"capacity_{ts}"
    out.mkdir(parents=True, exist_ok=False)
    journal = out / "journal.jsonl"

    def jl(state, **kw):
        with open(journal, "a") as fh:
            fh.write(json.dumps({"state": state, "mono_ns": time.monotonic_ns(), "wall_ns": time.time_ns(), **kw}) + "\n")

    params = {"label": label, "minutes": minutes, "image": image, "replicas": replicas,
              "exec_per_s_per_replica": exec_per_s, "api_per_s_per_replica": api_per_s,
              "warmup_s": warmup_s, "snapshot_every_s": snapshot_every_s, "stores": str(stores), "vcpus": vcpus}
    (out / "params.json").write_text(json.dumps(params, indent=2))
    jl("START", **params)
    tpath = _tetragon_metrics_path()
    (out / "metrics_tetragon_before.txt").write_text(_kubectl_raw(tpath) if tpath else "# no tetragon pod\n")
    (out / "metrics_apiserver_before.txt").write_text(_kubectl_raw("/metrics"))

    dep = loadctl.deploy(repo, image, replicas, exec_per_s, api_per_s, level=label)
    (out / "load_manifest.yaml").write_text(dep["manifest"])
    jl("LOAD_READY", rollout=dep["rollout"])
    time.sleep(warmup_s)
    jl("WARMUP_DONE")

    res = ResourceMonitor(out / "resources_1hz.csv")
    gro = GrowthMonitor({"audit": stores / "audit", "tetragon": stores / "tetragon"}, out / "growth_monitor_1hz.csv")
    fol = {
        "audit": Follower(stores / "audit" / "audit.json", out / "follower" / "audit.raw", out / "follower" / "audit_receipts.jsonl"),
        "tetragon": Follower(stores / "tetragon" / "tetragon.log", out / "follower" / "tetragon.raw", out / "follower" / "tetragon_receipts.jsonl"),
    }
    res.start(); gro.start()
    for f in fol.values():
        f.start()
    started = {k: f.wait_started() for k, f in fol.items()}
    jl("MONITORS_STARTED", followers_started=started)

    end = time.monotonic() + minutes * 60
    nxt_snap = time.monotonic() + snapshot_every_s
    snap_n = 0
    while time.monotonic() < end:
        if time.monotonic() >= nxt_snap:
            snap_n += 1
            sd = out / "snapshots" / f"{snap_n:02d}"
            copied = 0
            for store in ("audit", "tetragon"):
                (sd / store).mkdir(parents=True, exist_ok=True)
                for p in (stores / store).iterdir():
                    try:
                        shutil.copy2(p, sd / store / p.name); copied += 1
                    except FileNotFoundError:
                        pass            # rotated away during the copy
            jl("SNAPSHOT", n=snap_n, files=copied)
            nxt_snap += snapshot_every_s
        time.sleep(1)

    fsum = {k: f.stop_and_drain() for k, f in fol.items()}
    res.stop(); gro.stop()
    jl("MONITORS_STOPPED", followers=fsum, resource_errors=res.errors, growth_errors=gro.errors)
    (out / "metrics_tetragon_after.txt").write_text(_kubectl_raw(tpath) if tpath else "# no tetragon pod\n")
    (out / "metrics_apiserver_after.txt").write_text(_kubectl_raw("/metrics"))
    if not keep_load:
        jl("LOAD_DELETE", **loadctl.delete(wait=True))

    resources = summarise_resources(out / "resources_1hz.csv", vcpus)
    growth = bytes_written(out / "growth_monitor_1hz.csv")
    d_t = counter_deltas((out / "metrics_tetragon_before.txt").read_text(), (out / "metrics_tetragon_after.txt").read_text())
    d_a = counter_deltas((out / "metrics_apiserver_before.txt").read_text(), (out / "metrics_apiserver_after.txt").read_text())
    verdict = evaluate(resources, d_t, d_a, fsum, growth)
    report = {"params": params, "resources": resources, "store_growth": growth, "followers": fsum,
              "counter_deltas": {"tetragon": d_t, "apiserver": d_a}, "snapshots": snap_n, **verdict,
              "note": "Reference observer not yet built at G1; included in the G3 re-run (Section 14.2)."}
    (out / "capacity_report.json").write_text(json.dumps(report, indent=2))
    jl("END", passed=verdict["pass"])
    report["out_dir"] = str(out)
    return report


def reevaluate(out_dir: Path) -> dict:
    """Recompute the Section 14.2 verdict from a run's preserved files (no rerun).

    Writes capacity_report_reeval_<UTC>.json next to the original report, which is never modified.
    """
    out_dir = Path(out_dir)
    params = json.loads((out_dir / "params.json").read_text())
    fsum = None
    for line in open(out_dir / "journal.jsonl"):
        e = json.loads(line)
        if e.get("state") == "MONITORS_STOPPED":
            fsum = e["followers"]
    if fsum is None:
        raise RuntimeError("journal has no MONITORS_STOPPED record")
    resources = summarise_resources(out_dir / "resources_1hz.csv", params["vcpus"])
    growth = bytes_written(out_dir / "growth_monitor_1hz.csv")
    d_t = counter_deltas((out_dir / "metrics_tetragon_before.txt").read_text(), (out_dir / "metrics_tetragon_after.txt").read_text())
    d_a = counter_deltas((out_dir / "metrics_apiserver_before.txt").read_text(), (out_dir / "metrics_apiserver_after.txt").read_text())
    verdict = evaluate(resources, d_t, d_a, fsum, growth)
    report = {"reevaluated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "reason": "bytes_written() now tracks inode lives (inode numbers are reused after eviction)",
              "params": params, "resources": resources, "store_growth": growth, "followers": fsum,
              "counter_deltas": {"tetragon": d_t, "apiserver": d_a}, **verdict}
    path = out_dir / f"capacity_report_reeval_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    path.write_text(json.dumps(report, indent=2))
    report["written"] = str(path)
    return report
