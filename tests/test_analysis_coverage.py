"""Interval check of Delta_A (descriptive, post hoc): block-aware intervals and coverage on synthetic tables."""
import hashlib

import numpy as np
import pytest

from frx.analysis.coverage import (_shift_pvalue, all_intervals, coverage_at, marginal_p, mover_wilson_correlated,
                                   run, true_delta)
from frx.power import Scenario
from frx.stats import EFFECT_COEFS, mover_wilson, randomization_test


def _observed_like():
    y = np.ones((16, 4))
    y[:5, 3] = 0
    return y


def test_registered_interval_unchanged_and_correlated_mover_reduces_to_it():
    y = _observed_like()
    iv = all_intervals(y)
    assert iv["fallback_triggered"]
    est, lo, hi = mover_wilson([16, 16, 16, 11], [16] * 4, EFFECT_COEFS["A"])
    assert iv["registered"] == pytest.approx((lo, hi)) and iv["estimate"] == pytest.approx(-0.15625)
    assert iv["mover_wilson_correlated"] == pytest.approx((lo, hi))     # constant columns: zero correlation


def test_correlation_narrows_mover_for_positively_correlated_cells():
    rng = np.random.default_rng(1)
    u = rng.random(40) < 0.5
    y = np.column_stack([u, u | (rng.random(40) < 0.2), u, u & (rng.random(40) < 0.8)]).astype(float)
    _, lo0, hi0 = mover_wilson([int(v) for v in y.sum(0)], [40] * 4, EFFECT_COEFS["A"])
    _, lo1, hi1 = mover_wilson_correlated(y)
    assert hi1 - lo1 < hi0 - lo0


def test_inversion_matches_the_exact_test_at_zero():
    y = _observed_like()
    terms = np.concatenate([y[:, 1] - y[:, 0], y[:, 3] - y[:, 2]])
    assert _shift_pvalue(terms, 16, 0.0) == pytest.approx(0.0625)
    small = y[:6]                                       # 4**6 <= limit: the frozen test enumerates exactly
    t2 = np.concatenate([small[:, 1] - small[:, 0], small[:, 3] - small[:, 2]])
    assert _shift_pvalue(t2, 6, 0.0) == pytest.approx(randomization_test(small, "A")["p_value"])
    lo, hi = all_intervals(y)["test_inversion"]
    assert lo < -0.15625 < hi + 1e-9 and -1 <= lo


def test_truth_and_coverage():
    assert marginal_p(0.5, 1.0) == pytest.approx(0.5, abs=1e-9)
    assert true_delta({"LC": 0.9, "LD": 0.9, "MC": 0.9, "MD": 0.9}, 0.5) == pytest.approx(0.0, abs=1e-12)
    r = coverage_at(Scenario("t", {"LC": 0.9, "LD": 0.8, "MC": 0.9, "MD": 0.5}), 16, 0.5, 100, 7)
    for m in ("registered", "block_t", "mover_wilson", "mover_wilson_correlated", "test_inversion"):
        assert 0.7 <= r[f"coverage_{m}"] <= 1.0


def test_run_on_published_set(tmp_path):
    d = tmp_path / "set"
    (d / "data").mkdir(parents=True)
    lines = ["run_id,cohort,block,cell,Y"]
    y = _observed_like()
    for b in range(16):
        for j, c in enumerate(("LC", "LD", "MC", "MD")):
            lines.append(f"RUN-{4 * b + j:04d},confirmatory,BLOCK-{b + 1:02d},{c},{int(y[b, j])}")
    (d / "data" / "run_level.csv").write_text("\n".join(lines) + "\n")
    (d / "SHA256SUMS").write_text(f"{hashlib.sha256((d / 'data' / 'run_level.csv').read_bytes()).hexdigest()}  data/run_level.csv\n")
    from frx.freeze import REPO_ROOT
    r = run(d, REPO_ROOT, tmp_path / "out", n_sims=20)
    assert r["observed"]["cells"] == {"LC": 16, "LD": 16, "MC": 16, "MD": 11}
    assert len(r["coverage"]) == 6 * 4
    assert (tmp_path / "out" / "tables" / "latex" / "TS6_coverage.tex").exists()
    with pytest.raises(FileExistsError):
        run(d, REPO_ROOT, tmp_path / "out", n_sims=20)
