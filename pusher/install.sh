#!/usr/bin/env bash
# install.sh — set up the MikeyRocks//Solo stats pusher on umbrelOS.
# Usage:  sudo bash install.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

HOME_DIR=/home/umbrel/neon-solo
REPO_DEFAULT="MikeyRock/mikeyrocks-solo"
SRC="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$HOME_DIR"
install -m 755 "$SRC/neon-push.py" "$HOME_DIR/neon-push.py"

if [ ! -f "$HOME_DIR/config.env" ]; then
  read -rp "GitHub repo [$REPO_DEFAULT]: " REPO; REPO=${REPO:-$REPO_DEFAULT}
  read -rsp "GitHub fine-grained token (Contents: read & write on that repo only): " TOKEN; echo
  umask 077
  cat > "$HOME_DIR/config.env" <<EOF
NEON_REPO=$REPO
NEON_BRANCH=data
GITHUB_TOKEN=$TOKEN
EOF
fi
chmod 600 "$HOME_DIR/config.env"

echo "== dry run (nothing pushed) =="
python3 "$HOME_DIR/neon-push.py" --dry-run | head -n 40

cat > /etc/systemd/system/neon-solo.service <<EOF
[Unit]
Description=Push AxeBTC/AxeBCH stats to GitHub (MikeyRocks//Solo)
After=docker.service network-online.target
[Service]
Type=oneshot
Environment=NEON_HOME=$HOME_DIR
ExecStart=/usr/bin/python3 $HOME_DIR/neon-push.py
EOF

cat > /etc/systemd/system/neon-solo.timer <<EOF
[Unit]
Description=Run MikeyRocks//Solo stats push every 3 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=3min
AccuracySec=15s
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now neon-solo.timer
echo "== first push =="
systemctl start neon-solo.service && journalctl -u neon-solo.service -n 3 --no-pager
echo
echo "Installed. Check anytime:  systemctl status neon-solo.timer   /   journalctl -u neon-solo -n 20"
