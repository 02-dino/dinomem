#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# OpenClaw Config Guard (dinomem feature)
# Keeps openclaw.json ALWAYS valid on disk. If any write leaves the JSON broken
# (most common: a trailing comma), the file is reverted from the last known-good
# snapshot BEFORE the gateway can reload the broken config and crash-loop.
#
# WHY this exists: openclaw.json is the gateway's rulebook. A syntax-broken write
# makes the gateway fail to load config on the next reload/restart; with
# systemd Restart=always that becomes a crash-loop and every channel dies until
# the file is hand-fixed. (Real incident: 2026-07-15 — an agent left a trailing
# comma; gateway restart-looped until fixed.) This guard is an INDEPENDENT
# watchdog: it runs at the OS/systemd level, OUTSIDE OpenClaw, so it still works
# while OpenClaw itself is crashing/hanging/restarting.
#
# SAFE-BY-DESIGN contract:
#   - Auto-restore ONLY on JSON *syntax* breakage (a real, unambiguous crash risk).
#   - Syntax OK but `openclaw config validate` (schema) fails -> WARN-log only,
#     NEVER restore (do not silently undo a legitimate valid edit).
#   - Needs >=1 good snapshot (.guard-good) to restore; the snapshot refreshes
#     automatically every time the config is seen valid.
#
# TESTABLE: every path is env-overridable (GUARD_CFG/GUARD_GOOD/GUARD_LOG/
# GUARD_LOCK/GUARD_SETTLE) so it can be exercised against a FAKE file without
# ever touching the real config. install.sh runs that fake-file test before
# enabling anything.
# ---------------------------------------------------------------------------
set -uo pipefail

# Wide PATH so tools resolve when launched by systemd (minimal env). Adjust if
# your jq/flock/openclaw live elsewhere (see `command -v jq flock openclaw`).
export PATH="/usr/local/bin:/usr/bin:/bin:/home/linuxbrew/.linuxbrew/bin:$HOME/.linuxbrew/bin:$HOME/.local/bin:$HOME/.nvm/current/bin:$PATH"

OPENCLAW_DIR="${OPENCLAW_DIR:-$HOME/.openclaw}"
REAL_CFG="$OPENCLAW_DIR/openclaw.json"
CFG="${GUARD_CFG:-$REAL_CFG}"
GOOD="${GUARD_GOOD:-${CFG}.guard-good}"
LOG="${GUARD_LOG:-$OPENCLAW_DIR/logs/config-guard.log}"
LOCK="${GUARD_LOCK:-$OPENCLAW_DIR/locks/config-guard.lock}"
SETTLE="${GUARD_SETTLE:-2}"

mkdir -p "$(dirname "$LOG")" "$(dirname "$LOCK")" 2>/dev/null || true
log() { echo "$(date '+%Y-%m-%dT%H:%M:%S%z') $*" >> "$LOG"; }

# Single instance at a time (a burst of writes must not race N guards).
exec 9>"$LOCK" || exit 0
flock -n 9 || exit 0

[ -e "$CFG" ] || { log "SKIP: $CFG does not exist"; exit 0; }

# --- settle: wait for the write to finish (avoid a false alarm on a half file) ---
sig1="$(stat -c '%Y:%s' "$CFG" 2>/dev/null || echo a)"
sleep "$SETTLE"
sig2="$(stat -c '%Y:%s' "$CFG" 2>/dev/null || echo b)"
if [ "$sig1" != "$sig2" ]; then
  log "SKIP: file still being written (not settled) — wait for next trigger"
  exit 0
fi

# --- JSON syntax check (THIS is the core guard) ---
if jq empty "$CFG" 2>/dev/null; then
  cp -f "$CFG" "$GOOD"                          # valid -> refresh good snapshot
  # Extra (real config only): schema-level validate is WARN-only, never restore.
  if [ "$CFG" = "$REAL_CFG" ] && command -v openclaw >/dev/null 2>&1; then
    if ! timeout 20 openclaw config validate >/dev/null 2>&1; then
      log "WARN: syntax OK but 'config validate' (schema) failed — NOT restored, inspect manually"
    fi
  fi
  exit 0
fi

# --- syntax BROKEN -> restore from last good ---
if [ ! -s "$GOOD" ]; then
  log "ERROR: $CFG is BROKEN but no good snapshot exists ($GOOD) — CANNOT restore!"
  exit 1
fi
ts="$(date '+%Y%m%d-%H%M%S')"
cp -f "$CFG" "${CFG}.broken-${ts}" 2>/dev/null || true   # keep broken copy for forensics
cp -f "$GOOD" "$CFG"
log "RESTORED: $CFG was broken (syntax error) -> reverted from snapshot. Broken saved: ${CFG}.broken-${ts}"
exit 0
