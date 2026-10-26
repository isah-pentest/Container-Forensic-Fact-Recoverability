"""Orchestrator (Blueprint v3.0, Sections 9, 10 and 19). Implemented at G1-G3.

Hard rules enforced here:
- refuses to issue RUN- IDs unless protocol/FREEZE.json exists and its hashes match the tree;
- journals every state before continuing; never reuses a run ID;
- next run only after verify_package passes; next block only after verify_block passes;
- applies stop rules (Section 10.4) without inspecting outcomes.
"""
from __future__ import annotations

STATES = (
    "PRECHECK", "LOAD_READY", "SOURCE_READY", "ACQ_START_C", "ACTION_REQUEST",
    "CREATE_CONFIRMED", "EXEC_CONFIRMED", "DELETE_REQUESTED", "TERMINATION_CONFIRMED",
    "ACQ_END_C", "ACQ_START_D", "ACQ_END_D", "FLUSH_ACK", "SNAPSHOT_SEALED", "CLEANUP",
    "PACKAGE_CLOSED", "SCORED",
)


def require_freeze() -> None:
    """Raise unless protocol/FREEZE.json exists and matches the working tree. Implemented at G5."""
    raise NotImplementedError("implemented with the freeze procedure at G5")
