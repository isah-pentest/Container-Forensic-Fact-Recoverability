# Gate G4 — pilot: runbook

Blueprint v3.0, Sections 12.4, 14.3–14.4, 15.3–15.4, 18. Three pilot blocks (`PILOT-` IDs, stored
under `qualification/pilot/`, never confirmatory), each with LC, LD, MC, MD and one negative control
in seeded order, one load Deployment per block, replicas verified off-host before the next block.

## 0. Values that must be fixed first (G3)
`frx pilot-run` refuses while any non-G5 placeholder remains (`frx freeze-check`):
- `timing.control_idle_s` — NC action duration = median ACTION_REQUEST → TERMINATION_CONFIRMED in
  Q-a/Q-b (`frx qual-report`, field `G2.action_duration_s.control_idle_s_rule`);
- `host.fingerprint_stable_hash` — platform fingerprint collected exactly as the pilot collects it:
  `sudo env KUBECONFIG=$HOME/.kube/config PATH=$PATH .venv/bin/python -m frx.cli fingerprint --out ~/frx-evidence/qualification/g3/fingerprint_platform.json`

## 1. Off-host replica: SMB share on the Windows host (once)
Windows, **administrator** PowerShell on the workstation. A dedicated local account keeps your SHSU
password off the VM; the share and the firewall rule are limited to that account and the VM switch.
```powershell
$pw = Read-Host -AsSecureString "new password for frxrepl"
New-LocalUser -Name frxrepl -Password $pw -PasswordNeverExpires -UserMayNotChangePassword -Description "FRX replica share (VM only)"
New-Item -ItemType Directory -Path C:\frx-replica | Out-Null
icacls C:\frx-replica /inheritance:r /grant:r "frxrepl:(OI)(CI)M" "Administrators:(OI)(CI)F" "SYSTEM:(OI)(CI)F"
New-SmbShare -Name frx-replica -Path C:\frx-replica -FullAccess frxrepl -EncryptData $true
New-NetFirewallRule -DisplayName "FRX replica SMB from Hyper-V VM" -Direction Inbound -Protocol TCP -LocalPort 445 `
  -InterfaceAlias "vEthernet (Default Switch)" -RemoteAddress LocalSubnet -Action Allow
$env:COMPUTERNAME
```
If `New-LocalUser` or the firewall rule is refused, a domain policy blocks it: ask SHSU IT, or use a
second virtual disk instead and record that choice.

VM:
```bash
sudo apt-get install -y cifs-utils
sudo install -m 600 /dev/null /root/.frx-replica.cred
sudo nano /root/.frx-replica.cred      # three lines: username=frxrepl  password=<it>  domain=<COMPUTERNAME>
./scripts/mount_replica.sh             # mounts at /mnt/frx-replica and self-tests (4 MiB, sha256)
```
Re-run `./scripts/mount_replica.sh` after any reboot. Never write to C:\frx-replica by hand.

## 2. Pilot schedule (written once)
```bash
python3 -m frx.cli pilot-plan --seed <recorded number>
```

## 3. Run the pilot (≈ 35 min; inside tmux; audit receiver running in its own window)
```bash
./scripts/g2_harness_identity.sh                     # if the 12 h token may have expired
FRX_REPLICA=/mnt/frx-replica ./scripts/g4_pilot.sh   # all 3 blocks in one session
```
Each block: deploy load (60 s warm-up) → 5 slots → replacements of aborted/unscorable attempts at the
block end (≤ 2 per cell) → delete load → copy to the replica and verify every hash → next block.
A stop rule (package gate, ceiling, two OOMs in a row, replica mismatch, fingerprint change) ends the
session; an interrupted block is recorded INCOMPLETE and never resumed.

## 4. Score and evaluate
```bash
python3 -m frx.cli pilot-score
python3 -m frx.cli pilot-report
```
G4 passes with zero false attribution (NC + decoys), the ceiling respected, 100 % package gates,
3/3 blocks replicated and verified. The report also gives the inputs to G5: cell estimates
(x/n), write rates, δ_obs, package sizes (→ `disk_free_min_bytes` = 3 × max + 10 GB), block durations,
and whether the outcomes are deterministic (Section 12.4: H1 is withdrawn before freeze if so).
