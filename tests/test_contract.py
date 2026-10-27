from frx import contract as c


def test_repo_contract_structure():
    con = c.load_yaml(c.CONTRACT_PATH)
    assert c.check_contract(con) == []
    assert c.check_parameters(c.load_yaml(c.PARAMETERS_PATH)) == []


def test_placeholders_block_freeze_until_resolved():
    # After G2/G3 only the G5 values remain; freeze stays blocked until they are set.
    res = c.freeze_readiness()
    assert sorted(res["unresolved"]) == ["design.master_seed = <G5>", "design.n_blocks = <G5>"]


def test_unresolved_detects_placeholders():
    assert c.unresolved({"a": {"b": "<G2>"}, "c": ["<G5>", 1]}) == ["a.b = <G2>", "c[0] = <G5>"]


def test_f2_must_be_dual_eligible():
    con = c.load_yaml(c.CONTRACT_PATH)
    con["facts"]["F2"]["eligible"] = ["audit"]
    assert any("dual-eligible" in p for p in c.check_contract(con))
