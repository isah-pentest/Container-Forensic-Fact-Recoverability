# Series R runbook (k = 2 robustness extension)

Work in `~/frx-public` with its virtual environment. Every series-R file outside the repository goes
under `~/frx-evidence/robustness/`; the v3.0.1 trees are not written (attempts are appended to the
shared attempt ledger, as every cohort's are). Long steps run inside tmux.

## 1. k = 2 cluster (archives the current stores into the series-R tree)
```bash
cd ~/frx-public && source .venv/bin/activate
export FRX_EVIDENCE=~/frx-evidence/robustness
mkdir -p "$FRX_EVIDENCE/qualification"
./scripts/g1_cluster_down.sh
# tmux window "ref": the audit reference receiver
python3 -m frx.cli ref-receiver --listen 172.18.0.1:9880
# main window:
FRX_AUDIT_MAX_MB=10 FRX_AUDIT_BACKUPS=2 FRX_TETRAGON_MAX_MB=10 FRX_TETRAGON_BACKUPS=2 FRX_SIZES_FINAL=1 \
  FRX_AUDIT_WEBHOOK=http://172.18.0.1:9880 ./scripts/g1_cluster_up.sh 1.7.1
docker exec frx-control-plane grep -- "--audit-log-maxbackup" /etc/kubernetes/manifests/kube-apiserver.yaml   # =2
helm get values tetragon -n kube-system --all -o json | python3 -c "import json,sys;print(json.load(sys.stdin)['tetragon']['exportFileMaxBackups'])"   # 2
./scripts/g2_harness_identity.sh
./scripts/mount_replica.sh
```

## 2. Fingerprint, levels, capacity run at R4, store check
```bash
sudo env KUBECONFIG="$HOME/.kube/config" PATH="$PATH" .venv/bin/python -m frx.cli fingerprint \
  --out "$FRX_EVIDENCE/qualification/fingerprint_k2.json"            # note "stable_hash"
python -m frx.robustness levels --load-map ~/frx-evidence/qualification/load_map.json   # "problems": []
LOADIMG=$(python -c 'import yaml;print(yaml.safe_load(open("protocol/parameters.yaml"))["load"]["image_digest"])')
# R4 per-replica settings from the "levels" output:
FRX_LOAD_IMAGE="$LOADIMG" FRX_LOAD_REPLICAS=18 FRX_LOAD_EXEC=<R4 exec_per_s> FRX_LOAD_API=<R4 api_per_s> \
  FRX_CAPACITY_LABEL=R-k2-R4 FRX_CAPACITY_OUT="$FRX_EVIDENCE/qualification" ./scripts/g1_capacity_run.sh 20
CAP=$(ls -d "$FRX_EVIDENCE"/qualification/capacity_* | tail -1)
python -m frx.robustness store-check "$CAP" --out "$FRX_EVIDENCE/qualification/store_check.json"   # "pass": true
```

## 3. Plan, freeze, registration (before any series-R run)
```bash
python -m frx.robustness plan --load-map ~/frx-evidence/qualification/load_map.json \
  --fingerprint-hash <stable_hash> --capacity-report "$CAP/capacity_report.json" \
  --store-check "$FRX_EVIDENCE/qualification/store_check.json"
git add protocol/robustness/plan.json protocol/robustness/schedule.csv && git commit -m "Series R: plan and schedule"
python -m frx.robustness freeze
sha256sum protocol/robustness/FREEZE_R.json          # register this SHA-256 on OSF (hash commitment)
git add protocol/robustness/FREEZE_R.json && git commit -m "Series R: freeze"
python -m frx.robustness register --url <OSF URL> --registered-at <ISO time of the OSF registration>
git add protocol/robustness/registration_R.json && git commit -m "Series R: registration"
git tag -a series-R-frozen -m "Series R frozen and registered"
python -m frx.robustness verify                      # "ok": true
```

## 4. Run, score, analyse
```bash
./scripts/robustness_session.sh                      # in tmux; 16 delayed runs, about 40 min
python -m frx.robustness score
python -m frx.robustness analyze
```

## 5. Afterwards
The k = 1 cluster is needed only to collect new v3.0.1 data, which is not planned. To restore it:
`FRX_EVIDENCE=~/frx-evidence/robustness ./scripts/g1_cluster_down.sh`, then `g1_cluster_up.sh` with
`FRX_AUDIT_BACKUPS=1 FRX_TETRAGON_BACKUPS=1`.
