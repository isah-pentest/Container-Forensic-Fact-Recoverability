"""Read-only access to the locked evidence store, with re-verification before any analysis
(Section 15.3 item 5: hashes re-verified before analysis)."""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..contract import CONTRACT_PATH, PARAMETERS_PATH, load_yaml
from ..integrity import verify_hashes

CELLS = ("LC", "LD", "MC", "MD")


def sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in open(p)] if Path(p).exists() else []


def journal(pkg: Path) -> dict:
    """First record of every state in a run's timing journal."""
    out: dict = {}
    for e in read_jsonl(Path(pkg) / "timing" / "event_journal.jsonl"):
        out.setdefault(e["state"], e)
    return out


@dataclass
class Cohort:
    """One executed cohort: confirmatory (RUN-) or calibration (CAL-)."""
    name: str
    progress: Path
    scoring: Path
    runs: list = field(default_factory=list)          # RUN records from the progress log
    blocks_closed: list = field(default_factory=list)

    def load(self) -> "Cohort":
        prog = read_jsonl(self.progress)
        self.runs = [p for p in prog if p.get("event") == "RUN"]
        self.blocks_closed = [p for p in prog if p.get("event") == "BLOCK_CLOSED"]
        self.incomplete = [p["block"] for p in prog if p.get("event") == "BLOCK_INCOMPLETE"]
        return self

    def scored(self, positive_only: bool = True) -> list[dict]:
        return [r for r in self.runs if r["disposition"] == "SCORED" and not (positive_only and r["is_control"])]

    def scores(self, run_id: str) -> dict:
        return json.loads((self.scoring / run_id / "assessment" / "scores.json").read_text())

    def answers(self, run_id: str) -> dict:
        return json.loads((self.scoring / run_id / "assessment" / "fact_answers.json").read_text())


@dataclass
class Evidence:
    root: Path

    def __post_init__(self):
        self.root = Path(self.root).expanduser()
        self.lock_path = self.root / "scoring" / "scores_lock.json"
        self.scores_csv = self.root / "analysis_outputs" / "scores.csv"
        self.confirmatory = Cohort("confirmatory", self.root / "sessions" / "confirmatory_progress.jsonl",
                                   self.root / "scoring").load()
        self.calibration = Cohort("calibration", self.root / "calibration" / "calibration_progress.jsonl",
                                  self.root / "calibration" / "scoring").load()
        self.control_checks = read_jsonl(self.root / "sessions" / "control_checks.jsonl")
        self.params = load_yaml(PARAMETERS_PATH)
        self.contract = load_yaml(CONTRACT_PATH)

    # ------------------------------------------------------------------ integrity
    def verify(self) -> dict:
        """Score lock, analysis input, and every package and scoring tree against its manifest."""
        problems = []
        if not self.lock_path.exists():
            return {"ok": False, "problems": ["scores are not locked (scoring/scores_lock.json missing)"]}
        lock = json.loads(self.lock_path.read_text())
        for run_id, h in lock["scores_sha256"].items():
            p = self.root / "scoring" / run_id / "assessment" / "scores.json"
            if not p.exists() or sha(p) != h:
                problems.append(f"locked score changed or missing: {run_id}")
        if not self.scores_csv.exists() or sha(self.scores_csv) != lock["analysis_input_sha256"]:
            problems.append("analysis input scores.csv does not match the lock")
        n_pk = 0
        for coh in (self.confirmatory, self.calibration):
            for r in coh.runs:
                n_pk += 1
                for bad in verify_hashes(Path(r["package"])):
                    problems.append(f"{r['run_id']} package: {bad}")
                sd = coh.scoring / r["run_id"]
                if sd.exists():
                    for bad in verify_hashes(sd):
                        problems.append(f"{r['run_id']} scoring: {bad}")
        return {"ok": not problems, "problems": problems, "packages_checked": n_pk,
                "locked_scores": len(lock["scores_sha256"]), "lock_written": lock["locked_at"],
                "analysis_input_sha256": lock["analysis_input_sha256"]}

    def analysis_rows(self) -> list[dict]:
        """The locked analysis input (one row per scored positive confirmatory run)."""
        with open(self.scores_csv) as fh:
            rows = list(csv.DictReader(fh))
        for r in rows:
            r["Y"] = int(r["Y"])
        return rows

    def store_capacity(self) -> dict:
        st = self.params["stores"]
        mib = 1024 * 1024
        return {"audit": {"M": st["audit"]["max_size_mb"] * mib, "k": st["audit"]["max_backups"]},
                "tetragon": {"M": st["tetragon"]["max_size_mb"] * mib, "k": st["tetragon"]["max_backups"]}}
