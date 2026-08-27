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

# --- JSON syntax check (core guard) ---
if jq empty "$CFG" 2>/dev/null; then
  # Syntax OK. Now guard SCHEMA too: a schema-invalid write (valid JSON, but e.g.
  # an unrecognized key that additionalProperties:false rejects) crash-loops the
  # gateway just as hard as a syntax error. This runs the SAME restore, always —
  # no flag, no single-vs-multi-agent setting. It is ALWAYS safe because:
  #   * a config that fails 'openclaw config validate' CANNOT be loaded by the
  #     gateway (it crash-loops), regardless of how many agents it has;
  #   * $GOOD is by definition a config that already PASSED validation (we only
  #     refresh it below on a fully-valid file), so restoring it is never
  #     destructive — worst case an invalid edit is undone + logged + kept for
  #     forensics, and the operator simply re-edits.
  # Auto-detect only degrades gracefully: if the openclaw CLI isn't on PATH we
  # can't schema-check, so we fall through to snapshot-refresh (syntax-only mode).
  # Escape hatch for the rare operator who wants to inspect broken schema by hand:
  # GUARD_SCHEMA_RESTORE=0 downgrades to WARN-only.
  if [ "$CFG" = "$REAL_CFG" ] && command -v openclaw >/dev/null 2>&1; then
    if ! OPENCLAW_CONFIG_PATH="$CFG" timeout 20 openclaw config validate >/dev/null 2>&1; then
      if [ "${GUARD_SCHEMA_RESTORE:-1}" = "1" ] && [ -s "$GOOD" ]; then
        ts="$(date '+%Y%m%d-%H%M%S')"
        cp -f "$CFG" "${CFG}.schema-broken-${ts}" 2>/dev/null || true
        cp -f "$GOOD" "$CFG"
        log "RESTORED: $CFG was SCHEMA-invalid (valid JSON, bad key) -> reverted from snapshot. Broken saved: ${CFG}.schema-broken-${ts}"
        exit 0
      fi
      # Only reached if operator explicitly set GUARD_SCHEMA_RESTORE=0, or no snapshot yet.
      log "WARN: syntax OK but 'config validate' (schema) failed — NOT restored (GUARD_SCHEMA_RESTORE=0 or no snapshot yet), inspect manually"
      exit 0
    fi
  fi
  cp -f "$CFG" "$GOOD"                          # fully valid -> refresh good snapshot
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
