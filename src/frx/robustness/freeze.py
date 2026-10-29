"""Freeze and registration of series R (the v3.0.1 FREEZE.json and registration.json are not touched).

protocol/robustness/FREEZE_R.json records the SHA-256 of every series-R protocol file and the
digest of all tracked code at the freeze commit; protocol/robustness/registration_R.json records
where the SHA-256 of FREEZE_R.json was registered. Runs are refused unless both exist, the protocol
files still match and the tracked code (src, cluster, scripts, tests, reporting) is unchanged.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..freeze import REPO_ROOT, code_digest

DIR = Path("protocol") / "robustness"
FILES = ("AMENDMENT_R.md", "plan.json", "schedule.csv")
FREEZE = "FREEZE_R.json"
REGISTRATION = "registration_R.json"


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", "-c", "safe.directory=*", "-C", str(repo), *a], capture_output=True, text=True,
                          check=True).stdout.strip()


def write_freeze(repo: Path = REPO_ROOT) -> dict:
    repo = Path(repo)
    d = repo / DIR
    if (d / FREEZE).exists():
        raise FileExistsError(f"{DIR / FREEZE} exists (series R is frozen once)")
    missing = [f for f in FILES if not (d / f).exists()]
    if missing:
        raise FileNotFoundError(f"missing for freeze: {missing}")
    if _git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("commit everything first: the freeze records the committed code")
    rec = {"series": "R", "frozen_at": datetime.now(timezone.utc).isoformat(), "base_commit": _git(repo, "rev-parse", "HEAD"),
           "protocol_files": {str(DIR / f): _sha(d / f) for f in FILES}, "code": code_digest(repo),
           "supplements": "protocol v3.0.1 (protocol/FREEZE.json, protocol/registration.json): unchanged"}
    (d / FREEZE).write_text(json.dumps(rec, indent=2) + "\n")
    return {**rec, "freeze_sha256": _sha(d / FREEZE)}


def write_registration(url: str, registered_at: str, repo: Path = REPO_ROOT) -> dict:
    d = Path(repo) / DIR
    if (d / REGISTRATION).exists():
        raise FileExistsError(f"{DIR / REGISTRATION} exists")
    if not (d / FREEZE).exists():
        raise FileNotFoundError("freeze first: the registration cites FREEZE_R.json")
    rec = {"venue": "OSF Registries (hash commitment, series R)", "url": url, "registered_at": registered_at,
           "freeze_r_sha256": _sha(d / FREEZE), "recorded_at": datetime.now(timezone.utc).isoformat()}
    (d / REGISTRATION).write_text(json.dumps(rec, indent=2) + "\n")
    return rec


def check(repo: Path = REPO_ROOT) -> dict:
    """Ready to issue ROB- IDs? (frozen, registered, unchanged protocol files and tracked code)."""
    repo = Path(repo)
    d = repo / DIR
    problems = []
    if not (d / FREEZE).exists():
        return {"ok": False, "problems": [f"{DIR / FREEZE} does not exist"]}
    rec = json.loads((d / FREEZE).read_text())
    for f, h in rec["protocol_files"].items():
        p = repo / f
        if not p.exists() or _sha(p) != h:
            problems.append(f"{f} changed or missing")
    if code_digest(repo) != rec["code"]:
        problems.append("tracked code/cluster/scripts/tests/reporting changed since the series-R freeze")
    if not (d / REGISTRATION).exists():
        problems.append(f"{DIR / REGISTRATION} does not exist (register FREEZE_R.json first)")
    else:
        reg = json.loads((d / REGISTRATION).read_text())
        if reg.get("freeze_r_sha256") != _sha(d / FREEZE):
            problems.append("registration_R.json does not cite the current FREEZE_R.json")
    return {"ok": not problems, "problems": problems, "frozen_at": rec["frozen_at"],
            "freeze_sha256": _sha(d / FREEZE)}
