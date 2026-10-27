"""Render the kind cluster config and Tetragon Helm values (Blueprint v3.0, Sections 3.1, 6.3, 14).

Rendering is deterministic: the same inputs give byte-identical files, whose hashes are
recorded at each gate. Store sizes are provisional until G3 fixes M_q and k_q (Section 4.3);
changing them requires recreating the cluster.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

# kind v0.33.0 default node image (release notes; the digest is required).
KIND_V0_33_DEFAULT_IMAGE = (
    "kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5"
)
CLUSTER_NAME = "frx"
AUDIT_DIR_IN_NODE = "/var/log/kubernetes/audit"
AUDIT_FILE_IN_NODE = f"{AUDIT_DIR_IN_NODE}/audit.json"
POLICY_IN_NODE = "/etc/kubernetes/policies/audit-policy.yaml"
WEBHOOK_KUBECONFIG_IN_NODE = "/etc/kubernetes/policies/audit-webhook.kubeconfig"
TETRAGON_DIR_IN_NODE = "/var/log/tetragon"
TETRAGON_FILE = "tetragon.log"


class _Dumper(yaml.SafeDumper):
    """Multi-line strings (the kubeadm patch) as readable literal blocks."""


def _str_rep(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_rep)


@dataclass(frozen=True)
class StoreSizes:
    audit_max_mb: int
    audit_backups: int
    tetragon_max_mb: int
    tetragon_backups: int
    provisional: bool = True

    def check(self) -> None:
        for name in ("audit_max_mb", "tetragon_max_mb"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1 MB")
        for name in ("audit_backups", "tetragon_backups"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1 (Section 4.3 needs k >= 1)")


def render_audit_webhook_kubeconfig(server_url: str) -> str:
    """Kubeconfig for the audit webhook backend: the sealed reference observer (Section 3.4)."""
    if not server_url.startswith(("http://", "https://")):
        raise ValueError("webhook server must be an http(s) URL")
    cfg = {"apiVersion": "v1", "kind": "Config", "current-context": "frx-reference",
           "clusters": [{"name": "frx-reference", "cluster": {"server": server_url}}],
           "users": [{"name": "apiserver", "user": {}}],
           "contexts": [{"name": "frx-reference", "context": {"cluster": "frx-reference", "user": "apiserver"}}]}
    return yaml.safe_dump(cfg, sort_keys=False)


def render_kind_config(repo: Path, stores: Path, sizes: StoreSizes,
                       node_image: str = KIND_V0_33_DEFAULT_IMAGE,
                       audit_webhook_kubeconfig: Path | None = None) -> str:
    """kind Cluster config with a size-rotated, file-backed audit log and host-visible stores.

    With audit_webhook_kubeconfig, the API server also sends every audit event (same policy) to
    the reference receiver in batches of at most 1 s; the log backend is unchanged.
    """
    sizes.check()
    if "@sha256:" not in node_image:
        raise ValueError("node image must be pinned by @sha256 digest")
    repo, stores = Path(repo).resolve(), Path(stores).resolve()
    for p in (repo, stores):
        if str(p).startswith("/mnt/"):
            raise ValueError(f"{p} is on a Windows mount; use the WSL filesystem")
    patch = yaml.safe_dump({
        "kind": "ClusterConfiguration",
        "apiServer": {
            # map style: kind v0.32+ translates to the kubeadm v1beta4 list format
            "extraArgs": {
                "audit-policy-file": POLICY_IN_NODE,
                "audit-log-path": AUDIT_FILE_IN_NODE,
                "audit-log-format": "json",
                "audit-log-maxsize": str(sizes.audit_max_mb),
                "audit-log-maxbackup": str(sizes.audit_backups),
                "audit-log-maxage": "365",   # age-based deletion effectively disabled
                **({"audit-webhook-config-file": WEBHOOK_KUBECONFIG_IN_NODE,
                    "audit-webhook-mode": "batch",
                    "audit-webhook-batch-max-wait": "1s"} if audit_webhook_kubeconfig else {}),
            },
            "extraVolumes": [
                {"name": "audit-policies", "hostPath": "/etc/kubernetes/policies",
                 "mountPath": "/etc/kubernetes/policies", "readOnly": True,
                 "pathType": "DirectoryOrCreate"},
                {"name": "audit-logs", "hostPath": AUDIT_DIR_IN_NODE,
                 "mountPath": AUDIT_DIR_IN_NODE, "readOnly": False,
                 "pathType": "DirectoryOrCreate"},
            ],
        },
    }, sort_keys=False)
    cfg = {
        "kind": "Cluster",
        "apiVersion": "kind.x-k8s.io/v1alpha4",
        "name": CLUSTER_NAME,
        "nodes": [{
            "role": "control-plane",
            "image": node_image,
            "kubeadmConfigPatches": [patch],
            "extraMounts": [
                {"hostPath": str(repo / "cluster" / "audit-policy.yaml"),
                 "containerPath": POLICY_IN_NODE, "readOnly": True},
                *([{"hostPath": str(Path(audit_webhook_kubeconfig).resolve()),
                    "containerPath": WEBHOOK_KUBECONFIG_IN_NODE, "readOnly": True}]
                  if audit_webhook_kubeconfig else []),
                {"hostPath": str(stores / "audit"), "containerPath": AUDIT_DIR_IN_NODE},
                {"hostPath": str(stores / "tetragon"), "containerPath": TETRAGON_DIR_IN_NODE},
                # Tetragon on kind reads host processes through /procHost (Tetragon quick start)
                {"hostPath": "/proc", "containerPath": "/procHost"},
            ],
        }],
    }
    header = ("# Rendered by `frx cluster-config` (Blueprint v3.0). Do not edit by hand.\n"
              f"# Store sizes provisional until G3: {sizes.provisional}\n")
    return header + yaml.dump(cfg, Dumper=_Dumper, sort_keys=False)


def render_tetragon_values(sizes: StoreSizes) -> str:
    """Helm values: JSON export file is the source-side store T (Section 3.1)."""
    sizes.check()
    values = {
        "exportDirectory": TETRAGON_DIR_IN_NODE,          # top-level key in the chart
        "tetragon": {
            "hostProcPath": "/procHost",
            "enableK8sAPI": True,                         # pod metadata on process events
            "exportFilename": TETRAGON_FILE,
            "exportFileMaxSizeMB": sizes.tetragon_max_mb,
            "exportFileMaxBackups": sizes.tetragon_backups,
            "exportFileCompress": False,
            "exportFilePerm": "644",                      # readable by the acquisition user
            "exportAllowList": '{"event_set":["PROCESS_EXEC","PROCESS_EXIT"]}',
            # exportDenyList left at the chart default; recorded via `helm get values --all`
        },
    }
    header = ("# Rendered by `frx cluster-config` (Blueprint v3.0). Do not edit by hand.\n"
              f"# Store sizes provisional until G3: {sizes.provisional}\n")
    return header + yaml.safe_dump(values, sort_keys=False)
