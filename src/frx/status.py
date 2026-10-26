"""run_status.json construction and validation (Section 10).

Disposition is derived from flags and failure codes only; the outcome Y is never an input.
"""
from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "protocol" / "schemas" / "run_status.schema.json"


def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))


def derive_disposition(flags: dict, primary_failure_code: str | None) -> str:
    """Section 10.1 hierarchy, applied in order."""
    code = primary_failure_code or ""
    if code.startswith("PA-") or not flags.get("precheck_pass", False) \
            or not flags.get("load_check_pass", False) or not flags.get("source_ready", False):
        return "PRE_ACTION_ABORT"
    if code.startswith("UN-") or not flags.get("action_verified", False) \
            or not flags.get("termination_verified", False):
        return "UNSCORABLE"
    return "SCORED"


def consistency_problems(status: dict) -> list[str]:
    """Rules the JSON schema cannot express."""
    p = []
    f = status.get("flags", {})
    d = status.get("disposition")
    code = status.get("primary_failure_code") or ""
    if d != derive_disposition(f, code or None):
        p.append(f"disposition {d} inconsistent with flags/code (expected "
                 f"{derive_disposition(f, code or None)})")
    if d == "PRE_ACTION_ABORT" and code and not code.startswith("PA-"):
        p.append("PRE_ACTION_ABORT requires a PA-* code")
    if d == "UNSCORABLE" and not code.startswith("UN-"):
        p.append("UNSCORABLE requires a UN-* code")
    if d == "SCORED" and code and not code.startswith("CF-"):
        p.append("SCORED runs may carry only a CF-* primary code")
    if d == "SCORED" and code.startswith("CF-") and f.get("collector_complete", True) \
            and code != "CF-INTEG":
        p.append("CF-FOLLOW/CF-READ/CF-OOM requires collector_complete = false")
    if code == "CF-OOM" and not f.get("oom_event", False):
        p.append("CF-OOM requires oom_event = true")
    if status.get("arm") == "D" and d == "SCORED" and status.get("delay_observed_s") is None \
            and status.get("cell") != "NC":
        p.append("scored D run needs delay_observed_s")
    return p


def validate_status(status: dict) -> list[str]:
    errs = [f"schema: {e.message}" for e in _validator().iter_errors(status)]
    if not errs:
        errs += consistency_problems(status)
    return errs


def build_status(*, run_id: str, cohort: str, block: str | None, slot: int | None, cell: str,
                 arm: str, load: str, flags: dict, primary_failure_code: str | None,
                 secondary_failure_codes: list[str], replaces_run_id: str | None,
                 delay_observed_s: float | None, protocol_version: str, code_commit: str,
                 closed_at: str, notes: str = "") -> dict:
    status = {
        "run_id": run_id, "cohort": cohort, "block": block, "slot": slot, "cell": cell,
        "arm": arm, "load": load,
        "disposition": derive_disposition(flags, primary_failure_code),
        "primary_failure_code": primary_failure_code,
        "secondary_failure_codes": list(secondary_failure_codes),
        "flags": dict(flags), "replaces_run_id": replaces_run_id,
        "delay_observed_s": delay_observed_s, "protocol_version": protocol_version,
        "code_commit": code_commit, "closed_at": closed_at, "notes": notes,
    }
    errs = validate_status(status)
    if errs:
        raise ValueError("; ".join(errs))
    return status
