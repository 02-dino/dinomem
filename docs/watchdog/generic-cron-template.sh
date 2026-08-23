#!/usr/bin/env bash
# generic-cron-template.sh — External watchdog for OpenClaw gateway.
#
# Run this on a DIFFERENT machine from the gateway box (e.g. a VPS, home server,
# or another cloud VM). It pings the gateway health endpoint and SSH-restarts the
# service when the gateway is unresponsive for CONSECUTIVE_THRESHOLD checks.
#
# Install on monitor box:
#   1. Copy this file to /usr/local/bin/openclaw-watchdog.sh
#   2. chmod +x /usr/local/bin/openclaw-watchdog.sh
#   3. Edit variables below
#   4. crontab -e → add:
#        */5 * * * * bash /usr/local/bin/openclaw-watchdog.sh >> /var/log/openclaw-watchdog.log 2>&1
#   5. On the gateway box, signal configured:
#        echo "DINOMEM_WATCHDOG_CONFIGURED=1" >> /etc/openclaw/gateway.env
#
# State file: /tmp/openclaw_watchdog_failures  (integer, reset to 0 on success)
# No external dependencies beyond curl and ssh.

set -uo pipefail

# ── Configuration — edit these ────────────────────────────────────────────────
DINOMEM_GATEWAY_URL="${DINOMEM_GATEWAY_URL:-https://your-gateway.example.com}"
SSH_HOST="${SSH_HOST:-your-gateway-box-ip}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_rsa}"
SSH_USER="${SSH_USER:-root}"
SSH_PORT="${SSH_PORT:-22}"
# Restart command executed on the gateway box via SSH
RESTART_CMD="${RESTART_CMD:-systemctl restart openclaw-analyst}"
# Number of consecutive failures (each check = 5 min default) before restart
CONSECUTIVE_THRESHOLD="${CONSECUTIVE_THRESHOLD:-3}"
HEALTH_TIMEOUT_SECS="${HEALTH_TIMEOUT_SECS:-8}"
STATE_FILE="/tmp/openclaw_watchdog_failures"

# ── Logging ───────────────────────────────────────────────────────────────────
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [watchdog] $*"; }

# ── Health check ──────────────────────────────────────────────────────────────
HEALTH_URL="${DINOMEM_GATEWAY_URL%/}/health"
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -m "$HEALTH_TIMEOUT_SECS" "$HEALTH_URL" 2>/dev/null || echo "000")

if [ "$HTTP_STATUS" = "200" ]; then
  log "PASS: gateway healthy (HTTP 200) at $HEALTH_URL"
  echo "0" > "$STATE_FILE"
  exit 0
fi

# ── Failure: increment counter ────────────────────────────────────────────────
PREV=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
CURRENT=$(( PREV + 1 ))
echo "$CURRENT" > "$STATE_FILE"
log "FAIL: gateway returned HTTP $HTTP_STATUS (consecutive_failures=$CURRENT, threshold=$CONSECUTIVE_THRESHOLD)"

if [ "$CURRENT" -lt "$CONSECUTIVE_THRESHOLD" ]; then
  log "Waiting for threshold — no action yet."
  exit 0
fi

# ── Threshold reached: trigger restart ───────────────────────────────────────
log "THRESHOLD REACHED ($CURRENT >= $CONSECUTIVE_THRESHOLD) — restarting via SSH"
SSH_OPTS="-i $SSH_KEY -p $SSH_PORT -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes"
if ssh $SSH_OPTS "${SSH_USER}@${SSH_HOST}" "$RESTART_CMD" 2>&1; then
  log "SSH restart command succeeded: $RESTART_CMD"
  # Reset counter after triggering restart
  echo "0" > "$STATE_FILE"
else
  log "ERROR: SSH restart command failed — check SSH_HOST=$SSH_HOST SSH_KEY=$SSH_KEY SSH_USER=$SSH_USER"
  # Do NOT reset counter — keep retrying on next cron tick if SSH itself is the issue
fi
