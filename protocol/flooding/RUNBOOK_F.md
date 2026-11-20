# Series F runbook (post-termination flood, k = 1, frx-hv)

Work in the repository on branch `flooding-series` with its virtual environment. Every series-F file
outside the repository goes under `~/frx-evidence-F/`. The v3.0.1 and series-R evidence, the 31 October
snapshot and `C:\frx-replica` are not used. Long steps run inside tmux.

## 0. Windows host (once): the series-F replica share
Create `C:\frx-replica-F`, share it as `frx-replica-F` for the same local account as `frx-replica`, and do
not reuse the `frx-replica` share.

## 1. k = 1 cluster
```bash
source .venv/bin/activate
export FRX_EVIDENCE=~/frx-evidence-F
mkdir -p "$FRX_EVIDENCE/flooding/qualification"
./scripts/g1_cluster_down.sh
# tmux window "ref": the audit reference receiver
python3 -m frx.cli ref-receiver --listen 172.18.0.1:9880
# main window:
FRX_AUDIT_MAX_MB=10 FRX_AUDIT_BACKUPS=1 FRX_TETRAGON_MAX_MB=10 FRX_TETRAGON_BACKUPS=1 FRX_SIZES_FINAL=1 \
  FRX_AUDIT_WEBHOOK=http://172.18.0.1:9880 ./scripts/g1_cluster_up.sh 1.7.1
docker exec frx-control-plane grep -- "--audit-log-maxbackup" /etc/kubernetes/manifests/kube-apiserver.yaml   # =1
helm get values tetragon -n kube-system --all -o json | python3 -c "import json,sys;print(json.load(sys.stdin)['tetragon']['exportFileMaxBackups'])"   # 1
./scripts/g2_harness_identity.sh
FRX_REPLICA=/mnt/frx-replica-F FRX_REPLICA_SHARE=frx-replica-F ./scripts/mount_replica.sh
```

## 2. Fingerprint and G1 (stop if G1 fails)
```bash
sudo env KUBECONFIG="$HOME/.kube/config" PATH="$PATH" .venv/bin/python -m frx.cli fingerprint \
  --out "$FRX_EVIDENCE/flooding/qualification/fingerprint_F.json"         # note "stable_hash"
python -m frx.flooding levels | grep -A1 '"problems"'                     # []
sudo env KUBECONFIG="$HOME/.kube/config" PATH="$PATH" .venv/bin/python -m frx.flooding g1 \
  --stores ~/frx-stores --evidence "$FRX_EVIDENCE"                         # "pass": true (about 10 min)
sudo chown -R "$(id -u):$(id -g)" "$FRX_EVIDENCE"
```

## 3. Capacity run at F3 (CAL4) and store check
```bash
LOADIMG=$(python -c 'import yaml;print(yaml.safe_load(open("protocol/parameters.yaml"))["load"]["image_digest"])')
FRX_LOAD_IMAGE="$LOADIMG" FRX_LOAD_REPLICAS=18 FRX_LOAD_EXEC=5.892 FRX_LOAD_API=18.933 \
  FRX_CAPACITY_LABEL=F-k1-CAL4 FRX_CAPACITY_OUT="$FRX_EVIDENCE/flooding/qualification" ./scripts/g1_capacity_run.sh 20
CAP=$(ls -d "$FRX_EVIDENCE"/flooding/qualification/capacity_* | tail -1)
python -m frx.flooding store-check "$CAP" --out "$FRX_EVIDENCE/flooding/qualification/store_check.json"   # "pass": true
```

## 4. Plan, freeze, tag, push (before any counted run)
```bash
Q="$FRX_EVIDENCE/flooding/qualification"
python -m frx.flooding plan --g1 "$Q/g1_F.json" --fingerprint-hash <stable_hash> \
  --capacity-report "$CAP/capacity_report.json" --store-check "$Q/store_check.json"
git add protocol/flooding/plan.json protocol/flooding/schedule.csv && git commit -m "Series F: plan and schedule"
python -m frx.flooding freeze
git add protocol/flooding/FREEZE_F.json && git commit -m "Series F: freeze"
git tag -a series-F-frozen -m "Series F frozen"
git push origin flooding-series series-F-frozen       # the public timestamp of the freeze
python -m frx.flooding verify                          # "ok": true
```

## 5. Run, score, analyse
```bash
./scripts/flooding_session.sh                          # in tmux; 26 runs
python -m frx.flooding score --evidence ~/frx-evidence-F
python -m frx.flooding analyze --evidence ~/frx-evidence-F
```
Deviations are recorded in `protocol/flooding/DEVIATIONS_F.md` and committed.
