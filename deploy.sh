#!/usr/bin/env bash
# Deploy the bot to an Ubuntu server.
# Usage: ./deploy.sh <server-ip> [ssh-user] [ssh-key]
#   ssh-user defaults to "ubuntu" (Oracle Cloud); use "root" for most other VPS hosts.
# Copies code + .env, installs deps, and (re)starts the systemd service.
set -euo pipefail

HOST="${1:?usage: ./deploy.sh <server-ip> [ssh-user] [ssh-key]}"
SSH_USER="${2:-ubuntu}"
KEY="${3:-$HOME/.ssh/id_ed25519_oracle}"
SSH_OPTS="-i $KEY -o StrictHostKeyChecking=accept-new"
SSH="ssh $SSH_OPTS $SSH_USER@$HOST"
APP=/opt/cryptobot

cd "$(dirname "$0")"
[ -f .env ] || { echo "Missing .env"; exit 1; }

echo "==> Preparing server"
$SSH "set -e
  export DEBIAN_FRONTEND=noninteractive
  if ! command -v rsync >/dev/null || ! python3 -m venv --help >/dev/null 2>&1 || ! dpkg -s python3-venv >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv rsync
  fi
  id cryptobot >/dev/null 2>&1 || sudo useradd --system --home $APP --shell /usr/sbin/nologin cryptobot
  sudo mkdir -p $APP"

echo "==> Copying files"
rsync -az --rsync-path="sudo rsync" -e "ssh $SSH_OPTS" \
  crypto_bot.py trades.py requirements.txt deploy/cryptobot.service "$SSH_USER@$HOST":$APP/
rsync -az --rsync-path="sudo rsync" --chmod=F600 -e "ssh $SSH_OPTS" \
  .env "$SSH_USER@$HOST":$APP/.env

echo "==> Installing dependencies and starting service"
$SSH "set -e
  cd $APP
  [ -d venv ] || sudo python3 -m venv venv
  sudo venv/bin/pip install -q --upgrade pip
  sudo venv/bin/pip install -q -r requirements.txt
  sudo chown -R cryptobot:cryptobot $APP
  sudo cp cryptobot.service /etc/systemd/system/cryptobot.service
  sudo systemctl daemon-reload
  sudo systemctl enable -q cryptobot
  sudo systemctl restart cryptobot
  sleep 8
  sudo systemctl --no-pager --lines=15 status cryptobot"

echo "==> Done. Logs: ssh $SSH_OPTS $SSH_USER@$HOST sudo journalctl -u cryptobot -f"
