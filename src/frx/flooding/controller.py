"""Flood controller for series F: a thread beside the frozen block runner; the run code is not changed.

The frozen run procedure (frx.orchestrator.run.execute_run) sets the load rate once, before the action,
and has no step after TERMINATION_CONFIRMED. The controller therefore watches the run's own event
journal (timing/event_journal.jsonl, flushed per record) and changes the rate through the same
function the runner uses (frx.load.set_rate, a merge patch of the frx-load-rate ConfigMap):

  TERMINATION_CONFIRMED seen   -> flood setting of the run's level (F1-F3); nothing for F0
  t_term + window_s            -> the run's base (L) setting restored
  earlier end of the run        -> restored at once: ACQ_END_C (continuous arm), ACQ_START_D, ERROR,
                                   PRE_ACTION_ABORT, SNAPSHOT_SEALED, CLEANUP or PACKAGE_CLOSED

The controller never reads ground truth, the acquired streams or the stores, and never writes inside a
run package (packages are write-once and hashed). Its records go to <controller_dir>/<run_id>.jsonl.
The analysis uses the bytes measured by the frozen 1 Hz growth monitor, not the controller's targets.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable

RUN_DIR_RE = re.compile(r"^CAL-(\d{4,})$")
END_STATES = ("ACQ_END_C", "ACQ_START_D", "ERROR", "PRE_ACTION_ABORT", "SNAPSHOT_SEALED", "CLEANUP",
              "PACKAGE_CLOSED")


def _ns() -> dict:
    return {"mono_ns": time.monotonic_ns(), "wall_ns": time.time_ns()}


class FloodController(threading.Thread):
    """levels: {level: {"knob": {...}} or None}; base: {"knob": {...}} (applied on restore)."""

    def __init__(self, runs_dir: Path, controller_dir: Path, levels: dict, base: dict, window_s: float,
                 set_rate: Callable[[float, float, str], dict] | None = None, poll_s: float = 0.05,
                 log: Callable[[str], None] = print):
        super().__init__(name="frx-flood-controller", daemon=True)
        if set_rate is None:
            from ..load import set_rate as _sr
            set_rate = _sr
        self.runs_dir, self.cdir = Path(runs_dir), Path(controller_dir)
        self.levels, self.base, self.window_s = levels, base, float(window_s)
        self._set_rate, self.poll_s, self.log = set_rate, poll_s, log
        self._halt = threading.Event()
        self.handled: dict[str, dict] = {}
        self.errors: list[str] = []
        self._existing = self._run_dirs()          # packages present before the session are never handled

    # ------------------------------------------------------------------ helpers
    def _run_dirs(self) -> set[str]:
        if not self.runs_dir.exists():
            return set()
        return {p.name for p in self.runs_dir.iterdir() if p.is_dir() and RUN_DIR_RE.match(p.name)}

    def _current(self) -> str | None:
        new = [n for n in self._run_dirs() - self._existing if n not in self.handled]
        return max(new, key=lambda n: int(RUN_DIR_RE.match(n).group(1))) if new else None

    @staticmethod
    def _journal(pkg: Path) -> list[dict]:
        p = pkg / "timing" / "event_journal.jsonl"
        if not p.exists():
            return []
        out = []
        for line in p.read_text().splitlines():
            if line.endswith("}"):
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out

    def _patch(self, knob: dict, label: str) -> dict:
        t0 = _ns()
        try:
            ack = self._set_rate(knob["exec_per_s"], knob["api_per_s"], label)
            ok, err = True, None
        except Exception as e:                       # recorded; the next run's own RATE_SET resets the rate
            ack, ok, err = None, False, repr(e)
            self.errors.append(f"{label}: {err}")
        return {"knob": knob, "label": label, "ok": ok, "error": err, "ack": ack, "sent": t0, "acked": _ns()}

    def stop(self) -> None:
        self._halt.set()

    # ------------------------------------------------------------------ one run
    def _watch_run(self, run_id: str) -> None:
        pkg = self.runs_dir / run_id
        self.cdir.mkdir(parents=True, exist_ok=True)
        fh = open(self.cdir / f"{run_id}.jsonl", "x")

        def rec(event: str, **kw) -> None:
            fh.write(json.dumps({"event": event, "run_id": run_id, **_ns(), **kw}) + "\n")
            fh.flush()

        ident = None
        while ident is None and not self._halt.is_set():
            try:
                ident = json.loads((pkg / "identity.json").read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(self.poll_s)
        if ident is None:
            fh.close()
            return
        level, arm = ident.get("load_level"), ident.get("arm")
        flood = self.levels.get(level)
        rec("WATCH", level=level, arm=arm, is_control=ident.get("is_control"), flood=flood,
            window_s=self.window_s)
        state = {"level": level, "arm": arm, "flooded": False, "restored": None, "t_term": None}
        deadline = None
        while not self._halt.is_set():
            j = self._journal(pkg)
            states = {e["state"]: e for e in j if "state" in e}
            if state["t_term"] is None and "TERMINATION_CONFIRMED" in states:
                t = states["TERMINATION_CONFIRMED"]
                seen = _ns()
                state["t_term"] = {"mono_ns": t["mono_ns"], "wall_ns": t["wall_ns"]}
                rec("TERMINATION_SEEN", t_term=state["t_term"], detect_latency_s=(seen["mono_ns"] - t["mono_ns"]) / 1e9)
                ended = [s for s in END_STATES if s in states]
                if flood is not None and not ended:
                    p = self._patch(flood["knob"], f"{level}-FLOOD")
                    rec("FLOOD_SET", **p, onset_latency_s=(p["acked"]["mono_ns"] - t["mono_ns"]) / 1e9)
                    state["flooded"] = p["ok"]
                    deadline = t["mono_ns"] + int(self.window_s * 1e9)
                elif flood is None:
                    rec("NO_FLOOD", reason="level without flood setting")
            if state["flooded"] and state["restored"] is None:
                ended = [s for s in END_STATES if s in states]
                reason = "window elapsed" if time.monotonic_ns() >= deadline else (f"run state {ended[0]}" if ended else None)
                if reason:
                    p = self._patch(self.base["knob"], str(level))
                    state["restored"] = reason
                    rec("RESTORED", reason=reason, **p,
                        flood_s_controller=(p["acked"]["mono_ns"] - state["t_term"]["mono_ns"]) / 1e9)
            if "PACKAGE_CLOSED" in states:             # restored above in the same pass if it was flooding
                rec("DONE", flooded=state["flooded"], restored=state["restored"])
                break
            if self._current() not in (None, run_id):   # a later run started: never flood across runs
                if state["flooded"] and state["restored"] is None:
                    p = self._patch(self.base["knob"], str(level))
                    state["restored"] = "later run started"
                    rec("RESTORED", reason=state["restored"], **p)
                rec("DONE", flooded=state["flooded"], restored=state["restored"], note="later run started")
                break
            time.sleep(self.poll_s)
        else:
            if state["flooded"] and state["restored"] is None:
                p = self._patch(self.base["knob"], str(level))
                state["restored"] = "controller stopped"
                rec("RESTORED", reason=state["restored"], **p)
        fh.close()
        self.handled[run_id] = state
        self.log(json.dumps({"flood_controller": run_id, **{k: state[k] for k in ("level", "arm", "flooded", "restored")}}))

    def run(self) -> None:
        while not self._halt.is_set():
            cur = self._current()
            if cur is None:
                time.sleep(self.poll_s)
                continue
            try:
                self._watch_run(cur)
            except Exception as e:                   # never crash the session; recorded
                self.errors.append(f"{cur}: {e!r}")
                self.handled.setdefault(cur, {"error": repr(e)})
