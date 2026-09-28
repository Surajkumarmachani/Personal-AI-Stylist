#!/usr/bin/env bash
# Compute Engine startup script for stylist-vm. Runs as root on EVERY boot, so
# every step is idempotent. Installs Docker once, adds swap, and sends
# container logs to Cloud Logging. The app itself is started by update.sh
# (on deploy); on a reboot Docker restarts the containers by itself
# (restart: unless-stopped).
set -euo pipefail

# Swap as a safety margin on a 4 GB box: a burst that would otherwise OOM-kill
# litellm or the api slows down instead.
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon -a || true

if ! command -v docker >/dev/null 2>&1; then
  apt-get update -q
  apt-get install -y -q ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $VERSION_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi

# gcplogs: each container's output goes straight to Cloud Logging (free up to
# 50 GB/month) with no agent eating memory, and the JSON lines from
# LOG_FORMAT=json arrive with their severity. Docker's dual logging keeps
# `docker compose logs` working locally too.
if [ ! -f /etc/docker/daemon.json ]; then
  mkdir -p /etc/docker
  cat > /etc/docker/daemon.json <<'JSON'
{ "log-driver": "gcplogs" }
JSON
  systemctl restart docker
fi
systemctl enable --now docker

mkdir -p /opt/stylist/secrets
chmod 700 /opt/stylist
touch /opt/stylist/.vm-ready
