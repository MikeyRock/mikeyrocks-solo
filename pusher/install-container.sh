#!/usr/bin/env bash
# Run the stats pusher in a Docker container (auto-restarts, survives umbrelOS updates).
# Usage: sudo bash install-container.sh
set -euo pipefail
cd "$(dirname "$0")"
docker build -q -t mikeyrocks-solo-pusher . >/dev/null
docker rm -f mikeyrocks-solo-pusher >/dev/null 2>&1 || true
docker run -d --name mikeyrocks-solo-pusher --restart unless-stopped \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /home/umbrel:/home/umbrel \
  mikeyrocks-solo-pusher >/dev/null
# the container replaces the systemd timer
systemctl disable --now neon-solo.timer >/dev/null 2>&1 || true
rm -f /etc/systemd/system/neon-solo.timer /etc/systemd/system/neon-solo.service
systemctl daemon-reload || true
echo "running:"; docker ps --filter name=mikeyrocks-solo-pusher --format '  {{.Names}}  {{.Status}}'
