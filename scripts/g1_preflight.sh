#!/usr/bin/env bash
# G1 pre-flight (Blueprint v3.0, Sections 14.1-14.2). READ-ONLY: changes nothing, installs nothing.
# Checks that this WSL2 / Linux host can run the protocol, and prints a report to keep in
# the evidence store (software/preflight_<timestamp>.txt). Exit 0 = no FAIL lines.
#
# Usage:  ./scripts/g1_preflight.sh [EVIDENCE_DIR]   (default: ~/frx-evidence)
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
EVID="${1:-$HOME/frx-evidence}"
FAILS=0
ok()   { printf 'OK    %s\n' "$*"; }
warn() { printf 'WARN  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILS=$((FAILS+1)); }
have() { command -v "$1" >/dev/null 2>&1; }

echo "FRX G1 pre-flight  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "repo: $REPO   evidence: $EVID"
echo "commit: $(git -C "$REPO" describe --tags --always --dirty 2>/dev/null || echo n/a)"
echo

echo "== Host and kernel"
KREL="$(uname -r)"; echo "kernel: $KREL"
if grep -qi microsoft /proc/version 2>/dev/null; then
  ok "running under WSL2 (record .wslconfig and 'wsl --version' output from Windows)"
else
  ok "native Linux"
fi
if [ -r /sys/kernel/btf/vmlinux ]; then ok "kernel BTF present (/sys/kernel/btf/vmlinux)"; \
else fail "kernel BTF missing: Tetragon needs BTF; use a kernel built with CONFIG_DEBUG_INFO_BTF"; fi
if [ -r /proc/config.gz ]; then
  for k in CONFIG_BPF_SYSCALL CONFIG_DEBUG_INFO_BTF CONFIG_KPROBES CONFIG_BPF_EVENTS CONFIG_FTRACE_SYSCALLS; do
    v="$(zcat /proc/config.gz | grep -E "^$k=" | cut -d= -f2)"
    [ "$v" = "y" ] && ok "$k=y" || warn "$k=${v:-unset} (confirm Tetragon works at G1)"
  done
else
  warn "/proc/config.gz not available; kernel options cannot be listed here"
fi
INIT="$(ps -p 1 -o comm= 2>/dev/null)"
[ "$INIT" = "systemd" ] && ok "systemd is PID 1" || warn "PID 1 is '$INIT' (enable systemd in /etc/wsl.conf if Docker Engine runs inside WSL)"

echo
echo "== Filesystem placement (write-once evidence needs a native Linux filesystem)"
for p in "$REPO" "$EVID"; do
  case "$p" in
    /mnt/*) fail "$p is on a Windows drive (/mnt/...). Move it into the WSL filesystem, e.g. ~/";;
    *OneDrive*) fail "$p is inside OneDrive. Sync interferes with write-once files and hashes";;
    *) ;;
  esac
done
FST="$(stat -f -c %T "$REPO" 2>/dev/null)"
case "$FST" in
  ext2/ext3|xfs|btrfs) ok "repo filesystem: $FST";;
  *) fail "repo filesystem '$FST' (9p/v9fs/drvfs = Windows mount; use ~/ inside WSL)";;
esac
mkdir -p "$EVID" 2>/dev/null || true
if [ -d "$EVID" ]; then
  FREE_GB=$(( $(df -P -k "$EVID" | awk 'NR==2{print $4}') / 1024 / 1024 ))
  [ "$FREE_GB" -ge 50 ] && ok "free space at evidence store: ${FREE_GB} GB" \
                        || warn "free space at evidence store: ${FREE_GB} GB (size properly at G4, Section 14.4)"
  T="$EVID/.preflight_perm_test.$$"
  if echo x > "$T" && chmod a-w "$T" && ! ( echo y >> "$T" ) 2>/dev/null; then
    ok "read-only lock is enforced on this filesystem"
  else
    if [ "$(id -u)" = "0" ]; then warn "running as root: read-only lock cannot be tested (root ignores it); run as your normal user"
    else fail "read-only permission is not enforced at $EVID"; fi
  fi
  chmod u+w "$T" 2>/dev/null; rm -f "$T"
fi

echo
echo "== Resources visible to WSL / Linux"
MEM_GB=$(awk '/MemTotal/{printf "%d", $2/1024/1024}' /proc/meminfo)
CPUS=$(nproc)
echo "memory: ${MEM_GB} GB   vCPUs: ${CPUS}"
[ "$MEM_GB" -ge 12 ] && ok "memory >= 12 GB" || warn "memory ${MEM_GB} GB: raise 'memory=' in %UserProfile%\\.wslconfig (capacity run at G1 decides)"
[ "$CPUS" -ge 4 ] && ok "vCPUs >= 4" || warn "only ${CPUS} vCPUs visible"
SWAP_GB=$(awk '/SwapTotal/{printf "%d", $2/1024/1024}' /proc/meminfo); echo "swap: ${SWAP_GB} GB (record it; swapping during runs distorts timing)"

echo
echo "== Tools"
for t in python3 git docker kind kubectl helm; do
  if have "$t"; then
    case "$t" in
      python3) v="$(python3 --version 2>&1)";;
      git) v="$(git --version)";;
      docker) v="$(docker --version 2>&1)";;
      kind) v="$(kind version 2>&1)";;
      kubectl) v="$(kubectl version --client 2>&1 | head -1)";;
      helm) v="$(helm version --short 2>&1)";;
    esac
    ok "$t: $v"
  else
    fail "$t not found"
  fi
done
if have docker; then
  if docker info >/dev/null 2>&1; then
    ok "docker daemon reachable ($(docker info --format '{{.OperatingSystem}} / cgroup {{.CgroupVersion}}' 2>/dev/null))"
  else
    fail "docker daemon not reachable (start Docker Engine in WSL, or enable WSL integration in Docker Desktop)"
  fi
fi
python3 - <<'EOF' 2>/dev/null && ok "python packages importable" || fail "python packages missing: pip install -r requirements.lock && pip install -e ."
import numpy, scipy, yaml, jsonschema  # noqa
EOF

echo
echo "== Clock"
if have timedatectl && timedatectl show >/dev/null 2>&1; then
  echo "timedatectl: $(timedatectl show -p NTPSynchronized -p Timezone 2>/dev/null | tr '\n' ' ')"
fi
if have powershell.exe; then
  W=$(powershell.exe -NoProfile -Command "[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()" 2>/dev/null | tr -d '\r')
  L=$(date +%s%3N)
  if [ -n "$W" ]; then
    D=$(( L - W )); echo "WSL minus Windows clock: ${D} ms (includes ~0.3-1 s PowerShell start-up)"
    [ "${D#-}" -lt 2000 ] && ok "clock skew small" || fail "WSL clock differs from Windows by ${D} ms (after sleep/hibernate: 'sudo hwclock -s' or restart WSL)"
  fi
else
  warn "powershell.exe not reachable; check WSL clock against Windows manually"
fi

echo
echo "== Protocol state"
( cd "$REPO" && PYTHONPATH="$REPO/src" python3 -m frx.cli freeze-check >/dev/null 2>&1 ); rc=$?
[ $rc -eq 2 ] && ok "freeze-check: placeholders remain (expected before G5)" || echo "freeze-check exit $rc"

echo
if [ "$FAILS" -eq 0 ]; then echo "RESULT: no FAIL items. Save this report under $EVID/software/."; exit 0
else echo "RESULT: $FAILS FAIL item(s). Fix them before the G1 capacity run."; exit 1; fi
