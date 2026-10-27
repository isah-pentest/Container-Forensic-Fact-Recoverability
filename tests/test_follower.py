import os
import threading
import time
from pathlib import Path

from frx.acquisition.follower import Follower


class Producer:
    """Lumberjack-style writer: rename active -> stem-<n>.ext at max_bytes, keep k backups."""

    def __init__(self, d: Path, max_bytes: int, k: int):
        self.d, self.max, self.k = d, max_bytes, k
        self.active = d / "audit.json"
        self.n = 0
        self.written = bytearray()
        self.active.write_bytes(b"")

    def write_line(self, i: int):
        line = f'{{"i":{i},"pad":"{"x" * 40}"}}\n'.encode()
        if self.active.stat().st_size + len(line) > self.max:
            self.rotate()
        with open(self.active, "ab") as fh:
            fh.write(line)
        self.written += line

    def rotate(self):
        self.n += 1
        os.rename(self.active, self.d / f"audit-{self.n:06d}.json")
        self.active.write_bytes(b"")
        backups = sorted(self.d.glob("audit-*.json"))
        for old in backups[: max(0, len(backups) - self.k)]:
            old.unlink()


def test_follows_rotation_byte_exact(tmp_path):
    p = Producer(tmp_path, max_bytes=2000, k=3)
    for i in range(10):
        p.write_line(i)
    start = p.active.stat().st_size
    already = len(p.written)
    f = Follower(p.active, tmp_path / "out" / "a.raw", tmp_path / "out" / "r.jsonl", poll_s=0.01)
    f.start(); assert f.wait_started()
    for i in range(10, 400):
        p.write_line(i)
        time.sleep(0.001)          # producer paced like a real one: no file evicted before it is read
    time.sleep(0.2)
    s = f.stop_and_drain()
    got = (tmp_path / "out" / "a.raw").read_bytes()
    assert got == bytes(p.written[already:])           # byte-exact from the start offset
    assert s["rotations"] >= 5 and s["gaps"] == 0 and not s["errors"]


def test_recovers_double_rotation_between_polls(tmp_path):
    p = Producer(tmp_path, max_bytes=500, k=5)
    f = Follower(p.active, tmp_path / "out" / "a.raw", tmp_path / "out" / "r.jsonl", start_offset=0, poll_s=0.5)
    f.start(); assert f.wait_started()
    time.sleep(0.1)
    for i in range(30):          # several rotations inside one 0.5 s poll interval
        p.write_line(i)
    time.sleep(1.2)
    s = f.stop_and_drain()
    assert (tmp_path / "out" / "a.raw").read_bytes() == bytes(p.written)
    assert s["gaps"] == 0
    assert "recovered_backup" in (tmp_path / "out" / "r.jsonl").read_text()


def test_evicted_intermediate_file_is_a_gap(tmp_path):
    p = Producer(tmp_path, max_bytes=300, k=0)     # backups deleted immediately
    f = Follower(p.active, tmp_path / "out" / "a.raw", tmp_path / "out" / "r.jsonl", start_offset=0, poll_s=0.5)
    f.start(); assert f.wait_started()
    time.sleep(0.1)
    for i in range(40):
        p.write_line(i)
    time.sleep(1.2)
    f.stop_and_drain()
    got = (tmp_path / "out" / "a.raw").read_bytes()
    assert len(got) < len(p.written)               # data lost before the follower could read it
    # the loss is not silent: either a recorded gap or a rotation with less data than produced
    receipts = (tmp_path / "out" / "r.jsonl").read_text()
    assert "rotation" in receipts


def test_stress_many_rotations_between_polls_is_byte_exact_and_ordered(tmp_path):
    # Regression test: rotations between listing and opening once reordered data.
    p = Producer(tmp_path, max_bytes=3000, k=500)
    for i in range(10):
        p.write_line(i)
    already = len(p.written)
    f = Follower(p.active, tmp_path / "out" / "a.raw", tmp_path / "out" / "r.jsonl", poll_s=0.05)
    f.start(); assert f.wait_started()
    for i in range(10, 5000):          # unpaced: ~100 rotations, many inside one poll interval
        p.write_line(i)
    time.sleep(0.3)
    s = f.stop_and_drain()
    assert (tmp_path / "out" / "a.raw").read_bytes() == bytes(p.written[already:])
    assert s["gaps"] == 0 and not s["errors"] and p.n >= 90
