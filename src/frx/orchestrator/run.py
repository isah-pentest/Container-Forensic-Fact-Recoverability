"""One run, start to package close (Blueprint v3.0, Sections 7.1, 8.4, 9, 10, 14.3, 15, 19).

execute_run() follows the per-run loop of Section 19. Every state is journaled before the
next step; every failure maps to a disposition by rule; the package always closes with every
mandatory file, a manifest, a read-only lock and a passed completeness gate (else PK-INCOMPLETE).
Cluster access goes through a Platform object (platform.py), so the same code runs against a
simulated cluster in the tests.
"""
from __future__ import annotations

import csv
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..acquisition.follower import Follower
from ..acquisition.store import (ACTIVE_NAMES, LagSampler, finalize_raw, one_shot_copy,
                                 rotation_events, sha256_path, snapshot_store, start_state)
from ..capacity import DROP_RE, counter_deltas
from ..harness import RunIdentity
from ..harness.kube import StepFailed
from ..integrity import append_ledger, load_required, lock_readonly, verify_package, write_manifest
from ..monitors import GROWTH_HEADER, GrowthMonitor, ResourceMonitor, growth_from_rows, sample_stores
from ..status import REPO_ROOT, build_status, validate_status
from .package import Console, Package

FLAG_NAMES = ("precheck_pass", "load_check_pass", "action_verified", "termination_verified",
              "source_ready", "collector_complete", "integrity_pass", "arm_compliant",
              "carryover_flag", "oom_event", "drop_event", "outcome_observable")
OOM_MARKERS = ("Out of memory", "oom-kill", "oom_kill_process", "Killed process")


@dataclass
class RunSpec:
    run_id: str
    cohort: str                 # qualification | pilot | confirmatory | calibration
    part: str                   # Q-a, Q-b, Q-c, shakedown, BLOCK-NN ...
    cell: str                   # Q, LC, LD, MC, MD, NC, CAL
    arm: str                    # C | D
    load_level: str             # L, M, QC1 ...
    knob: dict                  # {"exec_per_s": .., "api_per_s": ..} per replica
    target_rates: dict          # {"audit": B/s, "tetragon": B/s} for the manipulation check
    block: str | None = None
    slot: int | None = None
    is_control: bool = False
    replaces_run_id: str | None = None
    prev_identity: dict | None = None        # {"namespace", "pod"} of the previous run (decoy)


@dataclass
class RunConfig:
    out_root: Path              # directory that receives <RUN-ID>/
    ledger: Path
    protocol_version: str = "3.0"
    delay_s: float = 60.0
    grace_s: float = 10.0
    epsilon_s: float = 5.0
    delay_tolerance_s: float = 2.0
    settle_s: float = 10.0
    warmup_s: float = 20.0
    manipulation_window_s: int = 15
    manipulation_tolerance: float = 0.20
    namespace_delete_timeout_s: float = 120.0
    control_idle_s: float = 10.0
    mem_min_fraction: float = 0.30
    disk_min_bytes: int = 11 * 10**9          # 3 x provisional package size + 10 GB (G4 measures)
    required_spec: Path = field(default_factory=lambda: REPO_ROOT / "protocol" / "required_files.yaml")


def _ns() -> dict:
    return {"mono_ns": time.monotonic_ns(), "wall_ns": time.time_ns()}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mem_available_fraction() -> float:
    info = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0])
    return info["MemAvailable"] / info["MemTotal"]


def _followers_alive() -> list[str]:
    return [t.name for t in threading.enumerate() if isinstance(t, Follower) and t.is_alive()]


def _process_table() -> str:
    try:
        return subprocess.run(["ps", "-eo", "pid,ppid,user,lstart,args"], capture_output=True,
                              text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError) as e:
        return f"# ps failed: {e!r}\n"


class _Abort(Exception):
    def __init__(self, code: str, msg: str):
        super().__init__(msg)
        self.code = code


def execute_run(spec: RunSpec, cfg: RunConfig, plat, rng=secrets) -> dict:
    """Run one attempt and close its package. Returns {run_id, package, status, gate}."""
    pkg = Package(Path(cfg.out_root) / spec.run_id)
    con = Console(pkg)
    journal = pkg.jsonl("timing/event_journal.jsonl")
    clock0 = plat.clock_status()
    offset_s = clock0.get("offset_s")

    def J(state: str, **kw) -> dict:
        rec = {"state": state, **_ns(), "clock_offset_s": offset_s, **kw}
        journal(rec)
        con.out(f"{spec.run_id} {state} {kw if kw else ''}")
        return rec

    short = uuid.uuid4().hex[:8]
    ident = RunIdentity(spec.run_id, f"frx-{short}", f"tgt-{short}", rng.token_hex(16))
    run_uuid = str(uuid.uuid4())
    commit = plat.code_commit()
    pkg.write_json("identity.json", {
        "run_id": spec.run_id, "run_uuid": run_uuid, "cohort": spec.cohort, "part": spec.part,
        "block": spec.block, "slot": spec.slot, "cell": spec.cell, "arm": spec.arm,
        "load_level": spec.load_level, "is_control": spec.is_control, "namespace": ident.namespace,
        "pod": ident.pod_name, "decoy_identity": spec.prev_identity,
        "replaces_run_id": spec.replaces_run_id, "protocol_version": cfg.protocol_version,
        "code_commit": commit, "images": plat.image_digests(), "harness_identity": plat.harness_identity,
        "created": _ns()})
    # Section 7.1 step 1: identity and nonce sealed before any action
    pkg.write_json("sealed/truth/run_identity.json", {"run_id": spec.run_id, "run_uuid": run_uuid,
                                                      "namespace": ident.namespace, "pod": ident.pod_name,
                                                      "nonce_hex": ident.nonce_hex, **_ns()})
    hj = pkg.mirrored_jsonl("ground_truth/harness_journal.jsonl", "sealed/truth/harness_journal.jsonl")
    harness = plat.make_harness(hj)
    receipts = pkg.jsonl("acquisition/read_receipts.jsonl")
    acq_out = open(pkg.path("acquisition/acquirer_stdout.log"), "x")
    acq_err = open(pkg.path("acquisition/acquirer_stderr.log"), "x")

    flags = {k: False for k in FLAG_NAMES}
    primary: str | None = None
    secondary: list[str] = []
    notes: list[str] = []
    truth: dict = {"namespace": ident.namespace, "pod": ident.pod_name, "uid": None,
                   "executable": None, "arguments": None, "harness_identity": plat.harness_identity,
                   "control": spec.is_control}
    t_action = t_term = None
    delay_observed = None
    followers: dict = {}
    lag = growth = None
    observers: list = []
    obs_results: list = []
    acquired: dict = {}
    reader_results: dict = {}
    namespace_created = False
    term_wall = None
    res_mon = ResourceMonitor(pkg.path("system/resources_1hz.csv"))
    res_mon.start()
    t_start_wall = time.time_ns()
    pkg.write_text("system/kubectl_pre.yaml", plat.kubectl_state())
    health_start = plat.metrics()
    pkg.write_json("source_store/health_start.json", {**_ns(), **health_start})
    load_files_written = start_state_written = False
    try:
        # ------------------------------------------------------------------ PRECHECK
        J("PRECHECK_START")
        if spec.prev_identity:
            deadline = time.monotonic() + cfg.namespace_delete_timeout_s
            first = True
            while not harness.namespace_gone(spec.prev_identity["namespace"]):
                if first:
                    flags["carryover_flag"] = True
                    notes.append("previous target namespace still present at precheck; waited")
                    first = False
                if time.monotonic() > deadline:
                    raise _Abort("PA-ISO", "previous target namespace not deleted in time")
                time.sleep(1)
        if _followers_alive():
            raise _Abort("PA-ISO", f"follower from a previous run alive: {_followers_alive()}")
        mem = _mem_available_fraction()
        disk = shutil.disk_usage(cfg.out_root).free
        J("HOST_HEADROOM", mem_available_fraction=round(mem, 4), disk_free_bytes=disk)
        if mem < cfg.mem_min_fraction or disk < cfg.disk_min_bytes:
            raise _Abort("PA-HOST", f"headroom: memory {mem:.2f}, disk {disk}")
        src_ok = plat.source_health()
        J("SOURCE_HEALTH", **src_ok)
        if not src_ok.get("ok"):
            raise _Abort("PA-SRC", f"source health: {src_ok}")
        harness.create_namespace(ident.namespace)
        namespace_created = True
        time.sleep(cfg.settle_s)
        flags["precheck_pass"] = True
        J("PRECHECK")

        # ------------------------------------------------------------------ LOAD_READY
        sp = plat.set_load(spec.load_level, spec.knob)
        pkg.write_json("load/rate_setpoint.json", {"level": spec.load_level, "knob": spec.knob,
                                                   "target_bytes_per_s": spec.target_rates, "ack": sp, **_ns()})
        J("RATE_SET", level=spec.load_level)
        time.sleep(cfg.warmup_s)
        rows = []
        for _ in range(cfg.manipulation_window_s + 1):
            rows += [dict(zip(GROWTH_HEADER, r)) for r in sample_stores(plat.stores)]
            time.sleep(1)
        g = growth_from_rows(rows)
        measured = {s: round(g.get(s, {}).get("bytes_per_s", 0.0), 1) for s in plat.stores}
        within = {s: abs(measured[s] - spec.target_rates[s]) <= cfg.manipulation_tolerance * spec.target_rates[s]
                  for s in plat.stores}
        pkg.write_json("load/load_check.json", {"window_s": cfg.manipulation_window_s,
                                                "target_bytes_per_s": spec.target_rates,
                                                "measured_bytes_per_s": measured, "within_tolerance": within,
                                                "tolerance": cfg.manipulation_tolerance, "samples": rows, **_ns()})
        load_files_written = True
        if not all(within.values()):
            raise _Abort("PA-LOAD", f"write rate outside tolerance: {measured} vs {spec.target_rates}")
        flags["load_check_pass"] = True
        J("LOAD_READY", measured_bytes_per_s=measured)

        # ------------------------------------------------------------------ SOURCE_READY
        growth = GrowthMonitor(plat.stores, pkg.path("sealed/growth_monitor_1hz.csv"))
        growth.start()
        observers = plat.reference_observers(ident.namespace, pkg.path("sealed/reference/.keep").parent)
        for ob in observers:
            ob.start()
        time.sleep(1.0)
        if not all(ob.alive() for ob in observers):
            raise _Abort("PA-SRC", "reference observer not running at SOURCE_READY")
        st0 = start_state(plat.stores)
        pkg.write_json("source_store/start_state.json", st0)
        start_state_written = True
        flags["source_ready"] = True
        J("SOURCE_READY")

        # ------------------------------------------------------------------ arm set-up
        if spec.arm == "C":
            for src, d in plat.stores.items():
                s = st0["stores"][src]
                followers[src] = Follower(Path(d) / ACTIVE_NAMES[src], pkg.path(f"acquired_raw/{src}/stream.part"),
                                          pkg.path("acquisition/read_receipts.jsonl"),
                                          start_offset=s["active_offset"], source=src)
            # receipts file is shared by the followers (their own lock); close the run appender's handle
            receipts.close()
            receipts = None
            for f in followers.values():
                f.start()
            ok = all(f.confirm_reading() for f in followers.values())
            same_inode = all(f.current_inode == st0["stores"][s]["active_inode"] for s, f in followers.items())
            if not ok or not same_inode:
                raise _Abort("PA-ISO", f"follower not confirmed at recorded offset (ok={ok}, inode={same_inode})")
            lag = LagSampler(followers, pkg.path("acquisition/follower_lag_1hz.csv"))
            lag.start()
            J("ACQ_START_C", offsets={s: st0["stores"][s]["active_offset"] for s in followers})
        else:
            alive = _followers_alive()
            table = _process_table()
            pkg.write_text("system/process_table_pre_action.txt",
                           f"# taken {_iso_now()}; follower threads alive in orchestrator: {alive}\n" + table)
            if alive:
                raise _Abort("PA-ISO", f"follower running in a D run: {alive}")
        flags["arm_compliant"] = True    # C confirmed / D no follower; D delay checked later

        # ------------------------------------------------------------------ action
        try:
            if spec.is_control:
                t_action = J("ACTION_REQUEST", control=True)
                hj({"step": "control", "note": "negative control: no create, exec or delete", **_ns()})
                time.sleep(cfg.control_idle_s)
                t_term = J("TERMINATION_CONFIRMED", control=True)
                flags["action_verified"] = flags["termination_verified"] = True
            else:
                t_action = J("ACTION_REQUEST")
                c = harness.create_pod_and_verify(ident)
                truth["uid"] = c["uid"]
                J("CREATE_CONFIRMED", uid=c["uid"], create_to_ready_s=c["create_to_ready_s"])
                e = harness.exec_nonce_and_verify(ident)
                truth["executable"], truth["arguments"] = e["executable"], e["arguments"]
                pkg.write_json("ground_truth/exec_result.json", e)
                pkg.write_json("sealed/truth/exec_result.json", e)
                J("EXEC_CONFIRMED", exit_code=e["exit_code"])
                d = harness.delete_pod_and_verify(ident)
                truth["delete_code"], truth["delete_response_uid"] = d["code"], d["response_uid"]
                J("DELETE_REQUESTED", code=d["code"])
                flags["action_verified"] = True
                tp = pkg.mirrored_jsonl("ground_truth/termination_poll.jsonl", "sealed/truth/termination_poll.jsonl")
                p = harness.poll_until_404(ident, tp)
                tp.close()
                term_wall = p["termination_confirmed"]["wall_ns"]
                t_term = J("TERMINATION_CONFIRMED", polls=p["polls"],
                           response_wall_ns=term_wall, response_mono_ns=p["termination_confirmed"]["mono_ns"])
                t_term["mono_ns"] = p["termination_confirmed"]["mono_ns"]
                flags["termination_verified"] = True
        except StepFailed as sf:
            primary = sf.code
            notes.append(str(sf))
            J("ACTION_FAILED", code=sf.code, detail=sf.detail)
            if sf.code == "UN-EXEC" and sf.detail:
                pkg.write_json("ground_truth/exec_result.json", sf.detail)
                pkg.write_json("sealed/truth/exec_result.json", sf.detail)

        # ------------------------------------------------------------------ acquisition end
        verified = flags["action_verified"] and flags["termination_verified"]
        if spec.arm == "C":
            if verified:
                wait = t_term["mono_ns"] / 1e9 + cfg.grace_s - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
            sums = {s: f.stop_and_drain() for s, f in followers.items()}
            lag.stop()
            for s, sm in sums.items():
                acq_out.write(f"follower {s}: {sm}\n")
            complete = all(not sm["errors"] and sm["gaps"] == 0 and not sm["alive"] for sm in sums.values())
            reader_results = {"followers": sums, "max_lag_bytes": lag.max_lag}
            J("ACQ_END_C", followers={s: {k: sm[k] for k in ("bytes_written", "rotations", "gaps")}
                                      for s, sm in sums.items()})
        else:
            if verified:
                target = t_term["mono_ns"] / 1e9 + cfg.delay_s
                wait = target - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
            start = J("ACQ_START_D")
            if verified:
                delay_observed = (start["mono_ns"] - t_term["mono_ns"]) / 1e9
            rr = {}
            for src, d in plat.stores.items():
                rr[src] = one_shot_copy(Path(d), ACTIVE_NAMES[src], pkg.path(f"acquired_raw/{src}/stream.part"),
                                        src, receipts)
                acq_out.write(f"reader {src}: {rr[src]}\n")
                if rr[src]["errors"]:
                    acq_err.write(f"reader {src} errors: {rr[src]['errors']}\n")
            complete = all(r["ok"] for r in rr.values())
            reader_results = {"readers": rr}
            J("ACQ_END_D", delay_observed_s=delay_observed,
              bytes={s: r["bytes"] for s, r in rr.items()})
            if verified and abs(delay_observed - cfg.delay_s) > cfg.delay_tolerance_s:
                flags["arm_compliant"] = False
                secondary.append("DV-DELAY")
        flags["collector_complete"] = bool(complete)
        for src in plat.stores:
            acquired[src] = finalize_raw(pkg.path(f"acquired_raw/{src}/stream.part"))
        J("FLUSH_ACK", sha256={s: a["sha256"] for s, a in acquired.items()},
          bytes={s: a["bytes"] for s, a in acquired.items()})
    except _Abort as ab:
        primary = ab.code
        notes.append(str(ab))
        J("PRE_ACTION_ABORT", code=ab.code, reason=str(ab))
    except Exception as ex:      # unexpected: keep everything, classify by how far the run got
        con.exception("unexpected error")
        notes.append(f"unexpected error: {ex!r}")
        if t_action is None:                       # nothing was done to the target yet
            primary = primary or ("PA-LOAD" if flags["precheck_pass"] and not flags["load_check_pass"]
                                  else "PA-ISO")
        elif not (flags["action_verified"] and flags["termination_verified"]):
            primary = primary or ("UN-CREATE" if truth["uid"] is None and not spec.is_control else
                                  "UN-EXEC" if truth["executable"] is None and not spec.is_control else
                                  "UN-DELETE" if not flags["action_verified"] else "UN-TERM")
        else:
            flags["collector_complete"] = False
        J("ERROR", error=repr(ex))
    finally:
        # stop anything still running; nothing below may raise past the package close
        for f in followers.values():
            if f.is_alive():
                try:
                    f.stop_and_drain()
                except Exception:
                    con.exception("follower stop")
        if lag is not None and lag.is_alive():
            lag.stop()
        for src in plat.stores:
            part = pkg.root / f"acquired_raw/{src}/stream.part"
            if part.exists():
                try:
                    acquired[src] = finalize_raw(part)
                except Exception:
                    con.exception("finalize")

    # ---------------------------------------------------------------------- sealed layer, cleanup
    try:
        if flags["source_ready"]:
            snaps = {src: snapshot_store(Path(d), pkg.path(f"sealed/snapshot/{src}/.x").parent, src)
                     for src, d in plat.stores.items()}
            for ob in observers:
                try:
                    obs_results.append(ob.stop())
                except Exception as ex:
                    obs_results.append({"error": repr(ex)})
                    con.exception("reference observer stop")
            if growth is not None:
                growth.stop()
            J("SNAPSHOT_SEALED", files={s: len(v["files"]) for s, v in snaps.items()},
              reference={o.get("observer", "?"): o.get("bytes") for o in obs_results})
            with open(pkg.path("sealed/growth_monitor_1hz.csv")) as fh:
                ev = rotation_events(csv.DictReader(fh))
            pkg.write_text("sealed/rotation_events.jsonl", "".join(json.dumps(e) + "\n" for e in ev))
        elif growth is not None:
            growth.stop()
    except Exception:
        con.exception("sealed layer")
        flags["collector_complete"] = False if flags["action_verified"] else flags["collector_complete"]

    health_end = plat.metrics()
    pkg.write_json("source_store/health_end.json", {**_ns(), **health_end})
    drops = {k: counter_deltas(health_start.get(k, ""), health_end.get(k, ""), DROP_RE)
             for k in ("tetragon_metrics", "apiserver_metrics")}
    ref_unhealthy = [o for o in obs_results if not o.get("healthy")]
    flags["drop_event"] = any(drops.values()) or bool(ref_unhealthy)
    if flags["drop_event"]:
        secondary.append("DV-DROP")
    if namespace_created:
        dn = harness.delete_namespace(ident.namespace, cfg.namespace_delete_timeout_s)
        J("CLEANUP", **dn)
        if not dn.get("gone"):
            notes.append("target namespace not gone within timeout (next precheck waits)")
    else:
        J("CLEANUP", note="no target namespace was created")
    res_mon.stop()
    t_end_wall = time.time_ns()
    klog = plat.kernel_log(t_start_wall, t_end_wall)
    pkg.write_text("system/kernel_log.txt", klog)
    flags["oom_event"] = any(m in klog for m in OOM_MARKERS)
    pkg.write_text("system/kubectl_post.yaml", plat.kubectl_state())
    pkg.write_json("system/collector_exits.json", {
        "reader_or_followers": reader_results, "reference_observers": obs_results,
        "resource_monitor_errors": res_mon.errors, "growth_monitor_errors": growth.errors if growth else None,
        "lag_sampler_errors": lag.errors if lag else None, "drop_counter_deltas": drops})
    clock1 = plat.clock_status()
    pkg.write_json("timing/clock_status.json", {"start": clock0, "end": clock1})

    # always-files that an early abort did not reach
    if not load_files_written:
        if not pkg.path("load/rate_setpoint.json").exists():
            pkg.write_json("load/rate_setpoint.json", {"reached": False, "notes": notes})
        pkg.write_json("load/load_check.json", {"reached": False, "notes": notes})
    if not start_state_written:
        pkg.write_json("source_store/start_state.json", {"reached": False, "notes": notes})

    # integrity: acquired files still match their FLUSH_ACK hashes
    integ = all(Path(a["path"]).exists() and sha256_path(Path(a["path"])) == a["sha256"] for a in acquired.values())
    flags["integrity_pass"] = bool(acquired) and integ
    if flags["action_verified"] and flags["termination_verified"] and flags["source_ready"] and primary is None:
        if not flags["collector_complete"]:
            primary = "CF-OOM" if flags["oom_event"] else ("CF-FOLLOW" if spec.arm == "C" else "CF-READ")
        elif not flags["integrity_pass"]:
            primary = "CF-INTEG"
    elif flags["oom_event"]:
        secondary.append("CF-OOM")
    flags["outcome_observable"] = (primary is None or primary.startswith("CF-")) and \
        flags["collector_complete"] and flags["integrity_pass"] and flags["source_ready"] and \
        flags["action_verified"] and flags["termination_verified"]

    pkg.write_json("sealed/truth/truth.json", {**truth, "flags_at_close": dict(flags), **_ns()})
    pkg.write_json("acquisition/acquirer_config.json", {
        "arm": spec.arm, "delay_s": cfg.delay_s if spec.arm == "D" else None,
        "grace_s": cfg.grace_s if spec.arm == "C" else None, "program": "frx.acquisition",
        "code_commit": commit, "stores": {s: str(d) for s, d in plat.stores.items()},
        "acquired": {s: {"sha256": a["sha256"], "bytes": a["bytes"]} for s, a in acquired.items()}})
    for fh in (acq_out, acq_err):
        fh.flush(); os.fsync(fh.fileno()); fh.close()
    if receipts is not None:
        receipts.close()
    hj.close()

    status = None
    try:
        status = build_status(
            run_id=spec.run_id, cohort=spec.cohort, block=spec.block or spec.part, slot=spec.slot,
            cell=spec.cell, arm=spec.arm, load=spec.load_level, flags=flags,
            primary_failure_code=primary, secondary_failure_codes=sorted(set(secondary)),
            replaces_run_id=spec.replaces_run_id, delay_observed_s=delay_observed,
            protocol_version=cfg.protocol_version, code_commit=commit, closed_at=_iso_now(),
            notes="; ".join(notes))
    except ValueError as e:
        con.err(f"run_status inconsistent: {e}")
        status = {"run_id": spec.run_id, "invalid": True, "error": str(e), "flags": flags,
                  "primary_failure_code": primary}
    J("PACKAGE_CLOSED", disposition=status.get("disposition"), primary_failure_code=primary)
    journal.close()
    con.close()
    pkg.write_json("run_status.json", status)
    phash = write_manifest(pkg.root)
    lock_readonly(pkg.root)
    gate = verify_package(pkg.root, load_required(cfg.required_spec), validate_status)
    append_ledger(cfg.ledger, {"run_id": spec.run_id, "block": spec.block or spec.part, "cell": spec.cell,
                               "started_at": datetime.fromtimestamp(t_start_wall / 1e9, timezone.utc).isoformat(),
                               "disposition": status.get("disposition", "INVALID"), "package_hash": phash})
    return {"run_id": spec.run_id, "package": str(pkg.root), "status": status, "gate": gate,
            "identity": {"namespace": ident.namespace, "pod": ident.pod_name}, "spec": asdict(spec)}
