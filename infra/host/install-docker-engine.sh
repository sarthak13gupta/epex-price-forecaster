#!/usr/bin/env bash
# Installs Docker Engine natively into this WSL2 distro, replacing Docker
# Desktop's WSL integration.
#
# WHY: Docker Desktop exposes the CLI as a symlink into its own mount
#   /usr/bin/docker -> /mnt/wsl/docker-desktop/cli-tools/usr/bin/docker
# so when Desktop stops, `docker` vanishes with "command not found" rather than
# failing to connect. A native engine is a real binary managed by systemd, needs
# no GUI running, and is Apache-2.0 licensed (Desktop requires a paid
# subscription above certain company size/revenue thresholds — check yours).
#
# BEFORE RUNNING:
#   Docker Desktop → Settings → Resources → WSL Integration → turn OFF for this
#   distro. Leaving it on means two engines competing for /var/run/docker.sock.
#
# Usage:  sudo ./infra/host/install-docker-engine.sh
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }

. /etc/os-release
CODENAME="${VERSION_CODENAME:?cannot determine Ubuntu codename}"
echo "==> Ubuntu ${VERSION_ID} (${CODENAME})"

# systemd must be PID 1, otherwise `systemctl enable --now docker` fails with a
# cryptic "System has not been booted with systemd" and this script aborts
# mid-install. WSL2 does not enable it by default.
if [ "$(ps -p 1 -o comm=)" != "systemd" ]; then
  cat <<'NOSYSTEMD'
ERROR: systemd is not PID 1 in this distro, so the docker service cannot be
       managed. Enable it first:

         sudo tee /etc/wsl.conf >/dev/null <<'EOF'
         [boot]
         systemd=true
         EOF

       Then from Windows PowerShell:  wsl --shutdown
       Reopen the distro and re-run this script.
NOSYSTEMD
  exit 1
fi
echo "==> systemd is PID 1"

if [ -d /mnt/wsl/docker-desktop ]; then
  echo
  echo "WARNING: /mnt/wsl/docker-desktop is still mounted."
  echo "         Turn OFF Docker Desktop's WSL integration for this distro first,"
  echo "         then re-run. Continuing anyway in 10s (Ctrl-C to abort)..."
  sleep 10
fi

echo "==> removing stale Docker Desktop symlinks"
for f in /usr/bin/docker /usr/bin/docker-compose /usr/bin/docker-credential-desktop; do
  [ -L "$f" ] && { rm -f "$f"; echo "    removed symlink $f"; }
done

echo "==> adding Docker's apt repository"
apt-get update -qq
apt-get install -y -qq ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.list <<LIST
deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${CODENAME} stable
LIST

echo "==> installing engine, CLI, buildx and the compose plugin"
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
                       docker-buildx-plugin docker-compose-plugin

echo "==> enabling the service (systemd is on in this distro, so it persists)"
systemctl enable --now docker

# Let the invoking user run docker without sudo. Takes effect on next login.
TARGET_USER="${SUDO_USER:-}"
if [ -n "$TARGET_USER" ] && [ "$TARGET_USER" != "root" ]; then
  usermod -aG docker "$TARGET_USER"
  echo "==> added ${TARGET_USER} to the docker group"
fi

echo
echo "==> verifying"
docker version --format '    client {{.Client.Version}} / server {{.Server.Version}}' || true
docker run --rm hello-world >/dev/null 2>&1 \
  && echo "    hello-world ran OK" \
  || echo "    hello-world FAILED — check: systemctl status docker"

cat <<'NEXT'

Done. Two things to finish:

  1. Group membership needs a new session. Either:
       newgrp docker
     or, from Windows PowerShell:
       wsl --shutdown          # then reopen the distro

  2. Confirm the CLI is now a real binary, not a Desktop symlink:
       ls -la $(which docker)  # expect /usr/bin/docker as a regular file

  You can now quit Docker Desktop permanently. Start/stop the engine with:
       sudo systemctl start docker   /   sudo systemctl stop docker
NEXT
