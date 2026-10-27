# frx-recoverability

Protocol tooling for **Forensic Fact Recoverability After Pod Termination: Effects of Acquisition Timing and Background Event Load on Retained Kubernetes Audit and eBPF Telemetry** — Blueprint Manual v3.0 (design-locked), in `protocol/`.

**Status:** `g1-platform` (G1 passed 2026-10-26; see `protocol/gates/G1.md`). Design locked; parameters are set at gates G1–G5 by the rules in Blueprint Section 22. No confirmatory (`RUN-`) IDs may be issued until `protocol/FREEZE.json` exists.

## Ground rules

- **Self-contained.** This repository was initialized empty. Nothing is copied from any earlier repository, run, dataset or script, and no earlier result is an input to any step.
- **No machine learning.** Operating points come from a closed-form formula, randomization is a seeded PRNG, reconstruction is a deterministic rule-based matcher, and inference uses randomization and classical statistics.
- **Evidence never enters git.** Evidence lives in a write-once store with an off-host replica (Blueprint Section 15). `evidence/` and `stores/` are git-ignored.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock && pip install -e .
pytest                      # 47 tests on synthetic fixtures
frx freeze-check            # lists what still blocks freeze (expected: G1-G5 placeholders)
frx headroom --m 1000000 --k 1 --elapsed 60   # Section 4.3 operating-point arithmetic
```

## Study host: Ubuntu Server VM on Hyper-V

**G1 finding (2026-10-26):** a WSL2 distro runs in a nested PID namespace, so Tetragon cannot attribute processes in new pods to their pod there. The study therefore runs in a full Ubuntu Server VM (or on native Linux). `scripts/g1_preflight.sh` now fails on any nested PID namespace.

1. Windows (Administrator PowerShell): save the BitLocker recovery key, enable Hyper-V, reboot.
2. Copy the Ubuntu Server ISO to `C:\HyperV\` and run `scripts/hyperv_create_vm.ps1 -Iso <iso>`
   (Gen 2, Secure Boot off, 12 GB static memory, 8 vCPUs, checkpoints disabled; the configuration
   is written to `C:\HyperV\frx-ubuntu-config.json`).
3. Install Ubuntu Server with OpenSSH; connect from VS Code with the **Remote - SSH** extension.
4. In the VM: clone the repo bundle, then `./scripts/host_bootstrap_ubuntu.sh` (pinned kind, kubectl,
   helm, Docker Engine, Python environment; automatic upgrades paused during the study).
5. `./scripts/g1_preflight.sh ~/frx-evidence`, then `./scripts/g1_cluster_up.sh 1.7.1` and the smoke test.

Shut WSL down (`wsl --shutdown`) while the VM runs: both need about 12 GB of the PC's RAM.

## Running on Windows with VS Code + WSL2 (tooling only; not a valid study host)

The repository **and** the evidence store must live on the WSL Linux filesystem (e.g. `~/frx-recoverability`, `~/frx-evidence`), never under `C:\` or OneDrive (`/mnt/c/...`). Windows mounts do not enforce the read-only locks, are much slower for many small files, and OneDrive sync can rewrite or hold files while they are being hashed.

1. Move the zip into WSL and unpack it there (from a WSL terminal):
   ```bash
   cp "/mnt/c/Users/<you>/OneDrive - <org>/Downloads/frx-recoverability.zip" ~/
   cd ~ && unzip frx-recoverability.zip && cd frx-recoverability
   git log --oneline --decorate        # expect the g0-scaffold tag
   git config core.autocrlf input      # keep LF line endings
   ```
2. Open it in VS Code through WSL: `code .` (requires the WSL extension). The window title should show `[WSL: <distro>]`.
3. Python environment inside WSL: see Quick start.
4. Size WSL in `%UserProfile%\.wslconfig` (Windows side), then run `wsl --shutdown` from PowerShell and reopen. Record this file in the evidence store at G1:
   ```ini
   [wsl2]
   memory=12GB
   processors=8
   swap=0
   ```
5. Run the read-only pre-flight and keep its report:
   ```bash
   mkdir -p ~/frx-evidence/software
   ./scripts/g1_preflight.sh ~/frx-evidence | tee ~/frx-evidence/software/preflight_$(date -u +%Y%m%dT%H%M%SZ).txt
   ```
   Run it as your normal user, not root. Fix every `FAIL` before the G1 capacity run.
6. During sessions: plug in power, disable Windows sleep and hibernation, pause OneDrive sync and Windows Update. After any sleep, check the WSL clock (`sudo hwclock -s` or `wsl --shutdown`) and re-run the pre-flight.
7. Off-host replica (Section 15.3): use a separate physical drive or machine. A OneDrive folder is acceptable only as a replica target set to "Always keep on this device", because hash verification must read every byte locally.

## What is implemented now vs. at gates

| Module | Status | Blueprint section |
| --- | --- | --- |
| `frx.metrics` — union rule, Y, C, cell proportions, Δ, block contrasts, F2 tables | Done, tested | 11 |
| `frx.stats` — randomization test (exact / Monte Carlo), block t CI, MOVER-Wilson, Wilson, Clopper–Pearson, McNemar, fallback trigger | Done, tested | 12 |
| `frx.power` — Monte Carlo power grid and the B-selection rule | Done, tested against the exact binomial value | 12.5 |
| `frx.randomization` — seeded five-slot block schedule | Done, tested | 8.2 |
| `frx.headroom` — H, predicted loss, operating-point rates | Done, tested | 4 |
| `frx.integrity` — write-once files, manifests, read-only lock, package and block gates, ledger | Done, tested | 15 |
| `frx.status` — disposition rules and `run_status.json` schema | Done, tested | 10 |
| `frx.contract` — contract/parameter checks and freeze readiness | Done, tested | 6, 7, 22 |
| `frx.harness`, `frx.acquisition`, `frx.orchestrator`, `frx.reconstruct` | Interfaces only; build and test on fixtures, then qualify | 7, 3, 9/19, 7 → G1–G3 |
| `frx.cluster`, `frx.fingerprint`, `frx.smoke` — rendered kind/Tetragon config, platform fingerprint, G1 smoke test | Done, tested (rendering, analysis) | 3, 6, 14 → G1 |
| `cluster/` — audit policy, target pod, load generator | Drafts frozen at G2/G3 | 3, 6 |

## Gate workflow

| Gate | What you do | Repo commands / files | Tag |
| --- | --- | --- | --- |
| G0 | Literature matrix; this protocol | `protocol/` | `g0-scaffold` |
| G1 | Pre-flight; bring up cluster; smoke test; capacity run at provisional ceiling (20 min) | `scripts/g1_preflight.sh`, `scripts/g1_cluster_up.sh`, `frx smoke`, `frx fingerprint` | `g1-platform` |
| G2 + G3 | Qualification series Q (33 `QUAL-` runs); fix contract details, store capacity, rates | `frx headroom ...`; fill `<G2>`/`<G3>` values | `g3-qualified` |
| G4 | 3 pilot blocks (`PILOT-`) | `frx verify-package`, `frx verify-block` | `g4-pilot` |
| G5 | Power simulation; seed; schedule; FREEZE.json; registration | `frx power ...`, `frx schedule ...`, `frx freeze-check` must exit 0 | `protocol-v3.0-frozen` |
| G6 | Confirmatory blocks, then calibration series (`CAL-`) | orchestrator; gates after every run and block | — |
| G7 | Clean-environment regeneration, ≥ 10% raw-to-fact audit | `./reproduce.sh <evidence>` ; `frx analyze` | `g7-analysis` |
| G8 | Sanitized release | `release/` in the evidence store | `g8-release` |

## Analysis input format

`frx analyze scores.csv --seed <frozen>` expects one row per scored confirmatory run:

```csv
run_id,block,cell,Y
RUN-0001,BLOCK-01,LD,1
```

Controls (`NC`) may be present and are excluded automatically. It reports cell proportions with Wilson intervals, H1 (randomization p, block t CI or the structure-triggered MOVER-Wilson CI), H2 under the fixed-sequence rule, and the exploratory interaction.

## Notes for negative-control runs

For `NC` runs, `action_verified` means the harness verified that **no** action was performed, and `termination_verified` is true by definition. Controls are never in the denominator of Y.

## Licence

To be chosen by the authors before public release (Blueprint Section 22.2).
