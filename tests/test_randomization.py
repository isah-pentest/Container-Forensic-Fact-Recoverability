from collections import Counter

from frx.randomization import make_schedule, write_schedule


def test_deterministic_and_complete(tmp_path):
    a = make_schedule(20261026, 6)
    b = make_schedule(20261026, 6)
    assert a == b
    assert make_schedule(1, 6) != a
    for blk in {r["block"] for r in a}:
        cells = Counter(r["cell"] for r in a if r["block"] == blk)
        assert cells == Counter({"LC": 1, "LD": 1, "MC": 1, "MD": 1, "NC": 1})
    h1 = write_schedule(a, tmp_path / "s1.csv", 20261026)
    h2 = write_schedule(b, tmp_path / "s2.csv", 20261026)
    assert h1 == h2


def test_factor_labels():
    for r in make_schedule(5, 3):
        if r["cell"] != "NC":
            assert r["load"] == r["cell"][0] and r["arm"] == r["cell"][1]
        assert r["load"] in "LM" and r["arm"] in "CD"
