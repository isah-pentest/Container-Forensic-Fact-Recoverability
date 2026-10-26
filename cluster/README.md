# cluster/

| File | Role | Frozen at |
| --- | --- | --- |
| `audit-policy.yaml` | API-server audit policy (RequestResponse for pod create/delete and `pods/exec`) | G3 |
| `manifests/target-pod.yaml` | Target pod template (`sleep infinity`, 1 s grace, no sidecars); image digest set at G2 | G2 |
| `manifests/load-generator.yaml`, `load/loadgen.py` | Persistent rate-controlled background load (Section 6.2) | G3 |

The kind cluster config and Tetragon Helm values are **rendered**, not hand-edited:

```bash
python3 -m frx.cli cluster-config --stores ~/frx-stores --out-dir <dir>
```

`scripts/g1_cluster_up.sh` renders them into the evidence store, creates the cluster, verifies
the audit flags and both stores, installs Tetragon at a pinned chart version and writes a
fingerprint. Store sizes stay provisional (10 MB x 5 backups) until G3 sets them by the
Section 4.3 rule (`--final`), which requires recreating the cluster.
