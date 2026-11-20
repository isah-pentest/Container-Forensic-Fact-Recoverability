"""Freeze of series F (the v3.0.1 and series-R freeze and registration records are not touched).

protocol/flooding/FREEZE_F.json records the SHA-256 of every series-F protocol file and the digest of all
tracked code at the freeze commit. Series F is not registered on OSF (author decision, 20 November
2026); the public record is the commit that adds FREEZE_F.json, tagged series-F-frozen and pushed to
the public repository before the first counted run. Runs are refused unless FREEZE_F.json exists, the
protocol files still match and the tracked code (src, cluster, scripts, tests, reporting) is unchanged.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..freeze import REPO_ROOT, code_digest

DIR = Path("protocol") / "flooding"
FILES = ("AMENDMENT_F.md", "plan.json", "schedule.csv")
FREEZE = "FREEZE_F.json"
TAG = "series-F-frozen"


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _git(repo: Path, *a: str) -> str:
    return subprocess.run(["git", "-c", "safe.directory=*", "-C", str(repo), *a], capture_output=True, text=True,
                          check=True).stdout.strip()


def write_freeze(repo: Path = REPO_ROOT) -> dict:
    repo = Path(repo)
    d = repo / DIR
    if (d / FREEZE).exists():
        raise FileExistsError(f"{DIR / FREEZE} exists (series F is frozen once)")
    missing = [f for f in FILES if not (d / f).exists()]
    if missing:
        raise FileNotFoundError(f"missing for freeze: {missing}")
    if _git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("commit everything first: the freeze records the committed code")
    rec = {"series": "F", "frozen_at": datetime.now(timezone.utc).isoformat(), "base_commit": _git(repo, "rev-parse", "HEAD"),
           "protocol_files": {str(DIR / f): _sha(d / f) for f in FILES}, "code": code_digest(repo),
           "public_record": f"commit adding this file, tagged {TAG} and pushed before the first counted run",
           "supplements": "protocol v3.0.1 and series R: unchanged"}
    (d / FREEZE).write_text(json.dumps(rec, indent=2) + "\n")
    return {**rec, "freeze_sha256": _sha(d / FREEZE)}


def check(repo: Path = REPO_ROOT) -> dict:
    """Ready to issue series-F IDs? (frozen, unchanged protocol files and tracked code, tag present)."""
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
        problems.append("tracked code/cluster/scripts/tests/reporting changed since the series-F freeze")
    try:
        tagged = _git(repo, "rev-list", "-n", "1", TAG)
        if _git(repo, "log", "-n", "1", "--format=%H", "--", str(DIR / FREEZE)) != tagged:
            problems.append(f"tag {TAG} does not point at the commit that added {DIR / FREEZE}")
    except subprocess.CalledProcessError:
        problems.append(f"tag {TAG} does not exist")
    return {"ok": not problems, "problems": problems, "frozen_at": rec["frozen_at"],
            "freeze_sha256": _sha(d / FREEZE)}
