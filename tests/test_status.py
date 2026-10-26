import pytest

from frx.status import build_status, derive_disposition, validate_status

FLAGS_OK = {k: True for k in ("precheck_pass", "load_check_pass", "action_verified",
                              "termination_verified", "source_ready", "collector_complete",
                              "integrity_pass", "arm_compliant", "outcome_observable")}
FLAGS_OK.update({"carryover_flag": False, "oom_event": False, "drop_event": False})


def make(**over):
    kw = dict(run_id="RUN-0001", cohort="confirmatory", block="BLOCK-01", slot=2, cell="MD",
              arm="D", load="M", flags=dict(FLAGS_OK), primary_failure_code=None,
              secondary_failure_codes=[], replaces_run_id=None, delay_observed_s=60.4,
              protocol_version="3.0", code_commit="abcdef1", closed_at="2026-11-02T15:04:05Z")
    kw.update(over)
    return build_status(**kw)


def test_valid_scored_run():
    assert make()["disposition"] == "SCORED"


def test_disposition_hierarchy():
    f = dict(FLAGS_OK, load_check_pass=False)
    assert derive_disposition(f, "PA-LOAD") == "PRE_ACTION_ABORT"
    f = dict(FLAGS_OK, action_verified=False)
    assert derive_disposition(f, "UN-EXEC") == "UNSCORABLE"
    f = dict(FLAGS_OK, collector_complete=False)
    assert derive_disposition(f, "CF-READ") == "SCORED"     # intention-to-measure


def test_collector_failure_rules():
    f = dict(FLAGS_OK, collector_complete=False)
    assert make(flags=f, primary_failure_code="CF-READ")["disposition"] == "SCORED"
    with pytest.raises(ValueError):
        make(flags=f, primary_failure_code="CF-OOM")          # oom_event must be true
    with pytest.raises(ValueError):
        make(primary_failure_code="CF-READ")                  # collector_complete still true


def test_delayed_needs_observed_delay():
    with pytest.raises(ValueError):
        make(delay_observed_s=None)


def test_bad_id_and_code_rejected():
    with pytest.raises(ValueError):
        make(run_id="TRIAL-1")
    s = make()
    s["primary_failure_code"] = "XX-1"
    assert validate_status(s)
