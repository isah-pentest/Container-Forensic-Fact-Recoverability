#!/usr/bin/env bash
# Mount the off-host replica (SMB share on the Windows host) at /mnt/frx-replica and self-test it
# (Blueprint v3.0, Section 15.3). Re-run after any VM or host reboot: the Hyper-V Default Switch
# subnet changes on host reboot, so the host address is read from the VM's default route each time.
#
# Usage: ./scripts/mount_replica.sh      env: FRX_REPLICA (/mnt/frx-replica), FRX_REPLICA_SHARE (frx-replica),
#                                             FRX_REPLICA_CRED (/root/.frx-replica.cred, mode 600)
set -euo pipefail
MNT="${FRX_REPLICA:-/mnt/frx-replica}"
SHARE="${FRX_REPLICA_SHARE:-frx-replica}"
CRED="${FRX_REPLICA_CRED:-/root/.frx-replica.cred}"
HOST="$(ip route | awk '/^default/ {print $3; exit}')"
command -v mount.cifs >/dev/null || { echo "install cifs-utils first: sudo apt-get install -y cifs-utils"; exit 2; }
sudo test -f "$CRED" || { echo "missing $CRED (username=, password=, domain=)"; exit 2; }
[ "$(sudo stat -c %a "$CRED")" = "600" ] || { echo "$CRED must be mode 600"; exit 2; }
sudo mkdir -p "$MNT"
if mountpoint -q "$MNT"; then
  echo "already mounted: $(findmnt -no SOURCE "$MNT")"
else
  # SMB 3.1.1 with encryption (seal); cache=none so verification reads come from the server
  sudo mount -t cifs "//$HOST/$SHARE" "$MNT" \
    -o "credentials=$CRED,uid=$(id -u),gid=$(id -g),vers=3.1.1,seal,cache=none,file_mode=0644,dir_mode=0755"
  echo "mounted //$HOST/$SHARE at $MNT"
fi
T="$MNT/.frx-selftest-$$"
head -c 4194304 /dev/urandom > "/tmp/frx-selftest-$$"
cp "/tmp/frx-selftest-$$" "$T" && sync
A="$(sha256sum < "/tmp/frx-selftest-$$")"; B="$(sha256sum < "$T")"
rm -f "$T" "/tmp/frx-selftest-$$"
[ "$A" = "$B" ] && echo "self-test OK: 4 MiB written and read back with identical sha256" \
                || { echo "self-test FAILED: hash mismatch"; exit 3; }
df -h "$MNT" | tail -1
