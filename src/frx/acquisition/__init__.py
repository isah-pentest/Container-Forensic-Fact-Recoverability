"""Acquisition (Blueprint v3.0, Sections 3.3-3.4).

C arm: Follower (follower.py), from the offset recorded at SOURCE_READY, rotation-aware.
D arm: one_shot_copy (store.py) of every present file at TERMINATION + delta.
Sealed layer: growth monitor (frx.monitors), reference observers (reference.py), post-run
snapshot (store.snapshot_store). Sealed outputs are never staged for reconstruction.
"""
from .follower import Follower
from .store import (ACTIVE_NAMES, LagSampler, finalize_raw, jsonl_appender, one_shot_copy,
                    rotation_events, snapshot_store, start_state)

__all__ = ["ACTIVE_NAMES", "Follower", "LagSampler", "finalize_raw", "jsonl_appender",
           "one_shot_copy", "rotation_events", "snapshot_store", "start_state"]
