"""Blocks and sessions (Blueprint v3.0, Sections 8.2, 10.4, 15.1, 15.3-15.4, 19).

Used by the pilot (G4, PILOT- IDs) and, after freeze, the confirmatory cohort (G6, RUN- IDs).

- open_session():   session_open.json (fingerprint, clock, ledger hash, code and protocol hashes);
                    refuses to open if the platform fingerprint differs from the reference.
- run_block():      one load Deployment per block, the five seeded slots in order, replacements of
                    PRE_ACTION_ABORT / UNSCORABLE attempts at the end of the same block, the package
                    gate after every run, stop rules, then off-host replication and the block gate.
- close_session():  re-verifies every package of the session and records the ledger hash.

A block never spans sessions: a block directory is created once; a block found started but not
closed is recorded INCOMPLETE and is not resumed (Section 8.2).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..integrity import lock_readonly, verify_block, verify_hashes, write_once
from .package import next_run_id
from .run import RunConfig, RunSpec, execute_run


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_once(path, (json.dumps(obj, indent=2, default=str) + "\n").encode())


# ---------------------------------------------------------------------- settings from parameters
def level_settings(params: dict, level: str) -> tuple[dict, dict]:
    """(per-replica knob, target bytes/s per store) for L, M or a calibration level."""
    ld = params["load"]
    if level in ("L", "M"):
        rate = ld["rate_low_bytes_per_s" if level == "L" else "rate_moderate_bytes_per_s"]
        knob = {"exec_per_s": ld["per_replica_exec_per_s"][level], "api_per_s": ld["per_replica_api_per_s"][level]}
        return knob, {"audit": float(rate["audit"]), "tetragon": float(rate["tetragon"])}
    c = ld["calibration"][level]
    return ({"exec_per_s": c["exec_per_s"], "api_per_s": c["api_per_s"]},
            {k: float(v) for k, v in c["target_bytes_per_s"].items()})


def run_config(params: dict, out_root: Path, ledger: Path) -> RunConfig:
    t, h = params["timing"], params["host"]
    for k in ("readiness_timeout_s", "termination_timeout_s", "namespace_delete_timeout_s", "control_idle_s"):
        if not isinstance(t.get(k), (int, float)):
            raise ValueError(f"parameters.timing.{k} is not set ({t.get(k)!r})")
    disk = h.get("disk_free_min_bytes")
    return RunConfig(out_root=out_root, ledger=ledger, protocol_version=str(params["protocol_version"]),
                     delay_s=t["delay_s"], grace_s=t["grace_s"], epsilon_s=t["epsilon_s"],
                     delay_tolerance_s=t["delay_tolerance_s"], settle_s=t["settle_s"], warmup_s=t["load_warmup_s"],
                     manipulation_window_s=t["manipulation_window_s"],
                     manipulation_tolerance=params["load"]["manipulation_tolerance"],
                     namespace_delete_timeout_s=t["namespace_delete_timeout_s"], control_idle_s=t["control_idle_s"],
                     mem_min_fraction=h["preaction_memory_min_fraction"],
                     **({"disk_min_bytes": int(disk)} if isinstance(disk, (int, float)) else {}))


# ---------------------------------------------------------------------- sessions
def open_session(sessions_root: Path, plat, ledger: Path, fingerprint: dict, reference_hash: str | None,
                 protocol_files: list[Path], label: str) -> dict:
    """Create sessions_root/SESSION-YYYYMMDD-NN with session_open.json. Raises on fingerprint change."""
    day = time.strftime("%Y%m%d", time.gmtime())
    sessions_root.mkdir(parents=True, exist_ok=True)
    n = 1 + sum(1 for p in sessions_root.glob(f"SESSION-{day}-*"))
    sdir = sessions_root / f"SESSION-{day}-{n:02d}"
    sdir.mkdir()
    stable = fingerprint.get("stable_hash")
    rec = {"session": sdir.name, "label": label, "opened_at": _now_iso(), "code_commit": plat.code_commit(),
           "fingerprint_stable_hash": stable, "reference_fingerprint": reference_hash,
           "fingerprint_matches": reference_hash is None or stable == reference_hash,
           "clock": plat.clock_status(), "ledger_sha256": _sha(ledger),
           "protocol_sha256": {str(p.name): _sha(p) for p in protocol_files}}
    _dump(sdir / "session_open.json", rec)
    _dump(sdir / "fingerprint.json", fingerprint)
    if not rec["fingerprint_matches"]:
        raise RuntimeError(f"platform fingerprint changed ({stable} != {reference_hash}); stop and audit (Section 10.4)")
    return {"dir": sdir, **rec}


def close_session(sdir: Path, plat, ledger: Path, packages: list[Path]) -> dict:
    problems = {}
    for p in packages:
        bad = verify_hashes(p)
        if bad:
            problems[p.name] = bad
    rec = {"closed_at": _now_iso(), "packages_reverified": len(packages), "hash_problems": problems,
           "ledger_sha256": _sha(ledger), "clock": plat.clock_status()}
    _dump(sdir / "session_close.json", rec)
    return rec


# ---------------------------------------------------------------------- replication
def _copy_package(src: Path, dst: Path) -> None:
    """Byte copy of a package (no metadata: CIFS/SMB replicas reject some mode and time changes).

    Each file is fsynced so it reaches the server; the replica is then verified by hash.
    """
    import os
    tmp = dst.with_name(dst.name + ".partial")
    if tmp.exists():
        shutil.rmtree(tmp)
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        (tmp / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            with open(Path(root) / f, "rb") as a, open(tmp / rel / f, "xb") as b:
                shutil.copyfileobj(a, b, 1 << 20)
                b.flush()
                os.fsync(b.fileno())
    os.rename(tmp, dst)                     # a package appears in the replica only when complete


def replicate(packages: list[Path], replica_runs: Path) -> dict:
    """Copy closed packages to the off-host replica and verify them there (Section 15.3 item 4)."""
    replica_runs.mkdir(parents=True, exist_ok=True)
    copied, lock_errors = [], []
    for pkg in packages:
        dst = replica_runs / pkg.name
        if not dst.exists():
            _copy_package(pkg, dst)
            try:
                lock_readonly(dst)
            except OSError as e:            # best effort on SMB; integrity rests on the hashes
                lock_errors.append(f"{pkg.name}: {e!r}")
            copied.append(pkg.name)
    v = verify_block(packages, replica_runs)
    return {"replica_root": str(replica_runs), "copied": copied, "packages": [p.name for p in packages],
            "lock_errors": lock_errors, **v}


# ---------------------------------------------------------------------- one block
@dataclass
class BlockContext:
    prefix: str                     # PILOT | RUN | CAL
    cohort: str                     # pilot | confirmatory | calibration
    cfg: RunConfig
    plat: object
    params: dict
    session_dir: Path
    replica_runs: Path
    progress: Path
    log: object = print
    prev_identity: dict | None = None
    consecutive_oom: int = 0
    per_cell_block_ceiling: int = 2
    block_warmup_s: float | None = None           # None: parameters.timing.load_warmup_block_start_s
    runs: list = field(default_factory=list)


def _progress(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


def read_progress(path: Path) -> list[dict]:
    return [json.loads(l) for l in open(path)] if path.exists() else []


def run_block(block: str, slots: list[dict], ctx: BlockContext) -> dict:
    """slots: schedule rows of one block in slot order ({slot, cell, load, arm, is_control})."""
    bdir = ctx.session_dir / "blocks" / block
    bdir.mkdir(parents=True, exist_ok=False)          # never reused: a block never spans sessions
    t0 = _now_iso()
    _progress(ctx.progress, {"event": "BLOCK_OPEN", "block": block, "session": ctx.session_dir.name, "at": t0})
    replicas = int(ctx.params["load"]["replicas"])
    knob0, _ = level_settings(ctx.params, slots[0]["load"])
    dep = ctx.plat.deploy_load(replicas, knob0, slots[0]["load"])
    _dump(bdir / "load_deployment.json", {"replicas": replicas, "deployed_at": _now_iso(),
                                          **{k: v for k, v in dep.items() if k != "manifest"}})
    write_once(bdir / "load_deployment.yaml", dep.get("manifest", "").encode())
    time.sleep(ctx.block_warmup_s if ctx.block_warmup_s is not None
               else ctx.params["timing"]["load_warmup_block_start_s"])

    queue = [(s, None) for s in slots]
    fails: dict[str, int] = {}
    packages: list[Path] = []
    results, stopped = [], None
    try:
        while queue:
            s, replaces = queue.pop(0)
            knob, targets = level_settings(ctx.params, s["load"])
            spec = RunSpec(run_id=next_run_id(ctx.prefix, ctx.cfg.ledger, Path(ctx.cfg.out_root)),
                           cohort=ctx.cohort, part=block, cell=s["cell"], arm=s["arm"], load_level=s["load"],
                           knob=knob, target_rates=targets, block=block, slot=int(s["slot"]),
                           is_control=bool(int(s["is_control"])), replaces_run_id=replaces,
                           prev_identity=ctx.prev_identity)
            r = execute_run(spec, ctx.cfg, ctx.plat)
            st = r["status"]
            disp = st.get("disposition", "INVALID")
            pkg = Path(r["package"])
            packages.append(pkg)
            rec = {"event": "RUN", "block": block, "slot": spec.slot, "cell": spec.cell, "arm": spec.arm,
                   "load": spec.load_level, "run_id": r["run_id"], "disposition": disp,
                   "primary_failure_code": st.get("primary_failure_code"), "replaces_run_id": replaces,
                   "identity": r["identity"], "is_control": spec.is_control, "gate_ok": r["gate"]["ok"],
                   "package": str(pkg)}
            _progress(ctx.progress, rec)
            results.append(rec)
            ctx.log(json.dumps({k: rec[k] for k in ("run_id", "cell", "disposition", "primary_failure_code", "gate_ok")}))
            if not spec.is_control and disp in ("SCORED", "UNSCORABLE"):
                ctx.prev_identity = r["identity"]          # decoy for the next run: the last pod that existed
            if not r["gate"]["ok"]:
                stopped = f"PK-INCOMPLETE on {r['run_id']}: {r['gate']['problems']}"
                break
            ctx.consecutive_oom = ctx.consecutive_oom + 1 if st.get("flags", {}).get("oom_event") else 0
            if ctx.consecutive_oom >= 2:
                stopped = "OOM in two consecutive runs"
                break
            if disp in ("PRE_ACTION_ABORT", "UNSCORABLE", "INVALID"):
                fails[spec.cell] = fails.get(spec.cell, 0) + 1
                if fails[spec.cell] > ctx.per_cell_block_ceiling:
                    stopped = f"ceiling: more than {ctx.per_cell_block_ceiling} aborted/unscorable attempts of {spec.cell} in {block}"
                    break
                queue.append((s, r["run_id"]))            # same cell, end of the same block
    finally:
        ctx.plat.delete_load()
    rep = replicate(packages, ctx.replica_runs)
    _dump(bdir / "replica_receipt.json", rep)
    if not rep["ok"] and stopped is None:
        stopped = f"block gate failed: {rep['problems']}"
    complete = stopped is None and not queue
    _progress(ctx.progress, {"event": "BLOCK_CLOSED", "block": block, "complete": complete, "stopped": stopped,
                             "replica_ok": rep["ok"], "opened_at": t0, "closed_at": _now_iso(),
                             "runs": [x["run_id"] for x in results]})
    ctx.runs += results
    return {"block": block, "complete": complete, "stopped": stopped, "replica_ok": rep["ok"], "runs": results,
            "packages": packages}


def blocks_from_schedule(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["block"], []).append(r)
    for b in out.values():
        b.sort(key=lambda r: int(r["slot"]))
    return out


def run_cohort(schedule_rows: list[dict], ctx: BlockContext, max_blocks: int | None = None) -> dict:
    """Run every block not yet opened, in schedule order, within this session. Stops on any stop rule."""
    prog = read_progress(ctx.progress)
    opened = {p["block"] for p in prog if p.get("event") == "BLOCK_OPEN"}
    closed = {p["block"] for p in prog if p.get("event") == "BLOCK_CLOSED"}
    for b in sorted(opened - closed):        # interrupted in an earlier session: never resumed
        _progress(ctx.progress, {"event": "BLOCK_INCOMPLETE", "block": b, "recorded_at": _now_iso(),
                                 "note": "interrupted; completed runs kept (Section 8.2, 10)"})
        closed.add(b)
    last_pos = [p for p in prog if p.get("event") == "RUN" and not p.get("is_control")
                and p["disposition"] in ("SCORED", "UNSCORABLE")]
    if last_pos and ctx.prev_identity is None:
        ctx.prev_identity = last_pos[-1]["identity"]
    done, out = 0, []
    for block, slots in blocks_from_schedule(schedule_rows).items():
        if block in opened or block in closed:
            continue
        if max_blocks is not None and done >= max_blocks:
            break
        res = run_block(block, slots, ctx)
        out.append({k: v for k, v in res.items() if k != "packages"})
        done += 1
        if res["stopped"]:
            return {"blocks": out, "stopped": res["stopped"]}
    remaining = [b for b in blocks_from_schedule(schedule_rows) if b not in opened and b not in closed
                 and b not in {o["block"] for o in out}]
    return {"blocks": out, "stopped": None, "remaining_blocks": remaining}
