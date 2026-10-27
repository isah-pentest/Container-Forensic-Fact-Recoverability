"""Protocol freeze (Blueprint v3.0, Sections 8.2, 16, 18 G5, 22.3).

FREEZE.json records the sha256 of every protocol file, the schedule, the master-seed record, the
power outputs and a digest of every tracked code, cluster and script file. The orchestrator
refuses to issue RUN- IDs unless FREEZE.json exists and the working tree still matches it.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .contract import freeze_readiness

REPO_ROOT = Path(__file__).resolve().parents[2]
FREEZE_PATH = REPO_ROOT / "protocol" / "FREEZE.json"
PROTOCOL_FILES = ("protocol/BLUEPRINT_v3.0.md", "protocol/BLUEPRINT_v3.0.docx", "protocol/contract.yaml",
                  "protocol/parameters.yaml", "protocol/required_files.yaml",
                  "protocol/schemas/run_status.schema.json", "protocol/qualification.yaml",
                  "protocol/randomization_schedule.csv", "protocol/master_seed.json",
                  "protocol/power/scenarios.json", "protocol/power/search.csv", "protocol/power/confirm.json",
                  "requirements.lock", "pyproject.toml")
CODE_DIRS = ("src", "cluster", "scripts", "tests", "reporting")


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _tracked(repo: Path, dirs) -> list[str]:
    out = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(repo), "ls-files", "--", *dirs],
                         capture_output=True, text=True, check=True).stdout.split()
    return sorted(out)


def code_digest(repo: Path = REPO_ROOT) -> dict:
    files = {f: _sha(repo / f) for f in _tracked(repo, CODE_DIRS)}
    joined = "".join(f"{h}  {f}\n" for f, h in files.items())
    return {"files": len(files), "sha256": hashlib.sha256(joined.encode()).hexdigest()}


def snapshot(repo: Path = REPO_ROOT) -> dict:
    missing = [f for f in PROTOCOL_FILES if not (repo / f).exists()]
    if missing:
        raise FileNotFoundError(f"missing for freeze: {missing}")
    return {"protocol_files": {f: _sha(repo / f) for f in PROTOCOL_FILES}, "code": code_digest(repo)}


def _fp(repo: Path) -> Path:
    return Path(repo) / "protocol" / "FREEZE.json"


def run_test_suite(repo: Path) -> dict:
    """Section 18 G5 'tests pass': the full suite, run by the freeze itself."""
    import sys
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=str(repo),
                       capture_output=True, text=True)
    lines = [l for l in r.stdout.strip().splitlines() if l.strip()]
    return {"returncode": r.returncode, "summary": lines[-1] if lines else "", "ok": r.returncode == 0}


def write_freeze(repo: Path = REPO_ROOT, check_readiness: bool = True, run_tests: bool = True) -> dict:
    if _fp(repo).exists():
        raise FileExistsError("FREEZE.json exists; a frozen protocol is never re-frozen (new version instead)")
    if check_readiness:
        fr = freeze_readiness()
        if fr["contract_problems"] or fr["parameter_problems"] or fr["unresolved"]:
            raise ValueError(f"not ready to freeze: {fr}")
    dirty = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(repo), "status", "--porcelain"],
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        raise RuntimeError(f"commit everything before freezing:\n{dirty}")
    tests = run_test_suite(repo) if run_tests else {"ok": True, "summary": "not run (unit test of freeze)"}
    if not tests["ok"]:
        raise RuntimeError(f"refusing to freeze: test suite failed ({tests['summary']})")
    head = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    rec = {"protocol_version": "3.0", "frozen_at": datetime.now(timezone.utc).isoformat(),
           "base_commit": head, "tests": tests["summary"], **snapshot(repo)}
    _fp(repo).write_text(json.dumps(rec, indent=2) + "\n")
    return rec


def check_freeze(repo: Path = REPO_ROOT) -> dict:
    """Problems between FREEZE.json and the working tree (empty list = matches)."""
    if not _fp(repo).exists():
        return {"ok": False, "problems": ["protocol/FREEZE.json does not exist"]}
    rec = json.loads(_fp(repo).read_text())
    now = snapshot(repo)
    problems = [f"{f} changed" for f, h in rec["protocol_files"].items() if now["protocol_files"].get(f) != h]
    if now["code"]["sha256"] != rec["code"]["sha256"]:
        problems.append("tracked code/cluster/scripts/tests/reporting changed since freeze")
    return {"ok": not problems, "problems": problems, "frozen_at": rec["frozen_at"]}


def seed_from_output_value(output_value: str) -> int:
    """Master-seed rule (protocol/gates/G5.md): first 64 bits of the beacon outputValue."""
    if len(output_value) < 16 or any(c not in "0123456789abcdefABCDEF" for c in output_value):
        raise ValueError("outputValue must be a hex string of at least 16 digits")
    return int(output_value[:16], 16)


def write_registration(repo: Path, venue: str, url: str, registered_at: str) -> dict:
    """Registration happens after the freeze (it uploads FREEZE.json), so it is a separate record."""
    path = Path(repo) / "protocol" / "registration.json"
    if path.exists():
        raise FileExistsError("registration.json exists")
    if not _fp(repo).exists():
        raise FileNotFoundError("freeze first: the registration cites FREEZE.json")
    rec = {"venue": venue, "url": url, "registered_at": registered_at,
           "freeze_json_sha256": _sha(_fp(repo)), "recorded_at": datetime.now(timezone.utc).isoformat()}
    path.write_text(json.dumps(rec, indent=2) + "\n")
    return rec


def check_ready_for_run_ids(repo: Path = REPO_ROOT) -> dict:
    r = check_freeze(repo)
    reg = Path(repo) / "protocol" / "registration.json"
    if not reg.exists():
        r["problems"].append("protocol/registration.json does not exist (register before the first RUN- ID)")
    elif json.loads(reg.read_text()).get("freeze_json_sha256") != (_sha(_fp(repo)) if _fp(repo).exists() else None):
        r["problems"].append("registration.json cites a different FREEZE.json")
    r["ok"] = not r["problems"]
    return r
