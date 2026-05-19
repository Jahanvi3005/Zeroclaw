#!/usr/bin/env bash
# Fallback deploy via rsync — use when GitHub is unavailable.
# Usage: ./deploy.sh
set -euo pipefail

HOST="clawgateway@34.51.178.206"
REMOTE_DIR="~/zeroproxy"

echo "==> Syncing files..."
rsync -avz --delete \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='.env' \
  --exclude='.git' \
  --exclude='temp/' \
  --exclude='sessions-*.tar' \
  --exclude='.codex' \
  --exclude='docs/' \
  --exclude='.env.old' \
  ./ "$HOST:$REMOTE_DIR/"

echo "==> Installing deps & restarting service..."
ssh "$HOST" "cd $REMOTE_DIR && ~/.local/bin/uv sync && sudo systemctl restart claw-proxy"

echo "==> Checking service status..."
ssh "$HOST" "sudo systemctl status claw-proxy --no-pager -l | head -20"
