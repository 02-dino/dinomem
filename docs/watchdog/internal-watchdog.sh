#!/usr/bin/env bash
# internal-watchdog.sh — on-box OpenClaw gateway health watchdog.
#
# Runs every N minutes via crontab. Pings /health on each openclaw-gateway-*
# systemd user service. If a gateway fails CONSECUTIVE_THRESHOLD checks in a row,
# restarts it via `systemctl --user restart`. Logs all events.
#
# This is the on-box layer. It catches:
#   - Gateway frozen (alive but unresponsive) — Restart=always does NOT catch this
#   - Gateway OOM-killed before systemd reacts
# It does NOT survive a full box freeze (swap thrash so severe even cron stalls).
# For that case, deploy the external watchdog (cloudflare-worker-template.js or
# generic-cron-template.sh) as an additional layer.
#
# Setup:
#   1. Copy this script to a stable path:
#        cp docs/watchdog/internal-watchdog.sh /root/.openclaw/scripts/gateway-watchdog.sh
#        chmod +x /root/.openclaw/scripts/gateway-watchdog.sh
#
#   2. Add to crontab (runs every 5 minutes):
#        */5 * * * * bash /root/.openclaw/scripts/gateway-watchdog.sh >> /root/.openclaw/logs/gateway-watchdog.log 2>&1
#
#   3. Optional env overrides (set in environment or at top of this script):
#        WATCHDOG_HEALTH_TIMEOUT   — curl timeout per check in seconds (default: 5)
#        WATCHDOG_CONSECUTIVE      — failures before restart (default: 2)
#        WATCHDOG_STATE_DIR        — where to store failure-count state (default: /tmp/dinomem_watchdog)
#        WATCHDOG_EXTRA_PORTS      — space-separated extra ports to check beyond auto-discovered services
#                                    e.g. "18789 19789" for plain-process gateways not registered as services
#
#   4. Signal to doctor.sh that internal watchdog is configured:
#        echo "DINOMEM_WATCHDOG_CONFIGURED=1" >> ~/.openclaw/gateway.systemd.env
#
# How it generalizes:
#   - Auto-discovers all openclaw-gateway-*.service via systemctl --user
#   - Falls back to WATCHDOG_EXTRA_PORTS for hosts that run gateways as plain processes
#   - Works with 1 agent or 10 agents — no hardcoded port list
#   - State files in WATCHDOG_STATE_DIR track consecutive failures per service/port
#   - Restart via systemctl --user (user services) or kill+nohup (plain processes)

set -uo pipefail

TIMEOUT="${WATCHDOG_HEALTH_TIMEOUT:-5}"
CONSECUTIVE="${WATCHDOG_CONSECUTIVE:-2}"
STATE_DIR="${WATCHDOG_STATE_DIR:-/tmp/dinomem_watchdog}"
EXTRA_PORTS="${WATCHDOG_EXTRA_PORTS:-}"
LOG_PREFIX="[gateway-watchdog] $(date '+%Y-%m-%d %H:%M:%S')"

mkdir -p "$STATE_DIR"

# ── Helpers ───────────────────────────────────────────────────────────────────

log()  { echo "$LOG_PREFIX $*"; }
ok()   { echo "$LOG_PREFIX [ok] $*"; }
warn() { echo "$LOG_PREFIX [WARN] $*"; }
fail() { echo "$LOG_PREFIX [FAIL] $*"; }

get_failures() {
  local key="$1"
  local f="$STATE_DIR/${key}.failures"
  [ -f "$f" ] && cat "$f" || echo 0
}

set_failures() {
  local key="$1" count="$2"
  echo "$count" > "$STATE_DIR/${key}.failures"
}

check_health() {
  local url="$1"
  curl -sf --max-time "$TIMEOUT" "$url" >/dev/null 2>&1
}

restart_service() {
  local svc="$1"
  warn "Restarting $svc via systemctl --user restart"
  if XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" \
     systemctl --user restart "$svc" 2>/dev/null; then
    ok "Restarted $svc"
    return 0
  else
    fail "systemctl --user restart $svc failed"
    return 1
  fi
}

restart_port() {
  # Fallback for plain-process gateways: find and kill the process, let systemd
  # or the original launcher recover it. Logs the PID for audit.
  local port="$1"
  local pid
  pid=$(lsof -ti :"$port" 2>/dev/null | head -1)
  if [ -n "$pid" ]; then
    warn "Port $port: killing frozen process $pid"
    kill -9 "$pid" 2>/dev/null && ok "Killed $pid on port $port" || fail "kill $pid failed"
  else
    warn "Port $port: no process found (already dead?)"
  fi
}

# ── Discover systemd user services ───────────────────────────────────────────

declare -A SVC_PORT  # service_name -> port

if command -v systemctl >/dev/null 2>&1; then
  while IFS= read -r svc; do
    [ -z "$svc" ] && continue
    # extract port from FragmentPath (ExecStart ...--port NNNN)
    frag=$(XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" \
           systemctl --user show "$svc" -p FragmentPath 2>/dev/null | cut -d= -f2)
    [ -z "$frag" ] || [ ! -f "$frag" ] && continue
    port=$(grep -oP '(?<=--port )\d+' "$frag" 2>/dev/null | head -1)
    [ -z "$port" ] && continue
    SVC_PORT["$svc"]="$port"
  done < <(XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" \
           systemctl --user list-unit-files --type=service --plain --no-legend 2>/dev/null \
           | grep "^openclaw-gateway" | grep -v "watchdog\|config-guard\|autosnapshot\|autocommit" \
           | awk '{print $1}')
fi

# ── Check each discovered service ────────────────────────────────────────────

for svc in "${!SVC_PORT[@]}"; do
  port="${SVC_PORT[$svc]}"
  key="${svc%.service}"
  url="http://127.0.0.1:${port}/health"

  if check_health "$url"; then
    ok "$svc (port $port): healthy"
    set_failures "$key" 0
  else
    failures=$(( $(get_failures "$key") + 1 ))
    set_failures "$key" "$failures"
    warn "$svc (port $port): health check failed ($failures/$CONSECUTIVE)"
    if [ "$failures" -ge "$CONSECUTIVE" ]; then
      fail "$svc: $failures consecutive failures — restarting"
      restart_service "$svc" && set_failures "$key" 0
    fi
  fi
done

# ── Check extra ports (plain-process fallback) ────────────────────────────────

for port in $EXTRA_PORTS; do
  key="port_${port}"
  url="http://127.0.0.1:${port}/health"

  if check_health "$url"; then
    ok "port $port: healthy"
    set_failures "$key" 0
  else
    failures=$(( $(get_failures "$key") + 1 ))
    set_failures "$key" "$failures"
    warn "port $port: health check failed ($failures/$CONSECUTIVE)"
    if [ "$failures" -ge "$CONSECUTIVE" ]; then
      fail "port $port: $failures consecutive failures — attempting restart"
      restart_port "$port" && set_failures "$key" 0
    fi
  fi
done

# ── Summary ───────────────────────────────────────────────────────────────────

total=$(( ${#SVC_PORT[@]} + $(echo $EXTRA_PORTS | wc -w) ))
log "checked $total gateway(s)"
