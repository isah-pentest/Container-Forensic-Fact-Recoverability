"""G5: confirmation stage, master-seed rule, freeze and registration checks (Sections 12.5, 16)."""
import json
import subprocess

import pytest

from frx.freeze import (PROTOCOL_FILES, check_freeze, check_ready_for_run_ids, seed_from_output_value,
                        write_freeze, write_registration)
from frx.power import Scenario, confirm


def test_seed_rule_first_64_bits():
    ov = "0123456789ABCDEF" + "f" * 112
    assert seed_from_output_value(ov) == 0x0123456789ABCDEF
    with pytest.raises(ValueError):
        seed_from_output_value("xyz")


def test_confirm_reports_checks():
    sc = {"p": Scenario("p", {"LC": 1.0, "LD": 1.0, "MC": 1.0, "MD": 0.2}),
          "c": Scenario("c", {"LC": 0.95, "LD": 0.95, "MC": 0.95, "MD": 0.3}),
          "cal": Scenario("cal", {"LC": 0.5, "LD": 0.5, "MC": 0.5, "MD": 0.5})}
    r = confirm(sc, 12, 5, "p", "c", "cal", ["cal"], n_sims=200, n_draws=300)
    assert set(r["checks"]) == {"primary", "conservative", "type_one_matches_exact_size", "type_one_le_alpha"}
    assert r["rows"]["p"]["power"] > 0.8 and r["rows"]["cal"]["power"] < 0.12


def _repo(tmp_path):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t"); git("config", "user.name", "t")
    for f in PROTOCOL_FILES:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text(f"content of {f}\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("x = 1\n")
    git("add", "-A"); git("commit", "-qm", "base")
    return tmp_path, git


def test_freeze_then_detect_changes_and_registration(tmp_path):
    repo, git = _repo(tmp_path)
    rec = write_freeze(repo, check_readiness=False, run_tests=False)
    assert rec["code"]["files"] == 1 and check_freeze(repo)["ok"]
    with pytest.raises(FileExistsError):
        write_freeze(repo, check_readiness=False, run_tests=False)                  # never re-frozen
    r = check_ready_for_run_ids(repo)
    assert not r["ok"] and any("registration" in p for p in r["problems"])
    write_registration(repo, "OSF Registries", "https://osf.io/abcde", "2026-10-27T09:00:00Z")
    assert check_ready_for_run_ids(repo)["ok"]
    (repo / "protocol" / "parameters.yaml").write_text("changed\n")
    assert check_freeze(repo)["problems"] == ["protocol/parameters.yaml changed"]
    (repo / "protocol" / "parameters.yaml").write_text("content of protocol/parameters.yaml\n")
    (repo / "src" / "x.py").write_text("x = 2\n")
    assert not check_freeze(repo)["ok"]


def test_freeze_refuses_dirty_tree(tmp_path):
    repo, git = _repo(tmp_path)
    (repo / "src" / "x.py").write_text("x = 3\n")
    with pytest.raises(RuntimeError, match="commit everything"):
        write_freeze(repo, check_readiness=False, run_tests=False)


def test_sign_test_size_lattice():
    from frx.power import sign_test_size
    # 24 nonzero terms: P(|S| >= 10) = 0.064 >= 0.05, P(|S| >= 12) = 0.0227 -> size 0.0227
    assert abs(sign_test_size(24) - 0.02266) < 1e-4
    assert sign_test_size(4) == 0.0                      # with 4 terms the smallest p is 2/16 = 0.125
    assert abs(sign_test_size(6) - 2 / 64) < 1e-12       # all six signs equal


def test_simulated_null_matches_exact_size():
    from frx.power import power_at
    r = power_at(Scenario("n", {"LC": .5, "LD": .5, "MC": .5, "MD": .5}), 12, 0.5, 600, 2000, 3)
    se = (r["exact_null_size"] * (1 - r["exact_null_size"]) / 600) ** 0.5
    assert abs(r["power"] - r["exact_null_size"]) <= 3 * se and r["power"] <= 0.05


def test_freeze_refuses_when_tests_fail(tmp_path):
    repo, git = _repo(tmp_path)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_fails():\n    assert False\n")
    git("add", "-A"); git("commit", "-qm", "failing test")
    with pytest.raises(RuntimeError, match="test suite failed"):
        write_freeze(repo, check_readiness=False, run_tests=True)
    assert not (repo / "protocol" / "FREEZE.json").exists()
