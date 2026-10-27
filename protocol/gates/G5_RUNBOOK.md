# Gate G5 — statistical freeze: runbook

Blueprint v3.0, Sections 8.2, 12.5, 16, 18, 22.2. No cluster needed. Run on the VM from
`~/frx-recoverability` with the venv active; commit after every step so the order is on record.
Rules fixed beforehand: `protocol/gates/G5.md`; scenarios: `protocol/power/scenarios.json`.

## 1. Search (deterministic; a few minutes)
```bash
python3 -m frx.cli power --scenarios protocol/power/scenarios.json --seed 20261027 \
  --primary pilot_informed --conservative conservative --out protocol/power/search.csv \
  | python3 -c "import sys,json; print('chosen_B =', json.load(sys.stdin)['chosen_B'])"
```

## 2. Confirm at the chosen B (2,000 × 2,000; repeat with the next grid B if it fails)
```bash
python3 -m frx.cli power-confirm --b <chosen_B> --seed 20261028
```
Pass = pilot_informed ≥ 0.80, conservative ≥ 0.70, null_calibration type-I in 0.035–0.065.

## 3. Fix B, then commit
```bash
python3 -m frx.cli g5-set-blocks --n-blocks <B>
git add -A && git commit -m "G5: power search and confirmation; B fixed"
```

## 4. Master seed from the NIST Randomness Beacon (after B is committed), then the schedule
```bash
curl -s https://beacon.nist.gov/beacon/2.0/pulse/last > ~/nist_pulse.json
python3 -m frx.cli seed-record --pulse-json ~/nist_pulse.json      # writes protocol/master_seed.json
SEED=$(python3 -c "import json;print(json.load(open('protocol/master_seed.json'))['master_seed'])")
python3 -m frx.cli schedule --seed "$SEED" --blocks <B>             # protocol/randomization_schedule.csv
git add -A && git commit -m "G5: master seed (NIST beacon) and randomization schedule"
```

## 5. Freeze, then tag
```bash
pytest -q
python3 -m frx.cli freeze-check          # nothing unresolved
python3 -m frx.cli freeze                # protocol/FREEZE.json
git add -A && git commit -m "G5: FREEZE.json"
git tag -a protocol-v3.0-frozen -m "Protocol v3.0 frozen"
```

## 6. Register (before the first RUN- ID)
Upload `protocol/FREEZE.json`, the blueprint, `contract.yaml`, `parameters.yaml`, the schedule and
`protocol/power/` to the registry (e.g. OSF Registries, embargo until publication), then record it:
```bash
python3 -m frx.cli register --venue "OSF Registries" --url <registration URL> --registered-at <timestamp shown>
git add -A && git commit -m "G5: registration recorded"
python3 -m frx.cli freeze-verify         # ok = true: RUN- IDs may now be issued
```
