"""Continuous-arm follower (Blueprint v3.0, Section 3.3, arm C).

Reads a producer's active log file from a recorded byte offset and follows size-based
rotation (lumberjack-style: the active file is renamed to a backup and a new file is created
under the original name). Every byte read is appended, unchanged, to one output file.
Rotation is detected by inode change; the old inode is drained through the still-open file
descriptor before switching, so no byte between the start offset and stop is skipped unless
the producer deleted a whole file before the follower reached it (recorded as a gap).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class Follower(threading.Thread):
    _receipt_lock = threading.Lock()      # followers of several stores may share one receipts file

    def __init__(self, active_path: Path, out_raw: Path, receipts: Path,
                 start_offset: int | None = None, poll_s: float = 0.1, chunk: int = 1 << 20,
                 source: str | None = None):
        super().__init__(daemon=True, name=f"follower:{Path(active_path).name}")
        self.active_path = Path(active_path)
        self.out_raw = Path(out_raw)
        self.receipts_path = Path(receipts)
        self.start_offset = start_offset
        self.poll_s = poll_s
        self.chunk = chunk
        self._stop_evt = threading.Event()   # not _stop/_started: those are threading.Thread internals
        self._ready = threading.Event()
        self.bytes_written = 0
        self.rotations = 0
        self.gaps = 0
        self.errors: list[str] = []
        self.max_lag_bytes = 0
        self._fd = None
        self._inode = None
        self._seen_inodes: set[int] = set()
        self._seen_names: set[str] = set()   # backups already read (inode numbers get reused)
        self._start_wall = time.time()
        self.source = source
        self.position = 0                    # byte offset in the current inode (for the lag sampler)
        self.current_inode = None

    # -------------------------------------------------------------- helpers
    def _receipt(self, **kw) -> None:
        kw.update(mono_ns=time.monotonic_ns(), wall_ns=time.time_ns())
        if self.source:
            kw["source"] = self.source
        with Follower._receipt_lock:
            with open(self.receipts_path, "a") as fh:
                fh.write(json.dumps(kw) + "\n")

    def _open_active(self, offset: int) -> None:
        fd = open(self.active_path, "rb")
        st = os.fstat(fd.fileno())
        if offset > st.st_size:            # file was rotated and recreated smaller
            offset = 0
        fd.seek(offset)
        self._fd, self._inode = fd, st.st_ino
        self.current_inode, self.position = st.st_ino, offset
        self._seen_inodes.add(st.st_ino)
        self._receipt(event="open", inode=st.st_ino, offset=offset, size=st.st_size)

    def _backups(self) -> list[tuple[float, Path, int, int]]:
        stem, ext = self.active_path.stem, self.active_path.suffix
        out = []
        for p in self.active_path.parent.glob(f"{stem}-*{ext}"):
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            out.append((st.st_mtime, p, st.st_ino, st.st_size))
        return sorted(out)

    def _unseen_backups(self, exclude_inode: int) -> list[tuple[Path, int]]:
        """Rotated backups (stem-<timestamp><ext>) never read, oldest first, keyed by unique name."""
        cands = [(p, ino) for mt, p, ino, _ in self._backups()
                 if p.name not in self._seen_names and ino != exclude_inode and mt >= self._start_wall]
        return sorted(cands, key=lambda t: t[0].name)

    def _read_unseen(self, out, exclude_inode: int) -> None:
        for bpath, bino in self._unseen_backups(exclude_inode):
            try:
                with open(bpath, "rb") as bf:
                    n = 0
                    while True:
                        data = bf.read(self.chunk)
                        if not data:
                            break
                        out.write(data); n += len(data)
                self.bytes_written += n
                self._seen_names.add(bpath.name)
                self._receipt(event="recovered_backup", path=str(bpath), inode=bino, bytes=n)
            except FileNotFoundError:
                self.gaps += 1
                self._seen_names.add(bpath.name)
                self._receipt(event="gap", path=str(bpath), inode=bino)

    def _drain(self, out) -> int:
        n = 0
        while True:
            data = self._fd.read(self.chunk)
            if not data:
                return n
            out.write(data)
            n += len(data)

    # -------------------------------------------------------------- thread body
    def run(self) -> None:
        self.out_raw.parent.mkdir(parents=True, exist_ok=True)
        try:
            offset = self.start_offset
            if offset is None:
                offset = self.active_path.stat().st_size
            self._open_active(offset)
        except OSError as e:
            self.errors.append(f"open: {e!r}")
            self._receipt(event="error", error=repr(e))
            self._ready.set()
            return
        self._ready.set()
        with open(self.out_raw, "ab") as out:
            while True:
                stopping = self._stop_evt.is_set()
                try:
                    self.bytes_written += self._drain(out)
                    self.position = self._fd.tell()
                    try:
                        st = self.active_path.stat()
                    except FileNotFoundError:
                        st = None              # mid-rotation: new file not created yet
                    if st is not None and st.st_ino != self._inode:
                        # rotated. The old file is final now: drain it once more through the open fd.
                        self.bytes_written += self._drain(out)
                        self.rotations += 1
                        old_inode = self._inode
                        old_pos = self._fd.tell()
                        self._fd.close()
                        for _, bp, bino, bsz in self._backups():
                            if bino == old_inode and bsz == old_pos:
                                self._seen_names.add(bp.name)
                        # backups skipped by multiple rotations between polls, oldest first
                        self._read_unseen(out, exclude_inode=st.st_ino)
                        self._open_active(0)
                        # rotations that happened between listing and opening: those backups are
                        # older than the file just opened (nothing read from it yet), so read them first
                        self._read_unseen(out, exclude_inode=self._inode)
                        self._receipt(event="rotation", old_inode=old_inode, new_inode=self._inode)
                        continue
                    if st is not None:
                        pos = self._fd.tell()
                        self.max_lag_bytes = max(self.max_lag_bytes, st.st_size - pos)
                    out.flush()
                except OSError as e:
                    self.errors.append(repr(e))
                    self._receipt(event="error", error=repr(e))
                if stopping:
                    break
                time.sleep(self.poll_s)
            out.flush()
            os.fsync(out.fileno())
        if self._fd:
            self._receipt(event="close", inode=self._inode, final_pos=self._fd.tell(),
                          bytes_written=self.bytes_written, rotations=self.rotations)
            self._fd.close()

    # -------------------------------------------------------------- control
    def wait_started(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(timeout) and not self.errors

    def confirm_reading(self, timeout: float = 5.0) -> bool:
        """ACQ_START (C): opened at the recorded offset, running, no errors."""
        return self.wait_started(timeout) and self.is_alive() and self._fd is not None

    def stop_and_drain(self, timeout: float = 30.0) -> dict:
        """Signal stop; the loop drains to end-of-file once more before exiting."""
        self._stop_evt.set()
        self.join(timeout)
        return self.summary()

    def summary(self) -> dict:
        return {"path": str(self.active_path), "source": self.source, "bytes_written": self.bytes_written,
                "rotations": self.rotations, "gaps": self.gaps, "errors": self.errors,
                "max_lag_bytes": self.max_lag_bytes, "alive": self.is_alive()}
