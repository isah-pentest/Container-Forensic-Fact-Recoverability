from frx.power import Scenario, choose_b, power_at


def test_type_one_error_controlled():
    null = Scenario("null", {"LC": 0.8, "LD": 0.8, "MC": 0.8, "MD": 0.8})
    r = power_at(null, b=12, sigma_b=0.5, n_sims=400, n_draws=500, seed=7)
    assert r["power"] <= 0.09          # nominal 0.05; generous Monte Carlo margin


def test_power_matches_exact_binomial():
    # With p_LC = p_LD = p_MC = 1 and p_MD = 0.5, only failing MD blocks give nonzero terms,
    # all with the same sign; the exact test rejects iff at least 6 MD failures (2/2**6 < 0.05).
    # Power = P(Binomial(B, 0.5) >= 6): B=12 -> 0.613, B=16 -> 0.895.
    from scipy.stats import binom
    alt = Scenario("alt", {"LC": 1.0, "LD": 1.0, "MC": 1.0, "MD": 0.5})
    for b in (12, 16):
        sim = power_at(alt, b=b, sigma_b=0.0, n_sims=600, n_draws=500, seed=2)["power"]
        assert abs(sim - (1 - binom.cdf(5, b, 0.5))) < 0.06


def test_choose_b_rule():
    rows = [{"scenario": s, "B": b, "sigma_b": 0.5, "power": pw}
            for s, b, pw in [("p", 10, 0.75), ("c", 10, 0.6), ("p", 12, 0.82), ("c", 12, 0.71),
                             ("p", 30, 0.99), ("c", 30, 0.99)]]
    assert choose_b(rows, "p", "c") == 12
    assert choose_b([r for r in rows if r["B"] == 30], "p", "c") is None   # beyond B_max
