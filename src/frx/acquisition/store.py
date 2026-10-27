"""Source-side store helpers (Blueprint v3.0, Sections 3.2-3.4, 8.4, 9, 15.2-15.3).

- start_state():     inode, size and backup list of each store at SOURCE_READY (sets o in Section 4).
- one_shot_copy():   the delayed arm's reader. Copies every file present at TERMINATION + delta
                     into ONE byte stream per store (backups oldest first, active file last), so
                     both arms deliver exactly one raw file per store and the file count cannot
                     reveal the arm. Byte ranges of every source file are kept in the receipts.
- snapshot_store():  sealed post-run copy of every file under its own name.
- LagSampler:        1 Hz follower lag (bytes behind end of the active file) for arm C.
- finalize_raw():    fsync, hash and rename an acquired stream to <sha256>.raw (FLUSH_ACK).
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path

ACTIVE_NAMES = {"audit": "audit.json", "tetragon": "tetragon.log"}


def _ns() -> dict:
    return {"mono_ns": time.monotonic_ns(), "wall_ns": time.time_ns()}


def backup_names(store_dir: Path, active_name: str) -> list[str]:
    """Rotated backups (<stem>-<timestamp><ext>), oldest first (lumberjack names sort by time)."""
    stem, ext = os.path.splitext(active_name)
    out = []
    for e in os.scandir(store_dir):
        if e.is_file(follow_symlinks=False) and e.name.startswith(stem + "-") and e.name.endswith(ext):
            out.append(e.name)
    return sorted(out)


def start_state(stores: dict[str, Path], active_names: dict[str, str] = ACTIVE_NAMES) -> dict:
    """Section 8.4 'store start state': every file's inode and size, and the active offset."""
    out = {"recorded": _ns(), "stores": {}}
    for src, d in stores.items():
        files = []
        for e in sorted(os.scandir(d), key=lambda x: x.name):
            try:
                st = e.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if e.is_file(follow_symlinks=False):
                files.append({"name": e.name, "inode": st.st_ino, "size": st.st_size,
                              "mtime_ns": st.st_mtime_ns})
        active = next((f for f in files if f["name"] == active_names[src]), None)
        out["stores"][src] = {
            "dir": str(d), "active_name": active_names[src], "files": files,
            "active_inode": active["inode"] if active else None,
            "active_offset": active["size"] if active else None,
            "backups": backup_names(d, active_names[src]),
        }
    return out


def one_shot_copy(store_dir: Path, active_name: str, out_raw: Path, source: str,
                  receipt, chunk: int = 1 << 20) -> dict:
    """Delayed-arm reader. Opens every present file at once, then copies them in order.

    receipt(dict) is called for every file. Returns a summary. A file deleted between listing
    and opening is recorded as 'vanished' (that is producer loss, not a reader failure);
    any other OSError is a reader failure (CF-READ).
    """
    t0 = _ns()
    opened: list[tuple[str, object, os.stat_result]] = []   # (name, fh, fstat)
    seen_inodes: set[int] = set()
    vanished: list[str] = []
    errors: list[str] = []

    def _open(name: str):
        try:
            fh = open(store_dir / name, "rb")
        except FileNotFoundError:
            vanished.append(name)
            receipt({"event": "vanished", "source": source, "name": name, **_ns()})
            return
        except OSError as e:
            errors.append(f"{name}: {e!r}")
            receipt({"event": "error", "source": source, "name": name, "error": repr(e), **_ns()})
            return
        st = os.fstat(fh.fileno())
        if st.st_ino in seen_inodes:
            fh.close()
            return
        seen_inodes.add(st.st_ino)
        opened.append((name, fh, st))

    names = backup_names(store_dir, active_name) + [active_name]
    for n in names:
        _open(n)
    # A rotation between listing and opening renames the old active file to a new backup name.
    # Re-list once and open any backup whose inode was not opened (it holds the newest old bytes).
    for n in backup_names(store_dir, active_name):
        if n not in names:
            _open(n)
    backups = sorted([o for o in opened if o[0] != active_name], key=lambda o: o[0])
    active = [o for o in opened if o[0] == active_name]
    order = backups + active

    out_raw.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    files = []
    with open(out_raw, "xb") as out:
        for name, fh, st in order:
            start = total
            t_open = _ns()
            try:
                while True:
                    data = fh.read(chunk)
                    if not data:
                        break
                    out.write(data)
                    total += len(data)
            except OSError as e:
                errors.append(f"{name}: {e!r}")
            finally:
                fh.close()
            rec = {"event": "copied", "source": source, "name": name, "inode": st.st_ino,
                   "size_at_open": st.st_size, "stream_start": start, "stream_end": total,
                   "bytes": total - start, "opened": t_open, **_ns()}
            receipt(rec)
            files.append(rec)
        out.flush()
        os.fsync(out.fileno())
    return {"source": source, "started": t0, "finished": _ns(), "bytes": total, "files": len(files),
            "vanished": vanished, "errors": errors, "ok": not errors}


def snapshot_store(store_dir: Path, out_dir: Path, source: str, receipt=None) -> dict:
    """Sealed post-run snapshot: byte copy of every file in the store under its own name."""
    out_dir.mkdir(parents=True, exist_ok=True)
    copied, vanished, errors = [], [], []
    for e in sorted(os.scandir(store_dir), key=lambda x: x.name):
        if not e.is_file(follow_symlinks=False):
            continue
        try:
            with open(e.path, "rb") as src, open(out_dir / e.name, "xb") as dst:
                ino = os.fstat(src.fileno()).st_ino
                shutil.copyfileobj(src, dst, 1 << 20)
                dst.flush()
                os.fsync(dst.fileno())
                n = dst.tell()
            copied.append({"name": e.name, "inode": ino, "bytes": n})
        except FileNotFoundError:
            vanished.append(e.name)
        except OSError as ex:
            errors.append(f"{e.name}: {ex!r}")
    res = {"source": source, "files": copied, "vanished": vanished, "errors": errors, **_ns()}
    if receipt:
        receipt({"event": "snapshot", **res})
    return res


def sha256_path(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def finalize_raw(tmp_path: Path) -> dict:
    """FLUSH_ACK for one acquired stream: fsync, hash, rename to <sha256>.raw (neutral name)."""
    with open(tmp_path, "rb+") as fh:
        fh.flush()
        os.fsync(fh.fileno())
    digest = sha256_path(tmp_path)
    final = tmp_path.parent / f"{digest}.raw"
    if final.exists():
        raise FileExistsError(final)
    os.rename(tmp_path, final)
    dfd = os.open(tmp_path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return {"path": str(final), "sha256": digest, "bytes": final.stat().st_size, **_ns()}


class LagSampler(threading.Thread):
    """1 Hz follower lag for arm C (M18): bytes between follower position and end of active file."""

    HEADER = ["mono_ns", "wall_ns", "source", "active_inode", "active_size", "follower_inode",
              "follower_pos", "lag_bytes", "bytes_written"]

    def __init__(self, followers: dict, out_csv: Path, period_s: float = 1.0):
        super().__init__(daemon=True, name="lag-sampler")
        self.followers, self.out_csv, self.period_s = followers, Path(out_csv), period_s
        self._stop_evt = threading.Event()
        self.errors: list[str] = []
        self.max_lag = {k: 0 for k in followers}

    def run(self) -> None:
        self.out_csv.parent.mkdir(parents=True, exist_ok=True)
        with open(self.out_csv, "x", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(self.HEADER)
            nxt = time.monotonic()
            while not self._stop_evt.is_set():
                t_m, t_w = time.monotonic_ns(), time.time_ns()
                for src, f in self.followers.items():
                    try:
                        st = f.active_path.stat()
                        ino, size = st.st_ino, st.st_size
                    except FileNotFoundError:
                        ino, size = "", ""
                    pos, fino = f.position, f.current_inode
                    lag = (size - pos) if (ino != "" and ino == fino) else ""
                    if lag != "":
                        self.max_lag[src] = max(self.max_lag[src], lag)
                    w.writerow([t_m, t_w, src, ino, size, fino, pos, lag, f.bytes_written])
                fh.flush()
                nxt += self.period_s
                self._stop_evt.wait(max(0.0, nxt - time.monotonic()))

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_evt.set()
        self.join(timeout)


def rotation_events(growth_rows, active_names: dict[str, str] = ACTIVE_NAMES) -> list[dict]:
    """Derive rotation and deletion events from 1 Hz growth samples (sealed, metadata only).

    rotation: the active name points to a new inode; deleted: an inode present in one sample is
    absent from the next two samples (two, to ignore a one-sample rename race).
    """
    by_t: dict[int, list] = {}
    for r in growth_rows:
        by_t.setdefault(int(r["mono_ns"]), []).append(r)
    times = sorted(by_t)
    events = []
    last_active: dict[str, int] = {}
    for i, t in enumerate(times):
        rows = by_t[t]
        for src, aname in active_names.items():
            act = [r for r in rows if r["store"] == src and r["name"] == aname]
            if act:
                ino = int(act[0]["inode"])
                if src in last_active and ino != last_active[src]:
                    events.append({"event": "rotation", "source": src, "mono_ns": t,
                                   "wall_ns": int(act[0]["wall_ns"]), "old_inode": last_active[src],
                                   "new_inode": ino})
                last_active[src] = ino
        if i + 2 < len(times):
            present = {(r["store"], r["name"], r["inode"]) for r in rows}
            nxt = {(r["store"], r["inode"]) for r in by_t[times[i + 1]]} | \
                  {(r["store"], r["inode"]) for r in by_t[times[i + 2]]}
            for store, name, ino in present:
                if (store, ino) not in nxt:
                    events.append({"event": "deleted", "source": store, "name": name,
                                   "inode": int(ino), "last_seen_mono_ns": t,
                                   "wall_ns": int(rows[0]["wall_ns"])})
    return events


def jsonl_appender(path: Path):
    """Exclusive-create a JSONL file and return a thread-safe append function."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "x")
    lock = threading.Lock()

    def append(obj: dict) -> None:
        with lock:
            fh.write(json.dumps(obj) + "\n")
            fh.flush()

    append.close = fh.close   # type: ignore[attr-defined]
    return append
