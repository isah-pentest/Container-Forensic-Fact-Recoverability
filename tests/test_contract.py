from frx import contract as c


def test_repo_contract_structure():
    con = c.load_yaml(c.CONTRACT_PATH)
    assert c.check_contract(con) == []
    assert c.check_parameters(c.load_yaml(c.PARAMETERS_PATH)) == []


def test_no_value_before_g5_is_still_open():
    # Gate-state independent: G1-G4 values are fixed; G5 values may be open or fixed. Freeze
    # refusal while placeholders remain is tested in test_unresolved_detects_placeholders and
    # test_freeze_g5 (a test that asserted the G5 values were still open went stale at G5).
    res = c.freeze_readiness()
    assert [u for u in res["unresolved"] if "<G5>" not in u] == []


def test_unresolved_detects_placeholders():
    assert c.unresolved({"a": {"b": "<G2>"}, "c": ["<G5>", 1]}) == ["a.b = <G2>", "c[0] = <G5>"]


def test_f2_must_be_dual_eligible():
    con = c.load_yaml(c.CONTRACT_PATH)
    con["facts"]["F2"]["eligible"] = ["audit"]
    assert any("dual-eligible" in p for p in c.check_contract(con))
