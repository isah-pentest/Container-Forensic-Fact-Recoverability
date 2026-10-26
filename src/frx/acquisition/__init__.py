"""Acquisition interfaces (Blueprint v3.0, Sections 3.3-3.4). Implemented and qualified at G3.

C arm: follower from a recorded offset, rotation-aware (open by name, track inode).
D arm: one-shot copy of the active file and every rotated backup at TERMINATION + delta.
Sealed layer: 1 Hz store-growth monitor (metadata only), optional reference observer,
post-run snapshot. Sealed outputs are never mounted by reconstruction before scores lock.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class Follower(Protocol):
    def start(self, store_dir: Path, start_state: dict, out_dir: Path) -> None: ...
    def confirm_reading(self) -> bool: ...
    def drain_and_stop(self) -> dict: ...          # returns receipts + lag summary


class OneShotReader(Protocol):
    def copy_all(self, store_dir: Path, out_dir: Path) -> dict: ...   # returns receipts


class GrowthMonitor(Protocol):
    def start(self, store_dirs: list[Path], out_csv: Path) -> None: ...
    def stop(self) -> None: ...


def snapshot_store(store_dir: Path, out_dir: Path) -> dict:
    """Copy every file in a source-side store byte-for-byte. Implemented at G3."""
    raise NotImplementedError("implemented and tested at G3")
