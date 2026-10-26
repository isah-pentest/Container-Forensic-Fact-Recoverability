"""Hashing, manifests, write-once locking, completeness gates and the ledger (Section 15)."""
from __future__ import annotations

import csv
import fnmatch
import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import yaml

MANIFEST = "manifests/SHA256SUMS"
SIZES = "manifests/sizes.csv"
GATE_RESULT = "manifests/required_files_check.json"
_MANIFEST_FILES = {MANIFEST, SIZES, GATE_RESULT}


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file():
            yield p.relative_to(root).as_posix()


def write_once(path: Path, data: bytes) -> None:
    """Exclusive-create write; raises FileExistsError if the file already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_manifest(pkg: Path) -> str:
    """Write SHA256SUMS and sizes.csv over every file (except manifest files). Returns package hash."""
    rows = []
    for rel in _iter_files(pkg):
        if rel in _MANIFEST_FILES:
            continue
        p = pkg / rel
        rows.append((rel, p.stat().st_size, sha256_file(p)))
    sums = "".join(f"{h}  {rel}\n" for rel, _, h in rows).encode()
    write_once(pkg / MANIFEST, sums)
    lines = ["relative_path,bytes,sha256"] + [f"{rel},{n},{h}" for rel, n, h in rows]
    write_once(pkg / SIZES, ("\n".join(lines) + "\n").encode())
    return hashlib.sha256(sums).hexdigest()


def package_hash(pkg: Path) -> str:
    return sha256_file(pkg / MANIFEST)


def lock_readonly(pkg: Path) -> None:
    """Remove write permission from every file and directory (immutability flags are host-specific)."""
    for p in sorted(pkg.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        mode = p.stat().st_mode
        p.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    m = pkg.stat().st_mode
    pkg.chmod(m & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def load_required(spec_path: Path) -> dict:
    with open(spec_path) as fh:
        return yaml.safe_load(fh)


def _matches(pattern: str, files: list[str]) -> list[str]:
    return [f for f in files if fnmatch.fnmatchcase(f, pattern)]


def verify_hashes(pkg: Path) -> list[str]:
    """Return a list of problems (empty if every listed hash matches and nothing is unlisted)."""
    problems = []
    man = pkg / MANIFEST
    if not man.exists():
        return [f"missing {MANIFEST}"]
    listed = {}
    for line in man.read_text().splitlines():
        if not line.strip():
            continue
        h, rel = line.split("  ", 1)
        listed[rel] = h
    for rel, h in listed.items():
        p = pkg / rel
        if not p.exists():
            problems.append(f"listed file missing: {rel}")
        elif sha256_file(p) != h:
            problems.append(f"hash mismatch: {rel}")
    for rel in _iter_files(pkg):
        if rel not in listed and rel not in _MANIFEST_FILES:
            problems.append(f"unlisted file: {rel}")
    return problems


def verify_package(pkg: Path, spec: dict, status_validator=None) -> dict:
    """Completeness gate for one run package (Section 15.4). Does not write anything."""
    files = [f for f in _iter_files(pkg)]
    problems: list[str] = []
    status_path = pkg / "run_status.json"
    status = None
    if not status_path.exists():
        problems.append("missing run_status.json")
    else:
        try:
            status = json.loads(status_path.read_text())
        except json.JSONDecodeError as e:
            problems.append(f"run_status.json not valid JSON: {e}")
    if status is not None and status_validator is not None:
        problems.extend(status_validator(status))
    disposition = (status or {}).get("disposition")
    arm = (status or {}).get("arm")
    is_control = (status or {}).get("cell") == "NC"

    required = list(spec.get("always", []))
    if disposition in ("SCORED",) and not is_control:
        required += spec.get("scored_positive", [])
    if disposition == "SCORED" and is_control:
        required += spec.get("scored_control", [])
    if arm == "C" and disposition == "SCORED":
        required += spec.get("arm_C", [])
    if arm == "D" and disposition == "SCORED":
        required += spec.get("arm_D", [])
    may_be_empty = set(spec.get("may_be_empty", {}).get(disposition or "", []))
    pfc = (status or {}).get("primary_failure_code") or ""
    may_be_absent = set(spec.get("may_be_empty_by_code", {}).get(pfc, []))
    may_be_empty |= may_be_absent
    if may_be_absent:
        receipt = pkg / "acquisition" / "read_receipts.jsonl"
        if not receipt.exists() or receipt.stat().st_size == 0:
            problems.append(f"{pfc}: explanatory receipt acquisition/read_receipts.jsonl "
                            "missing or empty")

    for pattern in required:
        hits = _matches(pattern, files)
        if not hits:
            if pattern in may_be_absent:
                continue
            problems.append(f"required file missing: {pattern}")
            continue
        if pattern in may_be_empty:
            continue
        for f in hits:
            if (pkg / f).stat().st_size == 0:
                problems.append(f"required file empty: {f}")

    problems.extend(verify_hashes(pkg))
    return {"package": str(pkg), "ok": not problems, "problems": problems,
            "checked_at": datetime.now(timezone.utc).isoformat()}


def append_ledger(ledger: Path, row: dict) -> None:
    fields = ["run_id", "block", "cell", "started_at", "disposition", "package_hash"]
    new = not ledger.exists()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    if not new:
        seen = {r["run_id"] for r in csv.DictReader(open(ledger))}
        if row["run_id"] in seen:
            raise ValueError(f"run_id {row['run_id']} already in ledger (IDs are never reused)")
    with open(ledger, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})
        fh.flush()
        os.fsync(fh.fileno())


def verify_block(packages: list[Path], replica_root: Path) -> dict:
    """Block gate: every package's manifest exists in the replica with identical file hashes."""
    problems = []
    for pkg in packages:
        rep = replica_root / pkg.name
        if not rep.exists():
            problems.append(f"replica missing: {pkg.name}")
            continue
        if (pkg / MANIFEST).read_bytes() != (rep / MANIFEST).read_bytes():
            problems.append(f"manifest differs in replica: {pkg.name}")
            continue
        for p in verify_hashes(rep):
            problems.append(f"replica {pkg.name}: {p}")
    return {"ok": not problems, "problems": problems,
            "checked_at": datetime.now(timezone.utc).isoformat()}
