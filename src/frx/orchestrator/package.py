"""Write-once run package helpers (Blueprint v3.0, Sections 15.2-15.3)."""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path

from ..acquisition.store import jsonl_appender
from ..integrity import write_once


class Package:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=False)     # a run directory is never reused

    def path(self, rel: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def write_json(self, rel: str, obj) -> Path:
        p = self.path(rel)
        write_once(p, (json.dumps(obj, indent=2, default=str) + "\n").encode())
        return p

    def write_text(self, rel: str, text: str) -> Path:
        p = self.path(rel)
        write_once(p, text.encode())
        return p

    def jsonl(self, rel: str):
        return jsonl_appender(self.path(rel))

    def mirrored_jsonl(self, rel: str, mirror_rel: str):
        """Append to ground_truth/<x> and its sealed/truth copy at write time (Section 15.2)."""
        a, b = self.jsonl(rel), self.jsonl(mirror_rel)

        def append(obj):
            a(obj)
            b(obj)
        append.close = lambda: (a.close(), b.close())   # type: ignore[attr-defined]
        return append


class Console:
    """Full, unfiltered orchestrator console for one run (console/*.log), echoed to the terminal."""

    def __init__(self, pkg: Package):
        self._out = open(pkg.path("console/orchestrator_stdout.log"), "x")
        self._err = open(pkg.path("console/orchestrator_stderr.log"), "x")
        self._lock = threading.Lock()

    def _line(self, msg: str) -> str:
        return f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())}Z {msg}\n"

    def out(self, msg: str) -> None:
        line = self._line(msg)
        with self._lock:
            self._out.write(line); self._out.flush()
            sys.stdout.write(line); sys.stdout.flush()

    def err(self, msg: str) -> None:
        line = self._line(msg)
        with self._lock:
            self._err.write(line); self._err.flush()
            sys.stderr.write(line); sys.stderr.flush()

    def exception(self, where: str) -> None:
        self.err(f"{where}:\n{traceback.format_exc()}")

    def close(self) -> None:
        for fh in (self._out, self._err):
            fh.flush(); os.fsync(fh.fileno()); fh.close()


RUN_ID_RE = re.compile(r"^(QUAL|PILOT|RUN|CAL)-(\d{4,})$")


def next_run_id(prefix: str, ledger: Path, *run_roots: Path) -> str:
    """Next unused ID for a prefix: above every ID in the ledger and every existing directory."""
    used = 0
    if ledger.exists():
        for row in csv.DictReader(open(ledger)):
            m = RUN_ID_RE.match(row.get("run_id", ""))
            if m and m.group(1) == prefix:
                used = max(used, int(m.group(2)))
    for root in run_roots:
        if root.exists():
            for p in root.iterdir():
                m = RUN_ID_RE.match(p.name)
                if m and m.group(1) == prefix:
                    used = max(used, int(m.group(2)))
    return f"{prefix}-{used + 1:04d}"
