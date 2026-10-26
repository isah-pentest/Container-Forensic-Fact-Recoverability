"""Loading and checking protocol/contract.yaml and protocol/parameters.yaml (Sections 6, 7)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .metrics import PRIMARY_FACTS, STATES

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPO_ROOT / "protocol" / "contract.yaml"
PARAMETERS_PATH = REPO_ROOT / "protocol" / "parameters.yaml"


def load_yaml(path: Path) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


def unresolved(obj: Any, path: str = "") -> list[str]:
    """Paths of values still written as '<Gx>' placeholders."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += unresolved(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += unresolved(v, f"{path}[{i}]")
    elif isinstance(obj, str) and obj.startswith("<G") and obj.endswith(">"):
        out.append(f"{path} = {obj}")
    return out


def check_contract(contract: dict) -> list[str]:
    """Structural checks that hold at every stage (placeholders allowed)."""
    p = []
    if tuple(contract.get("primary_facts", ())) != PRIMARY_FACTS:
        p.append("primary_facts must be [F1, F2, F3]")
    if tuple(contract.get("scoring_states", ())) != STATES:
        p.append("scoring_states must be CORRECT, FALSE, ABSTAIN, NOT_ELIGIBLE")
    facts = contract.get("facts", {})
    for f in PRIMARY_FACTS:
        spec = facts.get(f)
        if not spec:
            p.append(f"{f} missing")
            continue
        for src in spec.get("eligible", []):
            if src not in spec:
                p.append(f"{f}: eligible source {src} has no match rule")
    if facts.get("F2", {}).get("eligible") != ["audit", "tetragon"]:
        p.append("F2 must be dual-eligible [audit, tetragon] (otherwise protocol v3.1)")
    w = contract.get("run_window", {})
    if not (w.get("start_offset_s", 0) <= 0 <= w.get("end_offset_s", -1)):
        p.append("run_window offsets malformed")
    return p


def check_parameters(params: dict) -> list[str]:
    p = []
    t = params.get("timing", {})
    if not t.get("grace_s", 0) < t.get("delay_s", 0):
        p.append("timing: grace_s must be < delay_s")
    if params.get("design", {}).get("b_max") != 24:
        p.append("design.b_max is locked at 24")
    for src in ("audit", "tetragon"):
        k = params.get("stores", {}).get(src, {}).get("max_backups")
        if isinstance(k, int) and k < 1:
            p.append(f"stores.{src}.max_backups must be >= 1")
    return p


def freeze_readiness() -> dict:
    """What still blocks G5: structural problems plus unresolved placeholders."""
    c, prm = load_yaml(CONTRACT_PATH), load_yaml(PARAMETERS_PATH)
    return {
        "contract_problems": check_contract(c),
        "parameter_problems": check_parameters(prm),
        "unresolved": unresolved(c) + unresolved(prm),
    }
