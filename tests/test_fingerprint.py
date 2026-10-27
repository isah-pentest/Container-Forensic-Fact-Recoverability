from frx.fingerprint import stable_hash

BASE = {"host": {"kernel": "7.0.0"}, "tools": {"kind": "v0.33.0", "git_commit": "abc"},
        "cluster": {"node_info": {"kubeletVersion": "v1.37.0", "bootID": "b1"}},
        "files": {"cluster/audit-policy.yaml": "p1", "protocol/parameters.yaml": "x1"}}


def _with(**changes):
    import copy
    fp = copy.deepcopy(BASE)
    for path, v in changes.items():
        cur = fp
        *head, last = path.split("__")
        for h in head:
            cur = cur[h]
        cur[last] = v
    return fp


def test_stable_hash_ignores_boot_commit_and_protocol_files():
    h = stable_hash(BASE)
    assert stable_hash(_with(cluster__node_info__bootID="b2")) == h
    assert stable_hash(_with(tools__git_commit="def")) == h
    fp = _with(); fp["files"]["protocol/parameters.yaml"] = "x2"
    assert stable_hash(fp) == h


def test_stable_hash_detects_platform_changes():
    h = stable_hash(BASE)
    assert stable_hash(_with(host__kernel="7.0.1")) != h
    assert stable_hash(_with(cluster__node_info__kubeletVersion="v1.37.1")) != h
    fp = _with(); fp["files"]["cluster/audit-policy.yaml"] = "p2"
    assert stable_hash(fp) != h
