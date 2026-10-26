import pytest
import yaml

from frx.cluster import (AUDIT_FILE_IN_NODE, KIND_V0_33_DEFAULT_IMAGE, StoreSizes,
                         render_kind_config, render_tetragon_values)
from frx.status import REPO_ROOT

SIZES = StoreSizes(10, 5, 10, 5)


def test_kind_config_renders_audit_and_mounts(tmp_path):
    cfg = yaml.safe_load(render_kind_config(REPO_ROOT, tmp_path / "stores", SIZES))
    node = cfg["nodes"][0]
    assert node["image"] == KIND_V0_33_DEFAULT_IMAGE
    patch = yaml.safe_load(node["kubeadmConfigPatches"][0])
    args = patch["apiServer"]["extraArgs"]
    assert args["audit-log-path"] == AUDIT_FILE_IN_NODE
    assert args["audit-log-maxsize"] == "10" and args["audit-log-maxbackup"] == "5"
    mounts = {m["containerPath"]: m["hostPath"] for m in node["extraMounts"]}
    assert mounts["/procHost"] == "/proc"
    assert mounts["/var/log/tetragon"].endswith("stores/tetragon")
    assert mounts["/var/log/kubernetes/audit"].endswith("stores/audit")


def test_rendering_is_deterministic(tmp_path):
    a = render_kind_config(REPO_ROOT, tmp_path, SIZES)
    b = render_kind_config(REPO_ROOT, tmp_path, SIZES)
    assert a == b


def test_refuses_unpinned_image_windows_paths_and_zero_backups(tmp_path):
    with pytest.raises(ValueError):
        render_kind_config(REPO_ROOT, tmp_path, SIZES, node_image="kindest/node:v1.37.0")
    with pytest.raises(ValueError):
        render_kind_config(REPO_ROOT, "/mnt/c/Users/x/stores", SIZES)
    with pytest.raises(ValueError):
        render_kind_config(REPO_ROOT, tmp_path, StoreSizes(10, 0, 10, 5))


def test_tetragon_values_keys():
    v = yaml.safe_load(render_tetragon_values(SIZES))
    assert v["exportDirectory"] == "/var/log/tetragon"          # top-level in the chart
    t = v["tetragon"]
    assert t["hostProcPath"] == "/procHost" and t["exportFilePerm"] == "644"
    assert t["exportFileMaxSizeMB"] == 10 and t["exportFileMaxBackups"] == 5
    assert "PROCESS_EXEC" in t["exportAllowList"] and "PROCESS_EXIT" in t["exportAllowList"]
