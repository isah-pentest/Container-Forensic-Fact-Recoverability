"""Action harness interface (Blueprint v3.0, Section 7.1). Implemented and qualified at G2.

The harness runs outside the evaluated telemetry path. Every step verifies its own result and
writes to ground_truth/ and sealed/truth/ with exclusive-create. No error is discarded.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RunIdentity:
    run_id: str
    namespace: str      # frx-<short-uuid>
    pod_name: str       # tgt-<short-uuid>
    nonce_hex: str      # 128-bit, never given to the reconstructor


class Harness(Protocol):
    def create_pod_and_verify(self, ident: RunIdentity) -> dict:
        """POST pod; require 201 + metadata.uid; wait for Ready (frozen timeout). Raise on failure."""
    def exec_nonce_and_verify(self, ident: RunIdentity) -> dict:
        """Exec '<binary> FRX-<nonce>'; require exit 0 and nonce in stdout. Raise on failure."""
    def delete_pod_and_verify(self, ident: RunIdentity) -> dict:
        """DELETE pod; require success code; record response UID. Raise on failure."""
    def poll_until_404(self, ident: RunIdentity) -> dict:
        """GET every 0.5 s until 404; return TERMINATION_CONFIRMED timestamps. Raise on timeout."""
