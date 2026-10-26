"""Blinded rule-based reconstruction (Blueprint v3.0, Section 7). Implemented and qualified at G2.

Input: acquired_raw/ only, plus namespace and pod name and the frozen contract. Never the nonce,
UID, arm or load. Output: per (fact, source) an answer or ABSTAIN. One parser and one matcher
serve every cell; no keyword filtering of raw data. Scoring against sealed truth happens in a
separate step after all reconstruction outputs are hashed.
"""
from __future__ import annotations

from pathlib import Path


def reconstruct(acquired_raw: Path, namespace: str, pod_name: str, contract: dict) -> dict:
    raise NotImplementedError("implemented and tested on fixtures, then qualified at G2")
