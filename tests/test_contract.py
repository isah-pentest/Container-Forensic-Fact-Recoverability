from frx import contract as c


def test_repo_contract_structure():
    con = c.load_yaml(c.CONTRACT_PATH)
    assert c.check_contract(con) == []
    assert c.check_parameters(c.load_yaml(c.PARAMETERS_PATH)) == []


def test_placeholders_block_freeze_until_resolved():
    # After G2/G3 no G1/G2 value remains; the G5 values block freeze until they are set.
    res = c.freeze_readiness()
    assert not [u for u in res["unresolved"] if "<G1>" in u or "<G2>" in u]
    assert {"design.master_seed = <G5>", "design.n_blocks = <G5>"} <= set(res["unresolved"])


def test_unresolved_detects_placeholders():
    assert c.unresolved({"a": {"b": "<G2>"}, "c": ["<G5>", 1]}) == ["a.b = <G2>", "c[0] = <G5>"]


def test_f2_must_be_dual_eligible():
    con = c.load_yaml(c.CONTRACT_PATH)
    con["facts"]["F2"]["eligible"] = ["audit"]
    assert any("dual-eligible" in p for p in c.check_contract(con))
