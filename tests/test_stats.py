import numpy as np
import pytest

import frx.stats as st


def test_wilson_and_cp_known_values():
    lo, hi = st.wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.27753, abs=1e-4)
    lo, hi = st.clopper_pearson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(1 - 0.025 ** 0.1, abs=1e-6)
    lo, hi = st.clopper_pearson(10, 10)
    assert hi == 1.0


def test_mover_single_proportion_equals_wilson():
    est, lo, hi = st.mover_wilson([3], [10], [1.0])
    w = st.wilson(3, 10)
    assert est == pytest.approx(0.3) and (lo, hi) == pytest.approx(w)


def test_block_t_ci_hand_computed():
    m, lo, hi = st.block_t_ci(np.array([0.0, -0.5, 0.0, -0.5]))
    se = np.std([0, -0.5, 0, -0.5], ddof=1) / 2
    assert m == pytest.approx(-0.25)
    assert lo == pytest.approx(-0.25 - 3.182446 * se, abs=1e-4)
    assert hi == pytest.approx(-0.25 + 3.182446 * se, abs=1e-4)


def test_randomization_exact_small():
    y = np.array([[1, 0, 1, 0], [1, 0, 1, 0]], dtype=float)
    r = st.randomization_test(y, "A")
    assert r["method"] == "exact"
    assert r["statistic"] == pytest.approx(-1.0)
    assert r["p_value"] == pytest.approx(2 / 16)


def test_randomization_null_table():
    y = np.ones((6, 4))
    assert st.randomization_test(y, "A")["p_value"] == 1.0


def test_monte_carlo_agrees_with_exact(monkeypatch):
    rng = np.random.default_rng(3)
    y = (rng.random((8, 4)) < [0.9, 0.5, 0.9, 0.4]).astype(float)
    exact = st.randomization_test(y, "A")["p_value"]
    monkeypatch.setattr(st, "EXACT_LIMIT", 0)
    mc = st.randomization_test(y, "A", n_draws=200_000, seed=11)["p_value"]
    assert mc == pytest.approx(exact, abs=0.01)


def test_effect_S_uses_load_pairs():
    # load effect only: L cells succeed, M cells fail
    y = np.tile([1, 1, 0, 0], (4, 1)).astype(float)
    assert st.randomization_test(y, "A")["p_value"] == 1.0
    r = st.randomization_test(y, "S")
    assert r["statistic"] == pytest.approx(-1.0) and r["p_value"] == pytest.approx(2 / 2 ** 8)


def test_fallback_trigger_rules():
    y = np.tile([1, 1, 1, 0], (10, 1)).astype(float)       # MD all zero -> trigger
    assert st.fallback_triggered(y)
    rng = np.random.default_rng(1)
    y2 = (rng.random((20, 4)) < [0.7, 0.4, 0.7, 0.4]).astype(float)
    assert not st.fallback_triggered(y2)


def test_primary_analysis_reports_rule():
    y = np.tile([1, 1, 1, 0], (10, 1)).astype(float)
    out = st.primary_analysis(y, "A", n_draws=5000, seed=1)
    assert out["fallback_triggered"] and out["ci_rule"].startswith("MOVER")
    assert out["estimate"] == pytest.approx(-0.5)
    with pytest.raises(ValueError):
        st.primary_analysis(np.array([[1, 0.5, 1, 0]]))


def test_mcnemar():
    assert st.mcnemar_exact(0, 5) == pytest.approx(0.0625)
    assert st.mcnemar_exact(0, 0) == 1.0
