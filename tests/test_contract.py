from frx import contract as c


def test_repo_contract_structure():
    con = c.load_yaml(c.CONTRACT_PATH)
    assert c.check_contract(con) == []
    assert c.check_parameters(c.load_yaml(c.PARAMETERS_PATH)) == []


def test_placeholders_block_freeze_until_resolved():
    res = c.freeze_readiness()
    assert res["unresolved"], "placeholders are expected before gates G1-G5"
    assert any("process.binary" in u for u in res["unresolved"])


def test_f2_must_be_dual_eligible():
    con = c.load_yaml(c.CONTRACT_PATH)
    con["facts"]["F2"]["eligible"] = ["audit"]
    assert any("dual-eligible" in p for p in c.check_contract(con))
