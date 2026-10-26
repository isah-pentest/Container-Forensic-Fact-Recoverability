import pytest

from frx.headroom import (headroom, predicted_loss_probability, rate_for_headroom,
                          rate_for_loss_probability, remaining_budget)

MB = 1_000_000


def test_budget_and_headroom():
    assert remaining_budget(MB, 2, 0.25 * MB) == pytest.approx(2.75 * MB)
    assert headroom(MB, 2, 0.25 * MB, 1.375 * MB) == pytest.approx(2.0)
    assert headroom(MB, 1, 0, 0) == float("inf")


def test_loss_probability_band():
    # k = 1, M = 1 MB, 60 s: no loss below 1 MB written, certain loss above 2 MB
    assert predicted_loss_probability(MB / 60, 60, MB, 1) == pytest.approx(0.0, abs=1e-12)
    assert predicted_loss_probability(1.5 * MB / 60, 60, MB, 1) == pytest.approx(0.5)
    assert predicted_loss_probability(3 * MB / 60, 60, MB, 1) == 1.0


def test_inverse_rates():
    r = rate_for_loss_probability(0.5, 60, MB, 1)
    assert predicted_loss_probability(r, 60, MB, 1) == pytest.approx(0.5)
    r_low = rate_for_headroom(3.0, 60, MB, 1)          # worst case: B = k*M
    assert headroom(MB, 1, MB, r_low * 60) == pytest.approx(3.0)
    with pytest.raises(ValueError):
        rate_for_headroom(3.0, 60, MB, 0)
