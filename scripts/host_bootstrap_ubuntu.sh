#!/usr/bin/env bash
# Prepare a fresh Ubuntu Server VM (or native Ubuntu) as the FRX study host with pinned tools.
# Idempotent: safe to re-run. Every download is checksum-verified before install.
# Usage (inside the VM, from the repo root):  ./scripts/host_bootstrap_ubuntu.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
KIND_VERSION=v0.33.0
KUBECTL_VERSION=v1.37.0
[ "$(uname -m)" = "x86_64" ] || { echo "expects x86_64"; exit 2; }
case "$REPO" in /mnt/*|*OneDrive*) echo "repo must be on the Linux filesystem"; exit 2;; esac

echo "== packages"
sudo apt-get update
sudo apt-get install -y docker.io python3-venv python3-pip git curl jq unzip
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

echo "== kind $KIND_VERSION and kubectl $KUBECTL_VERSION (checksum-verified)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
curl -fsSLo "$tmp/kind" "https://kind.sigs.k8s.io/dl/$KIND_VERSION/kind-linux-amd64"
curl -fsSLo "$tmp/kind.sha256sum" "https://kind.sigs.k8s.io/dl/$KIND_VERSION/kind-linux-amd64.sha256sum"
( cd "$tmp" && echo "$(cut -d' ' -f1 kind.sha256sum)  kind" | sha256sum -c - )
sudo install -m 0755 "$tmp/kind" /usr/local/bin/kind
curl -fsSLo "$tmp/kubectl" "https://dl.k8s.io/release/$KUBECTL_VERSION/bin/linux/amd64/kubectl"
curl -fsSLo "$tmp/kubectl.sha256" "https://dl.k8s.io/release/$KUBECTL_VERSION/bin/linux/amd64/kubectl.sha256"
( cd "$tmp" && echo "$(cat kubectl.sha256)  kubectl" | sha256sum -c - )
sudo install -m 0755 "$tmp/kubectl" /usr/local/bin/kubectl

echo "== helm"
command -v helm >/dev/null || sudo snap install helm --classic

echo "== kernel limits kind needs for many pods (inotify)"
sudo tee /etc/sysctl.d/90-frx-kind.conf >/dev/null <<'SYSCTL'
fs.inotify.max_user_watches=524288
fs.inotify.max_user_instances=512
SYSCTL
sudo sysctl --system >/dev/null

echo "== freeze the platform during the study: stop automatic package upgrades"
# Re-enable after the study with: sudo systemctl enable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades
sudo systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades 2>/dev/null || true

echo "== Python environment"
cd "$REPO"
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q -r requirements.lock
pip install -q -e .
python3 -m pytest -q -p no:cacheprovider | tail -1

echo
echo "Done. Log out and back in (docker group), then:"
echo "  cd $REPO && source .venv/bin/activate && ./scripts/g1_preflight.sh ~/frx-evidence"
