import numpy as np
import pytest

from frx.metrics import (block_contrasts, block_matrix, cell_proportions, deltas,
                         f2_paired_table, incremental_contribution, run_outcome,
                         source_rates, union_state)


def test_union_rule():
    assert union_state({"audit": "CORRECT", "tetragon": "ABSTAIN"}) == "CORRECT"
    assert union_state({"audit": "CORRECT", "tetragon": "FALSE"}) == "CONFLICT"
    assert union_state({"audit": "ABSTAIN", "tetragon": "ABSTAIN"}) == "NOT_RECOVERED"
    assert union_state({"audit": "CORRECT", "tetragon": "NOT_ELIGIBLE"}) == "CORRECT"
    with pytest.raises(ValueError):
        union_state({"audit": "NOT_ELIGIBLE"})
    with pytest.raises(ValueError):
        union_state({"audit": "MAYBE"})


def test_run_outcome_conflict_not_recovered():
    fs = {"F1": {"audit": "CORRECT"},
          "F2": {"audit": "CORRECT", "tetragon": "FALSE"},
          "F3": {"audit": "CORRECT"}}
    y, c = run_outcome(fs)
    assert y == 0 and c == pytest.approx(2 / 3)


def test_run_outcome_all_correct():
    fs = {"F1": {"audit": "CORRECT"}, "F2": {"audit": "ABSTAIN", "tetragon": "CORRECT"},
          "F3": {"audit": "CORRECT"}}
    assert run_outcome(fs) == (1, 1.0)


def test_cell_proportions_ignore_controls():
    runs = [{"cell": "LC", "Y": 1}, {"cell": "LC", "Y": 0}, {"cell": "NC", "Y": 1}]
    p = cell_proportions(runs)
    assert p["LC"] == (1, 2, 0.5)
    assert p["MD"][1] == 0


def test_deltas_hand_computed():
    d = deltas({"LC": 1.0, "LD": 0.5, "MC": 1.0, "MD": 0.0})
    assert d["delta_A"] == pytest.approx(-0.75)
    assert d["delta_S"] == pytest.approx(-0.25)
    assert d["delta_AS"] == pytest.approx(-0.5)


def test_block_matrix_and_equivalence():
    runs = []
    data = {"B1": [1, 1, 1, 0], "B2": [1, 0, 1, 1], "B3": [1, 1, 0, 0]}
    for b, ys in data.items():
        for c, y in zip(("LC", "LD", "MC", "MD"), ys):
            runs.append({"block": b, "cell": c, "Y": y})
    runs.append({"block": "B4", "cell": "LC", "Y": 1})          # incomplete block
    y, complete, incomplete = block_matrix(runs)
    assert complete == ["B1", "B2", "B3"] and incomplete == ["B4"]
    p = cell_proportions([r for r in runs if r["block"] != "B4"])
    assert block_contrasts(y)["A"].mean() == pytest.approx(deltas({c: v[2] for c, v in p.items()})["delta_A"])


def test_block_matrix_rejects_duplicates():
    runs = [{"block": "B1", "cell": "LC", "Y": 1}, {"block": "B1", "cell": "LC", "Y": 0}]
    with pytest.raises(ValueError):
        block_matrix(runs)


def test_source_rates_exclude_not_eligible():
    r = source_rates(["CORRECT", "FALSE", "ABSTAIN", "NOT_ELIGIBLE", "CORRECT"])
    assert r["FALSE"] == (1, 4) and r["CORRECT"] == (2, 4)


def test_f2_tables():
    pairs = [("CORRECT", "CORRECT"), ("CORRECT", "ABSTAIN"), ("ABSTAIN", "CORRECT"),
             ("ABSTAIN", "ABSTAIN"), ("CORRECT", "FALSE")]
    assert f2_paired_table(pairs) == {"n11": 1, "n10": 2, "n01": 1, "n00": 1}
    inc = incremental_contribution(pairs)
    # union CORRECT in the first three pairs; the conflict pair is excluded
    assert inc["I_audit"] == (1, 3) and inc["I_tetragon"] == (1, 3)
