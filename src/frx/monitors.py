"""1 Hz monitors (Blueprint v3.0, Sections 3.4, 11 M19/M24, 14.3).

GrowthMonitor: name, inode, size and mtime of every file in each source-side store.
                Metadata only - it never opens a file, so it cannot leak content.
ResourceMonitor: host memory, load, CPU busy %, OOM-kill counter, and RSS of key processes.
Both sample on a monotonic schedule and write CSV rows as they go.
"""
from __future__ import annotations

import csv
import os
import threading
import time
from pathlib import Path

KEY_PROCESSES = ("kube-apiserver", "etcd", "tetragon", "kubelet", "containerd", "dockerd")


class _Sampler(threading.Thread):
    def __init__(self, out_csv: Path, period_s: float = 1.0, name: str = "sampler"):
        super().__init__(daemon=True, name=name)
        self.out_csv = Path(out_csv)
        self.period_s = period_s
        self._stop_evt = threading.Event()   # not _stop: threading.Thread internal
        self.samples = 0
        self.errors: list[str] = []

    def header(self) -> list[str]:
        raise NotImplementedError

    def sample(self) -> list[list]:
        raise NotImplementedError

    def run(self) -> None:
        self.out_csv.parent.mkdir(parents=True, exist_ok=True)
        with open(self.out_csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(self.header())
            nxt = time.monotonic()
            while not self._stop_evt.is_set():
                try:
                    for row in self.sample():
                        w.writerow(row)
                    fh.flush()
                    self.samples += 1
                except Exception as e:  # keep sampling; record the problem
                    self.errors.append(repr(e))
                nxt += self.period_s
                self._stop_evt.wait(max(0.0, nxt - time.monotonic()))

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_evt.set()
        self.join(timeout)


class GrowthMonitor(_Sampler):
    def __init__(self, store_dirs: dict[str, Path], out_csv: Path, period_s: float = 1.0):
        super().__init__(out_csv, period_s, "growth-monitor")
        self.store_dirs = {k: Path(v) for k, v in store_dirs.items()}

    def header(self):
        return ["mono_ns", "wall_ns", "store", "name", "inode", "size", "mtime_ns"]

    def sample(self):
        t_m, t_w = time.monotonic_ns(), time.time_ns()
        rows = []
        for store, d in self.store_dirs.items():
            try:
                entries = list(os.scandir(d))
            except FileNotFoundError:
                continue
            for e in entries:
                try:
                    st = e.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue          # deleted between listing and stat (rotation)
                if e.is_file(follow_symlinks=False):
                    rows.append([t_m, t_w, store, e.name, st.st_ino, st.st_size, st.st_mtime_ns])
        return rows


def _meminfo() -> dict[str, int]:
    out = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            k, v = line.split(":", 1)
            out[k] = int(v.split()[0])
    return out


def _cpu_times() -> tuple[int, int]:
    with open("/proc/stat") as fh:
        parts = fh.readline().split()[1:]
    vals = [int(x) for x in parts]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def _oom_kills() -> int:
    with open("/proc/vmstat") as fh:
        for line in fh:
            if line.startswith("oom_kill "):
                return int(line.split()[1])
    return -1


def _rss_by_name(names) -> dict[str, int]:
    tot = {n: 0 for n in names}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as fh:
                comm = fh.read().strip()
            if comm not in tot:
                continue
            with open(f"/proc/{pid}/status") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        tot[comm] += int(line.split()[1])
                        break
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return tot


class ResourceMonitor(_Sampler):
    def __init__(self, out_csv: Path, period_s: float = 1.0, names=KEY_PROCESSES):
        super().__init__(out_csv, period_s, "resource-monitor")
        self.names = tuple(names)
        self._last_cpu = _cpu_times()

    def header(self):
        return (["mono_ns", "wall_ns", "mem_total_kb", "mem_available_kb", "load1", "cpu_busy_pct",
                 "oom_kill_total", "self_rss_kb"] + [f"rss_kb_{n}" for n in self.names])

    def sample(self):
        t_m, t_w = time.monotonic_ns(), time.time_ns()
        mi = _meminfo()
        load1 = float(open("/proc/loadavg").read().split()[0])
        tot, idle = _cpu_times()
        dt, di = tot - self._last_cpu[0], idle - self._last_cpu[1]
        self._last_cpu = (tot, idle)
        busy = 100.0 * (1 - di / dt) if dt > 0 else 0.0
        self_rss = 0
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    self_rss = int(line.split()[1])
        rss = _rss_by_name(self.names)
        return [[t_m, t_w, mi.get("MemTotal"), mi.get("MemAvailable"), load1, round(busy, 2),
                 _oom_kills(), self_rss] + [rss[n] for n in self.names]]


def bytes_written(growth_csv: Path) -> dict[str, dict]:
    """Bytes written per store during the monitored window, from inode size histories.

    For an inode present at the first sample, growth = last size - first size; for an inode
    that appears later, growth = last size seen (it started empty). Evicted files count up to
    their last observed size. Also returns rotation count (new inodes) and duration.
    """
    first_t = None
    hist: dict[tuple[str, int], list[int]] = {}
    present_at_start: set[tuple[str, int]] = set()
    last_t = None
    with open(growth_csv) as fh:
        for row in csv.DictReader(fh):
            t = int(row["mono_ns"])
            if first_t is None:
                first_t = t
            key = (row["store"], int(row["inode"]))
            size = int(row["size"])
            if t == first_t:
                present_at_start.add(key)
            hist.setdefault(key, [size, size])
            hist[key][1] = size
            last_t = t
    out: dict[str, dict] = {}
    for (store, ino), (first, last) in hist.items():
        d = out.setdefault(store, {"bytes": 0, "new_inodes": 0})
        if (store, ino) in present_at_start:
            d["bytes"] += max(0, last - first)
        else:
            d["bytes"] += last
            d["new_inodes"] += 1
    dur = (last_t - first_t) / 1e9 if first_t is not None and last_t is not None else 0.0
    for d in out.values():
        d["duration_s"] = dur
        d["bytes_per_s"] = d["bytes"] / dur if dur > 0 else 0.0
    return out
