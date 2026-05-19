#!/usr/bin/env bash
# Manual end-to-end smoke against staging.
# Requires: env CLAW_ADMIN_API_URL + CLAW_ADMIN_TOKEN set; `claw-admin` on PATH.
set -euo pipefail

: "${CLAW_ADMIN_API_URL:?set CLAW_ADMIN_API_URL}"
: "${CLAW_ADMIN_TOKEN:?set CLAW_ADMIN_TOKEN}"
: "${SMOKE_USER:?set SMOKE_USER (email or user_id)}"

echo "==> /health"
curl -fsS "$CLAW_ADMIN_API_URL/health" | jq .

echo "==> claw-admin container list"
claw-admin container list | head -20

echo "==> resolve $SMOKE_USER"
USER_ID=$(claw-admin container status "$SMOKE_USER" | jq -r '.user_id // .container_id // empty')
echo "resolved: $USER_ID"

echo "==> claw-admin config show"
claw-admin config show "$SMOKE_USER" --section llm | jq .

echo "==> claw-admin admin admin-list"
claw-admin admin admin-list

echo "==> claw-admin orphan list"
claw-admin orphan list

echo "==> done"
